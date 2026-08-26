"""test_timeutil.py — unit tests for ghc_db_manager.timeutil"""

import datetime
import unittest
from ghc_db_manager import timeutil


class TestZoneOffsetSeconds(unittest.TestCase):

    def test_warsaw_winter_January(self):
        """Europe/Warsaw in January is UTC+1 (CET)."""
        dt = datetime.datetime(2026, 1, 15, 12, 0, tzinfo=datetime.timezone.utc)
        off = timeutil.zone_offset_seconds(dt, "Europe/Warsaw")
        self.assertEqual(off, 3600)  # +01:00

    def test_warsaw_summer_July(self):
        """Europe/Warsaw in July is UTC+2 (CEST)."""
        dt = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)
        off = timeutil.zone_offset_seconds(dt, "Europe/Warsaw")
        self.assertEqual(off, 7200)  # +02:00

    def test_warsaw_dst_transition_spring_forward(self):
        """Last Sunday March 01:00 UTC clocks spring forward to 02:00 local = UTC+2.
        At 00:30 UTC (before transition) offset is +1; at 01:30 UTC (after) +2.
        """
        # 01:30 UTC on 2026-03-29 (last Sunday March) → already in CEST (+2)
        dt_after = datetime.datetime(2026, 3, 29, 1, 30, tzinfo=datetime.timezone.utc)
        off_after = timeutil.zone_offset_seconds(dt_after, "Europe/Warsaw")
        self.assertEqual(off_after, 7200)  # +02:00 CEST

        # 00:30 UTC on same day → still in CET (+1)
        dt_before = datetime.datetime(2026, 3, 29, 0, 30, tzinfo=datetime.timezone.utc)
        off_before = timeutil.zone_offset_seconds(dt_before, "Europe/Warsaw")
        self.assertEqual(off_before, 3600)  # +01:00 CET

    def test_warsaw_dst_transition_autumn_back(self):
        """Last Sunday October 01:00 UTC clocks fall back to 00:00 local = UTC+1.
        At 00:30 UTC (before) offset is +2; at 02:30 UTC (after) +1.
        """
        # 00:30 UTC on 2026-10-25 (last Sunday October) → still in CEST (+2)
        dt_before = datetime.datetime(2026, 10, 25, 0, 30, tzinfo=datetime.timezone.utc)
        off_before = timeutil.zone_offset_seconds(dt_before, "Europe/Warsaw")
        self.assertEqual(off_before, 7200)  # +02:00 CEST

        # 02:30 UTC on same day → already in CET (+1)
        dt_after = datetime.datetime(2026, 10, 25, 2, 30, tzinfo=datetime.timezone.utc)
        off_after = timeutil.zone_offset_seconds(dt_after, "Europe/Warsaw")
        self.assertEqual(off_after, 3600)  # +01:00 CET

    def test_utc_offset(self):
        """UTC timezone always returns 0."""
        dt = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)
        off = timeutil.zone_offset_seconds(dt, "UTC")
        self.assertEqual(off, 0)

    def test_new_york_summer(self):
        """America/New_York in August is UTC-4 (EDT)."""
        dt = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.timezone.utc)
        off = timeutil.zone_offset_seconds(dt, "America/New_York")
        self.assertEqual(off, -14400)  # -04:00

    def test_unknown_timezone_raises(self):
        dt = datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc)
        with self.assertRaisesRegex(ValueError, "Unknown IANA timezone"):
            timeutil.zone_offset_seconds(dt, "Not/A/RealTZ")

    def test_naive_datetime_assumed_utc(self):
        """A naive datetime is treated as UTC (no error raised)."""
        dt = datetime.datetime(2026, 7, 15, 12, 0)  # naive
        off = timeutil.zone_offset_seconds(dt, "Europe/Warsaw")
        # Naive datetime → treated as UTC; zone offset for 2026-07-15 12:00 UTC is +2
        self.assertEqual(off, 7200)


class TestLocalMidnightAsUtc(unittest.TestCase):

    def test_summer_date_warsaw(self):
        """2026-07-15 local midnight in Warsaw = 2026-07-14 22:00Z."""
        result = timeutil.local_midnight_as_utc(datetime.date(2026, 7, 15), "Europe/Warsaw")
        expected = datetime.datetime(2026, 7, 14, 22, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(result, expected)

    def test_winter_date_warsaw(self):
        """2026-01-15 local midnight in Warsaw = 2026-01-14 23:00Z."""
        result = timeutil.local_midnight_as_utc(datetime.date(2026, 1, 15), "Europe/Warsaw")
        expected = datetime.datetime(2026, 1, 14, 23, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(result, expected)

    def test_utc_midnight(self):
        """UTC midnight on any date = UTC midnight."""
        result = timeutil.local_midnight_as_utc(datetime.date(2026, 7, 15), "UTC")
        expected = datetime.datetime(2026, 7, 15, 0, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(result, expected)

    def test_unknown_timezone_raises(self):
        with self.assertRaisesRegex(ValueError, "Unknown IANA timezone"):
            timeutil.local_midnight_as_utc(datetime.date(2026, 7, 15), "Fake/Zone")

    def test_january_15_2026_warsaw(self):
        """Reference check: 2026-01-15 Warsaw midnight = 2026-01-14 23:00Z."""
        result = timeutil.local_midnight_as_utc(datetime.date(2026, 1, 15), "Europe/Warsaw")
        expected = datetime.datetime(2026, 1, 14, 23, 0, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
