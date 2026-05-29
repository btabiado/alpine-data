"""
BTC & ETH ETF Flow Dashboard
============================

Reads daily flow data from data/btc_flows.csv and data/eth_flows.csv,
aggregates by day/week/month/year, and generates a self-contained
interactive HTML dashboard (dashboard.html).

Run:
    python app.py            # build + open dashboard
    python app.py --no-open  # just build
    python app.py --fetch    # fetch live first (needs API key, see fetch_live.py)

CSV schema (wide, USD millions, negative = outflow):
    date,IBIT,FBTC,BITB,...,Total
    2024-01-11,...,...
A "Total" column is optional; if missing, it's computed by summing the
other numeric columns.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
OUT = ROOT / "dashboard.html"

# Payload keys that get extracted into separate static files instead of
# being inlined in dashboard.html. Each sidecar is fetched lazily by the
# client only when the user actually opens the tab that needs it, cutting
# ~840KB off the initial HTML payload (whale alone is ~736KB of JSON).
# Add a key here to defer it; the client manifest in SIDECARS picks it up
# automatically. Keys MUST be top-level payload keys.
SIDECAR_KEYS: tuple[str, ...] = ("whale", "defi")

# Cap fear_greed history at the longest dashboard range button (3y). The
# alternative.me API ignores its ``?limit=`` query and returns the full
# history back to 2018, but the dashboard's range selector tops out at 3y
# (1095 days), so older entries just bloat the inlined payload. Trimming
# at build time also benefits stale caches that pre-date the fetcher fix
# — no need to re-fetch to see the size drop.
FEAR_GREED_MAX_DAYS = 1095


def split_payload_for_sidecars(
    payload: dict, keys: tuple[str, ...] = SIDECAR_KEYS
) -> tuple[dict, dict[str, dict], dict[str, str]]:
    """Pop ``keys`` out of ``payload`` and return:
      - trimmed payload (suitable for inlining in dashboard.html),
      - dict of sidecar payloads keyed by name (for writing to disk),
      - manifest mapping ``{key: "data-<key>.json"}`` for the JS loader.

    Keys that are absent or empty in ``payload`` are skipped — the manifest
    only points at sidecars that actually exist, so the client doesn't fire
    fetches that would 404.
    """
    trimmed = dict(payload)
    sidecars: dict[str, dict] = {}
    manifest: dict[str, str] = {}
    for k in keys:
        v = trimmed.get(k)
        if not v:
            continue
        sidecars[k] = trimmed.pop(k)
        manifest[k] = f"data-{k}.json"
    return trimmed, sidecars, manifest


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        print(f"  [skip] {path.name} not found", file=sys.stderr)
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "date" not in df.columns and "Date" in df.columns:
        df = df.rename(columns={"Date": "date"})
    if "date" not in df.columns:
        raise ValueError(f"{path}: missing 'date' column")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    for c in df.columns:
        if c == "date":
            continue
        df[c] = pd.to_numeric(
            df[c]
            .astype(str)
            .str.replace(",", "", regex=False)
            .str.replace("(", "-", regex=False)
            .str.replace(")", "", regex=False)
            .str.replace("$", "", regex=False)
            .str.strip(),
            errors="coerce",
        )
    return df


def ensure_total(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    total_col = next((c for c in df.columns if c.lower() == "total"), None)
    if total_col is None:
        numeric = df.drop(columns=["date"]).select_dtypes("number")
        df = df.copy()
        df["Total"] = numeric.sum(axis=1)
    else:
        df = df.rename(columns={total_col: "Total"})
    return df


def aggregate(df: pd.DataFrame) -> dict:
    if df.empty:
        return {
            "daily": [], "weekly": [], "monthly": [], "yearly": [],
            "cumulative": [], "by_fund": [], "yoy": {},
            "stats": {}, "funds": [], "last_date": None,
        }

    daily = df[["date", "Total"]].rename(columns={"Total": "flow"}).copy()
    daily["cumulative"] = daily["flow"].cumsum()

    s = df.set_index("date")["Total"]
    weekly = s.resample("W-MON", label="left", closed="left").sum().reset_index()
    weekly.columns = ["date", "flow"]
    weekly["cumulative"] = weekly["flow"].cumsum()

    monthly = s.resample("MS").sum().reset_index()
    monthly.columns = ["date", "flow"]
    monthly["cumulative"] = monthly["flow"].cumsum()

    yearly = s.resample("YS").sum().reset_index()
    yearly.columns = ["date", "flow"]
    yearly["cumulative"] = yearly["flow"].cumsum()

    try:
        from fund_meta import name_for as _fund_name
    except Exception:
        def _fund_name(s): return s

    fund_cols = [c for c in df.columns if c not in ("date", "Total")]
    fund_cols = [c for c in fund_cols if pd.api.types.is_numeric_dtype(df[c])]

    max_d = df["date"].max()
    win30 = max_d - pd.Timedelta(days=30)
    win60 = max_d - pd.Timedelta(days=60)
    win90 = max_d - pd.Timedelta(days=90)

    all_time_abs = sum(abs(float(df[c].sum())) for c in fund_cols) or 1.0
    by_fund = []
    by_fund_daily = {}
    for c in fund_cols:
        total = float(df[c].sum())
        last_30 = float(df[df["date"] >= win30][c].sum())
        last_60 = float(df[df["date"] >= win60][c].sum())
        last_90 = float(df[df["date"] >= win90][c].sum())
        by_fund.append({
            "fund": c,
            "name": _fund_name(c),
            "total": total,
            "last_30d": last_30,
            "last_60d": last_60,
            "last_90d": last_90,
            "share_pct": (abs(total) / all_time_abs) * 100.0,
            "last_flow": float(df[c].iloc[-1]),
            "last_date": df["date"].iloc[-1].strftime("%Y-%m-%d"),
        })
        # Daily series for charts (date, flow, cumulative)
        series = df[["date", c]].copy()
        series["cum"] = series[c].cumsum()
        by_fund_daily[c] = [
            {"date": r["date"].strftime("%Y-%m-%d"),
             "flow": float(r[c]) if pd.notna(r[c]) else 0.0,
             "cumulative": float(r["cum"]) if pd.notna(r["cum"]) else 0.0}
            for _, r in series.iterrows()
        ]
    by_fund.sort(key=lambda r: r["total"], reverse=True)

    yoy = {}
    df_y = df.copy()
    df_y["year"] = df_y["date"].dt.year
    df_y["doy"] = df_y["date"].dt.dayofyear
    for year, grp in df_y.groupby("year"):
        cum = grp.sort_values("date")["Total"].cumsum().tolist()
        doy = grp["doy"].tolist()
        yoy[str(int(year))] = {"doy": doy, "cumulative": cum}

    last = df.iloc[-1]
    last_7 = df.tail(7)["Total"].sum()
    last_30 = df.tail(30)["Total"].sum()
    ytd = df[df["date"].dt.year == df["date"].max().year]["Total"].sum()
    streak = streak_calc(df["Total"].tolist())

    stats = {
        "last_day_flow": float(last["Total"]),
        "last_date": last["date"].strftime("%Y-%m-%d"),
        "last_7d": float(last_7),
        "last_30d": float(last_30),
        "ytd": float(ytd),
        "all_time": float(df["Total"].sum()),
        "streak": streak,
    }

    def to_records(d):
        out = []
        for _, r in d.iterrows():
            out.append({
                "date": r["date"].strftime("%Y-%m-%d"),
                "flow": float(r["flow"]) if pd.notna(r["flow"]) else 0.0,
                "cumulative": float(r["cumulative"]) if pd.notna(r["cumulative"]) else 0.0,
            })
        return out

    return {
        "daily": to_records(daily),
        "weekly": to_records(weekly),
        "monthly": to_records(monthly),
        "yearly": to_records(yearly),
        "by_fund": by_fund,
        "by_fund_daily": by_fund_daily,
        "yoy": yoy,
        "stats": stats,
        "funds": fund_cols,
        "last_date": last["date"].strftime("%Y-%m-%d"),
    }


def streak_calc(values: list[float]) -> dict:
    if not values:
        return {"direction": "flat", "length": 0}
    direction = "up" if values[-1] > 0 else ("down" if values[-1] < 0 else "flat")
    length = 0
    for v in reversed(values):
        if (direction == "up" and v > 0) or (direction == "down" and v < 0):
            length += 1
        else:
            break
    return {"direction": direction, "length": length}


def build_payload() -> dict:
    """Read CSVs + JSON caches and return the full dashboard payload."""
    btc_df = ensure_total(load_csv(DATA_DIR / "btc_flows.csv"))
    eth_df = ensure_total(load_csv(DATA_DIR / "eth_flows.csv"))
    market = load_json(DATA_DIR / "market.json")
    whale = load_json(DATA_DIR / "whale.json")
    # Coinbase Pulse — taker buy/sell ratio per coin (BTC/ETH/SOL/LINK)
    # written by fetch_coinbase.py. Hangs off market.coinbase_pulse so the
    # dashboard reads it under DATA.market.coinbase_pulse (no sidecar; the
    # payload is tiny — ~4 coins × ~15 fields). Missing file -> empty dict,
    # renderer hides its card.
    coinbase_pulse = load_json(DATA_DIR / "coinbase.json")
    if isinstance(market, dict) and isinstance(coinbase_pulse, dict) and coinbase_pulse:
        market["coinbase_pulse"] = coinbase_pulse
    # Defensive cap on fear_greed history (see FEAR_GREED_MAX_DAYS docstring).
    # Done here instead of (only) at fetch so stale on-disk caches that
    # pre-date the fetcher's slice still get trimmed in the inlined payload.
    if isinstance(market, dict):
        fng = market.get("fear_greed")
        if isinstance(fng, list) and len(fng) > FEAR_GREED_MAX_DAYS:
            market["fear_greed"] = fng[-FEAR_GREED_MAX_DAYS:]
    # Promote market.defi to a top-level payload key so it can be split out
    # as a lazy-loaded sidecar (see SIDECAR_KEYS). The DeFi tab is the only
    # consumer; renderers read from DATA.defi after this hoist.
    defi = None
    if isinstance(market, dict):
        defi = market.pop("defi", None)
    payload = {
        "btc": aggregate(btc_df),
        "eth": aggregate(eth_df),
        "market": market,
        "whale": whale,
        "defi": defi or {},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        import signals as sig_mod
        payload["signals"] = sig_mod.compute_all(payload)
        # Top-20 simplified signals (one per market-cap top-20 coin, sorted
        # by score). Uses ONLY markets_top fields — no funding/ETF/F&G,
        # since those don't exist for the long tail. Empty list if
        # markets_top is unpopulated.
        try:
            payload["signals_top20"] = sig_mod.compute_all_top20(payload)
        except Exception as e:
            print(f"[signals_top20] error: {e}", file=sys.stderr)
            payload["signals_top20"] = []
    except Exception as e:
        print(f"[signals] error: {e}", file=sys.stderr)
        payload["signals"] = {"btc": None, "eth": None}
        payload["signals_top20"] = []
    try:
        # Point of Control + Value Area derived from existing price+volume.
        # No external API call — pure compute. Attached under market.poc.
        import fetch_market as fm_mod
        if isinstance(market, dict):
            market["poc"] = fm_mod.compute_poc_all(market)
    except Exception as e:
        print(f"[poc] error: {e}", file=sys.stderr)
    try:
        # Whale Sentiment Index — composite ±100 from existing on-chain
        # proxies. Pure compute. Attached under whale.sentiment.
        import fetch_market as fm_mod2
        if isinstance(whale, dict):
            whale["sentiment"] = fm_mod2.compute_whale_sentiment(whale)
    except Exception as e:
        print(f"[whale-sentiment] error: {e}", file=sys.stderr)
    try:
        # ETH parallel of the whale sentiment index. Attaches under
        # whale.eth.sentiment so the ETH whale panel can render its
        # own gauge card. Defensive: must never crash the build.
        import fetch_market as fm_mod_eth
        if isinstance(whale, dict):
            whale.setdefault("eth", {})["sentiment"] = (
                fm_mod_eth.compute_whale_sentiment_eth(whale)
            )
    except Exception as e:
        print(f"[whale-sentiment-eth] error: {e}", file=sys.stderr)
    try:
        import insights as ins_mod
        # Was limit=12 (Overview "Top insights" only needed 4). With every
        # tab now contributing its own insight pool, 12 wasn't enough to
        # survive cross-tab competition — high-scoring etf/trading/signals
        # entries filled all 12 slots and ainews/poc/social/etc bars showed
        # empty even when their rules fired. Bumped to 60 so each per-tab
        # insights bar has a real pool to filter from. Cost: ~5KB of payload.
        payload["insights"] = ins_mod.build_insights(payload, limit=60)
    except Exception as e:
        print(f"[insights] error: {e}", file=sys.stderr)
        payload["insights"] = []
    # LTHCS Composite Index summary — surfaces the long-term holding
    # conviction score for the Stocks tab and the standalone LTHCS tab.
    # Reads the latest dated JSON written by lthcs/index_aggregate.py +
    # the latest universe snapshot for the top-movers row. Defensive:
    # missing files render an empty-state placeholder, never crash.
    try:
        payload["lthcs"] = build_lthcs_payload()
    except Exception as e:
        print(f"[lthcs] error: {e}", file=sys.stderr)
        payload["lthcs"] = {}
    return payload


def _latest_dated_json(dir_path: Path) -> Path | None:
    """Return the lexically-greatest *.json under dir_path (date-named
    files like 2026-05-17.json sort correctly lexically). Ignores files
    that aren't pure date stems (e.g. snapshots/index.json which is a
    manifest, not a daily file)."""
    if not dir_path.exists() or not dir_path.is_dir():
        return None
    candidates = []
    for p in dir_path.glob("*.json"):
        stem = p.stem
        # YYYY-MM-DD is 10 chars, all digits + hyphens
        if len(stem) == 10 and stem[4] == "-" and stem[7] == "-":
            candidates.append(p)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stem)


def build_lthcs_payload() -> dict:
    """Build the LTHCS payload subtree for the Stocks + LTHCS tabs.

    Returns:
        {
          "available": bool,
          "index": <composite-index dict from data/lthcs/index/<date>.json>,
          "as_of": "<YYYY-MM-DD>",
          "movers": {"gainers": [...], "decliners": [...]},
          "universe_count": int,
          "insights": [ ... 3-5 insight dicts ... ],
        }
    On any error or missing data, returns {"available": False}.
    """
    index_dir = DATA_DIR / "lthcs" / "index"
    snap_dir = DATA_DIR / "lthcs" / "snapshots"
    index_file = _latest_dated_json(index_dir)
    snap_file = _latest_dated_json(snap_dir)
    if index_file is None and snap_file is None:
        return {"available": False}
    out: dict = {"available": False}
    if index_file is not None:
        idx = load_json(index_file)
        if idx:
            out["index"] = idx
            out["as_of"] = idx.get("as_of") or index_file.stem
            out["available"] = True
    if snap_file is not None:
        snap = load_json(snap_file)
        scores = snap.get("scores") if isinstance(snap, dict) else None
        if isinstance(scores, list) and scores:
            # Top movers by 30d drift (the snapshot field).
            def _drift(row): return row.get("drift_30d") or 0.0
            def _row(row):
                return {
                    "ticker": row.get("ticker"),
                    "score": row.get("lthcs_score"),
                    "band": row.get("band"),
                    "drift_30d": row.get("drift_30d"),
                    "sector": row.get("sector"),
                    "subscores": row.get("subscores") or {},
                }
            sorted_by_drift = sorted(
                [r for r in scores if r.get("ticker")],
                key=_drift,
                reverse=True,
            )
            out["movers"] = {
                "gainers": [_row(r) for r in sorted_by_drift[:5]],
                "decliners": [_row(r) for r in sorted_by_drift[-5:][::-1]],
            }
            out["universe_count"] = len(scores)
            out.setdefault("as_of", snap.get("calc_date"))
            out["available"] = True
    # Generate dynamic insights (3-5 items) from the auxiliary LTHCS files
    # (insider, holdings, macro breadth, sector strength, history). Best-effort:
    # any single source missing is silently skipped; the panel falls back to
    # the "none right now" placeholder when zero insights survive.
    try:
        out["insights"] = compute_lthcs_insights(
            as_of=out.get("as_of"),
            index_today=out.get("index") or {},
            snap_scores=(load_json(snap_file).get("scores") if snap_file else []) or [],
        )
    except Exception as e:
        print(f"[lthcs] insights error: {e}", file=sys.stderr)
        out["insights"] = []
    return out


def compute_lthcs_insights(
    as_of: str | None,
    index_today: dict,
    snap_scores: list,
) -> list:
    """Generate 3-5 insight cards for the LTHCS tab.

    Insights drawn from (in priority order):
      1. Cluster-insider-buying breadth      (insider/<date>.json)
      2. Heavy insider distribution          (insider/<date>.json)
      3. 13F accumulators (signal_score>+0.5)(holdings/<date>.json)
      4. 13F distributors (signal_score<-0.3)(holdings/<date>.json)
      5. Composite Index 1d delta            (index/<date>.json deltas)
      6. Macro regime flags                  (macro/breadth_<date>.json)
      7. Sector leader / laggard 1m          (macro/sector_strength_<date>.json)
      8. Band moves vs. yesterday            (history/by_ticker/<TICKER>.json)

    Returns top 3-5 by severity (high > medium > low), with category
    diversity preferred.
    """
    insider_dir = DATA_DIR / "lthcs" / "insider"
    holdings_dir = DATA_DIR / "lthcs" / "holdings"
    macro_dir = DATA_DIR / "lthcs" / "macro"
    index_dir = DATA_DIR / "lthcs" / "index"
    history_dir = DATA_DIR / "lthcs" / "history" / "by_ticker"

    candidates: list[dict] = []
    SEV_RANK = {"high": 0, "medium": 1, "low": 2}

    # ---- (1) and (2): insider signals ----
    insider_file = _latest_dated_json(insider_dir)
    if insider_file is not None:
        insider = load_json(insider_file)
        if isinstance(insider, dict):
            cluster = [t for t, v in insider.items()
                       if isinstance(v, dict) and v.get("cluster_buying")]
            if len(cluster) >= 3:
                tail = ", ".join(sorted(cluster)[:5])
                candidates.append({
                    "category": "insider",
                    "icon": "🔥",
                    "headline": f"{len(cluster)} cluster_buying flags this week",
                    "detail": f"{tail} — strongest single insider signal in the universe",
                    "severity": "high",
                })
            heavy = [
                (t, float(v.get("net_dollar_value") or 0.0))
                for t, v in insider.items()
                if isinstance(v, dict)
                and v.get("regime") == "heavy_selling"
                and v.get("ceo_cfo_action") == "selling"
            ]
            if len(heavy) >= 10:
                top3 = sorted(heavy, key=lambda x: x[1])[:3]
                tail = ", ".join(t for t, _ in top3)
                candidates.append({
                    "category": "insider",
                    "icon": "📉",
                    "headline": f"{len(heavy)} tickers with CEO/CFO heavy distribution",
                    "detail": f"largest net-$ sellers: {tail}",
                    "severity": "medium",
                })

    # ---- (3) and (4): 13F conviction ----
    holdings_file = _latest_dated_json(holdings_dir)
    if holdings_file is not None:
        holdings = load_json(holdings_file)
        if isinstance(holdings, dict):
            accum = [
                (t, float(v.get("signal_score") or 0.0))
                for t, v in holdings.items()
                if isinstance(v, dict)
                and v.get("conviction_signal") == "accumulating"
                and float(v.get("signal_score") or 0.0) > 0.5
            ]
            dist = [
                (t, float(v.get("signal_score") or 0.0))
                for t, v in holdings.items()
                if isinstance(v, dict)
                and float(v.get("signal_score") or 0.0) < -0.3
            ]
            if accum:
                top3 = sorted(accum, key=lambda x: -x[1])[:3]
                tail = ", ".join(f"{t} (+{s:.2f})" for t, s in top3)
                candidates.append({
                    "category": "13F",
                    "icon": "🏦",
                    "headline": f"{len(accum)} tickers with strong 13F accumulation",
                    "detail": f"top conviction: {tail}",
                    "severity": "medium" if len(accum) >= 3 else "low",
                })
            if dist:
                top3 = sorted(dist, key=lambda x: x[1])[:3]
                tail = ", ".join(f"{t} ({s:+.2f})" for t, s in top3)
                candidates.append({
                    "category": "13F",
                    "icon": "💼",
                    "headline": f"{len(dist)} tickers with 13F distribution",
                    "detail": f"largest sellers: {tail}",
                    "severity": "medium" if len(dist) >= 5 else "low",
                })

    # ---- (5): Composite Index 1d delta ----
    if index_today and as_of:
        try:
            today = datetime.strptime(as_of, "%Y-%m-%d")
            from datetime import timedelta as _td
            for back in range(1, 8):
                yest = today - _td(days=back)
                yfile = index_dir / f"{yest.strftime('%Y-%m-%d')}.json"
                if yfile.exists():
                    y = load_json(yfile)
                    if y and "score" in y and "score" in index_today:
                        s_today = float(index_today.get("score") or 0)
                        s_yest = float(y.get("score") or 0)
                        delta = s_today - s_yest
                        if abs(delta) >= 1:
                            sev = "high" if abs(delta) >= 10 else \
                                  "medium" if abs(delta) >= 5 else "low"
                            arrow = "▲" if delta > 0 else "▼"
                            icon = "📈" if delta > 0 else "📉"
                            candidates.append({
                                "category": "composite",
                                "icon": icon,
                                "headline": (
                                    f"Composite Index moved "
                                    f"{s_yest:+.0f} → {s_today:+.0f} "
                                    f"({arrow}{abs(delta):.0f}) over {back}d"
                                ),
                                "detail": (
                                    f"{index_today.get('label','LTHCS')} band "
                                    f"({index_today.get('band_key','—')})"
                                ),
                                "severity": sev,
                            })
                    break
        except Exception:
            pass

    # ---- (6): Macro regime ----
    breadth_file = None
    if macro_dir.exists():
        b_candidates = list(macro_dir.glob("breadth_*.json"))
        if b_candidates:
            breadth_file = max(b_candidates, key=lambda p: p.stem)
    if breadth_file is not None:
        breadth = load_json(breadth_file)
        flags = (breadth or {}).get("regime_flags") or {}
        tripped = [k for k, v in flags.items() if v]
        if tripped:
            candidates.append({
                "category": "regime",
                "icon": "⚠️",
                "headline": f"Macro regime flag tripped: {', '.join(tripped)}",
                "detail": "headwind to long-term holding conviction",
                "severity": "high",
            })
        else:
            candidates.append({
                "category": "regime",
                "icon": "📈",
                "headline": "Risk-on macro backdrop",
                "detail": "HY OAS, yield curve, and broad dollar all clean",
                "severity": "low",
            })

    # ---- (7): Sector leaders / laggards ----
    sector_file = None
    if macro_dir.exists():
        s_candidates = list(macro_dir.glob("sector_strength_*.json"))
        if s_candidates:
            sector_file = max(s_candidates, key=lambda p: p.stem)
    if sector_file is not None:
        sec = load_json(sector_file)
        sectors = (sec or {}).get("sectors") or {}
        if isinstance(sectors, dict) and sectors:
            ranked = []
            for etf, v in sectors.items():
                if isinstance(v, dict) and v.get("relative_1m") is not None:
                    ranked.append((
                        etf,
                        v.get("sector_name") or etf,
                        float(v.get("relative_1m") or 0.0),
                    ))
            if ranked:
                ranked.sort(key=lambda x: -x[2])
                top = ranked[0]
                bot = ranked[-1]
                candidates.append({
                    "category": "sector",
                    "icon": "📈",
                    "headline": (
                        f"Sector leader: {top[1]} "
                        f"({top[2]*100:+.1f}% rel 1m) · "
                        f"laggard: {bot[1]} ({bot[2]*100:+.1f}%)"
                    ),
                    "detail": "relative 1m return vs. SPY benchmark",
                    "severity": "low",
                })

    # ---- (8): Band moves vs. yesterday ----
    if history_dir.exists():
        band_changes = []
        try:
            for hp in history_dir.glob("*.json"):
                hd = load_json(hp)
                if not isinstance(hd, dict):
                    continue
                hist = hd.get("history") or []
                if len(hist) < 2:
                    continue
                # Identify newest + previous entry by date (entries may be
                # out of order; sort by date desc).
                by_date = sorted(
                    [h for h in hist if h.get("date")],
                    key=lambda h: h.get("date"),
                    reverse=True,
                )
                if len(by_date) < 2:
                    continue
                latest, prev = by_date[0], by_date[1]
                if latest.get("band") and prev.get("band") and \
                        latest.get("band") != prev.get("band"):
                    band_changes.append({
                        "ticker": hd.get("ticker") or hp.stem,
                        "from_band": prev.get("band"),
                        "to_band": latest.get("band"),
                        "score_delta": (
                            float(latest.get("score") or 0)
                            - float(prev.get("score") or 0)
                        ),
                    })
        except Exception:
            pass
        if len(band_changes) >= 5:
            band_changes.sort(key=lambda c: -abs(c["score_delta"]))
            top3 = band_changes[:3]
            tail = ", ".join(
                f"{c['ticker']} {c['from_band']}→{c['to_band']} "
                f"({c['score_delta']:+.1f})"
                for c in top3
            )
            candidates.append({
                "category": "movers",
                "icon": "📈",
                "headline": f"{len(band_changes)} tickers shifted band overnight",
                "detail": tail,
                "severity": "medium",
            })

    # ---- Prioritize: high > medium > low, with category diversity ----
    candidates.sort(key=lambda i: SEV_RANK.get(i.get("severity"), 9))
    picked: list[dict] = []
    seen_cats: set = set()
    # First pass — one per category, in severity order, until we reach 5.
    for c in candidates:
        if c["category"] not in seen_cats:
            picked.append(c)
            seen_cats.add(c["category"])
            if len(picked) >= 5:
                break
    # Second pass — fill remaining slots with the next-best items even if
    # the category repeats, but only if we have fewer than 3 insights.
    if len(picked) < 3:
        for c in candidates:
            if c not in picked:
                picked.append(c)
                if len(picked) >= 5:
                    break
    return picked[:5]


def _json_for_script(obj) -> str:
    """json.dumps + escape the characters that can break out of an inline
    <script> context: '<', '>', '&', "'". Plain json.dumps preserves
    '</script>' literally, which would break out of the script tag when
    embedded into HTML — a real XSS sink if any string field reaching the
    payload is attacker-controlled (today: the share label set via
    /api/share, broadcast to other viewers via the share link).

    Standard OWASP pattern: encode the dangerous bytes as \\uXXXX escapes.
    JSON parsers decode them back to the original characters at runtime,
    so DATA / SHARE_TOKEN / SIDECARS see the original strings — but the
    embedded JSON literal in the served HTML can't escape <script>.
    """
    return (json.dumps(obj)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("'", "\\u0027"))


def render_html(
    payload: dict,
    share_token: str | None = None,
    sidecars_manifest: dict[str, str] | None = None,
) -> str:
    html = HTML_TEMPLATE.replace("__DATA_JSON__", _json_for_script(payload))
    html = html.replace("__SHARE_TOKEN__", _json_for_script(share_token))
    html = html.replace("__SIDECARS_JSON__", _json_for_script(sidecars_manifest or {}))
    return html


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"  [warn] {path.name}: {e}", file=sys.stderr)
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--fetch", action="store_true", help="fetch live ETF flow data (needs API key)")
    ap.add_argument("--fetch-market", action="store_true", help="fetch live market + whale data (free)")
    args = ap.parse_args()

    if args.fetch:
        try:
            import fetch_live
            fetch_live.fetch_all(DATA_DIR)
        except Exception as e:
            print(f"[fetch] failed: {e}", file=sys.stderr)

    if args.fetch_market:
        try:
            import fetch_market
            fetch_market.fetch_all()
        except Exception as e:
            print(f"[fetch-market] failed: {e}", file=sys.stderr)
        # Coinbase Pulse — per-coin taker buy/sell ratio + ticker insights.
        # Public Exchange API, no auth. Separate fetcher so a Coinbase
        # outage can't poison the main market.json blob. Stale-fallback
        # inside fetch_coinbase.main keeps the prior data/coinbase.json
        # intact if all coins fail.
        try:
            import fetch_coinbase
            fetch_coinbase.main()
        except Exception as e:
            print(f"[fetch-coinbase] failed: {e}", file=sys.stderr)

    # Travel Advisories sidecar — refreshes data-travel.json (V1 root) from
    # travel.state.gov before the HTML render so the tab has fresh data.
    # Isolated in its own try/except: a scrape failure (or 404 from State
    # Dept) must NEVER kill the V1 build. The fetcher itself preserves the
    # prior good JSON on failure. Runs unconditionally (not behind
    # --fetch-market) because the source is a free public scrape and the
    # data only changes when State Dept reissues an advisory — same gating
    # as the V2 build does.
    try:
        import fetch_advisories
        rc = fetch_advisories.main(["--out", str(ROOT / "data-travel.json")])
        if rc != 0:
            print(f"[fetch-advisories] non-zero exit ({rc}); "
                  f"prior data-travel.json preserved.", file=sys.stderr)
    except Exception as e:
        print(f"[fetch-advisories] failed: {e}", file=sys.stderr)

    print("Building payload...")
    payload = build_payload()
    btc_n = len(payload["btc"].get("daily", []))
    eth_n = len(payload["eth"].get("daily", []))
    mkt_n = len(payload["market"].get("btc", {}).get("price", []))
    wh_n = len(payload["whale"].get("btc", {}).get("tx_volume_usd", []))
    print(f"  BTC ETF: {btc_n} rows  ETH ETF: {eth_n} rows  market: {mkt_n}  whale: {wh_n}")
    if btc_n == 0 and eth_n == 0 and mkt_n == 0 and wh_n == 0:
        print("No data found. Add CSVs to data/ and/or run --fetch-market.", file=sys.stderr)
        return 1

    # Split heavy tab-specific subtrees out of the inlined HTML payload and
    # write them as static sidecar files. The client fetches each one only
    # when the user opens the corresponding tab — first paint no longer pays
    # the cost of payloads the user may never view.
    trimmed, sidecars, manifest = split_payload_for_sidecars(payload)
    for name, blob in sidecars.items():
        sidecar_path = ROOT / f"data-{name}.json"
        sidecar_path.write_text(json.dumps(blob))
        print(f"  Wrote {sidecar_path.name} ({sidecar_path.stat().st_size:,} bytes)")

    # External sidecars produced by standalone fetchers (fetch_cpi.py,
    # fetch_supplies.py, fetch_metals.py — each dual-writes to v2/data-X.json
    # AND data-X.json at repo root). Always declare them in the manifest —
    # in CI, V1 builds BEFORE V2 (which is what triggers the dual-write
    # fetchers), so the .exists() check we used to do would always miss and
    # the JS lazy-loader would never try the fetch. By the time the page is
    # served from _site/, V2 has run and the files exist. The JS-side fetch
    # gracefully handles 404 → empty-state, so unconditional declaration is
    # safe and lets V1 pick up the data on the first deploy.
    #
    # Travel Advisories (data-travel.json) is in this same group: the V1
    # build calls fetch_advisories.main() above with --out pointing at the
    # V1 root, so the file is written before render_html. Declared
    # unconditionally for the same reason as cpi/supplies/metals — the JS
    # lazy-loader handles a missing file as an empty-state.
    for ext_key in ("cpi", "supplies", "metals", "travel"):
        manifest[ext_key] = f"data-{ext_key}.json"
    # UAP/MUFON sidecar: only v2/app.py owns the network fetch (writes
    # v2/data-mufon.json). Serve it from the V1 root so production V1 doesn't
    # depend on the /v2/ preview path. Locally we copy the already-built v2
    # file across (best-effort, no network); in CI the workflow stages the
    # freshly-built v2/data-mufon.json to the root after the V2 step. The JS
    # lazy-loader treats a missing file as an empty state.
    _mufon_src = ROOT / "v2" / "data-mufon.json"
    if _mufon_src.exists():
        try:
            shutil.copyfile(_mufon_src, ROOT / "data-mufon.json")
        except OSError as e:
            print(f"  [mufon] could not copy {_mufon_src} -> root: {e}", file=sys.stderr)
    manifest["mufon"] = "data-mufon.json"

    print(f"Writing {OUT.name}...")
    OUT.write_text(render_html(trimmed, sidecars_manifest=manifest))

    print(f"Done. {OUT}")
    if not args.no_open:
        webbrowser.open(OUT.as_uri())
    return 0


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Crypto Trading Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js" integrity="sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g" crossorigin="anonymous"></script>
<style>
:root{
  --bg:#0b0d12; --panel:#141821; --panel2:#1b2030; --border:#252b3a;
  --text:#e6e8ee; --muted:#8a93a6; --btc:#f7931a; --eth:#627eea; --link:#2a5ada; --ltc:#bfbbbb;
  --green:#22c55e; --red:#ef4444; --amber:#f59e0b; --purple:#a78bfa; --cyan:#06b6d4;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
header{padding:14px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;font-weight:600}
header .meta{color:var(--muted);font-size:12px}
.tabs{display:flex;gap:2px;padding:0 24px;border-bottom:1px solid var(--border);background:var(--panel)}
.tab{padding:11px 18px;cursor:pointer;color:var(--muted);font-size:13px;font-weight:500;border-bottom:2px solid transparent;letter-spacing:.02em}
.tab:hover{color:var(--text)}
.tab.active{color:var(--text);border-bottom-color:var(--btc)}
.tab.active.eth{border-bottom-color:var(--eth)}
.tab.active.link{border-bottom-color:var(--link)}
.controls{display:flex;gap:6px;flex-wrap:wrap;padding:14px 24px;border-bottom:1px solid var(--border);background:#0e1118}
.btn{background:var(--panel2);color:var(--text);border:1px solid var(--border);padding:5px 11px;border-radius:6px;cursor:pointer;font-size:12px}
.btn:hover{background:#222838}
.btn:focus-visible,.tab:focus-visible,.chip:focus-visible,a:focus-visible{outline:2px solid #a78bfa;outline-offset:2px}
.btn.active{background:var(--btc);color:#000;border-color:var(--btc)}
.btn.active.eth{background:var(--eth);color:#fff;border-color:var(--eth)}
.btn.active.link{background:var(--link);color:#fff;border-color:var(--link)}
.lbl{font-size:11px;color:var(--muted);align-self:center;margin:0 4px;letter-spacing:.04em;text-transform:uppercase}
.container{padding:18px 24px;display:grid;gap:18px;max-width:1600px;margin:0 auto}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
/* Top-25 signals strip layout. Outer #top20SignalCards is a vertical flex
   column — each populated bucket section gets its OWN full-width row, and
   cards within auto-fit horizontally via the inner grid. The previous
   flex-wrap-row approach made BUY (1 card) and HOLD (24 cards) split the
   viewport 50/50, which left a tiny DOGE card alongside a tall stack of
   HOLD cards — most of the BUY column was empty whitespace. Stacking
   means a single-card bucket only wastes 1 row of horizontal space
   (the empty slots next to that card), not 12 rows of vertical space. */
.signals-section{width:100%;min-width:0}
.signals-section.signals-empty-pill{display:flex;align-items:center;gap:8px;padding:4px 2px;font-size:12px;color:var(--muted);width:100%;margin:0}
.card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:12px}
.card h3{margin:0 0 4px;font-size:10px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:20px;font-weight:600;margin-top:2px}
.card .sub{font-size:11px;color:var(--muted);margin-top:3px}
.green{color:var(--green)} .red{color:var(--red)} .amber{color:var(--amber)}
.chart-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
.stock-card,.poc-card{transition:border-color .12s, transform .08s}
.stock-card:hover,.poc-card:hover{border-color:#a78bfa}
.stock-card:active,.poc-card:active{transform:scale(0.99)}
.stock-card:focus-visible,.poc-card:focus-visible{outline:2px solid #a78bfa;outline-offset:2px}
.chart-card .head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;gap:8px;flex-wrap:wrap}
.chart-card h2{font-size:13px;margin:0;font-weight:600}
.chart-card .desc{font-size:11px;color:var(--muted)}
/* UAP / MUFON tab shim — V2's mufon markup uses .v2-card* / .v2-chip* /
   .btn--small classes that don't exist in V1. Map them onto V1 tokens so the
   ported tab renders without restyling the copied HTML. */
.v2-card{background:var(--panel);border:1px solid var(--border);border-radius:10px}
.v2-card__head{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;padding:14px 14px 0}
.v2-card__title{font-size:13px;margin:0;font-weight:600}
.v2-card__subtitle{font-size:11px;color:var(--muted);margin-top:3px}
.v2-card__body{font-size:13px}
.v2-chip{display:inline-block;padding:3px 8px;border-radius:999px;font-size:11px;border:1px solid var(--border);color:var(--muted)}
.v2-chip--info{color:var(--btc);border-color:var(--btc)}
.btn--small{padding:4px 9px;font-size:11px}
/* Top-15 news-sentiment grid (Research tab). Three columns on desktop,
   two on mid-width tablets, one full-width column on phones (≤480px). */
.top-news-sentiment-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.top-news-sentiment-row{display:grid;grid-template-columns:64px 1fr 130px;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--panel);min-width:0}
.top-news-sentiment-row .tns-sym{font-weight:700;font-size:12px;letter-spacing:.02em;color:var(--text)}
.top-news-sentiment-row .tns-name{font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.top-news-sentiment-row .tns-stats{font-size:10px;color:var(--muted);margin-top:2px;display:flex;gap:8px;flex-wrap:wrap}
.top-news-sentiment-row .tns-bar{display:flex;height:8px;border-radius:3px;overflow:hidden;background:#1f2533}
.top-news-sentiment-row .tns-net{font-weight:600;font-size:11px;text-align:right}
/* INTENTIONAL non-standard breakpoint (kept after the 480/860 consolidation).
   This grid's 3-col → 2-col → 1-col staircase needs the middle step to fall
   ABOVE the dashboard's main 860px mobile boundary: each .top-news-sentiment-row
   carries 64+text+130px columns plus gaps, so 3-up gets cramped at ~900px and
   below. Folding into 860 would make 860-900px render the 3-col grid in ~280px
   cells (sym name + bar + net all collide). Leave at 900. */
@media (max-width:900px){
  .top-news-sentiment-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:480px){
  .top-news-sentiment-grid{grid-template-columns:1fr}
  .top-news-sentiment-row{grid-template-columns:52px 1fr 96px;gap:8px;padding:6px 8px}
  /* (Consolidated from a separate later block:) all modals — outer .modal-bg
     padding:24px eats 48px on a 375px viewport; combined with each modal's
     inner padding the content area is only ~291px wide. Tight for POC-detail
     tables and share-link URL row. */
  .modal-bg{padding:8px !important}
}
.chart-wrap{position:relative;height:300px}
.chart-wrap.tall{height:380px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:18px}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}
/* Symbol detail modal body: pair Signal + POC cards side-by-side. Uses a
   tighter min-column than .grid2 because the modal width caps at ~940px so
   the 420px default would force a single column most of the time. Stacks
   1-col on mobile via the existing .grid2 @media 860 override AND an
   explicit ≤480 rule below. */
.symbol-modal-body{grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px}
@media (max-width:480px){
  /* Explicit mobile stack for the symbol detail modal body — the 860px
     .grid2 override already collapses to 1-col much earlier, but spell it
     out at ≤480 too so future refactors of .grid2 don't accidentally
     re-introduce a side-by-side layout on a phone-width screen. */
  .symbol-modal-body{grid-template-columns:1fr !important;gap:10px}
}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
/* Markets table: rank in col 1, Coin (icon + symbol) in col 2 — left-align
   col 2 so all the icons start at the same x. Default right-align would
   make the icon position drift with text width row-by-row. */
#marketsTable th:nth-child(2),#marketsTable td:nth-child(2){text-align:left}
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
/* Tracker variant: full grid lines (both axes) + tighter rows for the
   Whale Activity Tracker. Vertical separators help the eye line up
   1d / 7d / 30d / 90d delta columns across rows. */
.tracker-grid{border:1px solid var(--border);border-radius:6px;overflow:hidden}
.tracker-grid th,.tracker-grid td{padding:4px 10px;border:1px solid var(--border)}
.tracker-grid thead th{background:#0e1118}
.tracker-grid tbody tr:nth-child(odd){background:rgba(255,255,255,.015)}
.empty{padding:48px 16px;text-align:center;color:var(--muted)}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:10px;letter-spacing:.04em;text-transform:uppercase;border:1px solid var(--border);color:var(--muted);margin-left:6px}
.tag.btc{color:var(--btc);border-color:var(--btc)}
.tag.eth{color:var(--eth);border-color:var(--eth)}
.tag.link{color:var(--link);border-color:var(--link)}
.tag.ltc{color:var(--ltc);border-color:var(--ltc)}
footer{padding:18px 24px;color:var(--muted);font-size:12px;text-align:center;border-top:1px solid var(--border);margin-top:24px}
/* Chat dock */
#chatDock{position:fixed;top:0;right:0;height:100vh;width:380px;background:var(--panel);border-left:1px solid var(--border);display:flex;flex-direction:column;transform:translateX(100%);transition:transform .25s ease;z-index:40;box-shadow:-4px 0 24px rgba(0,0,0,.35)}
#chatDock.open{transform:translateX(0)}
.chat-head{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.chat-head h2{margin:0;font-size:14px}
.chat-head .sub{font-size:11px;color:var(--muted);margin-top:2px}
.chat-msgs{flex:1;overflow-y:auto;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.msg{padding:8px 12px;border-radius:10px;font-size:13px;line-height:1.4;max-width:90%;white-space:pre-wrap;word-wrap:break-word}
.msg.user{background:#1f2533;align-self:flex-end;border:1px solid var(--border)}
.msg.bot{background:#10151f;align-self:flex-start;border:1px solid var(--border);border-left:3px solid #a78bfa}
.msg.err{background:#3b1414;align-self:flex-start;border:1px solid #6b1f1f;color:#fca5a5}
.chat-suggestions{padding:0 14px 8px;display:flex;flex-wrap:wrap;gap:6px}
.chat-suggestions .chip{font-size:10px;background:var(--panel2);border:1px solid var(--border);color:var(--muted);padding:3px 8px;border-radius:999px;cursor:pointer}
.chat-suggestions .chip:hover{background:#222838;color:var(--text)}
.chat-form{padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:6px}
.chat-form input{flex:1;background:#0b0d12;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-size:13px;outline:none}
.chat-form input:focus{border-color:#a78bfa}
.chat-form button{background:#a78bfa;color:#000;border:0;padding:8px 14px;border-radius:6px;cursor:pointer;font-weight:600;font-size:12px}
.chat-form button:disabled{opacity:.4;cursor:not-allowed}
#chatFab{position:fixed;bottom:24px;right:24px;width:52px;height:52px;border-radius:50%;background:#a78bfa;color:#000;border:0;cursor:pointer;font-size:24px;box-shadow:0 4px 14px rgba(167,139,250,.4);z-index:39;transition:transform .15s}
#chatFab:hover{transform:scale(1.08)}
#chatFab.hidden{display:none}
/* Recent symbol-lookup chips. Rendered below the header symbol-search form
   by renderSymbolRecentChips(); hidden via .hidden when the localStorage
   list is empty. The chip's × (.symbol-recent-chip-x) removes a single
   entry; clicking the chip itself fills the input and submits the form. */
.symbol-recent-chip{
  display:inline-flex;align-items:center;gap:4px;
  padding:3px 8px;border:1px solid var(--border);border-radius:12px;
  background:var(--panel);color:var(--text);font-size:11px;
  cursor:pointer;white-space:nowrap;line-height:1.2;font-family:inherit;
}
.symbol-recent-chip:hover{background:#10151f}
.symbol-recent-chip-x{
  display:inline-flex;align-items:center;justify-content:center;
  width:12px;height:12px;border-radius:50%;
  color:var(--muted);font-size:12px;line-height:1;
  margin-left:1px;
}
.symbol-recent-chip-x:hover{color:var(--text);background:#1f2533}
/* Mobile: tight layout — collapse multi-col grids, shrink header, KPI rows
   become 2-up instead of 1-up, chart heights capped. Desktop unchanged. */
@media (max-width:860px){
  /* Chat dock: full-width on mobile (was a separate 720px block; folded into
     860 since the dashboard itself collapses to mobile layout at 860 — a 380px
     floating sidebar on a 720-860px window was inconsistent with the rest of
     the mobile-mode UI). */
  #chatDock{width:100%}
  #overviewMacroRow{grid-template-columns:1fr !important}
  .grid2{grid-template-columns:1fr !important}
  .grid3{grid-template-columns:1fr !important}
  /* Top-25 header title block: on desktop it sits to the right of the filter
     chips with text-align:right. On mobile the chips wrap to their own line
     so the title is the only thing on its row — right-aligning it pushes the
     copy off the right edge. Reset to left-align under mobile width. */
  .top25-header-title{text-align:left !important;width:100%}
  /* Asset signal cards: keep 2 per row on mobile instead of one big card,
     and shrink fonts so price/change/volume don't dominate the screen. */
  /* Asset cards (BTC/ETH/LINK/LTC) — ultra-compact on mobile so the user
     sees Strong Buys + news above the fold. Was ~110px tall each → ~55px.
     Hides redundant fields (full coin name, "as of" date) that already
     live in the header / tooltips. */
  /* Top-25 signal strip on mobile. Outer #top20SignalCards is flex-wrap
     (see desktop rules) — on a phone the section's flex-basis:280px
     naturally wraps each section to its own row, so no outer override
     needed. Just force the INNER card grid to 2-up so users see 2 cards
     side-by-side per section instead of 1 narrow column. */
  #top20SignalCards .signals-section{flex:1 1 100%}
  #top20SignalCards .signals-section-grid{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:6px !important}
  #overviewSignals{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:6px}
  #overviewSignals .card{padding:6px 10px;border-left-width:3px}
  #overviewSignals .card h3{font-size:11px !important;margin:0 !important}
  /* Hide the full name "Bitcoin/Ethereum" next to the symbol */
  #overviewSignals .card > div:first-child > span.sub{display:none}
  /* Hide the "as of YYYY-MM-DD" date — same info is in the header timestamp */
  #overviewSignals .card > .sub:last-child{display:none}
  /* Price + % change row sizes */
  #overviewSignals .card .v{font-size:15px !important;margin-top:2px !important}
  #overviewSignals .card > div:nth-child(3){margin-top:2px !important}
  #overviewSignals .card > div:nth-child(3) span{font-size:10px !important}
  /* Strong Buys: tighter on mobile too. (#top20SignalCards is overridden
     above with full-width sections + 2-up inner grid — don't reset here.) */
  #overviewStrongBuys{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:6px}
  /* UX-F2: Top-25 by market cap card grid was inline minmax(180px,1fr) which
     collapses to 1-up at 375px (~1.8k px scroll for 25 cards). Match the
     Strong Buys sibling: 2-up on mobile. */
  #overviewTop15{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:6px}
  /* UX-F3: Stocks tab — the prior mobile rule targeted #stocksGrid (the outer
     wrapper holding 5 bucket sections) which is already 1fr. The actual cards
     live in inner .stocks-section-grid divs with inline minmax(280px,1fr).
     Without this override they stack 1-up (~6k px scroll for ~50 stocks). */
  #stocksGrid .stocks-section-grid{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:8px !important}
  /* UX-F5: AI-exposed stocks grid was inline minmax(220px,1fr) → 1-up on
     mobile (15 cards stacked). Match the AI KPI strip: 2-up. */
  #aiStocksGrid{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:6px}
  /* UX-F4: Whale Sentiment Index tables (BTC + ETH) have 4 columns whose
     Read column carries 6-10-word explanations — min-width > 375px forces the
     whole Whale tab to scroll horizontally on mobile. Convert table to a
     block element with its own horizontal scroll so the tab doesn't bleed. */
  #whaleSentimentCard table,
  #whaleEthSentimentCard table{display:block;overflow-x:auto;white-space:nowrap;max-width:100%}
  /* UX-F1: Header search input's inline width:130px + the four control buttons
     consume ~351px on a 375px viewport, collapsing the dashboard title to "…".
     Shrink the search input on mobile and shrink the controls' font. */
  header #symbolSearchInput{width:84px !important;font-size:11px;min-height:44px;padding:8px 10px}
  /* UX-F9: Futures explainer's inner .card carries inline padding:14px 16px
     which beats the non-!important mobile .card{padding:8px 10px}. The
     disclosure body re-flows with too much padding on phones; tighten it. */
  .futures-explainer .card{padding:8px 10px !important}
  /* POC volume profile fullscreen button is desktop-only — user reported
     the mobile-sized chart in the modal is already legible. Hide the
     control to keep the modal header clean on phones. */
  .poc-vol-fullscreen-btn{display:none !important}

  /* --- Compact mobile header (was ~200px tall, now ~104px) --- */
  header{padding:8px 12px;gap:6px;flex-wrap:nowrap;align-items:center}
  header > div:first-child{min-width:0;flex:1 1 auto}
  header h1{font-size:15px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  header .meta{display:none}
  /* Header button row: asset toggles + share/refresh, single line, no wrap */
  header .controls{flex-wrap:nowrap;gap:4px;flex:0 0 auto}
  header .controls .btn{padding:5px 8px;font-size:11px;min-height:44px}
  header .controls > span{width:6px !important}

  /* --- Tab bar: horizontal scroll strip (was wrapping to 2 lines + cut) --- */
  .tabs{
    padding:0 8px;
    gap:0;
    overflow-x:auto;
    overflow-y:hidden;
    flex-wrap:nowrap;
    white-space:nowrap;
    -webkit-overflow-scrolling:touch;
    scrollbar-width:none;
    /* Fade-right affordance so users see there's more content to scroll —
       without this, "Research" / "Whale Activity" looked like they didn't
       exist because they sit off-screen past 390px. */
    -webkit-mask-image:linear-gradient(to right,#000 calc(100% - 28px),transparent);
            mask-image:linear-gradient(to right,#000 calc(100% - 28px),transparent);
  }
  .tabs::-webkit-scrollbar{display:none}
  .tab{padding:9px 12px;font-size:13px;flex:0 0 auto;min-height:44px;display:inline-flex;align-items:center}

  /* --- Period/timeframe controls row below tabs (smaller buttons) --- */
  .controls{padding:8px 12px;gap:5px}
  .controls .btn{padding:5px 9px;font-size:11px;min-height:44px;display:inline-flex;align-items:center}

  /* --- KPI rows go 2-up on mobile so they don't eat the screen.
         #defiKpis, #whaleKpis, #tradingKpis, #etfKpis, #fundKpiGrid all use
         the .row class with minmax(180-220px,1fr) which forces 1 col on
         phones. Force 2 cols. --- */
  #defiKpis,
  #whaleKpis,
  #tradingKpis,
  #etfKpis,
  #fundKpiGrid,
  #pocTopGrid,
  #stocksGrid{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:8px}
  /* Featured POC cards (top-4 by signal score) carry 18px symbol, 64px
     arrow rail, 14/16px padding and a larger 64px-tall sparkline. At 2-up
     on a 375px phone each cell is ~155px wide — the rail alone eats it
     and the left column collapses to ~25px (symbol + badge + price all
     wrap to 3+ ragged rows). Force 1-up so featured cards keep their
     designed ~280px layout on phone. The 4-card section becomes a vertical
     stack, then the remaining cards fill the 2-up #pocTopGrid below. */
  #pocFeaturedRow{grid-template-columns:1fr !important;gap:8px}
  /* POC compact card sub-text bumped 10→11px for readability on phone */
  #pocTopGrid .poc-card .sub{font-size:11px !important}
  /* Ensure clickable card divs hit 44px touch target */
  .poc-card,.stock-card{min-height:44px}
  /* --- POC compact card mobile layout fix (#pocTopGrid 2-up at 375px ≈
         159px per cell). Each card is renderPocTopCards's compact branch
         with inline styles, so the mobile overrides have to be specific
         enough (and !important) to beat inline. Bugs at narrow widths:
         (a) header row "icon + symbol + score-chip + price" had 4 items
             in a flex-wrap:wrap → wrapped to 3 ragged rows, card heights
             went jagged. Tighten chip padding + shrink font so it fits.
         (b) "90d POC" row had 4 children (label/value/Δ%/VA-tag) with
             space-between and no wrap → squished or overflowed. Hide the
             redundant IN-VA/OUT pill on phone — the Δ% already carries
             color (green if above POC = supportive, red if below).
         (c) 44px arrow rail dominated the 159px card. Shrink to 36px so
             the data column has breathing room.
         (d) Inline padding 8px 10px is OK but tighten further on phone.  */
  #pocTopGrid .poc-card{padding:6px 8px !important}
  #pocTopGrid .poc-card span[title="Signal score"]{font-size:9px !important;padding:1px 4px !important;letter-spacing:-.01em}
  /* Arrow rail (the only direct flex child with flex-basis:44px). Match
     both featured (64px) and compact (44px) variants → universal 36px on
     phone, with smaller arrow + label fonts so it stays legible. */
  #pocTopGrid .poc-card > div > div[style*="border-left:1px solid"]{flex:0 0 32px !important;padding:1px 0 !important}
  #pocTopGrid .poc-card > div > div[style*="border-left:1px solid"] > div:first-child{font-size:20px !important}
  /* Hide the IN-VA / OUT pill on the compact card's 90d POC row (4-child
     space-between flex → overflows at 159px). The Δ% color (green/red)
     already conveys above/below-POC. Pill stays on featured cards. */
  #pocTopGrid .poc-card span[style*="font-size:9px"][style*="font-weight:600"]{display:none}
  /* Featured cards keep their 1-up layout (above) but tighten padding so
     they don't run tall vertically. */
  #pocFeaturedRow .poc-card[data-poc-featured]{padding:10px 12px !important}
  #defiKpis .card,
  #whaleKpis .card,
  #tradingKpis .card,
  #etfKpis .card,
  #fundKpiGrid .card{padding:10px 12px}
  #defiKpis .card .v,
  #whaleKpis .card .v,
  #tradingKpis .card .v,
  #etfKpis .card .v,
  #fundKpiGrid .card .v{font-size:17px !important}
  #defiKpis .card .sub,
  #whaleKpis .card .sub,
  #tradingKpis .card .sub,
  #etfKpis .card .sub,
  #fundKpiGrid .card .sub{font-size:10px !important}

  /* --- Cap chart heights on mobile (was 380px each, way too tall) --- */
  .chart-wrap.tall{height:280px}
  .chart-wrap{min-height:0}
  /* Breadth charts: tighter on phone */
  #stocksBreadthChart, #cryptoSignalsBreadthChart{}
  .chart-wrap:has(>#stocksBreadthChart),
  .chart-wrap:has(>#cryptoSignalsBreadthChart){height:160px !important}
  /* AI funding quadrant chart had inline height:380px which overrides
     .chart-wrap above; force it down on phones so it doesn't tower over
     the AI News tab. !important needed to beat the inline style.
     Drop further to 240px on the smallest widths — the bottom legend +
     two axis-title rows ("Last round size (USD, log)" / "Valuation
     (USD, log)") were eating ~70px of the 280px box, leaving the actual
     scatter area cramped. !important needed to beat the inline style. */
  .chart-wrap:has(>#aiQuadrantChart){height:240px !important;min-height:0 !important}
  /* Tighten the quadrant card padding so the chart gets a few extra px
     of horizontal room — log-axis tick labels ("$1B","$10B","$100B")
     were running right up to the card edge on 360px screens. */
  #aiQuadrantCard{padding:8px 10px !important}

  /* --- AI investment KPIs / Research benchmarks (#aiInvestmentKpisCard,
     #aiWhitepaperKpisCard) ---------------------------------------------
     Each KPI is rendered as an INNER <div class="chart-card"> with inline
     padding:12px 14px, an inline 24px value, an 11px label, source text,
     plus a delta pill — and the outer grid uses inline
     repeat(auto-fit,minmax(220px,1fr)) which collapses to 1-up on phones,
     so each card was ~135px tall × full-width. User feedback: "boxes
     pretty large on mobile" / "boxes really big on mobile". Force a
     tight 2-column grid, shrink the inner card padding + value + delta
     fonts, and hide the redundant prior_value/source fields on phone
     (clicking the card already opens the source URL in a new tab). */
  #aiInvestmentKpis,
  #aiWhitepaperKpis{
    grid-template-columns:repeat(2,minmax(0,1fr)) !important;
    gap:6px !important;
  }
  /* Inner KPI cell: shrink padding + radius. Inline padding on the inner
     <div> wrapper still applies, so override that too via descendant. */
  #aiInvestmentKpis > .chart-card,
  #aiInvestmentKpis > a.chart-card,
  #aiWhitepaperKpis > .chart-card,
  #aiWhitepaperKpis > a.chart-card{
    padding:0 !important;
    border-radius:6px;
  }
  #aiInvestmentKpis > .chart-card > div,
  #aiInvestmentKpis > a.chart-card > div,
  #aiWhitepaperKpis > .chart-card > div,
  #aiWhitepaperKpis > a.chart-card > div{
    padding:8px 10px !important;
    gap:4px !important;
  }
  /* Label row (uppercase, 11px). Drop to 10px and clamp to 2 lines so
     long labels don't cause uneven card heights. */
  #aiInvestmentKpis > .chart-card > div > div:first-child,
  #aiInvestmentKpis > a.chart-card > div > div:first-child,
  #aiWhitepaperKpis > .chart-card > div > div:first-child,
  #aiWhitepaperKpis > a.chart-card > div > div:first-child{
    font-size:9px !important;
    letter-spacing:.03em !important;
    line-height:1.25 !important;
  }
  /* Big value (was 24px, way too dominant in a 160px-wide cell on 360px
     phones). Drop to 17px. The inline "unit" span inside scales with em
     so it's covered too. */
  #aiInvestmentKpis > .chart-card > div > div:nth-child(2),
  #aiInvestmentKpis > a.chart-card > div > div:nth-child(2),
  #aiWhitepaperKpis > .chart-card > div > div:nth-child(2),
  #aiWhitepaperKpis > a.chart-card > div > div:nth-child(2){
    font-size:17px !important;
    line-height:1.1 !important;
  }
  #aiInvestmentKpis > .chart-card > div > div:nth-child(2) span,
  #aiInvestmentKpis > a.chart-card > div > div:nth-child(2) span,
  #aiWhitepaperKpis > .chart-card > div > div:nth-child(2) span,
  #aiWhitepaperKpis > a.chart-card > div > div:nth-child(2) span{
    font-size:10px !important;
  }
  /* Delta pill row: shrink the pill so it doesn't push the prior label
     to its own row in a 160px cell. */
  #aiInvestmentKpis > .chart-card > div > div:nth-child(3) > span,
  #aiInvestmentKpis > a.chart-card > div > div:nth-child(3) > span,
  #aiWhitepaperKpis > .chart-card > div > div:nth-child(3) > span,
  #aiWhitepaperKpis > a.chart-card > div > div:nth-child(3) > span{
    padding:1px 6px !important;
    font-size:10px !important;
  }
  /* Hide the source attribution line on phone — same info is on the
     desktop view, and tapping the card opens the source URL anyway.
     Saves ~16px per cell × N cells. */
  #aiInvestmentKpis > .chart-card > div > div.sub,
  #aiInvestmentKpis > a.chart-card > div > div.sub,
  #aiWhitepaperKpis > .chart-card > div > div.sub,
  #aiWhitepaperKpis > a.chart-card > div > div.sub{
    display:none !important;
  }

  /* --- GLOBAL CARD TIGHTENING (every tab, not just Overview) ---
     User reported all phone pages had boxes wasting too much space.
     Shrinks padding, fonts, and gaps across .card / .chart-card / .grid*
     so every section becomes ~40-50% shorter without losing data. */
  .container{padding:10px 12px;gap:10px}
  /* tab-ainews / tab-stocks / tab-poc each open a *nested* .container
     inside the outer page .container, which double-pads on mobile (24px
     side-pad vs 12px on every other tab → "boxes are out of sorts").
     Zero out padding/gap on inner containers so all 10 tabs align. */
  .container .container{padding:0;gap:10px}
  .card{padding:8px 10px;border-radius:6px}
  .chart-card{padding:10px 12px;border-radius:6px}
  .chart-card .head{flex-wrap:wrap;gap:4px;margin-bottom:4px}
  .chart-card .head h2{font-size:13px !important;line-height:1.2}
  .chart-card .head .desc,
  .chart-card .head span.desc{font-size:10px !important;line-height:1.3}
  /* Larger card titles (h3) used in non-chart-card cards */
  .card h3{font-size:12px !important;line-height:1.2;margin:0 0 4px 0}
  /* Common ".v" big-value text — applies to many KPI/asset cards */
  .card .v{font-size:16px !important}
  .card .sub{font-size:10px !important;line-height:1.3}
  /* Tables inside cards: tighter, scrollable horizontally if needed */
  .chart-card table,
  .card table{font-size:11px}
  .chart-card table th,
  .chart-card table td{padding:3px 4px}
  /* Grid gaps shrunk so 2-up cards sit closer */
  .grid2,.grid3{gap:8px !important}
  /* Period-button row on each tab — already tightened above; keep tight */
  .note{font-size:10px;padding:6px 10px;line-height:1.35}
  /* Mobile a11y: any clickable .btn or chat send button hits 44px regardless
     of inline padding/font overrides. Inline-flex+center keeps visual size.
     Covers: #insightsToggle, #configSignalsBtn, #chatClose, [data-pocwin],
     [data-fundwin], [data-cohortbin], [data-copy], [data-revoke], plus the
     chat form's submit button (#chatSend) which has no .btn class. */
  .chat-form button,
  button.btn{min-height:44px;display:inline-flex;align-items:center;justify-content:center}

  /* --- Stocks overview indices bar (DOW/S&P/NDX/VIX) ---
     Desktop uses auto-fit minmax(240px,1fr) which forces 1-up on a 375px
     phone — each cell ran ~80px tall × full-width with 20px price + 110×32
     sparkline. Force 2-up grid and shrink the per-cell price/sparkline so
     the bar drops to ~50% of its prior footprint. Renderer
     (renderOverviewIndices) emits per-index <div> with inner
     flex-direction:column block carrying [label span, big price span,
     pct span] then an SVG sparkline. Override the inline styles via
     !important. */
  #overviewIndicesWrap{padding:8px 10px !important}
  #overviewIndices{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:6px !important}
  #overviewIndices > div{padding:6px 8px !important;gap:6px !important}
  /* Big price number — inner block's 2nd span (after label, before pct) */
  #overviewIndices > div > div > span:nth-child(2){font-size:14px !important}
  /* Shrink the sparkline SVG */
  #overviewIndices svg{width:60px !important;height:20px !important}
}
.hidden{display:none !important}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50;display:flex;align-items:center;justify-content:center;padding:24px}
.note{font-size:11px;color:var(--muted);background:#10151f;border:1px solid var(--border);padding:8px 12px;border-radius:8px}
/* Collapsible explainer (Futures tab). Closed-by-default native <details>
   so the long perpetuals paragraph doesn't dominate above-the-fold on
   desktop or mobile. Suppress the default disclosure triangle in favor
   of a colored caret rotated via the [open] attribute selector. */
.futures-explainer{margin-bottom:10px}
.futures-explainer summary{cursor:pointer;font-size:12px;color:var(--muted);padding:4px 0;list-style:none;user-select:none}
.futures-explainer summary::-webkit-details-marker{display:none}
.futures-explainer summary::before{content:"\25B8 ";color:var(--purple)}
.futures-explainer[open] summary::before{content:"\25BE "}
/* Symbol search typeahead dropdown — floats under the header search input
   and shows up to 8 matching symbols pulled from DATA.market.markets_top,
   DATA.market.stocks_signals, and DATA.signals_top20. Anchored to the
   form's right edge so it doesn't overflow on the tight mobile (84px)
   input. Has min-width:200px so rows stay readable even when the input
   itself is narrower than the dropdown. */
.symbol-suggest{position:absolute;top:calc(100% + 4px);right:0;min-width:200px;max-width:320px;max-height:280px;overflow-y:auto;background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:4px;z-index:60;box-shadow:0 6px 24px rgba(0,0,0,.45)}
.symbol-suggest-row{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:6px 10px;border-radius:4px;cursor:pointer;font-size:12px}
.symbol-suggest-row:hover,.symbol-suggest-row.active{background:#10151f}
.symbol-suggest-sym{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:600;color:var(--text)}
.symbol-suggest-name{color:var(--muted);font-size:11px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:170px}
.symbol-suggest-empty{padding:8px 10px;color:var(--muted);font-size:11px;font-style:italic}
/* CPI / Supplies / Metals tabs — additive, mirrors V2's compact grid layout
   but uses V1 tokens (var(--panel), var(--border), var(--muted), .chart-card
   etc.) for visual continuity with the rest of the V1 dashboard. */
.cpi-cat{margin-bottom:14px}
.cpi-cat__head{display:flex;align-items:baseline;justify-content:space-between;margin:8px 2px 6px;gap:8px}
.cpi-cat__title{margin:0;font-size:12px;font-weight:700;color:var(--text);text-transform:uppercase;letter-spacing:.06em}
.cpi-cat__count{font-size:11px;color:var(--muted)}
.cpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px}
.cpi-mini{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 12px;display:flex;flex-direction:column;gap:6px;min-width:0}
.cpi-mini__head{display:flex;align-items:flex-start;justify-content:space-between;gap:8px}
.cpi-mini__label{font-size:12px;font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.cpi-mini__unit{font-size:10px;color:var(--muted);margin-top:1px}
.cpi-mini__val{font-size:15px;font-weight:700;color:var(--text);line-height:1}
.cpi-mini__chip{display:inline-block;padding:1px 7px;border-radius:999px;font-size:10px;font-weight:600;border:1px solid var(--border);color:var(--muted);white-space:nowrap}
.cpi-mini__chip.bad{color:#ef4444;border-color:#ef4444}
.cpi-mini__chip.good{color:#22c55e;border-color:#22c55e}
.cpi-mini__err{font-size:11px;color:#f59e0b;padding:6px 0}
/* Metals strength band */
.metals-strength{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
@media (max-width:820px){.metals-strength{grid-template-columns:1fr}}
.metals-col__lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
.metals-col__note{font-size:11px;color:var(--muted);line-height:1.4;margin-top:4px}
.metals-row{display:flex;align-items:center;gap:6px;min-width:0;margin-bottom:6px}
.metals-row__name{width:48px;font-size:11px;font-weight:600;color:var(--text)}
.metals-row__cells{display:flex;flex:1;gap:4px;min-width:0}
.metals-cell{text-align:center;flex:1;min-width:0}
.metals-cell__lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.metals-pct{display:inline-block;padding:1px 6px;border-radius:999px;font-size:10px;font-weight:600;border:1px solid var(--border);color:var(--muted);margin-top:2px}
.metals-pct.good{color:#22c55e;border-color:#22c55e}
.metals-pct.bad{color:#ef4444;border-color:#ef4444}
.metals-grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}
.metals-card{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px}
.metals-card h3{margin:0 0 4px;font-size:12px;font-weight:700;color:var(--text)}
.metals-card .sub{font-size:11px;color:var(--muted)}
/* Supplies grid */
.supplies-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:14px}
.supplies-snapshot{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:12px}
/* Travel Advisories tab — ported from V2 but mapped onto V1 tokens
   (var(--panel), var(--border), var(--muted), .card, etc.) so it lives
   alongside CPI / Supplies / Metals visually. The State Dept's 4-level
   scheme maps onto V1's --green/--amber/--red plus an orange shade for L3
   (matching the State Dept palette) and --purple for the Terrorism
   cross-cut. */
.travel-orange{--travel-orange:#fb923c}
.travel-subtabs{display:flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid var(--border);margin-bottom:14px}
.travel-subtab{background:none;border:none;border-bottom:2px solid transparent;padding:8px 14px;cursor:pointer;color:var(--muted);font:600 12px/1 inherit;letter-spacing:.04em;display:inline-flex;align-items:center;gap:6px;margin-bottom:-1px;font-family:inherit}
.travel-subtab:hover{color:var(--text)}
.travel-subtab.active{color:var(--text);border-bottom-color:var(--purple)}
.travel-dot{width:8px;height:8px;border-radius:99px;display:inline-block}
.travel-dot--l1{background:var(--green)}
.travel-dot--l2{background:var(--amber)}
.travel-dot--l3{background:#fb923c}
.travel-dot--l4{background:var(--red)}
.travel-dot--terror{background:var(--purple)}
.travel-stat{background:var(--panel);border:1px solid var(--border);border-top:3px solid var(--border);border-radius:10px;padding:14px 16px;cursor:pointer;text-align:left;font:inherit;color:var(--text);transition:transform .12s,border-color .12s;display:flex;flex-direction:column;gap:4px;font-family:inherit}
.travel-stat:hover{transform:translateY(-2px);border-color:var(--purple)}
.travel-stat:focus-visible{outline:2px solid var(--purple);outline-offset:2px}
.travel-stat--l1{border-top-color:var(--green)}
.travel-stat--l2{border-top-color:var(--amber)}
.travel-stat--l3{border-top-color:#fb923c}
.travel-stat--l4{border-top-color:var(--red)}
.travel-stat--terror{border-top-color:var(--purple)}
.travel-stat__num{font-size:28px;font-weight:700;line-height:1}
.travel-stat--l1 .travel-stat__num{color:var(--green)}
.travel-stat--l2 .travel-stat__num{color:var(--amber)}
.travel-stat--l3 .travel-stat__num{color:#fb923c}
.travel-stat--l4 .travel-stat__num{color:var(--red)}
.travel-stat--terror .travel-stat__num{color:var(--purple)}
.travel-stat__label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-top:4px}
.travel-stat__sub{font-size:11px;color:var(--muted)}
.travel-bullet{display:flex;gap:10px;align-items:flex-start;background:#0e1118;border:1px solid var(--border);border-radius:8px;padding:10px 12px;text-decoration:none;color:var(--text);transition:border-color .12s,transform .12s}
.travel-bullet:hover{border-color:var(--purple);transform:translateX(2px)}
.travel-bullet__stripe{width:3px;align-self:stretch;border-radius:3px;flex-shrink:0}
.travel-bullet__stripe--red{background:var(--red)}
.travel-bullet__stripe--amber{background:var(--amber)}
.travel-bullet__stripe--green{background:var(--green)}
.travel-bullet__body{flex:1;min-width:0}
.travel-bullet__top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.travel-bullet__date{font-size:11px;color:var(--muted)}
.travel-bullet__title{font-size:13px;font-weight:600;margin-top:4px;line-height:1.3}
.travel-bullet__excerpt{font-size:12px;color:var(--muted);margin-top:3px;line-height:1.45}
.travel-bullet__arrow{color:var(--muted);font-size:13px;flex-shrink:0}
.travel-contact-bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:12px;padding-top:10px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)}
.travel-contact-label{color:var(--text);text-transform:uppercase;letter-spacing:.06em;font-size:10px;font-weight:600}
.travel-contact-sep{color:var(--border)}
.travel-contact-link{color:var(--purple);text-decoration:none;font-weight:600}
.travel-contact-link:hover{text-decoration:underline}
.travel-bar-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.travel-bar-label{width:64px;font-size:11px;color:var(--muted);display:flex;align-items:center;gap:6px}
.travel-bar-track{flex:1;height:18px;background:#0e1118;border-radius:5px;overflow:hidden}
.travel-bar-fill{height:100%;border-radius:5px;transition:width .4s ease}
.travel-bar-fill--l1{background:var(--green)}
.travel-bar-fill--l2{background:var(--amber)}
.travel-bar-fill--l3{background:#fb923c}
.travel-bar-fill--l4{background:var(--red)}
.travel-bar-num{width:32px;text-align:right;font-size:12px;color:var(--text)}
.travel-recent{list-style:none;margin:0;padding:0;display:flex;flex-direction:column}
.travel-recent li{display:flex;gap:10px;padding:9px 0;border-top:1px solid var(--border)}
.travel-recent li:first-child{border-top:none;padding-top:0}
.travel-recent__dot{width:8px;height:8px;border-radius:99px;margin-top:6px;flex-shrink:0}
.travel-recent__dot--l1{background:var(--green)}
.travel-recent__dot--l2{background:var(--amber)}
.travel-recent__dot--l3{background:#fb923c}
.travel-recent__dot--l4{background:var(--red)}
.travel-recent__name{color:var(--text);text-decoration:none;font-size:13px;font-weight:600}
.travel-recent__name:hover{text-decoration:underline}
.travel-recent__meta{font-size:11px;color:var(--muted);margin-top:2px}
.travel-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.travel-search{flex:1 1 220px;background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:13px;font-family:inherit;outline:none}
.travel-search:focus{border-color:var(--purple)}
.travel-toggle{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--muted);font-size:12px;cursor:pointer;font-family:inherit}
.travel-toggle:hover{border-color:var(--purple)}
.travel-toggle.active{background:rgba(239,68,68,.15);border-color:var(--red);color:var(--red)}
.travel-segment{display:flex;background:#0e1118;border:1px solid var(--border);border-radius:6px;padding:2px;gap:2px}
.travel-seg-btn{background:none;border:none;border-radius:4px;padding:6px 10px;color:var(--muted);font-size:12px;cursor:pointer;font-family:inherit}
.travel-seg-btn:hover{color:var(--text)}
.travel-seg-btn.active{background:var(--purple);color:#0b0d12;font-weight:600}
.travel-select{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:8px 12px;color:var(--text);font-size:12px;cursor:pointer;font-family:inherit}
.travel-count{margin-left:auto;font-size:11px;color:var(--muted)}
.travel-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.travel-card{display:flex;background:var(--panel);border:1px solid var(--border);border-radius:10px;overflow:hidden;text-decoration:none;color:var(--text);transition:transform .12s,border-color .12s}
.travel-card:hover{transform:translateY(-2px);border-color:var(--purple)}
.travel-card__bar{width:4px;flex-shrink:0}
.travel-card__bar--l1{background:var(--green)}
.travel-card__bar--l2{background:var(--amber)}
.travel-card__bar--l3{background:#fb923c}
.travel-card__bar--l4{background:var(--red)}
.travel-card__body{padding:12px 14px;flex:1;min-width:0}
.travel-card__top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.travel-card__name{font-size:13.5px;font-weight:600;line-height:1.25;min-width:0;word-wrap:break-word}
.travel-card__badge{font-size:10.5px;border:1px solid;border-radius:5px;padding:1px 6px;flex-shrink:0;font-weight:700;letter-spacing:.04em}
.travel-card__badge--l1{color:var(--green);border-color:var(--green)}
.travel-card__badge--l2{color:var(--amber);border-color:var(--amber)}
.travel-card__badge--l3{color:#fb923c;border-color:#fb923c}
.travel-card__badge--l4{color:var(--red);border-color:var(--red)}
.travel-card__lvl{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
.travel-card__lvl--l1{color:var(--green)}
.travel-card__lvl--l2{color:var(--amber)}
.travel-card__lvl--l3{color:#fb923c}
.travel-card__lvl--l4{color:var(--red)}
.travel-card__chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:9px}
.travel-chip{display:inline-block;padding:1px 7px;border-radius:999px;font-size:10px;font-weight:600;border:1px solid var(--border);color:var(--muted);white-space:nowrap}
.travel-chip--terror{color:var(--red);border-color:var(--red)}
.travel-card__date{margin-top:10px;font-size:11px;color:var(--muted)}
.travel-card__norisk{font-size:11px;color:var(--muted);font-style:italic}
.travel-asof-chip{display:inline-block;padding:1px 8px;border-radius:999px;font-size:10px;font-weight:600;border:1px solid var(--green);color:var(--green);background:rgba(34,197,94,.08);white-space:nowrap}
.travel-empty{padding:24px 16px;text-align:center;color:var(--muted);font-size:12px;background:var(--panel);border:1px dashed var(--border);border-radius:10px}
@media (max-width:480px){
  .travel-subtab{padding:7px 10px;font-size:11px}
  .travel-stat__num{font-size:22px}
  .travel-controls{gap:6px}
  .travel-search{flex:1 1 100%;font-size:12px;padding:7px 10px}
  .travel-count{flex:1 1 100%;margin-left:0;text-align:right}
  .travel-grid{grid-template-columns:1fr;gap:8px}
}
</style>
</head>
<body>
<header>
  <div>
    <h1>Crypto Trading Dashboard</h1>
    <div class="meta"><span id="coverage"></span> &middot; <span id="generatedAt"></span></div>
  </div>
  <div class="controls" style="border:0;padding:0">
    <!-- Per-asset BTC/ETH/LINK/LTC selector removed from the header per user
         request — most tabs aren't asset-specific. ETF Flows + Futures stay
         pinned to BTC (state.asset='btc' default); add an inline toggle to
         those tabs if per-asset switching is needed. -->
    <form id="symbolSearchForm" style="margin:0;display:flex;gap:4px;position:relative" onsubmit="return false">
      <input id="symbolSearchInput" type="text" placeholder="Symbol(s): BTC, ETH, NVDA" autocomplete="off"
             aria-label="Search one or more stock or crypto symbols (comma-separated)"
             aria-autocomplete="list" aria-controls="symbolSearchSuggest" aria-expanded="false"
             style="background:#0b0d12;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:5px 8px;font-size:12px;width:160px;outline:none">
      <button class="btn" id="symbolSearchBtn" type="submit" aria-label="Look up symbol">🔍</button>
      <div id="symbolSearchSuggest" class="symbol-suggest hidden" role="listbox" aria-label="Symbol suggestions"></div>
      <!-- Recent-lookups chip row. Sits FURTHER BELOW the form than the
           autocomplete dropdown (top:calc(100% + 4px) vs the dropdown's
           top:100%) so they don't clash. Hidden when empty. Renderer:
           renderSymbolRecentChips(). -->
      <div id="symbolRecentChips" class="hidden"
           style="position:absolute;top:calc(100% + 4px);left:0;display:flex;gap:4px;flex-wrap:nowrap;max-width:min(360px,calc(100vw - 24px));overflow-x:auto;z-index:30;padding-bottom:2px"
           aria-label="Recent symbol lookups"></div>
    </form>
    <button class="btn" id="shareBtn" title="Mint a read-only share link (default 3-day expiry)">🔗 Share</button>
    <button class="btn" id="refreshBtn" title="Re-fetch market + whale data (server only)">↻ Refresh</button>
  </div>
</header>

<div class="tabs" role="tablist">
  <div class="tab" data-tab="ainews" role="tab" tabindex="0" aria-selected="false">AI News</div>
  <div class="tab active" data-tab="overview" role="tab" tabindex="0" aria-selected="true">Crypto</div>
  <div class="tab" data-tab="signals" role="tab" tabindex="0" aria-selected="false">Crypto Signals</div>
  <div class="tab" data-tab="whale" role="tab" tabindex="0" aria-selected="false">Whale Activity</div>
  <div class="tab" data-tab="poc" role="tab" tabindex="0" aria-selected="false">Point of Control</div>
  <div class="tab" data-tab="social" role="tab" tabindex="0" aria-selected="false">Research</div>
  <div class="tab" data-tab="defi" role="tab" tabindex="0" aria-selected="false">DeFi</div>
  <div class="tab" data-tab="etf" role="tab" tabindex="0" aria-selected="false">ETF Flows</div>
  <div class="tab" data-tab="trading" role="tab" tabindex="0" aria-selected="false">Futures</div>
  <div class="tab" data-tab="stocks" role="tab" tabindex="0" aria-selected="false">Stocks</div>
  <div class="tab" data-tab="lthcs" role="tab" tabindex="0" aria-selected="false">LTHCS</div>
  <div class="tab" data-tab="real_estate" role="tab" tabindex="0" aria-selected="false">Real Estate</div>
  <div class="tab" data-tab="cpi" role="tab" tabindex="0" aria-selected="false">CPI</div>
  <div class="tab" data-tab="supplies" role="tab" tabindex="0" aria-selected="false">Supplies</div>
  <div class="tab" data-tab="metals" role="tab" tabindex="0" aria-selected="false">Metals</div>
  <div class="tab" data-tab="travel" role="tab" tabindex="0" aria-selected="false">Travel Advisories</div>
  <div class="tab" data-tab="mufon" role="tab" tabindex="0" aria-selected="false">UAP</div>
</div>

<!-- Global Period + Timeframe header bar removed: it was clutter on tabs
     where it didn't drive content meaningfully (Overview/Signals/Stocks/POC
     /Research/DeFi never read it; ETF/Futures/Whale defaults work fine).
     state.period ('daily') and state.range ('all') defaults flow through
     to the remaining renderers silently. Per-chart Range / Timeframe /
     Compare-window controls (macro overlay, POC overlay, fund compare,
     cohort bin) remain inline on their respective charts. -->

<!-- ============ SHARE MODAL (mint / list / revoke share links) ============ -->
<!-- ============ CONFIGURE SIGNAL CARDS MODAL ============ -->
<div id="configSignalsModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;width:min(440px,100%);max-height:90vh;display:flex;flex-direction:column;gap:10px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:14px">⚙️ Configure signal cards</h2>
      <button class="btn" id="configSignalsClose" aria-label="Close configure signal cards">×</button>
    </div>
    <div class="sub">Pick which assets appear as signal cards on the Crypto tab. Selection persists in your browser.</div>
    <div id="configSignalsList" style="display:flex;flex-direction:column;gap:8px;padding:6px 0"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;border-top:1px solid var(--border);padding-top:10px">
      <button class="btn" id="configSignalsReset">Reset to default</button>
      <button class="btn active" id="configSignalsSave">Save</button>
    </div>
    <div id="configSignalsStatus" class="sub" style="color:var(--muted);min-height:14px"></div>
  </div>
</div>

<!-- (Legacy SIGNAL DETAIL MODAL retired here. Every entry point that
     previously opened #signalDetailModal now routes through openSignalDetail
     → lookupSymbol → the universal #symbolDetailModal which pairs Signal
     + POC side-by-side. Keeping this comment so a future grep finds the
     intentional removal.) -->

<!-- ============ STOCK DETAIL MODAL (Stocks tab card → full breakdown) ============ -->
<div id="stockDetailModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;width:min(820px,100%);max-height:92vh;display:flex;flex-direction:column;gap:10px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 id="stockDetailTitle" style="margin:0;font-size:15px">Stock detail</h2>
      <button class="btn" id="stockDetailClose" aria-label="Close stock detail">×</button>
    </div>
    <div id="stockDetailBody"></div>
  </div>
</div>

<!-- ============ POC DETAIL MODAL (POC tab card → full breakdown) ============ -->
<div id="pocDetailModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;width:min(820px,100%);max-height:92vh;display:flex;flex-direction:column;gap:10px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 id="pocDetailTitle" style="margin:0;font-size:15px">POC detail</h2>
      <button class="btn" id="pocDetailClose" aria-label="Close POC detail">×</button>
    </div>
    <div id="pocDetailBody"></div>
  </div>
</div>

<!-- POC volume profile fullscreen overlay (desktop only — fullscreen button
     is hidden via @media on mobile because the modal-sized chart is already
     legible on phone viewports). Click expand button → SVG copies in here
     full-viewport; click × or Escape to dismiss. -->
<div id="pocVolFullscreen" class="modal-bg hidden" style="padding:0">
  <div style="background:var(--panel);width:100vw;height:100vh;display:flex;flex-direction:column;gap:6px;padding:14px 18px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px">
      <div>
        <h2 id="pocVolFullscreenTitle" style="margin:0;font-size:16px">Volume profile</h2>
        <div class="sub" style="font-size:11px;color:var(--muted)">90d (30d dashed) · current price marker · Esc / × to close</div>
      </div>
      <button class="btn" id="pocVolFullscreenClose" aria-label="Close fullscreen volume profile">×</button>
    </div>
    <div id="pocVolFullscreenBody" style="flex:1;min-height:0;display:flex;align-items:center;justify-content:center"></div>
  </div>
</div>

<!-- ============ NEWS SENTIMENT DETAIL MODAL (Research tab — click any Top-25 row) ============ -->
<div id="newsSentimentDetailModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;width:min(720px,100%);max-height:92vh;display:flex;flex-direction:column;gap:10px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 id="newsSentimentDetailTitle" style="margin:0;font-size:15px">News sentiment</h2>
      <button class="btn" id="newsSentimentDetailClose" aria-label="Close news sentiment detail">×</button>
    </div>
    <div id="newsSentimentDetailBody"></div>
  </div>
</div>

<!-- ============ SYMBOL DETAIL MODAL (universal header search → consolidated view) ============ -->
<!-- Width bumped to 940px so the Signal + POC cards can sit side-by-side on
     desktop with breathing room. Stacks 1-col on mobile via .grid2 @media
     override at 860px (further tightened to ≤480px below in CSS). -->
<div id="symbolDetailModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;width:min(940px,100%);max-height:92vh;display:flex;flex-direction:column;gap:12px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 id="symbolDetailTitle" style="margin:0;font-size:15px">Symbol</h2>
      <button class="btn" id="symbolDetailClose" aria-label="Close symbol detail">×</button>
    </div>
    <div id="symbolDetailBody"></div>
  </div>
</div>

<!-- ============ POC EXPLAINER MODAL ============ -->
<div id="pocExplainerModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;width:min(620px,100%);max-height:90vh;display:flex;flex-direction:column;gap:10px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:14px">📊 What is Point of Control?</h2>
      <button class="btn" id="pocExplainerClose" aria-label="Close Point of Control explainer">×</button>
    </div>
    <div class="sub" style="line-height:1.55;color:var(--text)">
      <p><strong>Plain language.</strong> The Point of Control (POC) is the price level where the most volume has traded over a given window. Think of it as the price buyers and sellers keep <em>gravitating back to</em> — the market's recent "center of gravity."</p>

      <h3 style="margin:10px 0 4px;font-size:12px;letter-spacing:.04em;color:var(--text)">HOW THIS DASHBOARD COMPUTES IT</h3>
      <p>Each card builds a <strong>volume profile</strong>: daily candles are bucketed by price, weighted by traded volume. We run it over two lookbacks — <span class="tag">30d</span> (tactical) and <span class="tag">90d</span> (structural). The POC is the highest-volume bucket. The <strong>Value Area</strong> (VAH / VAL) is the contiguous range around the POC that contains <strong>70%</strong> of total volume — roughly one standard deviation of where price "agreed."</p>

      <h3 style="margin:10px 0 4px;font-size:12px;letter-spacing:.04em;color:var(--text)">HOW TO READ A CARD</h3>
      <div style="display:flex;gap:14px;align-items:center;margin:6px 0 8px;font-size:11px">
        <div style="flex:1;position:relative;height:54px;border:1px solid var(--border);border-radius:6px;background:linear-gradient(to top,#0b0d12,#13202a 30%,#1a3a4a 50%,#13202a 70%,#0b0d12)">
          <div style="position:absolute;left:0;right:0;top:48%;border-top:2px dashed #ffcc66"></div>
          <div style="position:absolute;left:0;right:0;top:18%;border-top:1px dotted #4a8;opacity:.7"></div>
          <div style="position:absolute;left:0;right:0;top:78%;border-top:1px dotted #4a8;opacity:.7"></div>
          <span style="position:absolute;right:6px;top:42%;color:#ffcc66">POC</span>
          <span style="position:absolute;right:6px;top:12%;color:#7ad">VAH</span>
          <span style="position:absolute;right:6px;top:72%;color:#7ad">VAL</span>
        </div>
      </div>
      <p><strong>POC price</strong> — fair-value magnet. <strong>VAH / VAL</strong> — the top and bottom of the 70% Value Area. <span class="tag">IN VA</span> means current price sits inside that band (consolidation / accepted value). <span class="tag">OUTSIDE</span> means price has broken above VAH or below VAL.</p>

      <p style="margin-top:6px"><strong>The big arrow on each card</strong> tells you which way value is migrating:
      <span style="color:#22c55e;font-weight:700">↑ UP</span> means the POC is drifting higher (accumulation) ·
      <span style="color:#ef4444;font-weight:700">↓ DOWN</span> means the POC is drifting lower (distribution) ·
      <span style="color:var(--muted);font-weight:700">· FLAT</span> means value is stable.
      The little chart on each card is the 30d POC's drift over the last 90 days. Distance % shows where current price sits relative to that POC.</p>

      <h3 style="margin:10px 0 4px;font-size:12px;letter-spacing:.04em;color:var(--text)">WHAT IT MEANS FOR TRADING</h3>
      <p>
        • <strong>Above POC + OUTSIDE</strong> → extended; the move is stretched relative to recent accepted value.<br>
        • <strong>Below POC + OUTSIDE</strong> → discount; price is trading below where most volume changed hands.<br>
        • <strong>Inside VA</strong> → consolidation; supply and demand are roughly balanced, breakouts from here are often more meaningful.
      </p>

      <h3 style="margin:10px 0 4px;font-size:12px;letter-spacing:.04em;color:var(--text)">ORIGINS</h3>
      <p>Volume profile and the Value Area concept come from <strong>Market Profile</strong>, developed by J. Peter Steidlmayer at the CBOT in the 1980s as a way to read auction-market behavior intraday.</p>

      <div class="note" style="margin-top:8px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:#1a1410;color:#e9d27a;font-size:11px">
        <strong>Not investment advice.</strong> POC describes where volume <em>has</em> traded, not where price <em>will</em> go. It's a statistical tendency, not a certainty — price can stay extended or break structure for a long time. Use it as context alongside other signals.
      </div>
    </div>
  </div>
</div>

<div id="shareModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;width:min(640px,100%);max-height:90vh;display:flex;flex-direction:column;gap:10px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:14px">🔗 Share dashboard (read-only)</h2>
      <button class="btn" id="shareClose" aria-label="Close share dashboard modal">×</button>
    </div>
    <div class="sub">Mints a token-gated URL. Anyone with the link can view this dashboard (data refreshes live), but cannot trigger refreshes, upload data, or use chat. Link auto-expires.</div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid var(--border)">
      <span class="lbl" style="margin:0">Public host</span>
      <input id="shareHost" placeholder="https://my-tunnel.trycloudflare.com" style="flex:1;min-width:200px;background:#0b0d12;color:var(--text);border:1px solid var(--border);padding:4px 8px;border-radius:4px;font:11px monospace" />
      <button class="btn" id="shareHostSave">Save</button>
      <a href="#" id="shareHostClear" style="font-size:11px;color:var(--muted)">Clear</a>
      <span id="shareHostStatus" class="sub" style="color:var(--muted)"></span>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;border-top:1px solid var(--border);padding-top:10px">
      <span class="lbl" style="margin:0">Expires in</span>
      <select id="shareDays" style="background:var(--panel2);color:var(--text);border:1px solid var(--border);padding:3px 6px;border-radius:4px">
        <option value="1">1 day</option>
        <option value="3" selected>3 days</option>
        <option value="7">7 days</option>
        <option value="14">14 days</option>
      </select>
      <input id="shareLabel" placeholder="Label (optional, e.g. 'for J. via SMS')" style="flex:1;min-width:160px;background:#0b0d12;color:var(--text);border:1px solid var(--border);padding:4px 8px;border-radius:4px;font:12px sans-serif" />
      <button class="btn" id="shareCreate">Mint link</button>
    </div>
    <div id="shareJustMinted" class="hidden" style="background:#1a2840;border:1px solid #2c3e5e;border-radius:6px;padding:10px">
      <div class="sub" style="margin-bottom:6px;color:#bfd2ff">New link · copy + text it:</div>
      <div style="display:flex;gap:6px;align-items:center">
        <input id="shareNewUrl" readonly style="flex:1;background:#0b0d12;color:var(--text);border:1px solid var(--border);padding:4px 8px;border-radius:4px;font:11px monospace" />
        <button class="btn" id="shareCopyBtn">Copy</button>
      </div>
      <div id="shareNewWarn" class="hidden" style="margin-top:6px;font-size:11px;color:#e9d27a;background:#2a2410;border:1px solid #4a3f1a;border-radius:4px;padding:4px 8px"></div>
    </div>
    <div style="border-top:1px solid var(--border);padding-top:10px">
      <div class="sub" style="margin-bottom:6px">Active links</div>
      <div id="shareList" style="display:flex;flex-direction:column;gap:6px;max-height:240px;overflow:auto"></div>
    </div>
    <div id="shareStatus" class="sub" style="color:var(--muted);min-height:14px"></div>
  </div>
</div>

<div class="container">
  <!-- ============ INSIGHTS BAR (always visible) ============ -->
  <div id="insightsBar" class="card" style="padding:10px 14px">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:14px">⚡</span>
        <strong style="font-size:13px;letter-spacing:.03em">Insights</strong>
        <span class="sub" id="insightsCount" style="color:var(--muted);font-size:11px"></span>
      </div>
      <button class="btn" id="insightsToggle" style="font-size:11px;padding:3px 8px">Hide</button>
    </div>
    <div id="insightsList" style="display:flex;flex-wrap:wrap;gap:6px"></div>
  </div>

  <!-- ============ OVERVIEW TAB (LANDING PAGE) ============ -->
  <div id="tab-overview">
    <!-- Top row: LEFT column stacks Latest crypto news (cap 3) + Top
         insights (cap 3) — the text-heavy lead-ins. RIGHT column carries
         AI-exposed stocks — a visual ticker grid mirroring the AI News tab.
         Restructured per user request to surface AI signals on the Crypto
         landing page above the fold. Mobile: stacks to a single column. -->
    <div id="overviewTopRow" style="display:grid;grid-template-columns:1fr 1.2fr;gap:12px;align-items:start">
      <div style="display:flex;flex-direction:column;gap:12px;min-width:0">
        <div class="chart-card" style="cursor:pointer" data-jump="trading" title="See full news feed in Trading tab">
          <div class="head"><h2>Latest crypto news</h2><span class="desc">Top 3 · click for full feed</span></div>
          <div id="overviewNews"></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Top insights</h2><span class="desc">Most-relevant 3 right now</span></div>
          <div id="overviewInsights" style="display:flex;flex-direction:column;gap:8px;padding:2px"></div>
        </div>
      </div>
      <div style="min-width:0">
        <div class="chart-card" id="overviewAiStocksCard">
          <div class="head">
            <h2>AI-exposed stocks</h2>
            <span class="desc">Signal score for tickers most exposed to AI/ML/chips &middot; filtered from Stocks tab</span>
          </div>
          <div id="overviewAiStocksGrid" class="row" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px"></div>
        </div>
      </div>
    </div>
    <style>
      @media (max-width: 860px) {
        #overviewTopRow { grid-template-columns: 1fr !important; }
      }
    </style>

    <!-- CRYPTO MARKET SENTIMENT — composite of Fear & Greed, top-50 signal
         score avg, and average perp funding rate. Rendered by
         renderOverviewSentiment(). Mirrors the visual pattern of
         #pocSentimentCard on the POC tab. -->
    <div class="card" id="overviewSentimentCard" style="padding:14px 16px;margin-bottom:6px;border-left:4px solid #a78bfa">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:6px;flex-wrap:wrap">
        <div>
          <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.06em">📊 CRYPTO MARKET SENTIMENT</div>
          <div style="font-size:11px;color:var(--muted)" id="overviewSentimentSubline">—</div>
        </div>
        <div style="text-align:right">
          <div id="overviewSentimentScore" style="font-size:28px;font-weight:700;line-height:1">—</div>
          <div id="overviewSentimentLabel" style="font-size:11px;font-weight:700;letter-spacing:.05em">—</div>
        </div>
      </div>
      <div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:#1f2533">
        <div style="background:#22c55e;width:0%" id="overviewSentimentBarPos"></div>
        <div style="background:#94a3b8;width:0%" id="overviewSentimentBarNeu"></div>
        <div style="background:#ef4444;width:0%" id="overviewSentimentBarNeg"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">
        <span style="color:#22c55e">BULLISH inputs</span>
        <span>NEUTRAL</span>
        <span style="color:#ef4444">BEARISH inputs</span>
      </div>
    </div>

    <!-- Row 1: Signal cards (HERO — clickable) -->
    <div style="display:flex;justify-content:flex-end;margin-bottom:-6px">
      <button class="btn" id="configSignalsBtn" style="font-size:11px;padding:3px 8px" title="Pick which assets show signal cards">⚙️ Configure</button>
    </div>
    <div class="row" id="overviewSignals" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))"></div>

    <!-- Strong Buy / Buy signals: up to 5 STRONG BUY or BUY signals from
         the top-50 strip, sorted by score descending so STRONG BUYs surface
         first. Hidden when none exist. Cards click through to the signal
         detail modal (same one the Signals-tab strip uses). -->
    <div id="overviewStrongBuysWrap" class="chart-card hidden" style="padding:12px 16px;margin-top:6px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">🚀 Strong Buy / Buy Signals <span class="tag">Top 50</span></h2>
        <span class="desc">Up to 5 STRONG BUY + BUY signals from the top-50 by market cap · sorted by score · click any card for the full breakdown</span>
      </div>
      <div class="row" id="overviewStrongBuys" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr))"></div>
    </div>

    <!-- Other top coins by market cap: structural "what's the rest of the
         market doing" view. The four pinned assets (BTC/ETH/LINK/LTC)
         already appear in their own big cards above with signal score
         surfaced — this grid skips them so cards don't duplicate. Re-sorts
         signals_top20 by CoinGecko market-cap rank. -->
    <div id="overviewTop15Wrap" class="chart-card hidden" style="padding:12px 16px;margin-top:6px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">🏆 Other top coins by market cap</h2>
        <span class="desc">Top non-pinned coins · price + signal · click any card for the full breakdown</span>
      </div>
      <div class="row" id="overviewTop15" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr))"></div>
    </div>

    <!-- Row 3: Macro snapshot (full width) -->
    <div id="overviewMacroRow">
      <div class="chart-card" style="cursor:pointer;display:flex;flex-direction:column" data-jump="trading" title="Open Trading tab for full 1Y view">
        <div class="head">
          <h2>Macro snapshot <span class="tag">FRED</span></h2>
          <span class="desc">BTC vs DXY · S&amp;P · Gold · 10Y &middot; normalized to 100 over 3M &middot; click to zoom in</span>
        </div>
        <div class="chart-wrap" style="flex:1;min-height:380px;height:auto"><canvas id="overviewMacroChart"></canvas></div>
      </div>
    </div>

    <!-- DEX pools — trending by volume + brand-new listings.
         Moved here from the (now-deleted) Markets tab. Useful at the end
         of Overview as a "what's hot in DeFi" peek without needing a
         dedicated tab. Memecoin/early-listing radar. -->
    <div class="grid2">
      <div class="chart-card">
        <div class="head">
          <h2>DEX trending pools <span class="tag">GeckoTerminal</span></h2>
          <span class="desc">top 10 DEX pools by 24h volume across all chains</span>
        </div>
        <div style="max-height:360px;overflow:auto">
          <table id="gtTrendingTable" style="margin:0;font-size:12px"><thead><tr>
            <th style="padding-left:14px">#</th>
            <th>Pool</th><th>Chain</th><th>Vol 24h</th><th>1d %</th><th>Tx 24h</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>
      <div class="chart-card">
        <div class="head">
          <h2>DEX new pools <span class="tag">GeckoTerminal</span></h2>
          <span class="desc">freshest listings · memecoin / early-listing radar</span>
        </div>
        <div style="max-height:360px;overflow:auto">
          <table id="gtNewTable" style="margin:0;font-size:12px"><thead><tr>
            <th style="padding-left:14px">#</th>
            <th>Pool</th><th>Chain</th><th>Vol 24h</th><th>1d %</th><th>Tx 24h</th>
          </tr></thead><tbody></tbody></table>
        </div>
      </div>
    </div>

    <!-- Coinbase spot quotes — moved per user request to sit right before
         breaking news. Bid/ask + 24h range from Coinbase Exchange. -->
    <div id="coinbaseSpotWrap" class="chart-card hidden" style="padding:12px 16px;margin-top:6px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">Coinbase spot <span class="tag">live exchange</span></h2>
        <span class="desc">Bid/ask + 24h range from Coinbase Exchange (US-regulated). Cross-check vs CoinGecko aggregate.</span>
      </div>
      <div style="overflow:auto">
        <table id="coinbaseSpotTable" style="margin:0;font-size:12px">
          <thead><tr>
            <th>Asset</th>
            <th style="text-align:right">Bid / Ask</th>
            <th style="text-align:right">24h range</th>
            <th style="text-align:right">24h %</th>
            <th style="text-align:right">24h volume</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>

    <!-- Coinbase Pulse — taker buy/sell ratio per coin (BTC/ETH/SOL/LINK)
         computed from the last 300 trades on the Coinbase Exchange public
         /trades endpoint. Sits next to the spot card so traders can read
         price + flow side-by-side. Source: DATA.market.coinbase_pulse,
         written by fetch_coinbase.py. -->
    <div id="coinbasePulseWrap" class="chart-card hidden" style="padding:12px 16px;margin-top:6px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">Coinbase Pulse <span class="tag">taker buy ratio</span></h2>
        <span class="desc">Coinbase taker buy-vs-sell over last 300 trades. &gt;50% = taker-side buying (bullish flow); &lt;50% = taker-side selling (bearish flow). Sparkline = 5-min buckets over last ~25 minutes.</span>
      </div>
      <div style="overflow:auto">
        <table id="coinbasePulseTable" style="margin:0;font-size:12px">
          <thead><tr>
            <th>Asset</th>
            <th style="text-align:right">Price</th>
            <th style="text-align:right">24h %</th>
            <th style="text-align:right">Buy ratio</th>
            <th style="text-align:right">Flow trend</th>
            <th style="text-align:right">Spread</th>
            <th style="text-align:right">24h vol</th>
          </tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <div id="coinbasePulseMeta" class="sub" style="margin-top:6px;font-size:10px;color:var(--muted)"></div>
    </div>

    <!-- Bottom-of-Overview: continues the news feed past the top-4 teaser
         (items 5-14) so the user doesn't read the same headlines twice. -->
    <div class="chart-card">
      <div class="head">
        <h2>More headlines</h2>
        <span class="desc">items 5-14 from the same feed</span>
      </div>
      <div id="overviewNewsHost"></div>
    </div>
  </div>

  <!-- ============ ETF FLOWS TAB ============ -->
  <div id="tab-etf" class="hidden">
    <!-- ETF FLOW SENTIMENT — composite of 7d net flow sum and 30d net flow
         sum, weighted 60/40 toward the 7d. Tracks the BTC/ETH toggle below.
         Rendered by renderEtfFlowSentiment(). -->
    <div class="card" id="etfFlowSentimentCard" style="padding:14px 16px;margin-bottom:10px;border-left:4px solid #a78bfa">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:6px;flex-wrap:wrap">
        <div>
          <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.06em">💰 ETF FLOW SENTIMENT</div>
          <div style="font-size:11px;color:var(--muted)" id="etfFlowSentimentSubline">—</div>
        </div>
        <div style="text-align:right">
          <div id="etfFlowSentimentScore" style="font-size:28px;font-weight:700;line-height:1">—</div>
          <div id="etfFlowSentimentLabel" style="font-size:11px;font-weight:700;letter-spacing:.05em">—</div>
        </div>
      </div>
      <div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:#1f2533">
        <div style="background:#22c55e;width:0%" id="etfFlowSentimentBarPos"></div>
        <div style="background:#94a3b8;width:0%" id="etfFlowSentimentBarNeu"></div>
        <div style="background:#ef4444;width:0%" id="etfFlowSentimentBarNeg"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">
        <span style="color:#22c55e">INFLOWS</span>
        <span>BALANCED</span>
        <span style="color:#ef4444">OUTFLOWS</span>
      </div>
    </div>
    <div class="card" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px">
      <span class="lbl" style="margin:0">Load data</span>
      <button class="btn" id="loadBtcBtn" title="Paste BTC ETF flow CSV from Farside">Paste BTC</button>
      <button class="btn" id="loadEthBtn" title="Paste ETH ETF flow CSV from Farside">Paste ETH</button>
      <button class="btn" id="seedBtcBtn" title="Pull BTC from canadiancode/btc-etf-flows GitHub mirror (may be stale)">Seed BTC (mirror)</button>
      <a class="btn" href="/bookmarklet" target="_blank" rel="noopener noreferrer" style="text-decoration:none" title="One-click bookmarklet for Farside pages">Get bookmarklet</a>
      <span id="loadStatus" class="sub" style="margin-left:8px;color:var(--muted)"></span>
    </div>
    <!-- Per-tab asset toggle: BTC or ETH (no spot LINK/LTC ETFs exist).
         Decoupled from the global state.asset — drives state.etfAsset only,
         persisted to localStorage. Mirrors the Whale tab's inline toggle. -->
    <div class="card" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 12px;margin-bottom:10px">
      <span class="lbl" style="margin:0">View</span>
      <button class="btn" data-etfasset="btc">BTC</button>
      <button class="btn" data-etfasset="eth">ETH</button>
    </div>
    <div id="etfEmpty" class="empty hidden">
      <div>No ETF flow data loaded yet.</div>
      <div style="margin-top:14px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
        <button class="btn" id="seedBtn" title="Pull from canadiancode/btc-etf-flows GitHub mirror (BTC Total only, may be stale)">Seed BTC from GitHub mirror</button>
        <button class="btn" id="pasteBtn">Paste CSV…</button>
      </div>
      <div id="seedStatus" class="sub" style="margin-top:10px;color:var(--muted)"></div>
    </div>
    <div id="pasteModal" class="modal-bg hidden">
      <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;width:min(720px,100%);max-height:90vh;display:flex;flex-direction:column;gap:10px">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <h2 style="margin:0;font-size:14px">Paste ETF flow CSV</h2>
          <button class="btn" id="pasteClose" aria-label="Close paste CSV modal">×</button>
        </div>
        <div class="sub">First line is the header. Tab-separated also OK (paste from a browser table). Asset:
          <select id="pasteAsset" style="background:var(--panel2);color:var(--text);border:1px solid var(--border);padding:3px 6px;border-radius:4px"><option value="btc">BTC</option><option value="eth">ETH</option></select>
        </div>
        <textarea id="pasteText" rows="12" style="width:100%;background:#0b0d12;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px;font:12px monospace;resize:vertical" placeholder="date,IBIT,FBTC,BITB,...,Total&#10;2024-01-11,111.7,227.0,...,655.3"></textarea>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn" id="pasteSubmit">Import</button>
        </div>
        <div id="pasteStatus" class="sub" style="color:var(--muted)"></div>
      </div>
    </div>
    <div id="etfContent">
      <div class="row" id="etfKpis"></div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>Net flow <span class="tag" id="tagAsset1">BTC</span></h2><span class="desc">USD millions, negative = outflow</span></div>
          <div class="chart-wrap"><canvas id="flowChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Cumulative flow <span class="tag" id="tagAsset2">BTC</span></h2><span class="desc">All-time running net</span></div>
          <div class="chart-wrap"><canvas id="cumChart"></canvas></div>
        </div>
      </div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>Year-over-year cumulative <span class="tag" id="tagAsset3">BTC</span></h2><span class="desc">By day-of-year</span></div>
          <div class="chart-wrap"><canvas id="yoyChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>By fund <span class="tag" id="tagAsset4">BTC</span></h2><span class="desc">All-time &amp; last 30d</span></div>
          <div style="max-height:300px;overflow:auto">
            <table id="fundTable"><thead><tr><th>Fund</th><th>All-time ($M)</th><th>Last 30d ($M)</th></tr></thead><tbody></tbody></table>
          </div>
        </div>
      </div>

      <!-- ===== By-Fund detail section ===== -->
      <div style="margin-top:6px;padding:14px 16px;background:#10151f;border:1px solid var(--border);border-radius:10px">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px">
          <div>
            <h2 style="margin:0;font-size:15px">Fund detail <span class="tag" id="tagFundDetail">BTC</span></h2>
            <div class="sub" style="color:var(--muted)">Per-fund KPIs, cumulative trajectory, and time-window comparison</div>
          </div>
          <div>
            <span class="lbl" style="margin:0">Compare window</span>
            <button class="btn active" data-fundwin="30">30d</button>
            <button class="btn" data-fundwin="60">60d</button>
            <button class="btn" data-fundwin="90">90d</button>
            <button class="btn" data-fundwin="all">All</button>
          </div>
        </div>
        <div id="fundKpiGrid" class="row" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))"></div>
      </div>

      <div class="grid2" style="margin-top:18px">
        <div class="chart-card">
          <div class="head"><h2>Cumulative flow stacked by fund <span class="tag" id="tagStack">BTC</span></h2><span class="desc">Running total per fund, USD millions</span></div>
          <div class="chart-wrap tall"><canvas id="fundStackChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Fund comparison <span class="tag" id="tagCompare">BTC</span></h2><span class="desc">Net flow over selected window</span></div>
          <div class="chart-wrap tall"><canvas id="fundCompareChart"></canvas></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ TRADING TAB ============ -->
  <div id="tab-trading" class="hidden">
    <div id="tradingEmpty" class="empty hidden">No market data. Run <code>python app.py --fetch-market</code>.</div>
    <div id="tradingContent">
      <details class="futures-explainer">
        <summary>What's a perpetual? &mdash; Futures &amp; perpetuals dashboard explainer</summary>
        <div class="card" style="padding:14px 16px;margin-top:6px;margin-bottom:10px;border-left:3px solid var(--btc)">
          <h2 style="margin:0 0 6px;font-size:14px">Futures &amp; perpetuals dashboard</h2>
          <p class="sub" style="font-size:12px;line-height:1.5;color:var(--muted);margin:0">
            Derivatives positioning for BTC, ETH, LINK, LTC. <strong style="color:var(--text)">Funding rate</strong> shows perp traders paying to hold longs (positive) or shorts (negative); extremes signal crowded positioning. <strong style="color:var(--text)">Open interest</strong> is total notional in active perp contracts. <strong style="color:var(--text)">Long/short ratio</strong> from OKX shows top-account positioning bias. <strong style="color:var(--text)">DVOL</strong> is Deribit's BTC/ETH implied-volatility index. The two tables list Coinbase International Exchange perps with the most extreme positive (crowded longs) and negative (crowded shorts) funding rates.
          </p>
        </div>
      </details>
      <!-- FUTURES POSITIONING SENTIMENT — composite of funding rate, long/short
           ratio, and 7d OI change for the currently-selected asset. Rendered
           by renderFuturesSentiment(). -->
      <div class="card" id="futuresSentimentCard" style="padding:14px 16px;margin-bottom:10px;border-left:4px solid #a78bfa">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:6px;flex-wrap:wrap">
          <div>
            <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.06em">🎯 FUTURES POSITIONING SENTIMENT</div>
            <div style="font-size:11px;color:var(--muted)" id="futuresSentimentSubline">—</div>
          </div>
          <div style="text-align:right">
            <div id="futuresSentimentScore" style="font-size:28px;font-weight:700;line-height:1">—</div>
            <div id="futuresSentimentLabel" style="font-size:11px;font-weight:700;letter-spacing:.05em">—</div>
          </div>
        </div>
        <div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:#1f2533">
          <div style="background:#22c55e;width:0%" id="futuresSentimentBarPos"></div>
          <div style="background:#94a3b8;width:0%" id="futuresSentimentBarNeu"></div>
          <div style="background:#ef4444;width:0%" id="futuresSentimentBarNeg"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">
          <span style="color:#22c55e">CROWDED LONGS</span>
          <span>BALANCED</span>
          <span style="color:#ef4444">CROWDED SHORTS</span>
        </div>
      </div>
      <!-- Per-tab asset toggle: BTC / ETH / LINK / LTC (full original set with
           derivatives data). Coupled to state.asset on click since Futures
           renderers are deeply tangled with the global asset (tradingAssetData,
           POC overlay, KPI dominance, DVOL empty-state copy). state.futuresAsset
           tracks the persisted choice and is mirrored to state.asset whenever
           the Futures tab is active. -->
      <div class="card" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:8px 12px;margin-bottom:10px">
        <span class="lbl" style="margin:0">View</span>
        <button class="btn" data-futuresasset="btc">BTC</button>
        <button class="btn" data-futuresasset="eth">ETH</button>
        <button class="btn" data-futuresasset="link">LINK</button>
        <button class="btn" data-futuresasset="ltc">LTC</button>
      </div>
      <div class="row" id="tradingKpis"></div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head" style="align-items:center;gap:10px;flex-wrap:wrap">
            <h2 style="margin:0">Price &amp; volume <span class="tag" id="tagPrice">BTC</span></h2>
            <span class="desc" style="flex:1">Spot price (line) &middot; 24h volume (bars)</span>
            <label style="font-size:11px;display:inline-flex;gap:5px;align-items:center;cursor:pointer">
              <input type="checkbox" id="pocOverlayToggle"> POC overlay
            </label>
            <span id="pocWinChips" style="display:none;gap:4px">
              <button class="btn" data-pocwin="d30" type="button" style="font-size:10px;padding:3px 8px">30d</button>
              <button class="btn" data-pocwin="d90" type="button" style="font-size:10px;padding:3px 8px">90d</button>
            </span>
          </div>
          <div class="chart-wrap tall"><canvas id="priceChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Funding rate <span class="tag" id="tagFunding">BTC</span></h2><span class="desc">OKX perpetual, daily mean &middot; +ve = longs pay shorts</span></div>
          <div class="chart-wrap"><canvas id="fundingChart"></canvas></div>
        </div>
      </div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>Open interest (USD) <span class="tag" id="tagOI">BTC</span></h2><span class="desc">OKX aggregated futures + perps</span></div>
          <div class="chart-wrap"><canvas id="oiChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Long/short account ratio <span class="tag" id="tagLS">BTC</span></h2><span class="desc">OKX traders &middot; >1 = more longs</span></div>
          <div class="chart-wrap"><canvas id="lsChart"></canvas></div>
        </div>
      </div>
      <!-- CADLI BTC reference price — 90d daily closes from the CoinDesk
           CADLI Cryptocurrency Real-Time Index. This is the regulated
           reference price used in derivatives settlement, so it sits with
           the rest of the futures-positioning surface. -->
      <div class="chart-card">
        <div class="head">
          <h2>CADLI BTC reference price <span class="tag">CoinDesk</span></h2>
          <span class="desc">90d OHLC from the CoinDesk CADLI Cryptocurrency Real-Time Index used in regulated derivatives pricing</span>
        </div>
        <div class="chart-wrap"><canvas id="cadliBtcChart"></canvas></div>
      </div>
      <!-- Coinbase International Exchange perpetuals positioning: two side-by-side
           tables surfacing the most crowded LONGS (highest funding) and SHORTS
           (most negative funding) from the ~246 PERP markets. Funding rate is
           a positioning gauge — positive = longs paying shorts (crowded long,
           squeeze setup); negative = shorts paying longs (contrarian buy zone). -->
      <div class="chart-card" style="background:transparent;border:0;padding:0">
        <div class="head" style="padding:0 4px 6px">
          <h2 style="margin:0">Coinbase Intl perpetuals positioning <span class="tag">FUNDING</span></h2>
          <span class="desc">crowded longs vs crowded shorts &middot; ~246 PERP markets</span>
        </div>
        <div class="grid2">
          <div class="chart-card">
            <div class="head">
              <h2>Most crowded LONGS <span class="tag">Coinbase Intl</span></h2>
              <span class="desc">highest funding rates &middot; squeeze setup risk</span>
            </div>
            <div style="overflow:auto">
              <table id="cieLongsTable" class="tracker-grid">
                <thead><tr><th>Symbol</th><th>Funding</th><th>Mark</th><th>Notional 24h</th><th>OI</th></tr></thead>
                <tbody></tbody>
              </table>
            </div>
          </div>
          <div class="chart-card">
            <div class="head">
              <h2>Most crowded SHORTS <span class="tag">Coinbase Intl</span></h2>
              <span class="desc">most negative funding &middot; contrarian zone</span>
            </div>
            <div style="overflow:auto">
              <table id="cieShortsTable" class="tracker-grid">
                <thead><tr><th>Symbol</th><th>Funding</th><th>Mark</th><th>Notional 24h</th><th>OI</th></tr></thead>
                <tbody></tbody>
              </table>
            </div>
          </div>
        </div>
        <div class="note" style="margin-top:8px;font-size:11px">
          Funding rate fires every 1h. Positive = longs pay shorts (crowded long, squeeze setup). Negative = shorts pay longs (crowded short, contrarian buy zone). Funding &times; 24 &asymp; approx daily annualized cost.
        </div>
      </div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>Implied volatility (DVOL) <span class="tag" id="tagDvol">BTC</span></h2><span class="desc">Deribit options-implied 30d vol, %</span></div>
          <div class="chart-wrap"><canvas id="dvolChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Fear &amp; Greed Index</h2><span class="desc">Crypto-wide sentiment, 0=fear 100=greed</span></div>
          <div class="chart-wrap"><canvas id="fngChart"></canvas></div>
        </div>
      </div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>ETH/BTC ratio <span class="tag">CoinGecko</span></h2><span class="desc">Relative strength</span></div>
          <div class="chart-wrap"><canvas id="ethbtcChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Market snapshot <span class="tag">CoinGecko</span></h2><span class="desc">CoinGecko global stats</span></div>
          <div style="padding:8px 4px"><table id="globalTable"><tbody></tbody></table></div>
        </div>
      </div>
      <div class="chart-card">
        <div class="head"><h2>Latest crypto news</h2><span class="desc">CoinDesk · Cointelegraph · Decrypt · The Block · BTC Magazine (RSS, auto-refresh)</span></div>
        <div id="newsFeed" style="max-height:480px;overflow:auto;padding:2px"></div>
      </div>
      <div class="chart-card" id="macroSection">
        <div class="head" style="flex-wrap:wrap;gap:8px">
          <div>
            <h2>Macro overlay <span class="tag">FRED</span></h2>
            <span class="desc">BTC vs DXY · S&amp;P 500 · Gold · 10Y yield — normalized to 100 at start of range</span>
          </div>
          <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
            <span class="lbl" style="margin:0">Range</span>
            <button class="btn" data-macrorange="1M">1M</button>
            <button class="btn" data-macrorange="3M">3M</button>
            <button class="btn" data-macrorange="6M">6M</button>
            <button class="btn active" data-macrorange="1Y">1Y</button>
          </div>
        </div>
        <div id="macroDisabled" class="sub hidden" style="color:var(--muted);padding:14px">Macro overlay disabled — set <code>FRED_API_KEY</code> in <code>~/.zprofile</code> to enable. See <code>docs/SETUP.md</code>.</div>
        <div id="macroEnabled">
          <div class="chart-wrap tall"><canvas id="macroChart"></canvas></div>
          <div class="row" id="macroKpis" style="margin-top:8px"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ STOCKS TAB ============ -->
  <div id="tab-stocks" class="hidden">
    <div class="container">
      <!-- LTHCS Insights row — dynamic 3-5 insights + corner CTA. Mirrors
           the LTHCS-tab layout so both tabs read consistently. Filled by
           renderLthcsInsightsRow(host) from DATA.lthcs.insights. -->
      <div class="card" id="stocksLthcsInsightsRow" style="padding:12px 14px;margin-bottom:10px;border-left:4px solid #a78bfa"></div>
      <!-- LTHCS Composite Index — long-term holding conviction across the
           167-ticker universe. Visual model mirrors the Whale Sentiment
           Index card (headline + ±100 gauge + component table). Top movers
           row + CTA to the full LTHCS dashboard follow. Rendered by
           renderLthcsCompositePanel(host) from DATA.lthcs at build time. -->
      <div class="chart-card" id="stocksLthcsCompositeCard" style="position:relative;margin-bottom:10px"></div>
      <!-- Traditional indices — DOW / S&P / NDX / VIX with 1d % + 90d
           sparkline. Moved here from the Crypto tab so macro equity context
           lives alongside the equity-signal grid that follows. -->
      <div id="overviewIndicesWrap" class="card" style="padding:12px 16px;margin-bottom:10px">
        <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:8px">
          <span style="font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.06em">TRADITIONAL INDICES</span>
          <span class="sub" style="font-size:11px;color:var(--muted)">Yahoo · 1d / 5d / 30d</span>
        </div>
        <div id="overviewIndices" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px"></div>
      </div>
      <!-- STOCK SIGNAL SENTIMENT — aggregate signal-score buckets across the
           top-50 most-active stocks (DATA.market.stocks_signals). Mirrors the
           POC sentiment card pattern: net index in [-100,+100] (positive =
           broad buy, negative = broad sell). Rendered by renderStocksSentiment(). -->
      <div class="card" id="stocksSentimentCard" style="padding:14px 16px;margin-bottom:10px;border-left:4px solid #a78bfa">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:6px;flex-wrap:wrap">
          <div>
            <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.06em">📊 STOCK SIGNAL SENTIMENT — TOP 50 MOST ACTIVE</div>
            <div style="font-size:11px;color:var(--muted)" id="stocksSentimentSubline">—</div>
          </div>
          <div style="text-align:right">
            <div id="stocksSentimentScore" style="font-size:28px;font-weight:700;line-height:1">—</div>
            <div id="stocksSentimentLabel" style="font-size:11px;font-weight:700;letter-spacing:.05em">—</div>
          </div>
        </div>
        <div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:#1f2533" id="stocksSentimentBar">
          <div style="background:#22c55e;width:0%" id="stocksSentimentBarBuy"></div>
          <div style="background:#f59e0b;width:0%" id="stocksSentimentBarHold"></div>
          <div style="background:#ef4444;width:0%" id="stocksSentimentBarSell"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">
          <span style="color:#22c55e">↑ <span id="stocksSentimentBuyCount">0</span> BUY+</span>
          <span>· <span id="stocksSentimentHoldCount">0</span> HOLD</span>
          <span style="color:#ef4444">↓ <span id="stocksSentimentSellCount">0</span> SELL+</span>
        </div>
      </div>
      <!-- Signal breadth chart (top of tab, before filter chips) -->
      <div class="chart-card">
        <div class="head">
          <h2>Stock signal breadth — 50 most active <span class="tag">Yahoo</span></h2>
          <span class="desc">Daily count of STRONG BUY / BUY / HOLD / SELL / STRONG SELL across the top-50 most-active US stocks &middot; last 90 days</span>
        </div>
        <div class="chart-wrap" style="height:220px"><canvas id="stocksBreadthChart"></canvas></div>
      </div>
      <div class="chart-card" style="margin-top:12px">
        <div class="head">
          <h2>Stock signals — Top 50 most active <span class="tag">Yahoo</span></h2>
          <span class="desc">Daily-volume leaders on US exchanges &middot; signal score across SMA / RSI / MACD / momentum / volume &middot; grouped by bucket Strong Buy &rarr; Strong Sell</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <span class="lbl" style="margin:0">Filter</span>
          <button class="btn active" data-stocksfilter="all">All</button>
          <button class="btn" data-stocksfilter="strong_buy">STRONG BUY+</button>
          <button class="btn" data-stocksfilter="buy">BUY+</button>
          <button class="btn" data-stocksfilter="hold">HOLD</button>
          <button class="btn" data-stocksfilter="sell">SELL+</button>
          <button class="btn" data-stocksfilter="strong_sell">STRONG SELL</button>
        </div>
        <div id="stocksGrid"></div>
      </div>
    </div>
  </div>

  <!-- ============ LTHCS TAB ============ -->
  <!-- Discoverability gateway for the standalone LTHCS dashboard. Renders
       the same composite-index summary the Stocks tab carries plus an
       Insights row + corner CTA to the full LTHCS dashboard. The full
       dashboard lives at /btc-eth-etf-dashboard/lthcs/ (staged by
       .github/workflows/pages.yml from the committed lthcs_tab/ dir).
       Layout (refined from b18e180):
         Row 1: Insights row + corner "Open full LTHCS →" button
         Row 2: Composite Index card (gauge + larger-font component table)
         Row 3: Gainers / Decliners as colored ticker boxes (Crypto-card model). -->
  <div id="tab-lthcs" class="hidden">
    <div class="container">
      <!-- Composite-index panel — narrative card (Step 1 verdict + Step 2
           components grid + movers). Promoted to the top of the LTHCS tab
           per user feedback so the daily read leads, not the insights row.
           Filled by renderLthcsNarrativePanel(host); the "Open full LTHCS
           dashboard →" CTA is tucked into the card head's top-right corner
           rather than living in a standalone row above (saves vertical
           space). -->
      <div class="chart-card" id="lthcsCompositeCard" style="position:relative;margin-bottom:10px"></div>
      <!-- Insights row — dynamic 3-5 LTHCS insights with a corner CTA.
           Moved below the composite card per user request — the insights
           are supporting context, not the lead-in. Filled by
           renderLthcsInsightsRow(host) from DATA.lthcs.insights. -->
      <div class="card" id="lthcsInsightsRow" style="padding:12px 14px;margin-bottom:10px;border-left:4px solid #a78bfa"></div>
    </div>
  </div>

  <!-- ============ REAL ESTATE TAB ============ -->
  <!-- Gateway for the standalone US Real Estate Markets dashboard. Shows
       headline freshness + a small stat strip computed at tab-activate time
       from /data/real_estate.json (committed daily by real-estate-daily.yml).
       Big CTA opens the full /real-estate/ page with all 10 KPIs, 5Y chart,
       and sortable 50-metro table. -->
  <div id="tab-real_estate" class="hidden">
    <div class="container">
      <div class="chart-card" id="realEstateCard">
        <div class="head">
          <h2>US Real Estate Markets <span class="tag">Zillow &middot; Redfin &middot; FRED</span></h2>
          <span class="desc">National housing heat index &middot; hot &amp; cooling markets &middot; refreshed daily &middot; full view at <a href="real-estate/" style="color:var(--accent)">/real-estate/</a></span>
        </div>
        <div id="realEstateSummary">
          <div class="empty" style="padding:12px 14px">Loading real-estate snapshot&hellip;</div>
        </div>
        <div style="padding:4px 14px 14px 14px">
          <a href="real-estate/" class="btn" style="display:inline-block;padding:10px 16px;border-radius:6px;background:var(--accent);color:#fff;text-decoration:none;font-weight:600">Open Full Dashboard &rarr;</a>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ AI NEWS TAB ============ -->
  <div id="tab-ainews" class="hidden">
    <div class="container">
      <div id="aiNewsEmpty" class="empty hidden">AI news not yet loaded. Run <code>python app.py --fetch-market</code> to populate.</div>
      <div id="aiNewsContent">
        <!-- Top row mirrors the Crypto Overview layout per user request:
             LEFT column stacks AI insights (cap 3) + Top AI news (cap 3);
             RIGHT column carries the AI-exposed stocks ticker grid (moved
             up from the bottom .grid2 row). Stacks 1-col under 860px. -->
        <div id="aiNewsTopRow" style="display:grid;grid-template-columns:1fr 1.2fr;gap:12px;align-items:start">
          <div style="display:flex;flex-direction:column;gap:12px;min-width:0">
            <div class="chart-card" id="aiNewsInsightsCard">
              <div class="head">
                <h2>AI insights <span class="tag">top 3</span></h2>
                <span class="desc">Signal flips, sentiment shifts and notable AI-ticker moves &middot; same source as the top bar, scoped to this tab</span>
              </div>
              <div id="aiNewsInsights" style="display:flex;flex-direction:column;gap:6px"></div>
            </div>
            <div class="chart-card" id="aiNewsTop5Card">
              <div class="head">
                <h2>Top AI news <span class="tag">latest 3</span></h2>
                <span class="desc">Most recent AI/ML/chips headlines &middot; click any row to open the article</span>
              </div>
              <div id="aiNewsTop5"></div>
            </div>
          </div>
          <div style="min-width:0">
            <div class="chart-card" id="aiNewsStocksCard">
              <div class="head">
                <h2>AI-exposed stocks</h2>
                <span class="desc">Signal score for tickers most exposed to AI/ML/chips &middot; filtered from Stocks tab</span>
              </div>
              <div id="aiStocksGrid" class="row" style="grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px"></div>
            </div>
          </div>
        </div>
        <style>
          @media (max-width: 860px) {
            #aiNewsTopRow { grid-template-columns: 1fr !important; }
          }
        </style>

        <!-- AI sentiment summary card (full width, below the top row) -->
        <div class="chart-card" id="aiNewsSummaryCard" style="margin-top:12px">
          <div class="head">
            <h2>AI news sentiment <span class="tag">live</span></h2>
            <span class="desc">Aggregate sentiment across AI/ML/chips coverage &middot; auto-classified POSITIVE / NEUTRAL / NEGATIVE</span>
          </div>
          <div id="aiNewsSummary"></div>
        </div>

        <!-- Quadrant scatter chart — pinned right under sentiment per user
             request (the big visual lead-in before the numbers). -->
        <div class="chart-card" id="aiQuadrantCard" style="margin-top:12px">
          <div class="head">
            <h2>AI funding quadrant <span class="tag">last round &times; valuation</span></h2>
            <span class="desc">X = last round size &middot; Y = total valuation &middot; log scale both axes &middot; each dot is a company (hover for name)</span>
          </div>
          <div class="chart-wrap" style="height:380px"><canvas id="aiQuadrantChart"></canvas></div>
        </div>

        <!-- AI investment KPI strip (curated) -->
        <div class="chart-card" id="aiInvestmentKpisCard" style="margin-top:12px">
          <div class="head">
            <h2>AI investment KPIs <span class="tag">Stanford AI Index &middot; Goldman &middot; McKinsey &middot; Epoch</span></h2>
            <span class="desc">Headline numbers from authoritative published sources &middot; click any card for source link</span>
          </div>
          <div class="row" id="aiInvestmentKpis" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px"></div>
        </div>

        <!-- Top funded AI companies table -->
        <div class="chart-card" id="aiTopFundedCard" style="margin-top:12px">
          <div class="head">
            <h2>Top funded AI companies <span class="tag">curated, public valuations</span></h2>
            <span class="desc">Sorted by latest known valuation &middot; click company for the source URL</span>
          </div>
          <div style="overflow:auto;max-height:420px">
            <table id="aiTopFundedTable" class="tracker-grid">
              <thead><tr>
                <th>Company</th>
                <th style="text-align:right">Valuation</th>
                <th style="text-align:right">Last round</th>
                <th>Stage</th>
                <th>HQ</th>
                <th>Category</th>
              </tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>

        <!-- (AI funding quadrant moved up — sits right under sentiment.) -->

        <!-- SEC EDGAR Form D — most-recent AI private placements -->
        <div class="chart-card" id="aiSecFormDCard" style="margin-top:12px">
          <div class="head">
            <h2>SEC Form D — recent AI private placements <span class="tag" id="aiSecFormDBadge">EDGAR</span></h2>
            <span class="desc">Rule 506(b)/506(c) filings from AI-adjacent issuers in the last 60 days &middot; click any issuer for the EDGAR filing</span>
          </div>
          <div style="overflow:auto;max-height:420px">
            <table id="aiSecFormDTable" class="tracker-grid">
              <thead><tr>
                <th>Issuer</th>
                <th style="text-align:right">Offering</th>
                <th style="text-align:right">Sold</th>
                <th>First sale</th>
                <th>Filed</th>
                <th>Exemption</th>
              </tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>

        <!-- White paper / research KPIs -->
        <div class="chart-card" id="aiWhitepaperKpisCard" style="margin-top:12px">
          <div class="head">
            <h2>Research benchmarks <span class="tag">Stanford AI Index &middot; Epoch &middot; IEA &middot; MLPerf</span></h2>
            <span class="desc">From peer-reviewed and major institutional reports &middot; click any card for source</span>
          </div>
          <div class="row" id="aiWhitepaperKpis" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px"></div>
        </div>

        <!-- Latest AI news feed — full-width now that AI-exposed stocks
             moved up to the top-right slot. -->
        <div class="chart-card" style="margin-top:12px">
          <div class="head">
            <h2>Latest AI news <span class="tag" id="aiNewsHeaderBadge"></span></h2>
            <span class="desc">Top 30 most recent &middot; click any row to open the article</span>
          </div>
          <div id="aiNewsFeed" style="max-height:640px;overflow-y:auto;border:1px solid var(--border);border-radius:6px"></div>
        </div>
        <!-- Bottom left: source breakdown -->
        <div class="chart-card" style="margin-top:12px">
          <div class="head">
            <h2>Source breakdown</h2>
            <span class="desc">Which publications are most positive / negative on AI coverage</span>
          </div>
          <div id="aiNewsSources"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ SIGNALS TAB ============ -->
  <div id="tab-signals" class="hidden">
    <div id="signalsEmpty" class="empty hidden">No signal data — needs price history. Run <code>--fetch-market</code>.</div>
    <div id="signalsContent">
      <!-- CRYPTO SIGNAL SENTIMENT — aggregate signal-score buckets across the
           top-50 by market cap (DATA.signals_top20). Mirrors the POC sentiment
           card pattern: net index in [-100,+100] (positive = broad buy signals,
           negative = broad sell signals). Rendered by renderCryptoSignalsSentiment(). -->
      <div class="card" id="cryptoSignalsSentimentCard" style="padding:14px 16px;margin-bottom:10px;border-left:4px solid #a78bfa">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:6px;flex-wrap:wrap">
          <div>
            <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.06em">📈 CRYPTO SIGNAL SENTIMENT — TOP 50 BY MARKET CAP</div>
            <div style="font-size:11px;color:var(--muted)" id="cryptoSignalsSentimentSubline">—</div>
          </div>
          <div style="text-align:right">
            <div id="cryptoSignalsSentimentScore" style="font-size:28px;font-weight:700;line-height:1">—</div>
            <div id="cryptoSignalsSentimentLabel" style="font-size:11px;font-weight:700;letter-spacing:.05em">—</div>
          </div>
        </div>
        <div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:#1f2533" id="cryptoSignalsSentimentBar">
          <div style="background:#22c55e;width:0%" id="cryptoSignalsSentimentBarBuy"></div>
          <div style="background:#f59e0b;width:0%" id="cryptoSignalsSentimentBarHold"></div>
          <div style="background:#ef4444;width:0%" id="cryptoSignalsSentimentBarSell"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">
          <span style="color:#22c55e">↑ <span id="cryptoSignalsSentimentBuyCount">0</span> BUY+</span>
          <span>· <span id="cryptoSignalsSentimentHoldCount">0</span> HOLD</span>
          <span style="color:#ef4444">↓ <span id="cryptoSignalsSentimentSellCount">0</span> SELL+</span>
        </div>
      </div>
      <!-- Signal breadth chart (top of tab) -->
      <div class="chart-card" style="margin-bottom:14px">
        <div class="head">
          <h2>Crypto signal breadth — top 50 by market cap <span class="tag">CoinGecko</span></h2>
          <span class="desc">Daily count of STRONG BUY / BUY / HOLD / SELL / STRONG SELL across the top-50 by market cap &middot; last 90 days</span>
        </div>
        <div class="chart-wrap" style="height:220px"><canvas id="cryptoSignalsBreadthChart"></canvas></div>
      </div>
      <!-- ============ TOP-50 COMPACT SIGNALS STRIP (moved to top of tab) ============ -->
      <div class="card" style="padding:12px 14px;margin-bottom:14px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px">
          <span class="lbl" style="margin:0">Filter</span>
          <button class="btn active" data-top20filter="all">All</button>
          <button class="btn" data-top20filter="buy">Buy</button>
          <button class="btn" data-top20filter="hold">Hold</button>
          <button class="btn" data-top20filter="sell">Sell</button>
          <span style="flex:1"></span>
          <div class="top25-header-title" style="text-align:right">
            <h2 style="margin:0;font-size:15px">Top 25 by market cap</h2>
            <div class="sub" style="color:var(--muted);font-size:11px">Simplified score from CoinGecko price/volume only · click any card for the full breakdown</div>
          </div>
        </div>
        <div id="top20SignalCards" style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start"></div>
      </div>

      <div class="note"><strong>Composite indicator, not investment advice.</strong> Score is a transparent sum of contributions from price trend (SMA50/200), momentum (RSI, MACD), positioning (funding), sentiment (Fear &amp; Greed), institutional flows (ETF 7d), and volatility (DVOL z-score). Range −100 to +100. Read the components below — that's where the score comes from. Do your own evaluation.</div>
      <div class="grid3" id="signalCards"></div>
      <!-- Per-coin alternating layout: for each of the top 25, render the
           full rich signal card (score + components) followed by a price/
           score history chart. Populated by renderPerCoinSignalList() —
           replaces the legacy hard-coded BTC/ETH/LINK/LTC 4-chart grid. -->
      <div id="perCoinSignalList" style="display:flex;flex-direction:column;gap:14px"></div>
    </div>
  </div>

  <!-- ============ Point of Control TAB ============ -->
  <div id="tab-poc" class="hidden">
    <div class="container">
      <div class="chart-card">
        <div class="head" style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap">
          <div style="min-width:0;flex:1">
            <h2 style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0">
              Point of Control — Top 50 by market cap, sorted by signal score
              <button class="btn" data-poc-help="1" aria-label="What is Point of Control?" title="What is Point of Control?" style="padding:1px 8px;font-size:11px;font-weight:700;line-height:1.4">?</button>
            </h2>
            <span class="desc">Volume-weighted price levels across 30d / 90d / 180d · naked POCs + value-area drift sparkline per coin</span>
          </div>
          <button class="btn" data-poc-help="1" style="font-size:11px;white-space:nowrap">📊 Learn about POC</button>
        </div>
        <!-- Inline "How to read this page" panel removed per user request —
             all of that explainer content lives in the Learn-about-POC modal
             (triggered by the data-poc-help button above + inside the modal).
             Keeps the tab cleaner; users who want the full primer click. -->

        <!-- POC SENTIMENT INDEX — aggregate across the top 25 by signal score.
             Computes UP / DOWN / FLAT migration counts + a net index in
             [-100,+100] (positive = broad accumulation, negative = broad
             distribution). Rendered by renderPocSentimentIndex(). -->
        <div class="card" id="pocSentimentCard" style="padding:14px 16px;margin-bottom:10px;border-left:4px solid #a78bfa">
          <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:6px;flex-wrap:wrap">
            <div>
              <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.06em">🐋 POC SENTIMENT — TOP 50 BY MARKET CAP</div>
              <div style="font-size:11px;color:var(--muted)" id="pocSentimentSubline">—</div>
            </div>
            <div style="text-align:right">
              <div id="pocSentimentScore" style="font-size:28px;font-weight:700;line-height:1">—</div>
              <div id="pocSentimentLabel" style="font-size:11px;font-weight:700;letter-spacing:.05em">—</div>
            </div>
          </div>
          <div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:#1f2533" id="pocSentimentBar">
            <div style="background:#22c55e;width:0%" id="pocSentimentBarUp"></div>
            <div style="background:#94a3b8;width:0%" id="pocSentimentBarFlat"></div>
            <div style="background:#ef4444;width:0%" id="pocSentimentBarDown"></div>
          </div>
          <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">
            <span style="color:#22c55e">↑ <span id="pocSentimentUpCount">0</span> UP</span>
            <span>· <span id="pocSentimentFlatCount">0</span> FLAT</span>
            <span style="color:#ef4444">↓ <span id="pocSentimentDownCount">0</span> DOWN</span>
          </div>
        </div>

        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <span class="lbl" style="margin:0">Filter</span>
          <button class="btn active" data-pocfilter="all">All</button>
          <button class="btn" data-pocfilter="strong_buy">STRONG BUY+</button>
          <button class="btn" data-pocfilter="buy">BUY+</button>
          <button class="btn" data-pocfilter="hold">HOLD</button>
          <button class="btn" data-pocfilter="sell">SELL+</button>
          <button class="btn" data-pocfilter="strong_sell">STRONG SELL</button>
        </div>
        <!-- Featured row — top 4 by signal score, evenly spread (1×4 on
             desktop, 2×2 on tablet, 1-up on phone). Rendered by
             renderPocTopCards into #pocFeaturedRow; everything else fills
             #pocTopGrid below at the standard compact size. -->
        <div id="pocFeaturedRow" class="row" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-bottom:12px"></div>
        <div id="pocTopGrid" class="row" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px"></div>
      </div>
    </div>
  </div>

  <!-- ============ DeFi TAB ============ -->
  <div id="tab-defi" class="hidden">
    <!-- Loading state shown while the lazy-loaded /data-defi.json sidecar
         is in-flight (see SIDECAR_FOR_TAB.defi). Toggled by renderAll
         based on SIDECAR_STATE.defi — hidden on first paint when defi is
         either inlined or already cached. -->
    <div id="defiLoading" class="hidden" style="text-align:center;padding:32px;color:var(--muted);font-size:13px">Loading DeFi data…</div>
    <!-- Tab body is hidden by renderAll while #defiLoading is shown, so the
         placeholder "—" KPIs don't flash before the lazy sidecar lands. -->
    <div id="defiContent">
    <!-- DEFI SENTIMENT — composite of TVL-weighted 7d chain momentum and
         stablecoin mcap 7d change. Rendered by renderDefiSentiment(). -->
    <div class="card" id="defiSentimentCard" style="padding:14px 16px;margin-bottom:10px;border-left:4px solid #a78bfa">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;margin-bottom:6px;flex-wrap:wrap">
        <div>
          <div style="font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.06em">🌊 DEFI SENTIMENT</div>
          <div style="font-size:11px;color:var(--muted)" id="defiSentimentSubline">—</div>
        </div>
        <div style="text-align:right">
          <div id="defiSentimentScore" style="font-size:28px;font-weight:700;line-height:1">—</div>
          <div id="defiSentimentLabel" style="font-size:11px;font-weight:700;letter-spacing:.05em">—</div>
        </div>
      </div>
      <div style="display:flex;height:10px;border-radius:5px;overflow:hidden;background:#1f2533">
        <div style="background:#22c55e;width:0%" id="defiSentimentBarPos"></div>
        <div style="background:#94a3b8;width:0%" id="defiSentimentBarNeu"></div>
        <div style="background:#ef4444;width:0%" id="defiSentimentBarNeg"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">
        <span style="color:#22c55e">EXPANSION</span>
        <span>STABLE</span>
        <span style="color:#ef4444">CONTRACTION</span>
      </div>
    </div>
    <!-- 4-card KPI strip (mirrors Whale tab pattern) -->
    <div class="row" id="defiKpis"></div>
    <!-- Per-chain selector (mirrors Whale BTC/ETH toggle). Default Ethereum;
         persists to localStorage.defiChain. -->
    <div class="card" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px;margin-bottom:10px">
      <span class="lbl" style="margin:0">Chain</span>
      <button class="btn active" data-defichain="Ethereum">Ethereum</button>
      <button class="btn" data-defichain="Solana">Solana</button>
      <button class="btn" data-defichain="Arbitrum">Arbitrum</button>
      <button class="btn" data-defichain="Base">Base</button>
    </div>
    <!-- Per-chain content area: summary + TVL history + top protocols -->
    <div class="row" id="defiChainSummary" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-bottom:10px"></div>
    <div class="grid2">
      <div class="chart-card">
        <div class="head">
          <h2><span id="defiTvlHistoryTitle">Ethereum</span> TVL history</h2>
          <span class="desc">Last 365 days · selected chain</span>
        </div>
        <div class="chart-wrap"><canvas id="defiTvlHistoryChart"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="head">
          <h2>Top protocols on <span id="defiTopProtoTitle">Ethereum</span></h2>
          <span class="desc">Top 10 by TVL · multi-chain protocols filtered to selected chain</span>
        </div>
        <div style="max-height:380px;overflow:auto">
          <table id="defiChainProtocolsTable">
            <thead><tr><th>#</th><th>Protocol</th><th>Category</th><th>TVL</th><th>1d</th><th>7d</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>
    <!-- Global tables: protocols (top 15) + yields -->
    <div class="grid2">
      <div class="chart-card">
        <div class="head"><h2>Top 15 DeFi protocols (global)</h2><span class="desc">All chains · by TVL · 1d/7d/30d %</span></div>
        <div style="max-height:380px;overflow:auto">
          <table id="defiProtocolsTable">
            <thead><tr><th>#</th><th>Protocol</th><th>Category</th><th>TVL</th><th>1d</th><th>7d</th><th>30d</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
      <div class="chart-card">
        <div class="head"><h2>Top stablecoin yields</h2><span class="desc">Sorted by TVL, ≥$5M</span></div>
        <div style="max-height:380px;overflow:auto">
          <table id="defiYieldsTable">
            <thead><tr><th>Pool</th><th>Chain</th><th>TVL</th><th>APY</th></tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
    </div>
    <!-- Optional: bridges (only renders if data present) -->
    <div class="chart-card hidden" id="defiBridgesCard">
      <div class="head"><h2>Top bridges by volume</h2><span class="desc">DefiLlama bridge volume</span></div>
      <div style="max-height:300px;overflow:auto">
        <table id="defiBridgesTable">
          <thead><tr><th>#</th><th>Bridge</th><th>24h volume</th><th>7d volume</th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
    </div><!-- /defiContent -->
  </div>

  <!-- ============ RESEARCH TAB (one-stop consolidated info page) ============ -->
  <div id="tab-social" class="hidden">
    <!-- ===== Per-coin news sentiment for the top 25 by market cap. Sourced
         from DATA.market.news (crypto_news_rss, all 5 free feeds) — items are
         keyword-matched to coin name/symbol on the client and scored
         POSITIVE/NEGATIVE/NEUTRAL via the same word-list approach we use for
         the AI news sentiment. Mobile-responsive grid (single column ≤480px). ===== -->
    <div class="chart-card" style="padding:12px 16px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">News sentiment — Top 25 by market cap <span class="tag">RSS</span></h2>
        <div class="sub" style="font-size:11px;color:var(--muted);margin-top:2px">Click any row for the full headline breakdown</div>
        <span class="desc">Per-coin mention counts + POSITIVE / NEGATIVE / NEUTRAL split, text-matched against the latest headlines (CoinDesk · Cointelegraph · Decrypt · The Block · Bitcoin Magazine)</span>
      </div>
      <div id="topNewsSentimentCards" class="top-news-sentiment-grid"></div>
    </div>
    <div class="chart-card">
      <div class="head">
        <h2>Top crypto news</h2>
        <span class="desc">latest headlines · CoinDesk · Cointelegraph · Decrypt · The Block · Bitcoin Magazine</span>
      </div>
      <div id="researchNewsHost"></div>
    </div>
    <div id="socialEmpty" class="empty hidden">
      No research data yet — all free sources (Reddit, CryptoCompare, Santiment) returned empty.
      Refresh or wait for the next hourly cron.
    </div>
    <div id="socialContent">
      <div class="sub" id="socialAsOf" style="margin-bottom:6px"></div>
      <div class="note">
        <strong>Research</strong> — one consolidated page for free social, dev, on-chain, news,
        and technical signals. Sources: Reddit (subscribers + top posts; cloud-IP-blocked, local-only),
        CryptoCompare social (legacy endpoint now auth-gated, may be empty),
        CryptoCompare news sentiment (keyless, POSITIVE/NEGATIVE/NEUTRAL labels),
        Santiment (daily-active addresses + dev activity, refreshed once a day at 00:00 UTC),
        and Point of Control (volume profile derived from existing price+volume series).
      </div>

      <!-- ===== CryptoCompare social + dev stats ===== -->
      <div class="chart-card" style="padding:12px 16px">
        <div class="head">
          <h2 style="margin:0;font-size:15px">Social + developer stats <span class="tag">CryptoCompare</span></h2>
          <span class="desc">Twitter followers · Reddit subscribers · GitHub stars / forks / open PRs</span>
        </div>
        <div class="row" id="ccSocialCards" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))"></div>
      </div>

      <!-- ===== Reddit pulse ===== -->
      <div class="chart-card" style="padding:12px 16px">
        <div class="head">
          <h2 style="margin:0;font-size:15px">Reddit pulse <span class="tag">/r/crypto subs</span></h2>
          <span class="desc">Subscribers · active users now · top 24h posts (Reddit blocks cloud IPs — local-only on the public mirror)</span>
        </div>
        <div class="row" id="redditCards" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))"></div>
      </div>

      <!-- (CC news sentiment moved to the top of the tab, outside socialContent) -->

      <!-- ===== Santiment on-chain + dev ===== -->
      <div class="chart-card" style="padding:12px 16px">
        <div class="head">
          <h2 style="margin:0;font-size:15px">On-chain &amp; dev activity <span class="tag">Santiment</span></h2>
          <span class="desc">Daily-active addresses · dev activity (7d) · refreshed once daily</span>
        </div>
        <div class="row" id="santimentCards" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))"></div>
      </div>

    </div>
  </div>

  <!-- ============ WHALE TAB ============ -->
  <div id="tab-whale" class="hidden">
    <div id="whaleEmpty" class="empty hidden">No whale data. Run <code>python app.py --fetch-market</code>.</div>
    <div id="whaleContent">
      <div class="sub" id="whaleAsOf" style="margin-bottom:6px"></div>
      <!-- Asset toggle: BTC (default) or ETH. Each panel below renders the
           selected asset's whale view; the other panel stays hidden. -->
      <div class="card" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px;margin-bottom:10px">
        <span class="lbl" style="margin:0">View</span>
        <button class="btn active" data-whaleasset="btc">BTC</button>
        <button class="btn" data-whaleasset="eth">ETH</button>
      </div>
      <!-- ===== BTC PANEL (default) ===== -->
      <div id="whaleBtcPanel">
      <div class="note">Free BTC on-chain proxies (blockchain.info + bitinfocharts cohorts). Glassnode-level metrics (true exchange flows, SOPR) require paid feed.</div>
      <!-- Headline: Whale Sentiment Index (composite ±100 from on-chain proxies) -->
      <div class="chart-card" id="whaleSentimentCard" style="position:relative"></div>
      <div class="row" id="whaleKpis"></div>
      <div class="chart-card">
        <div class="head">
          <h2>Whale Activity Tracker</h2>
          <span class="desc">snapshot across multiple time horizons</span>
        </div>
        <div style="overflow:auto">
          <table id="whaleTrackerTable" class="tracker-grid">
            <thead><tr>
              <th>Metric</th><th>Today</th><th>1d Δ</th><th>7d Δ</th><th>30d Δ</th><th>90d Δ</th>
            </tr></thead>
            <tbody></tbody>
          </table>
        </div>
      </div>
      <!-- Recent Whale Transactions: vouts ≥ $1M from the latest confirmed block -->
      <div class="chart-card hidden" id="whaleAlertsCard">
        <div class="head">
          <div>
            <h2>Recent Whale Transactions <span class="tag">mempool.space</span></h2>
            <span class="desc" id="whaleAlertsNote">—</span>
          </div>
        </div>
        <div style="overflow:auto">
          <table class="tracker-grid">
            <thead><tr>
              <th>Block</th><th style="text-align:right">USD value</th><th style="text-align:right">BTC</th><th>txid</th>
            </tr></thead>
            <tbody id="whaleAlertsBody"></tbody>
          </table>
        </div>
      </div>
      <!-- Whale vs non-whale supply held (real cohort data from bitinfocharts) -->
      <div class="chart-card">
        <div class="head" style="flex-wrap:wrap;gap:12px">
          <div>
            <h2>BTC supply: whales vs non-whales <span class="tag">bitinfocharts</span></h2>
            <span class="desc">Stacked: addresses with ≥1,000 BTC (whales) vs everyone else &middot; ~5y daily history binned to your selection</span>
          </div>
          <div class="controls" id="cohortBins" style="border:0;padding:0;margin:0;gap:4px">
            <span class="lbl" style="margin:0">Bin</span>
            <button class="btn" data-cohortbin="week">Weekly</button>
            <button class="btn active" data-cohortbin="month">Monthly</button>
            <button class="btn" data-cohortbin="quarter">Quarterly</button>
            <button class="btn" data-cohortbin="year">Yearly</button>
          </div>
        </div>
        <div class="chart-wrap tall"><canvas id="whaleCohortChart"></canvas></div>
        <div class="row" id="whaleCohortKpis" style="margin-top:10px"></div>
      </div>
      <!-- Whale activity proxy: BTC volume + avg tx size combined view -->
      <div class="chart-card">
        <div class="head">
          <h2>Whale activity proxy <span class="tag">FREE</span></h2>
          <span class="desc">Daily BTC moved on-chain (left axis) &middot; avg tx size USD (right axis) &middot; both rising together = whale-shaped activity</span>
        </div>
        <div class="chart-wrap tall"><canvas id="whaleProxyChart"></canvas></div>
        <div class="note" style="margin-top:10px;font-size:11px">
          ⚠️ Best free <em>flow</em> proxy. True whale-cohort flow split (volume by ≥1,000 BTC transactions) needs Glassnode Studio Lite (~$30/mo). If you sign up, paste the key and I'll wire the cohort-flow chart.
        </div>
      </div>
      <!-- BTC network state additions -->
      <div class="grid2">
        <div class="card" style="padding:12px 14px">
          <h3>Difficulty adjustment</h3>
          <div id="diffAdjBox" class="sub" style="font-size:12px;color:var(--muted);line-height:1.5"></div>
        </div>
        <div class="card" style="padding:12px 14px">
          <h3>Lightning Network</h3>
          <div id="lightningBox" class="sub" style="font-size:12px;color:var(--muted);line-height:1.5"></div>
        </div>
      </div>
      <div class="chart-card">
        <div class="head"><h2>Mining pool concentration <span class="tag">mempool.space</span></h2><span class="desc">Hashrate share by pool (1y window) &middot; top 2 = <span id="poolsTop2">?</span></span></div>
        <div class="chart-wrap tall"><canvas id="miningPoolsChart"></canvas></div>
      </div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>Avg. transaction value (USD)</h2><span class="desc">tx_volume_usd / tx_count &middot; rising = whales moving more per tx</span></div>
          <div class="chart-wrap"><canvas id="avgTxChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>On-chain transaction value (USD)</h2><span class="desc">Daily estimated USD value moved</span></div>
          <div class="chart-wrap"><canvas id="txVolChart"></canvas></div>
        </div>
      </div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>Active addresses</h2><span class="desc">Unique addresses used per day</span></div>
          <div class="chart-wrap"><canvas id="addrChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Hash rate (TH/s)</h2><span class="desc">Miner commitment, log scale</span></div>
          <div class="chart-wrap"><canvas id="hashChart"></canvas></div>
        </div>
      </div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>Miners revenue (USD)</h2><span class="desc">Block reward + fees</span></div>
          <div class="chart-wrap"><canvas id="minerChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>On-chain output volume (BTC)</h2><span class="desc">Total BTC moved per day</span></div>
          <div class="chart-wrap"><canvas id="outputChart"></canvas></div>
        </div>
      </div>
      <div class="chart-card hidden" id="multichainWhaleCard">
        <div class="head">
          <h2>Multi-chain whale snapshot <span class="tag">Blockchair</span></h2>
          <span class="desc">24h network stats + largest single tx · LTC / BCH / DOGE</span>
        </div>
        <div id="multichainWhaleGrid" class="row" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px"></div>
      </div>
      </div> <!-- /whaleBtcPanel -->
      <!-- ===== ETH PANEL ===== -->
      <div id="whaleEthPanel" class="hidden">
        <div class="note">ETH whale view: Blockchair (24h tx, largest tx, supply) + Coin Metrics Community (active addresses, transfer volume). True ETH whale cohorts (≥10K ETH addresses) require a paid feed.</div>
        <!-- Headline: ETH Whale Sentiment Index (composite ±100 from on-chain proxies) -->
        <div class="chart-card" id="whaleEthSentimentCard" style="position:relative"></div>
        <div class="row" id="whaleEthKpis"></div>
        <!-- Recent ETH whale tx feed — promoted directly after Sentiment+KPIs
             to mirror the BTC panel ordering. Hidden until data arrives. -->
        <div class="chart-card hidden" id="ethWhaleAlertsCard">
          <div class="head">
            <h2>Recent ETH whale transactions <span class="tag">Blockchair</span></h2>
            <span class="desc" id="ethWhaleAlertsNote">—</span>
          </div>
          <div style="overflow:auto">
            <table class="tracker-grid">
              <thead><tr>
                <th>Hash</th><th style="text-align:right">ETH</th><th style="text-align:right">USD value</th><th>Time</th>
              </tr></thead>
              <tbody id="ethWhaleAlertsBody"></tbody>
            </table>
          </div>
        </div>
        <!-- ETH Whale Activity Tracker — multi-horizon delta table. Mirrors
             the BTC panel's whaleTrackerTable but reads CM + Blockchair +
             Etherscan series available on the ETH side. Hidden when no row
             has any data. -->
        <div class="chart-card hidden" id="ethWhaleTrackerCard">
          <div class="head">
            <h2>ETH Whale Activity Tracker</h2>
            <span class="desc">snapshot across multiple time horizons (Coin Metrics + Etherscan)</span>
          </div>
          <div style="overflow:auto">
            <table id="ethWhaleTrackerTable" class="tracker-grid">
              <thead><tr>
                <th>Metric</th><th>Today</th><th>1d Δ</th><th>7d Δ</th><th>30d Δ</th><th>90d Δ</th>
              </tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        <!-- ETH whale activity proxy: combined two-axis chart of daily
             transactions + active addresses. Both rising together = whale-
             shaped activity (more txs per active wallet). ETH parallel of the
             BTC whaleProxyChart card. -->
        <div class="chart-card hidden" id="ethWhaleProxyCard">
          <div class="head">
            <h2>ETH whale activity proxy <span class="tag">FREE</span></h2>
            <span class="desc">Daily transactions (left axis) &middot; active addresses (right axis) &middot; Coin Metrics community tier &middot; both rising = whale-shaped activity</span>
          </div>
          <div class="chart-wrap tall"><canvas id="ethWhaleProxyChart"></canvas></div>
          <div class="note" style="margin-top:10px;font-size:11px">
            ⚠️ Best free <em>activity</em> proxy. True ETH cohort flow (volume by ≥10K ETH wallets) requires Glassnode / Nansen — both paid.
          </div>
        </div>
        <div class="chart-card">
          <div class="head">
            <h2>Largest ETH transaction (last 24h) <span class="tag">Blockchair</span></h2>
            <span class="desc">Single biggest tx by USD value on Ethereum mainnet over the past 24 hours</span>
          </div>
          <div id="ethLargestTxBox" class="sub" style="font-size:13px;color:var(--text);line-height:1.6;padding:6px 4px"></div>
        </div>
        <div class="grid2">
          <div class="chart-card">
            <div class="head"><h2>ETH active addresses</h2><span class="desc">Unique addresses used per day (Coin Metrics)</span></div>
            <div class="chart-wrap"><canvas id="ethActiveAddrChart"></canvas></div>
          </div>
          <div class="chart-card">
            <div class="head"><h2>ETH 24h trading volume (CoinGecko)</h2><span class="desc">Exchange-traded volume in USD — on-chain transfer volume requires a paid feed</span></div>
            <div class="chart-wrap"><canvas id="ethTxVolChart"></canvas></div>
          </div>
        </div>
        <div class="grid2">
          <div class="chart-card">
            <div class="head"><h2>ETH transactions per day</h2><span class="desc">Network throughput (Coin Metrics)</span></div>
            <div class="chart-wrap"><canvas id="ethTxCountChart"></canvas></div>
          </div>
          <div class="chart-card">
            <div class="head"><h2>ETH circulating supply</h2><span class="desc">Post-Merge supply has trended flat-to-deflationary (Coin Metrics)</span></div>
            <div class="chart-wrap"><canvas id="ethSupplyChart"></canvas></div>
          </div>
        </div>
        <div class="grid2">
          <div class="card" style="padding:12px 14px">
            <h3>ETH gas (gwei)</h3>
            <div id="ethGasBox" class="sub" style="font-size:12px;color:var(--muted);line-height:1.6"></div>
          </div>
          <div class="card" style="padding:12px 14px">
            <h3>ETH 24h network stats</h3>
            <div id="ethStatsBox" class="sub" style="font-size:12px;color:var(--muted);line-height:1.6"></div>
          </div>
        </div>
        <div class="chart-card">
          <div class="head">
            <h2>ETH blocks mined per day (90d) <span class="tag">Etherscan</span></h2>
            <span class="desc">Daily on-chain throughput proxy — block count between midnight-UTC checkpoints. ~7,200/day saturates the 12s slot target post-Merge.</span>
          </div>
          <div id="ethEtherscanDailyNoKey" class="sub hidden" style="font-size:12px;color:var(--muted);padding:6px 4px"></div>
          <div class="chart-wrap"><canvas id="ethEtherscanDailyChart"></canvas></div>
        </div>
      </div> <!-- /whaleEthPanel -->
    </div>
  </div>

  <!-- ============ CPI TAB (ported from V2) ============ -->
  <!-- 22 FRED Consumer Price Index series across 7 categories (Headlines /
       Food / Energy / Housing / Cars / Healthcare / Other). Sidecar is
       data-cpi.json — dual-written by fetch_cpi.py to v2/data-cpi.json
       AND data-cpi.json. Loads lazily via SIDECAR_FOR_TAB.cpi the first
       time the user opens this tab. When FRED_API_KEY is unset the
       payload arrives with fred_available=false and we render an
       empty-state explainer instead of an empty grid. -->
  <div id="tab-cpi" class="hidden">
    <div id="cpiLoading" class="hidden empty">Loading CPI data&hellip;</div>
    <div id="cpiContent">
      <div id="cpiEmpty" class="chart-card" style="margin-bottom:12px">
        <div class="head">
          <h2>Consumer Price Index <span class="tag">FRED</span></h2>
          <span class="desc" id="cpiEmptySub">CPI data unavailable — FRED_API_KEY not set on the server.</span>
        </div>
        <div class="empty" style="padding:24px 12px">
          Add <code>FRED_API_KEY=&lt;your_key&gt;</code> to <code>.env</code> and trigger a new deploy.
          Get a free key at <a href="https://fred.stlouisfed.org/docs/api/api_key.html" target="_blank" rel="noreferrer" style="color:#a78bfa">fred.stlouisfed.org/docs/api/api_key.html &#8599;</a>.
        </div>
      </div>
      <div id="cpiBody" class="hidden">
        <div class="chart-card" style="margin-bottom:12px">
          <div class="head">
            <h2>Consumer Price Index <span class="tag" id="cpiChip">FRED</span></h2>
            <span class="desc" id="cpiAsOf">Source: FRED</span>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:6px 0 4px">
            <span class="lbl" style="margin:0">View</span>
            <button class="btn active" data-cpiview="index100" type="button">Index = 100</button>
            <button class="btn" data-cpiview="absolute" type="button">Absolute</button>
            <button class="btn" data-cpiview="pctchange" type="button">% change</button>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="lbl" style="margin:0">Range</span>
            <button class="btn" data-cpirange="5y" type="button">5y</button>
            <button class="btn" data-cpirange="10y" type="button">10y</button>
            <button class="btn" data-cpirange="20y" type="button">20y</button>
            <button class="btn" data-cpirange="30y" type="button">30y</button>
            <button class="btn active" data-cpirange="all" type="button">All</button>
          </div>
        </div>
        <div id="cpiCategories"></div>
      </div>
    </div>
  </div>

  <!-- ============ SUPPLIES TAB (ported from V2) ============ -->
  <!-- Macro / logistics indicators: monthly TEU at the Port of Los Angeles
       (total + loaded imports as a leading-indicator subset), FRED
       inventory-to-sales ratio (ISRATIO), and NY Fed Global Supply Chain
       Pressure Index. Sidecar is data-supplies.json — produced by
       fetch_supplies.py. Inline-SVG charts only — no Chart.js dependency
       on this tab (mirrors V2's approach so the tab stays lightweight). -->
  <div id="tab-supplies" class="hidden">
    <div id="suppliesLoading" class="hidden empty">Loading global supplies&hellip;</div>
    <div id="suppliesContent">
      <div class="supplies-snapshot" id="suppliesSnapshot"></div>
      <div class="supplies-grid">
        <div class="chart-card" id="suppliesCardPorts">
          <div class="head">
            <h2>U.S. port container throughput <span class="tag" id="suppliesPortsAsOf">&mdash;</span></h2>
            <span class="desc">Monthly TEU &middot; Port of Los Angeles &middot; total vs loaded imports</span>
          </div>
          <div id="suppliesPortsChart"></div>
          <div class="sub" id="suppliesPortsFoot" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
        </div>
        <div class="chart-card" id="suppliesCardInv">
          <div class="head">
            <h2>Inventory-to-sales ratio <span class="tag" id="suppliesInvAsOf">&mdash;</span></h2>
            <span class="desc">Total business &middot; FRED series ISRATIO &middot; last 10y</span>
          </div>
          <div id="suppliesInvChart"></div>
          <div class="sub" id="suppliesInvFoot" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
        </div>
      </div>
      <div class="chart-card" id="suppliesCardGscpi" style="margin-top:14px">
        <div class="head">
          <h2>NY Fed Global Supply Chain Pressure Index <span class="tag" id="suppliesGscpiAsOf">&mdash;</span></h2>
          <span class="desc">Standard deviations from historical mean &middot; positive = pressure above average &middot; last 10y</span>
        </div>
        <div id="suppliesGscpiChart"></div>
        <div class="sub" id="suppliesGscpiFoot" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
      </div>
    </div>
  </div>

  <!-- ============ METALS TAB (ported from V2) ============ -->
  <!-- Gold + silver spot prices (5y daily lines from FRED + Yahoo),
       central-bank gold holdings (top 20 — IMF IRFCL), and world gold +
       silver mine production by country (USGS MCS). Strength KPI band
       at the top is derived from the same price series (gold/silver
       ratio, period returns, 52-week position) — no extra data. Sidecar
       is data-metals.json — produced by fetch_metals.py. -->
  <div id="tab-metals" class="hidden">
    <div id="metalsLoading" class="hidden empty">Loading gold &amp; silver data&hellip;</div>
    <div id="metalsContent">
      <div class="chart-card" id="metalsStrength" style="margin-bottom:14px">
        <div class="head">
          <h2>Gold &amp; silver &mdash; strength snapshot <span class="tag" id="metalsStrengthAsOf">&mdash;</span></h2>
          <span class="desc" id="metalsStrengthSub">Derived from the daily-close series below</span>
        </div>
        <div id="metalsStrengthBody"></div>
      </div>
      <div class="metals-grid2" id="metalsGrid">
        <div class="chart-card" id="metalsGoldPriceCard">
          <div class="head">
            <h2>Gold spot price <span class="tag" id="metalsGoldAsOf">&mdash;</span></h2>
            <span class="desc" id="metalsGoldSub">USD per troy ounce &middot; 5y daily</span>
          </div>
          <div id="metalsGoldBody"></div>
        </div>
        <div class="chart-card" id="metalsSilverPriceCard">
          <div class="head">
            <h2>Silver spot price <span class="tag" id="metalsSilverAsOf">&mdash;</span></h2>
            <span class="desc" id="metalsSilverSub">USD per troy ounce &middot; 5y daily</span>
          </div>
          <div id="metalsSilverBody"></div>
        </div>
        <div class="chart-card" id="metalsGoldProdCard">
          <div class="head">
            <h2>Gold mine production by country <span class="tag" id="metalsGoldProdAsOf">&mdash;</span></h2>
            <span class="desc" id="metalsGoldProdSub">Metric tons &middot; USGS MCS</span>
          </div>
          <div id="metalsGoldProdBody"></div>
        </div>
        <div class="chart-card" id="metalsSilverProdCard">
          <div class="head">
            <h2>Silver mine production by country <span class="tag" id="metalsSilverProdAsOf">&mdash;</span></h2>
            <span class="desc" id="metalsSilverProdSub">Metric tons &middot; USGS MCS</span>
          </div>
          <div id="metalsSilverProdBody"></div>
        </div>
        <div class="chart-card" id="metalsCBGoldCard">
          <div class="head">
            <h2>Central-bank gold holdings (top 20) <span class="tag" id="metalsCBGoldAsOf">&mdash;</span></h2>
            <span class="desc" id="metalsCBGoldSub">Tonnes &middot; latest IMF IRFCL report</span>
          </div>
          <div id="metalsCBGoldBody"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ TRAVEL ADVISORIES TAB ============ -->
  <!-- U.S. State Dept travel advisory levels (1-4) for ~225 destinations,
       latest bulletins feed, and per-level + terrorism filtering. Data is
       lazy-loaded from /data-travel.json via the SIDECARS mechanism (the
       sidecar itself is written by fetch_advisories.py during V1 build).
       The sub-view selector (overview / level1 / level2 / level34 / terror)
       lives in state.travelSub; selectTab('travel') resets it to 'overview'.
       Ported from V2 but uses V1 tokens (.chart-card, var(--panel), etc.)
       so it sits visually alongside CPI / Supplies / Metals. -->
  <div id="tab-travel" class="hidden">
    <div id="travelLoading" class="hidden empty">Loading travel advisories&hellip;</div>
    <div id="travelContent">
      <!-- Sub-view tab strip (Overview / L1 / L2 / L3&4 / Terrorism). These
           are INTERNAL sub-tabs — not new top-level tabs. -->
      <div class="travel-subtabs" id="travelSubtabs" role="tablist" aria-label="Travel advisory sub-views">
        <button class="travel-subtab active" data-travelsub="overview" role="tab" aria-selected="true">Overview</button>
        <button class="travel-subtab" data-travelsub="level1" role="tab" aria-selected="false"><span class="travel-dot travel-dot--l1"></span>Level 1</button>
        <button class="travel-subtab" data-travelsub="level2" role="tab" aria-selected="false"><span class="travel-dot travel-dot--l2"></span>Level 2</button>
        <button class="travel-subtab" data-travelsub="level34" role="tab" aria-selected="false"><span class="travel-dot travel-dot--l4"></span>Level 3 &amp; 4</button>
        <button class="travel-subtab" data-travelsub="terror" role="tab" aria-selected="false"><span class="travel-dot travel-dot--terror"></span>Terrorism</button>
      </div>

      <!-- Overview view: stat cards, bulletins, distribution, recent updates -->
      <div id="travelOverview" class="travel-view">
        <!-- Stat cards row — clickable; each L1-L4 card navigates to its level
             sub-view, Terrorism card navigates to the Terrorism sub-view. -->
        <div class="row" id="travelStatCards" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px"></div>

        <!-- Latest Bulletins feed + Consular Affairs contact bar -->
        <div class="chart-card" id="travelBulletinsCard" style="margin-bottom:14px">
          <div class="head">
            <h2>Latest bulletins <span class="travel-asof-chip" id="travelBulletinsAsOf">&bull; Updated &mdash;</span></h2>
            <span class="desc">Worldwide / regional alerts + recent advisory reissues &middot; newest first</span>
          </div>
          <div id="travelBulletinsList" style="display:grid;gap:8px"></div>
          <div class="travel-contact-bar">
            <span class="travel-contact-label">Consular Affairs 24/7</span>
            <span>+1-888-407-4747 (U.S./Canada)</span>
            <span class="travel-contact-sep">&middot;</span>
            <span>+1-202-501-4444 (abroad)</span>
            <span class="travel-contact-sep">&middot;</span>
            <a href="https://step.state.gov" target="_blank" rel="noreferrer" class="travel-contact-link">Enroll in STEP &#8599;</a>
          </div>
        </div>

        <div class="grid2">
          <!-- Advisory level distribution bars -->
          <div class="chart-card">
            <div class="head">
              <h2>Advisory distribution</h2>
              <span class="desc">Destination count per advisory level</span>
            </div>
            <div id="travelDistribution"></div>
            <div class="sub" id="travelDistributionFoot" style="margin-top:8px"></div>
          </div>
          <!-- Recently-updated countries feed (8 most recent) -->
          <div class="chart-card">
            <div class="head">
              <h2>Latest updates</h2>
              <span class="desc">8 most-recently reissued advisories</span>
            </div>
            <ul id="travelRecentList" class="travel-recent"></ul>
          </div>
        </div>
      </div>

      <!-- List view: shared shell for Level 1 / Level 2 / Level 3&4 / Terrorism.
           Controls bar (search, terrorism toggle, L3/L4 segmented when
           applicable, sort) + a count line + the country card grid. -->
      <div id="travelList" class="travel-view hidden">
        <div class="travel-controls">
          <input id="travelSearch" type="text" placeholder="Search destinations&hellip;" class="travel-search" aria-label="Search destinations">
          <div class="travel-segment hidden" id="travelLevelSegment" role="tablist" aria-label="Level 3 / 4 filter">
            <button class="travel-seg-btn active" data-travelsublevel="all">All 3 &amp; 4</button>
            <button class="travel-seg-btn" data-travelsublevel="3">Level 3</button>
            <button class="travel-seg-btn" data-travelsublevel="4">Level 4</button>
          </div>
          <button id="travelTerrorToggle" class="travel-toggle" type="button">&#9888; Terrorism-flagged only</button>
          <select id="travelSort" class="travel-select" aria-label="Sort destinations">
            <option value="level">Sort: Level (high&rarr;low)</option>
            <option value="name">Sort: Name (A&rarr;Z)</option>
            <option value="date">Sort: Recently updated</option>
          </select>
          <div class="travel-count" id="travelCount"></div>
        </div>
        <div id="travelGrid" class="travel-grid"></div>
        <div id="travelEmpty" class="travel-empty hidden">
          No destinations match your filters. Try clearing the search or terrorism-only toggle.
        </div>
      </div>
    </div><!-- /travelContent -->
  </div><!-- /tab-travel -->

  <!-- ============================================================
       UAP / MUFON tab (ported from V2 — uses V1 tokens + a small
       .v2-card* CSS shim). Five sections:
         A. Latest Updates — curated, baked-in (PURSUE drops + AARO).
         B. Document Library — curated external links to .gov archives.
         C. Sightings trend signal — derived client-side from data-mufon.json.
         D. US Sightings Map — NUFORC eyewitness data via the data-mufon.json
            sidecar (lazy-loaded; the renderer is a no-op until it lands).
         E. Sightings by classification — stacked-area shapes chart.
       Map uses a state-tile grid (50 + DC) instead of true polygons — under
       1KB inline vs. ~30KB+ for topojson.
       ============================================================ -->
  <div id="tab-mufon" class="hidden">
    <div class="container">

    <!-- Section A: Latest Updates -->
    <div class="v2-card" id="mufonUpdatesCard" style="margin-bottom:14px">
      <div class="v2-card__head">
        <div><h2 class="v2-card__title">Latest updates</h2><div class="v2-card__subtitle">Curated, government-sourced UAP releases</div></div>
        <div><span class="v2-chip v2-chip--info">● curated</span></div>
      </div>
      <div class="v2-card__body" id="mufonUpdatesBody" style="padding:14px"></div>
      <div style="padding:0 14px 12px;font-size:11px;color:var(--muted)">
        Last reviewed: 2026-05-25 — curated. PURSUE drops every few weeks, not daily.
      </div>
    </div>

    <!-- Section B: Document Library -->
    <div class="v2-card" id="mufonDocsCard" style="margin-bottom:14px">
      <div class="v2-card__head">
        <div><h2 class="v2-card__title">Document library</h2><div class="v2-card__subtitle">Primary .gov archives & official UAP portals</div></div>
        <div><span class="v2-chip v2-chip--info">● external</span></div>
      </div>
      <div class="v2-card__body" id="mufonDocsBody" style="padding:14px"></div>
    </div>

    <!-- Section C: Sightings trend signal (derived client-side from the
         data-mufon.json sidecar; no extra fetch). -->
    <div id="mufonTrendLoading" class="hidden" style="text-align:center;padding:24px;color:var(--muted);font-size:13px">Loading sightings trend…</div>
    <div id="mufonTrendContent">
      <div class="v2-card" id="mufonTrendCard" style="margin-bottom:14px">
        <div class="v2-card__head">
          <div>
            <h2 class="v2-card__title">Sightings trend</h2>
            <div class="v2-card__subtitle">Long-term direction of NUFORC report volume</div>
          </div>
          <div><span class="v2-chip v2-chip--info" id="mufonTrendAsOf">● —</span></div>
        </div>
        <div class="v2-card__body" id="mufonTrendBody" style="padding:14px"></div>
      </div>
    </div>

    <!-- Section D: US Sightings Map -->
    <div id="mufonMapLoading" class="hidden" style="text-align:center;padding:32px;color:var(--muted);font-size:13px">Loading NUFORC sightings…</div>
    <div id="mufonMapContent">
      <div class="v2-card" id="mufonMapCard">
        <div class="v2-card__head">
          <div>
            <h2 class="v2-card__title">US sightings map</h2>
            <div class="v2-card__subtitle" id="mufonMapSub">NUFORC eyewitness reports · click a state for top cities</div>
          </div>
          <div><span class="v2-chip v2-chip--info" id="mufonMapAsOf">● —</span></div>
        </div>
        <div class="v2-card__body" id="mufonMapBody" style="padding:14px">
          <!-- time-range filter row -->
          <div id="mufonMapFilters" style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center">
            <span style="font-size:11px;color:var(--muted);margin-right:4px">Window:</span>
            <button class="btn btn--small" data-mufonrange="30d">30d</button>
            <button class="btn btn--small" data-mufonrange="60d">60d</button>
            <button class="btn btn--small" data-mufonrange="90d">90d</button>
            <button class="btn btn--small" data-mufonrange="365d">12mo</button>
            <button class="btn btn--small" data-mufonrange="all">All-time</button>
            <span id="mufonRangeNote" style="font-size:11px;color:var(--muted);margin-left:8px"></span>
          </div>
          <!-- grid: map (left) + side panel (right) on wide screens, stacks on narrow -->
          <div style="display:grid;grid-template-columns:minmax(0,2fr) minmax(220px, 1fr);gap:14px" id="mufonMapGrid">
            <div id="mufonMapSvgWrap" style="min-width:0"></div>
            <div id="mufonMapSidePanel" style="background:var(--bg2,#0f1419);border:1px solid var(--bd,#27313d);border-radius:8px;padding:12px;font-size:12px"></div>
          </div>
          <div style="margin-top:12px;font-size:11px;color:var(--muted);line-height:1.5">
            Data: NUFORC via community archive — sighting <strong>reports</strong>, not verified phenomena.
            Filed shapes and durations are eyewitness claims. Browse the live database at
            <a href="https://nuforc.org/" target="_blank" rel="noopener noreferrer">nuforc.org</a>.
          </div>
        </div>
      </div>
    </div>

    <!-- Section E: Sightings by classification (shape) over time. -->
    <div class="v2-card" id="mufonShapesCard" style="margin-top:14px">
      <div class="v2-card__head">
        <div>
          <h2 class="v2-card__title">Sightings by classification</h2>
          <div class="v2-card__subtitle" id="mufonShapesSub">NUFORC-reported shapes since recording began (1906)</div>
        </div>
        <div><span class="v2-chip v2-chip--info" id="mufonShapesAsOf">● —</span></div>
      </div>
      <div class="v2-card__body" id="mufonShapesBody" style="padding:14px"></div>
    </div>

    </div>
  </div><!-- /tab-mufon -->
</div>

<footer>
  Sources: ETF flows from your <code>data/*.csv</code>. Trading from CoinGecko, OKX, Deribit, Alternative.me. Whale proxies from blockchain.info. Refresh with <code>python app.py --fetch-market</code>.
</footer>

<!-- ============ CHAT DOCK ============ -->
<button id="chatFab" title="Ask about the data" aria-label="Open data chat">💬</button>
<aside id="chatDock" aria-label="Data chat">
  <div class="chat-head">
    <div>
      <h2>Ask the data</h2>
      <div class="sub">Powered by Claude · context = your live dashboard</div>
    </div>
    <button class="btn" id="chatClose" style="padding:3px 8px;font-size:12px" aria-label="Close data chat">×</button>
  </div>
  <div class="chat-msgs" id="chatMsgs"></div>
  <div class="chat-suggestions" id="chatSuggestions">
    <span class="chip" data-q="What are today's biggest changes?">today's biggest changes</span>
    <span class="chip" data-q="Compare BTC and ETH ETF flow trends.">compare BTC vs ETH flows</span>
    <span class="chip" data-q="Which BTC ETF had the largest 30-day inflow?">top 30d BTC fund</span>
    <span class="chip" data-q="What does the signal breakdown say about positioning?">signal breakdown</span>
    <span class="chip" data-q="Is funding bullish or bearish right now?">funding view</span>
    <span class="chip" data-q="Summarize the most important insights for me.">summarise insights</span>
  </div>
  <form class="chat-form" id="chatForm" autocomplete="off">
    <input type="text" id="chatInput" placeholder="Ask anything about the data…" required>
    <button type="submit" id="chatSend">Send</button>
  </form>
</aside>

<script>
// Surface uncaught JS errors as a thin red banner at the top of the page so
// a runtime exception during init (which silently kills the rest of the
// script, including the symbol-search wiring) is visible instead of just
// "nothing happens." Banner contains message + first stack line; click to
// dismiss. Production-safe: zero overhead until an error actually fires.
window.addEventListener('error', e => {
  try {
    const existing = document.getElementById('__jsErrBanner');
    if (existing) { existing.parentNode && existing.parentNode.removeChild(existing); }
    const b = document.createElement('div');
    b.id = '__jsErrBanner';
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;'
      + 'background:#7f1d1d;color:#fff;padding:8px 14px;font:12px/1.4 monospace;'
      + 'border-bottom:2px solid #ef4444;cursor:pointer;white-space:pre-wrap';
    const where = e.filename ? ' @ ' + e.filename.split('/').pop() + ':' + e.lineno : '';
    b.textContent = '⚠ JS error: ' + (e.message || 'unknown') + where +
      '\n(click to dismiss)';
    b.addEventListener('click', () => b.parentNode && b.parentNode.removeChild(b));
    (document.body || document.documentElement).appendChild(b);
  } catch (_) { /* defensive */ }
});

const DATA = __DATA_JSON__;
const SHARE_TOKEN = __SHARE_TOKEN__;  // string when viewing via /share/<token>, else null
const IS_SHARE = !!SHARE_TOKEN;

// Sidecar manifest: { whale: "data-whale.json", ... }
// Keys listed here are NOT inlined in DATA; loadSidecar(name) fetches and
// caches them on demand. Renderers must already guard against missing
// subtrees with `(DATA.foo||{}).bar` — they do.
const SIDECARS = __SIDECARS_JSON__;
// 'loading' | 'loaded' | 'error' per sidecar name; absent = never requested.
const SIDECAR_STATE = {};

async function loadSidecar(name){
  const url = (SIDECARS||{})[name];
  if (!url) return null;                       // nothing to load — already inlined or absent
  if (SIDECAR_STATE[name] === 'loaded') return DATA[name];
  if (SIDECAR_STATE[name] === 'loading') return null;  // in-flight; caller will re-render on land
  SIDECAR_STATE[name] = 'loading';
  try {
    // Share viewers hit the URL behind Basic Auth — the IS_SHARE fetch wrapper
    // above only rewrites /api/* paths, so static sidecars are served fine.
    const resp = await fetch(url, {cache: 'default'});
    if (!resp.ok) throw new Error('HTTP '+resp.status);
    DATA[name] = await resp.json();
    SIDECAR_STATE[name] = 'loaded';
    return DATA[name];
  } catch(e) {
    SIDECAR_STATE[name] = 'error';
    console.warn('[sidecar:'+name+'] load failed:', e);
    return null;
  }
}

// Which sidecar (if any) each tab needs. Tabs absent here are eager-rendered.
// cpi / supplies / metals payloads are produced by standalone fetchers
// (fetch_cpi.py / fetch_supplies.py / fetch_metals.py) which dual-write to
// v2/data-X.json AND data-X.json — the latter is what V1 loads here.
// travel sidecar is written by fetch_advisories.py from the V1 build itself
// (see main() in app.py) to /data-travel.json at repo root.
const SIDECAR_FOR_TAB = {
  whale: 'whale', defi: 'defi',
  cpi: 'cpi', supplies: 'supplies', metals: 'metals',
  travel: 'travel', mufon: 'mufon',
};

// In share mode, transparently append ?share=<token> to all /api/* and
// /data-*.json fetches so the read-only allowlist on the server lets the
// call through without prompting for HTTP Basic Auth. Sidecar payloads
// (e.g. /data-whale.json) are part of the same read-only surface as the
// inlined data, so the share token must flow through to them too.
if (IS_SHARE) {
  const _origFetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    try {
      if (typeof input === 'string' && (input.startsWith('/api/') || input.startsWith('/data-'))) {
        const sep = input.includes('?') ? '&' : '?';
        input = input + sep + 'share=' + encodeURIComponent(SHARE_TOKEN);
      }
    } catch(_) {}
    return _origFetch(input, init);
  };
}

const state = { tab:'etf', asset:'btc', period:'daily', range:'all', fundwin:'30', macroRange:'1Y', cohortBin:'month',
  // Per-tab asset toggle for the Whale tab (independent of the global asset
  // selector). Persisted to localStorage so the chosen view sticks.
  whaleAsset: (typeof localStorage !== 'undefined' && localStorage.getItem('whaleAsset') === 'eth') ? 'eth' : 'btc',
  // Per-tab asset toggle for the ETF Flows tab — BTC or ETH only (no spot
  // LINK/LTC ETFs exist). Decoupled from state.asset: ETF renderers read
  // state.etfAsset via the etfAsset() helper, so switching ETF view does
  // NOT cascade to POC overlay / Futures / other tabs that read state.asset.
  etfAsset: (typeof localStorage !== 'undefined' && localStorage.getItem('etfAsset') === 'eth') ? 'eth' : 'btc',
  // Per-tab asset toggle for the Futures tab (BTC/ETH/LINK/LTC — the original
  // full set with derivatives data). Coupled to state.asset on click since
  // Futures renderers are tangled with the global asset state (tradingAssetData,
  // POC overlay, KPI dominance logic, DVOL empty-state copy, etc.). The
  // expected UX is that the Futures toggle IS the global asset selector while
  // the user is on this tab.
  futuresAsset: (function(){
    if (typeof localStorage === 'undefined') return 'btc';
    const v = localStorage.getItem('futuresAsset');
    return ['btc','eth','link','ltc'].includes(v) ? v : 'btc';
  })(),
  // Per-tab chain selector for the DeFi tab. One of Ethereum / Solana /
  // Arbitrum / Base (the 4 chains we have tvl_history for). Default Ethereum.
  defiChain: (function(){
    if (typeof localStorage === 'undefined') return 'Ethereum';
    const v = localStorage.getItem('defiChain');
    return ['Ethereum','Solana','Arbitrum','Base'].includes(v) ? v : 'Ethereum';
  })(),
  // CPI tab toggles — time range (5y/10y/20y/30y/all) and view mode
  // (index100 / absolute / pctchange). Default "all" range + "index100"
  // mirrors the V2 defaults so cross-version users see the same picture.
  cpiTimeRange: 'all',
  cpiViewMode: 'index100',
  // Travel Advisories tab — sub-view ('overview' / 'level1' / 'level2' /
  // 'level34' / 'terror'), L3/L4 segment ('all' / '3' / '4'), free-text
  // search, terrorism-only toggle (list views), and sort key. Defaults
  // mirror V2 so cross-version users land on the same Overview view.
  travelSub: 'overview',
  travelSubLevel: 'all',
  travelQuery: '',
  travelTerrorOnly: false,
  travelSort: 'level',
  // UAP / MUFON tab — sightings-map time filter + currently-selected state,
  // plus per-card range toggles for the trend + shapes cards. Anchored to the
  // dataset's most-recent entry (see fetch_mufon.py), not today's date.
  mufonTimeRange: 'all',
  mufonSelectedState: null,
  mufonTrendRange: '5y',
  mufonShapesRange: '5y' };

// ---------- formatters ----------
const fmtUSD = (n, unit='M') => {
  if (n == null || isNaN(n)) return '—';
  const sign = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (unit === 'M'){
    if (a >= 1000) return sign + '$' + (a/1000).toFixed(2) + 'B';
    return sign + '$' + a.toFixed(1) + 'M';
  }
  if (a >= 1e12) return sign + '$' + (a/1e12).toFixed(2) + 'T';
  if (a >= 1e9)  return sign + '$' + (a/1e9).toFixed(2) + 'B';
  if (a >= 1e6)  return sign + '$' + (a/1e6).toFixed(2) + 'M';
  if (a >= 1e3)  return sign + '$' + (a/1e3).toFixed(2) + 'K';
  return sign + '$' + a.toFixed(2);
};
const fmtSigned = n => (n>=0?'+':'') + fmtUSD(n);
const fmtPct = (n, d=2) => n==null?'—':(n*100).toFixed(d)+'%';
const fmtNum = (n, d=2) => n==null?'—':n.toLocaleString(undefined,{maximumFractionDigits:d});
// Defang URLs from third-party APIs (news, Reddit, CryptoCompare, image CDNs)
// before interpolating into href/src. Rejects javascript:, data:, vbscript:,
// file:, and any non-http(s) scheme. Pass '' as fallback for img src.
const sanitizeUrl = (u, fallback='#') =>
  (typeof u === 'string' && /^https?:\/\//i.test(u)) ? u : fallback;

const colorFor = n => n >= 0 ? '#22c55e' : '#ef4444';
const ACCENTS = {btc:'#f7931a', eth:'#627eea', link:'#2a5ada', ltc:'#bfbbbb'};
const accentFor = a => ACCENTS[a] || '#627eea';

// ---------- range helpers ----------
function rangeStartFor(rows){
  if (!rows || rows.length === 0) return null;
  const last = new Date(rows[rows.length-1].date);
  const map = {'3m':90,'6m':180,'1y':365,'2y':730,'3y':1095};
  if (state.range === 'all') return null;
  const days = map[state.range] || 0;
  const s = new Date(last); s.setDate(s.getDate()-days);
  return s.getTime();
}
function applyRange(rows){
  const t = rangeStartFor(rows);
  if (t == null) return rows || [];
  return (rows || []).filter(r => new Date(r.date).getTime() >= t);
}

// Resample a daily series into weekly/monthly/yearly buckets.
// `aggBy` is either a string ("sum"/"mean"/"last"/"max"/"min")
// applied to all numeric keys, or an object {keyName: aggregator}.
function bucketKey(dateStr, period){
  if (!dateStr) return dateStr;
  if (period === 'daily' || !period) return dateStr;
  if (period === 'monthly') return dateStr.slice(0,7) + '-01';
  if (period === 'yearly')  return dateStr.slice(0,4) + '-01-01';
  if (period === 'weekly'){
    const d = new Date(dateStr + 'T00:00:00Z');
    const day = d.getUTCDay() || 7; // 1..7 with Mon=1
    d.setUTCDate(d.getUTCDate() - (day - 1));
    return d.toISOString().slice(0,10);
  }
  return dateStr;
}
function resample(rows, period, aggBy){
  if (!rows || !rows.length || period === 'daily') return rows || [];
  const buckets = new Map();
  for (const r of rows){
    const k = bucketKey(r.date, period);
    if (!buckets.has(k)) buckets.set(k, []);
    buckets.get(k).push(r);
  }
  const out = [];
  for (const [k, items] of buckets){
    const sample = items[0];
    const o = {date:k};
    for (const key of Object.keys(sample)){
      if (key === 'date') continue;
      const vals = items.map(it => it[key]).filter(v => v != null && !isNaN(v));
      if (!vals.length){ o[key] = null; continue; }
      const ag = (typeof aggBy === 'object') ? (aggBy[key] || 'last') : aggBy;
      switch (ag){
        case 'sum':  o[key] = vals.reduce((s,v)=>s+v,0); break;
        case 'mean': o[key] = vals.reduce((s,v)=>s+v,0)/vals.length; break;
        case 'last': o[key] = vals[vals.length-1]; break;
        case 'first':o[key] = vals[0]; break;
        case 'max':  o[key] = Math.max(...vals); break;
        case 'min':  o[key] = Math.min(...vals); break;
        default:     o[key] = vals[vals.length-1];
      }
    }
    out.push(o);
  }
  out.sort((a,b)=> a.date.localeCompare(b.date));
  return out;
}
// Convenience: range + period aggregation
function ra(rows, aggBy){ return resample(applyRange(rows), state.period, aggBy); }

// ---------- chart helpers ----------
const charts = {};
function destroy(id){ if (charts[id]){ charts[id].destroy(); delete charts[id]; } }
function baseOpts({yLabel='', tooltipFmt=null}={}){
  return {
    responsive:true, maintainAspectRatio:false,
    plugins:{
      legend:{display:false, labels:{color:'#e6e8ee'}},
      tooltip:{
        callbacks: tooltipFmt ? {label: ctx => tooltipFmt(ctx.parsed.y, ctx)} : {},
      },
    },
    scales:{
      x:{ticks:{color:'#8a93a6', maxRotation:0, autoSkip:true, maxTicksLimit:10}, grid:{color:'#1f2533'}},
      y:{title:{display:!!yLabel, text:yLabel, color:'#8a93a6'}, ticks:{color:'#8a93a6'}, grid:{color:'#1f2533'}},
    },
  };
}

// ---------- ETF tab ----------
// ETF Flows tab is decoupled from the global state.asset — it reads its own
// per-tab asset (state.etfAsset, 'btc' or 'eth' only — no spot LINK/LTC ETFs).
// This keeps switching the ETF view from cascading to POC overlay / Futures /
// other tabs that still read state.asset.
function etfAsset(){ return state.etfAsset; }
function etfData(){ return DATA[etfAsset()] || {}; }

function renderEtfKpis(){
  const d = etfData(); const s = d.stats || {};
  const items = [
    {label:`Last day (${s.last_date||'—'})`, val:fmtSigned(s.last_day_flow), cls:s.last_day_flow>=0?'green':'red'},
    {label:'Last 7 days', val:fmtSigned(s.last_7d), cls:s.last_7d>=0?'green':'red'},
    {label:'Last 30 days', val:fmtSigned(s.last_30d), cls:s.last_30d>=0?'green':'red'},
    {label:'Year to date', val:fmtSigned(s.ytd), cls:s.ytd>=0?'green':'red'},
    {label:'All time net', val:fmtSigned(s.all_time), cls:s.all_time>=0?'green':'red'},
    {label:'Current streak', val: s.streak ? `${s.streak.length}d ${s.streak.direction}` : '—',
     cls: s.streak ? (s.streak.direction==='up'?'green':s.streak.direction==='down'?'red':'amber') : ''},
  ];
  document.getElementById('etfKpis').innerHTML = items.map(i =>
    `<div class="card"><h3>${i.label}</h3><div class="v ${i.cls}">${i.val}</div></div>`
  ).join('');
}

// ---------- Fund detail (new By-Fund section) ----------
const FUND_PALETTE = ['#f7931a','#627eea','#22c55e','#a78bfa','#ec4899','#06b6d4','#f59e0b','#10b981','#8b5cf6','#ef4444','#14b8a6','#fb923c','#84cc16'];

function fundWindowKey(){
  return state.fundwin === '60' ? 'last_60d'
       : state.fundwin === '90' ? 'last_90d'
       : state.fundwin === 'all' ? 'total'
       : 'last_30d';
}
function fundWindowLabel(){
  return state.fundwin === 'all' ? 'All-time' : state.fundwin + 'd';
}

function renderFundKpis(){
  const d = etfData();
  const funds = d.by_fund || [];
  const host = document.getElementById('fundKpiGrid');
  if (!funds.length){
    host.innerHTML = `<div class="empty" style="grid-column:1/-1">No per-fund data loaded. Use <b>Paste ${etfAsset().toUpperCase()}</b> with the full Farside table to populate fund-level views.</div>`;
    return;
  }
  const winKey = fundWindowKey();
  const winLabel = fundWindowLabel();
  // sort by the selected window (desc)
  const sorted = funds.slice().sort((a,b) => (b[winKey]||0) - (a[winKey]||0));
  host.innerHTML = sorted.map((f, i) => {
    const c = (etfAsset()==='eth' ? '#627eea' : '#f7931a');
    const flowCls = (f[winKey]||0) >= 0 ? 'green' : 'red';
    const totalCls = f.total >= 0 ? 'green' : 'red';
    return `
      <div class="card" style="border-left:3px solid ${FUND_PALETTE[i % FUND_PALETTE.length]}">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px">
          <div style="font-weight:700;font-size:14px;letter-spacing:.03em">${escapeHtml(f.fund)}</div>
          <div class="sub" style="font-size:10px;color:var(--muted)">${(Number(f.share_pct) || 0).toFixed(1)}% share</div>
        </div>
        <div class="sub" style="color:var(--muted);font-size:11px;margin-top:2px;min-height:14px">${escapeHtml(f.name||'')}</div>
        <div class="v ${flowCls}" style="font-size:18px;margin-top:6px">${fmtSigned(f[winKey]||0)}</div>
        <div class="sub" style="color:var(--muted);font-size:10px">${winLabel} net</div>
        <div style="display:flex;gap:8px;font-size:11px;margin-top:6px;flex-wrap:wrap">
          <span class="sub">30d <span class="${(f.last_30d>=0?'green':'red')}">${fmtSigned(f.last_30d)}</span></span>
          <span class="sub">60d <span class="${(f.last_60d>=0?'green':'red')}">${fmtSigned(f.last_60d)}</span></span>
          <span class="sub">90d <span class="${(f.last_90d>=0?'green':'red')}">${fmtSigned(f.last_90d)}</span></span>
        </div>
        <div class="sub" style="font-size:11px;margin-top:4px">All-time <span class="${totalCls}">${fmtSigned(f.total)}</span></div>
      </div>`;
  }).join('');
}

function renderFundStack(){
  const d = etfData();
  const fundDaily = d.by_fund_daily || {};
  const funds = (d.by_fund || []).slice().sort((a,b) => Math.abs(b.total) - Math.abs(a.total));
  destroy('fundStack');
  if (!funds.length){
    return;
  }
  // Range filter: use any one fund's date axis (they all share the same dates)
  const first = funds[0].fund;
  const ref = applyRange(fundDaily[first] || []);
  const labels = ref.map(r => r.date);
  const dateSet = new Set(labels);

  const datasets = funds.map((f, i) => {
    const color = FUND_PALETTE[i % FUND_PALETTE.length];
    const series = (fundDaily[f.fund] || []).filter(r => dateSet.has(r.date));
    return {
      label: f.fund,
      data: series.map(r => r.cumulative),
      borderColor: color,
      backgroundColor: color + '88',
      fill: true,
      pointRadius: 0,
      borderWidth: 1.2,
      tension: 0.2,
    };
  });
  charts.fundStack = new Chart(document.getElementById('fundStackChart'), {
    type:'line',
    data:{labels, datasets},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{labels:{color:'#e6e8ee', font:{size:10}}},
        tooltip:{mode:'index', intersect:false, callbacks:{label: ctx => `${ctx.dataset.label}: ${fmtSigned(ctx.parsed.y)}`}},
      },
      scales:{
        x:{ticks:{color:'#8a93a6', maxTicksLimit:10}, grid:{color:'#1f2533'}, stacked:true},
        y:{title:{display:true,text:'Cumulative ($M)',color:'#8a93a6'}, ticks:{color:'#8a93a6', callback:v=>fmtUSD(v)}, grid:{color:'#1f2533'}, stacked:true},
      },
    },
  });
}

function renderFundCompare(){
  const d = etfData();
  const funds = (d.by_fund || []);
  destroy('fundCompare');
  if (!funds.length) return;
  const winKey = fundWindowKey();
  const sorted = funds.slice().sort((a,b) => (b[winKey]||0) - (a[winKey]||0));
  const labels = sorted.map(f => f.fund);
  const data = sorted.map(f => f[winKey] || 0);
  const colors = data.map(v => v >= 0 ? '#22c55e' : '#ef4444');

  charts.fundCompare = new Chart(document.getElementById('fundCompareChart'), {
    type:'bar',
    data:{labels, datasets:[{data, backgroundColor:colors, borderWidth:0}]},
    options:{
      indexAxis: 'y',
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{display:false},
        tooltip:{callbacks:{label: ctx => fmtSigned(ctx.parsed.x)}},
      },
      scales:{
        x:{title:{display:true,text:`Net flow ${fundWindowLabel()} ($M)`, color:'#8a93a6'}, ticks:{color:'#8a93a6', callback:v=>fmtUSD(v)}, grid:{color:'#1f2533'}},
        y:{ticks:{color:'#e6e8ee', font:{weight:'600'}}, grid:{display:false}},
      },
    },
  });
}

function renderEtfFundTable(){
  const d = etfData(); const tb = document.querySelector('#fundTable tbody');
  if (!d.by_fund || !d.by_fund.length){ tb.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--muted)">No fund data</td></tr>'; return; }
  tb.innerHTML = d.by_fund.map(f => `<tr><td>${escapeHtml(f.fund)}</td><td class="${f.total>=0?'green':'red'}">${fmtSigned(f.total)}</td><td class="${f.last_30d>=0?'green':'red'}">${fmtSigned(f.last_30d)}</td></tr>`).join('');
}

function renderFlow(){
  const d = etfData(); const series = applyRange(d[state.period]);
  destroy('flow');
  charts.flow = new Chart(document.getElementById('flowChart'), {
    type:'bar',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.flow), backgroundColor:series.map(r=>colorFor(r.flow)), borderWidth:0}]},
    options: baseOpts({yLabel:'Flow ($M)', tooltipFmt:v=>fmtSigned(v)}),
  });
}
function renderCum(){
  const d = etfData(); const series = applyRange(d[state.period]); const c = accentFor(etfAsset());
  destroy('cum');
  charts.cum = new Chart(document.getElementById('cumChart'), {
    type:'line',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.cumulative), borderColor:c, backgroundColor:c+'33', fill:true, tension:0.2, pointRadius:0, borderWidth:2}]},
    options: baseOpts({yLabel:'Cumulative ($M)', tooltipFmt:v=>fmtSigned(v)}),
  });
}
function renderYoy(){
  const d = etfData(); const yoy = d.yoy || {};
  const palette = ['#f7931a','#627eea','#22c55e','#a78bfa','#ec4899','#06b6d4','#f59e0b'];
  const datasets = Object.keys(yoy).sort().map((y,i) => ({
    label:y, data:yoy[y].doy.map((doy,idx)=>({x:doy,y:yoy[y].cumulative[idx]})),
    borderColor:palette[i%palette.length], backgroundColor:'transparent', tension:0.2, pointRadius:0, borderWidth:2,
  }));
  destroy('yoy');
  charts.yoy = new Chart(document.getElementById('yoyChart'), {
    type:'line', data:{datasets},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{labels:{color:'#e6e8ee'}}, tooltip:{mode:'index',intersect:false}},
      scales:{
        x:{type:'linear', title:{display:true,text:'Day of year',color:'#8a93a6'}, ticks:{color:'#8a93a6'}, grid:{color:'#1f2533'}},
        y:{title:{display:true,text:'Cumulative ($M)',color:'#8a93a6'}, ticks:{color:'#8a93a6',callback:v=>fmtUSD(v)}, grid:{color:'#1f2533'}},
      },
    },
  });
}

// ---------- Trading tab ----------
function tradingAssetData(){ return (DATA.market||{})[state.asset] || {}; }

function renderTradingKpis(){
  const m = DATA.market || {}; const a = tradingAssetData(); const g = m.global || {}; const fng = (m.fear_greed||[]).slice(-1)[0];
  // Period-aware price delta: match the lookback window to the currently
  // selected Period button so the KPI sub-text isn't misleadingly "1d"
  // when the user is on Weekly / Monthly / Yearly view.
  const priceSeries = a.price || [];
  const lastPrice = priceSeries.slice(-1)[0];
  const periodLookback = { daily: 1, weekly: 7, monthly: 30, yearly: 365 };
  const periodLabel    = { daily: '1d', weekly: '7d', monthly: '30d', yearly: '1y' };
  const lookback = periodLookback[state.period] || 1;
  const lbLabel  = periodLabel[state.period]    || '1d';
  const prevPrice = priceSeries.length > lookback ? priceSeries[priceSeries.length - 1 - lookback] : null;
  const chgPct = (lastPrice && prevPrice && prevPrice.value) ? (lastPrice.value/prevPrice.value - 1) : null;
  const lastVol = (a.volume||[]).slice(-1)[0];
  const lastFund = (a.funding||[]).slice(-1)[0];
  const lastOI = (a.open_interest_usd||[]).slice(-1)[0];
  const lastLS = (a.long_short_ratio||[]).slice(-1)[0];
  const lastDvol = (a.dvol||[]).slice(-1)[0];
  const ethbtc = (m.ethbtc||[]).slice(-1)[0];

  const items = [
    {label:'Spot price', val: lastPrice ? fmtUSD(lastPrice.value, 'auto') : '—', sub: chgPct!=null ? (chgPct>=0?'+':'')+(chgPct*100).toFixed(2)+'% ' + lbLabel : '', cls: chgPct==null?'':(chgPct>=0?'green':'red')},
    {label:'24h volume', val: lastVol ? fmtUSD(lastVol.value,'auto') : '—'},
    {label:'Market cap', val: a.market_cap && a.market_cap.length ? fmtUSD(a.market_cap.slice(-1)[0].value,'auto') : '—'},
    {label:'Funding rate', val: lastFund ? (lastFund.rate*100).toFixed(4)+'%' : '—', cls: lastFund ? (lastFund.rate>=0?'green':'red') : '', sub: lastFund ? lastFund.date : ''},
    {label:'Open interest', val: lastOI ? fmtUSD(lastOI.oi_usd,'auto') : '—'},
    {label:'Long/short ratio', val: lastLS ? fmtNum(lastLS.ratio,2) : '—', cls: lastLS ? (lastLS.ratio>1?'green':'red') : ''},
    {label:'DVOL (implied vol)', val: lastDvol ? lastDvol.dvol.toFixed(1)+'%' : '—'},
    {label:'Fear & Greed', val: fng ? `${fng.value} (${fng.label})` : '—', cls: fng ? (fng.value>=60?'green':fng.value<=40?'red':'amber') : ''},
    // For BTC/ETH show their own dominance; for LINK there is no single
    // "LINK dominance" metric, so fall back to the broader BTC dominance
    // (the macro-context number most relevant regardless of asset focus).
    (state.asset === 'btc')
      ? {label:'BTC dominance', val: fmtNum(g.btc_dominance,2)+'%'}
      : (state.asset === 'eth')
        ? {label:'ETH dominance', val: fmtNum(g.eth_dominance,2)+'%'}
        : {label:'BTC dominance', val: fmtNum(g.btc_dominance,2)+'%', sub:'macro context'},
    {label:'ETH/BTC', val: ethbtc ? ethbtc.value.toFixed(5) : '—'},
  ];
  document.getElementById('tradingKpis').innerHTML = items.map(i =>
    `<div class="card"><h3>${i.label}</h3><div class="v ${i.cls||''}">${i.val}</div>${i.sub?`<div class="sub">${i.sub}</div>`:''}</div>`
  ).join('');
}

// POC overlay state (Trading-tab price chart). Persisted in localStorage.
// `on`  — boolean, render POC/VAH/VAL horizontal lines on price chart
// `win` — 'd30' or 'd90', which timeframe's POC to overlay
const pocOverlay = {
  on:  (typeof localStorage !== 'undefined') && localStorage.getItem('tradingShowPoc') === '1',
  win: ((typeof localStorage !== 'undefined') && localStorage.getItem('tradingPocWin') === 'd30') ? 'd30' : 'd90',
};

function pocLevelsFor(asset, win){
  const p = ((DATA.market||{}).poc || {})[asset];
  if (!p) return null;
  return p[win] || p.d90 || p.d30 || null;
}

function renderPriceVol(){
  const a = tradingAssetData(); const c = accentFor(state.asset);
  const price = ra(a.price, 'last');
  const vol = ra(a.volume, 'sum');
  const labels = price.map(r=>r.date);
  destroy('price');
  const datasets = [
    {type:'bar', label:'24h volume', data:vol.map(r=>r.value), backgroundColor:'#2a3140', yAxisID:'yVol', borderWidth:0, order:2},
    {type:'line', label:'Price', data:price.map(r=>r.value), borderColor:c, backgroundColor:c+'22', fill:false, tension:0.15, pointRadius:0, borderWidth:2, yAxisID:'yPrice', order:1},
  ];
  // POC overlay: extra constant-y datasets (POC, VAH, VAL + naked POCs) when
  // enabled. Chart.js annotation plugin isn't loaded so we use the simpler
  // "horizontal line via flat dataset" approach.
  if (pocOverlay.on){
    const flat = y => labels.map(()=>y);
    const lv = pocLevelsFor(state.asset, pocOverlay.win);
    if (lv && lv.poc != null){
      const tag = pocOverlay.win.toUpperCase();
      datasets.push(
        {type:'line', label:`POC ${tag}`, data:flat(lv.poc), borderColor:'#ffcc66', borderWidth:1.5, pointRadius:0, fill:false, yAxisID:'yPrice', order:0, spanGaps:true},
        {type:'line', label:`VAH ${tag}`, data:flat(lv.vah), borderColor:'#8fbf8f', borderWidth:1, borderDash:[6,4], pointRadius:0, fill:false, yAxisID:'yPrice', order:0},
        {type:'line', label:`VAL ${tag}`, data:flat(lv.val), borderColor:'#cf6a6a', borderWidth:1, borderDash:[6,4], pointRadius:0, fill:false, yAxisID:'yPrice', order:0},
      );
    }
    // Naked POCs (untested weekly POCs in last 180d) — thin dashed lines,
    // green if below current price (support), red if above (resistance).
    // Cap at 3 so the legend doesn't explode.
    const allPoc = ((DATA.market||{}).poc || {})[state.asset] || {};
    const naked = Array.isArray(allPoc.naked) ? allPoc.naked.slice(0, 3) : [];
    const cur = price.length ? price[price.length-1].value : null;
    naked.forEach(n => {
      if (n.poc == null) return;
      const isResist = (cur != null && n.poc > cur);
      const col = isResist ? '#cf6a6a' : '#8fbf8f';
      datasets.push({
        type:'line',
        label:`Naked ${fmtUSD(n.poc,'auto')} (${n.days_ago}d)`,
        data:flat(n.poc),
        borderColor:col, borderWidth:1, borderDash:[2,3],
        pointRadius:0, fill:false, yAxisID:'yPrice', order:0,
      });
    });
  }
  charts.price = new Chart(document.getElementById('priceChart'), {
    type:'bar',
    data:{labels, datasets},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{display:true,labels:{color:'#e6e8ee'}}, tooltip:{mode:'index', intersect:false, callbacks:{label:ctx=>ctx.dataset.label+': '+fmtUSD(ctx.parsed.y,'auto')}}},
      scales:{
        x:{ticks:{color:'#8a93a6', maxTicksLimit:10}, grid:{color:'#1f2533'}},
        yPrice:{position:'left', title:{display:true,text:'Price (USD)',color:'#8a93a6'}, ticks:{color:'#8a93a6', callback:v=>fmtUSD(v,'auto')}, grid:{color:'#1f2533'}},
        yVol:{position:'right', title:{display:true,text:'Volume (USD)',color:'#8a93a6'}, ticks:{color:'#8a93a6', callback:v=>fmtUSD(v,'auto')}, grid:{display:false}},
      },
    },
  });
}

function renderFunding(){
  const a = tradingAssetData(); const series = ra(a.funding, 'mean');
  destroy('funding');
  charts.funding = new Chart(document.getElementById('fundingChart'), {
    type:'bar',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.rate*100), backgroundColor:series.map(r=>colorFor(r.rate)), borderWidth:0}]},
    options: baseOpts({yLabel:'Rate (%)', tooltipFmt:v=>v.toFixed(4)+'%'}),
  });
}

function renderOI(){
  const a = tradingAssetData(); const series = ra(a.open_interest_usd, 'last');
  const c = accentFor(state.asset);
  destroy('oi');
  charts.oi = new Chart(document.getElementById('oiChart'), {
    type:'line',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.oi_usd), borderColor:c, backgroundColor:c+'22', fill:true, tension:0.2, pointRadius:0, borderWidth:2}]},
    options: baseOpts({yLabel:'OI (USD)', tooltipFmt:v=>fmtUSD(v,'auto')}),
  });
}

function renderLS(){
  const a = tradingAssetData(); const series = ra(a.long_short_ratio, 'mean');
  destroy('ls');
  charts.ls = new Chart(document.getElementById('lsChart'), {
    type:'line',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.ratio), borderColor:'#a78bfa', backgroundColor:'#a78bfa22', fill:true, tension:0.2, pointRadius:0, borderWidth:2}]},
    options: baseOpts({yLabel:'L/S ratio', tooltipFmt:v=>v.toFixed(3)}),
  });
}

// Coinbase International Exchange perpetuals positioning tables.
// Pulls market.coinbase_intl_perps (pre-sorted desc by funding_rate on the
// backend), splits into positive-funding (crowded longs) and negative-funding
// (crowded shorts) buckets, takes top 6 of each, and renders into the two
// table bodies on the Trading tab.
function renderCoinbaseIntlPerps(){
  const perps = (DATA.market || {}).coinbase_intl_perps || [];
  const longs  = perps.filter(p => p && typeof p.funding_rate === 'number' && p.funding_rate > 0)
                      .sort((a,b) => b.funding_rate - a.funding_rate)
                      .slice(0, 6);
  const shorts = perps.filter(p => p && typeof p.funding_rate === 'number' && p.funding_rate < 0)
                      .sort((a,b) => a.funding_rate - b.funding_rate)
                      .slice(0, 6);
  const emptyRow = '<tr><td colspan="5" style="text-align:center;color:var(--muted);padding:14px">No perpetuals data — wait for next refresh</td></tr>';

  function rowFor(p){
    const ratePct = (p.funding_rate * 100).toFixed(4) + '%';
    const cls = p.funding_rate >= 0 ? 'green' : 'red';
    const mark = (p.mark_price != null) ? fmtUSD(p.mark_price, 'auto') : '—';
    const notional = (p.notional_24h != null) ? fmtUSD(p.notional_24h, 'auto') : '—';
    const oi = (p.open_interest_base != null)
      ? fmtNum(p.open_interest_base, 0) + ' ' + (p.symbol || '')
      : '—';
    return `<tr>
      <td><strong>${p.symbol || '—'}</strong></td>
      <td class="${cls}" style="font-variant-numeric:tabular-nums">${ratePct}</td>
      <td style="font-variant-numeric:tabular-nums">${mark}</td>
      <td style="font-variant-numeric:tabular-nums">${notional}</td>
      <td style="font-variant-numeric:tabular-nums">${oi}</td>
    </tr>`;
  }

  const longsBody  = document.querySelector('#cieLongsTable tbody');
  const shortsBody = document.querySelector('#cieShortsTable tbody');
  if (longsBody)  longsBody.innerHTML  = longs.length  ? longs.map(rowFor).join('')  : emptyRow;
  if (shortsBody) shortsBody.innerHTML = shorts.length ? shorts.map(rowFor).join('') : emptyRow;
}

// CADLI BTC reference price chart — 90d daily close from the CoinDesk CADLI
// Cryptocurrency Real-Time Index. CADLI is the regulated reference price
// used in derivatives settlement, so it lives on the Futures tab alongside
// funding/OI. Data shape: [{date, open, high, low, close, volume}, ...] —
// we map close→value and re-use the shared lineChart() helper.
function renderCadliChart(){
  const bars = (DATA.market || {}).cadli_btc || [];
  const series = (bars || [])
    .filter(b => b && b.date && b.close != null)
    .map(b => ({date: b.date, value: b.close}));
  if (!chartOrEmpty('cadliBtcChart', series.length > 0, 'No CADLI BTC reference data — wait for next refresh.')) {
    destroy('cadliBtc');
    return;
  }
  lineChart('cadliBtcChart', 'cadliBtc', series, '#f7931a', v => fmtUSD(v, 'auto'));
}

// Toggle an empty-state placeholder inside a chart's .chart-wrap container.
// Returns true if data is present (caller should proceed to build the chart);
// false if the placeholder is shown (caller should skip the chart).
function chartOrEmpty(canvasId, hasData, msg){
  const canvas = document.getElementById(canvasId);
  if (!canvas) return hasData;
  const wrap = canvas.parentElement;
  let empty = wrap.querySelector('.chart-empty');
  if (!empty) {
    empty = document.createElement('div');
    empty.className = 'chart-empty';
    empty.style.cssText = 'position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px;text-align:center;padding:14px;line-height:1.4';
    wrap.appendChild(empty);
  }
  if (!hasData) {
    canvas.style.display = 'none';
    empty.style.display = 'flex';
    empty.textContent = msg || 'No data available.';
    return false;
  }
  canvas.style.display = '';
  empty.style.display = 'none';
  return true;
}

function renderDvol(){
  const a = tradingAssetData(); const series = ra(a.dvol, 'last');
  destroy('dvol');
  if (!chartOrEmpty('dvolChart', series.length > 0,
      `DVOL not available for ${state.asset.toUpperCase()} — Deribit only quotes BTC and ETH.`)) return;
  charts.dvol = new Chart(document.getElementById('dvolChart'), {
    type:'line',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.dvol), borderColor:'#06b6d4', backgroundColor:'#06b6d422', fill:true, tension:0.2, pointRadius:0, borderWidth:2}]},
    options: baseOpts({yLabel:'DVOL (%)', tooltipFmt:v=>v.toFixed(1)+'%'}),
  });
}

function renderFng(){
  const series = ra((DATA.market||{}).fear_greed, {value:'mean'});
  const colors = series.map(r => r.value>=60?'#22c55e' : r.value<=40?'#ef4444' : '#f59e0b');
  destroy('fng');
  charts.fng = new Chart(document.getElementById('fngChart'), {
    type:'bar',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.value), backgroundColor:colors, borderWidth:0}]},
    options: baseOpts({yLabel:'Index 0–100', tooltipFmt:(v,ctx)=>v+' ('+series[ctx.dataIndex].label+')'}),
  });
}

function renderEthBtc(){
  const series = ra((DATA.market||{}).ethbtc, 'last');
  destroy('ethbtc');
  charts.ethbtc = new Chart(document.getElementById('ethbtcChart'), {
    type:'line',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.value), borderColor:'#a78bfa', backgroundColor:'#a78bfa22', fill:true, tension:0.2, pointRadius:0, borderWidth:2}]},
    options: baseOpts({yLabel:'ETH/BTC', tooltipFmt:v=>v.toFixed(5)}),
  });
}

function renderGlobalTable(){
  const g = (DATA.market||{}).global || {};
  const rows = [
    ['Total market cap (all crypto)', fmtUSD(g.total_market_cap_usd,'auto')],
    ['Total 24h volume', fmtUSD(g.total_volume_usd,'auto')],
    ['BTC dominance', fmtNum(g.btc_dominance,2)+'%'],
    ['ETH dominance', fmtNum(g.eth_dominance,2)+'%'],
    ['Active cryptocurrencies', fmtNum(g.active_cryptos,0)],
  ];
  document.querySelector('#globalTable tbody').innerHTML = rows.map(r=>`<tr><td>${r[0]}</td><td>${r[1]}</td></tr>`).join('');
}

// ---------- Signals tab ----------
function signalColor(score){
  if (score >= 50) return '#16a34a';
  if (score >= 20) return '#22c55e';
  if (score > -20) return '#f59e0b';
  if (score > -50) return '#ef4444';
  return '#b91c1c';
}

function renderSignalCard(asset, container){
  const s = (DATA.signals||{})[asset];
  if (!s){
    return `<div class="chart-card"><h2 style="margin:0">${asset.toUpperCase()}</h2><div class="empty">No signal — need more price history</div></div>`;
  }
  const color = signalColor(s.score);
  const accent = accentFor(asset);
  const compRows = s.components.map(c => {
    const cls = c.contribution > 0 ? 'green' : (c.contribution < 0 ? 'red' : 'amber');
    const sign = (c.contribution>=0?'+':'') + c.contribution;
    return `<tr><td>${escapeHtml(c.name)}</td><td>${escapeHtml(String(c.value))}</td><td class="${cls}">${sign}</td><td style="color:var(--muted);font-size:12px">${escapeHtml(c.explanation||'')}</td></tr>`;
  }).join('');
  // Gauge: -100 to +100, 0 in middle
  const pct = ((s.score + 100) / 200) * 100;
  return `
    <div class="chart-card" style="position:relative">
      <div class="head" style="align-items:flex-start">
        <div>
          <h2 style="font-size:15px">${asset.toUpperCase()} signal <span class="tag ${asset}">$${s.price.toLocaleString(undefined,{maximumFractionDigits:0})}</span></h2>
          <div class="desc">as of ${escapeHtml(s.as_of)}</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:28px;font-weight:700;color:${color}">${s.label}</div>
          <div style="font-size:13px;color:var(--muted)">score <strong style="color:${color}">${s.score>=0?'+':''}${s.score}</strong> / ±100</div>
        </div>
      </div>
      <div style="height:10px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:5px;position:relative;margin:8px 0">
        <div style="position:absolute;top:-4px;left:calc(${pct.toFixed(1)}% - 4px);width:8px;height:18px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
      </div>
      ${signalScoreSparkline(s.history)}
      <table style="margin-top:6px"><thead><tr><th>Component</th><th>Value</th><th>±</th><th>Read</th></tr></thead><tbody>${compRows}</tbody></table>
      <div class="sub" style="margin-top:8px;font-size:11px">${escapeHtml(s.disclaimer)}</div>
    </div>`;
}

// Render the top-of-tab breadth chart for the Crypto Signals tab. Sources
// DATA.market.poc_top — each entry carries `signal_history: [{date,score}, ...90]`
// (signals_top20 has no history). Defensive: coins without a usable history
// array are filtered out and simply don't contribute to the breadth (they
// still show up on the Top 50 cards). Safe to call when DATA isn't loaded —
// the helper renders an empty-state message.
function renderCryptoSignalsBreadth(){
  const raw = ((DATA.market || {}).poc_top) || [];
  const items = (Array.isArray(raw) ? raw : [])
    .filter(e => e && Array.isArray(e.signal_history) && e.signal_history.length > 0)
    .map(e => ({history: e.signal_history}));
  renderBreadthChart(
    'cryptoSignalsBreadthChart',
    computeSignalBreadth(items, 90),
    'Crypto signal breadth — top 50 by market cap'
  );
}

// CRYPTO SIGNAL SENTIMENT — aggregate signal-score buckets across the top-50
// by market cap (DATA.signals_top20, stablecoins filtered). Mirrors the POC
// sentiment card pattern. Net index = ((BUY+STRONG_BUY) - (SELL+STRONG_SELL))
// / total × 100, clamped to [-100,+100]. Labels: STRONG ACCUMULATION /
// ACCUMULATION / NEUTRAL / DISTRIBUTION / STRONG DISTRIBUTION (mirroring POC).
function renderCryptoSignalsSentiment(){
  const card = document.getElementById('cryptoSignalsSentimentCard');
  if (!card) return;
  const isStable = s => { const u=(s||'').toUpperCase(); return /^USD/.test(u) || /USD$/.test(u) || u==='DAI'; };
  const list = (Array.isArray(DATA.signals_top20) ? DATA.signals_top20 : [])
    .filter(s => s && !isStable(s.symbol));
  if (list.length === 0){
    card.style.display = 'none';
    return;
  }
  card.style.display = '';
  // Bucket each entry by score using the standard thresholds.
  let strongBuy = 0, buy = 0, hold = 0, sell = 0, strongSell = 0;
  for (const s of list){
    const score = Number(s && s.score);
    if (!isFinite(score)) continue;
    if      (score >=  50) strongBuy++;
    else if (score >=  20) buy++;
    else if (score >  -20) hold++;
    else if (score >  -50) sell++;
    else                   strongSell++;
  }
  const buyTotal  = strongBuy + buy;
  const sellTotal = strongSell + sell;
  const total = Math.max(buyTotal + hold + sellTotal, 1);
  const net = Math.round(((buyTotal - sellTotal) / total) * 100);
  const label = net >=  50 ? 'STRONG ACCUMULATION'
              : net >=  20 ? 'ACCUMULATION'
              : net >  -20 ? 'NEUTRAL'
              : net >  -50 ? 'DISTRIBUTION'
              :              'STRONG DISTRIBUTION';
  const color = net >=  20 ? '#22c55e'
              : net <= -20 ? '#ef4444'
              :              '#f59e0b';
  const scoreEl   = document.getElementById('cryptoSignalsSentimentScore');
  const labelEl   = document.getElementById('cryptoSignalsSentimentLabel');
  const sublineEl = document.getElementById('cryptoSignalsSentimentSubline');
  if (scoreEl){
    scoreEl.textContent = (net >= 0 ? '+' : '') + net;
    scoreEl.style.color = color;
  }
  if (labelEl){
    labelEl.textContent = label;
    labelEl.style.color = color;
  }
  if (sublineEl){
    sublineEl.textContent = `${total} coins · positive = broad buy signals · negative = broad sell signals`;
  }
  const pctBuy  = (buyTotal  / total) * 100;
  const pctHold = (hold      / total) * 100;
  const pctSell = (sellTotal / total) * 100;
  const buyBar  = document.getElementById('cryptoSignalsSentimentBarBuy');
  const holdBar = document.getElementById('cryptoSignalsSentimentBarHold');
  const sellBar = document.getElementById('cryptoSignalsSentimentBarSell');
  if (buyBar)  buyBar.style.width  = pctBuy.toFixed(1)  + '%';
  if (holdBar) holdBar.style.width = pctHold.toFixed(1) + '%';
  if (sellBar) sellBar.style.width = pctSell.toFixed(1) + '%';
  const buyCount  = document.getElementById('cryptoSignalsSentimentBuyCount');
  const holdCount = document.getElementById('cryptoSignalsSentimentHoldCount');
  const sellCount = document.getElementById('cryptoSignalsSentimentSellCount');
  if (buyCount)  buyCount.textContent  = String(buyTotal);
  if (holdCount) holdCount.textContent = String(hold);
  if (sellCount) sellCount.textContent = String(sellTotal);
}

function renderSignals(){
  const sigData = DATA.signals || {};
  const top20  = DATA.signals_top20 || [];
  const empty = !sigData.btc && !sigData.eth && !sigData.link && !sigData.ltc && !top20.length;
  document.getElementById('signalsEmpty').classList.toggle('hidden', !empty);
  document.getElementById('signalsContent').classList.toggle('hidden', empty);
  if (empty) return;
  // Sentiment card at the very top of the tab (mirrors POC pattern).
  renderCryptoSignalsSentiment();
  // Breadth chart at the top of the tab (first visible widget).
  renderCryptoSignalsBreadth();
  // Sort cards descending by score so the strongest signals appear first.
  const sortedAssets = Object.entries(sigData)
    .filter(([k, v]) => v && typeof v.score === 'number')
    .sort((a, b) => (b[1].score || 0) - (a[1].score || 0))
    .map(([k]) => k);
  document.getElementById('signalCards').innerHTML =
    sortedAssets.map(a => renderSignalCard(a)).join('');
  renderTop20Signals();
  // Per-coin alternating signal card + history chart pattern for the
  // top 25 by market cap. Replaces the legacy hard-coded BTC/ETH/LINK/
  // LTC 4-chart grid so every top-25 coin gets the full breakdown plus
  // a price chart.
  renderPerCoinSignalList();
}

// Map a signal label to a coarse bucket used by the strip's filter chips
// and the colored chip on each compact card.
function labelBucket(label){
  const L = (label||'').toUpperCase();
  if (L.indexOf('BUY')  >= 0) return 'buy';
  if (L.indexOf('SELL') >= 0) return 'sell';
  return 'hold';
}

// Inline SVG sparkline for the detail modal. Tries the signal's own
// sparkline_7d first (top-50 entries carry this), then falls back to
// the 7-day tail of DATA.market[asset].price for the pinned 4 assets.
// Returns empty string if no usable series found.
function renderSignalSparkline(s){
  const ok = x => typeof x === 'number' && isFinite(x);
  let series = Array.isArray(s.sparkline_7d) ? s.sparkline_7d.filter(ok) : null;
  if (!series || series.length < 5){
    const lower = (s.symbol||'').toLowerCase();
    const main = (DATA.market||{})[lower];
    if (main && Array.isArray(main.price)){
      series = main.price.slice(-7).map(p => p.value).filter(ok);
    }
  }
  if (!series || series.length < 5) return '';
  const W = 640, H = 120, pad = 6;
  const min = Math.min(...series);
  const max = Math.max(...series);
  const range = max - min || 1;
  const step = (W - pad*2) / (series.length - 1);
  const yFor = v => pad + (H - pad*2) * (1 - (v - min) / range);
  const pts = series.map((v, i) => `${pad + i*step},${yFor(v).toFixed(1)}`).join(' ');
  const first = series[0], last = series[series.length-1];
  const up = last >= first;
  const color = up ? '#22c55e' : '#ef4444';
  const fillColor = up ? '#22c55e22' : '#ef444422';
  const area = `${pad},${H-pad} ${pts} ${pad + (series.length-1)*step},${H-pad}`;
  const pctChg = (last - first) / first * 100;
  const pctTxt = (pctChg >= 0 ? '+' : '') + pctChg.toFixed(2) + '%';
  return `
    <div style="background:#0e1118;border:1px solid var(--border);border-radius:6px;padding:6px 10px;margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;align-items:baseline;font-size:11px">
        <span class="sub" style="color:var(--muted)">Price · ${series.length === 168 ? '168h hourly' : series.length + 'd daily'} sparkline</span>
        <span style="color:${color};font-weight:600">${pctTxt} over window</span>
      </div>
      <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:120px;display:block;margin-top:4px">
        <polygon points="${area}" fill="${fillColor}"/>
        <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5"/>
      </svg>
    </div>`;
}

// Render the full signal-card breakdown from a raw signals_top20 entry.
// Mirrors renderSignalCard(asset) but keys off the object directly so the
// modal works for any coin, not just the four pinned in DATA.signals.
function renderSignalCardFromObj(s){
  if (!s) return '<div class="chart-card"><div class="empty">No data available.</div></div>';
  const color = signalColor(s.score);
  const sym = (s.symbol||'').toUpperCase();
  const compRows = (s.components||[]).map(c => {
    const cls = c.contribution > 0 ? 'green' : (c.contribution < 0 ? 'red' : 'amber');
    const sign = (c.contribution>=0?'+':'') + c.contribution;
    return `<tr><td>${escapeHtml(c.name)}</td><td>${escapeHtml(String(c.value))}</td><td class="${cls}">${sign}</td><td style="color:var(--muted);font-size:12px">${escapeHtml(c.explanation||'')}</td></tr>`;
  }).join('');
  const pct = ((s.score + 100) / 200) * 100;
  const priceStr = (s.price != null)
    ? '$' + Number(s.price).toLocaleString(undefined, {maximumFractionDigits: s.price>=1?2:6})
    : '—';
  return `
    <div class="chart-card" style="position:relative">
      <div class="head" style="align-items:flex-start">
        <div>
          <h2 style="font-size:15px">${sym} signal <span class="tag">${priceStr}</span></h2>
          <div class="desc">${escapeHtml(s.name||'')} · as of ${escapeHtml(s.as_of||'')}</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:28px;font-weight:700;color:${color}">${escapeHtml(s.label||'')}</div>
          <div style="font-size:13px;color:var(--muted)">score <strong style="color:${color}">${s.score>=0?'+':''}${s.score}</strong> / ±100</div>
        </div>
      </div>
      <div style="height:10px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:5px;position:relative;margin:8px 0">
        <div style="position:absolute;top:-4px;left:calc(${pct.toFixed(1)}% - 4px);width:8px;height:18px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
      </div>
      ${renderSignalSparkline(s)}
      <table style="margin-top:6px"><thead><tr><th>Component</th><th>Value</th><th>±</th><th>Read</th></tr></thead><tbody>${compRows}</tbody></table>
      <div class="sub" style="margin-top:8px;font-size:11px">${escapeHtml(s.disclaimer||'')}</div>
    </div>`;
}

function renderTop20Signals(){
  const host = document.getElementById('top20SignalCards');
  if (!host) return;
  const isStable = s => { const u=(s||'').toUpperCase(); return /^USD/.test(u) || /USD$/.test(u) || u==='DAI'; };
  // Per user request, trim the full signals_top20 (which is actually top 50)
  // down to top 25 by score so the grouped sections + breadth chart stay
  // tight. Stablecoins are filtered before the slice so they don't burn a
  // slot. The breadth chart at the top of the tab still uses the full 50
  // for its time-series — only THIS card grid is trimmed.
  const all = (DATA.signals_top20 || [])
    .filter(s => s && !isStable(s.symbol))
    .slice().sort((a,b) => (b.score||0) - (a.score||0))
    .slice(0, 25);
  if (!all.length){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:8px">No top-20 signals yet — refresh.</div>';
    return;
  }
  window._top20SignalsCache = {};
  // Build a single compact card. Uses stockLabelBucket so the bucket key
  // matches the section grouping AND the filter chips (data-top20filter).
  const cardHtml = s => {
    const sym = (s.symbol||'').toUpperCase();
    const color = signalColor(s.score);
    const bucket = stockLabelBucket(s.label);
    const img = sanitizeUrl(s.image, '')
      ? `<img src="${sanitizeUrl(s.image, '')}" alt="" style="width:32px;height:32px;border-radius:50%">`
      : `<div style="width:32px;height:32px;border-radius:50%;background:${color}33"></div>`;
    const score = (s.score>=0?'+':'') + s.score;
    window._top20SignalsCache[sym] = s;
    // Price formatting — auto-scales decimal places for small-cap coins
    const priceStr = (s.price != null)
      ? '$' + Number(s.price).toLocaleString(undefined, {maximumFractionDigits: s.price>=1000?0:s.price>=1?2:6})
      : '';
    return `<div class="card" data-symbol="${sym}" data-bucket="${bucket}" role="button" tabindex="0" aria-label="Open ${sym} signal detail" style="cursor:pointer;padding:8px 10px;display:flex;align-items:center;gap:10px;min-height:80px;max-height:100px;border-left:3px solid ${color};transition:transform .08s ease,background .08s ease">
      ${img}
      <div style="flex:1;min-width:0;overflow:hidden">
        <div style="display:flex;align-items:baseline;gap:6px;flex-wrap:wrap">
          <span style="font-weight:700;font-size:13px">${escapeHtml(sym)}</span>
          ${priceStr ? `<span style="font-size:11px;color:var(--text);font-variant-numeric:tabular-nums">${priceStr}</span>` : ''}
        </div>
        <div class="sub" style="color:var(--muted);font-size:11px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden">${escapeHtml(s.name||'')}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:13px;font-weight:700;color:${color};line-height:1.1">${escapeHtml(s.label||'')}</div>
        <div style="font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums">${score} / ±100</div>
      </div>
    </div>`;
  };
  // Group by bucket so we can render section headers above each sub-grid.
  // Mirrors renderStocksTab(): same five buckets, same glyphs/colors, same
  // data-* hook names so the filter chips can hide whole sections too.
  const byBucket = {strong_buy:[], buy:[], hold:[], sell:[], strong_sell:[]};
  all.forEach(s => {
    const b = stockLabelBucket(s.label);
    if (byBucket[b]) byBucket[b].push(s);
    else byBucket.hold.push(s);
  });
  const sections = [
    {key:'strong_buy',  glyph:'🔥', label:'STRONG BUY',  color:'#16a34a'},
    {key:'buy',         glyph:'✓',  label:'BUY',         color:'#22c55e'},
    {key:'hold',        glyph:'◯',  label:'HOLD',        color:'#f59e0b'},
    {key:'sell',        glyph:'↓',  label:'SELL',        color:'#ef4444'},
    {key:'strong_sell', glyph:'⛔', label:'STRONG SELL', color:'#b91c1c'},
  ];
  // Outer #top20SignalCards is an auto-fit grid, so each section becomes a
  // column on laptop widths. Previously every empty bucket consumed a full
  // column with body grid + "No coins" copy, so on a typical day (only HOLD
  // populated) the layout was 80% whitespace. Empty sections now collapse
  // to a single-row header pill that spans the full grid width via
  // grid-column:1/-1 — auto-fit redistributes the populated sections across
  // the remaining columns, while users still see at a glance which buckets
  // are empty today (and the filter chips still target them correctly).
  const allEmpty = sections.every(sec => (byBucket[sec.key] || []).length === 0);
  if (allEmpty){
    host.innerHTML = `<div class="sub" style="color:var(--muted);padding:24px;text-align:center;grid-column:1/-1">No signals available yet.</div>`;
  } else {
    host.innerHTML = sections.map(sec => {
      const items = byBucket[sec.key] || [];
      const n = items.length;
      if (n === 0){
        // Compact one-line pill, full-width. Layout lives in the
        // .signals-empty-pill class (see CSS) so the filter chip's
        // `style.display = ''` reset doesn't collapse the row to a block.
        return `<div class="signals-section signals-empty-pill" data-signals-section="${sec.key}" data-empty="1">
          <span aria-hidden="true">${escapeHtml(sec.glyph)}</span>
          <span style="font-weight:700;letter-spacing:0.2px;color:${sec.color}">${escapeHtml(sec.label)}</span>
          <span>0 today</span>
        </div>`;
      }
      const cards = items.map(cardHtml).join('');
      return `<div class="signals-section" data-signals-section="${sec.key}" style="margin-bottom:12px">
        <div style="display:flex;align-items:center;gap:8px;margin:0 0 6px 0">
          <h3 style="margin:0;font-size:13px;font-weight:700;letter-spacing:0.2px;color:var(--text)">
            <span aria-hidden="true">${escapeHtml(sec.glyph)}</span>
            ${escapeHtml(sec.label)}
            <span style="color:var(--muted);font-weight:500;margin-left:4px">(${n})</span>
          </h3>
          <span class="tag" style="background:${sec.color}22;color:${sec.color};border:1px solid ${sec.color}66;padding:1px 8px;border-radius:10px;font-size:10px;font-weight:700">${escapeHtml(sec.label)}</span>
        </div>
        <div class="row signals-section-grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px">${cards}</div>
      </div>`;
    }).join('');
  }
  // Bind click → modal
  host.querySelectorAll('[data-symbol]').forEach(el =>
    el.addEventListener('click', () => openSignalDetail(el.getAttribute('data-symbol')))
  );
  // Re-apply the active filter chip so section visibility matches selection
  // on re-render (e.g. after data refresh while a non-"all" chip is active).
  applyTop20Filter();
}

// Per-coin alternating layout: for each of the top 25 by market cap, append
// two stacked blocks to #perCoinSignalList:
//   A) The rich signal card (via renderSignalCardFromObj) — score, label,
//      component breakdown table, inline sparkline.
//   B) A history chart card. If the coin has a 90d signal/price history in
//      DATA.market.poc_top (joined by uppercase symbol), draw a score+price
//      overlay. Otherwise fall back to a 7-day price-only sparkline from
//      s.sparkline_7d.
// Clicks on either block open the existing signal-detail modal.
function renderPerCoinSignalList(){
  const host = document.getElementById('perCoinSignalList');
  if (!host) return;
  const isStable = s => { const u=(s||'').toUpperCase(); return /^USD/.test(u) || /USD$/.test(u) || u==='DAI'; };
  // Sort by rank ascending so top 25 BY MARKET CAP appear (matches the
  // user's stated intent — "all top-25 coins"). The strip above sorts by
  // score, so the two surfaces complement rather than duplicate.
  const top25 = (DATA.signals_top20 || [])
    .filter(s => s && !isStable(s.symbol))
    .slice()
    .sort((a,b) => (a.rank||999) - (b.rank||999))
    .slice(0, 25);
  if (!top25.length){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:8px">No per-coin signals yet — refresh.</div>';
    return;
  }
  // Build a lookup of poc_top entries by uppercase symbol so we can pull
  // 90d signal_history per coin without an extra fetch. DATA.market.poc_top
  // is the same source the breadth chart consumes.
  const pocBySym = {};
  ((DATA.market || {}).poc_top || []).forEach(p => {
    const sym = (p && p.symbol || '').toUpperCase();
    if (sym) pocBySym[sym] = p;
  });
  // Cache top-25 entries in the same global the strip uses so click→modal
  // works for these cards too (openSignalDetail reads _top20SignalsCache).
  window._top20SignalsCache = window._top20SignalsCache || {};
  // Build the markup in one pass, then chart-draw in a second pass once
  // the canvases are in the DOM.
  const chunks = [];
  top25.forEach(s => {
    const sym = (s.symbol || '').toUpperCase();
    window._top20SignalsCache[sym] = s;
    const poc = pocBySym[sym];
    const hasHist = poc && Array.isArray(poc.signal_history) && poc.signal_history.length >= 5;
    const chartTitle = hasHist
      ? `${sym} signal history (90d)`
      : `${sym} price (7d)`;
    const chartDesc = hasHist
      ? 'Score &middot; click for full breakdown'
      : 'Recent price trend · click for full breakdown';
    // Block A: rich signal card (score + components).
    chunks.push(
      `<div data-per-coin-symbol="${escapeHtml(sym)}" role="button" tabindex="0" aria-label="Open ${escapeHtml(sym)} signal detail" style="cursor:pointer" title="Click to open ${escapeHtml(sym)} signal detail">` +
      renderSignalCardFromObj(s) +
      `</div>`
    );
    // Block B: history/price chart card.
    chunks.push(
      `<div class="chart-card" data-per-coin-symbol="${escapeHtml(sym)}" role="button" tabindex="0" aria-label="Open ${escapeHtml(sym)} signal detail" style="cursor:pointer" title="Click to open ${escapeHtml(sym)} signal detail">
        <div class="head"><h2>${escapeHtml(chartTitle)}</h2><span class="desc">${chartDesc}</span></div>
        <div class="chart-wrap"><canvas id="perCoinChart-${escapeHtml(sym)}"></canvas></div>
      </div>`
    );
  });
  host.innerHTML = chunks.join('');
  // Second pass: draw the canvas chart for each card. Destroy any prior
  // instance under the same key so re-renders don't leak Chart.js handles.
  top25.forEach(s => {
    const sym = (s.symbol || '').toUpperCase();
    const canvas = document.getElementById('perCoinChart-' + sym);
    if (!canvas) return;
    const chartKey = 'perCoin_' + sym;
    destroy(chartKey);
    const poc = pocBySym[sym];
    const hasHist = poc && Array.isArray(poc.signal_history) && poc.signal_history.length >= 5;
    const accent = signalColor(Number(s.score) || 0);
    if (hasHist){
      // 90-day score history. If poc_top includes a price array on the
      // same date keys we'd overlay it too — current shape doesn't, so
      // we draw score-only at full width.
      const hist = poc.signal_history;
      const labels = hist.map(r => r.date);
      const scores = hist.map(r => Number(r.score));
      charts[chartKey] = new Chart(canvas, {
        type:'line',
        data:{labels, datasets:[
          {label:'Score', data:scores, borderColor:'#a78bfa', backgroundColor:'#a78bfa22', fill:true, tension:0.2, pointRadius:0, borderWidth:2},
        ]},
        options:{
          responsive:true, maintainAspectRatio:false,
          plugins:{legend:{labels:{color:'#e6e8ee'}}, tooltip:{mode:'index',intersect:false}},
          scales:{
            x:{ticks:{color:'#8a93a6',maxTicksLimit:10},grid:{color:'#1f2533'}},
            y:{min:-100,max:100,title:{display:true,text:'Score',color:'#8a93a6'},ticks:{color:'#8a93a6'},grid:{color:'#1f2533'}},
          },
        },
      });
    } else {
      // 7-day hourly price sparkline. sparkline_7d is ~168 hourly points;
      // we render as a simple line with synthetic positional labels (no
      // axes ticks) — sticking with Chart.js keeps the chart-card height
      // consistent with the score-history charts above.
      const series = (Array.isArray(s.sparkline_7d) ? s.sparkline_7d : [])
        .map(Number)
        .filter(v => isFinite(v));
      if (series.length < 5){
        // Defensive: poc-less coin with no usable sparkline. Leave the
        // canvas blank rather than throwing.
        return;
      }
      const labels = series.map((_, i) => i);
      const up = series[series.length-1] >= series[0];
      const lineColor = up ? '#22c55e' : '#ef4444';
      const fillColor = up ? '#22c55e22' : '#ef444422';
      charts[chartKey] = new Chart(canvas, {
        type:'line',
        data:{labels, datasets:[
          {label:'Price', data:series, borderColor:lineColor, backgroundColor:fillColor, fill:true, tension:0.25, pointRadius:0, borderWidth:1.5},
        ]},
        options:{
          responsive:true, maintainAspectRatio:false,
          plugins:{legend:{display:false}, tooltip:{mode:'index',intersect:false,callbacks:{title:()=>'',label:c=>'$'+Number(c.raw).toLocaleString(undefined,{maximumFractionDigits:c.raw>=1?2:6})}}},
          scales:{
            x:{display:false},
            y:{ticks:{color:'#8a93a6',callback:v=>fmtUSD(v,'auto')},grid:{color:'#1f2533'}},
          },
        },
      });
    }
  });
  // Wire clicks on either block (signal card or chart card) → modal.
  host.querySelectorAll('[data-per-coin-symbol]').forEach(el =>
    el.addEventListener('click', () => openSignalDetail(el.getAttribute('data-per-coin-symbol')))
  );
}

function openSignalDetail(sym){
  // Unified entry point — every click that previously opened a signal-only
  // modal (Top-25 by market cap, Top-50 signals strip, per-coin sentiment
  // cards, signal-history chart cards) now routes through `lookupSymbol`
  // so the user always gets the universal Signal + POC pair side-by-side.
  //
  // `lookupSymbol` handles all the same fallbacks the old function did:
  //   * `signals_top20` (top-50 strip)
  //   * `DATA.signals[sym]` for pinned BTC/ETH/LINK/LTC
  //   * Stock cards (when ticker is in stocks_signals)
  //   * Live lookup chain for unknown symbols (with fuzzy-suggest chips)
  //
  // For symbols outside `poc_top`, the POC slot renders an empty-state
  // card instead of being absent — consistent with the symbol-search UX.
  if (!sym) return;
  if (typeof lookupSymbol === 'function') lookupSymbol(String(sym));
}
// (closeSignalDetail removed — the legacy #signalDetailModal element was
// retired so there's nothing for this function to close. Click + keydown
// handlers that referenced it were also removed in wireTop20Modals below.)

// Apply the active Top-50 filter chip — hides both whole sections and any
// individual cards whose bucket doesn't match. Chip semantics:
//   all  → all 5 sections visible
//   buy  → STRONG BUY + BUY only
//   hold → HOLD only
//   sell → SELL + STRONG SELL only
// Idempotent — safe to call on every re-render so post-refresh state matches
// whatever chip is currently active.
function applyTop20Filter(bucket){
  let target = bucket;
  if (target == null){
    // Read from the chip with .active (single source of truth — chips are
    // wired by wireTop20Modals below). Default to 'all' if none is active.
    const activeChip = document.querySelector('[data-top20filter].active');
    target = activeChip ? activeChip.getAttribute('data-top20filter') : 'all';
  }
  // Buckets the active chip covers. 'buy' covers strong_buy too; 'sell'
  // covers strong_sell. 'all' covers everything.
  const allowed = target === 'all'  ? null
                : target === 'buy'  ? new Set(['strong_buy', 'buy'])
                : target === 'sell' ? new Set(['sell', 'strong_sell'])
                : target === 'hold' ? new Set(['hold'])
                : null;
  document.querySelectorAll('#top20SignalCards [data-signals-section]').forEach(sec => {
    sec.style.display = (!allowed || allowed.has(sec.getAttribute('data-signals-section'))) ? '' : 'none';
  });
  document.querySelectorAll('#top20SignalCards [data-symbol]').forEach(c => {
    c.style.display = (!allowed || allowed.has(c.getAttribute('data-bucket'))) ? '' : 'none';
  });
}

// One-time wiring for the detail modal + filter chips + POC explainer. Idempotent.
(function wireTop20Modals(){
  if (window._top20Wired) return; window._top20Wired = true;
  document.addEventListener('click', e => {
    const fb = e.target && e.target.closest && e.target.closest('[data-top20filter]');
    if (fb){
      const bucket = fb.getAttribute('data-top20filter');
      fb.parentElement.querySelectorAll('[data-top20filter]').forEach(b => b.classList.toggle('active', b===fb));
      applyTop20Filter(bucket);
    }
    // Click on any signal history chart-card → open the detail modal for
    // that asset. Uses the generalized openSignalDetail which falls back
    // to DATA.signals when the symbol isn't in the top-50 cache.
    const sigChart = e.target && e.target.closest && e.target.closest('[data-sig-asset]');
    if (sigChart) openSignalDetail(sigChart.getAttribute('data-sig-asset'));
  });
  // Keyboard activation for every clickable coin card across the dashboard.
  // Cards already carry role="button" + tabindex="0" + the appropriate
  // data-* attribute; one delegated handler keeps Enter/Space working
  // everywhere without per-renderer duplication. Skips elements with
  // their own keydown logic (form inputs, the symbol-search box, etc).
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const t = e.target;
    if (!t || typeof t.getAttribute !== 'function') return;
    // Don't steal Enter from form inputs / textareas / buttons that have
    // their own behaviour.
    const tag = (t.tagName || '').toUpperCase();
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    const sym = t.getAttribute('data-symbol') || t.getAttribute('data-per-coin-symbol');
    if (sym){
      e.preventDefault();
      openSignalDetail(sym);
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      const pm = document.getElementById('pocExplainerModal');
      if (pm && !pm.classList.contains('hidden')) pm.classList.add('hidden');
    }
  });
  // POC explainer: opened from (a) any legacy TrendSpider link, and
  // (b) the POC-tab "?" / "Learn about POC" buttons via data-poc-help.
  // Single delegated listener so dynamically rendered triggers also work.
  const pocModal = document.getElementById('pocExplainerModal');
  if (pocModal){
    const openPoc = e => { if (e) e.preventDefault(); pocModal.classList.remove('hidden'); };
    const closePoc = () => pocModal.classList.add('hidden');
    document.querySelectorAll('a[href*="trendspider.com"]').forEach(a => {
      a.addEventListener('click', openPoc);
      a.setAttribute('href', '#');
      a.setAttribute('title', 'What is POC?');
    });
    document.addEventListener('click', e => {
      const trig = e.target && e.target.closest && e.target.closest('[data-poc-help]');
      if (trig) openPoc(e);
    });
    document.getElementById('pocExplainerClose')?.addEventListener('click', closePoc);
    pocModal.addEventListener('click', e => { if (e.target.id === 'pocExplainerModal') closePoc(); });
  }
  // POC overlay (Trading-tab price chart): checkbox toggle + 30d/90d chips.
  const pocCb   = document.getElementById('pocOverlayToggle');
  const pocWrap = document.getElementById('pocWinChips');
  const setWinUI = () => {
    if (!pocWrap) return;
    pocWrap.querySelectorAll('button[data-pocwin]').forEach(b =>
      b.classList.toggle('active', b.dataset.pocwin === pocOverlay.win));
  };
  if (pocCb && pocWrap){
    pocCb.checked = pocOverlay.on;
    pocWrap.style.display = pocOverlay.on ? 'inline-flex' : 'none';
    setWinUI();
    pocCb.addEventListener('change', () => {
      pocOverlay.on = pocCb.checked;
      try { localStorage.setItem('tradingShowPoc', pocOverlay.on ? '1' : '0'); } catch(_) {}
      pocWrap.style.display = pocOverlay.on ? 'inline-flex' : 'none';
      renderPriceVol();
    });
    pocWrap.addEventListener('click', e => {
      const b = e.target.closest('button[data-pocwin]'); if (!b) return;
      pocOverlay.win = b.dataset.pocwin;
      try { localStorage.setItem('tradingPocWin', pocOverlay.win); } catch(_) {}
      setWinUI();
      renderPriceVol();
    });
  }
})();

// ---------- Whale tab ----------
function whaleData(){ return (DATA.whale||{}).btc || {}; }

// Render the Whale Sentiment Index headline card at the top of the Whale
// tab. Reads market.whale.sentiment which is computed Python-side in
// fetch_market.compute_whale_sentiment(). Same gauge pattern as the
// asset signal cards.
function renderWhaleSentiment(){
  const s = (DATA.whale || {}).sentiment;
  const host = document.getElementById('whaleSentimentCard');
  if (!host) return;
  if (!s){
    host.innerHTML = '<div class="sub" style="color:var(--muted)">No whale sentiment data yet — waiting on first fetch.</div>';
    return;
  }
  const color = signalColor(s.score);
  const pct = ((s.score + 100) / 200) * 100;
  const compRows = (s.components||[]).map(c => {
    const cls = c.contribution > 0 ? 'green' : (c.contribution < 0 ? 'red' : 'amber');
    const sign = (c.contribution>=0?'+':'') + c.contribution;
    return `<tr><td>${escapeHtml(c.name)}</td><td>${escapeHtml(String(c.value))}</td><td class="${cls}">${sign}</td><td style="color:var(--muted);font-size:12px">${escapeHtml(c.explanation||'')}</td></tr>`;
  }).join('');
  host.innerHTML = `
    <div class="head" style="align-items:flex-start">
      <div>
        <h2 style="font-size:15px">🐋 Whale Sentiment Index</h2>
        <div class="desc">Composite ±100 from on-chain proxies · as of ${escapeHtml(s.as_of||'?')}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:26px;font-weight:700;color:${color}">${escapeHtml(s.label||'')}</div>
        <div style="font-size:13px;color:var(--muted)">score <strong style="color:${color}">${s.score>=0?'+':''}${s.score}</strong> / ±100</div>
      </div>
    </div>
    <div style="height:10px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:5px;position:relative;margin:8px 0">
      <div style="position:absolute;top:-4px;left:calc(${pct.toFixed(1)}% - 4px);width:8px;height:18px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
    </div>
    <table style="margin-top:6px"><thead><tr><th>Component</th><th>Value</th><th>±</th><th>Read</th></tr></thead><tbody>${compRows}</tbody></table>
    <div class="sub" style="margin-top:8px;font-size:11px">${escapeHtml(s.disclaimer||'')}</div>
  `;
}

// LTHCS Composite Index panel — GUIDED NARRATIVE variant. Promoted from
// /lthcs/ on 2026-05-20 (mockups/revamp-B-narrative). Renders Step 1
// (verdict + gauge + band legend) + Step 2 (9 components with plain-
// English glosses + inline <details> popovers for jargon). V1's existing
// "About LTHCS" disclosure already covers Step 4 (how to read this), and
// the Insights row above covers Step 3 (why it matters) — so this
// in-V1 surface stays compact.
//
// Mounted at #lthcsCompositeCard (Overview tab) and #stocksLthcsCompositeCard
// (Stocks tab). The original wide-table renderer `renderLthcsCompositePanel`
// is kept directly below this one as a one-flip rollback.
function renderLthcsNarrativePanel(host){
  if (!host) return;
  const L = (DATA.lthcs || {});
  const idx = L.index || null;
  const link = '<a href="lthcs/" target="_blank" rel="noopener" style="color:#a78bfa;text-decoration:none;font-weight:600">Open full LTHCS dashboard →</a>';
  if (!L.available || !idx){
    host.innerHTML = `
      <div class="head" style="align-items:flex-start">
        <div>
          <h2 style="font-size:15px">📊 LTHCS Composite Index</h2>
          <div class="desc">Data populates on next daily pipeline run</div>
        </div>
      </div>
      <div class="sub" style="color:var(--muted);padding:8px 0">
        LTHCS Composite Index — data not yet available. The daily pipeline writes
        <code>data/lthcs/index/&lt;date&gt;.json</code>; this panel will fill in on the next run.
      </div>
      <div style="margin-top:8px">${link}</div>
    `;
    return;
  }

  // ---- Inputs
  const score = Number(idx.score) || 0;
  const tone = lthcsBandColor(idx.band_key) || idx.color || signalColor(score);
  const pct = ((Math.max(-100, Math.min(100, score)) + 100) / 200) * 100;
  const rawLabel = idx.label || 'NEUTRAL';
  const label = String(rawLabel).replace(/^LTHCS\s+/i, '').toUpperCase();
  const asOf = idx.as_of || L.as_of || '—';
  const components = Array.isArray(idx.components) ? idx.components : [];

  // ---- Plain-English gloss tied to verdict band
  const glossByLabel = {
    'ELITE':       'Broad-based strength — pillar averages, band lean, and macro all leaning the same way. The universe looks healthy for long-term holders.',
    'CONSTRUCTIVE':'More green than red, but mixed. Some signals are firming while others are catching up. Constructive backdrop, not all-clear.',
    'NEUTRAL':     'The universe is leaning slightly cautious today — more names softening than firming, but no clean directional bias yet. Worth watching the components below.',
    'WEAKENING':   'More red than green. Pillars or macro are weakening across the universe. Not a panic signal, but the burden of proof is on the bulls.',
    'DISTRIBUTING':'Broad weakness across pillars and macro. Time to re-underwrite holdings rather than add risk.'
  };
  const verdictGloss = glossByLabel[label] || 'A daily directional read on long-term-hold sentiment across the universe.';

  // ---- Per-component gloss + jargon popover meta (source: lthcs_help)
  const COMP_META = {
    'Band lean (bullish % minus bearish %)': { gloss: 'Of every 168 names we track, what share is in the top 3 bands vs. the bottom 2.', term: 'band lean', def: '% of the universe in the top three bands (Elite + High + Constructive) minus % in the bottom two (Weakening + Review). Positive = more strong names than weak ones.' },
    'Adoption pillar avg':                    { gloss: 'Average of the "who actually uses or holds this" score across all names.', term: 'Adoption pillar', def: 'Product traction, user/holder growth, network footprint. Built from retail-app downloads, employment growth, Wikipedia pageview trend.' },
    'Institutional pillar avg':               { gloss: 'Average of the "what sophisticated owners are doing" score.', term: 'Institutional pillar', def: 'Form 4 insider net buys, 13F qtr-over-qtr deltas, ETF AUM trend, put/call posture. Captures whether smart money is adding or trimming.' },
    'Financial pillar avg':                   { gloss: 'Average of the "can this business fund itself" score.', term: 'Financial pillar', def: 'TTM free cash flow yield, net cash, dividend coverage, buyback authorization remaining.' },
    'Thesis pillar avg':                      { gloss: 'Average of the "what the market thinks of the story" score. Often neutral in V1.', term: 'Thesis pillar', def: 'EPS revision breadth, price-target deltas, multi-timeframe trend posture. Falls back to neutral 50 when free-tier sentiment data is missing.' },
    'DES (demand environment) avg':           { gloss: 'Average of Demand-vs-Earnings Strength: is the run-up earned, or all multiple expansion?', term: 'DES', def: 'Demand-vs-Earnings Strength. Compares trailing return against trailing EPS growth, sector-relative. Negative = price ran ahead of earnings.' },
    'Macro regime (HY OAS / curve / USD)':    { gloss: 'Risk-on / risk-off composite from credit spreads, the yield curve, and the dollar.', term: 'Macro regime', def: 'HY OAS (junk-bond spreads), 2s10s (Treasury curve), and trade-weighted USD. Positive = risk-on backdrop that lifts long-duration assets.' },
    'Insider conviction breadth':             { gloss: 'Across the universe, are insiders net buying or net selling?', term: 'Form 4', def: 'SEC filing insiders submit when they buy or sell their own company\'s stock. This signal counts net-buy minus net-sell breadth across all names.' },
    '13F conviction breadth (acc vs dist)':   { gloss: 'Across the universe, are institutions accumulating or distributing? Lags one quarter.', term: '13F', def: 'Quarterly SEC filing from funds >$100M reporting their long equity holdings. Filed ~45 days after quarter-end, so this signal lags actual positioning.' }
  };

  // ---- Band legend (Step 1 footer): 5 cells, highlight the one we're in
  const legendActive = { 'ELITE':'elite','CONSTRUCTIVE':'constructive','NEUTRAL':'neutral','WEAKENING':'weakening','DISTRIBUTING':'distributing' }[label] || 'neutral';
  const legendCells = [
    { key: 'distributing', name: 'Distributing', range: '≤ −60' },
    { key: 'weakening',    name: 'Weakening',    range: '−60 to −30' },
    { key: 'neutral',      name: 'Neutral',      range: '−30 to +30' },
    { key: 'constructive', name: 'Constructive', range: '+30 to +60' },
    { key: 'elite',        name: 'Elite',        range: '≥ +60' }
  ].map(cell => {
    const on = cell.key === legendActive;
    return `<div style="padding:6px 4px;border-radius:6px;background:${on?tone:'var(--card)'};color:${on?'#0b0d12':'var(--muted)'};border:1px solid ${on?'var(--text)':'transparent'};text-align:center">
      <div style="font-weight:600;font-size:10px">${cell.name}</div>
      <div style="font-family:monospace;font-size:10px;opacity:0.85">${cell.range}</div>
    </div>`;
  }).join('');

  // ---- Step 2: components rendered as B-style cards (not a table)
  const compsHtml = components.map(c => {
    const meta = COMP_META[c.name] || { gloss: '', term: null, def: '' };
    const d = Number(c.delta) || 0;
    const deltaColor = d > 0 ? '#22c55e' : (d < 0 ? '#ef4444' : 'var(--muted)');
    const dStr = (d >= 0 ? '+' : '') + d;
    const cap = /^Band lean/i.test(c.name) ? 30 : 10;
    const fillPct = Math.min(Math.abs(d)/cap, 1) * 50;
    const fillSide = d >= 0 ? `left:50%` : `right:50%`;
    const fillColor = d >= 0 ? '#22c55e' : '#ef4444';

    // Wrap the jargon term with an inline <details> popover. !important
    // on display defends against Safari's UA default of display:block on
    // <details>/<summary> which otherwise breaks the inline name.
    let nameHtml = escapeHtml(c.name);
    if (meta.term){
      const re = new RegExp('(' + meta.term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'i');
      nameHtml = escapeHtml(c.name).replace(re,
        '<details class="lthcs-nar-term" style="display:inline !important;position:relative">' +
          '<summary style="display:inline !important;list-style:none;cursor:help;font-family:monospace;background:var(--bg);padding:1px 6px;border-radius:4px;font-size:11px;color:var(--muted);border-bottom:1px dotted #a78bfa">$1</summary>' +
          '<div role="note" style="position:absolute;left:0;top:1.6rem;z-index:50;width:min(280px,90vw);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px;box-shadow:0 6px 18px rgba(0,0,0,0.45);font-weight:400;font-size:12px;color:var(--muted);line-height:1.45;font-style:normal;text-transform:none;letter-spacing:normal">' +
            '<strong style="color:var(--text);display:block;margin-bottom:3px">' + escapeHtml(meta.term) + '</strong>' +
            meta.def +
          '</div>' +
        '</details>');
    }

    const valueStr = (c.value == null || c.value === '') ? '' : String(c.value);
    const valueHtml = valueStr
      ? `<span style="font-family:monospace;font-size:11px;font-weight:500;color:var(--muted);margin-left:6px;font-variant-numeric:tabular-nums">${escapeHtml(valueStr)}</span>`
      : '';
    const glossHtml = meta.gloss
      ? `<span style="color:var(--muted);margin:0 5px">·</span><span style="color:var(--muted);font-style:italic;font-size:11px">${meta.gloss}</span>`
      : '';

    return `<div style="background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:8px 12px;margin-bottom:6px">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap">
        <div style="font-size:13px;font-weight:600;color:var(--text)">${nameHtml}${valueHtml}</div>
        <div style="font-family:monospace;font-weight:700;font-size:12px;padding:1px 7px;border-radius:5px;background:var(--card);color:${deltaColor};font-variant-numeric:tabular-nums">${dStr}</div>
      </div>
      <p style="font-size:12px;margin:4px 0 0 0;color:var(--muted);line-height:1.4">${escapeHtml(c.read || '')}${glossHtml}</p>
      <div style="position:relative;height:4px;background:var(--card);border-radius:2px;margin-top:6px;overflow:hidden">
        <div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--border)"></div>
        <div style="position:absolute;top:0;bottom:0;${fillSide};width:${fillPct.toFixed(1)}%;background:${fillColor}"></div>
      </div>
    </div>`;
  }).join('');

  // ---- Existing V1 features preserved: movers row + dashboard CTA
  const moversRow = renderLthcsMoversRow(L.movers || {});

  // Small top-right CTA chip — mirrors the bottom dashboard link so users
  // can deep-link from the head without scrolling. Smaller than a standalone
  // button row above the card (which ate ~40px of vertical space).
  const headerCta = '<a href="lthcs/" target="_blank" rel="noopener" ' +
    'style="display:inline-flex;align-items:center;gap:3px;color:#a78bfa;' +
    'text-decoration:none;font-weight:600;font-size:11px;white-space:nowrap;' +
    'padding:3px 8px;border:1px solid #a78bfa;border-radius:999px;' +
    'background:rgba(167,139,250,0.08)">Open full LTHCS &rarr;</a>';

  host.innerHTML = `
    <div class="head" style="align-items:flex-start;justify-content:space-between">
      <div>
        <h2 style="font-size:15px">📊 LTHCS Composite Index</h2>
        <div class="desc">Where is the long-term-hold market? · as of ${escapeHtml(asOf)}</div>
      </div>
      <div style="flex-shrink:0">${headerCta}</div>
    </div>

    <!-- STEP 1: The big picture -->
    <div style="background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px 20px;margin:10px 0 12px 0">
      <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
        <div style="width:30px;height:30px;border-radius:50%;background:${tone};color:#0b0d12;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:14px;flex-shrink:0">1</div>
        <div>
          <h3 style="font-size:15px;margin:0;color:var(--text)">The big picture</h3>
          <div style="font-size:11px;color:var(--muted);margin-top:1px">Where is the market leaning right now?</div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:20px;flex-wrap:wrap;margin-bottom:10px">
        <div style="font-family:monospace;font-size:clamp(48px,8vw,72px);font-weight:700;line-height:1;color:${tone};font-variant-numeric:tabular-nums">${score>=0?'+':''}${score}</div>
        <div style="flex:1 1 280px">
          <div style="text-transform:uppercase;letter-spacing:0.1em;font-size:13px;font-weight:700;color:${tone}">${escapeHtml(label)}</div>
          <p style="font-size:14px;margin:6px 0;color:var(--text);line-height:1.5">${verdictGloss}</p>
          <div style="font-size:11px;color:var(--muted)">One number, scale −100 to +100. Computed daily at 23:00 UTC from 9 underlying signals.</div>
        </div>
      </div>
      <div style="position:relative;margin:12px 0 4px 0;height:14px">
        <div style="height:8px;border-radius:8px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);position:absolute;left:0;right:0;top:3px;opacity:0.55"></div>
        <div style="position:absolute;top:0;left:${pct.toFixed(1)}%;transform:translateX(-50%);width:14px;height:14px;background:${tone};border:2px solid var(--text);border-radius:50%"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-family:monospace;font-size:10px;color:var(--muted);margin-top:8px">
        <span>−100</span><span>−50</span><span>0</span><span>+50</span><span>+100</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:4px;margin-top:12px;font-size:10px">${legendCells}</div>
    </div>

    <!-- STEP 2: What changed — grid layout so all 9 fit in 3 rows.
         No outer step card on V1 (V1 is compact, no 4-step framing). -->
    <div style="margin:0 0 12px 0">
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:8px;flex-wrap:wrap">
        <div style="font-size:13px;font-weight:700;color:var(--text);letter-spacing:.02em">WHAT CHANGED INSIDE THE NUMBER</div>
        <div style="font-size:11px;color:var(--muted)">Green pushed up · red pushed down · hover <span style="border-bottom:1px dotted #a78bfa">underlined</span> for definition</div>
      </div>
      <div class="lthcs-nar-components" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px">${compsHtml}</div>
    </div>

    <div class="sub" style="margin-top:8px;font-size:11px;color:var(--muted)">${escapeHtml(idx.note || 'Aggregate of LTHCS universe. Directional read, not a trading signal.')}</div>
    ${moversRow}
    <div style="margin-top:10px;text-align:right">${link}</div>
  `;

  // One-time wiring: click-outside closes any open popover. Guarded with a
  // host-attached flag so re-renders don't stack listeners.
  if (!host._lthcsNarPopoverWired){
    host._lthcsNarPopoverWired = true;
    document.addEventListener('click', (e) => {
      host.querySelectorAll('details.lthcs-nar-term[open]').forEach(d => {
        if (!d.contains(e.target)) d.removeAttribute('open');
      });
    });
  }
}

// LTHCS Composite Index panel — long-term holding conviction score
// aggregated across the 167-ticker equity universe. Rendered into the
// host element passed by the caller (used by both the Stocks tab and the
// dedicated LTHCS tab). Visual model: mirrors renderWhaleSentiment above.
// Reads DATA.lthcs (built in Python build_lthcs_payload()):
//   { available, index: {as_of, score, label, color, components, note},
//     movers: {gainers, decliners}, universe_count }
// Empty-state: when LTHCS data isn't on disk yet (concurrent pipeline run
// not finished), renders a polite placeholder + dashboard link instead
// of crashing.
//
// 2026-05-20: superseded by renderLthcsNarrativePanel above. Kept in place
// as a one-flip rollback — change the two call sites back if the narrative
// version causes problems.
function renderLthcsCompositePanel(host){
  if (!host) return;
  const L = (DATA.lthcs || {});
  const idx = L.index || null;
  const link = '<a href="lthcs/" target="_blank" rel="noopener" style="color:#a78bfa;text-decoration:none;font-weight:600">Open full LTHCS dashboard →</a>';
  if (!L.available || !idx){
    host.innerHTML = `
      <div class="head" style="align-items:flex-start">
        <div>
          <h2 style="font-size:15px">📊 LTHCS Composite Index</h2>
          <div class="desc">Data populates on next daily pipeline run</div>
        </div>
      </div>
      <div class="sub" style="color:var(--muted);padding:8px 0">
        LTHCS Composite Index — data not yet available. The daily pipeline writes
        <code>data/lthcs/index/&lt;date&gt;.json</code>; this panel will fill in on the next run.
      </div>
      <div style="margin-top:8px">${link}</div>
    `;
    return;
  }
  const score = Number(idx.score) || 0;
  const color = idx.color || signalColor(score);
  const pct = ((Math.max(-100, Math.min(100, score)) + 100) / 200) * 100;
  const label = idx.label || 'LTHCS';
  const asOf = idx.as_of || L.as_of || '—';
  const components = Array.isArray(idx.components) ? idx.components : [];
  // Component-table fonts bumped per user feedback (post b18e180 refinement):
  // name/value/± from ~13px → 15px and Read 12px → 14px so the table reads
  // clearly without leaning in.
  const compRows = components.map(c => {
    const d = Number(c.delta) || 0;
    const cls = d > 0 ? 'green' : (d < 0 ? 'red' : 'amber');
    const sign = (d >= 0 ? '+' : '') + d;
    return `<tr>
      <td style="font-size:15px">${escapeHtml(c.name||'')}</td>
      <td style="font-size:15px;font-weight:600">${escapeHtml(String(c.value==null?'—':c.value))}</td>
      <td class="${cls}" style="font-size:15px;font-weight:600">${sign}</td>
      <td style="color:var(--muted);font-size:14px">${escapeHtml(c.read||'')}</td>
    </tr>`;
  }).join('');
  // Top movers — read DATA.lthcs.movers.gainers / .decliners (top 5 each
  // by drift_30d). Defensive: missing arrays render an empty mover row.
  // Rendered as ticker BOXES (Crypto-tab card model) instead of mini-tables
  // per user feedback.
  const moversRow = renderLthcsMoversRow(L.movers || {});
  host.innerHTML = `
    <div class="head" style="align-items:flex-start">
      <div>
        <h2 style="font-size:15px">📊 LTHCS Composite Index</h2>
        <div class="desc">Composite of band distribution / pillar averages / macro overlay / insider + institutional breadth (9 inputs) · as of ${escapeHtml(asOf)}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:26px;font-weight:700;color:${color}">${escapeHtml(label)}</div>
        <div style="font-size:13px;color:var(--muted)">score <strong style="color:${color}">${score>=0?'+':''}${score}</strong> / ±100</div>
      </div>
    </div>
    <div style="height:10px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:5px;position:relative;margin:8px 0">
      <div style="position:absolute;top:-4px;left:calc(${pct.toFixed(1)}% - 4px);width:8px;height:18px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
    </div>
    <table style="margin-top:6px"><thead><tr><th>Component</th><th>Value</th><th>±</th><th>Read</th></tr></thead><tbody>${compRows}</tbody></table>
    <div class="sub" style="margin-top:8px;font-size:11px">${escapeHtml(idx.note || 'Aggregate of LTHCS universe. Directional read, not a trading signal.')}</div>
    ${moversRow}
    <div style="margin-top:10px;text-align:right">${link}</div>
  `;
}

// LTHCS-band → CSS color. Maps the 5 LTHCS band slugs from the daily
// snapshot to the crypto-dashboard signal palette so the gainer/decliner
// boxes match the existing color system (no new tokens introduced).
function lthcsBandColor(band){
  switch ((band || '').toLowerCase()){
    case 'elite':         return '#16a34a';   // strong green
    case 'constructive':  return '#22c55e';   // green
    case 'monitor':       return '#f59e0b';   // amber
    case 'weakening':     return '#fb923c';   // salmon/orange
    case 'review':        return '#ef4444';   // red
    default:              return '#94a3b8';
  }
}

// Map LTHCS subscores → human-readable top-driver pillar label for the
// sub-line on each gainer/decliner box. Picks the pillar with the highest
// score (gainer) or lowest score (decliner) to surface "why".
function lthcsTopPillar(subs, mode){
  if (!subs || typeof subs !== 'object') return '';
  const PILLARS = {
    adoption_momentum: 'Adoption',
    institutional_confidence: 'Institutional',
    financial_evolution: 'Financial',
    thesis_integrity: 'Thesis',
    des: 'DES',
  };
  const entries = Object.entries(subs)
    .filter(([k, v]) => typeof v === 'number' && isFinite(v));
  if (!entries.length) return '';
  entries.sort((a, b) => mode === 'decliner' ? a[1] - b[1] : b[1] - a[1]);
  const [k, v] = entries[0];
  return `${PILLARS[k] || k} ${v.toFixed(0)}`;
}

// Side-by-side top-5 gainers / decliners as colored ticker boxes for the
// LTHCS composite panel. Visual model mirrors the Overview-tab BTC/ETH/
// LINK/LTC ticker cards (border-left tint + big score, click → drill-in).
// On desktop the two sections sit side-by-side with each section's 5 boxes
// flowing into a 2-3 column grid; on mobile (≤768px) it stacks to a single
// column via the grid-template-columns auto-fit min(140px,1fr).
function renderLthcsMoversRow(movers){
  const gainers = Array.isArray(movers.gainers) ? movers.gainers : [];
  const decliners = Array.isArray(movers.decliners) ? movers.decliners : [];
  if (!gainers.length && !decliners.length) return '';
  const fmtDrift = d => {
    const v = Number(d);
    if (!isFinite(v)) return '—';
    const sign = v >= 0 ? '+' : '';
    return `${sign}${v.toFixed(1)}`;
  };
  const fmtScore = s => {
    const v = Number(s);
    if (!isFinite(v)) return '—';
    return v.toFixed(0);
  };
  const driftColor = d => {
    const v = Number(d);
    if (!isFinite(v) || v === 0) return 'var(--muted)';
    return v > 0 ? '#22c55e' : '#ef4444';
  };
  const driftArrow = d => {
    const v = Number(d);
    if (!isFinite(v) || v === 0) return '·';
    return v > 0 ? '▲' : '▼';
  };
  // Each ticker box: ~150px wide / ~80-90px tall, colored left border + soft
  // band-tinted background, click → /lthcs/?ticker=<TICKER>. Encoded HTML
  // class consistent with the Crypto-tab .card pattern.
  const box = (r, mode) => {
    const ticker = (r.ticker || '').toUpperCase();
    const band = (r.band || '').toLowerCase();
    const accent = lthcsBandColor(band);
    const drift = Number(r.drift_30d);
    const driftStr = fmtDrift(r.drift_30d);
    const driftCol = driftColor(r.drift_30d);
    const arrow = driftArrow(r.drift_30d);
    const score = fmtScore(r.score);
    const pillar = lthcsTopPillar(r.subscores, mode);
    const subLine = pillar
      ? `Top: ${escapeHtml(pillar)}`
      : `${escapeHtml(band || 'band —')}`;
    return `<a class="card lthcs-mover-card"
      href="lthcs/?ticker=${encodeURIComponent(ticker)}"
      target="_blank" rel="noopener"
      title="Open ${escapeHtml(ticker)} on LTHCS dashboard"
      style="display:block;padding:8px 10px;border-left:4px solid ${accent};
        background:${accent}11;text-decoration:none;color:var(--text);
        cursor:pointer;min-height:80px">
      <div style="font-size:16px;font-weight:700;letter-spacing:.02em">${escapeHtml(ticker)}</div>
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:6px;margin-top:2px">
        <span style="font-size:24px;font-weight:700;color:${accent};line-height:1">${score}</span>
        <span style="font-size:12px;font-weight:600;color:${driftCol}">${arrow} ${driftStr}</span>
      </div>
      <div class="sub" style="font-size:11px;color:var(--muted);margin-top:4px;
        white-space:nowrap;text-overflow:ellipsis;overflow:hidden">${subLine}</div>
    </a>`;
  };
  const block = (title, rows, accent, mode) => `
    <div style="flex:1 1 320px;min-width:0">
      <div style="font-size:12px;font-weight:700;color:${accent};letter-spacing:.06em;margin-bottom:6px">${title}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px">
        ${rows.map(r => box(r, mode)).join('')}
      </div>
    </div>
  `;
  return `<div style="display:flex;flex-wrap:wrap;gap:14px;margin-top:14px">
    ${gainers.length ? block('▲ TOP 5 GAINERS (30D)', gainers, '#22c55e', 'gainer') : ''}
    ${decliners.length ? block('▼ TOP 5 DECLINERS (30D)', decliners, '#ef4444', 'decliner') : ''}
  </div>`;
}

// LTHCS Insights row + corner CTA — replaces the big intro card from
// b18e180. Reads DATA.lthcs.insights (3-5 dicts built server-side by
// compute_lthcs_insights). Each insight gets a small card with a
// severity-colored left border (high=red, medium=amber, low=green).
// The CTA "Open full LTHCS →" and an "ⓘ About" toggle sit in the
// top-right corner so they stay prominent without consuming a row.
function renderLthcsInsightsRow(host){
  if (!host) return;
  const L = (DATA.lthcs || {});
  const insights = Array.isArray(L.insights) ? L.insights : [];
  const cta = '<a class="btn" href="lthcs/" target="_blank" rel="noopener"' +
    ' style="display:inline-flex;align-items:center;gap:4px;background:#a78bfa;' +
    'color:#0b0d12;font-weight:700;padding:6px 12px;border-radius:6px;' +
    'text-decoration:none;font-size:12px;white-space:nowrap;flex:0 0 auto">' +
    'Open full LTHCS →</a>';
  // "About LTHCS" disclosure — a <details> button next to the CTA.
  // Opens an inline panel explaining what LTHCS is, the 5-pillar
  // calculation, and the data-source lineage. No modal infra needed.
  const aboutBtn = '<details class="lthcs-about-details" style="' +
    'flex:0 0 auto;position:relative">' +
    '<summary style="list-style:none;cursor:pointer;display:inline-flex;' +
    'align-items:center;gap:4px;background:#0e1118;color:var(--text);' +
    'font-weight:600;padding:6px 10px;border-radius:6px;border:1px solid var(--border);' +
    'font-size:12px;white-space:nowrap">ⓘ About LTHCS</summary>' +
    '<div class="lthcs-about-panel" style="position:absolute;top:calc(100% + 6px);' +
    'right:0;width:min(560px, calc(100vw - 32px));background:#0e1118;' +
    'border:1px solid var(--border);border-left:3px solid #a78bfa;border-radius:10px;' +
    'padding:14px 16px;z-index:10;box-shadow:0 8px 24px rgba(0,0,0,0.4);line-height:1.5;' +
    'font-size:13px;color:var(--text);max-height:70vh;overflow-y:auto">' +
    '<div style="font-size:14px;font-weight:700;margin-bottom:8px">What is LTHCS?</div>' +
    '<p style="margin:0 0 10px 0;color:var(--text)">' +
    'The <strong>Long-Term Holding Conviction Score</strong> is a daily 0-100 read on ' +
    'each of 167 US-listed stocks (DJIA 30 + NASDAQ-100 + S&P 100). It measures whether ' +
    'the underlying business and market context still justify <em>holding</em> the position ' +
    'long-term. Not a trade signal — a conviction signal.</p>' +
    '<div style="font-size:14px;font-weight:700;margin:12px 0 6px 0">How the score is calculated</div>' +
    '<p style="margin:0 0 6px 0;color:var(--muted);font-size:12px">' +
    'Each ticker is scored 0-100 across 5 pillars, weighted by its maturity stage ' +
    '(mature compounder / growth / recovery / etc.). Modifiers then refine: HY-stress, ' +
    'curve inversion, dollar strength, volatility percentile. Final score → band: ' +
    'Elite (90+) · High (80-89) · Constructive (70-79) · Monitor (60-69) · Weakening (50-59) · Review (0-49).</p>' +
    '<ul style="margin:6px 0 10px 16px;padding:0;color:var(--text);font-size:12px;line-height:1.5">' +
    '<li><strong>Adoption Momentum</strong> — revenue growth percentile + Google Trends acceleration</li>' +
    '<li><strong>Institutional Confidence</strong> — price momentum + SEC Form 4 insider activity + SEC 13F holdings</li>' +
    '<li><strong>Financial Evolution</strong> — gross profit, FCF, NII (for banks), credit quality</li>' +
    '<li><strong>Thesis Integrity</strong> — analyst consensus + earnings events + news sentiment</li>' +
    '<li><strong>DES — Demand Environment</strong> — sector regime + macro overlay</li>' +
    '</ul>' +
    '<div style="font-size:14px;font-weight:700;margin:12px 0 6px 0">' +
    'Data sources <span class="sub" style="font-weight:400;color:var(--muted);font-size:11px">' +
    '(~17 feeds across 10 categories, all free)</span></div>' +
    '<ol style="margin:6px 0 6px 18px;padding:0;color:var(--text);font-size:12px;line-height:1.55">' +
    '<li><strong>Yahoo Finance</strong> — daily prices, momentum, sector ETFs (XLK/XLF/XLE…)</li>' +
    '<li><strong>SEC EDGAR XBRL</strong> — revenue, gross profit, OCF, bank NII / PCL / non-interest income</li>' +
    '<li><strong>SEC 8-K</strong> — material event filings (restatements, earnings, dispositions)</li>' +
    '<li><strong>SEC Form 4</strong> — insider open-market transactions (165/168 covered today)</li>' +
    '<li><strong>SEC 13F</strong> — institutional holdings aggregated across 21 tracked managers</li>' +
    '<li><strong>FRED</strong> — CPI, Fed Funds, 10Y, real-10Y, VIX, M2, HY OAS, IG OAS, 2s10s, broad dollar</li>' +
    '<li><strong>EIA</strong> — WTI oil + "Today in Energy" RSS</li>' +
    '<li><strong>Finnhub</strong> — analyst recommendation consensus</li>' +
    '<li><strong>Sentiment surveys</strong> — CBOE put/call ratio, AAII retail bull/bear, NAAIM manager exposure</li>' +
    '<li><strong>Sector + AI news RSS</strong> — FDA press, Federal Reserve, HN Algolia, TechCrunch, VentureBeat</li>' +
    '</ol>' +
    '<p style="margin:6px 0 0 0;color:var(--muted);font-size:11px;font-style:italic">' +
    'Plus a weekly Google Trends batch (search-interest acceleration; cached separately ' +
    'because pytrends is rate-limited). Aggregated 0-100 directional read across the universe; ' +
    'not a trading recommendation.</p>' +
    '</div></details>';
  const sevColor = sev => ({
    high:   '#ef4444',
    medium: '#f59e0b',
    low:    '#22c55e',
  })[sev] || '#a78bfa';
  const header = `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:8px">
      <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">
        <strong style="font-size:13px;color:var(--text)">LTHCS Insights</strong>
        <span class="sub" style="font-size:11px;color:var(--muted)">
          ${insights.length ? insights.length + ' signals · as of ' + escapeHtml(L.as_of || '—') : 'none right now'}
        </span>
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">${aboutBtn}${cta}</div>
    </div>
  `;
  if (!insights.length){
    host.innerHTML = header +
      '<div class="sub" style="color:var(--muted);font-size:12px">' +
      'Nothing unusual right now. Insights populate when the daily pipeline ' +
      'finishes and writes <code>data/lthcs/insider/</code>, <code>holdings/</code>, ' +
      '<code>macro/</code>, and <code>history/</code> files.</div>';
    return;
  }
  const items = insights.map(i => {
    const c = sevColor(i.severity);
    const ic = i.icon || '•';
    const detail = i.detail
      ? `<div class="sub" style="font-size:11px;color:var(--muted);margin-top:2px;line-height:1.3">${escapeHtml(i.detail)}</div>`
      : '';
    return `<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 10px;
      background:#0e1118;border:1px solid var(--border);border-left:3px solid ${c};
      border-radius:8px;max-width:420px;flex:1 1 280px;min-height:40px">
      <span style="font-size:13px;line-height:1.2">${escapeHtml(ic)}</span>
      <div style="line-height:1.3;min-width:0">
        <div style="font-size:12px;color:var(--text);font-weight:600">${escapeHtml(i.headline || '')}</div>
        ${detail}
      </div>
    </div>`;
  }).join('');
  host.innerHTML = header +
    `<div style="display:flex;flex-wrap:wrap;gap:8px">${items}</div>`;
}

// Render the standalone LTHCS tab — Insights row + composite panel. Both
// hosts are filled in place; the panel host is the same as the Stocks-tab
// composite card so the visual model stays identical.
function renderLthcsTab(){
  renderLthcsInsightsRow(document.getElementById('lthcsInsightsRow'));
  renderLthcsNarrativePanel(document.getElementById('lthcsCompositeCard'));
}

// Real Estate tab — gateway summary. Fetches /data/real_estate.json once
// per session, computes a 5-stat headline (median ZHVI, # metros with
// positive YoY, average days on market, hottest market, coldest market),
// and shows it next to the CTA. Failure is graceful — if the JSON is
// missing the empty-state message stays in place.
let _reCache = null;
let _reSelectedState = null;
function renderRealEstateTab(){
  const host = document.getElementById('realEstateSummary');
  if (!host) return;
  if (_reCache) { _drawRealEstate(host, _reCache); return; }
  fetch('data/real_estate.json', {cache: 'no-store'})
    .then(r => r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)))
    .then(d => { _reCache = d; _drawRealEstate(host, d); })
    .catch(e => { host.innerHTML = '<div class="empty">Real-estate snapshot not yet loaded ('+ String(e.message || e) +'). Daily refresh runs at 06:00 UTC.</div>'; });
}
function _drawRealEstate(host, d){
  const metros = (d && d.metros) || [];
  if (!metros.length) { host.innerHTML = '<div class="empty" style="padding:12px 14px">No metro data in snapshot.</div>'; return; }
  const fmtUsd = n => n == null ? '—' : (n >= 1e6 ? '$' + (n/1e6).toFixed(2) + 'M' : '$' + Math.round(n/1000) + 'K');
  const fmtPct = n => n == null ? '—' : (n > 0 ? '+' : '') + n.toFixed(1) + '%';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  // Minimum-sales sanity gate so headline metros / heat index aren't
  // dominated by tiny markets that hit single-metric extremes on low
  // volume (matters now that the snapshot covers all ~894 MSAs).
  const MIN_SALES = 100;
  const qualifies = m => (m.kpis?.homes_sold?.value ?? 0) >= MIN_SALES;
  const qualifiedMetros = metros.filter(qualifies);

  // ---- Heat index ------------------------------------------------------
  // Normalize each signal to 0-100 then average. Endpoints documented in
  // the redesign spec — higher = hotter for all 4 final scores.
  // pct_above_list: 0.00 -> 0, 0.50 -> 100
  // days_on_market: 100d -> 0, 20d  -> 100
  // pct_price_cut:  0.40 -> 0, 0.05 -> 100
  // zhvi.yoy_pct:   -10  -> 0, 20   -> 100
  const norm = {
    above: v => clamp(((v - 0.00) / (0.50 - 0.00)) * 100, 0, 100),
    dom:   v => clamp(((100 - v) / (100 - 20))    * 100, 0, 100),
    cut:   v => clamp(((0.40 - v) / (0.40 - 0.05)) * 100, 0, 100),
    yoy:   v => clamp(((v - (-10)) / (20 - (-10))) * 100, 0, 100),
  };
  // pick value at sparkline index for a given KPI path, falling back to
  // value when spark is missing or out of range.
  const sparkAt = (m, kpi, idx) => {
    const sp = m.kpis?.[kpi]?.spark;
    if (Array.isArray(sp) && sp.length) {
      const i = idx < 0 ? sp.length + idx : idx;
      if (i >= 0 && i < sp.length && sp[i] != null) return sp[i];
    }
    return null;
  };
  // Compute the heat index for a particular point in time. sliceFn maps a
  // metro to its (above, dom, cut, yoy) values for that snapshot. Any
  // missing signal is dropped from that signal's mean; if every signal is
  // empty, returns null.
  const heatFor = sliceFn => {
    const sums = {above:[], dom:[], cut:[], yoy:[]};
    qualifiedMetros.forEach(m => {
      const s = sliceFn(m);
      if (s.above != null) sums.above.push(s.above);
      if (s.dom   != null) sums.dom.push(s.dom);
      if (s.cut   != null) sums.cut.push(s.cut);
      if (s.yoy   != null) sums.yoy.push(s.yoy);
    });
    const meanOf = arr => arr.length ? arr.reduce((a,b)=>a+b,0) / arr.length : null;
    const parts = [
      sums.above.length ? norm.above(meanOf(sums.above)) : null,
      sums.dom.length   ? norm.dom(meanOf(sums.dom))     : null,
      sums.cut.length   ? norm.cut(meanOf(sums.cut))     : null,
      sums.yoy.length   ? norm.yoy(meanOf(sums.yoy))     : null,
    ].filter(v => v != null);
    if (!parts.length) return null;
    return parts.reduce((a,b)=>a+b,0) / parts.length;
  };
  const currentSlice = m => ({
    above: m.kpis?.pct_above_list?.value,
    dom:   m.kpis?.days_on_market?.value,
    cut:   m.kpis?.pct_price_cut?.value,
    yoy:   m.kpis?.zhvi?.yoy_pct,
  });
  // Sparkline arrays have ~12 monthly points. idx -2 ≈ 30d ago; idx 0 ≈ 1y ago.
  // ZHVI yoy doesn't have a YoY-of-YoY easily, so 30d uses current, 1y uses
  // five_year_pct / 5 as a rough proxy.
  const pastSlice = (idx, yoyMode) => m => ({
    above: sparkAt(m, 'pct_above_list', idx),
    dom:   sparkAt(m, 'days_on_market', idx),
    cut:   sparkAt(m, 'pct_price_cut', idx),
    yoy:   yoyMode === 'current' ? m.kpis?.zhvi?.yoy_pct
         : yoyMode === '5y_proxy' ? (m.kpis?.zhvi?.five_year_pct != null ? m.kpis.zhvi.five_year_pct / 5 : null)
         : null,
  });
  const heatNow  = heatFor(currentSlice);
  const heat30d  = heatFor(pastSlice(-2, 'current'));
  const heat1y   = heatFor(pastSlice(0,  '5y_proxy'));
  const heatIdx  = heatNow == null ? null : Math.round(heatNow);
  const delta30  = (heatNow != null && heat30d != null) ? Math.round(heatNow - heat30d) : null;
  const delta1y  = (heatNow != null && heat1y  != null) ? Math.round(heatNow - heat1y)  : null;
  const heatLabel = v => v == null ? '—'
    : v <= 25 ? {t:'Cold',      c:'#4a90e2'}
    : v <= 45 ? {t:'Cool',      c:'#5fb3a8'}
    : v <= 65 ? {t:'Warm',      c:'#e8b04a'}
    : v <= 85 ? {t:'Hot',       c:'#e8744a'}
    :           {t:'Scorching', c:'#d04545'};
  const hLbl = heatLabel(heatIdx);

  // ---- State + Census-region aggregation ------------------------------
  // Roll the ~894 metros up to states (and the 4 Census regions). Price /
  // ratio signals are homes_sold-weighted so big markets dominate; counts
  // are summed. State heat reuses the national norm endpoints on the
  // weighted signal means so the colour scale is comparable everywhere.
  const fmtNum = n => n == null ? '—' : Math.round(n).toLocaleString();
  const CENSUS_REGION = {
    Northeast: ['CT','ME','MA','NH','RI','VT','NJ','NY','PA'],
    Midwest:   ['IL','IN','MI','OH','WI','IA','KS','MN','MO','NE','ND','SD'],
    South:     ['DE','FL','GA','MD','NC','SC','VA','DC','WV','AL','KY','MS','TN','AR','LA','OK','TX'],
    West:      ['AZ','CO','ID','MT','NV','NM','UT','WY','AK','CA','HI','OR','WA'],
  };
  const wt = m => m.kpis?.homes_sold?.value ?? 0;
  const wMean = (items, valFn) => {
    let sw = 0, swv = 0;
    items.forEach(m => { const v = valFn(m), w = wt(m); if (v != null && isFinite(v) && w > 0) { sw += w; swv += v * w; } });
    return sw > 0 ? swv / sw : null;
  };
  const uMean = (items, valFn) => {
    const xs = items.map(valFn).filter(v => v != null && isFinite(v));
    return xs.length ? xs.reduce((a,b)=>a+b,0) / xs.length : null;
  };
  const sumOf = (items, valFn) => {
    let s = 0, any = false;
    items.forEach(m => { const v = valFn(m); if (v != null && isFinite(v)) { s += v; any = true; } });
    return any ? s : null;
  };
  const heatFromSignals = (above, dom, cut, yoy) => {
    const parts = [];
    if (above != null) parts.push(norm.above(above));
    if (dom   != null) parts.push(norm.dom(dom));
    if (cut   != null) parts.push(norm.cut(cut));
    if (yoy   != null) parts.push(norm.yoy(yoy));
    return parts.length ? parts.reduce((a,b)=>a+b,0) / parts.length : null;
  };
  const aggregate = items => {
    const q = items.filter(qualifies);
    const base = q.length ? q : items;
    // Weighted by homes_sold, but fall back to an equal-weighted mean when a
    // state's metros all lack a sales count (e.g. WY) so it still gets a heat
    // value instead of dropping off the map.
    const above = wMean(base, m => m.kpis?.pct_above_list?.value) ?? uMean(base, m => m.kpis?.pct_above_list?.value);
    const dom   = wMean(base, m => m.kpis?.days_on_market?.value) ?? uMean(base, m => m.kpis?.days_on_market?.value);
    const cut   = wMean(base, m => m.kpis?.pct_price_cut?.value)  ?? uMean(base, m => m.kpis?.pct_price_cut?.value);
    const yoy   = wMean(base, m => m.kpis?.zhvi?.yoy_pct)         ?? uMean(base, m => m.kpis?.zhvi?.yoy_pct);
    const hv    = heatFromSignals(above, dom, cut, yoy);
    return {
      n: items.length,
      heat: hv == null ? null : Math.round(hv),
      zhvi: wMean(items, m => m.kpis?.zhvi?.value) ?? uMean(items, m => m.kpis?.zhvi?.value),
      zhviYoy: yoy,
      medianSale: wMean(items, m => m.kpis?.median_sale?.value) ?? uMean(items, m => m.kpis?.median_sale?.value),
      homesSold: sumOf(items, m => m.kpis?.homes_sold?.value),
      newListings: sumOf(items, m => m.kpis?.new_listings?.value),
      activeListings: sumOf(items, m => m.kpis?.active_listings?.value),
      permits: sumOf(items, m => m.kpis?.permits?.value),
      dom: dom,
      aboveList: above,
      priceCut: cut,
      saleToList: wMean(base, m => m.kpis?.sale_to_list?.value) ?? uMean(base, m => m.kpis?.sale_to_list?.value),
    };
  };
  const byState = {};
  metros.forEach(m => { if (m.state) (byState[m.state] = byState[m.state] || []).push(m); });
  const stateAgg = {};
  Object.keys(byState).forEach(s => { stateAgg[s] = aggregate(byState[s]); });
  const regionAgg = {};
  Object.entries(CENSUS_REGION).forEach(([r, arr]) => {
    regionAgg[r] = aggregate(metros.filter(m => arr.includes(m.state)));
  });
  if (_reSelectedState && !stateAgg[_reSelectedState]) _reSelectedState = null;

  // ---- Heat colour ramp (matches the hero gradient stops) -------------
  const HEAT_STOPS = [[0,[74,144,226]],[25,[95,179,168]],[50,[232,176,74]],[75,[232,116,74]],[100,[208,69,69]]];
  const heatColor = v => {
    if (v == null) return '#222836';
    v = clamp(v, 0, 100);
    for (let i = 1; i < HEAT_STOPS.length; i++) {
      if (v <= HEAT_STOPS[i][0]) {
        const a0 = HEAT_STOPS[i-1][0], c0 = HEAT_STOPS[i-1][1];
        const a1 = HEAT_STOPS[i][0],   c1 = HEAT_STOPS[i][1];
        const t = (v - a0) / (a1 - a0);
        const ch = k => Math.round(c0[k] + (c1[k] - c0[k]) * t);
        return 'rgb(' + ch(0) + ',' + ch(1) + ',' + ch(2) + ')';
      }
    }
    return 'rgb(208,69,69)';
  };

  // ---- US state heat-map (reuses the UAP tile grid layout) ------------
  const RE_CELL = 50, RE_GAP = 4, RE_COLS = 12, RE_ROWS = 8;
  const RE_W = RE_COLS * (RE_CELL + RE_GAP) - RE_GAP;
  const RE_H = RE_ROWS * (RE_CELL + RE_GAP) - RE_GAP;
  const buildTiles = () => MUFON_STATE_GRID.map(([code, cx, cy]) => {
    const x = cx * (RE_CELL + RE_GAP), y = cy * (RE_CELL + RE_GAP);
    const ag = stateAgg[code];
    const hv = ag ? ag.heat : null;
    const fill = ag ? heatColor(hv) : '#1a1f2b';
    const isSel = _reSelectedState === code;
    const stroke = isSel ? '#ffffff' : 'rgba(255,255,255,0.10)';
    const strokeW = isSel ? 2.5 : 1;
    const tip = ag ? (MUFON_STATE_NAMES[code]||code) + ': heat ' + (hv==null?'—':hv) + ' · ' + ag.n + ' metros'
                   : (MUFON_STATE_NAMES[code]||code) + ': no data';
    return ''
      + '<g class="reTile" data-restate="' + code + '" style="cursor:pointer">'
      +   '<rect x="'+x+'" y="'+y+'" width="'+RE_CELL+'" height="'+RE_CELL+'" rx="6" fill="'+fill+'" stroke="'+stroke+'" stroke-width="'+strokeW+'">'
      +     '<title>'+tip+'</title>'
      +   '</rect>'
      +   '<text x="'+(x+RE_CELL/2)+'" y="'+(y+RE_CELL/2-3)+'" text-anchor="middle" font-size="13" font-weight="700" fill="rgba(0,0,0,0.72)" pointer-events="none">'+code+'</text>'
      +   '<text x="'+(x+RE_CELL/2)+'" y="'+(y+RE_CELL/2+12)+'" text-anchor="middle" font-size="10" fill="rgba(0,0,0,0.6)" pointer-events="none">'+(hv==null?'—':hv)+'</text>'
      + '</g>';
  }).join('');
  const mapLegendStops = [['Cold','#4a90e2'],['Cool','#5fb3a8'],['Warm','#e8b04a'],['Hot','#e8744a'],['Scorch','#d04545']];
  const mapLegend = '<div style="display:flex;gap:8px;align-items:center;font-size:10px;color:var(--muted,#9aa3b2);margin-top:10px;flex-wrap:wrap">'
    + '<span>cooler</span>'
    + mapLegendStops.map(b => '<span style="display:inline-flex;align-items:center;gap:3px"><span style="display:inline-block;width:16px;height:12px;border-radius:3px;background:'+b[1]+'"></span>'+b[0]+'</span>').join('')
    + '<span>hotter</span></div>';
  const mapHtml =
    '<div style="padding:14px;background:var(--panel,#141923);border:1px solid var(--border,#2a3142);border-radius:10px;margin:0 14px 12px 14px">' +
      '<div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;margin-bottom:8px">' +
        '<div style="font-size:13px;font-weight:600;color:#e6e9ee">Housing Heat by State</div>' +
        '<div style="font-size:11px;color:var(--muted,#9aa3b2)">click a state for KPIs</div>' +
      '</div>' +
      '<div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start">' +
        '<div style="flex:1 1 420px;min-width:300px">' +
          '<svg id="reMapSvg" viewBox="0 0 '+RE_W+' '+RE_H+'" width="100%" preserveAspectRatio="xMidYMid meet" style="font-family:inherit;display:block;max-height:520px" role="img" aria-label="US housing heat by state">'+buildTiles()+'</svg>' +
          mapLegend +
        '</div>' +
        '<div id="reStatePanel" style="flex:1 1 280px;min-width:260px"></div>' +
      '</div>' +
    '</div>';

  // KPI grid used by the click-state panel.
  const statKpiGrid = ag => {
    const cell = (label, val, sub) =>
      '<div style="padding:8px 10px;background:var(--bg2,#0f1419);border:1px solid var(--border,#2a3142);border-radius:8px">' +
        '<div style="font-size:9px;color:var(--muted,#9aa3b2);text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+label+'</div>' +
        '<div style="font-size:14px;font-weight:600;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+val+'</div>' +
        (sub?'<div style="font-size:9px;color:var(--muted,#9aa3b2);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">'+sub+'</div>':'') +
      '</div>';
    return '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:10px">' +
      cell('Median ZHVI', fmtUsd(ag.zhvi), fmtPct(ag.zhviYoy) + ' YoY') +
      cell('Median sale', fmtUsd(ag.medianSale), '') +
      cell('Homes sold', fmtNum(ag.homesSold), 'monthly') +
      cell('Active listings', fmtNum(ag.activeListings), 'inventory') +
      cell('Days on market', ag.dom==null?'—':Math.round(ag.dom)+'d', '') +
      cell('Above list', ag.aboveList==null?'—':Math.round(ag.aboveList*100)+'%', 'sold over ask') +
      cell('Price cuts', ag.priceCut==null?'—':Math.round(ag.priceCut*100)+'%', 'reduced') +
      cell('Permits', fmtNum(ag.permits), 'building') +
    '</div>';
  };

  // Right-hand panel: selected-state detail, or a hottest-states leaderboard.
  const renderRePanel = () => {
    const panel = document.getElementById('reStatePanel');
    if (!panel) return;
    if (_reSelectedState && stateAgg[_reSelectedState]) {
      const s = _reSelectedState, ag = stateAgg[s];
      const hb = heatLabel(ag.heat) || {};
      const topMetros = (byState[s]||[]).slice()
        .sort((a,b)=>(b.kpis?.homes_sold?.value||0)-(a.kpis?.homes_sold?.value||0)).slice(0,6);
      panel.innerHTML =
        '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:2px">' +
          '<div style="font-weight:700;font-size:15px">'+esc(MUFON_STATE_NAMES[s]||s)+'</div>' +
          '<button id="rePanelClose" style="padding:2px 8px;font-size:11px;background:transparent;border:1px solid var(--border,#2a3142);color:var(--muted,#9aa3b2);border-radius:6px;cursor:pointer">×</button>' +
        '</div>' +
        '<div style="display:flex;align-items:baseline;gap:8px;margin-top:2px">' +
          '<span style="font-size:24px;font-weight:700;color:'+(hb.c||'#fff')+'">'+(ag.heat==null?'—':ag.heat)+'</span>' +
          '<span style="font-size:13px;font-weight:600;color:'+(hb.c||'var(--muted,#9aa3b2)')+'">'+(hb.t||'—')+'</span>' +
          '<span style="font-size:10px;color:var(--muted,#9aa3b2)">heat · '+ag.n+' metros · '+(REGION_OF[s]||'')+'</span>' +
        '</div>' +
        statKpiGrid(ag) +
        '<div style="font-size:10px;color:var(--muted,#9aa3b2);margin-top:12px;margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em">Largest metros</div>' +
        '<div style="display:flex;flex-direction:column;gap:4px">' +
          topMetros.map(m => '<div style="display:flex;justify-content:space-between;font-size:12px"><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(m.short_name||m.name)+'</span><span style="color:var(--muted,#9aa3b2);white-space:nowrap;margin-left:8px">'+fmtUsd(m.kpis?.zhvi?.value)+'</span></div>').join('') +
        '</div>';
      const cb = document.getElementById('rePanelClose');
      if (cb) cb.addEventListener('click', () => { _reSelectedState = null; _paintMap(); renderRePanel(); });
    } else {
      const ranked = Object.keys(stateAgg).filter(s => stateAgg[s].heat != null)
        .sort((a,b) => stateAgg[b].heat - stateAgg[a].heat).slice(0,10);
      const row = s => '<li style="display:flex;justify-content:space-between;padding:2px 0"><span><strong>'+esc(MUFON_STATE_NAMES[s]||s)+'</strong> <span style="color:var(--muted,#9aa3b2);font-size:10px">'+(REGION_OF[s]||'')+'</span></span><span style="color:'+heatColor(stateAgg[s].heat)+';font-weight:700">'+stateAgg[s].heat+'</span></li>';
      panel.innerHTML =
        '<div style="font-weight:700;font-size:14px;margin-bottom:2px">Hottest states</div>' +
        '<div style="font-size:11px;color:var(--muted,#9aa3b2);margin-bottom:8px">by housing heat index · click a tile for detail</div>' +
        '<ol style="margin:0;padding-left:20px;line-height:1.7;font-size:12px">'+ranked.map(row).join('')+'</ol>';
    }
  };
  const _paintMap = () => {
    const svg = document.getElementById('reMapSvg');
    if (!svg) return;
    svg.innerHTML = buildTiles();
    bindTiles();
  };
  const bindTiles = () => {
    document.querySelectorAll('#reMapSvg .reTile').forEach(g => {
      g.addEventListener('click', () => {
        const s = g.getAttribute('data-restate');
        if (!stateAgg[s]) return;
        _reSelectedState = (_reSelectedState === s) ? null : s;
        _paintMap();
        renderRePanel();
      });
    });
  };
  const REGION_OF = {};
  Object.entries(CENSUS_REGION).forEach(([r, arr]) => arr.forEach(s => REGION_OF[s] = r));

  // ---- Census-region rollup row ---------------------------------------
  const regionCard = name => {
    const ag = regionAgg[name];
    const hb = heatLabel(ag.heat) || {};
    return '<div style="flex:1;min-width:0;padding:10px;background:var(--panel,#141923);border:1px solid var(--border,#2a3142);border-top:3px solid '+heatColor(ag.heat)+';border-radius:8px">' +
      '<div style="font-size:11px;color:var(--muted,#9aa3b2);text-transform:uppercase;letter-spacing:.05em">'+name+'</div>' +
      '<div style="display:flex;align-items:baseline;gap:6px;margin-top:4px">' +
        '<span style="font-size:22px;font-weight:700;color:'+(hb.c||'#fff')+'">'+(ag.heat==null?'—':ag.heat)+'</span>' +
        '<span style="font-size:11px;font-weight:600;color:'+(hb.c||'var(--muted,#9aa3b2)')+'">'+(hb.t||'—')+'</span>' +
      '</div>' +
      '<div style="font-size:11px;color:var(--muted,#9aa3b2);margin-top:4px">'+fmtUsd(ag.zhvi)+' · '+fmtPct(ag.zhviYoy)+' YoY</div>' +
    '</div>';
  };
  const regionRowHtml =
    '<div style="font-size:13px;font-weight:600;color:#e6e9ee;padding:0 14px;margin-bottom:6px">Census regions</div>' +
    '<div style="display:flex;gap:8px;padding:0 14px;margin-bottom:12px;flex-wrap:wrap">' +
      ['Northeast','Midwest','South','West'].map(regionCard).join('') +
    '</div>';

  // ---- Sparkline SVG helper -------------------------------------------
  // Compact inline polyline, no fill, single colour. Returns '' on bad input.
  const spark = (arr, color, w, h) => {
    const pts = (arr || []).filter(v => v != null && isFinite(v));
    if (pts.length < 2) return '';
    const mn = Math.min(...pts), mx = Math.max(...pts);
    const rng = (mx - mn) || 1;
    const step = w / (pts.length - 1);
    const coords = pts.map((v, i) => i * step + ',' + (h - ((v - mn) / rng) * h).toFixed(1)).join(' ');
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '" preserveAspectRatio="none" style="display:block">' +
      '<polyline points="' + coords + '" fill="none" stroke="' + color + '" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  };

  // ---- Market lists ---------------------------------------------------
  const hotList = qualifiedMetros
    .filter(m => m.kpis?.pct_above_list?.value != null)
    .slice()
    .sort((a, b) => (b.kpis.pct_above_list.value || 0) - (a.kpis.pct_above_list.value || 0))
    .slice(0, 10);
  const coolList = qualifiedMetros
    .filter(m => m.kpis?.pct_price_cut?.value != null)
    .slice()
    .sort((a, b) => (b.kpis.pct_price_cut.value || 0) - (a.kpis.pct_price_cut.value || 0))
    .slice(0, 10);

  // ---- KPI row data ---------------------------------------------------
  const zhviValues = metros.map(m => m.kpis?.zhvi?.value).filter(v => v != null).sort((a,b)=>a-b);
  const medianZhvi = zhviValues.length ? zhviValues[Math.floor(zhviValues.length/2)] : null;
  const positiveYoy = metros.filter(m => (m.kpis?.zhvi?.yoy_pct ?? 0) > 0).length;
  const domValues = metros.map(m => m.kpis?.days_on_market?.value).filter(v => v != null);
  const avgDom = domValues.length ? Math.round(domValues.reduce((a,b)=>a+b,0) / domValues.length) : null;

  const generated = d.generated_at ? new Date(d.generated_at).toISOString().slice(0,10) : '—';

  // ---- HTML build -----------------------------------------------------
  // Hero: heat index card
  const needlePct = heatIdx == null ? 50 : heatIdx;
  const gradient = 'linear-gradient(90deg,#4a90e2 0%,#5fb3a8 25%,#e8b04a 50%,#e8744a 75%,#d04545 100%)';
  const chip = (label, sign) => {
    const color = sign > 0 ? '#3fb950' : sign < 0 ? '#f85149' : 'var(--muted,#9aa3b2)';
    return '<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 8px;border-radius:999px;background:rgba(255,255,255,0.04);border:1px solid var(--border,#2a3142);font-size:11px;color:' + color + '">' + label + '</span>';
  };
  const delta30Chip = delta30 == null
    ? chip('— 30d', 0)
    : chip((delta30 > 0 ? '↑ +' : delta30 < 0 ? '↓ ' : '') + delta30 + ' pts 30d', delta30);
  const delta1yChip = delta1y == null
    ? chip('— 1y', 0)
    : chip((delta1y > 0 ? '↑ +' : delta1y < 0 ? '↓ ' : '') + delta1y + ' pts 1y', delta1y);

  const heroHtml =
    '<div style="padding:14px;background:var(--panel,#141923);border:1px solid var(--border,#2a3142);border-radius:10px;margin:0 14px 12px 14px">' +
      '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">' +
        '<div style="font-size:11px;color:var(--muted,#9aa3b2);text-transform:uppercase;letter-spacing:0.05em">National Housing Heat Index</div>' +
      '</div>' +
      '<div style="display:flex;align-items:baseline;gap:14px;margin-top:6px;flex-wrap:wrap">' +
        '<div style="font-size:44px;font-weight:700;line-height:1;color:' + (hLbl.c || '#fff') + '">' + (heatIdx == null ? '—' : heatIdx) + '</div>' +
        '<div style="font-size:18px;font-weight:600;color:' + (hLbl.c || 'var(--muted,#9aa3b2)') + '">' + (hLbl.t || '—') + '</div>' +
        '<div style="font-size:11px;color:var(--muted,#9aa3b2)">/ 100</div>' +
      '</div>' +
      '<div style="position:relative;margin-top:12px;height:10px;border-radius:6px;background:' + gradient + ';border:1px solid var(--border,#2a3142)">' +
        (heatIdx == null ? '' :
          '<div style="position:absolute;top:-3px;bottom:-3px;left:calc(' + needlePct + '% - 2px);width:4px;background:#fff;border-radius:2px;box-shadow:0 0 0 1px rgba(0,0,0,0.55)"></div>') +
      '</div>' +
      '<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted,#9aa3b2);margin-top:4px">' +
        '<span>Cold</span><span>Cool</span><span>Warm</span><span>Hot</span><span>Scorching</span>' +
      '</div>' +
      '<div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">' + delta30Chip + delta1yChip + '</div>' +
      '<div style="margin-top:8px;font-size:11px;color:var(--muted,#9aa3b2);line-height:1.45">Composite of % above list, days-on-market, price cuts, and YoY ZHVI across ' + qualifiedMetros.length + ' qualifying metros.</div>' +
    '</div>';

  // Market card builder for carousels
  const marketCard = (m, mode) => {
    const accent = mode === 'hot' ? '#e8744a' : '#4a90e2';
    const kpi = mode === 'hot' ? 'pct_above_list' : 'pct_price_cut';
    const big = m.kpis?.[kpi]?.value;
    const bigPct = big == null ? '—' : Math.round(big * 100) + '%';
    const sub = mode === 'hot' ? 'above list' : 'price cuts';
    const yoy = m.kpis?.zhvi?.yoy_pct;
    const yoyColor = yoy == null ? 'var(--muted,#9aa3b2)' : (yoy > 0 ? '#3fb950' : yoy < 0 ? '#f85149' : 'var(--muted,#9aa3b2)');
    const sp = spark(m.kpis?.[kpi]?.spark, accent, 110, 24);
    return '<div style="scroll-snap-align:start;flex-shrink:0;width:150px;height:150px;display:flex;flex-direction:column;background:var(--panel,#141923);border:1px solid var(--border,#2a3142);border-top:3px solid ' + accent + ';border-radius:8px;padding:10px;box-sizing:border-box">' +
      '<div style="font-size:12px;font-weight:600;line-height:1.2;color:#e6e9ee;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + esc(m.short_name || m.name || '—') + '</div>' +
      '<div style="font-size:10px;color:var(--muted,#9aa3b2);margin-top:1px">' + esc(m.state || '') + '</div>' +
      '<div style="font-size:22px;font-weight:700;color:' + accent + ';margin-top:6px;line-height:1">' + bigPct + '</div>' +
      '<div style="font-size:10px;color:var(--muted,#9aa3b2);margin-top:1px">' + sub + '</div>' +
      '<div style="margin-top:auto;display:flex;align-items:center;justify-content:space-between;gap:6px">' +
        '<div style="flex:1;min-width:0">' + sp + '</div>' +
        '<div style="font-size:10px;font-weight:600;color:' + yoyColor + ';white-space:nowrap">' + (yoy == null ? '—' : (yoy > 0 ? '+' : '') + yoy.toFixed(1) + '%') + '</div>' +
      '</div>' +
    '</div>';
  };

  const carousel = (title, list, mode) => {
    if (!list.length) return '';
    const cards = list.map(m => marketCard(m, mode)).join('');
    return '<div style="font-size:13px;font-weight:600;color:#e6e9ee;padding:0 14px;margin-top:4px;margin-bottom:6px">' + title + '</div>' +
      '<div style="display:flex;gap:10px;overflow-x:auto;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;padding:4px 14px 12px 14px;margin:0">' +
        cards +
      '</div>';
  };

  const hotCarousel  = carousel('🔥 Hot Markets',     hotList,  'hot');
  const coolCarousel = carousel('❄️ Cooling Markets', coolList, 'cool');

  // Compressed KPI row
  const smallCard = (label, value, sub) =>
    '<div style="padding:8px 10px;background:var(--panel,#141923);border:1px solid var(--border,#2a3142);border-radius:8px;flex:1;min-width:0">' +
      '<div style="font-size:10px;color:var(--muted,#9aa3b2);text-transform:uppercase;letter-spacing:0.05em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + label + '</div>' +
      '<div style="font-size:14px;font-weight:600;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + value + '</div>' +
      (sub ? '<div style="font-size:10px;color:var(--muted,#9aa3b2);margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + sub + '</div>' : '') +
    '</div>';
  const natAgg = aggregate(metros);
  const natCards = [
    ['Median ZHVI',     fmtUsd(medianZhvi),                                              'across ' + metros.length + ' metros'],
    ['ZHVI YoY',        fmtPct(natAgg.zhviYoy),                                          'volume-weighted'],
    ['+YoY metros',     positiveYoy + ' / ' + metros.length,                             'positive ZHVI YoY'],
    ['Homes sold',      fmtNum(natAgg.homesSold),                                        'monthly, all metros'],
    ['Active listings', fmtNum(natAgg.activeListings),                                   'inventory'],
    ['Avg DOM',         avgDom == null ? '—' : avgDom + 'd',                             'days on market'],
    ['Above list',      natAgg.aboveList == null ? '—' : Math.round(natAgg.aboveList*100) + '%', 'sold over ask'],
    ['Price cuts',      natAgg.priceCut == null ? '—' : Math.round(natAgg.priceCut*100) + '%',   'listings reduced'],
  ];
  const kpiRowHtml =
    '<div style="font-size:13px;font-weight:600;color:#e6e9ee;padding:0 14px;margin-top:6px;margin-bottom:6px">National KPIs</div>' +
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;padding:0 14px">' +
      natCards.map(c => smallCard(c[0], c[1], c[2])).join('') +
    '</div>';

  const snapshotHtml =
    '<div style="padding:8px 14px 0 14px;font-size:10px;color:var(--muted,#9aa3b2);font-family:ui-monospace,monospace">snapshot ' + generated + ' UTC &middot; sources: Zillow Research, Redfin Data Center, FRED</div>';

  host.innerHTML = heroHtml + regionRowHtml + mapHtml + hotCarousel + coolCarousel + kpiRowHtml + snapshotHtml;
  renderRePanel();
  bindTiles();
}

// 8 focused KPIs based on cohort migration + tx-shape signals (not the
// noisy active-addresses/tx-count combo that was there before). See the
// agent report: cohort-driven metrics from bitinfocharts are the strongest
// whale signal we can get from free data. Replaces the prior 10-card set.
function renderWhaleKpisV2(){
  const w = whaleData();
  const dist = ((DATA.whale||{}).distribution || {}).buckets || [];
  const host = document.getElementById('whaleKpis');
  if (!host) return;
  const fmtBTC = b => b == null ? '—' :
    b >= 1e6 ? (b/1e6).toFixed(2) + 'M BTC' :
    b >= 1e3 ? (b/1e3).toFixed(1) + 'k BTC' :
    Math.round(b).toLocaleString() + ' BTC';
  const fmtPct = (p, d=2) => p == null ? '—' : (p >= 0 ? '+' : '') + p.toFixed(d) + '%';
  const last = a => (a||[]).slice(-1)[0];
  const meanN = (series, n) => {
    const arr = (series||[]).slice(-n).map(r => r?.value).filter(v => v != null);
    if (!arr.length) return null;
    return arr.reduce((s,v)=>s+v, 0) / arr.length;
  };
  const colorFor = (pct, t) => pct == null ? '' : (pct >= t ? 'green' : pct <= -t ? 'red' : 'amber');
  // Cohort helpers
  const whaleSup = r => (r.b1k_10k||0) + (r.b10k_100k||0) + (r.b100k_1m||0);
  const megaSup  = r => (r.b10k_100k||0) + (r.b100k_1m||0);
  const shrimpSup = r => (r.b0_01||0) + (r.b01_1||0);
  const totalSup = r => (r.b0_01||0)+(r.b01_1||0)+(r.b1_10||0)+(r.b10_100||0)+
                        (r.b100_1k||0)+(r.b1k_10k||0)+(r.b10k_100k||0)+(r.b100k_1m||0);

  const items = [];

  // ====== 4 cohort-based cards (need bitinfocharts data) ======
  if (dist.length >= 31){
    const cur = dist[dist.length-1];
    const prev30 = dist[dist.length-31];
    // 1. Whale Supply (≥1K BTC) + 30d % change
    const wNow = whaleSup(cur), w30 = whaleSup(prev30);
    const wPct = w30 ? (wNow - w30) / w30 * 100 : null;
    items.push({label:'Whale Supply (≥1K)', val: fmtBTC(wNow),
                cls: colorFor(wPct, 0.5), sub:`30d Δ ${fmtPct(wPct, 2)}`});
    // 2. Whale Δ 30d (BTC accumulated/sold)
    const wDeltaBtc = wNow - w30;
    items.push({label:'Whale Δ 30d', val: (wDeltaBtc>=0?'+':'') + fmtBTC(Math.abs(wDeltaBtc)),
                cls: wDeltaBtc >= 0 ? 'green' : 'red',
                sub:'net accumulation last 30d'});
    // 3. Mega-Whale Share (≥10K BTC as % of total tracked supply)
    const totalNow = totalSup(cur), totalPrev = totalSup(prev30);
    const megaShareNow = totalNow ? megaSup(cur) / totalNow * 100 : 0;
    const megaSharePrev = totalPrev ? megaSup(prev30) / totalPrev * 100 : 0;
    const sharePP = megaShareNow - megaSharePrev;
    items.push({label:'Mega-Whale Share', val: megaShareNow.toFixed(2)+'%',
                cls: sharePP >= 0.2 ? 'green' : sharePP <= -0.2 ? 'red' : 'amber',
                sub:`30d Δ ${sharePP>=0?'+':''}${sharePP.toFixed(2)}pp`});
    // 4. Shrimp Supply (<1 BTC) — retail counter-signal
    const sNow = shrimpSup(cur), sPrev = shrimpSup(prev30);
    const sPct = sPrev ? (sNow - sPrev) / sPrev * 100 : null;
    items.push({label:'Shrimp Supply (<1)', val: fmtBTC(sNow),
                cls: colorFor(sPct, 0.5), sub:`30d Δ ${fmtPct(sPct, 2)} · retail proxy`});
  }

  // ====== 4 flow/miner cards (from blockchain.info) ======
  // 5. Avg Tx Size USD — 7d vs 30d MA
  {
    const cur = (last(w.avg_tx_usd) || {}).value;
    const ma7  = meanN(w.avg_tx_usd, 7);
    const ma30 = meanN(w.avg_tx_usd, 30);
    const pct = (ma30 && ma7) ? (ma7 - ma30) / ma30 * 100 : null;
    items.push({label:'Avg Tx Size', val: cur ? fmtUSD(cur, 'auto') : '—',
                cls: colorFor(pct, 5), sub:`7d MA ${fmtPct(pct, 1)} vs 30d`});
  }
  // 6. Whale-Tx Proxy ($/active-addr, 7d MA vs 30d MA)
  {
    const v = (w.tx_volume_usd || []);
    const a = (w.active_addresses || []);
    const byDate = {};
    a.forEach(d => { if (d.date) byDate[d.date] = d.value; });
    const ratios = v.filter(d => d.date && byDate[d.date])
      .map(d => ({date:d.date, v: d.value / byDate[d.date]}));
    const m = n => ratios.slice(-n).reduce((s,r)=>s+r.v,0) / Math.min(n, ratios.length || 1);
    const r7 = ratios.length >= 7 ? m(7) : null;
    const r30 = ratios.length >= 30 ? m(30) : null;
    const pct = (r30 && r7) ? (r7 - r30) / r30 * 100 : null;
    items.push({label:'Whale-Tx Proxy', val: r7 != null ? fmtUSD(r7, 'auto') : '—',
                cls: colorFor(pct, 5), sub:`$/active addr · 7d ${fmtPct(pct, 1)}`});
  }
  // 7. Miner Revenue 7d sum + w/w delta
  {
    const arr = (w.miners_revenue_usd || []).map(r => r?.value).filter(v => v != null);
    const sum7 = arr.length >= 7 ? arr.slice(-7).reduce((s,v)=>s+v,0) : null;
    const sum14_7 = arr.length >= 14 ? arr.slice(-14, -7).reduce((s,v)=>s+v,0) : null;
    const pct = (sum14_7 && sum7) ? (sum7 - sum14_7) / sum14_7 * 100 : null;
    items.push({label:'Miner Revenue 7d', val: sum7 != null ? fmtUSD(sum7, 'auto') : '—',
                cls: colorFor(pct, 5), sub:`w/w ${fmtPct(pct, 1)}`});
  }
  // 8. Hash rate 30d trend
  {
    const arr = (w.hash_rate || []).map(r => r?.value).filter(v => v != null);
    const cur = arr.length ? arr[arr.length-1] : null;
    const prev30 = arr.length >= 31 ? arr[arr.length-31] : null;
    const pct = (prev30 && cur) ? (cur - prev30) / prev30 * 100 : null;
    items.push({label:'Hash rate 30d', val: cur ? (cur / 1e18).toFixed(1) + ' EH/s' : '—',
                cls: colorFor(pct, 2), sub:`30d ${fmtPct(pct, 1)}`});
  }

  host.innerHTML = items.map(i =>
    `<div class="card"><h3>${i.label}</h3><div class="v ${i.cls||''}">${i.val}</div>${i.sub?`<div class="sub">${i.sub}</div>`:''}</div>`
  ).join('');

  // "data as of" badge — show freshest date across primary series
  const asOfEl = document.getElementById('whaleAsOf');
  if (asOfEl){
    const candidates = [w.tx_volume_usd, w.active_addresses, w.miners_revenue_usd]
      .map(s => last(s)).filter(p => p && p.date);
    if (!candidates.length){
      asOfEl.textContent = ''; asOfEl.style.color = '';
    } else {
      const freshest = candidates.reduce((a,b) => a.date >= b.date ? a : b).date;
      const ageDays = Math.floor((Date.now() - new Date(freshest).getTime()) / 86400000);
      asOfEl.textContent = `data as of ${freshest} (${ageDays <= 0 ? 'today' : ageDays + 'd ago'})`;
      asOfEl.style.color = ageDays > 7 ? '#f59e0b' : '';
    }
  }
}

// Legacy KPI function kept for reference; renderWhaleKpisV2 is the new one
// wired into renderWhale(). Delete after a few commits if no rollback needed.
function renderWhaleKpis(){
  const w = whaleData();
  const last = (a) => (a||[]).slice(-1)[0];
  // Compute 1d delta-% from a value series. Returns null if <2 points.
  const delta1d = (series) => {
    const arr = series || [];
    if (arr.length < 2) return null;
    const cur = arr[arr.length-1]?.value;
    const prev = arr[arr.length-2]?.value;
    if (cur == null || prev == null || prev === 0) return null;
    return (cur - prev) / Math.abs(prev) * 100;
  };
  // Mean of the last N values.
  const meanN = (series, n) => {
    const arr = (series||[]).slice(-n).map(r => r?.value).filter(v => v != null);
    if (!arr.length) return null;
    return arr.reduce((s,v) => s+v, 0) / arr.length;
  };
  const deltaClass = (d) => d == null ? '' : (d >= 0 ? 'green' : 'red');
  const deltaStr = (d) => d == null ? '—' : (d >= 0 ? '+' : '') + d.toFixed(2) + '%';

  const items = [];

  // Active addresses
  {
    const cur = last(w.active_addresses);
    const d = delta1d(w.active_addresses);
    const avg30 = meanN(w.active_addresses, 30);
    items.push({
      label: 'Active addresses',
      val: cur ? fmtNum(cur.value, 0) : '—',
      cls: deltaClass(d),
      sub: `1d ${deltaStr(d)}${avg30 != null ? ` · 30d avg ${fmtNum(avg30, 0)}` : ''}`,
    });
  }
  // Tx count
  {
    const cur = last(w.tx_count);
    const d = delta1d(w.tx_count);
    items.push({
      label: 'Tx count',
      val: cur ? fmtNum(cur.value, 0) : '—',
      cls: deltaClass(d),
      sub: `1d ${deltaStr(d)}`,
    });
  }
  // Avg tx size — whale-movement proxy
  {
    const cur = last(w.avg_tx_usd);
    const d = delta1d(w.avg_tx_usd);
    items.push({
      label: 'Avg tx size',
      val: cur ? fmtUSD(cur.value, 'auto') : '—',
      cls: deltaClass(d),
      sub: `1d ${deltaStr(d)} · whale-movement proxy`,
    });
  }
  // Miner revenue (1d)
  {
    const cur = last(w.miners_revenue_usd);
    const d = delta1d(w.miners_revenue_usd);
    items.push({
      label: 'Miner revenue (1d)',
      val: cur ? fmtUSD(cur.value, 'auto') : '—',
      cls: deltaClass(d),
      sub: `1d ${deltaStr(d)}`,
    });
  }
  // Output volume (BTC)
  {
    const cur = last(w.output_volume_btc);
    const d = delta1d(w.output_volume_btc);
    items.push({
      label: 'Output volume',
      val: cur ? fmtNum(cur.value, 0) + ' BTC' : '—',
      cls: deltaClass(d),
      sub: `1d ${deltaStr(d)}`,
    });
  }
  // Hash rate (EH/s) — series is in TH/s but blockchain.info "hash_rate" is GH/s.
  // Existing chart treats it as TH/s; convert /1e9 from raw → EH/s here as
  // specified, which matches the order-of-magnitude expected by the UI.
  {
    const cur = last(w.hash_rate);
    const d = delta1d(w.hash_rate);
    const eh = cur ? cur.value / 1e9 : null;
    items.push({
      label: 'Hash rate',
      val: eh != null ? fmtNum(eh, 0) + ' EH/s' : '—',
      cls: deltaClass(d),
      sub: `1d ${deltaStr(d)}`,
    });
  }

  // --- Derived "tracking-style" KPIs ----------------------------------
  // Helper: read .value at offset from the end (0 = latest, 1 = yesterday).
  const at = (series, back) => {
    const arr = series || [];
    const i = arr.length - 1 - back;
    if (i < 0) return null;
    const r = arr[i];
    return (r && r.value != null) ? r.value : null;
  };

  // 7. Network velocity = tx_volume_usd / active_addresses (latest day).
  {
    const volCur = at(w.tx_volume_usd, 0);
    const addrCur = at(w.active_addresses, 0);
    const volPrev = at(w.tx_volume_usd, 1);
    const addrPrev = at(w.active_addresses, 1);
    const cur = (volCur != null && addrCur && addrCur !== 0) ? volCur / addrCur : null;
    const prev = (volPrev != null && addrPrev && addrPrev !== 0) ? volPrev / addrPrev : null;
    const d = (cur != null && prev != null && prev !== 0) ? (cur - prev) / Math.abs(prev) * 100 : null;
    items.push({
      label: 'Network velocity',
      val: cur != null ? fmtUSD(cur, 'auto') : '—',
      cls: deltaClass(d),
      sub: `1d ${deltaStr(d)} · USD moved per active address`,
    });
  }

  // 8. Miner profitability = miners_revenue_usd / (hash_rate / 1e9)  → $/EH/s.
  {
    const revCur = at(w.miners_revenue_usd, 0);
    const hashCur = at(w.hash_rate, 0);
    const revPrev = at(w.miners_revenue_usd, 1);
    const hashPrev = at(w.hash_rate, 1);
    const cur = (revCur != null && hashCur && hashCur !== 0) ? revCur / (hashCur / 1e9) : null;
    const prev = (revPrev != null && hashPrev && hashPrev !== 0) ? revPrev / (hashPrev / 1e9) : null;
    const d = (cur != null && prev != null && prev !== 0) ? (cur - prev) / Math.abs(prev) * 100 : null;
    items.push({
      label: 'Miner profitability',
      val: cur != null ? fmtUSD(cur, 'auto') : '—',
      cls: deltaClass(d),
      sub: `1d ${deltaStr(d)} · revenue per EH/s of hashpower`,
    });
  }

  // 9. 7d tx volume — sum of last 7d vs prior 7d.
  {
    const arr = (w.tx_volume_usd || []).map(r => r && r.value).filter(v => v != null);
    let cur = null, prev = null, d = null;
    if (arr.length >= 7) {
      cur = arr.slice(-7).reduce((s,v) => s+v, 0);
    }
    if (arr.length >= 14) {
      prev = arr.slice(-14, -7).reduce((s,v) => s+v, 0);
      if (prev !== 0) d = (cur - prev) / Math.abs(prev) * 100;
    }
    items.push({
      label: '7d tx volume',
      val: cur != null ? fmtUSD(cur, 'auto') : '—',
      cls: deltaClass(d),
      sub: `vs prior 7d: ${deltaStr(d)}`,
    });
  }

  // 10. 30d range position for active addresses — percentile within min↔max.
  {
    const arr = (w.active_addresses || []).slice(-30).map(r => r && r.value).filter(v => v != null);
    let pct = null;
    if (arr.length >= 2) {
      const mn = Math.min(...arr);
      const mx = Math.max(...arr);
      const cur = arr[arr.length - 1];
      if (mx !== mn && cur != null) pct = (cur - mn) / (mx - mn) * 100;
      else if (mx === mn && cur != null) pct = 100; // flat series → top of range
    }
    let cls = '';
    if (pct != null) {
      if (pct >= 66) cls = 'green';
      else if (pct <= 33) cls = 'red';
      else cls = 'amber';
    }
    items.push({
      label: '30d range position',
      val: pct != null ? pct.toFixed(0) + '%' : '—',
      cls,
      sub: 'of 30d active-address range',
    });
  }

  document.getElementById('whaleKpis').innerHTML = items.map(i =>
    `<div class="card"><h3>${i.label}</h3><div class="v ${i.cls||''}">${i.val}</div>${i.sub?`<div class="sub">${i.sub}</div>`:''}</div>`
  ).join('');

  // "data as of" badge — show freshest date across primary series so the user
  // notices when blockchain.info is stale.
  const asOfEl = document.getElementById('whaleAsOf');
  if (asOfEl){
    const candidates = [w.tx_volume_usd, w.active_addresses, w.large_tx]
      .map(s => last(s))
      .filter(p => p && p.date);
    if (!candidates.length){
      asOfEl.textContent = '';
      asOfEl.style.color = '';
    } else {
      const freshest = candidates.reduce((a,b) => a.date >= b.date ? a : b).date;
      const ageDays = Math.floor((Date.now() - new Date(freshest).getTime()) / 86400000);
      const ageStr = ageDays <= 0 ? 'today' : `${ageDays}d ago`;
      asOfEl.textContent = `data as of ${freshest} (${ageStr})`;
      asOfEl.style.color = ageDays > 7 ? '#f59e0b' : '';
    }
  }
}

function lineChart(canvasId, key, series, color, fmt){
  destroy(key);
  charts[key] = new Chart(document.getElementById(canvasId), {
    type:'line',
    data:{labels:series.map(r=>r.date), datasets:[{data:series.map(r=>r.value), borderColor:color, backgroundColor:color+'22', fill:true, tension:0.2, pointRadius:0, borderWidth:2}]},
    options: baseOpts({tooltipFmt:fmt}),
  });
}

function renderWhale(){
  const w = whaleData();
  lineChart('avgTxChart',  'avgTx',  ra(w.avg_tx_usd,        'mean'), '#a78bfa', v=>fmtUSD(v,'auto'));
  lineChart('txVolChart',  'txVol',  ra(w.tx_volume_usd,     'sum'),  '#22c55e', v=>fmtUSD(v,'auto'));
  lineChart('addrChart',   'addr',   ra(w.active_addresses,  'mean'), '#06b6d4', v=>fmtNum(v,0));
  lineChart('hashChart',   'hash',   ra(w.hash_rate,         'mean'), '#f7931a', v=>fmtNum(v,0)+' TH/s');
  lineChart('minerChart',  'miner',  ra(w.miners_revenue_usd,'sum'),  '#f59e0b', v=>fmtUSD(v,'auto'));
  lineChart('outputChart', 'output', ra(w.output_volume_btc, 'sum'),  '#627eea', v=>fmtNum(v,0)+' BTC');
  renderWhaleTracker();
  renderWhaleAlerts();
  renderWhaleCohortChart();
  renderWhaleProxyChart();
  renderMultichainWhale();
}

// Toggle which Whale-tab panel is visible. Called after state.whaleAsset
// changes and on initial render.
function syncWhalePanels(){
  const btc = document.getElementById('whaleBtcPanel');
  const eth = document.getElementById('whaleEthPanel');
  if (!btc || !eth) return;
  const isEth = state.whaleAsset === 'eth';
  btc.classList.toggle('hidden', isEth);
  eth.classList.toggle('hidden', !isEth);
}

// Dispatch table for the Whale tab: renders the currently-selected panel
// only. Lazy-rendering keeps chart sizing correct (Chart.js dislikes drawing
// to display:none canvases).
function renderWhalePanel(){
  syncWhalePanels();
  if (state.whaleAsset === 'eth'){
    renderWhaleEth();
  } else {
    renderWhaleSentiment();
    renderWhaleKpisV2();
    renderWhale();
    renderWhaleExtras();
  }
}

// ETH whale view — KPIs from Coin Metrics, largest 24h tx + network stats
// from Blockchair, gas oracle from the existing Etherscan v2 fetcher.
// ETH parallel of renderWhaleSentiment(). Reads DATA.whale.eth.sentiment
// (computed Python-side in fetch_market.compute_whale_sentiment_eth()) and
// renders into #whaleEthSentimentCard. Same gauge/composite-bar pattern as
// the BTC version so it inherits the same mobile-responsive behavior.
function renderWhaleSentimentEth(){
  const host = document.getElementById('whaleEthSentimentCard');
  if (!host) return;
  const s = (((DATA.whale || {}).eth) || {}).sentiment;
  if (!s || s.available === false || !Array.isArray(s.components) || !s.components.length){
    // Empty-state: hide cleanly so the panel doesn't show an awkward gap.
    host.style.display = 'none';
    return;
  }
  host.style.display = '';
  const color = signalColor(s.score);
  const pct = ((s.score + 100) / 200) * 100;
  const compRows = (s.components||[]).map(c => {
    const cls = c.contribution > 0 ? 'green' : (c.contribution < 0 ? 'red' : 'amber');
    const sign = (c.contribution>=0?'+':'') + c.contribution;
    return `<tr><td>${escapeHtml(c.name)}</td><td>${escapeHtml(String(c.value))}</td><td class="${cls}">${sign}</td><td style="color:var(--muted);font-size:12px">${escapeHtml(c.explanation||'')}</td></tr>`;
  }).join('');
  host.innerHTML = `
    <div class="head" style="align-items:flex-start">
      <div>
        <h2 style="font-size:15px">🐋 ETH Whale Sentiment Index</h2>
        <div class="desc">Composite ±100 from ETH on-chain proxies · as of ${escapeHtml(s.as_of||'?')}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:26px;font-weight:700;color:${color}">${escapeHtml(s.label||'')}</div>
        <div style="font-size:13px;color:var(--muted)">score <strong style="color:${color}">${s.score>=0?'+':''}${s.score}</strong> / ±100</div>
      </div>
    </div>
    <div style="height:10px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:5px;position:relative;margin:8px 0">
      <div style="position:absolute;top:-4px;left:calc(${pct.toFixed(1)}% - 4px);width:8px;height:18px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
    </div>
    <table style="margin-top:6px"><thead><tr><th>Component</th><th>Value</th><th>±</th><th>Read</th></tr></thead><tbody>${compRows}</tbody></table>
    <div class="sub" style="margin-top:8px;font-size:11px">${escapeHtml(s.disclaimer||'')}</div>
  `;
}

function renderWhaleEth(){
  // Sentiment card sits above the KPI strip. Safe to call even if data is
  // missing — it hides itself cleanly.
  renderWhaleSentimentEth();
  const eth = ((DATA.whale || {}).eth) || {};
  const bc  = eth.blockchair || {};
  const cm  = eth.coin_metrics || {};
  const gas = ((DATA.market || {}).eth_gas) || {};
  // Coin Metrics' TxTfrValAdjUSD is paid-only, so the on-chain transfer-volume
  // KPI/chart falls back to CoinGecko 24h trading volume (clearly labeled).
  const ethMarketVol = (((DATA.market || {}).eth) || {}).volume || [];

  const lastVal = (m) => { const s = cm[m] || []; return s.length ? s[s.length-1].value : null; };
  const aa  = lastVal('AdrActCnt');
  const txc = lastVal('TxCnt');
  const txv = ethMarketVol.length ? ethMarketVol[ethMarketVol.length-1].value : null;
  const sup = lastVal('SplyCur');
  // Prefer Blockchair-derived on-chain transfer volume (txs × avg-value × price).
  // Falls back to CoinGecko 24h trading volume (clearly labeled) if Blockchair
  // didn't return all three inputs.
  const onChainVol = bc.transfer_volume_24h_usd;
  const volKpi = (onChainVol != null)
    ? {label:'On-chain transfer volume (24h)',
       val: fmtUSD(onChainVol, 'auto'),
       sub:'via Blockchair — transactions_24h × avg_tx_value × price'}
    : {label:'24h trading volume',
       val: txv != null ? fmtUSD(txv, 'auto') : '—',
       sub:'CoinGecko — exchange-traded volume (on-chain feed unavailable)'};
  const kpis = [
    {label:'Active addresses (24h)', val: aa  != null ? fmtNum(aa, 0)                   : '—'},
    {label:'Transactions (24h)',     val: txc != null ? fmtNum(txc, 0)                  : '—'},
    volKpi,
    {label:'Supply (ETH)',           val: sup != null ? fmtNum(sup/1e6, 2) + 'M'        : '—'},
  ];
  const kpiHost = document.getElementById('whaleEthKpis');
  if (kpiHost) kpiHost.innerHTML = kpis.map(i =>
    `<div class="card"><h3>${i.label}</h3><div class="v">${i.val}</div>${i.sub?`<div class="sub">${i.sub}</div>`:''}</div>`
  ).join('');

  // Largest tx (24h) — Blockchair. Validate hash as 0x + 64 hex chars to defang
  // any javascript:/data: scheme injection through the href + innerHTML.
  const isEthTxHash = s => typeof s === 'string' && /^0x[0-9a-fA-F]{64}$/.test(s);
  const lt = bc.largest_tx_24h;
  const ltBox = document.getElementById('ethLargestTxBox');
  if (ltBox){
    const hash = (lt && isEthTxHash(lt.hash)) ? lt.hash : '';
    if (hash){
      const valFmt = lt.value_usd != null ? fmtUSD(lt.value_usd, 'auto') : '—';
      const shortHash = hash.slice(0, 10) + '…' + hash.slice(-8);
      ltBox.innerHTML = `<strong style="color:var(--text);font-size:18px">${valFmt}</strong>
        <span style="color:var(--muted)"> in a single transaction</span><br>
        <a href="https://etherscan.io/tx/${hash}" target="_blank" rel="noopener" style="color:#a78bfa;text-decoration:none">${shortHash} ↗</a>`;
    } else {
      ltBox.innerHTML = '<span style="color:var(--muted)">No data — Blockchair fetch may have failed.</span>';
    }
  }

  // Recent ETH whale transactions feed (≥ $1M, 24h) — sits right above the
  // single-largest card to give users the full feed first, headline second.
  renderEthWhaleAlerts();
  // Multi-horizon delta table + activity proxy chart — ETH parallels of the
  // BTC Tracker and Whale-Proxy cards. Each hides itself when the underlying
  // CM/Etherscan series are empty.
  renderEthWhaleTracker();
  renderEthWhaleProxyChart();

  // 180-day charts from Coin Metrics
  const slice180 = (arr) => (arr || []).slice(-180);
  lineChart('ethActiveAddrChart', 'ethActiveAddr', slice180(cm.AdrActCnt),      '#06b6d4', v=>fmtNum(v,0));
  lineChart('ethTxVolChart',      'ethTxVol',      slice180(ethMarketVol),      '#22c55e', v=>fmtUSD(v,'auto'));
  lineChart('ethTxCountChart',    'ethTxCount',    slice180(cm.TxCnt),          '#a78bfa', v=>fmtNum(v,0));
  lineChart('ethSupplyChart',     'ethSupply',     slice180(cm.SplyCur),        '#627eea', v=>fmtNum(v/1e6,2)+'M');

  // Etherscan 90d on-chain daily series. Env-gated by ETHERSCAN_API_KEY.
  // When the key is absent the fetcher returns {available:false,reason:...}
  // and we replace the chart with an inline hint instead of an empty canvas.
  const eds = eth.etherscan_daily || {};
  const edsCanvas  = document.getElementById('ethEtherscanDailyChart');
  const edsNoKey   = document.getElementById('ethEtherscanDailyNoKey');
  const edsSeries  = Array.isArray(eds.series) ? eds.series : [];
  const noKeyReason = (eds.available === false && eds.reason === 'no ETHERSCAN_API_KEY in env');
  if (noKeyReason || !edsSeries.length){
    destroy('ethEtherscanDaily');
    if (edsCanvas) edsCanvas.style.display = 'none';
    if (edsNoKey){
      edsNoKey.classList.remove('hidden');
      edsNoKey.innerHTML = noKeyReason
        ? 'Add <code>ETHERSCAN_API_KEY</code> to light up — free key from <a href="https://etherscan.io/apis" target="_blank" rel="noopener" style="color:#a78bfa">etherscan.io/apis</a>'
        : 'No data yet — Etherscan fetch may have failed or rate-limited.';
    }
  } else {
    if (edsCanvas) edsCanvas.style.display = '';
    if (edsNoKey)  edsNoKey.classList.add('hidden');
    lineChart('ethEtherscanDailyChart', 'ethEtherscanDaily', edsSeries.slice(-90),
              '#f59e0b', v=>fmtNum(v,0)+' blocks');
  }

  // Gas oracle
  const gasBox = document.getElementById('ethGasBox');
  if (gasBox){
    if (gas.safe_gwei != null || gas.propose_gwei != null || gas.fast_gwei != null){
      gasBox.innerHTML = `
        <div>Safe: <strong style="color:var(--text)">${(gas.safe_gwei||0).toFixed(1)} gwei</strong></div>
        <div>Propose: <strong style="color:var(--text)">${(gas.propose_gwei||0).toFixed(1)} gwei</strong></div>
        <div>Fast: <strong style="color:var(--text)">${(gas.fast_gwei||0).toFixed(1)} gwei</strong></div>
        <div style="margin-top:4px">Base fee: <strong style="color:var(--text)">${(gas.base_fee_gwei||0).toFixed(2)} gwei</strong></div>`;
    } else {
      gasBox.innerHTML = '<span>No gas data — Etherscan may have rate-limited.</span>';
    }
  }

  // Blockchair 24h network stats — txs, EIP-1559 burn, ERC-20/721 activity
  const statsBox = document.getElementById('ethStatsBox');
  if (statsBox){
    if (bc.blocks_24h || bc.transactions_24h){
      // Coerce: Blockchair sometimes returns numeric fields as strings.
      // Number(null/undefined/"") → NaN, which Number.isFinite rejects.
      const toFiniteNum = (v) => {
        if (v == null || v === '') return null;
        const n = Number(v);
        return Number.isFinite(n) ? n : null;
      };
      const avgFee = toFiniteNum(bc.avg_tx_fee_eth_24h);
      const mp    = toFiniteNum(bc.market_price_usd);
      const burn  = toFiniteNum(bc.burned_eth_24h);
      const erc20 = toFiniteNum(bc.erc20_transactions_24h);
      const erc721= toFiniteNum(bc.erc721_transactions_24h);
      const inflation = toFiniteNum(bc.inflation_eth_24h);
      // Deflationary if burn > inflation in the 24h window. Post-Merge this
      // flips between deflationary and mildly inflationary block-to-block.
      const netSupplyDelta = (burn != null && inflation != null) ? (inflation - burn) : null;
      const netCls = netSupplyDelta == null ? '' : (netSupplyDelta < 0 ? 'green' : 'red');
      const netLbl = netSupplyDelta == null ? '—' : (netSupplyDelta < 0 ? '⤓ deflationary' : '⤒ inflationary');
      statsBox.innerHTML = `
        <div>Blocks (24h): <strong style="color:var(--text)">${fmtNum(bc.blocks_24h||0, 0)}</strong></div>
        <div>Txs (24h): <strong style="color:var(--text)">${fmtNum(bc.transactions_24h||0, 0)}</strong></div>
        ${avgFee != null ? `<div>Avg tx fee: <strong style="color:var(--text)">${avgFee.toFixed(6)} ETH</strong>${mp != null ? ` (~$${(avgFee*mp).toFixed(2)})` : ''}</div>` : ''}
        ${burn != null ? `<div>EIP-1559 burn (24h): <strong class="${netCls}">${burn.toFixed(2)} ETH</strong>${mp != null ? ` (~${fmtUSD(burn*mp,'auto')})` : ''} <span style="color:var(--muted)">· ${netLbl}</span></div>` : ''}
        ${erc20  != null ? `<div>ERC-20 tx (24h): <strong style="color:var(--text)">${fmtNum(erc20, 0)}</strong></div>` : ''}
        ${erc721 != null ? `<div>ERC-721 tx (24h): <strong style="color:var(--text)">${fmtNum(erc721, 0)}</strong></div>` : ''}`;
    } else {
      statsBox.innerHTML = '<span>No Blockchair data.</span>';
    }
  }
}

// Recent Whale Transactions: vouts ≥ $1M scanned from the latest confirmed
// BTC block via mempool.space. Hidden when no transactions are present.
function renderWhaleAlerts(){
  const card = document.getElementById('whaleAlertsCard');
  if (!card) return;
  const txs = ((DATA.whale||{}).whale_transactions || []);
  if (!txs.length){ card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  const head = txs[0];
  const minsAgo = head.block_time ? Math.round((Date.now()/1000 - head.block_time)/60) : null;
  const note = document.getElementById('whaleAlertsNote');
  if (note){
    const heightPart = head.block_height ? `Block #${head.block_height.toLocaleString()}` : 'Latest block';
    const agePart    = minsAgo != null ? ` · ${minsAgo} min ago` : '';
    note.textContent = `${heightPart}${agePart} · ${txs.length} txs ≥ $1M`;
  }
  const tbody = document.getElementById('whaleAlertsBody');
  if (!tbody) return;
  // Validate txid as 64-char hex to defang any javascript:/data: scheme injection
  // before interpolating into href + innerHTML.
  const isHexTxid = s => typeof s === 'string' && /^[0-9a-fA-F]{64}$/.test(s);
  tbody.innerHTML = txs.slice(0, 10).map(t => {
    const txid = isHexTxid(t.txid) ? t.txid : '';
    const shortId = txid ? txid.slice(0,8) + '…' + txid.slice(-6) : '—';
    const txUrl = txid ? `https://mempool.space/tx/${txid}` : '#';
    const cls = (t.value_usd >= 10_000_000) ? 'green' : '';
    const blk = t.block_height ? t.block_height.toLocaleString() : '—';
    return `<tr>
      <td>${blk}</td>
      <td class="${cls}" style="text-align:right">${fmtUSD(t.value_usd, 'auto')}</td>
      <td style="text-align:right">${fmtNum(t.value_btc, 2)} BTC</td>
      <td><a href="${txUrl}" target="_blank" rel="noopener" style="color:#a78bfa;text-decoration:none">${shortId} ↗</a></td>
    </tr>`;
  }).join('');
}

// Recent ETH whale transactions: ≥ $1M last 24h from Blockchair. Hidden when
// no data. Mirrors renderWhaleAlerts() (BTC mempool feed) in structure.
function renderEthWhaleAlerts(){
  const card = document.getElementById('ethWhaleAlertsCard');
  if (!card) return;
  const txs = (((DATA.whale||{}).eth||{}).large_transactions) || [];
  if (!txs.length){ card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  const note = document.getElementById('ethWhaleAlertsNote');
  if (note){
    note.textContent = `${txs.length} txs ≥ $1M · last 24h`;
  }
  const tbody = document.getElementById('ethWhaleAlertsBody');
  if (!tbody) return;
  // Validate ETH tx hash as 0x + 64 hex chars to defang any javascript:/data:
  // scheme injection through the href + innerHTML.
  const isEthTxHash = s => typeof s === 'string' && /^0x[0-9a-fA-F]{64}$/.test(s);
  tbody.innerHTML = txs.slice(0, 10).map(t => {
    const hash = isEthTxHash(t.hash) ? t.hash : '';
    const shortHash = hash ? (hash.slice(0,10) + '…' + hash.slice(-8)) : '—';
    const txUrl = hash ? sanitizeUrl(`https://etherscan.io/tx/${hash}`) : '#';
    const eth = t.value_eth != null ? fmtNum(t.value_eth, 2) : '—';
    const usd = t.value_usd != null ? fmtUSD(t.value_usd, 'auto') : '—';
    const cls = (t.value_usd != null && t.value_usd >= 10_000_000) ? 'green' : '';
    const time = t.time ? escapeHtml(String(t.time)) : '—';
    const linkCell = hash
      ? `<a href="${txUrl}" target="_blank" rel="noopener" style="color:#a78bfa;text-decoration:none">${shortHash} ↗</a>`
      : '—';
    return `<tr>
      <td>${linkCell}</td>
      <td style="text-align:right">${eth}</td>
      <td class="${cls}" style="text-align:right">${usd}</td>
      <td style="color:var(--muted);font-size:12px">${time}</td>
    </tr>`;
  }).join('');
}

// ETH parallel of renderWhaleTracker(): multi-horizon snapshot table across
// the on-chain series we *do* have free access to on the ETH side. Rows are
// sourced from Coin Metrics (active addresses, tx count, supply), the
// Blockchair 24h snapshot (transfer volume, burn), and Etherscan
// (blocks/day, env-gated). Each row hides via "—" if its series is missing
// or too short; the whole card hides if no row has any data at all.
function renderEthWhaleTracker(){
  const card = document.getElementById('ethWhaleTrackerCard');
  const tbody = document.querySelector('#ethWhaleTrackerTable tbody');
  if (!card || !tbody) return;
  const eth = ((DATA.whale||{}).eth) || {};
  const cm  = eth.coin_metrics || {};
  const bc  = eth.blockchair || {};
  const eds = eth.etherscan_daily || {};
  // CM transfer-value (paid feed). May be missing; falls back to a dash.
  const txTfr = cm.TxTfrValAdjUSD || [];
  const edsSeries = Array.isArray(eds.series) ? eds.series : [];

  const rows = [
    { label: 'Active addresses',  series: cm.AdrActCnt || [],
      fmt: v => fmtNum(v, 0) },
    { label: 'Transactions (network)', series: cm.TxCnt || [],
      fmt: v => fmtNum(v, 0) },
    { label: 'Circulating supply (ETH)', series: cm.SplyCur || [],
      fmt: v => fmtNum(v/1e6, 2) + 'M ETH' },
    // Optional rows — these only populate when their feed returned data.
    { label: 'Transfer value (USD)', series: txTfr,
      fmt: v => fmtUSD(v, 'auto'),
      empty_note: 'paid feed' },
    { label: 'Blocks mined per day', series: edsSeries,
      fmt: v => fmtNum(v, 0) + ' blocks',
      empty_note: 'needs ETHERSCAN_API_KEY' },
  ];

  // Reuse the BTC tracker's pct-change helper for consistency.
  const dCell = (series, days) => {
    const d = _pctChange(series, days);
    if (d == null) return '<td>—</td>';
    const cls = d >= 0 ? 'green' : 'red';
    const sign = d >= 0 ? '+' : '';
    return `<td class="${cls}">${sign}${d.toFixed(2)}%</td>`;
  };

  let anyRowHasData = false;
  const html = rows.map(r => {
    const arr = r.series || [];
    const cur = arr.length ? arr[arr.length-1]?.value : null;
    if (cur != null) anyRowHasData = true;
    let today;
    if (cur != null){
      today = r.fmt(cur);
    } else if (r.empty_note){
      today = `<span style="color:var(--muted);font-size:11px">— (${r.empty_note})</span>`;
    } else {
      today = '—';
    }
    return `<tr>
      <td>${r.label}</td>
      <td>${today}</td>
      ${dCell(arr, 1)}
      ${dCell(arr, 7)}
      ${dCell(arr, 30)}
      ${dCell(arr, 90)}
    </tr>`;
  }).join('');

  if (!anyRowHasData){ card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  tbody.innerHTML = html;
}

// ETH parallel of renderWhaleProxyChart(): two-axis combined view of daily
// transactions + active addresses. When both rise together that's whale-
// shaped activity (more txs per active wallet). Hidden when either series
// is missing — we need both to make the cross-axis read meaningful.
function renderEthWhaleProxyChart(){
  const card = document.getElementById('ethWhaleProxyCard');
  if (!card) return;
  const eth = ((DATA.whale||{}).eth) || {};
  const cm  = eth.coin_metrics || {};
  const txc = cm.TxCnt || [];
  const aa  = cm.AdrActCnt || [];
  destroy('ethWhaleProxy');
  if (!txc.length || !aa.length){ card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  // Last 180 days, aligned on the union of dates so two y-axes stay synced.
  const slice = arr => (arr || []).slice(-180);
  const txcSeries = slice(txc);
  const aaSeries  = slice(aa);
  const dateSet = new Set([...txcSeries.map(r=>r.date), ...aaSeries.map(r=>r.date)]);
  const dates = Array.from(dateSet).sort();
  const txcByDate = Object.fromEntries(txcSeries.map(r=>[r.date, r.value]));
  const aaByDate  = Object.fromEntries(aaSeries.map(r=>[r.date, r.value]));
  const txcData = dates.map(d => txcByDate[d] ?? null);
  const aaData  = dates.map(d => aaByDate[d]  ?? null);
  charts.ethWhaleProxy = new Chart(document.getElementById('ethWhaleProxyChart'), {
    type:'line',
    data:{
      labels: dates,
      datasets:[
        {label:'Transactions per day', yAxisID:'y1', data:txcData,
         borderColor:'#a78bfa', backgroundColor:'#a78bfa22', fill:true,
         tension:0.2, pointRadius:0, borderWidth:1.8, spanGaps:true},
        {label:'Active addresses', yAxisID:'y2', data:aaData,
         borderColor:'#06b6d4', backgroundColor:'transparent', fill:false,
         tension:0.2, pointRadius:0, borderWidth:1.8, spanGaps:true},
      ],
    },
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{labels:{color:'#e6e8ee'}},
        tooltip:{mode:'index', intersect:false,
          callbacks:{label:ctx => {
            const v = ctx.parsed.y;
            return ctx.dataset.label + ': ' + fmtNum(v, 0);
          }}},
      },
      scales:{
        x:{ticks:{color:'#8a93a6', maxTicksLimit:10}, grid:{color:'#1f2533'}},
        y1:{type:'linear', position:'left', title:{display:true, text:'Transactions', color:'#a78bfa'},
            ticks:{color:'#8a93a6', callback:v=>fmtNum(v,0)}, grid:{color:'#1f2533'}},
        y2:{type:'linear', position:'right', title:{display:true, text:'Active addresses', color:'#06b6d4'},
            ticks:{color:'#8a93a6', callback:v=>fmtNum(v,0)}, grid:{display:false}},
      },
    },
  });
}

// Multi-chain whale snapshot: LTC / BCH / DOGE 24h Blockchair stats + the
// largest single tx per chain. Hidden when no chain data is present.
function renderMultichainWhale(){
  const card = document.getElementById('multichainWhaleCard');
  const grid = document.getElementById('multichainWhaleGrid');
  if (!card || !grid) return;
  const mc = ((DATA.whale||{}).multichain) || {};
  const keys = Object.keys(mc).filter(k => mc[k] && typeof mc[k] === 'object');
  if (!keys.length){ card.classList.add('hidden'); return; }
  // Per-chain explorer URL templates. Hash validated as 64 hex chars before
  // interpolation; chain id is whitelisted by lookup so it can never be
  // user-controlled.
  const EXPLORER = {
    'litecoin':     h => `https://blockchair.com/litecoin/transaction/${h}`,
    'bitcoin-cash': h => `https://blockchair.com/bitcoin-cash/transaction/${h}`,
    'dogecoin':     h => `https://blockchair.com/dogecoin/transaction/${h}`,
  };
  const isHexHash = s => typeof s === 'string' && /^[0-9a-fA-F]{64}$/.test(s);
  const cards = keys.map(k => {
    const c = mc[k] || {};
    const sym  = escapeHtml(c.symbol || k.toUpperCase());
    const name = escapeHtml(c.name || k);
    const price = c.market_price_usd != null ? fmtUSD(c.market_price_usd, 'auto') : '—';
    const blk = c.blocks_24h       != null ? fmtNum(c.blocks_24h, 0)       : '—';
    const txc = c.transactions_24h != null ? fmtNum(c.transactions_24h, 0) : '—';
    const sup = c.supply           != null ? fmtNum(c.supply, 0)           : '—';
    const lt  = c.largest_tx_24h || {};
    const hash = isHexHash(lt.hash) ? lt.hash : '';
    const mkUrl = EXPLORER[k];
    const txUrl = (hash && mkUrl) ? sanitizeUrl(mkUrl(hash)) : '#';
    const shortHash = hash ? (hash.slice(0,8) + '…' + hash.slice(-6)) : '—';
    const ltUsd = lt.value_usd != null ? fmtUSD(lt.value_usd, 'auto') : '—';
    const ltLink = hash
      ? `<a href="${txUrl}" target="_blank" rel="noopener" style="color:#a78bfa;text-decoration:none">${shortHash} ↗</a>`
      : '<span style="color:var(--muted)">—</span>';
    return `<div class="card" style="padding:12px 14px">
      <div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:6px">
        <span style="font-weight:700;font-size:18px;color:var(--text)">${sym}</span>
        <span class="sub" style="color:var(--muted);font-size:12px">${name}</span>
        <span style="margin-left:auto;font-size:12px;color:var(--text)">${price}</span>
      </div>
      <div class="sub" style="font-size:12px;color:var(--muted);line-height:1.6">
        <div>Blocks (24h): <strong style="color:var(--text)">${blk}</strong></div>
        <div>Tx (24h): <strong style="color:var(--text)">${txc}</strong></div>
        <div>Supply: <strong style="color:var(--text)">${sup}</strong></div>
        <div style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--border)">
          Largest 24h tx: <strong style="color:var(--text)">${ltUsd}</strong>
          <div>${ltLink}</div>
        </div>
      </div>
    </div>`;
  }).join('');
  if (!cards){ card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  grid.innerHTML = cards;
}

// BTC supply held: whales (≥1,000 BTC addresses) vs non-whales (<1,000 BTC).
// Real cohort data from bitinfocharts.com — daily back to ~2021-05. Honors
// the Range selector at the top of the Whale tab via _whaleRangeFilter.
function _whaleRangeFilter(rows){
  if (!rows || !rows.length) return rows || [];
  const range = state.range;
  const days = {'3m':90,'6m':180,'1y':365,'2y':730,'3y':1095}[range] || null;
  if (!days) return rows;
  return rows.slice(-days);
}

// Bin daily cohort rows down to weekly / monthly / quarterly / yearly buckets
// using the LAST value in each window (it's a stock metric — supply held —
// not a flow, so sampling the period-end value is the right aggregation).
function _binCohortRows(rows, mode){
  if (!rows || !rows.length) return [];
  if (mode === 'day') return rows;
  const keyFn = {
    week:    d => { const dt = new Date(d); const day = dt.getUTCDay() || 7;
                    dt.setUTCDate(dt.getUTCDate() - day + 1); return dt.toISOString().slice(0,10); },
    month:   d => d.slice(0,7) + '-01',
    quarter: d => { const [y,m] = d.split('-'); const q = Math.floor((parseInt(m,10)-1)/3); return `${y}-${String(q*3+1).padStart(2,'0')}-01`; },
    year:    d => d.slice(0,4) + '-01-01',
  }[mode] || (d => d);
  const seen = new Map();
  for (const r of rows) {
    const k = keyFn(r.date);
    // Last value wins → period-end snapshot
    seen.set(k, {...r, date: k});
  }
  return Array.from(seen.values()).sort((a,b) => a.date.localeCompare(b.date));
}

function renderWhaleCohortChart(){
  const dist = ((DATA.whale||{}).distribution || {});
  // Use the chart's own bin selector — independent from the tab Range buttons.
  // Show full history; the bin width controls visual density.
  const mode = state.cohortBin || 'month';
  const buckets = _binCohortRows(dist.buckets || [], mode);
  const kpiHost = document.getElementById('whaleCohortKpis');
  destroy('whaleCohort');
  if (!buckets.length) {
    if (kpiHost) kpiHost.innerHTML = '<div class="sub" style="color:var(--muted)">No cohort data — wait for next fetch, or run python app.py --fetch-market.</div>';
    return;
  }
  // Sum the 3 whale buckets vs the 5 non-whale buckets per row.
  const whales = buckets.map(r => (r.b1k_10k||0) + (r.b10k_100k||0) + (r.b100k_1m||0));
  const others = buckets.map(r => (r.b0_01||0) + (r.b01_1||0) + (r.b1_10||0) + (r.b10_100||0) + (r.b100_1k||0));
  const dates  = buckets.map(r => r.date);
  charts.whaleCohort = new Chart(document.getElementById('whaleCohortChart'), {
    type:'line',
    data:{
      labels: dates,
      datasets:[
        // Two-line view: whale supply and non-whale supply on the same axis,
        // both in BTC. Trend over time is the actionable signal — bar/stacked
        // hid the slope. Whales orange, non-whales blue, both filled lightly.
        {label:'Non-whales (<1,000 BTC)', data:others,
         borderColor:'#627eea', backgroundColor:'#627eea22',
         fill:false, tension:0.15, pointRadius:0, borderWidth:2},
        {label:'Whales (≥1,000 BTC)',     data:whales,
         borderColor:'#f7931a', backgroundColor:'#f7931a22',
         fill:false, tension:0.15, pointRadius:0, borderWidth:2},
      ],
    },
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{labels:{color:'#e6e8ee'}},
        tooltip:{mode:'index', intersect:false,
          callbacks:{label: ctx => ctx.dataset.label + ': ' + fmtNum(ctx.parsed.y, 0) + ' BTC'}},
      },
      scales:{
        x:{ticks:{color:'#8a93a6', maxTicksLimit:14}, grid:{display:false}},
        y:{ticks:{color:'#8a93a6', callback:v=>fmtNum(v/1e6, 1) + 'M'},
           grid:{color:'#1f2533'}, title:{display:true, text:'BTC supply held', color:'#8a93a6'}},
      },
    },
  });

  // KPI strip below the chart: latest snapshot
  if (kpiHost) {
    const last = buckets[buckets.length - 1];
    const wTotal = (last.b1k_10k||0) + (last.b10k_100k||0) + (last.b100k_1m||0);
    const nTotal = (last.b0_01||0) + (last.b01_1||0) + (last.b1_10||0) + (last.b10_100||0) + (last.b100_1k||0);
    const grand  = wTotal + nTotal;
    const whalePct = grand > 0 ? (wTotal / grand * 100) : 0;
    // 1y change in whale supply
    const lookback = Math.min(365, buckets.length - 1);
    const prev = buckets[buckets.length - 1 - lookback];
    const prevWhale = (prev.b1k_10k||0) + (prev.b10k_100k||0) + (prev.b100k_1m||0);
    const wDelta = prevWhale > 0 ? (wTotal - prevWhale) / prevWhale * 100 : null;
    const cards = [
      {label:'Whale supply (≥1K BTC)', val: fmtNum(wTotal/1e6, 2) + 'M BTC',
       sub: `${whalePct.toFixed(1)}% of tracked supply`, cls: ''},
      {label:'Non-whale supply',       val: fmtNum(nTotal/1e6, 2) + 'M BTC',
       sub: `${(100-whalePct).toFixed(1)}% of tracked supply`, cls: ''},
      {label:'≥10K BTC ("mega-whales")', val: fmtNum(((last.b10k_100k||0) + (last.b100k_1m||0))/1e6, 2) + 'M BTC',
       sub: `${last.date}`, cls: ''},
      {label:'Whale supply 1y Δ', val: wDelta == null ? '—' : `${wDelta>=0?'+':''}${wDelta.toFixed(2)}%`,
       sub:'whales accumulating or distributing', cls: wDelta == null ? '' : (wDelta>=0?'green':'red')},
    ];
    kpiHost.innerHTML = cards.map(c =>
      `<div class="card"><h3>${c.label}</h3><div class="v ${c.cls}">${c.val}</div><div class="sub">${c.sub}</div></div>`
    ).join('');
  }
}

// Whale activity proxy: combined two-axis chart of BTC moved per day +
// avg tx size USD. When both rise together that's whale-shaped activity.
// Range buttons (3M / 6M / 1Y / 2Y / 3Y / All) honored via the shared `ra`
// resampler — same as every other whale chart on this tab.
function renderWhaleProxyChart(){
  const w = whaleData();
  destroy('whaleProxy');
  const btcSeries  = ra(w.output_volume_btc, 'sum');
  const avgSeries  = ra(w.avg_tx_usd,        'mean');
  // Align on the union of dates so two y-axes stay synced.
  const dateSet = new Set([...btcSeries.map(r=>r.date), ...avgSeries.map(r=>r.date)]);
  const dates = Array.from(dateSet).sort();
  const btcByDate = Object.fromEntries(btcSeries.map(r=>[r.date, r.value]));
  const avgByDate = Object.fromEntries(avgSeries.map(r=>[r.date, r.value]));
  const btcData = dates.map(d => btcByDate[d] ?? null);
  const avgData = dates.map(d => avgByDate[d] ?? null);
  charts.whaleProxy = new Chart(document.getElementById('whaleProxyChart'), {
    type:'line',
    data:{
      labels: dates,
      datasets:[
        {label:'BTC moved on-chain', yAxisID:'y1', data:btcData,
         borderColor:'#627eea', backgroundColor:'#627eea22', fill:true,
         tension:0.2, pointRadius:0, borderWidth:1.8, spanGaps:true},
        {label:'Avg tx size (USD)', yAxisID:'y2', data:avgData,
         borderColor:'#f7931a', backgroundColor:'transparent', fill:false,
         tension:0.2, pointRadius:0, borderWidth:1.8, spanGaps:true},
      ],
    },
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{labels:{color:'#e6e8ee'}},
        tooltip:{mode:'index', intersect:false,
          callbacks:{label:ctx => {
            const v = ctx.parsed.y;
            return ctx.dataset.label + ': ' + (ctx.dataset.yAxisID === 'y1'
              ? fmtNum(v, 0) + ' BTC'
              : fmtUSD(v, 'auto'));
          }}},
      },
      scales:{
        x:{ticks:{color:'#8a93a6', maxTicksLimit:10}, grid:{color:'#1f2533'}},
        y1:{type:'linear', position:'left', title:{display:true, text:'BTC moved', color:'#627eea'},
            ticks:{color:'#8a93a6', callback:v=>fmtNum(v,0)+' BTC'}, grid:{color:'#1f2533'}},
        y2:{type:'linear', position:'right', title:{display:true, text:'Avg tx size USD', color:'#f7931a'},
            ticks:{color:'#8a93a6', callback:v=>fmtUSD(v,'auto')}, grid:{display:false}},
      },
    },
  });
}

// Multi-horizon delta table: each raw on-chain series shown at today vs
// 1d / 7d / 30d / 90d ago with coloured % deltas. Lives on the Whale tab
// between the KPI strip and the chart grid.
function _pctChange(series, daysBack){
  const arr = series || [];
  if (arr.length < daysBack + 1) return null;
  const cur = arr[arr.length-1]?.value;
  const old = arr[arr.length-1-daysBack]?.value;
  if (cur == null || old == null || old === 0) return null;
  return (cur - old) / Math.abs(old) * 100;
}

function renderWhaleTracker(){
  const tbody = document.querySelector('#whaleTrackerTable tbody');
  if (!tbody) return;
  const w = whaleData();

  // Each row: label, series, formatter for "Today" cell.
  const rows = [
    { label: 'Active addresses (network)', series: w.active_addresses,    fmt: v => fmtNum(v, 0) },
    { label: 'Tx count (network-wide)',    series: w.tx_count,            fmt: v => fmtNum(v, 0) },
    { label: 'Avg tx size',      series: w.avg_tx_usd,          fmt: v => fmtUSD(v, 'auto') },
    { label: 'Tx volume USD',    series: w.tx_volume_usd,       fmt: v => fmtUSD(v, 'auto') },
    // BTC actually moved on-chain — total of all transaction outputs that day.
    // Network-wide (blockchain.info doesn't expose per-address detail), but
    // since most BTC moved per day is in larger transactions, this is a
    // reasonable proxy for whale-cohort activity in BTC units.
    { label: 'BTC moved on-chain', series: w.output_volume_btc, fmt: v => fmtNum(v, 0) + ' BTC' },
    { label: 'Miner revenue',    series: w.miners_revenue_usd,  fmt: v => fmtUSD(v, 'auto') },
    // hash_rate raw is GH/s in blockchain.info; divide by 1e9 → EH/s for display.
    { label: 'Hash rate',        series: w.hash_rate,           fmt: v => fmtNum(v / 1e9, 0) + ' EH/s' },
  ];

  const dCell = (series, days) => {
    const d = _pctChange(series, days);
    if (d == null) return '<td>—</td>';
    const cls = d >= 0 ? 'green' : 'red';
    const sign = d >= 0 ? '+' : '';
    return `<td class="${cls}">${sign}${d.toFixed(2)}%</td>`;
  };

  tbody.innerHTML = rows.map(r => {
    const arr = r.series || [];
    const cur = arr.length ? arr[arr.length-1]?.value : null;
    const today = (cur != null) ? r.fmt(cur) : '—';
    return `<tr>
      <td>${r.label}</td>
      <td>${today}</td>
      ${dCell(r.series, 1)}
      ${dCell(r.series, 7)}
      ${dCell(r.series, 30)}
      ${dCell(r.series, 90)}
    </tr>`;
  }).join('');
}

// ---------- coverage / tabs / wiring ----------
function setActive(group, val){
  const isAssetGroup = (group === 'asset');
  const isWhaleAssetGroup = (group === 'whaleasset');
  const isEtfAssetGroup = (group === 'etfasset');
  const isFuturesAssetGroup = (group === 'futuresasset');
  document.querySelectorAll(`.btn[data-${group}]`).forEach(b => {
    b.classList.toggle('active', b.dataset[group] === val);
    // Only the BTC/ETH/LINK selector buttons get asset-tinted. Other groups
    // (Range, Period, Fundwin, Macrorange) keep the default active orange
    // — they don't represent an asset choice.
    if (isAssetGroup) {
      b.classList.toggle('eth',  state.asset === 'eth');
      b.classList.toggle('link', state.asset === 'link');
    }
    // Whale-tab BTC/ETH toggle: tint active button by selected asset.
    if (isWhaleAssetGroup) {
      b.classList.toggle('eth', state.whaleAsset === 'eth');
    }
    // ETF Flows BTC/ETH toggle: tint active button by selected asset.
    if (isEtfAssetGroup) {
      b.classList.toggle('eth', state.etfAsset === 'eth');
    }
    // Futures BTC/ETH/LINK/LTC toggle: tint active button by selected asset.
    if (isFuturesAssetGroup) {
      b.classList.toggle('eth',  state.futuresAsset === 'eth');
      b.classList.toggle('link', state.futuresAsset === 'link');
    }
  });
}

function renderCoverage(){
  const cov = document.getElementById('coverage');
  if (state.tab === 'etf'){
    const d = etfData();
    if (d.daily && d.daily.length){
      const f = d.daily[0].date, l = d.daily[d.daily.length-1].date;
      const days = Math.round((new Date(l)-new Date(f))/86400000);
      cov.textContent = `ETF ${etfAsset().toUpperCase()} ${f} → ${l} (${days}d, ${d.daily.length} obs)`;
    } else cov.textContent = `ETF ${etfAsset().toUpperCase()}: no data`;
  } else if (state.tab === 'trading'){
    const a = tradingAssetData();
    const p = a.price || [];
    if (p.length){
      const f = p[0].date, l = p[p.length-1].date;
      cov.textContent = `${state.asset.toUpperCase()} price ${f} → ${l} (${p.length} obs)`;
    } else cov.textContent = 'no market data';
  } else if (state.tab === 'whale'){
    const w = whaleData();
    const s = w.tx_volume_usd || [];
    if (s.length){
      const f = s[0].date, l = s[s.length-1].date;
      cov.textContent = `BTC on-chain ${f} → ${l} (${s.length} obs)`;
    } else cov.textContent = 'no whale data';
  } else {
    // Overview / Signals / Markets / DeFi: no single dominant data window —
    // leave the coverage span empty so the header reads cleanly.
    cov.textContent = '';
  }
}

// ---------- Insights bar ----------
function severityColor(sev){
  return ({
    good:   '#22c55e',
    bad:    '#ef4444',
    alert:  '#f59e0b',
    info:   '#06b6d4',
  })[sev] || '#a78bfa';
}
function severityIcon(sev, kind){
  if (kind === 'milestone') return '🏁';
  if (kind === 'anomaly')   return '⚠️';
  if (kind === 'signal')    return '📡';
  if (kind === 'trend')     return '📈';
  if (kind === 'etf')       return sev === 'bad' ? '📉' : '💵';
  return '•';
}
// Human-readable label for the current tab's insight bar header.
const TAB_LABELS = {
  etf: 'ETF flow insights',
  signals: 'Signals insights',
  trading: 'Trading insights',
  markets: 'Markets insights',
  defi: 'DeFi insights',
  whale: 'Whale insights',
  ainews: 'AI insights',
};
// Empty-state copy per tab — explains what would normally appear here so the
// reader knows "nothing today" rather than "is this broken?"
const TAB_EMPTY = {
  etf:     'No notable ETF flow changes today.',
  signals: 'No signal flips or extreme readings right now.',
  trading: 'No funding / sentiment / DVOL extremes right now.',
  markets: 'No notable macro / news / index moves right now.',
  defi:    'No notable DeFi / gas / TVL moves right now.',
  whale:   'On-chain looks quiet — no mempool / mining / hashrate anomalies.',
  ainews:  'No notable AI sentiment / funding / ticker moves right now.',
};

function renderInsights(){
  // The Insights bar is per-tab. Overview hides the bar entirely (it has its
  // own "Top insights" card inside the Overview content).
  const all = (DATA.insights || []);
  const tab = state.tab;
  const list = (tab === 'overview') ? all : all.filter(i => (i.tab || 'markets') === tab);
  const host = document.getElementById('insightsList');
  const cnt = document.getElementById('insightsCount');
  const label = TAB_LABELS[tab] || 'Insights';
  if (cnt) {
    const asOf = (DATA.generated_at || '').slice(0, 16);
    cnt.textContent = list.length
      ? `${label} · ${list.length} as of ${asOf}`
      : `${label} · none right now`;
  }
  // Re-label the strong "Insights" header if present (the bar's title).
  // Always set the strong header — was previously only setting it for
  // non-Overview tabs, which left a stale "ETF" or "Whale" label visible
  // if the user returned to Overview with the bar shown via the toggle.
  const headerStrong = host?.parentElement?.querySelector('strong');
  if (headerStrong) headerStrong.textContent = (tab === 'overview') ? 'Insights' : label;
  if (!list.length){
    const empty = TAB_EMPTY[tab] || 'Nothing unusual right now. Load more data or wait for the next refresh.';
    host.innerHTML = '<div class="sub" style="color:var(--muted)">' + empty + '</div>';
    return;
  }
  const cardHTML = (i) => {
    const c = severityColor(i.severity);
    const ic = severityIcon(i.severity, i.kind);
    const detail = i.detail ? `<div class="sub" style="font-size:10px;color:var(--muted);margin-top:1px">${escapeHtml(i.detail)}</div>` : '';
    return `<div style="display:flex;align-items:flex-start;gap:8px;padding:6px 10px;background:#0e1118;border:1px solid var(--border);border-left:3px solid ${c};border-radius:8px;max-width:360px;flex:1 1 280px">
      <span style="font-size:13px;line-height:1.2">${ic}</span>
      <div style="line-height:1.25">
        <div style="font-size:12px;color:var(--text)">${escapeHtml(i.headline)}</div>
        ${detail}
      </div>
    </div>`;
  };
  host.innerHTML = list.map(cardHTML).join('');

  // Also populate the AI News tab's inline insights card (when present).
  // Use the TIGHT card style from renderOverviewInsights() — the global-bar
  // cardHTML has flex:1 1 280px / max-width:360px (meant for horizontal
  // wrapping) which made cards stretch vertically inside a column container.
  // Capped at 3 to match the Crypto Overview layout.
  const inlineHost = document.getElementById('aiNewsInsights');
  if (inlineHost) {
    const ainewsList = all.filter(i => (i.tab || 'markets') === 'ainews').slice(0, 3);
    if (!ainewsList.length) {
      inlineHost.innerHTML = '<div class="sub" style="color:var(--muted);font-size:12px;padding:14px">' + (TAB_EMPTY['ainews']) + '</div>';
    } else {
      inlineHost.innerHTML = ainewsList.map(i => {
        const c = severityColor(i.severity);
        const ic = severityIcon(i.severity, i.kind);
        const detail = i.detail ? `<div class="sub" style="font-size:10px;color:var(--muted);margin-top:2px">${escapeHtml(i.detail)}</div>` : '';
        return `<div style="display:flex;align-items:flex-start;gap:8px;padding:8px 12px;background:#10151f;border:1px solid var(--border);border-left:3px solid ${c};border-radius:6px">
          <span style="font-size:13px">${ic}</span>
          <div style="flex:1;line-height:1.3">
            <div style="font-size:12px">${escapeHtml(i.headline)}</div>
            ${detail}
          </div>
        </div>`;
      }).join('');
    }
  }
}
function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// ---------- Markets tab ----------
const marketState = { sort: 'rank' };

// Markets tab was consolidated into Crypto Overview. The Top 25 / sortable
// markets table + Trending CoinGecko list were dropped (already covered by
// the Top-50 signals strip + Top-15 by mcap widget on Overview). What
// remains: traditional indices (top bar) + DEX pools (bottom of Overview),
// rendered by their own helper functions called from renderOverview().

// Render two GeckoTerminal DEX pool tables side-by-side on the Markets tab.
// Free DEX coverage across 1,800+ DEXes / 260+ chains, sourced from the
// /networks/trending_pools and /networks/new_pools endpoints.
function renderGeckoTerminalPools(){
  const gt = (DATA.market && DATA.market.geckoterminal) || {};
  const trending = gt.trending_pools || [];
  const fresh = gt.new_pools || [];
  const fillTable = (tbodySel, rows) => {
    const tbody = document.querySelector(tbodySel);
    if (!tbody) return;
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:14px">No data yet — wait for next refresh</td></tr>';
      return;
    }
    tbody.innerHTML = rows.slice(0, 10).map((p, i) => {
      const ch = p.change_24h_pct;
      const chCls = ch == null ? '' : (ch >= 0 ? 'green' : 'red');
      const chStr = ch == null ? '—' : (ch >= 0 ? '+' : '') + ch.toFixed(1) + '%';
      return `<tr>
        <td style="padding-left:14px;color:var(--muted)">${i+1}</td>
        <td style="text-align:left"><strong>${escapeHtml(p.name||'?')}</strong>
          <div class="sub" style="font-size:10px;color:var(--muted)">${escapeHtml(p.dex||'?')}</div></td>
        <td style="text-align:left">${escapeHtml(p.network||'?')}</td>
        <td>${fmtUSD(p.volume_24h_usd||0, 'auto')}</td>
        <td class="${chCls}">${chStr}</td>
        <td>${(p.transactions_24h||0).toLocaleString()}</td>
      </tr>`;
    }).join('');
  };
  fillTable('#gtTrendingTable tbody', trending);
  fillTable('#gtNewTable tbody', fresh);
}

function renderSparkline(values, isUp, w, h){
  if (!values || values.length < 2) return '';
  values = values.filter(v => typeof v === 'number' && isFinite(v));
  if (values.length < 2) return '';
  w = w || 90; h = h || 24;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const pts = values.map((v, i) => {
    const x = (i / (values.length - 1)) * w;
    const y = h - ((v - min) / range) * h;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  const color = isUp ? '#22c55e' : '#ef4444';
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="vertical-align:middle"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}

// data-mktsort buttons no longer exist (Markets tab deleted); kept as a
// no-op guard so we don't error if any old browser cache still has them.
document.querySelectorAll('.btn[data-mktsort]').forEach(b =>
  b.addEventListener('click', () => {
    /* noop — Markets tab consolidated into Crypto Overview */
  })
);

// ---------- DeFi tab ----------
// Brand colors for the 4 chains we render per-chain content for. Module-
// scope so renderDefi() and renderDefiChainSection() can share.
const DEFI_CHAIN_COLORS = {
  'Ethereum': '#627eea',
  'Solana':   '#14f195',
  'Arbitrum': '#28a0f0',
  'Base':     '#0052ff',
};

function renderDefi(){
  // DATA.defi is lazy-loaded via the sidecar mechanism (see SIDECAR_KEYS /
  // SIDECAR_FOR_TAB). On first paint after switching to this tab it may be
  // an empty object — the per-section guards below (chains.length, etc.)
  // degrade silently, and renderAll re-runs once the fetch lands.
  const defi = DATA.defi || {};
  const llama = (DATA.market || {}).defillama || {};
  const chains = defi.chains || [];
  const protocols = defi.protocols || [];
  const yields = defi.yields_stablecoin || [];
  const bridges = ((defi.bridges || {}).top_bridges) || [];

  // ---- DeFi sentiment composite card (top of tab) ----
  renderDefiSentiment();

  // ---- 4-card KPI strip (mirrors Whale tab layout) ----
  const totalTvl = chains.reduce((s, c) => s + (c.tvl_usd || 0), 0);
  const stable7d = llama.stablecoin_7d_change_usd;
  const stable7dStr = (stable7d == null)
    ? '—'
    : `7d ${stable7d>=0?'+':''}${fmtUSD(stable7d,'auto')}`;
  const items = [
    {label: 'Stablecoin mcap',  val: fmtUSD(llama.stablecoin_mcap_usd, 'auto'), sub: stable7dStr},
    {label: 'DEX 24h volume',   val: fmtUSD(llama.dex_volume_24h_usd,  'auto'), sub: 'DefiLlama'},
    {label: 'Protocol fees 24h',val: fmtUSD(llama.fees_24h_usd,        'auto'), sub: 'DefiLlama'},
    {label: 'Total DeFi TVL',   val: fmtUSD(totalTvl,                  'auto'), sub: `${chains.length} chains`},
  ];
  document.getElementById('defiKpis').innerHTML = items.map(i =>
    `<div class="card"><h3>${escapeHtml(i.label)}</h3><div class="v">${i.val}</div>${i.sub?`<div class="sub">${escapeHtml(i.sub)}</div>`:''}</div>`
  ).join('');

  // ---- Per-chain section (TVL history + summary + top protocols on chain) ----
  renderDefiChainSection();

  // ---- Global protocols table (top 15, shrunk from 25) ----
  const protoBody = document.querySelector('#defiProtocolsTable tbody');
  if (protoBody) {
    const top15 = protocols.slice(0, 15);
    protoBody.innerHTML = top15.map((p, i) => {
      const dir = v => v == null ? 'amber' : v >= 0 ? 'green' : 'red';
      const pct = v => v == null ? '—' : (v>=0?'+':'') + v.toFixed(2) + '%';
      return `<tr>
        <td style="color:var(--muted)">${i+1}</td>
        <td><strong>${escapeHtml(p.name||'')}</strong></td>
        <td><span class="sub" style="color:var(--muted);font-size:11px">${escapeHtml(p.category||'')}</span></td>
        <td>${fmtUSD(p.tvl_usd,'auto')}</td>
        <td class="${dir(p.change_1d_pct)}">${pct(p.change_1d_pct)}</td>
        <td class="${dir(p.change_7d_pct)}">${pct(p.change_7d_pct)}</td>
        <td class="${dir(p.change_1m_pct)}">${pct(p.change_1m_pct)}</td>
      </tr>`;
    }).join('');
  }

  // ---- Yields table ----
  const yieldsBody = document.querySelector('#defiYieldsTable tbody');
  if (yieldsBody) {
    yieldsBody.innerHTML = yields.map(y => `<tr>
      <td><strong>${escapeHtml(y.project||'')}</strong> <span class="sub" style="color:var(--muted);font-size:11px">${escapeHtml(y.symbol||'')}</span></td>
      <td>${escapeHtml(y.chain||'')}</td>
      <td>${fmtUSD(y.tvl_usd,'auto')}</td>
      <td class="${(y.apy_pct||0)>=4?'green':(y.apy_pct||0)>=1?'amber':'red'}">${(y.apy_pct||0).toFixed(2)}%</td>
    </tr>`).join('');
  }

  // ---- Optional bridges card (hidden when empty) ----
  const bridgesCard = document.getElementById('defiBridgesCard');
  const bridgesBody = document.querySelector('#defiBridgesTable tbody');
  if (bridgesCard && bridgesBody) {
    if (bridges.length) {
      bridgesCard.classList.remove('hidden');
      bridgesBody.innerHTML = bridges.slice(0, 15).map((b, i) => `<tr>
        <td style="color:var(--muted)">${i+1}</td>
        <td><strong>${escapeHtml(b.name||b.chain||'')}</strong></td>
        <td>${fmtUSD(b.volume_24h_usd ?? b.volume_24h ?? b.volume_usd_24h, 'auto')}</td>
        <td>${fmtUSD(b.volume_7d_usd  ?? b.volume_7d  ?? b.volume_usd_7d,  'auto')}</td>
      </tr>`).join('');
    } else {
      bridgesCard.classList.add('hidden');
      bridgesBody.innerHTML = '';
    }
  }
}

// Per-chain renderer. Drives chain summary cards, TVL-history line chart,
// and top-10 protocols on the selected chain. Called by renderDefi() on tab
// open AND by the chain-selector click handler, so toggling between chains
// only refreshes this section (not the KPI strip or global tables).
function renderDefiChainSection(){
  const defi = DATA.defi || {};
  const chains = defi.chains || [];
  const protocols = defi.protocols || [];
  const tvlHistory = defi.tvl_history || {};
  const selected = state.defiChain || 'Ethereum';
  const totalTvl = chains.reduce((s, c) => s + (c.tvl_usd || 0), 0);

  // Chain summary cards: TVL, share, 1d / 7d / 30d change
  const chainEntry = chains.find(c => c.name === selected) || {};
  const tvl = chainEntry.tvl_usd || 0;
  const share = totalTvl > 0 ? (tvl / totalTvl) * 100 : 0;
  const pctCardHtml = v => {
    if (v == null) return '<div class="v">—</div>';
    const cls = v >= 0 ? 'green' : 'red';
    return `<div class="v ${cls}">${v>=0?'+':''}${v.toFixed(2)}%</div>`;
  };
  const summaryItems = [
    {label: `${selected} TVL`, html: `<div class="v">${fmtUSD(tvl, 'auto')}</div>`, sub: `${share.toFixed(1)}% of global`},
    {label: '1d change',       html: pctCardHtml(chainEntry.change_1d_pct),         sub: '24-hour'},
    {label: '7d change',       html: pctCardHtml(chainEntry.change_7d_pct),         sub: 'weekly'},
    {label: '30d change',      html: pctCardHtml(chainEntry.change_1m_pct),         sub: 'monthly'},
  ];
  const sumHost = document.getElementById('defiChainSummary');
  if (sumHost) {
    sumHost.innerHTML = summaryItems.map(i =>
      `<div class="card"><h3>${escapeHtml(i.label)}</h3>${i.html}<div class="sub">${escapeHtml(i.sub)}</div></div>`
    ).join('');
  }

  // Section titles
  const titleA = document.getElementById('defiTvlHistoryTitle');
  const titleB = document.getElementById('defiTopProtoTitle');
  if (titleA) titleA.textContent = selected;
  if (titleB) titleB.textContent = selected;

  // TVL history line chart — single-chain
  destroy('defiTvlHistory');
  const series = tvlHistory[selected] || [];
  if (series.length) {
    const color = DEFI_CHAIN_COLORS[selected] || '#a78bfa';
    charts.defiTvlHistory = new Chart(document.getElementById('defiTvlHistoryChart'), {
      type:'line',
      data:{
        labels: series.map(p => p.date),
        datasets: [{
          label: selected,
          data: series.map(p => p.tvl_usd),
          borderColor: color,
          backgroundColor: color + '22',
          pointRadius: 0,
          borderWidth: 1.8,
          tension: 0.2,
          fill: true,
        }],
      },
      options:{
        responsive:true, maintainAspectRatio:false,
        plugins:{legend:{display:false}, tooltip:{mode:'index', intersect:false, callbacks:{label: ctx => `${ctx.dataset.label}: ${fmtUSD(ctx.parsed.y,'auto')}`}}},
        scales:{
          x:{ticks:{color:'#8a93a6', maxTicksLimit:10}, grid:{color:'#1f2533'}},
          y:{ticks:{color:'#8a93a6', callback:v=>fmtUSD(v,'auto')}, grid:{color:'#1f2533'}},
        },
      },
    });
  }

  // Top 10 protocols on the selected chain — filter via chains.includes().
  // TVL shown is the protocol's global TVL (DefiLlama doesn't break out per-
  // chain TVL in this payload); this surfaces which protocols touch this
  // chain ranked by overall scale.
  const chainProtoBody = document.querySelector('#defiChainProtocolsTable tbody');
  if (chainProtoBody) {
    const filtered = protocols.filter(p => Array.isArray(p.chains) && p.chains.includes(selected)).slice(0, 10);
    if (filtered.length) {
      chainProtoBody.innerHTML = filtered.map((p, i) => {
        const dir = v => v == null ? 'amber' : v >= 0 ? 'green' : 'red';
        const pct = v => v == null ? '—' : (v>=0?'+':'') + v.toFixed(2) + '%';
        return `<tr>
          <td style="color:var(--muted)">${i+1}</td>
          <td><strong>${escapeHtml(p.name||'')}</strong></td>
          <td><span class="sub" style="color:var(--muted);font-size:11px">${escapeHtml(p.category||'')}</span></td>
          <td>${fmtUSD(p.tvl_usd,'auto')}</td>
          <td class="${dir(p.change_1d_pct)}">${pct(p.change_1d_pct)}</td>
          <td class="${dir(p.change_7d_pct)}">${pct(p.change_7d_pct)}</td>
        </tr>`;
      }).join('');
    } else {
      chainProtoBody.innerHTML = `<tr><td colspan="6" class="sub" style="color:var(--muted);padding:12px">No protocols found for ${escapeHtml(selected)}.</td></tr>`;
    }
  }
}

// ---------- News feed (Trading tab) ----------
function renderNews(){
  const news = (DATA.market || {}).news || [];
  const host = document.getElementById('newsFeed');
  if (!host) return;
  if (!news.length) {
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No data available.</div>';
    return;
  }
  host.innerHTML = news.slice(0, 25).map(n =>
    `<a href="${sanitizeUrl(n.url)}" target="_blank" rel="noopener" style="display:block;padding:10px 12px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text);transition:background .1s" onmouseover="this.style.background='#10151f'" onmouseout="this.style.background=''">
      <div style="font-size:12px;color:var(--muted);margin-bottom:3px">
        <span style="color:#a78bfa;font-weight:600">${escapeHtml(n.source||'')}</span> · ${escapeHtml(n.date||'')}
      </div>
      <div style="font-size:13px;line-height:1.35;margin-bottom:3px">${escapeHtml(n.title||'')}</div>
      ${n.body ? `<div class="sub" style="font-size:11px;color:var(--muted)">${escapeHtml(n.body)}</div>` : ''}
    </a>`
  ).join('');
}

// ---------- Macro overlay (FRED) ----------
function _macroDaysForRange(r){
  return ({'1M':30, '3M':90, '6M':180, '1Y':365})[r] || 365;
}
function _macroFilter(series, days){
  if (!Array.isArray(series) || !series.length) return [];
  if (!days) return series;
  const last = new Date(series[series.length-1].date);
  const cutoff = new Date(last.getTime() - days*86400000);
  return series.filter(p => new Date(p.date) >= cutoff);
}
function _macroAlignToDates(series, dates){
  // Returns values aligned to `dates`, forward-filling from `series`.
  const out = new Array(dates.length).fill(null);
  if (!series.length || !dates.length) return out;
  let i = 0;
  let lastVal = null;
  for (let d = 0; d < dates.length; d++){
    while (i < series.length && series[i].date <= dates[d]){
      lastVal = series[i].value;
      i++;
    }
    out[d] = lastVal;
  }
  return out;
}
function _macroNormalize(values){
  // Find first non-null as base, normalize to 100.
  let base = null;
  for (const v of values){
    if (v != null && isFinite(v) && v !== 0){ base = v; break; }
  }
  if (base == null) return values.slice();
  return values.map(v => (v == null || !isFinite(v)) ? null : (v / base) * 100);
}
function renderMacro(){
  const section = document.getElementById('macroSection');
  if (!section) return;
  const fred = (DATA.market || {}).fred;
  const disabled = document.getElementById('macroDisabled');
  const enabled = document.getElementById('macroEnabled');
  if (!fred || !fred.available){
    if (disabled) disabled.classList.remove('hidden');
    if (enabled) enabled.classList.add('hidden');
    destroy('macro');
    return;
  }
  if (disabled) disabled.classList.add('hidden');
  if (enabled) enabled.classList.remove('hidden');

  const days = _macroDaysForRange(state.macroRange);
  // BTC price from market data (CoinGecko)
  const btcRaw = ((DATA.market||{}).btc || {}).price || [];
  const btc = _macroFilter(btcRaw, days);

  // Union of dates from BTC (daily, dense) — use BTC dates as the X axis.
  const labels = btc.map(p => p.date);
  if (!labels.length){
    destroy('macro');
    return;
  }
  const btcVals = btc.map(p => p.value);

  const dxyAll  = _macroFilter(fred.dxy          || [], days);
  const spxAll  = _macroFilter(fred.sp500        || [], days);
  const goldAll = _macroFilter(fred.gold         || [], days);
  const tnxAll  = _macroFilter(fred.treasury_10y || [], days);

  const dxyAligned  = _macroAlignToDates(dxyAll,  labels);
  const spxAligned  = _macroAlignToDates(spxAll,  labels);
  const goldAligned = _macroAlignToDates(goldAll, labels);
  const tnxAligned  = _macroAlignToDates(tnxAll,  labels);

  const btcNorm  = _macroNormalize(btcVals);
  const dxyNorm  = _macroNormalize(dxyAligned);
  const spxNorm  = _macroNormalize(spxAligned);
  const goldNorm = _macroNormalize(goldAligned);
  const tnxNorm  = _macroNormalize(tnxAligned);

  destroy('macro');
  charts.macro = new Chart(document.getElementById('macroChart'), {
    type:'line',
    data:{labels, datasets:[
      {label:'BTC',         data:btcNorm,  borderColor:'#f7931a', backgroundColor:'transparent', tension:0.2, pointRadius:0, borderWidth:2},
      {label:'DXY',         data:dxyNorm,  borderColor:'#22c55e', backgroundColor:'transparent', tension:0.2, pointRadius:0, borderWidth:1.5},
      {label:'S&P 500',     data:spxNorm,  borderColor:'#a78bfa', backgroundColor:'transparent', tension:0.2, pointRadius:0, borderWidth:1.5},
      {label:'Gold',        data:goldNorm, borderColor:'#facc15', backgroundColor:'transparent', tension:0.2, pointRadius:0, borderWidth:1.5},
      {label:'10Y Treasury',data:tnxNorm,  borderColor:'#06b6d4', backgroundColor:'transparent', tension:0.2, pointRadius:0, borderWidth:1.5},
    ]},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{labels:{color:'#e6e8ee'}},
        tooltip:{mode:'index', intersect:false, callbacks:{label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y == null ? '—' : ctx.parsed.y.toFixed(2)}`}},
      },
      scales:{
        x:{ticks:{color:'#8a93a6', maxTicksLimit:10}, grid:{color:'#1f2533'}},
        y:{title:{display:true, text:'Index (start = 100)', color:'#8a93a6'}, ticks:{color:'#8a93a6'}, grid:{color:'#1f2533'}},
      },
    },
  });

  // KPI cards: each series' latest value + 1d change
  function _lastChange(series){
    if (!series || series.length < 2) {
      const last = (series && series.length) ? series[series.length-1].value : null;
      return {last, change: null};
    }
    const last = series[series.length-1].value;
    const prev = series[series.length-2].value;
    if (last == null || prev == null || prev === 0) return {last, change: null};
    return {last, change: ((last - prev) / prev) * 100};
  }
  const cards = [
    {label:'BTC',         color:'#f7931a', fmt:v=>'$'+(v||0).toLocaleString(undefined,{maximumFractionDigits:0}), src:btcRaw},
    {label:'DXY',         color:'#22c55e', fmt:v=>(v||0).toFixed(2),                                                src:(fred.dxy||[])},
    {label:'S&P 500',     color:'#a78bfa', fmt:v=>(v||0).toLocaleString(undefined,{maximumFractionDigits:0}),       src:(fred.sp500||[])},
    {label:'Gold',        color:'#facc15', fmt:v=>'$'+(v||0).toLocaleString(undefined,{maximumFractionDigits:0}),   src:(fred.gold||[])},
    {label:'10Y Treasury',color:'#06b6d4', fmt:v=>(v||0).toFixed(2)+'%',                                            src:(fred.treasury_10y||[])},
  ];
  const host = document.getElementById('macroKpis');
  if (host){
    host.innerHTML = cards.map(c => {
      const {last, change} = _lastChange(c.src);
      const ch = (change == null) ? '<span class="sub" style="color:var(--muted)">—</span>'
                : `<span class="${change>=0?'green':'red'}">${change>=0?'+':''}${change.toFixed(2)}%</span>`;
      return `<div class="card" style="padding:10px 12px;min-width:130px;flex:1 1 130px;border-left:3px solid ${c.color}">
        <div class="lbl" style="margin:0">${c.label}</div>
        <div class="v" style="font-size:16px;font-weight:600;margin-top:2px">${last == null ? '—' : c.fmt(last)}</div>
        <div class="sub" style="font-size:11px;margin-top:2px">1d: ${ch}</div>
      </div>`;
    }).join('');
  }
}

// ---------- Whale tab additions: difficulty + Lightning + mining pools ----------
function renderWhaleExtras(){
  const extra = (DATA.market || {}).mempool_extra || {};
  const diff = extra.difficulty_adjustment || {};
  const ln = extra.lightning || {};
  const pools = extra.pools || {};

  // Difficulty card
  const dEl = document.getElementById('diffAdjBox');
  if (dEl) {
    // Use explicit `== null` so a legitimate 0 (e.g. zero blocks remaining,
    // or zero predicted change) still renders the card instead of showing
    // "Loading…" forever.
    if (diff.remaining_blocks == null && diff.difficulty_change_pct == null) {
      dEl.innerHTML = '<span style="color:var(--muted)">Loading…</span>';
    } else {
      // Distinguish missing data ("— days") from a legitimate zero so the card
      // doesn't claim "~0.0 days" when the API simply didn't return the field.
      const days = diff.remaining_time_ms == null ? null : diff.remaining_time_ms / 86400000;
      const daysStr = days == null ? '—' : days.toFixed(1);
      const changeColor = (diff.difficulty_change_pct || 0) >= 0 ? 'red' : 'green';  // higher diff = harder on miners
      dEl.innerHTML = `
        <div class="v" style="font-size:20px;font-weight:600;color:var(--text)">${(diff.difficulty_change_pct||0).toFixed(2)}%</div>
        <div class="sub" style="color:var(--muted)">estimated next retarget</div>
        <div style="margin-top:8px;font-size:11px">
          <span class="sub">Blocks left: <strong>${diff.remaining_blocks?.toLocaleString()||'?'}</strong></span> ·
          <span class="sub">~<strong>${daysStr}</strong> days</span><br>
          <span class="sub">Progress: ${(diff.progress_pct||0).toFixed(1)}%</span>
        </div>`;
    }
  }

  // Lightning card
  const lEl = document.getElementById('lightningBox');
  if (lEl) {
    // Explicit null-check: an LN network that genuinely has 0 nodes (e.g.
    // during a regional fetch outage) should not be stuck on "Loading…".
    if (ln.node_count == null) {
      lEl.innerHTML = '<span style="color:var(--muted)">Loading…</span>';
    } else {
      lEl.innerHTML = `
        <div class="v" style="font-size:20px;font-weight:600;color:var(--text)">${(ln.total_capacity_btc||0).toFixed(0)} BTC</div>
        <div class="sub" style="color:var(--muted)">total network capacity</div>
        <div style="margin-top:8px;font-size:11px">
          <span class="sub">Nodes: <strong>${(ln.node_count||0).toLocaleString()}</strong> (${(ln.clearnet_nodes||0).toLocaleString()} clearnet, ${(ln.tor_nodes||0).toLocaleString()} Tor)</span><br>
          <span class="sub">Channels: <strong>${(ln.channel_count||0).toLocaleString()}</strong></span> ·
          <span class="sub">avg ${(ln.avg_capacity_btc||0).toFixed(3)} BTC/ch</span>
        </div>`;
    }
  }

  // Mining pools chart
  const poolsArr = pools.pools || [];
  document.getElementById('poolsTop2').textContent = pools.top2_concentration_pct ? `${pools.top2_concentration_pct.toFixed(1)}%` : '?';
  destroy('miningPools');
  if (poolsArr.length) {
    const palette = ['#f7931a','#627eea','#22c55e','#a78bfa','#ec4899','#06b6d4','#f59e0b','#10b981','#8b5cf6','#ef4444','#14b8a6','#fb923c','#84cc16','#06b6d4','#a855f7'];
    charts.miningPools = new Chart(document.getElementById('miningPoolsChart'), {
      type:'bar',
      data:{
        labels: poolsArr.map(p => p.name),
        datasets:[{
          data: poolsArr.map(p => p.share_pct),
          backgroundColor: poolsArr.map((_, i) => palette[i % palette.length]),
          borderWidth: 0,
        }],
      },
      options:{
        indexAxis: 'y',
        responsive:true, maintainAspectRatio:false,
        plugins:{legend:{display:false}, tooltip:{callbacks:{label: ctx => `${ctx.parsed.x.toFixed(2)}% (${poolsArr[ctx.dataIndex].blocks} blocks)`}}},
        scales:{
          x:{title:{display:true,text:'Share of blocks (%)',color:'#8a93a6'}, ticks:{color:'#8a93a6'}, grid:{color:'#1f2533'}},
          y:{ticks:{color:'#e6e8ee'}, grid:{display:false}},
        },
      },
    });
  }
}

// ---------- Overview tab (landing page) ----------
function renderOverview(){
  renderOverviewSentiment();      // crypto market sentiment composite card
  renderOverviewSignals();
  renderCoinbaseSpot();           // compact Coinbase exchange bid/ask + 24h range
  renderCoinbasePulse();          // taker buy/sell ratio per coin (Coinbase /trades)
  renderOverviewStrongBuys();
  renderOverviewTop15();
  renderOverviewMacro();
  renderOverviewNews();           // top 3-item teaser + bottom 10-item feed
  renderOverviewInsights();
  renderOverviewAiStocks();       // AI-exposed stocks card (right side of top row)
  renderGeckoTerminalPools();     // bottom — also moved from Markets
}

// Coinbase spot widget: one compact row per asset showing bid/ask, 24h
// high/low range, 24h change %, and 24h volume in coin units. Cross-checks
// the aggregate price in the asset signal cards against a single regulated
// US venue. Source: DATA.market.coinbase (REST snapshot, fetched per refresh).
function renderCoinbaseSpot(){
  const wrap = document.getElementById('coinbaseSpotWrap');
  const tbody = document.querySelector('#coinbaseSpotTable tbody');
  if (!wrap || !tbody) return;
  const cb = ((DATA.market || {}).coinbase) || {};
  const order = ['btc', 'eth', 'link', 'ltc'];
  const rows = order.filter(k => cb[k] && typeof cb[k] === 'object');
  if (!rows.length){
    wrap.classList.add('hidden');
    tbody.innerHTML = '';
    return;
  }
  wrap.classList.remove('hidden');
  tbody.innerHTML = rows.map(k => {
    const q = cb[k] || {};
    const sym = k.toUpperCase();
    const bid = (q.bid != null) ? fmtUSD(q.bid, 'auto') : '—';
    const ask = (q.ask != null) ? fmtUSD(q.ask, 'auto') : '—';
    const lo  = (q.low_24h  != null) ? fmtUSD(q.low_24h,  'auto') : '—';
    const hi  = (q.high_24h != null) ? fmtUSD(q.high_24h, 'auto') : '—';
    const pct = (typeof q.change_24h_pct === 'number') ? q.change_24h_pct : null;
    const pctStr = (pct == null) ? '—' : ((pct >= 0 ? '+' : '') + pct.toFixed(2) + '%');
    const pctCls = (pct == null) ? '' : (pct >= 0 ? 'green' : 'red');
    const vol = (q.volume_24h != null) ? (fmtNum(q.volume_24h, q.volume_24h >= 1000 ? 0 : 2) + ' ' + escapeHtml(sym)) : '—';
    return `<tr>
      <td><strong>${escapeHtml(sym)}</strong></td>
      <td style="text-align:right;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;font-variant-numeric:tabular-nums">${bid} / ${ask}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${lo} → ${hi}</td>
      <td class="${pctCls}" style="text-align:right;font-variant-numeric:tabular-nums">${pctStr}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${vol}</td>
    </tr>`;
  }).join('');
}

// Coinbase Pulse: per-coin taker buy/sell ratio computed from the last 300
// trades on the Coinbase Exchange /trades endpoint. Color-coded green/red
// at 55/45% — taker buys >55% reads as bullish flow, <45% bearish, in
// between is balanced. Inline SVG sparkline of buy ratio over the trailing
// ~25 minutes when ≥2 buckets are present (fast-moving pairs often only
// give us one bucket because all 300 trades happened in <5 min). Source:
// DATA.market.coinbase_pulse, written by fetch_coinbase.py.
function renderCoinbasePulse(){
  const wrap = document.getElementById('coinbasePulseWrap');
  const tbody = document.querySelector('#coinbasePulseTable tbody');
  const meta = document.getElementById('coinbasePulseMeta');
  if (!wrap || !tbody) return;
  const pulse = ((DATA.market || {}).coinbase_pulse) || {};
  const byCoin = pulse.by_coin || {};
  const order = ['BTC', 'ETH', 'SOL', 'LINK'];
  const rows = order.filter(k => byCoin[k] && typeof byCoin[k] === 'object');
  if (!rows.length){
    wrap.classList.add('hidden');
    tbody.innerHTML = '';
    if (meta) meta.textContent = '';
    return;
  }
  wrap.classList.remove('hidden');

  // Inline SVG sparkline of buy ratio over the trailing ~25 minutes. Plots
  // each bucket's buy ratio on a 0-1 y-axis with a 50% reference line.
  // Returns "—" when fewer than 2 buckets exist (single point isn't a trend).
  function brSparkline(points){
    if (!Array.isArray(points) || points.length < 2) return '—';
    const w = 72, h = 18, pad = 1;
    const xs = points.map((_, i) => pad + i * (w - 2*pad) / (points.length - 1));
    const ys = points.map(p => {
      const br = Math.max(0, Math.min(1, Number(p.br) || 0));
      return pad + (1 - br) * (h - 2*pad);
    });
    const d = xs.map((x, i) => (i === 0 ? 'M' : 'L') + x.toFixed(1) + ',' + ys[i].toFixed(1)).join(' ');
    const last = Number(points[points.length-1].br) || 0;
    const stroke = last >= 0.55 ? '#22c55e' : (last <= 0.45 ? '#ef4444' : '#94a3b8');
    const midY = pad + 0.5 * (h - 2*pad);
    return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true" style="vertical-align:middle">
      <line x1="0" y1="${midY.toFixed(1)}" x2="${w}" y2="${midY.toFixed(1)}" stroke="#2a3142" stroke-dasharray="2,2" stroke-width="0.5"/>
      <path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>
    </svg>`;
  }

  tbody.innerHTML = rows.map(sym => {
    const q = byCoin[sym] || {};
    const price = (q.price != null) ? fmtUSD(q.price, 'auto') : '—';
    const pct = (typeof q.change_24h_pct === 'number') ? q.change_24h_pct : null;
    const pctStr = (pct == null) ? '—' : ((pct >= 0 ? '+' : '') + pct.toFixed(2) + '%');
    const pctCls = (pct == null) ? '' : (pct >= 0 ? 'green' : 'red');
    const br = (typeof q.buy_ratio_volume === 'number') ? q.buy_ratio_volume : null;
    let brStr = '—', brCls = '', brColor = 'var(--muted)';
    if (br != null){
      brStr = (br * 100).toFixed(1) + '%';
      if (br >= 0.55){ brCls = 'green'; brColor = '#22c55e'; }
      else if (br <= 0.45){ brCls = 'red'; brColor = '#ef4444'; }
      else { brCls = ''; brColor = '#94a3b8'; }
    }
    const brCount = (typeof q.buy_ratio_count === 'number')
      ? ' <span style="color:var(--muted);font-size:10px">(' + (q.buy_ratio_count * 100).toFixed(0) + '% by count)</span>'
      : '';
    const spark = brSparkline(q.buy_ratio_sparkline);
    const spread = (q.spread_bps != null) ? (Number(q.spread_bps).toFixed(2) + ' bps') : '—';
    const volUsd = (q.vol_24h_usd != null) ? fmtUSD(q.vol_24h_usd / 1e6) : '—';  // fmtUSD takes millions by default
    return `<tr>
      <td><strong>${escapeHtml(sym)}</strong></td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${price}</td>
      <td class="${pctCls}" style="text-align:right;font-variant-numeric:tabular-nums">${pctStr}</td>
      <td class="${brCls}" style="text-align:right;font-variant-numeric:tabular-nums;color:${brColor};font-weight:700">${brStr}${brCount}</td>
      <td style="text-align:right">${spark}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${spread}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${volUsd}</td>
    </tr>`;
  }).join('');

  if (meta){
    const when = pulse.generated_at || '';
    const src = pulse.source || 'Coinbase Exchange';
    meta.textContent = src + (when ? ' · generated ' + when : '');
  }
}

// Top 25 coins by market-cap rank from the top-50 signal computation.
// Different from Strong Buys (which filters by STRONG BUY label) — this
// shows the structural "core" of the market regardless of signal direction.
// Cards click through to the same detail modal as the Signals-tab strip.
function renderOverviewTop15(){
  const wrap = document.getElementById('overviewTop15Wrap');
  const host = document.getElementById('overviewTop15');
  if (!wrap || !host) return;
  const isStable = s => { const u=(s||'').toUpperCase(); return /^USD/.test(u) || /USD$/.test(u) || u==='DAI'; };
  // The four pinned assets (BTC/ETH/LINK/LTC) already render in their own
  // big cards above this grid with their signal score chip surfaced. Exclude
  // them here so the grid is genuinely "the OTHER coins worth watching"
  // instead of duplicating what the user just saw.
  const PINNED_ON_TOP = new Set(['BTC', 'ETH', 'LINK', 'LTC']);
  // signals_top20 is sorted by SCORE — re-sort by rank for this widget.
  const top15 = (DATA.signals_top20 || [])
    .filter(s => s && !isStable(s.symbol) && !PINNED_ON_TOP.has((s.symbol || '').toUpperCase()))
    .slice()
    .sort((a,b) => (a.rank ?? 999) - (b.rank ?? 999))
    .slice(0, 25);
  if (!top15.length){
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');
  window._top20SignalsCache = window._top20SignalsCache || {};
  host.innerHTML = top15.map(s => {
    const sym = (s.symbol||'').toUpperCase();
    const color = signalColor(s.score);
    window._top20SignalsCache[sym] = s;
    const img = sanitizeUrl(s.image, '')
      ? `<img src="${sanitizeUrl(s.image, '')}" alt="" style="width:28px;height:28px;border-radius:50%">`
      : `<div style="width:28px;height:28px;border-radius:50%;background:${color}33"></div>`;
    const priceStr = (s.price != null)
      ? '$' + Number(s.price).toLocaleString(undefined, {maximumFractionDigits: s.price>=1000?0:s.price>=1?2:6})
      : '';
    return `<div class="card" data-symbol="${sym}" role="button" tabindex="0" aria-label="Open ${sym} signal detail" style="cursor:pointer;padding:8px 10px;display:flex;align-items:center;gap:9px;min-height:72px;border-left:3px solid ${color}">
      ${img}
      <div style="flex:1;min-width:0;overflow:hidden">
        <div style="display:flex;align-items:baseline;gap:5px;flex-wrap:wrap">
          <span style="font-weight:700;font-size:12px">${escapeHtml(sym)}</span>
          ${priceStr ? `<span style="font-size:10px;color:var(--text);font-variant-numeric:tabular-nums">${priceStr}</span>` : ''}
        </div>
        <div class="sub" style="color:var(--muted);font-size:10px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden">${escapeHtml(s.name||'')}${s.rank ? ' · #' + s.rank : ''}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:11px;font-weight:700;color:${color};line-height:1.1">${escapeHtml(s.label||'')}</div>
        <div style="font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums">${(s.score>=0?'+':'')+s.score}</div>
      </div>
    </div>`;
  }).join('');
  host.querySelectorAll('[data-symbol]').forEach(el =>
    el.addEventListener('click', () => openSignalDetail(el.getAttribute('data-symbol')))
  );
}

// Up to 5 STRONG BUY + BUY signals pulled from the top-50 strip, surfaced
// prominently on the Crypto Overview before the news row. Hides the
// whole section when zero qualifying signals exist. Sorted by score so
// STRONG BUYs (>=50) appear first. Cards click through to the same
// detail modal the Signals-tab strip uses (cache is shared).
function renderOverviewStrongBuys(){
  const wrap = document.getElementById('overviewStrongBuysWrap');
  const host = document.getElementById('overviewStrongBuys');
  if (!wrap || !host) return;
  const isStable = s => { const u=(s||'').toUpperCase(); return /^USD/.test(u) || /USD$/.test(u) || u==='DAI'; };
  const QUALIFYING = new Set(['STRONG BUY', 'BUY']);
  const strongs = (DATA.signals_top20 || [])
    .filter(s => s && !isStable(s.symbol) && QUALIFYING.has((s.label || '').toUpperCase()))
    .slice()
    .sort((a, b) => (Number(b.score) || 0) - (Number(a.score) || 0))
    .slice(0, 5);
  if (!strongs.length){
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');
  // Re-cache so the click handler can find these too (top-20 strip may
  // not have rendered yet on a first overview-only page load).
  window._top20SignalsCache = window._top20SignalsCache || {};
  host.innerHTML = strongs.map(s => {
    const sym = (s.symbol||'').toUpperCase();
    const color = signalColor(s.score);
    window._top20SignalsCache[sym] = s;
    const img = sanitizeUrl(s.image, '')
      ? `<img src="${sanitizeUrl(s.image, '')}" alt="" style="width:28px;height:28px;border-radius:50%">`
      : `<div style="width:28px;height:28px;border-radius:50%;background:${color}33"></div>`;
    const priceStr = (s.price != null)
      ? '$' + Number(s.price).toLocaleString(undefined, {maximumFractionDigits: s.price>=1000?0:s.price>=1?2:6})
      : '';
    return `<div class="card" data-symbol="${sym}" role="button" tabindex="0" aria-label="Open ${sym} signal detail" style="cursor:pointer;padding:8px 10px;display:flex;align-items:center;gap:9px;min-height:72px;border-left:3px solid ${color}">
      ${img}
      <div style="flex:1;min-width:0;overflow:hidden">
        <div style="display:flex;align-items:baseline;gap:5px;flex-wrap:wrap">
          <span style="font-weight:700;font-size:12px">${escapeHtml(sym)}</span>
          ${priceStr ? `<span style="font-size:10px;color:var(--text);font-variant-numeric:tabular-nums">${priceStr}</span>` : ''}
        </div>
        <div class="sub" style="color:var(--muted);font-size:10px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden">${escapeHtml(s.name||'')}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:11px;font-weight:700;color:${color};line-height:1.1">${escapeHtml(s.label||'')}</div>
        <div style="font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums">${(s.score>=0?'+':'')+s.score}</div>
      </div>
    </div>`;
  }).join('');
  host.querySelectorAll('[data-symbol]').forEach(el =>
    el.addEventListener('click', () => openSignalDetail(el.getAttribute('data-symbol')))
  );
}

// Which signal cards appear on Overview. User-configurable via the
// ⚙️ Configure button — selection persists in localStorage so it
// survives reloads. Backend computes a signal for every asset key that
// has price data in payload.market; UI just picks which to display.
const SIGNAL_SUPPORTED = ['btc','eth','link','ltc'];
const SIGNAL_DEFAULT = ['btc','eth','link','ltc'];

function getSignalOrder(){
  try {
    const raw = localStorage.getItem('overviewSignals');
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length) {
        return parsed.filter(a => SIGNAL_SUPPORTED.includes(a));
      }
    }
  } catch(_) {}
  return SIGNAL_DEFAULT.slice();
}

function renderOverviewSignals(){
  // Top-row asset cards on the Overview tab. Shows latest price, 24h
  // % change, 24h volume, AND the composite signal score so the four
  // pinned assets carry their own decision-relevant info up front
  // (avoids duplicating them in the "Other top coins" grid below).
  // Click opens the universal Signal + POC modal — consistent with
  // every other coin-card entry point on the dashboard.
  const market = DATA.market || {};
  const order = getSignalOrder();
  const accent = a => ({btc:'#f7931a', eth:'#627eea', link:'#2a5ada', ltc:'#bfbbbb'})[a] || '#a78bfa';
  const ASSET_NAMES = {btc:'Bitcoin', eth:'Ethereum', link:'Chainlink', ltc:'Litecoin'};
  const fmtPrice = p => {
    if (p == null) return '—';
    const d = p >= 1000 ? 0 : (p >= 1 ? 2 : 4);
    return '$' + p.toLocaleString(undefined, {maximumFractionDigits: d, minimumFractionDigits: d});
  };
  const fmtVol = v => {
    if (v == null) return '—';
    if (v >= 1e9) return '$' + (v/1e9).toFixed(2) + 'B';
    if (v >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
    return '$' + Math.round(v).toLocaleString();
  };
  const host = document.getElementById('overviewSignals');
  if (!host) return;
  const sigsAll = DATA.signals || {};
  host.innerHTML = order.map(a => {
    const m = market[a] || {};
    const prices = m.price || [];
    const vols = m.volume || [];
    const lastP = prices.length ? prices[prices.length-1].value : null;
    const prevP = prices.length > 1 ? prices[prices.length-2].value : null;
    const lastV = vols.length ? vols[vols.length-1].value : null;
    const pct = (lastP != null && prevP && prevP > 0) ? (lastP / prevP - 1) * 100 : null;
    const pctColor = pct == null ? 'var(--muted)' : (pct >= 0 ? '#22c55e' : '#ef4444');
    const pctTxt  = pct == null ? '—' : (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%';
    const asOf = prices.length ? prices[prices.length-1].date : '—';
    // Signal chip — score + label from DATA.signals[a]. signalColor() does
    // the threshold mapping (≥50 STRONG BUY green, ≤-50 STRONG SELL red,
    // graded in between). When the asset isn't computed (rare — fetch
    // failure), the chip is omitted rather than showing a placeholder.
    const sig = sigsAll[a];
    let signalChip = '';
    if (sig && typeof sig.score === 'number'){
      const sc = sig.score;
      const lbl = sig.label || '';
      const col = signalColor(sc);
      const txt = lbl + ' ' + (sc >= 0 ? '+' : '') + sc;
      signalChip = '<span style="background:' + col + '22;color:' + col +
        ';border:1px solid ' + col + '55;padding:2px 8px;border-radius:4px;' +
        'font-size:11px;font-weight:700;white-space:nowrap;letter-spacing:.02em">' +
        escapeHtml(txt) + '</span>';
    }
    // 90-day price sparkline — uses the same renderSparkline helper as the
    // Traditional Indices bar. SVG scales via viewBox; width is responsive
    // (svg style="width:100%") so the chart fills the card regardless of
    // grid column width. Hidden when there's not enough history (<2 points).
    const sparkVals = prices.slice(-90).map(p => p.value).filter(v => typeof v === 'number' && isFinite(v));
    const sparkUp = sparkVals.length >= 2 ? (sparkVals[sparkVals.length-1] >= sparkVals[0]) : true;
    const sparkSvg = sparkVals.length >= 2
      ? renderSparkline(sparkVals, sparkUp, 240, 36).replace('<svg ', '<svg style="width:100%;height:36px;display:block" ')
      : '';
    const sparkBlock = sparkSvg
      ? `<div style="margin-top:8px;line-height:0">${sparkSvg}</div>`
      : '';
    const SYM = a.toUpperCase();
    return `<div class="card" style="cursor:pointer;border-left:4px solid ${accent(a)}" data-symbol="${SYM}" role="button" tabindex="0" aria-label="Open ${SYM} signal detail" title="Open ${SYM} Signal + POC detail">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
        <h3 style="font-size:13px;color:var(--text);margin:0">${SYM}</h3>
        ${signalChip}
      </div>
      <div class="v" style="font-size:26px;font-weight:700;margin-top:6px;color:var(--text)">${fmtPrice(lastP)}</div>
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px">
        <span style="font-size:13px;color:${pctColor};font-weight:600">${pctTxt}</span>
        <span class="sub" style="font-size:12px;color:var(--muted)">24h vol ${fmtVol(lastV)}</span>
      </div>
      ${sparkBlock}
      <div class="sub" style="font-size:11px;color:var(--muted);margin-top:6px">${ASSET_NAMES[a] || a} &middot; as of ${asOf}</div>
    </div>`;
  }).join('');

  // Wire click → universal Signal + POC modal. openSignalDetail delegates
  // through lookupSymbol so the same modal that Top-25 cards open also
  // opens here.
  host.querySelectorAll('[data-symbol]').forEach(el =>
    el.addEventListener('click', () => openSignalDetail(el.getAttribute('data-symbol')))
  );
  host.querySelectorAll('[data-symbol]').forEach(el =>
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' '){
        e.preventDefault();
        openSignalDetail(el.getAttribute('data-symbol'));
      }
    })
  );
}

// Configure signal cards modal — checkbox list of supported assets,
// saved to localStorage on Save, instantly re-renders the card row.
function openConfigSignals(){
  const modal = document.getElementById('configSignalsModal');
  const list  = document.getElementById('configSignalsList');
  const status = document.getElementById('configSignalsStatus');
  const ASSET_NAMES = {btc:'Bitcoin', eth:'Ethereum', link:'Chainlink', ltc:'Litecoin'};
  const current = new Set(getSignalOrder());
  list.innerHTML = SIGNAL_SUPPORTED.map(a =>
    `<label style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:#0e1118;border:1px solid var(--border);border-radius:6px;cursor:pointer">
      <input type="checkbox" data-asset="${a}" ${current.has(a) ? 'checked' : ''} style="cursor:pointer">
      <strong style="min-width:40px">${a.toUpperCase()}</strong>
      <span class="sub" style="color:var(--muted)">${ASSET_NAMES[a] || a}</span>
    </label>`
  ).join('');
  status.textContent = '';
  modal.classList.remove('hidden');
}
document.getElementById('configSignalsBtn')?.addEventListener('click', openConfigSignals);
document.getElementById('configSignalsClose')?.addEventListener('click', () =>
  document.getElementById('configSignalsModal').classList.add('hidden'));
document.getElementById('configSignalsModal')?.addEventListener('click', e => {
  if (e.target.id === 'configSignalsModal') e.target.classList.add('hidden');
});
document.getElementById('configSignalsSave')?.addEventListener('click', () => {
  const checked = Array.from(document.querySelectorAll('#configSignalsList input[type=checkbox]:checked'))
    .map(i => i.dataset.asset);
  if (!checked.length) {
    document.getElementById('configSignalsStatus').textContent = 'Pick at least one asset.';
    return;
  }
  try {
    localStorage.setItem('overviewSignals', JSON.stringify(checked));
  } catch(_) {}
  document.getElementById('configSignalsStatus').textContent = 'Saved.';
  renderOverviewSignals();
  setTimeout(() => document.getElementById('configSignalsModal').classList.add('hidden'), 600);
});
document.getElementById('configSignalsReset')?.addEventListener('click', () => {
  try { localStorage.removeItem('overviewSignals'); } catch(_) {}
  document.getElementById('configSignalsStatus').textContent = 'Reset to default.';
  openConfigSignals();  // re-render checkboxes
  renderOverviewSignals();
});

function renderOverviewMacro(){
  const fred = (DATA.market || {}).fred || {};
  destroy('overviewMacro');
  if (!fred.available){
    const ctx = document.getElementById('overviewMacroChart');
    if (ctx && ctx.getContext){
      const c = ctx.getContext('2d');
      c.clearRect(0, 0, ctx.width, ctx.height);
      c.fillStyle = '#8a93a6';
      c.font = '13px -apple-system';
      c.textAlign = 'center';
      c.fillText('Macro overlay disabled — set FRED_API_KEY', ctx.width/2, ctx.height/2);
    }
    return;
  }
  // Use 3-month window for the overview
  const cutoff = new Date(); cutoff.setDate(cutoff.getDate() - 90);
  const cutoffStr = cutoff.toISOString().slice(0,10);
  const filter = (arr) => (arr || []).filter(r => r.date >= cutoffStr);
  const btcPrice = filter(((DATA.market || {}).btc || {}).price);
  const dxy = filter(fred.dxy);
  const sp = filter(fred.sp500);
  const gold = filter(fred.gold);
  const ty10 = filter(fred.treasury_10y);

  // Build unified date axis
  const allDates = new Set();
  [btcPrice, dxy, sp, gold, ty10].forEach(s => s.forEach(p => allDates.add(p.date)));
  const labels = Array.from(allDates).sort();

  const align = (arr) => {
    const map = new Map(arr.map(p => [p.date, p.value]));
    let lastSeen = null;
    return labels.map(d => { if (map.has(d)) lastSeen = map.get(d); return lastSeen; });
  };
  const normalize = (arr) => {
    const first = arr.find(v => v != null);
    return first ? arr.map(v => v == null ? null : (v / first) * 100) : arr;
  };
  const series = [
    {label:'BTC',   data: normalize(align(btcPrice.map(p=>({date:p.date,value:p.value})))), color:'#f7931a'},
    {label:'DXY',   data: normalize(align(dxy)),  color:'#22c55e'},
    {label:'S&P',   data: normalize(align(sp)),   color:'#a78bfa'},
    {label:'Gold',  data: normalize(align(gold)), color:'#fbbf24'},
    {label:'10Y',   data: normalize(align(ty10)), color:'#06b6d4'},
  ].filter(s => s.data.some(v => v != null));

  charts.overviewMacro = new Chart(document.getElementById('overviewMacroChart'), {
    type:'line',
    data:{labels, datasets: series.map(s => ({
      label: s.label, data: s.data,
      borderColor: s.color, backgroundColor: 'transparent',
      pointRadius: 0, borderWidth: 1.6, tension: 0.2, spanGaps: true,
    }))},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{
        legend:{labels:{color:'#e6e8ee', font:{size:10}, boxWidth:14}},
        tooltip:{mode:'index', intersect:false, callbacks:{label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y?.toFixed(1)}`}},
      },
      scales:{
        x:{ticks:{color:'#8a93a6', maxTicksLimit:6}, grid:{color:'#1f2533'}},
        y:{title:{display:true, text:'Index (start = 100)', color:'#8a93a6', font:{size:10}}, ticks:{color:'#8a93a6'}, grid:{color:'#1f2533'}},
      },
    },
  });
}

// Compact one-liner index row — fits the thin bar at top of Overview.
// Layout per index: SYMBOL  VALUE  +X.YZ% (1d), with a tiny sparkline at
// the right. No 5d/30d (kept the most actionable signal, drop the rest).
function renderOverviewIndices(){
  const y = ((DATA.market || {}).yahoo_indices) || {};
  const items = [
    {key:'dow',     short:'DOW'},
    {key:'sp500',   short:'S&P'},
    {key:'nasdaq',  short:'NDX'},
    {key:'vix',     short:'VIX'},
  ];
  const host = document.getElementById('overviewIndices');
  if (!host) return;
  host.innerHTML = items.map(i => {
    const v = y[i.key];
    if (!v) return `<div style="display:flex;align-items:center;gap:8px;padding:10px 14px;background:#10151f;border:1px solid var(--border);border-radius:6px"><span style="font-size:12px;color:var(--muted);font-weight:700;letter-spacing:.06em">${i.short}</span><span style="font-size:14px;color:var(--muted)">—</span></div>`;
    const ch = v.change_1d_pct || 0;
    const cls = ch >= 0 ? 'green' : 'red';
    const pct = (ch >= 0 ? '+' : '') + ch.toFixed(2) + '%';
    // Bigger sparkline — 110×32 SVG (was 56×16)
    const spark = renderSparkline(v.sparkline_90d || [], ch >= 0, 110, 32);
    return `<div style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:#10151f;border:1px solid var(--border);border-radius:6px;font-variant-numeric:tabular-nums">
      <div style="display:flex;flex-direction:column;min-width:0;gap:2px">
        <span style="font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.06em">${i.short}</span>
        <span style="font-size:20px;font-weight:700;line-height:1">${(v.latest||0).toLocaleString(undefined,{maximumFractionDigits:0})}</span>
        <span class="${cls}" style="font-size:12px;font-weight:600">${pct}</span>
      </div>
      <span style="margin-left:auto;line-height:0">${spark}</span>
    </div>`;
  }).join('');
}

function renderOverviewNews(){
  const news = ((DATA.market || {}).news) || [];
  const host = document.getElementById('overviewNews');
  if (host){
    if (!news.length){
      host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No data available.</div>';
    } else {
      host.innerHTML = news.slice(0,3).map(n =>
        `<a href="${sanitizeUrl(n.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="display:block;padding:10px 12px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text)">
          <div style="font-size:11px;color:var(--muted);margin-bottom:2px">
            <span style="color:#a78bfa;font-weight:600">${escapeHtml(n.source||'')}</span> · ${escapeHtml(n.date||'')}
          </div>
          <div style="font-size:13px;line-height:1.35">${escapeHtml(n.title||'')}</div>
        </a>`
      ).join('');
    }
  }
  // Bottom-of-Overview "More headlines" feed. Picks up where the top
  // 3-item teaser left off so the user doesn't read the same titles
  // twice on a single page. If we have fewer than 13 total items we
  // simply render whatever is past index 3.
  const bottom = document.getElementById('overviewNewsHost');
  if (bottom){
    const more = news.slice(3, 13);
    if (!more.length){
      bottom.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No additional headlines beyond the top 4 above.</div>';
      return;
    }
    bottom.innerHTML = more.map(n =>
      `<a href="${sanitizeUrl(n.url)}" target="_blank" rel="noopener" style="display:block;padding:8px 10px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text)">
        <div style="font-weight:600;font-size:13px">${escapeHtml(n.title)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">${escapeHtml(n.source)} · ${escapeHtml(n.date)}</div>
      </a>`
    ).join('');
  }
}

function renderOverviewInsights(){
  const all = DATA.insights || [];
  const host = document.getElementById('overviewInsights');
  if (!host) return;
  if (!all.length){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No notable insights right now</div>';
    return;
  }
  host.innerHTML = all.slice(0,3).map(i => {
    const c = severityColor(i.severity);
    const ic = severityIcon(i.severity, i.kind);
    const detail = i.detail ? `<div class="sub" style="font-size:10px;color:var(--muted);margin-top:2px">${escapeHtml(i.detail)}</div>` : '';
    return `<div style="display:flex;align-items:flex-start;gap:8px;padding:8px 12px;background:#10151f;border:1px solid var(--border);border-left:3px solid ${c};border-radius:6px">
      <span style="font-size:13px">${ic}</span>
      <div style="flex:1;line-height:1.3">
        <div style="font-size:12px">${escapeHtml(i.headline)}</div>
        ${detail}
      </div>
    </div>`;
  }).join('');
}

// AI-exposed stocks card for the Crypto Overview tab. Same subset + same
// card layout as the AI News tab's #aiStocksGrid, just painted into a
// second host element. Tickers from AI_EXPOSED_TICKERS, sorted by score.
function renderOverviewAiStocks(){
  const host = document.getElementById('overviewAiStocksGrid');
  if (!host) return;
  const allStocks = ((DATA.market||{}).stocks_signals) || [];
  const set = new Set(AI_EXPOSED_TICKERS);
  const subset = (Array.isArray(allStocks) ? allStocks : [])
    .filter(s => s && s.symbol && set.has(String(s.symbol).toUpperCase()))
    .sort((a,b) => (Number(b.score)||0) - (Number(a.score)||0));
  if (!subset.length){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px;grid-column:1/-1">No AI-exposed tickers in current stocks_signals.</div>';
    return;
  }
  host.innerHTML = subset.map(s => {
    const score = Number(s.score) || 0;
    const color = score >= 20 ? '#22c55e' : (score <= -20 ? '#ef4444' : '#f59e0b');
    const chPct = Number(s.change_pct);
    const chColor = isFinite(chPct) ? (chPct >= 0 ? '#22c55e' : '#ef4444') : 'var(--muted)';
    const chTxt  = isFinite(chPct) ? ((chPct >= 0 ? '+' : '') + chPct.toFixed(2) + '%') : '—';
    const price  = Number(s.last_price);
    const priceTxt = isFinite(price)
      ? ('$' + price.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}))
      : '—';
    const scoreTxt = (score >= 0 ? '+' : '') + (Number.isInteger(score) ? score : score.toFixed(1));
    const clamped = Math.max(-100, Math.min(100, score));
    const pct = ((clamped + 100) / 200) * 100;
    const symbol = escapeHtml(String(s.symbol || ''));
    return `<div class="chart-card" style="padding:10px 12px">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px">
        <div style="min-width:0;display:flex;align-items:baseline;gap:6px">
          <div style="font-size:13px;font-weight:700;letter-spacing:0.3px">${symbol}</div>
          <div class="sub" style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:140px">${escapeHtml(String(s.name || ''))}</div>
        </div>
        <div style="text-align:right;line-height:1">
          <div style="font-size:16px;font-weight:700;color:${color}">${scoreTxt}</div>
          <div style="font-size:9px;color:${color};font-weight:600;margin-top:1px">${escapeHtml(String(s.label || ''))}</div>
        </div>
      </div>
      <div style="height:6px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:3px;position:relative;margin:4px 0 5px">
        <div style="position:absolute;top:-3px;left:calc(${pct.toFixed(1)}% - 4px);width:8px;height:12px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px">
        <div style="font-size:13px;font-weight:600;font-variant-numeric:tabular-nums">${priceTxt}</div>
        <div style="font-size:12px;font-weight:600;color:${chColor};font-variant-numeric:tabular-nums">${chTxt}</div>
      </div>
    </div>`;
  }).join('');
}

// Wire click-to-jump on the macro card and news card
document.addEventListener('click', (e) => {
  const card = e.target.closest('[data-jump]');
  if (card && !e.target.closest('a')) selectTab(card.dataset.jump);
});

// ---------- Research tab (one-stop social + dev + on-chain + POC) ----------
function socialData(){ return (DATA.market||{}).social || {}; }
const RESEARCH_ASSETS = ['btc','eth','link','ltc'];
const RESEARCH_ACCENT = a => ({btc:'#f7931a', eth:'#627eea', link:'#2a5ada', ltc:'#bfbbbb'})[a] || '#a78bfa';
const ASSET_FULLNAME = {btc:'Bitcoin', eth:'Ethereum', link:'Chainlink', ltc:'Litecoin'};
const fmtNumShort = n => n == null ? '—' :
  (n >= 1e9 ? (n/1e9).toFixed(2) + 'B' :
   n >= 1e6 ? (n/1e6).toFixed(1) + 'M' :
   n >= 1e3 ? (n/1e3).toFixed(1) + 'K' : String(Math.round(n)));
const fmtUsdShort = p => p == null ? '—' :
  '$' + (p >= 1000 ? Math.round(p).toLocaleString() :
         p >= 1    ? p.toFixed(2) :
         p >= 0.01 ? p.toFixed(4) : p.toFixed(6));

// ===== Point of Control =====
// Multi-timeframe POC ladder. Each card shows 4 timeframes' POCs in a
// compact table — clustering across timeframes signals high-conviction
// levels. Inline SVG volume profile histogram visualizes distribution shape.
const POC_TFS = [['d30','30d'],['d90','90d'],['d180','180d'],['d365','365d']];

// "Clustered" = 3+ of the 4 POCs land within 2% of each other.
function pocClustered(rows){
  const pocs = (rows||[]).filter(r => r && r.poc).map(r => r.poc);
  if (pocs.length < 3) return false;
  return pocs.some(ref => pocs.filter(p => Math.abs(p-ref)/ref*100 <= 2).length >= 3);
}

// Horizontal volume profile SVG. Primary = 90d filled histogram; 30d shown
// as a dashed overlay for divergence-at-a-glance. POC bin highlighted
// orange, VA band shaded blue, current price drawn as a green line
// (dashed if outside the binned range).
// Larger labeled volume-profile chart used in the POC detail modal. Uses
// a viewBox so it scales to its container width. Renders POC/VAH/VAL
// price labels on the right, a current-price marker with $value, and
// a small legend at the bottom.
function volumeProfileSVGLarge(primary, alt, current){
  // padR bumped to 96 to give bigger right-edge labels room. Without it,
  // "POC $1.33" and "NOW $1.09" used to clip the SVG edge at modal width.
  const W = 480, H = 360, padL = 8, padR = 96, padT = 18, padB = 26;
  if (!primary || !primary.buckets || !primary.buckets.length){
    return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:auto;max-height:420px;display:block;border-radius:6px;background:#0b0d12">
      <text x="${W/2}" y="${H/2}" text-anchor="middle" font-size="14" fill="#888">no profile</text>
    </svg>`;
  }
  const bks = primary.buckets;
  const maxV = Math.max(...bks.map(b => b.volume)) || 1;
  const prices = bks.map(b => b.price);
  const pMin = prices[0] - primary.step / 2;
  const pMax = prices[prices.length - 1] + primary.step / 2;
  const plotH = H - padT - padB;
  const plotW = W - padL - padR;
  const barH  = plotH / bks.length;
  const yFor  = i => padT + (bks.length - 1 - i) * barH;
  const yForPrice = p => padT + ((pMax - p) / (pMax - pMin)) * plotH;
  const bars = bks.map((b, i) => {
    const w = (b.volume / maxV) * plotW;
    const isPoc = Math.abs(b.price - primary.poc) < primary.step / 2 + 1e-6;
    const inVA  = b.price >= primary.val && b.price <= primary.vah;
    const fill  = isPoc ? '#ff6b35' : (inVA ? '#4a90e2' : '#7aa7d9');
    const op    = isPoc ? 1 : (inVA ? 0.85 : 0.5);
    return `<rect x="${padL}" y="${yFor(i)}" width="${w}" height="${Math.max(1, barH-1)}" fill="${fill}" opacity="${op}"/>`;
  }).join('');
  const vaTop = yForPrice(primary.vah);
  const vaBot = yForPrice(primary.val);
  const vaBand = `<rect x="0" y="${vaTop}" width="${W}" height="${vaBot - vaTop}" fill="#4a90e2" opacity="0.10"/>`;
  // 30d overlay as dashed gray polyline
  let altLine = '';
  if (alt && alt.buckets && alt.buckets.length){
    const maxA = Math.max(...alt.buckets.map(b => b.volume)) || 1;
    const pts = alt.buckets.map(b => {
      const y = yForPrice(b.price);
      const x = padL + (b.volume / maxA) * plotW;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    altLine = `<polyline points="${pts}" fill="none" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="3,2" opacity="0.75"/>`;
  }
  // Price labels on the right: POC / VAH / VAL — and the current price marker.
  // Font sizes bumped (was 10/11) so labels are legible at modal scale; VAH/VAL
  // are suppressed when they collide with POC (common when POC sits at the top
  // or bottom of the value area — same y, labels would stack illegibly). The
  // ladder table below the chart already lists the exact VAH/VAL/POC values,
  // so dropping a colliding tag costs nothing.
  const yPoc = yForPrice(primary.poc);
  const yVah = yForPrice(primary.vah);
  const yVal = yForPrice(primary.val);
  const labelX = W - padR + 6;
  const COLLISION = 14;  // SVG units — must exceed VAH/VAL font-size to fully hide overlap
  const pocLine = `<line x1="0" y1="${yPoc}" x2="${W - padR}" y2="${yPoc}" stroke="#ff6b35" stroke-width="1" opacity="0.5"/>`;
  const vahLabel = Math.abs(yVah - yPoc) < COLLISION
    ? ''
    : `<text x="${labelX}" y="${yVah + 5}" font-size="13" fill="#7aa7d9">VAH ${fmtUsdShort(primary.vah)}</text>`;
  const valLabel = Math.abs(yVal - yPoc) < COLLISION
    ? ''
    : `<text x="${labelX}" y="${yVal + 5}" font-size="13" fill="#7aa7d9">VAL ${fmtUsdShort(primary.val)}</text>`;
  const labels = `
    <text x="${labelX}" y="${yPoc + 5}" font-size="14" fill="#ff6b35" font-weight="700">POC ${fmtUsdShort(primary.poc)}</text>
    ${vahLabel}
    ${valLabel}`;
  let curMarker = '';
  if (current != null){
    const clamped = Math.min(Math.max(current, pMin), pMax);
    const yC = yForPrice(clamped);
    const dash = (current < pMin || current > pMax) ? 'stroke-dasharray="3,2"' : '';
    // NOW also dodges POC — if the current price is sitting right on the POC
    // (within COLLISION), skip the label; the green line still marks it and
    // the header price chip already shows the value.
    const nowLabel = Math.abs(yC - yPoc) < COLLISION
      ? ''
      : `<text x="${labelX}" y="${yC + 5}" font-size="14" fill="#00c853" font-weight="700">NOW ${fmtUsdShort(current)}</text>`;
    curMarker = `<line x1="0" y1="${yC}" x2="${W - padR}" y2="${yC}" stroke="#00c853" stroke-width="1.5" ${dash}/>
      ${nowLabel}`;
  }
  // Bottom legend — font bumped 9→11 to match the bigger label scale.
  const legendY = H - 8;
  const legend = `
    <g font-size="11" fill="#94a3b8">
      <rect x="${padL}" y="${legendY - 10}" width="12" height="10" fill="#ff6b35"/>
      <text x="${padL + 16}" y="${legendY - 1}">POC</text>
      <rect x="${padL + 52}" y="${legendY - 10}" width="12" height="10" fill="#4a90e2" opacity="0.85"/>
      <text x="${padL + 68}" y="${legendY - 1}">Value Area (70% vol)</text>
      <line x1="${padL + 196}" y1="${legendY - 5}" x2="${padL + 218}" y2="${legendY - 5}" stroke="#cbd5e1" stroke-width="1.5" stroke-dasharray="3,2"/>
      <text x="${padL + 222}" y="${legendY - 1}">30d overlay</text>
      <line x1="${padL + 296}" y1="${legendY - 5}" x2="${padL + 318}" y2="${legendY - 5}" stroke="#00c853" stroke-width="1.5"/>
      <text x="${padL + 322}" y="${legendY - 1}">Current price</text>
    </g>`;
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" style="width:100%;height:auto;max-height:420px;display:block;border-radius:6px;background:#0b0d12">
    ${vaBand}${bars}${altLine}${pocLine}${curMarker}${labels}${legend}
  </svg>`;
}

function volumeProfileSVG(primary, alt, current){
  const W = 120, H = 140, padL = 4, padR = 4;
  if (!primary || !primary.buckets || !primary.buckets.length){
    return `<svg width="${W}" height="${H}"><text x="${W/2}" y="${H/2}" text-anchor="middle" font-size="10" fill="#888">no profile</text></svg>`;
  }
  const bks = primary.buckets;
  const maxV = Math.max(...bks.map(b => b.volume)) || 1;
  const prices = bks.map(b => b.price);
  const pMin = prices[0] - primary.step / 2;
  const pMax = prices[prices.length - 1] + primary.step / 2;
  const barH = (H - 4) / bks.length;
  const barW = W - padL - padR;
  const yFor = i => 2 + (bks.length - 1 - i) * barH;
  const bars = bks.map((b, i) => {
    const w = (b.volume / maxV) * barW;
    const isPoc = Math.abs(b.price - primary.poc) < primary.step / 2 + 1e-6;
    const inVA  = b.price >= primary.val && b.price <= primary.vah;
    const fill  = isPoc ? '#ff6b35' : (inVA ? '#4a90e2' : '#7aa7d9');
    const op    = isPoc ? 1 : (inVA ? 0.85 : 0.45);
    return `<rect x="${padL}" y="${yFor(i)}" width="${w}" height="${Math.max(1, barH-1)}" fill="${fill}" opacity="${op}"/>`;
  }).join('');
  const vaTop = 2 + ((pMax - primary.vah) / (pMax - pMin)) * (H - 4);
  const vaBot = 2 + ((pMax - primary.val) / (pMax - pMin)) * (H - 4);
  const vaBand = `<rect x="0" y="${vaTop}" width="${W}" height="${vaBot - vaTop}" fill="#4a90e2" opacity="0.08"/>`;
  let altLine = '';
  if (alt && alt.buckets && alt.buckets.length){
    const maxA = Math.max(...alt.buckets.map(b => b.volume)) || 1;
    const pts = alt.buckets.map(b => {
      const y = 2 + ((pMax - b.price) / (pMax - pMin)) * (H - 4);
      const x = padL + (b.volume / maxA) * barW;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    altLine = `<polyline points="${pts}" fill="none" stroke="#888" stroke-width="1" stroke-dasharray="2,2" opacity="0.7"/>`;
  }
  let curMarker = '';
  if (current != null){
    const clamped = Math.min(Math.max(current, pMin), pMax);
    const yC = 2 + ((pMax - clamped) / (pMax - pMin)) * (H - 4);
    const dash = (current < pMin || current > pMax) ? 'stroke-dasharray="3,2"' : '';
    curMarker = `<line x1="0" y1="${yC}" x2="${W}" y2="${yC}" stroke="#00c853" stroke-width="1.5" ${dash}/>`;
  }
  return `<svg width="${W}" height="${H}">${vaBand}${bars}${altLine}${curMarker}</svg>`;
}

// Tiny inline SVG sparkline of the last 7 days of signal score for a
// signal card. Stroke color reflects first→last direction (green up,
// red down, muted flat). Returns empty string when the series is too
// short to draw a meaningful line.
function signalScoreSparkline(history){
  if (!Array.isArray(history) || history.length < 2) return '';
  const slice = history.slice(-7);
  const values = slice
    .map(p => (p && typeof p.score === 'number') ? p.score : null)
    .filter(v => v != null && isFinite(v));
  if (values.length < 2) return '';
  const lo = Math.min(...values), hi = Math.max(...values);
  const range = (hi - lo) || 1;
  const n = values.length;
  const w = 100, h = 30, padTop = 10, padBot = 2;
  const pts = values.map((v, i) => {
    const x = (i / (n - 1)) * w;
    const y = padTop + (1 - (v - lo) / range) * (h - padTop - padBot);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  const first = values[0], last = values[values.length - 1];
  // "Flat" = within 1% of the first value (use absolute fallback when
  // |first| is tiny so the relative test stays meaningful at score ~0).
  const flatTol = Math.max(Math.abs(first) * 0.01, 0.5);
  const trend = Math.abs(last - first) <= flatTol
    ? '#94a3b8'
    : (last >= first ? '#22c55e' : '#ef4444');
  const lastTxt = (last >= 0 ? '+' : '') + (Number.isInteger(last) ? last : last.toFixed(1));
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:30px;display:block;margin-top:6px;border-radius:3px;background:#0b0d12">
    <text x="2" y="7" font-size="6" fill="#64748b">7d score</text>
    <text x="${w-2}" y="7" font-size="6" fill="#64748b" text-anchor="end">${lastTxt}</text>
    <polyline points="${pts}" fill="none" stroke="${trend}" stroke-width="1.2" vector-effect="non-scaling-stroke" />
  </svg>`;
}

// ============ STOCKS TAB RENDERER ============
// Renders DATA.market.stocks_signals (top-20 most-active US stocks scored
// across SMA / RSI / MACD / momentum / volume) as a grid of cards sorted
// Strong Buy -> Strong Sell. Fetcher lives in fetch_market.py.
// Map a stock label to a filter-chip bucket. Label values from
// fetch_market._label_from_score: STRONG BUY / BUY / HOLD / SELL / STRONG SELL.
function stockLabelBucket(label){
  const L = (label||'').toUpperCase().trim();
  if (L === 'STRONG BUY')  return 'strong_buy';
  if (L === 'BUY')         return 'buy';
  if (L === 'STRONG SELL') return 'strong_sell';
  if (L === 'SELL')        return 'sell';
  return 'hold';
}
// Compact volume formatter: 124000000 -> "124M", 2_500_000_000 -> "2.5B".
function fmtVolumeCompact(v){
  const n = Number(v);
  if (!isFinite(n) || n <= 0) return '—';
  if (n >= 1e12) return (n/1e12).toFixed(n>=1e13?0:1).replace(/\.0$/,'') + 'T';
  if (n >= 1e9)  return (n/1e9 ).toFixed(n>=1e10?0:1).replace(/\.0$/,'') + 'B';
  if (n >= 1e6)  return (n/1e6 ).toFixed(n>=1e7 ?0:1).replace(/\.0$/,'') + 'M';
  if (n >= 1e3)  return (n/1e3 ).toFixed(n>=1e4 ?0:1).replace(/\.0$/,'') + 'K';
  return fmtNum(n, 0);
}
// STOCK SIGNAL SENTIMENT — aggregate signal-score buckets across the top-50
// most-active stocks (DATA.market.stocks_signals). Mirrors the POC sentiment
// card pattern. Net index = ((BUY+STRONG_BUY) - (SELL+STRONG_SELL)) / total
// × 100, clamped to [-100,+100]. Labels: STRONG ACCUMULATION / ACCUMULATION /
// NEUTRAL / DISTRIBUTION / STRONG DISTRIBUTION (mirroring POC).
function renderStocksSentiment(){
  const card = document.getElementById('stocksSentimentCard');
  if (!card) return;
  const list = ((DATA.market||{}).stocks_signals) || [];
  if (!Array.isArray(list) || list.length === 0){
    card.style.display = 'none';
    return;
  }
  card.style.display = '';
  let strongBuy = 0, buy = 0, hold = 0, sell = 0, strongSell = 0;
  for (const s of list){
    const score = Number(s && s.score);
    if (!isFinite(score)) continue;
    if      (score >=  50) strongBuy++;
    else if (score >=  20) buy++;
    else if (score >  -20) hold++;
    else if (score >  -50) sell++;
    else                   strongSell++;
  }
  const buyTotal  = strongBuy + buy;
  const sellTotal = strongSell + sell;
  const total = Math.max(buyTotal + hold + sellTotal, 1);
  const net = Math.round(((buyTotal - sellTotal) / total) * 100);
  const label = net >=  50 ? 'STRONG ACCUMULATION'
              : net >=  20 ? 'ACCUMULATION'
              : net >  -20 ? 'NEUTRAL'
              : net >  -50 ? 'DISTRIBUTION'
              :              'STRONG DISTRIBUTION';
  const color = net >=  20 ? '#22c55e'
              : net <= -20 ? '#ef4444'
              :              '#f59e0b';
  const scoreEl   = document.getElementById('stocksSentimentScore');
  const labelEl   = document.getElementById('stocksSentimentLabel');
  const sublineEl = document.getElementById('stocksSentimentSubline');
  if (scoreEl){
    scoreEl.textContent = (net >= 0 ? '+' : '') + net;
    scoreEl.style.color = color;
  }
  if (labelEl){
    labelEl.textContent = label;
    labelEl.style.color = color;
  }
  if (sublineEl){
    sublineEl.textContent = `${total} stocks · positive = broad buy · negative = broad sell`;
  }
  const pctBuy  = (buyTotal  / total) * 100;
  const pctHold = (hold      / total) * 100;
  const pctSell = (sellTotal / total) * 100;
  const buyBar  = document.getElementById('stocksSentimentBarBuy');
  const holdBar = document.getElementById('stocksSentimentBarHold');
  const sellBar = document.getElementById('stocksSentimentBarSell');
  if (buyBar)  buyBar.style.width  = pctBuy.toFixed(1)  + '%';
  if (holdBar) holdBar.style.width = pctHold.toFixed(1) + '%';
  if (sellBar) sellBar.style.width = pctSell.toFixed(1) + '%';
  const buyCount  = document.getElementById('stocksSentimentBuyCount');
  const holdCount = document.getElementById('stocksSentimentHoldCount');
  const sellCount = document.getElementById('stocksSentimentSellCount');
  if (buyCount)  buyCount.textContent  = String(buyTotal);
  if (holdCount) holdCount.textContent = String(hold);
  if (sellCount) sellCount.textContent = String(sellTotal);
}

function renderStocksTab(){
  const grid = document.getElementById('stocksGrid');
  if (!grid) return;
  const rows = ((DATA.market||{}).stocks_signals) || [];
  // LTHCS Insights row + Composite Index panel — pinned at the very top
  // of the Stocks tab as the canonical equity-conviction read across the
  // universe. Same visual model as the Whale Sentiment Index. Mirrors the
  // LTHCS tab. Insights row hosts the corner CTA so the intro card is gone.
  renderLthcsInsightsRow(document.getElementById('stocksLthcsInsightsRow'));
  renderLthcsNarrativePanel(document.getElementById('stocksLthcsCompositeCard'));
  // Traditional indices bar (DOW / S&P / NDX / VIX) moved here from the
  // Crypto tab — macro equity context belongs alongside the equity-signal grid.
  renderOverviewIndices();
  // Sentiment card at the very top of the tab (mirrors POC pattern).
  renderStocksSentiment();
  // Always (re)render the breadth chart first so it appears whether or not
  // there are scoreable rows. computeSignalBreadth/renderBreadthChart both
  // handle empty input gracefully with a "No data available." message.
  renderBreadthChart(
    'stocksBreadthChart',
    computeSignalBreadth(Array.isArray(rows) ? rows : [], 90),
    null
  );
  if (!Array.isArray(rows) || rows.length === 0){
    grid.innerHTML = '<div class="empty">Stock signals not yet loaded &mdash; run python app.py --fetch-market</div>';
    return;
  }
  const sorted = rows.slice().sort((a, b) => (Number(b.score)||0) - (Number(a.score)||0));
  // Group rows by bucket so we can render section headers above each grid.
  const byBucket = {strong_buy:[], buy:[], hold:[], sell:[], strong_sell:[]};
  sorted.forEach(s => {
    const b = stockLabelBucket(s.label);
    if (byBucket[b]) byBucket[b].push(s);
    else byBucket.hold.push(s);
  });
  // Render a single compact stock card. Click anywhere opens the full modal.
  const cardHtml = s => {
    const score = Number(s.score) || 0;
    const color = score >= 20 ? '#22c55e' : (score <= -20 ? '#ef4444' : '#f59e0b');
    const chPct = Number(s.change_pct);
    const chColor = isFinite(chPct) ? (chPct >= 0 ? '#22c55e' : '#ef4444') : 'var(--muted)';
    const chTxt  = isFinite(chPct) ? ((chPct >= 0 ? '+' : '') + chPct.toFixed(2) + '%') : '—';
    const price  = Number(s.last_price);
    const priceTxt = isFinite(price)
      ? ('$' + price.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}))
      : '—';
    const scoreTxt = (score >= 0 ? '+' : '') + (Number.isInteger(score) ? score : score.toFixed(1));
    const clamped = Math.max(-100, Math.min(100, score));
    const pct = ((clamped + 100) / 200) * 100;
    const bucket = stockLabelBucket(s.label);
    const symbol = escapeHtml(String(s.symbol || ''));
    return `<div class="chart-card stock-card" data-stock-symbol="${symbol}" data-stock-bucket="${bucket}" role="button" tabindex="0" aria-label="Open full ${symbol} signal detail" title="Click for full breakdown" style="padding:10px 12px;cursor:pointer">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px">
        <div style="min-width:0;display:flex;align-items:baseline;gap:6px">
          <div style="font-size:13px;font-weight:700;letter-spacing:0.3px">${symbol}</div>
          <div class="sub" style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:140px">${escapeHtml(String(s.name || ''))}</div>
        </div>
        <div style="text-align:right;line-height:1">
          <div style="font-size:16px;font-weight:700;color:${color}">${scoreTxt}</div>
          <div style="font-size:9px;color:${color};font-weight:600;margin-top:1px">${escapeHtml(String(s.label || ''))}</div>
        </div>
      </div>
      <div style="height:6px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:3px;position:relative;margin:4px 0 5px">
        <div style="position:absolute;top:-2px;left:calc(${pct.toFixed(1)}% - 3px);width:6px;height:10px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
      </div>
      <div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:12px">
        <div style="font-weight:600">${priceTxt}</div>
        <div style="color:${chColor};font-weight:600">${chTxt}</div>
      </div>
    </div>`;
  };
  // Section metadata: glyph + display label + pill color per bucket.
  const sections = [
    {key:'strong_buy',  glyph:'🔥', label:'STRONG BUY',  color:'#16a34a'},
    {key:'buy',         glyph:'✓',  label:'BUY',         color:'#22c55e'},
    {key:'hold',        glyph:'◯',  label:'HOLD',        color:'#f59e0b'},
    {key:'sell',        glyph:'↓',  label:'SELL',        color:'#ef4444'},
    {key:'strong_sell', glyph:'⛔', label:'STRONG SELL', color:'#b91c1c'},
  ];
  const html = sections.map(sec => {
    const items = byBucket[sec.key];
    const n = items.length;
    const cards = items.map(cardHtml).join('');
    const empty = n === 0
      ? `<div class="sub" data-stock-bucket="${sec.key}" style="color:var(--muted);padding:10px 4px;font-size:12px">No stocks in this bucket.</div>`
      : '';
    return `<div class="stocks-section" data-stocks-section="${sec.key}" style="margin-bottom:14px">
      <div style="display:flex;align-items:center;gap:8px;margin:0 0 8px 0">
        <h3 style="margin:0;font-size:14px;font-weight:700;letter-spacing:0.2px;color:var(--text)">
          <span aria-hidden="true">${escapeHtml(sec.glyph)}</span>
          ${escapeHtml(sec.label)}
          <span style="color:var(--muted);font-weight:500;margin-left:4px">(${n})</span>
        </h3>
        <span class="tag" style="background:${sec.color}22;color:${sec.color};border:1px solid ${sec.color}66;padding:1px 8px;border-radius:10px;font-size:10px;font-weight:700">${escapeHtml(sec.label)}</span>
      </div>
      <div class="row stocks-section-grid" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px">${cards}${empty}</div>
    </div>`;
  }).join('');
  grid.innerHTML = html;
  applyStocksFilter();
}

// ===== AI NEWS TAB =====
// Renders the AI News tab: aggregate sentiment summary, live article feed,
// AI-exposed stock signal subset, and source-level positive/negative breakdown.
// Reads DATA.market.ai_news (produced by fetch_market.py). Defensive: when
// available=false or missing, shows an empty state pointing to the fetcher.
const AI_EXPOSED_TICKERS = ['NVDA','GOOGL','MSFT','META','AMZN','AAPL','TSLA','AMD','INTC','ORCL','CRM','PLTR','SMCI','ARM','AVGO'];
// Neutral is intentionally a muted grey, not amber. Amber means "caution"
// per the palette spec; painting a neutral headline amber made it look
// like a warning. Grey reads as "no signal" which is the actual semantic.
const AI_SENT_COLOR = {POSITIVE:'#22c55e', NEGATIVE:'#ef4444', NEUTRAL:'#94a3b8'};

function renderAiNewsTab(){
  const ai = ((DATA.market||{}).ai_news) || null;
  const empty = document.getElementById('aiNewsEmpty');
  const content = document.getElementById('aiNewsContent');
  if (!empty || !content) return;
  const ok = ai && ai.available && Array.isArray(ai.items);
  empty.classList.toggle('hidden', !!ok);
  content.classList.toggle('hidden', !ok);
  if (!ok) return;

  // --- Summary card -------------------------------------------------------
  const sum = ai.summary || {};
  const pos = Number(sum.positive)||0, neg = Number(sum.negative)||0, neu = Number(sum.neutral)||0;
  const tot = Number(sum.total) || (pos+neg+neu) || 1;
  const posPct = pos/tot*100, negPct = neg/tot*100, neuPct = neu/tot*100;
  const net = (sum.net_score == null) ? 0 : Number(sum.net_score);
  const netColor = net > 0 ? '#22c55e' : (net < 0 ? '#ef4444' : '#f59e0b');
  const netTxt = (net >= 0 ? '+' : '') + (Number.isInteger(net) ? net : net.toFixed(1));
  const label = sum.sentiment_label || '—';
  const summaryHost = document.getElementById('aiNewsSummary');
  if (summaryHost){
    summaryHost.innerHTML = `
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <div>
          <div style="font-size:42px;font-weight:700;color:${netColor};line-height:1">${escapeHtml(netTxt)}</div>
          <div style="font-size:14px;color:${netColor};font-weight:600;margin-top:3px">${escapeHtml(String(label))}</div>
          <div class="sub" style="font-size:11px;color:var(--muted);margin-top:2px">net score · ${tot} articles</div>
        </div>
        <div style="text-align:right;font-size:11px;color:var(--muted);min-width:140px">
          <div><span style="color:#22c55e;font-weight:600">${pos}</span> positive</div>
          <div><span style="color:#f59e0b;font-weight:600">${neu}</span> neutral</div>
          <div><span style="color:#ef4444;font-weight:600">${neg}</span> negative</div>
        </div>
      </div>
      <div style="display:flex;height:14px;margin-top:10px;border-radius:4px;overflow:hidden;background:#1f2533">
        <div style="background:#22c55e;width:${posPct.toFixed(2)}%" title="${pos} positive"></div>
        <div style="background:#f59e0b;width:${neuPct.toFixed(2)}%" title="${neu} neutral"></div>
        <div style="background:#ef4444;width:${negPct.toFixed(2)}%" title="${neg} negative"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:10px;color:var(--muted)">
        <span style="color:#22c55e">${posPct.toFixed(0)}% +</span>
        <span>${neuPct.toFixed(0)}% ◯</span>
        <span style="color:#ef4444">${negPct.toFixed(0)}% −</span>
      </div>`;
  }

  // --- Feed (top 30 most-recent) -----------------------------------------
  // Sort once; reuse for the top-5 inline panel + the full top-30 feed.
  const sortedItems = (ai.items||[]).slice().sort((a,b)=>{
    const da = a && a.date ? Date.parse(a.date) : 0;
    const db = b && b.date ? Date.parse(b.date) : 0;
    return (db||0) - (da||0);
  });
  const articleRow = (n) => {
    const sc = AI_SENT_COLOR[n.sentiment] || 'var(--muted)';
    const dot = `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${sc};vertical-align:middle;margin-right:6px;flex-shrink:0"></span>`;
    return `<a href="${sanitizeUrl(n.url)}" target="_blank" rel="noopener" style="display:block;padding:10px 12px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text);transition:background .1s" onmouseover="this.style.background='#10151f'" onmouseout="this.style.background=''">
      <div style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted);margin-bottom:3px">
        ${dot}<span style="color:#a78bfa;font-weight:600">${escapeHtml(n.source||'')}</span>
        <span>· ${escapeHtml(n.date||'')}</span>
        <span style="color:${sc};font-weight:600;margin-left:auto">${escapeHtml((n.sentiment||'').slice(0,3))}</span>
      </div>
      <div style="font-size:13px;line-height:1.35;margin-bottom:3px">${escapeHtml(n.title||'')}</div>
      ${n.body ? `<div class="sub" style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical">${escapeHtml(n.body)}</div>` : ''}
    </a>`;
  };
  const feed = document.getElementById('aiNewsFeed');
  if (feed){
    const items = sortedItems.slice(0, 30);
    feed.innerHTML = items.length
      ? items.map(articleRow).join('')
      : '<div class="sub" style="color:var(--muted);padding:14px">No articles yet.</div>';
  }
  // Top-3 inline panel — capped at 3 to match the Crypto Overview layout.
  const top5 = document.getElementById('aiNewsTop5');
  if (top5){
    const items = sortedItems.slice(0, 3);
    top5.innerHTML = items.length
      ? items.map(articleRow).join('')
      : '<div class="sub" style="color:var(--muted);padding:14px">No articles yet.</div>';
  }

  // --- AI-exposed stock signal subset ------------------------------------
  const aiGrid = document.getElementById('aiStocksGrid');
  if (aiGrid){
    const allStocks = ((DATA.market||{}).stocks_signals) || [];
    const set = new Set(AI_EXPOSED_TICKERS);
    const subset = (Array.isArray(allStocks) ? allStocks : [])
      .filter(s => s && s.symbol && set.has(String(s.symbol).toUpperCase()))
      .sort((a,b)=>(Number(b.score)||0)-(Number(a.score)||0));
    if (!subset.length){
      aiGrid.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px;grid-column:1/-1">No AI-exposed tickers in current stocks_signals.</div>';
    } else {
      aiGrid.innerHTML = subset.map(s => {
        const score = Number(s.score) || 0;
        const color = score >= 20 ? '#22c55e' : (score <= -20 ? '#ef4444' : '#f59e0b');
        const chPct = Number(s.change_pct);
        const chColor = isFinite(chPct) ? (chPct >= 0 ? '#22c55e' : '#ef4444') : 'var(--muted)';
        const chTxt  = isFinite(chPct) ? ((chPct >= 0 ? '+' : '') + chPct.toFixed(2) + '%') : '—';
        const price  = Number(s.last_price);
        const priceTxt = isFinite(price)
          ? ('$' + price.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}))
          : '—';
        const scoreTxt = (score >= 0 ? '+' : '') + (Number.isInteger(score) ? score : score.toFixed(1));
        const clamped = Math.max(-100, Math.min(100, score));
        const pct = ((clamped + 100) / 200) * 100;
        const symbol = escapeHtml(String(s.symbol || ''));
        return `<div class="chart-card" style="padding:10px 12px">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px">
            <div style="min-width:0;display:flex;align-items:baseline;gap:6px">
              <div style="font-size:13px;font-weight:700;letter-spacing:0.3px">${symbol}</div>
              <div class="sub" style="font-size:10px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:140px">${escapeHtml(String(s.name || ''))}</div>
            </div>
            <div style="text-align:right;line-height:1">
              <div style="font-size:16px;font-weight:700;color:${color}">${scoreTxt}</div>
              <div style="font-size:9px;color:${color};font-weight:600;margin-top:1px">${escapeHtml(String(s.label || ''))}</div>
            </div>
          </div>
          <div style="height:6px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:3px;position:relative;margin:4px 0 5px">
            <div style="position:absolute;top:-2px;left:calc(${pct.toFixed(1)}% - 3px);width:6px;height:10px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
          </div>
          <div style="display:flex;align-items:baseline;justify-content:space-between;gap:8px;font-size:12px">
            <div style="font-weight:600">${priceTxt}</div>
            <div style="color:${chColor};font-weight:600">${chTxt}</div>
          </div>
        </div>`;
      }).join('');
    }
  }

  // --- Source breakdown table --------------------------------------------
  const srcHost = document.getElementById('aiNewsSources');
  if (srcHost){
    const bySrc = new Map();
    (ai.items||[]).forEach(n => {
      const k = String(n.source || 'unknown');
      if (!bySrc.has(k)) bySrc.set(k, {positive:0, negative:0, neutral:0, total:0});
      const r = bySrc.get(k);
      r.total += 1;
      const s = String(n.sentiment||'').toUpperCase();
      if (s === 'POSITIVE') r.positive += 1;
      else if (s === 'NEGATIVE') r.negative += 1;
      else r.neutral += 1;
    });
    const rows = Array.from(bySrc.entries())
      .map(([src, r]) => ({src, ...r, net: r.positive - r.negative}))
      .sort((a,b)=> b.total - a.total || b.net - a.net);
    if (!rows.length){
      srcHost.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No source data available.</div>';
    } else {
      srcHost.innerHTML = `<table style="width:100%;font-size:12px;border-collapse:collapse">
        <thead><tr style="color:var(--muted);text-align:left;border-bottom:1px solid var(--border)">
          <th style="padding:6px 8px">Source</th>
          <th style="padding:6px 8px;text-align:right">Total</th>
          <th style="padding:6px 8px;text-align:right;color:#22c55e">+</th>
          <th style="padding:6px 8px;text-align:right">◯</th>
          <th style="padding:6px 8px;text-align:right;color:#ef4444">−</th>
          <th style="padding:6px 8px;text-align:right">Net</th>
        </tr></thead><tbody>
        ${rows.map(r => {
          const netColor = r.net > 0 ? '#22c55e' : (r.net < 0 ? '#ef4444' : 'var(--muted)');
          const netTxt = (r.net >= 0 ? '+' : '') + r.net;
          return `<tr style="border-bottom:1px solid var(--border)">
            <td style="padding:6px 8px;color:#a78bfa;font-weight:600">${escapeHtml(r.src)}</td>
            <td style="padding:6px 8px;text-align:right">${r.total}</td>
            <td style="padding:6px 8px;text-align:right;color:#22c55e">${r.positive}</td>
            <td style="padding:6px 8px;text-align:right">${r.neutral}</td>
            <td style="padding:6px 8px;text-align:right;color:#ef4444">${r.negative}</td>
            <td style="padding:6px 8px;text-align:right;color:${netColor};font-weight:600">${netTxt}</td>
          </tr>`;
        }).join('')}
        </tbody></table>`;
    }
  }

  // --- AI curated / investment add-ons -----------------------------------
  // The data agent injects DATA.market.ai_curated.{investment_kpis,
  // top_funded_companies, whitepaper_kpis} and DATA.market.ai_funding.
  // If ai_curated is missing entirely, hide the four new sections but keep
  // the original AI news UI working.
  const market = DATA.market || {};
  const curated = market.ai_curated || null;
  const funding = market.ai_funding || null;
  const hasCurated = !!curated && (
    (Array.isArray(curated.investment_kpis)   && curated.investment_kpis.length) ||
    (Array.isArray(curated.top_funded_companies) && curated.top_funded_companies.length) ||
    (Array.isArray(curated.whitepaper_kpis)   && curated.whitepaper_kpis.length)
  );
  ['aiInvestmentKpisCard','aiTopFundedCard','aiQuadrantCard','aiWhitepaperKpisCard']
    .forEach(id => { const el = document.getElementById(id); if (el) el.classList.toggle('hidden', !hasCurated); });

  // YC startup count badge in the existing "Latest AI news" header.
  const badge = document.getElementById('aiNewsHeaderBadge');
  if (badge){
    const yc = funding && Array.isArray(funding.yc_companies) ? funding.yc_companies.length : 0;
    const articles = Array.isArray(ai.items) ? ai.items.length : 0;
    badge.textContent = yc > 0
      ? (articles + ' articles · ' + yc + ' YC AI companies')
      : (articles + ' articles');
  }

  // SEC Form D card renders independently of the curated dataset — it's
  // driven by market.ai_funding.form_d_filings (live EDGAR data).
  renderAiSecFormD();

  if (hasCurated){
    renderAiInvestmentKpis();
    renderAiTopFunded();
    renderAiQuadrant();
    renderAiWhitepaperKpis();
  } else {
    // Make sure any old scatter chart is torn down on empty state.
    destroy('aiQuadrant');
  }
}

// ---- AI investment KPI strip ------------------------------------------
function renderAiInvestmentKpis(){
  const host = document.getElementById('aiInvestmentKpis');
  if (!host) return;
  const kpis = (((DATA.market||{}).ai_curated||{}).investment_kpis) || [];
  if (!Array.isArray(kpis) || !kpis.length){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px;grid-column:1/-1">No investment KPIs available.</div>';
    return;
  }
  host.innerHTML = kpis.map(k => {
    const label    = escapeHtml(String(k.label || k.name || ''));
    const value    = escapeHtml(String(k.value == null ? '—' : k.value));
    const prior    = k.prior_value == null ? '' : escapeHtml(String(k.prior_value));
    const deltaRaw = k.delta == null ? null : Number(k.delta);
    const deltaTxt = (k.delta == null) ? (k.delta_label ? escapeHtml(String(k.delta_label)) : '')
                   : (isFinite(deltaRaw) ? ((deltaRaw >= 0 ? '+' : '') + deltaRaw.toLocaleString(undefined,{maximumFractionDigits:2})) : escapeHtml(String(k.delta)));
    const deltaColor = (isFinite(deltaRaw) ? (deltaRaw >= 0 ? '#22c55e' : '#ef4444') : 'var(--muted)');
    const src       = escapeHtml(String(k.source || k.source_label || k.publisher || ''));
    const srcUrl    = sanitizeUrl(k.source_url || k.url, '');
    const unit      = escapeHtml(String(k.unit || ''));
    const inner = `
      <div style="display:flex;flex-direction:column;gap:6px;padding:12px 14px">
        <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em">${label}</div>
        <div style="font-size:24px;font-weight:700;line-height:1.1">${value}${unit ? ' <span style="font-size:13px;color:var(--muted);font-weight:600">'+unit+'</span>' : ''}</div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          ${deltaTxt ? '<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;color:'+deltaColor+';border:1px solid '+deltaColor+'">'+deltaTxt+'</span>' : ''}
          ${prior ? '<span class="sub" style="font-size:10px;color:var(--muted)">prior: '+prior+'</span>' : ''}
        </div>
        ${src ? '<div class="sub" style="font-size:10px;color:var(--muted);margin-top:2px">source: <span style="color:#a78bfa;font-weight:600">'+src+'</span></div>' : ''}
      </div>`;
    if (srcUrl){
      return '<a class="chart-card" href="'+srcUrl+'" target="_blank" rel="noopener" style="text-decoration:none;color:var(--text);display:block">'+inner+'</a>';
    }
    return '<div class="chart-card">'+inner+'</div>';
  }).join('');
}

// ---- Top funded AI companies table ------------------------------------
function renderAiTopFunded(){
  const tb = document.querySelector('#aiTopFundedTable tbody');
  if (!tb) return;
  const rows = (((DATA.market||{}).ai_curated||{}).top_funded_companies) || [];
  if (!Array.isArray(rows) || !rows.length){
    tb.innerHTML = '<tr><td colspan="6" style="padding:14px;color:var(--muted)">No company data.</td></tr>';
    return;
  }
  const sorted = rows.slice().sort((a,b) => (Number(b.valuation_usd)||0) - (Number(a.valuation_usd)||0));
  tb.innerHTML = sorted.map(c => {
    const name    = escapeHtml(String(c.name || c.company || ''));
    const val     = Number(c.valuation_usd);
    const round   = Number(c.last_round_size_usd);
    const valTxt  = isFinite(val)   ? fmtUSD(val, 'auto')   : '—';
    const rndTxt  = isFinite(round) ? fmtUSD(round, 'auto') : '—';
    const stage   = escapeHtml(String(c.stage || c.last_round_stage || ''));
    const hq      = escapeHtml(String(c.hq || c.headquarters || c.country || ''));
    const cat     = escapeHtml(String(c.category || c.sector || ''));
    const url     = sanitizeUrl(c.source_url || c.url, '');
    const nameCell = url
      ? '<a href="'+url+'" target="_blank" rel="noopener" style="color:#a78bfa;font-weight:600;text-decoration:none">'+name+'</a>'
      : '<span style="font-weight:600">'+name+'</span>';
    return `<tr>
      <td>${nameCell}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${valTxt}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${rndTxt}</td>
      <td>${stage}</td>
      <td>${hq}</td>
      <td>${cat}</td>
    </tr>`;
  }).join('');
}

// ---- SEC EDGAR Form D — recent AI private placements ------------------
function renderAiSecFormD(){
  const card = document.getElementById('aiSecFormDCard');
  const tb = document.querySelector('#aiSecFormDTable tbody');
  if (!card || !tb) return;
  const rows = (((DATA.market||{}).ai_funding||{}).form_d_filings) || [];
  const badge = document.getElementById('aiSecFormDBadge');
  if (!Array.isArray(rows) || !rows.length){
    if (badge) badge.textContent = 'EDGAR · no recent filings';
    tb.innerHTML = '<tr><td colspan="6" style="padding:14px;color:var(--muted)">No AI-adjacent Form D filings in the last 60 days. EDGAR may be unreachable, or no qualifying issuers filed in that window.</td></tr>';
    return;
  }
  if (badge) badge.textContent = 'EDGAR · ' + rows.length + ' filings · last 60d';
  // Sort by filed_date desc so the freshest deals lead.
  const sorted = rows.slice().sort((a,b) => {
    const da = a && a.filed_date ? Date.parse(a.filed_date) : 0;
    const db = b && b.filed_date ? Date.parse(b.filed_date) : 0;
    return (db||0) - (da||0);
  });
  tb.innerHTML = sorted.map(f => {
    const issuer  = escapeHtml(String(f.issuer || ''));
    const offer   = Number(f.total_offering_amount);
    const sold    = Number(f.total_amount_sold);
    const offerTxt = isFinite(offer) ? fmtUSD(offer, 'auto') : '—';
    const soldTxt  = isFinite(sold)  ? fmtUSD(sold,  'auto') : '—';
    const firstSale = escapeHtml(String(f.date_of_first_sale || ''));
    const filed     = escapeHtml(String(f.filed_date || ''));
    const exemptions = Array.isArray(f.exemptions) ? f.exemptions.join(', ') : '';
    const exTxt = escapeHtml(String(exemptions || ''));
    const url   = sanitizeUrl(f.filing_url, '');
    const issuerCell = url
      ? '<a href="'+url+'" target="_blank" rel="noopener" style="color:#a78bfa;font-weight:600;text-decoration:none">'+issuer+'</a>'
      : '<span style="font-weight:600">'+issuer+'</span>';
    return `<tr>
      <td>${issuerCell}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${offerTxt}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${soldTxt}</td>
      <td style="font-size:11px;color:var(--muted)">${firstSale}</td>
      <td style="font-size:11px;color:var(--muted)">${filed}</td>
      <td style="font-size:11px;color:var(--muted)">${exTxt}</td>
    </tr>`;
  }).join('');
}

// ---- AI quadrant scatter chart ----------------------------------------
const AI_QUADRANT_COLORS = {
  'LLM':'#a78bfa','Agents':'#22c55e','Coding':'#f59e0b','Robotics':'#ef4444',
  'Vision':'#06b6d4','Search':'#ec4899','Infra':'#94a3b8','Chips':'#facc15',
  'Audio':'#10b981','Video':'#8b5cf6','Bio':'#34d399','Other':'#64748b',
};
function aiQuadrantColor(cat){
  if (!cat) return AI_QUADRANT_COLORS.Other;
  const k = String(cat);
  if (AI_QUADRANT_COLORS[k]) return AI_QUADRANT_COLORS[k];
  // Stable-ish hash to fallback palette for unexpected categories.
  let h = 0; for (let i=0;i<k.length;i++) h = (h*31 + k.charCodeAt(i)) & 0xffff;
  const palette = Object.values(AI_QUADRANT_COLORS);
  return palette[h % palette.length];
}
function renderAiQuadrant(){
  const canvas = document.getElementById('aiQuadrantChart');
  if (!canvas) return;
  destroy('aiQuadrant');
  const rows = (((DATA.market||{}).ai_curated||{}).top_funded_companies) || [];
  if (!Array.isArray(rows) || !rows.length) return;
  // Group by category so the legend doubles as a category key.
  const byCat = new Map();
  rows.forEach(c => {
    const x = Number(c.last_round_size_usd);
    const y = Number(c.valuation_usd);
    if (!isFinite(x) || !isFinite(y) || x <= 0 || y <= 0) return;
    const cat = String(c.category || c.sector || 'Other');
    if (!byCat.has(cat)) byCat.set(cat, []);
    byCat.get(cat).push({
      x, y,
      name:  String(c.name || c.company || ''),
      stage: String(c.stage || c.last_round_stage || ''),
      cat,
    });
  });
  if (!byCat.size) return;
  const datasets = Array.from(byCat.entries()).map(([cat, pts]) => ({
    label: cat,
    data: pts,
    backgroundColor: aiQuadrantColor(cat) + 'cc',
    borderColor: aiQuadrantColor(cat),
    pointRadius: 6,
    pointHoverRadius: 8,
    borderWidth: 1,
  }));
  charts.aiQuadrant = new Chart(canvas, {
    type: 'scatter',
    data: { datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true, position: 'bottom', labels: { color: '#e6e8ee', boxWidth: 10, font: { size: 11 } } },
        tooltip: {
          callbacks: {
            label: ctx => {
              const d = ctx.raw || {};
              return [
                (d.name || '') + (d.stage ? ' · ' + d.stage : ''),
                'Valuation: ' + fmtUSD(d.y, 'auto'),
                'Last round: ' + fmtUSD(d.x, 'auto'),
                d.cat ? ('Category: ' + d.cat) : '',
              ].filter(Boolean);
            },
          },
        },
      },
      scales: {
        x: {
          type: 'logarithmic',
          title: { display: true, text: 'Last round size (USD, log)', color: '#8a93a6' },
          ticks: { color: '#8a93a6', callback: v => fmtUSD(v, 'auto') },
          grid: { color: '#1f2533' },
        },
        y: {
          type: 'logarithmic',
          title: { display: true, text: 'Valuation (USD, log)', color: '#8a93a6' },
          ticks: { color: '#8a93a6', callback: v => fmtUSD(v, 'auto') },
          grid: { color: '#1f2533' },
        },
      },
    },
  });
}

// ---- Research / whitepaper KPI strip ----------------------------------
function renderAiWhitepaperKpis(){
  const host = document.getElementById('aiWhitepaperKpis');
  if (!host) return;
  const kpis = (((DATA.market||{}).ai_curated||{}).whitepaper_kpis) || [];
  if (!Array.isArray(kpis) || !kpis.length){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px;grid-column:1/-1">No research benchmarks available.</div>';
    return;
  }
  host.innerHTML = kpis.map(k => {
    const label    = escapeHtml(String(k.label || k.name || ''));
    const value    = escapeHtml(String(k.value == null ? '—' : k.value));
    const prior    = k.prior_value == null ? '' : escapeHtml(String(k.prior_value));
    const deltaRaw = k.delta == null ? null : Number(k.delta);
    const deltaTxt = (k.delta == null) ? (k.delta_label ? escapeHtml(String(k.delta_label)) : '')
                   : (isFinite(deltaRaw) ? ((deltaRaw >= 0 ? '+' : '') + deltaRaw.toLocaleString(undefined,{maximumFractionDigits:2})) : escapeHtml(String(k.delta)));
    const deltaColor = (isFinite(deltaRaw) ? (deltaRaw >= 0 ? '#22c55e' : '#ef4444') : 'var(--muted)');
    const src       = escapeHtml(String(k.source || k.source_label || k.publisher || ''));
    const srcUrl    = sanitizeUrl(k.source_url || k.url, '');
    const unit      = escapeHtml(String(k.unit || ''));
    const inner = `
      <div style="display:flex;flex-direction:column;gap:6px;padding:12px 14px">
        <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em">${label}</div>
        <div style="font-size:24px;font-weight:700;line-height:1.1">${value}${unit ? ' <span style="font-size:13px;color:var(--muted);font-weight:600">'+unit+'</span>' : ''}</div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          ${deltaTxt ? '<span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;color:'+deltaColor+';border:1px solid '+deltaColor+'">'+deltaTxt+'</span>' : ''}
          ${prior ? '<span class="sub" style="font-size:10px;color:var(--muted)">prior: '+prior+'</span>' : ''}
        </div>
        ${src ? '<div class="sub" style="font-size:10px;color:var(--muted);margin-top:2px">source: <span style="color:#a78bfa;font-weight:600">'+src+'</span></div>' : ''}
      </div>`;
    if (srcUrl){
      return '<a class="chart-card" href="'+srcUrl+'" target="_blank" rel="noopener" style="text-decoration:none;color:var(--text);display:block">'+inner+'</a>';
    }
    return '<div class="chart-card">'+inner+'</div>';
  }).join('');
}

// Build the full stock-detail card body (rendered into the modal).
function stockDetailHtml(s){
  const score = Number(s.score) || 0;
  const color = score >= 20 ? '#22c55e' : (score <= -20 ? '#ef4444' : '#f59e0b');
  const chPct = Number(s.change_pct);
  const chColor = isFinite(chPct) ? (chPct >= 0 ? '#22c55e' : '#ef4444') : 'var(--muted)';
  const chTxt  = isFinite(chPct) ? ((chPct >= 0 ? '+' : '') + chPct.toFixed(2) + '%') : '—';
  const price  = Number(s.last_price);
  const priceTxt = isFinite(price)
    ? ('$' + price.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}))
    : '—';
  const scoreTxt = (score >= 0 ? '+' : '') + (Number.isInteger(score) ? score : score.toFixed(1));
  const clamped = Math.max(-100, Math.min(100, score));
  const pct = ((clamped + 100) / 200) * 100;
  const spark = signalScoreSparkline(s.history || []);
  const comps = Array.isArray(s.components) ? s.components : [];
  const compRows = comps.map(c => {
    const cs = Number(c.score) || 0;
    const csColor = cs > 0 ? '#22c55e' : (cs < 0 ? '#ef4444' : 'var(--muted)');
    const csTxt = (cs >= 0 ? '+' : '') + (Number.isInteger(cs) ? cs : cs.toFixed(1));
    return `<tr>
      <td style="padding:4px 6px">${escapeHtml(String(c.name || ''))}</td>
      <td style="color:var(--muted);padding:4px 6px">${escapeHtml(String(c.value == null ? '' : c.value))}</td>
      <td style="color:${csColor};text-align:right;padding:4px 6px;font-weight:600">${csTxt}</td>
    </tr>`;
  }).join('');
  const volTxt = fmtVolumeCompact(s.volume);
  return `<div style="display:flex;flex-direction:column;gap:12px">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap">
      <div>
        <div style="font-size:24px;font-weight:700;letter-spacing:0.4px">${escapeHtml(String(s.symbol||''))}</div>
        <div class="sub" style="font-size:13px;color:var(--muted)">${escapeHtml(String(s.name||''))}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:34px;font-weight:700;color:${color};line-height:1">${scoreTxt}</div>
        <div style="font-size:13px;color:${color};font-weight:600;margin-top:3px">${escapeHtml(String(s.label||''))}</div>
      </div>
    </div>
    <div style="height:12px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:6px;position:relative">
      <div style="position:absolute;top:-3px;left:calc(${pct.toFixed(1)}% - 4px);width:8px;height:18px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
    </div>
    <div style="display:flex;align-items:baseline;gap:18px;flex-wrap:wrap;font-size:15px">
      <div><span style="color:var(--muted);font-size:11px">Last price</span><br><strong>${priceTxt}</strong></div>
      <div><span style="color:var(--muted);font-size:11px">Today</span><br><strong style="color:${chColor}">${chTxt}</strong></div>
      <div><span style="color:var(--muted);font-size:11px">Volume</span><br><strong>${volTxt}</strong></div>
    </div>
    ${spark ? `<div><div class="sub" style="font-size:11px;color:var(--muted);margin-bottom:4px">Signal score · last 7 days</div>${spark}</div>` : ''}
    ${compRows ? `<div><div class="sub" style="font-size:11px;color:var(--muted);margin-bottom:4px">Signal breakdown</div>
      <table style="font-size:12px;width:100%"><thead><tr style="color:var(--muted);text-align:left">
        <th style="padding:4px 6px">Component</th><th style="padding:4px 6px">Value</th><th style="text-align:right;padding:4px 6px">&plusmn;</th>
      </tr></thead><tbody>${compRows}</tbody></table></div>` : ''}
  </div>`;
}

// Empty-state POC card for stock-detail modal — shown only when the stock
// has no `poc` payload (recent IPOs / <30d of OHLCV bars where compute_stock_poc
// returns None). The common path now renders the real volume-profile card
// via `pocCompactCardHtml` since stocks carry `poc` data alongside crypto.
function stockPocEmptyHtml(){
  return `<div class="chart-card stock-poc-card">
    <div class="head"><h2 style="margin:0;font-size:14px">Point of Control</h2></div>
    <div class="sub" style="color:var(--muted);padding:14px 6px;font-size:12px">
      Not enough trading history for a volume profile on this ticker.
    </div>
  </div>`;
}

function openStockDetail(symbol){
  const rows = ((DATA.market||{}).stocks_signals) || [];
  const s = rows.find(r => r && String(r.symbol||'') === symbol);
  if (!s) return;
  const modal = document.getElementById('stockDetailModal');
  if (!modal) return;
  document.getElementById('stockDetailTitle').textContent = `${s.symbol} · ${s.name||''}`;
  // 2-column grid on desktop (.grid2 = auto-fit minmax(420px,1fr)); the global
  // mobile rule at ≤860px collapses .grid2 → 1fr automatically so the Signal
  // card stacks on top of the POC card on phone viewports. No extra media
  // query required. Left = existing Signal card (Score + breakdown), Right =
  // real POC card from compute_stock_poc (or empty-state for tickers with
  // <30d of OHLCV).
  const pocCard = s.poc ? pocCompactCardHtml(s) : stockPocEmptyHtml();
  document.getElementById('stockDetailBody').innerHTML =
    '<div class="grid2 stocks-modal-body">' +
      '<div class="chart-card stock-signal-card">' + stockDetailHtml(s) + '</div>' +
      pocCard +
    '</div>';
  modal.classList.remove('hidden');
}
function closeStockDetail(){
  const m = document.getElementById('stockDetailModal');
  if (m) m.classList.add('hidden');
}

// Wire up stock-card click + keyboard activation + modal close. Run once.
(function wireStockDetail(){
  if (window._stockDetailWired) return; window._stockDetailWired = true;
  document.addEventListener('click', e => {
    const card = e.target.closest && e.target.closest('.stock-card[data-stock-symbol]');
    if (card){
      // Ignore clicks on filter chips that bubble (chips live outside #stocksGrid anyway).
      openStockDetail(card.getAttribute('data-stock-symbol'));
      return;
    }
    if (e.target && e.target.id === 'stockDetailClose') closeStockDetail();
    if (e.target && e.target.id === 'stockDetailModal') closeStockDetail();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeStockDetail();
    const card = e.target && e.target.closest && e.target.closest('.stock-card[data-stock-symbol]');
    if (card && (e.key === 'Enter' || e.key === ' ')){
      e.preventDefault();
      openStockDetail(card.getAttribute('data-stock-symbol'));
    }
  });
})();

// Apply the active stocks filter chip — reads from localStorage on first
// run, then drives both the chip highlight + per-card display:none.
function applyStocksFilter(bucket){
  let target = bucket;
  if (target == null){
    try { target = localStorage.getItem('stocksFilter') || 'all'; } catch(_) { target = 'all'; }
  }
  // Color the active chip per its bucket family (green = buy-side,
  // red = sell-side, amber = hold, neutral for all).
  const chips = document.querySelectorAll('[data-stocksfilter]');
  if (!chips.length) return;
  let found = false;
  chips.forEach(b => {
    const isActive = b.getAttribute('data-stocksfilter') === target;
    if (isActive) found = true;
    b.classList.toggle('active', isActive);
    // Inline color the active chip (the .btn.active rule already styles it,
    // but we want the matching bucket color to bleed through).
    if (isActive){
      const c = target.indexOf('buy')  >= 0 ? '#22c55e'
              : target.indexOf('sell') >= 0 ? '#ef4444'
              : target === 'hold'           ? '#f59e0b'
              : '';
      b.style.borderColor = c || '';
      b.style.color       = c || '';
    } else {
      b.style.borderColor = '';
      b.style.color       = '';
    }
  });
  if (!found){
    // Persisted value no longer maps to a chip — fall back to "all".
    target = 'all';
    const allChip = document.querySelector('[data-stocksfilter="all"]');
    if (allChip) allChip.classList.add('active');
  }
  // Section-level filter: hide whole sections that don't match the chip.
  document.querySelectorAll('#stocksGrid [data-stocks-section]').forEach(sec => {
    sec.style.display = (target === 'all' || sec.getAttribute('data-stocks-section') === target) ? '' : 'none';
  });
  // Card-level filter: also hide individual cards whose bucket doesn't match
  // (defensive — in case cards live outside a wrapped section).
  document.querySelectorAll('#stocksGrid [data-stock-bucket]').forEach(card => {
    card.style.display = (target === 'all' || card.getAttribute('data-stock-bucket') === target) ? '' : 'none';
  });
}

// Wire up chip clicks once. Persists selection in localStorage.
(function wireStocksFilter(){
  if (window._stocksFilterWired) return; window._stocksFilterWired = true;
  document.addEventListener('click', e => {
    const fb = e.target && e.target.closest && e.target.closest('[data-stocksfilter]');
    if (!fb) return;
    const bucket = fb.getAttribute('data-stocksfilter');
    try { localStorage.setItem('stocksFilter', bucket); } catch(_) {}
    applyStocksFilter(bucket);
  });
})();

// ============ SIGNAL BREADTH HELPERS ============
// Aggregate per-asset rolling score histories into a per-day distribution
// across STRONG BUY / BUY / HOLD / SELL / STRONG SELL buckets.
//
// Input: array of items, each with `history: [{date, score}, ...]`.
// Output: one snapshot per date in the union of histories, capped to last
// `days` entries:
//   [{date, strong_buy, buy, hold, sell, strong_sell, total}, ...]
// Buckets: >=50 STRONG BUY · >=20 BUY · > -20 HOLD · > -50 SELL · else STRONG SELL.
function computeSignalBreadth(items, days){
  const cap = (typeof days === 'number' && days > 0) ? days : 90;
  if (!Array.isArray(items) || items.length === 0) return [];
  const dates = new Set();
  items.forEach(it => {
    const h = it && Array.isArray(it.history) ? it.history : null;
    if (!h) return;
    h.forEach(pt => { if (pt && pt.date) dates.add(String(pt.date)); });
  });
  const allDates = Array.from(dates).sort();
  if (!allDates.length) return [];
  const recent = allDates.slice(-cap);
  const recentSet = new Set(recent);
  // Bucket each (item, date) — only over dates that survived the cap.
  const acc = new Map();
  recent.forEach(d => acc.set(d, {date:d, strong_buy:0, buy:0, hold:0, sell:0, strong_sell:0, total:0}));
  items.forEach(it => {
    const h = it && Array.isArray(it.history) ? it.history : null;
    if (!h) return;
    h.forEach(pt => {
      if (!pt || !pt.date || !recentSet.has(String(pt.date))) return;
      const sc = Number(pt.score);
      if (!isFinite(sc)) return;
      const row = acc.get(String(pt.date));
      if (!row) return;
      if      (sc >= 50)  row.strong_buy   += 1;
      else if (sc >= 20)  row.buy          += 1;
      else if (sc > -20)  row.hold         += 1;
      else if (sc > -50)  row.sell         += 1;
      else                row.strong_sell  += 1;
      row.total += 1;
    });
  });
  return recent.map(d => acc.get(d));
}

// Render a stacked-bar breadth chart into the given canvas id using a
// [{date, strong_buy, buy, hold, sell, strong_sell, total}, ...] series.
// Stacking order bottom-up: strong_sell, sell, hold, buy, strong_buy.
// X labels shown every ~10 days. Empty input renders an inline message.
function renderBreadthChart(canvasId, breadth, title){
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  destroy(canvasId);
  if (!Array.isArray(breadth) || breadth.length === 0){
    // Replace the canvas with an inline empty-state so the card still
    // signals presence-of-section but doesn't render a blank chart.
    const wrap = canvas.parentElement;
    if (wrap){
      wrap.innerHTML = '<div class="empty" style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:12px">No data available.</div>';
    }
    return;
  }
  const labels = breadth.map(r => r.date);
  // Show roughly every 10th date so labels don't collide.
  const step = Math.max(1, Math.ceil(labels.length / 10));
  const datasets = [
    {label:'STRONG SELL', data: breadth.map(r => r.strong_sell), backgroundColor: '#b91c1c'},
    {label:'SELL',        data: breadth.map(r => r.sell),        backgroundColor: '#ef4444'},
    {label:'HOLD',        data: breadth.map(r => r.hold),        backgroundColor: '#f59e0b'},
    {label:'BUY',         data: breadth.map(r => r.buy),         backgroundColor: '#22c55e'},
    {label:'STRONG BUY',  data: breadth.map(r => r.strong_buy),  backgroundColor: '#16a34a'},
  ];
  charts[canvasId] = new Chart(canvas, {
    type: 'bar',
    data: { labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: '#e6e8ee', font: { size: 10 }, boxWidth: 12 } },
        title: title ? { display: true, text: String(title), color: '#e6e8ee', font: { size: 12 } } : { display: false },
        tooltip: {
          mode: 'index',
          intersect: false,
          callbacks: {
            title: (items) => {
              if (!items || !items.length) return '';
              const idx = items[0].dataIndex;
              return breadth[idx] ? breadth[idx].date : '';
            },
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y}`,
            footer: (items) => {
              if (!items || !items.length) return '';
              const idx = items[0].dataIndex;
              const r = breadth[idx];
              return r ? `Total: ${r.total}` : '';
            },
          },
        },
      },
      scales: {
        x: {
          stacked: true,
          ticks: {
            color: '#8a93a6',
            autoSkip: false,
            maxRotation: 0,
            callback: function(_value, index){
              const lab = labels[index];
              return (index % step === 0) ? lab : '';
            },
          },
          grid: { color: '#1f2533', display: false },
        },
        y: {
          stacked: true,
          title: { display: true, text: 'Count', color: '#8a93a6' },
          ticks: { color: '#8a93a6', precision: 0 },
          grid: { color: '#1f2533' },
        },
      },
    },
  });
}

// Tiny inline SVG sparkline of the rolling-30d POC over the last 90 days.
// Stroke color slopes green/red based on first→last direction.
function pocMigrationSparkline(series){
  if (!series || series.length < 5) return '';
  const values = series.map(p => p.poc).filter(v => typeof v === 'number' && isFinite(v));
  if (values.length < 5) return '';
  const lo = Math.min(...values), hi = Math.max(...values);
  const range = (hi - lo) || 1;
  const n = values.length;
  const w = 100, h = 30, padTop = 10, padBot = 2;
  const pts = values.map((v, i) => {
    const x = (i / (n - 1)) * w;
    const y = padTop + (1 - (v - lo) / range) * (h - padTop - padBot);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  const first = values[0], last = values[values.length - 1];
  const trend = last > first * 1.005 ? '#22c55e' : last < first * 0.995 ? '#ef4444' : '#94a3b8';
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:30px;display:block;margin-top:6px;border-radius:3px;background:#0b0d12">
    <text x="2" y="7" font-size="6" fill="#64748b">30d POC drift · ${n}d</text>
    <text x="${w-2}" y="7" font-size="6" fill="#64748b" text-anchor="end">${fmtUsdShort(last)}</text>
    <polyline points="${pts}" fill="none" stroke="${trend}" stroke-width="1.2" vector-effect="non-scaling-stroke" />
  </svg>`;
}

// Beefier version of pocMigrationSparkline used on STRONG BUY / BUY cards so
// the buy-rated coins stand out visually with an actual chart thumbnail.
// Same data (30d POC over last ~90d), filled area + gridline + min/max
// markers + larger viewport. Same min-length / null guards.
function pocMigrationSparklineLarge(series){
  if (!series || series.length < 5) return '';
  const values = series.map(p => p.poc).filter(v => typeof v === 'number' && isFinite(v));
  if (values.length < 5) return '';
  const lo = Math.min(...values), hi = Math.max(...values);
  const range = (hi - lo) || 1;
  const n = values.length;
  const w = 200, h = 68, padTop = 14, padBot = 8, padX = 2;
  const xFor = i => padX + (i / (n - 1)) * (w - padX * 2);
  const yFor = v => padTop + (1 - (v - lo) / range) * (h - padTop - padBot);
  const pts = values.map((v, i) => `${xFor(i).toFixed(1)},${yFor(v).toFixed(1)}`).join(' ');
  const first = values[0], last = values[values.length - 1];
  const up = last > first * 1.005;
  const down = last < first * 0.995;
  const trend = up ? '#22c55e' : down ? '#ef4444' : '#94a3b8';
  const fill  = up ? '#22c55e22' : down ? '#ef444422' : '#94a3b822';
  const areaPts = `${padX},${(h - padBot).toFixed(1)} ${pts} ${(w - padX).toFixed(1)},${(h - padBot).toFixed(1)}`;
  // Midline reference: the first value, so the slope is intuitive at a glance.
  const yMid = yFor(first);
  const chgPct = first > 0 ? ((last - first) / first * 100) : null;
  const chgTxt = chgPct == null ? '' : (chgPct >= 0 ? '+' : '') + chgPct.toFixed(1) + '%';
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:64px;display:block;margin-top:6px;border-radius:4px;background:#0b0d12">
    <line x1="${padX}" y1="${yMid.toFixed(1)}" x2="${w - padX}" y2="${yMid.toFixed(1)}" stroke="#1f2533" stroke-width="1" stroke-dasharray="2,3"/>
    <polygon points="${areaPts}" fill="${fill}" stroke="none"/>
    <polyline points="${pts}" fill="none" stroke="${trend}" stroke-width="1.6" vector-effect="non-scaling-stroke"/>
    <text x="4" y="10" font-size="7" fill="#94a3b8">30d POC · last ${n}d</text>
    <text x="${w - 4}" y="10" font-size="7" fill="${trend}" text-anchor="end" font-weight="700">${chgTxt}</text>
    <text x="4" y="${(h - 2).toFixed(1)}" font-size="6" fill="#64748b">${fmtUsdShort(lo)}</text>
    <text x="${w - 4}" y="${(h - 2).toFixed(1)}" font-size="6" fill="#64748b" text-anchor="end">${fmtUsdShort(hi)}</text>
  </svg>`;
}

function renderPocCards(){
  const poc = (DATA.market||{}).poc || {};
  const host = document.getElementById('pocCards');
  if (!host) return;
  host.innerHTML = RESEARCH_ASSETS.map(a => {
    const d = poc[a];
    const accent = RESEARCH_ACCENT(a);
    if (!d || !POC_TFS.some(([k]) => d[k])){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">${a.toUpperCase()}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no POC data</div></div>`;
    }
    const rows = POC_TFS.map(([k]) => d[k]);
    const anchor = d.d90 || d.d30 || rows.find(Boolean);
    // Cluster badge: 3+ TFs within 2%
    const clustered = pocClustered(rows);
    const clusterBadge = clustered
      ? '<span style="background:#a78bfa22;color:#a78bfa;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:600" title="3+ timeframes within 2%">🎯 CLUSTERED</span>'
      : '';
    // Migration badge: 30d vs 90d POC delta
    const mig = d.migration;
    let migBadge = '';
    if (mig){
      const cfg = mig.direction === 'UP'
        ? {bg:'#22c55e22', fg:'#22c55e', arrow:'↑', label:`Migrating UP ${mig.delta_pct >= 0 ? '+' : ''}${mig.delta_pct}%`}
        : mig.direction === 'DOWN'
        ? {bg:'#ef444422', fg:'#ef4444', arrow:'↓', label:`Migrating DOWN ${mig.delta_pct}%`}
        : {bg:'#6b728022', fg:'var(--muted)', arrow:'·', label:'Value stable'};
      const tip = (mig.explanation || '').replace(/"/g,'&quot;');
      migBadge = `<span title="${tip}" style="background:${cfg.bg};color:${cfg.fg};padding:2px 6px;border-radius:3px;font-size:10px;font-weight:600;cursor:help">${cfg.arrow} ${cfg.label}${mig.between_pocs ? ' ⇆' : ''}</span>`;
    }
    // 4-row ladder
    const ladder = POC_TFS.map(([k, label]) => {
      const r = d[k];
      if (!r) return `<tr><td style="color:var(--muted);font-size:10px">${label}</td><td colspan="3" style="color:var(--muted)">—</td></tr>`;
      const inVA = r.in_value_area;
      const tag = inVA
        ? '<span style="background:#22c55e22;color:#22c55e;padding:1px 5px;border-radius:3px;font-size:9px;font-weight:600">IN VA</span>'
        : '<span style="background:#f59e0b22;color:#f59e0b;padding:1px 5px;border-radius:3px;font-size:9px;font-weight:600">OUT</span>';
      const dc = r.distance_pct == null ? 'var(--muted)' : (r.distance_pct >= 0 ? '#22c55e' : '#ef4444');
      const dt = r.distance_pct == null ? '—' : (r.distance_pct >= 0 ? '+' : '') + r.distance_pct.toFixed(1) + '%';
      return `<tr>
        <td style="color:var(--muted);font-size:10px">${label}</td>
        <td style="font-weight:600">${fmtUsdShort(r.poc)}</td>
        <td style="color:${dc};text-align:right">${dt}</td>
        <td style="text-align:right">${tag}</td>
      </tr>`;
    }).join('');
    // Naked POCs subsection
    const naked = Array.isArray(d.naked) ? d.naked : [];
    const cur = anchor && anchor.current;
    const nakedHtml = naked.length ? `
      <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">
        <div style="font-size:11px;color:var(--muted);margin-bottom:4px">Naked POCs <span style="opacity:.7">(untested magnet levels, 180d)</span></div>
        ${naked.map(n => {
          const isSupport = cur != null && cur > n.poc;
          const col = isSupport ? '#22c55e' : '#ef4444';
          const sign = n.distance_pct >= 0 ? '+' : '';
          return `<div style="display:flex;justify-content:space-between;font-size:11px;padding:1px 0">
            <span style="color:${col};font-weight:600">${fmtUsdShort(n.poc)}</span>
            <span style="color:var(--muted)">${n.days_ago}d ago · ${sign}${n.distance_pct}%</span>
          </div>`;
        }).join('')}
      </div>` : '';
    const sparkline = pocMigrationSparkline(d.migration_series);
    return `<div class="card" style="border-left:4px solid ${accent}">
      <div style="display:flex;justify-content:space-between;align-items:baseline;gap:6px;flex-wrap:wrap">
        <h3 style="font-size:13px;color:var(--text);margin:0">${a.toUpperCase()}
          <span class="sub" style="color:var(--muted);font-size:10px">${fmtUsdShort(anchor.current)} now</span>
        </h3>
        <div style="display:flex;gap:4px;flex-wrap:wrap">${clusterBadge}${migBadge}</div>
      </div>
      ${sparkline}
      <div style="display:flex;gap:10px;margin-top:8px">
        <div style="flex:1;min-width:0">
          <table style="width:100%;font-size:11px;border-collapse:collapse">
            <thead><tr style="color:var(--muted);font-size:9px;text-align:left">
              <th>TF</th><th>POC</th><th style="text-align:right">Δ</th><th style="text-align:right">VA</th>
            </tr></thead>
            <tbody>${ladder}</tbody>
          </table>
        </div>
        <div style="flex:0 0 120px">${volumeProfileSVG(d.d90, d.d30, anchor.current)}</div>
      </div>
      ${nakedHtml}
    </div>`;
  }).join('');
}

// ===== Sentiment composite cards (Overview / DeFi / ETF Flows / Futures) =====
// Each tab has its own domain-specific sentiment composite card mirroring the
// visual pattern of #pocSentimentCard. paintSentimentCard() is the shared
// writer — given a card id prefix, a net score in [-100, +100], a label
// tier, a positive/neutral/negative weight split for the bar, and a
// subline, it paints all the DOM elements consistently across the 4 cards.
function paintSentimentCard(prefix, net, label, color, posPct, neuPct, negPct, subline){
  const card = document.getElementById(prefix + 'Card');
  if (!card) return;
  card.style.display = '';
  const scoreEl = document.getElementById(prefix + 'Score');
  const labelEl = document.getElementById(prefix + 'Label');
  const sublineEl = document.getElementById(prefix + 'Subline');
  const barPos = document.getElementById(prefix + 'BarPos');
  const barNeu = document.getElementById(prefix + 'BarNeu');
  const barNeg = document.getElementById(prefix + 'BarNeg');
  if (scoreEl){
    scoreEl.textContent = (net >= 0 ? '+' : '') + net;
    scoreEl.style.color = color;
  }
  if (labelEl){
    labelEl.textContent = label;
    labelEl.style.color = color;
  }
  if (sublineEl){
    sublineEl.textContent = subline;
  }
  if (barPos) barPos.style.width = posPct.toFixed(1) + '%';
  if (barNeu) barNeu.style.width = neuPct.toFixed(1) + '%';
  if (barNeg) barNeg.style.width = negPct.toFixed(1) + '%';
}
function hideSentimentCard(prefix){
  const card = document.getElementById(prefix + 'Card');
  if (card) card.style.display = 'none';
}
// Given a composite net score in [-100,+100], plus an array of normalized
// component scores in [-100,+100], compute the proportional bar split (pos /
// neu / neg) by summing absolute positive contributions, absolute negative
// contributions, and a "neutral" slack for anything in [-20,+20]. Returns
// percentages that sum to 100 (or all zeros if no components are finite).
function sentimentBarSplit(components){
  let pos = 0, neg = 0, neu = 0;
  for (const c of components){
    if (!isFinite(c)) continue;
    if (c >= 20) pos += c;
    else if (c <= -20) neg += -c;
    else neu += 20;
  }
  const total = pos + neg + neu;
  if (total <= 0) return { pos: 0, neu: 100, neg: 0 };
  return { pos: (pos / total) * 100, neu: (neu / total) * 100, neg: (neg / total) * 100 };
}
// Standard 5-bucket label + color from a net score in [-100,+100], with
// caller-supplied labels for each bucket so each tab can use domain-specific
// terminology (BULLISH vs INFLOWS vs CROWDED LONGS, etc.).
function sentimentBucket(net, labels){
  const label = net >=  50 ? labels[0]
              : net >=  20 ? labels[1]
              : net >  -20 ? labels[2]
              : net >  -50 ? labels[3]
              :              labels[4];
  const color = net >=  20 ? '#22c55e'
              : net <= -20 ? '#ef4444'
              :              '#f59e0b';
  return { label, color };
}
function clampScore(v){
  if (!isFinite(v)) return null;
  if (v >  100) return  100;
  if (v < -100) return -100;
  return v;
}

// ---- Overview: composite of Fear & Greed, top-50 signal avg, and avg perp
// funding rate. Subline lists the 3 inputs.
function renderOverviewSentiment(){
  const m = DATA.market || {};
  const components = [];
  const inputLabels = [];
  // 1) Fear & Greed: 0..100 → (-100..+100)
  const fngArr = Array.isArray(m.fear_greed) ? m.fear_greed : [];
  const fngLast = fngArr.length ? fngArr[fngArr.length - 1] : null;
  const fngVal = fngLast && Number(fngLast.value);
  if (fngLast && isFinite(fngVal)){
    components.push(clampScore((fngVal - 50) * 2));
    inputLabels.push('F&G');
  }
  // 2) Top-50 signal-score average (excluding stables — score is the per-coin
  //    composite already in ±100 range).
  const sigs = Array.isArray(DATA.signals_top20) ? DATA.signals_top20 : [];
  const STABLES = new Set(['USDT','USDC','DAI','TUSD','USDE','FDUSD','PYUSD','BUSD','USDD']);
  const sigScores = sigs
    .filter(s => s && !STABLES.has(String(s.symbol||'').toUpperCase()))
    .map(s => Number(s.score))
    .filter(v => isFinite(v));
  if (sigScores.length){
    const avg = sigScores.reduce((a,b)=>a+b,0) / sigScores.length;
    components.push(clampScore(avg));
    inputLabels.push('signal avg');
  }
  // 3) Avg Coinbase Intl perp funding rate. > 0.0001 (0.01%) per +0.0001
  //    contributes +20; clamp to ±100. Positive funding = crowded longs.
  const perps = Array.isArray(m.coinbase_intl_perps) ? m.coinbase_intl_perps : [];
  const rates = perps
    .map(p => p && Number(p.funding_rate))
    .filter(v => isFinite(v));
  if (rates.length){
    const avgRate = rates.reduce((a,b)=>a+b,0) / rates.length;
    components.push(clampScore((avgRate / 0.0001) * 20));
    inputLabels.push('perp funding');
  }
  if (!components.length){
    hideSentimentCard('overviewSentiment');
    return;
  }
  const net = Math.round(components.reduce((a,b)=>a+b,0) / components.length);
  const bucket = sentimentBucket(net,
    ['STRONG BULLISH','BULLISH','NEUTRAL','BEARISH','STRONG BEARISH']);
  const split = sentimentBarSplit(components);
  paintSentimentCard('overviewSentiment', net, bucket.label, bucket.color,
    split.pos, split.neu, split.neg,
    `Composite of Fear & Greed · Top-50 signal avg · perp funding rate (${components.length} inputs)`);
}

// ---- DeFi: TVL-weighted 7d chain momentum + stablecoin mcap 7d Δ.
function renderDefiSentiment(){
  const m = DATA.market || {};
  // DeFi sentiment card is only rendered from within the DeFi tab (called by
  // renderDefi), so it can safely read from the lazy-loaded DATA.defi.
  const defi = DATA.defi || {};
  const llama = m.defillama || {};
  const chains = Array.isArray(defi.chains) ? defi.chains : [];
  const components = [];
  // 1) TVL-weighted 7d chain momentum: Σ(tvl × change_7d_pct) / Σ(tvl).
  //    Clip absolute to ±50% → ±100.
  let wsum = 0, wnorm = 0;
  for (const c of chains){
    const tvl = Number(c && c.tvl_usd);
    const chg = Number(c && c.change_7d_pct);
    if (!isFinite(tvl) || tvl <= 0 || !isFinite(chg)) continue;
    wsum  += tvl * chg;
    wnorm += tvl;
  }
  if (wnorm > 0){
    const avgPct = wsum / wnorm;
    components.push(clampScore((avgPct / 50) * 100));
  }
  // 2) Stablecoin mcap 7d change as % of mcap → ±5% → ±100.
  const stableD = Number(llama && llama.stablecoin_7d_change_usd);
  const stableM = Number(llama && llama.stablecoin_mcap_usd);
  if (isFinite(stableD) && isFinite(stableM) && stableM > 0){
    const pct = (stableD / stableM) * 100;
    components.push(clampScore((pct / 5) * 100));
  }
  if (!components.length){
    hideSentimentCard('defiSentiment');
    return;
  }
  const net = Math.round(components.reduce((a,b)=>a+b,0) / components.length);
  const bucket = sentimentBucket(net,
    ['STRONG EXPANSION','EXPANSION','NEUTRAL','CONTRACTION','STRONG CONTRACTION']);
  const split = sentimentBarSplit(components);
  paintSentimentCard('defiSentiment', net, bucket.label, bucket.color,
    split.pos, split.neu, split.neg,
    `TVL-weighted 7d chain momentum · stablecoin mcap 7d Δ (${components.length} inputs)`);
}

// ---- ETF Flows: 7d net flow sum + 30d net flow sum, weighted 60/40 toward
// the 7d. Tracks state.etfAsset.
function renderEtfFlowSentiment(){
  const d = etfData();
  const daily = Array.isArray(d && d.daily) ? d.daily : [];
  if (!daily.length){
    hideSentimentCard('etfFlowSentiment');
    return;
  }
  const flowVal = r => {
    if (!r) return null;
    const v = Number(r.flow);
    return isFinite(v) ? v : null;
  };
  // Daily flow is in USD millions (Farside convention). $500M = 500.
  const NORM = 500;
  const last7 = daily.slice(-7).map(flowVal).filter(v => v != null);
  const last30 = daily.slice(-30).map(flowVal).filter(v => v != null);
  const components = [];
  if (last7.length){
    const sum7 = last7.reduce((a,b)=>a+b,0);
    components.push({ s: clampScore((sum7 / NORM) * 100), w: 0.6 });
  }
  if (last30.length){
    const sum30 = last30.reduce((a,b)=>a+b,0);
    components.push({ s: clampScore((sum30 / NORM) * 100), w: 0.4 });
  }
  if (!components.length){
    hideSentimentCard('etfFlowSentiment');
    return;
  }
  const totalW = components.reduce((a,b)=>a+b.w,0);
  const net = Math.round(components.reduce((a,b)=>a+b.s*b.w,0) / totalW);
  const sym = (etfAsset() || 'btc').toUpperCase();
  const bucket = sentimentBucket(net,
    ['STRONG INFLOWS','INFLOWS','BALANCED','OUTFLOWS','STRONG OUTFLOWS']);
  const split = sentimentBarSplit(components.map(c => c.s));
  paintSentimentCard('etfFlowSentiment', net, bucket.label, bucket.color,
    split.pos, split.neu, split.neg,
    `${sym} ETF · 7d net flow sum (60%) · 30d net flow sum (40%)`);
}

// ---- Futures: funding rate + long/short ratio + 7d OI %Δ. Tracks state.asset
// (toggle on the Futures tab mirrors choice into state.asset).
function renderFuturesSentiment(){
  const m = DATA.market || {};
  const asset = state && state.asset ? state.asset : 'btc';
  const a = m[asset] || {};
  const components = [];
  // 1) Funding rate. > 0.05% = +100, < -0.05% = -100, linear in between.
  const fundArr = Array.isArray(a.funding) ? a.funding : [];
  const fundLast = fundArr.length ? fundArr[fundArr.length - 1] : null;
  const rate = fundLast && Number(fundLast.rate);
  if (isFinite(rate)){
    // 0.05% as a fraction = 0.0005. Map ±0.0005 → ±100.
    components.push(clampScore((rate / 0.0005) * 100));
  }
  // 2) Long/short ratio. > 2 = +100, < 0.5 = -100. Log-scale linear: take
  //    log2(ratio); ±1 → ±100. Clamps via clampScore.
  const lsArr = Array.isArray(a.long_short_ratio) ? a.long_short_ratio : [];
  const lsLast = lsArr.length ? lsArr[lsArr.length - 1] : null;
  const ratio = lsLast && Number(lsLast.ratio);
  if (isFinite(ratio) && ratio > 0){
    const lg = Math.log2(ratio);
    components.push(clampScore(lg * 100));
  }
  // 3) 7d OI % change. ±50% → ±100.
  const oiArr = Array.isArray(a.open_interest_usd) ? a.open_interest_usd : [];
  if (oiArr.length > 7){
    const cur = Number(oiArr[oiArr.length - 1] && oiArr[oiArr.length - 1].oi_usd);
    const prev = Number(oiArr[oiArr.length - 1 - 7] && oiArr[oiArr.length - 1 - 7].oi_usd);
    if (isFinite(cur) && isFinite(prev) && prev > 0){
      const pct = (cur / prev - 1) * 100;
      components.push(clampScore((pct / 50) * 100));
    }
  }
  if (!components.length){
    hideSentimentCard('futuresSentiment');
    return;
  }
  const net = Math.round(components.reduce((a,b)=>a+b,0) / components.length);
  const sym = asset.toUpperCase();
  const bucket = sentimentBucket(net,
    ['STRONG CROWDED LONGS','CROWDED LONGS','BALANCED','CROWDED SHORTS','STRONG CROWDED SHORTS']);
  const split = sentimentBarSplit(components);
  paintSentimentCard('futuresSentiment', net, bucket.label, bucket.color,
    split.pos, split.neu, split.neg,
    `${sym} · funding rate · long/short ratio · 7d OI Δ (${components.length} inputs)`);
}

// ===== POC top-25 grid (Point of Control tab) =====
// Renders one card per top-25 coin from DATA.market.poc_top. Reuses the
// renderPocCards() layout but keyed off coin metadata (image/symbol/name/price)
// instead of the fixed RESEARCH_ASSETS list.
// POC SENTIMENT INDEX — aggregate migration direction across the top 25
// by signal score. Renders into #pocSentimentCard. Index in [-100,+100]:
// positive = broad accumulation (POCs drifting higher), negative = broad
// distribution. Label thresholds match the signal-score conventions used
// elsewhere on the dashboard (±50 strong, ±20 moderate).
function renderPocSentimentIndex(){
  const card = document.getElementById('pocSentimentCard');
  if (!card) return;
  const list = ((DATA.market || {}).poc_top) || [];
  if (!Array.isArray(list) || list.length === 0){
    card.style.display = 'none';
    return;
  }
  card.style.display = '';
  // Use ALL coins on the POC tab (top 50 by market cap) per user request.
  // No score-based filtering — the index represents broad migration across
  // the full top-50 universe.
  const scored = list;
  // Count migration direction across the full top 50.
  let up = 0, down = 0, flat = 0, considered = 0;
  for (const c of scored){
    const dir = c && c.poc && c.poc.migration && c.poc.migration.direction;
    if (dir === 'UP')        { up++;   considered++; }
    else if (dir === 'DOWN') { down++; considered++; }
    else if (dir === 'FLAT') { flat++; considered++; }
  }
  const total = Math.max(considered, 1);
  const net = Math.round(((up - down) / total) * 100);
  // Bucket label
  const label = net >=  50 ? 'STRONG ACCUMULATION'
              : net >=  20 ? 'ACCUMULATION'
              : net >  -20 ? 'NEUTRAL'
              : net >  -50 ? 'DISTRIBUTION'
              :              'STRONG DISTRIBUTION';
  const color = net >=  20 ? '#22c55e'
              : net <= -20 ? '#ef4444'
              :              '#f59e0b';
  // Write values into the DOM
  const scoreEl = document.getElementById('pocSentimentScore');
  const labelEl = document.getElementById('pocSentimentLabel');
  const sublineEl = document.getElementById('pocSentimentSubline');
  if (scoreEl){
    scoreEl.textContent = (net >= 0 ? '+' : '') + net;
    scoreEl.style.color = color;
  }
  if (labelEl){
    labelEl.textContent = label;
    labelEl.style.color = color;
  }
  if (sublineEl){
    sublineEl.textContent = `${considered} coins with migration data · positive = POCs drifting higher (broad accumulation) · negative = drifting lower (broad distribution)`;
  }
  const pctUp   = (up   / total) * 100;
  const pctFlat = (flat / total) * 100;
  const pctDown = (down / total) * 100;
  const upBar   = document.getElementById('pocSentimentBarUp');
  const flatBar = document.getElementById('pocSentimentBarFlat');
  const downBar = document.getElementById('pocSentimentBarDown');
  if (upBar)   upBar.style.width   = pctUp.toFixed(1) + '%';
  if (flatBar) flatBar.style.width = pctFlat.toFixed(1) + '%';
  if (downBar) downBar.style.width = pctDown.toFixed(1) + '%';
  const upCount   = document.getElementById('pocSentimentUpCount');
  const flatCount = document.getElementById('pocSentimentFlatCount');
  const downCount = document.getElementById('pocSentimentDownCount');
  if (upCount)   upCount.textContent   = String(up);
  if (flatCount) flatCount.textContent = String(flat);
  if (downCount) downCount.textContent = String(down);
}

function renderPocTopCards(){
  const host = document.getElementById('pocTopGrid');
  const featuredHost = document.getElementById('pocFeaturedRow');
  if (!host) return;
  const list = ((DATA.market || {}).poc_top) || [];
  if (!Array.isArray(list) || list.length === 0){
    host.innerHTML = '<div class="empty" style="grid-column:1/-1">POC data populating — run python app.py --fetch-market and reload.</div>';
    if (featuredHost) featuredHost.innerHTML = '';
    return;
  }
  // --- Join: index signals_top20 by uppercase symbol and attach score/label
  // onto each POC entry before sorting/rendering. Entries with no matching
  // signal get null score/label and sort LAST (treated as -Infinity).
  const sigArr = Array.isArray(DATA.signals_top20) ? DATA.signals_top20 : [];
  const sigBySym = {};
  sigArr.forEach(s => {
    if (!s) return;
    const k = String(s.symbol || '').toUpperCase();
    if (k) sigBySym[k] = s;
  });
  const joined = list.map(c => {
    const sk = String(c.symbol || c.coin_id || '').toUpperCase();
    const sig = sigBySym[sk];
    return Object.assign({}, c, {
      signal_score: sig && sig.score != null ? Number(sig.score) : null,
      signal_label: sig && sig.label != null ? String(sig.label) : null,
    });
  });
  const sorted = joined.slice().sort((a, b) => {
    const sa = (a.signal_score == null || !isFinite(a.signal_score)) ? -Infinity : Number(a.signal_score);
    const sb = (b.signal_score == null || !isFinite(b.signal_score)) ? -Infinity : Number(b.signal_score);
    return sb - sa;
  });
  // Top 4 cards get FEATURED treatment per user request — rendered into a
  // separate #pocFeaturedRow above the main grid so they spread evenly
  // (1×4 desktop / 2×2 tablet / 1-up phone). Position 5+ goes into the
  // regular #pocTopGrid at the standard compact size.
  const FEATURED_N = 4;
  const cardHtml = sorted.map((c, idx) => {
    const featured = idx < FEATURED_N;
    const cid = escapeHtml(String(c.coin_id || c.symbol || ''));
    const sym = escapeHtml(String(c.symbol || c.coin_id || '').toUpperCase());
    const imgUrl = sanitizeUrl(c.image, '');
    const imgSize = featured ? 28 : 18;
    const img = imgUrl
      ? `<img src="${imgUrl}" alt="" style="width:${imgSize}px;height:${imgSize}px;border-radius:50%">`
      : `<div style="width:${imgSize}px;height:${imgSize}px;border-radius:50%;background:#1f2533"></div>`;
    const priceTxt = fmtUsdShort(c.current_price);
    // Signal badge: score + label, color-coded green/red/amber.
    const sc = c.signal_score;
    const bucket = c.signal_label ? stockLabelBucket(c.signal_label) : 'hold';
    let sigBadge = '';
    if (sc != null && isFinite(sc)){
      const sColor = sc >= 20 ? '#22c55e' : (sc <= -20 ? '#ef4444' : '#f59e0b');
      const sTxt = (sc >= 0 ? '+' : '') + (Number.isInteger(sc) ? sc : sc.toFixed(1));
      const lblTxt = c.signal_label ? escapeHtml(String(c.signal_label)) : '';
      const badgeFont = featured ? 13 : 10;
      const badgePad  = featured ? '2px 9px' : '1px 6px';
      sigBadge = `<span style="background:${sColor}22;color:${sColor};padding:${badgePad};border-radius:4px;font-size:${badgeFont}px;font-weight:600;white-space:nowrap" title="Signal score">${sTxt}${lblTxt ? ' · ' + lblTxt : ''}</span>`;
    }
    const d = c.poc || {};
    if (!d.d30 && !d.d90 && !d.d180){
      return `<div class="card poc-card" data-poc-coin-id="${cid}" data-poc-bucket="${bucket}" role="button" tabindex="0" aria-label="Open ${sym} POC detail" title="Click for full breakdown" style="border-left:4px solid #a78bfa;padding:8px 10px;cursor:pointer">
        <div style="display:flex;align-items:center;gap:6px">
          ${img}
          <span style="font-weight:700;font-size:12px">${sym}</span>
          ${sigBadge}
          <span class="sub" style="font-size:10px;color:var(--muted);margin-left:auto">${priceTxt}</span>
        </div>
        <div class="sub" style="color:var(--muted);margin-top:4px;font-size:10px">no POC data</div>
      </div>`;
    }
    const anchor = d.d90 || d.d30 || d.d180;
    const anchorPoc = anchor ? fmtUsdShort(anchor.poc) : '—';
    const dp = anchor && anchor.distance_pct != null ? anchor.distance_pct : null;
    const dpColor = dp == null ? 'var(--muted)' : (dp >= 0 ? '#22c55e' : '#ef4444');
    const dpTxt = dp == null ? '—' : ((dp >= 0 ? '+' : '') + dp.toFixed(1) + '%');
    const inVA = anchor && anchor.in_value_area;
    const vaTag = anchor
      ? (inVA
          ? '<span style="background:#22c55e22;color:#22c55e;padding:0 4px;border-radius:3px;font-size:9px;font-weight:600">IN VA</span>'
          : '<span style="background:#f59e0b22;color:#f59e0b;padding:0 4px;border-radius:3px;font-size:9px;font-weight:600">OUT</span>')
      : '';
    // BIG migration arrow on the right edge — primary visual cue for direction.
    // Featured cards get an even bigger arrow rail.
    const mig = d.migration;
    const railW  = featured ? 64 : 44;
    const arrowFs= featured ? 38 : 26;
    const labelFs= featured ? 11 : 9;
    let migBlock;
    if (mig){
      const dlt = Number(mig.delta_pct);
      const dltTxt = isFinite(dlt) ? ((dlt >= 0 ? '+' : '') + dlt.toFixed(1) + '%') : '';
      const cfg = mig.direction === 'UP'
        ? {fg:'#22c55e', arrow:'↑', label:'UP'}
        : mig.direction === 'DOWN'
        ? {fg:'#ef4444', arrow:'↓', label:'DOWN'}
        : {fg:'#94a3b8',  arrow:'·', label:'FLAT'};
      const tip = escapeHtml(mig.explanation || `POC migration ${cfg.label}`);
      migBlock = `<div title="${tip}" style="flex:0 0 ${railW}px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;padding:2px 0;border-left:1px solid var(--border);color:${cfg.fg}">
        <div style="font-size:${arrowFs}px;line-height:1;font-weight:700">${cfg.arrow}</div>
        <div style="font-size:${labelFs}px;font-weight:700;letter-spacing:.04em">${cfg.label}</div>
        ${dltTxt ? `<div style="font-size:${labelFs}px;opacity:.85">${dltTxt}</div>` : ''}
      </div>`;
    } else {
      migBlock = `<div title="No migration data" style="flex:0 0 ${railW}px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;padding:2px 0;border-left:1px solid var(--border);color:var(--muted)">
        <div style="font-size:${arrowFs}px;line-height:1;font-weight:700">·</div>
        <div style="font-size:${labelFs}px;font-weight:700;letter-spacing:.04em">—</div>
      </div>`;
    }
    // 30d POC drift sparkline. Featured cards always get the big version,
    // and so do STRONG BUY / BUY cards below. Everything else gets the
    // tight 30px sparkline.
    const isBuy = bucket === 'strong-buy' || bucket === 'buy';
    const spark = (featured || isBuy)
      ? pocMigrationSparklineLarge(d.migration_series)
      : pocMigrationSparkline(d.migration_series);
    const cardPad   = featured ? '14px 16px' : '8px 10px';
    const symFs     = featured ? 18 : 12;
    const priceFs   = featured ? 13 : 10;
    const pocRowFs  = featured ? 14 : 11;
    const fallbackH = (featured || isBuy) ? 64 : 30;
    // Featured cards live in their own #pocFeaturedRow grid (separate from
    // the main #pocTopGrid below), so no grid-column span is needed — the
    // separate grid handles even spacing.
    const featuredCSS = featured
      ? 'border-left:6px solid #a78bfa;background:#10151f'
      : 'border-left:4px solid #a78bfa';
    return `<div class="card poc-card" data-poc-coin-id="${cid}" data-poc-bucket="${bucket}" ${featured ? 'data-poc-featured="1"' : ''} role="button" tabindex="0" aria-label="Open ${sym} POC detail" title="Click for full breakdown" style="${featuredCSS};padding:${cardPad};cursor:pointer">
      <div style="display:flex;align-items:stretch;gap:${featured ? 12 : 8}px">
        <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:${featured ? 6 : 3}px">
          <div style="display:flex;align-items:center;gap:${featured ? 8 : 6}px;flex-wrap:wrap">
            ${img}
            <span style="font-weight:700;font-size:${symFs}px">${sym}</span>
            ${sigBadge}
            <span class="sub" style="font-size:${priceFs}px;color:var(--muted);margin-left:auto">${priceTxt}</span>
          </div>
          <div style="display:flex;align-items:baseline;justify-content:space-between;gap:6px;font-size:${pocRowFs}px">
            <span style="color:var(--muted);font-size:${pocRowFs - 1}px">90d POC</span>
            <span style="font-weight:600">${anchorPoc}</span>
            <span style="color:${dpColor};font-weight:600">${dpTxt}</span>
            ${vaTag}
          </div>
          ${spark || `<div style="height:${fallbackH}px;margin-top:6px;border-radius:3px;background:#0b0d12;display:flex;align-items:center;justify-content:center;font-size:9px;color:var(--muted)">no drift data</div>`}
        </div>
        ${migBlock}
      </div>
    </div>`;
  });
  // Split: first FEATURED_N (4) cards into the featured row, the rest into
  // the main grid.
  if (featuredHost){
    featuredHost.innerHTML = cardHtml.slice(0, FEATURED_N).join('');
  }
  host.innerHTML = cardHtml.slice(FEATURED_N).join('');
  applyPocFilter();
}

// Apply the active POC filter chip — reads from localStorage on first run,
// then drives both the chip highlight + per-card display:none. Mirrors
// applyStocksFilter() exactly, keyed under 'pocFilter' instead.
function applyPocFilter(bucket){
  let target = bucket;
  if (target == null){
    try { target = localStorage.getItem('pocFilter') || 'all'; } catch(_) { target = 'all'; }
  }
  const chips = document.querySelectorAll('[data-pocfilter]');
  if (!chips.length) return;
  let found = false;
  chips.forEach(b => {
    const isActive = b.getAttribute('data-pocfilter') === target;
    if (isActive) found = true;
    b.classList.toggle('active', isActive);
    if (isActive){
      const c = target.indexOf('buy')  >= 0 ? '#22c55e'
              : target.indexOf('sell') >= 0 ? '#ef4444'
              : target === 'hold'           ? '#f59e0b'
              : '';
      b.style.borderColor = c || '';
      b.style.color       = c || '';
    } else {
      b.style.borderColor = '';
      b.style.color       = '';
    }
  });
  if (!found){
    target = 'all';
    const allChip = document.querySelector('[data-pocfilter="all"]');
    if (allChip) allChip.classList.add('active');
  }
  // Filter applies to BOTH the featured row and the main grid below it.
  document.querySelectorAll('#pocFeaturedRow [data-poc-bucket], #pocTopGrid [data-poc-bucket]').forEach(card => {
    card.style.display = (target === 'all' || card.getAttribute('data-poc-bucket') === target) ? '' : 'none';
  });
}

// Wire up POC filter chip clicks once. Persists selection in localStorage.
(function wirePocFilter(){
  if (window._pocFilterWired) return; window._pocFilterWired = true;
  document.addEventListener('click', e => {
    const fb = e.target && e.target.closest && e.target.closest('[data-pocfilter]');
    if (!fb) return;
    const bucket = fb.getAttribute('data-pocfilter');
    try { localStorage.setItem('pocFilter', bucket); } catch(_) {}
    applyPocFilter(bucket);
  });
})();

// Full POC detail (modal body). Mirrors the old verbose card layout —
// migration badge, ladder, naked POCs, migration sparkline.
function pocDetailHtml(c){
  const sym  = escapeHtml(String(c.symbol || c.coin_id || '').toUpperCase());
  const name = escapeHtml(c.name || '');
  const imgUrl = sanitizeUrl(c.image, '');
  const img = imgUrl
    ? `<img src="${imgUrl}" alt="" style="width:32px;height:32px;border-radius:50%">`
    : '<div style="width:32px;height:32px;border-radius:50%;background:#1f2533"></div>';
  // Stocks expose `last_price` rather than `current_price`; fall through to
  // the POC anchor's last close as a final fallback.
  const curPrice = c.current_price != null ? c.current_price : c.last_price;
  const priceTxt = fmtUsdShort(curPrice);
  const d = c.poc || {};
  const mig = d.migration;
  let migBadge = '';
  if (mig){
    // Coerce delta_pct to a finite number; bad/missing data shows "?" instead
    // of literally rendering "+null%" or any string the API might inject.
    const dlt = Number(mig.delta_pct);
    const dltTxt = isFinite(dlt) ? ((dlt >= 0 ? '+' : '') + dlt.toFixed(2) + '%') : '?';
    const cfg = mig.direction === 'UP'
      ? {bg:'#22c55e22', fg:'#22c55e', arrow:'↑', label:`Migrating UP ${dltTxt}`}
      : mig.direction === 'DOWN'
      ? {bg:'#ef444422', fg:'#ef4444', arrow:'↓', label:`Migrating DOWN ${dltTxt}`}
      : {bg:'#6b728022', fg:'var(--muted)', arrow:'·', label:'Value stable'};
    migBadge = `<span style="background:${cfg.bg};color:${cfg.fg};padding:3px 8px;border-radius:4px;font-size:12px;font-weight:600">${cfg.arrow} ${cfg.label}</span>`;
  }
  const POC_TOP_TFS = [['d30','30d'],['d90','90d'],['d180','180d']];
  const ladder = POC_TOP_TFS.map(([k, label]) => {
    const r = d[k];
    if (!r) return `<tr><td style="color:var(--muted);padding:5px 8px">${label}</td><td colspan="3" style="color:var(--muted);padding:5px 8px">—</td></tr>`;
    const inVA = r.in_value_area;
    const tag = inVA
      ? '<span style="background:#22c55e22;color:#22c55e;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600">IN VA</span>'
      : '<span style="background:#f59e0b22;color:#f59e0b;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600">OUT</span>';
    const dc = r.distance_pct == null ? 'var(--muted)' : (r.distance_pct >= 0 ? '#22c55e' : '#ef4444');
    const dt = r.distance_pct == null ? '—' : (r.distance_pct >= 0 ? '+' : '') + r.distance_pct.toFixed(1) + '%';
    return `<tr>
      <td style="color:var(--muted);padding:5px 8px">${label}</td>
      <td style="font-weight:600;padding:5px 8px">${fmtUsdShort(r.poc)}</td>
      <td style="color:${dc};text-align:right;padding:5px 8px;font-weight:600">${dt}</td>
      <td style="text-align:right;padding:5px 8px">${tag}</td>
    </tr>`;
  }).join('');
  const nakedArr = Array.isArray(d.naked) ? d.naked.slice(0, 5) : [];
  const anchor = d.d90 || d.d30 || d.d180;
  const cur = curPrice != null ? curPrice : (anchor && anchor.current);
  const nakedHtml = nakedArr.length ? `
    <div>
      <div class="sub" style="font-size:11px;color:var(--muted);margin-bottom:4px">Naked POCs · untested magnet levels (last 180d)</div>
      ${nakedArr.map(n => {
        const isSupport = cur != null && cur > n.poc;
        const col = isSupport ? '#22c55e' : '#ef4444';
        // Coerce both to finite numbers so a stringy or null upstream value
        // can't reflect into the DOM unescaped.
        const dp = Number(n.distance_pct);
        const dpTxt = isFinite(dp) ? ((dp >= 0 ? '+' : '') + dp.toFixed(2) + '%') : '—';
        const days = Number(n.days_ago);
        const daysTxt = isFinite(days) ? (days.toFixed(0) + 'd ago · ') : '';
        return `<div style="display:flex;justify-content:space-between;font-size:13px;padding:3px 0;border-bottom:1px solid var(--border)">
          <span style="color:${col};font-weight:600">${fmtUsdShort(n.poc)}</span>
          <span style="color:var(--muted)">${daysTxt}${dpTxt}</span>
        </div>`;
      }).join('')}
    </div>` : '';
  const sparkline = pocMigrationSparkline(d.migration_series);
  // Volume profile chart for the modal (90d primary, 30d overlay as
  // dashed gray). Uses the larger viewBox-scaled variant so it actually
  // reads at modal size. Only renders when buckets are present.
  const volProfile = (d.d90 && d.d90.buckets && d.d90.buckets.length)
    ? volumeProfileSVGLarge(d.d90, d.d30, cur)
    : ((d.d30 && d.d30.buckets && d.d30.buckets.length)
        ? volumeProfileSVGLarge(d.d30, null, cur)
        : '');
  return `<div style="display:flex;flex-direction:column;gap:14px">
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
      ${img}
      <div style="min-width:0;flex:1">
        <div style="font-size:22px;font-weight:700;letter-spacing:0.4px">${sym}</div>
        <div class="sub" style="font-size:12px;color:var(--muted)">${name}</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:11px;color:var(--muted)">Current price</div>
        <div style="font-size:18px;font-weight:700">${priceTxt}</div>
      </div>
      <button class="btn" data-poc-help="1" aria-label="What is POC?" title="What is Point of Control?" style="padding:1px 8px;font-size:11px;font-weight:700;line-height:1.4">?</button>
    </div>
    ${migBadge ? `<div>${migBadge}</div>` : ''}
    ${sparkline ? `<div><div class="sub" style="font-size:11px;color:var(--muted);margin-bottom:4px">30d POC drift · last 90 days</div>${sparkline}</div>` : ''}
    ${volProfile ? `<div class="poc-vol-profile-wrap" data-poc-vol-sym="${sym}">
      <div class="sub" style="display:flex;align-items:center;gap:8px;font-size:11px;color:var(--muted);margin-bottom:4px">
        <span style="flex:1">Volume profile · 90d (30d dashed) · current price marker</span>
        <button class="btn poc-vol-fullscreen-btn" data-poc-vol-fullscreen="1" aria-label="Expand volume profile to fullscreen" title="Expand to fullscreen" style="padding:2px 8px;font-size:11px;font-weight:600;line-height:1.4">⛶ Fullscreen</button>
      </div>
      ${volProfile}
    </div>` : ''}
    <div>
      <div class="sub" style="font-size:11px;color:var(--muted);margin-bottom:4px">Value-area ladder</div>
      <table style="width:100%;font-size:13px;border-collapse:collapse">
        <thead><tr style="color:var(--muted);font-size:10px;text-align:left">
          <th style="padding:5px 8px">Window</th><th style="padding:5px 8px">POC</th><th style="text-align:right;padding:5px 8px">Δ vs price</th><th style="text-align:right;padding:5px 8px">VA</th>
        </tr></thead>
        <tbody>${ladder}</tbody>
      </table>
    </div>
    ${nakedHtml}
  </div>`;
}

function openPocDetail(coinId){
  const list = ((DATA.market || {}).poc_top) || [];
  let c = list.find(r => r && String(r.coin_id || r.symbol || '') === coinId);
  if (!c){
    // Stock POC is embedded on stocks_signals rows (compute_stock_poc, same
    // shape). When clicked from the universal symbol modal we land here with
    // the ticker symbol as `coinId` — fall back to that source.
    const stocks = ((DATA.market || {}).stocks_signals) || [];
    const s = stocks.find(r => r && String(r.symbol || '') === coinId && r.poc);
    if (s) c = s;
  }
  if (!c) return;
  const modal = document.getElementById('pocDetailModal');
  if (!modal) return;
  document.getElementById('pocDetailTitle').textContent =
    `${String(c.symbol || c.coin_id || '').toUpperCase()} · ${c.name || ''} · POC`;
  document.getElementById('pocDetailBody').innerHTML = pocDetailHtml(c);
  modal.classList.remove('hidden');
}
function closePocDetail(){
  const m = document.getElementById('pocDetailModal');
  if (m) m.classList.add('hidden');
}

// Wire up POC-card click + keyboard activation. Mirrors wireStockDetail.
(function wirePocDetail(){
  if (window._pocDetailWired) return; window._pocDetailWired = true;
  document.addEventListener('click', e => {
    const card = e.target.closest && e.target.closest('.poc-card[data-poc-coin-id]');
    if (card){ openPocDetail(card.getAttribute('data-poc-coin-id')); return; }
    if (e.target && e.target.id === 'pocDetailClose') closePocDetail();
    if (e.target && e.target.id === 'pocDetailModal') closePocDetail();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closePocDetail();
    const card = e.target && e.target.closest && e.target.closest('.poc-card[data-poc-coin-id]');
    if (card && (e.key === 'Enter' || e.key === ' ')){
      e.preventDefault();
      openPocDetail(card.getAttribute('data-poc-coin-id'));
    }
  });
})();

// POC volume profile fullscreen overlay — desktop only (mobile reads the
// modal-sized chart fine per user feedback "mobile is perfect"). Clicks on
// the Fullscreen button inside the POC detail modal pop the SVG out into a
// full-viewport overlay. Close on × / click-outside / Escape.
(function wirePocVolFullscreen(){
  if (window._pocVolFullscreenWired) return; window._pocVolFullscreenWired = true;
  const open = (sym, svgHtml) => {
    const modal = document.getElementById('pocVolFullscreen');
    const body  = document.getElementById('pocVolFullscreenBody');
    const title = document.getElementById('pocVolFullscreenTitle');
    if (!modal || !body || !title) return;
    title.textContent = `${sym} · Volume profile`;
    // Clone the SVG and force it to fill the available area. The original
    // viewBox is preserved so axes/labels scale up proportionally.
    body.innerHTML = svgHtml;
    const svg = body.querySelector('svg');
    if (svg){
      svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
      svg.style.width = '100%';
      svg.style.height = '100%';
      svg.style.maxHeight = 'none';
      svg.style.maxWidth = '100%';
    }
    modal.classList.remove('hidden');
  };
  const close = () => {
    const m = document.getElementById('pocVolFullscreen');
    if (m) m.classList.add('hidden');
  };
  document.addEventListener('click', e => {
    const btn = e.target && e.target.closest && e.target.closest('[data-poc-vol-fullscreen]');
    if (btn){
      const wrap = btn.closest('.poc-vol-profile-wrap');
      const sym  = wrap ? wrap.getAttribute('data-poc-vol-sym') : '';
      const svg  = wrap ? wrap.querySelector('svg') : null;
      if (svg) open(sym, svg.outerHTML);
      return;
    }
    if (e.target && e.target.id === 'pocVolFullscreenClose') close();
    if (e.target && e.target.id === 'pocVolFullscreen') close();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });
})();

// Wire up News Sentiment detail modal — close on × / click-outside / Escape.
(function wireNewsSentimentDetail(){
  if (window._newsSentimentDetailWired) return; window._newsSentimentDetailWired = true;
  document.addEventListener('click', e => {
    if (e.target && e.target.id === 'newsSentimentDetailClose') closeNewsSentimentDetail();
    if (e.target && e.target.id === 'newsSentimentDetailModal') closeNewsSentimentDetail();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeNewsSentimentDetail();
  });
})();

// ===== CryptoCompare social + dev stats =====
function renderCCSocialCards(){
  const cc = (socialData().cryptocompare || {}).coins || {};
  const host = document.getElementById('ccSocialCards');
  if (!host) return;
  host.innerHTML = RESEARCH_ASSETS.map(a => {
    const c = cc[a];
    const accent = RESEARCH_ACCENT(a);
    if (!c){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">${a.toUpperCase()}</h3><div class="sub" style="color:var(--muted);margin-top:8px">No data available.</div></div>`;
    }
    return `<div class="card" style="border-left:4px solid ${accent}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">${a.toUpperCase()}</h3>
        <span class="sub" style="color:var(--muted);font-size:11px">${ASSET_FULLNAME[a]}</span>
      </div>
      <table style="margin-top:8px;font-size:11px;width:100%">
        <tbody>
          <tr><td class="sub" style="color:var(--muted)">Twitter followers</td><td class="right"><strong>${fmtNumShort(c.twitter_followers)}</strong></td></tr>
          <tr><td class="sub" style="color:var(--muted)">Reddit subs</td><td class="right"><strong>${fmtNumShort(c.reddit_subscribers)}</strong></td></tr>
          <tr><td class="sub" style="color:var(--muted)">Reddit active</td><td class="right">${fmtNumShort(c.reddit_active_users)}</td></tr>
          <tr><td class="sub" style="color:var(--muted)">GitHub stars</td><td class="right"><strong>${fmtNumShort(c.github_stars)}</strong></td></tr>
          <tr><td class="sub" style="color:var(--muted)">GitHub forks</td><td class="right">${fmtNumShort(c.github_forks)}</td></tr>
          <tr><td class="sub" style="color:var(--muted)">Open PRs / issues</td><td class="right">${fmtNumShort(c.github_open_pulls)} / ${fmtNumShort(c.github_open_issues)}</td></tr>
        </tbody>
      </table>
    </div>`;
  }).join('');
}

// ===== Reddit per-subreddit cards =====
function renderRedditCards(){
  const subs = (socialData().reddit || {}).subreddits || {};
  const host = document.getElementById('redditCards');
  if (!host) return;
  const order = ['cryptocurrency','cryptomarkets','bitcoin','ethereum','solana','cardano','chainlink','litecoin','defi'];
  const labelAccent = {bitcoin:'#f7931a', ethereum:'#627eea', chainlink:'#2a5ada', litecoin:'#bfbbbb', cryptocurrency:'#a78bfa', cryptomarkets:'#8b5cf6', solana:'#14f195', cardano:'#0033ad', defi:'#22c55e'};
  host.innerHTML = order.map(name => {
    const s = subs[name];
    const accent = labelAccent[name] || '#a78bfa';
    if (!s || !s.ok){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">/r/${s?.sub || name}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no Reddit data</div></div>`;
    }
    const posts = (s.top_posts || []).slice(0, 3).map(p => `
      <a href="${sanitizeUrl(p.url)}" target="_blank" rel="noopener" style="display:block;font-size:11px;color:var(--text);text-decoration:none;padding:4px 0;border-top:1px solid var(--border)">
        <span style="color:var(--muted)">▲ ${fmtNumShort(p.score)} · 💬 ${fmtNumShort(p.comments)}</span>
        <span style="display:block;color:var(--text);line-height:1.3">${(p.title||'').replace(/</g,'&lt;')}</span>
      </a>
    `).join('') || '<div class="sub" style="color:var(--muted);font-size:11px;padding:6px 0">No top posts.</div>';
    const trending = (s.trending || []).slice(0, 3).map(p => `
      <a href="${sanitizeUrl(p.url)}" target="_blank" rel="noopener" style="display:block;font-size:11px;color:var(--text);text-decoration:none;padding:3px 0">
        <span style="color:#f59e0b">🔥 ${fmtNumShort(p.score)}</span>
        <span style="color:var(--muted)"> · 💬 ${fmtNumShort(p.comments)}</span>
        <span style="display:block;color:var(--text);line-height:1.3">${(p.title||'').replace(/</g,'&lt;')}</span>
      </a>
    `).join('');
    const trendingBlock = trending
      ? `<div style="margin-top:8px;padding-top:6px;border-top:1px dashed var(--border)"><div class="sub" style="font-size:10px;color:var(--muted);margin-bottom:2px">🔥 Trending now</div>${trending}</div>`
      : '';
    const sent = s.sentiment || {label:'neutral', score:0, n:0};
    const sentBg = sent.label==='bullish' ? '#16331f' : sent.label==='bearish' ? '#3a1414' : '#27272a';
    const sentFg = sent.label==='bullish' ? '#22c55e' : sent.label==='bearish' ? '#ef4444' : '#a1a1aa';
    const sentPill = sent.n
      ? `<div style="margin-top:6px"><span style="display:inline-block;padding:2px 8px;border-radius:999px;font-size:10px;font-weight:600;background:${sentBg};color:${sentFg}">${sent.label} ${sent.score>=0?'+':''}${sent.score}</span></div>`
      : '';
    return `<div class="card" style="border-left:4px solid ${accent}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">/r/${s.sub}</h3>
        <span class="sub" style="color:var(--muted);font-size:11px">${s.label || ''}</span>
      </div>
      <div style="display:flex;gap:14px;margin-top:8px">
        <div>
          <div class="sub" style="font-size:10px;color:var(--muted)">Subscribers</div>
          <div class="v" style="font-size:18px;font-weight:700">${fmtNumShort(s.subscribers)}</div>
        </div>
        <div>
          <div class="sub" style="font-size:10px;color:var(--muted)">Active now</div>
          <div class="v" style="font-size:18px;font-weight:700">${fmtNumShort(s.active_users)}</div>
        </div>
      </div>
      ${sentPill}
      <div style="margin-top:8px">${posts}</div>
      ${trendingBlock}
    </div>`;
  }).join('');
}

// ===== Santiment on-chain + dev =====
function renderSantimentCards(){
  const coins = (socialData().santiment || {}).coins || {};
  const stale = (socialData().santiment || {}).stale;
  const host = document.getElementById('santimentCards');
  if (!host) return;
  const pct = v => v == null ? '' : `<span style="color:${v>=0?'#22c55e':'#ef4444'};font-weight:600">${v>=0?'+':''}${v.toFixed(1)}%</span>`;
  const stalePill = lag => lag ? `<span class="tag" style="background:#27272a;color:#a1a1aa;font-size:9px">~${lag}d</span>` : '';
  const mvrvTag = v => v == null ? '' :
      v < 1 ? '<span class="tag" style="background:#16331f;color:#22c55e;font-size:9px">undervalued</span>' :
      v > 3 ? '<span class="tag" style="background:#3a1414;color:#ef4444;font-size:9px">overvalued</span>' :
              '<span class="tag" style="background:#27272a;color:#a1a1aa;font-size:9px">normal</span>';
  const flowTag = v => v == null ? '' :
      v > 0 ? '<span class="tag" style="background:#16331f;color:#22c55e;font-size:9px">supply leaving exch</span>' :
              '<span class="tag" style="background:#3a1414;color:#ef4444;font-size:9px">supply hitting exch</span>';
  host.innerHTML = RESEARCH_ASSETS.map(a => {
    const c = coins[a];
    const accent = RESEARCH_ACCENT(a);
    if (!c){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">${a.toUpperCase()}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no Santiment data</div></div>`;
    }
    // Latest values + 7d deltas (computed in the python fetcher for each metric)
    const daaL = c.daily_active_addresses_latest;
    const daaD = c.daily_active_addresses_delta_pct;
    const devL = c.dev_activity_latest;
    const devD = c.dev_activity_delta_pct;
    const aa24L = c.active_addresses_24h_latest;
    const devcL = c.dev_contributors_latest;
    const ngL  = c.network_growth_latest;
    const mvrv = c.mvrv_usd_latest;
    const xout = c.exchange_outflow_latest;
    const xin  = c.exchange_inflow_latest;
    const netFlow = (xout != null && xin != null) ? (xout - xin) : null;
    const row = (label, val, extra, lag) =>
      `<tr><td style="color:var(--muted);font-size:11px">${label}${lag?' ':''}${stalePill(lag)}</td><td class="right" style="font-size:12px;font-variant-numeric:tabular-nums">${val == null ? '—' : (typeof val === 'string' ? val : fmtNumShort(val))} ${extra||''}</td></tr>`;
    return `<div class="card" style="border-left:4px solid ${accent}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">${a.toUpperCase()}</h3>
        <span class="sub" style="color:var(--muted);font-size:11px">${ASSET_FULLNAME[a]}</span>
      </div>
      <table style="margin-top:8px;width:100%"><tbody>
        ${row('DAA',            daaL, pct(daaD))}
        ${row('Active (24h)',   aa24L, '')}
        ${row('Dev activity',   devL, pct(devD))}
        ${row('Devs',           devcL, '')}
        ${row('Net growth',     ngL, '', 35)}
        ${row('MVRV',           mvrv == null ? null : mvrv.toFixed(2), mvrvTag(mvrv), 35)}
        ${row('Net exch flow',  netFlow, flowTag(netFlow), 35)}
      </tbody></table>
      ${stale ? '<div class="sub" style="font-size:10px;color:var(--muted);margin-top:6px">cached (daily-gated)</div>' : ''}
    </div>`;
  }).join('');
}

// Keyword lists for headline sentiment scoring (same approach the Python
// `_AI_NEWS_*` lists use server-side for the AI tab). Lowercased; matched
// substring-wise against title+body. POSITIVE iff ≥1 positive hit and 0
// negative hits, NEGATIVE iff the reverse, otherwise NEUTRAL.
const _NEWS_POS_KEYWORDS = [
  'rally', 'surge', 'soars', 'soar', 'jumps', 'jump', 'gains', 'gain',
  'breakout', 'breakthrough', 'launches', 'launch', 'partnership', 'adopts',
  'adoption', 'approves', 'approved', 'approval', 'wins', 'win', 'milestone',
  'record', 'all-time high', 'ath', 'bullish', 'rally', 'upgrade', 'upgraded',
  'beats', 'inflows', 'inflow', 'buys', 'accumulate', 'accumulation',
  'recovery', 'rebounds', 'rebound', 'outperform', 'green', 'institutional',
  'etf approval'
];
const _NEWS_NEG_KEYWORDS = [
  'hack', 'hacked', 'exploit', 'exploited', 'lawsuit', 'sued', 'sec ', 'fine',
  'crash', 'plunge', 'plunges', 'dump', 'dumps', 'tumbles', 'tumble', 'sinks',
  'sink', 'slide', 'slides', 'falls', 'fall', 'loses', 'loss', 'losses',
  'fraud', 'investigation', 'probe', 'ban', 'banned', 'banning', 'breach',
  'leak', 'leaked', 'outage', 'down', 'bearish', 'liquidation', 'liquidated',
  'rejected', 'rejection', 'denied', 'sell-off', 'selloff', 'crashes',
  'crackdown', 'sanction', 'sanctioned', 'rug', 'scam', 'theft', 'stolen',
  'delisting', 'delisted', 'outflows', 'outflow', 'warning', 'warns'
];

// Score a single news item by keyword presence. Mirrors `compute_ai_sentiment`
// in fetch_market.py so the Research tab uses the same POS/NEG/NEU contract
// as the AI News tab.
function scoreNewsItemSentiment(item){
  const title = (item && item.title) || '';
  const body  = (item && item.body)  || '';
  const text = (title + ' ' + (body || '')).toLowerCase();
  let hasPos = false, hasNeg = false;
  for (let i = 0; i < _NEWS_POS_KEYWORDS.length; i++){
    if (text.indexOf(_NEWS_POS_KEYWORDS[i]) !== -1){ hasPos = true; break; }
  }
  for (let i = 0; i < _NEWS_NEG_KEYWORDS.length; i++){
    if (text.indexOf(_NEWS_NEG_KEYWORDS[i]) !== -1){ hasNeg = true; break; }
  }
  if (hasPos && !hasNeg) return 'POSITIVE';
  if (hasNeg && !hasPos) return 'NEGATIVE';
  return 'NEUTRAL';
}

// Per-symbol alias map: headlines often refer to coins by an issuer / project
// name rather than by ticker or coin name (e.g. "Ripple" for XRP, "Binance"
// for BNB, "TON" for Toncoin). Without these, the matcher misses obvious
// mentions and ~18 of the top 25 coins score zero on a typical pull. Keep
// the alias list conservative — anything ≤3 chars or English-common (e.g.
// "Ton" as a unit, "Dai" as a name) gets word-boundary-matched, so a stray
// substring won't collide. Symbols already used as the ticker are not
// repeated here.
//
// NB: Keep aliases in ASCII; the matcher lowercases inputs before testing.
const _NEWS_COIN_ALIASES = {
  // major issuer/project/full names that don't equal the `coin.name`
  XRP:  ['Ripple', 'RippleNet', 'RippleLabs', 'Ripple Labs'],
  BNB:  ['Binance Coin', 'BinanceCoin'],
  USDT: ['Tether USD', 'Tether USDT'],
  USDC: ['USD Coin', 'Circle USDC'],
  TRX:  ['Tron'],                   // case-insensitive; word-boundary
  DOGE: ['Doge'],
  ADA:  ['Cardano ADA'],
  BCH:  ['BCH'],                    // the symbol IS the popular term
  LTC:  ['Litecoin LTC'],
  XLM:  ['Stellar Lumens', 'Lumens'],
  XMR:  ['Monero XMR'],
  TON:  ['The Open Network', 'Open Network'],
  ZEC:  ['Zcash ZEC'],
  HYPE: ['Hyperliquid HYPE'],
  LINK: ['Chainlink LINK'],
  DAI:  ['MakerDAO DAI', 'DAI stablecoin'],
  LEO:  ['Bitfinex LEO', 'LEO token'],
};

// Build per-coin aggregations from market.news + markets_top. News items have
// no `symbols`/`assets` field (see fetch_market.crypto_news_rss), so we
// case-insensitive substring-match the headline+body against each coin's
// symbol and name (word-boundary for short symbols to avoid 'BTC' matching
// 'BTCM' or 'BTC' inside 'BTCST', and to avoid 'ETH' matching 'ETHEREUM' —
// wait, we DO want to match Ethereum: we match the long name separately).
//
// Returns: [{ symbol, name, total, positive, negative, neutral, net_score,
//             recent: [{title, sentiment, url}] }, …] sorted by total desc,
//          then by net_score desc, capped at `topN`.
function groupNewsBySymbol(news, marketsTop, topN, ccByCoin){
  const coins = (marketsTop || []).slice(0, topN || 25);
  // CC per-coin sentiment (backend-aggregated; see fetch_cc_per_coin_news in
  // fetch_market.py) lets us score coins that aren't named in the 5 RSS
  // feeds we pull. We merge CC counts on TOP of RSS counts below so the
  // row total reflects the union. Empty/missing payload is fine — we
  // degrade to RSS-only.
  const cc = (ccByCoin && typeof ccByCoin === 'object') ? ccByCoin : {};
  const safeNews = (news && news.length) ? news : [];
  if (!coins.length) return [];
  if (!safeNews.length && !Object.keys(cc).length) return [];
  // Build per-coin matcher regexes once. Symbol requires word-boundary so
  // 'BTC' doesn't match the middle of unrelated tickers; '$SYM' cashtags
  // are accepted. Name is a case-insensitive substring — coin names are
  // long enough that false-positives are rare. Aliases (per
  // _NEWS_COIN_ALIASES) are folded into a single combined regex per coin so
  // we still get one pass per news item per coin.
  const escapeRe = s => String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const matchers = coins.map(c => {
    const sym = (c.symbol || '').toUpperCase();
    const name = c.name || '';
    const symRe  = sym  ? new RegExp('(?:^|[^a-z0-9])\\$?' + escapeRe(sym) + '(?:[^a-z0-9]|$)', 'i') : null;
    // Combine the coin name + any aliases into one alternation regex so we
    // don't pay N regex tests per news item per coin. Empty/duplicate
    // entries are dropped; everything is word-boundary-anchored to avoid
    // 'Ton' matching 'tonight' or 'Ripple' matching 'crippled'.
    const nameForms = [];
    if (name) nameForms.push(name);
    const aliases = _NEWS_COIN_ALIASES[sym] || [];
    for (let i = 0; i < aliases.length; i++){
      const a = aliases[i];
      if (a && nameForms.indexOf(a) === -1) nameForms.push(a);
    }
    // Sort longest-first so 'Bitcoin Cash' wins the regex race over 'Bitcoin'
    // inside the same alternation (regex engines prefer the leftmost match
    // among alternatives at the same position).
    nameForms.sort((a, b) => b.length - a.length);
    const nameAlt = nameForms.map(escapeRe).join('|');
    const nameRe = nameAlt ? new RegExp('(?:^|[^a-z0-9])(?:' + nameAlt + ')(?:[^a-z0-9]|$)', 'i') : null;
    return { symbol: sym, name: name, symRe, nameRe };
  });
  // Pre-score each news item once (avoids re-running the keyword loop per coin).
  const scored = safeNews.map(n => ({
    item: n,
    text: ((n && n.title) || '') + ' ' + ((n && n.body) || ''),
    sentiment: scoreNewsItemSentiment(n),
  }));
  const out = matchers.map(m => {
    let pos = 0, neg = 0, neu = 0;
    const recent = [];  // top 3 for the compact-row hover tooltip
    const allItems = []; // every matched item, for the click-to-expand modal
    for (let i = 0; i < scored.length; i++){
      const s = scored[i];
      const hit = (m.symRe && m.symRe.test(s.text)) || (m.nameRe && m.nameRe.test(s.text));
      if (!hit) continue;
      if (s.sentiment === 'POSITIVE') pos++;
      else if (s.sentiment === 'NEGATIVE') neg++;
      else neu++;
      const entry = {
        title:     (s.item && s.item.title) || '',
        body:      (s.item && s.item.body) || '',
        source:    (s.item && s.item.source) || '',
        date:      (s.item && s.item.date) || '',
        sentiment: s.sentiment,
        url:       (s.item && s.item.url) || '',
      };
      allItems.push(entry);
      if (recent.length < 3) recent.push(entry);
    }
    // Merge CC backend-aggregated counts (already scored server-side using
    // the same POS/NEG keyword lists as scoreNewsItemSentiment above). CC
    // items are appended to allItems so the click-to-expand modal shows
    // both RSS and CC headlines; `source` is tagged 'CC: <publisher>' so
    // the user can tell where each row came from.
    const ccRow = cc[m.symbol];
    if (ccRow){
      pos += (ccRow.positive || 0);
      neg += (ccRow.negative || 0);
      neu += (ccRow.neutral  || 0);
      const ccRecent = (ccRow.recent || []);
      for (let j = 0; j < ccRecent.length; j++){
        const r = ccRecent[j] || {};
        const entry = {
          title:     r.title  || '',
          body:      r.body   || '',
          source:    r.source ? ('CC: ' + r.source) : 'CryptoCompare',
          date:      r.date   || '',
          sentiment: r.sentiment || 'NEUTRAL',
          url:       r.url    || '',
        };
        allItems.push(entry);
        if (recent.length < 3) recent.push(entry);
      }
    }
    return {
      symbol: m.symbol,
      name: m.name,
      total: pos + neg + neu,
      positive: pos,
      negative: neg,
      neutral: neu,
      net_score: pos - neg,
      recent,
      allItems,
    };
  });
  // Sort by total mentions desc, then net_score desc as tiebreak; keep all
  // top-N coins in the list so users see zero-mention rows too (turns out
  // to be useful signal: 'no news this week' is itself meaningful).
  out.sort((a, b) => (b.total - a.total) || (b.net_score - a.net_score));
  return out;
}

// Stash the most recent groupNewsBySymbol output indexed by symbol so the
// detail modal (openNewsSentimentDetail) can look up matched items without
// recomputing the regex pass on click.
window._top25NewsCache = window._top25NewsCache || {};

function openNewsSentimentDetail(symbol){
  const upper = (symbol || '').toUpperCase();
  const lower = upper.toLowerCase();
  const row = (window._top25NewsCache || {})[upper];
  const modal = document.getElementById('newsSentimentDetailModal');
  const body  = document.getElementById('newsSentimentDetailBody');
  const title = document.getElementById('newsSentimentDetailTitle');
  if (!modal || !body || !title) return;
  // CryptoCompare deep data — only populated for RESEARCH_ASSETS (btc/eth/link/ltc).
  // When available we surface the richer view (7d trend, keyword chips, top
  // articles) above the RSS-matched headlines list.
  const ccCoin = ((socialData().cc_news || {}).coins || {})[lower] || null;
  if (!row && !ccCoin){
    title.textContent = upper + ' · news sentiment';
    body.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No matched headlines for this coin.</div>';
    modal.classList.remove('hidden');
    return;
  }
  // Prefer row metadata; fall back to CC if row missing (shouldn't happen for
  // top-25 but defensive).
  const sym = (row && row.symbol) || upper;
  const nm  = (row && row.name) || '';
  title.textContent = `${sym}${nm ? ' · ' + nm : ''} · news sentiment`;
  const rsv = row || {total:0, positive:0, negative:0, neutral:0, net_score:0, allItems:[]};
  const total = rsv.total || 1;
  const posPct = (rsv.positive / total) * 100;
  const neuPct = (rsv.neutral  / total) * 100;
  const negPct = (rsv.negative / total) * 100;
  const netColor = rsv.net_score > 0 ? '#22c55e' : rsv.net_score < 0 ? '#ef4444' : 'var(--muted)';
  const netTxt = rsv.total === 0 ? '—' : (rsv.net_score > 0 ? '+' : '') + rsv.net_score;

  // --- Optional CryptoCompare deep section (only for the 4 covered coins) ---
  let ccBlock = '';
  if (ccCoin){
    const ccTotal = (ccCoin.positive || 0) + (ccCoin.negative || 0) + (ccCoin.neutral || 0) || 1;
    const ccPosPct = (ccCoin.positive || 0) / ccTotal * 100;
    const ccNeuPct = (ccCoin.neutral  || 0) / ccTotal * 100;
    const ccNegPct = (ccCoin.negative || 0) / ccTotal * 100;
    const ccNetColor = ccCoin.net_score == null ? 'var(--muted)' : ccCoin.net_score > 0 ? '#22c55e' : ccCoin.net_score < 0 ? '#ef4444' : '#f59e0b';
    const ccNetTxt = (ccCoin.net_score > 0 ? '+' : '') + (ccCoin.net_score ?? 0);
    // 7-day daily-net sparkline (inline SVG bar chart of daily net sentiment).
    const trend = ccCoin.trend_7d || [];
    let sparkBlock = '';
    if (trend.length){
      const maxAbs = Math.max(1, ...trend.map(d => Math.abs(d.net || 0)));
      const sparkW = 260, sparkH = 36, barW = sparkW / trend.length;
      const bars = trend.map((d, i) => {
        const h = Math.max(1, Math.round((Math.abs(d.net) / maxAbs) * (sparkH/2 - 1)));
        const y = d.net >= 0 ? (sparkH/2 - h) : (sparkH/2);
        const fill = d.net > 0 ? '#22c55e' : (d.net < 0 ? '#ef4444' : '#6b7280');
        return `<rect x="${i*barW}" y="${y}" width="${Math.max(1,barW-1)}" height="${h}" fill="${fill}"><title>${escapeHtml(d.date||'')}: net ${d.net} (+${d.pos||0}/−${d.neg||0})</title></rect>`;
      }).join('');
      sparkBlock = `<div style="margin-top:10px;display:flex;align-items:center;gap:8px">
        <span class="sub" style="font-size:11px;color:var(--muted);min-width:30px">7d:</span>
        <svg width="${sparkW}" height="${sparkH}" viewBox="0 0 ${sparkW} ${sparkH}">
          <line x1="0" y1="${sparkH/2}" x2="${sparkW}" y2="${sparkH/2}" stroke="#374151" stroke-width="1"/>${bars}
        </svg>
      </div>`;
    }
    // Keyword chips (visual only — no filter logic in the modal).
    const skewColor = sk => sk == null ? '#6b7280' : sk > 0.3 ? '#22c55e' : sk < -0.3 ? '#ef4444' : '#a1a1aa';
    const chips = (ccCoin.top_keywords || []).slice(0, 10).map(k => {
      const bg = skewColor(k.sentiment_skew);
      return `<span style="border:1px solid ${bg};color:${bg};border-radius:10px;padding:2px 8px;margin:2px 4px 0 0;font-size:11px;display:inline-block">${escapeHtml(k.kw)} <span style="opacity:.65">${k.count}</span></span>`;
    }).join('');
    const chipsBlock = chips ? `<div style="margin-top:10px;line-height:1.8">${chips}</div>` : '';
    // Top articles from CC (different from RSS-matched — these are CC's curated picks).
    // Neutral = muted grey (not amber). Amber = caution per the palette
    // spec; using it for NEUTRAL made neutral headlines look like warnings.
    const SENT_COLOR = {POSITIVE: '#22c55e', NEGATIVE: '#ef4444', NEUTRAL: '#94a3b8'};
    const articles = (ccCoin.top_articles || []).slice(0, 6).map(art => {
      const sc = SENT_COLOR[art.sentiment] || 'var(--muted)';
      const dot = `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${sc};margin-right:6px;vertical-align:middle"></span>`;
      return `<a href="${sanitizeUrl(art.url)}" target="_blank" rel="noopener" style="display:block;padding:6px 0;font-size:12px;color:var(--text);text-decoration:none;border-top:1px solid var(--border);line-height:1.35">
        ${dot}<strong style="color:${sc}">${escapeHtml((art.sentiment||'?').slice(0,3))}</strong>
        <span style="color:var(--muted)"> · ${escapeHtml((art.source||'').slice(0,24))}</span>
        <div style="color:var(--text);margin-top:2px">${escapeHtml(art.title || '')}</div>
      </a>`;
    }).join('');
    const articlesBlock = articles
      ? `<div style="margin-top:12px">
          <div style="font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.06em;margin-bottom:4px">CRYPTOCOMPARE TOP ARTICLES</div>
          ${articles}
        </div>`
      : '';
    ccBlock = `
      <div style="background:#0e1118;border:1px solid var(--border);border-radius:6px;padding:12px;margin-bottom:14px">
        <div style="display:flex;align-items:baseline;justify-content:space-between;gap:10px;flex-wrap:wrap">
          <div style="font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.06em">CRYPTOCOMPARE · DEEP COVERAGE</div>
          <div style="font-size:11px;color:${ccNetColor};font-weight:700">net ${ccNetTxt} · ${ccCoin.article_count || 0} articles</div>
        </div>
        <div style="display:flex;height:10px;margin-top:8px;border-radius:3px;overflow:hidden;background:#1f2533">
          <div style="background:#22c55e;width:${ccPosPct}%"></div>
          <div style="background:#f59e0b;width:${ccNeuPct}%"></div>
          <div style="background:#ef4444;width:${ccNegPct}%"></div>
        </div>
        <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">
          <span style="color:#22c55e">${ccCoin.positive || 0} +</span>
          <span>${ccCoin.neutral || 0} ◯</span>
          <span style="color:#ef4444">${ccCoin.negative || 0} −</span>
        </div>
        ${sparkBlock}
        ${chipsBlock}
        ${articlesBlock}
      </div>`;
  }

  // --- RSS-matched headlines (always present for top-25 rows) ---
  const items = (rsv.allItems || []).slice().sort((a, b) => (b.date || '').localeCompare(a.date || ''));
  const itemsHtml = items.length
    ? items.map(n => {
        const col = n.sentiment === 'POSITIVE' ? '#22c55e' : n.sentiment === 'NEGATIVE' ? '#ef4444' : '#f59e0b';
        const dot = `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${col};margin-right:6px;vertical-align:middle"></span>`;
        return `<a href="${sanitizeUrl(n.url)}" target="_blank" rel="noopener" style="display:block;padding:8px 10px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text)">
          <div style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted);margin-bottom:3px">
            ${dot}<span style="color:#a78bfa;font-weight:600">${escapeHtml(n.source || '')}</span>
            <span>· ${escapeHtml(n.date || '')}</span>
            <span style="color:${col};font-weight:600;margin-left:auto">${escapeHtml((n.sentiment || '').slice(0,3))}</span>
          </div>
          <div style="font-size:13px;line-height:1.35">${escapeHtml(n.title || '')}</div>
        </a>`;
      }).join('')
    : '<div class="sub" style="color:var(--muted);padding:14px">No RSS-matched headlines for this coin in the current window.</div>';
  body.innerHTML = `
    <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px">
      <div>
        <div style="font-size:13px;color:var(--muted)">${rsv.total} mention${rsv.total === 1 ? '' : 's'} across recent headlines (RSS feeds)</div>
      </div>
      <div style="text-align:right">
        <div style="font-size:28px;font-weight:700;line-height:1;color:${netColor}">${netTxt}</div>
        <div style="font-size:11px;color:var(--muted)">net score</div>
      </div>
    </div>
    <div style="display:flex;height:12px;border-radius:4px;overflow:hidden;background:#1f2533;margin-bottom:4px">
      <div style="background:#22c55e;width:${posPct}%"></div>
      <div style="background:#f59e0b;width:${neuPct}%"></div>
      <div style="background:#ef4444;width:${negPct}%"></div>
    </div>
    <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-bottom:14px">
      <span style="color:#22c55e">${rsv.positive} positive</span>
      <span>${rsv.neutral} neutral</span>
      <span style="color:#ef4444">${rsv.negative} negative</span>
    </div>
    ${ccBlock}
    <div style="font-size:11px;color:var(--muted);font-weight:700;letter-spacing:.06em;margin-bottom:6px">MATCHED RSS HEADLINES</div>
    <div>${itemsHtml}</div>`;
  modal.classList.remove('hidden');
}
function closeNewsSentimentDetail(){
  const m = document.getElementById('newsSentimentDetailModal');
  if (m) m.classList.add('hidden');
}

function renderTopNewsSentiment(){
  const host = document.getElementById('topNewsSentimentCards');
  if (!host) return;
  const news = ((DATA.market || {}).news) || [];
  const marketsTop = ((DATA.market || {}).markets_top) || [];
  // CC backend-aggregated per-coin counts. Keyed by uppercase symbol; lifts
  // coverage for coins not named in our 5 RSS feeds (FIGR_HELOC, USDS, LEO,
  // XMR, TON, XLM, DAI, etc.). Missing/empty payload → RSS-only behavior.
  const ccByCoin = (((DATA.market || {}).news_sentiment_by_coin) || {}).coins || {};
  if (!marketsTop.length || (!news.length && !Object.keys(ccByCoin).length)){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No news headlines reference top-25 coins in the current window.</div>';
    return;
  }
  const rows = groupNewsBySymbol(news, marketsTop, 25, ccByCoin);
  // Stash by symbol so click-to-expand can read matched items without
  // recomputing the regex pass.
  window._top25NewsCache = {};
  rows.forEach(r => { if (r.symbol) window._top25NewsCache[r.symbol] = r; });
  const anyMentions = rows.some(r => r.total > 0);
  if (!anyMentions){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No news headlines reference top-25 coins in the current window.</div>';
    return;
  }
  host.innerHTML = rows.map(r => {
    const total = r.total || 1;  // avoid div-by-zero in width math
    const posPct = (r.positive / total) * 100;
    const neuPct = (r.neutral  / total) * 100;
    const negPct = (r.negative / total) * 100;
    const netColor = r.net_score > 0 ? '#22c55e'
                    : r.net_score < 0 ? '#ef4444'
                    : 'var(--muted)';
    const netLbl = r.total === 0 ? '—'
                  : (r.net_score > 0 ? '+' : '') + r.net_score;
    const barInner = r.total === 0
      ? `<div style="background:#1f2533;width:100%;height:100%"></div>`
      : `<div style="background:#22c55e;width:${posPct}%" title="${r.positive} positive"></div>
         <div style="background:#f59e0b;width:${neuPct}%" title="${r.neutral} neutral"></div>
         <div style="background:#ef4444;width:${negPct}%" title="${r.negative} negative"></div>`;
    const titleAttr = r.recent
      .map(rc => `${rc.sentiment[0]} · ${(rc.title || '').replace(/"/g, '”').slice(0, 100)}`)
      .join('\n');
    return `<div class="top-news-sentiment-row" data-tns-symbol="${escapeHtml(r.symbol)}" style="cursor:pointer" title="${escapeHtml(titleAttr || (r.symbol + ': no headline matches'))}">
      <div>
        <div class="tns-sym">${escapeHtml(r.symbol)}</div>
        <div class="tns-name">${escapeHtml(r.name)}</div>
      </div>
      <div style="min-width:0">
        <div class="tns-bar">${barInner}</div>
        <div class="tns-stats">
          <span>${r.total} mention${r.total === 1 ? '' : 's'}</span>
          <span style="color:#22c55e">${r.positive} +</span>
          <span>${r.neutral} ○</span>
          <span style="color:#ef4444">${r.negative} −</span>
        </div>
      </div>
      <div class="tns-net" style="color:${netColor}">net ${netLbl}</div>
    </div>`;
  }).join('');
  // Click any row → open the detail modal for that coin. Delegated so
  // re-renders don't need to re-bind.
  host.querySelectorAll('[data-tns-symbol]').forEach(el =>
    el.addEventListener('click', () => openNewsSentimentDetail(el.getAttribute('data-tns-symbol')))
  );
}

function renderResearchNews(){
  const host = document.getElementById('researchNewsHost');
  if (!host) return;
  const news = ((DATA.market || {}).news) || [];
  if (!news.length) {
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No data available.</div>';
    return;
  }
  const sorted = news.slice().sort((a, b) => {
    const da = a && a.date ? Date.parse(a.date) : 0;
    const db = b && b.date ? Date.parse(b.date) : 0;
    return (db || 0) - (da || 0);
  });
  host.innerHTML = sorted.slice(0, 15).map(n =>
    `<a href="${sanitizeUrl(n.url)}" target="_blank" rel="noopener" style="display:block;padding:8px 10px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text)">
      <div style="font-weight:600;font-size:13px">${escapeHtml(n.title || '')}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:2px">${escapeHtml(n.source || '')} · ${escapeHtml(n.date || '')}</div>
    </a>`
  ).join('');
}

function renderSocial(){
  const social = socialData();
  const poc = (DATA.market||{}).poc || {};
  const hasAny =
    Object.keys((social.cryptocompare||{}).coins||{}).length ||
    Object.keys((social.cc_news||{}).coins||{}).length ||
    Object.keys((social.reddit||{}).subreddits||{}).length ||
    Object.keys((social.santiment||{}).coins||{}).length ||
    Object.keys(poc).length;
  document.getElementById('socialEmpty').classList.toggle('hidden', !!hasAny);
  document.getElementById('socialContent').classList.toggle('hidden', !hasAny);
  const asOf = document.getElementById('socialAsOf');
  if (asOf) asOf.textContent = social.fetched_at ? 'Fetched ' + social.fetched_at : '';
  renderResearchNews();
  // Top-15 news-sentiment card sources data from DATA.market.news +
  // markets_top — independent of the social aggregate (`hasAny`), so render
  // it before the early return so the card still appears when reddit /
  // santiment / cc_news all returned empty.
  renderTopNewsSentiment();
  if (!hasAny) return;
  renderCCSocialCards();
  // renderCCNewsCards removed — the always-on top 4 deep cards (BTC/ETH/LINK/LTC)
  // were folded into the Top-25 click-to-expand modal. Click any row for the
  // detail; CC-covered coins surface the richer 7d trend + chips + curated
  // articles inside the modal alongside the RSS-matched headlines.
  renderRedditCards();
  renderSantimentCards();
}

function renderAll(){
  renderInsights();
  // tag updates — ETF-related tags follow state.etfAsset (decoupled from
  // global asset), Futures-related tags follow state.asset (Futures toggle
  // sets state.asset on click so they always match).
  ['1','2','3','4','FundDetail','Stack','Compare'].forEach(s=>{
    const t = document.getElementById('tagAsset'+s) || document.getElementById('tag'+s);
    if (!t) return;
    t.textContent = etfAsset().toUpperCase();
    t.className = 'tag ' + etfAsset();
  });
  ['Price','Funding','OI','LS','Dvol'].forEach(s=>{
    const t = document.getElementById('tagAsset'+s) || document.getElementById('tag'+s);
    if (!t) return;
    t.textContent = state.asset.toUpperCase();
    t.className = 'tag ' + state.asset;
  });

  // ETF empty check — etfAsset is constrained to btc/eth so we never hit the
  // LINK "no spot ETF" empty state from this tab anymore. (The toggle UI only
  // exposes BTC/ETH buttons.)
  const ed = etfData();
  const etfEmpty = !ed.daily || ed.daily.length === 0;
  const etfEmptyEl = document.getElementById('etfEmpty');
  if (!etfEmptyEl.dataset.original) etfEmptyEl.dataset.original = etfEmptyEl.innerHTML;
  etfEmptyEl.innerHTML = etfEmptyEl.dataset.original;
  // re-bind seed/paste buttons since innerHTML wiped their listeners
  rebindEtfImportButtons();
  etfEmptyEl.classList.toggle('hidden', !etfEmpty);
  document.getElementById('etfContent').classList.toggle('hidden', etfEmpty);

  const td = tradingAssetData();
  const trEmpty = !td.price || td.price.length === 0;
  document.getElementById('tradingEmpty').classList.toggle('hidden', !trEmpty);
  document.getElementById('tradingContent').classList.toggle('hidden', trEmpty);

  // Whale tab has its own BTC/ETH toggle (state.whaleAsset) — independent
  // from the global asset selector. Show the global "no data at all" empty
  // state only when BOTH panels are empty; otherwise let the toggle stay
  // visible and each panel handle its own per-asset empty state inline.
  const wd = whaleData();
  const ethWd = ((DATA.whale||{}).eth) || {};
  const whEmptyEl = document.getElementById('whaleEmpty');
  if (!whEmptyEl.dataset.original) whEmptyEl.dataset.original = whEmptyEl.innerHTML;
  const btcEmpty = !wd.tx_volume_usd || wd.tx_volume_usd.length === 0;
  const ethEmpty = !ethWd.coin_metrics || Object.keys(ethWd.coin_metrics).filter(k => k !== 'fetched_at').length === 0;
  const whEmpty = btcEmpty && ethEmpty;
  whEmptyEl.innerHTML = whEmptyEl.dataset.original;
  // While the whale sidecar is fetching, surface a loading state instead of
  // the static "no data" copy so users on the Whale tab don't think the
  // dashboard is broken during the ~500ms first-load.
  if (whEmpty && state.tab === 'whale' && SIDECAR_STATE.whale === 'loading'){
    whEmptyEl.innerHTML = '<div style="text-align:center;padding:32px;color:var(--muted);font-size:13px">Loading whale data…</div>';
  }
  whEmptyEl.classList.toggle('hidden', !whEmpty);
  document.getElementById('whaleContent').classList.toggle('hidden', whEmpty);

  if (state.tab === 'etf' && !etfEmpty){
    renderEtfFlowSentiment();
    renderEtfKpis(); renderEtfFundTable(); renderFlow(); renderCum(); renderYoy();
    renderFundKpis(); renderFundStack(); renderFundCompare();
  } else if (state.tab === 'etf'){
    // Empty-state safety: hide the sentiment card cleanly if there's no
    // ETF data loaded yet (otherwise it would persist stale numbers).
    renderEtfFlowSentiment();
  }
  if (state.tab === 'trading' && !trEmpty){
    renderFuturesSentiment();
    renderTradingKpis(); renderPriceVol(); renderFunding(); renderOI(); renderLS(); renderCoinbaseIntlPerps(); renderCadliChart(); renderDvol(); renderFng(); renderEthBtc(); renderGlobalTable();
  } else if (state.tab === 'trading'){
    renderFuturesSentiment();
  }
  if (state.tab === 'signals'){
    renderSignals();
  }
  if (state.tab === 'whale' && !whEmpty){
    renderWhalePanel();
  }
  if (state.tab === 'defi'){
    // Show the "Loading DeFi data…" placeholder while the sidecar is in
    // flight, then swap to the real content once it lands. Mirrors the
    // whale-tab loading-state branch above.
    const defiLoading = document.getElementById('defiLoading');
    const defiContent = document.getElementById('defiContent');
    const defiLoadingActive = SIDECAR_STATE.defi === 'loading';
    if (defiLoading) defiLoading.classList.toggle('hidden', !defiLoadingActive);
    if (defiContent) defiContent.classList.toggle('hidden', defiLoadingActive);
    if (!defiLoadingActive) renderDefi();
  }
  if (state.tab === 'trading' && !trEmpty){
    renderNews();
    renderMacro();
  }
  if (state.tab === 'overview'){
    renderOverview();
  }
  if (state.tab === 'social'){
    renderSocial();
  }
  if (state.tab === 'poc'){
    renderPocSentimentIndex();
    renderPocTopCards();
  }
  if (state.tab === 'stocks'){
    renderStocksTab();
  }
  if (state.tab === 'lthcs'){
    renderLthcsTab();
  }
  if (state.tab === 'real_estate'){
    renderRealEstateTab();
  }
  if (state.tab === 'ainews'){
    renderAiNewsTab();
  }
  if (state.tab === 'cpi'){
    renderCpiTab();
  }
  if (state.tab === 'supplies'){
    renderSuppliesTab();
  }
  if (state.tab === 'metals'){
    renderMetalsTab();
  }
  if (state.tab === 'travel'){
    renderTravelTab();
  }
  if (state.tab === 'mufon'){
    renderMufon();
  }
  renderCoverage();
}

function selectTab(t){
  state.tab = t;
  // Kick off lazy load of any sidecar this tab needs. Fire-and-forget —
  // renderAll() below runs immediately with an empty subtree (the
  // tab's empty-state handles that), then re-runs once the fetch lands.
  const _sc = SIDECAR_FOR_TAB[t];
  if (_sc && (SIDECARS||{})[_sc] && SIDECAR_STATE[_sc] !== 'loaded'){
    loadSidecar(_sc).then(loaded => { if (state.tab === t && loaded) renderAll(); });
  }
  // Close any open detail modals when switching tabs — leaving a POC or
  // Stocks modal floating over an unrelated tab is disorienting.
  document.querySelectorAll('.modal-bg').forEach(m => m.classList.add('hidden'));
  // Whale Activity is BTC-only (free on-chain proxies from blockchain.info).
  // Force the asset to BTC so the page renders something useful instead of
  // the "switch to BTC" empty state when the user is on ETH or LINK.
  if (t === 'whale' && state.asset !== 'btc') {
    state.asset = 'btc';
    setActive('asset', 'btc');
  }
  // Futures tab uses its own per-tab selector (state.futuresAsset) but the
  // renderers are tangled with state.asset — push the persisted Futures
  // choice into state.asset whenever the user enters the tab so the page
  // renders the asset they last picked here (not whatever global state.asset
  // happened to be from another tab).
  if (t === 'trading' && state.asset !== state.futuresAsset) {
    state.asset = state.futuresAsset;
    setActive('asset', state.asset);
  }
  // Scroll the newly-active tab into view on the horizontal-scroll tab
  // strip (mobile only — desktop the strip never overflows). Without this
  // "Research" / "Whale Activity" sat off-screen on iPhone widths.
  const _activeTab = document.querySelector(`.tab[data-tab="${t}"]`);
  if (_activeTab && _activeTab.scrollIntoView){
    try { _activeTab.scrollIntoView({inline:'center', block:'nearest', behavior:'smooth'}); }
    catch(_){ _activeTab.scrollIntoView(); }
  }
  document.querySelectorAll('.tab').forEach(el => {
    const isActive = el.dataset.tab === t;
    el.classList.toggle('active', isActive);
    el.setAttribute('aria-selected', isActive ? 'true' : 'false');
    el.classList.toggle('eth',  state.asset === 'eth');
    el.classList.toggle('link', state.asset === 'link');
  });
  document.getElementById('tab-overview').classList.toggle('hidden', t!=='overview');
  document.getElementById('tab-etf').classList.toggle('hidden', t!=='etf');
  document.getElementById('tab-trading').classList.toggle('hidden', t!=='trading');
  document.getElementById('tab-signals').classList.toggle('hidden', t!=='signals');
  document.getElementById('tab-defi').classList.toggle('hidden', t!=='defi');
  document.getElementById('tab-social').classList.toggle('hidden', t!=='social');
  document.getElementById('tab-whale').classList.toggle('hidden', t!=='whale');
  document.getElementById('tab-poc').classList.toggle('hidden', t!=='poc');
  document.getElementById('tab-stocks').classList.toggle('hidden', t!=='stocks');
  document.getElementById('tab-lthcs').classList.toggle('hidden', t!=='lthcs');
  document.getElementById('tab-real_estate').classList.toggle('hidden', t!=='real_estate');
  document.getElementById('tab-ainews').classList.toggle('hidden', t!=='ainews');
  document.getElementById('tab-cpi').classList.toggle('hidden', t!=='cpi');
  document.getElementById('tab-supplies').classList.toggle('hidden', t!=='supplies');
  document.getElementById('tab-metals').classList.toggle('hidden', t!=='metals');
  document.getElementById('tab-travel').classList.toggle('hidden', t!=='travel');
  document.getElementById('tab-mufon').classList.toggle('hidden', t!=='mufon');
  // Period selector now ETF-only. Trading and Whale tabs had it but it was
  // confusing (overlap with Timeframe / Range buttons); their charts are
  // daily by default. ETF Flows still needs Period for the daily/weekly/
  // monthly/yearly resampling toggle on per-fund flow tables and stacks.
  const showPeriod = (t === 'etf');
  document.querySelectorAll('.btn[data-period]').forEach(b => b.style.display = showPeriod ? '' : 'none');
  document.querySelectorAll('.lbl').forEach(b => { if (b.textContent.toUpperCase() === 'PERIOD') b.style.display = showPeriod ? '' : 'none'; });
  // Overview + Social are multi-asset snapshots; hide asset toggle there.
  const isOverview = (t === 'overview');
  const isSocial = (t === 'social');
  // Whale is BTC-only on-chain; hide ETH/LINK so the toggle stays consistent.
  const isWhale = (t === 'whale');
  document.querySelectorAll('.btn[data-asset]').forEach(b => {
    if (isOverview || isSocial) { b.style.display = 'none'; return; }
    if (isWhale && b.dataset.asset !== 'btc') { b.style.display = 'none'; return; }
    b.style.display = '';
  });
  // Range buttons only meaningfully affect ETF / Trading / Whale (which clip
  // their time-series). Signals is a daily snapshot, Markets is a top-25 list,
  // DeFi has its own zoom — none of them respond to Range. Hide elsewhere.
  const usesRange = (t === 'etf' || t === 'trading' || t === 'whale');
  document.querySelectorAll('.btn[data-range]').forEach(b => b.style.display = usesRange ? '' : 'none');
  document.querySelectorAll('.lbl').forEach(b => { if (b.textContent.toUpperCase() === 'TIMEFRAME') b.style.display = usesRange ? '' : 'none'; });
  const insightsBar = document.getElementById('insightsBar');
  // Overview has its own "Top insights" card; AI News tab has its own inline
  // insights card next to the sentiment summary — hide the global bar in
  // both cases to avoid showing the same insights twice.
  if (insightsBar) insightsBar.style.display = (isOverview || t === 'ainews') ? 'none' : '';
  renderAll();
}

// wire buttons
document.querySelectorAll('.btn[data-asset]').forEach(b =>
  b.addEventListener('click', () => {
    state.asset = b.dataset.asset;
    setActive('asset', state.asset);
    // Re-tint the active tab underline to match the new asset.
    document.querySelectorAll('.tab').forEach(el => {
      el.classList.toggle('eth',  state.asset === 'eth');
      el.classList.toggle('link', state.asset === 'link');
    });
    renderAll();
  })
);
document.querySelectorAll('.btn[data-period]').forEach(b =>
  b.addEventListener('click', () => { state.period = b.dataset.period; setActive('period', state.period); renderAll(); })
);
document.querySelectorAll('.btn[data-range]').forEach(b =>
  b.addEventListener('click', () => { state.range = b.dataset.range; setActive('range', state.range); renderAll(); })
);
document.querySelectorAll('.btn[data-fundwin]').forEach(b =>
  b.addEventListener('click', () => { state.fundwin = b.dataset.fundwin; setActive('fundwin', state.fundwin); renderAll(); })
);
document.querySelectorAll('.btn[data-macrorange]').forEach(b =>
  b.addEventListener('click', () => { state.macroRange = b.dataset.macrorange; setActive('macrorange', state.macroRange); renderMacro(); })
);
document.querySelectorAll('.btn[data-cohortbin]').forEach(b =>
  b.addEventListener('click', () => { state.cohortBin = b.dataset.cohortbin; setActive('cohortbin', state.cohortBin); renderWhaleCohortChart(); })
);
// Whale tab BTC/ETH toggle — per-tab asset selector, persisted to localStorage.
document.querySelectorAll('.btn[data-whaleasset]').forEach(b =>
  b.addEventListener('click', () => {
    state.whaleAsset = b.dataset.whaleasset;
    if (typeof localStorage !== 'undefined') localStorage.setItem('whaleAsset', state.whaleAsset);
    setActive('whaleasset', state.whaleAsset);
    renderAll();
  })
);
// Sync the active toggle to the persisted whaleAsset on initial load.
setActive('whaleasset', state.whaleAsset);

// ETF Flows tab BTC/ETH toggle — per-tab asset selector, decoupled from
// state.asset. Persisted to localStorage.
document.querySelectorAll('.btn[data-etfasset]').forEach(b =>
  b.addEventListener('click', () => {
    state.etfAsset = b.dataset.etfasset;
    if (typeof localStorage !== 'undefined') localStorage.setItem('etfAsset', state.etfAsset);
    setActive('etfasset', state.etfAsset);
    renderAll();
  })
);
// Sync the active toggle to the persisted etfAsset on initial load.
setActive('etfasset', state.etfAsset);

// Futures tab BTC/ETH/LINK/LTC toggle — coupled to state.asset since the
// Futures renderers all key off the global asset. Persisted to localStorage
// as state.futuresAsset and mirrored into state.asset on click. selectTab()
// also pushes futuresAsset → state.asset whenever the Futures tab opens.
document.querySelectorAll('.btn[data-futuresasset]').forEach(b =>
  b.addEventListener('click', () => {
    state.futuresAsset = b.dataset.futuresasset;
    if (typeof localStorage !== 'undefined') localStorage.setItem('futuresAsset', state.futuresAsset);
    state.asset = state.futuresAsset;
    setActive('futuresasset', state.futuresAsset);
    setActive('asset', state.asset);
    // Re-tint the active tab underline to match the new asset.
    document.querySelectorAll('.tab').forEach(el => {
      el.classList.toggle('eth',  state.asset === 'eth');
      el.classList.toggle('link', state.asset === 'link');
    });
    renderAll();
  })
);
// Sync the active toggle to the persisted futuresAsset on initial load.
setActive('futuresasset', state.futuresAsset);

// DeFi tab chain selector (Ethereum / Solana / Arbitrum / Base) — persists
// to localStorage.defiChain. Only re-renders the per-chain section so the
// rest of the tab stays in place.
document.querySelectorAll('.btn[data-defichain]').forEach(b =>
  b.addEventListener('click', () => {
    state.defiChain = b.dataset.defichain;
    if (typeof localStorage !== 'undefined') localStorage.setItem('defiChain', state.defiChain);
    setActive('defichain', state.defiChain);
    renderDefiChainSection();
  })
);
// Sync active toggle to persisted defiChain on initial load.
setActive('defichain', state.defiChain);

// ---------- Chat dock ----------
const chatDock = document.getElementById('chatDock');
const chatFab  = document.getElementById('chatFab');
const chatMsgs = document.getElementById('chatMsgs');
const chatForm = document.getElementById('chatForm');
const chatInput= document.getElementById('chatInput');
const chatSend = document.getElementById('chatSend');

// Chat requires the Flask backend at /api/chat (uses ANTHROPIC_API_KEY).
// On the public GitHub Pages mirror that route doesn't exist — the raw
// request returns HTTP 405 with an ugly HTML body. Keep the chat widget
// visible (users want to know the feature exists) but intercept the
// submit so a friendly explainer renders instead of letting the fetch
// fail. The interception is below — wired right before the form-submit
// listener.
const _chatIsServer = (typeof location !== 'undefined') &&
  ['127.0.0.1','localhost','0.0.0.0'].includes(location.hostname);

function openChat(){ chatDock?.classList.add('open'); chatFab?.classList.add('hidden'); setTimeout(()=>chatInput?.focus(), 200); }
function closeChat(){ chatDock?.classList.remove('open'); chatFab?.classList.remove('hidden'); }

chatFab?.addEventListener('click', openChat);
document.getElementById('chatClose')?.addEventListener('click', closeChat);

function appendMsg(role, text){
  const el = document.createElement('div');
  el.className = 'msg ' + role;
  el.textContent = text;
  chatMsgs.appendChild(el);
  chatMsgs.scrollTop = chatMsgs.scrollHeight;
  return el;
}

document.querySelectorAll('#chatSuggestions .chip').forEach(c =>
  c.addEventListener('click', () => {
    chatInput.value = c.dataset.q;
    chatForm.requestSubmit();
  })
);

async function streamChat(question){
  appendMsg('user', question);
  const botEl = appendMsg('bot', '…');
  let acc = '';
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({question}),
    });
    if (!resp.ok || !resp.body){
      const text = await resp.text().catch(()=>'(no body)');
      botEl.className = 'msg err';
      botEl.textContent = 'Error ' + resp.status + ': ' + text.slice(0, 300);
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream:true});
      // SSE: lines starting with "data: "
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const evt = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of evt.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (data === '[DONE]') return;
          try {
            const j = JSON.parse(data);
            if (j.error){
              botEl.className = 'msg err';
              botEl.textContent = j.error;
              continue;
            }
            if (j.text){
              if (acc === '') botEl.textContent = '';
              acc += j.text;
              botEl.textContent = acc;
              chatMsgs.scrollTop = chatMsgs.scrollHeight;
            }
          } catch (e) {}
        }
      }
    }
  } catch (e) {
    botEl.className = 'msg err';
    botEl.textContent = 'Network error: ' + e.message;
  }
}

// ---- Client-side Anthropic chat for the public mirror ----
//
// The public mirror is a static GitHub Pages site — there's no /api/chat
// endpoint. To make chat actually WORK here (instead of just showing a
// "run locally" message), users can paste their own Anthropic API key
// once. It's stored in localStorage and never leaves the browser except
// to api.anthropic.com directly.
//
// Same pattern as the Twelvedata stock-lookup key (already shipped).
// Per-message cost on claude-haiku-4-5 is ~$0.001, so even heavy use
// stays well under the free-tier credit budget for most users.

const ANTHROPIC_KEY_LS = 'anthropic_api_key';
function getAnthropicKey(){
  try { return localStorage.getItem(ANTHROPIC_KEY_LS) || ''; } catch(_) { return ''; }
}
function promptForAnthropicKey(){
  const k = window.prompt(
    "Paste your Anthropic API key to enable chat on this URL.\n\n" +
    "The key is stored ONLY in this browser's localStorage and sent\n" +
    "ONLY to api.anthropic.com (the same call the local server makes).\n\n" +
    "Get one at: console.anthropic.com → API Keys\n" +
    "Format: sk-ant-...\n\n" +
    "(To clear later, type /clearkey into the chat box.)"
  );
  if (!k) return '';
  const trimmed = String(k).trim();
  if (!/^sk-ant-/.test(trimmed)){
    alert("That doesn't look like an Anthropic key (should start with sk-ant-).");
    return '';
  }
  try { localStorage.setItem(ANTHROPIC_KEY_LS, trimmed); } catch(_) {}
  return trimmed;
}
function clearAnthropicKey(){
  try { localStorage.removeItem(ANTHROPIC_KEY_LS); } catch(_) {}
}

// Compact dashboard-data projection — mirrors chat.py's _summarise_payload
// so the model sees the same shape on the public mirror as it does in
// local Flask mode. Keep this tight: 30 days of daily ETF flows, top 8
// funds per asset, latest market snapshot, latest whale row, all insights.
function buildChatContext(){
  const out = { generated_at: DATA.generated_at };
  for (const asset of ['btc','eth','link']){
    const a = DATA[asset]; if (!a) continue;
    out[asset] = {
      stats: a.stats || {},
      last_date: a.last_date,
      recent_daily: (a.daily || []).slice(-30),
      by_fund_top: (a.by_fund || []).slice(0, 8),
    };
  }
  const sigs = DATA.signals || {};
  out.signals = {};
  for (const k of Object.keys(sigs)){
    const v = sigs[k]; if (!v) continue;
    out.signals[k] = {
      score: v.score, label: v.label, as_of: v.as_of,
      components: v.components, price: v.price,
    };
  }
  const m = DATA.market || {};
  const snap = { global: m.global || {} };
  const lastVal = (arr, key) => (arr && arr.length) ? arr[arr.length - 1][key] : null;
  for (const asset of ['btc','eth','link']){
    const ma = m[asset] || {};
    snap[asset] = {
      last_price:       lastVal(ma.price, 'value'),
      last_volume:      lastVal(ma.volume, 'value'),
      last_funding:     lastVal(ma.funding, 'rate'),
      last_oi_usd:      lastVal(ma.open_interest_usd, 'oi_usd'),
      last_long_short:  lastVal(ma.long_short_ratio, 'ratio'),
      last_dvol:        lastVal(ma.dvol, 'dvol'),
    };
  }
  snap.fear_greed_latest = (m.fear_greed && m.fear_greed.length) ? m.fear_greed[m.fear_greed.length - 1] : null;
  snap.ethbtc_latest     = (m.ethbtc     && m.ethbtc.length)     ? m.ethbtc[m.ethbtc.length - 1] : null;
  out.market_snapshot = snap;
  const whale = ((DATA.whale || {}).btc) || {};
  const wlast = {};
  for (const k of Object.keys(whale)){
    const v = whale[k];
    if (Array.isArray(v)) wlast[k] = v.length ? v[v.length - 1] : null;
  }
  out.btc_whale_latest = wlast;
  out.insights = DATA.insights || [];
  return out;
}

const CHAT_CLIENT_SYSTEM_PROMPT =
  "You are an analyst embedded in a private dashboard that tracks U.S. spot " +
  "BTC and ETH ETF flows, LINK trading metrics, perpetual funding, open " +
  "interest, implied volatility (DVOL), Fear & Greed, and BTC on-chain whale " +
  "proxies. You ALSO see a rules-based composite signal (-100..+100) per asset.\n\n" +
  "When the user asks a question, answer concisely using ONLY the dashboard " +
  "context below. If the data needed is not present, say so plainly.\n\n" +
  "NEVER give explicit investment advice or recommendations to buy or sell " +
  "specific assets. If asked, you may explain what the indicators say and let " +
  "the user draw their own conclusions. You may discuss risk factors.\n\n" +
  "Format:\n" +
  "- Lead with the direct answer in 1-2 sentences.\n" +
  "- Then give 2-4 bullet points with the supporting numbers from the data.\n" +
  "- Cite the date and metric explicitly (e.g. \"as of 2026-05-12, BTC ETF " +
  "7-day net = +$543M\").\n" +
  "- Keep total response under ~200 words unless the user asks for more.\n\n" +
  "Dashboard context (JSON):\n";

async function clientSideChatStream(question, botEl){
  let key = getAnthropicKey();
  if (!key){
    key = promptForAnthropicKey();
    if (!key){
      botEl.className = 'msg bot';
      botEl.textContent = "No key entered. Chat needs your Anthropic key to answer questions on this URL. Submit again to try the prompt once more.";
      return;
    }
  }
  // Build context. Cap at ~50KB to stay well within the 200K input window
  // while keeping cost predictable (~$0.001/msg on Haiku).
  let ctxJson;
  try { ctxJson = JSON.stringify(buildChatContext()); }
  catch(_) { ctxJson = '{}'; }
  if (ctxJson.length > 50000) ctxJson = ctxJson.slice(0, 50000) + '"...(truncated)"}';
  const system = CHAT_CLIENT_SYSTEM_PROMPT + ctxJson;

  let acc = '';
  try {
    const resp = await fetch('https://api.anthropic.com/v1/messages', {
      method: 'POST',
      headers: {
        'x-api-key': key,
        'anthropic-version': '2023-06-01',
        'anthropic-dangerous-direct-browser-access': 'true',
        'content-type': 'application/json',
      },
      body: JSON.stringify({
        model: 'claude-haiku-4-5-20251001',
        max_tokens: 800,
        stream: true,
        system: system,
        messages: [{ role: 'user', content: question }],
      }),
    });
    if (!resp.ok || !resp.body){
      const errText = await resp.text().catch(()=>'(no body)');
      botEl.className = 'msg err';
      let msg = 'HTTP ' + resp.status;
      try {
        const j = JSON.parse(errText);
        if (j && j.error && j.error.message) msg = j.error.message;
      } catch(_) { msg = errText.slice(0, 200); }
      if (resp.status === 401){
        msg = 'Invalid Anthropic key (HTTP 401). Type /clearkey in the chat box to reset and re-enter.';
      } else if (resp.status === 429){
        msg = 'Rate limited (HTTP 429). Wait a moment and try again, or check your Anthropic console for usage limits.';
      }
      botEl.textContent = msg;
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true){
      const {value, done} = await reader.read();
      if (done) break;
      buf += decoder.decode(value, {stream:true});
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0){
        const evt = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of evt.split('\n')){
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (!data || data === '[DONE]') continue;
          try {
            const j = JSON.parse(data);
            // Anthropic streaming events: content_block_delta with text_delta
            if (j.type === 'content_block_delta' && j.delta && j.delta.type === 'text_delta'){
              if (acc === '') botEl.textContent = '';
              acc += j.delta.text;
              botEl.textContent = acc;
              chatMsgs.scrollTop = chatMsgs.scrollHeight;
            } else if (j.type === 'message_stop'){
              return;
            } else if (j.type === 'error' && j.error){
              botEl.className = 'msg err';
              botEl.textContent = (j.error.message || 'stream error').slice(0, 300);
              return;
            }
          } catch(_) { /* heartbeat / non-JSON event */ }
        }
      }
    }
  } catch(e){
    botEl.className = 'msg err';
    botEl.textContent = 'Network error: ' + e.message;
  }
}

// On first chat-dock open in public-mirror mode, drop in a one-time intro
// explaining the BYO-key flow up front (so the user understands the
// prompt that will appear on first submit instead of being surprised).
let _chatIntroShown = false;
function _maybeShowChatIntro(){
  if (_chatIntroShown || _chatIsServer) return;
  _chatIntroShown = true;
  const el = appendMsg('bot',
    "Chat is wired to call Anthropic's API directly from your browser. " +
    "On the first question I'll prompt for your Anthropic API key — paste it " +
    "once and it's saved in this browser's localStorage (never sent anywhere " +
    "except api.anthropic.com).\n\n" +
    "Get a key: console.anthropic.com → API Keys. Cost on Haiku ≈ $0.001/msg.\n\n" +
    "Type /clearkey to reset the stored key."
  );
  el.className = 'msg bot';
}
chatFab?.addEventListener('click', _maybeShowChatIntro);

chatForm?.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = chatInput.value.trim();
  if (!q) return;
  chatInput.value = '';
  // /clearkey command — reset the stored Anthropic key on the public mirror.
  if (!_chatIsServer && /^\/clear\s*key$/i.test(q)){
    clearAnthropicKey();
    appendMsg('user', q);
    appendMsg('bot', 'Anthropic key cleared. The next question will prompt for a new key.');
    chatInput.focus();
    return;
  }
  if (!_chatIsServer){
    // Public mirror: stream from Anthropic directly via the user's key.
    // (streamChat() does its own appendMsg('user', …) in local Flask mode,
    // so we only append the user bubble on this branch.)
    appendMsg('user', q);
    const botEl = appendMsg('bot', '…');
    chatSend.disabled = true;
    clientSideChatStream(q, botEl).finally(() => {
      chatSend.disabled = false;
      chatInput.focus();
    });
    return;
  }
  // Local Flask mode — use the existing /api/chat path.
  chatSend.disabled = true;
  streamChat(q).finally(() => { chatSend.disabled = false; chatInput.focus(); });
});

// Insights show/hide
document.getElementById('insightsToggle')?.addEventListener('click', () => {
  const list = document.getElementById('insightsList');
  const btn = document.getElementById('insightsToggle');
  if (list.style.display === 'none') {
    list.style.display = 'flex'; btn.textContent = 'Hide';
  } else {
    list.style.display = 'none'; btn.textContent = 'Show';
  }
});
document.querySelectorAll('.tab').forEach(b => {
  b.addEventListener('click', () => selectTab(b.dataset.tab));
  b.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      selectTab(b.dataset.tab);
    }
  });
});

// ---------- live refresh (server mode only) ----------
// /api/refresh is now ASYNC server-side — it kicks off a background fetch
// thread and returns immediately with `{ok, in_progress: true}`. We can't
// just consume the returned payload; instead we update the button state to
// "fetching…" and rely on the existing 60-second /api/data polling loop to
// pick up the fresh payload once the background fetch finishes (~30-60s).
async function liveRefresh(force){
  const btn = document.getElementById('refreshBtn');
  if (btn) { btn.disabled = true; btn.textContent = '↻ refreshing…'; }
  try {
    if (force) {
      // Kick off background fetch — server returns immediately.
      const r = await fetch('/api/refresh', {method:'POST'});
      if (!r.ok) throw new Error('http '+r.status);
      // Poll /api/data every 5s for up to 90s — bail when we see a fresh
      // generated_at OR when the timeout elapses.
      const oldStamp = DATA.generated_at;
      const t0 = Date.now();
      let updated = false;
      while (Date.now() - t0 < 90_000) {
        await new Promise(res => setTimeout(res, 5000));
        try {
          const rr = await fetch('/api/data');
          if (!rr.ok) continue;
          const j = await rr.json();
          if (j && j.generated_at && j.generated_at !== oldStamp) {
            Object.assign(DATA, j);
            document.getElementById('generatedAt').textContent = 'generated ' + DATA.generated_at;
            renderAll();
            updated = true;
            break;
          }
        } catch(_) { /* retry next tick */ }
      }
      if (btn) btn.textContent = updated ? '↻ Refresh' : '↻ slow…';
    } else {
      // Plain poll for the latest cached payload.
      const r = await fetch('/api/data');
      if (!r.ok) throw new Error('http '+r.status);
      const j = await r.json();
      Object.assign(DATA, j);
      document.getElementById('generatedAt').textContent = 'generated ' + DATA.generated_at;
      renderAll();
      if (btn) btn.textContent = '↻ Refresh';
    }
  } catch (e) {
    if (btn) btn.textContent = '↻ failed';
    console.warn('refresh failed', e);
  } finally {
    if (btn) setTimeout(()=> { btn.disabled = false; btn.textContent = '↻ Refresh'; }, 1500);
  }
}

document.getElementById('refreshBtn')?.addEventListener('click', () => liveRefresh(true));

// Seed BTC from GitHub mirror — function so we can re-bind after innerHTML restore
function rebindEtfImportButtons(){
  const sb = document.getElementById('seedBtn');
  if (sb && !sb.dataset.bound) {
    sb.dataset.bound = '1';
    sb.addEventListener('click', async () => {
      const s = document.getElementById('seedStatus');
      s.textContent = 'Pulling from GitHub mirror…';
      try {
        const r = await fetch('/api/seed-etf', {method:'POST'});
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'failed');
        s.textContent = `Imported ${j.rows} rows. Reloading…`;
        setTimeout(() => liveRefresh(false), 400);
      } catch(e) { s.textContent = 'Seed failed: ' + e.message; }
    });
  }
  const pb = document.getElementById('pasteBtn');
  if (pb && !pb.dataset.bound) {
    pb.dataset.bound = '1';
    pb.addEventListener('click', () => pasteModal?.classList.remove('hidden'));
  }
}
rebindEtfImportButtons();

// Paste CSV modal
const pasteModal = document.getElementById('pasteModal');
const closeModal = () => pasteModal?.classList.add('hidden');
const openModal  = (preset) => {
  if (preset) document.getElementById('pasteAsset').value = preset;
  document.getElementById('pasteText').value = '';
  document.getElementById('pasteStatus').textContent = '';
  pasteModal?.classList.remove('hidden');
  setTimeout(() => document.getElementById('pasteText')?.focus(), 50);
};
document.getElementById('pasteClose')?.addEventListener('click', closeModal);

// Persistent ETF action-bar buttons
document.getElementById('loadBtcBtn')?.addEventListener('click', () => openModal('btc'));
document.getElementById('loadEthBtn')?.addEventListener('click', () => openModal('eth'));
document.getElementById('seedBtcBtn')?.addEventListener('click', async () => {
  const s = document.getElementById('loadStatus');
  s.textContent = 'Pulling from GitHub mirror…';
  try {
    const r = await fetch('/api/seed-etf', {method:'POST'});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    s.textContent = `Imported ${j.rows} BTC rows. Reloading…`;
    setTimeout(() => liveRefresh(false), 400);
    setTimeout(() => s.textContent = '', 4000);
  } catch(e) { s.textContent = 'Seed failed: ' + e.message; }
});
// Click outside the inner box to close
pasteModal?.addEventListener('click', (e) => { if (e.target === pasteModal) closeModal(); });
// Escape to close
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !pasteModal?.classList.contains('hidden')) closeModal(); });
document.getElementById('pasteSubmit')?.addEventListener('click', async () => {
  const s = document.getElementById('pasteStatus');
  const text = document.getElementById('pasteText').value.trim();
  const asset = document.getElementById('pasteAsset').value;
  if (!text) { s.textContent = 'Empty input'; return; }
  s.textContent = 'Uploading…';
  try {
    const r = await fetch('/api/upload-csv?asset=' + asset, {method:'POST', headers:{'Content-Type':'text/csv'}, body:text});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    s.textContent = `Imported ${j.rows} rows to ${j.path}. Reloading…`;
    setTimeout(() => { pasteModal.classList.add('hidden'); liveRefresh(false); }, 600);
  } catch(e) { s.textContent = 'Import failed: ' + e.message; }
});

// "Live server" mode = we have a Flask backend at /api/* (running locally).
// "Public mirror" mode = static HTML served from GitHub Pages — no backend.
const isServer = ['127.0.0.1','localhost','0.0.0.0'].includes(location.hostname);
if (isServer) setInterval(() => liveRefresh(false), 60000);
// On the public mirror, repurpose the Refresh button to be a simple
// page-reload — pulls the latest static HTML from GH Pages (which gets
// re-generated by the hourly Actions cron). Force-busts Safari's
// aggressive HTML cache via a timestamp query string. Share button is
// hidden because it depends on the local /api/share endpoint.
if (!isServer){
  const _rb = document.getElementById('refreshBtn');
  if (_rb){
    _rb.textContent = '↻ Reload';
    _rb.title = 'Reload page to get the latest hourly snapshot';
    // Remove the existing live-server click handler by cloning the node.
    const _clone = _rb.cloneNode(true);
    _rb.parentNode.replaceChild(_clone, _rb);
    _clone.addEventListener('click', () => {
      _clone.disabled = true;
      _clone.textContent = '↻ reloading…';
      // Cache-bust query — Safari mobile holds onto HTML aggressively
      const u = new URL(location.href);
      u.searchParams.set('_', Date.now().toString());
      location.replace(u.toString());
    });
  }
  const _sb = document.getElementById('shareBtn');
  if (_sb) _sb.style.display = 'none';
  // ETF Flows tab: "Paste BTC", "Paste ETH", "Seed BTC (mirror)" + the
  // inline "Seed BTC from GitHub mirror" all POST to /api/upload-csv or
  // /api/seed-etf, which only exist in local Flask mode. On the static
  // mirror they 404 — hide the buttons entirely. Public-mirror users
  // refresh the ETF CSVs via the hourly Pages workflow, not by clicking.
  ['loadBtcBtn', 'loadEthBtn', 'seedBtcBtn', 'seedBtn'].forEach(id => {
    const b = document.getElementById(id);
    if (b) b.style.display = 'none';
  });
}

// ---------- Share modal (owner side) ----------
const shareModal = document.getElementById('shareModal');
const shareBtn   = document.getElementById('shareBtn');
const shareClose = document.getElementById('shareClose');
const shareList  = document.getElementById('shareList');
const shareStatus= document.getElementById('shareStatus');

function _shareHost(){
  try { return (localStorage.getItem('shareHost') || '').trim(); } catch(e) { return ''; }
}
function _shareUrl(token){
  const h = _shareHost();
  return (h || location.origin) + '/share/' + token;
}
function _validShareHost(v){
  // must start with http:// or https://, no spaces, non-empty
  if (!v) return false;
  if (/\s/.test(v)) return false;
  return /^https?:\/\/\S+$/i.test(v);
}
function _expiryLabel(iso){
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const ms = d.getTime() - Date.now();
  if (ms <= 0) return 'expired';
  const days = ms / 86400000;
  if (days >= 1) return 'expires in ' + Math.round(days) + 'd';
  const hours = ms / 3600000;
  if (hours >= 1) return 'expires in ' + Math.round(hours) + 'h';
  return 'expires in <1h';
}
async function loadShareList(){
  if (!shareList) return;
  shareList.innerHTML = '<div class="sub" style="color:var(--muted)">loading…</div>';
  try {
    const r = await fetch('/api/share');
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    const rows = j.shares || [];
    if (!rows.length) {
      shareList.innerHTML = '<div class="sub" style="color:var(--muted)">No active links.</div>';
      return;
    }
    shareList.innerHTML = '';
    for (const s of rows) {
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:6px;align-items:center;border:1px solid var(--border);border-radius:6px;padding:6px 8px';
      const url = _shareUrl(s.token);
      const lbl = (s.label || '').replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c]));
      row.innerHTML =
        '<div style="flex:1;min-width:0">'
        + '<div style="font:11px monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + url + '</div>'
        + '<div class="sub" style="color:var(--muted);margin-top:2px">' + (lbl || '<em style="color:#5a6478">no label</em>') + ' · ' + _expiryLabel(s.expires_at) + '</div>'
        + '</div>'
        + '<button class="btn" data-copy="' + s.token + '" style="font-size:11px;padding:3px 8px">Copy</button>'
        + '<button class="btn" data-revoke="' + s.token + '" style="font-size:11px;padding:3px 8px;background:#3a1f1f">Revoke</button>';
      shareList.appendChild(row);
    }
    shareList.querySelectorAll('[data-copy]').forEach(b =>
      b.addEventListener('click', () => navigator.clipboard?.writeText(_shareUrl(b.dataset.copy)).then(() => { shareStatus.textContent = 'Copied.'; setTimeout(() => shareStatus.textContent = '', 1500); }))
    );
    shareList.querySelectorAll('[data-revoke]').forEach(b =>
      b.addEventListener('click', async () => {
        if (!confirm('Revoke this share link?')) return;
        try {
          const rr = await fetch('/api/share/' + encodeURIComponent(b.dataset.revoke), {method:'DELETE'});
          const jj = await rr.json();
          if (!jj.ok) throw new Error(jj.error || 'failed');
          shareStatus.textContent = 'Revoked.';
          loadShareList();
        } catch(e) { shareStatus.textContent = 'Revoke failed: ' + e.message; }
      })
    );
  } catch(e) {
    shareList.innerHTML = '<div class="sub" style="color:#ff8888">Failed to load: ' + e.message + '</div>';
  }
}
shareBtn?.addEventListener('click', () => {
  shareModal.classList.remove('hidden');
  document.getElementById('shareJustMinted').classList.add('hidden');
  shareStatus.textContent = '';
  // Prefill public-host input from localStorage
  const hostInput = document.getElementById('shareHost');
  const hostStatus = document.getElementById('shareHostStatus');
  if (hostInput) hostInput.value = _shareHost();
  if (hostStatus) hostStatus.textContent = '';
  loadShareList();
});
document.getElementById('shareHostSave')?.addEventListener('click', () => {
  const inp = document.getElementById('shareHost');
  const status = document.getElementById('shareHostStatus');
  let v = (inp.value || '').trim();
  // strip trailing slash(es)
  v = v.replace(/\/+$/, '');
  if (!_validShareHost(v)) {
    status.style.color = '#ff8888';
    status.textContent = 'Invalid: must start with http:// or https:// and contain no spaces.';
    return;
  }
  try { localStorage.setItem('shareHost', v); } catch(e) {}
  inp.value = v;
  status.style.color = 'var(--muted)';
  status.textContent = 'Saved.';
  setTimeout(() => { if (status.textContent === 'Saved.') status.textContent = ''; }, 1800);
  // If a minted URL is already on screen, refresh it to use the new host
  const newUrlInp = document.getElementById('shareNewUrl');
  if (newUrlInp && newUrlInp.value) {
    const m = newUrlInp.value.match(/\/share\/([^\/?#]+)$/);
    if (m) {
      newUrlInp.value = _shareUrl(m[1]);
      const warn = document.getElementById('shareNewWarn');
      if (warn) { warn.classList.add('hidden'); warn.textContent = ''; }
    }
  }
  // Refresh the active-links list so URLs reflect the new host
  loadShareList();
});
document.getElementById('shareHostClear')?.addEventListener('click', (e) => {
  e.preventDefault();
  try { localStorage.removeItem('shareHost'); } catch(err) {}
  const inp = document.getElementById('shareHost');
  const status = document.getElementById('shareHostStatus');
  if (inp) inp.value = '';
  if (status) {
    status.style.color = 'var(--muted)';
    status.textContent = 'Cleared — falling back to local origin.';
    setTimeout(() => { if (status.textContent.startsWith('Cleared')) status.textContent = ''; }, 2200);
  }
  // Refresh minted URL + list to reflect fallback
  const newUrlInp = document.getElementById('shareNewUrl');
  if (newUrlInp && newUrlInp.value) {
    const m = newUrlInp.value.match(/\/share\/([^\/?#]+)$/);
    if (m) {
      newUrlInp.value = _shareUrl(m[1]);
      const warn = document.getElementById('shareNewWarn');
      if (warn) {
        warn.textContent = 'Set a Public host above to make this URL textable.';
        warn.classList.remove('hidden');
      }
    }
  }
  loadShareList();
});
shareClose?.addEventListener('click', () => shareModal.classList.add('hidden'));
shareModal?.addEventListener('click', e => { if (e.target === shareModal) shareModal.classList.add('hidden'); });
document.getElementById('shareCreate')?.addEventListener('click', async () => {
  const days = parseFloat(document.getElementById('shareDays').value) || 3;
  const label = document.getElementById('shareLabel').value.trim();
  shareStatus.textContent = 'Minting…';
  try {
    const r = await fetch('/api/share', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({days, label})});
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || 'failed');
    const url = _shareUrl(j.share.token);
    const box = document.getElementById('shareJustMinted');
    document.getElementById('shareNewUrl').value = url;
    box.classList.remove('hidden');
    // Show/hide the "no public host" warning under the minted link
    const warn = document.getElementById('shareNewWarn');
    if (warn) {
      if (_shareHost()) {
        warn.classList.add('hidden');
        warn.textContent = '';
      } else {
        warn.textContent = 'Set a Public host above to make this URL textable.';
        warn.classList.remove('hidden');
      }
    }
    shareStatus.textContent = 'Created. Expires ' + new Date(j.share.expires_at).toLocaleString();
    document.getElementById('shareLabel').value = '';
    loadShareList();
  } catch(e) { shareStatus.textContent = 'Mint failed: ' + e.message; }
});
document.getElementById('shareCopyBtn')?.addEventListener('click', () => {
  const url = document.getElementById('shareNewUrl').value;
  navigator.clipboard?.writeText(url).then(() => { shareStatus.textContent = 'Copied — paste it into a text.'; });
});

// ---------- Share (read-only viewer) mode ----------
// When the page is served via /share/<token>, hide all owner-only controls
// and surface a small banner so the viewer knows it's a time-bounded link.
if (IS_SHARE) {
  // Hide write-action buttons. The chat dock and refresh button mutate
  // server state (or cost Anthropic credits), so they're owner-only.
  ['refreshBtn','loadBtcBtn','loadEthBtn','seedBtcBtn','chatFab','chatDock'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  // Hide the entire "Load data" action bar on the ETF tab.
  document.querySelectorAll('#tab-etf .card').forEach(el => {
    if (el.textContent && el.textContent.includes('Load data')) el.style.display = 'none';
  });
  // Inject a small banner under the header.
  const expIso = (DATA.share && DATA.share.expires_at) || '';
  const label  = (DATA.share && DATA.share.label) || '';
  const banner = document.createElement('div');
  banner.style.cssText = 'background:#1a2840;border:1px solid #2c3e5e;color:#bfd2ff;padding:8px 14px;border-radius:8px;margin:8px 14px;font-size:12px;display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap';
  let when = '';
  if (expIso) {
    const d = new Date(expIso);
    if (!isNaN(d.getTime())) when = ' · expires ' + d.toLocaleString();
  }
  banner.innerHTML = '<span>🔗 Read-only share link' + (label ? ' (<em>' + label.replace(/[<>&"']/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;",'"':"&quot;","'":"&#39;"}[c])) + '</em>)' : '') + when + '</span><span style="color:#8a93a6">live data · auto-refreshes</span>';
  const container = document.querySelector('.container');
  if (container) container.insertBefore(banner, container.firstChild);
}

// ============ UNIVERSAL SYMBOL SEARCH (header → consolidated modal) ============
// Searches: stocks_signals, signals_top20, poc_top, news, cc_news sentiment.
// Renders only the sections that match. On cache miss, falls back to a live
// CryptoCompare browser fetch (CORS-ok) so users can look up any crypto.

// --- live crypto helpers (cache-miss fallback for lookupSymbol) ---
async function liveCryptoLookup(symbol){
  const sym = String(symbol || '').toUpperCase();
  if (!sym) throw new Error('empty symbol');
  const url = 'https://min-api.cryptocompare.com/data/v2/histoday?fsym=' +
              encodeURIComponent(sym) + '&tsym=USD&limit=180';
  const resp = await fetch(url, { method: 'GET' });
  if (!resp.ok) throw new Error('http ' + resp.status);
  const j = await resp.json();
  if (!j || j.Response !== 'Success' || !j.Data || !Array.isArray(j.Data.Data)){
    throw new Error('non-success response');
  }
  const rows = j.Data.Data
    .filter(r => r && typeof r.close === 'number' && r.close > 0)
    .map(r => ({
      time: r.time,
      close: Number(r.close),
      volumefrom: Number(r.volumefrom) || 0,
      volumeto: Number(r.volumeto) || 0,
    }));
  if (rows.length < 10) throw new Error('not enough data');
  return rows;
}

function liveComputePOC(rows){
  // Bin closes into 60 buckets by price range, weighted by volumeto.
  const closes = rows.map(r => r.close);
  const vols = rows.map(r => r.volumeto || 0);
  const minP = Math.min.apply(null, closes);
  const maxP = Math.max.apply(null, closes);
  const range = maxP - minP || 1;
  const N = 60;
  const buckets = new Array(N).fill(0);
  for (let i = 0; i < closes.length; i++){
    let idx = Math.floor(((closes[i] - minP) / range) * N);
    if (idx >= N) idx = N - 1;
    if (idx < 0) idx = 0;
    buckets[idx] += vols[i];
  }
  let pocIdx = 0;
  for (let i = 1; i < N; i++) if (buckets[i] > buckets[pocIdx]) pocIdx = i;
  const totalVol = buckets.reduce((a, b) => a + b, 0);
  // Value area = bins around POC capturing ~70% of volume.
  let lo = pocIdx, hi = pocIdx, acc = buckets[pocIdx];
  const target = totalVol * 0.70;
  while (acc < target && (lo > 0 || hi < N - 1)){
    const left  = lo > 0       ? buckets[lo - 1] : -1;
    const right = hi < N - 1   ? buckets[hi + 1] : -1;
    if (right >= left){ hi += 1; acc += buckets[hi]; }
    else { lo -= 1; acc += buckets[lo]; }
  }
  const bucketPrice = (i) => minP + (i + 0.5) * (range / N);
  return {
    poc: bucketPrice(pocIdx),
    valueAreaLow: bucketPrice(lo),
    valueAreaHigh: bucketPrice(hi),
    current: closes[closes.length - 1],
  };
}

function liveComputeSignal(rows){
  const closes = rows.map(r => r.close);
  const n = closes.length;
  const last = closes[n - 1];
  const sma = (k) => {
    if (n < k) return null;
    let s = 0;
    for (let i = n - k; i < n; i++) s += closes[i];
    return s / k;
  };
  const sma50 = sma(50);
  const sma200 = sma(200);
  const mom5 = n > 5 ? ((last - closes[n - 6]) / closes[n - 6]) * 100 : 0;
  let rsi = null;
  if (n >= 15){
    let gains = 0, losses = 0;
    for (let i = n - 14; i < n; i++){
      const d = closes[i] - closes[i - 1];
      if (d >= 0) gains += d; else losses -= d;
    }
    const avgG = gains / 14, avgL = losses / 14;
    if (avgL === 0) rsi = 100;
    else { const rs = avgG / avgL; rsi = 100 - (100 / (1 + rs)); }
  }
  let score = 0;
  if (sma50 != null) score += (last > sma50 ? 20 : -20);
  if (sma200 != null) score += (last > sma200 ? 20 : -20);
  score += Math.max(-10, Math.min(10, mom5));
  if (rsi != null){ if (rsi > 70) score -= 10; else if (rsi < 30) score += 10; }
  if (sma50 != null && sma200 != null) score += (sma50 > sma200 ? 10 : -10);
  score = Math.max(-100, Math.min(100, Math.round(score)));
  let label;
  if (score >= 50) label = 'STRONG BUY';
  else if (score >= 20) label = 'BUY';
  else if (score <= -50) label = 'STRONG SELL';
  else if (score <= -20) label = 'SELL';
  else label = 'HOLD';
  return { score, label, sma50, sma200, mom5, rsi, last };
}

function liveSignalColor(label){
  if (label === 'STRONG BUY') return '#16a34a';
  if (label === 'BUY') return '#22c55e';
  if (label === 'STRONG SELL') return '#b91c1c';
  if (label === 'SELL') return '#ef4444';
  return '#f59e0b';
}

function liveFmtUsd(v){
  if (v == null || !isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1000) return '$' + v.toLocaleString(undefined, {maximumFractionDigits: 2});
  if (a >= 1)    return '$' + v.toFixed(2);
  if (a >= 0.01) return '$' + v.toFixed(4);
  return '$' + v.toPrecision(3);
}

function liveLooksLikeStock(sym){
  // Crude: 1-5 uppercase A-Z, no digits. Used to pick the live stock branch
  // when the symbol isn't in our cached stocks_signals AND CryptoCompare has
  // nothing for it. Widened from 3-4 to 1-5 chars so tickers like F (Ford),
  // BA, GE, MSTR all qualify; 5-char tickers like GOOGL would too.
  return /^[A-Z]{1,5}$/.test(sym);
}

// --- live stock helpers (cache-miss fallback for stock-shaped symbols) ---
// Uses Twelvedata as primary (800 req/day free tier, CORS *) with Alpha Vantage
// fallback (25 req/day free, CORS *). Both require a user-obtained free API
// key — Yahoo Finance has no browser-usable CORS and the only keyless option
// (marketdata.app) limits to AAPL on free tier. The key is stored in
// localStorage under `stock_api_key` (Twelvedata) or `stock_api_key_av`
// (Alpha Vantage). If neither is set, we surface a friendly setup message.

function liveStockGetKey(provider){
  try {
    if (provider === 'av') return (localStorage.getItem('stock_api_key_av') || '').trim();
    return (localStorage.getItem('stock_api_key') || '').trim();
  } catch (_) { return ''; }
}

async function liveStockLookupTwelvedata(sym, key){
  const url = 'https://api.twelvedata.com/time_series'
    + '?symbol=' + encodeURIComponent(sym)
    + '&interval=1day&outputsize=220&order=asc'
    + '&apikey=' + encodeURIComponent(key);
  const resp = await fetch(url, { method: 'GET' });
  if (!resp.ok) throw new Error('twelvedata http ' + resp.status);
  const j = await resp.json();
  if (!j || j.status === 'error' || !Array.isArray(j.values)){
    throw new Error('twelvedata: ' + (j && j.message ? j.message : 'no data'));
  }
  // Twelvedata returns oldest-first when order=asc — each {datetime,open,high,low,close,volume}
  const rows = j.values
    .map(v => ({
      date: v.datetime,
      close: Number(v.close),
      volume: Number(v.volume) || 0,
    }))
    .filter(r => isFinite(r.close) && r.close > 0);
  if (rows.length < 10) throw new Error('twelvedata: not enough rows (' + rows.length + ')');
  return { rows, source: 'Twelvedata' };
}

async function liveStockLookupAlphaVantage(sym, key){
  const url = 'https://www.alphavantage.co/query'
    + '?function=TIME_SERIES_DAILY&outputsize=full'
    + '&symbol=' + encodeURIComponent(sym)
    + '&apikey=' + encodeURIComponent(key);
  const resp = await fetch(url, { method: 'GET' });
  if (!resp.ok) throw new Error('alphavantage http ' + resp.status);
  const j = await resp.json();
  if (!j) throw new Error('alphavantage: empty');
  if (j['Error Message']) throw new Error('alphavantage: ' + j['Error Message']);
  if (j['Note'] || j['Information']){
    throw new Error('alphavantage: ' + (j['Note'] || j['Information']));
  }
  const series = j['Time Series (Daily)'];
  if (!series || typeof series !== 'object') throw new Error('alphavantage: no series');
  // Alpha Vantage returns newest-first as a map keyed by YYYY-MM-DD.
  const dates = Object.keys(series).sort(); // ascending after sort
  const rows = [];
  for (const d of dates){
    const o = series[d] || {};
    const close = Number(o['4. close']);
    const vol = Number(o['6. volume'] || o['5. volume']) || 0;
    if (isFinite(close) && close > 0){
      rows.push({ date: d, close: close, volume: vol });
    }
  }
  if (rows.length < 10) throw new Error('alphavantage: not enough rows (' + rows.length + ')');
  // Trim to last 220 sessions (≈ 1y) so downstream math matches Twelvedata.
  const trimmed = rows.length > 220 ? rows.slice(rows.length - 220) : rows;
  return { rows: trimmed, source: 'Alpha Vantage' };
}

async function liveStockLookup(sym){
  // Try Twelvedata first (better free tier), fall back to Alpha Vantage.
  const tdKey = liveStockGetKey('td');
  const avKey = liveStockGetKey('av');
  if (!tdKey && !avKey){
    const err = new Error('NO_STOCK_API_KEY');
    err.code = 'NO_STOCK_API_KEY';
    throw err;
  }
  let lastErr = null;
  if (tdKey){
    try { return await liveStockLookupTwelvedata(sym, tdKey); }
    catch (e){ lastErr = e; }
  }
  if (avKey){
    try { return await liveStockLookupAlphaVantage(sym, avKey); }
    catch (e){ lastErr = e; }
  }
  throw lastErr || new Error('stock lookup failed');
}

function liveComputeStockSignal(rows){
  // Mirror of Python compute_stock_signal scoring (simplified to the
  // SMA50 / SMA200 / RSI14 / 5d-momentum / golden-cross axis the CryptoCompare
  // path already uses — same component weights, same final mapping).
  const closes = rows.map(r => r.close);
  return liveComputeSignal(rows.map(r => ({ close: r.close })));
}

function liveStockPromptForKey(){
  // Modal-friendly inline prompt — uses window.prompt for simplicity; the
  // dashboard already uses prompt() elsewhere for ad-hoc inputs. Returns the
  // trimmed key (and persists it to localStorage) or '' if the user cancels.
  let key = null;
  try {
    key = window.prompt(
      'Enter a free Twelvedata API key to enable live stock lookup.\n' +
      '\n' +
      'Get one (10 sec, no card): https://twelvedata.com/pricing\n' +
      'Free tier = 800 requests/day, supports any US ticker.\n' +
      '\n' +
      '(Stored locally in this browser only; never sent to the dashboard.)'
    );
  } catch (_) { key = null; }
  if (!key) return '';
  const trimmed = String(key).trim();
  if (!trimmed) return '';
  try { localStorage.setItem('stock_api_key', trimmed); } catch (_) {}
  return trimmed;
}

function renderLiveStockSection(sym, rows, source){
  const sig = liveComputeStockSignal(rows);
  const poc = liveComputePOC(rows.map(r => ({
    close: r.close,
    volumeto: r.volume,  // POC weighting expects volumeto field
  })));
  const closes = rows.map(r => r.close);
  const n = closes.length;
  const last = closes[n - 1];
  const ch5  = n > 5  ? ((last - closes[n - 6])  / closes[n - 6])  * 100 : null;
  const ch30 = n > 30 ? ((last - closes[n - 31]) / closes[n - 31]) * 100 : null;
  const fmtPct   = (p) => p == null ? '—' : (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  const pctColor = (p) => p == null ? 'var(--muted)' : (p >= 0 ? '#22c55e' : '#ef4444');
  const sparkVals = closes.slice(-30);
  const sparkUp = sparkVals.length >= 2 && sparkVals[sparkVals.length - 1] >= sparkVals[0];
  const spark = renderSparkline(sparkVals, sparkUp, 160, 36);
  const pocDistPct = (poc.current && poc.poc) ? ((poc.current - poc.poc) / poc.poc) * 100 : null;
  const color = liveSignalColor(sig.label);
  return (
    '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:8px">' +
      '<div style="font-size:26px;font-weight:700;letter-spacing:0.4px">' + escapeHtml(sym) + '</div>' +
      '<div class="sub" style="font-size:12px;color:var(--muted)">(live from ' + escapeHtml(source) + ')</div>' +
    '</div>' +
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:10px">' +
      '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">Signal</div>' +
        '<div style="font-size:20px;font-weight:700;color:' + color + ';margin-top:4px">' + escapeHtml(sig.label) + '</div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted)">score ' + sig.score + ' / 100' +
          (sig.rsi != null ? ' · RSI ' + sig.rsi.toFixed(0) : '') +
        '</div>' +
      '</div>' +
      '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">Price</div>' +
        '<div style="font-size:18px;font-weight:700;margin-top:4px">' + escapeHtml(liveFmtUsd(last)) + '</div>' +
        '<div style="font-size:11px;margin-top:2px">' +
          '<span style="color:' + pctColor(ch5)  + '">5d '  + escapeHtml(fmtPct(ch5))  + '</span> · ' +
          '<span style="color:' + pctColor(ch30) + '">30d ' + escapeHtml(fmtPct(ch30)) + '</span>' +
        '</div>' +
      '</div>' +
      '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">POC (' + rows.length + 'd)</div>' +
        '<div style="font-size:14px;font-weight:600;margin-top:4px">' + escapeHtml(liveFmtUsd(poc.poc)) + '</div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted)">' +
          'VA ' + escapeHtml(liveFmtUsd(poc.valueAreaLow)) + ' &ndash; ' + escapeHtml(liveFmtUsd(poc.valueAreaHigh)) +
        '</div>' +
        '<div style="font-size:11px;margin-top:2px;color:' + pctColor(pocDistPct) + '">' +
          'current vs POC ' + escapeHtml(fmtPct(pocDistPct)) +
        '</div>' +
      '</div>' +
    '</div>' +
    (spark ? '<div style="margin-top:10px"><div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:4px">30d sparkline</div>' + spark + '</div>' : '') +
    '<div class="sub" style="font-size:11px;color:var(--muted);margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">' +
      'Live fetch &mdash; signal computed client-side (SMA50 / SMA200 / RSI14 / 5d momentum / golden cross).' +
      ' Run <code>python server.py</code> locally for the full 6-component scorer.' +
    '</div>'
  );
}

function renderLiveStockNoKey(sym, suggestions){
  return (
    '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:8px">' +
      '<div style="font-size:26px;font-weight:700;letter-spacing:0.4px">' + escapeHtml(sym) + '</div>' +
      '<div class="sub" style="font-size:12px;color:var(--muted)">live stock lookup &mdash; setup needed</div>' +
    '</div>' +
    '<div style="padding:14px 0;line-height:1.55">' +
      '<div style="margin-bottom:10px"><strong>' + escapeHtml(sym) + '</strong> isn&rsquo;t in the cached top-50 most-active list.</div>' +
      renderSymbolSuggestionsStrip(suggestions) +
      '<div class="sub" style="color:var(--muted);font-size:13px;margin-bottom:14px">' +
        'To enable live lookup for any US ticker on the public mirror, add a free ' +
        '<a href="https://twelvedata.com/pricing" target="_blank" rel="noopener" style="color:#60a5fa">Twelvedata</a> ' +
        'API key (800 requests/day free, no card). Stored locally in this browser only.' +
      '</div>' +
      '<button id="liveStockKeyBtn" type="button" ' +
        'style="background:#2563eb;color:#fff;border:none;border-radius:6px;padding:8px 14px;font-weight:600;cursor:pointer">' +
        'Add API key&hellip;' +
      '</button>' +
      '<div class="sub" style="margin-top:14px;font-size:12px;color:var(--muted)">' +
        'Or run the dashboard locally with <code>python server.py</code> &mdash; the local mode uses Yahoo Finance ' +
        'server-side via <code>/api/symbol/' + escapeHtml(sym) + '</code> and has no rate limits.' +
      '</div>' +
    '</div>'
  );
}

function renderLiveStockFailed(sym, errMsg, suggestions){
  return (
    '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:8px">' +
      '<div style="font-size:26px;font-weight:700;letter-spacing:0.4px">' + escapeHtml(sym) + '</div>' +
      '<div class="sub" style="font-size:12px;color:var(--muted)">live stock lookup failed</div>' +
    '</div>' +
    '<div class="sub" style="color:var(--muted);padding:14px 0;line-height:1.55">' +
      'No stock data for <strong>' + escapeHtml(sym) + '</strong>' +
      (errMsg ? ' &mdash; <code style="font-size:11px">' + escapeHtml(String(errMsg).slice(0, 200)) + '</code>' : '') +
      '<br><br>' +
      renderSymbolSuggestionsStrip(suggestions) +
      'Possible causes: invalid ticker, free-tier daily limit hit, or upstream outage. ' +
      'Try again later, or run the dashboard locally with <code>python server.py</code> ' +
      '(uses Yahoo Finance server-side with no rate limit).' +
    '</div>'
  );
}

// --- server-endpoint fallback (used in local Flask mode) ---
// Renders the same modal layout from the server's /api/symbol/<sym> response.
async function liveServerSymbolLookup(sym){
  const resp = await fetch('/api/symbol/' + encodeURIComponent(sym));
  if (!resp.ok){
    const status = resp.status;
    let msg = 'http ' + status;
    try { const j = await resp.json(); if (j && j.error) msg = j.error; } catch (_) {}
    const err = new Error(msg);
    err.status = status;
    throw err;
  }
  return await resp.json();
}

function renderServerSymbolSection(sym, payload){
  const kind = payload.kind || 'stock';
  const sourceLbl = kind === 'crypto' ? 'server · CryptoCompare' : 'server · Yahoo Finance';
  const score = Number(payload.score) || 0;
  const label = payload.label || 'HOLD';
  const last  = payload.price;
  const color = liveSignalColor(label);
  // Derive 5d/30d change from the embedded rolling history (best-effort).
  const hist = Array.isArray(payload.history) ? payload.history : [];
  const sparkVals = hist.slice(-30).map(h => Number(h.score)).filter(v => isFinite(v));
  const sparkUp = sparkVals.length >= 2 && sparkVals[sparkVals.length - 1] >= sparkVals[0];
  const spark = sparkVals.length >= 2 ? renderSparkline(sparkVals, sparkUp, 160, 36) : '';
  const poc = (payload.poc || {}).d180 || (payload.poc || {}).d90 || (payload.poc || {}).d30 || null;
  const pocPrice = poc && (poc.poc != null ? poc.poc : poc.price);
  const vaLow  = poc && (poc.value_area_low  != null ? poc.value_area_low  : poc.va_low);
  const vaHigh = poc && (poc.value_area_high != null ? poc.value_area_high : poc.va_high);
  const pocDistPct = (last != null && pocPrice) ? ((last - pocPrice) / pocPrice) * 100 : null;
  const fmtPct   = (p) => p == null ? '—' : (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  const pctColor = (p) => p == null ? 'var(--muted)' : (p >= 0 ? '#22c55e' : '#ef4444');
  return (
    '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:8px">' +
      '<div style="font-size:26px;font-weight:700;letter-spacing:0.4px">' + escapeHtml(sym) + '</div>' +
      '<div class="sub" style="font-size:12px;color:var(--muted)">(' + escapeHtml(sourceLbl) + ')</div>' +
    '</div>' +
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:10px">' +
      '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">Signal</div>' +
        '<div style="font-size:20px;font-weight:700;color:' + color + ';margin-top:4px">' + escapeHtml(label) + '</div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted)">score ' + score + ' / 100</div>' +
      '</div>' +
      '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">Price</div>' +
        '<div style="font-size:18px;font-weight:700;margin-top:4px">' + escapeHtml(liveFmtUsd(last)) + '</div>' +
      '</div>' +
      (pocPrice ?
        '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
          '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">POC</div>' +
          '<div style="font-size:14px;font-weight:600;margin-top:4px">' + escapeHtml(liveFmtUsd(pocPrice)) + '</div>' +
          (vaLow && vaHigh ?
            '<div class="sub" style="font-size:11px;color:var(--muted)">VA ' +
              escapeHtml(liveFmtUsd(vaLow)) + ' &ndash; ' + escapeHtml(liveFmtUsd(vaHigh)) +
            '</div>' : '') +
          '<div style="font-size:11px;margin-top:2px;color:' + pctColor(pocDistPct) + '">' +
            'current vs POC ' + escapeHtml(fmtPct(pocDistPct)) +
          '</div>' +
        '</div>' : '') +
    '</div>' +
    (spark ? '<div style="margin-top:10px"><div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:4px">90d score history</div>' + spark + '</div>' : '') +
    '<div class="sub" style="font-size:11px;color:var(--muted);margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">' +
      'Server endpoint &mdash; full 6-component scorer (SMA50/200, RSI14, MACD, 5d momentum, volume z-score, golden cross).' +
    '</div>'
  );
}

function renderLiveCryptoSection(sym, rows){
  const sig = liveComputeSignal(rows);
  const poc = liveComputePOC(rows);
  const closes = rows.map(r => r.close);
  const n = closes.length;
  const last = closes[n - 1];
  const ch5  = n > 5  ? ((last - closes[n - 6])  / closes[n - 6])  * 100 : null;
  const ch30 = n > 30 ? ((last - closes[n - 31]) / closes[n - 31]) * 100 : null;
  const fmtPct   = (p) => p == null ? '—' : (p >= 0 ? '+' : '') + p.toFixed(2) + '%';
  const pctColor = (p) => p == null ? 'var(--muted)' : (p >= 0 ? '#22c55e' : '#ef4444');
  const sparkVals = closes.slice(-30);
  const sparkUp = sparkVals.length >= 2 && sparkVals[sparkVals.length - 1] >= sparkVals[0];
  const spark = renderSparkline(sparkVals, sparkUp, 160, 36);
  const pocDistPct = (poc.current && poc.poc) ? ((poc.current - poc.poc) / poc.poc) * 100 : null;
  const color = liveSignalColor(sig.label);
  return (
    '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:8px">' +
      '<div style="font-size:26px;font-weight:700;letter-spacing:0.4px">' + escapeHtml(sym) + '</div>' +
      '<div class="sub" style="font-size:12px;color:var(--muted)">(live from CryptoCompare)</div>' +
    '</div>' +
    '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin-top:10px">' +
      '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">Signal</div>' +
        '<div style="font-size:20px;font-weight:700;color:' + color + ';margin-top:4px">' + escapeHtml(sig.label) + '</div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted)">score ' + sig.score + ' / 100' +
          (sig.rsi != null ? ' · RSI ' + sig.rsi.toFixed(0) : '') +
        '</div>' +
      '</div>' +
      '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">Price</div>' +
        '<div style="font-size:18px;font-weight:700;margin-top:4px">' + escapeHtml(liveFmtUsd(last)) + '</div>' +
        '<div style="font-size:11px;margin-top:2px">' +
          '<span style="color:' + pctColor(ch5)  + '">5d '  + escapeHtml(fmtPct(ch5))  + '</span> · ' +
          '<span style="color:' + pctColor(ch30) + '">30d ' + escapeHtml(fmtPct(ch30)) + '</span>' +
        '</div>' +
      '</div>' +
      '<div style="border:1px solid var(--border);border-radius:8px;padding:10px">' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase">POC (180d)</div>' +
        '<div style="font-size:14px;font-weight:600;margin-top:4px">' + escapeHtml(liveFmtUsd(poc.poc)) + '</div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted)">' +
          'VA ' + escapeHtml(liveFmtUsd(poc.valueAreaLow)) + ' &ndash; ' + escapeHtml(liveFmtUsd(poc.valueAreaHigh)) +
        '</div>' +
        '<div style="font-size:11px;margin-top:2px;color:' + pctColor(pocDistPct) + '">' +
          'current vs POC ' + escapeHtml(fmtPct(pocDistPct)) +
        '</div>' +
      '</div>' +
    '</div>' +
    (spark ? '<div style="margin-top:10px"><div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:4px">30d sparkline</div>' + spark + '</div>' : '') +
    '<div class="sub" style="font-size:11px;color:var(--muted);margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">' +
      'Live fetch &mdash; not cached. Use the dashboard&rsquo;s signals/POC tabs for full historical context.' +
    '</div>'
  );
}

// Historical-ticker rebrands. The old ticker no longer trades, so silently
// redirecting to the current name is unambiguous and helpful. Keep this map
// small: each entry is "the old/wrong ticker → the one the user almost
// certainly meant." Common typos that map to a still-distinct real ticker
// (e.g. INTL → INTC) do NOT go here — they're handled by the fuzzy-suggest
// UI below so the user can confirm rather than be silently redirected.
const SYMBOL_REBRANDS = {
  FB:   'META',  // Facebook → Meta rebrand (Oct 2021)
  TWTR: 'X',     // Twitter → X (now private but the alias is harmless)
};

// Levenshtein for short symbol strings. Returns 99 (≈ infinity) when the
// lengths diverge by more than 2 so we can short-circuit unrelated names.
function symbolEditDist(a, b){
  a = String(a || '').toUpperCase();
  b = String(b || '').toUpperCase();
  if (!a || !b) return 99;
  if (a === b) return 0;
  const m = a.length, n = b.length;
  if (Math.abs(m - n) > 2) return 99;
  let prev = Array(n + 1).fill(0).map((_, i) => i);
  for (let i = 1; i <= m; i++){
    const cur = [i];
    for (let j = 1; j <= n; j++){
      const cost = a.charCodeAt(i - 1) === b.charCodeAt(j - 1) ? 0 : 1;
      cur.push(Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost));
    }
    prev = cur;
  }
  return prev[n];
}

// Gather every symbol currently in the cached payload — stock_signals,
// signals_top20, markets_top, poc_top, plus the four pinned cryptos. Used
// only by symbolFuzzyMatches so we can suggest a close ticker when the
// user's query misses cache.
function collectCachedSymbols(){
  const market = (typeof DATA !== 'undefined' && DATA && DATA.market) ? DATA.market : {};
  const set = new Set();
  const add = (s) => { if (s) set.add(String(s).toUpperCase()); };
  ((market.stocks_signals) || []).forEach(s => s && add(s.symbol));
  (((typeof DATA !== 'undefined' && DATA && DATA.signals_top20) || [])).forEach(s => s && add(s.symbol));
  ((market.markets_top) || []).forEach(r => r && add(r.symbol));
  ((market.poc_top) || []).forEach(r => r && add(r.symbol));
  ['BTC', 'ETH', 'LINK', 'LTC'].forEach(add);
  return Array.from(set);
}

// Return up to `max` cached symbols within an edit distance of 2 from
// `query`, ranked by distance then alphabetically. Empty array when
// nothing comes close — caller falls through to its existing copy.
function symbolFuzzyMatches(query, max){
  const m = (typeof max === 'number' && max > 0) ? max : 3;
  const q = String(query || '').toUpperCase();
  if (!q) return [];
  const all = collectCachedSymbols();
  const ranked = [];
  for (let i = 0; i < all.length; i++){
    const s = all[i];
    if (s === q) continue;
    const d = symbolEditDist(q, s);
    if (d > 0 && d <= 2) ranked.push({ sym: s, d: d });
  }
  ranked.sort((a, b) => a.d - b.d || a.sym.localeCompare(b.sym));
  return ranked.slice(0, m).map(o => o.sym);
}

// Build the clickable "Did you mean: X · Y · Z" chip strip. Returns '' when
// suggestions is empty so callers can concat unconditionally. Chips carry
// data-suggest-sym so a delegated click handler can route to lookupSymbol.
function renderSymbolSuggestionsStrip(suggestions){
  if (!suggestions || !suggestions.length) return '';
  const chips = suggestions.map(s => {
    const esc = escapeHtml(s);
    return '<button type="button" class="symbol-suggest-chip" data-suggest-sym="' + esc + '" ' +
             'style="background:#2563eb22;color:#60a5fa;border:1px solid #2563eb55;' +
             'padding:4px 10px;border-radius:4px;cursor:pointer;font-weight:600;font-size:12px">' +
             esc +
           '</button>';
  }).join('');
  return '<div style="margin-bottom:14px;padding:10px 12px;background:#1e293b;border:1px solid #334155;border-radius:8px">' +
           '<div class="sub" style="font-size:11px;color:var(--muted);margin-bottom:6px">Did you mean:</div>' +
           '<div style="display:flex;gap:6px;flex-wrap:wrap">' + chips + '</div>' +
         '</div>';
}

// One-time delegated click handler for the .symbol-suggest-chip buttons.
// Idempotent — guards against re-wiring on every modal render.
(function wireSymbolSuggestionChips(){
  if (typeof document === 'undefined') return;
  if (window._symbolSuggestWired) return;
  window._symbolSuggestWired = true;
  document.addEventListener('click', e => {
    const btn = e.target && e.target.closest && e.target.closest('.symbol-suggest-chip');
    if (!btn) return;
    const sym = btn.getAttribute('data-suggest-sym');
    if (sym && typeof lookupSymbol === 'function') lookupSymbol(sym);
  });
})();

// Parse a raw symbol-search input into a deduped list of uppercase symbols.
// Accepts comma, semicolon, and whitespace separators. Caps at MAX_SYMBOLS
// (6) to keep the modal sane and avoid abuse. Applies SYMBOL_REBRANDS so
// retired tickers (FB → META) silently route to the current name. Exported
// for tests via the HTML payload — see test_dashboard_integration.py.
function parseSymbolSearchTokens(raw){
  const MAX_SYMBOLS = 6;
  if (raw == null) return [];
  const s = String(raw).trim();
  if (!s) return [];
  const parts = s.split(/[\s,;]+/);
  const seen = new Set();
  const out = [];
  for (let i = 0; i < parts.length; i++){
    let tok = String(parts[i] || '').trim();
    if (!tok) continue;
    // Strip cashtag prefix ($BTC → BTC) so paste from social mentions works.
    if (tok.charAt(0) === '$') tok = tok.slice(1).trim();
    if (!tok) continue;
    let up = tok.toUpperCase();
    // Unambiguous historical rebrand → silently rewrite. We dedup AFTER the
    // rewrite so typing "FB, META" doesn't open the modal for META twice.
    if (Object.prototype.hasOwnProperty.call(SYMBOL_REBRANDS, up)) up = SYMBOL_REBRANDS[up];
    if (seen.has(up)) continue;
    seen.add(up);
    out.push(up);
    if (out.length >= MAX_SYMBOLS) break;
  }
  return out;
}

// Resolve a single symbol against cached DATA. Returns an object describing
// what was found (or null if nothing matched the cache). Pulled out of
// lookupSymbol so both the single- and multi-symbol paths share resolution.
function resolveSymbolFromCache(sym){
  const symLower = sym.toLowerCase();
  const market = DATA.market || {};
  const eq = (a, b) => String(a || '').toUpperCase() === b;
  const stock = (market.stocks_signals || []).find(s => s && eq(s.symbol, sym));
  const cryptoSignal = (DATA.signals_top20 || []).find(s => s && eq(s.symbol, sym));
  // Crypto POC lives in market.poc_top; stock POC is embedded on the
  // stock row itself (compute_stock_poc, same shape as poc_top entries).
  const cryptoPoc = (market.poc_top || []).find(r => r && (eq(r.symbol, sym) || eq(r.coin_id, sym)));
  const poc = cryptoPoc || (stock && stock.poc ? stock : null);
  let newsRegex = null;
  try { newsRegex = new RegExp('\\b' + sym.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + '\\b', 'i'); }
  catch(_) { newsRegex = null; }
  const news = (market.news || []).filter(n => {
    if (!n) return false;
    const t = String(n.title || '');
    const b = String(n.body || '');
    if (newsRegex){
      return newsRegex.test(t) || newsRegex.test(b);
    }
    const up = sym;
    return t.toUpperCase().includes(up) || b.toUpperCase().includes(up);
  });
  const sentiment = ((((market.social || {}).cc_news || {}).coins) || {})[symLower] || null;
  const hasAny = !!(stock || cryptoSignal || poc || news.length || sentiment);
  const displayName =
    (stock && stock.name) ||
    (cryptoSignal && cryptoSignal.name) ||
    (poc && poc.name) ||
    '';
  return { sym: sym, stock: stock, cryptoSignal: cryptoSignal, poc: poc, news: news, sentiment: sentiment, hasAny: hasAny, displayName: displayName };
}

// Build the inner HTML for a single cache-hit symbol (everything below the
// modal title — header block + signal/POC/news/sentiment sections).
function buildSymbolSectionsHtml(resolved){
  const sym = resolved.sym;
  const stock = resolved.stock;
  const cryptoSignal = resolved.cryptoSignal;
  const poc = resolved.poc;
  const news = resolved.news;
  const sentiment = resolved.sentiment;
  const displayName = resolved.displayName;
  const sections = [];
  sections.push(
    '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:8px">' +
      '<div style="font-size:26px;font-weight:700;letter-spacing:0.4px">' + escapeHtml(sym) + '</div>' +
      (displayName ? '<div class="sub" style="font-size:13px;color:var(--muted)">' + escapeHtml(displayName) + '</div>' : '') +
    '</div>'
  );
  // Signal + POC pair — side-by-side on desktop, stacked on mobile.
  // Signal (left): stockDetailHtml for stocks, renderSignalCardFromObj for
  // top-50 cryptos. POC (right): pocCompactCardHtml for cryptos in poc_top,
  // pocEmptyCardHtml for stocks or cryptos outside top-50. Either side may
  // be empty individually; pairing renders only when at least one exists.
  const signalHtml = stock
    ? stockDetailHtml(stock)
    : (cryptoSignal ? renderSignalCardFromObj(cryptoSignal) : '');
  const signalLabel = stock ? 'Stock signal' : (cryptoSignal ? 'Crypto signal' : '');
  const hasSignal = !!signalHtml;
  const hasPoc    = !!poc;
  if (hasSignal || hasPoc){
    const pocCardHtml = hasPoc
      ? pocCompactCardHtml(poc)
      : pocEmptyCardHtml(sym, stock ? 'stock' : 'crypto');
    const signalSection = hasSignal
      ? '<div>' +
          '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">' + escapeHtml(signalLabel) + '</div>' +
          signalHtml +
        '</div>'
      : '<div class="chart-card" style="opacity:.85"><div class="empty" style="padding:18px 8px;font-size:12px">No signal data for <strong>' + escapeHtml(sym) + '</strong> in this build.</div></div>';
    const pocSection = '<div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Point of Control</div>' +
        pocCardHtml +
      '</div>';
    sections.push(
      '<div class="grid2 symbol-modal-body">' +
        signalSection +
        pocSection +
      '</div>'
    );
  }
  if (news && news.length){
    const sorted = news.slice().sort((a, b) => {
      const da = a && a.date ? Date.parse(a.date) : 0;
      const db = b && b.date ? Date.parse(b.date) : 0;
      return (db || 0) - (da || 0);
    });
    const newsRows = sorted.slice(0, 5).map(n => {
      return '<a href="' + sanitizeUrl(n.url) + '" target="_blank" rel="noopener" ' +
        'style="display:block;padding:8px 10px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text)">' +
          '<div style="font-weight:600;font-size:13px">' + escapeHtml(n.title || '') + '</div>' +
          '<div style="font-size:11px;color:var(--muted);margin-top:2px">' +
            escapeHtml(n.source_name || n.source || '') + (n.date ? ' · ' + escapeHtml(n.date) : '') +
          '</div>' +
        '</a>';
    }).join('');
    sections.push(
      '<div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">News · ' + sorted.length + ' match' + (sorted.length === 1 ? '' : 'es') + '</div>' +
        '<div style="border:1px solid var(--border);border-radius:8px;overflow:hidden">' + newsRows + '</div>' +
      '</div>'
    );
  }
  if (sentiment){
    const pos = Number(sentiment.positive) || 0;
    const neg = Number(sentiment.negative) || 0;
    const neu = Number(sentiment.neutral) || 0;
    const total = pos + neg + neu || 1;
    const posPct = (pos / total) * 100;
    const negPct = (neg / total) * 100;
    const neuPct = (neu / total) * 100;
    const net = sentiment.net_score;
    const netColor = net == null ? 'var(--muted)' : (net > 0 ? '#22c55e' : (net < 0 ? '#ef4444' : '#f59e0b'));
    const netTxt = net == null ? '—' : ((net > 0 ? '+' : '') + net);
    sections.push(
      '<div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">News sentiment</div>' +
        '<div style="display:flex;justify-content:space-between;align-items:baseline">' +
          '<span class="sub" style="color:var(--muted);font-size:11px">' + (Number(sentiment.article_count) || total) + ' articles scored</span>' +
          '<span style="color:' + netColor + ';font-weight:700;font-size:14px">net ' + netTxt + '</span>' +
        '</div>' +
        '<div style="display:flex;height:10px;margin-top:6px;border-radius:3px;overflow:hidden;background:#1f2533">' +
          '<div style="background:#22c55e;width:' + posPct.toFixed(1) + '%" title="' + pos + ' positive"></div>' +
          '<div style="background:#f59e0b;width:' + neuPct.toFixed(1) + '%" title="' + neu + ' neutral"></div>' +
          '<div style="background:#ef4444;width:' + negPct.toFixed(1) + '%" title="' + neg + ' negative"></div>' +
        '</div>' +
        '<div style="display:flex;justify-content:space-between;margin-top:4px;font-size:11px;color:var(--muted)">' +
          '<span style="color:#22c55e">' + pos + ' positive</span>' +
          '<span>' + neu + ' neutral</span>' +
          '<span style="color:#ef4444">' + neg + ' negative</span>' +
        '</div>' +
      '</div>'
    );
  }
  return sections.join('');
}

// Resolve and render HTML for a single symbol — cache hit OR live fallbacks.
// Returns { html, found, sym, displayName }. `found` is false only when
// neither cache nor any live source produced data. Reused by both the
// single- and multi-symbol modal paths.
async function resolveAndRenderSymbol(sym){
  const resolved = resolveSymbolFromCache(sym);
  if (resolved.hasAny){
    return { html: buildSymbolSectionsHtml(resolved), found: true, sym: sym, displayName: resolved.displayName };
  }
  // No cache hit — try live sources in the same order lookupSymbol does.
  if (typeof isServer !== 'undefined' && isServer){
    try {
      const payload = await liveServerSymbolLookup(sym);
      return { html: renderServerSymbolSection(sym, payload), found: true, sym: sym, displayName: '' };
    } catch (err) { /* fall through */ }
  }
  try {
    const rows = await liveCryptoLookup(sym);
    return { html: renderLiveCryptoSection(sym, rows), found: true, sym: sym, displayName: '' };
  } catch (err) { /* fall through */ }
  if (liveLooksLikeStock(sym)){
    try {
      const out = await liveStockLookup(sym);
      return { html: renderLiveStockSection(sym, out.rows, out.source), found: true, sym: sym, displayName: '' };
    } catch (err){
      // Surface the no-API-key panel as a "found" result so the user can
      // wire a key from inside the modal (matches single-symbol behavior).
      if (err && err.code === 'NO_STOCK_API_KEY'){
        return { html: renderLiveStockNoKey(sym, symbolFuzzyMatches(sym)), found: true, sym: sym, displayName: '' };
      }
      return { html: '', found: false, sym: sym, displayName: '' };
    }
  }
  return { html: '', found: false, sym: sym, displayName: '' };
}

// --- Recent symbol lookups ---------------------------------------------
// Persisted FIFO (most-recent-first) of the last RECENT_SYMBOL_CAP symbols
// the user successfully looked up. Backed by localStorage (key
// `recentSymbolLookups`); falls back to an in-memory array when storage is
// unavailable (Safari private mode, file:// origin restrictions, etc.).
// Rendered as a chip strip below the header symbol-search form by
// renderSymbolRecentChips(); chip click re-runs lookupSymbol, × removes a
// single entry.
const RECENT_SYMBOL_KEY = 'recentSymbolLookups';
const RECENT_SYMBOL_CAP = 6;

function _readSymbolRecents(){
  // Returns an array of uppercased symbol strings. Anything malformed in
  // storage (non-array, non-string entries, duplicates) gets normalised to
  // an empty list rather than crashing the renderer.
  try {
    const raw = window.localStorage.getItem(RECENT_SYMBOL_KEY);
    if (!raw) return (window._symbolRecents = []);
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return (window._symbolRecents = []);
    const seen = new Set();
    const out = [];
    for (const v of parsed){
      if (typeof v !== 'string') continue;
      const up = v.trim().toUpperCase();
      if (!up || seen.has(up)) continue;
      seen.add(up);
      out.push(up);
      if (out.length >= RECENT_SYMBOL_CAP) break;
    }
    return (window._symbolRecents = out);
  } catch (_) {
    // localStorage unavailable or JSON.parse threw — fall through to the
    // in-memory cache so chips still work for the rest of the session.
    if (!Array.isArray(window._symbolRecents)) window._symbolRecents = [];
    return window._symbolRecents;
  }
}

function _writeSymbolRecents(list){
  window._symbolRecents = list.slice(0, RECENT_SYMBOL_CAP);
  try {
    window.localStorage.setItem(RECENT_SYMBOL_KEY, JSON.stringify(window._symbolRecents));
  } catch (_) { /* private mode — in-memory only is fine */ }
}

function pushSymbolRecent(sym){
  if (typeof sym !== 'string') return;
  const up = sym.trim().toUpperCase();
  if (!up) return;
  const cur = _readSymbolRecents();
  const next = [up];
  for (const v of cur){
    if (v !== up) next.push(v);
    if (next.length >= RECENT_SYMBOL_CAP) break;
  }
  _writeSymbolRecents(next);
  renderSymbolRecentChips();
}

function removeSymbolRecent(sym){
  if (typeof sym !== 'string') return;
  const up = sym.trim().toUpperCase();
  if (!up) return;
  const cur = _readSymbolRecents();
  const next = cur.filter(v => v !== up);
  _writeSymbolRecents(next);
  renderSymbolRecentChips();
}

function renderSymbolRecentChips(){
  const host = document.getElementById('symbolRecentChips');
  if (!host) return;
  const list = _readSymbolRecents();
  if (!list.length){
    host.classList.add('hidden');
    host.innerHTML = '';
    return;
  }
  host.classList.remove('hidden');
  // Build buttons via DOM (escapeHtml is fine but explicit nodes avoid any
  // injection worry and let us bind handlers without inline strings).
  host.innerHTML = '';
  for (const sym of list){
    const btn = document.createElement('button');
    btn.type = 'button';                 // not 'submit' — we're inside the form
    btn.className = 'symbol-recent-chip';
    btn.setAttribute('data-symbol', sym);
    btn.setAttribute('aria-label', 'Re-open ' + sym);
    btn.title = 'Re-open ' + sym;
    const label = document.createElement('span');
    label.textContent = sym;
    btn.appendChild(label);
    const x = document.createElement('span');
    x.className = 'symbol-recent-chip-x';
    x.textContent = '×';            // ×
    x.setAttribute('role', 'button');
    x.setAttribute('aria-label', 'Remove ' + sym + ' from recents');
    x.title = 'Remove from recents';
    btn.appendChild(x);
    btn.addEventListener('click', (e) => {
      // Scope the × handler to the inner span so removing doesn't trigger
      // a lookup on the same click.
      if (e.target === x || (e.target && e.target.classList && e.target.classList.contains('symbol-recent-chip-x'))){
        e.preventDefault();
        e.stopPropagation();
        removeSymbolRecent(sym);
        return;
      }
      const input = document.getElementById('symbolSearchInput');
      const form = document.getElementById('symbolSearchForm');
      if (input) input.value = sym;
      // Prefer the form's submit handler (consistent with manual submit);
      // fall back to direct lookupSymbol if the form is missing.
      if (form){
        if (typeof form.requestSubmit === 'function') form.requestSubmit();
        else lookupSymbol(sym);
      } else {
        lookupSymbol(sym);
      }
    });
    host.appendChild(btn);
  }
}

// --- Compact POC card for the symbol-detail modal ---
//
// Slim variant: header (icon + symbol + migration chip), price vs 90d POC
// distance with IN VA / OUT badge, the 30d-drift sparkline reused from
// pocMigrationSparkline(), and a 2-row 30d/90d ladder.
//
// The card is wrapped in `.poc-card[data-poc-coin-id]` so the existing
// wirePocDetail() listener picks up the click and opens the rich
// pocDetailHtml() modal — no new click wiring needed.
//
// Intentionally does NOT reuse pocDetailHtml() directly: that one is
// designed for a full-width modal (volume profile + naked POCs + 4-col
// table) and dwarfs the Signal card when forced into a 2-col layout.
// Side-by-side display in the symbol modal is the whole point of this
// helper.
function pocCompactCardHtml(coin){
  if (!coin) return '';
  const sym = escapeHtml(String(coin.symbol || coin.coin_id || '').toUpperCase());
  const cid = escapeHtml(String(coin.coin_id || coin.symbol || ''));
  const d = coin.poc || {};
  const anchor = d.d90 || d.d30 || d.d180 || null;
  // Stocks expose `last_price`; crypto entries use `current_price`. Either
  // works as the "Current" readout — fall back through the POC anchor's
  // own `current` (last close at compute time) as a last resort.
  const cur = coin.current_price != null
    ? coin.current_price
    : (coin.last_price != null ? coin.last_price : (anchor && anchor.current));
  const priceTxt = fmtUsdShort(cur);
  // Migration chip — UP / DOWN / FLAT — mirrors the POC tab styling.
  const mig = d.migration;
  let migChip = '';
  if (mig){
    const dlt = Number(mig.delta_pct);
    const dltTxt = isFinite(dlt) ? ((dlt >= 0 ? '+' : '') + dlt.toFixed(2) + '%') : '';
    const cfg = mig.direction === 'UP'
      ? {bg:'#22c55e22', fg:'#22c55e', arrow:'↑', label:'UP'}
      : mig.direction === 'DOWN'
      ? {bg:'#ef444422', fg:'#ef4444', arrow:'↓', label:'DOWN'}
      : {bg:'#6b728022', fg:'var(--muted)', arrow:'·', label:'FLAT'};
    migChip = `<span style="background:${cfg.bg};color:${cfg.fg};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;white-space:nowrap">${cfg.arrow} ${cfg.label}${dltTxt ? ' ' + dltTxt : ''}</span>`;
  }
  // 90d POC distance + IN VA / OUT badge.
  const pocPrice = anchor ? anchor.poc : null;
  const distPct  = anchor && anchor.distance_pct != null ? Number(anchor.distance_pct) : null;
  const dColor = distPct == null ? 'var(--muted)' : (distPct >= 0 ? '#22c55e' : '#ef4444');
  const dTxt   = distPct == null ? '—' : ((distPct >= 0 ? '+' : '') + distPct.toFixed(2) + '%');
  const inVA = anchor && anchor.in_value_area;
  const vaTag = anchor
    ? (inVA
        ? '<span style="background:#22c55e22;color:#22c55e;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600">IN VA</span>'
        : '<span style="background:#f59e0b22;color:#f59e0b;padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600">OUT</span>')
    : '';
  // Mini ladder — 30d / 90d POC + distance%. 180d intentionally omitted to
  // keep the card visually paired with the Signal card height.
  const LADDER_TFS = [['d30','30d'], ['d90','90d']];
  const ladder = LADDER_TFS.map(([k, label]) => {
    const r = d[k];
    if (!r) {
      return `<tr><td style="color:var(--muted);padding:3px 6px">${label}</td><td colspan="2" style="color:var(--muted);padding:3px 6px">—</td></tr>`;
    }
    const dc = r.distance_pct == null ? 'var(--muted)' : (r.distance_pct >= 0 ? '#22c55e' : '#ef4444');
    const dt = r.distance_pct == null ? '—' : (r.distance_pct >= 0 ? '+' : '') + Number(r.distance_pct).toFixed(1) + '%';
    return `<tr>
      <td style="color:var(--muted);padding:3px 6px">${label}</td>
      <td style="font-weight:600;padding:3px 6px">${fmtUsdShort(r.poc)}</td>
      <td style="color:${dc};text-align:right;padding:3px 6px;font-weight:600">${dt}</td>
    </tr>`;
  }).join('');
  const sparkline = pocMigrationSparkline(d.migration_series);
  // Whole card is the click target — reuses wirePocDetail() in app.py.
  return `<div class="chart-card poc-card" data-poc-coin-id="${cid}" role="button" tabindex="0" aria-label="Open ${sym} full POC detail" title="Click for full POC breakdown" style="cursor:pointer;display:flex;flex-direction:column;gap:8px">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:8px;min-width:0">
        <h2 style="margin:0;font-size:13px;font-weight:600">Point of Control</h2>
        ${migChip}
      </div>
      <div style="text-align:right">
        <div class="sub" style="font-size:10px;color:var(--muted)">Current</div>
        <div style="font-size:14px;font-weight:700">${priceTxt}</div>
      </div>
    </div>
    ${anchor ? `<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;padding:6px 8px;border:1px solid var(--border);border-radius:6px;background:#0b0d12">
      <span class="sub" style="font-size:10px;color:var(--muted)">vs 90d POC ${fmtUsdShort(pocPrice)}</span>
      <span style="color:${dColor};font-weight:700;font-size:13px">${dTxt}</span>
      ${vaTag}
    </div>` : ''}
    ${sparkline ? `<div><div class="sub" style="font-size:10px;color:var(--muted);margin-bottom:2px">30d POC drift · last 90d</div>${sparkline}</div>` : ''}
    <div>
      <table style="width:100%;font-size:12px;border-collapse:collapse">
        <thead><tr style="color:var(--muted);font-size:9px;text-align:left">
          <th style="padding:3px 6px">Window</th>
          <th style="padding:3px 6px">POC</th>
          <th style="text-align:right;padding:3px 6px">Δ vs price</th>
        </tr></thead>
        <tbody>${ladder}</tbody>
      </table>
    </div>
    <div class="sub" style="font-size:10px;color:var(--muted);text-align:right">click for full breakdown ›</div>
  </div>`;
}

// Empty state for when poc_top has no entry for a symbol (e.g. stocks like
// NVDA — POC is crypto-only in this build — or a crypto outside the
// top-50-by-score window). Visually matches a regular chart-card so the
// 2-col layout doesn't lopsidedly leave one slot blank.
function pocEmptyCardHtml(sym, kind){
  const safeSym = escapeHtml(String(sym || '').toUpperCase());
  const isStock = kind === 'stock';
  const msg = isStock
    ? `Not enough trading history for <strong>${safeSym}</strong> to compute a volume profile.`
    : `<strong>${safeSym}</strong> isn't in the top-50 crypto POC window.`;
  const sub = isStock
    ? 'Stock POC needs at least 30 daily bars from Yahoo. Recent IPOs and thinly-traded tickers may not have enough history yet.'
    : 'The POC tab tracks the top 50 cryptos by signal score; symbols outside that set fall back to this empty state.';
  return `<div class="chart-card" style="display:flex;flex-direction:column;gap:8px;opacity:.85">
    <div style="display:flex;align-items:center;gap:8px">
      <h2 style="margin:0;font-size:13px;font-weight:600">Point of Control</h2>
      <span class="tag" style="font-size:9px">not available</span>
    </div>
    <div class="empty" style="padding:18px 8px;font-size:12px;line-height:1.5">
      <div style="font-size:24px;margin-bottom:6px">📊</div>
      <div>${msg}</div>
      <div class="sub" style="font-size:11px;color:var(--muted);margin-top:8px">${sub}</div>
    </div>
  </div>`;
}

async function lookupSymbol(query){
  // Multi-symbol entry point. Accepts a raw input string; splits on
  // comma/semicolon/whitespace, strips cashtag prefixes ($BTC → BTC),
  // dedupes, caps at 6, and routes to the single- or multi-symbol
  // render path. Empty input → no-op.
  const tokens = parseSymbolSearchTokens(query);
  if (!tokens.length) return;
  if (tokens.length > 1){
    return lookupSymbolsMulti(tokens);
  }
  const sym = tokens[0];
  const symLower = sym.toLowerCase();
  const market = DATA.market || {};
  // Compare A to a constant pre-uppercased value B. A few callers pass coin
  // IDs (lowercase) so we have to upper-case both sides every time — keep
  // the helper small enough that it can be inlined by the JIT.
  const eq = (a, b) => String(a || '').toUpperCase() === b;

  // 1) Stock signal (top-50 most active US stocks).
  const stock = (market.stocks_signals || []).find(s => s && eq(s.symbol, sym));
  // 2) Crypto signal — primary source: signals_top20 (computed from top-50
  //    markets_top, stables excluded, sorted by score).
  let cryptoSignal = (DATA.signals_top20 || []).find(s => s && eq(s.symbol, sym));
  // 3) POC entry (top-50 by score — covers more obscure coins than
  //    signals_top20). May be empty if poc_top fetch was rate-limited.
  //    Stocks compute their own POC inline (compute_stock_poc); fall back
  //    to the stock row when there's no crypto match.
  const cryptoPoc = (market.poc_top || []).find(r => r && (eq(r.symbol, sym) || eq(r.coin_id, sym)));
  const poc = cryptoPoc || (stock && stock.poc ? stock : null);
  // 4) markets_top backup (top-25 by mcap) — covers stables (USDT, USDC)
  //    and any coin filtered out of signals_top20.
  const marketTop = (market.markets_top || []).find(r => r && (eq(r.symbol, sym) || eq(r.id, sym)));
  // 5) Pinned-asset full series (BTC/ETH/LINK/LTC). market[<lower>] holds
  //    the full price/volume history when the symbol is one of the four
  //    we pin.
  const pinnedAsset = ({btc:1, eth:1, link:1, ltc:1}[symLower]) ? (market[symLower] || null) : null;
  // 6) Full signal detail for the two assets that get the rich 6-component
  //    scorer (BTC, ETH). When present this is much richer than the
  //    signals_top20 entry, so prefer it.
  const fullSignal = ({btc:1, eth:1}[symLower]) ? ((DATA.signals || {})[symLower] || null) : null;
  if (!cryptoSignal && fullSignal){
    // Reshape the full-signal output to match renderSignalCardFromObj's
    // expected shape (symbol/name/image fields).
    cryptoSignal = Object.assign({}, fullSignal, {
      symbol: sym, name: fullSignal.name || sym, image: null,
    });
  }
  // 7) News — case-insensitive whole-word match against title/body
  let newsRegex = null;
  try { newsRegex = new RegExp('\\b' + sym.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&') + '\\b', 'i'); }
  catch(_) { newsRegex = null; }
  const news = (market.news || []).filter(n => {
    if (!n) return false;
    const t = String(n.title || '');
    const b = String(n.body || '');
    if (newsRegex){
      return newsRegex.test(t) || newsRegex.test(b);
    }
    const up = sym;
    return t.toUpperCase().includes(up) || b.toUpperCase().includes(up);
  });
  // 8) Sentiment (CryptoCompare news)
  const sentiment = ((((market.social || {}).cc_news || {}).coins) || {})[symLower] || null;

  // 9) Full-name substring fallback — try matching the user's input against
  //    the `name` field on signals_top20 / markets_top / stocks_signals. Only
  //    triggers when NO direct symbol hit was found.
  let nameMatchSignal = null;
  let nameMatchMarketTop = null;
  let nameMatchStock = null;
  const hasDirectHit = !!(stock || cryptoSignal || poc || marketTop || pinnedAsset);
  if (!hasDirectHit){
    const needle = sym;  // already uppercased
    const nameContains = (n) => n && String(n).toUpperCase().includes(needle);
    nameMatchSignal     = (DATA.signals_top20 || []).find(s => s && nameContains(s.name));
    nameMatchMarketTop  = (market.markets_top || []).find(r => r && nameContains(r.name));
    nameMatchStock      = (market.stocks_signals || []).find(s => s && nameContains(s.name));
  }

  const effectiveStock  = stock || nameMatchStock;
  const effectiveSignal = cryptoSignal || nameMatchSignal;
  const effectiveMarketTop = marketTop || nameMatchMarketTop;

  const hasAny = !!(effectiveStock || effectiveSignal || poc || effectiveMarketTop ||
                    pinnedAsset || news.length || sentiment);
  const modal = document.getElementById('symbolDetailModal');
  const body  = document.getElementById('symbolDetailBody');
  const title = document.getElementById('symbolDetailTitle');
  if (!modal || !body || !title) return;

  const displayName =
    (effectiveStock && effectiveStock.name) ||
    (effectiveSignal && effectiveSignal.name) ||
    (poc && poc.name) ||
    (effectiveMarketTop && effectiveMarketTop.name) ||
    (pinnedAsset && ({btc:'Bitcoin', eth:'Ethereum', link:'Chainlink', ltc:'Litecoin'}[symLower])) ||
    '';
  title.textContent = displayName ? (sym + ' · ' + displayName) : sym;

  if (!hasAny){
    // No cached match — show spinner immediately, then try the live fallbacks
    // in this order:
    //   1) Local Flask server `/api/symbol/<sym>` (only when we're on the
    //      same-origin live-server mode — has Yahoo + full 6-component scorer
    //      and no per-day rate limit).
    //   2) CryptoCompare histoday (covers any crypto symbol, CORS-friendly).
    //   3) Live stock lookup via Twelvedata (Alpha Vantage as fallback) —
    //      only attempted when the symbol shape looks like a US ticker AND
    //      crypto came up empty.
    title.textContent = sym;
    body.innerHTML =
      '<div class="sub" style="color:var(--muted);padding:14px;text-align:center">' +
        'Fetching live data for <strong>' + escapeHtml(sym) + '</strong>&hellip;' +
      '</div>';
    modal.classList.remove('hidden');
    // Track this request so a fast follow-up doesn't render stale results.
    const reqId = (window._liveLookupReq = (window._liveLookupReq || 0) + 1);
    const stillCurrent = () => reqId === window._liveLookupReq;

    // 1) Server endpoint — only in local Flask mode.
    if (typeof isServer !== 'undefined' && isServer){
      try {
        const payload = await liveServerSymbolLookup(sym);
        if (!stillCurrent()) return;
        body.innerHTML = renderServerSymbolSection(sym, payload);
        pushSymbolRecent(sym);
        return;
      } catch (err){
        if (!stillCurrent()) return;
        // Fall through to the public-mirror code paths below.
      }
    }

    // 2) CryptoCompare — always cheap and works for any crypto.
    let cryptoErr = null;
    try {
      const rows = await liveCryptoLookup(sym);
      if (!stillCurrent()) return;
      body.innerHTML = renderLiveCryptoSection(sym, rows);
      pushSymbolRecent(sym);
      return;
    } catch (err){
      cryptoErr = err;
    }
    if (!stillCurrent()) return;

    // 3) Live stock lookup for stock-shaped symbols.
    if (liveLooksLikeStock(sym)){
      try {
        const out = await liveStockLookup(sym);
        if (!stillCurrent()) return;
        body.innerHTML = renderLiveStockSection(sym, out.rows, out.source);
        pushSymbolRecent(sym);
        return;
      } catch (err){
        if (!stillCurrent()) return;
        const suggestions = symbolFuzzyMatches(sym);
        if (err && err.code === 'NO_STOCK_API_KEY'){
          body.innerHTML = renderLiveStockNoKey(sym, suggestions);
          // Wire the "Add API key" button — on success, retry the lookup.
          const btn = document.getElementById('liveStockKeyBtn');
          if (btn){
            btn.addEventListener('click', () => {
              const k = liveStockPromptForKey();
              if (k) lookupSymbol(sym);
            });
          }
        } else {
          body.innerHTML = renderLiveStockFailed(sym, err && err.message, suggestions);
        }
        return;
      }
    }

    // Neither crypto nor stock-shaped — show a clear scoped-coverage message
    // plus any close-symbol suggestions from the cached payload (catches
    // common typos before the user thinks the dashboard is broken).
    const noMatchSuggestions = symbolFuzzyMatches(sym);
    body.innerHTML =
      '<div class="sub" style="color:var(--muted);padding:14px;text-align:center">' +
        'No data for <strong>' + escapeHtml(sym) + '</strong> &mdash; verify the ticker is in ' +
        'the top-25 crypto / top-50 stocks coverage, or try a different symbol.' +
      '</div>' +
      renderSymbolSuggestionsStrip(noMatchSuggestions);
    return;
  }

  // Reuse the shared section builder so single- and multi-symbol render
  // identically. Pass the already-resolved data (with name-substring
  // fallbacks promoted via effective*) to avoid a second lookup.
  body.innerHTML = buildSymbolSectionsHtml({
    sym: sym,
    stock: (typeof effectiveStock !== 'undefined' ? effectiveStock : stock),
    cryptoSignal: (typeof effectiveSignal !== 'undefined' ? effectiveSignal : cryptoSignal),
    poc: poc,
    news: news,
    sentiment: sentiment,
    displayName: displayName,
  });
  modal.classList.remove('hidden');
  pushSymbolRecent(sym);
}

// (Legacy inline section-building block was deleted here — it was
// orphaned dead code referencing an undeclared `sections` array,
// throwing a ReferenceError on every cached-symbol lookup after
// buildSymbolSectionsHtml was extracted upstream. That's why the
// modal would 'open once' and then never reappear.)

// Multi-symbol modal renderer. Opens the modal immediately with a spinner,
// then resolves each symbol in parallel and stacks the resulting cards.
// Found symbols render as <div class="multi-symbol-card"> blocks with an
// <h2> header. Misses are aggregated into a small footer note. If every
// token fails to resolve, the modal shows a single "Couldn't find any
// of: ..." error message.
async function lookupSymbolsMulti(tokens){
  const modal = document.getElementById('symbolDetailModal');
  const body  = document.getElementById('symbolDetailBody');
  const title = document.getElementById('symbolDetailTitle');
  if (!modal || !body || !title) return;
  title.textContent = tokens.join(' · ');
  body.innerHTML =
    '<div class="sub" style="color:var(--muted);padding:14px;text-align:center">' +
      'Looking up <strong>' + escapeHtml(tokens.join(', ')) + '</strong>&hellip;' +
    '</div>';
  modal.classList.remove('hidden');
  // Track this request so a fast follow-up doesn't render stale results.
  const reqId = (window._liveLookupReq = (window._liveLookupReq || 0) + 1);
  const results = await Promise.all(tokens.map(t => resolveAndRenderSymbol(t)));
  if (reqId !== window._liveLookupReq) return;
  const found = results.filter(r => r && r.found && r.html);
  const missed = results.filter(r => !(r && r.found && r.html)).map(r => r.sym);
  if (!found.length){
    body.innerHTML =
      '<div class="sub" style="color:var(--muted);padding:14px;text-align:center">' +
        'Couldn&rsquo;t find any of: <strong>' + escapeHtml(tokens.join(', ')) + '</strong>' +
      '</div>';
    return;
  }
  const cards = found.map(r => {
    const hdr =
      '<h2 style="margin:0 0 8px 0;font-size:15px;letter-spacing:0.3px">' +
        escapeHtml(r.sym) +
        (r.displayName ? ' <span class="sub" style="font-size:12px;color:var(--muted);font-weight:400">· ' + escapeHtml(r.displayName) + '</span>' : '') +
      '</h2>';
    return (
      '<div class="multi-symbol-card" style="padding:10px 0;border-top:1px solid var(--border)">' +
        hdr + r.html +
      '</div>'
    );
  }).join('');
  const footer = missed.length
    ? '<div class="sub" style="color:var(--muted);font-size:11px;padding-top:8px;border-top:1px solid var(--border);margin-top:6px">' +
        'Couldn&rsquo;t find: <strong>' + escapeHtml(missed.join(', ')) + '</strong>' +
      '</div>'
    : '';
  body.innerHTML = cards + footer;
}

function closeSymbolDetail(){
  const m = document.getElementById('symbolDetailModal');
  if (m) m.classList.add('hidden');
}

// Build the deduped suggestion list for the header symbol typeahead. Walks
// three sources (top-50 stocks + top-25 crypto markets + scored top-20
// crypto signals) once per keystroke. Prefix-match on the symbol field is
// preferred — it surfaces "BTC" when the user types "B" even though dozens
// of names also contain a "b". Substring-on-name is a fallback so a user
// who knows the brand ("Nvidia", "Solana") but not the ticker still gets
// hits. Dedupe by uppercased symbol so the same coin doesn't appear twice
// when it lives in both markets_top and signals_top20.
function buildSymbolSuggestions(rawQuery){
  const q = String(rawQuery || '').trim();
  if (!q) return [];
  const qUp = q.toUpperCase();
  const qLo = q.toLowerCase();
  const market = DATA.market || {};
  const sources = [
    { rows: market.markets_top || [],   kind: 'crypto' },
    { rows: DATA.signals_top20 || [],   kind: 'crypto' },
    { rows: market.stocks_signals || [],kind: 'stock'  },
  ];
  const seen = new Set();
  const prefixHits = [];
  const nameHits = [];
  for (const src of sources){
    for (const row of src.rows){
      if (!row) continue;
      const sym = String(row.symbol || '').toUpperCase();
      if (!sym || seen.has(sym)) continue;
      const name = String(row.name || '');
      const symPrefix = sym.startsWith(qUp);
      const nameSub = name && name.toLowerCase().includes(qLo);
      if (!symPrefix && !nameSub) continue;
      seen.add(sym);
      const entry = { symbol: sym, name: name, kind: src.kind };
      if (symPrefix) prefixHits.push(entry);
      else nameHits.push(entry);
      if (prefixHits.length + nameHits.length >= 32) break;
    }
  }
  // Prefix matches first (more relevant), name-substring matches after.
  return prefixHits.concat(nameHits).slice(0, 8);
}

function renderSymbolSuggestions(list){
  const box = document.getElementById('symbolSearchSuggest');
  const input = document.getElementById('symbolSearchInput');
  if (!box) return;
  const esc = s => String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
  // Empty list ≠ silent. When the user has typed something but nothing
  // matches the cache, show a single "Press Enter to search live" hint so
  // they get visible feedback that the form IS working. Pressing Enter
  // routes through `lookupSymbol` which handles the live-lookup chain
  // (including the fuzzy-suggest chips in the modal fallback).
  const query = (input && input.value || '').trim();
  if (!list || !list.length){
    if (!query){
      box.innerHTML = '';
      box.classList.add('hidden');
      if (input) input.setAttribute('aria-expanded', 'false');
      return;
    }
    box.innerHTML =
      '<div class="symbol-suggest-row symbol-suggest-empty" role="option" ' +
        'data-symbol="' + esc(query.toUpperCase()) + '" tabindex="-1" ' +
        'style="opacity:.85">' +
        '<span class="symbol-suggest-sym">↵</span>' +
        '<span class="symbol-suggest-name">Press Enter to search <strong>' +
          esc(query.toUpperCase()) + '</strong></span>' +
      '</div>';
    box.classList.remove('hidden');
    if (input) input.setAttribute('aria-expanded', 'true');
    return;
  }
  const html = list.map((r, i) =>
    '<div class="symbol-suggest-row" role="option" data-symbol="' + esc(r.symbol) +
    '" data-idx="' + i + '" tabindex="-1">' +
      '<span class="symbol-suggest-sym">' + esc(r.symbol) + '</span>' +
      '<span class="symbol-suggest-name">' + esc(r.name || '') + '</span>' +
    '</div>'
  ).join('');
  box.innerHTML = html;
  box.classList.remove('hidden');
  if (input) input.setAttribute('aria-expanded', 'true');
}

function _setSymbolSuggestActive(box, idx){
  if (!box) return;
  const rows = box.querySelectorAll('.symbol-suggest-row');
  rows.forEach(r => r.classList.remove('active'));
  if (idx == null || idx < 0 || idx >= rows.length){
    box._activeIdx = -1;
    return;
  }
  rows[idx].classList.add('active');
  box._activeIdx = idx;
  // Keep highlighted row visible inside the scroll container.
  const r = rows[idx];
  if (r && r.scrollIntoView) r.scrollIntoView({block:'nearest'});
}

(function wireSymbolSearch(){
  if (window._symbolSearchWired) return; window._symbolSearchWired = true;
  const form = document.getElementById('symbolSearchForm');
  const input = document.getElementById('symbolSearchInput');
  const suggest = document.getElementById('symbolSearchSuggest');
  if (form){
    form.addEventListener('submit', e => {
      e.preventDefault();
      if (suggest){ suggest.classList.add('hidden'); suggest._activeIdx = -1; }
      if (input){
        input.setAttribute('aria-expanded', 'false');
        lookupSymbol(input.value);
      }
    });
  }

  // Debounced typeahead: rebuild + render at most every ~120ms while typing.
  if (input){
    let debounceTimer = null;
    input.addEventListener('input', () => {
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        const list = buildSymbolSuggestions(input.value);
        renderSymbolSuggestions(list);
        if (suggest) suggest._activeIdx = -1;
      }, 120);
    });
    // Re-show suggestions on focus if there's still a query — covers the
    // case where the user blurred to click elsewhere then returned.
    input.addEventListener('focus', () => {
      if (!input.value) return;
      const list = buildSymbolSuggestions(input.value);
      renderSymbolSuggestions(list);
    });
    // Keyboard navigation: arrows + enter + escape.
    input.addEventListener('keydown', e => {
      if (!suggest || suggest.classList.contains('hidden')) return;
      const rows = suggest.querySelectorAll('.symbol-suggest-row');
      if (!rows.length) return;
      const cur = typeof suggest._activeIdx === 'number' ? suggest._activeIdx : -1;
      if (e.key === 'ArrowDown'){
        e.preventDefault();
        _setSymbolSuggestActive(suggest, (cur + 1) % rows.length);
      } else if (e.key === 'ArrowUp'){
        e.preventDefault();
        _setSymbolSuggestActive(suggest, cur <= 0 ? rows.length - 1 : cur - 1);
      } else if (e.key === 'Enter' && cur >= 0){
        e.preventDefault();
        const sym = rows[cur].getAttribute('data-symbol');
        if (sym){
          input.value = sym;
          suggest.classList.add('hidden');
          suggest._activeIdx = -1;
          input.setAttribute('aria-expanded', 'false');
          lookupSymbol(sym);
        }
      } else if (e.key === 'Escape'){
        suggest.classList.add('hidden');
        suggest._activeIdx = -1;
        input.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // Event-delegated click handler — bound once on the container, not per
  // keystroke. Picking a row fills the input and submits the form.
  if (suggest){
    suggest.addEventListener('click', e => {
      const row = e.target && e.target.closest && e.target.closest('.symbol-suggest-row');
      if (!row) return;
      const sym = row.getAttribute('data-symbol');
      if (!sym || !input) return;
      input.value = sym;
      suggest.classList.add('hidden');
      suggest._activeIdx = -1;
      input.setAttribute('aria-expanded', 'false');
      lookupSymbol(sym);
    });
    // Prevent the global document click-listener below from closing the
    // dropdown when the user is mousedown-ing on a row.
    suggest.addEventListener('mousedown', e => { e.preventDefault(); });
  }

  document.addEventListener('click', e => {
    if (e.target && e.target.id === 'symbolDetailClose') closeSymbolDetail();
    if (e.target && e.target.id === 'symbolDetailModal') closeSymbolDetail();
    // Close the suggestion dropdown on any click outside the input/dropdown.
    if (suggest && !suggest.classList.contains('hidden')){
      const t = e.target;
      if (!(t === input || (suggest.contains && suggest.contains(t)) ||
            (form && form.contains && form.contains(t)))){
        suggest.classList.add('hidden');
        suggest._activeIdx = -1;
        if (input) input.setAttribute('aria-expanded', 'false');
      }
    }
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeSymbolDetail();
  });
  // Initial paint of any previously-stored recents (no-op if list is empty).
  try { renderSymbolRecentChips(); } catch (_) { /* defensive — never block boot */ }
})();

// ─── UAP / MUFON TAB (ported from V2 — V1 visual style) ───────────────────
// Five sections rendered independently. Updates + Docs are populated from
// JS-local constants (curated content, no fetch); Trend / Map / Shapes are
// populated from the data-mufon.json sidecar (lazy-loaded, may be undefined
// on first paint — every renderer guards `if (!m) return;`).
//
// V2's empty-state branches call V2.empty(...). V1 has no V2 object, so we
// define a minimal shim here (inline-styled so it doesn't depend on V2's
// .v2-empty* CSS classes). Only the bits the mufon code uses are provided.
const V2 = {
  empty: function(opts){
    const o = opts || {};
    return '<div style="text-align:center;padding:28px 16px;color:var(--muted)">'
      + (o.icon  ? '<div style="font-size:28px;margin-bottom:8px">' + escapeHtml(String(o.icon))  + '</div>' : '')
      + (o.title ? '<div style="font-size:14px;font-weight:600;color:var(--text);margin-bottom:4px">' + escapeHtml(String(o.title)) + '</div>' : '')
      + (o.sub   ? '<div style="font-size:12px;line-height:1.5">' + escapeHtml(String(o.sub))   + '</div>' : '')
      + '</div>';
  }
};

// Curated content — edit these arrays to update the tab. Keeping it inline
// (not a fetcher) because PURSUE drops and AARO updates land sporadically
// and the operator wants to vet every line before it appears.
const MUFON_UPDATES = [
  {
    date: '2026-05-22',
    title: 'PURSUE Release 02',
    body: 'Department of War published the second tranche of UAP files at war.gov/ufo. Source counts vary; treat the headline number as "additional files released" rather than a precise figure.',
    href: 'https://www.war.gov/ufo/',
  },
  {
    date: '2026-05-08',
    title: 'PURSUE Release 01',
    body: 'First batch — 162 declassified files dating back to 1944–45 — released at war.gov/ufo. Coordinated across Pentagon, ODNI, FBI, NASA, DOE.',
    href: 'https://www.war.gov/ufo/',
  },
  {
    date: '2026-02-25',
    title: 'AARO caseload exceeds 2,000',
    body: 'All-domain Anomaly Resolution Office reported its case count crossed 2,000, up from ~1,600 in late 2024.',
    href: 'https://www.aaro.mil/',
  },
];

const MUFON_DOCS = [
  { title: 'PURSUE portal', url: 'https://www.war.gov/ufo/',
    desc: 'Department of War — current UAP file releases.' },
  { title: 'AARO',          url: 'https://www.aaro.mil/',
    desc: 'All-domain Anomaly Resolution Office — DoD UAP intake.' },
  { title: 'NARA — Project BLUE BOOK', url: 'https://www.archives.gov/research/military/air-force/ufos',
    desc: 'National Archives — Air Force UFO investigation records.' },
  { title: 'CIA Reading Room — UFOs', url: 'https://www.cia.gov/readingroom/collection/ufos-fact-or-fiction',
    desc: 'CIA — Declassified UFO Cold War material.' },
  { title: 'FBI Vault — UFO', url: 'https://vault.fbi.gov/UFO',
    desc: 'FBI — Public reading room UFO files.' },
  { title: 'ODNI 2024 Consolidated UAP Report', url: 'https://www.dni.gov/index.php/newsroom/reports-publications/reports-publications-2024/4020-uap-2024',
    desc: 'Director of National Intelligence — Congressional UAP report.' },
];

// State-tile grid (50 + DC) — [code, col, row], 12 cols x 8 rows. Hand-tuned
// for geographic intuitiveness; not a precise outline (deliberately).
const MUFON_STATE_GRID = [
  ["ME",11,0],["VT",10,1],["NH",11,1],
  ["WA",1,2],["MT",3,2],["ND",4,2],["MN",5,2],["WI",6,2],["MI",7,2],["NY",9,2],["MA",10,2],["RI",11,2],
  ["OR",1,3],["ID",2,3],["WY",3,3],["SD",4,3],["IA",5,3],["IL",6,3],["IN",7,3],["OH",8,3],["PA",9,3],["NJ",10,3],["CT",11,3],
  ["NV",2,4],["UT",3,4],["CO",4,4],["NE",5,4],["MO",6,4],["KY",7,4],["WV",8,4],["VA",9,4],["MD",10,4],["DE",11,4],
  ["CA",1,5],["AZ",3,5],["NM",4,5],["KS",5,5],["AR",6,5],["TN",7,5],["NC",8,5],["SC",9,5],["DC",10,5],
  ["OK",5,6],["LA",6,6],["MS",7,6],["AL",8,6],["GA",9,6],
  ["AK",0,7],["TX",5,7],["FL",10,7],["HI",11,7],
];

const MUFON_STATE_NAMES = {
  AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",CO:"Colorado",
  CT:"Connecticut",DE:"Delaware",DC:"District of Columbia",FL:"Florida",GA:"Georgia",
  HI:"Hawaii",ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",KY:"Kentucky",
  LA:"Louisiana",ME:"Maine",MD:"Maryland",MA:"Massachusetts",MI:"Michigan",MN:"Minnesota",
  MS:"Mississippi",MO:"Missouri",MT:"Montana",NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",
  NJ:"New Jersey",NM:"New Mexico",NY:"New York",NC:"North Carolina",ND:"North Dakota",
  OH:"Ohio",OK:"Oklahoma",OR:"Oregon",PA:"Pennsylvania",RI:"Rhode Island",SC:"South Carolina",
  SD:"South Dakota",TN:"Tennessee",TX:"Texas",UT:"Utah",VT:"Vermont",VA:"Virginia",
  WA:"Washington",WV:"West Virginia",WI:"Wisconsin",WY:"Wyoming",
};

function renderMufon(){
  renderMufonUpdates();
  renderMufonDocs();
  // Trend + Map are both gated on the data-mufon.json sidecar — if not yet
  // loaded, the loading placeholders elsewhere handle the empty state.
  renderMufonTrend();
  renderMufonMap();
  renderMufonShapes();
}

function renderMufonUpdates(){
  const el = document.getElementById('mufonUpdatesBody');
  if (!el) return;
  const items = MUFON_UPDATES.map(u => {
    const linkOpen  = u.href ? '<a href="'+u.href+'" target="_blank" rel="noopener noreferrer" style="color:inherit;text-decoration:none">' : '';
    const linkClose = u.href ? ' <span style="font-size:10px;color:var(--btc);text-decoration:underline">source ↗</span></a>' : '';
    return ''
      + '<div style="display:flex;gap:12px;padding:12px 0;border-top:1px solid var(--bd,#27313d)">'
      +   '<div style="flex:0 0 90px"><span class="v2-chip v2-chip--info" style="font-variant-numeric:tabular-nums">'+u.date+'</span></div>'
      +   '<div style="flex:1;min-width:0">'
      +     linkOpen
      +     '<div style="font-weight:600;margin-bottom:3px">'+u.title+linkClose+'</div>'
      +     (u.href ? '' : '</div>')
      +     '<div style="color:var(--muted);font-size:12px;line-height:1.45">'+u.body+'</div>'
      +   '</div>'
      + '</div>';
  }).join('');
  // Strip the leading border-top by zeroing the first item's border.
  el.innerHTML = items.replace('border-top:1px solid var(--bd,#27313d)', 'border-top:0');
}

function renderMufonDocs(){
  const el = document.getElementById('mufonDocsBody');
  if (!el) return;
  const cards = MUFON_DOCS.map(d => ''
    + '<a href="'+d.url+'" target="_blank" rel="noopener noreferrer" '
    +    'style="display:block;padding:12px;background:var(--bg2,#0f1419);'
    +    'border:1px solid var(--bd,#27313d);border-radius:8px;text-decoration:none;color:inherit;'
    +    'transition:border-color .15s ease, transform .15s ease" '
    +    'onmouseover="this.style.borderColor=\'var(--btc)\';this.style.transform=\'translateY(-1px)\'" '
    +    'onmouseout="this.style.borderColor=\'var(--bd,#27313d)\';this.style.transform=\'\'">'
    +   '<div style="font-weight:600;font-size:13px;margin-bottom:4px">'+d.title+' <span style="color:var(--btc);font-size:11px">↗</span></div>'
    +   '<div style="font-size:11px;color:var(--muted);line-height:1.4;margin-bottom:6px">'+d.desc+'</div>'
    +   '<div style="font-size:10px;color:var(--muted);font-family:monospace;word-break:break-all;opacity:.7">'+d.url.replace(/^https?:\/\//,'')+'</div>'
    + '</a>').join('');
  el.innerHTML = '<div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(220px, 1fr));gap:10px">' + cards + '</div>';
}

// ─── UAP SIGHTINGS TREND SIGNAL ───────────────────────────────────────────
// Drop the most-recent year if it looks partial (fewer than ~85% of the
// rolling 3y mean of full years). Returns the (years, counts) arrays with the
// partial year stripped AND a separate handle on the partial year.
function mufonSplitPartialYear(totalsByYear){
  const years = Object.keys(totalsByYear).map(Number).filter(y => isFinite(y)).sort((a,b)=>a-b);
  if (years.length < 4) return { full: years, partial: null, totals: totalsByYear };
  const last = years[years.length - 1];
  const prev3 = years.slice(-4, -1);  // 3 years before the last
  const prev3Mean = prev3.reduce((s,y) => s + (totalsByYear[String(y)] || 0), 0) / prev3.length;
  const lastVal = totalsByYear[String(last)] || 0;
  if (prev3Mean > 0 && lastVal < prev3Mean * 0.85) {
    return { full: years.slice(0, -1), partial: last, totals: totalsByYear };
  }
  return { full: years, partial: null, totals: totalsByYear };
}

// Map a 5y CAGR to a -100..+100 score with diminishing returns near the
// extremes (atan-based, like the crypto components).
function mufonCagrToScore(cagr){
  if (!isFinite(cagr)) return 0;
  const k = 0.04; // sensitivity
  const s = Math.atan(cagr / k) * (200 / Math.PI);
  return Math.max(-100, Math.min(100, Math.round(s)));
}

// Linear regression slope of {y_i = totals[year_i]} on year. Returns the
// slope in sightings-per-year. Pure stdlib (no library).
function mufonLinearSlope(years, totals){
  const n = years.length;
  if (n < 2) return null;
  const ys = years.map(String).map(s => Number(totals[s]) || 0);
  const mx = years.reduce((a,b) => a + b, 0) / n;
  const my = ys.reduce((a,b) => a + b, 0) / n;
  let num = 0, den = 0;
  for (let i = 0; i < n; i++){
    num += (years[i] - mx) * (ys[i] - my);
    den += (years[i] - mx) * (years[i] - mx);
  }
  if (den === 0) return null;
  return num / den;
}

// Month-keyed twin of mufonTrendSparkline. ``months`` is a list of "YYYY-MM"
// strings in ascending order; ``totals`` is the m.totals_by_month dict.
function mufonTrendSparklineMonths(months, totals){
  const n = months.length;
  if (n < 2) return '';
  const ys = months.map(function(s){ return Number(totals[s]) || 0; });
  const lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
  const range = (hi - lo) || 1;
  const w = 600, h = 90, padL = 4, padR = 4, padTop = 14, padBot = 18;
  const innerW = w - padL - padR;
  const innerH = h - padTop - padBot;
  const xFor = function(i){ return padL + (i / (n - 1)) * innerW; };
  const yFor = function(v){ return padTop + (1 - (v - lo) / range) * innerH; };
  const pts = months.map(function(m, i){ return xFor(i) + ',' + yFor(ys[i]).toFixed(1); }).join(' ');
  const first = ys[0], last = ys[n - 1];
  const flatTol = Math.max(Math.abs(first) * 0.10, 10);
  const stroke = Math.abs(last - first) <= flatTol
    ? 'var(--v2-warn, #fbbf24)'
    : (last >= first ? 'var(--v2-good, #4ade80)' : 'var(--v2-bad, #f87171)');
  const startLabel = '<text x="' + xFor(0).toFixed(1) + '" y="' + (h - 4) + '" '
    + 'text-anchor="start" font-size="9" fill="var(--muted)">' + months[0] + '</text>';
  const endLabel = '<text x="' + xFor(n - 1).toFixed(1) + '" y="' + (h - 4) + '" '
    + 'text-anchor="end" font-size="9" fill="var(--muted)">' + months[n - 1] + ' · ' + last.toLocaleString() + '</text>';
  return ''
    + '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" '
    +   'style="width:100%;height:90px;display:block;background:#0b0d12;border-radius:6px" '
    +   'role="img" aria-label="Monthly UAP sighting reports">'
    +   '<polyline points="' + pts + '" fill="none" stroke="' + stroke + '" stroke-width="1.6" '
    +     'vector-effect="non-scaling-stroke"/>'
    +   startLabel
    +   endLabel
    + '</svg>';
}

// Inline sparkline for the full totals_by_year series.
function mufonTrendSparkline(years, totals, peakYear){
  const n = years.length;
  if (n < 2) return '';
  const ys = years.map(String).map(s => Number(totals[s]) || 0);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const range = (hi - lo) || 1;
  const w = 600, h = 90, padL = 4, padR = 4, padTop = 14, padBot = 18;
  const innerW = w - padL - padR;
  const innerH = h - padTop - padBot;
  const xMin = years[0], xMax = years[n - 1];
  const xSpan = (xMax - xMin) || 1;
  const xFor = y => padL + ((y - xMin) / xSpan) * innerW;
  const yFor = v => padTop + (1 - (v - lo) / range) * innerH;
  const pts = years.map((yr, i) => xFor(yr) + ',' + yFor(ys[i]).toFixed(1)).join(' ');
  const first = ys[0], last = ys[n - 1];
  const flatTol = Math.max(Math.abs(first) * 0.10, 10);
  const stroke = Math.abs(last - first) <= flatTol
    ? 'var(--v2-warn, #fbbf24)'
    : (last >= first ? 'var(--v2-good, #4ade80)' : 'var(--v2-bad, #f87171)');
  const pi = peakYear != null ? years.indexOf(Number(peakYear)) : -1;
  const peakMarker = (pi >= 0) ? (
    '<circle cx="' + xFor(years[pi]).toFixed(1) + '" cy="' + yFor(ys[pi]).toFixed(1) + '" r="2.5" '
    + 'fill="#fff" stroke="' + stroke + '" stroke-width="1.5"/>'
    + '<text x="' + xFor(years[pi]).toFixed(1) + '" y="' + Math.max(8, yFor(ys[pi]) - 5).toFixed(1) + '" '
    + 'text-anchor="middle" font-size="9" fill="var(--muted)">peak ' + years[pi] + '</text>'
  ) : '';
  const startLabel = '<text x="' + xFor(years[0]).toFixed(1) + '" y="' + (h - 4) + '" '
    + 'text-anchor="start" font-size="9" fill="var(--muted)">' + years[0] + '</text>';
  const endLabel = '<text x="' + xFor(years[n - 1]).toFixed(1) + '" y="' + (h - 4) + '" '
    + 'text-anchor="end" font-size="9" fill="var(--muted)">' + years[n - 1] + ' · ' + last.toLocaleString() + '</text>';
  return ''
    + '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" '
    +   'style="width:100%;height:90px;display:block;background:#0b0d12;border-radius:6px" '
    +   'role="img" aria-label="Annual UAP sighting reports">'
    +   '<polyline points="' + pts + '" fill="none" stroke="' + stroke + '" stroke-width="1.6" '
    +     'vector-effect="non-scaling-stroke"/>'
    +   peakMarker
    +   startLabel
    +   endLabel
    + '</svg>';
}

function renderMufonTrend(){
  const m = DATA.mufon;
  const body = document.getElementById('mufonTrendBody');
  const asOf = document.getElementById('mufonTrendAsOf');
  if (!body) return;
  if (!m) return; // sidecar pending; selectTab() shows the loading placeholder

  // Empty-state — fetcher wrote an _error placeholder.
  if (!m.total_records || !m.totals_by_year) {
    if (asOf) asOf.textContent = '● unavailable';
    body.innerHTML = V2.empty({icon:'🛸', title:'No trend data',
      sub: m._error || 'Annual totals unavailable; retry on next dashboard refresh.',
      warm:false});
    return;
  }

  if (asOf) {
    const d = (m.date_range && m.date_range[1]) || '—';
    asOf.textContent = '● to ' + d + (m._stale ? ' (stale)' : '');
  }

  const totals = m.totals_by_year || {};
  const split = mufonSplitPartialYear(totals);
  const fullYears = split.full;          // years to actually fit a trend on
  const partialYear = split.partial;     // 2014 typically — flagged, not used for math
  const peakYear = fullYears.reduce((best, y) =>
    (totals[String(y)] > (totals[String(best)] || 0) ? y : best),
    fullYears[0] || null);

  // 5y CAGR on the last 5 *full* years -> drives the headline score+chip.
  let cagr = null, score = 0;
  if (fullYears.length >= 5) {
    const yEnd = fullYears[fullYears.length - 1];
    const yStart = fullYears[fullYears.length - 5];
    const vEnd = Number(totals[String(yEnd)]) || 0;
    const vStart = Number(totals[String(yStart)]) || 0;
    const span = yEnd - yStart;
    if (vStart > 0 && span > 0) {
      cagr = Math.pow(vEnd / vStart, 1 / span) - 1;
      score = mufonCagrToScore(cagr);
    }
  }

  // 5y momentum: mean(last 5 full years) vs mean(prior 5 full years).
  let mom = null, momPrev = null, momCur = null;
  if (fullYears.length >= 10) {
    const tail5 = fullYears.slice(-5);
    const prev5 = fullYears.slice(-10, -5);
    momCur = tail5.reduce((s,y) => s + (Number(totals[String(y)]) || 0), 0) / 5;
    momPrev = prev5.reduce((s,y) => s + (Number(totals[String(y)]) || 0), 0) / 5;
    if (momPrev > 0) mom = momCur / momPrev - 1;
  }

  // 10y linear slope (sightings per year) on the last 10 *full* years.
  let slope10 = null;
  if (fullYears.length >= 10) {
    slope10 = mufonLinearSlope(fullYears.slice(-10), totals);
  }

  // Recent buckets — sum across states.
  const rb = m.recent_buckets || {};
  const recent = { '30d':0, '60d':0, '90d':0, '365d':0 };
  for (const s in rb) {
    for (const k in recent) recent[k] += Number((rb[s] || {})[k]) || 0;
  }

  // Headline label — based on score band + momentum sign.
  let chipText = 'STABLE', chipCls = 'v2-chip--warn';
  if (score >= 60)       { chipText = 'ACCELERATING';  chipCls = 'v2-chip--good'; }
  else if (score >= 25)  { chipText = 'TRENDING UP';   chipCls = 'v2-chip--good'; }
  else if (score >= 10)  { chipText = 'GRADUAL RISE';  chipCls = 'v2-chip--good'; }
  else if (score <= -60) { chipText = 'COLLAPSING';    chipCls = 'v2-chip--bad';  }
  else if (score <= -25) { chipText = 'DECLINING';     chipCls = 'v2-chip--bad';  }
  else if (score <= -10) { chipText = 'DECELERATING';  chipCls = 'v2-chip--bad';  }
  // else: stays STABLE / warn

  const scoreColor = score >= 25 ? 'var(--v2-good)' :
                     score <= -25 ? 'var(--v2-bad)'  : 'var(--v2-warn)';
  const scoreTxt = (score >= 0 ? '+' : '') + score;
  const clamped = Math.max(-100, Math.min(100, score));
  const markerPct = ((clamped + 100) / 200) * 100;

  const trendRange = state.mufonTrendRange || '5y';
  const subYearly = (trendRange === '30d' || trendRange === '90d' || trendRange === 'ytd');
  let spark = '';
  let captionText = '';
  if (subYearly) {
    const totalsByMonth = m.totals_by_month || {};
    const allMonths = Object.keys(totalsByMonth).sort();
    let visibleMonths = [];
    if (allMonths.length) {
      if (trendRange === '30d') {
        visibleMonths = allMonths.slice(-2);
      } else if (trendRange === '90d') {
        visibleMonths = allMonths.slice(-4);
      } else {
        const latestMonth = allMonths[allMonths.length - 1];
        const ytdYear = latestMonth.slice(0, 4);
        visibleMonths = allMonths.filter(function(s){ return s.slice(0, 4) === ytdYear; });
      }
    }
    if (visibleMonths.length >= 2) {
      spark = mufonTrendSparklineMonths(visibleMonths, totalsByMonth);
      captionText = 'Monthly reports · ' + visibleMonths[0] + '–' + visibleMonths[visibleMonths.length - 1];
    } else if (visibleMonths.length === 1) {
      const only = visibleMonths[0];
      const cnt = Number(totalsByMonth[only]) || 0;
      spark = '<div style="padding:18px 12px;background:#0b0d12;border-radius:6px;color:var(--muted);font-size:12px;text-align:center">'
        + only + ' · ' + cnt.toLocaleString() + ' sightings (only one month in window)'
        + '</div>';
      captionText = 'Monthly reports · ' + only;
    } else {
      spark = '<div style="padding:18px 12px;background:#0b0d12;border-radius:6px;color:var(--muted);font-size:12px;text-align:center">Insufficient monthly data for this window — try 1y or longer.</div>';
      captionText = 'Monthly reports · (no data)';
    }
  }
  const rangeWindow = { '5y':5, '10y':10, '20y':20, '30y':30 }[trendRange] || fullYears.length;
  const visibleYears = (rangeWindow >= fullYears.length)
    ? fullYears
    : fullYears.slice(-rangeWindow);
  const peakInWindow = (peakYear != null && visibleYears.indexOf(Number(peakYear)) >= 0)
    ? peakYear : null;
  if (!subYearly) {
    spark = mufonTrendSparkline(visibleYears, totals, peakInWindow);
  }

  // Component table.
  function rowHtml(name, valueTxt, delta){
    const col = delta == null ? 'var(--muted)'
      : (delta > 0 ? 'var(--v2-good)' : (delta < 0 ? 'var(--v2-bad)' : 'var(--muted)'));
    const arrow = delta == null ? '·'
      : (delta > 0 ? '▲' : (delta < 0 ? '▼' : '·'));
    return '<tr>'
      + '<td style="padding:5px 6px;font-weight:500">' + escapeHtml(name) + '</td>'
      + '<td style="padding:5px 6px;color:var(--muted);font-variant-numeric:tabular-nums">' + valueTxt + '</td>'
      + '<td style="padding:5px 6px;color:' + col + ';text-align:right;font-weight:600">' + arrow + '</td>'
      + '</tr>';
  }

  const momTxt = (mom == null)
    ? '—'
    : ((mom >= 0 ? '+' : '') + (mom * 100).toFixed(1) + '% '
       + '(' + Math.round(momPrev).toLocaleString() + ' → ' + Math.round(momCur).toLocaleString() + '/yr)');
  const slopeTxt = (slope10 == null)
    ? '—'
    : ((slope10 >= 0 ? '+' : '') + Math.round(slope10).toLocaleString() + ' sightings/year (last 10y)');
  const peakTxt = (peakYear == null)
    ? '—'
    : (peakYear + ' — ' + (Number(totals[String(peakYear)]) || 0).toLocaleString() + ' sightings');
  const lastFull = fullYears.length ? fullYears[fullYears.length - 1] : null;
  const lastFullTxt = (lastFull == null)
    ? '—'
    : (lastFull + ' — ' + (Number(totals[String(lastFull)]) || 0).toLocaleString() + ' sightings');
  if (!subYearly) {
    captionText = 'Annual reports · ' + visibleYears[0] + '–' + (lastFull || '—')
      + (partialYear && trendRange === 'all' ? ' (' + partialYear + ' partial, excluded)' : '');
  }
  const partialTxt = (partialYear == null)
    ? null
    : (partialYear + ' — ' + (Number(totals[String(partialYear)]) || 0).toLocaleString()
       + ' sightings <em style="color:var(--muted);font-style:normal">(partial year)</em>');
  const cagrTxt = (cagr == null)
    ? '—'
    : ((cagr >= 0 ? '+' : '') + (cagr * 100).toFixed(1) + '%/yr CAGR');

  const recentAnchor = (m.date_range && m.date_range[1]) || '—';
  const recentTxt = '30d ' + recent['30d'].toLocaleString()
    + ' · 60d ' + recent['60d'].toLocaleString()
    + ' · 90d ' + recent['90d'].toLocaleString()
    + ' · 365d ' + recent['365d'].toLocaleString();

  const rows = [
    rowHtml('5y CAGR', cagrTxt, cagr),
    rowHtml('5y momentum', momTxt, mom),
    rowHtml('10y trend slope', slopeTxt, slope10),
    rowHtml('Peak year', peakTxt, null),
    rowHtml('Most recent full year', lastFullTxt, null),
    partialTxt ? rowHtml('Most recent partial year', partialTxt, null) : '',
    rowHtml('Recent windows (anchored to ' + recentAnchor + ')', recentTxt, null),
  ].join('');

  body.innerHTML = ''
    + '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px">'
    +   '<div style="min-width:0">'
    +     '<div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px">Signal direction</div>'
    +     '<span class="v2-chip ' + chipCls + '" style="font-size:14px;padding:6px 12px;font-weight:700">' + chipText + '</span>'
    +     '<div style="font-size:11px;color:var(--muted);margin-top:6px">5y CAGR-driven · last ' + fullYears.length + ' full years of data</div>'
    +   '</div>'
    +   '<div style="text-align:right">'
    +     '<div style="font-size:11px;color:var(--muted)">Score</div>'
    +     '<div style="font-size:34px;font-weight:700;color:' + scoreColor + ';line-height:1">' + scoreTxt + '</div>'
    +     '<div style="font-size:11px;color:var(--muted);margin-top:2px">of &plusmn;100</div>'
    +   '</div>'
    + '</div>'
    + '<div style="height:10px;background:linear-gradient(to right,#b91c1c 0%,var(--v2-bad) 25%,var(--v2-warn) 50%,var(--v2-good) 75%,#16a34a 100%);border-radius:5px;position:relative;margin-bottom:14px">'
    +   '<div style="position:absolute;top:-3px;left:calc(' + markerPct.toFixed(1) + '% - 3px);width:6px;height:16px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>'
    + '</div>'
    + '<div style="margin-bottom:14px">'
    +   '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:6px">'
    +     '<div style="font-size:11px;color:var(--muted)">' + captionText + '</div>'
    +     '<div class="mufon-trend-range" style="display:flex;gap:4px;flex-wrap:wrap">'
    +       ['30d','90d','ytd','1y','3y','5y','10y','20y','30y','all'].map(function(r){ return '<button class="btn btn--small' + (r === trendRange ? ' active' : '') + '" data-mufontrendrange="' + r + '" type="button">' + (r === 'all' ? 'All-time' : (r === 'ytd' ? 'YTD' : r)) + '</button>'; }).join('')
    +     '</div>'
    +   '</div>'
    +   spark
    + '</div>'
    + '<div style="margin-bottom:10px">'
    +   '<div style="font-size:11px;color:var(--muted);margin-bottom:4px">Component breakdown</div>'
    +   '<table style="font-size:12px;width:100%;border-collapse:collapse">'
    +     '<tbody>' + rows + '</tbody>'
    +   '</table>'
    + '</div>'
    + '<div style="margin-top:12px;padding:10px 12px;background:var(--bg2,#0f1419);border-left:3px solid var(--v2-warn,#fbbf24);border-radius:4px;font-size:11px;color:var(--muted);line-height:1.5">'
    +   '<strong style="color:var(--v2-warn,#fbbf24)">Note:</strong> '
    +   'Combines the planetsig community mirror (1906-2014) with a direct scrape of NUFORC\'s monthly subndx pages (2014+). '
    +   'Recent-window counts run through <strong>' + recentAnchor + '</strong> and reflect actual recent activity — they ARE the last 30/60/90/365 days from now. '
    +   'The "most recent partial year" row flags ' + (partialYear || 'the current year') + ' so the 5y CAGR doesn\'t compare a half-year to full ones.'
    + '</div>';
}

// UAP trend sparkline range-toggle wiring. Delegated on the trend card so
// it survives every re-render. Re-renders the trend card only.
(function(){
  const card = document.getElementById('mufonTrendCard');
  if (!card) return;
  card.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mufontrendrange]');
    if (!b) return;
    const v = b.getAttribute('data-mufontrendrange');
    if (v && v !== state.mufonTrendRange) {
      state.mufonTrendRange = v;
      renderMufonTrend();
    }
  });
})();

// Compute per-state count for the active time range.
function mufonCountsForRange(m, range){
  const out = {};
  if (!m) return out;
  if (range === 'all') {
    const bsy = m.by_state_year || {};
    for (const state in bsy) {
      let total = 0;
      for (const y in bsy[state]) total += bsy[state][y];
      out[state] = total;
    }
    return out;
  }
  const rb = m.recent_buckets || {};
  for (const state in rb) {
    out[state] = (rb[state] || {})[range] || 0;
  }
  return out;
}

function renderMufonMap(){
  const m = DATA.mufon;
  if (!m) return; // sidecar pending; selectTab() shows the loading placeholder

  const sub  = document.getElementById('mufonMapSub');
  const asOf = document.getElementById('mufonMapAsOf');
  const wrap = document.getElementById('mufonMapSvgWrap');
  const side = document.getElementById('mufonMapSidePanel');
  const note = document.getElementById('mufonRangeNote');
  if (!wrap) return;

  // Empty-state: the fetcher wrote an _error placeholder.
  if (!m.total_records) {
    if (asOf) asOf.textContent = '● unavailable';
    wrap.innerHTML = V2.empty({icon:'🛸', title:'No sightings data',
      sub: m._error || 'Upstream CSV unavailable; retry on next dashboard refresh.',
      warm:false});
    side.innerHTML = '';
    if (note) note.textContent = '';
    return;
  }

  if (asOf) {
    const d = (m.date_range && m.date_range[1]) || '—';
    asOf.textContent = '● to ' + d + (m._stale ? ' (stale)' : '');
  }

  const range = state.mufonTimeRange || 'all';
  const counts = mufonCountsForRange(m, range);

  if (note) {
    if (range === 'all') {
      const dr = m.date_range || [null,null];
      note.textContent = (dr[0] && dr[1]) ? ('All records ' + dr[0] + ' to ' + dr[1] + '.') : '';
    } else if (m._stale) {
      const anchor = (m.date_range && m.date_range[1]) || '—';
      note.textContent = 'Live scrape unavailable — window anchored to historical mirror cutoff (' + anchor + '), not today.';
    } else {
      note.textContent = 'Window anchored to today (UTC).';
    }
  }

  // Pre-compute color ramp.
  const values = Object.values(counts).filter(v => v > 0).sort((a,b) => a-b);
  const maxC = values.length ? values[values.length - 1] : 0;
  function bucketColor(c){
    if (!c) return 'var(--bg2, #0f1419)';
    if (!values.length) return 'var(--v2-good-bg, #1f3b2a)';
    const idx = values.indexOf(c);
    const q = idx / Math.max(1, values.length - 1); // 0..1
    if (q < 0.20) return 'var(--v2-good, #4ade80)';
    if (q < 0.45) return 'var(--v2-good, #4ade80)';
    if (q < 0.70) return 'var(--v2-warn, #fbbf24)';
    if (q < 0.90) return 'var(--v2-orange, #fb923c)';
    return 'var(--v2-bad, #f87171)';
  }

  // SVG dims: 12 cols x 8 rows tiles.
  const CELL = 50, GAP = 4;
  const COLS = 12, ROWS = 8;
  const W = COLS * (CELL + GAP) - GAP;
  const H = ROWS * (CELL + GAP) - GAP;

  // Build tiles
  const tiles = MUFON_STATE_GRID.map(([code, cx, cy]) => {
    const x = cx * (CELL + GAP);
    const y = cy * (CELL + GAP);
    const c = counts[code] || 0;
    const fill = bucketColor(c);
    const isSel = state.mufonSelectedState === code;
    const stroke = isSel ? 'var(--v2-info, #38bdf8)' : 'rgba(255,255,255,0.08)';
    const strokeW = isSel ? 2.5 : 1;
    return ''
      + '<g class="mufonTile" data-state="'+code+'" style="cursor:pointer">'
      +   '<rect x="'+x+'" y="'+y+'" width="'+CELL+'" height="'+CELL+'" rx="6" '
      +     'fill="'+fill+'" stroke="'+stroke+'" stroke-width="'+strokeW+'">'
      +     '<title>'+ (MUFON_STATE_NAMES[code]||code) +': '+c.toLocaleString()+' sightings</title>'
      +   '</rect>'
      +   '<text x="'+(x + CELL/2)+'" y="'+(y + CELL/2 - 4)+'" '
      +     'text-anchor="middle" font-size="13" font-weight="700" '
      +     'fill="rgba(0,0,0,0.7)" pointer-events="none">'+code+'</text>'
      +   '<text x="'+(x + CELL/2)+'" y="'+(y + CELL/2 + 12)+'" '
      +     'text-anchor="middle" font-size="10" '
      +     'fill="rgba(0,0,0,0.55)" pointer-events="none">'+c.toLocaleString()+'</text>'
      + '</g>';
  }).join('');

  // Legend
  const legendBuckets = [
    ['0', 'var(--bg2, #0f1419)'],
    ['Low', 'var(--v2-good, #4ade80)'],
    ['Med', 'var(--v2-warn, #fbbf24)'],
    ['High', 'var(--v2-orange, #fb923c)'],
    ['Max', 'var(--v2-bad, #f87171)'],
  ];
  const legend = '<div style="display:flex;gap:6px;align-items:center;font-size:11px;color:var(--muted);margin-top:10px">'
    + '<span>fewer</span>'
    + legendBuckets.map(b => '<span style="display:inline-block;width:18px;height:14px;border-radius:3px;background:'+b[1]+'" title="'+b[0]+'"></span>').join('')
    + '<span>more</span>'
    + (maxC ? '<span style="margin-left:12px">max: '+maxC.toLocaleString()+'</span>' : '')
    + '</div>';

  wrap.innerHTML = ''
    + '<svg viewBox="0 0 '+W+' '+H+'" width="100%" preserveAspectRatio="xMidYMid meet" '
    +    'style="font-family:inherit;display:block;max-height:560px" '
    +    'role="img" aria-label="US state sightings heatmap">'
    +   tiles
    + '</svg>'
    + legend;

  // Bind clicks (delegated through SVG since tiles re-render on each call).
  wrap.querySelectorAll('.mufonTile').forEach(g => {
    g.addEventListener('click', () => {
      const s = g.getAttribute('data-state');
      state.mufonSelectedState = (state.mufonSelectedState === s) ? null : s;
      renderMufonMap(); // re-render to update stroke + side panel
    });
  });

  // Side panel: top-10 cities for the selected state (or top-10 states overall).
  if (state.mufonSelectedState) {
    const s = state.mufonSelectedState;
    const cities = ((m.top_cities_by_state || {})[s]) || [];
    const c = counts[s] || 0;
    side.innerHTML = ''
      + '<div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">'
      +   '<div style="font-weight:700;font-size:14px">'+(MUFON_STATE_NAMES[s]||s)+'</div>'
      +   '<button class="btn btn--small" id="mufonSideClose" style="padding:2px 8px;font-size:11px">×</button>'
      + '</div>'
      + '<div style="font-size:11px;color:var(--muted);margin-bottom:10px">'+c.toLocaleString()+' sightings · '+(range==='all'?'all-time':range)+'</div>'
      + (cities.length
          ? '<div style="font-size:11px;color:var(--muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px">Top cities (all-time)</div>'
            + '<ol style="margin:0;padding-left:18px;line-height:1.7">'
            +   cities.map(c => '<li>'+c.city+' <span style="color:var(--muted)">— '+c.count.toLocaleString()+'</span></li>').join('')
            + '</ol>'
          : '<div style="font-size:11px;color:var(--muted);font-style:italic">No per-city detail available.</div>');
    const closeBtn = document.getElementById('mufonSideClose');
    if (closeBtn) closeBtn.addEventListener('click', () => {
      state.mufonSelectedState = null; renderMufonMap();
    });
  } else {
    // No selection — show top-10 states by current range.
    const ranked = Object.entries(counts).filter(([,c]) => c > 0).sort((a,b) => b[1]-a[1]).slice(0, 10);
    side.innerHTML = ''
      + '<div style="font-weight:700;font-size:14px;margin-bottom:8px">Top 10 states</div>'
      + '<div style="font-size:11px;color:var(--muted);margin-bottom:10px">'+(range==='all'?'All-time totals':range+' window')+' · click a tile for cities</div>'
      + (ranked.length
          ? '<ol style="margin:0;padding-left:18px;line-height:1.7">'
            + ranked.map(([s,c]) => '<li><strong>'+(MUFON_STATE_NAMES[s]||s)+'</strong> <span style="color:var(--muted)">— '+c.toLocaleString()+'</span></li>').join('')
            + '</ol>'
          : '<div style="font-size:11px;color:var(--muted);font-style:italic">No sightings in this window.</div>');
  }

  // Range filter buttons — bind once per render so they always reflect state.
  const filterRow = document.getElementById('mufonMapFilters');
  if (filterRow) {
    filterRow.querySelectorAll('button[data-mufonrange]').forEach(b => {
      const v = b.getAttribute('data-mufonrange');
      b.classList.toggle('active', v === (state.mufonTimeRange || 'all'));
      if (!b._mufonBound) {
        b._mufonBound = true;
        b.addEventListener('click', () => {
          if (v && v !== state.mufonTimeRange) {
            state.mufonTimeRange = v;
            renderMufonMap();
          }
        });
      }
    });
  }
}

// Sightings-by-classification card colors.
const MUFON_SHAPE_COLORS = [
  'var(--v2-info, #06b6d4)',     // 0: top shape (light)
  'var(--v2-warn, #f59e0b)',     // 1
  'var(--v2-good, #22c55e)',     // 2
  'var(--v2-bad, #ef4444)',      // 3
  'var(--v2-ai, #a78bfa)',       // 4
  'var(--v2-orange, #cc7a2b)',   // 5
  '#0ea5e9',                     // 6 (sky — derived from info)
  '#84cc16',                     // 7 (lime — derived from good)
];
const MUFON_SHAPE_COLOR_FALLBACK = 'rgba(148,163,184,0.55)'; // slate

function mufonShapeColor(rankIdx){
  if (rankIdx < MUFON_SHAPE_COLORS.length) return MUFON_SHAPE_COLORS[rankIdx];
  return MUFON_SHAPE_COLOR_FALLBACK;
}

// Month-keyed twin of the yearly stacked-area renderer.
function renderMufonShapesMonths(m, monthKeys, totals, byMonth, activeRange, rangeToggleHtml){
  const body = document.getElementById('mufonShapesBody');
  const asOf = document.getElementById('mufonShapesAsOf');
  if (!body) return;

  if (asOf) {
    const dr = m.date_range || [null, null];
    const asOfTxt = (dr[0] && dr[1]) ? (dr[0] + ' → ' + dr[1]) : '—';
    asOf.textContent = '● ' + asOfTxt + (m._stale ? ' (stale)' : '');
  }

  const subEl = document.getElementById('mufonShapesSub');
  if (subEl) {
    subEl.textContent = 'NUFORC-reported shapes · ' + monthKeys[0] + '–' + monthKeys[monthKeys.length - 1];
  }

  const ranked = totals.slice().sort(function(a,b){ return b.count - a.count; });
  const shapeOrder = ranked.map(function(s){ return s.shape; });
  const colorFor = {};
  shapeOrder.forEach(function(sh, i){ colorFor[sh] = mufonShapeColor(i); });

  const W = 720, H = 280;
  const padL = 44, padR = 12, padT = 14, padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const N = monthKeys.length;
  const monthSpan = Math.max(1, N - 1);

  let maxTotal = 0;
  monthKeys.forEach(function(mk){
    const b = byMonth[mk] || {};
    let t = 0;
    for (const sh in b) t += b[sh];
    if (t > maxTotal) maxTotal = t;
  });
  if (!maxTotal) maxTotal = 1;

  function xFor(i){ return padL + (i / monthSpan) * plotW; }
  function yFor(v){ return padT + plotH - (v / maxTotal) * plotH; }

  const runningBottom = {}; // monthKey -> cumulative bottom (units)
  monthKeys.forEach(function(mk){ runningBottom[mk] = 0; });

  const polygons = [];
  shapeOrder.forEach(function(sh, idx){
    let anyVal = false;
    const lowerPts = [];
    const upperPts = [];
    monthKeys.forEach(function(mk, i){
      const v = (byMonth[mk] || {})[sh] || 0;
      const lower = runningBottom[mk];
      const upper = lower + v;
      if (v > 0) anyVal = true;
      const xp = xFor(i);
      lowerPts.push(xp + ',' + yFor(lower));
      upperPts.push(xp + ',' + yFor(upper));
      runningBottom[mk] = upper;
    });
    if (!anyVal) return;
    const color = colorFor[sh];
    const pts = lowerPts.concat(upperPts.slice().reverse()).join(' ');
    polygons.push(
      '<polygon points="' + pts + '" fill="' + color + '" '
      + 'fill-opacity="0.82" stroke="none">'
      +   '<title>' + sh + ' — ' + (ranked[idx] ? ranked[idx].count.toLocaleString() : '?') + ' all-time</title>'
      + '</polygon>'
    );
  });

  const tickEvery = N > 8 ? 2 : 1;
  const xAxisMarks = monthKeys.map(function(mk, i){
    if (i % tickEvery !== 0 && i !== N - 1) return '';
    const xp = xFor(i);
    return ''
      + '<line x1="' + xp + '" y1="' + (padT + plotH) + '" '
      +     'x2="' + xp + '" y2="' + (padT + plotH + 4) + '" '
      +     'stroke="rgba(255,255,255,0.25)" stroke-width="1" />'
      + '<text x="' + xp + '" y="' + (padT + plotH + 16) + '" '
      +     'text-anchor="middle" font-size="9" '
      +     'fill="var(--muted)">' + mk + '</text>';
  }).join('');

  const yTickVals = [0, Math.round(maxTotal * 0.25), Math.round(maxTotal * 0.5),
                     Math.round(maxTotal * 0.75), maxTotal];
  const yAxisMarks = yTickVals.map(function(v){
    const yp = yFor(v);
    return ''
      + '<line x1="' + padL + '" y1="' + yp + '" '
      +     'x2="' + (padL + plotW) + '" y2="' + yp + '" '
      +     'stroke="rgba(255,255,255,0.06)" stroke-width="1" />'
      + '<text x="' + (padL - 6) + '" y="' + (yp + 3) + '" '
      +     'text-anchor="end" font-size="10" '
      +     'fill="var(--muted)" font-variant-numeric="tabular-nums">' + v.toLocaleString() + '</text>';
  }).join('');

  const frame = ''
    + '<line x1="' + padL + '" y1="' + padT + '" '
    +     'x2="' + padL + '" y2="' + (padT + plotH) + '" '
    +     'stroke="rgba(255,255,255,0.25)" stroke-width="1" />'
    + '<line x1="' + padL + '" y1="' + (padT + plotH) + '" '
    +     'x2="' + (padL + plotW) + '" y2="' + (padT + plotH) + '" '
    +     'stroke="rgba(255,255,255,0.25)" stroke-width="1" />';

  const chartSvg = ''
    + '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" '
    +    'preserveAspectRatio="xMidYMid meet" '
    +    'style="font-family:inherit;display:block;max-height:340px" '
    +    'role="img" aria-label="Stacked area chart of UAP sightings by shape, ' + monthKeys[0] + ' to ' + monthKeys[monthKeys.length - 1] + ' (range ' + activeRange + ')">'
    +   yAxisMarks
    +   polygons.join('')
    +   xAxisMarks
    +   frame
    + '</svg>';

  const grandTotal = ranked.reduce(function(s, r){ return s + r.count; }, 0) || 1;
  const legendRows = ranked.map(function(r, i){
    const pct = (r.count / grandTotal) * 100;
    const color = mufonShapeColor(i);
    return ''
      + '<li style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">'
      +   '<span style="display:inline-block;width:12px;height:12px;border-radius:3px;flex:0 0 auto;background:' + color + '"></span>'
      +   '<span style="flex:1;min-width:0;text-transform:capitalize">' + r.shape + '</span>'
      +   '<span style="color:var(--muted);font-variant-numeric:tabular-nums">' + r.count.toLocaleString() + '</span>'
      +   '<span style="color:var(--muted);font-variant-numeric:tabular-nums;width:42px;text-align:right">' + pct.toFixed(1) + '%</span>'
      + '</li>';
  }).join('');

  const legendBlock = ''
    + '<div style="background:var(--bg2,#0f1419);border:1px solid var(--bd,#27313d);'
    +    'border-radius:8px;padding:10px 12px">'
    +   '<div style="font-size:11px;color:var(--muted);text-transform:uppercase;'
    +     'letter-spacing:.5px;margin-bottom:6px">All-time totals</div>'
    +   '<ul style="list-style:none;margin:0;padding:0">' + legendRows + '</ul>'
    + '</div>';

  const dr = m.date_range || [null, null];
  const footnote = ''
    + '<div style="margin-top:12px;font-size:11px;color:var(--muted);line-height:1.5">'
    +   'Aggregated by NUFORC-reported shape (lowercased; blanks bucketed as "unknown"). '
    +   'Top 15 shapes shown; rarer ones collapse into "other". '
    +   'Monthly view spans ' + monthKeys[0] + ' through ' + monthKeys[monthKeys.length - 1] + ', '
    +   'sourced from shape_by_month (last ~36 months). Full history available on yearly ranges (1y+). '
    +   'Range toggle re-slices the stacked area only; the legend totals stay all-time.'
    + '</div>';

  body.innerHTML = ''
    + rangeToggleHtml
    + '<div style="display:grid;grid-template-columns:minmax(0,3fr) minmax(220px, 1fr);gap:14px;align-items:start">'
    +   '<div style="min-width:0">' + chartSvg + '</div>'
    +   legendBlock
    + '</div>'
    + footnote;
}

function renderMufonShapes(){
  const m = DATA.mufon;
  const body = document.getElementById('mufonShapesBody');
  const asOf = document.getElementById('mufonShapesAsOf');
  if (!body) return;
  if (!m) return; // sidecar pending; the parent loading state already shows.

  const totals = (m.shape_totals || []).slice();
  const byYear = m.shape_by_year || {};
  const byMonth = m.shape_by_month || {};
  const allYearKeys = Object.keys(byYear).sort();
  const allMonthKeys = Object.keys(byMonth).sort();

  if (!totals.length || !allYearKeys.length) {
    if (asOf) asOf.textContent = '● unavailable';
    body.innerHTML = V2.empty({
      icon: '🛸',
      title: 'No classification data',
      sub: 'Upstream CSV did not include a usable shape column.',
      warm: false,
    });
    return;
  }

  const activeRange = state.mufonShapesRange || '5y';
  const subYearlyShapes = (activeRange === '30d' || activeRange === '90d' || activeRange === 'ytd');

  const rangeToggleHtml = ''
    + '<div style="display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
    +   '<div class="mufon-shapes-range" style="display:flex;gap:4px;flex-wrap:wrap">'
    +     ['30d','90d','ytd','1y','3y','5y','10y','20y','30y','all'].map(function(r){ return '<button class="btn btn--small' + (r === activeRange ? ' active' : '') + '" data-mufonshapesrange="' + r + '" type="button">' + (r === 'all' ? 'All-time' : (r === 'ytd' ? 'YTD' : r)) + '</button>'; }).join('')
    +   '</div>'
    + '</div>';

  if (subYearlyShapes) {
    let monthKeys = [];
    if (allMonthKeys.length) {
      if (activeRange === '30d') {
        monthKeys = allMonthKeys.slice(-2);
      } else if (activeRange === '90d') {
        monthKeys = allMonthKeys.slice(-4);
      } else {
        const latest = allMonthKeys[allMonthKeys.length - 1];
        const ytdYear = latest.slice(0, 4);
        monthKeys = allMonthKeys.filter(function(s){ return s.slice(0, 4) === ytdYear; });
      }
    }
    if (monthKeys.length < 2) {
      if (asOf) {
        const dr = m.date_range || [null, null];
        const asOfTxt = (dr[0] && dr[1]) ? (dr[0] + ' → ' + dr[1]) : '—';
        asOf.textContent = '● ' + asOfTxt + (m._stale ? ' (stale)' : '');
      }
      const subEl = document.getElementById('mufonShapesSub');
      if (subEl) {
        subEl.textContent = 'NUFORC-reported shapes · monthly view';
      }
      body.innerHTML = ''
        + rangeToggleHtml
        + '<div style="padding:24px 16px;background:var(--bg2,#0f1419);border:1px solid var(--bd,#27313d);'
        +    'border-radius:8px;color:var(--muted);font-size:12px;text-align:center;line-height:1.5">'
        +   'Insufficient monthly data for this window — try 1y or longer.'
        + '</div>';
      return;
    }
    return renderMufonShapesMonths(m, monthKeys, totals, byMonth, activeRange, rangeToggleHtml);
  }

  const allYearNums = allYearKeys.map(y => parseInt(y, 10)).sort((a,b) => a-b);
  const dataMaxYear = allYearNums[allYearNums.length - 1];
  let windowYears;
  if (activeRange === 'all') {
    windowYears = allYearNums.length;
  } else {
    windowYears = parseInt(activeRange.replace('y',''), 10) || allYearNums.length;
  }
  const cutoff = dataMaxYear - windowYears + 1;
  const yearKeys = allYearNums.filter(y => y >= cutoff).map(y => String(y));

  const subEl = document.getElementById('mufonShapesSub');
  if (subEl) {
    if (activeRange === 'all') {
      subEl.textContent = 'NUFORC-reported shapes · ' + allYearNums[0] + '–' + dataMaxYear;
    } else if (yearKeys.length) {
      subEl.textContent = 'NUFORC-reported shapes · ' + yearKeys[0] + '–' + yearKeys[yearKeys.length - 1];
    }
  }

  if (asOf) {
    const dr = m.date_range || [null, null];
    const asOfTxt = (dr[0] && dr[1]) ? (dr[0] + ' → ' + dr[1]) : '—';
    asOf.textContent = '● ' + asOfTxt + (m._stale ? ' (stale)' : '');
  }

  const ranked = totals.slice().sort((a,b) => b.count - a.count);
  const shapeOrder = ranked.map(s => s.shape);
  const colorFor = {};
  shapeOrder.forEach((sh, i) => { colorFor[sh] = mufonShapeColor(i); });

  const W = 720, H = 280;
  const padL = 44, padR = 12, padT = 14, padB = 28;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;

  const years = yearKeys.map(y => parseInt(y, 10)).sort((a,b) => a-b);
  const yMin = years[0];
  const yMax = years[years.length - 1];
  const yearSpan = Math.max(1, yMax - yMin);
  const singlePoint = years.length < 2;

  let maxTotal = 0;
  const totalsByYear = {};
  yearKeys.forEach(y => {
    let t = 0;
    const b = byYear[y] || {};
    for (const sh in b) t += b[sh];
    totalsByYear[y] = t;
    if (t > maxTotal) maxTotal = t;
  });
  if (!maxTotal) maxTotal = 1;

  function xFor(year){ return padL + ((year - yMin) / yearSpan) * plotW; }
  function yFor(v){ return padT + plotH - (v / maxTotal) * plotH; }

  const runningBottom = {}; // yearKey -> current cumulative bottom (in units)
  yearKeys.forEach(y => { runningBottom[y] = 0; });

  const polygons = [];
  shapeOrder.forEach((sh, i) => {
    let anyVal = false;
    const lowerPts = [];
    const upperPts = [];
    const dotsForShape = [];
    yearKeys.forEach(y => {
      const yearN = parseInt(y, 10);
      const v = (byYear[y] || {})[sh] || 0;
      const lower = runningBottom[y];
      const upper = lower + v;
      if (v > 0) anyVal = true;
      const xp = singlePoint ? (padL + plotW / 2) : xFor(yearN);
      lowerPts.push(xp + ',' + yFor(lower));
      upperPts.push(xp + ',' + yFor(upper));
      if (v > 0) {
        const midY = yFor((lower + upper) / 2);
        const segW = Math.max(6, plotW * 0.06);
        dotsForShape.push({ x: xp, y: midY, h: Math.max(2, yFor(lower) - yFor(upper)), w: segW });
      }
      runningBottom[y] = upper;
    });
    if (!anyVal) return;
    const color = colorFor[sh];
    if (singlePoint) {
      dotsForShape.forEach(d => {
        polygons.push(
          '<rect x="' + (d.x - d.w / 2) + '" y="' + (d.y - d.h / 2) + '" '
          + 'width="' + d.w + '" height="' + d.h + '" '
          + 'fill="' + color + '" fill-opacity="0.82" stroke="none">'
          +   '<title>' + sh + ' — ' + (ranked[i] ? ranked[i].count.toLocaleString() : '?') + ' all-time</title>'
          + '</rect>'
        );
      });
    } else {
      const pts = lowerPts.concat(upperPts.slice().reverse()).join(' ');
      polygons.push(
        '<polygon points="' + pts + '" fill="' + color + '" '
        + 'fill-opacity="0.82" stroke="none">'
        +   '<title>' + sh + ' — ' + (ranked[i] ? ranked[i].count.toLocaleString() : '?') + ' all-time</title>'
        + '</polygon>'
      );
    }
  });

  const xTicks = [];
  let tickStep;
  if (yearSpan <= 1) tickStep = 1;
  else if (yearSpan <= 5) tickStep = 1;
  else if (yearSpan <= 12) tickStep = 2;
  else if (yearSpan <= 25) tickStep = 5;
  else if (yearSpan <= 60) tickStep = 10;
  else tickStep = 15;
  let t0 = Math.ceil(yMin / tickStep) * tickStep;
  for (let yy = t0; yy <= yMax; yy += tickStep) xTicks.push(yy);
  if (!xTicks.length || xTicks[0] !== yMin) xTicks.unshift(yMin);
  if (xTicks[xTicks.length - 1] !== yMax) xTicks.push(yMax);

  const xAxisMarks = xTicks.map(yy => {
    const xp = xFor(yy);
    return ''
      + '<line x1="' + xp + '" y1="' + (padT + plotH) + '" '
      +     'x2="' + xp + '" y2="' + (padT + plotH + 4) + '" '
      +     'stroke="rgba(255,255,255,0.25)" stroke-width="1" />'
      + '<text x="' + xp + '" y="' + (padT + plotH + 16) + '" '
      +     'text-anchor="middle" font-size="10" '
      +     'fill="var(--muted)">' + yy + '</text>';
  }).join('');

  const yTickVals = [0, Math.round(maxTotal * 0.25), Math.round(maxTotal * 0.5),
                     Math.round(maxTotal * 0.75), maxTotal];
  const yAxisMarks = yTickVals.map(v => {
    const yp = yFor(v);
    return ''
      + '<line x1="' + padL + '" y1="' + yp + '" '
      +     'x2="' + (padL + plotW) + '" y2="' + yp + '" '
      +     'stroke="rgba(255,255,255,0.06)" stroke-width="1" />'
      + '<text x="' + (padL - 6) + '" y="' + (yp + 3) + '" '
      +     'text-anchor="end" font-size="10" '
      +     'fill="var(--muted)" font-variant-numeric="tabular-nums">' + v.toLocaleString() + '</text>';
  }).join('');

  const frame = ''
    + '<line x1="' + padL + '" y1="' + padT + '" '
    +     'x2="' + padL + '" y2="' + (padT + plotH) + '" '
    +     'stroke="rgba(255,255,255,0.25)" stroke-width="1" />'
    + '<line x1="' + padL + '" y1="' + (padT + plotH) + '" '
    +     'x2="' + (padL + plotW) + '" y2="' + (padT + plotH) + '" '
    +     'stroke="rgba(255,255,255,0.25)" stroke-width="1" />';

  const chartSvg = ''
    + '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" '
    +    'preserveAspectRatio="xMidYMid meet" '
    +    'style="font-family:inherit;display:block;max-height:340px" '
    +    'role="img" aria-label="Stacked area chart of UAP sightings by shape, ' + yMin + ' to ' + yMax + ' (range ' + activeRange + ')">'
    +   yAxisMarks
    +   polygons.join('')
    +   xAxisMarks
    +   frame
    + '</svg>';

  const grandTotal = ranked.reduce((s, r) => s + r.count, 0) || 1;
  const legendRows = ranked.map((r, i) => {
    const pct = (r.count / grandTotal) * 100;
    const color = mufonShapeColor(i);
    return ''
      + '<li style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px">'
      +   '<span style="display:inline-block;width:12px;height:12px;border-radius:3px;flex:0 0 auto;background:' + color + '"></span>'
      +   '<span style="flex:1;min-width:0;text-transform:capitalize">' + r.shape + '</span>'
      +   '<span style="color:var(--muted);font-variant-numeric:tabular-nums">' + r.count.toLocaleString() + '</span>'
      +   '<span style="color:var(--muted);font-variant-numeric:tabular-nums;width:42px;text-align:right">' + pct.toFixed(1) + '%</span>'
      + '</li>';
  }).join('');

  const legendBlock = ''
    + '<div style="background:var(--bg2,#0f1419);border:1px solid var(--bd,#27313d);'
    +    'border-radius:8px;padding:10px 12px">'
    +   '<div style="font-size:11px;color:var(--muted);text-transform:uppercase;'
    +     'letter-spacing:.5px;margin-bottom:6px">All-time totals</div>'
    +   '<ul style="list-style:none;margin:0;padding:0">' + legendRows + '</ul>'
    + '</div>';

  const dr = m.date_range || [null, null];
  const footnote = ''
    + '<div style="margin-top:12px;font-size:11px;color:var(--muted);line-height:1.5">'
    +   'Aggregated by NUFORC-reported shape (lowercased; blanks bucketed as "unknown"). '
    +   'Top 15 shapes shown; rarer ones collapse into "other". '
    +   'Series runs ' + (dr[0] || '?') + ' through ' + (dr[1] || '?') + ', '
    +   'combining the planetsig historical mirror with a direct NUFORC subndx scrape'
    +   (m._stale ? ' <strong>(live scrape unavailable this run — historical only)</strong>' : '')
    +   '. Range toggle re-slices the stacked area only; the legend totals stay all-time.'
    + '</div>';

  body.innerHTML = ''
    + rangeToggleHtml
    + '<div style="display:grid;grid-template-columns:minmax(0,3fr) minmax(220px, 1fr);gap:14px;align-items:start">'
    +   '<div style="min-width:0">' + chartSvg + '</div>'
    +   legendBlock
    + '</div>'
    + footnote;
}

// UAP shapes range-toggle wiring. Delegated on the card so it survives every
// re-render. Re-renders the shapes card only.
(function(){
  const card = document.getElementById('mufonShapesCard');
  if (!card) return;
  card.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-mufonshapesrange]');
    if (!b) return;
    const v = b.getAttribute('data-mufonshapesrange');
    if (v && v !== state.mufonShapesRange) {
      state.mufonShapesRange = v;
      renderMufonShapes();
    }
  });
})();

// ============================================================================
// CPI / SUPPLIES / METALS TABS (ported from V2 — V1 visual style)
// ============================================================================
// Each tab is sidecar-loaded via the existing SIDECAR_FOR_TAB mechanism
// (see SIDECAR_FOR_TAB above). Renderers are NO-OPs until the sidecar lands
// (DATA.cpi / DATA.supplies / DATA.metals are undefined on first paint).
// All charts are inline SVG — no Chart.js dependency for these tabs.

// ---------- CPI: shared category list (must mirror fetch_cpi.py) ----------
const CPI_CATEGORY_ORDER = ['headlines','food','energy','housing','cars','healthcare','other'];
const CPI_CATEGORY_LABELS = {
  headlines:'Headlines', food:'Food', energy:'Energy', housing:'Housing',
  cars:'Cars', healthcare:'Healthcare', other:'Other',
};

function renderCpiTab(){
  const cpi = DATA.cpi || null;
  const emptyEl = document.getElementById('cpiEmpty');
  const bodyEl  = document.getElementById('cpiBody');
  const catsEl  = document.getElementById('cpiCategories');
  const asOf    = document.getElementById('cpiAsOf');
  const chip    = document.getElementById('cpiChip');
  const sub     = document.getElementById('cpiEmptySub');
  const loading = document.getElementById('cpiLoading');
  if (!emptyEl || !bodyEl) return;
  // Loading state — sidecar fetch in flight + no payload yet.
  if (!cpi && state.tab === 'cpi' && SIDECAR_STATE.cpi === 'loading'){
    if (loading) loading.classList.remove('hidden');
    emptyEl.classList.add('hidden');
    bodyEl.classList.add('hidden');
    return;
  }
  if (loading) loading.classList.add('hidden');

  const noPayload = !cpi;
  const noFredKey = !!cpi && cpi.fred_available === false;
  const noSeries  = !!cpi && (!Array.isArray(cpi.series) || cpi.series.length === 0);
  if (noPayload || noFredKey || noSeries){
    emptyEl.classList.remove('hidden');
    bodyEl.classList.add('hidden');
    if (sub){
      const note = (cpi && typeof cpi.note === 'string' && cpi.note.trim()) ? cpi.note.trim() : '';
      if (note) sub.textContent = note;
      else if (noPayload) sub.textContent = 'CPI sidecar not yet loaded.';
      else if (noFredKey) sub.textContent = 'FRED_API_KEY is not set on the server.';
      else sub.textContent = 'No CPI series available in the latest payload.';
    }
    return;
  }
  emptyEl.classList.add('hidden');
  bodyEl.classList.remove('hidden');

  const series = cpi.series || [];
  const okCount = series.filter(s => (s.observations||[]).length).length;
  let when = '';
  if (cpi.generated_at){ try { when = ' · updated ' + new Date(cpi.generated_at).toLocaleDateString(); } catch(_){} }
  if (asOf) asOf.textContent = 'Source: FRED · ' + okCount + ' of ' + series.length + ' series loaded' + when;
  if (chip) chip.textContent = 'FRED · ' + (state.cpiViewMode || 'index100');

  // Sync toggle button states to current state.
  document.querySelectorAll('#tab-cpi .btn[data-cpirange]').forEach(b => {
    b.classList.toggle('active', b.dataset.cpirange === (state.cpiTimeRange || 'all'));
  });
  document.querySelectorAll('#tab-cpi .btn[data-cpiview]').forEach(b => {
    b.classList.toggle('active', b.dataset.cpiview === (state.cpiViewMode || 'index100'));
  });

  if (!catsEl) return;
  const known = new Set(CPI_CATEGORY_ORDER);
  const groups = {};
  CPI_CATEGORY_ORDER.forEach(k => { groups[k] = []; });
  series.forEach(s => {
    const cat = (s && known.has(s.category)) ? s.category : 'other';
    groups[cat].push(s);
  });
  catsEl.innerHTML = CPI_CATEGORY_ORDER.map(cat => {
    const items = groups[cat];
    if (!items.length) return '';
    const okC = items.filter(s => (s.observations||[]).length).length;
    const cards = items.map(renderCpiCardV1).join('');
    return ''
      + '<section class="cpi-cat" data-cpi-cat="' + escapeHtml(cat) + '">'
      +   '<div class="cpi-cat__head">'
      +     '<h3 class="cpi-cat__title">' + escapeHtml(CPI_CATEGORY_LABELS[cat] || cat) + '</h3>'
      +     '<span class="cpi-cat__count">' + okC + ' / ' + items.length + '</span>'
      +   '</div>'
      +   '<div class="cpi-grid">' + cards + '</div>'
      + '</section>';
  }).join('');
}

function renderCpiCardV1(s){
  const label = escapeHtml(s.label || s.id || 'series');
  const unit  = escapeHtml(s.unit || '');
  const obs   = s.observations || [];
  const idAttr = escapeHtml(s.id || '');
  if (!obs.length){
    const err = escapeHtml(s.error || 'no observations');
    return ''
      + '<div class="cpi-mini" data-cpi-series="' + idAttr + '">'
      +   '<div class="cpi-mini__head"><div style="min-width:0">'
      +     '<div class="cpi-mini__label">' + label + '</div>'
      +     '<div class="cpi-mini__unit">' + unit + '</div>'
      +   '</div><span class="cpi-mini__chip" style="color:#f59e0b;border-color:#f59e0b">n/a</span></div>'
      +   '<div class="cpi-mini__err">' + err + '</div>'
      + '</div>';
  }
  // Clip to range.
  const range = state.cpiTimeRange || 'all';
  const years = ({ '5y':5, '10y':10, '20y':20, '30y':30 })[range] || null;
  let clipped = obs;
  if (years){
    const cutoff = new Date();
    cutoff.setFullYear(cutoff.getFullYear() - years);
    const cutoffStr = cutoff.toISOString().slice(0,10);
    clipped = obs.filter(o => o.date >= cutoffStr);
    if (clipped.length < 2) clipped = obs;
  }
  const mode = state.cpiViewMode || 'index100';
  const base = clipped[0].value;
  let pts;
  if (mode === 'absolute')       pts = clipped.map(o => ({date:o.date, value:o.value}));
  else if (mode === 'pctchange') pts = clipped.map(o => ({date:o.date, value: base ? ((o.value-base)/base)*100 : 0}));
  else                            pts = clipped.map(o => ({date:o.date, value: base ? (o.value/base)*100 : 100}));

  const startVal = clipped[0].value;
  const endVal   = clipped[clipped.length-1].value;
  // Headline value tracks the active view so it matches the chart: the
  // rebased index in index100, the % change in pctchange, the raw value
  // (dollar or index) in absolute.
  const dispVal  = pts[pts.length-1].value;
  const pctRange = startVal ? ((endVal - startVal) / startVal) * 100 : 0;
  const isDollar = (s.kind === 'dollar');
  const fmtValue = v => {
    if (mode === 'pctchange') return (v >= 0 ? '+' : '') + Number(v).toFixed(1) + '%';
    if (mode === 'index100')  return Number(v).toFixed(1);
    return isDollar ? '$' + Number(v).toFixed(2)
                    : Number(v).toFixed(Math.abs(v) >= 100 ? 1 : 2);
  };
  // YoY chip from raw obs (so chip is meaningful even when range < 1y).
  const lastRaw = obs[obs.length-1];
  let yoyPct = null;
  if (lastRaw && lastRaw.date){
    const lastDate = new Date(lastRaw.date);
    const targetDate = new Date(lastDate);
    targetDate.setFullYear(targetDate.getFullYear() - 1);
    const targetStr = targetDate.toISOString().slice(0,10);
    let prior = null;
    for (let i = obs.length-1; i >= 0; i--){
      if (obs[i].date <= targetStr){ prior = obs[i]; break; }
    }
    if (prior && prior.value) yoyPct = ((lastRaw.value - prior.value) / prior.value) * 100;
  }
  const chipPct = (yoyPct !== null) ? yoyPct : pctRange;
  const chipLbl = (yoyPct !== null) ? 'YoY' : ('since ' + pts[0].date.slice(0,4));
  // CPI rising = bad for consumers. Red = bad, green = good.
  const sevCls = chipPct >= 0 ? 'bad' : 'good';
  const arrow  = chipPct >= 0 ? '▲' : '▼';
  const chipText = arrow + ' ' + Math.abs(chipPct).toFixed(1) + '% ' + chipLbl;

  return ''
    + '<div class="cpi-mini" data-cpi-series="' + idAttr + '">'
    +   '<div class="cpi-mini__head"><div style="min-width:0">'
    +     '<div class="cpi-mini__label" title="' + escapeHtml(s.label || '') + '">' + label + '</div>'
    +     '<div class="cpi-mini__unit">' + unit + '</div>'
    +   '</div><div style="display:flex;flex-direction:column;align-items:flex-end;gap:2px;flex-shrink:0">'
    +     '<div class="cpi-mini__val">' + fmtValue(dispVal) + '</div>'
    +     '<span class="cpi-mini__chip ' + sevCls + '">' + escapeHtml(chipText) + '</span>'
    +   '</div></div>'
    +   cpiSparkSvgV1(pts, {isDollar, mode})
    + '</div>';
}

function cpiSparkSvgV1(pts, opts){
  const W = 320, H = 80, PADL = 32, PADR = 4, PADT = 6, PADB = 16;
  if (!pts || pts.length < 2){
    return '<div class="empty" style="padding:8px 0;font-size:11px">Not enough points to chart.</div>';
  }
  const ys = pts.map(p => p.value);
  const ymin = Math.min.apply(null, ys);
  const ymax = Math.max.apply(null, ys);
  const yspan = (ymax - ymin) || 1;
  const xspan = (pts.length - 1) || 1;
  const sx = i => PADL + (i / xspan) * (W - PADL - PADR);
  const sy = v => PADT + (1 - (v - ymin) / yspan) * (H - PADT - PADB);
  const path = pts.map((p,i) => (i===0 ? 'M' : 'L') + sx(i).toFixed(1) + ' ' + sy(p.value).toFixed(1)).join(' ');
  const area = path + ' L' + sx(pts.length-1).toFixed(1) + ' ' + (H-PADB).toFixed(1)
             + ' L' + sx(0).toFixed(1) + ' ' + (H-PADB).toFixed(1) + ' Z';
  const fmtY = v => (opts && opts.isDollar && opts.mode === 'absolute') ? '$' + v.toFixed(2)
                  : (opts && opts.mode === 'pctchange') ? v.toFixed(0) + '%'
                  : v.toFixed(1);
  const yticks = [ymin, ymax].map(v => ({v, y: sy(v)}));
  const yAxis = yticks.map(t =>
    '<line x1="' + PADL + '" y1="' + t.y.toFixed(1) + '" x2="' + (W-PADR) + '" y2="' + t.y.toFixed(1) +
      '" stroke="#252b3a" stroke-width="1" stroke-dasharray="2 3"/>' +
    '<text x="' + (PADL-4) + '" y="' + (t.y+3).toFixed(1) + '" font-size="9" fill="#8a93a6" text-anchor="end">' +
      escapeHtml(fmtY(t.v)) + '</text>'
  ).join('');
  const xLabels =
    '<text x="' + sx(0).toFixed(1) + '" y="' + (H-4) + '" font-size="9" fill="#8a93a6" text-anchor="start">' + escapeHtml(pts[0].date.slice(0,7)) + '</text>' +
    '<text x="' + sx(pts.length-1).toFixed(1) + '" y="' + (H-4) + '" font-size="9" fill="#8a93a6" text-anchor="end">' + escapeHtml(pts[pts.length-1].date.slice(0,7)) + '</text>';
  const stroke = '#f59e0b';
  const fill = 'rgba(245,158,11,0.10)';
  const lastTip = pts[pts.length-1].date + ': ' + fmtY(pts[pts.length-1].value);
  return ''
    + '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" height="' + H + '" preserveAspectRatio="none" role="img" aria-label="CPI series chart">'
    +   '<title>' + escapeHtml(lastTip) + '</title>'
    +   yAxis
    +   '<path d="' + area + '" fill="' + fill + '" stroke="none"/>'
    +   '<path d="' + path + '" fill="none" stroke="' + stroke + '" stroke-width="1.5" stroke-linejoin="round"/>'
    +   '<circle cx="' + sx(pts.length-1).toFixed(1) + '" cy="' + sy(pts[pts.length-1].value).toFixed(1) + '" r="2.5" fill="' + stroke + '"/>'
    +   xLabels
    + '</svg>';
}

// CPI tab control wiring — delegated on #tab-cpi.
(function(){
  const root = document.getElementById('tab-cpi');
  if (!root) return;
  root.addEventListener('click', (e) => {
    const r = e.target.closest('.btn[data-cpirange]');
    if (r){
      const v = r.dataset.cpirange;
      if (v && v !== state.cpiTimeRange){ state.cpiTimeRange = v; renderCpiTab(); }
      return;
    }
    const m = e.target.closest('.btn[data-cpiview]');
    if (m){
      const v = m.dataset.cpiview;
      if (v && v !== state.cpiViewMode){ state.cpiViewMode = v; renderCpiTab(); }
    }
  });
})();

// ---------- SUPPLIES tab ----------
function renderSuppliesTab(){
  const sup = DATA.supplies;
  const loading = document.getElementById('suppliesLoading');
  const content = document.getElementById('suppliesContent');
  if (!sup && state.tab === 'supplies' && SIDECAR_STATE.supplies === 'loading'){
    if (loading) loading.classList.remove('hidden');
    if (content) content.classList.add('hidden');
    return;
  }
  if (loading) loading.classList.add('hidden');
  if (content) content.classList.remove('hidden');
  if (!sup) {
    const snap = document.getElementById('suppliesSnapshot');
    if (snap) snap.innerHTML = '<div class="empty" style="padding:24px 12px;grid-column:1/-1">Supplies sidecar not yet loaded.</div>';
    return;
  }
  renderSuppliesSnapshotV1(sup);
  renderSuppliesPortsV1(sup);
  renderSuppliesInventoryV1(sup);
  renderSuppliesGscpiV1(sup);
}

function _suppPctDelta(obs, key){
  if (!Array.isArray(obs) || obs.length < 2) return null;
  const a = obs[obs.length-2][key];
  const b = obs[obs.length-1][key];
  if (a == null || b == null || a === 0) return null;
  return ((b - a) / a) * 100;
}
function _suppFmtPct(p){
  if (p == null || !isFinite(p)) return '—';
  return (p > 0 ? '+' : '') + p.toFixed(1) + '%';
}
function _suppFmtAbs(v){
  if (v == null || !isFinite(v)) return '—';
  if (Math.abs(v) >= 1e6) return (v/1e6).toFixed(2) + 'M';
  if (Math.abs(v) >= 1e3) return (v/1e3).toFixed(0) + 'k';
  return String(v);
}

function renderSuppliesSnapshotV1(sup){
  const host = document.getElementById('suppliesSnapshot');
  if (!host) return;
  const cards = [];
  const la = ((sup.port_teu||{}).los_angeles) || null;
  const inv = sup.inventory_ratio || null;
  const gscpi = sup.gscpi || null;

  function snapCard(title, sub, val, delta, deltaCls){
    const d = (delta == null) ? '' : '<span class="cpi-mini__chip ' + deltaCls + '" style="margin-left:6px">' + escapeHtml(delta) + '</span>';
    return ''
      + '<div class="card" style="padding:12px">'
      +   '<h3>' + escapeHtml(title) + '</h3>'
      +   '<div class="v">' + escapeHtml(val) + d + '</div>'
      +   '<div class="sub">' + escapeHtml(sub) + '</div>'
      + '</div>';
  }
  if (la && Array.isArray(la.observations) && la.observations.length){
    const last = la.observations[la.observations.length-1];
    const delta = _suppPctDelta(la.observations, 'total');
    const cls = (delta == null) ? '' : (delta > 0 ? 'good' : (delta < 0 ? 'bad' : ''));
    cards.push(snapCard('Port of L.A. · total', 'Monthly TEU · ' + (la.as_of||''), _suppFmtAbs(last.total), _suppFmtPct(delta) + ' MoM', cls));
    // Second card: loaded-imports-only — leading indicator for US consumer
    // demand (replaces the NY/NJ slot, since panynj.gov is unscrapeable and
    // the data.ny.gov Socrata feed it used to come from was retired 2015).
    if (last.loaded_imports != null){
      const deltaI = _suppPctDelta(la.observations, 'loaded_imports');
      const clsI = (deltaI == null) ? '' : (deltaI > 0 ? 'good' : (deltaI < 0 ? 'bad' : ''));
      cards.push(snapCard('Port of L.A. · loaded imports', 'Monthly TEU · ' + (la.as_of||''), _suppFmtAbs(last.loaded_imports), _suppFmtPct(deltaI) + ' MoM', clsI));
    }
  }
  if (inv && Array.isArray(inv.observations) && inv.observations.length){
    const last = inv.observations[inv.observations.length-1];
    const prev = inv.observations[inv.observations.length-2];
    const delta = (prev && prev.value) ? ((last.value - prev.value) / prev.value) * 100 : null;
    // Higher ratio = sluggish demand. Invert the color.
    const cls = (delta == null) ? '' : (delta > 0 ? 'bad' : (delta < 0 ? 'good' : ''));
    cards.push(snapCard('Inventory/Sales', 'ISRATIO · ' + (inv.as_of||''), last.value.toFixed(2), _suppFmtPct(delta) + ' MoM', cls));
  } else if (inv && inv.available === false){
    cards.push(snapCard('Inventory/Sales', 'FRED_API_KEY not set', '—', null, ''));
  }
  if (gscpi && Array.isArray(gscpi.observations) && gscpi.observations.length){
    const last = gscpi.observations[gscpi.observations.length-1];
    const prev = gscpi.observations[gscpi.observations.length-2];
    const delta = prev ? (last.value - prev.value) : null;
    // GSCPI: positive = stress. Color by level here (not delta).
    const cls = (last.value > 1) ? 'bad' : (last.value < -1 ? 'good' : '');
    const deltaStr = (delta == null) ? '' : ((delta > 0 ? '+' : '') + delta.toFixed(2) + ' MoM');
    cards.push(snapCard('GSCPI', 'NY Fed · ' + (gscpi.as_of||''), last.value.toFixed(2) + 'σ', deltaStr, cls));
  }
  host.innerHTML = cards.length ? cards.join('') : '<div class="empty" style="padding:24px 12px;grid-column:1/-1">No supply data on the last fetch.</div>';
}

// Shared inline-SVG line chart for the Supplies tab. series:
//   [{label, color:'orange'|'info'|'ai'|'good'|'bad', values:[{x,y}], xLabels:{first,last}, dashed?}]
function svgLineChartV1(series, opts){
  const o = Object.assign({width:560, height:200, padX:38, padY:14, zeroLine:false,
                           yFmt: (v) => v.toFixed(2)}, opts || {});
  const W = o.width, H = o.height, PX = o.padX, PY = o.padY;
  const allPts = [];
  for (const s of series){ for (const p of (s.values||[])){ allPts.push(p); } }
  if (!allPts.length){
    return '<div class="empty" style="padding:24px 12px">No data.</div>';
  }
  let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
  for (const p of allPts){
    if (p.x < xMin) xMin = p.x;
    if (p.x > xMax) xMax = p.x;
    if (p.y < yMin) yMin = p.y;
    if (p.y > yMax) yMax = p.y;
  }
  if (yMin === yMax){ yMin -= 1; yMax += 1; }
  const yPad = (yMax - yMin) * 0.08;
  yMin -= yPad; yMax += yPad;
  const xScale = (x) => PX + ((x - xMin) / Math.max(1, (xMax - xMin))) * (W - PX*2);
  const yScale = (y) => H - PY - ((y - yMin) / Math.max(1e-9, (yMax - yMin))) * (H - PY*2);
  function color(name){
    const map = { good:'#22c55e', bad:'#ef4444', warn:'#f59e0b',
                  info:'#06b6d4', ai:'#a78bfa', orange:'#f59e0b' };
    return map[name] || '#06b6d4';
  }
  const ticks = [];
  for (let i=0; i<=3; i++){
    const v = yMin + ((yMax - yMin) * i / 3);
    ticks.push({v, y: yScale(v)});
  }
  let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" preserveAspectRatio="xMidYMid meet" style="display:block;max-width:100%">';
  for (const t of ticks){
    svg += '<line x1="' + PX + '" x2="' + (W-PX) + '" y1="' + t.y + '" y2="' + t.y + '" stroke="#252b3a" stroke-width="1" stroke-dasharray="2 4"/>';
    svg += '<text x="' + (PX-6) + '" y="' + (t.y+4) + '" font-size="10" fill="#8a93a6" text-anchor="end">' + escapeHtml(o.yFmt(t.v)) + '</text>';
  }
  if (o.zeroLine && yMin < 0 && yMax > 0){
    const y0 = yScale(0);
    svg += '<line x1="' + PX + '" x2="' + (W-PX) + '" y1="' + y0 + '" y2="' + y0 + '" stroke="#8a93a6" stroke-width="1.5"/>';
  }
  for (const s of series){
    if (!s.values || !s.values.length) continue;
    const pts = s.values.map(p => xScale(p.x) + ',' + yScale(p.y)).join(' ');
    const dash = s.dashed ? ' stroke-dasharray="4 3"' : '';
    svg += '<polyline points="' + pts + '" fill="none" stroke="' + color(s.color) + '" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"' + dash + '/>';
    const last = s.values[s.values.length-1];
    svg += '<circle cx="' + xScale(last.x) + '" cy="' + yScale(last.y) + '" r="3" fill="' + color(s.color) + '"/>';
  }
  if (series[0] && series[0].xLabels){
    const xl = series[0].xLabels;
    svg += '<text x="' + PX + '" y="' + (H-2) + '" font-size="10" fill="#8a93a6" text-anchor="start">' + escapeHtml(xl.first || '') + '</text>';
    svg += '<text x="' + (W-PX) + '" y="' + (H-2) + '" font-size="10" fill="#8a93a6" text-anchor="end">' + escapeHtml(xl.last || '') + '</text>';
  }
  svg += '</svg>';
  const legend = series.map(s => '<span style="display:inline-flex;align-items:center;gap:6px;margin-right:14px;font-size:11px;color:#8a93a6">'
    + '<span style="width:10px;height:2px;background:' + color(s.color) + ';display:inline-block"></span>'
    + escapeHtml(s.label || '') + '</span>').join('');
  return svg + '<div style="margin-top:6px">' + legend + '</div>';
}

function _trimByMonths(obs, n){
  if (!Array.isArray(obs) || obs.length === 0) return [];
  const k = Math.min(obs.length, n);
  return obs.slice(obs.length - k);
}
function _ymToX(s){
  if (!s) return 0;
  const p = String(s).split('-');
  const y = parseInt(p[0], 10);
  const m = parseInt(p[1], 10);
  if (isNaN(y) || isNaN(m)) return 0;
  return y * 12 + (m - 1);
}
function _fmtYearMonth(s){
  if (!s) return '';
  const p = String(s).split('-');
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const mi = parseInt(p[1], 10) - 1;
  if (mi < 0 || mi > 11) return s;
  return months[mi] + ' ' + p[0];
}

function renderSuppliesPortsV1(sup){
  const host = document.getElementById('suppliesPortsChart');
  const foot = document.getElementById('suppliesPortsFoot');
  const asOf = document.getElementById('suppliesPortsAsOf');
  if (!host) return;
  const la = ((sup.port_teu||{}).los_angeles) || null;
  if (!la){
    host.innerHTML = '<div class="empty" style="padding:24px 12px">No port data on the last fetch.</div>';
    if (foot) foot.textContent = '';
    if (asOf) asOf.textContent = '—';
    return;
  }
  const trimmed = _trimByMonths(la.observations || [], 60);
  const xLabels = {first: _fmtYearMonth(trimmed[0] && trimmed[0].month),
                   last:  _fmtYearMonth(trimmed[trimmed.length-1] && trimmed[trimmed.length-1].month)};
  const series = [
    {label: 'L.A. total monthly TEU', color: 'orange',
     values: trimmed.map(o => ({x: _ymToX(o.month), y: o.total})),
     xLabels: xLabels},
  ];
  // Loaded imports = subset of total; included as a second line because it's
  // the leading indicator that actually tracks US consumer demand (empties
  // and exports add noise). Skip when the underlying source lacks the
  // breakdown (older POLA pages were total-only for a few rows).
  const hasLoadedImp = trimmed.some(o => o.loaded_imports != null);
  if (hasLoadedImp){
    series.push({label: 'L.A. loaded imports (subset)', color: 'info',
                 values: trimmed.filter(o => o.loaded_imports != null).map(o => ({x: _ymToX(o.month), y: o.loaded_imports})),
                 xLabels: xLabels});
  }
  host.innerHTML = svgLineChartV1(series, {
    yFmt: (v) => (v >= 1e6 ? (v/1e6).toFixed(1) + 'M' : (v/1e3).toFixed(0) + 'k'),
  });
  if (foot) foot.textContent = 'Source: ' + la.source + (la.as_of ? ' · ' + la.as_of : '') + ' · NY/NJ feed retired by publisher in 2015; L.A. shown alone.';
  if (asOf) asOf.textContent = la.as_of || '—';
}

function renderSuppliesInventoryV1(sup){
  const host = document.getElementById('suppliesInvChart');
  const foot = document.getElementById('suppliesInvFoot');
  const asOf = document.getElementById('suppliesInvAsOf');
  if (!host) return;
  const inv = sup.inventory_ratio;
  if (!inv || inv.available === false){
    host.innerHTML = '<div class="empty" style="padding:24px 12px">FRED_API_KEY required &mdash; free key at fredaccount.stlouisfed.org enables this chart.</div>';
    if (foot) foot.textContent = 'Series: FRED ISRATIO · Total business inventory-to-sales ratio';
    if (asOf) asOf.textContent = 'not configured';
    return;
  }
  if (!Array.isArray(inv.observations) || !inv.observations.length){
    host.innerHTML = '<div class="empty" style="padding:24px 12px">FRED returned an empty series.</div>';
    if (foot) foot.textContent = '';
    if (asOf) asOf.textContent = '—';
    return;
  }
  const trimmed = _trimByMonths(inv.observations, 120);
  const series = [{
    label: 'Inventory/Sales ratio', color: 'ai',
    values: trimmed.map(o => ({x: _ymToX(o.date), y: o.value})),
    xLabels: {first: _fmtYearMonth(trimmed[0] && trimmed[0].date),
              last:  _fmtYearMonth(trimmed[trimmed.length-1] && trimmed[trimmed.length-1].date)},
  }];
  host.innerHTML = svgLineChartV1(series, { yFmt: (v) => v.toFixed(2) });
  if (foot) foot.textContent = 'Source: ' + (inv.source || 'FRED') + ' · ' + (inv.label || 'ISRATIO');
  if (asOf) asOf.textContent = inv.as_of || '—';
}

function renderSuppliesGscpiV1(sup){
  const host = document.getElementById('suppliesGscpiChart');
  const foot = document.getElementById('suppliesGscpiFoot');
  const asOf = document.getElementById('suppliesGscpiAsOf');
  if (!host) return;
  const g = sup.gscpi;
  if (!g || !Array.isArray(g.observations) || !g.observations.length){
    host.innerHTML = '<div class="empty" style="padding:24px 12px">GSCPI unavailable &mdash; NY Fed CSV returned nothing.</div>';
    if (foot) foot.textContent = '';
    if (asOf) asOf.textContent = '—';
    return;
  }
  const trimmed = _trimByMonths(g.observations, 120);
  const last = trimmed[trimmed.length-1];
  const col = (last && last.value > 0) ? 'orange' : 'good';
  const series = [{
    label: 'GSCPI (σ from mean)', color: col,
    values: trimmed.map(o => ({x: _ymToX(o.date), y: o.value})),
    xLabels: {first: _fmtYearMonth(trimmed[0] && trimmed[0].date),
              last:  _fmtYearMonth(trimmed[trimmed.length-1] && trimmed[trimmed.length-1].date)},
  }];
  host.innerHTML = svgLineChartV1(series, {
    zeroLine: true,
    yFmt: (v) => (v >= 0 ? '+' : '') + v.toFixed(1) + 'σ',
  });
  if (foot) foot.textContent = 'Source: ' + (g.source || 'NY Fed') + ' · published monthly · zero = historical average';
  if (asOf) asOf.textContent = g.as_of || '—';
}

// ---------- METALS tab ----------
function renderMetalsTab(){
  const m = DATA.metals;
  const loading = document.getElementById('metalsLoading');
  const content = document.getElementById('metalsContent');
  if (!m && state.tab === 'metals' && SIDECAR_STATE.metals === 'loading'){
    if (loading) loading.classList.remove('hidden');
    if (content) content.classList.add('hidden');
    return;
  }
  if (loading) loading.classList.add('hidden');
  if (content) content.classList.remove('hidden');
  if (!m || typeof m !== 'object'){
    const strength = document.getElementById('metalsStrengthBody');
    if (strength) strength.innerHTML = '<div class="empty" style="padding:24px 12px">Metals sidecar not yet loaded.</div>';
    return;
  }
  metalsRenderStrengthV1(m);
  metalsRenderPriceCardV1('gold',   m.gold_price,   'metalsGoldBody',   'metalsGoldAsOf',   'metalsGoldSub',   '#f59e0b');
  metalsRenderPriceCardV1('silver', m.silver_price, 'metalsSilverBody', 'metalsSilverAsOf', 'metalsSilverSub', '#cbd5e1');
  metalsRenderProdCardV1(m.gold_mine_production,   {bodyId:'metalsGoldProdBody',   asOfId:'metalsGoldProdAsOf',   subId:'metalsGoldProdSub',   emptyTitle:'No gold production data',   color:'#f59e0b'});
  metalsRenderProdCardV1(m.silver_mine_production, {bodyId:'metalsSilverProdBody', asOfId:'metalsSilverProdAsOf', subId:'metalsSilverProdSub', emptyTitle:'No silver production data', color:'#cbd5e1'});
  metalsRenderCBGoldV1(m.central_bank_gold);
}

function _metalsFmtNum(n, d){
  if (n == null || isNaN(n)) return '—';
  if (d == null) d = 2;
  return Number(n).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}
function _metalsFmtInt(n){
  if (n == null || isNaN(n)) return '—';
  return Number(n).toLocaleString('en-US', {maximumFractionDigits: 0});
}
function _metalsPctChip(pct){
  if (pct == null || !isFinite(pct)) return '<span class="metals-pct">—</span>';
  const cls = pct >= 0 ? 'good' : 'bad';
  const sign = pct >= 0 ? '+' : '';
  return '<span class="metals-pct ' + cls + '">' + sign + pct.toFixed(2) + '%</span>';
}
function _metalsLookback(obs, days){
  if (!Array.isArray(obs) || obs.length < 2) return null;
  const idx = Math.max(0, obs.length - 1 - days);
  return obs[idx];
}

function metalsRenderStrengthV1(m){
  const body = document.getElementById('metalsStrengthBody');
  const asOf = document.getElementById('metalsStrengthAsOf');
  const sub  = document.getElementById('metalsStrengthSub');
  if (!body) return;
  const gold   = (m && m.gold_price && Array.isArray(m.gold_price.observations))   ? m.gold_price.observations : [];
  const silver = (m && m.silver_price && Array.isArray(m.silver_price.observations)) ? m.silver_price.observations : [];
  if (gold.length < 30 || silver.length < 30){
    body.innerHTML = '<div class="empty" style="padding:24px 12px">Insufficient price history &mdash; need 30+ daily closes for both metals.</div>';
    if (asOf) asOf.textContent = 'unavailable';
    return;
  }
  // Gold/silver ratio
  const pairLen = Math.min(gold.length, silver.length);
  const gTail = gold.slice(-pairLen);
  const sTail = silver.slice(-pairLen);
  const ratioObs = [];
  for (let i=0; i < pairLen; i++){
    const gv = gTail[i].value, sv = sTail[i].value;
    if (gv != null && sv != null && sv !== 0){
      ratioObs.push({date: gTail[i].date || sTail[i].date, value: gv / sv});
    }
  }
  const ratioNow = ratioObs.length ? ratioObs[ratioObs.length-1].value : null;
  let ratioLabel = '—', ratioCls = '';
  if (ratioNow != null){
    if (ratioNow < 50){ ratioLabel = 'Silver-strong'; ratioCls = 'good'; }
    else if (ratioNow > 70){ ratioLabel = 'Gold-strong'; ratioCls = 'bad'; }
    else { ratioLabel = 'Neutral band'; ratioCls = ''; }
  }
  const sparkColor = ratioCls === 'good' ? '#22c55e' : (ratioCls === 'bad' ? '#f59e0b' : '#06b6d4');
  const ratioSpark = metalsSparkV1(ratioObs.slice(-90), sparkColor);
  const col1 = ''
    + '<div style="display:flex;flex-direction:column;gap:6px;min-width:0">'
    +   '<div class="metals-col__lbl">Gold / silver ratio</div>'
    +   '<div style="display:flex;align-items:baseline;gap:8px;flex-wrap:wrap">'
    +     '<div style="font-size:24px;font-weight:700;color:var(--text)">' + (ratioNow == null ? '—' : ratioNow.toFixed(1)) + '</div>'
    +     '<span class="metals-pct ' + ratioCls + '">' + escapeHtml(ratioLabel) + '</span>'
    +   '</div>'
    +   ratioSpark
    +   '<div class="metals-col__note">Historical mean ~60. Above = silver undervalued; below = silver outperforming.</div>'
    + '</div>';

  // Period returns
  const windows = [
    {label:'1M', days:21}, {label:'3M', days:63}, {label:'6M', days:126},
    {label:'1Y', days:252}, {label:'5Y', days:1260},
  ];
  function returnsRow(name, obs){
    const last = obs[obs.length-1];
    const cells = windows.map(w => {
      if (obs.length <= w.days){
        return '<div class="metals-cell"><div class="metals-cell__lbl">' + w.label + '</div><div><span class="metals-pct">n/a</span></div></div>';
      }
      const ref = obs[obs.length-1-w.days];
      const pct = (ref && ref.value) ? ((last.value - ref.value) / ref.value) * 100 : null;
      return '<div class="metals-cell"><div class="metals-cell__lbl">' + w.label + '</div><div>' + _metalsPctChip(pct) + '</div></div>';
    }).join('');
    return '<div class="metals-row"><div class="metals-row__name">' + escapeHtml(name) + '</div><div class="metals-row__cells">' + cells + '</div></div>';
  }
  const col2 = ''
    + '<div style="display:flex;flex-direction:column;gap:4px;min-width:0">'
    +   '<div class="metals-col__lbl">Period returns</div>'
    +   returnsRow('Gold', gold)
    +   returnsRow('Silver', silver)
    +   '<div class="metals-col__note">Trading-day lookbacks vs latest close.</div>'
    + '</div>';

  // 52-week position
  function fiftyTwo(name, obs){
    const window = obs.slice(-252);
    let lo = Infinity, hi = -Infinity;
    for (const p of window){ if (p.value < lo) lo = p.value; if (p.value > hi) hi = p.value; }
    const cur = obs[obs.length-1].value;
    const range = hi - lo;
    const pos = range > 0 ? Math.max(0, Math.min(1, (cur - lo) / range)) : 0.5;
    const posPct = Math.round(pos * 100);
    const dotColor = posPct >= 70 ? '#f59e0b' : (posPct <= 30 ? '#22c55e' : '#06b6d4');
    const bar = ''
      + '<div style="position:relative;height:8px;background:#0b0d12;border-radius:4px;margin:4px 0">'
      +   '<div style="position:absolute;left:0;right:0;top:50%;height:1px;background:rgba(148,163,184,0.25)"></div>'
      +   '<div style="position:absolute;left:' + posPct + '%;top:-2px;width:4px;height:12px;background:' + dotColor + ';border-radius:2px;transform:translateX(-2px)"></div>'
      + '</div>';
    return ''
      + '<div style="display:flex;flex-direction:column;gap:2px;min-width:0;margin-bottom:6px">'
      +   '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:8px">'
      +     '<div style="font-size:11px;color:var(--text);font-weight:600">' + escapeHtml(name) + '</div>'
      +     '<div style="font-size:11px;color:var(--muted)">$' + _metalsFmtNum(cur, 2) + ' &middot; <span style="color:var(--text)">' + posPct + '%</span> of 52w</div>'
      +   '</div>'
      +   bar
      +   '<div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted)">'
      +     '<span>low $' + _metalsFmtNum(lo, 2) + '</span>'
      +     '<span>high $' + _metalsFmtNum(hi, 2) + '</span>'
      +   '</div>'
      + '</div>';
  }
  const col3 = ''
    + '<div style="display:flex;flex-direction:column;gap:4px;min-width:0">'
    +   '<div class="metals-col__lbl">52-week position</div>'
    +   fiftyTwo('Gold', gold)
    +   fiftyTwo('Silver', silver)
    + '</div>';

  body.innerHTML = '<div class="metals-strength">' + col1 + col2 + col3 + '</div>';
  const lastDate = gold[gold.length-1].date || silver[silver.length-1].date || '—';
  if (asOf) asOf.textContent = 'as of ' + lastDate;
  if (sub) sub.textContent = 'Derived from ' + Math.min(gold.length, silver.length) + ' daily closes · ratio, returns, 52w range';
}

function metalsSparkV1(points, color){
  if (!Array.isArray(points) || points.length < 2) return '';
  const W = 240, H = 30, padT = 3, padB = 3;
  let lo = Infinity, hi = -Infinity;
  for (const p of points){ if (p.value < lo) lo = p.value; if (p.value > hi) hi = p.value; }
  if (!isFinite(lo) || !isFinite(hi) || hi === lo) hi = lo + 1;
  const n = points.length;
  const pts = points.map((p, i) => {
    const x = (i / (n - 1)) * W;
    const y = padT + (1 - (p.value - lo) / (hi - lo)) * (H - padT - padB);
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="width:100%;height:30px;display:block;border-radius:4px;background:#0b0d12">'
    + '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.4" vector-effect="non-scaling-stroke"/>'
    + '</svg>';
}

function metalsRenderPriceCardV1(metal, payload, bodyId, asOfId, subId, color){
  const body = document.getElementById(bodyId);
  const asOf = document.getElementById(asOfId);
  const sub  = document.getElementById(subId);
  if (!body) return;
  const obs = (payload && Array.isArray(payload.observations)) ? payload.observations : [];
  if (!obs.length){
    body.innerHTML = '<div class="empty" style="padding:24px 12px">No ' + escapeHtml(metal) + ' price data.</div>';
    if (asOf) asOf.textContent = 'unavailable';
    return;
  }
  const last = obs[obs.length-1];
  const lastVal = last.value;
  const refs = [
    {label:'1d', obs: _metalsLookback(obs, 1)},
    {label:'1w', obs: _metalsLookback(obs, 5)},
    {label:'1m', obs: _metalsLookback(obs, 21)},
    {label:'1y', obs: _metalsLookback(obs, 252)},
  ];
  const deltas = refs.map(r => {
    if (!r.obs) return {label:r.label, pct:null};
    return {label:r.label, pct: (lastVal - r.obs.value) / r.obs.value * 100};
  });
  // Inline SVG line chart of full series.
  const W = 600, Hc = 160, padL = 8, padR = 8, padT = 6, padB = 18;
  const innerW = W - padL - padR, innerH = Hc - padT - padB;
  let lo = Infinity, hi = -Infinity;
  for (const p of obs){ if (p.value < lo) lo = p.value; if (p.value > hi) hi = p.value; }
  if (!isFinite(lo) || !isFinite(hi) || hi === lo) hi = lo + 1;
  const xStep = innerW / Math.max(1, obs.length - 1);
  const pts = obs.map((p, i) => {
    const x = padL + i * xStep;
    const y = padT + innerH - ((p.value - lo) / (hi - lo)) * innerH;
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  const areaPts = pts + ' ' + (padL + (obs.length - 1) * xStep).toFixed(1) + ',' +
                  (padT + innerH).toFixed(1) + ' ' + padL.toFixed(1) + ',' +
                  (padT + innerH).toFixed(1);
  const fillId = 'metalsFillV1_' + metal;
  let lastYearTick = '';
  const yearTicks = obs.map((p, i) => {
    const y = (p.date || '').slice(0, 4);
    if (y && y !== lastYearTick && i % Math.ceil(obs.length/6) === 0){
      lastYearTick = y;
      const x = padL + i * xStep;
      return '<text x="' + x.toFixed(0) + '" y="' + (Hc - 4) + '" text-anchor="middle" font-size="9" fill="#8a93a6">' + y + '</text>';
    }
    return '';
  }).join('');
  const chartSvg = ''
    + '<svg viewBox="0 0 ' + W + ' ' + Hc + '" preserveAspectRatio="none" style="width:100%;height:auto;max-height:200px;display:block;border-radius:6px;background:#0b0d12">'
    +   '<defs><linearGradient id="' + fillId + '" x1="0" y1="0" x2="0" y2="1">'
    +     '<stop offset="0%" stop-color="' + color + '" stop-opacity="0.35"/>'
    +     '<stop offset="100%" stop-color="' + color + '" stop-opacity="0"/>'
    +   '</linearGradient></defs>'
    +   '<polygon points="' + areaPts + '" fill="url(#' + fillId + ')"/>'
    +   '<polyline points="' + pts + '" fill="none" stroke="' + color + '" stroke-width="1.4" vector-effect="non-scaling-stroke"/>'
    +   yearTicks
    + '</svg>';
  const deltaCells = deltas.map(d =>
    '<div class="metals-cell"><div class="metals-cell__lbl">' + d.label + '</div><div>' + _metalsPctChip(d.pct) + '</div></div>'
  ).join('');
  body.innerHTML = ''
    + '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:6px">'
    +   '<div style="font-size:24px;font-weight:700;color:var(--text)">$' + _metalsFmtNum(lastVal, 2) + '</div>'
    +   '<div style="font-size:11px;color:var(--muted)">/troy oz</div>'
    + '</div>'
    + chartSvg
    + '<div style="display:flex;justify-content:space-between;margin-top:10px;gap:6px">' + deltaCells + '</div>'
    + '<div class="sub" style="font-size:11px;color:var(--muted);margin-top:8px">Source: ' + escapeHtml(payload.source || '—') + '</div>';
  if (asOf) asOf.textContent = 'as of ' + (last.date || '—');
  if (sub) sub.textContent = 'USD per troy ounce · ' + obs.length + ' daily closes';
}

function metalsHBarV1(rows, color){
  if (!rows.length) return '';
  const W = 600, rowH = 22, padTop = 4;
  const Hc = padTop + rows.length * rowH + 4;
  const max = rows[0].value || 1;
  const labelW = 150, valueW = 70;
  const barX = labelW + 6;
  const barW = W - barX - valueW - 4;
  const parts = rows.map((r, i) => {
    const y = padTop + i * rowH;
    const w = Math.max(2, (r.value / max) * barW);
    const lbl = escapeHtml(r.label);
    const val = _metalsFmtInt(r.value);
    return ''
      + '<text x="' + (labelW - 4) + '" y="' + (y + 14) + '" text-anchor="end" font-size="11" fill="#e6e8ee">' + lbl + '</text>'
      + '<rect x="' + barX + '" y="' + (y + 4) + '" width="' + w.toFixed(1) + '" height="14" rx="2" fill="' + color + '" opacity="0.85"/>'
      + '<text x="' + (barX + w + 4) + '" y="' + (y + 14) + '" font-size="11" fill="#8a93a6">' + val + '</text>';
  }).join('');
  return '<svg viewBox="0 0 ' + W + ' ' + Hc + '" preserveAspectRatio="xMinYMin meet" style="width:100%;height:auto;display:block;background:transparent">' + parts + '</svg>';
}

function metalsRenderCBGoldV1(payload){
  const body = document.getElementById('metalsCBGoldBody');
  const asOf = document.getElementById('metalsCBGoldAsOf');
  const sub  = document.getElementById('metalsCBGoldSub');
  if (!body) return;
  const rows = (payload && Array.isArray(payload.holdings)) ? payload.holdings : [];
  if (!rows.length){
    body.innerHTML = '<div class="empty" style="padding:24px 12px">No central-bank gold data on the last fetch.</div>';
    if (asOf) asOf.textContent = 'unavailable';
    return;
  }
  const bars = rows.map(r => ({label: r.country, value: r.tonnes}));
  body.innerHTML = metalsHBarV1(bars, '#f59e0b')
    + '<div class="sub" style="font-size:11px;color:var(--muted);margin-top:8px">Unit: ' + escapeHtml(payload.unit || 'tonnes')
    + ' · Source: ' + escapeHtml(payload.source || '—') + '</div>';
  if (asOf) asOf.textContent = payload.as_of || '—';
  if (sub) sub.textContent = 'Tonnes · top 20 holders · IMF IRFCL';
}

function metalsRenderProdCardV1(payload, opts){
  const body = document.getElementById(opts.bodyId);
  const asOf = document.getElementById(opts.asOfId);
  const sub  = document.getElementById(opts.subId);
  if (!body) return;
  const all = (payload && Array.isArray(payload.by_country)) ? payload.by_country : [];
  if (!all.length){
    body.innerHTML = '<div class="empty" style="padding:24px 12px">' + escapeHtml(opts.emptyTitle) + '.</div>';
    if (asOf) asOf.textContent = 'unavailable';
    return;
  }
  const top = all.slice(0, 10);
  const bars = top.map(r => ({label: r.country, value: r.tonnes}));
  body.innerHTML = metalsHBarV1(bars, opts.color)
    + '<div class="sub" style="font-size:11px;color:var(--muted);margin-top:8px">Unit: ' + escapeHtml(payload.unit || 'metric tons')
    + ' · Year: ' + (payload.year || '—')
    + ' · Source: ' + escapeHtml(payload.source || '—') + '</div>';
  if (asOf) asOf.textContent = String(payload.year || '—');
  if (sub) sub.textContent = 'Metric tons · top 10 producers · USGS MCS ' + (payload.year || '');
}

// ============================================================================
// TRAVEL ADVISORIES TAB (ported from V2 — V1 visual style)
// ============================================================================
// Sidecar-loaded via SIDECAR_FOR_TAB.travel from /data-travel.json (written
// by fetch_advisories.py during build). renderTravelTab() is a no-op until
// the sidecar lands. Reads state.travelSub / travelSubLevel / travelQuery /
// travelTerrorOnly / travelSort. Mirrors the V2 implementation but uses
// V1 tokens (.chart-card, .travel-* classes mapped to var(--green/amber/red/
// purple)) so it lives alongside CPI / Supplies / Metals visually.

const TRAVEL_LEVEL_SHORT = {
  1: 'Normal Precautions',
  2: 'Increased Caution',
  3: 'Reconsider Travel',
  4: 'Do Not Travel',
};
const TRAVEL_RISK_LABEL = {
  T:'Terrorism', C:'Crime', U:'Civil Unrest', H:'Health',
  K:'Kidnapping', N:'Natural Disaster', D:'Wrongful Detention',
  O:'Other', E:'Time-limited Event',
};

function travelFmtDate(iso){
  if (!iso) return '';
  const parts = String(iso).split('-');
  if (parts.length < 3) return iso;
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const m = parseInt(parts[1], 10) - 1;
  const d = parseInt(parts[2], 10);
  if (m < 0 || m > 11 || isNaN(d)) return iso;
  return months[m] + ' ' + d + ', ' + parts[0];
}

function renderTravelTab(){
  const travel = DATA.travel;
  const loading = document.getElementById('travelLoading');
  const content = document.getElementById('travelContent');
  // Loading state — sidecar fetch in flight + no payload yet.
  if (!travel && state.tab === 'travel' && SIDECAR_STATE.travel === 'loading'){
    if (loading) loading.classList.remove('hidden');
    if (content) content.classList.add('hidden');
    return;
  }
  if (loading) loading.classList.add('hidden');
  if (content) content.classList.remove('hidden');
  if (!travel || !Array.isArray(travel.advisories)) {
    // Mirrors the metals empty-state idiom — render a friendly placeholder
    // into the bulletins host so the tab isn't blank.
    const bull = document.getElementById('travelBulletinsList');
    if (bull) bull.innerHTML = '<div class="empty" style="padding:24px 12px">Travel advisories sidecar not yet loaded.</div>';
    return;
  }
  const advisories = travel.advisories;
  const bulletins = Array.isArray(travel.bulletins) ? travel.bulletins : [];

  // Aggregate counts: per-level + terrorism cross-cut
  const counts = {1:0, 2:0, 3:0, 4:0, terror:0};
  for (const a of advisories) {
    if (counts[a.level] != null) counts[a.level]++;
    if (Array.isArray(a.risks) && a.risks.indexOf('T') !== -1) counts.terror++;
  }

  const sub = state.travelSub || 'overview';

  // Toggle sub-view visibility
  const overviewEl = document.getElementById('travelOverview');
  const listEl = document.getElementById('travelList');
  if (overviewEl) overviewEl.classList.toggle('hidden', sub !== 'overview');
  if (listEl) listEl.classList.toggle('hidden', sub === 'overview');

  // Sync the sub-tab strip's active state
  document.querySelectorAll('.travel-subtab').forEach(b => {
    const isActive = b.dataset.travelsub === sub;
    b.classList.toggle('active', isActive);
    b.setAttribute('aria-selected', isActive ? 'true' : 'false');
  });

  if (sub === 'overview') {
    renderTravelOverviewV1(advisories, bulletins, counts, travel.generated_at);
  } else {
    renderTravelListV1(advisories, sub, counts);
  }
}

function renderTravelOverviewV1(advisories, bulletins, counts, generatedAt){
  // Stat cards row — clicking L1/L2/L3/L4 navigates to that level sub-view;
  // Terrorism card opens the Terrorism sub-view.
  const statHost = document.getElementById('travelStatCards');
  if (statHost) {
    const cards = [1,2,3,4].map(l =>
      '<button class="travel-stat travel-stat--l' + l + '" data-travelstat="' + l + '" type="button">' +
        '<div class="travel-stat__num">' + counts[l] + '</div>' +
        '<div class="travel-stat__label">Level ' + l + '</div>' +
        '<div class="travel-stat__sub">' + escapeHtml(TRAVEL_LEVEL_SHORT[l]) + '</div>' +
      '</button>'
    ).join('') +
    '<button class="travel-stat travel-stat--terror" data-travelstat="terror" type="button">' +
      '<div class="travel-stat__num">' + counts.terror + '</div>' +
      '<div class="travel-stat__label">Terrorism</div>' +
      '<div class="travel-stat__sub">Flagged destinations &rarr;</div>' +
    '</button>';
    statHost.innerHTML = cards;
  }

  // Updated-at chip
  const asOfEl = document.getElementById('travelBulletinsAsOf');
  if (asOfEl) {
    asOfEl.textContent = generatedAt
      ? '● Updated ' + travelFmtDate(String(generatedAt).slice(0,10))
      : '● Updated —';
  }

  // Bulletins list — sort newest first
  const bullHost = document.getElementById('travelBulletinsList');
  if (bullHost) {
    const sorted = bulletins.slice().sort((a,b) => String(b.date).localeCompare(String(a.date)));
    if (sorted.length === 0) {
      bullHost.innerHTML = '<div class="empty" style="padding:18px 12px;font-size:12px">No bulletins available. The State Dept RSS feed returned nothing on the last fetch.</div>';
    } else {
      bullHost.innerHTML = sorted.map(b => {
        const sev = (b.severity === 'red' || b.severity === 'amber' || b.severity === 'green') ? b.severity : 'amber';
        const href = sanitizeUrl(b.href, '#');
        // Map severity → V1's tag color class. The .tag base class already
        // has the pill shape; the inline color is set via the stripe + the
        // tag's inline style so we don't have to fork new .tag.red variants.
        const tagColor = sev === 'red' ? 'var(--red)' : sev === 'amber' ? 'var(--amber)' : 'var(--green)';
        return '<a class="travel-bullet" href="' + escapeHtml(href) + '" target="_blank" rel="noreferrer">' +
                 '<div class="travel-bullet__stripe travel-bullet__stripe--' + sev + '"></div>' +
                 '<div class="travel-bullet__body">' +
                   '<div class="travel-bullet__top">' +
                     '<span class="tag" style="color:' + tagColor + ';border-color:' + tagColor + ';margin-left:0">' + escapeHtml(b.tag || '') + '</span>' +
                     '<span class="travel-bullet__date">' + escapeHtml(travelFmtDate(b.date)) + '</span>' +
                   '</div>' +
                   '<div class="travel-bullet__title">' + escapeHtml(b.title || '') + '</div>' +
                   (b.body ? '<div class="travel-bullet__excerpt">' + escapeHtml(b.body) + '</div>' : '') +
                 '</div>' +
                 '<span class="travel-bullet__arrow">&#8599;</span>' +
               '</a>';
      }).join('');
    }
  }

  // Distribution bars
  const distHost = document.getElementById('travelDistribution');
  if (distHost) {
    const max = Math.max(counts[1], counts[2], counts[3], counts[4], 1);
    distHost.innerHTML = [1,2,3,4].map(l =>
      '<div class="travel-bar-row">' +
        '<div class="travel-bar-label"><span class="travel-dot travel-dot--l' + l + '"></span>Lvl ' + l + '</div>' +
        '<div class="travel-bar-track"><div class="travel-bar-fill travel-bar-fill--l' + l + '" style="width:' + ((counts[l]/max)*100).toFixed(1) + '%"></div></div>' +
        '<div class="travel-bar-num">' + counts[l] + '</div>' +
      '</div>'
    ).join('');
    const total = counts[1]+counts[2]+counts[3]+counts[4];
    const distFoot = document.getElementById('travelDistributionFoot');
    if (distFoot) {
      const high = total > 0 ? Math.round(((counts[3]+counts[4])/total)*100) : 0;
      distFoot.textContent = total + ' destinations rated · ' + high + '% at Level 3 or higher';
    }
  }

  // Recent updates feed (8 most recent reissues)
  const recentHost = document.getElementById('travelRecentList');
  if (recentHost) {
    const recent = advisories.slice().sort((a,b) => String(b.date).localeCompare(String(a.date))).slice(0,8);
    recentHost.innerHTML = recent.map(d => {
      const href = sanitizeUrl(d.url, '#');
      return '<li>' +
               '<span class="travel-recent__dot travel-recent__dot--l' + d.level + '"></span>' +
               '<div style="min-width:0;flex:1">' +
                 '<a class="travel-recent__name" href="' + escapeHtml(href) + '" target="_blank" rel="noreferrer">' + escapeHtml(d.name) + '</a>' +
                 '<div class="travel-recent__meta">Level ' + d.level + ' · ' + escapeHtml(TRAVEL_LEVEL_SHORT[d.level] || '') + ' · updated ' + escapeHtml(travelFmtDate(d.date)) + '</div>' +
               '</div>' +
             '</li>';
    }).join('');
  }
}

function renderTravelListV1(advisories, sub, counts){
  // Determine level filter and whether the segmented L3/L4 control is shown
  let levelSet = null;
  let merged = false;
  let isTerror = false;
  if (sub === 'level1') levelSet = [1];
  else if (sub === 'level2') levelSet = [2];
  else if (sub === 'level34') {
    merged = true;
    levelSet = state.travelSubLevel === '3' ? [3]
             : state.travelSubLevel === '4' ? [4]
             : [3,4];
  } else if (sub === 'terror') {
    isTerror = true;
  }

  // Show/hide controls
  const segment = document.getElementById('travelLevelSegment');
  if (segment) segment.classList.toggle('hidden', !merged);
  const terrorToggle = document.getElementById('travelTerrorToggle');
  if (terrorToggle) {
    terrorToggle.classList.toggle('hidden', isTerror); // hidden on the dedicated Terrorism view
    terrorToggle.classList.toggle('active', !!state.travelTerrorOnly);
  }
  // Sync segment active state
  document.querySelectorAll('.travel-seg-btn').forEach(b => {
    b.classList.toggle('active', (b.dataset.travelsublevel || 'all') === (state.travelSubLevel || 'all'));
  });
  // Sync sort + search inputs to state (in case of programmatic resets)
  const sortEl = document.getElementById('travelSort');
  if (sortEl && sortEl.value !== state.travelSort) sortEl.value = state.travelSort;
  const searchEl = document.getElementById('travelSearch');
  if (searchEl && searchEl.value !== state.travelQuery) searchEl.value = state.travelQuery;

  // Apply filters
  const q = (state.travelQuery || '').toLowerCase();
  const rows = advisories.filter(d => {
    if (levelSet && levelSet.indexOf(d.level) === -1) return false;
    if ((state.travelTerrorOnly || isTerror) && !(Array.isArray(d.risks) && d.risks.indexOf('T') !== -1)) return false;
    if (q && !String(d.name || '').toLowerCase().includes(q)) return false;
    return true;
  });

  // Sort
  rows.sort((a,b) => {
    if (state.travelSort === 'name') return String(a.name).localeCompare(String(b.name));
    if (state.travelSort === 'date') return String(b.date).localeCompare(String(a.date));
    // default: level (high→low), then name
    return (b.level - a.level) || String(a.name).localeCompare(String(b.name));
  });

  // Count line
  const countEl = document.getElementById('travelCount');
  if (countEl) {
    let extra = '';
    if (isTerror) extra = ' · terrorism-flagged';
    else if (merged && state.travelSubLevel && state.travelSubLevel !== 'all') extra = ' · Level ' + state.travelSubLevel + ' only';
    else if (merged) extra = ' · L3 + L4';
    countEl.textContent = rows.length + ' shown' + extra;
  }

  // Grid + empty state
  const gridHost = document.getElementById('travelGrid');
  const emptyEl = document.getElementById('travelEmpty');
  if (rows.length === 0) {
    if (gridHost) gridHost.innerHTML = '';
    if (emptyEl) emptyEl.classList.remove('hidden');
    return;
  }
  if (emptyEl) emptyEl.classList.add('hidden');
  if (!gridHost) return;
  gridHost.innerHTML = rows.map(d => {
    const lv = d.level;
    const href = sanitizeUrl(d.url, '#');
    const risks = Array.isArray(d.risks) ? d.risks : [];
    const chips = risks.length === 0
      ? '<span class="travel-card__norisk">No specific risk indicators</span>'
      : risks.map(r => {
          const isT = (r === 'T');
          const cls = isT ? 'travel-chip travel-chip--terror' : 'travel-chip';
          return '<span class="' + cls + '" title="' + escapeHtml(TRAVEL_RISK_LABEL[r] || r) + '">' +
                   escapeHtml(TRAVEL_RISK_LABEL[r] || r) +
                 '</span>';
        }).join('');
    return '<a class="travel-card" href="' + escapeHtml(href) + '" target="_blank" rel="noreferrer">' +
             '<div class="travel-card__bar travel-card__bar--l' + lv + '"></div>' +
             '<div class="travel-card__body">' +
               '<div class="travel-card__top">' +
                 '<span class="travel-card__name">' + escapeHtml(d.name || '') + '</span>' +
                 '<span class="travel-card__badge travel-card__badge--l' + lv + '">L' + lv + '</span>' +
               '</div>' +
               '<div class="travel-card__lvl travel-card__lvl--l' + lv + '">' + escapeHtml(TRAVEL_LEVEL_SHORT[lv] || '') + '</div>' +
               '<div class="travel-card__chips">' + chips + '</div>' +
               '<div class="travel-card__date">Issued ' + escapeHtml(travelFmtDate(d.date)) + ' &rarr;</div>' +
             '</div>' +
           '</a>';
  }).join('');
}

// ─── TRAVEL — control wiring (sub-tabs, stat cards, filters) ─────────────
// Delegated handlers anchored to #tab-travel so events from other tabs
// (e.g. share modal close × buttons inside other cards) never reach these.
// Mirrors V2's wireTravelControls IIFE.
(function wireTravelControlsV1(){
  function bind(){
    const root = document.getElementById('tab-travel');
    if (!root) return;
    // Sub-tab strip + stat cards + L3/L4 segment + terrorism toggle
    root.addEventListener('click', e => {
      const subBtn = e.target.closest('.travel-subtab');
      if (subBtn) {
        const next = subBtn.dataset.travelsub;
        if (next && next !== state.travelSub) {
          state.travelSub = next;
          // Leaving level34 resets the L3/L4 segment so re-entry shows All.
          if (next !== 'level34') state.travelSubLevel = 'all';
          renderTravelTab();
        }
        return;
      }
      const stat = e.target.closest('.travel-stat');
      if (stat) {
        const v = stat.dataset.travelstat;
        if (v === '1') { state.travelSub = 'level1'; state.travelSubLevel = 'all'; }
        else if (v === '2') { state.travelSub = 'level2'; state.travelSubLevel = 'all'; }
        else if (v === '3') { state.travelSub = 'level34'; state.travelSubLevel = '3'; }
        else if (v === '4') { state.travelSub = 'level34'; state.travelSubLevel = '4'; }
        else if (v === 'terror') { state.travelSub = 'terror'; }
        renderTravelTab();
        return;
      }
      const seg = e.target.closest('.travel-seg-btn');
      if (seg) {
        state.travelSubLevel = seg.dataset.travelsublevel || 'all';
        renderTravelTab();
        return;
      }
      if (e.target.closest('#travelTerrorToggle')) {
        state.travelTerrorOnly = !state.travelTerrorOnly;
        renderTravelTab();
        return;
      }
    });
    // Search box + sort dropdown
    root.addEventListener('input', e => {
      if (e.target.id === 'travelSearch') {
        state.travelQuery = e.target.value || '';
        renderTravelTab();
      } else if (e.target.id === 'travelSort') {
        state.travelSort = e.target.value || 'level';
        renderTravelTab();
      }
    });
    root.addEventListener('change', e => {
      if (e.target.id === 'travelSort') {
        state.travelSort = e.target.value || 'level';
        renderTravelTab();
      }
    });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
})();

document.getElementById('generatedAt').textContent = 'generated ' + DATA.generated_at;
selectTab('overview');
renderAll();
</script>
</body>
</html>
"""



if __name__ == "__main__":
    sys.exit(main())
