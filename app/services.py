"""Recommendation rules + research report assembly.

Recommendations are rule-based over verified (or clearly labelled demo)
inputs and the deterministic GrowthRadar score. They are never generated
from sentiment or a single indicator, and every record is stored permanently.
"""
from __future__ import annotations


def recommend(score_total: float, confidence: float, metrics: dict,
              concentration_pct: float | None = None) -> dict:
    """Returns recommendation label + rationale + invalidation conditions."""
    reasons, risks = [], []
    fcf_neg = metrics.get("fcf_positive") is False
    ps_hot = (metrics.get("ps_vs_sector_pct") or 0) > 100
    if fcf_neg:
        risks.append("Negative free cash flow")
    if ps_hot:
        risks.append("Valuation multiple far above sector median")
    if (metrics.get("debt_to_equity") or 0) > 2.0:
        risks.append("High debt relative to equity")
    if concentration_pct is not None and concentration_pct > 25:
        risks.append(f"Position is {concentration_pct:.0f}% of the portfolio (concentration)")

    if confidence < 0.5:
        label = "Watch closely"
        reasons.append("Too much data is missing to support a stronger call")
    elif score_total >= 75 and not ps_hot:
        label = "Buy candidate"
        reasons.append(f"GrowthRadar score {score_total} with confidence {confidence}")
    elif score_total >= 65:
        label = "Accumulate gradually"
        reasons.append(f"GrowthRadar score {score_total}; staged entry suits the risk profile")
    elif score_total >= 50:
        label = "Hold"
        reasons.append(f"GrowthRadar score {score_total}; thesis intact but not compelling")
    elif score_total >= 35:
        label = "Watch closely"
        reasons.append(f"GrowthRadar score {score_total}; fundamentals are mixed")
    else:
        label = "Reduce exposure"
        reasons.append(f"GrowthRadar score {score_total}; weak current investment profile")

    if ps_hot and label in ("Buy candidate", "Accumulate gradually"):
        label = "Watch closely"
        reasons.append("Valuation is too extended for an entry call despite the score")

    return {
        "recommendation": label,
        "rationale": reasons,
        "risks": risks,
        "invalidation": [
            "Revenue growth turns negative for two consecutive quarters",
            "Free cash flow deteriorates materially from the level at recommendation time",
            "A core product/contract assumption behind the thesis is lost",
        ],
        "entry_range": None,  # never invented; set only when a methodology supports it
        "entry_range_note": "No reliable entry range calculated — insufficient valuation history.",
    }


def research_report(security: dict, metrics: dict | None, score) -> dict:
    """Structured research output. Facts come from the provider record;
    interpretations are labelled as such. Unverifiable items say so."""
    m = metrics or {}
    known = lambda v: v if v is not None else "Not available"
    return {
        "ticker": security["ticker"],
        "data_status": security.get("data_status", "DEMO"),
        "business_overview": {
            "name": security["name"],
            "exchange": security["exchange"],
            "sector": security["sector"],
            "note": "Detailed business description requires a filings/IR source; not fabricated here.",
        },
        "financial_analysis": {
            "revenue_growth_pct": known(m.get("revenue_growth_pct")),
            "gross_margin_pct": known(m.get("gross_margin_pct")),
            "fcf_margin_pct": known(m.get("fcf_margin_pct")),
            "debt_to_equity": known(m.get("debt_to_equity")),
            "current_ratio": known(m.get("current_ratio")),
        },
        "growthradar_score": {
            "total": score.total, "label": score.label, "confidence": score.confidence,
            "calculated_at": score.calculated_at,
            "factors": [{"key": f.key, "weight": f.weight, "score": f.score,
                         "explanation": f.explanation, "data": f.data} for f in score.factors],
            "missing": score.missing,
        },
        "risk_analysis": [r for r in [
            "Cash burn: free cash flow is negative" if m.get("fcf_positive") is False else None,
            "Valuation: price/sales far above sector median" if (m.get("ps_vs_sector_pct") or 0) > 100 else None,
            "Data risk: key metrics unavailable" if score.missing else None,
        ] if r],
        "early_growth_signals": [s for s in [
            "Revenue is accelerating" if (m.get("revenue_acceleration_pct") or 0) > 3 else None,
            "Institutional ownership is rising" if (m.get("institutional_change_pct") or 0) > 2 else None,
            "Net insider buying reported" if m.get("insider_net_buying") else None,
            "Unusual volume trend" if (m.get("volume_trend_pct") or 0) > 20 else None,
        ] if s],
        "sources": [{"name": security.get("provider", "demo"),
                     "retrieved_at": security.get("retrieved_at"),
                     "source_timestamp": security.get("source_timestamp")}],
    }
