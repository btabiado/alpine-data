"""Free historical series for Stage 3: settlement outcomes, spot closes, DVOL (research/stage1_spot_features.md).

Every series is stored with the time each value became *available*, not just its label date:
  settlement  Deribit delivery price = 30-min TWAP of the index ending 08:00 UTC; available 08:00 UTC that day
  spot        venue daily candle (bar open 00:00 UTC); its close is available 00:00 UTC the next day
  dvol        Deribit DVOL daily candle (bar open 00:00 UTC); close available 00:00 UTC the next day
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from .http import JsonClient, RawArchive

SETTLEMENT_INDEX = {"BTC": "btc_usd", "ETH": "eth_usd", "XRP": "xrp_usdc", "LINK": "link_usdc"}
SPOT_ROUTE = {  # Stage 1 recommendations: never splice venues
    "BTC": ("coinbase", "BTC-USD"), "ETH": ("coinbase", "ETH-USD"),
    "XRP": ("bitstamp", "xrpusd"), "LINK": ("coinbase", "LINK-USD"),
}
DVOL_CURRENCY = {"BTC": "BTC", "ETH": "ETH"}


def _client(base, archive, venue, interval=0.35):
    return JsonClient(base, archive, venue, min_interval_s=interval, max_retries=3, backoff_s=2.0, timeout_s=30)


def settlement_prices(asset: str, archive: RawArchive | None) -> pd.DataFrame:
    c = _client("https://www.deribit.com/api/v2", archive, "deribit")
    name, rows, offset = SETTLEMENT_INDEX[asset], [], 0
    while True:
        fr = c.get("public/get_delivery_prices", {"index_name": name, "offset": offset, "count": 100})
        if not fr.ok or "result" not in fr.data:
            raise RuntimeError(f"delivery prices {name}: {fr.error or fr.data}")
        res = fr.data["result"]
        rows.extend(res["data"])
        offset += len(res["data"])
        if not res["data"] or offset >= res["records_total"]:
            break
    df = pd.DataFrame(rows).rename(columns={"delivery_price": "price"})
    df["date"] = pd.to_datetime(df["date"]).dt.tz_localize("UTC")
    df["obs_time"] = df["date"] + pd.Timedelta(hours=8)          # TWAP window ends 08:00 UTC
    df["available_time"] = df["obs_time"]
    df = df.sort_values("date").drop_duplicates("date")
    return df.assign(asset=asset, series="settlement", source=f"deribit:{name}")[
        ["asset", "series", "source", "date", "obs_time", "available_time", "price"]]


def spot_closes(asset: str, archive: RawArchive | None, start: str = "2014-01-01") -> pd.DataFrame:
    venue, pair = SPOT_ROUTE[asset]
    now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = []
    if venue == "coinbase":
        c = _client("https://api.exchange.coinbase.com", archive, "coinbase")
        t = pd.Timestamp(start, tz="UTC").to_pydatetime()
        while t < now:
            t2 = min(t + timedelta(days=299), now)
            fr = c.get(f"products/{pair}/candles", {"granularity": 86400, "start": t.isoformat(), "end": t2.isoformat()})
            if not fr.ok:
                raise RuntimeError(f"coinbase {pair}: {fr.error}")
            rows.extend({"open_time": r[0], "close": r[4]} for r in fr.data)
            t = t2
    else:
        c = _client("https://www.bitstamp.net/api/v2", archive, "bitstamp")
        t = int(pd.Timestamp(start, tz="UTC").timestamp())
        while t < now.timestamp():
            fr = c.get(f"ohlc/{pair}/", {"step": 86400, "limit": 1000, "start": t})
            if not fr.ok:
                raise RuntimeError(f"bitstamp {pair}: {fr.error}")
            bars = fr.data.get("data", {}).get("ohlc", [])
            if not bars:
                t += 1000 * 86400
                continue
            rows.extend({"open_time": int(b["timestamp"]), "close": float(b["close"])} for b in bars)
            t = int(bars[-1]["timestamp"]) + 86400
    df = pd.DataFrame(rows).drop_duplicates("open_time")
    df["date"] = pd.to_datetime(df["open_time"], unit="s", utc=True)
    df = df[df["date"] < pd.Timestamp(now)]                       # drop the in-progress bar
    df["obs_time"] = df["date"] + pd.Timedelta(days=1)            # close is the 00:00 UTC next-day print
    df["available_time"] = df["obs_time"]
    df = df.sort_values("date").rename(columns={"close": "price"})
    return df.assign(asset=asset, series="spot", source=f"{venue}:{pair}")[
        ["asset", "series", "source", "date", "obs_time", "available_time", "price"]]


def dvol(asset: str, archive: RawArchive | None) -> pd.DataFrame:
    if asset not in DVOL_CURRENCY:
        return pd.DataFrame(columns=["asset", "date", "available_time", "dvol"])
    c = _client("https://www.deribit.com/api/v2", archive, "deribit")
    end = int(datetime.now(timezone.utc).timestamp() * 1000)
    start = int(pd.Timestamp("2021-03-01", tz="UTC").timestamp() * 1000)
    rows, t = [], start
    while t < end:
        t2 = min(t + 900 * 86400 * 1000, end)
        fr = c.get("public/get_volatility_index_data", {"currency": DVOL_CURRENCY[asset], "start_timestamp": t,
                                                        "end_timestamp": t2, "resolution": "1D"})
        if not fr.ok or "result" not in fr.data:
            raise RuntimeError(f"dvol {asset}: {fr.error or fr.data}")
        rows.extend({"ts": r[0], "dvol": r[4]} for r in fr.data["result"]["data"])
        t = t2 + 1
    df = pd.DataFrame(rows).drop_duplicates("ts")
    df["date"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    today = pd.Timestamp(datetime.now(timezone.utc).date(), tz="UTC")
    df = df[df["date"] < today]
    df["available_time"] = df["date"] + pd.Timedelta(days=1)       # daily close known at next 00:00 UTC
    return df.sort_values("date").assign(asset=asset)[["asset", "date", "available_time", "dvol"]]
