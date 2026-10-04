#!/usr/bin/env python3
"""Persist the dashboard's composite/sentiment indexes as a daily time series.

WHY
---
LTHCS already writes one JSON per day to data/lthcs/index/, which is why it can
show history. Every OTHER composite on this dashboard — Crypto Market Sentiment,
Crypto Signal Sentiment, the BTC and ETH Whale Sentiment Indexes, the Money Flow
Index, POC sentiment — is computed at render time and then thrown away. The
number you see is true for the instant the page was built and is unrecoverable
afterwards.

That is a permanent, compounding data loss: these composites are the most
distilled signal on the site, and every day without a snapshot is a day of
history that can never be reconstructed.

This script closes that gap using the same shape LTHCS uses:

    data/composites/<YYYY-MM-DD>.json
    {
      "as_of": "2026-08-02",
      "generated_at": "2026-08-02T20:40:11Z",
      "indexes": {
        "whale_sentiment_btc": {"score": -12, "label": "...", "as_of": "..."},
        ...
      }
    }

Once a few days accumulate, each gauge can render a sparkline from this
directory the same way the LTHCS pages do.

THE KEYS (the frontend's COMPOSITE_HISTORY_CARDS registry consumes these
verbatim; every entry is either the five-field object below or null)::

    crypto_signal_sentiment    poc_signal_breadth
    whale_sentiment_btc        whale_sentiment_eth
    money_flow_index           lthcs_composite
    overview_sentiment         defi_sentiment        stocks_signal_breadth
    etf_flow_sentiment         etf_flow_sentiment_btc   etf_flow_sentiment_eth
    futures_sentiment          futures_sentiment_btc    futures_sentiment_eth
                               futures_sentiment_link   futures_sentiment_ltc

    {"score": <number>, "label": <str|null>, "as_of": "YYYY-MM-DD"|null,
     "stale": <bool>, "note": <str|null>}

Five of those — overview_sentiment, defi_sentiment, etf_flow_sentiment,
futures_sentiment, stocks_signal_breadth — were added after PR #25 made every
composite card clickable: the cards opened a history modal over an archive
that had never recorded them.

PER-ASSET CARDS. ETF flows (BTC/ETH) and Futures positioning (BTC/ETH/LINK/LTC)
do not show one number, they show the number for the asset the toggle is on,
and those series genuinely diverge. Each asset therefore gets its own key and
its own date; a cross-asset mean would archive a value no card ever displayed.
The bare `etf_flow_sentiment` / `futures_sentiment` keys the frontend registry
asks for are written too, holding the card's DEFAULT asset (btc), with the
duplication named in the note.

DESIGN NOTES
------------
* Read-only w.r.t. every other artifact. It consumes the caches the build has
  already produced (data/market.json, data/whale.json, the ETF CSVs) and writes
  exactly one new file.
* NO NEW NETWORK CALLS. It must be safe to run at the end of every pages build.
* Each index records its OWN as_of, taken from the data, never from wall clock.
  A composite derived from a frozen input must look frozen in the history too —
  that is precisely how the 2026-06-07 breadth freeze stayed invisible.
* `stale` is recorded per index when the underlying payload is flagged
  stale-kept, so a backfilled chart can grey those points instead of implying
  the signal genuinely flat-lined.
* Missing inputs are recorded as null rather than skipped, so a gap in the
  series is visible as a gap.
* THE ARITHMETIC MIRRORS THE SHIPPED JS. Every composite here is also computed
  in the browser by app.py / v2/app.py; the archive is worthless if it stores a
  different number than the card displayed, so clampScore(), Math.round() and
  sentimentBucket() are ported exactly (see _clamp / _jsround / _bucket).
  Verified by executing both against one payload — the built dashboard.html in
  headless Chromium versus collect() — and comparing score and label per key.
* ONE DELIBERATE DIVERGENCE, always toward absence: where an input is MISSING,
  this file records null and the shipped JS coerces to zero (`Number(null)`
  is 0 and `isFinite(null)` is true, so renderFuturesSentiment paints +0
  BALANCED for an asset with no funding/long-short/open-interest rows at all,
  and renderStocksSentiment counts a row with no score as a HOLD). A zero is a
  reading and null is an absence; a chart drawn from this archive must be able
  to tell them apart, so the absence is what gets written and the count of
  excluded inputs is disclosed in `note`.

Run from the repo root:  python scripts/snapshot_composites.py
"""
from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

