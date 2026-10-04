#!/usr/bin/env python3
"""Enrich Snowflake Summit vendors with live news (Google News RSS) + company facts (Wikidata).

Keyless, free. Reads ``snowflake_summit/vendors.json``; for each of the ~197
vendors it gathers:

  * **Google News RSS search** — recent headlines naming the vendor → merged
    into ``news.json`` in the exact shape the Summit dashboard already renders
    ({vendor, headline, date, url, source, summary, relevance}). No template
    change needed downstream. Replaced GDELT DOC 2.0, which never delivered a
    usable article to this feed (see the diagnosis note above main()).
  * **Wikidata** — founded year, headquarters, employee count, industry, and the
    official website → written to ``enrichment.json`` (keyed by vendor name).
    build.py merges these onto each vendor so the detail sheet shows them.

Both sources need no key. Results are cached (``.enrich_cache.json``) with a TTL
so most CI runs are cache hits — news is cheap to refresh, company facts almost
never change. A transient upstream failure keeps the last good data
(stale-keep) instead of wiping the dashboard, and per-vendor failures are
isolated so one bad lookup never breaks the run.

pages.yml runs this on every deploy and commits ``news.json`` back to the repo
(see the "Commit Summit news feed" step), so the committed file, the deployed
/summit/ page and the data-health monitor all see the same feed.

    python snowflake_summit/enrich_vendors.py
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDORS_PATH = HERE / "vendors.json"
NEWS_PATH = HERE / "news.json"
ENRICH_PATH = HERE / "enrichment.json"
CACHE_PATH = HERE / ".enrich_cache.json"

# Identifies the project to every upstream (Google News, Wikidata).
_UA = "alpine-data (+https://github.com/btabiado/alpine-data)"
GNEWS_RSS = "https://news.google.com/rss/search"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Cache TTLs (seconds). News refreshes a few times a day; company facts (founded,
# HQ, employees) change rarely, so they get a long TTL to keep CI cheap. With
# hourly deploys a 12h news TTL means each run re-queries only the ~1/12 of
# vendors whose entry expired, which is what keeps the request rate polite.
NEWS_TTL = 12 * 3600
WD_TTL = 30 * 24 * 3600
# Cache slot for news. GDELT entries lived under "news"; a new slot means a
# restored CI cache cannot replay GDELT's phrase-match junk as if it were fresh.
NEWS_CACHE_KEY = "gnews"

NEWS_MAX_PER_VENDOR = 4  # headlines kept per vendor per fetch
NEWS_WINDOW_DAYS = 30    # Google News `when:` window, also enforced on parse
NEWS_MIN_TO_WRITE = 8    # don't overwrite curated news.json with a near-empty fetch
MAX_WORKERS = 6
HTTP_TIMEOUT = 8.0
# Hard wall-clock budget for the whole enrichment pass. Once exceeded, remaining
# vendors short-circuit to cached data (no network) so the CI build never
# stalls. The cache persists across runs, so a cold first pass that only gets
# partway through is finished by the next run(s) — every vendor lands within a
# few builds without any single build dragging.
ENRICH_BUDGET = 240.0
# Wikidata gets a *stricter* deadline than news. Both used to share one budget
# and Wikidata runs second inside the same per-vendor call, so on a cold cache
# 3 WD requests per vendor ate the wall clock the news fetch needed. News is the
# feed that goes stale; company facts have a 30-day TTL and can wait a run.
WD_BUDGET_SHARE = 0.5

# Google News has no published rate limit, but bursts get HTTP 503 (seen live
# from a dev sandbox after ~20 requests at 1.5s spacing; it cleared within a
# minute). Space requests process-wide so the worker pool cannot burst, and
# retry a throttle with a real backoff rather than hammering.
GNEWS_MIN_INTERVAL = 2.0    # seconds between Google News requests, process-wide
GNEWS_RETRIES = 2
GNEWS_BACKOFF = 6.0         # seconds; multiplied by the attempt number
RETRY_BACKOFF = 1.5         # Wikidata
WD_MIN_INTERVAL = 1.0       # seconds between Wikidata requests, process-wide
WD_RETRIES = 1
# If the upstream is hard-down, stop after this many consecutive transport
# failures instead of burning the whole budget proving it 197 times.
GNEWS_GIVE_UP_AFTER = 8

# The feed is allowed to be quiet, but not silently frozen. If news.json's own
# `generated` date is older than this and this run added nothing, that is an
# alarm regardless of which upstream excuse produced it.
STALE_ALERT_DAYS = 3
# Auto-fetched headlines kept per vendor across runs. Older ones for the same
# vendor are evicted first, so fresh news rotates through without ever pushing
# the hand-curated Summit announcements (which carry summaries) out of the feed.
AUTO_PER_VENDOR = 6
# Absolute bound on news.json. 345 curated + 197 vendors x AUTO_PER_VENDOR fits
# under it, so in practice the per-vendor bound does the work. The cap EVICTS
# THE OLDEST; it used to refuse the newest, which turned a size limit into a
# permanent freeze — see _merge_feed().
NEWS_CAP = 1600

# Wikidata property ids we read.
P_INCEPTION = "P571"
P_HQ = "P159"
P_EMPLOYEES = "P1128"
P_INDUSTRY = "P452"
P_WEBSITE = "P856"


# ------------------------------------------------------------------ call stats
# Every upstream outcome is counted by (tag, reason) so the run can explain
# *why* it has no news instead of just reporting that it has none. Without this
# an HTTP 429/503, a DNS failure, an unparseable body and a genuinely quiet
# news day were all the same thing: `None`.
_STATS_LOCK = threading.Lock()
_OK = collections.Counter()          # tag -> responses that parsed
_FAILS = collections.Counter()       # "tag:reason" -> count
_SAMPLES: dict[str, str] = {}        # "tag:reason" -> first example detail
_ATTEMPTS = collections.Counter()    # tag -> calls attempted
_CONSEC_FAIL = collections.Counter() # tag -> consecutive transport failures


def _note(tag: str, reason: str, detail: str = "") -> None:
    with _STATS_LOCK:
        _ATTEMPTS[tag] += 1
        if reason == "ok":
            _OK[tag] += 1
            _CONSEC_FAIL[tag] = 0
        else:
            key = f"{tag}:{reason}"
            _FAILS[key] += 1
            _CONSEC_FAIL[tag] += 1
            if detail and key not in _SAMPLES:
                _SAMPLES[key] = " ".join(detail.split())[:200]


def _dead(tag: str, limit: int) -> bool:
    """True once `tag` has failed `limit` times in a row — upstream is down."""
    with _STATS_LOCK:
        return _CONSEC_FAIL[tag] >= limit


# ---------------------------------------------------------------- http helpers
_RETRYABLE = (403, 408, 429, 500, 502, 503, 504)


def _http_get(url: str, timeout: float = HTTP_TIMEOUT, tag: str = "http",
              retries: int = 0, backoff: float = RETRY_BACKOFF,
              accept: str = "application/json") -> "str | None":
    """GET → decoded body, or None on an HTTP/transport failure. Never raises.

    Every failure is *classified and counted* (see `_note`) so an outage shows
    up as "gnews:http_503 x40" in the run log instead of silence. Success is
    NOT counted here: the caller decides whether the body actually parsed.
    Retries only on throttle/5xx, the one class where waiting helps."""
    last_reason, last_detail = "unknown", ""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = (e.read() or b"").decode("utf-8", "replace")
            except OSError:
                # The body is a diagnostic nicety. If the stream is already
                # consumed or the socket died, we still have e.code, which is
                # the part that drives retry/reporting.
                pass
            last_reason, last_detail = f"http_{e.code}", body
            if e.code in _RETRYABLE and attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            _note(tag, last_reason, last_detail)
            return None
        except Exception as e:  # URLError, socket timeout, DNS, TLS, ...
            inner = getattr(e, "reason", e)
            last_reason = "timeout" if isinstance(inner, TimeoutError) else "network"
            last_detail = f"{type(e).__name__}: {inner}"
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            _note(tag, last_reason, last_detail)
            return None
    _note(tag, last_reason, last_detail)
    return None


def _get_json(url: str, timeout: float = HTTP_TIMEOUT, tag: str = "http", retries: int = 0):
    """GET → parsed JSON, or None on failure. Never raises.

    An empty 200 body is an ANSWER (zero results), not a failure: counting it as
    `non_json` made a quiet upstream indistinguishable from a broken one."""
    text = _http_get(url, timeout=timeout, tag=tag, retries=retries)
    if text is None:
        return None
    if not text.strip():
        _note(tag, "ok")
        return {}
    try:
        data = json.loads(text, strict=False)
    except ValueError as e:
        # A rejected query or exhausted quota sometimes arrives as HTTP 200 with
        # a plain-text body. That body is the most useful diagnostic there is.
        _note(tag, "non_json", f"{e} | body={text.strip()[:180]}")
        return None
    _note(tag, "ok")
    return data


# ------------------------------------------------------------ request pacing
class _Gate:
    """Process-wide minimum spacing between requests to one upstream, so the
    worker pool cannot burst."""

    def __init__(self, interval: float):
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            gap = self._next - now
            if gap > 0:
                time.sleep(gap)
                now += gap
            self._next = now + self.interval


_GNEWS_GATE = _Gate(GNEWS_MIN_INTERVAL)
# Wikidata answered HTTP 429 to ~half of an unpaced 6-worker sweep (checked
# live 2026-10-04), which left most vendors without facts.
_WD_GATE = _Gate(WD_MIN_INTERVAL)


# ------------------------------------------------------------ google news query
# Search terms for directory names that are not what the press calls the
# company (legal suffixes, "an IBM Company", a domain name). Several terms are
# OR-ed in the query and any of them may match the headline.
SEARCH_TERMS: dict[str, list[str]] = {
    "Hakkoda (an IBM Company)": ["Hakkoda"],
    "Mendix (a Siemens Business)": ["Mendix"],
    "Wipro Limited": ["Wipro"],
    "Validio AB": ["Validio"],
    "Glean Technologies": ["Glean"],
    "TEKSystems Global Services": ["TEKsystems"],
    "Prefect Technologies": ["Prefect"],
    "Artie Technologies": ["Artie"],
    "paradime.io": ["Paradime"],
    "Timbrai": ["timbr.ai", "Timbr"],
    "Dagster Labs": ["Dagster"],
    "Astrato Analytics": ["Astrato"],
    "Hevo Data": ["Hevo"],
    "Seemore Data": ["Seemore"],
    "Mastech Digital": ["Mastech"],
    "MaxMyCloud AI": ["MaxMyCloud"],
    "Treasure AI": ["Treasure AI", "Treasure Data"],
    "Sigma": ["Sigma Computing", "Sigma"],
    "LTM": ["LTIMindtree", "LTM"],
    "Kipi.ai": ["Kipi.ai", "Kipi"],
}

# Household names whose news volume is mostly unrelated to this directory.
# Their query requires a Snowflake mention, which keeps the feed on-topic for a
# Summit partner page instead of filling it with generic Microsoft headlines.
SNOWFLAKE_ONLY: frozenset[str] = frozenset({
    "AWS", "Microsoft", "Google Cloud", "IBM", "SAP", "Salesforce", "OpenAI",
    "Capgemini", "Cognizant", "Infosys", "KPMG", "Wipro Limited", "NTT DATA",
    "EPAM", "Genpact", "CDW", "ServiceNow", "S&P Global", "TransUnion",
    "The Trade Desk", "Dun & Bradstreet", "FactSet", "Crunchbase", "LTM",
    "Capital One Software", "Hexaware", "Mastek", "Slalom",
})

# Names that are ordinary words, places or surnames ("Coastal", "Chalk",
# "Monte Carlo", "Redpanda"). Their query must also hit Snowflake or one of the
# vendor's own product terms (CONTEXT_TERMS), and the headline must name them
# AND carry a data/AI context word. A generic "data OR AI" query was tried
# first and still returned coastal-flooding and Sigma Lithium stories.
AMBIGUOUS: frozenset[str] = frozenset({
    "Arango", "Archetype", "Artie Technologies", "Astronomer", "Atlan",
    "Atrium", "Bigeye", "Bruin", "Chalk", "Coalesce", "Coastal", "DataHub",
    "edata", "Elementum", "Estuary", "Euno", "Flexor", "Foundational",
    "Glean Technologies", "Gray Swan", "Hex", "Honeydew", "Insider One",
    "Kumo", "Matia", "Maxa", "Merkle", "Meta Integration", "Monte Carlo",
    "NICE", "Posit", "Precisely", "Precog", "Prefect Technologies", "Promethium",
    "Prophecy", "Quest", "Redpanda", "Reducto", "Reflex", "Remix", "Retool",
    "Row Zero", "Safe Software", "SELECT", "Seemore Data", "Sigma", "Snowplow",
    "Solid Data", "Starburst", "Steep", "Sundial", "Yuki",
    # Added after the first full sweep: a Syrian town (Tal Tamr), a surname,
    # a Kyrgyz gold deposit (El Domo) and a UAE student platform.
    "Tamr", "Sifflet", "Domo", "Sparq",
})

# Product terms that identify an AMBIGUOUS vendor's own coverage, OR-ed with
# "Snowflake" in its query. Vendors not listed fall back to Snowflake alone.
CONTEXT_TERMS: dict[str, list[str]] = {
    "Arango": ["ArangoDB", "graph database"],
    "Artie Technologies": ["change data capture", "data streaming"],
    "Astronomer": ["Airflow"],
    "Atlan": ["metadata", "data catalog", "governance"],
    "Bigeye": ["data observability"],
    "Chalk": ["feature store", "Chalk AI"],
    "Coalesce": ["data transformation", "Coalesce.io"],
    "DataHub": ["metadata", "data catalog"],
    "Elementum": ["supply chain", "workflow automation"],
    "Estuary": ["Estuary Flow", "change data capture"],
    "Glean Technologies": ["enterprise search", "Work AI"],
    "Hex": ["Hex Technologies", "data notebook"],
    "Kumo": ["Kumo AI", "graph neural network"],
    "Merkle": ["dentsu"],
    "Monte Carlo": ["data observability", "AI observability"],
    "NICE": ["CXone", "contact center"],
    "Posit": ["RStudio", "Positron"],
    "Precisely": ["data integrity"],
    "Prefect Technologies": ["workflow orchestration", "Prefect Cloud"],
    "Promethium": ["data fabric"],
    "Prophecy": ["data engineering", "data prep"],
    "Quest": ["Quest Software", "erwin"],
    "Redpanda": ["Kafka", "streaming data"],
    "Reducto": ["document parsing", "OCR"],
    "Reflex": ["Python web apps", "Reflex.dev"],
    "Retool": ["internal tools", "low-code"],
    "Sigma": ["Sigma Computing", "business intelligence"],
    "Snowplow": ["behavioral data", "customer data"],
    "Starburst": ["Trino", "lakehouse"],
    "Tamr": ["entity resolution", "master data"],
    "Sifflet": ["data observability"],
    "Domo": ["Progress Software", "business intelligence"],
}

# Known collisions, excluded in the query and again on the headline.
NEGATIVE_TERMS: dict[str, list[str]] = {
    "Sigma": ["Two Sigma", "Six Sigma", "Sigma Lithium", "Sigma Alpha", "Phi Sigma"],
    "Monte Carlo": ["Monte Carlo simulation", "Monte Carlo method",
                    "Monte Carlo integration", "Monte Carlo tree"],
    "Coastal": ["Coastal Carolina", "Coastal Financial", "Coastal Bridge"],
    "Hex": ["Jonah Hex", "hex key"],
    "Quest": ["Quest Diagnostics", "Quest Resource"],
    "Atlan": ["Crystal of Atlan", "Marine Atlan"],
    "Redpanda": ["red panda"],
    "Tamr": ["Tal Tamr", "Tel Tamr"],
    "Domo": ["El Domo"],
}

# Title words that make an ambiguous name a data/AI company story.
_CONTEXT_RE = re.compile(
    r"\b(data|analytics|snowflake|databricks|cloud|platform|software|saas|"
    r"startup|funding|raises|series [a-h]|valuation|acqui\w*|enterprise|agents?|"
    r"agentic|llms?|genai|generative|governance|observability|warehouse|"
    r"lakehouse|pipelines?|etl|database|dbt|gartner|forrester|machine learning|"
    r"semantic|catalog|metadata|notebook|business intelligence)\b",
    re.IGNORECASE)
_AI_RE = re.compile(r"\bAI\b")  # case-sensitive: "AI", not "ai" inside words

# Publishers whose "articles" are auto-generated stock, revenue, funding or
# token-price pages, not news. Simply Wall St also uses "Snowflake" as the name
# of its stock-analysis graphic, which defeats the Snowflake-context query for
# big companies; Bybit's hit was a crypto token sharing a vendor's name.
SOURCE_BLOCKLIST: frozenset[str] = frozenset({
    "simply wall st", "simply wall street", "getlatka", "tracxn", "bybit"})


def _terms_for(name: str) -> list[str]:
    return SEARCH_TERMS.get(name) or [name]


def gnews_query(name: str, window_days: int = NEWS_WINDOW_DAYS) -> str:
    """The Google News search string for one vendor."""
    terms = _terms_for(name)
    phrase = " OR ".join(f'"{t}"' for t in terms)
    q = f"({phrase})" if len(terms) > 1 else phrase
    if name in AMBIGUOUS and CONTEXT_TERMS.get(name):
        ctx = " OR ".join(f'"{c}"' if " " in c else c for c in CONTEXT_TERMS[name])
        q += f" (Snowflake OR {ctx})"
    elif name in SNOWFLAKE_ONLY or name in AMBIGUOUS:
        q += " Snowflake"
    for neg in NEGATIVE_TERMS.get(name, []):
        q += f' -"{neg}"'
    return f"{q} when:{window_days}d"


def gnews_url(name: str) -> str:
    return GNEWS_RSS + "?" + urllib.parse.urlencode({
        "q": gnews_query(name), "hl": "en-US", "gl": "US", "ceid": "US:en"})


def _mentions(title: str, term: str) -> bool:
    """Whole-word mention. Case-sensitive whenever the term has a capital, so
    "SiGMA World" is not Sigma and "NICE" is not "nice"; an all-lower-case term
    (tavily, insightsoftware) matches any casing."""
    flags = 0 if any(c.isupper() for c in term) else re.IGNORECASE
    return re.search(r"(?<![\w])" + re.escape(term) + r"(?![\w])", title, flags) is not None


def _rss_day(pub: str) -> str:
    """RFC 822 pubDate → 'YYYY-MM-DD' in UTC, or '' if unparseable."""
    try:
        dt = parsedate_to_datetime((pub or "").strip())
    except (TypeError, ValueError, IndexError):
        return ""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def parse_gnews_rss(xml_text: str, name: str, today: "date | None" = None,
                    window_days: int = NEWS_WINDOW_DAYS,
                    limit: int = NEWS_MAX_PER_VENDOR) -> list[dict]:
    """Google News RSS → this vendor's items in news.json's shape, newest first.

    Raises ValueError on a body that is not RSS, so the caller can count it as
    an upstream failure rather than as a quiet news day. Google matches the
    article body, not just the headline, so every item is re-checked here: the
    headline must name the vendor (and, for an ambiguous name, carry a data/AI
    context word), must fall inside the window, and must not be a known
    collision or an auto-generated stock page. Syndicated copies of one story
    (same headline, different outlet) collapse to one.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"not RSS: {e}") from e
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError(f"not RSS: root <{root.tag}>")
    today = today or datetime.now(timezone.utc).date()
    oldest = (today - timedelta(days=window_days)).isoformat()
    terms = _terms_for(name)
    negatives = [n.lower() for n in NEGATIVE_TERMS.get(name, [])]
    items: list[dict] = []
    seen: set[str] = set()
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src_el = it.find("source")
        source = ((src_el.text if src_el is not None else "") or "").strip()
        if not title or not link:
            continue
        # Google appends " - Publisher" to every title; the dashboard shows the
        # publisher separately.
        if source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].rstrip()
        if source.lower() in SOURCE_BLOCKLIST:
            continue
        if not any(_mentions(title, t) for t in terms):
            continue
        low = title.lower()
        if any(n in low for n in negatives):
            continue
        if name in AMBIGUOUS and not (_CONTEXT_RE.search(title) or _AI_RE.search(title)):
            continue
        day = _rss_day(it.findtext("pubDate") or "")
        if not day or day < oldest or day > today.isoformat():
            continue
        key = re.sub(r"\W+", " ", low).strip()
        if key in seen or link in seen:
            continue
        seen.update((key, link))
        items.append({
            "vendor": name,
            "headline": title,
            "date": day,
            "url": link,
            "source": source,
            "summary": "",  # the RSS carries no abstract; the headline is it
            "relevance": "high" if ("snowflake" in low or "summit" in low) else "medium",
        })
    items.sort(key=lambda n: n["date"], reverse=True)
    return items[:limit]


