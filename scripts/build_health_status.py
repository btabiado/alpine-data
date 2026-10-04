#!/usr/bin/env python3
"""Emit data/health/status.json — central monitoring snapshot.

Scans data/ and data/.stale/ for file mtimes, classifies each entry against
per-source freshness thresholds, and writes a single JSON the health page
reads. Pure stdlib, no extra deps so the pages.yml step is cheap.

Run from repo root: python scripts/build_health_status.py
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
STALE_DIR = DATA_DIR / ".stale"
SUMMIT_DIR = REPO_ROOT / "snowflake_summit"
OUT_PATH = DATA_DIR / "health" / "status.json"


@dataclass
class Threshold:
    fresh_h: float
    stale_h: float


# Per-source freshness thresholds (hours).
# - fresh: age below this → green
# - stale: age below this → amber; above → red
# Defaults chosen from how often each pipeline actually refreshes:
# market/whale = hourly cron, ETF flows = Farside daily, AI news = a few times
# per day, LTHCS = daily, insights = daily.
DEFAULT = Threshold(fresh_h=6, stale_h=24)
THRESHOLDS: dict[str, Threshold] = {
    # rendered files baked into dashboard.html
    "market.json": Threshold(2, 6),
    "whale.json": Threshold(2, 6),
    "ai_curated.json": Threshold(8, 24),
    "ai_curated_wiki.json": Threshold(8, 24),
    "btc_flows.csv": Threshold(30, 48),
    "eth_flows.csv": Threshold(30, 48),
    "insights_history.json": Threshold(26, 48),
    "shares.json": Threshold(168, 720),  # rarely changes
    # --- feeds that were red on /health/ for reasons that are NOT rot ---
    # A status page that cries wolf gets ignored, which is how a genuinely
    # dead feed (TSA) sat unnoticed for 45 days. These three were falling
    # through to DEFAULT (6h/24h), which does not describe their cadence:
    #
    # equity ETF flows only move on TRADING days, so Friday's row is already
    # ~3 days old by Monday morning and ~4 across a Monday holiday.
    "equity_etf_flows.csv": Threshold(96, 168),
    # data-city.json is judged on cities[].data_health.last_updated (see
    # NESTED_DATE_PATHS), which is the oldest MONTHLY municipal series behind a
    # city — 311 calls, crime reports, building permits. Those publish a month
    # at a time and several lag a further month, so the newest COMPLETE month a
    # healthy city can offer is 28-60 days old depending on where in the month
    # you look. Under the inherited DEFAULT (6h/24h) this feed is unsatisfiable
    # BY CONSTRUCTION: it can never be green, no matter how well the pipeline
    # runs. A permanently red light is how the genuinely dead TSA feed sat
    # unnoticed for 45 days, which is the failure this table exists to prevent.
    #
    # 62d/100d: a one-month-lagging series' month-start sits 59-92 days back,
    # plus cron slack. This deliberately does NOT turn the feed green today —
    # Miami reads 976d because Miami-Dade publishes County 311 only as frozen
    # per-year snapshots and the newest is 2023. That one is a real dead
    # upstream and it SHOULD stay red; the point of this entry is that when it
    # is red, it is red for a true reason.
    "data-city.json": Threshold(62 * 24, 100 * 24),
    # real_estate.json is a once-a-day cron; 12h old is normal, not "stale".
    "real_estate.json": Threshold(30, 48),
    # metro_coords.json is STATIC reference data — Census CBSA gazetteer
    # centroids, refreshed manually when Census publishes a new annual file.
    # It is supposed to be months old.
    "metro_coords.json": Threshold(8760, 17520),  # ~1y / ~2y
    # root-level v2 artifacts
    "data-defi.json": Threshold(8, 24),
    "data-whale.json": Threshold(2, 6),
    # high-frequency upstream caches
    "coinbase_spot.json": Threshold(1, 4),
    "mempool_space.json": Threshold(2, 6),
    "etherscan_gas.json": Threshold(2, 6),
    # daily-ish upstream caches
    "fetch_fred.json": Threshold(30, 72),
    "fetch_sec_form_d_filings.json": Threshold(30, 72),
    "fetch_yc_ai_companies.json": Threshold(168, 720),
    # LTHCS pipeline (daily cron)
    "universe.json": Threshold(26, 48),
    "weights.json": Threshold(26, 48),
    "prewarm_status.json": Threshold(26, 48),
    "13f_institutions.json": Threshold(720, 2160),  # quarterly
    "13f_cusip_map.json": Threshold(720, 2160),  # quarterly
    # Snowflake Summit curated data (committed, refreshed manually/occasionally)
    "vendors.json": Threshold(720, 2160),  # vendor roster — rarely changes
    "news.json": Threshold(168, 336),      # curated vendor news — ~weekly
    "floorplan.json": Threshold(720, 2160),
}

# Maps each rendered data file to the V1 tab(s) that consume it. Drives the
# "per-tab" section on the health page. Multi-tab entries get listed under
# each tab so a glance at any tab's row tells you if its inputs are fresh.
TAB_INPUTS: dict[str, list[str]] = {
    "Crypto": ["market.json"],
    "Crypto Signals": ["market.json"],
    "Whale": ["whale.json", "data-whale.json"],
    "POC": ["market.json"],
    "ETF Flows": ["btc_flows.csv", "eth_flows.csv"],
    "AI News": ["ai_curated.json", "ai_curated_wiki.json"],
    "Research": ["ai_curated.json"],
    "DeFi": ["data-defi.json"],
    "Futures": ["market.json"],
    "Stocks": ["market.json"],
    "LTHCS": ["lthcs/universe.json"],
    "Insights": ["insights_history.json"],
    "Summit": ["snowflake_summit/vendors.json", "snowflake_summit/news.json"],
}


def classify(age_h: float, t: Threshold) -> str:
    if age_h < t.fresh_h:
        return "fresh"
    if age_h < t.stale_h:
        return "stale"
    return "critical"


def humanize_age(age_h: float) -> str:
    if age_h < 1:
        return f"{int(age_h * 60)}m"
    if age_h < 48:
        return f"{age_h:.1f}h"
    return f"{age_h / 24:.1f}d"


# Keys that carry a file's own freshness signal, in priority order.
#
# Matched case- and separator-insensitively (see _norm_key), so `as_of`, `asOf`
# and `AsOf` are the same key. That generality is not cosmetic: this list has
# now drifted behind the data TWICE. Round one was `generated_utc`/`tstr`
# (cfpb, usaspending, opensky silently fell through to mtime). Round two was
# `compiled_at` — data/ai_curated.json's ONLY date field, which meant an
# 80-day-old file reported "no signal" and, in the old watchdog, exited 0 —
# and data-aviation.json's camelCase `asOf`, which the snake-only list could
# not see at all. Spelling should never be the difference between watched and
# unwatched, so the third round of drift is caught by _content_age_probe's
# drift scan below instead of by someone noticing months later.
# ORDER IS SIGNIFICANT — first match wins, so this is a preference ranking,
# not a set. Observation dates MUST outrank clocks.
#
# `data_date` sat below `fetched_at`/`updated_at`/`saved_at` here. Nothing
# breaks today because no current file carries both, but the moment a feed
# gains a real observation date alongside an existing fetch stamp, it would be
# dated from the CLOCK — silently, and on exactly the feed someone had just
# taken the trouble to give honest provenance to.
_DATE_KEYS: tuple[str, ...] = (
    # --- real observation dates: what the data describes -------------------
    "data_date",
    "last_date", "last_updated",
    "as_of", "compiled_at",
    # --- build stamps: when this artifact was assembled ---------------------
    "generated_at", "generated_utc", "generated",
    # --- fetch clocks: LAST RESORT. These advance whether or not the data
    #     moved, so a feed dated from one can never appear stale.
    "fetched_at", "refreshed_at", "updated_at", "saved_at",
    "tstr",
)


def _norm_key(k: str) -> str:
    """Fold a key to its comparison form: lowercase, separators removed.
    `as_of`, `asOf`, `AsOf` and `as-of` all collapse to `asof`."""
    return k.lower().replace("_", "").replace("-", "").replace(" ", "")


_DATE_KEYS_NORM = {_norm_key(k) for k in _DATE_KEYS}

# A date value must BEGIN with a literal ISO-8601 date. Anything else is prose.
_ISO_PREFIX = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?")


def _parse_date_value(v: object) -> "datetime | None":
    """Parse a freshness value, or return None. Strict on purpose.

    data-aviation.json's `asOf` is the prose string
        "FAA airman data Dec 31 2025 · FAA aircraft registry late May 2026 · ..."
    A lenient parser that grabbed "Dec 31 2025" out of that would publish a
    date the file does not actually assert. A WRONG date is strictly worse than
    no date, because a wrong date gets believed and a missing one gets
    investigated — so prose resolves to None (→ UNKNOWN) rather than to a
    plausible-looking lie.
    """
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not _ISO_PREFIX.match(s):
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # Trailing junk after a valid timestamp — opensky's `tstr` is
        # "2026-08-02 21:13 UTC", which fromisoformat rejects for the word
        # "UTC". Retry against just the ISO-shaped prefix, keeping the CLOCK
        # TIME. The previous code dropped straight to the date-only fallback
        # here, which back-dated every opensky read to midnight and could add
        # up to 24 phantom hours to an HOURLY feed measured against a 24h
        # threshold — a false alarm manufactured by the parser.
        try:
            dt = datetime.fromisoformat(_ISO_PREFIX.match(s).group(0))
        except ValueError:
            try:
                dt = datetime.strptime(s[:10], "%Y-%m-%d")
            except ValueError:
                return None
    # A naive timestamp is read as UTC: every producer in this repo stamps UTC,
    # and a few hours of skew on a multi-day freshness threshold is noise.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class AgeProbe:
    """Result of reading a file's own freshness signal.

    `drift_keys` is the self-detection channel: when nothing recognised
    parsed but the file plainly does contain dates, those key paths are
    reported so the NEXT allowlist drift announces itself instead of
    returning a silent None.
    """
    age_h: float | None = None
    key: str | None = None                                  # what produced age_h
    drift_keys: list[str] = field(default_factory=list)     # unrecognised date-ish keys
    note: str = ""                                          # why age_h is None


def _scan_for_date_keys(obj: object, prefix: str = "", depth: int = 0,
                        out: "list[str] | None" = None,
                        budget: "list[int] | None" = None) -> list[str]:
    """Bounded hunt for date-looking values anywhere in a small JSON tree.

    Bounded hard (depth 3, two items per list, 2000 nodes, 6 reported paths)
    because this also runs over data-mufon.json, which holds 144k records —
    an unbounded walk would turn a cheap health check into a minute of CPU.
    """
    out = [] if out is None else out
    budget = [2000] if budget is None else budget
    if depth > 3 or len(out) >= 6 or budget[0] <= 0:
        return out
    budget[0] -= 1
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if _parse_date_value(v) is not None:
                if depth > 0 or _norm_key(str(k)) not in _DATE_KEYS_NORM:
                    out.append(p)
            else:
                _scan_for_date_keys(v, p, depth + 1, out, budget)
            if len(out) >= 6 or budget[0] <= 0:
                break
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:2]):
            _scan_for_date_keys(v, f"{prefix}[{i}]", depth + 1, out, budget)
            if len(out) >= 6 or budget[0] <= 0:
                break
    return out


# --- row-level freshness -----------------------------------------------------
# Some feeds rewrite their top-level timestamp on EVERY run even when the data
# inside was carried forward from the Actions cache. market.json is the case
# that burned us: fetch_market.py stamps the envelope unconditionally, so
# `generated_at` said "0h old" while every markets_top row was a stale-keep
# copy of a 16-day-old CoinGecko response. The envelope described the run, not
# the data, and /health/ reported green the whole time.
#
# This is a different failure from allowlist drift. Drift is "we did not know
# the key"; this is "the key is present, recent, and describing the wrong
# thing". No ordering of _DATE_KEYS can catch it, which is why row age is
# consulted BEFORE that scan rather than ranked inside it.


def _newest_row_age_h(rows: list, now_ts: float) -> "float | None":
    """Age (hours) of the most recently OBSERVED row in a list of records.

    Uses each row's frozen `as_of`. Rows without one are ignored — they carry
    no provable observation date. Returns None if no row has a usable `as_of`,
    so the caller falls back to the envelope rather than inventing a number.

    `as_of` is DAY-GRANULAR (fetch_market.py truncates CoinGecko's
    `last_updated` to 10 chars), so age is measured in whole days against
    today's UTC date: observed today = 0h, yesterday = 24h. Subtracting a
    date-only stamp from the wall clock instead would report a feed fetched
    20 minutes ago as ~20h old at 20:00 UTC and turn the health page red every
    evening — a watchdog that cries wolf daily is one everybody mutes.
    """
    today = datetime.fromtimestamp(now_ts, timezone.utc).date()
    newest_days = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        iso = r.get("as_of")
        if not isinstance(iso, str) or not iso.strip():
            continue
        try:
            d = datetime.strptime(iso.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        days = (today - d).days
        if newest_days is None or days < newest_days:
            newest_days = days
    if newest_days is None:
        return None
    return max(0.0, newest_days * 24.0)


# filename -> key holding the row list whose `as_of` defines real freshness.
ROW_FRESHNESS_SOURCES: dict[str, str] = {
    "market.json": "markets_top",
}


def _row_content_age_h(name: str, data: dict, now_ts: float) -> "float | None":
    key = ROW_FRESHNESS_SOURCES.get(name)
    if not key:
        return None
    rows = data.get(key)
    if not isinstance(rows, list) or not rows:
        return None
    return _newest_row_age_h(rows, now_ts)


def _content_age_probe(path: Path, now_ts: float) -> AgeProbe:
    """Age (hours) from a file's INTERNAL freshness signal — the last data row's
    date for CSVs, or a generated_at/compiled_at/asOf/... field for JSON.

    Catches the mtime false-green where a stateless CI run rewrites a file with
    stale CONTENT (e.g. committed btc_flows.csv) so its mtime looks fresh while
    the data inside is weeks old.
    """
    try:
        if path.suffix == ".csv":
            lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
            if len(lines) < 2:
                return AgeProbe(note="fewer than two non-blank rows")
            ds = lines[-1].split(",")[0].strip()[:10]
            try:
                dt = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                return AgeProbe(note=f"last row's first column is not a date: {ds!r}")
            return AgeProbe(age_h=max(0.0, (now_ts - dt.timestamp()) / 3600.0),
                            key="last CSV row")
        if path.suffix != ".json":
            return AgeProbe(note=f"no reader for {path.suffix or 'extensionless'} files")

        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            return AgeProbe(note="top level is not a JSON object")

        # Row-level age wins over ANY envelope key, so this runs before the
        # _DATE_KEYS scan below. For the files in ROW_FRESHNESS_SOURCES the
        # envelope describes the RUN, not the data: fetch_market.py stamps
        # market.json's `generated_at` unconditionally, so it read "0h old"
        # while every markets_top row was a stale-keep copy of a 16-day-old
        # CoinGecko response. A key-priority ranking cannot fix that — the
        # envelope key is genuinely present and genuinely recent; it is just
        # describing something other than the data.
        row_age = _row_content_age_h(path.name, data, now_ts)
        if row_age is not None:
            return AgeProbe(age_h=row_age, key="newest row `as_of`")

        # One pass to build the normalised lookup, so camelCase and snake_case
        # spellings of the same key resolve identically.
        by_norm: dict[str, str] = {}
        for k in data:
            by_norm.setdefault(_norm_key(str(k)), str(k))

        seen_but_unparseable: list[str] = []
        for cand in _DATE_KEYS:
            orig = by_norm.get(_norm_key(cand))
            if orig is None:
                continue
            dt = _parse_date_value(data[orig])
            if dt is None:
                raw = data[orig]
                if isinstance(raw, str) and raw.strip():
                    seen_but_unparseable.append(f"{orig}={raw.strip()[:60]!r}")
                continue
            return AgeProbe(age_h=max(0.0, (now_ts - dt.timestamp()) / 3600.0),
                            key=orig)

        # Nothing recognised resolved. Before giving up, say what the file DOES
        # look like it contains, so allowlist drift surfaces itself.
        drift = _scan_for_date_keys(data)
        if seen_but_unparseable:
            note = ("recognised date key(s) present but unparseable: "
                    + "; ".join(seen_but_unparseable))
        elif drift:
            note = ("no recognised date key; unrecognised date-shaped value(s) at: "
                    + ", ".join(drift)
                    + ". Add the right key to _DATE_KEYS (top-level) or to "
                      "NESTED_DATE_PATHS (nested).")
        else:
            note = "no date-shaped value anywhere in the first 3 levels"
        return AgeProbe(drift_keys=drift, note=note)
    except Exception as exc:  # unreadable / malformed file
        return AgeProbe(note=f"{type(exc).__name__}: {exc}"[:200])


# --------------------------------------------------------------------------
# Nested dates: a container's stamp says nothing about its contents
# --------------------------------------------------------------------------
#
# data-city.json reads fresh from its top-level `generated_at` while every
# cities[].data_health.last_updated inside it stopped moving months ago. Same
# shape as the crypto breadth freeze — the FILE was rewritten hourly while the
# DATA inside it stale-kept.
#
# This lives here rather than in data_health.py so /health/ and the watchdog
# resolve age through the identical code. Two monitors that can disagree about
# what "stale" means is worse than one, and the disagreement is what people
# argue about instead of fixing the feed.
#
# Path syntax:  `a.b`   exact key
#               `a[].b` every item of the list at `a`
#               `a.*.b` every value of the dict at `a`
#
# Only genuine FRESHNESS signals belong here. A date describing a real-world
# event — when an advisory was issued, when a funding round closed, when a news
# item was published — is content, not provenance, and wiring it would alarm on
# data that is perfectly correct. See
# tests/test_data_health.py::test_nested_date_sweep_is_documented for the four
# paths deliberately left out and why.
NESTED_DATE_PATHS: dict[str, tuple[str, ...]] = {
    # THE deep one. city-daily.yml refreshes the top-level stamp every night;
    # the per-city data_health block underneath it has not moved since April,
    # and Miami's since 2023-12.
    "data-city.json": ("cities[].data_health.last_updated",),
    # MUFON's top-level `generated_at` is `_now_iso()` written unconditionally
    # (fetch_mufon.py), including on a run that served all 144 months from the
    # committed cache and touched no network. Judging the feed by it means the
    # feed can never be stale — the precise defect this whole effort exists to
    # remove, on the feed that was frozen for eight weeks.
    # date_range[1] is the newest sighting actually in the payload: a real
    # observation date, and the same signal the dashboard's UAP tab stamps.
    "data-mufon.json": ("date_range[1]",),
    # Same — the whale sidecar carries as_of inside its sentiment blocks. Taking
    # the oldest mirrors PR #25's fMin rule so the monitor and the dashboard
    # gauge agree about the same payload.
    "data-whale.json": ("sentiment.as_of", "eth.sentiment.as_of"),
    # `checked_at` is when the FETCH ran; `prior_generated_at` is how old the
    # data actually being served is. Rule 1: report the age of the DATA.
    "data-travel-fetch-status.json": ("prior_generated_at",),
    # Its only stamp, one level down under a metadata block. Found by the drift
    # scan above, not by reading the directory.
    "snowflake_summit/vendors.json": ("_meta.generated",),
}


_INDEXED = re.compile(r"^(.*)\[(-?\d+)\]$")


def _select(node: object, tokens: list) -> list:
    """Every value a dotted path selects. See NESTED_DATE_PATHS for syntax."""
    if not tokens:
        return [node]
    tok, rest = tokens[0], tokens[1:]
    out: list = []
    if tok == "*":
        values = (node.values() if isinstance(node, dict)
                  else node if isinstance(node, list) else [])
        for v in values:
            out.extend(_select(v, rest))
        return out
    # `a[N]` — ONE element, not all of them. Needed for span-shaped values like
    # data-mufon.json's date_range ["1906-11-11", "2026-06-05"], where the pair
    # is [oldest_record, newest_record]. Selecting both and taking the oldest
    # (nested_age_h's rule) would date the feed to 1906 and scream forever;
    # the freshness signal is unambiguously the END of the span.
    m = _INDEXED.match(tok)
    if m:
        tok, idx = m.group(1), int(m.group(2))
        if not isinstance(node, dict) or tok not in node:
            return out
        v = node[tok]
        if isinstance(v, list) and -len(v) <= idx < len(v):
            out.extend(_select(v[idx], rest))
        return out
    iterate = tok.endswith("[]")
    if iterate:
        tok = tok[:-2]
    if not isinstance(node, dict) or tok not in node:
        return out
    v = node[tok]
    if iterate:
        if isinstance(v, list):
            for item in v:
                out.extend(_select(item, rest))
        return out
    return _select(v, rest)


def nested_age_h(path: Path, now_ts: float, specs) -> "tuple[float | None, str]":
    """OLDEST age (hours) among every date the given paths select.

    Oldest, not newest: a container of N dated parts is only as fresh as its
    stalest part. Taking the newest is how a payload whose every city stopped
    updating in April still reports 13.6h.
    """
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None, ""
    oldest: float | None = None
    where = ""
    for spec in specs:
        for value in _select(data, spec.split(".")):
            dt = _parse_date_value(value)
            if dt is None:
                continue
            age = max(0.0, (now_ts - dt.timestamp()) / 3600.0)
            if oldest is None or age > oldest:
                oldest, where = age, spec
    return oldest, where


def resolve_age(path: Path, now_ts: float, rel: "str | None" = None) -> AgeProbe:
    """A file's honest age: its own stamp, then the OLDER of that and whatever
    its contents say about themselves.

    `rel` is the repo-relative key into NESTED_DATE_PATHS; it defaults to the
    bare filename so callers that only have a leaf name still match.
    """
    probe = _content_age_probe(path, now_ts)
    specs = NESTED_DATE_PATHS.get(rel or "") or NESTED_DATE_PATHS.get(path.name)
    if not specs:
        return probe
    nested, where = nested_age_h(path, now_ts, specs)
    if nested is None:
        return probe
    if probe.age_h is None:
        probe.age_h, probe.key = nested, where
        probe.note = f"no top-level stamp; age taken from nested {where}"
    elif nested > probe.age_h:
        probe.note = (
            f"container says {humanize_age(probe.age_h)} via "
            f"{probe.key or 'top level'}, but contents at {where} are "
            f"{humanize_age(nested)} old — reporting the older of the two, "
            f"because a composite is only as fresh as its oldest input")
        probe.age_h, probe.key = nested, where
    return probe


def _content_age_h(path: Path, now_ts: float) -> "float | None":
    """Back-compatible wrapper — age in hours, or None when unresolvable."""
    return _content_age_probe(path, now_ts).age_h


def _age_and_provenance(entry: Path, mtime: float, now: float,
                        rel: "str | None" = None) -> "tuple[float, AgeProbe]":
    """mtime age, raised to the content age when the file states one.

    max(), not min(): mtime is a lower bound that a stateless CI checkout
    resets on every run, so the only direction it can be wrong is too young.
    """
    age_h = (now - mtime) / 3600.0
    probe = resolve_age(entry, now, rel)
    if probe.age_h is not None:
        age_h = max(age_h, probe.age_h)
    return age_h, probe


def _provenance_fields(probe: AgeProbe) -> dict:
    """Row fields that say WHERE the age came from — or why it is unknown.

    `date_key: null` on a row is the visible symptom of allowlist drift, and
    `date_drift` names the keys the file actually uses so the fix is obvious.
    """
    out: dict = {"date_key": probe.key}
    if probe.age_h is None:
        out["date_unresolved"] = probe.note
        if probe.drift_keys:
            out["date_drift"] = probe.drift_keys
    return out


def scan(path: Path, rel_to: Path, threshold_key_fn=None) -> list[dict]:
    """Return one entry per regular file under `path`. mtime → age_h → status."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    now = datetime.now(timezone.utc).timestamp()
    for entry in sorted(path.iterdir()):
        if entry.name.startswith("."):
            continue
        if not entry.is_file():
            continue
        if entry.suffix in (".bak", ".tmp"):
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        rel = entry.relative_to(rel_to).as_posix()
        age_h, probe = _age_and_provenance(entry, mtime, now, rel)
        key = threshold_key_fn(entry) if threshold_key_fn else entry.name
        t = THRESHOLDS.get(key, DEFAULT)
        rows.append({
            "name": entry.name,
            "path": str(entry.relative_to(rel_to)),
            "size_bytes": entry.stat().st_size,
            "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "age_h": round(age_h, 2),
            "age_human": humanize_age(age_h),
            "status": classify(age_h, t),
            "fresh_h": t.fresh_h,
            "stale_h": t.stale_h,
            **_provenance_fields(probe),
        })
    return rows


