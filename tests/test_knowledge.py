"""test_knowledge.py — unit tests for ghc_db_manager.knowledge"""

import struct
import unittest
from ghc_db_manager import knowledge as kn


class TestDedupeHash(unittest.TestCase):

    def test_instant_returns_24_bytes(self):
        h = kn.dedupe_hash_instant(4, 1, 1786287698619)
        self.assertEqual(len(h), 24)

    def test_instant_matches_struct_pack(self):
        """The instant hash must equal struct.pack('>qqq', app, dev, time)."""
        h = kn.dedupe_hash_instant(4, 1, 1786287698619)
        expected = struct.pack('>qqq', 4, 1, 1786287698619)
        self.assertEqual(h, expected)

    def test_interval_returns_32_bytes(self):
        h = kn.dedupe_hash_interval(6, 1, 1740000000000, 1740100000000)
        self.assertEqual(len(h), 32)

    def test_interval_starts_with_instant_bytes(self):
        """First 24B of interval hash = instant hash of (app, dev, start_ms)."""
        app, dev, start, end = 6, 1, 1740000000000, 1740100000000
        instant = kn.dedupe_hash_instant(app, dev, start)
        interval = kn.dedupe_hash_interval(app, dev, start, end)
        self.assertEqual(interval[:24], instant)
        self.assertEqual(len(interval), 32)
        self.assertEqual(len(interval[24:]), 8)


class TestLocalDateEpochDays(unittest.TestCase):

    def test_formula_positive_offset(self):
        # May 15, 2026 06:55:03 UTC + offset +7200 → local date = 20588 (May 15)
        epoch_ms = 1778828103000
        days = kn.local_date_epoch_days(epoch_ms, 7200)
        self.assertEqual(days, 20588)

    def test_formula_negative_offset(self):
        # Dec 28, 2026 00:00 UTC with UTC-5 offset → local date Dec 27 = day 20814
        # (UTC midnight with UTC-5 = previous day 19:00 local = still Dec 27 in UTC-5)
        epoch_ms = 1798416000000
        days = kn.local_date_epoch_days(epoch_ms, -18000)
        self.assertEqual(days, 20814)


class TestDeterministicUuid(unittest.TestCase):

    def test_stable_across_calls(self):
        key = "zepp-wave2"
        domain = "weight"
        start = 1740000000000
        u1 = kn.deterministic_uuid(key, domain, start)
        u2 = kn.deterministic_uuid(key, domain, start)
        self.assertEqual(u1, u2)

    def test_differs_across_domains(self):
        key = "zepp-wave2"
        start = 1740000000000
        u1 = kn.deterministic_uuid(key, "weight", start)
        u2 = kn.deterministic_uuid(key, "steps", start)
        self.assertNotEqual(u1, u2)

    def test_differs_with_different_start(self):
        key = "zepp-wave2"
        domain = "weight"
        u1 = kn.deterministic_uuid(key, domain, 1740000000000)
        u2 = kn.deterministic_uuid(key, domain, 1750000000000)
        self.assertNotEqual(u1, u2)

    def test_interval_includes_end_ms(self):
        key = "zepp-wave2"
        domain = "sleep"
        start = 1740000000000
        end = 1740100000000
        u1 = kn.deterministic_uuid(key, domain, start, end)
        u2 = kn.deterministic_uuid(key, domain, start, end + 1)
        self.assertNotEqual(u1, u2)

    def test_returns_16_bytes(self):
        b = kn.deterministic_uuid("key", "weight", 1740000000000)
        self.assertEqual(len(b), 16)


class TestRecordTypeIds(unittest.TestCase):

    def test_all_ids_positive_integers(self):
        for name, tid in kn.RECORD_TYPE_IDS.items():
            self.assertIsInstance(tid, int)
            self.assertGreater(tid, 0, f"{name} should have positive id")

    def test_expected_ids(self):
        self.assertEqual(kn.RECORD_TYPE_IDS["steps"], 1)
        self.assertEqual(kn.RECORD_TYPE_IDS["heart_rate"], 11)
        self.assertEqual(kn.RECORD_TYPE_IDS["body_fat"], 17)
        self.assertEqual(kn.RECORD_TYPE_IDS["weight"], 26)
        self.assertEqual(kn.RECORD_TYPE_IDS["lean_body_mass"], 27)
        self.assertEqual(kn.RECORD_TYPE_IDS["exercise_session"], 37)
        self.assertEqual(kn.RECORD_TYPE_IDS["sleep_session"], 38)


class TestSleepStageIds(unittest.TestCase):

    def test_expected_values(self):
        self.assertEqual(kn.SLEEP_STAGE_IDS["AWAKE"], 1)
        self.assertEqual(kn.SLEEP_STAGE_IDS["LIGHT"], 4)
        self.assertEqual(kn.SLEEP_STAGE_IDS["DEEP"], 5)
        self.assertEqual(kn.SLEEP_STAGE_IDS["REM"], 6)


class TestZeppSportMap(unittest.TestCase):

    def test_all_values_are_tuples(self):
        for sport, val in kn.ZEPP_SPORT_MAP.items():
            self.assertIsInstance(val, tuple)
            self.assertEqual(len(val), 2)
            self.assertIsInstance(val[0], int)
            self.assertIsInstance(val[1], str)

    def test_expected_mappings(self):
        self.assertEqual(kn.ZEPP_SPORT_MAP["1"], (56, "Outdoor Running"))
        self.assertEqual(kn.ZEPP_SPORT_MAP["6"], (74, "Pool Swimming"))
        self.assertEqual(kn.ZEPP_SPORT_MAP["22"], (64, "Football"))
        self.assertEqual(kn.ZEPP_SPORT_MAP["140"], (46, "Kayaking"))


class TestConstants(unittest.TestCase):

    def test_known_user_version(self):
        self.assertEqual(kn.KNOWN_USER_VERSION, 23)

    def test_zone_offset_max_seconds(self):
        self.assertEqual(kn.ZONE_OFFSET_MAX_SECONDS, 64800)

    def test_client_record_version(self):
        self.assertEqual(kn.CLIENT_RECORD_VERSION, "0")

    def test_device_unknown_id(self):
        self.assertEqual(kn.DEVICE_UNKNOWN_ID, 1)

    def test_do_not_touch_tables(self):
        self.assertIn("android_metadata", kn.DO_NOT_TOUCH_TABLES)
        self.assertIn("change_logs_table", kn.DO_NOT_TOUCH_TABLES)
        self.assertIn("access_logs_table", kn.DO_NOT_TOUCH_TABLES)

    def test_generated_columns_includes_local_date_time(self):
        self.assertIn("local_date_time", kn.GENERATED_COLUMNS)

    def test_recording_method_values(self):
        self.assertEqual(kn.RECORDING_METHOD["UNKNOWN"], 0)
        self.assertEqual(kn.RECORDING_METHOD["ACTIVELY_RECORDED"], 1)
        self.assertEqual(kn.RECORDING_METHOD["AUTOMATICALLY_RECORDED"], 2)
        self.assertEqual(kn.RECORDING_METHOD["MANUAL_ENTRY"], 3)


if __name__ == "__main__":
    unittest.main()
