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
  --text:#e6e8ee; --muted:#8a93a6; --btc:#f7931a; --eth:#627eea; --link:#2a5ada;
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
th{color:var(--muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.05em}
.empty{padding:48px 16px;text-align:center;color:var(--muted)}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:10px;letter-spacing:.04em;text-transform:uppercase;border:1px solid var(--border);color:var(--muted);margin-left:6px}
.tag.btc{color:var(--btc);border-color:var(--btc)}
.tag.eth{color:var(--eth);border-color:var(--eth)}
.tag.link{color:var(--link);border-color:var(--link)}
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
    <span style="width:14px"></span>
    <button class="btn" id="shareBtn" title="Mint a read-only share link (default 3-day expiry)">🔗 Share</button>
    <button class="btn" id="refreshBtn" title="Re-fetch market + whale data (server only)">↻ Refresh</button>
  </div>
</header>

<div class="tabs">
  <div class="tab active" data-tab="overview">Overview</div>
  <div class="tab" data-tab="signals">Signals</div>
  <div class="tab" data-tab="etf">ETF Flows</div>
  <div class="tab" data-tab="trading">Trading</div>
  <div class="tab" data-tab="markets">Markets</div>
  <div class="tab" data-tab="defi">DeFi</div>
  <div class="tab" data-tab="whale">Whale Activity</div>
</div>

<div class="controls">
  <span class="lbl">Period</span>
  <button class="btn active" data-period="daily">Daily</button>
  <button class="btn" data-period="weekly">Weekly</button>
  <button class="btn" data-period="monthly">Monthly</button>
  <button class="btn" data-period="yearly">Yearly</button>
  <span class="lbl" style="margin-left:14px">Range</span>
  <button class="btn" data-range="3m">3M</button>
  <button class="btn" data-range="6m">6M</button>
  <button class="btn" data-range="1y">1Y</button>
  <button class="btn" data-range="2y">2Y</button>
  <button class="btn" data-range="3y">3Y</button>
  <button class="btn active" data-range="all">All</button>
</div>

<!-- ============ SHARE MODAL (mint / list / revoke share links) ============ -->
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
    <!-- Row 1: Signal cards (HERO — our secret sauce, clickable) -->
    <div class="row" id="overviewSignals" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))"></div>

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

    <!-- Row 3: Traditional indices (LEFT, 1/3) + Macro snapshot (RIGHT, 2/3) -->
    <div id="overviewMacroRow" style="display:grid;grid-template-columns:minmax(280px,1fr) minmax(0,2fr);gap:18px;align-items:stretch">
      <div class="chart-card" style="display:flex;flex-direction:column">
        <div class="head">
          <h2>Traditional indices <span class="tag">Yahoo</span></h2>
          <span class="desc">US market close · 1d / 5d / 30d</span>
        </div>
        <div id="overviewIndices" style="display:flex;flex-direction:column;gap:8px;padding:2px;flex:1;justify-content:flex-start"></div>
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
      </div>
    </div>
  </div>

  <!-- ============ MARKETS TAB ============ -->
  <div id="tab-markets" class="hidden">
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

  <!-- ============ WHALE TAB ============ -->
  <div id="tab-whale" class="hidden">
    <div id="whaleEmpty" class="empty hidden">No whale data. Run <code>python app.py --fetch-market</code>.</div>
    <div id="whaleContent">
      <div class="sub" id="whaleAsOf" style="margin-bottom:6px"></div>
      <div class="note">Free on-chain proxies (BTC). Real whale exchange-flow series need a paid feed (Glassnode / CryptoQuant). ETH-side proxies require Etherscan v2 — not yet wired.</div>
      <div class="row" id="whaleKpis"></div>
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

const state = { tab:'etf', asset:'btc', period:'daily', range:'all', fundwin:'30', macroRange:'1Y' };

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
const ACCENTS = {btc:'#f7931a', eth:'#627eea', link:'#2a5ada'};
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
  const lastPrice = (a.price||[]).slice(-1)[0]; const prevPrice = (a.price||[]).slice(-2,-1)[0];
  const chgPct = (lastPrice && prevPrice && prevPrice.value) ? (lastPrice.value/prevPrice.value - 1) : null;
  const lastVol = (a.volume||[]).slice(-1)[0];
  const lastFund = (a.funding||[]).slice(-1)[0];
  const lastOI = (a.open_interest_usd||[]).slice(-1)[0];
  const lastLS = (a.long_short_ratio||[]).slice(-1)[0];
  const lastDvol = (a.dvol||[]).slice(-1)[0];
  const ethbtc = (m.ethbtc||[]).slice(-1)[0];

  const items = [
    {label:'Spot price', val: lastPrice ? fmtUSD(lastPrice.value, 'auto') : '—', sub: chgPct!=null ? (chgPct>=0?'+':'')+(chgPct*100).toFixed(2)+'% 1d' : '', cls: chgPct==null?'':(chgPct>=0?'green':'red')},
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
  const empty = !sigData.btc && !sigData.eth && !sigData.link;
  document.getElementById('signalsEmpty').classList.toggle('hidden', !empty);
  document.getElementById('signalsContent').classList.toggle('hidden', empty);
  if (empty) return;
  document.getElementById('signalCards').innerHTML =
    renderSignalCard('btc') + renderSignalCard('eth') + renderSignalCard('link');
  renderSignalChart('sigBtcChart','btc');
  renderSignalChart('sigEthChart','eth');
  renderSignalChart('sigLinkChart','link');
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
  renderOverviewMacro();
  renderOverviewIndices();
  renderOverviewNews();
  renderOverviewInsights();
}

