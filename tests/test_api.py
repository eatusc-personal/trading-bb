"""Endpoint smoke tests via FastAPI TestClient.

Covers the routes that don't need external market data — health, the security
status report, and input validation — so the suite runs offline. Importing
python_api.main pulls in the full app (OpenBB), so this file is a little heavier
than the pure-unit tests.
"""

import pytest
from fastapi.testclient import TestClient

from python_api.main import app

client = TestClient(app)


def test_health_ok():
    res = client.get("/api/health")
    assert res.status_code == 200
    assert isinstance(res.json(), dict)


def test_security_status_ok():
    res = client.get("/api/security/status")
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body, dict)


@pytest.mark.parametrize("bad", ["@@@", "toolongsymbolname", "../etc", "a b"])
def test_invalid_symbol_is_rejected(bad):
    res = client.get(f"/api/ticker/{bad}/quote")
    assert res.status_code in (400, 404, 422)
