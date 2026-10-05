"""Index Exiles: tickers that left every tracked index stay active and scored.

Owner's rule: a ticker that was in the S&P 500, S&P 100, NASDAQ-100 or DJIA
and has been dropped from all of them keeps being scored daily. It is
marked ``index_exile`` (grouped as an "Index Exile"), its
``index_membership`` is empty, and it is never deactivated for leaving an
index. Covers:

* the schema (lthcs/schemas/universe.py),
* the committed data against its committed evidence (no network),
* the sync rule in scripts/lthcs_universe_sp500_sync.py, both modes,
* index-level aggregates / scopes excluding exiles,
* the front-end filter + badge on every LTHCS page that filters by index.
"""

from __future__ import annotations

import copy
import csv
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

import fetch_stock_money_flow as money_flow
from lthcs.schemas.universe import IndexExile, IndexHistoryEvent, Universe, UniverseEntry
from scripts import lthcs_leaderboards as leaderboards
from scripts import lthcs_universe_sp500_sync as sync

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data" / "lthcs"
MAPPING = DATA / "universe_candidate" / "mapping_2026-10-05"
SP500_DIR = DATA / "universe_candidate" / "sp500_2026-10-05"
EXILES = {"AZN", "GFS", "LCID", "MDB", "TEAM", "TTD", "ZS"}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _entry(ticker: str, tags: List[str], **extra: Any) -> Dict[str, Any]:
    e = {"ticker": ticker, "name": ticker + " Inc.", "exchange": "NASDAQ", "index_membership": tags,
         "sector": "Technology", "industry": "Software", "maturity_stage": "standard_compounder",
         "active": True}
    e.update(extra)
    return e


def _marker(**over: Any) -> Dict[str, Any]:
    m = {"former_indexes": ["NASDAQ-100"], "dropped_on": "2026-01-20", "detected_on": "2026-10-05",
         "source": "https://example.test/changes (revision 1)",
         "drops": [{"index": "NASDAQ-100", "dropped_on": "2026-01-20", "source": "https://example.test"}]}
    m.update(over)
    return m


# --- schema --------------------------------------------------------------------


def test_schema_accepts_an_exile_and_its_history() -> None:
    e = UniverseEntry.model_validate(_entry("AZN", [], index_exile=_marker(), index_history=[
        {"event": "exiled", "date": "2026-10-05", "indexes": ["NASDAQ-100"], "source": "sync"}]))
    assert e.is_index_exile and e.active and e.index_exile.dropped_on.isoformat() == "2026-01-20"
    # dropped_on may be null (date not established) — never a guess.
    UniverseEntry.model_validate(_entry("X", [], index_exile=_marker(dropped_on=None, drops=[])))


def test_schema_rejects_an_exile_that_is_still_in_an_index() -> None:
    with pytest.raises(ValidationError, match="an exile is in no tracked index"):
        UniverseEntry.model_validate(_entry("AZN", ["NASDAQ-100"], index_exile=_marker()))


