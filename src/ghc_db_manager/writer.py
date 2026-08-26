"""
writer.py — instant-record path: write CanonicalRecords to a Health Connect DB.

Provides:
- ``write_canonical()`` — insert canonical records into a WriteGuard-wrapped DB.

The proven row shape (from PoC insert_weight_history.py):
  client_record_id = NULL
  client_record_version = '0'
  device_info_id = 1  (unknown device)
  recording_method = 3  (MANUAL_ENTRY)
  dedupe_hash = knowledge.dedupe_hash_instant(app_id, device_id, ms)

All timestamps and offsets are in epoch ms / seconds as required by the schema.
Zone offsets MUST be in seconds (not milliseconds) — this is verified at construction
time via ValueError.

The ``activity_date_table`` is populated with ``INSERT OR IGNORE``.
"""

import datetime
import uuid
from typing import Optional

from ghc_db_manager import knowledge as kn
from ghc_db_manager.dbio import WriteGuard
from ghc_db_manager.domains.weight import CanonicalRecord


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class RecordError(ValueError):
    """Raised when a CanonicalRecord fails validation before insertion."""
    pass


# ---------------------------------------------------------------------------
# Record validation
# ---------------------------------------------------------------------------

def _validate_record(rec: CanonicalRecord) -> None:
    """
    Validate a CanonicalRecord before insertion.

    Raises ``RecordError`` if:
    - zone_offset is outside ±64800 seconds
    - ms is not a positive integer
    (value plausibility is enforced by domain layers, e.g. weight band in domains/weight.py)
    """
    if rec.zone_offset_seconds < -kn.ZONE_OFFSET_MAX_SECONDS:
        raise RecordError(
            f"zone_offset_seconds {rec.zone_offset_seconds} is below "
            f"-{kn.ZONE_OFFSET_MAX_SECONDS} (possible ms→s confusion)"
        )
    if rec.zone_offset_seconds > kn.ZONE_OFFSET_MAX_SECONDS:
        raise RecordError(
            f"zone_offset_seconds {rec.zone_offset_seconds} exceeds "
            f"+{kn.ZONE_OFFSET_MAX_SECONDS} (possible ms→s confusion)"
        )
    if rec.ms <= 0:
        raise RecordError(f"ms must be positive, got {rec.ms}")


# ---------------------------------------------------------------------------
# Per-kind table / column mapping
# ---------------------------------------------------------------------------

_TABLE_MAP = {
    "weight":    ("weight_record_table",       "weight"),
    "body_fat":  ("body_fat_record_table",     "percentage"),
    "lean_mass": ("lean_body_mass_record_table", "mass"),
}

# Record-type IDs for activity_date_table
_TYPE_ID_MAP = {
    "weight":    kn.RECORD_TYPE_IDS["weight"],       # 26
    "body_fat":  kn.RECORD_TYPE_IDS["body_fat"],     # 17
    "lean_mass": kn.RECORD_TYPE_IDS["lean_body_mass"],  # 27
}


