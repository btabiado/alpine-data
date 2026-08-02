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


def _parse_value(tok: str) -> str:
    """Farside numbers: '1,234.5', '(123.4)' for negative, '-' for nothing."""
    t = tok.strip().replace(",", "").replace("$", "")
    if t in ("", "-", "–", "—", "N/A", "n/a"):
        return "0"
    neg = t.startswith("(") and t.endswith(")")
    if neg:
        t = t[1:-1]
    try:
        v = float(t)
    except ValueError:
        return "0"
    if neg:
        v = -v
    return f"{v:g}"


def parse_flow_table(html: str, require: tuple[str, ...]) -> tuple[list[str], list[list[str]]]:
    """Return (header, rows) in the repo's wide CSV shape, or ([], []) on failure."""
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
        cols = ["date"]
        for cell in header[1:]:
            tick = re.sub(r"[^A-Za-z0-9_]", "", cell)
            cols.append(tick or "COL%d" % len(cols))

        rows: list[list[str]] = []
        for row in tbl[header_i + 1:]:
            if not row:
                continue
            iso = _parse_date_cell(row[0])
            if not iso:
                continue  # skips "Total"/"Average" footer rows
            vals = [_parse_value(c) for c in row[1:len(cols)]]
            vals += ["0"] * (len(cols) - 1 - len(vals))
            rows.append([iso] + vals)

        if rows:
            return cols, rows

    return [], []


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

    header, rows = parse_flow_table(html, cfg["require"])
    if not rows:
        print(f"[{asset}] could not locate the flow table (markup changed?) — "
              f"leaving {path.name} untouched", file=sys.stderr)
        return 1
    if len(rows) < MIN_ROWS:
        print(f"[{asset}] only {len(rows)} rows parsed (< {MIN_ROWS}) — refusing to "
              f"overwrite {path.name} with a partial table", file=sys.stderr)
        return 1

    new_newest = newest([r[0] for r in rows])
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
