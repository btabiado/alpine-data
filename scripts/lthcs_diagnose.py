#!/usr/bin/env python3
"""
LTHCS per-ticker diagnostic tool.

Audits each pillar of the LTHCS composite for a small set of high-conviction
tickers, marking each pillar's input as REAL / PARTIAL / NEUTRAL / STUB /
MISSING and emitting a diagnosis of whether a surprising score is driven by
(a) real signal, (b) calibration, or (c) missing data.

Usage:
    cd ~/Documents/btc-eth-etf-dashboard
    source .venv/bin/activate
    python scripts/lthcs_diagnose.py AAPL INTC NVDA

Options:
    --snapshot YYYY-MM-DD   Pin a specific snapshot (default: latest from index.json)
    --data-root <path>      Override data/lthcs/ root for tests
    --json                  Emit structured JSON instead of human-readable output

Exit codes:
    0  success
    1  file-read / data-load error
    2  one or more requested tickers not found in the snapshot
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


PILLAR_ORDER = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]
PILLAR_DISPLAY = {
    "adoption_momentum": "adoption_momentum",
    "institutional_confidence": "institutional",
    "financial_evolution": "financial_evolution",
    "thesis_integrity": "thesis_integrity",
    "des": "des",
}
DEFAULT_TICKERS = ["AAPL", "INTC", "NVDA"]


# ------------------------------------------------------------------ loaders


def load_json(path: Path) -> Any:
    """Load a JSON file, raising a friendly error on failure."""
    try:
        with path.open("r") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"ERROR: required file missing: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: could not parse {path}: {exc}")


def resolve_snapshot_date(data_root: Path, snapshot: Optional[str]) -> str:
    """Return the snapshot date to use. Read latest from index.json if unset."""
    if snapshot:
        return snapshot
    index = load_json(data_root / "snapshots" / "index.json")
    latest = index.get("latest")
    if not latest:
        raise SystemExit("ERROR: snapshots/index.json has no 'latest' key")
    return latest


def load_universe_map(data_root: Path) -> Dict[str, Dict[str, Any]]:
    """Return ticker -> universe metadata mapping."""
    u = load_json(data_root / "universe.json")
    return {row["ticker"]: row for row in u.get("tickers", [])}


def load_snapshot_map(data_root: Path, date: str) -> Tuple[Dict[str, Dict[str, Any]], str]:
    """Return (ticker -> snapshot row, calc_date)."""
    snap = load_json(data_root / "snapshots" / f"{date}.json")
    return {r["ticker"]: r for r in snap.get("scores", [])}, snap.get("calc_date", date)


def load_variable_detail_map(
    data_root: Path, date: str
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return ticker -> pillar -> detail row."""
    vd = load_json(data_root / "variable_detail" / f"{date}.json")
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in vd.get("variables", []):
        out.setdefault(row["ticker"], {})[row["pillar"]] = row
    return out


def load_narratives_map(data_root: Path, date: str) -> Dict[str, Dict[str, Any]]:
    """Return ticker -> narrative row (optional file; tolerate absence)."""
    path = data_root / "narratives" / f"{date}.json"
    if not path.exists():
        return {}
    n = load_json(path)
    return {r["ticker"]: r for r in n.get("narratives", [])}


def load_sentiment(data_root: Path, ticker: str) -> Optional[Dict[str, Any]]:
    """Optional per-ticker sentiment file."""
    path = data_root / "sentiment" / f"{ticker}.json"
    if not path.exists():
        return None
    return load_json(path)


def load_history(data_root: Path, ticker: str) -> Optional[Dict[str, Any]]:
    path = data_root / "history" / "by_ticker" / f"{ticker}.json"
    if not path.exists():
        return None
    return load_json(path)


def load_rotation(data_root: Path) -> Dict[str, Any]:
    path = data_root / "thesis_rotation.json"
    if not path.exists():
        return {}
    return load_json(path).get("tickers", {})


# ----------------------------------------------------------- pillar status


