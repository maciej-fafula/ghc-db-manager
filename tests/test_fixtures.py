"""test_fixtures.py — tests for Phase B synthetic fixtures.

These tests verify:
  1. The fixture DB builds without errors and has the correct schema (user_version=23).
  2. Seeded rows satisfy local_date formula and zone-offset bounds.
  3. A seeded weight row's dedupe_hash matches the knowledge formula.
  4. mini-libra.csv parses to the expected row count.
  5. mini-zepp/BODY.csv has expected row counts per height (two persons).
  6. poison.json loads and has >= 6 cases.
  7. Privacy guard: no fixture file contains real PoC markers.
"""

import csv
import json
import os
import pathlib
import sqlite3
import struct
import tempfile
import unittest

# Path to fixtures directory
FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
MINI_ZEPP_DIR = FIXTURES_DIR / "mini-zepp"

# Import knowledge helpers (add src to path for test runner)
import sys
_SRC = pathlib.Path(__file__).parent.parent / "src"
sys.path.insert(0, str(_SRC))
from ghc_db_manager import knowledge as kn


class TestFixtureDb(unittest.TestCase):
    """Tests for the synthetic HC-schema fixture database."""

    @classmethod
    def setUpClass(cls):
        # Build the fixture DB in a temp file
        cls._db_path = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._db_path.close()
        # Import and build
        from tests.fixtures.make_fixture_db import build
        build(cls._db_path.name)
        cls.conn = sqlite3.connect(cls._db_path.name)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        pathlib.Path(cls._db_path.name).unlink(missing_ok=True)

    def test_user_version_is_23(self):
        """PRAGMA user_version must be 23 (KNOWN_USER_VERSION)."""
        uv = self.conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(uv, kn.KNOWN_USER_VERSION)

    def test_weight_rows_have_valid_zone_offset(self):
        """All zone_offset values must be within ±64800 seconds."""
        bad = self.conn.execute(
            "SELECT COUNT(*) FROM weight_record_table "
            "WHERE zone_offset NOT BETWEEN -64800 AND 64800"
        ).fetchone()[0]
        self.assertEqual(bad, 0)

    def test_interval_rows_have_valid_zone_offsets(self):
        """start_zone_offset and end_zone_offset must be within ±64800 seconds."""
        for tbl in ["steps_record_table", "distance_record_table",
                    "total_calories_burned_record_table", "sleep_session_record_table",
                    "heart_rate_record_table", "exercise_session_record_table"]:
            bad = self.conn.execute(
                f"SELECT COUNT(*) FROM {tbl} "
                f"WHERE start_zone_offset NOT BETWEEN -64800 AND 64800 "
                f"OR end_zone_offset NOT BETWEEN -64800 AND 64800"
            ).fetchone()[0]
            self.assertEqual(bad, 0, f"{tbl} has out-of-range zone offsets")

    def test_weight_local_date_formula(self):
        """weight_record_table local_date must match knowledge.local_date_epoch_days."""
        rows = self.conn.execute(
            "SELECT time, zone_offset, local_date FROM weight_record_table"
        ).fetchall()
        self.assertGreater(len(rows), 0, "weight table is empty")
        for time_ms, zone_off, local_date in rows:
            expected = kn.local_date_epoch_days(time_ms, zone_off)
            self.assertEqual(
                local_date, expected,
                f"weight local_date mismatch: time={time_ms}, off={zone_off}, "
                f"stored={local_date}, expected={expected}"
            )

    def test_steps_local_date_formula(self):
        """steps_record_table local_date must match formula for interval records."""
        rows = self.conn.execute(
            "SELECT start_time, start_zone_offset, local_date FROM steps_record_table"
        ).fetchall()
        self.assertGreater(len(rows), 0, "steps table is empty")
        for start_ms, start_off, local_date in rows:
            expected = kn.local_date_epoch_days(start_ms, start_off)
            self.assertEqual(
                local_date, expected,
                f"steps local_date mismatch: start={start_ms}, off={start_off}, "
                f"stored={local_date}, expected={expected}"
            )

    def test_weight_dedupe_hash_matches_formula(self):
        """Dedupe hash of the first weight row must equal dedupe_hash_instant."""
        row = self.conn.execute(
            "SELECT app_info_id, device_info_id, time, dedupe_hash "
            "FROM weight_record_table LIMIT 1"
        ).fetchone()
        app, dev, time_ms, stored_hash = row
        expected_hash = kn.dedupe_hash_instant(app, dev, time_ms)
        self.assertEqual(
            stored_hash, expected_hash,
            "weight dedupe_hash does not match knowledge.dedupe_hash_instant"
        )
        # Also verify it's exactly 24 bytes
        self.assertEqual(len(stored_hash), 24)

    def test_steps_dedupe_hash_matches_formula(self):
        """Dedupe hash of the first steps row must equal dedupe_hash_interval."""
        row = self.conn.execute(
            "SELECT app_info_id, device_info_id, start_time, end_time, dedupe_hash "
            "FROM steps_record_table LIMIT 1"
        ).fetchone()
        app, dev, start_ms, end_ms, stored_hash = row
        expected_hash = kn.dedupe_hash_interval(app, dev, start_ms, end_ms)
        self.assertEqual(
            stored_hash, expected_hash,
            "steps dedupe_hash does not match knowledge.dedupe_hash_interval"
        )
        self.assertEqual(len(stored_hash), 32)

    def test_all_uuids_unique(self):
        """Every record table must have unique uuids."""
        for tbl, _, _, _, _ in [
            ("weight_record_table", "time", None, 26, False),
            ("steps_record_table", "start_time", "count", 1, True),
            ("sleep_session_record_table", "start_time", None, 38, True),
            ("heart_rate_record_table", "start_time", None, 11, True),
            ("exercise_session_record_table", "start_time", None, 37, True),
        ]:
            n, d = self.conn.execute(
                f"SELECT COUNT(*), COUNT(DISTINCT uuid) FROM {tbl}"
            ).fetchone()
            self.assertEqual(
                n, d,
                f"{tbl}: {n} rows but only {d} distinct uuids"
            )

    def test_seed_data_present(self):
        """Fixture DB must have seeded rows in each domain."""
        expected = {
            "weight_record_table": 5,
            "body_fat_record_table": 3,
            "lean_body_mass_record_table": 2,
            "steps_record_table": 6,
            "distance_record_table": 2,
            "total_calories_burned_record_table": 2,
            "sleep_session_record_table": 2,
            "heart_rate_record_table": 2,
            "exercise_session_record_table": 4,
            "heart_rate_record_series_table": 3,
            "sleep_stages_table": 2,
        }
        for tbl, min_count in expected.items():
            n = self.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            self.assertGreaterEqual(
                n, min_count,
                f"{tbl}: expected >={min_count} rows, got {n}"
            )

    def test_two_apps_seeded(self):
        """application_info_table must contain 2 known apps."""
        rows = self.conn.execute(
            "SELECT row_id, package_name FROM application_info_table"
        ).fetchall()
        self.assertGreaterEqual(len(rows), 2, "need at least 2 apps")
        pkg_names = {r[1] for r in rows}
        self.assertIn("com.example.tracker", pkg_names)
        self.assertIn("com.example.watch", pkg_names)

    def test_device_unknown_present(self):
        """device_info_table row_id=1 must be the unknown device."""
        row = self.conn.execute(
            "SELECT manufacturer, model FROM device_info_table WHERE row_id=1"
        ).fetchone()
        self.assertIsNotNone(row, "device row_id=1 not found")
        # Unknown device has NULL manufacturer/model
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])

    def test_heart_rate_series_samples(self):
        """heart_rate_record_table must have a session with at least 3 series samples."""
        # Find a session with series
        sessions_with_samples = self.conn.execute(
            """SELECT COUNT(*) FROM heart_rate_record_table h
               JOIN heart_rate_record_series_table s ON h.row_id = s.parent_key
               GROUP BY h.row_id
               HAVING COUNT(*) >= 3"""
        ).fetchall()
        self.assertGreater(
            len(sessions_with_samples), 0,
            "no heart_rate session with >= 3 series samples found"
        )

    def test_sleep_stages_have_two_stages(self):
        """sleep_session_record_table must have a session with exactly 2 stages."""
        sessions_with_2_stages = self.conn.execute(
            """SELECT COUNT(*) FROM sleep_session_record_table s
               JOIN sleep_stages_table t ON s.row_id = t.parent_key
               GROUP BY s.row_id
               HAVING COUNT(*) = 2"""
        ).fetchone()
        self.assertIsNotNone(
            sessions_with_2_stages,
            "no sleep session with exactly 2 stages found"
        )

    def test_steps_cutoff_invariant(self):
        """At least one steps row must be at or after the cutoff for invariant testing."""
        CUTOFF_MS = 1773792000000  # 2026-03-18T00:00:00Z
        at_or_after = self.conn.execute(
            "SELECT COUNT(*) FROM steps_record_table WHERE start_time >= ?",
            (CUTOFF_MS,)
        ).fetchone()[0]
        self.assertGreaterEqual(
            at_or_after, 1,
            "fixture needs at least 1 steps row at/after cutoff for invariant tests"
        )


