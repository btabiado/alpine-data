"""LTHCS Claude-derived news sentiment for the Thesis pillar.

This module is an OPT-IN replacement for the engagement-tier heuristic in
:func:`lthcs.sources.ai_news.compute_thesis_signal_from_news`. The heuristic
infers sentiment from mention count + HN-points/comments, which is a proxy
for "how loud is this ticker in the AI news cycle" — not direction.

Here we ask Claude to read 3-10 actual headlines/snippets per ticker and
return a structured JSON classification: a continuous sentiment score in
[-1, +1], a discrete label, confidence, key drivers, and key risks.

Design goals (mirrors ``lthcs.narratives_llm``)
-----------------------------------------------

* Cheap. The long system prompt (LTHCS context + sentiment scale +
  output schema) is cached via ``cache_control: ephemeral`` so the
  ~1.1k system tokens are reused across all 168 tickers in one run. With
  Sonnet pricing (~$3/M input, ~$15/M output, 10% cached read rate) a
  whole-universe run is roughly $0.30.
* Robust. Missing ``ANTHROPIC_API_KEY``, missing ``anthropic`` SDK,
  any API/network error, or any JSON parse failure falls back to the
  existing engagement heuristic. The daily pipeline never crashes.
* Opt-in. Nothing here runs unless ``LTHCS_LLM_SENTIMENT_ENABLED=1``
  is set, and ``lthcs_daily.py`` Step 5 is the only wire-up site.

Public surface:

* :func:`compute_llm_sentiment` -- single ticker
* :func:`compute_universe_llm_sentiment` -- whole-universe batch helper

Both always return dicts with the same shape (see
:func:`compute_llm_sentiment` docstring) -- successful LLM calls and
fallbacks are interchangeable from a caller's perspective.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from . import ai_news as _ai_news

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "claude-sonnet-4-5"
ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_ENABLED = "LTHCS_LLM_SENTIMENT_ENABLED"
ENV_MODEL = "LTHCS_LLM_SENTIMENT_MODEL"

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
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=[{"role": "user", "content": user_message}],
        )


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
        response = _call_anthropic(
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


__all__ = [
    "DEFAULT_MODEL",
    "ENV_API_KEY",
    "ENV_ENABLED",
    "ENV_MODEL",
    "LABEL_THRESHOLDS",
    "SYSTEM_PROMPT",
    "VALID_LABELS",
    "build_system_blocks",
    "build_user_message",
    "compute_llm_sentiment",
    "compute_universe_llm_sentiment",
]
