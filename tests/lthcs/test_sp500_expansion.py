"""Guards for the 2026-10-05 S&P 500 + DJIA universe sync.

Covers the data contract (universe validates, membership matches the
committed constituent snapshot, every ticker is wired into the side tables)
and the honesty rules for tickers that have no score history yet.
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from lthcs import score
from lthcs.narratives import generate_narratives
from lthcs.persist import LthcsPersist
from lthcs.schemas import Universe

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data" / "lthcs"
CANDIDATE = DATA / "universe_candidate" / "sp500_2026-10-05"


def _universe() -> dict:
    return json.loads((DATA / "universe.json").read_text())


def _sp500_symbols() -> set:
    with (CANDIDATE / "_constituents_sp500.csv").open(newline="", encoding="utf-8") as fh:
        return {r["Symbol"] for r in csv.DictReader(fh)}


def test_universe_validates_against_schema() -> None:
    Universe.model_validate(_universe())


def test_universe_covers_current_sp500_and_djia() -> None:
    u = _universe()
    active = {t["ticker"] for t in u["tickers"] if t.get("active", True)}
    sp = _sp500_symbols()
    djia = set(json.loads((CANDIDATE / "_source.json").read_text())["djia"]["components"])
    assert len(sp) == 503 and len(djia) == 30
    assert sp <= active
    assert djia <= active
    tagged_djia = {t["ticker"] for t in u["tickers"]
                   if t.get("active", True) and "DJIA" in t["index_membership"]}
    assert tagged_djia == djia
    tagged_sp = {t["ticker"] for t in u["tickers"]
                 if t.get("active", True) and "S&P 500" in t["index_membership"]}
    assert tagged_sp == sp


def test_no_previously_active_ticker_was_dropped() -> None:
    before = json.loads((DATA / "universe.pre-wave-a.json").read_text())
    now = {t["ticker"]: t for t in _universe()["tickers"]}
    for t in before["tickers"]:
        assert t["ticker"] in now, t["ticker"]


def test_bny_restored_and_bk_stays_inactive() -> None:
    by = {t["ticker"]: t for t in _universe()["tickers"]}
    assert by["BNY"]["active"] is True
    assert by["BK"]["active"] is False
    assert by["BNY"]["maturity_stage"] == by["BK"]["maturity_stage"]
    assert by["EA"]["active"] is False  # taken private; not in the S&P 500


def test_every_active_ticker_is_wired_into_side_tables() -> None:
    u = _universe()
    active = [t["ticker"] for t in u["tickers"] if t.get("active", True)]
    groups = json.loads((DATA / "peer_groups.json").read_text())["sector_groups"]
    grouped = {t for g in groups.values() for t in g["tickers"]}
    cusip_map = json.loads((DATA / "13f_cusip_map.json").read_text())["tickers"]
    missing_group = [t for t in active if t not in grouped]
    missing_cusip_entry = [t for t in active if t not in cusip_map]
    assert not missing_group, missing_group
    assert not missing_cusip_entry, missing_cusip_entry
    # A ticker sits in exactly one curated cohort.
    seen: dict = {}
    for name, g in groups.items():
        for t in g["tickers"]:
            assert t not in seen, "%s in %s and %s" % (t, seen[t], name)
            seen[t] = name


def test_new_tickers_have_documented_maturity_stage() -> None:
    for t in _universe()["tickers"]:
        if str(t.get("source", "")).startswith("sp500_sync"):
            assert t.get("maturity_note"), t["ticker"]
            assert t.get("exchange") and t.get("industry"), t["ticker"]


# --- honesty: no fake trend for tickers without history --------------------


def test_drift_windows_without_prior_lists_missing_windows() -> None:
    assert score.drift_windows_without_prior({}) == ["1d", "7d", "30d", "90d"]
    assert score.drift_windows_without_prior(
        {"1d": 50.0, "7d": 49.0, "30d": None, "90d": float("nan")}) == ["30d", "90d"]


def test_compute_lthcs_score_marks_new_ticker_drift_unavailable() -> None:
    weights = json.loads((DATA / "weights.json").read_text())
    subs = {k: 50.0 for k in score.PILLAR_ORDER}
    row = score.compute_lthcs_score(
        ticker="NEWCO", sector="Industrials", maturity_stage="standard_compounder",
        pillar_subscores=subs, weights_config=weights, prior_scores=None)
    assert row["drift_30d"] == 0.0  # numeric contract unchanged ...
    assert row["drift_unavailable"] == ["1d", "7d", "30d", "90d"]  # ... but flagged


def test_narrative_does_not_claim_flat_trend_for_new_ticker() -> None:
    weights = json.loads((DATA / "weights.json").read_text())
    subs = {k: 50.0 for k in score.PILLAR_ORDER}
    row = score.compute_lthcs_score(
        ticker="NEWCO", sector="Industrials", maturity_stage="standard_compounder",
        pillar_subscores=subs, weights_config=weights, prior_scores=None)
    row["history_points"] = 0
    out = generate_narratives(row)
    assert "over 30 days" not in out["todays_take"]
    assert "no score history yet" in out["todays_take"]
    assert "+0.0" not in out["why_changed"]


def test_fill_history_gaps_skips_tickers_outside_active_set(tmp_path: Path) -> None:
    store = LthcsPersist(tmp_path)
    store.append_history_entry("BK", "2026-07-01", 55.0, "monitor", "v1")
    store.append_history_entry("JPM", "2026-07-01", 60.0, "monitor", "v1")
    store.fill_history_gaps(today="2026-07-05", tickers=["JPM"])
    bk = store.read_history("BK")["history"]
    jpm = store.read_history("JPM")["history"]
    assert [r["date"] for r in bk] == ["2026-07-01"]
    assert sum(1 for r in jpm if r.get("synthetic")) == 3


# --- runtime helpers ----------------------------------------------------------


def test_prune_cache_keeps_only_long_lived_unexpired_entries(tmp_path: Path) -> None:
    from scripts.lthcs_prune_cache import prune

    now = time.time()
    root = tmp_path / "lthcs"
    (root / "sec_13f").mkdir(parents=True)
    (root / "yahoo").mkdir()

    def env(path: Path, ttl: float, age: float) -> None:
        path.write_text(json.dumps({"fetched_at": now - age, "ttl_seconds": ttl, "value": 1}))

    env(root / "sec_13f" / "filing.json", 365 * 86400, 86400)       # keep
    env(root / "sec_13f" / "old.json", 30 * 86400, 31 * 86400)      # expired
    env(root / "yahoo" / "prices.json", 86400, 3600)                # short TTL
    env(root / "sec_13f" / "subs.json", 7 * 86400, 60)              # == 7d: not kept
    (root / "trends_progress.json").write_text("{}")                # not an envelope
    kept, deleted = prune(root, min_ttl_days=7, now=now)
    assert (kept, deleted) == (1, 4)
    assert (root / "sec_13f" / "filing.json").exists()


def test_trends_rotation_orders_least_recently_covered_first(tmp_path: Path) -> None:
    from scripts.lthcs_trends_weekly import order_least_recently_covered

    trends = tmp_path / "trends"
    trends.mkdir()
    (trends / "2026-W38.json").write_text(json.dumps(
        {"tickers": {"AAA": {"series": [1]}, "BBB": {"series": [1]}}}))
    (trends / "2026-W39.json").write_text(json.dumps(
        {"tickers": {"AAA": {"series": [1]}, "CCC": {"series": []}}}))
    order = order_least_recently_covered(["AAA", "BBB", "CCC", "DDD"], trends)
    # never covered (CCC had an empty series, DDD absent) -> BBB (W38) -> AAA (W39)
    assert order == ["CCC", "DDD", "BBB", "AAA"]
