"""
sources/generic_csv.py — generic CSV adapter driven by an explicit column-mapping file.

Adapter for arbitrary CSV exports.  Driven by an explicit mapping file (JSON) the user
provides.  Only instant kinds are supported (weight/body_fat/lean_mass).

Mapping file format (JSON)::

    {
        "columns": {
            "time":        "Timestamp",
            "weight_kg":   "Weight (kg)",
            "body_fat":    "Fat %",
            "lean_mass":   "Muscle (kg)"
        },
        "time_format": "auto",          # "auto" | "ISO8601" | strptime pattern
        "tz":          "Europe/Warsaw", # IANA timezone
        "encoding":    "utf-8-sig",     # passed to open()
        "delimiter":   ","              # CSV delimiter
    }

Refuses ambiguous input:
  - Every key in "columns" MUST appear in the CSV header.
  - Numeric fields MUST have unit suffix: weight_kg, body_fat, lean_mass.
  - Unparseable timestamps → error listing offending rows (first 5).
"""

import csv
import datetime
import json
import pathlib
from typing import Any
from zoneinfo import ZoneInfo as _ZoneInfo

from ghc_db_manager.sources import RawRecord, register


# ---------------------------------------------------------------------------
# Mapping file validation
# ---------------------------------------------------------------------------

# Fields that must be present
_REQUIRED_COLUMNS = ("time",)

# Numeric unit suffixes required for each kind
_UNIT_SUFFIX: dict[str, str] = {
    "weight_kg":   "kg",
    "body_fat":    "percent",
    "lean_mass":   "kg",
}

# Kinds that the generic adapter may emit
_VALID_KINDS = frozenset({"weight", "body_fat", "lean_mass"})


class MappingError(ValueError):
    """
    Raised when the mapping file or CSV is ambiguous / malformed.
    Errors are structured so callers can present a clear list of problems.
    """

    def __init__(
        self,
        message: str,
        *,
        row_errors: list[tuple[int, str]] | None = None,
    ):
        super().__init__(message)
        self.row_errors = row_errors or []


def _load_mapping(mapping_path: str) -> dict[str, Any]:
    """
    Load and validate a generic CSV mapping JSON file.

    Raises MappingError on any validation failure.
    """
    try:
        with open(mapping_path, encoding="utf-8") as fh:
            mapping: dict[str, Any] = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise MappingError(
            f"Cannot load mapping file {mapping_path!r}: {exc}"
        ) from exc

    if "columns" not in mapping:
        raise MappingError("Mapping file must contain a 'columns' key.")

    columns: dict[str, str] = mapping["columns"]

    # Required columns
    for key in _REQUIRED_COLUMNS:
        if key not in columns:
            raise MappingError(
                f"Missing required column mapping {key!r} in 'columns'. "
                f"Declared columns: {list(columns.keys())}"
            )

    # Numeric field names (keys) must have the correct unit suffix.
    # The value is the CSV column name (what appears in the CSV header).
    ambiguous: list[str] = []
    for field_name in columns:
        if field_name == "weight":
            ambiguous.append(
                "  Field name 'weight' is ambiguous — declare 'weight_kg' "
                "(unit suffix required to avoid kg/pounds confusion)"
            )
        elif field_name == "fat" or field_name == "body_fat_percent":
            ambiguous.append(
                f"  Field name {field_name!r} should be 'body_fat' "
                "(unit suffix required)"
            )

    if ambiguous:
        raise MappingError(
            "Ambiguous column names in mapping:\n" + "\n".join(ambiguous)
        )

    return mapping


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

_AUTO_FORMATS = [
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
]


