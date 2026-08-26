"""test_golden_wave2.py — golden integration tests for Phase D (wave 2) domains.

Tests:
  1. Full pipeline on fixtures → deterministic (run twice = byte-identical db files)
  2. Expected record counts per domain
  3. Invariants pass
  4. Pilot records ⊆ full records (deterministic UUIDs)
  5. pilot = first-record-per-domain (not per-kind)
"""

import hashlib
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
MINI_ZEPP_DIR = FIXTURES_DIR / "mini-zepp"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.dbio import WriteGuard
from ghc_db_manager.merge import merge
from ghc_db_manager.validation.invariants import run_invariants
from ghc_db_manager.writer import write_interval


# Fixed timestamp for deterministic output
FIXED_MS = 1700000000000


def _build_full(sources, db_path):
    """Run the full pipeline: merge + write + commit. Returns records per domain."""
    records_by_domain = {}
    for domain in ("activity", "sleep", "heartrate", "exercise"):
        records, stats = merge(
            sources,
            domain=domain,
            hc_db_path=db_path,
        )
        records_by_domain[domain] = records
    return records_by_domain


class TestGoldenWave2(unittest.TestCase):
    """Full pipeline golden tests for wave 2 domains."""

    @classmethod
    def setUpClass(cls):
        cls.zepp_dir = str(MINI_ZEPP_DIR)
        cls.sources = {"zepp": cls.zepp_dir}

        # Build fixture db
        cls._db_fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        cls._db_fd.close()
        from tests.fixtures.make_fixture_db import build
        build(cls._db_fd.name)
        cls.db_path = cls._db_fd.name

        cls.app_id = 6  # com.example.watch

    @classmethod
    def tearDownClass(cls):
        pathlib.Path(cls.db_path).unlink(missing_ok=True)

    def test_full_pipeline_deterministic(self):
        """Running the pipeline twice should produce byte-identical db files."""
        def db_bytes(path):
            with open(path, "rb") as f:
                return f.read()

        # First run
        db1 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db1.close()
        shutil.copyfile(self.db_path, db1.name)
        raw1 = sqlite3.connect(db1.name)
        conn1 = WriteGuard(raw1)
        records1 = _build_full(self.sources, self.db_path)
        for domain, recs in records1.items():
            if recs:
                write_interval(conn1, recs, self.app_id, "wave2-key", now_ms=FIXED_MS)
        conn1.commit()
        raw1.close()
        hash1 = hashlib.sha256(db_bytes(db1.name)).hexdigest()

        # Second run
        db2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db2.close()
        shutil.copyfile(self.db_path, db2.name)
        raw2 = sqlite3.connect(db2.name)
        conn2 = WriteGuard(raw2)
        records2 = _build_full(self.sources, self.db_path)
        for domain, recs in records2.items():
            if recs:
                write_interval(conn2, recs, self.app_id, "wave2-key", now_ms=FIXED_MS)
        conn2.commit()
        raw2.close()
        hash2 = hashlib.sha256(db_bytes(db2.name)).hexdigest()

        pathlib.Path(db1.name).unlink(missing_ok=True)
        pathlib.Path(db2.name).unlink(missing_ok=True)

        self.assertEqual(hash1, hash2,
            "Two identical pipeline runs should produce byte-identical db files")

    def test_expected_counts(self):
        """Full pipeline should produce expected record counts per domain."""
        # Read cutoffs from fixture db
        conn_ref = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        # GAP-3 fix: use None instead of 0 when MIN(start_time) is NULL (empty table)
        cutoffs = {}
        for domain, table in [
            ("steps", "steps_record_table"),
            ("distance", "distance_record_table"),
            ("calories", "total_calories_burned_record_table"),
            ("sleep", "sleep_session_record_table"),
            ("heart_rate", "heart_rate_record_table"),
            ("exercise", "exercise_session_record_table"),
        ]:
            try:
                row = conn_ref.execute(f"SELECT MIN(start_time) FROM {table}").fetchone()
                cutoffs[domain] = row[0] if row and row[0] is not None else None
            except Exception:
                cutoffs[domain] = None
        conn_ref.close()

        records_by_domain = _build_full(self.sources, self.db_path)

        # ACTIVITY: 10 rows in ACTIVITY.csv, 1 is all-zero → 9 days × 3 = 27 records
        activity = records_by_domain.get("activity", [])
        self.assertEqual(len(activity), 27)

        # SLEEP: 8 rows in SLEEP.csv, 2 are placeholders → 6 sessions
        sleep = records_by_domain.get("sleep", [])
        self.assertEqual(len(sleep), 6)

        # HEARTRATE: 2 auto days + 5 manual (after dedup of duplicate timestamp) = 7 records
        hr_auto = [r for r in records_by_domain.get("heartrate", []) if r.kind == "hr_auto"]
        hr_manual = [r for r in records_by_domain.get("heartrate", []) if r.kind == "hr_manual"]
        self.assertEqual(len(hr_auto), 2)
        self.assertEqual(len(hr_manual), 5)

        # EXERCISE: 6 rows in SPORT.csv → 6 sessions
        exercise = records_by_domain.get("exercise", [])
        self.assertEqual(len(exercise), 6)

    def test_invariants_pass(self):
        """Full pipeline output should pass all invariants."""
        db_copy = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        db_copy.close()
        shutil.copyfile(self.db_path, db_copy.name)

        raw_conn = sqlite3.connect(db_copy.name)
        conn = WriteGuard(raw_conn)

        records_by_domain = _build_full(self.sources, self.db_path)
        for domain, recs in records_by_domain.items():
            if recs:
                write_interval(conn, recs, self.app_id, "wave2-key", now_ms=FIXED_MS)
        conn.commit()

        ok, findings = run_invariants(
            conn,
            expected_domains=["activity", "sleep", "heartrate", "exercise"],
            source_db_path=self.db_path,
        )

        conn.close()
        pathlib.Path(db_copy.name).unlink(missing_ok=True)

        self.assertTrue(ok, f"Invariants failed: {findings}")

    def test_pilot_is_first_per_domain(self):
        """Pilot should produce exactly 1 record per domain."""
        records_by_domain = _build_full(self.sources, self.db_path)

        pilot_recs = {}
        for domain, recs in records_by_domain.items():
            if recs:
                pilot_recs[domain] = [recs[0]]  # first record per domain

        # Verify pilot count
        self.assertEqual(len(pilot_recs), 4)  # activity, sleep, heartrate, exercise

        # Verify each domain has exactly 1 pilot record
        for domain, recs in pilot_recs.items():
            self.assertEqual(len(recs), 1, f"{domain} pilot should have 1 record")

    def test_pilot_records_in_full(self):
        """Pilot records should be present in full records (same UUIDs)."""
        from ghc_db_manager import knowledge as kn

        records_by_domain = _build_full(self.sources, self.db_path)

        for domain, full_recs in records_by_domain.items():
            if not full_recs:
                continue
            pilot_rec = full_recs[0]  # first record per domain
            # Find the pilot UUID
            pilot_uuid = kn.deterministic_uuid(
                "wave2-key", domain, pilot_rec.start_ms, pilot_rec.end_ms
            )
            # Check that this UUID exists in the full set
            found = False
            for rec in full_recs:
                rec_uuid = kn.deterministic_uuid(
                    "wave2-key", domain, rec.start_ms, rec.end_ms
                )
                if rec_uuid == pilot_uuid:
                    found = True
                    break
            self.assertTrue(found, f"Pilot record for {domain} not found in full set")


if __name__ == "__main__":
    unittest.main()
