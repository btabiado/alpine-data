#!/usr/bin/env python3
"""Sync data/lthcs/universe.json to the current S&P 500 + DJIA constituents.

Inputs (one dated candidate directory, committed so the sync is
reproducible and auditable)::

    data/lthcs/universe_candidate/<dir>/
        _source.json             provenance: URLs, revision ids, fetch date,
                                 the DJIA component list
        _constituents_sp500.csv  snapshot of the S&P 500 constituent table
                                 (Symbol, Security, GICS Sector,
                                 GICS Sub-Industry, Date added, CIK)
        _sec_exchange.json       ticker -> listing exchange (SEC
                                 company_tickers_exchange.json subset)
        _yahoo_metrics.json      maturity-heuristic inputs for constituents
                                 that are absent from the May 2026 seed

Rules
-----
* Universe = current S&P 500 ∪ current DJIA ∪ everything already in it.
  Nobody already present is dropped or deactivated here; NASDAQ-100-only
  names stay active.
* ``index_membership``: "S&P 500" and "DJIA" tags are recomputed from the
  inputs for every ACTIVE entry. "S&P 100" / "NASDAQ-100" tags are left as
  they are unless ``--index-tags-dir`` is given (see below). Inactive
  entries are untouched.
* ``exchange`` is refreshed from the SEC exchange file; ``industry`` is
  filled from the GICS sub-industry where an entry has none.
* Maturity stage for new tickers:
    1. ticker restored under a new symbol (``RESTORES``) -> copy the old
       entry's stage;
    2. ticker present in ``sp500_candidate_seed.json`` (directly or under
       its pre-rename symbol, ``SEED_ALIASES``) -> the seed's documented
       heuristic value (same source Wave A used);
    3. otherwise the seed's documented heuristic, applied to the Yahoo
       metrics snapshot (see ``heuristic_stage``). Every non-obvious call is
       written to ``maturity_note``.
* New tickers carry no scores and no history. Nothing is backfilled.

S&P 100 / NASDAQ-100 tags (``--index-tags-dir``)
------------------------------------------------
A second dated directory holds the two other index lists::

    data/lthcs/universe_candidate/<dir>/
        _source.json             "sp100" / "ndx100" blocks: URL, revision id
                                 or as-of date, fetch date
        _constituents_sp100.csv  Symbol, Name, Sector
        _constituents_ndx100.csv Symbol, Name

For every ACTIVE entry the "S&P 100" and "NASDAQ-100" tags are recomputed
from those lists (S&P 500 / DJIA tags are not touched in this mode).
Constituents that are not in the universe are reported, never added: the
universe stays S&P 500 + DJIA + everything already in it. An entry that
loses its last tag stays active ("nobody dropped"). The report goes to
``_index_tag_report.json`` in the same directory.

Usage::

    python scripts/lthcs_universe_sp500_sync.py \\
        --candidate-dir data/lthcs/universe_candidate/sp500_2026-10-05 --write

    python scripts/lthcs_universe_sp500_sync.py \\
        --index-tags-dir data/lthcs/universe_candidate/mapping_2026-10-05 --write
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data" / "lthcs"

# New symbol -> symbol of the existing (now inactive) universe entry for the
# same company. The old entry stays inactive (its history file is keyed by
# the old symbol and is not merged — scores are never re-labelled).
RESTORES = {"BNY": "BK"}

# Current symbol -> symbol the May 2026 seed listed the same company under.
# VMRK is the renamed Equity Residential (SEC CIK 906107, 8-K 2026-08-17:
# AvalonBay merged into it), so its seed row is EQR, not AVB; both carry
# the same seed stage (standard_compounder).
SEED_ALIASES = {"FISV": "FI", "MRSH": "MMC", "VMRK": "EQR", "PSKY": "PARA"}

INDEX_ORDER = ["S&P 500", "S&P 100", "NASDAQ-100", "DJIA"]

EXCHANGE_MAP = {"NYSE": "NYSE", "Nasdaq": "NASDAQ", "CBOE": "CBOE", "NYSE American": "AMEX"}

SECTOR_MAP = {"Information Technology": "Technology"}

# GICS sub-industry -> GICS industry group, in the (pre-2023) vocabulary the
# seed and Wave A records use for ``sector_group``.
SUBINDUSTRY_TO_GROUP = {
    "Advertising": "Media & Entertainment",
    "Broadcasting": "Media & Entertainment",
    "Cable & Satellite": "Media & Entertainment",
    "Interactive Home Entertainment": "Media & Entertainment",
    "Interactive Media & Services": "Media & Entertainment",
    "Movies & Entertainment": "Media & Entertainment",
    "Publishing": "Media & Entertainment",
    "Integrated Telecommunication Services": "Telecommunication Services",
    "Wireless Telecommunication Services": "Telecommunication Services",
    "Apparel Retail": "Retailing",
    "Automotive Retail": "Retailing",
    "Broadline Retail": "Retailing",
    "Computer & Electronics Retail": "Retailing",
    "Distributors": "Retailing",
    "Home Improvement Retail": "Retailing",
    "Homefurnishing Retail": "Retailing",
    "Other Specialty Retail": "Retailing",
    "Apparel, Accessories & Luxury Goods": "Consumer Durables & Apparel",
    "Consumer Electronics": "Consumer Durables & Apparel",
    "Footwear": "Consumer Durables & Apparel",
    "Homebuilding": "Consumer Durables & Apparel",
    "Leisure Products": "Consumer Durables & Apparel",
    "Automobile Manufacturers": "Automobiles & Components",
    "Automotive Parts & Equipment": "Automobiles & Components",
    "Casinos & Gaming": "Consumer Services",
    "Hotels, Resorts & Cruise Lines": "Consumer Services",
    "Restaurants": "Consumer Services",
    "Specialized Consumer Services": "Consumer Services",
    "Consumer Staples Merchandise Retail": "Food & Staples Retailing",
    "Food Distributors": "Food & Staples Retailing",
    "Food Retail": "Food & Staples Retailing",
    "Agricultural Products & Services": "Food, Beverage & Tobacco",
    "Distillers & Vintners": "Food, Beverage & Tobacco",
    "Packaged Foods & Meats": "Food, Beverage & Tobacco",
    "Soft Drinks & Non-alcoholic Beverages": "Food, Beverage & Tobacco",
    "Tobacco": "Food, Beverage & Tobacco",
    "Household Products": "Household & Personal Products",
    "Personal Care Products": "Household & Personal Products",
    "Integrated Oil & Gas": "Energy",
    "Oil & Gas Equipment & Services": "Energy",
    "Oil & Gas Exploration & Production": "Energy",
    "Oil & Gas Refining & Marketing": "Energy",
    "Oil & Gas Storage & Transportation": "Energy",
    "Diversified Banks": "Banks",
    "Regional Banks": "Banks",
    "Asset Management & Custody Banks": "Diversified Financials",
    "Consumer Finance": "Diversified Financials",
    "Financial Exchanges & Data": "Diversified Financials",
    "Investment Banking & Brokerage": "Diversified Financials",
    "Multi-Sector Holdings": "Diversified Financials",
    "Transaction & Payment Processing Services": "Diversified Financials",
    "Insurance Brokers": "Insurance",
    "Life & Health Insurance": "Insurance",
    "Multi-line Insurance": "Insurance",
    "Property & Casualty Insurance": "Insurance",
    "Reinsurance": "Insurance",
    "Biotechnology": "Pharmaceuticals, Biotechnology & Life Sciences",
    "Life Sciences Tools & Services": "Pharmaceuticals, Biotechnology & Life Sciences",
    "Pharmaceuticals": "Pharmaceuticals, Biotechnology & Life Sciences",
    "Health Care Distributors": "Health Care Equipment & Services",
    "Health Care Equipment": "Health Care Equipment & Services",
    "Health Care Facilities": "Health Care Equipment & Services",
    "Health Care Services": "Health Care Equipment & Services",
    "Health Care Supplies": "Health Care Equipment & Services",
    "Health Care Technology": "Health Care Equipment & Services",
    "Managed Health Care": "Health Care Equipment & Services",
    "Aerospace & Defense": "Capital Goods",
    "Agricultural & Farm Machinery": "Capital Goods",
    "Building Products": "Capital Goods",
    "Construction & Engineering": "Capital Goods",
    "Construction Machinery & Heavy Transportation Equipment": "Capital Goods",
    "Electrical Components & Equipment": "Capital Goods",
    "Heavy Electrical Equipment": "Capital Goods",
    "Industrial Conglomerates": "Capital Goods",
    "Industrial Machinery & Supplies & Components": "Capital Goods",
    "Trading Companies & Distributors": "Capital Goods",
    "Data Processing & Outsourced Services": "Commercial & Professional Services",
    "Diversified Support Services": "Commercial & Professional Services",
    "Environmental & Facilities Services": "Commercial & Professional Services",
    "Human Resource & Employment Services": "Commercial & Professional Services",
    "Research & Consulting Services": "Commercial & Professional Services",
    "Air Freight & Logistics": "Transportation",
    "Cargo Ground Transportation": "Transportation",
    "Passenger Airlines": "Transportation",
    "Passenger Ground Transportation": "Transportation",
    "Rail Transportation": "Transportation",
    "Application Software": "Software & Services",
    "Internet Services & Infrastructure": "Software & Services",
    "IT Consulting & Other Services": "Software & Services",
    "Systems Software": "Software & Services",
    "Communications Equipment": "Technology Hardware & Equipment",
    "Electronic Components": "Technology Hardware & Equipment",
    "Electronic Equipment & Instruments": "Technology Hardware & Equipment",
    "Electronic Manufacturing Services": "Technology Hardware & Equipment",
    "Technology Distributors": "Technology Hardware & Equipment",
    "Technology Hardware, Storage & Peripherals": "Technology Hardware & Equipment",
    "Semiconductor Materials & Equipment": "Semiconductors & Semiconductor Equipment",
    "Semiconductors": "Semiconductors & Semiconductor Equipment",
    "Commodity Chemicals": "Materials",
    "Construction Materials": "Materials",
    "Copper": "Materials",
    "Fertilizers & Agricultural Chemicals": "Materials",
    "Gold": "Materials",
    "Industrial Gases": "Materials",
    "Metal, Glass & Plastic Containers": "Materials",
    "Paper & Plastic Packaging Products & Materials": "Materials",
    "Specialty Chemicals": "Materials",
    "Steel": "Materials",
    "Data Center REITs": "Real Estate",
    "Health Care REITs": "Real Estate",
    "Hotel & Resort REITs": "Real Estate",
    "Industrial REITs": "Real Estate",
    "Multi-Family Residential REITs": "Real Estate",
    "Office REITs": "Real Estate",
    "Other Specialized REITs": "Real Estate",
    "Real Estate Services": "Real Estate",
    "Retail REITs": "Real Estate",
    "Self-Storage REITs": "Real Estate",
    "Single-Family Residential REITs": "Real Estate",
    "Telecom Tower REITs": "Real Estate",
    "Timber REITs": "Real Estate",
    "Electric Utilities": "Utilities",
    "Gas Utilities": "Utilities",
    "Independent Power Producers & Energy Traders": "Utilities",
    "Multi-Utilities": "Utilities",
    "Water Utilities": "Utilities",
}

TECH_SUB_BUCKET = {
    "Semiconductors": "Semiconductors",
    "Semiconductor Materials & Equipment": "Semiconductors",
    "Application Software": "Software",
    "Systems Software": "Software",
    "IT Consulting & Other Services": "IT Services",
    "Internet Services & Infrastructure": "IT Services",
    "Communications Equipment": "Hardware",
    "Electronic Components": "Hardware",
    "Electronic Equipment & Instruments": "Hardware",
    "Electronic Manufacturing Services": "Hardware",
    "Technology Distributors": "Hardware",
    "Technology Hardware, Storage & Peripherals": "Hardware",
}

TECH_PEER_GROUP = {
    "Hardware": "tech_hardware",
    "Semiconductors": "tech_semiconductors",
    "Software": "tech_software",
    "IT Services": "tech_it_services",
}

# Online platforms whose GICS sub-industry would otherwise land them in
# consumer_cyclical / communication_telecom; the curated peer_groups.json
# keeps such names in tech_internet (AMZN/BKNG/ABNB/DASH, TTD precedent).
INTERNET_PLATFORMS = {"EBAY", "EXPE", "APP"}

# Custody banks follow BK's precedent (banks cohort).
CUSTODY_BANKS = {"BNY", "STT", "NTRS"}

# --index-tags-dir inputs: index tag -> (_source.json key, constituents file).
TAG_LISTS = {
    "S&P 100": ("sp100", "_constituents_sp100.csv"),
    "NASDAQ-100": ("ndx100", "_constituents_ndx100.csv"),
}
_UNREFRESHED_TAGS = "S&P 100 / NASDAQ-100 tags were not refreshed in this sync."

TODAY = "2026-10-05"
FIVE_YEARS_AGO = _dt.date(2021, 10, 5)
TWENTY_YEARS_AGO = _dt.date(2006, 10, 5)


def peer_group_for(ticker: str, sector: str, sub: str, tech_bucket: Optional[str]) -> str:
    if sector == "Technology":
        return TECH_PEER_GROUP.get(tech_bucket or "", "tech_hardware")
    if sector == "Communication Services":
        if sub in ("Interactive Media & Services", "Interactive Home Entertainment") or ticker in INTERNET_PLATFORMS:
            return "tech_internet"
        return "communication_telecom"
    if sector == "Consumer Discretionary":
        return "tech_internet" if ticker in INTERNET_PLATFORMS else "consumer_cyclical"
    if sector == "Consumer Staples":
        return "consumer_staples"
    if sector == "Health Care":
        return "healthcare_pharma" if sub in ("Pharmaceuticals", "Biotechnology") else "healthcare_devices"
    if sector == "Industrials":
        return "industrials_aero"
    if sector == "Energy":
        return "energy"
    if sector in ("Utilities", "Real Estate"):
        return "utilities_reits"
    if sector == "Materials":
        return "materials"
    if sector == "Financials":
        if sub in ("Diversified Banks", "Regional Banks") or ticker in CUSTODY_BANKS:
            return "banks"
        return "financials_non_bank"
    return "other"


def _year(ms: Any) -> Optional[_dt.date]:
    try:
        return _dt.datetime.fromtimestamp(float(ms) / 1000.0, tz=_dt.timezone.utc).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def heuristic_stage(sector: str, m: Dict[str, Any]) -> Tuple[str, str]:
    """Apply the seed's documented maturity heuristic to one metrics row.

    sp500_candidate_seed.json ``maturity_stage_heuristic``:
      mature_compounder   market_cap > $200B AND >20y public AND stable margins
      standard_compounder $5B-$200B AND >5y public
      growth_compounder   high revenue growth (>20% YoY) OR IPO <5y ago
      pre_profit_growth   negative GAAP earnings, revenue-positive
      recovery_*          stabilizing after operational stress (judgement)
      financial           banks / insurance / asset managers
    Returns (stage, note).
    """
    mcap = m.get("marketCap")
    g = m.get("revenueGrowth")
    eps = m.get("trailingEps")
    first = _year(m.get("firstTradeDateMilliseconds"))
    facts = "Yahoo %s: mcap %s, rev growth %s, trailing EPS %s, first trade %s" % (
        TODAY,
        ("$%.0fB" % (mcap / 1e9)) if isinstance(mcap, (int, float)) else "n/a",
        ("%+.0f%%" % (g * 100)) if isinstance(g, (int, float)) else "n/a",
        eps if eps is not None else "n/a",
        first.isoformat() if first else "n/a",
    )
    if sector == "Financials":
        note = "financial: Financials-sector convention of the seed/Wave A (%s)" % facts
        if (isinstance(eps, (int, float)) and eps < 0) or (isinstance(g, (int, float)) and g > 0.20):
            note += "; uncertain: growth/fintech profile rather than a classic bank/insurer"
        return "financial", note
    if not isinstance(g, (int, float)) and not isinstance(eps, (int, float)):
        return "standard_compounder", "default standard_compounder: no Yahoo fundamentals (uncertain; %s)" % facts
    if isinstance(eps, (int, float)) and eps < 0:
        if isinstance(g, (int, float)) and g > 0.20:
            return "pre_profit_growth", "pre_profit_growth: negative GAAP EPS with >20%% revenue growth (%s)" % facts
        return "recovery_stabilization", (
            "recovery_stabilization: negative GAAP EPS without >20%% growth read as operational "
            "stress (judgement call, uncertain; %s)" % facts)
    if isinstance(g, (int, float)) and g > 0.20:
        return "growth_compounder", "growth_compounder: revenue growth >20%% YoY (%s)" % facts
    if first and first > FIVE_YEARS_AGO:
        # Applied literally, as the seed did for KVUE / SOLV / VLTO.
        return "growth_compounder", (
            "growth_compounder via the heuristic's IPO<5y clause (%s); uncertain: a spin-off of an "
            "established business, growth <=20%%" % facts)
    if isinstance(mcap, (int, float)) and mcap > 200e9 and first and first < TWENTY_YEARS_AGO:
        return "mature_compounder", "mature_compounder: >$200B and >20y public (%s)" % facts
    return "standard_compounder", "standard_compounder (%s)" % facts


def _ordered_indices(tags: List[str]) -> List[str]:
    seen = [t for t in INDEX_ORDER if t in tags]
    return seen + sorted(t for t in tags if t not in INDEX_ORDER)


def build(candidate_dir: Path) -> Dict[str, Any]:
    src = json.loads((candidate_dir / "_source.json").read_text())
    exch = json.loads((candidate_dir / "_sec_exchange.json").read_text())
    metrics = json.loads((candidate_dir / "_yahoo_metrics.json").read_text())["tickers"]
    with (candidate_dir / "_constituents_sp500.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    sp = {r["Symbol"].strip().upper(): r for r in rows}
    djia = set(src["djia"]["components"])
    seed = {t["symbol"]: t for t in json.loads((DATA / "sp500_candidate_seed.json").read_text())["tickers"]}
    universe = json.loads((DATA / "universe.json").read_text())
    by = {e["ticker"]: e for e in universe["tickers"]}

    report: Dict[str, Any] = {"added": [], "restored": [], "index_changes": [], "exchange_changes": [],
                              "industry_filled": []}

    # ---- existing entries ----
    for e in universe["tickers"]:
        t = e["ticker"]
        if not e.get("active", True):
            continue
        tags = [x for x in e.get("index_membership", []) if x not in ("S&P 500", "DJIA")]
        if t in sp:
            tags.append("S&P 500")
        if t in djia:
            tags.append("DJIA")
        tags = _ordered_indices(tags)
        if tags != e.get("index_membership"):
            report["index_changes"].append({"ticker": t, "from": e.get("index_membership"), "to": tags})
            e["index_membership"] = tags
        ex = EXCHANGE_MAP.get(exch.get(t, ""))
        if ex and e.get("exchange") != ex:
            report["exchange_changes"].append({"ticker": t, "from": e.get("exchange"), "to": ex})
            e["exchange"] = ex
        if not e.get("industry") and t in sp:
            e["industry"] = sp[t]["GICS Sub-Industry"]
            report["industry_filled"].append(t)

    # ---- new entries ----
    new_entries: List[Dict[str, Any]] = []
    for t in sorted(set(sp) | djia):
        if t in by:
            continue
        r = sp.get(t)
        if r is None:
            raise SystemExit("DJIA component %s is not in the S&P 500 table; add it by hand" % t)
        sector = SECTOR_MAP.get(r["GICS Sector"], r["GICS Sector"])
        sub = r["GICS Sub-Industry"]
        seed_key = SEED_ALIASES.get(t, t)
        s = seed.get(seed_key)
        if t in RESTORES:
            old = by[RESTORES[t]]
            stage = old["maturity_stage"]
            note = ("Restored: same company as inactive entry %s (ticker change); stage copied from it. "
                    "History starts fresh under %s — %s scores are not re-labelled." % (RESTORES[t], t, RESTORES[t]))
            name = old["name"]
            group = SUBINDUSTRY_TO_GROUP[sub]
            report["restored"].append({"ticker": t, "from": RESTORES[t]})
        elif s is not None:
            stage = s["inferred_maturity_stage"]
            note = "Seed heuristic (sp500_candidate_seed.json, 2026-05-20)"
            if seed_key != t:
                note += "; seed listed this company as %s before its ticker change" % seed_key
            name = s["name"] if seed_key == t else r["Security"]
            group = s.get("gics_sector_group") or SUBINDUSTRY_TO_GROUP[sub]
        else:
            m = metrics.get(t)
            if m is None:
                raise SystemExit("no seed entry and no Yahoo metrics for %s" % t)
            stage, note = heuristic_stage(sector, m)
            name = m.get("longName") or r["Security"]
            group = SUBINDUSTRY_TO_GROUP[sub]
        ex = EXCHANGE_MAP.get(exch.get(t, ""))
        if ex is None:
            raise SystemExit("no exchange for %s" % t)
        tags = ["S&P 500"] + (["DJIA"] if t in djia else [])
        entry: Dict[str, Any] = {
            "ticker": t,
            "name": name,
            "exchange": ex,
            "index_membership": tags,
            "sector": sector,
            "industry": sub,
        }
        bucket = TECH_SUB_BUCKET.get(sub) if sector == "Technology" else None
        if sector == "Technology":
            entry["tech_sub_bucket"] = bucket or "Hardware"
        entry.update({
            "maturity_stage": stage,
            "active": True,
            "maturity_note": note,
            "sector_group": group,
            "cik": str(int(r["CIK"])).zfill(10),
            "source": "sp500_sync_%s" % TODAY,
        })
        entry["_peer_group"] = peer_group_for(t, sector, sub, entry.get("tech_sub_bucket"))
        new_entries.append(entry)
        report["added"].append(t)

    return {"universe": universe, "new": new_entries, "report": report, "source": src}


def _tags_sentence(tags_src: Dict[str, Any], tags_dir: Path) -> str:
    sp, nd = tags_src["sp100"], tags_src["ndx100"]
    return (
        "S&P 100 / NASDAQ-100 tags refreshed %s: S&P 100 from %s (revision %s), NASDAQ-100 from %s "
        "(Nasdaq list as of %s); inputs in %s."
        % (tags_src["fetched_at"], sp["url"], sp["revision_id"], nd["url"], nd["as_of"],
           _rel(tags_dir))
    )


def _rel(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix() + "/"
    except ValueError:
        return str(path)


def _set_tags_sentence(description: str, sentence: str) -> str:
    """Swap the S&P 100 / NASDAQ-100 sentence of the universe description."""
    if _UNREFRESHED_TAGS in description:
        return description.replace(_UNREFRESHED_TAGS, sentence)
    pattern = re.compile(r"S&P 100 / NASDAQ-100 tags refreshed .*?; inputs in \S+/\.")
    if pattern.search(description):
        return pattern.sub(lambda _m: sentence, description)
    return (description.rstrip() + " " + sentence).strip()


def _with_counts(description: str, universe: Dict[str, Any]) -> str:
    active = sum(1 for e in universe["tickers"] if e.get("active", True))
    return re.sub(r"\d+ tickers, \d+ active\.",
                  "%d tickers, %d active." % (len(universe["tickers"]), active), description)


def refresh_index_tags(universe: Dict[str, Any], tags_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Recompute "S&P 100" / "NASDAQ-100" tags of every active entry.

    Returns (report, _source.json of ``tags_dir``). Mutates ``universe``.
    """
    src = json.loads((tags_dir / "_source.json").read_text())
    members: Dict[str, set] = {}
    for tag, (key, fname) in TAG_LISTS.items():
        if key not in src:
            raise SystemExit("%s/_source.json has no %r block" % (tags_dir, key))
        with (tags_dir / fname).open(newline="", encoding="utf-8") as fh:
            members[tag] = {r["Symbol"].strip().upper() for r in csv.DictReader(fh) if r.get("Symbol")}
    by = {e["ticker"]: e for e in universe["tickers"]}
    report: Dict[str, Any] = {"source": _rel(tags_dir), "changes": [], "by_index": {}}
    for e in universe["tickers"]:
        if not e.get("active", True):
            continue
        old = list(e.get("index_membership") or [])
        tags = [x for x in old if x not in TAG_LISTS]
        tags += [tag for tag in TAG_LISTS if e["ticker"] in members[tag]]
        tags = _ordered_indices(tags)
        if tags != old:
            report["changes"].append({"ticker": e["ticker"], "from": old, "to": tags})
            e["index_membership"] = tags
    for tag in TAG_LISTS:
        before = {c["ticker"] for c in report["changes"] if tag in c["from"] and tag not in c["to"]}
        after = {c["ticker"] for c in report["changes"] if tag in c["to"] and tag not in c["from"]}
        report["by_index"][tag] = {
            "constituents": len(members[tag]),
            "tagged_active": sum(1 for e in universe["tickers"]
                                 if e.get("active", True) and tag in e.get("index_membership", [])),
            "added": sorted(after),
            "removed": sorted(before),
            "not_in_universe": sorted(t for t in members[tag] if t not in by),
            "inactive_in_universe": sorted(t for t in members[tag]
                                           if t in by and not by[t].get("active", True)),
        }
    report["active_without_index_tag"] = sorted(
        e["ticker"] for e in universe["tickers"]
        if e.get("active", True) and not e.get("index_membership"))
    return report, src