CACHE = REPO_ROOT / "data"
OUT_DIR = CACHE / "composites"


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _entry(score, label=None, as_of=None, stale=False, note=None):
    if score is None:
        return None
    return {
        "score": score,
        "label": label,
        "as_of": as_of,
        "stale": bool(stale),
        "note": note,
    }


def _alias(entry, source_key: str):
    """A second key holding the SAME observation as ``source_key``.

    Used for the two per-asset cards, whose single registry key has to hold
    the default asset's series. The copy carries the identical score, label,
    date and stale flag — nothing is recomputed and nothing is re-dated —
    and its note names the key it duplicates, so a reader of the archive can
    see it is one observation recorded twice and not two independent ones.
    """
    if not isinstance(entry, dict):
        return None
    out = dict(entry)
    note = out.get("note")
    out["note"] = (f"{note}; " if note else "") + f"same series as {source_key}"
    return out


def _series_last_date(series) -> str | None:
    """Newest .date in a [{date,...}] series — the honest as_of for anything
    derived from it.

    Scans BACKWARDS for the last row that actually carries a date, the same
    way the frontend's ``fLast()`` does: a trailing undated point is a hole
    in the tail, not proof the whole series is undatable, and blanking the
    answer over it would turn a plottable observation into a lost one.
    """
    if not isinstance(series, list) or not series:
        return None
    for row in reversed(series):
        if isinstance(row, dict):
            d = row.get("date")
            if isinstance(d, str) and len(d) >= 10:
                return d[:10]
    return None


def _oldest(dates) -> str | None:
    """MIN of a set of contributing dates.

    A composite is only as fresh as its OLDEST input. Taking the newest is
    how one still-updating coin makes a breadth index built from 50 of them
    read as current — the exact mechanism behind the freeze this directory
    exists to make visible.
    """
    ds = [d[:10] for d in (dates or []) if isinstance(d, str) and len(d) >= 10]
    return min(ds) if ds else None


def _poc_entry_date(e) -> str | None:
    """Observation date of one poc_top entry: its explicit ``as_of`` (which
    fetch_market pins and never advances on carry-forward), else the last
    date of its own signal_history."""
    if not isinstance(e, dict):
        return None
    d = e.get("as_of")
    if isinstance(d, str) and len(d) >= 10:
        return d[:10]
    return _series_last_date(e.get("signal_history"))


def _whale_sentiment_date(sent) -> str | None:
    """Observation date of a whale-sentiment composite — or None.

    ``sentiment.as_of`` USED to be ``whale["fetched_at"][:10]``: the wall
    clock at fetch time, which advanced on every run even when nothing
    on-chain moved. Persisting that into the composite history would bake a
    permanently-fresh-looking date into an archive whose entire purpose is
    to make freezes visible.

    fetch_market now derives ``as_of`` from the oldest contributing proxy
    series and tags the payload with ``as_of_basis``. A payload without that
    tag predates the fix (whale.json is restored from the Actions cache and
    never committed, so old shapes do persist) and its ``as_of`` is refused —
    the caller falls back to dating the composite from the raw series.
    """
    if not isinstance(sent, dict):
        return None
    if not isinstance(sent.get("as_of_basis"), str):
        return None
    d = sent.get("as_of")
    return d[:10] if isinstance(d, str) and len(d) >= 10 else None


# ---------------------------------------------------------------------------
# Shared arithmetic — the browser's, ported exactly.
#
# Every composite below is ALSO computed in JS at render time (app.py /
# v2/app.py). The archive is only worth keeping if the number it stores is the
# number the card showed, so these mirror the shipped helpers one-for-one:
# clampScore(), Math.round() and sentimentBucket().
# ---------------------------------------------------------------------------

def _num(v):
    """A finite float, or None for anything that is not a number.

    This is where the one deliberate divergence from the browser lives. JS
    ``Number(null)`` is 0 and ``isFinite(null)`` is true, so a missing value
    silently becomes a neutral reading in the renderers. Here it becomes
    None, the caller drops the input, and the count of dropped inputs is
    disclosed in the entry's ``note`` — because the archive is the only
    record, and a fabricated zero in it is unrecoverable.
    """
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _clamp(v):
    """clampScore(): finite → clamped to ±100, else None."""
    f = _num(v)
    if f is None:
        return None
    return max(-100.0, min(100.0, f))


def _jsround(x: float) -> int:
    """JS ``Math.round`` — ties go UP (toward +Infinity), NOT Python's
    banker's rounding. round(2.5) is 2 in Python and 3 in JS; a composite
    archived at a different value than the card displayed is a lie of
    exactly the kind this file exists to prevent."""
    return int(math.floor(x + 0.5))


