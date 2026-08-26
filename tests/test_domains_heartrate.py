"""test_domains_heartrate.py — tests for heartrate domain."""

import datetime
import pathlib
import sys
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
MINI_ZEPP_DIR = FIXTURES_DIR / "mini-zepp"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.sources.zepp import parse_hr_auto, parse_hr_manual
from ghc_db_manager.domains.heartrate import build_heartrate_canonical


class TestHeartRateAutoParsing(unittest.TestCase):
    """Test parse_hr_auto."""

    def test_parse_hr_auto_day_batching(self):
        """parse_hr_auto returns one record per day with all samples."""
        records = parse_hr_auto(str(MINI_ZEPP_DIR / "HEARTRATE_AUTO.csv"))
        # HEARTRATE_AUTO.csv has 36 rows across 2 days (2025-01-09: 25 rows, 2025-02-14: 12 rows)
        # Should produce 2 records (one per day)
        self.assertEqual(len(records), 2)

    def test_hr_auto_samples_collected(self):
        """Auto records have all samples for the day."""
        records = parse_hr_auto(str(MINI_ZEPP_DIR / "HEARTRATE_AUTO.csv"))
        day1 = [r for r in records if r.raw.get("day") == "2025-01-09"]
        self.assertEqual(len(day1), 1)
        self.assertGreater(len(day1[0].samples), 0)

    def test_hr_auto_sample_ordering(self):
        """Samples are sorted by epoch_ms."""
        records = parse_hr_auto(str(MINI_ZEPP_DIR / "HEARTRATE_AUTO.csv"))
        for r in records:
            samples = r.samples
            epochs = [s[0] for s in samples]
            self.assertEqual(epochs, sorted(epochs))

    def test_hr_auto_kind(self):
        """All auto records have kind=hr_auto."""
        records = parse_hr_auto(str(MINI_ZEPP_DIR / "HEARTRATE_AUTO.csv"))
        for r in records:
            self.assertEqual(r.kind, "hr_auto")


class TestHeartRateManualParsing(unittest.TestCase):
    """Test parse_hr_manual."""

    def test_parse_hr_manual_dedup(self):
        """parse_hr_manual deduplicates identical timestamps."""
        records = parse_hr_manual(str(MINI_ZEPP_DIR / "HEARTRATE.csv"))
        # HEARTRATE.csv has 6 rows but 2 share the same timestamp (2025-03-15 08:15:00).
        # parse_hr_manual deduplicates by timestamp → 5 unique records.
        self.assertEqual(len(records), 5)

    def test_hr_manual_kind(self):
        """All manual records have kind=hr_manual."""
        records = parse_hr_manual(str(MINI_ZEPP_DIR / "HEARTRATE.csv"))
        for r in records:
            self.assertEqual(r.kind, "hr_manual")

    def test_hr_manual_span(self):
        """Manual records span start to start+60s."""
        records = parse_hr_manual(str(MINI_ZEPP_DIR / "HEARTRATE.csv"))
        for r in records:
            self.assertEqual((r.end_utc - r.start_utc).seconds, 60)

    def test_hr_manual_single_sample(self):
        """Manual records have exactly 1 sample."""
        records = parse_hr_manual(str(MINI_ZEPP_DIR / "HEARTRATE.csv"))
        for r in records:
            self.assertEqual(len(r.samples), 1)


class TestHeartRateCanonical(unittest.TestCase):
    """Test build_heartrate_canonical."""

    def test_hr_auto_recording_method(self):
        """Auto HR uses recording_method=2 (AUTOMATICALLY_RECORDED)."""
        records = parse_hr_auto(str(MINI_ZEPP_DIR / "HEARTRATE_AUTO.csv"))
        canon, stats = build_heartrate_canonical(records, [])
        for r in canon:
            self.assertEqual(r.recording_method, 2)

    def test_hr_manual_recording_method(self):
        """Manual HR uses recording_method=3 (MANUAL_ENTRY)."""
        manual_records = parse_hr_manual(str(MINI_ZEPP_DIR / "HEARTRATE.csv"))
        canon, stats = build_heartrate_canonical([], manual_records)
        for r in canon:
            self.assertEqual(r.recording_method, 3)

    def test_cutoff_enforcement(self):
        """Records at/after cutoff are filtered."""
        auto_records = parse_hr_auto(str(MINI_ZEPP_DIR / "HEARTRATE_AUTO.csv"))
        manual_records = parse_hr_manual(str(MINI_ZEPP_DIR / "HEARTRATE.csv"))

        cutoff = datetime.datetime(2025, 2, 1, tzinfo=datetime.timezone.utc)
        cutoff_ms = int(cutoff.timestamp() * 1000)

        canon, stats = build_heartrate_canonical(
            auto_records, manual_records,
            cutoffs={"heart_rate": cutoff_ms}
        )
        for r in canon:
            self.assertLess(r.start_ms, cutoff_ms)


if __name__ == "__main__":
    unittest.main()
