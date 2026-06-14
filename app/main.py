"""FastAPI application: routes, OAuth flow, and the live state API."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import spotify_client, weather as weather_mod
from .config import settings
from .state import UserSession, engine

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("market_music")

BASE_DIR = Path(__file__).resolve().parent
COOKIE_NAME = "mm_session"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("starting poller (interval=%ss, assets=%s)",
             settings.poll_interval_seconds, settings.tracked_assets)
    engine.start()
    try:
        yield
    finally:
        await engine.stop()


app = FastAPI(title="market-music", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --- session helpers ------------------------------------------------------

def _current_session(request: Request) -> Optional[UserSession]:
    return engine.sessions.get(request.cookies.get(COOKIE_NAME))


def _set_session_cookie(response, session: UserSession) -> None:
    response.set_cookie(
        COOKIE_NAME, session.session_id,
        httponly=True, samesite="lax", secure=settings.cookie_secure,
        max_age=60 * 60 * 24 * 7, path="/",
    )


# --- pages ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    session = _current_session(request)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "spotify_configured": settings.spotify_configured,
        "deepseek_configured": settings.deepseek_configured,
        "logged_in": bool(session and session.logged_in),
        "display_name": session.display_name if session else None,
        "poll_interval": settings.poll_interval_seconds,
    })


# --- state API ------------------------------------------------------------

@app.get("/api/health")
async def health():
    running = engine.is_running()
    body = {"status": "ok" if running else "degraded",
            "poller_running": running,
            "polls": engine.state.poll_count}
    return JSONResponse(body, status_code=200 if running else 503)


@app.get("/api/state")
async def state(request: Request):
    session = _current_session(request)
    s = engine.state
    body = {
        "updated_at": s.updated_at,
        "poll_count": s.poll_count,
        "last_error": s.last_error,
        "poll_interval": settings.poll_interval_seconds,
        "config": {
            "spotify_configured": settings.spotify_configured,
            "deepseek_configured": settings.deepseek_configured,
            "tracked_assets": settings.tracked_assets,
            "market_data_provider": settings.market_data_provider,
            "polygon_configured": settings.polygon_configured,
        },
        "snapshot": s.snapshot.to_dict() if s.snapshot else None,
        "emotion": s.emotion.to_dict() if s.emotion else None,
        "music_plan": s.music_plan.to_dict() if s.music_plan else None,
        "weather": s.weather.to_dict() if s.weather else None,
        "history": s.history,
        "session": {
            "logged_in": bool(session and session.logged_in),
            "display_name": session.display_name if session else None,
            "auto_sync": session.auto_sync if session else False,
            "playlist": (session.last_playlist.to_dict()
                         if session and session.last_playlist else None),
            "synced_emotion": session.last_synced_emotion if session else None,
        },
    }
    return JSONResponse(body)


# --- Weather / location ---------------------------------------------------

@app.post("/api/location")
async def set_location(request: Request):
    """Set the listener's location (city or ZIP) so weather factors into the
    music. Global for this single-user MVP; no Spotify login required."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    query = (payload.get("query") or "").strip()
    if not query:
        return JSONResponse({"error": "Enter a city or ZIP code."}, status_code=400)
    try:
        w = await engine.set_location(query)
    except weather_mod.WeatherError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        log.exception("set_location failed")
        return JSONResponse({"error": "Weather lookup failed, please try again."}, status_code=502)
    return {"ok": True, "weather": w.to_dict(),
            "music_plan": engine.state.music_plan.to_dict() if engine.state.music_plan else None}


@app.post("/api/location/clear")
async def clear_location():
    await engine.clear_location()
    return {"ok": True}


# --- Spotify OAuth --------------------------------------------------------

@app.get("/auth/login")
async def login(request: Request):
    if not settings.spotify_configured:
        return JSONResponse({"error": "Spotify is not configured on the server."}, status_code=400)
    session = _current_session(request) or engine.sessions.create()
    session.oauth_state = secrets.token_urlsafe(16)
    url = spotify_client.build_authorize_url(session.oauth_state)
    response = RedirectResponse(url, status_code=302)
    _set_session_cookie(response, session)
    return response


@app.get("/auth/callback")
async def callback(request: Request, code: Optional[str] = None,
                   state: Optional[str] = None, error: Optional[str] = None):
    session = _current_session(request)
    if error:
        return RedirectResponse(f"/?auth_error={error}", status_code=302)
    if (not session or not code or not state or not session.oauth_state
            or not secrets.compare_digest(state, session.oauth_state)):
        # State mismatch / missing session => reject (CSRF protection).
        return RedirectResponse("/?auth_error=state_mismatch", status_code=302)

    session.oauth_state = None
    try:
        token = await spotify_client.exchange_code(code)
        session.token = token
        client = spotify_client.SpotifyClient(token.access_token)
        me = await client.get_me()
        session.user_id = me["id"]
        session.display_name = me.get("display_name") or me["id"]
    except Exception as exc:
        log.exception("oauth callback failed")
        return RedirectResponse(f"/?auth_error=token_exchange", status_code=302)

    response = RedirectResponse("/?auth=ok", status_code=302)
    _set_session_cookie(response, session)
    return response


