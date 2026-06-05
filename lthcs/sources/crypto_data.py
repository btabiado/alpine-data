"""Crypto data adapter for the LTHCS crypto extension.

This is a thin adapter that REUSES data already produced by the V1 crypto
dashboard (``app.py`` -> ``fetch_market.py``) plus a handful of optional
free-tier supplements. It does NOT replace any existing fetcher and never
modifies the existing crypto data files.

Sources reused from V1 (read-only):

* ``data/whale.json`` -- BTC + ETH on-chain proxies (tx_volume, active
  addresses, hash rate, miners revenue, etc.) produced by
  ``fetch_market.py``. We also fall back to the repo-root
  ``data-whale.json`` because the legacy file lives there.
* ``data/btc_flows.csv`` / ``data/eth_flows.csv`` -- daily Farside ETF
  flows by issuer, with a ``Total`` column. Already in the repo (these
  ship to V1's ETF Flows tab).
* ``data/market.json`` -- CoinGecko markets_top snapshot when present.

Sources fetched fresh by this adapter (cache 24h to
``.cache/lthcs/crypto_data/``):

* CoinGecko ``/coins/markets`` -- price + market cap + 30d ROI. Free
  tier, no key required.
* Blockchain.info ``/charts`` -- BTC active addresses, hash rate (for
  the cases where ``whale.json`` is empty).
* DeFiLlama ``/stablecoins`` -- aggregate stablecoin market cap +
  prior-period series (for DES stablecoin-supply Δ30d).

All HTTP calls are wrapped in a per-call try/except that returns an
empty / neutral payload on failure. The adapter NEVER raises -- callers
treat absent data as a soft signal and the pillars renormalize.
"""

from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from lthcs.sources._cache import FileCache


__all__ = [
    "CryptoDataAdapter",
    "load_whale_payload",
    "load_etf_flows",
    "load_market_payload",
    "compute_etf_flow_30d",
    "fetch_coingecko_markets",
    "fetch_stablecoin_total",
    "fetch_blockchain_chart",
    "funding_rate_metrics",
    "long_short_ratio_metrics",
]


# Cache TTLs (24h matches the daily-pipeline cadence).
_CACHE_TTL_SECONDS = 24 * 60 * 60

# Polite per-call timeout. CoinGecko free tier sometimes stalls; we don't
# want a stuck socket to wedge the runner.
_HTTP_TIMEOUT = 15.0

# Map crypto symbol -> CoinGecko coin id. Expanded in Tier 5 #27 Phase 5
# to cover the 10-asset mature large-cap universe. Add new entries here
# whenever ``data/lthcs/crypto_universe.json`` grows; the runner reads
# this map to issue a single batched CoinGecko ``/coins/markets`` call.
# Note: POL (Polygon's renamed token) currently routes through the
# legacy ``matic-network`` CoinGecko id — CG kept the slug after the
# 2024 MATIC->POL migration so all historical data stays addressable.
COINGECKO_IDS: Dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "ADA": "cardano",
    "AVAX": "avalanche-2",
    "DOT": "polkadot",
    "LINK": "chainlink",
    "POL": "matic-network",
    "XRP": "ripple",
    "DOGE": "dogecoin",
}


def _cache_root() -> Path:
    return Path(os.environ.get("LTHCS_CACHE_DIR", ".cache/lthcs"))


_cache = FileCache("crypto_data", root=_cache_root())


# --- Path helpers ----------------------------------------------------------

def _repo_root() -> Path:
    # crypto_data.py -> lthcs/sources/ -> lthcs/ -> repo root
    return Path(__file__).resolve().parent.parent.parent


def _whale_candidate_paths() -> List[Path]:
    """Whale JSON can live in two places:

    * ``data/whale.json`` (canonical, written by fetch_market.py)
    * ``data-whale.json`` (legacy repo-root sidecar still used by app.py
      in some configurations).

    Try both, prefer the canonical one. The list is in priority order.
    """
    root = _repo_root()
    return [root / "data" / "whale.json", root / "data-whale.json"]


# --- Whale / on-chain payload ---------------------------------------------

