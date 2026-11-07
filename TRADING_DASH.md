# Trading Dashboard Technical + Functional Specification

## 1) Purpose and Scope

This repository contains a local-first trading dashboard composed of:
- A Next.js 16 App Router frontend (React 19, TypeScript).
- A FastAPI backend (`python_api/main.py`) powered by OpenBB (`openbb` SDK) and pandas/numpy.
- A thin Next API proxy layer that forwards browser calls to the Python API.

Primary use cases:
- Market overview (gainers/losers/active), alerts, after-hours movers.
- Ticker detail views with OHLCV history and CSV export.
- Technical analysis with RSI/SMA/MACD visualizations.
- Screener with server-side filters + client-side preset buttons.
- Multi-chart board window (up to 9 tickers) with candlestick mini-charts, zoom and pan.
- Watchlist persisted in browser localStorage.
- Social sentiment scanner (Reddit + Stocktwits enrichment) with confidence/ranking pipeline.
- Headline news feed aggregation (MarketWatch, Financial Times, NYTimes Business, Investing.com, SEC press releases).

## 2) Repository Structure

```
.
├── app/                         # Next.js App Router pages and API routes
│   ├── api/                     # Next route handlers, proxying to FastAPI
│   ├── charts/                  # Chart Board page
│   ├── news/                    # Headline News page
│   ├── screener/                # Screener page
│   ├── social/                  # Social Sentiment page
│   ├── technical/               # Technical analysis page
│   ├── ticker/[symbol]/         # Ticker detail page
│   ├── watchlist/               # Watchlist page
│   ├── globals.css              # Global styling/tokens
│   ├── layout.tsx               # Root layout and app shell wrapper
│   └── page.tsx                 # Market Overview home page
├── components/
│   ├── app-shell.tsx            # Global nav, search/open chart window
│   ├── market-table.tsx         # Sortable market table
│   ├── price-volume-chart.tsx   # Recharts + SVG candlestick chart component
│   └── ticker-board-link.tsx    # Link helper that opens chart board popup window
├── lib/
│   ├── api.ts                   # Browser GET/POST helpers
│   └── proxy.ts                 # Next->Python API proxy helper
├── python_api/
│   ├── main.py                  # FastAPI app, middleware and router wiring
│   ├── routers/                 # One APIRouter per API area
│   ├── *.py                     # Service modules (market data, social scan, ETFs, ...)
│   └── social_baseline.db       # SQLite baseline store for social scanner normalization
├── public/                      # Static assets
├── package.json                 # Node scripts/dependencies
├── tsconfig.json                # TS config
├── eslint.config.mjs            # ESLint config
├── postcss.config.mjs           # Tailwind PostCSS plugin config
└── README.md
```

Notes:
- `.next`, `.venv`, `node_modules` are environment/build artifacts (excluded above).

## 3) Runtime Architecture

### 3.1 Request Flow
1. Browser loads Next.js pages (client components).
2. UI requests `app/api/...` endpoints (same-origin).
3. Next route handlers call `proxy()` in `lib/proxy.ts`.
4. `proxy()` forwards request to FastAPI (`PYTHON_API_BASE`, default `http://127.0.0.1:8001`).
5. FastAPI executes data retrieval/transforms and returns JSON.
6. Next returns JSON response back to browser.

### 3.2 Process Model
`npm run dev` starts two processes via `concurrently`:
- `next dev` on port 3000.
- `uvicorn python_api.main:app --reload --host 127.0.0.1 --port 8001`.

### 3.3 Caching Layers
- Frontend fetches use `cache: "no-store"`.
- Next proxy uses `cache: "no-store"` when calling Python API.
- Python backend uses in-memory TTL cache (`_cache`) for high-frequency endpoints.
- Social sentiment normalization history persists in SQLite (`python_api/social_baseline.db`).

## 4) Frontend Application

### 4.1 Global Shell and Navigation
File: `components/app-shell.tsx`
- Left sidebar nav items:
  - Market (`/`)
  - Chart Board (`/charts`)
  - Headline News (`/news`)
  - Technical (`/technical`)
  - Screener (`/screener`)
  - Social Sentiment (`/social`)
  - Watchlist (`/watchlist`)
- Header ticker search form opens chart board popup (`window.open`) with query `?add=SYMBOL`.
- Refresh button calls `router.refresh()`.

### 4.2 Styling System
Files: `app/globals.css`, Tailwind v4 + custom utility classes
- Dark theme CSS variables for panels/borders/text/status colors.
- Reusable class conventions:
  - `.card`, `.btn`, `.input`, `.badge`
  - `.table`, `.table-wrap`
  - `.kpi-label`, `.kpi-value`
