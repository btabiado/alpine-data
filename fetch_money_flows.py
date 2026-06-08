#!/usr/bin/env python3
"""
fetch_money_flows.py  —  ICI weekly money-market & long-term mutual-fund flows.

Feeds the Money Flow Index (MFX) composite (see SPEC_money_flow_index.md). Two
free, keyless ICI weekly Excel (.xls) files become two static JSON snapshots that
render in prod with NO API key:

  A) Money Market Fund assets ("cash on the sidelines")
     https://www.ici.org/mm_summary_data_<YEAR>.xls   -> data-mmf.json
  B) Estimated Long-Term Mutual Fund net new cash flows
     https://www.ici.org/flows_data_<YEAR>.xls         -> data-mf-flows.json

WHY A FETCHER (not a browser call):
  ici.org serves the .xls without permissive CORS headers, and the sheets need
  header-row hunting + merged-cell column mapping that belongs in Python. We pull
  server-side (GitHub Action), parse, and write a small JSON the dashboard reads.

LAYOUT NOTES (verified live 2026-06-07):
  MMF "Public Report" sheet:
    - Title/units/as-of-date in rows 0-3; section banner row has
      "TOTAL - ALL MONEY MARKET FUNDS" / "INSTITUTIONAL ..." / "RETAIL ...".
    - Three category sub-rows ("TOTAL"/"GOVERNMENT"/"PRIME"...) then a
      "# Classes" / "TNA" row. We want the TNA column under each section's TOTAL,
      plus GOVERNMENT-TOTAL and PRIME TNA under the all-funds section.
    - Values are MILLIONS of USD -> divide by 1000 for billions.
  Flows "Weekly MF Flow Estimates" sheet:
    - Has a MONTHLY block first, then an "Estimated Weekly Net New Cash Flow"
      banner; ONLY rows below that banner are weekly. Columns: Total equity,
      Domestic (Total domestic), Total world, Total bond. Millions -> billions.

PARSING STRATEGY:
  Read with header=None, then *search row labels case-insensitively* to locate
  the real header/data rows instead of hardcoding indices, so a layout nudge
  (extra title row, etc.) doesn't break the build. Defensive throughout: any
  failure returns a neutral/empty dict rather than crashing the page build.

USAGE:
  python3 fetch_money_flows.py          # fetch + write both JSONs + print latest
  from fetch_money_flows import fetch_all
  blocks = fetch_all(write=True)        # {"mmf": {...}, "mf_flows": {...}}

REQUIRES: requests, pandas, xlrd (xlrd 2.x reads .xls). `pip install --user xlrd`.
"""
import os
import io
import json
import datetime as _dt

import requests

try:
    import pandas as pd
except Exception:  # pragma: no cover - pandas is a hard dep in this repo
    pd = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_MMF = os.path.join(HERE, "data-mmf.json")
OUT_FLOWS = os.path.join(HERE, "data-mf-flows.json")

# Browser-like UA: ici.org 403s some default clients.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "application/vnd.ms-excel,*/*"}

MMF_URL = "https://www.ici.org/mm_summary_data_{year}.xls"
FLOWS_URL = "https://www.ici.org/flows_data_{year}.xls"

MAX_WEEKS = 52          # keep ~1y of weekly history
HTTP_TIMEOUT = 60
MAX_BYTES = 8 * 1024 * 1024  # 8 MB cap; these files are ~50-60 KB


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _years_to_try():
    """Current year first, then prior year (handles the Jan rollover before the
    new file is posted)."""
    y = _dt.date.today().year
    return [y, y - 1]


def _download_xls(url_tmpl):
    """Return (bytes, resolved_url) for the first year that returns a real .xls,
    or (None, None) on total failure. Never raises."""
    for year in _years_to_try():
        url = url_tmpl.format(year=year)
        try:
            r = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
        except Exception as exc:  # network blip
            print(f"  [warn] GET {url} failed: {exc}")
            continue
        if r.status_code != 200:
            print(f"  [warn] {url} -> HTTP {r.status_code}")
            continue
        content = r.content or b""
        if not content or len(content) > MAX_BYTES:
            print(f"  [warn] {url} -> {len(content)} bytes (rejected)")
            continue
        # .xls (BIFF) starts with the OLE2 magic D0 CF 11 E0; some old ICI files
        # are pre-OLE BIFF (09 08). Accept either rather than guess by suffix.
        head = content[:8]
        if not (head.startswith(b"\xd0\xcf\x11\xe0") or head[:2] in (b"\x09\x08", b"\x09\x04")):
            # Could still be parseable; let pandas try, but log it.
            print(f"  [warn] {url} unexpected magic {head[:4]!r}; trying anyway")
        print(f"  [ok] {url} -> {len(content)} bytes")
        return content, url
    return None, None