def gnews_news(name: str) -> "list[dict] | None":
    """Recent headlines for one vendor, [] when there are none, None on failure.

    The None/[] distinction matters: a successful empty answer is cached (so a
    quiet vendor is not re-queried every hourly run), while a failure keeps the
    previous items and is retried next run."""
    if _dead("gnews", GNEWS_GIVE_UP_AFTER):
        return None  # upstream is hard-down; don't spend the budget re-proving it
    _GNEWS_GATE.wait()
    text = _http_get(gnews_url(name), tag="gnews", retries=GNEWS_RETRIES,
                     backoff=GNEWS_BACKOFF,
                     accept="application/rss+xml, application/xml;q=0.9, */*;q=0.1")
    if text is None:
        return None
    try:
        items = parse_gnews_rss(text, name)
    except ValueError as e:
        _note("gnews", "non_rss", f"{e} | body={text.strip()[:180]}")
        return None
    _note("gnews", "ok")
    return items


# --------------------------------------------------------------- wikidata facts
def _wd_get(url: str):
    _WD_GATE.wait()
    return _get_json(url, tag="wikidata", retries=WD_RETRIES)


def _wd_search_qid(name: str) -> str | None:
    url = WIKIDATA_API + "?" + urllib.parse.urlencode({
        "action": "wbsearchentities", "search": name, "language": "en",
        "type": "item", "limit": "1", "format": "json",
    })
    data = _wd_get(url)
    hits = (data or {}).get("search") or []
    return hits[0].get("id") if hits else None


