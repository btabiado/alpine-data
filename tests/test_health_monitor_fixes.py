"""/health/ + data_health false positives and coverage gaps (audit 2026-10-04).

* whale "critical 43.5h": a 6h limit applied to a DAILY series dated yesterday
* ETF CSVs "critical 2.8d" on a Sunday: data through Friday is current
* DeFi "stale 19.5h": a date-only `as_of` read as midnight
* data/cpi.json "missing": nothing writes it (real path: data-cpi.json)
* data-mmf / data-mf-flows / data-equity-etf-flows / data-defi unwatched
* 144 of 150 "critical" rows were immutable NUFORC month caches
* no /health/ rows for City, Travel, Aviation/TSA, UAP, CPI, Metals, Supplies,
  Money Flow, Stock Flow, Real Estate
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return sys.modules[name]


@pytest.fixture(scope="module")
def bhs():
    return _load("build_health_status")


@pytest.fixture(scope="module")
def dh():
    return _load("data_health")


def _ts(*a):
    return datetime(*a, tzinfo=timezone.utc).timestamp()


def _write(p: Path, payload):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return p


# ---- day-granular date-only stamps ---------------------------------------

def test_same_day_date_only_as_of_is_zero_hours(bhs, tmp_path):
    p = _write(tmp_path / "data-defi.json", {"as_of": "2026-10-04"})
    probe = bhs._content_age_probe(p, _ts(2026, 10, 4, 19, 30))
    assert probe.age_h == 0.0
    t = bhs.THRESHOLDS["data-defi.json"]
    assert bhs.classify(probe.age_h, t) == "fresh"


def test_daily_whale_series_dated_yesterday_is_fresh(bhs, tmp_path):
    p = _write(tmp_path / "data-whale.json",
               {"fetched_at": "2026-10-04T21:00:00Z",
                "sentiment": {"as_of": "2026-10-03"},
                "eth": {"sentiment": {"as_of": "2026-10-03"}}})
    probe = bhs.resolve_age(p, _ts(2026, 10, 4, 21, 11), "data-whale.json")
    assert probe.age_h == 24.0
    assert bhs.classify(probe.age_h, bhs.THRESHOLDS["data-whale.json"]) == "fresh"
    # three days behind is still loud
    assert bhs.classify(72.0, bhs.THRESHOLDS["data-whale.json"]) == "critical"


# ---- trading-day clock -----------------------------------------------------

def test_trading_day_age_skips_weekends(bhs):
    fri = date(2026, 10, 2)
    assert bhs.trading_day_age_h(fri, _ts(2026, 10, 4, 21, 11)) == 0.0     # Sunday
    assert bhs.trading_day_age_h(fri, _ts(2026, 10, 5, 9, 15)) == pytest.approx(9.25)
    assert bhs.trading_day_age_h(fri, _ts(2026, 10, 7, 0, 0)) == pytest.approx(48.0)
    assert bhs.trading_day_age_h(date(2026, 10, 1), _ts(2026, 10, 2, 21, 0)) == pytest.approx(21.0)


def test_etf_csv_with_friday_data_is_fresh_on_sunday(bhs, tmp_path):
    p = _write(tmp_path / "btc_flows.csv", "date,Total\n2026-10-01,5\n2026-10-02,7\n")
    probe = bhs._content_age_probe(p, _ts(2026, 10, 4, 21, 11))
    assert probe.age_h == 0.0 and "weekday" in probe.key
    assert bhs.classify(probe.age_h, bhs.THRESHOLDS["btc_flows.csv"]) == "fresh"
    # a genuinely missing trading week still goes red
    late = bhs._content_age_probe(p, _ts(2026, 10, 9, 21, 0))
    assert bhs.classify(late.age_h, bhs.THRESHOLDS["btc_flows.csv"]) == "critical"


def test_data_health_uses_the_same_trading_day_budget(bhs):
    for rel in ("data/btc_flows.csv", "data/eth_flows.csv", "data/equity_etf_flows.csv"):
        assert bhs.threshold_for(rel) == bhs.THRESHOLDS[Path(rel).name], rel
        assert Path(rel).name in bhs.TRADING_DAY_FEEDS, rel


# ---- immutable month caches ------------------------------------------------

def test_immutable_month_caches_aggregate_into_one_row(bhs, tmp_path):
    stale = tmp_path / "data" / ".stale"
    for ym in ("201407", "201408", "202606"):
        _write(stale / f"nuforc_subndx_{ym}.json", {"rows": []})
    this_month = datetime.now(timezone.utc).strftime("%Y%m")
    _write(stale / f"nuforc_subndx_{this_month}.json", {"rows": []})   # still mutable
    _write(stale / "fetch_fred.json", {"fetched_at": "2026-01-01T00:00:00Z"})
    rows = bhs.collect_stale(stale, tmp_path)
    names = [r["name"] for r in rows]
    agg = [r for r in rows if r.get("immutable")]
    assert len(agg) == 1 and agg[0]["files"] == 3
    assert agg[0]["months"] == ["201407", "202606"] and agg[0]["status"] == "fresh"
    assert f"nuforc_subndx_{this_month}.json" in names        # current month kept per-file
    assert "fetch_fred.json" in names
    assert not any(n == "nuforc_subndx_201407.json" for n in names)


# ---- manifest-driven coverage ---------------------------------------------

def test_tabs_cover_every_dashboard_area(bhs):
    for tab in ("City", "Travel", "Aviation / TSA", "UAP", "CPI", "Metals",
                "Supplies", "Money Flow", "Stock Flow", "Real Estate"):
        assert tab in bhs.TAB_INPUTS, tab


def test_manifest_rows_use_payload_dates_suppressions_and_unavailable(bhs, dh, tmp_path):
    today = datetime.now(timezone.utc).date().isoformat()
    _write(tmp_path / "data-defi.json", {"as_of": today})
    _write(tmp_path / "data-mf-flows.json",
           {"as_of": None, "weekly": [], "available": False,
            "unavailable_reason": "ICI download failed: HTTP 403"})
    _write(tmp_path / "v2" / "data-mufon.json",          # DEPLOYED built_path
           {"generated_at": datetime.now(timezone.utc).isoformat(),
            "date_range": ["1906-11-11", "2026-06-09"]})
    _write(tmp_path / "data-city.json",
           {"generated_at": datetime.now(timezone.utc).isoformat(),
            "cities": [{"data_health": {"last_updated": "2026-08-01T00:00:00+00:00"}}]})
    rows = {r["path"]: r for r in bhs.collect_manifest_feeds(tmp_path)}
    assert rows["data-defi.json"]["status"] == "fresh"
    mf = rows["data-mf-flows.json"]
    assert mf["status"] == "critical" and "403" in mf["unavailable_reason"]
    mufon = rows["data-mufon.json"]
    assert mufon["date_key"] == "date_range[1]"
    if datetime.now(timezone.utc).date() <= dh.SUPPRESSIONS["data-mufon.json"].until:
        assert mufon["suppressed_until"] == dh.SUPPRESSIONS["data-mufon.json"].until.isoformat()
    assert rows["data-city.json"]["date_key"] == "cities[].data_health.last_updated"
    # a feed with an unusual cadence gets its own entry in the one table
    _write(tmp_path / "data-stock-money-flow.json", {"as_of": today})
    rows = {r["path"]: r for r in bhs.collect_manifest_feeds(tmp_path)}
    assert rows["data-stock-money-flow.json"]["stale_h"] == 120


def test_health_page_and_watchdog_share_one_threshold_per_feed(bhs, dh, tmp_path, monkeypatch):
    """/health/ (build_health_status) and data_health used to keep separate
    tables: THRESHOLDS here, a per-feed limit_h in MANIFEST there, and
    data/stock_money_flow_history.csv was critical at 168h on one and failed
    at 120h on the other. Now MANIFEST has no limit field, and for every file
    both monitors report, /health/'s critical boundary IS data_health's limit."""
    assert "limit_h" not in dh.Feed.__dataclass_fields__
    now = datetime.now(timezone.utc)
    day, stamp = now.date().isoformat(), now.isoformat()
    for rel in ("data/stock_money_flow_history.csv", "data/travel_advisory_levels.csv",
                "data/btc_flows.csv", "data/equity_etf_flows.csv"):
        _write(tmp_path / rel, f"date,x\n{day},1\n")
    for rel in ("data/real_estate.json", "data-tsa.json", "data-stock-money-flow.json",
                "data-aviation.json", "data-defi.json", "data-cfpb.json"):
        _write(tmp_path / rel, {"as_of": day, "generated_at": stamp})
    _write(tmp_path / "data" / "composites" / f"{day}.json", {"generated_at": stamp})

    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    watchdog = {r.path: r.limit_h for r in dh.evaluate(dh.BUILT, history=False)
                if r.limit_h is not None}
    page = {r["path"]: r["stale_h"]
            for r in bhs.scan(tmp_path / "data", tmp_path)
            + bhs.collect_manifest_feeds(tmp_path)}

    both = sorted(set(watchdog) & set(page))
    assert len(both) >= 9, both          # the comparison really covered feeds
    assert {p: page[p] for p in both} == {p: watchdog[p] for p in both}
    assert watchdog["data/stock_money_flow_history.csv"] == 120
    assert watchdog["data/composites/"] == 48     # a directory feed, keyed by path


def test_build_artifacts_are_watched_and_phantoms_removed(dh):
    for rel in ("data-mmf.json", "data-mf-flows.json", "data-equity-etf-flows.json",
                "data-defi.json"):
        assert dh.MANIFEST[rel].kind == dh.BUILT, rel
    for phantom in ("data/cpi.json", "data/metals.json", "data/supplies.json"):
        assert phantom not in dh.MANIFEST
    assert dh.MANIFEST["data-cpi.json"].kind == dh.BUILT
    assert dh.MANIFEST["data/shares.json"].kind == dh.DELEGATED


def test_unavailable_payload_is_unknown_with_reason_in_built_mode(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(dh, "MANIFEST", {
        "data-mf-flows.json": dh.Feed(dh.BUILT, "test owner")})
    monkeypatch.setattr(dh, "SUPPRESSIONS", {})
    _write(tmp_path / "data-mf-flows.json",
           {"as_of": None, "weekly": [], "available": False,
            "unavailable_reason": "ICI download failed: HTTP 403"})
    res = {r.path: r for r in dh.evaluate(dh.BUILT)}
    r = res["data-mf-flows.json"]
    assert r.status == dh.UNKNOWN and "HTTP 403" in r.detail


def test_equity_etf_sidecar_is_judged_on_trade_date_not_run_date(bhs, tmp_path):
    p = _write(tmp_path / "data-equity-etf-flows.json",
               {"as_of": "2026-10-04", "trade_date": "2026-09-25", "tickers": {}})
    probe = bhs.resolve_age(p, _ts(2026, 10, 4, 12, 0), "data-equity-etf-flows.json")
    assert probe.age_h == 9 * 24.0 and probe.key == "trade_date"
