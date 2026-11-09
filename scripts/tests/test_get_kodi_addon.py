"""Tests for the add-on database handling in files/get_kodi_addon.py."""

import os
import sqlite3
import tempfile
import types
import unittest

from scripts.tests.helpers import get_kodi_addon

KODI_ADDON_DB = get_kodi_addon.KODI_ADDON_DB
Database = get_kodi_addon.Database
Manager = get_kodi_addon.Manager


def shape(connection):
    """Tables and indexes with their columns, independent of SQL spelling."""
    result = {}
    rows = connection.execute(
        "SELECT type, name, tbl_name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for kind, name, table in rows:
        if kind == "table":
            info = connection.execute("PRAGMA table_info({0})".format(name)).fetchall()
            result[name] = (kind, [row[1:] for row in info])
        else:
            info = connection.execute("PRAGMA index_xinfo({0})".format(name)).fetchall()
            unique = connection.execute(
                "SELECT \"unique\" FROM pragma_index_list(?) WHERE name = ?",
                (table, name),
            ).fetchone()[0]
            result[name] = (kind, table, unique, [row[1:] for row in info])
    return result


class PopulateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, "userdata", "Database", name)

    def test_new_database_matches_kodi_ddl(self):
        for n, record in KODI_ADDON_DB["schemas"].items():
            if not record["supported"]:
                continue
            with self.subTest(schema=n):
                db = Database(self.path("Addons{0}.db".format(n)))
                db.populate(n)

                expected = sqlite3.connect(":memory:")
                for statement in record["ddl"]:
                    expected.execute(statement)

                self.assertEqual(shape(db.connection), shape(expected))
                self.assertEqual(
                    db.connection.execute("SELECT idVersion FROM version").fetchall(),
                    [(n,)],
                )

    def test_populate_is_repeatable(self):
        db = Database(self.path("Addons33.db"))
        db.populate(33)
        db.populate(33)
        self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM version").fetchone(), (1,))

    def test_existing_database_is_left_alone(self):
        path = self.path("Addons27.db")
        os.makedirs(os.path.dirname(path))
        existing = sqlite3.connect(path)
        for statement in KODI_ADDON_DB["schemas"][27]["ddl"]:
            existing.execute(statement)
        existing.execute("INSERT INTO version VALUES (27, 0)")
        existing.commit()
        before = shape(existing)
        existing.close()

        db = Database(path)
        db.populate(33)
        self.assertEqual(shape(db.connection), before)
        self.assertEqual(db.connection.execute("SELECT idVersion FROM version").fetchall(), [(27,)])

    def test_idempotent(self):
        self.assertEqual(
            Database.idempotent("CREATE UNIQUE INDEX i ON t(c)"),
            "CREATE UNIQUE INDEX IF NOT EXISTS i ON t(c)",
        )
        self.assertEqual(
            Database.idempotent("CREATE TABLE IF NOT EXISTS t (c)"),
            "CREATE TABLE IF NOT EXISTS t (c)",
        )


class DatabaseVersionTest(unittest.TestCase):
    def version(self, major):
        manager = types.SimpleNamespace(kodi_version=types.SimpleNamespace(major=major))
        return Manager.database_version.fget(manager)

    def test_supported_versions(self):
        self.assertEqual(self.version(18), 27)
        self.assertEqual(self.version(21), 33)

    def test_kodi_16_is_rejected(self):
        with self.assertRaisesRegex(Exception, "unsupported Kodi version: 16 .*installed"):
            self.version(16)

    def test_unknown_versions_are_rejected(self):
        for major in (15, 99):
            with self.subTest(major=major):
                with self.assertRaisesRegex(Exception, "unsupported Kodi version"):
                    self.version(major)


if __name__ == "__main__":
    unittest.main()