def collect_rendered() -> list[dict]:
    """Top-level data files baked into dashboard.html."""
    rows = scan(DATA_DIR, REPO_ROOT)
    # also pull root-level data-*.json
    now = datetime.now(timezone.utc).timestamp()
    for name in ("data-defi.json", "data-whale.json"):
        p = REPO_ROOT / name
        if not p.exists():
            continue
        mtime = p.stat().st_mtime
        age_h, probe = _age_and_provenance(p, mtime, now, name)
        t = THRESHOLDS.get(name, DEFAULT)
        rows.append({
            "name": name,
            "path": name,
            "size_bytes": p.stat().st_size,
            "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "age_h": round(age_h, 2),
            "age_human": humanize_age(age_h),
            "status": classify(age_h, t),
            "fresh_h": t.fresh_h,
            "stale_h": t.stale_h,
            **_provenance_fields(probe),
        })
    return rows


def collect_stale() -> list[dict]:
    """Upstream fetcher caches (data/.stale/). Mtime here = last successful fetch."""
    return scan(STALE_DIR, REPO_ROOT)


def collect_summit() -> list[dict]:
    """Snowflake Summit curated data files (committed under snowflake_summit/).
    The Summit dashboard is built offline from these — no live API — so this is
    the right place to see whether its vendor/news data is current."""
    rows: list[dict] = []
    if not SUMMIT_DIR.exists():
        return rows
    now = datetime.now(timezone.utc).timestamp()
    for name in ("vendors.json", "news.json", "floorplan.json"):
        p = SUMMIT_DIR / name
        if not p.exists():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        age_h, probe = _age_and_provenance(p, mtime, now,
                                           f"snowflake_summit/{name}")
        t = THRESHOLDS.get(name, DEFAULT)
        rows.append({
            "name": f"snowflake_summit/{name}",
            "path": f"snowflake_summit/{name}",
            "size_bytes": p.stat().st_size,
            "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "age_h": round(age_h, 2),
            "age_human": humanize_age(age_h),
            "status": classify(age_h, t),
            "fresh_h": t.fresh_h,
            "stale_h": t.stale_h,
            **_provenance_fields(probe),
        })
    return rows


