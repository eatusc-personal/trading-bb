# Progress Log

## 2026-06-09 (late night) — App-wide speed pass: SWR everywhere

- **Generic `swr_serve()` layer** (main.py): slow read endpoints now serve their last completed result instantly and refresh in a single-flight background thread when older than their freshness window. Results persist in SQLite (`swr_cache` table), so dev reloads / restarts never cause a cold start. Applied to:
  - `/api/market/overview` (60s freshness), `/api/alerts` default universe (120s), `/api/market/after-hours` (120s), `/api/market/indicators` (300s), `/api/news/headlines` (300s). Social already had its own SWR.
  - Endpoints attach `cache: {age_sec, refreshing, stale, ...}`; Market + News pages silently re-poll a few times while `refreshing` so fresh data swaps in without user action.
- **News feeds parallelized**: 5 RSS feeds were fetched sequentially (worst case ~40s); now concurrent (bounded by slowest single feed).
- **`fetch_quote(enrich=False)`**: ticker quote, watchlist, screener, top-volume triggers, and liveness checks skip the market-cap profile enrichment round-trip (only overview/after-hours/social actually use market cap).
- Measured (warm): overview 0.17s, alerts 0.04s, after-hours 0.03s, indicators 0.04s, news 0.03s, social 0.09s. After a full process restart: 0.02-0.6s (served from persisted SWR results while refreshing in background).
- Repo prep: `.gitignore` extended (python, *.db, env), `python_api/requirements.txt` added with pinned versions.

## 2026-06-09 (night) — Social Sentiment tab: timeouts fixed (stale-while-revalidate)

- Root cause: `/api/social/sentiment` ran the full live scan (paced Reddit ×5 subreddits + Stocktwits + ApeWisdom, 30-60s wall clock) inline on every request, against a 25s Next proxy timeout and 30s browser timeout → frequent timeouts whenever the 120s result cache was cold.
- Fix: the heavy scan is now `run_social_sentiment_scan()` and only ever runs in a **single-flight background thread**. The endpoint always answers instantly from the last completed result and attaches `cache: {age_sec, refreshing, stale, last_error, ...}`.
  - Auto-refresh: result older than `SOCIAL_RESULT_REFRESH_AFTER_SEC` (default 600s) triggers a background rescan on access; `?refresh=true` forces one.
  - Restart resilience: on process start the last persisted signal run is restored from SQLite (`social_signal_run`/`social_signal_row`), so even `--reload` cycles never serve an empty tab.
  - UI: data-age badge ("Data: 4m ago" / "Scanning… (12s)"), auto-polls every 6s while a background scan runs, "Refresh Now" button, distinct first-run "warming" state, and a non-blocking warning when the last background scan failed (previous results stay visible).
  - Proxy timeout 25s → 60s (belt-and-braces; responses are now ~instant).
- Verified: cold request 1.5s (restored data), forced refresh returns instantly with `refreshing: true`, fresh scan swapped in after ~25s with no timeout anywhere.

## 2026-06-09 (evening) — Editable theme groups + Group Pulse

- **Peer groups are now editable data, not code**: moved to `python_api/peer_groups.json` (seeded on first run; file is source of truth afterwards, mtime-cached). CRUD API: `POST /api/divergence/groups`, `PUT/DELETE /api/divergence/groups/{id}`. UI: ✎ edit button on every group chip + "+ New Group" modal (name, category, benchmark, members, notes; two-click delete).
- **19 new themed groups** added across categories — Crypto (BTC miners/AI pivot incl. WGMI benchmark, crypto treasury vs IBIT, exchanges/fin-infra vs ARKF, crypto-adjacent DC hardware), Defense & Drones (tactical drones, counter-UAS, naval/unmanned maritime, defense electronics/RF/EW), Semis & Photonics (AI optics, silicon photonics, GaN/SiC power, RF compound, semicap niche, advanced packaging), and 5 "My Buckets" research baskets. Ticker hygiene: SQ→XYZ, MOG.A→MOG-A, GLXY US listing; dropped EMKR (delisted), CAES (private), promotional treasury micro caps. Private names (Anduril, Shield AI, Saronic, ...) noted in group notes — not scannable.
- **Group Pulse** (`GET /api/divergence/overview`): one click shows every group's median % gain/loss over 1D / 1W (5td) / 1M (21td) + benchmark % + best/worst member, computed from the same cached 6M history the scans use (consistent dates; warms the scan cache). Sortable by window; group name / Scan button runs the full divergence scan. Cold ~30s for ~220 symbols, warm <2s.
- **Quote repair**: `fetch_quote` now fills rows missing a usable last price (yfinance returns NaN `last_price` for ETFs — previously surfaced as bogus -100% moves on Watchlist/ETF pages) from cached daily history, preferring history's prior bar for the % change.
- yfinance pacing budget 120 → 240/min so a cold all-groups pulse never self-throttles.
- Known data quirk: late at night (after ~midnight UTC) Yahoo briefly drops the just-closed session's daily bar, so pulse/scan "as of" can show the prior session until their EOD data lands. Self-heals; pulse displays its as-of date.

