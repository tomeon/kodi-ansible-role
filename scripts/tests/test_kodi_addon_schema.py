"""Offline tests for scripts/kodi_addon_schema.py."""

import unittest

from scripts.tests.helpers import get_kodi_addon, schema

BASE_CPP = r"""
void CDatabase::CreateDatabase()
{
  m_pDS->exec("CREATE TABLE version (idVersion integer, iCompressCount integer)\n");
  m_pDS->exec(PrepareSQL("INSERT INTO version VALUES(%i, 0)", GetSchemaVersion()));
}
"""

ADDON_CPP = r"""
int CAddonDatabase::GetMinSchemaVersion() const
{
  return 21;
}

int CAddonDatabase::GetSchemaVersion() const
{
  return 33;
}

void CAddonDatabase::CreateTables()
{
  CLog::Log(LOGINFO, "create installed table");  // a "quoted" comment
  m_pDS->exec("CREATE TABLE installed (id INTEGER PRIMARY KEY, "
              "addonID TEXT UNIQUE, enabled BOOLEAN, installDate TEXT, "  /* inline comment */
              "origin TEXT NOT NULL DEFAULT '')\n");
  m_pDS->exec("CREATE TABLE package (id integer primary key, note text DEFAULT \"x\")\n");
}

void CAddonDatabase::CreateAnalytics()
{
  m_pDS->exec("CREATE UNIQUE INDEX idxPackage ON package(id)");
}
"""

# Kodi 16 declares the version numbers inline in the header.
JARVIS_H = r"""
class CAddonDatabase : public CDatabase
{
protected:
  virtual int GetMinSchemaVersion() const { return 15; }
  virtual int GetSchemaVersion() const { return 20; }
};
"""

JARVIS_CPP = r"""
void CAddonDatabase::CreateTables()
{
  m_pDS->exec("CREATE TABLE disabled (id integer primary key, addonID text)\n");
}

void CAddonDatabase::CreateAnalytics()
{
  m_pDS->exec("CREATE UNIQUE INDEX idxDisabled ON disabled(addonID)\n");
}
"""


class SelectReleasesTest(unittest.TestCase):
    def test_prefers_final_release_and_newest_point_release(self):
        releases = schema.select_releases(
            [
                "21.0-Omega",
                "21.3-Omega",
                "21.2-Omega",
                "21.0rc2-Omega",
                "Frodo-ATV2-iOS6",
                "not-a-release",
            ]
        )
        self.assertEqual(releases, {21: ("21.3-Omega", "omega")})

    def test_uses_newest_prerelease_without_final_release(self):
        releases = schema.select_releases(
            ["22.0a3-Piers", "22.0rc1-Piers", "22.0b2-Piers"]
        )
        self.assertEqual(releases, {22: ("22.0rc1-Piers", "piers")})


class ExtractTest(unittest.TestCase):
    def test_extracts_concatenated_literals_and_escapes(self):
        record = schema.extract_from_sources("ref", ADDON_CPP, None, BASE_CPP)
        self.assertEqual(record["schema"], 33)
        self.assertEqual(record["min_schema"], 21)
        self.assertEqual(
            record["ddl"],
            (
                "CREATE TABLE version (idVersion integer, iCompressCount integer)",
                "CREATE TABLE installed (id INTEGER PRIMARY KEY, addonID TEXT UNIQUE, "
                "enabled BOOLEAN, installDate TEXT, origin TEXT NOT NULL DEFAULT '')",
                'CREATE TABLE package (id integer primary key, note text DEFAULT "x")',
                "CREATE UNIQUE INDEX idxPackage ON package(id)",
            ),
        )
        self.assertTrue(record["supported"])

    def test_reads_versions_from_header_and_flags_missing_installed_table(self):
        record = schema.extract_from_sources("ref", JARVIS_CPP, JARVIS_H, BASE_CPP)
        self.assertEqual((record["schema"], record["min_schema"]), (20, 15))
        self.assertFalse(record["supported"])

    def test_requires_columns_the_script_writes(self):
        cpp = ADDON_CPP.replace("installDate TEXT, ", "")
        record = schema.extract_from_sources("ref", cpp, None, BASE_CPP)
        self.assertFalse(record["supported"])

    def test_rejects_non_literal_sql(self):
        cpp = ADDON_CPP.replace(
            'm_pDS->exec("CREATE UNIQUE INDEX idxPackage ON package(id)");',
            'm_pDS->exec(PrepareSQL("CREATE INDEX %s", name));',
        )
        with self.assertRaisesRegex(schema.ExtractionError, "not a string literal"):
            schema.extract_from_sources("ref", cpp, None, BASE_CPP)

    def test_rejects_missing_function(self):
        cpp = ADDON_CPP.replace("CreateAnalytics", "CreateSomethingElse")
        with self.assertRaisesRegex(schema.ExtractionError, "CreateAnalytics not found"):
            schema.extract_from_sources("ref", cpp, None, BASE_CPP)

    def test_rejects_ddl_sqlite_cannot_run(self):
        cpp = ADDON_CPP.replace("ON package(id)", "ON nosuchtable(id)")
        with self.assertRaisesRegex(schema.ExtractionError, "does not run"):
            schema.extract_from_sources("ref", cpp, None, BASE_CPP)


