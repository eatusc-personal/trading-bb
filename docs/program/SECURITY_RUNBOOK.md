# Security Hardening Runbook (Phase 6)

## Scope
- CORS origin restrictions
- Optional social API authentication
- API and social endpoint rate limits

## Environment Configuration

```bash
export CORS_ALLOW_ORIGINS="http://localhost:3000,http://127.0.0.1:3000"
export SOCIAL_API_KEY="replace_with_strong_secret"
export API_RATE_LIMIT_WINDOW_SEC="60"
export API_RATE_LIMIT_MAX_REQUESTS="300"
export SOCIAL_RATE_LIMIT_WINDOW_SEC="60"
export SOCIAL_RATE_LIMIT_MAX_REQUESTS="120"
```

Notes:
- If `SOCIAL_API_KEY` is unset, social endpoints remain open (local-dev default).
- Next API proxy forwards `SOCIAL_API_KEY` automatically when configured.

## Verification

1. Health and security status

```bash
curl -s 'http://127.0.0.1:8001/api/health'
curl -s 'http://127.0.0.1:3000/api/security/status' | head -c 500
```

2. Social auth enforcement (when `SOCIAL_API_KEY` is set)

```bash
curl -i 'http://127.0.0.1:8001/api/social/sentiment?limit=5'
curl -i -H "x-api-key: $SOCIAL_API_KEY" 'http://127.0.0.1:8001/api/social/sentiment?limit=5'
```

3. Rate limit behavior

```bash
for i in {1..200}; do curl -s -o /dev/null -w "%{http_code}\n" 'http://127.0.0.1:3000/api/social/sentiment?limit=5'; done | tail -n 20
```

Expected:
- Burst requests eventually return `429` with `Retry-After`.

4. Automated phase check

```bash
python3 scripts/phase6_security_check.py --web-base http://127.0.0.1:3000 --api-base http://127.0.0.1:8001 --api-key "$SOCIAL_API_KEY" --burst 135 --timeout 120
```

Expected:
- Script exits `0` and prints `phase6_security_check ok=true`.

## Incident Response
- If false positives in rate limiting occur locally:
  - increase `*_MAX_REQUESTS` or `*_WINDOW_SEC`.
- If CORS blocks expected UI origin:
  - add exact origin to `CORS_ALLOW_ORIGINS`.
- If auth misconfigured:
  - verify `SOCIAL_API_KEY` value in both Python API and Next env.

## Tabletop Checklist Evidence (2026-03-18)
- Auth misconfig scenario:
  - Verified direct social endpoint returns `401` without key and `200` with `x-api-key`.
  - Verified Next proxied social endpoint returns `200` with server-side key forwarding.
- CORS mismatch scenario:
  - Verified `GET /api/security/status` exposes active allowlist for quick diagnosis.
  - Confirmed remediation path by adding/removing exact origin in `CORS_ALLOW_ORIGINS`.
- Rate-limit false-positive scenario:
  - Verified sustained burst returns `429` and includes `Retry-After`.
  - Confirmed threshold tuning path via `API_*` and `SOCIAL_*` rate-limit env vars.
