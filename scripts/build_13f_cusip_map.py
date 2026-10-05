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

    # Verify every ACTIVE ticker against the SEC Official List of Section
    # 13(f) Securities (download the TXT edition first; sec.gov needs a
    # descriptive User-Agent). Exit 1 on any problem; optionally write the
    # list rows that back each CUSIP as committed evidence.
    python -m scripts.build_13f_cusip_map --audit 13flist2026q2.txt \\
        --previous-list 13flist2026q1.txt \\
        --evidence-out data/lthcs/universe_candidate/mapping_2026-10-05/_sec_13flist_rows.json

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


# --- SEC Official List of Section 13(f) Securities ---------------------------
#
# The quarterly list (https://www.sec.gov/rules-regulations/staff-guidance/
# official-list-section-13f-securities, TXT edition) is fixed-width:
#   cols 1-9    CUSIP (9 chars)
#   col 10      "*" when options trade on the security
#   cols 11-40  issuer name
#   cols 41-59  issue description (COM, CL A, SPONSORED ADS, CALL, PUT, ...)
#   cols 60-70  status: "*A*" added / "*D*" deleted since the previous list
# A "*D*" row is no longer a 13(f) security for that quarter.

def parse_sec_13f_list(path: Path) -> Dict[str, Dict[str, Any]]:
    """``{cusip: {cusip, options, issuer, description, status}}`` for one list."""
    out: Dict[str, Dict[str, Any]] = {}
    with open(path, "r", encoding="latin-1") as fh:
        for line in fh:
            line = line.rstrip("\r\n")
            if len(line) < 40:
                continue
            cusip = line[0:9].strip()
            if len(cusip) != 9:
                continue
            out[cusip] = {
                "cusip": cusip,
                "options": line[9:10] == "*",
                "issuer": line[10:40].strip(),
                "description": line[40:59].strip(),
                "status": line[59:70].strip(),
            }
    return out


def cusip_check_digit(cusip8: str) -> str:
    """Standard CUSIP modulus-10 "double-add-double" check digit."""
    total = 0
    for i, ch in enumerate(cusip8.upper()):
        if ch.isdigit():
            v = int(ch)
        elif ch.isalpha():
            v = ord(ch) - 55
        else:
            v = {"*": 36, "@": 37, "#": 38}[ch]
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return str((10 - total % 10) % 10)


def _live(row: Optional[Dict[str, Any]]) -> bool:
    return bool(row) and row.get("status") != "*D*" and row.get("description") not in ("CALL", "PUT")


def audit(universe: List[Dict[str, Any]], cusip_map: Dict[str, Any],
          current: Dict[str, Dict[str, Any]],
          previous: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Check every ACTIVE ticker's CUSIPs against the official list(s).

    A ticker passes when it has at least one CUSIP that is live on the
    ``current`` list, and every CUSIP it lists is live on either the
    current list or (predecessor kept for the quarter-over-quarter compare)
    the ``previous`` list. CUSIPs must carry a valid check digit, and no
    CUSIP may be claimed by two active tickers.
    """
    previous = previous or {}
    entries = cusip_map.get("tickers", {})
    problems: Dict[str, List[str]] = {}
    claims: Dict[str, List[str]] = {}
    covered = 0
    active = [e["ticker"] for e in universe if e.get("active", True)]
    for t in active:
        cusips = (entries.get(t) or {}).get("cusips") or []
        issues: List[str] = []
        if not cusips:
            issues.append("no CUSIP")
        if any(_live(current.get(c)) for c in cusips):
            covered += 1
        elif cusips:
            issues.append("no CUSIP live on the current list")
        for c in cusips:
            claims.setdefault(c[:8], []).append(t)
            if len(c) != 9 or cusip_check_digit(c[:8]) != c[8]:
                issues.append("%s: bad check digit" % c)
            if not (_live(current.get(c)) or _live(previous.get(c))):
                issues.append("%s: not live on the current or previous list" % c)
        if issues:
            problems[t] = issues
    for c8, tickers in claims.items():
        if len(tickers) > 1:
            for t in tickers:
                problems.setdefault(t, []).append("%s claimed by %s" % (c8, ",".join(tickers)))
    return {"active": len(active), "covered": covered, "problems": problems}


def evidence_rows(universe: List[Dict[str, Any]], cusip_map: Dict[str, Any],
                  current: Dict[str, Dict[str, Any]],
                  previous: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The official-list rows backing each active ticker's CUSIPs."""
    previous = previous or {}
    entries = cusip_map.get("tickers", {})
    out: Dict[str, Any] = {}
    for e in universe:
        if not e.get("active", True):
            continue
        t = e["ticker"]
        rows = []
        for c in (entries.get(t) or {}).get("cusips") or []:
            if c in current:
                rows.append(dict(current[c], list="current"))
            if c in previous and not _live(current.get(c)):
                rows.append(dict(previous[c], list="previous"))
        out[t] = rows
    return out


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
    parser.add_argument(
        "--audit", type=Path, metavar="LIST_TXT",
        help="Check every active ticker's CUSIPs against a downloaded SEC "
             "Official 13(f) list (TXT edition); exit 1 on any problem"
    )
    parser.add_argument(
        "--previous-list", type=Path, metavar="LIST_TXT",
        help="The previous quarter's list (predecessor CUSIPs kept for the "
             "quarter-over-quarter compare must be live on it)"
    )
    parser.add_argument(
        "--evidence-out", type=Path, metavar="JSON",
        help="With --audit: write the list rows backing every active "
             "ticker's CUSIPs to this file"
    )
    args = parser.parse_args(argv)

    universe = _load_universe()
    cusip_map = _load_cusip_map()

    if args.audit:
        current = parse_sec_13f_list(args.audit)
        previous = parse_sec_13f_list(args.previous_list) if args.previous_list else {}
        result = audit(universe, cusip_map, current, previous)
        print("{covered}/{active} active tickers have a CUSIP live on {name}".format(
            name=args.audit.name, **result))
        for t, issues in sorted(result["problems"].items()):
            print("  {}: {}".format(t, "; ".join(issues)))
        if args.evidence_out:
            payload = {
                "current_list": args.audit.name,
                "previous_list": args.previous_list.name if args.previous_list else None,
                "tickers": evidence_rows(universe, cusip_map, current, previous),
            }
            args.evidence_out.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
            print("Wrote {}".format(args.evidence_out))
        return 1 if result["problems"] else 0

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
