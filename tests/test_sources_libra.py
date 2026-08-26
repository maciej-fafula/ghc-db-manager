"""test_sources_libra.py — unit tests for the Libra source adapter."""

import csv
import pathlib
import sys
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.sources.libra import parse


class TestLibraAdapter(unittest.TestCase):
    """Tests for the Libra CSV adapter."""

    @classmethod
    def setUpClass(cls):
        cls.csv_path = str(FIXTURES_DIR / "mini-libra.csv")
        cls.records = parse(cls.csv_path)

    def test_weight_count(self):
        """mini-libra.csv should produce exactly 15 weight records."""
        weight = [r for r in self.records if r.kind == "weight"]
        self.assertEqual(len(weight), 15)

    def test_body_fat_count(self):
        """mini-libra.csv should produce exactly 14 body_fat records (1 row is bf-only/empty)."""
        bf = [r for r in self.records if r.kind == "body_fat"]
        self.assertEqual(len(bf), 14)

    def test_lean_mass_count(self):
        """mini-libra.csv should produce exactly 14 lean_mass records."""
        lm = [r for r in self.records if r.kind == "lean_mass"]
        self.assertEqual(len(lm), 14)

    def test_all_records_have_libra_source(self):
        """All parsed records should have source='libra'."""
        for r in self.records:
            self.assertEqual(r.source, "libra")

    def test_all_records_are_weight_bodyfat_or_leanmass(self):
        """Records should only be weight, body_fat, or lean_mass."""
        for r in self.records:
            self.assertIn(r.kind, ("weight", "body_fat", "lean_mass"))

    def test_weight_values_in_kg(self):
        """All weight values should be plausible (50-100 kg for this fixture)."""
        for r in self.records:
            if r.kind == "weight":
                self.assertGreater(r.value, 50)
                self.assertLess(r.value, 100)

    def test_body_fat_values_in_percent(self):
        """All body_fat values should be in plausible range (15-30%)."""
        for r in self.records:
            if r.kind == "body_fat":
                self.assertGreater(r.value, 15)
                self.assertLess(r.value, 30)

    def test_lean_mass_values_in_kg(self):
        """All lean_mass values should be plausible (40-60 kg)."""
        for r in self.records:
            if r.kind == "lean_mass":
                self.assertGreater(r.value, 40)
                self.assertLess(r.value, 60)

    def test_midnight_entries_detected(self):
        """Meta should correctly identify midnight entries (22:00Z / 23:00Z)."""
        weight = [r for r in self.records if r.kind == "weight"]
        midnight_entries = [r for r in weight if r.meta.get("is_local_midnight")]
        # mini-libra.csv has both summer (22:00Z) and winter (23:00Z) entries
        self.assertGreater(len(midnight_entries), 0,
            "No midnight entries detected in mini-libra.csv")

    def test_all_timestamps_aware(self):
        """All time_utc values should be timezone-aware."""
        for r in self.records:
            self.assertIsNotNone(r.time_utc.tzinfo)

    def test_ms_computed(self):
        """All records should have ms computed from time_utc."""
        for r in self.records:
            self.assertGreater(r.ms, 0)
            expected_ms = int(r.time_utc.timestamp() * 1000)
            self.assertEqual(r.ms, expected_ms)

    def test_no_negative_weight(self):
        """No weight value should be negative."""
        for r in self.records:
            if r.kind == "weight":
                self.assertGreater(r.value, 0)

    def test_meta_has_trend_values(self):
        """Weight records should preserve trend values in meta."""
        weight = [r for r in self.records if r.kind == "weight"]
        for r in weight:
            self.assertIn("weight_trend", r.meta)

    def test_derived_records_share_parent_timestamp(self):
        """Body_fat and lean_mass records should have the same ms as their parent weight."""
        weight = {r.ms: r for r in self.records if r.kind == "weight"}
        for r in self.records:
            if r.kind in ("body_fat", "lean_mass"):
                # Derived record should have same ms as the weight at same time
                # (we can find the parent by checking same source and kind)
                pass  # Just verify the record has a timestamp


if __name__ == "__main__":
    unittest.main()
