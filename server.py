"""
Live web server for the dashboard.

    python server.py
    # → http://127.0.0.1:8765/

Endpoints:
    GET  /                       dashboard HTML (re-rendered from latest cache on every load)
    GET  /api/data               JSON payload for in-page hot reload
    POST /api/refresh            re-fetches market + whale data, returns fresh payload
    GET  /healthz                liveness probe

Background:
    A daemon thread refreshes market + whale data every REFRESH_MINUTES
    (default 30). Set to 0 to disable.

Env:
    HOST        bind address           (default 127.0.0.1)
    PORT        bind port              (default 8765)
    REFRESH_MINUTES  auto-fetch every N minutes (default 30, 0 to disable)
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, jsonify, request

import app as dash
import fetch_live
import fetch_market
import shares

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
REFRESH_MINUTES = int(os.environ.get("REFRESH_MINUTES", "30"))

flask_app = Flask(__name__)


# ---------- HTTP Basic Auth ----------
# Set both env vars to enable. If either is missing, the dashboard runs
# wide-open (legacy behaviour) so local dev stays frictionless.
#
#   export DASH_USER="btabiado"
#   export DASH_PASS="<your-strong-password>"
#
DASH_USER = os.environ.get("DASH_USER")
DASH_PASS = os.environ.get("DASH_PASS")
AUTH_ENABLED = bool(DASH_USER and DASH_PASS)

# Endpoints that bypass auth (so the bookmarklet from Farside still works
# cross-origin, and so the health probe stays usable for monitoring).
_AUTH_BYPASS = {"/healthz"}

# Endpoints that share-token holders are explicitly allowed to hit. Anything
# that mutates server state (refresh, CSV upload, ETF seed, share admin) is
# intentionally excluded — share viewers are strictly read-only.
_SHARE_ALLOWED = {"/", "/api/data", "/api/chat", "/api/export/csv"}


# Whitelist of series the CSV exporter is allowed to surface. Anything outside
# this set is rejected with 400 — both to keep the surface area small and to
# avoid leaking unrelated payload fields (e.g. insights text) via this route.
_ALLOWED_SERIES: set[str] = {
    "btc.daily",
    "eth.daily",
    "market.btc.price",
    "market.eth.price",
    "market.link.price",
    "market.btc.funding",
    "market.eth.funding",
    "market.btc.dvol",
    "market.fear_greed",
    "market.fred.dxy",
    "market.fred.sp500",
    "market.fred.gold",
    "market.fred.treasury_10y",
    "whale.btc.tx_volume_usd",
    "whale.btc.tx_count",
    "whale.btc.active_addresses",
    "whale.btc.avg_tx_usd",
    "whale.btc.miners_revenue_usd",
    "whale.btc.hash_rate",
}


def _resolve_series(payload: dict, dotted_path: str) -> list[dict]:
    """Walk a dotted path into payload and return a list of row-dicts.

    Returns [] if any intermediate key is missing or the final value isn't a list.
    """
    node: Any = payload
    for part in dotted_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return []
        node = node[part]
    if not isinstance(node, list):
        return []
    # Filter to dict-shaped rows only (defensive; payload should already be clean)
    return [r for r in node if isinstance(r, dict)]


def _sorted_headers(rows: list[dict]) -> list[str]:
    """Return CSV header columns: 'date' first (if present), then the rest sorted."""
    keys: set[str] = set()
    for r in rows:
        keys.update(r.keys())
    if not keys:
        return []
    rest = sorted(k for k in keys if k != "date")
    return (["date"] if "date" in keys else []) + rest


def _challenge() -> Response:
    return Response(
        "Authentication required.",
        401,
        {"WWW-Authenticate": 'Basic realm="ETF Dashboard"'},
    )


def _share_token_from_request() -> str | None:
    """Extract a share token from the path /share/<token>(/...) or ?share=<t>."""
    p = request.path
    if p == "/share" or p.startswith("/share/"):
        rest = p[len("/share/"):] if p.startswith("/share/") else ""
        tok = rest.split("/", 1)[0] if rest else ""
        if tok:
            return tok
    return request.args.get("share")


@flask_app.before_request
def _require_auth():
    if not AUTH_ENABLED:
        return None  # auth off, anything goes (legacy)
    if request.method == "OPTIONS":
        return None  # CORS preflight
    if request.path in _AUTH_BYPASS:
        return None
    # The /share/<token> GET route is always allowed through auth — the route
    # handler itself returns a friendly 410 page for invalid tokens. Without
    # this, recipients of a stale text link would see a browser auth prompt
    # instead of a clear "this link expired" message.
    if request.method == "GET" and request.path.startswith("/share/"):
        return None
    # Share-token bypass: valid tokens may hit a small read-only allowlist
    # (the data endpoint and chat). Anything else falls through to Basic Auth.
    tok = _share_token_from_request()
    if tok and shares.is_valid(tok):
        if request.path in _SHARE_ALLOWED:
            return None
    auth = request.authorization
    if auth and auth.username and auth.password and DASH_USER and DASH_PASS:
        user_ok = secrets.compare_digest(str(auth.username), str(DASH_USER))
        pass_ok = secrets.compare_digest(str(auth.password), str(DASH_PASS))
        if user_ok and pass_ok:
            return None
    return _challenge()


@flask_app.after_request
def _cors(resp):
    """Allow the bookmarklet on third-party pages (Farside, SoSoValue) to POST CSVs here."""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


@flask_app.route("/api/upload-csv", methods=["OPTIONS"])
def _upload_csv_preflight():
    return ("", 204)

# Track last successful fetch + any fetch lock
_state = {
    "last_fetch_at": None,
    "last_fetch_error": None,
    "fetching": False,
    "lock": threading.Lock(),
}


def _do_fetch() -> tuple[bool, str | None]:
    """Run market + whale fetch. Returns (ok, error_msg)."""
    if _state["fetching"]:
        return False, "already fetching"
    with _state["lock"]:
        _state["fetching"] = True
        try:
            fetch_market.fetch_all()
            _state["last_fetch_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _state["last_fetch_error"] = None
            return True, None
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            _state["last_fetch_error"] = err
            return False, err
        finally:
            _state["fetching"] = False


def _refresher():
    """Background daemon: periodic fetch."""
    if REFRESH_MINUTES <= 0:
        return
    interval = REFRESH_MINUTES * 60
    while True:
        time.sleep(interval)
        ok, err = _do_fetch()
        ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        if ok:
            print(f"[{ts}] auto-refresh ok", flush=True)
        else:
            print(f"[{ts}] auto-refresh FAILED: {err}", flush=True)


def _expired_share_page() -> Response:
    body = """<!doctype html>