class TestMiniLibra(unittest.TestCase):
    """Tests for the mini-libra.csv synthetic fixture."""

    def test_mini_libra_row_count(self):
        """mini-libra.csv must have exactly 15 data rows."""
        path = FIXTURES_DIR / "mini-libra.csv"
        self.assertTrue(path.exists(), f"{path} not found")

        with open(path, encoding="utf-8-sig") as f:
            lines = [l.rstrip() for l in f]

        # Skip comment lines (start with #)
        data_lines = [l for l in lines if l and not l.startswith("#")]
        self.assertEqual(
            len(data_lines), 15,
            f"expected 15 data rows, got {len(data_lines)}"
        )

    def test_mini_libra_semicolon_delimiter(self):
        """Data lines must use semicolons as delimiter."""
        path = FIXTURES_DIR / "mini-libra.csv"
        with open(path, encoding="utf-8-sig") as f:
            lines = [l.rstrip() for l in f if l.strip() and not l.startswith("#")]

        for line in lines[:3]:
            self.assertIn(";", line, "Libra uses semicolons as delimiter")

    def test_mini_libra_comment_lines(self):
        """File must have #Version: and #Units: comment lines."""
        path = FIXTURES_DIR / "mini-libra.csv"
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
        self.assertIn("#Version:", content)
        self.assertIn("#Units:", content)

    def test_mini_libra_two_dst_variants(self):
        """File must have at least one 22:00:00.000Z (summer) and one 23:00:00.000Z (winter)."""
        path = FIXTURES_DIR / "mini-libra.csv"
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
        has_summer = "T22:00:00.000Z" in content
        has_winter = "T23:00:00.000Z" in content
        self.assertTrue(has_summer, "no summer-time 22:00Z entry found")
        self.assertTrue(has_winter, "no winter-time 23:00Z entry found")

    def test_mini_libra_year_gap(self):
        """There must be a gap between years in the data."""
        path = FIXTURES_DIR / "mini-libra.csv"
        with open(path, encoding="utf-8-sig") as f:
            raw_lines = [l.rstrip() for l in f]
        # Match PoC parsing: strip leading '#' from comment lines, skip Version/Units
        lines = [
            l[1:] if l.startswith("#") else l
            for l in raw_lines
            if l.strip() and not l.startswith("#Version") and not l.startswith("#Units")
        ]
        reader = csv.DictReader(lines, delimiter=";")
        years = []
        for row in reader:
            year = int(row["date"][:4])
            years.append(year)
        gaps = [years[i+1] - years[i] for i in range(len(years)-1)]
        self.assertGreater(max(gaps), 1, "no year gap found in Libra data")

    def test_mini_libra_has_body_fat_and_without(self):
        """Some rows must have body fat/muscle mass filled; others must be weight-only."""
        path = FIXTURES_DIR / "mini-libra.csv"
        with open(path, encoding="utf-8-sig") as f:
            raw_lines = [l.rstrip() for l in f]
        # Match PoC parsing: strip leading '#' from comment lines, skip Version/Units
        lines = [
            l[1:] if l.startswith("#") else l
            for l in raw_lines
            if l.strip() and not l.startswith("#Version") and not l.startswith("#Units")
        ]
        reader = csv.DictReader(lines, delimiter=";")
        rows_with_bf = 0
        rows_without_bf = 0
        for row in reader:
            if row["body fat"].strip():
                rows_with_bf += 1
            else:
                rows_without_bf += 1
        self.assertGreater(rows_with_bf, 0, "no rows with body fat found")
        self.assertGreater(rows_without_bf, 0, "no rows without body fat found")