def _mean_score(components) -> int | None:
    """``Math.round(mean(components))`` over the clamped inputs, or None when
    nothing contributed. None means ABSENT — never 0, which is a reading."""
    vals = [c for c in components if c is not None]
    if not vals:
        return None
    return _jsround(sum(vals) / len(vals))


def _bucket(net, labels) -> str:
    """sentimentBucket(): the exact 5-way label ladder the card paints."""
    if net >= 50:
        return labels[0]
    if net >= 20:
        return labels[1]
    if net > -20:
        return labels[2]
    if net > -50:
        return labels[3]
    return labels[4]


def _last(series, field: str):
    """Last element's ``field`` from a [{...}] series, as a float."""
    if not isinstance(series, list) or not series:
        return None
    last = series[-1]
    return _num(last.get(field)) if isinstance(last, dict) else None


# ---------------------------------------------------------------------------
# Per-source observation dates
# ---------------------------------------------------------------------------

def _signals_top20(market: dict) -> list:
    """The top-50 simplified signal rows the cards score from.

    ``signals_top20`` is computed at RENDER time by app.py / v2/app.py and
    only ever exists in their in-memory payload — it is never written to
    data/market.json, which is all this script can see. Recompute it from
    the same pure function the builders call, over the same markets_top
    rows, so the composites that read it are not permanently null.
    """
    rows = market.get("signals_top20")
    if rows:
        return rows if isinstance(rows, list) else []
    try:
        import signals as _sig
        return _sig.compute_all_top20({"market": market}) or []
    except Exception as e:  # never fail the build over a composite
        print(f"  [composites] signals_top20 recompute skipped: "
              f"{type(e).__name__}: {e}")
        return []


def _signals_freshness(rows) -> dict:
    """signalsTop20Freshness(), ported.

    Returns ``{date, stale, total, dated, trusted}``. ``date`` is the OLDEST
    row observation date, or None.

    The ``computed_at`` gate is load-bearing: before the signals.py
    provenance fix, ``as_of`` on these rows was scoring time (a clock read),
    and a payload restored from the Actions cache can still carry that older
    shape. An untrusted payload contributes NO date — it is counted as
    undated instead, exactly as the card does.
    """
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if not rows:
        return {"date": None, "stale": 0, "total": 0, "dated": 0, "trusted": False}
    trusted = any(isinstance(r.get("computed_at"), str) for r in rows)
    if not trusted:
        return {"date": None, "stale": 0, "total": len(rows), "dated": 0,
                "trusted": False}
    dates, stale = [], 0
    for r in rows:
        if r.get("stale") is True:
            stale += 1
        d = r.get("as_of")
        if isinstance(d, str) and len(d) >= 10:
            dates.append(d[:10])
    return {"date": _oldest(dates), "stale": stale, "total": len(rows),
            "dated": len(dates), "trusted": True}


def _perps_freshness(market: dict) -> dict:
    """perpsFreshness(): oldest Coinbase quote timestamp across the perp rows
    actually averaged. The exchange stamps each quote; ``market.fetched_at``
    is our clock and is never used here."""
    perps = [p for p in (market.get("coinbase_intl_perps") or [])
             if isinstance(p, dict)]
    dates = []
    for p in perps:
        d = p.get("as_of") or p.get("as_of_ts")
        if isinstance(d, str) and len(d) >= 10:
            dates.append(d[:10])
    return {"date": _oldest(dates), "total": len(perps), "dated": len(dates)}


def _defi_observation_date(defi: dict) -> str | None:
    """app.py's ``defi_observation_date()``, ported: max date WITHIN each
    DefiLlama daily-TVL series, then MIN ACROSS series. The DeFi card charts
    every chain side by side, so it is only as fresh as its stalest chain."""
    hist = (defi or {}).get("tvl_history")
    if not isinstance(hist, dict):
        return None
    lasts = []
    for series in hist.values():
        if not isinstance(series, list) or not series:
            continue
        dates = [p["date"][:10] for p in series
                 if isinstance(p, dict) and isinstance(p.get("date"), str)
                 and len(p["date"]) >= 10]
        if dates:
            lasts.append(max(dates))
    return min(lasts) if lasts else None


def _futures_freshness(a: dict) -> str | None:
    """futuresFreshness(): oldest last-row date across funding / long-short /
    open-interest. All three carry real per-row dates."""
    return _oldest([
        _series_last_date(a.get("funding")),
        _series_last_date(a.get("long_short_ratio")),
        _series_last_date(a.get("open_interest_usd")),
    ])


