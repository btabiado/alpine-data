"""Guards for the 2026-10-05 mapping follow-up to the S&P 500 sync.

Each section checks committed data against the committed evidence in
data/lthcs/universe_candidate/mapping_2026-10-05/ (no network):

* 13F CUSIPs: every active ticker has a CUSIP that the official SEC list
  carries, with a valid check digit, claimed by no other active ticker.
* S&P 100 / NASDAQ-100 tags match the committed constituent snapshots.
* The nine maturity stages the sync marked uncertain are decided.
* The strict-bank allowlist holds every active bank that files the bank
  XBRL concept family.
* No carried-forward synthetic history rows remain for BK / EA / DOW (DOW's
  were re-created by the 2026-10-06 catch-up and removed again), every
  synthetic row left is a gap-day fill, and the pruning rule keeps real rows
  and gap-day fills.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from lthcs.persist import LthcsPersist
from lthcs.pillars import financial
from scripts.build_13f_cusip_map import cusip_check_digit
from scripts.lthcs_prune_carried_forward_history import carried_forward_rows

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data" / "lthcs"
EVIDENCE = DATA / "universe_candidate" / "mapping_2026-10-05"


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _universe() -> list:
    return _json(DATA / "universe.json")["tickers"]


def _active() -> list:
    return [e for e in _universe() if e.get("active", True)]


# --- 13F CUSIPs ---------------------------------------------------------------


def test_cusip_check_digit_matches_known_cusips() -> None:
    for cusip in ("037833100", "02079K305", "G54950103", "86800U302", "43849R105"):
        assert cusip_check_digit(cusip[:8]) == cusip[8], cusip


def test_every_active_ticker_has_a_cusip_on_the_official_list() -> None:
    cmap = _json(DATA / "13f_cusip_map.json")
    entries = cmap["tickers"]
    rows = _json(EVIDENCE / "_sec_13flist_rows.json")["tickers"]
    unresolved = cmap["provenance"]["unresolved"]
    for e in _active():
        t = e["ticker"]
        cusips = entries[t]["cusips"]
        if not cusips:
            assert t in unresolved, "%s has no CUSIP and no recorded reason" % t
            continue
        live_now = [r for r in rows[t] if r["list"] == "current" and r["status"] != "*D*"]
        assert live_now, "%s: no CUSIP live on the current SEC list" % t
        backed = {r["cusip"] for r in rows[t] if r["status"] != "*D*"}
        for c in cusips:
            assert len(c) == 9 and cusip_check_digit(c[:8]) == c[8], (t, c)
            assert c in backed, "%s: %s is not live on the current or previous list" % (t, c)
            assert all(r["description"] not in ("CALL", "PUT") for r in rows[t]), t


def test_no_cusip_is_claimed_by_two_active_tickers() -> None:
    entries = _json(DATA / "13f_cusip_map.json")["tickers"]
    seen: dict = {}
    for e in _active():
        for c in entries[e["ticker"]]["cusips"]:
            assert c[:8] not in seen, "%s claimed by %s and %s" % (c, seen.get(c[:8]), e["ticker"])
            seen[c[:8]] = e["ticker"]


def test_cusip_map_records_its_source() -> None:
    prov = _json(DATA / "13f_cusip_map.json")["provenance"]
    assert prov["sec_13f_list"]["url"].startswith("https://www.sec.gov/files/investment/13flist")
    assert prov["coverage"]["after"] == "515/515"
    assert set(prov["corrected_2026-10-05"]) >= {"CEG", "CTAS", "DASH", "LIN", "LRCX", "SMCI"}


# --- S&P 100 / NASDAQ-100 tags ----------------------------------------------------


@pytest.mark.parametrize("tag,fname", [("S&P 100", "_constituents_sp100.csv"),
                                       ("NASDAQ-100", "_constituents_ndx100.csv")])
def test_index_tags_match_committed_constituents(tag: str, fname: str) -> None:
    with (EVIDENCE / fname).open(newline="", encoding="utf-8") as fh:
        members = {r["Symbol"] for r in csv.DictReader(fh)}
    assert len(members) == 101  # 100 companies, Alphabet listed twice
    active = {e["ticker"]: e for e in _active()}
    tagged = {t for t, e in active.items() if tag in e["index_membership"]}
    assert tagged == members & set(active)


def test_index_tag_sources_are_cited() -> None:
    src = _json(EVIDENCE / "_source.json")
    assert src["sp100"]["revision_id"] and src["ndx100"]["as_of"]
    desc = _json(DATA / "universe.json")["description"]
    assert str(src["sp100"]["revision_id"]) in desc
    assert "not refreshed" not in desc


# --- maturity stages --------------------------------------------------------------


def test_uncertain_maturity_stages_are_decided() -> None:
    review = _json(EVIDENCE / "_maturity_review.json")["tickers"]
    by = {e["ticker"]: e for e in _universe()}
    assert set(review) == {"APO", "COIN", "CPAY", "HOOD", "IBKR", "ECHO", "MRNA", "FDXF", "HONA"}
    for t, r in review.items():
        assert by[t]["maturity_stage"] == r["to"], t
        note = by[t]["maturity_note"]
        assert "uncertain" not in note and "_maturity_review.json" in note, t
        assert r["sec_xbrl"]["revenue_fy"] or r["sec_xbrl"].get("revenue_quarters"), t
    assert not [e["ticker"] for e in _active() if "uncertain" in (e.get("maturity_note") or "")]


# --- strict-bank allowlist ----------------------------------------------------------

# Documented in lthcs/pillars/financial.py: passes the XBRL test, kept out.
BANK_TEST_EXCLUSIONS = {"AXP"}
# Documented in lthcs/pillars/financial.py: in the allowlist without passing.
BANK_TEST_GRANDFATHERED = {"SCHW", "BLK"}


def test_bank_allowlist_holds_every_active_bank_by_xbrl() -> None:
    check = _json(EVIDENCE / "_bank_xbrl_check.json")
    rows = check["tickers"]
    active = {e["ticker"]: e for e in _active()}
    fin = {t for t, e in active.items() if e.get("sector") == "Financials"}
    assert fin <= set(rows), sorted(fin - set(rows))
    passing = {t for t, r in rows.items() if r["passes"] and t in active}
    assert passing - BANK_TEST_EXCLUSIONS <= financial.BANK_TICKERS
    assert set(check["added"]) <= financial.BANK_TICKERS
    for t in financial.BANK_TICKERS:
        if t in active:
            assert rows[t]["passes"] or t in BANK_TEST_GRANDFATHERED, t


def test_every_gics_bank_is_in_the_allowlist() -> None:
    for e in _active():
        if e.get("industry") in ("Diversified Banks", "Regional Banks", "Banks") \
                or e.get("sector_group") == "Banks":
            assert financial.is_bank_ticker(e["ticker"]), e["ticker"]


def test_new_bank_routes_through_the_bank_path() -> None:
    def quarters(vals):
        ends = ["2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31",
                "2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
        starts = ["2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01",
                  "2025-01-01", "2025-04-01", "2025-07-01", "2025-10-01"]
        return [{"start_date": s, "end_date": e, "value": v, "form": "10-Q"}
                for s, e, v in zip(starts, ends, vals)]
    for ticker in ("MTB", "STT", "SYF"):
        out = financial.compute_financial(
            ticker, [], [], [], {}, sector="Financials",
            nii_rows=quarters([1700, 1710, 1720, 1730, 1740, 1750, 1760, 1770]),
            noninterest_rows=quarters([600] * 8),
            pcl_rows=quarters([120] * 8))
        assert out["sector_path"] == "bank", ticker
    # A Financials name outside the allowlist stays on the standard path.
    out = financial.compute_financial("IBKR", [], [], [], {}, sector="Financials",
                                      nii_rows=quarters([100] * 8))
    assert out["sector_path"] == "standard"


# --- carried-forward synthetic history rows ------------------------------------------


def test_no_carried_forward_rows_left_for_bk_ea_dow() -> None:
    record = _json(EVIDENCE / "_synthetic_rows_removed.json")["tickers"]
    assert {t: r["removed_count"] for t, r in record.items()} == {"BK": 87, "EA": 51, "DOW": 141}
    store = LthcsPersist(DATA)
    for t, r in record.items():
        hist = store.read_history(t)["history"]
        dates = {row["date"] for row in hist}
        assert not dates & set(r["removed_dates"]), t
        real = [row for row in hist if not row.get("synthetic")]
        assert real, t
        last_real = max(row["date"] for row in real)
        assert not [row for row in hist if row.get("synthetic") and row["date"] > last_real], t
        assert r["ticker_rows_in_those_snapshots"] == 0 and r["snapshot_files_on_removed_dates"] > 0
        assert sorted(row["date"] for row in hist if row.get("synthetic")) == r["synthetic_rows_kept"]


def test_dow_rows_recreated_by_the_2026_10_06_catch_up_are_removed() -> None:
    # The 2026-10-06 lthcs-daily --catch-up (commit 5433902b) wrote DOW's
    # 2026-05-16 score onto every day 05-17..10-05 again: DOW is active
    # (re-added 2026-10-05), and catch-up then only skipped inactive tickers.
    record = _json(EVIDENCE / "_synthetic_rows_removed_2026-10-06.json")["tickers"]
    assert {t: r["removed_count"] for t, r in record.items()} == {"DOW": 142}
    r = record["DOW"]
    assert (r["removed_from"], r["removed_to"]) == ("2026-05-17", "2026-10-05")
    assert r["ticker_rows_in_those_snapshots"] == 0 and r["snapshot_files_on_removed_dates"] == 129
    hist = LthcsPersist(DATA).read_history("DOW")["history"]
    assert not [row for row in hist if row.get("synthetic")]
    assert not {row["date"] for row in hist} & set(r["removed_dates"])


def test_every_equity_synthetic_row_is_a_gap_day_fill() -> None:
    # The catch-up rule (LthcsPersist.fill_history_gaps) on committed data: a
    # synthetic row copies the ticker's last real row, no run (snapshot file)
    # happened between that row and the synthetic day, so the day itself was
    # a missed run (health/known_gaps.json), and that real row is not older
    # than the ticker's universe added_on.
    store = LthcsPersist(DATA)
    snapshot_dates = store.list_snapshot_dates()
    bad = []
    for e in _universe():
        last_real = None
        for row in sorted(store.read_history(e["ticker"])["history"], key=lambda r: r["date"]):
            if not row.get("synthetic"):
                last_real = row
                continue
            ok = (last_real is not None
                  and not any(last_real["date"] < s <= row["date"] for s in snapshot_dates)
                  and (row["score"], row["band"]) == (last_real["score"], last_real["band"])
                  and (not e.get("added_on") or last_real["date"] >= e["added_on"]))
            if not ok:
                bad.append((e["ticker"], row["date"]))
    assert not bad, bad[:10]


def test_carried_forward_rule_keeps_real_rows_and_gap_day_fills() -> None:
    hist = [
        {"date": "2026-07-01", "score": 50.0, "band": "monitor"},
        {"date": "2026-07-02", "score": 50.0, "band": "monitor", "synthetic": True},  # gap day
        {"date": "2026-07-03", "score": 51.0, "band": "monitor"},
        {"date": "2026-07-04", "score": 51.0, "band": "monitor", "synthetic": True},
        {"date": "2026-07-05", "score": 51.0, "band": "monitor", "synthetic": True},  # gap day
        {"date": "2026-07-06", "score": 49.0, "band": "monitor", "synthetic": True},  # odd value
    ]
    snapshot_dates = {"2026-07-01", "2026-07-03", "2026-07-04", "2026-07-06"}
    remove, ambiguous = carried_forward_rows(hist, {"2026-07-01", "2026-07-03"}, snapshot_dates)
    assert [r["date"] for r in remove] == ["2026-07-04", "2026-07-05"]
    assert [r["date"] for r in ambiguous] == ["2026-07-06"]
    # The same run is kept when the ticker was scored on one of its days.
    remove, ambiguous = carried_forward_rows(hist, {"2026-07-01", "2026-07-03", "2026-07-06"},
                                             snapshot_dates)
    assert remove == [] and ambiguous == []


def test_drop_synthetic_entries_never_removes_real_rows(tmp_path: Path) -> None:
    store = LthcsPersist(tmp_path)
    store.append_history_entry("XYZ", "2026-07-01", 55.0, "monitor", "v1")
    store.fill_history_gaps(today="2026-07-04", tickers=["XYZ"])
    assert store.drop_synthetic_entries("XYZ", ["2026-07-01", "2026-07-02"]) == 1
    left = [(r["date"], bool(r.get("synthetic"))) for r in store.read_history("XYZ")["history"]]
    assert sorted(left) == [("2026-07-01", False), ("2026-07-03", True)]
