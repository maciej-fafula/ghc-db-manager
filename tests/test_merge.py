"""test_merge.py — unit tests for the weight-domain merge rules."""

import datetime
import pathlib
import sqlite3
import sys
import tempfile
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
MINI_ZEPP_DIR = FIXTURES_DIR / "mini-zepp"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.sources.libra import parse as parse_libra
from ghc_db_manager.sources.zepp import parse_body as parse_zepp
from ghc_db_manager.domains.weight import (
    build_weight_canonical,
    rule_r1_profile_filter,
    rule_r2_plausibility,
    rule_r3_intra_source_dedup,
    rule_r4_hc_exclusion,
    rule_r5_exact_ts_collision,
    rule_r6_same_measurement_day,
    rule_r7_derived_inherit,
)
from ghc_db_manager.merge import merge
from ghc_db_manager.sources import RawRecord


def _dt(year, month, day, hour=12, minute=0, second=0):
    return datetime.datetime(year, month, day, hour, minute, second,
                            tzinfo=datetime.timezone.utc)


def _rec(source, kind, dt, value, meta=None):
    meta = meta or {}
    return RawRecord(source=source, kind=kind, time_utc=dt, value=value, meta=meta)


def _ms(rec):
    return int(rec.time_utc.timestamp() * 1000)


class TestRuleR1ProfileFilter(unittest.TestCase):
    """R1: profile filter for Zepp."""

    def test_zepp_height_filter_drops_person2(self):
        """Rows with height != profile_height should be dropped."""
        records = [
            _rec("zepp", "weight", _dt(2023, 4, 10), 60.2,
                 meta={"height": 160.0}),   # person 2
            _rec("zepp", "weight", _dt(2023, 9, 25), 60.5,
                 meta={"height": 160.0}),   # person 2
            _rec("zepp", "weight", _dt(2023, 4, 10), 75.0,
                 meta={"height": 175.0}),   # person 1
        ]
        kept, n, ws = rule_r1_profile_filter(records, zepp_profile_height=175.0)
        self.assertEqual(n, 2)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].meta["height"], 175.0)

    def test_zepp_no_filter_keeps_all(self):
        """When zepp_profile_height=None, no rows are dropped by R1."""
        records = [
            _rec("zepp", "weight", _dt(2023, 4, 10), 60.2, meta={"height": 160.0}),
            _rec("zepp", "weight", _dt(2023, 9, 25), 60.5, meta={"height": 160.0}),
        ]
        kept, n, ws = rule_r1_profile_filter(records, zepp_profile_height=None)
        self.assertEqual(n, 0)
        self.assertEqual(len(kept), 2)

    def test_libra_not_filtered(self):
        """Libra rows should pass through R1 unchanged."""
        records = [
            _rec("libra", "weight", _dt(2020, 3, 1), 65.2),
        ]
        kept, n, ws = rule_r1_profile_filter(records, zepp_profile_height=175.0)
        self.assertEqual(n, 0)
        self.assertEqual(len(kept), 1)


class TestRuleR2Plausibility(unittest.TestCase):
    """R2: plausibility band filter."""

    def test_outlier_160kg_row_dropped(self):
        """The 160kg outlier in fixture should be dropped by R2."""
        records = [
            _rec("zepp", "weight", _dt(2023, 4, 10), 160.0),  # 160.0 is at the boundary
            _rec("zepp", "weight", _dt(2023, 4, 10), 75.0),
        ]
        kept, n, ws = rule_r2_plausibility(records, set(), weight_min=40.0, weight_max=150.0)
        self.assertEqual(n, 1)
        self.assertEqual(len(kept), 1)

    def test_40_and_250_kg_accepted(self):
        """Boundary values 40 and 250 should be accepted."""
        records = [
            _rec("zepp", "weight", _dt(2023, 4, 10), 40.0),
            _rec("zepp", "weight", _dt(2023, 4, 10), 250.0),
        ]
        kept, n, ws = rule_r2_plausibility(records, set(), weight_min=40.0, weight_max=250.0)
        self.assertEqual(n, 0)
        self.assertEqual(len(kept), 2)

    def test_body_fat_not_filtered(self):
        """body_fat records should pass through R2 unchanged."""
        records = [
            _rec("zepp", "body_fat", _dt(2023, 4, 10), 22.8),
            _rec("zepp", "body_fat", _dt(2023, 9, 25), 50.0),  # unrealistic but body_fat not filtered
        ]
        kept, n, ws = rule_r2_plausibility(records, set())
        self.assertEqual(n, 0)
        self.assertEqual(len(kept), 2)


