#!/usr/bin/env python3
"""Refresh data/btc_flows.csv and data/eth_flows.csv from Farside Investors.

WHY THIS EXISTS
---------------
These two CSVs are the ETF Flows tab's entire data source, and until now
NOTHING refreshed them. `money-flow-daily.yml` only handles *equity* ETF
flows (data/equity_etf_flows.csv); the crypto ones were updated by hand by
pasting a Farside table into `parse_farside.py`. They had been frozen since
2026-05-11/12 — roughly three months — while the tab presented them as
current.

The keyless fallback in `fetch_live.py` is NOT a solution: it pulls the
`canadiancode/btc-etf-flows` mirror, which is itself abandoned (its last row
is 2025-05-02, a year *older* than the committed data) and it only carries a
`Total` column, so wiring it up would both regress the dates and destroy the
per-fund breakdown. Verified 2026-08-02.

So we go to the canonical free source, Farside Investors, directly.

WHY FROM GITHUB ACTIONS
-----------------------
Same reason `scripts/fetch_cfpb.py` and `scripts/fetch_usaspending.py` live
here: Actions runners have clean egress, while Cloudflare/datacenter IPs get
blocked by many upstreams. This is the established house pattern for
"fetch it in CI, commit it as a keyless feed".

SAFETY CONTRACT (important — this scraper is HTML-shape dependent)
------------------------------------------------------------------
Farside can change its markup at any time, and a half-parsed table would be
worse than stale data: it would silently truncate real history. So this
script REFUSES to write unless the parse clearly succeeded:

  * the parsed table must contain the fund columns we expect,
  * it must yield at least MIN_ROWS rows,
  * its newest date must be >= the newest date already on disk,
  * and no more than MAX_HISTORICAL_WITHHELD settled rows may have their
    Total withheld for a missing fund (see ABSENCE IS NOT ZERO).

If any check fails we leave the existing CSV untouched and exit non-zero, so
the workflow goes red and the failure is visible — the same
preserve-and-shout behaviour `fetch_tsa.py` uses. Existing history is never
truncated: we merge by date, with freshly-fetched rows winning.

WHEN FARSIDE RETURNS HTTP 403
-----------------------------
GitHub Actions has been answered HTTP 403 by Farside since 2026-10-06. The
2026-10-15 daily audit still had both CSVs ending 2026-10-05 and
`etf-flows-daily` red on this step (PR #97 fixed zeros-versus-blanks, not the
block). A 403 is a refusal, not a bad parse, and this script does not try to
get around it: the User-Agent stays the one above, and there is no proxy and
no browser. It logs the status and the URL, then tries, in order:

  1. For BTC only, TFTC's public series
     (https://www.tftc.io/bitcoin-etf-flows/data.json). The file says
     CC BY 4.0 and names no key. Dollars are stored as US$ millions, one
     decimal, in the columns this repo already has. TFTC fills dates that
     have no real row yet; it does not rewrite a row Farside already settled.
     ETH has no such series. A license other than CC BY 4.0 is refused.
  2. `data/etf_flows_manual.json`, and the `ETF_FLOWS_MANUAL` text (the
     workflow_dispatch `manual_entry` input, JSON or CSV). An empty cell is
     not a zero. While Farside published a date, that row wins. While Farside
     is blocked, a date in the override replaces the stored row.

If every source that exists is blocked and nothing new was merged, the process
exits 0 and emits a warning that names the 403 and how far behind the CSV is.
The feed stays stale in data health, which is the alarm that keeps the audit
red. A non-403 failure, a refused parse, or a malformed override still exits
non-zero and does not write.

ABSENCE IS NOT ZERO (the reason for the None-vs-0.0 split below)
----------------------------------------------------------------
Farside posts a trading day's flows OVERNIGHT, so the row for the current
day exists on the page with every cell still showing "-". Read naively that
becomes a row of literal zeros:

    2026-08-03,0,0,0,0,0,0,0,0,0,0,0,0,0

which is a lie in three places at once. It draws a cliff to zero on every
fund's chart, it feeds the ETF composite as a real -100% swing, and it makes
the CSV claim a freshness it does not have. A day with no settled data is
not a day of no flows.

So `_parse_value` returns None for a cell that carries NO reading and a
number for one that does, and the two are never conflated:

  * a "-" / blank / "n/a" cell is written as an EMPTY CSV cell, never "0".
    Farside prints a literal "0.0" for a fund that reported no creations or
    redemptions; that parses to 0.0 and stays "0". A dash means the fund has
    not reported (or did not exist yet), which is not the same statement.
    (If Farside ever starts using "-" for a settled zero, the guard below
    refuses the write rather than blanking the history.)
  * Farside's Total column is a running SUM of whatever cells are filled in,
    so it prints a number even when the day is unreported or half reported.
    2026-10-06 was committed at 15:59Z, before the US close, as a row of
    zeros: no fund had reported, yet the row still carried a numeric Total,
    so the old "skip only all-dash rows" check let it through. 2026-10-02 was
    first published as Total 31.7 (FBTC + MSBT only) before IBIT's 158.2
    arrived and it became 189.9. So the Total is kept only when the row is
    COMPLETE: no fund that was reporting in the previous ACTIVE_LOOKBACK rows
    is missing. A fund that has not launched yet (no reading in that window)
    does not hold the Total back. See `withhold_incomplete_totals`.
  * a row where EVERY cell is absent after that (today before Farside
    publishes, a market holiday) is not a reading of anything, so it is not
    written at all, and is named on stderr rather than dropped silently.
    A row on a weekend or NYSE holiday is never written either unless it
    carries a non-zero number (which is shouted about): no session, no flow.
  * a row with at least one real number IS a reading, so it is written —
    INCLUDING an all-zero one. A real zero must stay representable.

The same split protects the freshness guard. A placeholder row of zeros
sitting on disk for a date the source has not reached would otherwise make
`new_newest < old_newest` true forever and wedge the scraper into permanent
refuse-to-regress. `refresh()` therefore drops information-free rows (every
cell zero or empty) that the source does not back: dated AFTER the newest row
the source reports, dated on a day the source lists with no reading, or dated
on a weekend / NYSE holiday. That happens before comparing, and real history,
including a genuine all-zero day the source still reports, is never touched.

Pure stdlib. Run from the repo root:
    python scripts/fetch_etf_flows.py            # both assets
    python scripts/fetch_etf_flows.py --asset btc
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

try:  # run as `python scripts/fetch_etf_flows.py`: scripts/ is sys.path[0]
    from history_continuity import NYSE_HOLIDAYS
    from build_health_status import threshold_for, trading_day_age_h
except ImportError:  # imported from the repo root (tests, parse_farside)
    from scripts.history_continuity import NYSE_HOLIDAYS
    from scripts.build_health_status import threshold_for, trading_day_age_h

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

UA = "AlpineDataWorks-feed/1.0 (+https://alpinedataworks.com)"

# Operator override, merged on every run. Empty arrays are the steady state.
# The workflow_dispatch input arrives in this env var as JSON or CSV text.
MANUAL_PATH = DATA_DIR / "etf_flows_manual.json"
MANUAL_ENV = "ETF_FLOWS_MANUAL"

# Substring the TFTC file must carry. Anything else is not the terms we accepted.
_CC_BY_40 = "creativecommons.org/licenses/by/4.0"

SOURCES: dict[str, dict] = {
    "btc": {
        "url": "https://farside.co.uk/bitcoin-etf-flow-all-data/",
        "csv": DATA_DIR / "btc_flows.csv",
        # Funds we must see to believe the parse. Farside occasionally adds a
        # column (a new ETF launches); we only require a core subset so a new
        # fund does not fail the run, and unknown columns are carried through.
        "require": ("IBIT", "FBTC", "GBTC"),
        # US spot-bitcoin ETFs began trading 2024-01-11. A row dated earlier
        # cannot be a reading: a hand-pasted 2024-01-01 row duplicating
        # 2024-01-11 inflated the all-time cumulative by $655.3M for months.
        "first_trading_day": "2024-01-11",
        # Public backup used only when Farside does not return a table. The
        # payload's own license field is checked on every fetch; CC BY 4.0
        # requires the attribution printed when rows from it are written.
        "backup": {
            "name": "TFTC",
            "url": "https://www.tftc.io/bitcoin-etf-flows/data.json",
            "license": "https://creativecommons.org/licenses/by/4.0/",
            "attribution": "TFTC — tftc.io/bitcoin-etf-flows",
        },
    },
    "eth": {
        "url": "https://farside.co.uk/ethereum-etf-flow-all-data/",
        "csv": DATA_DIR / "eth_flows.csv",
        "require": ("ETHA", "FETH", "ETHE"),
        "first_trading_day": "2024-07-23",  # US spot-ether ETFs' first session
    },
}

MIN_ROWS = 100  # both tables have 400+ rows of history; anything less is a bad parse

# A fund counts as "reporting" on a row when it carried a number on any of the
# previous ACTIVE_LOOKBACK rows: two trading weeks, enough to see through
# several consecutive half-published days.
ACTIVE_LOOKBACK = 10

# The newest SETTLING_ROWS rows are where Farside is still filling days in.
# Withheld Totals are expected there and nowhere else. Older rows are settled:
# a fund that never reports again has closed, and does not hold a Total back.
# More than MAX_HISTORICAL_WITHHELD settled rows with a reporting fund on "-"
# means the "'-' = not reported" assumption above has stopped holding (Farside
# started using "-" for a settled zero, say). Then most historical Totals
# would be withheld, so refuse the write: a red run beats quietly blanking
# the history.
SETTLING_ROWS = 5
MAX_HISTORICAL_WITHHELD = 5

# Farside prints "Total" and occasionally an average row; those are not dates.
_DATE_CELL = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})\s*$")


class _TableParser(HTMLParser):
    """Collect every <table> on the page as a list-of-rows-of-cell-text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._tbl: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._tbl = []
        elif tag == "tr" and self._tbl is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self._tbl is not None:
            self.tables.append(self._tbl)
            self._tbl = None
        elif tag == "tr" and self._tbl is not None and self._row is not None:
            self._tbl.append(self._row)
            self._row = None
        elif tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def _parse_date_cell(text: str) -> str | None:
    m = _DATE_CELL.match(text)
    if not m:
        return None
    try:
        dt = datetime.strptime(
            f"{int(m.group(1))} {m.group(2).title()} {int(m.group(3))}", "%d %b %Y"
        )
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d")


