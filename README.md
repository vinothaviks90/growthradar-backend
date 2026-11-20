# AI GrowthRadar — Backend

FastAPI + SQLAlchemy backend for the AI GrowthRadar Android app. Modular
monolith (no microservices at this stage): auth, preferences, market data
ingestion, fundamentals, discovery, deterministic growth scoring, research,
recommendations, portfolio, Missed Opportunity Tracker, alerts and
recommendation-performance tracking.

## Run locally (no paid infrastructure)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload   # http://127.0.0.1:8000, docs at /docs
.venv/bin/python -m pytest tests/ -q      # 19 tests
```

SQLite is used by default (`growthradar.db`). For production set
`DATABASE_URL=postgresql://...` (install a PostgreSQL driver such as
`psycopg[binary]`), run behind HTTPS, and set a real `SECRET_KEY`.

## Configuration

All operational settings live in `config/*.yaml` and are validated/loaded at
startup: `providers.yaml`, `ai_models.yaml`, `news_sources.yaml`,
`scoring.yaml`, `monitoring.yaml`, `alerts.yaml`, `sectors.yaml`, `app.yaml`.
Secrets come from the environment only — see `.env.example`. Never commit
real keys; never ship provider keys in the Android APK.

## Data providers

Provider-independent ingestion (`app/providers.py`). The default provider is
**demo**: clearly labelled fixtures (`data_status: "DEMO"`) used for
development and tests — never present them as live data.

Before enabling a paid/free provider, verify its current pricing, free-tier
limits, US + TSX/TSXV coverage, fundamentals/insider/institutional coverage,
real-time vs delayed pricing, and commercial redistribution licensing:

| Provider | Notes (verify before use) |
|---|---|
| Finnhub | Free tier exists; US-focused, TSX coverage limited |
| Financial Modeling Prep | TSX data on paid tiers |
| Twelve Data | TSX on paid tiers |
| Alpha Vantage | Free tier ~25 requests/day |
| SEC EDGAR | Free, official US filings (declare a User-Agent) |
| SEDAR+ | Official Canadian filings; no simple public API |

Every record carries provider, source timestamp, retrieval timestamp and
freshness. Missing data is returned as unavailable — never zero-filled, never
invented. Material disagreement between two providers should be flagged via
the data-quality path before display.

## Scoring

`app/scoring.py` is the deterministic GrowthRadar engine (weights in
`config/scoring.yaml`). The LLM never invents or alters scores. Missing
factors reduce confidence instead of silently scoring zero.

## Monitoring & alerts

Target interval 30 minutes, run by a backend scheduler (add APScheduler or a
systemd timer/cron invoking a worker), with push via Firebase Cloud
Messaging. Android WorkManager is best-effort only and is not relied on for
the 30-minute cadence. Alerts are deduplicated (24h window by default).

## Compliance notes

Research and decision-support only — not financial advice, no trade
execution, no brokerage credentials. Before a public commercial launch,
obtain qualified Canadian/US legal and compliance review (advice licensing,
privacy/PIPEDA, data licensing).
