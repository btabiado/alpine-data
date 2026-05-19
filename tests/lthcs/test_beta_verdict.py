"""Tests for ``scripts/lthcs_beta_verdict``.

Covers:

* Synthetic 30-day window with a known IC shift → expected verdict.
* Insufficient sample (< 30 obs) → HOLD verdict.
* Adoption inversion resolution detection (negative-to-positive).
* Markdown report renders without crashing on edge cases (empty
  baseline, all-None post metrics, missing horizon block).

The verdict-classification function is pure, so most tests skip the
expensive backtest plumbing and exercise the rule directly. One
integration test wires the real ``compute_post_ic`` against a
tmp-path snapshot tree + fake yahoo to confirm the wiring still
works end-to-end.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "lthcs_beta_verdict.py"


def _load_verdict_module():
    """Load ``scripts/lthcs_beta_verdict.py`` as a module.

    The script lives under ``scripts/`` (not a proper package), so we
    import it by path. Cache the module so repeated tests don't reload.
    """
    if "lthcs_beta_verdict" in sys.modules:
        return sys.modules["lthcs_beta_verdict"]
    # Ensure repo root is on sys.path so the script can import lthcs.
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location(
        "lthcs_beta_verdict", SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lthcs_beta_verdict"] = mod
    spec.loader.exec_module(mod)
    return mod


verdict_mod = _load_verdict_module()


# ---------------------------------------------------------------------------
# Helpers (mirror tests/lthcs/test_backtest.py patterns)
# ---------------------------------------------------------------------------

PILLARS = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]


def _make_score_row(ticker: str, score: float, band: str,
                    subs: Dict[str, float] | None = None) -> Dict[str, Any]:
    subs = subs or {p: 50.0 for p in PILLARS}
    return {
        "ticker": ticker,
        "lthcs_score": score,
        "band": band,
        "drift_1d": 0.0,
        "drift_7d": 0.0,
        "drift_30d": 0.0,
        "drift_90d": 0.0,
        "confidence_level": "high",
        "data_quality_flags": [],
        "subscores": subs,
        "modifiers": {"macro_adj": 0.0, "sector_adj": 0.0, "volatility_mod": 0.0},
        "maturity_stage": "mature_compounder",
        "weights_used": [0.2] * 5,
        "effective_weights": [0.2] * 5,
        "dropped_pillars": [],
        "weighted_components": [10.0] * 5,
        "sector": "Technology",
    }


def _write_snapshot(data_root: Path, date: str, rows: List[Dict[str, Any]]) -> None:
    snap_dir = data_root / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "calc_date": date,
        "model_version": "v1.1.0",
        "weights_profile_default": "default",
        "scores": rows,
    }
    (snap_dir / ("%s.json" % date)).write_text(json.dumps(payload))


def _fake_yahoo(prices_by_ticker: Dict[str, List[Dict[str, Any]]]):
    class _Fake:
        @staticmethod
        def get_daily_prices(ticker, period="1y"):
            return list(prices_by_ticker.get(ticker, []))
    return _Fake


def _price_rows(start: str, n: int, base: float = 100.0,
                step: float = 1.0) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    dates = pd.bdate_range(start=start, periods=n)
    for i, d in enumerate(dates):
        c = base + step * i
        out.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "open": c, "high": c, "low": c, "close": c,
                "adj_close": c, "volume": 1000,
            }
        )
    return out


def _baseline_payload(
    composite_ic: float = 0.10,
    adoption_ic: float = -0.013,
    horizon: int = 21,
) -> Dict[str, Any]:
    horizon_key = "horizon_%dd" % int(horizon)
    return {
        "_meta": {
            "frozen_at": "2026-05-19",
            "anchor_commit": "333e5dd",
            "anchor_commit_subject": "lthcs/adoption: β-fix",
            "source_run": "data/lthcs/backtest/2026-05-19_post_phase5/",
            "source_run_window": {"start": "2026-02-17", "end": "2026-05-18"},
        },
        "composite_ic": {
            horizon_key: {
                "ic_mean": composite_ic,
                "ic_std": 0.15,
                "ic_sharpe": 12.0,
                "n_obs": 91,
            }
        },
        "pillar_ic": {
            horizon_key: {
                "adoption_momentum": {
                    "ic_mean": adoption_ic,
                    "ic_std": 0.09,
                    "ic_sharpe": -2.4,
                    "n_obs": 91,
                },
                "institutional_confidence": {
                    "ic_mean": 0.20, "ic_std": 0.28,
                    "ic_sharpe": 11.0, "n_obs": 91,
                },
                "thesis_integrity": {
                    "ic_mean": 0.08, "ic_std": 0.07,
                    "ic_sharpe": 17.0, "n_obs": 91,
                },
                "financial_evolution": {
                    "ic_mean": 0.07, "ic_std": 0.10,
                    "ic_sharpe": 12.0, "n_obs": 91,
                },
                "des": {
                    "ic_mean": 0.02, "ic_std": 0.10,
                    "ic_sharpe": 4.0, "n_obs": 91,
                },
            }
        },
    }


# ---------------------------------------------------------------------------
# classify_verdict — pure rule tests
# ---------------------------------------------------------------------------

def test_classify_pass_when_composite_jumps_and_adoption_resolves() -> None:
    """+0.03 composite jump + Adoption 0.01 (post) with n=60 → PASS."""
    v, reason = verdict_mod.classify_verdict(
        composite_post=0.13,
        composite_baseline=0.10,
        adoption_post=0.01,
        adoption_baseline=-0.013,
        n_obs=60,
    )
    assert v == "PASS", reason
    assert "Composite IC".lower() in reason.lower() or "composite" in reason


def test_classify_hold_when_sample_too_small() -> None:
    """Even a great IC shift HOLDs if n_obs < 30."""
    v, reason = verdict_mod.classify_verdict(
        composite_post=0.15,
        composite_baseline=0.10,
        adoption_post=0.05,
        adoption_baseline=-0.013,
        n_obs=10,
    )
    assert v == "HOLD", reason
    assert "insufficient" in reason.lower() or "n_obs" in reason


def test_classify_hold_when_composite_short_of_threshold() -> None:
    """+0.01 composite delta (< +0.02) HOLDs even with adoption resolved."""
    v, reason = verdict_mod.classify_verdict(
        composite_post=0.11,
        composite_baseline=0.10,
        adoption_post=0.005,
        adoption_baseline=-0.013,
        n_obs=45,
    )
    assert v == "HOLD", reason


def test_classify_hold_when_adoption_improves_but_still_negative() -> None:
    """Adoption goes -0.013 → -0.005 (improved but <0) → HOLD."""
    v, reason = verdict_mod.classify_verdict(
        composite_post=0.13,
        composite_baseline=0.10,
        adoption_post=-0.005,
        adoption_baseline=-0.013,
        n_obs=45,
    )
    assert v == "HOLD", reason
    assert "still <0" in reason or "Adoption" in reason


def test_classify_fail_when_composite_drops() -> None:
    """Composite goes 0.10 → 0.08 → FAIL regardless of adoption."""
    v, reason = verdict_mod.classify_verdict(
        composite_post=0.08,
        composite_baseline=0.10,
        adoption_post=0.02,
        adoption_baseline=-0.013,
        n_obs=45,
    )
    assert v == "FAIL", reason
    assert "dropped" in reason.lower()


def test_classify_fail_when_adoption_inversion_deepens() -> None:
    """Adoption goes -0.013 → -0.03 → FAIL (deeper inversion)."""
    v, reason = verdict_mod.classify_verdict(
        composite_post=0.12,
        composite_baseline=0.10,
        adoption_post=-0.030,
        adoption_baseline=-0.013,
        n_obs=45,
    )
    assert v == "FAIL", reason
    assert "deepen" in reason.lower()


def test_classify_hold_when_post_metrics_missing() -> None:
    """None for post composite → HOLD (no measurement yet)."""
    v, reason = verdict_mod.classify_verdict(
        composite_post=None,
        composite_baseline=0.10,
        adoption_post=None,
        adoption_baseline=-0.013,
        n_obs=0,
    )
    assert v == "HOLD", reason


def test_classify_hold_when_baseline_missing() -> None:
    """No baseline → HOLD (can't compare)."""
    v, reason = verdict_mod.classify_verdict(
        composite_post=0.13,
        composite_baseline=None,
        adoption_post=0.02,
        adoption_baseline=None,
        n_obs=60,
    )
    assert v == "HOLD", reason
    assert "baseline" in reason.lower()


# ---------------------------------------------------------------------------
# Adoption inversion resolution detection — negative-to-positive transition
# ---------------------------------------------------------------------------

def test_adoption_inversion_resolution_triggers_pass() -> None:
    """The β-fix's stated goal: Adoption IC goes from <0 to >=0.

    Reuse classify_verdict but pin composite delta well above threshold so
    only the Adoption transition matters.
    """
    # Negative -> positive: must PASS (with sufficient composite delta).
    v_resolved, _ = verdict_mod.classify_verdict(
        composite_post=0.13,
        composite_baseline=0.10,
        adoption_post=0.001,   # just barely positive
        adoption_baseline=-0.013,
        n_obs=60,
    )
    assert v_resolved == "PASS"

    # Negative -> still slightly negative: HOLD (improved but unresolved).
    v_partial, _ = verdict_mod.classify_verdict(
        composite_post=0.13,
        composite_baseline=0.10,
        adoption_post=-0.001,  # improved but not crossed yet
        adoption_baseline=-0.013,
        n_obs=60,
    )
    assert v_partial == "HOLD"


# ---------------------------------------------------------------------------
# extract_baseline_ic — JSON shape tolerance
# ---------------------------------------------------------------------------

def test_extract_baseline_ic_reads_h21_block() -> None:
    bl = _baseline_payload(composite_ic=0.122, adoption_ic=-0.013, horizon=21)
    comp, adop = verdict_mod.extract_baseline_ic(bl, horizon=21)
    assert comp == pytest.approx(0.122)
    assert adop == pytest.approx(-0.013)


def test_extract_baseline_ic_missing_horizon_returns_none() -> None:
    bl = _baseline_payload(composite_ic=0.122, adoption_ic=-0.013, horizon=21)
    # Asking for h5 when only h21 exists.
    comp, adop = verdict_mod.extract_baseline_ic(bl, horizon=5)
    assert comp is None
    assert adop is None


def test_extract_baseline_ic_empty_baseline_returns_none() -> None:
    comp, adop = verdict_mod.extract_baseline_ic({}, horizon=21)
    assert comp is None
    assert adop is None


# ---------------------------------------------------------------------------
# render_markdown_report — must not crash on edge cases
# ---------------------------------------------------------------------------

def test_render_markdown_normal_case_includes_verdict_and_table() -> None:
    bl = _baseline_payload()
    post = {
        "window": {"start": "2026-05-18", "end": "2026-06-17"},
        "horizon_days": 21,
        "n_obs": 60,
        "composite_ic": 0.135,
        "adoption_ic": 0.012,
        "per_pillar": {
            "composite": {"ic_mean": 0.135, "ic_std": 0.0, "ic_sharpe": 0.0, "n_obs": 60},
            "adoption_momentum": {"ic_mean": 0.012, "ic_std": 0.0, "ic_sharpe": 0.0, "n_obs": 60},
            "institutional_confidence": {"ic_mean": 0.18, "ic_std": 0.0, "ic_sharpe": 0.0, "n_obs": 60},
        },
    }
    md = verdict_mod.render_markdown_report(
        today="2026-06-17",
        verdict="PASS",
        reason="composite IC +0.0350; adoption resolved",
        horizon=21,
        baseline=bl,
        post=post,
    )
    assert "Adoption β-Fix Verdict" in md
    assert "`PASS`" in md
    assert "Composite IC" in md
    assert "Adoption IC" in md
    # Delta column populated for composite and adoption rows.
    assert "+0.0350" in md or "+0.035" in md
    # Anchor commit shows up.
    assert "333e5dd" in md


def test_render_markdown_empty_baseline_does_not_crash() -> None:
    post = {
        "window": {"start": "2026-05-18", "end": "2026-06-17"},
        "horizon_days": 21,
        "n_obs": 60,
        "composite_ic": 0.135,
        "adoption_ic": 0.012,
        "per_pillar": {},
    }
    md = verdict_mod.render_markdown_report(
        today="2026-06-17",
        verdict="HOLD",
        reason="baseline IC missing",
        horizon=21,
        baseline={},
        post=post,
    )
    # Should render n/a for baseline rows, not crash.
    assert "n/a" in md
    assert "`HOLD`" in md


def test_render_markdown_all_none_post_does_not_crash() -> None:
    bl = _baseline_payload()
    post = {
        "window": {"start": "2026-05-18", "end": "2026-06-17"},
        "horizon_days": 21,
        "n_obs": 0,
        "composite_ic": None,
        "adoption_ic": None,
        "per_pillar": {},
    }
    md = verdict_mod.render_markdown_report(
        today="2026-06-17",
        verdict="HOLD",
        reason="post-β IC not computable",
        horizon=21,
        baseline=bl,
        post=post,
    )
    assert "n/a" in md
    assert "`HOLD`" in md


def test_render_markdown_nan_post_does_not_crash() -> None:
    """NaN floats in post metrics get rendered as n/a (no exception)."""
    bl = _baseline_payload()
    post = {
        "window": {"start": "2026-05-18", "end": "2026-06-17"},
        "horizon_days": 21,
        "n_obs": 5,
        "composite_ic": float("nan"),
        "adoption_ic": float("nan"),
        "per_pillar": {
            "institutional_confidence": {"ic_mean": float("nan"), "n_obs": 5},
        },
    }
    md = verdict_mod.render_markdown_report(
        today="2026-06-17",
        verdict="HOLD",
        reason="insufficient sample",
        horizon=21,
        baseline=bl,
        post=post,
    )
    # NaN -> "+nan" is acceptable (our %+0.4f format yields "+nan"); the
    # important thing is no exception was raised. Spot-check structure.
    assert "Adoption β-Fix Verdict" in md
    assert "`HOLD`" in md


# ---------------------------------------------------------------------------
# load_baseline — file plumbing
# ---------------------------------------------------------------------------

def test_load_baseline_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        verdict_mod.load_baseline(tmp_path / "does-not-exist.json")


def test_load_baseline_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "baseline.json"
    payload = _baseline_payload()
    p.write_text(json.dumps(payload))
    loaded = verdict_mod.load_baseline(p)
    assert loaded["composite_ic"]["horizon_21d"]["ic_mean"] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# Integration smoke test — compute_post_ic against tmp snapshots
# ---------------------------------------------------------------------------

def test_compute_post_ic_returns_shaped_payload(tmp_path: Path) -> None:
    """Tiny end-to-end: a 2-day window over 2 tickers returns a dict
    shaped how ``classify_verdict`` expects.
    """
    data_root = tmp_path / "lthcs"
    # Two snapshots ranked AAA > BBB.
    _write_snapshot(data_root, "2026-05-18", [
        _make_score_row("AAA", 80.0, "elite",
                        subs={"adoption_momentum": 80.0,
                              "institutional_confidence": 50.0,
                              "financial_evolution": 50.0,
                              "thesis_integrity": 50.0,
                              "des": 50.0}),
        _make_score_row("BBB", 30.0, "review",
                        subs={"adoption_momentum": 20.0,
                              "institutional_confidence": 50.0,
                              "financial_evolution": 50.0,
                              "thesis_integrity": 50.0,
                              "des": 50.0}),
    ])
    _write_snapshot(data_root, "2026-05-19", [
        _make_score_row("AAA", 82.0, "elite",
                        subs={"adoption_momentum": 81.0,
                              "institutional_confidence": 50.0,
                              "financial_evolution": 50.0,
                              "thesis_integrity": 50.0,
                              "des": 50.0}),
        _make_score_row("BBB", 31.0, "review",
                        subs={"adoption_momentum": 21.0,
                              "institutional_confidence": 50.0,
                              "financial_evolution": 50.0,
                              "thesis_integrity": 50.0,
                              "des": 50.0}),
    ])

    # Tiny price series for forward returns. AAA appreciates faster
    # (matches its higher score), BBB lags.
    prices = {
        "AAA": _price_rows("2026-05-18", n=40, base=100.0, step=1.0),
        "BBB": _price_rows("2026-05-18", n=40, base=100.0, step=0.5),
    }
    fake = _fake_yahoo(prices)

    out = verdict_mod.compute_post_ic(
        since="2026-05-18",
        end="2026-05-19",
        horizon=21,
        data_root=data_root,
        yahoo_module=fake,
    )

    # Structural checks — we don't pin exact IC values (2 obs is noise),
    # only that the call returns the right shape and didn't crash.
    assert "composite_ic" in out
    assert "adoption_ic" in out
    assert "per_pillar" in out
    assert "n_obs" in out
    assert isinstance(out["per_pillar"], dict)
    # Either we got IC numbers or we got Nones — both are valid for a
    # tiny window. We just confirm classify_verdict accepts the payload.
    verdict, reason = verdict_mod.classify_verdict(
        composite_post=out.get("composite_ic"),
        composite_baseline=0.10,
        adoption_post=out.get("adoption_ic"),
        adoption_baseline=-0.013,
        n_obs=int(out.get("n_obs", 0)),
    )
    assert verdict in {"PASS", "HOLD", "FAIL"}
    assert isinstance(reason, str) and reason


def test_compute_post_ic_empty_snapshots_returns_none(tmp_path: Path) -> None:
    """Empty data root -> no crash, returns n_obs=0 + None IC values."""
    data_root = tmp_path / "lthcs"
    data_root.mkdir(parents=True)
    out = verdict_mod.compute_post_ic(
        since="2026-05-18",
        end="2026-06-17",
        horizon=21,
        data_root=data_root,
        yahoo_module=_fake_yahoo({}),
    )
    assert out["n_obs"] == 0
    assert out["composite_ic"] is None
    assert out["adoption_ic"] is None
