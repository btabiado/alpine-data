"""Money Flow Index (MFX) — pure-compute money-flow indicators + composite.

This module implements the math layer for the Money Flow Index gauge described
in ``SPEC_money_flow_index.md``. It has **no network dependencies for the math**:
:func:`build_money_flow_index` consumes a payload of already-fetched per-index
legs (ETF flows, computed buy/sell-pressure ratios, ICI mutual-fund flows, and
money-market WoW changes) and fuses them into a single ±100 gauge plus three
first-class per-index sub-gauges (Dow / S&P 500 / Nasdaq).

The three classic indicators — :func:`mfi`, :func:`cmf`, :func:`obv` — are pure
functions over OHLCV bars (``{date, open, high, low, close, volume}``). The
orchestrator (``fetch_market.py``) computes MFI/CMF per index from widened
``yahoo_chart_history()`` output and hands the trailing series into the payload.

Composite method (mirrors the LTHCS / CNN Fear & Greed scorer):

* Reuses :func:`lthcs.normalize.z_score` and :func:`lthcs.normalize.z_to_0_100`.
* Each component → trailing z-score (against its own rolling history) → centered
  to ±100 via ``(z_to_0_100(z) - 50) * 2``.
* **Per-index sub-score** = mean(z(ETF flow), z(MFI−50)) → ±100.
* **Market headline** = blend of [dollar-weighted mean of the 3 sub-scores] +
  [ICI equity mutual-fund-flow z] − [MMF WoW-change z, INVERTED], capped ±100,
  band-labelled.

Guardrails honoured here:

* **No double-counting** — ETF flow is per-index only; ICI MF flow + MMF are
  market-wide only. They never mix.
* **Inverted MMF** — rising "cash on the sidelines" is bearish for equity flow,
  so the MMF WoW-change z enters with a negative sign.
* **Graceful degradation** — a missing leg collapses to neutral (sub-score 0 /
  MFI 50) and the remaining headline components are renormalised; the gauge is
  never blank and the build never crashes.

Defensive throughout: every public function returns ``None`` / ``[]`` / a
neutral dict on bad input rather than raising.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from lthcs.normalize import z_score, z_to_0_100

__all__ = [
    "mfi",
    "cmf",
    "obv",
    "build_money_flow_index",
]


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------

def _f(value: Any) -> Optional[float]:
    """Coerce ``value`` to a finite float, or ``None`` if impossible/NaN/inf."""
    try:
        if value is None:
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    # NaN != NaN; inf is also unusable for our purposes.
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _bar_ohlcv(bar: Mapping[str, Any]) -> Optional[Dict[str, float]]:
    """Extract finite high/low/close/volume from a single bar, or None.

    ``open`` is tolerated-but-unused by the money-flow indicators; only
    high/low/close/volume are required for MFI/CMF/OBV.
    """
    if not isinstance(bar, Mapping):
        return None
    h = _f(bar.get("high"))
    l = _f(bar.get("low"))
    c = _f(bar.get("close"))
    v = _f(bar.get("volume"))
    if h is None or l is None or c is None or v is None:
        return None
    return {"high": h, "low": l, "close": c, "volume": v}


def _clean_bars(bars: Optional[Sequence[Mapping[str, Any]]]) -> List[Dict[str, float]]:
    """Return the bars with usable high/low/close/volume, in input order."""
    if not bars:
        return []
    out: List[Dict[str, float]] = []
    for b in bars:
        ohlcv = _bar_ohlcv(b)
        if ohlcv is not None:
            out.append(ohlcv)
    return out


def _clip(value: float, lo: float, hi: float) -> float:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


# ---------------------------------------------------------------------------
# 1) Money Flow Index
# ---------------------------------------------------------------------------

def mfi(bars: Optional[Sequence[Mapping[str, Any]]], period: int = 14) -> Optional[float]:
    """Money Flow Index over the last ``period`` bars.

    Algorithm (standard 14-day MFI):

    * typical price ``tp = (high + low + close) / 3`` for each bar.
    * raw money flow ``rmf = tp * volume``.
    * each bar's rmf is *positive* when its tp rises vs the prior bar's tp,
      *negative* when it falls (ties are dropped, per convention).
    * ``MFI = 100 - 100 / (1 + sum(positive) / sum(negative))`` over the last
      ``period`` directional bars.

    Returns the latest MFI in ``[0, 100]``, or ``None`` when there is
    insufficient/invalid data (need at least ``period + 1`` clean bars so we
    have ``period`` direction comparisons).

    Edge cases:
    * All flow positive (no down days) -> 100.0.
    * All flow negative (no up days)   -> 0.0.
    """
    if not isinstance(period, int) or period < 1:
        return None
    clean = _clean_bars(bars)
    if len(clean) < period + 1:
        return None

    # Typical price for every clean bar.
    tps = [(b["high"] + b["low"] + b["close"]) / 3.0 for b in clean]
    rmf = [tps[i] * clean[i]["volume"] for i in range(len(clean))]

    # Walk the last `period` directional comparisons (bar i vs bar i-1).
    pos = 0.0
    neg = 0.0
    start = len(clean) - period  # first index whose vs-prior comparison we count
    for i in range(start, len(clean)):
        if tps[i] > tps[i - 1]:
            pos += rmf[i]
        elif tps[i] < tps[i - 1]:
            neg += rmf[i]
        # tp unchanged -> neither (standard MFI convention)

    if neg == 0.0:
        # No negative money flow over the window.
        return 100.0 if pos > 0.0 else 50.0
    money_ratio = pos / neg
    value = 100.0 - (100.0 / (1.0 + money_ratio))
    return float(_clip(value, 0.0, 100.0))


# ---------------------------------------------------------------------------
# 2) Chaikin Money Flow
# ---------------------------------------------------------------------------

def cmf(bars: Optional[Sequence[Mapping[str, Any]]], period: int = 20) -> Optional[float]:
    """Chaikin Money Flow over the last ``period`` bars.

    ``CMF = sum( mfv ) / sum( volume )`` over the window, where each bar's
    money-flow volume is::

        mfm = ((close - low) - (high - close)) / (high - low)   # money-flow multiplier
        mfv = mfm * volume

    Range ``[-1, +1]``. Bars where ``high == low`` (no range) contribute a
    multiplier of 0 (guard against division by zero) but still count their
    volume in the denominator — matching the standard treatment.

    Returns the latest CMF, or ``None`` on insufficient/invalid data
    (need at least ``period`` clean bars), or ``None`` if total window volume
    is zero.
    """
    if not isinstance(period, int) or period < 1:
        return None
    clean = _clean_bars(bars)
    if len(clean) < period:
        return None

    window = clean[-period:]
    mfv_sum = 0.0
    vol_sum = 0.0
    for b in window:
        h, l, c, v = b["high"], b["low"], b["close"], b["volume"]
        rng = h - l
        if rng <= 0.0:
            mfm = 0.0  # guard h == l (also covers bad h < l)
        else:
            mfm = ((c - l) - (h - c)) / rng
        mfv_sum += mfm * v
        vol_sum += v

    if vol_sum == 0.0:
        return None
    value = mfv_sum / vol_sum
    return float(_clip(value, -1.0, 1.0))


# ---------------------------------------------------------------------------
# 3) On-Balance Volume
# ---------------------------------------------------------------------------

def obv(bars: Optional[Sequence[Mapping[str, Any]]]) -> List[float]:
    """On-Balance Volume series, one value per clean bar.

    Starts at 0.0 on the first bar; thereafter adds the bar's volume when the
    close rises vs the prior close, subtracts it when the close falls, and
    leaves OBV unchanged when the close is flat.

    Returns ``[]`` when there are no usable bars.
    """
    clean = _clean_bars(bars)
    if not clean:
        return []
    out: List[float] = [0.0]
    for i in range(1, len(clean)):
        prev_c = clean[i - 1]["close"]
        cur_c = clean[i]["close"]
        vol = clean[i]["volume"]
        if cur_c > prev_c:
            out.append(out[-1] + vol)
        elif cur_c < prev_c:
            out.append(out[-1] - vol)
        else:
            out.append(out[-1])
    return out


# ---------------------------------------------------------------------------
# 4) Composite — build_money_flow_index
# ---------------------------------------------------------------------------

# Per-index ETF -> display-name map (the three first-class sub-gauges).
_INDEX_MAP = (
    ("DIA", "Dow"),
    ("SPY", "S&P 500"),
    ("QQQ", "Nasdaq"),
)

# Band labels for the ±100 score (per spec).
def _band_label(score: float) -> str:
    if score <= -60:
        return "Heavy Outflow"
    if score <= -30:
        return "Outflow"
    if score < 30:
        return "Neutral"
    if score < 60:
        return "Inflow"
    return "Heavy Inflow"


def _z_to_pm100(z: Optional[float]) -> float:
    """Map a z-score to ±100 via the shared 0..100 normaliser, centered.

    ``(z_to_0_100(z) - 50) * 2`` -> z=0 → 0, z=+clip → +100, z=−clip → −100.
    A ``None`` z (missing component) collapses to neutral 0.0, mirroring the
    z_to_0_100 NaN→50 fallback.
    """
    if z is None:
        return 0.0
    return float((z_to_0_100(z) - 50.0) * 2.0)


def _component_z(today: Any, trailing: Any) -> Optional[float]:
    """z-score of ``today`` against its ``trailing`` history, or None.

    Returns ``None`` when today is non-finite or the trailing series has fewer
    than 2 usable points (so the caller can treat the component as missing and
    renormalise, rather than silently scoring it 0).
    """
    val = _f(today)
    if val is None:
        return None
    if not trailing:
        return None
    series = [x for x in (_f(t) for t in trailing) if x is not None]
    if len(series) < 2:
        return None
    z = z_score(val, series)
    # z_score returns NaN only when value is NaN (already guarded) — but be safe.
    if z != z:
        return None
    return float(z)


def _per_index_subscore(leg: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compute one index's sub-gauge from its leg payload.

    Expected leg shape (all optional; missing -> neutral)::

        {
          "etf_flow":      <float today's ΔSO×NAV flow, $>,
          "etf_flow_hist": [<trailing etf_flow series>],
          "mfi":           <float latest MFI 0..100>,
          "mfi_hist":      [<trailing MFI series>],
          "cmf":           <float latest CMF -1..1>,   # carried through for UI
          "dollar_volume": <float, for dollar-weighting the headline>,
        }

    Sub-score = mean of the available components in
    [ z(etf_flow), z(mfi - 50) ] each mapped to ±100. A component is "available"
    only when it has both a finite today value and >=2 trailing points; missing
    components drop out and the mean is taken over what remains. If *nothing* is
    available the sub-score is neutral 0.
    """
    leg = leg if isinstance(leg, Mapping) else {}

    # ETF-flow component.
    flow_today = leg.get("etf_flow")
    flow_hist = leg.get("etf_flow_hist")
    z_flow = _component_z(flow_today, flow_hist)

    # MFI(-50) component — center MFI so 50 (neutral) maps to 0 before z-scoring.
    mfi_today = _f(leg.get("mfi"))
    mfi_hist = leg.get("mfi_hist")
    z_mfi: Optional[float] = None
    if mfi_today is not None and mfi_hist:
        centered_hist = [v - 50.0 for v in (_f(t) for t in mfi_hist) if v is not None]
        z_mfi = _component_z(mfi_today - 50.0, centered_hist)

    parts: List[float] = []
    if z_flow is not None:
        parts.append(_z_to_pm100(z_flow))
    if z_mfi is not None:
        parts.append(_z_to_pm100(z_mfi))

    if parts:
        sub = sum(parts) / len(parts)
    else:
        sub = 0.0  # neutral fallback (no usable components)
    sub = float(_clip(sub, -100.0, 100.0))

    return {
        "score": sub,
        "label": _band_label(sub),
        "mfi": mfi_today,
        "cmf": _f(leg.get("cmf")),
        "etf_flow": _f(flow_today),
        "_dollar_volume": _f(leg.get("dollar_volume")),
        "_z_flow": z_flow,
        "_z_mfi": z_mfi,
    }