def _read_sheet(content):
    """Read the first sheet of an .xls into a header-less DataFrame. Returns None
    on failure."""
    if pd is None:
        print("  [err] pandas unavailable")
        return None
    try:
        return pd.read_excel(io.BytesIO(content), sheet_name=0,
                             header=None, engine="xlrd")
    except Exception as exc:
        print(f"  [err] read_excel failed: {exc}")
        return None


def _norm(x):
    """Lower-cased, whitespace-collapsed string for label matching."""
    if x is None:
        return ""
    try:
        if pd is not None and pd.isna(x):
            return ""
    except Exception:
        pass
    return " ".join(str(x).strip().lower().split())


def _to_float(x):
    """Coerce a cell to float; return None if not numeric."""
    if x is None:
        return None
    try:
        if pd is not None and pd.isna(x):
            return None
    except Exception:
        pass
    try:
        s = str(x).strip().replace(",", "")
        if s == "" or s in ("-", "--", "n/a", "na"):
            return None
        return float(s)
    except Exception:
        return None


def _parse_date(x):
    """Return an ISO 'YYYY-MM-DD' string from many date encodings, else None."""
    if x is None:
        return None
    try:
        if pd is not None and pd.isna(x):
            return None
    except Exception:
        pass
    # Already a datetime/Timestamp?
    try:
        if hasattr(x, "year") and hasattr(x, "month") and hasattr(x, "day"):
            return _dt.date(x.year, x.month, x.day).isoformat()
    except Exception:
        pass
    s = str(x).strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m-%d-%Y"):
        try:
            return _dt.datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            continue
    # pandas fallback (handles 6/3/2026 etc.)
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if ts is not None and not pd.isna(ts):
            return ts.date().isoformat()
    except Exception:
        pass
    return None


def _b(millions):
    """Millions -> billions, rounded to 2dp, preserving None."""
    if millions is None:
        return None
    return round(millions / 1000.0, 2)


def _is_date_row(row):
    """A data row whose first cell parses as a date."""
    return _parse_date(row.iloc[0]) is not None


# ---------------------------------------------------------------------------
# A) Money Market Fund assets
# ---------------------------------------------------------------------------
def _find_mmf_tna_columns(df):
    """Locate the TNA columns we need by walking the section/category/'TNA' header
    chain. Returns a dict of column indices:
        {"total","institutional","retail","government","prime"}
    Robust to column shifts: it scans every column, forward-fills the section
    banner + category labels, and picks the 'TNA' column whose forward-filled
    section/category matches.

    Section banners (row contains them somewhere): TOTAL - ALL ..., INSTITUTIONAL
    ..., RETAIL .... Category labels: TOTAL / GOVERNMENT / PRIME. Below those a
    row carries '# Classes' / 'TNA' per column.
    """
    nrows, ncols = df.shape
    # 1) find the 'TNA'/'# Classes' header row (the one with the most 'tna' cells)
    tna_row = None
    best = 0
    for r in range(min(nrows, 15)):
        cnt = sum(1 for c in range(ncols) if _norm(df.iat[r, c]) == "tna")
        if cnt > best:
            best, tna_row = cnt, r
    if tna_row is None or best == 0:
        return {}

    # 2) forward-fill section banner row (the row with the three SECTION strings)
    sect_row = None
    for r in range(tna_row):
        joined = " | ".join(_norm(df.iat[r, c]) for c in range(ncols))
        if "money market funds" in joined and ("institutional" in joined or "retail" in joined):
            sect_row = r
            break
    # forward-fill helper across a header row
    def ffill_row(r):
        out, cur = [], ""
        for c in range(ncols):
            v = _norm(df.iat[r, c])
            if v:
                cur = v
            out.append(cur)
        return out

    sect = ffill_row(sect_row) if sect_row is not None else [""] * ncols

    # 3) category row(s): scan the rows between section and tna for TOTAL/GOV/PRIME.
    #    Build a per-column "category" by forward-fill of whichever of these rows
    #    holds the category words.
    cat_row = None
    for r in range((sect_row or 0) + 1, tna_row + 1):
        joined = " ".join(_norm(df.iat[r, c]) for c in range(ncols))
        if "prime" in joined and "government" in joined:
            cat_row = r
            break
    cat = ffill_row(cat_row) if cat_row is not None else [""] * ncols

    def sect_key(s):
        if "institutional" in s:
            return "institutional"
        if "retail" in s:
            return "retail"
        if "total - all" in s or ("total" in s and "all" in s):
            return "total"
        return None

    cols = {}
    for c in range(ncols):
        if _norm(df.iat[tna_row, c]) != "tna":
            continue
        sk = sect_key(sect[c])
        ck = cat[c]
        if sk is None:
            continue
        # all-funds section: capture total / government-total / prime
        if sk == "total":
            if "total" == ck and "total" not in cols:
                cols["total"] = c
            elif "government" in ck and "government" not in cols:
                cols["government"] = c
            elif "prime" in ck and "prime" not in cols:
                cols["prime"] = c
        elif sk in ("institutional", "retail"):
            # we want each section's TOTAL TNA (the first TNA under that section)
            if "total" == ck and sk not in cols:
                cols[sk] = c
    return cols


