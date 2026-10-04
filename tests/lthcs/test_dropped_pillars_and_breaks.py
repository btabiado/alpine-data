"""LTHCS placeholders shown as real, and the 2026-10-04 methodology break.

Audit 2026-10-04:
* thesis was `thesis_unavailable` on 215/215 tickers (dropped from the
  composite) yet its stub values (55.0 / 58.8 / 50.0) appeared in pillar
  bars, mover subscores and the "Thesis pillar avg 54.3" card;
* crypto Adoption Momentum was a neutral 50.0 for 9/10 coins at full weight,
  and DES was the identical 56.9 for all 10 (market-wide inputs only);
* every LLM narrative was the templated fallback (missing_api_key) and said
  "no prior snapshot available" although prior snapshots exist;
* SEC data restored the financial pillar on 2026-10-04: 73 tickers moved
  >10 pts and "98 tickers shifted band overnight" — not a market signal.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from lthcs import methodology, narratives, narratives_llm, score
from lthcs.index_aggregate import compute_lthcs_index
from lthcs.persist import LthcsPersist

REPO_ROOT = Path(__file__).resolve().parents[2]
WEIGHTS = json.loads((REPO_ROOT / "data" / "lthcs" / "weights.json").read_text())


# ---- equity: dropped pillar is null, composite unchanged -----------------

def _subs(**over):
    base = {"adoption_momentum": 40.0, "institutional_confidence": 70.0,
            "financial_evolution": 65.0, "thesis_integrity": 55.0, "des": 40.0}
    base.update(over)
    return base


def test_dropped_thesis_is_published_as_null():
    row = score.compute_lthcs_score(
        "AAPL", "Technology", "mature_compounder", _subs(), WEIGHTS,
        data_quality_flags=["thesis_unavailable"])
    assert row["dropped_pillars"] == ["thesis_integrity"]
    assert row["subscores"]["thesis_integrity"] is None
    assert row["subscores"]["financial_evolution"] == 65.0
    # The stub never influenced the composite: changing it changes nothing.
    other = score.compute_lthcs_score(
        "AAPL", "Technology", "mature_compounder", _subs(thesis_integrity=99.0),
        WEIGHTS, data_quality_flags=["thesis_unavailable"])
    assert other["lthcs_score"] == row["lthcs_score"]


def test_undropped_pillar_keeps_its_value():
    row = score.compute_lthcs_score(
        "AAPL", "Technology", "mature_compounder", _subs(), WEIGHTS)
    assert row["subscores"]["thesis_integrity"] == 55.0


def _snap_row(ticker, thesis_stub, dropped=True, fin=60.0):
    return {"ticker": ticker, "band": "monitor", "lthcs_score": 55.0,
            "subscores": {"adoption_momentum": 50.0, "institutional_confidence": 50.0,
                          "financial_evolution": fin, "thesis_integrity": thesis_stub,
                          "des": 50.0},
            "dropped_pillars": ["thesis_integrity"] if dropped else []}


def test_pillar_avg_card_is_na_when_pillar_dropped_everywhere():
    # Mix of pre-fix stub numbers and post-fix nulls: both must be ignored.
    rows = [_snap_row("A", 55.0), _snap_row("B", 58.8), _snap_row("C", None)]
    idx = compute_lthcs_index(rows, as_of="2026-10-04")
    thesis = next(c for c in idx["components"] if c["name"] == "Thesis pillar avg")
    assert thesis["value"] == "n/a" and thesis["delta"] == 0
    assert "dropped" in thesis["read"]
    fin = next(c for c in idx["components"] if c["name"] == "Financial pillar avg")
    assert fin["value"] == 60.0


def test_pillar_avg_uses_only_tickers_where_it_was_scored():
    rows = [_snap_row("A", 90.0, dropped=False), _snap_row("B", 10.0, dropped=True)]
    idx = compute_lthcs_index(rows, as_of="2026-10-04")
    thesis = next(c for c in idx["components"] if c["name"] == "Thesis pillar avg")
    assert thesis["value"] == 90.0


# ---- narratives: dropped pillar not ranked; fallback carries prior ---------

def test_template_never_cites_a_dropped_pillar():
    row = dict(_snap_row("AAPL", None), drift_1d=1.0, drift_30d=0.5,
               lthcs_score=51.8, band="weakening")
    row["subscores"]["institutional_confidence"] = 68.7
    text = json.dumps(narratives.generate_narratives(row))
    assert "Thesis Integrity" not in text


def test_llm_fallback_uses_the_prior_snapshot(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    today = dict(_snap_row("AAPL", None, fin=75.0), drift_1d=3.8, drift_30d=0.8,
                 lthcs_score=51.8, band="weakening")
    prior = dict(_snap_row("AAPL", None, fin=60.0), lthcs_score=48.0)
    out = narratives_llm.generate_llm_narrative("AAPL", today, [],
                                                prior_snapshot_row=prior)
    assert out["fallback"] is True and out["fallback_reason"] == "missing_api_key"
    assert "no prior snapshot" not in out["why_changed"]
    assert "Financial Evolution" in out["why_changed"]


def test_fallback_summary_and_persisted_top_level_flag(tmp_path):
    rows = [{"ticker": t, "fallback": True, "fallback_reason": "missing_api_key"}
            for t in ("A", "B")]
    summ = narratives_llm.fallback_summary(rows)
    assert summ == {"fallback": True, "fallback_count": 2, "llm_count": 0,
                    "fallback_reasons": {"missing_api_key": 2},
                    "narrative_source": "template_fallback"}
    p = LthcsPersist(data_root=tmp_path).write_narratives_llm(
        "2026-10-04", "some-model", rows, meta={"fallback_count": 2})
    payload = json.loads(p.read_text())
    assert payload["fallback"] is True
    assert payload["narrative_source"] == "template_fallback"
    mixed = narratives_llm.fallback_summary(rows + [{"ticker": "C", "fallback": False}])
    assert mixed["fallback"] is False and mixed["narrative_source"] == "mixed"


def test_llm_prompt_marks_dropped_pillar_na():
    msg = narratives_llm._format_subscores(
        {"adoption_momentum": 40.0, "thesis_integrity": None})
    assert "Thesis Integrity=n/a" in msg


# ---- crypto: placeholder pillars dropped and renormalised ------------------

def _crypto_mod():
    spec = importlib.util.spec_from_file_location(
        "lthcs_crypto_daily_t", REPO_ROOT / "scripts" / "lthcs_crypto_daily.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Adapter:
    def inputs_for(self, symbol):
        return {}


def _pillar(sub, dq):
    return lambda symbol, inputs: {"sub_score": sub, "components": {}, "data_quality": dq}


def test_crypto_drops_all_missing_adoption_and_marketwide_des(monkeypatch):
    cd = _crypto_mod()
    monkeypatch.setattr(cd, "compute_crypto_adoption",
                        _pillar(50.0, {"has_active_addresses": False, "has_tx_volume": False}))
    monkeypatch.setattr(cd, "compute_crypto_institutional",
                        _pillar(70.0, {"has_market": True}))
    monkeypatch.setattr(cd, "compute_crypto_financial",
                        _pillar(80.0, {"has_market": True}))
    monkeypatch.setattr(cd, "compute_crypto_thesis",
                        _pillar(50.0, {"has_funding": False, "has_long_short": False}))
    monkeypatch.setattr(cd, "compute_crypto_des",
                        _pillar(56.9, {"has_stablecoin": True, "has_exchange_reserves": False,
                                       "has_macro_overlay": False}))
    asset = {"symbol": "ETH", "weight_profile": "eth"}
    row = cd.score_asset(asset, _Adapter(), WEIGHTS, calc_date="2026-10-04")
    assert set(row["dropped_pillars"]) == {"adoption_momentum", "thesis_integrity", "des"}
    assert "adoption_unavailable" in row["data_quality_flags"]
    assert "des_asset_inputs_unavailable" in row["data_quality_flags"]
    assert row["subscores"]["adoption_momentum"] is None
    assert row["subscores"]["des"] is None
    assert row["subscores"]["financial_evolution"] == 80.0
    eff = dict(zip(score.PILLAR_ORDER, row["effective_weights"]))
    assert eff["adoption_momentum"] == eff["des"] == eff["thesis_integrity"] == 0.0
    assert abs(sum(row["effective_weights"]) - 1.0) < 1e-9
    # composite is a blend of the two measured pillars only
    assert 70.0 <= row["lthcs_score"] <= 80.0


def test_crypto_des_with_asset_specific_reserves_is_kept(monkeypatch):
    cd = _crypto_mod()
    for name, sub in (("compute_crypto_adoption", 60.0), ("compute_crypto_institutional", 70.0),
                      ("compute_crypto_financial", 80.0)):
        monkeypatch.setattr(cd, name, _pillar(sub, {"x": True}))
    monkeypatch.setattr(cd, "compute_crypto_thesis", _pillar(50.0, {"has_funding": False}))
    monkeypatch.setattr(cd, "compute_crypto_des",
                        _pillar(61.0, {"has_stablecoin": True, "has_exchange_reserves": True}))
    row = cd.score_asset({"symbol": "BTC", "weight_profile": "btc"}, _Adapter(), WEIGHTS,
                         calc_date="2026-10-04")
    assert row["dropped_pillars"] == ["thesis_integrity"]
    assert row["subscores"]["des"] == 61.0


# ---- methodology break -------------------------------------------------------

def _history(tmp_path, ticker, points):
    p = LthcsPersist(data_root=tmp_path)
    path = p.history_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ticker": ticker, "history": [
        {"date": d, "score": s} for d, s in points]}))
    return p


def test_registry_has_the_sec_restore_break():
    assert "2026-10-04" in methodology.break_dates()
    assert methodology.breaks_between("2026-10-03", "2026-10-04")
    assert not methodology.breaks_between("2026-10-04", "2026-10-05")
    assert methodology.recent_breaks("2026-11-01")


def test_drift_is_reanchored_at_the_break(tmp_path):
    pts = [("2026-09-04", 40.0), ("2026-09-27", 41.0), ("2026-10-03", 42.0),
           ("2026-10-04", 60.0), ("2026-10-05", 61.0)]
    p = _history(tmp_path, "AAPL", pts)
    raw = p.read_prior_scores("AAPL", "2026-10-06")
    assert raw["30d"] == 40.0 and raw["7d"] == 41.0
    adj = p.read_prior_scores("AAPL", "2026-10-06", breaks=("2026-10-04",))
    assert adj["1d"] == 61.0            # window after the break: unchanged
    assert adj["7d"] == 60.0            # re-anchored at the break day
    assert adj["30d"] == 60.0


def test_drift_on_the_break_day_has_no_comparable_prior(tmp_path):
    p = _history(tmp_path, "AAPL", [("2026-10-03", 42.0)])
    adj = p.read_prior_scores("AAPL", "2026-10-04", breaks=("2026-10-04",))
    assert adj["1d"] is None
    assert score.compute_drift(60.0, adj)["drift_1d"] == 0.0


def test_snapshot_carries_methodology_breaks(tmp_path):
    p = LthcsPersist(data_root=tmp_path)
    path = p.write_snapshot("2026-10-05", "v1.1.0", "standard_compounder", [],
                            extra={"methodology_breaks": methodology.recent_breaks("2026-10-05")})
    payload = json.loads(path.read_text())
    assert payload["methodology_breaks"][0]["date"] == "2026-10-04"
    assert set(payload) >= {"calc_date", "model_version", "scores"}
