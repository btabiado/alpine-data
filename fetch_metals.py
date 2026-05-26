"""
Gold & silver metals fetcher for the V2 dashboard's Metals tab.

Sources (all free, no auth required):
  FRED            GOLDPMGBD228NLBM    London Gold PM Fix (USD/oz, daily)
                  -- requires FRED_API_KEY; falls back to Yahoo on miss.
  Yahoo Finance   GC=F                Gold futures continuous front-month
  Yahoo Finance   SI=F                Silver futures continuous front-month
  IMF SDMX        IRFCL dataflow      Central bank gold holdings, monthly,
                  IRFCLDT1_IRFCL56_FTO indicator = fine troy ounces.
                  https://api.imf.org/external/sdmx/2.1/data/IMF.STA,IRFCL/...
  USGS / ScienceBase  MCS Silver Data Release (DOI 10.5066/P13XCP3R)
                  Annual world silver mine production by country (metric tons).
  USGS / ScienceBase  MCS Gold Data Release (item 65b7d7b2d34e36a39045b4c8)
                  Annual world gold mine production by country (metric tons).

Output: v2/data-metals.json (sidecar consumed by the V2 dashboard's
Metals tab via the existing SIDECARS lazy-load mechanism).

Schema:
    {
      "generated_at": "2026-05-25T17:30:00Z",
      "gold_price":   {"unit": "USD/oz", "observations": [{"date":..., "value":...}, ...],
                        "source": "FRED:GOLDPMGBD228NLBM | Yahoo:GC=F"},
      "silver_price": {"unit": "USD/oz", "observations": [...],
                        "source": "Yahoo:SI=F"},
      "central_bank_gold": {"unit": "tonnes", "as_of": "YYYY-MM-DD",
                            "holdings": [{"country":..., "tonnes":...}, ...top 20],
                            "source": "IMF IRFCL (IRFCLDT1_IRFCL56_FTO)"},
      "silver_mine_production": {"unit": "metric tons", "year": YYYY,
                                  "by_country": [{"country":..., "tonnes":...}, ...],
                                  "source": "USGS MCS <year>"},
      "gold_mine_production":   {"unit": "metric tons", "year": YYYY,
                                  "by_country": [{"country":..., "tonnes":...}, ...],
                                  "source": "USGS MCS <year>"}
    }

Resilience: each source is wrapped in its own try/except so one failure
doesn't kill the others. On any per-source failure the prior good value
for that key is preserved from the existing output JSON (never blanked,
never zeroed). The file is only written once at the end, so the dashboard
either sees a fully-fresh payload or the last-known-good payload mixed
with whatever sources DID succeed this run.

CLI:
    python fetch_metals.py                 # default --out v2/data-metals.json
    python fetch_metals.py --out PATH      # custom output path
    python fetch_metals.py --no-network    # offline parser self-test only
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0; +metals-fetcher)"
H = {"User-Agent": UA}
ROOT = Path(__file__).parent
DEFAULT_OUT = ROOT / "v2" / "data-metals.json"
# V1 dual-write target — the V1 dashboard lazy-loads /data-metals.json via
# the same SIDECARS mechanism it already uses for whale/defi (those live at
# data-whale.json / data-defi.json next to dashboard.html). Writing here
# keeps V2's existing wiring untouched while giving V1 a self-contained
# sidecar that the CI stage step picks up automatically (it globs
# `data-*.json` at repo root). Pass --out-v1 '' to disable.
DEFAULT_OUT_V1 = ROOT / "data-metals.json"

# IMF SDMX namespaces (SDMX-ML StructureSpecificData v2.1)
NS_MSG = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"


# ----- helpers ---------------------------------------------------------------

def _get_json(url: str, params: dict | None = None, timeout: int = 30) -> Any:
    try:
        r = requests.get(url, params=params, headers=H, timeout=timeout)
        if r.status_code != 200:
            print(f"  [metals] {url} -> HTTP {r.status_code}", file=sys.stderr)
            return None
        return r.json()
    except Exception as e:
        print(f"  [metals] {url} -> {e}", file=sys.stderr)
        return None


def _get_text(url: str, params: dict | None = None, timeout: int = 60,
              accept: str | None = None) -> str | None:
    h = dict(H)
    if accept:
        h["Accept"] = accept
    try:
        r = requests.get(url, params=params, headers=h, timeout=timeout)
        if r.status_code != 200:
            print(f"  [metals] {url} -> HTTP {r.status_code}", file=sys.stderr)
            return None
        # IMF returns BOMless UTF-8 already; sciencebase CSVs have a BOM.
        return r.text
    except Exception as e:
        print(f"  [metals] {url} -> {e}", file=sys.stderr)
        return None


# ----- gold price (FRED + Yahoo fallback) -----------------------------------

def fetch_gold_price() -> dict | None:
    """Daily gold price, last ~5y. FRED London PM Fix first, Yahoo GC=F fallback.

    Returns None on total failure so the caller preserves the prior payload.
    """
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    obs: list[dict] = []
    source = ""

    # Primary: FRED London Gold PM Fix (the "official" series the existing
    # macro overlay uses).
    if api_key:
        start = (datetime.now(timezone.utc).date() - timedelta(days=365 * 5)).isoformat()
        j = _get_json(
            "https://api.stlouisfed.org/fred/series/observations",
            {
                "series_id": "GOLDPMGBD228NLBM",
                "api_key": api_key,
                "file_type": "json",
                "observation_start": start,
            },
        )
        if isinstance(j, dict):
            for o in j.get("observations") or []:
                d, raw = o.get("date"), o.get("value")
                if not d or raw in (None, ".", ""):
                    continue
                try:
                    obs.append({"date": d, "value": float(raw)})
                except (TypeError, ValueError):
                    continue
        if obs:
            source = "FRED:GOLDPMGBD228NLBM"

    # Fallback: Yahoo gold futures (no key required; ~5y of daily closes).
    if not obs:
        yobs = _yahoo_daily("GC=F", range_="5y")
        if yobs:
            obs = yobs
            source = "Yahoo:GC=F"
        elif source:
            source += " | Yahoo:GC=F (fallback unused)"

    if not obs:
        return None
    return {"unit": "USD/oz", "observations": obs, "source": source}


def _yahoo_daily(symbol: str, range_: str = "5y") -> list[dict]:
    """Daily closes from Yahoo's public chart API. Empty list on any failure."""
    j = _get_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        {"range": range_, "interval": "1d"},
    )
    if not isinstance(j, dict):
        return []
    try:
        result = (j.get("chart") or {}).get("result", [])[0]
        ts = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    except (IndexError, AttributeError, TypeError):
        return []
    out: list[dict] = []
    for t, c in zip(ts, closes):
        if t is None or c is None:
            continue
        out.append({
            "date": datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%d"),
            "value": float(c),
        })
    return out