def _claim_value(claims: dict, pid: str):
    """First main-snak datavalue for a property, or None."""
    arr = claims.get(pid) or []
    for c in arr:
        snak = (c.get("mainsnak") or {})
        if snak.get("snaktype") != "value":
            continue
        return (snak.get("datavalue") or {}).get("value")
    return None


def _claim_qids(claims: dict, pid: str) -> list[str]:
    out = []
    for c in claims.get(pid) or []:
        snak = c.get("mainsnak") or {}
        if snak.get("snaktype") != "value":
            continue
        val = (snak.get("datavalue") or {}).get("value") or {}
        qid = val.get("id")
        if qid:
            out.append(qid)
    return out


def _resolve_labels(qids: list[str]) -> dict[str, str]:
    """Batch-resolve a list of QIDs to English labels (one request)."""
    qids = [q for q in dict.fromkeys(qids) if q]
    if not qids:
        return {}
    url = WIKIDATA_API + "?" + urllib.parse.urlencode({
        "action": "wbgetentities", "ids": "|".join(qids[:50]),
        "props": "labels", "languages": "en", "format": "json",
    })
    data = _wd_get(url)
    ents = (data or {}).get("entities") or {}
    out = {}
    for qid, ent in ents.items():
        lbl = (((ent.get("labels") or {}).get("en") or {}).get("value"))
        if lbl:
            out[qid] = lbl
    return out


