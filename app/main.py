"""AI GrowthRadar backend — FastAPI application.

Local development:  uvicorn app.main:app --reload  (SQLite, demo provider)
Production: set DATABASE_URL (PostgreSQL), MARKET_PROVIDER and provider keys
in the environment. API keys live server-side only, never in the Android APK.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from . import models, portfolio_math as pm, scoring, services
from .models import SessionLocal, init_db
from .providers import DemoProvider, get_provider

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name) as f:
        return yaml.safe_load(f)


SCORING_CFG = _load_yaml("scoring.yaml")
SECTORS_CFG = _load_yaml("sectors.yaml")["sectors"]
ALERTS_CFG = _load_yaml("alerts.yaml")["alerts"]
APP_CFG = _load_yaml("app.yaml")["app"]

app = FastAPI(title="AI GrowthRadar", version=APP_CFG.get("version", "1.0.0"))
init_db()
provider = get_provider()


def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


def current_user(x_token: str | None = Header(default=None), session=Depends(db)):
    if not x_token:
        raise HTTPException(401, "Missing X-Token header")
    user = session.query(models.User).filter_by(token=x_token).first()
    if not user:
        raise HTTPException(401, "Invalid token")
    return user


def _score_for(ticker: str, session):
    sec = provider.get_security(ticker)
    if not sec:
        raise HTTPException(404, f"Unknown ticker {ticker}")
    metrics_rec = provider.get_metrics(ticker) or {}
    sector_growth = (SECTORS_CFG.get(sec["sector"]) or {}).get("growth_rate_pct")
    result = scoring.compute_score(metrics_rec, sector_growth, SCORING_CFG["weights"])
    session.add(models.GrowthScoreRecord(
        ticker=sec["ticker"], total=result.total, label=result.label,
        confidence=result.confidence,
        factors_json=json.dumps([{"key": f.key, "weight": f.weight, "score": f.score,
                                  "explanation": f.explanation} for f in result.factors]),
        data_status=sec.get("data_status", "DEMO")))
    session.commit()
    return sec, metrics_rec, result


@app.get("/health")
def health():
    return {"status": "ok", "app": APP_CFG["name"], "provider": provider.name,
            "disclaimer": APP_CFG["disclaimer"]}


class AuthIn(BaseModel):
    email: str
    password: str


@app.post("/auth/register")
def register(body: AuthIn, session=Depends(db)):
    if session.query(models.User).filter_by(email=body.email).first():
        raise HTTPException(400, "Email already registered")
    salt = secrets.token_hex(8)
    user = models.User(email=body.email,
                       password_hash=f"{salt}${_hash(body.password, salt)}",
                       token=secrets.token_hex(24))
    session.add(user)
    session.commit()
    return {"token": user.token}


@app.post("/auth/login")
def login(body: AuthIn, session=Depends(db)):
    user = session.query(models.User).filter_by(email=body.email).first()
    if not user:
        raise HTTPException(401, "Invalid credentials")
    salt, hashed = user.password_hash.split("$", 1)
    if _hash(body.password, salt) != hashed:
        raise HTTPException(401, "Invalid credentials")
    return {"token": user.token}


@app.get("/securities")
def securities(market: str | None = None, sector: str | None = None,
               max_price: float | None = None, currency: str | None = None):
    items = provider.list_securities()
    if market:
        items = [s for s in items if s["exchange"].upper() == market.upper()]
    if sector:
        items = [s for s in items if s["sector"] == sector]
    if currency:
        items = [s for s in items if s["currency"] == currency.upper()]
    if max_price is not None:
        items = [s for s in items if s["price"] <= max_price]
    return {"count": len(items), "securities": items}


@app.get("/securities/{ticker}")
def security_detail(ticker: str, session=Depends(db)):
    sec, metrics, result = _score_for(ticker, session)
    return {"security": sec, "metrics": metrics,
            "score": {"total": result.total, "label": result.label,
                      "confidence": result.confidence, "calculated_at": result.calculated_at,
                      "missing": result.missing,
                      "factors": [{"key": f.key, "weight": f.weight, "score": f.score,
                                   "explanation": f.explanation, "data": f.data}
                                  for f in result.factors]}}


@app.get("/discover")
def discover(max_price: float | None = None, sector: str | None = None,
             min_score: float = 0, session=Depends(db)):
    out = []
    for sec in provider.list_securities():
        if max_price is not None and sec["price"] > max_price:
            continue
        if sector and sec["sector"] != sector:
            continue
        _, _, result = _score_for(sec["ticker"], session)
        if result.total < min_score:
            continue
        out.append({"ticker": sec["ticker"], "name": sec["name"], "exchange": sec["exchange"],
                    "currency": sec["currency"], "sector": sec["sector"], "price": sec["price"],
                    "market_cap_m": sec["market_cap_m"], "score": result.total,
                    "label": result.label, "confidence": result.confidence,
                    "data_status": sec.get("data_status", "DEMO")})
    out.sort(key=lambda x: x["score"], reverse=True)
    return {"count": len(out), "opportunities": out}


_demo_provider = DemoProvider()
_snapshot_cache: dict = {"at": 0.0, "data": None}
_snapshot_building = {"active": False}


def _build_snapshot() -> dict:
    """Assemble the snapshot payload in parallel. The provider paces its
    own calls to stay inside the free tier's rate limit; profiles and
    metrics are cached provider-side, so steady-state rebuilds are mostly
    fresh quotes. Records with no usable price (provider cannot serve the
    ticker and no fixture price exists) are skipped rather than shown
    with an invented price. Sector identity always comes from the
    catalogue, so live records keep the app's sector taxonomy."""

    def one(base):
        ticker = base["ticker"]
        try:
            live = provider.get_security(ticker)
        except Exception:
            live = None  # one unservable ticker must not kill the snapshot
        sec = dict(live or base)
        sec["sector"] = base.get("sector")
        growth = (SECTORS_CFG.get(base.get("sector")) or {}).get("growth_rate_pct")
        sec["sector_growth_pct"] = growth
        if not sec.get("price"):
            return None
        try:
            metrics = provider.get_metrics(ticker) or _demo_provider.get_metrics(ticker) or {}
        except Exception:
            metrics = _demo_provider.get_metrics(ticker) or {}
        result = scoring.compute_score(metrics, growth, SCORING_CFG["weights"])
        return {"security": sec, "metrics": metrics,
                "score": {"total": result.total, "label": result.label,
                          "confidence": result.confidence, "missing": result.missing}}

    bases = _demo_provider.list_securities()
    with ThreadPoolExecutor(max_workers=6) as ex:
        items = [it for it in ex.map(one, bases) if it]
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "provider": provider.name, "count": len(items), "items": items}