_NO_READING = ("", "-", "–", "—", "N/A", "n/a")


def _parse_value(tok: str) -> str | None:
    """Farside numbers: '1,234.5', '(123.4)' for negative, '-' for NO READING.

    Returns None when the cell carries no reading at all, and a numeric
    string otherwise — including "0" for a literal zero. Callers must keep
    the two apart; collapsing None to "0" is the exact bug documented under
    ABSENCE IS NOT ZERO above.
    """
    t = tok.strip().replace(",", "").replace("$", "")
    if t in _NO_READING:
        return None
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        v = float(t)
    except ValueError:
        return None
    if neg:
        v = -v
    return f"{v:g}"


def _num(cell) -> float | None:
    """A parsed or stored cell as a float, or None when it carries no reading."""
    if cell is None:
        return None
    s = str(cell).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def carries_no_information(row: list[str]) -> bool:
    """True when every value cell of a stored CSV row is empty or exactly 0.

    That is the shape the pre-fix scraper wrote for a day Farside had not
    published, since every "-" became "0", and it says nothing a missing row
    would not. Callers apply it only to rows the source does not back with a
    reading: dated past the source's newest day, listed by the source with no
    reading, or dated on a day the market was closed. A genuine all-zero
    trading day that the source still reports is never touched.
    """
    for cell in row[1:]:
        s = (cell or "").strip()
        if not s:
            continue
        try:
            if float(s) != 0.0:
                return False
        except ValueError:
            return False
    return True


def market_closed(iso: str) -> bool:
    """True on a weekend or an NYSE full-day closure: no session, so no flow."""
    try:
        d = date.fromisoformat(iso)
    except (TypeError, ValueError):
        return False
    return d.weekday() >= 5 or d in NYSE_HOLIDAYS


