"""
knowledge.py — SINGLE source of truth for Health Connect internals.

Every constant carries an evidence comment (source: PoC logcat crash /
empirical db row / AOSP documentation).  Do not hard-code these values
anywhere else in the package.
"""

import uuid
import struct
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

KNOWN_USER_VERSION = 23
"""
source: empirical — checked against backup-hc/health_connect_export.db
(HC version on the phone at time of PoC import, 2026-08-22).
Warn if user_version != KNOWN_USER_VERSION.
"""

# ---------------------------------------------------------------------------
# Zone offset
# ---------------------------------------------------------------------------

ZONE_OFFSET_MAX_SECONDS = 64800
"""
source: PoC logcat crash (java.time.DateTimeException: Zone offset not in
valid range: -18:00 to +18:00).  Valid range is ±64800 seconds.
Writing milliseconds (e.g. 7200000 instead of 7200) triggers this crash
and rejects the entire import.
"""

# ---------------------------------------------------------------------------
# Units  (source: PoC §4, AOSP Health Connect Data Types docs)
# ---------------------------------------------------------------------------

WEIGHT_GRAMS = "grams"         # weight_record_table.mass / lean_body_mass_record_table.mass
ENERGY_CALORIES_TIMES_1000 = "calories×1000"  # total_calories_burned_record_table.energy
DISTANCE_METERS = "meters"      # distance_record_table.distance
PERCENT = "percent"            # body_fat_record_table.percentage
EPOCH_MS = "epoch_ms"          # all _record_table.time / start_time / end_time columns

# ---------------------------------------------------------------------------
# Recording method  (source: AOSP RecordingMethod enum, empirical verification)
# ---------------------------------------------------------------------------

RECORDING_METHOD: dict[str, int] = {
    "UNKNOWN": 0,                 # RecordingMethod.UNKNOWN
    "ACTIVELY_RECORDED": 1,       # RecordingMethod.ACTIVELY_RECORDED
    "AUTOMATICALLY_RECORDED": 2,  # RecordingMethod.AUTOMATICALLY_RECORDED
    "MANUAL_ENTRY": 3,            # RecordingMethod.MANUAL_ENTRY
}

# ---------------------------------------------------------------------------
# Record type IDs  (source: AOSP RecordTypeIdentifier, empirical against db rows)
# ---------------------------------------------------------------------------

RECORD_TYPE_IDS: dict[str, int] = {
    "steps": 1,
    "heart_rate": 11,
    "body_fat": 17,
    "weight": 26,
    "lean_body_mass": 27,
    "exercise_session": 37,
    "sleep_session": 38,
}

# ---------------------------------------------------------------------------
# Sleep stage IDs  (source: AOSP SleepStage, empirical against db rows)
# ---------------------------------------------------------------------------

SLEEP_STAGE_IDS: dict[str, int] = {
    "AWAKE": 1,
    "LIGHT": 4,
    "DEEP": 5,
    "REM": 6,
}

# ---------------------------------------------------------------------------
# Zepp → HC ExerciseType mapping
# (source: empirical — compared Zepp sport CSV type field against the
# exercise_session rows that imported successfully in the PoC wave 2)
# ---------------------------------------------------------------------------

ZEPP_SPORT_MAP: dict[str, Tuple[int, str]] = {
    "1":  (56, "Outdoor Running"),
    "6":  (74, "Pool Swimming"),
    "13": (56, "Cross Country Running"),
    "16": (0,  "Free Training"),
    "22": (64, "Football"),
    "24": (0,  "Indoor Fitness"),
    "49": (36, "HIIT"),
    "52": (70, "Strength Training"),
    "76": (16, "Dance"),
    "140":(46, "Kayaking"),
}

# ---------------------------------------------------------------------------
# Protected tables  — never write to these
# (source: PoC logcat + empirical; tables confirmed to exist in every export)
# ---------------------------------------------------------------------------

DO_NOT_TOUCH_TABLES: list[str] = [
    "android_metadata",
    "change_logs_table",
    "change_log_request_table",
    "access_logs_table",
    "read_access_logs_table",
    "backup_change_token_table",
    "migration_entity_table",
    "pre_migration_category_priority_table",
]

# ---------------------------------------------------------------------------
# Generated columns  — never write to these (HC recomputes them on import)
# ---------------------------------------------------------------------------

GENERATED_COLUMNS: frozenset[str] = frozenset({"local_date_time"})
"""
local_date_time is a generated column (source: AOSP schema, confirmed by
PoC post-import diff showing HC silently overwrites it for interval records).
"""

# ---------------------------------------------------------------------------
# Proven row shape  (source: empirical — decoded dedupe_hash from a known-good
# Google Fit weight row; used unchanged in all PoC pilot/full inserts)
# ---------------------------------------------------------------------------

CLIENT_RECORD_VERSION = "0"
"""source: empirical — every successfully imported row in PoC has version '0'."""

DEVICE_UNKNOWN_ID = 1
"""source: empirical — row_id=1 in device_info_table is the empty 'unknown device' entry."""

