"""
validation/diff.py — post-import database diff with expected-deviation model.

Compares a pre-import snapshot database against a post-import fresh export,
reporting data loss, unexpected mutations, and classifying known-deviations
as benign (HC local_date normalization) or expected (live-domain growth,
user-declared deletions).

Evidence basis:
  - README.md §4 point 6: HC recomputes local_date of interval records from
    local START instant — 462 sleep sessions normalized (benign).
  - Parent plan E.6: weight −1 from deliberate UI deletion = EXPECTED_DELETION;
    live domains grow slightly after import (EXPECTED_GROWTH).
"""

import sqlite3
from typing import Optional

from ghc_db_manager import knowledge as kn
from ghc_db_manager.dbio import open_readonly

# ---------------------------------------------------------------------------
# Domain classification
# ---------------------------------------------------------------------------

# Live-domain tables: records can appear in fresh export that were not in
# the snapshot because they were recorded after the snapshot was taken.
# By default, growth in these tables is EXPECTED_GROWTH (not an error).
# Pass --no-allow-growth to disable.
_LIVE_DOMAINS: frozenset[str] = frozenset({
    "steps", "distance", "calories",
    "sleep", "heart_rate", "exercise",
    # activity table names as used in knowledge.TABLES:
    "steps", "distance", "calories",
    "sleep", "heart_rate", "exercise",
})

# Domain → child table for series/stages (None = no child table)
_SERIES_CHILDREN: dict[str, str] = {
    "heart_rate": "heart_rate_record_series_table",
    "sleep": "sleep_stages_table",
}

# Interval tables (local_date recomputed by HC on import — BENIGN_NORMALIZATION)
_INTERVAL_DOMAINS: frozenset[str] = frozenset({
    "steps", "distance", "calories",
    "sleep", "heart_rate", "exercise",
})

# Columns to compare when the same UUID appears in both DBs.
# Per table-type field groups.
_INSTANT_FIELDS = ("time", "zone_offset", "local_date", "weight",
                   "percentage", "mass")
_INTERVAL_FIELDS = ("start_time", "start_zone_offset", "end_time",
                    "end_zone_offset", "local_date", "count", "distance",
                    "energy", "recording_method", "dedupe_hash")
_SLEEP_EXTRA = ("start_time", "start_zone_offset", "end_time",
                 "end_zone_offset", "local_date", "recording_method",
                 "dedupe_hash")


def _table_rowids(conn: sqlite3.Connection, table: str
                  ) -> dict[bytes, dict]:
    """Return {uuid: {col: value, ...}} for all rows in table."""
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [desc[0] for desc in cur.description]
    result = {}
    for row in cur:
        d = dict(zip(cols, row))
        uuid_bytes = d["uuid"]
        result[uuid_bytes] = d
    return result


def _count_delta_class(
    snap_count: int,
    fresh_count: int,
    domain: str,
    expected_deletions: dict[str, int],
    allow_growth: bool,
) -> tuple[int, str]:
    """
    Classify a count delta.

    Returns (delta, classification_label).
    """
    delta = fresh_count - snap_count
    if delta > 0:
        if allow_growth or domain in _LIVE_DOMAINS:
            return delta, "EXPECTED_GROWTH"
        return delta, "UNEXPECTED"
    if delta < 0:
        declared = expected_deletions.get(domain, 0)
        if -delta <= declared:
            return delta, "EXPECTED_DELETION"
        return delta, "UNEXPECTED"
    return 0, "UNCHANGED"


def _compare_row_fields(
    snap_row: dict,
    fresh_row: dict,
    domain: str,
    is_interval: bool,
) -> list[tuple[str, object, object]]:
    """
    Compare non-uuid, non-metadata fields between two rows.

    Returns list of (field_name, snap_value, fresh_value) for fields
    that differ.
    """
    diffs = []
    if is_interval:
        fields = _INTERVAL_FIELDS if domain != "sleep" else _SLEEP_EXTRA
    else:
        fields = _INSTANT_FIELDS

    skip_fields = {"uuid", "row_id", "last_modified_time",
                   "client_record_id", "client_record_version",
                   "device_info_id", "app_info_id",
                   "local_date_time",  # generated — skip
                   "local_date_time_start_time", "local_date_time_end_time",
                   "notes", "title", "exercise_type", "has_route",
                   "parent_key", "stage_start_time", "stage_end_time",
                   "stage_type", "beats_per_minute", "epoch_millis"}

    for f in fields:
        if f in skip_fields:
            continue
        sv = snap_row.get(f)
        fv = fresh_row.get(f)
        if sv != fv:
            diffs.append((f, sv, fv))
    return diffs


