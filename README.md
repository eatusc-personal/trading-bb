# Trading Dashboard (OpenBB-backed)

A local, full-stack market-research dashboard: real-time quotes and technicals,
a **peer-divergence scanner** (leave-one-out z-scores that flag stocks *not*
moving with their group), a **Reddit/Stocktwits social-sentiment signal-lab**
with tiered scoring and event studies, screeners, ETF holdings, and consolidated
news — one Next.js app over a Python FastAPI backend with multi-provider market
data, per-provider budget guards, and stale-while-revalidate caching.

**Stack:** Next.js 16 (App Router, TypeScript, Tailwind) · Python FastAPI ·
OpenBB + provider routing (TwelveData / FMP / Marketstack) · SQLite.

## Screenshots

| Market Overview | Divergence Scanner |
| --- | --- |
| ![Market overview](docs/media/overview.png) | ![Divergence scanner](docs/media/divergence.png) |

> Add screenshots to `docs/media/` before publishing (run `npm run dev`, then capture the Market and Divergence pages).

## Quickstart

```bash
git clone https://github.com/eatusc/trading-bb && cd trading-bb
python3 -m venv .venv && .venv/bin/pip install -r python_api/requirements.txt
cp .env.example .env       # add at least one market-data provider key
npm install
npm run dev                # Next on :3000, Python API on :8001
```

Built on:
- Next.js 16 (App Router, TypeScript, Tailwind)
- Python FastAPI layer over an OpenBB install (in `.venv`)
- One-command development startup with `npm run dev`

## Engineering highlights

- **Peer-divergence scanner** — flags stocks that are *not* moving with their
  group using a robust **leave-one-out z-score** (median/MAD) over 1D/5D/20D
  windows, gated by a 60-day **correlation and beta** check vs an equal-weight
  peer index so habitual independent movers don't false-trigger.
  (`python_api/divergence.py`)
- **Resilient market-data layer** — a multi-provider router
  (TwelveData → FMP → Marketstack → yfinance) with automatic **failover**,
  per-provider **budget guards** (per-minute/day, SQLite-persisted), and
  cooldown/backoff on quota or auth failures.
- **Stale-while-revalidate everywhere** — slow endpoints answer instantly from
  the last completed result and refresh in a **single-flight** background
  thread; results persist in SQLite so restarts never cold-start (warm
  responses 20–200ms).
- **Social-sentiment signal-lab** — multi-source ingestion (Reddit, Stocktwits,
  Apewisdom) with ticker extraction, tiered candidate scoring, tradability /
  liquidity gates, and horizon-based **event-study** backtesting of signals.
- **Confluence-ranked daily report** — cross-tab synthesis (movers, technical
  flags, social buzz, peer divergence, headlines, ETFs) ranked by how many
  independent signals agree.
- **Hardening** — symbol regex + request size caps, per-IP rate limiting,
  optional API-key auth, and localhost-scoped CORS.

## What was built

### Frontend pages
- `/` Market Overview
  - Top gainers, top losers, most active
  - Summary cards
  - Search filter
  - Manual refresh
  - Last updated timestamp
  - Alerts/flags panel
  - After-hours movers
  - Industry Divergence chips (click → divergence scan of that peer group)
- `/charts` Chart Board
  - Opens in dedicated popup window from ticker clicks
  - Up to 9 charts
  - Candlestick mini-charts with volume lane
  - Last close + visible-range % change per card, link to ticker detail
  - Mouse wheel zoom + drag pan interactions
- `/news`
  - Consolidated headlines from notable free sources
  - Relative age, source filter chips, $CASHTAG links to ticker detail
- `/ticker/[symbol]` Ticker Detail
  - Header + key quote stats
  - Historical price chart
  - Volume chart
  - Range selection: 1M, 3M, 6M, 1Y, 5Y
  - Price table preview
  - CSV download
- `/technical`
  - RSI(14), SMA20/50/200, MACD
  - Recently searched ticker section
  - Deep-linkable: `/technical?symbol=XYZ`
  - Technical flags:
    - RSI overbought/oversold
    - Above 50 SMA
    - Above 200 SMA
    - Bullish/bearish 50 SMA crossover
    - MACD bull/bear cross, unusual volume (RVOL)
  - Calculation notes section
- `/screener`
  - Filters:
    - ticker contains
    - min/max price
    - min volume
    - min/max % change
    - RSI min/max
    - above 20/50/200 SMA yes/no/any
  - Server-side filtering and sortable result table
  - Preset buttons (incl. real semiconductor peer list)
  - "Screen My Watchlist" — run filters against your watchlist symbols
  - Custom universe via `POST /api/screener` `symbols` field (≤100 symbols)
  - Table zoom controls
