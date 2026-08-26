# ghc-db-manager v0.2.0: honesty fixes + database aggregation

**Date:** 2026-08-26
**Status:** PLANNED — revised after Oracle review (17 findings: 7 MUST-FIX, 10 SHOULD-CONSIDER; all accepted, 3 with refinements — see Progress Log 2026-08-26 11:15)

## Problem
Two gaps in v0.1.0. First, honesty: the README markets the tool as offline/local, but the documented workflow has files transiting cloud storage — HC's scheduled export writes only to a cloud provider app, and the import picker (SAF) is verified with a cloud-storage source. Second, database growth: live HC accumulates high-granularity records (~1,200 one-sample HR records and ~2,800 samples per day; ~140 step intervals per day from multiple devices), reaching 95 MB after one backfill cycle. Users need a safe way to coarsen old data (e.g., steps older than a year → one daily record per app) with **per-app daily sums conserved in the database** (displayed totals in HC reader apps are a separate, empirically-verified concern — see C.3 step 5).

## Current State
- v0.1.0 published (commit a5cf411, 249 tests green): backfill-only tool — inspect/plan/pilot/build/validate/diff; additive workflow proven on a real phone.
- Real-db sizing (2026-08-23 export, 95 MB): steps 24,946; HR 173,238 records + 462,336 samples (the bulk of the bytes); distance 5,533; calories 4,544; sleep stages 12,934; sessions 2,473; exercise 779; weight 549.
- HC import MERGES only (proven); import rejection is atomic (PoC pilot observation). There is no replace semantics → aggregation requires wipe-and-reimport.
- Known v0.1.0 latent bug (found by review, verified in code): `validation/invariants.py` marks sleep local_date as end-based, but HC's canonical recomputed form is start-based (PoC E.6: 462 sessions normalized) — real phone exports would false-FAIL that invariant. Fixture db seeds end-based sleep, masking the bug.
- Writer instant path rejects `value <= 0` (writer.py:64); interval path does not — inconsistent, must be resolved before zero-day policy (B.3).

## Target State
- README/docs/skill corrected with the precise split: **processing is local; transport is not** — scheduled export → cloud provider app (only option), import → SAF picker (any document provider; verified with Drive, local providers untested), documented laptop↔phone workflow transits cloud storage.
- New `ghcdb aggregate` command: per-table, per-app coarsening of records older than a configurable threshold, three HR policies, policy-conditional conservation invariants, dry-run report, manifest emission, and a guided full-wipe+import runbook with an explicit rollback definition (second wipe + import original — never overlay).
- Aggregation-aware `diff` driven by the aggregate manifest.
- v0.2.0 released with tests and fixtures covering aggregation edge cases.

---

## TODO List
<!-- PRIMARY NAVIGATION — always update here first -->

### Phase A: ~~Honesty fixes + v0.1.0 latent bug~~ — EXTRACTED into v0.1.1 (shipped 2026-08-26)
Commit: shipped as `fix:` + `docs:` in v0.1.1 — see `20260826-ghc-db-manager-v0.1.1-plan.md`

- [x] 2026-08-26 11:40 - Baby Step A.1–A.3: DONE in v0.1.1 (README honesty three-part split + Data flow diagram; docs sweep; sleep invariant start-based fix incl. zepp.py root cause; FK coverage all tables; zero-value consistency; regression tests). NOTE: aggregate-specific doc items from original A.1 (seven-command workflow, wipe path in Safety, no-pilot carve-out) remain covered by E.3.

### Phase B: Aggregation engine (core, no CLI yet)
Commit: `feat: aggregation engine with policy-conditional conservation invariants`

