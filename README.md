# 🎵 market-music

**A Spotify app that turns live market conditions into music.**

market-music watches the stock & crypto markets in near-real-time, classifies the
current *market emotion* (euphoric, fearful, chaotic, calm, …), uses DeepSeek to
interpret that mood as music, and builds/updates a Spotify playlist to match the
regime — optionally starting playback on an active device.

> This is a modern rewrite of the original Raspberry-Pi-bound `market-music`
> concept. The legacy scripts are preserved under [`legacy/`](legacy/).

![mood dashboard](https://user-images.githubusercontent.com/6701857/176485770-2cf22726-7a57-427f-9654-24ddce894b8d.gif)

---

## How it works

```
yfinance ──► market engine ──► emotion classifier ──► DeepSeek ──► Spotify search ──► playlist
 (prices)     (signals)         (deterministic rules)   (mood→queries)   (tracks)        (+ playback)
```

1. **Market engine** (`app/market_data.py`) polls SPY, QQQ, BTC, ETH and the VIX
   every ~45s via Yahoo Finance (free, no key) and derives bounded signals:
   intraday % change, 5-min / 1-hour momentum, gap from prior close, realized
   volatility, trend, and three aggregates — **risk-on score**, **volatility
   shock**, **dispersion** — plus an intraday **reversal** detector.
2. **Emotion classifier** (`app/emotion_engine.py`) maps those signals to one of
   ten emotions with **transparent, deterministic rules** (no ML). Returns the
   emotion, a confidence, a human summary, and the inputs.
3. **DeepSeek** (`app/deepseek_client.py`) interprets the mood and proposes
   Spotify search queries + a narrative. *Optional* — if no key is set (or the
   call fails) the app falls back to a built-in per-emotion music mapping.
4. **Spotify** (`app/spotify_client.py`) — OAuth login, then builds the playlist
   from **Search** results (the deprecated Recommendations/Audio-Features
   endpoints are intentionally not used), and can start playback on a device.

The web UI (`app/templates`, `app/static`) is an emotion-reactive dashboard that
polls `/api/state` and recolors itself to the current mood.

## Quick start

```bash
git clone <this repo> && cd market-music
cp .env.example .env          # add your Spotify (+ optional DeepSeek) keys
./run.sh                      # creates .venv, installs deps, starts the server
```

Then open **http://127.0.0.1:8000**.

The **market dashboard works with no keys at all.** Spotify keys unlock playlist
generation and playback; a DeepSeek key upgrades the music selection from the
built-in mapping to LLM-curated queries.

### Manual run

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

## Configuration

Copy `.env.example` to `.env`. Key settings:

| Variable | Purpose |
| --- | --- |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | From the [Spotify dashboard](https://developer.spotify.com/dashboard). Required for playlists. |
| `SPOTIFY_REDIRECT_URI` | Must **exactly** match a Redirect URI on your Spotify app. Default `http://127.0.0.1:8000/auth/callback`. |
| `DEEPSEEK_API_KEY` | Optional. From [platform.deepseek.com](https://platform.deepseek.com). |
| `TRACKED_ASSETS` | Comma-separated tickers (default `SPY,QQQ,BTC-USD,ETH-USD,^VIX`). |
| `POLL_INTERVAL_SECONDS` | Market poll cadence (default `45`). |
| `PLAYLIST_NAME` / `PLAYLIST_SIZE` | The managed playlist's name and length. |

### Spotify app setup

1. Create an app at the Spotify Developer Dashboard.
2. Add the redirect URI **exactly**: `http://127.0.0.1:8000/auth/callback`
   (Spotify rejects `http://localhost` for new apps — use the loopback IP).
3. Copy the Client ID/Secret into `.env`.
4. Playback control requires **Spotify Premium** and an active device.

## API

| Endpoint | Description |
| --- | --- |
| `GET /` | Dashboard UI |
| `GET /api/state` | Full live state: snapshot, emotion, music plan, session, history |
| `GET /api/health` | Liveness + poll count |
| `GET /auth/login` → `GET /auth/callback` | Spotify OAuth |
| `POST /api/playlist/sync` | Build/refresh the managed playlist to the current mood |
| `POST /api/play` | Start playback (`{"device_id": "..."}`, optional) |
| `GET /api/devices` | List the user's Spotify devices |
| `POST /api/settings` | `{"auto_sync": true}` — auto-rebuild playlist on mood change |

Interactive docs at `/docs`.

## The ten market emotions

| Emotion | Regime |
| --- | --- |
| euphoric | broad, clean risk-on rally (low vol) |
| happy | constructive, mildly bullish |
| calm | quiet, range-bound, low volatility |
| anxious | choppy/indecisive, rising volatility |
| fearful | broad risk-off selloff, elevated vol |
| angry | violent, fast selling |
| surprised | sharp intraday reversal |
| chaotic | extreme volatility, assets pulling apart |
| depressed | slow, low-volatility grind lower |
| manic | frenzied, high-volatility melt-up |

## Extending the market feed

`market_data.py` defines a `MarketDataProvider` interface. The MVP ships
`YFinanceProvider`; drop in a `PolygonProvider` (or Alpaca, etc.) and swap the
module-level `_provider` to use a paid/real-time feed without touching the rest
of the app.

## Notes & limitations

- Yahoo Finance data is **delayed** (~15 min for equities) and unofficial — fine
  for a "market mood" toy, not for trading.
- State is in-memory and single-process (one local user). Restarting clears
  sessions.
- This project is for fun/education. **Not financial advice.**

## Project layout

```
app/
  config.py          # env-driven settings
  models.py          # dataclasses (version-agnostic, no pydantic coupling)
  market_data.py     # provider interface + yfinance engine + signals
  emotion_engine.py  # deterministic emotion classifier
  deepseek_client.py # DeepSeek (OpenAI-compatible) mood interpreter
  music.py           # emotion→music mapping + playlist assembly via search
  spotify_client.py  # OAuth + Web API client
  state.py           # sessions, app state, background poller
  main.py            # FastAPI routes + lifespan
  templates/ static/ # web UI
legacy/              # the original Raspberry Pi scripts
```
