"""Composite-index snapshot honesty tests (``scripts/snapshot_composites.py``).

The composites written here are the daily history behind the dashboard's
gauges, so a wrong date in this directory is permanent — it cannot be
recomputed later. Two rules dominate:

  * a composite is only as fresh as its OLDEST contributing input (min);
  * the number of cache-served inputs is disclosed alongside the date,
    because a date alone under-reports a partially-frozen breadth index.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def sc():
    """Load the script by path — scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(
        "snapshot_composites", REPO_ROOT / "scripts" / "snapshot_composites.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- helpers ----------

def test_oldest_takes_the_min(sc):
    assert sc._oldest(["2026-08-01", "2026-06-09", "2026-07-15"]) == "2026-06-09"


def test_oldest_ignores_junk_and_empties(sc):
    assert sc._oldest([]) is None
    assert sc._oldest(None) is None
    assert sc._oldest([None, "", 42, "short"]) is None
    assert sc._oldest(["2026-06-09T10:00:00Z"]) == "2026-06-09"


def test_poc_entry_date_prefers_pinned_as_of(sc):
    assert sc._poc_entry_date({"as_of": "2026-06-09",
                               "signal_history": [{"date": "2026-08-01"}]}) == "2026-06-09"


def test_poc_entry_date_falls_back_to_signal_history(sc):
    assert sc._poc_entry_date({"signal_history": [{"date": "2026-07-04"}]}) == "2026-07-04"
    assert sc._poc_entry_date({}) is None
    assert sc._poc_entry_date(None) is None


# ---------- collect() ----------

def _write_caches(tmp_path, market, whale=None):
    (tmp_path / "market.json").write_text(json.dumps(market))
    (tmp_path / "whale.json").write_text(json.dumps(whale or {}))


def test_crypto_signal_sentiment_uses_oldest_row_not_build_stamp(sc, tmp_path, monkeypatch):
    """Regression: as_of used to be market.generated_at — the payload's
    build stamp, current on every run regardless of the coins beneath it."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "generated_at": "2026-08-02T20:40:11Z",
        "signals_top20": [
            {"symbol": "BTC", "score": 60, "as_of": "2026-08-01"},
            {"symbol": "ETH", "score": -60, "as_of": "2026-06-09", "stale": True},
            {"symbol": "SOL", "score": 30, "as_of": "2026-08-01"},
        ],
    })
    got = sc.collect()["crypto_signal_sentiment"]
    assert got["as_of"] == "2026-06-09"
    assert got["as_of"] != "2026-08-02"
    assert got["stale"] is True
    assert "1 of 3 cached" in got["note"]


def test_crypto_signal_sentiment_clean_run_has_no_cache_note(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "generated_at": "2026-08-02T20:40:11Z",
        "signals_top20": [
            {"symbol": "BTC", "score": 60, "as_of": "2026-08-02"},
            {"symbol": "ETH", "score": 30, "as_of": "2026-08-02"},
        ],
    })
    got = sc.collect()["crypto_signal_sentiment"]
    assert got["as_of"] == "2026-08-02"
    assert got["stale"] is False
    assert "cached" not in got["note"]


def test_crypto_signal_sentiment_undated_rows_give_no_date(sc, tmp_path, monkeypatch):
    """Rule 5: no honest date -> None recorded, never the build stamp."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "generated_at": "2026-08-02T20:40:11Z",
        "signals_top20": [{"symbol": "BTC", "score": 60}],
    })
    assert sc.collect()["crypto_signal_sentiment"]["as_of"] is None