def wikidata_facts(name: str) -> dict:
    """Company facts from Wikidata, or {} if no confident company match."""
    qid = _wd_search_qid(name)
    if not qid:
        return {}
    url = WIKIDATA_API + "?" + urllib.parse.urlencode({
        "action": "wbgetentities", "ids": qid, "props": "claims",
        "format": "json",
    })
    data = _wd_get(url)
    ent = ((data or {}).get("entities") or {}).get(qid) or {}
    claims = ent.get("claims") or {}

    facts: dict = {}
    # Inception → year.
    inc = _claim_value(claims, P_INCEPTION)
    if isinstance(inc, dict) and inc.get("time"):
        t = inc["time"]  # e.g. '+2014-00-00T00:00:00Z'
        yr = t[1:5] if len(t) >= 5 else ""
        if yr.isdigit():
            facts["founded"] = yr
    # Employees → integer.
    emp = _claim_value(claims, P_EMPLOYEES)
    if isinstance(emp, dict) and emp.get("amount"):
        try:
            facts["employees"] = f"{int(float(str(emp['amount']).lstrip('+'))):,}"
        except (ValueError, TypeError):
            pass
    # Official website.
    site = _claim_value(claims, P_WEBSITE)
    if isinstance(site, str) and site.startswith("http"):
        facts["website"] = site
    # HQ + industry need label resolution.
    ref_qids = _claim_qids(claims, P_HQ)[:1] + _claim_qids(claims, P_INDUSTRY)[:1]
    labels = _resolve_labels(ref_qids) if ref_qids else {}
    hq = _claim_qids(claims, P_HQ)
    if hq and labels.get(hq[0]):
        facts["headquarters"] = labels[hq[0]]
    ind = _claim_qids(claims, P_INDUSTRY)
    if ind and labels.get(ind[0]):
        facts["industry"] = labels[ind[0]]

    # Require at least one org-ish fact, else the search likely matched the wrong
    # entity (a common word, a person, etc.) — drop it.
    if not facts:
        return {}
    facts["wikidata_qid"] = qid
    facts["wikidata_url"] = f"https://www.wikidata.org/wiki/{qid}"
    return facts


