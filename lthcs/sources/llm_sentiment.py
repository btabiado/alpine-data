"""LTHCS Claude-derived news sentiment for the Thesis pillar (SHADOW run).

This module is a SHADOW alternative to the engagement-tier heuristic in
:func:`lthcs.sources.ai_news.compute_thesis_signal_from_news`. The
heuristic infers sentiment from mention count + HN-points/comments,
which is a proxy for "how loud is this ticker in the AI news cycle" --
not direction.

Here we ask Claude (default ``claude-haiku-4-5``) to read 3-10 actual
headlines/snippets per ticker and return a structured JSON
classification: a continuous sentiment score in [-1, +1], a discrete
label, confidence, key drivers, and key risks.

Per ``docs/lthcs-llm-sentiment-shadow-spec.md`` this module is wired as
a SHADOW: writes flow into ``data/lthcs/llm_sentiment/`` (NOT
``data/lthcs/sentiment/``, which is the Finnhub/AV-driven Thesis rotation
cache) and onto ``components.llm_sentiment_shadow_*`` fields on
variable_detail rows. Production Thesis math
(:func:`lthcs.pillars.thesis.compute_thesis_with_refinement`) is byte-
untouched.

Promotion gate (spec §5; eval lives in
``scripts/lthcs_compare_llm_sentiment.py``, NOT this PR):
LLM 21d-forward Spearman IC > Finnhub IC + 0.03 AND LLM IC t-stat > 2.0
AND fallback rate < 10% over a 30 trading-day window -- only then is
``weights.json:thesis.use_llm_primary`` flipped true in a follow-up
commit.

Design goals (mirrors ``lthcs.narratives_llm``)
-----------------------------------------------

* Cheap. The long system prompt (LTHCS context + sentiment scale +
  output schema) is cached via ``cache_control: ephemeral`` so the
  ~1.1k system tokens are reused across all 167 tickers in one run.
  Haiku 4.5 + caching is ~$0.19/day for the universe (cost model in
  spec §4).
* Capped. ``LTHCS_LLM_SENTIMENT_MAX_USD_PER_DAY`` (default ``1.00``)
  aborts persistence cleanly if a run exceeds the budget. Production
  Thesis path is unaffected -- the shadow file simply isn't written.
* Resilient. 429 / 5xx errors are retried with exponential backoff
  (1s / 4s / 16s) before falling through to the engagement heuristic
  fallback. Missing ``ANTHROPIC_API_KEY``, missing ``anthropic`` SDK,
  unparseable JSON also fall back. The daily pipeline never crashes.
* Opt-in. Nothing here runs unless ``LTHCS_LLM_SENTIMENT_ENABLED=1``
  is set. ``lthcs_daily.py`` Stage 2/4 is the only wire-up site.

Public surface:

* :func:`compute_llm_sentiment` -- single ticker
* :func:`compute_universe_llm_sentiment` -- whole-universe batch helper
* :func:`score_universe` -- gated, persisted shadow entrypoint
  (returns ``None`` when the env flag is off; safe to call
  unconditionally from the pipeline)
* :func:`is_enabled` -- env-flag check helper

All entry points always return dicts with the same shape (see
:func:`compute_llm_sentiment` docstring) -- successful LLM calls and
fallbacks are interchangeable from a caller's perspective.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import ai_news as _ai_news

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default model is Haiku 4.5 -- cheap, fast, sufficient for a structured
# classification task with a fixed JSON schema. Override at the env level
# via LTHCS_LLM_SENTIMENT_MODEL.
DEFAULT_MODEL = "claude-haiku-4-5"
ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_ENABLED = "LTHCS_LLM_SENTIMENT_ENABLED"
ENV_MODEL = "LTHCS_LLM_SENTIMENT_MODEL"
ENV_MAX_USD_PER_DAY = "LTHCS_LLM_SENTIMENT_MAX_USD_PER_DAY"

# Default daily-run cost cap in USD. Spec §4 estimates ~$0.19/day for a
# 167-ticker Haiku run with caching; this leaves a 5x safety margin.
DEFAULT_MAX_USD_PER_DAY = 1.0

# Default retry parameters for 429 / 5xx errors. Spec §6.
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_S = (1.0, 4.0, 16.0)

# Per-million-token pricing in USD. Indexed by Anthropic model id. Cache
# reads are typically ~10% of input. Used by _estimate_cost_usd() to
# enforce the run-level cap.
MODEL_PRICING_PER_MTOK: Dict[str, Dict[str, float]] = {
    # Haiku 4.5 (October 2025) per Anthropic pricing page.
    "claude-haiku-4-5": {
        "input": 1.00,
        "cached_input": 0.10,
        "output": 5.00,
    },
    # Sonnet 4.5 (October 2025) -- retained for override testing.
    "claude-sonnet-4-5": {
        "input": 3.00,
        "cached_input": 0.30,
        "output": 15.00,
    },
    # Opus 4.5.
    "claude-opus-4-5": {
        "input": 15.00,
        "cached_input": 1.50,
        "output": 75.00,
    },
}

# Persistence layout. The Thesis rotation cache lives under data/lthcs/
# sentiment/ -- DO NOT write there from the shadow path; that's owned by
# state.rotation in lthcs_daily.py and is consumed by production Thesis.
_DEFAULT_DATA_ROOT = Path("data") / "lthcs"
SHADOW_DAILY_DIRNAME = "llm_sentiment"
SHADOW_BY_TICKER_DIRNAME = "llm_sentiment_by_ticker"
SHADOW_TICKER_HISTORY_LIMIT = 60

# Anthropic prompt caching beta header (harmless on newer SDKs).
PROMPT_CACHING_BETA_HEADER = "prompt-caching-2024-07-31"

# Generous enough for the JSON envelope including a few key_drivers/risks.
MAX_OUTPUT_TOKENS = 600

# How many news items to pass to the model per ticker. Beyond ~10 the
# signal dilutes and tokens balloon. The most engagement-weighted items
# go in; the rest are dropped.
DEFAULT_MAX_NEWS_ITEMS = 10

# Discrete-label thresholds on the continuous [-1, +1] score.
LABEL_THRESHOLDS = (
    ("extreme_bearish", -1.01, -0.7),
    ("bearish",        -0.7,  -0.3),
    ("neutral",        -0.3,   0.3),
    ("bullish",         0.3,   0.7),
    ("extreme_bullish", 0.7,   1.01),
)

# The five labels the model is allowed to return.
VALID_LABELS = {row[0] for row in LABEL_THRESHOLDS}


# Long, cache-friendly system prompt. Keep this stable across runs --
# any churn here invalidates the prompt cache for the day.
SYSTEM_PROMPT = """You are a financial-news sentiment classifier for the LTHCS (Long-Term Hold Confidence Score) framework.