def classify_pillar(pillar: str, detail: Optional[Dict[str, Any]],
                    sub_score: Optional[float],
                    snapshot_flags: List[str]) -> Tuple[str, str]:
    """
    Return (status, notes) for one pillar.

    Status one of: REAL | PARTIAL | NEUTRAL | STUB | MISSING.

    Logic:
      - MISSING : no variable_detail row for this pillar.
      - STUB    : pillar is dropped (renormalized out) or its stub flag is in
                  the snapshot's data_quality_flags.
      - PARTIAL : at least one core sub-component present + at least one
                  stubbed/missing sub-component (e.g. Adoption has Trends-stub).
      - NEUTRAL : pillar has real data but sub_score landed at 50.0 (no signal).
      - REAL    : everything in the pillar is real.
    """
    if detail is None:
        return ("MISSING", "no variable_detail row for this pillar")

    dq = detail.get("data_quality", {}) or {}
    components = detail.get("components", {}) or {}

    # Pillar-specific stub detection
    flag_map = {
        "thesis_integrity": "thesis_unavailable",
        "institutional_confidence": "institutional_stub",
        "adoption_momentum": "trends_stub",
    }

    notes_parts: List[str] = []

    if pillar == "adoption_momentum":
        has_rev = bool(dq.get("has_revenue"))
        has_trends = bool(dq.get("has_trends"))
        rev_g = components.get("revenue_growth_yoy")
        rev_sub = components.get("revenue_subscore")
        if rev_g is not None:
            notes_parts.append(f"revenue YoY {rev_g*100:+.1f}% -> rev_subscore {rev_sub:.1f}")
        if not has_trends:
            notes_parts.append("Trends sub-component STUB neutral 50 (Adoption renorms internally; revenue carries 100% within pillar)")
        if has_rev and not has_trends:
            return ("PARTIAL", "; ".join(notes_parts))
        if not has_rev and not has_trends:
            return ("STUB", "; ".join(notes_parts) or "no revenue or trends data")
        if has_rev and has_trends:
            if sub_score is not None and abs(sub_score - 50.0) < 0.05:
                return ("NEUTRAL", "; ".join(notes_parts) or "all real data, score lands at 50")
            return ("REAL", "; ".join(notes_parts))
        return ("PARTIAL", "; ".join(notes_parts))

    if pillar == "institutional_confidence":
        has_mom = bool(dq.get("has_momentum"))
        has_inst = bool(dq.get("has_inst_holdings"))
        mom = components.get("momentum_pct_90d")
        mom_sub = components.get("momentum_subscore")
        if mom is not None:
            notes_parts.append(f"90d momentum {mom*100:+.1f}% percentile-vs-universe = {mom_sub:.1f}")
        if not has_inst:
            notes_parts.append("13F holdings sub-component STUB (V1 limitation)")
        if has_mom and not has_inst:
            return ("PARTIAL", "; ".join(notes_parts))
        if has_mom and has_inst:
            if sub_score is not None and abs(sub_score - 50.0) < 0.05:
                return ("NEUTRAL", "; ".join(notes_parts))
            return ("REAL", "; ".join(notes_parts))
        return ("STUB", "; ".join(notes_parts) or "neither momentum nor holdings present")

    if pillar == "financial_evolution":
        has_rev = bool(dq.get("has_revenue"))
        has_mar = bool(dq.get("has_margin"))
        has_ocf = bool(dq.get("has_ocf"))
        rev_sub = components.get("revenue_subscore")
        mar_sub = components.get("margin_subscore")
        ocf_sub = components.get("ocf_subscore")
        ttm_ocf = components.get("ttm_ocf_margin")
        slope = components.get("margin_trend_slope")
        parts = []
        if rev_sub is not None:
            parts.append(f"rev_subscore {rev_sub:.1f}")
        if mar_sub is not None:
            slope_disp = f"{slope:+.4f}" if slope is not None else "n/a"
            parts.append(f"margin_subscore {mar_sub:.1f} (slope {slope_disp})")
        if ocf_sub is not None:
            ocf_disp = f"{ttm_ocf*100:+.1f}%" if ttm_ocf is not None else "n/a"
            parts.append(f"ocf_subscore {ocf_sub:.1f} (ttm_ocf_margin {ocf_disp})")
        notes_parts.extend(parts)
        all_real = has_rev and has_mar and has_ocf
        any_real = has_rev or has_mar or has_ocf
        if all_real:
            if sub_score is not None and abs(sub_score - 50.0) < 0.05:
                return ("NEUTRAL", "; ".join(notes_parts))
            return ("REAL", "; ".join(notes_parts))
        if any_real:
            missing = [k for k, v in (("rev", has_rev), ("margin", has_mar), ("ocf", has_ocf)) if not v]
            notes_parts.append(f"missing sub-components: {', '.join(missing)}")
            return ("PARTIAL", "; ".join(notes_parts))
        return ("STUB", "; ".join(notes_parts) or "no real fundamentals available")

    if pillar == "thesis_integrity":
        has_sent = bool(dq.get("has_sentiment"))
        article_count = components.get("article_count", 0)
        mean_sent = components.get("mean_sentiment_score")
        last_scored = dq.get("last_scored")
        days_since = dq.get("days_since_scored")
        sufficient = bool(dq.get("article_count_sufficient"))
        flagged_unavail = flag_map["thesis_integrity"] in snapshot_flags
        if flagged_unavail:
            notes_parts.append(f"data_quality_flags includes 'thesis_unavailable' -> pillar RENORMED out of composite")
        if last_scored:
            stale_tag = " (stale)" if dq.get("is_stale") else ""
            d = "?" if days_since is None else str(days_since)
            notes_parts.append(f"last_scored={last_scored} ({d}d old){stale_tag}")
        else:
            notes_parts.append("never scored by AV NEWS_SENTIMENT")
        if mean_sent is not None:
            notes_parts.append(f"{article_count} articles, mean_sentiment {mean_sent:+.2f}")
        if flagged_unavail or not has_sent or not sufficient:
            return ("STUB", "; ".join(notes_parts))
        # has_sent and sufficient must both be True here (the inverse case
        # returned STUB above).
        if sub_score is not None and abs(sub_score - 50.0) < 0.05:
            return ("NEUTRAL", "; ".join(notes_parts) or "real sentiment but mean lands at neutral 50")
        return ("REAL", "; ".join(notes_parts))

    if pillar == "des":
        has_macro = bool(dq.get("has_macro_inputs"))
        signals = dq.get("macro_signals_present", 0)
        overrides = components.get("applied_overrides", []) or []
        total = components.get("total_contribution")
        notes_parts.append(f"{signals} macro signals present")
        if overrides:
            notes_parts.append(f"sector/ticker overrides applied: {', '.join(overrides)}")
        if total is not None:
            notes_parts.append(f"total contribution {total:+.3f} -> sub_score {sub_score:.1f}")
        if not has_macro or signals == 0:
            return ("STUB", "; ".join(notes_parts))
        if sub_score is not None and abs(sub_score - 50.0) < 0.05:
            return ("NEUTRAL", "; ".join(notes_parts))
        return ("REAL", "; ".join(notes_parts))

    return ("MISSING", f"unknown pillar '{pillar}'")


