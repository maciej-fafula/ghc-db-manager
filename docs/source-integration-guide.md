# Source Integration Guide

**How to add support for a new tracker or CSV export format.**

This guide describes two independent paths:

- **Track 1 — prepare data** (no code): export to CSV, write a `generic_csv` mapping file, import. No adapter code needed.
- **Track 2 — write an adapter** (Python code): implement a source adapter that emits `RawRecord` / `IntervalRecord` streams, register it, write a fixture + test.

Both tracks share the same mandatory checklist before any real import.

---

## Track 1: Prepare Data with `generic_csv`

Use this when your tracker or app can export a CSV file but has no dedicated adapter.

### Step 1 — Export to CSV

Export your data to a plain CSV file. Keep the following in mind:

- One measurement per row.
- Column headers should be human-readable (they become your mapping keys).
- Timestamps should be in a parseable format (ISO 8601 is safest).
- Missing values: leave the cell empty. Do not use `null`, `N/A`, or `-`.

### Step 2 — Write the Mapping File

The mapping file is a JSON file that tells `generic_csv`:

- Which CSV column contains the timestamp.
- Which columns contain which measurements (with unit suffix).
- The timezone of the timestamps.
- The timestamp format.

Create a file named `<your-data>-mapping.json` next to your CSV file.

```json
{
  "columns": {
    "time":        "Timestamp",
    "weight_kg":   "Weight (kg)",
    "body_fat":    "Body Fat %",
    "lean_mass":   "Muscle (kg)"
  },
  "time_format": "ISO8601",
  "tz":          "Europe/Warsaw",
  "encoding":    "utf-8-sig",
  "delimiter":   ","
}
```

**Unit suffix is required.** Declare `weight_kg` not `weight`, `body_fat` not `fat`, `lean_mass` not `muscle`. Without the suffix the adapter refuses the mapping with a clear error — this prevents kg/pounds and percent/decimal-point ambiguities.

### Step 3 — Verify the Mapping

Run `ghcdb plan` with the generic source to do a dry-run:

```bash
ghcdb plan --db health_connect_export.db \
  --source generic=/path/to/your-data-mapping.json
```

Review the output. If you see parse errors, fix the `time_format` or column names in the mapping.

### Limits

`generic_csv` emits **instant kinds only**: `weight`, `body_fat`, `lean_mass`. Interval-domain kinds (activity, sleep, heart rate, exercise) require a full adapter because they need source-specific session logic (pairing start/stop rows, per-minute stage aggregation, etc.).

---

## Track 2: Write an Adapter

Use this when Track 1 is insufficient — most commonly for interval-domain data or trackers with quirks that cannot be expressed in a column map.

### The Adapter Contract

A source adapter is a callable that accepts a file/directory path and returns a list of `RawRecord` (instant) or `IntervalRecord` (interval) objects.

```python
from ghc_db_manager.sources import RawRecord, IntervalRecord, register

def parse(path: str) -> list[RawRecord | IntervalRecord]:
    records: list[RawRecord | IntervalRecord] = []
    # ... parse logic ...
    return records

register("mytracker", parse)
```

Place the adapter file in `src/ghc_db_manager/sources/` as `mytracker.py`. It will be imported lazily when you call `ghcdb plan --source mytracker=/path/to/data`.

### `RawRecord` — Instant Measurements

Used for: weight, body_fat, lean_mass.

```python
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class RawRecord:
    source: str          # "mytracker"
    kind: str            # "weight" | "body_fat" | "lean_mass"
    time_utc: datetime   # timezone-aware UTC datetime
    value: float         # value in source unit (kg for weight/lean, percent for body_fat)
    meta: dict           # source-specific extra fields (kept for provenance)
    raw: dict            # raw parsed fields for debugging
    ms: int              # epoch ms (computed from time_utc if not given)
```

### `IntervalRecord` — Interval Data

Used for: activity (steps/distance/calories), sleep sessions + stages, heart rate series, exercise sessions.

```python
@dataclass
class IntervalRecord:
    source: str
    kind: str            # "steps" | "distance" | "calories" |
                        # "sleep" | "sleep_stage" |
                        # "hr_auto" | "hr_manual" | "exercise"
    start_utc: datetime
    end_utc: datetime
    start_offset_seconds: int   # local TZ offset at start_utc
    end_offset_seconds: int     # local TZ offset at end_utc
    local_date: int            # epoch days of local date
    samples: list              # for hr_auto/hr_manual: [(epoch_ms, bpm), ...]
    stages: list               # for sleep: [(stage_start_ms, stage_end_ms, HC_stage_id), ...]
    extra: dict                # domain-specific (e.g. exercise_type, count, distance)
    raw: dict
```

### Common Quirks to Handle

| Situation | How to handle |
|---|---|
| Missing value sentinel (e.g. `null` string) | Check for the sentinel before converting; skip if None |
| Local-time timestamps | Convert to UTC using `zone_offset_seconds()` from `timeutil.py` |
| Multi-person exports | Filter by a distinguishing column (e.g. height) in the adapter |
| Placeholder rows (start == stop, zero values) | Skip them in the adapter |
| Duplicate timestamps | Deduplicate in the adapter before returning |
| Second-person data | Filter by a profile field (e.g. height in Zepp BODY) |
| Per-minute stage data | Group by night, merge consecutive same-stage minutes into segments |

### Registry Entry

Call `register("sourcename", parse_func)` at module level. The name is case-insensitive. After registration, use `ghcdb plan --source sourcename=/path/to/data`.

### RawRecord vs CanonicalRecord