def test_poc_breadth_takes_oldest_contributing_coin(sc, tmp_path, monkeypatch):
    """Regression: this took max(dates), so one live coin masked a list of
    carried-forward ones."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"poc_top": [
        {"coin_id": "bitcoin", "as_of": "2026-08-01",
         "signal_history": [{"date": "2026-08-01", "score": 20}]},
        {"coin_id": "ethereum", "as_of": "2026-06-09", "stale": True,
         "signal_history": [{"date": "2026-06-09", "score": -40}]},
    ]})
    got = sc.collect()["poc_signal_breadth"]
    assert got["as_of"] == "2026-06-09"
    assert got["stale"] is True
    assert "1 of 2 cached" in got["note"]
    assert got["score"] == -10  # mean of 20 and -40


def test_poc_breadth_backfills_date_from_signal_history(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"poc_top": [
        {"coin_id": "bitcoin", "signal_history": [{"date": "2026-07-04", "score": 10}]},
    ]})
    assert sc.collect()["poc_signal_breadth"]["as_of"] == "2026-07-04"


def test_crypto_signal_sentiment_recomputed_from_markets_top(sc, tmp_path, monkeypatch):
    """`signals_top20` only exists in the builders' in-memory payload, never
    in data/market.json — which is why every committed snapshot recorded
    null for this gauge. Recompute it from markets_top, and the recomputed
    entries must carry the rows' own observation dates."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    spark = [100.0 + i * 0.1 for i in range(168)]
    _write_caches(tmp_path, {
        "generated_at": "2026-08-02T20:40:11Z",
        "markets_top": [
            {"symbol": "BTC", "name": "Bitcoin", "rank": 1, "price_usd": 1.0,
             "market_cap_usd": 1e12, "volume_24h_usd": 1e10,
             "change_24h_pct": 1.0, "change_7d_pct": 9.0, "change_30d_pct": 20.0,
             "sparkline_7d": spark, "as_of": "2026-08-01"},
            {"symbol": "ETH", "name": "Ether", "rank": 2, "price_usd": 1.0,
             "market_cap_usd": 5e11, "volume_24h_usd": 1e10,
             "change_24h_pct": -1.0, "change_7d_pct": -9.0, "change_30d_pct": -20.0,
             "sparkline_7d": spark[::-1], "as_of": "2026-06-09", "stale": True},
        ],
    })
    got = sc.collect()["crypto_signal_sentiment"]
    assert got is not None, "gauge must no longer be permanently null"
    assert got["as_of"] == "2026-06-09"
    assert got["stale"] is True
    assert "1 of 2 cached" in got["note"]


def test_crypto_signal_sentiment_recompute_survives_empty_markets_top(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"markets_top": []})
    assert sc.collect()["crypto_signal_sentiment"] is None


def test_collect_missing_inputs_are_null_not_absent(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {})
    idx = sc.collect()
    assert idx["crypto_signal_sentiment"] is None
    assert idx["poc_signal_breadth"] is None


# ---------- whale composites: the archive must not be poisoned ----------
# whale_sentiment_btc / _eth persisted ``sentiment.as_of``, which at the time
# was ``whale["fetched_at"][:10]`` — the fetch clock. Writing that into the
# composite HISTORY is worse than showing it on a page: the history is the
# only record, so a run that served entirely stale on-chain numbers would be
# archived forever as a fresh observation.

BASIS = "oldest contributing on-chain series"


def test_whale_sentiment_date_refuses_a_pre_fix_payload(sc):
    """No `as_of_basis` ⇒ the as_of is the old clock read ⇒ refuse it."""
    assert sc._whale_sentiment_date({"as_of": "2026-08-02"}) is None
    assert sc._whale_sentiment_date({"as_of": "2026-08-02",
                                     "as_of_basis": BASIS}) == "2026-08-02"
    assert sc._whale_sentiment_date(None) is None
    assert sc._whale_sentiment_date({"as_of_basis": BASIS}) is None


def test_whale_btc_snapshot_uses_the_composite_observation_date(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {}, {
        "fetched_at": "2026-08-02T23:00:00+00:00",
        "sentiment": {"score": -12, "label": "NEUTRAL",
                      "as_of": "2026-06-09", "as_of_basis": BASIS},
        "btc": {"tx_volume_usd": [{"date": "2026-08-01", "value": 1}]},
    })
    got = sc.collect()["whale_sentiment_btc"]
    assert got["as_of"] == "2026-06-09"