# ---------------------------------------------------------------------------
# The nine composites the cards can chart
# ---------------------------------------------------------------------------

def overview_sentiment(market: dict, top20) -> dict | None:
    """Crypto Market Sentiment — renderOverviewSentiment().

    Mean of Fear & Greed, the top-50 signal average and average perp funding.

    DATE: min across the inputs THAT CARRY AN OBSERVATION DATE, and the count
    that carry none is disclosed in the note — a min taken over a subset,
    reported bare, would imply the whole composite is that fresh.
    """
    components, dated, undated = [], [], 0

    fng = market.get("fear_greed")
    fng_last = fng[-1] if isinstance(fng, list) and fng else None
    fng_val = _num(fng_last.get("value")) if isinstance(fng_last, dict) else None
    if fng_val is not None:
        components.append(_clamp((fng_val - 50) * 2))
        d = _series_last_date(fng)
        if d:
            dated.append(d)
        else:
            undated += 1

    stables = {"USDT", "USDC", "DAI", "TUSD", "USDE", "FDUSD", "PYUSD",
               "BUSD", "USDD"}
    sig_rows = [s for s in (top20 or [])
                if isinstance(s, dict)
                and str(s.get("symbol") or "").upper() not in stables]
    sig_scores = [v for v in (_num(s.get("score")) for s in sig_rows)
                  if v is not None]
    sf = _signals_freshness(sig_rows)
    if sig_scores:
        components.append(_clamp(sum(sig_scores) / len(sig_scores)))
        if sf["date"]:
            dated.append(sf["date"])
        else:
            undated += 1

    pf = _perps_freshness(market)
    rates = [v for v in (_num(p.get("funding_rate"))
                         for p in (market.get("coinbase_intl_perps") or [])
                         if isinstance(p, dict)) if v is not None]
    if rates:
        components.append(_clamp((sum(rates) / len(rates) / 0.0001) * 20))
        if pf["date"]:
            dated.append(pf["date"])
        else:
            undated += 1

    net = _mean_score(components)
    if net is None:
        return None
    # Notes ride on EVERY archived point and the whole archive is inlined
    # into the page, so they carry the disclosures and nothing else — the
    # formula already lives in the card registry's `what` text.
    note = f"{len(components)} of 3 inputs: F&G, signal avg, perp funding"
    if undated:
        note += f"; {undated} carrying no observation date"
    if sf["stale"]:
        note += f"; {sf['stale']} of {sf['total']} signal rows cached"
    return _entry(
        net,
        _bucket(net, ["STRONG BULLISH", "BULLISH", "NEUTRAL", "BEARISH",
                      "STRONG BEARISH"]),
        _oldest(dated),
        stale=bool(sf["stale"]),
        note=note,
    )


def defi_sentiment(market: dict) -> dict | None:
    """DeFi Sentiment — renderDefiSentiment().

    TVL-weighted 7d chain momentum plus stablecoin market-cap 7d change.

    DATE: ``defi.as_of`` — the MIN that fetch_market.defi_provenance() wrote
    across the subtree's inputs (daily TVL buckets carry DefiLlama's own
    timestamps; the bare chain/protocol snapshots have none, and date to the
    instant they were pulled, which is defensible only because they have no
    stale-keep path and arrive EMPTY on failure rather than cached). Whatever
    it is, it was frozen into market.json when the fetch ran, so re-reading a
    cached market.json here cannot advance it. Falls back to the daily TVL
    history in the same subtree, and to None — this file never reads a clock,
    so a market.json with neither records a visible gap.
    """
    defi = market.get("defi")
    defi = defi if isinstance(defi, dict) else {}
    llama = market.get("defillama")
    llama = llama if isinstance(llama, dict) else {}
    components = []

    wsum = wnorm = 0.0
    for c in (defi.get("chains") or []):
        if not isinstance(c, dict):
            continue
        tvl = _num(c.get("tvl_usd"))
        chg = _num(c.get("change_7d_pct"))
        if tvl is None or tvl <= 0 or chg is None:
            continue
        wsum += tvl * chg
        wnorm += tvl
    if wnorm > 0:
        components.append(_clamp((wsum / wnorm / 50) * 100))

    sd = _num(llama.get("stablecoin_7d_change_usd"))
    sm = _num(llama.get("stablecoin_mcap_usd"))
    if sd is not None and sm is not None and sm > 0:
        components.append(_clamp((sd / sm * 100 / 5) * 100))

    net = _mean_score(components)
    if net is None:
        return None
    as_of = defi.get("as_of")
    as_of = as_of[:10] if isinstance(as_of, str) and len(as_of) >= 10 else None
    basis = "defi.as_of"
    if not as_of:
        as_of = _defi_observation_date(defi)
        basis = "oldest daily TVL series" if as_of else "no dated DeFi input"
    return _entry(
        net,
        _bucket(net, ["STRONG EXPANSION", "EXPANSION", "NEUTRAL",
                      "CONTRACTION", "STRONG CONTRACTION"]),
        as_of,
        note=f"{len(components)} of 2 inputs: chain TVL momentum, stablecoin "
             f"mcap; date from {basis}",
    )