def fetch_silver_price() -> dict | None:
    """Daily silver futures closes (USD/oz), ~5y. Yahoo SI=F only — FRED has
    no daily LBMA silver fix series, futures are the realistic public source."""
    obs = _yahoo_daily("SI=F", range_="5y")
    if not obs:
        return None
    return {"unit": "USD/oz", "observations": obs, "source": "Yahoo:SI=F"}


# ----- central-bank gold (IMF IRFCL SDMX) -----------------------------------

# Country code -> human-readable name mapping. IMF uses ISO-3166 alpha-3 with
# a few legacy codes (e.g. XKX for Kosovo, EUR for Euro Area aggregate, U2 for
# Eurosystem). We don't pull the IMF codelist on every run — it's 500KB and
# only ~80 countries actually report gold. Override common ones inline; let
# unknowns fall through with the raw code as a label so nothing breaks.
_IMF_COUNTRY_NAME = {
    "USA": "United States", "DEU": "Germany", "ITA": "Italy", "FRA": "France",
    "RUS": "Russia", "CHN": "China", "CHE": "Switzerland", "JPN": "Japan",
    "IND": "India", "NLD": "Netherlands", "TUR": "Turkey", "TWN": "Taiwan",
    "POL": "Poland", "PRT": "Portugal", "UZB": "Uzbekistan", "KAZ": "Kazakhstan",
    "GBR": "United Kingdom", "SAU": "Saudi Arabia", "ESP": "Spain", "AUT": "Austria",
    "THA": "Thailand", "BEL": "Belgium", "SGP": "Singapore", "SWE": "Sweden",
    "ZAF": "South Africa", "MEX": "Mexico", "LBY": "Libya", "PHL": "Philippines",
    "ROU": "Romania", "DNK": "Denmark", "PAK": "Pakistan", "ARG": "Argentina",
    "DZA": "Algeria", "IDN": "Indonesia", "FIN": "Finland", "BGR": "Bulgaria",
    "MYS": "Malaysia", "PER": "Peru", "BRA": "Brazil", "EUR": "ECB (Euro Area)",
    "U2": "Eurosystem", "AUS": "Australia", "GRC": "Greece", "CZE": "Czech Republic",
    "HUN": "Hungary", "EGY": "Egypt", "KOR": "Korea, Rep.", "SVK": "Slovak Republic",
    "QAT": "Qatar", "JOR": "Jordan", "IRL": "Ireland", "LBN": "Lebanon",
    "ISL": "Iceland", "BOL": "Bolivia", "LTU": "Lithuania", "LVA": "Latvia",
    "MNG": "Mongolia", "TJK": "Tajikistan", "SRB": "Serbia", "UKR": "Ukraine",
    "KGZ": "Kyrgyz Republic", "BLR": "Belarus", "GHA": "Ghana", "MMR": "Myanmar",
    "BIH": "Bosnia and Herzegovina", "MKD": "North Macedonia", "ALB": "Albania",
    "ARE": "United Arab Emirates", "ISR": "Israel", "NOR": "Norway", "COL": "Colombia",
    "CHL": "Chile", "VEN": "Venezuela", "NZL": "New Zealand", "CAN": "Canada",
    "MUS": "Mauritius", "MAR": "Morocco", "OMN": "Oman", "BHR": "Bahrain",
    "KWT": "Kuwait", "IRQ": "Iraq", "URY": "Uruguay", "MDA": "Moldova",
    "AZE": "Azerbaijan", "ARM": "Armenia", "GEO": "Georgia", "EST": "Estonia",
    "MLT": "Malta", "CYP": "Cyprus", "LUX": "Luxembourg", "SVN": "Slovenia",
}