@pytest.mark.parametrize("bad", [
    {"former_indexes": []},
    {"former_indexes": ["Russell 2000"]},
    {"dropped_on": "2026-10-06"},                                   # after detected_on
    {"drops": [{"index": "DJIA", "dropped_on": None, "source": "x"}]},  # not a former index
    {"detected_on": None},
    {"source": ""},
    {"surprise": 1},                                                 # extra="forbid"
])
def test_schema_rejects_malformed_exile_markers(bad: Dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        IndexExile.model_validate(_marker(**bad))


def test_rejoin_event_keeps_the_cleared_marker() -> None:
    ev = IndexHistoryEvent.model_validate({"event": "rejoined", "date": "2027-01-02",
                                           "indexes": ["NASDAQ-100"], "source": "sync",
                                           "exile": _marker()})
    assert ev.exile is not None and ev.exile.former_indexes == ["NASDAQ-100"]
    with pytest.raises(ValidationError):
        IndexHistoryEvent.model_validate({"event": "deactivated", "date": "2027-01-02", "source": "x"})


# --- committed data ------------------------------------------------------------------


def test_committed_universe_validates_and_marks_exactly_the_seven_exiles() -> None:
    uni = Universe.model_validate(_json(DATA / "universe.json"))
    exiles = {t.ticker for t in uni.tickers if t.index_exile is not None}
    untagged_active = {t.ticker for t in uni.tickers if t.active and not t.index_membership}
    assert exiles == EXILES
    assert untagged_active == EXILES
    for t in uni.tickers:
        if t.ticker in EXILES:
            assert t.active, "%s: an index exit must never deactivate a ticker" % t.ticker
            assert t.inactive_reason is None
            assert [ev.event for ev in t.index_history] == ["exiled"]


def test_exile_markers_match_the_committed_evidence() -> None:
    ev = _json(MAPPING / "_index_exiles.json")
    assert set(ev["tickers"]) == EXILES
    for src in ev["sources"].values():
        assert src["revision_id"] and src["url"].startswith("https://en.wikipedia.org/wiki/Historical_components")
    by = {e["ticker"]: e for e in _json(DATA / "universe.json")["tickers"]}
    for t, rec in ev["tickers"].items():
        m = by[t]["index_exile"]
        assert m["former_indexes"] == rec["former_indexes"], t
        assert {c["index"] for c in rec["changes"]} == set(rec["former_indexes"]), t
        # dropped_on = the day it left its LAST index, straight from the evidence.
        assert m["dropped_on"] == max(c["dropped_on"] for c in rec["changes"]), t
        assert {(d["index"], d["dropped_on"]) for d in m["drops"]} == \
            {(c["index"], c["dropped_on"]) for c in rec["changes"]}, t
        for c in rec["changes"]:
            assert c["joined_on"] < c["dropped_on"] <= m["detected_on"], t
            assert str(ev["sources"]["ndx100_changes" if c["index"] == "NASDAQ-100"
                                     else "sp500_changes"]["revision_id"]) in c["source"], t
        assert "_index_exiles.json" in m["source"], t
    # Spot-check the dates recorded from the change tables.
    assert by["AZN"]["index_exile"]["dropped_on"] == "2026-01-20"
    assert by["LCID"]["index_exile"]["dropped_on"] == "2023-12-18"
    assert by["TTD"]["index_exile"]["former_indexes"] == ["S&P 500", "NASDAQ-100"]
    assert by["TTD"]["index_exile"]["dropped_on"] == "2026-09-21"


def test_exiles_are_in_none_of_the_current_constituent_lists() -> None:
    def symbols(path: Path) -> set:
        with path.open(newline="", encoding="utf-8") as fh:
            return {r["Symbol"].strip().upper() for r in csv.DictReader(fh)}

    current = (symbols(SP500_DIR / "_constituents_sp500.csv")
               | symbols(MAPPING / "_constituents_sp100.csv")
               | symbols(MAPPING / "_constituents_ndx100.csv")
               | set(_json(SP500_DIR / "_source.json")["djia"]["components"]))
    assert not (EXILES & current)


def test_universe_description_names_the_exiles() -> None:
    desc = _json(DATA / "universe.json")["description"]
    assert "Index Exiles (left every tracked index; still active and scored daily, counted in no index): " \
           "AZN, GFS, LCID, MDB, TEAM, TTD, ZS (7)." in desc


def test_validate_fails_on_an_untagged_active_ticker_without_a_marker(tmp_path, monkeypatch) -> None:
    from lthcs import validate
    raw = _json(DATA / "universe.json")
    ok, _ = validate.validate_universe(DATA / "universe.json")
    assert ok
    del next(e for e in raw["tickers"] if e["ticker"] == "AZN")["index_exile"]
    p = tmp_path / "universe.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    ok, _ = validate.validate_universe(p)
    assert not ok


# --- sync rule: unit -----------------------------------------------------------------


def test_rule_drop_marks_exile_and_keeps_it_active() -> None:
    e = _entry("ZS", [])
    assert sync.apply_exile_rule(e, ["NASDAQ-100"], "2026-10-05", "SRC rev 7") == "exiled"
    assert e["active"] is True and "inactive_reason" not in e
    m = e["index_exile"]
    assert m["former_indexes"] == ["NASDAQ-100"]
    assert m["detected_on"] == "2026-10-05" and m["source"] == "SRC rev 7"
    assert m["dropped_on"] is None  # the run date is not the index's effective date
    assert e["index_history"] == [{"event": "exiled", "date": "2026-10-05",
                                   "indexes": ["NASDAQ-100"], "source": "SRC rev 7"}]
    UniverseEntry.model_validate(e)


def test_rule_rejoin_clears_marker_and_records_history() -> None:
    e = _entry("ZS", [])
    sync.apply_exile_rule(e, ["S&P 500", "NASDAQ-100"], "2026-10-05", "A")
    marker = copy.deepcopy(e["index_exile"])
    e["index_membership"] = ["NASDAQ-100"]
    assert sync.apply_exile_rule(e, [], "2027-03-23", "B") == "rejoined"
    assert "index_exile" not in e and e["active"] is True
    assert [h["event"] for h in e["index_history"]] == ["exiled", "rejoined"]
    assert e["index_history"][1] == {"event": "rejoined", "date": "2027-03-23",
                                     "indexes": ["NASDAQ-100"], "source": "B", "exile": marker}
    UniverseEntry.model_validate(e)


def test_rule_keeps_a_sourced_marker_and_ignores_never_indexed_or_inactive() -> None:
    e = _entry("AZN", [], index_exile=_marker())
    before = copy.deepcopy(e)
    assert sync.apply_exile_rule(e, [], "2026-11-01", "C") is None
    assert e == before  # sourced dropped_on is not overwritten
    never = _entry("NEW", [])
    assert sync.apply_exile_rule(never, [], "2026-11-01", "C") is None
    assert "index_exile" not in never
    gone = _entry("EA", [], active=False, inactive_reason="taken private")
    assert sync.apply_exile_rule(gone, ["S&P 500"], "2026-11-01", "C") is None
    assert "index_exile" not in gone and gone["active"] is False


# --- sync rule: both modes end to end ---------------------------------------------------


def _write_tags_dir(d: Path, sp100: List[str], ndx: List[str]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "_source.json").write_text(json.dumps({
        "fetched_at": "2026-12-21",
        "sp100": {"url": "https://example.test/sp100", "revision_id": 11},
        "ndx100": {"url": "https://example.test/ndx", "as_of": "2026-12-19"}}), encoding="utf-8")
    for fname, syms in (("_constituents_sp100.csv", sp100), ("_constituents_ndx100.csv", ndx)):
        with (d / fname).open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Symbol", "Name"])
            for s in syms:
                w.writerow([s, s])
    return d


def test_index_tags_mode_exiles_then_readmits(tmp_path) -> None:
    uni = {"tickers": [_entry("AAA", ["NASDAQ-100"]), _entry("BBB", ["S&P 500", "NASDAQ-100"])]}
    d1 = _write_tags_dir(tmp_path / "t1", [], ["BBB"])
    report, _ = sync.refresh_index_tags(uni, d1, "2026-12-21")
    a = uni["tickers"][0]
    assert report["exiled"] == ["AAA"] and report["rejoined"] == []
    assert a["active"] is True and a["index_membership"] == []
    assert a["index_exile"]["source"] == "S&P 100: https://example.test/sp100 (revision 11); " \
                                         "NASDAQ-100: https://example.test/ndx (as of 2026-12-19)"
    assert "index_exile" not in uni["tickers"][1]  # still in the S&P 500
    d2 = _write_tags_dir(tmp_path / "t2", [], ["AAA", "BBB"])
    report, _ = sync.refresh_index_tags(uni, d2, "2027-03-23")
    assert report["rejoined"] == ["AAA"]
    assert "index_exile" not in a and a["index_membership"] == ["NASDAQ-100"]
    assert [h["event"] for h in a["index_history"]] == ["exiled", "rejoined"]
    Universe.model_validate({"version": "t", "last_updated": "2027-03-23", "tickers": uni["tickers"]})


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """A throwaway data/lthcs + S&P 500 candidate dir; sync.DATA points at it."""
    data = tmp_path / "data"
    data.mkdir()
    (data / "universe.json").write_text(json.dumps({
        "version": "3.0.0", "last_updated": "2026-10-05",
        "description": "LTHCS universe test. 3 tickers, 3 active.",
        "tickers": [
            _entry("AAA", ["S&P 500"]),
            _entry("BBB", ["S&P 500"]),
            _entry("CCC", ["S&P 500", "NASDAQ-100"]),
        ]}), encoding="utf-8")
    (data / "sp500_candidate_seed.json").write_text(json.dumps({"tickers": []}), encoding="utf-8")
    (data / "peer_groups.json").write_text(json.dumps({"last_updated": "x", "sector_groups": {}}),
                                           encoding="utf-8")
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "_source.json").write_text(json.dumps({
        "fetched_at": "2026-12-21",
        "sp500": {"url": "https://example.test/sp500", "revision_id": 42},
        "djia": {"url": "https://example.test/djia", "revision_id": 43, "components": []}}),
        encoding="utf-8")
    (cand / "_sec_exchange.json").write_text(json.dumps({}), encoding="utf-8")
    (cand / "_yahoo_metrics.json").write_text(json.dumps({"tickers": {}}), encoding="utf-8")
    with (cand / "_constituents_sp500.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Symbol", "Security", "GICS Sector", "GICS Sub-Industry", "Date added", "CIK"])
        w.writerow(["BBB", "BBB Inc.", "Information Technology", "Application Software", "2020-01-01", "1"])
    monkeypatch.setattr(sync, "DATA", data)
    return data, cand


