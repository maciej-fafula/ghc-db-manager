# Troubleshooting Guide

This catalog maps Health Connect import failures to their causes and fixes,
based on evidence from the PoC and known HC import failure modes.

---

## Import Rejection — Crash-Level Errors

These errors cause the entire import to be rejected. HC shows a generic
"Import failed" dialog. Capture diagnostics with ADB:

```bash
# Clear logcat buffer, run import, dump relevant logcat
adb logcat -c
# [import file via HC Settings → Manage data → Backup and restore]
adb logcat -d | grep -A20 "Import failed"
```

---

### `DateTimeException: Zone offset not in valid range: -18:00 to +18:00`

**Cause**: `zone_offset` written in **milliseconds** instead of **seconds**.
HC's `ZoneOffset.ofSeconds()` rejects values larger than ±64800.

**Fix**: Use seconds throughout. The `ghc-db-manager` tool always uses
seconds; this should never happen with official builds. If it does:

1. File a bug report with the database that triggered it.
2. As a workaround, inspect the failing table with sqlite3 and verify
   that `zone_offset` values are in the range ±64800 (not ±64800000).
3. If a manual database edit is required, correct the offsets:

```sql
-- Example: fix zone_offset in weight_record_table (seconds, not ms)
UPDATE weight_record_table
SET zone_offset = zone_offset / 1000
WHERE zone_offset > 64800 OR zone_offset < -64800;
```

**Reference**: `knowledge.ZONE_OFFSET_MAX_SECONDS = 64800`.

---

### `UNIQUE constraint failed: <table>.dedupe_hash`

**Cause**: Duplicate source timestamps from the same app+device producing the
same dedupe hash. HC rejects the entire import on first collision.

**Fix**:

1. Re-run the merge plan with dedup stats enabled.
2. Check the dedup statistics in the merge output — identical timestamps
   from the same source usually indicate a data quality issue.
3. If re-exporting is possible, verify that the source CSV has no duplicate
   timestamp rows for the same device.
4. If the source data genuinely contains same-timestamp measurements
   (e.g. multiple manual entries per minute), use a dedup strategy:
   richer-row-wins or latest-wins per the merge spec.

**Reference**: `knowledge.dedupe_hash_instant()` and
`knowledge.dedupe_hash_interval()`.

---

### `IllegalStateException ... user_version ...`

**Cause**: The `user_version` in the database file is **newer** than what
HC on the phone understands. HC aborts the import.

**Fix**:

1. Re-export from the phone using a **fresh** export (not an old one).
2. Use the latest HC export as the base for import, never an old snapshot.
3. If you have a manually edited db with a mismatched version:

```bash
# Check user_version
sqlite3 your_db.db "PRAGMA user_version;"
# The known-safe value for current HC schema is 23
```

**Reference**: `knowledge.KNOWN_USER_VERSION = 23`.

---

### Per-Record-Type Parse Error (e.g., `NULL` value in non-nullable column)

**Cause**: A row in the source data has a `NULL` or out-of-range value in a
column HC expects to be non-null (e.g., `weight` column, time columns).

**Fix**:

1. Run `ghcdb validate` on the modified database before importing:

```bash
ghcdb validate modified.db
```

2. If validation fails, inspect the failing rows:

```sql
SELECT * FROM <table> WHERE weight IS NULL;
SELECT * FROM <table> WHERE time IS NULL;
```

3. Correct the NULLs or filter out the bad source records and rebuild.

4. For out-of-range `zone_offset` or impossible timestamps, same approach:
   identify and fix the source data, then rebuild.

---

## Import Accepted But Records Not Visible in HC UI

The import completes without error but data does not appear in Health
Connect or reading apps.

### `activity_date` Coverage Gap

**Cause**: `local_date` values exist in record tables but no corresponding
entries in `activity_date_table`. HC uses `activity_date_table` for daily
chart rendering; records without coverage are invisible.

**Fix**:

```bash
# Run validate to detect this
ghcdb validate modified.db
# Fix: ensure activity_date_table is populated
# The ghc-db-manager writer does this automatically with INSERT OR IGNORE.
# For manual fixes:
-- For each (local_date, record_type_id) in the record table:
INSERT OR IGNORE INTO activity_date_table (epoch_days, record_type_id)
  SELECT DISTINCT local_date, <record_type_id> FROM <record_table>;
```

---

### Daily Charts Showing Shifted Date

**Cause**: `zone_offset` assignment for date-only source entries (e.g., Libra
weight entries stored as local midnight) — the local date may differ by one
day depending on the time zone.

**Fix**:

1. Verify that `local_date` for the affected records matches the expected
   local date (not the UTC date).
2. Check the zone offset used during import — it must match the user's
   time zone at the time of the measurement.
3. For Libra exports: midnight entries are stored as 22:00Z/23:00Z in the
   CSV; the zone rule applies +2h (summer) or +1h (winter) to convert to
   local midnight. Verify the merge plan uses the correct timezone.

---

## No-ADB Path (Users Without ADB Access)

Users without a computer cannot capture logcat diagnostics. Troubleshooting
steps are degraded but some signals are available.

### HC Import Notification Text

HC shows a brief success/failure toast notification after each import
attempt:

- **"Import successful"** — no crash-level error; data should be present.
  If records are missing, check `activity_date` coverage (above).
- **"Import failed"** — a parse exception occurred. Try:
  1. Re-export the base database from HC (fresh export, not old one).
  2. Re-run the plan from scratch.
  3. If it fails again, try with a smaller pilot subset to isolate
     the failing record.

### Retry with Pilot Subset

If a full import fails without ADB diagnostics:

1. Build a **pilot** import (one record per domain) and import it first:

```bash
ghcdb pilot --db fresh_export.db --source libra=libra.csv --out pilot
# Import pilot.zip first, verify in UI
# Then build full:
ghcdb build --db fresh_export.db --source libra=libra.csv --out full
# Import full.zip
```

2. If the pilot succeeds but full fails, the error is in a non-pilot record.
3. Binary-search by splitting the source data into halves until the
   failing record is isolated.

---

## ADB Capture Commands Reference

```bash
# Full import diagnostics
adb logcat -c
# Import file now
adb logcat -d > logcat.txt
grep -A30 "Import failed" logcat.txt

# Check database integrity
adb shell "run-as com.android.healthconnect cat /data/data/com.android.healthconnect/databases/health_connect_export.db" > export.db
# Then inspect locally
sqlite3 export.db "PRAGMA integrity_check;"

# Check specific table for bad offsets
sqlite3 export.db "SELECT COUNT(*) FROM weight_record_table WHERE zone_offset > 64800 OR zone_offset < -64800;"

# activity_date coverage check
sqlite3 export.db "
SELECT 'Missing activity_date for:',
       COUNT(DISTINCT w.local_date) as missing_dates
FROM weight_record_table w
WHERE NOT EXISTS (SELECT 1 FROM activity_date_table a
                  WHERE a.epoch_days = w.local_date
                    AND a.record_type_id = 26);
"
```
