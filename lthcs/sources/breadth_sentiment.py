"""Breadth / sentiment regime indicators for the LTHCS pipeline.

Three free public sentiment series bundled into one module — they share
nothing operationally except that they all sample the same
"how-bullish-is-the-market" question from different vantage points:

    * CBOE Put/Call Ratio (daily)     -> options-market hedging tone
    * AAII Investor Sentiment (weekly) -> retail bull/bear/neutral %
    * NAAIM Exposure Index (weekly)    -> active-manager equity exposure

Each indicator is fetched, parsed, percentile-ranked vs trailing 1-yr
history (where applicable), and classified into a coarse regime label.
A roll-up ``composite_regime`` counts how many of the three are flashing
"defensive" tone in the same week.

Design conventions (mirror ``sector_rss.py`` + ``fred.py``):

    * stdlib HTML / CSV parsing — no BeautifulSoup, no pandas at runtime
    * per-source ``FileCache`` keyed by today's ISO date (these series
      update at most daily; weekly ones change Thursdays) so the daily
      pipeline never re-fetches the same day
    * any upstream error returns ``None`` for that source and is recorded
      in ``data_quality.failed_sources``; the public function NEVER
      raises — this is a "nice to have" pillar and a feed outage must
      not bubble up into the daily run
    * a browser-flavored ``User-Agent`` is sent on every request; CBOE
      and AAII both 403 default ``python-requests`` UAs.

Live URL drift notes (2026-05-17 audit):
    * CBOE retired the per-day CSV at ``cdn.cboe.com/...volume.csv``.
      Daily put/call ratios now live in a Next.js-rendered HTML page at
      ``/markets/us/options/market-statistics/daily`` with the values
      embedded in a streaming JSON payload. We scrape the payload via
      regex. ``CBOE_DAILY_CSV_PREFIX`` is kept as a module attribute for
      backward compatibility (tests reference it) but is no longer hit
      live.
    * AAII still serves the weekly survey at
      ``/sentimentsurvey/sent_results`` but the page no longer has the
      "Bullish 28% Neutral 28% Bearish 44%" inline text — it's a proper
      HTML table now. The parser scans for the table whose header row
      reads "Reported Date / Bullish / Neutral / Bearish" and takes the
      first data row.
    * NAAIM keeps the same page URL but the WordPress template changed.
      The historical-readings table now has ``id="surveydata"`` with
      column ``NAAIM Number Mean/Average``. We parse that table and
      fall back to the JS-embedded Google-Visualization chart series if
      the table is missing.
"""

from __future__ import annotations

import csv
import datetime as _dt
import html
import io
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

# Historical CSV prefix. Kept as a module attribute because tests import it
# to stub URLs; in practice CBOE has retired this endpoint and we route
# CBOE traffic through ``CBOE_DAILY_HTML`` below. A future restoration of
# the CSV path would only need a flip in ``fetch_put_call``.
CBOE_DAILY_CSV_PREFIX = (
    "https://cdn.cboe.com/data/us/options/market_statistics/daily/"
)
# Live HTML page that embeds the daily ratios as Next.js streaming JSON.
# Issues a 302 to /markets/us/options/market-statistics/daily — we follow
# redirects so this short URL still works.
CBOE_DAILY_HTML = "https://www.cboe.com/us/options/market_statistics/daily/"
# Legacy alias retained for compatibility.
CBOE_EQUITY_PUT_CALL_HTML = CBOE_DAILY_HTML

# AAII publishes the weekly sentiment-survey results on a public HTML page.
# Free, no login. We parse the table whose header reads
# "Reported Date / Bullish / Neutral / Bearish".
AAII_SENTIMENT_URL = "https://www.aaii.com/sentimentsurvey/sent_results"

# NAAIM Exposure Index — weekly. We parse the table with id="surveydata".
NAAIM_EXPOSURE_URL = "https://www.naaim.org/programs/naaim-exposure-index/"


# ---------------------------------------------------------------------------
# Cache / rate-limit singletons
# ---------------------------------------------------------------------------

