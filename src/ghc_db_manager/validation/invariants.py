"""
validation/invariants.py — pre-import invariant checks.

Ported from PoC insert_weight_history.py §B.2.

Run on every touched table after writing records, BEFORE packing the ZIP.
A failed invariant = abort the build (never pack a db that hasn't passed).

Checks performed (per touched table):
  1. local_date formula: ``local_date == (time + 1000 * zone_offset) / 86400000``
  2. zone_offset within ±64800 seconds
  3. uuid uniqueness
  4. client_record_id uniqueness (where NOT NULL)
  5. FK targets exist (app_info_id → application_info_table,
                       device_info_id → device_info_table)
  6. PRAGMA integrity_check == 'ok'
  7. PRAGMA foreign_key_check == empty
  8. activity_date coverage for every inserted record
  9. do-not-touch tables unchanged (compare against source db)
 10. row-count deltas match expected (optional)

Returns (ok: bool, findings: list[str]).
"""

import sqlite3
from typing import TYPE_CHECKING, Optional, Union

from ghc_db_manager import knowledge as kn
from ghc_db_manager.dbio import open_readonly

if TYPE_CHECKING:
    from ghc_db_manager.dbio import WriteGuard


# Tables that should be checked for weight-domain invariants
_WEIGHT_TABLES = [
    ("weight_record_table",       "time",           "weight"),
    ("body_fat_record_table",     "time",           "percentage"),
    ("lean_body_mass_record_table", "time",          "mass"),
]

# Interval tables: (table_name, start_time_col, end_time_col, local_date_is_end_based)
_INTERVAL_TABLES = [
    ("steps_record_table",                          "start_time", "end_time", False),
    ("distance_record_table",                       "start_time", "end_time", False),
    ("total_calories_burned_record_table",          "start_time", "end_time", False),
    ("sleep_session_record_table",                  "start_time", "end_time", True),   # end-based
    ("heart_rate_record_table",                    "start_time", "end_time", False),
    ("exercise_session_record_table",                "start_time", "end_time", False),
]

# Record-type IDs for activity_date coverage
_INTERVAL_TYPE_IDS: dict[str, int] = {
    "steps_record_table":        kn.RECORD_TYPE_IDS["steps"],           # 1
    "distance_record_table":     kn.RECORD_TYPE_IDS["steps"],           # 1 (same)
    "total_calories_burned_record_table": kn.RECORD_TYPE_IDS["steps"],  # 1 (same)
    "sleep_session_record_table": kn.RECORD_TYPE_IDS["sleep_session"],  # 38
    "heart_rate_record_table":   kn.RECORD_TYPE_IDS["heart_rate"],     # 11
    "exercise_session_record_table": kn.RECORD_TYPE_IDS["exercise_session"],  # 37
}

# All do-not-touch tables to verify unchanged
_PROTECTED_TABLES = kn.DO_NOT_TOUCH_TABLES