def _classify_field_diffs(
    diffs: list[tuple[str, object, object]],
    domain: str,
    is_interval: bool,
) -> str:
    """
    Classify a set of field diffs for a row present in both DBs.

    Returns:
      BENIGN_NORMALIZATION  — only local_date differs (HC recompute on import)
      UNEXPECTED            — any other diff
    """
    if not diffs:
        return "UNCHANGED"
    # Only local_date differs → benign HC normalization
    if all(f == "local_date" for f, _, _ in diffs):
        if is_interval and domain in _INTERVAL_DOMAINS:
            return "BENIGN_NORMALIZATION"
    return "UNEXPECTED"


def _load_table_data(
    conn: sqlite3.Connection, domain: str,
) -> tuple[dict[bytes, dict], int]:
    """Load all UUID-indexed rows and total count for a domain's table."""
    table = kn.TABLES[domain][0]
    rows = _table_rowids(conn, table)
    count = len(rows)
    return rows, count


def _load_series_counts(
    conn: sqlite3.Connection,
    parent_table: str,
    series_table: str,
    parent_uuids: set[bytes],
) -> dict[bytes, int]:
    """
    For each parent UUID, return the count of child rows (series/stages).

    Uses parent_key = row_id of the parent table.
    """
    if not parent_uuids:
        return {}
    # Build uuid→rowid map for the parent table
    uuid_to_rowid: dict[bytes, int] = {}
    cur = conn.execute(
        f"SELECT row_id, uuid FROM {parent_table} WHERE uuid IN ({','.join('?' * len(parent_uuids))})",
        list(parent_uuids),
    )
    for rowid, uuidb in cur.fetchall():
        uuid_to_rowid[uuidb] = rowid

    if not uuid_to_rowid:
        return {}

    # Count child rows per parent rowid
    rowid_list = list(uuid_to_rowid.values())
    placeholders = ",".join("?" * len(rowid_list))
    cur = conn.execute(
        f"""SELECT s.parent_key, COUNT(*) as cnt
            FROM {series_table} s
            WHERE s.parent_key IN ({placeholders})
            GROUP BY s.parent_key""",
        rowid_list,
    )
    # Map back to uuid
    result: dict[bytes, int] = {}
    for parent_key, cnt in cur.fetchall():
        for uuidb, rid in uuid_to_rowid.items():
            if rid == parent_key:
                result[uuidb] = cnt
                break
    return result


