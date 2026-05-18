"""LTHCS data audit + scoring quality assessment (READ-ONLY).

Run: .venv/bin/python scripts/lthcs_audit_data_quality.py

Reports:
1. Per-ticker per-pillar component coverage matrix for today's snapshot
2. Empirical pillar distributions for today
3. 90-day band distribution health
4. Cross-source consistency (Form 4 vs 13F, etc.)
5. Data freshness per source
6. Pipeline integration health (today vs yesterday)
"""
from __future__ import annotations

import json
import os
import statistics
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data" / "lthcs"
TODAY = "2026-05-18"
YESTERDAY = "2026-05-17"


def jload(p: Path):
    if not p.exists():
        return None
    return json.load(open(p))


def section(title: str):
    print("\n" + "=" * 78)
    print("  " + title)
    print("=" * 78)


def stats(xs: list[float]) -> dict:
    xs = sorted(xs)
    if not xs:
        return {}
    n = len(xs)
    def pct(p):
        k = max(0, min(n - 1, int(round(p * (n - 1)))))
        return xs[k]
    return dict(
        n=n,
        mean=round(statistics.mean(xs), 2),
        median=round(statistics.median(xs), 2),
        stdev=round(statistics.pstdev(xs), 2) if n > 1 else 0.0,
        min=round(min(xs), 2),
        p5=round(pct(0.05), 2),
        p25=round(pct(0.25), 2),
        p75=round(pct(0.75), 2),
        p95=round(pct(0.95), 2),
        max=round(max(xs), 2),
    )


def audit_today_variable_detail():
    section(f"1. PER-TICKER COMPONENT COVERAGE — {TODAY}")
    vd = jload(DATA / "variable_detail" / f"{TODAY}.json")
    if not vd:
        print("MISSING variable_detail")
        return None
    # Build pillar -> ticker -> data_quality map
    by_p = defaultdict(dict)
    for row in vd["variables"]:
        by_p[row["pillar"]][row["ticker"]] = row

    # Per-pillar component coverage stats
    pillars = ["adoption_momentum", "institutional_confidence", "financial_evolution", "thesis_integrity", "des"]
    coverage = {}
    for p in pillars:
        rows = by_p[p]
        n = len(rows)
        flags = defaultdict(int)
        for r in rows.values():
            for k, v in (r.get("data_quality") or {}).items():
                if isinstance(v, bool):
                    if v:
                        flags[k] += 1
                elif k == "source" and v:
                    flags[f"source={v}"] += 1
        coverage[p] = {"n_tickers": n, "flags": dict(flags)}
        print(f"\n  {p}  (n={n})")
        for k, v in sorted(flags.items()):
            pct = 100.0 * v / n if n else 0
            print(f"    {k:30s} {v:4d}  {pct:5.1f}%")
    return coverage, by_p


def audit_pillar_distributions(today_snap):
    section(f"2. EMPIRICAL PILLAR DISTRIBUTIONS — {TODAY}")
    scores = today_snap["scores"]
    n = len(scores)
    print(f"  Universe size: {n}")
    out = {}
    for p in ["adoption_momentum", "institutional_confidence", "financial_evolution", "thesis_integrity", "des"]:
        xs = [s["subscores"][p] for s in scores]
        n_at_50 = sum(1 for x in xs if abs(x - 50.0) < 0.0001)
        s = stats(xs)
        s["n_at_50"] = n_at_50
        s["pct_at_50"] = round(100.0 * n_at_50 / n, 1)
        out[p] = s
        print(f"\n  {p}")
        print(f"    n={s['n']} mean={s['mean']} median={s['median']} std={s['stdev']}  range=[{s['min']}, {s['max']}]")
        print(f"    p5/p25/p75/p95 = {s['p5']} / {s['p25']} / {s['p75']} / {s['p95']}")
        print(f"    n_at_exactly_50 = {n_at_50} ({s['pct_at_50']}%)")
    # Composite
    comp = [s["lthcs_score"] for s in scores]
    s = stats(comp)
    print(f"\n  composite_lthcs_score")
    print(f"    n={s['n']} mean={s['mean']} median={s['median']} std={s['stdev']}  range=[{s['min']}, {s['max']}]")
    out["composite"] = s
    return out