def test_whale_btc_snapshot_falls_back_to_the_oldest_series(sc, tmp_path, monkeypatch):
    """Pre-fix payload: refuse its as_of and date it from the raw series —
    the OLDEST of them, not one hand-picked series' last date."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {}, {
        "fetched_at": "2026-08-02T23:00:00+00:00",
        "sentiment": {"score": -12, "label": "NEUTRAL", "as_of": "2026-08-02"},
        "btc": {
            "tx_volume_usd":      [{"date": "2026-08-01", "value": 1}],
            "hash_rate":          [{"date": "2026-08-01", "value": 1}],
            "active_addresses":   [{"date": "2026-06-09", "value": 1}],
        },
    })
    got = sc.collect()["whale_sentiment_btc"]
    assert got["as_of"] == "2026-06-09"
    assert got["as_of"] != "2026-08-02", "fetch clock leaked into the archive"


def test_whale_eth_snapshot_dates_from_its_own_series(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {}, {
        "fetched_at": "2026-08-02T23:00:00+00:00",
        "eth": {
            "sentiment": {"score": 5, "label": "NEUTRAL", "as_of": "2026-08-02"},
            "coin_metrics": {"AdrActCnt": [{"date": "2026-07-30", "value": 1}],
                             "TxCnt":     [{"date": "2026-06-09", "value": 1}]},
            "etherscan_daily": {"series": [{"date": "2026-08-01", "value": 7200}]},
        },
    })
    got = sc.collect()["whale_sentiment_eth"]
    assert got["as_of"] == "2026-06-09"


def test_whale_snapshot_has_no_date_when_nothing_is_datable(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {}, {
        "fetched_at": "2026-08-02T23:00:00+00:00",
        "sentiment": {"score": 3, "label": "NEUTRAL", "as_of": "2026-08-02"},
        "btc": {},
    })
    got = sc.collect()["whale_sentiment_btc"]
    assert got["as_of"] is None


# =====================================================================
# The five composites PR #25 made clickable with nothing to plot
# =====================================================================
# overview_sentiment / defi_sentiment / etf_flow_sentiment /
# futures_sentiment / stocks_signal_breadth were computed in the browser at
# render time and thrown away, so their cards opened a history modal over an
# archive that had never recorded them.
#
# Two properties dominate, and they pull in opposite directions:
#
#   * the archived number must be THE NUMBER THE CARD SHOWED — the arithmetic
#     is a port of the shipped JS, down to Math.round's half-up rounding and
#     sentimentBucket's label ladder; and
#   * where an input is ABSENT the archive records null even though the same
#     JS coerces that absence to 0. A zero is a reading, null is a gap, and a
#     chart has to be able to tell them apart.

ALL_NEW_KEYS = (
    "overview_sentiment", "defi_sentiment", "stocks_signal_breadth",
    "etf_flow_sentiment", "etf_flow_sentiment_btc", "etf_flow_sentiment_eth",
    "futures_sentiment", "futures_sentiment_btc", "futures_sentiment_eth",
    "futures_sentiment_link", "futures_sentiment_ltc",
)


def _series(field, values, start="2026-07-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [{"date": (d0 + timedelta(days=i)).isoformat(), field: v}
            for i, v in enumerate(values)]


def _etf_csv(tmp_path, asset, rows):
    lines = ["date,FUNDA,FUNDB,Total"]
    for d, a, b in rows:
        lines.append(f"{d},{a},{b},{a + b}")
    (tmp_path / f"{asset}_flows.csv").write_text("\n".join(lines) + "\n")


# ---------- ported-arithmetic helpers ----------

def test_helpers_match_javascript_semantics(sc):
    """Math.round is half-UP; Python's round() is half-to-EVEN. A composite
    archived at 2 that the card painted as 3 is precisely the class of quiet
    lie this directory exists to prevent."""
    assert sc._jsround(2.5) == 3           # Python's round() would give 2
    assert sc._jsround(-2.5) == -2         # JS Math.round(-2.5) === -2
    assert sc._jsround(-0.5) == 0
    assert sc._clamp(1e9) == 100 and sc._clamp(-1e9) == -100
    assert sc._clamp("nope") is None and sc._clamp(None) is None
    ladder = ["A", "B", "C", "D", "E"]
    assert [sc._bucket(v, ladder) for v in (50, 20, 19, -20, -50)] == \
        ["A", "B", "C", "D", "E"]


def test_mean_score_of_nothing_is_none_not_zero(sc):
    """The whole contract in one assertion: absence must not become a
    reading of neutral."""
    assert sc._mean_score([]) is None
    assert sc._mean_score([None, None]) is None
    assert sc._mean_score([0]) == 0


# ---------- overview_sentiment ----------

def test_overview_takes_the_oldest_dated_input(sc, tmp_path, monkeypatch):
    """F&G is from yesterday, the one signal row is 8 weeks old: the
    composite is as old as the signal row, never the freshest of the three."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    spark = [100.0 + i * 0.1 for i in range(168)]
    _write_caches(tmp_path, {
        "fetched_at": "2026-08-03T01:00:00Z",
        "fear_greed": [{"date": "2026-08-02", "value": 66}],
        "coinbase_intl_perps": [
            {"symbol": "BTC", "funding_rate": 0.0001, "as_of": "2026-08-01"}],
        "markets_top": [
            {"symbol": "BTC", "name": "B", "rank": 1, "price_usd": 1.0,
             "market_cap_usd": 1e12, "volume_24h_usd": 1e10,
             "change_24h_pct": 1.0, "change_7d_pct": 9.0, "change_30d_pct": 20.0,
             "sparkline_7d": spark, "as_of": "2026-06-09", "stale": True},
        ],
    })
    got = sc.collect()["overview_sentiment"]
    assert got["as_of"] == "2026-06-09"
    assert got["as_of"] != "2026-08-03", "fetch clock leaked into the archive"
    assert got["stale"] is True
    assert "1 of 1 signal rows cached" in got["note"]
    assert got["label"] in ("STRONG BULLISH", "BULLISH", "NEUTRAL",
                            "BEARISH", "STRONG BEARISH")