def apply(result: Dict[str, Any], tags: Optional[Tuple[Dict[str, Any], Path]] = None) -> None:
    universe = result["universe"]
    src = result["source"]
    new = result["new"]
    peer_assign = {e["ticker"]: e.pop("_peer_group") for e in new}
    universe["tickers"].extend(new)
    universe["tickers"].sort(key=lambda e: e["ticker"])
    active = sum(1 for e in universe["tickers"] if e.get("active", True))
    universe["version"] = "3.0.0"
    universe["last_updated"] = TODAY
    universe["description"] = (
        "LTHCS universe = current S&P 500 (all 503 share lines) + current DJIA 30 + the earlier "
        "NASDAQ-100 / S&P 100 / Wave A names (none dropped). %d tickers, %d active. "
        "Constituents: %s (revision %s, fetched %s); DJIA: %s (revision %s; GOOGL replaced VZ on "
        "2026-06-29). %s New tickers start "
        "with no history. Provenance and inputs: data/lthcs/universe_candidate/sp500_2026-10-05/."
        % (len(universe["tickers"]), active, src["sp500"]["url"], src["sp500"]["revision_id"],
           src["fetched_at"], src["djia"]["url"], src["djia"]["revision_id"],
           _tags_sentence(*tags) if tags else _UNREFRESHED_TAGS)
    )
    (DATA / "universe.json").write_text(json.dumps(universe, indent=2) + "\n", encoding="utf-8")

    pg_path = DATA / "peer_groups.json"
    pg = json.loads(pg_path.read_text())
    for t, g in peer_assign.items():
        group = pg["sector_groups"].setdefault(g, {"tickers": []})
        if t not in group["tickers"]:
            group["tickers"].append(t)
    for g in pg["sector_groups"].values():
        g["tickers"] = sorted(g["tickers"])
    pg["last_updated"] = TODAY
    pg_path.write_text(json.dumps(pg, indent=2) + "\n", encoding="utf-8")


