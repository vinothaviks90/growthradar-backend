"""Provider abstraction: providers can be swapped without touching the app.

Every record carries provider, source timestamp, retrieval timestamp and a
freshness status. Demo data is always labelled DEMO and must never be shown
as live market information.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from . import demo_data


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MarketDataProvider(ABC):
    name = "base"

    @abstractmethod
    def list_securities(self) -> list[dict]:
        ...

    @abstractmethod
    def get_security(self, ticker: str) -> dict | None:
        ...

    @abstractmethod
    def get_metrics(self, ticker: str) -> dict | None:
        ...

    @abstractmethod
    def get_news(self, ticker: str | None = None) -> list[dict]:
        ...


class DemoProvider(MarketDataProvider):
    """Clearly labelled demo fixtures used until a licensed provider is
    configured. Values are illustrative test data, NOT real market data."""

    name = "demo"

    def _wrap(self, rec: dict) -> dict:
        out = dict(rec)
        out.update({
            "provider": self.name,
            "data_status": "DEMO",
            "freshness": "demo-fixture",
            "source_timestamp": "demo-fixture",
            "retrieved_at": _now(),
        })
        return out

    def list_securities(self) -> list[dict]:
        return [self._wrap(s) for s in demo_data.SECURITIES]

    def get_security(self, ticker: str) -> dict | None:
        for s in demo_data.SECURITIES:
            if s["ticker"].upper() == ticker.upper():
                return self._wrap(s)
        return None

    def get_metrics(self, ticker: str) -> dict | None:
        m = demo_data.METRICS.get(ticker.upper())
        return self._wrap(m) if m else None

    def get_news(self, ticker: str | None = None) -> list[dict]:
        items = demo_data.NEWS
        if ticker:
            items = [n for n in items if n["ticker"].upper() == ticker.upper()]
        return [self._wrap(n) for n in items]


_PACE_LOCK = __import__("threading").Lock()
_PACE_LAST = [0.0]
_PACE_MIN_INTERVAL = 1.05  # Finnhub free tier: 60 calls/minute


def _finnhub_get(path: str, params: dict, api_key: str) -> dict | list:
    import json as _json
    import time as _time
    import urllib.parse
    import urllib.request

    with _PACE_LOCK:
        wait = _PACE_MIN_INTERVAL - (_time.monotonic() - _PACE_LAST[0])
        if wait > 0:
            _time.sleep(wait)
        _PACE_LAST[0] = _time.monotonic()

    url = "https://finnhub.io/api/v1" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-Finnhub-Token": api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return _json.loads(resp.read().decode("utf-8"))


class FinnhubProvider(MarketDataProvider):
    """Live Finnhub adapter (US exchanges on the free tier; TSX symbols
    return 403 there, so .TO tickers stay on demo until a TSX-capable
    provider is configured). Requires FINNHUB_API_KEY.

    Fields Finnhub does not supply are returned as None, never invented;
    the scoring engine lowers confidence for missing inputs.
    The transport is injectable (`fetch`) so tests run offline.
    """

    name = "finnhub"

    # US-listed universe served live: every non-TSX entry in the demo
    # catalogue. TSX names are excluded on purpose (free-tier 403).
    UNIVERSE = [s["ticker"] for s in demo_data.SECURITIES
                if not s["ticker"].endswith((".TO", ".V"))]

    def __init__(self, fetch=None):
        self.api_key = os.environ.get("FINNHUB_API_KEY", "")
        if not self.api_key and fetch is None:
            raise RuntimeError("FINNHUB_API_KEY not set")
        self._fetch = fetch or _finnhub_get
        # Profiles barely change (24 h cache); TTM metrics are quarterly
        # (30 min cache). Quotes are always fetched fresh. These caches
        # keep a 60-name snapshot inside the free tier's 60 calls/minute.
        self._profile_cache: dict[str, tuple[float, dict]] = {}
        self._metric_cache: dict[str, tuple[float, dict]] = {}

    def _get(self, path, **params):
        return self._fetch(path, params, self.api_key)

    def _wrap(self, rec: dict, source_ts: str) -> dict:
        out = dict(rec)
        out.update({
            "provider": self.name,
            "data_status": "LIVE",
            "freshness": "live",
            "source_timestamp": source_ts,
            "retrieved_at": _now(),
        })
        return out

    def get_security(self, ticker: str) -> dict | None:
        t = ticker.upper()
        if t.endswith(".TO") or t.endswith(".V"):
            return None  # not covered on the Finnhub free tier
        import time as _time
        cached = self._profile_cache.get(t)
        if cached and _time.monotonic() - cached[0] < 86_400:
            profile = cached[1]
        else:
            profile = self._get("/stock/profile2", symbol=t)
            if profile:
                self._profile_cache[t] = (_time.monotonic(), profile)
        if not profile or not profile.get("name"):
            return None
        quote = self._get("/quote", symbol=t)
        ts = _now()
        if quote and quote.get("t"):
            ts = datetime.fromtimestamp(quote["t"], timezone.utc).isoformat()
        rec = {
            "ticker": t,
            "name": profile.get("name"),
            "exchange": profile.get("exchange"),
            "currency": profile.get("currency") or "USD",
            # Sector identity comes from our catalogue when the ticker is
            # catalogued, so API sector filters and sector-growth scoring
            # use the app taxonomy — not Finnhub's free-text industry,
            # which silently broke both.
            "sector": next((s["sector"] for s in demo_data.SECURITIES
                            if s["ticker"] == t), profile.get("finnhubIndustry")),
            "price": quote.get("c") if quote else None,
            "prev_close": quote.get("pc") if quote else None,
            "day_change_pct": quote.get("dp") if quote else None,
            "market_cap_m": profile.get("marketCapitalization"),
            "shares_outstanding_m": profile.get("shareOutstanding"),
        }
        return self._wrap(rec, ts)

    def list_securities(self) -> list[dict]:
        out = []
        for t in self.UNIVERSE:
            sec = self.get_security(t)
            if sec:
                out.append(sec)
        return out

    def get_metrics(self, ticker: str) -> dict | None:
        t = ticker.upper()
        if t.endswith(".TO") or t.endswith(".V"):
            return None
        import time as _time
        cached = self._metric_cache.get(t)
        if cached and _time.monotonic() - cached[0] < 1_800:
            payload = cached[1]
        else:
            payload = self._get("/stock/metric", symbol=t, metric="all")
            if payload:
                self._metric_cache[t] = (_time.monotonic(), payload)
        m = (payload or {}).get("metric") or {}
        if not m:
            return None

        def g(key):
            v = m.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        vol10, vol3m = g("10DayAverageTradingVolume"), g("3MonthAverageTradingVolume")
        volume_trend = None
        if vol10 is not None and vol3m:
            volume_trend = (vol10 / vol3m - 1.0) * 100.0
        pfcf = g("pfcfShareTTM")
        rec = {
            "revenue_growth_pct": g("revenueGrowthTTMYoy"),
            "revenue_acceleration_pct": None,
            "eps_growth_pct": g("epsGrowthTTMYoy"),
            "gross_margin_pct": g("grossMarginTTM"),
            "operating_margin_trend_pts": None,
            "fcf_margin_pct": None,
            "debt_to_equity": g("totalDebt/totalEquityQuarterly") or g("totalDebt/totalEquityAnnual"),
            "current_ratio": g("currentRatioQuarterly") or g("currentRatioAnnual"),
            "fcf_positive": (pfcf > 0) if pfcf is not None else None,
            "cash_to_debt": None,
            "pe_vs_sector_pct": None,
            "peg": g("pegTTM"),
            "ps_vs_sector_pct": None,
            "institutional_ownership_pct": None,
            "institutional_change_pct": None,
            "insider_net_buying": None,
            # Genuine relative strength vs the S&P 500, 26-week, from Finnhub.
            "relative_strength_6m_pct": g("priceRelativeToS&P50026Week"),
            "volume_trend_pct": volume_trend,
            "catalysts": [],
            # Extra live fields for detail screens and early-growth signals.
            "beta": g("beta"),
            "week52_high": g("52WeekHigh"),
            "week52_low": g("52WeekLow"),
            "pe_ttm": g("peTTM"),
            "ps_ttm": g("psTTM"),
            "roe_ttm_pct": g("roeTTM"),
            "operating_margin_pct": g("operatingMarginTTM"),
            "net_margin_pct": g("netProfitMarginTTM"),
        }
        return self._wrap(rec, _now())

    def get_news(self, ticker: str | None = None) -> list[dict]:
        if not ticker:
            return []
        t = ticker.upper()
        if t.endswith(".TO") or t.endswith(".V"):
            return []
        from datetime import timedelta

        today = datetime.now(timezone.utc).date()
        items = self._get("/company-news", symbol=t,
                          **{"from": (today - timedelta(days=30)).isoformat(),
                             "to": today.isoformat()})
        out = []
        for n in items or []:
            ts = n.get("datetime")
            published = (datetime.fromtimestamp(ts, timezone.utc).isoformat()
                         if isinstance(ts, (int, float)) else "")
            rec = {
                "ticker": t,
                "headline": n.get("headline", ""),
                "publisher": n.get("source", ""),
                "published_at": published,
                "url": n.get("url", ""),
                "verified": False,
                "sentiment": "neutral",
                "impact": n.get("summary", "")[:280],
            }
            out.append(self._wrap(rec, published or _now()))
        return out


def get_provider(name: str | None = None) -> MarketDataProvider:
    name = (name or os.environ.get("MARKET_PROVIDER", "demo")).lower()
    if name == "demo":
        return DemoProvider()
    if name == "finnhub":
        return FinnhubProvider()
    raise ValueError(f"Unknown provider: {name}")