- Uses gradient page background and custom typography stack.

### 4.3 Market Page (`app/page.tsx`)
Functional sections:
- KPI cards: top gainers/losers/active counts + alert count.
- Search filter by ticker/company.
- Market cap filter chips: `All`, `Micro`, `Small`, `Mid`, `Large`, `Mega`.
- Three sortable market tables via `MarketTable` component.
- After-hours movers table (commodities + stocks/ETFs).
- Alerts/flags table.
- Static industry tags section.

Loading model:
- Overview, alerts, and after-hours load independently (parallel client calls).
- Each section supports its own loading/error state.

### 4.4 Chart Board (`app/charts/page.tsx`)
Core behavior:
- Dedicated popup board (`openbb-chart-board` window).
- Maintains symbol list in localStorage (`openbb_chart_board_symbols`).
- Adds symbols from query param `?add=...` and deduplicates.
- Max 9 charts; newest additions retained when over capacity.
- Default chart range: `1Y`.

Per-chart interactions (`ChartCard` + `MiniCandlestick`):
- SVG candlestick rendering from historical OHLC data.
- Mouse wheel zoom (changes visible window percent).
- Shift + wheel pans horizontally.
- Pointer drag pans horizontally and vertically.
- Double-click resets zoom + pan.
- Remove chart button per tile.

### 4.5 Ticker Detail (`app/ticker/[symbol]/page.tsx`)
- Fetches quote and historical data in parallel for selected symbol.
- Range switching (`1M`, `3M`, `6M`, `1Y`, `5Y`).
- KPI strip: last, daily %, volume, 52w high/low, MA50/MA200.
- Price/volume chart via shared `PriceVolumeChart`.
- Preview table of recent rows.
- CSV download of full loaded history.

### 4.6 Technical Page (`app/technical/page.tsx`)
- Symbol input + range toggles.
- Fetches `/api/ticker/{symbol}/technicals`.
- Displays rule-flag status badges.
- Price/volume chart with SMA overlays.
- RSI chart and MACD chart (Recharts).
- Calculation-notes section driven by backend metadata.

### 4.7 Screener Page (`app/screener/page.tsx`)
- Server-side screener payload builder.
- Manual filters: ticker contains, price bounds, volume, % change, RSI, SMA booleans.
- Preset buttons:
  - Momentum Breakouts
  - Oversold Bounce
  - Strong Uptrend
  - High Volume Movers
  - Semiconductors
  - Utilities Style
- Result table with:
  - Ticker, name, price, % change, volume, RSI, RVOL, above SMA20/50/200.
- Table zoom controls (`-`, `Reset`, `+`) plus Ctrl/Cmd+wheel zoom.

### 4.8 Social Sentiment Page (`app/social/page.tsx`)
- Pulls ranked social opportunities from backend social scanner.
- Filters by ticker text, min mentions, sentiment label.
- Displays confidence + buzz metrics and “why popping” drivers.
- Shows scanner notes returned by backend pipeline.

### 4.9 Watchlist Page (`app/watchlist/page.tsx`)
- Local watchlist symbol entry and removal.
- Storage key: `openbb_watchlist_symbols` in browser localStorage.
- Fetches quotes for list via `/api/watchlist/quote`.
- Displays symbol, name, price, % move, volume.

### 4.10 Headline News (`app/news/page.tsx`)
- Fetches `/api/news/headlines?limit=40`.
- Client-side filter by headline/source text.
- Table with published time, source, headline link.

### 4.11 Reusable Components

`components/market-table.tsx`
- Sortable columns (symbol/price/%change/volume).
- Renders ticker links using `TickerBoardLink`.

`components/price-volume-chart.tsx`
- If OHLC exists: custom SVG candlestick chart.
- Else fallback to Recharts area chart.
- Optional SMA overlays in both chart modes.
- Volume rendered via Recharts bar chart.

`components/ticker-board-link.tsx`
- Intercepts link click and opens/targets named chart popup window.
- Ensures ticker workflows don’t navigate away from current page.

## 5) Next API Proxy Layer

All `app/api/*/route.ts` handlers proxy to Python API through `lib/proxy.ts`.

Routes exposed by Next layer:
- `GET /api/market/overview`
- `GET /api/market/after-hours`
- `GET /api/news/headlines?limit=`
- `GET /api/ticker/[symbol]/historical?range=`
- `GET /api/ticker/[symbol]/technicals?range=`
- `GET /api/ticker/[symbol]/quote`
- `POST /api/screener`
- `GET /api/social/sentiment?limit=`
- `GET /api/watchlist/quote?symbols=`
- `GET /api/alerts?symbols=`