def _etf_daily_totals(path: Path) -> list[tuple[str, float | None]]:
    """``aggregate(ensure_total(load_csv(path)))['daily']``, without pandas:
    (date, total flow $m) per row, chronological.

    Same column rules as app.py: a case-insensitive ``Total`` column wins,
    otherwise the numeric per-fund columns are summed. Same number cleanup
    (thousands separators, $, accounting parentheses).

    An unreadable total is kept as a DATED ROW WITH A None FLOW rather than
    dropped, because the card's windows are the last 7 and last 30 ROWS: a
    dropped row would silently pull an eighth trading day into the 7d sum.
    The None is skipped when summing (renderEtfFlowSentiment's flowVal does
    the same with NaN) — a day nobody reported is not a day of zero flow.
    """
    if not path.exists():
        return []
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    except Exception as e:
        print(f"  [composites] {path.name}: {type(e).__name__}: {e}")
        return []
    if not rows:
        return []
    cols = [c for c in (rows[0].keys()) if isinstance(c, str)]
    date_col = next((c for c in cols if c.lower() == "date"), None)
    if not date_col:
        return []
    total_col = next((c for c in cols if c.lower() == "total"), None)
    fund_cols = [c for c in cols if c not in (date_col, total_col)]

    def _cell(v):
        if not isinstance(v, str):
            return _num(v)
        s = (v.replace(",", "").replace("(", "-").replace(")", "")
              .replace("$", "").strip())
        return _num(s)

    out: list[tuple[str, float | None]] = []
    for r in rows:
        d = (r.get(date_col) or "").strip()[:10]
        if len(d) < 10:
            continue                      # load_csv drops undated rows too
        if total_col is not None:
            tot = _cell(r.get(total_col))
        else:
            vals = [v for v in (_cell(r.get(c)) for c in fund_cols)
                    if v is not None]
            tot = sum(vals) if vals else None
        out.append((d, tot))
    out.sort(key=lambda t: t[0])
    return out


def etf_flow_sentiment(asset: str) -> dict | None:
    """ETF Flow Sentiment — renderEtfFlowSentiment(), for one asset.

    7d net flow sum (60%) and 30d net flow sum (40%), $500m ⇒ ±100.

    DATE: the last row of the daily series both windows read — the same
    source #etfAsOf and etfFreshness() use, so the card and its history
    cannot disagree. The Farside CSV carries no cache flag, so ``stale``
    stays False here rather than being invented.
    """
    daily = _etf_daily_totals(CACHE / f"{asset}_flows.csv")
    if not daily:
        return None
    NORM = 500.0
    weighted, total_w = 0.0, 0.0
    parts = []
    for window, weight in ((7, 0.6), (30, 0.4)):
        # Last N ROWS (the card slices the series, not the calendar), then
        # drop the rows that reported no number at all.
        vals = [v for _, v in daily[-window:] if v is not None]
        if not vals:
            continue
        s = _clamp((sum(vals) / NORM) * 100)
        if s is None:
            continue
        weighted += s * weight
        total_w += weight
        parts.append(f"{window}d")
    if total_w <= 0:
        return None
    net = _jsround(weighted / total_w)
    return _entry(
        net,
        _bucket(net, ["STRONG INFLOWS", "INFLOWS", "BALANCED", "OUTFLOWS",
                      "STRONG OUTFLOWS"]),
        daily[-1][0],
        note=f"{asset.upper()} ETF; {'+'.join(parts)} net flow sum "
             f"(60/40); {len(daily)} daily rows",
    )


