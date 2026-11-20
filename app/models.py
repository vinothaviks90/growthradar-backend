"""Database models (SQLAlchemy). SQLite by default for local development;
PostgreSQL via DATABASE_URL in production. Covers the brief's core entities;
filings/institutional/insider tables are represented through metrics and
research records until a licensed provider supplies that data.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer, String,
                        Text, UniqueConstraint, create_engine)
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./growthradar.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}
                       if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


def _now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    token = Column(String, unique=True)
    created_at = Column(DateTime, default=_now)


class UserPreference(Base):
    __tablename__ = "user_preferences"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    key = Column(String, nullable=False)
    value = Column(Text)
    __table_args__ = (UniqueConstraint("user_id", "key"),)


class Portfolio(Base):
    __tablename__ = "portfolios"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String, nullable=False)
    created_at = Column(DateTime, default=_now)


class Holding(Base):
    """Current position, maintained from transactions (average cost)."""
    __tablename__ = "holdings"
    id = Column(Integer, primary_key=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"))
    ticker = Column(String, nullable=False)
    exchange = Column(String)
    currency = Column(String)
    account_type = Column(String, default="non-registered")  # tfsa|rrsp|non-registered
    qty = Column(Float, default=0.0)
    avg_cost = Column(Float, default=0.0)
    realized_pnl = Column(Float, default=0.0)
    thesis = Column(Text)
    __table_args__ = (UniqueConstraint("portfolio_id", "ticker", "account_type"),)


class Transaction(Base):
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"))
    ticker = Column(String, nullable=False)
    side = Column(String, nullable=False)  # buy|sell|dividend
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String)
    account_type = Column(String, default="non-registered")
    note = Column(Text)
    created_at = Column(DateTime, default=_now)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    ticker = Column(String, nullable=False)
    added_at = Column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("user_id", "ticker"),)


class GrowthScoreRecord(Base):
    __tablename__ = "growth_scores"
    id = Column(Integer, primary_key=True)
    ticker = Column(String, nullable=False, index=True)
    total = Column(Float)
    label = Column(String)
    confidence = Column(Float)
    factors_json = Column(Text)
    data_status = Column(String)  # DEMO or provider name
    calculated_at = Column(DateTime, default=_now)


class Recommendation(Base):
    """Permanent recommendation history — never rewritten after the fact."""
    __tablename__ = "investment_recommendations"
    id = Column(Integer, primary_key=True)
    ticker = Column(String, nullable=False, index=True)
    recommendation = Column(String, nullable=False)
    price_at_recommendation = Column(Float)
    score = Column(Float)
    confidence = Column(Float)
    entry_range_json = Column(Text)
    thesis = Column(Text)
    risks = Column(Text)
    created_at = Column(DateTime, default=_now)


class SoldPosition(Base):
    __tablename__ = "sold_positions"
    id = Column(Integer, primary_key=True)
    portfolio_id = Column(Integer, ForeignKey("portfolios.id"))
    ticker = Column(String, nullable=False)
    purchase_date = Column(DateTime)
    purchase_price = Column(Float)
    qty_sold = Column(Float)
    sale_date = Column(DateTime, default=_now)
    sale_price = Column(Float)
    realized_pnl = Column(Float)
    thesis = Column(Text)
    reason_for_selling = Column(Text)
    score_at_sale = Column(Float)
    recommendation_at_sale = Column(String)
    retention_months = Column(Integer, default=12)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    ticker = Column(String)
    category = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    dedupe_key = Column(String, index=True)
    created_at = Column(DateTime, default=_now)


class ResearchReport(Base):
    __tablename__ = "research_reports"
    id = Column(Integer, primary_key=True)
    ticker = Column(String, nullable=False, index=True)
    report_json = Column(Text)
    data_status = Column(String)
    created_at = Column(DateTime, default=_now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    actor = Column(String)
    action = Column(String)
    detail = Column(Text)
    created_at = Column(DateTime, default=_now)


def init_db():
    Base.metadata.create_all(engine)