def build_money_flow_index(payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build the Money Flow Index (MFX) composite dict from a payload.

    Parameters
    ----------
    payload:
        A mapping holding ``payload["market"]`` with the per-index legs and the
        market-wide ICI / MMF legs::

            payload["market"] = {
              "DIA": <leg>, "SPY": <leg>, "QQQ": <leg>,   # per-index (see _per_index_subscore)
              "ici_equity_flow":       <float today's ICI equity MF net flow, $>,
              "ici_equity_flow_hist":  [<trailing ICI equity-flow series>],
              "mmf_wow_change":        <float latest MMF week-over-week change, $>,
              "mmf_wow_change_hist":   [<trailing MMF WoW-change series>],
              "as_of":                 "YYYY-MM-DD",        # optional
            }

    Returns
    -------
    dict
        ``{as_of, headline:{score,label,components:[...]}, per_index:[...]}``.
        Always returns a well-formed dict; missing legs degrade to neutral and
        the headline renormalises over the components that are present.

    Guardrails
    ----------
    * ETF flow is consumed per-index only; ICI MF flow + MMF are market-wide
      only. They are never summed together (no double-counting).
    * The MMF WoW-change z-score enters **inverted** (negated): a build-up of
      cash on the sidelines pulls the gauge toward outflow.
    """
    market = (payload or {}).get("market") if isinstance(payload, Mapping) else None
    market = market if isinstance(market, Mapping) else {}

    # --- Per-index sub-gauges -------------------------------------------------
    per_index: List[Dict[str, Any]] = []
    sub_details: List[Dict[str, Any]] = []
    for etf, name in _INDEX_MAP:
        d = _per_index_subscore(market.get(etf))
        per_index.append({
            "index": name,
            "etf": etf,
            "score": d["score"],
            "label": d["label"],
            "mfi": d["mfi"],
            "cmf": d["cmf"],
            "etf_flow": d["etf_flow"],
        })
        sub_details.append({"etf": etf, "name": name, **d})

    # Dollar-weighted mean of the 3 sub-scores. Fall back to equal weight when
    # no dollar-volume info is supplied (so the legs still count).
    weights = [(d["_dollar_volume"] or 0.0) for d in sub_details]
    wsum = sum(weights)
    if wsum > 0.0:
        index_blend = sum(d["score"] * w for d, w in zip(sub_details, weights)) / wsum
    else:
        index_blend = sum(d["score"] for d in sub_details) / len(sub_details)
    index_blend = float(_clip(index_blend, -100.0, 100.0))

    # --- Market-wide legs -----------------------------------------------------
    # ICI equity mutual-fund flow (positive z -> inflow, enters as-is).
    z_ici = _component_z(
        market.get("ici_equity_flow"),
        market.get("ici_equity_flow_hist"),
    )
    # Money-market WoW change (positive z -> cash building up -> bearish -> INVERT).
    z_mmf = _component_z(
        market.get("mmf_wow_change"),
        market.get("mmf_wow_change_hist"),
    )

    # --- Headline blend (equal-weight over the components that are present) ----
    components: List[Dict[str, Any]] = []
    contributions: List[float] = []

    # Component 1: dollar-weighted per-index blend (always present).
    components.append({
        "name": "Index ETF + buy/sell flow",
        "z": None,
        "contribution": index_blend,
        "explain": (
            "Dollar-weighted mean of the Dow / S&P 500 / Nasdaq sub-gauges "
            "(per-index ETF ΔSO×NAV flow + MFI buy/sell pressure)."
        ),
    })
    contributions.append(index_blend)

    # Component 2: ICI equity mutual-fund flow.
    if z_ici is not None:
        c_ici = _z_to_pm100(z_ici)
        components.append({
            "name": "Equity mutual-fund flow (ICI)",
            "z": round(z_ici, 4),
            "contribution": c_ici,
            "explain": (
                "Market-wide ICI weekly equity mutual-fund net flow vs its "
                "trailing history. Positive = retail inflow."
            ),
        })
        contributions.append(c_ici)

    # Component 3: money-market WoW change, INVERTED.
    if z_mmf is not None:
        c_mmf = _z_to_pm100(-z_mmf)  # invert: rising cash = bearish
        components.append({
            "name": "Money-market cash (inverted)",
            "z": round(z_mmf, 4),
            "contribution": c_mmf,
            "explain": (
                "Market-wide money-market-fund week-over-week change vs trailing "
                "history, INVERTED: cash piling up on the sidelines pulls toward "
                "outflow."
            ),
        })
        contributions.append(c_mmf)

    headline_score = sum(contributions) / len(contributions) if contributions else 0.0
    headline_score = float(_clip(headline_score, -100.0, 100.0))

    as_of = market.get("as_of") if isinstance(market.get("as_of"), str) else None
    if not as_of:
        as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "as_of": as_of,
        "headline": {
            "score": round(headline_score, 2),
            "label": _band_label(headline_score),
            "components": [
                {
                    "name": c["name"],
                    "z": c["z"],
                    "contribution": round(c["contribution"], 2),
                    "explain": c["explain"],
                }
                for c in components
            ],
        },
        "per_index": [
            {
                "index": p["index"],
                "etf": p["etf"],
                "score": round(p["score"], 2),
                "label": p["label"],
                "mfi": round(p["mfi"], 2) if p["mfi"] is not None else None,
                "cmf": round(p["cmf"], 4) if p["cmf"] is not None else None,
                "etf_flow": p["etf_flow"],
            }
            for p in per_index
        ],
    }


# ---------------------------------------------------------------------------
# Self-test / live verification
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import urllib.request

    def _fetch_ohlcv(ticker: str) -> List[Dict[str, Any]]:
        """Fetch ~6mo daily OHLCV for one ticker straight from Yahoo (self-test only)."""
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            "?range=6mo&interval=1d"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                j = json.load(resp)
        except Exception as exc:  # noqa: BLE001 - self-test only
            print(f"  [{ticker}] fetch failed: {exc}")
            return []
        try:
            result = j["chart"]["result"][0]
            ts = result.get("timestamp") or []
            q = result["indicators"]["quote"][0]
            o, h, l, c, v = (q.get(k) or [] for k in ("open", "high", "low", "close", "volume"))
        except (KeyError, IndexError, TypeError):
            return []
        bars: List[Dict[str, Any]] = []
        for i, t in enumerate(ts):
            if t is None or i >= len(c) or c[i] is None:
                continue
            bars.append({
                "date": datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d"),
                "open": o[i] if i < len(o) else None,
                "high": h[i] if i < len(h) else None,
                "low": l[i] if i < len(l) else None,
                "close": c[i],
                "volume": v[i] if i < len(v) else 0,
            })
        return bars

    print("=" * 64)
    print("PART A — real MFI/CMF/OBV from live Yahoo 6mo daily OHLCV")
    print("=" * 64)
    live_mfi: Dict[str, Optional[float]] = {}
    live_series: Dict[str, List[Dict[str, Any]]] = {}
    for tk in ("SPY", "QQQ", "DIA"):
        bars = _fetch_ohlcv(tk)
        live_series[tk] = bars
        m = mfi(bars, 14)
        cm = cmf(bars, 20)
        ob = obv(bars)
        live_mfi[tk] = m
        print(f"\n{tk}: {len(bars)} bars  ({bars[0]['date'] if bars else '-'} .. "
              f"{bars[-1]['date'] if bars else '-'})")
        print(f"  MFI(14) = {m}")
        print(f"  CMF(20) = {cm}")
        print(f"  OBV last = {ob[-1] if ob else None}  (len {len(ob)})")

    print("\n" + "=" * 64)
    print("PART B — composite build_money_flow_index() on a SYNTHETIC payload")
    print("=" * 64)

    # Build trailing MFI history per ticker from the live series (rolling MFI),
    # so the synthetic payload uses realistic, non-degenerate distributions.
    def _rolling_mfi(bars: List[Dict[str, Any]], n: int = 30) -> List[float]:
        out: List[float] = []
        for end in range(15, len(bars) + 1):
            val = mfi(bars[:end], 14)
            if val is not None:
                out.append(val)
        return out[-n:]

    synth_market: Dict[str, Any] = {"as_of": "2026-06-07"}
    # Per-index legs. ETF-flow history is synthetic (no flow feed in self-test);
    # MFI history is derived from the real price series.
    flow_today = {"DIA": 4.2e8, "SPY": 9.5e8, "QQQ": -3.1e8}
    flow_hist_base = {
        "DIA": [1.0e8, -2.0e8, 3.0e8, 1.5e8, -1.0e8, 2.2e8, 0.5e8, -0.8e8, 1.1e8, 2.0e8],
        "SPY": [5.0e8, -3.0e8, 8.0e8, 2.0e8, -4.0e8, 6.0e8, 1.0e8, -2.0e8, 4.0e8, 7.0e8],
        "QQQ": [2.0e8, 3.0e8, -1.0e8, 4.0e8, -2.0e8, 1.0e8, 3.5e8, -0.5e8, 2.5e8, 1.5e8],
    }
    dollar_vol = {"DIA": 1.2e9, "SPY": 3.5e10, "QQQ": 2.1e10}
    for etf in ("DIA", "SPY", "QQQ"):
        bars = live_series[etf]
        mfi_hist = _rolling_mfi(bars)
        synth_market[etf] = {
            "etf_flow": flow_today[etf],
            "etf_flow_hist": flow_hist_base[etf],
            "mfi": live_mfi[etf] if live_mfi[etf] is not None else 50.0,
            "mfi_hist": mfi_hist if mfi_hist else [45, 50, 55, 48, 52],
            "cmf": cmf(bars, 20),
            "dollar_volume": dollar_vol[etf],
        }

    # Market-wide legs (synthetic, $).
    synth_market["ici_equity_flow"] = 1.8e9
    synth_market["ici_equity_flow_hist"] = [
        -2.0e9, 1.0e9, -0.5e9, 0.8e9, -1.2e9, 0.3e9, 1.5e9, -0.7e9, 0.9e9, -0.4e9,
    ]
    # MMF WoW change: a big build-up of cash this week (bearish -> should pull DOWN).
    synth_market["mmf_wow_change"] = 6.0e10
    synth_market["mmf_wow_change_hist"] = [
        1.0e10, -0.5e10, 2.0e10, 0.5e10, -1.0e10, 1.5e10, 0.2e10, -0.8e10, 1.1e10, 0.7e10,
    ]

    result = build_money_flow_index({"market": synth_market})
    print(json.dumps(result, indent=2))

    # Sign sanity: a positive MMF WoW build-up must contribute negatively.
    mmf_comp = next(
        (c for c in result["headline"]["components"]
         if c["name"].startswith("Money-market")),
        None,
    )
    print("\nMMF-inversion check:",
          "PASS" if (mmf_comp and mmf_comp["contribution"] < 0) else "n/a/FAIL",
          f"(contribution={mmf_comp['contribution'] if mmf_comp else None})")
