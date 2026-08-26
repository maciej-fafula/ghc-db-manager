# ghc-db-manager — Architecture Proposal

**Status:** proposal (PoC proven 2026-08-22/23 — see `docs/case-study-poc.md`)
**License:** MIT, absolutely no warranty — the tool edits personal health databases.

## 1. What it is

A Python CLI that backfills historical health data (weight from Libra, activity/sleep/HR/workouts from Zepp, or generic CSV) into an Android **Health Connect export database**, producing a ZIP the user re-imports on the phone via HC's built-in restore. Offline, local, no phone connection needed except for the final manual import.

## 2. Design principles (each one paid for in the PoC)

| Principle | Origin |
|---|---|
| Source db is read-only; all work on copies | rollback safety |
| Cutoffs auto-derived from the freshest export (`MIN(start_time)` per table); nothing ≥ cutoff is ever inserted | "no duplicates of live data" requirement |
| Deterministic UUIDv5 per (project, domain, interval) | pilot ⊆ full set; idempotent re-imports (conflict-ignored) |
| One proven row shape (crid NULL, version '0', device 1, formula dedupe_hash) | the shape that passed a real import |
| All HC internals in ONE knowledge module (units, seconds-offsets, ids, formulas) | the `zone_offset`-in-ms crash must be structurally impossible |
| Invariants gate every build; failed invariant = no ZIP | a rejected full import costs an hour |
| Pilot-first workflow is mandatory, not optional | bisect debugging cost ~2h in the PoC |
| Post-import diff understands expected deviations (live growth, HC local_date normalization) | E.6 lesson |

## 3. Component architecture

```
┌──────────────┐   ┌──────────────┐   ┌────────────┐   ┌─────────┐   ┌───────────┐
│  sources/    │ → │  domains/    │ → │  merge.py  │ → │ writer  │ → │ packing   │
│ libra, zepp, │   │ weight,      │   │ dedup rule │   │ inserts │   │ zip +     │
│ generic CSV  │   │ activity,    │   │ engine     │   │ +series │   │ checksums │
│ (adapters)   │   │ sleep, HR,   │   │ (ordered)  │   │ +stages │   └───────────┘
└──────────────┘   │ exercise     │   └────────────┘   └────┬────┘
                   │ (specs)      │                         │
                   └──────────────┘                  ┌──────▼──────┐
┌──────────────┐                                     │ validation/ │
│ knowledge.py │ ──── referenced by everything  ──→  │ invariants, │
│ HC internals │                                     │ diff        │
└──────────────┘                                     └─────────────┘
```

- **`knowledge.py`** — single source of truth: units, zone-offset-seconds, dedupe_hash formulas, local_date rules, record-type & stage ids, sport mapping, do-not-touch table list. No other module hardcodes these.
- **`sources/`** — adapters producing a common `RawRecord` stream; registry pattern so new sources are one file each.
- **`domains/`** — per-domain specs: field mapping, plausibility filters, dedup rules, attribution default, local_date basis. A domain = instant or interval + optional series/stages.
- **`merge.py`** — ordered rule engine (filter → intra-source dedup → HC-exclusion → cross-source dedup), emits canonical records (CSV + JSON artifacts for provenance).
- **`writer.py`** — inserts with the proven shape; deterministic UUIDs; populates `activity_date_table`; handles series/stages via `lastrowid`.
- **`validation/`** — pre-import invariants (hard gate) and post-import diff (uuid-keyed roundtrip, expected-deviation model).
- **`timeutil.py`** — zone offsets via `zoneinfo` (PoC hardcoded Europe/Warsaw; generalize to any IANA zone, DST-correct).

## 4. CLI surface

```
ghcdb inspect  <export.db>                       # coverage + cutoffs report
ghcdb plan    --db F --source libra=… [--domains …]  # dry-run, dedup stats, attribution review
ghcdb pilot                                      # 1 record/domain zip (subset of full)
ghcdb build                                      # canonical + modified db + full zip (invariants-gated)
ghcdb validate <modified.db>                     # run invariants alone
ghcdb diff <snapshot.db> <fresh.db>              # post-import validation
```

Config via `--attr <domain>=<package>` (attribution), `--tz Europe/Warsaw`, `--project-key` (UUID namespace salt).

## 5. Directory structure

```
ghc-db-manager/
├── LICENSE                      # MIT
├── README.md                    # user-facing: what/why/how + big no-warranty banner
├── ghc-db-manager-skill.md      # LLM operating manual (this repo's differentiator)
├── ARCHITECTURE.md              # this file
├── pyproject.toml               # py3.10+, deps: none beyond stdlib (sqlite3, zoneinfo, uuid, argparse)
├── src/ghc_db_manager/
│   ├── __init__.py
│   ├── cli.py
│   ├── knowledge.py
│   ├── timeutil.py
│   ├── dbio.py                  # read-only open, copy, do-not-touch guard
│   ├── merge.py
│   ├── writer.py
│   ├── packing.py
│   ├── sources/{__init__,libra,zepp,generic_csv}.py
│   ├── domains/{__init__,weight,activity,sleep,heartrate,exercise}.py
│   └── validation/{__init__,invariants,diff}.py
├── tests/
│   ├── fixtures/make_fixture_db.py   # tiny synthetic HC-schema db builder
│   ├── test_knowledge.py             # formula roundtrips vs known-good vectors from PoC
│   ├── test_sources_*.py
│   ├── test_merge.py
│   ├── test_writer.py                # incl. the ms-offset regression test
│   └── test_diff.py
└── docs/
    ├── hc-internals.md          # expanded §4 knowledge with evidence
    └── case-study-poc.md        # this PoC: 2 waves, 11.6k records, byte-exact validation
```

## 6. Implementation plan (port from PoC)

1. **M1 skeleton:** `knowledge.py` + `dbio.py` + `inspect` + fixture db + tests (port constants & formulas verbatim from PoC scripts; regression test: offsets must be seconds).
2. **M2 weight domain:** port `merge_weight.py` + `insert_weight_history.py` into sources/domains/writer; `plan`/`build`/`pilot`/`validate` commands.
3. **M3 remaining domains:** port `build_wave2.py` (activity, sleep+stages, HR+series, exercise+sport map).
4. **M4 diff:** port E.6 validation with expected-deviation model.
5. **M5 polish:** generic CSV adapter, README, packaging, release as `ghc-db-manager` (console script `ghcdb`).

PoC scripts map 1:1 onto modules — the port is mostly extraction + tests, not redesign.

## 7. Risks / open questions

- HC schema evolves (`user_version` 23 today); `knowledge.py` should carry a version table and `inspect` should warn on unknown versions.
- Import behavior (merge semantics, local_date recompute) is undocumented and can change in future HC releases — the skill mandates pilot-first, which detects breakage cheaply.
- Not all users have `adb`; phone-side failure diagnostics without logcat degrade to "try pilot again".
