"""Unit tests for the per-provider request budget guard (no network, no DB)."""

from python_api import provider_budget as pb


def test_known_provider_default_limits():
    per_min, per_day = pb._limits_for("twelvedata")
    assert per_min == 8  # documented free-tier default


def test_env_override_of_per_minute_limit(monkeypatch):
    monkeypatch.setenv("BUDGET_TESTPROV_PER_MIN", "3")
    per_min, _ = pb._limits_for("testprov")
    assert per_min == 3


def test_unknown_provider_is_unlimited_by_default():
    per_min, per_day = pb._limits_for("nosuchprovider")
    assert (per_min, per_day) == (0, 0)  # 0 = unlimited


def test_per_minute_budget_denies_once_exhausted(monkeypatch):
    monkeypatch.setenv("BUDGET_TESTDENY_PER_MIN", "2")
    assert pb.try_consume("testdeny")[0] is True
    assert pb.try_consume("testdeny")[0] is True
    ok, reason = pb.try_consume("testdeny")
    assert ok is False
    assert "per-minute" in reason