def fetch_mmf():
    """Download + parse ICI weekly MMF assets. Returns:
        {as_of, source, unit, weekly:[{date,total,retail,institutional,
                                        government,prime}, ...], wow_change}
    weekly is oldest->newest (last ~52 wks); values in USD billions.
    Returns a neutral shell ({...,"weekly":[]}) on any failure."""
    print("[MMF] fetching ICI money-market summary ...")
    shell = {"as_of": _now_iso(), "source": "ICI", "unit": "USD billions",
             "weekly": [], "wow_change": None}
    content, url = _download_xls(MMF_URL)
    if content is None:
        print("  [err] no MMF file downloaded")
        return shell
    df = _read_sheet(content)
    if df is None or df.empty:
        return shell

    cols = _find_mmf_tna_columns(df)
    if "total" not in cols:
        print(f"  [err] could not locate MMF TNA columns (found {cols})")
        return shell
    print(f"  [info] MMF columns: {cols}")

    weekly = []
    for r in range(df.shape[0]):
        row = df.iloc[r]
        d = _parse_date(row.iloc[0])
        if d is None:
            continue
        total = _to_float(row.iloc[cols["total"]])
        if total is None:
            continue  # skip header/blank rows whose col0 happened to parse
        rec = {
            "date": d,
            "total": _b(total),
            "retail": _b(_to_float(row.iloc[cols["retail"]])) if "retail" in cols else None,
            "institutional": _b(_to_float(row.iloc[cols["institutional"]])) if "institutional" in cols else None,
        }
        if "government" in cols:
            rec["government"] = _b(_to_float(row.iloc[cols["government"]]))
        if "prime" in cols:
            rec["prime"] = _b(_to_float(row.iloc[cols["prime"]]))
        weekly.append(rec)

    # sort oldest->newest, keep last MAX_WEEKS
    weekly.sort(key=lambda x: x["date"])
    weekly = weekly[-MAX_WEEKS:]
    shell["weekly"] = weekly

    # as_of = latest data date; wow_change on total
    if weekly:
        shell["as_of"] = weekly[-1]["date"]
        if len(weekly) >= 2 and weekly[-1]["total"] is not None and weekly[-2]["total"] is not None:
            shell["wow_change"] = round(weekly[-1]["total"] - weekly[-2]["total"], 2)
    return shell


# ---------------------------------------------------------------------------
# B) Estimated Long-Term Mutual Fund net new cash flows (WEEKLY section only)
# ---------------------------------------------------------------------------
def _find_flows_columns(df):
    """Map the equity/bond columns by walking the 3-row merged header
    (Equity/Bond banner -> Total equity/Domestic/World/Total bond -> sub-rows).
    Returns {"total_equity","domestic_equity","world_equity","total_bond"}.

    Strategy: forward-fill each of the first ~8 header rows across columns, then
    for each column build a combined 'top mid low' label and match keywords.
    """
    nrows, ncols = df.shape
    hdr_rows = list(range(min(nrows, 8)))

    def ffill_row(r):
        out, cur = [], ""
        for c in range(ncols):
            v = _norm(df.iat[r, c])
            if v:
                cur = v
            out.append(cur)
        return out

    # We deliberately do NOT forward-fill the "top" (Equity/Bond) banner because
    # it would smear Equity across bond columns. Instead read each header cell at
    # its own column and concatenate the *non-filled* labels per column, then use
    # targeted matching on the mid/low labels which are unique enough.
    cols = {}
    for c in range(ncols):
        labels = " ".join(_norm(df.iat[r, c]) for r in hdr_rows).strip()
        if not labels:
            continue
        # exact-ish matches; order matters (check specific before generic)
        if "total equity" in labels and "total_equity" not in cols:
            cols["total_equity"] = c
        elif "total domestic" in labels and "domestic_equity" not in cols:
            cols["domestic_equity"] = c
        elif "total world" in labels and "world_equity" not in cols:
            cols["world_equity"] = c
        elif "total bond" in labels and "total_bond" not in cols:
            cols["total_bond"] = c
    return cols