def load_whale_payload(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the whale payload from the canonical location (or ``path``).

    Returns the parsed JSON dict, or ``{}`` if no file is found or it
    fails to parse. Never raises.
    """
    if path is not None:
        candidates: List[Path] = [Path(path)]
    else:
        candidates = _whale_candidate_paths()
    for p in candidates:
        try:
            if not p.exists():
                continue
            with p.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def load_market_payload(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load ``data/market.json`` written by ``fetch_market.py``.

    Returns the parsed JSON dict, or ``{}`` if the file is missing or
    fails to parse. Never raises.

    The payload has top-level per-asset blocks (``btc``, ``eth``,
    ``link``, ``ltc``) with ``funding`` and ``long_short_ratio`` daily
    series. The Thesis pillar reads these via :func:`funding_rate_metrics`
    and :func:`long_short_ratio_metrics`.
    """
    if path is not None:
        candidates: List[Path] = [Path(path)]
    else:
        candidates = [_repo_root() / "data" / "market.json"]
    for p in candidates:
        try:
            if not p.exists():
                continue
            with p.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def whale_series(whale: Dict[str, Any], asset: str, key: str) -> List[Dict[str, Any]]:
    """Return the time-series list under ``whale[<asset>][key]`` if any.

    ``asset`` is e.g. ``"btc"`` or ``"eth"``. ``key`` is the metric name
    (``"active_addresses"``, ``"hash_rate"``, ``"tx_volume_usd"``,
    ``"miners_revenue_usd"``, etc.). Returns an empty list when the
    series is missing or not a list.
    """
    if not isinstance(whale, dict):
        return []
    block = whale.get(asset.lower())
    if not isinstance(block, dict):
        return []
    series = block.get(key)
    if isinstance(series, list):
        return [r for r in series if isinstance(r, dict)]
    return []


def whale_distribution(whale: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return ``whale['distribution']['buckets']`` if any (BTC cohort series)."""
    if not isinstance(whale, dict):
        return []
    dist = whale.get("distribution")
    if not isinstance(dist, dict):
        return []
    buckets = dist.get("buckets")
    if isinstance(buckets, list):
        return [r for r in buckets if isinstance(r, dict)]
    return []


# --- ETF flows -------------------------------------------------------------

def load_etf_flows(symbol: str, *, data_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Read the Farside ETF flow CSV for BTC or ETH.

    Returns a list of ``{"date": "YYYY-MM-DD", "total": float}`` rows
    sorted ascending by date. SOL has no ETF coverage yet, so it always
    returns an empty list.
    """
    sym = (symbol or "").upper().strip()
    fname_map = {"BTC": "btc_flows.csv", "ETH": "eth_flows.csv"}
    fname = fname_map.get(sym)
    if not fname:
        return []
    root = Path(data_dir) if data_dir is not None else (_repo_root() / "data")
    path = root / fname
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                date_str = (r.get("date") or "").strip()
                if not date_str:
                    continue
                total_raw = r.get("Total")
                if total_raw is None or str(total_raw).strip() == "":
                    continue
                try:
                    total = float(total_raw)
                except (TypeError, ValueError):
                    continue
                rows.append({"date": date_str, "total": total})
    except OSError:
        return []
    rows.sort(key=lambda r: r["date"])
    return rows


def compute_etf_flow_30d(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Sum the trailing 30 ETF-flow rows (USD millions).

    Returns ``None`` when fewer than 30 rows are available (the signal is
    too thin to score). Otherwise returns the float sum.
    """
    if not rows or len(rows) < 30:
        return None
    tail = rows[-30:]
    total = 0.0
    for r in tail:
        try:
            total += float(r.get("total") or 0.0)
        except (TypeError, ValueError):
            continue
    return float(total)


def compute_etf_flow_pace(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Return the 30d/prior-30d ratio of ETF flows (signed).

    Used as a "pace acceleration" signal: a positive ratio means recent
    flows exceed the prior month. None when we don't have 60 rows.
    """
    if not rows or len(rows) < 60:
        return None
    recent = sum(float(r.get("total") or 0.0) for r in rows[-30:])
    prior = sum(float(r.get("total") or 0.0) for r in rows[-60:-30])
    if abs(prior) < 1e-6:
        return None
    return float((recent - prior) / abs(prior))


# --- Generic HTTP helpers --------------------------------------------------

def _http_get(url: str, *, timeout: float = _HTTP_TIMEOUT) -> Optional[bytes]:
    """GET ``url`` and return the response body, or None on any failure.

    Polite ``User-Agent``; small timeout; never raises.
    """
    req = Request(
        url,
        headers={
            "User-Agent": "lthcs-crypto/1.0 (+https://github.com/alpine-data)",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (URLError, TimeoutError, OSError, ValueError):
        return None


def _cached_json(cache_key: str, url: str) -> Any:
    """Fetch + cache a JSON-returning endpoint. Returns parsed value or None."""
    hit = _cache.get(cache_key)
    if hit is not None:
        return hit.value
    body = _http_get(url)
    if body is None:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    _cache.set(cache_key, parsed, ttl_seconds=_CACHE_TTL_SECONDS)
    return parsed


# --- CoinGecko -------------------------------------------------------------

def fetch_coingecko_markets(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch markets snapshot for ``symbols`` (BTC/ETH/SOL).

    Returns a dict keyed by uppercase symbol with ``price_usd``,
    ``market_cap_usd``, ``price_change_pct_30d`` (None if unavailable)
    and ``circulating_supply``. Empty dict on any failure.
    """
    ids = [COINGECKO_IDS[s.upper()] for s in symbols if s.upper() in COINGECKO_IDS]
    if not ids:
        return {}
    params = {
        "vs_currency": "usd",
        "ids": ",".join(ids),
        "price_change_percentage": "30d",
        "order": "market_cap_desc",
        "per_page": "10",
        "page": "1",
        "sparkline": "false",
    }
    url = "https://api.coingecko.com/api/v3/coins/markets?" + urlencode(params)
    cache_key = "coingecko/markets/" + ",".join(sorted(ids))
    payload = _cached_json(cache_key, url)
    if not isinstance(payload, list):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    by_id = {v: k for k, v in COINGECKO_IDS.items()}
    for row in payload:
        if not isinstance(row, dict):
            continue
        sym = by_id.get(row.get("id"))
        if sym is None:
            continue
        out[sym] = {
            "price_usd": _safe_float(row.get("current_price")),
            "market_cap_usd": _safe_float(row.get("market_cap")),
            "price_change_pct_30d": _safe_float(
                row.get("price_change_percentage_30d_in_currency")
            ),
            "circulating_supply": _safe_float(row.get("circulating_supply")),
            "total_volume_usd": _safe_float(row.get("total_volume")),
        }
    return out


# --- Blockchain.info ------------------------------------------------------

# Common BTC charts (https://www.blockchain.com/explorer/api/charts_api).
_BLOCKCHAIN_INFO_CHARTS = {
    "active_addresses": "n-unique-addresses",
    "hash_rate": "hash-rate",
    "tx_volume_usd": "estimated-transaction-volume-usd",
    "miners_revenue_usd": "miners-revenue",
}


def fetch_blockchain_chart(metric: str, *, days: int = 60) -> List[Dict[str, Any]]:
    """Fetch a daily blockchain.info chart series.

    Returns a list of ``{"date": "YYYY-MM-DD", "value": float}``. Empty
    on any failure. ``metric`` is a friendly key from the
    ``_BLOCKCHAIN_INFO_CHARTS`` map.
    """
    chart = _BLOCKCHAIN_INFO_CHARTS.get(metric)
    if not chart:
        return []
    params = {
        "timespan": "%sdays" % int(days),
        "format": "json",
        "sampled": "false",
    }
    url = "https://api.blockchain.info/charts/%s?%s" % (chart, urlencode(params))
    cache_key = "blockchain.info/%s/%dd" % (chart, int(days))
    payload = _cached_json(cache_key, url)
    if not isinstance(payload, dict):
        return []
    values = payload.get("values")
    if not isinstance(values, list):
        return []
    out: List[Dict[str, Any]] = []
    for v in values:
        if not isinstance(v, dict):
            continue
        ts = v.get("x")
        val = _safe_float(v.get("y"))
        if ts is None or val is None:
            continue
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            continue
        date_str = time.strftime("%Y-%m-%d", time.gmtime(ts_int))
        out.append({"date": date_str, "value": val})
    out.sort(key=lambda r: r["date"])
    return out


# --- DeFiLlama stablecoins ------------------------------------------------

def fetch_stablecoin_total() -> Dict[str, Any]:
    """Return ``{"now": float, "delta_30d_pct": float|None}`` for the
    aggregate stablecoin market cap.

    Uses DeFiLlama's ``/stablecoins`` endpoint. ``delta_30d_pct`` is the
    percent change between the most recent total and the snapshot ~30
    days back (or ``None`` if the series is too short).
    """
    url = "https://stablecoins.llama.fi/stablecoincharts/all"
    cache_key = "defillama/stablecoincharts/all"
    payload = _cached_json(cache_key, url)
    if not isinstance(payload, list):
        return {"now": None, "delta_30d_pct": None}
    # Each row has totalCirculating with peggedUSD field.
    totals: List[Tuple[int, float]] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        ts = row.get("date")
        circ = row.get("totalCirculating") or row.get("totalCirculatingUSD") or {}
        if isinstance(circ, dict):
            v = circ.get("peggedUSD")
        else:
            v = circ
        v_f = _safe_float(v)
        try:
            ts_i = int(ts)
        except (TypeError, ValueError):
            continue
        if v_f is None:
            continue
        totals.append((ts_i, v_f))
    if not totals:
        return {"now": None, "delta_30d_pct": None}
    totals.sort(key=lambda x: x[0])
    now = totals[-1][1]
    delta_30d_pct: Optional[float] = None
    if len(totals) >= 31:
        prior = totals[-31][1]
        if prior > 0:
            delta_30d_pct = float((now - prior) / prior * 100.0)
    return {"now": float(now), "delta_30d_pct": delta_30d_pct}


# --- Small utilities ------------------------------------------------------

def _safe_float(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def values_only(series: List[Dict[str, Any]], *, n: Optional[int] = None) -> List[float]:
    """Extract a tail of ``value`` floats from a ``{date, value}`` series."""
    vals: List[float] = []
    for r in series or []:
        f = _safe_float(r.get("value") if isinstance(r, dict) else None)
        if f is None:
            continue
        vals.append(f)
    if n is not None and len(vals) > n:
        return vals[-n:]
    return vals


def pct_change_30d(series: List[Dict[str, Any]]) -> Optional[float]:
    """Return percentage change between value[-1] and value[-31].

    Needs at least 31 points (today + 30 prior days). None otherwise.
    """
    vals = values_only(series)
    if len(vals) < 31:
        return None
    base = vals[-31]
    if base == 0:
        return None
    return float((vals[-1] - base) / base * 100.0)


def mean(values: List[float]) -> Optional[float]:
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return None
    return float(sum(cleaned) / len(cleaned))


# --- Perp (funding rate, L/S ratio) extractors ----------------------------

# Per-asset key in ``data/market.json`` written by ``fetch_market.py``.
# SOL is intentionally absent in V1 (OKX universe is BTC/ETH/LINK/LTC).
_MARKET_ASSET_KEY = {
    "BTC": "btc",
    "ETH": "eth",
}


def _series_floats(series: Any, value_key: str) -> List[float]:
    """Extract a list of floats from a ``[{date, <value_key>}, ...]`` series.

    Tolerant of missing / malformed rows. Empty list on bad input. Never
    raises.
    """
    if not isinstance(series, list):
        return []
    out: List[float] = []
    for r in series:
        if not isinstance(r, dict):
            continue
        f = _safe_float(r.get(value_key))
        if f is None:
            continue
        out.append(f)
    return out


def funding_rate_metrics(
    market_payload: Dict[str, Any],
    symbol: str,
) -> Dict[str, Optional[float]]:
    """Derive funding-rate metrics for ``symbol`` from a market payload.

    ``market_payload`` is the dict from :func:`load_market_payload`.
    ``fetch_market.py:okx_funding`` returns a daily series of
    ``{"date", "rate"}`` rows where ``rate`` is the **decimal** per-8h
    funding rate (e.g. ``0.0001`` for 1 bp / 8h). The Thesis pillar
    works in **percent per 8h**, so we multiply by 100 before returning.

    Returns::

        {
            "latest_pct_8h":    float | None,
            "mean_30d_pct_8h":  float | None,
        }

    Both fields are ``None`` when the underlying series is missing,
    empty, or the asset has no perp coverage in V1 (e.g. SOL).
    """
    asset_key = _MARKET_ASSET_KEY.get((symbol or "").upper())
    if not asset_key:
        return {"latest_pct_8h": None, "mean_30d_pct_8h": None}
    block = market_payload.get(asset_key) if isinstance(market_payload, dict) else None
    if not isinstance(block, dict):
        return {"latest_pct_8h": None, "mean_30d_pct_8h": None}
    rates = _series_floats(block.get("funding"), "rate")
    if not rates:
        return {"latest_pct_8h": None, "mean_30d_pct_8h": None}
    # Convert decimal -> percent.
    pct_rates = [r * 100.0 for r in rates]
    latest = pct_rates[-1]
    window = pct_rates[-30:] if len(pct_rates) >= 30 else pct_rates
    mean_30d = mean(window)
    return {
        "latest_pct_8h": float(latest),
        "mean_30d_pct_8h": float(mean_30d) if mean_30d is not None else None,
    }


def long_short_ratio_metrics(
    market_payload: Dict[str, Any],
    symbol: str,
) -> Dict[str, Optional[float]]:
    """Derive L/S-ratio metrics for ``symbol`` from a market payload.

    ``fetch_market.py:okx_long_short`` returns ``[{"date", "ratio"}]``
    where ``ratio`` is the top-trader long/short account ratio (1.0 =
    balanced; >1 = more crowded longs; <1 = more crowded shorts). The
    Thesis pillar consumes the latest ratio directly; we also surface a
    30d mean for trend / variable_detail.

    Returns::

        {
            "latest":     float | None,
            "mean_30d":   float | None,
        }

    Both fields are ``None`` when the underlying series is missing or
    the asset has no perp coverage in V1 (e.g. SOL).
    """
    asset_key = _MARKET_ASSET_KEY.get((symbol or "").upper())
    if not asset_key:
        return {"latest": None, "mean_30d": None}
    block = market_payload.get(asset_key) if isinstance(market_payload, dict) else None
    if not isinstance(block, dict):
        return {"latest": None, "mean_30d": None}
    ratios = _series_floats(block.get("long_short_ratio"), "ratio")
    # Strip non-positive ratios (the pillar treats them as missing anyway).
    ratios = [r for r in ratios if r > 0]
    if not ratios:
        return {"latest": None, "mean_30d": None}
    latest = ratios[-1]
    window = ratios[-30:] if len(ratios) >= 30 else ratios
    mean_30d = mean(window)
    return {
        "latest": float(latest),
        "mean_30d": float(mean_30d) if mean_30d is not None else None,
    }


# --- High-level adapter ---------------------------------------------------

class CryptoDataAdapter:
    """One-stop loader that returns a single ``CryptoInputs``-shaped dict
    per asset, suitable for passing into the crypto pillars.

    Heavy work (HTTP fetches, file reads) is performed lazily and
    memoized per-instance. Construct one ``CryptoDataAdapter`` per run.
    """

    def __init__(
        self,
        *,
        data_dir: Optional[Path] = None,
        offline: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else (_repo_root() / "data")
        self.offline = bool(offline)
        self._whale: Optional[Dict[str, Any]] = None
        self._etf_flows: Dict[str, List[Dict[str, Any]]] = {}
        self._coingecko: Optional[Dict[str, Dict[str, Any]]] = None
        self._stablecoins: Optional[Dict[str, Any]] = None
        self._bc_charts: Dict[str, List[Dict[str, Any]]] = {}
        self._market: Optional[Dict[str, Any]] = None

    # ----- lazy loaders ---------------------------------------------------

    def whale(self) -> Dict[str, Any]:
        if self._whale is None:
            self._whale = load_whale_payload()
        return self._whale

    def etf_flows(self, symbol: str) -> List[Dict[str, Any]]:
        sym = (symbol or "").upper()
        if sym not in self._etf_flows:
            self._etf_flows[sym] = load_etf_flows(sym, data_dir=self.data_dir)
        return self._etf_flows[sym]

    def coingecko(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        if self._coingecko is None:
            if self.offline:
                self._coingecko = {}
            else:
                self._coingecko = fetch_coingecko_markets(symbols)
        return self._coingecko

    def stablecoins(self) -> Dict[str, Any]:
        if self._stablecoins is None:
            if self.offline:
                self._stablecoins = {"now": None, "delta_30d_pct": None}
            else:
                self._stablecoins = fetch_stablecoin_total()
        return self._stablecoins

    def blockchain_chart(self, metric: str) -> List[Dict[str, Any]]:
        if metric in self._bc_charts:
            return self._bc_charts[metric]
        if self.offline:
            series: List[Dict[str, Any]] = []
        else:
            series = fetch_blockchain_chart(metric)
        self._bc_charts[metric] = series
        return series

    def market_payload(self) -> Dict[str, Any]:
        """Lazy-load ``data/market.json`` (from ``fetch_market.py``).

        Cached for the adapter's lifetime. Offline-safe: the file is
        purely a local read, so we load it regardless of ``offline``.
        Returns ``{}`` when the file is missing or invalid.
        """
        if self._market is None:
            self._market = load_market_payload(self.data_dir / "market.json")
        return self._market

    def funding_rate(self, symbol: str) -> Dict[str, Optional[float]]:
        """Per-asset funding-rate metrics (percent per 8h).

        Returns ``{"latest_pct_8h", "mean_30d_pct_8h"}``. Both fields are
        ``None`` when the underlying series is missing or the asset has
        no perp coverage in V1 (e.g. SOL). Never raises.
        """
        return funding_rate_metrics(self.market_payload(), symbol)

    def long_short_ratio(self, symbol: str) -> Dict[str, Optional[float]]:
        """Per-asset L/S-ratio metrics.

        Returns ``{"latest", "mean_30d"}``. Both fields are ``None`` when
        the underlying series is missing or the asset has no perp
        coverage in V1 (e.g. SOL). Never raises.
        """
        return long_short_ratio_metrics(self.market_payload(), symbol)

    # ----- composite asset input -----------------------------------------

    def inputs_for(self, symbol: str) -> Dict[str, Any]:
        """Build the per-asset input dict consumed by the crypto pillars.

        The dict is intentionally a flat namespace -- each pillar reaches
        in for the fields it needs. Missing fields are simply absent
        (the pillar functions treat absence as "neutral 50").
        """
        sym = (symbol or "").upper()
        asset_key = sym.lower()
        whale = self.whale()
        # Prefer the whale payload's series; fall back to blockchain.info
        # for BTC if whale.json is empty (which it is in fresh checkouts).
        active_addr_series = whale_series(whale, asset_key, "active_addresses")
        hash_rate_series = whale_series(whale, asset_key, "hash_rate")
        tx_vol_series = whale_series(whale, asset_key, "tx_volume_usd")
        miners_rev_series = whale_series(whale, asset_key, "miners_revenue_usd")

        if sym == "BTC":
            if not active_addr_series:
                active_addr_series = self.blockchain_chart("active_addresses")
            if not hash_rate_series:
                hash_rate_series = self.blockchain_chart("hash_rate")
            if not tx_vol_series:
                tx_vol_series = self.blockchain_chart("tx_volume_usd")
            if not miners_rev_series:
                miners_rev_series = self.blockchain_chart("miners_revenue_usd")

        # Whale-cohort distribution (BTC-only at the moment; eth/sol falls
        # back to neutral inside the pillar).
        distribution = whale_distribution(whale) if sym == "BTC" else []

        # Market snapshot from CoinGecko (single shared call across syms).
        # The full universe is requested in one batch on the first call;
        # subsequent calls hit the per-instance memo.
        markets = self.coingecko(sorted(COINGECKO_IDS.keys()))
        market_block = markets.get(sym) or {}

        # ETF flows. SOL returns []. The pillar will fall back to neutral.
        etf_rows = self.etf_flows(sym)

        # Stablecoins (universe-wide; same value for all assets).
        stable_block = self.stablecoins()

        # Perp positioning (funding rate + L/S ratio) for the Thesis pillar.
        # Only BTC/ETH have OKX coverage in V1; SOL falls back to None and the
        # Thesis pillar collapses to the narrative-sentiment placeholder
        # (which itself defaults to neutral 50).
        funding = self.funding_rate(sym)
        ls = self.long_short_ratio(sym)

        return {
            "symbol": sym,
            "active_addresses_series": active_addr_series,
            "hash_rate_series": hash_rate_series,
            "tx_volume_usd_series": tx_vol_series,
            "miners_revenue_usd_series": miners_rev_series,
            "distribution_series": distribution,
            "market": market_block,
            "etf_flow_rows": etf_rows,
            "stablecoins": stable_block,
            # Thesis pillar inputs (per spec §2.4 + §6).
            "funding_rate_pct_8h": funding.get("latest_pct_8h"),
            "funding_rate_30d_mean_pct_8h": funding.get("mean_30d_pct_8h"),
            "long_short_ratio": ls.get("latest"),
            "long_short_ratio_30d_mean": ls.get("mean_30d"),
        }