# -------------------------------------------------------------- diagnosis


def diagnose(
    snapshot_row: Dict[str, Any],
    pillar_states: List[Tuple[str, str, str]],
) -> List[str]:
    """
    Apply the diagnostic rules from the spec to produce a bullet-list verdict.

    pillar_states: list of (pillar, status, notes) in PILLAR_ORDER.
    """
    statuses = {p: s for (p, s, _n) in pillar_states}
    composite = snapshot_row["lthcs_score"]
    subscores = snapshot_row.get("subscores", {})
    effective = snapshot_row.get("effective_weights", [])
    weighted_components = snapshot_row.get("weighted_components", [])
    dropped = snapshot_row.get("dropped_pillars", [])

    has_stub_partial = any(s in ("STUB", "PARTIAL") for s in statuses.values())
    has_neutral = any(s == "NEUTRAL" for s in statuses.values())
    all_real = all(s == "REAL" for s in statuses.values())

    # Reconciliation check: sum of weighted_components ~= composite (pre-modifiers)
    modifiers = snapshot_row.get("modifiers", {}) or {}
    mod_sum = (
        (modifiers.get("macro_adj") or 0)
        + (modifiers.get("sector_adj") or 0)
        + (modifiers.get("volatility_mod") or 0)
    )
    reconciled = (
        sum(weighted_components) + mod_sum if weighted_components else None
    )
    calibration_drift = (
        None if reconciled is None else abs(reconciled - composite)
    )

    bullets: List[str] = []

    # Headline classification
    if has_stub_partial:
        if any(statuses[p] == "STUB" for p in statuses):
            bullets.append("VERDICT: DATA GAP - one or more pillars are STUBBED and renormed out of the composite. Wait for upstream coverage (e.g. Thesis rotation) before treating this score as decisive.")
        else:
            bullets.append("VERDICT: PARTIAL DATA - all pillars contribute, but some have stubbed sub-components (e.g. Trends inside Adoption, 13F inside Institutional). Score is directionally useful but not fully informed.")
    elif all_real and (composite >= 80 or composite <= 30):
        bullets.append("VERDICT: REAL SIGNAL at an extreme. All 5 pillars use real data and the composite is at an extreme band - the model is telling you something genuine.")
    elif all_real:
        bullets.append("VERDICT: REAL SIGNAL. All 5 pillars use real data; composite is mid-band and reflects what's there.")
    else:
        bullets.append("VERDICT: MIXED. See pillar table for missing/neutral inputs.")

    if calibration_drift is not None and calibration_drift > 0.2:
        bullets.append(
            f"CALIBRATION FLAG: sum(weighted_components)+modifiers = {reconciled:.2f} vs composite {composite:.2f} (delta {calibration_drift:.2f}). Effective-weight reconciliation is off."
        )

    if dropped:
        bullets.append(
            f"Pillars renormalized out: {', '.join(dropped)} - their weight has been redistributed across surviving pillars (see 'effective_weights' column)."
        )

    if has_neutral:
        neutral_pillars = [p for p, s in statuses.items() if s == "NEUTRAL"]
        bullets.append(
            f"NEUTRAL-by-data: {', '.join(neutral_pillars)} have REAL inputs but sub-scores landed at 50 (no directional signal). This is a feature, not a bug - the data simply doesn't tilt."
        )

    # Identify binding constraint and dominant driver
    if subscores and effective and len(effective) == len(PILLAR_ORDER):
        contribs = []
        for i, p in enumerate(PILLAR_ORDER):
            sub = subscores.get(p)
            w = effective[i] if i < len(effective) else 0.0
            if sub is None:
                continue
            contribs.append((p, sub, w, sub * w))
        if contribs:
            contribs_real = [c for c in contribs if statuses.get(c[0]) in ("REAL", "PARTIAL", "NEUTRAL")]
            if contribs_real:
                top = max(contribs_real, key=lambda r: r[3])
                low = min(contribs_real, key=lambda r: r[3])
                bullets.append(
                    f"Top driver: {top[0]} (sub={top[1]:.1f}, weight={top[2]*100:.1f}%, contribution={top[3]:.2f})."
                )
                bullets.append(
                    f"Binding constraint: {low[0]} (sub={low[1]:.1f}, weight={low[2]*100:.1f}%, contribution={low[3]:.2f}). Moving this up has the most score upside."
                )

    return bullets


