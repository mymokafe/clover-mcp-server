"""90-day window splitting for Clover time-ranged endpoints.

Clover caps historical filters (orders, payments) at ~90-day windows.
split_window() breaks an arbitrary date range into ≤90-day chunks so that
tools transparently support queries spanning multiple months or years.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo


def split_window(
    date_from: date,
    date_to: date,
    max_days: int = 90,
) -> list[tuple[date, date]]:
    """Split [date_from, date_to] into chunks of at most max_days each.

    Returns a list of (start, end) pairs that together cover the full range.
    """
    if date_from > date_to:
        raise ValueError(f"date_from ({date_from}) must be ≤ date_to ({date_to})")

    chunks: list[tuple[date, date]] = []
    cursor = date_from
    delta = timedelta(days=max_days - 1)

    while cursor <= date_to:
        chunk_end = min(cursor + delta, date_to)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)

    return chunks


def date_to_ms(d: date, end_of_day: bool = False, tz: tzinfo | None = None) -> int:
    """Convert a date to a Unix timestamp in milliseconds.

    With `tz` (the merchant's timezone) the boundary is LOCAL midnight, so a
    "day" matches the store's business day; without it, UTC midnight.
    If end_of_day=True, return the last millisecond of that day.
    Clover time filters use millisecond epoch timestamps.
    """
    zone = tz or UTC
    if end_of_day:
        dt = datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=zone)
    else:
        dt = datetime(d.year, d.month, d.day, 0, 0, 0, 0, tzinfo=zone)

    return int(dt.timestamp() * 1000)