def test_sp500_mode_exiles_a_dropped_ticker_and_never_deactivates(sandbox) -> None:
    data, cand = sandbox
    assert sync.main(["--candidate-dir", str(cand), "--write", "--run-date", "2026-12-21"]) == 0
    by = {e["ticker"]: e for e in _json(data / "universe.json")["tickers"]}
    assert all(e["active"] for e in by.values())
    assert by["AAA"]["index_membership"] == []
    assert by["AAA"]["index_exile"]["former_indexes"] == ["S&P 500"]
    assert by["AAA"]["index_exile"]["source"] == \
        "S&P 500: https://example.test/sp500 (revision 42); DJIA: https://example.test/djia (revision 43)"
    assert by["AAA"]["index_exile"]["detected_on"] == "2026-12-21"
    assert "index_exile" not in by["BBB"]
    assert by["CCC"]["index_membership"] == ["NASDAQ-100"] and "index_exile" not in by["CCC"]
    rep = _json(cand / "_sync_report.json")
    assert rep["exiled"] == ["AAA"]
    desc = _json(data / "universe.json")["description"]
    assert desc.endswith("counted in no index): AAA (1).")
    Universe.model_validate(_json(data / "universe.json"))


def test_combined_run_judges_exile_on_the_final_tags(sandbox, tmp_path) -> None:
    """AAA leaves the S&P 500 but joins the NASDAQ-100 in the same run: no exile.
    CCC leaves both: exile, with both former indexes."""
    data, cand = sandbox
    tags = _write_tags_dir(tmp_path / "tags", [], ["AAA"])
    assert sync.main(["--candidate-dir", str(cand), "--index-tags-dir", str(tags), "--write",
                      "--run-date", "2026-12-21"]) == 0
    by = {e["ticker"]: e for e in _json(data / "universe.json")["tickers"]}
    assert by["AAA"]["index_membership"] == ["NASDAQ-100"] and "index_exile" not in by["AAA"]
    assert "index_history" not in by["AAA"]
    assert by["CCC"]["index_exile"]["former_indexes"] == ["S&P 500", "NASDAQ-100"]
    assert "NASDAQ-100: https://example.test/ndx" in by["CCC"]["index_exile"]["source"]
    assert all(e["active"] for e in by.values())
    assert _json(tags / "_index_tag_report.json")["exiled"] == ["CCC"]