- [ ] Baby Step B.1: `aggregation/engine.py` — threshold semantics: cutoff per table = (newest record end_time in that table) minus threshold (`365d`), rounded to local-day boundary; deterministic, wall-clock-independent. **Eligibility unit is the DAY, not the record**: if ANY record in an (app, local_date) group is ineligible (ends at/after cutoff), the whole group passes through untouched — the tool must never manufacture same-app intra-day overlap from threshold straddlers
- [ ] Baby Step B.2: Per-app grouping (user decision): group by (table, app_info_id, local_date); aggregated record attributed to the SAME app; `recording_method` = most common among source records (mixed-method groups get a dry-run warning — provenance dilution is visible, not silent); `device_info_id` = most common source device in the group, fallback DEVICE_UNKNOWN (preserves phone-vs-watch device attribution)
- [ ] Baby Step B.3: Domain aggregators (steps/distance/calories) → one daily interval record per (app, day): start=local midnight, end=next midnight, value = SUM with deterministic summation order (`ORDER BY start_time` — float addition is order-dependent; byte-identical reruns depend on it); zone offsets from tz at those instants; local_date start-based; uuid namespace `<project>/aggregate/<table>/<package_name>/<day>` (**package_name, not rowid** — rowids are unstable across exports/devices); dedupe_hash interval formula with the inherited device id. Zero-sum days: drop the aggregated record, NEVER delete activity_date rows, coverage invariant checks only kept records
- [ ] Baby Step B.4: HR aggregators, three policies (flag `--hr-policy`, default `downsample`); record span = first→last source sample of the day (NOT midnight-midnight — guarantees sample timestamps land inside the span):
  - `merge` (conservative): collapse only 1-sample records into day records (samples preserved verbatim); multi-sample series records pass through
  - `downsample` (default): eligible HR for a day → one day record; samples = per-hour averages, buckets half-open `[HH:00, HH+1:00)` local, averaged sample timestamp = bucket midpoint; empty hours dropped; min/max drift reported as documented loss (NOT gated — averages destroy extremes by design)
  - `stats` (aggressive): one day record with exactly 3 samples — min and max at their original timestamps, average at local noon **clamped into the record span** (night-shift spans)
  - All policies: DELETE child rows in `heart_rate_record_series_table` for every deleted parent BEFORE inserting new samples (the 462k sample rows are the bulk of the bytes; orphans defeat the size goal and ship garbage)
- [ ] Baby Step B.5: Destructive writer mode (new — do NOT bend `write_interval`): single transaction on the copied db; per-record inherited recording_method, per-group source app_info_id and device_info_id; DELETE source rows + child series rows; INSERT aggregates; WriteGuard still applies (record tables are not protected); invariants run before pack
- [ ] Baby Step B.6: Conservation invariants (hard gate, policy-conditional): per (table, app, local_date): SUM(count)/SUM(distance)/SUM(energy) identical before/after; HR: `merge`/`stats` → min/max exact, `downsample` → drift reported; **zero orphaned rows** in heart_rate_record_series_table and sleep_stages_table; new series count == reported sample reduction; uuid/dedupe_hash uniqueness incl. aggregates; activity_date coverage for kept records; integrity_check; intra-app overlap precondition on source (overlapping intervals within (table, app, day) in the INPUT → warn + abort, since sum semantics vs HC display are unverified); VACUUM + size delta report
- [ ] Baby Step B.7: Manifest emission — JSON next to the output db: aggregated tables, policy per table, cutoff per table, per-day expected sums (and HR min/max or sample counts per policy), uuid namespace; consumed by `diff --aggregated` (D) — diff cannot guess cutoffs
- [ ] Baby Step B.8: Re-run safety — engine recognizes its own aggregated rows (uuid recomputable from the namespace): same-parameters rerun is a no-op (idempotent — E.2 test); mixing a previously-aggregated day-record with new raw samples for the same day → refuse with explanation (averaging averages is statistically wrong), suggest full-history re-aggregation from a pre-aggregation export instead

### Phase C: CLI — `ghcdb aggregate`
Commit: `feat: aggregate command with dry-run, manifest, and wipe runbook`

- [ ] Baby Step C.1: `ghcdb aggregate <export.db> --aggregate steps:365d --aggregate heart_rate:365d [--hr-policy downsample|stats|merge] --tz Europe/Warsaw --out <prefix>` — selected-tables-only semantics: only listed tables aggregated, all others pass through byte-identical
- [ ] Baby Step C.2: Dry-run mode (default): per table — eligible days, records before/after, reduction %, per-day sum spot-checks (first/last/3 deterministic "random" days), mixed-method warnings, overlap-precondition findings, estimated size after VACUUM; NO writes
- [ ] Baby Step C.3: Build mode: aggregated db + manifest + packed zip; all B.6 invariants; refuses if output would overwrite the source export (rollback protection); `last_modified_time` pinned to the source db's MAX(last_modified_time) (deterministic reruns — no wall clock); prints the runbook:
  1. Keep this export — it is the ONLY rollback
  2. HC → verify auto-delete is OFF (could purge just-imported old data)
  3. Verify no HC app updates pending (schema drift)
  4. HC → Manage data → Delete ALL data (all types) — schedule immediately after a scheduled export, apps quiesced
  5. Import the aggregated zip; then **empirical UI verification**: pick one known pre-cutoff day, compare the HC daily total shown before wipe vs after import (per-app DB sums are conserved by invariant, but HC's cross-app priority dedup of now-fully-overlapping daily records may display only one app's value — if displayed totals drop, stop and report; contingency design: record spans first→last coverage instead of midnight-midnight)
  6. Expect app re-syncs to re-add recent fine-grained data (partial — see C.4)
  7. Post-import: `ghcdb diff --aggregated` against the NEXT scheduled export; that export also becomes the new rollback baseline
