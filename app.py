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
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
OUT = ROOT / "dashboard.html"


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
    payload = {
        "btc": aggregate(btc_df),
        "eth": aggregate(eth_df),
        "market": market,
        "whale": whale,
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
        import insights as ins_mod
        payload["insights"] = ins_mod.build_insights(payload, limit=12)
    except Exception as e:
        print(f"[insights] error: {e}", file=sys.stderr)
        payload["insights"] = []
    return payload


def render_html(payload: dict, share_token: str | None = None) -> str:
    html = HTML_TEMPLATE.replace("__DATA_JSON__", json.dumps(payload))
    html = html.replace("__SHARE_TOKEN__", json.dumps(share_token))
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

    print(f"Writing {OUT.name}...")
    OUT.write_text(render_html(payload))

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
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
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
.chart-wrap{position:relative;height:300px}
.chart-wrap.tall{height:380px}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:18px}
.grid3{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px}
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
@media (max-width:720px){
  #chatDock{width:100%}
}
/* Mobile: tight layout — collapse multi-col grids, shrink header, KPI rows
   become 2-up instead of 1-up, chart heights capped. Desktop unchanged. */
@media (max-width:860px){
  #overviewMacroRow{grid-template-columns:1fr !important}
  .grid2{grid-template-columns:1fr !important}
  .grid3{grid-template-columns:1fr !important}
  /* Asset signal cards: keep 2 per row on mobile instead of one big card,
     and shrink fonts so price/change/volume don't dominate the screen. */
  /* Asset cards (BTC/ETH/LINK/LTC) — ultra-compact on mobile so the user
     sees Strong Buys + news above the fold. Was ~110px tall each → ~55px.
     Hides redundant fields (full coin name, "as of" date) that already
     live in the header / tooltips. */
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
  /* Strong Buys + Top-50 strip cards: tighter on mobile too */
  #overviewStrongBuys{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:6px}
  #top20SignalCards{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:6px}

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
  /* POC compact card sub-text bumped 10→11px for readability on phone */
  #pocTopGrid .poc-card .sub{font-size:11px !important}
  /* Ensure clickable card divs hit 44px touch target */
  .poc-card,.stock-card{min-height:44px}
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

  /* --- GLOBAL CARD TIGHTENING (every tab, not just Overview) ---
     User reported all phone pages had boxes wasting too much space.
     Shrinks padding, fonts, and gaps across .card / .chart-card / .grid*
     so every section becomes ~40-50% shorter without losing data. */
  .container{padding:10px 12px;gap:10px}
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
}
.hidden{display:none !important}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:50;display:flex;align-items:center;justify-content:center;padding:24px}
.note{font-size:11px;color:var(--muted);background:#10151f;border:1px solid var(--border);padding:8px 12px;border-radius:8px}
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
    <form id="symbolSearchForm" style="margin:0;display:flex;gap:4px" onsubmit="return false">
      <input id="symbolSearchInput" type="text" placeholder="Symbol (BTC, NVDA…)" autocomplete="off"
             aria-label="Search any stock or crypto symbol"
             style="background:#0b0d12;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:5px 8px;font-size:12px;width:130px;outline:none">
      <button class="btn" id="symbolSearchBtn" type="submit" aria-label="Look up symbol">🔍</button>
    </form>
    <button class="btn" id="shareBtn" title="Mint a read-only share link (default 3-day expiry)">🔗 Share</button>
    <button class="btn" id="refreshBtn" title="Re-fetch market + whale data (server only)">↻ Refresh</button>
  </div>
</header>

<div class="tabs" role="tablist">
  <div class="tab active" data-tab="overview" role="tab" tabindex="0" aria-selected="true">Overview</div>
  <div class="tab" data-tab="signals" role="tab" tabindex="0" aria-selected="false">Crypto Signals</div>
  <div class="tab" data-tab="whale" role="tab" tabindex="0" aria-selected="false">Whale Activity</div>
  <div class="tab" data-tab="stocks" role="tab" tabindex="0" aria-selected="false">Stocks</div>
  <div class="tab" data-tab="poc" role="tab" tabindex="0" aria-selected="false">Point of Control</div>
  <div class="tab" data-tab="social" role="tab" tabindex="0" aria-selected="false">Research</div>
  <div class="tab" data-tab="defi" role="tab" tabindex="0" aria-selected="false">DeFi</div>
  <div class="tab" data-tab="etf" role="tab" tabindex="0" aria-selected="false">ETF Flows</div>
  <div class="tab" data-tab="trading" role="tab" tabindex="0" aria-selected="false">Futures</div>
  <div class="tab" data-tab="ainews" role="tab" tabindex="0" aria-selected="false">AI News</div>
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
    <div class="sub">Pick which assets appear as signal cards on the Overview. Selection persists in your browser.</div>
    <div id="configSignalsList" style="display:flex;flex-direction:column;gap:8px;padding:6px 0"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;border-top:1px solid var(--border);padding-top:10px">
      <button class="btn" id="configSignalsReset">Reset to default</button>
      <button class="btn active" id="configSignalsSave">Save</button>
    </div>
    <div id="configSignalsStatus" class="sub" style="color:var(--muted);min-height:14px"></div>
  </div>
</div>

<!-- ============ SIGNAL DETAIL MODAL (top-20 strip → full breakdown) ============ -->
<div id="signalDetailModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;width:min(720px,100%);max-height:90vh;display:flex;flex-direction:column;gap:8px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 id="signalDetailTitle" style="margin:0;font-size:14px">Signal detail</h2>
      <button class="btn" id="signalDetailClose" aria-label="Close signal detail">×</button>
    </div>
    <div id="signalDetailBody"></div>
  </div>
</div>

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

<!-- ============ SYMBOL DETAIL MODAL (universal header search → consolidated view) ============ -->
<div id="symbolDetailModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:16px;width:min(820px,100%);max-height:92vh;display:flex;flex-direction:column;gap:12px;overflow:auto">
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
    <!-- Traditional indices — compact bar above the asset cards. Dow/SPX/
         NDX/VIX 1d/5d/30d + 90d sparkline per index. Moved here from the
         Markets tab so it's visible on the landing page as macro context. -->
    <div id="overviewIndicesWrap" class="card" style="padding:12px 16px;margin-bottom:6px">
      <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:8px">
        <span style="font-size:12px;font-weight:700;color:var(--muted);letter-spacing:.06em">TRADITIONAL INDICES</span>
        <span class="sub" style="font-size:11px;color:var(--muted)">Yahoo · 1d / 5d / 30d</span>
      </div>
      <div id="overviewIndices" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:10px"></div>
    </div>
    <!-- Row 1: Signal cards (HERO — clickable) -->
    <div style="display:flex;justify-content:flex-end;margin-bottom:-6px">
      <button class="btn" id="configSignalsBtn" style="font-size:11px;padding:3px 8px" title="Pick which assets show signal cards">⚙️ Configure</button>
    </div>
    <div class="row" id="overviewSignals" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))"></div>

    <!-- Strong Buys: up to 5 STRONG BUY signals from the top-50 strip.
         Hidden when none exist. Cards click through to the signal detail
         modal (same one the Signals-tab strip uses). -->
    <div id="overviewStrongBuysWrap" class="chart-card hidden" style="padding:12px 16px;margin-top:6px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">🚀 Strong Buys <span class="tag">Top 50</span></h2>
        <span class="desc">Up to 5 strongest signals from the top-50 by market cap · click any card for the full breakdown</span>
      </div>
      <div class="row" id="overviewStrongBuys" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr))"></div>
    </div>

    <!-- Top 15 by market cap: structural "what's the market doing" view.
         Re-sorts signals_top20 by CoinGecko market-cap rank (not by score)
         so the largest 15 coins are always visible, regardless of bull/bear.
         Each card shows symbol + price + label + score, click → full modal. -->
    <div id="overviewTop15Wrap" class="chart-card hidden" style="padding:12px 16px;margin-top:6px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">🏆 Top 15 by market cap</h2>
        <span class="desc">Largest 15 coins · price + signal · click any card for the full breakdown</span>
      </div>
      <div class="row" id="overviewTop15" style="grid-template-columns:repeat(auto-fit,minmax(180px,1fr))"></div>
    </div>

    <!-- Row 2: top news + top insights -->
    <div class="grid2">
      <div class="chart-card" style="cursor:pointer" data-jump="trading" title="See full news feed in Trading tab">
        <div class="head"><h2>Latest crypto news</h2><span class="desc">Top 4 · click for full feed</span></div>
        <div id="overviewNews"></div>
      </div>
      <div class="chart-card">
        <div class="head"><h2>Top insights</h2><span class="desc">Most-relevant 4 right now</span></div>
        <div id="overviewInsights" style="display:flex;flex-direction:column;gap:8px;padding:2px"></div>
      </div>
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

    <!-- Bottom-of-Overview: breaking news feed (top 10 most recent) -->
    <div class="chart-card">
      <div class="head">
        <h2>Breaking news</h2>
        <span class="desc">latest crypto headlines</span>
      </div>
      <div id="overviewNewsHost"></div>
    </div>
  </div>

  <!-- ============ ETF FLOWS TAB ============ -->
  <div id="tab-etf" class="hidden">
    <div class="card" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 14px">
      <span class="lbl" style="margin:0">Load data</span>
      <button class="btn" id="loadBtcBtn" title="Paste BTC ETF flow CSV from Farside">Paste BTC</button>
      <button class="btn" id="loadEthBtn" title="Paste ETH ETF flow CSV from Farside">Paste ETH</button>
      <button class="btn" id="seedBtcBtn" title="Pull BTC from canadiancode/btc-etf-flows GitHub mirror (may be stale)">Seed BTC (mirror)</button>
      <a class="btn" href="/bookmarklet" target="_blank" style="text-decoration:none" title="One-click bookmarklet for Farside pages">Get bookmarklet</a>
      <span id="loadStatus" class="sub" style="margin-left:8px;color:var(--muted)"></span>
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
      <div class="card" style="padding:14px 16px;margin-bottom:10px;border-left:3px solid var(--btc)">
        <h2 style="margin:0 0 6px;font-size:14px">Futures &amp; perpetuals dashboard</h2>
        <p class="sub" style="font-size:12px;line-height:1.5;color:var(--muted);margin:0">
          Derivatives positioning for BTC, ETH, LINK, LTC. <strong style="color:var(--text)">Funding rate</strong> shows perp traders paying to hold longs (positive) or shorts (negative); extremes signal crowded positioning. <strong style="color:var(--text)">Open interest</strong> is total notional in active perp contracts. <strong style="color:var(--text)">Long/short ratio</strong> from OKX shows top-account positioning bias. <strong style="color:var(--text)">DVOL</strong> is Deribit's BTC/ETH implied-volatility index. The two tables list Coinbase International Exchange perps with the most extreme positive (crowded longs) and negative (crowded shorts) funding rates.
        </p>
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

  <!-- ============ AI NEWS TAB ============ -->
  <div id="tab-ainews" class="hidden">
    <div class="container">
      <div id="aiNewsEmpty" class="empty hidden">AI news not yet loaded. Run <code>python app.py --fetch-market</code> to populate.</div>
      <div id="aiNewsContent">
        <!-- Top: AI sentiment summary card -->
        <div class="chart-card" id="aiNewsSummaryCard">
          <div class="head">
            <h2>AI news sentiment <span class="tag">live</span></h2>
            <span class="desc">Aggregate sentiment across AI/ML/chips coverage &middot; auto-classified POSITIVE / NEUTRAL / NEGATIVE</span>
          </div>
          <div id="aiNewsSummary"></div>
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

        <!-- Quadrant scatter chart -->
        <div class="chart-card" id="aiQuadrantCard" style="margin-top:12px">
          <div class="head">
            <h2>AI funding quadrant <span class="tag">last round &times; valuation</span></h2>
            <span class="desc">X = last round size &middot; Y = total valuation &middot; log scale both axes &middot; each dot is a company (hover for name)</span>
          </div>
          <div class="chart-wrap" style="height:380px"><canvas id="aiQuadrantChart"></canvas></div>
        </div>

        <!-- White paper / research KPIs -->
        <div class="chart-card" id="aiWhitepaperKpisCard" style="margin-top:12px">
          <div class="head">
            <h2>Research benchmarks <span class="tag">Stanford AI Index &middot; Epoch &middot; IEA &middot; MLPerf</span></h2>
            <span class="desc">From peer-reviewed and major institutional reports &middot; click any card for source</span>
          </div>
          <div class="row" id="aiWhitepaperKpis" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px"></div>
        </div>

        <div class="grid2" style="margin-top:12px">
          <!-- Middle: AI news feed -->
          <div class="chart-card">
            <div class="head">
              <h2>Latest AI news <span class="tag" id="aiNewsHeaderBadge"></span></h2>
              <span class="desc">Top 30 most recent &middot; click any row to open the article</span>
            </div>
            <div id="aiNewsFeed" style="max-height:640px;overflow-y:auto;border:1px solid var(--border);border-radius:6px"></div>
          </div>
          <!-- Bottom right: AI-exposed stock signal cards -->
          <div class="chart-card">
            <div class="head">
              <h2>AI-exposed stocks</h2>
              <span class="desc">Signal score for tickers most exposed to AI/ML/chips &middot; filtered from Stocks tab</span>
            </div>
            <div id="aiStocksGrid" class="row" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px"></div>
          </div>
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
          <div>
            <h2 style="margin:0;font-size:15px">Top 50 by market cap</h2>
            <div class="sub" style="color:var(--muted);font-size:11px">Simplified score from CoinGecko price/volume only · click any card for the full breakdown</div>
          </div>
          <span style="flex:1"></span>
          <span class="lbl" style="margin:0">Filter</span>
          <button class="btn active" data-top20filter="all">All</button>
          <button class="btn" data-top20filter="buy">Buy</button>
          <button class="btn" data-top20filter="hold">Hold</button>
          <button class="btn" data-top20filter="sell">Sell</button>
        </div>
        <div id="top20SignalCards" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px"></div>
      </div>

      <div class="note"><strong>Composite indicator, not investment advice.</strong> Score is a transparent sum of contributions from price trend (SMA50/200), momentum (RSI, MACD), positioning (funding), sentiment (Fear &amp; Greed), institutional flows (ETF 7d), and volatility (DVOL z-score). Range −100 to +100. Read the components below — that's where the score comes from. Do your own evaluation.</div>
      <div class="grid3" id="signalCards"></div>
      <div class="grid3">
        <div class="chart-card" data-sig-asset="BTC" style="cursor:pointer" title="Click to open BTC signal detail">
          <div class="head"><h2>BTC signal history (90d)</h2><span class="desc">Score &middot; price overlay · click for full breakdown</span></div>
          <div class="chart-wrap"><canvas id="sigBtcChart"></canvas></div>
        </div>
        <div class="chart-card" data-sig-asset="ETH" style="cursor:pointer" title="Click to open ETH signal detail">
          <div class="head"><h2>ETH signal history (90d)</h2><span class="desc">Score &middot; price overlay · click for full breakdown</span></div>
          <div class="chart-wrap"><canvas id="sigEthChart"></canvas></div>
        </div>
        <div class="chart-card" data-sig-asset="LINK" style="cursor:pointer" title="Click to open LINK signal detail">
          <div class="head"><h2>LINK signal history (90d)</h2><span class="desc">Score &middot; price overlay · click for full breakdown</span></div>
          <div class="chart-wrap"><canvas id="sigLinkChart"></canvas></div>
        </div>
        <div class="chart-card" data-sig-asset="LTC" style="cursor:pointer" title="Click to open LTC signal detail">
          <div class="head"><h2>LTC signal history (90d)</h2><span class="desc">Score &middot; price overlay · click for full breakdown</span></div>
          <div class="chart-wrap"><canvas id="sigLtcChart"></canvas></div>
        </div>
      </div>
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
        <div class="note" style="margin:6px 0 10px;padding:8px 10px;border:1px solid var(--border);border-radius:6px;background:#0e1620;color:var(--text);font-size:11px;line-height:1.5">
          <strong>How to read this page.</strong>
          Each card shows one coin's 90d Point of Control — the price where the most volume has traded.
          The big arrow on the right tells you which way value is migrating:
          <span style="color:#22c55e;font-weight:700">↑ UP</span> (POC drifting higher · accumulation)
          ·
          <span style="color:#ef4444;font-weight:700">↓ DOWN</span> (POC drifting lower · distribution)
          ·
          <span style="color:var(--muted);font-weight:700">· FLAT</span> (value stable).
          Distance % shows where current price sits relative to that POC.
          Click any card for the full value-area ladder, naked POCs, and drift detail.
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
        <div id="pocTopGrid" class="row" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px"></div>
      </div>
    </div>
  </div>

  <!-- ============ DeFi TAB ============ -->
  <div id="tab-defi" class="hidden">
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
  </div>

  <!-- ============ RESEARCH TAB (one-stop consolidated info page) ============ -->
  <div id="tab-social" class="hidden">
    <!-- ===== CryptoCompare news sentiment — pinned to TOP per user request,
         BTC card renders first (RESEARCH_ASSETS = [btc,eth,link,ltc]) ===== -->
    <div class="chart-card" style="padding:12px 16px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">News sentiment by coin <span class="tag">CryptoCompare</span></h2>
        <span class="desc">POSITIVE / NEGATIVE / NEUTRAL split from CryptoCompare's keyless news API · 50 most recent articles per coin · 7d trend bars + clickable keyword chips</span>
      </div>
      <div class="row" id="ccNewsCards" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))"></div>
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
        <!-- Glassnode-powered KPIs — visible only if GLASSNODE_API_KEY is set
             and the metric returned 200. Otherwise stays empty. -->
        <div id="glassnodeStrip" class="hidden" style="margin-top:12px;padding-top:10px;border-top:1px dashed var(--border)">
          <div class="sub" style="margin-bottom:8px;color:var(--muted)">
            🔓 <strong>Glassnode</strong> — true cohort metrics (replaces the bitinfocharts proxy when active)
          </div>
          <div class="row" id="glassnodeKpis"></div>
        </div>
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
        <div class="row" id="whaleEthKpis"></div>
        <div class="chart-card">
          <div class="head">
            <h2>Largest ETH transaction (last 24h) <span class="tag">Blockchair</span></h2>
            <span class="desc">Single biggest tx by USD value on Ethereum mainnet over the past 24 hours</span>
          </div>
          <div id="ethLargestTxBox" class="sub" style="font-size:13px;color:var(--text);line-height:1.6;padding:6px 4px"></div>
        </div>
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
      </div> <!-- /whaleEthPanel -->
    </div>
  </div>
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
const DATA = __DATA_JSON__;
const SHARE_TOKEN = __SHARE_TOKEN__;  // string when viewing via /share/<token>, else null
const IS_SHARE = !!SHARE_TOKEN;

