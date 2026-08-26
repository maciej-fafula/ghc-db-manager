"""test_domains_activity.py — tests for activity domain."""

import datetime
import pathlib
import sys
import tempfile
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
MINI_ZEPP_DIR = FIXTURES_DIR / "mini-zepp"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.sources.zepp import parse_activity
from ghc_db_manager.domains.activity import build_activity_canonical


class TestActivityParsing(unittest.TestCase):
    """Test parse_activity."""

    def test_parse_activity(self):
        """parse_activity returns IntervalRecords with correct fields."""
        records = parse_activity(str(MINI_ZEPP_DIR / "ACTIVITY.csv"))

        # Should have 3 records per non-zero day
        # ACTIVITY.csv has 10 rows, one is all-zero (2025-02-20)
        # So 9 non-zero days × 3 = 27 records
        self.assertEqual(len(records), 27)

        # All should be activity kinds
        for r in records:
            self.assertIn(r.kind, ("steps", "distance", "calories"))

    def test_all_zero_day_skipped(self):
        """All-zero day (2025-02-20) is skipped."""
        records = parse_activity(str(MINI_ZEPP_DIR / "ACTIVITY.csv"))
        # Find records for 2025-02-20
        dates = [r.start_utc.date() for r in records]
        self.assertNotIn(datetime.date(2025, 2, 20), dates)

    def test_energy_is_kcal_times_1000(self):
        """Calories extra.energy is kcal × 1000."""
        records = parse_activity(str(MINI_ZEPP_DIR / "ACTIVITY.csv"))
        # Find the first calories record (2025-01-15 → 385 kcal → 385000 energy)
        calorie_records = [r for r in records if r.kind == "calories"]
        self.assertGreater(len(calorie_records), 0)
        first = calorie_records[0]
        self.assertEqual(first.extra["energy"], 385000.0)
        # Verify all calorie records follow kcal × 1000
        for r in calorie_records:
            raw_cal = int(r.raw.get("calories", "0"))
            self.assertEqual(r.extra["energy"], raw_cal * 1000.0)

    def test_local_midnight_start(self):
        """Activity records start at local midnight (in their timezone)."""
        records = parse_activity(str(MINI_ZEPP_DIR / "ACTIVITY.csv"))
        for r in records:
            # start_utc + offset = midnight local time
            # i.e. the record represents the period 00:00–23:59:59 local
            local_start = r.start_utc + datetime.timedelta(seconds=r.start_offset_seconds)
            self.assertEqual(local_start.hour, 0)
            self.assertEqual(local_start.minute, 0)
            self.assertEqual(local_start.second, 0)
            # end should be exactly 1 day later
            self.assertEqual((r.end_utc - r.start_utc).days, 1)

    def test_count_fields(self):
        """Activity records have count/distance/energy extra fields."""
        records = parse_activity(str(MINI_ZEPP_DIR / "ACTIVITY.csv"))
        for r in records:
            if r.kind == "steps":
                self.assertIn("count", r.extra)
                self.assertIsInstance(r.extra["count"], int)
            elif r.kind == "distance":
                self.assertIn("distance", r.extra)
                self.assertIsInstance(r.extra["distance"], float)
            elif r.kind == "calories":
                self.assertIn("energy", r.extra)


class TestActivityCanonical(unittest.TestCase):
    """Test build_activity_canonical."""

    def test_cutoff_enforcement(self):
        """Records at/after cutoff are filtered."""
        records = parse_activity(str(MINI_ZEPP_DIR / "ACTIVITY.csv"))
        # Use a cutoff in the middle of the data
        cutoff = datetime.datetime(2025, 6, 1, tzinfo=datetime.timezone.utc)
        cutoff_ms = int(cutoff.timestamp() * 1000)

        canon, stats = build_activity_canonical(records, cutoffs={"steps": cutoff_ms})
        # Records from June onwards should be cut off for steps
        self.assertGreater(cutoff_ms, 0)
        # Verify cutoff filtering worked
        for r in canon:
            if r.kind == "steps":
                self.assertLess(r.start_ms, cutoff_ms)

    def test_returns_correct_stats(self):
        """Returns rule stats dict."""
        records = parse_activity(str(MINI_ZEPP_DIR / "ACTIVITY.csv"))
        _, stats = build_activity_canonical(records)
        self.assertIsInstance(stats, dict)


if __name__ == "__main__":
    unittest.main()
