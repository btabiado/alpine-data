"""Tests for ``scripts.lthcs_leaderboards``.

The helper is a pure transform — given a snapshot dict (and optional universe
dict for scope filtering), it should produce 7 deterministic ranked lists.

We test the contract, not the snapshot-on-disk: every test builds its own
small synthetic snapshot so it stays stable across daily data refreshes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from scripts import lthcs_leaderboards as mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ticker_row(
    ticker: str,
    composite: float,
    pillars: Dict[str, float],
    band: str = "monitor",
    index_membership: List[str] | None = None,
) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "lthcs_score": composite,
        "band": band,
        "subscores": pillars,
        "_index_membership": index_membership or ["S&P 100"],
    }


@pytest.fixture()
def snapshot() -> Dict[str, Any]:
    """Synthetic 12-row snapshot — enough to exercise top-N truncation and
    tie-breaking, but small enough to reason about by hand.

    Layout:
      AAA-LLL composites 95→40 (descending in 5pt steps)
      Pillar values give each pillar a unique winner to spot-check.
    """
    scores: List[Dict[str, Any]] = []
    composites = [95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40]
    tickers = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF",
               "GGG", "HHH", "III", "JJJ", "KKK", "LLL"]
    for tkr, comp in zip(tickers, composites):
        # Default pillars track the composite. Override per row below.
        scores.append(
            _ticker_row(
                tkr,
                comp,
                {
                    "adoption_momentum": comp,
                    "institutional_confidence": comp,
                    "financial_evolution": comp,
                    "thesis_integrity": comp,
                    "des": comp,
                },
            )
        )

    # Unique pillar winners — each gets a 100 on exactly one pillar and a
    # depressed composite, so they only top the pillar they own.
    overrides = {
        "GGG": ("adoption_momentum", 100.0),
        "HHH": ("institutional_confidence", 100.0),
        "III": ("financial_evolution", 100.0),
        "JJJ": ("thesis_integrity", 100.0),
        "KKK": ("des", 100.0),
    }
    for row in scores:
        if row["ticker"] in overrides:
            key, val = overrides[row["ticker"]]
            row["subscores"][key] = val

    return {"calc_date": "2026-05-18", "scores": scores}


@pytest.fixture()
def universe(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Universe synthesized so we can exercise scope filtering. Half the
    tickers are DJIA, the other half NASDAQ-100 only.
    """
    tickers = []
    for i, row in enumerate(snapshot["scores"]):
        members = ["S&P 100"]
        if i % 2 == 0:
            members.append("DJIA")
        else:
            members.append("NASDAQ-100")
        tickers.append({
            "ticker": row["ticker"],
            "active": True,
            "index_membership": members,
        })
    # One inactive ticker that mustn't appear in any scope set.
    tickers.append({
        "ticker": "DEAD",
        "active": False,
        "index_membership": ["DJIA", "S&P 100", "NASDAQ-100"],
    })
    return {"tickers": tickers}


# ---------------------------------------------------------------------------
# compute_leaderboards
# ---------------------------------------------------------------------------


def test_composite_top_is_descending(snapshot):
    out = mod.compute_leaderboards(snapshot)
    assert [e["ticker"] for e in out["composite_top"]] == [
        "AAA", "BBB", "CCC", "DDD", "EEE", "FFF",
        "GGG", "HHH", "III", "JJJ",
    ]
    scores = [e["pillar_score"] for e in out["composite_top"]]
    assert scores == sorted(scores, reverse=True)


def test_composite_bottom_is_ascending(snapshot):
    out = mod.compute_leaderboards(snapshot)
    bottoms = [e["ticker"] for e in out["composite_bottom"]]
    # Lowest 10 of 12, ascending.
    assert bottoms == [
        "LLL", "KKK", "JJJ", "III", "HHH", "GGG",
        "FFF", "EEE", "DDD", "CCC",
    ]


def test_pillar_top_picks_pillar_winner(snapshot):
    out = mod.compute_leaderboards(snapshot)
    # GGG owns adoption_momentum with a 100 despite a depressed composite.
    assert out["pillars"]["adoption_momentum"][0]["ticker"] == "GGG"
    assert out["pillars"]["institutional_confidence"][0]["ticker"] == "HHH"
    assert out["pillars"]["financial_evolution"][0]["ticker"] == "III"
    assert out["pillars"]["thesis_integrity"][0]["ticker"] == "JJJ"
    assert out["pillars"]["des"][0]["ticker"] == "KKK"