# LTHCS context

LTHCS scores 168 US-listed equities 0-100 daily across five pillars:
1. Adoption Momentum (revenue + Google Trends slope)
2. Institutional Confidence (price momentum + 13F + insider Form 4)
3. Financial Evolution (FCF margin + ROIC + gross margin + OCF growth)
4. Thesis Integrity (qualitative moat + capital allocation + governance)
5. Demand Environment (sector ETF strength + macro overlay)

Your output feeds the Thesis Integrity pillar. The pillar is universe-percentile-ranked, so be calibrated -- if everyone gets +0.8 the signal collapses.

# Sentiment scale (continuous, [-1.0, +1.0])

-1.0  Catastrophic. Existential threat, fraud allegations, criminal investigation, going-concern doubt, mass executive resignations.
-0.7  Severe negative. Major earnings miss with guidance cut, large product recall, regulatory enforcement action, key-customer loss.
-0.4  Moderate negative. Single earnings miss, competitor wins a contested deal, executive departure, downgrade-cluster from sell-side.
-0.2  Mild negative. Soft sub-segment guidance, minor recall, mixed quarter with some misses.
 0.0  Neutral. Mixed news, routine product launches, no decisive directional signal.
+0.2  Mild positive. In-line quarter with positive guide on one segment, modest product win, minor analyst upgrade.
+0.4  Moderate positive. Clear earnings beat with raised guide, major new customer win, positive product reception, upgrade-cluster.
+0.7  Strong positive. Blowout quarter with structural step-up in TAM, multi-billion-dollar contract, breakthrough product/technology launch.
+1.0  Transformational. Industry-defining win (the "Nvidia data-center moment"), accretive acquisition with synergies, regulatory tailwind that changes the long-term P&L.

# Discrete labels (derived from the score; pick the one that matches your score)

