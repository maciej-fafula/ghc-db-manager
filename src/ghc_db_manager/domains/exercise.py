"""
domains/exercise.py — exercise-session domain specification.

SPORT rows → exercise_session_record_table entries.
end = start + sportTime(s).
exercise_type + title from ZEPP_SPORT_MAP.
has_route = 0.
recording_method = 1 (ACTIVELY_RECORDED).

DOUBLE-COUNT GUARD: NO per-workout distance/calorie records are emitted.
The PoC showed that ACTIVITY already provides daily summaries, and SPORT
records would double-count if we also wrote workout-segment records.
"""

import datetime
from dataclasses import dataclass, field
from typing import Optional

from ghc_db_manager.sources import IntervalRecord


@dataclass
class ExerciseCanonicalRecord:
    """
    Canonical exercise-session record.
    """
    source: str
    start_utc: datetime.datetime
    end_utc: datetime.datetime
    start_offset_seconds: int
    end_offset_seconds: int
    local_date: int
    exercise_type: int
    title: str
    has_route: int = 0  # always 0
    recording_method: int = 1  # always 1 (ACTIVELY_RECORDED)

    @property
    def kind(self) -> str:
        return "exercise"

    @property
    def start_ms(self) -> int:
        return int(self.start_utc.timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(self.end_utc.timestamp() * 1000)


def build_exercise_canonical(
    exercise_records: list[IntervalRecord],
    *,
    cutoffs: dict[str, int | None] | None = None,
) -> tuple[list[ExerciseCanonicalRecord], dict[str, int]]:
    """
    Build canonical exercise records from SPORT data.

    Parameters
    ----------
    exercise_records : list[IntervalRecord]
        Output of parse_sport() — SPORT rows.
    cutoffs : dict[str, int] | None
        Cutoff in ms for exercise sessions.
        If None, no cutoff filtering is applied.

    Returns
    -------
    (canonical_records, rule_stats)
    """
    if cutoffs is None:
        cutoffs = {}

    records: list[ExerciseCanonicalRecord] = []
    stats: dict[str, int] = {
        "sessions": 0,
        "cutoff": 0,
    }

    cutoff = cutoffs.get("exercise")

    for rec in exercise_records:
        s_ms = rec.start_ms()

        if cutoff is not None and s_ms >= cutoff:
            stats["cutoff"] += 1
            continue

        extra = rec.extra

        records.append(ExerciseCanonicalRecord(
            source="zepp",
            start_utc=rec.start_utc,
            end_utc=rec.end_utc,
            start_offset_seconds=rec.start_offset_seconds,
            end_offset_seconds=rec.end_offset_seconds,
            local_date=rec.local_date,
            exercise_type=extra.get("exercise_type", 0),
            title=extra.get("title", ""),
            has_route=extra.get("has_route", 0),
            recording_method=1,  # ACTIVELY_RECORDED
        ))
        stats["sessions"] += 1

    return records, stats
