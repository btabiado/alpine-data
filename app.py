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
    except Exception as e:
        print(f"[signals] error: {e}", file=sys.stderr)
        payload["signals"] = {"btc": None, "eth": None}
    try:
        # Point of Control + Value Area derived from existing price+volume.
        # No external API call — pure compute. Attached under market.poc.
        import fetch_market as fm_mod
        if isinstance(market, dict):
            market["poc"] = fm_mod.compute_poc_all(market)
    except Exception as e:
        print(f"[poc] error: {e}", file=sys.stderr)
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
/* Mobile: collapse Overview macro/indices row to one column so phone view doesn't horizontal-scroll. */
@media (max-width:860px){
  #overviewMacroRow{grid-template-columns:1fr !important}
  .grid2{grid-template-columns:1fr !important}
  .grid3{grid-template-columns:1fr !important}
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
    <button class="btn active" data-asset="btc">BTC</button>
    <button class="btn" data-asset="eth">ETH</button>
    <button class="btn" data-asset="link">LINK</button>
    <button class="btn" data-asset="ltc">LTC</button>
    <span style="width:14px"></span>
    <button class="btn" id="shareBtn" title="Mint a read-only share link (default 3-day expiry)">🔗 Share</button>
    <button class="btn" id="refreshBtn" title="Re-fetch market + whale data (server only)">↻ Refresh</button>
  </div>
</header>

<div class="tabs">
  <div class="tab active" data-tab="overview">Crypto Overview</div>
  <div class="tab" data-tab="signals">Signals</div>
  <div class="tab" data-tab="etf">ETF Flows</div>
  <div class="tab" data-tab="trading">Trading</div>
  <div class="tab" data-tab="markets">Markets</div>
  <div class="tab" data-tab="defi">DeFi</div>
  <div class="tab" data-tab="social">Research</div>
  <div class="tab" data-tab="whale">Whale Activity</div>
</div>

<div class="controls">
  <span class="lbl">Period</span>
  <button class="btn active" data-period="daily">Daily</button>
  <button class="btn" data-period="weekly">Weekly</button>
  <button class="btn" data-period="monthly">Monthly</button>
  <button class="btn" data-period="yearly">Yearly</button>
  <span class="lbl" style="margin-left:14px">Timeframe</span>
  <button class="btn" data-range="3m">3M</button>
  <button class="btn" data-range="6m">6M</button>
  <button class="btn" data-range="1y">1Y</button>
  <button class="btn" data-range="2y">2Y</button>
  <button class="btn" data-range="3y">3Y</button>
  <button class="btn active" data-range="all">All</button>
</div>

<!-- ============ SHARE MODAL (mint / list / revoke share links) ============ -->
<!-- ============ CONFIGURE SIGNAL CARDS MODAL ============ -->
<div id="configSignalsModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;width:min(440px,100%);max-height:90vh;display:flex;flex-direction:column;gap:10px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:14px">⚙️ Configure signal cards</h2>
      <button class="btn" id="configSignalsClose">×</button>
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

<div id="shareModal" class="modal-bg hidden">
  <div style="background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:18px;width:min(640px,100%);max-height:90vh;display:flex;flex-direction:column;gap:10px;overflow:auto">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <h2 style="margin:0;font-size:14px">🔗 Share dashboard (read-only)</h2>
      <button class="btn" id="shareClose">×</button>
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
    <!-- Row 1: Signal cards (HERO — clickable) -->
    <div style="display:flex;justify-content:flex-end;margin-bottom:-6px">
      <button class="btn" id="configSignalsBtn" style="font-size:11px;padding:3px 8px" title="Pick which assets show signal cards">⚙️ Configure</button>
    </div>
    <div class="row" id="overviewSignals" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))"></div>
    <!-- LunarCrush social KPI strip — only renders when LUNARCRUSH_API_KEY is set
         and the snapshot returned data. Otherwise stays empty/hidden. -->
    <div id="lunarcrushStrip" class="hidden">
      <div class="sub" style="margin:6px 14px 6px;color:var(--muted)">
        🌙 <strong>LunarCrush</strong> — social sentiment for the assets above
      </div>
      <div class="row" id="lunarcrushKpis"></div>
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

    <!-- Row 3: Top 10 by 24h trading volume (LEFT, 1/3) + Macro snapshot (RIGHT, 2/3) -->
    <div id="overviewMacroRow" style="display:grid;grid-template-columns:minmax(280px,1fr) minmax(0,2fr);gap:18px;align-items:stretch">
      <div class="chart-card" style="cursor:pointer;display:flex;flex-direction:column" data-jump="markets" title="See full top 25 on Markets tab">
        <div class="head">
          <h2>Top 10 crypto by 24h volume <span class="tag">CoinGecko</span></h2>
          <span class="desc">most actively traded crypto right now · click to open Markets</span>
        </div>
        <div id="overviewTopVolume" style="display:flex;flex-direction:column;gap:6px;padding:2px;flex:1;justify-content:flex-start"></div>
      </div>
      <div class="chart-card" style="cursor:pointer;display:flex;flex-direction:column" data-jump="trading" title="Open Trading tab for full 1Y view">
        <div class="head">
          <h2>Macro snapshot <span class="tag">FRED</span></h2>
          <span class="desc">BTC vs DXY · S&amp;P · Gold · 10Y &middot; normalized to 100 over 3M &middot; click to zoom in</span>
        </div>
        <div class="chart-wrap" style="flex:1;min-height:380px;height:auto"><canvas id="overviewMacroChart"></canvas></div>
      </div>
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
          <button class="btn" id="pasteClose">×</button>
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
      <div class="row" id="tradingKpis"></div>
      <div class="grid2">
        <div class="chart-card">
          <div class="head"><h2>Price &amp; volume <span class="tag" id="tagPrice">BTC</span></h2><span class="desc">Spot price (line) &middot; 24h volume (bars)</span></div>
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
          <div class="head"><h2>ETH/BTC ratio</h2><span class="desc">Relative strength</span></div>
          <div class="chart-wrap"><canvas id="ethbtcChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>Market snapshot</h2><span class="desc">CoinGecko global stats</span></div>
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

  <!-- ============ SIGNALS TAB ============ -->
  <div id="tab-signals" class="hidden">
    <div id="signalsEmpty" class="empty hidden">No signal data — needs price history. Run <code>--fetch-market</code>.</div>
    <div id="signalsContent">
      <div class="note"><strong>Composite indicator, not investment advice.</strong> Score is a transparent sum of contributions from price trend (SMA50/200), momentum (RSI, MACD), positioning (funding), sentiment (Fear &amp; Greed), institutional flows (ETF 7d), and volatility (DVOL z-score). Range −100 to +100. Read the components below — that's where the score comes from. Do your own evaluation.</div>
      <div class="grid3" id="signalCards"></div>
      <div class="grid3">
        <div class="chart-card">
          <div class="head"><h2>BTC signal history (90d)</h2><span class="desc">Score &middot; price overlay</span></div>
          <div class="chart-wrap"><canvas id="sigBtcChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>ETH signal history (90d)</h2><span class="desc">Score &middot; price overlay</span></div>
          <div class="chart-wrap"><canvas id="sigEthChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>LINK signal history (90d)</h2><span class="desc">Score &middot; price overlay</span></div>
          <div class="chart-wrap"><canvas id="sigLinkChart"></canvas></div>
        </div>
        <div class="chart-card">
          <div class="head"><h2>LTC signal history (90d)</h2><span class="desc">Score &middot; price overlay</span></div>
          <div class="chart-wrap"><canvas id="sigLtcChart"></canvas></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ MARKETS TAB ============ -->
  <div id="tab-markets" class="hidden">
    <!-- Traditional indices strip — moved here from Overview. Macro context
         lives alongside the crypto top-25 so the user can scan both. -->
    <div class="chart-card" style="padding:14px 16px">
      <div class="head">
        <h2 style="margin:0;font-size:15px">Traditional indices <span class="tag">Yahoo</span></h2>
        <span class="desc">US market close · 1d / 5d / 30d &middot; 90d sparkline</span>
      </div>
      <div id="overviewIndices" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;padding:4px 2px"></div>
    </div>
    <div class="card" style="padding:12px 16px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px">
        <h2 style="margin:0;font-size:15px">Top 25 by market cap</h2>
        <span class="sub" id="marketsAsOf" style="color:var(--muted);font-size:11px"></span>
        <span style="flex:1"></span>
        <span class="lbl" style="margin:0">Sort</span>
        <button class="btn active" data-mktsort="rank">Rank</button>
        <button class="btn" data-mktsort="change_24h_pct">24h %</button>
        <button class="btn" data-mktsort="change_7d_pct">7d %</button>
        <button class="btn" data-mktsort="volume_24h_usd">Volume</button>
      </div>
    </div>
    <div class="card" style="padding:0;overflow:auto">
      <table id="marketsTable" style="margin:0">
        <thead>
          <tr>
            <th style="padding-left:14px">#</th>
            <th>Coin</th>
            <th>Price</th>
            <th>1h</th>
            <th>24h</th>
            <th>7d</th>
            <th>30d</th>
            <th>Volume 24h</th>
            <th>Market Cap</th>
            <th>Last 7d</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
    <div class="chart-card">
      <div class="head"><h2>Trending on CoinGecko (last 24h search)</h2><span class="desc">Retail attention proxy</span></div>
      <div id="trendingList" style="display:flex;flex-wrap:wrap;gap:8px;padding:6px 4px"></div>
    </div>
    <!-- DEX side of the market (GeckoTerminal) — trending pools by volume + brand-new pools -->
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
  </div>

  <!-- ============ DeFi TAB ============ -->
  <div id="tab-defi" class="hidden">
    <div class="row" id="defiKpis"></div>
    <div class="grid2">
      <div class="chart-card">
        <div class="head"><h2>TVL by chain</h2><span class="desc">Top 20 chains, 24h change colored</span></div>
        <div class="chart-wrap tall"><canvas id="defiChainsChart"></canvas></div>
      </div>
      <div class="chart-card">
        <div class="head"><h2>TVL history</h2><span class="desc">Ethereum + Solana + Arbitrum + Base, last 365 days</span></div>
        <div class="chart-wrap"><canvas id="defiTvlHistoryChart"></canvas></div>
      </div>
    </div>
    <div class="grid2">
      <div class="chart-card">
        <div class="head"><h2>Top 25 DeFi protocols</h2><span class="desc">By TVL, with 1d/7d/30d %</span></div>
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
  </div>

  <!-- ============ RESEARCH TAB (one-stop consolidated info page) ============ -->
  <div id="tab-social" class="hidden">
    <div id="socialEmpty" class="empty hidden">
      No research data yet — all free sources (Reddit, CryptoCompare, Santiment, LunarCrush) returned empty.
      Refresh or wait for the next hourly cron.
    </div>
    <div id="socialContent">
      <div class="sub" id="socialAsOf" style="margin-bottom:6px"></div>
      <div class="note">
        <strong>Research</strong> — one consolidated page for free social, dev, on-chain, and
        technical signals. Sources: Reddit (subscribers + top posts), CryptoCompare
        (Twitter / Reddit / GitHub stats per coin), Santiment (daily-active addresses +
        dev activity, refreshed once a day at 00:00 UTC to preserve the 100-call free quota),
        LunarCrush (when account plan allows), and Point of Control (volume profile
        derived from existing price+volume series).
      </div>

      <!-- ===== Point of Control ===== -->
      <div class="chart-card" style="padding:12px 16px">
        <div class="head">
          <h2 style="margin:0;font-size:15px">Point of Control <span class="tag">Volume profile</span></h2>
          <span class="desc">
            POC = price with highest cumulative volume.
            Value Area = ~70% of volume around POC.
            <a href="https://trendspider.com/learning-center/understanding-point-of-control-a-guide-for-investors-and-traders/" target="_blank" rel="noopener" style="color:#a78bfa">What is POC? ↗</a>
          </span>
        </div>
        <div class="row" id="pocCards" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))"></div>
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
          <span class="desc">Subscribers · active users now · top 24h posts</span>
        </div>
        <div class="row" id="redditCards" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))"></div>
      </div>

      <!-- ===== Santiment on-chain + dev ===== -->
      <div class="chart-card" style="padding:12px 16px">
        <div class="head">
          <h2 style="margin:0;font-size:15px">On-chain &amp; dev activity <span class="tag">Santiment</span></h2>
          <span class="desc">Daily-active addresses · dev activity (7d) · refreshed once daily</span>
        </div>
        <div class="row" id="santimentCards" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr))"></div>
      </div>

      <!-- ===== LunarCrush (kept for completeness; usually 402 on free plan) ===== -->
      <div class="chart-card" style="padding:12px 16px">
        <div class="head">
          <h2 style="margin:0;font-size:15px">LunarCrush <span class="tag">Plan-gated</span></h2>
          <span class="desc">Galaxy Score · Alt Rank · per-coin social — empty unless your plan covers free endpoints (HTTP 402 otherwise)</span>
        </div>
        <div class="row" id="socialCoinCards" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))"></div>
      </div>

      <div class="grid2">
        <div class="chart-card" style="padding:12px 16px">
          <div class="head"><h2 style="margin:0;font-size:15px">Trending topics <span class="tag">LunarCrush</span></h2><span class="desc">Top 20 by 24h interactions</span></div>
          <div style="max-height:520px;overflow:auto">
            <table id="socialTopicsTable" style="font-size:12px">
              <thead><tr><th>#</th><th>Topic</th><th class="right">24h interactions</th><th class="right">1h</th><th class="right">Posts</th></tr></thead>
              <tbody></tbody>
            </table>
          </div>
        </div>
        <div class="chart-card" style="padding:12px 16px">
          <div class="head">
            <h2 style="margin:0;font-size:15px">Topic sentiment <span class="tag">LunarCrush</span></h2>
            <span class="desc">
              <select id="socialTopicPick" style="background:#0e1118;color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:12px">
                <option value="bitcoin">Bitcoin</option>
                <option value="ethereum">Ethereum</option>
                <option value="chainlink">Chainlink</option>
                <option value="litecoin">Litecoin</option>
              </select>
            </span>
          </div>
          <div id="socialTopicDetail" style="padding:6px 2px"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ WHALE TAB ============ -->
  <div id="tab-whale" class="hidden">
    <div id="whaleEmpty" class="empty hidden">No whale data. Run <code>python app.py --fetch-market</code>.</div>
    <div id="whaleContent">
      <div class="sub" id="whaleAsOf" style="margin-bottom:6px"></div>
      <div class="note">Free on-chain proxies (BTC). Real whale exchange-flow series need a paid feed (Glassnode / CryptoQuant). ETH-side proxies require Etherscan v2 — not yet wired.</div>
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
        <div class="head"><h2>Mining pool concentration</h2><span class="desc">Hashrate share by pool (1y window) &middot; top 2 = <span id="poolsTop2">?</span></span></div>
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
    </div>
  </div>
