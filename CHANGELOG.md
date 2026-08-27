# Changelog

All notable changes to ghc-db-manager are documented here.
Format follows Keep a Changelog; versioning is semantic.

## [0.1.2] — 2026-08-27

### Fixed
Real-data audit against the original PoC found 20 defects that synthetic fixtures
had masked. Highlights:
- Zepp file dispatch matched exact stems; real exports suffix filenames with timestamps (BODY_1787404253776.csv) — ALL Zepp data was silently skipped. Now prefix-based dispatch (SLEEP_MINUTE before SLEEP, etc.).
- Default attribution was a fixture rowid (5) that resolved to Google Fit on real databases. Now per-domain package-name defaults resolved per-db.
- Cutoff semantics: activity and HR eligibility now END-based (whole interval before cutoff — a start-based check admitted days overlapping live data).
- Sleep stage segmentation: merged same-stage minutes across gaps and fragmented on duplicate minutes (real data: 207 duplicated minutes in re-sync blocks produced 141-segment garbage nights). Now deduped (keep-first) with strict +1-minute continuity.
- Empty HC table now means "import all history" (cutoff None) instead of silently dropping the whole domain (cutoff 0).
- R6 same-measurement rule evaluated only the first pair per day (break); R3 richness tiebreak was dead for Zepp rows; R1 leaked null-height rows.
- `--tz` was accepted and ignored — now threaded end-to-end.
- Zepp SLEEP 'naps' column (embedded JSON) crashed raw-field capture; inverted sleep sessions (stop < start, export defect) are now skipped with a stat instead of imported or crashing.
- Writer insert counts now reflect actual inserts (INSERT OR IGNORE aware); unknown Zepp sport types warn instead of vanishing silently.
- activity_date coverage invariants scoped to rows inserted by the current build (real dbs contain ~22k pre-existing native rows without coverage — full-table scans false-failed real builds); one pinned last_modified_time per build.
- Ported PoC sanity gates for weight (person-filter warning, band recheck, uniqueness, min-ts plausibility) and the post-insert cutoff invariant.

Verified by rebuilding both PoC waves on the real personal data: wave 1 exact
parity (750/750 canonical rows, 0 value differences), wave 2 exact on all
insert counts (2480×3 activity, 2214 sleep + 7391 stages, 457+169 HR with
288,428 samples, 602 exercise) with two documented deviations where the tool
is stricter than the PoC (12 inverted sessions skipped; stages deduped and
clipped to session bounds). 264 tests.

## [0.1.1] — 2026-08-26

### Fixed
- Sleep `local_date` invariant checked the wrong basis (end-based) while Health Connect's canonical recomputed form is start-based — `ghcdb validate`/`diff` false-FAILED on real post-import exports. Fixed the invariant, the Zepp sleep adapter (root cause: it computed end-based local_date), and the fixture database seeding (which had masked the bug by encoding the writer's shape instead of the canonical export shape). Added a direction-pinning regression test: canonical start-based sleep passes, end-based fails.
- FK invariant coverage extended from weight tables only to all record tables in the knowledge registry (missing app/device references now caught for every domain).

### Changed
- Writer-level `value <= 0` rejection removed from the instant-record path; value plausibility is enforced by domain layers (weight band, generic-CSV bounds) where it belongs. No behavior change for valid inputs.

### Documentation
- Corrected offline/local positioning: the tool processes everything locally, but the documented workflow transits cloud storage — Health Connect's scheduled export writes only to a cloud provider app, and the import picker (SAF) accepts any document source (Drive-verified; local providers untested). Added a Data flow section with diagram.

## [0.1.0] — 2026-08-23

### Added
- Initial release. Python CLI (stdlib-only) that backfills historical health data (Libra, Zepp, generic CSV) into an Android Health Connect export database for re-import via Health Connect's built-in restore.
- Workflow: `inspect → plan → pilot → build → validate → diff`, with deterministic UUIDs (pilot is a strict subset of the full build; re-imports idempotent), pre-import invariants as a hard build gate, and a post-import diff with an expected-deviation model.
- Synthetic-only test fixtures (privacy-safe, published) including poison cases; 249 tests. Verified against Health Connect user_version 23 on a real phone.
- MIT license — absolutely no warranty.
