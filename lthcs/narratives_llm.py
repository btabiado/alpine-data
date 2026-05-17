"""LTHCS LLM-backed narrative generator.

This module is an OPT-IN replacement for the templated narratives in
:mod:`lthcs.narratives`. It calls the Anthropic Claude API with prompt
caching to produce richer, factually-grounded narratives per ticker.

Design goals
------------

* Cheap. The system prompt (the LTHCS framework explanation + style
  guide) and the universe-wide macro context are stamped with
  ``cache_control`` blocks. Across 168 tickers in one run they only
  pay full price for the first call and ~10% of cached cost for the
  rest.
* Robust. Missing ``ANTHROPIC_API_KEY``, missing ``anthropic`` SDK, or
  any API/network error falls back to :func:`lthcs.narratives.generate_narratives`.
  The daily pipeline never crashes because of LLM trouble.
* Opt-in. Nothing here runs unless ``LTHCS_NARRATIVES_LLM_ENABLED=1``
  is set, and Stage 7 of ``lthcs_daily.py`` is the only wire-up site.

The public surface is two functions:

* :func:`generate_llm_narrative` -- single ticker
* :func:`generate_universe_narratives` -- whole-universe batch helper

Both always return narrative dicts in the same shape as
:func:`lthcs.narratives.generate_narratives` plus a few telemetry fields
(``narrative``, ``model``, ``input_tokens``, ``output_tokens``,
``cached_input_tokens``, ``generated_at``).
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import logging
import os
from typing import Any, Dict, List, Optional

from . import narratives as _templated

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-5"
ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_ENABLED = "LTHCS_NARRATIVES_LLM_ENABLED"
ENV_MODEL = "LTHCS_NARRATIVES_LLM_MODEL"

# Anthropic prompt caching beta header. Many recent SDK versions don't
# require it (caching is GA), but we pass it for older SDKs. Harmless if
# the SDK no longer needs it.
PROMPT_CACHING_BETA_HEADER = "prompt-caching-2024-07-31"

# Conservative token budget for the user message portion of the prompt.
MAX_OUTPUT_TOKENS = 400

# The system prompt is intentionally long: it defines the LTHCS framework
# and the desired writing style. Long => high cache hit value.
SYSTEM_PROMPT = """You are an analyst writing concise long-term-hold confidence narratives for the LTHCS framework.

# LTHCS framework

LTHCS (Long-Term Hold Confidence Score) is a 0-100 composite scored daily for each ticker in a 168-name universe. The composite is a weighted blend of five pillars:

1. Adoption Momentum (revenue growth + Google Trends slope) - is the franchise still being adopted?
2. Institutional Confidence (price momentum, 13F holdings, insider Form 4 activity) - what are the smart-money cohort doing?
3. Financial Evolution (FCF margin, ROIC, gross margin, OCF growth) - is the business compounding cleanly?
4. Thesis Integrity (qualitative score: moat, capital allocation, governance) - does the original thesis still hold?
5. Demand Environment (sector ETF strength, macro regime overlay) - is the operating environment supportive?

Each pillar is normalized 0-100 (universe-relative percentile). The composite uses sector-aware maturity weights (mature_compounder vs growth_compounder).

# Bands

The composite maps to six bands:

- Elite (90-100): highest conviction, hold/add
- High Confidence (80-89): strong, hold
- Constructive (70-79): healthy, hold
- Monitor (60-69): watch for confirmation
- Weakening (50-59): structural concerns, reduce-on-strength candidates
- Structural Review (0-49): exit/review decision required

# Insider Form 4 signal language

When citing insider activity:
- "Open-market" sales (planned_10b5_1=false) are the highest-information signal - especially CEO/CFO/Director sales over $5M.
- "10b5-1 plan" sales (planned_10b5_1=true) are scheduled diversification - lower signal, don't over-weight.
- "Cluster buying" (3+ insiders buying in 30 days) is rare and bullish.
- Use the conviction_score (-1.0 to +1.0) as your directional anchor; regime label ("heavy_selling", "balanced", "cluster_buying") describes the band.

# 13F holdings signal language