# -------------------------------------------------------------- formatters


def fmt_pct(weight: float) -> str:
    return f"{weight*100:.0f}%"


def render_section(
    ticker: str,
    universe_row: Optional[Dict[str, Any]],
    snapshot_row: Dict[str, Any],
    detail_map: Dict[str, Dict[str, Any]],
    narrative_row: Optional[Dict[str, Any]],
    sentiment: Optional[Dict[str, Any]],
    history: Optional[Dict[str, Any]],
    rotation: Dict[str, Any],
) -> Tuple[str, List[Tuple[str, str, str]]]:
    """
    Build the human-readable section for one ticker.
    Returns (rendered_text, pillar_states) for downstream cross-ticker summary.
    """
    lines: List[str] = []
    bar = "=" * 75

    name = universe_row.get("name") if universe_row else ticker
    sector = universe_row.get("sector") if universe_row else "?"
    stage = snapshot_row.get("maturity_stage", "?")

    lines.append(bar)
    lines.append(f"  {ticker} - {name} ({sector} . {stage})")
    lines.append(bar)
    lines.append("")
    lines.append(
        f"  Composite: {snapshot_row['lthcs_score']:.1f} ({snapshot_row.get('band','?')})            confidence: {snapshot_row.get('confidence_level','?')}"
    )
    lines.append(
        f"  Drift:     {snapshot_row.get('drift_1d',0):+.1f} (1d) / {snapshot_row.get('drift_30d',0):+.1f} (30d)"
    )
    flags = snapshot_row.get("data_quality_flags", []) or []
    lines.append(f"  Flags:     {flags}")
    dropped = snapshot_row.get("dropped_pillars", []) or []
    lines.append(f"  Dropped pillars (renormalized out): {dropped}")
    lines.append("")
    lines.append("  Pillar breakdown:")
    lines.append("  " + "-" * 71)
    header = "    {p:<19} {s:<7} {w:<7} {sub:<10} {contrib:<13} {notes}"
    lines.append(header.format(p="pillar", s="status", w="weight", sub="sub_score",
                               contrib="contribution", notes="notes"))
    lines.append("  " + "-" * 71)

    subscores = snapshot_row.get("subscores", {})
    weights_used = snapshot_row.get("weights_used", [])
    effective = snapshot_row.get("effective_weights", [])
    weighted_components = snapshot_row.get("weighted_components", [])

    pillar_states: List[Tuple[str, str, str]] = []
    for i, p in enumerate(PILLAR_ORDER):
        detail = detail_map.get(p)
        sub = subscores.get(p)
        eff_w = effective[i] if i < len(effective) else 0.0
        wc = weighted_components[i] if i < len(weighted_components) else 0.0
        status, notes = classify_pillar(p, detail, sub, flags)
        pillar_states.append((p, status, notes))
        sub_disp = "n/a" if sub is None else f"{sub:.1f}"
        contrib_disp = "n/a" if wc is None else f"{wc:.2f}"
        # word-wrap notes naively at ~50 chars for readability
        wrapped = wrap_text(notes, width=50, indent=" " * 50)
        lines.append(
            f"    {PILLAR_DISPLAY[p]:<19} {status:<7} {fmt_pct(eff_w):<7} "
            f"{sub_disp:<10} {contrib_disp:<13} {wrapped}"
        )

    lines.append("  " + "-" * 71)
    total_eff = sum(effective) if effective else 0.0
    composite = snapshot_row["lthcs_score"]
    lines.append(f"    {'Total':<19} {'':<7} {fmt_pct(total_eff):<7} {'':<10} {'':<13} composite={composite:.1f}")
    lines.append("")

    m = snapshot_row.get("modifiers", {}) or {}
    lines.append(
        f"  Modifiers: macro_adj {m.get('macro_adj',0):+.1f}  "
        f"sector_adj {m.get('sector_adj',0):+.1f}  "
        f"volatility_mod {m.get('volatility_mod',0):+.1f}"
    )
    lines.append("")

    lines.append("  --- Diagnosis " + "-" * 60)
    for b in diagnose(snapshot_row, pillar_states):
        lines.append(wrap_paragraph("    " + b, width=72, hang_indent="      "))
    lines.append("")

    # Rotation freshness
    rot = rotation.get(ticker)
    if rot:
        lines.append(f"  Thesis rotation: last_scored {rot.get('last_scored','?')}")
    else:
        lines.append("  Thesis rotation: never scored by AV NEWS_SENTIMENT (in queue)")
    lines.append("")

    # History sparkline
    if history and history.get("history"):
        hist = history["history"][:5]
        items = [f"{h['date']}={h['score']:.1f}" for h in hist]
        lines.append("  History: " + "  ".join(items))
        lines.append("")

    # Narrative
    if narrative_row:
        lines.append("  --- Narrative " + "-" * 58)
        for label, key in (
            ("Today's take",      "todays_take"),
            ("Why changed",       "why_changed"),
            ("Why not to sell",   "why_not_to_sell"),
            ("What would break",  "what_would_break"),
        ):
            txt = narrative_row.get(key) or ""
            lines.append(wrap_paragraph(f"    {label}: {txt}",
                                       width=74, hang_indent=" " * 20))
        lines.append("")

    return "\n".join(lines), pillar_states


