#!/usr/bin/env python3
"""Generate Kodi's add-on database DDL and codename map from Kodi's source.

Kodi creates its add-on database (`userdata/Database/Addons<N>.db`) in C++:
`CAddonDatabase::CreateTables` and `CAddonDatabase::CreateAnalytics` in
`xbmc/addons/AddonDatabase.cpp`, plus the `version` table created by
`CDatabase` in `xbmc/dbwrappers/Database.cpp`.  This script reads those
files at the newest release tag of each Kodi major version and writes:

- the `KODI_ADDON_DB` block in `files/get_kodi_addon.py`: major version ->
  schema version, and each schema's DDL; and
- `vars/kodi_codenames.yml`: major version -> codename, taken from the
  release tag names (e.g. `21.3-Omega`).

Usage:

    scripts/kodi_addon_schema.py --check         # exit 1 if outputs are stale
    scripts/kodi_addon_schema.py --write         # regenerate outputs
    scripts/kodi_addon_schema.py --report        # print a Markdown summary
    scripts/kodi_addon_schema.py --check-master  # exit 1 if Kodi's `master`
                                                 # has a schema no release has

Extraction is strict: anything it cannot read exactly (e.g. SQL built at run
time rather than written as string literals) is an error, not a guess.

Needs only the Python standard library, `git`, and network access to
github.com and raw.githubusercontent.com.
"""

import argparse
import difflib
import json
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_URL = "https://github.com/xbmc/xbmc.git"
RAW_URL = "https://raw.githubusercontent.com/xbmc/xbmc/{ref}/{path}"

ADDON_DB_CPP = "xbmc/addons/AddonDatabase.cpp"
ADDON_DB_H = "xbmc/addons/AddonDatabase.h"
BASE_DB_CPP = "xbmc/dbwrappers/Database.cpp"

# Oldest major version whose add-on database schema is recorded.  Older
# versions are rejected by `get_kodi_addon.py` simply by being absent.
SCHEMA_MIN_MAJOR = 16

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "files" / "get_kodi_addon.py"
CODENAMES_PATH = ROOT / "vars" / "kodi_codenames.yml"

BEGIN_MARKER = "# BEGIN GENERATED KODI_ADDON_DB"
END_MARKER = "# END GENERATED KODI_ADDON_DB"

TAG_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:(?P<pre>a|b|rc)(?P<pre_num>\d+))?-(?P<codename>[A-Za-z]+)$"
)
PRE_ORDER = {"a": 0, "b": 1, "rc": 2}


class ExtractionError(Exception):
    pass


# --- Fetching ---------------------------------------------------------------


def fetch(ref, path, retries=3):
    """Return the text of `path` at `ref`, or None if it does not exist."""
    url = RAW_URL.format(ref=ref, path=path)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** (attempt + 1))


def list_tags():
    out = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", REPO_URL],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line.split("refs/tags/", 1)[1] for line in out.splitlines() if line]


# --- Release selection ------------------------------------------------------


def select_releases(tags):
    """Map each major version to its newest release tag.

    A final release (`21.3-Omega`) beats any pre-release; a major with no
    final release yet uses its newest pre-release (`22.0rc1-Piers`).
    Returns {major: (tag, codename)}.
    """
    best = {}
    for tag in tags:
        m = TAG_RE.match(tag)
        if not m:
            continue
        major = int(m["major"])
        pre = m["pre"]
        key = (
            pre is None,
            int(m["minor"]),
            int(m["patch"] or 0),
            PRE_ORDER.get(pre, 0),
            int(m["pre_num"] or 0),
        )
        if major not in best or key > best[major][0]:
            best[major] = (key, tag, m["codename"].lower())
    return {major: (tag, codename) for major, (_, tag, codename) in best.items()}


# --- C++ scanning -----------------------------------------------------------


def _skip_literal(src, i):
    """Given src[i] == '"' or "'", return the index just past the literal."""
    quote = src[i]
    i += 1
    while src[i] != quote:
        i += 2 if src[i] == "\\" else 1
    return i + 1