- `/etfs` Popular ETFs
  - Top ETFs by volume with industry/category and largest holdings
  - Holdings modal: clickable holdings, top performers/losers, one-click Divergence Scan of the top-20 holdings vs the ETF
- `/social`
  - **Never blocks**: the endpoint serves the last completed scan instantly (stale-while-revalidate); the 30-60s
    Reddit/Stocktwits scan runs in a single-flight background thread. Data-age badge + auto-polling while refreshing;
    "Refresh Now" forces a background rescan. Last scan survives restarts (restored from SQLite).
    Tune with `SOCIAL_RESULT_REFRESH_AFTER_SEC` (default 600 = auto-rescan when data older than 10 min on access).
  - Compact view by default (toggle Detailed for all columns) + tier filter (All / A+B / A only)
  - Reddit-first social sentiment scanner
  - Cross-source enrichment with Stocktwits
  - Tiered candidate quality (`A/B/C`), mention-quality score, weighted source confirmation
  - Tradability overlays: liquidity/spread/volatility gates, market-cap bucket, market regime
  - Catalyst/options overlays: catalyst tags and options context score
  - Candidate confidence, validation status, explainability fields, and ranking outputs
- `/watchlist`
  - Add/remove symbols (multi-add: "AAPL, MSFT NVDA"; validated)
  - Local persistence via `localStorage`
  - Live quote refresh
  - Click-through to ticker detail
  - One-click Divergence Scan of the whole watchlist (3+ symbols)
- `/divergence` Divergence Scanner
  - **Group Pulse**: one click shows every group's median gain/loss over 1D / 1W / 1M plus benchmark and best/worst member — click any group to drill into a full scan
  - 35 editable peer groups in categories: Industries, Crypto (miners, treasury, exchanges, DC hardware), Defense & Drones (tactical UAS, counter-UAS, naval, defense electronics), Semis & Photonics (photonics, silicon photonics, GaN/SiC, RF, semicap, packaging), My Buckets
  - Groups are stored in `python_api/peer_groups.json` — edit via the ✎ button / "+ New Group" on the page (or the file directly): name, category, benchmark, members, notes
  - Scans peer groups and custom stock/ETF sets; flags stocks NOT moving with their peers (robust leave-one-out z-score over 1D/5D/20D windows)
  - 60-day correlation/beta vs equal-weight peer index filters out habitual independent movers
  - Plain-English labels: "Held up while group fell", "Lagged group rally", etc.
  - Deep links: `/divergence?groups=semiconductors` or `?symbols=AAPL,MSFT,...&benchmark=QQQ` (auto-scans)
  - Backend: `python_api/divergence.py` — `GET/POST /api/divergence/groups`, `PUT/DELETE /api/divergence/groups/{id}`, `GET /api/divergence/overview`, `POST /api/divergence/scan`

### Python API
Path: `python_api/main.py`

Endpoints:
- `GET /api/health`
- `GET /api/security/status`
- `GET /api/market/overview`
- `GET /api/market/after-hours`
- `GET /api/news/headlines`
- `GET /api/ticker/{symbol}/quote`
- `GET /api/ticker/{symbol}/historical?range=1Y`
- `GET /api/ticker/{symbol}/technicals?range=1Y`
- `POST /api/screener`
- `GET /api/social/sentiment`
- `GET /api/social/signal-lab/runs`
- `GET /api/social/signal-lab/run/{run_id}`
- `GET /api/social/signal-lab/event-study/{run_id}`
- `GET /api/watchlist/quote?symbols=AAPL,MSFT`
- `GET /api/alerts`

Includes:
- Symbol validation
- Error handling with readable messages
- Graceful empty responses
- Light in-memory TTL caching
- Technical indicator calculations in pandas
- Social baseline persistence in SQLite (`python_api/social_baseline.db`)
- Source-health diagnostics in social sentiment responses

## Local Architecture
- Next frontend runs on `http://localhost:3000`
- Python API runs on `http://127.0.0.1:8001`
- Next API routes in `app/api/*` proxy requests to Python API
- Browser calls only Next routes (`/api/...`), so it feels like one app

## Scripts
From `the repo root`:

- `npm run dev`
  - Starts both:
    - Next dev server
    - `./.venv/bin/uvicorn python_api.main:app --reload --host 127.0.0.1 --port 8001`
- `npm run build`
- `npm run lint`
- `npm run typecheck`
- `npm run metrics:phase3-eval`
  - Runs Phase 3 holdout benchmark comparison using `docs/program/PHASE3_BENCHMARK.json`
- `npm run metrics:phase5-event-study`
  - Runs horizon-based event-study metrics for the latest persisted signal run
- `npm run metrics:phase6-security`
  - Runs Phase 6 security checks (status/auth/rate-limit); pass `--api-key` when auth is enabled

