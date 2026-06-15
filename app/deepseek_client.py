"""DeepSeek integration.

DeepSeek exposes an OpenAI-compatible Chat Completions API, so we call it
directly with httpx (no SDK dependency / version coupling). Given the
classified market emotion and the underlying signals, DeepSeek returns a short
narrative plus concrete Spotify *search queries* used to build the playlist.

The whole thing is best-effort: if the key is missing, the call fails, or the
JSON is malformed, `interpret()` returns None and the caller falls back to the
deterministic mapping in `music.py`. The app never hard-depends on DeepSeek.
"""
from __future__ import annotations

import json
from typing import List, Optional

import httpx

from .config import settings
from .models import EmotionResult, MarketSnapshot, MusicPlan, TimeContext, Weather

_SYSTEM_PROMPT = """You are a music director for an app that turns live financial-market \
conditions into a Spotify playlist. You are given the market's current "emotion" \
(already classified) and the underlying signals, and sometimes the listener's local \
weather. Translate that combined mood into music.

Respond with ONLY a JSON object, no prose, with exactly these keys:
{
  "narrative": "1-2 vivid sentences describing the mood and the music that fits it",
  "search_queries": ["6-10 Spotify search queries that will surface matching tracks"],
  "seed_genres": ["3-6 genre words"],
  "target_energy": 0.0-1.0,
  "target_valence": 0.0-1.0
}

Guidelines for search_queries: each should be 2-4 words combining a genre/mood/era \
that a human would type into Spotify (e.g. "euphoric festival house", "melancholic \
piano", "aggressive thrash metal", "calm lo-fi beats"). Make them musically diverse \
but coherent with the emotion. target_energy is sonic intensity; target_valence is \
musical positivity (happy=high, sad=low).

If weather is provided, treat it as a secondary modifier of the market mood — the \
market drives the overall energy, the weather tints the texture. For example: rain, \
snow, fog or overcast skies over a calm/sideways/mildly-positive market call for \
cozier, lower-energy, lo-fi / chill / rainy-day music; clear, sunny skies brighten \
and lift the selection; a storm can add edge or drama. Never let mild weather \
override a strong market signal (a violent selloff stays intense even in sunshine), \
and weave the weather into the narrative when it meaningfully shifts the vibe.

If a time-of-day is provided, treat its energy_ceiling as a HARD CAP on sonic \
intensity: target_energy must not exceed it, and the search_queries must match \
that energy. Late at night, even a euphoric or chaotic market should be expressed \
calmly (e.g. nocturnal/downtempo/ambient/late-night versions of the mood) rather \
than aggressive, high-BPM, headbanging music. Midday allows full energy. Mention \
the hour in the narrative when it shapes the choice."""


def _build_user_payload(emotion: EmotionResult, snapshot: MarketSnapshot,
                        weather: Optional[Weather] = None,
                        time_ctx: Optional[TimeContext] = None) -> str:
    movers = {}
    for sym, a in snapshot.assets.items():
        movers[sym] = {
            "change_pct": a.change_pct,
            "trend": a.trend,
        }
    payload = {
        "emotion": emotion.emotion,
        "confidence": emotion.confidence,
        "classifier_summary": emotion.summary,
        "signals": {
            "risk_on_score": round(snapshot.risk_on_score, 3),
            "vol_shock_score": round(snapshot.vol_shock_score, 3),
            "dispersion": round(snapshot.dispersion, 3),
            "reversal_score": round(snapshot.reversal_score, 3),
        },
        "assets": movers,
    }
    if weather is not None:
        payload["weather"] = {
            "location": weather.location_name,
            "condition": weather.condition,
            "is_precipitating": weather.is_precip,
            "temperature_f": weather.temp_f,
            "wind_mph": weather.wind_mph,
        }
    if time_ctx is not None:
        payload["time_of_day"] = {
            "local_time": time_ctx.local_time,
            "daypart": time_ctx.daypart,
            "energy_ceiling": time_ctx.energy_ceiling,
        }
    return json.dumps(payload)


def _coerce_plan(emotion: str, data: dict) -> Optional[MusicPlan]:
    # The model output is untrusted: guard against non-object JSON and
    # wrong-typed fields so any deviation routes to the deterministic fallback.
    if not isinstance(data, dict):
        return None
    try:
        raw_queries = data.get("search_queries")
        if not isinstance(raw_queries, list):
            return None
        queries = [str(q).strip() for q in raw_queries if str(q).strip()]
        if not queries:
            return None
        raw_genres = data.get("seed_genres")
        genres = ([str(g).strip() for g in raw_genres if str(g).strip()]
                  if isinstance(raw_genres, list) else [])
        energy = float(data.get("target_energy", 0.5))
        valence = float(data.get("target_valence", 0.5))
        narrative = str(data.get("narrative", "")).strip()
        return MusicPlan(
            emotion=emotion,
            narrative=narrative,
            search_queries=queries[:12],
            seed_genres=genres[:6],
            target_energy=max(0.0, min(1.0, energy)),
            target_valence=max(0.0, min(1.0, valence)),
            source="deepseek",
        )
    except (TypeError, ValueError):
        return None


async def interpret(emotion: EmotionResult, snapshot: MarketSnapshot,
                    weather: Optional[Weather] = None,
                    time_ctx: Optional[TimeContext] = None) -> Optional[MusicPlan]:
    """Ask DeepSeek to turn the market emotion (plus optional weather and
    time-of-day) into a music plan. Returns None on any failure so the caller
    can fall back to the deterministic mapping."""
    if not settings.deepseek_configured:
        return None

    url = f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"
    body = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_payload(emotion, snapshot, weather, time_ctx)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.8,
        "max_tokens": 600,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        plan = _coerce_plan(emotion.emotion, parsed)
        # Hard-cap energy to the time-of-day ceiling even if the model overshot.
        if plan and time_ctx and plan.target_energy > time_ctx.energy_ceiling:
            plan.target_energy = time_ctx.energy_ceiling
        return plan
    except (httpx.HTTPError, KeyError, IndexError, TypeError, AttributeError,
            ValueError, json.JSONDecodeError):
        # Any malformed/empty response -> None so the caller uses the fallback.
        return None