Proxy behavior (`lib/proxy.ts`):
- Adds JSON content-type header.
- Returns raw upstream text with upstream status code.
- On connection error returns HTTP 502 with detail message.

## 6) Backend (FastAPI) Technical Design

Files: `python_api/main.py` (app wiring), `python_api/routers/` (endpoints) and the service modules beside them

### 6.1 Core Dependencies and Patterns
- FastAPI for HTTP API.
- OpenBB SDK (`obb`) as market data provider (default `yfinance`).
- pandas/numpy for transforms and indicator calculations.
- In-memory TTL cache with thread lock.
- SQLite for social baseline persistence.
- CORS enabled wide-open (`allow_origins=["*"]`).

### 6.2 Validation and Utility Functions
- `validate_symbol`: pattern checks symbol format.
- `normalize_pct`, `to_float`: numeric safety conversion helpers.
- `market_cap_bucket`: categorizes into Micro/Small/Mid/Large/Mega.
- `range_to_start`: maps selected range to lookback date.
- `row_to_market_record`: normalizes quote/discovery rows into frontend shape.

### 6.3 Data Fetch Utilities
- `fetch_quote(symbols, ttl=30)` with cache.
- `fetch_history(symbol, range_key, ttl=120)` with cache and required OHLCV columns.
- `compute_indicators`:
  - SMA20, SMA50, SMA200
  - RSI14 (Wilder style)
  - MACD, signal, histogram
  - avg_volume20, RVOL
  - daily_change_pct

### 6.4 Screener Universe
- `build_universe()` builds symbol set from OpenBB discovery lists:
  - gainers
  - losers
  - active
  - growth_tech
  - aggressive_small_caps
- Fallback universe if discovery fails.

### 6.5 Social Scanner Subsystem

#### Source strategy
- Reddit primary subreddits:
  - `wallstreetbets`, `stocks`, `options`, `investing`, `shortsqueeze`
- Fallback ingestion chain per subreddit:
  1. Reddit JSON (`hot`, `new`)
  2. Pushshift endpoint
  3. old.reddit HTML parse
- Optional enrichment source:
  - Stocktwits symbol stream API for top candidates

#### Candidate extraction and filtering
- Regex extraction supports cashtags and uppercase tokens.
- Exclusion list for common false positives.
- Context scoring (`mention_context_score`) for finance terms + cashtag bonus.
- Symbol master lookup before quote validation:
  - Pulls NASDAQ and other listed symbol files.
  - Filters test issues, warrants/rights/units/preferred.

#### Evidence + ambiguity gating
- Minimum evidence checks include combinations of:
  - mentions across multiple authors
  - cashtag presence
  - high engagement
  - cross-source confirmation
- Ambiguous tickers (`AI`, `IT`, `ON`, `US`, etc.) require stricter evidence.

#### Validation strategy
- Confidence score built from:
  - symbol master match
  - cashtag count
  - finance context score
  - multi-author and cross-source bonuses
  - ambiguity/spam penalties
- Only top confidence candidates are quote-validated (batched where possible).

#### Ranking model
Final rank score combines:
- Qualified mention volume.
- Unique authors.
- Engagement.
- Momentum (last 6h vs previous 6h).
- Abnormal buzz metrics (current 1h vs 30d hourly baseline).
- Engagement z-score.
- Sentiment score.
- Candidate confidence.

#### Baseline persistence
- SQLite DB `python_api/social_baseline.db`.
- Table stores per ticker/hour:
  - mentions
  - unique authors
  - engagement
- Rolling retention cleanup (~45 days).
- 30-day hourly averages/std used for normalization output fields.

### 6.6 Backend Endpoints and Functional Contracts

`GET /api/health`
- Returns status, timestamp, provider.

`GET /api/market/overview`
- Returns top gainers/losers/active arrays + summary counts.
- Adds per-list error fields if source-specific failure occurs.

`GET /api/market/after-hours`
- Quotes predefined commodities and stock/ETF symbols.
- Returns top movers sorted by absolute % move.

`GET /api/news/headlines?limit=5..60`
- Aggregates MarketWatch Top Stories, Financial Times, NYTimes Business, Investing.com Stocks, and SEC press-release RSS feeds.
- Deduplicates by URL and sorts by publish timestamp.

`GET /api/ticker/{symbol}/historical?range=`
- Returns full OHLCV data series + preview tail.

