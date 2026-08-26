"""test_writer_interval.py — tests for write_interval."""

import datetime
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest

FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
sys.path.insert(0, str(FIXTURES_DIR.parent.parent / "src"))

from ghc_db_manager.dbio import WriteGuard
from ghc_db_manager.domains.activity import ActivityCanonicalRecord
from ghc_db_manager.domains.sleep import SleepCanonicalRecord
from ghc_db_manager.domains.heartrate import HeartRateCanonicalRecord
from ghc_db_manager.domains.exercise import ExerciseCanonicalRecord
from ghc_db_manager.writer import write_interval, IntervalRecordError


# Build fixture db once for the class
_fixture_db = None


def _build_fixture_db():
    global _fixture_db
    if _fixture_db is None:
        from tests.fixtures.make_fixture_db import build
        _fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        _fd.close()
        build(_fd.name)
        _fixture_db = _fd.name
    return _fixture_db


class TestWriteInterval(unittest.TestCase):
    """Test write_interval function."""

    @classmethod
    def setUpClass(cls):
        cls.db_path = _build_fixture_db()

    def _copy_db(self):
        fd = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        fd.close()
        shutil.copyfile(self.db_path, fd.name)
        return fd.name

    def test_interval_insert_shape(self):
        """Activity records insert with correct shape."""
        db_copy = self._copy_db()
        raw_conn = sqlite3.connect(db_copy)
        conn = WriteGuard(raw_conn)

        # Create a steps record
        start_utc = datetime.datetime(2025, 1, 15, 23, 0, 0, tzinfo=datetime.timezone.utc)
        end_utc = datetime.datetime(2025, 1, 16, 23, 0, 0, tzinfo=datetime.timezone.utc)

        record = ActivityCanonicalRecord(
            source="zepp",
            kind="steps",
            start_utc=start_utc,
            end_utc=end_utc,
            start_offset_seconds=3600,
            end_offset_seconds=3600,
            local_date=20250115,
            extra={"count": 8432},
        )

        inserted = write_interval(conn, [record], app_info_id=6, project_key="test-key")

        conn.commit()
        conn.close()
        pathlib.Path(db_copy).unlink(missing_ok=True)

        self.assertGreater(inserted["steps"], 0)

    def test_dedupe_hash_interval(self):
        """Records use 32B interval dedupe_hash."""
        db_copy = self._copy_db()
        raw_conn = sqlite3.connect(db_copy)
        conn = WriteGuard(raw_conn)

        start_utc = datetime.datetime(2025, 1, 15, 23, 0, 0, tzinfo=datetime.timezone.utc)
        end_utc = datetime.datetime(2025, 1, 16, 23, 0, 0, tzinfo=datetime.timezone.utc)

        record = ActivityCanonicalRecord(
            source="zepp", kind="steps",
            start_utc=start_utc, end_utc=end_utc,
            start_offset_seconds=3600, end_offset_seconds=3600,
            local_date=20250115, extra={"count": 8432},
        )

        write_interval(conn, [record], app_info_id=6, project_key="test-key")
        conn.commit()

        # Check dedupe_hash is 32 bytes
        row = raw_conn.execute("SELECT dedupe_hash FROM steps_record_table ORDER BY row_id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(len(row[0]), 32)

        conn.close()
        pathlib.Path(db_copy).unlink(missing_ok=True)

    def test_offsets_in_seconds(self):
        """Zone offsets are stored in seconds (not milliseconds)."""
        db_copy = self._copy_db()
        raw_conn = sqlite3.connect(db_copy)
        conn = WriteGuard(raw_conn)

        start_utc = datetime.datetime(2025, 1, 15, 23, 0, 0, tzinfo=datetime.timezone.utc)
        end_utc = datetime.datetime(2025, 1, 16, 23, 0, 0, tzinfo=datetime.timezone.utc)

        record = ActivityCanonicalRecord(
            source="zepp", kind="distance",
            start_utc=start_utc, end_utc=end_utc,
            start_offset_seconds=3600, end_offset_seconds=3600,
            local_date=20250115, extra={"distance": 7040.0},
        )

        write_interval(conn, [record], app_info_id=6, project_key="test-key")
        conn.commit()

        row = raw_conn.execute("SELECT start_zone_offset FROM distance_record_table ORDER BY row_id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], 3600)  # 1 hour in seconds, NOT 3600000 ms

        conn.close()
        pathlib.Path(db_copy).unlink(missing_ok=True)

    def test_local_date_start_based(self):
        """local_date is start-based for activity records."""
        db_copy = self._copy_db()
        raw_conn = sqlite3.connect(db_copy)
        conn = WriteGuard(raw_conn)

        # 2025-01-15 in Europe/Warsaw: winter, UTC+1
        # Local midnight 2025-01-15 Warsaw = 2025-01-14 23:00 UTC
        start_utc = datetime.datetime(2025, 1, 14, 23, 0, 0, tzinfo=datetime.timezone.utc)
        end_utc = datetime.datetime(2025, 1, 15, 23, 0, 0, tzinfo=datetime.timezone.utc)

        record = ActivityCanonicalRecord(
            source="zepp", kind="calories",
            start_utc=start_utc, end_utc=end_utc,
            start_offset_seconds=3600, end_offset_seconds=3600,
            local_date=20250115, extra={"energy": 385000.0},
        )

        write_interval(conn, [record], app_info_id=6, project_key="test-key")
        conn.commit()

        row = raw_conn.execute("SELECT local_date FROM total_calories_burned_record_table ORDER BY row_id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], 20250115)

        conn.close()
        pathlib.Path(db_copy).unlink(missing_ok=True)

    def test_sleep_stages_lastrowid(self):
        """Sleep stages are inserted with parent_key linking to session."""
        db_copy = self._copy_db()
        raw_conn = sqlite3.connect(db_copy)
        conn = WriteGuard(raw_conn)

        start_utc = datetime.datetime(2025, 1, 15, 23, 30, 0, tzinfo=datetime.timezone.utc)
        end_utc = datetime.datetime(2025, 1, 16, 6, 45, 0, tzinfo=datetime.timezone.utc)
        # epoch_ms of start_utc for filtering
        new_start_ms = int(start_utc.timestamp() * 1000)

        record = SleepCanonicalRecord(
            source="zepp",
            start_utc=start_utc, end_utc=end_utc,
            start_offset_seconds=3600, end_offset_seconds=3600,
            local_date=20250116,
            stages=[
                (1736898000000, 1736905200000, 4),  # LIGHT stage
                (1736905200000, 1736921400000, 5),  # DEEP stage
            ],
        )

        inserted = write_interval(conn, [record], app_info_id=6, project_key="test-key")
        conn.commit()

        self.assertEqual(inserted["sleep"], 1)
        self.assertEqual(inserted["sleep_stages"], 2)

        # Verify parent_key linkage for the newly inserted session
        # (filter by start_time to isolate the new session from fixture rows)
        sessions = raw_conn.execute(
            "SELECT row_id FROM sleep_session_record_table WHERE start_time = ?",
            (new_start_ms,),
        ).fetchall()
        self.assertEqual(len(sessions), 1)
        session_id = sessions[0][0]

        stages = raw_conn.execute(
            "SELECT parent_key, stage_type FROM sleep_stages_table WHERE parent_key = ? ORDER BY stage_start_time",
            (session_id,),
        ).fetchall()
        self.assertEqual(len(stages), 2)
        for parent_key, _ in stages:
            self.assertEqual(parent_key, session_id)

        conn.close()
        pathlib.Path(db_copy).unlink(missing_ok=True)

    def test_hr_series_lastrowid(self):
        """HR series samples are inserted with parent_key linking to session."""
        db_copy = self._copy_db()
        raw_conn = sqlite3.connect(db_copy)
        conn = WriteGuard(raw_conn)

        start_utc = datetime.datetime(2025, 1, 9, 0, 0, 0, tzinfo=datetime.timezone.utc)
        end_utc = datetime.datetime(2025, 1, 9, 1, 0, 0, tzinfo=datetime.timezone.utc)
        new_start_ms = int(start_utc.timestamp() * 1000)

        record = HeartRateCanonicalRecord(
            source="zepp", kind="hr_auto",
            start_utc=start_utc, end_utc=end_utc,
            start_offset_seconds=3600, end_offset_seconds=3600,
            local_date=20250109,
            samples=[
                (1736380800000, 68),
                (1736381100000, 70),
                (1736381400000, 72),
            ],
            recording_method=2,
        )

        inserted = write_interval(conn, [record], app_info_id=6, project_key="test-key")
        conn.commit()

        self.assertEqual(inserted["hr_auto"], 1)
        self.assertEqual(inserted["hr_series"], 3)

        # Verify parent_key linkage — isolate the newly inserted session by start_time
        sessions = raw_conn.execute(
            "SELECT row_id FROM heart_rate_record_table WHERE start_time = ?",
            (new_start_ms,),
        ).fetchall()
        self.assertEqual(len(sessions), 1)
        session_id = sessions[0][0]

        series = raw_conn.execute(
            "SELECT parent_key, beats_per_minute FROM heart_rate_record_series_table WHERE parent_key = ? ORDER BY epoch_millis",
            (session_id,),
        ).fetchall()
        self.assertEqual(len(series), 3)
        for parent_key, _ in series:
            self.assertEqual(parent_key, session_id)

        conn.close()
        pathlib.Path(db_copy).unlink(missing_ok=True)

    def test_exercise_extras(self):
        """Exercise records include exercise_type, title, has_route."""
        db_copy = self._copy_db()
        raw_conn = sqlite3.connect(db_copy)
        conn = WriteGuard(raw_conn)

        start_utc = datetime.datetime(2025, 1, 10, 7, 30, 0, tzinfo=datetime.timezone.utc)
        end_utc = datetime.datetime(2025, 1, 10, 8, 30, 0, tzinfo=datetime.timezone.utc)

        record = ExerciseCanonicalRecord(
            source="zepp",
            start_utc=start_utc, end_utc=end_utc,
            start_offset_seconds=3600, end_offset_seconds=3600,
            local_date=20250110,
            exercise_type=56,
            title="Outdoor Running",
            has_route=0,
        )

        inserted = write_interval(conn, [record], app_info_id=6, project_key="test-key")
        conn.commit()

        row = raw_conn.execute(
            "SELECT exercise_type, title, has_route FROM exercise_session_record_table ORDER BY row_id DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], 56)  # exercise_type
        self.assertEqual(row[1], "Outdoor Running")
        self.assertEqual(row[2], 0)  # has_route

        conn.close()
        pathlib.Path(db_copy).unlink(missing_ok=True)

    def test_ms_offset_raises(self):
        """Passing millisecond offsets raises IntervalRecordError."""
        db_copy = self._copy_db()
        raw_conn = sqlite3.connect(db_copy)
        conn = WriteGuard(raw_conn)

        start_utc = datetime.datetime(2025, 1, 15, 23, 0, 0, tzinfo=datetime.timezone.utc)
        end_utc = datetime.datetime(2025, 1, 16, 23, 0, 0, tzinfo=datetime.timezone.utc)

        # Large offset (> 64800 * 10) indicates ms was passed as seconds
        record = ActivityCanonicalRecord(
            source="zepp", kind="steps",
            start_utc=start_utc, end_utc=end_utc,
            start_offset_seconds=3600000,  # 3600000 seconds (1000 hours) = clearly ms
            end_offset_seconds=3600000,
            local_date=20250115,
            extra={"count": 8432},
        )

        with self.assertRaises(IntervalRecordError):
            write_interval(conn, [record], app_info_id=6, project_key="test-key")

        conn.close()
        pathlib.Path(db_copy).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
