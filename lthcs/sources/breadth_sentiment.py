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
    * a polite ``User-Agent`` is sent on every request; CBOE in
      particular 403s default ``python-requests`` UAs
"""

from __future__ import annotations

import csv
import datetime as _dt
import html
import io
import os
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

# CBOE publishes a per-day CSV under this prefix. The filename pattern is
# ``YYYY-MM-DD_volume.csv``. They have occasionally shuffled the path; we
# build URLs lazily so a config override can swap the prefix without code
# changes.
CBOE_DAILY_CSV_PREFIX = (
    "https://cdn.cboe.com/data/us/options/market_statistics/daily/"
)
# HTML fallback if the CSV path 404s. Equity-only put/call table.
CBOE_EQUITY_PUT_CALL_HTML = (
    "https://www.cboe.com/us/options/market_statistics/daily/"
)

# AAII publishes the weekly sentiment-survey results on a public HTML page.
# Free, no login. Layout has changed a few times; the parser scans for
# percentages near the words "Bullish" / "Neutral" / "Bearish" rather
# than relying on a specific table id.
AAII_SENTIMENT_URL = "https://www.aaii.com/sentimentsurvey/sent_results"

# NAAIM Exposure Index — weekly. The page renders a table of historical
# values. We grab the most recent rows from the embedded table.
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
# covers (cboe csv, cboe html fallback, aaii, naaim) in one burst; refill
# at 1/min so a misbehaving caller can't hammer upstream.
_BUCKET = TokenBucket(capacity=4, refill_rate=1.0 / 60.0)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "LTHCS-Dashboard/1.0 (+https://github.com/bryantabiadon/btc-eth-etf-dashboard) "
        "Python-requests"
    ),
    "Accept": "text/csv, text/html;q=0.9, */*;q=0.5",
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
    pipeline.
    """
    if not _BUCKET.try_acquire():
        return None
    try:
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=timeout)
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


def _parse_cboe_csv(text: str) -> List[Tuple[str, float]]:
    """Parse a CBOE daily-volume CSV into (date, p/c ratio) rows.

    The CBOE CSV uses a small preamble before the actual header row, so
    we sniff for a row that contains a ``P/C Ratio``-like column. Any
    rows that fail to parse a float are skipped, not raised.

    The CBOE schema also varies: some files have one P/C column, some
    have multiple ("Total", "Equity", "Index"). We prefer "Total P/C
    Ratio" if present, else fall back to the first column whose header
    matches ``P/C Ratio``.
    """
    if not text:
        return []

    reader = csv.reader(io.StringIO(text))
    header_idx: Optional[int] = None
    date_idx: Optional[int] = None
    header_row: Optional[List[str]] = None

    rows: List[List[str]] = list(reader)
    for i, row in enumerate(rows):
        # Look for a header row that mentions "P/C Ratio" (case-insensitive).
        joined = " ".join(c.strip().lower() for c in row)
        if "p/c ratio" in joined or "p/c" in joined and "ratio" in joined:
            header_row = row
            # Prefer "Total P/C Ratio" else first "P/C Ratio".
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
            # Data begins on the next row.
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


def _cboe_url_for(date: _dt.date) -> str:
    return f"{CBOE_DAILY_CSV_PREFIX}{date.isoformat()}_volume.csv"


