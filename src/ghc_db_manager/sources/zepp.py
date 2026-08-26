"""
sources/zepp.py — Zepp/Huami adapter (Phase C: BODY.csv; Phase D: all others).

Parses all Zepp CSV categories and emits RawRecords (weight/body_fat) and
IntervalRecords (activity/sleep/heartrate/exercise).

Zepp CSV formats (from backup-zepp/ and mini-zepp/):
  BODY:        time,weight,height,bmi,fatRate,...
  ACTIVITY:    date,steps,distance,runDistance,calories
  SLEEP:       date,deepSleepTime,shallowSleepTime,wakeTime,start,stop,REMTime,naps
  SLEEP_MINUTE: date,time,stage,hr,respiratory_rate
  HEARTRATE_AUTO: date,time,heartRate
  HEARTRATE:   time,heartRate
  SPORT:       type,startTime,sportTime(s),...

Notes:
  - ``null`` is the literal string for missing values.
  - Timestamps in UTC files use +0000 / +00:00 suffix.
  - SLEEP_MINUTE times are LOCAL (Warsaw); date = wake date; hours ≥20
    belong to the previous calendar night.
  - When a directory is passed to load_zepp(), it is walked recursively and
    all CSV files are parsed, with results aggregated by kind.
"""

import csv
import datetime
import pathlib
import sys

from ghc_db_manager.sources import RawRecord, IntervalRecord, register


# ---------------------------------------------------------------------------
# Sentinel for the literal 'null' string in Zepp files
# ---------------------------------------------------------------------------

_NULL = "null"


def _parse_timestamp(time_str: str) -> datetime.datetime:
    """Parse a Zepp UTC timestamp string (ISO-8601 with +0000 / +00:00 suffix)."""
    normalised = time_str.replace("+0000", "+00:00")
    return datetime.datetime.fromisoformat(normalised)


def _null_float(value_str: str) -> float | None:
    """Return float(value) or None if the value is the literal 'null' or empty."""
    v = value_str.strip()
    if v == _NULL or v == "":
        return None
    return float(v)


def _null_int(value_str: str) -> int | None:
    """Return int(value) or None if the value is the literal 'null' or empty."""
    v = value_str.strip()
    if v == _NULL or v == "":
        return None
    return int(v)


# ---------------------------------------------------------------------------
# Timezone helpers for local-time minute data
# ---------------------------------------------------------------------------

from ghc_db_manager.timeutil import zone_offset_seconds

TZ = "Europe/Warsaw"


def _local_time_to_utc(date: datetime.date, time_str: str, tz_name: str = TZ) -> datetime.datetime:
    """
    Convert a LOCAL (date, HH:MM or HH:MM:SS) pair to a UTC datetime.

    The time_str is wall-clock local time; we reconstruct the local datetime,
    look up its offset, and subtract to get UTC.
    """
    parts = time_str.strip().split(":")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) > 2 else 0

    # Local datetime in the target timezone
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    local_dt = datetime.datetime(date.year, date.month, date.day, hour, minute, second, tzinfo=tz)

    # UTC offset at this instant
    utc_offset = local_dt.utcoffset()
    assert utc_offset is not None
    utc_delta = datetime.timedelta(seconds=int(utc_offset.total_seconds()))

    # UTC equivalent
    utc_naive = (local_dt - utc_delta).replace(tzinfo=None)
    return datetime.datetime(
        utc_naive.year, utc_naive.month, utc_naive.day,
        utc_naive.hour, utc_naive.minute, utc_naive.second,
        tzinfo=datetime.timezone.utc
    )


def _evening_previous_day(date: datetime.date, time_str: str, threshold_hour: int = 20) -> datetime.date:
    """
    Apply the evening→previous-day rule.

    If the hour in time_str is >= threshold_hour (default 20), return date - 1 day.
    Otherwise return date as-is.

    This maps SLEEP_MINUTE rows (date = wake date) to their sleep night.
    """
    parts = time_str.strip().split(":")
    hour = int(parts[0])
    if hour >= threshold_hour:
        return date - datetime.timedelta(days=1)
    return date


