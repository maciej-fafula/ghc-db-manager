"""
domains/weight.py — weight-domain specification.

Ordered dedup / filter rules ported from PoC merge_weight.py spec §3.

Rule ordering (applied sequentially):
  R1  Profile filter        — zepp: keep only rows where height == profile_height
                              (libra: no filter by default)
  R2  Plausibility band     — keep only rows where 40 kg ≤ weight ≤ 250 kg
                              (configurable via ``weight_min`` / ``weight_max``)
  R3  Intra-source dup ts   — for records with identical UTC timestamps within
                              the same source: richer row wins; derived rows
                              (body_fat/lean_mass) from dropped weights are also
                              dropped at that timestamp.
  R4  HC exclusion          — same rounded value (±0.1 kg) AND |Δt| ≤ 2 s as an
                              existing HC row → drop the new row
  R5  Cross-source collision — identical UTC timestamp across sources:
                              priority_source wins (libra by default)
  R6  Same-measurement day  — same calendar day, Δt ≤ 30 min AND |Δw| ≤ 0.5 kg,
                              OR |Δw| == 0 AND Δt ≤ 2 h → drop lower-priority row
  R7  Derived kinds inherit — body_fat / lean_mass records are dropped if their
                              parent weight row was dropped by any earlier rule.

Zone assignment:
  - Libra "date-only" entries (22:00:00.000Z / 23:00:00.000Z patterns) get
    fixed offsets: +7200 s / +3600 s respectively.
  - All other entries: ``zone_offset_seconds(time_utc, tz)`` with the configured
    IANA timezone (default Europe/Warsaw).

Output: CanonicalRecord list + per-rule statistics dict.
"""

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from ghc_db_manager.sources import RawRecord
from ghc_db_manager.timeutil import zone_offset_seconds


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

WEIGHT_MIN_KG = 40.0
WEIGHT_MAX_KG = 250.0

LIBRA_MIDNIGHT_SUMMER_OFFSET = 7200
LIBRA_MIDNIGHT_WINTER_OFFSET = 3600


# ---------------------------------------------------------------------------
# Canonical record
# ---------------------------------------------------------------------------

@dataclass
class CanonicalRecord:
    """
    A canonicalised weight-domain record ready for writing to the DB.
    """
    source: str
    kind: str
    time_utc: datetime.datetime
    ms: int
    zone_offset_seconds: int
    local_date: int
    value: float
    unit: str
    priority: int
    parent_ms: Optional[int] = None
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Zone offset helpers
# ---------------------------------------------------------------------------

def zone_offset_for_libra(dt: datetime.datetime) -> int:
    hhmmssms = (dt.hour, dt.minute, dt.second, dt.microsecond)
    if hhmmssms == (22, 0, 0, 0):
        return LIBRA_MIDNIGHT_SUMMER_OFFSET
    if hhmmssms == (23, 0, 0, 0):
        return LIBRA_MIDNIGHT_WINTER_OFFSET
    # For non-midnight Libra entries, use the configured tz (GAP-6 fix)
    return zone_offset_seconds(dt, "Europe/Warsaw")


def zone_offset_for_source(source: str, dt: datetime.datetime, tz: str = "Europe/Warsaw") -> int:
    """
    GAP-6 fix: tz parameter threads through to zone_offset_seconds for non-Libra sources.
    Libra always uses its fixed-offset rules; only non-midnight entries fall back to tz.
    """
    if source == "libra":
        return zone_offset_for_libra(dt)
    return zone_offset_seconds(dt, tz)


def _ms(rec: RawRecord) -> int:
    return int(rec.time_utc.timestamp() * 1000)


# ---------------------------------------------------------------------------
# R1 — profile filter
# ---------------------------------------------------------------------------