## Install / Run

```bash
cd trading-bb
npm install
npm run dev
```

Open:
- `http://localhost:3000`

## Notes / limitations (OpenBB 4.7.1 + provider router)

OpenBB 4.7.1 routes used: `obb.equity.price.historical`, `obb.equity.price.quote`,
`obb.equity.discovery.{gainers,losers,active}` (the deprecated
`obb.equity.historical` is not used).

- A single universal screener endpoint with all requested filters is not exposed directly by OpenBB 4.7.1.
- Screener is implemented by building a practical universe, then calculating technicals locally and filtering server-side.
- Candlestick charts are implemented via custom SVG components.
- Market data calls use provider routing with fallback and cooldown (`twelvedata`, `fmp`, `marketstack` by default; optional `yfinance` opt-in).

## Social Ingestion Environment (Phase 1)
Optional env vars for public Reddit ingestion pacing:

```bash
export REDDIT_USER_AGENT="openbb-local-dashboard/1.0 (by /u/your_reddit_user)"
export REDDIT_MIN_INTERVAL_SEC="0.8"
export REDDIT_PUBLIC_LIMIT="25"
export REDDIT_FETCH_RETRIES="1"
export REDDIT_BACKOFF_BASE_SEC="0.4"
export REDDIT_FETCH_TIMEOUT_SEC="2"
export STOCKTWITS_FETCH_TIMEOUT_SEC="4"
export STOCKTWITS_TRENDING_TIMEOUT_SEC="4"
export APEWISDOM_ENABLED="1"
export APEWISDOM_TIMEOUT_SEC="4"
export APEWISDOM_MAX_RESULTS="80"
export SOCIAL_SKIP_REDDIT_IF_FALLBACK_READY="1"
export SOCIAL_SCAN_TIME_BUDGET_SEC="30"
export SOCIAL_MAX_STOCKTWITS_SYMBOLS="8"
export SOCIAL_QUOTE_VALIDATION_ENABLED="0"
export QUOTE_PROVIDER_AUTH_COOLDOWN_SEC="900"
```

## Performance Notes
- **Stale-while-revalidate everywhere**: slow read endpoints (market overview, alerts, after-hours, indicators, news, social) answer instantly from their last completed result and refresh in a single-flight background thread when stale. Results persist in SQLite (`swr_cache`), so restarts never cold-start. Responses include `cache: {age_sec, refreshing, stale}` and pages silently re-poll while a refresh runs. Warm responses are 20-200ms.
- Per-symbol history fetches are parallelized everywhere (`SCREENER_HISTORY_WORKERS`, default 6): screener, alerts, market indicators, divergence. News RSS feeds fetch concurrently.
- In-memory TTL caches keep repeat scans nearly free: history `HISTORY_CACHE_TTL_SEC` (600s default), quotes `QUOTE_CACHE_TTL_SEC` (60s), divergence history 30 min.
- Quote fetches skip market-cap profile enrichment where market cap isn't displayed (`fetch_quote(enrich=False)`).
- SQLite (`social_baseline.db`) runs in WAL mode; schema DDL runs once per process; retention pruning runs at startup + daily.

## Data Storage & Deployment
All persistent state lives in three places (everything else is in-memory cache or browser localStorage):

| What | Where | Migrate? |
|------|-------|----------|
| Editable divergence peer groups | `python_api/peer_groups.json` | **Commit to git** (small, text, user-curated) |
| Social baselines (45d), symbol master snapshots, signal-lab runs (14d), after-hours/ETF snapshots, SWR endpoint results, validity/liveness caches | `python_api/social_baseline.db` (SQLite, WAL — also `-wal`/`-shm` sidecars) | Copy **once** via rsync/scp. Do NOT commit — binary, churns on every request. Losing it is non-fatal: caches rebuild, but social buzz baselines take ~30 days to re-learn. |
| Provider API budget counters (per-day) | `python_api/provider_budget.db` | No — rebuilds daily |
| API keys / env config | `.env` / `.env.local` | Copy manually (never commit) |
| Watchlist, chart-board symbols, selected divergence groups | Browser localStorage | Per-browser; re-add or export by hand |

Fresh-machine setup:
```bash
git clone <repo> && cd trading-bb
python3 -m venv .venv && .venv/bin/pip install -r python_api/requirements.txt
npm install
# optional: rsync old machine's python_api/social_baseline.db into python_api/
npm run dev          # or production: npm run build && PORT=3500 npm run start  (+ uvicorn without --reload)
```