When citing institutional 13F activity:
- conviction_signal in {accumulating, steady, distributing, mixed} - use exactly this taxonomy.
- "accumulating" = net buyers materially outweigh sellers among the 21-tracked-manager cohort.
- "distributing" = the inverse.
- "mixed" = roughly balanced; not a strong signal.
- Name 1-3 specific top holders when relevant (BlackRock, State Street, Vanguard, Wellington, Capital, etc.).

# Style guide

- 80-150 words. One paragraph. No bullets, no headings.
- Factual. No hedging filler ("it appears that...", "one could argue..."). State observations directly.
- Identify the BINDING pillar (the lowest one currently dragging the composite) and the SUPPORTING pillar (the highest one anchoring it). The narrative should frame the score as the resolution of those two forces.
- Cite specific data points: dollar values, transaction counts, manager names, percentile bands. Avoid vague language like "strong" or "concerning" without a number behind it.
- The macro overlay is context, not a driver. Mention it only when it materially changes the picture.
- Tone: analyst desk note, not marketing copy. No emojis, no exclamations. American English."""


# ---------------------------------------------------------------------------
# Module load: detect SDK availability lazily so import never fails.
# ---------------------------------------------------------------------------


def _import_anthropic():
    """Import the anthropic SDK lazily; return module or None on failure."""
    try:
        import anthropic  # type: ignore

        return anthropic
    except Exception as exc:  # pragma: no cover - env-dependent
        logger.debug("anthropic SDK not importable: %s", exc)
        return None


def _api_key() -> Optional[str]:
    key = os.environ.get(ENV_API_KEY, "").strip()
    return key or None


def _model_from_env(default: str = DEFAULT_MODEL) -> str:
    return os.environ.get(ENV_MODEL, "").strip() or default


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _format_subscores(subs: Dict[str, float]) -> str:
    """Render the 5 pillar sub-scores in a compact, prompt-friendly form."""
    order = _templated.PILLAR_ORDER
    parts = []
    for p in order:
        name = _templated.HUMAN_PILLAR_NAMES.get(p, p)
        val = float(subs.get(p, 50.0))
        parts.append(f"{name}={val:.1f}")
    return ", ".join(parts)


def _binding_and_supporting(subs: Dict[str, float]) -> Dict[str, Any]:
    """Pick lowest + highest pillar (the binding constraint + the anchor)."""
    order = _templated.PILLAR_ORDER
    ordered = [(p, float(subs.get(p, 50.0))) for p in order]
    by_high = sorted(ordered, key=lambda kv: kv[1], reverse=True)
    by_low = sorted(ordered, key=lambda kv: kv[1])
    return {
        "supporting_pillar": _templated.HUMAN_PILLAR_NAMES.get(by_high[0][0], by_high[0][0]),
        "supporting_score": round(by_high[0][1], 1),
        "binding_pillar": _templated.HUMAN_PILLAR_NAMES.get(by_low[0][0], by_low[0][0]),
        "binding_score": round(by_low[0][1], 1),
    }


def _summarize_insider(insider: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not insider:
        return {"available": False}
    raw = insider.get("raw_transactions") or []
    # Highlight the 3 largest open-market transactions in absolute dollars.
    open_market = [t for t in raw if not t.get("planned_10b5_1")]
    open_market_sorted = sorted(
        open_market, key=lambda t: abs(float(t.get("value") or 0.0)), reverse=True
    )[:3]
    highlights = []
    for t in open_market_sorted:
        if not t.get("value"):
            continue
        highlights.append(
            {
                "code": t.get("code"),
                "date": t.get("date"),
                "insider": t.get("insider"),
                "role": t.get("role"),
                "value_usd": round(float(t.get("value") or 0.0), 0),
                "planned_10b5_1": bool(t.get("planned_10b5_1")),
            }
        )
    return {
        "available": True,
        "regime": _classify_insider_regime(insider),
        "conviction_score": round(float(insider.get("conviction_score") or 0.0), 2),
        "cluster_buying": bool(insider.get("cluster_buying")),
        "ceo_cfo_action": insider.get("ceo_cfo_action") or "neutral",
        "net_dollar_value": round(float(insider.get("net_dollar_value") or 0.0), 0),
        "buy_count": int(insider.get("buy_count") or 0),
        "top_open_market_transactions": highlights,
    }


def _classify_insider_regime(insider: Dict[str, Any]) -> str:
    """Map conviction_score / cluster_buying to a regime label."""
    cs = float(insider.get("conviction_score") or 0.0)
    if insider.get("cluster_buying"):
        return "cluster_buying"
    if cs <= -0.6:
        return "heavy_selling"
    if cs <= -0.2:
        return "net_selling"
    if cs >= 0.6:
        return "heavy_buying"
    if cs >= 0.2:
        return "net_buying"
    return "balanced"


def _summarize_holdings(holdings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not holdings:
        return {"available": False}
    top_holders = holdings.get("top_holders") or []
    return {
        "available": True,
        "conviction_signal": holdings.get("conviction_signal") or "mixed",
        "signal_score": round(float(holdings.get("signal_score") or 0.0), 2),
        "manager_count": holdings.get("manager_count"),
        "latest_quarter": holdings.get("latest_quarter"),
        "share_change_pct": (
            holdings.get("quarter_over_quarter", {}) or {}
        ).get("share_change_pct"),
        "net_buyers": (holdings.get("quarter_over_quarter", {}) or {}).get(
            "net_buyers"
        ),
        "net_sellers": (holdings.get("quarter_over_quarter", {}) or {}).get(
            "net_sellers"
        ),
        "top_3_holders": [
            {"manager": h.get("manager"), "value_bn": h.get("value_bn")}
            for h in top_holders[:3]
        ],
    }


def _summarize_macro(macro: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not macro:
        return {"available": False}
    flags = macro.get("regime_flags") or {}
    return {
        "available": True,
        "hy_oas_pct": (macro.get("hy_oas") or {}).get("current"),
        "ig_oas_pct": (macro.get("ig_oas") or {}).get("current"),
        "yield_curve_2s10s": (macro.get("yield_curve_2s10s") or {}).get("current"),
        "curve_inverted": bool(flags.get("curve_inverted")),
        "dollar_strong": bool(flags.get("dollar_strong")),
        "hy_stress": bool(flags.get("hy_stress")),
    }


def _summarize_variable_detail(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Index variable_detail rows by pillar, trimmed to components + sub_score."""
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        pillar = row.get("pillar")
        if not pillar:
            continue
        out[pillar] = {
            "sub_score": row.get("sub_score"),
            "components": row.get("components") or {},
            "data_quality": row.get("data_quality") or {},
        }
    return out


