"""test_domains_exercise.py — tests for exercise domain."""

import datetime
import pathlib
import sys
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
MINI_ZEPP_DIR = FIXTURES_DIR / "mini-zepp"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.sources.zepp import parse_sport
from ghc_db_manager.domains.exercise import build_exercise_canonical


class TestExerciseParsing(unittest.TestCase):
    """Test parse_sport."""

    def test_parse_sport_returns_intervals(self):
        """parse_sport returns IntervalRecords with kind=exercise."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        # SPORT.csv has 6 workout rows
        self.assertEqual(len(records), 6)
        for r in records:
            self.assertEqual(r.kind, "exercise")

    def test_exercise_end_is_start_plus_duration(self):
        """end = start + sportTime(s)."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        for r in records:
            dur_s = int(r.raw.get("sportTime(s)", "0"))
            expected_end = r.start_utc + datetime.timedelta(seconds=dur_s)
            self.assertEqual(r.end_utc, expected_end)

    def test_exercise_type_mapping(self):
        """Type IDs are mapped via ZEPP_SPORT_MAP."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        # SPORT.csv has types: 1, 6, 16, 52, 76, 140
        types_seen = set()
        for r in records:
            types_seen.add(r.extra.get("exercise_type"))
        # All 6 types should be mapped (exercise_type may be 0 for "Free Training")
        self.assertEqual(len(types_seen), 6)
        for t in types_seen:
            self.assertIsInstance(t, int)

    def test_exercise_title_preserved(self):
        """Title is preserved from ZEPP_SPORT_MAP."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        for r in records:
            title = r.extra.get("title") or ""
            self.assertIsInstance(title, str)
            self.assertGreater(len(title), 0)

    def test_has_route_is_zero(self):
        """has_route is always 0 (no GPS data)."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        for r in records:
            self.assertEqual(r.extra.get("has_route"), 0)

    def test_no_distance_calorie_records_emitted(self):
        """Double-count guard: exercise records have no distance/calorie extra fields."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        for r in records:
            # These should not exist in the extra dict
            self.assertNotIn("distance", r.extra)
            self.assertNotIn("calories", r.extra)
            self.assertNotIn("energy", r.extra)


class TestExerciseCanonical(unittest.TestCase):
    """Test build_exercise_canonical."""

    def test_recording_method_is_active(self):
        """Exercise records use recording_method=1 (ACTIVELY_RECORDED)."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        canon, stats = build_exercise_canonical(records)
        for r in canon:
            self.assertEqual(r.recording_method, 1)

    def test_exercise_type_values(self):
        """exercise_type values are from ZEPP_SPORT_MAP."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        canon, stats = build_exercise_canonical(records)
        for r in canon:
            self.assertIsInstance(r.exercise_type, int)
            self.assertGreaterEqual(r.exercise_type, 0)

    def test_cutoff_enforcement(self):
        """Records at/after cutoff are filtered."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        cutoff = datetime.datetime(2025, 3, 1, tzinfo=datetime.timezone.utc)
        cutoff_ms = int(cutoff.timestamp() * 1000)
        canon, stats = build_exercise_canonical(records, cutoffs={"exercise": cutoff_ms})
        for r in canon:
            self.assertLess(r.start_ms, cutoff_ms)

    def test_all_fixture_types_mapped(self):
        """All 6 fixture sport types are mapped."""
        records = parse_sport(str(MINI_ZEPP_DIR / "SPORT.csv"))
        canon, stats = build_exercise_canonical(records)
        # Types: 1 (Outdoor Running→56), 6 (Pool Swimming→74), 16 (Free Training→0),
        # 52 (Strength Training→70), 76 (Dance→16), 140 (Kayaking→46)
        types = {r.exercise_type for r in canon}
        self.assertEqual(len(types), 6)


if __name__ == "__main__":
    unittest.main()