@app.post("/auth/logout")
async def logout(request: Request):
    sid = request.cookies.get(COOKIE_NAME)
    if sid:
        engine.sessions.delete(sid)
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


# --- Spotify actions ------------------------------------------------------

def _require_login(request: Request) -> Optional[UserSession]:
    session = _current_session(request)
    if not session or not session.logged_in:
        return None
    return session


@app.post("/api/playlist/sync")
async def sync_playlist(request: Request):
    session = _require_login(request)
    if not session:
        return JSONResponse({"error": "Not logged in to Spotify."}, status_code=401)
    if engine.state.emotion is None:
        return JSONResponse({"error": "No market reading yet — try again in a moment."}, status_code=503)
    try:
        info = await engine.sync_playlist(session)
    except spotify_client.SpotifyAuthError:
        return JSONResponse({"error": "Spotify session expired. Please log in again."}, status_code=401)
    except spotify_client.SpotifyApiError as exc:
        return JSONResponse({"error": exc.message}, status_code=502)
    except Exception:
        log.exception("sync failed")
        return JSONResponse({"error": "Unexpected error building the playlist. Please try again."}, status_code=500)
    return {"ok": True, "playlist": info.to_dict()}


@app.post("/api/settings")
async def update_settings(request: Request):
    session = _require_login(request)
    if not session:
        return JSONResponse({"error": "Not logged in to Spotify."}, status_code=401)
    payload = await request.json()
    if "auto_sync" in payload:
        session.auto_sync = bool(payload["auto_sync"])
    return {"ok": True, "auto_sync": session.auto_sync}


@app.get("/api/devices")
async def devices(request: Request):
    session = _require_login(request)
    if not session:
        return JSONResponse({"error": "Not logged in to Spotify."}, status_code=401)
    try:
        client = await engine.ensure_client(session)
        device_list = await client.get_devices()
    except spotify_client.SpotifyAuthError:
        return JSONResponse({"error": "Spotify session expired. Please log in again."}, status_code=401)
    except Exception:
        log.exception("device list failed")
        return JSONResponse({"error": "Could not reach Spotify. Please try again."}, status_code=502)
    # Skip restricted/cast devices with a null id — they can't be targeted.
    return {"devices": [{"id": d.get("id"), "name": d.get("name"),
                         "type": d.get("type"), "is_active": d.get("is_active")}
                        for d in device_list if d.get("id")]}


@app.post("/api/play")
async def play(request: Request):
    session = _require_login(request)
    if not session:
        return JSONResponse({"error": "Not logged in to Spotify."}, status_code=401)
    payload = await request.json()
    device_id = payload.get("device_id")

    # Ensure there's a playlist to play; sync first if needed.
    if not session.playlist_id:
        try:
            await engine.sync_playlist(session)
        except spotify_client.SpotifyAuthError:
            return JSONResponse({"error": "Spotify session expired. Please log in again."}, status_code=401)
        except spotify_client.SpotifyApiError as exc:
            return JSONResponse({"error": exc.message}, status_code=502)
        except Exception:
            log.exception("playlist prep failed")
            return JSONResponse({"error": "Could not prepare the playlist. Please try again."}, status_code=502)

    # Don't ask Spotify to play an empty context (it returns a confusing 4xx).
    if session.last_playlist is not None and session.last_playlist.track_count == 0:
        return JSONResponse({"error": "No tracks available to play yet — try syncing again in a moment."}, status_code=409)

    context_uri = f"spotify:playlist:{session.playlist_id}"
    try:
        client = await engine.ensure_client(session)
        await client.start_playback(context_uri, device_id=device_id)
    except spotify_client.SpotifyAuthError:
        return JSONResponse({"error": "Spotify session expired. Please log in again."}, status_code=401)
    except spotify_client.SpotifyApiError as exc:
        if exc.status == 404:  # NO_ACTIVE_DEVICE
            msg = "No active Spotify device found. Open Spotify on a device and try again."
        elif exc.status == 403:  # playback control requires Premium
            msg = "Spotify Premium is required to control playback."
        else:
            msg = exc.message
        return JSONResponse({"error": msg}, status_code=502)
    except Exception:
        log.exception("playback failed")
        return JSONResponse({"error": "Unexpected error starting playback. Please try again."}, status_code=500)
    return {"ok": True, "playing": context_uri}