def _flows_weekly_start_row(df):
    """Index of the first data row of the WEEKLY section. ICI puts a MONTHLY block
    first, then an 'Estimated Weekly Net New Cash Flow' banner; weekly data starts
    right after it. If the banner isn't found, fall back to the first date row
    (degrades to parsing whatever dates exist)."""
    nrows = df.shape[0]
    for r in range(nrows):
        joined = " ".join(_norm(df.iat[r, c]) for c in range(df.shape[1]))
        if "weekly" in joined and ("net new cash flow" in joined or "cash flow" in joined):
            return r + 1
    # fallback
    for r in range(nrows):
        if _parse_date(df.iat[r, 0]) is not None:
            return r
    return nrows


def fetch_mf_flows():
    """Download + parse ICI Estimated WEEKLY Long-Term MF net new cash flows.
    Returns:
        {as_of, source, unit, weekly:[{date,total_equity,domestic_equity,
                                       world_equity,total_bond}, ...]}
    oldest->newest, USD billions. Neutral shell on failure."""
    print("[FLOWS] fetching ICI long-term MF flows ...")
    shell = {"as_of": _now_iso(), "source": "ICI", "unit": "USD billions",
             "weekly": []}
    content, url = _download_xls(FLOWS_URL)
    if content is None:
        print("  [err] no flows file downloaded")
        return shell
    df = _read_sheet(content)
    if df is None or df.empty:
        return shell

    cols = _find_flows_columns(df)
    if "total_equity" not in cols:
        print(f"  [err] could not locate flows columns (found {cols})")
        return shell
    print(f"  [info] flows columns: {cols}")

    start = _flows_weekly_start_row(df)
    print(f"  [info] weekly section starts at row {start}")

    weekly = []
    for r in range(start, df.shape[0]):
        row = df.iloc[r]
        d = _parse_date(row.iloc[0])
        if d is None:
            continue
        te = _to_float(row.iloc[cols["total_equity"]])
        # a real weekly row needs at least a total-equity number
        if te is None:
            continue
        rec = {
            "date": d,
            "total_equity": _b(te),
            "domestic_equity": _b(_to_float(row.iloc[cols["domestic_equity"]])) if "domestic_equity" in cols else None,
            "world_equity": _b(_to_float(row.iloc[cols["world_equity"]])) if "world_equity" in cols else None,
            "total_bond": _b(_to_float(row.iloc[cols["total_bond"]])) if "total_bond" in cols else None,
        }
        weekly.append(rec)

    weekly.sort(key=lambda x: x["date"])
    weekly = weekly[-MAX_WEEKS:]
    shell["weekly"] = weekly
    if weekly:
        shell["as_of"] = weekly[-1]["date"]
    return shell


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _write_json(path, obj):
    try:
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
        print(f"  [write] {path} ({os.path.getsize(path)} bytes)")
    except Exception as exc:
        print(f"  [err] could not write {path}: {exc}")


def fetch_all(write=True):
    """Fetch both ICI datasets. If write=True, persist data-mmf.json and
    data-mf-flows.json. Returns {"mmf": <dict>, "mf_flows": <dict>}."""
    mmf = fetch_mmf()
    flows = fetch_mf_flows()
    if write:
        _write_json(OUT_MMF, mmf)
        _write_json(OUT_FLOWS, flows)
    return {"mmf": mmf, "mf_flows": flows}


def _print_summary(blocks):
    mmf = blocks.get("mmf", {})
    flows = blocks.get("mf_flows", {})
    print("\n" + "=" * 64)
    print("MMF (data-mmf.json)  unit:", mmf.get("unit"))
    w = mmf.get("weekly") or []
    print(f"  weeks parsed: {len(w)}  as_of: {mmf.get('as_of')}  wow_change: {mmf.get('wow_change')}")
    if w:
        last = w[-1]
        print(f"  latest {last['date']}: total={last['total']}  "
              f"retail={last.get('retail')}  institutional={last.get('institutional')}  "
              f"govt={last.get('government')}  prime={last.get('prime')}  (USD billions)")
    print("-" * 64)
    print("MF FLOWS (data-mf-flows.json)  unit:", flows.get("unit"))
    f = flows.get("weekly") or []
    print(f"  weeks parsed: {len(f)}  as_of: {flows.get('as_of')}")
    if f:
        last = f[-1]
        print(f"  latest {last['date']}: total_equity={last['total_equity']}  "
              f"domestic={last.get('domestic_equity')}  world={last.get('world_equity')}  "
              f"total_bond={last.get('total_bond')}  (USD billions)")
    print("=" * 64)


if __name__ == "__main__":
    blocks = fetch_all(write=True)
    _print_summary(blocks)
