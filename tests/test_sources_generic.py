"""test_sources_generic.py — unit tests for the generic CSV adapter."""

import json
import pathlib
import sys
import tempfile
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.sources.generic_csv import (
    MappingError,
    parse,
    parse_with_mapping,
)


class TestGenericCsvAdapter(unittest.TestCase):
    """Tests for the generic CSV adapter."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="test_generic_")
        cls.tmp = pathlib.Path(cls.tmpdir)

        # ---- happy path CSV ----
        # Columns: Timestamp, Weight (kg), Fat %, Muscle (kg)
        cls.csv_path = cls.tmp / "foofit.csv"
        cls.csv_path.write_text(
            "Timestamp,Weight (kg),Fat %,Muscle (kg)\n"
            "2026-01-15T10:00:00.000Z,75.2,22.1,58.6\n"
            "2026-01-22T10:00:00.000Z,74.8,21.9,58.3\n"
            "2026-02-01T09:30:00.000Z,74.5,21.8,58.1\n",
            encoding="utf-8",
        )

        cls.mapping_path = cls.tmp / "foofit-mapping.json"
        cls.mapping_path.write_text(
            json.dumps({
                "columns": {
                    "time": "Timestamp",
                    "weight_kg": "Weight (kg)",
                    "body_fat": "Fat %",
                    "lean_mass": "Muscle (kg)",
                },
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )

        cls.records = parse(str(cls.mapping_path))

    def test_weight_count(self):
        """Should produce 3 weight records."""
        weight = [r for r in self.records if r.kind == "weight"]
        self.assertEqual(len(weight), 3)

    def test_body_fat_count(self):
        """Should produce 3 body_fat records."""
        bf = [r for r in self.records if r.kind == "body_fat"]
        self.assertEqual(len(bf), 3)

    def test_lean_mass_count(self):
        """Should produce 3 lean_mass records."""
        lm = [r for r in self.records if r.kind == "lean_mass"]
        self.assertEqual(len(lm), 3)

    def test_source_is_generic(self):
        """All records should have source='generic'."""
        for r in self.records:
            self.assertEqual(r.source, "generic")

    def test_kinds_are_valid(self):
        """Records should only be weight, body_fat, or lean_mass."""
        for r in self.records:
            self.assertIn(r.kind, ("weight", "body_fat", "lean_mass"))

    def test_weight_values_kg(self):
        """Weight values should be in kg range."""
        for r in self.records:
            if r.kind == "weight":
                self.assertGreater(r.value, 70)
                self.assertLess(r.value, 80)

    def test_body_fat_values_percent(self):
        """Body fat values should be in percent range."""
        for r in self.records:
            if r.kind == "body_fat":
                self.assertGreater(r.value, 20)
                self.assertLess(r.value, 25)

    def test_timestamps_aware(self):
        """All time_utc values should be timezone-aware."""
        for r in self.records:
            self.assertIsNotNone(r.time_utc.tzinfo)

    def test_ms_computed(self):
        """All records should have ms computed from time_utc."""
        for r in self.records:
            self.assertGreater(r.ms, 0)
            expected_ms = int(r.time_utc.timestamp() * 1000)
            self.assertEqual(r.ms, expected_ms)

    def test_meta_has_row_number(self):
        """Meta should contain the CSV row number."""
        for r in self.records:
            self.assertIn("csv_row", r.meta)

    def test_raw_fields_preserved(self):
        """Raw fields from the CSV should be preserved."""
        for r in self.records:
            self.assertIn("Timestamp", r.raw)

    def test_raw_fields_stripped(self):
        """Raw field values should be stripped (no leading/trailing spaces)."""
        for r in self.records:
            for v in r.raw.values():
                if v:
                    self.assertEqual(v, v.strip())


class TestGenericCsvRefusalCases(unittest.TestCase):
    """Tests that the adapter refuses ambiguous / malformed input."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_generic_refuse_")
        self.tmp = pathlib.Path(self.tmpdir)

    def test_missing_csv_column_raises(self):
        """Mapping that declares a column not in the CSV raises MappingError."""
        csv_path = self.tmp / "x.csv"
        csv_path.write_text("Timestamp,Weight\n2026-01-01,75.0\n", encoding="utf-8")

        mapping_path = self.tmp / "x-mapping.json"
        mapping_path.write_text(
            json.dumps({
                "columns": {
                    "time": "Timestamp",
                    "weight_kg": "Weight",  # correct column name
                },
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )
        # This should succeed because "Weight" IS in the CSV
        records = parse(str(mapping_path))
        self.assertEqual(len(records), 1)

    def test_missing_column_in_csv_raises(self):
        """Mapping that declares a column not in the CSV raises MappingError."""
        csv_path = self.tmp / "x.csv"
        csv_path.write_text("Timestamp,WeightKg\n2026-01-01,75.0\n", encoding="utf-8")

        mapping_path = self.tmp / "x-mapping.json"
        mapping_path.write_text(
            json.dumps({
                "columns": {
                    "time": "Timestamp",
                    "weight_kg": "Weight (kg)",  # not in CSV
                },
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(MappingError) as ctx:
            parse(str(mapping_path))
        self.assertIn("not found in CSV", str(ctx.exception))

    def test_bare_weight_column_raises(self):
        """A bare 'weight' field name (no _kg suffix) is rejected."""
        csv_path = self.tmp / "x.csv"
        csv_path.write_text("Timestamp,Weight\n2026-01-01,75.0\n", encoding="utf-8")

        mapping_path = self.tmp / "x-mapping.json"
        mapping_path.write_text(
            json.dumps({
                "columns": {
                    "time": "Timestamp",
                    "weight": "Weight",  # bare name, ambiguous — should be weight_kg
                },
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(MappingError) as ctx:
            parse(str(mapping_path))
        self.assertIn("Ambiguous column name", str(ctx.exception))
        self.assertIn("weight", str(ctx.exception))

    def test_bad_timestamp_raises_with_row_info(self):
        """Unparseable timestamps produce an error listing offending rows."""
        csv_path = self.tmp / "x.csv"
        csv_path.write_text(
            "Timestamp,Weight (kg)\n"
            "2026-01-01,75.0\n"
            "not-a-date,75.0\n"
            "also-not,74.0\n"
            "2026-01-04,73.0\n"
            "bad-one,72.0\n"
            "still-bad,71.0\n",
            encoding="utf-8",
        )

        mapping_path = self.tmp / "x-mapping.json"
        mapping_path.write_text(
            json.dumps({
                "columns": {
                    "time": "Timestamp",
                    "weight_kg": "Weight (kg)",
                },
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(MappingError) as ctx:
            parse(str(mapping_path))
        # Should report at least one bad row
        self.assertIn("cannot parse time", str(ctx.exception))
        # row_errors should be populated
        self.assertGreater(len(ctx.exception.row_errors), 0)

    def test_bare_fat_column_raises(self):
        """A bare 'fat' field name (no suffix) is rejected — must be body_fat."""
        csv_path = self.tmp / "x.csv"
        csv_path.write_text("Timestamp,Fat\n2026-01-01,22.1\n", encoding="utf-8")

        mapping_path = self.tmp / "x-mapping.json"
        mapping_path.write_text(
            json.dumps({
                "columns": {
                    "time": "Timestamp",
                    "fat": "Fat",  # bare name, ambiguous — should be body_fat
                },
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(MappingError) as ctx:
            parse(str(mapping_path))
        self.assertIn("Ambiguous column name", str(ctx.exception))

    def test_mapping_file_not_found_raises(self):
        """Passing a non-existent mapping path raises MappingError."""
        with self.assertRaises(MappingError) as ctx:
            parse(str(self.tmp / "nonexistent-mapping.json"))
        self.assertIn("Mapping file not found", str(ctx.exception))


class TestGenericCsvRegistry(unittest.TestCase):
    """Smoke test for the adapter registry entry."""

    def test_generic_registered(self):
        """The 'generic' adapter should be registered."""
        from ghc_db_manager.sources import ADAPTERS
        self.assertIn("generic", ADAPTERS)

    def test_generic_loads(self):
        """load_source('generic', ...) should invoke parse."""
        from ghc_db_manager.sources import load_source
        tmpdir = tempfile.mkdtemp(prefix="test_generic_reg_")
        tmp = pathlib.Path(tmpdir)

        csv_path = tmp / "data.csv"
        csv_path.write_text(
            "Timestamp,Weight (kg)\n2026-03-01,72.0\n",
            encoding="utf-8",
        )
        mapping_path = tmp / "data-mapping.json"
        mapping_path.write_text(
            json.dumps({
                "columns": {"time": "Timestamp", "weight_kg": "Weight (kg)"},
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )
        records = load_source("generic", str(mapping_path))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].kind, "weight")


class TestGenericCsvEdgeCases(unittest.TestCase):
    """Edge case handling."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_generic_edge_")
        self.tmp = pathlib.Path(self.tmpdir)

    def test_empty_cells_skipped(self):
        """Empty cells in optional numeric columns are skipped silently."""
        csv_path = self.tmp / "x.csv"
        csv_path.write_text(
            "Timestamp,Weight (kg),Fat %\n"
            "2026-01-01,75.0,\n"   # no body_fat
            "2026-01-02,,22.0\n",   # no weight
            encoding="utf-8",
        )
        mapping_path = self.tmp / "x-mapping.json"
        mapping_path.write_text(
            json.dumps({
                "columns": {
                    "time": "Timestamp",
                    "weight_kg": "Weight (kg)",
                    "body_fat": "Fat %",
                },
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )
        records = parse(str(mapping_path))
        weight = [r for r in records if r.kind == "weight"]
        bf = [r for r in records if r.kind == "body_fat"]
        self.assertEqual(len(weight), 1)
        self.assertEqual(len(bf), 1)

    def test_implausible_values_skipped(self):
        """Values outside plausible range are not emitted."""
        csv_path = self.tmp / "x.csv"
        csv_path.write_text(
            "Timestamp,Weight (kg)\n"
            "2026-01-01,350.0\n"   # out of band
            "2026-01-02,74.0\n",   # OK
            encoding="utf-8",
        )
        mapping_path = self.tmp / "x-mapping.json"
        mapping_path.write_text(
            json.dumps({
                "columns": {"time": "Timestamp", "weight_kg": "Weight (kg)"},
                "time_format": "auto",
                "tz": "UTC",
                "encoding": "utf-8",
                "delimiter": ",",
            }),
            encoding="utf-8",
        )
        records = parse(str(mapping_path))
        weight = [r for r in records if r.kind == "weight"]
        self.assertEqual(len(weight), 1)
        self.assertEqual(weight[0].value, 74.0)


if __name__ == "__main__":
    unittest.main()
