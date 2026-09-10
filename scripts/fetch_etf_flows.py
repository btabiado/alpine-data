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
  * and its newest date must be >= the newest date already on disk.

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

  * a row where EVERY cell is absent is not a reading of anything, so it is
    not written at all (and is counted + named on stderr, never dropped
    silently);
  * a row with at least one real number IS a reading, so it is written —
    INCLUDING an all-zero one. Farside prints a literal "0"/"0.0" on a
    genuine no-flow trading day, that parses to 0.0, not None, and the row
    survives. A real zero must stay representable.
  * within a written row, a per-fund "-" keeps rendering as 0: on a day the
    table has settled, Farside uses it for "this fund saw no creations or
    redemptions", which is a reading of zero. Only whole-row absence means
    "not settled yet".

The same split protects the freshness guard. A placeholder row of zeros
sitting on disk for a date the source has not reached would otherwise make
`new_newest < old_newest` true forever and wedge the scraper into permanent
refuse-to-regress. `refresh()` therefore drops all-zero rows dated AFTER the
newest row the source actually reports, before comparing. That is bounded to
unsettled dates — committed history is never touched by it.

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
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

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
    },
    "eth": {
        "url": "https://farside.co.uk/ethereum-etf-flow-all-data/",
        "csv": DATA_DIR / "eth_flows.csv",
        "require": ("ETHA", "FETH", "ETHE"),
    },
}

MIN_ROWS = 100  # both tables have 400+ rows of history; anything less is a bad parse

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


def _is_all_zero(row: list[str]) -> bool:
    """True when every value cell of a stored CSV row parses to exactly 0.

    Used only to identify placeholder rows written for a date the source had
    not settled. A row that is genuinely all zeros is indistinguishable on
    disk, which is why callers additionally bound this to dates the source
    does not report at all.
    """
    for cell in row[1:]:
        try:
            if float(cell) != 0.0:
                return False
        except ValueError:
            return False
    return True


def parse_flow_table(
    html: str,
    require: tuple[str, ...],
    unsettled: list[str] | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Return (header, rows) in the repo's wide CSV shape, or ([], []) on failure.

    Rows carrying no reading at all — every cell "-", or the date cell alone,
    which is how Farside renders a day it has not posted yet — are left OUT
    of `rows`. Pass a list as `unsettled` to receive their dates so the caller
    can disclose them; absence is reported, never silently discarded.
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

        rows: list[list[str]] = []
        skipped: list[str] = []
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
            if all(v is None for v in vals):
                # Not settled yet (or the market was closed): no cell on this
                # row is a reading, so the row states nothing. Writing it would
                # publish a fabricated zero for every fund.
                skipped.append(iso)
                continue
            # At least one real number, so this day IS a reading and must be
            # kept even if it totals zero. A remaining per-fund "-" on a
            # settled row means that fund saw no flow — a zero, not a gap.
            rows.append([iso] + [("0" if v is None else v) for v in vals])

        if rows:
            # EARN the "Total" name given to an unlabelled last column above.
            # If that column is really the daily total it equals the sum of the
            # funds beside it; if the table shape changed and it is actually a
            # fund, calling it Total would make ensure_total() adopt one fund's
            # flow as the whole day's. Check it against the real parsed rows.
            if unlabelled_last > 0 and not _behaves_like_a_total(rows, unlabelled_last):
                cols[unlabelled_last] = "COL%d" % unlabelled_last
                print("[etf-flows] last column is unlabelled and does NOT sum to "
                      "the funds beside it; leaving it positional so the "
                      "schema-change guard can refuse the write",
                      file=sys.stderr)
            if unsettled is not None:
                unsettled[:] = skipped
            return cols, rows

    return [], []


def _behaves_like_a_total(rows: list[list[str]], idx: int,
                          tol: float = 0.15, need: float = 0.8) -> bool:
    """True when column ``idx`` equals the sum of the other value columns.

    Tolerant on purpose: Farside rounds each cell to one decimal, so a row of
    thirteen funds can drift from their own total by a few tenths without
    anything being wrong. Requires agreement on ``need`` of the rows that carry
    enough numbers to judge, so a handful of odd rows cannot veto a real total
    and a coincidental single match cannot manufacture one.
    """
    agree = considered = 0
    for r in rows:
        try:
            vals = [float(v) for v in r[1:]]
        except (TypeError, ValueError):
            continue
        # `idx` indexes `cols`, whose first entry is "date"; `vals` has that
        # entry stripped, so the candidate sits at idx-1 and a row is usable
        # when it has at least idx values. `<=` here skipped EVERY row when the
        # total was the last column — which is the only case this function is
        # ever called for.
        if len(vals) < idx:
            continue
        cand = vals[idx - 1]
        others = [v for j, v in enumerate(vals, start=1) if j != idx]
        if cand is None or not others:
            continue
        # An all-zero row agrees with everything; it is not evidence.
        if cand == 0 and not any(others):
            continue
        considered += 1
        if abs(sum(others) - cand) <= max(tol, abs(cand) * 0.01):
            agree += 1
    return considered >= 5 and (agree / considered) >= need


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
    header, rows = parse_flow_table(html, cfg["require"], unsettled)
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
              f"written (unsettled or market closed): {', '.join(unsettled)}",
              file=sys.stderr)

    # Drop placeholder rows a pre-fix run may have left on disk: all-zero, and
    # dated AFTER the newest day the source actually reports, so they cannot be
    # real readings. Left in place they would (a) keep publishing a fake zero
    # cliff and (b) pin old_newest into the future, making the regress guard
    # below reject every future fetch forever. Bounded to unsettled dates —
    # real history, including genuine all-zero days, is never touched.
    placeholders = [d for d, r in old_rows.items()
                    if d > new_newest and _is_all_zero(r)]
    for d in placeholders:
        del old_rows[d]
    if placeholders:
        print(f"[{asset}] dropping {len(placeholders)} stale all-zero placeholder "
              f"row(s) dated past the source's newest day ({new_newest}): "
              f"{', '.join(sorted(placeholders))}", file=sys.stderr)
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