# 24h TTL — these series sample at most daily (CBOE) or weekly (AAII /
# NAAIM). Even when stale by one day, the daily pipeline still gets a
# usable signal.
_CACHE_TTL_SECONDS = 24 * 60 * 60

# One shared cache namespace. Sub-keys (``cboe/...``, ``aaii/...``,
# ``naaim/...``) disambiguate.
_CACHE = FileCache("breadth_sentiment")

# One shared bucket — at most a few requests per daily run. Capacity 4
# covers (cboe, aaii, naaim) with headroom; refill at 1/min so a
# misbehaving caller can't hammer upstream.
_BUCKET = TokenBucket(capacity=4, refill_rate=1.0 / 60.0)

# CBOE / AAII / NAAIM all run behind Cloudflare-style WAFs that 403 the
# default ``python-requests`` User-Agent. A real browser UA is fine.
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Regime thresholds (kept as module constants so tests can document the
# boundary semantics explicitly).
_PUTCALL_COMPLACENT_MAX = 0.7   # < 0.7
_PUTCALL_NORMAL_MAX = 1.0       # 0.7 <= x <= 1.0
_PUTCALL_ELEVATED_MAX = 1.3     # 1.0 < x <= 1.3
# >1.3 => "panic"

_AAII_EXTREME_BULL_MIN = 30     # spread > 30
_AAII_BULL_MIN = 10             # 10 < spread <= 30
_AAII_NEUTRAL_MIN = -10         # -10 <= spread <= 10
_AAII_BEAR_MIN = -30            # -30 <= spread < -10
# < -30 => extreme_bearish

_NAAIM_DEFENSIVE_MAX = 30       # < 30
_NAAIM_MODERATE_MAX = 80        # 30 <= x <= 80
_NAAIM_AGGRESSIVE_MAX = 120     # 80 < x <= 120
# > 120 => leveraged

# Tokens that mark a "defensive" regime signal for the composite roll-up.
_DEFENSIVE_PUTCALL = {"elevated_hedging", "panic"}
_DEFENSIVE_AAII = {"bearish", "extreme_bearish"}
_DEFENSIVE_NAAIM = {"defensive"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_iso() -> str:
    return _dt.date.today().isoformat()


def _log(msg: str) -> None:
    """Stderr logger — quiet by default but discoverable in CI logs."""
    print(f"[breadth_sentiment] {msg}", file=sys.stderr)


def _cache_for(cache_dir: Optional[Path]) -> FileCache:
    """Return the shared cache, or a tmp-rooted one for the caller."""
    if cache_dir is None:
        return _CACHE
    return FileCache("breadth_sentiment", root=cache_dir)


def _http_get(url: str, *, timeout: int = 20) -> Optional[str]:
    """Hit ``url`` once, return text or None on any failure.

    Honors the shared token bucket; if the bucket is empty we treat that
    as a temporary failure (return None) rather than blocking the daily
    pipeline. Follows redirects (CBOE and NAAIM both 301/302 to a
    canonical path).
    """
    if not _BUCKET.try_acquire():
        return None
    try:
        resp = requests.get(
            url,
            headers=_DEFAULT_HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )
    except requests.RequestException:
        return None
    if getattr(resp, "status_code", 0) != 200:
        return None
    return getattr(resp, "text", "") or ""


def _percentile(value: float, history: List[float]) -> Optional[float]:
    """Where does ``value`` rank within ``history`` (0..1, inclusive)?

    Returns ``None`` if history is empty. Uses the "<=" convention so a
    value at the max comes back as 1.0 and at the min as ~0.0+.
    """
    if not history:
        return None
    sorted_h = sorted(history)
    below = sum(1 for v in sorted_h if v <= value)
    return below / len(sorted_h)


def _mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _strip_html_text(s: str) -> str:
    # Inline (?is) flags instead of runtime re.I|re.S — CodeQL
    # py/bad-tag-filter inspects the pattern string itself and doesn't
    # trust runtime IGNORECASE kwargs.
    s = re.sub(r"(?is)<script\b[^>]*>.*?</script>", " ", s)
    s = re.sub(r"(?is)<style\b[^>]*>.*?</style>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# 1) CBOE Put/Call Ratio
# ---------------------------------------------------------------------------


def _putcall_regime(ratio: float) -> str:
    """Classify the latest P/C ratio.

    Boundaries (per spec):
        complacent       : ratio  < 0.7
        normal           : 0.7   <= ratio <= 1.0
        elevated_hedging : 1.0   <  ratio <= 1.3
        panic            : ratio  > 1.3
    """
    if ratio < _PUTCALL_COMPLACENT_MAX:
        return "complacent"
    if ratio <= _PUTCALL_NORMAL_MAX:
        return "normal"
    if ratio <= _PUTCALL_ELEVATED_MAX:
        return "elevated_hedging"
    return "panic"


# Backward-compat CSV parser. The live CSV endpoint is dead but tests and
# callers may still feed CSV text (e.g. a manual download) through this
# helper, so we keep it intact.
def _parse_cboe_csv(text: str) -> List[Tuple[str, float]]:
    """Parse a CBOE daily-volume CSV into (date, p/c ratio) rows.

    The CBOE CSV uses a small preamble before the actual header row, so
    we sniff for a row that contains a ``P/C Ratio``-like column. Any
    rows that fail to parse a float are skipped, not raised.
    """
    if not text:
        return []

    reader = csv.reader(io.StringIO(text))
    header_idx: Optional[int] = None
    date_idx: Optional[int] = None
    header_row: Optional[List[str]] = None

    rows: List[List[str]] = list(reader)
    for i, row in enumerate(rows):
        joined = " ".join(c.strip().lower() for c in row)
        if "p/c ratio" in joined or "p/c" in joined and "ratio" in joined:
            header_row = row
            for j, cell in enumerate(row):
                if cell.strip().lower() == "total p/c ratio":
                    header_idx = j
                    break
            if header_idx is None:
                for j, cell in enumerate(row):
                    if "p/c ratio" in cell.strip().lower():
                        header_idx = j
                        break
            for j, cell in enumerate(row):
                if cell.strip().lower() in {"date", "trade date"}:
                    date_idx = j
                    break
            data_start = i + 1
            break
    else:
        return []

    if header_idx is None or header_row is None:
        return []

    out: List[Tuple[str, float]] = []
    for row in rows[data_start:]:
        if not row or len(row) <= header_idx:
            continue
        raw = row[header_idx].strip()
        if not raw:
            continue
        try:
            val = float(raw)
        except ValueError:
            continue
        date_str = ""
        if date_idx is not None and len(row) > date_idx:
            date_str = row[date_idx].strip()
        out.append((date_str, val))
    return out


# CBOE HTML parsing — the page embeds optionsData as a streaming JSON
# chunk where every quote is backslash-escaped (e.g. ``\"name\"``). We
# scrape with a regex that matches that escaped form. Pattern is anchored
# on the ``PUT/CALL RATIO`` token so a layout reshuffle that keeps the
# JSON structure still parses.
_CBOE_RATIO_RE = re.compile(
    r'\\"name\\":\\"([A-Z +/]*?PUT/CALL RATIO)\\",\\"value\\":\\"([0-9.]+)\\"'
)
_CBOE_DATE_RE = re.compile(r'\\"selectedDate\\":\\"(\d{4}-\d{2}-\d{2})\\"')


def _parse_cboe_html(html_text: str) -> Optional[Tuple[str, float]]:
    """Pull the latest Total P/C Ratio + report date from the CBOE page.

    Returns ``(iso_date, ratio)`` or ``None`` if neither could be found.
    Prefers ``TOTAL PUT/CALL RATIO``; falls back to ``EQUITY PUT/CALL
    RATIO`` if Total is absent (which would be a structural change but
    Equity is still a useable proxy).
    """
    if not html_text:
        return None

    ratios: Dict[str, float] = {}
    for m in _CBOE_RATIO_RE.finditer(html_text):
        name = m.group(1).strip().upper()
        try:
            ratios[name] = float(m.group(2))
        except ValueError:
            continue
    if not ratios:
        return None

    # Prefer Total, fall back to Equity. (Index is a poor sentiment
    # proxy — it's dominated by hedging flows.)
    value: Optional[float] = None
    for key in ("TOTAL PUT/CALL RATIO", "EQUITY PUT/CALL RATIO"):
        if key in ratios:
            value = ratios[key]
            break
    if value is None:
        return None

    date_match = _CBOE_DATE_RE.search(html_text)
    iso_date = date_match.group(1) if date_match else _today_iso()
    return iso_date, value


def fetch_put_call(cache_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch the latest CBOE Total Put/Call Ratio.

    Live data comes from the CBOE Next.js HTML page (the per-day CSV
    endpoint was retired). Returns ``None`` on any unrecoverable
    failure. Cache TTL: 24h, keyed by today's ISO date.
    """
    cache = _cache_for(cache_dir)
    today = _today_iso()
    cache_key = f"cboe/putcall/{today}"

    hit = None
    try:
        hit = cache.get(cache_key)
    except Exception:
        hit = None
    if hit is not None:
        # CacheHit.value may be None (negative-cached) — fall through if so.
        if hit.value is not None:
            return dict(hit.value)

    body = _http_get(CBOE_DAILY_HTML)
    if not body:
        _log("CBOE daily page fetch failed")
        try:
            cache.set(cache_key, None, ttl_seconds=_CACHE_TTL_SECONDS)
        except Exception as e:
            _log(f"cboe negative-cache write suppressed: {type(e).__name__}")
        return None

    parsed = _parse_cboe_html(body)
    if parsed is None:
        _log("CBOE daily page parsed empty (Total/Equity P/C ratio not found)")
        return None

    last_updated, latest_val = parsed

    # We don't carry a rolling 30d / 1y history on disk (would require a
    # separate ingestion job). Both mean_30d and percentile_1y are left
    # as None so callers can degrade gracefully. The regime label is the
    # primary signal.
    result: Dict[str, Any] = {
        "latest": float(latest_val),
        "mean_30d": None,
        "percentile_1y": None,
        "regime": _putcall_regime(float(latest_val)),
        "source": "cboe_daily_html",
        "last_updated": last_updated,
    }

    try:
        cache.set(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)
    except Exception as e:
        _log(f"cboe cache write suppressed: {type(e).__name__}")
    return dict(result)


# ---------------------------------------------------------------------------
# 2) AAII Sentiment Survey
# ---------------------------------------------------------------------------


def _aaii_regime(spread: float) -> str:
    """Classify a bull-bear spread.

    Boundaries (per spec):
        extreme_bullish  : spread >  30
        bullish          : 10  <  spread <= 30
        neutral          : -10 <= spread <= 10
        bearish          : -30 <= spread < -10
        extreme_bearish  : spread < -30
    """
    if spread > _AAII_EXTREME_BULL_MIN:
        return "extreme_bullish"
    if spread > _AAII_BULL_MIN:
        return "bullish"
    if spread >= _AAII_NEUTRAL_MIN:
        return "neutral"
    if spread >= _AAII_BEAR_MIN:
        return "bearish"
    return "extreme_bearish"


# Anchored on the "Reported Date / Bullish / Neutral / Bearish" header
# the AAII page renders. We pull the header to confirm column order
# (defensively — they could swap Neutral and Bearish in a redesign), then
# pull the first data row.
_AAII_HEADER_RE = re.compile(
    r"Reported\s+Date\s+Bullish\s+Neutral\s+Bearish", re.I
)
# A data row in the stripped table text. Date is "May 13" style (no year),
# followed by three percentages. We allow whitespace between cells.
_AAII_ROW_RE = re.compile(
    r"([A-Z][a-z]{2}\s+\d{1,2})\s+"
    r"(\d{1,2}(?:\.\d+)?)\s*%\s+"
    r"(\d{1,2}(?:\.\d+)?)\s*%\s+"
    r"(\d{1,2}(?:\.\d+)?)\s*%"
)

# Old-style "Bullish 28.5% Neutral 29.4% Bearish 42.1%" inline text, kept
# as a fallback for tests / archived copies of the page.
_AAII_INLINE_RE = re.compile(
    r"bullish[^0-9%]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%\s*"
    r"(?:[^0-9%]{0,40}neutral[^0-9%]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%\s*)?"
    r"(?:[^0-9%]{0,40}bearish[^0-9%]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%)",
    re.I | re.S,
)

# Fallback date discovery patterns.
_AAII_DATE_PATTERNS = [
    re.compile(
        r"(?:week ending|as of|reported)[:\s]+([A-Za-z]+\s+\d{1,2},\s*\d{4})",
        re.I,
    ),
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})"),
]


def _resolve_aaii_year(month: int, day: int) -> int:
    """Best-effort year inference for an AAII row that omits the year.

    AAII publishes weekly, so the latest row is always within the last
    couple of weeks of today. We assume current year unless that would
    put the row more than ~60 days in the future, in which case roll
    back to the prior year.
    """
    today = _dt.date.today()
    try:
        candidate = _dt.date(today.year, month, day)
    except ValueError:
        return today.year
    if (candidate - today).days > 60:
        return today.year - 1
    return today.year


_AAII_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_aaii_short_date(raw: str) -> Optional[str]:
    """Parse "May 13" / "Sep 5" into an ISO date using the year heuristic."""
    parts = raw.split()
    if len(parts) != 2:
        return None
    month = _AAII_MONTH_MAP.get(parts[0][:4].lower()) or _AAII_MONTH_MAP.get(
        parts[0][:3].lower()
    )
    if not month:
        return None
    try:
        day = int(parts[1])
    except ValueError:
        return None
    year = _resolve_aaii_year(month, day)
    try:
        return _dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def _parse_aaii_date(text: str) -> Optional[str]:
    for pat in _AAII_DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1)
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
            try:
                return _dt.datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _parse_aaii_html(html_text: str) -> Optional[Dict[str, Any]]:
    """Pull bullish/neutral/bearish % and week-ending date from AAII HTML.

    Live layout: a table with headers
    "Reported Date / Bullish / Neutral / Bearish" and rows like
    "May 13   39.3%  24.1%  36.6%". The first data row is the latest.

    Fallback path: an inline "Bullish 28.5% Neutral 29.4% Bearish 42.1%"
    pattern, kept so unit-tests and archived snapshots still parse.

    Returns a dict with ``bullish_pct``, ``neutral_pct``, ``bearish_pct``,
    and ``week_ending``, or ``None`` if none of those could be extracted.
    """
    if not html_text:
        return None
    text = _strip_html_text(html_text)
    if not text:
        return None

    # Preferred: table layout. Anchor on the header to make sure we
    # interpret the columns in the right order.
    header = _AAII_HEADER_RE.search(text)
    if header:
        # Look for the first row after the header.
        tail = text[header.end():]
        row = _AAII_ROW_RE.search(tail)
        if row:
            try:
                bullish = float(row.group(2))
                neutral = float(row.group(3))
                bearish = float(row.group(4))
            except ValueError:
                bullish = neutral = bearish = None  # type: ignore[assignment]
            week_ending = _parse_aaii_short_date(row.group(1))
            if bullish is not None and bearish is not None:
                return {
                    "bullish_pct": bullish,
                    "neutral_pct": neutral,
                    "bearish_pct": bearish,
                    "week_ending": week_ending or _parse_aaii_date(text),
                }

    # Fallback: inline text layout used in older fixtures.
    m = _AAII_INLINE_RE.search(text)
    if m:
        try:
            bullish = float(m.group(1))
        except (TypeError, ValueError):
            bullish = None  # type: ignore[assignment]
        try:
            neutral = float(m.group(2)) if m.group(2) else None
        except (TypeError, ValueError):
            neutral = None
        try:
            bearish = float(m.group(3)) if m.group(3) else None
        except (TypeError, ValueError):
            bearish = None
        if bullish is not None and bearish is not None:
            return {
                "bullish_pct": bullish,
                "neutral_pct": neutral,
                "bearish_pct": bearish,
                "week_ending": _parse_aaii_date(text),
            }

    return None


def fetch_aaii_sentiment(
    cache_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch the most recent AAII weekly sentiment survey.

    Returns a dict shaped as in the module docstring, or ``None`` on
    failure. Cache TTL: 24h, keyed by today's ISO date.

    The 4-week MA of bull-bear spread is approximated from a history
    cache keyed by ``aaii/history``; the first run will have a 1-point
    history and ``spread_4w_ma`` will equal ``bull_bear_spread``.
    """
    cache = _cache_for(cache_dir)
    today = _today_iso()
    cache_key = f"aaii/latest/{today}"

    hit = None
    try:
        hit = cache.get(cache_key)
    except Exception:
        hit = None
    if hit is not None and hit.value is not None:
        return dict(hit.value)

    text = _http_get(AAII_SENTIMENT_URL)
    if not text:
        _log("AAII sentiment page fetch failed")
        return None

    parsed = _parse_aaii_html(text)
    if not parsed:
        _log("AAII sentiment page parsed empty")
        return None

    bullish = parsed.get("bullish_pct")
    bearish = parsed.get("bearish_pct")
    neutral = parsed.get("neutral_pct")
    if bullish is None or bearish is None:
        _log("AAII missing bullish or bearish; cannot compute spread")
        return None

    spread = bullish - bearish

    # Update rolling history (keyed independently from the daily latest
    # entry so the 4w MA is stable across reruns).
    history_key = "aaii/history"
    history: List[Dict[str, Any]] = []
    try:
        h_hit = cache.get(history_key)
        if h_hit is not None and isinstance(h_hit.value, list):
            history = [dict(r) for r in h_hit.value]
    except Exception:
        history = []

    week_ending = parsed.get("week_ending")
    # Only append a new history row if the week_ending date is new.
    if not history or (week_ending and history[-1].get("week_ending") != week_ending):
        history.append({"week_ending": week_ending, "spread": spread})
        # Keep the last 52 weeks.
        history = history[-52:]
        try:
            cache.set(history_key, history, ttl_seconds=_CACHE_TTL_SECONDS * 90)
        except Exception as e:
            _log(f"aaii history cache write suppressed: {type(e).__name__}")

    recent_spreads = [float(r["spread"]) for r in history[-4:] if r.get("spread") is not None]
    spread_4w_ma = _mean(recent_spreads)

    result: Dict[str, Any] = {
        "bullish_pct": float(bullish),
        "bearish_pct": float(bearish),
        "neutral_pct": float(neutral) if neutral is not None else None,
        "bull_bear_spread": float(spread),
        "spread_4w_ma": float(spread_4w_ma) if spread_4w_ma is not None else None,
        "regime": _aaii_regime(float(spread)),
        "week_ending": week_ending,
    }

    try:
        cache.set(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)
    except Exception as e:
        _log(f"aaii cache write suppressed: {type(e).__name__}")
    return dict(result)


# ---------------------------------------------------------------------------
# 3) NAAIM Exposure Index
# ---------------------------------------------------------------------------


def _naaim_regime(exposure: float) -> str:
    """Classify the NAAIM exposure reading.

    Boundaries (per spec):
        defensive : exposure  < 30
        moderate  : 30  <= exposure <= 80
        aggressive: 80  <  exposure <= 120
        leveraged : exposure  > 120
    """
    if exposure < _NAAIM_DEFENSIVE_MAX:
        return "defensive"
    if exposure <= _NAAIM_MODERATE_MAX:
        return "moderate"
    if exposure <= _NAAIM_AGGRESSIVE_MAX:
        return "aggressive"
    return "leveraged"


# After HTML strip, the NAAIM history table comes out as:
# "Date NAAIM Number Mean/Average Bearish Quart1 Quart2 Quart3 Bullish Deviation
#  05/13/2026 77.34 -200 78.75 99.00 100.00 200 68.17 05/06/2026 ..."
# So every 8 cells = one row, starting with a MM/DD/YYYY date and the
# second cell is the NAAIM Number.
_NAAIM_HEADER_RE = re.compile(
    r"Date\s+NAAIM\s+Number(?:\s+Mean(?:/Average)?)?", re.I
)
_NAAIM_ROW_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})\s+(-?\d{1,3}(?:\.\d+)?)"
)

# Google-visualization chart fallback. Each entry is
# ``[new Date(2026, 4, 13), 77.34]`` where the month is 0-indexed.
_NAAIM_CHART_RE = re.compile(
    r"new\s+Date\(\s*(\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\s*\)\s*,\s*"
    r"(-?\d{1,3}(?:\.\d+)?)"
)


def _parse_naaim_date(raw: str) -> Optional[str]:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return _dt.datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _parse_naaim_html(html_text: str) -> List[Tuple[str, float]]:
    """Return a list of (week_ending_iso, exposure) rows, newest first.

    Primary path: the ``surveydata`` table. After HTML-stripping we have
    ``mm/dd/yyyy <average> ...`` cells; we match consecutive date+number
    pairs and take the number as the NAAIM exposure for that row.

    Fallback: the Google-Visualization line-chart data, which encodes the
    same series as ``[new Date(year, month0, day), value]`` literals. We
    only fall back if the table extraction yields zero rows.

    Plausibility filter: NAAIM exposure is bounded roughly to
    ``[-200, 300]``; anything outside is filtered out.
    """
    if not html_text:
        return []

    rows: List[Tuple[str, float]] = []
    seen: set = set()

    text = _strip_html_text(html_text)
    if text:
        header = _NAAIM_HEADER_RE.search(text)
        scan_from = text[header.end():] if header else text
        for m in _NAAIM_ROW_RE.finditer(scan_from):
            iso = _parse_naaim_date(m.group(1))
            if not iso:
                continue
            try:
                val = float(m.group(2))
            except ValueError:
                continue
            if val < -200 or val > 300:
                continue
            if iso in seen:
                continue
            seen.add(iso)
            rows.append((iso, val))

    # Chart fallback. Use the FIRST chart block we encounter — that's
    # NAAIM Number, not the S&P 500 series (which would also be matched
    # by the regex). We bound the scan to the section before the
    # ``drawSpChart`` / ``S&P 500`` marker.
    if not rows:
        chart_section = html_text
        sp_idx = chart_section.lower().find("drawspchart")
        if sp_idx == -1:
            sp_idx = chart_section.find("S&P 500")
        if sp_idx > 0:
            chart_section = chart_section[:sp_idx]
        for m in _NAAIM_CHART_RE.finditer(chart_section):
            year = int(m.group(1))
            month0 = int(m.group(2))
            day = int(m.group(3))
            try:
                val = float(m.group(4))
            except ValueError:
                continue
            if val < -200 or val > 300:
                continue
            try:
                iso = _dt.date(year, month0 + 1, day).isoformat()
            except ValueError:
                continue
            if iso in seen:
                continue
            seen.add(iso)
            rows.append((iso, val))

    # Newest first.
    rows.sort(key=lambda r: r[0], reverse=True)
    return rows


def fetch_naaim_exposure(
    cache_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Fetch the most recent NAAIM Exposure Index reading.

    Returns ``None`` on failure. Cache TTL: 24h, keyed by today's ISO date.
    """
    cache = _cache_for(cache_dir)
    today = _today_iso()
    cache_key = f"naaim/latest/{today}"

    hit = None
    try:
        hit = cache.get(cache_key)
    except Exception:
        hit = None
    if hit is not None and hit.value is not None:
        return dict(hit.value)

    text = _http_get(NAAIM_EXPOSURE_URL)
    if not text:
        _log("NAAIM page fetch failed")
        return None

    rows = _parse_naaim_html(text)
    if not rows:
        _log("NAAIM page parsed empty")
        return None

    latest_date, latest_val = rows[0]
    history_vals = [v for _, v in rows]
    mean_4w = _mean(history_vals[:4]) if len(history_vals) >= 1 else None
    # Use up to 52 readings for the 1y percentile.
    pctile_1y = (
        _percentile(latest_val, history_vals[:52])
        if len(history_vals) >= 4
        else None
    )

    result: Dict[str, Any] = {
        "exposure": float(latest_val),
        "mean_4w": float(mean_4w) if mean_4w is not None else None,
        "percentile_1y": float(pctile_1y) if pctile_1y is not None else None,
        "regime": _naaim_regime(float(latest_val)),
        "week_ending": latest_date,
    }
    try:
        cache.set(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)
    except Exception as e:
        _log(f"naaim cache write suppressed: {type(e).__name__}")
    return dict(result)


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


def _composite_regime(
    put_call: Optional[Dict[str, Any]],
    aaii: Optional[Dict[str, Any]],
    naaim: Optional[Dict[str, Any]],
) -> str:
    """Roll up a coarse "cautious vs euphoric" tone across the three signals.

    Counts how many of the three are flashing a defensive regime token:

        put_call.regime  in {elevated_hedging, panic}
        aaii.regime      in {bearish, extreme_bearish}
        naaim.regime     in {defensive}

    And how many are flashing a euphoric token:

        put_call.regime  in {complacent}
        aaii.regime      in {extreme_bullish}
        naaim.regime     in {leveraged}

    Output:
        "extreme_caution" : >=2 defensive, 0 euphoric
        "cautious"        : 1 defensive, 0 euphoric
        "euphoric"        : >=2 euphoric, 0 defensive
        "complacent"      : 1 euphoric, 0 defensive
        "mixed"           : both sides present, or all three missing
        "neutral"         : everything in the middle bucket
    """
    defensive = 0
    euphoric = 0
    counted = 0

    for src, defensive_set, euphoric_set in (
        (put_call, _DEFENSIVE_PUTCALL, {"complacent"}),
        (aaii, _DEFENSIVE_AAII, {"extreme_bullish"}),
        (naaim, _DEFENSIVE_NAAIM, {"leveraged"}),
    ):
        if not src:
            continue
        counted += 1
        regime = src.get("regime")
        if regime in defensive_set:
            defensive += 1
        elif regime in euphoric_set:
            euphoric += 1

    if counted == 0:
        return "mixed"
    if defensive and euphoric:
        return "mixed"
    if defensive >= 2:
        return "extreme_caution"
    if defensive == 1:
        return "cautious"
    if euphoric >= 2:
        return "euphoric"
    if euphoric == 1:
        return "complacent"
    return "neutral"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fetch_breadth_sentiment(
    cache_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Fetch all three breadth/sentiment indicators in one call.

    Returns a dict shaped::

        {
          "as_of": "YYYY-MM-DD",
          "put_call": {...} | None,
          "aaii": {...} | None,
          "naaim": {...} | None,
          "composite_regime": str,
          "data_quality": {
              "sources_ok": int,
              "sources_failed": int,
              "failed_sources": [str, ...],
          },
        }

    NEVER raises — every source is wrapped in a broad try/except so a
    feed outage degrades to ``None`` for that key only.
    """
    failures: List[str] = []

    def _safe(name: str, fn: Any) -> Optional[Dict[str, Any]]:
        try:
            result = fn(cache_dir=cache_dir)
        except Exception as exc:  # noqa: BLE001 — last-line-of-defense
            _log(f"{name} fetch raised: {exc}")
            failures.append(name)
            return None
        if result is None:
            failures.append(name)
        return result

    put_call = _safe("put_call", fetch_put_call)
    aaii = _safe("aaii", fetch_aaii_sentiment)
    naaim = _safe("naaim", fetch_naaim_exposure)

    sources_ok = sum(1 for r in (put_call, aaii, naaim) if r is not None)
    composite = _composite_regime(put_call, aaii, naaim)

    return {
        "as_of": _today_iso(),
        "put_call": put_call,
        "aaii": aaii,
        "naaim": naaim,
        "composite_regime": composite,
        "data_quality": {
            "sources_ok": sources_ok,
            "sources_failed": len(failures),
            "failed_sources": failures,
        },
    }


__all__ = [
    "CBOE_DAILY_CSV_PREFIX",
    "CBOE_DAILY_HTML",
    "CBOE_EQUITY_PUT_CALL_HTML",
    "AAII_SENTIMENT_URL",
    "NAAIM_EXPOSURE_URL",
    "fetch_breadth_sentiment",
    "fetch_put_call",
    "fetch_aaii_sentiment",
    "fetch_naaim_exposure",
]