# ---------------------------------------------------------------------------
# Phase C — BODY (already implemented)
# ---------------------------------------------------------------------------

def _safe_raw_fields(row: dict) -> dict:
    """DictReader rows can contain row[None] = [extra values] when a row has
    more fields than the header (real Zepp SLEEP 'naps' column embeds JSON with
    commas and backslash-escaped quotes that defeat CSV quoting). Keep traceability
    without crashing: skip the None key, stringify lists."""
    out = {}
    for k, v in row.items():
        if k is None:
            out["_extra_fields"] = ",".join(str(x) for x in v) if isinstance(v, list) else str(v)
        elif isinstance(v, str):
            out[k] = v.strip()
        else:
            out[k] = str(v)
    return out

def parse_body(path: str) -> list[RawRecord]:
    """
    Parse a Zepp BODY CSV (UTF-8 with BOM, literal 'null' strings for missing
    values) and emit RawRecords for weight and body_fat.
    """
    records: list[RawRecord] = []

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            time_str = row.get("time", "").strip()
            if not time_str:
                continue

            dt = _parse_timestamp(time_str)

            weight_kg = _null_float(row.get("weight", ""))
            if weight_kg is None:
                continue

            height_str = row.get("height", "").strip()
            height: float | None = float(height_str) if height_str else None

            bf_raw = _null_float(row.get("fatRate", ""))
            bf_value: float | None = bf_raw if (bf_raw is not None and bf_raw > 0) else None

            muscle_rate_raw = row.get("muscleRate", "").strip()
            muscle_rate_note: str | None = None
            if muscle_rate_raw not in (_NULL, ""):
                muscle_rate_note = f"muscleRate={muscle_rate_raw} is percent — not mapped to lean_mass"

            meta = {
                "height": height,
                "bmi": _null_float(row.get("bmi", "")),
                "body_water_rate": _null_float(row.get("bodyWaterRate", "")),
                "bone_mass": _null_float(row.get("boneMass", "")),
                "metabolism": _null_float(row.get("metabolism", "")),
                "visceral_fat": _null_float(row.get("visceralFat", "")),
                "muscle_rate_note": muscle_rate_note,
            }

            raw_fields = _safe_raw_fields(row)

            records.append(
                RawRecord(
                    source="zepp",
                    kind="weight",
                    time_utc=dt,
                    value=weight_kg,
                    meta=meta,
                    raw=raw_fields,
                )
            )

            if bf_value is not None and bf_value > 0:
                records.append(
                    RawRecord(
                        source="zepp",
                        kind="body_fat",
                        time_utc=dt,
                        value=bf_value,
                        meta=meta,
                        raw=raw_fields,
                    )
                )

    return records


# ---------------------------------------------------------------------------
# Phase D — ACTIVITY
# ---------------------------------------------------------------------------

def parse_activity(path: str, tz: str = TZ) -> list[IntervalRecord]:
    """
    Parse a Zepp ACTIVITY CSV (daily summaries: steps, distance, calories).

    Each row: date,steps,distance,runDistance,calories
    One IntervalRecord per day per metric (steps/distance/calories).

    GAP-9 fix: emit all three records (steps/distance/calories) whenever the
    day is not all-zero, INCLUDING zero-valued metrics (PoC parity).

    GAP-6 fix: tz parameter threads through for local-time conversions.
    """
    records: list[IntervalRecord] = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("date", "").strip()
            if not date_str:
                continue

            day = datetime.date.fromisoformat(date_str)

            steps = _null_int(row.get("steps", "")) or 0
            distance = _null_int(row.get("distance", "")) or 0
            calories = _null_int(row.get("calories", "")) or 0

            if steps == 0 and distance == 0 and calories == 0:
                continue  # all-zero day skip

            # Local midnight in UTC (GAP-6 fix: use tz param)
            from ghc_db_manager.timeutil import local_midnight_as_utc
            start_utc = local_midnight_as_utc(day, tz)
            end_utc = start_utc + datetime.timedelta(days=1)

            start_off = zone_offset_seconds(start_utc, tz)
            end_off = zone_offset_seconds(end_utc, tz)

            from ghc_db_manager import knowledge as kn
            local_date = kn.local_date_epoch_days(int(start_utc.timestamp() * 1000), start_off)

            raw_fields = _safe_raw_fields(row)

            # GAP-9 fix: emit ALL three records when day is not all-zero,
            # INCLUDING zero-valued metrics (PoC parity).
            records.append(IntervalRecord(
                kind="steps",
                start_utc=start_utc,
                end_utc=end_utc,
                start_offset_seconds=start_off,
                end_offset_seconds=end_off,
                local_date=local_date,
                extra={"count": steps},
                raw=raw_fields,
            ))

            records.append(IntervalRecord(
                kind="distance",
                start_utc=start_utc,
                end_utc=end_utc,
                start_offset_seconds=start_off,
                end_offset_seconds=end_off,
                local_date=local_date,
                extra={"distance": float(distance)},
                raw=raw_fields,
            ))

            records.append(IntervalRecord(
                kind="calories",
                start_utc=start_utc,
                end_utc=end_utc,
                start_offset_seconds=start_off,
                end_offset_seconds=end_off,
                local_date=local_date,
                extra={"energy": float(calories) * 1000.0},  # kcal × 1000
                raw=raw_fields,
            ))

    return records


