#!/usr/bin/env python3
"""
make_fixture_db.py — build a tiny synthetic Health Connect-schema SQLite db.

Run directly:  python3 tests/fixtures/make_fixture_db.py /tmp/fixture.db
Or import:       from tests.fixtures.make_fixture_db import build; build('/tmp/fixture.db')

The db contains:
  PRAGMA user_version = 23
  All wave-relevant tables (exact DDL from real schema)
  Seeded with deterministic synthetic rows in PROVEN shape:
    - 2 apps: com.example.tracker (id=5), com.example.watch (id=6)
    - 1 device: row_id=1 (unknown device)
    - Known-good rows per domain with correct dedupe_hash and zone offsets
    - Cutoff date: 2026-03-18T00:00:00Z — most rows are before; 1-2 are at/after
  Deterministic: fixed uuids via knowledge.deterministic_uuid('fixture', ...)
"""

import sys
import sqlite3
import datetime
import json

sys.path.insert(0, str(__file__).rsplit("/tests/fixtures", 1)[0] + "/src")
from ghc_db_manager import knowledge as kn


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PROJECT_KEY = "fixture"
TZ_WARSAW = "Europe/Warsaw"

# All seeded interval data ends before this cutoff (steps cutoff from PoC).
# 2026-03-18T00:00:00Z
CUTOFF_MS = 1773792000000

NOW_MS = 1774204800000  # ~2026-03-23T00:00:00Z (fixture "now")


def _dt(year, month, day, hour=0, minute=0, second=0, tz=datetime.timezone.utc):
    return datetime.datetime(year, month, day, hour, minute, second, tzinfo=tz)


def _epoch_ms(dt: datetime.datetime) -> int:
    return int(dt.timestamp() * 1000)


def _zone_offset(dt: datetime.datetime) -> int:
    """Return Warsaw offset in seconds at instant (unused placeholder — _warsaw_offset is used instead)."""
    from ghc_db_manager.timeutil import zone_offset_seconds
    return zone_offset_seconds(dt, TZ_WARSAW)


def _warsaw_offset(dt: datetime.datetime) -> int:
    """Europe/Warsaw offset in seconds at a UTC instant (DST-aware)."""
    # Use zoneinfo via the timeutil approach
    from ghc_db_manager.timeutil import zone_offset_seconds
    return zone_offset_seconds(dt, TZ_WARSAW)


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------

def _uuid(domain: str, start_ms: int, end_ms: int | None = None) -> bytes:
    return kn.deterministic_uuid(PROJECT_KEY, domain, start_ms, end_ms)


def _dedupe_instant(app_id: int, device_id: int, time_ms: int) -> bytes:
    return kn.dedupe_hash_instant(app_id, device_id, time_ms)


def _dedupe_interval(app_id: int, device_id: int, start_ms: int, end_ms: int) -> bytes:
    return kn.dedupe_hash_interval(app_id, device_id, start_ms, end_ms)


# ---------------------------------------------------------------------------
# DDL — exact column lists from real HC schema
# ---------------------------------------------------------------------------

