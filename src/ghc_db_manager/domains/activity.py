"""
domains/activity.py — activity-domain specification (steps, distance, calories).

Per-day interval records from Zepp ACTIVITY CSV.
Each calendar day → up to 3 interval records (steps, distance, calories).

Rules:
  - Skip all-zero days (steps=0 AND distance=0 AND calories=0)
  - Cutoff enforcement per table (from HC db inspection)
  - local_date = start-based epoch days
  - energy = kcal × 1000
  - distance in meters as-is
"""

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ghc_db_manager.sources import IntervalRecord


# ---------------------------------------------------------------------------
# Canonical interval record for activity domain
# ---------------------------------------------------------------------------

@dataclass
class ActivityCanonicalRecord:
    """
    Canonical activity-domain interval record.
    """
    source: str
    kind: str  # "steps" | "distance" | "calories"
    start_utc: datetime.datetime
    end_utc: datetime.datetime
    start_offset_seconds: int
    end_offset_seconds: int
    local_date: int
    extra: dict = field(default_factory=dict)

    @property
    def start_ms(self) -> int:
        return int(self.start_utc.timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(self.end_utc.timestamp() * 1000)


def build_activity_canonical(
    interval_records: list[IntervalRecord],
    *,
    cutoffs: Optional[dict[str, int]] = None,
) -> tuple[list[ActivityCanonicalRecord], dict[str, int]]:
    """
    Build canonical activity records from IntervalRecords.

    Filters:
      - Skips all-zero days (steps=0 AND distance=0 AND calories=0)
      - Enforces cutoffs per table (steps, distance, calories)

    Parameters
    ----------
    interval_records : list[IntervalRecord]
        Output of parse_activity().
    cutoffs : dict[str, int] | None
        Per-table cutoffs as dict: {"steps": ms, "distance": ms, "calories": ms}.
        If None, no cutoff filtering is applied.

    Returns
    -------
    (canonical_records, rule_stats)
    """
    if cutoffs is None:
        cutoffs = {}

    records: list[ActivityCanonicalRecord] = []
    stats: dict[str, int] = {
        "cutoff_steps": 0,
        "cutoff_distance": 0,
        "cutoff_calories": 0,
    }

    # Group by date and kind
    by_day_kind: dict[tuple, dict] = defaultdict(dict)

    for rec in interval_records:
        key = (rec.start_utc.date(), rec.kind)
        by_day_kind[key][rec.kind] = rec

    for (day, kind), recs_dict in sorted(by_day_kind.items()):
        # Get the record for this day/kind
        rec = recs_dict.get(kind)
        if rec is None:
            continue

        # Check cutoff
        cutoff = cutoffs.get(rec.kind)
        if cutoff is not None and rec.start_ms() >= cutoff:
            stats[f"cutoff_{rec.kind}"] += 1
            continue

        records.append(ActivityCanonicalRecord(
            source="zepp",
            kind=rec.kind,
            start_utc=rec.start_utc,
            end_utc=rec.end_utc,
            start_offset_seconds=rec.start_offset_seconds,
            end_offset_seconds=rec.end_offset_seconds,
            local_date=rec.local_date,
            extra=rec.extra,
        ))

    return records, stats