class BuildTest(unittest.TestCase):
    def fetcher(self, sources):
        def fetch(ref, path):
            return sources[ref].get(path)

        return fetch

    def files(self, cpp, header=None):
        return {
            schema.ADDON_DB_CPP: cpp,
            schema.ADDON_DB_H: header,
            schema.BASE_DB_CPP: BASE_CPP,
        }

    def test_groups_majors_by_schema(self):
        sources = {
            "16.1-Jarvis": self.files(JARVIS_CPP, JARVIS_H),
            "21.3-Omega": self.files(ADDON_CPP),
            "22.0rc1-Piers": self.files(ADDON_CPP),
        }
        releases = {
            15: ("15.2-Isengard", "isengard"),
            16: ("16.1-Jarvis", "jarvis"),
            21: ("21.3-Omega", "omega"),
            22: ("22.0rc1-Piers", "piers"),
        }
        data = schema.build(releases, self.fetcher(sources))
        self.assertEqual(data["majors"], {16: 20, 21: 33, 22: 33})
        self.assertEqual(data["schemas"][33]["sources"], ["21.3-Omega", "22.0rc1-Piers"])
        self.assertEqual(data["codenames"][15], "isengard")
        self.assertIn("kodi_codename_latest: piers\n", schema.render_codenames(data))

    def test_rejects_same_schema_with_different_ddl(self):
        sources = {
            "21.3-Omega": self.files(ADDON_CPP),
            "22.0rc1-Piers": self.files(ADDON_CPP.replace("id integer", "id INTEGER")),
        }
        releases = {21: ("21.3-Omega", "omega"), 22: ("22.0rc1-Piers", "piers")}
        with self.assertRaisesRegex(schema.ExtractionError, "ddl differs"):
            schema.build(releases, self.fetcher(sources))

    def test_master_drift(self):
        data = {
            "majors": {21: 33},
            "schemas": {33: schema.extract_from_sources("r", ADDON_CPP, None, BASE_CPP)},
        }
        same = schema.extract_from_sources("master", ADDON_CPP, None, BASE_CPP)
        self.assertIsNone(schema.master_drift(data, same))
        bumped = dict(same, schema=34)
        self.assertIn("schema 34", schema.master_drift(data, bumped))
        changed = dict(same, ddl=same["ddl"] + ("CREATE TABLE x (y)",))
        self.assertIn("without a version bump", schema.master_drift(data, changed))


class RenderTest(unittest.TestCase):
    def test_inject_replaces_only_the_marked_block(self):
        text = "a\n{0}\nold\n{1}\nb\n".format(schema.BEGIN_MARKER, schema.END_MARKER)
        block = "{0}\nnew\n{1}\n".format(schema.BEGIN_MARKER, schema.END_MARKER)
        self.assertEqual(schema.inject(text, block), "a\n" + block + "b\n")

    def test_inject_requires_markers(self):
        with self.assertRaises(schema.ExtractionError):
            schema.inject("no markers here\n", "x\n")

    def test_committed_block_matches_renderer(self):
        """The block in get_kodi_addon.py is exactly what the renderer writes."""
        db = get_kodi_addon.KODI_ADDON_DB
        data = {
            "majors": db["majors"],
            "schemas": {
                n: dict(record, sources=list(record["sources"]))
                for n, record in db["schemas"].items()
            },
        }
        text = schema.SCRIPT_PATH.read_text()
        start = text.index(schema.BEGIN_MARKER)
        end = text.index("\n", text.index(schema.END_MARKER)) + 1
        self.assertEqual(text[start:end], schema.render_python(data))


if __name__ == "__main__":
    unittest.main()