DDL_STATEMENTS = [
    # Metadata
    "CREATE TABLE IF NOT EXISTS android_metadata (locale TEXT)",
    # Note: sqlite_sequence is created automatically by SQLite for AUTOINCREMENT tables

    # Reference tables
    """CREATE TABLE application_info_table (
        row_id INTEGER PRIMARY KEY,
        package_name TEXT NOT NULL UNIQUE,
        app_name TEXT,
        app_icon BLOB,
        record_types_used TEXT
    )""",
    """CREATE TABLE device_info_table (
        row_id INTEGER PRIMARY KEY,
        manufacturer TEXT,
        model TEXT,
        device_type INTEGER
    )""",
    """CREATE TABLE activity_date_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        epoch_days INTEGER NOT NULL,
        record_type_id INTEGER NOT NULL,
        UNIQUE(epoch_days, record_type_id)
    )""",

    # Instant-record tables (time + zone_offset)
    """CREATE TABLE weight_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        time INTEGER,
        zone_offset INTEGER,
        local_date INTEGER,
        weight REAL,
        local_date_time INTEGER AS (time + 1000 * zone_offset)
    )""",
    """CREATE TABLE body_fat_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        time INTEGER,
        zone_offset INTEGER,
        local_date INTEGER,
        percentage REAL,
        local_date_time INTEGER AS (time + 1000 * zone_offset)
    )""",
    """CREATE TABLE lean_body_mass_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        time INTEGER,
        zone_offset INTEGER,
        local_date INTEGER,
        mass REAL,
        local_date_time INTEGER AS (time + 1000 * zone_offset)
    )""",

    # Interval-record tables (start_time/end_time + start_zone_offset/end_zone_offset)
    """CREATE TABLE steps_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        start_time INTEGER,
        start_zone_offset INTEGER,
        end_time INTEGER,
        end_zone_offset INTEGER,
        local_date INTEGER,
        count INTEGER,
        local_date_time_start_time INTEGER AS (start_time + 1000 * start_zone_offset),
        local_date_time_end_time INTEGER AS (end_time + 1000 * end_zone_offset)
    )""",
    """CREATE TABLE distance_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        start_time INTEGER,
        start_zone_offset INTEGER,
        end_time INTEGER,
        end_zone_offset INTEGER,
        local_date INTEGER,
        distance REAL,
        local_date_time_start_time INTEGER AS (start_time + 1000 * start_zone_offset),
        local_date_time_end_time INTEGER AS (end_time + 1000 * end_zone_offset)
    )""",
    """CREATE TABLE total_calories_burned_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        start_time INTEGER,
        start_zone_offset INTEGER,
        end_time INTEGER,
        end_zone_offset INTEGER,
        local_date INTEGER,
        energy REAL,
        local_date_time_start_time INTEGER AS (start_time + 1000 * start_zone_offset),
        local_date_time_end_time INTEGER AS (end_time + 1000 * end_zone_offset)
    )""",
    """CREATE TABLE sleep_session_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        start_time INTEGER,
        start_zone_offset INTEGER,
        end_time INTEGER,
        end_zone_offset INTEGER,
        local_date INTEGER,
        notes TEXT,
        title TEXT,
        local_date_time_start_time INTEGER AS (start_time + 1000 * start_zone_offset),
        local_date_time_end_time INTEGER AS (end_time + 1000 * end_zone_offset)
    )""",
    """CREATE TABLE heart_rate_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        start_time INTEGER,
        start_zone_offset INTEGER,
        end_time INTEGER,
        end_zone_offset INTEGER,
        local_date INTEGER,
        local_date_time_start_time INTEGER AS (start_time + 1000 * start_zone_offset),
        local_date_time_end_time INTEGER AS (end_time + 1000 * end_zone_offset)
    )""",
    """CREATE TABLE exercise_session_record_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid BLOB NOT NULL UNIQUE,
        last_modified_time INTEGER,
        client_record_id TEXT,
        client_record_version TEXT,
        device_info_id INTEGER,
        app_info_id INTEGER,
        recording_method INTEGER,
        dedupe_hash BLOB UNIQUE,
        start_time INTEGER,
        start_zone_offset INTEGER,
        end_time INTEGER,
        end_zone_offset INTEGER,
        local_date INTEGER,
        notes TEXT,
        exercise_type INTEGER,
        title TEXT,
        has_route INTEGER,
        local_date_time_start_time INTEGER AS (start_time + 1000 * start_zone_offset),
        local_date_time_end_time INTEGER AS (end_time + 1000 * end_zone_offset)
    )""",

    # Series / child tables
    """CREATE TABLE heart_rate_record_series_table (
        parent_key INTEGER,
        beats_per_minute INTEGER,
        epoch_millis INTEGER
    )""",
    """CREATE TABLE sleep_stages_table (
        parent_key INTEGER NOT NULL,
        stage_start_time INTEGER NOT NULL,
        stage_end_time INTEGER NOT NULL,
        stage_type INTEGER NOT NULL
    )""",

    # Protected / do-not-touch tables
    """CREATE TABLE change_logs_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        record_type INTEGER,
        app_id INTEGER,
        uuids BLOB NOT NULL,
        operation_type INTEGER,
        time INTEGER,
        medical_resource_type INTEGER,
        medical_data_source_id INTEGER
    )""",
    """CREATE TABLE change_log_request_table (
        row_id INTEGER PRIMARY KEY,
        packages_to_filter TEXT NOT NULL,
        package_name TEXT NOT NULL,
        record_types TEXT,
        row_id_change_logs_table INTEGER,
        time INTEGER,
        medical_resource_types TEXT
    )""",
    """CREATE TABLE access_logs_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        app_id INTEGER NOT NULL,
        record_type TEXT NOT NULL,
        access_time INTEGER NOT NULL,
        operation_type INTEGER NOT NULL,
        medical_resource_type TEXT,
        medical_data_source_accessed INTEGER
    )""",
    """CREATE TABLE read_access_logs_table (
        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reader_app_id INTEGER NOT NULL,
        writer_app_id INTEGER NOT NULL,
        record_type TEXT NOT NULL,
        read_time INTEGER NOT NULL,
        write_time INTEGER NOT NULL
    )""",
    """CREATE TABLE backup_change_token_table (
        row_id INTEGER PRIMARY KEY,
        record_type INTEGER NOT NULL,
        data_table_page_token INTEGER,
        change_logs_request_token TEXT
    )""",
    """CREATE TABLE migration_entity_table (
        row_id INTEGER PRIMARY KEY,
        entity_id TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE pre_migration_category_priority_table (
        category INTEGER UNIQUE,
        priority_order TEXT NOT NULL
    )""",

    # Indexes (needed for FK relationships)
    "CREATE INDEX idx_weight_record_table_0 ON weight_record_table(device_info_id)",
    "CREATE INDEX idx_weight_record_table_1 ON weight_record_table(app_info_id)",
    "CREATE INDEX idx_body_fat_record_table_0 ON body_fat_record_table(device_info_id)",
    "CREATE INDEX idx_body_fat_record_table_1 ON body_fat_record_table(app_info_id)",
    "CREATE INDEX idx_lean_body_mass_record_table_0 ON lean_body_mass_record_table(device_info_id)",
    "CREATE INDEX idx_lean_body_mass_record_table_1 ON lean_body_mass_record_table(app_info_id)",
    "CREATE INDEX idx_steps_record_table_0 ON steps_record_table(device_info_id)",
    "CREATE INDEX idx_steps_record_table_1 ON steps_record_table(app_info_id)",
    "CREATE INDEX idx_distance_record_table_0 ON distance_record_table(device_info_id)",
    "CREATE INDEX idx_distance_record_table_1 ON distance_record_table(app_info_id)",
    "CREATE INDEX idx_total_calories_burned_record_table_0 ON total_calories_burned_record_table(device_info_id)",
    "CREATE INDEX idx_total_calories_burned_record_table_1 ON total_calories_burned_record_table(app_info_id)",
    "CREATE INDEX idx_sleep_session_record_table_0 ON sleep_session_record_table(device_info_id)",
    "CREATE INDEX idx_sleep_session_record_table_1 ON sleep_session_record_table(app_info_id)",
    "CREATE INDEX idx_heart_rate_record_table_0 ON heart_rate_record_table(device_info_id)",
    "CREATE INDEX idx_heart_rate_record_table_1 ON heart_rate_record_table(app_info_id)",
    "CREATE INDEX idx_exercise_session_record_table_0 ON exercise_session_record_table(device_info_id)",
    "CREATE INDEX idx_exercise_session_record_table_1 ON exercise_session_record_table(app_info_id)",
    "CREATE INDEX idx_heart_rate_record_series_table_0 ON heart_rate_record_series_table(parent_key)",
    "CREATE INDEX idx_sleep_stages_table_0 ON sleep_stages_table(parent_key)",
]


