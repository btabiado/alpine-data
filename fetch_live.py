"""BTC ETF-flow seeding helper for the local dev server.

WHAT IS LEFT IN HERE, AND WHY
-----------------------------
One function: `fetch_btc_from_github_mirror`. It is reachable — `server.py`
imports this module and `POST /api/seed-etf` calls it, which is what the
"Seed BTC (mirror)" button in the dashboard's ETF Flows tab fires. Two tests
in `tests/test_server.py` cover that route.

WHAT WAS REMOVED (2026-08-03), AND WHY
--------------------------------------
This module used to carry three more ETF-flow integrations. All three were
dead, and their existence was actively misleading — two repo secrets
(COINGLASS_API_KEY, SOSOVALUE_API_KEY) were created for code that could never
run:

  * `fetch_coinglass()`  — open-api-v4.coinglass.com, gated on COINGLASS_API_KEY
  * `fetch_sosovalue()`  — api.sosovalue.com, gated on SOSOVALUE_API_KEY
                           (that host no longer resolves at all — NXDOMAIN)
  * `fetch_all()`        — the dispatcher, plus `write_csv`, `_to_millions`
                           and `_normalize_provider_rows`, which existed only
                           to serve the two provider paths

`fetch_all()` had exactly two call sites, `app.py --fetch` and
`v2/app.py --fetch`, and NO workflow anywhere passes `--fetch` (pages.yml uses
`--fetch-market`). Crypto ETF flows are refreshed by `scripts/fetch_etf_flows.py`
(Farside, keyless) on its own cron — see `.github/workflows/etf-flows-daily.yml`.

`fetch_all` survives only as a tombstone that raises: the two call sites live
in files this change is not allowed to touch, and a clear "this was retired,
here is what replaced it" beats an AttributeError.

ON THE MIRROR ITSELF
--------------------
The mirror is reachable but ABANDONED: its last row is 2025-05-02, a year
OLDER than the committed data/btc_flows.csv (2026-05-12), and it carries only
a `Total` column against the committed file's 13 per-fund columns. Letting it
write unconditionally means one button click silently regresses the dataset by
a year and destroys the per-fund breakdown, so the writer now refuses to go
backwards — see `fetch_btc_from_github_mirror`.
"""

from __future__ import annotations

from pathlib import Path

import requests

UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)"


MIRROR_BTC_CSV = "https://raw.githubusercontent.com/canadiancode/btc-etf-flows/main/Bitcoin-ETF-Flow-Data/data/BTC_ETF_INFLOWS_OUTFLOWS.csv"


def _newest_date(csv_text: str) -> str:
    """Newest ISO date in a `date,...` CSV body. '' when there isn't one.

    Dates are ISO (YYYY-MM-DD) in both the committed file and our mirror
    output, so a plain string max is a correct date max.
    """
    newest = ""
    for line in csv_text.splitlines()[1:]:
        d = line.split(",", 1)[0].strip().strip('"')
        if len(d) == 10 and d[4] == "-" and d[7] == "-" and d > newest:
            newest = d
    return newest


def fetch_btc_from_github_mirror(data_dir: Path) -> int:
    """Pull the community-maintained Farside mirror into data/btc_flows.csv.

    Total column only — the mirror does not carry the per-fund breakdown.

    REFUSES TO REGRESS. The mirror is abandoned (last row 2025-05-02, verified
    2026-08-03) while the committed data/btc_flows.csv runs to 2026-05-12 with
    13 per-fund columns. Overwriting the newer file with the older feed is a
    silent year-long data regression, so if the file on disk already ends at or
    after the mirror's newest row we leave it alone and raise. The caller
    (`server.py`'s /api/seed-etf) turns that into a 503 with the reason logged,
    which is the honest answer: the seed did not happen, and here is why.

    Returns the row count written.
    """
    r = requests.get(MIRROR_BTC_CSV, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    text = r.text
    out_lines = ["date,Total"]
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        # Format: "20240111T",655.3
        try:
            d_part, v_part = line.split(",", 1)
            d = d_part.strip().strip('"').rstrip("T")
            iso = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            v = v_part.strip().strip('"')
            float(v)  # validate
            out_lines.append(f"{iso},{v}")
        except Exception:
            continue

    path = data_dir / "btc_flows.csv"
    mirror_newest = _newest_date("\n".join(out_lines))
    if path.exists():
        try:
            existing = path.read_text()
        except OSError:
            existing = ""
        existing_newest = _newest_date(existing)
        if existing_newest and (not mirror_newest or existing_newest >= mirror_newest):
            raise RuntimeError(
                f"refusing to seed from the mirror: it would regress "
                f"{path.name}. On disk runs to {existing_newest}; the mirror "
                f"ends {mirror_newest or 'nowhere (no parsable rows)'}. The "
                f"mirror is abandoned and Total-only; crypto ETF flows come "
                f"from scripts/fetch_etf_flows.py (Farside) instead."
            )

    path.write_text("\n".join(out_lines) + "\n")
    return len(out_lines) - 1


def fetch_all(data_dir: Path) -> None:
    """RETIRED. Kept only so `app.py --fetch` fails with a sentence, not a
    stack-shaped AttributeError.

    The CoinGlass and SoSoValue paths this used to dispatch to are gone (see
    the module docstring); the mirror fallback it used to fall back to is a
    year staler than the committed CSVs.
    """
    raise RuntimeError(
        "fetch_live.fetch_all is retired: the CoinGlass and SoSoValue ETF-flow "
        "paths were dead code (no workflow passes --fetch, and api.sosovalue.com "
        "no longer resolves). Crypto ETF flows are refreshed by "
        "scripts/fetch_etf_flows.py (Farside, keyless) via "
        ".github/workflows/etf-flows-daily.yml."
    )