def wrap_text(text: str, width: int, indent: str) -> str:
    """Wrap a single notes string onto subsequent indented lines."""
    if not text:
        return ""
    out_lines: List[str] = []
    cur = ""
    for word in text.split():
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= width:
            cur = cur + " " + word
        else:
            out_lines.append(cur)
            cur = word
    if cur:
        out_lines.append(cur)
    return ("\n" + indent).join(out_lines)


def wrap_paragraph(text: str, width: int, hang_indent: str) -> str:
    """Wrap a paragraph preserving leading indent on first line."""
    if not text:
        return ""
    # split off leading spaces
    leading = ""
    rest = text
    for ch in text:
        if ch == " ":
            leading += " "
            rest = rest[1:]
        else:
            break
    words = rest.split()
    out_lines: List[str] = []
    cur = leading
    first = True
    for word in words:
        candidate = (cur + " " + word) if cur.strip() else (cur + word)
        if len(candidate) <= width:
            cur = candidate
        else:
            out_lines.append(cur)
            cur = hang_indent + word
            first = False
    if cur:
        out_lines.append(cur)
    return "\n".join(out_lines)


def render_cross_ticker(
    rows: List[Tuple[str, Dict[str, Any], List[Tuple[str, str, str]]]],
) -> str:
    """
    Bottom comparison table: ticker, composite, band, top driver, weakest, status.
    """
    lines: List[str] = []
    lines.append("=" * 75)
    lines.append("  Cross-ticker comparison")
    lines.append("=" * 75)
    header = "  {t:<6} {c:<10} {b:<13} {top:<22} {low:<22} {st}"
    lines.append(header.format(t="ticker", c="composite", b="band",
                               top="top driver", low="weakest pillar", st="overall"))
    lines.append("  " + "-" * 71)

    for ticker, snap, states in rows:
        statuses = {p: s for (p, s, _n) in states}
        subscores = snap.get("subscores", {})
        effective = snap.get("effective_weights", [])
        contribs = []
        for i, p in enumerate(PILLAR_ORDER):
            sub = subscores.get(p)
            w = effective[i] if i < len(effective) else 0.0
            if sub is None:
                continue
            contribs.append((p, sub, w, sub * w))
        contribs_real = [c for c in contribs if statuses.get(c[0]) in ("REAL", "PARTIAL", "NEUTRAL")]
        if contribs_real:
            top = max(contribs_real, key=lambda r: r[3])
            low = min(contribs_real, key=lambda r: r[3])
            top_disp = f"{top[0][:18]} ({top[1]:.0f})"
            low_disp = f"{low[0][:18]} ({low[1]:.0f})"
        else:
            top_disp = "-"
            low_disp = "-"

        if any(s == "STUB" for s in statuses.values()):
            overall = "DATA GAP"
        elif any(s == "PARTIAL" for s in statuses.values()):
            overall = "PARTIAL"
        elif any(s == "NEUTRAL" for s in statuses.values()):
            overall = "REAL+NEUTRAL"
        else:
            overall = "REAL"

        lines.append(header.format(
            t=ticker,
            c=f"{snap['lthcs_score']:.1f}",
            b=snap.get("band", "?")[:12],
            top=top_disp,
            low=low_disp,
            st=overall,
        ))
    lines.append("  " + "-" * 71)
    return "\n".join(lines)