def futures_sentiment(market: dict, asset: str) -> dict | None:
    """Futures Positioning Sentiment — renderFuturesSentiment(), for one asset.

    Funding rate, long/short ratio and 7d open-interest change.

    DATE: futuresFreshness() — oldest last-row date across the three series,
    including a series that carries a date but contributed no component. That
    is deliberate: it is the date the CARD showed, and it is the more
    conservative of the two readings.
    """
    a = market.get(asset)
    a = a if isinstance(a, dict) else {}
    components = []

    rate = _last(a.get("funding"), "rate")
    if rate is not None:
        components.append(_clamp((rate / 0.0005) * 100))

    ratio = _last(a.get("long_short_ratio"), "ratio")
    if ratio is not None and ratio > 0:
        components.append(_clamp(math.log2(ratio) * 100))

    oi = a.get("open_interest_usd")
    if isinstance(oi, list) and len(oi) > 7:
        cur = _last(oi, "oi_usd")
        prev_row = oi[-8]
        prev = _num(prev_row.get("oi_usd")) if isinstance(prev_row, dict) else None
        if cur is not None and prev is not None and prev > 0:
            components.append(_clamp(((cur / prev - 1) * 100 / 50) * 100))

    net = _mean_score(components)
    if net is None:
        return None
    return _entry(
        net,
        _bucket(net, ["STRONG CROWDED LONGS", "CROWDED LONGS", "BALANCED",
                      "CROWDED SHORTS", "STRONG CROWDED SHORTS"]),
        _futures_freshness(a),
        note=f"{asset.upper()}; {len(components)} of 3 inputs: funding, "
             f"long/short, 7d OI",
    )


def stocks_signal_breadth(market: dict) -> dict | None:
    """Equity Signal Breadth — renderStocksSentiment().

    ((BUY+STRONG BUY) − (SELL+STRONG SELL)) / scored rows × 100.

    DATE: oldest ``as_of`` (the last Yahoo daily bar each score was computed
    from), falling back per row to ``history[-1].date``. Rows carrying no
    date at all are excluded from the min and COUNTED in the note, and
    cache-served rows are counted too — fetch_market drops a ticker rather
    than carrying it forward today, but the day that changes the stamp must
    not quietly start lying.
    """
    rows = [s for s in (market.get("stocks_signals") or []) if isinstance(s, dict)]
    if not rows:
        return None
    buy = sell = hold = 0
    scored = 0
    for s in rows:
        v = _num(s.get("score"))
        if v is None:
            continue
        scored += 1
        if v >= 20:
            buy += 1
        elif v > -20:
            hold += 1
        else:
            sell += 1
    if not scored:
        return None
    total = max(buy + hold + sell, 1)
    net = _jsround((buy - sell) / total * 100)
    dates, cached = [], 0
    for s in rows:
        if s.get("stale") is True:
            cached += 1
        d = s.get("as_of")
        if not (isinstance(d, str) and len(d) >= 10):
            d = _series_last_date(s.get("history"))
        if isinstance(d, str) and len(d) >= 10:
            dates.append(d[:10])
    note = f"{total} stocks; {buy} buy+, {sell} sell+"
    unscored = len(rows) - scored
    if unscored > 0:
        note += f"; {unscored} of {len(rows)} carrying no score"
    undated = len(rows) - len(dates)
    if undated > 0:
        note += f"; {undated} of {len(rows)} carrying no observation date"
    if cached:
        note += f"; {cached} of {len(rows)} cached"
    return _entry(
        net,
        _bucket(net, ["STRONG ACCUMULATION", "ACCUMULATION", "NEUTRAL",
                      "DISTRIBUTION", "STRONG DISTRIBUTION"]),
        _oldest(dates),
        stale=bool(cached),
        note=note,
    )