def audit_band_distribution_today(today_snap):
    section(f"3a. BAND DISTRIBUTION — {TODAY}")
    bc = Counter(s["band"] for s in today_snap["scores"])
    n = sum(bc.values())
    for b in ["elite", "high_confidence", "constructive", "monitor", "weakening", "review"]:
        print(f"    {b:18s} {bc.get(b, 0):4d}  {100.0*bc.get(b,0)/n:5.1f}%")
    return dict(bc)


def audit_band_distribution_90d():
    section("3b. BAND DISTRIBUTION — 90-day history (post-recalibration)")
    snaps_dir = DATA / "snapshots"
    files = sorted(snaps_dir.glob("*.json"))
    cnt = Counter()
    days = 0
    for f in files:
        if f.name == "index.json":
            continue
        d = jload(f)
        if not d or "scores" not in d:
            continue
        days += 1
        for s in d["scores"]:
            cnt[s["band"]] += 1
    total = sum(cnt.values())
    print(f"  total snapshots: {days}  total ticker-day observations: {total}")
    for b in ["elite", "high_confidence", "constructive", "monitor", "weakening", "review"]:
        print(f"    {b:18s} {cnt.get(b, 0):6d}  {100.0*cnt.get(b,0)/total:5.1f}%")
    return dict(cnt), days


def audit_cross_source_consistency():
    section("4. CROSS-SOURCE CONSISTENCY — Form 4 (insider) vs 13F (holdings)")
    ins = jload(DATA / "insider" / f"{YESTERDAY}.json")  # use 5-17 since 5-18 is missing!
    hold = jload(DATA / "holdings" / f"{YESTERDAY}.json")
    if not ins or not hold:
        print("  MISSING insider/holdings; cannot run")
        return
    # Compare conviction_score (insider) to signal_score (13F holdings)
    common = sorted(set(ins.keys()) & set(hold.keys()))
    pairs = []
    insider_buy_13f_buy = 0
    insider_sell_13f_sell = 0
    disagree = 0
    both_pos = both_neg = mixed = 0
    for t in common:
        ic = ins[t].get("conviction_score")
        hs = hold[t].get("signal_score")
        if ic is None or hs is None:
            continue
        pairs.append((t, ic, hs))
        if ic > 0 and hs > 0:
            both_pos += 1
        elif ic < 0 and hs < 0:
            both_neg += 1
        elif ic == 0 or hs == 0:
            pass
        else:
            mixed += 1
    n = len(pairs)
    print(f"  n_pairs: {n}")
    # Simple correlation manually (Spearman approx using ranks)
    if n > 5:
        # Pearson
        ic_vals = [p[1] for p in pairs]
        hs_vals = [p[2] for p in pairs]
        m1 = sum(ic_vals)/n
        m2 = sum(hs_vals)/n
        num = sum((a-m1)*(b-m2) for a,b in zip(ic_vals, hs_vals))
        den1 = (sum((a-m1)**2 for a in ic_vals))**0.5
        den2 = (sum((b-m2)**2 for b in hs_vals))**0.5
        pearson = num/(den1*den2) if den1>0 and den2>0 else None
        print(f"  Pearson(insider_conviction, 13f_signal_score): {pearson:.3f}" if pearson else "  Pearson: undefined")
    print(f"  both_positive (smart money agrees BUY): {both_pos}")
    print(f"  both_negative (smart money agrees SELL): {both_neg}")
    print(f"  disagree (opposite signs): {mixed}")


def audit_freshness():
    section("5. DATA FRESHNESS — per source")
    today = TODAY
    sources = {
        "snapshots": DATA / "snapshots",
        "variable_detail": DATA / "variable_detail",
        "narratives": DATA / "narratives",
        "insider": DATA / "insider",
        "holdings": DATA / "holdings",
        "sentiment": DATA / "sentiment",
        "analyst_breadth": DATA / "analyst_breadth",
        "macro": DATA / "macro",
        "trends": DATA / "trends",
        "index": DATA / "index",
    }
    import datetime as _dt
    today_d = _dt.date(2026, 5, 18)
    for name, p in sources.items():
        if not p.exists():
            print(f"  {name:20s} DIR MISSING")
            continue
        files = sorted(p.glob("*.json"))
        if not files:
            print(f"  {name:20s} EMPTY")
            continue
        last = files[-1].name
        # try to parse a date
        stem = last.replace(".json", "")
        # take last 10 chars or YYYY-MM-DD pattern
        import re
        m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
        if m:
            d = _dt.date.fromisoformat(m.group(1))
            stale = (today_d - d).days
        else:
            stale = "n/a"
        print(f"  {name:20s} files={len(files):4d}  latest={last}  stale_days={stale}")


