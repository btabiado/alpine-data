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
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

try:  # run as `python scripts/fetch_etf_flows.py`: scripts/ is sys.path[0]
    from history_continuity import NYSE_HOLIDAYS
except ImportError:  # imported from the repo root (tests, parse_farside)
    from scripts.history_continuity import NYSE_HOLIDAYS

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

UA = "AlpineDataWorks-feed/1.0 (+https://alpinedataworks.com)"

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


def refresh(asset: str) -> int:
    cfg = SOURCES[asset]
    path: Path = cfg["csv"]
    old_header, old_rows = read_existing(path)
    old_newest = newest(list(old_rows))

    try:
        html = fetch_html(cfg["url"])
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"[{asset}] fetch failed ({type(e).__name__}: {e}) — "
              f"leaving {path.name} untouched", file=sys.stderr)
        return 1

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

    dropped = drop_pre_launch(merged, cfg.get("first_trading_day"))
    if dropped:
        print(f"[{asset}] dropping {len(dropped)} row(s) dated before the first "
              f"ETF trading day {cfg.get('first_trading_day')}: {', '.join(dropped)}",
              file=sys.stderr)
    for a, b in duplicate_value_rows(list(merged.values())):
        # Cannot tell which date is real, so shout instead of guessing.
        print(f"[{asset}] WARNING: {b} repeats {a}'s per-fund flows exactly — "
              f"likely a copy-paste row; inspect {path.name}", file=sys.stderr)

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(header)
    for d in sorted(merged):
        w.writerow(merged[d])
    path.write_text(out.getvalue(), encoding="utf-8")

    added = len(merged) - len(old_rows)
    try:
        shown = path.relative_to(REPO_ROOT)
    except ValueError:          # path outside the repo (tests use a tmpdir)
        shown = path
    print(f"[{asset}] wrote {shown} — {len(merged)} rows "
          f"(+{added} new), through {new_newest} (was {old_newest or 'empty'})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--asset", choices=sorted(SOURCES), action="append",
                    help="limit to one asset (repeatable); default is all")
    args = ap.parse_args()
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