def withhold_incomplete_totals(rows: list[list], total_idx: int,
                               lookback: int = ACTIVE_LOOKBACK,
                               settling: int = SETTLING_ROWS) -> list[str]:
    """Blank (in place) the Total of every row where a reporting fund is missing.

    ``rows`` are chronological ``[date, v1, ..., vn]`` lists with None for a
    cell that carried no reading, and ``total_idx`` is the Total's index in
    them. A missing fund holds the Total back when it carried a number on any
    of the previous ``lookback`` rows. Two kinds of "-" do not:
      * a fund that has not launched yet (no reading in that window; Farside
        prints "-" before a fund's first session);
      * on rows older than the newest ``settling`` rows, a fund that never
        reports again (it closed). Near the newest edge the same shape is far
        more likely a fund that simply has not reported yet, so it counts.

    Farside's Total is a SUM over whatever cells are filled in. On a day that
    is still being published it is a partial sum that looks like a total
    (2026-10-02 read 31.7 until IBIT's 158.2 arrived), and on a day nobody has
    reported yet it is 0.0. Neither is the day's flow. Returns the dates whose
    Total was withheld.
    """
    last_seen: dict[int, int] = {}
    for i, r in enumerate(rows):
        for j in range(1, len(r)):
            if j != total_idx and r[j] is not None:
                last_seen[j] = i
    edge = len(rows) - settling
    withheld: list[str] = []
    for i, r in enumerate(rows):
        if r[total_idx] is None:
            continue
        window = rows[max(0, i - lookback):i]
        for j in range(1, len(r)):
            if j == total_idx or r[j] is not None:
                continue
            if i < edge and last_seen.get(j, -1) < i:
                continue  # never reports again: closed, not a gap in this day
            if any(j < len(w) and w[j] is not None for w in window):
                r[total_idx] = None
                withheld.append(r[0])
                break
    return withheld


