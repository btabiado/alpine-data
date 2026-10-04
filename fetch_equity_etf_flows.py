"""Per-index ETF money flow for the Money Flow Index (MFX).

net_flow_t = (SharesOutstanding_t - SharesOutstanding_prev) * NAV_t

ETFs create/redeem shares only when authorized participants put net new cash in
(creation) or pull it out (redemption), so the day-over-day change in shares
outstanding -- valued at NAV -- is the cleanest per-fund "money flow" proxy
available without a paid feed. We track the three index proxies:

    SPY -> S&P 500     DIA -> Dow     QQQ -> Nasdaq-100

Data sourcing (tiered, keyless, defensive)
------------------------------------------
1. PRIMARY  -- Yahoo crumb-gated quoteSummary.
   Yahoo's public chart API meta has NO sharesOutstanding, so we run the
   crumb dance: warm a cookie from finance.yahoo.com, fetch a crumb from
   /v1/test/getcrumb, then hit /v10/finance/quoteSummary with
   modules=defaultKeyStatistics,price. That yields sharesOutstanding +
   regularMarketPrice + navPrice in one shot. Yahoo aggressively rate-limits
   by IP (HTTP 429); on a fresh CI runner it usually works, on a hammered
   dev box it does not -- hence the fallback.

2. FALLBACK -- Nasdaq's keyless quote API (api.nasdaq.com).
   .../quote/TICKER/info       -> lastSalePrice (official close) + timestamp
   .../quote/TICKER/summary    -> MarketCap (= price * shares_out)
   Shares outstanding is recovered exactly as MarketCap / price -- and Nasdaq
   reports the real round creation-unit share count (e.g. SPY 862,330,000),
   not a stale derived figure. NAV is approximated by the official close price
   (intraday premium/discount on these mega-cap ETFs is a few basis points).

NAV vs price
------------
We persist both. net_flow is computed against NAV when a true NAV is available
(Yahoo navPrice), otherwise against price (the Nasdaq path). For SPY/DIA/QQQ the
two differ by basis points so the flow magnitude is unaffected.

Warm-up
-------
A single run captures ONE shares-outstanding snapshot; net_flow needs >= 2 days
to difference. We attempt no synthetic seeding (no free source publishes a clean
daily shares-outstanding *history* for these trusts), so on a brand-new install
the flow series is empty on day one and the gauge falls back to neutral for the
ETF leg until the second daily run lands (~1 trading-day warm-up). The CSV
accumulates one row per ticker per run and the JSON history grows with it.

Public API
----------
    fetch() -> dict
        {as_of, source, tickers: {SPY: {shares_out, price, nav, ...}, ...}}
        live snapshot only -- does not touch disk.

    main(write=True) -> dict
        fetch(), append to data/equity_etf_flows.csv computing net_flow vs the
        prior row per ticker, write data-equity-etf-flows.json, print real
        numbers. Returns the JSON payload dict.

Trade date and history integrity
--------------------------------
Each snapshot is dated by the trading session it observes, NOT by the quote
vendor's label and NOT by the runner's clock:
  * Yahoo: the quote's own ``regularMarketTime`` converted to New York time.
  * Nasdaq: the NYSE calendar (most recent session that has opened). Nasdaq's
    ``lastTradeTimestamp`` is ignored on purpose: on 2026-10-04 it read
    "Oct 1, 2026" while ``lastSalePrice`` was the Oct-2 close, so a weekend
    build differenced the Oct-2 share count against Sep-30 and published it
    as a second, wrong-signed Oct-1 flow.
A snapshot whose trade date is <= the newest row already in the CSV is never
appended (that session is already recorded), history is de-duplicated by
date, and the headline per-ticker flow is ALWAYS the CSV's latest row.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
CSV_PATH = DATA_DIR / "equity_etf_flows.csv"
JSON_PATH = ROOT / "data-equity-etf-flows.json"

# DIA -> Dow, SPY -> S&P 500, QQQ -> Nasdaq-100
TICKERS = ["SPY", "QQQ", "DIA"]
INDEX_LABEL = {"SPY": "S&P 500", "QQQ": "Nasdaq-100", "DIA": "Dow"}

CSV_COLS = ["date", "ticker", "shares_out", "nav", "price", "net_flow_musd"]

# A browser-shaped UA is required for both the Yahoo crumb dance and the
# Nasdaq API gateway (both 403/429 a generic UA).
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 20


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _num(s) -> float | None:
    """Parse a Nasdaq-style money string ('$737.55', '636,011,491,500') -> float."""
    if s is None:
        return None
    try:
        return float(str(s).replace("$", "").replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# NYSE session calendar (keyless, offline)
# --------------------------------------------------------------------------- #
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - tzdata missing; EST is close enough
    _ET = timezone(timedelta(hours=-5))

_OPEN_HHMM = (9, 30)


def _easter(y: int) -> _date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return _date(y, month, day)


def _nth_weekday(y: int, month: int, weekday: int, n: int) -> _date:
    d = _date(y, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y: int, month: int, weekday: int) -> _date:
    nxt = _date(y + (month == 12), month % 12 + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d: _date) -> _date:
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(y: int) -> set[_date]:
    """Full-day NYSE closures for year `y` (standard rule set; ad-hoc closures
    such as national days of mourning are not modelled)."""
    hs: set[_date] = set()
    nyd = _date(y, 1, 1)
    if nyd.weekday() == 6:
        hs.add(nyd + timedelta(days=1))
    elif nyd.weekday() < 5:
        hs.add(nyd)  # a Saturday New Year's Day is not observed on Dec 31
    hs.add(_nth_weekday(y, 1, 0, 3))            # Martin Luther King Jr. Day
    hs.add(_nth_weekday(y, 2, 0, 3))            # Washington's Birthday
    hs.add(_easter(y) - timedelta(days=2))      # Good Friday
    hs.add(_last_weekday(y, 5, 0))              # Memorial Day
    if y >= 2022:
        hs.add(_observed(_date(y, 6, 19)))      # Juneteenth
    hs.add(_observed(_date(y, 7, 4)))           # Independence Day
    hs.add(_nth_weekday(y, 9, 0, 1))            # Labor Day
    hs.add(_nth_weekday(y, 11, 3, 4))           # Thanksgiving
    hs.add(_observed(_date(y, 12, 25)))         # Christmas
    return hs


def is_trading_day(d: _date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def session_date(now: datetime | None = None) -> str:
    """Most recent NYSE session that has OPENED as of `now` (YYYY-MM-DD).

    Intraday or after the close on a trading day -> that day. Before the open,
    on a weekend or on a holiday -> the previous trading day. This is the
    session whose shares-outstanding/price a snapshot taken at `now` observes.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    et = now.astimezone(_ET)
    d = et.date()
    if not (is_trading_day(d) and (et.hour, et.minute) >= _OPEN_HHMM):
        d -= timedelta(days=1)
        while not is_trading_day(d):
            d -= timedelta(days=1)
    return d.isoformat()


