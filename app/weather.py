"""Local weather lookup — an optional mood factor.

Uses only free, key-free public APIs:
  - Geocoding: zippopotam.us for US ZIP codes, Open-Meteo geocoding for city
    names (covers "Seattle", "London", "Paris, FR", etc.).
  - Current conditions: Open-Meteo forecast API (WMO weather codes).

`fetch_weather()` is best-effort: it returns None (or raises WeatherError with a
user-friendly message) so the rest of the app degrades gracefully when no
location is set or a lookup fails.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from .models import Weather

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
ZIP_URL = "https://api.zippopotam.us/us/{zip}"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_US_ZIP = re.compile(r"^\d{5}$")

# WMO weather code -> (condition label, emoji, is_precipitation).
WMO = {
    0: ("Clear", "☀️", False),
    1: ("Mostly clear", "\U0001F324️", False),
    2: ("Partly cloudy", "⛅", False),
    3: ("Overcast", "☁️", False),
    45: ("Fog", "\U0001F32B️", False),
    48: ("Freezing fog", "\U0001F32B️", False),
    51: ("Light drizzle", "\U0001F326️", True),
    53: ("Drizzle", "\U0001F326️", True),
    55: ("Heavy drizzle", "\U0001F327️", True),
    56: ("Freezing drizzle", "\U0001F327️", True),
    57: ("Freezing drizzle", "\U0001F327️", True),
    61: ("Light rain", "\U0001F326️", True),
    63: ("Rain", "\U0001F327️", True),
    65: ("Heavy rain", "\U0001F327️", True),
    66: ("Freezing rain", "\U0001F327️", True),
    67: ("Freezing rain", "\U0001F327️", True),
    71: ("Light snow", "\U0001F328️", True),
    73: ("Snow", "\U0001F328️", True),
    75: ("Heavy snow", "❄️", True),
    77: ("Snow grains", "\U0001F328️", True),
    80: ("Rain showers", "\U0001F326️", True),
    81: ("Rain showers", "\U0001F327️", True),
    82: ("Violent rain showers", "⛈️", True),
    85: ("Snow showers", "\U0001F328️", True),
    86: ("Heavy snow showers", "❄️", True),
    95: ("Thunderstorm", "⛈️", True),
    96: ("Thunderstorm w/ hail", "⛈️", True),
    99: ("Thunderstorm w/ hail", "⛈️", True),
}


class WeatherError(Exception):
    """Raised with a user-friendly message when a lookup fails."""


def describe_code(code: int):
    return WMO.get(code, ("Unknown", "\U0001F321️", False))


async def _geocode(client: httpx.AsyncClient, query: str) -> dict:
    """Resolve a city name or US ZIP to {name, latitude, longitude}."""
    q = query.strip()
    if not q:
        raise WeatherError("Please enter a city or ZIP code.")

    # US ZIP -> zippopotam (reliable for postal codes).
    if _US_ZIP.match(q):
        try:
            resp = await client.get(ZIP_URL.format(zip=q))
            if resp.status_code == 200:
                data = resp.json()
                place = (data.get("places") or [{}])[0]
                name = place.get("place name", q)
                state = place.get("state abbreviation") or place.get("state", "")
                label = f"{name}, {state}".strip(", ") if state else name
                return {
                    "name": label,
                    "latitude": float(place["latitude"]),
                    "longitude": float(place["longitude"]),
                }
        except (httpx.HTTPError, KeyError, ValueError, IndexError, TypeError):
            pass  # fall through to name-based geocoding
        # Some ZIPs may not resolve; surface a clear message.
        raise WeatherError(f"Couldn't find ZIP code “{q}”.")

    # City name -> Open-Meteo geocoding. The API matches on a bare place name,
    # so for inputs like "Portland, OR" or "Paris, France" we try the full
    # string first, then fall back to just the part before the comma.
    candidates = [q]
    if "," in q:
        head = q.split(",")[0].strip()
        if head and head != q:
            candidates.append(head)

    results = []
    for name in candidates:
        try:
            resp = await client.get(GEO_URL, params={"name": name, "count": 1, "language": "en"})
            resp.raise_for_status()
            results = resp.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            raise WeatherError("Weather lookup is unavailable right now.")
        if results:
            break
    if not results:
        raise WeatherError(f"Couldn't find a location named “{q}”.")
    r = results[0]
    parts = [r.get("name"), r.get("admin1"), r.get("country_code")]
    label = ", ".join(p for p in parts if p)
    try:
        return {"name": label, "latitude": float(r["latitude"]), "longitude": float(r["longitude"])}
    except (KeyError, ValueError, TypeError):
        raise WeatherError(f"Couldn't find a location named “{q}”.")


async def _current(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    """Returns the full forecast JSON (current conditions + timezone metadata)."""
    resp = await client.get(FORECAST_URL, params={
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,weather_code,precipitation,wind_speed_10m",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "auto",   # also returns utc_offset_seconds + timezone name
    })
    resp.raise_for_status()
    return resp.json()


async def fetch_weather(query: str) -> Weather:
    """Resolve `query` (city or ZIP) and return current conditions.

    Raises WeatherError (with a user-friendly message) on failure.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        loc = await _geocode(client, query)
        try:
            forecast = await _current(client, loc["latitude"], loc["longitude"])
        except (httpx.HTTPError, ValueError):
            raise WeatherError("Weather service is unavailable right now.")

    cur = forecast.get("current", {}) or {}
    raw_code = cur.get("weather_code")
    try:
        code = int(raw_code) if raw_code is not None else -1
    except (ValueError, TypeError):
        code = -1
    condition, emoji, is_precip = describe_code(code)

    offset = forecast.get("utc_offset_seconds")
    try:
        offset = int(offset) if offset is not None else None
    except (ValueError, TypeError):
        offset = None

    return Weather(
        query=query.strip(),
        location_name=loc["name"],
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        code=code,
        condition=condition,
        emoji=emoji,
        is_precip=is_precip,
        temp_f=cur.get("temperature_2m"),
        precipitation=cur.get("precipitation"),
        wind_mph=cur.get("wind_speed_10m"),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        utc_offset_seconds=offset,
        timezone=forecast.get("timezone"),
    )