# Aggregates / non-country entities the IMF returns alongside countries.
# Excluded from the "Top 20" rankings — they double-count member states.
_IMF_AGGREGATES = {"EUR", "U2", "EMU"}


def fetch_central_bank_gold() -> dict | None:
    """Latest reported monthly gold-reserve holdings by country, top 20.

    Pulls IMF SDMX IRFCL indicator ``IRFCLDT1_IRFCL56_FTO`` (Official reserve
    assets, gold — fine troy ounces) for all reporting countries, picks the
    most recent observation per country, converts to metric tonnes
    (1 troy oz = 31.1034768 g; the IMF stores values without scaling, e.g.
    USA = 261,499,000 troy oz = 8,133.5 t), filters out IMF aggregates, and
    returns the top 20.
    """
    # COUNTRY=.IRFCLDT1_IRFCL56_FTO.SECTOR=.FREQ=M — leave country/sector wild
    url = ("https://api.imf.org/external/sdmx/2.1/data/IMF.STA,IRFCL/"
           ".IRFCLDT1_IRFCL56_FTO..M")
    # Last ~24 months: enough that even slow-updating reporters land in window.
    start = (datetime.now(timezone.utc).date() - timedelta(days=400)).isoformat()[:7]
    # IMF's gateway rejects the spec-correct vnd.sdmx Accept header with HTTP
    # 500. The default text/* negotiation returns SDMX-ML StructureSpecificData
    # without it, which is what we parse below.
    xml = _get_text(url, {"startPeriod": start}, timeout=90)
    if not xml:
        return None

    try:
        latest_per_country = _parse_imf_irfcl(xml)
    except Exception as e:
        print(f"  [metals/imf] parse failed: {e}", file=sys.stderr)
        return None

    if not latest_per_country:
        return None

    # troy oz -> metric tonnes
    TROY_OZ_TO_T = 31.1034768 / 1_000_000.0
    # Sanity cap: the U.S. (largest official holder by far) is ~8,134 t.
    # The IMF series occasionally surfaces clearly-wrong outliers (e.g.,
    # Angola at 20,898 t which is ~4,000x its real ~5 t holding). Drop
    # anything above this cap rather than pollute the top-20 ranking.
    SANITY_CAP_TONNES = 10_000.0
    rows: list[dict] = []
    for country_code, (period, value) in latest_per_country.items():
        if country_code in _IMF_AGGREGATES:
            continue
        tonnes = value * TROY_OZ_TO_T
        if tonnes > SANITY_CAP_TONNES:
            print(f"  [metals/imf] dropping implausible {country_code} = "
                  f"{tonnes:.0f}t (>{SANITY_CAP_TONNES:.0f}t cap)",
                  file=sys.stderr)
            continue
        rows.append({
            "country": _IMF_COUNTRY_NAME.get(country_code, country_code),
            "tonnes": round(tonnes, 2),
            "code": country_code,
            "as_of": period,
        })

    rows.sort(key=lambda r: r["tonnes"], reverse=True)
    top = rows[:20]
    # Use the most recent period across the top-20 as the headline "as of"
    # (per-country dates are already ISO-normalized by _parse_imf_irfcl).
    as_of = max((r["as_of"] for r in top), default="")
    # Drop per-row as_of/code to keep payload small; per-country dates only
    # matter if we surface "stale country X" in the UI, which the MVP doesn't.
    for r in top:
        r.pop("as_of", None)
        r.pop("code", None)

    return {
        "unit": "tonnes",
        "as_of": as_of,
        "holdings": top,
        "source": "IMF IRFCL (IRFCLDT1_IRFCL56_FTO)",
    }