def test_overview_discloses_inputs_that_carry_no_date(sc, tmp_path, monkeypatch):
    """A min taken over a subset, reported bare, implies the whole composite
    is that fresh."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "fear_greed": [{"value": 80}],                       # no date
        "coinbase_intl_perps": [
            {"symbol": "BTC", "funding_rate": 0.0002, "as_of": "2026-07-20"}],
    })
    got = sc.collect()["overview_sentiment"]
    assert got["as_of"] == "2026-07-20"
    assert "1 carrying no observation date" in got["note"]


def test_overview_refuses_a_pre_provenance_signals_payload(sc, tmp_path, monkeypatch):
    """signals_top20[].as_of was scoring time before the signals.py fix.
    Rows without `computed_at` contribute NO date — they are counted as
    undated instead, exactly as signalsTop20Freshness() does."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "signals_top20": [{"symbol": "BTC", "score": 40, "as_of": "2026-08-03"}],
    })
    got = sc.collect()["overview_sentiment"]
    assert got["as_of"] is None
    assert "1 carrying no observation date" in got["note"]


def test_overview_is_null_when_nothing_contributes(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {})
    assert sc.collect()["overview_sentiment"] is None


# ---------- defi_sentiment ----------

def test_defi_uses_the_subtree_observation_date(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "fetched_at": "2026-08-03T01:00:00Z",
        "defillama": {"stablecoin_mcap_usd": 2e11,
                      "stablecoin_7d_change_usd": 2e9},
        "defi": {"as_of": "2026-07-21",
                 "chains": [{"tvl_usd": 6e10, "change_7d_pct": 3.0}]},
    })
    got = sc.collect()["defi_sentiment"]
    assert got["as_of"] == "2026-07-21"
    assert "date from defi.as_of" in got["note"]