# ----------------------------------------------------------------------- cache
def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _fresh(entry: dict, ttl: float, now: float) -> bool:
    return bool(entry) and (now - entry.get("ts", 0)) < ttl


_SKIPPED = collections.Counter()  # "news"/"wd" -> vendors short-circuited by budget


def enrich_one(vendor: dict, cache: dict, now: float,
               deadline: float, wd_deadline: float | None = None,
               fetch_news=None, fetch_wd=None) -> tuple[list[dict], dict, bool]:
    """Return (news_items, wd_facts, news_fetched_live) for one vendor.

    Cache is used where fresh. Past a deadline we skip network and return cached
    data so the pool drains instead of the build hanging on the long tail.
    Wikidata gets the earlier deadline (``wd_deadline``) so a cold cache cannot
    let 3 near-static company-fact requests per vendor starve the news fetch —
    which is the part that actually goes stale.

    A successful news fetch is cached even when it found nothing, so a quiet
    vendor costs one request per NEWS_TTL rather than one per run; only a
    failed fetch (None) falls back to the previous items. ``fetch_news`` /
    ``fetch_wd`` exist so tests can run this without the network."""
    fetch_news = fetch_news or gnews_news
    fetch_wd = fetch_wd or wikidata_facts
    name = (vendor.get("name") or "").strip()
    if not name:
        return [], {}, False
    if wd_deadline is None:
        wd_deadline = deadline
    ent = cache.get(name) or {}
    ent.pop("news", None)  # retired GDELT slot; see NEWS_CACHE_KEY
    live = False

    news_entry = ent.get(NEWS_CACHE_KEY) or {}
    if _fresh(news_entry, NEWS_TTL, now):
        news = news_entry.get("items") or []
    elif time.time() > deadline:
        _SKIPPED["news"] += 1
        news = news_entry.get("items") or []
    else:
        fetched = fetch_news(name)
        if fetched is None:
            news = news_entry.get("items") or []  # stale-keep
        else:
            ent[NEWS_CACHE_KEY] = {"ts": now, "items": fetched}
            news, live = fetched, True

    wd_entry = ent.get("wd") or {}
    if _fresh(wd_entry, WD_TTL, now):
        wd = wd_entry.get("facts") or {}
    elif time.time() > wd_deadline:
        _SKIPPED["wd"] += 1
        wd = wd_entry.get("facts") or {}
    else:
        wd = fetch_wd(name)
        if wd:
            ent["wd"] = {"ts": now, "facts": wd}
        else:
            wd = wd_entry.get("facts") or {}  # stale-keep

    cache[name] = ent
    return news, wd, live


