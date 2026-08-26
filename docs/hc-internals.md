# Health Connect Internals — Evidence Trail

> **Scope:** This document is the persistent, citable knowledge base for HC export/import internals, field semantics, and failure modes. Every claim is tagged with its evidence source. Everything is verified against HC user_version=23 (~2026-08). Drift from newer HC versions is a known risk — always validate against a fresh export before large imports.

---

## 1. Export / Import Mechanics

### 1.1 Export Structure [OBSERVED]

HC export is a **ZIP file** containing exactly one entry:

```
Health Connect.zip
└── health_connect_export.db   ← plain SQLite, unencrypted, no HMAC
```

No encryption, no signature, no manifest. The file is a raw SQLite 3 database. Evidence:
- The ZIP was opened and the entry extracted directly; `file(1)` reports `SQLite 3.x database`.
- A modified export (with inserted rows) was successfully imported on the phone — proving no integrity hash locks the content.
- AOSP research (`librarian`): `BackupExport.java` writes `Health Connect.zip` using `ZipOutputStream` with a single entry named `health_connect_export.db`.

### 1.2 Import Mechanism [OBSERVED + INFERRED]

Import is triggered by the user at **Settings → Health Connect → Manage data → Backup and restore → Import data**. This opens a **Storage Access Framework (SAF)** file picker. The default filename offered is `Health Connect.zip`.

The user picks any `.zip` file; no cloud backup provider is required (the picker just presents local/cloud files). Evidence: a manually placed zip on Google Drive was imported successfully.

There is **no on-demand export API** for the user — exports are **scheduled only** (daily/weekly/monthly in HC settings). This means validation of a completed import must wait for the *next* scheduled export and cannot be done immediately.

### 1.3 Merge Semantics — Proven by Marker Test [OBSERVED]

The critical question: does import **replace** existing HC data, **merge** with it, or **overwrite** newer data?

**Evidence trail:**
1. A fresh export was taken at T0.
2. A manual weight entry (100.0 kg) was added to HC *after* T0.
3. The unmodified T0 export was re-imported.
4. The post-T0 manual entry **survived** the import.

**Conclusion:** HC import **merges**. It does not replace and does not delete data newer than the export. uuid and dedupe_hash duplicates are conflict-ignored via `insertOrIgnoreOnConflict` (AOSP `UpsertTransactionRequest.createForRestore`).

> **Implication:** Fresh export before final import is hygiene, not a hard requirement. It is still recommended for clean validation.

### 1.4 Scheduled-Only Export [OBSERVED]

HC does not expose an on-demand export button. The export is purely scheduled. Evidence: the feature exists only in Settings → Health Connect → Manage data → Backup and restore, as a passive "your backup" display tied to the scheduled interval. No manual trigger in the UI.

---

## 2. Import Pipeline (AOSP)

The following pipeline is read from AOSP source (`librarian` module, `BackupRestore*.java`). It describes how HC processes an imported ZIP.

### Step 1 — ZIP extraction
The ZIP is opened; the single entry `health_connect_export.db` is extracted to a temp file.

### Step 2 — Schema compatibility check (`canMerge`)
```java
// From AOSP: BackupRestore.java
if (user_version > device_user_version) {
    throw new IllegalStateException("Import user_version exceeds device version");
}
```
Only `user_version` is checked. No checksum, no HMAC, no manifest.

### Step 3 — `DatabaseMerger.merge()`
Per-record-type loop. For each record type (weight, steps, sleep, etc.):
1. Read all rows from the imported file using a standard `SELECT * FROM <table> JOIN application_info_table ... WHERE ...` query.
2. For each row, build an `UpsertTransactionRequest.createForRestore()`.
3. Call `insertOrIgnoreOnConflict` — silently skips rows whose `uuid` or `dedupe_hash` already exists on device.

### Step 4 — Post-merge reconciliation
After all record types are merged:
- `record_types_used` is updated (reconciled) to reflect all record types now present.
- Priority table is updated.
- `change_logs` entries are written for audit.

### Step 5 — `last_modified_time` overwritten
Every merged row's `last_modified_time` is set to `System.currentTimeMillis()` at import time (the "now" of the import). This is a known HC behavior — the original export's timestamps are not preserved as modification times.

---

## 3. Validation: What IS and IS NOT Checked

