"""
sources/__init__.py — adapter registry and common record types.

Each source adapter is a callable that accepts a path and returns a list of
RawRecord (instant measurements) or IntervalRecord (interval records).
Adapters are registered by name via the ``ADAPTERS`` dict.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Callable


# ---------------------------------------------------------------------------
# IntervalRecord — for interval-domain data (activity, sleep, heartrate, exercise)
# ---------------------------------------------------------------------------

@dataclass
class IntervalRecord:
    """
    Interval-domain record (start_time/end_time) from source adapters.

    Used for activity (steps/distance/calories), sleep (sessions + stages),
    heart rate (series), and exercise sessions.

    Attributes
    ----------
    source : str
        Identifier of the originating source (e.g. "zepp").
    kind : str
        Record kind: "steps", "distance", "calories", "sleep", "sleep_stage",
                      "hr_auto", "hr_manual", "exercise".
    start_utc : datetime
        UTC start instant (tz-aware datetime).
    end_utc : datetime
        UTC end instant (tz-aware datetime).
    start_offset_seconds : int
        Local timezone offset at start_utc (seconds).
    end_offset_seconds : int
        Local timezone offset at end_utc (seconds).
    local_date : int
        Local epoch days (start-based for most domains; end-based for sleep).
    samples : list[tuple[int, int]]
        For hr_auto/hr_manual: list of (epoch_ms, bpm).
    stages : list[tuple[int, int, int]]
        For sleep: list of (stage_start_ms, stage_end_ms, HC_stage_type_id).
    extra : dict
        Domain-specific extra fields.
    raw : dict
        Raw parsed fields from the source file.
    """
    source: str = "zepp"
    kind: str = ""
    start_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    end_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    start_offset_seconds: int = 0
    end_offset_seconds: int = 0
    local_date: int = 0
    samples: list = field(default_factory=list)
    stages: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def start_ms(self) -> int:
        return int(self.start_utc.timestamp() * 1000)

    def end_ms(self) -> int:
        return int(self.end_utc.timestamp() * 1000)


# ---------------------------------------------------------------------------
# RawRecord — for instant-domain data (weight, body_fat, etc.)
# ---------------------------------------------------------------------------

@dataclass
class RawRecord:
    """
    Instant-measurement record produced by source adapters.

    Attributes
    ----------
    source : str
        Identifier of the originating source (e.g. "libra", "zepp").
    kind : str
        Record kind: "weight" | "body_fat" | "lean_mass".
    time_utc : datetime
        UTC timestamp of the measurement (tz-aware datetime).
    value : float
        Value in the source unit (kg for weight/lean_mass, percent for body_fat).
    meta : dict
        Additional source-specific fields preserved for provenance.
    raw : dict, optional
        The raw parsed fields from the source file (useful for debugging).
    ms : int
        Epoch milliseconds (UTC) — computed from ``time_utc`` on init.
    """
    source: str
    kind: str
    time_utc: datetime
    value: float
    meta: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    ms: int = field(default=0)

    def __post_init__(self):
        if self.time_utc.tzinfo is None:
            raise ValueError(f"time_utc must be timezone-aware, got naive {self.time_utc}")
        if self.ms == 0:
            self.ms = int(self.time_utc.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

# Adapter signature: (path: str) -> list[RawRecord | IntervalRecord]
SourceAdapter = Callable[[str], list[RawRecord | IntervalRecord]]

ADAPTERS: dict[str, SourceAdapter] = {}


def register(name: str, adapter: SourceAdapter) -> None:
    ADAPTERS[name.lower()] = adapter


def get_adapter(name: str) -> SourceAdapter:
    return ADAPTERS[name.lower()]


def load_source(name: str, path: str) -> list[RawRecord | IntervalRecord]:
    """
    Load and parse ``path`` using the registered adapter for ``name``.

    Returns a list of RawRecord (instant measurements) and/or IntervalRecord
    (interval records).  Adapters are imported lazily on first use to avoid
    circular import issues.
    """
    name_lower = name.lower()
    if name_lower not in ADAPTERS:
        # Lazy import to trigger registration
        if name_lower == "libra":
            from ghc_db_manager.sources import libra  # noqa: F401
        elif name_lower == "zepp":
            from ghc_db_manager.sources import zepp   # noqa: F401
    adapter = ADAPTERS.get(name_lower)
    if adapter is None:
        raise KeyError(
            f"No adapter registered for source {name!r}. "
            f"Registered sources: {sorted(ADAPTERS.keys())}"
        )
    return adapter(path)
