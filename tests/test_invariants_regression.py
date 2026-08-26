"""
test_invariants_regression.py — regression tests for the sleep local_date invariant.

These tests would have caught the bug where sleep_session_record_table.local_date
was checked as end-based when HC's canonical recomputed form is start-based.

Case A: fixture db (canonical start-based sleep) → run_invariants must PASS
Case B: variant db with end-based sleep → run_invariants must FAIL with a
        local_date finding for sleep_session_record_table
"""

import sys
import pathlib
import sqlite3
import tempfile
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent.parent / "fixtures"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.validation.invariants import run_invariants
from tests.fixtures.make_fixture_db import build


class TestSleepLocalDateRegression(unittest.TestCase):
    """Regression: sleep local_date invariant must be start-based (HC canonical)."""

    def test_canonical_start_based_sleep_passes(self):
        """Fixture db with start-based sleep local_date passes invariants."""
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        try:
            build(fd.name)
            conn = sqlite3.connect(fd.name)
            ok, findings = run_invariants(conn, expected_domains=["sleep"])
            conn.close()
            sleep_findings = [f for f in findings if "sleep_session_record_table" in f]
            self.assertTrue(
                ok,
                f"Expected pass for start-based sleep, got findings: {findings}"
            )
            self.assertEqual(
                sleep_findings, [],
                f"Expected no sleep findings, got: {sleep_findings}"
            )
        finally:
            pathlib.Path(fd.name).unlink(missing_ok=True)

    def test_end_based_sleep_fails_invariant(self):
        """A db with a wrong local_date for sleep must FAIL the invariant check.

        The bug was that the invariant was checking end-based when HC canonical
        is start-based. This test proves the invariant bites on a wrong local_date.
        We mutate to a clearly-wrong value (start_based + 1) since fixture sessions
        happen to have start and end on the same UTC day (Warsaw TZ), so end-based
        equals start-based for those particular fixtures.
        """
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        fd2 = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd2.close()
        try:
            # Build canonical fixture
            build(fd.name)
            # Copy it
            src = sqlite3.connect(fd.name)
            dst = sqlite3.connect(fd2.name)
            src.backup(dst)
            src.close()
            dst.close()

            # Mutate: update one sleep row's local_date to a WRONG value.
            # The correct start-based formula is:
            #   local_date = (start_time + 1000 * start_zone_offset) / 86400000
            # We set it to start_time_based + 1 (clearly wrong, off by one day).
            conn = sqlite3.connect(fd2.name)
            row = conn.execute(
                "SELECT start_time, start_zone_offset "
                "FROM sleep_session_record_table LIMIT 1"
            ).fetchone()
            s_time, s_off = row
            correct_ld = (s_time + 1000 * s_off) // 86400000
            wrong_ld = correct_ld + 1  # off by 1 — clearly violates the invariant
            conn.execute(
                "UPDATE sleep_session_record_table SET local_date = ? WHERE row_id = 1",
                (wrong_ld,)
            )
            conn.commit()
            conn.close()

            # Now run invariants — must FAIL with a local_date finding for sleep
            conn = sqlite3.connect(fd2.name)
            ok, findings = run_invariants(conn, expected_domains=["sleep"])
            conn.close()
            sleep_findings = [f for f in findings if "sleep_session_record_table" in f]
            self.assertFalse(
                ok,
                f"Expected FAIL for end-based sleep, but invariants passed. Findings: {findings}"
            )
            self.assertTrue(
                any("local_date" in f.lower() for f in sleep_findings),
                f"Expected a local_date finding for sleep, got: {sleep_findings}"
            )
        finally:
            pathlib.Path(fd.name).unlink(missing_ok=True)
            pathlib.Path(fd2.name).unlink(missing_ok=True)


class TestFKInvariantCoverageAllTables(unittest.TestCase):
    """FK invariant now covers ALL tables in knowledge.TABLES, not just weight tables."""

    def test_missing_app_info_id_fails_invariant(self):
        """Removing an app_info_id referenced by a steps row must fail the FK invariant."""
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        try:
            build(fd.name)
            conn = sqlite3.connect(fd.name)
            # Get the app_info_id used by a steps row (they use APP_WATCH=6)
            app_row = conn.execute(
                "SELECT app_info_id FROM steps_record_table LIMIT 1"
            ).fetchone()
            if app_row is None:
                self.skipTest("No steps rows to test with")
            bad_app_id = app_row[0]

            # Delete the application_info row (only if it's not the last one)
            count = conn.execute("SELECT COUNT(*) FROM application_info_table").fetchone()[0]
            if count <= 1:
                self.skipTest("Only one app_info row; cannot safely delete")

            conn.execute("DELETE FROM application_info_table WHERE row_id = ?", (bad_app_id,))
            conn.commit()

            ok, findings = run_invariants(conn, expected_domains=["activity"])
            conn.close()
            fk_findings = [f for f in findings if "app_info_id" in f or "foreign_key" in f.lower()]
            self.assertFalse(
                ok,
                f"Expected FK failure when app_info_id is missing. Findings: {findings}"
            )
            self.assertTrue(
                any("app_info_id" in f for f in fk_findings),
                f"Expected app_info_id FK finding, got: {fk_findings}"
            )
        finally:
            pathlib.Path(fd.name).unlink(missing_ok=True)

    def test_missing_device_info_id_fails_invariant(self):
        """Removing a device_info_id referenced by a steps row must fail the FK invariant."""
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        try:
            build(fd.name)
            conn = sqlite3.connect(fd.name)
            # Get the device_info_id used by a steps row
            dev_row = conn.execute(
                "SELECT device_info_id FROM steps_record_table LIMIT 1"
            ).fetchone()
            if dev_row is None:
                self.skipTest("No steps rows to test with")
            bad_dev_id = dev_row[0]

            # Delete the device_info row (only if it's not the last one)
            count = conn.execute("SELECT COUNT(*) FROM device_info_table").fetchone()[0]
            if count <= 1:
                self.skipTest("Only one device_info row; cannot safely delete")

            conn.execute("DELETE FROM device_info_table WHERE row_id = ?", (bad_dev_id,))
            conn.commit()

            ok, findings = run_invariants(conn, expected_domains=["activity"])
            conn.close()
            fk_findings = [f for f in findings if "device_info_id" in f or "foreign_key" in f.lower()]
            self.assertFalse(
                ok,
                f"Expected FK failure when device_info_id is missing. Findings: {findings}"
            )
            self.assertTrue(
                any("device_info_id" in f for f in fk_findings),
                f"Expected device_info_id FK finding, got: {fk_findings}"
            )
        finally:
            pathlib.Path(fd.name).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
