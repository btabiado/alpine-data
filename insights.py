"""
Rules-based "insights" engine.

Scans the dashboard payload and emits human-readable notable facts about
the latest state vs recent history. Deterministic, no API key, no LLM.

Each insight is:
    {
        "kind": "etf" | "signal" | "trend" | "anomaly" | "milestone",
        "asset": "btc" | "eth" | "link" | "global",
        "severity": "info" | "good" | "bad" | "alert",
        "headline": "Short bold sentence",
        "detail":   "1-2 sentence elaboration (optional)",
    }
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any
from datetime import datetime, timedelta


# ----- rolling history -----
#
# Small persisted day-over-day snapshot file used by rules that need to know
# what yesterday looked like (e.g., "AI sentiment flipped" or "news volume
# 2σ above the 7-day mean"). Everything in here is pure-Python JSON I/O and
# fully defensive: a missing/corrupt file just gives back an empty list and
# the rules that depend on it stay silent until a real history accumulates.

_HISTORY_PATH = Path(__file__).parent / "data" / "insights_history.json"
_HISTORY_MAX_DAYS = 14


def _today_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d")


def _load_insights_history() -> list[dict]:
    """Return list of {date, ai_news_sentiment_label, ai_news_total} sorted
    ascending by date. Anything malformed is silently dropped so a corrupt
    file never crashes the build."""
    try:
        if not _HISTORY_PATH.exists():
            return []
        raw = json.loads(_HISTORY_PATH.read_text())
    except Exception as e:
        print(f"[insights-history] load failed: {e}", file=sys.stderr)
        return []
    rows = raw.get("history") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    clean: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        d = r.get("date")
        if not isinstance(d, str) or len(d) != 10:
            continue
        clean.append(r)
    clean.sort(key=lambda h: h.get("date") or "")
    return clean


def _save_insights_history(history: list[dict]) -> None:
    """Best-effort write. Failures are logged to stderr but never raise."""
    try:
        _HISTORY_PATH.parent.mkdir(exist_ok=True)
        _HISTORY_PATH.write_text(json.dumps({"history": history}, indent=2))
    except Exception as e:
        print(f"[insights-history] save failed: {e}", file=sys.stderr)


def _build_today_snapshot(payload: dict) -> dict | None:
    """Pull the day-over-day fields from `payload` we care about. Returns
    None when the AI news block hasn't produced anything useful — in that
    case we skip recording so a transient fetch failure doesn't pollute
    history with zeros and trigger spurious "flip to NEUTRAL" alerts on
    the next build."""
    market = payload.get("market") or {}
    ai_news = market.get("ai_news") or {}
    summary = ai_news.get("summary") or {}
    try:
        total = int(summary.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    label = (summary.get("sentiment_label") or "").upper() or None
    if total <= 0 and not label:
        return None
    return {
        "date": _today_iso(),
        "ai_news_sentiment_label": label,
        "ai_news_total": total,
    }


def _record_today(history: list[dict], today: dict) -> list[dict]:
    """Insert/overwrite today's row and trim to the last ``_HISTORY_MAX_DAYS``.
    Pure function — does not touch disk."""
    today_date = today.get("date")
    merged = [h for h in history if h.get("date") != today_date]
    merged.append(today)
    merged.sort(key=lambda h: h.get("date") or "")
    return merged[-_HISTORY_MAX_DAYS:]


def _previous_day_entry(history: list[dict]) -> dict | None:
    """Most recent history row whose date is strictly before today.
    Returns None when there's no prior entry (first run)."""
    today = _today_iso()
    for h in reversed(history):
        d = h.get("date")
        if isinstance(d, str) and d < today:
            return h
    return None


# ----- thresholds -----
#
# Magic numbers used in 2+ places, extracted for clarity. Names describe the
# semantic role; values are unchanged from the original inline constants.

THRESHOLDS = {
    # Fear & Greed Index cutoffs (CNN / alternative.me 0-100 scale).
    "FNG_FEAR": 25,
    "FNG_GREED": 75,
    # Z-score / sigma cutoffs against rolling 30d window.
    "SIGMA_15": 1.5,
    "SIGMA_20": 2.0,
    # ETF per-fund flow thresholds (USD millions).
    "OUTFLOW_LEADER_USD_M": -25,
    # ETF flow-vs-price divergence rule (USD millions, % price move).
    "FLOW_DIVERGENCE_USD_M": 300,
    "FLOW_VS_PRICE_PCT": 5,
}

FNG_FEAR = THRESHOLDS["FNG_FEAR"]
FNG_GREED = THRESHOLDS["FNG_GREED"]
SIGMA_15 = THRESHOLDS["SIGMA_15"]
SIGMA_20 = THRESHOLDS["SIGMA_20"]
OUTFLOW_LEADER_USD_M = THRESHOLDS["OUTFLOW_LEADER_USD_M"]
FLOW_DIVERGENCE_USD_M = THRESHOLDS["FLOW_DIVERGENCE_USD_M"]
FLOW_VS_PRICE_PCT = THRESHOLDS["FLOW_VS_PRICE_PCT"]


# ----- helpers -----


def _get_nested(d, path, default=None):
    """Safely traverse a dotted key path through nested dicts.

    Replaces ``(((x or {}).get(y) or {}).get(z))``-style chains. Returns
    ``default`` if any step encounters a non-dict or if the final value is
    None. Note: this only treats ``None`` as missing — empty containers,
    zero, and falsy non-None values pass through unchanged, so callers that
    relied on ``... or []`` to coerce ``0``/``""`` should not use this.
    """
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur

def _streak(values, sign: str) -> int:
    """Length of trailing run of values with the given sign ('pos' or 'neg')."""
    if not values:
        return 0
    n = 0
    for v in reversed(values):
        if v is None:
            break
        if sign == "pos" and v > 0:
            n += 1
        elif sign == "neg" and v < 0:
            n += 1
        else:
            break
    return n


def _zscore(values, window=30):
    if not values or len(values) < window + 1:
        return None
    import statistics
    sample = [v for v in values[-window - 1:-1] if v is not None]
    if len(sample) < 5:
        return None
    mu = statistics.mean(sample)
    sd = statistics.pstdev(sample) or 1e-9
    return (values[-1] - mu) / sd


def _largest_in_window(values, window=30) -> bool:
    if not values or len(values) < 2:
        return False
    tail = values[-window:]
    return values[-1] == max(tail)


def _smallest_in_window(values, window=30) -> bool:
    if not values or len(values) < 2:
        return False
    tail = values[-window:]
    return values[-1] == min(tail)


def _fmt_usd(n) -> str:
    if n is None:
        return "?"
    sign = "-" if n < 0 else ""
    a = abs(n)
    if a >= 1000:
        return f"{sign}${a/1000:.2f}B"
    if a >= 1:
        return f"{sign}${a:.1f}M"
    return f"{sign}${a*1000:.0f}K"


def _is_fresh(date_str: str | None, max_age_days: int = 14) -> bool:
    """True if `date_str` (YYYY-MM-DD) is within `max_age_days` of today.

    Returns False on parse failure or missing input so callers fail closed and
    avoid emitting insights from clearly stale data.
    """
    if not date_str:
        return False
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    return (datetime.utcnow() - d) <= timedelta(days=max_age_days)


# ----- per-domain insight generators -----