def collect() -> dict:
    market = _load(CACHE / "market.json") or {}
    whale = _load(CACHE / "whale.json") or {}
    idx: dict[str, dict | None] = {}

    # --- Whale Sentiment Index (BTC) — computed Python-side in fetch_market ---
    # Fallback when the payload predates the provenance fix: the OLDEST last
    # date across the blockchain.info series the composite is built from.
    # Taking one series' last date (or worse, `fetched_at`) would overstate
    # the composite exactly the way this archive exists to catch.
    ws = (whale or {}).get("sentiment") or {}
    btc = (whale or {}).get("btc") or {}
    idx["whale_sentiment_btc"] = _entry(
        ws.get("score"), ws.get("label"),
        _whale_sentiment_date(ws) or _oldest([
            _series_last_date(btc.get(k)) for k in (
                "hash_rate", "miners_revenue_usd", "avg_tx_usd",
                "output_volume_btc", "active_addresses", "tx_volume_usd",
            )
        ]),
    )

    # --- ETH Whale Sentiment Index ---
    wse = ((whale or {}).get("eth") or {}).get("sentiment") or {}
    eth_cm = (((whale or {}).get("eth") or {}).get("coin_metrics") or {})
    eth_eds = (((whale or {}).get("eth") or {}).get("etherscan_daily") or {})
    idx["whale_sentiment_eth"] = _entry(
        wse.get("score"), wse.get("label"),
        _whale_sentiment_date(wse) or _oldest(
            [_series_last_date(eth_cm.get(k)) for k in ("AdrActCnt", "TxCnt")]
            + [_series_last_date(eth_eds.get("series")
                                 if isinstance(eth_eds, dict) else None)]
        ),
    )

    # --- Money Flow Index (±100 headline) ---
    mf = market.get("money_flow") or market.get("money_flow_index") or {}
    if isinstance(mf, dict):
        head = mf.get("headline") if isinstance(mf.get("headline"), dict) else mf
        idx["money_flow_index"] = _entry(
            head.get("score") if isinstance(head, dict) else None,
            (head or {}).get("label"), (head or {}).get("as_of") or mf.get("as_of"),
        )
    else:
        idx["money_flow_index"] = None

    # --- Crypto Signal Sentiment (top-50 buy/sell breadth, the "-33" gauge) ---
    # Mirrors renderCryptoSignalsSentiment(): stablecoins excluded, then
    # ((BUY+STRONG_BUY) - (SELL+STRONG_SELL)) / total * 100.
    def _is_stable(sym: str) -> bool:
        u = (sym or "").upper()
        return u.startswith("USD") or u.endswith("USD") or u == "DAI"

    # `signals_top20` is computed at RENDER time by app.py / v2/app.py and
    # only ever exists in their in-memory payload — it is never written to
    # data/market.json, which is all this script can see. That is why every
    # committed snapshot so far records `crypto_signal_sentiment: null`:
    # the gauge the user watches has no history at all. Recompute it here
    # from the same pure function the builders call, over the same
    # markets_top rows, so the series actually starts accumulating.
    # Computed ONCE — overview_sentiment reads the same rows.
    top20 = _signals_top20(market)
    sigs = [s for s in (top20 or [])
            if isinstance(s, dict) and not _is_stable(s.get("symbol"))]
    if sigs:
        def bucket(s):
            v = s.get("score")
            if not isinstance(v, (int, float)):
                return None
            if v >= 50:  return "strong_buy"
            if v >= 20:  return "buy"
            if v <= -50: return "strong_sell"
            if v <= -20: return "sell"
            return "hold"
        buckets = [bucket(s) for s in sigs]
        buckets = [b for b in buckets if b]
        total = len(buckets)
        if total:
            pos = sum(1 for b in buckets if b in ("buy", "strong_buy"))
            neg = sum(1 for b in buckets if b in ("sell", "strong_sell"))
            score = int(round(max(-100, min(100, (pos - neg) / total * 100))))
            # as_of was `market.generated_at` — the payload's BUILD stamp,
            # which reads current on every run no matter how old the coins
            # under it are. Each signals_top20 entry now carries the
            # CoinGecko observation date of the row it was scored from, so
            # the composite takes the oldest of those, and discloses how
            # many were served from cache.
            cached = sum(1 for s in sigs if s.get("stale"))
            note = f"{total} coins; {pos} buy+, {neg} sell+"
            if cached:
                note += f"; {cached} of {len(sigs)} cached"
            idx["crypto_signal_sentiment"] = _entry(
                score, None, _oldest(s.get("as_of") for s in sigs),
                stale=bool(cached), note=note,
            )
        else:
            idx["crypto_signal_sentiment"] = None
    else:
        idx["crypto_signal_sentiment"] = None

    # --- POC sentiment / breadth source ---
    # poc_top is the series that froze at 2026-06-07 behind a stale-keep
    # fallback. Record its true as_of AND whether any entry is stale-flagged, so a
    # future chart can show the flat-line for what it is.
    poc = market.get("poc_top") or []
    if isinstance(poc, list) and poc:
        entries = [e for e in poc if isinstance(e, dict)]
        # MIN, not max. This used to record the newest contributing date,
        # so a single coin still fetching kept the whole breadth index
        # looking live while carried-forward coins underneath it were
        # weeks old.
        hist_dates = [_poc_entry_date(e) for e in entries]
        cached = sum(1 for e in entries if e.get("stale"))
        scores = [
            (e.get("signal_history") or [{}])[-1].get("score")
            for e in entries if e.get("signal_history")
        ]
        scores = [s for s in scores if isinstance(s, (int, float))]
        note = f"{len(scores)} coins; mean of latest per-coin signal"
        if cached:
            note += f"; {cached} of {len(entries)} cached"
        idx["poc_signal_breadth"] = _entry(
            int(round(sum(scores) / len(scores))) if scores else None,
            None,
            _oldest(hist_dates),
            stale=bool(cached),
            note=note,
        )
    else:
        idx["poc_signal_breadth"] = None

    # --- LTHCS composite (already has its own history; mirrored here so one
    #     directory answers "what did every index read that day") ---
    lthcs_dir = CACHE / "lthcs" / "index"
    if lthcs_dir.is_dir():
        files = sorted(p for p in lthcs_dir.glob("*.json") if p.stem[:4].isdigit())
        if files:
            l = _load(files[-1]) or {}
            idx["lthcs_composite"] = _entry(
                l.get("score"), l.get("label"), l.get("as_of") or files[-1].stem,
            )

    # --- The five cards PR #25 made clickable with nothing to plot ----------
    # Crypto Market Sentiment, DeFi, ETF flows, Futures positioning and Equity
    # breadth were computed in the browser and thrown away, exactly as the
    # four above were before PR #23. Each is recomputed here from the same
    # cached inputs the builders render from, and dated from THE DATA.
    idx["overview_sentiment"] = overview_sentiment(market, top20)
    idx["defi_sentiment"] = defi_sentiment(market)
    idx["stocks_signal_breadth"] = stocks_signal_breadth(market)

    # PER-ASSET CARDS (ETF flows: BTC/ETH · Futures: BTC/ETH/LINK/LTC).
    #
    # These two cards do not show one number — they show the number for the
    # asset the toggle is on, and the four futures series diverge (BTC can be
    # crowded long while LTC is crowded short). Archiving a cross-asset mean
    # would store a value NO card has ever displayed, so every asset gets its
    # own key and its own honest date.
    #
    # The frontend's COMPOSITE_HISTORY_CARDS registry maps one card to one
    # key and its chart draws one series, so the bare `futures_sentiment` /
    # `etf_flow_sentiment` keys it already asks for are ALSO written, holding
    # the card's DEFAULT asset (state.asset / state.etfAsset both default to
    # 'btc' — the value every visitor sees before touching the toggle). The
    # note says which asset it is, so the duplication is disclosed rather
    # than silent, and a per-asset frontend can switch to the _<asset> keys
    # without the archive changing shape.
    for asset in ("btc", "eth"):
        idx[f"etf_flow_sentiment_{asset}"] = etf_flow_sentiment(asset)
    for asset in ("btc", "eth", "link", "ltc"):
        idx[f"futures_sentiment_{asset}"] = futures_sentiment(market, asset)
    idx["etf_flow_sentiment"] = _alias(
        idx.get("etf_flow_sentiment_btc"), "etf_flow_sentiment_btc")
    idx["futures_sentiment"] = _alias(
        idx.get("futures_sentiment_btc"), "futures_sentiment_btc")

    return idx


def main() -> int:
    idx = collect()
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    payload = {
        "as_of": today,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "indexes": idx,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{today}.json"

    # pages.yml runs hourly, but this is a DAILY series. Rewrite today's file
    # only when an index value actually moved — comparing `indexes` and
    # ignoring `generated_at`, which changes every run by definition. Without
    # this the repo would take 24 no-op commits a day.
    prev = _load(out)
    if prev and prev.get("indexes") == idx:
        print(f"{out.relative_to(REPO_ROOT)} already current — no change")
        return 0

    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")

    have = [k for k, v in idx.items() if v]
    miss = [k for k, v in idx.items() if not v]
    stale = [k for k, v in idx.items() if v and v.get("stale")]

    print(f"wrote {out.relative_to(REPO_ROOT)}")
    print(f"  captured ({len(have)}): {', '.join(have) or '-'}")
    if stale:
        print(f"  stale-flagged ({len(stale)}): {', '.join(stale)}")
    if miss:
        print(f"  unavailable ({len(miss)}): {', '.join(miss)}")

    # Never fail the build — a missing composite is a gap in the series, not a
    # reason to block a deploy. The freshness watchdog is what raises the alarm.
    return 0


if __name__ == "__main__":
    sys.exit(main())