def fetch_put_call(cache_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Fetch the latest CBOE Put/Call Ratio with 30d mean + 1y percentile.

    Tries the CSV path for today, then walks back up to 5 business-ish
    days if 404 (markets closed). Returns ``None`` on any unrecoverable
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

    # Walk back up to 6 calendar days to find a published file.
    today_date = _dt.date.today()
    text: Optional[str] = None
    used_date: Optional[_dt.date] = None
    for delta in range(0, 7):
        candidate = today_date - _dt.timedelta(days=delta)
        # Skip weekends — CBOE doesn't publish Sat/Sun files.
        if candidate.weekday() >= 5:
            continue
        url = _cboe_url_for(candidate)
        body = _http_get(url)
        if body and "p/c" in body.lower():
            text = body
            used_date = candidate
            break

    if not text:
        _log("CBOE CSV unavailable for the last 7 days; falling back to None")
        try:
            cache.set(cache_key, None, ttl_seconds=_CACHE_TTL_SECONDS)
        except Exception:
            pass
        return None

    rows = _parse_cboe_csv(text)
    if not rows:
        _log(f"CBOE CSV for {used_date} parsed empty")
        return None

    # Take the most recent value as "latest". Most CBOE daily files only
    # carry one trading day, but we sort defensively.
    sorted_rows = sorted(rows, key=lambda r: r[0])
    latest_date, latest_val = sorted_rows[-1]

    # We don't have a 30d / 1y history in a single daily file. The spec
    # asks for trailing means + percentile; we approximate by pulling
    # the CSVs for the previous ~252 trading days, capped to 5 attempts
    # to keep the burst-budget reasonable. In practice we only need 30
    # for the mean, and the percentile bucket can be approximated from
    # whatever rolling window we have.
    #
    # NOTE: To respect the rate-limit budget in V1 we do NOT walk a full
    # year here. Instead we return percentile_1y=None and mean_30d=None
    # when we only have one day of data — callers can degrade. A future
    # enhancement could cache a rolling 252-day series on disk.
    history_vals = [v for _, v in sorted_rows]
    mean_30d = _mean(history_vals[-30:]) if len(history_vals) >= 5 else None
    pctile_1y = (
        _percentile(latest_val, history_vals[-252:])
        if len(history_vals) >= 20
        else None
    )

    result: Dict[str, Any] = {
        "latest": float(latest_val),
        "mean_30d": float(mean_30d) if mean_30d is not None else None,
        "percentile_1y": float(pctile_1y) if pctile_1y is not None else None,
        "regime": _putcall_regime(float(latest_val)),
        "source": "cboe_daily_csv",
        "last_updated": (
            used_date.isoformat() if used_date is not None else None
        ),
    }

    try:
        cache.set(cache_key, result, ttl_seconds=_CACHE_TTL_SECONDS)
    except Exception:
        pass
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


_AAII_LABEL_PATTERNS = {
    "bullish": re.compile(r"bullish[^0-9%]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%", re.I),
    "neutral": re.compile(r"neutral[^0-9%]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%", re.I),
    "bearish": re.compile(r"bearish[^0-9%]{0,40}([0-9]+(?:\.[0-9]+)?)\s*%", re.I),
}

# A date that looks like "May 14, 2026" or "5/14/2026" or "2026-05-14"
_AAII_DATE_PATTERNS = [
    re.compile(
        r"(?:week ending|as of|reported)[:\s]+([A-Za-z]+\s+\d{1,2},\s*\d{4})",
        re.I,
    ),
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
    re.compile(r"(\d{1,2}/\d{1,2}/\d{4})"),
]


def _strip_html_text(s: str) -> str:
    s = re.sub(r"<script\b[^>]*>.*?</script>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<style\b[^>]*>.*?</style>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_aaii_date(text: str) -> Optional[str]:
    for pat in _AAII_DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        raw = m.group(1)
        # Try several formats.
        for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
            try:
                return _dt.datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                continue
    return None


def _parse_aaii_html(html_text: str) -> Optional[Dict[str, Any]]:
    """Pull bullish/neutral/bearish % and week-ending date from AAII HTML.

    Returns a dict with keys ``bullish_pct``, ``neutral_pct``,
    ``bearish_pct``, ``week_ending`` (any of which may be None if the
    page layout has drifted). Returns ``None`` only if we can't find
    *any* of the three percentages.
    """
    if not html_text:
        return None
    text = _strip_html_text(html_text)
    if not text:
        return None

    out: Dict[str, Any] = {
        "bullish_pct": None,
        "neutral_pct": None,
        "bearish_pct": None,
        "week_ending": _parse_aaii_date(text),
    }
    for key, pat in _AAII_LABEL_PATTERNS.items():
        m = pat.search(text)
        if not m:
            continue
        try:
            out[f"{key}_pct"] = float(m.group(1))
        except ValueError:
            continue

    if (
        out["bullish_pct"] is None
        and out["neutral_pct"] is None
        and out["bearish_pct"] is None
    ):
        return None
    return out


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
        except Exception:
            pass

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
    except Exception:
        pass
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


# Matches a row in NAAIM's history table: a date cell followed by the
# Mean / "NAAIM Number" exposure cell. Tolerant of the surrounding
# layout so a minor template change doesn't break the scrape.
_NAAIM_ROW_PATTERN = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})"
    r"[^0-9\-]{0,200}?(-?\d{1,3}(?:\.\d+)?)",
    re.S,
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

    Scans the page text (HTML-stripped) for date+number pairs. This is
    intentionally fuzzy — NAAIM's WordPress template has changed several
    times and we can't depend on a specific table id. Anything that
    parses as a date next to a -100..300 number is treated as a row.
    """
    if not html_text:
        return []
    text = _strip_html_text(html_text)
    if not text:
        return []

    out: List[Tuple[str, float]] = []
    seen: set = set()
    for m in _NAAIM_ROW_PATTERN.finditer(text):
        date_raw, num_raw = m.group(1), m.group(2)
        iso = _parse_naaim_date(date_raw)
        if not iso:
            continue
        try:
            val = float(num_raw)
        except ValueError:
            continue
        # Plausibility filter: NAAIM exposure is bounded roughly to
        # [-200, 300]. Anything outside is almost certainly a different
        # number (year, percent change, etc.).
        if val < -200 or val > 300:
            continue
        if iso in seen:
            continue
        seen.add(iso)
        out.append((iso, val))

    # Newest first.
    out.sort(key=lambda r: r[0], reverse=True)
    return out


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
    except Exception:
        pass
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
    "CBOE_EQUITY_PUT_CALL_HTML",
    "AAII_SENTIMENT_URL",
    "NAAIM_EXPOSURE_URL",
    "fetch_breadth_sentiment",
    "fetch_put_call",
    "fetch_aaii_sentiment",
    "fetch_naaim_exposure",
]