### 3.1 What IS checked [OBSERVED]

| Check | Mechanism | Failure mode |
|---|---|---|
| `user_version ≤ device_schema_version` | `canMerge()` comparison | `IllegalStateException: ...version` → entire import rejected |
| Row-level parse success | Per-type read; any exception aborts the entire import | Generic parse error at the offending record type |

### 3.2 What IS NOT checked [OBSERVED]

- **No checksum / HMAC.** A fully modified export (with inserted rows) imported successfully. No integrity token is verified.
- **No record_types_used correctness.** Even if `record_types_used` is stale or missing entries for new types, HC reconciles it post-merge. No need to edit this table manually.
- **No priority table correctness.** Similarly reconciled post-merge.
- **No per-field range validation** (beyond SQLite type constraints). Out-of-range values in non-key columns are accepted if they parse.

> **Evidence for no checksum:** A wave-1 import inserted 750 rows into a modified export; the modified export imported successfully. If any HMAC were verified, this would have failed.

---

## 4. Schema Field Reference

> **Legend:**
> - `[OBSERVED]` — seen directly in a real HC export database or behavior
> - `[INFERRED]` — read from AOSP source; not directly confirmed against an export
> - `[DOCUMENTED]` — from official Android/HC documentation

### 4.1 Core Identifiers

| Field | Type | Description | Evidence |
|---|---|---|---|
| `uuid` | `BLOB` (16 bytes) | Record UUID. 16-byte binary blob. HC stores native UUIDs but exports them as binary BLOB — not hex strings. | [OBSERVED] `SELECT hex(uuid) FROM weight_record_table` returns 32 hex chars per row |
| `client_record_id` | `TEXT` or `NULL` | Idempotency key set by the originating app. When set, `dedupe_hash` is `NULL`. | [OBSERVED] Some rows have this set; those rows have `dedupe_hash = NULL` |
| `client_record_version` | `TEXT` | Version string for `client_record_id`-based deduplication. | [OBSERVED] All imported rows used `'0'` |
| `device_info_id` | `INTEGER` | FK to `device_info_table`. Value `1` is the generic "unknown device" entry present in all exports. | [OBSERVED] Google Fit rows and pilot-proven rows use `device_info_id = 1` |

### 4.2 Deduplication Hash

| Field | Type | Description | Evidence |
|---|---|---|---|
| `dedupe_hash` | `BLOB` (24 or 32 bytes) | Big-endian concatenation of `appInfoId(8B) + deviceInfoId(8B) + time(8B) [+ end_time(8B)]`. 24B for instant records, 32B for interval records. `NULL` when `client_record_id` is set. | [OBSERVED] Verified byte-exact against a real Google Fit row (weight, instant): BE concat of `application_info_table.rowid=2` (8B) + `device_info_table.rowid=1` (8B) + `time` (8B) matches `dedupe_hash` column exactly |

**Formula:**
- Instant (weight, heart rate instant): `appInfoId(8BBE) || deviceInfoId(8BBE) || time(8BBE)` → 24 bytes
- Interval (steps, sleep, exercise): `appInfoId(8BBE) || deviceInfoId(8BBE) || start_time(8BBE) || end_time(8BBE)` → 32 bytes

### 4.3 Time Fields

| Field | Type | Description | Evidence |
|---|---|---|---|
| `time` | `INTEGER` | Epoch milliseconds (UTC), instant records. | [OBSERVED] `1704067200000` (2024-01-01 00:00:00 UTC) stored as integer ms |
| `start_time` | `INTEGER` | Epoch milliseconds (UTC), interval record start. | [OBSERVED] |
| `end_time` | `INTEGER` | Epoch milliseconds (UTC), interval record end. Must be > `start_time`. | [OBSERVED] |
| `zone_offset` | `INTEGER` | **Zone offset in SECONDS**, not milliseconds. Valid range: ±64800. `+7200` = CEST, `+3600` = CET. | [OBSERVED] [CRASH-PROVEN] Writing milliseconds (e.g. `7200000`) causes `DateTimeException: Zone offset not in valid range: -18:00 to +18:00` and rejects the entire import. The working value is `7200` (seconds). |

