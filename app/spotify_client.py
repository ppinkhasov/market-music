"""Spotify OAuth (Authorization Code flow) + Web API client.

Implemented directly on httpx so token handling lives in the web session
rather than spotipy's on-disk cache, which fits a multi-request web app better.

Track selection uses the Search endpoint only. Spotify deprecated the
Recommendations and Audio-Features endpoints for newer apps (Nov 2024), so we
never call them.
"""
from __future__ import annotations

import base64
import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional

import httpx

from .config import settings
from .models import Track

log = logging.getLogger("market_music")

AUTH_BASE = "https://accounts.spotify.com"
API_BASE = "https://api.spotify.com/v1"


@dataclass
class TokenSet:
    access_token: str
    refresh_token: Optional[str]
    expires_at: float           # epoch seconds
    scope: str = ""
    token_type: str = "Bearer"

    def is_expiring(self, leeway: int = 60) -> bool:
        return time.time() >= (self.expires_at - leeway)


class SpotifyAuthError(Exception):
    pass


class SpotifyApiError(Exception):
    def __init__(self, status: int, detail: str = ""):
        self.status = status
        # Raw upstream body, kept for server-side logging only — never returned
        # to the client (it can carry request ids / scope detail).
        self.detail = detail
        # Generic, status-derived message safe to show users.
        self.message = f"Spotify request failed (HTTP {status})."
        super().__init__(f"{self.message} {detail}".strip())


# --- OAuth ----------------------------------------------------------------

def build_authorize_url(state: str) -> str:
    params = {
        "client_id": settings.spotify_client_id,
        "response_type": "code",
        "redirect_uri": settings.spotify_redirect_uri,
        "scope": settings.spotify_scopes,
        "state": state,
        "show_dialog": "false",
    }
    return f"{AUTH_BASE}/authorize?" + urllib.parse.urlencode(params)


def _basic_auth_header() -> str:
    raw = f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def _token_request(form: dict) -> TokenSet:
    headers = {
        "Authorization": _basic_auth_header(),
        "Content-Type": "application/x-www-form-urlencoded",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(f"{AUTH_BASE}/api/token", data=form, headers=headers)
    if resp.status_code != 200:
        # Log the raw body server-side; keep it out of the raised message,
        # which can surface to the client.
        log.warning("Spotify token request failed %s: %s", resp.status_code, resp.text[:500])
        raise SpotifyAuthError(f"token request failed ({resp.status_code})")
    data = resp.json()
    return TokenSet(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=time.time() + float(data.get("expires_in", 3600)),
        scope=data.get("scope", ""),
        token_type=data.get("token_type", "Bearer"),
    )


async def exchange_code(code: str) -> TokenSet:
    return await _token_request({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.spotify_redirect_uri,
    })


async def refresh_token(token: TokenSet) -> TokenSet:
    if not token.refresh_token:
        raise SpotifyAuthError("no refresh token available")
    refreshed = await _token_request({
        "grant_type": "refresh_token",
        "refresh_token": token.refresh_token,
    })
    # Spotify may omit a new refresh token; keep the old one.
    if not refreshed.refresh_token:
        refreshed.refresh_token = token.refresh_token
    return refreshed


# --- API client -----------------------------------------------------------

class SpotifyClient:
    """Thin async wrapper over the Spotify Web API. Assumes a valid access
    token (refresh is handled by the session layer before construction)."""

    def __init__(self, access_token: str):
        self._token = access_token

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(self, method: str, path: str, *, params=None, json=None,
                       allow_empty: bool = False) -> Optional[dict]:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.request(method, url, headers=self._headers,
                                        params=params, json=json)
        if resp.status_code == 401:
            raise SpotifyAuthError("access token expired or invalid")
        if resp.status_code >= 400:
            log.warning("Spotify API %s on %s %s: %s",
                        resp.status_code, method, path, resp.text[:500])
            raise SpotifyApiError(resp.status_code, resp.text)
        if resp.status_code == 204 or not resp.content:
            return None if allow_empty else {}
        return resp.json()

    async def get_me(self) -> dict:
        return await self._request("GET", "/me")

    async def search_tracks(self, query: str, limit: int = 10,
                            market: str = "from_token") -> List[Track]:
        data = await self._request("GET", "/search", params={
            "q": query,
            "type": "track",
            "limit": max(1, min(50, limit)),
            "market": market,
        })
        items = (data or {}).get("tracks", {}).get("items", []) or []
        tracks: List[Track] = []
        for it in items:
            if not it or not it.get("uri"):
                continue
            artists = it.get("artists") or []
            artist = artists[0]["name"] if artists else "Unknown"
            images = (it.get("album") or {}).get("images") or []
            image = images[-1]["url"] if images else ""
            tracks.append(Track(
                name=it.get("name", "Unknown"),
                artist=artist,
                uri=it["uri"],
                url=(it.get("external_urls") or {}).get("spotify", ""),
                image=image,
            ))
        return tracks

    async def find_playlist_by_name(self, name: str) -> Optional[dict]:
        """Look for a playlist this user owns with the given name (paginated)."""
        url = "/me/playlists?limit=50"
        while url:
            data = await self._request("GET", url)
            for pl in (data or {}).get("items", []) or []:
                if pl and pl.get("name") == name:
                    return pl
            url = (data or {}).get("next")
        return None

    async def create_playlist(self, user_id: str, name: str, public: bool,
                              description: str = "") -> dict:
        return await self._request("POST", f"/users/{user_id}/playlists", json={
            "name": name,
            "public": public,
            "description": description[:300],
        })

    async def replace_playlist_tracks(self, playlist_id: str, uris: List[str]) -> None:
        # PUT replaces the full tracklist (max 100 uris per call).
        await self._request("PUT", f"/playlists/{playlist_id}/tracks",
                            json={"uris": uris[:100]}, allow_empty=True)

    async def update_playlist_details(self, playlist_id: str, *, name: str = None,
                                      description: str = None) -> None:
        body = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description[:300]
        if body:
            await self._request("PUT", f"/playlists/{playlist_id}", json=body,
                                allow_empty=True)

    async def get_devices(self) -> List[dict]:
        data = await self._request("GET", "/me/player/devices")
        return (data or {}).get("devices", []) or []

    async def get_playback(self) -> Optional[dict]:
        return await self._request("GET", "/me/player", allow_empty=True)

    async def start_playback(self, context_uri: str, device_id: Optional[str] = None) -> None:
        params = {"device_id": device_id} if device_id else None
        await self._request("PUT", "/me/player/play", params=params,
                            json={"context_uri": context_uri}, allow_empty=True)
