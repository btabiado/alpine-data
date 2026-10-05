#!/usr/bin/env python3
"""History continuity: is every feed's accumulated history actually accumulating?

data_health.py answers "is the NEWEST point fresh". That question cannot see a
hole in the middle. The composite archive lost 2026-08-19..21 to a wedged
pages.yml queue, the LTHCS crypto snapshots lost four September days to a cron
that started after midnight and wrote the next day's file, and every archived
composite carried `whale_sentiment_btc: null` for two months, all while the
freshness check was green, because the newest file was always fresh.

This module checks the other axis. Each feed in data_health.MANIFEST may carry
`history=(History(...), ...)` specs that say where its history lives and how
often it must grow. For the last N days it reports:

  * GAP        an expected day / trading day / month with no entry
  * DUPLICATE  the same period (and key, e.g. ticker) recorded twice
  * FIELD GAP  a dated entry exists but a required field inside it is null

A missing period that cannot be backfilled from any real source is recorded in
health/known_gaps.json (published at /health/known_gaps.json, so the dashboard
can disclose it). Known gaps are reported, never failed; anything else fails.

A field whose source is unavailable BY DESIGN (an API that needs a key this
deployment does not have) is disclosed one of two ways, and both stop
disclosing the moment the key exists, so "key set but still null" fails:

  * the payload says so: the member carries
    ``"unavailable": {"<dotted.field>": {"reason": "...", "requires_env": "X"}}``
    (snapshot_composites.py writes this only while X is unset at write time);
  * known_gaps.json holds an OPEN-ENDED entry: ``"end": null`` plus
    ``"until_env": "X"``. It covers every period from `start` on while X is
    unset in the monitor's environment, and nothing once X is set.
    data-health.yml and daily-audit.yml map X for exactly this check.

Only INTERNAL continuity is judged: between the first entry inside the window
and the last entry. The trailing edge ("the history stopped growing") is the
freshness check's job, and judging it twice would report one outage twice.

Sources a spec can read (all offline except `r2`, which reads the published
coverage file the pages build writes):

  files  a directory of YYYY-MM-DD.json members (data/composites/, LTHCS)
  csv    a CSV with a date column (ETF flows, equity ETF flows, history CSVs)
  json   dated rows nested in a JSON document (city / real-estate / TSA series)
  git    commit dates of a file that is overwritten in place (cfpb, opensky):
         for those, git history IS the archive, so a day with no commit is a
         day with no stored snapshot
  r2     per-file daily presence in health/r2-coverage.json (the R2 archive of
         every deploy-time data-*.json)

Pure stdlib. Nothing here touches the network; the r2 payload is handed in.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

DAILY = "daily"
WEEKDAY = "weekday"
TRADING = "trading"     # NYSE trading days: weekdays minus exchange holidays
MONTHLY = "monthly"
CADENCES = (DAILY, WEEKDAY, TRADING, MONTHLY)

SOURCES = ("files", "csv", "json", "git", "r2")

DEFAULT_WINDOW_DAYS = 35
DEFAULT_MONTHLY_WINDOW_DAYS = 400

# NYSE full-day closures. Kept explicit rather than computed: the exchange
# adds one-off closures (2025-01-09, national day of mourning) that no rule
# predicts, and a wrong holiday list is a permanent false alarm. Extend when
# the NYSE publishes the next year's calendar.
NYSE_HOLIDAYS = frozenset(date.fromisoformat(d) for d in (
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
))

KNOWN_GAPS_REL = "health/known_gaps.json"


@dataclass(frozen=True)
class History:
    """Where one feed's accumulated history lives and how often it must grow.

    `path` is repo-relative: a directory ending in '/' for `files`, a file
    otherwise. For `json`, `rows` is a build_health_status._select path to the
    containers ("cities[].extended[]", "" for the document root), `series_key`
    the list inside each container, and `date_field` the date inside each row
    ("" when the rows ARE date strings). `group` names a container field that
    labels its series in findings. `key_fields` extend the duplicate key (a
    CSV with one row per date AND ticker). `required_fields` are dotted paths
    that must be non-null in every `files` member inside the window; suffix
    one with "@YYYY-MM-DD" when the writer only started emitting it that day.
    `since` is the first date the cadence applies (a series that went from
    weekdays to every day, say).
    """
    path: str
    cadence: str
    source: str = "files"
    label: str = ""
    date_field: str = "date"
    rows: str = ""
    series_key: str = ""
    group: str = ""
    key_fields: tuple[str, ...] = ()
    required_fields: tuple[str, ...] = ()
    window_days: int | None = None
    since: str | None = None

    @property
    def name(self) -> str:
        return self.label or self.path

    def window(self) -> int:
        if self.window_days:
            return self.window_days
        return DEFAULT_MONTHLY_WINDOW_DAYS if self.cadence == MONTHLY else DEFAULT_WINDOW_DAYS


@dataclass
class Finding:
    """Verdict for one History spec. `missing`/`duplicates`/`field_gaps` hold
    only UNDISCLOSED problems; `disclosed` holds the ones known_gaps.json
    already explains."""
    spec: History
    periods_checked: int = 0
    first: str | None = None
    last: str | None = None
    missing: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    field_gaps: list[tuple[str, str]] = field(default_factory=list)  # (field, "group: period")
    disclosed: list[str] = field(default_factory=list)
    error: str | None = None     # the history could not be read at all
    note: str | None = None      # nothing to judge, and why that is not a failure
    skipped: str | None = None   # not checkable in THIS checkout (see _from_git)

    @property
    def ok(self) -> bool:
        return not (self.error or self.missing or self.duplicates or self.field_gaps)


# --------------------------------------------------------------------------
# Dates and periods
# --------------------------------------------------------------------------

_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_US_DAY = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def parse_period(value, cadence: str) -> str | None:
    """Normalise one recorded date to its period key, or None if undatable.

    Daily-type cadences key on 'YYYY-MM-DD', monthly on 'YYYY-MM'. Accepts
    ISO dates and datetimes, 'YYYY-MM', and TSA's 'M/D/YYYY'. Anything else is
    None: a value the parser cannot date is reported, never guessed at.
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    day: date | None = None
    m = _ISO_DAY.match(s)
    if m:
        try:
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    else:
        m = _US_DAY.match(s)
        if m:
            try:
                day = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                return None
        else:
            m = _ISO_MONTH.match(s)
            if m and cadence == MONTHLY and 1 <= int(m.group(2)) <= 12:
                return f"{m.group(1)}-{m.group(2)}"
            return None
    if cadence == MONTHLY:
        return f"{day.year:04d}-{day.month:02d}"
    return day.isoformat()


