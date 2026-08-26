"""
cli.py — ghc-db-manager command-line interface.

Usage::

    ghcdb inspect <export.db>
    ghcdb plan   --db DB --source NAME=PATH [--domains weight] [--tz TZ]
                   [--attr NAME=PKG] [--zepp-height CM]
    ghcdb pilot  --db DB --source NAME=PATH [--domains weight] [--tz TZ]
                   [--attr NAME=PKG] [--zepp-height CM] --out PREFIX
    ghcdb build  --db DB --source NAME=PATH [--domains weight] [--tz TZ]
                   [--attr NAME=PKG] [--zepp-height CM] --out PREFIX
    ghcdb validate <modified.db>
    ghcdb diff   <snapshot.db> <fresh.db>
"""

import argparse
import csv
import datetime
import pathlib
import shutil
import sqlite3
import sys
import tempfile

from ghc_db_manager import __version__
from ghc_db_manager.dbio import copy_db, open_readonly, WriteGuard
from ghc_db_manager.knowledge import (
    CLIENT_RECORD_VERSION,
    DEVICE_UNKNOWN_ID,
    RECORDING_METHOD,
    RECORD_TYPE_IDS,
    TABLES,
    dedupe_hash_instant,
    deterministic_uuid,
    local_date_epoch_days,
)
from ghc_db_manager.merge import merge
from ghc_db_manager.packing import pack_zip
from ghc_db_manager.validation.invariants import run_invariants
from ghc_db_manager.writer import write_canonical, write_interval


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TZ = "Europe/Warsaw"
DEFAULT_PROJECT_KEY = "ghc-db-manager"
# Default attribution per domain, by PACKAGE NAME (never rowid — rowids are
# unstable across exports; the v0.1.1 real-data audit caught DEFAULT_APP_INFO_ID=5
# resolving to Google Fit on a real db while meaning the fixture app on fixtures).
# These are the apps that PRODUCED the data (PoC decision); --attr overrides.
ZEPP_PACKAGE = "com.huami.watch.hmwatchmanager"
LIBRA_PACKAGE = "net.cachapa.libra"
DEFAULT_ATTRS = {
    "weight": LIBRA_PACKAGE,
    "activity": ZEPP_PACKAGE,
    "sleep": ZEPP_PACKAGE,
    "heartrate": ZEPP_PACKAGE,
    "exercise": ZEPP_PACKAGE,
}


def _resolve_attr_map(conn, domains, attrs):
    """Resolve per-domain app_info_id from --attr overrides + source-derived
    defaults. Raises with guidance if a package is missing from the db."""
    out = {}
    for domain in domains:
        domain = domain.strip()
        pkg = attrs.get(domain, DEFAULT_ATTRS.get(domain))
        if pkg is None:
            continue
        try:
            out[domain] = _resolve_app_id(conn, pkg)
        except Exception as exc:
            raise ValueError(
                f"Attribution package {pkg!r} for domain {domain!r} not found in "
                f"the export db; pass --attr {domain}=<installed package>"
            ) from exc
    return out


# ---------------------------------------------------------------------------
# Source parsing helpers
# ---------------------------------------------------------------------------

def _parse_sources(args_sources) -> dict[str, str]:
    """Parse --source NAME=PATH arguments into a dict."""
    sources = {}
    if args_sources:
        for spec in args_sources:
            if "=" not in spec:
                raise ValueError(f"Invalid source spec {spec!r}; expected NAME=PATH")
            name, path = spec.split("=", 1)
            sources[name.strip()] = path.strip()
    return sources


def _parse_attrs(args_attr) -> dict[str, str]:
    """Parse --attr NAME=PKG arguments into a dict."""
    attrs = {}
    if args_attr:
        for spec in args_attr:
            if "=" not in spec:
                raise ValueError(f"Invalid attr spec {spec!r}; expected NAME=PKG")
            name, pkg = spec.split("=", 1)
            attrs[name.strip()] = pkg.strip()
    return attrs


# ---------------------------------------------------------------------------
# App ID resolution
# ---------------------------------------------------------------------------

