"""
sources/libra.py — Libra CSV adapter.

Parses a Libra export CSV (semicolon-delimited, UTF-8 with BOM, # comment
header lines) and emits RawRecords for weight, body_fat, and lean_mass.

Libra CSV format (per PoC):
  - First lines: #Version: N, #Units: kg, #date;weight;weight trend;body fat;...
  - Data lines: ISO-8601 UTC datetime;semicolon;values...
  - "date-only" entries are stored as local midnight in UTC:
      summer → 22:00:00.000Z  → zone offset +7200 s
      winter → 23:00:00.000Z  → zone offset +3600 s
  - body fat / muscle mass columns may be empty.
"""

import csv
import datetime
from pathlib import Path
from typing import TextIO

from ghc_db_manager.sources import RawRecord, register


# The expected header line starts with "#date;" (the '#' is a comment in the file).
# After stripping the leading '#' from comment lines we get the actual CSV header.
EXPECTED_HEADER_PREFIX = "date"


def _parse_timestamp(date_str: str) -> datetime.datetime:
    """Parse a Libra date string to a UTC-aware datetime."""
    # Libra uses ISO-8601 with Z suffix: 2020-03-01T22:00:00.000Z
    return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))


def _is_local_midnight(dt: datetime.datetime) -> bool:
    """Return True if this is a local-midnight entry (22:00 or 23:00 UTC)."""
    return (
        dt.hour in (22, 23)
        and dt.minute == 0
        and dt.second == 0
        and dt.microsecond == 0
    )


def _strip_comment_lines(f: TextIO) -> list[str]:
    """
    Read all lines from ``f``, stripping leading '#' from comment lines,
    skipping blank lines, and removing the #Version and #Units header lines.
    Returns a list of remaining data-line strings.
    """
    raw_lines = f.read().splitlines()
    result = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#Version") or stripped.startswith("#Units"):
            continue
        if stripped.startswith("#"):
            # Header line(s) — strip the leading '#'
            result.append(stripped[1:].strip())
        else:
            result.append(stripped)
    return result


def _parse_body_fat(value_str: str) -> float | None:
    """Return float body fat or None if empty."""
    v = value_str.strip()
    return float(v) if v else None


def parse(path: str) -> list[RawRecord]:
    """
    Parse a Libra CSV file and return a list of RawRecords.

    Emits one weight record per row, plus optionally a body_fat and/or
    lean_mass record when those columns are non-empty and positive.
    """
    records: list[RawRecord] = []

    with open(path, encoding="utf-8-sig") as f:
        data_lines = _strip_comment_lines(f)

    if not data_lines:
        return records

    reader = csv.DictReader(data_lines, delimiter=";")
    # Normalise field names (DictReader lower-cases keys in some Python versions
    # but we be defensive)
    header_field = "date"  # the first column name after comment stripping

    for row in reader:
        date_str = row.get(header_field, row.get("date", "")).strip()
        if not date_str:
            continue

        dt = _parse_timestamp(date_str)

        weight_str = row.get("weight", "").strip()
        if not weight_str:
            continue
        weight_kg = float(weight_str)

        # Body fat (percent)
        bf_raw = row.get("body fat", "").strip()
        bf_value: float | None = None
        if bf_raw:
            try:
                bf_value = float(bf_raw)
            except ValueError:
                pass

        # Muscle mass (kg)
        mm_str = row.get("muscle mass", "").strip()
        mm_value: float | None = None
        if mm_str:
            try:
                mm_value = float(mm_str)
            except ValueError:
                pass

        # Meta — preserve original trend values and raw fields for provenance
        meta = {
            "weight_trend": row.get("weight trend", "").strip() or None,
            "bf_raw": bf_raw or None,
            "bf_trend": row.get("body fat trend", "").strip() or None,
            "mm_raw": mm_str or None,
            "mm_trend": row.get("muscle mass trend", "").strip() or None,
            "is_local_midnight": _is_local_midnight(dt),
        }

        raw_fields = {k: (v.strip() if isinstance(v, str) else str(v)) for k, v in row.items() if k is not None}

        # Weight record — always emitted
        records.append(
            RawRecord(
                source="libra",
                kind="weight",
                time_utc=dt,
                value=weight_kg,
                meta=meta,
                raw=raw_fields,
            )
        )

        # Body fat record — only if non-empty and positive
        if bf_value is not None and bf_value > 0:
            records.append(
                RawRecord(
                    source="libra",
                    kind="body_fat",
                    time_utc=dt,
                    value=bf_value,
                    meta=meta,
                    raw=raw_fields,
                )
            )

        # Lean mass record — only if non-empty and positive
        if mm_value is not None and mm_value > 0:
            records.append(
                RawRecord(
                    source="libra",
                    kind="lean_mass",
                    time_utc=dt,
                    value=mm_value,
                    meta=meta,
                    raw=raw_fields,
                )
            )

    return records


# Register under the standard name
register("libra", parse)