def rule_r1_profile_filter(
    records: list[RawRecord],
    *,
    zepp_profile_height: Optional[float] = None,
) -> tuple[list[RawRecord], int, set[int]]:
    """
    Filter Zepp rows by profile height.
    Returns (kept, dropped_count, kept_weight_ms).

    GAP-14 fix: when zepp_profile_height is given, height=None rows are DROPPED
    (PoC strict equality: if filter is active, None means unknown → drop).
    This is counted in stats as R1_profile_filter.
    """
    if zepp_profile_height is None:
        kept_ws = {_ms(r) for r in records if r.kind == "weight"}
        return list(records), 0, kept_ws

    kept: list[RawRecord] = []
    dropped = 0
    kept_ws: set[int] = set()
    for rec in records:
        if rec.source == "zepp":
            h = rec.meta.get("height")
            # GAP-14 fix: when filter is active, height=None → DROP (strict equality)
            # h is None OR h != zepp_profile_height → drop
            if h is None or h != zepp_profile_height:
                dropped += 1
                continue
        kept.append(rec)
        if rec.kind == "weight":
            kept_ws.add(_ms(rec))
    return kept, dropped, kept_ws


# ---------------------------------------------------------------------------
# R2 — plausibility band
# ---------------------------------------------------------------------------

def rule_r2_plausibility(
    records: list[RawRecord],
    kept_ws: set[int],
    *,
    weight_min: float = WEIGHT_MIN_KG,
    weight_max: float = WEIGHT_MAX_KG,
) -> tuple[list[RawRecord], int, set[int]]:
    """
    Drop weight records outside [weight_min, weight_max].
    Returns (kept, dropped_count, updated_kept_ws).
    """
    kept: list[RawRecord] = []
    dropped = 0
    new_ws: set[int] = set()
    for rec in records:
        if rec.kind == "weight":
            if not (weight_min <= rec.value <= weight_max):
                dropped += 1
                continue
            new_ws.add(_ms(rec))
        kept.append(rec)
    # Union with previous weight ms (only for non-weight records)
    kept_ws = kept_ws | new_ws
    return kept, dropped, kept_ws


# ---------------------------------------------------------------------------
# R3 — intra-source duplicate timestamps: richer row wins
# ---------------------------------------------------------------------------

def _richness_key(rec: RawRecord) -> tuple:
    """
    Compute a richness score for R3 dedup tiebreaking.

    GAP-4 fix: originally only read Libra-style meta keys (bf_raw, mm_raw).
    Zepp dup-ts rows always tie on those → first-in-file wins unfairly.
    Now also uses Zepp signals (fatRate, body_water_rate, bmi) as tiebreakers.
    """
    return (
        1 if rec.kind == "weight" else 0,
        1 if rec.kind == "body_fat" else 0,
        # Libra-style tiebreakers
        1 if (rec.meta.get("bf_raw") is not None) else 0,
        1 if (rec.meta.get("mm_raw") is not None) else 0,
        # GAP-4 fix: Zepp-specific tiebreakers
        1 if (rec.meta.get("fatRate") is not None) else 0,
        1 if (rec.meta.get("body_water_rate") is not None) else 0,
        1 if (rec.meta.get("bmi") is not None) else 0,
    )


def rule_r3_intra_source_dedup(
    records: list[RawRecord],
    kept_ws: set[int],
) -> tuple[list[RawRecord], int, set[int]]:
    """
    For records with identical (source, ms), keep only the richest weight row.
    All derived rows from dropped weights are also dropped.
    Returns (kept, dropped_count, updated_kept_ws).
    """
    groups: dict[tuple, list[RawRecord]] = defaultdict(list)
    for rec in records:
        groups[(rec.source, _ms(rec))].append(rec)

    kept: list[RawRecord] = []
    dropped = 0
    winning_ws: set[int] = set()

    for (_src, _ms_val), group in groups.items():
        if len(group) == 1:
            kept.extend(group)
            for r in group:
                if r.kind == "weight":
                    winning_ws.add(_ms(r))
        else:
            # Multiple rows at same (source, ms)
            weight_rows = [r for r in group if r.kind == "weight"]
            if weight_rows:
                richest = max(weight_rows, key=_richness_key)
                winning_ms = _ms(richest)
                winning_ws.add(winning_ms)
                for r in group:
                    if r.kind == "weight":
                        if r is richest:
                            kept.append(r)
                        else:
                            dropped += 1
                    else:
                        # Derived row — only keep if its timestamp matches winning weight
                        if _ms(r) == winning_ms:
                            kept.append(r)
                        else:
                            dropped += 1
            else:
                # No weight row — keep first derived row
                kept.append(group[0])
                dropped += len(group) - 1

    # Update kept_ws: remove ws that were dropped
    kept_ws = (kept_ws & winning_ws) | winning_ws
    return kept, dropped, kept_ws