- extreme_bearish: score in [-1.0, -0.7)
- bearish:        score in [-0.7, -0.3)
- neutral:        score in [-0.3, +0.3]
- bullish:        score in (+0.3, +0.7]
- extreme_bullish: score in (+0.7, +1.0]

# Calibration discipline

- Default to NEUTRAL. The neutral band (-0.3 to +0.3) is wide on purpose.
- Reserve |score| > 0.5 for clear, substantive directional signals with specific facts in the news items.
- Do NOT score on "hype" or "lots of mentions" alone -- that's the old heuristic this is replacing.
- Be factual and decisive. No hedging filler. Cite specific facts from the news items.
- If the news items are routine product launches, analyst day recaps, or generic AI-industry mentions, the answer is neutral.

# Output format

Return EXACTLY this JSON (no prose, no markdown fence):

{
  "mean_sentiment_score": <float in [-1.0, 1.0]>,
  "label": <one of: extreme_bullish, bullish, neutral, bearish, extreme_bearish>,
  "polarity_confidence": <float in [0.0, 1.0], your self-assessed confidence>,
  "key_drivers": [<up to 3 short strings citing specific facts from the news>],
  "key_risks": [<up to 3 short strings citing specific risks from the news>],
  "rationale": <one short sentence explaining the call>
}

The label MUST be consistent with the score band defined above. If you find no substantive directional signal, return score 0.0, label "neutral", and explain in rationale."""


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


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_system_blocks() -> List[Dict[str, Any]]:
    """Construct the cached system prompt.

    Returns a single text block with ``cache_control: ephemeral``. The
    system prompt is identical across all tickers in a run so the
    cache hit rate after the first call is ~100%.
    """
    return [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _coerce_news_item(raw: Any) -> Optional[Dict[str, Any]]:
    """Normalize one news item to the shape we put in the prompt.

    Accepts the dicts returned by :func:`lthcs.sources.ai_news.aggregate_ai_news`'s
    HN and RSS parsers, and the looser ``{title, summary/snippet, source,
    date}`` shape from other call sites.
    """
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    if not title:
        return None
    snippet = (
        raw.get("snippet")
        or raw.get("summary")
        or raw.get("description")
        or ""
    )
    snippet = str(snippet).strip()
    if len(snippet) > 400:
        snippet = snippet[:400].rstrip() + "..."
    source = (raw.get("source") or "").strip() or "unknown"
    date = (
        raw.get("date")
        or raw.get("time_published")
        or raw.get("published_at")
        or ""
    )
    return {
        "title": title,
        "snippet": snippet,
        "source": source,
        "date": str(date) if date else "",
    }


def _rank_key(item: Dict[str, Any]) -> tuple:
    """Sort key: HN points (desc), then date (desc)."""
    return (
        int(item.get("points") or 0),
        item.get("time_published") or item.get("date") or "",
    )


def build_user_message(
    ticker: str,
    news_items: List[Dict[str, Any]],
    max_news_items: int = DEFAULT_MAX_NEWS_ITEMS,
) -> str:
    """Build the per-ticker user prompt (NOT cached -- changes every call)."""
    # Rank by engagement before truncating so we keep the most informative
    # items. Original ai_news items have ``points`` and ``time_published``.
    ranked = sorted(news_items or [], key=_rank_key, reverse=True)
    coerced: List[Dict[str, Any]] = []
    for raw in ranked[: max(int(max_news_items), 1)]:
        item = _coerce_news_item(raw)
        if item is not None:
            coerced.append(item)
    payload: Dict[str, Any] = {
        "ticker": ticker.upper(),
        "news_items": coerced,
        "item_count": len(coerced),
    }
    return (
        "Classify the news below for the ticker. Return JSON only -- "
        "no prose, no preamble, no markdown fences.\n\n"
        + json.dumps(payload, indent=2, sort_keys=True, default=str)
    )


# ---------------------------------------------------------------------------
# Anthropic call + response parsing
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
    handles 429/5xx with exponential backoff per spec §6.
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
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user_message}],
        )


def _is_retryable_error(exc: BaseException) -> bool:
    """Return True for 429 / 5xx / connection errors that warrant a retry.

    The anthropic SDK raises typed errors (``RateLimitError``,
    ``APIStatusError``, ``APIConnectionError``); we detect them
    structurally so this module's tests don't need the real SDK
    installed. Anything else is treated as fatal (caller falls back).
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
    # Generic httpx-style: look for status_code attribute and check 429/5xx.
    status = getattr(exc, "status_code", None)
    if status is None:
        # Some SDK errors stash the status on response.status_code.
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

    Spec §6: 3 attempts at 1s / 4s / 16s with light jitter; anything
    that isn't a retryable error raises immediately so the caller can
    drop to the engagement-heuristic fallback.
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
            # Picks the i-th backoff or the last entry if attempts > len(backoff_s).
            delay_base = backoff_s[i] if i < len(backoff_s) else backoff_s[-1]
            jitter = 1.0 + random.uniform(-0.1, 0.1)
            sleep_fn(max(0.0, delay_base * jitter))
    # Unreachable -- the loop either returns or raises.
    if last_exc is not None:  # pragma: no cover
        raise last_exc
    raise RuntimeError("unreachable: retry loop exited without result")


