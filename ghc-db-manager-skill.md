---
name: ghc-db-manager
description: Merge historical health data (Libra, Zepp, generic CSV) into an Android Health Connect export database, then re-import it on the phone via Health Connect's built-in restore. Use when the user wants to backfill Health Connect with history from tracker-app exports, repair/inspect an HC export database, or validate a previous import. Covers the full workflow: inspect coverage → plan/dedup → pilot → build → phone import → post-import diff.
---

# ghc-db-manager — skill for language models

You are operating a tool that edits **Health Connect export databases** (SQLite) so they can be re-imported on an Android phone via HC's built-in restore. This works because HC's import does no integrity checking beyond a schema-version comparison — but it WILL reject the entire import on any per-row parse error, so every rule below is load-bearing. Health data is personal and hard to un-delete: follow the workflow exactly, never skip the pilot.

## Absolute rules

1. **Never modify the user's only copy of an export.** Always work on a copy; the source db is opened read-only.
2. **Never insert records at/after a domain cutoff.** Cutoffs = `MIN(start_time)` (or `MIN(time)` for instant tables) per table in the user's freshest export. History below the cutoff is missing; anything at/after it is already live on the phone and will duplicate.
3. **Never write these tables:** `android_metadata`, `change_logs*`, `access_logs*`, `backup_change_token_table`, `migration*`, `pre_migration*`. Also never write generated columns (`local_date_time`).
4. **Pilot before full import. Always.** A 1-record-per-domain pilot (subset of the full set — see UUID rule) imported on the phone, verified in the UI against source values. A rejected full import wastes an hour; a rejected pilot costs a minute.
5. **No on-demand export exists.** HC exports are scheduled (daily/weekly/monthly) or taken by the user in Settings. Post-import validation therefore diffs the *next* scheduled export.

## Health Connect internals you must know (all empirically verified)

- **Export** = ZIP with exactly one entry named `health_connect_export.db` (plain SQLite). **Import** = Settings → Health Connect (search) → Manage data → Backup and restore → Import data; a SAF file picker, default name `Health Connect.zip`. Import **merges** (never replaces; newer on-phone data survives; uuid/dedupe_hash duplicates are conflict-ignored).
- **`zone_offset` columns are SECONDS** (valid ±64800). Writing milliseconds causes `java.time.DateTimeException: Zone offset not in valid range: -18:00 to +18:00` and rejects the WHOLE import. This is the #1 failure.
- **Units:** weight & lean body mass in **grams** (REAL), energy in **calories ×1000**, distance in **meters**, body fat in **percent**, timestamps in **epoch milliseconds**.
- **`local_date`** = epoch days of local time: `(time + 1000·zone_offset_seconds) / 86400000`. For interval records HC's import **recomputes it from the LOCAL START instant** and silently overwrites whatever you wrote — so compute it start-based yourself and don't be surprised by post-import diffs on it.
- **`dedupe_hash`** = big-endian concat: instant records `appInfoId(8B)+deviceInfoId(8B)+time(8B)` (24B); interval records `+end_time(8B)` (32B). NULL when `client_record_id` is set. Proven row shape: `client_record_id=NULL`, `client_record_version='0'`, `device_info_id=1` (the empty "unknown device" row), `recording_method` 0=unknown/1=actively recorded/2=automatic/3=manual entry.
- **`uuid`** = 16-byte BLOB. Use **deterministic UUIDv5** (`NAMESPACE_URL`, key like `<project>/<domain>/<start_ms>[-<end_ms>]`) so pilot ⊆ full set and re-imports are idempotent (conflict-ignored).
- **`app_info_id`** must reference a package row that exists in the export's `application_info_table` (or is installed on the phone). Attribute to the app that actually produced the data.
- **`activity_date_table`** (`epoch_days`, `record_type_id`, UNIQUE pair): populate with `INSERT OR IGNORE` for every inserted record's local date, or records may be invisible in daily aggregations. Record type ids: steps=1, HR=11, body_fat=17, weight=26, lean=27, exercise=37, sleep=38. Sleep stages: AWAKE=1, LIGHT=4, DEEP=5, REM=6.
- **Series tables** (`heart_rate_record_series_table`, `sleep_stages_table`) reference the parent record's `row_id` (`parent_key`) — insert parent first, use `cursor.lastrowid`.
- **`user_version`** of the edited db must stay ≤ the phone's HC schema version (don't touch it).
- **Zepp sport type → HC ExerciseType:** 1→56 running, 6→74 pool swimming, 13→56 cross-country, 16→0 other, 22→64 soccer, 24→0 other, 49→36 HIIT, 52→70 strength, 76→16 dancing, 140→46 paddling.