def _refresh_snapshot_bg() -> None:
    if _snapshot_building["active"]:
        return
    _snapshot_building["active"] = True
    try:
        data = _build_snapshot()
        _snapshot_cache.update(at=time.time(), data=data)
    except Exception:
        pass  # keep serving the previous snapshot
    finally:
        _snapshot_building["active"] = False


@app.on_event("startup")
def _warm_snapshot() -> None:
    threading.Thread(target=_refresh_snapshot_bg, daemon=True).start()


@app.get("/snapshot")
def snapshot():
    """One payload for the Android app: every security in the universe with
    its metrics and GrowthRadar score. Live records carry data_status=LIVE;
    anything the active provider cannot serve (for example TSX names on the
    Finnhub free tier) falls back to the demo fixture and stays labelled
    DEMO per record. Served from a 5-minute cache; a stale cache is
    returned immediately while a rebuild runs in the background."""
    now = time.time()
    if _snapshot_cache["data"]:
        if now - _snapshot_cache["at"] >= 300:
            threading.Thread(target=_refresh_snapshot_bg, daemon=True).start()
        return _snapshot_cache["data"]
    data = _build_snapshot()
    _snapshot_cache.update(at=now, data=data)
    return data


@app.get("/securities/{ticker}/research")
def research(ticker: str, session=Depends(db)):
    sec, metrics, result = _score_for(ticker, session)
    report = services.research_report(sec, metrics, result)
    session.add(models.ResearchReport(ticker=sec["ticker"], report_json=json.dumps(report),
                                      data_status=sec.get("data_status", "DEMO")))
    session.commit()
    return report


@app.post("/securities/{ticker}/recommendation")
def make_recommendation(ticker: str, session=Depends(db), user=Depends(current_user)):
    sec, metrics, result = _score_for(ticker, session)
    rec = services.recommend(result.total, result.confidence, metrics)
    record = models.Recommendation(
        ticker=sec["ticker"], recommendation=rec["recommendation"],
        price_at_recommendation=sec["price"], score=result.total,
        confidence=result.confidence, entry_range_json=json.dumps(rec["entry_range"]),
        thesis="; ".join(rec["rationale"]), risks="; ".join(rec["risks"]))
    session.add(record)
    session.commit()
    return {"id": record.id, "ticker": sec["ticker"], **rec,
            "price_at_recommendation": sec["price"], "data_status": sec.get("data_status")}