// In share mode, transparently append ?share=<token> to all /api/* fetches so
// the read-only allowlist on the server lets the call through without prompting
// for HTTP Basic Auth.
if (IS_SHARE) {
  const _origFetch = window.fetch.bind(window);
  window.fetch = function(input, init){
    try {
      if (typeof input === 'string' && input.startsWith('/api/')) {
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
  // Per-tab chain selector for the DeFi tab. One of Ethereum / Solana /
  // Arbitrum / Base (the 4 chains we have tvl_history for). Default Ethereum.
  defiChain: (function(){
    if (typeof localStorage === 'undefined') return 'Ethereum';
    const v = localStorage.getItem('defiChain');
    return ['Ethereum','Solana','Arbitrum','Base'].includes(v) ? v : 'Ethereum';
  })() };

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
function etfData(){ return DATA[state.asset] || {}; }

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
    host.innerHTML = `<div class="empty" style="grid-column:1/-1">No per-fund data loaded. Use <b>Paste ${state.asset.toUpperCase()}</b> with the full Farside table to populate fund-level views.</div>`;
    return;
  }
  const winKey = fundWindowKey();
  const winLabel = fundWindowLabel();
  // sort by the selected window (desc)
  const sorted = funds.slice().sort((a,b) => (b[winKey]||0) - (a[winKey]||0));
  host.innerHTML = sorted.map((f, i) => {
    const c = (state.asset==='eth' ? '#627eea' : state.asset==='link' ? '#2a5ada' : '#f7931a');
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
  const d = etfData(); const series = applyRange(d[state.period]); const c = accentFor(state.asset);
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

function renderSignalChart(canvasId, asset){
  const s = (DATA.signals||{})[asset];
  destroy('sig_'+asset);
  if (!s) return;
  const labels = s.history.map(r=>r.date);
  const scores = s.history.map(r=>r.score);
  const prices = s.history.map(r=>r.price);
  const accent = accentFor(asset);
  charts['sig_'+asset] = new Chart(document.getElementById(canvasId), {
    type:'line',
    data:{labels, datasets:[
      {label:'Score', data:scores, borderColor:'#a78bfa', backgroundColor:'#a78bfa22', fill:true, tension:0.2, pointRadius:0, borderWidth:2, yAxisID:'yScore'},
      {label:'Price', data:prices, borderColor:accent, backgroundColor:'transparent', tension:0.2, pointRadius:0, borderWidth:1.5, yAxisID:'yPrice'},
    ]},
    options:{
      responsive:true, maintainAspectRatio:false,
      plugins:{legend:{labels:{color:'#e6e8ee'}}, tooltip:{mode:'index',intersect:false}},
      scales:{
        x:{ticks:{color:'#8a93a6',maxTicksLimit:10},grid:{color:'#1f2533'}},
        yScore:{position:'left',min:-100,max:100,title:{display:true,text:'Score',color:'#8a93a6'},ticks:{color:'#8a93a6'},grid:{color:'#1f2533'}},
        yPrice:{position:'right',title:{display:true,text:'Price',color:'#8a93a6'},ticks:{color:'#8a93a6',callback:v=>fmtUSD(v,'auto')},grid:{display:false}},
      },
    },
  });
}

// Render the top-of-tab breadth chart for the Crypto Signals tab. Sources
// signals_top20 (each entry carries `history: [{date,score}, ...]`). Safe to
// call when DATA isn't loaded — the helper renders an empty-state message.
function renderCryptoSignalsBreadth(){
  const items = Array.isArray(DATA.signals_top20) ? DATA.signals_top20 : [];
  renderBreadthChart(
    'cryptoSignalsBreadthChart',
    computeSignalBreadth(items, 90),
    null
  );
}

function renderSignals(){
  const sigData = DATA.signals || {};
  const top20  = DATA.signals_top20 || [];
  const empty = !sigData.btc && !sigData.eth && !sigData.link && !sigData.ltc && !top20.length;
  document.getElementById('signalsEmpty').classList.toggle('hidden', !empty);
  document.getElementById('signalsContent').classList.toggle('hidden', empty);
  if (empty) return;
  // Breadth chart at the top of the tab (first visible widget).
  renderCryptoSignalsBreadth();
  // Sort cards descending by score so the strongest signals appear first.
  const sortedAssets = Object.entries(sigData)
    .filter(([k, v]) => v && typeof v.score === 'number')
    .sort((a, b) => (b[1].score || 0) - (a[1].score || 0))
    .map(([k]) => k);
  document.getElementById('signalCards').innerHTML =
    sortedAssets.map(a => renderSignalCard(a)).join('');
  renderSignalChart('sigBtcChart','btc');
  renderSignalChart('sigEthChart','eth');
  renderSignalChart('sigLinkChart','link');
  renderSignalChart('sigLtcChart','ltc');
  renderTop20Signals();
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
  const all = (DATA.signals_top20 || [])
    .filter(s => s && !isStable(s.symbol))
    .slice().sort((a,b) => (b.score||0) - (a.score||0));
  if (!all.length){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:8px">No top-20 signals yet — refresh.</div>';
    return;
  }
  window._top20SignalsCache = {};
  host.innerHTML = all.map(s => {
    const sym = (s.symbol||'').toUpperCase();
    const color = signalColor(s.score);
    const bucket = labelBucket(s.label);
    const img = sanitizeUrl(s.image, '')
      ? `<img src="${sanitizeUrl(s.image, '')}" alt="" style="width:32px;height:32px;border-radius:50%">`
      : `<div style="width:32px;height:32px;border-radius:50%;background:${color}33"></div>`;
    const score = (s.score>=0?'+':'') + s.score;
    window._top20SignalsCache[sym] = s;
    // Price formatting — auto-scales decimal places for small-cap coins
    const priceStr = (s.price != null)
      ? '$' + Number(s.price).toLocaleString(undefined, {maximumFractionDigits: s.price>=1000?0:s.price>=1?2:6})
      : '';
    return `<div class="card" data-symbol="${sym}" data-bucket="${bucket}" style="cursor:pointer;padding:8px 10px;display:flex;align-items:center;gap:10px;min-height:80px;max-height:100px;border-left:3px solid ${color};transition:transform .08s ease,background .08s ease">
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
  }).join('');
  // Bind click → modal
  host.querySelectorAll('[data-symbol]').forEach(el =>
    el.addEventListener('click', () => openSignalDetail(el.getAttribute('data-symbol')))
  );
}

function openSignalDetail(sym){
  // Look up the signal: first the top-50 strip cache, then fall back to
  // the main 4 signals (so the history-chart click handlers work too).
  const upper = (sym||'').toUpperCase();
  const lower = upper.toLowerCase();
  let s = (window._top20SignalsCache || {})[upper];
  if (!s){
    const main = (DATA.signals || {})[lower];
    if (main){
      // Reshape main-signal output to match renderSignalCardFromObj's expected shape
      s = {...main, symbol: upper, name: main.name || upper, image: null};
    }
  }
  const modal = document.getElementById('signalDetailModal');
  if (!modal || !s) return;
  document.getElementById('signalDetailTitle').textContent = `${upper} · ${s.name||''} · signal detail`;
  document.getElementById('signalDetailBody').innerHTML = renderSignalCardFromObj(s);
  modal.classList.remove('hidden');
}
function closeSignalDetail(){
  const m = document.getElementById('signalDetailModal');
  if (m) m.classList.add('hidden');
}

// One-time wiring for the detail modal + filter chips + POC explainer. Idempotent.
(function wireTop20Modals(){
  if (window._top20Wired) return; window._top20Wired = true;
  document.addEventListener('click', e => {
    if (e.target && e.target.id === 'signalDetailClose') closeSignalDetail();
    if (e.target && e.target.id === 'signalDetailModal') closeSignalDetail();
    const fb = e.target && e.target.closest && e.target.closest('[data-top20filter]');
    if (fb){
      const bucket = fb.getAttribute('data-top20filter');
      fb.parentElement.querySelectorAll('[data-top20filter]').forEach(b => b.classList.toggle('active', b===fb));
      document.querySelectorAll('#top20SignalCards [data-symbol]').forEach(c => {
        c.style.display = (bucket==='all' || c.getAttribute('data-bucket')===bucket) ? '' : 'none';
      });
    }
    // Click on any signal history chart-card → open the detail modal for
    // that asset. Uses the generalized openSignalDetail which falls back
    // to DATA.signals when the symbol isn't in the top-50 cache.
    const sigChart = e.target && e.target.closest && e.target.closest('[data-sig-asset]');
    if (sigChart) openSignalDetail(sigChart.getAttribute('data-sig-asset'));
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') {
      closeSignalDetail();
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
  renderGlassnodeStrip();
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
function renderWhaleEth(){
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
  const kpis = [
    {label:'Active addresses (24h)', val: aa  != null ? fmtNum(aa, 0)                   : '—'},
    {label:'Transactions (24h)',     val: txc != null ? fmtNum(txc, 0)                  : '—'},
    {label:'24h trading volume',     val: txv != null ? fmtUSD(txv, 'auto')             : '—'},
    {label:'Supply (ETH)',           val: sup != null ? fmtNum(sup/1e6, 2) + 'M'        : '—'},
  ];
  const kpiHost = document.getElementById('whaleEthKpis');
  if (kpiHost) kpiHost.innerHTML = kpis.map(i =>
    `<div class="card"><h3>${i.label}</h3><div class="v">${i.val}</div></div>`
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

  // Recent ETH whale transactions feed (≥ $1M, 24h) — sits right below the
  // single-largest card to give users both the headline and the full feed.
  renderEthWhaleAlerts();

  // 180-day charts from Coin Metrics
  const slice180 = (arr) => (arr || []).slice(-180);
  lineChart('ethActiveAddrChart', 'ethActiveAddr', slice180(cm.AdrActCnt),      '#06b6d4', v=>fmtNum(v,0));
  lineChart('ethTxVolChart',      'ethTxVol',      slice180(ethMarketVol),      '#22c55e', v=>fmtUSD(v,'auto'));
  lineChart('ethTxCountChart',    'ethTxCount',    slice180(cm.TxCnt),          '#a78bfa', v=>fmtNum(v,0));
  lineChart('ethSupplyChart',     'ethSupply',     slice180(cm.SplyCur),        '#627eea', v=>fmtNum(v/1e6,2)+'M');

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
      const avgFee = bc.avg_tx_fee_eth_24h;
      const mp    = bc.market_price_usd;
      const burn  = bc.burned_eth_24h;
      const erc20 = bc.erc20_transactions_24h;
      const erc721= bc.erc721_transactions_24h;
      const inflation = bc.inflation_eth_24h;
      // Deflationary if burn > inflation in the 24h window. Post-Merge this
      // flips between deflationary and mildly inflationary block-to-block.
      const netSupplyDelta = (burn != null && inflation != null) ? (inflation - burn) : null;
      const netCls = netSupplyDelta == null ? '' : (netSupplyDelta < 0 ? 'green' : 'red');
      const netLbl = netSupplyDelta == null ? '—' : (netSupplyDelta < 0 ? '⤓ deflationary' : '⤒ inflationary');
      statsBox.innerHTML = `
        <div>Blocks (24h): <strong style="color:var(--text)">${fmtNum(bc.blocks_24h||0, 0)}</strong></div>
        <div>Txs (24h): <strong style="color:var(--text)">${fmtNum(bc.transactions_24h||0, 0)}</strong></div>
        ${avgFee != null ? `<div>Avg tx fee: <strong style="color:var(--text)">${avgFee.toFixed(6)} ETH</strong>${mp ? ` (~$${(avgFee*mp).toFixed(2)})` : ''}</div>` : ''}
        ${burn != null ? `<div>EIP-1559 burn (24h): <strong class="${netCls}">${burn.toFixed(2)} ETH</strong>${mp ? ` (~${fmtUSD(burn*mp,'auto')})` : ''} <span style="color:var(--muted)">· ${netLbl}</span></div>` : ''}
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

// Glassnode KPIs: only render when the user has set GLASSNODE_API_KEY and
// at least one metric came back 200. Otherwise the whole strip stays hidden.
function renderGlassnodeStrip(){
  const strip = document.getElementById('glassnodeStrip');
  const host  = document.getElementById('glassnodeKpis');
  if (!strip || !host) return;
  const gn = ((DATA.whale||{}).glassnode || {});
  const series = gn.series || {};
  if (!gn.available) {
    strip.classList.add('hidden');
    return;
  }
  strip.classList.remove('hidden');
  // Helper: latest value + 7d % change for a series
  const latest = (s) => {
    const arr = series[s] || [];
    if (!arr.length) return null;
    const last = arr[arr.length-1]?.value;
    if (last == null) return null;
    const back = arr.length > 7 ? arr[arr.length-1-7]?.value : null;
    const ch = (back != null && back !== 0) ? ((last - back) / Math.abs(back) * 100) : null;
    return {last, ch};
  };
  const items = [];
  const w1k  = latest('addresses/min_1k_count');
  const w10k = latest('addresses/min_10k_count');
  const txv  = latest('transactions/transfers_volume_sum');
  const txEx = latest('transactions/transfers_to_exchanges_sum');
  const txFx = latest('transactions/transfers_from_exchanges_sum');
  const prof = latest('supply/profit_relative');
  if (w1k)  items.push({label:'Whale addresses (≥1K BTC)',  val:fmtNum(w1k.last, 0), sub:`7d ${w1k.ch == null ? '—' : (w1k.ch>=0?'+':'')+w1k.ch.toFixed(2)+'%'}`, cls: w1k.ch == null ? '' : (w1k.ch>=0?'green':'red')});
  if (w10k) items.push({label:'Mega-whale addresses (≥10K)', val:fmtNum(w10k.last, 0), sub:`7d ${w10k.ch == null ? '—' : (w10k.ch>=0?'+':'')+w10k.ch.toFixed(2)+'%'}`, cls: w10k.ch == null ? '' : (w10k.ch>=0?'green':'red')});
  if (txv)  items.push({label:'Transfer volume (BTC)',      val:fmtNum(txv.last, 0) + ' BTC', sub:`7d ${txv.ch == null ? '—' : (txv.ch>=0?'+':'')+txv.ch.toFixed(2)+'%'}`, cls: txv.ch == null ? '' : (txv.ch>=0?'green':'red')});
  if (txEx) items.push({label:'Exchange inflow (BTC)',      val:fmtNum(txEx.last, 0) + ' BTC', sub:`7d ${txEx.ch == null ? '—' : (txEx.ch>=0?'+':'')+txEx.ch.toFixed(2)+'%'}`, cls: txEx.ch == null ? '' : (txEx.ch>=0?'red':'green')});
  if (txFx) items.push({label:'Exchange outflow (BTC)',     val:fmtNum(txFx.last, 0) + ' BTC', sub:`7d ${txFx.ch == null ? '—' : (txFx.ch>=0?'+':'')+txFx.ch.toFixed(2)+'%'}`, cls: txFx.ch == null ? '' : (txFx.ch>=0?'green':'red')});
  if (prof) items.push({label:'Supply in profit',           val:(prof.last*100).toFixed(1)+'%', sub:`7d ${prof.ch == null ? '—' : (prof.ch>=0?'+':'')+prof.ch.toFixed(2)+'pp'}`, cls: prof.ch == null ? '' : (prof.ch>=0?'green':'red')});
  if (!items.length) {
    host.innerHTML = '<div class="sub" style="color:var(--muted)">Key valid but no metrics returned data — check Glassnode tier.</div>';
    return;
  }
  host.innerHTML = items.map(i =>
    `<div class="card"><h3>${i.label}</h3><div class="v ${i.cls||''}">${i.val}</div><div class="sub">${i.sub}</div></div>`
  ).join('');
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
  });
}

function renderCoverage(){
  const cov = document.getElementById('coverage');
  if (state.tab === 'etf'){
    const d = etfData();
    if (d.daily && d.daily.length){
      const f = d.daily[0].date, l = d.daily[d.daily.length-1].date;
      const days = Math.round((new Date(l)-new Date(f))/86400000);
      cov.textContent = `ETF ${state.asset.toUpperCase()} ${f} → ${l} (${days}d, ${d.daily.length} obs)`;
    } else cov.textContent = `ETF ${state.asset.toUpperCase()}: no data`;
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
  const headerStrong = host?.parentElement?.querySelector('strong');
  if (headerStrong && tab !== 'overview') headerStrong.textContent = label;
  if (!list.length){
    const empty = TAB_EMPTY[tab] || 'Nothing unusual right now. Load more data or wait for the next refresh.';
    host.innerHTML = '<div class="sub" style="color:var(--muted)">' + empty + '</div>';
    return;
  }
  host.innerHTML = list.map(i => {
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
  }).join('');
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
  const defi = (DATA.market || {}).defi || {};
  const llama = (DATA.market || {}).defillama || {};
  const chains = defi.chains || [];
  const protocols = defi.protocols || [];
  const yields = defi.yields_stablecoin || [];
  const bridges = ((defi.bridges || {}).top_bridges) || [];

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
  const defi = (DATA.market || {}).defi || {};
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
  renderOverviewIndices();        // top bar — moved from deleted Markets tab
  renderOverviewSignals();
  renderCoinbaseSpot();           // compact Coinbase exchange bid/ask + 24h range
  renderOverviewStrongBuys();
  renderOverviewTop15();
  renderOverviewMacro();
  renderOverviewNews();           // top 4-item teaser + bottom 10-item feed
  renderOverviewInsights();
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

// Top 15 coins by market-cap rank from the top-50 signal computation.
// Different from Strong Buys (which filters by STRONG BUY label) — this
// shows the structural "core" of the market regardless of signal direction.
// Cards click through to the same detail modal as the Signals-tab strip.
function renderOverviewTop15(){
  const wrap = document.getElementById('overviewTop15Wrap');
  const host = document.getElementById('overviewTop15');
  if (!wrap || !host) return;
  const isStable = s => { const u=(s||'').toUpperCase(); return /^USD/.test(u) || /USD$/.test(u) || u==='DAI'; };
  // signals_top20 is sorted by SCORE — re-sort by rank for this widget.
  const top15 = (DATA.signals_top20 || [])
    .filter(s => s && !isStable(s.symbol))
    .slice()
    .sort((a,b) => (a.rank ?? 999) - (b.rank ?? 999))
    .slice(0, 15);
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
    return `<div class="card" data-symbol="${sym}" style="cursor:pointer;padding:8px 10px;display:flex;align-items:center;gap:9px;min-height:72px;border-left:3px solid ${color}">
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

// Up to 5 STRONG BUY signals pulled from the top-50 strip, surfaced
// prominently on the Crypto Overview before the news row. Hides the
// whole section when zero strong buys exist. Cards click through to
// the same detail modal the Signals-tab strip uses (cache is shared).
function renderOverviewStrongBuys(){
  const wrap = document.getElementById('overviewStrongBuysWrap');
  const host = document.getElementById('overviewStrongBuys');
  if (!wrap || !host) return;
  const isStable = s => { const u=(s||'').toUpperCase(); return /^USD/.test(u) || /USD$/.test(u) || u==='DAI'; };
  const strongs = (DATA.signals_top20 || [])
    .filter(s => s && !isStable(s.symbol) && (s.label || '').toUpperCase() === 'STRONG BUY')
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
    return `<div class="card" data-symbol="${sym}" style="cursor:pointer;padding:8px 10px;display:flex;align-items:center;gap:9px;min-height:72px;border-left:3px solid ${color}">
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
  // % change, and 24h volume for each configured asset. Click jumps
  // to the Trading tab for that asset.
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
    return `<div class="card" style="cursor:pointer;border-left:4px solid ${accent(a)}" data-jump="trading" data-asset="${a}" title="Open Trading tab for ${a.toUpperCase()}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">${a.toUpperCase()}</h3>
        <span class="sub" style="color:var(--muted);font-size:11px">${ASSET_NAMES[a] || a}</span>
      </div>
      <div class="v" style="font-size:26px;font-weight:700;margin-top:6px;color:var(--text)">${fmtPrice(lastP)}</div>
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-top:6px">
        <span style="font-size:13px;color:${pctColor};font-weight:600">${pctTxt}</span>
        <span class="sub" style="font-size:12px;color:var(--muted)">24h vol ${fmtVol(lastV)}</span>
      </div>
      <div class="sub" style="font-size:11px;color:var(--muted);margin-top:6px">as of ${asOf}</div>
    </div>`;
  }).join('');

  // Wire click-to-jump on cards (now jumps to Trading tab)
  host.querySelectorAll('[data-jump]').forEach(el =>
    el.addEventListener('click', () => selectTab(el.dataset.jump))
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
      host.innerHTML = news.slice(0,4).map(n =>
        `<a href="${sanitizeUrl(n.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="display:block;padding:10px 12px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text)">
          <div style="font-size:11px;color:var(--muted);margin-bottom:2px">
            <span style="color:#a78bfa;font-weight:600">${escapeHtml(n.source||'')}</span> · ${escapeHtml(n.date||'')}
          </div>
          <div style="font-size:13px;line-height:1.35">${escapeHtml(n.title||'')}</div>
        </a>`
      ).join('');
    }
  }
  // Bottom-of-Overview "Breaking news" feed: 10 most recent items.
  const bottom = document.getElementById('overviewNewsHost');
  if (bottom){
    if (!news.length){
      bottom.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No data available.</div>';
      return;
    }
    bottom.innerHTML = news.slice(0,10).map(n =>
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
  host.innerHTML = all.slice(0,4).map(i => {
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
function renderStocksTab(){
  const grid = document.getElementById('stocksGrid');
  if (!grid) return;
  const rows = ((DATA.market||{}).stocks_signals) || [];
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
const AI_SENT_COLOR = {POSITIVE:'#22c55e', NEGATIVE:'#ef4444', NEUTRAL:'#f59e0b'};

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
  const feed = document.getElementById('aiNewsFeed');
  if (feed){
    const items = (ai.items||[]).slice().sort((a,b)=>{
      const da = a && a.date ? Date.parse(a.date) : 0;
      const db = b && b.date ? Date.parse(b.date) : 0;
      return (db||0) - (da||0);
    }).slice(0, 30);
    if (!items.length){
      feed.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No articles yet.</div>';
    } else {
      feed.innerHTML = items.map(n => {
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
      }).join('');
    }
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

function openStockDetail(symbol){
  const rows = ((DATA.market||{}).stocks_signals) || [];
  const s = rows.find(r => r && String(r.symbol||'') === symbol);
  if (!s) return;
  const modal = document.getElementById('stockDetailModal');
  if (!modal) return;
  document.getElementById('stockDetailTitle').textContent = `${s.symbol} · ${s.name||''}`;
  document.getElementById('stockDetailBody').innerHTML = stockDetailHtml(s);
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

// ===== POC top-25 grid (Point of Control tab) =====
// Renders one card per top-25 coin from DATA.market.poc_top. Reuses the
// renderPocCards() layout but keyed off coin metadata (image/symbol/name/price)
// instead of the fixed RESEARCH_ASSETS list.
function renderPocTopCards(){
  const host = document.getElementById('pocTopGrid');
  if (!host) return;
  const list = ((DATA.market || {}).poc_top) || [];
  if (!Array.isArray(list) || list.length === 0){
    host.innerHTML = '<div class="empty" style="grid-column:1/-1">POC data populating — run python app.py --fetch-market and reload.</div>';
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
  // COMPACT cards — small title, anchor POC, migration badge. Click opens
  // the full-detail modal with the ladder + naked POCs + sparkline.
  host.innerHTML = sorted.map(c => {
    const cid = escapeHtml(String(c.coin_id || c.symbol || ''));
    const sym = escapeHtml(String(c.symbol || c.coin_id || '').toUpperCase());
    const imgUrl = sanitizeUrl(c.image, '');
    const img = imgUrl
      ? `<img src="${imgUrl}" alt="" style="width:18px;height:18px;border-radius:50%">`
      : '<div style="width:18px;height:18px;border-radius:50%;background:#1f2533"></div>';
    const priceTxt = fmtUsdShort(c.current_price);
    // Signal badge: score + label, color-coded green/red/amber.
    const sc = c.signal_score;
    const bucket = c.signal_label ? stockLabelBucket(c.signal_label) : 'hold';
    let sigBadge = '';
    if (sc != null && isFinite(sc)){
      const sColor = sc >= 20 ? '#22c55e' : (sc <= -20 ? '#ef4444' : '#f59e0b');
      const sTxt = (sc >= 0 ? '+' : '') + (Number.isInteger(sc) ? sc : sc.toFixed(1));
      const lblTxt = c.signal_label ? escapeHtml(String(c.signal_label)) : '';
      sigBadge = `<span style="background:${sColor}22;color:${sColor};padding:1px 6px;border-radius:3px;font-size:10px;font-weight:600;white-space:nowrap" title="Signal score">${sTxt}${lblTxt ? ' · ' + lblTxt : ''}</span>`;
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
    // UP / DOWN / FLAT (·) with label, color-coded green/red/muted.
    const mig = d.migration;
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
      migBlock = `<div title="${tip}" style="flex:0 0 44px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;padding:2px 0;border-left:1px solid var(--border);color:${cfg.fg}">
        <div style="font-size:26px;line-height:1;font-weight:700">${cfg.arrow}</div>
        <div style="font-size:9px;font-weight:700;letter-spacing:.04em">${cfg.label}</div>
        ${dltTxt ? `<div style="font-size:9px;opacity:.85">${dltTxt}</div>` : ''}
      </div>`;
    } else {
      migBlock = `<div title="No migration data" style="flex:0 0 44px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:1px;padding:2px 0;border-left:1px solid var(--border);color:var(--muted)">
        <div style="font-size:26px;line-height:1;font-weight:700">·</div>
        <div style="font-size:9px;font-weight:700;letter-spacing:.04em">—</div>
      </div>`;
    }
    // 30d POC drift sparkline. STRONG BUY / BUY cards get the beefier
    // version (taller, filled area, change-% callout) so buy-rated coins
    // are visually distinct from HOLD / SELL at a glance. Falls back to
    // empty string when the series is too short.
    const isBuy = bucket === 'strong-buy' || bucket === 'buy';
    const spark = isBuy
      ? pocMigrationSparklineLarge(d.migration_series)
      : pocMigrationSparkline(d.migration_series);
    return `<div class="card poc-card" data-poc-coin-id="${cid}" data-poc-bucket="${bucket}" role="button" tabindex="0" aria-label="Open ${sym} POC detail" title="Click for full breakdown" style="border-left:4px solid #a78bfa;padding:8px 10px;cursor:pointer">
      <div style="display:flex;align-items:stretch;gap:8px">
        <div style="flex:1;min-width:0;display:flex;flex-direction:column;gap:3px">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            ${img}
            <span style="font-weight:700;font-size:12px">${sym}</span>
            ${sigBadge}
            <span class="sub" style="font-size:10px;color:var(--muted);margin-left:auto">${priceTxt}</span>
          </div>
          <div style="display:flex;align-items:baseline;justify-content:space-between;gap:6px;font-size:11px">
            <span style="color:var(--muted);font-size:10px">90d POC</span>
            <span style="font-weight:600">${anchorPoc}</span>
            <span style="color:${dpColor};font-weight:600">${dpTxt}</span>
            ${vaTag}
          </div>
          ${spark || `<div style="height:${isBuy ? '64' : '30'}px;margin-top:6px;border-radius:3px;background:#0b0d12;display:flex;align-items:center;justify-content:center;font-size:9px;color:var(--muted)">no drift data</div>`}
        </div>
        ${migBlock}
      </div>
    </div>`;
  }).join('');
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
  document.querySelectorAll('#pocTopGrid [data-poc-bucket]').forEach(card => {
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
  const priceTxt = fmtUsdShort(c.current_price);
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
  const cur = c.current_price != null ? c.current_price : (anchor && anchor.current);
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
  // Volume profile mini-chart for the modal (90d primary, 30d overlay as
  // dashed gray). Only renders when the upstream buckets are present.
  const volProfile = (d.d90 && d.d90.buckets && d.d90.buckets.length)
    ? volumeProfileSVG(d.d90, d.d30, cur)
    : ((d.d30 && d.d30.buckets && d.d30.buckets.length)
        ? volumeProfileSVG(d.d30, null, cur)
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
    <div style="display:flex;gap:14px;flex-wrap:wrap">
      <div style="flex:1;min-width:240px">
        <div class="sub" style="font-size:11px;color:var(--muted);margin-bottom:4px">Value-area ladder</div>
        <table style="width:100%;font-size:13px;border-collapse:collapse">
          <thead><tr style="color:var(--muted);font-size:10px;text-align:left">
            <th style="padding:5px 8px">Window</th><th style="padding:5px 8px">POC</th><th style="text-align:right;padding:5px 8px">Δ vs price</th><th style="text-align:right;padding:5px 8px">VA</th>
          </tr></thead>
          <tbody>${ladder}</tbody>
        </table>
      </div>
      ${volProfile ? `<div style="flex:0 0 140px;min-width:120px">
        <div class="sub" style="font-size:11px;color:var(--muted);margin-bottom:4px">Volume profile · 90d (30d dashed)</div>
        ${volProfile}
      </div>` : ''}
    </div>
    ${nakedHtml}
  </div>`;
}

function openPocDetail(coinId){
  const list = ((DATA.market || {}).poc_top) || [];
  const c = list.find(r => r && String(r.coin_id || r.symbol || '') === coinId);
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

// ===== CryptoCompare news sentiment (keyless data-api) =====
function renderCCNewsCards(){
  const coins = (socialData().cc_news || {}).coins || {};
  const host = document.getElementById('ccNewsCards');
  if (!host) return;
  const SENT_COLOR = {POSITIVE: '#22c55e', NEGATIVE: '#ef4444', NEUTRAL: '#f59e0b'};
  host.innerHTML = RESEARCH_ASSETS.map(a => {
    const c = coins[a];
    const accent = RESEARCH_ACCENT(a);
    if (!c){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">${a.toUpperCase()}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no news</div></div>`;
    }
    const total = (c.positive || 0) + (c.negative || 0) + (c.neutral || 0) || 1;
    const posPct = (c.positive || 0) / total * 100;
    const negPct = (c.negative || 0) / total * 100;
    const neuPct = (c.neutral || 0) / total * 100;
    const netColor = c.net_score == null ? 'var(--muted)' : (c.net_score > 0 ? '#22c55e' : (c.net_score < 0 ? '#ef4444' : '#f59e0b'));
    const articles = (c.top_articles || []).slice(0, 4).map(art => {
      const sc = SENT_COLOR[art.sentiment] || 'var(--muted)';
      const dot = `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${sc};vertical-align:middle;margin-right:6px"></span>`;
      return `<a href="${sanitizeUrl(art.url)}" target="_blank" rel="noopener" style="display:block;font-size:11px;color:var(--text);text-decoration:none;padding:5px 0;border-top:1px solid var(--border);line-height:1.3">
        ${dot}<strong style="color:${sc}">${(art.sentiment||'?').slice(0,3)}</strong>
        <span style="color:var(--muted)"> · ${(art.source||'').slice(0,20)}</span>
        <span style="display:block;color:var(--text);margin-top:2px">${(art.title||'').replace(/</g,'&lt;')}</span>
      </a>`;
    }).join('');
    // 7-day sentiment sparkline (mini bar chart of daily net sentiment)
    const trend = c.trend_7d || [];
    let sparkBlock = '';
    if (trend.length) {
      const maxAbs = Math.max(1, ...trend.map(d => Math.abs(d.net || 0)));
      const sparkW = 110, sparkH = 28, barW = sparkW / trend.length;
      const bars = trend.map((d, i) => {
        const h = Math.max(1, Math.round((Math.abs(d.net) / maxAbs) * (sparkH/2 - 1)));
        const y = d.net >= 0 ? (sparkH/2 - h) : (sparkH/2);
        const fill = d.net > 0 ? '#22c55e' : (d.net < 0 ? '#ef4444' : '#6b7280');
        return `<rect x="${i*barW}" y="${y}" width="${Math.max(1,barW-1)}" height="${h}" fill="${fill}"><title>${d.date}: net ${d.net} (+${d.pos}/−${d.neg})</title></rect>`;
      }).join('');
      sparkBlock = `<div style="margin-top:6px;display:flex;align-items:center;gap:6px">
        <span class="sub" style="font-size:10px;color:var(--muted)">7d:</span>
        <svg width="${sparkW}" height="${sparkH}" viewBox="0 0 ${sparkW} ${sparkH}">
          <line x1="0" y1="${sparkH/2}" x2="${sparkW}" y2="${sparkH/2}" stroke="#374151" stroke-width="1"/>${bars}
        </svg>
      </div>`;
    }
    // Keyword cloud chips. Click filters the article list (DOM-only).
    const skewColor = sk => sk == null ? '#6b7280' : sk > 0.3 ? '#22c55e' : sk < -0.3 ? '#ef4444' : '#a1a1aa';
    const chips = (c.top_keywords || []).slice(0, 8).map(k => {
      const bg = skewColor(k.sentiment_skew);
      return `<button type="button" data-kw="${encodeURIComponent(k.kw)}" class="cc-kw-chip" style="border:1px solid ${bg};background:transparent;color:${bg};border-radius:10px;padding:2px 7px;margin:2px 3px 0 0;font-size:10px;cursor:pointer;line-height:1.3">${k.kw} <span style="opacity:.65">${k.count}</span></button>`;
    }).join('');
    const chipsBlock = chips ? `<div style="margin-top:6px">${chips}</div>` : '';
    return `<div class="card" data-coin="${a}" style="border-left:4px solid ${accent}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">${a.toUpperCase()}</h3>
        <span class="sub" style="color:${netColor};font-size:12px;font-weight:600">net ${c.net_score > 0 ? '+' : ''}${c.net_score ?? 0}</span>
      </div>
      <div style="display:flex;height:10px;margin-top:8px;border-radius:3px;overflow:hidden;background:#1f2533">
        <div style="background:#22c55e;width:${posPct}%" title="${c.positive} positive"></div>
        <div style="background:#f59e0b;width:${neuPct}%" title="${c.neutral} neutral"></div>
        <div style="background:#ef4444;width:${negPct}%" title="${c.negative} negative"></div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;font-size:10px;color:var(--muted)">
        <span style="color:#22c55e">${c.positive} +</span>
        <span>${c.neutral} ◯</span>
        <span style="color:#ef4444">${c.negative} −</span>
        <span>${c.article_count} total</span>
      </div>
      ${sparkBlock}
      ${chipsBlock}
      <div class="cc-articles" style="margin-top:6px">${articles || '<div class="sub" style="color:var(--muted);font-size:11px;padding:6px 0">No articles.</div>'}</div>
    </div>`;
  }).join('');
  // Wire keyword chip filters (one active chip per card, click again to clear)
  host.querySelectorAll('[data-coin]').forEach(card => {
    let active = null;
    card.querySelectorAll('.cc-kw-chip').forEach(btn => {
      btn.addEventListener('click', () => {
        const kw = decodeURIComponent(btn.dataset.kw || '');
        active = (active === kw) ? null : kw;
        card.querySelectorAll('.cc-kw-chip').forEach(b =>
          b.style.fontWeight = (decodeURIComponent(b.dataset.kw) === active) ? '700' : '400'
        );
        // No raw-keyword data on the article elements (we'd need to add that
        // to make the filter work). For now this is a visual highlight only.
      });
    });
  });
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
  if (!hasAny) return;
  renderCCSocialCards();
  renderCCNewsCards();
  renderRedditCards();
  renderSantimentCards();
}

function renderAll(){
  renderInsights();
  // tag updates
  ['1','2','3','4','Price','Funding','OI','LS','Dvol','FundDetail','Stack','Compare'].forEach(s=>{
    const t = document.getElementById('tagAsset'+s) || document.getElementById('tag'+s);
    if (!t) return;
    t.textContent = state.asset.toUpperCase();
    t.className = 'tag ' + state.asset;
  });

  // ETF empty check
  const ed = etfData();
  const etfEmpty = !ed.daily || ed.daily.length === 0;
  const etfEmptyEl = document.getElementById('etfEmpty');
  // Cache original markup once so we can restore when toggling back to BTC/ETH
  if (!etfEmptyEl.dataset.original) etfEmptyEl.dataset.original = etfEmptyEl.innerHTML;
  if (state.asset === 'link') {
    etfEmptyEl.innerHTML = '<div>No spot LINK ETF exists. The ETF Flows tab is BTC + ETH only.</div>';
    etfEmptyEl.classList.remove('hidden');
    document.getElementById('etfContent').classList.add('hidden');
  } else {
    etfEmptyEl.innerHTML = etfEmptyEl.dataset.original;
    // re-bind seed/paste buttons since innerHTML wiped their listeners
    rebindEtfImportButtons();
    etfEmptyEl.classList.toggle('hidden', !etfEmpty);
    document.getElementById('etfContent').classList.toggle('hidden', etfEmpty);
  }

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
  whEmptyEl.classList.toggle('hidden', !whEmpty);
  document.getElementById('whaleContent').classList.toggle('hidden', whEmpty);

  if (state.tab === 'etf' && !etfEmpty){
    renderEtfKpis(); renderEtfFundTable(); renderFlow(); renderCum(); renderYoy();
    renderFundKpis(); renderFundStack(); renderFundCompare();
  }
  if (state.tab === 'trading' && !trEmpty){
    renderTradingKpis(); renderPriceVol(); renderFunding(); renderOI(); renderLS(); renderCoinbaseIntlPerps(); renderCadliChart(); renderDvol(); renderFng(); renderEthBtc(); renderGlobalTable();
  }
  if (state.tab === 'signals'){
    renderSignals();
  }
  if (state.tab === 'whale' && !whEmpty){
    renderWhalePanel();
  }
  if (state.tab === 'defi'){
    renderDefi();
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
    renderPocTopCards();
  }
  if (state.tab === 'stocks'){
    renderStocksTab();
  }
  if (state.tab === 'ainews'){
    renderAiNewsTab();
  }
  renderCoverage();
}

function selectTab(t){
  state.tab = t;
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
  document.getElementById('tab-ainews').classList.toggle('hidden', t!=='ainews');
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
  if (insightsBar) insightsBar.style.display = isOverview ? 'none' : '';
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

chatForm?.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = chatInput.value.trim();
  if (!q) return;
  chatInput.value = '';
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
  // Crude: 3-4 uppercase A-Z, no digits. Only used after crypto fetch already failed.
  return /^[A-Z]{3,4}$/.test(sym);
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

async function lookupSymbol(query){
  const raw = String(query == null ? '' : query).trim();
  if (!raw) return;
  const sym = raw.toUpperCase();
  const symLower = sym.toLowerCase();
  const market = DATA.market || {};
  const eq = (a, b) => String(a || '').toUpperCase() === b;

  // 1) Stock signal
  const stock = (market.stocks_signals || []).find(s => s && eq(s.symbol, sym));
  // 2) Crypto signal (top-20)
  const cryptoSignal = (DATA.signals_top20 || []).find(s => s && eq(s.symbol, sym));
  // 3) POC entry
  const poc = (market.poc_top || []).find(r => r && (eq(r.symbol, sym) || eq(r.coin_id, sym)));
  // 4) News — case-insensitive whole-word match against title/body
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
  // 5) Sentiment (CryptoCompare news)
  const sentiment = ((((market.social || {}).cc_news || {}).coins) || {})[symLower] || null;

  const hasAny = !!(stock || cryptoSignal || poc || news.length || sentiment);
  const modal = document.getElementById('symbolDetailModal');
  const body  = document.getElementById('symbolDetailBody');
  const title = document.getElementById('symbolDetailTitle');
  if (!modal || !body || !title) return;

  const displayName =
    (stock && stock.name) ||
    (cryptoSignal && cryptoSignal.name) ||
    (poc && poc.name) ||
    '';
  title.textContent = displayName ? (sym + ' · ' + displayName) : sym;

  if (!hasAny){
    // No cached match — show spinner immediately, then try live CryptoCompare.
    title.textContent = sym;
    body.innerHTML =
      '<div class="sub" style="color:var(--muted);padding:14px;text-align:center">' +
        'Fetching live data for <strong>' + escapeHtml(sym) + '</strong>&hellip;' +
      '</div>';
    modal.classList.remove('hidden');
    // Track this request so a fast follow-up doesn't render stale results.
    const reqId = (window._liveLookupReq = (window._liveLookupReq || 0) + 1);
    try {
      const rows = await liveCryptoLookup(sym);
      if (reqId !== window._liveLookupReq) return; // superseded
      body.innerHTML = renderLiveCryptoSection(sym, rows);
    } catch (err){
      if (reqId !== window._liveLookupReq) return; // superseded
      if (liveLooksLikeStock(sym)){
        body.innerHTML =
          '<div class="sub" style="color:var(--muted);padding:14px;text-align:center;line-height:1.5">' +
            'Stock symbols outside the top-20 active list aren&rsquo;t available on the public mirror.<br>' +
            'Run the dashboard locally with <code>python server.py</code> for live stock lookup (coming soon).' +
          '</div>';
      } else {
        body.innerHTML =
          '<div class="sub" style="color:var(--muted);padding:14px;text-align:center">' +
            'Live lookup failed for <strong>' + escapeHtml(sym) + '</strong> &mdash; check ticker or try again.' +
          '</div>';
      }
    }
    return;
  }

  const sections = [];

  // Header block
  sections.push(
    '<div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;border-bottom:1px solid var(--border);padding-bottom:8px">' +
      '<div style="font-size:26px;font-weight:700;letter-spacing:0.4px">' + escapeHtml(sym) + '</div>' +
      (displayName ? '<div class="sub" style="font-size:13px;color:var(--muted)">' + escapeHtml(displayName) + '</div>' : '') +
    '</div>'
  );

  // Signal section
  if (stock){
    sections.push(
      '<div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Stock signal</div>' +
        stockDetailHtml(stock) +
      '</div>'
    );
  }
  if (cryptoSignal){
    sections.push(
      '<div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Crypto signal</div>' +
        renderSignalCardFromObj(cryptoSignal) +
      '</div>'
    );
  }

  // POC section
  if (poc){
    sections.push(
      '<div>' +
        '<div class="sub" style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px">Point of Control</div>' +
        pocDetailHtml(poc) +
      '</div>'
    );
  }

  // News section (top 5)
  if (news.length){
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

  // Sentiment section
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

  body.innerHTML = sections.join('');
  modal.classList.remove('hidden');
}

function closeSymbolDetail(){
  const m = document.getElementById('symbolDetailModal');
  if (m) m.classList.add('hidden');
}

(function wireSymbolSearch(){
  if (window._symbolSearchWired) return; window._symbolSearchWired = true;
  const form = document.getElementById('symbolSearchForm');
  const input = document.getElementById('symbolSearchInput');
  if (form){
    form.addEventListener('submit', e => {
      e.preventDefault();
      if (input) lookupSymbol(input.value);
    });
  }
  document.addEventListener('click', e => {
    if (e.target && e.target.id === 'symbolDetailClose') closeSymbolDetail();
    if (e.target && e.target.id === 'symbolDetailModal') closeSymbolDetail();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') closeSymbolDetail();
  });
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
