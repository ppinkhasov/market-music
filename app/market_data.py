"""Market data engine.

Fetches live-ish prices for the tracked basket and derives the signals the
emotion engine needs. Built around an `AssetSpec` (what to fetch + from where)
and a `MarketDataProvider` interface, so sources can be mixed per-asset.

Providers:
  - YFinanceProvider: free, no key, ~15-min delayed. Default.
  - PolygonProvider: real-time stocks + futures (entitlements permitting), with
    automatic per-symbol fallback to yfinance for anything it can't serve.

All price-action derivation lives in one place (`_metrics_from_series`), so
both providers feed plain close-price lists and get identical metrics.
"""
from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx
import pandas as pd
import yfinance as yf

from .config import settings
from .models import AssetMetrics, MarketSnapshot

# Trend classification threshold (intraday % move).
_TREND_FLAT_PCT = 0.15
# ~ trading minutes in a US session, used to annualize 1-minute realized vol.
_MINUTES_PER_YEAR = 252 * 390


@dataclass
class AssetSpec:
    """One tracked asset: how to display it and where to fetch it."""
    symbol: str                 # display symbol, e.g. "SPY", "ES", "BTC"
    name: str
    source: str                 # "polygon_stock" | "polygon_future" | "yfinance"
    source_id: str              # provider-specific id (ticker / product code / yf symbol)
    klass: str                  # "equity" | "crypto" | "vol" (drives aggregation)
    weight: float = 1.0
    kind: str = "stock"         # "stock" | "future" | "etf" | "index"
    yf_fallback: Optional[str] = None  # yfinance symbol to use if the source fails

    @property
    def provider_source_label(self) -> str:
        return "polygon" if self.source.startswith("polygon") else "yfinance"


@dataclass
class RawSeries:
    """Provider output for one asset: ascending close prices + context."""
    intraday: List[float] = field(default_factory=list)   # 1-min closes, oldest->newest
    daily: List[float] = field(default_factory=list)       # daily closes, oldest->newest
    today_open: Optional[float] = None
    market_open: bool = False
    last_ts: Optional[float] = None    # epoch seconds of the latest intraday bar
    source_label: str = ""
    error: Optional[str] = None


# --- default baskets -------------------------------------------------------

# yfinance basket symbol -> (name, klass, kind)
_YF_META = {
    "SPY": ("S&P 500 (SPY)", "equity", "etf"),
    "QQQ": ("Nasdaq 100 (QQQ)", "equity", "etf"),
    "BTC-USD": ("Bitcoin", "crypto", "crypto"),
    "ETH-USD": ("Ethereum", "crypto", "crypto"),
    "^VIX": ("Volatility Index (VIX)", "vol", "index"),
    "ES=F": ("S&P 500 Futures (ES)", "equity", "future"),
    "NQ=F": ("Nasdaq 100 Futures (NQ)", "equity", "future"),
}


def _yf_spec(symbol: str) -> AssetSpec:
    name, klass, kind = _YF_META.get(symbol, (symbol, "equity", "stock"))
    return AssetSpec(symbol=symbol, name=name, source="yfinance",
                     source_id=symbol, klass=klass, weight=1.0, kind=kind,
                     yf_fallback=symbol)


# Advanced (Polygon) basket: real-time stocks + futures, VIX via yfinance.
# Equity futures (ES/NQ) extend SPY/QQQ overnight, so they carry lower weight to
# avoid double-counting the same index.
ADVANCED_BASKET: List[AssetSpec] = [
    AssetSpec("SPY", "S&P 500 (SPY)", "polygon_stock", "SPY", "equity", 1.0, "etf", "SPY"),
    AssetSpec("QQQ", "Nasdaq 100 (QQQ)", "polygon_stock", "QQQ", "equity", 1.0, "etf", "QQQ"),
    AssetSpec("ES", "S&P 500 Futures (ES)", "polygon_future", "ES", "equity", 0.6, "future", "ES=F"),
    AssetSpec("NQ", "Nasdaq 100 Futures (NQ)", "polygon_future", "NQ", "equity", 0.6, "future", "NQ=F"),
    AssetSpec("BTC", "Bitcoin Futures (BTC)", "polygon_future", "BTC", "crypto", 0.6, "future", "BTC-USD"),
    AssetSpec("ETH", "Ether Futures (ETH)", "polygon_future", "ETH", "crypto", 0.4, "future", "ETH-USD"),
    AssetSpec("VIX", "Volatility Index (VIX)", "yfinance", "^VIX", "vol", 1.0, "index", "^VIX"),
]


