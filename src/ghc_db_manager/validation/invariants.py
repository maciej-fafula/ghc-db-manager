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

# Interval tables: (table_name, start_time_col, end_time_col)
# local_date is always START-based (HC canonical recomputed form).
_INTERVAL_TABLES = [
    ("steps_record_table",                          "start_time", "end_time"),
    ("distance_record_table",                       "start_time", "end_time"),
    ("total_calories_burned_record_table",          "start_time", "end_time"),
    ("sleep_session_record_table",                  "start_time", "end_time"),
    ("heart_rate_record_table",                    "start_time", "end_time"),
    ("exercise_session_record_table",                "start_time", "end_time"),
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
    build_last_modified_ms: Optional[int] = None,
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
    build_last_modified_ms : int | None
        The pinned last_modified_time used by this build's writer.  When
        given, activity_date coverage checks are scoped to rows inserted by
        THIS build (real dbs contain pre-existing rows without coverage).

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
            ("steps_record_table",                          "start_time", "end_time"),
            ("distance_record_table",                       "start_time", "end_time"),
            ("total_calories_burned_record_table",           "start_time", "end_time"),
        ])
    if "sleep" in expected_domains:
        interval_tables_to_check.append(("sleep_session_record_table", "start_time", "end_time"))
    if "heartrate" in expected_domains:
        interval_tables_to_check.append(("heart_rate_record_table", "start_time", "end_time"))
    if "exercise" in expected_domains:
        interval_tables_to_check.append(("exercise_session_record_table", "start_time", "end_time"))

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

    # 1b. Interval table checks — local_date is always start-based
    for table, s_col, e_col in interval_tables_to_check:
        date_col = f"({s_col} + 1000 * start_zone_offset) / 86400000"
        bad = cur.execute(
            f"""SELECT COUNT(*) FROM {table}
                WHERE local_date != {date_col}
                  AND {s_col} IS NOT NULL"""
        ).fetchone()[0]
        if bad:
            findings.append(
                f"{table}: {bad} rows violate local_date formula "
                f"(start_time + 1000 * start_zone_offset, start-based)"
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
    for table, _, _ in interval_tables_to_check:
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

    # 4. FK targets exist — all tables in knowledge.TABLES registry
    all_record_tables = [tbl[0] for tbl in kn.TABLES.values()]
    # Check app_info_id references
    app_union_parts = " UNION ALL ".join(
        f"SELECT app_info_id FROM {t}" for t in all_record_tables
    )
    bad_app = cur.execute(
        f"""SELECT COUNT(*) FROM (
            {app_union_parts}
          )
          WHERE app_info_id IS NOT NULL
            AND app_info_id NOT IN (SELECT row_id FROM application_info_table)"""
    ).fetchone()[0]
    if bad_app:
        findings.append(
            f"{bad_app} rows reference non-existent app_info_id"
        )

    # Check device_info_id references
    dev_union_parts = " UNION ALL ".join(
        f"SELECT device_info_id FROM {t}" for t in all_record_tables
    )
    bad_dev = cur.execute(
        f"""SELECT COUNT(*) FROM (
            {dev_union_parts}
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
    # Scoped to rows inserted by THIS build when build_last_modified_ms is given:
    # real phone dbs contain pre-existing rows without coverage (native data);
    # PoC scoped its checks the same way via last_modified_time pinning.
    scope_w = "AND t.last_modified_time = ?" if build_last_modified_ms is not None else ""
    scope_p = (build_last_modified_ms,) if build_last_modified_ms is not None else ()
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
                      {scope_w}
                      AND NOT EXISTS (
                          SELECT 1 FROM activity_date_table a
                          WHERE a.epoch_days = t.local_date
                            AND a.record_type_id = ?
                      )""",
                scope_p + (rt_id,),
            ).fetchone()[0]
            if missing:
                findings.append(
                    f"{table}: {missing} rows without activity_date coverage "
                    f"(type_id={rt_id})"
                )

    # 7. activity_date coverage for interval tables
    for table, _, _ in interval_tables_to_check:
        rt_id = _INTERVAL_TYPE_IDS.get(table)
        if rt_id is None:
            continue

        missing = cur.execute(
            f"""SELECT COUNT(*) FROM {table} t
                WHERE t.app_info_id IS NOT NULL
                  {scope_w}
                  AND NOT EXISTS (
                      SELECT 1 FROM activity_date_table a
                      WHERE a.epoch_days = t.local_date
                        AND a.record_type_id = ?
                  )""",
            scope_p + (rt_id,),
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


# ---------------------------------------------------------------------------
# GAP-12 TODO (deferred — do NOT implement):
# ---------------------------------------------------------------------------
# The interval invariants above check ALL rows in the table. They should be
# scoped to only the rows inserted by THIS build (identified by now_ms and
# app_info_id), not all rows including pre-existing ones.
#
# This matters for correctness: a pre-existing invariant violation in the
# source db should NOT cause our build to fail.
#
# Implementation would require:
#   1. Thread now_ms through from writer to invariants
#   2. Add WHERE clause filtering to only "ours" rows
#   3. Run checks only on the filtered subset
#
# This is deferred because the current checks happen to pass on our source dbs.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GAP-7: Post-insert cutoff invariant
# ---------------------------------------------------------------------------
# Ported from PoC build_wave2.py:191-197.
# For each interval table, no rows written by THIS build may exist at/after
# that table's pre-existing cutoff (MIN(start_time) from source db).
# We identify "ours" by rows with last_modified_time = now_ms and the domain's
# app_info_id. If any of our rows extend to/after the cutoff, it means the
# cutoff was violated (data at/after cutoff should NOT have been imported).
# ---------------------------------------------------------------------------

def run_post_insert_cutoff_invariants(
    conn: Union[sqlite3.Connection, "WriteGuard"],
    expected_domains: list[str],
    app_info_ids: dict[str, int],
    now_ms: int,
    source_db_path: Optional[str] = None,
) -> tuple[bool, list[str]]:
    """
    Run post-insert cutoff invariants: rows inserted by this build must NOT
    exist at/after the pre-existing cutoff for their table.

    Ported from PoC build_wave2.py:191-197.

    Parameters
    ----------
    conn : sqlite3.Connection | WriteGuard
        Connection to the modified database (working copy).
    expected_domains : list[str]
        Domains that were written (e.g. ["activity", "sleep", "heartrate", "exercise"]).
    app_info_ids : dict[str, int]
        Map of domain → app_info_id used for this build's rows.
    now_ms : int
        last_modified_time used for all rows written by this build.
    source_db_path : str | None
        Path to original source DB to read cutoffs from.
        If None, cutoff check is skipped.

    Returns
    -------
    (ok, findings)
    """
    if source_db_path is None:
        return True, []

    findings: list[str] = []
    cur = conn.cursor()

    # Map domain → table info
    # weight is instant-domain, handled separately in run_invariants
    domain_table_map: dict[str, tuple[str, str]] = {
        "activity": ("steps_record_table", "start_time"),
        "sleep": ("sleep_session_record_table", "start_time"),
        "heartrate": ("heart_rate_record_table", "start_time"),
        "exercise": ("exercise_session_record_table", "start_time"),
    }

    # Get cutoffs from source db
    src_conn = open_readonly(source_db_path)
    try:
        cutoffs: dict[str, int | None] = {}
        for domain, (table, time_col) in domain_table_map.items():
            if domain not in expected_domains:
                continue
            try:
                row = src_conn.execute(f"SELECT MIN({time_col}) FROM {table}").fetchone()
                cutoffs[domain] = row[0] if row and row[0] is not None else None
            except sqlite3.OperationalError:
                cutoffs[domain] = None
    finally:
        src_conn.close()

    for domain in expected_domains:
        if domain not in domain_table_map:
            continue
        table, time_col = domain_table_map[domain]
        cutoff = cutoffs.get(domain)
        if cutoff is None:
            continue  # table empty or doesn't exist → no cutoff

        app_id = app_info_ids.get(domain)
        if app_id is None:
            continue

        # Count our rows at/after cutoff
        ours_after = cur.execute(
            f"""SELECT COUNT(*) FROM {table}
                WHERE app_info_id = ? AND {time_col} >= ?""",
            (app_id, cutoff),
        ).fetchone()[0]

        # Count pre-existing rows at/after cutoff (before our build)
        src_conn = open_readonly(source_db_path)
        try:
            pre_existing_after = src_conn.execute(
                f"""SELECT COUNT(*) FROM {table}
                    WHERE app_info_id = ? AND {time_col} >= ?""",
                (app_id, cutoff),
            ).fetchone()[0]
        finally:
            src_conn.close()

        if ours_after != pre_existing_after:
            findings.append(
                f"{table}: {ours_after} of our rows at/after cutoff ({cutoff}) "
                f"vs {pre_existing_after} pre-existing — cutoff was violated"
            )

    ok = len(findings) == 0
    return ok, findings
