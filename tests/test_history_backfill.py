"""Persistence and backfill tests: scripts/snapshot_history.py,
scripts/backfill_history.py, the whale fix in scripts/snapshot_composites.py,
and the two source fixes found by the history audit (Port of LA's malformed
total cell, CoinGecko's doubled trailing day) plus the MCS 2026 parser.

The rule every test here defends: a value in the archive is either what the
site observed, or something reproduced from a REAL source and labelled as
such. Never a guess, never an interpolation, never a silent overwrite.
All offline.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
for p in (str(SCRIPTS), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sh():
    return _load("snapshot_history")


@pytest.fixture(scope="module")
def bh():
    return _load("backfill_history")


@pytest.fixture(scope="module")
def sc():
    return _load("snapshot_composites")


def _read(path: Path) -> list[dict]:
    import csv
    with path.open(newline="") as fh:
        return list(csv.DictReader(fh))


STOCK = {"as_of": "2026-10-02", "stocks": [
    {"symbol": "AAPL", "score": 28.9, "label": "Neutral", "mfi": 70.26, "cmf": 0.0575,
     "as_of": "2026-10-02"},
    {"symbol": "NVDA", "score": 21.5, "label": "Neutral", "mfi": 82.37, "cmf": -0.2167},
    {"symbol": "XYZ", "score": None, "label": "n/a"},              # unscored: skipped
]}


def _travel(day: str, levels: dict[str, int], source="html") -> dict:
    return {"generated_at": f"{day}T00:30:00Z", "source": source,
            "advisories": [{"name": n, "level": l, "date": "2026-08-01"}
                           for n, l in levels.items()]}


# ==========================================================================
# snapshot_history: stock money flow
# ==========================================================================

def test_stock_rows_only_for_completed_bars(sh):
    rows = sh.stock_rows(STOCK, date(2026, 10, 3))
    assert {r["symbol"] for r in rows} == {"AAPL", "NVDA"}       # no score, no row
    assert all(r["date"] == "2026-10-02" for r in rows)
    # On the bar's own day it is still partial (intraday): nothing recorded.
    assert sh.stock_rows(STOCK, date(2026, 10, 2)) == []


def test_stale_fallback_stock_payload_is_not_a_new_observation(sh):
    """The committed repo copy is a 2026-06-08 fallback; a build that failed
    to rewrite it must not seed the history with June."""
    old = {**STOCK, "as_of": "2026-06-08",
           "stocks": [{**s, "as_of": "2026-06-08"} for s in STOCK["stocks"]]}
    assert sh.stock_rows(old, date(2026, 10, 4)) == []
    assert sh.stock_rows(old, date(2026, 10, 4), max_lag_days=None)   # backfill mode


def test_stock_history_is_append_only_and_idempotent(sh, tmp_path):
    assert sh.apply_stock(tmp_path, STOCK, date(2026, 10, 4)) == 2
    assert sh.apply_stock(tmp_path, STOCK, date(2026, 10, 4)) == 0
    changed = {**STOCK, "stocks": [{**STOCK["stocks"][0], "score": -99}]}
    assert sh.apply_stock(tmp_path, changed, date(2026, 10, 4)) == 0
    rows = _read(tmp_path / sh.STOCK_CSV)
    assert [r["score"] for r in rows if r["symbol"] == "AAPL"] == ["28.9"]
    assert list(rows[0]) == sh.STOCK_HEADER


# ==========================================================================
# snapshot_history: travel advisories
# ==========================================================================

def test_travel_needs_a_payload_generated_that_day(sh, tmp_path):
    p = _travel("2026-10-03", {"A": 1, "B": 4})
    assert sh.apply_travel(tmp_path, p, "2026-10-04", require_generated_on="2026-10-04") == (0, 0)
    assert sh.apply_travel(tmp_path, p, "2026-10-03", require_generated_on="2026-10-03") == (1, 2)
    lv = _read(tmp_path / sh.TRAVEL_LEVELS_CSV)
    assert lv[0]["countries"] == "2" and lv[0]["level_4"] == "1"


def test_travel_change_log_records_only_changes(sh, tmp_path):
    sh.apply_travel(tmp_path, _travel("2026-10-01", {"A": 1, "B": 2}), "2026-10-01")
    sh.apply_travel(tmp_path, _travel("2026-10-02", {"A": 1, "B": 3}), "2026-10-02")
    # B missing from one fetch is not recorded as a removal
    sh.apply_travel(tmp_path, _travel("2026-10-03", {"A": 1, "C": 2, "B": 3}), "2026-10-03")
    ch = [(r["date"], r["country"], r["level"], r["prev_level"])
          for r in _read(tmp_path / sh.TRAVEL_CHANGES_CSV)]
    assert ch == [("2026-10-01", "A", "1", ""), ("2026-10-01", "B", "2", ""),
                  ("2026-10-02", "B", "3", "2"), ("2026-10-03", "C", "2", "")]


def test_travel_refuses_a_partial_fetch(sh, tmp_path):
    full = {f"C{i}": 1 for i in range(10)}
    sh.apply_travel(tmp_path, _travel("2026-10-01", full), "2026-10-01")
    thin = {f"C{i}": 1 for i in range(5)}
    assert sh.apply_travel(tmp_path, _travel("2026-10-02", thin), "2026-10-02") == (0, 0)


def test_backfilled_older_day_keeps_the_change_log_honest(sh, tmp_path):
    """A backfill lands BEFORE rows the live build wrote. The live baseline
    row for a country whose level did not change must then disappear, and a
    real change must name the level that was actually in force."""
    sh.apply_travel(tmp_path, _travel("2026-10-04", {"A": 1, "B": 3}), "2026-10-04")
    sh.apply_travel(tmp_path, _travel("2026-09-01", {"A": 1, "B": 2}), "2026-09-01",
                    provenance="r2@2026-09-01")
    ch = [(r["date"], r["country"], r["level"], r["prev_level"], r["provenance"])
          for r in _read(tmp_path / sh.TRAVEL_CHANGES_CSV)]
    assert ch == [("2026-09-01", "A", "1", "", "r2@2026-09-01"),
                  ("2026-09-01", "B", "2", "", "r2@2026-09-01"),
                  ("2026-10-04", "B", "3", "2", "")]
    assert [r["date"] for r in _read(tmp_path / sh.TRAVEL_LEVELS_CSV)] == [
        "2026-09-01", "2026-10-04"]


# ==========================================================================
# snapshot_composites: whale sentiment is captured, not archived as null
# ==========================================================================

def _whale_tree(days: int = 40, end: str = "2026-10-03") -> dict:
    last = date.fromisoformat(end)
    ds = [(last - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    ser = lambda base, step: [{"date": d, "value": base + i * step} for i, d in enumerate(ds)]
    return {
        "fetched_at": f"{end}T23:00:00+00:00",
        "btc": {"hash_rate": ser(100, 1), "miners_revenue_usd": ser(1000, -5),
                "avg_tx_usd": ser(50, 0.5), "output_volume_btc": ser(10, 0.1),
                "active_addresses": ser(700, 2), "tx_volume_usd": ser(5, 0.1)},
        "distribution": {"buckets": [{"date": d, "b1k_10k": 100 + i, "b10k_100k": 50,
                                      "b100k_1m": 0} for i, d in enumerate(ds)]},
        "eth": {"coin_metrics": {"AdrActCnt": ser(400, 3), "TxCnt": ser(1000, 1)},
                "etherscan_daily": {"available": False}},
    }


def test_snapshot_computes_whale_sentiment_the_builders_compute(sc, tmp_path, monkeypatch):
    import fetch_market as fm
    whale = _whale_tree()
    (tmp_path / "whale.json").write_text(json.dumps(whale))
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    idx = sc.collect()
    want = fm.compute_whale_sentiment(whale)
    assert idx["whale_sentiment_btc"]["score"] == want["score"]
    assert idx["whale_sentiment_btc"]["as_of"] == want["as_of"] == "2026-10-03"
    assert idx["whale_sentiment_eth"]["score"] == fm.compute_whale_sentiment_eth(whale)["score"]


def test_eth_no_data_placeholder_is_not_archived_as_a_zero(sc, tmp_path, monkeypatch):
    whale = _whale_tree()
    whale["eth"] = {"coin_metrics": {}, "etherscan_daily": {}}
    (tmp_path / "whale.json").write_text(json.dumps(whale))
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    assert sc.collect()["whale_sentiment_eth"] is None


# ==========================================================================
# backfill_history: composites
# ==========================================================================

def test_truncate_whale_cuts_on_chain_a_day_behind_cohorts(bh):
    t = bh.truncate_whale(_whale_tree(), "2026-09-30")
    assert t["btc"]["hash_rate"][-1]["date"] == "2026-09-29"
    assert t["distribution"]["buckets"][-1]["date"] == "2026-09-30"
    assert t["eth"]["coin_metrics"]["TxCnt"][-1]["date"] == "2026-09-29"


def test_series_recompute_matches_the_live_formula_on_the_last_day(bh):
    """The rule's own check: recomputing the newest day reproduces exactly
    what the card computed from the full payload (2026-10-04 in prod)."""
    import fetch_market as fm
    whale = _whale_tree(end="2026-10-03")
    whale["distribution"]["buckets"].append({"date": "2026-10-04", "b1k_10k": 140,
                                             "b10k_100k": 50, "b100k_1m": 0})
    live = fm.compute_whale_sentiment(whale)["score"]
    got = bh.whale_entries_from_series(whale, "2026-10-04", "fixture")
    assert got["whale_sentiment_btc"]["score"] == live
    assert got["whale_sentiment_btc"]["backfilled_from"].startswith("series:fixture")
    assert "backfilled" in got["whale_sentiment_btc"]["note"]


def test_merge_never_overwrites_an_observed_value(bh):
    idx = {"a": {"score": 1, "as_of": "2026-09-01"}, "b": None,
           "c": {"score": 2, "backfilled_from": "series:x"}}
    fills = {"a": {"score": 9, "backfilled_from": "r2:k"},
             "b": {"score": 5, "backfilled_from": "git:y"},
             "c": {"score": 3, "backfilled_from": "r2:k"},
             "d": None}
    assert sorted(bh.merge(idx, fills)) == ["b", "c"]
    assert idx["a"]["score"] == 1                     # observed: untouched
    assert idx["c"]["score"] == 3                     # recompute superseded by r2
    assert "d" not in idx
    # ...and a series recompute never supersedes an r2 copy
    assert bh.merge(idx, {"c": {"score": 7, "backfilled_from": "series:z"}}) == []


def _git(root: Path, *args, env=None):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                   env={**os.environ, **(env or {})})


def _commit(root: Path, rel: str, text: str, when: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", rel,
         env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    return tmp_path


def test_etf_and_lthcs_come_from_the_file_as_committed_that_day(bh, repo):
    rows_a = "date,Total\n" + "\n".join(f"2026-09-{d:02d},100" for d in range(1, 11))
    rows_b = rows_a + "\n2026-09-11,-900\n"
    for asset in ("btc", "eth"):
        _commit(repo, f"data/{asset}_flows.csv", rows_a, "2026-09-10T14:00:00+00:00")
        _commit(repo, f"data/{asset}_flows.csv", rows_b, "2026-09-11T14:00:00+00:00")
    _commit(repo, "data/lthcs/index/2026-09-10.json",
            json.dumps({"score": -20, "label": "L", "as_of": "2026-09-10"}),
            "2026-09-10T23:00:00+00:00")
    e10 = bh.etf_entries_from_git("2026-09-10", repo)
    e11 = bh.etf_entries_from_git("2026-09-11", repo)
    assert e10["etf_flow_sentiment_btc"]["as_of"] == "2026-09-10"
    assert e11["etf_flow_sentiment_btc"]["as_of"] == "2026-09-11"
    assert e10["etf_flow_sentiment_btc"]["score"] > e11["etf_flow_sentiment_btc"]["score"]
    assert "git:" in e10["etf_flow_sentiment_btc"]["backfilled_from"]
    assert e10["etf_flow_sentiment"]["score"] == e10["etf_flow_sentiment_btc"]["score"]
    assert bh.etf_entries_from_git("2026-09-01", repo)["etf_flow_sentiment_btc"] is None
    lt = bh.lthcs_entry_from_git("2026-09-11", repo)
    assert lt["score"] == -20 and lt["as_of"] == "2026-09-10"


def test_backfill_creates_missing_days_with_nulls_for_the_unrecoverable(bh, repo):
    from datetime import datetime, timezone
    _commit(repo, "data/btc_flows.csv", "date,Total\n2026-09-09,1\n2026-09-10,2\n",
            "2026-09-10T14:00:00+00:00")
    cdir = repo / "data" / "composites"
    cdir.mkdir(parents=True)
    (cdir / "2026-09-09.json").write_text(json.dumps({"as_of": "2026-09-09", "indexes": {
        "money_flow_index": {"score": 1, "as_of": "2026-09-08"},
        "whale_sentiment_btc": None}}))
    rep = bh.backfill_composites(repo, _whale_tree(end="2026-09-10"), "fixture",
                                 "2026-09-10", ("2026-09-10", "2026-09-10"),
                                 datetime(2026, 10, 4, tzinfo=timezone.utc))
    assert set(rep) == {"2026-09-09", "2026-09-10"}
    new = json.loads((cdir / "2026-09-10.json").read_text())
    assert new["backfilled"] and new["as_of"] == "2026-09-10"
    assert new["indexes"]["money_flow_index"] is None          # no real source
    assert new["indexes"]["whale_sentiment_btc"]["backfilled_from"].startswith("series:")
    old = json.loads((cdir / "2026-09-09.json").read_text())
    assert old["indexes"]["money_flow_index"] == {"score": 1, "as_of": "2026-09-08"}


def test_r2_backfill_uses_the_served_payload_and_marks_it(bh, sh, tmp_path):
    cdir = tmp_path / "data" / "composites"
    cdir.mkdir(parents=True)
    (cdir / "2026-09-01.json").write_text(json.dumps({"as_of": "2026-09-01", "indexes": {
        "whale_sentiment_btc": {"score": 5, "backfilled_from": "series:x", "as_of": "2026-08-31"},
        "whale_sentiment_eth": None}}))
    basis = "oldest contributing on-chain series"
    served = {
        "2026-09-01": {
            "data-whale.json": {"sentiment": {"score": -12, "label": "NEUTRAL",
                                              "as_of": "2026-08-31", "as_of_basis": basis},
                                "eth": {"sentiment": {"available": False, "score": 0}}},
            sh.STOCK_SRC: {**STOCK, "as_of": "2026-09-01",
                           "stocks": [{**STOCK["stocks"][0], "as_of": "2026-09-01"}]},
            sh.TRAVEL_SRC: _travel("2026-09-01", {"A": 2}),
        },
        "2026-09-02": {sh.TRAVEL_SRC: _travel("2026-09-01", {"A": 2})},  # stale-kept
    }
    rep = bh.backfill_from_r2(tmp_path, "2026-09-01", "2026-09-02",
                              lambda d, f: served.get(d, {}).get(f))
    snap = json.loads((cdir / "2026-09-01.json").read_text())["indexes"]
    assert snap["whale_sentiment_btc"]["score"] == -12
    assert snap["whale_sentiment_btc"]["backfilled_from"].startswith("r2:raw/alpine-data/2026-09-01/")
    assert snap["whale_sentiment_eth"] is None       # NO DATA is not a zero
    assert rep["stock_rows"] == 1 and rep["travel_days"] == 1
    assert _read(tmp_path / sh.STOCK_CSV)[0]["provenance"] == "r2@2026-09-01"


# ==========================================================================
# source fixes found by the audit
# ==========================================================================

POLA_ROWS = """
<table><tr><td></td><td>Loaded Imports</td><td>Empty Imports</td><td>Total Imports</td>
<td>Loaded Exports</td><td>Empty Exports</td><td>Total Exports</td><td>Total TEUs</td></tr>
<tr><td>October</td><td>506,613.20</td><td>4,199.60</td><td>510,812.80</td><td>143,935.75</td>
<td>325,980.00</td><td>469,915.75</td><td>980,728.55</td></tr>
<tr><td>November</td><td>&nbsp;464,819.70</td><td>1,247.70</td><td>466,067.40</td>
<td>130,916.50</td><td>292,762.25</td><td>423,678.75</td><td>889.,748.15</td></tr>
<tr><td>December</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td><td>&nbsp;</td>
<td>&nbsp;</td><td>&nbsp;</td></tr>
</table>"""


def test_pola_malformed_total_is_derived_from_its_own_subtotals():
    """portoflosangeles.org's 2020 page prints November's total as
    '889.,748.15'. The month shipped with no total. The page's own identity
    (imports + exports) and its calendar-year total both give 889,746.15."""
    import fetch_supplies as fs
    rows = {r["month"]: r for r in fs.parse_pola_year_table(POLA_ROWS, 2020)}
    assert rows["2020-10"]["total"] == 980729 and "total_derived" not in rows["2020-10"]
    assert rows["2020-11"]["total"] == 889746
    assert "889.,748.15" in rows["2020-11"]["total_derived"]
    assert "2020-12" not in rows          # a blank month in progress stays absent


def test_coingecko_trailing_now_sample_does_not_double_today():
    import fetch_market as fm
    day = 86_400_000
    t0 = 1_790_985_600_000                      # 2026-10-03T00:00Z
    pts = [[t0, 1.0], [t0 + day, 2.0], [t0 + day + 75_000_000, 3.0]]
    got = fm._one_per_day(pts)
    assert [p["date"] for p in got] == ["2026-10-03", "2026-10-04"]
    assert got[-1]["value"] == 3.0             # the latest reading wins


MCS_LONG = (
    "MCS chapter,Section,Commodity,Country,Statistics,Statistics_detail,Unit,Year,Value\n"
    "SILVER,Salient Statistics-United States,Silver,United States,Production,"
    "Production: Mine,metric tons,2024,\"1,050\"\n"
    "SILVER,World Mine Production and Reserves,Silver,Mexico,Production,"
    "Mine production,metric tons,2024,\"5,780\"\n"
    "SILVER,World Mine Production and Reserves,Silver,Mexico,Production,"
    "Mine production,metric tons,2025,\"6,300\"\n"
    "SILVER,World Mine Production and Reserves,Silver,Peru,Production,"
    "Mine production,metric tons,2025,\"3,600\"\n"
    "SILVER,World Mine Production and Reserves,Silver,Other countries,Production,"
    "Mine production,metric tons,2025,\"2,100\"\n"
    "SILVER,World Mine Production and Reserves,Silver,World total,Production,"
    "Mine production: rounded,metric tons,2025,\"26,000\"\n"
    "SILVER,World Mine Production and Reserves,Silver,Peru,Reserves,"
    "Reserves,metric tons,2025,\"110,000\"\n"
    "GOLD,World Mine Production and Reserves,Gold,China,Production,"
    "Mine production,metric tons,2025,380\n"
)


def test_mcs_2026_long_table_gives_the_newest_production_year():
    import fetch_metals as fmt
    s = fmt._parse_usgs_mcs_combined_csv(MCS_LONG, "SILVER", "Silver", 2026)
    assert s["year"] == 2025
    assert s["by_country"] == [{"country": "Mexico", "tonnes": 6300.0},
                               {"country": "Peru", "tonnes": 3600.0}]
    assert s["source"] == "USGS MCS 2026 (Silver, 2025 estimate)"
    g = fmt._parse_usgs_mcs_combined_csv(MCS_LONG, "GOLD", "Gold", 2026)
    assert g["by_country"] == [{"country": "China", "tonnes": 380.0}]
    assert fmt._parse_usgs_mcs_combined_csv(MCS_LONG, "COPPER", "Copper", 2026) is None