def _skip_comment(src, i):
    """If a comment starts at src[i], return the index just past it."""
    if src.startswith("//", i):
        end = src.find("\n", i)
        return len(src) if end < 0 else end
    if src.startswith("/*", i):
        return src.index("*/", i) + 2
    return None


def _match_bracket(src, i):
    """Given src[i] in '({', return the index of its matching bracket."""
    pairs = {"(": ")", "{": "}"}
    stack = [pairs[src[i]]]
    i += 1
    while stack:
        c = src[i]
        if c in "\"'":
            i = _skip_literal(src, i)
            continue
        end = _skip_comment(src, i)
        if end is not None:
            i = end
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c == stack[-1]:
            stack.pop()
        elif c in ")}":
            raise ExtractionError("unbalanced brackets at offset {0}".format(i))
        i += 1
    return i - 1


def function_body(src, cls, name):
    """Return the body of `cls::name(...)`'s definition, or None."""
    m = re.search(
        r"\b{0}::{1}\s*\([^)]*\)\s*(?:const\s*)?(?:override\s*)?\{{".format(cls, name),
        src,
    )
    if not m:
        return None
    start = m.end() - 1
    return src[start + 1 : _match_bracket(src, start)]


def int_method(cpp, header, name):
    """Return the integer a `CAddonDatabase::name()` returns.

    Looks for an out-of-line definition in the .cpp file first, then for an
    inline definition in the header (as in Kodi 16).
    """
    body = function_body(cpp, "CAddonDatabase", name)
    if body is None and header is not None:
        m = re.search(
            r"\b{0}\s*\(\s*\)\s*const\s*(?:override\s*)?\{{([^}}]*)\}}".format(name),
            header,
        )
        body = m and m.group(1)
    m = body and re.fullmatch(r"\s*return\s+(\d+)\s*;\s*", body)
    if not m:
        raise ExtractionError("cannot read CAddonDatabase::{0}()".format(name))
    return int(m.group(1))


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}


def _decode_literal(body):
    def replace(m):
        try:
            return _ESCAPES[m.group(1)]
        except KeyError:
            raise ExtractionError("unsupported escape \\{0}".format(m.group(1)))

    return re.sub(r"\\(.)", replace, body)


def exec_statements(body, where, strict=True):
    """Return the SQL of every `m_pDS->exec(...)` call in `body`.

    With `strict`, every call must pass only string literals (which C++
    concatenates); anything else, such as `PrepareSQL(...)`, is an error.
    Without it, such calls are skipped.
    """
    statements = []
    for m in re.finditer(r"m_pDS\s*->\s*exec\s*\(", body):
        open_paren = m.end() - 1
        args = body[open_paren + 1 : _match_bracket(body, open_paren)]
        parts = []
        i = 0
        while i < len(args):
            end = _skip_comment(args, i)
            if end is not None:
                i = end
            elif args[i].isspace():
                i += 1
            elif args[i] == '"':
                end = _skip_literal(args, i)
                parts.append(_decode_literal(args[i + 1 : end - 1]))
                i = end
            elif strict:
                raise ExtractionError(
                    "{0}: m_pDS->exec() argument is not a string literal: {1}".format(
                        where, " ".join(args.split())
                    )
                )
            else:
                parts = None
                break
        if parts is None:
            continue
        if not parts:
            raise ExtractionError("{0}: empty m_pDS->exec() call".format(where))
        statements.append(normalize_sql("".join(parts)))
    return statements


def normalize_sql(sql):
    return " ".join(sql.split()).rstrip(";").rstrip()


# --- Extraction -------------------------------------------------------------


def has_installed_table(ddl):
    """Whether the schema has the `installed` columns `get_kodi_addon.py` uses."""
    db = sqlite3.connect(":memory:")
    try:
        for statement in ddl:
            db.execute(statement)
        columns = {row[1] for row in db.execute("PRAGMA table_info(installed)")}
    except sqlite3.Error as e:
        raise ExtractionError("generated DDL does not run: {0}".format(e))
    finally:
        db.close()
    return {"addonID", "enabled"} <= columns