def build_system_blocks(macro_breadth: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Construct the cached system prompt.

    Returns a list of system blocks with cache_control on each cacheable
    segment. Anthropic's prompt caching keys on prefix identity, so the
    macro block is appended *after* the framework prompt and gets its
    own cache_control marker so it can be reused across all 168 tickers
    on the same day.
    """
    blocks: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    macro = _summarize_macro(macro_breadth)
    if macro.get("available"):
        macro_text = (
            "# Today's macro overlay\n"
            + json.dumps(macro, indent=2, sort_keys=True)
            + "\n\nUse this context only when it materially affects the narrative."
        )
        blocks.append(
            {
                "type": "text",
                "text": macro_text,
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks


def build_user_message(
    ticker: str,
    snapshot_row: Dict[str, Any],
    variable_detail_rows: List[Dict[str, Any]],
    insider_data: Optional[Dict[str, Any]] = None,
    holdings_data: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the per-ticker user prompt (NOT cached -- changes every call)."""
    subs = dict(snapshot_row.get("subscores") or {})
    bs = _binding_and_supporting(subs)
    payload: Dict[str, Any] = {
        "ticker": ticker,
        "sector": snapshot_row.get("sector"),
        "lthcs_score": snapshot_row.get("lthcs_score"),
        "band": snapshot_row.get("band"),
        "drift_1d": snapshot_row.get("drift_1d"),
        "drift_30d": snapshot_row.get("drift_30d"),
        "confidence_level": snapshot_row.get("confidence_level"),
        "subscores": {k: round(float(v), 1) for k, v in subs.items()},
        "binding_and_supporting": bs,
        "pillar_components": _summarize_variable_detail(variable_detail_rows),
        "insider": _summarize_insider(insider_data),
        "holdings": _summarize_holdings(holdings_data),
    }
    return (
        "Write one LTHCS narrative for the ticker below following the system style guide. "
        "Return only the narrative paragraph -- no preamble, no labels.\n\n"
        + json.dumps(payload, indent=2, sort_keys=True, default=str)
    )


# ---------------------------------------------------------------------------
# Anthropic call + parsing
# ---------------------------------------------------------------------------


def _call_anthropic(
    *,
    client,
    model: str,
    system_blocks: List[Dict[str, Any]],
    user_message: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
):
    """Single API call wrapping the SDK; isolated for easy mocking."""
    try:
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user_message}],
            extra_headers={"anthropic-beta": PROMPT_CACHING_BETA_HEADER},
        )
    except TypeError:
        # Older/newer SDKs may not accept extra_headers kwarg this way.
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user_message}],
        )


