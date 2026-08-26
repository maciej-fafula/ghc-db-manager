# Case Study: Backfilling Health Connect with Historical Data

**Date:** 2026-08-22/23
**Scope:** One phone, one HC instance, two source ecosystems (Libra, Zepp/Amazfit), ~14 years of history.

---

## The Problem

Health Connect (HC) on the phone held approximately 5 months of data — as far back as the active integrations' history went (2026-03-18 onward). Two parallel, non-cooperating archives existed:

- **Archive A (Libra):** weight tracker with no HC integration — 14 years of measurements (2012–2026), no export beyond CSV.
- **Archive B (Zepp/Huami):** watch companion with partial HC sync (current data only) — 7 years of watch data (2019–2026), no history sent to HC.

The goal: a single, complete HC database containing all historical data, with no duplicates and provenance preserved.

---

## The Approach

### Aggregate numbers

| Wave | Domain | Records | Source |
|------|--------|---------|--------|
| 1 | weight + body fat + lean mass | 750 total (540 + 127 + 83) | Libra + Zepp BODY |
| 2 | steps / distance / calories | 3 × 2,480 daily interval records | Zepp ACTIVITY |
| 2 | sleep sessions + stages | 2,226 sessions + 9,044 stage segments (429 nights) | Zepp SLEEP + SLEEP_MINUTE |
| 2 | heart rate | 457 day-batched records + 288,428 series samples + 169 manual | Zepp HEARTRATE_AUTO + HEARTRATE |
| 2 | exercise sessions | 602 workouts | Zepp SPORT |

**Wave 2 total:** ~10,900 records + 288,428 HR samples + 9,044 sleep stages.

### Attribution

All imported records were attributed to their actual source apps:
- Weight domain → `net.cachapa.libra` (Libra's existing HC app_info entry)
- Zepp domains → `com.huami.watch.hmwatchmanager` (Zepp's existing HC app_info entry)

### Cutoff enforcement

A hard constraint: import only history **strictly before** the earliest existing HC data for each domain. Cutoffs were computed from the fresh export's `MIN(start_time)` per table:

| Domain | Cutoff |
|--------|--------|
| steps | 2026-03-18 |
| distance / calories / sleep | 2026-03-19 |
| heart rate | 2026-04-11 |
| exercise | 2026-05-02 |

No record at or after a cutoff was imported. Re-imports of the same file are idempotent (deterministic UUIDs → conflict-ignored).

---

## Pilot-First Methodology

### Why a pilot is mandatory

HC's import rejects the **entire** file on any per-row parse error. A 750-row wave-1 import and a 10,900-row wave-2 import are both too large to debug by failure. A pilot with 1 record per domain costs one minute to diagnose; a failed full import costs an hour.

### The zone-offset crash and its bisect

The first pilot import (3 records) was rejected. Logcat showed:

```
java.time.DateTimeException: Zone offset not in valid range: -18:00 to +18:00
Import failed during database merge...
```

**Bisect strategy:** Build two modified exports from the same base:
- **Pilot 0:** record-free modification (the base export with no rows added) — **passed**
- **Pilot 2:** rows with millisecond offsets — **rejected**

**Result:** Record-free pass + record-mod reject → the bug is content-level, not file-level. The root cause was `zone_offset` written in **milliseconds** instead of **seconds**. The fix: convert all offsets to seconds (divide by 1000) before INSERT.

The second pilot (same rows, corrected offset unit) passed. Full wave-1 import then passed on the phone.

---

## Validation Results

### Wave 1 (weight domain)

The next scheduled HC export was diffed against the modified database:

- **Byte-level roundtrip:** 540 inserted weight rows — uuid, timestamps, zone offsets, local dates, values, recording method, dedupe hashes — **0 missing, 0 field diffs**.
- Per-app counts: Libra 540, existing HC entries preserved.
- Value range: 2012-05-02 → 2026-08-20, all within 60–160 kg band.
- Zero person-2 leak (118 excluded rows from a shared Zepp BODY export).
- Zero 4.2 kg erroneous entry (excluded by plausibility filter).

### Wave 2 (activity / sleep / HR / exercise)

- **Byte-level roundtrip:** ~10,900 records — **0 missing, 0 field diffs** across all domains.
- **One benign finding:** HC's import pipeline recomputed `local_date` for 462 sleep sessions that had been written end-based. HC normalizes interval records to its canonical local-start formula. The overwrite was silent; no data lost; final state self-consistent. This was an empirical inference error in the builder, not a data loss incident.
- HR series samples (288,428): intact.
- Sleep stage segments (9,044): intact; +11 stages from the night after import (live data).
- Exercise type distribution: dominated by pool swimming (442 sessions) — matches Zepp's sport mix and the sport type mapping table.

---

## Lessons That Shaped the Tool

### 1. Deterministic UUIDs

Using UUIDv5 with `NAMESPACE_URL` and domain-specific keys (`zepp-wave2/<domain>/<start>-<end>`) means the pilot's UUIDs are a subset of the full set's UUIDs. If the pilot passes, the same row shape in the full import is proven. Re-imports are idempotent (conflict-ignored via the uuid UNIQUE constraint). This eliminates the need for a separate test marker and allows any re-import to be safe.

### 2. Invariants as Build Gate

The build script checks every invariant before packing:
- Cutoffs enforced (no record ≥ domain cutoff)
- Zone offsets within ±64800 seconds
- `local_date` formula correct per domain
- uuid and dedupe_hash unique per table
- FK targets exist (app_info_id, device_info_id)
- `PRAGMA integrity_check` passes
- Do-not-touch tables unchanged

If any invariant fails, the ZIP is not created. This makes the import step low-risk — the file reaching the phone has already passed all structural checks.

### 3. Expected-Deviation Diff Model

Post-import validation diffs the next scheduled export against a snapshot. Not all diffs are errors:
- **Live domain growth:** steps, HR, sleep stages all grow slightly after import (apps re-sync)
- **Benign normalization:** `local_date` may change to HC's canonical formula for interval records
- **Deliberate deletions:** expected negatives (e.g. a duplicate removed via HC UI)

The diff model accounts for these. Zero unexpected diffs across all fields of all imported records is the target.

### 4. zone_offset Is Seconds, Not Milliseconds

The single most impactful lesson. The crash was unambiguous in logcat, the bisect was clean, and the fix was mechanical — but it would have destroyed a full import if not caught by the pilot. No amount of reading the AOSP source could replace the empirical pilot signal.

---

## Outcome

The phone's HC database now contains:
- 14 years of weight history (2012–2026) from Libra + Zepp
- 7 years of activity, sleep, heart rate, and workout history (2019–2026) from Zepp
- Live data continuing to grow from active integrations

The daily scheduled HC export is the authoritative backup. The import is complete.
