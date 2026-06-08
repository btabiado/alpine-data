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
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
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
            pass
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
            if so and price:
                return {
                    "shares_out": float(so),
                    "price": float(price),
                    "nav": float(nav) if nav else float(price),
                    "source": "yahoo_quotesummary",
                }
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
    trade_date = pdat.get("lastTradeTimestamp")  # e.g. "Jun 4, 2026"
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
    # normalize "Jun 4, 2026" -> "2026-06-04" if parseable (the real trade day)
    if trade_date:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                out["trade_date"] = datetime.strptime(trade_date.strip(), fmt).strftime(
                    "%Y-%m-%d"
                )
                break
            except ValueError:
                continue
    return out


# --------------------------------------------------------------------------- #
# unified snapshot
# --------------------------------------------------------------------------- #
def fetch() -> dict:
    """Live snapshot of shares_out + price + nav for SPY/QQQ/DIA. No disk I/O.

    Tries the Yahoo crumb path first (one handshake reused across tickers),
    falling back per-ticker to Nasdaq. The per-row trade date prefers Nasdaq's
    real lastTradeTimestamp, else today's UTC date.
    """
    session, crumb = _yahoo_session_crumb()
    tickers: dict[str, dict] = {}
    src_used = set()
    trade_date = None

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
        trade_date = trade_date or rec.get("trade_date")
        tickers[tk] = {
            "index": INDEX_LABEL[tk],
            "shares_out": round(rec["shares_out"], 0),
            "price": round(rec["price"], 4),
            "nav": round(rec.get("nav", rec["price"]), 4),
            "source": rec.get("source"),
        }

    return {
        "as_of": _today(),
        "trade_date": trade_date or _today(),
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


def _last_row_for(rows: list[dict], ticker: str, before_date: str) -> dict | None:
    """Most-recent CSV row for `ticker` strictly before `before_date`."""
    prior = [
        r for r in rows
        if r.get("ticker") == ticker and (r.get("date") or "") < before_date
    ]
    if not prior:
        return None
    return max(prior, key=lambda r: r.get("date", ""))


def main(write: bool = True) -> dict:
    """Fetch, append CSV (computing net_flow vs prior row), write JSON, print."""
    snap = fetch()
    date = snap.get("trade_date") or snap.get("as_of")
    existing = _read_csv_rows()

    payload_tickers: dict[str, dict] = {}
    new_rows: list[dict] = []
    appended_count = 0

    for tk in TICKERS:
        rec = snap["tickers"].get(tk)
        if not rec:
            print(f"  {tk}: NO DATA (both sources failed)")
            continue
        so = float(rec["shares_out"])
        nav = float(rec["nav"])
        price = float(rec["price"])

        prev = _last_row_for(existing, tk, date)
        net_flow_musd = None
        if prev:
            try:
                prev_so = float(prev["shares_out"])
                # net flow in $ millions = ΔSO * NAV / 1e6
                net_flow_musd = round((so - prev_so) * nav / 1_000_000.0, 2)
            except (ValueError, TypeError, KeyError):
                net_flow_musd = None

        new_rows.append({
            "date": date,
            "ticker": tk,
            "shares_out": int(round(so)),
            "nav": round(nav, 4),
            "price": round(price, 4),
            "net_flow_musd": "" if net_flow_musd is None else net_flow_musd,
        })

        # build per-ticker history (prior rows + this one) for the JSON
        hist = [
            {"date": r["date"], "net_flow_musd": _num(r.get("net_flow_musd"))}
            for r in existing
            if r.get("ticker") == tk and _num(r.get("net_flow_musd")) is not None
        ]
        if net_flow_musd is not None:
            hist.append({"date": date, "net_flow_musd": net_flow_musd})
        hist.sort(key=lambda x: x["date"])

        payload_tickers[tk] = {
            "index": INDEX_LABEL[tk],
            "shares_out": int(round(so)),
            "price": round(price, 4),
            "nav": round(nav, 4),
            "net_flow_musd": net_flow_musd,
            "source": rec.get("source"),
            "history": hist[-90:],  # cap to last ~quarter
        }

    payload = {
        "as_of": snap["as_of"],
        "trade_date": date,
        "source": snap["source"],
        "note": (
            "net_flow = ΔSharesOutstanding × NAV; per-index ETF flow only "
            "(never summed into the market-wide ICI aggregate). A single run is "
            "an SO snapshot; net_flow requires >=2 daily runs to accumulate."
        ),
        "tickers": payload_tickers,
    }

    if write and new_rows:
        # skip rows already present for this (date,ticker) so reruns are idempotent
        seen = {(r.get("date"), r.get("ticker")) for r in existing}
        append_rows = [r for r in new_rows if (r["date"], r["ticker"]) not in seen]
        appended_count = len(append_rows)
        write_header = not CSV_PATH.exists()
        if append_rows:
            with CSV_PATH.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=CSV_COLS)
                if write_header:
                    w.writeheader()
                for r in append_rows:
                    w.writerow(r)
        with JSON_PATH.open("w") as f:
            json.dump(payload, f, indent=2)

    # ---- print real numbers ------------------------------------------------ #
    print(f"\nEquity ETF flows  as_of={payload['as_of']}  trade_date={date}  source={snap['source']}")
    print(f"{'TKR':<5}{'INDEX':<12}{'SHARES_OUT':>16}{'PRICE':>11}{'NAV':>11}{'NET_FLOW_$M':>14}")
    for tk in TICKERS:
        p = payload_tickers.get(tk)
        if not p:
            print(f"{tk:<5}{INDEX_LABEL[tk]:<12}{'--':>16}{'--':>11}{'--':>11}{'--':>14}")
            continue
        nf = p["net_flow_musd"]
        nf_s = "(warm-up)" if nf is None else f"{nf:,.2f}"
        print(
            f"{tk:<5}{p['index']:<12}{p['shares_out']:>16,}"
            f"{p['price']:>11,.2f}{p['nav']:>11,.2f}{nf_s:>14}"
        )
    if write and new_rows:
        total = len(existing) + appended_count
        print(f"\nwrote {CSV_PATH}  (+{appended_count} rows, {total} total)")
        print(f"wrote {JSON_PATH}")
    return payload


if __name__ == "__main__":
    main(write=True)
