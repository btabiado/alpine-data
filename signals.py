"""
Rules-based BTC and ETH composite signal.

NOT investment advice. Transparent score from -100 (bearish) to +100 (bullish)
combining trend, momentum, volatility, sentiment, positioning, and ETF flows.
Every component and its contribution is exposed so you can see exactly why the
signal is what it is.
"""

from __future__ import annotations

from typing import Any
import pandas as pd


# ---------- indicator helpers ----------

def _series(rows: list[dict] | None, key: str = "value") -> pd.Series:
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    s = df.set_index("date")[key].astype(float).sort_index()
    # Drop duplicate dates (CoinGecko sometimes returns the same day twice)
    s = s[~s.index.duplicated(keep="last")]
    return s


def _rsi(s: pd.Series, period: int = 14) -> pd.Series:
    if s.empty:
        return s
    d = s.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ru = up.ewm(alpha=1 / period, adjust=False).mean()
    rd = dn.ewm(alpha=1 / period, adjust=False).mean()
    rs = ru / rd.replace(0, pd.NA)
    return 100 - 100 / (1 + rs)


def _macd_hist(s: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.Series:
    if s.empty:
        return s
    ef = s.ewm(span=fast, adjust=False).mean()
    es = s.ewm(span=slow, adjust=False).mean()
    macd = ef - es
    return macd - macd.ewm(span=sig, adjust=False).mean()


# ---------- scoring rules ----------

def _score_at(date, price, sma50, sma200, rsi, macd, funding, fng, etf, dvol_z=None, vix_z=None):
    s = 0
    parts = []

    if date in sma50.index and pd.notna(sma50.loc[date]):
        c = 20 if price.loc[date] > sma50.loc[date] else -20
        s += c; parts.append(("SMA50", c))
    if date in sma200.index and pd.notna(sma200.loc[date]):
        c = 20 if price.loc[date] > sma200.loc[date] else -20
        s += c; parts.append(("SMA200", c))
    if date in rsi.index and pd.notna(rsi.loc[date]):
        v = rsi.loc[date]
        c = 15 if v < 30 else (-15 if v > 70 else 0)
        s += c; parts.append(("RSI", c))
    if date in macd.index and pd.notna(macd.loc[date]):
        c = 10 if macd.loc[date] > 0 else -10
        s += c; parts.append(("MACD", c))
    if not funding.empty:
        # nearest-prior funding value
        f = funding[funding.index <= date]
        if not f.empty:
            v = f.iloc[-1]
            c = 10 if v < 0 else (-10 if v > 1e-4 else 0)
            s += c; parts.append(("Funding", c))
    if not fng.empty:
        f = fng[fng.index <= date]
        if not f.empty:
            v = f.iloc[-1]
            c = 10 if v < 30 else (-10 if v > 70 else 0)
            s += c; parts.append(("F&G", c))
    if not etf.empty:
        last7 = etf[(etf.index <= date) & (etf.index >= date - pd.Timedelta(days=7))]
        if not last7.empty:
            v = float(last7.sum())
            c = 10 if v > 0 else (-10 if v < 0 else 0)
            s += c; parts.append(("ETF7d", c))
    if dvol_z is not None and date in dvol_z.index and pd.notna(dvol_z.loc[date]):
        z = dvol_z.loc[date]
        c = 5 if z < -1 else (-5 if z > 1 else 0)
        s += c; parts.append(("DVOL", c))
    if vix_z is not None and date in vix_z.index and pd.notna(vix_z.loc[date]):
        # VIX is inverse: low VIX = risk-on = bullish; high VIX = risk-off = bearish
        z = vix_z.loc[date]
        c = 5 if z < -1 else (-5 if z > 1 else 0)
        s += c; parts.append(("VIX", c))
    return s, parts


def _label(score: int) -> str:
    if score >= 50: return "STRONG BUY"
    if score >= 20: return "BUY"
    if score > -20: return "HOLD"
    if score > -50: return "SELL"
    return "STRONG SELL"


# ---------- main ----------

def compute_signal(asset: str, payload: dict) -> dict | None:
    mkt = (payload.get("market") or {}).get(asset) or {}
    price = _series(mkt.get("price"))
    if price.empty or len(price) < 30:
        return None

    funding = _series(mkt.get("funding"), key="rate")
    dvol = _series(mkt.get("dvol"), key="dvol")
    fng_rows = (payload.get("market") or {}).get("fear_greed") or []
    fng = _series(fng_rows, key="value")
    etf_daily = (payload.get(asset) or {}).get("daily") or []
    etf = _series(etf_daily, key="flow") if etf_daily else pd.Series(dtype=float)

    sma50 = price.rolling(50).mean()
    sma200 = price.rolling(200).mean()
    rsi = _rsi(price)
    macd = _macd_hist(price)
    dvol_z = ((dvol - dvol.rolling(30).mean()) / dvol.rolling(30).std()) if not dvol.empty else None

    # VIX (macro vol gauge) — same series for every asset; low VIX = risk-on
    vix_series_rows = (((payload.get("market") or {}).get("yahoo_indices") or {}).get("vix") or {}).get("series_90d") or []
    vix = _series(vix_series_rows)
    vix_z = ((vix - vix.rolling(30).mean()) / vix.rolling(30).std()) if not vix.empty and len(vix) > 30 else None

    # Build components for the latest date
    last = price.index[-1]
    last_price = float(price.iloc[-1])
    components: list[dict] = []

    def add(name, value, contribution, explanation):
        components.append({"name": name, "value": value, "contribution": contribution, "explanation": explanation})

    if last in sma50.index and pd.notna(sma50.loc[last]):
        v = float(sma50.loc[last]); diff = (last_price/v - 1)*100
        c = 20 if last_price > v else -20
        add("Price vs SMA50", f"{diff:+.1f}%", c, "above 50d MA — uptrend" if c > 0 else "below 50d MA — downtrend")
    if last in sma200.index and pd.notna(sma200.loc[last]):
        v = float(sma200.loc[last]); diff = (last_price/v - 1)*100
        c = 20 if last_price > v else -20
        add("Price vs SMA200", f"{diff:+.1f}%", c, "above 200d MA — bull regime" if c > 0 else "below 200d MA — bear regime")
    if last in rsi.index and pd.notna(rsi.loc[last]):
        v = float(rsi.loc[last])
        c = 15 if v < 30 else (-15 if v > 70 else 0)
        e = "oversold" if c > 0 else ("overbought" if c < 0 else "neutral")
        add("RSI(14)", f"{v:.1f}", c, e)
    if last in macd.index and pd.notna(macd.loc[last]):
        v = float(macd.loc[last])
        c = 10 if v > 0 else -10
        add("MACD histogram", f"{v:+.1f}", c, "momentum up" if c > 0 else "momentum down")
    if not funding.empty:
        f = funding[funding.index <= last]
        if not f.empty:
            v = float(f.iloc[-1])
            c = 10 if v < 0 else (-10 if v > 1e-4 else 0)
            e = "negative funding — contrarian buy" if c > 0 else ("crowded long" if c < 0 else "neutral positioning")
            add("Funding rate", f"{v*100:.4f}%", c, e)
    if not fng.empty:
        f = fng[fng.index <= last]
        if not f.empty:
            v = int(f.iloc[-1])
            c = 10 if v < 30 else (-10 if v > 70 else 0)
            e = "extreme fear (contrarian buy)" if c > 0 else ("extreme greed (contrarian sell)" if c < 0 else "neutral sentiment")
            add("Fear & Greed", str(v), c, e)
    if not etf.empty:
        age_days = (last - etf.index[-1]).days
        if age_days <= 14:
            recent = etf[etf.index >= last - pd.Timedelta(days=10)]
            v = float(recent.sum())
            c = 10 if v > 0 else (-10 if v < 0 else 0)
            e = "institutional buying" if c > 0 else ("institutional selling" if c < 0 else "no flow")
            add("ETF flow 7d", f"{v:+.0f} $M", c, e)
        else:
            add("ETF flow 7d", "stale", 0, f"latest is {etf.index[-1].strftime('%Y-%m-%d')} ({age_days}d ago) — skipped")
    if dvol_z is not None and last in dvol_z.index and pd.notna(dvol_z.loc[last]):
        z = float(dvol_z.loc[last])
        c = 5 if z < -1 else (-5 if z > 1 else 0)
        e = "vol crushed (long-vol setup)" if c > 0 else ("vol spike (caution)" if c < 0 else "normal vol")
        add("DVOL z-score (30d)", f"{z:+.2f}σ", c, e)
    if vix_z is not None and not vix_z.empty:
        # Use the most recent available VIX z-score (VIX has its own calendar)
        latest_vix = vix_z.dropna()
        if not latest_vix.empty:
            z = float(latest_vix.iloc[-1])
            c = 5 if z < -1 else (-5 if z > 1 else 0)
            e = ("VIX crushed — macro risk-on" if c > 0
                 else "VIX spike — macro risk-off" if c < 0
                 else "VIX normal")
            add("VIX z-score (30d)", f"{z:+.2f}σ", c, e)

    score = sum(c["contribution"] for c in components)

    # 90-day historical score for the chart
    horizon = price.index[-90:] if len(price) >= 90 else price.index
    history = []
    for d in horizon:
        sc, _ = _score_at(d, price, sma50, sma200, rsi, macd, funding, fng, etf, dvol_z, vix_z)
        history.append({"date": d.strftime("%Y-%m-%d"), "score": int(sc), "price": float(price.loc[d])})

    return {
        "score": int(score),
        "label": _label(score),
        "components": components,
        "as_of": last.strftime("%Y-%m-%d"),
        "price": last_price,
        "history": history,
        "disclaimer": "Rules-based indicator. Not investment advice. Evaluate on your own.",
    }


def compute_all(payload: dict) -> dict:
    return {
        "btc": compute_signal("btc", payload),
        "eth": compute_signal("eth", payload),
        "link": compute_signal("link", payload),
        "ltc": compute_signal("ltc", payload),
    }


# ---------- simplified top-20 signal (CoinGecko sparkline only) ----------
# The 4-coin signal above uses funding (OKX), DVOL (Deribit), Fear & Greed,
# and ETF flows — none of which we have for the long tail (SOL, XRP, ADA,
# etc.). The simplified scorer below works from ONLY the markets_top entry
# (price + 24h/7d/30d % change + 168-hour sparkline + volume + mcap).
# Output shape matches compute_signal() so the same render logic can be
# reused for both. Score still bounded ±100; label thresholds unchanged.

from datetime import datetime, timezone

_STABLE_PREFIX = "USD"  # USDT, USDC, USDS, USD1, USDe, USDP, ...

def _is_stable_symbol(sym: str) -> bool:
    s = (sym or "").upper()
    return s.startswith(_STABLE_PREFIX) or s.endswith(_STABLE_PREFIX) or s == "DAI"


def _rsi_from_list(prices: list[float], period: int = 14) -> float | None:
    """Wilder RSI on a plain list of prices. Returns None if too short."""
    if not prices or len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        d = prices[i] - prices[i-1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    a = 1.0 / period
    g, l = gains[0], losses[0]
    for i in range(1, len(gains)):
        g = a * gains[i] + (1 - a) * g
        l = a * losses[i] + (1 - a) * l
    if l == 0:
        return 100.0
    rs = g / l
    return 100 - 100 / (1 + rs)


def _slope_pct(prices: list[float]) -> float | None:
    """OLS slope of `prices` (assumed evenly-spaced) normalized to total
    % change over the full window. Returns None if too few points."""
    s = [p for p in prices if p is not None]
    n = len(s)
    if n < 10:
        return None
    xm = (n - 1) / 2.0
    ym = sum(s) / n
    cov = sum((i - xm) * (s[i] - ym) for i in range(n))
    var = sum((i - xm) ** 2 for i in range(n))
    if var == 0 or ym == 0:
        return None
    return (cov / var / ym) * 100 * n


def compute_signal_simple(coin: dict) -> dict | None:
    """Score one markets_top entry on the 6-component simplified system.
    See module-top docstring for component breakdown. Returns None if
    sparkline is too short to compute.

    Field names match the actual markets_top shape from fetch_market.py
    (price_usd, market_cap_usd, volume_24h_usd, change_*_pct, sparkline_7d,
    rank, symbol, name, image)."""
    if not coin:
        return None
    spark = coin.get("sparkline_7d") or []
    if len(spark) < 30:
        return None
    comps: list[dict] = []
    def add(name, value, c, expl):
        comps.append({"name": name, "value": value,
                      "contribution": int(c), "explanation": expl})

    c7   = coin.get("change_7d_pct")
    c30  = coin.get("change_30d_pct")
    c24  = coin.get("change_24h_pct")
    vol  = coin.get("volume_24h_usd") or 0
    mcap = coin.get("market_cap_usd") or 0
    price = coin.get("price_usd") or (spark[-1] if spark else None)

    # 1) 7d momentum  — saturates at ±10% → ±25
    if c7 is not None:
        c = max(-25, min(25, round(c7 / 10 * 25)))
        add("7d momentum", f"{c7:+.1f}%", c,
            "trending up" if c > 0 else "trending down" if c < 0 else "flat")
    # 2) 30d momentum — saturates at ±25% → ±25
    if c30 is not None:
        c = max(-25, min(25, round(c30 / 25 * 25)))
        add("30d momentum", f"{c30:+.1f}%", c,
            "strong uptrend" if c > 0 else "downtrend" if c < 0 else "flat")
    # 3) Sparkline OLS slope (7d) → ±15
    sp = _slope_pct(spark)
    if sp is not None:
        c = max(-15, min(15, round(sp / 15 * 15)))
        add("7d trend slope", f"{sp:+.1f}%", c,
            "rising" if c > 0 else "falling" if c < 0 else "sideways")
    # 4) 24h contrarian (mean-reversion signal) → ±10
    if c24 is not None:
        if   c24 <= -8: c, e = 10,  "oversold 24h — contrarian buy"
        elif c24 >=  8: c, e = -10, "overbought 24h — contrarian sell"
        elif c24 <= -4: c, e = 5,   "weak 24h — mild contrarian buy"
        elif c24 >=  4: c, e = -5,  "hot 24h — mild contrarian sell"
        else:           c, e = 0,   "neutral 24h"
        add("24h contrarian", f"{c24:+.2f}%", c, e)
    # 5) Volume turnover (vol/mcap), confirming-or-divergent → ±10/±15
    if mcap > 0 and vol > 0:
        t = vol / mcap
        up = (c7 or 0) >= 0
        if   t > 0.15: c, e = (10 if up else -10), ("volume surge confirming trend" if up else "volume surge on weakness")
        elif t > 0.08: c, e = (5  if up else -5 ), ("elevated volume"               if up else "elevated sell volume")
        elif t < 0.02: c, e = -5, "thin volume — weak conviction"
        else:          c, e = 0,  "normal turnover"
        add("Volume turnover", f"{t*100:.1f}%/d", c, e)
    # 6) RSI(14) on hourly sparkline → ±10
    rsi = _rsi_from_list(spark, 14)
    if rsi is not None:
        if   rsi < 30: c, e = 10,  "oversold"
        elif rsi > 70: c, e = -10, "overbought"
        elif rsi < 40: c, e = 5,   "leaning oversold"
        elif rsi > 60: c, e = -5,  "leaning overbought"
        else:          c, e = 0,   "neutral"
        add("RSI(14) sparkline", f"{rsi:.1f}", c, e)

    score = max(-100, min(100, sum(x["contribution"] for x in comps)))
    return {
        "score": int(score),
        "label": _label(score),
        "components": comps,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "price": float(price) if price else None,
        "symbol": coin.get("symbol"),
        "name": coin.get("name"),
        "rank": coin.get("rank"),
        "image": coin.get("image"),
        "disclaimer": "Rules-based indicator (simplified — based on CoinGecko "
                      "price/volume only, no funding/ETF/F&G). Not investment "
                      "advice. Evaluate on your own.",
    }


def compute_all_top20(payload: dict, exclude_stables: bool = True,
                     limit: int = 50) -> list[dict]:
    """Iterate the top N by market cap (default 50; legacy name "top20" kept
    for backward compat with the payload key + JS identifiers), compute the
    simplified signal for each, return sorted by score descending (strongest
    BUY first, strongest SELL last). Stablecoins excluded by default."""
    rows = ((payload.get("market") or {}).get("markets_top") or [])
    if exclude_stables:
        rows = [r for r in rows if not _is_stable_symbol(r.get("symbol", ""))]
    rows = rows[:limit]
    out = [s for s in (compute_signal_simple(r) for r in rows) if s]
    out.sort(key=lambda s: s["score"], reverse=True)
    return out