def _resolve_app_id(conn: sqlite3.Connection, package_name: str) -> int:
    """Look up app_info_id for ``package_name`` in the db."""
    row = conn.execute(
        "SELECT row_id FROM application_info_table WHERE package_name = ?",
        (package_name,),
    ).fetchone()
    if row is None:
        raise ValueError(
            f"Package {package_name!r} not found in application_info_table. "
            f"Available packages: "
            f"{[r[0] for r in conn.execute('SELECT package_name FROM application_info_table').fetchall()]}"
        )
    return row[0]


# ---------------------------------------------------------------------------
# Canonical CSV export
# ---------------------------------------------------------------------------

def _write_canonical_csv(
    path: pathlib.Path,
    records: list,
    domains: list[str] | tuple[str, ...],
) -> None:
    """Write canonical records to a CSV file for provenance."""
    if not records:
        return
    fieldnames = [
        "source", "kind", "timestamp_utc", "ms", "zone_offset_seconds",
        "local_date", "value", "unit", "priority", "meta_json",
    ]
    import json
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for rec in records:
            w.writerow({
                "source": rec.source,
                "kind": rec.kind,
                "timestamp_utc": rec.time_utc.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "ms": rec.ms,
                "zone_offset_seconds": rec.zone_offset_seconds,
                "local_date": rec.local_date,
                "value": rec.value,
                "unit": rec.unit,
                "priority": rec.priority,
                "meta_json": json.dumps(rec.meta, ensure_ascii=False),
            })


# ---------------------------------------------------------------------------
# cmd_plan
# ---------------------------------------------------------------------------

