"""
Coinbase Pulse — public-API taker buy/sell ratio + insights per coin.

Pulls per-coin live data from the Coinbase Exchange public API
(api.exchange.coinbase.com) — no auth, no API key required:

  1. /products/<X>-USD/ticker  → last price, bid, ask, 24h base volume, time
  2. /products/<X>-USD/stats   → open/high/low/last, 24h + 30d volume
  3. /products/<X>-USD/trades  → last 300 prints (with side + size + time)

From the 300 most recent trades we compute, per coin:

  * buy_ratio_volume — sum(size where side==buy)  / sum(all size)
  * buy_ratio_count  — count(side==buy) / count(all)        [cross-check]
  * buy_ratio_sparkline — bucket buy_ratio_volume into 5-minute windows
    over the trailing ~25 minutes for an inline SVG sparkline.

Coinbase's `side` field reflects the TAKER side of each trade — the side
of the resting order that was hit. So "buy" means an aggressive buyer
crossed the spread, which is the conventional bullish-flow signal.

This is purely additive to V1 — it lives next to fetch_market.py and
writes to data/coinbase.json (parallel to data/market.json and
data/whale.json). app.py loads it in build_payload() and the dashboard
template reads it under DATA.coinbase.

Output: data/coinbase.json
Consumed by: app.py build_payload()  →  DATA.coinbase  →  Coinbase Pulse card.

CLI:
    python fetch_coinbase.py               # live fetch -> data/coinbase.json
    python fetch_coinbase.py --no-network  # self-test with synthetic data
    python fetch_coinbase.py --out PATH    # alternate output path
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)"
H = {"User-Agent": UA}

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DEFAULT_OUT = DATA_DIR / "coinbase.json"

# V1 spec: BTC, ETH, SOL, LINK. (V1 also tracks LTC in its existing
# coinbase_spot widget — we deliberately scope this to the user-requested
# four to keep the payload small and the card readable.)
COINS: list[tuple[str, str]] = [
    ("BTC", "BTC-USD"),
    ("ETH", "ETH-USD"),
    ("SOL", "SOL-USD"),
    ("LINK", "LINK-USD"),
]

BASE = "https://api.exchange.coinbase.com"

# Inter-request delay to be polite to the public API. Spec asked for 200ms.
REQ_DELAY_SEC = 0.20

# Sparkline window: 5-minute buckets over the trailing 25 minutes (5 buckets).
SPARKLINE_BUCKET_MIN = 5
SPARKLINE_BUCKETS = 5

TRADES_LIMIT = 300


# ----- HTTP helpers ----------------------------------------------------------


def _get(url: str, params: dict | None = None, timeout: int = 25) -> Any:
    """GET JSON, returning None on non-200 / network error. Mirrors
    fetch_market.py's `_get` so logs look the same in CI."""
    try:
        r = requests.get(url, params=params, headers=H, timeout=timeout)
        if r.status_code != 200:
            print(f"  [skip] {url} -> {r.status_code}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"  [skip] {url} -> {e}", file=sys.stderr)
        return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Coinbase returns e.g. "2026-05-25T20:59:50.123456Z"
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (ValueError, TypeError):
        return default


# ----- per-coin fetch + compute ---------------------------------------------


def _compute_sparkline(trades: list[dict]) -> list[dict]:
    """Bucket buy_ratio_volume into trailing 5-min windows.

    Returns a list of {"t": ISO bucket-start, "br": float in [0,1]} ordered
    oldest -> newest. Buckets with zero trades are dropped (the card hides
    sparklines that are too sparse to read)."""
    if not trades:
        return []
    # Anchor buckets to the newest trade time (Coinbase server clock) so we
    # don't drift if the build host clock is off.
    newest = None
    for t in trades:
        dt = _parse_iso(t.get("time"))
        if dt is not None and (newest is None or dt > newest):
            newest = dt
    if newest is None:
        return []
    bucket_sec = SPARKLINE_BUCKET_MIN * 60
    window_sec = bucket_sec * SPARKLINE_BUCKETS
    cutoff = newest - timedelta(seconds=window_sec)
    # bucket index 0 = oldest, SPARKLINE_BUCKETS-1 = newest
    buys = [0.0] * SPARKLINE_BUCKETS
    totals = [0.0] * SPARKLINE_BUCKETS
    for t in trades:
        dt = _parse_iso(t.get("time"))
        if dt is None or dt <= cutoff:
            continue
        offset_sec = (dt - cutoff).total_seconds()
        idx = min(SPARKLINE_BUCKETS - 1, max(0, int(offset_sec // bucket_sec)))
        size = _float(t.get("size"))
        side = (t.get("side") or "").lower()
        totals[idx] += size
        if side == "buy":
            buys[idx] += size
    out: list[dict] = []
    for i in range(SPARKLINE_BUCKETS):
        if totals[i] <= 0:
            continue
        bucket_start = cutoff + timedelta(seconds=bucket_sec * i)
        out.append({
            "t": bucket_start.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "br": round(buys[i] / totals[i], 4),
        })
    return out


def _fetch_one(symbol: str, product: str) -> dict | None:
    """Fetch ticker + stats + trades for a single product, compute insights.

    Returns None if the basic ticker call fails (skip this coin); returns a
    partial dict (with what data did come back) if only some sub-calls fail.
    Sleeps REQ_DELAY_SEC between the three HTTP calls."""
    ticker = _get(f"{BASE}/products/{product}/ticker")
    if not isinstance(ticker, dict):
        return None
    time.sleep(REQ_DELAY_SEC)
    stats = _get(f"{BASE}/products/{product}/stats")
    time.sleep(REQ_DELAY_SEC)
    trades = _get(f"{BASE}/products/{product}/trades", {"limit": TRADES_LIMIT})

    price = _float(ticker.get("price"))
    bid = _float(ticker.get("bid"))
    ask = _float(ticker.get("ask"))
    # Spread in basis points relative to the mid. 1 bp = 0.01%.
    spread_bps: float | None = None
    if bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        if mid > 0:
            spread_bps = round((ask - bid) / mid * 10_000, 2)

    open_24h = high_24h = low_24h = change_24h_pct = None
    vol_24h_usd = None
    if isinstance(stats, dict):
        open_24h = _float(stats.get("open")) or None
        high_24h = _float(stats.get("high")) or None
        low_24h = _float(stats.get("low")) or None
        if open_24h and open_24h > 0 and price > 0:
            change_24h_pct = round((price / open_24h - 1) * 100, 3)
        # stats.volume is in BASE units (e.g. BTC). Convert to USD using
        # current price for a comparable cross-coin liquidity number.
        vol_base = _float(stats.get("volume"))
        if vol_base > 0 and price > 0:
            vol_24h_usd = round(vol_base * price, 2)

    buy_ratio_volume: float | None = None
    buy_ratio_count: float | None = None
    sparkline: list[dict] = []
    trade_count = 0
    last_trade_time: str | None = None
    if isinstance(trades, list) and trades:
        trade_count = len(trades)
        total_size = 0.0
        buy_size = 0.0
        buy_count = 0
        for t in trades:
            size = _float(t.get("size"))
            side = (t.get("side") or "").lower()
            total_size += size
            if side == "buy":
                buy_size += size
                buy_count += 1
        if total_size > 0:
            buy_ratio_volume = round(buy_size / total_size, 4)
        if trade_count > 0:
            buy_ratio_count = round(buy_count / trade_count, 4)
        # The newest trade by time — also acts as a "data freshness" probe.
        newest_dt: datetime | None = None
        for t in trades:
            dt = _parse_iso(t.get("time"))
            if dt is not None and (newest_dt is None or dt > newest_dt):
                newest_dt = dt
        if newest_dt is not None:
            last_trade_time = newest_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        sparkline = _compute_sparkline(trades)

    return {
        "price": price or None,
        "bid": bid or None,
        "ask": ask or None,
        "spread_bps": spread_bps,
        "vol_24h_usd": vol_24h_usd,
        "open_24h": open_24h,
        "change_24h_pct": change_24h_pct,
        "high_24h": high_24h,
        "low_24h": low_24h,
        "buy_ratio_volume": buy_ratio_volume,
        "buy_ratio_count": buy_ratio_count,
        "buy_ratio_sparkline": sparkline,
        "trade_count_300": trade_count,
        "last_trade_time": last_trade_time,
    }


# ----- mock for --no-network self-test --------------------------------------


def _mock_payload() -> dict:
    """Deterministic synthetic payload for offline CI / self-test."""
    rng = random.Random(20260525)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    by_coin: dict[str, dict] = {}
    base_price = {"BTC": 77000.0, "ETH": 2900.0, "SOL": 170.0, "LINK": 14.0}
    for sym, _ in COINS:
        p = base_price[sym] * (1 + (rng.random() - 0.5) * 0.02)
        br_vol = round(0.45 + rng.random() * 0.20, 4)
        sparkline = [
            {
                "t": (now - timedelta(minutes=SPARKLINE_BUCKET_MIN * (SPARKLINE_BUCKETS - i)))
                .isoformat().replace("+00:00", "Z"),
                "br": round(0.45 + rng.random() * 0.20, 4),
            }
            for i in range(SPARKLINE_BUCKETS)
        ]
        by_coin[sym] = {
            "price": round(p, 2),
            "bid": round(p - 0.5, 2),
            "ask": round(p + 0.5, 2),
            "spread_bps": round(1.0 / p * 10_000, 2) if p else None,
            "vol_24h_usd": round(p * 10_000, 2),
            "open_24h": round(p * 0.998, 2),
            "change_24h_pct": 0.2,
            "high_24h": round(p * 1.01, 2),
            "low_24h": round(p * 0.99, 2),
            "buy_ratio_volume": br_vol,
            "buy_ratio_count": round(br_vol + (rng.random() - 0.5) * 0.04, 4),
            "buy_ratio_sparkline": sparkline,
            "trade_count_300": 300,
            "last_trade_time": now.isoformat().replace("+00:00", "Z"),
        }
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "source": "Coinbase Exchange public API (mock)",
        "by_coin": by_coin,
        "mock": True,
    }


# ----- entrypoint ------------------------------------------------------------


def build_payload(no_network: bool = False) -> dict:
    """Fetch all coins, compute metrics, return the full payload dict."""
    if no_network:
        return _mock_payload()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    by_coin: dict[str, dict] = {}
    for sym, product in COINS:
        try:
            entry = _fetch_one(sym, product)
        except Exception as e:
            print(f"  [coinbase] {product}: fatal {e}", file=sys.stderr)
            entry = None
        if entry is not None:
            by_coin[sym] = entry
    return {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "source": "Coinbase Exchange public API",
        "by_coin": by_coin,
    }


def main(out: Path | None = None, no_network: bool = False) -> int:
    """Write data/coinbase.json. Stale-fallback: if EVERYTHING fails (zero
    coins fetched and the file already exists), leave the prior file intact.
    """
    out_path = out or DEFAULT_OUT
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_payload(no_network=no_network)
    n = len(payload.get("by_coin") or {})
    if n == 0 and out_path.exists():
        print(
            f"  [coinbase] all {len(COINS)} coins failed — keeping prior {out_path.name}",
            file=sys.stderr,
        )
        return 0
    out_path.write_text(json.dumps(payload, indent=2))
    src = payload.get("source", "")
    print(f"  [coinbase] wrote {out_path.name} ({n}/{len(COINS)} coins, source={src})")
    return 0


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Fetch Coinbase Pulse data for V1 dashboard.")
    ap.add_argument("--out", type=Path, default=None, help="output JSON path (default: data/coinbase.json)")
    ap.add_argument("--no-network", action="store_true", help="self-test with synthetic data (no HTTP calls)")
    args = ap.parse_args()
    return main(out=args.out, no_network=args.no_network)


if __name__ == "__main__":
    sys.exit(_cli())