def _quote_session_date(epoch, now: datetime | None = None) -> str | None:
    """Trade date from a quote's own epoch timestamp (New York date), or None
    when missing, unparseable, not a trading day, or later than the session
    calendar allows."""
    try:
        ts = float(epoch)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    d = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_ET).date()
    if not is_trading_day(d) or d.isoformat() > session_date(now):
        return None
    return d.isoformat()


# --------------------------------------------------------------------------- #
# source 1 -- Yahoo crumb-gated quoteSummary  (PRIMARY)
# --------------------------------------------------------------------------- #
def _yahoo_session_crumb() -> tuple[requests.Session, str] | tuple[None, None]:
    """Establish a Yahoo cookie session and fetch a crumb. (None, None) on fail."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,application/json,*/*"})
    try:
        # fc.yahoo.com seeds the A1/A3 consent cookie; finance.yahoo.com as backup.
        try:
            s.get("https://fc.yahoo.com", timeout=TIMEOUT)
        except requests.RequestException:
            pass  # cookie seeding is best-effort; finance.yahoo.com below is the fallback
        s.get("https://finance.yahoo.com", timeout=TIMEOUT)
    except requests.RequestException:
        return None, None
    for host in ("query2", "query1"):
        try:
            r = s.get(
                f"https://{host}.finance.yahoo.com/v1/test/getcrumb", timeout=TIMEOUT
            )
        except requests.RequestException:
            continue
        crumb = (r.text or "").strip()
        if r.status_code == 200 and crumb and "Too Many" not in crumb and len(crumb) < 40:
            return s, crumb
    return None, None


def get_shares_outstanding(ticker: str, session=None, crumb=None) -> dict:
    """Yahoo quoteSummary -> {shares_out, price, nav} for one ticker.

    Returns {} on any failure (rate limit, missing module, parse error).
    A caller may pass a pre-built (session, crumb) to avoid re-running the
    handshake per ticker.
    """
    if session is None or crumb is None:
        session, crumb = _yahoo_session_crumb()
    if session is None or not crumb:
        return {}
    for host in ("query2", "query1"):
        try:
            r = session.get(
                f"https://{host}.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                params={"modules": "defaultKeyStatistics,price", "crumb": crumb},
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            res = (r.json().get("quoteSummary") or {}).get("result") or []
            if not res:
                continue
            res = res[0]
            dks = res.get("defaultKeyStatistics") or {}
            pr = res.get("price") or {}
            so = (dks.get("sharesOutstanding") or {}).get("raw")
            price = (pr.get("regularMarketPrice") or {}).get("raw")
            nav = (pr.get("navPrice") or {}).get("raw")
            rmt = pr.get("regularMarketTime")
            if isinstance(rmt, dict):
                rmt = rmt.get("raw")
            if so and price:
                out = {
                    "shares_out": float(so),
                    "price": float(price),
                    "nav": float(nav) if nav else float(price),
                    "source": "yahoo_quotesummary",
                }
                qd = _quote_session_date(rmt)
                if qd:
                    out["trade_date"] = qd
                return out
        except (ValueError, AttributeError, TypeError, KeyError):
            continue
    return {}


# --------------------------------------------------------------------------- #
# source 2 -- Nasdaq keyless API  (FALLBACK, verified live)
# --------------------------------------------------------------------------- #
def _nasdaq_headers() -> dict:
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }


def get_shares_outstanding_nasdaq(ticker: str) -> dict:
    """Nasdaq info+summary -> {shares_out, price, nav, trade_date}.

    shares_out is recovered exactly as MarketCap / price (Nasdaq reports the
    real round creation-unit share count). NAV is approximated by the official
    close. Returns {} on failure.
    """
    h = _nasdaq_headers()
    base = "https://api.nasdaq.com/api/quote"
    try:
        ri = requests.get(
            f"{base}/{ticker}/info", params={"assetclass": "etf"}, headers=h, timeout=TIMEOUT
        )
        rs = requests.get(
            f"{base}/{ticker}/summary", params={"assetclass": "etf"}, headers=h, timeout=TIMEOUT
        )
    except requests.RequestException:
        return {}
    if ri.status_code != 200 or rs.status_code != 200:
        return {}
    try:
        pdat = (ri.json().get("data") or {}).get("primaryData") or {}
        sdat = (rs.json().get("data") or {}).get("summaryData") or {}
    except (ValueError, AttributeError):
        return {}
    price = _num(pdat.get("lastSalePrice"))
    mcap = _num((sdat.get("MarketCap") or {}).get("value"))
    # lastTradeTimestamp (e.g. "Oct 1, 2026") is kept for diagnostics only. It
    # has been observed one session BEHIND lastSalePrice (it said Oct 1 while
    # the price was the Oct-2 close), so it must not date the row; fetch()
    # dates Nasdaq rows from the NYSE session calendar instead.
    trade_label = pdat.get("lastTradeTimestamp")
    if not price or not mcap:
        return {}
    so = mcap / price
    out = {
        "shares_out": float(so),
        "price": float(price),
        "nav": float(price),  # NAV ~= close for mega-cap index ETFs
        "market_cap": float(mcap),
        "source": "nasdaq_marketcap",
    }
    if trade_label:
        out["vendor_trade_label"] = str(trade_label).strip()
    return out


# --------------------------------------------------------------------------- #
# unified snapshot
# --------------------------------------------------------------------------- #
def fetch(now: datetime | None = None) -> dict:
    """Live snapshot of shares_out + price + nav for SPY/QQQ/DIA. No disk I/O.

    Tries the Yahoo crumb path first (one handshake reused across tickers),
    falling back per-ticker to Nasdaq. Each ticker's trade date is the Yahoo
    quote's own regularMarketTime (New York date) when present, else the NYSE
    session calendar -- never Nasdaq's lastTradeTimestamp, never the UTC date.
    """
    session, crumb = _yahoo_session_crumb()
    tickers: dict[str, dict] = {}
    src_used = set()
    cal_date = session_date(now)

    for tk in TICKERS:
        rec = {}
        if session and crumb:
            rec = get_shares_outstanding(tk, session, crumb)
        if not rec:
            rec = get_shares_outstanding_nasdaq(tk)
            if rec:
                time.sleep(0.4)  # be polite to api.nasdaq.com between tickers
        if not rec:
            continue
        src_used.add(rec.get("source", "unknown"))
        tickers[tk] = {
            "index": INDEX_LABEL[tk],
            "shares_out": round(rec["shares_out"], 0),
            "price": round(rec["price"], 4),
            "nav": round(rec.get("nav", rec["price"]), 4),
            "source": rec.get("source"),
            "trade_date": rec.get("trade_date") or cal_date,
        }
        if rec.get("vendor_trade_label"):
            tickers[tk]["vendor_trade_label"] = rec["vendor_trade_label"]

    dates = sorted({t["trade_date"] for t in tickers.values()})
    return {
        "as_of": _today(),
        "trade_date": dates[-1] if dates else cal_date,
        "source": "+".join(sorted(src_used)) if src_used else "none",
        "tickers": tickers,
    }


# --------------------------------------------------------------------------- #
# persistence + flow computation
# --------------------------------------------------------------------------- #
def _read_csv_rows() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    try:
        with CSV_PATH.open(newline="") as f:
            return list(csv.DictReader(f))
    except (OSError, csv.Error):
        return []


def _ticker_rank(tk: str) -> int:
    return TICKERS.index(tk) if tk in TICKERS else len(TICKERS)


def dedupe_rows(rows: list[dict]) -> tuple[list[dict], int]:
    """One row per (date, ticker), sorted by date then SPY/QQQ/DIA.

    The FIRST occurrence wins: it is the row that was recorded (and committed)
    first; a later row with the same key can only be a re-append of a snapshot
    for a session that was already on file. Rows without a date or ticker are
    dropped. Returns (rows, number_dropped).
    """
    seen: dict[tuple[str, str], dict] = {}
    for r in rows:
        k = ((r.get("date") or "").strip(), (r.get("ticker") or "").strip())
        if not k[0] or not k[1] or k in seen:
            continue
        seen[k] = r
    out = sorted(seen.values(), key=lambda r: (r["date"], _ticker_rank(r["ticker"])))
    return out, len(rows) - len(out)


def _write_csv(rows: list[dict]) -> None:
    with CSV_PATH.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def build_payload(snap: dict, existing: list[dict]) -> tuple[dict, list[dict]]:
    """Pure core of main(): (JSON payload, CSV rows to append). No I/O.

    `existing` must already be de-duplicated. A ticker's snapshot is appended
    only when its trade date is AFTER that ticker's newest existing row;
    otherwise that session is already on file and the snapshot is discarded
    (re-differencing it against an older row is how a weekend build published
    a wrong-day, wrong-sign flow). Headline fields always come from the
    ticker's latest row, so every number shown carries the date it describes.
    """
    payload_tickers: dict[str, dict] = {}
    new_rows: list[dict] = []
    skipped: dict[str, str] = {}

    for tk in TICKERS:
        rows_tk = [r for r in existing if r.get("ticker") == tk]
        newest = rows_tk[-1] if rows_tk else None
        rec = (snap.get("tickers") or {}).get(tk)
        appended = False
        if rec:
            tk_date = rec.get("trade_date") or snap.get("trade_date")
            if not tk_date:
                skipped[tk] = "snapshot has no trade date"
            elif newest is not None and tk_date <= newest["date"]:
                skipped[tk] = (f"snapshot trade_date {tk_date} <= newest CSV row "
                               f"{newest['date']}; session already recorded")
            else:
                so = float(rec["shares_out"])
                nav = float(rec["nav"])
                price = float(rec["price"])
                net_flow_musd = None
                if newest is not None:
                    try:
                        # net flow in $ millions = ΔSO * NAV / 1e6
                        net_flow_musd = round(
                            (so - float(newest["shares_out"])) * nav / 1_000_000.0, 2)
                    except (ValueError, TypeError, KeyError):
                        net_flow_musd = None
                row = {
                    "date": tk_date,
                    "ticker": tk,
                    "shares_out": int(round(so)),
                    "nav": round(nav, 4),
                    "price": round(price, 4),
                    "net_flow_musd": "" if net_flow_musd is None else net_flow_musd,
                }
                new_rows.append(row)
                rows_tk.append(row)
                appended = True

        if not rows_tk:
            continue
        latest = rows_tk[-1]
        hist = [
            {"date": r["date"], "net_flow_musd": _num(r.get("net_flow_musd"))}
            for r in rows_tk
            if _num(r.get("net_flow_musd")) is not None
        ]
        payload_tickers[tk] = {
            "index": INDEX_LABEL[tk],
            "date": latest["date"],
            "shares_out": int(round(_num(latest.get("shares_out")) or 0)),
            "price": _num(latest.get("price")),
            "nav": _num(latest.get("nav")),
            "net_flow_musd": _num(latest.get("net_flow_musd")),
            "source": (rec or {}).get("source") if appended else "csv",
            "history": hist[-90:],  # cap to last ~quarter
        }

    data_dates = [t["date"] for t in payload_tickers.values()]
    shown_sources = sorted({t["source"] for t in payload_tickers.values() if t.get("source")})
    payload = {
        "as_of": snap.get("as_of"),
        # The session the headline flows describe (newest row shown), not the
        # snapshot's own label: if the snapshot was discarded these differ.
        "trade_date": max(data_dates) if data_dates else None,
        "snapshot_trade_date": snap.get("trade_date"),
        # Where the numbers SHOWN came from ("csv" = the recorded history);
        # the live snapshot's vendor is kept separately.
        "source": "+".join(shown_sources) if shown_sources else snap.get("source"),
        "snapshot_source": snap.get("source"),
        "note": (
            "net_flow = ΔSharesOutstanding × NAV; per-index ETF flow only "
            "(never summed into the market-wide ICI aggregate). A single run is "
            "an SO snapshot; net_flow requires >=2 daily runs to accumulate."
        ),
        "tickers": payload_tickers,
    }
    if skipped:
        payload["snapshot_skipped"] = skipped
    return payload, new_rows


def main(write: bool = True, now: datetime | None = None) -> dict:
    """Fetch, append CSV (computing net_flow vs prior row), write JSON, print."""
    snap = fetch(now)
    raw = _read_csv_rows()
    existing, dropped = dedupe_rows(raw)
    payload, new_rows = build_payload(snap, existing)

    if write:
        if dropped or new_rows:
            # Rewrite (not append) whenever duplicates were found so the repair
            # sticks; a plain append keeps the committed bytes untouched.
            if dropped or not CSV_PATH.exists():
                _write_csv(dedupe_rows(existing + new_rows)[0])
            else:
                with CSV_PATH.open("a", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
                    for r in new_rows:
                        w.writerow(r)
        with JSON_PATH.open("w") as f:
            json.dump(payload, f, indent=2)

    # ---- print real numbers ------------------------------------------------ #
    print(f"\nEquity ETF flows  as_of={payload['as_of']}  trade_date={payload['trade_date']}  "
          f"snapshot_trade_date={payload['snapshot_trade_date']}  source={snap.get('source')}")
    for tk, why in (payload.get("snapshot_skipped") or {}).items():
        print(f"  {tk}: snapshot not appended -- {why}")
    print(f"{'TKR':<5}{'INDEX':<12}{'DATE':>12}{'SHARES_OUT':>16}{'PRICE':>11}{'NET_FLOW_$M':>14}")
    for tk in TICKERS:
        p = payload["tickers"].get(tk)
        if not p:
            print(f"{tk:<5}{INDEX_LABEL[tk]:<12}{'--':>12}{'--':>16}{'--':>11}{'--':>14}")
            continue
        nf = p["net_flow_musd"]
        nf_s = "(warm-up)" if nf is None else f"{nf:,.2f}"
        price_s = "--" if p["price"] is None else f"{p['price']:,.2f}"
        print(f"{tk:<5}{p['index']:<12}{p['date']:>12}{p['shares_out']:>16,}{price_s:>11}{nf_s:>14}")
    if write:
        if dropped:
            print(f"\nrepaired {CSV_PATH}: removed {dropped} duplicate (date,ticker) rows")
        if new_rows or dropped:
            print(f"wrote {CSV_PATH}  (+{len(new_rows)} rows)")
        print(f"wrote {JSON_PATH}")
    return payload


if __name__ == "__main__":
    main(write=True)