def collect_lthcs() -> list[dict]:
    """data/lthcs/ has its own pipeline — snapshot top-level files first, then
    a sample of nested files. Top-level always wins so the LTHCS tab card's
    input (e.g. universe.json) is guaranteed present even with the cap."""
    p = DATA_DIR / "lthcs"
    if not p.exists():
        return []
    now = datetime.now(timezone.utc).timestamp()

    def row_for(entry: Path) -> dict | None:
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            return None
        rel = entry.relative_to(REPO_ROOT)
        age_h = (now - mtime) / 3600.0
        t = THRESHOLDS.get(entry.name, DEFAULT)
        return {
            "name": str(rel.relative_to(Path("data/lthcs"))),
            "path": str(rel),
            "size_bytes": entry.stat().st_size,
            "mtime_iso": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "age_h": round(age_h, 2),
            "age_human": humanize_age(age_h),
            "status": classify(age_h, t),
            "fresh_h": t.fresh_h,
            "stale_h": t.stale_h,
        }

    top = [row_for(e) for e in sorted(p.glob("*.json")) if e.is_file()]
    nested = [row_for(e) for e in sorted(p.rglob("*.json"))
              if e.is_file() and e.parent != p]
    rows = [r for r in top if r] + [r for r in nested if r]
    return rows[:50]  # cap to keep payload small