def _parse_imf_irfcl(xml: str) -> dict[str, tuple[str, float]]:
    """Parse IMF SDMX-ML StructureSpecificData into {country: (period, value)}.

    Only keeps the most recent observation per country.
    """
    # SDMX-ML namespaces vary per dataflow (the ns1 dataflow URN suffix
    # includes the version), and the public IMF gateway actually returns
    # *bare* <Series>/<Obs> tags (no ns prefix at all). Easiest: regex
    # accepting an optional "<prefix>:" so both flavors parse.
    out: dict[str, tuple[str, float]] = {}
    series_re = re.compile(
        r'<(?:[A-Za-z0-9]+:)?Series\b([^>]*)>(.*?)</(?:[A-Za-z0-9]+:)?Series>',
        re.S)
    obs_re = re.compile(
        r'<(?:[A-Za-z0-9]+:)?Obs\b[^>]*TIME_PERIOD="([^"]+)"[^>]*OBS_VALUE="([^"]+)"',
        re.S)

    for m in series_re.finditer(xml):
        attrs, body = m.group(1), m.group(2)
        c_match = re.search(r'COUNTRY="([^"]+)"', attrs)
        if not c_match:
            continue
        country = c_match.group(1)
        latest_period, latest_val = "", None
        for obs in obs_re.finditer(body):
            period, raw = obs.group(1), obs.group(2)
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            # Normalize so "2026-M04" / "2026-04" / "2026-04-01" all sort by
            # actual calendar order — naive lex would put "2026-M9" after
            # "2026-M10". _month_to_iso is the canonical form.
            norm = _month_to_iso(period)
            if norm > latest_period:
                latest_period, latest_val = norm, v
        if latest_val is not None and latest_val > 0:
            out[country] = (latest_period, latest_val)
    return out


def _month_to_iso(period: str) -> str:
    """Normalize an IMF time-period string to ISO YYYY-MM-DD.

    The IMF SDMX gateway returns monthly periods as ``YYYY-MM`` *or*
    ``YYYY-Mmm`` (e.g. ``2026-M04``). Both map to first-of-month here so
    the front-end can display a stable "as of" date. Pass through other
    formats unchanged."""
    period = period or ""
    m = re.fullmatch(r"(\d{4})-M(\d{1,2})", period)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    if re.fullmatch(r"\d{4}-\d{2}", period):
        return period + "-01"
    return period


# ----- silver mine production (USGS MCS) ------------------------------------

# ScienceBase item IDs for the world silver data release per MCS edition.
# The MCS 2025 release dropped the world-by-country CSV; until USGS reposts
# it, we fall through to MCS 2024 (which has 2022 + 2023-estimate columns).
_USGS_SILVER_RELEASES = [
    # (sciencebase_item_id, file_label_match, year_columns_in_priority_order)
    # 2024 release: per the meta, Prod_t_est_2023 is the latest estimate.
    ("65b7d88dd34e36a39045b51d", "world", ["Prod_t_est_2023", "Prod_t_2022"]),
]