# --- aggregates exclude exiles ----------------------------------------------------------


def test_index_scopes_and_aggregates_exclude_exiles() -> None:
    uni = _json(DATA / "universe.json")
    for scope in leaderboards.SCOPE_TO_INDEX:
        members = leaderboards.load_universe_index_members(uni, scope)
        assert members and not (members & EXILES), scope
    assert leaderboards.load_universe_index_members(uni, "exiles") == EXILES
    by = {e["ticker"]: e for e in uni["tickers"]}
    # Stock money-flow index scope (S&P 500 / NASDAQ-100 / DJIA).
    assert not [t for t in EXILES if money_flow._in_scope(by[t])]
    # No index count anywhere can include an exile: none carries an index tag.
    for idx in ("S&P 500", "S&P 100", "NASDAQ-100", "DJIA"):
        assert not [t for t in EXILES if idx in by[t]["index_membership"]]


def test_exiles_stay_in_the_scored_universe() -> None:
    """Exiles are active, so lthcs_daily scores them; the latest snapshot has them."""
    snaps = sorted(p for p in (DATA / "snapshots").glob("*.json") if p.stem[:4].isdigit())
    scored = {r["ticker"] for r in _json(snaps[-1])["scores"]}
    assert EXILES <= scored


# --- front end ----------------------------------------------------------------------------

PAGES = {
    # page html: (script, marker proving the exile control exists)
    "lthcs_tab/index.html": ("lthcs_tab/lthcs-tab.js", 'data-index="exiles"'),
    "lthcs_table/index.html": ("lthcs_table/lthcs-table.js", '<option value="exiles"'),
    "lthcs_tab_v2/index.html": ("lthcs_tab_v2/lthcs-v2.js", 'data-filter-value="exiles"'),
    "lthcs_tab/heatmap/index.html": ("lthcs_tab/heatmap/lthcs-heatmap.js", 'data-value="exiles"'),
    "lthcs_leaderboards/index.html": ("lthcs_leaderboards/lthcs-leaderboards.js", 'data-scope="exiles"'),
}