## 2026-06-09 (later) — Tab improvements, speed pass

### Per-tab improvements
- **Market**: static "Industries" badges replaced with clickable **Industry Divergence** chips — each opens `/divergence` with that peer group preselected and auto-scans.
- **Chart Board**: candlestick cards now show a volume lane under the candles, last close + visible-range % change in the header, and a "Detail" link to the ticker page. `useSearchParams` wrapped in Suspense (build safety).
- **News**: relative age column ("2h ago"), source filter chips with counts, headline count selector (40/80/120), and $CASHTAG chips that link to ticker detail.
- **Technical**: 3 new signal badges computed from data already returned — MACD Bull/Bear Cross and Unusual Volume (live RVOL shown). Supports `/technical?symbol=XYZ` deep links (linked from ticker detail).
- **Screener**: backend `ScreenerRequest.symbols` lets a request supply its own universe (≤100 symbols). New **Screen My Watchlist** button runs current filters against the localStorage watchlist. The bogus `ticker_contains: "SM"` semiconductor preset now uses the real peer list. Results table columns are sortable.
- **Social**: tier filter (All / A+B / A only) and a **Compact View** default that hides 6 secondary columns (Authors, Momentum, Quality, Source W, Confidence, Engagement); Detailed View restores them.
- **Watchlist**: comma/space-separated multi-add with symbol validation and Enter-to-add; **Divergence Scan** button (3+ symbols) and **Screen These** shortcut.
- **Top ETFs**: holdings in the modal are clickable (chart board), and a **Divergence Scan** button scans the top-20 holdings against each other with the ETF itself as benchmark.
- **Ticker detail**: fixed crash when provider omits `ma_50d`/`ma_200d`/52-week fields (twelvedata/marketstack); daily change is color-coded; added "Technical View" link.
- **Divergence**: accepts `?groups=`, `?symbols=`, `?benchmark=` URL params and auto-scans — used by the Market/Watchlist/ETF deep links.

### Speed pass
- `/api/alerts` history fetches parallelized (was sequential over up to 40 symbols): cold 8-symbol scan ~1.1s vs ~6-8s before; warm ~60ms.
- `market_indicators` FMP grade fetches parallelized (was sequential per watch symbol); FMP grades/earnings-calendar calls now also go through the provider budget guard.
- `_social_db_conn()` no longer replays ~15 `CREATE TABLE/INDEX` statements on every connection — schema DDL runs once per process; SQLite now uses WAL + busy_timeout.
- `SCREENER_HISTORY_WORKERS` default 4 → 6.
- yfinance politeness budget raised 30 → 120/min so a cold 40-symbol screener/alerts run never self-throttles (still ~2 req/s pacing).

## 2026-06-09

### Completed
- New **Divergence Scanner** tab (`/divergence`, nav item added):
  - Backend `python_api/divergence.py` — 16 curated peer groups (semis, software, banks, energy, sector ETFs vs SPY, ...) plus custom stock/ETF sets with optional benchmark.
  - Detection: per-window (1D/5D/20D) returns, leave-one-out robust z-score (median/MAD) vs peers, 60-day correlation + beta vs equal-weight peer index. Confirmed diverger = |z| ≥ threshold AND corr ≥ min_corr.
  - Endpoints: `GET /api/divergence/groups`, `POST /api/divergence/scan`; Next proxy routes under `app/api/divergence/*`.
  - History cached 30 min per symbol; shares provider router + budget guard, so scans don't burn provider quota.