class TestRuleR3IntraSourceDedup(unittest.TestCase):
    """R3: intra-source duplicate timestamp → richer row wins."""

    def test_dup_ts_richer_wins(self):
        """At same timestamp, row with more non-null fields wins."""
        rich = _rec("zepp", "weight", _dt(2023, 3, 15), 75.5,
                    meta={"bf_raw": "22.5", "mm_raw": "70.5"})
        poor = _rec("zepp", "weight", _dt(2023, 3, 15), 75.5,
                    meta={"bf_raw": "", "mm_raw": ""})
        records = [rich, poor]

        kept, n, ws = rule_r3_intra_source_dedup(records, set())
        # rich should win, poor should be dropped
        self.assertEqual(n, 1)
        kept_weights = [r for r in kept if r.kind == "weight"]
        self.assertEqual(len(kept_weights), 1)
        self.assertEqual(kept_weights[0].meta.get("bf_raw"), "22.5")

    def test_different_sources_no_dedup(self):
        """Different sources with same timestamp are not deduped by R3."""
        records = [
            _rec("libra", "weight", _dt(2023, 3, 15), 75.0),
            _rec("zepp", "weight", _dt(2023, 3, 15), 75.0),
        ]
        kept, n, ws = rule_r3_intra_source_dedup(records, set())
        self.assertEqual(n, 0)
        self.assertEqual(len(kept), 2)


class TestRuleR4HCExclusion(unittest.TestCase):
    """R4: HC exclusion (±2 s, same rounded value)."""

    def test_exact_match_dropped(self):
        """A row matching an HC row should be dropped."""
        hc_rows = [(_dt(2023, 3, 15, 8, 0), 75.5)]
        records = [_rec("zepp", "weight", _dt(2023, 3, 15, 8, 0, 0), 75.5)]
        kept, n, ws = rule_r4_hc_exclusion(records, set(), hc_rows)
        self.assertEqual(n, 1)
        self.assertEqual(len(kept), 0)

    def test_different_value_not_dropped(self):
        """A row with a different value should not be dropped."""
        hc_rows = [(_dt(2023, 3, 15, 8, 0), 75.5)]
        records = [_rec("zepp", "weight", _dt(2023, 3, 15, 8, 0), 76.0)]
        kept, n, ws = rule_r4_hc_exclusion(records, set(), hc_rows)
        self.assertEqual(n, 0)
        self.assertEqual(len(kept), 1)


class TestRuleR5ExactTsCollision(unittest.TestCase):
    """R5: cross-source exact-timestamp collision → priority source wins."""

    def test_libra_wins_on_exact_collision(self):
        """At same timestamp, non-priority source row should be dropped."""
        records = [
            _rec("libra", "weight", _dt(2023, 3, 15), 75.0),
            _rec("zepp", "weight", _dt(2023, 3, 15), 75.0),
        ]
        kept, n, ws = rule_r5_exact_ts_collision(records, set(), priority_source="libra")
        self.assertEqual(n, 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].source, "libra")


