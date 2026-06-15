"""Emotion -> music translation and playlist assembly.

Two responsibilities:
  1. Produce a MusicPlan (narrative + Spotify search queries) for an emotion.
     DeepSeek is tried first; if unavailable we use the deterministic mapping
     below so the app works fully without any LLM.
  2. Turn a MusicPlan into a concrete list of tracks via Spotify *search*
     (Spotify deprecated the recommendations/audio-features endpoints for new
     apps, so search is the robust path).
"""
from __future__ import annotations

from typing import List, Optional

from . import deepseek_client
from .models import EmotionResult, MarketSnapshot, MusicPlan, TimeContext, Track, Weather

# Deterministic fallback: hand-tuned music profile per emotion. Used whenever
# DeepSeek is not configured or fails.
EMOTION_MUSIC = {
    "euphoric": {
        "queries": ["euphoric festival house", "uplifting trance", "feel good dance pop",
                    "stadium anthems", "disco funk party", "summer hits"],
        "genres": ["dance", "house", "pop", "edm"],
        "energy": 0.92, "valence": 0.95,
    },
    "happy": {
        "queries": ["feel good indie pop", "happy acoustic", "sunny pop rock",
                    "upbeat funk", "good vibes playlist", "indie summer"],
        "genres": ["pop", "indie", "funk"],
        "energy": 0.7, "valence": 0.85,
    },
    "calm": {
        "queries": ["calm lo-fi beats", "ambient chill", "soft piano", "acoustic morning",
                    "peaceful instrumental", "chillhop"],
        "genres": ["ambient", "lo-fi", "chill"],
        "energy": 0.25, "valence": 0.55,
    },
    "anxious": {
        "queries": ["tense electronic", "moody alternative", "restless indie", "dark synth",
                    "nervous post-punk", "uneasy ambient"],
        "genres": ["alternative", "electronic", "post-punk"],
        "energy": 0.6, "valence": 0.35,
    },
    "fearful": {
        "queries": ["dark ambient", "ominous soundtrack", "tense cinematic", "brooding electronic",
                    "haunting strings", "dread drone"],
        "genres": ["ambient", "soundtrack", "electronic"],
        "energy": 0.45, "valence": 0.2,
    },
    "angry": {
        "queries": ["aggressive thrash metal", "hard rock rage", "angry rap", "hardcore punk",
                    "intense industrial", "rage workout"],
        "genres": ["metal", "rock", "punk", "industrial"],
        "energy": 0.97, "valence": 0.25,
    },
    "surprised": {
        "queries": ["unexpected genre mashup", "eclectic mix", "experimental pop", "wild card hits",
                    "plot twist playlist", "glitch pop"],
        "genres": ["experimental", "pop", "electronic"],
        "energy": 0.7, "valence": 0.6,
    },
    "chaotic": {
        "queries": ["chaotic breakcore", "frantic drum and bass", "noise rock", "glitchcore",
                    "hyperpop chaos", "manic electronic"],
        "genres": ["breakcore", "drum and bass", "hyperpop", "noise"],
        "energy": 0.98, "valence": 0.4,
    },
    "depressed": {
        "queries": ["sad slowcore", "melancholic piano", "depressing indie", "rainy day blues",
                    "downtempo melancholy", "lonely ambient"],
        "genres": ["slowcore", "indie", "ambient", "blues"],
        "energy": 0.2, "valence": 0.12,
    },
    "manic": {
        "queries": ["frenetic hardstyle", "high energy techno", "manic hyperpop", "speed garage",
                    "relentless drum and bass", "peak time rave"],
        "genres": ["hardstyle", "techno", "hyperpop", "drum and bass"],
        "energy": 0.99, "valence": 0.65,
    },
}

_NARRATIVE = {
    "euphoric": "Markets are ripping higher with volatility asleep — pure celebration. Cue the anthems.",
    "happy": "A constructive, green tape calls for sunny, feel-good sounds.",
    "calm": "Quiet, range-bound markets pair with soft, low-key ambience.",
    "anxious": "Choppy and indecisive — tense, restless music for an uneasy tape.",
    "fearful": "Risk-off and volatile. Dark, ominous textures for a market in retreat.",
    "angry": "Violent selling demands loud, aggressive, cathartic music.",
    "surprised": "A sharp reversal flips the script — eclectic, unexpected picks.",
    "chaotic": "Volatility is off the leash and assets disagree. Frantic, high-octane chaos.",
    "depressed": "A slow, low-energy bleed lower — melancholic and downtempo.",
    "manic": "A frenzied melt-up running hot — relentless, high-energy sounds.",
}


def fallback_plan(emotion: str) -> MusicPlan:
    profile = EMOTION_MUSIC.get(emotion, EMOTION_MUSIC["calm"])
    return MusicPlan(
        emotion=emotion,
        narrative=_NARRATIVE.get(emotion, _NARRATIVE["calm"]),
        search_queries=list(profile["queries"]),
        seed_genres=list(profile["genres"]),
        target_energy=profile["energy"],
        target_valence=profile["valence"],
        source="fallback",
    )