def test_defi_falls_back_to_the_oldest_tvl_series(sc, tmp_path, monkeypatch):
    """No fetcher stamp: date it from the daily TVL history the same way
    app.py's defi_observation_date does — newest bucket WITHIN a chain, then
    the OLDEST chain, because the tab charts them side by side."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "fetched_at": "2026-08-03T01:00:00Z",
        "defi": {
            "chains": [{"tvl_usd": 6e10, "change_7d_pct": -2.0}],
            "tvl_history": {
                "Ethereum": _series("value", [1, 2], start="2026-08-01"),
                "Solana":   _series("value", [1, 2], start="2026-07-20"),
            },
        },
    })
    got = sc.collect()["defi_sentiment"]
    assert got["as_of"] == "2026-07-21"           # Solana's last bucket
    assert got["as_of"] != "2026-08-03", "fetch clock leaked into the archive"
    assert "oldest daily TVL series" in got["note"]


def test_defi_is_null_when_the_subtree_is_missing(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"defi": {}, "defillama": {}})
    assert sc.collect()["defi_sentiment"] is None


# ---------- etf_flow_sentiment ----------

def test_etf_flow_dates_from_the_last_daily_row(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {})
    _etf_csv(tmp_path, "btc", [("2026-05-10", 100.0, 50.0),
                               ("2026-05-11", 60.0, 40.0),
                               ("2026-05-12", 10.0, 5.0)])
    got = sc.collect()["etf_flow_sentiment_btc"]
    assert got["as_of"] == "2026-05-12"
    # 7d sum == 30d sum == 265 -> (265/500)*100 = 53 in both windows
    assert got["score"] == 53
    assert got["label"] == "STRONG INFLOWS"
    assert "3 daily rows" in got["note"]


def test_etf_flow_reads_the_total_column_case_insensitively(sc, tmp_path, monkeypatch):
    """ensure_total() matches `Total` case-insensitively and otherwise sums
    the per-fund columns, and load_csv strips $ , and accounting parens.
    This reader has to agree or the archived number stops being the card's."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {})
    (tmp_path / "btc_flows.csv").write_text(
        'date,IBIT,GBTC\n2026-05-12,"1,200.0",(200.0)\n')
    got = sc.collect()["etf_flow_sentiment_btc"]
    assert got["score"] == 100          # (1000/500)*100, clamped
    assert got["as_of"] == "2026-05-12"


def test_etf_flow_keeps_an_unreported_row_out_of_the_sum_but_in_the_window(
        sc, tmp_path, monkeypatch):
    """The card's windows are the last 7/30 ROWS. Dropping a row with no
    number would quietly pull an eighth trading day into the 7d sum, and
    counting it as 0 would invent a day of flat flows. It stays in the
    window and out of the sum, exactly as flowVal() does with NaN."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {})
    (tmp_path / "btc_flows.csv").write_text(
        "date,IBIT,Total\n"
        "2026-05-10,100.0,100.0\n"
        "2026-05-11,,\n"                       # reported nothing
        "2026-05-12,150.0,150.0\n")
    rows = sc._etf_daily_totals(tmp_path / "btc_flows.csv")
    assert [d for d, _ in rows] == ["2026-05-10", "2026-05-11", "2026-05-12"]
    assert rows[1][1] is None                  # absence, not 0.0
    got = sc.collect()["etf_flow_sentiment_btc"]
    assert got["score"] == 50                  # (250/500)*100, the None skipped
    assert got["as_of"] == "2026-05-12"


def test_etf_flow_missing_csv_is_null_not_zero(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {})
    idx = sc.collect()
    assert idx["etf_flow_sentiment_btc"] is None
    assert idx["etf_flow_sentiment_eth"] is None
    assert idx["etf_flow_sentiment"] is None


# ---------- futures_sentiment (per asset) ----------

def test_futures_is_archived_per_asset(sc, tmp_path, monkeypatch):
    """The four assets diverge — BTC crowded long while ETH is crowded short.
    One blended number would archive a value no card ever displayed."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "fetched_at": "2026-08-03T01:00:00Z",
        "btc": {"funding": _series("rate", [0.0005], start="2026-08-01"),
                "long_short_ratio": _series("ratio", [2.0], start="2026-08-01")},
        "eth": {"funding": _series("rate", [-0.0005], start="2026-07-10"),
                "long_short_ratio": _series("ratio", [0.5], start="2026-07-10")},
    })
    idx = sc.collect()
    assert idx["futures_sentiment_btc"]["score"] == 100
    assert idx["futures_sentiment_btc"]["label"] == "STRONG CROWDED LONGS"
    assert idx["futures_sentiment_btc"]["as_of"] == "2026-08-01"
    assert idx["futures_sentiment_eth"]["score"] == -100
    assert idx["futures_sentiment_eth"]["label"] == "STRONG CROWDED SHORTS"
    assert idx["futures_sentiment_eth"]["as_of"] == "2026-07-10"
    assert idx["futures_sentiment_link"] is None
    assert idx["futures_sentiment_ltc"] is None


