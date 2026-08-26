"""
timeutil.py — timezone helpers using the stdlib zoneinfo module.

All zone-offset logic in one place; DST-correct, IANA-generic.
Replaces the PoC's hardcoded Europe/Warsaw offsets.
"""

import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def zone_offset_seconds(at_utc: datetime.datetime, tz_name: str) -> int:
    """
    Return the UTC offset in **seconds** for the given IANA timezone at a
    specific UTC instant.

    Uses ``zoneinfo.ZoneInfo.utcoffset()`` which handles DST automatically.

    Args:
        at_utc: a datetime in UTC (tz-aware, or assumed UTC if naive).
        tz_name: IANA timezone name, e.g. ``"Europe/Warsaw"``,
                 ``"America/New_York"``, ``"UTC"``.

    Returns:
        Offset in seconds (e.g. 7200 for UTC+2, -18000 for UTC-5).

    Raises:
        ValueError: if ``tz_name`` is not a known IANA timezone.

    Example:
        >>> import datetime
        >>> zone_offset_seconds(
        ...     datetime.datetime(2026, 7, 15, 12, 0, tzinfo=datetime.timezone.utc),
        ...     "Europe/Warsaw"
        ... )
        7200
    """
    if at_utc.tzinfo is None:
        at_utc = at_utc.replace(tzinfo=datetime.timezone.utc)
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown IANA timezone: {tz_name!r}") from None
    # utcoffset is a datetime.timedelta; convert to seconds
    delta = tz.utcoffset(at_utc.astimezone(tz))
    if delta is None:
        # UTC itself has None utcoffset
        return 0
    return int(delta.total_seconds())


def local_midnight_as_utc(date: datetime.date, tz_name: str) -> datetime.datetime:
    """
    Return the UTC instant of local midnight (00:00) for the given date
    in the given timezone.

    This is the inverse of the zone_offset computation and is needed when
    a source entry has only a calendar date (no time) stored as local
    midnight — e.g. Libra CSV "date-only" entries stored as 22:00Z / 23:00Z
    depending on DST.

    The caller supplies the zone offset; this function returns the UTC
    instant that corresponds to 00:00 local time in ``tz_name``.

    Args:
        date: calendar date in the target local timezone.
        tz_name: IANA timezone name.

    Returns:
        UTC datetime of local midnight (tz-aware).

    Raises:
        ValueError: if ``tz_name`` is not a known IANA timezone.

    Example:
        # 2026-07-15 local midnight in Warsaw is 2026-07-14 22:00Z
        >>> local_midnight_as_utc(datetime.date(2026, 7, 15), "Europe/Warsaw")
        datetime.datetime(2026, 7, 14, 22, 0, tzinfo=datetime.timezone.utc)
    """
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise ValueError(f"Unknown IANA timezone: {tz_name!r}") from None

    # Construct local midnight (wall-clock midnight in the target timezone)
    local_midnight = datetime.datetime(
        date.year, date.month, date.day, 0, 0, 0, tzinfo=tz
    )
    # Offset from UTC at this instant
    utc_offset = local_midnight.utcoffset()
    if utc_offset is None:
        # UTC itself — midnight in UTC is just midnight UTC
        return datetime.datetime(
            date.year, date.month, date.day, 0, 0, 0, tzinfo=datetime.timezone.utc
        )
    # UTC equivalent: subtract the offset from the local instant's wall-clock value.
    # We construct the UTC naive datetime by removing tzinfo after subtraction.
    utc_naive = (local_midnight - utc_offset).replace(tzinfo=None)
    return datetime.datetime(
        utc_naive.year, utc_naive.month, utc_naive.day,
        utc_naive.hour, utc_naive.minute, utc_naive.second,
        utc_naive.microsecond, tzinfo=datetime.timezone.utc
    )
