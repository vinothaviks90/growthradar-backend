"""Deterministic GrowthRadar scoring engine.

Rules (from the product brief):
- The score is computed by this code only. No LLM invents or alters numbers.
- Every factor shows the data used and an explanation.
- Missing data never becomes zero silently: the factor is marked unavailable,
  its weight is redistributed across available factors, and confidence drops.
- Momentum is deliberately low-weight so a price run-up alone cannot produce
  a high score.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_WEIGHTS = {
    "revenue_earnings": 20,
    "business_quality": 15,
    "industry_growth": 15,
    "financial_health": 15,
    "valuation": 15,
    "institutional": 10,
    "momentum": 5,
    "catalysts": 5,
}

BANDS = [(85, "Exceptional research candidate"),
         (75, "Strong research candidate"),
         (65, "Promising, requires further evaluation"),
         (50, "Moderate or uncertain opportunity"),
         (0, "Weak current investment profile")]


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _scale(value: float, low: float, high: float) -> float:
    """Linear 0..100 scale of value between low (->0) and high (->100)."""
    if high == low:
        return 50.0
    return _clamp((value - low) / (high - low) * 100.0)


@dataclass
class FactorResult:
    key: str
    weight: int
    score: float | None          # None = unavailable
    explanation: str
    data: dict = field(default_factory=dict)


@dataclass
class ScoreResult:
    total: float
    label: str
    confidence: float            # 0..1 share of weight backed by data
    factors: list[FactorResult]
    calculated_at: str
    missing: list[str]


def _f_revenue_earnings(m: dict) -> FactorResult:
    parts, notes = [], []
    if m.get("revenue_growth_pct") is not None:
        parts.append(_scale(m["revenue_growth_pct"], -10, 50))
        notes.append(f"Revenue growth {m['revenue_growth_pct']}% YoY")
    if m.get("revenue_acceleration_pct") is not None:
        parts.append(_scale(m["revenue_acceleration_pct"], -15, 25))
        notes.append(f"Acceleration {m['revenue_acceleration_pct']} pts vs prior period")
    if m.get("eps_growth_pct") is not None:
        parts.append(_scale(m["eps_growth_pct"], -20, 60))
        notes.append(f"EPS growth {m['eps_growth_pct']}%")
    if not parts:
        return FactorResult("revenue_earnings", 0, None, "Revenue/EPS data not available", {})
    return FactorResult("revenue_earnings", 0, sum(parts) / len(parts),
                        "; ".join(notes), {k: m.get(k) for k in
                        ("revenue_growth_pct", "revenue_acceleration_pct", "eps_growth_pct")})


def _f_business_quality(m: dict) -> FactorResult:
    parts, notes = [], []
    if m.get("gross_margin_pct") is not None:
        parts.append(_scale(m["gross_margin_pct"], 5, 75))
        notes.append(f"Gross margin {m['gross_margin_pct']}%")
    if m.get("operating_margin_trend_pts") is not None:
        parts.append(_scale(m["operating_margin_trend_pts"], -5, 8))
        notes.append(f"Operating margin trend {m['operating_margin_trend_pts']} pts")
    if m.get("fcf_margin_pct") is not None:
        parts.append(_scale(m["fcf_margin_pct"], -20, 30))
        notes.append(f"FCF margin {m['fcf_margin_pct']}%")
    if not parts:
        return FactorResult("business_quality", 0, None, "Margin data not available", {})
    return FactorResult("business_quality", 0, sum(parts) / len(parts), "; ".join(notes),
                        {k: m.get(k) for k in ("gross_margin_pct", "operating_margin_trend_pts", "fcf_margin_pct")})


def _f_industry(m: dict, sector_growth: float | None) -> FactorResult:
    if sector_growth is None:
        return FactorResult("industry_growth", 0, None, "Sector growth rate not configured", {})
    return FactorResult("industry_growth", 0, _scale(sector_growth, 0, 25),
                        f"Sector growth rate {sector_growth}% (configured sector data)",
                        {"sector_growth_pct": sector_growth})


def _f_financial_health(m: dict) -> FactorResult:
    parts, notes = [], []
    if m.get("debt_to_equity") is not None:
        parts.append(_scale(-m["debt_to_equity"], -3.0, 0.0))  # lower debt scores higher
        notes.append(f"Debt/equity {m['debt_to_equity']}")
    if m.get("current_ratio") is not None:
        parts.append(_scale(m["current_ratio"], 0.5, 2.5))
        notes.append(f"Current ratio {m['current_ratio']}")
    if m.get("fcf_positive") is not None:
        parts.append(85.0 if m["fcf_positive"] else 25.0)
        notes.append("Positive free cash flow" if m["fcf_positive"] else "Negative free cash flow")
    if m.get("cash_to_debt") is not None:
        parts.append(_scale(m["cash_to_debt"], 0.0, 1.5))
        notes.append(f"Cash/debt {m['cash_to_debt']}")
    if not parts:
        return FactorResult("financial_health", 0, None, "Balance-sheet data not available", {})
    return FactorResult("financial_health", 0, sum(parts) / len(parts), "; ".join(notes),
                        {k: m.get(k) for k in ("debt_to_equity", "current_ratio", "fcf_positive", "cash_to_debt")})


def _f_valuation(m: dict) -> FactorResult:
    parts, notes = [], []
    # Valuation compares multiples to sector medians. A low share price alone
    # is never treated as cheap (brief rule 13).
    if m.get("pe_vs_sector_pct") is not None:
        parts.append(_scale(-m["pe_vs_sector_pct"], -60, 40))  # below sector median scores higher
        notes.append(f"P/E {m['pe_vs_sector_pct']}% vs sector median")
    if m.get("peg") is not None and m["peg"] > 0:
        parts.append(_scale(-m["peg"], -3.0, -0.5))
        notes.append(f"PEG {m['peg']}")
    if m.get("ps_vs_sector_pct") is not None:
        parts.append(_scale(-m["ps_vs_sector_pct"], -80, 40))
        notes.append(f"P/S {m['ps_vs_sector_pct']}% vs sector median")
    if not parts:
        return FactorResult("valuation", 0, None, "Valuation multiples not available", {})
    return FactorResult("valuation", 0, sum(parts) / len(parts), "; ".join(notes),
                        {k: m.get(k) for k in ("pe_vs_sector_pct", "peg", "ps_vs_sector_pct")})


def _f_institutional(m: dict) -> FactorResult:
    parts, notes = [], []
    if m.get("institutional_ownership_pct") is not None:
        parts.append(_scale(m["institutional_ownership_pct"], 5, 80))
        notes.append(f"Institutional ownership {m['institutional_ownership_pct']}%")
    if m.get("institutional_change_pct") is not None:
        parts.append(_scale(m["institutional_change_pct"], -10, 10))
        notes.append(f"Ownership change {m['institutional_change_pct']} pts")
    if m.get("insider_net_buying") is not None:
        parts.append(80.0 if m["insider_net_buying"] else 45.0)
        notes.append("Net insider buying reported" if m["insider_net_buying"] else "No net insider buying reported")
    if not parts:
        return FactorResult("institutional", 0, None, "Institutional/insider data not available", {})
    return FactorResult("institutional", 0, sum(parts) / len(parts), "; ".join(notes),
                        {k: m.get(k) for k in ("institutional_ownership_pct", "institutional_change_pct", "insider_net_buying")})


def _f_momentum(m: dict) -> FactorResult:
    parts, notes = [], []
    if m.get("relative_strength_6m_pct") is not None:
        parts.append(_scale(m["relative_strength_6m_pct"], -30, 40))
        notes.append(f"6M relative strength {m['relative_strength_6m_pct']}% vs benchmark")
    if m.get("volume_trend_pct") is not None:
        parts.append(_scale(m["volume_trend_pct"], -20, 60))
        notes.append(f"Volume trend {m['volume_trend_pct']}%")
    if not parts:
        return FactorResult("momentum", 0, None, "Price/volume history not available", {})
    return FactorResult("momentum", 0, sum(parts) / len(parts), "; ".join(notes),
                        {k: m.get(k) for k in ("relative_strength_6m_pct", "volume_trend_pct")})


def _f_catalysts(m: dict) -> FactorResult:
    cats = m.get("catalysts") or []
    verified = [c for c in cats if c.get("verified")]
    if not cats:
        return FactorResult("catalysts", 0, None, "No catalysts on record", {})
    score = _clamp(40 + 20 * len(verified) + 5 * (len(cats) - len(verified)))
    return FactorResult("catalysts", 0, score,
                        f"{len(verified)} verified catalyst(s), {len(cats) - len(verified)} unverified",
                        {"catalyst_count": len(cats), "verified_count": len(verified)})


def compute_score(metrics: dict, sector_growth: float | None,
                  weights: dict | None = None) -> ScoreResult:
    weights = dict(weights or DEFAULT_WEIGHTS)
    builders = {
        "revenue_earnings": lambda: _f_revenue_earnings(metrics),
        "business_quality": lambda: _f_business_quality(metrics),
        "industry_growth": lambda: _f_industry(metrics, sector_growth),
        "financial_health": lambda: _f_financial_health(metrics),
        "valuation": lambda: _f_valuation(metrics),
        "institutional": lambda: _f_institutional(metrics),
        "momentum": lambda: _f_momentum(metrics),
        "catalysts": lambda: _f_catalysts(metrics),
    }
    factors: list[FactorResult] = []
    total_weight = sum(weights.values())
    avail_weight = 0
    weighted = 0.0
    missing: list[str] = []
    for key, build in builders.items():
        fr = build()
        fr.weight = weights.get(key, 0)
        factors.append(fr)
        if fr.score is None:
            missing.append(key)
        else:
            avail_weight += fr.weight
            weighted += fr.score * fr.weight
    if avail_weight == 0:
        total = 0.0
    else:
        # Redistribute unavailable weight across available factors.
        total = weighted / avail_weight
    label = next(lbl for cut, lbl in BANDS if total >= cut)
    return ScoreResult(
        total=round(total, 1),
        label=label,
        confidence=round(avail_weight / total_weight, 2) if total_weight else 0.0,
        factors=factors,
        calculated_at=datetime.now(timezone.utc).isoformat(),
        missing=missing,
    )