def extract_from_sources(ref, cpp, header, base_cpp):
    """Extract one schema from the text of the three source files."""
    if cpp is None:
        raise ExtractionError("{0}: {1} not found".format(ref, ADDON_DB_CPP))
    if base_cpp is None:
        raise ExtractionError("{0}: {1} not found".format(ref, BASE_DB_CPP))

    ddl = []
    version_ddl = [
        s
        for s in exec_statements(
            base_cpp, "{0}:{1}".format(ref, BASE_DB_CPP), strict=False
        )
        if s.startswith("CREATE TABLE version ")
    ]
    if len(version_ddl) != 1:
        raise ExtractionError(
            "{0}: expected one `CREATE TABLE version` in {1}, found {2}".format(
                ref, BASE_DB_CPP, len(version_ddl)
            )
        )
    ddl.extend(version_ddl)

    for name in ("CreateTables", "CreateAnalytics"):
        body = function_body(cpp, "CAddonDatabase", name)
        if body is None:
            raise ExtractionError("{0}: CAddonDatabase::{1} not found".format(ref, name))
        statements = exec_statements(body, "{0}:CAddonDatabase::{1}".format(ref, name))
        if not statements:
            raise ExtractionError("{0}: CAddonDatabase::{1} has no SQL".format(ref, name))
        ddl.extend(statements)

    return {
        "schema": int_method(cpp, header, "GetSchemaVersion"),
        "min_schema": int_method(cpp, header, "GetMinSchemaVersion"),
        "ddl": tuple(ddl),
        "supported": has_installed_table(ddl),
    }


def extract(ref, fetcher=fetch):
    return extract_from_sources(
        ref,
        fetcher(ref, ADDON_DB_CPP),
        fetcher(ref, ADDON_DB_H),
        fetcher(ref, BASE_DB_CPP),
    )


def build(releases, fetcher=fetch):
    """Build the data behind both outputs from {major: (tag, codename)}."""
    majors = {}
    schemas = {}
    for major in sorted(m for m in releases if m >= SCHEMA_MIN_MAJOR):
        tag = releases[major][0]
        record = extract(tag, fetcher)
        n = record["schema"]
        majors[major] = n
        if n not in schemas:
            schemas[n] = dict(record, sources=[tag])
            continue
        known = schemas[n]
        for field in ("ddl", "min_schema", "supported"):
            if record[field] != known[field]:
                raise ExtractionError(
                    "schema {0}: {1} differs between {2} and {3}".format(
                        n, field, known["sources"][-1], tag
                    )
                )
        known["sources"].append(tag)
    codenames = {major: codename for major, (_, codename) in releases.items()}
    return {"majors": majors, "schemas": schemas, "codenames": codenames}


# --- Rendering --------------------------------------------------------------


def render_python(data):
    q = json.dumps  # double-quoted, valid Python string literals
    lines = [
        BEGIN_MARKER,
        "# Written by `scripts/kodi_addon_schema.py --write`; do not edit by hand.",
        "# Add-on database DDL as Kodi creates it (`CAddonDatabase::CreateTables`",
        "# and `CreateAnalytics` in xbmc/addons/AddonDatabase.cpp, plus the `version`",
        "# table from xbmc/dbwrappers/Database.cpp), at each `sources` release tag",
        "# of https://github.com/xbmc/xbmc.",
        "KODI_ADDON_DB = {",
        '    "majors": {',
    ]
    lines += ["        {0}: {1},".format(k, v) for k, v in sorted(data["majors"].items())]
    lines += ["    },", '    "schemas": {']
    for n, record in sorted(data["schemas"].items()):
        lines += [
            "        {0}: {{".format(n),
            '            "min_schema": {0},'.format(record["min_schema"]),
            '            "supported": {0},'.format(record["supported"]),
            '            "sources": (',
        ]
        lines += ["                {0},".format(q(s)) for s in record["sources"]]
        lines += ["            ),", '            "ddl": (']
        lines += ["                {0},".format(q(s)) for s in record["ddl"]]
        lines += ["            ),", "        },"]
    lines += ["    },", "}", END_MARKER]
    return "\n".join(lines) + "\n"