# ---------------------------------------------------------------------------
# Phase D — SLEEP
# ---------------------------------------------------------------------------

def parse_sleep(path: str) -> list[IntervalRecord]:
    """
    Parse a Zepp SLEEP CSV (sleep sessions with start/stop UTC times).

    Each row: date,deepSleepTime,shallowSleepTime,wakeTime,start,stop,REMTime,naps
    Skips placeholder rows where start == stop.
    """
    records: list[IntervalRecord] = []
    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            start_str = row.get("start", "").strip()
            stop_str = row.get("stop", "").strip()

            if not start_str or not stop_str:
                continue
            if start_str == stop_str:
                continue  # placeholder skip

            s_dt = _parse_timestamp(start_str)
            e_dt = _parse_timestamp(stop_str)

            from ghc_db_manager import knowledge as kn
            s_off = zone_offset_seconds(s_dt, TZ)
            e_off = zone_offset_seconds(e_dt, TZ)

            # Sleep sessions: local_date is START-based (HC canonical recomputed form, per PoC E.6)
            local_date = kn.local_date_epoch_days(int(s_dt.timestamp() * 1000), s_off)

            raw_fields = _safe_raw_fields(row)

            records.append(IntervalRecord(
                kind="sleep",
                start_utc=s_dt,
                end_utc=e_dt,
                start_offset_seconds=s_off,
                end_offset_seconds=e_off,
                local_date=local_date,
                raw=raw_fields,
            ))

    return records


