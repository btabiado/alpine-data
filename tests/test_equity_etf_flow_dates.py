"""Money Flow per-index ETF flows: right day, right sign, one row per day.

On 2026-10-04 (a Sunday) the deploy build ran fetch_equity_etf_flows.main().
Nasdaq's quote said ``lastTradeTimestamp: "Oct 1, 2026"`` while its
``lastSalePrice`` was the Oct-2 close (Nasdaq's own historical table lists
769.64 under 10/02/2026). The snapshot was therefore dated 10-01, differenced
against the 09-30 row, and shipped as a second 2026-10-01 history point and
the headline: SPY -34.65 / QQQ -27.16 / DIA -5.61 ($M), against the committed
10-02 row's SPY -23.75 / QQQ +126.03 / DIA +16.63.

Pinned here, offline: rows are dated by the quote's own regularMarketTime or
the NYSE session calendar (never Nasdaq's label, never the UTC date); a
snapshot dated <= the newest CSV row is discarded; history is de-duplicated
by date; the headline is the CSV's latest row; the committed CSV is clean.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import fetch_equity_etf_flows as eef

ROOT = Path(__file__).resolve().parents[1]

# The committed rows around the incident (data/equity_etf_flows.csv).
CSV_TAIL = """date,ticker,shares_out,nav,price,net_flow_musd
2026-09-30,SPY,862375024,768.03,768.03,-8.69
2026-09-30,QQQ,672936227,742.99,742.99,-54.19
2026-09-30,DIA,70450986,512.93,512.93,5.64
2026-10-01,SPY,862148359,759.59,759.59,-172.17
2026-10-01,QQQ,672795070,737.475,737.475,-104.1
2026-10-01,DIA,70427450,505.13,505.13,-11.89
2026-10-02,SPY,862117568,771.27,771.27,-23.75
2026-10-02,QQQ,672962624,752.16,752.16,126.03
2026-10-02,DIA,70459977,511.285,511.285,16.63
"""


def _rows(text=CSV_TAIL):
    return list(csv.DictReader(io.StringIO(text)))


def _utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def _snap(trade_date, so=None, price=None, source="nasdaq_marketcap"):
    so = so or {"SPY": 862330000, "QQQ": 672925000, "DIA": 70440000}
    price = price or {"SPY": 769.64, "QQQ": 750.0, "DIA": 510.0}
    return {"as_of": "2026-10-04", "trade_date": trade_date, "source": source,
            "tickers": {tk: {"index": eef.INDEX_LABEL[tk], "shares_out": so[tk],
                             "price": price[tk], "nav": price[tk], "source": source,
                             "trade_date": trade_date} for tk in eef.TICKERS}}


# ---------------------------------------------------------------- calendar

@pytest.mark.parametrize("now,expected", [
    ("2026-10-04T21:40:00", "2026-10-02"),  # Sunday build -> Friday's session
    ("2026-10-03T12:00:00", "2026-10-02"),  # Saturday
    ("2026-10-02T12:00:00", "2026-10-01"),  # Friday 08:00 ET, before the open
    ("2026-10-02T13:30:00", "2026-10-02"),  # Friday 09:30 ET, the open
    ("2026-10-02T23:00:00", "2026-10-02"),  # Friday after the close
    ("2026-10-05T08:30:00", "2026-10-02"),  # Monday pre-open -> Friday
    ("2026-04-03T18:00:00", "2026-04-02"),  # Good Friday 2026
    ("2026-11-27T18:00:00", "2026-11-27"),  # day after Thanksgiving (open)
    ("2026-11-26T18:00:00", "2026-11-25"),  # Thanksgiving
    ("2026-07-03T18:00:00", "2026-07-02"),  # Independence Day observed (Fri)
    ("2027-01-01T18:00:00", "2026-12-31"),  # New Year's Day
])
def test_session_date(now, expected):
    assert eef.session_date(_utc(now)) == expected


def test_nyse_holidays_2026():
    assert eef.nyse_holidays(2026) == {
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
        date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
        date(2026, 11, 26), date(2026, 12, 25)}


def test_quote_epoch_is_dated_in_new_york_time():
    # 2026-10-02 20:00 UTC = 16:00 ET close.
    ts = _utc("2026-10-02T20:00:00").timestamp()
    assert eef._quote_session_date(ts, _utc("2026-10-04T21:40:00")) == "2026-10-02"
    # A Saturday timestamp is not a session, and the future is refused.
    assert eef._quote_session_date(_utc("2026-10-03T15:00:00").timestamp(),
                                   _utc("2026-10-04T21:40:00")) is None
    assert eef._quote_session_date(_utc("2026-10-05T15:00:00").timestamp(),
                                   _utc("2026-10-04T21:40:00")) is None
    assert eef._quote_session_date(None) is None


# ---------------------------------------------------------------- sources

class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


def test_nasdaq_last_trade_label_no_longer_dates_the_row(monkeypatch):
    info = {"data": {"primaryData": {"lastSalePrice": "$769.64",
                                     "lastTradeTimestamp": "Oct 1, 2026"}}}
    summ = {"data": {"summaryData": {"MarketCap": {"value": "663,683,601,200"}}}}
    monkeypatch.setattr(eef.requests, "get",
                        lambda url, **k: _Resp(info if url.endswith("/info") else summ))
    rec = eef.get_shares_outstanding_nasdaq("SPY")
    assert "trade_date" not in rec
    assert rec["vendor_trade_label"] == "Oct 1, 2026"
    assert rec["shares_out"] == pytest.approx(862_330_000, rel=1e-6)


def test_fetch_dates_nasdaq_rows_from_the_session_calendar(monkeypatch):
    monkeypatch.setattr(eef, "_yahoo_session_crumb", lambda: (None, None))
    monkeypatch.setattr(eef.time, "sleep", lambda *_: None)
    monkeypatch.setattr(eef, "get_shares_outstanding_nasdaq", lambda tk: {
        "shares_out": 1.0e6, "price": 100.0, "nav": 100.0, "source": "nasdaq_marketcap",
        "vendor_trade_label": "Oct 1, 2026"})
    snap = eef.fetch(_utc("2026-10-04T21:40:00"))
    assert snap["trade_date"] == "2026-10-02"
    assert {t["trade_date"] for t in snap["tickers"].values()} == {"2026-10-02"}


# ---------------------------------------------------------------- persistence

def test_weekend_build_reuses_the_committed_row_instead_of_a_new_flow():
    """The incident, replayed with the snapshot now correctly dated 10-02."""
    payload, new_rows = eef.build_payload(_snap("2026-10-02"), _rows())
    assert new_rows == []
    flows = {tk: payload["tickers"][tk]["net_flow_musd"] for tk in eef.TICKERS}
    assert flows == {"SPY": -23.75, "QQQ": 126.03, "DIA": 16.63}
    assert payload["trade_date"] == "2026-10-02"
    assert all(t["source"] == "csv" for t in payload["tickers"].values())
    assert payload["source"] == "csv"


def test_a_mislabelled_older_snapshot_is_discarded_not_redifferenced():
    """Even if a vendor mislabels the session again, nothing older than the
    newest row can be appended or shown."""
    payload, new_rows = eef.build_payload(_snap("2026-10-01"), _rows())
    assert new_rows == []
    assert payload["tickers"]["SPY"]["net_flow_musd"] == -23.75
    for tk in eef.TICKERS:
        dates = [h["date"] for h in payload["tickers"][tk]["history"]]
        assert len(dates) == len(set(dates)), f"{tk} history has duplicate dates"
        assert dates == sorted(dates)
    assert "snapshot_skipped" in payload


def test_a_new_session_is_appended_and_differenced_against_the_newest_row():
    payload, new_rows = eef.build_payload(_snap("2026-10-05"), _rows())
    assert [r["date"] for r in new_rows] == ["2026-10-05"] * 3
    spy = payload["tickers"]["SPY"]
    expected = round((862330000 - 862117568) * 769.64 / 1e6, 2)
    assert spy["net_flow_musd"] == expected
    assert spy["date"] == "2026-10-05"
    assert spy["history"][-2:] == [{"date": "2026-10-02", "net_flow_musd": -23.75},
                                   {"date": "2026-10-05", "net_flow_musd": expected}]


def test_failed_fetch_still_shows_the_latest_recorded_flow():
    snap = {"as_of": "2026-10-04", "trade_date": "2026-10-02", "source": "none", "tickers": {}}
    payload, new_rows = eef.build_payload(snap, _rows())
    assert new_rows == []
    assert payload["tickers"]["QQQ"]["net_flow_musd"] == 126.03


def test_dedupe_keeps_the_first_row_per_date_and_ticker():
    dup = CSV_TAIL + "2026-10-01,SPY,862330000,769.64,769.64,-34.65\n"
    rows, dropped = eef.dedupe_rows(_rows(dup))
    assert dropped == 1
    spy_101 = [r for r in rows if r["date"] == "2026-10-01" and r["ticker"] == "SPY"]
    assert len(spy_101) == 1 and spy_101[0]["net_flow_musd"] == "-172.17"


def test_main_repairs_duplicate_rows_on_disk(tmp_path, monkeypatch):
    csv_path = tmp_path / "equity_etf_flows.csv"
    csv_path.write_text(CSV_TAIL + "2026-10-01,SPY,862330000,769.64,769.64,-34.65\n")
    monkeypatch.setattr(eef, "CSV_PATH", csv_path)
    monkeypatch.setattr(eef, "JSON_PATH", tmp_path / "out.json")
    monkeypatch.setattr(eef, "fetch", lambda now=None: _snap("2026-10-02"))
    payload = eef.main(write=True)
    assert csv_path.read_text() == CSV_TAIL
    assert payload["tickers"]["SPY"]["net_flow_musd"] == -23.75


def test_main_leaves_a_clean_csv_byte_identical(tmp_path, monkeypatch):
    csv_path = tmp_path / "equity_etf_flows.csv"
    csv_path.write_text(CSV_TAIL)
    monkeypatch.setattr(eef, "CSV_PATH", csv_path)
    monkeypatch.setattr(eef, "JSON_PATH", tmp_path / "out.json")
    monkeypatch.setattr(eef, "fetch", lambda now=None: _snap("2026-10-02"))
    eef.main(write=True)
    assert csv_path.read_text() == CSV_TAIL


def test_committed_csv_has_one_row_per_session_and_no_weekends():
    rows = list(csv.DictReader((ROOT / "data" / "equity_etf_flows.csv").open(newline="")))
    keys = Counter((r["date"], r["ticker"]) for r in rows)
    assert not [k for k, n in keys.items() if n > 1]
    assert not [r["date"] for r in rows if date.fromisoformat(r["date"]).weekday() >= 5]