# ---------------------------------------------------------------------------
# Seed data helpers
# ---------------------------------------------------------------------------

APP_TRACKER = 5   # com.example.tracker
APP_WATCH = 6    # com.example.watch
DEVICE_UNKNOWN = 1


def _insert_weight(conn, app_id, device_id, dt_utc, weight_kg):
    """Insert a weight row. dt_utc is a UTC datetime."""
    t = _epoch_ms(dt_utc)
    off = _warsaw_offset(dt_utc)
    local_date = kn.local_date_epoch_days(t, off)
    dh = _dedupe_instant(app_id, device_id, t)
    uuid = _uuid("weight", t)
    conn.execute(
        """INSERT INTO weight_record_table
           (uuid, last_modified_time, client_record_id, client_record_version,
            device_info_id, app_info_id, recording_method, dedupe_hash,
            time, zone_offset, local_date, weight)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid, NOW_MS, None, kn.CLIENT_RECORD_VERSION,
         device_id, app_id, kn.RECORDING_METHOD["MANUAL_ENTRY"],
         dh, t, off, local_date, weight_kg)
    )


def _insert_body_fat(conn, app_id, device_id, dt_utc, percentage):
    t = _epoch_ms(dt_utc)
    off = _warsaw_offset(dt_utc)
    local_date = kn.local_date_epoch_days(t, off)
    dh = _dedupe_instant(app_id, device_id, t)
    uuid = _uuid("body_fat", t)
    conn.execute(
        """INSERT INTO body_fat_record_table
           (uuid, last_modified_time, client_record_id, client_record_version,
            device_info_id, app_info_id, recording_method, dedupe_hash,
            time, zone_offset, local_date, percentage)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid, NOW_MS, None, kn.CLIENT_RECORD_VERSION,
         device_id, app_id, kn.RECORDING_METHOD["MANUAL_ENTRY"],
         dh, t, off, local_date, percentage)
    )