# ScienceBase item IDs for the world gold data release per MCS edition.
# MCS 2025 (item 6797fdc7d34ea8c18376e1a0) ships only salient + meta —
# no world-by-country CSV — so the only usable release for production
# data is currently MCS 2024 (item 65b7d7b2d34e36a39045b4c8), which has
# Prod_t_2022 + Prod_t_est_2023 columns. Listed in priority order so that
# whenever USGS reposts the world CSV in a later MCS we just prepend it.
_USGS_GOLD_RELEASES = [
    # (sciencebase_item_id, file_label_match, year_columns_in_priority_order)
    ("65b7d7b2d34e36a39045b4c8", "world", ["Prod_t_est_2023", "Prod_t_2022"]),
]


def fetch_silver_mine_production() -> dict | None:
    """World silver mine production by country (metric tons), latest annual.

    USGS Mineral Commodity Summaries — Silver chapter, world production
    table, published annually. Public domain.
    """
    return _fetch_usgs_mcs_production(_USGS_SILVER_RELEASES, metal_label="Silver")


def _fetch_usgs_gold_production() -> dict | None:
    """World gold mine production by country (metric tons), latest annual.

    USGS Mineral Commodity Summaries — Gold chapter, world production
    table, published annually. Public domain. Mirrors the silver fetcher's
    structure; the underlying CSV schema is identical.
    """
    return _fetch_usgs_mcs_production(_USGS_GOLD_RELEASES, metal_label="Gold")


def _fetch_usgs_mcs_production(releases: list[tuple[str, str, list[str]]],
                               metal_label: str = "") -> dict | None:
    """Shared ScienceBase walker for USGS MCS world-production CSVs.

    Iterates `releases` in priority order, fetches the catalog metadata,
    finds the world CSV by filename fragment, and feeds it to the shared
    MCS parser. Returns the first successful result, or None.
    """
    for item_id, file_label, year_cols in releases:
        meta = _get_json(
            f"https://www.sciencebase.gov/catalog/item/{item_id}",
            {"format": "json"}, timeout=30,
        )
        if not isinstance(meta, dict):
            continue
        # Find the world CSV by name fragment match (filename varies per year).
        url = None
        for f in meta.get("files") or []:
            name = (f.get("name") or "").lower()
            if name.endswith(".csv") and file_label in name:
                url = f.get("url")
                break
        if not url:
            continue
        csv_text = _get_text(url, timeout=30)
        if not csv_text:
            continue
        # Silver has a back-compat wrapper that re-stamps the source string
        # with the DOI suffix; gold uses the plain shared parser.
        if metal_label == "Silver":
            result = _parse_usgs_silver_csv(csv_text, year_cols)
        else:
            result = _parse_usgs_mcs_csv(csv_text, year_cols,
                                         metal_label=metal_label)
        if result:
            return result
    return None


def _parse_usgs_silver_csv(csv_text: str, year_cols: list[str]
                           ) -> dict | None:
    """Silver convenience wrapper around the shared USGS MCS CSV parser.

    Preserves the original ``USGS MCS Silver <year> (DOI 10.5066/P13XCP3R)``
    source label for back-compat with anything that grepped the prior string.
    """
    res = _parse_usgs_mcs_csv(csv_text, year_cols, metal_label="Silver")
    if res:
        res["source"] = (
            f"USGS MCS Silver {res.get('year', '')} (DOI 10.5066/P13XCP3R)"
        )
    return res