def _annotate(level: str, title: str, message: str) -> None:
    """Emit a GitHub Actions annotation *and* a plain line.

    The Summit step runs with ``continue-on-error: true`` and ``|| echo``, so an
    exit code alone is invisible. A ``::error::`` annotation surfaces on the run
    summary page regardless, and the step summary gives it somewhere durable to
    live. Outside CI this is just a printed line."""
    one = " ".join(message.split())
    print(f"::{level} title={title}::{one}")
    print(f"[enrich] {level.upper()}: {one}")
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"- **{level.upper()}** {title}: {one}\n")
        except OSError:
            # Best-effort cosmetics. The ::warning/::error workflow command and
            # the [enrich] line above already carry the alarm, so a summary
            # file that is absent, read-only or full must not raise out of the
            # ALARM path itself and swallow the signal it exists to emit.
            pass


def _upstream_report(tag: str) -> str:
    """Human-readable outcome breakdown for one upstream."""
    with _STATS_LOCK:
        att, ok = _ATTEMPTS[tag], _OK[tag]
        fails = {k.split(":", 1)[1]: v for k, v in _FAILS.items() if k.startswith(tag + ":")}
        samples = {k: v for k, v in _SAMPLES.items() if k.startswith(tag + ":")}
    if not att:
        return f"{tag}: no calls made (all cache hits or budget-skipped)"
    parts = [f"{tag}: {ok}/{att} ok"]
    if fails:
        parts.append("failures " + ", ".join(f"{r}x{n}" for r, n in sorted(fails.items())))
    for k, v in sorted(samples.items()):
        parts.append(f'first {k} -> "{v}"')
    return " · ".join(parts)


