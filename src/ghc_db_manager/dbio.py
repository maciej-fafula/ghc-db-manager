"""
dbio.py — Health Connect database I/O helpers.

Provides:
- ``open_readonly()`` — open a db file in read-only mode via sqlite3 + file: URI.
- ``copy_db()`` — shutil.copyfile wrapper.
- ``WriteGuard`` — sqlite3.Connection wrapper that blocks writes to protected
  tables and generated columns.
"""

import pathlib
import shutil
import sqlite3
import re
from typing import Optional

from ghc_db_manager.knowledge import DO_NOT_TOUCH_TABLES, GENERATED_COLUMNS


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ProtectedTableError(RuntimeError):
    """Raised when a WriteGuard connection is asked to write a protected table."""

    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def open_readonly(path: str | pathlib.Path) -> sqlite3.Connection:
    """
    Open a Health Connect export database in read-only mode.

    Uses the sqlite3 ``file:`` URI with ``mode=ro`` so that any accidental
    write attempt raises an error rather than modifying the source file.

    Args:
        path: path to the ``health_connect_export.db`` file.

    Returns:
        A connected sqlite3.Connection (read-only).
    """
    src = str(pathlib.Path(path).resolve())
    # file: URI — mode=ro ensures OS-level read-only enforcement
    uri = f"file:{src}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    return conn


def copy_db(src: str | pathlib.Path, dst: str | pathlib.Path) -> None:
    """
    Copy a database file from ``src`` to ``dst``.

    Uses ``shutil.copyfile`` (not copy) to ensure the destination is an exact
    byte-for-byte copy of the source.
    """
    shutil.copyfile(str(src), str(dst))


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if ``table`` exists in the connected database."""
    cur = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cur.fetchone()[0] > 0


def user_version(conn: sqlite3.Connection) -> int:
    """Return the ``user_version`` PRAGMA value."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


# ---------------------------------------------------------------------------
# WriteGuard
# ---------------------------------------------------------------------------

_PROTECTED_TABLE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in DO_NOT_TOUCH_TABLES) + r")\b",
    re.IGNORECASE,
)

_GENERATED_COL_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(c) for c in GENERATED_COLUMNS) + r")\b",
    re.IGNORECASE,
)


class WriteGuard:
    """
    A sqlite3.Connection wrapper that blocks writes to protected tables
    and generated columns.

    All ``execute`` / ``executemany`` / ``executescript`` calls are
    intercepted; if the SQL statement appears to target a protected table
    or write to a generated column, a ``ProtectedTableError`` is raised.

    Limitations
    ------------
    The guard uses a simple regex on the first SQL token(s) and is therefore
    subject to the usual SQL injection / evasion caveats.  It is a safety
    net, not a full SQL parser.  Complex statements (e.g. nested triggers,
    ATTACH) that indirectly mutate protected tables are **not** blocked.
    Do not rely on this guard for security; treat the source export as
    read-only at all times.

    Usage::

        conn = sqlite3.connect('working_copy.db')
        guard = WriteGuard(conn)
        guard.execute("INSERT INTO weight_record_table ...")   # allowed
        guard.execute("INSERT INTO change_logs_table ...")      # raises ProtectedTableError
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._conn.row_factory = value

    def _check(self, sql: str) -> None:
        """
        Raise ``ProtectedTableError`` if ``sql`` writes to a protected table
        or writes a generated column.
        """
        upper = sql.upper()
        # Check protected tables
        if _PROTECTED_TABLE_PATTERN.search(upper):
            raise ProtectedTableError(
                f"Write to protected table attempted. "
                f"Protected tables: {DO_NOT_TOUCH_TABLES}"
            )
        # Check generated columns — e.g. INSERT INTO ... (local_date_time, ...)
        if _GENERATED_COL_PATTERN.search(upper):
            raise ProtectedTableError(
                f"Write to generated column attempted. "
                f"Generated columns: {sorted(GENERATED_COLUMNS)}"
            )

    def execute(self, sql: str, parameters=()) -> sqlite3.Cursor:
        self._check(sql)
        return self._conn.execute(sql, parameters)

    def executemany(self, sql: str, parameters) -> sqlite3.Cursor:
        self._check(sql)
        return self._conn.executemany(sql, parameters)

    def executescript(self, sql: str) -> sqlite3.Cursor:
        self._check(sql)
        return self._conn.executescript(sql)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def cursor(self) -> sqlite3.Cursor:
        return self._conn.cursor()

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self._conn.__exit__(*args)