- **Provider budget guard** (`python_api/provider_budget.py`): per-minute + per-day budgets per provider, daily counts persisted in SQLite (`provider_budget.db`) so `--reload` restarts don't reset them; defaults match free tiers with a 0.9 safety margin; exhausted providers are skipped by the router. New `GET /api/providers/budget`; included in `/api/security/status`.
- Quota conservation: history cache TTL now `HISTORY_CACHE_TTL_SEC` (default 600s, was 120s), quote TTL `QUOTE_CACHE_TTL_SEC` (default 60s, was 30s).
- Cleanup from code review:
  - Archived legacy Streamlit `app.py` → `legacy/streamlit_app.py`.
  - Added structured logging (`logging`, `API_LOG_LEVEL`); previously-silent except blocks in symbol-master fetch, alias-seed load, env loading, and market-cap enrichment now log warnings.
  - Stale-snapshot warning + `snapshot_stale` field in symbol-master health (> 7 days).
  - Daily SQLite retention pass (`prune_sqlite_caches`) for `symbol_validity_cache` (180d), `quote_liveness_cache` (3d), `after_hours_snapshot` (30d), `popular_etf_snapshot` (60d); runs at startup and every 24h.
  - Metrics scripts + smoke test now respect `WEB_BASE` (default port 3500, matching the launcher); CORS defaults include port 3500.

## 2026-03-20

### Completed
- Implemented market-data provider routing + fallback in `python_api/main.py`:
  - Quote priority: `twelvedata,fmp,marketstack,yfinance`
  - History priority: `fmp,twelvedata,marketstack,yfinance`
  - Per-provider cooldown on quota/auth failures.
- Added source attribution in API responses (`provider`, `source_providers`, and row-level provider fields where applicable).
- Fixed screener runtime crash (`Cannot read properties of null (reading 'toFixed')`) by:
  - Backend numeric hardening in `python_api/main.py` (`to_float`, `normalize_pct`, `finite_or`) to avoid emitting non-finite values.
  - Frontend defensive formatting in `app/screener/page.tsx` (`fmtFixed`, `fmtInt`, `asFiniteNumber`) to avoid `toFixed` on null/invalid values.
  - Extended the same formatting guard to RSI/RVOL cells to prevent `NaN` display.

### Screener Criteria Validation (all returned data)
- Base run count: `39`
- `ticker_contains`: `1`
- `min_price`: `29`
- `max_price`: `30`
- `min_volume`: `29`
- `min_percent_change`: `40`
- `max_percent_change`: `40`
- `rsi_min`: `29`
- `rsi_max`: `30`
- `above_sma20=true`: `12`
- `above_sma20=false`: `28`
- `above_sma50=true`: `9`
- `above_sma50=false`: `31`
- `above_sma200=true`: `16`
- `above_sma200=false`: `24`

### Verification
- Python syntax: `./.venv/bin/python -m py_compile python_api/main.py` passed.
- TypeScript: `npm run typecheck` passed.
- Lint: `npm run lint` still reports one pre-existing unrelated issue in `app/charts/page.tsx:347` (`react-hooks/set-state-in-effect`).

### Notes for Next Context Window
- Primary files touched:
  - `python_api/main.py`
  - `app/screener/page.tsx`
  - `README.md`
- If screener behavior changes again, check backend numeric sanitation first (`finite_or`) and frontend formatter usage (`fmtFixed` / `fmtInt`).

## 2026-03-20 (Session 2)

### Completed
- Implemented new backend endpoint: `GET /api/market/indicators`
  - Headline sentiment score + 24h news velocity (from existing RSS headline pipeline).
  - Analyst grade drift + recent updates (FMP `stable/grades`).
  - Earnings proximity for tracked symbols (FMP `stable/earnings-calendar`).
  - Added explicit source notes in payload (`rss_blend`, `fmp_grades`, `fmp_earnings_calendar`).
- Added Next proxy route:
  - `app/api/market/indicators/route.ts`
- Added dashboard UI panel on home page:
  - “Sentiment & News Indicators” with KPI cards + upcoming earnings table + source note.
  - File: `app/page.tsx`
- Added screener source note in UI:
  - Displays `source_providers`/`provider` near screener tables so active API sources are visible.
  - File: `app/screener/page.tsx`
- Adjusted screener presets to be more practical in normal conditions:
  - `Momentum Breakouts`: lower thresholds.
  - `High Volume Movers`: lower thresholds.

### Provider/Plan Findings (from live checks)
- FMP `stable/earnings-calendar`: available on current key.
- FMP `stable/grades`: available on current key.
- FMP stock news endpoints: mostly restricted/empty on current plan.
- Therefore headline indicators currently use RSS blend instead of FMP news.

### Verification
- `./.venv/bin/python -m py_compile python_api/main.py` passed.
- `npm run typecheck` passed.
- Preset check (live):
  - `Momentum Breakouts` equivalent filters: `count=1`
  - `High Volume Movers` equivalent filters: `count=1`
- New indicators check (live):
  - headline score present
  - analyst updates present (`140` in sample run)
  - earnings proximity computed for tracked symbols