def cmd_plan(args: argparse.Namespace) -> int:
    """
    ``ghcdb plan`` — dry run: load sources, run domain rules, report stats.

    No writes to the database.
    """
    sources = _parse_sources(args.sources)
    if not sources:
        print("ERROR: --source is required", file=sys.stderr)
        return 1

    domains = (args.domains or "weight").split(",")

    # Parse tz
    tz = args.tz or DEFAULT_TZ

    # Parse zepp height filter
    zepp_height: float | None = None
    if args.zepp_height is not None:
        zepp_height = float(args.zepp_height)

    # Parse attribution
    attrs = _parse_attrs(args.attr)
    app_info_id = None
    conn_ref = open_readonly(args.db)
    attr_map = _resolve_attr_map(conn_ref, domains, attrs)
    conn_ref.close()
    app_info_id = attr_map.get("weight")
    if app_info_id is None and "weight" in domains:
        raise ValueError(
            "Weight attribution could not be resolved; pass --attr weight=<package>"
        )

    print(f"Plan: db={args.db}")
    print(f"  sources : {sources}")
    print(f"  domains : {domains}")
    print(f"  tz      : {tz}")
    print(f"  zepp height filter: {zepp_height}")
    print()

    all_stats: dict[str, dict[str, int]] = {}
    all_records: dict[str, list] = {}

    for domain in domains:
        domain = domain.strip()
        if domain not in ("weight", "activity", "sleep", "heartrate", "exercise"):
            print(f"Skipping unknown domain {domain!r}", file=sys.stderr)
            continue

        try:
            records, stats = merge(
                sources,
                domain=domain,
                zepp_profile_height=zepp_height,
                hc_db_path=args.db,
                priority_source="libra",
                tz=tz,
            )
        except Exception as exc:
            print(f"ERROR merging domain {domain!r}: {exc}", file=sys.stderr)
            return 1

        all_stats[domain] = stats
        all_records[domain] = records

        print(f"[{domain}]")
        if stats:
            for rule, count in sorted(stats.items()):
                print(f"  {rule}: -{count}")
        else:
            print("  (no rules triggered)")

        # Count per source/kind
        from collections import Counter
        counts = Counter((r.source, r.kind) for r in records)
        for (src, kind), n in sorted(counts.items()):
            print(f"  {src}/{kind}: {n}")
        print(f"  TOTAL: {len(records)} canonical records")
        print()

        # Sample records
        if records:
            print("Sample records (first 5):")
            for rec in records[:5]:
                if hasattr(rec, 'time_utc') and hasattr(rec, 'value'):
                    # Weight-style record
                    print(
                        f"  {rec.source}/{rec.kind}: "
                        f"{rec.time_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                        f"value={rec.value} unit={rec.unit} "
                        f"zone_off={rec.zone_offset_seconds} local_date={rec.local_date}"
                    )
                else:
                    # Interval-style record
                    print(
                        f"  {rec.source}/{rec.kind}: "
                        f"start={rec.start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                        f"end={rec.end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')} "
                        f"local_date={rec.local_date} "
                        f"stages={len(getattr(rec, 'stages', []))} "
                        f"samples={len(getattr(rec, 'samples', []))}"
                    )
        print()

    return 0


# ---------------------------------------------------------------------------
# cmd_build / cmd_pilot shared logic
# ---------------------------------------------------------------------------

def _build_impl(
    args: argparse.Namespace,
    pilot: bool = False,
) -> int:
    """
    Shared implementation for ``build`` and ``pilot`` commands.

    If ``pilot`` is True, only the first record per kind is written
    (same deterministic UUIDs as the full set).
    """
    sources = _parse_sources(args.sources)
    if not sources:
        print("ERROR: --source is required", file=sys.stderr)
        return 1

    if not args.out:
        print("ERROR: --out is required", file=sys.stderr)
        return 1

    out_prefix = pathlib.Path(args.out)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    domains = (args.domains or "weight").split(",")
    tz = args.tz or DEFAULT_TZ
    zepp_height: float | None = float(args.zepp_height) if args.zepp_height else None
    attrs = _parse_attrs(args.attr)

    # Resolve per-domain app_info_id (source-derived defaults, --attr overrides)
    conn_ref = open_readonly(args.db)
    try:
        attr_map = _resolve_attr_map(conn_ref, domains, attrs)
    finally:
        conn_ref.close()
    app_info_id = attr_map.get("weight")

    project_key = DEFAULT_PROJECT_KEY

    # One pinned last_modified_time for the whole build: deterministic reruns,
    # and invariants can scope coverage checks to rows THIS build inserted.
    build_now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

    # Copy source db → working copy
    db_copy = out_prefix.with_suffix(".db")
    copy_db(args.db, db_copy)

    # Open with WriteGuard
    raw_conn = sqlite3.connect(str(db_copy))
    conn = WriteGuard(raw_conn)

    print(f"Building: db_copy={db_copy}")
    print(f"  domains : {domains}")
    print(f"  tz      : {tz}")
    print(f"  pilot   : {pilot}")

    ok = True
    all_records_map: dict[str, list] = {}

    # Separate weight from interval domains
    weight_domains = [d for d in domains if d.strip() == "weight"]
    interval_domains = [d for d in domains if d.strip() in ("activity", "sleep", "heartrate", "exercise")]

    for domain in domains:
        domain = domain.strip()
        if domain == "weight":
            # Weight uses write_canonical
            try:
                records, stats = merge(
                    sources,
                    domain=domain,
                    zepp_profile_height=zepp_height,
                    hc_db_path=args.db,
                    priority_source="libra",
                    tz=tz,
                )
            except Exception as exc:
                print(f"ERROR merging domain {domain!r}: {exc}", file=sys.stderr)
                conn.close()
                return 1

            # In pilot mode: keep only the first record per kind
            if pilot:
                by_kind: dict[str, list] = {}
                for r in records:
                    by_kind.setdefault(r.kind, []).append(r)
                records = [v[0] for v in by_kind.values()]

            print(f"[{domain}]")
            if stats:
                for rule, count in sorted(stats.items()):
                    print(f"  {rule}: -{count}")
            print(f"  writing {len(records)} canonical records (pilot={pilot})")

            try:
                inserted = write_canonical(
                    conn,
                    records,
                    attr_map["weight"],
                    project_key,
                    now_ms=build_now_ms,
                )
                for kind, n in inserted.items():
                    if n > 0:
                        print(f"  inserted {kind}: {n}")
            except Exception as exc:
                print(f"ERROR writing domain {domain!r}: {exc}", file=sys.stderr)
                conn.rollback()
                conn.close()
                return 1

            all_records_map[domain] = records

        elif domain in ("activity", "sleep", "heartrate", "exercise"):
            # Interval domains use write_interval
            try:
                records, stats = merge(
                    sources,
                    domain=domain,
                    zepp_profile_height=zepp_height,
                    hc_db_path=args.db,
                    priority_source="libra",
                    tz=tz,
                )
            except Exception as exc:
                print(f"ERROR merging domain {domain!r}: {exc}", file=sys.stderr)
                conn.close()
                return 1

            # In pilot mode: keep only the FIRST RECORD PER DOMAIN (not per kind)
            if pilot and records:
                records = [records[0]]

            print(f"[{domain}]")
            if stats:
                for rule, count in sorted(stats.items()):
                    print(f"  {rule}: -{count}")
            print(f"  writing {len(records)} canonical records (pilot={pilot})")

            try:
                inserted = write_interval(
                    conn,
                    records,
                    attr_map[domain],
                    project_key,
                    now_ms=build_now_ms,
                )
                for kind, n in inserted.items():
                    if n > 0:
                        print(f"  inserted {kind}: {n}")
            except Exception as exc:
                print(f"ERROR writing domain {domain!r}: {exc}", file=sys.stderr)
                conn.rollback()
                conn.close()
                return 1

            all_records_map[domain] = records

        else:
            print(f"Skipping unknown domain {domain!r}", file=sys.stderr)
            continue

    conn.commit()

    # Write canonical CSV (weight only for now)
    csv_path = out_prefix.with_suffix(".canonical.csv")
    _write_canonical_csv(csv_path, all_records_map.get("weight", []), domains)

    # Run invariants BEFORE packing
    print()
    print("Running invariants...")
    try:
        src_ref = open_readonly(args.db)
        invariants_ok, findings = run_invariants(
            conn,
            expected_domains=[d.strip() for d in domains if d.strip() in (
                "weight", "activity", "sleep", "heartrate", "exercise"
            )],
            source_db_path=args.db,
            build_last_modified_ms=build_now_ms,
        )
        src_ref.close()
    except Exception as exc:
        print(f"ERROR running invariants: {exc}", file=sys.stderr)
        findings = [str(exc)]
        invariants_ok = False

    if not invariants_ok:
        print("INVARIANTS FAILED — aborting before packing:", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)
        conn.close()
        return 1

    print(f"Invariants: PASS")

    # Pack ZIP
    zip_name = out_prefix.with_suffix(".zip")
    # Rename db copy to expected name first
    packed_db_name = "health_connect_export.db"
    import zipfile
    with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(str(db_copy), arcname=packed_db_name)

    print(f"Output: {db_copy}")
    print(f"        {csv_path}")
    print(f"        {zip_name}")

    conn.close()
    return 0


# ---------------------------------------------------------------------------
# cmd_pilot
# ---------------------------------------------------------------------------

def cmd_pilot(args: argparse.Namespace) -> int:
    """``ghcdb pilot`` — build 1-record-per-domain pilot ZIP."""
    return _build_impl(args, pilot=True)


# ---------------------------------------------------------------------------
# cmd_build
# ---------------------------------------------------------------------------

def cmd_build(args: argparse.Namespace) -> int:
    """``ghcdb build`` — build full import ZIP."""
    return _build_impl(args, pilot=False)


# ---------------------------------------------------------------------------
# cmd_validate
# ---------------------------------------------------------------------------

def cmd_validate(args: argparse.Namespace) -> int:
    """
    ``ghcdb validate <modified.db>``

    Run pre-import invariants on a modified DB.

    GAP-13 fix: added --domains flag (default "weight" for backwards compat).
    """
    domains = (args.domains or "weight").split(",")

    try:
        conn = sqlite3.connect(args.db)
    except Exception as exc:
        print(f"ERROR: cannot open {args.db!r}: {exc}", file=sys.stderr)
        return 1

    print(f"Validating: {args.db}")
    ok, findings = run_invariants(conn, expected_domains=domains)

    if ok:
        print("INVARIANTS: PASS")
    else:
        print("INVARIANTS: FAIL", file=sys.stderr)
        for f in findings:
            print(f"  {f}", file=sys.stderr)

    conn.close()
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# cmd_diff
# ---------------------------------------------------------------------------

def cmd_diff(args: argparse.Namespace) -> int:
    """
    ``ghcdb diff`` — post-import diff: compare snapshot vs fresh export.

    --expected-deletions accepts table=N to declare known deletions.
    --no-allow-growth disables default EXPECTED_GROWTH classification.
    """
    import re

    # Parse expected deletions: table=N (repeatable)
    expected_deletions: dict[str, int] = {}
    if args.expected_deletions:
        for spec in args.expected_deletions:
            m = re.match(r"^(\w+)=(\d+)$", spec)
            if not m:
                print(
                    f"ERROR: invalid expected-deletion spec {spec!r}; "
                    f"expected table=N",
                    file=sys.stderr,
                )
                return 1
            expected_deletions[m.group(1)] = int(m.group(2))

    allow_growth = not args.no_allow_growth

    try:
        from ghc_db_manager.validation.diff import diff_databases, render_text
    except ImportError as exc:
        print(f"ERROR: cannot import diff module: {exc}", file=sys.stderr)
        return 1

    try:
        report = diff_databases(
            args.snapshot,
            args.fresh,
            expected_deletions=expected_deletions,
            allow_growth=allow_growth,
        )
    except Exception as exc:
        print(f"ERROR running diff: {exc}", file=sys.stderr)
        return 1

    text = render_text(report)
    print(text)

    if report["verdict"] == "FAIL":
        return 1
    return 0


# ---------------------------------------------------------------------------
# cmd_inspect (existing)
# ---------------------------------------------------------------------------

def cmd_inspect(args: argparse.Namespace) -> int:
    """Existing inspect command."""
    try:
        conn = open_readonly(args.db)
    except Exception as exc:
        print(f"ERROR: cannot open {args.db!r}: {exc}", file=sys.stderr)
        return 1

    uv = conn.execute("PRAGMA user_version").fetchone()[0]
    from ghc_db_manager.knowledge import KNOWN_USER_VERSION
    print(f"Database : {args.db}")
    print(f"user_version: {uv}", end="")
    if uv != KNOWN_USER_VERSION:
        print(f"  ⚠  UNEXPECTED (known good = {KNOWN_USER_VERSION})", file=sys.stderr)
    else:
        print(f"  (matches known value {KNOWN_USER_VERSION})")

    print()
    print("Per-table coverage")
    print("=" * 72)

    cutoffs: dict[str, int | None] = {}

    for domain, (table, time_col, val_col, rec_type, is_interval) in TABLES.items():
        exists = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()[0]
        if not exists:
            print(f"\n  [{domain}] table {table!r} does not exist — skipped")
            cutoffs[domain] = None
            continue

        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        row = conn.execute(f"SELECT MIN({time_col}), MAX({time_col}) FROM {table}").fetchone()
        min_ts, max_ts = row if row else (None, None)

        def fmt_ts(ms: int | None) -> str:
            if ms is None:
                return "N/A"
            utc = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc)
            return utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        print(f"\n  [{domain}] {table}  ({total} rows)")
        print(f"    time column : {time_col}")
        print(f"    min        : {fmt_ts(min_ts)}")
        print(f"    max        : {fmt_ts(max_ts)}")

        app_split = conn.execute(
            f"""SELECT a.package_name, COUNT(*) as cnt
                FROM {table} t
                JOIN application_info_table a ON t.app_info_id = a.row_id
                GROUP BY a.package_name
                ORDER BY cnt DESC
                LIMIT 10"""
        ).fetchall()
        if app_split:
            print(f"    per-app    : ", end="")
            print(", ".join(f"{pkg or '(null)'}={cnt}" for pkg, cnt in app_split))
        else:
            print(f"    per-app    : (no join result)")

        cutoffs[domain] = min_ts

    print()
    print("Computed CUTOFFs (MIN time per domain = nothing at/after this may import)")
    print("=" * 72)
    for domain, cut in cutoffs.items():
        if cut is None:
            print(f"  {domain:<14} : table not found")
        else:
            utc = datetime.datetime.fromtimestamp(cut / 1000, datetime.timezone.utc)
            print(f"  {domain:<14} : {utc.strftime('%Y-%m-%dT%H:%M:%SZ')}")

    conn.close()
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghcdb",
        description="Health Connect database backfill tool.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    # inspect
    p = sub.add_parser("inspect", help="inspect an export database")
    p.add_argument("db", help="path to health_connect_export.db")

    # plan
    p = sub.add_parser("plan", help="dry-run: show what would be imported")
    p.add_argument("--db", required=True, help="path to fresh export db")
    p.add_argument("--source", action="append", dest="sources", metavar="NAME=PATH",
                   help="source (repeatable)")
    p.add_argument("--domains", help="comma-separated domains (default: weight)")
    p.add_argument("--tz", help="IANA timezone (default: Europe/Warsaw)")
    p.add_argument("--attr", action="append", metavar="DOMAIN=PKG",
                   help="attribution package name per domain")
    p.add_argument("--zepp-height", type=float, metavar="CM",
                   help="Zepp profile height in cm for person filter (R1)")

    # pilot
    p = sub.add_parser("pilot", help="build 1-record-per-domain pilot zip")
    p.add_argument("--db", required=True, help="path to fresh export db")
    p.add_argument("--source", action="append", dest="sources", metavar="NAME=PATH",
                   help="source (repeatable)")
    p.add_argument("--domains", help="comma-separated domains (default: weight)")
    p.add_argument("--tz", help="IANA timezone (default: Europe/Warsaw)")
    p.add_argument("--attr", action="append", metavar="DOMAIN=PKG",
                   help="attribution package name per domain")
    p.add_argument("--zepp-height", type=float, metavar="CM",
                   help="Zepp profile height in cm for person filter (R1)")
    p.add_argument("--out", required=True, help="output prefix for pilot files")

    # build
    p = sub.add_parser("build", help="build full import zip")
    p.add_argument("--db", required=True, help="path to fresh export db")
    p.add_argument("--source", action="append", dest="sources", metavar="NAME=PATH",
                   help="source (repeatable)")
    p.add_argument("--domains", help="comma-separated domains (default: weight)")
    p.add_argument("--tz", help="IANA timezone (default: Europe/Warsaw)")
    p.add_argument("--attr", action="append", metavar="DOMAIN=PKG",
                   help="attribution package name per domain")
    p.add_argument("--zepp-height", type=float, metavar="CM",
                   help="Zepp profile height in cm for person filter (R1)")
    p.add_argument("--out", required=True, help="output prefix for build files")

    # validate
    p = sub.add_parser("validate", help="run pre-import invariants on a modified db")
    p.add_argument("db", help="path to modified (working copy) db")
    p.add_argument("--domains", help="comma-separated domains (default: weight)")

    # diff
    p = sub.add_parser("diff", help="post-import diff (Phase E)")
    p.add_argument("snapshot", help="path to pre-import snapshot db")
    p.add_argument("fresh", help="path to post-import fresh export db")
    p.add_argument(
        "--expected-deletions", action="append", dest="expected_deletions",
        metavar="DOMAIN=N",
        help="expected row deletion count per domain (repeatable, e.g. weight=1)")
    p.add_argument(
        "--no-allow-growth", action="store_true",
        help="disable EXPECTED_GROWTH classification for live domains")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        "inspect": cmd_inspect,
        "plan": cmd_plan,
        "pilot": cmd_pilot,
        "build": cmd_build,
        "validate": cmd_validate,
        "diff": cmd_diff,
    }

    return dispatch[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