`GET /api/ticker/{symbol}/technicals?range=`
- Computes indicators from 5Y source history then slices to requested range.
- Returns latest indicator snapshot, boolean flags, chart rows, and calculation notes.

`GET /api/ticker/{symbol}/quote`
- Returns quote fields with normalized daily % change.

`POST /api/screener`
- Applies server-side filters to derived universe and indicator fields.
- Returns ranked filtered rows + universe count.

`GET /api/social/sentiment?limit=5..100`
- Runs social scanner pipeline and returns ranked actionable rows.
- Includes counts, errors, source diagnostics, and notes.

`GET /api/watchlist/quote?symbols=...`
- Validates symbols and returns quote snapshot rows.

`GET /api/alerts?symbols=`
- Computes per-symbol alert flags:
  - RSI thresholds
  - 50SMA crossovers
  - RVOL >= 2
  - large daily move (>=5%)

## 7) Data Models (Frontend-Visible)

Major response shapes used by UI:
- Market overview rows: `symbol`, `name`, `price`, `percent_change`, `volume`, `market_cap`, `market_cap_bucket`.
- Historical row: `date`, `open`, `high`, `low`, `close`, `volume`.
- Technical latest: close/RSI/SMA/MACD/RVOL + boolean flags.
- Screener row: price/volume/%change/RSI/SMA booleans/RVOL.
- Social row:
  - `raw_mentions`, `qualified_mentions`, `unique_authors`
  - `cashtag_mentions`, `finance_context_score`, `candidate_confidence`
  - `cross_source_confirmed`, `validation_status`, `ambiguity_risk`
  - buzz normalization fields (`mention_buzz_ratio`, `author_buzz_ratio`, `engagement_zscore`)
  - ranking fields (`rank_score`, `why`, `sources`)

## 8) Persistence and State

Browser localStorage keys:
- `openbb_chart_board_symbols`: chart board window symbol stack.
- `openbb_watchlist_symbols`: watchlist symbols.

Backend persisted state:
- `python_api/social_baseline.db`: rolling social baseline metrics.

Ephemeral memory:
- `_cache` in Python for TTL cached endpoint payloads/dataframes.

## 9) Performance Characteristics

Current performance strategies:
- Progressive independent loading on Market page (overview/alerts/after-hours).
- Backend TTL caches reduce repeated OpenBB/network work.
- Social scanner limits costly quote validation to top confidence candidates.
- Batched quote validation in chunks in social pipeline.

Known heavy paths:
- Social sentiment endpoint is the most expensive due to multi-source scraping, enrichment, validation, and scoring.
- Screener calls historical+indicator logic per symbol in universe.

## 10) Error Handling and Resilience

Frontend:
- Each page uses local loading/error state and shows user-readable error cards.

Proxy layer:
- If Python API unavailable, Next returns 502 with target base URL detail.

Backend:
- Symbol validation returns HTTP 400 on invalid format.
- Upstream data failures mapped to HTTP 502 in quote/history fetch helpers.
- Several aggregators collect per-source errors while still returning partial results.

## 11) Security/Operational Notes

- CORS is permissive (`*`) in local API.
- No authentication or authorization layer is present.
- No rate limiter is implemented.
- Social scraping uses rotating user agents and small sleeps to reduce immediate blocking pressure.
- Watchlist/chart board state is client-local only (not shared/server-side).

## 12) Build, Run, and Tooling

Node scripts (`package.json`):
- `npm run dev` runs Next + FastAPI together.
- `npm run build` builds Next app.
- `npm run start` starts production Next server.
- `npm run lint` runs ESLint on `app`, `components`, `lib`.
- `npm run typecheck` runs `tsc --noEmit`.

Config files:
- `tsconfig.json`: strict mode, App Router path alias `@/*`.
- `eslint.config.mjs`: Next core-web-vitals + TS config.
- `postcss.config.mjs`: Tailwind PostCSS plugin.
- `next.config.ts`: currently default/empty config object.

## 13) Functional Coverage Summary

Implemented feature set includes:
- Market overview dashboards with filters.
- After-hours movers including commodities and stocks.
- Industry tag section.
- Headline news tab in left nav.
- Screener presets and zoom controls.
- Chart board popup workflow with stacked charts (max 9).
- Candlestick rendering across chart board and detail chart component.
- Drag pan + wheel zoom interactions on chart board tiles.
- Watchlist local persistence + quote refresh.
- Social sentiment scanner with multi-source confidence pipeline and ticker validation improvements.

