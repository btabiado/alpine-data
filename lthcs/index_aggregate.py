"""LTHCS Composite Index — single ±100 sentiment score for the universe.

Mirrors the V1 Crypto Trading Dashboard's "Whale Sentiment Index" pattern:
a top-level "where is the long-term-hold market in aggregate" read that
aggregates today's snapshot, modifiers, and additive macro / insider /
13F layers into one number with a labelled band and a per-component
breakdown.

The composite is range [-100, +100], decomposed as:

    Band lean                  ±30   (universe band distribution)
    Adoption pillar avg        ±10
    Institutional pillar avg   ±10
    Financial pillar avg       ±10
    Thesis pillar avg          ±10
    DES pillar avg             ±10
    Macro regime               ±10   (hy_stress / curve_inverted / dollar_strong)
    Insider conviction breadth ±10   (Form 4 universe rollup)
    13F conviction breadth     ±10   (institutional accumulation/distribution)

The shape of the output dict matches what the crypto-dashboard's
:func:`renderWhaleSentiment` consumes, so the front-end can share most
of the gauge / table CSS. Inputs are tolerated as None — a missing layer
just drops that component from the breakdown and renormalises the score.

Pure compute. No I/O. No HTTP. Wire it into ``lthcs_daily.py`` between
narrative generation and persistence so the resulting JSON is written by
the existing Stage 8 path.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional


# Universe-band buckets for "lean":
#   bullish = elite + high_confidence + constructive
#   bearish = weakening + review
#   monitor stays neutral so it doesn't get double-counted with a constructive tilt
_BULLISH_BANDS = ("elite", "high_confidence", "constructive")
_BEARISH_BANDS = ("weakening", "review")

# Pillar names (must match the keys in snapshot_rows[i]["subscores"] and
# the in-progress score.PILLAR_ORDER).
_PILLAR_KEYS = (
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
)

# Cap each pillar component contribution at ±10. The mean lives in
# [0, 100]; the centered version (mean - 50) lives in [-50, +50]. Dividing
# by 5 gives ±10. This matches what the spec calls out.
_PILLAR_CAP = 10
_BAND_LEAN_CAP = 30
_MACRO_CAP = 10
_INSIDER_CAP = 10
_HOLDINGS_CAP = 10

# Label thresholds. The spec uses the standard ±60 / ±30 bands.
def _label_for(score: int) -> str:
    if score >= 60:
        return "LTHCS ELITE"
    if score >= 30:
        return "LTHCS CONSTRUCTIVE"
    if score > -30:
        return "LTHCS NEUTRAL"
    if score > -60:
        return "LTHCS WEAKENING"
    return "LTHCS DISTRIBUTING"


# Map label → band key in weights.json.score_bands. We use the bright
# variant of the band color for the headline number; the front-end picks
# its own foreground based on `color`.
_LABEL_BAND_KEY: Dict[str, str] = {
    "LTHCS ELITE": "elite",
    "LTHCS CONSTRUCTIVE": "constructive",
    "LTHCS NEUTRAL": "monitor",
    "LTHCS WEAKENING": "weakening",
    "LTHCS DISTRIBUTING": "review",
}

# Bright variants matching --band-*-bright tokens in lthcs.css. Frontend
# can override; this is the server-side hint.
_BAND_BRIGHT_COLOR: Dict[str, str] = {
    "elite": "#4D7AB5",
    "high_confidence": "#6FD18C",
    "constructive": "#E9C04A",
    "monitor": "#F0A861",
    "weakening": "#E27A5C",
    "review": "#C25640",
}


# ---------------------------------------------------------------------------
# Component arithmetic helpers
# ---------------------------------------------------------------------------

def _clip(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _band_lean(snapshot_rows: Iterable[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """Compute the universe band lean component.

    Lean = (% bullish bands) - (% bearish bands), scaled to [-30, +30].
    """
    rows = list(snapshot_rows or [])
    if not rows:
        return None
    bullish = 0
    bearish = 0
    for row in rows:
        band = row.get("band")
        if band in _BULLISH_BANDS:
            bullish += 1
        elif band in _BEARISH_BANDS:
            bearish += 1
    total = len(rows)
    # Lean is the net spread on a [-1, +1] axis; scale by _BAND_LEAN_CAP.
    lean_axis = (bullish - bearish) / total
    delta = int(round(_clip(lean_axis * _BAND_LEAN_CAP, -_BAND_LEAN_CAP, _BAND_LEAN_CAP)))
    lean_pct = (bullish - bearish) / total * 100.0
    if delta <= -20:
        read = "universe distributing"
    elif delta < 0:
        read = "universe weakening"
    elif delta == 0:
        read = "universe balanced"
    elif delta < 20:
        read = "universe constructive"
    else:
        read = "universe broadly bullish"
    return {
        "name": "Band lean (bullish % minus bearish %)",
        "value": "%+.1f%%" % lean_pct,
        "delta": delta,
        "read": read,
    }


_PILLAR_DISPLAY: Dict[str, str] = {
    "adoption_momentum": "Adoption pillar avg",
    "institutional_confidence": "Institutional pillar avg",
    "financial_evolution": "Financial pillar avg",
    "thesis_integrity": "Thesis pillar avg",
    "des": "DES (demand environment) avg",
}

_PILLAR_READS: Dict[str, Dict[str, str]] = {
    "adoption_momentum": {
        "neg": "adoption fading",
        "neutral": "adoption neutral",
        "pos": "adoption strengthening",
    },
    "institutional_confidence": {
        "neg": "institutional confidence eroding",
        "neutral": "institutional steady",
        "pos": "institutional accumulation",
    },
    "financial_evolution": {
        "neg": "fundamentals softening",
        "neutral": "fundamentals stable",
        "pos": "fundamentals constructive",
    },
    "thesis_integrity": {
        "neg": "narrative bearish",
        "neutral": "narrative mixed",
        "pos": "sentiment supportive",
    },
    "des": {
        "neg": "macro headwind",
        "neutral": "macro neutral",
        "pos": "macro tailwind",
    },
}


def _pillar_mean_component(
    snapshot_rows: Iterable[Mapping[str, Any]],
    pillar: str,
) -> Optional[Dict[str, Any]]:
    rows = list(snapshot_rows or [])
    if not rows:
        return None
    vals: List[float] = []
    for row in rows:
        subs = row.get("subscores") or {}
        v = _safe_float(subs.get(pillar))
        if v is not None:
            vals.append(v)
    if not vals:
        return None
    mean = sum(vals) / len(vals)
    centered = mean - 50.0
    raw = centered / 5.0  # ±10 cap when centered hits ±50
    delta = int(round(_clip(raw, -_PILLAR_CAP, _PILLAR_CAP)))
    reads = _PILLAR_READS[pillar]
    if delta <= -3:
        read = reads["neg"]
    elif delta >= 3:
        read = reads["pos"]
    else:
        read = reads["neutral"]
    return {
        "name": _PILLAR_DISPLAY[pillar],
        "value": round(mean, 1),
        "delta": delta,
        "read": read,
    }


def _macro_component(breadth_snapshot: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """+5 per non-flag tripped; -5 per flag tripped. Capped ±10."""
    if not breadth_snapshot:
        return None
    flags = breadth_snapshot.get("regime_flags") or {}
    keys = ("hy_stress", "curve_inverted", "dollar_strong")
    tripped = [k for k in keys if bool(flags.get(k))]
    clean = [k for k in keys if not bool(flags.get(k))]
    # +5 each non-flag, -5 each tripped, clipped to ±10.
    raw = (len(clean) * 5) - (len(tripped) * 5)
    delta = int(_clip(raw, -_MACRO_CAP, _MACRO_CAP))
    if not tripped:
        label = "clean"
        read = "risk-on backdrop"
    elif len(tripped) == 1:
        label = "one flag: %s" % tripped[0]
        read = "macro mixed"
    elif len(tripped) == 2:
        label = "two flags: %s" % ", ".join(tripped)
        read = "macro headwind"
    else:
        label = "all flags tripped"
        read = "macro stress"
    return {
        "name": "Macro regime (HY OAS / curve / USD)",
        "value": label,
        "delta": delta,
        "read": read,
    }


_INSIDER_STRONG_BUY = ("strong_buying", "cluster_buying")
_INSIDER_HEAVY_SELL = ("heavy_selling",)


def _insider_component(insider_by_ticker: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not insider_by_ticker:
        return None
    strong = 0
    heavy = 0
    for sym, info in insider_by_ticker.items():
        if not isinstance(info, Mapping):
            continue
        regime = info.get("regime")
        cluster = bool(info.get("cluster_buying"))
        if regime in _INSIDER_STRONG_BUY or cluster:
            strong += 1
        elif regime in _INSIDER_HEAVY_SELL:
            heavy += 1
    total_active = strong + heavy
    # Scale: (strong - heavy) / max(total_active, 6) capped ±1, then ±10.
    # We use max(total_active, 6) so a tiny universe with 1 signal doesn't
    # swing the whole composite. Realistic universes have ~10-30 active.
    if total_active == 0:
        delta = 0
        read = "insider flow quiet"
    else:
        denom = max(total_active, 6)
        axis = (strong - heavy) / denom
        delta = int(round(_clip(axis * _INSIDER_CAP, -_INSIDER_CAP, _INSIDER_CAP)))
        if delta <= -5:
            read = "insiders distributing"
        elif delta < 0:
            read = "mixed insider flow"
        elif delta == 0:
            read = "insiders balanced"
        elif delta < 5:
            read = "modest insider buying"
        else:
            read = "insiders accumulating"
    value = "%d active signals" % total_active if total_active else "no active signals"
    return {
        "name": "Insider conviction breadth",
        "value": value,
        "delta": delta,
        "read": read,
    }


def _holdings_component(holdings_by_ticker: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not holdings_by_ticker:
        return None
    accumulating = 0
    distributing = 0
    for sym, info in holdings_by_ticker.items():
        if not isinstance(info, Mapping):
            continue
        sig = info.get("conviction_signal")
        if sig == "accumulating":
            accumulating += 1
        elif sig == "distributing":
            distributing += 1
    total_active = accumulating + distributing
    if total_active == 0:
        delta = 0
        read = "institutions steady"
    else:
        denom = max(total_active, 6)
        axis = (accumulating - distributing) / denom
        delta = int(round(_clip(axis * _HOLDINGS_CAP, -_HOLDINGS_CAP, _HOLDINGS_CAP)))
        if delta <= -5:
            read = "institutions distributing"
        elif delta < 0:
            read = "institutional rebalancing soft"
        elif delta == 0:
            read = "institutions balanced"
        elif delta < 5:
            read = "modest institutional buying"
        else:
            read = "institutions accumulating"
    value = "%d vs %d" % (accumulating, distributing)
    return {
        "name": "13F conviction breadth (acc vs dist)",
        "value": value,
        "delta": delta,
        "read": read,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_lthcs_index(
    snapshot_rows: List[Dict[str, Any]],
    variable_detail_rows: Optional[List[Dict[str, Any]]] = None,
    insider_by_ticker: Optional[Mapping[str, Any]] = None,
    holdings_by_ticker: Optional[Mapping[str, Any]] = None,
    breadth_snapshot: Optional[Mapping[str, Any]] = None,
    breadth_sentiment_snapshot: Optional[Mapping[str, Any]] = None,
    sector_strength: Optional[Mapping[str, Any]] = None,
    as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """Aggregate the LTHCS universe into one ±100 composite.

    Inputs mirror the ``PipelineState`` fields the daily pipeline already
    builds. Any input may be ``None``; the corresponding component is
    dropped from the breakdown and the composite renormalises.

    Returns a dict with ``score`` (int in [-100, +100]), ``label`` (one
    of ``LTHCS ELITE / CONSTRUCTIVE / NEUTRAL / WEAKENING / DISTRIBUTING``),
    ``color`` (band-bright hex), ``components`` (list of dicts with
    ``name``, ``value``, ``delta``, ``read``), and a ``note`` disclaimer.
    """
    _ = variable_detail_rows  # accepted for symmetry; pillar avgs use snapshot subscores
    _ = breadth_sentiment_snapshot  # accepted for forward compat; not yet scored
    _ = sector_strength  # accepted for forward compat; not yet scored

    components: List[Dict[str, Any]] = []
    total = 0

    band = _band_lean(snapshot_rows)
    if band is not None:
        components.append(band)
        total += band["delta"]

    for pillar in _PILLAR_KEYS:
        comp = _pillar_mean_component(snapshot_rows, pillar)
        if comp is not None:
            components.append(comp)
            total += comp["delta"]

    macro = _macro_component(breadth_snapshot)
    if macro is not None:
        components.append(macro)
        total += macro["delta"]

    insider = _insider_component(insider_by_ticker)
    if insider is not None:
        components.append(insider)
        total += insider["delta"]

    holdings = _holdings_component(holdings_by_ticker)
    if holdings is not None:
        components.append(holdings)
        total += holdings["delta"]

    score = int(_clip(total, -100, 100))
    label = _label_for(score)
    band_key = _LABEL_BAND_KEY[label]
    color = _BAND_BRIGHT_COLOR.get(band_key, "#E27A5C")

    n_tickers = len(list(snapshot_rows or []))
    return {
        "as_of": as_of or "",
        "score": score,
        "label": label,
        "color": color,
        "band_key": band_key,
        "components": components,
        "note": (
            "Aggregate of %d-ticker LTHCS universe. Directional read, "
            "not a trading signal." % n_tickers
        ),
    }