def _extract_text(response: Any) -> str:
    """Pull the assistant text out of an Anthropic Message response."""
    try:
        content = getattr(response, "content", None) or []
        parts: List[str] = []
        for block in content:
            # SDK objects have .text; dicts have ['text'].
            text = getattr(block, "text", None)
            if text is None and isinstance(block, dict):
                text = block.get("text")
            if text:
                parts.append(str(text))
        return "\n".join(parts).strip()
    except Exception:
        return ""


def _extract_usage(response: Any) -> Dict[str, int]:
    """Pull token counts from response.usage; tolerant of SDK shape drift."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {"input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0}

    def _g(name: str) -> int:
        val = getattr(usage, name, None)
        if val is None and isinstance(usage, dict):
            val = usage.get(name)
        try:
            return int(val or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "input_tokens": _g("input_tokens"),
        "output_tokens": _g("output_tokens"),
        # Anthropic exposes both cache_read_input_tokens and cache_creation_input_tokens.
        # Reads are the ones that save money; report them as cached_input_tokens.
        "cached_input_tokens": _g("cache_read_input_tokens"),
        "cache_creation_input_tokens": _g("cache_creation_input_tokens"),
    }


# ---------------------------------------------------------------------------
# Fallback helper
# ---------------------------------------------------------------------------


def _fallback_narrative(
    ticker: str,
    snapshot_row: Dict[str, Any],
    reason: str,
) -> Dict[str, Any]:
    """Build the templated narrative and stamp it with fallback metadata."""
    templated = _templated.generate_narratives(snapshot_row)
    # Concatenate the four templated paragraphs into a single narrative
    # field, so callers always get a uniform shape.
    combined = " ".join(
        [
            templated.get("todays_take", ""),
            templated.get("why_changed", ""),
            templated.get("why_not_to_sell", ""),
            templated.get("what_would_break", ""),
        ]
    ).strip()
    return {
        "ticker": ticker,
        "narrative": combined,
        "model": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "generated_at": _now_iso(),
        "fallback": True,
        "fallback_reason": reason,
        # Also keep the four templated keys for backward compatibility
        # with any callers that still want the V1 fields.
        "todays_take": templated.get("todays_take"),
        "why_changed": templated.get("why_changed"),
        "why_not_to_sell": templated.get("why_not_to_sell"),
        "what_would_break": templated.get("what_would_break"),
        "confidence_level": templated.get("confidence_level"),
    }


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_llm_narrative(
    ticker: str,
    snapshot_row: Dict[str, Any],
    variable_detail_rows: List[Dict[str, Any]],
    insider_data: Optional[Dict[str, Any]] = None,
    holdings_data: Optional[Dict[str, Any]] = None,
    macro_breadth: Optional[Dict[str, Any]] = None,
    model: str = DEFAULT_MODEL,
    use_cache: bool = True,
    client: Any = None,
) -> Dict[str, Any]:
    """Generate one LLM-backed narrative; fall back to template on any failure.

    Parameters mirror the per-ticker inputs available in the daily pipeline.

    The ``client`` parameter is intended for testing -- pass a mock Anthropic
    client to avoid network calls. In production, leave it None and the
    function will construct one from ``ANTHROPIC_API_KEY``.
    """
    if client is None:
        api_key = _api_key()
        if not api_key:
            return _fallback_narrative(ticker, snapshot_row, "missing_api_key")
        anthropic = _import_anthropic()
        if anthropic is None:
            return _fallback_narrative(ticker, snapshot_row, "anthropic_sdk_unavailable")
        try:
            client = anthropic.Anthropic(api_key=api_key)
        except Exception as exc:
            logger.warning("Anthropic client construction failed: %s", exc)
            return _fallback_narrative(ticker, snapshot_row, "client_init_failed")

    try:
        system_blocks = build_system_blocks(macro_breadth if use_cache else None)
        # When caching is disabled, strip cache_control to skip caching.
        if not use_cache:
            system_blocks = [
                {k: v for k, v in b.items() if k != "cache_control"}
                for b in system_blocks
            ]
        user_msg = build_user_message(
            ticker=ticker,
            snapshot_row=snapshot_row,
            variable_detail_rows=variable_detail_rows or [],
            insider_data=insider_data,
            holdings_data=holdings_data,
        )
        response = _call_anthropic(
            client=client,
            model=model,
            system_blocks=system_blocks,
            user_message=user_msg,
        )
    except Exception as exc:
        logger.warning("Anthropic call failed for %s: %s", ticker, exc)
        return _fallback_narrative(ticker, snapshot_row, f"api_error: {exc}")

    text = _extract_text(response)
    if not text:
        return _fallback_narrative(ticker, snapshot_row, "empty_response")

    usage = _extract_usage(response)
    return {
        "ticker": ticker,
        "narrative": text,
        "model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "generated_at": _now_iso(),
        "fallback": False,
        "confidence_level": snapshot_row.get("confidence_level", "unknown"),
    }


def generate_universe_narratives(
    snapshot_rows: List[Dict[str, Any]],
    variable_detail_by_ticker: Dict[str, List[Dict[str, Any]]],
    insider_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    holdings_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    macro_breadth: Optional[Dict[str, Any]] = None,
    model: str = DEFAULT_MODEL,
    max_concurrency: int = 5,
    client: Any = None,
) -> Dict[str, Dict[str, Any]]:
    """Generate narratives for an entire universe in parallel.

    Falls back per-ticker if any single call fails. Returns a dict
    keyed by ticker. Concurrency is bounded by ``max_concurrency``.

    If the SDK or API key are unavailable, every ticker gets the
    templated fallback -- the function never raises.
    """
    insider_by_ticker = insider_by_ticker or {}
    holdings_by_ticker = holdings_by_ticker or {}
    variable_detail_by_ticker = variable_detail_by_ticker or {}

    # Build a single client up-front so we don't repeat env checks per ticker.
    # On failure, we still proceed -- each generate_llm_narrative call will
    # short-circuit to fallback.
    if client is None:
        api_key = _api_key()
        anthropic = _import_anthropic() if api_key else None
        if api_key and anthropic is not None:
            try:
                client = anthropic.Anthropic(api_key=api_key)
            except Exception as exc:
                logger.warning("Anthropic client construction failed: %s", exc)
                client = None

    results: Dict[str, Dict[str, Any]] = {}

    def _one(row: Dict[str, Any]) -> Dict[str, Any]:
        ticker = row.get("ticker") or "?"
        return generate_llm_narrative(
            ticker=ticker,
            snapshot_row=row,
            variable_detail_rows=variable_detail_by_ticker.get(ticker, []),
            insider_data=insider_by_ticker.get(ticker),
            holdings_data=holdings_by_ticker.get(ticker),
            macro_breadth=macro_breadth,
            model=model,
            client=client,
        )

    if not snapshot_rows:
        return results

    workers = max(1, int(max_concurrency))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_ticker = {ex.submit(_one, row): row.get("ticker", "?") for row in snapshot_rows}
        for fut in concurrent.futures.as_completed(future_to_ticker):
            ticker = future_to_ticker[fut]
            try:
                results[ticker] = fut.result()
            except Exception as exc:
                logger.warning("Universe narrative task crashed for %s: %s", ticker, exc)
                # Find the original row to feed into fallback.
                row = next((r for r in snapshot_rows if r.get("ticker") == ticker), {"ticker": ticker})
                results[ticker] = _fallback_narrative(ticker, row, f"task_crash: {exc}")
    return results
