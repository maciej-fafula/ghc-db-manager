# Changelog

All notable changes to ghc-db-manager are documented here.
Format follows Keep a Changelog; versioning is semantic.

## [0.1.1] — 2026-08-26

### Fixed
- Sleep `local_date` invariant checked the wrong basis (end-based) while Health Connect's
  canonical recomputed form is start-based — `ghcdb validate`/`diff` false-FAILED on real
  post-import exports. Fixed the invariant, the Zepp sleep adapter (root cause: it computed
  end-based local_date), and the fixture database seeding (which had masked the bug by
  encoding the writer's shape instead of the canonical export shape). Added a
  direction-pinning regression test: canonical start-based sleep passes, end-based fails.
- FK invariant coverage extended from weight tables only to all record tables in the
  knowledge registry (missing app/device references now caught for every domain).

### Changed
- Writer-level `value <= 0` rejection removed from the instant-record path; value
  plausibility is enforced by domain layers (weight band, generic-CSV bounds) where it
  belongs. No behavior change for valid inputs.

### Documentation
- Corrected offline/local positioning: the tool processes everything locally, but the
  documented workflow transits cloud storage — Health Connect's scheduled export writes
  only to a cloud provider app, and the import picker (SAF) accepts any document source
  (Drive-verified; local providers untested). Added a Data flow section with diagram.

## [0.1.0] — 2026-08-23

### Added
- Initial release. Python CLI (stdlib-only) that backfills historical health data
  (Libra, Zepp, generic CSV) into an Android Health Connect export database for
  re-import via Health Connect's built-in restore.
- Workflow: `inspect → plan → pilot → build → validate → diff`, with deterministic
  UUIDs (pilot is a strict subset of the full build; re-imports idempotent),
  pre-import invariants as a hard build gate, and a post-import diff with an
  expected-deviation model.
- Synthetic-only test fixtures (privacy-safe, published) including poison cases;
  249 tests. Verified against Health Connect user_version 23 on a real phone.
- MIT license — absolutely no warranty.