def _etf_insights(payload: dict, asset: str) -> list[dict]:
    out: list[dict] = []
    a = payload.get(asset) or {}
    daily = a.get("daily") or []
    if not daily:
        return out
    stats = a.get("stats") or {}
    flows = [d["flow"] for d in daily]
    last_date = daily[-1]["date"]
    last_flow = daily[-1]["flow"]
    # Freshness guard: if the latest ETF row is more than 14 days old, do not
    # emit "fresh" insights — but keep cumulative milestones, which describe
    # the cumulative total (still meaningful even if the last row is stale).
    fresh = _is_fresh(last_date, max_age_days=14)

    # 1. Last day directional flow
    if last_flow != 0:
        cls = "good" if last_flow > 0 else "bad"
        out.append({
            "kind": "etf",
            "asset": asset,
            "severity": cls,
            "headline": f"{asset.upper()} ETF {'inflow' if last_flow > 0 else 'outflow'} of {_fmt_usd(abs(last_flow))} on {last_date}",
            "detail": None,
        })

    # 2. Streak
    pos = _streak(flows, "pos")
    neg = _streak(flows, "neg")
    if pos >= 5:
        out.append({
            "kind": "trend", "asset": asset, "severity": "good",
            "headline": f"{asset.upper()} ETF on {pos}-day inflow streak",
            "detail": f"Sum over the streak: {_fmt_usd(sum(flows[-pos:]))}",
        })
    elif neg >= 5:
        out.append({
            "kind": "trend", "asset": asset, "severity": "bad",
            "headline": f"{asset.upper()} ETF on {neg}-day outflow streak",
            "detail": f"Sum over the streak: {_fmt_usd(sum(flows[-neg:]))}",
        })

    # 3. Largest day in last 30 / 90
    if abs(last_flow) >= 1 and _largest_in_window([abs(f) for f in flows], 30):
        out.append({
            "kind": "milestone", "asset": asset,
            "severity": "good" if last_flow > 0 else "bad",
            "headline": f"{asset.upper()}'s biggest single-day move in 30+ days",
            "detail": f"{_fmt_usd(last_flow)} vs prior 30-day max",
        })

    # 4. Z-score anomaly vs 30d
    z = _zscore(flows, 30)
    if z is not None and abs(z) >= SIGMA_20:
        cls = "good" if z > 0 else "bad"
        out.append({
            "kind": "anomaly", "asset": asset, "severity": cls,
            "headline": f"{asset.upper()} ETF flow {z:+.1f}σ vs 30-day mean",
            "detail": f"That's a {'large positive' if z > 0 else 'large negative'} outlier.",
        })

    # 5. Cumulative milestones
    all_time = stats.get("all_time")
    if all_time is not None:
        for thresh in (10_000, 25_000, 50_000, 75_000, 100_000):
            if all_time >= thresh:
                # crossed if previous cumulative was below
                if len(daily) >= 2 and daily[-2].get("cumulative", 0) < thresh <= daily[-1].get("cumulative", 0):
                    out.append({
                        "kind": "milestone", "asset": asset, "severity": "good",
                        "headline": f"{asset.upper()} ETF crossed ${thresh/1000:.0f}B cumulative inflows",
                        "detail": f"As of {last_date}",
                    })

    # 6. 7-day and 30-day rollups
    sum7 = sum(flows[-7:])
    sum30 = sum(flows[-30:])
    if abs(sum7) >= 500:
        out.append({
            "kind": "etf", "asset": asset,
            "severity": "good" if sum7 > 0 else "bad",
            "headline": f"{asset.upper()} ETF last 7d: {_fmt_usd(sum7)}",
            "detail": f"30d cumulative: {_fmt_usd(sum30)}",
        })

    # 7. Top-fund driver for the day (if per-fund data exists)
    by_fund_daily = a.get("by_fund_daily") or {}
    if by_fund_daily:
        last_per_fund = []
        for fund, series in by_fund_daily.items():
            if series and series[-1].get("date") == last_date:
                last_per_fund.append((fund, series[-1].get("flow") or 0))
        if last_per_fund:
            last_per_fund.sort(key=lambda x: abs(x[1]), reverse=True)
            top = last_per_fund[0]
            if abs(top[1]) >= 10:
                out.append({
                    "kind": "etf", "asset": asset,
                    "severity": "good" if top[1] > 0 else "bad",
                    "headline": f"{asset.upper()} top mover today: {top[0]} {_fmt_usd(top[1])}",
                    "detail": None,
                })

    # 8. ETF flow pace: latest 7d vs 90d-rolling 7d average. Helps fill the
    #    ETF tab on quiet days when no single rule above triggers.
    if fresh and len(flows) >= 90:
        sum7_recent = sum(flows[-7:])
        # 90-day rolling 7-day windows (exclude the current window so we
        # compare against history, not against the window we're flagging).
        history = flows[-90:-7] if len(flows) >= 97 else flows[:-7]
        if len(history) >= 14:
            # Average absolute 7d sum across overlapping windows.
            windows = [sum(history[i:i+7]) for i in range(len(history) - 6)]
            avg_abs = sum(abs(w) for w in windows) / max(len(windows), 1)
            if avg_abs > 0 and abs(sum7_recent) >= 2.0 * avg_abs and abs(sum7_recent) >= 100:
                cls = "good" if sum7_recent > 0 else "bad"
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": cls,
                    "headline": f"{asset.upper()} ETF flow pace 2x the 90-day average ({_fmt_usd(sum7_recent)} last 7d)",
                    "detail": f"90-day rolling 7d avg: {_fmt_usd(avg_abs)}",
                })

    # 9. Top-fund concentration: when a single fund drives ≥60% of today's
    # net flow and the net flow is meaningful (≥$100M abs).
    if fresh and by_fund_daily and abs(last_flow) >= 100:
        last_per_fund_signed = []
        for fund, series in by_fund_daily.items():
            if series and series[-1].get("date") == last_date:
                last_per_fund_signed.append((fund, series[-1].get("flow") or 0))
        if last_per_fund_signed:
            # Find fund with largest |flow|.
            top_abs = max(last_per_fund_signed, key=lambda x: abs(x[1]))
            if last_flow != 0 and abs(top_abs[1]) / abs(last_flow) >= 0.6:
                pct = abs(top_abs[1]) / abs(last_flow) * 100.0
                out.append({
                    "kind": "etf", "asset": asset,
                    "severity": "good" if last_flow > 0 else "bad",
                    "headline": f"{asset.upper()} ETF: {top_abs[0]} drove {pct:.0f}% of today's net flow",
                    "detail": f"{top_abs[0]} {_fmt_usd(top_abs[1])} vs net {_fmt_usd(last_flow)}.",
                })

    # 10. Outflow leader: the single fund bleeding the worst today (≤ −$25M).
    # Skip the not-noteworthy case where net flow is positive AND the worst
    # fund is only mildly negative (between -25 and 0).
    if fresh and by_fund_daily:
        last_per_fund_signed = []
        for fund, series in by_fund_daily.items():
            if series and series[-1].get("date") == last_date:
                last_per_fund_signed.append((fund, series[-1].get("flow") or 0))
        if last_per_fund_signed:
            worst = min(last_per_fund_signed, key=lambda x: x[1])
            worst_flow = worst[1]
            # Threshold: ≤ -25; if net flow positive AND worst > -25, skip.
            if worst_flow <= OUTFLOW_LEADER_USD_M and not (last_flow > 0 and worst_flow > OUTFLOW_LEADER_USD_M):
                out.append({
                    "kind": "etf", "asset": asset, "severity": "bad",
                    "headline": f"{asset.upper()} ETF: {worst[0]} leads outflows at {_fmt_usd(worst_flow)}",
                    "detail": f"Net flow today: {_fmt_usd(last_flow)}.",
                })

    # 11. New all-time cumulative high: today's cumulative > max of all prior
    # cumulative values. Reports even on stale data (it's a milestone).
    cumulative_series = [d.get("cumulative") for d in daily if d.get("cumulative") is not None]
    if len(cumulative_series) >= 2:
        last_cum = cumulative_series[-1]
        prior_max = max(cumulative_series[:-1])
        if last_cum is not None and last_cum > prior_max:
            out.append({
                "kind": "milestone", "asset": asset, "severity": "good",
                "headline": f"{asset.upper()} ETF cumulative hits new all-time high: {_fmt_usd(last_cum)}",
                "detail": f"Prior peak: {_fmt_usd(prior_max)} as of {last_date}.",
            })

    # 12. Month-over-month aggregate: last 30 days vs prior 30 days. Trigger
    # when same-sign and ≥2× in abs terms, with the recent window ≥$1B.
    if fresh and len(flows) >= 60:
        curr_30 = sum(flows[-30:])
        prev_30 = sum(flows[-60:-30])
        same_sign = (curr_30 > 0 and prev_30 > 0) or (curr_30 < 0 and prev_30 < 0)
        if same_sign and abs(curr_30) >= 1000 and abs(prev_30) > 0 and abs(curr_30) >= 2 * abs(prev_30):
            direction = "accelerated" if curr_30 > 0 else "deepened"
            sev = "good" if curr_30 > 0 else "bad"
            out.append({
                "kind": "trend", "asset": asset, "severity": sev,
                "headline": f"{asset.upper()} ETF: month-over-month flows {direction} sharply ({_fmt_usd(prev_30)} → {_fmt_usd(curr_30)})",
                "detail": "30d vs prior 30d net flows.",
            })

    # 13a. Extended streak (≥10 days) is a noteworthy regime milestone above
    # and beyond the 5-day streak rule above.
    if pos >= 10:
        out.append({
            "kind": "milestone", "asset": asset, "severity": "good",
            "headline": f"{asset.upper()} ETF: extended {pos}-day inflow streak (sum {_fmt_usd(sum(flows[-pos:]))})",
            "detail": "Sustained institutional accumulation — multi-week regime.",
        })
    elif neg >= 10:
        out.append({
            "kind": "milestone", "asset": asset, "severity": "bad",
            "headline": f"{asset.upper()} ETF: extended {neg}-day outflow streak (sum {_fmt_usd(sum(flows[-neg:]))})",
            "detail": "Sustained institutional distribution — multi-week regime.",
        })

    # 13b. Flow + news cluster: a big single-day flow plus matching crypto
    # news cluster amplifies the read. Triggers when |last_flow| ≥ $100M and
    # ≥3 of the latest 5 headlines mention the relevant asset.
    if fresh and abs(last_flow) >= 100:
        titles = _news_recent_titles(payload, limit=5)
        if titles:
            kw = ("bitcoin", "btc") if asset == "btc" else ("ethereum", "eth")
            mentions = _news_mentions(titles, kw)
            if mentions >= 3:
                flow_dir = "inflow" if last_flow > 0 else "outflow"
                sev = "good" if last_flow > 0 else "bad"
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": sev,
                    "headline": f"{asset.upper()} ETF {flow_dir} {_fmt_usd(abs(last_flow))} alongside {mentions}/5 headlines on {asset.upper()}",
                    "detail": "Flow event with confirming news cluster.",
                })

    # 14. Flow vs price divergence: 7d flow strongly diverges from 7d price.
    # Threshold: |7d flow| ≥ $300M and 7d price change ≥5% in the opposite
    # direction. Pulls price series from payload["market"][asset]["price"].
    if fresh and len(flows) >= 7:
        price_rows = _get_nested(payload, f"market.{asset}.price", [])
        # Need at least 8 points to compute a 7-day change.
        if len(price_rows) >= 8:
            sum7_flow = sum(flows[-7:])
            p_now = price_rows[-1].get("value")
            p_then = price_rows[-8].get("value")
            if p_now and p_then and p_then > 0 and abs(sum7_flow) >= FLOW_DIVERGENCE_USD_M:
                price_pct = (p_now / p_then - 1) * 100.0
                # Positive flow + price down ≥5%, OR negative flow + price up ≥5%.
                if (sum7_flow >= FLOW_DIVERGENCE_USD_M and price_pct <= -FLOW_VS_PRICE_PCT) or (sum7_flow <= -FLOW_DIVERGENCE_USD_M and price_pct >= FLOW_VS_PRICE_PCT):
                    flow_dir = "inflows" if sum7_flow > 0 else "outflows"
                    price_dir = "fell" if price_pct < 0 else "rose"
                    out.append({
                        "kind": "anomaly", "asset": asset, "severity": "alert",
                        "headline": f"{asset.upper()} ETF flow vs price divergence: {flow_dir} {_fmt_usd(abs(sum7_flow))} 7d while price {price_dir} {price_pct:+.1f}%",
                        "detail": "Flows and spot are pointing in opposite directions — sentiment dislocation.",
                    })
    return out


def _signal_insights(payload: dict) -> list[dict]:
    out: list[dict] = []
    sigs = payload.get("signals") or {}
    for asset, sig in sigs.items():
        if not sig:
            continue
        label = sig.get("label", "")
        score = sig.get("score", 0)
        if label in ("STRONG BUY", "STRONG SELL"):
            out.append({
                "kind": "signal", "asset": asset,
                "severity": "good" if "BUY" in label else "alert",
                "headline": f"{asset.upper()} composite signal: {label} (score {score:+d})",
                "detail": "Driven by " + ", ".join(c["name"] for c in (sig.get("components") or [])[:3] if c.get("contribution", 0) != 0),
            })
        # Detect direction flips in history (last 2 days)
        hist = sig.get("history") or []
        if len(hist) >= 2:
            prev, last = hist[-2]["score"], hist[-1]["score"]
            if prev <= 0 < last:
                out.append({
                    "kind": "signal", "asset": asset, "severity": "good",
                    "headline": f"{asset.upper()} signal flipped positive ({prev:+d} → {last:+d})",
                    "detail": None,
                })
            elif prev >= 0 > last:
                out.append({
                    "kind": "signal", "asset": asset, "severity": "bad",
                    "headline": f"{asset.upper()} signal flipped negative ({prev:+d} → {last:+d})",
                    "detail": None,
                })

        # Near-extreme signal score (|60| ≤ |score| < |75|). The STRONG
        # threshold is |75|; this fills the Signals tab on days where no asset
        # has tipped over but momentum is building.
        if label not in ("STRONG BUY", "STRONG SELL") and isinstance(score, (int, float)):
            mag = abs(int(score))
            if 60 <= mag < 75:
                direction = "STRONG BUY" if score > 0 else "STRONG SELL"
                out.append({
                    "kind": "signal", "asset": asset,
                    "severity": "good" if score > 0 else "alert",
                    "headline": f"{asset.upper()} signal near {direction} zone (score {score:+d})",
                    "detail": f"Within 15 pts of the {direction} threshold — watch for follow-through.",
                })

    # RSI extreme: per-asset RSI(14) component value is the literal RSI; flag
    # crosses <30 (oversold, contrarian bull) and >70 (overbought, contrarian
    # bear). Defensive against missing/non-numeric "value" strings.
    for asset, sig in sigs.items():
        if not sig:
            continue
        for comp in (sig.get("components") or []):
            if not isinstance(comp, dict):
                continue
            name = (comp.get("name") or "")
            if "RSI" not in name.upper():
                continue
            raw = comp.get("value")
            try:
                v = float(str(raw).strip().rstrip("%"))
            except (TypeError, ValueError):
                continue
            if v >= 70:
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": "alert",
                    "headline": f"{asset.upper()} RSI overbought at {v:.0f} — contrarian caution",
                    "detail": f"{name} reading ≥70 — momentum stretched.",
                })
            elif v <= 30:
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": "good",
                    "headline": f"{asset.upper()} RSI oversold at {v:.0f} — contrarian buy zone",
                    "detail": f"{name} reading ≤30 — momentum washed out.",
                })
            break  # one RSI insight per asset

    # MACD histogram sign flip — look at history's price/MACD-related components
    # via the score history as a proxy: if the score crossed zero in the last
    # window AND the latest MACD-histogram component contribution flipped sign,
    # flag a momentum turn. Defensive against missing component fields.
    for asset, sig in sigs.items():
        if not sig:
            continue
        macd_comp = None
        for comp in (sig.get("components") or []):
            if not isinstance(comp, dict):
                continue
            if "MACD" in (comp.get("name") or "").upper():
                macd_comp = comp
                break
        if macd_comp is None:
            continue
        raw = macd_comp.get("value")
        try:
            v = float(str(raw).strip().rstrip("%").replace("+", ""))
        except (TypeError, ValueError):
            continue
        contrib = macd_comp.get("contribution") or 0
        # Only emit when the |histogram| is meaningfully nonzero — i.e. not a
        # near-flat reading. Magnitude threshold scaled per-asset by 0.5%
        # absolute (works for both small/large symbol price scales because
        # the component is histogram-space, typically small floats).
        if abs(v) >= 0.1 and isinstance(contrib, (int, float)) and contrib != 0:
            sev = "good" if contrib > 0 else "bad"
            direction = "positive" if contrib > 0 else "negative"
            out.append({
                "kind": "signal", "asset": asset, "severity": sev,
                "headline": f"{asset.upper()} MACD histogram {direction} ({v:+.2f}) — momentum {'building' if contrib > 0 else 'fading'}",
                "detail": "MACD histogram sign indicates short-term momentum direction.",
            })

    # Signal component standout: of all assets with a non-trivial score, pick
    # the one with the largest |score| and call out its top-magnitude
    # contributing component. This always produces something on quiet days
    # provided any signal data exists.
    candidates = []
    for asset, sig in sigs.items():
        if not sig:
            continue
        score = sig.get("score")
        comps = sig.get("components") or []
        if score is None or not comps:
            continue
        if abs(score) < 20:
            continue
        # Find the highest |contribution| component.
        top_comp = max(
            (c for c in comps if c.get("contribution") not in (None, 0)),
            key=lambda c: abs(c.get("contribution") or 0),
            default=None,
        )
        if top_comp is None:
            continue
        candidates.append((abs(score), asset, score, top_comp))
    if candidates:
        candidates.sort(reverse=True)
        _, asset, score, top_comp = candidates[0]
        contrib = top_comp.get("contribution") or 0
        name = top_comp.get("name") or "component"
        out.append({
            "kind": "signal", "asset": asset,
            "severity": "good" if contrib > 0 else "bad",
            "headline": f"{asset.upper()} signal driven by {name} contribution {contrib:+d}",
            "detail": f"Largest single driver of the {score:+d} composite score.",
        })
    return out