> ⚠️ **[CRASH POST-MORTEM]**
>
> **What happened:** The first pilot import was rejected. Logcat showed:
> ```
> java.time.DateTimeException: Zone offset not in valid range: -18:00 to +18:00
> Import failed during database merge...
> ```
> **Root cause:** Zone offsets were written as **milliseconds** (`7200000`) instead of **seconds** (`7200`).
>
> **Diagnosis (bisect):**
> - Pilot 0: record-free modification of the export → **passed** (no rows = no offset error)
> - Pilot 2: rows with millisecond offsets → **rejected**
> - Conclusion: rejection is content-level, not file-level. The offset unit was the bug.
>
> **Fix:** Convert all zone offsets from ms → s before INSERT. The import pipeline uses `ZoneOffset.ofTotalSeconds(int seconds)` internally.
>
> **The same crash recurs if offsets are written in ms.** Always verify offset unit before packing.

### 4.4 Date Fields

| Field | Type | Description | Evidence |
|---|---|---|---|
| `local_date` | `INTEGER` | Epoch days of the record's local date: `(time + 1000·zone_offset_s) / 86400000` (integer division). For interval records, HC's import pipeline **recomputes this from the LOCAL START instant** and silently overwrites whatever was written. | [OBSERVED] During wave-2 validation, 462 sleep session `local_date` values were changed post-import from end-based to start-based (HC's canonical formula). HC recomputed silently; no data loss. All other fields were byte-identical. |
| `local_date_time` | `INTEGER` (generated) | Generated column — **never write to it**.HC computes it from `local_date` + `start_time`. | [OBSERVED] `PRAGMA table_info` marks it as generated. Writes to it are ignored or rejected. |

**`local_date` formula (canonical, used by HC on import for interval records):**
```
local_date = floor((start_time_instant_utc_ms + 1000 × zone_offset_seconds) / 86400000)
```
Where `start_time_instant_utc_ms` is the UTC epoch ms of the local day's start (i.e., 00:00:00 local time converted to UTC).

**`local_date` recompute behavior:** For **interval records** (steps, distance, calories, exercise, sleep sessions), HC recomputes `local_date` from the local START instant on import. This means:
- If you write end-based `local_date` for sleep, HC will silently rewrite 462 of them to start-based.
- This is **benign** — the final state is self-consistent with HC's convention.
- Instant records (weight, body fat, heart rate) are **not** recomputed.

### 4.5 Units

| Domain | DB Unit | Multiplier from source | Notes |
|---|---|---|---|
| Weight (`weight_record_table`) | **grams** (REAL) | kg × 1000 | e.g. 98.7 kg → `98700.0` |
| Lean body mass (`lean_body_mass_record_table`) | **grams** (REAL) | kg × 1000 | same as weight |
| Body fat (`body_fat_record_table`) | **percent** (REAL) | no conversion | e.g. `28.9` (not 0.289) |
| Energy / calories (`total_calories_record_table`) | **calories × 1000** (REAL) | kcal × 1000 | e.g. 412.0 kcal → `412000.0`; existing rows show this scale |
| Distance (`distance_record_table`) | **meters** (REAL) | no conversion (Zepp m = HC m) | e.g. `5401.0` |
| Steps (`steps_record_table`) | count (INTEGER) | none | integer |
| Heart rate (`heart_rate_record_table`) | BPM (INTEGER) | none | integer |
| Sleep stages (`sleep_stages_table`) | stage_id (INTEGER) | see §5 | enum |
| Exercise (`exercise_session_record_table`) | seconds (INTEGER) | Zepp sportTime already in s | interval |

All timestamps are epoch **milliseconds** (INTEGER).

### 4.6 Attribution

| Field | Description | Evidence |
|---|---|---|
| `app_info_id` | FK to `application_info_table`. The row must exist in the export or on the phone. | [OBSERVED] `app_info_id = 6` → `com.huami.watch.hmwatchmanager`; `app_info_id = 7` → `net.cachapa.libra` |
| `recording_method` | Enum integer. See §5.3. | [DOCUMENTED] + [OBSERVED] |

### 4.7 Activity Date Table

| Field | Description | Evidence |
|---|---|---|
| `epoch_days` | Integer epoch day | [OBSERVED] Same formula as `local_date` |
| `record_type_id` | RecordType enum integer | [OBSERVED] |
| `(epoch_days, record_type_id)` | UNIQUE pair | Must be populated with `INSERT OR IGNORE` for every inserted record's local date |

**Why it matters:** If a date/type pair is missing from `activity_date_table`, records for that day may not appear in HC's daily aggregation views.

---

## 5. Enumerations

### 5.1 Record Type IDs (`activity_date_table`, `record_types_used`)

| ID | Type name | Table | Evidence |
|---|---|---|---|
| 1 | `STEPS` | `steps_record_table` | [OBSERVED] |
| 11 | `HEART_RATE` | `heart_rate_record_table` | [OBSERVED] |
| 17 | `BODY_FAT` | `body_fat_record_table` | [OBSERVED] |
| 26 | `WEIGHT` | `weight_record_table` | [OBSERVED] |
| 27 | `LEAN_BODY_MASS` | `lean_body_mass_record_table` | [OBSERVED] |
| 37 | `EXERCISE` | `exercise_session_record_table` | [OBSERVED] |
| 38 | `SLEEP` | `sleep_session_record_table` | [OBSERVED] |

Note: ID 16 (`BasalMetabolicRate`) is written daily by Zepp — not a weight type.

### 5.2 Sleep Stage IDs (`sleep_stages_table.stage_id`)

| ID | Stage | Evidence |
|---|---|---|
| 1 | `AWAKE` | [OBSERVED] |
| 4 | `LIGHT` | [OBSERVED] |
| 5 | `DEEP` | [OBSERVED] |
| 6 | `REM` | [OBSERVED] |

### 5.3 Recording Method Enum (`recording_method`)

| Value | Name | Usage | Evidence |
|---|---|---|---|
| 0 | `UNKNOWN` | Fallback | [DOCUMENTED] |
| 1 | `ACTIVELY_RECORDED` | Workouts / manual measurements | [DOCUMENTED] |
| 2 | `AUTOMATICALLY_RECORDED` | Watch auto-tracked (steps, sleep, auto-HR) | [DOCUMENTED] |
| 3 | `MANUAL_ENTRY` | Manual entries (Libra weight, manual HR) | [DOCUMENTED] + [OBSERVED] |

---

## 6. Failure Catalog

| Logcat signature | Likely cause | Fix |
|---|---|---|
| `java.time.DateTimeException: Zone offset not in valid range: -18:00 to +18:00` | `zone_offset` written in **milliseconds** instead of **seconds** | Convert all offsets to seconds (divide by 1000) |
| `UNIQUE constraint failed: <table>.dedupe_hash` | Duplicate source timestamps not filtered; dedupe_hash collision | Apply dedup rules (exact-ts collision, same-measurement pair) before build |
| `IllegalStateException ... version` | `user_version` of imported db > device schema version | Use a newer base export or do not bump user_version |
| `SQLite constraintException: <column> NOT NULL` | NULL value in a NOT NULL column for that record type | Inspect domain rows for NULLs in required columns; check units |
| `BackupRestore...Import failed during database merge... Details:` | Generic parse error at a specific record type | Run `adb logcat -c` → retry import → `adb logcat -d | grep -A15 "Import failed"` to get the full stack trace and offending line |

**Diagnostic command:**
```bash
adb logcat -c          # clear logcat
# perform import on phone
adb logcat -d | grep -A15 "Import failed"
```

---

## 7. Version Assumptions

All entries in this document are verified against:

| Parameter | Value |
|---|---|
| HC export `user_version` | 23 |
| HC app version | ~2026-08 |
| Export schema | Plain SQLite 3, no encryption |
| Validation date | 2026-08-22/23 |

**Drift risks:**
- Newer HC versions may bump `user_version`. The `user_version ≤ device` check is the gate.
- New record types or columns could be added. Always validate row shapes against a fresh export of the target phone before a large import.
- `local_date` recompute behavior was observed for sleep sessions; other interval record types may also be recomputed. Always validate `local_date` against the next scheduled export.

**Mitigation:** Pilot-first methodology. A small subset (1 record per domain) imported first, verified in the HC UI, catches version-sensitive issues before a large import wastes an hour.

---

## 8. Version History

| Date | Version | Change |
|---|---|---|
| 2026-08-23 | 0.1.0 | Initial evidence trail — all entries tagged [OBSERVED]/[INFERRED]/[DOCUMENTED]. `local_date` recompute behavior documented after wave-2 validation finding. |