def audit_pipeline_integration_health(yesterday_vd):
    section("6. PIPELINE INTEGRATION HEALTH — today vs yesterday")
    today_vd = jload(DATA / "variable_detail" / f"{TODAY}.json")
    by_p_today = defaultdict(list)
    by_p_y = defaultdict(list)
    for row in today_vd["variables"]:
        by_p_today[row["pillar"]].append(row)
    for row in yesterday_vd["variables"]:
        by_p_y[row["pillar"]].append(row)
    print(f"\n  Institutional confidence data_quality comparison:")
    print(f"  {'flag':30s} {YESTERDAY:>10s} {TODAY:>10s}")
    flags_to_check = ["has_momentum", "has_inst_holdings", "has_insider", "has_holdings"]
    for flag in flags_to_check:
        y_cnt = sum(1 for r in by_p_y["institutional_confidence"] if r["data_quality"].get(flag))
        t_cnt = sum(1 for r in by_p_today["institutional_confidence"] if r["data_quality"].get(flag))
        print(f"  {flag:30s} {y_cnt:>10d} {t_cnt:>10d}")
    # Thesis sources
    print(f"\n  Thesis integrity sources used (today):")
    src_today = Counter()
    for r in by_p_today["thesis_integrity"]:
        s = r["data_quality"].get("source") or "<none>"
        src_today[s] += 1
    for k, v in src_today.most_common():
        print(f"    {k:30s} {v:4d}")
    # ocf / margin coverage in financial
    print(f"\n  Financial evolution component coverage (today):")
    for flag in ["has_revenue", "has_margin", "has_ocf"]:
        c = sum(1 for r in by_p_today["financial_evolution"] if r["data_quality"].get(flag))
        print(f"    {flag:30s} {c:4d}")
    # adoption coverage
    print(f"\n  Adoption momentum component coverage (today):")
    for flag in ["has_revenue", "has_trends"]:
        c = sum(1 for r in by_p_today["adoption_momentum"] if r["data_quality"].get(flag))
        print(f"    {flag:30s} {c:4d}")
    # Returns the today dict for downstream
    return by_p_today, by_p_y


def audit_sector_gaps(today_snap, by_p_today):
    section("7. SYSTEMATIC GAPS by sector")
    # Build ticker -> sector map
    sec_map = {s["ticker"]: s.get("sector") for s in today_snap["scores"]}
    # Compute pillar-NaN-equivalent (sub_score=50.0 indicates missing/neutral) by sector
    pillars = ["adoption_momentum", "institutional_confidence", "financial_evolution", "thesis_integrity", "des"]
    # Show OCF coverage by sector for financial
    print("\n  Financial evolution — has_ocf=False (likely banks):")
    misses = []
    for r in by_p_today["financial_evolution"]:
        if not r["data_quality"].get("has_ocf"):
            misses.append((r["ticker"], sec_map.get(r["ticker"])))
    sec_counts = Counter(s for _, s in misses)
    for s, c in sec_counts.most_common():
        print(f"    {s:30s} {c:4d}")
    print(f"  total missing OCF: {len(misses)}")
    # Thesis source breakdown by sector
    print("\n  Thesis source distribution by sector (top 5 sectors):")
    src_by_sec = defaultdict(Counter)
    for r in by_p_today["thesis_integrity"]:
        sec = sec_map.get(r["ticker"])
        src = r["data_quality"].get("source") or "<none>"
        src_by_sec[sec][src] += 1
    for sec, cnt in list(src_by_sec.items())[:8]:
        print(f"    {sec}: {dict(cnt)}")


def main():
    today_snap = jload(DATA / "snapshots" / f"{TODAY}.json")
    yesterday_vd = jload(DATA / "variable_detail" / f"{YESTERDAY}.json")

    coverage, by_p_today_v = audit_today_variable_detail()
    pillar_stats = audit_pillar_distributions(today_snap)
    today_bands = audit_band_distribution_today(today_snap)
    hist_bands, days = audit_band_distribution_90d()
    audit_cross_source_consistency()
    audit_freshness()
    by_p_today, by_p_y = audit_pipeline_integration_health(yesterday_vd)
    audit_sector_gaps(today_snap, by_p_today)


if __name__ == "__main__":
    main()