def parse_sleep_minute(path: str, tz: str = TZ) -> list[IntervalRecord]:
    """
    Parse a Zepp SLEEP_MINUTE CSV (per-minute sleep stage data).

    Each row: date,time,stage,hr,respiratory_rate
    - date = wake date (the morning date)
    - time = LOCAL time (Warsaw)
    - stage = LIGHT / DEEP / REM / WAKE
    - Hours ≥20 belong to the PREVIOUS calendar night (evening rule)

    Groups consecutive same-stage minutes into segments, then emits one
    IntervalRecord per night (date = wake date) with stages list.

    Real-data audit (2026-08): 207 duplicate (night_date, time_str) rows exist
    across 4 re-sync blocks. Deduplication uses keep-FIRST policy (documented
    choice: 11 real dup keys have conflicting stages; keep-first is deliberate).
    """
    from collections import defaultdict

    # Group minutes by wake-date
    mins_by_wake_date: dict[datetime.date, list[dict]] = defaultdict(list)

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("date", "").strip()
            time_str = row.get("time", "").strip()
            stage = row.get("stage", "").strip()

            if not date_str or not time_str or not stage:
                continue
            if stage not in ("LIGHT", "DEEP", "REM", "WAKE"):
                continue

            wake_date = datetime.date.fromisoformat(date_str)

            # Apply evening rule: if hour >= 20, this belongs to previous night
            night_date = _evening_previous_day(wake_date, time_str)

            mins_by_wake_date[wake_date].append({
                "night_date": night_date,
                "time_str": time_str,
                "stage": stage,
            })

    # ---- GAP-1 FIX: dedupe (night_date, time_str) BEFORE segmentation ----
    # Real data has 207 duplicate (date,time) rows; duplicates fragment segments.
    # keep-FIRST policy: 11 real dup keys have conflicting stages — first wins.
    for wake_date in mins_by_wake_date:
        seen: dict[tuple, dict] = {}
        for m in mins_by_wake_date[wake_date]:
            key = (m["night_date"], m["time_str"])
            if key not in seen:
                seen[key] = m
        mins_by_wake_date[wake_date] = list(seen.values())

    # Build stage segments per wake date
    records: list[IntervalRecord] = []
    from ghc_db_manager import knowledge as kn

    for wake_date, mins in mins_by_wake_date.items():
        stages: list[tuple[int, int, int]] = []
        segs: list[list] = []  # [start_utc, end_utc, stage_str]

        # Sort by night_date then time
        sorted_mins = sorted(mins, key=lambda m: (m["night_date"], m["time_str"]))

        for m in sorted_mins:
            night_date = m["night_date"]
            time_str = m["time_str"]
            stage_str = m["stage"]

            # Convert local time to UTC (GAP-6 fix: use tz param instead of hardcoded global)
            t_utc = _local_time_to_utc(night_date, time_str, tz)

            if (
                segs
                and segs[-1][2] == stage_str
                and t_utc == segs[-1][1] + datetime.timedelta(minutes=1)
            ):
                # Same stage AND strictly consecutive minute — extend end.
                # (PoC parity: gaps in minute data start a NEW segment; merging
                # across gaps silently fabricates stage coverage.)
                segs[-1][1] = t_utc
            else:
                segs.append([t_utc, t_utc, stage_str])

        # Convert segments to (start_ms, end_ms_exclusive, stage_id) tuples
        # end_ms = start of next minute (start + 1 min)
        for seg in segs:
            a_utc = seg[0]
            b_utc = seg[1]
            stage_str = seg[2]

            stage_id = kn.SLEEP_STAGE_IDS.get(stage_str)
            if stage_id is None:
                continue

            start_ms = int(a_utc.timestamp() * 1000)
            # End is the start of the minute AFTER the last minute
            # If segment is a single minute at a_utc, end = a_utc + 1 min
            end_ms = int((b_utc + datetime.timedelta(minutes=1)).timestamp() * 1000)

            stages.append((start_ms, end_ms, stage_id))

        if stages:
            records.append(IntervalRecord(
                kind="sleep_stage",
                start_utc=datetime.datetime.now(datetime.timezone.utc),  # placeholder; not used for stages-only
                end_utc=datetime.datetime.now(datetime.timezone.utc),
                local_date=int(wake_date.toordinal()),  # not used
                stages=stages,
                raw={"wake_date": str(wake_date), "minute_count": len(mins)},
            ))

    return records


# ---------------------------------------------------------------------------
# Phase D — HEARTRATE_AUTO
# ---------------------------------------------------------------------------

