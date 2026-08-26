"""test_dbio.py — unit tests for ghc_db_manager.dbio"""

import pathlib
import sqlite3
import tempfile
import unittest

from ghc_db_manager import dbio
from ghc_db_manager.dbio import (
    copy_db,
    WriteGuard,
    ProtectedTableError,
    DO_NOT_TOUCH_TABLES,
)


def _make_test_db(path: str | pathlib.Path) -> None:
    """Create a tiny SQLite db with protected + scratch tables."""
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE change_logs_table (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE android_metadata (locale TEXT)")
    conn.execute("CREATE TABLE scratch_table (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE activity_date_table (epoch_days INTEGER, record_type_id INTEGER)"
    )
    conn.commit()
    conn.close()


class TestCopyDb(unittest.TestCase):

    def test_copy_db_exact_copy(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            src_path = tf.name

        _make_test_db(src_path)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            dst_path = tf.name

        try:
            copy_db(src_path, dst_path)
            src_size = pathlib.Path(src_path).stat().st_size
            dst_size = pathlib.Path(dst_path).stat().st_size
            self.assertEqual(src_size, dst_size)
            # Verify content
            conn = sqlite3.connect(dst_path)
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            table_names = {r[0] for r in tables}
            self.assertIn("change_logs_table", table_names)
            conn.close()
        finally:
            pathlib.Path(src_path).unlink(missing_ok=True)
            pathlib.Path(dst_path).unlink(missing_ok=True)


class TestWriteGuard(unittest.TestCase):

    def _make_guard(self, path: str | pathlib.Path):
        """Return (WriteGuard, raw_connection) tuple."""
        conn = sqlite3.connect(str(path))
        return WriteGuard(conn), conn

    def test_allows_insert_into_scratch_table(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            _make_test_db(p)
            guard_conn, raw_conn = self._make_guard(p)
            guard_conn.execute("INSERT INTO scratch_table (value) VALUES (?)", ("hello",))
            guard_conn.commit()
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)

    def test_blocks_protected_table_insert(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            _make_test_db(p)
            guard_conn, raw_conn = self._make_guard(p)
            with self.assertRaises(ProtectedTableError) as ctx:
                guard_conn.execute("INSERT INTO change_logs_table (id) VALUES (1)")
            self.assertIn("change_logs_table", str(ctx.exception))
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)

    def test_blocks_protected_table_update(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            _make_test_db(p)
            guard_conn, raw_conn = self._make_guard(p)
            with self.assertRaises(ProtectedTableError):
                guard_conn.execute(
                    "UPDATE change_logs_table SET id=2 WHERE id=1"
                )
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)

    def test_blocks_protected_table_delete(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            _make_test_db(p)
            guard_conn, raw_conn = self._make_guard(p)
            with self.assertRaises(ProtectedTableError):
                guard_conn.execute("DELETE FROM change_logs_table")
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)

    def test_blocks_all_protected_tables(self):
        """Ensure every table in DO_NOT_TOUCH_TABLES is blocked."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            conn = sqlite3.connect(p)
            for t in DO_NOT_TOUCH_TABLES:
                conn.execute(f"CREATE TABLE {t} (id INTEGER)")
            conn.commit()
            conn.close()

            guard_conn, raw_conn = self._make_guard(p)
            for t in DO_NOT_TOUCH_TABLES:
                with self.assertRaises(ProtectedTableError, msg=f"should block {t}"):
                    guard_conn.execute(f"INSERT INTO {t} (id) VALUES (1)")
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)

    def test_blocks_generated_column_insert(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            conn = sqlite3.connect(p)
            conn.execute(
                "CREATE TABLE test_table (id INTEGER, local_date_time TEXT)"
            )
            conn.commit()
            conn.close()

            guard_conn, raw_conn = self._make_guard(p)
            with self.assertRaises(ProtectedTableError) as ctx:
                guard_conn.execute(
                    "INSERT INTO test_table (id, local_date_time) VALUES (1, '2026-01-01')"
                )
            self.assertIn("local_date_time", str(ctx.exception))
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)

    def test_allows_activity_date_table(self):
        """activity_date_table is NOT protected — we must write to it."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            _make_test_db(p)
            guard_conn, raw_conn = self._make_guard(p)
            guard_conn.execute(
                "INSERT INTO activity_date_table (epoch_days, record_type_id) VALUES (20588, 26)"
            )
            guard_conn.commit()
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)

    def test_executemany_blocks_protected(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            _make_test_db(p)
            guard_conn, raw_conn = self._make_guard(p)
            with self.assertRaises(ProtectedTableError):
                guard_conn.executemany(
                    "INSERT INTO change_logs_table (id) VALUES (?)", [(1,), (2,)]
                )
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)

    def test_executescript_blocks_protected(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as tf:
            p = tf.name
        try:
            _make_test_db(p)
            guard_conn, raw_conn = self._make_guard(p)
            with self.assertRaises(ProtectedTableError):
                guard_conn.executescript(
                    "INSERT INTO android_metadata (locale) VALUES ('en_US');"
                )
            raw_conn.close()
        finally:
            pathlib.Path(p).unlink(missing_ok=True)


class TestTableExists(unittest.TestCase):

    def test_table_exists_true(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE real_table (id INTEGER)")
        conn.commit()
        self.assertTrue(dbio.table_exists(conn, "real_table"))
        self.assertFalse(dbio.table_exists(conn, "nonexistent"))
        conn.close()


class TestUserVersion(unittest.TestCase):

    def test_user_version_read(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA user_version = 23")
        self.assertEqual(dbio.user_version(conn), 23)
        conn.close()


if __name__ == "__main__":
    unittest.main()
