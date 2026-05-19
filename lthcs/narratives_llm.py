"""LTHCS Claude-derived narrative generator (SHADOW run).

This module is a SHADOW alternative to the templated narratives in
:mod:`lthcs.narratives`. The templated path is the production source for
``data/lthcs/narratives/<calc_date>.json`` and the V1 detail-modal UI.
This module writes to a parallel directory
``data/lthcs/narratives_llm/<calc_date>.json`` so the UI can flip
between them via a localStorage toggle without disturbing the production
file.

Per ``docs/lthcs-llm-narratives-spec.md`` this module is wired as a
SHADOW: writes flow into ``data/lthcs/narratives_llm/`` (NOT
``data/lthcs/narratives/``, which is owned by Stage 7's templated
generator) and onto a per-ticker rolling history under
``data/lthcs/narratives_llm_by_ticker/``. Production narratives
(:func:`lthcs.narratives.generate_narratives`) are byte-untouched.

Promotion gate (spec §6): LLM narratives are a UX improvement, not a
scoring change. No IC test gates promotion -- the decision is
qualitative. A localStorage toggle in the About modal flips the UI
source; default off. After ~30 days of opt-in feedback the default may
flip on (or hold indefinitely as opt-in).

Design goals (mirrors ``lthcs.sources.llm_sentiment``)
-----------------------------------------------------

* Cheap. The long system prompt (LTHCS framework + four-section output
  schema + style guide) is cached via ``cache_control: ephemeral`` so
  the ~1.4k system tokens are reused across all 167 tickers in one
  run. Haiku 4.5 + caching is ~$0.31/day for the universe (cost model
  in spec §5).
* Capped. ``LTHCS_LLM_NARRATIVES_MAX_USD_PER_DAY`` (default ``2.00``)
  aborts shadow persistence cleanly if a run exceeds the budget.
  Production templated path is unaffected -- the shadow file simply
  isn't written.
* Resilient. 429 / 5xx errors are retried with exponential backoff
  (1s / 4s / 16s) before falling through to the templated fallback.
  Missing ``ANTHROPIC_API_KEY``, missing ``anthropic`` SDK, bad JSON
  also fall back per-ticker. The daily pipeline never crashes.
* Opt-in. Nothing here runs unless ``LTHCS_LLM_NARRATIVES_ENABLED=1``
  is set. ``lthcs_daily.py`` Stage 7.5b is the only wire-up site.
  ``LTHCS_NARRATIVES_LLM_ENABLED`` (the old name) is honored for one
  release with a DeprecationWarning.

Public surface
--------------

* :func:`generate_llm_narrative` -- single ticker
* :func:`generate_universe_narratives` -- whole-universe batch helper
* :func:`score_universe` -- gated, persisted shadow entrypoint
  (returns ``None`` when the env flag is off; safe to call
  unconditionally from the pipeline)
* :func:`is_enabled` -- env-flag check helper

All entry points always return dicts in the four-section shape
(``todays_take``, ``why_changed``, ``why_not_to_sell``,
``what_would_break``, ``confidence_level``) so the UI is a one-line
path branch from the templated file, not a two-shape adapter.
Successful LLM calls and fallbacks are interchangeable from a caller's
perspective.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import hashlib
import json
import logging
import os
import random
import re
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import narratives as _templated

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default model is Haiku 4.5 -- spec §5. Cheap, fast, sufficient for a
# constrained four-section JSON generation task. Override via env.
DEFAULT_MODEL = "claude-haiku-4-5"
ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_ENABLED = "LTHCS_LLM_NARRATIVES_ENABLED"
# Old env-flag name kept as a synonym for one release. Logs a
# DeprecationWarning when only the legacy name is set.
ENV_ENABLED_LEGACY = "LTHCS_NARRATIVES_LLM_ENABLED"
ENV_MODEL = "LTHCS_LLM_NARRATIVES_MODEL"
ENV_MODEL_LEGACY = "LTHCS_NARRATIVES_LLM_MODEL"
ENV_MAX_USD_PER_DAY = "LTHCS_LLM_NARRATIVES_MAX_USD_PER_DAY"

# Default daily-run cost cap in USD. Spec §5 estimates ~$0.31/day for a
# 167-ticker Haiku run with caching; cap is ~6x to leave headroom.
DEFAULT_MAX_USD_PER_DAY = 2.0

# Default retry parameters for 429 / 5xx errors. Spec §7.
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_S = (1.0, 4.0, 16.0)

# Per-million-token pricing in USD. Indexed by Anthropic model id.
# Kept locally (Option B in spec §5) -- a shared helper extraction is
# tracked in the open-items audit. Update this table on Haiku 4.6.
MODEL_PRICING_PER_MTOK: Dict[str, Dict[str, float]] = {
    "claude-haiku-4-5": {
        "input": 1.00,
        "cached_input": 0.10,
        "output": 5.00,
    },
    "claude-sonnet-4-5": {
        "input": 3.00,
        "cached_input": 0.30,
        "output": 15.00,
    },
    "claude-opus-4-5": {
        "input": 15.00,
        "cached_input": 1.50,
        "output": 75.00,
    },
}

# Persistence layout. Templated narratives live under data/lthcs/
# narratives/ -- DO NOT write there from the shadow path.
_DEFAULT_DATA_ROOT = Path("data") / "lthcs"
SHADOW_DAILY_DIRNAME = "narratives_llm"
SHADOW_BY_TICKER_DIRNAME = "narratives_llm_by_ticker"
SHADOW_TICKER_HISTORY_LIMIT = 60

# Anthropic prompt caching beta header (harmless on newer SDKs).
PROMPT_CACHING_BETA_HEADER = "prompt-caching-2024-07-31"

# Conservative output budget. Four ~2-3 sentence sections plus a
# confidence label fits comfortably under 600 tokens.
MAX_OUTPUT_TOKENS = 600

# The four-section keys the LLM is expected to emit, mirroring V1's
# templated shape. Open-question #1 in the spec resolves to V1 keys.
NARRATIVE_SECTION_KEYS = (
    "todays_take",
    "why_changed",
    "why_not_to_sell",
    "what_would_break",
)
VALID_CONFIDENCE_LEVELS = {"high", "medium", "low"}


# Long, cache-friendly system prompt. Keep this stable across runs --
# any churn here invalidates the prompt cache for the day.
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

# Output format

Return EXACTLY this JSON (no prose, no markdown fence, no preamble):

{
  "todays_take": <2-3 sentences: composite read (band + score + drift + supporting pillar)>,
  "why_changed": <2-3 sentences: what changed since yesterday — cite specific component deltas; if prior-day data is not provided, summarize today's drift>,
  "why_not_to_sell": <2-3 sentences: bottom-pillar (binding) callout — cite specific component values and data_quality gaps>,
  "what_would_break": <2-3 sentences: what would break the thesis — cite the next band-down threshold and the structural-review threshold>,
  "confidence_level": <one of: "high", "medium", "low">
}

# Style guide

- 2-3 sentences per section. No bullets, no headings inside the strings.
- Identify the BINDING pillar (the lowest one currently dragging the composite) and the SUPPORTING pillar (the highest one anchoring it). Frame the score as the resolution of those two forces.
- Reference specific numbers and ``data_quality`` flags. Don't use generic adjectives like "strong" or "weak" without grounding them in a metric.
- The macro overlay is context, not a driver. Mention it only when it materially changes the picture.
- Factual, analytical, no hype. American English. No emojis, no exclamations. Match the tone of an analyst desk note. No hedging filler ("it appears that...", "one could argue...")."""


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
    # New name wins; legacy honored for one release with a warning.
    new = os.environ.get(ENV_MODEL, "").strip()
    if new:
        return new
    legacy = os.environ.get(ENV_MODEL_LEGACY, "").strip()
    if legacy:
        warnings.warn(
            "%s is deprecated; use %s instead." % (ENV_MODEL_LEGACY, ENV_MODEL),
            DeprecationWarning,
            stacklevel=2,
        )
        return legacy
    return default


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _data_quality_by_pillar(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        pillar = row.get("pillar")
        if not pillar:
            continue
        out[pillar] = dict(row.get("data_quality") or {})
    return out


def build_system_blocks(macro_breadth: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Construct the cached system prompt.

    Returns a list of system blocks with ``cache_control`` on each cacheable
    segment. Anthropic's prompt caching keys on prefix identity, so the
    macro block is appended *after* the framework prompt and gets its own
    cache_control marker so it can be reused across all 167 tickers on
    the same day.
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
    prior_snapshot_row: Optional[Dict[str, Any]] = None,
) -> str:
    """Build the per-ticker user prompt (NOT cached -- changes every call).

    Spec §4 calls for the prior-day composite + subscores in the payload
    so the LLM can write the ``why_changed`` section honestly. When
    ``prior_snapshot_row`` is None we pass an explicit
    ``prior_day: {available: False}`` marker so the model knows to lean
    on today's ``drift_*`` fields instead.
    """
    subs = dict(snapshot_row.get("subscores") or {})
    bs = _binding_and_supporting(subs)
    prior_block: Dict[str, Any]
    if prior_snapshot_row:
        prior_subs = dict(prior_snapshot_row.get("subscores") or {})
        prior_block = {
            "available": True,
            "lthcs_score": prior_snapshot_row.get("lthcs_score"),
            "band": prior_snapshot_row.get("band"),
            "subscores": {k: round(float(v), 1) for k, v in prior_subs.items()},
        }
    else:
        prior_block = {"available": False}
    payload: Dict[str, Any] = {
        "ticker": ticker,
        "sector": snapshot_row.get("sector"),
        "lthcs_score": snapshot_row.get("lthcs_score"),
        "band": snapshot_row.get("band"),
        "drift_1d": snapshot_row.get("drift_1d"),
        "drift_7d": snapshot_row.get("drift_7d"),
        "drift_30d": snapshot_row.get("drift_30d"),
        "confidence_level": snapshot_row.get("confidence_level"),
        "subscores": {k: round(float(v), 1) for k, v in subs.items()},
        "binding_and_supporting": bs,
        "pillar_components": _summarize_variable_detail(variable_detail_rows),
        "data_quality_by_pillar": _data_quality_by_pillar(variable_detail_rows),
        "insider": _summarize_insider(insider_data),
        "holdings": _summarize_holdings(holdings_data),
        "prior_day": prior_block,
    }
    return (
        "Write one four-section LTHCS narrative for the ticker below following the system "
        "style guide and the exact JSON schema. Return JSON only — no prose, no preamble, "
        "no markdown fences.\n\n"
        + json.dumps(payload, indent=2, sort_keys=True, default=str)
    )


def _prompt_hash(user_message: str, model: str) -> str:
    """SHA-256 the (model, user_message) tuple for idempotency checks."""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x1f")
    h.update(user_message.encode("utf-8"))
    return h.hexdigest()


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
    """Single API call wrapping the SDK; isolated for easy mocking.

    No retry here -- :func:`_call_anthropic_with_retry` wraps this and
    handles 429/5xx with exponential backoff per spec §7.
    """
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


def _is_retryable_error(exc: BaseException) -> bool:
    """Return True for 429 / 5xx / connection errors that warrant a retry.

    Detects the anthropic SDK's typed errors structurally so this
    module's tests don't need the real SDK installed. Anything else is
    treated as fatal (caller falls back).
    """
    name = type(exc).__name__
    if name in {
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "APIResponseValidationError",
        "InternalServerError",
        "ServiceUnavailableError",
    }:
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    return False


def _call_anthropic_with_retry(
    *,
    client,
    model: str,
    system_blocks: List[Dict[str, Any]],
    user_message: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    attempts: int = DEFAULT_RETRY_ATTEMPTS,
    backoff_s: Tuple[float, ...] = DEFAULT_RETRY_BACKOFF_S,
    sleep_fn=time.sleep,
):
    """Call Anthropic, retry on rate-limit / 5xx with exponential backoff.

    Spec §7: 3 attempts at 1s / 4s / 16s with light jitter; anything
    that isn't a retryable error raises immediately so the caller can
    drop to the templated fallback.
    """
    last_exc: Optional[BaseException] = None
    n = max(1, int(attempts))
    for i in range(n):
        try:
            return _call_anthropic(
                client=client,
                model=model,
                system_blocks=system_blocks,
                user_message=user_message,
                max_tokens=max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 -- structural check below
            last_exc = exc
            if i >= n - 1 or not _is_retryable_error(exc):
                raise
            delay_base = backoff_s[i] if i < len(backoff_s) else backoff_s[-1]
            jitter = 1.0 + random.uniform(-0.1, 0.1)
            sleep_fn(max(0.0, delay_base * jitter))
    if last_exc is not None:  # pragma: no cover
        raise last_exc
    raise RuntimeError("unreachable: retry loop exited without result")


def _extract_text(response: Any) -> str:
    """Pull the assistant text out of an Anthropic Message response."""
    try:
        content = getattr(response, "content", None) or []
        parts: List[str] = []
        for block in content:
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
        "cached_input_tokens": _g("cache_read_input_tokens"),
        "cache_creation_input_tokens": _g("cache_creation_input_tokens"),
    }


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_json_envelope(raw_text: str) -> Optional[Dict[str, Any]]:
    """Extract the first JSON object from the model output.

    Models occasionally wrap JSON in a markdown fence even when asked
    not to. The greedy curly-brace match handles both fenced and bare
    JSON. Returns None if no object parses.
    """
    if not raw_text:
        return None
    text = raw_text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    fence_stripped = re.sub(r"^```(?:json)?\s*", "", text)
    fence_stripped = re.sub(r"\s*```\s*$", "", fence_stripped)
    try:
        parsed = json.loads(fence_stripped)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    match = _JSON_OBJ_RE.search(text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            return None
    return None


def _parse_four_section_json(raw_text: str) -> Optional[Dict[str, Any]]:
    """Parse the LLM output into the canonical four-section shape.

    Returns None if any required section is missing or the JSON
    envelope can't be located. Caller treats None as a parse failure
    and falls back to the templated narrative.

    Tolerates two key conventions:

    - V1 keys: ``todays_take``, ``why_changed``, ``why_not_to_sell``,
      ``what_would_break``.
    - Spec keys: ``section_1_todays_take`` etc. -- coerced to V1.
    """
    parsed = _parse_json_envelope(raw_text)
    if not isinstance(parsed, dict):
        return None

    # Spec-key fallback aliasing.
    alias = {
        "section_1_todays_take": "todays_take",
        "section_2_why_changed": "why_changed",
        "section_3_why_not_to_sell": "why_not_to_sell",
        "section_4_what_would_break": "what_would_break",
    }
    for spec_key, v1_key in alias.items():
        if v1_key not in parsed and spec_key in parsed:
            parsed[v1_key] = parsed[spec_key]

    out: Dict[str, Any] = {}
    for key in NARRATIVE_SECTION_KEYS:
        val = parsed.get(key)
        if not isinstance(val, str) or not val.strip():
            return None
        out[key] = val.strip()

    confidence = str(parsed.get("confidence_level") or "").strip().lower()
    if confidence not in VALID_CONFIDENCE_LEVELS:
        confidence = "medium"
    out["confidence_level"] = confidence
    return out


# ---------------------------------------------------------------------------
# Fallback helper
# ---------------------------------------------------------------------------


def _fallback_narrative(
    ticker: str,
    snapshot_row: Dict[str, Any],
    reason: str,
    *,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the templated narrative and stamp it with fallback metadata."""
    try:
        templated = _templated.generate_narratives(snapshot_row)
    except Exception:  # pragma: no cover - templated is well-tested
        templated = {
            "todays_take": "",
            "why_changed": "",
            "why_not_to_sell": "",
            "what_would_break": "",
            "confidence_level": "unknown",
        }
    return {
        "ticker": ticker,
        "todays_take": templated.get("todays_take", ""),
        "why_changed": templated.get("why_changed", ""),
        "why_not_to_sell": templated.get("why_not_to_sell", ""),
        "what_would_break": templated.get("what_would_break", ""),
        "confidence_level": templated.get("confidence_level", "unknown"),
        "model": model,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "generated_at": _now_iso(),
        "fallback": True,
        "fallback_reason": reason,
    }


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
    prior_snapshot_row: Optional[Dict[str, Any]] = None,
    model: str = DEFAULT_MODEL,
    use_cache: bool = True,
    client: Any = None,
) -> Dict[str, Any]:
    """Generate one LLM-backed four-section narrative; fall back on any failure.

    Parameters mirror the per-ticker inputs available in the daily
    pipeline. ``prior_snapshot_row`` is optional; when provided it
    enables a more honest ``why_changed`` section (spec §4).

    The ``client`` parameter is intended for testing -- pass a mock
    Anthropic client to avoid network calls. In production, leave it
    None and the function will construct one from ``ANTHROPIC_API_KEY``.
    """
    ticker = (ticker or "").upper().strip() or "?"

    if client is None:
        api_key = _api_key()
        if not api_key:
            return _fallback_narrative(ticker, snapshot_row, "missing_api_key", model=None)
        anthropic = _import_anthropic()
        if anthropic is None:
            return _fallback_narrative(
                ticker, snapshot_row, "anthropic_sdk_unavailable", model=None
            )
        try:
            client = anthropic.Anthropic(api_key=api_key)
        except Exception as exc:
            logger.warning("Anthropic client construction failed: %s", exc)
            return _fallback_narrative(
                ticker, snapshot_row, "client_init_failed", model=None
            )

    try:
        system_blocks = build_system_blocks(macro_breadth if use_cache else None)
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
            prior_snapshot_row=prior_snapshot_row,
        )
        response = _call_anthropic_with_retry(
            client=client,
            model=model,
            system_blocks=system_blocks,
            user_message=user_msg,
        )
    except Exception as exc:
        logger.warning("Anthropic call failed for %s: %s", ticker, exc)
        return _fallback_narrative(
            ticker, snapshot_row, "api_error: %s" % exc, model=model
        )

    raw_text = _extract_text(response)
    if not raw_text:
        return _fallback_narrative(ticker, snapshot_row, "empty_response", model=model)

    parsed = _parse_four_section_json(raw_text)
    if parsed is None:
        return _fallback_narrative(ticker, snapshot_row, "json_parse_error", model=model)

    usage = _extract_usage(response)
    return {
        "ticker": ticker,
        "todays_take": parsed["todays_take"],
        "why_changed": parsed["why_changed"],
        "why_not_to_sell": parsed["why_not_to_sell"],
        "what_would_break": parsed["what_would_break"],
        "confidence_level": parsed["confidence_level"],
        "model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "generated_at": _now_iso(),
        "fallback": False,
        "fallback_reason": None,
        "raw_response": raw_text,
    }


def generate_universe_narratives(
    snapshot_rows: List[Dict[str, Any]],
    variable_detail_by_ticker: Dict[str, List[Dict[str, Any]]],
    insider_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    holdings_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    macro_breadth: Optional[Dict[str, Any]] = None,
    prior_snapshot_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
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
    prior_snapshot_by_ticker = prior_snapshot_by_ticker or {}

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
            prior_snapshot_row=prior_snapshot_by_ticker.get(ticker),
            model=model,
            client=client,
        )

    if not snapshot_rows:
        return results

    workers = max(1, int(max_concurrency))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_ticker = {
            ex.submit(_one, row): row.get("ticker", "?") for row in snapshot_rows
        }
        for fut in concurrent.futures.as_completed(future_to_ticker):
            ticker = future_to_ticker[fut]
            try:
                results[ticker] = fut.result()
            except Exception as exc:
                logger.warning("Universe narrative task crashed for %s: %s", ticker, exc)
                row = next(
                    (r for r in snapshot_rows if r.get("ticker") == ticker),
                    {"ticker": ticker},
                )
                results[ticker] = _fallback_narrative(
                    ticker, row, "task_crash: %s" % exc, model=model
                )
    return results


# ---------------------------------------------------------------------------
# Cost cap helper (spec §5)
# ---------------------------------------------------------------------------


def _estimate_cost_usd(usage_dicts: List[Dict[str, Any]], model: str) -> float:
    """Sum per-call token usage and apply per-model pricing.

    ``usage_dicts`` is a list of the per-ticker output dicts returned by
    :func:`generate_llm_narrative` (or their ``usage`` sub-dicts; both
    shapes are accepted). Unknown models default to Haiku pricing
    (cheaper -- biased to NOT trip the cap on an unmapped model id).
    """
    pricing = MODEL_PRICING_PER_MTOK.get(model) or MODEL_PRICING_PER_MTOK[DEFAULT_MODEL]
    input_tok = 0
    cached_tok = 0
    output_tok = 0
    for u in usage_dicts or []:
        if not isinstance(u, dict):
            continue
        u2 = u.get("usage") if "usage" in u and isinstance(u["usage"], dict) else u
        input_tok += int(u2.get("input_tokens") or 0)
        cached_tok += int(u2.get("cached_input_tokens") or 0)
        output_tok += int(u2.get("output_tokens") or 0)
    cost = (
        input_tok * pricing["input"] / 1_000_000.0
        + cached_tok * pricing["cached_input"] / 1_000_000.0
        + output_tok * pricing["output"] / 1_000_000.0
    )
    return round(cost, 6)


def _max_usd_per_day() -> float:
    raw = os.environ.get(ENV_MAX_USD_PER_DAY, "").strip()
    if not raw:
        return DEFAULT_MAX_USD_PER_DAY
    try:
        val = float(raw)
        if val <= 0:
            return DEFAULT_MAX_USD_PER_DAY
        return val
    except (TypeError, ValueError):
        return DEFAULT_MAX_USD_PER_DAY


def is_enabled() -> bool:
    """Return True iff the shadow is enabled via env flag.

    New name ``LTHCS_LLM_NARRATIVES_ENABLED=1`` is preferred; the legacy
    ``LTHCS_NARRATIVES_LLM_ENABLED=1`` is honored for one release with a
    DeprecationWarning. Default is OFF.
    """
    new = os.environ.get(ENV_ENABLED, "").strip()
    if new == "1":
        return True
    if new and new != "1":
        return False
    legacy = os.environ.get(ENV_ENABLED_LEGACY, "").strip()
    if legacy == "1":
        warnings.warn(
            "%s is deprecated; use %s instead."
            % (ENV_ENABLED_LEGACY, ENV_ENABLED),
            DeprecationWarning,
            stacklevel=2,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Shadow persistence (spec §3)
# ---------------------------------------------------------------------------


def _shadow_daily_path(calc_date: str, data_root: Optional[Path] = None) -> Path:
    root = Path(data_root) if data_root else _DEFAULT_DATA_ROOT
    return root / SHADOW_DAILY_DIRNAME / f"{calc_date}.json"


def _shadow_ticker_path(ticker: str, data_root: Optional[Path] = None) -> Path:
    root = Path(data_root) if data_root else _DEFAULT_DATA_ROOT
    return root / SHADOW_BY_TICKER_DIRNAME / f"{ticker.upper()}.json"


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str))
    tmp.replace(path)


def write_shadow_daily(
    calc_date: str,
    rows: List[Dict[str, Any]],
    *,
    data_root: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write the per-day aggregate ``data/lthcs/narratives_llm/<date>.json``.

    Format matches the templated ``narratives/<date>.json`` shape
    ``{calc_date, model_version, narratives: [...]}`` so the UI can
    swap sources with a one-line branch. ``extra`` carries run-level
    metadata stamped into the file under ``meta`` for ops visibility.
    """
    path = _shadow_daily_path(calc_date, data_root=data_root)
    meta = dict(extra or {})
    payload = {
        "calc_date": calc_date,
        "generated_at": _now_iso(),
        "model_version": meta.get("model") or "",
        "meta": meta,
        "narratives": list(rows or []),
    }
    _atomic_write_json(path, payload)
    return path


def append_shadow_ticker_history(
    ticker: str,
    record: Dict[str, Any],
    *,
    data_root: Optional[Path] = None,
    history_limit: int = SHADOW_TICKER_HISTORY_LIMIT,
) -> Path:
    """Append today's record to ``data/lthcs/narratives_llm_by_ticker/<T>.json``.

    Rolling history capped at ``history_limit`` entries (newest last).
    Duplicate same-day entries replace the existing tail entry so a
    ``--force`` re-run doesn't double-append.
    """
    path = _shadow_ticker_path(ticker, data_root=data_root)
    history: List[Dict[str, Any]] = []
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if isinstance(existing, list):
                history = existing
        except (OSError, ValueError):
            history = []
    today = record.get("calc_date") or record.get("generated_at", "")[:10]
    if history and history[-1].get("calc_date") == today:
        history[-1] = record
    else:
        history.append(record)
    if len(history) > history_limit:
        history = history[-history_limit:]
    _atomic_write_json(path, history)
    return path


# ---------------------------------------------------------------------------
# Gated shadow entrypoint (spec §3 / §5 / §7)
# ---------------------------------------------------------------------------


def score_universe(
    snapshot_rows: List[Dict[str, Any]],
    variable_detail_by_ticker: Dict[str, List[Dict[str, Any]]],
    *,
    calc_date: str,
    insider_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    holdings_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    macro_breadth: Optional[Dict[str, Any]] = None,
    prior_snapshot_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    model: Optional[str] = None,
    max_concurrency: int = 5,
    client: Any = None,
    data_root: Optional[Path] = None,
    persist: bool = True,
    cost_cap_usd: Optional[float] = None,
    shadow_run_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Gated shadow entrypoint -- called from ``lthcs_daily.py`` Stage 7.5b.

    Returns ``None`` (no-op) when ``LTHCS_LLM_NARRATIVES_ENABLED`` is
    not ``"1"``. Otherwise:

    1. Run :func:`generate_universe_narratives` across the snapshot.
    2. Estimate cost from the response usage objects.
    3. If cost exceeds ``LTHCS_LLM_NARRATIVES_MAX_USD_PER_DAY`` (default
       $2.00), log and SKIP persistence -- the prior day's shadow file
       is the last good record.
    4. Else (when ``persist=True``) write
       ``data/lthcs/narratives_llm/<calc_date>.json`` and append
       ``data/lthcs/narratives_llm_by_ticker/<T>.json`` per ticker.

    Returns a dict with keys ``{"results", "meta", "persisted"}``.
    ``meta`` carries ``model``, ``total_cost_usd``, ``cost_cap_usd``,
    ``cost_cap_hit``, ``ticker_count``, ``fallback_count``,
    ``shadow_run_id``.

    NEVER touches ``data/lthcs/narratives/`` (the production templated
    narratives file). Spec §7.
    """
    if not is_enabled():
        return None

    model = (model or _model_from_env()).strip() or DEFAULT_MODEL
    cap = float(cost_cap_usd) if cost_cap_usd is not None else _max_usd_per_day()
    run_id = shadow_run_id or _now_iso()

    results = generate_universe_narratives(
        snapshot_rows=snapshot_rows or [],
        variable_detail_by_ticker=variable_detail_by_ticker or {},
        insider_by_ticker=insider_by_ticker,
        holdings_by_ticker=holdings_by_ticker,
        macro_breadth=macro_breadth,
        prior_snapshot_by_ticker=prior_snapshot_by_ticker,
        model=model,
        max_concurrency=max_concurrency,
        client=client,
    )

    total_cost = _estimate_cost_usd(list(results.values()), model)
    fallback_count = sum(1 for r in results.values() if r.get("fallback"))
    cost_cap_hit = total_cost > cap

    meta = {
        "model": model,
        "total_cost_usd": total_cost,
        "cost_cap_usd": cap,
        "cost_cap_hit": cost_cap_hit,
        "ticker_count": len(results),
        "fallback_count": fallback_count,
        "shadow_run_id": run_id,
    }

    if cost_cap_hit:
        logger.warning(
            "! Stage 7.5b: LLM narrative cost cap hit ($%.4f > $%.2f); "
            "skipping shadow persistence.",
            total_cost,
            cap,
        )
        return {"results": results, "meta": meta, "persisted": False}

    if not persist:
        return {"results": results, "meta": meta, "persisted": False}

    # Stamp each per-ticker record with the calc_date and shadow_run_id
    # for downstream history-file dedupe.
    for sym, rec in results.items():
        rec.setdefault("calc_date", calc_date)
        rec.setdefault("shadow_run_id", run_id)

    # The shadow daily file mirrors the templated narratives shape (a
    # list under "narratives"), so deterministically order by ticker
    # before writing.
    ordered_rows = [results[sym] for sym in sorted(results.keys())]

    try:
        write_shadow_daily(
            calc_date,
            ordered_rows,
            data_root=data_root,
            extra={
                "model": model,
                "total_cost_usd": total_cost,
                "ticker_count": len(results),
                "fallback_count": fallback_count,
                "shadow_run_id": run_id,
            },
        )
        for sym, rec in results.items():
            try:
                append_shadow_ticker_history(sym, rec, data_root=data_root)
            except Exception as exc:  # pragma: no cover - filesystem-edge
                logger.warning("shadow history write failed for %s: %s", sym, exc)
    except Exception as exc:
        logger.warning("shadow daily write failed: %s", exc)
        return {"results": results, "meta": meta, "persisted": False}

    return {"results": results, "meta": meta, "persisted": True}


__all__ = [
    "DEFAULT_MAX_USD_PER_DAY",
    "DEFAULT_MODEL",
    "ENV_API_KEY",
    "ENV_ENABLED",
    "ENV_ENABLED_LEGACY",
    "ENV_MAX_USD_PER_DAY",
    "ENV_MODEL",
    "ENV_MODEL_LEGACY",
    "MODEL_PRICING_PER_MTOK",
    "NARRATIVE_SECTION_KEYS",
    "SHADOW_BY_TICKER_DIRNAME",
    "SHADOW_DAILY_DIRNAME",
    "SHADOW_TICKER_HISTORY_LIMIT",
    "SYSTEM_PROMPT",
    "VALID_CONFIDENCE_LEVELS",
    "append_shadow_ticker_history",
    "build_system_blocks",
    "build_user_message",
    "generate_llm_narrative",
    "generate_universe_narratives",
    "is_enabled",
    "score_universe",
    "write_shadow_daily",
]