def parse_hr_auto(path: str, tz: str = TZ) -> list[IntervalRecord]:
    """
    Parse a Zepp HEARTRATE_AUTO CSV (every-5-minute automatic samples).

    Each row: date,time,heartRate
    Groups by day, emits one IntervalRecord per day with samples list.
    """
    from collections import defaultdict

    samples_by_day: dict[datetime.date, list[tuple[int, int]]] = defaultdict(list)

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            date_str = row.get("date", "").strip()
            time_str = row.get("time", "").strip()
            hr_str = row.get("heartRate", "").strip()

            if not date_str or not time_str:
                continue

            day = datetime.date.fromisoformat(date_str)
            bpm = _null_int(hr_str)
            if bpm is None:
                continue

            # Convert local time to UTC (GAP-6 fix: use tz param)
            t_utc = _local_time_to_utc(day, time_str, tz)
            epoch_ms = int(t_utc.timestamp() * 1000)

            samples_by_day[day].append((epoch_ms, bpm))

    records: list[IntervalRecord] = []
    for day, samples in sorted(samples_by_day.items()):
        if not samples:
            continue

        samples_sorted = sorted(samples)  # sort by epoch_ms
        first_ms = samples_sorted[0][0]
        last_ms = samples_sorted[-1][0]

        first_utc = datetime.datetime.fromtimestamp(first_ms / 1000, datetime.timezone.utc)
        last_utc = datetime.datetime.fromtimestamp(last_ms / 1000, datetime.timezone.utc)

        from ghc_db_manager import knowledge as kn
        # GAP-6 fix: use tz param instead of hardcoded TZ
        start_off = zone_offset_seconds(first_utc, tz)
        end_off = zone_offset_seconds(last_utc, tz)
        local_date = kn.local_date_epoch_days(first_ms, start_off)

        records.append(IntervalRecord(
            kind="hr_auto",
            start_utc=first_utc,
            end_utc=last_utc,
            start_offset_seconds=start_off,
            end_offset_seconds=end_off,
            local_date=local_date,
            samples=samples_sorted,
            raw={"day": str(day), "sample_count": len(samples_sorted)},
        ))

    return records


# ---------------------------------------------------------------------------
# Phase D — HEARTRATE (manual)
# ---------------------------------------------------------------------------

def parse_hr_manual(path: str) -> list[IntervalRecord]:
    """
    Parse a Zepp HEARTRATE CSV (manual single measurements).

    Each row: time,heartRate
    Each record: start=ts, end=ts+60s.
    Deduplicates identical timestamps.
    """
    seen: set[str] = set()
    records: list[IntervalRecord] = []

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            time_str = row.get("time", "").strip()
            hr_str = row.get("heartRate", "").strip()

            if not time_str:
                continue
            if time_str in seen:
                continue
            seen.add(time_str)

            bpm = _null_int(hr_str)
            if bpm is None:
                continue

            dt = _parse_timestamp(time_str)
            dt_end = dt + datetime.timedelta(minutes=1)

            from ghc_db_manager import knowledge as kn
            off = zone_offset_seconds(dt, TZ)
            local_date = kn.local_date_epoch_days(int(dt.timestamp() * 1000), off)

            records.append(IntervalRecord(
                kind="hr_manual",
                start_utc=dt,
                end_utc=dt_end,
                start_offset_seconds=off,
                end_offset_seconds=zone_offset_seconds(dt_end, TZ),
                local_date=local_date,
                samples=[(int(dt.timestamp() * 1000), bpm)],
                raw={"time": time_str, "heartRate": hr_str},
            ))

    return records


# ---------------------------------------------------------------------------
# Phase D — SPORT (exercise)
# ---------------------------------------------------------------------------

from ghc_db_manager.knowledge import ZEPP_SPORT_MAP