def parse_flow_table(
    html: str,
    require: tuple[str, ...],
    unsettled: list[str] | None = None,
    withheld: list[str] | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Return (header, rows) in the repo's wide CSV shape, or ([], []) on failure.

    A cell that carries no reading is returned as "" (an empty CSV cell),
    never "0". The Total is "" as well unless the row is complete (see
    `withhold_incomplete_totals`); pass a list as `withheld` to receive those
    dates. Rows that are left with no reading at all (every cell "-", or the
    date cell alone, which is how Farside renders a day it has not posted
    yet) and information-free rows dated on a day the market was closed are
    left OUT of `rows`. Pass a list as `unsettled` to receive their dates, so
    the caller can disclose them. Absence is reported, never silently dropped.
    """
    p = _TableParser()
    p.feed(html)

    for tbl in p.tables:
        # Just enough to be a header + at least one data row. Do NOT gate on a
        # bigger number here: whether the table is substantial enough to trust
        # is decided once, at the write layer, by MIN_ROWS in refresh().
        if len(tbl) < 2:
            continue
        # Find the header row: the one naming the funds we require. Match
        # case-insensitively, but KEEP the original casing for the output
        # columns — the committed CSVs use "Total", not "TOTAL", and a case
        # mismatch would look like a schema change and discard prior rows.
        header: list[str] | None = None
        header_i = -1
        for i, row in enumerate(tbl[:6]):
            up = [c.upper().replace(" ", "") for c in row]
            if all(any(req in cell for cell in up) for req in require):
                header, header_i = row, i
                break
        if header is None:
            continue

        # First column is the date column; strip the rest to bare tickers.
        #
        # THE TRAILING TOTAL COLUMN. Farside's last column is the daily total,
        # and on the ETH page its header cell is EMPTY. The old fallback named
        # it positionally — "COL11" — and that shipped: data/eth_flows.csv on
        # main carries COL11 where it used to carry Total.
        #
        # That is not cosmetic. app.py's ensure_total() looks for a column
        # literally named "total" and, finding none, COMPUTES one by summing
        # every numeric column — including the unrecognised total itself. Every
        # ETH flow number on the dashboard was therefore exactly DOUBLE. The
        # committed data proves the shape: on 2026-07-30 the funds sum to 12.8
        # and COL11 is 12.8.
        #
        # So an unlabelled LAST column is named "Total", and the claim is then
        # VERIFIED below against the parsed rows rather than assumed — if it
        # does not behave like a total we fall back to the positional name and
        # let the schema-change guard refuse the write, which is the safe
        # direction.
        cols = ["date"]
        last_i = len(header) - 1
        unlabelled_last = -1
        for i, cell in enumerate(header[1:], start=1):
            tick = re.sub(r"[^A-Za-z0-9_]", "", cell)
            if not tick and i == last_i:
                unlabelled_last = len(cols)
                tick = "Total"
            cols.append(tick or "COL%d" % len(cols))

        raw: list[list] = []
        for row in tbl[header_i + 1:]:
            if not row:
                continue
            iso = _parse_date_cell(row[0])
            if not iso:
                continue  # skips "Total"/"Average" footer rows
            vals = [_parse_value(c) for c in row[1:len(cols)]]
            # Pad a short row with absence, NOT with zeros. A cell the markup
            # never emitted is missing data; calling it 0 invents a reading.
            vals += [None] * (len(cols) - 1 - len(vals))
            raw.append([iso] + vals)
        if not raw:
            continue
        raw.sort(key=lambda r: r[0])

        # EARN the "Total" name given to an unlabelled last column above. If
        # that column is really the daily total it equals the sum of the funds
        # beside it; if the table shape changed and it is actually a fund,
        # calling it Total would make ensure_total() adopt one fund's flow as
        # the whole day's. Check it against the real parsed rows.
        if unlabelled_last > 0 and not _behaves_like_a_total(raw, unlabelled_last):
            cols[unlabelled_last] = "COL%d" % unlabelled_last
            print("[etf-flows] last column is unlabelled and does NOT sum to "
                  "the funds beside it; leaving it positional so the "
                  "schema-change guard can refuse the write",
                  file=sys.stderr)

        total_idx = next((j for j, c in enumerate(cols)
                          if j and c.lower() == "total"), None)
        held = withhold_incomplete_totals(raw, total_idx) if total_idx else []

        rows: list[list[str]] = []
        skipped: list[str] = []
        for r in raw:
            vals = r[1:]
            if all(v is None for v in vals):
                # Not published yet, or the market was closed: no cell on this
                # row is a reading, so the row states nothing. Writing it would
                # publish a fabricated zero for every fund.
                skipped.append(r[0])
                continue
            if market_closed(r[0]):
                if all(_num(v) in (None, 0.0) for v in vals):
                    # No session, so no flow, whatever the template printed.
                    skipped.append(r[0])
                    continue
                print(f"[etf-flows] WARNING: {r[0]} was a market holiday or "
                      f"weekend but carries non-zero flows; keeping it, "
                      f"inspect the source", file=sys.stderr)
            # At least one real number, so this day IS a reading and must be
            # kept even if it totals zero. Its "-" cells stay empty.
            rows.append([r[0]] + ["" if v is None else v for v in vals])

        if rows:
            if unsettled is not None:
                unsettled[:] = skipped
            if withheld is not None:
                withheld[:] = [d for d in held if d not in skipped]
            return cols, rows

    return [], []


def _behaves_like_a_total(rows: list[list], idx: int,
                          tol: float = 0.15, need: float = 0.8) -> bool:
    """True when column ``idx`` equals the sum of the other value columns.

    Tolerant on purpose: Farside rounds each cell to one decimal, so a row of
    thirteen funds can drift from their own total by a few tenths without
    anything being wrong. Requires agreement on ``need`` of the rows that carry
    enough numbers to judge, so a handful of odd rows cannot veto a real total
    and a coincidental single match cannot manufacture one. Cells with no
    reading (None or "") are left out of the sum, as Farside leaves them out
    of its own Total.
    """
    agree = considered = 0
    for r in rows:
        vals = [_num(v) for v in r[1:]]
        # `idx` indexes `cols`, whose first entry is "date"; `vals` has that
        # entry stripped, so the candidate sits at idx-1 and a row is usable
        # when it has at least idx values. `<=` here skipped EVERY row when the
        # total was the last column — which is the only case this function is
        # ever called for.
        if len(vals) < idx:
            continue
        cand = vals[idx - 1]
        others = [v for j, v in enumerate(vals, start=1)
                  if j != idx and v is not None]
        if cand is None or not others:
            continue
        # An all-zero row agrees with everything; it is not evidence.
        if cand == 0 and not any(others):
            continue
        considered += 1
        if abs(sum(others) - cand) <= max(tol, abs(cand) * 0.01):
            agree += 1
    return considered >= 5 and (agree / considered) >= need


def drop_pre_launch(rows: dict[str, list[str]], first_day: str | None) -> list[str]:
    """Remove (in place) rows dated before the asset's first ETF trading day.

    Returns the dropped dates so the caller can name them on stderr. A flow
    dated before the funds existed is a paste/parse error by construction.
    """
    if not first_day:
        return []
    bad = sorted(d for d in rows if d < first_day)
    for d in bad:
        del rows[d]
    return bad


def duplicate_value_rows(rows: list[list[str]], min_nonzero: int = 3) -> list[tuple[str, str]]:
    """Pairs of dates whose per-fund value cells are byte-for-byte identical.

    Only rows with at least ``min_nonzero`` non-zero FUND cells (the trailing
    Total is excluded) are compared: several funds repeating the exact same
    flows on two different days is a copy-paste signature, whereas a single
    fund printing the same number twice (e.g. ETHA -12.8) is a plausible
    coincidence and must not be flagged. An empty (unreported) cell compares
    as absent, not as zero.
    """
    seen: dict[tuple, str] = {}
    dupes: list[tuple[str, str]] = []
    for r in sorted(rows, key=lambda x: x[0]):
        vals = tuple(_num(v) for v in r[1:-1])
        if sum(1 for v in vals if v) < min_nonzero:
            continue
        if vals in seen:
            dupes.append((seen[vals], r[0]))
        else:
            seen[vals] = r[0]
    return dupes


def read_existing(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    if not path.exists():
        return [], {}
    with path.open(newline="", encoding="utf-8") as fh:
        r = list(csv.reader(fh))
    if not r:
        return [], {}
    return r[0], {row[0]: row for row in r[1:] if row}


def newest(dates) -> str:
    return max(dates) if dates else ""


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _gh_escape(text: str) -> str:
    """Workflow-command escaping: % CR LF would truncate the annotation."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def emit_warning(title: str, message: str) -> None:
    print(f"[etf-flows] WARNING: {title}: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title={title}::{_gh_escape(message)}")


def is_blocked(exc: BaseException) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and exc.code == 403


def explain_fetch_error(source: str, url: str, exc: BaseException) -> str:
    """A 403 names the host and the refusal. Other failures stay generic."""
    if is_blocked(exc):
        headers = getattr(exc, "headers", None)
        mitigated = ""
        if headers is not None and headers.get("cf-mitigated"):
            mitigated = f" Cloudflare cf-mitigated={headers.get('cf-mitigated')}."
        reason = getattr(exc, "reason", None) or "Forbidden"
        return (f"{source} returned HTTP 403 {reason} for {url}.{mitigated} "
                f"The host is blocking this request. Not retrying with a "
                f"different User-Agent, proxy, or browser.")
    return f"{source} fetch failed ({type(exc).__name__}: {exc}) for {url}"


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("payload is not a JSON object")
    return data


def usd_to_millions(value) -> str | None:
    """TFTC publishes USD. This repo's CSVs are US$ millions, one decimal."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"flow is not a number: {value!r}")
    if isinstance(value, float) and (value != value or abs(value) == float("inf")):
        raise ValueError(f"flow is not a number: {value!r}")
    return f"{round(float(value) / 1_000_000.0, 1):g}"


def _manual_cell(value) -> str | None:
    """None when the cell has no reading. 0 is a reading. Junk is an error."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"unsupported value {value!r}")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or abs(value) == float("inf")):
            raise ValueError(f"unsupported value {value!r}")
        return f"{float(value):g}"
    parsed = _parse_value(value)
    if parsed is None and value.strip() not in _NO_READING:
        raise ValueError(f"not a number or a blank, got {value!r}")
    return parsed


def _parse_manual_row(row, label: str, asset: str, index: int):
    if not isinstance(row, dict):
        return None, f"{label}: {asset} row {index} is not an object"
    if "date" not in row or str(row.get("date") or "").strip() == "":
        return None, f"{label}: {asset} row {index} is missing date"
    try:
        iso = date.fromisoformat(str(row["date"]).strip()[:10]).isoformat()
    except ValueError:
        return None, (f"{label}: {asset} row {index} date must be YYYY-MM-DD, "
                      f"got {row['date']!r}")
    cells: dict[str, str | None] = {}
    for key, val in row.items():
        if key in ("date", "asset") or str(key).startswith("_"):
            continue
        try:
            cells[str(key).strip()] = _manual_cell(val)
        except ValueError as e:
            return None, f"{label}: {asset} {iso} column {key}: {e}"
    return {"date": iso, "cells": cells}, None


def parse_manual_document(text: str, label: str = "manual entry"):
    """JSON ``{"btc": [...], "eth": [...]}`` or a CSV with an ``asset`` column.

    Returns ``({asset: [entry, ...]}, None)`` or ``({}, error)``. An entry is
    ``{"date", "cells"}`` and a cell is a numeric string or None (no reading).
    """
    raw = text.strip()
    if not raw:
        return {"btc": [], "eth": []}, None
    if raw[0] == "{":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return {}, f"{label}: invalid JSON ({e})"
        if not isinstance(data, dict):
            return {}, f"{label}: expected a JSON object with btc and eth arrays"
        unknown = [k for k in data
                   if k not in ("btc", "eth") and not str(k).startswith("_")]
        if unknown:
            return {}, (f"{label}: unknown key(s) {', '.join(map(str, unknown))}; "
                        f"expected btc and eth")
        out: dict[str, list] = {"btc": [], "eth": []}
        for asset in ("btc", "eth"):
            rows = data.get(asset, [])
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                return {}, f"{label}: {asset} must be a list of row objects"
            for i, row in enumerate(rows, start=1):
                entry, err = _parse_manual_row(row, label, asset, i)
                if err:
                    return {}, err
                out[asset].append(entry)
        return out, None
    if raw[0] == "[":
        return {}, (f"{label}: a bare JSON list has no asset. Use "
                    f'{{"btc": [...], "eth": [...]}}.')
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        return {}, f"{label}: CSV has no header"
    fields = {(f or "").strip() for f in reader.fieldnames}
    if "asset" not in fields or "date" not in fields:
        return {}, f"{label}: CSV needs asset and date columns"
    out = {"btc": [], "eth": []}
    for i, rec in enumerate(reader, start=1):
        norm = {(k or "").strip(): v for k, v in rec.items()}
        asset = (norm.get("asset") or "").strip().lower()
        if asset not in out:
            return {}, f"{label}: row {i} asset must be btc or eth, got {asset!r}"
        entry, err = _parse_manual_row(norm, label, asset, i)
        if err:
            return {}, err
        out[asset].append(entry)
    return out, None


# Set by main() from --manual-entry. Later sources win on the same date.
MANUAL_EXTRA_TEXT = ""


def load_manual(asset: str):
    """Rows for ``asset`` from the committed file, then the env, then the CLI.

    Returns ``(entries, None)`` or ``([], error)``. Entries later in that
    order replace earlier ones for the same date, so a workflow_dispatch
    input wins over the committed file.
    """
    chunks: list[tuple[str, str]] = []
    if MANUAL_PATH.exists():
        chunks.append((str(MANUAL_PATH.name), MANUAL_PATH.read_text(encoding="utf-8")))
    env = os.environ.get(MANUAL_ENV, "")
    if env.strip():
        chunks.append((MANUAL_ENV, env))
    if MANUAL_EXTRA_TEXT.strip():
        chunks.append(("--manual-entry", MANUAL_EXTRA_TEXT))
    by_date: dict[str, dict] = {}
    order: list[str] = []
    for label, text in chunks:
        parsed, err = parse_manual_document(text, label)
        if err:
            return [], err
        for entry in parsed.get(asset, []):
            if entry["date"] not in by_date:
                order.append(entry["date"])
            by_date[entry["date"]] = entry
    return [by_date[d] for d in order], None


def _blank_to_none(cell) -> str | None:
    if cell is None:
        return None
    s = str(cell).strip()
    return s if s else None


def overlay_manual(header: list[str], merged: dict[str, list[str]],
                   entries: list[dict], protected: set[str], cfg: dict):
    """Merge manual rows into ``merged`` (copied). Returns (out, notes, error).

    ``protected`` dates are left alone — a live Farside row wins. Any other
    manual date replaces the stored row. An empty cell stays empty. A row
    with no reading, or a closed-market row of zeros, is not written.
    """
    if not entries:
        return merged, [], None
    if not header:
        return merged, [], "manual rows arrived but the CSV has no header to place them in"
    known = set(header)
    total_idx = next((j for j, c in enumerate(header) if j and c.lower() == "total"), None)
    first = cfg.get("first_trading_day")
    out = {d: list(r) for d, r in merged.items()}
    notes: list[str] = []
    skipped_protected: list[str] = []
    for entry in entries:
        iso = entry["date"]
        if first and iso < first:
            return merged, notes, (f"manual {iso} is before the first ETF trading "
                                   f"day {first}")
        if iso in protected:
            skipped_protected.append(iso)
            continue
        unknown = sorted(c for c in entry["cells"] if c not in known)
        if unknown:
            return merged, notes, (f"manual {iso} has unknown column(s) "
                                   f"{', '.join(unknown)}; columns are "
                                   f"{', '.join(header)}")
        values = [entry["cells"].get(c) for c in header[1:]]
        if all(v is None for v in values):
            notes.append(f"{iso} had no reading and was not written")
            if iso in out and carries_no_information(out[iso]):
                del out[iso]
            continue
        prior = [d for d in sorted(out) if d < iso][-ACTIVE_LOOKBACK:]
        series = []
        for d in prior:
            row = out[d]
            series.append([d] + [_blank_to_none(row[i] if i < len(row) else None)
                                 for i in range(1, len(header))])
        series.append([iso] + list(values))
        if total_idx:
            withhold_incomplete_totals(series, total_idx)
        final = series[-1]
        vals = final[1:]
        if all(v is None for v in vals):
            notes.append(f"{iso} had no reading and was not written")
            continue
        if market_closed(iso) and all(_num(v) in (None, 0.0) for v in vals):
            notes.append(f"{iso} is a weekend or NYSE holiday with no flow "
                         f"and was not written")
            if iso in out and carries_no_information(out[iso]):
                del out[iso]
            continue
        if market_closed(iso):
            notes.append(f"WARNING: {iso} was a market holiday or weekend but "
                         f"the manual row has non-zero flows; keeping it")
        out[iso] = [iso] + ["" if v is None else str(v) for v in vals]
    if skipped_protected:
        shown = ", ".join(skipped_protected[:6])
        extra = len(skipped_protected) - 6
        if extra > 0:
            shown += f", +{extra} more"
        notes.append(f"manual date(s) left to Farside: {shown}")
    return out, notes, None


def parse_tftc_payload(payload: dict, header: list[str], require: tuple[str, ...]):
    """TFTC's CC BY 4.0 JSON into rows aligned to ``header``.

    Returns ``(rows, warnings, error)``. A missing per-fund key is no reading.
    A literal 0 becomes ``"0"``. The license and the USD unit are checked here
    so a changed file is refused instead of copied.
    """
    if not isinstance(payload, dict):
        return [], [], "backup payload is not a JSON object"
    license_url = str(payload.get("license") or "")
    if _CC_BY_40 not in license_url:
        shown = license_url or "missing"
        return [], [], (f"backup refused: license is {shown!r}, expected CC BY 4.0")
    units = str(payload.get("units") or "")
    if units.upper() != "USD":
        return [], [], f"backup refused: units are {units!r}, expected USD"
    days = payload.get("days")
    if not isinstance(days, list):
        return [], [], "backup payload has no days array"
    if len(days) < MIN_ROWS:
        return [], [], f"backup refused: {len(days)} days (< {MIN_ROWS})"
    if not header or header[0] != "date":
        return [], [], "backup target CSV has no header"
    known = set(header)
    unknown_hits: dict[str, int] = {}
    raw: list[list] = []
    recent_cut = None
    # Unknown-fund warnings only for the newest days, which are the ones a
    # gap-fill might write. History we will not touch should not shout daily.
    dated = []
    for i, day in enumerate(days):
        if not isinstance(day, dict) or not day.get("date"):
            return [], [], f"backup day {i} has no date"
        try:
            iso = date.fromisoformat(str(day["date"]).strip()[:10]).isoformat()
        except ValueError:
            return [], [], f"backup day {i} date is not YYYY-MM-DD: {day.get('date')!r}"
        dated.append(iso)
    if dated:
        recent_cut = sorted(dated)[-15]
    for i, day in enumerate(days):
        iso = date.fromisoformat(str(day["date"]).strip()[:10]).isoformat()
        per = day.get("perEtfUsd") or {}
        if per is None:
            per = {}
        if not isinstance(per, dict):
            return [], [], f"backup {iso} perEtfUsd is not an object"
        if recent_cut and iso >= recent_cut:
            for ticker, val in per.items():
                if ticker not in known and val not in (None, 0, 0.0):
                    unknown_hits[ticker] = unknown_hits.get(ticker, 0) + 1
        try:
            vals = []
            for col in header[1:]:
                if col.lower() == "total":
                    vals.append(usd_to_millions(day.get("netFlowUsd")))
                elif col in per:
                    vals.append(usd_to_millions(per[col]))
                else:
                    vals.append(None)
        except ValueError as e:
            return [], [], f"backup {iso}: {e}"
        raw.append([iso] + vals)
    raw.sort(key=lambda r: r[0])
    newest_row = raw[-1]
    for req in require:
        if req not in header:
            return [], [], f"backup target header has no {req} column"
        if newest_row[header.index(req)] is None:
            return [], [], f"backup newest day {newest_row[0]} is missing {req}"
    total_idx = next((j for j, c in enumerate(header) if j and c.lower() == "total"), None)
    held = withhold_incomplete_totals(raw, total_idx) if total_idx else []
    rows: list[list[str]] = []
    skipped = 0
    for r in raw:
        vals = r[1:]
        if all(v is None for v in vals):
            skipped += 1
            continue
        if market_closed(r[0]) and all(_num(v) in (None, 0.0) for v in vals):
            skipped += 1
            continue
        rows.append([r[0]] + ["" if v is None else v for v in vals])
    if len(rows) < MIN_ROWS:
        return [], [], f"backup refused: {len(rows)} usable rows (< {MIN_ROWS})"
    warnings = []
    if unknown_hits:
        detail = ", ".join(f"{k} on {n} recent day(s)"
                          for k, n in sorted(unknown_hits.items()))
        warnings.append(f"backup funds not in the CSV were left out: {detail}")
    if skipped:
        warnings.append(f"{skipped} backup row(s) had no reading and were not used")
    if held:
        warnings.append(f"Total withheld on {len(held)} backup row(s) with a fund still missing")
    return rows, warnings, None


def gap_fill(merged: dict[str, list[str]], rows: list[list[str]]) -> list[str]:
    """Insert rows for dates that are absent or carry no information.

    A date that already has a real reading is left exactly as stored. Returns
    the dates that changed.
    """
    added = []
    for r in rows:
        current = merged.get(r[0])
        if current is None or carries_no_information(current):
            if current != r:
                merged[r[0]] = r
                added.append(r[0])
    return added


def newest_reported(header: list[str], rows: dict[str, list[str]]) -> str:
    """Newest date whose Total is filled in.

    Matches the health probe: a trailing row with an empty Total is a day
    still being published, and it does not make the feed fresh.
    """
    if not rows:
        return ""
    total_i = None
    if header and header[-1].strip().lower() == "total":
        total_i = len(header) - 1
    for d in sorted(rows, reverse=True):
        row = rows[d]
        if total_i is None:
            return d
        if total_i < len(row) and str(row[total_i]).strip():
            return d
    return ""


def describe_staleness(path: Path, header: list[str], rows: dict[str, list[str]],
                       today: date | None = None) -> tuple[bool, str]:
    """(is_stale, prose). The limit is the same 56 weekday-hour budget /health/ uses."""
    today = today or _today()
    newest_iso = newest_reported(header, rows)
    limit = threshold_for(path.name).stale_h
    if not newest_iso:
        return True, f"{path.name} has no row with a Total (health limit {limit:.0f} weekday-hours)"
    last = date.fromisoformat(newest_iso)
    now = datetime.now(timezone.utc)
    if today != now.date():
        now = datetime(today.year, today.month, today.day, 23, tzinfo=timezone.utc)
    age_h = trading_day_age_h(last, now.timestamp())
    end = today - timedelta(days=1)
    missing = []
    d = last + timedelta(days=1)
    while d <= end:
        if not market_closed(d.isoformat()):
            missing.append(d.isoformat())
        d += timedelta(days=1)
    shown = ", ".join(missing[:8])
    if len(missing) > 8:
        shown += f", +{len(missing) - 8} more"
    text = (f"newest reported day {newest_iso} is {age_h:.0f} weekday-hours old "
            f"(health limit {limit:.0f}h). Missing sessions: {shown or 'none'}")
    return age_h > limit, text


def _write_table(path: Path, header: list[str], merged: dict[str, list[str]]) -> None:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    for d in sorted(merged):
        w.writerow(merged[d])
    path.write_text(out.getvalue(), encoding="utf-8")


def _show(path: Path):
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:          # path outside the repo (tests use a tmpdir)
        return path


def _same_rows(old: dict[str, list[str]], new: dict[str, list[str]]) -> bool:
    if set(old) != set(new):
        return False
    return all(list(new[d]) == list(old[d]) for d in old)


def _notes(asset: str, notes: list[str]) -> None:
    for note in notes:
        print(f"[{asset}] {note}", file=sys.stderr)


def _warn_if_stale(asset: str, path: Path, header: list[str],
                   rows: dict[str, list[str]], causes: list[str]) -> bool:
    stale, text = describe_staleness(path, header, rows)
    if not stale:
        return False
    cause = " ".join(causes)
    emit_warning(
        f"ETF flows not refreshed ({asset})",
        f"{cause} {text}. Add the missing days to data/etf_flows_manual.json "
        f"or re-run etf-flows-daily with the manual_entry input. An empty cell "
        f"is unreported, never 0.",
    )
    return True


def _fallback(asset: str, cfg: dict, path: Path, old_header: list[str],
              old_rows: dict[str, list[str]], manual: list[dict],
              exc: BaseException) -> int:
    """Farside raised. Try the backup, then the manual override.

    A clean block (HTTP 403 from every source that exists) warns and returns
    0. The CSV is only written when a backup or the override actually adds or
    replaces a row. Anything else — a timeout, a backup that no longer says
    CC BY 4.0, a malformed override — returns 1 and does not write.
    """
    cause = explain_fetch_error("Farside", cfg["url"], exc)
    print(f"[{asset}] {cause}", file=sys.stderr)
    farside_blocked = is_blocked(exc)
    causes = [cause]
    backup_status = "absent"
    header = list(old_header)
    merged = {d: list(r) for d, r in old_rows.items()}

    backup = cfg.get("backup")
    if backup and not header:
        backup_status = "failed"
        msg = f"{backup['name']} not applied: {path.name} has no header"
        print(f"[{asset}] {msg}", file=sys.stderr)
        causes.append(msg)
    elif backup:
        print(f"[{asset}] Farside did not return a table; trying {backup['name']} "
              f"({backup['url']})", file=sys.stderr)
        try:
            payload = fetch_json(backup["url"])
        except OSError as e:
            backup_status = "blocked" if is_blocked(e) else "failed"
            msg = explain_fetch_error(backup["name"], backup["url"], e)
            print(f"[{asset}] {msg}", file=sys.stderr)
            causes.append(msg)
        except (json.JSONDecodeError, ValueError) as e:
            backup_status = "failed"
            msg = (f"{backup['name']} payload could not be read "
                   f"({type(e).__name__}: {e})")
            print(f"[{asset}] {msg}", file=sys.stderr)
            causes.append(msg)
        else:
            rows, warnings, err = parse_tftc_payload(payload, header, cfg["require"])
            if err:
                backup_status = "failed"
                print(f"[{asset}] {err}", file=sys.stderr)
                causes.append(f"{backup['name']}: {err}")
            else:
                backup_status = "ok"
                for w in warnings:
                    print(f"[{asset}] {w}", file=sys.stderr)
                added_dates = gap_fill(merged, rows)
                if added_dates:
                    print(f"[{asset}] filled {len(added_dates)} date(s) from "
                          f"{backup['name']} ({backup['attribution']}), "
                          f"CC BY 4.0 ({backup['license']}). USD converted to "
                          f"US$ millions, one decimal. Existing readings were "
                          f"not rewritten.", file=sys.stderr)
                else:
                    print(f"[{asset}] {backup['name']} had no dates beyond the "
                          f"rows already stored", file=sys.stderr)

    if manual:
        merged, notes, err = overlay_manual(header, merged, manual, set(), cfg)
        if err:
            print(f"[{asset}] {err} — leaving {path.name} untouched",
                  file=sys.stderr)
            return 1
        _notes(asset, notes)

    if header:
        dropped = drop_pre_launch(merged, cfg.get("first_trading_day"))
        if dropped:
            print(f"[{asset}] dropping {len(dropped)} row(s) dated before the first "
                  f"ETF trading day {cfg.get('first_trading_day')}: "
                  f"{', '.join(dropped)}", file=sys.stderr)

    changed = bool(header) and not _same_rows(old_rows, merged)
    hard = (not farside_blocked) or backup_status == "failed"
    if not changed:
        if not hard:
            if not _warn_if_stale(asset, path, header, old_rows, causes):
                print(f"[{asset}] no new rows. {path.name} is within the health "
                      f"limit; Farside remains blocked.", file=sys.stderr)
            return 0
        print(f"[{asset}] leaving {path.name} untouched", file=sys.stderr)
        return 1

    for a, b in duplicate_value_rows(list(merged.values())):
        print(f"[{asset}] WARNING: {b} repeats {a}'s per-fund flows exactly — "
              f"likely a copy-paste row; inspect {path.name}", file=sys.stderr)
    _write_table(path, header, merged)
    print(f"[{asset}] wrote {_show(path)} — {len(merged)} rows, "
          f"through {newest(list(merged))}")
    _warn_if_stale(asset, path, header, merged, causes)
    return 0


def refresh(asset: str) -> int:
    cfg = SOURCES[asset]
    path: Path = cfg["csv"]
    old_header, old_rows = read_existing(path)
    old_newest = newest(list(old_rows))

    manual, err = load_manual(asset)
    if err:
        print(f"[{asset}] {err} — leaving {path.name} untouched", file=sys.stderr)
        return 1

    try:
        html = fetch_html(cfg["url"])
    except OSError as e:
        return _fallback(asset, cfg, path, old_header, old_rows, manual, e)

    unsettled: list[str] = []
    withheld: list[str] = []
    header, rows = parse_flow_table(html, cfg["require"], unsettled, withheld)
    if not rows:
        print(f"[{asset}] could not locate the flow table (markup changed?) — "
              f"leaving {path.name} untouched", file=sys.stderr)
        return 1
    if len(rows) < MIN_ROWS:
        print(f"[{asset}] only {len(rows)} rows parsed (< {MIN_ROWS}) — refusing to "
              f"overwrite {path.name} with a partial table", file=sys.stderr)
        return 1

    new_newest = newest([r[0] for r in rows])

    if unsettled:
        print(f"[{asset}] {len(unsettled)} row(s) carried no reading and were not "
              f"written (not published yet, or market closed): "
              f"{', '.join(unsettled)}", file=sys.stderr)

    # A withheld Total belongs at the newest edge, where Farside is still
    # filling the day in. Many of them further back mean the "-" convention
    # this parser relies on has changed; see MAX_HISTORICAL_WITHHELD. The edge
    # is the newest SETTLING_ROWS rows of the TABLE (written or not), the same
    # window withhold_incomplete_totals uses.
    settling = set(sorted([r[0] for r in rows] + unsettled)[-SETTLING_ROWS:])
    pending = [d for d in withheld if d in settling]
    historical = [d for d in withheld if d not in settling]
    if pending:
        print(f"[{asset}] {len(pending)} partly published day(s) written with "
              f"an empty Total (funds still missing): {', '.join(pending)}",
              file=sys.stderr)
    if len(historical) > MAX_HISTORICAL_WITHHELD:
        print(f"[{asset}] {len(historical)} settled rows have a fund showing '-' "
              f"(first: {', '.join(historical[:8])}) — Farside no longer seems to "
              f"use '-' only for 'not reported'; refusing to blank their Totals "
              f"in {path.name}", file=sys.stderr)
        return 1
    if historical:
        print(f"[{asset}] WARNING: Total withheld on {len(historical)} settled "
              f"row(s) where a reporting fund shows '-': {', '.join(historical)}",
              file=sys.stderr)

    # Drop rows a pre-fix run left on disk that carry no information (every
    # cell zero or empty) and that the source does not back with a reading:
    #   * dated AFTER the newest day the source reports. Left in place they
    #     keep publishing a fake zero cliff and pin old_newest into the future,
    #     so the regress guard below would reject every future fetch forever;
    #   * dated on a day the source lists with no reading, or on a weekend or
    #     NYSE holiday: the old parser wrote those "-" rows as zeros, and the
    #     merge below would otherwise keep them forever, because the fresh
    #     parse no longer emits a row to overwrite them with.
    # Real history, including a genuine all-zero day the source still reports,
    # is never touched.
    unbacked = set(unsettled)
    placeholders = sorted(d for d, r in old_rows.items()
                          if carries_no_information(r)
                          and (d > new_newest or d in unbacked or market_closed(d)))
    for d in placeholders:
        del old_rows[d]
    if placeholders:
        print(f"[{asset}] dropping {len(placeholders)} information-free row(s) "
              f"the source does not report (unpublished, holiday or past "
              f"{new_newest}): {', '.join(placeholders)}", file=sys.stderr)
        old_newest = newest(list(old_rows))

    if old_newest and new_newest < old_newest:
        print(f"[{asset}] fetched data ends {new_newest}, older than the {old_newest} "
              f"already on disk — refusing to regress {path.name}", file=sys.stderr)
        return 1

    # Merge: keep every historical date we already have, let fresh rows win.
    # Never truncate — a short upstream table must not delete our history.
    merged: dict[str, list[str]] = {}
    if old_header and old_header == header:
        merged.update(old_rows)
    elif old_header:
        print(f"[{asset}] column set changed\n    old: {old_header}\n    new: {header}\n"
              f"    keeping fetched columns; prior rows are re-derived from this fetch",
              file=sys.stderr)
    for r in rows:
        merged[r[0]] = r

    if manual:
        # Farside published these dates, so the override cannot clobber them.
        merged, notes, err = overlay_manual(
            header, merged, manual, {r[0] for r in rows}, cfg)
        if err:
            print(f"[{asset}] {err} — leaving {path.name} untouched",
                  file=sys.stderr)
            return 1
        _notes(asset, notes)

    dropped = drop_pre_launch(merged, cfg.get("first_trading_day"))
    if dropped:
        print(f"[{asset}] dropping {len(dropped)} row(s) dated before the first "
              f"ETF trading day {cfg.get('first_trading_day')}: {', '.join(dropped)}",
              file=sys.stderr)
    for a, b in duplicate_value_rows(list(merged.values())):
        # Cannot tell which date is real, so shout instead of guessing.
        print(f"[{asset}] WARNING: {b} repeats {a}'s per-fund flows exactly — "
              f"likely a copy-paste row; inspect {path.name}", file=sys.stderr)

    _write_table(path, header, merged)

    added = len(merged) - len(old_rows)
    through = newest(list(merged)) or new_newest
    print(f"[{asset}] wrote {_show(path)} — {len(merged)} rows "
          f"(+{added} new), through {through} (was {old_newest or 'empty'})")
    return 0


def main() -> int:
    global MANUAL_EXTRA_TEXT
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset", choices=sorted(SOURCES), action="append",
                    help="limit to one asset (repeatable); default is all")
    ap.add_argument("--manual-entry", metavar="FILE",
                    help="JSON or CSV to merge this run, in addition to "
                         "data/etf_flows_manual.json and ETF_FLOWS_MANUAL. "
                         "On a date that appears in more than one, this file wins.")
    args = ap.parse_args()
    if args.manual_entry:
        MANUAL_EXTRA_TEXT = Path(args.manual_entry).read_text(encoding="utf-8")
    assets = args.asset or sorted(SOURCES)

    rc = 0
    for a in assets:
        rc |= refresh(a)
    if rc:
        print("\nAt least one asset failed; existing CSVs were preserved.",
              file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