def _insert_lean_mass(conn, app_id, device_id, dt_utc, mass_kg):
    t = _epoch_ms(dt_utc)
    off = _warsaw_offset(dt_utc)
    local_date = kn.local_date_epoch_days(t, off)
    dh = _dedupe_instant(app_id, device_id, t)
    uuid = _uuid("lean_mass", t)
    conn.execute(
        """INSERT INTO lean_body_mass_record_table
           (uuid, last_modified_time, client_record_id, client_record_version,
            device_info_id, app_info_id, recording_method, dedupe_hash,
            time, zone_offset, local_date, mass)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid, NOW_MS, None, kn.CLIENT_RECORD_VERSION,
         device_id, app_id, kn.RECORDING_METHOD["MANUAL_ENTRY"],
         dh, t, off, local_date, mass_kg)
    )


def _insert_interval(conn, table_name, domain, app_id, device_id,
                     start_dt_utc, end_dt_utc, value_col, value):
    """Generic interval-record insert for steps/distance/calories."""
    s_ms = _epoch_ms(start_dt_utc)
    e_ms = _epoch_ms(end_dt_utc)
    s_off = _warsaw_offset(start_dt_utc)
    e_off = _warsaw_offset(end_dt_utc)
    local_date = kn.local_date_epoch_days(s_ms, s_off)
    dh = _dedupe_interval(app_id, device_id, s_ms, e_ms)
    uuid = _uuid(domain, s_ms, e_ms)
    conn.execute(
        f"""INSERT INTO {table_name}
           (uuid, last_modified_time, client_record_id, client_record_version,
            device_info_id, app_info_id, recording_method, dedupe_hash,
            start_time, start_zone_offset, end_time, end_zone_offset,
            local_date, {value_col})
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid, NOW_MS, None, kn.CLIENT_RECORD_VERSION,
         device_id, app_id, kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"],
         dh, s_ms, s_off, e_ms, e_off, local_date, value)
    )


def _insert_sleep(conn, app_id, device_id, start_dt_utc, end_dt_utc, stages=None):
    """Insert a sleep session (optionally with stages)."""
    s_ms = _epoch_ms(start_dt_utc)
    e_ms = _epoch_ms(end_dt_utc)
    s_off = _warsaw_offset(start_dt_utc)
    e_off = _warsaw_offset(end_dt_utc)
    # HC uses end-based local_date for sleep sessions
    local_date = kn.local_date_epoch_days(e_ms, e_off)
    dh = _dedupe_interval(app_id, device_id, s_ms, e_ms)
    uuid = _uuid("sleep", s_ms, e_ms)
    cur = conn.execute(
        """INSERT INTO sleep_session_record_table
           (uuid, last_modified_time, client_record_id, client_record_version,
            device_info_id, app_info_id, recording_method, dedupe_hash,
            start_time, start_zone_offset, end_time, end_zone_offset,
            local_date, notes, title)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid, NOW_MS, None, kn.CLIENT_RECORD_VERSION,
         device_id, app_id, kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"],
         dh, s_ms, s_off, e_ms, e_off, local_date,
         "Synthetic fixture sleep", "Nightly Sleep")
    )
    if stages:
        parent_key = cur.lastrowid
        for st_start_ms, st_end_ms, stage_type in stages:
            conn.execute(
                """INSERT INTO sleep_stages_table
                   (parent_key, stage_start_time, stage_end_time, stage_type)
                   VALUES (?,?,?,?)""",
                (parent_key, st_start_ms, st_end_ms, stage_type)
            )


def _insert_heart_rate_session(conn, app_id, device_id, start_dt_utc, end_dt_utc, samples=None):
    """Insert a heart_rate session (optionally with series samples)."""
    s_ms = _epoch_ms(start_dt_utc)
    e_ms = _epoch_ms(end_dt_utc)
    s_off = _warsaw_offset(start_dt_utc)
    e_off = _warsaw_offset(end_dt_utc)
    local_date = kn.local_date_epoch_days(s_ms, s_off)
    dh = _dedupe_interval(app_id, device_id, s_ms, e_ms)
    uuid = _uuid("heart_rate", s_ms, e_ms)
    cur = conn.execute(
        """INSERT INTO heart_rate_record_table
           (uuid, last_modified_time, client_record_id, client_record_version,
            device_info_id, app_info_id, recording_method, dedupe_hash,
            start_time, start_zone_offset, end_time, end_zone_offset, local_date)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid, NOW_MS, None, kn.CLIENT_RECORD_VERSION,
         device_id, app_id, kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"],
         dh, s_ms, s_off, e_ms, e_off, local_date)
    )
    if samples:
        parent_key = cur.lastrowid
        for epoch_ms, bpm in samples:
            conn.execute(
                """INSERT INTO heart_rate_record_series_table
                   (parent_key, beats_per_minute, epoch_millis)
                   VALUES (?,?,?)""",
                (parent_key, bpm, epoch_ms)
            )


