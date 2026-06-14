"""Market data engine.

Fetches live-ish prices for the tracked basket and derives the signals the
emotion engine needs. Built around a small provider interface so a paid feed
(Polygon, Alpaca, ...) can be dropped in later without touching the rest of the
app.

The MVP provider uses `yfinance` (free, public, no key). Yahoo's intraday data
is delayed ~15 minutes for equities, which is perfectly fine for a "market
mood" app.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
import yfinance as yf

from .models import AssetMetrics, MarketSnapshot

# Friendly labels + asset-class weights used when aggregating into a single
# "risk-on" reading. The VIX is special-cased (it is the inverse of risk-on).
ASSET_META = {
    "SPY": {"name": "S&P 500 (SPY)", "klass": "equity", "weight": 1.0},
    "QQQ": {"name": "Nasdaq 100 (QQQ)", "klass": "equity", "weight": 1.0},
    "BTC-USD": {"name": "Bitcoin", "klass": "crypto", "weight": 0.6},
    "ETH-USD": {"name": "Ethereum", "klass": "crypto", "weight": 0.4},
    "^VIX": {"name": "Volatility Index (VIX)", "klass": "vol", "weight": 1.0},
}

# Trend classification threshold (intraday % move).
_TREND_FLAT_PCT = 0.15
# ~ trading minutes in a US session, used to annualize 1-minute realized vol.
_MINUTES_PER_YEAR = 252 * 390


def meta_for(symbol: str) -> dict:
    return ASSET_META.get(symbol, {"name": symbol, "klass": "equity", "weight": 0.5})


class MarketDataProvider(ABC):
    """Interface for a market data source."""

    @abstractmethod
    def fetch(self, symbols: List[str]) -> Dict[str, AssetMetrics]:
        ...


def _pct(curr: Optional[float], base: Optional[float]) -> Optional[float]:
    if curr is None or base is None or base == 0:
        return None
    return (curr / base - 1.0) * 100.0


def _last_valid(series: Optional[pd.Series]) -> Optional[float]:
    if series is None or len(series) == 0:
        return None
    s = series.dropna()
    if s.empty:
        return None
    return float(s.iloc[-1])


class YFinanceProvider(MarketDataProvider):
    """Free MVP provider backed by Yahoo Finance via yfinance."""

    def fetch(self, symbols: List[str]) -> Dict[str, AssetMetrics]:
        results: Dict[str, AssetMetrics] = {}

        # Two batched downloads: one intraday (1m bars, today) for live signals,
        # one daily (recent history) for prior close, gaps and a vol baseline.
        intraday = self._download(symbols, period="1d", interval="1m")
        daily = self._download(symbols, period="1mo", interval="1d")

        for symbol in symbols:
            try:
                results[symbol] = self._build_metrics(
                    symbol,
                    self._slice(intraday, symbol),
                    self._slice(daily, symbol),
                )
            except Exception as exc:  # never let one bad symbol kill the poll
                m = meta_for(symbol)
                results[symbol] = AssetMetrics(symbol=symbol, name=m["name"], error=str(exc))
        return results

    # --- yfinance plumbing -------------------------------------------------

    @staticmethod
    def _download(symbols: List[str], period: str, interval: str) -> Optional[pd.DataFrame]:
        try:
            df = yf.download(
                tickers=symbols,
                period=period,
                interval=interval,
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
            if df is None or df.empty:
                return None
            return df
        except Exception:
            return None

    @staticmethod
    def _slice(df: Optional[pd.DataFrame], symbol: str) -> Optional[pd.DataFrame]:
        """Pull one symbol's OHLCV frame out of a (possibly multi-index) download."""
        if df is None or df.empty:
            return None
        cols = df.columns
        if isinstance(cols, pd.MultiIndex):
            if symbol in cols.get_level_values(0):
                sub = df[symbol]
            else:
                return None
        else:
            # Single-symbol download: columns are just OHLCV.
            sub = df
        sub = sub.dropna(how="all")
        return sub if not sub.empty else None

    # --- signal derivation -------------------------------------------------

    def _build_metrics(
        self,
        symbol: str,
        intraday: Optional[pd.DataFrame],
        daily: Optional[pd.DataFrame],
    ) -> AssetMetrics:
        m = meta_for(symbol)
        metrics = AssetMetrics(symbol=symbol, name=m["name"])

        # Prior close + today's open come from the daily series (most reliable).
        prev_close = None
        today_open = None
        if daily is not None and "Close" in daily and len(daily) >= 1:
            closes = daily["Close"].dropna()
            if len(closes) >= 2:
                prev_close = float(closes.iloc[-2])
            # With only one daily close we have no genuine prior close; leave
            # prev_close as None so change_pct/gap_pct become None (not a
            # misleading ~0% computed against today's own close).
            if "Open" in daily:
                opens = daily["Open"].dropna()
                if len(opens) >= 1:
                    today_open = float(opens.iloc[-1])

        # Latest price: prefer fresh intraday, else last daily close.
        price = None
        market_open = False
        if intraday is not None and "Close" in intraday:
            price = _last_valid(intraday["Close"])
            market_open = price is not None
        if price is None and daily is not None and "Close" in daily:
            price = _last_valid(daily["Close"])
            # Without intraday we treat the latest daily close as prior close's
            # successor, so prev_close should be the close *before* it.

        metrics.price = price
        metrics.prev_close = prev_close
        metrics.market_open = market_open

        # Intraday change vs prior close (the headline number).
        metrics.change_pct = _pct(price, prev_close)
        # Gap: today's open vs prior close.
        metrics.gap_pct = _pct(today_open, prev_close)

        # Short-window momentum from 1m bars (position-based, robust to gaps).
        if intraday is not None and "Close" in intraday:
            closes = intraday["Close"].dropna()
            if len(closes) >= 6:
                metrics.change_5m = _pct(float(closes.iloc[-1]), float(closes.iloc[-6]))
            if len(closes) >= 61:
                metrics.change_1h = _pct(float(closes.iloc[-1]), float(closes.iloc[-61]))
            metrics.realized_vol = self._realized_vol(closes)

        # If the market is closed (no intraday), fall back to a daily vol proxy.
        if metrics.realized_vol is None and daily is not None and "Close" in daily:
            metrics.realized_vol = self._daily_realized_vol(daily["Close"].dropna())

        metrics.trend = self._trend(metrics.change_pct)
        return metrics

    @staticmethod
    def _realized_vol(closes: pd.Series) -> Optional[float]:
        """Annualized realized vol (%) from intraday 1-minute returns."""
        if len(closes) < 10:
            return None
        rets = closes.pct_change().dropna()
        if rets.empty:
            return None
        sigma = float(rets.std())
        if math.isnan(sigma):
            return None
        return sigma * math.sqrt(_MINUTES_PER_YEAR) * 100.0

    @staticmethod
    def _daily_realized_vol(closes: pd.Series) -> Optional[float]:
        if len(closes) < 3:
            return None
        rets = closes.pct_change().dropna()
        if rets.empty:
            return None
        sigma = float(rets.std())
        if math.isnan(sigma):
            return None
        return sigma * math.sqrt(252) * 100.0

    @staticmethod
    def _trend(change_pct: Optional[float]) -> str:
        if change_pct is None:
            return "flat"
        if change_pct > _TREND_FLAT_PCT:
            return "up"
        if change_pct < -_TREND_FLAT_PCT:
            return "down"
        return "flat"

    @staticmethod
    def intraday_reversal(intraday_close: pd.Series) -> Optional[float]:
        """Detect an intraday reversal: compare the first-half drift to the last
        30 minutes. Returns -1..+1 (positive = reversed upward)."""
        s = intraday_close.dropna()
        if len(s) < 40:
            return None
        first = float(s.iloc[0])
        mid = float(s.iloc[len(s) // 2])
        last = float(s.iloc[-1])
        recent_base = float(s.iloc[max(0, len(s) - 30)])
        early_dir = mid - first
        late_dir = last - recent_base
        # Reversal when early and late move in opposite directions.
        if early_dir == 0 or last == 0:
            return 0.0
        early_pct = early_dir / first
        late_pct = late_dir / recent_base
        if early_pct * late_pct < 0:  # opposite signs => reversal
            mag = min(1.0, (abs(early_pct) + abs(late_pct)) * 50.0)
            return math.copysign(mag, late_pct)
        return 0.0


def _aggregate(symbols: List[str], assets: Dict[str, AssetMetrics],
               intraday_closes: Dict[str, pd.Series]) -> dict:
    """Combine per-asset metrics into the bounded aggregate signals."""
    # Risk-on: weighted, squashed average of risk-asset moves; VIX inverts.
    weighted_sum = 0.0
    weight_total = 0.0
    risk_changes: List[float] = []
    vix_change: Optional[float] = None

    for symbol in symbols:
        a = assets.get(symbol)
        if not a or a.change_pct is None:
            continue
        m = meta_for(symbol)
        if m["klass"] == "vol":
            vix_change = a.change_pct
            continue
        w = m["weight"]
        # Squash each asset's % move into -1..1 (≈±2.5% saturates).
        squashed = math.tanh(a.change_pct / 2.5)
        weighted_sum += squashed * w
        weight_total += w
        risk_changes.append(a.change_pct)

    risk_on = weighted_sum / weight_total if weight_total else 0.0
    # VIX up => risk-off. A +15% VIX spike is a strong risk-off signal.
    if vix_change is not None:
        risk_on -= 0.5 * math.tanh(vix_change / 15.0)
    risk_on = max(-1.0, min(1.0, risk_on))

    # Volatility shock: driven by VIX level/change, equity realized vol, gaps.
    vol_shock = 0.0
    vix = assets.get("^VIX")
    if vix and vix.price is not None:
        # Level term: VIX 15 -> ~0, 30 -> ~0.5, 45+ -> ~1.
        vol_shock = max(vol_shock, max(0.0, min(1.0, (vix.price - 15.0) / 30.0)))
    if vix_change is not None:
        vol_shock = max(vol_shock, max(0.0, math.tanh(vix_change / 20.0)))
    # Big gaps in equities are a shock signal too.
    for symbol in ("SPY", "QQQ"):
        a = assets.get(symbol)
        if a and a.gap_pct is not None:
            vol_shock = max(vol_shock, max(0.0, min(1.0, (abs(a.gap_pct) - 0.5) / 2.5)))
    # Fast short-window moves (velocity) across risk assets. Exclude the VIX:
    # its 5-minute % swings are structurally far larger and are already
    # captured by the level/change terms above (avoids double-counting).
    fast = [abs(assets[s].change_5m) for s in symbols
            if meta_for(s)["klass"] != "vol"
            and assets.get(s) and assets[s].change_5m is not None]
    if fast:
        vol_shock = max(vol_shock, max(0.0, min(1.0, (max(fast) - 0.3) / 1.2)))
    vol_shock = max(0.0, min(1.0, vol_shock))

    # Dispersion: how much risk assets disagree (chop/indecision signal).
    dispersion = 0.0
    if len(risk_changes) >= 2:
        mean = sum(risk_changes) / len(risk_changes)
        var = sum((c - mean) ** 2 for c in risk_changes) / len(risk_changes)
        std = math.sqrt(var)
        dispersion = max(0.0, min(1.0, std / 2.0))  # ~2% std saturates

    # Reversal: average reversal signal across equities + BTC.
    rev_vals: List[float] = []
    for symbol in ("SPY", "QQQ", "BTC-USD"):
        s = intraday_closes.get(symbol)
        if s is not None:
            r = YFinanceProvider.intraday_reversal(s)
            if r is not None:
                rev_vals.append(r)
    reversal = sum(rev_vals) / len(rev_vals) if rev_vals else 0.0

    return {
        "risk_on_score": risk_on,
        "vol_shock_score": vol_shock,
        "dispersion": dispersion,
        "reversal_score": max(-1.0, min(1.0, reversal)),
    }


# Module-level provider instance (swap here to use a different feed).
_provider: MarketDataProvider = YFinanceProvider()


def fetch_snapshot(symbols: List[str]) -> MarketSnapshot:
    """Fetch and assemble a full market snapshot. Blocking (run in a thread)."""
    now = datetime.now(timezone.utc).isoformat()

    # Grab intraday closes once for both metrics and reversal/aggregation.
    if isinstance(_provider, YFinanceProvider):
        intraday_df = YFinanceProvider._download(symbols, period="1d", interval="1m")
        daily_df = YFinanceProvider._download(symbols, period="1mo", interval="1d")
        assets: Dict[str, AssetMetrics] = {}
        intraday_closes: Dict[str, pd.Series] = {}
        for symbol in symbols:
            try:
                sub_intraday = YFinanceProvider._slice(intraday_df, symbol)
                sub_daily = YFinanceProvider._slice(daily_df, symbol)
                assets[symbol] = _provider._build_metrics(symbol, sub_intraday, sub_daily)
                if sub_intraday is not None and "Close" in sub_intraday:
                    intraday_closes[symbol] = sub_intraday["Close"].dropna()
            except Exception as exc:
                m = meta_for(symbol)
                assets[symbol] = AssetMetrics(symbol=symbol, name=m["name"], error=str(exc))
    else:
        # Generic provider path. NOTE: the reversal sub-signal needs the
        # intraday close series, which the MarketDataProvider.fetch() contract
        # does not return — so reversal_score is 0.0 for non-yfinance providers
        # until the interface is extended to carry intraday closes.
        assets = _provider.fetch(symbols)
        intraday_closes = {}

    agg = _aggregate(symbols, assets, intraday_closes)
    any_data = any(a.price is not None for a in assets.values())

    return MarketSnapshot(
        timestamp=now,
        assets=assets,
        any_data=any_data,
        **agg,
    )
