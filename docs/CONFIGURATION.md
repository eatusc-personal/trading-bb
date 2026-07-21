# Configuration and operations

Environment reference, persistence map, and runbooks, moved out of the README.
Starter template: `.env.example`. The Python API auto-loads `.env` /
`.env.local` from either the repo root or `python_api/`.

## Market data provider routing

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

Behavior:

- Quote/history calls auto-rotate providers based on priority and temporary
  cooldown on quota/auth failures.
- Yahoo (`yfinance`) is disabled by default. Set `ENABLE_YFINANCE="1"` and
  include `yfinance` in priority lists only if your account has access.
- If you use OpenBB discovery endpoints directly for universe-building, keep
  `DISCOVERY_PROVIDER=""` (auto/default) or set a provider you can access.

## Provider budget guard

Per-provider request budgets prevent free-tier limits from ever being exceeded
(`python_api/provider_budget.py`). Daily usage persists in SQLite across
restarts. Defaults (scaled by `PROVIDER_BUDGET_SAFETY_PCT`, default 0.9):
twelvedata 8/min + 800/day, fmp 10/min + 250/day, marketstack 5/min + 3/day,
yfinance 30/min pacing. Override per provider: `BUDGET_TWELVEDATA_PER_DAY=800`,
`BUDGET_FMP_PER_MIN=5`, etc. (0 = unlimited). When a provider's budget is
exhausted the router skips to the next provider instead of burning quota.
Inspect live usage: `GET /api/providers/budget`. Cache TTLs are env-tunable:
`HISTORY_CACHE_TTL_SEC` (default 600), `QUOTE_CACHE_TTL_SEC` (default 60).

## Social ingestion

Optional env vars for public Reddit ingestion pacing and the fallback sources:

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

Behavior:

- Uses low-rate public Reddit listing endpoints with pacing + retry/backoff.
- Adds fast fallback sources (`apewisdom` + Stocktwits trending) and can skip
  Reddit when fallback is ready.
- Pushshift is disabled in production ingestion flow.

Optional TikTok provider hook (no scraping):

```bash
export TIKTOK_PROVIDER_URL="https://your-provider-endpoint"
export TIKTOK_PROVIDER_API_KEY="your_api_key"
export TIKTOK_PROVIDER_TIMEOUT_SEC="10"
```

If configured, TikTok rows are merged into social sentiment scoring as source
`tiktok`.

## Security hardening

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
- The Next proxy forwards `SOCIAL_API_KEY` to the Python API automatically in
  server-side requests.

## Performance notes

- Stale-while-revalidate everywhere: slow read endpoints (market overview,
  alerts, after-hours, indicators, news, social) answer instantly from their
  last completed result and refresh in a single-flight background thread when
  stale. Results persist in SQLite (`swr_cache`), so restarts never
  cold-start. Responses include `cache: {age_sec, refreshing, stale}` and
  pages silently re-poll while a refresh runs. Warm responses are 20-200ms.
- Per-symbol history fetches are parallelized everywhere
  (`SCREENER_HISTORY_WORKERS`, default 6): screener, alerts, market
  indicators, divergence. News RSS feeds fetch concurrently.
- In-memory TTL caches keep repeat scans nearly free: history
  `HISTORY_CACHE_TTL_SEC` (600s default), quotes `QUOTE_CACHE_TTL_SEC` (60s),
  divergence history 30 min.
- Quote fetches skip market-cap profile enrichment where market cap isn't
  displayed (`fetch_quote(enrich=False)`).
- SQLite (`social_baseline.db`) runs in WAL mode; schema DDL runs once per
  process; retention pruning runs at startup + daily.

## Data storage and deployment

All persistent state lives in three places (everything else is in-memory cache
or browser localStorage):

| What | Where | Migrate? |
|------|-------|----------|
| Editable divergence peer groups | `python_api/peer_groups.json` | Commit to git (small, text, user-curated) |
| Social baselines (45d), symbol master snapshots, signal-lab runs (14d), after-hours/ETF snapshots, SWR endpoint results, validity/liveness caches | `python_api/social_baseline.db` (SQLite, WAL; also `-wal`/`-shm` sidecars) | Copy once via rsync/scp. Do NOT commit: binary, churns on every request. Losing it is non-fatal: caches rebuild, but social buzz baselines take ~30 days to re-learn. |
| Provider API budget counters (per-day) | `python_api/provider_budget.db` | No; rebuilds daily |
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

## Smoke test runbook

After `npm run dev`, verify key routes:

```bash
curl -s 'http://127.0.0.1:3000/api/market/overview' | head -c 400
curl -s 'http://127.0.0.1:3000/api/news/headlines?limit=10' | head -c 400
curl -s 'http://127.0.0.1:3000/api/social/sentiment?limit=5' | head -c 600
curl -s 'http://127.0.0.1:3000/api/security/status' | head -c 500
curl -s 'http://127.0.0.1:3000/technical' | rg 'Technical View|Recently Searched|MACD'
curl -s 'http://127.0.0.1:3000/' | rg 'Market Overview|After Hours Movers|Alerts / Flags|Industries'
```