def _insert_exercise(conn, app_id, device_id, start_dt_utc, end_dt_utc,
                     exercise_type, title):
    s_ms = _epoch_ms(start_dt_utc)
    e_ms = _epoch_ms(end_dt_utc)
    s_off = _warsaw_offset(start_dt_utc)
    e_off = _warsaw_offset(end_dt_utc)
    local_date = kn.local_date_epoch_days(s_ms, s_off)
    dh = _dedupe_interval(app_id, device_id, s_ms, e_ms)
    uuid = _uuid("exercise", s_ms, e_ms)
    conn.execute(
        """INSERT INTO exercise_session_record_table
           (uuid, last_modified_time, client_record_id, client_record_version,
            device_info_id, app_info_id, recording_method, dedupe_hash,
            start_time, start_zone_offset, end_time, end_zone_offset,
            local_date, notes, exercise_type, title, has_route)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uuid, NOW_MS, None, kn.CLIENT_RECORD_VERSION,
         device_id, app_id, kn.RECORDING_METHOD["ACTIVELY_RECORDED"],
         dh, s_ms, s_off, e_ms, e_off, local_date,
         "Synthetic fixture exercise", exercise_type, title, 0)
    )


# ---------------------------------------------------------------------------
# Main build function
# ---------------------------------------------------------------------------

def build(path: str) -> None:
    """
    Create and populate the synthetic HC-schema fixture database.

    Parameters
    ----------
    path : str
        Path for the output SQLite file.
    """
    conn = sqlite3.connect(path)

    # Set user_version first
    conn.execute("PRAGMA user_version = 23")

    # Create all tables and indexes
    for ddl in DDL_STATEMENTS:
        conn.execute(ddl)

    # Seed reference rows
    conn.execute(
        "INSERT INTO application_info_table (row_id, package_name, app_name, record_types_used) "
        "VALUES (5, 'com.example.tracker', 'Example Tracker', '1,11,17,26,27,37,38')"
    )
    conn.execute(
        "INSERT INTO application_info_table (row_id, package_name, app_name, record_types_used) "
        "VALUES (6, 'com.example.watch', 'Example Watch', '1,11,37,38')"
    )
    conn.execute(
        "INSERT INTO device_info_table (row_id, manufacturer, model, device_type) "
        "VALUES (1, NULL, NULL, NULL)"  # unknown device
    )
    conn.execute(
        "INSERT INTO device_info_table (row_id, manufacturer, model, device_type) "
        "VALUES (2, 'Example Corp', 'FitWatch Pro', 2)"
    )

    # -------------------------------------------------------------------------
    # WEIGHT — 5 rows, all before cutoff
    # -------------------------------------------------------------------------
    # Person: fictional Anna Nowak, 2024–2025 weight entries
    _insert_weight(conn, APP_TRACKER, DEVICE_UNKNOWN,
                   _dt(2024, 1, 15, 7, 30), 68.4)
    _insert_weight(conn, APP_TRACKER, DEVICE_UNKNOWN,
                   _dt(2024, 6, 10, 6, 45), 67.2)   # summer (DST)
    _insert_weight(conn, APP_TRACKER, DEVICE_UNKNOWN,
                   _dt(2025, 2, 20, 7, 0), 69.1)
    _insert_weight(conn, APP_WATCH, DEVICE_UNKNOWN,
                   _dt(2025, 9, 5, 6, 15), 68.8)
    _insert_weight(conn, APP_TRACKER, 2,  # different device
                   _dt(2026, 1, 10, 7, 30), 70.3)

    # -------------------------------------------------------------------------
    # BODY FAT — 3 rows before cutoff
    # -------------------------------------------------------------------------
    _insert_body_fat(conn, APP_TRACKER, DEVICE_UNKNOWN,
                     _dt(2024, 1, 15, 7, 30), 22.5)
    _insert_body_fat(conn, APP_TRACKER, DEVICE_UNKNOWN,
                     _dt(2025, 2, 20, 7, 0), 23.1)
    _insert_body_fat(conn, APP_WATCH, DEVICE_UNKNOWN,
                     _dt(2025, 9, 5, 6, 15), 22.8)

    # -------------------------------------------------------------------------
    # LEAN BODY MASS — 2 rows before cutoff
    # -------------------------------------------------------------------------
    _insert_lean_mass(conn, APP_TRACKER, DEVICE_UNKNOWN,
                      _dt(2024, 1, 15, 7, 30), 53.0)
    _insert_lean_mass(conn, APP_TRACKER, DEVICE_UNKNOWN,
                      _dt(2025, 2, 20, 7, 0), 53.1)

    # -------------------------------------------------------------------------
    # STEPS — 5 rows; row 4 is at cutoff, row 5 is after
    # Days: 2026-03-13 through 2026-03-18 (local midnight → UTC)
    # -------------------------------------------------------------------------
    def day_start_utc(year, month, day):
        """Local midnight in Warsaw converted to UTC."""
        import datetime as dt_module
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(TZ_WARSAW)
        local_midnight = dt_module.datetime(year, month, day, 0, 0, 0, tzinfo=tz)
        utc_offset = local_midnight.utcoffset() or datetime.timedelta(0)
        return (local_midnight - utc_offset).replace(tzinfo=dt_module.timezone.utc)

    ONE_DAY = datetime.timedelta(days=1)

    # Day 1: 2026-03-13 (Fri) — UTC 2026-03-12 23:00 (winter, CET +1)
    d1_start = day_start_utc(2026, 3, 13)
    _insert_interval(conn, "steps_record_table", "steps",
                     APP_TRACKER, DEVICE_UNKNOWN,
                     d1_start, d1_start + ONE_DAY,
                     "count", 8432)

    # Day 2: 2026-03-14 — UTC 2026-03-13 23:00 (still winter)
    d2_start = day_start_utc(2026, 3, 14)
    _insert_interval(conn, "steps_record_table", "steps",
                     APP_WATCH, DEVICE_UNKNOWN,
                     d2_start, d2_start + ONE_DAY,
                     "count", 10234)

    # Day 3: 2026-03-15 — UTC 2026-03-14 23:00 (still winter)
    d3_start = day_start_utc(2026, 3, 15)
    _insert_interval(conn, "steps_record_table", "steps",
                     APP_TRACKER, 2,
                     d3_start, d3_start + ONE_DAY,
                     "count", 6201)

    # Day 4: 2026-03-17 — UTC 2026-03-16 23:00 (still winter; 3 days before cutoff)
    d4_start = day_start_utc(2026, 3, 17)
    _insert_interval(conn, "steps_record_table", "steps",
                     APP_TRACKER, DEVICE_UNKNOWN,
                     d4_start, d4_start + ONE_DAY,
                     "count", 7100)

    # Day 5: 2026-03-18 (cutoff day) — UTC 2026-03-17 23:00
    # This row starts exactly at the cutoff; deliberately kept for invariant test
    d5_start = day_start_utc(2026, 3, 18)
    _insert_interval(conn, "steps_record_table", "steps",
                     APP_TRACKER, DEVICE_UNKNOWN,
                     d5_start, d5_start + ONE_DAY,
                     "count", 5000)

    # Day 6: 2026-03-19 — UTC 2026-03-18 23:00 — AFTER cutoff
    d6_start = day_start_utc(2026, 3, 19)
    _insert_interval(conn, "steps_record_table", "steps",
                     APP_WATCH, DEVICE_UNKNOWN,
                     d6_start, d6_start + ONE_DAY,
                     "count", 9876)

    # -------------------------------------------------------------------------
    # DISTANCE — 2 rows before cutoff
    # -------------------------------------------------------------------------
    _insert_interval(conn, "distance_record_table", "distance",
                     APP_TRACKER, DEVICE_UNKNOWN,
                     d1_start, d1_start + ONE_DAY,
                     "distance", 6430.0)
    _insert_interval(conn, "distance_record_table", "distance",
                     APP_WATCH, DEVICE_UNKNOWN,
                     d2_start, d2_start + ONE_DAY,
                     "distance", 8120.0)

    # -------------------------------------------------------------------------
    # CALORIES — 2 rows before cutoff
    # -------------------------------------------------------------------------
    _insert_interval(conn, "total_calories_burned_record_table", "calories",
                     APP_TRACKER, DEVICE_UNKNOWN,
                     d1_start, d1_start + ONE_DAY,
                     "energy", 492000.0)
    _insert_interval(conn, "total_calories_burned_record_table", "calories",
                     APP_WATCH, DEVICE_UNKNOWN,
                     d2_start, d2_start + ONE_DAY,
                     "energy", 615000.0)

    # -------------------------------------------------------------------------
    # SLEEP — 2 sessions; session 2 has 2 stages
    # -------------------------------------------------------------------------
    # Session 1: 2026-02-15 night — before cutoff
    s1_start = _dt(2026, 2, 15, 23, 30)  # local 23:30 = UTC 22:30 (winter)
    s1_end = _dt(2026, 2, 16, 6, 45)
    _insert_sleep(conn, APP_TRACKER, DEVICE_UNKNOWN, s1_start, s1_end)

    # Session 2: 2026-03-14 night — before cutoff, with 2 stages
    # Session: 23:00 UTC → 06:30 UTC (7.5 h)
    # LIGHT: 23:30 → 01:00 UTC (90 min)
    # DEEP:  01:00 → 05:30 UTC (270 min)
    # Wake:  05:30 → 06:30 UTC (60 min)
    s2_start = _dt(2026, 3, 14, 23, 0)   # UTC 22:00 (winter, CET)
    s2_end   = _dt(2026, 3, 15, 6, 30)
    s2_light_start = _epoch_ms(s2_start) + 30 * 60 * 1000
    s2_light_end   = s2_light_start + 90 * 60 * 1000
    s2_deep_start  = s2_light_end
    s2_deep_end    = _epoch_ms(s2_end) - 60 * 60 * 1000
    _insert_sleep(conn, APP_WATCH, DEVICE_UNKNOWN, s2_start, s2_end, [
        (s2_light_start, s2_light_end, kn.SLEEP_STAGE_IDS["LIGHT"]),
        (s2_deep_start, s2_deep_end, kn.SLEEP_STAGE_IDS["DEEP"]),
    ])

    # -------------------------------------------------------------------------
    # HEART RATE — 1 session with 3 series samples + 1 manual (no samples)
    # -------------------------------------------------------------------------
    # Auto session: 2026-01-20 afternoon
    hr_start = _dt(2026, 1, 20, 15, 0)
    hr_end = _dt(2026, 1, 20, 15, 45)
    samples = [
        (_epoch_ms(_dt(2026, 1, 20, 15, 0)), 68),
        (_epoch_ms(_dt(2026, 1, 20, 15, 15)), 72),
        (_epoch_ms(_dt(2026, 1, 20, 15, 30)), 71),
    ]
    _insert_heart_rate_session(conn, APP_WATCH, DEVICE_UNKNOWN, hr_start, hr_end, samples)

    # Manual entry: 2026-02-05 morning, single measurement
    _insert_heart_rate_session(conn, APP_TRACKER, DEVICE_UNKNOWN,
                               _dt(2026, 2, 5, 8, 10),
                               _dt(2026, 2, 5, 8, 11))

    # -------------------------------------------------------------------------
    # EXERCISE — 4 sessions (types 1, 6, 52, 76), all before cutoff
    # -------------------------------------------------------------------------
    # Outdoor Running (type 1 → HC 56) on 2026-03-01 morning
    ex1_start = _dt(2026, 3, 1, 7, 30)
    ex1_end = _dt(2026, 3, 1, 8, 25)
    _insert_exercise(conn, APP_TRACKER, DEVICE_UNKNOWN, ex1_start, ex1_end, 56, "Outdoor Running")

    # Pool Swimming (type 6 → HC 74) on 2026-03-03 evening
    ex2_start = _dt(2026, 3, 3, 19, 0)
    ex2_end = _dt(2026, 3, 3, 19, 55)
    _insert_exercise(conn, APP_TRACKER, 2, ex2_start, ex2_end, 74, "Pool Swimming")

    # Strength Training (type 52 → HC 70) on 2026-03-07 afternoon
    ex3_start = _dt(2026, 3, 7, 17, 30)
    ex3_end = _dt(2026, 3, 7, 18, 15)
    _insert_exercise(conn, APP_WATCH, DEVICE_UNKNOWN, ex3_start, ex3_end, 70, "Strength Training")

    # Dance (type 76 → HC 16) on 2026-03-10 evening
    ex4_start = _dt(2026, 3, 10, 20, 0)
    ex4_end = _dt(2026, 3, 10, 21, 0)
    _insert_exercise(conn, APP_TRACKER, DEVICE_UNKNOWN, ex4_start, ex4_end, 16, "Dance")

    # -------------------------------------------------------------------------
    # activity_date_table — all dates that appear in seeded records
    # -------------------------------------------------------------------------
    # Collect all (epoch_days, record_type_id) pairs from the seeded data
    all_activity_dates: set[tuple[int, int]] = set()

    def _add_activity_date(record_type_id: int, dt_utc: datetime.datetime):
        t = _epoch_ms(dt_utc)
        off = _warsaw_offset(dt_utc)
        ld = kn.local_date_epoch_days(t, off)
        all_activity_dates.add((ld, record_type_id))

    # Weight dates
    for dt in [_dt(2024, 1, 15, 7, 30), _dt(2024, 6, 10, 6, 45),
               _dt(2025, 2, 20, 7, 0), _dt(2025, 9, 5, 6, 15),
               _dt(2026, 1, 10, 7, 30)]:
        _add_activity_date(26, dt)  # weight

    # Body fat dates
    for dt in [_dt(2024, 1, 15, 7, 30), _dt(2025, 2, 20, 7, 0), _dt(2025, 9, 5, 6, 15)]:
        _add_activity_date(17, dt)  # body_fat

    # Lean mass dates
    for dt in [_dt(2024, 1, 15, 7, 30), _dt(2025, 2, 20, 7, 0)]:
        _add_activity_date(27, dt)  # lean_mass

    # Steps dates (d1_start through d6_start)
    for d_start in [d1_start, d2_start, d3_start, d4_start, d5_start, d6_start]:
        _add_activity_date(1, d_start)  # steps

    # Sleep dates
    _add_activity_date(38, s1_end)
    _add_activity_date(38, s2_end)

    # Heart rate dates
    _add_activity_date(11, hr_start)
    _add_activity_date(11, _dt(2026, 2, 5, 8, 10))  # manual entry

    # Exercise dates
    for ex_start in [ex1_start, ex2_start, ex3_start, ex4_start]:
        _add_activity_date(37, ex_start)

    for epoch_days, record_type_id in all_activity_dates:
        conn.execute(
            "INSERT OR IGNORE INTO activity_date_table (epoch_days, record_type_id) VALUES (?, ?)",
            (epoch_days, record_type_id)
        )

    conn.commit()
    conn.close()
    print(f"Fixture DB built: {path}")
    _report_counts(path)


def _report_counts(path: str) -> None:
    """Print row counts for each seeded table."""
    conn = sqlite3.connect(path)
    tables = [
        "weight_record_table", "body_fat_record_table", "lean_body_mass_record_table",
        "steps_record_table", "distance_record_table", "total_calories_burned_record_table",
        "sleep_session_record_table", "heart_rate_record_table", "exercise_session_record_table",
        "heart_rate_record_series_table", "sleep_stages_table",
        "application_info_table", "device_info_table", "activity_date_table",
    ]
    print("Row counts:")
    for t in tables:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {n}")
        except Exception:
            print(f"  {t}: (not found)")
    conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <output_path.db>")
        sys.exit(1)
    build(sys.argv[1])