The adapter emits `RawRecord` (or `IntervalRecord`). The merge layer (`merge.py`) applies domain rules and emits `CanonicalRecord` objects that carry `zone_offset_seconds`, `local_date`, `unit`, and `priority`. The canonical layer is what the writer inserts. Adapters should **not** compute `local_date` or zone offsets — the merge layer handles that using the timezone you pass to `ghcdb plan --tz`.

### Reusing vs Writing a Domain Spec

If your tracker emits the same kinds as an existing domain (e.g. weight from a new scale), you **reuse the existing domain spec** — just add your adapter to the registry and `merge.py` will automatically include it in the weight merge.

If your tracker emits a **new record type** (e.g. blood pressure, which has both a systolic and diastolic value and no existing domain), you need to write a new domain spec in `domains/`. Study the existing weight domain (`domains/weight.py`) as a template — the structure is: plausibility filter → dedup rules → attribution → canonical record builder.

---

## Mandatory Checklist (Both Tracks)

Complete every item before running `ghcdb build`. Skip items at your own risk.

### Fixtures

- [ ] **Synthetic fixture CSV** created in `tests/fixtures/`. Must include:
  - [ ] Duplicate timestamps (same `time_utc` + kind)
  - [ ] Missing values in optional columns
  - [ ] Out-of-band values (implausible weight, body fat outside 1–60%)
  - [ ] DST boundary timestamps (transition days in the target timezone)
  - [ ] At-least-one row at or after the cutoff time (R3 filter)
  - [ ] A second-person row (multi-person tracker only)
- [ ] Fixture added to `tests/fixtures/mini-<sourcename>/` directory

### Tests

- [ ] **Happy-path unit test**: fixture parses to expected counts per kind
- [ ] **Refusal test**: ambiguous column name (no unit suffix) raises `MappingError`
- [ ] **Refusal test**: missing declared column raises `MappingError`
- [ ] **Refusal test**: unparseable timestamp row raises `MappingError` with row numbers
- [ ] **Registry smoke test**: `load_source("sourcename", path)` returns records
- [ ] **Golden test** (`test_golden_*.py`): deterministic byte-exact build output (pilot ⊆ full set)
- [ ] **Invariant tests** green on fixture database

### Pilot

- [ ] `ghcdb pilot --db <real-export.db> --source sourcename=<data-path> --out pilot`
- [ ] Pilot ZIP uploaded to phone, imported via HC → Import data
- [ ] All pilot records verified in HC UI (values, timestamps, attribution)
- [ ] For interval domains: statistics (avg/min/max) checked against source values

### Full Import

- [ ] `ghcdb build --db <real-export.db> --source sourcename=<data-path> --out full`
- [ ] Full ZIP imported on phone
- [ ] **Post-import diff**: `ghcdb diff <pre-import-snapshot.db> <next-scheduled-export.db>`
  - [ ] `--expected-deletions` flags set for any deliberate deletions
  - [ ] `EXPECTED_GROWTH` for live domains is understood and expected
  - [ ] Verdict is `PASS` or `PASS_WITH_EXPECTED_DEVIATIONS`

---

## Worked Example: FooFit Scale (Both Tracks)

### The Source Data

FooFit exports a single CSV:

```csv
Date,WeightKg,FatPct,MuscleKg
2026-01-15,75.2,22.1,58.6
2026-01-22,74.8,21.9,58.3
2026-02-01,74.5,21.8,58.1
```

---

### Track 1: FooFit via `generic_csv`

**Step 1** — mapping file `foofit-mapping.json`:

```json
{
  "columns": {
    "time":      "Date",
    "weight_kg": "WeightKg",
    "body_fat":  "FatPct",
    "lean_mass": "MuscleKg"
  },
  "time_format": "%Y-%m-%d",
  "tz":         "Europe/Warsaw",
  "encoding":   "utf-8",
  "delimiter":  ","
}
```

Note: `weight_kg` (not `weight`), `body_fat` (not `FatPct` alone — the adapter checks suffix, not spelling).

**Step 2** — plan:

```bash
ghcdb plan --db health_connect_export.db \
  --source generic=foofit-mapping.json \
  --domains weight
```

**Step 3** — review output, then pilot and build normally.

---

### Track 2: FooFit Adapter

If you prefer a code adapter (e.g. if FooFit has quirks that make the mapping file insufficient):

**`src/ghc_db_manager/sources/foofit.py`**:

```python
"""sources/foofit.py — FooFit scale adapter (worked example)."""

import csv
import datetime

from ghc_db_manager.sources import RawRecord, register


def _parse_date(value: str, tz: str = "Europe/Warsaw") -> datetime.datetime:
    """Parse '2026-01-15' as local midnight → UTC."""
    from ghc_db_manager.timeutil import local_midnight_as_utc
    d = datetime.date.fromisoformat(value)
    return local_midnight_as_utc(d, tz)


def parse(path: str) -> list[RawRecord]:
    records: list[RawRecord] = []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            dt = _parse_date(row["Date"])

            if row["WeightKg"]:
                records.append(RawRecord(
                    source="foofit", kind="weight",
                    time_utc=dt, value=float(row["WeightKg"]),
                    meta={}, raw=dict(row),
                ))

            if row["FatPct"]:
                records.append(RawRecord(
                    source="foofit", kind="body_fat",
                    time_utc=dt, value=float(row["FatPct"]),
                    meta={}, raw=dict(row),
                ))

            if row["MuscleKg"]:
                records.append(RawRecord(
                    source="foofit", kind="lean_mass",
                    time_utc=dt, value=float(row["MuscleKg"]),
                    meta={}, raw=dict(row),
                ))
    return records


register("foofit", parse)
```

**Use**:

```bash
ghcdb plan --db health_connect_export.db \
  --source foofit=foofit-export.csv \
  --domains weight
```

The adapter is a one-file drop-in. No changes to `merge.py`, `domains/`, or `writer.py` are needed for instant-kind data.
