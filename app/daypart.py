"""Time-of-day as a mood factor.

The market sets the emotion; the time of day caps how *energetic* the music
should be — you don't want headbanging at 2am. Each daypart carries an energy
ceiling (0..1) and a descriptor woven into the narrative.

Local time comes from the set location's timezone (via the weather lookup's
`utc_offset_seconds`); with no location we fall back to the server's local time
(the app normally runs on the listener's own machine).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import TimeContext, Weather

# (start_hour, daypart label, descriptor, energy_ceiling). Ascending by hour;
# the last entry whose start_hour <= current hour wins.
_DAYPARTS = [
    (0,  "late night",     "the dead of night — keep it hushed and nocturnal", 0.35),
    (5,  "dawn",           "first light — gentle and slow to wake", 0.50),
    (7,  "morning",        "morning — bright but easy, a warm-up", 0.72),
    (10, "midday",         "peak daylight — full energy on the table", 1.00),
    (16, "late afternoon", "late afternoon — still lively", 0.90),
    (18, "evening",        "evening — winding down a notch", 0.75),
    (21, "night",          "night — mellow and low-key", 0.55),
    (23, "late night",     "the small hours approach — soft and quiet", 0.40),
]


def _for_hour(hour: int):
    chosen = _DAYPARTS[0]
    for entry in _DAYPARTS:
        if hour >= entry[0]:
            chosen = entry
    return chosen


def _fmt_time(dt: datetime) -> str:
    # Cross-platform 12-hour format without a leading zero (avoid %-I).
    return dt.strftime("%I:%M %p").lstrip("0")


def compute(weather: Optional[Weather]) -> TimeContext:
    """Current local TimeContext, using the location tz if known, else server."""
    if weather is not None and weather.utc_offset_seconds is not None:
        local = datetime.now(timezone.utc) + timedelta(seconds=weather.utc_offset_seconds)
        tz = weather.timezone or "local"
    else:
        local = datetime.now()  # server local time
        tz = "server time"
    hour = local.hour
    _, label, descriptor, ceiling = _for_hour(hour)
    return TimeContext(
        hour=hour,
        daypart=label,
        descriptor=descriptor,
        energy_ceiling=ceiling,
        local_time=_fmt_time(local),
        tz=tz,
    )