def test_futures_bare_key_holds_the_default_asset_and_says_so(sc, tmp_path, monkeypatch):
    """state.asset defaults to 'btc', so the registry's one-series key holds
    BTC — and the note names the key it duplicates, so the archive cannot be
    misread as two independent observations."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "btc": {"funding": _series("rate", [0.00025], start="2026-08-01")},
        "eth": {"funding": _series("rate", [-0.00025], start="2026-08-01")},
    })
    idx = sc.collect()
    assert idx["futures_sentiment"]["score"] == idx["futures_sentiment_btc"]["score"]
    assert idx["futures_sentiment"]["as_of"] == idx["futures_sentiment_btc"]["as_of"]
    assert "same series as futures_sentiment_btc" in idx["futures_sentiment"]["note"]
    assert idx["futures_sentiment"]["score"] != idx["futures_sentiment_eth"]["score"]


def test_futures_takes_the_oldest_of_its_three_series(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"btc": {
        "funding": _series("rate", [0.0002], start="2026-08-01"),
        "long_short_ratio": _series("ratio", [1.2], start="2026-06-09"),
        "open_interest_usd": _series("oi_usd", [1e10] * 9, start="2026-07-25"),
    }})
    got = sc.collect()["futures_sentiment_btc"]
    assert got["as_of"] == "2026-06-09"
    assert "3 of 3 inputs" in got["note"]


def test_futures_absent_series_is_null_never_a_fabricated_zero(sc, tmp_path, monkeypatch):
    """DELIBERATE DIVERGENCE from the shipped card. renderFuturesSentiment
    paints +0 BALANCED for an asset with no rows at all — `isFinite(null)` is
    true in JS, so a missing funding series becomes a neutral component. The
    archive refuses: absence is null, and a chart must be able to tell a
    genuine neutral reading from no reading at all."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"btc": {"funding": [], "long_short_ratio": [],
                                     "open_interest_usd": []}})
    idx = sc.collect()
    assert idx["futures_sentiment_btc"] is None
    assert idx["futures_sentiment"] is None


# ---------- stocks_signal_breadth ----------

def test_stocks_breadth_counts_buckets_like_the_card(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"stocks_signals": [
        {"symbol": "AAPL", "score": 55, "as_of": "2026-07-31"},
        {"symbol": "MSFT", "score": 25, "as_of": "2026-07-31"},
        {"symbol": "NVDA", "score": 5, "as_of": "2026-07-31"},
        {"symbol": "TSLA", "score": -60, "as_of": "2026-07-31"},
    ]})
    got = sc.collect()["stocks_signal_breadth"]
    assert got["score"] == 25                       # (2-1)/4*100
    assert got["label"] == "ACCUMULATION"
    assert got["as_of"] == "2026-07-31"
    assert "4 stocks; 2 buy+, 1 sell+" in got["note"]