def _parse_timestamp(
    value: str,
    time_format: str,
    tz_name: str,
) -> datetime.datetime:
    """
    Parse a timestamp string and return a tz-aware UTC datetime.

    ``time_format`` may be:
      - "auto"   — try AUTO_FORMATS in order
      - "ISO8601" — use datetime.fromisoformat (handles ±HH:MM suffixes)
      - anything else — passed to datetime.strptime directly
    """
    try:
        tz = _ZoneInfo(tz_name)
    except Exception:
        raise MappingError(f"Unknown IANA timezone: {tz_name!r}") from None

    if time_format == "auto":
        # Try each format
        for fmt in _AUTO_FORMATS:
            try:
                naive = datetime.datetime.strptime(value, fmt)
                return _local_to_utc_naive(tz, naive)
            except ValueError:
                continue
        # Last resort: ISO8601
        try:
            normalised = value.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(normalised)
            if dt.tzinfo is None:
                dt = _local_to_utc_naive(tz, dt)
            return dt
        except ValueError as exc:
            raise ValueError(
                f"Cannot parse timestamp {value!r} with any auto format"
            ) from exc

    elif time_format.upper() == "ISO8601":
        normalised = value.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = _local_to_utc_naive(tz, dt)
        return dt

    else:
        naive = datetime.datetime.strptime(value, time_format)
        return _local_to_utc_naive(tz, naive)


def _local_to_utc_naive(
    tz: "_ZoneInfo",
    local_dt: datetime.datetime,
) -> datetime.datetime:
    """
    Convert a naive local datetime to a tz-aware UTC datetime.

    The naive datetime is assumed to be in the given timezone; we look up
    its UTC offset and subtract to get UTC.
    """
    aware = local_dt.replace(tzinfo=tz)
    utc_offset = aware.utcoffset()
    assert utc_offset is not None
    utc_naive = (aware - utc_offset).replace(tzinfo=None)
    return utc_naive.replace(tzinfo=datetime.timezone.utc)


# ---------------------------------------------------------------------------
# Plausibility helpers
# ---------------------------------------------------------------------------

def _plausibility_filter(kind: str, value: float) -> bool:
    """Return True if value is in plausible range for the kind."""
    if kind == "weight":
        return 20.0 <= value <= 300.0
    elif kind == "body_fat":
        return 1.0 <= value <= 60.0
    elif kind == "lean_mass":
        return 10.0 <= value <= 200.0
    return True


# ---------------------------------------------------------------------------
# Main parse function
# ---------------------------------------------------------------------------

def parse_with_mapping(
    csv_path: str,
    mapping: dict[str, Any],
) -> list[RawRecord]:
    """
    Parse a CSV file using an already-loaded mapping dict.

    Returns a list of RawRecords for weight, body_fat, and lean_mass.

    Raises MappingError if the CSV cannot be parsed or the mapping is violated.
    """
    encoding = mapping.get("encoding", "utf-8-sig")
    delimiter = mapping.get("delimiter", ",")
    tz_name = mapping.get("tz", "Europe/Warsaw")
    time_format = mapping.get("time_format", "auto")

    columns: dict[str, str] = mapping["columns"]

    records: list[RawRecord] = []
    row_errors: list[tuple[int, str]] = []

    with open(csv_path, encoding=encoding) as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        header = reader.fieldnames or []

        # Validate: every declared column must appear in the CSV
        missing: list[str] = []
        for field_name in columns.values():
            if field_name not in header:
                missing.append(field_name)
        if missing:
            raise MappingError(
                f"Declared column(s) not found in CSV: {missing}. "
                f"CSV headers: {header}"
            )

        for row_num, row in enumerate(reader, start=2):  # row 1 is header
            # ---- time ----
            time_col = columns.get("time", "")
            time_str = row.get(time_col, "").strip()
            if not time_str:
                continue  # skip blank rows silently

            try:
                dt = _parse_timestamp(time_str, time_format, tz_name)
            except Exception as exc:
                if len(row_errors) < 5:
                    row_errors.append((row_num, f"row {row_num}: cannot parse time {time_str!r}: {exc}"))
                continue

            raw_fields = {k: v.strip() for k, v in row.items() if v is not None}

            # ---- weight ----
            weight_col = columns.get("weight_kg")
            if weight_col:
                val_str = row.get(weight_col, "").strip()
                if val_str:
                    try:
                        val = float(val_str)
                        if _plausibility_filter("weight", val):
                            records.append(
                                RawRecord(
                                    source="generic",
                                    kind="weight",
                                    time_utc=dt,
                                    value=val,
                                    meta={"csv_row": row_num},
                                    raw=raw_fields,
                                )
                            )
                    except ValueError:
                        if len(row_errors) < 5:
                            row_errors.append(
                                (row_num, f"row {row_num}: cannot parse weight_kg {val_str!r}")
                            )

            # ---- body_fat ----
            bf_col = columns.get("body_fat")
            if bf_col:
                val_str = row.get(bf_col, "").strip()
                if val_str:
                    try:
                        val = float(val_str)
                        if _plausibility_filter("body_fat", val):
                            records.append(
                                RawRecord(
                                    source="generic",
                                    kind="body_fat",
                                    time_utc=dt,
                                    value=val,
                                    meta={"csv_row": row_num},
                                    raw=raw_fields,
                                )
                            )
                    except ValueError:
                        if len(row_errors) < 5:
                            row_errors.append(
                                (row_num, f"row {row_num}: cannot parse body_fat {val_str!r}")
                            )

            # ---- lean_mass ----
            lm_col = columns.get("lean_mass")
            if lm_col:
                val_str = row.get(lm_col, "").strip()
                if val_str:
                    try:
                        val = float(val_str)
                        if _plausibility_filter("lean_mass", val):
                            records.append(
                                RawRecord(
                                    source="generic",
                                    kind="lean_mass",
                                    time_utc=dt,
                                    value=val,
                                    meta={"csv_row": row_num},
                                    raw=raw_fields,
                                )
                            )
                    except ValueError:
                        if len(row_errors) < 5:
                            row_errors.append(
                                (row_num, f"row {row_num}: cannot parse lean_mass {val_str!r}")
                            )

    if row_errors:
        # Deduplicate and truncate to 5
        seen: set[str] = set()
        unique: list[str] = []
        for _, msg in row_errors:
            if msg not in seen:
                seen.add(msg)
                unique.append(msg)
                if len(unique) >= 5:
                    break
        raise MappingError(
            f"Parse errors in {csv_path!r}:\n  " + "\n  ".join(unique),
            row_errors=row_errors,
        )

    return records


