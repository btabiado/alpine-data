#!/usr/bin/env python3
"""Build the "Competitive Landscape" dashboard at landscape/dashboard.html.

The inverse companion to the Snowflake Summit partner dashboard (/summit/):
it catalogs the AI & data vendors that were NOT at Snowflake Summit 2026.

  * Landscape view  — the 691 non-summit vendors alone, cross-filtered by
    stack-layer (segment) x relationship-to-Snowflake x geo x type x tier.
  * Market Map view — all 888 vendors together (197 present + 691 absent),
    anchored by a stack-layer x presence marimekko.

Data sources (all read-only except this dashboard's own output):
  * landscape/non_summit_vendors.json     — the 691, enriched.
  * ../snowflake_summit/vendors.json       — the 197 Summit partners (NEVER written).
  * landscape/summit_segment_map.json      — overlay mapping the 197 onto the
    shared 19-segment taxonomy + relationship axis (so vendors.json stays intact).

    python landscape/build.py
"""
from __future__ import annotations

import json
from collections import Counter, OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent
NS_PATH = HERE / "non_summit_vendors.json"
SUMMIT_PATH = HERE.parent / "snowflake_summit" / "vendors.json"
OVERLAY_PATH = HERE / "summit_segment_map.json"
CHARTJS_PATH = HERE / "vendor" / "chart.umd.js"
OUT_PATH = HERE / "dashboard.html"

# Canonical 19-segment order is computed from the data (by total count desc).
CONSULTING = "Data & AI Consulting / SI"


def norm_segment(c: str) -> str:
    c = (c or "").strip()
    if c.startswith("Data & AI Consulting"):
        return CONSULTING
    return c or "Other"


def load_ns():
    d = json.load(open(NS_PATH))
    vs = d["vendors"]
    out = []
    for v in vs:
        out.append({
            "name": v.get("name", ""),
            "segment": v.get("segment") or norm_segment(v.get("category", "")),
            "relationship": v.get("relationship", "adjacent"),
            "tier": int(v.get("tier_notability", 3) or 3),
            "geo": v.get("geo", "Unknown"),
            "type": v.get("company_type", "Private"),
            "desc": v.get("one_liner", ""),
            "hq": v.get("hq", ""),
            "stage": v.get("funding_stage", ""),
            "funding": v.get("total_funding", ""),
            "investors": v.get("notable_investors", ""),
            "why": v.get("why_notable", ""),
            "website": v.get("website", ""),
            "at_summit": False,
        })
    return out


def load_summit():
    """197 partners, mapped onto the shared taxonomy via the overlay (read-only)."""
    if not SUMMIT_PATH.exists():
        return []
    d = json.load(open(SUMMIT_PATH))
    vs = d["vendors"] if isinstance(d, dict) else d
    overlay = {}
    if OVERLAY_PATH.exists():
        try:
            overlay = json.load(open(OVERLAY_PATH)).get("map", {})
        except Exception:
            overlay = {}
    out = []
    for v in vs:
        name = v.get("name", "")
        ov = overlay.get(name, {})
        seg = ov.get("segment") or norm_segment(v.get("category", ""))
        rel = ov.get("relationship", "adjacent")
        ctype = v.get("company_type", "") or ""
        ctype = "Public" if "public" in ctype.lower() else ("Consulting" if "consult" in ctype.lower() else "Private")
        out.append({
            "name": name,
            "segment": norm_segment(seg),
            "relationship": rel,
            "tier": v.get("tier", ""),          # A/B/C partner tier (kept distinct)
            "geo": "",
            "type": ctype,
            "desc": v.get("niche", "") or v.get("category", ""),
            "booth": v.get("booth", ""),
            "website": v.get("website", ""),
            "at_summit": True,
        })
    return out, bool(overlay)


def build_segments(ns, summit):
    present = Counter(v["segment"] for v in summit)
    absent = Counter(v["segment"] for v in ns)
    segs = set(present) | set(absent)
    rows = []
    for s in segs:
        p, a = present.get(s, 0), absent.get(s, 0)
        rows.append({"segment": s, "present": p, "absent": a, "total": p + a})
    rows.sort(key=lambda r: (-r["total"], r["segment"]))
    return rows


