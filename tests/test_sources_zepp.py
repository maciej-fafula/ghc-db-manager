"""test_sources_zepp.py — unit tests for the Zepp source adapter."""

import pathlib
import sys
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
MINI_ZEPP_DIR = FIXTURES_DIR / "mini-zepp"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.sources import zepp
from ghc_db_manager.sources.zepp import parse_body


class TestZeppAdapter(unittest.TestCase):
    """Tests for the Zepp BODY CSV adapter."""

    @classmethod
    def setUpClass(cls):
        cls.csv_path = str(MINI_ZEPP_DIR / "BODY.csv")
        cls.records = parse_body(cls.csv_path)

    def test_raw_count(self):
        """BODY.csv has 14 rows → 14 weight records."""
        weight = [r for r in self.records if r.kind == "weight"]
        self.assertEqual(len(weight), 14)

    def test_body_fat_count(self):
        """Body fat records only for rows with fatRate > 0 and not null."""
        bf = [r for r in self.records if r.kind == "body_fat"]
        self.assertGreater(len(bf), 0)
        # Row 4 (3.3kg outlier) has fatRate=0 → no body_fat
        # Rows with null fatRate → no body_fat
        self.assertLessEqual(len(bf), 14)

    def test_muscle_rate_never_emitted(self):
        """muscleRate is a percent — it should NEVER be emitted as lean_mass."""
        kinds = {r.kind for r in self.records}
        self.assertNotIn("lean_mass", kinds,
            "muscleRate should NOT be mapped to lean_mass (it's a percent, not mass)")

    def test_null_handling(self):
        """Records with null values should be handled gracefully."""
        # Row 13: fatRate=null, muscleRate=null
        # This row should still produce a weight record
        weight_vals = [r.value for r in self.records if r.kind == "weight"]
        self.assertEqual(len(weight_vals), 14)

    def test_outlier_weight_not_filtered(self):
        """The 3.3kg outlier should be parsed (filtered by domain rules, not adapter)."""
        weight = [r for r in self.records if r.kind == "weight"]
        outlier = [r for r in weight if r.value < 10]
        self.assertEqual(len(outlier), 1,
            "3.3kg outlier should be in raw output (filtered later by domain rules)")

    def test_zepp_source(self):
        """All records should have source='zepp'."""
        for r in self.records:
            self.assertEqual(r.source, "zepp")

    def test_height_in_meta(self):
        """Height should be stored in meta."""
        weight = [r for r in self.records if r.kind == "weight"]
        heights = {r.meta.get("height") for r in weight}
        self.assertIn(175.0, heights)
        self.assertIn(160.0, heights)

    def test_ms_computed(self):
        """All records should have ms computed from time_utc."""
        for r in self.records:
            self.assertGreater(r.ms, 0)
            expected_ms = int(r.time_utc.timestamp() * 1000)
            self.assertEqual(r.ms, expected_ms)

    def test_all_timestamps_aware(self):
        """All time_utc values should be timezone-aware."""
        for r in self.records:
            self.assertIsNotNone(r.time_utc.tzinfo)

    def test_muscle_rate_note_in_meta(self):
        """When muscleRate is present, meta should contain a note."""
        # Find records where muscleRate was non-null
        has_note = [r for r in self.records
                    if r.meta.get("muscle_rate_note") is not None]
        self.assertGreater(len(has_note), 0,
            "Records with muscleRate should have a note explaining it's not mapped")


if __name__ == "__main__":
    unittest.main()


class TestTimestampSuffixedFilenames(unittest.TestCase):
    """Regression: real Zepp exports suffix filenames with timestamps
    (BODY_1787404253776.csv); v0.1.1 dispatch matched exact stems only and
    silently returned [] for every real export file (found by real-data audit
    2026-08-26; fixtures used bare names and masked the bug)."""

    def test_body_with_timestamp_suffix_parses(self):
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as td:
            dst = pathlib.Path(td) / "BODY_1787404253776.csv"
            shutil.copyfile(pathlib.Path(__file__).parent / "fixtures" / "mini-zepp" / "BODY.csv", dst)
            recs = zepp.load_zepp(str(dst))
            self.assertGreater(len(recs), 0)

    def test_directory_with_suffixed_files_parses(self):
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(__file__).parent / "fixtures" / "mini-zepp"
            for f in src.glob("*.csv"):
                shutil.copyfile(f, pathlib.Path(td) / f"{f.stem}_1787404253776.csv")
            recs = zepp.load_zepp(td)
            kinds = {r.kind for r in recs}
            self.assertIn("weight", kinds)          # BODY
            self.assertIn("sleep", kinds)           # SLEEP
            self.assertIn("exercise", kinds)        # SPORT

    def test_activity_minute_skipped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "ACTIVITY_MINUTE_123.csv"
            p.write_text("date,time,steps\n2026-01-01,10:00,5\n", encoding="utf-8")
            self.assertEqual(zepp.load_zepp(str(p)), [])
