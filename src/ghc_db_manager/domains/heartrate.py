"""
domains/heartrate.py — heart-rate domain specification.

hr_auto: one interval record per day spanning first→last sample, samples as list.
hr_manual: 1-sample records, start=ts, end=ts+60s, dedupe identical timestamps.
recording_method: auto=2 (AUTOMATICALLY_RECORDED), manual=3 (MANUAL_ENTRY).

Rules:
  - Day batching for auto HR (one record per day)
  - Manual: start=ts, end=ts+60s
  - Dedup identical timestamps for manual
  - local_date = start-based epoch days
"""

import datetime
from dataclasses import dataclass, field
from typing import Optional

from ghc_db_manager.sources import IntervalRecord


@dataclass
class HeartRateCanonicalRecord:
    """
    Canonical heart-rate interval record.
    """
    source: str
    kind: str  # "hr_auto" | "hr_manual"
    start_utc: datetime.datetime
    end_utc: datetime.datetime
    start_offset_seconds: int
    end_offset_seconds: int
    local_date: int
    samples: list[tuple[int, int]] = field(default_factory=list)  # (epoch_ms, bpm)
    recording_method: int = 2  # 2=auto, 3=manual

    @property
    def start_ms(self) -> int:
        return int(self.start_utc.timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(self.end_utc.timestamp() * 1000)


def build_heartrate_canonical(
    hr_auto_records: list[IntervalRecord],
    hr_manual_records: list[IntervalRecord],
    *,
    cutoffs: Optional[dict[str, int]] = None,
) -> tuple[list[HeartRateCanonicalRecord], dict[str, int]]:
    """
    Build canonical heart-rate records.

    Parameters
    ----------
    hr_auto_records : list[IntervalRecord]
        Output of parse_hr_auto() — day-batched auto HR.
    hr_manual_records : list[IntervalRecord]
        Output of parse_hr_manual() — manual single measurements (deduped).
    cutoffs : dict[str, int] | None
        Cutoff in ms for heart_rate records.
        If None, no cutoff filtering is applied.

    Returns
    -------
    (canonical_records, rule_stats)
    """
    if cutoffs is None:
        cutoffs = {}

    records: list[HeartRateCanonicalRecord] = []
    stats: dict[str, int] = {
        "hr_auto": 0,
        "hr_manual": 0,
        "cutoff_auto": 0,
        "cutoff_manual": 0,
    }

    cutoff = cutoffs.get("heart_rate")

    # hr_auto
    for rec in hr_auto_records:
        if cutoff is not None and rec.start_ms() >= cutoff:
            stats["cutoff_auto"] += 1
            continue

        records.append(HeartRateCanonicalRecord(
            source="zepp",
            kind="hr_auto",
            start_utc=rec.start_utc,
            end_utc=rec.end_utc,
            start_offset_seconds=rec.start_offset_seconds,
            end_offset_seconds=rec.end_offset_seconds,
            local_date=rec.local_date,
            samples=rec.samples,
            recording_method=2,  # AUTOMATICALLY_RECORDED
        ))
        stats["hr_auto"] += 1

    # hr_manual (already deduped in parse_hr_manual)
    for rec in hr_manual_records:
        if cutoff is not None and rec.start_ms() >= cutoff:
            stats["cutoff_manual"] += 1
            continue

        records.append(HeartRateCanonicalRecord(
            source="zepp",
            kind="hr_manual",
            start_utc=rec.start_utc,
            end_utc=rec.end_utc,
            start_offset_seconds=rec.start_offset_seconds,
            end_offset_seconds=rec.end_offset_seconds,
            local_date=rec.local_date,
            samples=rec.samples,
            recording_method=3,  # MANUAL_ENTRY
        ))
        stats["hr_manual"] += 1

    return records, stats