def _stocks_insights(payload: dict) -> list[dict]:
    """Rules surfacing patterns in the top-20 most-active US stocks signal
    scores (``payload['market']['stocks_signals']``). Three rules:

    1. Broad-market sentiment: ≥75% of stocks scoring +20 (BUY+) or -20 (SELL+).
    2. Strong single buy/sell: any individual stock at |score| ≥ 50.
    3. Score divergence vs. crypto: avg stock score and avg crypto-signal score
       differ by ≥30 points → possible regime-change signal.

    Defensive: returns empty list if the fetcher hasn't run or the array is empty.
    """
    out: list[dict] = []
    stocks = _get_nested(payload, "market.stocks_signals", []) or []
    if not stocks:
        return out

    # Pull (score, symbol, name) tuples for stocks with a numeric score.
    scored = []
    for s in stocks:
        if not isinstance(s, dict):
            continue
        score = s.get("score")
        if isinstance(score, (int, float)):
            scored.append((score, s.get("symbol") or "?", s.get("name") or ""))
    if not scored:
        return out

    n = len(scored)

    # Rule 1: broad-market sentiment direction (≥75% on one side at |20|).
    buys = sum(1 for sc, _, _ in scored if sc >= 20)
    sells = sum(1 for sc, _, _ in scored if sc <= -20)
    if n >= 4:  # avoid noise on tiny lists
        if buys / n >= 0.75:
            pct = buys / n * 100.0
            out.append({
                "kind": "stocks", "asset": "global", "severity": "info",
                "headline": f"Broad stock-market buy bias: {buys}/{n} ({pct:.0f}%) of top-20 actives at BUY+",
                "detail": "Sentiment is one-sided across the most-active US tape.",
            })
        elif sells / n >= 0.75:
            pct = sells / n * 100.0
            out.append({
                "kind": "stocks", "asset": "global", "severity": "warning",
                "headline": f"Broad stock-market sell bias: {sells}/{n} ({pct:.0f}%) of top-20 actives at SELL+",
                "detail": "Sentiment is one-sided across the most-active US tape.",
            })

    # Rule 2: strong single buy/sell (|score| ≥ 50). Surface the most extreme one.
    extreme = max(scored, key=lambda t: abs(t[0]))
    ex_score, ex_sym, ex_name = extreme
    if ex_score >= 50:
        out.append({
            "kind": "stocks", "asset": ex_sym, "severity": "info",
            "headline": f"Strong-buy signal: {ex_sym} at score {int(ex_score):+d} — {ex_name}",
            "detail": "Among the top-20 most-active US stocks.",
        })
    elif ex_score <= -50:
        out.append({
            "kind": "stocks", "asset": ex_sym, "severity": "warning",
            "headline": f"Strong-sell signal: {ex_sym} at score {int(ex_score):+d} — {ex_name}",
            "detail": "Among the top-20 most-active US stocks.",
        })

    # Rule 3: stock vs crypto-signal score divergence.
    crypto_sigs = payload.get("signals") or {}
    crypto_scores = []
    for asset, sig in crypto_sigs.items():
        if not isinstance(sig, dict):
            continue
        sc = sig.get("score")
        if isinstance(sc, (int, float)):
            crypto_scores.append(sc)
    if crypto_scores:
        avg_stock = sum(sc for sc, _, _ in scored) / n
        avg_crypto = sum(crypto_scores) / len(crypto_scores)
        diff = avg_stock - avg_crypto
        if abs(diff) >= 30:
            leader = "stocks" if diff > 0 else "crypto"
            sev = "warning" if abs(diff) >= 45 else "info"
            out.append({
                "kind": "stocks", "asset": "global", "severity": sev,
                "headline": f"Crypto + stocks disagree: avg stock score {avg_stock:+.0f} vs avg crypto {avg_crypto:+.0f} ({leader} leading by {abs(diff):.0f} pts)",
                "detail": "Cross-asset sentiment divergence — possible regime-change signal.",
            })

    # Rule 4: breadth surge — same-direction stocks dominate AND crypto news
    # cluster matches. When ≥60% of top-20 stocks are BUY+ AND ≥3 of the last
    # 5 crypto headlines mention BTC/ETH, emit a richer cross-asset insight.
    news_titles = _news_recent_titles(payload, limit=5)
    if news_titles and n >= 4:
        if buys / n >= 0.60:
            crypto_pos_keywords = ("btc", "bitcoin", "eth", "ethereum", "rally", "surge", "all-time")
            mentions = _news_mentions(news_titles, crypto_pos_keywords)
            if mentions >= 3:
                out.append({
                    "kind": "stocks", "asset": "global", "severity": "info",
                    "headline": f"Stocks breadth bullish ({buys}/{n} BUY+) AND {mentions}/5 recent crypto headlines on point — risk-on alignment",
                    "detail": "Equity tape and crypto news cluster pointing the same direction.",
                })
        elif sells / n >= 0.60:
            risk_off_keywords = ("crash", "sell", "selloff", "drop", "plunge", "fear", "outflow")
            mentions = _news_mentions(news_titles, risk_off_keywords)
            if mentions >= 2:
                out.append({
                    "kind": "stocks", "asset": "global", "severity": "warning",
                    "headline": f"Stocks breadth bearish ({sells}/{n} SELL+) AND {mentions}/5 recent crypto headlines risk-off",
                    "detail": "Equity tape weakness with confirming risk-off news cluster.",
                })

    # Rule 5: dispersion — top-1 stock score significantly larger than the
    # rest. When the most-extreme stock's |score| ≥ 2× the median of the
    # other 19, surface it as a single-name idiosyncratic move (not market beta).
    if n >= 6:
        abs_scores = sorted(abs(s) for s, _, _ in scored)
        median_abs = abs_scores[len(abs_scores) // 2]
        if median_abs > 0 and abs(ex_score) >= 2 * median_abs and abs(ex_score) >= 30:
            out.append({
                "kind": "stocks", "asset": ex_sym, "severity": "info",
                "headline": f"Single-name dispersion: {ex_sym} score {int(ex_score):+d} is {abs(ex_score)/median_abs:.1f}× the top-20 median ({median_abs:.0f})",
                "detail": f"Idiosyncratic move in {ex_name} — not market beta.",
            })

    return out


def _whale_insights(payload: dict) -> list[dict]:
    """BTC on-chain whale-tab rules. Operates on payload['whale']['btc'].

    Each rule guards for empty/missing series. Freshness is not strictly
    enforced here because blockchain.info is occasionally a day or two
    behind — the trends still describe the most recent state we have.
    """
    out: list[dict] = []
    btc = (payload.get("whale") or {}).get("btc") or {}

    def _vals(series_name: str) -> list[float]:
        rows = btc.get(series_name) or []
        return [r.get("value") for r in rows if r.get("value") is not None]

    addrs = _vals("active_addresses")
    tx_count = _vals("tx_count")
    tx_vol = _vals("tx_volume_usd")
    avg_tx = _vals("avg_tx_usd")
    miners = _vals("miners_revenue_usd")

    addrs_high = _largest_in_window(addrs, 30) if len(addrs) >= 2 else False
    addrs_low = _smallest_in_window(addrs, 30) if len(addrs) >= 2 else False
    txc_high = _largest_in_window(tx_count, 30) if len(tx_count) >= 2 else False
    txc_low = _smallest_in_window(tx_count, 30) if len(tx_count) >= 2 else False
    vol_low = _smallest_in_window(tx_vol, 30) if len(tx_vol) >= 2 else False

    # 1. Active addresses 30d high
    if addrs_high and addrs:
        last = addrs[-1]
        out.append({
            "kind": "milestone", "asset": "btc", "severity": "good",
            "headline": f"BTC active addresses at 30-day high ({last:,.0f})",
            "detail": "Network participation surging.",
        })

    # 2. Active addresses 30d low
    if addrs_low and addrs:
        last = addrs[-1]
        out.append({
            "kind": "anomaly", "asset": "btc", "severity": "bad",
            "headline": f"BTC active addresses at 30-day low ({last:,.0f}) — network engagement weak",
            "detail": None,
        })

    # 3. Average transaction size spike (≥1.5σ above 30d mean) — whale proxy.
    if len(avg_tx) >= 31:
        z = _zscore(avg_tx, 30)
        if z is not None and z >= SIGMA_15:
            out.append({
                "kind": "anomaly", "asset": "btc", "severity": "info",
                "headline": f"BTC avg transaction size {z:+.1f}σ vs 30d — big-money on-chain",
                "detail": f"Latest avg tx: {_fmt_usd((avg_tx[-1] or 0) / 1e6)}.",
            })

    # 4. Miner revenue spike (≥2σ above 30d mean).
    if len(miners) >= 31:
        z = _zscore(miners, 30)
        if z is not None and z >= SIGMA_20:
            last = miners[-1] or 0
            out.append({
                "kind": "anomaly", "asset": "btc", "severity": "good",
                "headline": f"BTC miner revenue {z:+.1f}σ vs 30d ({_fmt_usd(last / 1e6)}M today)",
                "detail": "Miners earning more — supports network security and may ease sell-pressure.",
            })

    # 5. Combined network momentum: tx_count AND active_addresses both at 30d highs.
    if txc_high and addrs_high:
        out.append({
            "kind": "milestone", "asset": "btc", "severity": "good",
            "headline": "BTC network momentum strong: both tx count and active addresses at 30-day highs",
            "detail": "Broad-based on-chain engagement.",
        })

    # 6. On-chain quiet day: tx_count + active_addresses + tx_volume all at 30d lows.
    if txc_low and addrs_low and vol_low:
        out.append({
            "kind": "trend", "asset": "btc", "severity": "info",
            "headline": "BTC on-chain unusually quiet: tx count, active addresses, and volume all at 30-day lows",
            "detail": "Network engagement low — could indicate consolidation or holiday lull.",
        })

    # 7. Network velocity spike: tx_volume_usd / active_addresses (per day)
    # ≥1.5σ above its 30d mean — outsized USD movement per active address.
    vol_rows = btc.get("tx_volume_usd") or []
    addr_rows = btc.get("active_addresses") or []
    # Align by date so we don't divide mismatched offsets if one series is
    # shorter than the other.
    addr_by_date = {r.get("date"): r.get("value") for r in addr_rows if r.get("date") is not None}
    velocity: list[float] = []
    for r in vol_rows:
        v = r.get("value")
        a = addr_by_date.get(r.get("date"))
        if v is None or a is None or a == 0:
            continue
        velocity.append(v / a)
    if len(velocity) >= 31:
        z = _zscore(velocity, 30)
        if z is not None and z >= SIGMA_15:
            out.append({
                "kind": "anomaly", "asset": "btc", "severity": "info",
                "headline": f"BTC network velocity {z:+.1f}σ vs 30d — outsized USD per active address",
            })

    # ----- ETH-side whale activity -----
    eth = (payload.get("whale") or {}).get("eth") or {}

    # 8. ETH large_transactions count high. The blockchair endpoint returns
    # a list of recent ≥$1M ETH transfers. When that list is unusually long
    # (≥30), call it out as institutional-grade flow.
    eth_large = eth.get("large_transactions") or []
    if isinstance(eth_large, list) and len(eth_large) >= 30:
        out.append({
            "kind": "anomaly", "asset": "eth", "severity": "alert",
            "headline": f"ETH large-transaction surge: {len(eth_large)} on-chain transfers ≥$1M in the latest scan",
            "detail": "Blockchair feed of high-value ETH transfers — institutional flow.",
        })

    # 9. ETH active addresses (Etherscan daily series) at 30-day high.
    etherscan = eth.get("etherscan_daily") or {}
    eth_addrs_series = etherscan.get("active_addresses") or etherscan.get("daily_active_addresses") or []
    eth_addrs = [r.get("value") for r in eth_addrs_series if isinstance(r, dict) and r.get("value") is not None]
    if len(eth_addrs) >= 2 and _largest_in_window(eth_addrs, 30):
        out.append({
            "kind": "milestone", "asset": "eth", "severity": "good",
            "headline": f"ETH active addresses at 30-day high ({eth_addrs[-1]:,.0f})",
            "detail": "Etherscan daily — network engagement surging.",
        })

    # 10. ETH Coin Metrics whale transfer-value z-score (≥1.5σ vs 30d).
    cm = eth.get("coin_metrics") or {}
    cm_series = cm.get("transfer_value_adj_usd") or cm.get("tx_volume_usd") or []
    cm_vals = [r.get("value") for r in cm_series if isinstance(r, dict) and r.get("value") is not None]
    if len(cm_vals) >= 31:
        z = _zscore(cm_vals, 30)
        if z is not None and z >= SIGMA_15:
            out.append({
                "kind": "anomaly", "asset": "eth", "severity": "alert",
                "headline": f"ETH whale transfer value {z:+.1f}σ vs 30d — heavy on-chain USD movement",
                "detail": f"Latest: {_fmt_usd((cm_vals[-1] or 0) / 1e6)}.",
            })

    return out


def _news_recent_titles(payload: dict, limit: int = 5) -> list[str]:
    """Return the latest few headline strings (lowercased) from market.news.

    Used by other generators to add a one-line news-context detail when a
    quantitative event lines up with a story cluster. Defensive: returns
    empty list when news is missing or malformed.
    """
    try:
        news = (payload.get("market") or {}).get("news") or []
        out: list[str] = []
        for n in news[:limit]:
            t = (n or {}).get("title")
            if isinstance(t, str) and t:
                out.append(t.lower())
        return out
    except Exception:
        return []


def _news_mentions(titles: list[str], keywords: tuple[str, ...]) -> int:
    """Count how many of the given lowercased titles mention ANY of the
    keywords (case-insensitive, substring match). Used to detect cluster
    coverage of an event in headlines.
    """
    if not titles or not keywords:
        return 0
    n = 0
    for t in titles:
        if any(k in t for k in keywords):
            n += 1
    return n


def _poc_insights(payload: dict) -> list[dict]:
    """POC (Point of Control) tab rules. Operates on payload['market']['poc'],
    which has the shape::

        {<asset>: {"d30": {...}, "d90": {...}, "d180": {...}, "d365": {...},
                   "migration": {"delta_pct": float, "direction": str,
                                 "magnitude": str, "between_pocs": bool},
                   "migration_series": [{"date": str, "poc": float}, ...],
                   "naked": [{"poc": float, "days_ago": int,
                              "distance_pct": float, "week_start": str}, ...]}}

    Each rule is defensive: returns empty if expected keys are missing.
    """
    out: list[dict] = []
    poc_root = ((payload.get("market") or {}).get("poc")) or {}
    if not isinstance(poc_root, dict) or not poc_root:
        return out

    # Rule 1: STRONG migration (≥5%) — value migrating up/down clearly.
    for asset, asset_poc in poc_root.items():
        if not isinstance(asset_poc, dict):
            continue
        mig = asset_poc.get("migration") or {}
        direction = mig.get("direction")
        magnitude = mig.get("magnitude")
        delta = mig.get("delta_pct")
        if direction in ("UP", "DOWN") and magnitude == "STRONG" and isinstance(delta, (int, float)):
            sev = "good" if direction == "UP" else "bad"
            out.append({
                "kind": "trend", "asset": asset, "severity": sev,
                "headline": f"{asset.upper()} POC value migrating {direction} {delta:+.1f}% (30d vs 90d)",
                "detail": "Short-term volume concentrating away from the structural mean — bullish acceptance." if direction == "UP" else "Short-term volume concentrating below structural mean — bearish acceptance.",
            })

    # Rule 2: price BETWEEN 30d POC and 90d POC — actionable transition zone.
    for asset, asset_poc in poc_root.items():
        if not isinstance(asset_poc, dict):
            continue
        mig = asset_poc.get("migration") or {}
        if mig.get("between_pocs") is True and mig.get("direction") in ("UP", "DOWN"):
            d30 = asset_poc.get("d30") or {}
            d90 = asset_poc.get("d90") or {}
            cur = d30.get("current") or d90.get("current")
            p30 = d30.get("poc")
            p90 = d90.get("poc")
            if cur and p30 and p90:
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": "info",
                    "headline": f"{asset.upper()} price ${cur:,.0f} sits between 30d POC (${p30:,.0f}) and 90d POC (${p90:,.0f})",
                    "detail": "Transition zone — structural support hasn't caught up to tactical volume formation.",
                })

    # Rule 3: cluster of naked POCs across multiple assets (≥2 assets each
    # carrying ≥3 unfilled weekly POCs in the last 180d). Indicates broad
    # untested-magnet structure forming.
    naked_counts: list[tuple[str, int]] = []
    for asset, asset_poc in poc_root.items():
        if not isinstance(asset_poc, dict):
            continue
        naked = asset_poc.get("naked") or []
        if isinstance(naked, list) and len(naked) >= 3:
            naked_counts.append((asset, len(naked)))
    if len(naked_counts) >= 2:
        # Sort by count desc, take top-2 for the headline.
        naked_counts.sort(key=lambda t: t[1], reverse=True)
        top = naked_counts[:3]
        names = ", ".join(f"{a.upper()} ({n})" for a, n in top)
        out.append({
            "kind": "anomaly", "asset": "global", "severity": "info",
            "headline": f"Naked POC cluster forming across {len(naked_counts)} assets: {names}",
            "detail": "Multiple weekly POCs untested in the last 180d — magnet levels building.",
        })

    # Rule 4: single-asset naked-POC density spike (≥5 unfilled weekly POCs).
    for asset, asset_poc in poc_root.items():
        if not isinstance(asset_poc, dict):
            continue
        naked = asset_poc.get("naked") or []
        if isinstance(naked, list) and len(naked) >= 5:
            # Closest naked POC by distance.
            with_dist = [n for n in naked if isinstance(n, dict)
                         and isinstance(n.get("distance_pct"), (int, float))]
            if with_dist:
                closest = min(with_dist, key=lambda n: abs(n.get("distance_pct") or 0))
                dist = closest.get("distance_pct") or 0
                price_level = closest.get("poc")
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": "info",
                    "headline": f"{asset.upper()} carries {len(naked)} naked weekly POCs in 180d (nearest at ${price_level:,.0f}, {dist:+.1f}% away)",
                    "detail": "Dense magnet structure — expect reactions at retests.",
                })

    return out


