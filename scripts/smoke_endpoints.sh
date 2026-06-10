#!/usr/bin/env bash
set -euo pipefail

BASE="${1:-${WEB_BASE:-http://127.0.0.1:3500}}"

run_page() {
  local name="$1"
  local url="$2"
  local must_have="$3"
  local body code
  body="$(mktemp)"
  code="$(curl -sS -m 40 -o "$body" -w '%{http_code}' "${BASE}${url}" || true)"
  if [[ "$code" != "200" ]]; then
    echo "FAIL [$name] GET $url -> HTTP $code"
    head -c 320 "$body"; echo
    rm -f "$body"
    return 1
  fi
  if ! rg -q "$must_have" "$body"; then
    echo "FAIL [$name] missing pattern /$must_have/"
    head -c 420 "$body"; echo
    rm -f "$body"
    return 1
  fi
  echo "PASS [$name] GET $url"
  rm -f "$body"
}

run_get() {
  local name="$1"
  local url="$2"
  local jq_expr="$3"
  local body code
  body="$(mktemp)"
  code="$(curl -sS -m 60 -o "$body" -w '%{http_code}' "${BASE}${url}" || true)"
  if [[ "$code" != "200" ]]; then
    echo "FAIL [$name] GET $url -> HTTP $code"
    head -c 320 "$body"; echo
    rm -f "$body"
    return 1
  fi
  if ! jq -e "$jq_expr" "$body" >/dev/null 2>&1; then
    echo "FAIL [$name] jq validation failed: $jq_expr"
    head -c 420 "$body"; echo
    rm -f "$body"
    return 1
  fi
  echo "PASS [$name] GET $url"
  rm -f "$body"
}

run_post() {
  local name="$1"
  local url="$2"
  local json="$3"
  local jq_expr="$4"
  local body code
  body="$(mktemp)"
  code="$(curl -sS -m 90 -o "$body" -w '%{http_code}' -H 'Content-Type: application/json' -d "$json" "${BASE}${url}" || true)"
  if [[ "$code" != "200" ]]; then
    echo "FAIL [$name] POST $url -> HTTP $code"
    head -c 320 "$body"; echo
    rm -f "$body"
    return 1
  fi
  if ! jq -e "$jq_expr" "$body" >/dev/null 2>&1; then
    echo "FAIL [$name] jq validation failed: $jq_expr"
    head -c 420 "$body"; echo
    rm -f "$body"
    return 1
  fi
  echo "PASS [$name] POST $url"
  rm -f "$body"
}

# Tabs/pages
run_page "tab-home" "/" "Market Overview|OpenBB"
run_page "tab-news" "/news" "Headlines|News"
run_page "tab-screener" "/screener" "Screener"
run_page "tab-social" "/social" "Social Sentiment"
run_page "tab-watchlist" "/watchlist" "Watchlist"
run_page "tab-etfs" "/etfs" "Popular ETFs|Top ETFs"
run_page "tab-technical" "/technical" "Technical View|Technical"
run_page "tab-charts" "/charts" "Chart Board|No charts yet"

# Shared API functions
run_get "market-overview" "/api/market/overview" '(.gainers|type=="array") and (.losers|type=="array") and (.active|type=="array") and (.summary.gainers_count>=0)'
run_get "after-hours" "/api/market/after-hours" '(.rows|type=="array") and (.count>=0)'
run_get "alerts" "/api/alerts" '(.rows|type=="array") and (.count>=0)'
run_get "market-indicators" "/api/market/indicators" '(.headline.sentiment_score|type=="number") and (.earnings.next_events|type=="array")'
run_get "news-headlines" "/api/news/headlines?limit=20" '(.rows|type=="array") and (.count>=0)'
run_get "watchlist-quote" "/api/watchlist/quote?symbols=AAPL,MSFT,NVDA" '(.quotes|type=="array") and (.count>=0)'
run_get "etfs-popular" "/api/etfs/popular?limit=10" '(.rows|type=="array") and (.count>=0)'
run_get "social-sentiment" "/api/social/sentiment?limit=20" '(.rows|type=="array") and (.count>=0)'
run_get "security-status" "/api/security/status" '(.market_data.provider|type=="string")'
run_post "screener-default" "/api/screener" '{"limit":40}' '(.rows|type=="array") and (.count>=0) and (.universe_count>=0)'
run_get "screener-top-volume" "/api/screener/top-volume-triggers?limit=20&universe_size=100" '(.rows|type=="array") and (.checked_count>=0)'

# Chart/ticker data paths (same endpoints used by chart board + ticker detail)
for sym in AAPL MSFT NVDA TSLA SPY CHPT CLOV UPST KOSS; do
  run_get "ticker-quote-$sym" "/api/ticker/$sym/quote" '(.symbol|type=="string")'
  run_get "ticker-historical-$sym" "/api/ticker/$sym/historical?range=1Y" '(.data|type=="array") and (.rows>50)'
  run_get "ticker-technicals-$sym" "/api/ticker/$sym/technicals?range=1Y" '(.latest.close|type=="number")'
done

# Invalid symbol should return structured non-200 (not transport failure)
bad_body="$(mktemp)"
bad_code="$(curl -sS -m 20 -o "$bad_body" -w '%{http_code}' "${BASE}/api/ticker/INVALID%25/historical?range=1Y" || true)"
if [[ "$bad_code" == "400" || "$bad_code" == "404" || "$bad_code" == "422" ]]; then
  echo "PASS [ticker-invalid-symbol] expected non-200 HTTP $bad_code"
else
  echo "FAIL [ticker-invalid-symbol] unexpected HTTP $bad_code"
  head -c 320 "$bad_body"; echo
  rm -f "$bad_body"
  exit 1
fi
rm -f "$bad_body"

echo "ALL_SMOKE_TESTS_PASSED"