# ----------------------------------------------------------------- analyze


def analyze_ticker(
    ticker: str,
    data_root: Path,
    date: str,
    universe_map: Dict[str, Dict[str, Any]],
    snapshot_map: Dict[str, Dict[str, Any]],
    detail_map: Dict[str, Dict[str, Dict[str, Any]]],
    narrative_map: Dict[str, Dict[str, Any]],
    rotation: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[List[Tuple[str, str, str]]]]:
    """
    Build a structured analysis dict for one ticker. Returns (data, states)
    or (None, None) if ticker not in snapshot.
    """
    snap = snapshot_map.get(ticker)
    if snap is None:
        return None, None

    detail = detail_map.get(ticker, {})
    flags = snap.get("data_quality_flags", []) or []
    pillar_states: List[Tuple[str, str, str]] = []
    pillar_blocks: List[Dict[str, Any]] = []
    subscores = snap.get("subscores", {})
    effective = snap.get("effective_weights", [])
    weighted_components = snap.get("weighted_components", [])
    for i, p in enumerate(PILLAR_ORDER):
        d = detail.get(p)
        sub = subscores.get(p)
        eff_w = effective[i] if i < len(effective) else 0.0
        wc = weighted_components[i] if i < len(weighted_components) else None
        status, notes = classify_pillar(p, d, sub, flags)
        pillar_states.append((p, status, notes))
        pillar_blocks.append({
            "pillar": p,
            "status": status,
            "effective_weight": eff_w,
            "sub_score": sub,
            "contribution": wc,
            "notes": notes,
            "components": (d or {}).get("components"),
            "data_quality": (d or {}).get("data_quality"),
        })

    data = {
        "ticker": ticker,
        "name": (universe_map.get(ticker) or {}).get("name"),
        "sector": (universe_map.get(ticker) or {}).get("sector"),
        "maturity_stage": snap.get("maturity_stage"),
        "composite": snap.get("lthcs_score"),
        "band": snap.get("band"),
        "confidence_level": snap.get("confidence_level"),
        "drift_1d": snap.get("drift_1d"),
        "drift_30d": snap.get("drift_30d"),
        "modifiers": snap.get("modifiers"),
        "data_quality_flags": flags,
        "dropped_pillars": snap.get("dropped_pillars", []),
        "pillars": pillar_blocks,
        "diagnosis": diagnose(snap, pillar_states),
        "narrative": narrative_map.get(ticker),
        "thesis_rotation": rotation.get(ticker),
        "snapshot_date": date,
    }
    return data, pillar_states