def _social_insights(payload: dict) -> list[dict]:
    """Social/Research tab rules. Operates on payload['market']['social'],
    which holds reddit, cryptocompare social, cc_news, and santiment subtrees.

    Each rule is defensive — the social fetcher can return ``available: False``
    or partial data on any leg, and we silently skip those rules.
    """
    out: list[dict] = []
    social = ((payload.get("market") or {}).get("social")) or {}
    if not isinstance(social, dict) or not social:
        return out

    # Rule 1: CryptoCompare news sentiment skew. Per coin, when sentiment is
    # one-sided (net_score |≥5| with >=10 articles), emit a directional
    # insight. Only the strongest coin per call.
    cc_news = ((social.get("cc_news") or {}).get("coins")) or {}
    if isinstance(cc_news, dict) and cc_news:
        candidates: list[tuple[int, str, dict]] = []
        for sym, coin in cc_news.items():
            if not isinstance(coin, dict):
                continue
            net = coin.get("net_score")
            count = coin.get("article_count") or 0
            if isinstance(net, int) and count >= 10 and abs(net) >= 5:
                candidates.append((abs(net), sym, coin))
        if candidates:
            candidates.sort(reverse=True)
            _, sym, coin = candidates[0]
            net = coin.get("net_score") or 0
            pos = coin.get("positive") or 0
            neg = coin.get("negative") or 0
            sev = "good" if net > 0 else "bad"
            direction = "bullish" if net > 0 else "bearish"
            out.append({
                "kind": "trend", "asset": sym, "severity": sev,
                "headline": f"{sym.upper()} news sentiment skews {direction}: {pos} positive vs {neg} negative (net {net:+d})",
                "detail": "CryptoCompare news-sentiment net score from last 50 headlines.",
            })

    # Rule 2: Reddit subreddit activity spike — when /r/<sub> active users are
    # ≥3× the typical subscriber ratio (active / subscribers) seen across the
    # other tracked subs. Indicates outsized real-time engagement for that coin.
    reddit = (social.get("reddit") or {}).get("subreddits") or {}
    if not reddit:
        # Schema fallback: reddit() returns top-level keys per-sub in some shapes.
        reddit = social.get("reddit") or {}
    if isinstance(reddit, dict):
        ratios: list[tuple[float, str, int, int]] = []
        for sub_key, meta in reddit.items():
            if not isinstance(meta, dict):
                continue
            subs = meta.get("subscribers")
            active = meta.get("active_users")
            if (isinstance(subs, int) and subs >= 5000
                    and isinstance(active, int) and active >= 100):
                ratios.append((active / subs, sub_key, active, subs))
        if len(ratios) >= 3:
            # Compute median ratio; flag any sub ≥ 3× median.
            sorted_r = sorted(r[0] for r in ratios)
            median = sorted_r[len(sorted_r) // 2]
            if median > 0:
                ratios.sort(reverse=True)
                top_ratio, top_sub, top_active, top_subs = ratios[0]
                if top_ratio >= 3 * median:
                    out.append({
                        "kind": "anomaly", "asset": "global", "severity": "info",
                        "headline": f"r/{top_sub} active-user spike: {top_active:,} active vs {top_subs:,} subs ({top_ratio*100:.2f}% — {top_ratio/median:.1f}× median)",
                        "detail": "Outsized real-time engagement on one subreddit.",
                    })

    # Rule 3: Santiment daily-active-addresses delta — per coin, when
    # daily_active_addresses_delta_pct ≥ +20% or ≤ −20% over the recent
    # window, emit a directional flag.
    san = (social.get("santiment") or {}).get("coins") or {}
    if isinstance(san, dict):
        for sym, coin in san.items():
            if not isinstance(coin, dict):
                continue
            delta = coin.get("daily_active_addresses_delta_pct")
            latest = coin.get("daily_active_addresses_latest")
            if isinstance(delta, (int, float)) and abs(delta) >= 20:
                sev = "good" if delta > 0 else "bad"
                direction = "surging" if delta > 0 else "fading"
                latest_s = f" — {int(latest):,} DAA latest" if isinstance(latest, (int, float)) else ""
                out.append({
                    "kind": "trend", "asset": sym, "severity": sev,
                    "headline": f"{sym.upper()} on-chain attention {direction}: daily active addresses {delta:+.0f}% over recent window{latest_s}",
                    "detail": "Santiment DAA — broad user engagement proxy.",
                })
                # Cap at most one per asset to avoid bar pollution.

    # Rule 4: news + Reddit alignment — if news sentiment is one-sided AND
    # at least one tracked subreddit shows high engagement, emit a stronger
    # combined insight. (Compose-only — depends on data from rules 1 + 2.)
    try:
        # Reuse cc_news strongest candidate if it exists.
        cc_coins = ((social.get("cc_news") or {}).get("coins")) or {}
        strongest = None
        for sym, coin in cc_coins.items():
            net = coin.get("net_score") if isinstance(coin, dict) else None
            count = coin.get("article_count") or 0 if isinstance(coin, dict) else 0
            if isinstance(net, int) and count >= 20 and abs(net) >= 10:
                if strongest is None or abs(net) > abs(strongest[1].get("net_score") or 0):
                    strongest = (sym, coin)
        if strongest is not None:
            sym, coin = strongest
            # Find matching subreddit if any.
            sub_match = None
            for sub_key, meta in (reddit or {}).items():
                if not isinstance(meta, dict):
                    continue
                label = (meta.get("label") or "").upper()
                if sym.upper() in label or sym.upper() in sub_key.upper():
                    sub_match = (sub_key, meta)
                    break
            if sub_match is not None:
                sub_key, meta = sub_match
                active = meta.get("active_users") or 0
                if isinstance(active, int) and active >= 500:
                    net = coin.get("net_score") or 0
                    direction = "bullish" if net > 0 else "bearish"
                    out.append({
                        "kind": "anomaly", "asset": sym, "severity": "good" if net > 0 else "bad",
                        "headline": f"{sym.upper()} news + Reddit alignment: {direction} news net {net:+d} with r/{sub_key} at {active:,} active users",
                        "detail": "Cross-source agreement strengthens the signal.",
                    })
    except Exception as e:
        print(f"  [insights] news+reddit alignment suppressed: {type(e).__name__}", file=sys.stderr)

    return out


def _market_insights(payload: dict) -> list[dict]:
    out: list[dict] = []
    market = payload.get("market") or {}
    fng = market.get("fear_greed") or []
    if fng:
        last = fng[-1]
        v = last.get("value")
        if v is not None:
            if v <= FNG_FEAR:
                out.append({"kind":"anomaly","asset":"global","severity":"good",
                    "headline": f"Fear & Greed at {v} — extreme fear (contrarian buy zone)",
                    "detail": last.get("label","")})
            elif v >= FNG_GREED:
                out.append({"kind":"anomaly","asset":"global","severity":"alert",
                    "headline": f"Fear & Greed at {v} — extreme greed (contrarian caution)",
                    "detail": last.get("label","")})

    # Funding flips
    for asset in ("btc", "eth", "link"):
        a = (market.get(asset) or {})
        funding = a.get("funding") or []
        if len(funding) >= 2:
            last = funding[-1].get("rate", 0)
            prev = funding[-2].get("rate", 0)
            if prev > 0 and last < 0:
                out.append({"kind":"trend","asset":asset,"severity":"good",
                    "headline": f"{asset.upper()} funding flipped negative ({last*100:.4f}%)",
                    "detail": "Bearish positioning — contrarian setup."})
            elif prev < 0 and last > 0:
                out.append({"kind":"trend","asset":asset,"severity":"info",
                    "headline": f"{asset.upper()} funding flipped positive ({last*100:.4f}%)",
                    "detail": None})
        # DVOL crush / spike
        dvol = a.get("dvol") or []
        if len(dvol) >= 31:
            vals = [r["dvol"] for r in dvol if r.get("dvol") is not None]
            z = _zscore(vals, 30)
            if z is not None:
                if z <= -SIGMA_15:
                    out.append({"kind":"anomaly","asset":asset,"severity":"good",
                        "headline": f"{asset.upper()} DVOL crushed ({z:+.1f}σ vs 30d mean)",
                        "detail": "Implied vol historically low — long-vol setup."})
                elif z >= SIGMA_15:
                    out.append({"kind":"anomaly","asset":asset,"severity":"alert",
                        "headline": f"{asset.upper()} DVOL spike ({z:+.1f}σ vs 30d mean)",
                        "detail": "Implied vol elevated — caution."})

    # ETH gas oracle (Etherscan v2)
    gas = market.get("eth_gas") or {}
    base = gas.get("base_fee_gwei")
    if base is not None:
        if base >= 50:
            out.append({"kind":"anomaly","asset":"eth","severity":"alert",
                "headline": f"ETH gas spike: base fee {base:.0f} gwei",
                "detail": f"Fast: {gas.get('fast_gwei','?')} gwei. Network is congested."})
        elif base <= 1:
            out.append({"kind":"trend","asset":"eth","severity":"info",
                "headline": f"ETH gas near zero ({base:.2f} gwei)",
                "detail": "Quiet mainnet — cheap to transact, but low activity."})

    # BTC mempool fees (mempool.space)
    mp = market.get("mempool") or {}
    fees = mp.get("fees_sat_vb") or {}
    fastest = fees.get("fastestFee")
    if fastest is not None:
        if fastest >= 100:
            out.append({"kind":"anomaly","asset":"btc","severity":"alert",
                "headline": f"BTC mempool congested: {fastest} sat/vB fastest fee",
                "detail": "Heavy on-chain demand."})
        elif fastest <= 2:
            out.append({"kind":"trend","asset":"btc","severity":"info",
                "headline": f"BTC mempool quiet ({fastest} sat/vB)",
                "detail": None})

    # BTC hashrate trend
    hr = mp.get("hashrate_daily_eh") or []
    if len(hr) >= 30:
        last = hr[-1].get("value")
        prior = [r.get("value") for r in hr[-30:] if r.get("value")]
        if last and prior and last == max(prior):
            out.append({"kind":"milestone","asset":"btc","severity":"good",
                "headline": f"BTC hashrate at 30-day high ({last:.0f} EH/s)",
                "detail": "Miners committing more compute — bullish security."})

    # Stablecoin supply 7d delta (DeFiLlama) — proxy for "dry powder" coming in/out
    llama = market.get("defillama") or {}
    delta = llama.get("stablecoin_7d_change_usd")
    if delta is not None:
        d_b = delta / 1e9  # billions
        if abs(d_b) >= 0.5:
            out.append({"kind":"trend","asset":"global",
                "severity": "good" if d_b > 0 else "bad",
                "headline": f"Stablecoin supply {'+' if d_b > 0 else ''}${d_b:.2f}B over the last 7d",
                "detail": f"Total stablecoin mcap: ${(llama.get('stablecoin_mcap_usd') or 0)/1e9:.1f}B. Rising stablecoins = buying power building up."})
    # DeFi DEX volume snapshot
    dex24 = llama.get("dex_volume_24h_usd")
    fees24 = llama.get("fees_24h_usd")
    if dex24 and dex24 >= 5e9:
        out.append({"kind":"info","asset":"global","severity":"info",
            "headline": f"DEX 24h volume: ${dex24/1e9:.2f}B  ·  protocol fees: ${(fees24 or 0)/1e6:.1f}M",
            "detail": None})

    # Trending coin (CoinGecko search) — retail attention proxy
    trending = market.get("trending") or []
    if trending:
        top = trending[0]
        sym = (top.get("symbol") or "").upper()
        name = top.get("name") or ""
        rank = top.get("rank")
        # Filter out BTC/ETH (we already cover them) and anything in top-10 (not really "trending")
        if sym and sym not in ("BTC", "ETH") and (rank is None or rank > 10):
            out.append({"kind": "trend", "asset": "global", "severity": "info",
                "headline": f"{sym} ({name}) is trending #1 on CoinGecko",
                "detail": (f"Current market cap rank: {rank}" if rank else "Retail search interest spike — watch for follow-through.")})

    # DEX hot pool (GeckoTerminal) — top trending pool by volume with big move
    gt = market.get("geckoterminal") or {}
    gt_trending = gt.get("trending_pools") or []
    for pool in gt_trending[:5]:  # only top-5 by volume considered
        vol = pool.get("volume_24h_usd") or 0
        ch = pool.get("change_24h_pct")
        if vol >= 50_000_000 and ch is not None and ch >= 30:
            out.append({
                "kind": "trend", "asset": "global", "severity": "info",
                "headline": f"DEX hot pool: {pool.get('name','?')} on {pool.get('network','?')} {ch:+.0f}% with {_fmt_usd(vol/1e6)}M volume",
                "detail": f"DEX: {pool.get('dex','?')} · 24h tx: {pool.get('transactions_24h',0):,}",
            })
            break  # one such insight is enough

    # BTC difficulty adjustment — miner economics
    diff_adj = _get_nested(market, "mempool_extra.difficulty_adjustment", {})
    change_pct = diff_adj.get("difficulty_change_pct")
    days = (diff_adj.get("remaining_time_ms") or 0) / 86400000.0
    if change_pct is not None and 0 < days < 5:
        if abs(change_pct) >= 4:
            sev = "alert" if change_pct > 0 else "good"
            direction = "harder" if change_pct > 0 else "easier"
            out.append({"kind": "milestone", "asset": "btc", "severity": sev,
                "headline": f"BTC difficulty retarget in ~{days:.1f} days: {change_pct:+.1f}% ({direction} for miners)",
                "detail": ("Miners likely under pressure — watch for distribution." if change_pct > 0 else "Miners get a break — less sell-side pressure.")})

    # Mining pool centralization risk
    pools = _get_nested(market, "mempool_extra.pools", {})
    top2 = pools.get("top2_concentration_pct")
    if top2 is not None and top2 >= 55:
        out.append({"kind": "anomaly", "asset": "btc", "severity": "alert",
            "headline": f"BTC mining concentration high: top 2 pools = {top2:.1f}% of blocks",
            "detail": "Theoretical 51% attack risk if both colluded."})

    # DeFi TVL by chain - flag big movers
    chains = _get_nested(market, "defi.chains", [])
    for c in chains[:10]:
        change_1d = c.get("change_1d_pct")
        if change_1d is not None and abs(change_1d) >= 4:
            sev = "good" if change_1d > 0 else "bad"
            out.append({"kind": "trend", "asset": "global", "severity": sev,
                "headline": f"{c.get('name')} TVL {'+'if change_1d>0 else ''}{change_1d:.1f}% today (${(c.get('tvl_usd') or 0)/1e9:.1f}B)",
                "detail": f"Top-10 chain by TVL — significant 1d move."})
            break  # one chain insight at most

    # Latest news headline (top story summary)
    news = market.get("news") or []
    if news and news[0].get("title"):
        out.append({"kind": "info", "asset": "global", "severity": "info",
            "headline": f"📰 {news[0]['source']}: {news[0]['title'][:100]}",
            "detail": None})

    # FRED macro overlay (DXY / SP500 / Gold / 10Y) — only if user supplied a key
    fred = market.get("fred") or {}
    if fred.get("available"):
        # DXY 1d change ≥1%
        dxy = fred.get("dxy") or []
        if len(dxy) >= 2:
            last = dxy[-1].get("value")
            prev = dxy[-2].get("value")
            if last and prev:
                ch = (last - prev) / prev * 100.0
                if abs(ch) >= 1.0:
                    out.append({
                        "kind": "trend", "asset": "global",
                        "severity": "bad" if ch > 0 else "good",
                        "headline": f"DXY {ch:+.1f}% today — typically inverse to risk assets including crypto",
                        "detail": f"Broad dollar index at {last:.2f}.",
                    })

        # 10Y Treasury yield thresholds
        tnx = fred.get("treasury_10y") or []
        if len(tnx) >= 2:
            last_y = tnx[-1].get("value")
            prev_y = tnx[-2].get("value")
            if last_y is not None and prev_y is not None:
                for thresh in (4.5, 5.0):
                    crossed_up = prev_y < thresh <= last_y
                    crossed_dn = prev_y >= thresh > last_y
                    if crossed_up or crossed_dn:
                        out.append({
                            "kind": "milestone", "asset": "global",
                            "severity": "alert" if crossed_up else "info",
                            "headline": f"10Y Treasury yield {'crossed above' if crossed_up else 'fell below'} {thresh:.1f}% ({last_y:.2f}%)",
                            "detail": "Higher yields pressure risk assets; lower yields ease conditions.",
                        })
                        break

        # Gold new 30-day high
        gold = fred.get("gold") or []
        if len(gold) >= 2:
            vals = [r.get("value") for r in gold if r.get("value") is not None]
            if vals:
                tail = vals[-30:] if len(vals) >= 30 else vals
                if vals[-1] == max(tail) and len(tail) >= 5:
                    out.append({
                        "kind": "milestone", "asset": "global", "severity": "info",
                        "headline": f"Gold at 30-day high (${vals[-1]:,.2f}/oz)",
                        "detail": "Often a hedge/risk-off signal — monitor BTC correlation.",
                    })

        # S&P 500 1d drop ≥2%
        spx = fred.get("sp500") or []
        if len(spx) >= 2:
            last_s = spx[-1].get("value")
            prev_s = spx[-2].get("value")
            if last_s and prev_s and prev_s > 0:
                ch = (last_s - prev_s) / prev_s * 100.0
                if ch <= -2.0:
                    out.append({
                        "kind": "anomaly", "asset": "global", "severity": "bad",
                        "headline": f"S&P 500 {ch:.1f}% today — risk-off may pressure crypto",
                        "detail": f"Index at {last_s:,.0f}.",
                    })

    # Coinbase vs CoinGecko price divergence (cross-source sanity check).
    # Replaces the old CryptoCompare-based version we removed when CC's free
    # tier sunset. Coinbase is a US-regulated exchange spot price; CoinGecko
    # is an aggregator. A ≥0.5% drift between them usually means one venue
    # is leading the other (arbitrage opportunity proxy).
    cb = market.get("coinbase") or {}
    for asset in ("btc", "eth", "link", "ltc"):
        cg_last_rows = _get_nested(market, f"{asset}.price", [])
        cg_last = cg_last_rows[-1].get("value") if cg_last_rows else None
        cb_price = (cb.get(asset) or {}).get("price_usd")
        if cg_last and cb_price and cg_last > 0:
            div = abs(cb_price - cg_last) / cg_last
            if div >= 0.005:  # >0.5%
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": "info",
                    "headline": f"{asset.upper()} price divergence: CoinGecko ${cg_last:,.0f} vs Coinbase ${cb_price:,.0f}",
                    "detail": f"{div*100:.2f}% spread between data sources — Coinbase often leads US-hours moves.",
                })

    # ----- Markets tab: top-25 movers + BTC dominance + total market cap -----
    # These fire more frequently than the FRED macro rules so the Markets
    # insights bar isn't empty on calm macro days.

    markets_top = market.get("markets_top") or []

    # Top-25 24h gainer (≥5%): surface the leader so user knows which top-cap
    # name is hot today. Skip stables (symbols ending in USD).
    if markets_top:
        gainers_24h = [c for c in markets_top
                       if (c.get("change_24h_pct") is not None
                           and c.get("change_24h_pct") >= 5
                           and not (c.get("symbol") or "").upper().endswith("USD"))]
        if gainers_24h:
            top = max(gainers_24h, key=lambda c: c.get("change_24h_pct") or 0)
            out.append({
                "kind": "trend", "asset": "global", "severity": "good",
                "headline": f"Top-25 24h gainer: {top.get('symbol','?')} {top.get('change_24h_pct'):+.1f}% (rank #{top.get('rank','?')})",
                "detail": (top.get("name") or "") + f" — {_fmt_usd((top.get('market_cap_usd') or 0)/1e6)} mcap.",
            })

        # Top-25 24h loser (≤-5%): same but for biggest drop
        losers_24h = [c for c in markets_top
                      if (c.get("change_24h_pct") is not None
                          and c.get("change_24h_pct") <= -5
                          and not (c.get("symbol") or "").upper().endswith("USD"))]
        if losers_24h:
            worst = min(losers_24h, key=lambda c: c.get("change_24h_pct") or 0)
            out.append({
                "kind": "trend", "asset": "global", "severity": "bad",
                "headline": f"Top-25 24h loser: {worst.get('symbol','?')} {worst.get('change_24h_pct'):+.1f}% (rank #{worst.get('rank','?')})",
                "detail": (worst.get("name") or "") + f" — {_fmt_usd((worst.get('market_cap_usd') or 0)/1e6)} mcap.",
            })

        # Top-25 7d gainer (≥15%)
        gainers_7d = [c for c in markets_top
                      if (c.get("change_7d_pct") is not None
                          and c.get("change_7d_pct") >= 15
                          and not (c.get("symbol") or "").upper().endswith("USD"))]
        if gainers_7d:
            top = max(gainers_7d, key=lambda c: c.get("change_7d_pct") or 0)
            out.append({
                "kind": "trend", "asset": "global", "severity": "good",
                "headline": f"Top-25 7d momentum: {top.get('symbol','?')} {top.get('change_7d_pct'):+.1f}% week (rank #{top.get('rank','?')})",
                "detail": (top.get("name") or "") + " — sustained breakout.",
            })

        # Top-25 7d loser (≤-15%)
        losers_7d = [c for c in markets_top
                     if (c.get("change_7d_pct") is not None
                         and c.get("change_7d_pct") <= -15
                         and not (c.get("symbol") or "").upper().endswith("USD"))]
        if losers_7d:
            worst = min(losers_7d, key=lambda c: c.get("change_7d_pct") or 0)
            out.append({
                "kind": "trend", "asset": "global", "severity": "bad",
                "headline": f"Top-25 7d laggard: {worst.get('symbol','?')} {worst.get('change_7d_pct'):+.1f}% week (rank #{worst.get('rank','?')})",
                "detail": (worst.get("name") or "") + " — sustained drawdown.",
            })

    # BTC dominance threshold crossings (50% / 55% / 60% / 65%)
    g = market.get("global") or {}
    btc_d = g.get("btc_dominance")
    if btc_d is not None:
        # We don't track yesterday's dominance, so just flag standout regimes.
        if btc_d >= 60:
            out.append({
                "kind": "milestone", "asset": "global", "severity": "info",
                "headline": f"BTC dominance high: {btc_d:.1f}% — alt season unlikely",
                "detail": "Capital concentrated in BTC vs alts.",
            })
        elif btc_d <= 45:
            out.append({
                "kind": "milestone", "asset": "global", "severity": "info",
                "headline": f"BTC dominance low: {btc_d:.1f}% — alt rotation in play",
                "detail": "Capital diversifying out of BTC.",
            })

    # Total crypto market cap milestones ($3T / $4T / $5T)
    total_mcap = g.get("total_market_cap_usd")
    if total_mcap is not None and total_mcap > 0:
        t = total_mcap / 1e12
        for thresh in (5.0, 4.0, 3.0):
            if t >= thresh:
                # Only emit the highest threshold crossed (avoid stacking)
                out.append({
                    "kind": "milestone", "asset": "global", "severity": "good",
                    "headline": f"Total crypto market cap above ${thresh:.0f}T (now ${t:.2f}T)",
                    "detail": "Asset class scale milestone.",
                })
                break

    # ----- Whale tab: BTC on-chain transfer volume + active-address swings -----
    whale = (payload.get("whale") or {}).get("btc") or {}

    # Whale tx volume spike (≥2σ above 30d mean)
    tx_vol = whale.get("tx_volume_usd") or []
    if len(tx_vol) >= 31:
        last_row = tx_vol[-1] or {}
        if _is_fresh(last_row.get("date"), max_age_days=14):
            vals = [r.get("value") for r in tx_vol if r.get("value") is not None]
            z = _zscore(vals, 30)
            if z is not None and z >= SIGMA_20:
                last_v = vals[-1]
                out.append({
                    "kind": "anomaly", "asset": "btc", "severity": "alert",
                    "headline": f"BTC on-chain transfer volume spike: Whale tx volume {z:+.1f}σ vs 30d mean",
                    "detail": f"Latest day: {_fmt_usd(last_v / 1e6)} (USD). Heavy on-chain movement.",
                })

    # Active addresses anomaly (≥1.5σ either direction)
    addrs = whale.get("active_addresses") or []
    if len(addrs) >= 31:
        last_row = addrs[-1] or {}
        if _is_fresh(last_row.get("date"), max_age_days=14):
            vals = [r.get("value") for r in addrs if r.get("value") is not None]
            z = _zscore(vals, 30)
            if z is not None and abs(z) >= SIGMA_15:
                sev = "good" if z > 0 else "bad"
                last_v = vals[-1]
                out.append({
                    "kind": "anomaly", "asset": "btc", "severity": sev,
                    "headline": f"BTC active addresses {z:+.1f}σ vs 30d",
                    "detail": f"Latest day: ~{int(last_v):,} addresses. "
                              f"{'Surging' if z > 0 else 'Falling'} network participation.",
                })

    # ----- Markets tab: traditional indices intraday moves -----
    yi = market.get("yahoo_indices") or {}

    def _series_1d_change(idx_dict: dict | None) -> tuple[float | None, str | None]:
        if not idx_dict:
            return None, None
        series = idx_dict.get("series_90d") or []
        if len(series) < 2:
            return None, None
        last = series[-1].get("value")
        prev = series[-2].get("value")
        if not last or not prev:
            return None, None
        return (last / prev - 1) * 100.0, idx_dict.get("latest_date") or series[-1].get("date")

    # NASDAQ 1d move
    ch_nas, date_nas = _series_1d_change(yi.get("nasdaq"))
    if ch_nas is not None and abs(ch_nas) >= 1.5 and _is_fresh(date_nas, max_age_days=7):
        out.append({
            "kind": "trend", "asset": "global",
            "severity": "good" if ch_nas > 0 else "bad",
            "headline": f"NASDAQ {ch_nas:+.2f}% on the day",
            "detail": f"Risk-on tech tape — typically correlated with crypto majors.",
        })

    # Dow Jones 1d move
    ch_dji, date_dji = _series_1d_change(yi.get("dow"))
    if ch_dji is not None and abs(ch_dji) >= 1.5 and _is_fresh(date_dji, max_age_days=7):
        out.append({
            "kind": "trend", "asset": "global",
            "severity": "good" if ch_dji > 0 else "bad",
            "headline": f"Dow Jones {ch_dji:+.2f}% on the day",
            "detail": "Broad US large-cap industrial bellwether.",
        })

    # VIX threshold crossings (20 = calm↔fear, 30 = fear↔panic)
    vix = yi.get("vix") or {}
    vix_series = vix.get("series_90d") or []
    if len(vix_series) >= 2:
        last_v = vix_series[-1].get("value")
        prev_v = vix_series[-2].get("value")
        last_d = vix.get("latest_date") or vix_series[-1].get("date")
        if last_v is not None and prev_v is not None and _is_fresh(last_d, max_age_days=7):
            for thresh, calm_label, fear_label in (
                (20.0, "calm", "fear"),
                (30.0, "fear", "panic"),
            ):
                crossed_up = prev_v < thresh <= last_v
                crossed_dn = prev_v >= thresh > last_v
                if crossed_up:
                    out.append({
                        "kind": "milestone", "asset": "global", "severity": "alert",
                        "headline": f"VIX crossed above {thresh:.0f} ({last_v:.1f}) — {calm_label}→{fear_label}",
                        "detail": "Volatility regime shift higher; risk assets typically wobble.",
                    })
                    break
                if crossed_dn:
                    out.append({
                        "kind": "milestone", "asset": "global", "severity": "good",
                        "headline": f"VIX fell below {thresh:.0f} ({last_v:.1f}) — {fear_label}→{calm_label}",
                        "detail": "Volatility regime cooling; supportive for risk-on.",
                    })
                    break

    # ----- Trading tab: Open Interest and Long/Short crowding -----
    for asset in ("btc", "eth", "link"):
        a = market.get(asset) or {}

        # Open interest surge (≥1.5σ above 30d mean). The cached series uses
        # `oi_usd` keyed rows; tolerate the prompt-spec `oi` field too.
        oi_rows = a.get("open_interest_usd") or a.get("open_interest") or []
        if len(oi_rows) >= 31:
            last_row = oi_rows[-1] or {}
            if _is_fresh(last_row.get("date"), max_age_days=14):
                vals = [
                    (r.get("oi_usd") if r.get("oi_usd") is not None else r.get("oi"))
                    for r in oi_rows
                ]
                vals = [v for v in vals if v is not None]
                z = _zscore(vals, 30) if len(vals) >= 31 else None
                if z is not None and z >= SIGMA_15:
                    out.append({
                        "kind": "anomaly", "asset": asset, "severity": "alert",
                        "headline": f"{asset.upper()} open interest {z:+.1f}σ above 30d mean",
                        "detail": f"Latest OI: {_fmt_usd((vals[-1] or 0) / 1e6)} — leverage building, watch for squeezes.",
                    })

        # Long/Short ratio extremes
        ls_rows = a.get("long_short_ratio") or a.get("long_short") or []
        if ls_rows:
            last_row = ls_rows[-1] or {}
            ratio = last_row.get("ratio")
            if ratio is not None and _is_fresh(last_row.get("date"), max_age_days=14):
                if ratio >= 2.5:
                    out.append({
                        "kind": "anomaly", "asset": asset, "severity": "alert",
                        "headline": f"{asset.upper()} L/S ratio crowded long ({ratio:.2f})",
                        "detail": "Contrarian: heavy long positioning often precedes long-squeezes.",
                    })
                elif ratio <= 0.7:
                    out.append({
                        "kind": "anomaly", "asset": asset, "severity": "good",
                        "headline": f"{asset.upper()} L/S ratio crowded short ({ratio:.2f})",
                        "detail": "Contrarian: heavy short positioning often precedes short-squeezes.",
                    })

    # ----- Trading tab extras: F&G regime crossings + OI vs price divergence -----
    # F&G 25/75 crossings — companion to the existing absolute fear/greed
    # rule. This fires only when the index *crosses* the threshold day-over-
    # day (not just sits below/above), so it surfaces transition events.
    fng_series = market.get("fear_greed") or []
    if isinstance(fng_series, list) and len(fng_series) >= 2:
        prev_v = (fng_series[-2] or {}).get("value")
        last_v = (fng_series[-1] or {}).get("value")
        if isinstance(prev_v, (int, float)) and isinstance(last_v, (int, float)):
            for thresh, lo_label, hi_label in (
                (25, "extreme fear", "fear"),
                (75, "greed", "extreme greed"),
            ):
                crossed_up = prev_v < thresh <= last_v
                crossed_dn = prev_v >= thresh > last_v
                if crossed_up:
                    out.append({
                        "kind": "milestone", "asset": "global",
                        "severity": "info" if thresh == 25 else "alert",
                        "headline": f"Fear & Greed crossed above {thresh} ({int(last_v)}): exiting {lo_label} into {hi_label}",
                        "detail": "Sentiment regime transition.",
                    })
                    break
                if crossed_dn:
                    out.append({
                        "kind": "milestone", "asset": "global",
                        "severity": "good" if thresh == 75 else "alert",
                        "headline": f"Fear & Greed crossed below {thresh} ({int(last_v)}): exiting {hi_label} into {lo_label}",
                        "detail": "Sentiment regime transition.",
                    })
                    break

    # OI vs price divergence — when 7d OI change and 7d price change point in
    # opposite directions (≥5% magnitudes), flag the dislocation. Mirrors
    # the ETF flow vs price divergence pattern.
    for asset in ("btc", "eth", "link"):
        a = market.get(asset) or {}
        oi_rows = a.get("open_interest_usd") or a.get("open_interest") or []
        price_rows = a.get("price") or []
        if len(oi_rows) < 8 or len(price_rows) < 8:
            continue
        try:
            oi_then_raw = oi_rows[-8] or {}
            oi_now_raw = oi_rows[-1] or {}
            oi_then = oi_then_raw.get("oi_usd") if oi_then_raw.get("oi_usd") is not None else oi_then_raw.get("oi")
            oi_now = oi_now_raw.get("oi_usd") if oi_now_raw.get("oi_usd") is not None else oi_now_raw.get("oi")
            p_then = (price_rows[-8] or {}).get("value")
            p_now = (price_rows[-1] or {}).get("value")
            if not (oi_then and oi_now and p_then and p_now):
                continue
            if oi_then <= 0 or p_then <= 0:
                continue
            oi_pct = (oi_now / oi_then - 1) * 100.0
            p_pct = (p_now / p_then - 1) * 100.0
            if abs(oi_pct) >= 5 and abs(p_pct) >= 5 and (oi_pct > 0) != (p_pct > 0):
                oi_dir = "rose" if oi_pct > 0 else "fell"
                p_dir = "fell" if p_pct < 0 else "rose"
                sev = "alert" if oi_pct > 0 else "info"
                out.append({
                    "kind": "anomaly", "asset": asset, "severity": sev,
                    "headline": f"{asset.upper()} OI vs price divergence: OI {oi_dir} {oi_pct:+.1f}% while price {p_dir} {p_pct:+.1f}% (7d)",
                    "detail": "Leverage building against the tape — squeeze setup.",
                })
        except Exception:
            continue

    # ----- DeFi tab extras: second-place chain mover + protocol mover -----
    # `chains[:10]` mover loop above breaks on the FIRST big mover. Catch a
    # second-place mover here so the DeFi tab gets two chain insights when
    # multiple chains are moving.
    chains_extra = ((market.get("defi") or {}).get("chains")) or []
    if chains_extra:
        # Re-scan, skip the index that emitted above (first |change_1d|≥4).
        first_emitter = None
        for idx, c in enumerate(chains_extra[:10]):
            ch = c.get("change_1d_pct")
            if ch is not None and abs(ch) >= 4:
                first_emitter = idx
                break
        if first_emitter is not None:
            for idx, c in enumerate(chains_extra[:10]):
                if idx == first_emitter:
                    continue
                ch = c.get("change_1d_pct")
                if ch is not None and abs(ch) >= 4:
                    sev = "good" if ch > 0 else "bad"
                    out.append({
                        "kind": "trend", "asset": "global", "severity": sev,
                        "headline": f"{c.get('name')} TVL {'+'if ch>0 else ''}{ch:.1f}% today (${(c.get('tvl_usd') or 0)/1e9:.1f}B)",
                        "detail": "Second-place 1d chain mover in the top-10 by TVL.",
                    })
                    break

    # Top DeFi protocol mover (≥10% 1d change)
    protocols = ((market.get("defi") or {}).get("protocols")) or []
    if protocols:
        # Sort by |change_1d_pct| descending, ignoring None.
        ranked = sorted(
            (p for p in protocols if p.get("change_1d_pct") is not None),
            key=lambda p: abs(p.get("change_1d_pct") or 0),
            reverse=True,
        )
        if ranked:
            top = ranked[0]
            ch = top.get("change_1d_pct") or 0
            if abs(ch) >= 10:
                sev = "good" if ch > 0 else "bad"
                tvl_b = (top.get("tvl_usd") or 0) / 1e9
                out.append({
                    "kind": "trend", "asset": "global", "severity": sev,
                    "headline": f"{top.get('name')} TVL {'+' if ch > 0 else ''}{ch:.1f}% today (${tvl_b:.2f}B)",
                    "detail": f"Largest 1d protocol mover by % change. Category: {top.get('category') or '—'}.",
                })

    # ----- DeFi tab extras: TVL history z-score + bridge flow movement -----
    # Single-chain TVL z-score: scan defi.tvl_history.{chain}, compute z-score
    # vs 30d for each chain, emit the top ≥|1.5σ| outlier. Adds a non-obvious
    # TVL anomaly even when 1d % moves are unremarkable.
    tvl_hist = ((market.get("defi") or {}).get("tvl_history")) or {}
    if isinstance(tvl_hist, dict) and tvl_hist:
        z_candidates: list[tuple[float, str, float]] = []
        for chain_name, series in tvl_hist.items():
            if not isinstance(series, list):
                continue
            vals = [r.get("value") for r in series if isinstance(r, dict) and r.get("value") is not None]
            if len(vals) < 31:
                continue
            z = _zscore(vals, 30)
            if z is not None and abs(z) >= SIGMA_15:
                z_candidates.append((abs(z), chain_name, z))
        if z_candidates:
            z_candidates.sort(reverse=True)
            _, chain_name, z_val = z_candidates[0]
            sev = "good" if z_val > 0 else "bad"
            # Latest non-null value for the chain.
            chain_vals = [r.get("value") for r in tvl_hist[chain_name]
                          if isinstance(r, dict) and r.get("value") is not None]
            last_val = chain_vals[-1] if chain_vals else None
            if isinstance(last_val, (int, float)):
                out.append({
                    "kind": "anomaly", "asset": "global", "severity": sev,
                    "headline": f"{chain_name} TVL {z_val:+.1f}σ vs 30d (now ${last_val/1e9:.2f}B)",
                    "detail": "Multi-day TVL anomaly — not just a one-day blip.",
                })

    # Bridges: surface notable bridge volume from defi.bridges when present.
    bridges = ((market.get("defi") or {}).get("bridges")) or []
    if isinstance(bridges, list) and bridges:
        # Sort by 24h volume desc; top entry as the dominant pipe.
        ranked_bridges = sorted(
            (b for b in bridges
             if isinstance(b, dict) and isinstance(b.get("volume_24h_usd"), (int, float))),
            key=lambda b: b.get("volume_24h_usd") or 0,
            reverse=True,
        )
        if ranked_bridges:
            top = ranked_bridges[0]
            vol = top.get("volume_24h_usd") or 0
            if vol >= 100_000_000:
                out.append({
                    "kind": "trend", "asset": "global", "severity": "info",
                    "headline": f"Bridge flow leader: {top.get('name','?')} moved {_fmt_usd(vol/1e6)} in 24h",
                    "detail": "Largest cross-chain flow today — liquidity rotation across L1/L2.",
                })

    # ETH/BTC ratio extremes
    ethbtc = market.get("ethbtc") or []
    if len(ethbtc) >= 60:
        vals = [r["value"] for r in ethbtc]
        last = vals[-1]
        m6 = min(vals[-180:]) if len(vals) >= 180 else min(vals)
        x6 = max(vals[-180:]) if len(vals) >= 180 else max(vals)
        if last <= m6 * 1.005:
            out.append({"kind":"anomaly","asset":"global","severity":"info",
                "headline": f"ETH/BTC at ~6-month low ({last:.5f})",
                "detail": None})
        elif last >= x6 * 0.995:
            out.append({"kind":"anomaly","asset":"global","severity":"info",
                "headline": f"ETH/BTC at ~6-month high ({last:.5f})",
                "detail": None})
    return out


