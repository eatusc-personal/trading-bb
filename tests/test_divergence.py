"""Unit tests for the divergence scanner's pure math and gating logic.

Everything runs on synthetic data: the full-scan test monkeypatches the
module's history fetcher, so no network or provider keys are needed.
"""

import pandas as pd
import pytest

from python_api import divergence
from python_api.divergence import (
    _direction_label,
    _flat_threshold_pct,
    _robust_z,
    _window_return,
)

# ---------------------------------------------------------------------------
# _robust_z
# ---------------------------------------------------------------------------


def test_robust_z_known_values():
    # median 3.0, MAD 1.0 -> scale = 1.4826
    peers = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _robust_z(3.0, peers) == pytest.approx(0.0)
    assert _robust_z(6.0, peers) == pytest.approx(3.0 / 1.4826)
    assert _robust_z(0.0, peers) == pytest.approx(-3.0 / 1.4826)


def test_robust_z_requires_at_least_three_peers():
    assert _robust_z(1.0, []) is None
    assert _robust_z(1.0, [1.0]) is None
    assert _robust_z(1.0, [1.0, 2.0]) is None


def test_robust_z_mad_degenerate_falls_back_to_std():
    # median 1.0 but MAD 0 (a majority of identical values); falls back to the
    # population std of the peers instead of returning None.
    peers = [1.0, 1.0, 1.0, 1.0, 10.0]
    std = float(pd.Series(peers).std(ddof=0))  # 3.6
    assert std == pytest.approx(3.6)
    assert _robust_z(8.2, peers) == pytest.approx((8.2 - 1.0) / std)


def test_robust_z_all_identical_peers_returns_none():
    assert _robust_z(5.0, [2.0, 2.0, 2.0]) is None


def test_robust_z_is_clamped_to_15():
    # Tiny but non-degenerate spread -> raw z would be in the thousands.
    peers = [1.0, 1.001, 0.999]
    assert _robust_z(1000.0, peers) == 15.0
    assert _robust_z(-1000.0, peers) == -15.0


# ---------------------------------------------------------------------------
# _window_return
# ---------------------------------------------------------------------------


def test_window_return_simple():
    series = pd.Series([100.0, 102.0, 110.0])
    assert _window_return(series, 1) == pytest.approx((110.0 / 102.0 - 1) * 100)
    assert _window_return(series, 2) == pytest.approx(10.0)


def test_window_return_insufficient_data():
    assert _window_return(pd.Series([100.0, 110.0]), 2) is None
    assert _window_return(pd.Series([], dtype=float), 1) is None


def test_window_return_zero_base():
    assert _window_return(pd.Series([0.0, 110.0]), 1) is None


def test_window_return_skips_nans():
    series = pd.Series([100.0, float("nan"), 110.0])
    assert _window_return(series, 1) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Flat thresholds and labels
# ---------------------------------------------------------------------------


def test_flat_threshold_scales_with_window():
    assert _flat_threshold_pct(1) == pytest.approx(0.5)
    assert _flat_threshold_pct(4) == pytest.approx(1.0)
    assert _flat_threshold_pct(20) == pytest.approx(0.5 * 20**0.5)


def test_direction_labels_cover_all_branches():
    # Group fell (<= -0.5% for a 1-day window)
    assert _direction_label(-2.0, 1.5, 1) == "Held up while group fell"
    assert _direction_label(-2.0, -1.5, 1) == "Fell harder than group"
    # Group rallied (>= +0.5%)
    assert _direction_label(2.0, 1.5, 1) == "Led group rally"
    assert _direction_label(2.0, -1.5, 1) == "Lagged group rally"
    # Flat group
    assert _direction_label(0.1, 3.0, 1) == "Moved independently of flat group"
    # Boundary: exactly at the flat threshold counts as a move
    assert _direction_label(-0.5, 1.0, 1) == "Held up while group fell"
    assert _direction_label(0.5, -1.0, 1) == "Lagged group rally"


# ---------------------------------------------------------------------------
# Correlation/beta gating in a full scan (synthetic history, no network)
# ---------------------------------------------------------------------------


def _closes(returns: list[float], start: float = 100.0) -> list[float]:
    out, price = [], start
    for r in returns:
        price *= 1 + r
        out.append(price)
    return out