# ---------------------------------------------------------------------------
# R4 — HC exclusion (±2 s tolerance on same rounded value)
# ---------------------------------------------------------------------------

def rule_r4_hc_exclusion(
    records: list[RawRecord],
    kept_ws: set[int],
    hc_rows: list[tuple[datetime.datetime, float]],
) -> tuple[list[RawRecord], int, set[int]]:
    """
    Drop weight records matching an existing HC row (same rounded value ±0.1 kg,
    |Δt| ≤ 2 s).  HC rows are only for weight; body_fat/lean_mass are checked
    via R7 after this step.
    Returns (kept, dropped_count, updated_kept_ws).
    """
    def in_hc(dt: datetime.datetime, val: float) -> bool:
        for hc_dt, hc_val in hc_rows:
            if round(hc_val, 1) == round(val, 1) and abs(
                (hc_dt - dt).total_seconds()
            ) <= 2:
                return True
        return False

    kept: list[RawRecord] = []
    dropped = 0
    new_ws: set[int] = set()
    for rec in records:
        if rec.kind == "weight" and in_hc(rec.time_utc, rec.value):
            dropped += 1
            continue
        kept.append(rec)
        if rec.kind == "weight":
            new_ws.add(_ms(rec))
    kept_ws = (kept_ws & new_ws) | new_ws
    return kept, dropped, kept_ws


# ---------------------------------------------------------------------------
# R5 — cross-source exact-timestamp collision: priority source wins
# ---------------------------------------------------------------------------

def rule_r5_exact_ts_collision(
    records: list[RawRecord],
    kept_ws: set[int],
    priority_source: str = "libra",
) -> tuple[list[RawRecord], int, set[int]]:
    """
    Keep all priority_source rows; drop non-priority rows whose ms collides
    with a priority_source row.
    Returns (kept, dropped_count, updated_kept_ws).
    """
    priority_ms: set[int] = {
        _ms(r) for r in records if r.source == priority_source
    }

    kept: list[RawRecord] = []
    dropped = 0
    new_ws: set[int] = set()
    for rec in records:
        if rec.source == priority_source or _ms(rec) not in priority_ms:
            kept.append(rec)
            if rec.kind == "weight":
                new_ws.add(_ms(rec))
        else:
            dropped += 1
    kept_ws = (kept_ws & new_ws) | new_ws
    return kept, dropped, kept_ws


# ---------------------------------------------------------------------------
# R6 — same-measurement day: drop lower-priority row
# ---------------------------------------------------------------------------

def rule_r6_same_measurement_day(
    records: list[RawRecord],
    kept_ws: set[int],
    priority_source: str = "libra",
) -> tuple[list[RawRecord], int, set[int]]:
    """
    For same-day pairs:
      - Δt ≤ 30 min AND |Δw| ≤ 0.5 kg  → drop lower-priority row
      - |Δw| == 0 AND Δt ≤ 2 h          → drop lower-priority row
    Returns (kept, dropped_count, updated_kept_ws).
    """
    weight_by_date: dict[datetime.date, list[RawRecord]] = defaultdict(list)
    for rec in records:
        if rec.kind == "weight" and _ms(rec) in kept_ws:
            weight_by_date[rec.time_utc.date()].append(rec)

    drop_ms: set[int] = set()

    for _date, day_records in weight_by_date.items():
        for i, rec_a in enumerate(day_records):
            for rec_b in day_records[i + 1:]:
                if rec_a.source == rec_b.source:
                    continue
                # Determine lower-priority record
                if rec_a.source == priority_source:
                    lower_ms = _ms(rec_b)
                elif rec_b.source == priority_source:
                    lower_ms = _ms(rec_a)
                else:
                    continue

                dt_ms = abs(_ms(rec_a) - _ms(rec_b))
                dt_min = dt_ms / 60_000
                dw = abs(rec_a.value - rec_b.value)

                # NOTE: no break — every pair must be evaluated (a priority row
                # can match multiple lower-priority rows; PoC parity 2024-01-30:
                # Libra 07:33 vs Zepp 07:32 AND Zepp 08:32 both Δw==0, Δt≤2h)
                if dt_min <= 30 and dw <= 0.5:
                    drop_ms.add(lower_ms)
                elif dw == 0 and dt_min <= 120:
                    drop_ms.add(lower_ms)

    kept: list[RawRecord] = []
    dropped = 0
    new_ws: set[int] = set()
    for rec in records:
        if rec.kind == "weight" and _ms(rec) in drop_ms:
            dropped += 1
            continue
        kept.append(rec)
        if rec.kind == "weight":
            new_ws.add(_ms(rec))
    # R6 only removes from kept_ws; it never adds new weights (only R1-R3 do)
    kept_ws = kept_ws - drop_ms
    return kept, dropped, kept_ws


