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
from .models import EmotionResult, MarketSnapshot, MusicPlan, Track

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


async def build_music_plan(emotion: EmotionResult, snapshot: MarketSnapshot) -> MusicPlan:
    """Try DeepSeek, fall back to the deterministic mapping."""
    plan = await deepseek_client.interpret(emotion, snapshot)
    if plan is not None and plan.search_queries:
        # If DeepSeek omitted genres, borrow from the fallback profile.
        if not plan.seed_genres:
            plan.seed_genres = EMOTION_MUSIC.get(emotion.emotion, {}).get("genres", [])
        return plan
    return fallback_plan(emotion.emotion)


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