def build_tab_view(rendered: list[dict], lthcs: list[dict]) -> list[dict]:
    by_path = {r["path"]: r for r in rendered}
    for r in lthcs:
        by_path[r["path"]] = r
    out = []
    for tab, inputs in TAB_INPUTS.items():
        rows = []
        worst = "fresh"
        for inp in inputs:
            full = f"data/{inp}" if not inp.startswith(("data/", "data-")) else inp
            row = by_path.get(full) or by_path.get(inp)
            if row:
                rows.append(row)
                if row["status"] == "critical":
                    worst = "critical"
                elif row["status"] == "stale" and worst != "critical":
                    worst = "stale"
            else:
                rows.append({"name": inp, "path": inp, "status": "missing",
                             "age_human": "—", "mtime_iso": None})
                worst = "critical"
        out.append({"tab": tab, "status": worst, "inputs": rows})
    return out


def main() -> int:
    rendered = collect_rendered() + collect_summit()
    stale = collect_stale()
    lthcs = collect_lthcs()
    tabs = build_tab_view(rendered, lthcs)

    # Allowlist drift, surfaced instead of swallowed. A row whose file plainly
    # contains dates but under a key _DATE_KEYS does not know about is the
    # exact shape of the compiled_at / asOf misses; naming it here means the
    # next one announces itself on the build log the day it appears.
    drifted = [r for r in rendered + stale if r.get("date_drift")]
    for r in drifted:
        print(f"  [date-key drift] {r['path']}: {r.get('date_unresolved', '')}")

    summary = {
        "fresh": sum(1 for r in rendered + stale + lthcs if r.get("status") == "fresh"),
        "stale": sum(1 for r in rendered + stale + lthcs if r.get("status") == "stale"),
        "critical": sum(1 for r in rendered + stale + lthcs if r.get("status") == "critical"),
        "total": len(rendered) + len(stale) + len(lthcs),
        "date_key_drift": len(drifted),
    }

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "tabs": tabs,
        "rendered": rendered,
        "upstream": stale,
        "lthcs": lthcs,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"wrote {OUT_PATH.relative_to(REPO_ROOT)} "
          f"({summary['total']} files: {summary['fresh']} fresh, "
          f"{summary['stale']} stale, {summary['critical']} critical"
          + (f", {len(drifted)} with unrecognised date keys" if drifted else "")
          + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
