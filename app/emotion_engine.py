"""Deterministic market-emotion classifier.

No ML. Just transparent, ordered rules over the bounded aggregate signals
produced by `market_data`:

    risk_on_score   -1 (risk-off)  ..  +1 (risk-on)
    vol_shock_score  0 (calm)      ..   1 (volatility shock)
    dispersion       0 (aligned)   ..   1 (assets disagree / choppy)
    reversal_score  -1 (rev. down) ..  +1 (reversal up)

Each branch returns an emotion, a confidence (how strongly the deciding
signals clear their thresholds), and a human-readable reason. Rules are ordered
by intensity/urgency so the most acute regime wins.
"""
from __future__ import annotations

from typing import List, Tuple

from .models import EmotionResult, MarketSnapshot

# Every emotion the engine can emit, with a short descriptor used in summaries.
EMOTIONS = {
    "euphoric": "broad, clean risk-on rally",
    "happy": "constructive, mildly bullish tape",
    "calm": "quiet, range-bound, low volatility",
    "anxious": "choppy and indecisive with rising volatility",
    "fearful": "broad risk-off selloff with elevated volatility",
    "angry": "violent, fast selling",
    "surprised": "sharp intraday reversal",
    "chaotic": "extreme volatility with assets pulling apart",
    "depressed": "slow, low-volatility grind lower",
    "manic": "frenzied, high-volatility melt-up",
}


def _clamp(v: float, lo: float = 0.5, hi: float = 0.95) -> float:
    return max(lo, min(hi, v))


def _direction_word(score: float) -> str:
    if score > 0.1:
        return "higher"
    if score < -0.1:
        return "lower"
    return "flat"


def _movers(snapshot: MarketSnapshot) -> str:
    """Compact textual description of the notable movers."""
    bits: List[str] = []
    for sym in ("SPY", "QQQ", "BTC-USD", "ETH-USD"):
        a = snapshot.assets.get(sym)
        if a and a.change_pct is not None:
            label = {"SPY": "SPY", "QQQ": "QQQ", "BTC-USD": "BTC", "ETH-USD": "ETH"}[sym]
            bits.append(f"{label} {a.change_pct:+.2f}%")
    vix = snapshot.assets.get("^VIX")
    if vix and vix.change_pct is not None:
        bits.append(f"VIX {vix.change_pct:+.1f}%")
    return ", ".join(bits)


def classify(snapshot: MarketSnapshot) -> EmotionResult:
    ro = snapshot.risk_on_score
    vs = snapshot.vol_shock_score
    disp = snapshot.dispersion
    rev = snapshot.reversal_score

    scores = {
        "risk_on": ro,
        "vol_shock": vs,
        "dispersion": disp,
        "reversal": rev,
    }

    if not snapshot.any_data:
        return EmotionResult(
            emotion="calm",
            confidence=0.5,
            summary="No live market data available right now — assuming a quiet, calm tape.",
            scores=scores,
            reasons=["no usable market data"],
        )

    emotion, confidence, reason = _decide(ro, vs, disp, rev)

    movers = _movers(snapshot)
    descriptor = EMOTIONS[emotion]
    summary = f"Market reads {emotion} — {descriptor}. {reason}"
    if movers:
        summary += f" ({movers})."

    return EmotionResult(
        emotion=emotion,
        confidence=round(confidence, 2),
        summary=summary,
        scores=scores,
        reasons=[reason],
    )


def _decide(ro: float, vs: float, disp: float, rev: float) -> Tuple[str, float, str]:
    """Ordered decision rules. First match wins."""

    # 1. CHAOTIC — extreme volatility *and* assets pulling in different
    #    directions. The market has lost coherence.
    if vs >= 0.7 and disp >= 0.5:
        conf = _clamp(0.6 + (vs - 0.7) * 0.8 + (disp - 0.5) * 0.4)
        return "chaotic", conf, "Volatility is extreme and assets are pulling apart in different directions."

    # 2. SURPRISED — a strong intraday reversal dominates the narrative.
    if abs(rev) >= 0.6:
        conf = _clamp(0.55 + (abs(rev) - 0.6) * 1.0)
        way = "upward" if rev > 0 else "downward"
        return "surprised", conf, f"The market has staged a sharp {way} reversal intraday."

    # 3. ANGRY — violent, fast selling: risk-off plus a high-vol shock.
    if ro <= -0.35 and vs >= 0.65:
        conf = _clamp(0.6 + (-ro - 0.35) * 0.6 + (vs - 0.65) * 0.6)
        return "angry", conf, "Selling is violent and fast, with volatility spiking hard."

    # 4. FEARFUL — broad risk-off with clearly elevated volatility.
    if ro <= -0.4 and vs >= 0.45:
        conf = _clamp(0.58 + (-ro - 0.4) * 0.6 + (vs - 0.45) * 0.5)
        return "fearful", conf, "Risk assets are selling off while volatility climbs — classic risk-off."

    # 5. MANIC — strong rally but with elevated volatility (frenzied melt-up).
    if ro >= 0.55 and vs >= 0.55:
        conf = _clamp(0.6 + (ro - 0.55) * 0.6 + (vs - 0.55) * 0.4)
        return "manic", conf, "A powerful rally is running hot — strong gains paired with high volatility."

    # 6. EUPHORIC — strong, broad rally on calm volatility (clean risk-on).
    if ro >= 0.55 and vs < 0.45:
        conf = _clamp(0.62 + (ro - 0.55) * 0.7)
        return "euphoric", conf, "A broad, clean rally is underway with volatility well-behaved."

    # 7. ANXIOUS — elevated volatility without a clear direction
    #    (choppy/indecisive). Checked before HAPPY so a mildly-positive tape
    #    with elevated volatility reads as anxious, not constructive.
    if vs >= 0.4 and abs(ro) < 0.3:
        conf = _clamp(0.55 + (vs - 0.4) * 0.6 + disp * 0.3)
        return "anxious", conf, "Volatility is up but direction is unclear — a choppy, indecisive tape."

    # 8. HAPPY — constructive, mildly bullish tape.
    if ro >= 0.2 and vs < 0.55:
        conf = _clamp(0.55 + (ro - 0.2) * 0.7)
        return "happy", conf, "Risk assets are grinding higher in a constructive tape."

    # 9. DEPRESSED — slow, low-volatility grind lower.
    if ro <= -0.2 and vs < 0.45:
        conf = _clamp(0.55 + (-ro - 0.2) * 0.7)
        return "depressed", conf, "A slow, low-energy bleed lower with little volatility."

    # 10. CALM — default: quiet, range-bound, low volatility.
    conf = _clamp(0.55 + (0.3 - min(abs(ro), 0.3)) * 0.5 + (0.3 - min(vs, 0.3)) * 0.5)
    return "calm", conf, "The tape is quiet and range-bound with low volatility."
