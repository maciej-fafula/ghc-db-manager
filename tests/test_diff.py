"""
tests/test_diff.py — Phase E diff validation tests.

Tests the diff_databases() function and CLI against fixture databases,
using direct sqlite3 manipulation to create fresh variants.

Test cases:
  (a) identical → PASS
  (b) +1 steps row (growth) → PASS_WITH_EXPECTED_DEVIATIONS
  (c) -1 weight row with --expected-deletions weight=1 → PASS_WITH_EXPECTED_DEVIATIONS
  (d) delete a snapshot uuid entirely → FAIL (data loss)
  (e) mutate a sleep row's local_date only → BENIGN_NORMALIZATION → PASS_WITH_EXPECTED_DEVIATIONS
  (f) mutate a weight value → FAIL
"""

import os
import shutil
import sqlite3
import tempfile
import unittest

# Build fixture db path
FIXTURE_PROJECT_ROOT = os.path.join(
    os.path.dirname(__file__), ".."
)
SRC_ROOT = os.path.join(FIXTURE_PROJECT_ROOT, "src")
import sys
sys.path.insert(0, SRC_ROOT)

from tests.fixtures.make_fixture_db import build as build_fixture


class TestDiff(unittest.TestCase):
    """Test diff_databases() and render_text() against fixture variants."""

    @classmethod
    def setUpClass(cls):
        """Build the fixture db once for all tests."""
        cls.fd, cls.fixture_path = tempfile.mkstemp(suffix=".db")
        os.close(cls.fd)
        build_fixture(cls.fixture_path)

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.fixture_path)

    def _copy_fixture(self) -> str:
        """Make a temp copy of the fixture db. Caller manages cleanup."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        shutil.copyfile(self.fixture_path, path)
        return path

    def _get_weight_uuid(self, conn: sqlite3.Connection) -> bytes:
        """Return the uuid of the first weight row."""
        return conn.execute(
            "SELECT uuid FROM weight_record_table LIMIT 1"
        ).fetchone()[0]

    def _get_sleep_uuid(self, conn: sqlite3.Connection) -> bytes:
        """Return the uuid of the first sleep session row."""
        return conn.execute(
            "SELECT uuid FROM sleep_session_record_table LIMIT 1"
        ).fetchone()[0]

    def _get_steps_uuid(self, conn: sqlite3.Connection) -> bytes:
        """Return the uuid of the first steps row."""
        return conn.execute(
            "SELECT uuid FROM steps_record_table LIMIT 1"
        ).fetchone()[0]

    # ------------------------------------------------------------------
    # Case (a): identical → PASS
    # ------------------------------------------------------------------

    def test_identical_dbs_pass(self):
        """Identical snapshot and fresh should give verdict PASS."""
        from ghc_db_manager.validation.diff import diff_databases

        snap = self.fixture_path
        fresh = self._copy_fixture()
        try:
            report = diff_databases(snap, fresh)
            self.assertEqual(report["verdict"], "PASS")
            self.assertEqual(report["unexpected_findings"], [])
            self.assertEqual(report["expected_deviations"], [])
        finally:
            os.unlink(fresh)

    # ------------------------------------------------------------------
    # Case (b): +1 steps row (growth) → PASS_WITH_EXPECTED_DEVIATIONS
    # ------------------------------------------------------------------

    def test_steps_growth_is_expected_deviation(self):
        """Add one steps row in fresh → EXPECTED_GROWTH → PASS_WITH."""
        from ghc_db_manager.validation.diff import diff_databases

        snap = self.fixture_path
        fresh = self._copy_fixture()
        try:
            conn = sqlite3.connect(fresh)
            # Insert an extra steps row (any values, uuid doesn't matter)
            # Columns: uuid, last_modified_time, client_record_id, client_record_version,
            #          device_info_id, app_info_id, recording_method, dedupe_hash,
            #          start_time, start_zone_offset, end_time, end_zone_offset, local_date, count
            conn.execute(
                """INSERT INTO steps_record_table
                   (uuid, last_modified_time, client_record_id, client_record_version,
                    device_info_id, app_info_id, recording_method, dedupe_hash,
                    start_time, start_zone_offset, end_time, end_zone_offset,
                    local_date, count)
                   VALUES
                   (?, 1777000000000, NULL, '0', 1, 5, 2,
                    X'01000000010000000000000001000000',
                    1777000000000, 3600, 1777080000000, 3600, 20580, 9999)""",
                (b"\x00" * 16,)
            )
            conn.commit()
            conn.close()

            report = diff_databases(snap, fresh)
            self.assertEqual(report["verdict"], "PASS_WITH_EXPECTED_DEVIATIONS")
            devs = report["expected_deviations"]
            self.assertTrue(
                any("steps" in d and "EXPECTED_GROWTH" in d for d in devs),
                f"Expected GROWTH deviation not found in {devs}"
            )
        finally:
            os.unlink(fresh)

    # ------------------------------------------------------------------
    # Case (c): -1 weight row with --expected-deletions weight=1 → PASS_WITH
    # ------------------------------------------------------------------

    def test_declared_weight_deletion_is_expected(self):
        """Delete a weight row, declare it → PASS_WITH."""
        from ghc_db_manager.validation.diff import diff_databases

        snap = self.fixture_path
        fresh = self._copy_fixture()
        try:
            # Delete one weight row from fresh
            conn = sqlite3.connect(fresh)
            conn.execute(
                "DELETE FROM weight_record_table WHERE rowid = (SELECT MIN(rowid) FROM weight_record_table)"
            )
            conn.commit()
            conn.close()

            # With expected_deletions, should be PASS_WITH
            report = diff_databases(
                snap, fresh,
                expected_deletions={"weight": 1},
                allow_growth=False,
            )
            self.assertEqual(report["verdict"], "PASS_WITH_EXPECTED_DEVIATIONS")
            devs = report["expected_deviations"]
            self.assertTrue(
                any("weight" in d and "EXPECTED_DELETION" in d for d in devs),
                f"Expected DELETION deviation not found in {devs}"
            )
        finally:
            os.unlink(fresh)

    # ------------------------------------------------------------------
    # Case (d): delete a snapshot uuid entirely → FAIL
    # ------------------------------------------------------------------

    def test_missing_uuid_is_data_loss_fail(self):
        """Missing UUID in fresh (not declared) → FAIL."""
        from ghc_db_manager.validation.diff import diff_databases

        snap = self.fixture_path
        fresh = self._copy_fixture()
        try:
            conn = sqlite3.connect(fresh)
            # Delete one weight row from fresh (use rowid to delete one row)
            conn.execute(
                "DELETE FROM weight_record_table WHERE rowid = (SELECT MIN(rowid) FROM weight_record_table)"
            )
            conn.commit()
            conn.close()

            report = diff_databases(
                snap, fresh,
                expected_deletions={},  # no declaration
                allow_growth=False,
            )
            self.assertEqual(report["verdict"], "FAIL")
            self.assertTrue(
                any("missing in fresh" in f or "data loss" in f
                    for f in report["unexpected_findings"]),
                f"Data loss finding not in unexpected: {report['unexpected_findings']}"
            )
        finally:
            os.unlink(fresh)

    # ------------------------------------------------------------------
    # Case (e): mutate sleep local_date only → BENIGN_NORMALIZATION → PASS_WITH
    # ------------------------------------------------------------------

    def test_sleep_local_date_only_change_is_benign(self):
        """Change only local_date on a sleep row → BENIGN_NORMALIZATION."""
        from ghc_db_manager.validation.diff import diff_databases

        snap = self.fixture_path
        fresh = self._copy_fixture()
        try:
            conn = sqlite3.connect(fresh)
            # Pick a sleep session and change only its local_date
            sleep_uuid = self._get_sleep_uuid(conn)
            # Get current local_date
            row = conn.execute(
                "SELECT start_time, start_zone_offset FROM sleep_session_record_table WHERE uuid = ?",
                (sleep_uuid,)
            ).fetchone()
            old_ld = (row[0] + 1000 * row[1]) // 86400000
            new_ld = old_ld + 1  # shift by one day — simulating HC normalization
            conn.execute(
                "UPDATE sleep_session_record_table SET local_date = ? WHERE uuid = ?",
                (new_ld, sleep_uuid)
            )
            conn.commit()
            conn.close()

            report = diff_databases(snap, fresh)
            self.assertEqual(report["verdict"], "PASS_WITH_EXPECTED_DEVIATIONS")
            devs = report["expected_deviations"]
            self.assertTrue(
                any("BENIGN_NORMALIZATION" in d for d in devs),
                f"BENIGN_NORMALIZATION not found in deviations: {devs}"
            )
        finally:
            os.unlink(fresh)

    # ------------------------------------------------------------------
    # Case (f): mutate a weight value → FAIL
    # ------------------------------------------------------------------

    def test_weight_value_mutation_is_unexpected_fail(self):
        """Change a weight value → UNEXPECTED field diff → FAIL."""
        from ghc_db_manager.validation.diff import diff_databases

        snap = self.fixture_path
        fresh = self._copy_fixture()
        try:
            conn = sqlite3.connect(fresh)
            # Change weight value of first row
            conn.execute(
                "UPDATE weight_record_table SET weight = weight + 1.0 WHERE rowid = (SELECT MIN(rowid) FROM weight_record_table)"
            )
            conn.commit()
            conn.close()

            report = diff_databases(snap, fresh)
            self.assertEqual(report["verdict"], "FAIL")
            self.assertTrue(
                any("field diff" in f or "weight" in f
                    for f in report["unexpected_findings"]),
                f"Unexpected finding not in list: {report['unexpected_findings']}"
            )
        finally:
            os.unlink(fresh)

    # ------------------------------------------------------------------
    # render_text smoke test
    # ------------------------------------------------------------------

    def test_render_text_produces_output(self):
        """render_text produces non-empty text with verdict line."""
        from ghc_db_manager.validation.diff import diff_databases, render_text

        snap = self.fixture_path
        fresh = self._copy_fixture()
        try:
            report = diff_databases(snap, fresh)
            text = render_text(report)
            self.assertIsInstance(text, str)
            self.assertTrue(len(text) > 0)
            self.assertIn("verdict:", text)
            self.assertIn("PASS", text)
        finally:
            os.unlink(fresh)


if __name__ == "__main__":
    unittest.main()
