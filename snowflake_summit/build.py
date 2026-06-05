#!/usr/bin/env python3
"""Snowflake Summit 2026 — Partner Scouting dashboard builder.

Reads vendors.json (the Master Directory transcribed from Bryan's Snowflake
Summit 2026 partner scouting workbook) and writes a self-contained dashboard.html
with KPIs, charts, a ranked/filterable partner table, and "Must-See" highlights.

The scoring shown is Bryan's own directional scoring from the workbook:
  Snowflake relevance / AI relevance / Retail-customer / IPO-upside / Bryan-fit
  -> blended Overall Score (0-10) and a Priority Tier (A / B / C).

Usage:
    python build.py                 # uses ./vendors.json -> ./dashboard.html
    python build.py my_export.json  # use a different file (same schema)
"""
import json
import sys
import os
import hashlib
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

SCORE_KEYS = [
    ("bryan_score", "Bryan Rec"),
    ("snowflake_score", "Snowflake"),
    ("ai_score", "AI"),
    ("retail_score", "Retail/Cust"),
    ("ipo_score", "IPO/Upside"),
]


def load(path):
    with open(path) as f:
        data = json.load(f)
    return data.get("_meta", {}), data.get("vendors", data if isinstance(data, list) else [])


def rank(vendors):
    tier_rank = {"A": 0, "B": 1, "C": 2, "D": 3}
    vendors.sort(key=lambda v: (
        tier_rank.get(v.get("tier"), 9),
        -(v.get("overall_score") or 0),
        -(v.get("bryan_score") or 0),
    ))
    for i, v in enumerate(vendors, 1):
        v["rank"] = i
    # Hidden gems: strong overall (>=7) but NOT already a Tier-A must-see.
    for v in vendors:
        v["hidden_gem"] = (v.get("overall_score") or 0) >= 7.0 and v.get("tier") != "A"
    return vendors


def is_public(v):
    return "public" in (v.get("company_type") or "").lower()


def avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else 0


def kpis(vendors):
    n = len(vendors)
    a = sum(1 for v in vendors if v.get("tier") == "A")
    b = sum(1 for v in vendors if v.get("tier") == "B")
    pub = sum(1 for v in vendors if is_public(v))
    cats = {v.get("category") for v in vendors}
    return [
        {"label": "Partner Vendors", "value": n, "sub": f"{len(cats)} categories", "filter": "all"},
        {"label": "Must-See (Tier A)", "value": a, "sub": "top priority", "filter": "tierA"},
        {"label": "Priority (Tier A+B)", "value": a + b, "sub": f"{round(100*(a+b)/n) if n else 0}% of floor", "filter": "tierAB"},
        {"label": "Avg Overall Score", "value": avg([v.get("overall_score") for v in vendors]), "sub": "out of 10"},
        {"label": "Avg AI Score", "value": avg([v.get("ai_score") for v in vendors]), "sub": "the Summit's hot theme"},
        {"label": "Public Companies", "value": pub, "sub": f"{n-pub} private/other", "filter": "public", "subFilter": "private"},
    ]


def aggregate_count(vendors, key):
    out = {}
    for v in vendors:
        k = v.get(key) or "—"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _jitter(name, salt):
    """Deterministic ±0.2 jitter (md5 of name) so dots on the coarse score grid
    don't overplot, while the build stays byte-reproducible."""
    h = int(hashlib.md5((str(name) + salt).encode("utf-8")).hexdigest(), 16)
    return ((h % 1000) / 1000.0 - 0.5) * 0.4


def magic_quadrant(vendors):
    """Gartner-style Magic Quadrant from the 0-10 scores. Ability to Execute (Y)
    = mean(Snowflake, Retail/Customer); Completeness of Vision (X) = mean(AI,
    IPO/Upside). Cross at the cohort MEAN of each axis. mq_x/mq_y carry jitter
    for plotting; mq_execute/mq_vision keep the true values for the tooltip."""
    for v in vendors:
        ex = avg([v.get("snowflake_score"), v.get("retail_score")])
        vi = avg([v.get("ai_score"), v.get("ipo_score")])
        v["mq_execute"] = ex
        v["mq_vision"] = vi
    tx = round(sum(v["mq_vision"] for v in vendors) / len(vendors), 2) if vendors else 5.0
    ty = round(sum(v["mq_execute"] for v in vendors) / len(vendors), 2) if vendors else 5.0

    # Jitter the plot coords off the coarse score grid, but clamp each dot to stay
    # on its TRUE quadrant side of the cross so a near-cross dot never crosses the
    # line and looks misclassified relative to its tier colour / tooltip quadrant.
    def _plot(true, cross, salt, name):
        p = true + _jitter(name, salt)
        p = max(cross + 0.03, p) if true >= cross else min(cross - 0.03, p)
        return max(0.0, min(10.0, round(p, 3)))

    for v in vendors:
        v["mq_x"] = _plot(v["mq_vision"], tx, "x", v.get("name"))
        v["mq_y"] = _plot(v["mq_execute"], ty, "y", v.get("name"))
    counts = {"Leaders": 0, "Challengers": 0, "Visionaries": 0, "Niche Players": 0}
    for v in vendors:
        hi_e = v["mq_execute"] >= ty
        hi_v = v["mq_vision"] >= tx
        q = ("Leaders" if (hi_e and hi_v) else "Challengers" if hi_e
             else "Visionaries" if hi_v else "Niche Players")
        v["mq_quadrant"] = q
        counts[q] += 1
    return {"tx": tx, "ty": ty, "counts": counts}


def magic_quadrant_segments(vendors, min_n=5, var_floor=1.0):
    """Per-niche Magic Quadrant metadata for the drill-down, keyed on the
    workbook's own `niche` taxonomy. Niches below min_n fold into 'Other' so the
    selector has no degenerate quadrants. Each niche gets its OWN cohort-mean
    cross (segment-relative). `drillable` is false when the cohort is too flat
    (var < var_floor) — e.g. a large template-scored niche bucket whose scores
    give ~0 variance — so the UI caveats it as a ranked list."""
    import statistics as _st
    from collections import Counter
    for v in vendors:
        v["mq_segment"] = v.get("niche") or "Other"
    sizes = Counter(v["mq_segment"] for v in vendors)
    for v in vendors:
        if sizes[v["mq_segment"]] < min_n:
            v["mq_segment"] = "Other"
    groups = {}
    for v in vendors:
        groups.setdefault(v["mq_segment"], []).append(v)
    out = []
    for label, vs in groups.items():
        n = len(vs)
        exs = [v["mq_execute"] for v in vs]
        vis = [v["mq_vision"] for v in vs]
        tx = round(sum(vis) / n, 2)
        ty = round(sum(exs) / n, 2)
        var = round(_st.pstdev(exs) + _st.pstdev(vis), 2) if n > 1 else 0.0
        counts = {"Leaders": 0, "Challengers": 0, "Visionaries": 0, "Niche Players": 0}
        for v in vs:
            hi_e = v["mq_execute"] >= ty
            hi_v = v["mq_vision"] >= tx
            counts["Leaders" if (hi_e and hi_v) else "Challengers" if hi_e
                   else "Visionaries" if hi_v else "Niche Players"] += 1
        out.append({
            "label": label, "n": n, "tx": tx, "ty": ty, "var": var,
            "skew": round(max(counts.values()) / n, 2),
            "drillable": bool(n >= min_n and var >= var_floor),
            "counts": counts, "tierA": sum(1 for v in vs if v.get("tier") == "A"),
        })
    out.sort(key=lambda s: (-s["n"], s["label"]))
    return out


def load_news():
    """Load the partner Summit-news feed (news.json) if present.

    Sidecar produced by the research swarm (and, later, a news cron). Shape is
    either a bare list of items or {"generated": "...", "items": [...]}. Always
    returns {"items": [...], "generated": "..."} and never raises — a missing or
    garbled file just yields an empty feed so the dashboard still builds."""
    p = os.path.join(HERE, "news.json")
    try:
        with open(p) as f:
            data = json.load(f)
    except Exception:
        return {"items": [], "generated": ""}
    if isinstance(data, list):
        return {"items": data, "generated": ""}
    items = data.get("items", [])
    return {"items": items if isinstance(items, list) else [], "generated": data.get("generated", "")}