def _item_day(item: dict) -> str:
    """One news item's publication day as YYYY-MM-DD, or "" if it has none."""
    s = str((item or {}).get("date") or "").strip()[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-" and s.replace("-", "").isdigit():
        return s
    return ""


def _feed_data_date(items: list[dict], today: str) -> str:
    """The feed's DATA date: the newest item in it, clamped to today.

    Rule 1 of the freshness contract — report the age of the DATA, not of the
    run. `generated` used to be a straight clock read, which PR #24 narrowed to
    "clock read on a day content moved". That is still the wrong quantity: a run
    that merges one 45-day-old article would stamp the feed with today's
    date and report a two-month-old feed as gathered this morning.

    Newest-item (max) is the right reducer *here specifically* and nowhere else
    in this repo: a news feed is feed-shaped, so the honest claim is "the feed
    has seen nothing more recent than this" — the same rule app.py's
    aiNewsFreshness() applies with fMax. A composite of parallel sources takes
    the OLDEST; a stream takes the newest. Clamped to today because a source
    timezone can hand us a date a few hours in the future, and a feed cannot be
    fresher than now.
    """
    days = [d for d in (_item_day(i) for i in items) if d]
    return min(max(days), today) if days else ""


def _merge_feed(existing: list[dict], fresh: list[dict],
                cap: int = NEWS_CAP) -> "tuple[list[dict], list[dict]]":
    """Merge fresh items into the curated feed under a size cap.

    Returns ``(merged, evicted)``.

    THE CAP EVICTS THE OLDEST. It used to do the opposite — ``room = cap -
    len(existing)`` and ``fresh[:room]`` — which drops the NEWEST items once the
    feed is full and, at exactly ``cap`` items, makes ``added`` permanently 0.
    That is a freeze switch with a timer on it: the feed stops accepting news,
    `generated` stops moving, and the monitor alarms forever with no action that
    can clear it. A size limit must bound the feed, not end it.

    Curated ordering is preserved for everything that survives, so the
    hand-written entries the dashboard relies on keep their sequence.
    """
    merged = list(existing) + list(fresh)
    if len(merged) <= cap:
        return merged, []
    # Rank by publication day, newest first; undated items sort last but keep
    # their relative position (index tiebreak) so curation order is stable.
    order = sorted(range(len(merged)),
                   key=lambda i: (_item_day(merged[i]), -i), reverse=True)
    keep = set(order[:cap])
    return ([merged[i] for i in range(len(merged)) if i in keep],
            [merged[i] for i in range(len(merged)) if i not in keep])


def _is_auto(item: dict) -> bool:
    """True for a headline this script fetched (Google News link), as opposed
    to a hand-curated item, which links straight to the publisher."""
    host = urllib.parse.urlsplit(str((item or {}).get("url") or "")).netloc.lower()
    return host == "news.google.com"


def _bound_auto(items: list[dict], per_vendor: int = AUTO_PER_VENDOR) -> "tuple[list[dict], list[dict]]":
    """Keep each vendor's newest `per_vendor` auto-fetched items; curated items
    are never touched. Returns ``(kept, evicted)`` in the original order.

    Without this, fresh headlines would eventually push the curated Summit
    announcements out through the global cap, because those are dated June
    2026 and every new headline is newer."""
    by_vendor: dict[str, list[int]] = collections.defaultdict(list)
    for i, it in enumerate(items):
        if _is_auto(it):
            by_vendor[str(it.get("vendor") or "")].append(i)
    drop: set[int] = set()
    for idxs in by_vendor.values():
        ranked = sorted(idxs, key=lambda i: (_item_day(items[i]), -i), reverse=True)
        drop.update(ranked[per_vendor:])
    return ([it for i, it in enumerate(items) if i not in drop],
            [it for i, it in enumerate(items) if i in drop])


def _age_days(iso: str) -> int | None:
    try:
        y, m, d = (int(x) for x in iso.split("-")[:3])
        return (date.today() - date(y, m, d)).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# WHY news.json SAT AT 2026-06-04 FOR FOUR MONTHS — and what changed.
#
# CAUSE 1: nothing committed news.json back. pages.yml ran this script, built
#   /summit/ from the result and threw the runner away, so the committed file
#   (the one the data-health monitor reads) was frozen by construction. FIXED:
#   pages.yml now has a "Commit Summit news feed" step after the rebuild.
#
# CAUSE 2: GDELT never produced a usable feed. Checked live on 2026-10-04:
#   api.gdeltproject.org answers HTTP 429 "Please limit requests to one every
#   5 seconds", and this script spaced calls 0.35s apart, so every sweep was
#   throttled. The few articles that did get through (64 across 29 vendors in
#   four months, seen in the deployed /summit/ page) were mostly noise, because
#   GDELT matched the vendor name anywhere in the article text: "Coastal" ->
#   fishing fleets, "Chalk" -> tyre changes, "Atlan" -> Apple TV listings. At
#   the rate GDELT allows, 197 vendors take ~16 minutes against a 6-minute
#   step. REPLACED with Google News RSS search, which answers from GitHub's
#   runners, plus headline-level checks in parse_gnews_rss().
#
# CAUSE 3 (fixed earlier): a zero-match 200 with an EMPTY BODY was counted as
#   `non_json`, so a quiet day and an outage produced identical stats.
#
# CAUSE 4 (fixed earlier): the NEWS_CAP merge refused the newest items instead
#   of evicting the oldest, a freeze switch on a timer. The cap now evicts the
#   oldest, and _bound_auto() rotates auto-fetched headlines per vendor so
#   they can never evict the curated Summit announcements.
# ---------------------------------------------------------------------------


def main() -> int:
    vraw = _load_json(VENDORS_PATH, {})
    vendors = vraw.get("vendors", vraw if isinstance(vraw, list) else [])
    if not vendors:
        print("[enrich] no vendors.json — nothing to do")
        return 0

    cache = _load_json(CACHE_PATH, {})
    cold_cache = not cache
    now = time.time()
    deadline = now + ENRICH_BUDGET
    wd_deadline = now + ENRICH_BUDGET * WD_BUDGET_SHARE

    all_news: list[dict] = []
    enrichment: dict[str, dict] = {}
    ok_news = ok_wd = live_news_vendors = 0

    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(enrich_one, v, cache, now, deadline, wd_deadline): v for v in vendors}
        for fut in cf.as_completed(futs):
            name = (futs[fut].get("name") or "").strip()
            try:
                news, wd, live = fut.result()
            except Exception as e:
                _note("worker", type(e).__name__, str(e))
                news, wd, live = [], {}, False
            if news:
                all_news.extend(news)
                ok_news += 1
            if live:
                live_news_vendors += 1
            if wd:
                enrichment[name] = wd
                ok_wd += 1

    # Sort fetched news newest-first.
    all_news.sort(key=lambda n: n.get("date", ""), reverse=True)
    # When this RUN happened. Kept separate from the feed's data date on
    # purpose, and deliberately not named anything in
    # build_health_status._DATE_KEYS — a run timestamp must never be mistaken
    # for a freshness signal by the watchdog.
    run_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    gathered_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Persist cache (best effort).
    try:
        CACHE_PATH.write_text(json.dumps(cache))
    except OSError:
        # The cache only saves work on the NEXT run; everything needed for
        # THIS run is already in memory. A read-only or full disk should cost
        # us a slow rebuild next time, not this run's enrichment output.
        pass

    # Write enrichment.json (always — accumulates across runs). Wikidata facts
    # (founded year, HQ, headcount) carry no observation date of their own, so
    # the run stamp is the only thing this file can honestly claim.
    ENRICH_PATH.write_text(json.dumps(
        {"generated": run_day, "gathered_at": gathered_at,
         "by_vendor": enrichment}, ensure_ascii=False, indent=1))

    # MERGE fetched items into the existing (curated) feed rather than
    # REPLACING it. Curated items carry summaries the RSS lacks, so a blind
    # overwrite silently degraded the hand-curated feed on every deploy.
    # Preserve every curated item in its curated order; append only URLs not
    # already present, then bound the auto-fetched items per vendor.
    try:
        _data = json.loads(NEWS_PATH.read_text())
    except Exception:
        _data = {}
    existing = _data.get("items") if isinstance(_data, dict) else None
    if not isinstance(existing, list):
        existing = []
    prior_generated = (_data.get("generated") or "") if isinstance(_data, dict) else ""

    seen = {(it.get("url") or it.get("headline") or "").strip() for it in existing}
    fresh = []
    for it in all_news:
        k = (it.get("url") or it.get("headline") or "").strip()
        if k and k not in seen:
            fresh.append(it)
            seen.add(k)
    fresh.sort(key=lambda n: n.get("date", ""), reverse=True)
    pool, evicted = _bound_auto(list(existing) + fresh)
    merged, capped = _merge_feed(pool, [])
    evicted += capped
    kept_keys = {(it.get("url") or it.get("headline") or "").strip() for it in merged}
    # Count what actually SURVIVED the cap, not what we tried to add — an item
    # that arrived and was immediately evicted for being older than everything
    # in the feed did not move the feed forward and must not claim to have.
    added = sum(1 for it in fresh
                if (it.get("url") or it.get("headline") or "").strip() in kept_keys)

    news_attempts = _ATTEMPTS["gnews"]
    news_ok = _OK["gnews"]
    # A transport-level wipeout is a different animal from "the news was quiet".
    upstream_down = news_attempts > 0 and news_ok == 0
    upstream_degraded = news_attempts > 0 and news_ok < news_attempts * 0.5

    # Only WRITE when we actually have something to add. The old code stamped
    # `generated` with today's date on every write, including "+0 new" writes,
    # so a permanently frozen feed still advertised itself as gathered today.
    # `generated` now moves only when the content moves — AND it is no longer a
    # clock read at all: it is the newest item date in the feed (see
    # _feed_data_date). The run's own timestamp lives in `gathered_at`, which
    # the watchdog deliberately does not treat as a freshness signal.
    if added and len(all_news) >= NEWS_MIN_TO_WRITE:
        NEWS_PATH.write_text(json.dumps(
            {"generated": _feed_data_date(merged, run_day),
             "gathered_at": gathered_at,
             "items": merged}, ensure_ascii=False, indent=1))
        news_status = (f"merged news.json (+{added} new from {ok_news} vendors, "
                       f"{len(merged)} total"
                       + (f", {len(evicted)} older auto-fetched items evicted"
                          if evicted else "") + ")")
        preserved = False
    else:
        if len(all_news) < NEWS_MIN_TO_WRITE:
            why = (f"only {len(all_news)} items gathered across {len(vendors)} vendors "
                   f"— below threshold {NEWS_MIN_TO_WRITE}")
        elif not fresh:
            why = f"{len(all_news)} items gathered but all {len(all_news)} already in the feed"
        else:
            why = (f"all {len(fresh)} new items were older than what the feed "
                   f"already holds for their vendors, so none survived the "
                   f"{AUTO_PER_VENDOR}-per-vendor / {NEWS_CAP}-item bounds")
        news_status = f"PRESERVED existing news.json ({why})"
        preserved = True

    stale_days = _age_days(prior_generated) if preserved else 0

    # ------------------------------------------------------------------ signal
    # Preserving is a legitimate action; preserving *silently* is the bug. Every
    # preserve now says so with the upstream evidence attached, and anything
    # that has been preserving for more than STALE_ALERT_DAYS is an error, not
    # a note — that is the condition that let this feed sit frozen for 60 days.
    diag = " | ".join(p for p in (_upstream_report("gnews"), _upstream_report("wikidata")) if p)
    if preserved:
        detail = (f"{news_status}; feed last gathered {prior_generated or 'unknown'}"
                  + (f" ({stale_days}d ago)" if stale_days is not None else "")
                  + f". {diag}")
        if upstream_down:
            detail += (" — every Google News call failed at the transport/parse layer, so "
                       "this is an upstream outage, not a quiet news day.")
        elif upstream_degraded:
            detail += " — majority of Google News calls failed; treat as a partial outage."
        if cold_cache:
            detail += " Cache was cold (.enrich_cache.json missing/empty)."
        if _SKIPPED["news"]:
            detail += f" {_SKIPPED['news']} vendors skipped on the {ENRICH_BUDGET:.0f}s budget."
        hard = upstream_down or (stale_days is not None and stale_days >= STALE_ALERT_DAYS)
        _annotate("error" if hard else "warning", "Summit news feed not refreshed", detail)
    else:
        print(f"[enrich] {news_status} · {diag}")

    print(f"[enrich] {len(vendors)} vendors · {news_status} · "
          f"vendors fetched live: {live_news_vendors} · "
          f"enrichment.json: {ok_wd} vendors with Wikidata facts")

    # Non-zero exit so the workflow step can go red. pages.yml still runs this
    # under `|| echo` so a news outage never blocks the deploy; the ::error::
    # annotation above is what surfaces it.
    return 1 if (preserved and (upstream_down or (stale_days or 0) >= STALE_ALERT_DAYS)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