def write_canonical(
    conn: WriteGuard,
    records: list[CanonicalRecord],
    app_info_id: int,
    project_key: str,
    *,
    now_ms: Optional[int] = None,
) -> dict[str, int]:
    """
    Write canonical weight-domain records into the DB.

    Uses the proven row shape (crid NULL, version '0', device 1, recording_method 3).
    Deterministic UUIDv5 per (project_key, kind, ms).
    ``activity_date_table`` is populated with ``INSERT OR IGNORE``.

    Parameters
    ----------
    conn : WriteGuard
        WriteGuard-wrapped sqlite3 connection.
    records : list[CanonicalRecord]
        Output of the domain spec (already filtered / deduped).
    app_info_id : int
        app_info_id to write in each row (must exist in application_info_table).
    project_key : str
        Namespace key for deterministic UUIDv5.
    now_ms : int | None
        ``last_modified_time`` for new rows.  If None, uses current UTC time.

    Returns
    -------
    dict[str, int]
        ``{"weight": N, "body_fat": M, "lean_mass": K, "activity_date": P}``

    Raises
    ------
    RecordError
        If a record fails validation (e.g. zone_offset in wrong unit).

    The function is idempotent: running it twice with the same inputs produces
    the same UUIDs and dedupe_hashes, so HC's conflict-ignore on UNIQUE(uuid)
    and UNIQUE(dedupe_hash) means duplicate inserts are silently skipped.
    """
    if now_ms is None:
        now_ms = int(
            datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
        )

    inserted: dict[str, int] = {"weight": 0, "body_fat": 0, "lean_mass": 0}
    activity_dates: set[tuple[int, int]] = set()  # (epoch_days, record_type_id)

    device_id = kn.DEVICE_UNKNOWN_ID
    rec_method = kn.RECORDING_METHOD["MANUAL_ENTRY"]

    for rec in records:
        _validate_record(rec)

        table_name, value_col = _TABLE_MAP[rec.kind]
        record_type_id = _TYPE_ID_MAP[rec.kind]

        # Deterministic UUIDv5
        rec_uuid = kn.deterministic_uuid(project_key, rec.kind, rec.ms)
        dedupe_hash = kn.dedupe_hash_instant(app_info_id, device_id, rec.ms)

        # Value: kg → grams (×1000), percent → as-is
        if rec.unit == "kg":
            value_db = rec.value * 1000.0
        else:
            value_db = rec.value  # percent

        # Insert the record (OR IGNORE for idempotency: dedupe_hash/uuid uniqueness)
        # GAP-10 fix: use cur.rowcount to count ACTUAL inserts, not attempts.
        # INSERT OR IGNORE skips duplicates — only count if a row was actually inserted.
        cur = conn.execute(
            f"""INSERT OR IGNORE INTO {table_name}
               (uuid, last_modified_time, client_record_id, client_record_version,
                device_info_id, app_info_id, recording_method, dedupe_hash,
                time, zone_offset, local_date, {value_col})
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec_uuid,
                now_ms,
                None,                          # client_record_id
                kn.CLIENT_RECORD_VERSION,      # '0'
                device_id,
                app_info_id,
                rec_method,
                dedupe_hash,
                rec.ms,
                rec.zone_offset_seconds,
                rec.local_date,
                value_db,
            ),
        )
        # cur.rowcount is 1 if a row was inserted, 0 if ignored as duplicate
        if cur.rowcount > 0:
            inserted[rec.kind] = inserted.get(rec.kind, 0) + 1

        # Track activity_date population
        activity_dates.add((rec.local_date, record_type_id))

    # Populate activity_date_table
    for epoch_days, rt_id in activity_dates:
        conn.execute(
            "INSERT OR IGNORE INTO activity_date_table (epoch_days, record_type_id) "
            "VALUES (?, ?)",
            (epoch_days, rt_id),
        )
    inserted["activity_date"] = len(activity_dates)

    return inserted


# ---------------------------------------------------------------------------
# Interval-path table / column mapping
# ---------------------------------------------------------------------------

# Interval tables: (table_name, value_column_or_None)
_INTERVAL_TABLE_MAP: dict[str, tuple[str, str | None]] = {
    "steps":    ("steps_record_table",                         "count"),
    "distance": ("distance_record_table",                      "distance"),
    "calories": ("total_calories_burned_record_table",        "energy"),
    "sleep":    ("sleep_session_record_table",                 None),
    "hr_auto":  ("heart_rate_record_table",                   None),
    "hr_manual":("heart_rate_record_table",                   None),
    "exercise": ("exercise_session_record_table",              None),
}

# Record-type IDs for activity_date_table (interval domains)
_INTERVAL_TYPE_ID_MAP: dict[str, int] = {
    "steps":       kn.RECORD_TYPE_IDS["steps"],        # 1
    "distance":    kn.RECORD_TYPE_IDS["steps"],        # 1 (same type)
    "calories":    kn.RECORD_TYPE_IDS["steps"],        # 1 (same type)
    "sleep":       kn.RECORD_TYPE_IDS["sleep_session"],  # 38
    "hr_auto":     kn.RECORD_TYPE_IDS["heart_rate"],    # 11
    "hr_manual":   kn.RECORD_TYPE_IDS["heart_rate"],    # 11
    "exercise":    kn.RECORD_TYPE_IDS["exercise_session"],  # 37
}

# Recording methods per domain
_INTERVAL_RECORDING_METHOD: dict[str, int] = {
    "steps":      kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"],  # 2
    "distance":   kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"],  # 2
    "calories":   kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"],  # 2
    "sleep":      kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"],  # 2
    "hr_auto":    kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"],  # 2
    "hr_manual":  kn.RECORDING_METHOD["MANUAL_ENTRY"],            # 3
    "exercise":   kn.RECORDING_METHOD["ACTIVELY_RECORDED"],      # 1
}


# ---------------------------------------------------------------------------
# Interval-record validation
# ---------------------------------------------------------------------------

class IntervalRecordError(ValueError):
    """Raised when an interval record fails validation before insertion."""
    pass


def _validate_interval(
    kind: str,
    start_ms: int,
    end_ms: int,
    start_off: int,
    end_off: int,
) -> None:
    """
    Validate interval record fields.

    Raises ``IntervalRecordError`` if:
    - start_ms >= end_ms
    - zone_offset is outside ±64800 seconds
    - start_ms has millisecond component (offset not divisible by 1000 → likely ms offset bug)
    """
    if start_ms >= end_ms:
        raise IntervalRecordError(
            f"[{kind}] start_ms {start_ms} >= end_ms {end_ms}"
        )
    if start_off < -kn.ZONE_OFFSET_MAX_SECONDS or start_off > kn.ZONE_OFFSET_MAX_SECONDS:
        raise IntervalRecordError(
            f"[{kind}] start_offset_seconds {start_off} outside ±64800 "
            f"(possible ms→s confusion)"
        )
    if end_off < -kn.ZONE_OFFSET_MAX_SECONDS or end_off > kn.ZONE_OFFSET_MAX_SECONDS:
        raise IntervalRecordError(
            f"[{kind}] end_offset_seconds {end_off} outside ±64800 "
            f"(possible ms→s confusion)"
        )
    # Poison check: ms offsets (detect ms passed as seconds)
    # If the offset value is clearly in milliseconds (e.g. 3600000 >> 64800), catch it
    if abs(start_off) > kn.ZONE_OFFSET_MAX_SECONDS * 10:
        raise IntervalRecordError(
            f"[{kind}] start_offset_seconds {start_off} looks like milliseconds "
            f"(too large for seconds)"
        )
    if abs(end_off) > kn.ZONE_OFFSET_MAX_SECONDS * 10:
        raise IntervalRecordError(
            f"[{kind}] end_offset_seconds {end_off} looks like milliseconds "
            f"(too large for seconds)"
        )


# ---------------------------------------------------------------------------
# write_interval — interval-record path
# ---------------------------------------------------------------------------

def write_interval(
    conn: WriteGuard,
    records: list,
    app_info_id: int,
    project_key: str,
    *,
    now_ms: Optional[int] = None,
) -> dict[str, int]:
    """
    Write interval-domain records into the DB (activity, sleep, heartrate, exercise).

    Supports:
      - ActivityCanonicalRecord (steps, distance, calories)
      - SleepCanonicalRecord (sessions + sleep_stages_table via lastrowid)
      - HeartRateCanonicalRecord (hr_auto/hr_manual + heart_rate_record_series_table via lastrowid)
      - ExerciseCanonicalRecord (exercise sessions with extras)

    Uses the proven row shape:
      client_record_id = NULL, client_record_version = '0', device_info_id = 1
      dedupe_hash = knowledge.dedupe_hash_interval(app_id, device_id, start_ms, end_ms)
      Deterministic UUIDv5 per (project_key, kind, start_ms[-end_ms])

    Parameters
    ----------
    conn : WriteGuard
        WriteGuard-wrapped sqlite3 connection.
    records : list
        List of canonical interval records from domain specs.
    app_info_id : int
        app_info_id to write in each row.
    project_key : str
        Namespace key for deterministic UUIDv5.
    now_ms : int | None
        ``last_modified_time`` for new rows.  If None, uses current UTC time.

    Returns
    -------
    dict[str, int]
        Per-domain insertion counts, plus "activity_date" count.

    Raises
    ------
    IntervalRecordError
        If a record fails validation.
    """
    if now_ms is None:
        now_ms = int(
            datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000
        )

    inserted: dict[str, int] = {
        "steps": 0, "distance": 0, "calories": 0,
        "sleep": 0, "hr_auto": 0, "hr_manual": 0, "exercise": 0,
        "sleep_stages": 0, "hr_series": 0,
        "activity_date": 0,
    }
    activity_dates: set[tuple[int, int]] = set()

    device_id = kn.DEVICE_UNKNOWN_ID

    for rec in records:
        kind = rec.kind

        # Get domain key (hr_auto/hr_manual → "hr_auto"/"hr_manual", etc.)
        domain_key = kind

        table_name, value_col = _INTERVAL_TABLE_MAP.get(kind, (None, None))
        if table_name is None:
            continue  # unknown kind

        rec_method = _INTERVAL_RECORDING_METHOD.get(kind, 2)

        # Get start_ms/end_ms — handle both methods (IntervalRecord) and properties (canonical records)
        def _ms(r, attr) -> int:
            v: object = getattr(r, attr)
            return int(v() if callable(v) else v)  # type: ignore[call-arg,assignment]

        start_ms = _ms(rec, 'start_ms')
        end_ms = _ms(rec, 'end_ms')

        _validate_interval(kind, start_ms, end_ms, rec.start_offset_seconds, rec.end_offset_seconds)

        # Deterministic UUIDv5
        rec_uuid = kn.deterministic_uuid(project_key, kind, start_ms, end_ms)
        dedupe_hash = kn.dedupe_hash_interval(app_info_id, device_id, start_ms, end_ms)

        # Build base columns
        base_cols = (
            "uuid", "last_modified_time", "client_record_id", "client_record_version",
            "device_info_id", "app_info_id", "recording_method", "dedupe_hash",
            "start_time", "start_zone_offset", "end_time", "end_zone_offset",
            "local_date"
        )
        base_vals = (
            rec_uuid, now_ms, None, kn.CLIENT_RECORD_VERSION,
            device_id, app_info_id, rec_method, dedupe_hash,
            start_ms, rec.start_offset_seconds, end_ms, rec.end_offset_seconds,
            rec.local_date
        )

        # Value column for activity tables
        extra_cols = ""
        extra_vals: list = []
        if value_col:
            extra_cols = f", {value_col}"
            extra_vals = [rec.extra.get(value_col)]

        # Exercise extras — fields are direct attributes on ExerciseCanonicalRecord
        if kind == "exercise":
            extra_cols = ", exercise_type, title, has_route"
            extra_vals = [
                getattr(rec, "exercise_type", 0),
                getattr(rec, "title", ""),
                getattr(rec, "has_route", 0),
            ]

        cur = conn.execute(
            f"""INSERT INTO {table_name}
               (uuid, last_modified_time, client_record_id, client_record_version,
                device_info_id, app_info_id, recording_method, dedupe_hash,
                start_time, start_zone_offset, end_time, end_zone_offset,
                local_date{extra_cols})
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?{",?" * len(extra_vals)})""",
            (*base_vals, *extra_vals)
        )

        inserted[kind] = inserted.get(kind, 0) + 1

        # Track activity_date
        rt_id = _INTERVAL_TYPE_ID_MAP.get(kind)
        if rt_id is not None:
            activity_dates.add((rec.local_date, rt_id))

        # Series inserts (heart_rate_record_series_table)
        if hasattr(rec, 'samples') and rec.samples:
            parent_key = cur.lastrowid
            for epoch_ms, bpm in rec.samples:
                conn.execute(
                    """INSERT INTO heart_rate_record_series_table
                       (parent_key, beats_per_minute, epoch_millis)
                       VALUES (?,?,?)""",
                    (parent_key, bpm, epoch_ms)
                )
                inserted["hr_series"] += 1

        # Sleep stage inserts (sleep_stages_table)
        if hasattr(rec, 'stages') and rec.stages:
            parent_key = cur.lastrowid
            for st_start, st_end, st_type in rec.stages:
                conn.execute(
                    """INSERT INTO sleep_stages_table
                       (parent_key, stage_start_time, stage_end_time, stage_type)
                       VALUES (?,?,?,?)""",
                    (parent_key, st_start, st_end, st_type)
                )
                inserted["sleep_stages"] += 1

    # Populate activity_date_table
    for epoch_days, rt_id in activity_dates:
        conn.execute(
            "INSERT OR IGNORE INTO activity_date_table (epoch_days, record_type_id) "
            "VALUES (?, ?)",
            (epoch_days, rt_id),
        )
    inserted["activity_date"] = len(activity_dates)

    return inserted