def parse(path: str) -> list[RawRecord]:
    """
    Parse a generic CSV file using a mapping file.

    The path must point to the **mapping file** (JSON).  The CSV file path is
    read from the mapping's "csv" key, or if absent, derived from the mapping
    filename by stripping the .json extension.

    This two-file design (mapping + data) keeps the CSV unaltered and lets
    users version-control the mapping alongside the data.

    Example layout::

        myfit-export.csv
        myfit-mapping.json   ← path passed to this function
    """
    mapping_path = pathlib.Path(path)

    # Mapping file must be a .json
    if mapping_path.suffix.lower() != ".json":
        raise MappingError(
            f"Mapping path {mapping_path!r} must be a .json file. "
            f"generic_csv adapter requires a JSON mapping file; "
            f"see docs/source-integration-guide.md"
        )

    if not mapping_path.exists():
        raise MappingError(
            f"Mapping file not found: {mapping_path!r}. "
            f"generic_csv adapter requires a JSON mapping file; "
            f"see docs/source-integration-guide.md"
        )

    mapping = _load_mapping(str(mapping_path))

    # CSV path: explicit "csv" key in mapping, or derived from mapping file path
    if "csv" in mapping:
        csv_path = pathlib.Path(mapping["csv"])
        if not csv_path.is_absolute():
            # Relative paths are relative to the mapping file's directory
            csv_path = mapping_path.parent / csv_path
    elif mapping_path.stem.endswith("-mapping"):
        # Convention: foofit-mapping.json → foofit.csv
        csv_path = mapping_path.parent / (mapping_path.stem.removesuffix("-mapping") + ".csv")
    else:
        # Default: strip .json → .csv
        csv_path = mapping_path.with_suffix(".csv")

    if not csv_path.exists():
        raise MappingError(
            f"CSV file not found: {csv_path!r} "
            f"(derived from mapping file {mapping_path!r}). "
            f"Place the CSV alongside the mapping file, "
            f"or add a 'csv' key to the mapping JSON."
        )

    return parse_with_mapping(str(csv_path), mapping)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

register("generic", parse)  # type: ignore[arg-type]