def diff_databases(
    snapshot_path: str,
    fresh_path: str,
    imported_app_packages: Optional[list[str]] = None,
    expected_deletions: Optional[dict[str, int]] = None,
    allow_growth: bool = True,
) -> dict:
    """
    Compare a pre-import snapshot DB against a post-import fresh export DB.

    Parameters
    ----------
    snapshot_path : str
        Path to the pre-import database (the "snapshot").
    fresh_path : str
        Path to the post-import database (the "fresh" export).
    imported_app_packages : list[str] | None
        App package names that were used during import.
        If None, defaults to all domains having growth allowed.
    expected_deletions : dict[str, int] | None
        Domain → expected deletion count, e.g. ``{"weight": 1}``.
        Entries here are accepted as EXPECTED_DELETION rather than data loss.
    allow_growth : bool
        If True (default), count increases in any domain are classified as
        EXPECTED_GROWTH (user added new records after import).
        If False, growth is classified as UNEXPECTED.

    Returns
    -------
    dict
        A structured report with keys:
          - ``verdict``: "PASS" | "PASS_WITH_EXPECTED_DEVIATIONS" | "FAIL"
          - ``by_domain``: dict[domain, dict] with per-domain findings
          - ``expected_deviations``: list[str] (only when verdict is
            PASS_WITH_EXPECTED_DEVIATIONS)
          - ``unexpected_findings``: list[str] (only when verdict is FAIL)
          - ``snap_path``, ``fresh_path``
    """
    if expected_deletions is None:
        expected_deletions = {}
    snap_conn = open_readonly(snapshot_path)
    fresh_conn = open_readonly(fresh_path)

    try:
        report: dict = {
            "verdict": "PASS",
            "by_domain": {},
            "expected_deviations": [],
            "unexpected_findings": [],
            "snap_path": snapshot_path,
            "fresh_path": fresh_path,
            "allow_growth": allow_growth,
            "expected_deletions": dict(expected_deletions),
        }

        for domain in kn.TABLES:
            table, time_col, val_col, rec_type, is_interval = kn.TABLES[domain]

            # --- Count comparison ---
            snap_count = snap_conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            fresh_count = fresh_conn.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            delta, delta_class = _count_delta_class(
                snap_count, fresh_count, domain,
                expected_deletions, allow_growth,
            )

            # --- Load UUID-keyed rows ---
            snap_rows, _ = _load_table_data(snap_conn, domain)
            fresh_rows, _ = _load_table_data(fresh_conn, domain)

            snap_uuids = set(snap_rows.keys())
            fresh_uuids = set(fresh_rows.keys())

            # Rows in snapshot but not in fresh = data loss
            missing_uuids = snap_uuids - fresh_uuids
            # Rows in fresh but not in snapshot = new rows (growth)
            new_uuids = fresh_uuids - snap_uuids
            common_uuids = snap_uuids & fresh_uuids

            # Classify field diffs for common rows
            benign_norm = []
            unexpected_field_diffs = []
            for uuidb in common_uuids:
                diffs = _compare_row_fields(
                    snap_rows[uuidb], fresh_rows[uuidb],
                    domain, is_interval,
                )
                cls = _classify_field_diffs(diffs, domain, is_interval)
                if cls == "BENIGN_NORMALIZATION":
                    benign_norm.append(uuidb)
                elif cls == "UNEXPECTED":
                    unexpected_field_diffs.append((uuidb, diffs))

            # --- Series / stage integrity ---
            series_changes = []
            if domain in _SERIES_CHILDREN:
                series_table = _SERIES_CHILDREN[domain]
                snap_series_counts = _load_series_counts(
                    snap_conn, table, series_table, snap_uuids)
                fresh_series_counts = _load_series_counts(
                    fresh_conn, table, series_table, fresh_uuids)
                for uuidb in common_uuids:
                    snap_sc = snap_series_counts.get(uuidb, 0)
                    fresh_sc = fresh_series_counts.get(uuidb, 0)
                    if snap_sc != fresh_sc:
                        series_changes.append((uuidb, snap_sc, fresh_sc))

            # --- Per-app counts (weight table only) ---
            app_counts = {}
            if domain == "weight" and fresh_count > 0:
                cur = fresh_conn.execute(
                    f"""SELECT a.package_name, COUNT(*) as cnt
                        FROM {table} t
                        JOIN application_info_table a
                          ON t.app_info_id = a.row_id
                        GROUP BY a.package_name""")
                app_counts = {r[0] or "(null)": r[1] for r in cur.fetchall()}

            # --- Ranges (min/max timestamps and value column) ---
            ranges: dict[str, object] = {}
            try:
                row = fresh_conn.execute(
                    f"""SELECT
                           MIN({time_col}),
                           MAX({time_col}),
                           MIN({val_col}),
                           MAX({val_col})
                       FROM {table}"""
                ).fetchone()
                if row:
                    ranges = {
                        "min_time": row[0],
                        "max_time": row[1],
                        f"min_{val_col}": row[2] if val_col else None,
                        f"max_{val_col}": row[3] if val_col else None,
                    }
            except Exception:
                pass

            # --- Build per-domain report section ---
            domain_report: dict = {
                "table": table,
                "snap_count": snap_count,
                "fresh_count": fresh_count,
                "delta": delta,
                "delta_class": delta_class,
                "missing_uuids": len(missing_uuids),
                "new_uuids": len(new_uuids),
                "benign_normalization": len(benign_norm),
                "unexpected_field_diffs": len(unexpected_field_diffs),
                "unexpected_field_diff_details": unexpected_field_diffs[:5],
                "series_changes": series_changes,
                "app_counts": app_counts,
                "ranges": ranges,
            }

            # Track deviations
            if delta_class == "EXPECTED_GROWTH" and delta > 0:
                report["expected_deviations"].append(
                    f"{domain}: +{delta} rows (EXPECTED_GROWTH)")
            if delta_class == "EXPECTED_DELETION":
                report["expected_deviations"].append(
                    f"{domain}: {delta} rows (EXPECTED_DELETION, declared)")
            if benign_norm:
                report["expected_deviations"].append(
                    f"{domain}: {len(benign_norm)} rows with local_date-only "
                    "diff (BENIGN_NORMALIZATION — HC recomputes from local start)")

            # Track unexpected findings
            if delta_class == "UNEXPECTED":
                report["unexpected_findings"].append(
                    f"{domain}: unexpected count delta {delta}")
            if missing_uuids and delta_class != "EXPECTED_DELETION":
                # Only flag data loss as unexpected if the deletion was NOT
                # declared via expected_deletions.  When delta_class is
                # EXPECTED_DELETION, the missing rows are explained by the
                # user's declared deletions.
                report["unexpected_findings"].append(
                    f"{domain}: {len(missing_uuids)} rows missing in fresh "
                    "(data loss — not declared as expected deletion)")
            if unexpected_field_diffs:
                for uuidb, diffs in unexpected_field_diffs:
                    snap_ts = snap_rows[uuidb].get(time_col, "?")
                    report["unexpected_findings"].append(
                        f"{domain}: row {uuidb.hex()[:8]}… "
                        f"field diffs: "
                        f"{[(f, sv, fv) for f, sv, fv in diffs]}")
            if series_changes:
                for uuidb, snap_sc, fresh_sc in series_changes:
                    report["unexpected_findings"].append(
                        f"{domain}: row {uuidb.hex()[:8]}… "
                        f"series/stage count changed {snap_sc}→{fresh_sc}")

            report["by_domain"][domain] = domain_report

        # --- Compute verdict ---
        if report["unexpected_findings"]:
            report["verdict"] = "FAIL"
        elif report["expected_deviations"]:
            report["verdict"] = "PASS_WITH_EXPECTED_DEVIATIONS"
        else:
            report["verdict"] = "PASS"

        return report

    finally:
        snap_conn.close()
        fresh_conn.close()


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def render_text(report: dict) -> str:
    """
    Render a diff report as human-readable text.

    Parameters
    ----------
    report : dict
        The return value of ``diff_databases``.

    Returns
    -------
    str
        Multi-line human-readable text.
    """
    lines = []
    verdict = report["verdict"]
    lines.append("=" * 72)
    lines.append(f"DIFF REPORT  verdict: {verdict}")
    lines.append("=" * 72)
    lines.append(f"  snapshot : {report['snap_path']}")
    lines.append(f"  fresh    : {report['fresh_path']}")
    lines.append(f"  allow_growth: {report['allow_growth']}")
    if report["expected_deletions"]:
        lines.append(f"  expected_deletions: {report['expected_deletions']}")
    lines.append("")

    # Verdict summary
    if verdict == "PASS":
        lines.append("RESULT: PASS — snapshot and fresh are consistent.")
    elif verdict == "PASS_WITH_EXPECTED_DEVIATIONS":
        lines.append("RESULT: PASS_WITH_EXPECTED_DEVIATIONS")
        lines.append("")
        lines.append("Expected deviations (benign / declared):")
        for dev in report["expected_deviations"]:
            lines.append(f"  • {dev}")
    else:  # FAIL
        lines.append("RESULT: FAIL — unexpected findings detected:")
        for finding in report["unexpected_findings"]:
            lines.append(f"  ✗ {finding}")
        lines.append("")
        lines.append("Expected deviations:")
        for dev in report["expected_deviations"]:
            lines.append(f"  • {dev}")

    lines.append("")
    lines.append("-" * 72)
    lines.append("PER-DOMAIN DETAILS")
    lines.append("-" * 72)

    for domain, dr in report["by_domain"].items():
        table = dr["table"]
        lines.append(f"")
        lines.append(f"[{domain}] {table}")
        lines.append(
            f"  count: snapshot={dr['snap_count']}  "
            f"fresh={dr['fresh_count']}  "
            f"delta={dr['delta']}  [{dr['delta_class']}]"
        )
        if dr["missing_uuids"]:
            lines.append(f"  data loss: {dr['missing_uuids']} rows absent from fresh")
        if dr["new_uuids"]:
            lines.append(f"  new rows: {dr['new_uuids']} rows absent from snapshot")
        if dr["benign_normalization"]:
            lines.append(
                f"  local_date-only diffs: {dr['benign_normalization']} rows "
                f"(BENIGN_NORMALIZATION — HC recomputes from local start)"
            )
        if dr["unexpected_field_diffs"]:
            lines.append(
                f"  unexpected field diffs: {dr['unexpected_field_diffs']} rows"
            )
            for uuidb, diffs in dr["unexpected_field_diff_details"]:
                lines.append(
                    f"    uuid={uuidb.hex()[:16]}…: "
                    f"{[(f, sv, fv) for f, sv, fv in diffs]}"
                )
        if dr["series_changes"]:
            lines.append(f"  series/stage changes: {len(dr['series_changes'])}")
            for uuidb, snap_sc, fresh_sc in dr["series_changes"][:5]:
                lines.append(
                    f"    uuid={uuidb.hex()[:16]}…: "
                    f"{snap_sc}→{fresh_sc} samples"
                )
        if dr["app_counts"]:
            lines.append(f"  per-app counts (fresh):")
            for pkg, cnt in sorted(dr["app_counts"].items()):
                lines.append(f"    {pkg}: {cnt}")
        if dr["ranges"]:
            def fmt_ts(ms):
                if ms is None:
                    return "N/A"
                import datetime
                return datetime.datetime.fromtimestamp(
                    ms / 1000, datetime.timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            rng = dr["ranges"]
            val_key = [k for k in rng if k.startswith("min_") and k != "min_time"]
            lines.append(f"  ranges (fresh):")
            lines.append(
                f"    time: {fmt_ts(rng.get('min_time'))} "
                f"→ {fmt_ts(rng.get('max_time'))}"
            )
            if val_key:
                vk = val_key[0]
                lines.append(
                    f"    {vk[4:]}: {rng.get(f'min_{vk[4:]}')} "
                    f"→ {rng.get(f'max_{vk[4:]}')}"
                )

    lines.append("")
    lines.append("=" * 72)
    return "\n".join(lines)