def _month_add(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def is_expected(day: date, cadence: str) -> bool:
    if cadence == DAILY:
        return True
    if cadence == WEEKDAY:
        return day.weekday() < 5
    if cadence == TRADING:
        return day.weekday() < 5 and day not in NYSE_HOLIDAYS
    raise ValueError(f"not a day cadence: {cadence}")


def expected_periods(start: str, end: str, cadence: str) -> list[str]:
    """Every period the cadence promises in [start, end], both inclusive."""
    if start > end:
        return []
    if cadence == MONTHLY:
        out, cur = [], start[:7]
        while cur <= end[:7]:
            out.append(cur)
            cur = _month_add(cur, 1)
        return out
    out = []
    d, stop = date.fromisoformat(start), date.fromisoformat(end)
    while d <= stop:
        if is_expected(d, cadence):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def compress(periods: list[str]) -> list[str]:
    """['2026-08-19','2026-08-20','2026-08-21','2026-09-02'] ->
    ['2026-08-19..2026-08-21', '2026-09-02']. Consecutive = next calendar
    day (or month), so a weekend between two missing trading days does not
    merge them: the reader should see exactly which days are absent."""
    out: list[list[str]] = []
    for p in sorted(set(periods)):
        if out:
            prev = out[-1][1]
            if len(p) == 7:
                adjacent = _month_add(prev, 1) == p
            else:
                adjacent = (date.fromisoformat(p) - date.fromisoformat(prev)).days == 1
            if adjacent:
                out[-1][1] = p
                continue
        out.append([p, p])
    return [a if a == b else f"{a}..{b}" for a, b in out]


# --------------------------------------------------------------------------
# Collectors: each returns (rows, error). A row is (period, key, member)
# where `key` extends the period for duplicate detection and `member` is the
# parsed JSON for `files` sources (else None).
# --------------------------------------------------------------------------

def _from_files(spec: History, root: Path):
    d = root / spec.path
    if not d.is_dir():
        return [], f"history directory {spec.path} does not exist"
    rows = []
    for p in sorted(d.glob("*.json")):
        period = parse_period(p.stem, spec.cadence)
        if not period or len(p.stem) != 10:
            continue   # index.json, latest.json and friends are not members
        member = None
        if spec.required_fields:
            try:
                member = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                member = {}
        rows.append((period, "", member))
    return rows, None


def _from_csv(spec: History, root: Path):
    p = root / spec.path
    if not p.is_file():
        return [], f"history file {spec.path} does not exist"
    rows, undated = [], 0
    try:
        with p.open(encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            if spec.date_field not in (reader.fieldnames or []):
                return [], (f"{spec.path} has no {spec.date_field!r} column "
                            f"(columns: {reader.fieldnames})")
            for r in reader:
                period = parse_period(r.get(spec.date_field), spec.cadence)
                if not period:
                    undated += 1
                    continue
                key = "|".join(str(r.get(k, "")) for k in spec.key_fields)
                rows.append((period, key, None))
    except (OSError, csv.Error) as exc:
        return [], f"{spec.path} unreadable: {exc}"
    if undated and not rows:
        return [], f"{spec.path}: none of {undated} rows carries a parseable date"
    return rows, None


def _select(node, path: str) -> list:
    # Shared with the freshness monitor so the two read nested JSON through
    # one selector grammar. Imported lazily: build_health_status lives next
    # to this file and is only on sys.path once data_health set it up.
    from build_health_status import _select as bhs_select
    return bhs_select(node, [t for t in path.split(".") if t]) if path else [node]


def _from_json(spec: History, root: Path):
    p = root / spec.path
    if not p.is_file():
        return [], f"history file {spec.path} does not exist"
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [], f"{spec.path} unreadable: {type(exc).__name__}: {exc}"
    containers = _select(doc, spec.rows)
    if not containers:
        return [], f"{spec.path}: selector {spec.rows!r} matched nothing"
    groups: dict[str, list] = {}
    for i, c in enumerate(containers):
        if not isinstance(c, dict):
            continue
        series = c.get(spec.series_key) if spec.series_key else c
        if not isinstance(series, list):
            continue
        gname = str(c.get(spec.group)) if spec.group and c.get(spec.group) else f"#{i}"
        out = groups.setdefault(gname, [])
        for r in series:
            raw = r if not spec.date_field else (r.get(spec.date_field) if isinstance(r, dict) else None)
            period = parse_period(raw, spec.cadence)
            if period:
                out.append((period, "", None))
    if not groups:
        return [], f"{spec.path}: no {spec.series_key!r} series under {spec.rows!r}"
    return groups, None


def _git_is_shallow(root: Path) -> bool:
    try:
        out = subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                             cwd=root, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return True
    return out.returncode != 0 or out.stdout.strip() == "true"


def _from_git(spec: History, root: Path, since: date):
    # check() has already skipped shallow / non-repo checkouts.
    try:
        out = subprocess.run(
            ["git", "log", f"--since={since.isoformat()}T00:00:00Z",
             "--format=%cd", "--date=format-local:%Y-%m-%d", "--", spec.path],
            cwd=root, capture_output=True, text=True, timeout=60,
            env={**os.environ, "TZ": "UTC"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [], f"git log failed: {exc}"
    if out.returncode != 0:
        return [], f"git log failed: {out.stderr.strip()[:200]}"
    # One commit per day is the promise; several commits on one day are the
    # same file overwritten, not a duplicate record, so they collapse.
    days = sorted({d for d in out.stdout.split() if parse_period(d, DAILY)})
    return [(d, "", None) for d in days], None


def _from_r2(spec: History, coverage: dict | None):
    if not isinstance(coverage, dict):
        return [], "R2 coverage report unavailable"
    if coverage.get("empty_reason"):
        return [], f"R2 coverage report is empty: {coverage['empty_reason']}"
    groups: dict[str, list] = {}
    for rows in (coverage.get("categories") or {}).values():
        for r in rows or []:
            if not isinstance(r, dict) or not r.get("file"):
                continue
            present = r.get("present") or {}
            groups[r["file"]] = [(d, "", None) for d, ok in present.items()
                                 if ok and parse_period(d, DAILY)]
    if not groups:
        return [], "R2 coverage report lists no files"
    return groups, None


# --------------------------------------------------------------------------
# Known gaps (health/known_gaps.json)
# --------------------------------------------------------------------------

def load_known_gaps(root: Path) -> list[dict]:
    """Every disclosed, unfillable gap. A missing or broken file yields []
    and the gaps it would have explained fail loudly, which is the safe
    direction: a lost disclosure must not silently mute anything."""
    p = root / KNOWN_GAPS_REL
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    gaps = doc.get("gaps") if isinstance(doc, dict) else None
    return [g for g in (gaps or []) if isinstance(g, dict)]


def verify_known_gaps(gaps: list[dict], specs: dict[str, tuple]) -> list[str]:
    """Structural problems in known_gaps.json. `specs` maps a MANIFEST rel to
    its History specs, so a disclosure cannot point at a history nobody checks."""
    problems = []
    for i, g in enumerate(gaps):
        where = f"known_gaps[{i}]"
        feed, hist = g.get("feed"), g.get("history")
        if feed not in specs:
            problems.append(f"{where}: feed {feed!r} has no history spec in MANIFEST")
            continue
        if hist not in {s.name for s in specs[feed]}:
            problems.append(f"{where}: {feed} has no history named {hist!r}")
        open_ended = g.get("end") is None
        if open_ended and not (isinstance(g.get("until_env"), str) and g["until_env"].strip()):
            problems.append(f"{where}: an open-ended gap (end: null) needs until_env, "
                            f"the env var whose presence ends it")
        for k in ("start",) if open_ended else ("start", "end"):
            if not isinstance(g.get(k), str) or not (_ISO_DAY.match(g[k]) or _ISO_MONTH.match(g[k])):
                problems.append(f"{where}: {k} must be YYYY-MM-DD or YYYY-MM")
        if isinstance(g.get("start"), str) and isinstance(g.get("end"), str) and g["start"] > g["end"]:
            problems.append(f"{where}: start is after end")
        if len(str(g.get("reason", "")).strip()) < 30:
            problems.append(f"{where}: a disclosure needs a real reason")
        if not g.get("backfill_attempted"):
            problems.append(f"{where}: say which real sources were tried "
                            f"(backfill_attempted) before calling a gap unfillable")
    return problems


def _disclosed(gaps: list[dict], feed: str, spec: History, period: str,
               group: str | None = None, fld: str | None = None) -> bool:
    for g in gaps:
        if g.get("feed") != feed or g.get("history") != spec.name:
            continue
        if fld is not None and g.get("field") not in (fld, "*"):
            continue
        if fld is None and g.get("field"):
            continue
        if g.get("group") and g.get("group") != group:
            continue
        until_env = g.get("until_env")
        if until_env and _env_set(until_env):
            # The missing key is configured now; the reason no longer holds.
            continue
        start = str(g.get("start", ""))
        end = g.get("end")
        # Compare on the period's own resolution, so a monthly gap can be
        # written as 2020-11 and a daily one as a day range. A null end is
        # open-ended (only valid with until_env; see verify_known_gaps).
        if start[:len(period)] <= period and (
                end is None or period <= str(end)[:len(period)]):
            return True
    return False


def _env_set(name: str) -> bool:
    return bool(os.environ.get(str(name), "").strip())


def unavailable_reason(member, fld: str) -> str | None:
    """The payload's own statement that `fld` is unavailable by design, or
    None. Shape: member["unavailable"][fld] = {"reason": str, "requires_env":
    optional env var}. A marker whose requires_env IS set in this
    environment is ignored: the key exists, so a null is a real gap."""
    if not isinstance(member, dict):
        return None
    marks = member.get("unavailable")
    mark = marks.get(fld) if isinstance(marks, dict) else None
    if isinstance(mark, str):
        mark = {"reason": mark}
    if not isinstance(mark, dict):
        return None
    reason = str(mark.get("reason") or "").strip()
    if len(reason) < 10:     # a bare flag explains nothing; do not mute on it
        return None
    req = mark.get("requires_env")
    if req and _env_set(req):
        return None
    return reason


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------

def _field_value(member, dotted: str):
    node = member
    for tok in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(tok)
    return node


def _judge(rows, spec: History, feed: str, today: date, gaps: list[dict],
           group: str | None, f: Finding, end: str | None = None) -> None:
    window_start = today - timedelta(days=spec.window())
    lo = window_start.isoformat()
    if spec.since and spec.since > lo:
        lo = spec.since
    if spec.cadence == MONTHLY:
        lo = lo[:7]
    inside = [r for r in rows if r[0] >= lo and r[0] <= today.isoformat()[:len(r[0])]]
    if not inside:
        return
    periods = sorted({r[0] for r in inside})
    first, last = periods[0], periods[-1]
    if end and end > last:
        last = end   # judged up to a shared end date (R2: the bucket's newest day)
    f.first = min(f.first or first, first)
    f.last = max(f.last or last, last)
    expected = expected_periods(first, last, spec.cadence)
    f.periods_checked += len(expected)
    have = set(periods)
    tag = f"{group}: " if group else ""
    for p in expected:
        if p in have:
            continue
        if _disclosed(gaps, feed, spec, p, group):
            f.disclosed.append(tag + p)
        else:
            f.missing.append(tag + p)
    counts = Counter((r[0], r[1]) for r in inside)
    for (p, k), n in sorted(counts.items()):
        if n > 1:
            f.duplicates.append(f"{tag}{p}{' ' + k if k else ''} x{n}")
    for req in spec.required_fields:
        # "indexes.foo@2026-10-04": required from that day on. A key the
        # writer only started emitting that day is absent before it, which is
        # not a gap in the record, just a record that did not exist yet.
        fld, _, req_since = req.partition("@")
        for p, _, member in inside:
            if req_since and p < req_since:
                continue
            if member is None or _field_value(member, fld) is not None:
                continue
            if _disclosed(gaps, feed, spec, p, group, fld):
                f.disclosed.append(f"{tag}{p} {fld}=null")
            elif unavailable_reason(member, fld):
                f.disclosed.append(f"{tag}{p} {fld}=null (unavailable by design)")
            else:
                f.field_gaps.append((fld, tag + p))


def check(feed: str, spec: History, root: Path, today: date,
          gaps: list[dict] | None = None, r2_coverage: dict | None = None) -> Finding:
    """Judge one History spec over its window ending `today`."""
    gaps = gaps or []
    f = Finding(spec)
    if spec.source == "files":
        rows, err = _from_files(spec, root)
    elif spec.source == "csv":
        rows, err = _from_csv(spec, root)
    elif spec.source == "json":
        rows, err = _from_json(spec, root)
    elif spec.source == "git":
        if _git_is_shallow(root):
            # A shallow clone (tests.yml, daily-audit.yml, a quick local
            # checkout) holds one commit, which reads as "every day missing".
            # That is not a finding about the feed, so it is a skip, not a
            # failure. The run that owns this check, data-health.yml, checks
            # out with fetch-depth: 0, and tests/test_history_continuity.py
            # fails if that line ever disappears.
            f.skipped = ("shallow clone: daily commits are judged by "
                         "data-health.yml, which checks out full history")
            return f
        rows, err = _from_git(spec, root, today - timedelta(days=spec.window()))
    elif spec.source == "r2":
        rows, err = _from_r2(spec, r2_coverage)
    else:
        rows, err = [], f"unknown history source {spec.source!r}"
    if err:
        f.error = err
        return f
    if isinstance(rows, dict):
        # Grouped sources (one series per city feed / per R2 file). For R2, a
        # file that silently stopped being uploaded must show up too, so every
        # file is judged up to the newest date anywhere in the bucket.
        end_all = None
        if spec.source == "r2":
            end_all = max((r[0] for rs in rows.values() for r in rs), default=None)
        for gname, rs in sorted(rows.items()):
            _judge(rs, spec, feed, today, gaps, gname, f, end=end_all)
    else:
        _judge(rows, spec, feed, today, gaps, None, f)
    if f.periods_checked == 0 and not f.error:
        newest = _newest(rows)
        if newest is None:
            f.error = "the history holds no dated entries at all"
        else:
            # Entries exist but all predate the window: the history stopped
            # growing. That is staleness, which the freshness check owns;
            # reporting it here too would page twice for one outage.
            f.note = (f"newest entry {newest} predates the {spec.window()}-day "
                      f"window; staleness is the freshness check's verdict")
    return f


def _newest(rows) -> str | None:
    flat = rows if isinstance(rows, list) else [r for rs in rows.values() for r in rs]
    return max((r[0] for r in flat), default=None)


def summarize(f: Finding, limit: int = 12) -> str:
    """One human line for a finding: what is missing, doubled or null."""
    if f.skipped:
        return f.skipped
    parts = []
    if f.error:
        parts.append(f.error)
    if f.note:
        parts.append(f.note)
    if f.missing:
        grouped = any(": " in m for m in f.missing)
        c = f.missing if grouped else compress(f.missing)
        parts.append(f"missing {len(f.missing)}: {', '.join(c[:limit])}"
                     + (" ..." if len(c) > limit else ""))
    if f.duplicates:
        parts.append(f"duplicates: {', '.join(f.duplicates[:limit])}")
    if f.field_gaps:
        by_field: dict[str, list[str]] = {}
        for fld, where in f.field_gaps:
            by_field.setdefault(fld, []).append(where)
        for fld, wheres in by_field.items():
            grouped = any(": " in w for w in wheres)
            c = wheres if grouped else compress(wheres)
            parts.append(f"{fld} null on {len(wheres)}: {', '.join(c[:limit])}"
                         + (" ..." if len(c) > limit else ""))
    if f.disclosed:
        by_payload = sum(1 for d in f.disclosed if d.endswith("(unavailable by design)"))
        if by_payload < len(f.disclosed):
            parts.append(f"{len(f.disclosed) - by_payload} disclosed in {KNOWN_GAPS_REL}")
        if by_payload:
            parts.append(f"{by_payload} unavailable by design (payload says why)")
    if not parts or (f.note and len(parts) == 1):
        if f.note:
            return f.note
        parts.append(f"{f.periods_checked} {f.spec.cadence} periods, none missing "
                     f"({f.first}..{f.last})")
    return "; ".join(parts)
