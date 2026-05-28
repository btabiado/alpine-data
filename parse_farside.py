"""
Parse the column-major Farside table format that appears when you paste
a Farside ETF flow table into a plain-text field.

Detected layout (per Farside's BTC table):
    Line 1:        "Date,"          (header marker)
    Lines 2..N:    fund tickers     (e.g. IBIT, FBTC, ..., Total)
    Then repeating blocks of (N) lines per day:
        first line:   "DD Mon YYYY,"
        next N-1:     numeric values, blanks as "-", negatives as "(123.4)"

Convert into a wide CSV:
    date,IBIT,FBTC,...,Total
    YYYY-MM-DD,...

Usable as:
    python parse_farside.py < input.txt > output.csv
or imported: parse_farside_vertical(text: str) -> str (wide CSV)
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from io import StringIO


_DATE_RE = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4}),?\s*$")


def _is_date_line(line: str) -> bool:
    return bool(_DATE_RE.match(line.strip()))


def _parse_date(line: str) -> str:
    m = _DATE_RE.match(line.strip())
    if not m:
        raise ValueError(f"not a date line: {line!r}")
    day = int(m.group(1))
    mon = m.group(2)[:3].title()
    year = int(m.group(3))
    dt = datetime.strptime(f"{day} {mon} {year}", "%d %b %Y")
    return dt.strftime("%Y-%m-%d")


def _parse_value(token: str) -> float:
    t = token.strip().replace(",", "").replace("$", "")
    if t in ("", "-", "—", "n/a", "N/A"):
        return 0.0
    if t.startswith("(") and t.endswith(")"):
        t = "-" + t[1:-1]
    try:
        return float(t)
    except ValueError:
        return 0.0


def looks_like_vertical_farside(text: str) -> bool:
    """Two accepted layouts:
      A) Header-prefix:  first line is 'Date' / 'Date,', followed by fund tickers, then date blocks.
      B) Header-less:    first non-empty line IS a date line. Block size is auto-detected
                         by the distance to the next date line.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    head = lines[0].strip().rstrip(",").lower()
    if head == "date":
        for i, ln in enumerate(lines[1:], start=1):
            if _is_date_line(ln):
                return i >= 3
        return False
    if _is_date_line(lines[0]):
        for i, ln in enumerate(lines[1:], start=1):
            if _is_date_line(ln):
                return i >= 2  # at least 1 numeric value between dates
        return False
    return False


# Default headers when paste is header-less.
# Ordered to match Farside's column layout (left → right) at the time the
# parser was written. Adjust by editing the CSV after import if needed.
DEFAULT_HEADERS = {
    "btc": ["IBIT", "FBTC", "BITB", "ARKB", "BTCO", "EZBC", "BRRR", "HODL", "BTCW", "MSBT", "GBTC", "BTC", "Total"],
    "eth": ["ETHA", "ETH2", "FETH", "ETHW", "CETH", "ETHV", "QETH", "EZET", "ETHE", "ETH_MINI", "Total"],
}


def parse_farside_vertical(text: str, asset_hint: str | None = None) -> str:
    """Convert vertical Farside paste into a wide CSV string.

    Handles both header-prefix and headerless layouts. If the paste is
    headerless, uses DEFAULT_HEADERS[asset_hint] (or generic c1..cN if no
    hint). Block size is auto-detected from the first date-to-date gap.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""

    head = lines[0].strip().rstrip(",").lower()
    headerless = head != "date"

    if headerless:
        if not _is_date_line(lines[0]):
            raise ValueError("first line is neither 'Date' header nor a date row")
        # Find next date line to compute block size
        first_date_idx = 0
        next_date_idx = None
        for i in range(1, len(lines)):
            if _is_date_line(lines[i]):
                next_date_idx = i
                break
        if next_date_idx is None:
            raise ValueError("only one date row found; cannot infer block size")
        n_values = next_date_idx - first_date_idx - 1
        # Pick a header
        if asset_hint and asset_hint.lower() in DEFAULT_HEADERS:
            hdr = DEFAULT_HEADERS[asset_hint.lower()]
            if len(hdr) != n_values:
                # Pad/truncate gracefully so we don't crash; surface in the CSV header
                if len(hdr) < n_values:
                    hdr = hdr + [f"col{i}" for i in range(len(hdr) + 1, n_values + 1)]
                else:
                    hdr = hdr[:n_values]
            funds = hdr
        else:
            funds = [f"col{i+1}" for i in range(n_values - 1)] + ["Total"] if n_values >= 1 else []
    else:
        # Header-prefix layout
        first_date_idx = None
        for i, ln in enumerate(lines[1:], start=1):
            if _is_date_line(ln):
                first_date_idx = i
                break
        if first_date_idx is None:
            raise ValueError("no date rows found")
        funds = [ln.strip() for ln in lines[1:first_date_idx]]
        n_values = len(funds)

    block_size = 1 + n_values  # date row + N values

    rows: list[list] = []
    i = first_date_idx
    while i < len(lines):
        date_ln = lines[i]
        if not _is_date_line(date_ln):
            i += 1
            continue
        date_iso = _parse_date(date_ln)
        vals = lines[i + 1 : i + block_size]
        if len(vals) < n_values:
            break
        parsed = [_parse_value(v) for v in vals[:n_values]]
        rows.append([date_iso] + parsed)
        i += block_size

    out = StringIO()
    out.write("date," + ",".join(funds) + "\n")
    for r in rows:
        out.write(",".join([r[0]] + [_fmt_num(v) for v in r[1:]]) + "\n")
    return out.getvalue()


def _fmt_num(v: float) -> str:
    if v == 0:
        return "0"
    # v == 0 already short-circuited above; only the magnitude test matters.
    return f"{v:.1f}" if abs(v) >= 0.1 else f"{v:g}"


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] not in ("-", "--stdin"):
        with open(argv[1], encoding="utf-8") as fh:
            text = fh.read()
    else:
        text = sys.stdin.read()
    if not looks_like_vertical_farside(text):
        print("input does not look like vertical Farside paste", file=sys.stderr)
        return 1
    sys.stdout.write(parse_farside_vertical(text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
