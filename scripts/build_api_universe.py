#!/usr/bin/env python3
"""Build the data file behind /api-universe/ (deploy-time, never committed).

The API Universe page (api_universe/index.html) is a radial map of every entry
in the Data Sources Catalog: Alpine Data in the centre, linked to the APIs it
already uses, then a ring of domains, a ring of categories and one dot per API.
The page is static; everything it draws comes from the JSON this writes.

Reads (both committed):
  health/api_catalog.json     the catalog: categories -> entries
  health/catalog_health.json  the weekly link check (scripts/check_catalog_links.py)

Writes:
  api_universe/data.json      compact arrays, about 0.7 MB (gitignored)

pages.yml runs this before the stage step, which publishes the page and this
file at /api-universe/ and /api-universe/data.json.

Domains. The catalog has no domain level; the page groups its categories into
the eight DOMAINS below by category-name prefix. Every category must match
exactly one prefix. A new or renamed category that matches none (or two) stops
the build with a message naming it, rather than landing in a catch-all or
silently vanishing from the map: add a prefix for it to the right domain.
tests/test_api_universe.py runs the same mapping over the committed catalog, so
a catalog change that breaks it fails CI on its PR.

Link problems are matched by URL, the way /health/'s catalog pane and the
workbook (scripts/build_catalog_xlsx.py) match them: an entry is flagged when
its own normalised URL is listed as dead or unreachable. Matching by service
name instead would flag an entry whose URL was corrected after the check.

Usage:
    python scripts/build_api_universe.py [--catalog PATH] [--health PATH] [--out PATH]

Exit codes: 0 written; 1 the inputs cannot be mapped or are inconsistent
(the message says which category or count); 2 an input file is missing or
is not JSON.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = "health/api_catalog.json"
HEALTH = "health/catalog_health.json"
OUT = "api_universe/data.json"

# Domain -> category-name prefixes, in the order the page draws them clockwise
# from 12 o'clock. Prefixes (not full names) so a category's long tail can be
# reworded without touching this table; each must still pick out one domain.
DOMAINS: dict[str, tuple[str, ...]] = {
    "AI & Agents": (
        "Agent evaluation", "Agent orchestration", "Agent tooling", "Autonomous agent",
        "Browser automation", "LLM gateways", "Local model runtimes", "Low-code agent",
        "Model & dataset registries", "Multimodal generation", "Open agent protocols",
        "Open foundation model", "Open model hosting", "Prompt/agent memory", "Vector DBs",
        "Accessibility, fonts", "Computer & Information",
    ),
    "Health & Life Sciences": (
        "Biological & Biomedical", "Biomedical literature", "Clinical trial registries",
        "Clinical trial results", "Clinical trials", "Drug databases", "Drug regulatory",
        "Genomics", "Medicine & Clinical", "Neuroscience", "Nutrition, food",
        "Pharmaceuticals", "Public health",
    ),
    "Research & Scholarship": (
        "Altmetrics", "Bibliographic", "Data repositories", "Open access discovery",
        "Preprint servers", "Researcher & org", "Scholarly books", "Theses", "Patents",
        "Standards & specifications", "Identity, standards", "Grants & funding",
        "Mathematics & Statistics", "Physics & Astronomy", "Chemistry & Chemical",
        "Materials science", "Engineering (", "Education & Pedagogy",
    ),
    "Government, Law & Defense": (
        "Aerospace & Defense", "Election, legislative", "Government & scientific",
        "Government procurement", "Law & Legal", "US defense spending",
        "US federal government", "US legislative", "US military", "Political science",
    ),
    "Earth, Climate & Energy": (
        "Air quality", "Disaster, humanitarian", "Earth, Geology", "Environmental science",
        "Forestry", "Geospatial basemaps", "Geography, GIS", "Ocean, Marine",
        "Renewable energy", "Utilities & Water", "Waste, Recycling", "Agriculture & Food",
        "Zoos, Aquariums",
    ),
    "Travel & Mobility": (
        "Aviation & air traffic", "Maritime & vessel", "Public transit",
        "Transportation, Rail", "Tourism, Travel-tech", "Hospitality, Hotels",
    ),
    "Business & Industry": (
        "Advertising", "Business, Finance", "Cannabis", "Chemicals & Materials",
        "Construction", "Economics & Econometrics", "Fashion", "Insurance",
        "Manufacturing", "Mining, Metals", "Retail, CPG", "Robotics", "Semiconductors",
        "Telecom",
    ),
    "Culture & Society": (
        "Anthropology", "Cultural heritage", "History & Digital", "Linguistics",
        "Philosophy", "Sociology", "Survey microdata", "Gaming & Esports",
        "Sports statistics", "Psychology",
    ),
}

IN_USE = "In use"
STATUSES = {IN_USE, "Available"}

# Order of the per-API arrays in the output. The page reads them by position
# (api_universe/index.html, the DATA.apis.map(...) line); keep the two in step.
FIELDS = ("cat", "name", "provider", "free_tier", "price", "blurb", "in_use",
          "keyless", "key_required", "url", "link_problem", "free")


class CatalogError(ValueError):
    """The catalog cannot be drawn as it stands. The message says what to fix."""


class InputError(OSError):
    """An input file is missing or is not JSON."""


def domain_of(category: str) -> int:
    """Index into DOMAINS of the one domain whose prefix starts `category`."""
    if not isinstance(category, str) or not category.strip():
        raise CatalogError(f"a catalog category has no name ({category!r})")
    hits = [i for i, prefixes in enumerate(DOMAINS.values())
            if any(category.startswith(p) for p in prefixes)]
    if len(hits) == 1:
        return hits[0]
    names = list(DOMAINS)
    if not hits:
        raise CatalogError(
            f"category {category!r} matches no domain prefix. Add a prefix for it "
            f"to one of {names} in DOMAINS (scripts/build_api_universe.py).")
    raise CatalogError(
        f"category {category!r} matches prefixes in more than one domain "
        f"({[names[i] for i in hits]}). Make the prefixes in DOMAINS "
        f"(scripts/build_api_universe.py) pick out exactly one.")


def _norm(url: str) -> str:
    # Same normalisation as build_catalog_xlsx._norm, so the page and the
    # workbook flag the same rows (tests/test_api_universe.py checks this).
    return (url or "").strip().rstrip("/").lower()


def link_problems(health: dict) -> dict[str, str]:
    """Normalised URL -> the label the page shows for it."""
    out: dict[str, str] = {}
    for rec in health.get("unreachable") or []:
        out[_norm(rec.get("url", ""))] = "Unreachable"
    for rec in health.get("dead") or []:   # dead wins over unreachable
        code = rec.get("code")
        out[_norm(rec.get("url", ""))] = f"Dead link (HTTP {code})" if code else "Dead link"
    out.pop("", None)
    return out


def build(catalog: dict, health: dict) -> dict:
    """The page's data, from the two parsed inputs. Raises CatalogError."""
    categories = catalog.get("categories")
    if not isinstance(categories, list) or not categories:
        raise CatalogError("the catalog has no categories")

    # Map every category before building anything, so one run reports all of
    # the categories that need a domain rather than the first of them.
    problems, dom_of = [], {}
    for c in categories:
        name = c.get("category") if isinstance(c, dict) else None
        try:
            dom_of[name] = domain_of(name)
        except CatalogError as e:
            problems.append(str(e))
    if problems:
        raise CatalogError("\n".join(problems))
    dupes = sorted(n for n, k in Counter(c["category"] for c in categories).items() if k > 1)
    if dupes:
        raise CatalogError(f"categories listed more than once: {dupes}")

    domains = list(DOMAINS)
    empty = [d for i, d in enumerate(domains) if i not in dom_of.values()]
    if empty:
        raise CatalogError(f"no category maps to domain(s) {empty}; the map would "
                           f"draw an empty domain circle. Remove it from DOMAINS or "
                           f"move a category's prefix into it.")

    bad = link_problems(health)
    ordered = sorted(categories, key=lambda c: (dom_of[c["category"]], c["category"]))
    cats, apis = [], []
    for ci, c in enumerate(ordered):
        entries = c.get("entries") or []
        if "count" in c and c["count"] != len(entries):
            raise CatalogError(f"category {c['category']!r} says count={c['count']} "
                               f"but lists {len(entries)} entries")
        cats.append([c["category"], dom_of[c["category"]]])
        for e in entries:
            status = e.get("s")
            if status not in STATUSES:
                raise CatalogError(f"entry {e.get('n')!r} in {c['category']!r} has "
                                   f"status {status!r}; expected one of {sorted(STATUSES)}")
            apis.append([ci, e.get("n", ""), e.get("co", ""), e.get("f", ""), e.get("p", ""),
                         e.get("b", ""), 1 if status == IN_USE else 0,
                         1 if e.get("kl") else 0, 1 if e.get("kr") else 0, e.get("u", ""),
                         bad.get(_norm(e.get("u", "")), ""), 1 if e.get("fr") else 0])

    stated = (catalog.get("meta") or {}).get("total")
    if stated is not None and stated != len(apis):
        raise CatalogError(f"catalog meta.total is {stated} but its categories list "
                           f"{len(apis)} entries")

    meta = {
        "source": {"catalog": CATALOG, "health": HEALTH},
        "total": len(apis),
        "categories": len(cats),
        "domains": len(domains),
        "in_use": sum(a[6] for a in apis),
        "keyless": sum(a[7] for a in apis),
        "link_problems": sum(1 for a in apis if a[10]),
        "checked": health.get("checked_at") or "",
        "health": {k: (health.get("counts") or {}).get(k, 0)
                   for k in ("ok", "gated", "dead", "unreachable")},
    }
    return {"meta": meta, "fields": list(FIELDS), "domains": domains,
            "cats": cats, "apis": apis}


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InputError(f"input missing: {path}") from None
    except ValueError as e:
        raise InputError(f"input is not JSON: {path} ({e})") from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--catalog", default=str(ROOT / CATALOG))
    ap.add_argument("--health", default=str(ROOT / HEALTH))
    ap.add_argument("--out", default=str(ROOT / OUT))
    args = ap.parse_args(argv)

    try:
        catalog, health = _load(Path(args.catalog)), _load(Path(args.health))
    except InputError as e:
        print(f"[api-universe] {e}", file=sys.stderr)
        return 2
    try:
        data = build(catalog, health)
    except CatalogError as e:
        print(f"[api-universe] cannot build the API Universe:\n{e}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":"), ensure_ascii=False),
                   encoding="utf-8")
    m = data["meta"]
    print(f"[api-universe] wrote {out} ({out.stat().st_size:,} bytes): {m['total']:,} APIs, "
          f"{m['categories']} categories, {m['domains']} domains, {m['in_use']} in use, "
          f"{m['keyless']:,} keyless, {m['link_problems']} with link problems "
          f"(link check {m['checked'][:10] or 'unknown'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