def _parse_usgs_mcs_csv(csv_text: str, year_cols: list[str],
                        metal_label: str = "") -> dict | None:
    """Pick the freshest year column with data, return top-by-tonnes rows.

    CSV has columns: Source, Country, Type, Prod_t_2022, Prod_t_est_2023, ...
    Skip 'World total (rounded)' and 'Other countries' aggregates. The
    USGS MCS world-production CSVs for gold and silver share the same
    schema, so a single parser handles both.

    `metal_label` is only used to compose the human-readable `source` string.
    """
    # Strip UTF-8 BOM if present.
    if csv_text.startswith("﻿"):
        csv_text = csv_text[1:]
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    if not rows:
        return None

    # Pick the first year column that has real data in it.
    chosen_col, chosen_year = "", 0
    for col in year_cols:
        if col not in (reader.fieldnames or []):
            continue
        if any(_safe_float(r.get(col)) is not None for r in rows):
            chosen_col = col
            m = re.search(r"(\d{4})", col)
            chosen_year = int(m.group(1)) if m else 0
            break
    if not chosen_col:
        return None

    by_country: list[dict] = []
    for r in rows:
        country = (r.get("Country") or "").strip()
        if not country:
            continue
        # Skip aggregates — they roll up by-country totals. USGS MCS gold
        # additionally lumps non-disclosed producers into "Other countries",
        # which we also exclude (matches the silver fetcher's behaviour).
        if "world total" in country.lower() or country.lower().startswith("other"):
            continue
        val = _safe_float(r.get(chosen_col))
        if val is None or val <= 0:
            continue
        by_country.append({"country": country, "tonnes": val})

    by_country.sort(key=lambda x: x["tonnes"], reverse=True)
    if not by_country:
        return None
    label = (metal_label + " ") if metal_label else ""
    return {
        "unit": "metric tons",
        "year": chosen_year,
        "by_country": by_country,
        "source": f"USGS MCS {label}{chosen_year}".strip(),
    }


def _safe_float(x: Any) -> float | None:
    if x is None or x == "" or x == "NA":
        return None
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None


# ----- top-level orchestration ----------------------------------------------

def build_payload(prior: dict | None = None) -> dict:
    """Run all 4 fetchers, preserve prior values per-key on failure."""
    prior = prior or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    out: dict[str, Any] = {"generated_at": now}

    for key, fn, label in [
        ("gold_price",             fetch_gold_price,              "gold price"),
        ("silver_price",           fetch_silver_price,            "silver price"),
        ("central_bank_gold",      fetch_central_bank_gold,       "central-bank gold"),
        ("silver_mine_production", fetch_silver_mine_production,  "silver mine production"),
        ("gold_mine_production",   _fetch_usgs_gold_production,   "gold mine production"),
    ]:
        try:
            v = fn()
        except Exception as e:
            print(f"  [metals/{key}] fatal: {e}", file=sys.stderr)
            v = None
        if v is None:
            prev = prior.get(key)
            if prev is not None:
                print(f"  [metals/{key}] {label} fetch returned nothing; "
                      f"preserving prior value", file=sys.stderr)
                out[key] = prev
            else:
                print(f"  [metals/{key}] {label} fetch returned nothing and "
                      f"no prior value; omitting", file=sys.stderr)
        else:
            out[key] = v

    return out


# ----- self-test ------------------------------------------------------------

_SAMPLE_USGS_CSV = (
    '﻿Source,Country,Type,Prod_t_2022,Prod_t_est_2023,Prod_notes,'
    'Reserves_t,Reserves_notes\n'
    'MCS2024,Mexico,"mine production, silver content",6195,6400,,37000,\n'
    'MCS2024,China,"mine production, silver content",3480,3400,,72000,\n'
    'MCS2024,Other countries,"mine production, silver content",2940,3000,,57000,\n'
    'MCS2024,World total (rounded),"mine production, silver content",'
    '25600,26000,,610000,\n'
)

# Real-shape gold sample (USGS lumps non-disclosed producers into
# "Other countries", same as silver — that row must be filtered out).
_SAMPLE_USGS_GOLD_CSV = (
    '﻿Source,Country,Type,Prod_t_2022,Prod_t_est_2023,Prod_notes,'
    'Reserves_t,Reserves_notes\n'
    'MCS2024,China,"Gold - contained content, mine production, metric tons",'
    '372,370,,3000,\n'
    'MCS2024,Australia,"Gold - contained content, mine production, metric tons",'
    '314,310,,12000,\n'
    'MCS2024,Russia,"Gold - contained content, mine production, metric tons",'
    '310,310,,11100,\n'
    'MCS2024,Other countries,"Gold - contained content, mine production, metric tons",'
    '726,700,,9200,\n'
    'MCS2024,World total (rounded),"Gold - contained content, mine production, metric tons",'
    '3060,3000,,59000,\n'
)