# ---------------------------------------------------------------------------
# R7 — derived kinds inherit parent weight decision
# ---------------------------------------------------------------------------

def rule_r7_derived_inherit(
    records: list[RawRecord],
    kept_ws: set[int],
) -> tuple[list[RawRecord], int]:
    """
    Drop body_fat / lean_mass rows whose parent weight row was dropped.
    Returns (kept, dropped_count).
    """
    kept: list[RawRecord] = []
    dropped = 0
    for rec in records:
        if rec.kind != "weight":
            if _ms(rec) not in kept_ws:
                dropped += 1
                continue
        kept.append(rec)
    return kept, dropped


# ---------------------------------------------------------------------------
# Sanity gates (GAP-5: port from PoC merge_weight.py §8)
# ---------------------------------------------------------------------------

def run_weight_sanity_gates(
    canonical: list["CanonicalRecord"],
    *,
    zepp_profile_height: Optional[float] = None,
    weight_min: float = WEIGHT_MIN_KG,
    weight_max: float = WEIGHT_MAX_KG,
) -> tuple[list[str], bool]:
    """
    Run post-merge sanity gates on canonical weight records.

    Ported from PoC merge_weight.py §8 with adaptations:
      - Band is 40-250 kg (PoC used 60-160 dataset-specifically; generic 40-250 chosen)
      - Min-ts: warn if oldest record > 30 years back (simple warning, no hard fail)
      - Hard fail: (source, kind, ts) uniqueness violation

    Parameters
    ----------
    canonical : list[CanonicalRecord]
        Output of build_weight_canonical.
    zepp_profile_height : float | None
        If set, Zepp rows with different height should not be present (leak check).
    weight_min / weight_max : float
        Plausibility band.

    Returns
    -------
    (warnings, hard_fail)
        warnings: list of human-readable warning messages
        hard_fail: True if any gate failed hard (build should abort)
    """
    import sys
    warnings: list[str] = []
    hard_fail = False

    # (a) HARD WARN when Zepp weight rows present and --zepp-height NOT given
    # (person-2 leak risk — PoC merge_weight.py:152 check)
    zepp_weights = [r for r in canonical if r.source == "zepp" and r.kind == "weight"]
    if zepp_weights and zepp_profile_height is None:
        msg = (
            "HARD WARN: Zepp weight rows are present but --zepp-height was NOT given. "
            "Person-2 leak risk (height=None means all Zepp rows pass R1). "
            "Pass --zepp-height to filter."
        )
        print(msg, file=sys.stderr)
        warnings.append(msg)
        hard_fail = True

    # (b) Value-band recheck on canonical output
    for r in canonical:
        if r.kind == "weight":
            if not (weight_min <= r.value <= weight_max):
                msg = (
                    f"weight out of band: {r.source}/{r.kind} at "
                    f"{r.time_utc.isoformat()} value={r.value} not in [{weight_min}, {weight_max}]"
                )
                warnings.append(msg)

    # (c) (source, kind, ts) uniqueness
    seen_keys: set[tuple] = set()
    for r in canonical:
        key = (r.source, r.kind, r.time_utc.isoformat())
        if key in seen_keys:
            msg = f"(source, kind, ts) not unique: {r.source}/{r.kind} at {r.time_utc.isoformat()}"
            warnings.append(msg)
            hard_fail = True
        seen_keys.add(key)

    # (d) Min-ts sanity (warn if > 30 years back)
    if canonical:
        import datetime as dt
        now = dt.datetime.now(dt.timezone.utc)
        min_ts = min(r.time_utc for r in canonical)
        age_years = (now - min_ts).days / 365.25
        if age_years > 30:
            msg = (
                f"min-ts sanity: oldest record is {age_years:.1f} years old "
                f"({min_ts.date()}) — verify this is intentional"
            )
            warnings.append(msg)

    return warnings, hard_fail