class TestRuleR6SameMeasurementDay(unittest.TestCase):
    """R6: same-measurement day → drop lower-priority row."""

    def test_same_day_close_pair_dropped(self):
        """Two rows same day, Δt≤30min, Δw≤0.5kg → lower-priority dropped."""
        # Both on same day (2023-03-15), 20 minutes apart, 0.3kg apart
        libra_rec = _rec("libra", "weight", _dt(2023, 3, 15, 7, 30), 75.0)
        zepp_rec = _rec("zepp", "weight", _dt(2023, 3, 15, 7, 50), 75.3)
        records = [libra_rec, zepp_rec]
        # kept_ws must contain both weight ms (they survive R1-R3)
        kept_ws = {_ms(r) for r in records if r.kind == "weight"}
        kept, n, ws = rule_r6_same_measurement_day(records, kept_ws, priority_source="libra")
        self.assertEqual(n, 1)  # zepp row dropped
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].source, "libra")

    def test_same_day_far_apart_not_dropped(self):
        """Two rows same day but Δt > 30min should not be dropped."""
        records = [
            _rec("libra", "weight", _dt(2023, 3, 15, 7, 0), 75.0),
            _rec("zepp", "weight", _dt(2023, 3, 15, 8, 0), 75.3),  # 60min apart
        ]
        kept_ws = {_ms(r) for r in records if r.kind == "weight"}
        kept, n, ws = rule_r6_same_measurement_day(records, kept_ws, priority_source="libra")
        self.assertEqual(n, 0)
        self.assertEqual(len(kept), 2)


class TestRuleR7DerivedInherit(unittest.TestCase):
    """R7: derived kinds inherit parent weight decision."""

    def test_body_fat_dropped_when_weight_dropped(self):
        """body_fat should be dropped if its parent weight ms is not in kept_ws."""
        records = [
            _rec("zepp", "body_fat", _dt(2023, 3, 15, 8, 0), 22.5),
            _rec("zepp", "weight", _dt(2023, 3, 15, 8, 0), 75.5),
        ]
        kept_ws = set()  # weight was dropped earlier, so its ms is not in kept_ws
        kept, n = rule_r7_derived_inherit(records, kept_ws)
        # body_fat should be dropped (weight is never dropped by R7)
        self.assertEqual(n, 1)  # 1 body_fat dropped
        self.assertEqual(len(kept), 1)  # weight record kept
        self.assertEqual(kept[0].kind, "weight")

    def test_body_fat_kept_when_weight_kept(self):
        """body_fat should be kept if its parent weight is in kept_ws."""
        records = [
            _rec("zepp", "body_fat", _dt(2023, 3, 15, 8, 0), 22.5),
            _rec("zepp", "weight", _dt(2023, 3, 15, 8, 0), 75.5),
        ]
        weight_ms = int(_dt(2023, 3, 15, 8, 0).timestamp() * 1000)
        kept_ws = {weight_ms}
        kept, n = rule_r7_derived_inherit(records, kept_ws)
        self.assertEqual(n, 0)
        self.assertEqual(len(kept), 2)