_SAMPLE_IMF_XML = '''<?xml version="1.0"?>
<message:StructureSpecificData xmlns:message="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message" xmlns:ns1="urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=IMF.STA:IRFCL(11.0.0):ObsLevelDim:TIME_PERIOD">
  <message:DataSet>
    <ns1:Series COUNTRY="USA" INDICATOR="IRFCLDT1_IRFCL56_FTO" FREQUENCY="M">
      <ns1:Obs TIME_PERIOD="2025-01" OBS_VALUE="261499000"/>
      <ns1:Obs TIME_PERIOD="2025-03" OBS_VALUE="261499000"/>
    </ns1:Series>
    <ns1:Series COUNTRY="DEU" INDICATOR="IRFCLDT1_IRFCL56_FTO" FREQUENCY="M">
      <ns1:Obs TIME_PERIOD="2025-03" OBS_VALUE="108300000"/>
    </ns1:Series>
    <ns1:Series COUNTRY="EUR" INDICATOR="IRFCLDT1_IRFCL56_FTO" FREQUENCY="M">
      <ns1:Obs TIME_PERIOD="2025-03" OBS_VALUE="16000000"/>
    </ns1:Series>
  </message:DataSet>
</message:StructureSpecificData>'''


def _self_test() -> int:
    failed: list[str] = []

    # USGS CSV parser (silver wrapper)
    res = _parse_usgs_silver_csv(_SAMPLE_USGS_CSV, ["Prod_t_est_2023", "Prod_t_2022"])
    if not res:
        failed.append("USGS parser returned None on sample CSV")
    else:
        if res.get("year") != 2023:
            failed.append(f"expected year=2023, got {res.get('year')}")
        countries = [r["country"] for r in res["by_country"]]
        if "Other countries" in countries or any("World total" in c for c in countries):
            failed.append("aggregate row leaked into by_country")
        if countries and countries[0] != "Mexico":
            failed.append(f"expected Mexico first, got {countries[0]}")
        if "Silver" not in (res.get("source") or "") or "DOI" not in (res.get("source") or ""):
            failed.append(f"silver source label lost DOI suffix: {res.get('source')}")

    # USGS CSV parser (gold path — same schema, shared parser)
    gres = _parse_usgs_mcs_csv(_SAMPLE_USGS_GOLD_CSV,
                               ["Prod_t_est_2023", "Prod_t_2022"],
                               metal_label="Gold")
    if not gres:
        failed.append("USGS gold parser returned None on sample CSV")
    else:
        if gres.get("year") != 2023:
            failed.append(f"gold: expected year=2023, got {gres.get('year')}")
        if gres.get("unit") != "metric tons":
            failed.append(f"gold: expected unit='metric tons', got {gres.get('unit')}")
        gcountries = [r["country"] for r in gres["by_country"]]
        if "Other countries" in gcountries or any("World total" in c for c in gcountries):
            failed.append("gold: aggregate row leaked into by_country")
        if gcountries and gcountries[0] != "China":
            failed.append(f"gold: expected China first, got {gcountries[0]}")
        # Shape: each row has the two expected keys, tonnes is a number.
        for r in gres["by_country"]:
            if set(r.keys()) != {"country", "tonnes"}:
                failed.append(f"gold: row has unexpected keys: {r.keys()}")
                break
            if not isinstance(r["tonnes"], (int, float)):
                failed.append(f"gold: tonnes not numeric: {r}")
                break
        if "Gold" not in (gres.get("source") or ""):
            failed.append(f"gold source label missing 'Gold': {gres.get('source')}")

    # IMF parser (periods are normalized to ISO YYYY-MM-DD on parse)
    parsed = _parse_imf_irfcl(_SAMPLE_IMF_XML)
    if parsed.get("USA", ("", 0))[0] != "2025-03-01":
        failed.append(f"IMF parser USA latest period wrong: {parsed.get('USA')}")
    if parsed.get("DEU", ("", 0))[1] != 108300000.0:
        failed.append(f"IMF parser DEU value wrong: {parsed.get('DEU')}")
    if "EUR" not in parsed:
        failed.append("IMF parser dropped EUR (it's filtered at ranking, not parsing)")

    # Parser must handle the bare-tag flavor IMF actually returns (no ns prefix
    # on Series/Obs). Regression: original parser required <ns1:Series>.
    bare = '''<msg><DataSet>
      <Series COUNTRY="USA" INDICATOR="IRFCLDT1_IRFCL56_FTO">
        <Obs TIME_PERIOD="2026-M04" OBS_VALUE="261499000"/>
      </Series></DataSet></msg>'''
    p2 = _parse_imf_irfcl(bare)
    if p2.get("USA", ("", 0))[0] != "2026-04-01":
        failed.append(f"bare-tag IMF parser USA period wrong: {p2.get('USA')}")

    # _month_to_iso must order M04 / M10 correctly via normalization
    if _month_to_iso("2026-M04") != "2026-04-01":
        failed.append(f"_month_to_iso quirk on M format: {_month_to_iso('2026-M04')}")
    if not (_month_to_iso("2026-M9") < _month_to_iso("2026-M10")):
        failed.append("_month_to_iso must give calendar order for M9/M10")

    # Aggregation: build_payload-style top-N from sample IMF data
    TROY = 31.1034768 / 1_000_000.0
    expected_usa = round(261499000 * TROY, 2)
    if abs(expected_usa - 8133.46) > 0.5:
        failed.append(f"USA tonnage conversion sanity check failed: {expected_usa}")

    # Month -> ISO helper
    if _month_to_iso("2025-03") != "2025-03-01":
        failed.append(f"_month_to_iso quirk: {_month_to_iso('2025-03')}")
    if _month_to_iso("2025-03-15") != "2025-03-15":
        failed.append("_month_to_iso should pass through full dates")

    # safe_float helper
    if _safe_float("NA") is not None or _safe_float("") is not None:
        failed.append("_safe_float should reject NA / empty")
    if _safe_float("1,234") != 1234.0:
        failed.append("_safe_float should strip commas")

    if failed:
        for f in failed:
            print(f"  [self-test FAIL] {f}", file=sys.stderr)
        return 1
    print("  [self-test OK] all parser assertions passed.")
    return 0


