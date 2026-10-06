"""Tests for ``scripts/lthcs_calibrate_bands.py`` and ``lthcs/bands.py``.

All tests run against synthetic snapshots / a copy of weights.json in
``tmp_path``; the real ``data/lthcs/`` tree is never modified. The copy
carries the fixed test band set (tests/lthcs/band_fixture.py) and no
calibration provenance, so these tests do not depend on what the live bands
were last calibrated to.
"""

from __future__ import annotations

import importlib.util
import json
import random
import re
import sys
from pathlib import Path

import pytest

from tests.lthcs.band_fixture import FIXTURE_SCORE_BANDS, fixture_band_ranges

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "lthcs_calibrate_bands.py"
REAL_WEIGHTS = REPO_ROOT / "data" / "lthcs" / "weights.json"

BANDS = ("elite", "high_confidence", "constructive", "monitor", "weakening", "review")


@pytest.fixture(scope="module")
def cal():
    spec = importlib.util.spec_from_file_location("lthcs_calibrate_bands", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _snapshot(path: Path, calc_date: str, scores):
    rows = [
        {"ticker": f"T{i:04d}", "lthcs_score": s, "band": "monitor"}
        for i, s in enumerate(scores)
    ]
    path.write_text(json.dumps({
        "calc_date": calc_date,
        "model_version": "v1.1.0",
        "weights_profile_default": "standard_compounder",
        "scores": rows,
    }))
    return path


def _normal_scores(n, mu=48.0, sd=10.0, seed=7):
    rng = random.Random(seed)
    return [round(min(99.9, max(1.0, rng.gauss(mu, sd))), 1) for _ in range(n)]


def _weights_text_with_fixture_bands(text: str) -> str:
    """The live weights.json text with the fixture band numbers and without a
    ``score_bands_calibration`` block. Edited as text so the hand-aligned
    layout the --write path must preserve is still there."""
    for b, spec in FIXTURE_SCORE_BANDS.items():
        pat = re.compile(r'("' + re.escape(b) + r'"\s*:\s*\{\s*"min"\s*:\s*)-?\d+(\s*,\s*"max"\s*:\s*)-?\d+')
        text, n = pat.subn(lambda m: f'{m.group(1)}{spec["min"]}{m.group(2)}{spec["max"]}', text, count=1)
        assert n == 1, b
    key = '"score_bands_calibration"'
    idx = text.find(key)
    if idx >= 0:
        start = text.index(":", idx + len(key)) + 1
        while text[start] in " \t\r\n":
            start += 1
        _, end = json.JSONDecoder().raw_decode(text, start)
        text = text[:text.rindex(",", 0, idx)] + text[end:]
    cfg = json.loads(text)
    assert {k: (v["min"], v["max"]) for k, v in cfg["score_bands"].items()} == fixture_band_ranges()
    assert "score_bands_calibration" not in cfg
    return text


@pytest.fixture()
def env(tmp_path):
    snaps = tmp_path / "snapshots"
    snaps.mkdir()
    weights = tmp_path / "weights.json"
    weights.write_text(_weights_text_with_fixture_bands(REAL_WEIGHTS.read_text()))
    (snaps / "index.json").write_text("{}")  # non-dated file must be ignored
    return snaps, weights


def _assert_contiguous(ranges, min_width):
    prev_lo = 101
    for b in BANDS:
        lo, hi = ranges[b]
        assert hi == prev_lo - 1, (b, ranges)
        assert hi - lo + 1 >= min_width, (b, ranges)
        prev_lo = lo
    assert prev_lo == 0
    assert ranges["elite"][1] == 100


# ---------------------------------------------------------------------------
# Core math on synthetic distributions
# ---------------------------------------------------------------------------

def test_uniform_distribution_hits_targets(cal):
    # 0.0, 0.1, ... 99.9 uniformly -> 1000 scores, each integer has 10.
    scores = [i / 10 for i in range(1000)]
    r = cal.propose_cutoffs(scores, cal.DEFAULT_TARGETS_PCT, 3)
    assert r == {
        "elite": (95, 100),
        "high_confidence": (85, 94),
        "constructive": (60, 84),
        "monitor": (30, 59),
        "weakening": (10, 29),
        "review": (0, 9),
    }


def test_normal_distribution_shares_close_to_targets(cal):
    scores = _normal_scores(600)
    r = cal.propose_cutoffs(scores, cal.DEFAULT_TARGETS_PCT, 3)
    _assert_contiguous(r, 3)
    counts = cal.count_by_band(scores, r)
    for b in BANDS:
        share = 100.0 * counts[b] / len(scores)
        assert abs(share - cal.DEFAULT_TARGETS_PCT[b]) <= 3.0, (b, share, r)


def test_compressed_distribution_enforces_min_width_and_order(cal):
    # Everything in a 2-point window: quantiles collapse, structure must hold.
    scores = [50.0 + (i % 20) / 10 for i in range(500)]
    for w in (1, 3, 5):
        r = cal.propose_cutoffs(scores, cal.DEFAULT_TARGETS_PCT, w)
        _assert_contiguous(r, w)
    # All-identical scores too.
    r = cal.propose_cutoffs([42.0] * 450, cal.DEFAULT_TARGETS_PCT, 3)
    _assert_contiguous(r, 3)


def test_extreme_distributions_clamp_to_edges(cal):
    hi = cal.propose_cutoffs([99.9] * 500, cal.DEFAULT_TARGETS_PCT, 4)
    _assert_contiguous(hi, 4)
    lo = cal.propose_cutoffs([0.0] * 500, cal.DEFAULT_TARGETS_PCT, 4)
    _assert_contiguous(lo, 4)


def test_custom_targets_and_validation(cal):
    t = cal.parse_targets("elite=10,high_confidence=10,constructive=20,monitor=20,weakening=20,review=20")
    assert t["elite"] == 10.0
    with pytest.raises(cal.CalibrationError):
        cal.parse_targets("elite=10,high_confidence=10")  # missing bands
    with pytest.raises(cal.CalibrationError):
        cal.parse_targets("elite=50,high_confidence=10,constructive=20,monitor=20,weakening=20,review=20")
    with pytest.raises(cal.CalibrationError):
        cal.parse_targets("bogus=100")
    with pytest.raises(cal.CalibrationError):
        cal.propose_cutoffs([50.0], cal.DEFAULT_TARGETS_PCT, 17)  # 6*17 > 101


def test_snapshot_parsing_skips_unscored_rows(cal, tmp_path):
    p = tmp_path / "2026-01-02.json"
    p.write_text(json.dumps({"calc_date": "2026-01-02", "scores": [
        {"ticker": "A", "lthcs_score": 50.0},
        {"ticker": "B", "lthcs_score": None},
        {"ticker": "C", "lthcs_score": "n/a"},
        {"ticker": "D", "lthcs_score": float("nan")},
        {"ticker": "E", "lthcs_score": True},
        {"ticker": "F", "lthcs_score": 61},
    ]}))
    [(d, vals)] = cal.load_snapshots([p])
    assert d == "2026-01-02"
    assert vals == {"A": 50.0, "F": 61.0}


def test_rejects_non_snapshot_json(cal, tmp_path):
    p = tmp_path / "2026-01-02.json"
    p.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(cal.CalibrationError):
        cal.load_snapshots([p])


# ---------------------------------------------------------------------------
# CLI: guard, read-only default, --write provenance
# ---------------------------------------------------------------------------

def test_guard_refuses_small_universe(cal, env, capsys):
    snaps, weights = env
    _snapshot(snaps / "2026-10-04.json", "2026-10-04", _normal_scores(219))
    before = weights.read_bytes()
    rc = cal.main(["--snapshot-dir", str(snaps), "--weights", str(weights), "--write"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "refusing to calibrate" in err and "219" in err and "400" in err
    assert weights.read_bytes() == before


def test_guard_checks_every_pooled_snapshot(cal, env, capsys):
    snaps, weights = env
    _snapshot(snaps / "2026-10-03.json", "2026-10-03", _normal_scores(219, seed=1))
    _snapshot(snaps / "2026-10-04.json", "2026-10-04", _normal_scores(520, seed=2))
    rc = cal.main(["--snapshot-dir", str(snaps), "--weights", str(weights), "--snapshots", "2"])
    assert rc == 2
    assert "2026-10-03: 219" in capsys.readouterr().err
    # Latest alone passes.
    assert cal.main(["--snapshot-dir", str(snaps), "--weights", str(weights)]) == 0


def test_guard_override_allows_small_universe_read_only(cal, env, capsys):
    snaps, weights = env
    _snapshot(snaps / "2026-10-04.json", "2026-10-04", _normal_scores(219))
    before = weights.read_bytes()
    rc = cal.main(["--snapshot-dir", str(snaps), "--weights", str(weights), "--min-tickers", "100"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERRIDDEN" in out
    assert "read-only" in out
    assert weights.read_bytes() == before


def test_default_picks_latest_dated_snapshot_and_reports(cal, env, capsys):
    snaps, weights = env
    _snapshot(snaps / "2026-10-01.json", "2026-10-01", _normal_scores(100, seed=3))
    _snapshot(snaps / "2026-10-04.json", "2026-10-04", _normal_scores(520, seed=4))
    before = weights.read_bytes()
    rc = cal.main(["--snapshot-dir", str(snaps), "--weights", str(weights)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "snapshot(s):   2026-10-04" in out
    assert "520 scored" in out
    for b in BANDS:
        assert b in out
    assert "85-100" in out  # current (fixture) elite range shown
    assert "elite/high_confidence populated: yes" in out
    assert weights.read_bytes() == before  # no change without --write


def test_write_updates_bands_and_adds_provenance(cal, env, capsys):
    snaps, weights = env
    original = json.loads(weights.read_text())
    _snapshot(snaps / "2026-10-03.json", "2026-10-03", _normal_scores(520, seed=5))
    _snapshot(snaps / "2026-10-04.json", "2026-10-04", _normal_scores(521, seed=6))
    rc = cal.main([
        "--snapshot-dir", str(snaps), "--weights", str(weights),
        "--snapshots", "2", "--write", "--today", "2026-10-05",
    ])
    assert rc == 0
    new = json.loads(weights.read_text())

    # Bands: same keys/labels/colors, new contiguous numbers.
    assert list(new["score_bands"]) == list(original["score_bands"])
    for b in BANDS:
        assert new["score_bands"][b]["label"] == original["score_bands"][b]["label"]
        assert new["score_bands"][b]["color"] == original["score_bands"][b]["color"]
    ranges = {b: (new["score_bands"][b]["min"], new["score_bands"][b]["max"]) for b in BANDS}
    _assert_contiguous(ranges, 3)
    assert ranges["elite"] != (85, 100)

    prov = new["score_bands_calibration"]
    assert prov["calibrated_at"] == "2026-10-05"
    assert prov["calibrated_from"] == ["2026-10-03", "2026-10-04"]
    assert prov["ticker_count"] == 521
    assert prov["observations"] == 1041
    assert prov["method"] == cal.METHOD
    assert prov["targets_pct"] == cal.DEFAULT_TARGETS_PCT
    assert prov["previous_cutoffs"]["elite"] == [85, 100]
    assert new["last_updated"] == "2026-10-05"

    # Everything else untouched.
    for k in original:
        if k not in ("score_bands", "last_updated"):
            assert new[k] == original[k], k

    # Schema still validates (provenance block is allowed).
    from lthcs.schemas.weights import Weights
    Weights.model_validate(new)

    # Hand-aligned layout of the profiles block survives.
    assert '"standard_compounder":       [0.25, 0.20, 0.15, 0.20, 0.20]' in weights.read_text()

    # Second --write replaces (not duplicates) the provenance block.
    rc = cal.main([
        "--snapshot-dir", str(snaps), "--weights", str(weights),
        "--write", "--today", "2026-10-06", "--targets",
        "elite=10,high_confidence=10,constructive=20,monitor=20,weakening=20,review=20",
    ])
    assert rc == 0
    text = weights.read_text()
    assert text.count('"score_bands_calibration"') == 1
    again = json.loads(text)
    assert again["score_bands_calibration"]["calibrated_at"] == "2026-10-06"
    assert again["score_bands_calibration"]["calibrated_from"] == ["2026-10-04"]


# ---------------------------------------------------------------------------
# lthcs.bands: consumers read the live config
# ---------------------------------------------------------------------------

def test_bands_module_reads_weights_and_falls_back(tmp_path):
    from lthcs import bands

    w = tmp_path / "weights.json"
    cfg = json.loads(REAL_WEIGHTS.read_text())
    cfg["score_bands"]["elite"]["min"] = 72
    cfg["score_bands"]["high_confidence"].update(min=64, max=71)
    w.write_text(json.dumps(cfg))
    rng = bands.band_ranges(bands.load_score_bands(w))
    assert rng["elite"] == (72, 100)
    assert rng["high_confidence"] == (64, 71)
    assert bands.ordered_bands(bands.load_score_bands(w))[0] == ("elite", 72, 100)

    missing = bands.load_score_bands(tmp_path / "nope.json")
    assert missing == bands.DEFAULT_SCORE_BANDS


def test_narratives_follow_live_band_floor(monkeypatch):
    from lthcs import narratives

    custom = {
        "elite": {"min": 72, "max": 100}, "high_confidence": {"min": 64, "max": 71},
        "constructive": {"min": 55, "max": 63}, "monitor": {"min": 45, "max": 54},
        "weakening": {"min": 36, "max": 44}, "review": {"min": 0, "max": 35},
    }
    monkeypatch.setattr(narratives, "load_score_bands", lambda: custom)
    out = narratives.generate_narratives({
        "ticker": "X", "lthcs_score": 67.0, "band": "high_confidence",
        "subscores": {p: 70.0 for p in narratives.PILLAR_ORDER},
    })
    # Floor of high_confidence is 64 -> "below 64" (63.9 rounds to 64).
    assert "Constructive composite move below 64" in out["what_would_break"]


def test_llm_prompt_band_lines_follow_config():
    from lthcs import narratives_llm

    custom = {
        "elite": {"min": 72, "max": 100}, "high_confidence": {"min": 64, "max": 71},
        "constructive": {"min": 55, "max": 63}, "monitor": {"min": 45, "max": 54},
        "weakening": {"min": 36, "max": 44}, "review": {"min": 0, "max": 35},
    }
    text = narratives_llm._band_prompt_lines(custom)
    assert "- Elite (72-100):" in text
    assert "- Structural Review (0-35):" in text
    live = json.loads(REAL_WEIGHTS.read_text())["score_bands"]
    assert f"- Elite ({live['elite']['min']}-{live['elite']['max']}):" in narratives_llm.SYSTEM_PROMPT


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_quality_runner_distribution_uses_weights_cutoffs(tmp_path, monkeypatch):
    runner = _load_script("lthcs_quality_audit_runner")
    cfg = json.loads(REAL_WEIGHTS.read_text())
    cfg["score_bands"]["elite"]["min"] = 72
    cfg["score_bands"]["high_confidence"].update(min=64, max=71)
    cfg["score_bands"]["constructive"].update(min=55, max=63)
    cfg["score_bands"]["monitor"].update(min=45, max=54)
    cfg["score_bands"]["weakening"].update(min=36, max=44)
    cfg["score_bands"]["review"].update(min=0, max=35)
    (tmp_path / "weights.json").write_text(json.dumps(cfg))
    monkeypatch.setattr(runner, "DATA", tmp_path)
    snap = {"scores": [{"lthcs_score": v} for v in (78.4, 72.0, 70.0, 64.5, 50.0, 35.9, 20.0)]}
    d = runner._distribution_summary(snap)
    assert d["elite_count"] == 2
    assert d["high_conf_count"] == 2
    assert d["review_count"] == 2
    assert d["band_cutoffs"] == {"elite_min": 72, "high_confidence_min": 64, "review_max": 35}


def test_band_verdict_review_overflow_scales_with_universe():
    audit = _load_script("lthcs_weight_threshold_audit")
    # 52 of 520 = 10% -> healthy on the expanded universe...
    assert audit.band_verdict(52, "review", 520) == "KEEP"
    # ...but 100 of 520 (19%) overflows.
    assert "SHIFT-UP" in audit.band_verdict(100, "review", 520)
    # Without a total the legacy absolute threshold still applies.
    assert "SHIFT-UP" in audit.band_verdict(50, "review")


def _live_ranges():
    live = json.loads(REAL_WEIGHTS.read_text())["score_bands"]
    return {k: (v["min"], v["max"]) for k, v in live.items()}


def test_fallback_bands_mirror_the_live_weights():
    # lthcs.bands.DEFAULT_SCORE_BANDS and lthcs_tab/lthcs-bands.js are only
    # used when weights.json cannot be read, but then they must not bring
    # back older cutoffs: a recalibration updates them in the same change.
    from lthcs import bands

    live = json.loads(REAL_WEIGHTS.read_text())["score_bands"]
    assert bands.DEFAULT_SCORE_BANDS == live
    js = (REPO_ROOT / "lthcs_tab" / "lthcs-bands.js").read_text(encoding="utf-8")
    block = js[js.index("export const DEFAULT_SCORE_BANDS"):]
    block = block[:block.index("};")]
    js_ranges = {m.group(1): (int(m.group(2)), int(m.group(3))) for m in re.finditer(
        r"(\w+):\s*\{\s*min:\s*(\d+),\s*max:\s*(\d+)", block)}
    assert js_ranges == _live_ranges()


def test_help_page_fallback_bands_mirror_the_live_weights():
    # lthcs_help/index.html shows static band ranges until lthcs-help-bands.js
    # replaces them from weights.json (and for good if that fetch fails or JS
    # is off). They sat at the pre-2026-10-06 cutoffs after a recalibration,
    # so keep them in step like the other fallbacks.
    html = (REPO_ROOT / "lthcs_help" / "index.html").read_text(encoding="utf-8")
    ui_to_key = {"high": "high_confidence"}
    found = {
        ui_to_key.get(m.group(1), m.group(1)): (int(m.group(2)), int(m.group(3)))
        for m in re.finditer(
            r'class="lhlp-band" data-band="(\w+)">.*?'
            r'class="lhlp-band-range">(\d+)&ndash;(\d+)<',
            html, re.S)
    }
    assert found == _live_ranges()


def test_dashboard_payload_carries_live_bands_for_the_about_panel(tmp_path, monkeypatch):
    # The dashboard's "About LTHCS" panel used to hard-code its band list
    # (and it had drifted: "Elite (90+) · High (80-89)"); it now renders
    # DATA.lthcs.score_bands, built from weights.json.
    import app

    snaps = tmp_path / "lthcs" / "snapshots"
    snaps.mkdir(parents=True)
    (snaps / "2026-10-06.json").write_text(json.dumps({"calc_date": "2026-10-06", "scores": [
        {"ticker": "A", "lthcs_score": 60.0, "band": "constructive"}]}))
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    out = app.build_lthcs_payload()
    assert [b["key"] for b in out["score_bands"]] == list(BANDS)
    assert {b["key"]: (b["min"], b["max"]) for b in out["score_bands"]} == _live_ranges()
    assert "Elite (90+)" not in app.HTML_TEMPLATE
    assert "L.score_bands" in app.HTML_TEMPLATE
