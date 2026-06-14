"""Application state, session store, and the background market poller.

Single-process, in-memory state suitable for a local MVP. The poller runs as an
asyncio task: every interval it fetches a market snapshot (blocking yfinance call
offloaded to a thread), classifies the emotion, and — only when the emotion
changes — asks DeepSeek for a fresh music plan and auto-syncs playlists for any
logged-in sessions that opted in.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from . import emotion_engine, market_data, music, weather as weather_mod
from .config import settings
from .models import EmotionResult, MarketSnapshot, MusicPlan, PlaylistInfo, Weather
from .spotify_client import SpotifyClient, TokenSet, refresh_token

log = logging.getLogger("market_music")


@dataclass
class UserSession:
    session_id: str
    token: Optional[TokenSet] = None
    user_id: Optional[str] = None
    display_name: Optional[str] = None
    playlist_id: Optional[str] = None
    playlist_url: Optional[str] = None
    auto_sync: bool = False
    oauth_state: Optional[str] = None
    last_synced_emotion: Optional[str] = None
    last_playlist: Optional[PlaylistInfo] = None
    created_at: float = field(default_factory=time.time)
    # Per-session token-refresh lock (lazily created on the running loop so
    # independent sessions never serialize behind one global lock).
    refresh_lock: Optional[asyncio.Lock] = None

    @property
    def logged_in(self) -> bool:
        return self.token is not None and self.user_id is not None


class SessionStore:
    # Drop abandoned pre-auth sessions (created by /auth/login but never
    # completed) older than this, so the in-memory store can't grow unbounded.
    PREAUTH_TTL_SECONDS = 30 * 60

    def __init__(self) -> None:
        self._sessions: Dict[str, UserSession] = {}

    def _prune(self) -> None:
        cutoff = time.time() - self.PREAUTH_TTL_SECONDS
        stale = [sid for sid, s in self._sessions.items()
                 if not s.logged_in and s.created_at < cutoff]
        for sid in stale:
            self._sessions.pop(sid, None)

    def create(self) -> UserSession:
        self._prune()
        sid = secrets.token_urlsafe(24)
        session = UserSession(session_id=sid)
        self._sessions[sid] = session
        return session

    def get(self, session_id: Optional[str]) -> Optional[UserSession]:
        if not session_id:
            return None
        return self._sessions.get(session_id)

    def get_or_create(self, session_id: Optional[str]) -> UserSession:
        existing = self.get(session_id)
        return existing if existing else self.create()

    def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def logged_in_sessions(self) -> List[UserSession]:
        return [s for s in self._sessions.values() if s.logged_in]


@dataclass
class AppState:
    snapshot: Optional[MarketSnapshot] = None
    emotion: Optional[EmotionResult] = None
    music_plan: Optional[MusicPlan] = None
    weather: Optional[Weather] = None
    updated_at: Optional[str] = None
    last_error: Optional[str] = None
    poll_count: int = 0
    history: List[dict] = field(default_factory=list)  # recent {emotion, ts, confidence}

    def record_history(self, emotion: EmotionResult, ts: str) -> None:
        if self.history and self.history[-1]["emotion"] == emotion.emotion:
            return  # only record transitions
        self.history.append({
            "emotion": emotion.emotion,
            "confidence": round(emotion.confidence, 2),
            "ts": ts,
        })
        self.history = self.history[-20:]


class Engine:
    """Owns the global app state and the polling loop."""

    def __init__(self) -> None:
        self.state = AppState()
        self.sessions = SessionStore()
        # NOTE: asyncio primitives are created lazily in start(), on the
        # running serving loop. On Python 3.9, asyncio.Lock()/Event() bind to
        # whatever loop exists at construction time; building them here (the
        # singleton is created at import time, before uvicorn's loop exists)
        # would bind them to the wrong loop and break the poller under load.
        self._lock: Optional[asyncio.Lock] = None
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None
        # Weather (optional mood factor). Location can be preset via env or set
        # live in the UI; weather is refreshed on a slower cadence than prices.
        self._location: Optional[str] = (settings.weather_location.strip() or None)
        self._weather: Optional[Weather] = None
        self._weather_fetched_at: float = 0.0

    WEATHER_TTL_SECONDS = 600  # refetch weather at most every 10 minutes

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
            self._stop = asyncio.Event()
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())
            self._task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: "asyncio.Task") -> None:
        # Surface an unexpectedly-dead poller instead of swallowing its error.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("poller task exited unexpectedly: %r", exc)
            self.state.last_error = f"poller stopped: {exc}"

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        # Prime immediately, then loop on the interval. Every await in the loop
        # body is guarded so a transient error can never kill the poller.
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the loop alive no matter what
                log.exception("poll failed")
                self.state.last_error = str(exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poller wait failed")
                await asyncio.sleep(settings.poll_interval_seconds)

    # --- polling -----------------------------------------------------------

    async def _refresh_weather(self) -> bool:
        """Refresh weather if a location is set and the cache is stale.
        Returns True if the weather condition changed this refresh."""
        if not self._location:
            if self._weather is not None:  # location was cleared
                self._weather = None
                return True
            return False
        if (self._weather is not None
                and time.time() - self._weather_fetched_at < self.WEATHER_TTL_SECONDS):
            return False
        prev_code = self._weather.code if self._weather else None
        prev_precip = self._weather.is_precip if self._weather else None
        target = self._location  # snapshot before the network await
        try:
            w = await weather_mod.fetch_weather(target)
        except Exception:
            log.warning("weather refresh failed for %r", target)
            return False
        # Compare-and-swap: discard this result if the location changed (e.g. a
        # set_location/clear_location ran) while we were fetching, so a stale
        # refresh can't clobber a newer user selection.
        if self._location != target:
            return False
        self._weather = w
        self._weather_fetched_at = time.time()
        return (w.code != prev_code) or (w.is_precip != prev_precip)

    async def poll_once(self) -> None:
        # Bound the (blocking) fetch so a slow/hung data source can't stall the
        # poller past its interval. The worker thread can't be cancelled, but
        # wait_for unblocks the loop; per-call timeouts cap the orphaned thread.
        budget = max(30, settings.poll_interval_seconds - 5)
        try:
            snapshot = await asyncio.wait_for(
                asyncio.to_thread(market_data.fetch_snapshot), timeout=budget)
        except asyncio.TimeoutError:
            self.state.last_error = "market data fetch timed out"
            log.warning("market data fetch timed out after %ss", budget)
            return
        emotion = emotion_engine.classify(snapshot)
        weather_changed = await self._refresh_weather()
        # Snapshot weather once so the plan and the published state.weather are
        # built from the same value even if a location change races the await.
        weather_now = self._weather
        ts = datetime.now(timezone.utc).isoformat()

        prev_emotion = self.state.emotion.emotion if self.state.emotion else None
        emotion_changed = prev_emotion != emotion.emotion

        # Rebuild the music plan when the market regime OR the weather changes
        # (or we have none yet) — so a shift to rain re-tints the music.
        plan = self.state.music_plan
        if (emotion_changed or weather_changed or plan is None
                or plan.emotion != emotion.emotion):
            plan = await music.build_music_plan(emotion, snapshot, weather_now)

        async with self._lock:
            self.state.snapshot = snapshot
            self.state.emotion = emotion
            self.state.music_plan = plan
            self.state.weather = weather_now
            self.state.updated_at = ts
            self.state.last_error = None
            self.state.poll_count += 1
            self.state.record_history(emotion, ts)

        log.info("poll #%d: emotion=%s conf=%.2f risk_on=%.2f vol=%.2f weather=%s",
                 self.state.poll_count, emotion.emotion, emotion.confidence,
                 snapshot.risk_on_score, snapshot.vol_shock_score,
                 weather_now.condition if weather_now else "n/a")

        if emotion_changed or weather_changed:
            await self._auto_sync_all()

    # --- location / weather ------------------------------------------------

    async def set_location(self, query: str) -> Weather:
        """Resolve a city/ZIP, fetch weather, and rebuild the plan immediately.
        Raises weather.WeatherError on a bad/failed lookup."""
        w = await weather_mod.fetch_weather(query)  # may raise WeatherError
        self._location = query.strip()
        self._weather = w
        self._weather_fetched_at = time.time()
        await self._rebuild_plan_now()
        return w

    async def clear_location(self) -> None:
        self._location = None
        self._weather = None
        self._weather_fetched_at = 0.0
        await self._rebuild_plan_now()

    async def _rebuild_plan_now(self) -> None:
        """Rebuild the music plan against the current emotion + weather."""
        weather_now = self._weather
        if self.state.emotion is None or self.state.snapshot is None:
            self.state.weather = weather_now
            return
        plan = await music.build_music_plan(self.state.emotion, self.state.snapshot, weather_now)
        async with self._lock:
            self.state.music_plan = plan
            self.state.weather = weather_now
        # emotion is guaranteed non-None here (early return above).
        await self._auto_sync_all(plan)

    async def _auto_sync_all(self, plan: Optional[MusicPlan] = None) -> None:
        # Snapshot the plan once so every session in this pass syncs the same one.
        if plan is None:
            async with self._lock:
                plan = self.state.music_plan
        for session in self.sessions.logged_in_sessions():
            if not session.auto_sync:
                continue
            try:
                await self.sync_playlist(session, plan)
            except Exception:
                log.exception("auto-sync failed for session %s", session.session_id[:8])

    # --- spotify helpers ---------------------------------------------------

    async def ensure_client(self, session: UserSession) -> SpotifyClient:
        if not session.token:
            raise RuntimeError("not logged in")
        if session.token.is_expiring():
            if session.refresh_lock is None:
                session.refresh_lock = asyncio.Lock()
            async with session.refresh_lock:
                if session.token.is_expiring():  # re-check under lock
                    session.token = await refresh_token(session.token)
        return SpotifyClient(session.token.access_token)

    async def sync_playlist(self, session: UserSession,
                            plan: Optional[MusicPlan] = None) -> PlaylistInfo:
        """Build/refresh this session's managed playlist to the current mood."""
        if self.state.emotion is None or self.state.snapshot is None:
            raise RuntimeError("no market state yet")

        plan = plan or self.state.music_plan or await music.build_music_plan(
            self.state.emotion, self.state.snapshot, self._weather)

        client = await self.ensure_client(session)

        # Resolve user id once.
        if not session.user_id:
            me = await client.get_me()
            session.user_id = me["id"]
            session.display_name = me.get("display_name") or me["id"]

        tracks = await music.assemble_tracks(client, plan, settings.playlist_size)
        uris = [t.uri for t in tracks]

        description = (f"Auto-generated by market-music. Mood: {plan.emotion}. "
                       f"{plan.narrative}")[:300]

        # Find-or-create the managed playlist (by stored id, else by name).
        if not session.playlist_id:
            existing = await client.find_playlist_by_name(settings.playlist_name)
            if existing:
                session.playlist_id = existing["id"]
                session.playlist_url = (existing.get("external_urls") or {}).get("spotify", "")
        if not session.playlist_id:
            created = await client.create_playlist(
                session.user_id, settings.playlist_name,
                settings.playlist_public, description)
            session.playlist_id = created["id"]
            session.playlist_url = (created.get("external_urls") or {}).get("spotify", "")

        if uris:
            await client.replace_playlist_tracks(session.playlist_id, uris)
        await client.update_playlist_details(
            session.playlist_id,
            description=description)

        ts = datetime.now(timezone.utc).isoformat()
        info = PlaylistInfo(
            id=session.playlist_id,
            name=settings.playlist_name,
            url=session.playlist_url or f"https://open.spotify.com/playlist/{session.playlist_id}",
            track_count=len(tracks),
            tracks=tracks,
            updated_for_emotion=plan.emotion,
            updated_at=ts,
        )
        session.last_playlist = info
        session.last_synced_emotion = plan.emotion
        return info


# Singleton engine.
engine = Engine()