def parse_sport(path: str, tz: str = TZ, stats_out: dict | None = None) -> list[IntervalRecord]:
    """
    Parse a Zepp SPORT CSV (workout sessions).

    Each row: type,startTime,sportTime(s),...
    end = start + sportTime.
    Maps type → (HC exercise_type, title) via ZEPP_SPORT_MAP.
    NO distance/calorie records are emitted (double-count guard).

    GAP-6 fix: tz parameter for local-time conversions.
    GAP-11 fix: unknown sport types are counted as `unknown_sport_type` in stats
    (returned via the stats_out parameter) instead of silently dropped.
    """
    records: list[IntervalRecord] = []

    with open(path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            type_str = row.get("type", "").strip()
            start_str = row.get("startTime", "").strip()
            dur_str = row.get("sportTime(s)", "").strip()

            if not type_str or not start_str or not dur_str:
                continue

            sport_type = ZEPP_SPORT_MAP.get(type_str)
            if sport_type is None:
                # GAP-11: unknown sport types must not vanish silently
                if stats_out is not None:
                    stats_out["unknown_sport_type"] = stats_out.get("unknown_sport_type", 0) + 1
                print(
                    f"WARNING: unknown Zepp sport type {type_str!r} in {pathlib.Path(path).name} "
                    f"— row skipped (extend ZEPP_SPORT_MAP to import it)",
                    file=sys.stderr,
                )
                continue

            hc_type, title = sport_type

            s_dt = _parse_timestamp(start_str)
            dur_s = int(dur_str)
            e_dt = s_dt + datetime.timedelta(seconds=dur_s)

            from ghc_db_manager import knowledge as kn
            # GAP-6 fix: use tz param
            s_off = zone_offset_seconds(s_dt, tz)
            e_off = zone_offset_seconds(e_dt, tz)
            local_date = kn.local_date_epoch_days(int(s_dt.timestamp() * 1000), s_off)

            raw_fields = _safe_raw_fields(row)

            records.append(IntervalRecord(
                kind="exercise",
                start_utc=s_dt,
                end_utc=e_dt,
                start_offset_seconds=s_off,
                end_offset_seconds=e_off,
                local_date=local_date,
                extra={
                    "exercise_type": hc_type,
                    "title": title,
                    "has_route": 0,
                },
                raw=raw_fields,
            ))

    return records


# ---------------------------------------------------------------------------
# Unified load function (used by the registry)
# ---------------------------------------------------------------------------

# Real Zepp exports name files with a timestamp suffix (BODY_1787404253776.csv),
# so dispatch matches by PREFIX, most specific first (SLEEP_MINUTE before SLEEP,
# HEARTRATE_AUTO before HEARTRATE). ACTIVITY_MINUTE/ACTIVITY_STAGE are not
# imported (PoC scope) and are explicitly skipped.
_FILE_DISPATCH: list[tuple[str, str | None]] = [
    ("SLEEP_MINUTE", "parse_sleep_minute"),
    ("HEARTRATE_AUTO", "parse_hr_auto"),
    ("ACTIVITY_MINUTE", None),   # not imported (out of PoC scope)
    ("ACTIVITY_STAGE", None),    # not imported (out of PoC scope)
    ("BODY", "parse_body"),
    ("ACTIVITY", "parse_activity"),
    ("SLEEP", "parse_sleep"),
    ("HEARTRATE", "parse_hr_manual"),
    ("SPORT", "parse_sport"),
]


def _dispatch_zepp_file(path: str, tz: str = TZ) -> list:
    """GAP-6 fix: tz parameter threaded through dispatch."""
    stem = pathlib.Path(path).stem.upper()
    for prefix, fn_name in _FILE_DISPATCH:
        if stem.startswith(prefix):
            if fn_name is None:
                return []
            fn = globals()[fn_name]
            # Pass tz to parse functions that accept it
            if fn_name in ("parse_sleep_minute", "parse_hr_auto",
                           "parse_activity", "parse_sport"):
                return fn(path, tz)
            return fn(path)
    return []


def load_zepp(path: str, tz: str = TZ) -> list[RawRecord | IntervalRecord]:
    """
    Load all Zepp data from a path.

    GAP-6 fix: accepts optional tz IANA timezone string, threaded to all
    parse functions that do local-time conversions (SLEEP_MINUTE, HEARTRATE_AUTO,
    ACTIVITY, SPORT).

    If path is a file → dispatches to the appropriate parse_* function based
    on filename prefix (BODY_* → parse_body, ACTIVITY_* → parse_activity, etc.
    — real exports suffix filenames with timestamps).

    If path is a directory → walks the directory tree, finds all .csv files,
    parses them all, and returns aggregated records.

    Returns a mixed list of RawRecord (weight/body_fat) and IntervalRecord
    (activity/sleep/heartrate/exercise).
    """
    p = pathlib.Path(path)

    if p.is_file():
        return _dispatch_zepp_file(path, tz)

    elif p.is_dir():
        results: list[RawRecord | IntervalRecord] = []
        for csv_path in p.rglob("*.csv"):
            results.extend(load_zepp(str(csv_path), tz))
        return results

    return []


# ---------------------------------------------------------------------------
# Registry — register the unified loader
# ---------------------------------------------------------------------------

register("zepp", load_zepp)
