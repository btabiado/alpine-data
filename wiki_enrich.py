"""Wikipedia infobox enrichment for AI company metadata.

Reads the curated AI company list from ``data/ai_curated.json``, looks up
each company's English Wikipedia page via the MediaWiki ``parse`` API, parses
the ``{{Infobox company}}`` template, and merges values for ``founded``,
``num_employees``, ``hq`` and ``industry`` back onto the company record.

Cached results are written alongside the input file at
``data/ai_curated_wiki.json`` with a ``fetched_at`` timestamp per company.
Entries fresher than ``CACHE_TTL_SECONDS`` are reused.

Design rules:
  * stdlib-only HTTP (``urllib.request``) so we don't widen ``requirements.txt``.
  * Polite User-Agent — Wikipedia rate-limits anonymous traffic without one.
  * Always fall back to the hardcoded curated values; never raise.
  * Funding-round fields (size, date, stage) are not in Wikipedia infoboxes,
    so we leave the existing curated values untouched.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
CACHE_FILE = DATA_DIR / "ai_curated_wiki.json"

USER_AGENT = (
    "etf-flow-dashboard/1.0 wiki-enrich "
    "(https://github.com/local; contact=local) python-urllib"
)

# Re-fetch entries older than seven days.
CACHE_TTL_SECONDS = 7 * 24 * 3600

# Override Wikipedia article titles where the company name doesn't match the
# article slug. Only entries that need disambiguation belong here.
WIKI_TITLE_OVERRIDES: dict[str, str] = {
    "xAI": "XAI (company)",
    "Mistral AI": "Mistral AI",
    "Character.AI": "Character.ai",
    "Inflection AI": "Inflection AI",
    "Figure AI": "Figure (robotics company)",
    "Sakana AI": "Sakana AI",
    "Safe Superintelligence (SSI)": "Safe Superintelligence Inc.",
    "Magic Dev": "Magic (company)",
    "Together AI": "Together AI",
    "Suno": "Suno AI",
    "Runway": "Runway (company)",
    "Sierra": "Sierra (AI company)",
    "Harvey": "Harvey AI",
    "Glean": "Glean (company)",
    "Perplexity": "Perplexity AI",
    "Cohere": "Cohere",
}

# ----------------------------------------------------------------------------
# Wikitext infobox parser
# ----------------------------------------------------------------------------

_INFOBOX_HEAD_RE = re.compile(r"\{\{\s*Infobox\s+company\b", re.IGNORECASE)


def _extract_infobox_block(wikitext: str) -> str | None:
    """Return the raw ``{{Infobox company ...}}`` block, or None.

    Handles nested ``{{...}}`` templates by tracking brace depth.
    """
    if not wikitext:
        return None
    m = _INFOBOX_HEAD_RE.search(wikitext)
    if not m:
        return None
    start = m.start()
    depth = 0
    i = start
    n = len(wikitext)
    while i < n - 1:
        two = wikitext[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return wikitext[start:i]
            continue
        i += 1
    return None


def _split_top_level_pipes(body: str) -> list[str]:
    """Split a template body on ``|`` separators that are at brace/bracket depth 0."""
    parts: list[str] = []
    buf: list[str] = []
    brace = 0
    bracket = 0
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        two = body[i:i + 2]
        if two == "{{":
            brace += 1
            buf.append("{{")
            i += 2
            continue
        if two == "}}":
            brace -= 1
            buf.append("}}")
            i += 2
            continue
        if two == "[[":
            bracket += 1
            buf.append("[[")
            i += 2
            continue
        if two == "]]":
            bracket -= 1
            buf.append("]]")
            i += 2
            continue
        if ch == "|" and brace == 0 and bracket == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))
    return parts


# Strip simple wiki markup so callers see plain values.
_REF_RE = re.compile(r"<ref[^>]*?/>|<ref[^>]*?>.*?</ref>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BRACE_TEMPLATE_RE = re.compile(r"\{\{[^{}]*\}\}")
_WIKILINK_PIPED_RE = re.compile(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]")
_WIKILINK_BARE_RE = re.compile(r"\[\[([^\[\]]+)\]\]")


def _clean_value(raw: str) -> str:
    """Reduce wikitext markup to a plain string."""
    s = raw or ""
    s = _COMMENT_RE.sub("", s)
    s = _REF_RE.sub("", s)
    # Repeatedly collapse innermost templates until none remain.
    for _ in range(6):
        new = _BRACE_TEMPLATE_RE.sub("", s)
        if new == s:
            break
        s = new
    s = _WIKILINK_PIPED_RE.sub(lambda m: m.group(2), s)
    s = _WIKILINK_BARE_RE.sub(lambda m: m.group(1), s)
    s = _HTML_TAG_RE.sub(" ", s)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    # Collapse whitespace runs.
    s = re.sub(r"\s+", " ", s).strip()
    # Trim trailing wiki list markers.
    s = s.strip(" ,;:|*")
    return s


def parse_infobox(wikitext: str) -> dict[str, str]:
    """Parse an Infobox company block into a flat ``{field: cleaned_value}`` dict.

    Returns ``{}`` if no infobox is found. Never raises on malformed input.
    """
    try:
        block = _extract_infobox_block(wikitext)
        if not block:
            return {}
        # Strip the leading ``{{Infobox company`` and trailing ``}}``.
        body = block[2:-2]  # drop outer braces
        # Drop the leading "Infobox company" header by splitting once.
        # Anything before the first pipe is the template name.
        parts = _split_top_level_pipes(body)
        if not parts:
            return {}
        # parts[0] is the template name segment ("Infobox company"); skip it.
        out: dict[str, str] = {}
        for raw in parts[1:]:
            if "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            key = key.strip().lower()
            cleaned = _clean_value(value)
            if key and cleaned:
                out[key] = cleaned
        return out
    except Exception as e:  # defensive: never crash the build
        print(f"  [wiki-enrich] parse_infobox failed: {e}", file=sys.stderr)
        return {}


# ----------------------------------------------------------------------------
# Field extraction (founded year, num_employees, hq, industry)
# ----------------------------------------------------------------------------

_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_NUMBER_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


def extract_founded_year(value: str) -> int | None:
    if not value:
        return None
    m = _YEAR_RE.search(value)
    if not m:
        return None
    try:
        year = int(m.group(1))
    except ValueError:
        return None
    now_year = datetime.now(tz=timezone.utc).year
    if 1800 <= year <= now_year + 1:
        return year
    return None


def extract_num_employees(value: str) -> int | None:
    """Pull a representative employee count from a free-form infobox value.

    Wikipedia values can look like ``"1,500 (2024)"`` or ``"~3,000"`` or
    ``"500–700"``. We pick the largest integer mentioned (so ranges resolve
    to the upper bound) and ignore the year-in-parens.
    """
    if not value:
        return None
    # Strip any (YEAR) suffix.
    stripped = re.sub(r"\((?:c\.?\s*)?(?:19|20)\d{2}\)", "", value).strip()
    candidates: list[int] = []
    for m in _NUMBER_RE.finditer(stripped):
        token = m.group(1).replace(",", "")
        try:
            n = int(float(token))
        except ValueError:
            continue
        # Filter out obvious year tokens (1900-2099) that snuck through.
        if 1800 <= n <= 2099 and "." not in token and len(token) == 4:
            continue
        if 1 <= n <= 10_000_000:
            candidates.append(n)
    if not candidates:
        return None
    return max(candidates)


def extract_fields(infobox: dict[str, str]) -> dict[str, Any]:
    """Pull the subset of fields we care about from a parsed infobox."""
    out: dict[str, Any] = {}
    if not infobox:
        return out
    founded_raw = infobox.get("founded") or infobox.get("foundation") or ""
    year = extract_founded_year(founded_raw)
    if year is not None:
        out["founded_year"] = year
    emp_raw = (
        infobox.get("num_employees")
        or infobox.get("employees")
        or ""
    )
    emp = extract_num_employees(emp_raw)
    if emp is not None:
        out["num_employees"] = emp
    hq = (
        infobox.get("hq_location_country")
        or infobox.get("location_country")
        or infobox.get("hq_location")
        or infobox.get("location")
        or ""
    )
    if hq:
        out["hq"] = hq
    industry = infobox.get("industry") or ""
    if industry:
        out["industry"] = industry
    return out


# ----------------------------------------------------------------------------
# HTTP fetch (MediaWiki parse API)
# ----------------------------------------------------------------------------

WIKIPEDIA_PARSE_ENDPOINT = "https://en.wikipedia.org/w/api.php"


def _http_get_json(url: str, *, timeout: float = 15.0) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read()
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as e:
        print(f"  [wiki-enrich] GET {url} failed: {e}", file=sys.stderr)
        return None


def fetch_wikitext(
    title: str,
    *,
    http_get_json=_http_get_json,
) -> str | None:
    """Fetch raw wikitext for a given article title. Returns None on any error."""
    if not title:
        return None
    qs = urllib.parse.urlencode({
        "action": "parse",
        "page": title,
        "prop": "wikitext",
        "format": "json",
        "redirects": 1,
    })
    url = f"{WIKIPEDIA_PARSE_ENDPOINT}?{qs}"
    payload = http_get_json(url)
    if not isinstance(payload, dict):
        return None
    parse = payload.get("parse") or {}
    wt = (parse.get("wikitext") or {}).get("*")
    if isinstance(wt, str) and wt:
        return wt
    return None


def fetch_company_infobox_fields(
    company_name: str,
    *,
    http_get_json=_http_get_json,
) -> dict[str, Any]:
    """End-to-end: name -> wikitext -> infobox -> {founded_year, ...}.

    Returns ``{}`` on any failure. Tries override title first, falling back to
    the company name itself.
    """
    candidates: list[str] = []
    override = WIKI_TITLE_OVERRIDES.get(company_name)
    if override:
        candidates.append(override)
    if company_name and company_name not in candidates:
        candidates.append(company_name)
    for title in candidates:
        wt = fetch_wikitext(title, http_get_json=http_get_json)
        if not wt:
            continue
        infobox = parse_infobox(wt)
        fields = extract_fields(infobox)
        if fields:
            fields["_wiki_title"] = title
            return fields
    return {}


# ----------------------------------------------------------------------------
# Cache
# ----------------------------------------------------------------------------

def _load_cache(cache_path: Path = CACHE_FILE) -> dict[str, dict]:
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text())
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"  [wiki-enrich] cache load failed: {e}", file=sys.stderr)
    return {}


def _save_cache(cache: dict[str, dict], cache_path: Path = CACHE_FILE) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))
    except Exception as e:
        print(f"  [wiki-enrich] cache save failed: {e}", file=sys.stderr)


def _is_fresh(entry: dict, now: float, ttl: float) -> bool:
    ts = entry.get("fetched_at")
    if not ts:
        return False
    try:
        # Accept either ISO timestamp or epoch seconds.
        if isinstance(ts, (int, float)):
            return (now - float(ts)) < ttl
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        epoch = dt.timestamp()
        return (now - epoch) < ttl
    except Exception:
        return False


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------

def enrich_companies(
    companies: Iterable[dict],
    *,
    cache_path: Path = CACHE_FILE,
    ttl_seconds: float = CACHE_TTL_SECONDS,
    fetcher=fetch_company_infobox_fields,
    now: float | None = None,
) -> list[dict]:
    """Return a new list of company dicts with Wikipedia fields merged in.

    Hardcoded curated values always win — Wikipedia values only fill gaps.
    Cache is keyed by company ``name``. Entries fresher than ``ttl_seconds``
    are reused without an HTTP call.
    """
    now_epoch = float(now) if now is not None else time.time()
    cache = _load_cache(cache_path)
    out: list[dict] = []
    cache_dirty = False
    for raw in companies or []:
        if not isinstance(raw, dict):
            out.append(raw)  # pass through unexpected shapes untouched
            continue
        company = dict(raw)  # shallow copy
        name = str(company.get("name") or "").strip()
        wiki: dict[str, Any] = {}
        if name:
            cached_entry = cache.get(name) or {}
            if _is_fresh(cached_entry, now_epoch, ttl_seconds):
                wiki = dict(cached_entry.get("fields") or {})
            else:
                try:
                    wiki = fetcher(name) or {}
                except Exception as e:
                    print(
                        f"  [wiki-enrich] fetch for {name!r} failed: {e}",
                        file=sys.stderr,
                    )
                    wiki = {}
                cache[name] = {
                    "fetched_at": datetime.fromtimestamp(
                        now_epoch, tz=timezone.utc
                    ).isoformat().replace("+00:00", "Z"),
                    "fields": wiki,
                }
                cache_dirty = True
        # Fill only missing fields; curated wins on conflict.
        for key in ("founded_year", "num_employees", "hq", "industry"):
            if key in wiki and not company.get(key):
                company[key] = wiki[key]
        # Surface the Wikipedia title we used, if any, for debugging.
        if "_wiki_title" in wiki and "wiki_title" not in company:
            company["wiki_title"] = wiki["_wiki_title"]
        out.append(company)
    if cache_dirty:
        _save_cache(cache, cache_path)
    return out


def enrich_ai_curated(curated: dict, **kwargs) -> dict:
    """Enrich the ``top_funded_companies`` list inside a curated dict.

    Returns a shallow copy with the enriched list swapped in. Never raises.
    """
    if not isinstance(curated, dict):
        return curated
    rows = curated.get("top_funded_companies") or []
    try:
        enriched = enrich_companies(rows, **kwargs)
    except Exception as e:
        print(f"  [wiki-enrich] enrich_ai_curated failed: {e}", file=sys.stderr)
        return curated
    out = dict(curated)
    out["top_funded_companies"] = enriched
    return out
