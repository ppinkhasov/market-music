"""Application configuration, loaded from environment / .env.

We deliberately avoid pydantic-settings here so the app runs on any pydantic
version (or none). Everything is plain env parsing with sensible defaults.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import List

from pathlib import Path

from dotenv import load_dotenv

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

load_dotenv()


def update_env(updates: dict) -> None:
    """Persist key=value pairs to the project .env, updating existing keys in
    place and appending new ones (comments/other lines preserved)."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    # Strip newlines from values so a value can never inject extra .env lines.
    remaining = {str(k): str(v).replace("\n", "").replace("\r", "") for k, v in updates.items()}
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, val in remaining.items():
        out.append(f"{key}={val}")
    content = "\n".join(out) + "\n"
    # Atomic write: render to a temp file in the same dir, fsync, then os.replace
    # so a crash/power-loss/disk-full mid-write can never truncate the real .env.
    fd, tmp = tempfile.mkstemp(dir=str(ENV_PATH.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, ENV_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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

    # --- Market data provider ---
    # "yfinance" (free, delayed) or "polygon" (real-time stocks + futures).
    market_data_provider: str = os.getenv("MARKET_DATA_PROVIDER", "yfinance").lower()
    polygon_api_key: str = os.getenv("POLYGON_API_KEY", "")

    # --- Market polling ---
    tracked_assets: List[str] = field(
        default_factory=lambda: _split_csv(os.getenv("TRACKED_ASSETS", "")) or list(DEFAULT_ASSETS)
    )
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "45"))
    # An asset whose latest bar is older than this is treated as "closed" and
    # excluded from the mood (e.g. stale cash equities while futures still trade).
    # Generous enough not to flag delayed-but-live feeds (yfinance ~15 min).
    stale_after_seconds: int = int(os.getenv("STALE_AFTER_SECONDS", "1800"))

    # --- Weather (optional mood factor) ---
    # Preset starting location (city or ZIP); can also be set live in the UI.
    weather_location: str = os.getenv("WEATHER_LOCATION", "")

    # --- Auto-sync + DJ stream ---
    # How often (seconds) the playlist re-syncs to the market while auto-sync is
    # on, even if the emotion hasn't changed.
    auto_sync_interval_seconds: int = int(os.getenv("AUTO_SYNC_INTERVAL_SECONDS", "180"))
    # DJ mode: fade the device volume down/up around track boundaries for a
    # continuous-stream feel. Tick is how often the fade loop checks playback.
    dj_tick_seconds: float = float(os.getenv("DJ_TICK_SECONDS", "2"))
    dj_fade_seconds: float = float(os.getenv("DJ_FADE_SECONDS", "6"))
    dj_floor_volume: int = int(os.getenv("DJ_FLOOR_VOLUME", "20"))

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

    @property
    def polygon_configured(self) -> bool:
        return bool(self.polygon_api_key)

    # Scopes required for: reading profile, creating/editing playlists, and
    # controlling/reading playback on an active device.
    spotify_scopes: str = (
        "user-read-private "
        "playlist-modify-public playlist-modify-private playlist-read-private "
        "user-read-playback-state user-modify-playback-state user-read-currently-playing"
    )


settings = Settings()