def test_stocks_breadth_takes_the_oldest_row_and_counts_cache(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"stocks_signals": [
        {"symbol": "AAPL", "score": 55, "as_of": "2026-07-31"},
        {"symbol": "MSFT", "score": 25, "as_of": "2026-06-09", "stale": True},
        {"symbol": "TSLA", "score": -60, "history": [{"date": "2026-07-15"}]},
        {"symbol": "NEW", "score": 30},             # no date anywhere
    ]})
    got = sc.collect()["stocks_signal_breadth"]
    assert got["as_of"] == "2026-06-09"
    assert got["stale"] is True
    assert "1 of 4 cached" in got["note"]
    assert "1 of 4 carrying no observation date" in got["note"]


def test_stocks_breadth_excludes_unscored_rows_and_discloses_them(sc, tmp_path, monkeypatch):
    """DELIBERATE DIVERGENCE, same rule as the futures one: the card's
    `Number(null)` is 0, so an unscored row lands in the HOLD bucket and
    dilutes the breadth. Here it is excluded and counted."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"stocks_signals": [
        {"symbol": "AAPL", "score": 55, "as_of": "2026-07-31"},
        {"symbol": "XXXX", "score": None, "as_of": "2026-07-31"},
    ]})
    got = sc.collect()["stocks_signal_breadth"]
    assert got["score"] == 100                      # the 1 scored row is a buy
    assert "1 of 2 carrying no score" in got["note"]


def test_stocks_breadth_is_null_when_no_row_is_scored(sc, tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {"stocks_signals": [{"symbol": "XXXX"}]})
    assert sc.collect()["stocks_signal_breadth"] is None


# ---------- the archive contract as a whole ----------

def test_every_new_key_is_present_even_with_no_inputs(sc, tmp_path, monkeypatch):
    """A key absent from the file is indistinguishable from a key that was
    never tracked. Every one is emitted, null when unavailable."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {})
    idx = sc.collect()
    for key in ALL_NEW_KEYS:
        assert key in idx, key
        assert idx[key] is None, key