class TestMiniZepp(unittest.TestCase):
    """Tests for mini-zepp/ synthetic fixtures."""

    def test_body_row_count(self):
        """BODY.csv must have 14 data rows."""
        path = MINI_ZEPP_DIR / "BODY.csv"
        self.assertTrue(path.exists(), f"{path} not found")
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 14)

    def test_body_two_heights(self):
        """BODY.csv must have two distinct heights (two persons)."""
        path = MINI_ZEPP_DIR / "BODY.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            heights = {float(row["height"]) for row in reader}
        self.assertEqual(
            len(heights), 2,
            f"expected 2 distinct heights, got {heights}"
        )
        self.assertIn(175.0, heights)
        self.assertIn(160.0, heights)

    def test_body_height_175_count(self):
        """Height 175.0 rows should be the majority (person 1)."""
        path = MINI_ZEPP_DIR / "BODY.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            h175 = [row for row in reader if float(row["height"]) == 175.0]
        self.assertGreaterEqual(
            len(h175), 9,
            f"expected at least 9 rows for height 175.0, got {len(h175)}"
        )

    def test_body_height_160_count(self):
        """Height 160.0 rows should be exactly 3 (person 2)."""
        path = MINI_ZEPP_DIR / "BODY.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            h160 = [row for row in reader if float(row["height"]) == 160.0]
        self.assertEqual(
            len(h160), 3,
            f"expected exactly 3 rows for height 160.0, got {len(h160)}"
        )

    def test_body_implausible_outlier(self):
        """BODY.csv must have an implausible weight row (~3.x kg)."""
        path = MINI_ZEPP_DIR / "BODY.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            outliers = [row for row in reader if 2.0 <= float(row["weight"]) <= 5.0]
        self.assertGreaterEqual(
            len(outliers), 1,
            "no implausible 3.x kg weight outlier found"
        )

    def test_body_null_literal(self):
        """BODY.csv must contain literal 'null' strings in fatRate or muscleRate."""
        path = MINI_ZEPP_DIR / "BODY.csv"
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
        self.assertIn("null", content, "no literal 'null' found in BODY.csv")

    def test_body_duplicate_timestamp(self):
        """BODY.csv must have a duplicate timestamp pair where one row is richer."""
        path = MINI_ZEPP_DIR / "BODY.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        from collections import Counter
        times = Counter(row["time"] for row in rows)
        dupes = {t for t, c in times.items() if c > 1}
        self.assertGreaterEqual(
            len(dupes), 1,
            f"no duplicate timestamp found in BODY.csv. times={times}"
        )
        # Verify at least one duplicate pair has different fatRate/muscleRate
        dupe_rows = [row for row in rows if row["time"] in dupes]
        fatRates = {r["fatRate"] for r in dupe_rows}
        self.assertGreater(
            len(fatRates), 1,
            "duplicate timestamp rows must differ in fatRate richness"
        )

    def test_activity_row_count(self):
        """ACTIVITY.csv must have 10 data rows."""
        path = MINI_ZEPP_DIR / "ACTIVITY.csv"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 10)

    def test_activity_has_all_zero_day(self):
        """ACTIVITY.csv must have at least one all-zero day."""
        path = MINI_ZEPP_DIR / "ACTIVITY.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            zero_days = [
                row for row in reader
                if int(row["steps"]) == 0 and int(row["distance"]) == 0 and int(row["calories"]) == 0
            ]
        self.assertGreaterEqual(
            len(zero_days), 1,
            "no all-zero activity day found"
        )

    def test_sleep_row_count(self):
        """SLEEP.csv must have 8 data rows."""
        path = MINI_ZEPP_DIR / "SLEEP.csv"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 8)

    def test_sleep_placeholder_rows(self):
        """SLEEP.csv must have at least 2 placeholder rows where start == stop."""
        path = MINI_ZEPP_DIR / "SLEEP.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            placeholders = [
                row for row in reader
                if row["start"] == row["stop"]
            ]
        self.assertGreaterEqual(
            len(placeholders), 2,
            f"expected at least 2 placeholder rows, got {len(placeholders)}"
        )

    def test_sleep_minute_row_count(self):
        """SLEEP_MINUTE.csv must have at least 35 rows across ~3 nights."""
        path = MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertGreaterEqual(
            len(rows), 35,
            f"expected >= 35 rows, got {len(rows)}"
        )

    def test_sleep_minute_evening_times(self):
        """SLEEP_MINUTE.csv must have entries with time >= 20:00."""
        path = MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            evening = [
                row for row in reader
                if int(row["time"].split(":")[0]) >= 20
            ]
        self.assertGreaterEqual(
            len(evening), 1,
            "no evening entries (>=20:00) found in SLEEP_MINUTE.csv"
        )

    def test_sleep_minute_all_four_stages(self):
        """SLEEP_MINUTE.csv must contain all four stage types: LIGHT, DEEP, REM, WAKE."""
        path = MINI_ZEPP_DIR / "SLEEP_MINUTE.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            stages = {row["stage"] for row in reader}
        expected = {"LIGHT", "DEEP", "REM", "WAKE"}
        missing = expected - stages
        self.assertEqual(
            len(missing), 0,
            f"missing stage types in SLEEP_MINUTE.csv: {missing}"
        )

    def test_heartrate_auto_row_count(self):
        """HEARTRATE_AUTO.csv must have at least 30 rows (2 days × ~15 samples)."""
        path = MINI_ZEPP_DIR / "HEARTRATE_AUTO.csv"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertGreaterEqual(
            len(rows), 30,
            f"expected >= 30 rows, got {len(rows)}"
        )

    def test_heartrate_row_count(self):
        """HEARTRATE.csv must have 6 data rows."""
        path = MINI_ZEPP_DIR / "HEARTRATE.csv"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 6)

    def test_heartrate_duplicate_timestamp(self):
        """HEARTRATE.csv must have at least one duplicate timestamp."""
        path = MINI_ZEPP_DIR / "HEARTRATE.csv"
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        from collections import Counter
        times = Counter(row["time"] for row in rows)
        dupes = {t for t, c in times.items() if c > 1}
        self.assertGreaterEqual(
            len(dupes), 1,
            "no duplicate timestamp found in HEARTRATE.csv"
        )

    def test_sport_row_count(self):
        """SPORT.csv must have 6 data rows."""
        path = MINI_ZEPP_DIR / "SPORT.csv"
        self.assertTrue(path.exists())
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 6)

    def test_sport_covers_required_types(self):
        """SPORT.csv must cover sport types 1, 6, 16, 52, 76 (and optionally 140)."""
        path = MINI_ZEPP_DIR / "SPORT.csv"
        required_types = {"1", "6", "16", "52", "76"}
        with open(path, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            types = {row["type"] for row in reader}
        missing = required_types - types
        self.assertEqual(
            len(missing), 0,
            f"sport types missing from SPORT.csv: {missing}"
        )


class TestPoisonJson(unittest.TestCase):
    """Tests for poison.json — cases that must be rejected."""

    def test_poison_json_loads(self):
        """poison.json must be valid JSON."""
        path = FIXTURES_DIR / "poison.json"
        self.assertTrue(path.exists(), f"{path} not found")
        with open(path) as f:
            cases = json.load(f)
        self.assertIsInstance(cases, list)
        self.assertGreaterEqual(
            len(cases), 6,
            f"poison.json must have at least 6 cases, got {len(cases)}"
        )

    def test_poison_cases_have_required_fields(self):
        """Each poison case must have name, description, and payload."""
        path = FIXTURES_DIR / "poison.json"
        with open(path) as f:
            cases = json.load(f)
        for case in cases:
            self.assertIn("name", case)
            self.assertIn("description", case)
            self.assertIn("payload", case)

    def test_poison_zone_offset_in_ms(self):
        """Must have a case with zone_offset in milliseconds (7200000)."""
        path = FIXTURES_DIR / "poison.json"
        with open(path) as f:
            cases = json.load(f)
        found = False
        for c in cases:
            if c["name"] == "zone_offset_milliseconds":
                found = True
                # payload should have zone_offset = 7200000
                vals = c["payload"]["values"]
                self.assertEqual(vals.get("zone_offset"), 7200000)
                break
        self.assertTrue(found, "zone_offset_milliseconds case not found")

    def test_poison_cutoff_violation(self):
        """Must have a case with a record at or after the cutoff timestamp."""
        path = FIXTURES_DIR / "poison.json"
        with open(path) as f:
            cases = json.load(f)
        names = {c["name"] for c in cases}
        self.assertIn("record_at_cutoff_timestamp", names)
        self.assertIn("record_after_cutoff", names)

    def test_poison_duplicate_uuid(self):
        """Must have a case with duplicate uuid entries."""
        path = FIXTURES_DIR / "poison.json"
        with open(path) as f:
            cases = json.load(f)
        found = any(c["name"] == "duplicate_uuid" for c in cases)
        self.assertTrue(found, "duplicate_uuid case not found")

    def test_poison_duplicate_dedupe_hash(self):
        """Must have a case with duplicate dedupe_hash entries."""
        path = FIXTURES_DIR / "poison.json"
        with open(path) as f:
            cases = json.load(f)
        found = any(c["name"] == "duplicate_dedupe_hash" for c in cases)
        self.assertTrue(found, "duplicate_dedupe_hash case not found")

    def test_poison_change_logs_table(self):
        """Must have a case attempting to write to change_logs_table."""
        path = FIXTURES_DIR / "poison.json"
        with open(path) as f:
            cases = json.load(f)
        found = any(c["name"] == "write_to_change_logs_table" for c in cases)
        self.assertTrue(found, "write_to_change_logs_table case not found")

    def test_poison_local_date_time_column(self):
        """Must have a case attempting to write to the generated local_date_time column."""
        path = FIXTURES_DIR / "poison.json"
        with open(path) as f:
            cases = json.load(f)
        found = any(c["name"] == "write_to_local_date_time_column" for c in cases)
        self.assertTrue(found, "write_to_local_date_time_column case not found")


class TestPrivacyGuard(unittest.TestCase):
    """Privacy tests: fixture files must not contain real PoC markers."""

    # Blocklist of strings that must NOT appear in any fixture file.
    # These are drawn from the real PoC data/scripts.
    REAL_MARKERS = [
        "cachapa",       # real PoC weight-app name
        "huami",         # Zepp/Huami brand in real data
        "openscale",     # openScale app name in PoC
        "Libra_2026",    # real Libra export filename
        "1786287698619", # real epoch timestamp from PoC merge_weight.py
        "1787404253776", # real timestamp from Zepp BODY filename
        "1787404250266", # real timestamp from Zepp SLEEP filename
        "1787404251541", # real timestamp from Zepp SLEEP_MINUTE filename
        "1787404253310", # real timestamp from Zepp HEARTRATE_AUTO filename
        "1787404252098", # real timestamp from Zepp HEARTRATE filename
        "1787404254426", # real timestamp from Zepp SPORT filename
    ]

    @classmethod
    def _all_fixture_files(cls):
        """Yield all fixture file paths."""
        if FIXTURES_DIR.exists():
            for p in FIXTURES_DIR.rglob("*"):
                if p.is_file() and p.suffix in (".csv", ".json", ".py"):
                    yield p
        # Also scan mini-zepp subdirectory explicitly
        if MINI_ZEPP_DIR.exists():
            for p in MINI_ZEPP_DIR.iterdir():
                if p.is_file():
                    yield p

    def test_no_real_markers_in_fixtures(self):
        """No fixture file may contain any real PoC marker string."""
        violations = []
        for path in self._all_fixture_files():
            if path.name == "make_fixture_db.py":
                continue  # allow the builder script (not a fixture data file)
            try:
                with open(path, "rb") as f:
                    content = f.read().decode("utf-8", errors="ignore")
            except Exception:
                continue
            for marker in self.REAL_MARKERS:
                if marker in content:
                    violations.append(f"{path.name}: contains {marker!r}")
        self.assertEqual(
            len(violations), 0,
            "Privacy violation — fixture files contain real PoC markers:\n  " +
            "\n  ".join(violations)
        )

    def test_no_real_export_filename(self):
        """Fixture files must not reference the real HC export filename."""
        path = FIXTURES_DIR / "mini-libra.csv"
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
        self.assertNotIn("Libra_2026", content)


if __name__ == "__main__":
    unittest.main()