def write_expand_input(result: Dict[str, Any], candidate_dir: Path) -> None:
    cols = ["ticker", "name", "sector", "sector_group", "maturity_stage", "index_membership",
            "tech_sub_bucket", "cik"]
    with (candidate_dir / "_input.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for e in result["new"]:
            w.writerow({
                "ticker": e["ticker"], "name": e["name"], "sector": e["sector"],
                "sector_group": e["sector_group"], "maturity_stage": e["maturity_stage"],
                "index_membership": "|".join(e["index_membership"]),
                "tech_sub_bucket": e.get("tech_sub_bucket", ""), "cik": e["cik"],
            })


def _write_tag_report(report: Dict[str, Any], tags_dir: Path) -> None:
    path = tags_dir / "_index_tag_report.json"
    if report["changes"] or not path.exists():
        path.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    else:
        # A re-run against an already-synced universe changes nothing; keep
        # the report of the run that did change the tags.
        print("no tag changes; kept %s" % _rel(path).rstrip("/"))
    for tag, r in report["by_index"].items():
        print("%s: %d constituents, %d active tagged | +%d -%d | not in universe: %s"
              % (tag, r["constituents"], r["tagged_active"], len(r["added"]), len(r["removed"]),
                 ", ".join(r["not_in_universe"]) or "-"))
    if report["active_without_index_tag"]:
        print("active, now without any index tag:", ", ".join(report["active_without_index_tag"]))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--candidate-dir", type=Path,
                    help="S&P 500 + DJIA candidate directory (adds tickers, refreshes those tags)")
    ap.add_argument("--index-tags-dir", type=Path,
                    help="directory with the S&P 100 / NASDAQ-100 lists (refreshes those tags)")
    ap.add_argument("--write", action="store_true", help="write universe.json + peer_groups.json")
    args = ap.parse_args(argv)
    if not args.candidate_dir and not args.index_tags_dir:
        ap.error("give --candidate-dir and/or --index-tags-dir")
    if not args.candidate_dir:
        universe = json.loads((DATA / "universe.json").read_text())
        report, tags_src = refresh_index_tags(universe, args.index_tags_dir)
        _write_tag_report(report, args.index_tags_dir)
        if args.write:
            universe["description"] = _with_counts(_set_tags_sentence(
                universe.get("description", ""), _tags_sentence(tags_src, args.index_tags_dir)), universe)
            (DATA / "universe.json").write_text(json.dumps(universe, indent=2) + "\n", encoding="utf-8")
            print("wrote universe.json")
        return 0
    result = build(args.candidate_dir)
    tags = None
    if args.index_tags_dir:
        # New entries are not in universe["tickers"] until apply(); tag them too.
        view = {"tickers": result["universe"]["tickers"] + result["new"]}
        report, tags_src = refresh_index_tags(view, args.index_tags_dir)
        _write_tag_report(report, args.index_tags_dir)
        tags = (tags_src, args.index_tags_dir)
    write_expand_input(result, args.candidate_dir)
    rep = result["report"]
    (args.candidate_dir / "_sync_report.json").write_text(
        json.dumps({k: rep[k] for k in rep}, indent=1) + "\n", encoding="utf-8")
    stages: Dict[str, int] = {}
    for e in result["new"]:
        stages[e["maturity_stage"]] = stages.get(e["maturity_stage"], 0) + 1
    print("new: %d (restored %d) | index changes: %d | exchange changes: %d | industry filled: %d"
          % (len(rep["added"]), len(rep["restored"]), len(rep["index_changes"]),
             len(rep["exchange_changes"]), len(rep["industry_filled"])))
    print("new-ticker stages:", dict(sorted(stages.items())))
    if args.write:
        apply(result, tags)
        print("wrote universe.json and peer_groups.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
