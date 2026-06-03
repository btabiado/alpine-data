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
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

SCORE_KEYS = [
    ("snowflake_score", "Snowflake"),
    ("ai_score", "AI"),
    ("retail_score", "Retail/Cust"),
    ("ipo_score", "IPO/Upside"),
    ("bryan_score", "Bryan Fit"),
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
        {"label": "Partner Vendors", "value": n, "sub": f"{len(cats)} categories"},
        {"label": "Must-See (Tier A)", "value": a, "sub": "top priority"},
        {"label": "Priority (Tier A+B)", "value": a + b, "sub": f"{round(100*(a+b)/n) if n else 0}% of floor"},
        {"label": "Avg Overall Score", "value": avg([v.get("overall_score") for v in vendors]), "sub": "out of 10"},
        {"label": "Avg AI Score", "value": avg([v.get("ai_score") for v in vendors]), "sub": "the Summit's hot theme"},
        {"label": "Public Companies", "value": pub, "sub": f"{n-pub} private/other"},
    ]


def aggregate_count(vendors, key):
    out = {}
    for v in vendors:
        k = v.get(key) or "—"
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def render(meta, vendors, src_path):
    by_cat = aggregate_count(vendors, "category")
    by_niche = aggregate_count(vendors, "niche")
    by_tier = {t: sum(1 for v in vendors if v.get("tier") == t) for t in ["A", "B", "C", "D"]}
    by_tier = {t: c for t, c in by_tier.items() if c}

    # average score profile across the five dimensions, Tier A vs all
    def profile(subset):
        return [avg([v.get(k) for v in subset]) for k, _ in SCORE_KEYS]
    tierA = [v for v in vendors if v.get("tier") == "A"]

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
        "gems": [v for v in vendors if v["hidden_gem"] and v.get("tier") != "A"][:6],
        "best_fit": sorted(vendors, key=lambda v: -(v.get("bryan_score") or 0))[:6],
        "vendors": vendors,
    }
    html = HTML_TEMPLATE.replace("/*__DATA__*/", json.dumps(payload, ensure_ascii=False))
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
    out_path = os.path.join(HERE, "dashboard.html")
    with open(out_path, "w") as f:
        f.write(html)
    return out_path


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
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
  .logo{width:34px;height:34px;border-radius:8px;background:linear-gradient(135deg,#29b5e8,#1b7fb8);display:flex;align-items:center;justify-content:center;font-weight:800;color:#06121f}
  h1{font-size:20px;margin:0;letter-spacing:.01em}
  .sub{color:var(--muted);font-size:12.5px;margin-top:4px}
  .dl{margin-left:auto;background:var(--accent2);border:1px solid var(--accent);color:#dff3ff;padding:8px 13px;border-radius:9px;font-size:12.5px;text-decoration:none;white-space:nowrap}
  .dl:hover{background:#176a9c}
  .wrap{max-width:1320px;margin:0 auto;padding:20px 24px 60px}
  .kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:6px 0 24px}
  .kpi{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 15px}
  .kpi .v{font-size:25px;font-weight:800;color:#fff;line-height:1}
  .kpi .l{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);margin-top:8px}
  .kpi .s{font-size:11px;color:var(--accent);margin-top:3px}
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
  .ovr b{font-size:22px;color:#fff}.ovr span{font-size:10.5px;color:var(--muted)}
  .tag{display:inline-block;font-size:10px;padding:2px 8px;border-radius:20px;font-weight:700}
  .tA{background:rgba(52,211,153,.16);color:var(--A)}.tB{background:rgba(251,191,36,.16);color:var(--B)}
  .tC{background:rgba(100,116,139,.2);color:#aab6c9}.tD{background:rgba(100,116,139,.2);color:#aab6c9}
  .tNi{background:rgba(41,181,232,.16);color:#7fd6f5;border:1px solid rgba(41,181,232,.3)}
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
  .controls{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:11px;align-items:center}
  .controls input,.controls select{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 10px;font-size:12.5px}
  .ovrbar{display:inline-block;height:7px;border-radius:4px;vertical-align:middle;margin-right:7px}
  .note{color:var(--muted);font-size:12px;margin-top:18px;line-height:1.55;border-top:1px solid var(--border);padding-top:14px}
  .note code{color:var(--accent)}
  @media(max-width:1000px){.kpis{grid-template-columns:repeat(3,1fr)}.grid{grid-template-columns:1fr}.cards{grid-template-columns:1fr}}
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
    <a class="dl" href="../" style="margin-left:auto;background:transparent;border-color:var(--border);color:var(--muted)" title="Back to the main dashboard">← Main dashboard</a>
    <a class="dl" href="Snowflake_Summit_2026_Master_Partner_Scouting.xlsx" download style="margin-left:0">⬇ Download source spreadsheet</a>
  </div>
</header>
<div class="wrap">
  <div class="kpis" id="kpis"></div>

  <div class="topbar">
    <div class="searchwrap">
      <input id="search" placeholder="Search all vendors — name, category, niche…" autocomplete="off"/>
    </div>
    <span class="hit" id="searchhit"></span>
  </div>

  <h3 class="sec">⭐ Must-See Vendors <span class="hint">— Priority Tier A, ranked by Overall Score</span></h3>
  <div class="cards" id="mustsee"></div>

  <div class="grid" style="margin-top:22px">
    <div class="panel"><h4>Top 15 by Overall Score</h4><canvas id="topChart"></canvas></div>
    <div class="panel"><h4>Priority Tier mix</h4><canvas id="tierChart"></canvas></div>
  </div>
  <div class="grid">
    <div class="panel"><h4>Partners by Niche</h4><canvas id="nicheChart"></canvas></div>
    <div class="panel"><h4>Avg score profile — Tier A vs all</h4><canvas id="profChart"></canvas></div>
  </div>

  <h3 class="sec">💎 Hidden Gems <span class="hint">— Overall ≥ 7 but not Tier A</span></h3>
  <div class="cards" id="gems"></div>

  <h3 class="sec">🤝 Best Bryan-Fit <span class="hint">— top career / networking fit</span></h3>
  <div class="cards" id="bestfit"></div>

  <h3 class="sec">All Partner Vendors <span class="hint">— click a column to sort · 💎 = hidden gem</span></h3>
  <div class="panel">
    <div class="controls">
      <select id="nicheFilter"><option value="">All niches</option></select>
      <select id="catFilter"><option value="">All categories</option></select>
      <select id="tierFilter"><option value="">All tiers</option></select>
    </div>
    <div class="scroll">
    <table id="vtable">
      <thead><tr>
        <th data-k="rank">#</th><th data-k="name">Partner</th><th data-k="booth">Booth</th>
        <th data-k="niche">Niche</th><th data-k="category">Category</th><th data-k="company_type">Type</th>
        <th data-k="snowflake_score" class="num">Snow</th><th data-k="ai_score" class="num">AI</th>
        <th data-k="retail_score" class="num">Retail</th><th data-k="ipo_score" class="num">IPO</th>
        <th data-k="bryan_score" class="num">Fit</th>
        <th data-k="overall_score" class="num">Overall</th><th data-k="tier">Tier</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </div>

  <div class="note" id="note"></div>
</div>

<script>
const DATA = /*__DATA__*/;
const fmt = n => (n===null||n===undefined||n==='')?"—":n;
const tierClass = t => ({A:'tA',B:'tB',C:'tC',D:'tD'})[t]||'tC';

document.getElementById('subhead').textContent =
  `${DATA.vendors.length} partners · ${DATA.meta.event||''} · scoring by ${DATA.meta.owner||'owner'} · source: ${DATA.source}`;

document.getElementById('kpis').innerHTML = DATA.kpis.map(k=>
  `<div class="kpi"><div class="v">${k.value}</div><div class="l">${k.label}</div><div class="s">${k.sub}</div></div>`).join('');

function scoreChips(v){
  const items=[['Snow',v.snowflake_score],['AI',v.ai_score],['Retail',v.retail_score],['IPO',v.ipo_score],['Fit',v.bryan_score]];
  return items.map(([l,x])=>`<span class="schip">${l} <b>${fmt(x)}</b></span>`).join('');
}
function card(v){
  return `<div class="card2 ${v.hidden_gem?'':''}"><div class="rk">${v.rank}</div>
    <div class="nm">${v.name} <span class="tag ${tierClass(v.tier)}">${v.tier}</span> <span class="tag tNi">${fmt(v.niche)}</span></div>
    <div class="ct">${v.category} · booth ${fmt(v.booth)}</div>
    <div class="scores">${scoreChips(v)}</div>
    <div class="ovr"><b>${fmt(v.overall_score)}</b><span>/ 10 overall${v.company_type?(' · '+v.company_type):''}</span></div></div>`;
}
document.getElementById('mustsee').innerHTML = DATA.must_see.map(card).join('');
document.getElementById('gems').innerHTML = DATA.gems.length?DATA.gems.map(card).join(''):'<div class="sub">None above threshold.</div>';
document.getElementById('bestfit').innerHTML = DATA.best_fit.map(card).join('');

const C={grid:'#243352',tick:'#8da2c8'};
const tierColor=t=>({A:'#34d399',B:'#fbbf24',C:'#64748b',D:'#475569'})[t]||'#64748b';

new Chart(document.getElementById('topChart'),{type:'bar',
  data:{labels:DATA.top15.map(v=>v.name),
    datasets:[{data:DATA.top15.map(v=>v.overall_score),backgroundColor:DATA.top15.map(v=>tierColor(v.tier))}]},
  options:{indexAxis:'y',plugins:{legend:{display:false},tooltip:{callbacks:{afterLabel:c=>'Tier '+DATA.top15[c.dataIndex].tier}}},
    scales:{x:{min:0,max:10,ticks:{color:C.tick},grid:{color:C.grid}},y:{ticks:{color:C.tick,font:{size:11}},grid:{display:false}}}}});

new Chart(document.getElementById('tierChart'),{type:'doughnut',
  data:{labels:Object.keys(DATA.by_tier).map(t=>'Tier '+t),
    datasets:[{data:Object.values(DATA.by_tier),backgroundColor:Object.keys(DATA.by_tier).map(tierColor)}]},
  options:{plugins:{legend:{position:'right',labels:{color:C.tick}}}}});

const niches=Object.entries(DATA.by_niche);
new Chart(document.getElementById('nicheChart'),{type:'bar',
  data:{labels:niches.map(c=>c[0]),datasets:[{data:niches.map(c=>c[1]),backgroundColor:'#29b5e8'}]},
  options:{indexAxis:'y',plugins:{legend:{display:false}},onClick:(e,els)=>{if(els.length){nicheSel.value=niches[els[0].index][0];draw();document.getElementById('vtable').scrollIntoView({behavior:'smooth'});}},
    scales:{x:{ticks:{color:C.tick},grid:{color:C.grid}},y:{ticks:{color:C.tick,font:{size:10.5}},grid:{display:false}}}}});

new Chart(document.getElementById('profChart'),{type:'radar',
  data:{labels:DATA.score_labels,datasets:[
    {label:'Tier A',data:DATA.profile_a,borderColor:'#34d399',backgroundColor:'rgba(52,211,153,.15)',pointBackgroundColor:'#34d399'},
    {label:'All partners',data:DATA.profile_all,borderColor:'#29b5e8',backgroundColor:'rgba(41,181,232,.12)',pointBackgroundColor:'#29b5e8'}]},
  options:{plugins:{legend:{labels:{color:C.tick}}},
    scales:{r:{min:0,max:10,angleLines:{color:C.grid},grid:{color:C.grid},pointLabels:{color:C.tick,font:{size:11}},ticks:{display:false}}}}});

// Table
const tbody=document.querySelector('#vtable tbody');
const nicheSel=document.getElementById('nicheFilter'),catSel=document.getElementById('catFilter'),tierSel=document.getElementById('tierFilter');
Object.keys(DATA.by_niche).forEach(nz=>nicheSel.add(new Option(`${nz} (${DATA.by_niche[nz]})`,nz)));
[...new Set(DATA.vendors.map(v=>v.category))].sort().forEach(c=>catSel.add(new Option(c,c)));
['A','B','C'].forEach(t=>tierSel.add(new Option('Tier '+t,t)));
let sortK='rank',sortAsc=true;
function draw(){
  const q=document.getElementById('search').value.toLowerCase(),nz=nicheSel.value,cf=catSel.value,tf=tierSel.value;
  let r=DATA.vendors.filter(v=>(!q||v.name.toLowerCase().includes(q)||(v.category||'').toLowerCase().includes(q)||(v.niche||'').toLowerCase().includes(q))
    &&(!nz||v.niche===nz)&&(!cf||v.category===cf)&&(!tf||v.tier===tf));
  const hit=document.getElementById('searchhit');
  if(hit) hit.textContent = (q||nz||cf||tf) ? `${r.length} of ${DATA.vendors.length} match` : `${DATA.vendors.length} vendors`;
  r.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(typeof x==='string'){x=x.toLowerCase();y=(y||'').toLowerCase();}
    if(x===null||x===undefined)x=-1;if(y===null||y===undefined)y=-1;return (x>y?1:x<y?-1:0)*(sortAsc?1:-1);});
  tbody.innerHTML=r.map(v=>{
    const w=Math.round(((v.overall_score||0)/10)*54)+6;
    return `<tr class="${v.hidden_gem?'gem-row':''}">
      <td class="num">${v.rank}</td><td class="name">${v.name}</td><td>${fmt(v.booth)}</td>
      <td><span class="tag tNi">${fmt(v.niche)}</span></td><td>${fmt(v.category)}</td><td>${fmt(v.company_type)}</td>
      <td class="num">${fmt(v.snowflake_score)}</td><td class="num">${fmt(v.ai_score)}</td>
      <td class="num">${fmt(v.retail_score)}</td><td class="num">${fmt(v.ipo_score)}</td><td class="num">${fmt(v.bryan_score)}</td>
      <td class="num"><span class="ovrbar" style="width:${w}px;background:${tierColor(v.tier)}"></span><b>${fmt(v.overall_score)}</b></td>
      <td><span class="tag ${tierClass(v.tier)}">${v.tier}</span></td></tr>`;}).join('');
}
document.querySelectorAll('#vtable th').forEach(th=>th.onclick=()=>{
  const k=th.dataset.k;if(sortK===k)sortAsc=!sortAsc;else{sortK=k;sortAsc=(k==='rank'||k==='name'||k==='category'||k==='tier'||k==='niche');}draw();});
['input','change'].forEach(e=>{document.getElementById('search').addEventListener(e,draw);nicheSel.addEventListener(e,draw);catSel.addEventListener(e,draw);tierSel.addEventListener(e,draw);});
draw();

document.getElementById('note').innerHTML =
  `<b>Scoring:</b> all scores are ${DATA.meta.owner||'the owner'}'s directional 0–10 ratings from the scouting workbook — `+
  `Snowflake relevance, AI relevance, retail/customer-analytics relevance, IPO/upside, and Bryan career/networking fit — `+
  `blended into an <b>Overall Score</b> and a <b>Priority Tier</b> (A = must-see). `+
  `<b>Niche</b> is a broad value-taxonomy label (Agents, Agent Platform, ETL, Dashboard, API, Security, Cost Savings, Governance, Observability, Database, Customer Data, Consulting, …) rolled up from each vendor's category — searchable and filterable above. `+
  `<b>Caveat:</b> ${DATA.meta.caveat||''} `+
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
    print("Tier A must-see:")
    for v in [v for v in vendors if v.get("tier") == "A"][:14]:
        print(f"  {v['rank']:>3}. {v['name']:<16} overall={v['overall_score']}  booth {v['booth']}  [{v['category']}]")


if __name__ == "__main__":
    main()