## Workflow

### 1. Inspect
```
ghcdb inspect <fresh-export.db>
```
Report per-table coverage (min/max time, counts, per-app split) and computed cutoffs. Ask the user which domains and sources to backfill. If the export is not fresh, request a new one first (scheduled export or Settings).

### 2. Plan (dry run)
```
ghcdb plan --db <fresh-export.db> --source libra=<csv> --source zepp=<dir> [--domains weight,sleep,...]
```
Shows what would be imported per domain, dedup/filter statistics (exact-timestamp collisions, same-measurement pairs, already-in-HC, plausibility filters), and the proposed attribution. Review with the user; attribution and any data filters (e.g. a second person's rows in shared exports) need explicit confirmation.

### 3. Pilot
```
ghcdb pilot   # writes pilot db + "Health Connect pilot.zip"
```
One record per domain (first of each), same deterministic UUIDs as the full set. Instruct the user: upload the zip to Drive → HC → Import data → verify each record in the UI (values, times, attribution, and that daily charts show them). Compare values against the source — statistics like avg/min/max HR prove the series samples arrived.

### 4. Build
```
ghcdb build   # writes canonical CSV/JSON + modified db + "Health Connect full.zip"
```
Runs all invariants automatically (cutoffs, offsets ±64800 s, local_date formula, uuid/dedupe_hash uniqueness, FK targets, `PRAGMA integrity_check`, do-not-touch tables unchanged, activity_date coverage). A failed invariant aborts the build — never pack a db that hasn't passed.

### 5. Phone import (user-side)
Import the full zip via HC → Import data. Larger imports (100k+ series rows) take minutes. If it fails: `adb logcat -c` → retry import → `adb logcat -d | grep -A15 "Import failed"` and read the exception:
- `DateTimeException: Zone offset...` → offsets written in ms (bug, must fix in builder)
- `UNIQUE constraint failed: ...dedupe_hash` → duplicate source timestamps (dedup missing)
- `IllegalStateException ... version` → `user_version` newer than phone (wrong base db)
- Generic parse error at a record type → inspect that domain's rows for NULLs in NOT NULL columns or wrong units

### 6. Validate (next day)
```
ghcdb diff <snapshot.db> <next-scheduled-export.db>
```
Uuid-keyed roundtrip per domain (0 missing / 0 field-diffs expected), counts with expected deviations (live domains grow; deliberate deletions show as negatives; HC may normalize `local_date` on interval records — benign). Report anything unexpected before declaring success.

## Source quirks (known adapters)

- **Libra CSV:** semicolon-separated; `#` comment lines carry the header; date-only entries are stored as local midnight in UTC (22:00Z summer / 23:00Z winter for Europe/Warsaw) — assign zone offsets accordingly or daily charts shift a day.
- **Zepp CSVs:** missing values are the literal string `null`; BODY may contain a second person (filter by height); `muscleRate` is a PERCENT (never map to lean mass); SLEEP has placeholder rows (start==stop → skip); SLEEP_MINUTE times are local with hours ≥20 belonging to the previous date; HEARTRATE files can contain duplicate timestamps.
- **Generic CSV adapter:** requires explicit column mapping + unit declaration; apply the same plausibility/dedup gates.

## Safety notes for the user

- Backfills are attributed to the source app and are indistinguishable from real app data in HC.
- Deleting imported history later means deleting it day-by-day in the HC UI (or wiping a whole data type).
- Keep the pre-import export; it is the only rollback.
