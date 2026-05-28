"""One-shot builder for ``data/lthcs/13f_cusip_map.json``.

Reads ``data/lthcs/universe.json`` and emits / refreshes the CUSIP +
name-aliases map used by ``lthcs.sources.sec_13f`` to match 13F
holdings rows to LTHCS tickers. The Phase 1 build seeded all 168
tickers by hand from 10-K cover pages + the OpenFIGI free tier; this
script is the maintenance tool for adding new tickers and revising
aliases without hand-editing the JSON.

Usage::

    # Show tickers in universe.json that are MISSING from the map.
    python -m scripts.build_13f_cusip_map --missing

    # Dump the merged JSON (current + universe defaults for missing
    # tickers) to stdout for hand review.
    python -m scripts.build_13f_cusip_map --dump

    # Write the merged JSON back to disk (creates a .bak first).
    python -m scripts.build_13f_cusip_map --write

The script is intentionally NON-DESTRUCTIVE — it never deletes
existing entries, only adds new tickers from the universe with empty
``cusips`` arrays (signaling "needs review") plus a single
``name_aliases`` entry derived from the universe's ``name`` field. The
operator is expected to hand-edit the JSON to fill in the CUSIPs from
the issuer's most recent 10-K cover page, OpenFIGI, or any other
authoritative source.

OpenFIGI API helper (optional)::

    # Hit the OpenFIGI free tier (25 req / 6 sec, no key required) to
    # populate the CUSIP for new tickers. Limited to small batches so
    # the rate limit doesn't bite.
    OPENFIGI_API_KEY=xxx python -m scripts.build_13f_cusip_map \\
        --openfigi-fill --tickers AAPL,MSFT

The OpenFIGI block is a stub — coverage is uneven for sub-mega-cap
names and the operator typically gets faster results by hand-copying
CUSIPs from the cover page of each 10-K. See spec
``docs/lthcs-full-13f-impl-spec.md`` §9.1 for the trade-offs.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"
CUSIP_MAP_PATH = REPO_ROOT / "data" / "lthcs" / "13f_cusip_map.json"


def _load_universe() -> List[Dict[str, Any]]:
    with open(UNIVERSE_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    tickers = data.get("tickers")
    if not isinstance(tickers, list):
        raise SystemExit("universe.json: expected top-level 'tickers' list")
    return tickers


def _load_cusip_map() -> Dict[str, Any]:
    if not CUSIP_MAP_PATH.exists():
        return {
            "version": 1,
            "as_of": "",
            "description": "Externalized ticker -> CUSIP map for sec_13f.",
            "tickers": {},
        }
    with open(CUSIP_MAP_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _alias_from_name(name: str) -> str:
    """Derive a coarse name alias from a universe ``name`` field.

    Strip common corporate suffixes so the resulting alias is short
    enough for the issuer-name-startswith match in ``sec_13f``.
    """
    s = (name or "").lower()
    # Strip suffixes.
    s = re.sub(r"\b(inc|incorporated|corp|corporation|co|company|llc|ltd|plc|holdings|holdco|nv|sa|ag|group|class\s+[a-z]|cl\s+[a-z]|com|common\s+stock)\b\.?", "", s)
    # Collapse whitespace + drop trailing punctuation.
    s = re.sub(r"\s+", " ", s).strip(" .,&-/")
    return s


def _missing_tickers(
    universe: List[Dict[str, Any]], cusip_map: Dict[str, Any]
) -> List[str]:
    have = set(cusip_map.get("tickers", {}).keys())
    return [t["ticker"] for t in universe if t.get("ticker") and t["ticker"] not in have]


def _merge(
    universe: List[Dict[str, Any]], cusip_map: Dict[str, Any]
) -> Dict[str, Any]:
    """Add stub entries for universe tickers missing from the CUSIP map.

    Existing entries are preserved verbatim; new entries get an empty
    ``cusips`` array (signaling "needs review") plus a single
    ``name_aliases`` derived from ``_alias_from_name(universe.name)``.
    """
    out_tickers = dict(cusip_map.get("tickers", {}))
    for entry in universe:
        ticker = entry.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            continue
        if ticker in out_tickers:
            continue
        alias = _alias_from_name(entry.get("name") or "")
        out_tickers[ticker] = {
            "cusips": [],
            "name_aliases": [alias] if alias else [],
        }
    return {
        **cusip_map,
        "tickers": out_tickers,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--missing", action="store_true",
        help="List universe tickers absent from 13f_cusip_map.json"
    )
    parser.add_argument(
        "--dump", action="store_true",
        help="Print the merged map (universe + existing) to stdout"
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Write the merged map back to disk (creates .bak first)"
    )
    args = parser.parse_args(argv)

    universe = _load_universe()
    cusip_map = _load_cusip_map()

    if args.missing:
        missing = _missing_tickers(universe, cusip_map)
        if not missing:
            print("All {} universe tickers present in CUSIP map.".format(len(universe)))
            return 0
        print("Missing ({}):".format(len(missing)))
        for t in sorted(missing):
            print("  {}".format(t))
        return 1

    if args.dump:
        merged = _merge(universe, cusip_map)
        json.dump(merged, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    if args.write:
        merged = _merge(universe, cusip_map)
        if CUSIP_MAP_PATH.exists():
            backup = CUSIP_MAP_PATH.with_suffix(CUSIP_MAP_PATH.suffix + ".bak")
            shutil.copyfile(CUSIP_MAP_PATH, backup)
            print("Backup written to {}".format(backup))
        with open(CUSIP_MAP_PATH, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
            fh.write("\n")
        added = [t for t in merged["tickers"] if t not in cusip_map.get("tickers", {})]
        print("Wrote {} (added {} new tickers).".format(CUSIP_MAP_PATH, len(added)))
        if added:
            print("New entries need CUSIPs filled in manually:")
            for t in sorted(added):
                print("  {}".format(t))
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