# ---------------------------------------------------------------------- main


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LTHCS per-ticker diagnostic. Audits each pillar as REAL/PARTIAL/NEUTRAL/STUB/MISSING.",
    )
    parser.add_argument("tickers", nargs="*", help="Tickers to audit (defaults to AAPL INTC NVDA)")
    parser.add_argument("--snapshot", help="Pin snapshot date YYYY-MM-DD (default: latest)")
    parser.add_argument("--data-root", default="data/lthcs",
                        help="Data root directory (default: data/lthcs)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit structured JSON instead of human-readable output")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    tickers = [t.upper() for t in (args.tickers or DEFAULT_TICKERS)]
    data_root = Path(args.data_root).resolve()

    # Bail early if data_root is bogus
    if not data_root.exists():
        print(f"ERROR: data root {data_root} does not exist", file=sys.stderr)
        return 1

    try:
        date = resolve_snapshot_date(data_root, args.snapshot)
        universe_map = load_universe_map(data_root)
        snapshot_map, calc_date = load_snapshot_map(data_root, date)
        detail_map = load_variable_detail_map(data_root, date)
        narrative_map = load_narratives_map(data_root, date)
        rotation = load_rotation(data_root)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1

    missing: List[str] = []
    sections: List[Tuple[str, Dict[str, Any], List[Tuple[str, str, str]]]] = []
    json_results: List[Dict[str, Any]] = []
    rendered: List[str] = []

    for tk in tickers:
        data, states = analyze_ticker(
            tk, data_root, date, universe_map, snapshot_map,
            detail_map, narrative_map, rotation,
        )
        if data is None or states is None:
            missing.append(tk)
            continue

        snap_row = snapshot_map[tk]
        sentiment = load_sentiment(data_root, tk)
        history = load_history(data_root, tk)
        section_text, _ = render_section(
            tk,
            universe_map.get(tk),
            snap_row,
            detail_map.get(tk, {}),
            narrative_map.get(tk),
            sentiment,
            history,
            rotation,
        )
        rendered.append(section_text)
        sections.append((tk, snap_row, states))
        # add sentiment + history into json
        data["sentiment_file"] = sentiment
        data["history"] = history
        json_results.append(data)

    if args.as_json:
        out = {
            "snapshot_date": calc_date,
            "tickers_requested": tickers,
            "tickers_missing": missing,
            "results": json_results,
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"LTHCS diagnostic - snapshot {calc_date}")
        print()
        for s in rendered:
            print(s)
        if sections:
            print(render_cross_ticker(sections))

    if missing:
        print("", file=sys.stderr)
        print(f"WARNING: tickers not in snapshot: {', '.join(missing)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