def load_enrichment():
    """Load Wikidata vendor facts (enrichment.json) if present. Produced by
    enrich_vendors.py. Returns {vendor_name: {founded, headquarters, employees,
    industry, website, wikidata_url, ...}}; never raises."""
    p = os.path.join(HERE, "enrichment.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        by = data.get("by_vendor", {}) if isinstance(data, dict) else {}
        return by if isinstance(by, dict) else {}
    except Exception:
        return {}


def render(meta, vendors, src_path):
    mq = magic_quadrant(vendors)
    mq_segments = magic_quadrant_segments(vendors)
    by_cat = aggregate_count(vendors, "category")
    by_niche = aggregate_count(vendors, "niche")
    by_tier = {t: sum(1 for v in vendors if v.get("tier") == t) for t in ["A", "B", "C", "D"]}
    by_tier = {t: c for t, c in by_tier.items() if c}

    # average score profile across the five dimensions, Tier A vs all
    def profile(subset):
        return [avg([v.get(k) for v in subset]) for k, _ in SCORE_KEYS]
    tierA = [v for v in vendors if v.get("tier") == "A"]
    _news = load_news()
    # Merge Wikidata company facts onto each vendor (founded / HQ / industry /
    # employees). Backfill the existing Employees field when the directory
    # didn't have one. Missing enrichment.json is a no-op.
    _enrich = load_enrichment()
    for _v in vendors:
        _wd = _enrich.get(_v.get("name")) or {}
        if _wd:
            _v["wd"] = _wd
            if not _v.get("employees") and _wd.get("employees"):
                _v["employees"] = _wd["employees"]

    payload = {
        "meta": meta,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": os.path.basename(src_path),
        "score_labels": [lbl for _, lbl in SCORE_KEYS],
        "score_keys": [k for k, _ in SCORE_KEYS],
        "kpis": kpis(vendors),
        "by_cat": by_cat,
        "by_niche": by_niche,
        "by_tier": by_tier,
        "profile_all": profile(vendors),
        "profile_a": profile(tierA),
        "top15": vendors[:15],
        "must_see": tierA,
        "gems": [v for v in vendors if v["hidden_gem"]][:6],
        "best_fit": sorted(vendors, key=lambda v: -(v.get("bryan_score") or 0))[:6],
        "mq": mq,
        "mq_segments": mq_segments,
        "vendors": vendors,
        "news": _news["items"],
        "news_generated": _news["generated"],
    }
    # Embed as a JS object literal at /*__DATA__*/. json.dumps does NOT escape
    # "</script>", U+2028 or U+2029, any of which would break out of the inline
    # <script> at HTML-parse time — so neutralise them. "<\/" is identical to
    # "</" once the JS string is parsed, so the data round-trips unchanged.
    data_json = (json.dumps(payload, ensure_ascii=False)
                 .replace("</", "<\\/")
                 .replace("\u2028", "\\u2028")
                 .replace("\u2029", "\\u2029"))
    html = HTML_TEMPLATE.replace("/*__DATA__*/", data_json)
    # Inline Chart.js so the page is fully self-contained (no CDN dependency) —
    # works offline and for any recipient regardless of their network policy.
    chartjs_path = os.path.join(HERE, "vendor", "chart.umd.js")
    if os.path.exists(chartjs_path):
        with open(chartjs_path) as cf:
            chartjs = cf.read()
        html = html.replace(
            '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>',
            "<script>\n/* Chart.js 4.4.1 (vendored, MIT) */\n" + chartjs + "\n</script>",
        )
    # Inline the compressed Basecamp floor-map photo (built artifact) as a data
    # URI so the Floor Map view's reference image is self-contained.
    floor_path = os.path.join(HERE, "floor_basecamp.jpg")
    floor_uri = ""
    if os.path.exists(floor_path):
        import base64
        with open(floor_path, "rb") as ff:
            floor_uri = "data:image/jpeg;base64," + base64.b64encode(ff.read()).decode("ascii")
    html = html.replace("__FLOOR_IMG_SRC__", floor_uri)
    # Inline the floor-plan layout spec (built artifact) so the spatial map renders offline.
    fp_path = os.path.join(HERE, "floorplan.json")
    fp_json = '{"regions":[]}'
    if os.path.exists(fp_path):
        with open(fp_path) as fp:
            fp_json = fp.read().replace("</", "<\\/")
    html = html.replace("/*__FLOORPLAN__*/", fp_json)
    out_path = os.path.join(HERE, "dashboard.html")
    with open(out_path, "w") as f:
        f.write(html)
    return out_path


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
<title>Snowflake Summit 2026 — Partner Scouting Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root{
    --bg:#0b1020; --panel:#121a30; --panel2:#172241; --border:#243352;
    --text:#e8eeff; --muted:#8da2c8; --accent:#29b5e8; --accent2:#11567f;
    --A:#34d399; --B:#fbbf24; --C:#64748b; --gem:#a78bfa; --fit:#f472b6;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
       background:linear-gradient(180deg,#0b1020,#0d1426);color:var(--text)}
  header{padding:24px 28px 16px;border-bottom:1px solid var(--border);background:linear-gradient(120deg,#0e1730,#10243f)}
  .brand{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .navbtns{margin-left:auto;display:flex;gap:8px;flex-wrap:wrap;align-items:center;justify-content:flex-end}
  .infobtn{background:transparent;border:1px solid var(--border);color:var(--muted);border-radius:9px;padding:7px 11px;font-size:12px;cursor:pointer;font-family:inherit;white-space:nowrap}
  .infobtn:hover,.infobtn[aria-expanded=true]{border-color:var(--accent);color:#dff3ff}
  .scorepop{position:fixed;top:72px;right:18px;z-index:240;width:362px;max-width:calc(100vw - 32px);background:linear-gradient(165deg,#15233f,#0f1830);border:1px solid var(--accent2);border-radius:14px;box-shadow:0 22px 60px rgba(0,0,0,.6);padding:16px 18px 14px;color:var(--text)}
  .scorepop[hidden]{display:none}
  .scorepop .x{position:absolute;top:8px;right:12px;background:none;border:none;color:var(--muted);font-size:20px;line-height:1;cursor:pointer}
  .scorepop .x:hover{color:var(--text)}
  .scorepop h3{margin:0 0 8px;font-size:14.5px}
  .scorepop p{margin:8px 0;font-size:12px;color:var(--muted);line-height:1.55}
  .scorepop ul{margin:8px 0;padding-left:17px}
  .scorepop li{margin:5px 0;font-size:12px;color:#cfddf4;line-height:1.5}
  .scorepop b{color:var(--text)}
  .logo{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,#29b5e8,#1b7fb8);display:flex;align-items:center;justify-content:center;font-weight:800;color:#06121f}
  h1{font-size:24px;font-weight:800;margin:0;letter-spacing:.01em}
  .sub{color:var(--muted);font-size:12.5px;margin-top:4px}
  .dl{background:var(--accent2);border:1px solid var(--accent);color:#dff3ff;padding:8px 13px;border-radius:9px;font-size:12.5px;text-decoration:none;white-space:nowrap}
  /* Per-vendor homepage link (↗) rendered next to each partner name + in the detail sheet. */
  .homelink{display:inline-flex;align-items:center;justify-content:center;width:24px;height:24px;margin-left:2px;color:var(--muted);text-decoration:none;font-size:12px;font-weight:700;line-height:1;border-radius:5px;vertical-align:baseline;opacity:1;transition:color .15s,background .15s}
  .homelink:hover,.homelink:focus{color:var(--accent);background:rgba(41,181,232,.12)}
  .homelink:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
  .homedl{font-size:12.5px}
  @media(max-width:640px){.homelink{width:26px;height:26px;font-size:14px}}
  .dl:hover{background:#176a9c}
  .wrap{max-width:1320px;margin:0 auto;padding:20px 24px 60px}
  .kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:6px 0 24px}
  .kpi{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 15px}
  .kpi .v{font-size:23px;font-weight:800;color:var(--text);line-height:1}
  .kpi .l{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-top:8px}
  .kpi .s{font-size:11px;color:var(--accent);margin-top:3px}
  .kpi-click{cursor:pointer;transition:border-color .12s ease,transform .08s ease}
  .kpi-click:hover{border-color:var(--accent);transform:translateY(-1px)}
  .kpi-click:focus-within,.kpi-click:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  /* The main number/label is its own button on tiles that also have a sub-link
     (keeps the two controls as siblings, never nested). It fills the tile so the
     whole top area stays clickable. */
  .kpi-main-link{display:block;color:inherit;text-decoration:none;cursor:pointer}
  .kpi-main-link:focus{outline:none}
  .kpi-sub-link{display:inline-flex;align-items:center;min-height:24px;padding:5px 2px;color:inherit;text-decoration:none;border-bottom:1px dashed currentColor;cursor:pointer}
  .kpi-sub-link:hover{opacity:.8}
  @media(max-width:640px){#vModal .col-opt{display:none}
    /* The KPI pop-up table must fit the phone width — let the Partner name wrap
       so it can't force the Tier column off-screen behind a horizontal scroll.
       (Scoped to #vModal; the desktop main table keeps its nowrap.) */
    #vModal th,#vModal td{white-space:normal}
    #vModal td.name{word-break:break-word}}
  h3.sec{margin:22px 0 12px;font-size:15px}
  h3.sec .hint{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
  .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
  .card2{background:linear-gradient(160deg,#15233f,#101a30);border:1px solid var(--border);border-radius:12px;padding:13px 14px;position:relative;overflow:hidden}
  .card2 .rk{position:absolute;top:8px;right:11px;font-size:26px;font-weight:900;color:rgba(41,181,232,.16)}
  .card2 .nm{font-size:14.5px;font-weight:700;padding-right:30px}
  .card2 .ct{font-size:10.5px;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;margin:2px 0 7px}
  .scores{display:flex;gap:5px;flex-wrap:wrap;margin:7px 0 4px}
  .schip{font-size:10px;padding:2px 6px;border-radius:6px;background:var(--panel2);border:1px solid var(--border);color:var(--muted)}
  .schip b{color:var(--text)}
  .ovr{display:flex;align-items:baseline;gap:7px;margin-top:6px}
  .ovr b{font-size:22px;color:var(--text)}.ovr span{font-size:10.5px;color:var(--muted)}
  .tag{display:inline-block;font-size:10px;padding:2px 8px;border-radius:20px;font-weight:700}
  .tA{background:rgba(52,211,153,.16);color:var(--A)}.tB{background:rgba(251,191,36,.16);color:var(--B)}
  .tC{background:rgba(96,165,250,.18);color:#93c5fd}.tD{background:rgba(59,130,246,.2);color:#3b82f6}
  .tNi{background:rgba(41,181,232,.16);color:#7fd6f5;border:1px solid rgba(41,181,232,.3)}
  /* Print / Save-as-PDF: keep the dark theme (so the canvas charts render as on
     screen), hide interactive controls, and expand the table so the WHOLE
     dashboard lands in the PDF. print-color-adjust:exact makes browsers keep
     the panel/background colours. */
  @media print{
    @page{margin:9mm}
    html,body{background:#0b1020 !important;-webkit-print-color-adjust:exact;print-color-adjust:exact}
    html{zoom:.55 !important}
    .no-print,.dl,.homelink,.controls,#search,#mqBack,#mqSegSel{display:none !important}
    .wrap{width:1320px;max-width:none;padding:6px}
    .panel,.card2,.kpi,canvas,.nitem{break-inside:avoid;page-break-inside:avoid;-webkit-print-color-adjust:exact;print-color-adjust:exact}
    h3.sec{break-after:avoid;page-break-after:avoid}
    .scroll{max-height:none !important;overflow:visible !important}
    table{font-size:10px}
  }
  .panel{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px 18px}
  .grid{display:grid;grid-template-columns:1.25fr 1fr;gap:16px;margin-bottom:16px}
  .grid h4,.panel h4{margin:0 0 12px;font-size:13.5px;font-weight:700}
  canvas{max-height:300px}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:8px 9px;border-bottom:1px solid var(--border);white-space:nowrap}
  th{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;cursor:pointer;user-select:none;position:sticky;top:0;background:var(--panel)}
  th:hover{color:var(--text)}
  td.num{text-align:right;font-variant-numeric:tabular-nums}
  tbody tr:hover{background:var(--panel2)}
  td.name{font-weight:600}
  .scroll{max-height:560px;overflow:auto;border:1px solid var(--border);border-radius:10px}
  .gem-row td.name::after{content:" 💎";font-size:10px}
  .topbar{display:flex;gap:10px;align-items:center;margin:4px 0 18px;flex-wrap:wrap}
  .topbar .searchwrap{position:relative;flex:1;min-width:240px}
  .topbar .searchwrap::before{content:"🔎";position:absolute;left:13px;top:50%;transform:translateY(-50%);font-size:14px;opacity:.7}
  .topbar input{width:100%;background:var(--panel);border:1px solid var(--border);color:var(--text);
       border-radius:11px;padding:12px 14px 12px 38px;font-size:14px}
  .topbar input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px rgba(41,181,232,.18)}
  .topbar .hit{font-size:12px;color:var(--muted);white-space:nowrap}
  /* Search name-lookup typeahead dropdown */
  .sugbox{position:absolute;top:calc(100% + 5px);left:0;right:0;background:var(--panel2);border:1px solid var(--border);border-radius:9px;z-index:60;max-height:300px;overflow:auto;display:none;box-shadow:0 10px 28px rgba(0,0,0,.45)}
  .sugbox.on{display:block}
  .sug{padding:8px 12px;cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:10px;border-bottom:1px solid var(--border)}
  .sug:last-child{border-bottom:none}
  .sug:hover,.sug.act{background:var(--panel)}
  .sug .nm{font-weight:600;font-size:13px}
  .sug .meta{color:var(--muted);font-size:11px;white-space:nowrap}
  .controls{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:11px;align-items:center}
  .controls input,.controls select{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 10px;font-size:12.5px}
  /* In-table filter row: a dropdown aligned under each filterable column header.
     Static (not sticky) so it scrolls away while the column titles stick. */
  .filterrow th{position:static;background:var(--panel);padding:4px 6px;border-bottom:1px solid var(--border)}
  .filterrow select{width:100%;min-width:0;background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:5px 6px;font-size:11px}
  .ovrbar{display:inline-block;height:7px;border-radius:4px;vertical-align:middle;margin-right:7px}
  /* Summit News feed */
  .nitem{display:flex;gap:16px;padding:22px 24px;margin-bottom:14px;background:linear-gradient(160deg,#15233f,#101a30);border:1px solid var(--border);border-radius:12px}
  .nitem:last-child{margin-bottom:0}
  .nitem:hover{border-color:var(--accent);background:linear-gradient(160deg,#18294a,#111c34)}
  .nitem .nd{font-size:12px;color:var(--muted);white-space:nowrap;min-width:76px;font-variant-numeric:tabular-nums;padding-top:3px}
  .nitem .nbody{flex:1;min-width:0}
  .nitem .nh{font-size:17px;font-weight:600;line-height:1.45}
  .nitem .nh a{color:var(--text);text-decoration:none}
  .nitem .nh a:hover{color:var(--accent);text-decoration:underline}
  .nitem .nh a .ext{font-size:.82em;color:var(--accent);margin-left:4px;opacity:.75;text-decoration:none;font-weight:400}
  .nitem .nh a:hover .ext{opacity:1}
  /* News-only view (opened in its own window via ?view=news) + category buckets */
  #newsView,#mqView,#mapView{display:none}
  body.newsmode>header,body.newsmode #dashwrap,body.mqmode>header,body.mqmode #dashwrap,body.mapmode>header,body.mapmode #dashwrap{display:none}
  body.newsmode #newsView{display:block}
  body.mqmode #mqView{display:block}
  body.mapmode #mapView{display:block}
  /* ===== Interactive Basecamp floor map (?view=map) ===== */
  .mapwrap{max-width:1320px;margin:0 auto;padding:8px 24px 60px}
  .maplegend{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin:6px 0 14px;font-size:12px;color:var(--muted)}
  .maplegend .lg{display:inline-flex;align-items:center;gap:6px}
  .maplegend .sw{width:13px;height:13px;border-radius:4px;display:inline-block;border:1px solid var(--border)}
  .mapscroll{overflow-x:auto;border:1px solid var(--border);border-radius:14px;background:linear-gradient(180deg,#0e1730,#0c1426);padding:14px}
  .maprow{display:flex;gap:12px;align-items:flex-start;min-width:min-content}
  /* Always-visible horizontal scroll + slider for the wide Zone-columns map */
  #mapColsWrap{scrollbar-width:auto;scrollbar-color:var(--accent) var(--panel2)}
  #mapColsWrap::-webkit-scrollbar{height:14px}
  #mapColsWrap::-webkit-scrollbar-track{background:var(--panel2);border-radius:7px}
  #mapColsWrap::-webkit-scrollbar-thumb{background:var(--accent);border-radius:7px;border:3px solid var(--panel2)}
  #mapColsNav{margin:0 0 8px}
  .mapcols-hint{font-size:11px;color:var(--muted);text-align:center;letter-spacing:.04em;margin-bottom:4px}
  .mapcols-slider{width:100%;accent-color:var(--accent);cursor:grab}
  .zonecol{flex:0 0 auto;width:150px;display:flex;flex-direction:column;gap:7px}
  .zonehd{font-size:12px;font-weight:800;color:var(--accent);text-align:center;letter-spacing:.04em;padding:6px 4px 8px;border-bottom:2px solid var(--accent2)}
  .booth{border:1px solid var(--border);border-radius:8px;padding:7px 9px;background:var(--panel);cursor:pointer;transition:transform .08s ease,border-color .12s ease}
  .booth:hover,.booth:focus{transform:translateY(-1px);border-color:var(--accent);outline:none}
  .booth .bn{font-size:10px;color:var(--muted);font-variant-numeric:tabular-nums}
  .booth .bnm{font-size:12px;font-weight:700;color:var(--text);line-height:1.25;margin-top:1px}
  .booth .bt{font-size:9.5px;text-transform:uppercase;letter-spacing:.03em;margin-top:3px;font-weight:700}
  .booth.tA{border-left:4px solid var(--A)} .booth.tB{border-left:4px solid var(--B)} .booth.tC{border-left:4px solid var(--C)}
  .booth.must{box-shadow:0 0 0 1px var(--A) inset;background:linear-gradient(160deg,#13261f,#101a30)}
  .booth.space{background:linear-gradient(160deg,#102338,#0d1830);border-style:dashed;border-color:#2c5f86;cursor:default}
  .booth.space:hover{transform:none;border-color:#2c5f86}
  .booth.space .bnm{color:#7fd6f5}
  .booth.dim{opacity:.2}
  .mapfoot{font-size:11.5px;color:var(--muted);line-height:1.6;margin-top:14px}
  .mapfoot code{color:var(--accent)}
  .mapsearch{background:var(--panel);border:1px solid var(--border);color:var(--text);border-radius:10px;padding:9px 13px;font-size:13.5px;width:300px;max-width:100%}
  .mapsearch:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 2px rgba(41,181,232,.18)}
  .floorimg{margin-top:16px}
  .floorimg>summary{cursor:pointer;color:var(--accent);font-size:12px;letter-spacing:.04em}
  .floorimg img{width:100%;max-width:1200px;border:1px solid var(--border);border-radius:12px;margin-top:10px;display:block}
  @media(max-width:600px){.zonecol{width:130px}}
  /* ----- spatial floor-PLAN (toggle) ----- */
  .maptoggle{display:inline-flex;border:1px solid var(--border);border-radius:9px;overflow:hidden;margin:0 0 14px}
  .maptoggle button{background:var(--panel);border:none;color:var(--muted);font:inherit;font-size:12.5px;padding:8px 14px;cursor:pointer}
  .maptoggle button.on{background:var(--accent2);color:#dff3ff}
  .maptoggle button+button{border-left:1px solid var(--border)}
  .booth:focus-visible,.planbooth:focus-visible{outline:2px solid #cfe8ff;outline-offset:2px}
  .planbox{position:relative;width:100%;aspect-ratio:100/62;min-height:340px;background:linear-gradient(180deg,#0e1730,#0c1426);border:1px solid var(--border);border-radius:14px;overflow:hidden}
  .planregion{position:absolute;box-sizing:border-box;border-radius:5px;overflow:hidden}
  .planzone{border:1px dashed #2a3a5c;padding:11px 2px 3px}
  .planzone>.zl{position:absolute;top:1px;left:0;right:0;text-align:center;font-size:8px;font-weight:800;color:var(--accent);letter-spacing:.02em}
  .planzone .dots{display:flex;flex-wrap:wrap;gap:2px;justify-content:center;align-content:flex-start;height:100%;overflow:visible}
  .planbooth{width:16px;height:16px;border-radius:4px;cursor:pointer;border:1px solid rgba(0,0,0,.4)}
  .planbooth.must{width:22px;height:22px;border-radius:50%;box-shadow:0 0 0 2px #0b1020,0 0 0 4px var(--A)}
  .planbooth.space{cursor:default;border-radius:50%;width:12px;height:12px;opacity:.65}
  .planbooth.dim{opacity:.13}
  .planbooth:hover{transform:scale(1.55);z-index:5;position:relative}
  /* landmarks "framed out" — faint, recessive, so the booth dots dominate */
  .planmark{display:flex;align-items:center;justify-content:center;text-align:center;padding:2px;font-size:7.5px;line-height:1.05;color:#56688a;border:1px solid #1c2a44;background:rgba(20,30,54,.22);font-weight:600;opacity:.78}
  .planmark.k-theater{border-color:#274d6b;color:#7ba2bd}
  .planmark.k-lounge{border-color:#2a5544;color:#86bba2}
  .planmark.k-concourse{border-color:#43386a;color:#a397c6}
  .planmark.k-anchor{border-color:#2f486b;color:#93a9cf;font-weight:700;font-size:8px}
  .planmark.k-structure{border-color:#212d45;color:#52648a;font-weight:400}
  .plancontainer{border:1px solid #1e2c48;background:rgba(120,150,200,.012);pointer-events:none}
  .plancontainer>.cl{position:absolute;top:2px;left:5px;font-size:8px;color:#5d6f92;font-weight:700;letter-spacing:.03em}
  @media(max-width:640px){.planbox{aspect-ratio:auto;height:560px}.planmark{font-size:7px}}
  .bucketbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
  .bchip{background:var(--panel2);border:1px solid var(--border);color:var(--muted);border-radius:20px;padding:6px 12px;font-size:12px;font-family:inherit;line-height:1.3;cursor:pointer;user-select:none;white-space:nowrap}
  .bchip:hover{color:var(--text)}
  .bchip.on{background:var(--accent2);border-color:var(--accent);color:#dff3ff}
  .bchip .bc{opacity:.65;font-weight:400;margin-left:2px}
  /* A-/A+ text-zoom control */
  .zoomctl{display:inline-flex;align-items:center;gap:6px}
  .zbtn{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:7px;min-width:30px;height:30px;font-size:14px;font-weight:700;cursor:pointer;padding:0 6px}
  .zbtn:hover{border-color:var(--accent);color:#dff3ff}
  .zlevel{font-size:11px;color:var(--muted);min-width:36px;text-align:center;font-variant-numeric:tabular-nums}
  /* Gold embossed cursive signature — signs the foot of every view + the PDF */
  .signature{text-align:center;margin:38px auto 18px;padding-top:22px;border-top:1px solid var(--border);max-width:1320px}
  .sigrule{width:140px;height:1px;margin:0 auto 12px;background:linear-gradient(90deg,transparent,#d4af37,transparent)}
  .signame{font-family:"Snell Roundhand","Apple Chancery","Brush Script MT",cursive;font-style:italic;font-size:34px;line-height:1.1;
    background:linear-gradient(92deg,#9e7c2f,#f7e98e 28%,#e6c200 50%,#fdf5a6 72%,#b8860b);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;
    filter:drop-shadow(0 1px 1px rgba(0,0,0,.55)) drop-shadow(0 0 7px rgba(212,175,55,.20));letter-spacing:.5px}
  .sigsub{font-size:10px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);margin-top:8px}
  .note:empty{display:none}
  .scoredef{margin:16px auto 0;max-width:740px}
  .scoredef>summary{cursor:pointer;color:var(--accent);font-size:11px;letter-spacing:.08em;text-transform:uppercase;text-align:center;list-style:none}
  .scoredef>summary::-webkit-details-marker{display:none}
  .scoredef>summary:hover{text-decoration:underline}
  .scoredef[open]>summary{margin-bottom:4px}
  .scoredef-body{text-align:left;font-size:11.5px;color:var(--muted);line-height:1.6;margin-top:10px;padding-top:12px;border-top:1px solid var(--border)}
  .scoredef-body code{color:var(--accent)}
  @media print{.signame{-webkit-text-fill-color:#b8902f !important;filter:none}}
  .nitem .nmeta{font-size:12.5px;color:var(--muted);margin-top:7px;display:flex;gap:9px;align-items:center;flex-wrap:wrap}
  .nitem .nsum{font-size:14px;color:#c3d2ee;margin-top:8px;white-space:normal;line-height:1.6}
  .rdot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:middle;flex:none}
  .note{color:var(--muted);font-size:12px;margin-top:18px;line-height:1.55;border-top:1px solid var(--border);padding-top:14px}
  /* Vendor detail modal — click any card or table row */
  .vmodal{position:fixed;inset:0;background:rgba(6,12,24,.74);display:none;z-index:200;align-items:flex-start;justify-content:center;overflow:auto;padding:40px 16px}
  .vmodal.on{display:flex}
  .vsheet{background:linear-gradient(165deg,#15233f,#0f1830);border:1px solid var(--border);border-radius:16px;max-width:640px;width:100%;padding:22px 26px;position:relative;box-shadow:0 24px 60px rgba(0,0,0,.55)}
  .vsheet .x{position:absolute;top:12px;right:16px;cursor:pointer;font-size:22px;line-height:1;color:var(--muted);background:none;border:none}
  .vsheet .x:hover{color:var(--text)}
  .vsheet h2{margin:0;font-size:21px;display:inline}
  .vsec{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);margin:16px 0 2px;font-weight:700}
  .vrow{display:flex;gap:12px;padding:8px 0;border-bottom:1px solid var(--border);font-size:13px}
  .vrow:last-child{border-bottom:none}
  .vrow .k{color:var(--muted);min-width:160px;flex:none}
  .vrow .vv{color:var(--text);font-weight:600}
  @media(max-width:480px){.vrow{flex-direction:column;gap:2px}.vrow .k{min-width:0}}
  .note code{color:var(--accent)}
  /* Back-to-top — fixed floating control, revealed after scrolling down */
  .totop{position:fixed;right:22px;bottom:24px;width:46px;height:46px;border-radius:50%;background:var(--accent2);border:1px solid var(--accent);color:#dff3ff;font-size:20px;cursor:pointer;opacity:0;visibility:hidden;transition:opacity .25s ease,visibility .25s ease;z-index:120;box-shadow:0 6px 18px rgba(0,0,0,.4)}
  .totop.show{opacity:1;visibility:visible}
  .totop:hover{background:#176a9c}
  @media(max-width:920px){.grid{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:700px){.kpis{grid-template-columns:repeat(3,1fr)}.cards{grid-template-columns:1fr}}
  @media(max-width:430px){.kpis{grid-template-columns:repeat(2,1fr)}.nitem{flex-direction:column;gap:8px;padding:18px 20px}.nitem .nd{min-width:0}}
  /* Mobile compaction — desktop sizes are too big on phones */
  @media(max-width:480px){
    .wrap{padding:14px 12px}
    header{padding:16px 14px 12px}
    h1{font-size:17px}
    h3.sec{font-size:13.5px;margin:18px 0 10px}
    .kpi{padding:11px 12px}
    .kpi .v{font-size:21px}
    .kpi .l{font-size:9.5px}
    .kpis{gap:8px}
    .card2{padding:12px 13px}
    .card2 .nm{font-size:13.5px}
    .card2 .ovr b{font-size:18px}
    .card2 .rk{font-size:20px}
    .vsheet{padding:18px 16px}
    .nitem .nh{font-size:15px}
  }
  /* =======================================================================
     MOBILE-ONLY REDESIGN  (max-width:640px) — additive, must NOT affect desktop.
     Native pinch-zoom + a real responsive layout replace the old text-zoom
     "magnifier". Every rule here is scoped to phones; the >=641px render is
     untouched. Built up in phases: P0 universal · P1 charts/floor-map/MQ · P2 polish.
     ======================================================================= */
  /* ---------- P0 · universal ---------- */
  @media (max-width:640px){
    /* Kill the in-page zoom control — native pinch-zoom replaces it. This is the
       #1 reason every screen looked like a shrunk desktop. */
    .zoomctl{display:none !important}
    /* Safe-area insets — clear the iOS status bar (top) and the Safari toolbar
       (bottom) now that viewport-fit=cover lets content run under them. */
    header{padding-top:max(16px,env(safe-area-inset-top))}
    .wrap,.mapwrap{padding-bottom:max(28px,env(safe-area-inset-bottom))}
    /* Comfortable tap targets (>=44px) with breathing room. */
    .dl,.infobtn{min-height:44px;display:inline-flex;align-items:center;gap:5px}
    .navbtns{gap:8px}
    .bchip{min-height:40px;display:inline-flex;align-items:center}
    /* Never let a stray element force a horizontal scrollbar on the page. */
    .wrap,.mapwrap{max-width:100%;overflow-x:clip}
  }
  /* Tools menu + bottom sheet are hidden entirely on desktop; revealed on phones. */
  .toolsbtn,.toolsheet,.toolsheet-backdrop{display:none}
  /* ---------- P0 · header collapse ---------- */
  @media (max-width:640px){
    h1{font-size:clamp(1.05rem,4.6vw,1.4rem);line-height:1.2}
    .sub{font-size:11.5px;margin-top:3px}
    header{padding-left:14px;padding-right:14px;padding-bottom:12px}
    .brand{gap:10px}
    /* Collapse the 6-action cluster → 2 chips (Magic Quadrant, Floor Map) + Tools ▾. */
    .navbtns{width:100%;margin-left:0;gap:8px;justify-content:flex-start}
    .navbtns>#scoreInfoBtn,
    .navbtns>a[href="?view=news"],
    .navbtns>a[download],
    .navbtns>a[href="#allpartners"]{display:none}
    .navbtns>a[href="?view=mq"],
    .navbtns>a[href="?view=map"]{flex:1 1 auto;justify-content:center;font-size:12.5px;padding:10px 6px;white-space:nowrap}
    .toolsbtn{display:inline-flex;align-items:center;gap:5px;min-height:44px;background:var(--panel2);
      border:1px solid var(--border);color:var(--text);border-radius:9px;padding:10px 12px;font-size:12.5px;
      font-family:inherit;cursor:pointer;white-space:nowrap}
    .toolsbtn[aria-expanded=true]{border-color:var(--accent);color:#dff3ff}
    /* The bottom sheet itself (slide-up). */
    .toolsheet-backdrop.open{display:block;position:fixed;inset:0;background:rgba(6,12,24,.5);z-index:300}
    .toolsheet.open{display:flex;flex-direction:column;position:fixed;left:0;right:0;bottom:0;z-index:310;
      background:linear-gradient(180deg,#15233f,#0f1830);border-top:1px solid var(--accent2);
      border-radius:18px 18px 0 0;padding:8px 14px max(18px,env(safe-area-inset-bottom));
      box-shadow:0 -16px 40px rgba(0,0,0,.5)}
    .toolsheet-grip{display:block;width:42px;height:4px;border-radius:3px;background:var(--border);margin:4px auto 8px}
    .toolsheet-item{display:flex;align-items:center;gap:11px;min-height:48px;padding:12px 8px;
      border:none;border-bottom:1px solid var(--border);background:none;color:var(--text);
      font:inherit;font-size:15px;text-decoration:none;text-align:left;width:100%;cursor:pointer}
    .toolsheet-item:last-child{border-bottom:none}
    .toolsheet-item:active{background:var(--panel2)}
  }
  /* ---------- P1 · charts ---------- */
  @media (max-width:640px){
    /* Let the taller mobile aspect-ratios render fully — the global 300px canvas
       cap (and valChart's inline 380px) would otherwise clip the plot. */
    #topChart,#nicheChart,#profChart,#valChart{max-height:none!important}
  }
  /* ---------- P1 · floor map ---------- */
  .planfit,.planhint{display:none}
  @media (max-width:640px){
    /* Zone columns → full-width vertical sections; no horizontal scroll or slider. */
    #mapColsWrap{overflow-x:hidden;padding:12px}
    #mapColsWrap .maprow{flex-direction:column;gap:22px;min-width:0}
    #mapColsWrap .zonecol{width:100%}
    #mapColsNav{display:none!important}
    .zonehd{text-align:left;font-size:13px}
    .booth{padding:10px 12px}
    .booth .bnm{font-size:14px}
    .booth .bn{font-size:11px}
    .mapsearch{width:100%}
    .maptoggle{display:flex;width:100%;margin-bottom:12px}
    .maptoggle button{flex:1;min-height:44px;font-size:13px}
    /* Floor plan → a pinch-zoom / pannable canvas with a Fit button. */
    #mapPlanWrap{position:relative;overflow:hidden;touch-action:none;border:1px solid var(--border);
      border-radius:14px;background:linear-gradient(180deg,#0e1730,#0c1426)}
    #mapPlanWrap .planbox{height:460px;border:none;border-radius:0;background:none}
    #mapPlan{transform-origin:0 0}
    .planfit{display:inline-flex;align-items:center;gap:5px;position:absolute;top:10px;right:10px;z-index:6;
      background:rgba(15,24,48,.94);border:1px solid var(--accent);color:#dff3ff;border-radius:10px;
      padding:9px 12px;font:inherit;font-size:13px;font-weight:600;min-height:40px;cursor:pointer}
    .planhint{display:block;position:absolute;left:10px;bottom:10px;z-index:6;pointer-events:none;
      background:rgba(11,16,32,.78);color:var(--muted);border-radius:8px;padding:5px 9px;font-size:11px}
  }
  /* ---------- P1 · magic quadrant ---------- */
  @media (max-width:640px){
    /* Square plot fitting screen width (was a fixed 560px-tall portrait box). */
    #mqBox{height:auto!important;aspect-ratio:1}
    #mqChart{max-height:none!important}
    /* Full-width "drill into niche" control + comfortable chips. */
    #mqSegSel{flex:1 1 100%;width:100%;min-height:40px;font-size:13px}
    #mqQuadChips,#mqTierChips{gap:8px}
    #mqBack{min-height:40px}
  }
  /* ---------- P2 · table -> cards, bottom-sheet detail, sticky header ---------- */
  .vcards,.vfilters-m{display:none}
  @keyframes sheetUp{from{transform:translateY(100%)}to{transform:translateY(0)}}
  @media (max-width:640px){
    /* All Partner Vendors: the 13-column table -> full-width stacked cards, with
       the column filters surfaced as a full-width bar above them. */
    .scroll{display:none}
    .sorthint{display:none}
    .vfilters-m{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px}
    .vfilters-m select{width:100%;background:var(--panel2);border:1px solid var(--border);color:var(--text);
      border-radius:9px;padding:10px;font-size:13px;min-height:44px}
    .vcards{display:flex;flex-direction:column;gap:10px}
    .vcard{background:linear-gradient(160deg,#15233f,#101a30);border:1px solid var(--border);border-radius:12px;padding:13px 14px;cursor:pointer}
    .vcard:active{border-color:var(--accent)}
    .vcard.gem .vcard-name::after{content:" 💎";font-size:11px}
    .vcard-top{display:flex;align-items:center;gap:8px}
    .vcard-rank{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums;min-width:34px}
    .vcard-name{font-weight:700;font-size:15px;flex:1;min-width:0}
    .vcard-meta{font-size:12px;color:var(--muted);margin-top:7px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
    .vcard-score{margin-top:9px;font-size:13px;color:var(--muted)}
    .vcard-score b{color:var(--text);font-size:17px}
    /* Vendor detail -> slide-up bottom sheet (was a centered modal). */
    .vmodal{align-items:flex-end;padding:0}
    .vsheet{max-width:none;width:100%;border-radius:18px 18px 0 0;
      padding:16px 18px max(20px,env(safe-area-inset-bottom));max-height:92dvh;overflow:auto;animation:sheetUp .22s ease}
    .vsheet::before{content:"";display:block;width:42px;height:4px;border-radius:3px;background:var(--border);margin:0 auto 12px}
    .vsheet .x{top:14px;right:16px;font-size:26px}
    /* Sticky, compacting header (chips / Back stay reachable; safe-area aware). */
    header{position:sticky;top:0;z-index:50}
    body.compact header .sub{display:none}
    body.compact header{padding-top:max(8px,env(safe-area-inset-top));padding-bottom:8px}
    body.compact h1{font-size:1.02rem}
    body.compact .logo{width:28px;height:28px}
  }
  /* ---------- P3 · highlight re-layout (declutter) ---------- */
  @media (max-width:640px){
    /* Slim 2-up highlight cards: drop the per-dimension breakout chips (tap a card
       for the full breakdown), keep name / tier / niche / overall. */
    #bestfit{grid-template-columns:repeat(2,1fr);gap:8px}
    .card2 .scores{display:none}
    .card2{padding:11px 12px}
    .card2 .nm{font-size:12.5px;padding-right:22px}
    .card2 .ct{font-size:9.5px}
    .card2 .rk{font-size:17px;top:6px;right:9px}
    .card2 .ovr{margin-top:4px}
    .card2 .ovr b{font-size:17px}
    /* Floor Map: the spatial Floor plan is the only mobile view — drop the toggle
       and the Zone-columns list entirely. */
    .maptoggle{display:none!important}
    #mapColsWrap,#mapColsNav{display:none!important}
  }
  /* ---------- P4 · audit fixes (accessibility) ---------- */
  /* Honor reduced-motion everywhere — kills the slide-up sheet, the disclosure
     rotate, chart load animation, and smooth-scroll for users who ask for it. */
  @media (prefers-reduced-motion: reduce){
    *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
      transition-duration:.01ms!important;scroll-behavior:auto!important}
  }
  @media (max-width:640px){
    /* Visible keyboard focus on the new interactive elements. */
    .vcard:focus-visible,.toolsbtn:focus-visible,
    .toolsheet-item:focus-visible,.planfit:focus-visible,
    .navbtns>a[href="?view=mq"]:focus-visible,.navbtns>a[href="?view=map"]:focus-visible{
      outline:2px solid #cfe8ff;outline-offset:2px}
    /* >=44px tap targets across the mobile controls. */
    .planfit,.bchip,#mqQuadChips button,#mqTierChips button,#mqSegSel,#mqBack,
    .navbtns>a[href="?view=mq"],.navbtns>a[href="?view=map"]{min-height:44px}
    .kpi-sub-link{min-height:44px;padding:6px 2px}
    #search{min-height:44px}
    /* Signal that the slim highlight cards reveal the full score breakdown on tap. */
    .card2{padding-bottom:26px}
    .card2::after{content:"tap for scores ›";position:absolute;right:12px;bottom:9px;
      font-size:9px;color:var(--accent);opacity:.8;text-transform:uppercase;letter-spacing:.03em;white-space:nowrap}
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="logo">❄</div>
    <div>
      <h1>Snowflake Summit 2026 — Partner Scouting Dashboard</h1>
      <div class="sub" id="subhead"></div>
    </div>
    <div class="navbtns">
      <a class="dl" href="../#summit" title="Return to the main dashboard (Summit tab)">← Back to dashboard</a>
      <button type="button" class="infobtn no-print scoreInfoTrigger" id="scoreInfoBtn" aria-haspopup="dialog" aria-expanded="false" aria-controls="scorePop" title="How the scores are calculated">ⓘ Scoring</button>
      <a class="dl" href="?view=news" target="_blank" rel="noopener" title="Opens the partner news feed in its own window">📰 Summit News ↗</a>
      <a class="dl" href="?view=mq" target="_blank" rel="noopener" title="Opens the Magic Quadrant in its own window">📊 Magic Quadrant ↗</a>
      <a class="dl" href="?view=map" target="_blank" rel="noopener" title="Opens the interactive Basecamp floor map in its own window">🗺 Floor Map ↗</a>
      <a class="dl" href="#allpartners" title="Jump to the full partner table">🤝 All Partner Vendors</a>
      <a class="dl" href="Snowflake_Summit_2026_Master_Partner_Scouting.xlsx" download title="Download the source spreadsheet (.xlsx)">⬇ Download</a>
      <span class="zoomctl" title="Zoom"><button type="button" class="zbtn" data-zoom="out" aria-label="Zoom out">🔍−</button><span class="zlevel">100%</span><button type="button" class="zbtn" data-zoom="in" aria-label="Zoom in">🔍+</button></span>
      <button type="button" class="toolsbtn no-print" id="toolsBtn" aria-haspopup="true" aria-expanded="false" aria-controls="toolsSheet">⚙ Tools ▾</button>
    </div>
  </div>
</header>
<!-- Mobile-only "Tools ▾" bottom sheet (hidden on desktop) — holds the actions
     pulled out of the header so the phone landing is just 2 chips + Tools. -->
<div class="toolsheet-backdrop no-print" id="toolsBackdrop"></div>
<div class="toolsheet no-print" id="toolsSheet" role="dialog" aria-modal="true" aria-label="More tools" aria-hidden="true">
  <div class="toolsheet-grip" aria-hidden="true"></div>
  <button type="button" class="toolsheet-item scoreInfoTrigger" aria-haspopup="dialog" aria-controls="scorePop">ⓘ <span>Scoring — how the scores work</span></button>
  <a class="toolsheet-item" href="?view=news" target="_blank" rel="noopener">📰 <span>Summit News</span></a>
  <a class="toolsheet-item" href="#allpartners">🤝 <span>All Partner Vendors</span></a>
  <a class="toolsheet-item" href="Snowflake_Summit_2026_Master_Partner_Scouting.xlsx" download>⬇ <span>Download</span></a>
</div>
<div class="scorepop no-print" id="scorePop" role="dialog" aria-modal="false" aria-label="How the scores are calculated" hidden>
  <button class="x" type="button" id="scorePopX" aria-label="Close">&times;</button>
  <h3>How the scores work</h3>
  <p>Every partner is rated <b>0–10</b> on five directional, scouting-lens dimensions, then blended into an Overall:</p>
  <ul>
    <li><b>Bryan Recommend</b> — career &amp; networking fit for Bryan specifically; baseline calibrated from a sample of <b>30 vendor booth selling pitches</b> (directional — to confirm).</li>
    <li><b>Snowflake</b> — relevance to the Snowflake ecosystem (native apps, integrations, Summit presence, joint go-to-market).</li>
    <li><b>AI</b> — strength &amp; relevance of the vendor's AI / ML / agent story.</li>
    <li><b>Retail / Customer</b> — fit for retail &amp; customer-analytics use cases (the scouting focus).</li>
    <li><b>IPO / Upside</b> — growth trajectory, funding / valuation momentum, and exit / investment upside.</li>
    <li><b>Overall</b> — the simple mean (average) of the five scores.</li>
  </ul>
  <p>Scores are Bryan's directional ratings from the scouting workbook (some researched, some template / estimated). <b>Tier</b> is a curated priority call (A = must-see) that uses Overall as a guideline (≈ 7.5+ / 6+ / below), set editorially. Funding &amp; valuation figures were enriched via AI web research — verify before relying.</p>
</div>
<div class="wrap" id="dashwrap">
  <div class="kpis" id="kpis"></div>

  <h3 class="sec">🤝 Bryan’s Recommendation <span class="hint">— top career / networking fit</span></h3>
  <div class="cards" id="bestfit"></div>

  <div class="topbar">
    <div class="searchwrap">
      <input id="search" placeholder="Search all vendors — name, category, niche…" autocomplete="off"/>
    </div>
    <span class="hit" id="searchhit"></span>
  </div>

  <h3 class="sec" id="allpartners">All Partner Vendors <span class="hint">— <span class="sorthint">click a column to sort · </span>💎 = hidden gem</span></h3>
  <div class="panel">
    <div class="vfilters-m no-print" id="vFiltersM"></div>
    <div class="scroll">
    <table id="vtable">
      <thead>
      <tr>
        <th data-k="rank">#</th><th data-k="name">Partner</th><th data-k="booth">Booth</th>
        <th data-k="niche">Niche</th><th data-k="category">Category</th><th data-k="company_type">Type</th>
        <th data-k="snowflake_score" class="num">Snow</th><th data-k="ai_score" class="num">AI</th>
        <th data-k="retail_score" class="num">Retail</th><th data-k="ipo_score" class="num">IPO</th>
        <th data-k="bryan_score" class="num">Bryan</th>
        <th data-k="overall_score" class="num">Overall</th><th data-k="tier">Tier</th>
      </tr>
      <tr class="filterrow">
        <th></th><th></th><th></th>
        <th><select id="nicheFilter"><option value="">All niches</option></select></th>
        <th><select id="catFilter"><option value="">All categories</option></select></th>
        <th><select id="typeFilter"><option value="">All types</option></select></th>
        <th></th><th></th><th></th><th></th><th></th><th></th>
        <th><select id="tierFilter"><option value="">All tiers</option></select></th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
    <div class="vcards" id="vcards"></div>
    <div id="vmore" class="no-print"></div>
  </div>

  <div class="grid" style="margin-top:22px">
    <div class="panel"><h4>Top <span class="barNum">15</span> by Overall Score</h4><canvas id="topChart" role="img" aria-label="Bar chart: the 15 highest-scoring partners by overall score (0–10)."></canvas></div>
    <div class="panel"><h4>Priority Tier mix</h4><canvas id="tierChart" role="img" aria-label="Doughnut chart: partner counts by priority tier (A, B, C)."></canvas></div>
  </div>
  <div class="grid">
    <div class="panel"><h4>Partners by Niche</h4><canvas id="nicheChart" role="img" aria-label="Bar chart: partner counts grouped by value niche."></canvas></div>
    <div class="panel"><h4>Avg score profile — Tier A vs all</h4><canvas id="profChart" role="img" aria-label="Radar chart: average score profile across the five dimensions, Tier A versus all partners."></canvas></div>
  </div>
  <div class="panel" style="margin-bottom:16px"><h4>💰 Top <span class="barNum">15</span> by Valuation <span class="hint" style="font-weight:400;color:var(--muted)">— parsed from reported valuation / market cap; hover for detail</span></h4><canvas id="valChart" style="max-height:380px" role="img" aria-label="Bar chart: the 15 partners with the highest reported valuation or market cap."></canvas></div>

  <div class="note" id="note"></div>
  <button id="toTop" type="button" class="totop no-print" aria-label="Back to top" title="Back to top">↑</button>
</div>

<div id="newsView">
  <header>
    <div class="brand">
      <div class="logo">📰</div>
      <div>
        <h1>Snowflake Summit 2026 — Partner News</h1>
        <div class="sub">Live announcements from partner vendors · its own window</div>
      </div>
      <span class="zoomctl" title="Zoom" style="margin-left:auto"><button type="button" class="zbtn" data-zoom="out" aria-label="Zoom out">🔍−</button><span class="zlevel">100%</span><button type="button" class="zbtn" data-zoom="in" aria-label="Zoom in">🔍+</button></span>
      <button type="button" class="infobtn no-print scoreInfoTrigger" aria-haspopup="dialog" aria-expanded="false" aria-controls="scorePop" title="How the scores are calculated" style="margin-left:14px">ⓘ Scoring</button>
      <a class="dl" href="?" style="margin-left:8px">← Back to dashboard</a>
    </div>
  </header>
  <div class="wrap">
    <h3 class="sec" id="news">📰 Summit News <span class="hint">— Snowflake Summit 2026 announcements from partner vendors, by category</span></h3>
    <div id="newsBuckets" class="bucketbar"></div>
    <div class="panel">
      <div class="controls" style="margin-bottom:11px">
        <span class="sub" id="newsMeta"></span>
        <label class="sub" style="margin-left:auto;align-self:center">Relevance</label>
        <select id="newsRel"><option value="">All</option><option value="high">High only</option><option value="medium">High + Medium</option></select>
        <label class="sub" style="align-self:center">Vendor</label>
        <select id="newsVendor"><option value="">All vendors</option></select>
      </div>
      <div class="newsfeed" id="newsfeed"></div>
      <div class="sub" id="newsEmpty" style="display:none;padding:16px 4px">No partner Summit announcements gathered yet — the feed populates on the next research run.</div>
      <div class="sub" style="margin-top:11px;line-height:1.5">Gathered by AI research agents searching public news / press per vendor; each link opens the primary source. Directional — verify before relying. Refreshes whenever the feed is rebuilt.</div>
    </div>
  </div>
</div>

<div id="mqView">
  <header>
    <div class="brand">
      <div class="logo">📊</div>
      <div>
        <h1>Snowflake Summit 2026 — Magic Quadrant</h1>
        <div class="sub" id="mqsub">drill into a niche · its own window</div>
      </div>
      <span class="zoomctl" title="Zoom" style="margin-left:auto"><button type="button" class="zbtn" data-zoom="out" aria-label="Zoom out">🔍−</button><span class="zlevel">100%</span><button type="button" class="zbtn" data-zoom="in" aria-label="Zoom in">🔍+</button></span>
      <button type="button" class="infobtn no-print scoreInfoTrigger" aria-haspopup="dialog" aria-expanded="false" aria-controls="scorePop" title="How the scores are calculated" style="margin-left:14px">ⓘ Scoring</button>
      <a class="dl" href="?" style="margin-left:8px">← Back to dashboard</a>
    </div>
  </header>
  <div class="wrap">
    <h3 class="sec" id="mq">📊 Magic Quadrant <span class="hint">— all partners, or drill into a niche</span></h3>
    <div class="panel">
      <div class="controls" style="margin-bottom:8px">
        <label class="sub" style="align-self:center">Drill into niche:</label>
        <select id="mqSegSel"></select>
        <button id="mqBack" type="button" style="display:none;background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 11px;font-size:12px;cursor:pointer">← All partners</button>
        <span id="mqCrumb" class="sub" style="align-self:center"></span>
      </div>
      <div id="mqLegend" class="sub" style="margin-bottom:8px"></div>
      <div class="sub" style="margin:2px 0 3px">Quadrant <span style="color:var(--muted)">— where a partner sits</span></div>
      <div id="mqQuadChips" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px"></div>
      <div class="sub" style="margin:4px 0 3px">Tier <span style="color:var(--muted)">— Bryan's scouting priority (= dot colour)</span></div>
      <div id="mqTierChips" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px"></div>
      <div id="mqBox" style="height:560px;position:relative">
        <canvas id="mqChart" style="max-height:560px" role="img" aria-label="Magic Quadrant scatter plot: partners positioned by Ability to Execute (vertical) and Completeness of Vision (horizontal), split into Leaders, Challengers, Visionaries, and Niche Players."></canvas>
      </div>
      <div id="mqCaveat" class="sub" style="margin-top:12px;line-height:1.5;color:#cfe0ff"></div>
      <div class="sub" style="margin-top:10px;line-height:1.55">
        <b style="color:var(--text)">How to read:</b> <b style="color:var(--text)">Ability to Execute</b> (vertical) = mean of Snowflake &amp; Retail/Customer scores · <b style="color:var(--text)">Completeness of Vision</b> (horizontal) = mean of AI &amp; IPO/Upside scores — both on the 0–10 scale. <b style="color:var(--text)">A dot's colour is its scouting tier</b> (<b style="color:var(--A)">A</b> / <b style="color:#fbbf24">B</b> / <b style="color:#60a5fa">C</b>) — independent of which quadrant it lands in. The cross sits at the cohort <i>average</i>: <b style="color:var(--A)">Leaders</b> (execute + vision), <b style="color:var(--accent)">Challengers</b> (execute), <b style="color:var(--gem)">Visionaries</b> (vision), <b style="color:#94a3b8">Niche Players</b> (neither). Tier-A must-sees are labelled; hover any dot for exact scores. <b style="color:var(--text)">Drill-down:</b> pick a niche to see its own quadrant, re-centred on that niche's cohort average. <b>This is Bryan's directional scouting, not an official Gartner Magic Quadrant</b> — small or template-scored niches (flagged) are exploratory only.
      </div>
    </div>
  </div>
</div>

<div id="mapView">
  <header>
    <div class="brand">
      <div class="logo">🗺</div>
      <div>
        <h1>Snowflake Summit 2026 — Basecamp Floor Map</h1>
        <div class="sub">Partner + Snowflake activations · our scouted partners placed by booth zone · its own window</div>
      </div>
      <span class="zoomctl" title="Zoom" style="margin-left:auto"><button type="button" class="zbtn" data-zoom="out" aria-label="Zoom out">🔍−</button><span class="zlevel">100%</span><button type="button" class="zbtn" data-zoom="in" aria-label="Zoom in">🔍+</button></span>
      <button type="button" class="infobtn no-print scoreInfoTrigger" aria-haspopup="dialog" aria-expanded="false" aria-controls="scorePop" title="How the scores are calculated" style="margin-left:14px">ⓘ Scoring</button>
      <a class="dl" href="?" style="margin-left:8px">← Back to dashboard</a>
    </div>
  </header>
  <div class="mapwrap">
    <h3 class="sec" id="floormap">🗺 Your Guide to Basecamp <span class="hint">— our scouted partners on the Summit floor; click any booth for full detail</span></h3>
    <div style="margin:4px 0 12px"><input id="mapSearch" class="mapsearch" type="search" placeholder="Highlight a vendor by name…" autocomplete="off"></div>
    <div class="maptoggle" role="group" aria-label="Floor map view">
      <button type="button" id="tabPlan" class="on" aria-pressed="true">🗺 Floor plan</button>
      <button type="button" id="tabCols" aria-pressed="false">▦ Zone columns</button>
    </div>
    <div class="maplegend" id="mapLegend"></div>
    <div id="mapPlanWrap"><div class="planbox" id="mapPlan" role="region" aria-label="Basecamp floor plan"></div><button type="button" class="planfit no-print" id="planFit" aria-label="Fit floor plan to screen">⤢ Fit</button><span class="planhint" aria-hidden="true">pinch to zoom · drag to pan</span></div>
    <div id="mapColsNav" style="display:none">
      <div class="mapcols-hint">◀ slide to pan across all zones ▶</div>
      <input type="range" id="mapColsSlider" class="mapcols-slider" min="0" max="1000" value="0" aria-label="Scroll the floor map left and right">
    </div>
    <div class="mapscroll" id="mapColsWrap" role="region" aria-label="Basecamp floor map, scroll horizontally" tabindex="0" style="display:none"><div class="maprow" id="mapRow"></div></div>
    <div class="mapfoot" id="mapFoot"></div>
    <details class="floorimg"><summary>📷 Original Basecamp board (photo) — cross-reference the real layout</summary><img src="__FLOOR_IMG_SRC__" alt="Snowflake Summit 2026 — Your Guide to Basecamp, original floor-map board" loading="lazy"></details>
  </div>
</div>

<div class="signature">
  <div class="sigrule"></div>
  <div class="signame">BDT- Bryan D Tabiadon</div>
  <div class="sigsub">Partner Scouting · Snowflake Summit 2026</div>
  <details class="scoredef"><summary>Scoring Defined</summary><div class="scoredef-body" id="scoreDefBody"></div></details>
</div>

<script>
const DATA = /*__DATA__*/;
const fmt = n => (n===null||n===undefined||n==='')?"—":n;
const esc = s => String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
// Per-vendor homepage. Real curated URL (Tier A/B) when v.website is set; else a
// DuckDuckGo "!ducky" I'm-Feeling-Lucky redirect to the top result (Tier C fallback),
// so every partner name links through to a site. homeLink()'s stopPropagation keeps
// the click from also firing the card/row -> detail-modal delegation (see vModal).
function homeUrl(v){var w=(v&&v.website)?String(v.website).trim():'';return /^https?:\/\//i.test(w)?w:'https://duckduckgo.com/?q='+encodeURIComponent('!ducky '+((v&&v.name)||''));}
function homeLink(v){var w=(v&&v.website)?String(v.website).trim():'',real=/^https?:\/\//i.test(w),u=homeUrl(v),nm=esc((v&&v.name)||'');return '<a class="homelink" href="'+esc(u)+'" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()" title="'+(real?'Visit homepage':'Search for homepage')+' ↗" aria-label="'+(real?'Visit '+nm+' homepage':'Search the web for '+nm)+' (opens in a new tab)">↗</a>';}
const tierClass = t => ({A:'tA',B:'tB',C:'tC',D:'tD'})[t]||'tC';

// View router: ?view=news opens the news-only view (in its own window).
var _view=new URLSearchParams(location.search).get('view');
if(_view==='news'){
  document.body.classList.add('newsmode');
  document.title='Summit News — Snowflake Summit 2026';
}else if(_view==='mq'){
  document.body.classList.add('mqmode');
  document.title='Magic Quadrant — Snowflake Summit 2026';
  var _mqsub=document.getElementById('mqsub');
  if(_mqsub) _mqsub.textContent=`All ${DATA.vendors.length} partners, or drill into a niche · its own window`;
}else if(_view==='map'){
  document.body.classList.add('mapmode');
  document.title='Basecamp Floor Map — Snowflake Summit 2026';
}

// A-/A+ text zoom — scales the page via CSS zoom; persists across visits + views.
// MOBILE (<=640px): the control is hidden and native pinch-zoom replaces it, so we
// never apply a persisted shrink (the old "80%" that made phones look like tiny
// desktop). Desktop path is unchanged.
(function(){
  var isMobile = window.matchMedia && window.matchMedia('(max-width:640px)').matches;
  var z=parseFloat(localStorage.getItem('summitZoom')); if(!z||isNaN(z)) z=1;
  if(isMobile) z=1;
  function apply(){document.documentElement.style.zoom=z;document.querySelectorAll('.zlevel').forEach(function(e){e.textContent=Math.round(z*100)+'%';});}
  function step(d){z=Math.min(1.6,Math.max(0.8,Math.round((z+d)*100)/100));localStorage.setItem('summitZoom',String(z));apply();}
  document.querySelectorAll('.zbtn').forEach(function(b){b.addEventListener('click',function(){step(b.getAttribute('data-zoom')==='in'?0.1:-0.1);});});
  if(!isMobile) apply();
})();

// Mobile "Tools ▾" bottom sheet — opens the actions pulled out of the phone header
// (Scoring / Summit News / spreadsheet / PDF). No-op on desktop (the trigger is hidden).
(function(){
  var btn=document.getElementById('toolsBtn'), sheet=document.getElementById('toolsSheet'), back=document.getElementById('toolsBackdrop');
  if(!btn||!sheet) return;
  var lastFocus=null;
  function items(){return Array.prototype.filter.call(sheet.querySelectorAll('button,[href],[tabindex]:not([tabindex="-1"])'),function(el){return el.offsetParent!==null&&!el.disabled;});}
  function open(){lastFocus=document.activeElement;sheet.classList.add('open');if(back)back.classList.add('open');btn.setAttribute('aria-expanded','true');sheet.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';var f=items();if(f.length)f[0].focus();}
  function close(restore){sheet.classList.remove('open');if(back)back.classList.remove('open');btn.setAttribute('aria-expanded','false');sheet.setAttribute('aria-hidden','true');document.body.style.overflow='';if(restore!==false&&lastFocus&&lastFocus.focus){try{lastFocus.focus();}catch(_){}}lastFocus=null;}
  btn.addEventListener('click',function(e){e.stopPropagation();sheet.classList.contains('open')?close():open();});
  if(back) back.addEventListener('click',function(){close();});
  // item navigates / opens scorePop itself, so close without stealing focus back
  sheet.querySelectorAll('.toolsheet-item').forEach(function(it){it.addEventListener('click',function(){setTimeout(function(){close(false);},0);});});
  document.addEventListener('keydown',function(e){
    if(!sheet.classList.contains('open'))return;
    if(e.key==='Escape'){close();return;}
    if(e.key==='Tab'){var f=items();if(!f.length){e.preventDefault();return;}var first=f[0],last=f[f.length-1];
      if(!sheet.contains(document.activeElement)){first.focus();e.preventDefault();}
      else if(e.shiftKey&&document.activeElement===first){last.focus();e.preventDefault();}
      else if(!e.shiftKey&&document.activeElement===last){first.focus();e.preventDefault();}}
  });
})();

// Re-run all the load-time mobile/desktop branching cleanly when the viewport
// crosses the 640px breakpoint (e.g. a phone rotated to landscape) — otherwise the
// CSS flips but the JS-built layout (card list, section order) would not.
(function(){var mq=window.matchMedia&&window.matchMedia('(max-width:640px)');
  if(mq&&mq.addEventListener)mq.addEventListener('change',function(){location.reload();});})();

(function(){
  var sh=document.getElementById('subhead'); if(!sh) return;
  var base=`${DATA.vendors.length} partners · ${DATA.meta.event||''} · scoring by ${DATA.meta.owner||'owner'}`;
  // Drop the "· source: …" tail on phones to keep the header subtitle to one tidy line.
  var isMobile = window.matchMedia && window.matchMedia('(max-width:640px)').matches;
  sh.textContent = isMobile ? base : (base + ` · source: ${DATA.source}`);
})();

document.getElementById('kpis').innerHTML = DATA.kpis.map(k=>{
  const clickable=!!k.filter;
  const body=`<div class="v">${k.value}</div><div class="l">${k.label}</div>`;
  // A tile with a subFilter holds TWO buttons (main + sub). To avoid nesting one
  // role=button inside another (an a11y violation), the outer .kpi is a plain
  // container in that case and the main number/label is its own button sibling
  // of the sub-link. Tiles without a subFilter stay a single button.
  if(clickable && k.subFilter){
    return `<div class="kpi kpi-click">`+
      `<a href="#" class="kpi-main-link" data-filter="${k.filter}" role="button" tabindex="0" aria-haspopup="dialog" title="Show all ${k.value} ${k.label}" aria-label="Show all ${k.value} ${k.label}">${body}</a>`+
      `<div class="s"><a href="#" class="kpi-sub-link" data-filter="${k.subFilter}" role="button" tabindex="0" aria-haspopup="dialog" title="Show these" aria-label="Show ${k.sub}">${k.sub}</a></div></div>`;
  }
  return `<div class="kpi${clickable?' kpi-click':''}"${clickable?` data-filter="${k.filter}" role="button" tabindex="0" aria-haspopup="dialog" title="Show all ${k.value} ${k.label}" aria-label="Show all ${k.value} ${k.label}"`:''}>${body}<div class="s">${k.sub}</div></div>`;
}).join('');
(function(){
  var kp=document.getElementById('kpis'); if(!kp) return;
  function pick(t){var el=t.closest&&t.closest('[data-filter]'); return el?el.getAttribute('data-filter'):null;}
  kp.addEventListener('click',function(e){var f=pick(e.target); if(f){e.preventDefault(); if(window.summitOpenList) window.summitOpenList(f);}});
  kp.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '||e.key==='Spacebar'){var f=pick(e.target); if(f){e.preventDefault(); if(window.summitOpenList) window.summitOpenList(f);}}});
})();

function scoreChips(v){
  const items=[['Bryan',v.bryan_score],['Snow',v.snowflake_score],['AI',v.ai_score],['Retail',v.retail_score],['IPO',v.ipo_score]];
  return items.map(([l,x])=>`<span class="schip">${l} <b>${fmt(x)}</b></span>`).join('');
}
function card(v){
  return `<div class="card2" data-v="${esc(v.name)}" tabindex="0" role="button" aria-label="View company detail for ${esc(v.name)}" style="cursor:pointer" title="Click for full company detail"><div class="rk">${v.rank}</div>
    <div class="nm">${esc(v.name)}${homeLink(v)} <span class="tag ${tierClass(v.tier)}">${esc(v.tier)}</span> <span class="tag tNi">${esc(fmt(v.niche))}</span></div>
    <div class="ct">${esc(v.category)} · booth ${esc(fmt(v.booth))}</div>
    <div class="scores">${scoreChips(v)}</div>
    <div class="ovr"><b>${fmt(v.overall_score)}</b><span>/ 10 overall${v.company_type?(' · '+esc(v.company_type)):''}</span></div></div>`;
}
document.getElementById('bestfit').innerHTML = DATA.best_fit.map(card).join('');

// The desktop DOM now orders Bryan → analytics → search → table, and mobile
// follows that same order, so no mobile-only re-layout is needed here. (The
// Bryan heading is identical on both breakpoints; the old Must-See / Hidden
// Gems disclosure sections were removed in the reorg.)

const C={grid:'#243352',tick:'#8da2c8'};
if(window.Chart){Chart.defaults.font.family='-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif';Chart.defaults.color=C.tick;}
// Phones: fewer bars + a taller plot + bigger ticks so the bar labels stop
// colliding. Desktop keeps the default 15 bars / aspectRatio 2 (unchanged).
var IS_MOBILE=!!(window.matchMedia&&window.matchMedia('(max-width:640px)').matches);
var BARN=IS_MOBILE?10:15;
// Keep the "Top N" chart headings honest when we trim to 10 bars on phones.
if(IS_MOBILE) document.querySelectorAll('.barNum').forEach(function(e){e.textContent=BARN;});
const tierColor=t=>({A:'#34d399',B:'#fbbf24',C:'#60a5fa',D:'#3b82f6'})[t]||'#60a5fa';
// Inline plugin: print the value on each bar / doughnut slice (numbers on charts).
const valueLabels={id:'valueLabels',afterDatasetsDraw(chart){
  const ctx=chart.ctx,type=chart.config.type,area=chart.chartArea;ctx.save();
  chart.data.datasets.forEach((ds,di)=>{const meta=chart.getDatasetMeta(di);if(meta.hidden)return;
    meta.data.forEach((el,i)=>{const v=ds.data[i];if(v==null||v==='')return;
      if(type==='bar'){ctx.font='700 11px -apple-system,BlinkMacSystemFont,sans-serif';ctx.textBaseline='middle';
        var t=String(v),w=ctx.measureText(t).width;
        if(el.x+6+w<=area.right){ctx.fillStyle='#e8eeff';ctx.textAlign='left';ctx.fillText(t,el.x+6,el.y);}
        else{ctx.fillStyle='#06121f';ctx.textAlign='right';ctx.fillText(t,el.x-6,el.y);}}
      else if(type==='doughnut'){var p=el.getCenterPoint();
        ctx.font='800 13px -apple-system,BlinkMacSystemFont,sans-serif';ctx.fillStyle='#06121f';ctx.textAlign='center';ctx.textBaseline='middle';
        ctx.fillText(String(v),p.x,p.y);}});});
  ctx.restore();
}};

var topData=DATA.top15.slice(0,BARN);
new Chart(document.getElementById('topChart'),{type:'bar',plugins:[valueLabels],
  data:{labels:topData.map(v=>v.name),
    datasets:[{data:topData.map(v=>v.overall_score),backgroundColor:'#29b5e8',borderRadius:4}]},
  options:{indexAxis:'y',maintainAspectRatio:true,aspectRatio:IS_MOBILE?0.92:2,plugins:{legend:{display:false},tooltip:{callbacks:{afterLabel:c=>'Tier '+topData[c.dataIndex].tier}}},
    scales:{x:{min:0,max:10,ticks:{color:C.tick},grid:{color:C.grid}},y:{ticks:{color:C.tick,font:{size:IS_MOBILE?12:11}},grid:{display:false}}}}});

new Chart(document.getElementById('tierChart'),{type:'doughnut',plugins:[valueLabels],
  data:{labels:Object.keys(DATA.by_tier).map(t=>'Tier '+t),
    datasets:[{data:Object.values(DATA.by_tier),backgroundColor:Object.keys(DATA.by_tier).map(tierColor)}]},
  options:{plugins:{legend:{position:'right',labels:{color:C.tick}}}}});

var niches=Object.entries(DATA.by_niche);
if(IS_MOBILE) niches=niches.slice().sort((a,b)=>b[1]-a[1]).slice(0,BARN);
new Chart(document.getElementById('nicheChart'),{type:'bar',plugins:[valueLabels],
  data:{labels:niches.map(c=>c[0]),datasets:[{data:niches.map(c=>c[1]),backgroundColor:'#29b5e8'}]},
  options:{indexAxis:'y',maintainAspectRatio:true,aspectRatio:IS_MOBILE?0.92:2,plugins:{legend:{display:false}},onClick:(e,els)=>{if(els.length){nicheSel.value=niches[els[0].index][0];draw();document.getElementById('vtable').scrollIntoView({behavior:'smooth'});}},
    scales:{x:{ticks:{color:C.tick},grid:{color:C.grid}},y:{ticks:{color:C.tick,font:{size:IS_MOBILE?11.5:10.5}},grid:{display:false}}}}});

var profOpts={plugins:{legend:{labels:{color:C.tick}}},
    scales:{r:{min:0,max:10,angleLines:{color:C.grid},grid:{color:C.grid},pointLabels:{color:C.tick,font:{size:IS_MOBILE?9.5:11}},ticks:{display:false}}}};
// Phones: make the radar a square that fits the column, with padding so the
// point labels (Bryan/Snow/AI/Retail/IPO) don't clip at the edges.
if(IS_MOBILE){profOpts.maintainAspectRatio=true;profOpts.aspectRatio=1;profOpts.layout={padding:10};}
new Chart(document.getElementById('profChart'),{type:'radar',
  data:{labels:DATA.score_labels,datasets:[
    {label:'Tier A',data:DATA.profile_a,borderColor:'#34d399',backgroundColor:'rgba(52,211,153,.15)',pointBackgroundColor:'#34d399'},
    {label:'All partners',data:DATA.profile_all,borderColor:'#29b5e8',backgroundColor:'rgba(41,181,232,.12)',pointBackgroundColor:'#29b5e8'}]},
  options:profOpts});

// Top valuations — parse the $ figures from valuation/market_cap and chart the top 15.
(function(){
  var cv=document.getElementById('valChart'); if(!cv) return;
  function pv(s){if(!s)return null;var m=String(s).match(/\$\s*([\d.]+)\s*([BMT])/i);if(!m)return null;var n=parseFloat(m[1]);var u=m[2].toUpperCase();return u==='T'?n*1000000:u==='B'?n*1000:n;}
  function fb(n){return n>=1000000?('$'+(n/1000000).toFixed(n%1000000?2:0)+'T'):n>=1000?('$'+(n/1000).toFixed(n%1000?1:0)+'B'):('$'+Math.round(n)+'M');}
  var rows=(DATA.vendors||[]).map(function(v){return {name:v.name,tier:v.tier,num:pv(v.market_cap),raw:v.market_cap};}).filter(function(r){return r.num;}).sort(function(a,b){return b.num-a.num;}).slice(0,BARN);
  if(!rows.length) return;
  new Chart(cv,{type:'bar',
    data:{labels:rows.map(function(r){return r.name;}),datasets:[{data:rows.map(function(r){return r.num;}),backgroundColor:rows.map(function(r){return tierColor(r.tier);})}]},
    options:{indexAxis:'y',maintainAspectRatio:true,aspectRatio:IS_MOBILE?0.95:2,plugins:{legend:{display:false},tooltip:{callbacks:{label:function(c){return fb(c.raw)+(rows[c.dataIndex].raw?(' · '+rows[c.dataIndex].raw):'');}}}},
      scales:{x:{ticks:{color:C.tick,callback:function(v){return fb(v);}},grid:{color:C.grid}},y:{ticks:{color:C.tick,font:{size:11}},grid:{display:false}}}},
    plugins:[{id:'vlab',afterDatasetsDraw:function(ch){var ctx=ch.ctx,a=ch.chartArea;ctx.save();ctx.font='700 11px -apple-system,BlinkMacSystemFont,sans-serif';ctx.textBaseline='middle';ch.getDatasetMeta(0).data.forEach(function(el,i){var t=fb(rows[i].num),w=ctx.measureText(t).width;if(el.x+6+w<=a.right){ctx.fillStyle='#e8eeff';ctx.textAlign='left';ctx.fillText(t,el.x+6,el.y);}else{ctx.fillStyle='#06121f';ctx.textAlign='right';ctx.fillText(t,el.x-6,el.y);}});ctx.restore();}}]
  });
})();

// Table
const tbody=document.querySelector('#vtable tbody');
const nicheSel=document.getElementById('nicheFilter'),catSel=document.getElementById('catFilter'),tierSel=document.getElementById('tierFilter'),typeSel=document.getElementById('typeFilter');
Object.keys(DATA.by_niche).forEach(nz=>nicheSel.add(new Option(`${nz} (${DATA.by_niche[nz]})`,nz)));
[...new Set(DATA.vendors.map(v=>v.category))].sort().forEach(c=>catSel.add(new Option(c,c)));
[...new Set(DATA.vendors.map(v=>v.company_type).filter(Boolean))].sort().forEach(t=>typeSel.add(new Option(t,t)));
['A','B','C'].forEach(t=>tierSel.add(new Option('Tier '+t,t)));
// Phones: surface the column filters as a full-width bar above the cards (the
// table that normally hosts them is hidden). Moving the elements keeps their
// change-listeners intact; desktop leaves them in the table columns.
if(IS_MOBILE){var fbar=document.getElementById('vFiltersM');if(fbar){[nicheSel,catSel,typeSel,tierSel].forEach(function(s){fbar.appendChild(s);});}}
let sortK='rank',sortAsc=true,vExpanded=false;
var VPREVIEW=25;
function vcardHTML(v){
  var w=Math.round(((v.overall_score||0)/10)*54)+6;
  return '<div class="vcard'+(v.hidden_gem?' gem':'')+'" data-v="'+esc(v.name)+'" role="button" tabindex="0" aria-label="View company detail for '+esc(v.name)+'">'+
    '<div class="vcard-top"><span class="vcard-rank">#'+v.rank+'</span><span class="vcard-name">'+esc(v.name)+homeLink(v)+'</span><span class="tag '+tierClass(v.tier)+'">'+esc(v.tier)+'</span></div>'+
    '<div class="vcard-meta"><span class="tag tNi">'+esc(fmt(v.niche))+'</span> · booth '+esc(fmt(v.booth))+' · '+esc(fmt(v.category))+'</div>'+
    '<div class="vcard-score"><span class="ovrbar" style="width:'+w+'px;background:'+tierColor(v.tier)+'"></span><b>'+fmt(v.overall_score)+'</b> <span>/ 10'+(v.company_type?(' · '+esc(v.company_type)):'')+'</span></div>'+
    '</div>';
}
function rowHTML(v){
  const w=Math.round(((v.overall_score||0)/10)*54)+6;
  return `<tr class="${v.hidden_gem?'gem-row':''}" data-v="${esc(v.name)}" tabindex="0" aria-label="View company detail for ${esc(v.name)}" style="cursor:pointer">
      <td class="num">${v.rank}</td><td class="name">${esc(v.name)}${homeLink(v)}</td><td>${esc(fmt(v.booth))}</td>
      <td><span class="tag tNi">${esc(fmt(v.niche))}</span></td><td>${esc(fmt(v.category))}</td><td>${esc(fmt(v.company_type))}</td>
      <td class="num">${fmt(v.snowflake_score)}</td><td class="num">${fmt(v.ai_score)}</td>
      <td class="num">${fmt(v.retail_score)}</td><td class="num">${fmt(v.ipo_score)}</td><td class="num">${fmt(v.bryan_score)}</td>
      <td class="num"><span class="ovrbar" style="width:${w}px;background:${tierColor(v.tier)}"></span><b>${fmt(v.overall_score)}</b></td>
      <td><span class="tag ${tierClass(v.tier)}">${esc(v.tier)}</span></td></tr>`;
}
function draw(){
  const q=document.getElementById('search').value.toLowerCase(),nz=nicheSel.value,cf=catSel.value,tf=tierSel.value,yf=typeSel.value;
  const filtering=!!(q||nz||cf||tf||yf);
  let r=DATA.vendors.filter(v=>(!q||v.name.toLowerCase().includes(q)||(v.category||'').toLowerCase().includes(q)||(v.niche||'').toLowerCase().includes(q))
    &&(!nz||v.niche===nz)&&(!cf||v.category===cf)&&(!tf||v.tier===tf)&&(!yf||v.company_type===yf));
  r.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(typeof x==='string'){x=x.toLowerCase();y=(y||'').toLowerCase();}
    if(x===null||x===undefined)x=-1;if(y===null||y===undefined)y=-1;return (x>y?1:x<y?-1:0)*(sortAsc?1:-1);});
  // Scanning the page = a compact preview (top VPREVIEW) with NO inner scrollbar, so
  // a scroll gesture flows past the table to the rest of the page. Engaging it
  // (searching/filtering, or tapping "Show all") expands to the full set inside a
  // contained 560px scroll box. Same cap applies to the mobile card list.
  const showAll=filtering||vExpanded, total=r.length;
  const shown=showAll?r:r.slice(0,VPREVIEW);
  const hit=document.getElementById('searchhit');
  if(hit) hit.textContent = filtering ? `${total} of ${DATA.vendors.length} match` : (showAll?`${DATA.vendors.length} vendors`:`Top ${Math.min(VPREVIEW,total)} · ${DATA.vendors.length} total`);
  // Contain the expanded list in a 560px scroll box (matches the design); when
  // showing the preview, leave it uncapped so a scroll gesture flows past it.
  // On mobile the table (.scroll) is hidden and the cards (#vcards) are the live
  // surface, so the cap has to target #vcards there.
  var cap=document.querySelector(IS_MOBILE?'#vcards':'.scroll');
  if(cap){cap.style.maxHeight=showAll?'560px':'none';cap.style.overflowY=showAll?'auto':'visible';}
  if(IS_MOBILE){
    var vc=document.getElementById('vcards');
    if(vc) vc.innerHTML=shown.map(vcardHTML).join('');
  } else {
    tbody.innerHTML=shown.map(rowHTML).join('');
  }
  var more=document.getElementById('vmore');
  if(more){
    if(!filtering && total>VPREVIEW){
      more.innerHTML='<button type="button" class="vmore-btn" aria-expanded="'+(vExpanded?'true':'false')+'" aria-controls="'+(IS_MOBILE?'vcards':'vtable')+'" style="width:100%;min-height:44px;background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:10px;padding:12px 18px;font-size:13px;font-weight:600;cursor:pointer;margin-top:10px">'+(vExpanded?('▴ Show top '+VPREVIEW+' only'):('▾ Show all '+total+' partners'))+'</button>';
    } else { more.innerHTML=''; }
  }
}
function syncSortAria(){document.querySelectorAll('#vtable thead tr:first-child th[data-k]').forEach(function(th){th.setAttribute('aria-sort', th.dataset.k===sortK?(sortAsc?'ascending':'descending'):'none');});}
document.querySelectorAll('#vtable thead tr:first-child th').forEach(function(th){
  var k=th.dataset.k; if(!k) return;
  th.tabIndex=0; th.setAttribute('role','button'); th.setAttribute('aria-sort','none');
  function doSort(){if(sortK===k)sortAsc=!sortAsc;else{sortK=k;sortAsc=(k==='rank'||k==='name'||k==='category'||k==='tier'||k==='niche');}draw();syncSortAria();}
  th.onclick=doSort;
  th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '||e.key==='Spacebar'){e.preventDefault();doSort();}});
});
syncSortAria();
['input','change'].forEach(e=>{document.getElementById('search').addEventListener(e,draw);nicheSel.addEventListener(e,draw);catSel.addEventListener(e,draw);tierSel.addEventListener(e,draw);typeSel.addEventListener(e,draw);});
(function(){var m=document.getElementById('vmore');if(m)m.addEventListener('click',function(e){var b=e.target.closest('.vmore-btn');if(!b)return;vExpanded=!vExpanded;draw();if(!vExpanded){var h=document.getElementById('allpartners');if(h)h.scrollIntoView({block:'start'});}});})();
draw();
// Print / Save-as-PDF: temporarily render the FULL partner list (table + cards)
// so the PDF captures all partners, not just the top-VPREVIEW preview. Restore
// the on-screen expand state afterward.
(function(){var wasExp;function expand(){wasExp=vExpanded;vExpanded=true;draw();}function restore(){vExpanded=wasExp;draw();}
  window.addEventListener('beforeprint',expand);window.addEventListener('afterprint',restore);
  if(window.matchMedia){var mq=window.matchMedia('print');var h=function(e){if(e.matches)expand();else restore();};if(mq.addEventListener)mq.addEventListener('change',h);else if(mq.addListener)mq.addListener(h);}
})();

// Summit News feed (news-only view, ?view=news). Category buckets + relevance /
// vendor filters. Newest first. Every field esc()-escaped; URLs http(s)-only.
(function(){
  var feed=document.getElementById('newsfeed'), metaEl=document.getElementById('newsMeta'),
      relSel=document.getElementById('newsRel'), venSel=document.getElementById('newsVendor'),
      empty=document.getElementById('newsEmpty'), bucketBar=document.getElementById('newsBuckets');
  if(!feed) return;
  var news=(DATA.news||[]).slice();
  var tierOf={}; (DATA.vendors||[]).forEach(function(v){tierOf[v.name]=v.tier;});
  var relRank={high:3,medium:2,low:1}, relColor={high:'#34d399',medium:'#fbbf24',low:'#64748b'};
  function bucketOf(n){
    var t=((n.headline||'')+' '+(n.summary||'')).toLowerCase();
    if(/partner of the year|\baward|recogniz/.test(t)) return 'Awards';
    if(/acqui|\bmerger|\bmerge\b|raises|\bfunding|series [a-e]\b|seed round|invest/.test(t)) return 'M&A / Funding';
    if(/marketplace|native app|integrat|connector|interoperab/.test(t)) return 'Integrations';
    if(/launch|unveil|introduc|\bdebut|general availability|announces? new|new .{0,20}capabilit/.test(t)) return 'Launches';
    if(/\bbooth|session|reception|happy hour|keynote|\bevent|exhibit|aperitif/.test(t)) return 'Events & Booths';
    if(/partnership|collaborat|\bjoins\b|expand|alliance/.test(t)) return 'Partnerships';
    return 'Other';
  }
  var BUCKETS=['Awards','Launches','Integrations','Partnerships','Events & Booths','M&A / Funding','Other'];
  var ICON={'Awards':'🏆','Launches':'🚀','Integrations':'🔗','Partnerships':'🤝','Events & Booths':'📅','M&A / Funding':'💰','Other':'•'};
  news.forEach(function(n){ n._bucket=bucketOf(n); });
  news.sort(function(a,b){var d=String(b.date||'').localeCompare(String(a.date||''));return d!==0?d:((relRank[b.relevance]||0)-(relRank[a.relevance]||0));});
  Array.from(new Set(news.map(function(n){return n.vendor;}))).sort().forEach(function(v){venSel.add(new Option(v,v));});
  var bucketFilter='';
  if(bucketBar){
    var counts={}; news.forEach(function(n){counts[n._bucket]=(counts[n._bucket]||0)+1;});
    var chips=[['','All',news.length]].concat(BUCKETS.filter(function(b){return counts[b];}).map(function(b){return [b, ICON[b]+' '+b, counts[b]];}));
    bucketBar.innerHTML=chips.map(function(c){return '<button type="button" class="bchip'+(c[0]===''?' on':'')+'" data-b="'+esc(c[0])+'" aria-pressed="'+(c[0]===''?'true':'false')+'">'+esc(c[1])+' <span class="bc">'+c[2]+'</span></button>';}).join('');
    bucketBar.querySelectorAll('.bchip').forEach(function(ch){ch.addEventListener('click',function(){bucketFilter=ch.getAttribute('data-b');bucketBar.querySelectorAll('.bchip').forEach(function(x){x.classList.remove('on');x.setAttribute('aria-pressed','false');});ch.classList.add('on');ch.setAttribute('aria-pressed','true');render();});});
  }
  function render(){
    var rf=relSel.value, vf=venSel.value, min=relRank[rf]||0;
    var rows=news.filter(function(n){return (!rf||(relRank[n.relevance]||0)>=min)&&(!vf||n.vendor===vf)&&(!bucketFilter||n._bucket===bucketFilter);});
    metaEl.textContent = news.length ? (rows.length+' of '+news.length+' announcement'+(news.length===1?'':'s')+(DATA.news_generated?(' · gathered '+DATA.news_generated):'')) : 'Feed is empty';
    empty.style.display = rows.length?'none':'block';
    feed.style.display = rows.length?'block':'none';
    feed.innerHTML = rows.map(function(n){
      var t=tierOf[n.vendor]||'C';
      var safe=(n.url && /^https?:\/\//i.test(n.url)) ? n.url : '';
      var head=safe?('<a href="'+esc(safe)+'" target="_blank" rel="noopener noreferrer" title="Opens in a new tab">'+esc(n.headline)+'<span class="ext" aria-hidden="true">↗</span></a>'):esc(n.headline);
      return '<div class="nitem">'+
        '<div class="nd">'+esc(n.date||'')+'</div>'+
        '<div class="nbody">'+
          '<div class="nh"><span class="rdot" style="background:'+(relColor[n.relevance]||'#64748b')+'"></span>'+head+'</div>'+
          '<div class="nmeta"><span class="tag '+tierClass(t)+'">'+esc(n.vendor)+' · '+t+'</span><span>'+esc(n._bucket)+'</span><span>'+esc(n.source||'')+'</span></div>'+
          (n.summary?('<div class="nsum">'+esc(n.summary)+'</div>'):'')+
        '</div></div>';
    }).join('');
  }
  ['change','input'].forEach(function(e){relSel.addEventListener(e,render);venSel.addEventListener(e,render);});
  render();
})();

// Name look-up typeahead on the search box — suggests matching vendor names as
// you type; click / Enter jumps the table to that vendor. (draw() still filters
// the table live; this is the by-name lookup on top.)
(function(){
  var inp=document.getElementById('search'); if(!inp) return;
  var wrap=inp.closest('.searchwrap')||inp.parentNode;
  var box=document.createElement('div'); box.className='sugbox'; wrap.appendChild(box);
  var items=[], act=-1;
  function close(){box.classList.remove('on');box.innerHTML='';items=[];act=-1;}
  function pick(v){var set=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;set.call(inp,v.name);inp.dispatchEvent(new Event('input',{bubbles:true}));close();var t=document.getElementById('vtable');if(t)t.scrollIntoView({behavior:'smooth',block:'start'});}
  function hi(){[].forEach.call(box.querySelectorAll('.sug[data-i]'),function(el){var on=(+el.dataset.i===act);el.classList.toggle('act',on);if(on)el.scrollIntoView({block:'nearest'});});}
  function build(){
    var q=inp.value.trim().toLowerCase();
    if(!q){close();return;}
    var m=DATA.vendors.filter(function(v){return (v.name||'').toLowerCase().includes(q);});
    m.sort(function(a,b){var ai=(a.name||'').toLowerCase().indexOf(q),bi=(b.name||'').toLowerCase().indexOf(q);if(ai!==bi)return ai-bi;return (b.overall_score||0)-(a.overall_score||0);});
    items=m.slice(0,8); act=-1;
    if(!items.length){box.innerHTML='<div class="sug" style="cursor:default;color:var(--muted)">No vendor matches</div>';box.classList.add('on');return;}
    box.innerHTML=items.map(function(v,i){return '<div class="sug" data-i="'+i+'"><span class="nm">'+esc(v.name)+' <span class="tag '+tierClass(v.tier)+'">'+esc(v.tier)+'</span></span><span class="meta">'+esc(v.niche)+' &middot; '+(v.overall_score!=null?v.overall_score:'')+'</span></div>';}).join('');
    box.classList.add('on');
    [].forEach.call(box.querySelectorAll('.sug[data-i]'),function(el){el.addEventListener('mousedown',function(e){e.preventDefault();pick(items[+el.dataset.i]);});});
  }
  inp.addEventListener('input',build);
  inp.addEventListener('focus',function(){if(inp.value.trim())build();});
  inp.addEventListener('keydown',function(e){
    if(!box.classList.contains('on'))return;
    if(e.key==='ArrowDown'){act=Math.min(act+1,items.length-1);hi();e.preventDefault();}
    else if(e.key==='ArrowUp'){act=Math.max(act-1,0);hi();e.preventDefault();}
    else if(e.key==='Enter'){if(act>=0&&items[act]){pick(items[act]);e.preventDefault();}}
    else if(e.key==='Escape'){close();}
  });
  document.addEventListener('mousedown',function(e){if(!wrap.contains(e.target))close();});
})();

// ----- Magic Quadrant + niche drill-down -----
(function(){
  const V=DATA.vendors, SEGS=DATA.mq_segments||[];
  const ALL={label:'All Partners',n:V.length,tx:DATA.mq.tx,ty:DATA.mq.ty,drillable:true,all:true};
  const byLabel={}; SEGS.forEach(s=>byLabel[s.label]=s);
  const qFill={'Leaders':'rgba(52,211,153,.07)','Challengers':'rgba(41,181,232,.06)','Visionaries':'rgba(167,139,250,.06)','Niche Players':'rgba(100,116,139,.05)'};
  let active=ALL, mqCross={tx:ALL.tx,ty:ALL.ty};
  const quadOf=(v,s)=>{const he=v.mq_execute>=s.ty,hv=v.mq_vision>=s.tx;return he&&hv?'Leaders':he?'Challengers':hv?'Visionaries':'Niche Players';};
  const vendorsIn=s=>s.all?V:V.filter(v=>v.mq_segment===s.label);
  const sel=document.getElementById('mqSegSel');
  const mkOpt=(val,txt)=>{const o=document.createElement('option');o.value=val;o.textContent=txt;sel.appendChild(o);};
  mkOpt('All Partners','All Partners ('+V.length+')');
  SEGS.forEach(s=>mkOpt(s.label, s.label+' ('+s.n+')'+(s.drillable?'':' — flat')));
  const legend=document.getElementById('mqLegend'), crumb=document.getElementById('mqCrumb'),
        back=document.getElementById('mqBack'), caveat=document.getElementById('mqCaveat');
  // Quadrant filter chips — show/hide each quadrant's dots (same cross; a visual
  // filter, NOT a re-quadrant). All four segments — Leaders, Challengers,
  // Visionaries, Niche Players — are enabled by default; chips toggle each off.
  const QUADS=['Leaders','Challengers','Visionaries','Niche Players'];
  const qDot={'Leaders':'#34d399','Challengers':'#29b5e8','Visionaries':'#a78bfa','Niche Players':'#64748b'};
  let visQ=new Set(['Leaders','Challengers','Visionaries','Niche Players']);
  const chipsEl=document.getElementById('mqQuadChips');
  // Tier filter chips — a SECOND, independent row. Quadrant = where a partner
  // sits; Tier = the dot's colour (Bryan's scouting priority). Data has no 'D'.
  const TIERS=['A','B','C'];
  let visT=new Set(['A','B','C']);
  const tierChipsEl=document.getElementById('mqTierChips');
  function renderChips(cc){
    chipsEl.innerHTML=QUADS.map(function(q){var on=visQ.has(q);return '<button type="button" data-q="'+q+'" title="Show/hide the '+q+' quadrant (does not change dot colour/tier)" style="display:inline-flex;align-items:center;gap:6px;background:'+(on?'var(--panel2)':'transparent')+';border:1px solid '+(on?qDot[q]:'var(--border)')+';color:'+(on?'var(--text)':'var(--muted)')+';border-radius:14px;padding:4px 11px;font-size:12px;cursor:pointer;opacity:'+(on?'1':'.55')+'"><span style="width:11px;height:9px;border-radius:2px;background:'+qDot[q]+'33;border:1px solid '+qDot[q]+';display:inline-block"></span>'+q+' <b style="color:'+(on?'var(--text)':'var(--muted)')+'">'+(cc[q]||0)+'</b></button>';}).join('');
    chipsEl.querySelectorAll('button').forEach(function(b){b.onclick=function(){var q=this.dataset.q; if(visQ.has(q))visQ.delete(q); else visQ.add(q); render(active);};});
  }
  function renderTierChips(tc){tierChipsEl.innerHTML=TIERS.map(function(t){var on=visT.has(t);var col=tierColor(t);return '<button type="button" data-t="'+t+'" title="Show/hide Tier '+t+' partners (their dot colour)" style="display:inline-flex;align-items:center;gap:6px;background:'+(on?'var(--panel2)':'transparent')+';border:1px solid '+(on?col:'var(--border)')+';color:'+(on?'var(--text)':'var(--muted)')+';border-radius:14px;padding:4px 11px;font-size:12px;cursor:pointer;opacity:'+(on?'1':'.55')+'"><span style="width:9px;height:9px;border-radius:50%;background:'+col+';display:inline-block"></span>Tier '+t+' <b style="color:'+(on?'var(--text)':'var(--muted)')+'">'+(tc[t]||0)+'</b></button>';}).join('');tierChipsEl.querySelectorAll('button').forEach(function(b){b.onclick=function(){var t=this.dataset.t;if(visT.has(t))visT.delete(t);else visT.add(t);render(active);};});}
  const mqPlugin={id:'mqQuad',
    beforeDraw(ch){
      const a=ch.chartArea, x=ch.scales.x, y=ch.scales.y; if(!a)return;
      const ctx=ch.ctx, cx=x.getPixelForValue(mqCross.tx), cy=y.getPixelForValue(mqCross.ty);
      ctx.save();
      ctx.fillStyle=qFill['Leaders'];      ctx.fillRect(cx,a.top,a.right-cx,cy-a.top);
      ctx.fillStyle=qFill['Challengers'];  ctx.fillRect(a.left,a.top,cx-a.left,cy-a.top);
      ctx.fillStyle=qFill['Visionaries'];  ctx.fillRect(cx,cy,a.right-cx,a.bottom-cy);
      ctx.fillStyle=qFill['Niche Players'];ctx.fillRect(a.left,cy,cx-a.left,a.bottom-cy);
      ctx.strokeStyle='#2c3e60';ctx.lineWidth=1;ctx.setLineDash([5,4]);
      ctx.beginPath();ctx.moveTo(cx,a.top);ctx.lineTo(cx,a.bottom);ctx.moveTo(a.left,cy);ctx.lineTo(a.right,cy);ctx.stroke();
      ctx.setLineDash([]);
      // Phones: pin the quadrant corner labels tighter, smaller and fainter so
      // they recede behind the dots. Desktop keeps 12px / full opacity / 10-8 inset.
      var cf=IS_MOBILE?9:12, ca=IS_MOBILE?0.45:1, ci=IS_MOBILE?4:10, cj=IS_MOBILE?4:8;
      ctx.font='800 '+cf+'px -apple-system,BlinkMacSystemFont,sans-serif';
      ctx.fillStyle='rgba(52,211,153,'+(0.85*ca)+')'; ctx.textAlign='right';ctx.textBaseline='top';   ctx.fillText('LEADERS',a.right-ci,a.top+cj);
      ctx.fillStyle='rgba(41,181,232,'+(0.85*ca)+')'; ctx.textAlign='left'; ctx.textBaseline='top';    ctx.fillText('CHALLENGERS',a.left+ci,a.top+cj);
      ctx.fillStyle='rgba(167,139,250,'+(0.9*ca)+')'; ctx.textAlign='right';ctx.textBaseline='bottom'; ctx.fillText('VISIONARIES',a.right-ci,a.bottom-cj);
      ctx.fillStyle='rgba(148,163,184,'+(0.95*ca)+')';ctx.textAlign='left'; ctx.textBaseline='bottom'; ctx.fillText('NICHE PLAYERS',a.left+ci,a.bottom-cj);
      ctx.restore();
    },
    afterDatasetsDraw(ch){
      const ctx=ch.ctx, x=ch.scales.x, y=ch.scales.y, a=ch.chartArea;
      ctx.save();ctx.font='600 '+(IS_MOBILE?9:10)+'px -apple-system,BlinkMacSystemFont,sans-serif';ctx.fillStyle='rgba(232,238,255,.92)';ctx.textBaseline='middle';
      let lab=vendorsIn(active).filter(v=>v.tier==='A' && visT.has(v.tier) && visQ.has(quadOf(v,active)));
      if(!active.all && lab.length===0) lab=vendorsIn(active).filter(v=>visT.has(v.tier) && visQ.has(quadOf(v,active))).sort((p,q)=>(q.overall_score||0)-(p.overall_score||0)).slice(0,3);
      if(IS_MOBILE){
        // Collision-avoid: draw highest-overall first and skip any label that would
        // overlap one already placed (right-edge labels flip left so they can't clip).
        // Unlabeled dots stay tappable for their detail — keeps the Leaders cluster legible.
        var drawn=[], lh=11;
        lab.slice().sort((p,q)=>(q.overall_score||0)-(p.overall_score||0)).forEach(function(v){
          var px=x.getPixelForValue(v.mq_x),py=y.getPixelForValue(v.mq_y),w=ctx.measureText(v.name).width;
          var left=!!(a&&px+6+w>a.right), rx=left?px-6-w:px+6, ry=py-lh/2, k;
          for(k=0;k<drawn.length;k++){var d=drawn[k];if(rx<d.x+d.w+3&&rx+w+3>d.x&&ry<d.y+d.h+2&&ry+lh+2>d.y)return;}
          drawn.push({x:rx,y:ry,w:w,h:lh});
          ctx.textAlign=left?'right':'left';ctx.fillText(left?(v.name+' '):(' '+v.name),left?px-6:px+6,py);
        });
      } else {
        ctx.textAlign='left';
        lab.forEach(v=>ctx.fillText(' '+v.name, x.getPixelForValue(v.mq_x)+5, y.getPixelForValue(v.mq_y)));
      }
      ctx.restore();
    }
  };
  function datasetsFor(s){
    const vs=vendorsIn(s);
    return ['A','B','C','D'].map(t=>({label:'Tier '+t,
      data:vs.filter(v=>v.tier===t && visT.has(v.tier) && visQ.has(quadOf(v,s))).map(v=>({x:v.mq_x,y:v.mq_y,name:v.name,tier:v.tier,ov:v.overall_score,cat:v.category,ex:v.mq_execute,vi:v.mq_vision,q:quadOf(v,s)})),
      backgroundColor:tierColor(t)+'cc',borderColor:tierColor(t),borderWidth:1,pointRadius:t==='A'?6:3.5,pointHoverRadius:8})).filter(d=>d.data.length);
  }
  var mqVals=[]; V.forEach(function(v){mqVals.push(v.mq_x,v.mq_y);});
  var mqLo=Math.max(0,Math.floor(Math.min.apply(null,mqVals.length?mqVals:[3])));
  const chart=new Chart(document.getElementById('mqChart'),{type:'scatter',data:{datasets:datasetsFor(ALL)},
    options:{maintainAspectRatio:false,animation:false,
      // Phones: tap a dot to open its full detail sheet (labels are sparse on
      // mobile, so this is how you identify an unlabeled dot). Desktop unchanged.
      onClick:IS_MOBILE?function(e,els,ch){if(els&&els.length){var d=ch.data.datasets[els[0].datasetIndex].data[els[0].index];if(d&&d.name&&window.summitOpenVendor)window.summitOpenVendor(d.name);}}:undefined,
      layout:{padding:{right:IS_MOBILE?12:0}},
      plugins:{legend:{labels:{color:C.tick,usePointStyle:true}},
        tooltip:{displayColors:false,padding:16,titleFont:{size:18,weight:'700'},bodyFont:{size:16},bodySpacing:7,callbacks:{
          title:c=>c[0].raw.name,
          label:c=>['Tier '+c.raw.tier+' · '+c.raw.q,'Overall  '+c.raw.ov+' / 10','Execute '+c.raw.ex+'  ·  Vision '+c.raw.vi, c.raw.cat]}}},
      scales:{
        x:{min:mqLo,max:10,title:{display:true,text:'Completeness of Vision  →   (AI + IPO/Upside)',color:'#aab6c9',font:{weight:'700'}},ticks:{color:C.tick},grid:{color:C.grid}},
        y:{min:mqLo,max:10,title:{display:true,text:'Ability to Execute  →   (Snowflake + Retail)',color:'#aab6c9',font:{weight:'700'}},ticks:{color:C.tick},grid:{color:C.grid}}}},
    plugins:[mqPlugin]});
  function render(s){
    active=s; mqCross={tx:s.tx,ty:s.ty};
    chart.data.datasets=datasetsFor(s); chart.update();
    const vs=vendorsIn(s); const cc={Leaders:0,Challengers:0,Visionaries:0,'Niche Players':0}, tc={A:0,B:0,C:0};
    vs.forEach(function(v){var q=quadOf(v,s); if(visT.has(v.tier))cc[q]++; if(visQ.has(q))tc[v.tier]=(tc[v.tier]||0)+1;});
    renderChips(cc); renderTierChips(tc);
    legend.innerHTML=(s.all?'All '+V.length+' partners':s.n+' partners in '+esc(s.label))+
      ' · cross at '+(s.all?'fleet':'niche')+' avg — Vision <b style="color:var(--text)">'+s.tx+'</b> · Execute <b style="color:var(--text)">'+s.ty+'</b> &nbsp;·&nbsp; <span style="color:var(--muted)"><b style="color:var(--text)">Position</b> = quadrant — where a partner sits. <b style="color:var(--text)">Dot colour</b> = scouting tier (<b style="color:'+tierColor('A')+'">A</b>/<b style="color:'+tierColor('B')+'">B</b>/<b style="color:'+tierColor('C')+'">C</b>). Toggle a <b>Tier</b> chip to hide a colour (e.g. hide Tier B to drop the yellow dots); toggle a <b>Quadrant</b> chip to hide a region. They are independent — many Leaders are Tier B.</span>';
    crumb.innerHTML = s.all?'' : '&nbsp; All Partners › <b style="color:var(--text)">'+esc(s.label)+'</b>';
    back.style.display = s.all?'none':'';
    caveat.innerHTML = s.all ? '' : (s.drillable
      ? '<b>Niche view.</b> The cross is recomputed to this niche cohort average, so positions are relative to '+esc(s.label)+' — a Leader here may sit elsewhere on the all-partners quadrant.'+((s.n<10||s.skew>0.7)?' <b style="color:#fbbf24">Small or lopsided cohort</b> ('+s.n+' partners, '+Math.round(s.skew*100)+'% in one quadrant) — interpret with care.':'')
      : '⚠ <b>'+esc(s.label)+' is a flat / directional cohort</b> ('+s.n+' partners with little score spread; many carry template scores). Read it as a ranked list rather than a true quadrant.');
    if(sel.value!==(s.all?'All Partners':s.label)) sel.value=s.all?'All Partners':s.label;
  }
  sel.addEventListener('change',()=>render(sel.value==='All Partners'?ALL:(byLabel[sel.value]||ALL)));
  back.addEventListener('click',()=>render(ALL));
  render(ALL);
})();

// Scoring-info popover — the upper-right "ⓘ Scoring" link defines each score component.
(function(){
  var pop=document.getElementById('scorePop'); if(!pop) return;
  var triggers=document.querySelectorAll('.scoreInfoTrigger'); if(!triggers.length) return;
  var x=document.getElementById('scorePopX');
  function setExp(v){Array.prototype.forEach.call(triggers,function(b){b.setAttribute('aria-expanded',String(v));});}
  function open(){pop.hidden=false;setExp(true);if(x)x.focus();}
  function close(){pop.hidden=true;setExp(false);}
  Array.prototype.forEach.call(triggers,function(b){b.addEventListener('click',function(e){e.stopPropagation();if(pop.hidden)open();else close();});});
  if(x) x.addEventListener('click',close);
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&!pop.hidden)close();});
  document.addEventListener('click',function(e){if(pop.hidden)return;if(pop.contains(e.target))return;if(e.target.classList&&e.target.classList.contains('scoreInfoTrigger'))return;close();});
})();

// ----- Interactive Basecamp floor map (?view=map): spatial floor-plan + zone columns -----
(function(){
  var plan=document.getElementById('mapPlan'), row=document.getElementById('mapRow');
  if(!plan && !row) return;
  var FP; try{ FP=(/*__FLOORPLAN__*/); }catch(e){ FP={regions:[]}; }
  var REG=(FP&&FP.regions)||[], CH=(FP&&FP.canvasH)||62;
  // Official non-vendor Snowflake spaces from the printed Basecamp board.
  var SPACES=[
    ['AI Pop-Up',6101],['Basecamp South Theater 1',1001],['Basecamp South Theater 2',1017],
    ['Basecamp South Theater 3',2901],['Basecamp South Theater 4',2911],['Battle for the Snowflake AI Dataverse',6005],
    ['Braindate Lounge',1007],['Builders Hub',6001],['Builders Hub Theater',6004],
    ['Customer Spotlights Check-In & Photography',1224],['Customer Spotlights Recording Studio',1225],['Data Cloud Now',1601],
    ['Data Superheroes Lounge',7002],['Hands-On Challenges',6006],['Hands-On Labs 01',7101],
    ['Hands-On Labs 02',7102],['Hands-On Labs 03',7103],['Keynote',8000],['Major League Hacking Zone',6003],
    ['Meeting Village',4001],['Olympic & Paralympic Zone',6002],['Platform Peak',7001],['Startup Lodge',2700],
    ['theCUBE',1417],['Vertical Village',2002],['Vertical Village Theater 1',2003],['Vertical Village Theater 2',2004]
  ];
  function digits(b){var m=String(b==null?'':b).match(/\d+/);return m?parseInt(m[0],10):NaN;}
  function zoneOf(b){var n=digits(b);return isNaN(n)?null:Math.floor(n/100)*100;}
  var byZone={}, total=(DATA.vendors||[]).length, placedV=0, must=0;
  (DATA.vendors||[]).forEach(function(v){var z=zoneOf(v.booth);if(z!=null){placedV++;if(v.tier==='A')must++;(byZone[z]=byZone[z]||[]).push({b:digits(v.booth),kind:'v',v:v});}});
  SPACES.forEach(function(s){var z=zoneOf(s[1]);if(z!=null)(byZone[z]=byZone[z]||[]).push({b:s[1],kind:'s',name:s[0]});});
  Object.keys(byZone).forEach(function(z){byZone[z].sort(function(a,b){return a.b-b.b;});});
  var zkeys=Object.keys(byZone).map(Number).sort(function(a,b){return a-b;});

  if(row){
    row.innerHTML=zkeys.map(function(z){
      var cells=byZone[z].map(function(it){
        if(it.kind==='s') return '<div class="booth space"><div class="bn">'+it.b+'</div><div class="bnm">❄ '+esc(it.name)+'</div></div>';
        var v=it.v,isM=(v.tier==='A');
        return '<div class="booth t'+esc(v.tier||'C')+(isM?' must':'')+'" data-v="'+esc(v.name)+'" tabindex="0" role="button" aria-label="Booth '+esc(fmt(v.booth))+': '+esc(v.name)+', tier '+esc(v.tier)+', overall '+esc(fmt(v.overall_score))+' of 10" title="Click for full company detail">'+
          '<div class="bn">#'+esc(fmt(v.booth))+(isM?' ⭐':'')+'</div><div class="bnm">'+esc(v.name)+'</div>'+
          '<div class="bt" style="color:'+tierColor(v.tier)+'">'+esc(v.tier)+' · '+esc(fmt(v.overall_score))+'/10</div></div>';
      }).join('');
      return '<div class="zonecol"><div class="zonehd">'+z+'s</div>'+cells+'</div>';
    }).join('');
  }

  if(plan && REG.length){
    function pos(r){return 'left:'+r.x+'%;top:'+(r.y/CH*100).toFixed(2)+'%;width:'+r.w+'%;height:'+(r.h/CH*100).toFixed(2)+'%';}
    var html='';
    // big container areas first (faint, behind, small corner label)
    REG.forEach(function(r){
      if(r.kind==='booth-zone' || (r.w*r.h)<=250) return;
      html+='<div class="planregion plancontainer" style="'+pos(r)+'"><span class="cl">'+esc(r.label)+'</span></div>';
    });
    // feature boxes (theaters, lounges, concourse rooms, large named anchors)
    REG.forEach(function(r){
      if(r.kind==='booth-zone' || (r.w*r.h)>250) return;
      if(r.kind==='anchor' && (r.w*r.h)<40) return;
      var kc=({theater:'k-theater',lounge:'k-lounge',concourse:'k-concourse',anchor:'k-anchor',structure:'k-structure','snowflake-space':'k-concourse'})[r.kind]||'k-structure';
      html+='<div class="planregion planmark '+kc+'" style="'+pos(r)+'" title="'+esc(r.label)+'">'+esc(r.label)+'</div>';
    });
    REG.forEach(function(r){
      if(r.kind!=='booth-zone') return;
      var z=digits(r.key), list=byZone[z];
      if(!list||!list.length) return;
      var dots=list.map(function(it){
        if(it.kind==='s') return '<span class="planbooth space" style="background:#1c4a6e" title="❄ '+esc(it.name)+'"></span>';
        var v=it.v,isM=(v.tier==='A');
        return '<span class="planbooth'+(isM?' must':'')+'" data-v="'+esc(v.name)+'" tabindex="0" role="button" style="background:'+tierColor(v.tier)+'" title="#'+esc(fmt(v.booth))+' '+esc(v.name)+' — '+esc(v.tier)+' '+esc(fmt(v.overall_score))+'/10" aria-label="Booth '+esc(fmt(v.booth))+': '+esc(v.name)+', tier '+esc(v.tier)+'"></span>';
      }).join('');
      html+='<div class="planregion planzone" style="'+pos(r)+'"><span class="zl">'+esc(r.label)+'</span><div class="dots">'+dots+'</div></div>';
    });
    plan.innerHTML=html;
  }

  document.getElementById('mapLegend').innerHTML=
    '<span class="lg"><span class="sw" style="background:'+tierColor('A')+'"></span> Tier A ⭐ must-see</span>'+
    '<span class="lg"><span class="sw" style="background:'+tierColor('B')+'"></span> Tier B</span>'+
    '<span class="lg"><span class="sw" style="background:'+tierColor('C')+'"></span> Tier C</span>'+
    '<span class="lg"><span class="sw" style="background:#1c4a6e;border-color:#2c5f86"></span> ❄ Snowflake space</span>';
  document.getElementById('mapFoot').innerHTML=
    '<b style="color:var(--text)">'+placedV+'</b> of '+total+' scouted partners placed'+((total-placedV)>0?(' ('+(total-placedV)+' without a numeric booth omitted)'):'')+
    ' · <b style="color:var(--text)">'+must+'</b> Tier-A must-sees ⭐ · '+SPACES.length+' Snowflake spaces ❄. '+
    '<b>Floor plan</b> approximates the printed Basecamp board (booths shown as dots in their zone); <b>Zone columns</b> lists every booth. Click any partner for full detail; cross-check against the original photo below.';

  var si=document.getElementById('mapSearch');
  if(si) si.addEventListener('input',function(){
    var q=si.value.trim().toLowerCase();
    Array.prototype.forEach.call(document.querySelectorAll('#mapView .booth[data-v],#mapView .planbooth[data-v]'),function(el){
      var hit=!q||(el.getAttribute('data-v')||'').toLowerCase().indexOf(q)>=0;
      el.classList.toggle('dim',!hit);
      if(q&&!hit){el.setAttribute('aria-hidden','true');el.setAttribute('tabindex','-1');}
      else{el.removeAttribute('aria-hidden');el.setAttribute('tabindex','0');}
    });
  });

  var tabPlan=document.getElementById('tabPlan'), tabCols=document.getElementById('tabCols'),
      planWrap=document.getElementById('mapPlanWrap'), colsWrap=document.getElementById('mapColsWrap'),
      colsNav=document.getElementById('mapColsNav'), slider=document.getElementById('mapColsSlider');
  var isMob=!!(window.matchMedia&&window.matchMedia('(max-width:640px)').matches);
  function syncSlider(){if(!slider||!colsWrap)return;var max=colsWrap.scrollWidth-colsWrap.clientWidth;slider.value=max>0?Math.round(colsWrap.scrollLeft/max*1000):0;}
  if(slider&&colsWrap){
    slider.addEventListener('input',function(){var max=colsWrap.scrollWidth-colsWrap.clientWidth;colsWrap.scrollLeft=max*(slider.value/1000);});
    colsWrap.addEventListener('scroll',syncSlider,{passive:true});
    colsWrap.addEventListener('wheel',function(e){if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){colsWrap.scrollLeft+=e.deltaY;e.preventDefault();}},{passive:false});
  }
  // MOBILE: the spatial floor plan becomes a pinch-zoom + pannable canvas (a CSS
  // transform on #mapPlan, clipped by #mapPlanWrap) with a Fit button. The booths
  // are tiny when the whole floor is fit to a phone, so zoom-in is the point.
  // Desktop keeps the original static plan untouched.
  var planReset=function(){};
  if(isMob && plan && planWrap){
    var pz={s:1,tx:0,ty:0}, pts=new Map(), last=null, pinch=null, moved=false;
    function applyT(){plan.style.transform='translate('+pz.tx+'px,'+pz.ty+'px) scale('+pz.s+')';}
    function rel(e){var r=planWrap.getBoundingClientRect();return {x:e.clientX-r.left,y:e.clientY-r.top};}
    function zoomTo(ns,cx,cy){ns=Math.min(6,Math.max(1,ns));var cX=(cx-pz.tx)/pz.s,cY=(cy-pz.ty)/pz.s;pz.s=ns;pz.tx=cx-cX*ns;pz.ty=cy-cY*ns;applyT();}
    planReset=function(){pz.s=1;pz.tx=0;pz.ty=0;applyT();};
    planWrap.addEventListener('pointerdown',function(e){pts.set(e.pointerId,rel(e));try{planWrap.setPointerCapture(e.pointerId);}catch(_){}moved=false;
      if(pts.size===1){last=rel(e);} else if(pts.size===2){var a=[...pts.values()];pinch={d:Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y),s:pz.s};}});
    planWrap.addEventListener('pointermove',function(e){if(!pts.has(e.pointerId))return;var p=rel(e);pts.set(e.pointerId,p);
      if(pts.size>=2&&pinch){var a=[...pts.values()];var d=Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y);if(pinch.d>0){zoomTo(pinch.s*d/pinch.d,(a[0].x+a[1].x)/2,(a[0].y+a[1].y)/2);moved=true;}}
      else if(pts.size===1&&last&&pz.s>1){pz.tx+=p.x-last.x;pz.ty+=p.y-last.y;last=p;moved=true;applyT();}
      else if(pts.size===1){last=p;}});
    function up(e){pts.delete(e.pointerId);if(pts.size<2)pinch=null;last=pts.size?[...pts.values()][0]:null;}
    planWrap.addEventListener('pointerup',up);planWrap.addEventListener('pointercancel',up);
    // A drag / pinch must not also fire the booth-detail click.
    plan.addEventListener('click',function(e){if(moved){e.stopPropagation();e.preventDefault();moved=false;}},true);
    var pf=document.getElementById('planFit'); if(pf) pf.addEventListener('click',planReset);
  }
  function show(p){
    if(planWrap) planWrap.style.display=p?'block':'none';
    if(colsWrap) colsWrap.style.display=p?'none':'block';
    if(colsNav) colsNav.style.display=(p||isMob)?'none':'block';
    if(tabPlan){tabPlan.classList.toggle('on',p);tabPlan.setAttribute('aria-pressed',String(p));}
    if(tabCols){tabCols.classList.toggle('on',!p);tabCols.setAttribute('aria-pressed',String(!p));}
    if(p&&isMob) planReset();
    if(!p&&!isMob) setTimeout(syncSlider,30);
  }
  if(tabPlan) tabPlan.addEventListener('click',function(){show(true);});
  if(tabCols) tabCols.addEventListener('click',function(){show(false);});
  // Mobile shows ONLY the spatial Floor plan (Zone columns is removed on phones);
  // desktop keeps both, defaulting to the plan when one exists.
  show(isMob ? true : !!(plan && REG.length));
})();

// Download / print to PDF — the @media print stylesheet restyles the page; the
// browser print dialog saves the whole dashboard (all sections + current MQ view).
(function(){document.querySelectorAll('.pdfBtn').forEach(function(b){b.addEventListener('click',function(){window.print();});});})();
(function(){var t=document.getElementById('toTop');if(!t)return;t.addEventListener('click',function(){window.scrollTo({top:0,behavior:'smooth'});});
  var onScroll=function(){t.classList.toggle('show',(window.scrollY||document.documentElement.scrollTop)>320);};
  window.addEventListener('scroll',onScroll,{passive:true});onScroll();})();

// Phones: the sticky header compacts (subtitle hides, bar slims) once you scroll,
// so it stays out of the way while keeping the chips / Back link reachable.
(function(){
  if(!(window.matchMedia&&window.matchMedia('(max-width:640px)').matches))return;
  var ticking=false;
  function on(){document.body.classList.toggle('compact',(window.scrollY||document.documentElement.scrollTop)>80);ticking=false;}
  window.addEventListener('scroll',function(){if(!ticking){ticking=true;requestAnimationFrame(on);}},{passive:true});on();
})();

// Vendor detail — click any vendor card or table row for full company info.
(function(){
  var m=document.createElement('div'); m.className='vmodal'; m.id='vModal';
  m.innerHTML='<div class="vsheet" role="dialog" aria-modal="true" aria-labelledby="vModalTitle" tabindex="-1"><button class="x" type="button" aria-label="Close">&times;</button><div id="vBody"></div></div>';
  document.body.appendChild(m);
  var body=m.querySelector('#vBody');
  var sheet=m.querySelector('.vsheet'), lastFocus=null;
  var byName={}; (DATA.vendors||[]).forEach(function(v){byName[v.name]=v;});
  function row(k,val){return (val==null||val==='')?'':'<div class="vrow"><span class="k">'+esc(k)+'</span><span class="vv">'+esc(val)+'</span></div>';}
  function open(name){
    var v=byName[name]; if(!v) return;
    var company=row('Valuation / Market cap',v.market_cap)+row('Funding raised',v.funding)+row('Round',v.round)+row('Revenue / ARR',v.revenue)+row('Employees',v.employees)+row('Key investors / owner',v.investors)+row('Ticker / parent',v.ticker_parent);
    var wd=v.wd||{};
    var wdrows=row('Founded',wd.founded)+row('Headquarters',wd.headquarters)+row('Industry',wd.industry);
    var wdlink=wd.wikidata_url?('<div class="vrow" style="border:none"><span class="vv" style="font-weight:400"><a class="dl" href="'+esc(wd.wikidata_url)+'" target="_blank" rel="noopener noreferrer">📚 View on Wikidata ↗</a></span></div>'):'';
    var scores=[['Bryan Recommend',v.bryan_score],['Snowflake',v.snowflake_score],['AI',v.ai_score],['Retail / Customer',v.retail_score],['IPO / Upside',v.ipo_score]]
      .map(function(s){return '<div class="vrow"><span class="k">'+s[0]+'</span><span class="vv">'+fmt(s[1])+' / 10</span></div>';}).join('')
      +'<div class="vrow"><span class="k">Overall</span><span class="vv">'+fmt(v.overall_score)+' / 10</span></div>';
    body.innerHTML='<h2 id="vModalTitle">'+esc(v.name)+'</h2> <span class="tag '+tierClass(v.tier)+'">'+esc(v.tier)+'</span> <span class="tag tNi">'+esc(fmt(v.niche))+'</span>'+
      '<div class="sub" style="margin-top:5px">'+esc(fmt(v.category))+' · booth '+esc(fmt(v.booth))+(v.company_type?(' · '+esc(v.company_type)):'')+'</div>'+
      '<div style="margin-top:11px"><a class="dl homedl" href="'+esc(homeUrl(v))+'" target="_blank" rel="noopener noreferrer" aria-label="'+(v.website?'Visit homepage':'Search the web for homepage')+', opens in a new tab">'+(v.website?'🌐 Visit homepage':'🔎 Find homepage')+' ↗</a></div>'+
      '<div class="vsec">Company</div>'+(company||'<div class="sub" style="padding:6px 0">No funding / valuation data on file yet.</div>')+
      ((wdrows||wdlink)?('<div class="vsec">Company facts <span class="sub" style="font-weight:400;font-size:11px">· via Wikidata</span></div>'+wdrows+wdlink):'')+
      '<div class="vsec">Scores</div>'+scores+
      (v.notes?('<div class="vsec">Notes</div><div class="vrow" style="border:none"><span class="vv" style="font-weight:400;line-height:1.55">'+esc(v.notes)+'</span></div>'):'')+
      (v.source?('<div class="sub" style="margin-top:12px;font-size:11px;word-break:break-word">Source: '+esc(v.source)+'</div>'):'');
    lastFocus=document.activeElement; m.classList.add('on'); document.body.style.overflow='hidden'; var xb=m.querySelector('.x'); (xb||sheet).focus();
  }
  function close(){m.classList.remove('on'); document.body.style.overflow=''; if(lastFocus&&lastFocus.focus){try{lastFocus.focus();}catch(_){}} lastFocus=null;}
  window.summitOpenVendor=open; // let the Magic-Quadrant dots (and other views) open the detail sheet
  // KPI tile pop-ups: reuse this overlay + focus-trap to show a filtered partner table.
  function openList(fk){
    var V=DATA.vendors||[];
    var list = fk==='all'?V.slice()
      : fk==='tierA'?V.filter(function(v){return v.tier==='A';})
      : fk==='tierAB'?V.filter(function(v){return v.tier==='A'||v.tier==='B';})
      : fk==='public'?V.filter(function(v){return /public/i.test(v.company_type||'');})
      : fk==='private'?V.filter(function(v){return !/public/i.test(v.company_type||'');})
      : V.slice();
    var titles={all:'All Partner Vendors',tierA:'Must-See — Tier A',tierAB:'Priority — Tier A + B',public:'Public Companies',private:'Private / Other'};
    list.sort(function(a,b){return (a.rank||1e9)-(b.rank||1e9);});
    var rows=list.map(function(v){
      // Rows are clickable (data-v) so a tap opens the full detail sheet — that's
      // the only way to recover Niche/Booth on phones, where .col-opt is hidden.
      return '<tr data-v="'+esc(v.name)+'" role="button" tabindex="0" style="cursor:pointer" aria-label="View company detail for '+esc(v.name)+'"><td class="num">'+v.rank+'</td><td class="name">'+esc(v.name)+'</td>'+
        '<td class="col-opt"><span class="tag tNi">'+esc(fmt(v.niche))+'</span></td><td class="col-opt">'+esc(fmt(v.booth))+'</td>'+
        '<td class="num"><b>'+fmt(v.overall_score)+'</b></td><td><span class="tag '+tierClass(v.tier)+'">'+esc(v.tier)+'</span></td></tr>';
    }).join('');
    body.innerHTML='<h2 id="vModalTitle">'+esc(titles[fk]||'Partners')+'</h2> <span class="tag tNi">'+list.length+' partners</span>'+
      '<div style="max-height:60vh;overflow:auto;margin-top:13px;border:1px solid var(--border);border-radius:10px">'+
      '<table><thead><tr><th class="num">#</th><th>Partner</th><th class="col-opt">Niche</th><th class="col-opt">Booth</th><th class="num">Overall</th><th>Tier</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
    lastFocus=document.activeElement; m.classList.add('on'); document.body.style.overflow='hidden'; var xb=m.querySelector('.x'); (xb||sheet).focus();
  }
  window.summitOpenList=openList;
  m.addEventListener('click',function(e){if(e.target===m||e.target.classList.contains('x'))close();});
  document.addEventListener('keydown',function(e){
    if(!m.classList.contains('on'))return;
    if(e.key==='Escape'){close();return;}
    if(e.key==='Tab'){
      var f=Array.prototype.filter.call(sheet.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'),function(el){return el.offsetParent!==null&&!el.disabled;});
      if(!f.length){e.preventDefault();return;}
      var first=f[0],last=f[f.length-1];
      if(!sheet.contains(document.activeElement)){first.focus();e.preventDefault();}
      else if(e.shiftKey&&document.activeElement===first){last.focus();e.preventDefault();}
      else if(!e.shiftKey&&document.activeElement===last){first.focus();e.preventDefault();}
    }
  });
  document.addEventListener('click',function(e){var el=e.target.closest&&e.target.closest('[data-v]');if(el){var n=el.getAttribute('data-v');if(n&&byName[n])open(n);}});
  document.addEventListener('keydown',function(e){
    if(m.classList.contains('on'))return;
    if(e.key==='Enter'||e.key===' '||e.key==='Spacebar'){
      var a=document.activeElement, el=a&&a.closest&&a.closest('[data-v]');
      if(el&&el===a){var n=el.getAttribute('data-v');if(n&&byName[n]){e.preventDefault();open(n);}}
    }
  });
})();

document.getElementById('scoreDefBody').innerHTML =
  `<b>Scoring:</b> all scores are ${esc(DATA.meta.owner||'the owner')}'s directional 0–10 ratings from the scouting workbook — `+
  `Bryan Recommend (career/networking fit, baselined against a sample of 30 vendor booth selling pitches), Snowflake relevance, AI relevance, retail/customer-analytics relevance, and IPO/upside — `+
  `blended into an <b>Overall Score</b> and a <b>Priority Tier</b> — tier is a scouting-priority call (A = must-see) that uses the Overall as a guideline (roughly A ≈ 7.5+, B ≈ 6+, C below) but is set editorially, so a handful of vendors sit a half-point either side of those marks. `+
  `Most high-priority vendors carry researched scores; some lower-priority and consulting entries share directional / template values. `+
  `<b>Niche</b> is a broad value-taxonomy label (Agents, Agent Platform, ETL, Dashboard, API, Security, Cost Savings, Governance, Observability, Database, Customer Data, Consulting, …) rolled up from each vendor's category — searchable and filterable above. `+
  `<b>Caveat:</b> ${esc(DATA.meta.caveat||'')} `+
  `<b>Data sources:</b> funding, valuation, round and employee figures were enriched via AI web research (Crunchbase / PitchBook / news / company sites) and are directional — verify before relying. `+
  `Regenerate after editing <code>vendors.json</code> with <code>python build.py</code>.`;
</script>
</body>
</html>
"""


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "vendors.json")
    meta, vendors = load(src)
    rank(vendors)
    out = render(meta, vendors, src)
    print(f"Scored {len(vendors)} partners -> {out}")
    tier_a = [v for v in vendors if v.get("tier") == "A"]
    print(f"Tier A must-see ({len(tier_a)}):")
    for v in tier_a:
        print(f"  {v['rank']:>3}. {v['name']:<16} overall={v['overall_score']}  booth {v['booth']}  [{v['category']}]")


if __name__ == "__main__":
    main()