@pytest.mark.parametrize("html", sorted(PAGES))
def test_every_index_filter_has_an_index_exiles_option(html: str) -> None:
    script, marker = PAGES[html]
    page = (REPO_ROOT / html).read_text(encoding="utf-8").replace("&nbsp;", " ")
    assert marker in page
    i = page.index(marker)
    assert "Index Exiles" in page[i:i + 400]
    js = (REPO_ROOT / script).read_text(encoding="utf-8")
    assert re.search(r"import \{[^}]*\bexileInfo\b[^}]*\} from '[./]*(lthcs_tab/)?lthcs-exile\.js'", js), script
    assert "EXILE_FILTER_KEY" in js


@pytest.mark.parametrize("script", ["lthcs_tab/lthcs-tab.js", "lthcs_table/lthcs-table.js",
                                    "lthcs_tab_v2/lthcs-v2.js"])
def test_cards_and_rows_carry_the_exile_badge(script: str) -> None:
    js = (REPO_ROOT / script).read_text(encoding="utf-8")
    assert "exileBadgeHTML(row.exile" in js
    assert "data-exile=" in js


def test_exile_badge_styles_ship_on_every_page() -> None:
    for css in ("lthcs_tab/lthcs.css", "lthcs_tab_v2/lthcs-v2.css"):
        assert ".lthcs-exile-badge" in (REPO_ROOT / css).read_text(encoding="utf-8"), css


def test_card_view_counts_exiles_only_on_their_own_button() -> None:
    js = (REPO_ROOT / "lthcs_tab" / "lthcs-tab.js").read_text(encoding="utf-8")
    body = js[js.index("function inGroup("):]
    body = body[:body.index("\n}\n")]
    assert "if (key === EXILE_FILTER_KEY) return !!row.exile;" in body
    assert "row.indices.includes(key)" in body
    # The three index filters / counts never test row.exile: an exile has no
    # index key, so it cannot be counted as an index member.
    assert "!inGroup(row, index)" in js and "inGroup(row, indexKey)" in js


EXILE_JS = REPO_ROOT / "lthcs_tab" / "lthcs-exile.js"


@pytest.fixture(scope="module")
def exile_js():
    src = EXILE_JS.read_text(encoding="utf-8").replace("export const ", "const ") \
        .replace("export function ", "function ")
    mr = None
    try:
        import py_mini_racer as mr  # type: ignore
    except ImportError:
        mr = None
    if mr is not None:
        ctx = mr.MiniRacer()
        ctx.eval(src)
        return lambda fn, *a: ctx.call(fn, *a)
    node = shutil.which("node")
    if not node:
        pytest.skip("no JS engine (py_mini_racer / node) to run the shipped JS")

    def call(fn, *args):
        prog = src + "\nprocess.stdout.write(JSON.stringify(%s(...%s)));" % (fn, json.dumps(list(args)))
        out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout
        return json.loads(out)
    return call


def test_exile_tooltip_from_committed_data(exile_js) -> None:
    by = {e["ticker"]: e for e in _json(DATA / "universe.json")["tickers"]}
    tip = lambda t: exile_js("exileTooltip", exile_js("exileInfo", by[t]))  # noqa: E731
    assert tip("AZN") == "Left NASDAQ-100 on 2026-01-20; still scored daily"
    assert tip("TTD") == "Left NASDAQ-100 on 2025-12-22 and S&P 500 on 2026-09-21; still scored daily"
    assert exile_js("exileInfo", by["AAPL"]) is None
    for t in EXILES:
        assert "still scored daily" in tip(t)


def test_exile_tooltip_never_invents_a_date(exile_js) -> None:
    info = exile_js("exileInfo", {"active": True, "index_exile": {
        "former_indexes": ["S&P 100"], "dropped_on": None, "drops": []}})
    assert exile_js("exileTooltip", info) == "Left S&P 100 (date not established); still scored daily"
    # A delisted (inactive) ticker is not an exile, even with a stale marker.
    assert exile_js("exileInfo", {"active": False, "index_exile": {"former_indexes": ["DJIA"]}}) is None
    badge = exile_js("exileBadgeHTML", info, "x")
    assert 'class="lthcs-exile-badge x"' in badge and ">Exile<" in badge