@app.get("/recommendations")
def list_recommendations(session=Depends(db)):
    rows = session.query(models.Recommendation).order_by(models.Recommendation.id.desc()).all()
    out = []
    for r in rows:
        sec = provider.get_security(r.ticker)
        perf = None
        if sec and r.price_at_recommendation:
            perf = round(pm.recommendation_return(r.price_at_recommendation, sec["price"]), 2)
        out.append({"id": r.id, "ticker": r.ticker, "recommendation": r.recommendation,
                    "price_at_recommendation": r.price_at_recommendation,
                    "current_price_demo": sec["price"] if sec else None,
                    "return_since_pct": perf, "score": r.score, "confidence": r.confidence,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "evaluation_note": "30/90/180/365-day evaluations are computed against stored price history once a live provider is connected; periods not yet elapsed show as pending."})
    return {"recommendations": out}


class WatchIn(BaseModel):
    ticker: str


@app.post("/watchlist")
def watch_add(body: WatchIn, session=Depends(db), user=Depends(current_user)):
    if not provider.get_security(body.ticker):
        raise HTTPException(404, "Unknown ticker")
    item = models.WatchlistItem(user_id=user.id, ticker=body.ticker.upper())
    session.merge(item)
    session.commit()
    return {"watching": body.ticker.upper()}


@app.get("/watchlist")
def watch_list(session=Depends(db), user=Depends(current_user)):
    rows = session.query(models.WatchlistItem).filter_by(user_id=user.id).all()
    return {"watchlist": [r.ticker for r in rows]}


class PortfolioIn(BaseModel):
    name: str


@app.post("/portfolios")
def portfolio_create(body: PortfolioIn, session=Depends(db), user=Depends(current_user)):
    p = models.Portfolio(user_id=user.id, name=body.name)
    session.add(p)
    session.commit()
    return {"id": p.id, "name": p.name}


class TxIn(BaseModel):
    portfolio_id: int
    ticker: str
    side: str  # buy|sell|dividend
    qty: float
    price: float
    account_type: str = "non-registered"
    reason_for_selling: str | None = None
    thesis: str | None = None


