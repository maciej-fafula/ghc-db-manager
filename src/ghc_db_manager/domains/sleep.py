"""
domains/sleep.py — sleep-domain specification.

Sleep sessions from Zepp SLEEP CSV (start/stop UTC) with stages from
SLEEP_MINUTE CSV (grouped by night date, evening ≥20h → previous day rule).

Rules:
  - Skip placeholder rows (start == stop)
  - local_date = start-based epoch days (HC canonical recomputed form, per PoC E.6 / hc-internals.md §4.4)
  - Stages from SLEEP_MINUTE:
      * Group by wake date
      * Evening hour (≥20) minutes belong to previous calendar night
      * Merge consecutive same-stage minutes into segments
      * Clip segments to session bounds
      * Nights without SLEEP_MINUTE → session only (no fabricated stages)
  - stage ids from knowledge.SLEEP_STAGE_IDS
"""

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ghc_db_manager.sources import IntervalRecord


@dataclass
class SleepCanonicalRecord:
    """
    Canonical sleep-domain interval record.
    """
    source: str
    start_utc: datetime.datetime
    end_utc: datetime.datetime
    start_offset_seconds: int
    end_offset_seconds: int
    local_date: int
    stages: list[tuple[int, int, int]] = field(default_factory=list)  # (start_ms, end_ms, stage_type)
    raw: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return "sleep"

    @property
    def start_ms(self) -> int:
        return int(self.start_utc.timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(self.end_utc.timestamp() * 1000)


def build_sleep_canonical(
    sleep_records: list[IntervalRecord],
    sleep_minute_records: list[IntervalRecord],
    *,
    cutoffs: dict[str, int | None] | None = None,
) -> tuple[list[SleepCanonicalRecord], dict[str, int]]:
    """
    Build canonical sleep records from SLEEP sessions and SLEEP_MINUTE stages.

    Parameters
    ----------
    sleep_records : list[IntervalRecord]
        Output of parse_sleep() — sleep sessions with start/stop UTC.
    sleep_minute_records : list[IntervalRecord]
        Output of parse_sleep_minute() — per-night stage data.
    cutoffs : dict[str, int] | None
        Cutoff in ms for sleep sessions.
        If None, no cutoff filtering is applied.

    Returns
    -------
    (canonical_records, rule_stats)
    """
    if cutoffs is None:
        cutoffs = {}

    # GAP-8 fix: stages_by_wake_date was using dict assignment (later file overwrites
    # earlier). Now accumulate/merge: extend list and dedupe by (start_ms, stage).
    stages_by_wake_date: dict[str, list[tuple[int, int, int]]] = {}
    for rec in sleep_minute_records:
        wake_date_str = rec.raw.get("wake_date", "")
        if wake_date_str and rec.stages:
            if wake_date_str not in stages_by_wake_date:
                stages_by_wake_date[wake_date_str] = []
            # Extend with stages from this file
            for stage_tuple in rec.stages:
                stages_by_wake_date[wake_date_str].append(stage_tuple)
    # Dedupe by (start_ms, stage) keeping first
    for wake_date_str in stages_by_wake_date:
        seen: dict[tuple, tuple] = {}
        for st in stages_by_wake_date[wake_date_str]:
            key = (st[0], st[2])  # (start_ms, stage_type)
            if key not in seen:
                seen[key] = st
        stages_by_wake_date[wake_date_str] = list(seen.values())

    records: list[SleepCanonicalRecord] = []
    stats: dict[str, int] = {
        "sessions": 0,
        "sessions_with_stages": 0,
        "cutoff": 0,
        "skipped_inverted": 0,  # source rows with stop <= start (Zepp export defect)
    }

    cutoff = cutoffs.get("sleep")

    for rec in sleep_records:
        s_dt = rec.start_utc
        e_dt = rec.end_utc
        s_ms = rec.start_ms()
        e_ms = rec.end_ms()

        # Zepp export defect (real data 2026-08 audit): some rows have
        # stop = start - 60s (e.g. 22:00:00 → 21:59:00). Skip with a stat
        # instead of crashing the build or importing garbage intervals.
        if s_ms >= e_ms:
            stats["skipped_inverted"] += 1
            continue

        # Check cutoff
        if cutoff is not None and s_ms >= cutoff:
            stats["cutoff"] += 1
            continue

        # Find stages for this session
        session_date_str = rec.raw.get("date", "")
        stages = list(stages_by_wake_date.get(session_date_str, []))

        if stages:
            # Clip stages to session bounds
            clipped: list[tuple[int, int, int]] = []
            for st_start, st_end, st_type in stages:
                if st_end <= s_ms or st_start >= e_ms:
                    continue  # outside session
                # Clip to session
                clipped_start = max(st_start, s_ms)
                clipped_end = min(st_end, e_ms)
                if clipped_end > clipped_start:
                    clipped.append((clipped_start, clipped_end, st_type))
            stages = clipped

        stats["sessions"] += 1
        if stages:
            stats["sessions_with_stages"] += 1

        records.append(SleepCanonicalRecord(
            source="zepp",
            start_utc=s_dt,
            end_utc=e_dt,
            start_offset_seconds=rec.start_offset_seconds,
            end_offset_seconds=rec.end_offset_seconds,
            local_date=rec.local_date,
            stages=stages,
            raw=rec.raw,
        ))

    return records, stats