# Emotions intense enough that mild weather should NOT soften them.
_HIGH_INTENSITY = {"angry", "chaotic", "manic", "euphoric", "fearful"}


def _weather_adjust_fallback(plan: MusicPlan, weather: Optional[Weather]) -> MusicPlan:
    """Deterministically tint the fallback plan with the weather, so the feature
    works even without DeepSeek. The market still drives intensity; weather only
    nudges texture, and never overrides a high-intensity market regime."""
    if weather is None:
        return plan

    if weather.is_precip and plan.emotion not in _HIGH_INTENSITY:
        # Rain/snow/storm over a non-extreme tape -> cozier, lower-energy.
        cozy = ["rainy day lo-fi", "cozy chill beats", "rainy day jazz"]
        plan.search_queries = cozy + [q for q in plan.search_queries if q not in cozy]
        plan.search_queries = plan.search_queries[:10]
        plan.target_energy = max(0.1, plan.target_energy - 0.2)
        plan.target_valence = max(0.05, plan.target_valence - 0.1)
        plan.narrative = (f"{weather.condition} in {weather.location_name} over a "
                          f"{plan.emotion} market — leaning into cozy, low-key rainy-day sounds.")
    elif weather.code in (0, 1) and plan.emotion in ("happy", "calm", "euphoric"):
        # Clear skies brighten an already-positive tape.
        plan.search_queries = (["sunny day feel good"]
                               + [q for q in plan.search_queries if q != "sunny day feel good"])[:10]
        plan.target_valence = min(1.0, plan.target_valence + 0.05)
        plan.narrative = (f"Clear skies in {weather.location_name} brightening a "
                          f"{plan.emotion} market — sunny, upbeat picks.")
    return plan


# Calm, late-hours queries blended in when the time-of-day caps energy hard.
_NIGHT_QUERIES = ["late night lo-fi", "downtempo nocturnal", "ambient after hours", "chill nightdrive"]
# Dayparts that are genuinely nocturnal — soften the selection here, but NOT at
# dawn (a wake-up, not a wind-down) even though its ceiling is also low.
_NIGHT_DAYPARTS = {"night", "late night"}


def _time_adjust_fallback(plan: MusicPlan, time_ctx: Optional[TimeContext],
                          weather: Optional[Weather] = None) -> MusicPlan:
    """Cap the fallback plan's energy to the hour and, at night, soften the
    selection so a high-energy market isn't headbanging at 2am."""
    if time_ctx is None or plan.target_energy <= time_ctx.energy_ceiling:
        return plan
    plan.target_energy = time_ctx.energy_ceiling
    if time_ctx.daypart in _NIGHT_DAYPARTS:  # soften even high-energy moods
        plan.search_queries = (_NIGHT_QUERIES
                               + [q for q in plan.search_queries if q not in _NIGHT_QUERIES])[:10]
        # Compose with the weather tint (if any) so it isn't erased.
        if weather is not None and weather.is_precip:
            plan.narrative = (f"{weather.condition} in {weather.location_name}, and it's "
                              f"{time_ctx.local_time} — cozy, low-key and nocturnal for the "
                              f"{plan.emotion} read.")
        else:
            plan.narrative = (f"It's {time_ctx.local_time} ({time_ctx.daypart}) — keeping the "
                              f"{plan.emotion} market read low-key and nocturnal for the hour.")
    return plan


async def build_music_plan(emotion: EmotionResult, snapshot: MarketSnapshot,
                           weather: Optional[Weather] = None,
                           time_ctx: Optional[TimeContext] = None) -> MusicPlan:
    """Try DeepSeek (which sees weather + time directly), else fall back to the
    deterministic mapping with weather and time-of-day tints applied."""
    plan = await deepseek_client.interpret(emotion, snapshot, weather, time_ctx)
    if plan is not None and plan.search_queries:
        # If DeepSeek omitted genres, borrow from the fallback profile.
        if not plan.seed_genres:
            plan.seed_genres = EMOTION_MUSIC.get(emotion.emotion, {}).get("genres", [])
        return plan
    fb = _weather_adjust_fallback(fallback_plan(emotion.emotion), weather)
    return _time_adjust_fallback(fb, time_ctx, weather)


async def assemble_tracks(spotify, plan: MusicPlan, limit: int) -> List[Track]:
    """Run each search query against Spotify and collect a deduped track list.

    `spotify` is an authenticated SpotifyClient. Tracks are interleaved across
    queries so the playlist reflects the full mood, not just the first query.
    """
    per_query = max(4, (limit // max(1, len(plan.search_queries))) + 3)
    buckets: List[List[Track]] = []
    for query in plan.search_queries:
        try:
            tracks = await spotify.search_tracks(query, limit=per_query)
            buckets.append(tracks)
        except Exception:
            buckets.append([])

    # Round-robin interleave so early queries don't dominate.
    collected: List[Track] = []
    seen = set()
    idx = 0
    while len(collected) < limit and any(idx < len(b) for b in buckets):
        for b in buckets:
            if idx < len(b):
                t = b[idx]
                if t.uri not in seen:
                    seen.add(t.uri)
                    collected.append(t)
                    if len(collected) >= limit:
                        break
        idx += 1
    return collected