# ----- per-tab classification -----
#
# Every insight is tagged with the dashboard tab it belongs to so the
# Insights bar can filter to "just the things relevant to what I'm looking
# at right now." The Overview tab is intentionally NOT in the mapping —
# Overview shows its own curated "Top insights" card from the global list,
# and the per-tab Insights bar is hidden when Overview is active.

VALID_TABS = {"etf", "signals", "trading", "defi", "whale", "stocks",
              "poc", "social", "ainews"}


# Mirrors the AI_EXPOSED_TICKERS list in app.py's renderAiNewsTab so the AI
# insights rules look at the same subset of stocks_signals the AI tab does.
_AI_EXPOSED_TICKERS = (
    "NVDA", "GOOGL", "MSFT", "META", "AMZN", "AAPL", "TSLA",
    "AMD", "INTC", "ORCL", "CRM", "PLTR", "SMCI", "ARM", "AVGO",
)


def _ainews_insights(payload: dict) -> list[dict]:
    """Rules surfacing notable AI-tab events. All emitted insights are tagged
    ``tab="ainews"`` so the per-tab Insights bar finally has something to show.
    Defensive throughout — every block guards for missing/malformed data so a
    bad fetch never raises.

    Inputs read:
      * ``payload['market']['ai_news']`` — RSS-driven AI news + sentiment summary
      * ``payload['market']['ai_curated']['top_funded_companies']`` — mega-rounds
      * ``payload['market']['stocks_signals']`` — to filter for AI-exposed names
    """
    out: list[dict] = []
    market = payload.get("market") or {}
    ai_news = market.get("ai_news") or {}
    items = ai_news.get("items") or []
    summary = ai_news.get("summary") or {}

    try:
        pos = int(summary.get("positive") or 0)
        neg = int(summary.get("negative") or 0)
        total = int(summary.get("total") or 0)
        net_score = summary.get("net_score")
        if net_score is None:
            net_score = pos - neg
        net_score = int(net_score)
        sentiment_label = (summary.get("sentiment_label") or "").upper()
    except (TypeError, ValueError):
        pos = neg = total = net_score = 0
        sentiment_label = ""

    # Rule 1: AI news sentiment skew. Fires when the labelled overall flips to
    # POSITIVE/NEGATIVE AND the net skew is materially above the floor — we
    # require both ≥15 articles for stability and |net_score|/total ≥ 0.25.
    if total >= 15 and sentiment_label in ("POSITIVE", "NEGATIVE"):
        try:
            skew = abs(net_score) / max(total, 1)
        except ZeroDivisionError:
            skew = 0.0
        if skew >= 0.25:
            pos_pct = (pos / total) * 100.0
            neg_pct = (neg / total) * 100.0
            if sentiment_label == "POSITIVE":
                out.append({
                    "kind": "trend", "asset": "global", "severity": "good",
                    "headline": f"AI news sentiment skews POSITIVE: {pos_pct:.0f}% pos vs {neg_pct:.0f}% neg across {total} articles",
                    "detail": f"Net score {net_score:+d}. Sentiment is one-sided across today's AI tape.",
                    "score": 60 + int(skew * 30),
                })
            else:
                out.append({
                    "kind": "trend", "asset": "global", "severity": "bad",
                    "headline": f"AI news sentiment skews NEGATIVE: {neg_pct:.0f}% neg vs {pos_pct:.0f}% pos across {total} articles",
                    "detail": f"Net score {net_score:+d}. Sentiment is one-sided across today's AI tape.",
                    "score": 60 + int(skew * 30),
                })

    # Rule 2: AI news volume surge. We don't persist a 7-day history of total
    # article counts, so we use an absolute threshold tuned against the
    # fetcher's natural ceiling (cap 60, typical ~25-40). ≥50 articles in the
    # 24h window is a "heavy news day."
    if total >= 50:
        out.append({
            "kind": "anomaly", "asset": "global", "severity": "info",
            "headline": f"AI news flow heavy: {total} articles in the last 24h",
            "detail": "Above the typical daily cadence — busy news day across AI feeds.",
            "score": 55,
        })

    # Rule 3: Single-source dominance. One feed accounts for >40% of today's
    # AI news. Useful when one publisher floods the feed (helps the reader
    # weight the sentiment skew above).
    if total >= 20 and items:
        try:
            by_source: dict[str, int] = {}
            for it in items:
                if not isinstance(it, dict):
                    continue
                src = (it.get("source") or it.get("source_name") or "").strip()
                if not src:
                    continue
                by_source[src] = by_source.get(src, 0) + 1
            if by_source:
                top_src, top_cnt = max(by_source.items(), key=lambda kv: kv[1])
                share = top_cnt / total
                if share >= 0.40:
                    out.append({
                        "kind": "anomaly", "asset": "global", "severity": "info",
                        "headline": f"AI news flow concentrated: {top_src} is {share*100:.0f}% of today's {total} articles",
                        "detail": "One source dominates — read the sentiment skew with that in mind.",
                        "score": 50,
                    })
        except Exception as e:
            print(f"  [insights] ai news source-concentration suppressed: {type(e).__name__}", file=sys.stderr)

    # Rule 4: Top AI-exposed ticker — STRONG BUY / STRONG SELL (|score| ≥ 50)
    # OR a label flip vs. ~7 days ago using the cached score history. Pick
    # only the single most-extreme name so we don't flood the bar.
    stocks = market.get("stocks_signals") or []
    exposed_set = {t.upper() for t in _AI_EXPOSED_TICKERS}
    ai_stocks: list[dict] = []
    try:
        for s in stocks:
            if not isinstance(s, dict):
                continue
            sym = (s.get("symbol") or "").upper()
            if sym and sym in exposed_set:
                ai_stocks.append(s)
    except Exception:
        ai_stocks = []

    # Rule 4a: strongest |score| at ≥50
    try:
        scored = [
            (s, float(s.get("score")))
            for s in ai_stocks
            if isinstance(s.get("score"), (int, float))
        ]
        if scored:
            top_stock, top_score = max(scored, key=lambda t: abs(t[1]))
            if abs(top_score) >= 50:
                sym = (top_stock.get("symbol") or "?").upper()
                name = top_stock.get("name") or ""
                label = top_stock.get("label") or ""
                if top_score > 0:
                    out.append({
                        "kind": "signal", "asset": sym, "severity": "good",
                        "headline": f"{sym} AI-exposed ticker {label}: score {int(top_score):+d}",
                        "detail": f"{name} — strongest score among the AI-exposed subset.",
                        "score": 70,
                    })
                else:
                    out.append({
                        "kind": "signal", "asset": sym, "severity": "alert",
                        "headline": f"{sym} AI-exposed ticker {label}: score {int(top_score):+d}",
                        "detail": f"{name} — weakest score among the AI-exposed subset.",
                        "score": 70,
                    })
    except Exception as e:
        print(f"  [insights] ai-exposed top score suppressed: {type(e).__name__}", file=sys.stderr)

    # Rule 4b: label flip vs ~7d ago using cached rolling score history.
    # We compare today's score against the score from ~7 entries ago and
    # require the sign / strong-tier label to have flipped.
    try:
        for s in ai_stocks:
            hist = s.get("history") or []
            if not isinstance(hist, list) or len(hist) < 8:
                continue
            now_row = hist[-1] or {}
            then_row = hist[-8] or {}
            now_score = now_row.get("score")
            then_score = then_row.get("score")
            if not isinstance(now_score, (int, float)) or not isinstance(then_score, (int, float)):
                continue
            now_score = int(now_score)
            then_score = int(then_score)
            # Require a meaningful magnitude on at least one side AND a sign flip
            # so we don't fire on noisy crossings near zero.
            if max(abs(now_score), abs(then_score)) < 30:
                continue
            flipped_up = then_score <= 0 < now_score
            flipped_dn = then_score >= 0 > now_score
            if not (flipped_up or flipped_dn):
                continue
            sym = (s.get("symbol") or "?").upper()
            label = s.get("label") or ""
            sev = "good" if flipped_up else "bad"
            out.append({
                "kind": "signal", "asset": sym, "severity": sev,
                "headline": f"{sym} signal flipped {'positive' if flipped_up else 'negative'}: {then_score:+d} → {now_score:+d}{(' (' + label + ')') if label else ''}",
                "detail": "AI-exposed ticker direction change over the last ~7 trading days.",
                "score": 65,
            })
            break  # one flip is enough — don't spam the bar
    except Exception as e:
        print(f"  [insights] ai-exposed label flip suppressed: {type(e).__name__}", file=sys.stderr)

    # Rule 5: Sentiment / price divergence. AI news leans one way but the
    # AI-exposed stocks lean the opposite. Helpful for spotting dislocations.
    try:
        if total >= 15 and ai_stocks and sentiment_label in ("POSITIVE", "NEGATIVE"):
            stock_scores = [
                float(s.get("score"))
                for s in ai_stocks
                if isinstance(s.get("score"), (int, float))
            ]
            if stock_scores:
                avg_stock = sum(stock_scores) / len(stock_scores)
                if sentiment_label == "POSITIVE" and avg_stock <= -10:
                    out.append({
                        "kind": "anomaly", "asset": "global", "severity": "alert",
                        "headline": f"AI sentiment / price divergence: news net {net_score:+d} but AI-exposed stocks avg score {avg_stock:+.0f}",
                        "detail": "News tape bullish while AI-exposed equities lag — possible dislocation.",
                        "score": 75,
                    })
                elif sentiment_label == "NEGATIVE" and avg_stock >= 10:
                    out.append({
                        "kind": "anomaly", "asset": "global", "severity": "info",
                        "headline": f"AI sentiment / price divergence: news net {net_score:+d} but AI-exposed stocks avg score {avg_stock:+.0f}",
                        "detail": "News tape bearish while AI-exposed equities hold up — possible dislocation.",
                        "score": 75,
                    })
    except Exception as e:
        print(f"  [insights] ai sentiment/price divergence suppressed: {type(e).__name__}", file=sys.stderr)

    # Rule 6: Mega funding round in the last 7 days. Curated snapshot exposes
    # `top_funded_companies` with last_round_size_usd + last_round_date. We
    # surface ≥$1B rounds dated within the last 7 days.
    try:
        curated = market.get("ai_curated") or {}
        companies = curated.get("top_funded_companies") or []
        recent_mega: list[tuple[float, dict]] = []
        for c in companies:
            if not isinstance(c, dict):
                continue
            size = c.get("last_round_size_usd")
            date_str = c.get("last_round_date")
            if not isinstance(size, (int, float)) or size < 1_000_000_000:
                continue
            if not _is_fresh(date_str, max_age_days=7):
                continue
            recent_mega.append((float(size), c))
        if recent_mega:
            recent_mega.sort(key=lambda t: t[0], reverse=True)
            size, c = recent_mega[0]
            name = c.get("name") or "?"
            stage = c.get("last_round_stage") or ""
            valuation = c.get("valuation_usd")
            size_b = size / 1e9
            val_part = ""
            if isinstance(valuation, (int, float)) and valuation > 0:
                val_part = f" at ${valuation/1e9:.0f}B valuation"
            stage_part = f" ({stage})" if stage else ""
            out.append({
                "kind": "milestone", "asset": "global", "severity": "good",
                "headline": f"AI mega-round this week: {name} raised ${size_b:.1f}B{val_part}{stage_part}",
                "detail": f"Closed {c.get('last_round_date','')}. Among the largest AI rounds on record.",
                "score": 85,
            })
    except Exception as e:
        print(f"  [insights] ai mega-round suppressed: {type(e).__name__}", file=sys.stderr)

    # Rule 7 (rolling history): sentiment label flipped vs yesterday. Only
    # fires on a POSITIVE↔NEGATIVE transition with material skew on both
    # sides — NEUTRAL transitions get ignored because the sentiment_label is
    # already gated on |net_score|/total ≥ 0.10 upstream and a NEUTRAL day
    # often just means "fewer articles," not "consensus shifted." Requires a
    # prior day in `data/insights_history.json` to compare against.
    try:
        history = _load_insights_history()
        prev = _previous_day_entry(history)
        prev_label = (prev or {}).get("ai_news_sentiment_label")
        if (
            sentiment_label in ("POSITIVE", "NEGATIVE")
            and prev_label in ("POSITIVE", "NEGATIVE")
            and sentiment_label != prev_label
            and total >= 15
        ):
            sev = "good" if sentiment_label == "POSITIVE" else "bad"
            out.append({
                "kind": "anomaly", "asset": "global", "severity": sev,
                "headline": f"AI news sentiment flipped {prev_label} → {sentiment_label} day-over-day",
                "detail": f"Net score {net_score:+d} across {total} articles. "
                          f"Prior day's labelled sentiment was {prev_label}.",
                "score": 75,
            })

        # Rule 8 (rolling history): today's article count is ≥2σ above the
        # trailing 7-day mean. Needs ≥4 prior days of data with non-trivial
        # totals to compute a stable mean/std; tighter than 30d but matches
        # the cadence of the AI news fetcher which can swing day-to-day. The
        # absolute floor of mean+5 guards against false positives when the
        # baseline collapses to near-zero on a quiet weekend.
        prior_totals = [
            int(h.get("ai_news_total") or 0)
            for h in history
            if h.get("date") and h.get("date") < _today_iso()
        ]
        prior_totals = [t for t in prior_totals[-7:] if t > 0]
        if len(prior_totals) >= 4 and total > 0:
            mean_total = statistics.mean(prior_totals)
            std_total = statistics.pstdev(prior_totals) or 1.0
            z = (total - mean_total) / std_total
            if z >= SIGMA_20 and total >= mean_total + 5:
                out.append({
                    "kind": "anomaly", "asset": "global", "severity": "info",
                    "headline": f"AI news volume surge: {total} articles · {z:+.1f}σ vs 7-day mean ({mean_total:.0f})",
                    "detail": "Article count materially above the trailing weekly cadence — busy news day.",
                    "score": 65,
                })
    except Exception as e:
        # Defensive — rules 7/8 must never tank the rest of the ainews bar.
        print(f"[insights-rolling] error: {e}", file=sys.stderr)

    return out