- [ ] Baby Step C.4: Warnings (enumerated, not hand-waved): wipe loses everything written after the export's newest record; re-sync is partial and app-dependent (PoC: steps +47, HR +3 recovered — same-day only); NEVER recovers: manual HC entries, direct-to-HC apps that don't re-sync history (openScale-style), data beyond an app's cloud retention. **Rollback definition: a second full wipe + import of the kept original export. NEVER import the original over aggregated data without wiping first — merge semantics would insert original rows alongside aggregated rows and every aggregated day double-counts (franken-state).** Failed-import recovery (import rejected = atomic, nothing changed): just import the original after the wipe; note this restores the export's state, not the pre-wipe state

### Phase D: Aggregation-aware diff
Commit: `feat: diff --aggregated mode (manifest-driven, policy-aware)`

- [ ] Baby Step D.1: `validation/diff.py` — `--aggregated <manifest>`: aggregated tables compared per (app, local_date) against manifest expected sums — pre-cutoff days must match exactly; post-cutoff days = live growth (expected); **declared re-sync deviations** (new channel, analogous to --expected-deletions): re-synced fine-grained rows may land on pre-cutoff days IN ADDITION to aggregated rows (fresh ≥ snapshot tolerated on declared days only — re-sync is not contracted to same-day); policy-dependent HR metric (merge/stats: min/max; downsample: sample count + drift report); one-side-only days classified (new live days vs missing = FAIL); pass-through tables keep uuid roundtrip; verdicts PASS / PASS_WITH_EXPECTED_DEVIATIONS / FAIL
- [ ] Baby Step D.2: Tests for the new mode on fixtures (incl. manifest-driven cutoff boundary, declared re-sync day, HR policy metrics)

### Phase E: Fixtures, tests, docs, release
Commit: `test: aggregation fixtures; docs: aggregation guide; chore: v0.2.0`

- [ ] Baby Step E.1: Fixture extensions — synthetic multi-app step intervals (phone + watch, overlapping and disjoint days); HR days with dense samples; **threshold-straddler days** (records crossing cutoff — whole day must pass through); **DST-transition days** (23h/25h local-midnight spans, differing start/end offsets); **midnight-straddling source intervals** (documented start-day attribution); zero-value days; mixed recording_method groups; a day with records from two apps; empty-hour HR days; poison: planted wrong sum (invariant must abort), planted orphan series row, planted intra-app overlap in source
- [ ] Baby Step E.2: Tests — per-domain aggregators (sums, spans, attribution, device inheritance); three HR policies with exact expected samples (bucket edges: sample at exactly HH:00:00 → bucket HH); threshold boundary (straddler day untouched); determinism (two runs byte-identical — pinned last_modified_time + ordered summation); pass-through byte-identity; idempotency (re-aggregate aggregated db = no-op); refuse-mix (aggregated day + new raw samples); conservation invariants incl. policy-conditional HR; CLI dry-run/build smoke; diff --aggregated scenarios
- [ ] Baby Step E.3: Docs — README "Shrinking your database" (workflow, policies, trade-offs, per-app-sums-vs-displayed-totals caveat, irreversibility, rollback = second wipe + import original); skill file: aggregate workflow, no-pilot rationale, new invariants, manifest; troubleshooting: post-wipe re-sync quirks, franken-state warning, displayed-totals-drop diagnosis
- [ ] Baby Step E.4: Version 0.2.0 (pyproject + __init__), full suite green, tag

---

## Progress Log

