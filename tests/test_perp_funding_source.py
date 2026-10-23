"""Coinbase International perps: the INTX instruments route 404'd on
2026-10-23 (daily audit). Rows now come from the keyless public book
summary. Dated futures and stale quotes are dropped. Offline: HTTP is stubbed.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import api_status
import fetch_market as fm

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _book(sym, funding=0.00005, when=None, name=None, **over):
    when = when or NOW - timedelta(minutes=5)
    row = {
        "instrument_name": name or f"{sym}_USDC-PERPETUAL",
        "base_currency": sym,
        "quote_currency": "USDC",
        "mark_price": 100.0,
        "estimated_delivery_price": 99.5,
        "open_interest": 10.0,
        "volume": 1.0,
        "volume_notional": 100.0,
        "funding_8h": funding,
        "creation_timestamp": _ms(when),
    }
    row.update(over)
    return row


def _envelope(*rows):
    return {"jsonrpc": "2.0", "result": list(rows)}


def test_book_summary_keeps_fresh_perps_and_drops_dated_futures(monkeypatch):
    seen = []

    def fake_get(url, params=None, **kw):
        seen.append(url)
        return _envelope(
            _book("BTC", funding=0.0002),
            _book("ETH", funding=-0.0004),
            _book("SOL", funding=0.0001, name="SOL_USDC-25OCT26"),  # dated future
            {"instrument_name": "NOPE"},
            "junk",
        )

    monkeypatch.setattr(fm, "_get", fake_get)
    rows = fm.coinbase_intl_perpetuals(now=NOW)
    assert seen == [fm.CB_INTL_PERP_URL]
    assert [r["symbol"] for r in rows] == ["BTC", "ETH"]
    assert rows[0]["funding_rate"] == 0.0002
    assert rows[0]["mark_price"] == 100.0
    assert rows[0]["index_price"] == 99.5
    assert rows[0]["open_interest_base"] == 10.0
    assert rows[0]["notional_24h"] == 100.0
    assert rows[0]["as_of"] == "2026-10-04"
    assert rows[0]["as_of_ts"].endswith("Z")
    st = fm._CB_INTL_LAST_STATUS
    assert st["available"] is True
    assert st["perps_total"] == 2
    assert st["trading_fresh"] == 2
    assert st["excluded_not_perpetual"] == 2
    assert st["excluded_stale_quote"] == 0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan", None, "nope"])
def test_finite_rejects_non_numbers(bad):
    """NaN and the infinities are not rates. The old ``n != n`` check was that
    test written as a comparison of a value with itself."""
    assert fm._finite(bad) is None


def test_finite_keeps_a_real_rate_including_zero():
    assert fm._finite(0) == 0.0
    assert fm._finite("0.0002") == pytest.approx(0.0002)
    assert fm._finite(-1.5) == -1.5


def test_non_finite_funding_is_dropped_and_a_zero_rate_is_kept(monkeypatch):
    body = _envelope(
        _book("BTC", funding=float("nan")),
        _book("ETH", funding=float("inf")),
        _book("SOL", funding=0.0),
    )
    monkeypatch.setattr(fm, "_get", lambda url, params=None, **kw: body)
    rows = fm.coinbase_intl_perpetuals(now=NOW)
    assert [r["symbol"] for r in rows] == ["SOL"]
    assert rows[0]["funding_rate"] == 0.0
    assert fm._CB_INTL_LAST_STATUS["excluded_missing_funding"] == 2
    assert fm._CB_INTL_LAST_STATUS["trading_fresh"] == 1


def test_stale_and_unfunded_perps_are_dropped(monkeypatch):
    body = _envelope(
        _book("OLD", when=NOW - timedelta(hours=60)),
        _book("UNDATED", when=NOW),
        _book("NOFUND", funding=None),
    )
    body["result"][1]["creation_timestamp"] = None
    monkeypatch.setattr(fm, "_get", lambda url, params=None, **kw: body)
    assert fm.coinbase_intl_perpetuals(now=NOW) == []
    st = fm._CB_INTL_LAST_STATUS
    assert st["available"] is False
    assert st["excluded_stale_quote"] == 2
    assert st["excluded_missing_funding"] == 1
    assert "without funding_8h" in st["reason"]
    assert "older than 24h" in st["reason"]


def test_unreachable_book_summary_degrades_without_raising(monkeypatch):
    monkeypatch.setattr(fm, "_get", lambda url, params=None, **kw: None)
    assert fm.coinbase_intl_perpetuals(now=NOW) == []
    st = fm._CB_INTL_LAST_STATUS
    assert st["available"] is False
    assert st["reason"] == "derivatives book summary unreachable"

    def boom(*args, **kwargs):
        raise RuntimeError("secret-ish detail")

    monkeypatch.setattr(fm, "_get", boom)
    assert fm.coinbase_intl_perpetuals(now=NOW) == []
    assert "RuntimeError" in fm._CB_INTL_LAST_STATUS["reason"]
    assert "secret-ish" not in fm._CB_INTL_LAST_STATUS["reason"]


def _retired_host_is_only_mentioned_as_history(path: Path):
    """The dead INTX host may be named in a comment that records the 404.
    It must not be a URL the code requests."""
    dead = "api.international.coinbase.com"
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if dead not in line:
            continue
        stripped = line.strip()
        assert stripped.startswith("#"), f"{path.name}:{n} still requests {dead}"


def test_probe_uses_the_public_book_summary_and_not_the_retired_host():
    target = next(t for t in api_status.TARGETS if t["label"] == "Coinbase Intl (perps)")
    assert target["url"] == fm.CB_INTL_PERP_URL
    assert target.get("key_env") is None
    assert target.get("retired") is not True
    assert "api.international.coinbase.com" not in target["url"]
    _retired_host_is_only_mentioned_as_history(REPO_ROOT / "fetch_market.py")
    _retired_host_is_only_mentioned_as_history(REPO_ROOT / "api_status.py")
    assert "api.international.coinbase.com" not in (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    # README names the retired route once, as the thing that 404'd.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.count("api.international.coinbase.com") == 1
    assert "funding_8h" in readme
    app = (REPO_ROOT / "app.py").read_text(encoding="utf-8")
    assert "fires every 1h" not in app
    assert "246 PERP" not in app
    assert "8-hour" in app


def test_probe_of_the_new_url_is_up_on_http_200(monkeypatch):
    target = next(t for t in api_status.TARGETS if t["label"] == "Coinbase Intl (perps)")
    seen = []

    class Resp:
        status = 200

        def read(self, n=-1):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def urlopen(req, timeout=None, context=None):
        seen.append(req.full_url)
        return Resp()

    monkeypatch.setattr(api_status.urllib.request, "urlopen", urlopen)
    row = api_status._probe_one(target, timeout=1, attempts=1)
    assert seen == [fm.CB_INTL_PERP_URL]
    assert row["verdict"] == "up"
    assert row["status"] == 200
    assert row["needs_key"] is False
    assert row["reachable"] is True


def test_perp_funding_summary_uses_fresh_okx_rows_only():
    out = fm.perp_funding_summary({
        "BTC": [{"date": "2026-10-03", "rate": 0.0001}, {"date": "2026-10-04", "rate": 0.0002}],
        "ETH": [{"date": "2026-10-03", "rate": 0.0004}],
        "LTC": [{"date": "2026-09-03", "rate": 0.009}],     # frozen: excluded
        "LINK": [],
    }, now=NOW)
    assert out["available"] is True
    assert {r["symbol"] for r in out["rows"]} == {"BTC", "ETH"}
    assert abs(out["avg_rate"] - 0.0003) < 1e-12
    assert out["as_of"] == "2026-10-03"                    # oldest contributing row
    assert {e["symbol"] for e in out["excluded"]} == {"LTC", "LINK"}


def test_perp_funding_summary_unavailable_when_all_stale():
    out = fm.perp_funding_summary({"BTC": [{"date": "2026-09-03", "rate": 0.0001}]}, now=NOW)
    assert out["available"] is False and out["avg_rate"] is None and out["as_of"] is None


def _sc():
    spec = importlib.util.spec_from_file_location(
        "snapshot_composites_pf", REPO_ROOT / "scripts" / "snapshot_composites.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_archive_uses_perp_funding_and_ignores_frozen_coinbase_rows(tmp_path, monkeypatch):
    sc = _sc()
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    (tmp_path / "market.json").write_text(json.dumps({
        "fetched_at": "2026-10-04T21:00:00Z",
        "fear_greed": [{"date": "2026-10-04", "value": 50}],
        "coinbase_intl_perps": [{"symbol": "BTC", "funding_rate": 0.01,
                                 "as_of": "2026-09-03"}],
        "perp_funding": {"available": True, "avg_rate": 0.0001, "as_of": "2026-10-04",
                         "rows": [{"symbol": "BTC", "rate": 0.0001, "as_of": "2026-10-04"}]},
    }))
    (tmp_path / "whale.json").write_text("{}")
    got = sc.collect()["overview_sentiment"]
    # F&G 50 -> 0, funding 0.0001 -> +20; mean 10. The frozen 0.01 would clamp to +100.
    assert got["score"] == 10
    assert got["as_of"] == "2026-10-04"
    assert got["stale"] is False


def test_archive_flags_stale_when_an_input_is_frozen(tmp_path, monkeypatch):
    sc = _sc()
    monkeypatch.setattr(sc, "CACHE", tmp_path)
    (tmp_path / "market.json").write_text(json.dumps({
        "fetched_at": "2026-10-04T21:00:00Z",
        "fear_greed": [{"date": "2026-10-04", "value": 50}],
        "coinbase_intl_perps": [{"symbol": "BTC", "funding_rate": 0.0001,
                                 "as_of": "2026-09-03"}],
    }))
    (tmp_path / "whale.json").write_text("{}")
    got = sc.collect()["overview_sentiment"]
    assert got["as_of"] == "2026-09-03"
    assert got["stale"] is True
    assert "31d behind the fetch" in got["note"]
