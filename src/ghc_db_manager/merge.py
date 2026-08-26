"""
merge.py — thin orchestrator: load sources, run domain spec, return canonical records.

No I/O beyond reading source files.  All domain logic lives in ``domains/``.
"""

import datetime
import sqlite3
from typing import Optional

from ghc_db_manager import knowledge as kn
from ghc_db_manager.domains.weight import build_weight_canonical, CanonicalRecord as WeightCanonicalRecord
from ghc_db_manager.domains.activity import build_activity_canonical, ActivityCanonicalRecord
from ghc_db_manager.domains.sleep import build_sleep_canonical, SleepCanonicalRecord
from ghc_db_manager.domains.heartrate import build_heartrate_canonical, HeartRateCanonicalRecord
from ghc_db_manager.domains.exercise import build_exercise_canonical, ExerciseCanonicalRecord
from ghc_db_manager.sources import load_source, IntervalRecord


def load_hc_weight_rows(
    db_path: str,
) -> list[tuple[datetime.datetime, float]]:
    """
    Load existing HC weight rows from a READ-ONLY open db.

    Returns list of (utc_datetime, weight_kg) for use in HC exclusion (R4).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT time, weight FROM weight_record_table"
        ).fetchall()
    finally:
        conn.close()
    return [
        (datetime.datetime.fromtimestamp(t / 1000, datetime.timezone.utc), w / 1000.0)
        for t, w in rows
    ]


def get_cutoffs_from_db(db_path: str) -> dict[str, int | None]:
    """
    Read per-table MIN(start_time) cutoffs from a read-only HC export db.

    Returns dict mapping domain name → cutoff_ms (int) or None.
    None means the table is empty → import ALL history (no cutoff).
    For tables that don't exist or have no rows, value is None.
    For tables with existing rows, value is MIN(start_time) in ms.
    """
    interval_tables = [
        ("steps",     "steps_record_table"),
        ("distance",  "distance_record_table"),
        ("calories",  "total_calories_burned_record_table"),
        ("sleep",     "sleep_session_record_table"),
        ("heart_rate","heart_rate_record_table"),
        ("exercise",  "exercise_session_record_table"),
    ]

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cutoffs: dict[str, int | None] = {}
        for domain, table in interval_tables:
            try:
                row = conn.execute(f"SELECT MIN(start_time) FROM {table}").fetchone()
                # GAP-3 fix: MIN(start_time) is NULL when table is empty.
                # Use None (no cutoff = import all history) instead of 0.
                # 0 as cutoff would incorrectly drop ALL records (since all start_ms > 0).
                cutoffs[domain] = row[0] if row and row[0] is not None else None
            except sqlite3.OperationalError:
                cutoffs[domain] = None
        return cutoffs
    finally:
        conn.close()


def merge(
    sources: dict[str, str],
    domain: str = "weight",
    *,
    # Weight-domain options
    zepp_profile_height: Optional[float] = None,
    weight_min: float = 40.0,
    weight_max: float = 250.0,
    hc_db_path: Optional[str] = None,
    priority_source: str = "libra",
    tz: str = "Europe/Warsaw",
) -> tuple[list, dict[str, int]]:
    """
    Load all sources, run the domain spec, return canonical records + stats.

    Parameters
    ----------
    sources : dict[str, str]
        Map of source name → file/directory path.
    domain : str
        Domain to process: "weight" | "activity" | "sleep" | "heartrate" | "exercise".
    zepp_profile_height : float | None
        If set, Zepp rows with different height are filtered out (R1) [weight only].
    weight_min / weight_max : float
        Plausibility band for weight rows (R2) [weight only].
    hc_db_path : str | None
        Path to the HC export db for reading existing rows (R4 exclusion) and cutoffs.
        If None, HC exclusion is skipped and no cutoffs are applied.
    priority_source : str
        Source that wins cross-source collisions (R5, R6) [weight only]. Default: "libra".
    tz : str
        IANA timezone for zone offset computation.

    Returns
    -------
    (canonical_records, rule_stats)
    """
    # Get cutoffs from db if available
    cutoffs: dict[str, int | None] = {}
    if hc_db_path is not None:
        cutoffs = get_cutoffs_from_db(hc_db_path)

    if domain == "weight":
        return _merge_weight(sources, zepp_profile_height, weight_min, weight_max,
                             hc_db_path, priority_source, tz)
    elif domain == "activity":
        return _merge_activity(sources, cutoffs, tz)
    elif domain == "sleep":
        return _merge_sleep(sources, cutoffs, tz)
    elif domain == "heartrate":
        return _merge_heartrate(sources, cutoffs, tz)
    elif domain == "exercise":
        return _merge_exercise(sources, cutoffs, tz)
    else:
        raise NotImplementedError(f"Domain {domain!r} not implemented yet")


def _merge_weight(
    sources: dict[str, str],
    zepp_profile_height: Optional[float],
    weight_min: float,
    weight_max: float,
    hc_db_path: Optional[str],
    priority_source: str,
    tz: str,
) -> tuple[list[WeightCanonicalRecord], dict[str, int]]:
    """Weight-domain merge."""
    hc_rows: list[tuple[datetime.datetime, float]] = []
    if hc_db_path is not None:
        hc_rows = load_hc_weight_rows(hc_db_path)

    all_raw: list = []
    for name, path in sources.items():
        # GAP-6 fix: thread tz to load_source
        records = load_source(name, path, tz)
        all_raw.extend(records)

    canonical, stats = build_weight_canonical(
        all_raw,
        zepp_profile_height=zepp_profile_height,
        weight_min=weight_min,
        weight_max=weight_max,
        hc_rows=hc_rows if hc_rows else None,
        priority_source=priority_source,
        tz=tz,
    )
    return canonical, stats


def _merge_activity(
    sources: dict[str, str],
    cutoffs: dict[str, int | None],
    tz: str,
) -> tuple[list[ActivityCanonicalRecord], dict[str, int]]:
    """Activity-domain merge."""
    all_records: list[IntervalRecord] = []
    for name, path in sources.items():
        # GAP-6 fix: thread tz to load_source
        records = load_source(name, path, tz)
        for r in records:
            if isinstance(r, IntervalRecord) and r.kind in ("steps", "distance", "calories"):
                all_records.append(r)

    # Build activity cutoffs dict
    act_cutoffs = {
        "steps": cutoffs.get("steps", 0),
        "distance": cutoffs.get("distance", 0),
        "calories": cutoffs.get("calories", 0),
    }

    return build_activity_canonical(all_records, cutoffs=act_cutoffs)


def _merge_sleep(
    sources: dict[str, str],
    cutoffs: dict[str, int | None],
    tz: str,
) -> tuple[list[SleepCanonicalRecord], dict[str, int]]:
    """Sleep-domain merge."""
    sleep_records: list[IntervalRecord] = []
    sleep_minute_records: list[IntervalRecord] = []

    for name, path in sources.items():
        # GAP-6 fix: thread tz to load_source
        records = load_source(name, path, tz)
        for r in records:
            if isinstance(r, IntervalRecord):
                if r.kind == "sleep":
                    sleep_records.append(r)
                elif r.kind == "sleep_stage":
                    sleep_minute_records.append(r)

    act_cutoffs = {
        "sleep": cutoffs.get("sleep", 0),
    }

    return build_sleep_canonical(sleep_records, sleep_minute_records, cutoffs=act_cutoffs)


def _merge_heartrate(
    sources: dict[str, str],
    cutoffs: dict[str, int | None],
    tz: str,
) -> tuple[list[HeartRateCanonicalRecord], dict[str, int]]:
    """Heart-rate-domain merge."""
    hr_auto_records: list[IntervalRecord] = []
    hr_manual_records: list[IntervalRecord] = []

    for name, path in sources.items():
        # GAP-6 fix: thread tz to load_source
        records = load_source(name, path, tz)
        for r in records:
            if isinstance(r, IntervalRecord):
                if r.kind == "hr_auto":
                    hr_auto_records.append(r)
                elif r.kind == "hr_manual":
                    hr_manual_records.append(r)

    act_cutoffs = {
        "heart_rate": cutoffs.get("heart_rate", 0),
    }

    return build_heartrate_canonical(hr_auto_records, hr_manual_records, cutoffs=act_cutoffs)


def _merge_exercise(
    sources: dict[str, str],
    cutoffs: dict[str, int | None],
    tz: str,
) -> tuple[list[ExerciseCanonicalRecord], dict[str, int]]:
    """Exercise-domain merge."""
    exercise_records: list[IntervalRecord] = []

    for name, path in sources.items():
        # GAP-6 fix: thread tz to load_source
        records = load_source(name, path, tz)
        for r in records:
            if isinstance(r, IntervalRecord) and r.kind == "exercise":
                exercise_records.append(r)

    act_cutoffs = {
        "exercise": cutoffs.get("exercise", 0),
    }

    return build_exercise_canonical(exercise_records, cutoffs=act_cutoffs)