def _extract_text(response: Any) -> str:
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

    Models very occasionally wrap JSON in a markdown fence even when asked
    not to. The greedy curly-brace match handles both fenced and bare JSON.
    Returns None if no object parses.
    """
    if not raw_text:
        return None
    text = raw_text.strip()
    # Quick path: the whole thing is JSON.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    # Strip ```json fences.
    fence_stripped = re.sub(r"^```(?:json)?\s*", "", text)
    fence_stripped = re.sub(r"\s*```\s*$", "", fence_stripped)
    try:
        parsed = json.loads(fence_stripped)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, TypeError):
        pass
    # Greedy curly match.
    match = _JSON_OBJ_RE.search(text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            return None
    return None


def _label_from_score(score: float) -> str:
    """Map a continuous score in [-1, +1] to one of the 5 discrete labels."""
    for label, lo, hi in LABEL_THRESHOLDS:
        if lo <= score < hi:
            return label
    if score >= 1.0:
        return "extreme_bullish"
    if score <= -1.0:
        return "extreme_bearish"
    return "neutral"


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _normalize_classification(parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Coerce the model's JSON into our canonical shape.

    Returns None if the required fields are missing or unparseable -- the
    caller treats that as a parse failure and falls back.
    """
    if not isinstance(parsed, dict):
        return None
    # mean_sentiment_score is mandatory.
    score_raw = parsed.get("mean_sentiment_score")
    if score_raw is None:
        # Some models will hand back "score" or "sentiment" -- be tolerant.
        score_raw = parsed.get("score") or parsed.get("sentiment")
    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        return None
    score = round(_clamp(score, -1.0, 1.0), 4)

    # Label: prefer model's, but validate against threshold table.
    label = str(parsed.get("label") or "").strip().lower()
    if label not in VALID_LABELS:
        label = _label_from_score(score)
    else:
        # Enforce label/score consistency. If the model's label disagrees
        # with the score band, the score wins (it's what downstream uses).
        score_band = _label_from_score(score)
        if label != score_band:
            label = score_band

    confidence_raw = parsed.get("polarity_confidence")
    if confidence_raw is None:
        confidence_raw = parsed.get("confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else 0.5
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = round(_clamp(confidence, 0.0, 1.0), 4)

    def _string_list(value: Any, limit: int = 5) -> List[str]:
        if not isinstance(value, list):
            return []
        out: List[str] = []
        for v in value:
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
            if len(out) >= limit:
                break
        return out

    return {
        "mean_sentiment_score": score,
        "label": label,
        "polarity_confidence": confidence,
        "key_drivers": _string_list(parsed.get("key_drivers")),
        "key_risks": _string_list(parsed.get("key_risks")),
        "rationale": str(parsed.get("rationale") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Fallback helper
# ---------------------------------------------------------------------------


def _fallback_from_ai_news(
    ticker: str,
    news_items: List[Dict[str, Any]],
    reason: str,
    *,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a sentiment dict via the engagement heuristic.

    Uses the same ``compute_thesis_signal_from_news`` shape as ai_news,
    then projects it onto our output schema with ``fallback=True``.
    """
    # Reconstruct the ai_news-shaped news_dict expected by the heuristic.
    total_mentions = len(news_items or [])
    hn_total_points = 0
    hn_total_comments = 0
    for item in news_items or []:
        if not isinstance(item, dict):
            continue
        if (item.get("source") or "").upper() == "HN":
            hn_total_points += int(item.get("points") or 0)
            hn_total_comments += int(item.get("num_comments") or 0)
    news_dict = {
        "ticker": ticker.upper(),
        "total_mentions": total_mentions,
        "hn_total_points": hn_total_points,
        "hn_total_comments": hn_total_comments,
    }
    sig = _ai_news.compute_thesis_signal_from_news(news_dict)
    score = sig.get("mean_sentiment_score")
    label = _label_from_score(float(score)) if isinstance(score, (int, float)) else "neutral"
    return {
        "ticker": ticker.upper(),
        "model": model,
        "mean_sentiment_score": score,
        "label": label,
        "polarity_confidence": 0.3 if score is not None else 0.0,
        "key_drivers": [],
        "key_risks": [],
        "raw_classification": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "generated_at": _now_iso(),
        "fallback": True,
        "fallback_reason": reason,
        # Preserve the engagement-heuristic provenance for transparency.
        "engagement_tier": sig.get("engagement_tier"),
        "article_count": sig.get("article_count"),
    }


def _neutral_no_news(ticker: str, reason: str = "no_news") -> Dict[str, Any]:
    """Empty-news input -> neutral non-signal (still callable into the pipeline)."""
    return {
        "ticker": ticker.upper(),
        "model": None,
        "mean_sentiment_score": None,
        "label": "neutral",
        "polarity_confidence": 0.0,
        "key_drivers": [],
        "key_risks": [],
        "raw_classification": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "generated_at": _now_iso(),
        "fallback": True,
        "fallback_reason": reason,
        "article_count": 0,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_llm_sentiment(
    ticker: str,
    news_items: List[Dict[str, Any]],
    model: str = DEFAULT_MODEL,
    *,
    use_cache: bool = True,
    client: Any = None,
    max_news_items: int = DEFAULT_MAX_NEWS_ITEMS,
) -> Dict[str, Any]:
    """Classify news sentiment for one ticker via the Claude API.

    Parameters
    ----------
    ticker:
        Ticker symbol, case-insensitive (we upper-case internally).
    news_items:
        List of dicts. Recommended shape -- ``{title, snippet/summary,
        source, date/time_published, points (optional), num_comments
        (optional)}``. Items without a title are dropped. Items beyond
        ``max_news_items`` are dropped after ranking by HN points then
        recency.
    model:
        Anthropic model id. Defaults to ``claude-sonnet-4-5``.
    use_cache:
        When True (default), the system prompt carries an ephemeral
        ``cache_control`` marker so 168 per-ticker calls share the same
        cached prefix.
    client:
        For testing -- pass a stand-in Anthropic client with a
        ``.messages.create`` method. In production leave it None.

    Returns
    -------
    A dict with keys (see module docstring):
        ticker, model, mean_sentiment_score, label, polarity_confidence,
        key_drivers, key_risks, raw_classification, input_tokens,
        output_tokens, cached_input_tokens, generated_at, fallback,
        fallback_reason.

    On any error or missing API key, returns the engagement-heuristic
    fallback shape with ``fallback=True``. Never raises.
    """
    ticker = (ticker or "").upper().strip() or "?"

    if not news_items:
        return _neutral_no_news(ticker)

    if client is None:
        api_key = _api_key()
        if not api_key:
            return _fallback_from_ai_news(ticker, news_items, "missing_api_key", model=None)
        anthropic = _import_anthropic()
        if anthropic is None:
            return _fallback_from_ai_news(
                ticker, news_items, "anthropic_sdk_unavailable", model=None
            )
        try:
            client = anthropic.Anthropic(api_key=api_key)
        except Exception as exc:
            logger.warning("Anthropic client construction failed: %s", exc)
            return _fallback_from_ai_news(
                ticker, news_items, "client_init_failed", model=None
            )

    try:
        system_blocks = build_system_blocks()
        if not use_cache:
            system_blocks = [
                {k: v for k, v in b.items() if k != "cache_control"}
                for b in system_blocks
            ]
        user_msg = build_user_message(ticker, news_items, max_news_items=max_news_items)
        response = _call_anthropic_with_retry(
            client=client,
            model=model,
            system_blocks=system_blocks,
            user_message=user_msg,
        )
    except Exception as exc:
        logger.warning("Anthropic call failed for %s: %s", ticker, exc)
        return _fallback_from_ai_news(
            ticker, news_items, f"api_error: {exc}", model=model
        )

    raw_text = _extract_text(response)
    if not raw_text:
        return _fallback_from_ai_news(ticker, news_items, "empty_response", model=model)

    parsed = _parse_json_envelope(raw_text)
    normalized = _normalize_classification(parsed) if parsed else None
    if normalized is None:
        return _fallback_from_ai_news(
            ticker, news_items, "json_parse_error", model=model
        )

    usage = _extract_usage(response)
    return {
        "ticker": ticker,
        "model": model,
        "mean_sentiment_score": normalized["mean_sentiment_score"],
        "label": normalized["label"],
        "polarity_confidence": normalized["polarity_confidence"],
        "key_drivers": normalized["key_drivers"],
        "key_risks": normalized["key_risks"],
        "rationale": normalized["rationale"],
        "raw_classification": raw_text,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cached_input_tokens": usage.get("cached_input_tokens", 0),
        "generated_at": _now_iso(),
        "fallback": False,
        "fallback_reason": None,
        "article_count": len(news_items),
    }


def compute_universe_llm_sentiment(
    news_by_ticker: Dict[str, List[Dict[str, Any]]],
    model: str = DEFAULT_MODEL,
    *,
    max_concurrency: int = 5,
    client: Any = None,
    max_news_items: int = DEFAULT_MAX_NEWS_ITEMS,
) -> Dict[str, Dict[str, Any]]:
    """Run :func:`compute_llm_sentiment` across a whole universe in parallel.

    Falls back per-ticker if any single call fails. Returns a dict keyed
    by ticker. Concurrency is bounded by ``max_concurrency``. If the SDK
    or API key are unavailable, every ticker gets the engagement-heuristic
    fallback -- the function never raises.
    """
    news_by_ticker = news_by_ticker or {}

    # Build one client upfront so we don't repeat env checks per ticker.
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
    if not news_by_ticker:
        return results

    def _one(ticker: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        return compute_llm_sentiment(
            ticker=ticker,
            news_items=items,
            model=model,
            client=client,
            max_news_items=max_news_items,
        )

    workers = max(1, int(max_concurrency))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_ticker = {
            ex.submit(_one, ticker, items): ticker
            for ticker, items in news_by_ticker.items()
        }
        for fut in concurrent.futures.as_completed(future_to_ticker):
            ticker = future_to_ticker[fut]
            try:
                results[ticker] = fut.result()
            except Exception as exc:
                logger.warning("Universe sentiment task crashed for %s: %s", ticker, exc)
                results[ticker] = _fallback_from_ai_news(
                    ticker, news_by_ticker.get(ticker) or [], f"task_crash: {exc}", model=model
                )
    return results


# ---------------------------------------------------------------------------
# Cost cap helper (spec §4)
# ---------------------------------------------------------------------------


def _estimate_cost_usd(usage_dicts: List[Dict[str, Any]], model: str) -> float:
    """Sum per-call token usage and apply per-model pricing.

    ``usage_dicts`` is a list of the per-ticker output dicts returned
    by :func:`compute_llm_sentiment` (or their ``usage`` sub-dicts; both
    shapes are accepted). Unknown models default to Haiku pricing
    (cheaper end -- biased to NOT trip the cap on an unmapped model id).
    """
    pricing = MODEL_PRICING_PER_MTOK.get(model) or MODEL_PRICING_PER_MTOK[DEFAULT_MODEL]
    input_tok = 0
    cached_tok = 0
    output_tok = 0
    for u in usage_dicts or []:
        if not isinstance(u, dict):
            continue
        # Support both the flat result dict and a nested {usage: {...}}.
        u2 = u.get("usage") if "usage" in u and isinstance(u["usage"], dict) else u
        input_tok += int(u2.get("input_tokens") or 0)
        cached_tok += int(u2.get("cached_input_tokens") or 0)
        output_tok += int(u2.get("output_tokens") or 0)
    # input_tokens already excludes cached reads in Anthropic's usage object,
    # but cached_input_tokens is reported separately and priced at the
    # cached rate.
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
    """Return True iff ``LTHCS_LLM_SENTIMENT_ENABLED=1`` in the env.

    Default is OFF. Lets ``lthcs_daily.py`` call into this module
    unconditionally; nothing happens until the user flips the flag.
    """
    return os.environ.get(ENV_ENABLED, "").strip() == "1"


# ---------------------------------------------------------------------------
# Shadow persistence (spec §2)
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
    results: Dict[str, Dict[str, Any]],
    *,
    data_root: Optional[Path] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write the per-day aggregate ``data/lthcs/llm_sentiment/<date>.json``.

    Mirrors the schema-friendly per-ticker dict returned by
    :func:`compute_llm_sentiment`. ``extra`` carries run-level
    metadata (model, total_cost_usd, ticker_count) for ops visibility.
    """
    path = _shadow_daily_path(calc_date, data_root=data_root)
    payload = {
        "calc_date": calc_date,
        "generated_at": _now_iso(),
        "meta": dict(extra or {}),
        "results": dict(results or {}),
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
    """Append today's record to ``data/lthcs/llm_sentiment_by_ticker/<T>.json``.

    Rolling history capped at ``history_limit`` entries (newest last). The
    file format mirrors ``data/lthcs/sentiment/<T>.json`` shape: a JSON
    list. Duplicate same-day entries replace the existing tail entry so
    a ``--force`` re-run doesn't double-append.
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
# Gated shadow entrypoint (spec §2 / §7)
# ---------------------------------------------------------------------------


def score_universe(
    news_by_ticker: Dict[str, List[Dict[str, Any]]],
    *,
    calc_date: str,
    model: Optional[str] = None,
    max_concurrency: int = 5,
    max_news_items: int = DEFAULT_MAX_NEWS_ITEMS,
    client: Any = None,
    data_root: Optional[Path] = None,
    persist: bool = True,
    cost_cap_usd: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Gated shadow entrypoint -- called from ``lthcs_daily.py`` Stage 2.

    Returns ``None`` (no-op) when ``LTHCS_LLM_SENTIMENT_ENABLED`` is not
    ``"1"``. Otherwise:

    1. Run :func:`compute_universe_llm_sentiment` across the merged news.
    2. Estimate cost from the response usage objects.
    3. If cost exceeds ``LTHCS_LLM_SENTIMENT_MAX_USD_PER_DAY`` (default
       $1.00), log and SKIP persistence -- the prior day's shadow file is
       the last good record.
    4. Else (when ``persist=True``) write
       ``data/lthcs/llm_sentiment/<calc_date>.json`` and append
       ``data/lthcs/llm_sentiment_by_ticker/<T>.json`` per ticker.

    Returns a dict with keys ``{"results", "meta"}``. ``meta`` carries
    ``model``, ``total_cost_usd``, ``cost_cap_usd``, ``cost_cap_hit``,
    ``ticker_count``, ``fallback_count``.

    NEVER touches ``data/lthcs/sentiment/`` (the production Thesis
    rotation cache). Spec §7 #1.
    """
    if not is_enabled():
        return None

    model = (model or _model_from_env()).strip() or DEFAULT_MODEL
    cap = float(cost_cap_usd) if cost_cap_usd is not None else _max_usd_per_day()

    results = compute_universe_llm_sentiment(
        news_by_ticker=news_by_ticker or {},
        model=model,
        max_concurrency=max_concurrency,
        client=client,
        max_news_items=max_news_items,
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
    }

    if cost_cap_hit:
        logger.warning(
            "! Stage 2: LLM sentiment cost cap hit ($%.4f > $%.2f); "
            "skipping shadow persistence.",
            total_cost,
            cap,
        )
        return {"results": results, "meta": meta, "persisted": False}

    if not persist:
        return {"results": results, "meta": meta, "persisted": False}

    # Stamp each per-ticker record with the calc_date for history files.
    for sym, rec in results.items():
        rec.setdefault("calc_date", calc_date)

    try:
        write_shadow_daily(
            calc_date,
            results,
            data_root=data_root,
            extra={
                "model": model,
                "total_cost_usd": total_cost,
                "ticker_count": len(results),
                "fallback_count": fallback_count,
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
    "ENV_MAX_USD_PER_DAY",
    "ENV_MODEL",
    "LABEL_THRESHOLDS",
    "MODEL_PRICING_PER_MTOK",
    "SHADOW_BY_TICKER_DIRNAME",
    "SHADOW_DAILY_DIRNAME",
    "SHADOW_TICKER_HISTORY_LIMIT",
    "SYSTEM_PROMPT",
    "VALID_LABELS",
    "append_shadow_ticker_history",
    "build_system_blocks",
    "build_user_message",
    "compute_llm_sentiment",
    "compute_universe_llm_sentiment",
    "is_enabled",
    "score_universe",
    "write_shadow_daily",
]
