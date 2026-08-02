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

import collections
import concurrent.futures as cf
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
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

# GDELT's DOC endpoint is keyless and throttled per source IP; GitHub-hosted
# runners share heavily-used IPs. Firing 197 requests through 6 workers with no
# spacing is exactly the shape that gets an IP throttled, and a throttled GDELT
# answers with HTTP 429 *or* an HTTP-200 plain-text error page — which the old
# blind `except: return None` turned into "0 articles" indistinguishable from
# "no news today". Space the calls out and retry once on a throttle.
GDELT_MIN_INTERVAL = 0.35   # seconds between GDELT requests, process-wide
GDELT_RETRIES = 1
RETRY_BACKOFF = 1.5
# If the upstream is hard-down, stop after this many consecutive transport
# failures instead of burning the whole budget proving it 197 times.
GDELT_GIVE_UP_AFTER = 25

# The feed is allowed to be quiet, but not silently frozen. If news.json's own
# `generated` date is older than this and this run added nothing, that is an
# alarm regardless of which upstream excuse produced it.
STALE_ALERT_DAYS = 3
NEWS_CAP = 500           # max items retained in news.json

# Wikidata property ids we read.
P_INCEPTION = "P571"
P_HQ = "P159"
P_EMPLOYEES = "P1128"
P_INDUSTRY = "P452"
P_WEBSITE = "P856"


# ------------------------------------------------------------------ call stats
# Every upstream outcome is counted by (tag, reason) so the run can explain
# *why* it has no news instead of just reporting that it has none. Without this
# an HTTP 429, a DNS failure, a GDELT plain-text error page and a genuinely
# quiet news day were all the same thing: `None`.
_STATS_LOCK = threading.Lock()
_OK = collections.Counter()          # tag -> responses that parsed as JSON
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
def _get_json(url: str, timeout: float = HTTP_TIMEOUT, tag: str = "http", retries: int = 0):
    """GET → parsed JSON, or None on failure. Never raises.

    Unlike the original bare ``except Exception: return None``, every failure
    mode is *classified and counted* (see `_note`) so a 60-day outage shows up
    as "gdelt:http_429 x197" in the run log instead of silence. Retries only on
    throttle/5xx, which is the one class where waiting actually helps."""
    last_reason, last_detail = "unknown", ""
    for attempt in range(retries + 1):
        raw = None
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": _UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = (e.read() or b"").decode("utf-8", "replace")
            except OSError:
                # The body is a diagnostic nicety — GDELT's error text usually
                # says *why* (rate limit vs bad query). If the stream is
                # already consumed or the socket died, we still have e.code,
                # which is the part that drives retry/reporting. Never let
                # reading the explanation mask the error being explained.
                pass
            last_reason, last_detail = f"http_{e.code}", body
            if e.code in (403, 408, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            _note(tag, last_reason, last_detail)
            return None
        except Exception as e:  # URLError, socket timeout, DNS, TLS, ...
            inner = getattr(e, "reason", e)
            last_reason = "timeout" if isinstance(inner, TimeoutError) else "network"
            last_detail = f"{type(e).__name__}: {inner}"
            if attempt < retries:
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue
            _note(tag, last_reason, last_detail)
            return None

        text = raw.decode("utf-8", "replace")
        try:
            # strict=False: GDELT's ArtList regularly emits raw control chars
            # inside article titles. Under the default strict parser one bad
            # title discards the entire vendor's response.
            data = json.loads(text, strict=False)
        except ValueError as e:
            # GDELT reports a rejected query or an exhausted quota as HTTP 200
            # with a plain-text body. That body is the single most useful
            # diagnostic this script can capture, so keep it.
            _note(tag, "non_json", f"{e} | body={text.strip()[:180]}")
            return None
        _note(tag, "ok")
        return data
    _note(tag, last_reason, last_detail)
    return None


# --------------------------------------------------------------- gdelt throttle
_GDELT_GATE = threading.Lock()
_GDELT_NEXT = 0.0


def _gdelt_wait() -> None:
    """Space GDELT requests process-wide so the worker pool cannot burst."""
    global _GDELT_NEXT
    if GDELT_MIN_INTERVAL <= 0:
        return
    with _GDELT_GATE:
        now = time.monotonic()
        gap = _GDELT_NEXT - now
        if gap > 0:
            time.sleep(gap)
            now += gap
        _GDELT_NEXT = now + GDELT_MIN_INTERVAL


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
    if _dead("gdelt", GDELT_GIVE_UP_AFTER):
        return []  # upstream is hard-down; don't spend the budget re-proving it
    _gdelt_wait()
    data = _get_json(url, tag="gdelt", retries=GDELT_RETRIES)
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
    data = _get_json(url, tag="wikidata")
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
    data = _get_json(url, tag="wikidata")
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
    data = _get_json(url, tag="wikidata")
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
               deadline: float, wd_deadline: float | None = None) -> tuple[list[dict], dict, bool]:
    """Return (news_items, wd_facts, news_is_live) for one vendor.

    Cache is used where fresh. Past a deadline we skip network and return cached
    data so the pool drains instead of the build hanging on the long tail.
    Wikidata gets the earlier deadline (``wd_deadline``) so a cold cache cannot
    let 3 near-static company-fact requests per vendor starve the news fetch —
    which is the part that actually goes stale."""
    name = (vendor.get("name") or "").strip()
    if not name:
        return [], {}, False
    if wd_deadline is None:
        wd_deadline = deadline
    ent = cache.get(name) or {}
    live = False

    news_entry = ent.get("news") or {}
    if _fresh(news_entry, NEWS_TTL, now):
        news = news_entry.get("items") or []
    elif time.time() > deadline:
        _SKIPPED["news"] += 1
        news = news_entry.get("items") or []
    else:
        news = gdelt_news(name)
        if news:  # only refresh cache on a successful (non-empty) fetch
            ent["news"] = {"ts": now, "items": news}
            live = True
        else:
            news = news_entry.get("items") or []  # stale-keep

    wd_entry = ent.get("wd") or {}
    if _fresh(wd_entry, WD_TTL, now):
        wd = wd_entry.get("facts") or {}
    elif time.time() > wd_deadline:
        _SKIPPED["wd"] += 1
        wd = wd_entry.get("facts") or {}
    else:
        wd = wikidata_facts(name)
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