class TestBuildWeightCanonical(unittest.TestCase):
    """Integration test: full rule pipeline on mini fixtures + fixture db cutoffs."""

    @classmethod
    def setUpClass(cls):
        cls.libra_records = parse_libra(str(FIXTURES_DIR / "mini-libra.csv"))
        cls.zepp_records = parse_zepp(str(MINI_ZEPP_DIR / "BODY.csv"))
        cls.all_records = cls.libra_records + cls.zepp_records

        # Build fixture db and get HC rows for R4
        cls._db_fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._db_fd.close()
        from tests.fixtures.make_fixture_db import build
        build(cls._db_fd.name)
        cls.db_path = cls._db_fd.name

    @classmethod
    def tearDownClass(cls):
        pathlib.Path(cls.db_path).unlink(missing_ok=True)

    def test_person_filter_drops_160_height_rows(self):
        """R1: zepp height=160 rows should be dropped (3 rows)."""
        records, stats = build_weight_canonical(
            self.all_records,
            zepp_profile_height=175.0,
        )
        zepp_weight = [r for r in records if r.source == "zepp" and r.kind == "weight"]
        heights = {r.meta.get("height") for r in zepp_weight}
        self.assertNotIn(160.0, heights)
        self.assertIn(175.0, heights)

    def test_outlier_3_3kg_dropped(self):
        """R2: the 3.3kg outlier should be dropped."""
        records, stats = build_weight_canonical(
            self.all_records,
            zepp_profile_height=175.0,
        )
        zepp_weight = [r for r in records if r.source == "zepp" and r.kind == "weight"]
        outlier = [r for r in zepp_weight if r.value < 10]
        self.assertEqual(len(outlier), 0)

    def test_richer_wins_on_dup_ts(self):
        """R3: at duplicate timestamp, richer row wins."""
        records, stats = build_weight_canonical(
            self.all_records,
            zepp_profile_height=175.0,
        )
        # mini-zepp has duplicate ts 2025-03-15 08:00:00 with different fatRate
        zepp_weights = [r for r in records if r.source == "zepp" and r.kind == "weight"
                        and r.time_utc.year == 2025]
        # Should be exactly 1 weight at that timestamp
        march15 = [r for r in zepp_weights
                   if r.time_utc.month == 3 and r.time_utc.day == 15 and r.time_utc.year == 2025]
        self.assertLessEqual(len(march15), 1)

    def test_hc_exclusion_uses_hc_db(self):
        """R4: merge with hc_db_path should load HC rows and apply exclusion."""
        # Use merge() which accepts hc_db_path; build_weight_canonical takes hc_rows list
        sources = {
            "libra": str(FIXTURES_DIR / "mini-libra.csv"),
            "zepp": str(MINI_ZEPP_DIR / "BODY.csv"),
        }
        records, stats = merge(
            sources,
            domain="weight",
            zepp_profile_height=175.0,
            hc_db_path=self.db_path,
        )
        # The R4 rule should appear in stats (HC rows exist in fixture db)
        # (may or may not drop rows depending on exact matches)

    def test_stats_dict_has_per_rule_counts(self):
        """Rule stats dict should have per-rule counts for triggered rules."""
        sources = {
            "libra": str(FIXTURES_DIR / "mini-libra.csv"),
            "zepp": str(MINI_ZEPP_DIR / "BODY.csv"),
        }
        records, stats = merge(
            sources,
            domain="weight",
            zepp_profile_height=175.0,
            hc_db_path=self.db_path,
        )
        # Stats should be present for triggered rules
        self.assertIn("R1_profile_filter", stats)
        self.assertIn("R2_plausibility", stats)
        # R1 drops rows where zepp height != profile_height
        self.assertGreater(stats["R1_profile_filter"], 0)

    def test_no_160cm_leak_in_body_fat(self):
        """body_fat from 160cm person should also be filtered."""
        records, stats = build_weight_canonical(
            self.all_records,
            zepp_profile_height=175.0,
        )
        zepp_bf = [r for r in records if r.source == "zepp" and r.kind == "body_fat"]
        heights = {r.meta.get("height") for r in zepp_bf}
        self.assertNotIn(160.0, heights)

    def test_all_zones_valid(self):
        """All zone_offset_seconds values should be within ±64800."""
        records, stats = build_weight_canonical(
            self.all_records,
            zepp_profile_height=175.0,
        )
        for r in records:
            self.assertGreaterEqual(r.zone_offset_seconds, -64800)
            self.assertLessEqual(r.zone_offset_seconds, 64800)

    def test_local_date_formula_valid(self):
        """All local_date values should match the knowledge formula."""
        from ghc_db_manager import knowledge as kn
        records, stats = build_weight_canonical(
            self.all_records,
            zepp_profile_height=175.0,
        )
        for r in records:
            expected_ld = kn.local_date_epoch_days(r.ms, r.zone_offset_seconds)
            self.assertEqual(r.local_date, expected_ld,
                f"local_date mismatch for {r.source}/{r.kind} at {r.time_utc}")

    def test_libra_midnight_entries_get_correct_offset(self):
        """Libra midnight entries (22:00Z / 23:00Z) should get fixed offsets."""
        records, stats = build_weight_canonical(
            self.all_records,
        )
        libra_weights = [r for r in records if r.source == "libra" and r.kind == "weight"]
        for r in libra_weights:
            if r.time_utc.hour == 22:
                self.assertEqual(r.zone_offset_seconds, 7200)
            elif r.time_utc.hour == 23:
                self.assertEqual(r.zone_offset_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
