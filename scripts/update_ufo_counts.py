#!/usr/bin/env python3
"""Update the UAP dashboard from one saved NUFORC page.

This script does not use the network. Open the index in a browser yourself,
save it, then run:

    python3 scripts/update_ufo_counts.py

Put the saved HTML, CSV, or copied table in incoming/nuforc/ first. You can
also pass the file path:

    python3 scripts/update_ufo_counts.py "/path/to/saved page.html"

On success the script rewrites data-mufon.json with counts only (month, state,
shape, and the other totals the UAP tab already draws) and deletes the saved
file. Report text is never written.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fetch_mufon as fm  # noqa: E402


INCOMING_DIR = ROOT / "incoming" / "nuforc"
DEFAULT_OUT = ROOT / "data-mufon.json"
MANUAL_URL = "https://nuforc.org/subndx/?id=all"
# A saved page that is this much smaller than the counts already on file is
# treated as a partial copy, not a full index. Replacing would wipe history.
SHRINK_GUARD_MIN_EXISTING = 1000
SHRINK_GUARD_FRACTION = 0.5
_ACCEPTED_SUFFIXES = {".html", ".htm", ".csv", ".txt", ".tsv"}

_DATE_HEADERS = {
    "occurred", "datetime", "date", "date/time", "date / time", "date time",
    "sighted", "event date", "time",
}
_CITY_HEADERS = {"city", "town"}
_STATE_HEADERS = {"state", "st"}
_SHAPE_HEADERS = {"shape", "form"}


class _TableParser(HTMLParser):
    """Collect every HTML table as a list of rows of cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._skip = 0
        self._in_cell = False
        self._cell: list[str] = []
        self._row: list[str] = []
        self._table: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "table":
            if self._table_depth == 0:
                self._table = []
            self._table_depth += 1
        elif self._table_depth and tag == "tr":
            self._row = []
        elif self._table_depth and tag in {"td", "th"}:
            self._in_cell = True
            self._cell = []
        elif self._in_cell and tag == "br":
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
            return
        if self._skip:
            return
        if tag in {"td", "th"} and self._in_cell:
            self._row.append(" ".join("".join(self._cell).split()))
            self._in_cell = False
            self._cell = []
        elif tag == "tr" and self._table_depth:
            if any(cell.strip() for cell in self._row):
                self._table.append(self._row)
            self._row = []
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
            if self._table_depth == 0 and self._table:
                self.tables.append(self._table)
                self._table = []

    def handle_data(self, data: str) -> None:
        if self._skip or not self._in_cell:
            return
        self._cell.append(data)


def _header_key(cell: str) -> str:
    return re.sub(r"\s+", " ", cell.strip().lower())


def _header_index(headers: list[str], names: set[str]) -> int | None:
    for i, cell in enumerate(headers):
        if _header_key(cell) in names:
            return i
    return None


def _is_header_row(row: list[str]) -> bool:
    keys = {_header_key(cell) for cell in row}
    if keys & _DATE_HEADERS:
        return True
    return bool(keys & _CITY_HEADERS) and bool(keys & _STATE_HEADERS)