function renderOverviewSignals(){
  const sigs = DATA.signals || {};
  const order = ['btc','eth','link'];
  const labelColor = (label) => ({
    'STRONG BUY':'#16a34a', 'BUY':'#22c55e', 'HOLD':'#f59e0b',
    'SELL':'#ef4444', 'STRONG SELL':'#b91c1c',
  })[label] || '#a78bfa';
  const accent = a => ({btc:'#f7931a', eth:'#627eea', link:'#2a5ada'})[a] || '#a78bfa';
  const host = document.getElementById('overviewSignals');
  if (!host) return;
  host.innerHTML = order.map(a => {
    const s = sigs[a];
    if (!s){
      return `<div class="card"><h3>${a.toUpperCase()}</h3><div class="v">—</div><div class="sub">no signal yet</div></div>`;
    }
    const color = labelColor(s.label);
    const pct = ((s.score + 100) / 200) * 100;
    return `<div class="card" style="cursor:pointer;border-left:4px solid ${accent(a)}" data-jump="signals" title="Open Signals tab for ${a.toUpperCase()}">
      <div style="display:flex;justify-content:space-between;align-items:baseline">
        <h3 style="font-size:13px;color:var(--text)">${a.toUpperCase()} signal</h3>
        <span class="sub" style="color:var(--muted);font-size:11px">$${(s.price||0).toLocaleString(undefined,{maximumFractionDigits:0})}</span>
      </div>
      <div style="display:flex;align-items:baseline;gap:10px;margin-top:6px">
        <div class="v" style="font-size:26px;font-weight:700;color:${color}">${s.label}</div>
        <div class="sub" style="font-size:13px;color:${color};font-weight:600">${s.score>=0?'+':''}${s.score} / ±100</div>
      </div>
      <div style="height:8px;margin-top:8px;background:linear-gradient(to right,#b91c1c 0%,#ef4444 25%,#f59e0b 50%,#22c55e 75%,#16a34a 100%);border-radius:4px;position:relative">
        <div style="position:absolute;top:-3px;left:calc(${pct.toFixed(1)}% - 3px);width:6px;height:14px;background:#fff;border-radius:1px;box-shadow:0 0 0 2px #0b0d12"></div>
      </div>
      <div class="sub" style="font-size:11px;color:var(--muted);margin-top:6px">as of ${s.as_of || '?'}</div>
    </div>`;
  }).join('');

  // Wire click-to-jump on signal cards
  host.querySelectorAll('[data-jump]').forEach(el =>
    el.addEventListener('click', () => selectTab(el.dataset.jump))
  );
}

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
    renderTradingKpis(); renderPriceVol(); renderFunding(); renderOI(); renderLS(); renderDvol(); renderFng(); renderEthBtc(); renderGlobalTable();
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
  document.getElementById('tab-whale').classList.toggle('hidden', t!=='whale');
  // Period selector applies to ETF / Trading / Whale (not Signals — daily snapshot)
  const showPeriod = (t === 'etf' || t === 'trading' || t === 'whale');
  document.querySelectorAll('.btn[data-period]').forEach(b => b.style.display = showPeriod ? '' : 'none');
  document.querySelectorAll('.lbl').forEach(b => { if (b.textContent.toUpperCase() === 'PERIOD') b.style.display = showPeriod ? '' : 'none'; });
  // Overview simplification: hide BTC/ETH/LINK asset toggle, Range buttons, and the redundant Insights bar.
  const isOverview = (t === 'overview');
  // Whale is BTC-only on-chain; hide ETH/LINK so the toggle stays consistent.
  const isWhale = (t === 'whale');
  document.querySelectorAll('.btn[data-asset]').forEach(b => {
    if (isOverview) { b.style.display = 'none'; return; }
    if (isWhale && b.dataset.asset !== 'btc') { b.style.display = 'none'; return; }
    b.style.display = '';
  });
  document.querySelectorAll('.btn[data-range]').forEach(b => b.style.display = isOverview ? 'none' : '');
  document.querySelectorAll('.lbl').forEach(b => { if (b.textContent.toUpperCase() === 'RANGE') b.style.display = isOverview ? 'none' : ''; });
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
async function liveRefresh(force){
  const btn = document.getElementById('refreshBtn');
  if (btn) { btn.disabled = true; btn.textContent = '↻ refreshing…'; }
  try {
    const url = force ? '/api/refresh' : '/api/data';
    const opts = force ? {method:'POST'} : {};
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error('http '+r.status);
    const j = await r.json();
    const fresh = force ? j.data : j;
    Object.assign(DATA, fresh);
    document.getElementById('generatedAt').textContent = 'generated ' + DATA.generated_at;
    renderAll();
    if (btn) btn.textContent = '↻ Refresh';
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
