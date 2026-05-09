"""
Optional live-fetch helpers.

Reads API keys from environment variables and writes wide-format
CSVs into data/ that match the schema in app.py.

Supported providers (pick whichever you have):

  SOSOVALUE_API_KEY      docs: https://sosovalue.com  (Open API)
  COINGLASS_API_KEY      docs: https://www.coinglass.com/pricing  (v4 API)

Usage:
    SOSOVALUE_API_KEY=xxx python app.py --fetch
    COINGLASS_API_KEY=xxx python app.py --fetch

If neither key is set, this module raises and app.py falls back to the
existing CSVs in data/.
"""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable

import requests

UA = "Mozilla/5.0 (compatible; etf-flow-dashboard/1.0)"


MIRROR_BTC_CSV = "https://raw.githubusercontent.com/canadiancode/btc-etf-flows/main/Bitcoin-ETF-Flow-Data/data/BTC_ETF_INFLOWS_OUTFLOWS.csv"


def fetch_btc_from_github_mirror(data_dir: Path) -> int:
    """Pull a community-maintained Farside mirror. Total column only.
    The mirror may be stale — community-maintained, not real-time.
    Returns row count written.
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
    path.write_text("\n".join(out_lines) + "\n")
    return len(out_lines) - 1


def fetch_all(data_dir: Path) -> None:
    keys = {
        "sosovalue": os.environ.get("SOSOVALUE_API_KEY"),
        "coinglass": os.environ.get("COINGLASS_API_KEY"),
    }
    if not any(keys.values()):
        # Fallback: pull the GitHub mirror for BTC (Total column only).
        try:
            n = fetch_btc_from_github_mirror(data_dir)
            print(f"  [mirror] wrote btc_flows.csv ({n} rows from canadiancode/btc-etf-flows; may be stale)")
            return
        except Exception as e:
            raise RuntimeError(
                f"no API key, mirror fetch failed ({e}). "
                "Set SOSOVALUE_API_KEY or COINGLASS_API_KEY, or paste a CSV manually."
            )
    if keys["sosovalue"]:
        for asset in ("btc", "eth"):
            rows = fetch_sosovalue(asset, keys["sosovalue"])
            write_csv(data_dir / f"{asset}_flows.csv", rows)
            print(f"  [sosovalue] wrote {asset}_flows.csv ({len(rows)} rows)")
        return
    if keys["coinglass"]:
        for asset in ("btc", "eth"):
            rows = fetch_coinglass(asset, keys["coinglass"])
            write_csv(data_dir / f"{asset}_flows.csv", rows)
            print(f"  [coinglass] wrote {asset}_flows.csv ({len(rows)} rows)")


def fetch_sosovalue(asset: str, api_key: str) -> list[dict]:
    """SoSoValue Open API.

    Endpoint shape based on their public docs; if their schema changes,
    adjust here. This requests historical daily net inflow per ETF.
    """
    asset_type = "us-btc-spot" if asset == "btc" else "us-eth-spot"
    url = "https://api.sosovalue.com/openapi/v2/etf/historicalInflowChart"
    headers = {"x-soso-api-key": api_key, "User-Agent": UA, "Content-Type": "application/json"}
    body = {"type": asset_type}
    r = requests.post(url, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data") or payload.get("result") or []
    return _normalize_provider_rows(data)


def fetch_coinglass(asset: str, api_key: str) -> list[dict]:
    """CoinGlass v4 API: ETF flow history."""
    base = "https://open-api-v4.coinglass.com/api/etf"
    path = "/bitcoin/flow-history" if asset == "btc" else "/ethereum/flow-history"
    headers = {"CG-API-KEY": api_key, "User-Agent": UA}
    r = requests.get(base + path, headers=headers, timeout=30)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data") or []
    return _normalize_provider_rows(data)


def _normalize_provider_rows(data: Iterable) -> list[dict]:
    """Coerce a provider's per-day list into our wide CSV rows.

    Accepts items shaped like:
        {"date": "...", "list": [{"ticker":"IBIT","netInflow":...}, ...]}
        {"date": "...", "IBIT":..., "FBTC":...}
        {"date": "...", "perFundFlows": {"IBIT":..., ...}}
    Values are converted to USD millions.
    """
    rows: list[dict] = []
    for item in data:
        date = item.get("date") or item.get("day") or item.get("dt")
        if not date:
            continue
        # Normalize date to YYYY-MM-DD
        if isinstance(date, (int, float)):
            from datetime import datetime, timezone
            date = datetime.fromtimestamp(date / 1000 if date > 1e12 else date, tz=timezone.utc).strftime("%Y-%m-%d")

        funds = {}
        if "list" in item and isinstance(item["list"], list):
            for f in item["list"]:
                t = f.get("ticker") or f.get("symbol")
                v = _to_millions(f.get("netInflow") or f.get("flow") or f.get("value"))
                if t is not None and v is not None:
                    funds[t] = v
        elif "perFundFlows" in item and isinstance(item["perFundFlows"], dict):
            for t, v in item["perFundFlows"].items():
                v = _to_millions(v)
                if v is not None:
                    funds[t] = v
        else:
            for k, v in item.items():
                if k in ("date", "day", "dt", "total", "Total"):
                    continue
                v = _to_millions(v)
                if v is not None:
                    funds[k] = v
        if funds:
            rows.append({"date": date, **funds})
    rows.sort(key=lambda r: r["date"])
    return rows


def _to_millions(v) -> float | None:
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    # Heuristic: providers often return raw USD; convert to millions if it looks raw.
    if abs(v) > 1e6:
        v = v / 1e6
    return round(v, 3)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    cols = ["date"]
    seen = set(cols)
    for r in rows:
        for k in r.keys():
            if k not in seen:
                cols.append(k)
                seen.add(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
