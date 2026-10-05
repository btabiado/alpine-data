"""V1 LTHCS payload: a methodology break is not a market signal.

On 2026-10-04 SEC data restored the financial pillar; "98 tickers shifted
band overnight" and a 1d composite jump were presented as market moves, and
mover subscores carried the dropped thesis placeholder. Offline fixture tree.
"""
from __future__ import annotations

import json
from pathlib import Path

import app


def _w(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj))


def _tree(tmp_path, prev_date, latest_date):
    lthcs = tmp_path / "lthcs"
    for i in range(6):
        _w(lthcs / "history" / "by_ticker" / f"T{i}.json", {
            "ticker": f"T{i}", "history": [
                {"date": prev_date, "score": 40.0, "band": "watch"},
                {"date": latest_date, "score": 60.0, "band": "monitor"}]})
    _w(lthcs / "index" / f"{prev_date}.json", {"score": -40, "as_of": prev_date})
    _w(lthcs / "index" / f"{latest_date}.json",
       {"score": -22, "as_of": latest_date, "components": []})
    rows = [{"ticker": f"T{i}", "lthcs_score": 60.0, "band": "monitor",
             "drift_30d": float(i), "sector": "Tech",
             "subscores": {"financial_evolution": 70.0, "thesis_integrity": 55.0},
             "dropped_pillars": ["thesis_integrity"]} for i in range(6)]
    _w(lthcs / "snapshots" / f"{latest_date}.json",
       {"calc_date": latest_date, "scores": rows})
    return tmp_path


def test_band_shift_and_composite_jump_across_break_are_relabelled(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_DIR", _tree(tmp_path, "2026-10-03", "2026-10-04"))
    out = app.build_lthcs_payload()
    heads = [i["headline"] for i in out["insights"]]
    assert not any("shifted band overnight" in h for h in heads)
    meth = [i for i in out["insights"] if i["category"] == "methodology"]
    assert meth and all("not a market signal" in i["headline"] for i in meth)
    assert all(i["methodology_breaks"] == ["2026-10-04"] for i in meth)
    # movers: dropped placeholder is null, and the 30d window is annotated
    g = out["movers"]["gainers"][0]
    assert g["subscores"]["thesis_integrity"] is None
    assert g["subscores"]["financial_evolution"] == 70.0
    assert "methodology change on 2026-10-04" in out["movers"]["note"]


def test_ordinary_band_shift_is_still_a_movers_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "DATA_DIR", _tree(tmp_path, "2027-03-01", "2027-03-02"))
    out = app.build_lthcs_payload()
    assert any("shifted band overnight" in i["headline"] for i in out["insights"])
    assert "note" not in out["movers"]