</div>

<footer>
  Sources: ETF flows from your <code>data/*.csv</code>. Trading from CoinGecko, OKX, Deribit, Alternative.me. Whale proxies from blockchain.info. Refresh with <code>python app.py --fetch-market</code>.
</footer>

<!-- ============ CHAT DOCK ============ -->
<button id="chatFab" title="Ask about the data">💬</button>
<aside id="chatDock" aria-label="Data chat">
  <div class="chat-head">
    <div>
      <h2>Ask the data</h2>
      <div class="sub">Powered by Claude · context = your live dashboard</div>
    </div>
    <button class="btn" id="chatClose" style="padding:3px 8px;font-size:12px">×</button>
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

const state = { tab:'etf', asset:'btc', period:'daily', range:'all', fundwin:'30', macroRange:'1Y', cohortBin:'month' };

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
          <div style="font-weight:700;font-size:14px;letter-spacing:.03em">${f.fund}</div>
          <div class="sub" style="font-size:10px;color:var(--muted)">${f.share_pct.toFixed(1)}% share</div>
        </div>
        <div class="sub" style="color:var(--muted);font-size:11px;margin-top:2px;min-height:14px">${f.name||''}</div>
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
  tb.innerHTML = d.by_fund.map(f => `<tr><td>${f.fund}</td><td class="${f.total>=0?'green':'red'}">${fmtSigned(f.total)}</td><td class="${f.last_30d>=0?'green':'red'}">${fmtSigned(f.last_30d)}</td></tr>`).join('');
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

function renderPriceVol(){
  const a = tradingAssetData(); const c = accentFor(state.asset);
  const price = ra(a.price, 'last');
  const vol = ra(a.volume, 'sum');
  const labels = price.map(r=>r.date);
  destroy('price');
  charts.price = new Chart(document.getElementById('priceChart'), {
    type:'bar',
    data:{labels, datasets:[
      {type:'bar', label:'24h volume', data:vol.map(r=>r.value), backgroundColor:'#2a3140', yAxisID:'yVol', borderWidth:0, order:2},
      {type:'line', label:'Price', data:price.map(r=>r.value), borderColor:c, backgroundColor:c+'22', fill:false, tension:0.15, pointRadius:0, borderWidth:2, yAxisID:'yPrice', order:1},
    ]},
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
    return `<tr><td>${c.name}</td><td>${c.value}</td><td class="${cls}">${sign}</td><td style="color:var(--muted);font-size:12px">${c.explanation}</td></tr>`;
  }).join('');
  // Gauge: -100 to +100, 0 in middle
  const pct = ((s.score + 100) / 200) * 100;
  return `
    <div class="chart-card" style="position:relative">
      <div class="head" style="align-items:flex-start">
        <div>
          <h2 style="font-size:15px">${asset.toUpperCase()} signal <span class="tag ${asset}">$${s.price.toLocaleString(undefined,{maximumFractionDigits:0})}</span></h2>
          <div class="desc">as of ${s.as_of}</div>
        </div>
        <div style="text-align:right">
          <div style="font-size:28px;font-weight:700;color:${color}">${s.label}</div>
          <div style="font-size:13px;color:var(--muted)">score <strong style="color:${color}">${s.score>=0?'+':''}${s.score}</strong> / ±100</div>
        </div>
      </div>
      <div style="height:10px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:5px;position:relative;margin:8px 0">
        <div style="position:absolute;top:-4px;left:calc(${pct.toFixed(1)}% - 4px);width:8px;height:18px;background:#fff;border-radius:2px;box-shadow:0 0 0 2px #0b0d12"></div>
      </div>
      <table style="margin-top:6px"><thead><tr><th>Component</th><th>Value</th><th>±</th><th>Read</th></tr></thead><tbody>${compRows}</tbody></table>
      <div class="sub" style="margin-top:8px;font-size:11px">${s.disclaimer}</div>
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

function renderSignals(){
  const sigData = DATA.signals || {};
  const empty = !sigData.btc && !sigData.eth && !sigData.link && !sigData.ltc;
  document.getElementById('signalsEmpty').classList.toggle('hidden', !empty);
  document.getElementById('signalsContent').classList.toggle('hidden', empty);
  if (empty) return;
  document.getElementById('signalCards').innerHTML =
    renderSignalCard('btc') + renderSignalCard('eth') + renderSignalCard('link') + renderSignalCard('ltc');
  renderSignalChart('sigBtcChart','btc');
  renderSignalChart('sigEthChart','eth');
  renderSignalChart('sigLinkChart','link');
  renderSignalChart('sigLtcChart','ltc');
}

// ---------- Whale tab ----------
function whaleData(){ return (DATA.whale||{}).btc || {}; }

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
  renderWhaleCohortChart();
  renderWhaleProxyChart();
  renderGlassnodeStrip();
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
    type:'bar',
    data:{
      labels: dates,
      datasets:[
        // Order matters for stacking: whales on top so the orange band rides
        // the blue base — visually obvious that whales are a fraction of total.
        {label:'Non-whales (<1,000 BTC)', data:others,
         backgroundColor:'#627eea', borderWidth:0, stack:'supply'},
        {label:'Whales (≥1,000 BTC)',     data:whales,
         backgroundColor:'#f7931a', borderWidth:0, stack:'supply'},
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
        x:{stacked:true, ticks:{color:'#8a93a6', maxTicksLimit:14}, grid:{display:false}},
        y:{stacked:true, ticks:{color:'#8a93a6', callback:v=>fmtNum(v/1e6, 1) + 'M'},
           grid:{color:'#1f2533'}, title:{display:true, text:'BTC supply held (stacked)', color:'#8a93a6'}},
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
  document.querySelectorAll(`.btn[data-${group}]`).forEach(b => {
    b.classList.toggle('active', b.dataset[group] === val);
    // Only the BTC/ETH/LINK selector buttons get asset-tinted. Other groups
    // (Range, Period, Fundwin, Macrorange) keep the default active orange
    // — they don't represent an asset choice.
    if (isAssetGroup) {
      b.classList.toggle('eth',  state.asset === 'eth');
      b.classList.toggle('link', state.asset === 'link');
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

function renderMarkets(){
  // Traditional indices were moved here from Overview — reuse the same
  // render function which still targets #overviewIndices (id kept stable
  // so we don't break any other references).
  renderOverviewIndices();
  const rows = (DATA.market && DATA.market.markets_top) || [];
  const host = document.querySelector('#marketsTable tbody');
  if (!host) return;
  const asOf = document.getElementById('marketsAsOf');
  if (asOf) asOf.textContent = rows.length ? `${rows.length} coins · auto-refreshes every 30 min` : 'No data yet — refresh';
  // Sort
  const k = marketState.sort;
  const sorted = rows.slice().sort((a,b) => {
    const av = a[k], bv = b[k];
    if (k === 'rank') return (av||999) - (bv||999);
    return (bv||0) - (av||0);
  });
  const html = sorted.map(c => {
    const dir = v => v == null ? 'amber' : v >= 0 ? 'green' : 'red';
    const pct = v => v == null ? '—' : (v>=0?'+':'') + v.toFixed(2) + '%';
    const spark = (c.sparkline_7d || []).slice(-50);
    const sparkSvg = renderSparkline(spark, c.change_7d_pct >= 0);
    const img = c.image ? `<img src="${c.image}" alt="" style="width:18px;height:18px;border-radius:50%;vertical-align:middle;margin-right:6px">` : '';
    return `<tr>
      <td style="padding-left:14px;color:var(--muted)">${c.rank||''}</td>
      <td><strong>${img}${escapeHtml(c.symbol||'')}</strong> <span class="sub" style="color:var(--muted);font-size:11px">${escapeHtml(c.name||'')}</span></td>
      <td>${fmtUSD(c.price_usd,'auto')}</td>
      <td class="${dir(c.change_1h_pct)}">${pct(c.change_1h_pct)}</td>
      <td class="${dir(c.change_24h_pct)}">${pct(c.change_24h_pct)}</td>
      <td class="${dir(c.change_7d_pct)}">${pct(c.change_7d_pct)}</td>
      <td class="${dir(c.change_30d_pct)}">${pct(c.change_30d_pct)}</td>
      <td>${fmtUSD(c.volume_24h_usd,'auto')}</td>
      <td>${fmtUSD(c.market_cap_usd,'auto')}</td>
      <td>${sparkSvg}</td>
    </tr>`;
  }).join('');
  host.innerHTML = html;

  // Trending
  const trending = (DATA.market && DATA.market.trending) || [];
  const tHost = document.getElementById('trendingList');
  if (tHost) {
    if (!trending.length) tHost.innerHTML = '<div class="sub" style="color:var(--muted)">No trending data</div>';
    else tHost.innerHTML = trending.map((t,i) =>
      `<span style="display:inline-flex;align-items:center;gap:6px;padding:6px 12px;background:var(--panel2);border:1px solid var(--border);border-radius:999px;font-size:12px">
        <span style="color:var(--muted);font-size:10px">#${i+1}</span>
        ${t.thumb ? `<img src="${t.thumb}" alt="${escapeHtml(t.symbol||'')}" style="width:14px;height:14px;border-radius:50%">` : ''}
        <strong>${escapeHtml(t.symbol||'')}</strong>
        <span class="sub" style="color:var(--muted)">${escapeHtml(t.name||'')}${t.rank?` · rank ${t.rank}`:''}</span>
      </span>`
    ).join('');
  }

  // GeckoTerminal DEX pools — trending (by 24h volume) + new (newest listings)
  renderGeckoTerminalPools();
}

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

function renderSparkline(values, isUp){
  if (!values || values.length < 2) return '';
  const w = 90, h = 24;
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

document.querySelectorAll('.btn[data-mktsort]').forEach(b =>
  b.addEventListener('click', () => {
    marketState.sort = b.dataset.mktsort;
    document.querySelectorAll('.btn[data-mktsort]').forEach(x => x.classList.toggle('active', x.dataset.mktsort === marketState.sort));
    renderMarkets();
  })
);

// ---------- DeFi tab ----------
function renderDefi(){
  const defi = (DATA.market || {}).defi || {};
  const llama = (DATA.market || {}).defillama || {};
  const chains = defi.chains || [];
  const protocols = defi.protocols || [];
  const yields = defi.yields_stablecoin || [];
  const tvlHistory = defi.tvl_history || {};

  // KPIs
  const totalTvl = chains.reduce((s, c) => s + (c.tvl_usd || 0), 0);
  const ethTvl = chains.find(c => c.name === 'Ethereum')?.tvl_usd || 0;
  const solTvl = chains.find(c => c.name === 'Solana')?.tvl_usd || 0;
  const items = [
    {label: 'Total DeFi TVL', val: fmtUSD(totalTvl, 'auto'), sub: `${chains.length} chains tracked`},
    {label: 'Ethereum TVL', val: fmtUSD(ethTvl, 'auto'), sub: `${((ethTvl/totalTvl)*100).toFixed(1)}% share`},
    {label: 'Solana TVL', val: fmtUSD(solTvl, 'auto'), sub: `${((solTvl/totalTvl)*100).toFixed(1)}% share`},
    {label: 'Stablecoin mcap', val: fmtUSD(llama.stablecoin_mcap_usd, 'auto'), sub: `7d ${llama.stablecoin_7d_change_usd>=0?'+':''}${fmtUSD(llama.stablecoin_7d_change_usd,'auto')}`},
    {label: 'DEX 24h volume', val: fmtUSD(llama.dex_volume_24h_usd, 'auto')},
    {label: 'Protocol fees 24h', val: fmtUSD(llama.fees_24h_usd, 'auto')},
  ];
  document.getElementById('defiKpis').innerHTML = items.map(i =>
    `<div class="card"><h3>${i.label}</h3><div class="v">${i.val}</div>${i.sub?`<div class="sub">${i.sub}</div>`:''}</div>`
  ).join('');

  // Chains bar chart (top 15)
  destroy('defiChains');
  const top15 = chains.slice(0, 15);
  charts.defiChains = new Chart(document.getElementById('defiChainsChart'), {
    type:'bar',
    data:{
      labels: top15.map(c => c.name),
      datasets: [{
        data: top15.map(c => c.tvl_usd || 0),
        // Treat missing change_1d_pct as neutral grey (not green) so we don't
        // pretend every chain rose when DeFiLlama hasn't returned a delta yet.
        backgroundColor: top15.map(c => {
          const ch = c.change_1d_pct;
          if (ch == null) return '#475569';   // slate-600 — "no data"
          return ch >= 0 ? '#22c55e' : '#ef4444';
        }),
        borderWidth: 0,
      }],
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      plugins: {legend:{display:false}, tooltip:{callbacks:{label: ctx => {
        const c = top15[ctx.dataIndex];
        const ch = c.change_1d_pct;
        const chStr = (ch == null) ? '—' : `${ch>=0?'+':''}${ch.toFixed(2)}%`;
        return `TVL ${fmtUSD(ctx.parsed.x,'auto')} (1d ${chStr})`;
      }}}},
      scales: {
        x:{ticks:{color:'#8a93a6', callback:v=>fmtUSD(v,'auto')}, grid:{color:'#1f2533'}},
        y:{ticks:{color:'#e6e8ee'}, grid:{display:false}},
      },
    },
  });

  // TVL history (4 chains)
  destroy('defiTvlHistory');
  const palette = {Ethereum:'#627eea', Solana:'#9945FF', Arbitrum:'#28a0f0', Base:'#0052ff'};
  const activeChains = Object.keys(tvlHistory).filter(k => tvlHistory[k].length);
  // Build union of dates across all active chains so an empty Ethereum series
  // doesn't strip x-axis labels from the rest of the chart.
  const dateSet = new Set();
  activeChains.forEach(chain => {
    tvlHistory[chain].forEach(p => dateSet.add(p.date));
  });
  const labels = Array.from(dateSet).sort();
  const datasets = activeChains.map(chain => {
    const byDate = {};
    tvlHistory[chain].forEach(p => { byDate[p.date] = p.tvl_usd; });
    return {
      label: chain,
      data: labels.map(d => (d in byDate ? byDate[d] : null)),
      borderColor: palette[chain] || '#a78bfa',
      backgroundColor: 'transparent',
      pointRadius: 0,
      borderWidth: 1.8,
      tension: 0.2,
      spanGaps: true,
    };
  });
  if (datasets.length) {
    charts.defiTvlHistory = new Chart(document.getElementById('defiTvlHistoryChart'), {
      type:'line',
      data:{labels, datasets},
      options:{
        responsive:true, maintainAspectRatio:false,
        plugins:{legend:{labels:{color:'#e6e8ee'}}, tooltip:{mode:'index', intersect:false, callbacks:{label: ctx => `${ctx.dataset.label}: ${fmtUSD(ctx.parsed.y,'auto')}`}}},
        scales:{
          x:{ticks:{color:'#8a93a6', maxTicksLimit:10}, grid:{color:'#1f2533'}},
          y:{ticks:{color:'#8a93a6', callback:v=>fmtUSD(v,'auto')}, grid:{color:'#1f2533'}},
        },
      },
    });
  }

  // Protocols table
  const protoBody = document.querySelector('#defiProtocolsTable tbody');
  if (protoBody) {
    protoBody.innerHTML = protocols.map((p, i) => {
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

  // Yields table
  const yieldsBody = document.querySelector('#defiYieldsTable tbody');
  if (yieldsBody) {
    yieldsBody.innerHTML = yields.map(y => `<tr>
      <td><strong>${escapeHtml(y.project||'')}</strong> <span class="sub" style="color:var(--muted);font-size:11px">${escapeHtml(y.symbol||'')}</span></td>
      <td>${escapeHtml(y.chain||'')}</td>
      <td>${fmtUSD(y.tvl_usd,'auto')}</td>
      <td class="${(y.apy_pct||0)>=4?'green':(y.apy_pct||0)>=1?'amber':'red'}">${(y.apy_pct||0).toFixed(2)}%</td>
    </tr>`).join('');
  }
}

// ---------- News feed (Trading tab) ----------
function renderNews(){
  const news = (DATA.market || {}).news || [];
  const host = document.getElementById('newsFeed');
  if (!host) return;
  if (!news.length) {
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No headlines available</div>';
    return;
  }
  host.innerHTML = news.slice(0, 25).map(n =>
    `<a href="${n.url}" target="_blank" rel="noopener" style="display:block;padding:10px 12px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text);transition:background .1s" onmouseover="this.style.background='#10151f'" onmouseout="this.style.background=''">
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
  renderOverviewSignals();
  renderLunarcrushStrip();
  renderOverviewMacro();
  renderOverviewTopVolume();
  renderOverviewNews();
  renderOverviewInsights();
}

// LunarCrush social KPI strip — visible only when the env-gated snapshot
// returned data (LUNARCRUSH_API_KEY set in ~/.zprofile). Shows the social
// signal for the same assets that have signal cards above (BTC/ETH/LINK/LTC
// by default, or whatever the user configured via the ⚙️ picker).
function renderLunarcrushStrip(){
  const strip = document.getElementById('lunarcrushStrip');
  const host  = document.getElementById('lunarcrushKpis');
  if (!strip || !host) return;
  const lc = (DATA.market || {}).lunarcrush || {};
  if (!lc.available || !(lc.coins || []).length) {
    strip.classList.add('hidden');
    return;
  }
  const order = getSignalOrder().map(a => a.toUpperCase());
  const bySym = {};
  for (const c of lc.coins) {
    if (c.symbol) bySym[c.symbol.toUpperCase()] = c;
  }
  const cards = order.map(sym => {
    const c = bySym[sym];
    if (!c) {
      return `<div class="card"><h3>${sym} social</h3><div class="v">—</div><div class="sub">not in top 50</div></div>`;
    }
    const galaxy = c.galaxy_score != null ? c.galaxy_score : '—';
    const altrank = c.alt_rank != null ? `#${c.alt_rank}` : '—';
    const sentiment = c.sentiment != null ? c.sentiment : null;
    // Sentiment from LunarCrush is 1-5 (1=bearish, 5=bullish)
    const sentLabel = sentiment == null ? '—' :
      sentiment >= 4 ? `bullish (${sentiment})` :
      sentiment <= 2 ? `bearish (${sentiment})` :
      `neutral (${sentiment})`;
    const sentCls = sentiment == null ? '' :
      sentiment >= 4 ? 'green' : sentiment <= 2 ? 'red' : 'amber';
    return `<div class="card">
      <h3>${sym} social</h3>
      <div class="v ${sentCls}" style="font-size:18px">${sentLabel}</div>
      <div class="sub">Galaxy ${galaxy} · AltRank ${altrank}</div>
    </div>`;
  });
  host.innerHTML = cards.join('');
  strip.classList.remove('hidden');
}

// Top 10 coins by 24h trading volume (USD). Derives from the cached
// markets_top list (which is top-25 by market cap) by re-sorting on
// volume_24h_usd. Filters out stablecoins (USD-suffix) so the strip
// stays focused on price-discovery flow, not USDT/USDC settlement churn.
function renderOverviewTopVolume(){
  const host = document.getElementById('overviewTopVolume');
  if (!host) return;
  const all = (DATA.market && DATA.market.markets_top) || [];
  // Stablecoin filter: catch every "USD"-prefixed (USDT/USDC/USDS/USD1/USDe/USDP/...)
  // or "USD"-suffixed (BUSD/FDUSD/PYUSD/TUSD/GUSD/...) symbol, plus DAI by name.
  // Anything new that follows either naming convention auto-qualifies.
  const isStable = c => {
    const s = (c.symbol || '').toUpperCase();
    return /^USD/.test(s) || /USD$/.test(s) || s === 'DAI';
  };
  const rows = all
    .filter(c => !isStable(c) && (c.volume_24h_usd || 0) > 0)
    .sort((a, b) => (b.volume_24h_usd || 0) - (a.volume_24h_usd || 0))
    .slice(0, 10);
  if (!rows.length) {
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:8px">No market data yet — refresh.</div>';
    return;
  }
  host.innerHTML = rows.map((c, i) => {
    const img = c.image ? `<img src="${c.image}" alt="" style="width:18px;height:18px;border-radius:50%">` : '';
    const ch24 = c.change_24h_pct;
    const chCls = ch24 == null ? '' : (ch24 >= 0 ? 'green' : 'red');
    const chStr = ch24 == null ? '—' : (ch24 >= 0 ? '+' : '') + ch24.toFixed(2) + '%';
    return `<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;border-bottom:1px solid var(--border);font-size:12px">
      <span style="color:var(--muted);width:18px;text-align:right">${i+1}</span>
      ${img}
      <span style="font-weight:600;min-width:48px">${escapeHtml(c.symbol||'')}</span>
      <span style="flex:1;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(c.name||'')}</span>
      <span style="font-variant-numeric:tabular-nums;min-width:64px;text-align:right">${fmtUSD(c.volume_24h_usd, 'auto')}</span>
      <span class="${chCls}" style="font-variant-numeric:tabular-nums;min-width:56px;text-align:right">${chStr}</span>
    </div>`;
  }).join('');
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

function renderOverviewIndices(){
  const y = ((DATA.market || {}).yahoo_indices) || {};
  const items = [
    {key:'dow',     short:'DOW',    name:'Dow Jones'},
    {key:'sp500',   short:'S&P 500',name:'S&P 500'},
    {key:'nasdaq',  short:'NASDAQ', name:'NASDAQ Composite'},
    {key:'vix',     short:'VIX',    name:'Volatility Index'},
  ];
  const host = document.getElementById('overviewIndices');
  if (!host) return;
  host.innerHTML = items.map(i => {
    const v = y[i.key];
    if (!v) return `<div class="card" style="padding:10px 12px"><h3 style="font-size:11px">${i.short}</h3><div class="v">—</div></div>`;
    const cls1d = (v.change_1d_pct||0) >= 0 ? 'green' : 'red';
    const cls5d = (v.change_5d_pct||0) >= 0 ? 'green' : 'red';
    const cls30d = (v.change_30d_pct||0) >= 0 ? 'green' : 'red';
    const pct = x => (x>=0?'+':'') + x.toFixed(2) + '%';
    const spark = renderSparkline(v.sparkline_90d || [], (v.change_30d_pct||0) >= 0);
    return `<div class="card" style="padding:10px 12px;display:flex;align-items:center;gap:12px">
      <div style="flex:1;min-width:0">
        <h3 style="margin:0;font-size:11px;color:var(--muted)">${i.short}</h3>
        <div class="v" style="font-size:20px;font-weight:600;margin-top:2px">${(v.latest||0).toLocaleString(undefined,{maximumFractionDigits:2})}</div>
        <div style="display:flex;gap:8px;font-size:11px;margin-top:2px">
          <span class="${cls1d}">${pct(v.change_1d_pct||0)} 1d</span>
          <span class="${cls5d}">${pct(v.change_5d_pct||0)} 5d</span>
          <span class="${cls30d}">${pct(v.change_30d_pct||0)} 30d</span>
        </div>
      </div>
      <div style="flex-shrink:0">${spark}</div>
    </div>`;
  }).join('');
}

function renderOverviewNews(){
  const news = ((DATA.market || {}).news) || [];
  const host = document.getElementById('overviewNews');
  if (!host) return;
  if (!news.length){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:14px">No headlines yet</div>';
    return;
  }
  host.innerHTML = news.slice(0,4).map(n =>
    `<a href="${n.url}" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="display:block;padding:10px 12px;border-bottom:1px solid var(--border);text-decoration:none;color:var(--text)">
      <div style="font-size:11px;color:var(--muted);margin-bottom:2px">
        <span style="color:#a78bfa;font-weight:600">${escapeHtml(n.source||'')}</span> · ${escapeHtml(n.date||'')}
      </div>
      <div style="font-size:13px;line-height:1.35">${escapeHtml(n.title||'')}</div>
    </a>`
  ).join('');
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
function renderPocCards(){
  const poc = (DATA.market||{}).poc || {};
  const host = document.getElementById('pocCards');
  if (!host) return;
  host.innerHTML = RESEARCH_ASSETS.map(a => {
    const d = poc[a];
    const accent = RESEARCH_ACCENT(a);
    if (!d || (!d.d30 && !d.d90)){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">${a.toUpperCase()}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no POC data</div></div>`;
    }
    const r = d.d90 || d.d30;
    const inVA = r.in_value_area;
    const distColor = r.distance_pct == null ? 'var(--muted)' : (r.distance_pct >= 0 ? '#22c55e' : '#ef4444');
    const distTxt  = r.distance_pct == null ? '—' : (r.distance_pct >= 0 ? '+' : '') + r.distance_pct.toFixed(2) + '%';
    const vaBadge = inVA
      ? '<span style="background:#22c55e22;color:#22c55e;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:600">IN VA</span>'
      : '<span style="background:#f59e0b22;color:#f59e0b;padding:2px 6px;border-radius:3px;font-size:10px;font-weight:600">OUTSIDE</span>';
    // 30d alt
    const r30 = d.d30;
    const alt30 = r30
      ? `<div class="sub" style="font-size:11px;color:var(--muted);margin-top:6px">30d POC ${fmtUsdShort(r30.poc)} · VA ${fmtUsdShort(r30.val)} – ${fmtUsdShort(r30.vah)}</div>`
      : '';
    return `<div class="card" style="border-left:4px solid ${accent}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">${a.toUpperCase()} <span class="sub" style="color:var(--muted);font-size:10px">${r.lookback_days}d</span></h3>
        ${vaBadge}
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:8px">
        <div>
          <div class="sub" style="font-size:10px;color:var(--muted)">POC</div>
          <div class="v" style="font-size:18px;font-weight:700">${fmtUsdShort(r.poc)}</div>
        </div>
        <div style="text-align:right">
          <div class="sub" style="font-size:10px;color:var(--muted)">Current</div>
          <div class="v" style="font-size:18px;font-weight:700">${fmtUsdShort(r.current)}</div>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:12px">
        <span class="sub" style="color:var(--muted)">VA: ${fmtUsdShort(r.val)} – ${fmtUsdShort(r.vah)}</span>
        <span style="color:${distColor};font-weight:600">${distTxt} from POC</span>
      </div>
      ${alt30}
    </div>`;
  }).join('');
}

// ===== CryptoCompare social + dev stats =====
function renderCCSocialCards(){
  const cc = (socialData().cryptocompare || {}).coins || {};
  const host = document.getElementById('ccSocialCards');
  if (!host) return;
  host.innerHTML = RESEARCH_ASSETS.map(a => {
    const c = cc[a];
    const accent = RESEARCH_ACCENT(a);
    if (!c){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">${a.toUpperCase()}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no data</div></div>`;
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
  const order = ['cryptocurrency', 'bitcoin', 'ethereum', 'chainlink', 'litecoin'];
  const labelAccent = {bitcoin:'#f7931a', ethereum:'#627eea', chainlink:'#2a5ada', litecoin:'#bfbbbb', cryptocurrency:'#a78bfa'};
  host.innerHTML = order.map(name => {
    const s = subs[name];
    const accent = labelAccent[name] || '#a78bfa';
    if (!s || !s.ok){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">/r/${s?.sub || name}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no Reddit data</div></div>`;
    }
    const posts = (s.top_posts || []).slice(0, 3).map(p => `
      <a href="${p.url}" target="_blank" rel="noopener" style="display:block;font-size:11px;color:var(--text);text-decoration:none;padding:4px 0;border-top:1px solid var(--border)">
        <span style="color:var(--muted)">▲ ${fmtNumShort(p.score)} · 💬 ${fmtNumShort(p.comments)}</span>
        <span style="display:block;color:var(--text);line-height:1.3">${(p.title||'').replace(/</g,'&lt;')}</span>
      </a>
    `).join('') || '<div class="sub" style="color:var(--muted);font-size:11px;padding:6px 0">No top posts.</div>';
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
      <div style="margin-top:8px">${posts}</div>
    </div>`;
  }).join('');
}

// ===== Santiment on-chain + dev =====
function renderSantimentCards(){
  const coins = (socialData().santiment || {}).coins || {};
  const stale = (socialData().santiment || {}).stale;
  const host = document.getElementById('santimentCards');
  if (!host) return;
  host.innerHTML = RESEARCH_ASSETS.map(a => {
    const c = coins[a];
    const accent = RESEARCH_ACCENT(a);
    if (!c){
      return `<div class="card" style="border-left:4px solid ${accent}"><h3 style="font-size:13px">${a.toUpperCase()}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no Santiment data</div></div>`;
    }
    const daa = c.daily_active_addresses || [];
    const dev = c.dev_activity || [];
    const lastDAA = daa.length ? daa[daa.length-1] : null;
    const firstDAA = daa.length ? daa[0] : null;
    const lastDev = dev.length ? dev[dev.length-1] : null;
    const daaDelta = (lastDAA && firstDAA && firstDAA.value)
      ? (lastDAA.value - firstDAA.value) / firstDAA.value * 100
      : null;
    const daaColor = daaDelta == null ? 'var(--muted)' : (daaDelta >= 0 ? '#22c55e' : '#ef4444');
    const daaDeltaTxt = daaDelta == null ? '' : ' (' + (daaDelta >= 0 ? '+' : '') + daaDelta.toFixed(1) + '% 7d)';
    return `<div class="card" style="border-left:4px solid ${accent}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">${a.toUpperCase()}</h3>
        <span class="sub" style="color:var(--muted);font-size:11px">${ASSET_FULLNAME[a]}</span>
      </div>
      <div style="margin-top:8px">
        <div class="sub" style="font-size:10px;color:var(--muted)">Daily-active addresses (latest)</div>
        <div class="v" style="font-size:18px;font-weight:700">${lastDAA ? fmtNumShort(lastDAA.value) : '—'}<span style="font-size:11px;color:${daaColor};font-weight:600">${daaDeltaTxt}</span></div>
      </div>
      <div style="margin-top:8px">
        <div class="sub" style="font-size:10px;color:var(--muted)">Dev activity (latest)</div>
        <div class="v" style="font-size:18px;font-weight:700">${lastDev ? Math.round(lastDev.value).toLocaleString() : '—'}</div>
      </div>
      ${stale ? '<div class="sub" style="font-size:10px;color:var(--muted);margin-top:8px">cached (daily-gated)</div>' : ''}
    </div>`;
  }).join('');
}

// ===== LunarCrush (kept for completeness; usually empty due to 402) =====
function renderSocialCoinCards(){
  const lunar = socialData().lunarcrush || {};
  const coins = lunar.coins || {};
  const order = ['btc','eth','link','ltc'];
  const accent = a => ({btc:'#f7931a', eth:'#627eea', link:'#2a5ada', ltc:'#bfbbbb'})[a] || '#a78bfa';
  const host = document.getElementById('socialCoinCards');
  if (!host) return;
  host.innerHTML = order.map(a => {
    const c = coins[a];
    if (!c){
      return `<div class="card" style="border-left:4px solid ${accent(a)}"><h3 style="font-size:13px">${a.toUpperCase()}</h3><div class="sub" style="color:var(--muted);margin-top:8px">no social data</div></div>`;
    }
    const gs = c.galaxy_score;
    const ar = c.alt_rank;
    const pc = c.percent_change_24h;
    const pcColor = pc == null ? 'var(--muted)' : (pc >= 0 ? '#22c55e' : '#ef4444');
    const pcTxt  = pc == null ? '—' : (pc >= 0 ? '+' : '') + Number(pc).toFixed(2) + '%';
    const intxs = c.interactions_24h;
    const intxsTxt = intxs == null ? '—' :
      (intxs >= 1e6 ? (intxs/1e6).toFixed(1) + 'M' :
       intxs >= 1e3 ? (intxs/1e3).toFixed(1) + 'K' : String(intxs));
    return `<div class="card" style="border-left:4px solid ${accent(a)}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">${a.toUpperCase()}</h3>
        <span class="sub" style="color:var(--muted);font-size:11px">${c.name || ''}</span>
      </div>
      <div style="display:flex;gap:14px;margin-top:8px">
        <div>
          <div class="sub" style="font-size:10px;color:var(--muted)">Galaxy</div>
          <div class="v" style="font-size:20px;font-weight:700">${gs == null ? '—' : Math.round(gs)}</div>
        </div>
        <div>
          <div class="sub" style="font-size:10px;color:var(--muted)">Alt rank</div>
          <div class="v" style="font-size:20px;font-weight:700">${ar == null ? '—' : '#' + ar}</div>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;margin-top:8px;font-size:12px">
        <span style="color:${pcColor};font-weight:600">${pcTxt}</span>
        <span class="sub" style="color:var(--muted)">24h intxs ${intxsTxt}</span>
      </div>
    </div>`;
  }).join('');
}

function renderSocialTopics(){
  const social = socialData();
  const topics = (social.lunarcrush || {}).topics || [];
  const tbody = document.querySelector('#socialTopicsTable tbody');
  if (!tbody) return;
  if (!topics.length){
    tbody.innerHTML = '<tr><td colspan="5" class="sub" style="color:var(--muted);padding:12px 6px">No trending topic data.</td></tr>';
    return;
  }
  const fmtN = n => n == null ? '—' :
    (n >= 1e6 ? (n/1e6).toFixed(1) + 'M' :
     n >= 1e3 ? (n/1e3).toFixed(1) + 'K' : String(n));
  tbody.innerHTML = topics.map((t, i) => {
    const trend = t.trend;
    const trendArrow = trend == null ? '' : (trend > 0 ? '▲' : (trend < 0 ? '▼' : '·'));
    const trendColor = trend == null ? 'var(--muted)' : (trend > 0 ? '#22c55e' : (trend < 0 ? '#ef4444' : 'var(--muted)'));
    return `<tr>
      <td style="color:var(--muted)">${(t.topic_rank ?? (i+1))}</td>
      <td><strong>${t.title || t.topic || '?'}</strong> <span style="color:${trendColor}">${trendArrow}</span></td>
      <td class="right">${fmtN(t.interactions_24h)}</td>
      <td class="right" style="color:var(--muted)">${fmtN(t.interactions_1h)}</td>
      <td class="right" style="color:var(--muted)">${fmtN(t.num_posts)}</td>
    </tr>`;
  }).join('');
}

function renderSocialTopicDetail(){
  const pick = document.getElementById('socialTopicPick');
  const host = document.getElementById('socialTopicDetail');
  if (!pick || !host) return;
  const social = socialData();
  const detail = ((social.lunarcrush || {}).topic_detail || {})[pick.value];
  if (!detail){
    host.innerHTML = '<div class="sub" style="color:var(--muted);padding:12px 4px">No data for this topic.</div>';
    return;
  }
  const fmtN = n => n == null ? '—' :
    (n >= 1e6 ? (n/1e6).toFixed(1) + 'M' :
     n >= 1e3 ? (n/1e3).toFixed(1) + 'K' : String(n));
  const sentByType = detail.types_sentiment || {};
  const countByType = detail.types_count || {};
  const intxByType = detail.types_interactions || {};
  // Sort sources by 24h interactions
  const sources = Object.keys(sentByType).concat(Object.keys(countByType))
    .filter((v,i,a) => a.indexOf(v) === i)
    .sort((a, b) => (intxByType[b]||0) - (intxByType[a]||0));
  const rows = sources.map(src => {
    const s = sentByType[src];
    const sColor = s == null ? 'var(--muted)' : (s >= 60 ? '#22c55e' : (s >= 40 ? '#f59e0b' : '#ef4444'));
    const pct = s == null ? 0 : Math.max(0, Math.min(100, s));
    return `<tr>
      <td><strong>${src}</strong></td>
      <td style="width:40%">
        <div style="background:#1f2533;border-radius:3px;height:8px;position:relative">
          <div style="background:${sColor};width:${pct}%;height:100%;border-radius:3px"></div>
        </div>
      </td>
      <td class="right" style="color:${sColor};font-weight:600">${s == null ? '—' : Math.round(s)}%</td>
      <td class="right" style="color:var(--muted)">${fmtN(countByType[src])} posts</td>
      <td class="right" style="color:var(--muted)">${fmtN(intxByType[src])} intxs</td>
    </tr>`;
  }).join('');
  host.innerHTML = `
    <div style="display:flex;gap:18px;margin-bottom:10px;flex-wrap:wrap">
      <div><div class="sub" style="font-size:10px;color:var(--muted)">Rank</div><div style="font-size:18px;font-weight:700">#${detail.topic_rank ?? '—'}</div></div>
      <div><div class="sub" style="font-size:10px;color:var(--muted)">24h interactions</div><div style="font-size:18px;font-weight:700">${fmtN(detail.interactions_24h)}</div></div>
      <div><div class="sub" style="font-size:10px;color:var(--muted)">Contributors</div><div style="font-size:18px;font-weight:700">${fmtN(detail.num_contributors)}</div></div>
      <div><div class="sub" style="font-size:10px;color:var(--muted)">Posts</div><div style="font-size:18px;font-weight:700">${fmtN(detail.num_posts)}</div></div>
    </div>
    <table style="font-size:12px;margin-top:4px">
      <thead><tr><th>Source</th><th>Positive sentiment</th><th class="right">%</th><th class="right">Posts</th><th class="right">Intxs</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="sub" style="color:var(--muted);padding:8px 4px">No per-source breakdown.</td></tr>'}</tbody>
    </table>`;
}

function renderSocial(){
  const social = socialData();
  const lunar = social.lunarcrush || {};
  const poc = (DATA.market||{}).poc || {};
  const hasAny =
    Object.keys(lunar.coins||{}).length ||
    (lunar.topics||[]).length ||
    Object.keys(lunar.topic_detail||{}).length ||
    Object.keys((social.cryptocompare||{}).coins||{}).length ||
    Object.keys((social.reddit||{}).subreddits||{}).length ||
    Object.keys((social.santiment||{}).coins||{}).length ||
    Object.keys(poc).length;
  document.getElementById('socialEmpty').classList.toggle('hidden', !!hasAny);
  document.getElementById('socialContent').classList.toggle('hidden', !hasAny);
  const asOf = document.getElementById('socialAsOf');
  if (asOf) asOf.textContent = social.fetched_at ? 'Fetched ' + social.fetched_at : '';
  if (!hasAny) return;
  renderPocCards();
  renderCCSocialCards();
  renderRedditCards();
  renderSantimentCards();
  renderSocialCoinCards();
  renderSocialTopics();
  renderSocialTopicDetail();
}

// Wire topic-picker change. Idempotent — addEventListener dedupes via marker.
(function wireSocialPicker(){
  const pick = document.getElementById('socialTopicPick');
  if (pick && !pick.dataset.wired){
    pick.dataset.wired = '1';
    pick.addEventListener('change', renderSocialTopicDetail);
  }
})();

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

  const wd = whaleData();
  const whEmptyEl = document.getElementById('whaleEmpty');
  if (!whEmptyEl.dataset.original) whEmptyEl.dataset.original = whEmptyEl.innerHTML;
  const whEmpty = !wd.tx_volume_usd || wd.tx_volume_usd.length === 0;
  if (state.asset !== 'btc') {
    whEmptyEl.innerHTML = `<div>Whale Activity is BTC-only (free on-chain proxies via blockchain.info). Switch to BTC to view.</div>`;
    whEmptyEl.classList.remove('hidden');
    document.getElementById('whaleContent').classList.add('hidden');
  } else {
    whEmptyEl.innerHTML = whEmptyEl.dataset.original;
    whEmptyEl.classList.toggle('hidden', !whEmpty);
    document.getElementById('whaleContent').classList.toggle('hidden', whEmpty);
  }

  if (state.tab === 'etf' && !etfEmpty){
    renderEtfKpis(); renderEtfFundTable(); renderFlow(); renderCum(); renderYoy();
    renderFundKpis(); renderFundStack(); renderFundCompare();
  }
  if (state.tab === 'trading' && !trEmpty){
    renderTradingKpis(); renderPriceVol(); renderFunding(); renderOI(); renderLS(); renderCoinbaseIntlPerps(); renderDvol(); renderFng(); renderEthBtc(); renderGlobalTable();
  }
  if (state.tab === 'signals'){
    renderSignals();
  }
  if (state.tab === 'whale' && state.asset === 'btc' && !whEmpty){
    renderWhaleKpis(); renderWhale(); renderWhaleExtras();
  }
  if (state.tab === 'markets'){
    renderMarkets();
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
  renderCoverage();
}

function selectTab(t){
  state.tab = t;
  // Whale Activity is BTC-only (free on-chain proxies from blockchain.info).
  // Force the asset to BTC so the page renders something useful instead of
  // the "switch to BTC" empty state when the user is on ETH or LINK.
  if (t === 'whale' && state.asset !== 'btc') {
    state.asset = 'btc';
    setActive('asset', 'btc');
  }
  document.querySelectorAll('.tab').forEach(el => {
    el.classList.toggle('active', el.dataset.tab === t);
    el.classList.toggle('eth',  state.asset === 'eth');
    el.classList.toggle('link', state.asset === 'link');
  });
  document.getElementById('tab-overview').classList.toggle('hidden', t!=='overview');
  document.getElementById('tab-etf').classList.toggle('hidden', t!=='etf');
  document.getElementById('tab-trading').classList.toggle('hidden', t!=='trading');
  document.getElementById('tab-signals').classList.toggle('hidden', t!=='signals');
  document.getElementById('tab-markets').classList.toggle('hidden', t!=='markets');
  document.getElementById('tab-defi').classList.toggle('hidden', t!=='defi');
  document.getElementById('tab-social').classList.toggle('hidden', t!=='social');
  document.getElementById('tab-whale').classList.toggle('hidden', t!=='whale');
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
document.querySelectorAll('.tab').forEach(b =>
  b.addEventListener('click', () => selectTab(b.dataset.tab))
);

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

// If running under server, poll /api/data every 60s for the latest cached payload
const isServer = location.protocol.startsWith('http');
if (isServer) setInterval(() => liveRefresh(false), 60000);

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

document.getElementById('generatedAt').textContent = 'generated ' + DATA.generated_at;
selectTab('overview');
renderAll();
</script>
</body>
</html>
"""



if __name__ == "__main__":
    sys.exit(main())