def _market_insight_tab(insight: dict) -> str:
    """Classify a market-generator insight to a single dashboard tab.

    Headlines are stable strings emitted by `_market_insights`; the matching
    is intentionally explicit so test_insights can catch drift if someone
    changes a headline without updating the mapping.
    """
    raw = insight.get("headline") or ""
    h = raw.lower()
    # Trading desk: sentiment, funding, IV, BTC↔ETH crosses, OI, L/S
    if "fear & greed" in h: return "trading"
    if "funding flipped" in h: return "trading"
    if "dvol" in h: return "trading"
    if "eth/btc" in h: return "trading"
    if "open interest" in h: return "trading"
    if "l/s ratio" in h: return "trading"
    if "oi vs price divergence" in h: return "trading"
    # DeFi: gas, stablecoin supply, DEX volume, chain/protocol TVL,
    # bridge flow leader
    if "gas" in h: return "defi"
    if "stablecoin supply" in h: return "defi"
    if "dex 24h" in h: return "defi"
    if "tvl" in h: return "defi"
    if "bridge flow" in h: return "defi"
    # Whale / on-chain: mempool, hashrate, difficulty, mining concentration,
    # on-chain transfer volume, active-address swings
    if "mempool" in h: return "whale"
    if "hashrate" in h: return "whale"
    if "difficulty retarget" in h: return "whale"
    if "mining concentration" in h: return "whale"
    if "whale tx volume" in h: return "whale"
    if "on-chain transfer volume" in h: return "whale"
    if "active addresses" in h: return "whale"
    # Markets / macro: traditional indices, news, trending tickers, source divergence.
    # The "Markets" tab was folded into the Crypto/Overview tab; routing these to
    # tab="markets" would silently drop them in the per-tab insights bar filter
    # (renderInsights matches state.tab strict-equals; "markets" matches nothing).
    # Route by content: traditional-indices + macro → Stocks tab (where the
    # Traditional Indices card lives now), crypto-wide moves → Crypto Signals,
    # news → Crypto/Overview's Top insights card.
    if "dxy" in h: return "stocks"
    if "10y treasury" in h: return "stocks"
    if "gold at" in h: return "stocks"
    if "s&p 500" in h: return "stocks"
    if "nasdaq" in h: return "stocks"
    if "dow jones" in h: return "stocks"
    if "vix " in h or h.startswith("vix"): return "stocks"
    if "📰" in raw: return "social"
    if "trending #1" in h: return "signals"
    if "price divergence" in h: return "signals"
    if "dex hot pool" in h: return "defi"
    # Top-25 movers + dominance + total mcap milestones → Crypto Signals
    if "top-25" in h: return "signals"
    if "btc dominance" in h: return "signals"
    if "total crypto market cap" in h: return "signals"
    # Default: anything else macro-flavoured falls into Crypto Signals.
    return "signals"