@app.post("/transactions")
def add_transaction(body: TxIn, session=Depends(db), user=Depends(current_user)):
    p = session.query(models.Portfolio).filter_by(id=body.portfolio_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Portfolio not found")
    sec = provider.get_security(body.ticker)
    if not sec:
        raise HTTPException(404, "Unknown ticker")
    holding = (session.query(models.Holding)
               .filter_by(portfolio_id=p.id, ticker=sec["ticker"], account_type=body.account_type)
               .first())
    if not holding:
        holding = models.Holding(portfolio_id=p.id, ticker=sec["ticker"], exchange=sec["exchange"],
                                 currency=sec["currency"], account_type=body.account_type)
        session.add(holding)
        session.flush()
    pos = pm.Position(qty=holding.qty, avg_cost=holding.avg_cost, realized_pnl=holding.realized_pnl)
    sold_record = None
    if body.side == "buy":
        pos.buy(body.qty, body.price)
        if body.thesis:
            holding.thesis = body.thesis
    elif body.side == "sell":
        if body.qty > pos.qty + 1e-9:
            raise HTTPException(400, "Sell quantity exceeds holding")
        avg_before = pos.avg_cost
        pnl = pos.sell(body.qty, body.price)
        _, _, score_at_sale = _score_for(sec["ticker"], session)
        sold_record = models.SoldPosition(
            portfolio_id=p.id, ticker=sec["ticker"], purchase_price=avg_before,
            qty_sold=body.qty, sale_price=body.price, realized_pnl=round(pnl, 2),
            thesis=holding.thesis, reason_for_selling=body.reason_for_selling,
            score_at_sale=score_at_sale.total)
        session.add(sold_record)
    elif body.side == "dividend":
        pos.realized_pnl += body.qty * body.price
    else:
        raise HTTPException(400, "side must be buy, sell or dividend")
    holding.qty, holding.avg_cost, holding.realized_pnl = pos.qty, pos.avg_cost, pos.realized_pnl
    session.add(models.Transaction(portfolio_id=p.id, ticker=sec["ticker"], side=body.side,
                                   qty=body.qty, price=body.price, currency=sec["currency"],
                                   account_type=body.account_type))
    session.commit()
    return {"holding": {"ticker": holding.ticker, "qty": holding.qty,
                        "avg_cost": round(holding.avg_cost, 4),
                        "realized_pnl": round(holding.realized_pnl, 2)},
            "missed_opportunity_tracking": ({"sold_position_id": sold_record.id}
                                            if sold_record else None)}


@app.get("/portfolios/{portfolio_id}/summary")
def portfolio_summary(portfolio_id: int, session=Depends(db), user=Depends(current_user)):
    p = session.query(models.Portfolio).filter_by(id=portfolio_id, user_id=user.id).first()
    if not p:
        raise HTTPException(404, "Portfolio not found")
    holdings = session.query(models.Holding).filter_by(portfolio_id=p.id).all()
    total_value = total_cost = total_unreal = total_real = 0.0
    rows, alerts = [], []
    milestones = ALERTS_CFG.get("profit_milestones_pct", [25, 50, 75, 100])
    for h in holdings:
        sec = provider.get_security(h.ticker)
        price = sec["price"] if sec else None
        value = (price or 0) * h.qty
        cost = h.avg_cost * h.qty
        unreal = pm.unrealized_pnl(pm.Position(qty=h.qty, avg_cost=h.avg_cost), price) if price else None
        gain_pct = pm.pct_change(h.avg_cost, price) if price and h.avg_cost else None
        total_value += value
        total_cost += cost
        total_unreal += unreal or 0
        total_real += h.realized_pnl
        if gain_pct is not None:
            for mstone in pm.milestones_hit(gain_pct, milestones):
                alerts.append({"ticker": h.ticker, "category": "profit_milestone",
                               "message": f"{h.ticker} is up {gain_pct:.1f}% — the +{mstone:.0f}% milestone is a REVIEW trigger, not a sell instruction."})
        rows.append({"ticker": h.ticker, "account_type": h.account_type, "qty": h.qty,
                     "avg_cost": round(h.avg_cost, 4), "current_price": price,
                     "value": round(value, 2), "unrealized_pnl": round(unreal, 2) if unreal is not None else None,
                     "gain_pct": round(gain_pct, 2) if gain_pct is not None else None,
                     "allocation_pct": None, "data_status": sec.get("data_status") if sec else None})
    for r in rows:
        r["allocation_pct"] = round(r["value"] / total_value * 100, 2) if total_value else 0.0
    return {"portfolio": p.name, "total_value": round(total_value, 2),
            "total_cost": round(total_cost, 2), "unrealized_pnl": round(total_unreal, 2),
            "realized_pnl": round(total_real, 2), "holdings": rows, "review_alerts": alerts,
            "note": "Values use the active provider's prices; DEMO prices are fixtures, not live values."}


@app.get("/missed-opportunities")
def missed(session=Depends(db), user=Depends(current_user)):
    rows = session.query(models.SoldPosition).all()
    out = []
    for s in rows:
        sec = provider.get_security(s.ticker)
        stats = pm.post_sale_stats(s.sale_price, sec["price"], s.qty_sold) if sec else None
        _, _, now_score = _score_for(s.ticker, session)
        out.append({"ticker": s.ticker, "sale_price": s.sale_price,
                    "current_price": sec["price"] if sec else None,
                    "qty_sold": s.qty_sold, "realized_pnl": s.realized_pnl,
                    "reason_for_selling": s.reason_for_selling,
                    "score_at_sale": s.score_at_sale, "score_now": now_score.total,
                    "thesis_status": ("Original thesis may be strengthening — score improved since sale"
                                      if now_score.total > (s.score_at_sale or 0) + 5 else
                                      "No material score improvement since sale"),
                    **(stats or {}),
                    "data_status": sec.get("data_status") if sec else None})
    out.sort(key=lambda x: x.get("move_since_sale_pct") or 0, reverse=True)
    return {"missed_opportunities": out}


@app.get("/alerts")
def alerts(session=Depends(db), user=Depends(current_user)):
    rows = session.query(models.Alert).filter_by(user_id=user.id) \
        .order_by(models.Alert.id.desc()).limit(100).all()
    return {"alerts": [{"id": a.id, "ticker": a.ticker, "category": a.category,
                        "message": a.message,
                        "created_at": a.created_at.isoformat() if a.created_at else None}
                       for a in rows]}


@app.get("/news")
def news(ticker: str | None = None):
    return {"news": provider.get_news(ticker),
            "note": "Demo provider returns labelled fixtures only. Connect a licensed news source for real items."}


@app.get("/config")
def config():
    return {"scoring_weights": SCORING_CFG["weights"], "sectors": SECTORS_CFG,
            "alerts": ALERTS_CFG, "app": APP_CFG}