def _cell(row: list[str], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return row[idx].strip()


def _rows_from_indexes(table: list[list[str]], start: int,
                       dt_idx: int, city_idx: int, state_idx: int,
                       shape_idx: int) -> list[dict]:
    rows: list[dict] = []
    for raw in table[start:]:
        dt = fm._parse_datetime(_cell(raw, dt_idx))
        if dt is None:
            continue
        shape = (fm._norm_shape(_cell(raw, shape_idx))
                 if shape_idx >= 0 else "unknown")
        rows.append({
            "dt": dt,
            "city": fm._norm_city(_cell(raw, city_idx)),
            "state": fm._norm_state(_cell(raw, state_idx)),
            "shape": shape,
        })
    return rows


def _positional_layout(row: list[str]) -> tuple[int, int, int, int] | None:
    """Guess NUFORC column positions when the paste has no header row.

    Monthly index rows are link, occurred, city, state, country, shape, ...
    Older mirror rows are datetime, city, state, country, shape, ...
    """
    if not row:
        return None
    if fm._parse_datetime(row[0].strip()):
        shape_idx = 4 if len(row) > 4 else -1
        return 0, 1 if len(row) > 1 else -1, 2 if len(row) > 2 else -1, shape_idx
    if len(row) > 1 and fm._parse_datetime(row[1].strip()):
        shape_idx = 5 if len(row) > 5 else -1
        return 1, 2, 3 if len(row) > 3 else -1, shape_idx
    return None


def rows_from_table(table: list[list[str]]) -> list[dict]:
    """Turn one table into count-only rows. Summary text is not kept."""
    if not table:
        return []
    header_at = next((i for i, row in enumerate(table[:8]) if _is_header_row(row)), None)
    if header_at is not None:
        headers = table[header_at]
        dt_idx = _header_index(headers, _DATE_HEADERS)
        if dt_idx is None:
            return []
        city_idx = _header_index(headers, _CITY_HEADERS)
        state_idx = _header_index(headers, _STATE_HEADERS)
        shape_idx = _header_index(headers, _SHAPE_HEADERS)
        return _rows_from_indexes(
            table, header_at + 1,
            dt_idx,
            city_idx if city_idx is not None else -1,
            state_idx if state_idx is not None else -1,
            shape_idx if shape_idx is not None else -1,
        )
    for row in table[:8]:
        layout = _positional_layout(row)
        if layout is not None:
            return _rows_from_indexes(table, 0, *layout)
    return []


def _looks_like_html(text: str) -> bool:
    sample = text.lstrip()[:4000].lower()
    return "<table" in sample or "<html" in sample or sample.startswith("<")


def rows_from_html(text: str) -> list[dict]:
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    best: list[dict] = []
    for table in parser.tables:
        parsed = rows_from_table(table)
        if len(parsed) > len(best):
            best = parsed
    return best


def _delimiter(text: str) -> str:
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line
            break
    if "\t" in first:
        return "\t"
    if first.count(";") > first.count(","):
        return ";"
    return ","


def rows_from_delimited(text: str) -> list[dict]:
    reader = csv.reader(io.StringIO(text), delimiter=_delimiter(text))
    table = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    return rows_from_table(table)


def parse_saved(text: str) -> list[dict]:
    """Parse a saved page or a copied table. Returns count-only rows."""
    if not text or not text.strip():
        return []
    if _looks_like_html(text):
        return rows_from_html(text)
    return rows_from_delimited(text)


def build_payload(rows: list[dict], *, now: datetime | None = None) -> dict:
    """Dashboard sidecar. Counts only — no report text, links, or ids."""
    payload = fm.aggregate(rows)
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["generated_at"] = stamp
    payload["source"] = "Manual copy of the NUFORC index (counts only)"
    payload["source_url"] = MANUAL_URL
    payload["data_through"] = payload["date_range"][1]
    # Windows end on the newest report in the file, not on today's date.
    payload["_stale"] = True
    payload["live_refresh"] = {
        "ok": True,
        "manual": True,
        "months_refreshed": 0,
        "cause": None,
        "blocked_by": None,
        "attempted_at": stamp,
    }
    return payload


def shrinks_history(rows: list[dict], out_path: Path) -> tuple[bool, int]:
    """True when the saved file is too small to replace the current counts."""
    if not out_path.is_file():
        return False, 0
    try:
        previous = json.loads(out_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return False, 0
    old = previous.get("total_records")
    if not isinstance(old, int):
        return False, 0
    if old >= SHRINK_GUARD_MIN_EXISTING and len(rows) < old * SHRINK_GUARD_FRACTION:
        return True, old
    return False, old


def find_input(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    if not INCOMING_DIR.is_dir():
        return None
    candidates = [
        p for p in INCOMING_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in _ACCEPTED_SUFFIXES and p.name != "README.md"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _write_payload(out_path: Path, payload: dict) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(out_path)


def _empty_message(path: Path) -> str:
    return (
        f"No report rows found in {path.name}.\n"
        "\n"
        "The saved file has to include the table of reports.\n"
        f"  1. Open {MANUAL_URL} in your browser and wait until the table is visible.\n"
        "  2. Save the page as HTML, or select the whole table and paste it into a\n"
        "     .csv or .txt file.\n"
        f"  3. Put that file in {INCOMING_DIR.relative_to(ROOT)} and run this command again.\n"
        "\n"
        "The counts file was not changed, and your saved file was left in place."
    )


def _shrink_message(path: Path, found: int, existing: int) -> str:
    return (
        f"Stopped. {path.name} has {found:,} reports, and the dashboard currently\n"
        f"has {existing:,}. This looks like part of the table, not the full list at\n"
        f"{MANUAL_URL}.\n"
        "\n"
        "The counts file was not changed, and your saved file was left in place.\n"
        "Copy the entire table, then run the command again. If this file really is\n"
        "the full list, run the same command with --replace."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Update UAP counts from a saved NUFORC page. No network.")
    parser.add_argument(
        "path", nargs="?",
        help="Saved HTML, CSV, or pasted table. Default: the newest file in incoming/nuforc/.")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=argparse.SUPPRESS)
    parser.add_argument(
        "--replace", action="store_true",
        help="Replace the counts even if the saved file is much smaller than the current file.")
    args = parser.parse_args(argv)

    src = find_input(args.path)
    if src is None:
        print(
            "No saved page found.\n"
            f"Open {MANUAL_URL} in your browser, save the page (or copy the table\n"
            f"into a .csv or .txt file), put it in {INCOMING_DIR.relative_to(ROOT)}/,\n"
            "and run:\n"
            "\n"
            "    python3 scripts/update_ufo_counts.py",
            file=sys.stderr)
        return 1
    if not src.is_file():
        print(f"File not found: {src}", file=sys.stderr)
        return 1

    try:
        text = src.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        print(f"Could not read {src}: {exc}", file=sys.stderr)
        return 1

    rows = parse_saved(text)
    if not rows:
        print(_empty_message(src), file=sys.stderr)
        return 1

    out_path = Path(args.out)
    shrink, existing = shrinks_history(rows, out_path)
    if shrink and not args.replace:
        print(_shrink_message(src, len(rows), existing), file=sys.stderr)
        return 1

    payload = build_payload(rows)
    _write_payload(out_path, payload)
    try:
        src.unlink()
    except OSError as exc:
        print(f"Updated {out_path.name}, but could not delete {src}: {exc}",
              file=sys.stderr)
        print("Delete that saved file yourself. Do not commit it.", file=sys.stderr)
        return 1

    through = payload.get("data_through") or "unknown"
    print(f"Updated {out_path.name}: {payload['total_records']:,} reports, "
          f"through {through}.")
    print(f"Deleted {src.name}. Report text was not saved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