def _age_days(iso: str) -> int | None:
    try:
        y, m, d = (int(x) for x in iso.split("-")[:3])
        return (date.today() - date(y, m, d)).days
    except Exception:
        return None


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

    # Sort fresh GDELT news newest-first.
    all_news.sort(key=lambda n: n.get("date", ""), reverse=True)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Persist cache (best effort).
    try:
        CACHE_PATH.write_text(json.dumps(cache))
    except OSError:
        # The cache only saves work on the NEXT run; everything needed for
        # THIS run is already in memory. A read-only or full disk should cost
        # us a slow rebuild next time, not this run's enrichment output.
        pass

    # Write enrichment.json (always — accumulates across runs).
    ENRICH_PATH.write_text(json.dumps(
        {"generated": generated, "by_vendor": enrichment}, ensure_ascii=False, indent=1))

    # MERGE fresh GDELT items into the existing (curated) feed rather than
    # REPLACING it. Curated items carry summaries that GDELT's ArtList lacks, so
    # a blind overwrite silently degraded the hand-curated feed on every deploy.
    # Preserve EVERY existing item in its curated order; append only GDELT URLs
    # not already present.
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
    room = max(0, NEWS_CAP - len(existing))
    added = min(len(fresh), room)

    gdelt_attempts = _ATTEMPTS["gdelt"]
    gdelt_ok = _OK["gdelt"]
    # A transport-level wipeout is a different animal from "the news was quiet".
    upstream_down = gdelt_attempts > 0 and gdelt_ok == 0
    upstream_degraded = gdelt_attempts > 0 and gdelt_ok < gdelt_attempts * 0.5

    # Only WRITE when we actually have something to add. The old code stamped
    # `generated` with today's date on every write, including "+0 new" writes,
    # so a permanently frozen feed still advertised itself as gathered today.
    # `generated` now moves only when the content moves.
    if added and len(all_news) >= NEWS_MIN_TO_WRITE:
        merged = list(existing) + fresh[:room]
        NEWS_PATH.write_text(json.dumps(
            {"generated": generated, "items": merged}, ensure_ascii=False, indent=1))
        news_status = (f"merged news.json (+{added} new from {ok_news} vendors, "
                       f"{len(merged)} total)")
        preserved = False
    else:
        if len(all_news) < NEWS_MIN_TO_WRITE:
            why = (f"only {len(all_news)} items gathered across {len(vendors)} vendors "
                   f"— below threshold {NEWS_MIN_TO_WRITE}")
        elif not fresh:
            why = f"{len(all_news)} items gathered but all {len(all_news)} already in the feed"
        else:
            why = (f"{len(fresh)} new items dropped: feed is at the {NEWS_CAP}-item cap "
                   f"({len(existing)} existing, room=0)")
        news_status = f"PRESERVED existing news.json ({why})"
        preserved = True

    stale_days = _age_days(prior_generated) if preserved else 0

    # ------------------------------------------------------------------ signal
    # Preserving is a legitimate action; preserving *silently* is the bug. Every
    # preserve now says so with the upstream evidence attached, and anything
    # that has been preserving for more than STALE_ALERT_DAYS is an error, not
    # a note — that is the condition that let this feed sit frozen for 60 days.
    diag = " | ".join(p for p in (_upstream_report("gdelt"), _upstream_report("wikidata")) if p)
    if preserved:
        detail = (f"{news_status}; feed last gathered {prior_generated or 'unknown'}"
                  + (f" ({stale_days}d ago)" if stale_days is not None else "")
                  + f". {diag}")
        if upstream_down:
            detail += (" — every GDELT call failed at the transport/parse layer, so this is "
                       "an upstream outage, not a quiet news day.")
        elif upstream_degraded:
            detail += " — majority of GDELT calls failed; treat as a partial outage."
        if cold_cache:
            detail += " Cache was cold (.enrich_cache.json missing/empty)."
        if _SKIPPED["news"]:
            detail += f" {_SKIPPED['news']} vendors skipped on the {ENRICH_BUDGET:.0f}s budget."
        hard = upstream_down or (stale_days is not None and stale_days >= STALE_ALERT_DAYS)
        _annotate("error" if hard else "warning", "Summit news feed not refreshed", detail)
    else:
        print(f"[enrich] {news_status} · {diag}")

    print(f"[enrich] {len(vendors)} vendors · {news_status} · "
          f"live-news vendors: {live_news_vendors} · "
          f"enrichment.json: {ok_wd} vendors with Wikidata facts")

    # Non-zero exit so the workflow step can go red. Requires dropping the
    # `|| echo` that currently swallows it (see pages.yml).
    return 1 if (preserved and (upstream_down or (stale_days or 0) >= STALE_ALERT_DAYS)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
