"""Domain models.

Plain dataclasses (not pydantic) so the app is independent of any pydantic
version. Each model knows how to serialize itself to a JSON-friendly dict via
`to_dict()`, which the API layer returns directly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional


def _round(value: Optional[float], ndigits: int = 2) -> Optional[float]:
    return round(value, ndigits) if value is not None else None


@dataclass
class AssetMetrics:
    """Per-asset snapshot of price action and derived signals."""

    symbol: str
    name: str
    price: Optional[float] = None
    prev_close: Optional[float] = None
    change_pct: Optional[float] = None        # intraday % vs prior close
    change_5m: Optional[float] = None          # % over last ~5 minutes
    change_1h: Optional[float] = None          # % over last ~60 minutes
    gap_pct: Optional[float] = None            # open vs prior close
    realized_vol: Optional[float] = None       # annualized intraday realized vol (%)
    trend: str = "flat"                        # "up" | "down" | "flat"
    market_open: bool = False                  # had fresh intraday data this poll
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("price", "prev_close", "change_pct", "change_5m", "change_1h",
                  "gap_pct", "realized_vol"):
            d[k] = _round(d[k], 4 if k in ("price", "prev_close") else 2)
        return d


@dataclass
class MarketSnapshot:
    """Aggregate market state at a point in time."""

    timestamp: str
    assets: Dict[str, AssetMetrics] = field(default_factory=dict)
    # Aggregate signals (all bounded):
    risk_on_score: float = 0.0     # -1 (risk-off) .. +1 (risk-on)
    vol_shock_score: float = 0.0   # 0 (calm) .. 1 (volatility shock)
    dispersion: float = 0.0        # 0 .. 1 (how much assets disagree)
    reversal_score: float = 0.0    # -1 .. +1 (intraday reversal magnitude/direction)
    any_data: bool = False         # did we get usable data for at least one asset

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "assets": {s: a.to_dict() for s, a in self.assets.items()},
            "risk_on_score": _round(self.risk_on_score, 3),
            "vol_shock_score": _round(self.vol_shock_score, 3),
            "dispersion": _round(self.dispersion, 3),
            "reversal_score": _round(self.reversal_score, 3),
            "any_data": self.any_data,
        }


@dataclass
class EmotionResult:
    """Output of the deterministic emotion classifier."""

    emotion: str
    confidence: float
    summary: str
    scores: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "emotion": self.emotion,
            "confidence": _round(self.confidence, 2),
            "summary": self.summary,
            "scores": {k: _round(v, 3) for k, v in self.scores.items()},
            "reasons": self.reasons,
        }


@dataclass
class MusicPlan:
    """How the current emotion should be turned into music."""

    emotion: str
    narrative: str
    search_queries: List[str]
    seed_genres: List[str]
    target_energy: float       # 0..1
    target_valence: float      # 0..1 (musical positivity)
    source: str                # "deepseek" | "fallback"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Track:
    name: str
    artist: str
    uri: str
    url: str = ""
    image: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlaylistInfo:
    id: str
    name: str
    url: str
    track_count: int
    tracks: List[Track] = field(default_factory=list)
    updated_for_emotion: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tracks"] = [t if isinstance(t, dict) else t.to_dict() for t in self.tracks]
        return d
