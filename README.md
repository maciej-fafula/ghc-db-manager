<!--
 SPDX-License-Identifier: MIT

 ghc-db-manager — Health Connect database backfill tool
 Copyright 2026 ghc-db-manager contributors

 Subject to the full MIT licence terms: reproduction and use of this
 software is permitted provided this notice is preserved.

 ABSOLUTELY NO WARRANTY — This software is provided 'as-is', without
 any express or implied warranty, including but not limited to the
 implied warranties of merchantability, fitness for a particular purpose,
 and non-infringement.  In no event shall the authors or copyright
 holders be liable for any claim, damages, or other liability, whether
 in an action of contract, tort, or otherwise, arising from, out of,
 or in connection with the software or the use or other dealings in
 the software.

 This tool edits personal health databases.  Incorrect use can result
 in data loss.  Always work on a copy of the export database, always
 run the pilot step before any full import, and keep the pre-import
 export as a rollback.
-->
[![status: experimental](https://img.shields.io/badge/status-experimental-orange)](https://github.com/ghc-db-manager/ghc-db-manager)
[![verified: HC user_version 23](https://img.shields.io/badge/HC%20user__version-23-blue)](docs/hc-internals.md)

# ghc-db-manager

**Backfill Health Connect with historical tracker data.**

`ghc-db-manager` edits an Android Health Connect export database to add historical
records from Libra, Zepp, or any CSV export, then re-imports it on the phone via
Health Connect's built-in restore.  No ADB needed.  No phone connection needed.
All work is local and offline.

---

## What It Does

Health Connect (HC) stores health data on the phone in a SQLite database.
When you export that database (Settings → Manage data → Backup and restore →
Export data) you get a ZIP containing `health_connect_export.db`.  That file is
what `ghc-db-manager` operates on.

The tool:

1. **Reads** your tracker exports (CSV from Libra, Zepp, or any generic CSV).
2. **Plans** the import: shows deduplication statistics, conflicting values,
   and attribution.
3. **Builds** a modified copy of the export database with the new records inserted.
4. **Packs** it into a ZIP you import on the phone.

Backfills are **indistinguishable from app data** in HC — they carry the
attribution of the source app and appear alongside live recordings.

---

## Six-Command Workflow

### 1 — Inspect

```bash
ghcdb inspect health_connect_export.db
```

Report per-table coverage (min/max timestamps, per-app counts) and computed
cutoffs.  Run this on a **fresh** export — the cutoffs (earliest timestamps per
table) define what will be imported and what will be skipped.

### 2 — Plan

```bash
ghcdb plan \
  --db health_connect_export.db \
  --source libra=libra-export.csv \
  --source zepp=zepp-backup/ \
  --source generic=myfit-mapping.json \
  --domains weight,sleep,activity,heartrate,exercise \
  --tz Europe/Warsaw
```

Dry run.  Shows per-domain statistics: how many records pass the plausibility
filters, how many are deduplicated against HC, and what the attribution would be.
Review the attribution and any data filters before proceeding.

### 3 — Pilot

```bash
ghcdb pilot \
  --db health_connect_export.db \
  --source libra=libra-export.csv \
  --out pilot
```

Builds a **1-record-per-domain** pilot ZIP.  This is a subset of the full
import using the same deterministic UUIDs — so the full import later is
conflict-ignored on those records.

**Upload the pilot ZIP to Drive, import it on the phone, and verify every
record in the HC UI** before running the full build.  A rejected pilot costs
a minute; a rejected full import costs an hour.

### 4 — Build

```bash
ghcdb build \
  --db health_connect_export.db \
  --source libra=libra-export.csv \
  --out full
```

Full import.  Runs all pre-import invariants automatically — cutoffs,
zone-offset range, local_date formula, dedupe_hash uniqueness, FK targets,
`PRAGMA integrity_check`.  **If any invariant fails, the ZIP is not packed.**

### 5 — Phone Import

Copy the output ZIP to Drive → open Health Connect on the phone → Manage data
→ Backup and restore → Import data → select the ZIP from Drive.  Larger imports
(100k+ rows) take minutes.

If the import fails: `adb logcat -c` → retry → `adb logcat -d | grep -A15 "Import failed"`.
Common causes:

| Exception | Cause | Fix |
|---|---|---|
| `Zone offset...` | offsets written in milliseconds | bug in builder — must fix |
| `UNIQUE constraint...dedupe_hash` | duplicate source timestamps | dedup rule gap |
| `IllegalStateException...version` | `user_version` mismatch | wrong base db |
| Generic parse error | NULL in NOT NULL column | inspect domain rows |

### 6 — Diff

```bash
ghcdb diff pre-import-export.db post-import-export.db
```

Uuid-keyed roundtrip validation against the **next scheduled export** (there is
no on-demand export; post-import diff always runs against the next scheduled
one).  Reports `PASS`, `PASS_WITH_EXPECTED_DEVIATIONS`, or `FAIL`.

Use `--expected-deletions weight=3` to declare known deliberate deletions.
Use `--no-allow-growth` to flag any new records as unexpected.

---

## Safety

- **Personal data stays local.**  The tool runs entirely offline.  No network
  calls, no telemetry, no third-party services.

- **Source database is read-only.**  All operations work on a copy.  The
  original export is never modified.

- **Pilot first.**  The workflow is ordered `inspect → plan → pilot → build`.
  Skipping the pilot means you discover problems after a full import instead of
  before.

- **Backfills are indistinguishable from app data.**  Imported records carry the
  source app attribution and merge with existing data — they look identical to
  HC.

- **Deletion is day-by-day.**  HC has no bulk delete.  Removing imported history
  means deleting it one day at a time in the HC UI (or wiping a data type
  entirely).

- **Keep the pre-import export.**  It is the only rollback.  Always export
  fresh before running `ghcdb build`.

---

## Requirements

- Android 14+ with Health Connect installed
- A Health Connect export (Settings → Manage data → Backup and restore)
- Python 3.10 or later
- **No third-party dependencies.**  The package uses only the Python standard
  library (`sqlite3`, `zoneinfo`, `uuid`, `argparse`, `csv`, `json`, `zipfile`,
  `pathlib`).

---

## Install

### From PyPI (when published)

```bash
pip install ghc-db-manager
ghcdb --help
```

### From source

```bash
git clone https://github.com/ghc-db-manager/ghc-db-manager.git
cd ghc-db-manager
pip install .
ghcdb --help
```

The `ghcdb` command is installed as a console script.  Verify:

```bash
ghcdb inspect --help
```

---

## Documentation

| Document | What it covers |
|---|---|
| [`docs/hc-internals.md`](docs/hc-internals.md) | HC schema, units, offset rules, dedupe_hash formula, record-type IDs, failure catalog. Evidence-tagged: `[OBSERVED]` / `[INFERRED]` / `[DOCUMENTED]` |
| [`docs/source-integration-guide.md`](docs/source-integration-guide.md) | How to add a new tracker: Track 1 (prepare data with generic_csv mapping), Track 2 (write a source adapter), mandatory checklist |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | logcat signature → cause → fix catalog; non-ADB fallback paths |
| [`docs/case-study-poc.md`](docs/case-study-poc.md) | The PoC story: two import waves, aggregate numbers only, no personal data |

---

## Status

**Experimental.**  Verified against Health Connect `user_version = 23`.
Schema changes in future HC releases may require version updates to
`knowledge.py` and the fixture builder.

The PoC (2026-08-22/23) imported 11,600+ records across two waves and
validated byte-exact on a real phone.  The `ghc-db-manager` package is the
open-source reimplementation of that PoC with a published test suite.

---

## License

MIT.  See [`LICENSE`](LICENSE).

**Absolutely no warranty.**  This software is provided "as-is".  See the
full MIT licence terms at [`LICENSE`](LICENSE).  In particular: this tool
edits personal health databases.  Incorrect use can result in permanent data
loss.  Always work on a copy, always pilot before full import, and keep the
pre-import export as a rollback.