def render():
    ns = load_ns()
    summit, have_overlay = load_summit()
    segments = build_segments(ns, summit)

    rel_ns = Counter(v["relationship"] for v in ns)
    geo_ns = Counter(v["geo"] for v in ns)
    tier_ns = Counter(v["tier"] for v in ns)

    payload = {
        "meta": {
            "generated": "2026-06-07",
            "ns_total": len(ns),
            "summit_total": len(summit),
            "all_total": len(ns) + len(summit),
            "have_overlay": have_overlay,
            "n_segments": len(segments),
            "absent_gt_present": sum(1 for s in segments if s["absent"] > s["present"]),
        },
        "ns": ns,
        "summit": summit,
        "segments": segments,
        "relCounts": dict(rel_ns),
        "geoCounts": dict(geo_ns),
        "tierCounts": {str(k): v for k, v in tier_ns.items()},
    }

    data_json = json.dumps(payload, ensure_ascii=False)
    data_json = data_json.replace("</", "<\\/").replace(" ", "\\u2028").replace(" ", "\\u2029")

    html = HTML_TEMPLATE.replace("/*__DATA__*/", data_json)

    chartjs = ""
    if CHARTJS_PATH.exists():
        chartjs = CHARTJS_PATH.read_text()
    if chartjs:
        html = html.replace("<!--__CHARTJS__-->", "<script>\n" + chartjs + "\n</script>")
    else:
        html = html.replace("<!--__CHARTJS__-->", '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>')

    OUT_PATH.write_text(html)
    return payload


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Competitive Landscape — AI & Data Vendors NOT at Snowflake Summit 2026</title>
<!--__CHARTJS__-->
<style>
:root{
  --bg:#0b1020; --panel:#121a30; --panel2:#172241; --border:#243352;
  --text:#e8eeff; --muted:#8da2c8; --accent:#29b5e8; --accent2:#11567f;
  --sub:#f87171;      /* substitute (competitor) */
  --adj:#34d399;      /* adjacent (complementor) */
  --orb:#a78bfa;      /* different-orbit */
  --present:#29b5e8;  /* at summit */
  --absent:#fbbf24;   /* not at summit */
  --t1:#34d399; --t2:#fbbf24; --t3:#64748b;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.4}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1280px;margin:0 auto;padding:16px}
header.top{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;padding:14px 16px;border-bottom:1px solid var(--border);position:sticky;top:0;background:rgba(11,16,32,.92);backdrop-filter:blur(8px);z-index:20}
header.top h1{font-size:17px;margin:0;font-weight:700;letter-spacing:.2px}
header.top .sub{color:var(--muted);font-size:12.5px;margin-top:2px}
.spacer{flex:1}
.navlink{font-size:12.5px;color:var(--muted);border:1px solid var(--border);border-radius:999px;padding:5px 11px;white-space:nowrap}
.navlink:hover{color:var(--text);border-color:var(--accent);text-decoration:none}
.tabs{display:flex;gap:6px;padding:12px 16px 0;max-width:1280px;margin:0 auto}
.tab{appearance:none;background:var(--panel);color:var(--muted);border:1px solid var(--border);border-bottom:none;
  border-radius:10px 10px 0 0;padding:9px 16px;font-size:14px;font-weight:600;cursor:pointer}
.tab[aria-selected="true"]{background:var(--panel2);color:var(--text);box-shadow:inset 0 2px 0 var(--accent)}
.view{display:none}
.view.active{display:block}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px;margin:14px 0}
.panel h2{font-size:14px;margin:0 0 12px;font-weight:700;letter-spacing:.3px;color:var(--text)}
.panel .hint{color:var(--muted);font-size:12.5px;font-weight:400;margin-left:6px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--panel2);border:1px solid var(--border);border-radius:12px;padding:12px 14px;text-align:left;cursor:pointer;color:inherit}
.kpi:hover{border-color:var(--accent)}
.kpi .n{font-size:26px;font-weight:800;line-height:1}
.kpi .l{color:var(--muted);font-size:12px;margin-top:5px}
.kpi.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px}
.filters select,.filters input{background:var(--panel2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px}
.filters input{min-width:180px}
.chip{appearance:none;background:var(--panel2);color:var(--muted);border:1px solid var(--border);border-radius:999px;padding:6px 12px;font-size:12.5px;cursor:pointer;font-weight:600}
.chip[aria-pressed="true"]{color:#0b1020;background:var(--accent);border-color:var(--accent)}
.chip.sub[aria-pressed="true"]{background:var(--sub);border-color:var(--sub)}
.chip.adj[aria-pressed="true"]{background:var(--adj);border-color:var(--adj)}
.chip.orb[aria-pressed="true"]{background:var(--orb);border-color:var(--orb)}
.grid2{display:grid;grid-template-columns:1.3fr 1fr;gap:14px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.chartbox{position:relative;height:260px}
.chartbox.tall{height:340px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);vertical-align:top}
th{position:sticky;top:0;background:var(--panel);color:var(--muted);font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;cursor:pointer;user-select:none;white-space:nowrap}
th[aria-sort]:after{content:" ↕";opacity:.4}
th[aria-sort="ascending"]:after{content:" ↑";opacity:1}
th[aria-sort="descending"]:after{content:" ↓";opacity:1}
tbody tr:hover{background:var(--panel2)}
.scroll{max-height:620px;overflow:auto;border:1px solid var(--border);border-radius:12px}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap}
.pill.sub{background:rgba(248,113,113,.16);color:#fca5a5}
.pill.adj{background:rgba(52,211,153,.14);color:#6ee7b7}
.pill.orb{background:rgba(167,139,250,.16);color:#c4b5fd}
.pill.t1{background:rgba(52,211,153,.16);color:#6ee7b7}
.pill.t2{background:rgba(251,191,36,.16);color:#fcd34d}
.pill.t3{background:rgba(100,116,139,.18);color:#cbd5e1}
.pill.present{background:rgba(41,181,232,.16);color:#7dd3fc}
.pill.absent{background:rgba(251,191,36,.16);color:#fcd34d}
.seg{color:var(--muted);font-size:12px}
.muted{color:var(--muted)}
.showall{margin:10px 0 0;text-align:center}
.btn{appearance:none;background:var(--panel2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px 16px;font-size:13px;cursor:pointer;font-weight:600}
.btn:hover{border-color:var(--accent)}
.legend{display:flex;flex-wrap:wrap;gap:14px;font-size:12px;color:var(--muted);margin:0 0 10px}
.legend .sw{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;vertical-align:-1px}
/* marimekko */
.mekko{display:flex;align-items:stretch;gap:3px;height:360px;overflow-x:auto;padding-bottom:6px}
.mcol{display:flex;flex-direction:column;min-width:34px;cursor:default}
.mcol .bars{flex:1;display:flex;flex-direction:column;border-radius:6px;overflow:hidden;border:1px solid var(--border)}
.mcol .b-abs{background:linear-gradient(180deg,#fbbf24,#d99e1e);color:#3a2c05;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px}
.mcol .b-pre{background:linear-gradient(180deg,#29b5e8,#1c87b0);color:#04222e;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:12px}
.mcol .mlabel{font-size:10.5px;color:var(--muted);margin-top:6px;text-align:center;line-height:1.15;height:48px;overflow:hidden}
.mcol:hover .bars{outline:2px solid var(--accent)}
.statstrip{display:flex;flex-wrap:wrap;gap:18px;margin:0 0 4px}
.statstrip .s{font-size:13px}
.statstrip .s b{font-size:22px;display:block;font-weight:800}
.foot{color:var(--muted);font-size:12px;padding:18px 4px 30px;text-align:center;line-height:1.6}
.skip{position:absolute;left:-999px}
.skip:focus{left:8px;top:8px;position:fixed;background:var(--accent);color:#04222e;padding:8px 12px;border-radius:8px;z-index:50}
@media (max-width:820px){.grid2,.grid3{grid-template-columns:1fr}.wrap{padding:10px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
</head>
<body>
<a class="skip" href="#main">Skip to content</a>
<header class="top">
  <div>
    <h1>🔭 Competitive Landscape</h1>
    <div class="sub">AI &amp; data vendors <b>not</b> at Snowflake Summit 2026 — the inverse of the 197-partner directory</div>
  </div>
  <div class="spacer"></div>
  <a class="navlink" href="../summit/">← Summit partners</a>
  <a class="navlink" href="../#summit">Main dashboard</a>
</header>

<div class="tabs" role="tablist" aria-label="Views">
  <button class="tab" id="tab-ls" role="tab" aria-selected="true" aria-controls="view-ls">Landscape <span class="muted">· 691 absent</span></button>
  <button class="tab" id="tab-mm" role="tab" aria-selected="false" aria-controls="view-mm" tabindex="-1">Market Map <span class="muted">· all 888</span></button>
</div>

<main id="main" class="wrap">

<!-- ===================== LANDSCAPE VIEW ===================== -->
<section class="view active" id="view-ls" role="tabpanel" aria-labelledby="tab-ls">
  <div class="panel">
    <h2>The 691 absent vendors <span class="hint">click a tile to filter · the inverse of who showed up at /summit/</span></h2>
    <div class="kpis" id="lsKpis"></div>
  </div>

  <div class="panel">
    <div class="filters" id="lsFilters">
      <input id="lsSearch" type="search" placeholder="Search name / description…" aria-label="Search vendors">
      <select id="lsSeg" aria-label="Segment"><option value="">All segments</option></select>
      <select id="lsGeo" aria-label="Geography"><option value="">All geographies</option></select>
      <select id="lsType" aria-label="Company type"><option value="">All types</option></select>
      <span style="width:8px"></span>
      <button class="chip" data-rel="" aria-pressed="true">All relationships</button>
      <button class="chip sub" data-rel="substitute" aria-pressed="false">Substitutes</button>
      <button class="chip adj" data-rel="adjacent" aria-pressed="false">Adjacent</button>
      <button class="chip orb" data-rel="different-orbit" aria-pressed="false">Different-orbit</button>
      <span style="width:8px"></span>
      <button class="chip" data-tier="" aria-pressed="true">All tiers</button>
      <button class="chip" data-tier="1" aria-pressed="false">Tier 1</button>
      <button class="chip" data-tier="2" aria-pressed="false">Tier 2</button>
      <button class="chip" data-tier="3" aria-pressed="false">Tier 3</button>
    </div>
    <div class="grid3">
      <div><div class="chartbox tall"><canvas id="segChart"></canvas></div></div>
      <div><div class="chartbox tall"><canvas id="relChart"></canvas></div></div>
      <div><div class="chartbox tall"><canvas id="geoChart"></canvas></div></div>
    </div>
  </div>

  <div class="panel">
    <h2><span id="lsCount">0</span> vendors <span class="hint" id="lsHint"></span></h2>
    <div class="scroll"><table id="lsTable"><thead></thead><tbody></tbody></table></div>
    <div class="showall"><button class="btn" id="lsShowAll" hidden>Show all</button></div>
  </div>
</section>

<!-- ===================== MARKET MAP VIEW ===================== -->
<section class="view" id="view-mm" role="tabpanel" aria-labelledby="tab-mm" hidden>
  <div class="panel" id="mmOverlayWarn" hidden>
    <h2 style="color:var(--absent)">Summit overlay pending</h2>
    <div class="muted">The 197-partner segment overlay (<code>summit_segment_map.json</code>) isn't present yet, so the “present” side is empty. Re-run <code>build.py</code> after it lands.</div>
  </div>
  <div class="panel">
    <div class="statstrip" id="mmStats"></div>
  </div>
  <div class="panel">
    <h2>Stack layer × presence <span class="hint">column width ∝ vendors in that layer · amber = absent from summit, blue = present</span></h2>
    <div class="legend">
      <span><span class="sw" style="background:var(--absent)"></span>Not at Summit (absent)</span>
      <span><span class="sw" style="background:var(--present)"></span>At Summit (present)</span>
    </div>
    <div class="mekko" id="mekko" role="img" aria-label="Marimekko of vendors by stack layer and summit presence"></div>
  </div>
  <div class="panel">
    <div class="filters" id="mmFilters">
      <input id="mmSearch" type="search" placeholder="Search all 888…" aria-label="Search all vendors">
      <select id="mmSeg" aria-label="Segment"><option value="">All segments</option></select>
      <span style="width:8px"></span>
      <button class="chip" data-pres="" aria-pressed="true">All</button>
      <button class="chip" data-pres="present" aria-pressed="false">At Summit</button>
      <button class="chip" data-pres="absent" aria-pressed="false">Absent</button>
      <span style="width:8px"></span>
      <button class="chip" data-mrel="" aria-pressed="true">All relationships</button>
      <button class="chip sub" data-mrel="substitute" aria-pressed="false">Substitutes</button>
      <button class="chip adj" data-mrel="adjacent" aria-pressed="false">Adjacent</button>
      <button class="chip orb" data-mrel="different-orbit" aria-pressed="false">Different-orbit</button>
    </div>
    <h2><span id="mmCount">0</span> vendors</h2>
    <div class="scroll"><table id="mmTable"><thead></thead><tbody></tbody></table></div>
    <div class="showall"><button class="btn" id="mmShowAll" hidden>Show all</button></div>
  </div>
</section>

<div class="foot">
  First-pass competitive-landscape map · generated 2026-06-07 · 691 absent vendors researched via a 19-segment agent swarm, cross-checked against the 197 Snowflake Summit 2026 partners.<br>
  Funding / valuation / HQ are best-effort from public sources — validate before external use. The Summit directory (<code>vendors.json</code>) is unmodified.
</div>

<script>
const DATA = /*__DATA__*/;
const esc=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const REL_LABEL={substitute:'Substitute',adjacent:'Adjacent','different-orbit':'Different-orbit'};
const REL_CLASS={substitute:'sub',adjacent:'adj','different-orbit':'orb'};

/* ---------- tabs ---------- */
const tabs=[document.getElementById('tab-ls'),document.getElementById('tab-mm')];
const views={'tab-ls':'view-ls','tab-mm':'view-mm'};
function selectTab(t){
  tabs.forEach(x=>{const on=x===t;x.setAttribute('aria-selected',on);x.tabIndex=on?0:-1;
    const v=document.getElementById(views[x.id]);v.classList.toggle('active',on);v.hidden=!on;});
  if(t.id==='tab-mm'){renderMekko();drawMM();}
}
tabs.forEach((t,i)=>{
  t.addEventListener('click',()=>selectTab(t));
  t.addEventListener('keydown',e=>{
    if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();
      const n=tabs[(i+(e.key==='ArrowRight'?1:tabs.length-1))%tabs.length];n.focus();selectTab(n);}
  });
});

/* ---------- shared table renderer ---------- */
function makeTable(tableEl, cols){
  const thead=tableEl.querySelector('thead'), tbody=tableEl.querySelector('tbody');
  let sortKey=null, sortDir=1;
  thead.innerHTML='<tr>'+cols.map(c=>`<th data-k="${c.k}">${esc(c.t)}</th>`).join('')+'</tr>';
  thead.querySelectorAll('th').forEach(th=>{
    th.tabIndex=0; th.setAttribute('role','button');
    const go=()=>{const k=th.dataset.k;
      if(sortKey===k)sortDir*=-1; else {sortKey=k;sortDir=1;}
      thead.querySelectorAll('th').forEach(x=>x.removeAttribute('aria-sort'));
      th.setAttribute('aria-sort',sortDir>0?'ascending':'descending');
      tableEl._render();};
    th.addEventListener('click',go);
    th.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();go();}});
  });
  tableEl._cols=cols;
  tableEl._sort=(rows)=>{
    if(!sortKey)return rows;
    const c=cols.find(x=>x.k===sortKey);
    return rows.slice().sort((a,b)=>{
      let va=(c.sort?c.sort(a):a[sortKey]), vb=(c.sort?c.sort(b):b[sortKey]);
      if(typeof va==='number'&&typeof vb==='number')return (va-vb)*sortDir;
      return String(va||'').localeCompare(String(vb||''))*sortDir;
    });
  };
  return {thead,tbody};
}
function renderRows(tableEl, rows, limit){
  const cols=tableEl._cols;
  const shown=limit?rows.slice(0,limit):rows;
  tableEl.querySelector('tbody').innerHTML=shown.map(v=>'<tr>'+cols.map(c=>`<td>${c.cell(v)}</td>`).join('')+'</tr>').join('');
  return rows.length;
}

/* ---------- LANDSCAPE ---------- */
const NS=DATA.ns;
const lsState={rel:'',tier:'',seg:'',geo:'',type:'',q:'',kpi:'',limit:120};
const LS_TABLE=document.getElementById('lsTable');
const lsCols=[
  {k:'name',t:'Vendor',cell:v=>`<b>${esc(v.name)}</b>${v.website?` <a href="${esc(v.website)}" target="_blank" rel="noopener" aria-label="${esc(v.name)} website">↗</a>`:''}<div class="seg">${esc(v.segment)}</div>`},
  {k:'relationship',t:'Rel.',cell:v=>`<span class="pill ${REL_CLASS[v.relationship]}">${esc(REL_LABEL[v.relationship]||v.relationship)}</span>`},
  {k:'desc',t:'What they do',cell:v=>esc(v.desc)},
  {k:'stage',t:'Stage',cell:v=>esc(v.stage||'—')},
  {k:'funding',t:'Funding',cell:v=>esc(v.funding||'—')},
  {k:'geo',t:'HQ',cell:v=>`${esc(v.hq||v.geo||'—')}`,sort:v=>v.geo},
  {k:'tier',t:'Tier',cell:v=>`<span class="pill t${v.tier}">T${v.tier}</span>`,sort:v=>v.tier},
];
makeTable(LS_TABLE,lsCols);

function lsFiltered(){
  const q=lsState.q.toLowerCase();
  return NS.filter(v=>{
    if(lsState.rel&&v.relationship!==lsState.rel)return false;
    if(lsState.tier&&String(v.tier)!==lsState.tier)return false;
    if(lsState.seg&&v.segment!==lsState.seg)return false;
    if(lsState.geo&&v.geo!==lsState.geo)return false;
    if(lsState.type&&v.type!==lsState.type)return false;
    if(lsState.kpi==='public'&&v.type!=='Public')return false;
    if(lsState.kpi==='t1'&&v.tier!==1)return false;
    if(lsState.kpi==='exus'&&(v.geo==='North America'))return false;
    if(q&&!(v.name.toLowerCase().includes(q)||(v.desc||'').toLowerCase().includes(q)))return false;
    return true;
  });
}
LS_TABLE._render=()=>{
  let rows=lsFiltered(); rows=LS_TABLE._sort(rows);
  const total=rows.length;
  document.getElementById('lsCount').textContent=total;
  document.getElementById('lsHint').textContent=lsState.limit<total?`showing first ${lsState.limit} — sorted; use filters or “Show all”`:'';
  renderRows(LS_TABLE,rows,lsState.limit);
  document.getElementById('lsShowAll').hidden=!(lsState.limit<total);
};
function lsKpis(){
  const r=DATA.relCounts, exus=NS.filter(v=>v.geo!=='North America'&&v.geo!=='Unknown').length;
  const t1=(DATA.tierCounts['1']||0), pub=NS.filter(v=>v.type==='Public').length;
  const tiles=[
    {n:NS.length,l:'Absent vendors',kpi:''},
    {n:r['substitute']||0,l:'Substitutes (compete)',kpi:'',rel:'substitute'},
    {n:r['adjacent']||0,l:'Adjacent (could partner)',kpi:'',rel:'adjacent'},
    {n:r['different-orbit']||0,l:'Different-orbit',kpi:'',rel:'different-orbit'},
    {n:t1,l:'Tier-1 marquee',kpi:'t1'},
    {n:pub,l:'Public companies',kpi:'public'},
    {n:exus,l:'HQ outside North America',kpi:'exus'},
  ];
  document.getElementById('lsKpis').innerHTML=tiles.map((t,i)=>
    `<button class="kpi" data-i="${i}" data-rel="${t.rel||''}" data-kpi="${t.kpi}"><div class="n">${t.n}</div><div class="l">${esc(t.l)}</div></button>`).join('');
  document.querySelectorAll('#lsKpis .kpi').forEach(b=>b.addEventListener('click',()=>{
    const rel=b.dataset.rel, kpi=b.dataset.kpi;
    // toggle off if same
    if(rel){lsState.rel=(lsState.rel===rel?'':rel);lsState.kpi='';syncRelChips();}
    else {lsState.kpi=(lsState.kpi===kpi?'':kpi);lsState.rel='';syncRelChips();}
    document.querySelectorAll('#lsKpis .kpi').forEach(x=>x.classList.remove('on'));
    if((rel&&lsState.rel)||(!rel&&lsState.kpi))b.classList.add('on');
    lsState.limit=120;LS_TABLE._render();
  }));
}
function syncRelChips(){
  document.querySelectorAll('#lsFilters .chip[data-rel]').forEach(c=>c.setAttribute('aria-pressed',String(c.dataset.rel===lsState.rel)));
}
function fillSelect(sel,vals){sel.innerHTML=sel.children[0].outerHTML+vals.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');}

/* charts */
let segChart,relChart,geoChart;
const REL_COLORS={substitute:'#f87171',adjacent:'#34d399','different-orbit':'#a78bfa'};
function drawLSCharts(){
  const segs=DATA.segments.slice().sort((a,b)=>b.absent-a.absent);
  const segLabels=segs.map(s=>s.segment), segVals=segs.map(s=>s.absent);
  const rc=DATA.relCounts, gc=DATA.geoCounts;
  const Cdef=Chart.defaults; Cdef.color='#8da2c8'; Cdef.font.family=getComputedStyle(document.body).fontFamily;
  segChart=new Chart(document.getElementById('segChart'),{type:'bar',
    data:{labels:segLabels,datasets:[{label:'Absent',data:segVals,backgroundColor:'#fbbf24'}]},
    options:{indexAxis:'y',maintainAspectRatio:false,plugins:{legend:{display:false},title:{display:true,text:'Absent vendors by stack layer'}},
      scales:{y:{ticks:{font:{size:10},autoSkip:false}}}}});
  const relK=['substitute','adjacent','different-orbit'];
  relChart=new Chart(document.getElementById('relChart'),{type:'doughnut',
    data:{labels:relK.map(k=>REL_LABEL[k]),datasets:[{data:relK.map(k=>rc[k]||0),backgroundColor:relK.map(k=>REL_COLORS[k])}]},
    options:{maintainAspectRatio:false,plugins:{legend:{position:'bottom'},title:{display:true,text:'Relationship to Snowflake'}}}});
  const geoOrder=Object.entries(gc).sort((a,b)=>b[1]-a[1]);
  geoChart=new Chart(document.getElementById('geoChart'),{type:'bar',
    data:{labels:geoOrder.map(g=>g[0]),datasets:[{label:'Vendors',data:geoOrder.map(g=>g[1]),backgroundColor:'#29b5e8'}]},
    options:{indexAxis:'y',maintainAspectRatio:false,plugins:{legend:{display:false},title:{display:true,text:'HQ geography'}}}});
}

/* wire landscape filters */
function initLandscape(){
  lsKpis(); drawLSCharts();
  fillSelect(document.getElementById('lsSeg'),DATA.segments.map(s=>s.segment));
  fillSelect(document.getElementById('lsGeo'),Object.keys(DATA.geoCounts).sort());
  fillSelect(document.getElementById('lsType'),[...new Set(NS.map(v=>v.type))].sort());
  document.getElementById('lsSearch').addEventListener('input',e=>{lsState.q=e.target.value;lsState.limit=120;LS_TABLE._render();});
  document.getElementById('lsSeg').addEventListener('change',e=>{lsState.seg=e.target.value;lsState.limit=120;LS_TABLE._render();});
  document.getElementById('lsGeo').addEventListener('change',e=>{lsState.geo=e.target.value;lsState.limit=120;LS_TABLE._render();});
  document.getElementById('lsType').addEventListener('change',e=>{lsState.type=e.target.value;lsState.limit=120;LS_TABLE._render();});
  document.querySelectorAll('#lsFilters .chip[data-rel]').forEach(c=>c.addEventListener('click',()=>{
    lsState.rel=c.dataset.rel;lsState.kpi='';syncRelChips();
    document.querySelectorAll('#lsKpis .kpi').forEach(x=>x.classList.remove('on'));lsState.limit=120;LS_TABLE._render();}));
  document.querySelectorAll('#lsFilters .chip[data-tier]').forEach(c=>c.addEventListener('click',()=>{
    lsState.tier=c.dataset.tier;
    document.querySelectorAll('#lsFilters .chip[data-tier]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.tier===lsState.tier)));
    lsState.limit=120;LS_TABLE._render();}));
  document.getElementById('lsShowAll').addEventListener('click',()=>{lsState.limit=99999;LS_TABLE._render();});
  LS_TABLE._render();
}

/* ---------- MARKET MAP ---------- */
const ALL=DATA.ns.concat(DATA.summit);
const mmState={pres:'',rel:'',seg:'',q:'',limit:150};
const MM_TABLE=document.getElementById('mmTable');
const mmCols=[
  {k:'name',t:'Vendor',cell:v=>`<b>${esc(v.name)}</b>${v.website?` <a href="${esc(v.website)}" target="_blank" rel="noopener">↗</a>`:''}<div class="seg">${esc(v.segment)}</div>`},
  {k:'at_summit',t:'Presence',cell:v=>v.at_summit?'<span class="pill present">At Summit</span>':'<span class="pill absent">Absent</span>',sort:v=>v.at_summit?1:0},
  {k:'relationship',t:'Rel.',cell:v=>`<span class="pill ${REL_CLASS[v.relationship]}">${esc(REL_LABEL[v.relationship]||v.relationship)}</span>`},
  {k:'type',t:'Type',cell:v=>esc(v.type||'—')},
  {k:'desc',t:'Note',cell:v=>esc(v.desc||'')},
];
makeTable(MM_TABLE,mmCols);
function mmFiltered(){
  const q=mmState.q.toLowerCase();
  return ALL.filter(v=>{
    if(mmState.pres==='present'&&!v.at_summit)return false;
    if(mmState.pres==='absent'&&v.at_summit)return false;
    if(mmState.rel&&v.relationship!==mmState.rel)return false;
    if(mmState.seg&&v.segment!==mmState.seg)return false;
    if(q&&!(v.name.toLowerCase().includes(q)||(v.desc||'').toLowerCase().includes(q)))return false;
    return true;
  });
}
MM_TABLE._render=()=>{
  let rows=mmFiltered();rows=MM_TABLE._sort(rows);
  document.getElementById('mmCount').textContent=rows.length;
  renderRows(MM_TABLE,rows,mmState.limit);
  document.getElementById('mmShowAll').hidden=!(mmState.limit<rows.length);
};
let mmInit=false;
function drawMM(){
  if(!mmInit){
    mmInit=true;
    document.getElementById('mmOverlayWarn').hidden=DATA.meta.have_overlay;
    const m=DATA.meta;
    document.getElementById('mmStats').innerHTML=[
      ['At Summit','present',m.summit_total],['Absent','absent',m.ns_total],
      ['Total mapped','',m.all_total],['Layers where absent &gt; present','',m.absent_gt_present+' / '+m.n_segments],
    ].map(s=>`<div class="s"><b>${s[2]}</b>${s[0]}</div>`).join('');
    fillSelect(document.getElementById('mmSeg'),DATA.segments.map(s=>s.segment));
    document.getElementById('mmSearch').addEventListener('input',e=>{mmState.q=e.target.value;mmState.limit=150;MM_TABLE._render();});
    document.getElementById('mmSeg').addEventListener('change',e=>{mmState.seg=e.target.value;mmState.limit=150;MM_TABLE._render();});
    document.querySelectorAll('#mmFilters .chip[data-pres]').forEach(c=>c.addEventListener('click',()=>{
      mmState.pres=c.dataset.pres;
      document.querySelectorAll('#mmFilters .chip[data-pres]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.pres===mmState.pres)));
      mmState.limit=150;MM_TABLE._render();}));
    document.querySelectorAll('#mmFilters .chip[data-mrel]').forEach(c=>c.addEventListener('click',()=>{
      mmState.rel=c.dataset.mrel;
      document.querySelectorAll('#mmFilters .chip[data-mrel]').forEach(x=>x.setAttribute('aria-pressed',String(x.dataset.mrel===mmState.rel)));
      mmState.limit=150;MM_TABLE._render();}));
    document.getElementById('mmShowAll').addEventListener('click',()=>{mmState.limit=99999;MM_TABLE._render();});
  }
  MM_TABLE._render();
}
let mekkoDone=false;
function renderMekko(){
  if(mekkoDone)return; mekkoDone=true;
  const segs=DATA.segments;
  const maxTotal=Math.max(...segs.map(s=>s.total));
  document.getElementById('mekko').innerHTML=segs.map(s=>{
    const absPct=s.total?Math.round(s.absent/s.total*100):0, prePct=100-absPct;
    const short=s.segment.replace(/ & /g,' & ').replace(/, /g,', ');
    return `<div class="mcol" style="flex:${s.total}" title="${esc(s.segment)} — ${s.absent} absent / ${s.present} present (${s.total} total)">
      <div class="bars">
        <div class="b-abs" style="height:${absPct}%">${s.absent>3?s.absent:''}</div>
        <div class="b-pre" style="height:${prePct}%">${s.present>3?s.present:''}</div>
      </div>
      <div class="mlabel">${esc(short)}<br><span style="opacity:.7">${s.total}</span></div>
    </div>`;
  }).join('');
}

/* ---------- boot ---------- */
initLandscape();
</script>
</body>
</html>
"""


def main():
    p = render()
    m = p["meta"]
    print(f"Built {OUT_PATH}")
    print(f"  non-summit: {m['ns_total']} | summit(overlay {'on' if m['have_overlay'] else 'OFF'}): {m['summit_total']} | total: {m['all_total']}")
    print(f"  segments: {m['n_segments']} | layers where absent>present: {m['absent_gt_present']}")


if __name__ == "__main__":
    main()