# ----- CLI ------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch gold/silver metals data.")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"Output JSON path (default: {DEFAULT_OUT})")
    ap.add_argument("--out-v1", default=str(DEFAULT_OUT_V1),
                    help=f"V1 dashboard dual-write path (default: {DEFAULT_OUT_V1}; "
                         f"pass empty string '' to disable)")
    ap.add_argument("--no-network", action="store_true",
                    help="Run offline parser self-test and exit (no HTTP).")
    args = ap.parse_args(argv)

    if args.no_network:
        return _self_test()

    out_path = Path(args.out)
    prior: dict | None = None
    if out_path.exists():
        try:
            prior = json.loads(out_path.read_text())
        except Exception as e:
            print(f"  [metals] could not read prior {out_path}: {e}", file=sys.stderr)

    payload = build_payload(prior=prior)

    # Sanity gate: refuse to overwrite the seed if EVERY source failed AND we
    # have no prior file at all. Returning non-zero lets the caller log it.
    have_any = any(k in payload for k in (
        "gold_price", "silver_price", "central_bank_gold",
        "silver_mine_production", "gold_mine_production"))
    if not have_any:
        print(f"  [metals] every source failed and no prior to fall back on", file=sys.stderr)
        return 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2))
    sizes = {
        "gold_price":             len((payload.get("gold_price") or {}).get("observations") or []),
        "silver_price":           len((payload.get("silver_price") or {}).get("observations") or []),
        "central_bank_gold":      len((payload.get("central_bank_gold") or {}).get("holdings") or []),
        "silver_mine_production": len((payload.get("silver_mine_production") or {}).get("by_country") or []),
        "gold_mine_production":   len((payload.get("gold_mine_production") or {}).get("by_country") or []),
    }
    print(f"  Wrote {out_path} (gold:{sizes['gold_price']}d "
          f"silver:{sizes['silver_price']}d CB:{sizes['central_bank_gold']} "
          f"Ag_prod:{sizes['silver_mine_production']} "
          f"Au_prod:{sizes['gold_mine_production']})")

    # V1 dual-write — non-fatal on failure. V1 reads /data/metals.json
    # lazily; if the mirror fails the tab just shows its empty state until
    # the next run.
    v1_out_arg = (args.out_v1 or "").strip()
    if v1_out_arg:
        v1_out_path = Path(v1_out_arg)
        try:
            v1_out_path.parent.mkdir(parents=True, exist_ok=True)
            v1_out_path.write_text(json.dumps(payload, indent=2))
            print(f"  [metals] mirrored payload to {v1_out_path}")
        except Exception as e:
            print(f"  [metals] v1 dual-write failed ({v1_out_path}): {e}",
                  file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