# ---------------------------------------------------------------------------
# TABLES registry
# domain name -> (table_name, time_column, value_column or None, record_type_id, is_interval)
#
# time_column: for instant tables this is 'time'; for interval tables 'start_time'/'end_time'
# value_column: None for session/interval-only tables (sleep, exercise, HR with series)
# is_interval: True for daily/session records; False for instant measurements
# ---------------------------------------------------------------------------

TABLES: dict[str, Tuple[str, str, Optional[str], int, bool]] = {
    "weight":        ("weight_record_table",                "time",           "weight",       26, False),
    "body_fat":      ("body_fat_record_table",              "time",           "percentage",   17, False),
    "lean_mass":     ("lean_body_mass_record_table",        "time",           "mass",         27, False),
    "steps":         ("steps_record_table",                 "start_time",     "count",         1, True),
    "distance":      ("distance_record_table",              "start_time",     "distance",      1, True),
    "calories":      ("total_calories_burned_record_table", "start_time",     "energy",        1, True),
    "sleep":         ("sleep_session_record_table",         "start_time",     None,           38, True),
    "heart_rate":    ("heart_rate_record_table",             "start_time",     None,           11, True),
    "exercise":      ("exercise_session_record_table",      "start_time",     None,           37, True),
}

# ---------------------------------------------------------------------------
# Dedup hash helpers
# ---------------------------------------------------------------------------

def dedupe_hash_instant(app_id: int, device_id: int, time_ms: int) -> bytes:
    """
    Instant-record dedupe_hash = big-endian concat of
    appInfoId(8B) + deviceInfoId(8B) + time(8B)  →  24 bytes.

    source: empirical — decoded a known-good Google Fit weight row
    (app=5, dev=1, time=...) and verified struct.pack('>qqq', app, dev, time)
    matches the stored blob exactly (make_pilot_bisect.py verify_hash_formula).
    """
    return struct.pack('>qqq', app_id, device_id, time_ms)


def dedupe_hash_interval(
    app_id: int, device_id: int, start_ms: int, end_ms: int
) -> bytes:
    """
    Interval-record dedupe_hash = big-endian concat of
    appInfoId(8B) + deviceInfoId(8B) + start_time(8B) + end_time(8B)  →  32 bytes.

    source: empirical — verified against build_wave2.py interval inserts;
    start_ms and end_ms are each 8-byte big-endian signed integers.
    Confirmed with struct.pack('>qqq', ...) pattern for the first 24B,
    then struct.pack('>q', end_ms) for the final 8B.
    """
    return struct.pack('>qqq', app_id, device_id, start_ms) + struct.pack('>q', end_ms)


def local_date_epoch_days(epoch_ms: int, zone_offset_seconds: int) -> int:
    """
    Convert UTC epoch_ms + zone offset (seconds) to local epoch days.

    Formula: (epoch_ms + 1000 * zone_offset_seconds) // 86400000

    source: PoC §4 / AOSP — local_date is stored as integer epoch days
    of the *local* time.  For interval records HC recomputes it from the
    LOCAL START instant on import (benign normalization; we compute it
    start-based ourselves).
    """
    return (epoch_ms + 1000 * zone_offset_seconds) // 86400000


def deterministic_uuid(
    project_key: str,
    domain: str,
    start_ms: int,
    end_ms: Optional[int] = None,
) -> bytes:
    """
    Deterministic UUIDv5 (NAMESPACE_URL) for a record.

    Namespace key: ``{project_key}/{domain}/{start_ms}[-{end_ms}]``

    source: PoC wave-2 builder — ensures pilot ⊆ full set and re-imports
    are idempotent (HC ignores duplicate UUIDs).  UUID is returned as 16
    raw bytes (suitable for BLOB column).
    """
    key = f"{project_key}/{domain}/{start_ms}"
    if end_ms is not None:
        key = f"{key}-{end_ms}"
    return uuid.uuid5(uuid.NAMESPACE_URL, key).bytes


# ---------------------------------------------------------------------------
# HC import semantics documentation
# ---------------------------------------------------------------------------

HC_IMPORT_NOTES: str = """
Health Connect import semantics (all empirically verified):

1. MERGE — HC imports do not overwrite or delete existing on-phone data.
   Newer on-phone records survive; duplicate uuid/dedupe_hash are
   conflict-ignored.  (Marker test: modified timestamp on a pilot row
   survived a second import of the unmodified db.)

2. local_date recomputation — for interval records HC's import pipeline
   recomputes local_date from the LOCAL START instant (not the end or the
   written value).  PoC observed 462 sleep sessions normalized from
   end-based values to start-based values.  This is benign normalization;
   final state is self-consistent.  We always compute local_date
   start-based ourselves.

3. Integrity check — HC only checks user_version (must be ≤ device schema
   version) and absence of parse exceptions.  No checksums.  Any per-row
   parse error rejects the *entire* import.

4. No on-demand export — exports are scheduled (daily/weekly/monthly) or
   taken manually in HC Settings.  There is no way to force an immediate
   export.  Post-import diff must run against the next scheduled export.
"""