## Provider Budget Guard
Per-provider request budgets prevent free-tier limits from ever being exceeded
(`python_api/provider_budget.py`). Daily usage persists in SQLite across restarts.
Defaults (scaled by `PROVIDER_BUDGET_SAFETY_PCT`, default 0.9): twelvedata 8/min + 800/day,
fmp 10/min + 250/day, marketstack 5/min + 3/day, yfinance 30/min pacing.
Override per provider: `BUDGET_TWELVEDATA_PER_DAY=800`, `BUDGET_FMP_PER_MIN=5`, etc. (0 = unlimited).
When a provider's budget is exhausted the router skips to the next provider instead of burning quota.
Inspect live usage: `GET /api/providers/budget`. Cache TTLs are env-tunable:
`HISTORY_CACHE_TTL_SEC` (default 600), `QUOTE_CACHE_TTL_SEC` (default 60).

Market data provider routing env vars:

```bash
export TWELVE_DATA_API_KEY="..."
export FMP_API_KEY="..."
export MARKETSTACK_API_KEY="..."
export QUOTE_PROVIDER_PRIORITY="twelvedata,fmp,marketstack"
export HISTORY_PROVIDER_PRIORITY="twelvedata,fmp,marketstack"
export ENABLE_YFINANCE="0"
export DISCOVERY_PROVIDER=""
export MARKET_DATA_PROVIDER_COOLDOWN_SEC="180"
export MARKET_DATA_TIMEOUT_SEC="8"
export SCREENER_UNIVERSE_SYMBOLS="AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL,SPY,QQQ"
```

The Python API also auto-loads these vars from `.env` / `.env.local` in either:
- repo root (`./.env`, `./.env.local`)
- `python_api/` (`python_api/.env`, `python_api/.env.local`)

Starter template: `.env.example`

Behavior:
- Uses low-rate public Reddit listing endpoints.
- Applies pacing + retry/backoff to reduce transient failures.
- Adds fast fallback sources (`apewisdom` + Stocktwits trending) and can skip Reddit when fallback is ready.
- Quote/history calls auto-rotate providers based on priority and temporary cooldown on quota/auth failures.
- Yahoo (`yfinance`) is disabled by default. Set `ENABLE_YFINANCE="1"` and include `yfinance` in priority lists only if your account has access.
- If you use OpenBB discovery endpoints directly for universe-building, keep `DISCOVERY_PROVIDER=""` (auto/default) or set a provider you can access.
- Pushshift is disabled in production ingestion flow.

Optional TikTok provider hook (no scraping):

```bash
export TIKTOK_PROVIDER_URL="https://your-provider-endpoint"
export TIKTOK_PROVIDER_API_KEY="your_api_key"
export TIKTOK_PROVIDER_TIMEOUT_SEC="10"
```

If configured, TikTok rows are merged into social sentiment scoring as source `tiktok`.

## Security Hardening Environment (Phase 6)
Optional env vars:

```bash
export CORS_ALLOW_ORIGINS="http://localhost:3000,http://127.0.0.1:3000"
export SOCIAL_API_KEY="replace_with_strong_secret"
export API_RATE_LIMIT_WINDOW_SEC="60"
export API_RATE_LIMIT_MAX_REQUESTS="300"
export SOCIAL_RATE_LIMIT_WINDOW_SEC="60"
export SOCIAL_RATE_LIMIT_MAX_REQUESTS="120"
```

Notes:
- If `SOCIAL_API_KEY` is set, `/api/social/*` endpoints require auth.
- Next proxy forwards `SOCIAL_API_KEY` to Python API automatically in server-side requests.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest        # 15 tests, ~6s
```

`tests/` covers the pure indicator math (SMA/RSI/MACD/RVOL + trigger flags),
the per-provider budget guard (limits, env overrides, exhaustion), and endpoint
smoke tests (health, security status, symbol validation) via FastAPI's
`TestClient` — all offline, no market-data calls.

## Smoke Test Runbook
After `npm run dev`, verify key routes:

```bash
curl -s 'http://127.0.0.1:3000/api/market/overview' | head -c 400
curl -s 'http://127.0.0.1:3000/api/news/headlines?limit=10' | head -c 400
curl -s 'http://127.0.0.1:3000/api/social/sentiment?limit=5' | head -c 600
curl -s 'http://127.0.0.1:3000/api/security/status' | head -c 500
curl -s 'http://127.0.0.1:3000/technical' | rg 'Technical View|Recently Searched|MACD'
curl -s 'http://127.0.0.1:3000/' | rg 'Market Overview|After Hours Movers|Alerts / Flags|Industries'
```

## License

This project's own code is licensed **MIT** — see [LICENSE](LICENSE).

It depends on [OpenBB](https://github.com/OpenBB-finance/OpenBB) (installed via
pip, not vendored here), which is licensed **AGPL-3.0**. Market data is subject
to each provider's terms (TwelveData, FMP, Marketstack, etc.). This is an
educational/research tool, not financial advice.