### 2026-08-26 11:00 — Plan created after design discussion
- **Changed:** none (planning only)
- **Learning:** Aggregation is the tool's first destructive transformation; HC's merge-only import forces a wipe-and-reimport cycle, making rollback discipline (kept export) and sum-conservation invariants the two non-negotiable safety mechanisms. Real-db sizing: HR is 80% of volume — policy choice there dominates the outcome.

### 2026-08-26 11:15 — Oracle review: 17 findings, all accepted (3 with refinements); plan revised
- **Changed:** plan rewritten — day-granular eligibility (B.1), package_name uuid namespace + ordered summation + zero-day policy (B.3), HR span/bucket/placement edges + series deletion (B.4), destructive writer mode (B.5), policy-conditional invariants + orphan checks + overlap precondition (B.6), manifest (B.7), re-run safety (B.8), rollback redefinition + runbook hardening (C.3/C.4), manifest-driven diff (D.1), fixture/test gaps closed (E.1/E.2), honesty wording precision + v0.1.0 sleep-invariant bug fix (A.1–A.3)
- **Learning:** The two blocking defect clusters: (1) rollback narrative was wrong — merge semantics make "re-import the original" a doubling bug except in the narrow rejected-import case; rollback is ALWAYS a second wipe + import original. (2) Invariants promised more than mechanisms deliver — downsample destroys HR extremes by design (min/max gate mathematically unsatisfiable for the default policy → policy-conditional gates), and "daily totals" are only per-app DB sums: HC's cross-app priority dedup of fully-overlapping daily records may display one app's value — empirical UI verification is now a mandatory runbook step with a contingency design. Code-level findings verified before acceptance: invariants.py:47 sleep end-based (contradicts PoC E.6 start-based canonical — latent v0.1.0 false-alarm on real exports, masked by end-based fixture seeding); writer.py:64 `value <= 0` inconsistency. Oracle's hc-internals §1.2 reconciliation was right — our own evidence says the SAF picker is not cloud-only; honesty fix must be precise, not blanket.

---

## Notes
- **User decisions (2026-08-26):** per-app aggregation (no cross-app merge); HR policies = `merge` (conservative) / `downsample` (default) / `stats` (aggressive); sleep stages NEVER aggregated (hypnogram is irreversible loss); replace workflow = **full wipe + import** (wave-1 pattern) — accepted trade-off: data written after the export is lost unless apps re-sync (re-sync is partial, app-dependent, same-day only in PoC evidence).
- **Key decisions:** threshold relative to each table's NEWEST record; **day is the eligibility unit** (never manufacture same-app overlap); aggregated records keep source-app attribution, most-common device and recording_method (mixed-method warned in dry-run); uuid namespace uses **package_name** (rowids unstable across exports); conservation = per-app daily DB sums exact (steps/distance/calories), HR policy-conditional (merge/stats: min/max exact; downsample: drift reported); rollback = **second full wipe + import original, never overlay**; `last_modified_time` pinned from source db (determinism); VACUUM after aggregation.
- **Constraints:** HC merge semantics make non-wipe replacement impossible and make original-export overlay a doubling bug; aggregated zip must be a COMPLETE database (all domains, aggregated + pass-through); diff needs the aggregate manifest (cutoffs/policies are not guessable); displayed daily totals in HC readers are NOT guaranteed equal to per-app DB sums (cross-app priority dedup unverified for fully-overlapping daily records — empirical verification step + contingency: spans first→last instead of midnight-midnight).
- **Known risks:** user wipes but import fails → import original after wipe (restores export state, not pre-wipe state); apps re-syncing after wipe re-introduce fine-grained recent data (expected; may also land on pre-cutoff days → declared-deviation channel in diff); displayed-totals drop after aggregation (contingency design documented); future HC schema versions — knowledge.py version guard applies.
- **Dependencies:** A independent (A.3 bug fix should land FIRST — B/C invariants build on it); B before C; C before D (manifest shape); E last (fixtures parallelizable with B, but E.1 poison cases depend on B.6 final invariant list).
- **Scope guard:** v0.2.0 = README honesty + aggregation + selected-tables semantics + manifest-driven diff + v0.1.0 invariant bug fix. NOT in scope: cross-app merge, sleep-stage aggregation, automatic on-device deletion (impossible by design), GUI.