def _synthetic_frames() -> dict[str, pd.DataFrame]:
    """Four members over 70 sessions.

    AAA/BBB/CCC follow the same alternating +1%/-1% driver; DDD moves exactly
    opposite (perfectly anti-correlated with its peers). On the final day the
    group rallies (+1.0/+1.2/+0.8) while CCC drops 4%.
    """
    n = 70
    dates = pd.bdate_range("2026-01-05", periods=n)

    def rets(sign: int, last: float) -> list[float]:
        r = [sign * (0.01 if i % 2 == 0 else -0.01) for i in range(n)]
        r[-1] = last
        return r

    data = {
        "AAA": rets(1, 0.010),
        "BBB": rets(1, 0.012),
        "CCC": rets(1, -0.04),
        "DDD": rets(-1, 0.008),
    }
    return {sym: pd.DataFrame({"close": _closes(r)}, index=dates) for sym, r in data.items()}


def test_scan_flags_correlated_diverger_and_gates_uncorrelated(monkeypatch):
    frames = _synthetic_frames()
    monkeypatch.setattr(divergence, "_fetch_history", lambda sym: frames[sym])

    result = divergence._scan_one_set(
        "test",
        "Test Set",
        ["AAA", "BBB", "CCC", "DDD"],
        benchmark=None,
        windows=[1],
        z_threshold=2.0,
        min_corr=0.30,
    )

    assert result["members_scanned"] == 4
    rows = {r["symbol"]: r for r in result["rows"]}

    # CCC normally tracks the group (60d corr well above the gate) and broke
    # away on the last day: it must be a confirmed diverger.
    ccc = rows["CCC"]
    assert ccc["peer_corr_60d"] >= 0.30
    assert ccc["is_diverger"] is True
    assert "CCC" in result["divergers"]
    sig = ccc["signals"][0]
    assert sig["confirmed"] is True
    # peers [1.0, 1.2, 0.8]: median 1.0, MAD 0.2 -> raw z ~ -16.9, clamped
    assert sig["z"] == pytest.approx(-15.0)
    # group median (+0.9%) is above the 1-day flat threshold (0.5%)
    assert sig["label"] == "Lagged group rally"

    # DDD is perfectly anti-correlated with the group: reported, never flagged.
    ddd = rows["DDD"]
    assert ddd["peer_corr_60d"] < 0.30
    assert ddd["low_correlation"] is True
    assert ddd["is_diverger"] is False

    # Members that moved with the group don't trigger.
    assert rows["AAA"]["is_diverger"] is False
    assert rows["BBB"]["is_diverger"] is False

    # Rows are sorted by |z| so the diverger leads the table.
    assert result["rows"][0]["symbol"] == "CCC"


def test_scan_uncorrelated_signal_is_reported_but_unconfirmed(monkeypatch):
    """Give the anti-correlated member the outlier move: its z-score still
    appears, but the signal stays unconfirmed and it is not a diverger."""
    frames = _synthetic_frames()
    # Rebuild DDD with the last-day crash instead of CCC.
    n = 70
    dates = pd.bdate_range("2026-01-05", periods=n)
    ddd_rets = [-(0.01 if i % 2 == 0 else -0.01) for i in range(n)]
    ddd_rets[-1] = -0.04
    ccc_rets = [0.01 if i % 2 == 0 else -0.01 for i in range(n)]
    ccc_rets[-1] = 0.008
    frames["DDD"] = pd.DataFrame({"close": _closes(ddd_rets)}, index=dates)
    frames["CCC"] = pd.DataFrame({"close": _closes(ccc_rets)}, index=dates)
    monkeypatch.setattr(divergence, "_fetch_history", lambda sym: frames[sym])

    result = divergence._scan_one_set(
        "test",
        "Test Set",
        ["AAA", "BBB", "CCC", "DDD"],
        benchmark=None,
        windows=[1],
        z_threshold=2.0,
        min_corr=0.30,
    )
    rows = {r["symbol"]: r for r in result["rows"]}
    ddd = rows["DDD"]
    assert ddd["low_correlation"] is True
    assert ddd["signals"], "outlier move should still be reported as a signal"
    assert all(sig["confirmed"] is False for sig in ddd["signals"])
    assert ddd["is_diverger"] is False
    assert result["divergers"] == []


def test_scan_errors_when_too_few_members_have_history(monkeypatch):
    frames = _synthetic_frames()

    def fetch(sym):
        if sym in ("CCC", "DDD"):
            raise RuntimeError("no data")
        return frames[sym]

    monkeypatch.setattr(divergence, "_fetch_history", fetch)
    result = divergence._scan_one_set(
        "test", "Test Set", ["AAA", "BBB", "CCC", "DDD"], None, [1], 2.0, 0.30
    )
    assert result["rows"] == []
    assert "error" in result
    assert set(result["fetch_errors"]) == {"CCC", "DDD"}
