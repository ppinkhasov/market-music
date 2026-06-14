"""Application configuration, loaded from environment / .env.

We deliberately avoid pydantic-settings here so the app runs on any pydantic
version (or none). Everything is plain env parsing with sensible defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


# Default basket: equities (SPY, QQQ), crypto (BTC, ETH), and the VIX as the
# market's "fear gauge". Crypto trades 24/7, which keeps the app alive when the
# US equity market is closed.
DEFAULT_ASSETS = ["SPY", "QQQ", "BTC-USD", "ETH-USD", "^VIX"]


@dataclass
class Settings:
    # --- Spotify ---
    spotify_client_id: str = os.getenv("SPOTIFY_CLIENT_ID", "")
    spotify_client_secret: str = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    # Spotify now rejects http://localhost for new apps; 127.0.0.1 is allowed.
    spotify_redirect_uri: str = os.getenv(
        "SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8000/auth/callback"
    )

    # --- DeepSeek (OpenAI-compatible chat completions API) ---
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # --- Market polling ---
    tracked_assets: List[str] = field(
        default_factory=lambda: _split_csv(os.getenv("TRACKED_ASSETS", "")) or list(DEFAULT_ASSETS)
    )
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "45"))

    # --- Weather (optional mood factor) ---
    # Preset starting location (city or ZIP); can also be set live in the UI.
    weather_location: str = os.getenv("WEATHER_LOCATION", "")

    # --- Playlist ---
    playlist_name: str = os.getenv("PLAYLIST_NAME", "Market Music \U0001F3B6 Live Mood")
    playlist_size: int = int(os.getenv("PLAYLIST_SIZE", "25"))
    playlist_public: bool = os.getenv("PLAYLIST_PUBLIC", "true").lower() == "true"

    # --- Server ---
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))
    # Set the Secure flag on the session cookie. Defaults to off so local
    # http://127.0.0.1 dev works; set COOKIE_SECURE=true behind HTTPS.
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.deepseek_api_key)

    # Scopes required for: reading profile, creating/editing playlists, and
    # controlling/reading playback on an active device.
    spotify_scopes: str = (
        "user-read-private "
        "playlist-modify-public playlist-modify-private playlist-read-private "
        "user-read-playback-state user-modify-playback-state user-read-currently-playing"
    )


settings = Settings()