def test_every_entry_has_exactly_the_five_fields_the_reader_folds(sc, tmp_path, monkeypatch):
    """load_composite_history() copies score/label/as_of/stale/note and
    nothing else. A count smuggled into a sixth field would be silently
    dropped, which is why the cache disclosure rides in `note`."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    _write_caches(tmp_path, {
        "fear_greed": [{"date": "2026-08-02", "value": 66}],
        "stocks_signals": [{"symbol": "AAPL", "score": 55, "as_of": "2026-07-31"}],
        "btc": {"funding": _series("rate", [0.0002], start="2026-08-01")},
        "defi": {"as_of": "2026-07-21",
                 "chains": [{"tvl_usd": 1e10, "change_7d_pct": 1.0}]},
    })
    _etf_csv(tmp_path, "btc", [("2026-05-12", 10.0, 5.0)])
    idx = sc.collect()
    populated = {k for k, v in idx.items() if v}
    assert set(ALL_NEW_KEYS) & populated, "fixture populated nothing"
    for key, entry in idx.items():
        if entry is None:
            continue
        assert set(entry) == {"score", "label", "as_of", "stale", "note"}, key
        assert isinstance(entry["score"], (int, float)), key
        assert entry["as_of"] is None or len(entry["as_of"]) == 10, key
        assert isinstance(entry["stale"], bool), key


def test_snapshot_is_idempotent_across_the_new_keys(sc, tmp_path, monkeypatch):
    """pages.yml runs hourly on a DAILY series: an unchanged run must not
    rewrite the file, or the repo takes 24 no-op commits a day."""
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    out_dir = tmp_path / "composites"
    monkeypatch.setattr(sc, "OUT_DIR", out_dir)
    monkeypatch.setattr(sc, "REPO_ROOT", tmp_path)   # main() prints a rel path
    _write_caches(tmp_path, {
        "fear_greed": [{"date": "2026-08-02", "value": 66}],
        "stocks_signals": [{"symbol": "AAPL", "score": 55, "as_of": "2026-07-31"}],
        "btc": {"funding": _series("rate", [0.0002], start="2026-08-01")},
    })
    _etf_csv(tmp_path, "btc", [("2026-05-12", 10.0, 5.0)])
    assert sc.main() == 0
    written = sorted(out_dir.glob("*.json"))
    assert len(written) == 1
    first = written[0].read_bytes()
    assert sc.main() == 0
    assert written[0].read_bytes() == first, "rewrote an unchanged snapshot"

    # ...and a real move DOES rewrite it.
    m = json.loads((tmp_path / "market.json").read_text())
    m["stocks_signals"][0]["score"] = -80
    (tmp_path / "market.json").write_text(json.dumps(m))
    assert sc.main() == 0
    assert written[0].read_bytes() != first
    after = json.loads(written[0].read_text())["indexes"]
    assert after["stocks_signal_breadth"]["score"] == -100


def test_written_snapshot_round_trips_through_the_v1_reader(sc, tmp_path, monkeypatch):
    """The writer and the reader must agree about the schema. Fold a file
    this script actually wrote through app.load_composite_history() and
    require every new key to arrive as a plottable point dated from the
    DATA, not from the snapshot filename."""
    app = pytest.importorskip("app", reason="v1 builder not importable")
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    monkeypatch.setattr(sc, "OUT_DIR", tmp_path / "composites")
    monkeypatch.setattr(sc, "REPO_ROOT", tmp_path)   # main() prints a rel path
    _write_caches(tmp_path, {
        "fear_greed": [{"date": "2026-06-09", "value": 20}],
        "stocks_signals": [{"symbol": "AAPL", "score": 55, "as_of": "2026-07-31"}],
        "btc": {"funding": _series("rate", [0.0002], start="2026-08-01")},
        "eth": {"funding": _series("rate", [-0.0002], start="2026-07-05")},
        "defi": {"as_of": "2026-07-21",
                 "chains": [{"tvl_usd": 1e10, "change_7d_pct": 1.0}]},
    })
    _etf_csv(tmp_path, "btc", [("2026-05-12", 10.0, 5.0)])
    assert sc.main() == 0
    monkeypatch.setattr(app, "DATA_DIR", tmp_path)
    hist = app.load_composite_history()
    assert hist["snapshots"] == 1
    for key in ("overview_sentiment", "defi_sentiment", "stocks_signal_breadth",
                "etf_flow_sentiment", "etf_flow_sentiment_btc",
                "futures_sentiment", "futures_sentiment_btc",
                "futures_sentiment_eth"):
        assert key in hist["indexes"], key
        e = hist["indexes"][key]
        assert e["dated"] == 1 and e["undated"] == 0 and e["missing"] == 0, key
        assert len(e["points"]) == 1, key
    # Each point sits at its OWN observation date, not at the capture day.
    pts = {k: v["points"][0] for k, v in hist["indexes"].items() if v["points"]}
    assert pts["overview_sentiment"]["as_of"] == "2026-06-09"
    assert pts["etf_flow_sentiment_btc"]["as_of"] == "2026-05-12"
    assert pts["futures_sentiment_eth"]["as_of"] == "2026-07-05"
    assert pts["defi_sentiment"]["as_of"] == "2026-07-21"
    assert pts["overview_sentiment"]["as_of"] != pts["overview_sentiment"]["snapshot"]


def test_every_registry_key_the_cards_claim_is_written(sc):
    """PR #25 made nine composite cards clickable. Every key their registry
    asks for must exist in this writer, or the card opens a modal over an
    archive that will never hold it — which is the half-delivered state this
    round is closing."""
    import re
    writer = (REPO_ROOT / "scripts" / "snapshot_composites.py").read_text()
    for app_file in ("app.py", "v2/app.py"):
        path = REPO_ROOT / app_file
        if not path.exists():  # pragma: no cover
            continue
        js = path.read_text(encoding="utf-8")
        keys = re.findall(r"card: '\w+', key: '(\w+)'", js)
        assert len(keys) >= 9, f"{app_file}: found {len(keys)} history cards"
        for key in keys:
            assert f'"{key}"' in writer or f"'{key}'" in writer, \
                f"{app_file} charts {key}; snapshot_composites.py never writes it"