<html><head><meta charset="utf-8"><title>Share link expired</title>
<style>
  body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#0b0d12;color:#e6e8ee;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px}
  .box{max-width:480px;text-align:center;border:1px solid #1f2937;border-radius:12px;padding:32px;background:#10151f}
  h1{margin:0 0 12px;font-size:20px}
  p{margin:8px 0;color:#8a93a6}
</style></head><body>
<div class="box">
  <h1>Share link expired or revoked</h1>
  <p>This link is no longer active. Ask whoever sent it to mint a fresh one.</p>
</div></body></html>"""
    return Response(body, status=410, mimetype="text/html")


@flask_app.route("/")
def index() -> Response:
    tok = _share_token_from_request()
    share_token = tok if (tok and shares.is_valid(tok)) else None
    payload = dash.build_payload()
    payload["server"] = {
        "last_fetch_at": _state["last_fetch_at"],
        "auto_refresh_minutes": REFRESH_MINUTES,
    }
    html = dash.render_html(payload, share_token=share_token)
    return Response(html, mimetype="text/html")


@flask_app.route("/share/<token>")
def share_view(token: str) -> Response:
    """Public dashboard view for a share token. Bypasses Basic Auth.

    Behaviour:
      * Valid + unexpired token → serve the dashboard with a small read-only
        banner and write-actions hidden client-side.
      * Unknown or expired token → 410 Gone + "share expired" page.
    """
    if not shares.is_valid(token):
        return _expired_share_page()
    payload = dash.build_payload()
    payload["server"] = {
        "last_fetch_at": _state["last_fetch_at"],
        "auto_refresh_minutes": REFRESH_MINUTES,
    }
    entry = shares.get(token) or {}
    payload["share"] = {
        "token": token,
        "expires_at": entry.get("expires_at"),
        "label": entry.get("label", ""),
    }
    html = dash.render_html(payload, share_token=token)
    return Response(html, mimetype="text/html")


@flask_app.route("/api/data")
def api_data() -> Response:
    payload = dash.build_payload()
    payload["server"] = {
        "last_fetch_at": _state["last_fetch_at"],
        "auto_refresh_minutes": REFRESH_MINUTES,
    }
    return jsonify(payload)


@flask_app.route("/api/export/csv")
def api_export_csv() -> Response:
    """Export a single time-series from the payload as a CSV download.

    Query:
        series   dotted path into the payload (must be in `_ALLOWED_SERIES`)
        from     optional ISO date (YYYY-MM-DD), inclusive lower bound
        to       optional ISO date (YYYY-MM-DD), inclusive upper bound

    Empty / missing series still returns a valid CSV (header row only),
    so callers can build URLs without 404-handling for sparse data.
    """
    series = (request.args.get("series") or "").strip()
    if not series:
        return jsonify({"ok": False, "error": "series not in allowlist"}), 400
    if series not in _ALLOWED_SERIES:
        return jsonify({"ok": False, "error": "series not in allowlist"}), 400

    date_from = (request.args.get("from") or "").strip() or None
    date_to = (request.args.get("to") or "").strip() or None

    payload = dash.build_payload()
    rows = _resolve_series(payload, series)

    if date_from or date_to:
        rows = [
            r for r in rows
            if (not date_from or str(r.get("date", "")) >= date_from)
            and (not date_to or str(r.get("date", "")) <= date_to)
        ]

    # Header order: 'date' first, then the rest alphabetically. If the series is
    # empty, fall back to the canonical column set inferred from the path so the
    # caller still gets a useful header row.
    headers = _sorted_headers(rows) or _fallback_headers(series)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(headers)
    for r in rows:
        writer.writerow([r.get(h, "") for h in headers])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_filename = f"{series}_{today}.csv"
    # Strip anything outside [A-Za-z0-9_.-] so a hostile `series` cannot break
    # out of the quoted filename in Content-Disposition.
    filename = re.sub(r"[^A-Za-z0-9_.-]", "", raw_filename)
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _fallback_headers(series: str) -> list[str]:
    """Canonical column order for known series when no data rows are present."""
    if series in ("btc.daily", "eth.daily"):
        return ["date", "flow", "cumulative"]
    if series in ("market.btc.funding", "market.eth.funding"):
        return ["date", "rate"]
    if series in ("market.btc.dvol", "market.eth.dvol"):
        return ["date", "dvol"]
    if series == "market.fear_greed":
        return ["date", "label", "value"]
    # Everything else (prices, FRED series, whale metrics) is date+value.
    return ["date", "value"]


@flask_app.route("/api/share", methods=["GET", "POST"])
def api_share() -> Response:
    """Create or list share tokens. Always requires Basic Auth (the
    `_require_auth` check earlier rejects share-token holders from /api/share*).
    """
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            days = float(body.get("days", 3))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "days must be a number"}), 400
        if days <= 0 or days > 30:
            return jsonify({"ok": False, "error": "days must be between 0 and 30"}), 400
        label = (body.get("label") or "")[:120]
        try:
            entry = shares.create(days=days, label=label, created_by=DASH_USER or "")
        except Exception as e:
            return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500
        return jsonify({"ok": True, "share": entry})
    # GET: list active shares
    shares.prune_expired()
    return jsonify({"ok": True, "shares": shares.list_all()})


@flask_app.route("/api/share/<token>", methods=["DELETE"])
def api_share_revoke(token: str) -> Response:
    removed = shares.revoke(token)
    return jsonify({"ok": True, "removed": removed})


@flask_app.route("/api/refresh", methods=["POST", "GET"])
def api_refresh() -> Response:
    """Trigger a fresh fetch_all() in the background.

    Used to be synchronous (block until fetch_all completed) but a full
    fetch takes ~60s — longer than Safari's default fetch timeout — so the
    UI would show "refresh failed" even though the underlying fetch was
    still running and would eventually succeed. Now: kick off a thread,
    return immediately with `{ok, in_progress: true}`. The browser's
    existing 60-second polling of `/api/data` picks up the fresh payload
    when the background fetch finishes.

    If a fetch is already running, returns the same in_progress shape
    rather than queueing a duplicate.
    """
    if _state["fetching"]:
        return jsonify({"ok": True, "in_progress": True, "status": "already running"})
    # Spawn the fetch on a daemon thread so the response can return now.
    t = threading.Thread(target=_do_fetch, daemon=True, name="manual-refresh")
    t.start()
    return jsonify({
        "ok": True,
        "in_progress": True,
        "status": "kicked off",
        "last_fetch_at": _state["last_fetch_at"],
        "estimated_seconds": 60,
    })


@flask_app.route("/api/seed-etf", methods=["POST"])
def api_seed_etf() -> Response:
    """Pull BTC ETF flows from the community GitHub mirror.

    Mirror is community-maintained and may be stale. Total column only.
    """
    try:
        n = fetch_live.fetch_btc_from_github_mirror(dash.DATA_DIR)
        return jsonify({"ok": True, "rows": n, "source": "canadiancode/btc-etf-flows"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 503


@flask_app.route("/api/upload-csv", methods=["POST"])
def api_upload_csv() -> Response:
    """Accept a pasted CSV body and persist it to data/{asset}_flows.csv.

    Body: text/csv or text/plain
    Query: ?asset=btc|eth (default btc)
    Validates that the first line contains 'date' and at least one numeric column.
    """
    asset = (request.args.get("asset") or "btc").lower()
    if asset not in ("btc", "eth"):
        return jsonify({"ok": False, "error": "asset must be btc or eth"}), 400
    body = request.get_data(as_text=True) or ""
    body = body.strip()
    if not body:
        return jsonify({"ok": False, "error": "empty body"}), 400

    # Auto-detect Farside's column-major paste (each cell on its own line)
    try:
        import parse_farside
        if parse_farside.looks_like_vertical_farside(body):
            body = parse_farside.parse_farside_vertical(body, asset_hint=asset)
    except Exception as e:
        return jsonify({"ok": False, "error": f"farside parse: {e}"}), 400

    # Allow tab-separated (typical browser copy-paste from Farside)
    sample = body.splitlines()[0]
    if "\t" in sample and "," not in sample:
        body = "\n".join(line.replace("\t", ",") for line in body.splitlines())

    head = body.splitlines()[0].lower()
    if "date" not in head:
        return jsonify({"ok": False, "error": "first line must contain a 'date' column"}), 400

    path = dash.DATA_DIR / f"{asset}_flows.csv"
    path.write_text(body + ("\n" if not body.endswith("\n") else ""))
    rows = max(0, len(body.splitlines()) - 1)
    return jsonify({"ok": True, "rows": rows, "path": str(path.name)})


BOOKMARKLET_JS = """
(async () => {
  const PORT = 8765;
  const ENDPOINT = `http://127.0.0.1:${PORT}/api/upload-csv`;
  const AUTH = __AUTH_HEADER__;  // populated server-side when DASH_USER/DASH_PASS set
  const tables = document.querySelectorAll('table');
  if (!tables.length) { alert('No <table> on this page.'); return; }
  let best = null, bestRows = 0;
  for (const t of tables) {
    const n = t.querySelectorAll('tr').length;
    const text = (t.innerText || '').slice(0, 800);
    const isFlow = /\\b(IBIT|FBTC|GBTC|ETHA|FETH|ETHE|ETHW)\\b/.test(text) && /\\bTotal\\b/i.test(text);
    if (isFlow && n > bestRows) { best = t; bestRows = n; }
  }
  if (!best) { alert('No ETF flow table found on this page.'); return; }
  const rows = best.querySelectorAll('tr');
  const lines = [];
  for (const r of rows) {
    const cells = r.querySelectorAll('th, td');
    const out = [];
    for (const c of cells) {
      let t = (c.innerText || '').trim().replace(/\\u00a0/g,' ').replace(/,(?=\\d{3}\\b)/g,'');
      if (/^\\(.+\\)$/.test(t)) t = '-' + t.slice(1, -1);
      if (t === '-' || t === '\\u2014') t = '0';
      t = t.replace(/[^-0-9.A-Za-z _:/]/g,'');
      out.push(t);
    }
    lines.push(out.join(','));
  }
  const csv = lines.join('\\n');
  const url = location.href.toLowerCase();
  const tableText = (best.innerText || '').toUpperCase();
  let asset = 'btc';
  if (url.includes('ethereum') || /\\b(ETHA|FETH|ETHE|ETHW)\\b/.test(tableText)) asset = 'eth';
  if (!confirm(`Send ${lines.length-1} rows to ${asset.toUpperCase()} dashboard?\\nFirst line: ${lines[0].slice(0,80)}\\nLast line:  ${lines[lines.length-1].slice(0,80)}`)) return;
  try {
    const headers = { 'Content-Type': 'text/plain' };
    if (AUTH) headers['Authorization'] = AUTH;
    const resp = await fetch(`${ENDPOINT}?asset=${asset}`, {
      method: 'POST',
      headers,
      body: csv,
    });
    const j = await resp.json();
    if (j.ok) alert(`Imported ${j.rows} ${asset.toUpperCase()} rows. Reload http://127.0.0.1:${PORT}/`);
    else alert(`Upload failed: ${j.error || 'unknown error'}`);
  } catch (e) {
    alert(`Network error: ${e.message}\\nIs the dashboard server running?`);
  }
})();
""".strip()


@flask_app.route("/bookmarklet")
def bookmarklet_page() -> Response:
    import base64
    import urllib.parse
    # If auth is enabled, embed the user's credentials in the bookmarklet
    # so cross-origin POSTs from farside.co.uk can pass auth.
    if AUTH_ENABLED:
        token = base64.b64encode(f"{DASH_USER}:{DASH_PASS}".encode()).decode()
        auth_literal = '"Basic ' + token + '"'
    else:
        auth_literal = "null"
    js_src = BOOKMARKLET_JS.replace("__AUTH_HEADER__", auth_literal)
    minified = " ".join(js_src.split())
    href = "javascript:" + urllib.parse.quote(minified, safe="(){};,:='\"$.+_-/?&* []!@#%^&|<>")
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>ETF flow bookmarklet</title>
<style>
  body{font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:720px;margin:40px auto;padding:0 20px;background:#0b0d12;color:#e6e8ee}
  a.bm{display:inline-block;background:#f7931a;color:#000;padding:10px 18px;border-radius:8px;text-decoration:none;font-weight:600;border:1px solid #f7931a}
  a.bm:hover{background:#ffaa3a}
  code{background:#1b2030;padding:2px 6px;border-radius:4px;font:13px monospace}
  pre{background:#10151f;padding:14px;border-radius:8px;overflow:auto;font:12px monospace;max-height:240px}
  .step{margin:18px 0}
  h1{font-size:22px;margin:0 0 6px} h2{font-size:15px;margin-top:28px}
  .muted{color:#8a93a6;font-size:13px}
</style></head><body>
<h1>ETF Flow Importer Bookmarklet</h1>
<p class="muted">Drag the orange button to your bookmarks bar. Then visit Farside (or any page with a spot ETF flow table), click the bookmark, and the data flows straight into your local dashboard.</p>

<div class="step">
  <strong>1.</strong> Drag this to your bookmarks bar &rarr;
  &nbsp;&nbsp;<a class="bm" href="__BM_HREF__">↳ Send ETF Flow</a>
</div>

<div class="step">
  <strong>2.</strong> Make sure the dashboard server is running:
  <code>cd ~/btc-eth-etf-dashboard &amp;&amp; .venv/bin/python server.py</code>
</div>

<div class="step">
  <strong>3.</strong> Open one of these pages:
  <ul>
    <li><a href="https://farside.co.uk/bitcoin-etf-flow-all-data/" target="_blank">farside.co.uk/bitcoin-etf-flow-all-data/</a> &nbsp;<span class="muted">(BTC)</span></li>
    <li><a href="https://farside.co.uk/ethereum-etf-flow-all-data/" target="_blank">farside.co.uk/ethereum-etf-flow-all-data/</a> &nbsp;<span class="muted">(ETH)</span></li>
  </ul>
</div>

<div class="step">
  <strong>4.</strong> Click the bookmark. You'll see a confirmation dialog showing row count and the first/last rows; click OK to import.
</div>

<div class="step">
  <strong>5.</strong> Reload <a href="/">http://127.0.0.1:8765/</a> &mdash; the ETF Flows tab will reflect the new data.
</div>

<h2>What the bookmarklet does</h2>
<p class="muted">It scans the page for the largest <code>&lt;table&gt;</code> containing recognized ETF tickers (IBIT, FBTC, ETHA, FETH&hellip;), serialises it to CSV (with parens-negatives and dash-blanks normalised), guesses BTC vs ETH from the URL/contents, and POSTs to <code>/api/upload-csv</code>. The server's auto-detect parser handles either Farside layout. No data leaves your machine.</p>

<h2>Source</h2>
<pre>__BM_SRC__</pre>
</body></html>"""
    html = html.replace("__BM_HREF__", href)
    html = html.replace("__BM_SRC__", BOOKMARKLET_JS.replace("<", "&lt;"))
    return Response(html, mimetype="text/html")


@flask_app.route("/api/chat", methods=["POST"])
def api_chat() -> Response:
    """Stream a Claude answer over Server-Sent Events.

    Body: {"question": "..."}.
    Streams 'data: {"text": "..."}\\n\\n' chunks, ends with 'data: [DONE]'.
    """
    try:
        body = request.get_json(silent=True) or {}
        question = (body.get("question") or "").strip()
        if not question:
            return jsonify({"ok": False, "error": "empty question"}), 400

        import chat as chat_mod
        payload = dash.build_payload()
        out_of_scope_warning = chat_mod.is_out_of_scope(question)
        configured = chat_mod.is_configured()
        mcp_meta = chat_mod.mcp_status()

        def gen():
            try:
                # Emit a small meta frame up front so the client can flip
                # any "social tools active" UI before the first text chunk
                # arrives. Backwards-compatible: clients that ignore unknown
                # keys see no behaviour change.
                yield 'data: ' + json.dumps({"meta": {
                    "llm_configured": configured,
                    "mcp_available": mcp_meta["mcp_available"],
                    "mcp_servers": mcp_meta["servers"],
                }}) + '\n\n'
                if not configured:
                    # No API key — return rule-based fallback answer
                    text = chat_mod.fallback_answer(question, payload)
                    scope_note = ("Note: I won't make explicit buy/sell calls or price "
                                  "predictions, but here's what the indicators currently say.\n\n"
                                  if out_of_scope_warning else "")
                    notice = "(LLM offline — using rule-based fallback. Set ANTHROPIC_API_KEY to enable Claude.)\n\n"
                    full = scope_note + notice + text
                    # Emit in chunks of ~50 chars to feel like streaming
                    step = 50
                    for i in range(0, len(full), step):
                        yield 'data: ' + json.dumps({"text": full[i:i+step]}) + '\n\n'
                    yield 'data: [DONE]\n\n'
                    return
                if out_of_scope_warning:
                    yield 'data: ' + json.dumps({"text":
                        "Note: I won't make explicit buy/sell calls or price predictions, "
                        "but here's what the indicators currently say.\n\n"}) + '\n\n'
                for chunk in chat_mod.stream_answer(question, payload):
                    yield 'data: ' + json.dumps({"text": chunk}) + '\n\n'
                yield 'data: [DONE]\n\n'
            except Exception as e:
                yield 'data: ' + json.dumps({"error": f"{type(e).__name__}: {e}"}) + '\n\n'
                yield 'data: [DONE]\n\n'

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    except Exception as e:
        return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


@flask_app.route("/healthz")
def health() -> Response:
    return jsonify({
        "ok": True,
        "last_fetch_at": _state["last_fetch_at"],
        "fetching": _state["fetching"],
        "last_fetch_error": _state["last_fetch_error"],
    })


def main() -> int:
    # Drop any expired share tokens left over from previous runs.
    pruned = shares.prune_expired()
    if pruned:
        print(f"  pruned {pruned} expired share token(s)", flush=True)
    # Kick off background refresher (skips re-fetch if data is already fresh).
    t = threading.Thread(target=_refresher, daemon=True, name="refresher")
    t.start()
    print(f"Dashboard live on http://{HOST}:{PORT}/", flush=True)
    print(f"  auto-refresh every {REFRESH_MINUTES} min" + (" (disabled)" if REFRESH_MINUTES == 0 else ""), flush=True)
    flask_app.run(host=HOST, port=PORT, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
