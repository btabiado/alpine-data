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

Run from the repo root:  python scripts/snapshot_composites.py
"""
from __future__ import annotations

import json
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


def _series_last_date(series) -> str | None:
    """Newest .date in a [{date,...}] series — the honest as_of for anything
    derived from it."""
    if not isinstance(series, list) or not series:
        return None
    last = series[-1]
    if isinstance(last, dict):
        d = last.get("date")
        if isinstance(d, str):
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
    top20 = market.get("signals_top20")
    if not top20:
        try:
            import signals as _sig
            top20 = _sig.compute_all_top20({"market": market})
        except Exception as e:  # never fail the build over a composite
            print(f"  [composites] signals_top20 recompute skipped: "
                  f"{type(e).__name__}: {e}")
            top20 = []
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
