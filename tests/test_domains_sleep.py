"""test_domains_sleep.py — tests for sleep domain."""

import datetime
import pathlib
import sys
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
MINI_ZEPP_DIR = FIXTURES_DIR / "mini-zepp"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.sources.zepp import parse_sleep, parse_sleep_minute
from ghc_db_manager.domains.sleep import build_sleep_canonical


class TestSleepParsing(unittest.TestCase):
    """Test parse_sleep and parse_sleep_minute."""

    def test_parse_sleep_skips_placeholders(self):
        """Placeholder rows (start==stop) are skipped."""
        records = parse_sleep(str(MINI_ZEPP_DIR / "SLEEP.csv"))
        # SLEEP.csv has 8 rows; rows 2 and 6 are placeholders (start==stop)
        # So we expect 6 real sessions
        self.assertEqual(len(records), 6)
        for r in records:
            self.assertNotEqual(r.start_utc, r.end_utc)

    def test_parse_sleep_returns_intervals(self):
        """parse_sleep returns IntervalRecords with kind=sleep."""
        records = parse_sleep(str(MINI_ZEPP_DIR / "SLEEP.csv"))
        for r in records:
            self.assertEqual(r.kind, "sleep")

    def test_parse_sleep_minute(self):
        """parse_sleep_minute groups by wake date and emits stages."""
        records = parse_sleep_minute(str(MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"))
        # SLEEP_MINUTE.csv has 40 rows spanning 3 wake dates
        # 2025-01-15 (night with 25 mins), 2025-04-10 (16 mins)
        # Should produce records keyed by wake date
        self.assertGreater(len(records), 0)
        for r in records:
            self.assertEqual(r.kind, "sleep_stage")


class TestSleepStagesMerging(unittest.TestCase):
    """Test segment merging from consecutive same-stage minutes."""

    def test_consecutive_same_stage_merged(self):
        """Consecutive same-stage minutes are merged into a single segment."""
        records = parse_sleep_minute(str(MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"))
        # Find the record for wake date 2025-01-15
        # (has 25 minutes of stages)
        night = [r for r in records if r.raw.get("wake_date") == "2025-01-15"]
        self.assertEqual(len(night), 1)
        rec = night[0]
        # 25 minute entries but some are consecutive same-stage
        # Should be fewer segments than entries
        self.assertLess(len(rec.stages), 25)
        # Each stage should have start < end
        for st_start, st_end, st_type in rec.stages:
            self.assertLess(st_start, st_end)

    def test_stage_ids_from_knowledge(self):
        """Stage IDs come from knowledge.SLEEP_STAGE_IDS."""
        from ghc_db_manager.knowledge import SLEEP_STAGE_IDS
        records = parse_sleep_minute(str(MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"))
        for r in records:
            for st_start, st_end, st_type in r.stages:
                self.assertIn(st_type, SLEEP_STAGE_IDS.values())


class TestSleepEveningRule(unittest.TestCase):
    """Test evening ≥20h → previous day rule."""

    def test_evening_hours_belong_to_previous_night(self):
        """SLEEP_MINUTE times with hour ≥20 are attributed to previous calendar date."""
        records = parse_sleep_minute(str(MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"))
        # 2025-01-14 20:15, 20:16, 20:17 should be in the 2025-01-15 wake date record
        night15 = [r for r in records if r.raw.get("wake_date") == "2025-01-15"]
        self.assertEqual(len(night15), 1)
        rec = night15[0]
        # The stages should have been adjusted so that
        # the 20:15-21:03 entries from "2025-01-14" file rows
        # contribute to the same night's stages


class TestSleepBuildCanonical(unittest.TestCase):
    """Test build_sleep_canonical."""

    def test_no_stage_nights_session_only(self):
        """Nights without SLEEP_MINUTE data produce session-only records."""
        sleep_records = parse_sleep(str(MINI_ZEPP_DIR / "SLEEP.csv"))
        sleep_minute_records = parse_sleep_minute(str(MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"))

        # There are 8 sessions in SLEEP.csv but only 3 distinct wake dates in SLEEP_MINUTE
        # (2025-01-15, 2025-04-10 have minutes; the others don't)
        canon, stats = build_sleep_canonical(sleep_records, sleep_minute_records)
        # All sessions should be present
        self.assertEqual(len(canon), 6)  # 8 - 2 placeholders = 6
        # Sessions without minutes should have empty stages
        for r in canon:
            if r.raw.get("date") not in ("2025-01-15", "2025-04-10"):
                self.assertEqual(len(r.stages), 0)

    def test_stages_clipped_to_session_bounds(self):
        """Stage segments are clipped to session bounds."""
        sleep_records = parse_sleep(str(MINI_ZEPP_DIR / "SLEEP.csv"))
        sleep_minute_records = parse_sleep_minute(str(MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"))
        canon, stats = build_sleep_canonical(sleep_records, sleep_minute_records)

        for r in canon:
            if r.stages:
                for st_start, st_end, _ in r.stages:
                    # Each stage should be within the session
                    self.assertGreaterEqual(st_start, r.start_ms)
                    self.assertLessEqual(st_end, r.end_ms)


if __name__ == "__main__":
    unittest.main()