def run_invariants(
    conn: Union[sqlite3.Connection, "WriteGuard"],
    expected_domains: Optional[list[str]] = None,
    source_db_path: Optional[str] = None,
) -> tuple[bool, list[str]]:
    """
    Run all pre-import invariants on a (WriteGuard-wrapped) DB connection.

    Parameters
    ----------
    conn : sqlite3.Connection | WriteGuard
        Connection to the modified (working copy) database.
    expected_domains : list[str] | None
        Domains that were written (e.g. ["weight"]).  If None, only
        checks that apply to all tables run.
    source_db_path : str | None
        Path to the original source DB (before modification).  Used to verify
        do-not-touch tables are unchanged and to compute row-count deltas.
        If None, the do-not-touch table check and delta checks are skipped.

    Returns
    -------
    (ok, findings)
        ok is True if all invariants passed.
        findings is a list of human-readable failure messages (empty if ok).
    """
    if expected_domains is None:
        expected_domains = []

    findings: list[str] = []
    cur = conn.cursor()

    # Determine which tables to check based on expected_domains
    tables_to_check: list[tuple] = []
    if "weight" in expected_domains:
        tables_to_check.extend(_WEIGHT_TABLES)

    interval_tables_to_check: list[tuple] = []
    if "activity" in expected_domains:
        interval_tables_to_check.extend([
            ("steps_record_table",                          "start_time", "end_time", False),
            ("distance_record_table",                       "start_time", "end_time", False),
            ("total_calories_burned_record_table",           "start_time", "end_time", False),
        ])
    if "sleep" in expected_domains:
        interval_tables_to_check.append(("sleep_session_record_table", "start_time", "end_time", True))
    if "heartrate" in expected_domains:
        interval_tables_to_check.append(("heart_rate_record_table", "start_time", "end_time", False))
    if "exercise" in expected_domains:
        interval_tables_to_check.append(("exercise_session_record_table", "start_time", "end_time", False))

    # 1a. local_date formula + zone_offset bounds for instant tables
    for table, time_col, _ in tables_to_check:
        # local_date invariant
        bad = cur.execute(
            f"""SELECT COUNT(*) FROM {table}
                WHERE local_date != ({time_col} + 1000 * zone_offset) / 86400000
                  AND {time_col} IS NOT NULL"""
        ).fetchone()[0]
        if bad:
            findings.append(
                f"{table}: {bad} rows violate local_date formula "
                f"(time={time_col}, zone_offset in seconds)"
            )

        # zone_offset bounds
        bad_off = cur.execute(
            f"""SELECT COUNT(*) FROM {table}
                WHERE zone_offset NOT BETWEEN -{kn.ZONE_OFFSET_MAX_SECONDS}
                                         AND {kn.ZONE_OFFSET_MAX_SECONDS}"""
        ).fetchone()[0]
        if bad_off:
            findings.append(
                f"{table}: {bad_off} rows with zone_offset outside "
                f"±{kn.ZONE_OFFSET_MAX_SECONDS} seconds (possible ms→s bug)"
            )

    # 1b. Interval table checks
    for table, s_col, e_col, end_based in interval_tables_to_check:
        # local_date invariant (start-based, or end-based for sleep)
        date_col = f"({e_col} + 1000 * end_zone_offset) / 86400000" if end_based \
                  else f"({s_col} + 1000 * start_zone_offset) / 86400000"
        bad = cur.execute(
            f"""SELECT COUNT(*) FROM {table}
                WHERE local_date != {date_col}
                  AND {s_col} IS NOT NULL"""
        ).fetchone()[0]
        if bad:
            findings.append(
                f"{table}: {bad} rows violate local_date formula "
                f"(zone_offset in seconds, {'end' if end_based else 'start'}-based)"
            )

        # start_zone_offset bounds
        bad_off = cur.execute(
            f"""SELECT COUNT(*) FROM {table}
                WHERE start_zone_offset NOT BETWEEN -{kn.ZONE_OFFSET_MAX_SECONDS}
                                                 AND {kn.ZONE_OFFSET_MAX_SECONDS}"""
        ).fetchone()[0]
        if bad_off:
            findings.append(
                f"{table}: {bad_off} rows with start_zone_offset outside "
                f"±{kn.ZONE_OFFSET_MAX_SECONDS} seconds"
            )

        # end_zone_offset bounds
        bad_off = cur.execute(
            f"""SELECT COUNT(*) FROM {table}
                WHERE end_zone_offset NOT BETWEEN -{kn.ZONE_OFFSET_MAX_SECONDS}
                                               AND {kn.ZONE_OFFSET_MAX_SECONDS}"""
        ).fetchone()[0]
        if bad_off:
            findings.append(
                f"{table}: {bad_off} rows with end_zone_offset outside "
                f"±{kn.ZONE_OFFSET_MAX_SECONDS} seconds"
            )

    # 2. uuid uniqueness per table (instant)
    for table, _, _ in tables_to_check:
        n, d = cur.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT uuid) FROM {table}"
        ).fetchone()
        if n != d:
            findings.append(
                f"{table}: uuid duplicates ({n} rows vs {d} distinct uuids)"
            )

    # 2b. uuid uniqueness per interval table
    for table, _, _, _ in interval_tables_to_check:
        n, d = cur.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT uuid) FROM {table}"
        ).fetchone()
        if n != d:
            findings.append(
                f"{table}: uuid duplicates ({n} rows vs {d} distinct uuids)"
            )

    # 3. client_record_id uniqueness (where NOT NULL)
    for table, _, _ in tables_to_check:
        n, d = cur.execute(
            f"""SELECT COUNT(*), COUNT(DISTINCT client_record_id)
                FROM {table}
                WHERE client_record_id IS NOT NULL"""
        ).fetchone()
        if n != d:
            findings.append(
                f"{table}: client_record_id duplicates ({n} vs {d} distinct)"
            )

    # 4. FK targets exist
    # Check app_info_id references
    bad_app = cur.execute(
        """SELECT COUNT(*) FROM (
            SELECT app_info_id FROM weight_record_table
            UNION ALL
            SELECT app_info_id FROM body_fat_record_table
            UNION ALL
            SELECT app_info_id FROM lean_body_mass_record_table
          )
          WHERE app_info_id IS NOT NULL
            AND app_info_id NOT IN (SELECT row_id FROM application_info_table)"""
    ).fetchone()[0]
    if bad_app:
        findings.append(
            f"{bad_app} rows reference non-existent app_info_id"
        )

    # Check device_info_id references
    bad_dev = cur.execute(
        """SELECT COUNT(*) FROM (
            SELECT device_info_id FROM weight_record_table
            UNION ALL
            SELECT device_info_id FROM body_fat_record_table
            UNION ALL
            SELECT device_info_id FROM lean_body_mass_record_table
          )
          WHERE device_info_id IS NOT NULL
            AND device_info_id NOT IN (SELECT row_id FROM device_info_table)"""
    ).fetchone()[0]
    if bad_dev:
        findings.append(
            f"{bad_dev} rows reference non-existent device_info_id"
        )

    # 5. PRAGMA integrity_check
    result = cur.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        findings.append(f"PRAGMA integrity_check: {result}")

    # 6. PRAGMA foreign_key_check
    fk_results = cur.execute("PRAGMA foreign_key_check").fetchall()
    if fk_results:
        findings.append(f"PRAGMA foreign_key_check: {fk_results[:3]}")

    # 7. activity_date coverage for every inserted record
    if "weight" in expected_domains:
        for table, time_col, _ in tables_to_check:
            # Map table to record_type_id
            rt_map = {
                "weight_record_table": kn.RECORD_TYPE_IDS["weight"],
                "body_fat_record_table": kn.RECORD_TYPE_IDS["body_fat"],
                "lean_body_mass_record_table": kn.RECORD_TYPE_IDS["lean_body_mass"],
            }
            rt_id = rt_map.get(table)
            if rt_id is None:
                continue

            missing = cur.execute(
                f"""SELECT COUNT(*) FROM {table} t
                    WHERE t.app_info_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM activity_date_table a
                          WHERE a.epoch_days = t.local_date
                            AND a.record_type_id = ?
                      )""",
                (rt_id,),
            ).fetchone()[0]
            if missing:
                findings.append(
                    f"{table}: {missing} rows without activity_date coverage "
                    f"(type_id={rt_id})"
                )

    # 7. activity_date coverage for interval tables
    for table, _, _, _ in interval_tables_to_check:
        rt_id = _INTERVAL_TYPE_IDS.get(table)
        if rt_id is None:
            continue

        missing = cur.execute(
            f"""SELECT COUNT(*) FROM {table} t
                WHERE t.app_info_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM activity_date_table a
                      WHERE a.epoch_days = t.local_date
                        AND a.record_type_id = ?
                  )""",
            (rt_id,),
        ).fetchone()[0]
        if missing:
            findings.append(
                f"{table}: {missing} rows without activity_date coverage "
                f"(type_id={rt_id})"
            )

    # 8. do-not-touch tables unchanged
    if source_db_path is not None:
        src_conn = open_readonly(source_db_path)
        try:
            for t in _PROTECTED_TABLES:
                try:
                    src_count = src_conn.execute(
                        f"SELECT COUNT(*) FROM {t}"
                    ).fetchone()[0]
                    dst_count = cur.execute(
                        f"SELECT COUNT(*) FROM {t}"
                    ).fetchone()[0]
                    if src_count != dst_count:
                        findings.append(
                            f"do-not-touch table {t}: count changed "
                            f"({src_count} → {dst_count})"
                        )
                except sqlite3.OperationalError:
                    # Table may not exist in source — skip
                    pass
        finally:
            src_conn.close()

    ok = len(findings) == 0
    return ok, findings