def render_codenames(data):
    codenames = data["codenames"]
    lines = [
        "---",
        "# Written by `scripts/kodi_addon_schema.py --write`; do not edit by hand.",
        "# Kodi major version -> codename, from the release tags of",
        "# https://github.com/xbmc/xbmc (e.g. `21.3-Omega`).",
        "",
        "kodi_codenames:",
    ]
    lines += ["  '{0}': {1}".format(k, v) for k, v in sorted(codenames.items())]
    lines += [
        "",
        "# Used for versions newer than any listed above.",
        "kodi_codename_latest: {0}".format(codenames[max(codenames)]),
    ]
    return "\n".join(lines) + "\n"


def inject(text, block):
    start = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if start < 0 or end < start:
        raise ExtractionError("generated-block markers not found")
    end = text.index("\n", end) + 1
    return text[:start] + block + text[end:]


def render_report(releases, data, master=None):
    lines = [
        "| Kodi | Release tag | Codename | Add-on schema | Supported |",
        "| --- | --- | --- | --- | --- |",
    ]
    for major, (tag, codename) in sorted(releases.items()):
        schema = data["majors"].get(major)
        if schema is None:
            lines.append("| {0} | `{1}` | {2} | - | no |".format(major, tag, codename))
        else:
            supported = "yes" if data["schemas"][schema]["supported"] else "no"
            lines.append(
                "| {0} | `{1}` | {2} | {3} | {4} |".format(
                    major, tag, codename, schema, supported
                )
            )
    if master is not None:
        lines += ["", "Kodi `master`: add-on schema {0}.".format(master["schema"])]
    return "\n".join(lines) + "\n"


def master_drift(data, master):
    """Describe how `master`'s schema differs from the newest release's, if at all."""
    newest = data["schemas"][data["majors"][max(data["majors"])]]
    newest_n = data["majors"][max(data["majors"])]
    if master["schema"] != newest_n:
        return "Kodi `master` uses add-on schema {0}; the newest release uses {1}.".format(
            master["schema"], newest_n
        )
    if master["ddl"] != newest["ddl"]:
        diff = "\n".join(
            difflib.unified_diff(
                newest["ddl"], master["ddl"], "release", "master", lineterm=""
            )
        )
        return "Kodi `master` changed schema {0}'s DDL without a version bump:\n{1}".format(
            newest_n, diff
        )
    return None


# --- CLI --------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if outputs are stale")
    mode.add_argument("--write", action="store_true", help="regenerate outputs")
    parser.add_argument("--report", action="store_true", help="print a Markdown summary")
    parser.add_argument(
        "--check-master",
        action="store_true",
        help="exit 1 if Kodi's master branch has a schema no release has",
    )
    args = parser.parse_args(argv)
    if not (args.check or args.write or args.report or args.check_master):
        parser.error("nothing to do; pass --check, --write, --report or --check-master")

    try:
        releases = select_releases(list_tags())
        data = build(releases)
        master = extract("master") if (args.report or args.check_master) else None
    except ExtractionError as e:
        print("error: {0}".format(e), file=sys.stderr)
        return 2

    status = 0
    outputs = [
        (SCRIPT_PATH, inject(SCRIPT_PATH.read_text(), render_python(data))),
        (CODENAMES_PATH, render_codenames(data)),
    ]
    for path, new in outputs:
        old = path.read_text() if path.exists() else ""
        if old == new:
            continue
        rel = str(path.relative_to(ROOT))
        if args.write:
            path.write_text(new)
            print("updated {0}".format(rel), file=sys.stderr)
        elif args.check:
            sys.stdout.writelines(
                difflib.unified_diff(
                    old.splitlines(True), new.splitlines(True), "a/" + rel, "b/" + rel
                )
            )
            status = 1

    if args.report:
        sys.stdout.write(render_report(releases, data, master))

    if args.check_master:
        drift = master_drift(data, master)
        if drift:
            print(drift, file=sys.stderr)
            status = 1

    return status


if __name__ == "__main__":
    sys.exit(main())