# ---------------------------------------------------------------------------
# Build canonical records
# ---------------------------------------------------------------------------

def build_weight_canonical(
    raw_records: list[RawRecord],
    *,
    zepp_profile_height: Optional[float] = None,
    weight_min: float = WEIGHT_MIN_KG,
    weight_max: float = WEIGHT_MAX_KG,
    hc_rows: Optional[list[tuple[datetime.datetime, float]]] = None,
    priority_source: str = "libra",
    tz: str = "Europe/Warsaw",
) -> tuple[list[CanonicalRecord], dict[str, int]]:
    """
    Run the full weight-domain rule pipeline on ``raw_records``.

    Returns (canonical_records, rule_stats).
    """
    from ghc_db_manager import knowledge as kn

    stats: dict[str, int] = {}
    # A Zepp DIRECTORY source yields mixed RawRecord + IntervalRecord lists;
    # the weight domain consumes only instant kinds.
    records = [r for r in raw_records if getattr(r, "kind", None) in ("weight", "body_fat", "lean_mass")]

    # Track kept weight ms through each rule
    kept_ws: set[int] = set()

    # R1
    records, n, kept_ws = rule_r1_profile_filter(
        records, zepp_profile_height=zepp_profile_height
    )
    if n:
        stats["R1_profile_filter"] = n

    # R2
    records, n, kept_ws = rule_r2_plausibility(
        records, kept_ws, weight_min=weight_min, weight_max=weight_max
    )
    if n:
        stats["R2_plausibility"] = n

    # R3
    records, n, kept_ws = rule_r3_intra_source_dedup(records, kept_ws)
    if n:
        stats["R3_intra_source_dedup"] = n

    # R4
    if hc_rows:
        records, n, kept_ws = rule_r4_hc_exclusion(records, kept_ws, hc_rows)
        if n:
            stats["R4_hc_exclusion"] = n

    # R5
    records, n, kept_ws = rule_r5_exact_ts_collision(
        records, kept_ws, priority_source=priority_source
    )
    if n:
        stats["R5_exact_ts_collision"] = n

    # R6
    records, n, kept_ws = rule_r6_same_measurement_day(
        records, kept_ws, priority_source=priority_source
    )
    if n:
        stats["R6_same_measurement_day"] = n

    # R7
    records, n = rule_r7_derived_inherit(records, kept_ws)
    if n:
        stats["R7_derived_inherit"] = n

    # Final weight ms set
    final_ws: set[int] = {
        _ms(r) for r in records if r.kind == "weight"
    }

    # Build CanonicalRecords
    canonical: list[CanonicalRecord] = []
    for rec in records:
        if rec.kind != "weight" and _ms(rec) not in final_ws:
            continue  # should not happen, but be safe
        # GAP-6 fix: pass tz to zone_offset_for_source
        off = zone_offset_for_source(rec.source, rec.time_utc, tz)
        ld = kn.local_date_epoch_days(_ms(rec), off)
        unit = "kg" if rec.kind in ("weight", "lean_mass") else "percent"
        priority = 0 if rec.source == priority_source else 1

        # Parent weight ms for derived kinds
        parent_ms: Optional[int] = None
        if rec.kind != "weight":
            parent_ms = _ms(rec)  # same timestamp as parent weight

        canonical.append(
            CanonicalRecord(
                source=rec.source,
                kind=rec.kind,
                time_utc=rec.time_utc,
                ms=_ms(rec),
                zone_offset_seconds=off,
                local_date=ld,
                value=rec.value,
                unit=unit,
                priority=priority,
                parent_ms=parent_ms,
                meta=rec.meta,
            )
        )

    # GAP-5: run sanity gates on canonical output
    gate_warnings, gate_hard_fail = run_weight_sanity_gates(
        canonical,
        zepp_profile_height=zepp_profile_height,
        weight_min=weight_min,
        weight_max=weight_max,
    )
    if gate_warnings:
        stats["gate_warnings"] = len(gate_warnings)
    if gate_hard_fail:
        stats["gate_hard_fail"] = 1

    return canonical, stats