def build_insights(payload: dict, limit: int = 12) -> list[dict]:
    """Top-level entry. Returns up to `limit` insights, prioritised."""
    etf_btc = _etf_insights(payload, "btc")
    etf_eth = _etf_insights(payload, "eth")
    sigs = _signal_insights(payload)
    stocks = _stocks_insights(payload)
    whales = _whale_insights(payload)
    mkts = _market_insights(payload)
    ainews = _ainews_insights(payload)
    pocs = _poc_insights(payload)
    socials = _social_insights(payload)

    # Tag with the tab each insight belongs to.
    for i in etf_btc + etf_eth:
        i.setdefault("tab", "etf")
    for i in sigs:
        i.setdefault("tab", "signals")
    for i in stocks:
        i.setdefault("tab", "stocks")
    for i in whales:
        i.setdefault("tab", "whale")
    for i in pocs:
        i.setdefault("tab", "poc")
    for i in socials:
        i.setdefault("tab", "social")
    for i in mkts:
        i.setdefault("tab", _market_insight_tab(i))
    for i in ainews:
        i.setdefault("tab", "ainews")

    out = etf_btc + etf_eth + sigs + stocks + whales + mkts + ainews + pocs + socials

    # Prioritise: milestones + anomalies first, then ETF, then trends, then signals, then info
    rank = {
        ("milestone", "good"): 1, ("milestone", "bad"): 1, ("milestone", "alert"): 1,
        ("anomaly", "good"): 2,   ("anomaly", "alert"): 2, ("anomaly", "bad"): 2,
        ("etf", "good"): 3,        ("etf", "bad"): 3,
        ("signal", "good"): 4,     ("signal", "bad"): 4, ("signal", "alert"): 4,
        ("stocks", "warning"): 4,  ("stocks", "info"): 6,
        ("trend", "good"): 5,      ("trend", "bad"): 5,
    }
    out.sort(key=lambda r: rank.get((r["kind"], r["severity"]), 9))

    # Persist a small rolling snapshot for next build's day-over-day rules
    # (sentiment-flip + news volume σ in `_ainews_insights`). We do this after
    # ranking so the rules above see only *prior* days — today's snapshot
    # never feeds into its own thresholds. Best-effort I/O: a write failure
    # is logged but never raises.
    snapshot = _build_today_snapshot(payload)
    if snapshot is not None:
        history = _load_insights_history()
        history = _record_today(history, snapshot)
        _save_insights_history(history)

    return out[:limit]
