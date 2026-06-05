#!/usr/bin/env python3
"""Enrich Snowflake Summit vendors with live news (GDELT) + company facts (Wikidata).

Keyless, free. Reads ``snowflake_summit/vendors.json``; for each of the ~197
vendors it gathers:

  * **GDELT DOC 2.0** — recent news articles mentioning the vendor (+ a Snowflake
    context hint) → written to ``news.json`` in the exact shape the Summit
    dashboard already renders ({vendor, headline, date, url, source, summary,
    relevance}). No template change needed downstream.
  * **Wikidata** — founded year, headquarters, employee count, industry, and the
    official website → written to ``enrichment.json`` (keyed by vendor name).
    build.py merges these onto each vendor so the detail sheet shows them.

Both APIs need no key. Results are cached (``.enrich_cache.json``) with a TTL so
most CI runs are cache hits — news is cheap to refresh, company facts almost
never change. A transient upstream failure keeps the last good data
(stale-keep) instead of wiping the dashboard, and per-vendor failures are
isolated so one bad lookup never breaks the run.

    python snowflake_summit/enrich_vendors.py
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDORS_PATH = HERE / "vendors.json"
NEWS_PATH = HERE / "news.json"
ENRICH_PATH = HERE / "enrichment.json"
CACHE_PATH = HERE / ".enrich_cache.json"

_UA = "BDT-Dashboards/1.0 (Snowflake Summit vendor enrichment; +https://github.com/btabiado/alpine-data)"
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# Cache TTLs (seconds). News refreshes a few times a day; company facts (founded,
# HQ, employees) change rarely, so they get a long TTL to keep CI cheap.
NEWS_TTL = 12 * 3600
WD_TTL = 30 * 24 * 3600

GDELT_MAX = 4            # articles kept per vendor
NEWS_TIMESPAN = "60days"
NEWS_MIN_TO_WRITE = 8    # don't overwrite curated news.json with a near-empty fetch
MAX_WORKERS = 5
HTTP_TIMEOUT = 15.0

# Wikidata property ids we read.
P_INCEPTION = "P571"
P_HQ = "P159"
P_EMPLOYEES = "P1128"
P_INDUSTRY = "P452"
P_WEBSITE = "P856"


# ---------------------------------------------------------------- http helpers
def _get_json(url: str, timeout: float = HTTP_TIMEOUT, retries: int = 1):
    """GET → parsed JSON, or None on any failure (with one polite backoff retry
    on rate-limit / 5xx). Never raises."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except Exception:
            if attempt < retries:
                time.sleep(1.0)
                continue
            return None
    return None


# ------------------------------------------------------------------- gdelt news
def _gdelt_date(seendate: str) -> str:
    """GDELT seendate '20260603T120000Z' → 'YYYY-MM-DD' (best effort)."""
    s = (seendate or "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return ""


def gdelt_news(name: str) -> list[dict]:
    """Recent news items for a vendor, in the dashboard's news.json item shape."""
    # Quote the vendor name as a phrase; add a Snowflake hint to bias toward
    # event-relevant coverage. GDELT ranks by recency (sort=DateDesc).
    query = f'"{name}" (Snowflake OR "data cloud")'
    url = (GDELT_DOC + "?" + urllib.parse.urlencode({
        "query": query, "mode": "ArtList", "maxrecords": str(GDELT_MAX),
        "format": "json", "sort": "DateDesc", "timespan": NEWS_TIMESPAN,
    }))
    data = _get_json(url)
    arts = (data or {}).get("articles") or []
    items: list[dict] = []
    seen_urls: set[str] = set()
    for a in arts:
        u = (a.get("url") or "").strip()
        title = (a.get("title") or "").strip()
        if not u or not title or u in seen_urls:
            continue
        seen_urls.add(u)
        low = title.lower()
        rel = "high" if ("snowflake" in low or "summit" in low) else "medium"
        items.append({
            "vendor": name,
            "headline": title,
            "date": _gdelt_date(a.get("seendate", "")),
            "url": u,
            "source": (a.get("domain") or "").strip(),
            "summary": "",  # GDELT ArtList has no abstract; headline carries it
            "relevance": rel,
        })
    return items


# --------------------------------------------------------------- wikidata facts
def _wd_search_qid(name: str) -> str | None:
    url = WIKIDATA_API + "?" + urllib.parse.urlencode({
        "action": "wbsearchentities", "search": name, "language": "en",
        "type": "item", "limit": "1", "format": "json",
    })
    data = _get_json(url)
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
    data = _get_json(url)
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
    data = _get_json(url)
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


def enrich_one(vendor: dict, cache: dict, now: float) -> tuple[list[dict], dict]:
    """Return (news_items, wd_facts) for one vendor, using cache where fresh."""
    name = (vendor.get("name") or "").strip()
    if not name:
        return [], {}
    ent = cache.get(name) or {}

    news_entry = ent.get("news") or {}
    if _fresh(news_entry, NEWS_TTL, now):
        news = news_entry.get("items") or []
    else:
        news = gdelt_news(name)
        if news:  # only refresh cache on a successful (non-empty) fetch
            ent["news"] = {"ts": now, "items": news}
        else:
            news = news_entry.get("items") or []  # stale-keep

    wd_entry = ent.get("wd") or {}
    if _fresh(wd_entry, WD_TTL, now):
        wd = wd_entry.get("facts") or {}
    else:
        wd = wikidata_facts(name)
        if wd:
            ent["wd"] = {"ts": now, "facts": wd}
        else:
            wd = wd_entry.get("facts") or {}  # stale-keep

    cache[name] = ent
    return news, wd


def main() -> int:
    vraw = _load_json(VENDORS_PATH, {})
    vendors = vraw.get("vendors", vraw if isinstance(vraw, list) else [])
    if not vendors:
        print("[enrich] no vendors.json — nothing to do")
        return 0

    cache = _load_json(CACHE_PATH, {})
    now = time.time()

    all_news: list[dict] = []
    enrichment: dict[str, dict] = {}
    ok_news = ok_wd = 0

    with cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(enrich_one, v, cache, now): v for v in vendors}
        for fut in cf.as_completed(futs):
            name = (futs[fut].get("name") or "").strip()
            try:
                news, wd = fut.result()
            except Exception:
                news, wd = [], {}
            if news:
                all_news.extend(news)
                ok_news += 1
            if wd:
                enrichment[name] = wd
                ok_wd += 1

    # Sort news newest-first; cap the feed.
    all_news.sort(key=lambda n: n.get("date", ""), reverse=True)
    all_news = all_news[:300]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Persist cache (best effort).
    try:
        CACHE_PATH.write_text(json.dumps(cache))
    except Exception:
        pass

    # Write enrichment.json (always — accumulates across runs).
    ENRICH_PATH.write_text(json.dumps(
        {"generated": generated, "by_vendor": enrichment}, ensure_ascii=False, indent=1))

    # Only overwrite the (curated) news feed when GDELT actually returned a
    # meaningful set — otherwise keep whatever is already there.
    if len(all_news) >= NEWS_MIN_TO_WRITE:
        NEWS_PATH.write_text(json.dumps(
            {"generated": generated, "items": all_news}, ensure_ascii=False, indent=1))
        news_status = f"wrote news.json ({len(all_news)} items from {ok_news} vendors)"
    else:
        news_status = (f"kept existing news.json (only {len(all_news)} fresh items "
                       f"gathered — below threshold {NEWS_MIN_TO_WRITE})")

    print(f"[enrich] {len(vendors)} vendors · {news_status} · "
          f"enrichment.json: {ok_wd} vendors with Wikidata facts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