def test_pillar_winner_carries_its_composite(snapshot):
    """Each row should carry both the pillar score being ranked AND the
    overall composite, so the page can show both."""
    out = mod.compute_leaderboards(snapshot)
    winner = out["pillars"]["adoption_momentum"][0]
    assert winner["ticker"] == "GGG"
    assert winner["pillar_score"] == 100.0
    # GGG's composite in the fixture is 65 (7th of 12).
    assert winner["composite"] == 65


def test_top_n_truncation(snapshot):
    out = mod.compute_leaderboards(snapshot, top_n=3)
    assert len(out["composite_top"]) == 3
    assert len(out["composite_bottom"]) == 3
    for arr in out["pillars"].values():
        assert len(arr) == 3


def test_ticker_set_filter_restricts_universe(snapshot):
    only = {"AAA", "BBB", "CCC"}
    out = mod.compute_leaderboards(snapshot, ticker_set=only)
    # Only the 3 in-scope tickers should appear, period.
    for board in [out["composite_top"], out["composite_bottom"]]:
        assert {e["ticker"] for e in board} <= only
    for arr in out["pillars"].values():
        assert {e["ticker"] for e in arr} <= only


def test_deterministic_tie_break_by_ticker_asc():
    """Two tickers with identical scores should order by ticker A→Z."""
    snap = {
        "calc_date": "2026-05-18",
        "scores": [
            _ticker_row("ZZZ", 80.0, {"adoption_momentum": 50.0,
                                       "institutional_confidence": 50.0,
                                       "financial_evolution": 50.0,
                                       "thesis_integrity": 50.0,
                                       "des": 50.0}),
            _ticker_row("AAA", 80.0, {"adoption_momentum": 50.0,
                                       "institutional_confidence": 50.0,
                                       "financial_evolution": 50.0,
                                       "thesis_integrity": 50.0,
                                       "des": 50.0}),
        ],
    }
    out = mod.compute_leaderboards(snap)
    assert [e["ticker"] for e in out["composite_top"]] == ["AAA", "ZZZ"]


# ---------------------------------------------------------------------------
# Scope filtering (via universe.json)
# ---------------------------------------------------------------------------


def test_load_universe_returns_none_for_all(universe):
    assert mod.load_universe_index_members(universe, "all") is None


def test_load_universe_djia_excludes_inactive(universe):
    djia = mod.load_universe_index_members(universe, "djia")
    assert isinstance(djia, set)
    assert "DEAD" not in djia
    # Even-indexed tickers from the snapshot fixture are DJIA — that's 6.
    assert len(djia) == 6


def test_load_universe_rejects_unknown_scope(universe):
    with pytest.raises(ValueError):
        mod.load_universe_index_members(universe, "russell-2000")


# ---------------------------------------------------------------------------
# find_latest_snapshot
# ---------------------------------------------------------------------------


def test_find_latest_snapshot_picks_alphabetically_last(tmp_path: Path):
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    for date in ("2026-05-15", "2026-05-17", "2026-05-16"):
        (snap_dir / f"{date}.json").write_text("{}")
    # Decoy that must be ignored.
    (snap_dir / "index.json").write_text("{}")

    latest = mod.find_latest_snapshot(tmp_path)
    assert latest.name == "2026-05-17.json"


def test_find_latest_snapshot_raises_when_empty(tmp_path: Path):
    (tmp_path / "snapshots").mkdir()
    with pytest.raises(FileNotFoundError):
        mod.find_latest_snapshot(tmp_path)


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_cli_json_smoke(tmp_path: Path, snapshot, capsys):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snapshot))

    rc = mod.main([
        "--snapshot", str(snap_path),
        "--data-root", str(tmp_path),  # unused for --scope all
        "--json",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["calc_date"] == "2026-05-18"
    assert payload["scope"] == "all"
    assert "composite_top" in payload["boards"]
    assert payload["boards"]["composite_top"][0]["ticker"] == "AAA"


def test_cli_pretty_smoke(tmp_path: Path, snapshot, capsys):
    snap_path = tmp_path / "snap.json"
    snap_path.write_text(json.dumps(snapshot))

    rc = mod.main([
        "--snapshot", str(snap_path),
        "--data-root", str(tmp_path),
    ])
    assert rc == 0
    out = capsys.readouterr().out
    # Smoke: title for each of the 7 boards appears.
    assert "Top 10 by Composite" in out
    assert "Top 10 by Adoption Momentum" in out
    assert "Top 10 by Institutional Confidence" in out
    assert "Top 10 by Financial Evolution" in out
    assert "Top 10 by Thesis Integrity" in out
    assert "Top 10 by DES" in out
    assert "Bottom 10 by Composite" in out
