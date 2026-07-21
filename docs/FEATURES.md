# Feature inventory

The full per-page checklist of what was built, moved out of the README to keep
it readable. See [TRADING_DASH.md](../TRADING_DASH.md) for the technical spec.

## Frontend pages

- `/` Market Overview
  - Top gainers, top losers, most active
  - Summary cards
  - Search filter
  - Manual refresh
  - Last updated timestamp
  - Alerts/flags panel
  - After-hours movers
  - Industry Divergence chips (click opens a divergence scan of that peer group)
- `/charts` Chart Board
  - Opens in dedicated popup window from ticker clicks
  - Up to 9 charts
  - Candlestick mini-charts with volume lane
  - Last close + visible-range % change per card, link to ticker detail
  - Mouse wheel zoom + drag pan interactions
- `/news`
  - Consolidated headlines from notable free sources (MarketWatch, Financial
    Times, NYTimes Business, Investing.com, SEC press releases)
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
  - "Screen My Watchlist": run filters against your watchlist symbols
  - Custom universe via `POST /api/screener` `symbols` field (max 100 symbols)
  - Table zoom controls
- `/etfs` Popular ETFs
  - Top ETFs by volume with industry/category and largest holdings
  - Holdings modal: clickable holdings, top performers/losers, one-click
    Divergence Scan of the top-20 holdings vs the ETF
- `/social`
  - Never blocks: the endpoint serves the last completed scan instantly
    (stale-while-revalidate); the 30-60s Reddit/Stocktwits scan runs in a
    single-flight background thread. Data-age badge + auto-polling while
    refreshing; "Refresh Now" forces a background rescan. Last scan survives
    restarts (restored from SQLite). Tune with
    `SOCIAL_RESULT_REFRESH_AFTER_SEC` (default 600 = auto-rescan when data
    older than 10 min on access).
  - Compact view by default (toggle Detailed for all columns) + tier filter
    (All / A+B / A only)
  - Reddit-first social sentiment scanner
  - Cross-source enrichment with Stocktwits
  - Tiered candidate quality (`A/B/C`), mention-quality score, weighted source
    confirmation
  - Tradability overlays: liquidity/spread/volatility gates, market-cap
    bucket, market regime
  - Catalyst/options overlays: catalyst tags and options context score
  - Candidate confidence, validation status, explainability fields, and
    ranking outputs
- `/watchlist`
  - Add/remove symbols (multi-add: "AAPL, MSFT NVDA"; validated)
  - Local persistence via `localStorage`
  - Live quote refresh
  - Click-through to ticker detail
  - One-click Divergence Scan of the whole watchlist (3+ symbols)
- `/divergence` Divergence Scanner
  - Group Pulse: one click shows every group's median gain/loss over
    1D / 1W / 1M plus benchmark and best/worst member; click any group to
    drill into a full scan
  - 35 editable peer groups in categories: Industries, Crypto (miners,
    treasury, exchanges, DC hardware), Defense & Drones (tactical UAS,
    counter-UAS, naval, defense electronics), Semis & Photonics (photonics,
    silicon photonics, GaN/SiC, RF, semicap, packaging), My Buckets
  - Groups are stored in `python_api/peer_groups.json`; edit via the edit
    button / "+ New Group" on the page (or the file directly): name,
    category, benchmark, members, notes
  - Scans peer groups and custom stock/ETF sets; flags stocks NOT moving with
    their peers (robust leave-one-out z-score over 1D/5D/20D windows)
  - 60-day correlation/beta vs equal-weight peer index filters out habitual
    independent movers
  - Plain-English labels: "Held up while group fell", "Lagged group rally", etc.
  - Deep links: `/divergence?groups=semiconductors` or
    `?symbols=AAPL,MSFT,...&benchmark=QQQ` (auto-scans)
  - Backend: `python_api/divergence.py`; `GET/POST /api/divergence/groups`,
    `PUT/DELETE /api/divergence/groups/{id}`, `GET /api/divergence/overview`,
    `POST /api/divergence/scan`

## Python API

Path: `python_api/main.py` (routes), with the domain logic split across
`providers`, `news`, `social`, `divergence`, `symbol_master`, `indicators`,
`provider_budget`, `db`, `net`, `cache`, `config`, `utils`.

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

## Scripts

From the repo root:

- `npm run dev`: starts both the Next dev server and
  `./.venv/bin/uvicorn python_api.main:app --reload --host 127.0.0.1 --port 8001`
- `npm run build`, `npm run lint`, `npm run typecheck`
- `npm run metrics:phase3-eval`: Phase 3 holdout benchmark comparison using
  `docs/program/PHASE3_BENCHMARK.json`
- `npm run metrics:phase5-event-study`: horizon-based event-study metrics for
  the latest persisted signal run
- `npm run metrics:phase6-security`: Phase 6 security checks
  (status/auth/rate-limit); pass `--api-key` when auth is enabled

## Notes / limitations (OpenBB 4.7.1 + provider router)

OpenBB 4.7.1 routes used: `obb.equity.price.historical`,
`obb.equity.price.quote`, `obb.equity.discovery.{gainers,losers,active}` (the
deprecated `obb.equity.historical` is not used).

- A single universal screener endpoint with all requested filters is not
  exposed directly by OpenBB 4.7.1.
- Screener is implemented by building a practical universe, then calculating
  technicals locally and filtering server-side.
- Candlestick charts are implemented via custom SVG components.
- Market data calls use provider routing with fallback and cooldown
  (`twelvedata`, `fmp`, `marketstack` by default; optional `yfinance` opt-in).
