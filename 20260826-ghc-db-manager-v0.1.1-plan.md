# ghc-db-manager v0.1.1: patch release — invariant bug fix + honesty fixes

**Date:** 2026-08-26
**Status:** COMPLETE — v0.1.1 (2026-08-26): all phases done, 252/252 tests green

## Problem
v0.1.0 ships a latent false-alarm bug and an honesty gap. The bug: `validation/invariants.py` checks sleep `local_date` as end-based (line 47), but HC's canonical recomputed form is start-based (PoC E.6: 462 sessions normalized on import) — any user running `ghcdb validate`/`diff` against a real post-import export gets a false FAIL on sleep. The fixture db seeds end-based sleep, so our own tests mask it. The honesty gap: README/docs market the tool as offline/local while the documented workflow transits cloud storage (scheduled export writes only to a cloud provider app; import verified via Drive).

## Current State
- v0.1.0 published (commit a5cf411, 249 tests green, public repo).
- Verified in code: `invariants.py:47` `("sleep_session_record_table", ..., True)  # end-based`; `writer.py:64` rejects `value <= 0` on the instant path only (interval path has no such check — inconsistent); FK invariant covers weight tables only.
- Oracle review (#16) confirmed the sleep-invariant contradiction with `hc-internals.md` §4.4 and `knowledge.py`; (#14) specified the precise honesty wording (SAF picker is NOT cloud-only — our own evidence says local providers work; cloud-only is the scheduled export).

## Target State
- Invariants agree with HC canonical behavior; real post-import exports pass `validate`/`diff` cleanly.
- README/docs/skill state the accurate data-flow split; no overclaim anywhere.
- v0.1.1 tagged and pushed, with the repo's first CHANGELOG entry.

---

## TODO List
<!-- PRIMARY NAVIGATION — always update here first -->

### Phase A: Invariant bug fix (correctness)
Commit: `fix: sleep local_date invariant basis + FK coverage + zero-value consistency`

- [x] 2026-08-26 11:35 - Baby Step A.1: invariants.py — sleep local_date → start-based; end-based branch removed (all interval tables uniformly start-based)
- [x] 2026-08-26 11:35 - Baby Step A.2: fixture db sleep reseeded start-based (canonical real-export shape)
- [x] 2026-08-26 11:35 - Baby Step A.3: regression tests — canonical passes / end-based fails (direction-pinning) + FK missing app/device fails
- [x] 2026-08-26 11:35 - Baby Step A.4: FK invariant extended to all 9 record tables (registry-driven UNION)
- [x] 2026-08-26 11:35 - Baby Step A.5: writer-level value<=0 rejection removed (domain layers own plausibility); ROOT CAUSE also fixed: zepp.py sleep adapter computed end-based local_date (source of the bug, not just the check); 252/252 green (+4 regression, -1 obsolete)

### Phase B: Honesty fixes (docs)
Commit: `docs: correct offline claims — precise cloud-transport wording`

- [x] 2026-08-26 11:35 - Baby Step B.1: README — three-part split (scheduled export cloud-only; SAF import any source, Drive-verified/local-untested; processing local), Data flow section + ASCII diagram, Safety section split into accurate bullets
- [x] 2026-08-26 11:35 - Baby Step B.2: sweep — skill/hc-internals §1.2/integration-guide/case-study verified accurate as-is (grep: zero overclaim matches remaining)

### Phase C: Release chores
Commit: `chore: v0.1.1`

- [x] 2026-08-26 11:40 - Baby Step C.1: CHANGELOG.md created (0.1.0 retroactive + 0.1.1 entries)
- [x] 2026-08-26 11:40 - Baby Step C.2: version 0.1.1 (pyproject + __init__), 252/252 green, writer.py _ms() type annotations fixed (pre-existing LSP noise), commit + tag v0.1.1 + push
- [x] 2026-08-26 11:40 - Baby Step C.3: v0.2.0 plan bookkeeping — Phase A marked as extracted into v0.1.1

---

## Progress Log

### 2026-08-26 11:25 — Mini-plan created (extracted from v0.2.0 Phase A + release hygiene)
- **Changed:** none (planning only)
- **Learning:** The sleep-invariant bug is the textbook case for why fixtures must encode the CANONICAL shape of real data, not the shape our own writer happened to produce — the fixture agreed with the bug. The regression test (A.3) pins the direction: real-export shape passes, legacy shape fails.

### 2026-08-26 12:15 — FINAL 0.1.1 TASK: real-data audit vs PoC (fixes released as v0.1.2)
- **Changed:** 20 defects found & fixed by running the tool against the REAL PoC data (the fixtures had masked all of them): (1) Zepp dispatch matched exact stems — real exports use timestamp suffixes, ALL Zepp data silently skipped; (2) raw_fields crashed on row[None] lists (real SLEEP 'naps' embeds JSON defeating CSV quoting); (3) weight domain touched .meta on IntervalRecords; (4) DEFAULT_APP_INFO_ID=5 was a fixture rowid → resolved to Google Fit on real db (replaced with per-domain package-name defaults); (5) R6 `break` skipped multi-match pairs (wave-1 +1 row); (6) activity cutoff start-based instead of end-based (+1 day overlapping live data); (7) stage segmentation merged across gaps; (8) SLEEP_MINUTE duplicate minutes fragmented segments (207 dups, 141-segment garbage nights — Oracle closed the +67 mystery); (9) HR cutoff start-based; (10) empty table → cutoff 0 → domain silently dropped (now None=import-all); (11) R3 richness key dead for Zepp; (12) sanity gates missing (person-2 leak warning etc.); (13) --tz silently ignored (threaded end-to-end); (14) post-insert cutoff invariant; (15) multi-file SLEEP_MINUTE overwrite; (16) zero-value activity semantics; (17) write counts attempts not inserts; (18) unknown sport silent drop (now warned+counted); (19) validate --domains; (20) R1 height-None leak. Plus: activity_date coverage invariants scoped to build rows (real dbs have 22k pre-existing uncovered native rows — full-table scan false-failed real builds); single pinned build_now_ms per build.
- **Learning:** Fixtures encoded simplified shapes (bare filenames, clean rows) and the DEFAULT app id came FROM the fixture — the tool had never once touched real export data before this audit. The decisive test was rebuilding both PoC waves on the real sources and diffing against the PoC canonical files: wave 1 now EXACT parity (750/750 rows, 0 value diffs), wave 2 exact on all counts with two documented deviations where the tool is MORE correct than the PoC (12 inverted sleep sessions skipped; stages deduped+clipped: 7391 vs PoC's 9044 raw/7542 clipped-emulation). Oracle verification (ora-1) caught 8 latent gaps the parity numbers alone hid (e.g. HR cutoff luck, empty-table inversion). 264/264 tests green.

---

### 2026-08-26 11:40 — v0.1.1 COMPLETE
- **Changed:** invariants sleep basis fixed (+ root cause in zepp.py adapter — the check was wrong BECAUSE the adapter computed end-based and the fixture inherited it), FK coverage all tables, writer zero-value loosened to domain layers, README honesty (three-part split + Data flow diagram), CHANGELOG.md created, version 0.1.1, writer.py LSP annotations cleaned
- **Learning:** The invariant bug had a two-layer cause: adapter computed end-based local_date (root), invariant check encoded it (symptom), fixture masked both. Fixing only the check would have broken golden tests against the still-wrong adapter — the fixer caught this correctly. 252/252 green.

---

## Notes
- **Scope:** correctness fix + docs honesty + release hygiene ONLY. No aggregation code, no CLI changes visible to users beyond validate/diff behaving correctly on real exports.
- **Additions beyond the extracted v0.2.0 Phase A** (user asked "anything else?"): CHANGELOG.md (first entry — repo hygiene for a public tool), the direction-pinning regression test (A.3), FK coverage extension (was buried in v0.2.0 A.3, cheap and safe now), v0.2.0-plan bookkeeping (C.3) so the next plan doesn't reference a phase that moved.
- **Deliberately NOT in 0.1.1:** determinism pinning of `last_modified_time` (Oracle #13 — only matters for aggregation reruns; golden tests already pin it manually), any runbook/wipe documentation (ships with aggregate in 0.2.0), workflow-section changes (still six commands until 0.2.0).
- **Dependencies:** A before C.2 (suite must be green with the fix); B independent of A; C last.
- **Risk:** none beyond a normal patch — no data-path changes for existing build/pilot users (writer zero-value change only loosens a rejection; domain bands still guard plausibility).
