"""Universe ``added_on``: when a ticker joined universe.json, and what the
validators may expect of snapshots before that.

2026-10-05's snapshot was committed at 01:59Z and the S&P 500 sync added 300
tickers at 02:08Z (commit 6dbf23cc), so the weekly validator called all 300
absent from every snapshot ever taken. Covers the schema, the committed data
against git, the daily validator (lthcs.validate --date) and the sync tool.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict

import pytest
from pydantic import ValidationError

from lthcs import validate
from lthcs.schemas.universe import Universe, UniverseEntry
from scripts import lthcs_universe_sp500_sync as sync

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "data" / "lthcs"
SYNC_SOURCE = "sp500_sync_2026-10-05"


def _entry(ticker: str, **extra: Any) -> Dict[str, Any]:
    e = {"ticker": ticker, "name": ticker + " Inc.", "exchange": "NYSE",
         "index_membership": ["S&P 500"], "sector": "Industrials",
         "industry": "Machinery", "maturity_stage": "standard_compounder", "active": True}
    e.update(extra)
    return e


def test_schema_accepts_an_iso_added_on_and_rejects_prose() -> None:
    assert str(UniverseEntry.model_validate(_entry("A", added_on="2026-10-05")).added_on) == "2026-10-05"
    assert UniverseEntry.model_validate(_entry("A")).added_on is None
    with pytest.raises(ValidationError):
        UniverseEntry.model_validate(_entry("A", added_on="late May 2026"))


def test_committed_add_dates_are_exactly_the_sync_batch() -> None:
    uni = json.loads((DATA / "universe.json").read_text(encoding="utf-8"))
    stamped = {e["ticker"]: e["added_on"] for e in uni["tickers"] if e.get("added_on")}
    batch = {e["ticker"] for e in uni["tickers"] if e.get("source") == SYNC_SOURCE}
    assert len(batch) == 300
    assert set(stamped) == batch
    assert set(stamped.values()) == {"2026-10-05"}


def test_committed_add_dates_match_git() -> None:
    """The stamp is evidence, not a guess: the batch first appears in
    universe.json in the 2026-10-05 sync commit."""
    def tickers(rev: str) -> set:
        out = subprocess.run(["git", "show", f"{rev}:data/lthcs/universe.json"],
                             cwd=REPO_ROOT, capture_output=True, text=True)
        if out.returncode != 0:
            pytest.skip(f"git history unavailable ({out.stderr.strip()[:80]})")
        return {e["ticker"] for e in json.loads(out.stdout)["tickers"]}

    added = tickers("6dbf23cc") - tickers("6dbf23cc^")
    uni = json.loads((DATA / "universe.json").read_text(encoding="utf-8"))
    assert added == {e["ticker"] for e in uni["tickers"] if e.get("added_on") == "2026-10-05"}


def _write_day(data: Path, day: str, tickers) -> None:
    for sub in ("snapshots", "variable_detail", "narratives", "history/by_ticker"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    scores = [{"ticker": t, "lthcs_score": 55.0,
               "subscores": {k: 55.0 for k in validate.PILLAR_KEYS}} for t in tickers]
    (data / "snapshots" / f"{day}.json").write_text(json.dumps({"scores": scores}))
    (data / "variable_detail" / f"{day}.json").write_text(json.dumps({"variables": []}))
    (data / "narratives" / f"{day}.json").write_text(
        json.dumps({"narratives": [{"ticker": t} for t in tickers]}))
    for t in tickers:
        (data / "history" / "by_ticker" / f"{t}.json").write_text(
            json.dumps({"history": [{"date": day}]}))


def test_daily_validator_expects_an_added_ticker_from_the_next_day(tmp_path, monkeypatch) -> None:
    data = tmp_path / "lthcs"
    monkeypatch.setattr(validate, "DATA_DIR", data)
    universe = Universe.model_validate({"version": "t", "last_updated": "2026-10-05", "tickers": [
        _entry("AAPL"), _entry("A", added_on="2026-10-05")]})
    _write_day(data, "2026-10-05", ["AAPL"])
    assert validate.validate_snapshot_for_date("2026-10-05", universe) is True
    _write_day(data, "2026-10-06", ["AAPL"])
    assert validate.validate_snapshot_for_date("2026-10-06", universe) is False


def test_sync_stamps_new_entries_with_their_add_date(tmp_path, monkeypatch) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "universe.json").write_text(json.dumps({
        "version": "3.0.0", "last_updated": "2026-10-05", "description": "t",
        "tickers": [_entry("AAA")]}), encoding="utf-8")
    (data / "sp500_candidate_seed.json").write_text(json.dumps({"tickers": [
        {"symbol": "NEWC", "name": "New Co", "inferred_maturity_stage": "standard_compounder"}]}),
        encoding="utf-8")
    cand = tmp_path / "cand"
    cand.mkdir()
    (cand / "_source.json").write_text(json.dumps({
        "fetched_at": "2026-10-05",
        "sp500": {"url": "https://example.test/sp500", "revision_id": 1},
        "djia": {"url": "https://example.test/djia", "revision_id": 2, "components": []}}),
        encoding="utf-8")
    (cand / "_sec_exchange.json").write_text(json.dumps({"NEWC": "NYSE"}), encoding="utf-8")
    (cand / "_yahoo_metrics.json").write_text(json.dumps({"tickers": {}}), encoding="utf-8")
    (cand / "_constituents_sp500.csv").write_text(
        "Symbol,Security,GICS Sector,GICS Sub-Industry,Date added,CIK\n"
        "AAA,AAA Inc.,Information Technology,Application Software,2020-01-01,1\n"
        "NEWC,New Co,Information Technology,Application Software,2026-09-22,2\n",
        encoding="utf-8")
    monkeypatch.setattr(sync, "DATA", data)
    result = sync.build(cand, apply_exiles=False)
    assert [e["ticker"] for e in result["new"]] == ["NEWC"]
    new = result["new"][0]
    assert new["added_on"] == sync.TODAY
    new.pop("_peer_group")
    UniverseEntry.model_validate(new)