def resolve_basket() -> List[AssetSpec]:
    """Pick the asset basket based on configuration."""
    if settings.market_data_provider == "polygon" and settings.polygon_configured:
        return list(ADVANCED_BASKET)
    return [_yf_spec(s) for s in settings.tracked_assets]


# --- shared metric derivation ---------------------------------------------

def _pct(curr: Optional[float], base: Optional[float]) -> Optional[float]:
    if curr is None or base is None or base == 0:
        return None
    return (curr / base - 1.0) * 100.0


def _epoch_seconds(v) -> Optional[float]:
    """Normalize a timestamp (s/ms/us/ns) to epoch seconds."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1e17:      # nanoseconds
        return v / 1e9
    if v > 1e14:      # microseconds
        return v / 1e6
    if v > 1e11:      # milliseconds
        return v / 1e3
    return v          # seconds


def _series_is_stale(raw: "Optional[RawSeries]") -> bool:
    """True if a raw series has no intraday data or its latest bar is too old."""
    if raw is None or not raw.intraday:
        return True
    if raw.last_ts is None:
        return False  # has intraday but no timestamp -> assume live
    return (time.time() - raw.last_ts) > settings.stale_after_seconds


def _trend(change_pct: Optional[float]) -> str:
    if change_pct is None:
        return "flat"
    if change_pct > _TREND_FLAT_PCT:
        return "up"
    if change_pct < -_TREND_FLAT_PCT:
        return "down"
    return "flat"


def _realized_vol(closes: List[float], periods_per_year: int) -> Optional[float]:
    # Needs at least 3 closes (2 returns) for a sample stdev. Callers gate
    # intraday at >=10; the daily fallback supplies short series.
    if len(closes) < 3:
        return None
    rets = [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))
            if closes[i - 1]]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    sigma = math.sqrt(var)
    if math.isnan(sigma):
        return None
    return sigma * math.sqrt(periods_per_year) * 100.0


def _metrics_from_series(spec: AssetSpec, raw: RawSeries) -> AssetMetrics:
    """Build AssetMetrics from ascending close lists (shared by all providers)."""
    m = AssetMetrics(symbol=spec.symbol, name=spec.name,
                     source=raw.source_label or spec.provider_source_label,
                     kind=spec.kind)
    if raw.error:
        m.error = raw.error

    intraday = raw.intraday or []
    daily = raw.daily or []

    prev_close = float(daily[-2]) if len(daily) >= 2 else None
    price = float(intraday[-1]) if intraday else (float(daily[-1]) if daily else None)

    m.price = price
    m.prev_close = prev_close
    m.market_open = raw.market_open and bool(intraday)
    # Staleness: an asset whose latest bar is old is "closed" (e.g. cash equities
    # over the weekend while index futures still trade). Excluded from the mood.
    if not intraday:
        m.stale = True
    elif raw.last_ts is not None:
        m.data_age_seconds = time.time() - raw.last_ts
        m.stale = m.data_age_seconds > settings.stale_after_seconds
    else:
        m.stale = False  # has intraday but no usable timestamp -> assume live
    m.change_pct = _pct(price, prev_close)
    m.gap_pct = _pct(raw.today_open, prev_close)

    if len(intraday) >= 6:
        m.change_5m = _pct(intraday[-1], intraday[-6])
    if len(intraday) >= 61:
        m.change_1h = _pct(intraday[-1], intraday[-61])

    if len(intraday) >= 10:
        m.realized_vol = _realized_vol(intraday, _MINUTES_PER_YEAR)
    elif len(daily) >= 3:
        m.realized_vol = _realized_vol(daily, 252)

    m.trend = _trend(m.change_pct)
    return m


def _intraday_reversal(closes: List[float]) -> Optional[float]:
    """Detect an intraday reversal from a 1-min close list. -1..+1."""
    s = [c for c in closes if c is not None]
    if len(s) < 40:
        return None
    first, mid, last = float(s[0]), float(s[len(s) // 2]), float(s[-1])
    # Late-window base = last 30 bars, but never before the midpoint, so the
    # early (first->mid) and late (base->last) windows can't overlap on short series.
    recent_base = float(s[max(len(s) // 2, len(s) - 30)])
    early = mid - first
    late = last - recent_base
    if early == 0 or first == 0 or recent_base == 0:
        return 0.0
    early_pct, late_pct = early / first, late / recent_base
    if early_pct * late_pct < 0:
        mag = min(1.0, (abs(early_pct) + abs(late_pct)) * 50.0)
        return math.copysign(mag, late_pct)
    return 0.0


# --- providers -------------------------------------------------------------

class MarketDataProvider(ABC):
    @abstractmethod
    def fetch(self, specs: List[AssetSpec]) -> Dict[str, RawSeries]:
        ...


class YFinanceProvider(MarketDataProvider):
    """Free MVP provider backed by Yahoo Finance via yfinance."""

    def fetch(self, specs: List[AssetSpec]) -> Dict[str, RawSeries]:
        if not specs:
            return {}
        ids = [s.source_id for s in specs]
        intraday_df = self._download(ids, period="1d", interval="1m")
        daily_df = self._download(ids, period="1mo", interval="1d")
        out: Dict[str, RawSeries] = {}
        for spec in specs:
            try:
                out[spec.symbol] = self._series_for(spec.source_id, intraday_df, daily_df)
            except Exception as exc:
                out[spec.symbol] = RawSeries(source_label="yfinance", error=str(exc))
        return out

    @staticmethod
    def _download(ids: List[str], period: str, interval: str) -> Optional[pd.DataFrame]:
        try:
            df = yf.download(tickers=ids, period=period, interval=interval,
                             group_by="ticker", auto_adjust=False,
                             progress=False, threads=True)
            return df if df is not None and not df.empty else None
        except Exception:
            return None

    @staticmethod
    def _slice(df: Optional[pd.DataFrame], sid: str) -> Optional[pd.DataFrame]:
        if df is None or df.empty:
            return None
        cols = df.columns
        if isinstance(cols, pd.MultiIndex):
            if sid in cols.get_level_values(0):
                sub = df[sid]
            else:
                return None
        else:
            sub = df
        sub = sub.dropna(how="all")
        return sub if not sub.empty else None

    def _series_for(self, sid: str, intraday_df, daily_df) -> RawSeries:
        intraday = self._slice(intraday_df, sid)
        daily = self._slice(daily_df, sid)
        raw = RawSeries(source_label="yfinance")
        if intraday is not None and "Close" in intraday:
            closes = intraday["Close"].dropna()
            raw.intraday = [float(x) for x in closes.tolist()]
            raw.market_open = bool(raw.intraday)
            if len(closes):
                try:  # last bar's timestamp -> epoch seconds (index is tz-aware)
                    raw.last_ts = float(pd.Timestamp(closes.index[-1]).timestamp())
                except Exception:
                    pass
        if daily is not None and "Close" in daily:
            raw.daily = [float(x) for x in daily["Close"].dropna().tolist()]
            if "Open" in daily:
                opens = daily["Open"].dropna()
                if len(opens):
                    raw.today_open = float(opens.iloc[-1])
        return raw


class PolygonProvider(MarketDataProvider):
    """Real-time stocks + futures via Polygon (api.polygon.io). Front-month
    futures contracts are resolved once per day and cached."""

    STOCK_BASE = "https://api.polygon.io"
    FUT_BASE = "https://api.polygon.io"
    # Tight per-phase timeouts so a hung endpoint can't hold a call near 20s.
    _TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

    def __init__(self, api_key: str):
        self._key = api_key
        self._front_cache: Dict[str, Tuple[str, str]] = {}  # product_code -> (date, ticker)
        # Persistent client -> keep-alive connection reuse across the ~12 calls
        # per poll instead of a fresh TCP+TLS handshake per request.
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=self._TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
        )

    def _get(self, url: str, params: dict) -> dict:
        # One bounded retry for transient rate-limit / gateway errors.
        r = None
        for attempt in range(2):
            r = self._client.get(url, params=params)
            if r.status_code in (429, 502, 503, 504) and attempt == 0:
                delay = 1.0
                try:
                    ra = r.headers.get("Retry-After")
                    if ra:
                        delay = min(float(ra), 3.0)
                except (TypeError, ValueError):
                    pass
                time.sleep(delay)
                continue
            break
        if r is None or r.status_code >= 400:
            code = r.status_code if r is not None else "n/a"
            text = r.text[:120] if r is not None else ""
            raise RuntimeError(f"polygon {code}: {text}")
        return r.json()

    def fetch(self, specs: List[AssetSpec]) -> Dict[str, RawSeries]:
        out: Dict[str, RawSeries] = {}
        for spec in specs:
            try:
                if spec.source == "polygon_stock":
                    out[spec.symbol] = self._fetch_stock(spec.source_id)
                elif spec.source == "polygon_future":
                    out[spec.symbol] = self._fetch_future(spec.source_id)
                else:
                    out[spec.symbol] = RawSeries(error="unsupported source")
            except Exception as exc:
                out[spec.symbol] = RawSeries(source_label="polygon", error=str(exc))
        return out

    # --- stocks ---
    def _fetch_stock(self, ticker: str) -> RawSeries:
        today = date.today()
        frm_min = (today - timedelta(days=5)).isoformat()
        frm_day = (today - timedelta(days=40)).isoformat()
        to = today.isoformat()
        raw = RawSeries(source_label="polygon")

        j = self._get(f"{self.STOCK_BASE}/v2/aggs/ticker/{ticker}/range/1/minute/{frm_min}/{to}",
                      {"adjusted": "true", "sort": "asc", "limit": 50000})
        bars = j.get("results") or []
        raw.intraday = [float(b["c"]) for b in bars if b.get("c") is not None]
        raw.market_open = bool(raw.intraday)
        if bars and bars[-1].get("t") is not None:
            raw.last_ts = _epoch_seconds(bars[-1]["t"])  # ms epoch

        jd = self._get(f"{self.STOCK_BASE}/v2/aggs/ticker/{ticker}/range/1/day/{frm_day}/{to}",
                       {"adjusted": "true", "sort": "asc", "limit": 60})
        dbars = jd.get("results") or []
        raw.daily = [float(b["c"]) for b in dbars if b.get("c") is not None]
        if dbars and dbars[-1].get("o") is not None:
            raw.today_open = float(dbars[-1]["o"])
        return raw

    # --- futures ---
    def _front_month(self, product_code: str) -> str:
        today = date.today().isoformat()
        cached = self._front_cache.get(product_code)
        if cached and cached[0] == today:
            return cached[1]
        # NB: this endpoint wants `date` (not `as_of`) — verified live: `date`
        # returns the current active contracts (front-month resolves to e.g.
        # ESM6 with data), whereas `as_of` returns a wrong far-dated contract.
        j = self._get(f"{self.FUT_BASE}/futures/v1/contracts",
                      {"product_code": product_code, "active": "true",
                       "date": today, "limit": 1000})
        rows = j.get("results") or []
        # Outright contracts only (skip calendar spreads "A-B"), nearest expiry.
        outr = [c for c in rows
                if c.get("ticker") and "-" not in c["ticker"]
                and (c.get("last_trade_date") or "9999") >= today]
        outr.sort(key=lambda c: c.get("last_trade_date") or "9999")
        if not outr:
            raise RuntimeError(f"no active front-month for {product_code}")
        ticker = outr[0]["ticker"]
        self._front_cache[product_code] = (today, ticker)
        return ticker

    def _fetch_future(self, product_code: str) -> RawSeries:
        ticker = self._front_month(product_code)
        today = date.today()
        frm_min = (today - timedelta(days=5)).isoformat()
        frm_day = (today - timedelta(days=40)).isoformat()
        raw = RawSeries(source_label="polygon")

        j = self._get(f"{self.FUT_BASE}/futures/v1/aggs/{ticker}",
                      {"resolution": "1min", "window_start.gte": frm_min,
                       "sort": "window_start.asc", "limit": 50000})
        bars = j.get("results") or []
        raw.intraday = [float(b["close"]) for b in bars if b.get("close") is not None]
        raw.market_open = bool(raw.intraday)
        if bars and bars[-1].get("window_start") is not None:
            raw.last_ts = _epoch_seconds(bars[-1]["window_start"])

        jd = self._get(f"{self.FUT_BASE}/futures/v1/aggs/{ticker}",
                       {"resolution": "1session", "window_start.gte": frm_day,
                        "sort": "window_start.asc", "limit": 60})
        dbars = jd.get("results") or []
        raw.daily = [float(b["close"]) for b in dbars if b.get("close") is not None]
        if dbars and dbars[-1].get("open") is not None:
            raw.today_open = float(dbars[-1]["open"])
        return raw


# --- aggregation -----------------------------------------------------------

def _aggregate(specs: List[AssetSpec], assets: Dict[str, AssetMetrics],
               intraday_closes: Dict[str, List[float]]) -> dict:
    spec_by_symbol = {s.symbol: s for s in specs}
    weighted_sum = 0.0
    weight_total = 0.0
    risk_changes: List[float] = []
    vix_change: Optional[float] = None

    # Only live assets drive the mood; stale (closed-market) assets are skipped,
    # so e.g. on a weekend the read comes from index futures + crypto, not from
    # Friday's frozen cash-equity prints.
    for sym, a in assets.items():
        spec = spec_by_symbol.get(sym)
        if not spec or a.change_pct is None or a.stale:
            continue
        if spec.klass == "vol":
            vix_change = a.change_pct
            continue
        squashed = math.tanh(a.change_pct / 2.5)
        weighted_sum += squashed * spec.weight
        weight_total += spec.weight
        risk_changes.append(a.change_pct)

    risk_on = weighted_sum / weight_total if weight_total else 0.0
    if vix_change is not None:
        risk_on -= 0.5 * math.tanh(vix_change / 15.0)
    risk_on = max(-1.0, min(1.0, risk_on))

    # Volatility shock: VIX level/change, equity gaps, fast short-window moves.
    vol_shock = 0.0
    vix = next((a for s, a in assets.items()
                if spec_by_symbol.get(s) and spec_by_symbol[s].klass == "vol"
                and not a.stale), None)
    if vix and vix.price is not None:
        vol_shock = max(vol_shock, max(0.0, min(1.0, (vix.price - 15.0) / 30.0)))
    if vix_change is not None:
        vol_shock = max(vol_shock, max(0.0, math.tanh(vix_change / 20.0)))
    for sym, a in assets.items():
        spec = spec_by_symbol.get(sym)
        if spec and spec.klass == "equity" and not a.stale and a.gap_pct is not None:
            vol_shock = max(vol_shock, max(0.0, min(1.0, (abs(a.gap_pct) - 0.5) / 2.5)))
    fast = [abs(a.change_5m) for s, a in assets.items()
            if spec_by_symbol.get(s) and spec_by_symbol[s].klass != "vol"
            and not a.stale and a.change_5m is not None]
    if fast:
        vol_shock = max(vol_shock, max(0.0, min(1.0, (max(fast) - 0.3) / 1.2)))
    vol_shock = max(0.0, min(1.0, vol_shock))

    dispersion = 0.0
    if len(risk_changes) >= 2:
        mean = sum(risk_changes) / len(risk_changes)
        std = math.sqrt(sum((c - mean) ** 2 for c in risk_changes) / len(risk_changes))
        dispersion = max(0.0, min(1.0, std / 2.0))

    rev_vals = [_intraday_reversal(intraday_closes[s])
                for s in intraday_closes
                if spec_by_symbol.get(s) and spec_by_symbol[s].klass != "vol"
                and not assets[s].stale]
    rev_vals = [r for r in rev_vals if r is not None]
    reversal = sum(rev_vals) / len(rev_vals) if rev_vals else 0.0

    return {
        "risk_on_score": risk_on,
        "vol_shock_score": vol_shock,
        "dispersion": dispersion,
        "reversal_score": max(-1.0, min(1.0, reversal)),
    }


# --- providers registry ----------------------------------------------------

_yf_provider = YFinanceProvider()
_polygon_provider: Optional[PolygonProvider] = None


def _get_polygon() -> Optional[PolygonProvider]:
    global _polygon_provider
    if not settings.polygon_configured:
        return None
    if _polygon_provider is None:
        _polygon_provider = PolygonProvider(settings.polygon_api_key)
    return _polygon_provider


def fetch_snapshot(specs: Optional[List[AssetSpec]] = None) -> MarketSnapshot:
    """Fetch and assemble a full market snapshot. Blocking (run in a thread)."""
    if specs is None:
        specs = resolve_basket()
    now = datetime.now(timezone.utc).isoformat()

    # Group specs by provider.
    yf_specs = [s for s in specs if s.source == "yfinance"]
    poly_specs = [s for s in specs if s.source.startswith("polygon")]

    raw: Dict[str, RawSeries] = {}
    if yf_specs:
        raw.update(_yf_provider.fetch(yf_specs))

    polygon = _get_polygon()
    if poly_specs and polygon is not None:
        raw.update(polygon.fetch(poly_specs))
    elif poly_specs:
        # Polygon not configured but specs ask for it -> all fall back to yfinance.
        raw.update(_yf_provider.fetch([_fallback_spec(s) for s in poly_specs]))

    # Per-symbol fallback: any Polygon asset that errored or returned no price
    # is retried via yfinance using its yf_fallback id.
    # Retry via yfinance when Polygon errored / returned nothing, or when a
    # crypto asset is stale (CME crypto futures close on weekends, but yfinance
    # crypto spot trades 24/7 — keeps a live crypto floor for the mood).
    failed = [s for s in poly_specs
              if s.yf_fallback and (raw.get(s.symbol) is None
                                    or raw[s.symbol].error
                                    or (not raw[s.symbol].intraday and not raw[s.symbol].daily)
                                    or (s.klass == "crypto" and _series_is_stale(raw.get(s.symbol))))]
    if failed:
        fb = _yf_provider.fetch([_fallback_spec(s) for s in failed])
        for s in failed:
            r = fb.get(s.symbol)
            if not r or not (r.intraday or r.daily):
                continue
            cur = raw.get(s.symbol)
            # Use the fallback when Polygon had nothing usable, or it's fresher.
            if (cur is None or cur.error or (not cur.intraday and not cur.daily)
                    or (_series_is_stale(cur) and not _series_is_stale(r))):
                raw[s.symbol] = r

    # Build metrics + collect intraday close lists for reversal.
    assets: Dict[str, AssetMetrics] = {}
    intraday_closes: Dict[str, List[float]] = {}
    for spec in specs:
        r = raw.get(spec.symbol) or RawSeries(error="no data")
        assets[spec.symbol] = _metrics_from_series(spec, r)
        if r.intraday:
            intraday_closes[spec.symbol] = r.intraday

    agg = _aggregate(specs, assets, intraday_closes)
    any_data = any(a.price is not None for a in assets.values())
    return MarketSnapshot(timestamp=now, assets=assets, any_data=any_data, **agg)


def _fallback_spec(spec: AssetSpec) -> AssetSpec:
    """A yfinance version of a Polygon spec (same display symbol)."""
    return AssetSpec(symbol=spec.symbol, name=spec.name, source="yfinance",
                     source_id=spec.yf_fallback or spec.symbol, klass=spec.klass,
                     weight=spec.weight, kind=spec.kind, yf_fallback=spec.yf_fallback)
