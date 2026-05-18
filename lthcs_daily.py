"""LTHCS daily pipeline CLI.

One-command runner that orchestrates the five LTHCS pillars + the score
combiner + templated narratives + persistence in a single end-to-end
daily run, per PHASE_1_BUILD_SPEC.md sections 6 and 11.

Stage layout (see :class:`PipelineState`)::

    Stage 1: Load universe + weights config
    Stage 2: Fetch raw data per source (with caching + rate-limit)
    Stage 3: Data quality checks (freshness, nulls, outliers)
    Stage 4: Normalize raw values to 0-100 sub-scores per pillar
    Stage 5: Apply sector + macro + volatility modifiers
    Stage 6: Calculate final LTHCS score, cap [0, 100], assign band + drift
    Stage 7: Generate templated narratives
    Stage 8: Persist snapshot, variable detail, narratives; rebuild history

Each stage prints exactly one line beginning with ``✓`` or ``✗``
so a human glancing at the log can spot the failure point.

Phase 2 note: a ``--stage N`` resume flag is intentionally NOT
implemented in V1. The pipeline is fast enough end-to-end (~minutes per
75-ticker universe with warm caches) that staged resume is overkill;
Phase 2 may add it once we have a snapshot of intermediate state on
disk.

Usage::

    python lthcs_daily.py                       # Full run, all active tickers
    python lthcs_daily.py --tickers AAPL,LCID   # Subset
    python lthcs_daily.py --dry-run             # Compute but don't write
    python lthcs_daily.py --force               # Overwrite today's snapshot
    python lthcs_daily.py --skip-thesis         # Bypass AV (don't burn token)
    python lthcs_daily.py --catch-up            # Forward-fill any missed days
    python lthcs_daily.py --verbose             # Extra stage diagnostics
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Repo-root self-locator so the script works whether invoked from cwd or via
# an absolute path. (The test suite also benefits because importing this
# module never depends on cwd.)
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lthcs import MODEL_VERSION, narratives, score
from lthcs.index_aggregate import compute_lthcs_index
from lthcs.persist import LthcsPersist
from lthcs.pillars import adoption, des, financial, institutional, thesis
from lthcs.sources import (
    ai_news,
    alpha_vantage,
    analyst_breadth,
    breadth_sentiment,
    eia,
    finnhub,
    fred,
    fred_breadth,
    google_trends,
    sec_8k,
    sec_13f,
    sec_edgar,
    sec_form4,
    sector_etf,
    sector_rss,
    yahoo,
    yahoo_events,
)
from lthcs.sources.thesis_rotation import ThesisRotation

# Top ~30 broadly-watched mega-caps that get rotation priority for
# Alpha Vantage news sentiment. These names drive composite-band shifts
# in the published dashboard, and on free-tier AV (~5-25 calls/day) the
# alphabetical default leaves them waiting weeks for refresh.
# Priority names get up to half the daily quota; the rest of the universe
# still rotates through normally.
PRIORITY_THESIS_TICKERS = [
    "AAPL", "MSFT", "NVDA", "GOOG", "GOOGL", "AMZN", "META",
    "BRK.B", "TSLA", "AVGO", "LLY", "JPM", "V", "UNH", "XOM",
    "MA", "COST", "HD", "PG", "JNJ", "ORCL", "NFLX", "KO",
    "MRK", "BAC", "CRM", "CVX", "AMD", "PEP", "WMT", "ABBV",
]


UNIVERSE_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"
WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "weights.json"
SECTOR_WEIGHTS_PATH = REPO_ROOT / "data" / "lthcs" / "sector_des_weights.json"

DEFAULT_WEIGHTS_PROFILE = "standard_compounder"

# Standard band names so Stage 6 always prints a stable column order even
# when the universe happens to contain zero of a band.
_BAND_ORDER_DISPLAY = [
    ("elite", "Elite"),
    ("high_confidence", "High"),
    ("constructive", "Constructive"),
    ("monitor", "Monitor"),
    ("weakening", "Weakening"),
    ("review", "Review"),
]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse the CLI flags. Pure -- no side effects.

    ``--tickers`` accepts a comma-separated list (whitespace tolerated).
    """
    p = argparse.ArgumentParser(
        prog="lthcs_daily",
        description="LTHCS daily pipeline runner (all 5 pillars + score + persist).",
    )
    p.add_argument(
        "--tickers",
        default=None,
        help="Comma-separated ticker subset (default: all active in universe.json).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute everything but skip persistence (no disk writes).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite today's snapshot/narratives/variable_detail if present.",
    )
    p.add_argument(
        "--skip-thesis",
        action="store_true",
        help="Skip Alpha Vantage call entirely (Thesis falls back to neutral 50).",
    )
    p.add_argument(
        "--catch-up",
        action="store_true",
        help=(
            "Forward-fill any missing dates between the last history entry "
            "and today. Each gap day gets a synthetic history entry equal to "
            "the last actual snapshot (marked synthetic=True) so charts have "
            "no visible gaps when the daily cron missed a run. Idempotent."
        ),
    )
    p.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help=(
            "Compute the LTHCS pipeline as if today were the given date "
            "(YYYY-MM-DD). Used by scripts/lthcs_backfill.py for historical "
            "reconstruction. When set: state.calc_date = the as-of date, all "
            "source-module fetches receive as_of=<date>, snapshot is written "
            "to snapshots/<as-of>.json, and history entries are appended for "
            "that date. Mutually exclusive with --catch-up."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Emit extra per-stage diagnostics.",
    )
    args = p.parse_args(argv)

    # --- Validate --as-of (must be YYYY-MM-DD, not in the future) ---
    if args.as_of is not None:
        if not isinstance(args.as_of, str) or not re.match(r"^\d{4}-\d{2}-\d{2}$", args.as_of):
            p.error("--as-of must be a YYYY-MM-DD date string, got %r" % args.as_of)
        try:
            as_of_dt = date.fromisoformat(args.as_of)
        except ValueError:
            p.error("--as-of %r is not a real calendar date" % args.as_of)
        if as_of_dt > date.today():
            p.error(
                "--as-of %s is in the future (today=%s)"
                % (args.as_of, date.today().isoformat())
            )
        if args.catch_up:
            p.error("--as-of and --catch-up are mutually exclusive")

    return args


# ---------------------------------------------------------------------------
# Pipeline state
# ---------------------------------------------------------------------------

@dataclass
class PipelineState:
    """All intermediate data the eight stages mutate in turn.

    Each stage reads what it needs, writes its outputs, and returns
    True/False. The state is the seam tests poke at when they want to
    exercise a single stage in isolation.
    """
    args: argparse.Namespace
    calc_date: str = ""
    persist: Optional[LthcsPersist] = None

    # Stage 1
    universe: Dict[str, Any] = field(default_factory=dict)
    weights_config: Dict[str, Any] = field(default_factory=dict)
    sector_weights: Dict[str, Any] = field(default_factory=dict)
    active_tickers: List[str] = field(default_factory=list)
    by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Stage 2
    macro_inputs: Dict[str, Optional[float]] = field(default_factory=dict)
    # FRED breadth / regime snapshot (HY OAS, IG OAS, 2s10s, broad dollar).
    # Additive market-health layer; downstream scoring will start consuming
    # in a follow-up commit. Persisted to data/lthcs/macro/breadth_<date>.json.
    breadth_snapshot: Optional[Dict[str, Any]] = None
    # Sector ETF (XLK/XLF/XLE/etc. vs SPY) 1m + 3m relative strength.
    # Additive sector-context layer for downstream Adoption / Thesis use.
    # Persisted to data/lthcs/macro/sector_strength_<date>.json.
    sector_strength: Optional[Dict[str, Any]] = None
    # Breadth sentiment regime (CBOE put/call + AAII bull/bear + NAAIM
    # active-manager exposure). Additive market-state layer; persisted to
    # data/lthcs/macro/breadth_sentiment_<date>.json. Downstream scoring
    # consumption deferred to follow-up commit.
    breadth_sentiment_snapshot: Optional[Dict[str, Any]] = None
    # Per-ticker analyst-rating breadth derived from yahoo_events cache
    # (cache-read-only — no extra network calls). Map of ticker → breadth
    # dict (regime, breadth_score in [-1,+1], firm_count, raw_actions).
    # Persisted to data/lthcs/analyst_breadth/<date>.json.
    analyst_breadth_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Per-ticker SEC 13F institutional holdings aggregated across 21
    # tracked managers (BlackRock, Vanguard, State Street, etc.).
    # Quarterly cadence; first-run is slow (~8min cold cache, near-instant
    # warm). Map of ticker → holdings dict (regime, conviction_signal,
    # signal_score, manager_count, top_holders, quarter_over_quarter).
    # Persisted to data/lthcs/holdings/<date>.json.
    holdings_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Per-ticker Google Trends acceleration (read from weekly cache;
    # daily pipeline never calls pytrends — rate limit). Map of ticker
    # → trends dict (acceleration_4w_pct, regime, signal_score).
    # Populated from data/lthcs/trends/<YYYY-Www>.json written by the
    # scripts/lthcs_trends_weekly.py batch.
    trends_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Per-ticker SEC Form 4 insider transactions (open-market only,
    # 10b5-1 / awards / exercises filtered). Map of ticker → insider
    # dict (regime, conviction_score, cluster_buying flag, ceo_cfo_action).
    # Persisted to data/lthcs/insider/<date>.json. 90-day rolling window.
    insider_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    av_response: Optional[Dict[str, Any]] = None       # legacy single-anchor path (kept for backward-compat)
    av_anchor_ticker: Optional[str] = None             # legacy single-anchor path
    rotation: Optional[ThesisRotation] = None
    rotation_scored_today: List[str] = field(default_factory=list)
    rotation_failures: List[str] = field(default_factory=list)
    rev_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    gp_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    ocf_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # Bank-specific concept rows (only populated for tickers in
    # financial.BANK_TICKERS allowlist). Stays empty for non-banks.
    nii_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    pcl_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    noninterest_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    momentum_by_ticker: Dict[str, Optional[float]] = field(default_factory=dict)
    volatility_by_ticker: Dict[str, Optional[float]] = field(default_factory=dict)
    # AI news aggregation — per-ticker mention counts + engagement (free,
    # no rate limit; HN Algolia + TC/VB RSS).
    ai_news_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    fetch_counts: Dict[str, int] = field(default_factory=dict)

    # Stage 3
    data_quality_flags: Dict[str, List[str]] = field(default_factory=dict)
    scored_tickers: List[str] = field(default_factory=list)

    # Stage 4
    pillar_results: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)

    # Stage 6
    snapshot_rows: List[Dict[str, Any]] = field(default_factory=list)

    # Stage 7
    narrative_rows: List[Dict[str, Any]] = field(default_factory=list)

    # Stage 8
    variable_detail_rows: List[Dict[str, Any]] = field(default_factory=list)

    # Stage 7.5 — LTHCS composite "where is the universe?" index. Mirrors
    # the V1 Whale Sentiment Index pattern. Persisted in Stage 8 alongside
    # macro snapshots.
    lthcs_index: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Macro input helpers (copied from scripts/lthcs_check_week5_distribution.py
# because we can't import from scripts/)
# ---------------------------------------------------------------------------

def _yoy_change_pct(series: List[Dict[str, Any]], _days=(350, 380)) -> Optional[float]:
    if not series or len(series) < 13:
        return None
    latest = series[-1]
    if latest.get("value") is None:
        return None
    for prior in reversed(series[:-1]):
        if prior.get("value") is None:
            continue
        try:
            delta = (
                datetime.fromisoformat(latest["date"])
                - datetime.fromisoformat(prior["date"])
            ).days
        except (ValueError, KeyError):
            continue
        if _days[0] <= delta <= _days[1]:
            try:
                return (latest["value"] / prior["value"] - 1.0) * 100.0
            except ZeroDivisionError:
                return None
    return None


def _bp_change_30d(series: List[Dict[str, Any]]) -> Optional[float]:
    if not series or len(series) < 25:
        return None
    latest = series[-1]
    if latest.get("value") is None:
        return None
    for prior in reversed(series[:-1]):
        if prior.get("value") is None:
            continue
        try:
            d = (
                datetime.fromisoformat(latest["date"])
                - datetime.fromisoformat(prior["date"])
            ).days
        except (ValueError, KeyError):
            continue
        if 25 <= d <= 35:
            return (latest["value"] - prior["value"]) * 100.0
    return None


def build_macro_inputs(as_of: Optional[str] = None) -> Dict[str, Optional[float]]:
    """Pull the macro state from FRED + EIA. Any one signal failing returns None.

    Wraps each network call in a try/except so a single source outage
    doesn't crash the whole pipeline -- the DES pillar tolerates None
    inputs (they contribute 0 tilt).

    When ``as_of`` is provided, every FRED/EIA fetch receives ``as_of=...``
    so the macro snapshot reflects what was known at that historical
    date. Sources that don't yet support ``as_of`` (or fail) drop to
    ``None`` per the standard try/except guard.
    """
    fred_kw: Dict[str, Any] = {"as_of": as_of} if as_of else {}
    eia_kw: Dict[str, Any] = {"as_of": as_of} if as_of else {}
    try:
        cpi_series = fred.get_series("CPIAUCSL", **fred_kw)
    except Exception:
        cpi_series = []
    try:
        ten_y_series = fred.get_series("DGS10", **fred_kw)
    except Exception:
        ten_y_series = []
    try:
        ff = fred.get_latest_value("FEDFUNDS", **fred_kw)
    except Exception:
        ff = None
    try:
        unrate = fred.get_latest_value("UNRATE", **fred_kw)
    except Exception:
        unrate = None
    try:
        wti = eia.get_latest_value("wti", **eia_kw)
    except Exception:
        wti = None
    # --- Phase 1.5 expanded macro signals (des-audit-framework HIGH gaps) ---
    # Each wrapped in try/except per the existing pattern: a single FRED
    # outage for one series must not crash the pipeline. Missing values
    # propagate as None -> DES treats them as 0 tilt (no contribution).
    try:
        real_10y_series = fred.get_series("DFII10", **fred_kw)
    except Exception:
        real_10y_series = []
    try:
        vix_series = fred.get_series("VIXCLS", **fred_kw)
    except Exception:
        vix_series = []
    try:
        m2_series = fred.get_series("M2SL", **fred_kw)
    except Exception:
        m2_series = []

    return {
        "cpi_yoy_pct": _yoy_change_pct(cpi_series),
        "fed_funds_pct": ff["value"] if ff else None,
        "ten_y_yield_pct": (
            ten_y_series[-1]["value"] if ten_y_series else None
        ),
        "ten_y_30d_change_bp": _bp_change_30d(ten_y_series),
        "unemployment_pct": unrate["value"] if unrate else None,
        "wti_oil_usd": wti["value"] if wti else None,
        "real_10y_yield_pct": (
            real_10y_series[-1]["value"] if real_10y_series else None
        ),
        "vix_index": (
            vix_series[-1]["value"] if vix_series else None
        ),
        "m2_yoy_pct": _yoy_change_pct(m2_series),
    }


def _empty_av_response() -> Dict[str, Any]:
    """A minimal AV-shaped payload that parse_ticker_sentiment handles."""
    return {"items": "0", "feed": []}


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def stage_1_load_config(state: PipelineState) -> bool:
    try:
        state.universe = json.loads(UNIVERSE_PATH.read_text())
        state.weights_config = json.loads(WEIGHTS_PATH.read_text())
        state.sector_weights = json.loads(SECTOR_WEIGHTS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print("✗ Stage 1: config load failed: %s" % exc)
        return False

    by_ticker: Dict[str, Dict[str, Any]] = {}
    active = []
    for entry in state.universe.get("tickers", []):
        if not isinstance(entry, dict):
            continue
        sym = entry.get("ticker")
        if not isinstance(sym, str):
            continue
        by_ticker[sym] = entry
        if entry.get("active", True):
            active.append(sym)

    subset: Optional[List[str]] = None
    if state.args.tickers:
        subset = [t.strip().upper() for t in state.args.tickers.split(",") if t.strip()]
        # Restrict to those that are present in the universe; warn on any unknowns.
        unknown = [t for t in subset if t not in by_ticker]
        if unknown:
            print(
                "  warning: unknown tickers in --tickers, dropping: %s"
                % ", ".join(unknown)
            )
        subset = [t for t in subset if t in by_ticker]

    if subset is not None:
        state.active_tickers = subset
    else:
        state.active_tickers = active

    state.by_ticker = by_ticker
    # --as-of overrides today's date so historical backfill writes to the
    # right snapshot file and downstream code consumes the requested date.
    as_of_override = getattr(state.args, "as_of", None)
    if as_of_override:
        state.calc_date = as_of_override
    else:
        state.calc_date = date.today().isoformat()
    if state.persist is None:
        state.persist = LthcsPersist()

    profiles = (state.weights_config or {}).get("profiles") or {}
    print(
        "✓ Stage 1: Loaded %d tickers, %d weight profiles"
        % (len(state.active_tickers), len(profiles))
    )
    return True


def stage_2_fetch_data(state: PipelineState) -> bool:
    """Per-ticker source fan-out.

    Each individual fetch is wrapped in try/except so a single bad
    ticker / source outage doesn't take down the whole run -- the
    failure surfaces as missing data, which Stage 3 catches.
    """
    counts = {
        "yahoo_prices_ok": 0,
        "yahoo_momentum_ok": 0,
        "yahoo_vol_ok": 0,
        "sec_rev_ok": 0,
        "sec_gp_ok": 0,
        "sec_ocf_ok": 0,
        "fred_ok": 0,
        "eia_ok": 0,
    }

    # Historical-backfill mode: pass as_of=<date> to every source that
    # supports it; warn-and-skip sources that have no historical archive.
    as_of: Optional[str] = getattr(state.args, "as_of", None)
    as_of_kw: Dict[str, Any] = {"as_of": as_of} if as_of else {}
    if as_of and state.args.verbose:
        print("  --as-of %s active; sources without history will be skipped" % as_of)

    n = len(state.active_tickers)
    for sym in state.active_tickers:
        # Yahoo
        try:
            yahoo.get_daily_prices(sym, **as_of_kw)
            counts["yahoo_prices_ok"] += 1
        except Exception:
            pass
        try:
            mom = yahoo.get_momentum_pct(sym, days=90, **as_of_kw)
            state.momentum_by_ticker[sym] = mom
            if mom is not None:
                counts["yahoo_momentum_ok"] += 1
        except Exception:
            state.momentum_by_ticker[sym] = None
        try:
            vol = yahoo.get_volatility(sym, window=30, **as_of_kw)
            state.volatility_by_ticker[sym] = vol
            if vol is not None:
                counts["yahoo_vol_ok"] += 1
        except Exception:
            state.volatility_by_ticker[sym] = None

        # SEC EDGAR
        try:
            rev = sec_edgar.get_revenue_history(sym, **as_of_kw)
            state.rev_by_ticker[sym] = rev or []
            if rev:
                counts["sec_rev_ok"] += 1
        except Exception:
            state.rev_by_ticker[sym] = []
        try:
            gp = sec_edgar.get_gross_profit_history(sym, **as_of_kw)
            state.gp_by_ticker[sym] = gp or []
            if gp:
                counts["sec_gp_ok"] += 1
        except Exception:
            state.gp_by_ticker[sym] = []
        try:
            ocf = sec_edgar.get_operating_cash_flow_history(sym, **as_of_kw)
            state.ocf_by_ticker[sym] = ocf or []
            if ocf:
                counts["sec_ocf_ok"] += 1
        except Exception:
            state.ocf_by_ticker[sym] = []

        # Bank-specific concepts (only for the strict-bank allowlist).
        # Non-banks skip this fetch entirely to avoid wasted HTTP calls.
        if financial.is_bank_ticker(sym):
            try:
                state.nii_by_ticker[sym] = (
                    sec_edgar.get_net_interest_income_history(sym, **as_of_kw) or []
                )
            except Exception:
                state.nii_by_ticker[sym] = []
            try:
                state.pcl_by_ticker[sym] = (
                    sec_edgar.get_provision_for_credit_losses_history(sym, **as_of_kw) or []
                )
            except Exception:
                state.pcl_by_ticker[sym] = []
            try:
                state.noninterest_by_ticker[sym] = (
                    sec_edgar.get_noninterest_income_history(sym, **as_of_kw) or []
                )
            except Exception:
                state.noninterest_by_ticker[sym] = []

    # Macro (one shot, shared across tickers)
    try:
        state.macro_inputs = build_macro_inputs(as_of=as_of)
        if state.macro_inputs.get("ten_y_yield_pct") is not None:
            counts["fred_ok"] = 1
        if state.macro_inputs.get("wti_oil_usd") is not None:
            counts["eia_ok"] = 1
    except Exception:
        state.macro_inputs = {}

    # FRED breadth / regime snapshot (HY OAS, IG OAS, 2s10s, broad dollar).
    # Additive: persisted today; downstream scoring will consume in a
    # follow-up commit. A full failure must not crash the pipeline.
    try:
        state.breadth_snapshot = fred_breadth.fetch_breadth_snapshot(**as_of_kw)
        ok = state.breadth_snapshot.get("data_quality", {}).get("sources_ok", 0)
        counts["fred_breadth_ok"] = ok
    except Exception as exc:
        if state.args.verbose:
            print("  fred_breadth fetch failed: %s" % exc)
        state.breadth_snapshot = None

    # Sector ETF relative strength (XLK/XLF/XLE/... vs SPY).
    # Additive: persisted today; downstream Adoption / Thesis consumption
    # in a follow-up commit. Skip in --skip-thesis runs to keep test
    # pipelines fast (yfinance can be slow under flaky network).
    if not state.args.skip_thesis:
        try:
            state.sector_strength = sector_etf.fetch_sector_strength(**as_of_kw)
            counts["sector_etf_ok"] = len(state.sector_strength.get("sectors", {}))
        except Exception as exc:
            if state.args.verbose:
                print("  sector_etf fetch failed: %s" % exc)
            state.sector_strength = None
    else:
        state.sector_strength = None

    # Breadth sentiment regime (put/call, AAII, NAAIM). Three independent
    # public sources; module always returns a dict, never raises. We gate
    # downstream usage on data_quality.sources_ok >= 2 in a later commit.
    # In --as-of mode this source has no historical archive (live polls
    # only); skip with a warning rather than write today's reading into a
    # past snapshot.
    if as_of:
        print(
            "  WARNING: skipping breadth_sentiment in backfill mode "
            "(no historical reconstruction available)"
        )
        state.breadth_sentiment_snapshot = None
    else:
        try:
            state.breadth_sentiment_snapshot = breadth_sentiment.fetch_breadth_sentiment()
            ok = state.breadth_sentiment_snapshot.get("data_quality", {}).get("sources_ok", 0)
            counts["breadth_sentiment_ok"] = ok
        except Exception as exc:
            if state.args.verbose:
                print("  breadth_sentiment fetch failed: %s" % exc)
            state.breadth_sentiment_snapshot = None
    # NOTE: analyst_breadth runs AFTER the Thesis cascade (Step 2 warms
    # the yahoo_events recommendations cache it depends on). See Stage 2
    # cascade section below.

    # Alpha Vantage: rotation — score up to 25 least-recently-scored tickers,
    # each via a single-ticker call. Sentiment files live in
    # <data_root>/sentiment/<TICKER>.json, committed to the repo so the
    # browser tab can read them and replays work across days.
    # data_root is taken from state.persist for test isolation.
    persist_root = state.persist.data_root if state.persist is not None else None
    rotation = ThesisRotation(data_root=persist_root, model_version="v1.0.0")
    state.rotation = rotation
    av_status = "skipped"
    # Alpha Vantage free tier has no historical news archive, so backfill
    # mode forces the same fallback as --skip-thesis (Thesis pillar drops
    # to neutral 50, recomposed via renorm in the downstream weights).
    if as_of:
        print(
            "  WARNING: skipping alpha_vantage in backfill mode "
            "(no historical reconstruction available)"
        )
    if not state.args.skip_thesis and not as_of and state.active_tickers:
        try:
            # PRIORITY ROTATION: highest-impact mega-caps (the ~30 most
            # broadly-watched index leaders) jump the alphabetical queue.
            # Without this, AAPL → ABBV → ABNB → ABT → ACN... means top-of-
            # universe names where conviction matters most wait weeks for
            # their alphabetical turn. Priority list is intentionally
            # narrow so the rest of the universe still rotates.
            active_set = set(state.active_tickers)
            priority_due = [
                t for t in PRIORITY_THESIS_TICKERS
                if t in active_set and rotation.is_stale(
                    rotation.read_sentiment(t), today=state.calc_date
                )
            ]
            non_priority = [
                t for t in state.active_tickers if t not in PRIORITY_THESIS_TICKERS
            ]
            # Reserve up to half the daily budget for priority names.
            priority_budget = min(len(priority_due), rotation.DAILY_BUDGET // 2 + 1)
            non_priority_budget = rotation.DAILY_BUDGET - priority_budget

            priority_picks = priority_due[:priority_budget]
            remainder = rotation.select_tickers_for_today(
                non_priority, today=state.calc_date, budget=non_priority_budget
            )
            # De-dup just in case the rotation manager re-picks priority
            # names from the non-priority pool.
            seen = set(priority_picks)
            todays_picks = priority_picks + [t for t in remainder if t not in seen]
        except Exception as exc:
            if state.args.verbose:
                print("  rotation selection failed: %s" % exc)
            todays_picks = []

        scored_now: List[str] = []
        failed_now: List[str] = []
        for pick in todays_picks:
            try:
                # Per-ticker AV call (cached 24h; rate-limited 25/day by the
                # token bucket inside alpha_vantage.py).
                resp = alpha_vantage.get_news_sentiment([pick], limit=50)
                summary = alpha_vantage.parse_ticker_sentiment(resp, pick)
                rotation.write_sentiment(
                    ticker=pick,
                    article_count=summary["article_count"],
                    mean_sentiment_score=summary["mean_sentiment_score"],
                    mean_relevance_score=summary["mean_relevance_score"],
                    label_counts=summary["label_counts"],
                    today=state.calc_date,
                )
                rotation.record_scored(pick, today=state.calc_date)
                scored_now.append(pick)
            except Exception as exc:
                failed_now.append(pick)
                if state.args.verbose:
                    print("  AV %s failed: %s" % (pick, type(exc).__name__))
                # Stop attempting once we hit the rate-limit signal to save
                # whatever budget the bucket may still grant on retry tomorrow.
                if "RateLimit" in type(exc).__name__:
                    break

        state.rotation_scored_today = scored_now
        state.rotation_failures = failed_now
        if scored_now:
            av_status = "%d scored (%d failed)" % (len(scored_now), len(failed_now))
        elif failed_now:
            av_status = "all failed"
        else:
            av_status = "no picks"

    # Supplement priority for tickers without fresh AV sentiment:
    #   1. Finnhub recommendation       (REAL analyst-consensus direction;
    #                                    free for any US-listed name with
    #                                    analyst coverage)
    #   2. SEC 8-K material events     (structured corporate events)
    #   3. Yahoo earnings + analyst    (recent quarter beat/miss; broker
    #                                    upgrades/downgrades)
    #   4. Sector RSS                  (FDA/EIA/Fed RSS for 36 mapped
    #                                    pharma/energy/financials names)
    #   5. AI news engagement           (HN/TC/VB; fallback for AI cohort)
    #
    # Each step writes to the per-ticker sentiment file ONLY if no fresher
    # signal is already there. Steps 2-5 require |sentiment| >= 0.15 to
    # avoid neutral signals replacing the composite-renorm path. Finnhub
    # (step 1) writes unconditionally — analyst consensus is real
    # directional sentiment.
    finnhub_supplement_count = 0
    ai_news_supplement_count = 0
    sec_8k_supplement_count = 0
    yahoo_event_supplement_count = 0
    sector_rss_supplement_count = 0

    def _has_fresh_sentiment(sym: str) -> bool:
        if state.rotation is None:
            return False
        existing = state.rotation.read_sentiment(sym)
        if existing is None:
            return False
        return not state.rotation.is_stale(existing, today=state.calc_date)

    # Sentiment "meaningfulness" threshold: a supplement write must give
    # sentiment that's at least this far from neutral. Otherwise we let
    # the composite-renorm path keep dropping the Thesis pillar — which is
    # the right behavior when the supplement source has no directional
    # signal (routine 8-K items, no recent earnings event, etc.).
    _MIN_MEANINGFUL_SENTIMENT = 0.15

    def _write_supplement(sym: str, sig: Dict[str, Any]) -> bool:
        if state.rotation is None:
            return False
        score_val = sig.get("mean_sentiment_score")
        if score_val is None:
            return False
        if abs(float(score_val)) < _MIN_MEANINGFUL_SENTIMENT:
            return False
        try:
            state.rotation.write_sentiment(
                ticker=sym,
                article_count=sig.get("article_count", 0),
                mean_sentiment_score=score_val,
                mean_relevance_score=sig.get("mean_relevance_score"),
                label_counts=sig.get("label_counts", {}),
                today=state.calc_date,
            )
            state.rotation.record_scored(sym, today=state.calc_date)
            return True
        except Exception:
            return False

    def _write_supplement_unconditional(sym: str, sig: Dict[str, Any]) -> bool:
        """Same as _write_supplement but skips the meaningfulness threshold.
        Used for Finnhub where the score is real sentiment (not engagement
        or routine-event noise) so even a split bullish/bearish reading
        carries information."""
        if state.rotation is None:
            return False
        score_val = sig.get("mean_sentiment_score")
        if score_val is None:
            return False
        try:
            state.rotation.write_sentiment(
                ticker=sym,
                article_count=sig.get("article_count", 0),
                mean_sentiment_score=score_val,
                mean_relevance_score=sig.get("mean_relevance_score"),
                label_counts=sig.get("label_counts", {}),
                today=state.calc_date,
            )
            state.rotation.record_scored(sym, today=state.calc_date)
            return True
        except Exception:
            return False

    if not state.args.skip_thesis and state.active_tickers:
        # --- Step 1: Finnhub analyst-recommendation consensus ---
        # Finnhub's news_sentiment endpoint is PAID-tier-only (HTTP 403 on
        # free), so we use recommendation_trends instead: aggregated
        # analyst buy/hold/sell counts that yield a real directional
        # consensus_score in [-1, +1]. This is genuine sentiment with
        # direction — not engagement-derived — so it slots at the top of
        # the cascade. Available free for any US-listed name covered by
        # at least one analyst.
        finnhub_keyless = False
        for sym in state.active_tickers:
            if finnhub_keyless:
                break
            if _has_fresh_sentiment(sym):
                continue
            try:
                reco_history = finnhub.get_recommendation_trends(sym, **as_of_kw)
            except finnhub.FinnhubAPIKeyMissing:
                finnhub_keyless = True
                break
            except finnhub.FinnhubRateLimit:
                break
            except Exception:
                continue
            if not reco_history:
                continue
            reco_signal = finnhub.parse_recommendation_signal(reco_history)
            consensus = reco_signal.get("consensus_score")
            total = reco_signal.get("total_analysts", 0)
            if consensus is None or total < 3:
                continue
            # Convert consensus_score in [-1, +1] to a Thesis-pillar payload.
            # Label counts split: total_analysts spread across the 3 buckets
            # we map (Bullish from buy_count, Neutral from hold_count,
            # Bearish from sell_count). The intermediate "Somewhat-*"
            # buckets stay at zero — analyst trends don't give that
            # granularity.
            buy_n = reco_signal.get("buy_count", 0)
            hold_n = reco_signal.get("hold_count", 0)
            sell_n = reco_signal.get("sell_count", 0)
            sig = {
                "ticker": sym,
                "article_count": int(total),
                "mean_sentiment_score": float(consensus),
                "mean_relevance_score": 1.0,
                "label_counts": {
                    "Bearish": int(sell_n),
                    "Somewhat-Bearish": 0,
                    "Neutral": int(hold_n),
                    "Somewhat-Bullish": 0,
                    "Bullish": int(buy_n),
                },
                "source": "finnhub_recommendation",
                "last_scored": state.calc_date,
            }
            if _write_supplement_unconditional(sym, sig):
                finnhub_supplement_count += 1

        # --- Step 2: SEC 8-K material events ---
        # Real-time structured events (CEO changes, restatements, material
        # agreements). Highest signal-to-noise; runs across the entire
        # active universe.
        for sym in state.active_tickers:
            if _has_fresh_sentiment(sym):
                continue
            try:
                sig = sec_8k.event_signal_for_ticker(sym, days=90, **as_of_kw)
            except Exception:
                continue
            if sig.get("article_count", 0) <= 0:
                continue
            if _write_supplement(sym, sig):
                sec_8k_supplement_count += 1

        # --- Step 2: Yahoo earnings + analyst actions ---
        # Earnings surprise gives concrete sentiment direction; analyst
        # actions are recency-weighted broker signal.
        for sym in state.active_tickers:
            if _has_fresh_sentiment(sym):
                continue
            try:
                earnings = yahoo_events.get_earnings_dates(sym, limit=4, **as_of_kw)
                sig = yahoo_events.summarize_earnings_for_thesis(earnings)
            except Exception:
                sig = {}
            if sig.get("mean_sentiment_score") is not None:
                if _write_supplement(sym, sig):
                    yahoo_event_supplement_count += 1
                    continue
            # Try analyst actions if no earnings signal.
            try:
                actions = yahoo_events.get_analyst_actions(sym, days=90, **as_of_kw)
                sig = yahoo_events.summarize_analyst_actions_for_thesis(actions)
            except Exception:
                continue
            if sig.get("mean_sentiment_score") is None:
                continue
            if _write_supplement(sym, sig):
                yahoo_event_supplement_count += 1

        # --- Step 4: Sector RSS (FDA + EIA + Fed) ---
        # Free, no auth. Fires for the 36 ticker-keyword-mapped names in
        # pharma / energy / financials. Additive on top of earlier steps.
        try:
            sector_events = sector_rss.aggregate_sector_events(
                state.active_tickers
            )
        except Exception as exc:
            if state.args.verbose:
                print("  sector-RSS fetch failed: %s" % exc)
            sector_events = {}

        for sym, ev in sector_events.items():
            if ev.get("event_count", 0) <= 0:
                continue
            if _has_fresh_sentiment(sym):
                continue
            sig = sector_rss.parse_thesis_signal(ev)
            if _write_supplement(sym, sig):
                sector_rss_supplement_count += 1

        # --- Step 5: AI news engagement (fallback, AI cohort only) ---
        # Skip in --as-of mode: HN Algolia + RSS feeds expose only "now",
        # not the archive that would have been visible on the historical
        # date. We'd otherwise pollute a 2026-04-15 snapshot with 2026-05-17
        # news engagement scores.
        if as_of:
            print(
                "  WARNING: skipping ai_news in backfill mode "
                "(no historical reconstruction available)"
            )
            state.ai_news_by_ticker = {}
        else:
            try:
                state.ai_news_by_ticker = ai_news.aggregate_ai_news(
                    state.active_tickers
                )
            except Exception as exc:
                if state.args.verbose:
                    print("  AI-news fetch failed: %s" % exc)
                state.ai_news_by_ticker = {}

            for sym, news_dict in state.ai_news_by_ticker.items():
                if news_dict.get("total_mentions", 0) < 3:
                    continue
                if _has_fresh_sentiment(sym):
                    continue
                sig = ai_news.compute_thesis_signal_from_news(news_dict)
                if _write_supplement(sym, sig):
                    ai_news_supplement_count += 1

        # --- Analyst-rating breadth (cache-read-only over yahoo_events) ---
        # Runs AFTER the Thesis cascade so the yahoo_events recommendations
        # cache is warm. Pure derivation, no extra network calls. 30d window
        # is the spec default; we can also persist a 90d window if useful.
        try:
            state.analyst_breadth_by_ticker = (
                analyst_breadth.compute_universe_breadth(
                    state.active_tickers, window_days=30
                )
            )
            counts["analyst_breadth_covered"] = len(state.analyst_breadth_by_ticker)
        except Exception as exc:
            if state.args.verbose:
                print("  analyst_breadth compute failed: %s" % exc)
            state.analyst_breadth_by_ticker = {}

        # --- SEC Form 4 insider transactions (per-ticker, 90d window) ---
        # Reuses sec_edgar's session/headers + its own 24h submissions
        # cache + 30d XML cache. First-day run is slow (universe-wide
        # backfill); subsequent runs hit cache. Skippable in test/dry-run
        # paths via --skip-thesis to avoid hammering SEC during dev.
        if not state.args.skip_thesis:
            try:
                state.insider_by_ticker = (
                    sec_form4.fetch_universe_insider_transactions(
                        state.active_tickers, window_days=90, **as_of_kw
                    )
                )
                counts["insider_covered"] = len(state.insider_by_ticker)
            except Exception as exc:
                if state.args.verbose:
                    print("  sec_form4 fetch failed: %s" % exc)
                state.insider_by_ticker = {}

            # --- SEC 13F institutional holdings (quarterly cadence) ---
            # Aggregates across 21 tracked managers. First-run is slow
            # (~8 min cold cache fetching ~600MB of 13F XMLs); cached
            # extractions are tiny (~18MB) and quarter-stable so re-runs
            # within the quarter are near-instant.
            try:
                state.holdings_by_ticker = (
                    sec_13f.fetch_universe_institutional_holdings(
                        state.active_tickers, **as_of_kw
                    )
                )
                counts["holdings_covered"] = sum(
                    1 for v in state.holdings_by_ticker.values()
                    if isinstance(v, dict) and v.get("manager_count", 0) > 0
                )
            except Exception as exc:
                if state.args.verbose:
                    print("  sec_13f fetch failed: %s" % exc)
                state.holdings_by_ticker = {}
        else:
            state.insider_by_ticker = {}
            state.holdings_by_ticker = {}

        # --- Google Trends acceleration (cache-read-only) ---
        # Runs in skip-thesis paths too because there's no network call;
        # we just read the most recent data/lthcs/trends/<YYYY-Www>.json.
        # The weekly batch script populates the cache; if no file exists
        # yet, this returns an empty dict and Adoption falls back to
        # revenue-only (legacy behavior).
        try:
            state.trends_by_ticker = (
                google_trends.get_universe_trends_acceleration(
                    state.active_tickers
                )
            )
            counts["trends_covered"] = len(state.trends_by_ticker)
        except Exception as exc:
            if state.args.verbose:
                print("  google_trends read failed: %s" % exc)
            state.trends_by_ticker = {}

    state.fetch_counts = counts
    counts["finnhub_supplement"] = finnhub_supplement_count
    counts["sec_8k_supplement"] = sec_8k_supplement_count
    counts["yahoo_event_supplement"] = yahoo_event_supplement_count
    counts["ai_news_supplement"] = ai_news_supplement_count

    if state.args.verbose:
        for k, v in counts.items():
            print("  %s: %d" % (k, v))

    coverage = (
        state.rotation.coverage_stats(state.active_tickers, today=state.calc_date)
        if state.rotation
        else None
    )
    coverage_str = (
        ""
        if coverage is None
        else "  (coverage: fresh %d, stale %d, never %d, today %d)"
        % (coverage["fresh"], coverage["stale"], coverage["never_scored"], coverage["scored_today"])
    )

    print(
        "✓ Stage 2: Fetched %d/%d Yahoo, %d/%d SEC EDGAR, %d/1 FRED, %d/1 EIA, AV=%s%s"
        % (
            counts["yahoo_momentum_ok"],
            n,
            counts["sec_rev_ok"],
            n,
            counts["fred_ok"],
            counts["eia_ok"],
            av_status,
            coverage_str,
        )
    )
    return True


def stage_3_quality_checks(state: PipelineState) -> bool:
    flagged = 0
    sufficient = 0
    state.data_quality_flags = {}
    state.scored_tickers = []
    for sym in state.active_tickers:
        flags: List[str] = []
        has_prices = state.momentum_by_ticker.get(sym) is not None
        has_sec = bool(state.rev_by_ticker.get(sym))

        if not has_prices:
            flags.append("yahoo_unavailable")
        if not has_sec:
            flags.append("sec_unavailable")
        # Thesis: ticker is "available" iff we have fresh stored sentiment.
        thesis_available = False
        if state.rotation is not None and not state.args.skip_thesis:
            sentiment = state.rotation.read_sentiment(sym)
            if sentiment is not None and not state.rotation.is_stale(
                sentiment, today=state.calc_date
            ):
                thesis_available = True
        if not thesis_available:
            flags.append("thesis_unavailable")
        if state.volatility_by_ticker.get(sym) is None:
            flags.append("volatility_unavailable")

        state.data_quality_flags[sym] = flags

        # A ticker can still be scored as long as one of Yahoo OR SEC has
        # data; both being empty means every pillar collapses to 50.
        if has_prices or has_sec:
            sufficient += 1
            state.scored_tickers.append(sym)
        if flags:
            flagged += 1

    n = len(state.active_tickers)
    print(
        "✓ Stage 3: Quality passed; %d/%d with sufficient data, %d flagged"
        % (sufficient, n, flagged)
    )
    return True


def stage_4_compute_subscores(state: PipelineState) -> bool:
    # Build peer growth distribution once (every ticker contributes once).
    peer_growths: Dict[str, Optional[float]] = {}
    for sym in state.scored_tickers:
        try:
            peer_growths[sym] = adoption.compute_revenue_growth_yoy(
                state.rev_by_ticker.get(sym, [])
            )
        except Exception:
            peer_growths[sym] = None

    # Maturity-stage-relative peer groups for revenue_growth_yoy percentile.
    # Without this, AAPL's +6% growth gets ranked against LCID's +68% growth
    # and lands at the universe median (~46th percentile) — mechanically
    # under-representing every standard_compounder. Standard compounders
    # should be benchmarked against other standard compounders; pre-profit
    # growth names against each other; recovery names against each other.
    # Fall back to the universe-wide distribution if a maturity stage has
    # fewer than _MIN_MATURITY_PEERS members (too few for percentile).
    _MIN_MATURITY_PEERS = 5
    peer_growths_by_stage: Dict[str, Dict[str, Optional[float]]] = {}
    for sym, g in peer_growths.items():
        stage = (state.by_ticker.get(sym, {}) or {}).get(
            "maturity_stage", "standard_compounder"
        )
        peer_growths_by_stage.setdefault(stage, {})[sym] = g

    def _peer_growths_for(sym: str) -> Dict[str, Optional[float]]:
        stage = (state.by_ticker.get(sym, {}) or {}).get(
            "maturity_stage", "standard_compounder"
        )
        bucket = peer_growths_by_stage.get(stage, {})
        if len(bucket) >= _MIN_MATURITY_PEERS:
            return bucket
        return peer_growths  # fall back to full universe

    state.pillar_results = {}
    for sym in state.scored_tickers:
        entry = state.by_ticker.get(sym, {})
        sector = entry.get("sector", "")
        my_peer_growths = _peer_growths_for(sym)
        try:
            ad = adoption.compute_adoption(
                sym, state.rev_by_ticker.get(sym, []), [], my_peer_growths,
                trends_data=state.trends_by_ticker.get(sym),
                universe_trends_data=state.trends_by_ticker,
            )
        except Exception:
            ad = _neutral_pillar_result(sym, "adoption_momentum")
        try:
            ins = institutional.compute_institutional(
                sym,
                state.momentum_by_ticker.get(sym),
                state.momentum_by_ticker,
                insider_data=state.insider_by_ticker.get(sym),
                holdings_data=state.holdings_by_ticker.get(sym),
            )
        except Exception:
            ins = _neutral_pillar_result(sym, "institutional_confidence")
        try:
            fin = financial.compute_financial(
                sym,
                state.rev_by_ticker.get(sym, []),
                state.gp_by_ticker.get(sym, []),
                state.ocf_by_ticker.get(sym, []),
                my_peer_growths,
                # Bank path: when the ticker is in BANK_TICKERS allowlist and
                # we have its NII/PCL/Noninterest concepts (fetched in Stage
                # 2 for those tickers), compute_financial routes through the
                # bank decomposition instead of GP/OCF. Non-banks pass empty
                # lists and stay on the standard path.
                sector=sector,
                nii_rows=state.nii_by_ticker.get(sym, []),
                pcl_rows=state.pcl_by_ticker.get(sym, []),
                noninterest_rows=state.noninterest_by_ticker.get(sym, []),
                # Bank cohort dicts: pass the universe-wide NII/PCL/Noninterest
                # maps so the bank path can compute percentiles *within the
                # bank cohort* (audit Tier-3 #15 fix). The function filters
                # to BANK_TICKERS allowlist internally; non-banks ignore.
                bank_cohort_nii_rows=state.nii_by_ticker,
                bank_cohort_pcl_rows=state.pcl_by_ticker,
                bank_cohort_noninterest_rows=state.noninterest_by_ticker,
            )
        except Exception:
            fin = _neutral_pillar_result(sym, "financial_evolution")

        # Thesis: read per-ticker stored sentiment (written by the rotation
        # over a 3-day cycle). If the file is missing or stale, the new
        # function returns a neutral 50 with data_quality flags set.
        if not state.args.skip_thesis and state.rotation is not None:
            try:
                sentiment = state.rotation.read_sentiment(sym)
                th = thesis.compute_thesis_from_stored_sentiment(
                    sym, sentiment, today=state.calc_date
                )
            except Exception:
                th = _neutral_thesis(sym)
        else:
            th = _neutral_thesis(sym)

        try:
            de = des.compute_des(
                ticker=sym,
                sector=sector,
                macro_inputs=state.macro_inputs,
                sector_weights=state.sector_weights,
            )
        except Exception:
            de = _neutral_pillar_result(sym, "des")

        state.pillar_results[sym] = {
            "adoption_momentum": ad,
            "institutional_confidence": ins,
            "financial_evolution": fin,
            "thesis_integrity": th,
            "des": de,
        }

    print(
        "✓ Stage 4: Sub-scores computed for %d tickers across 5 pillars"
        % len(state.pillar_results)
    )
    return True


def _neutral_pillar_result(ticker: str, pillar: str) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "sub_score": 50.0,
        "components": {},
        "data_quality": {pillar + "_unavailable": True},
    }


def _neutral_thesis(ticker: str) -> Dict[str, Any]:
    return {
        "ticker": ticker,
        "sub_score": 50.0,
        "components": {
            "article_count": 0,
            "mean_sentiment_score": None,
            "mean_relevance_score": None,
            "label_counts": {},
        },
        "data_quality": {
            "has_sentiment": False,
            "article_count_sufficient": False,
        },
    }


def stage_5_apply_modifiers(state: PipelineState) -> bool:
    """V1: modifiers are applied inside compute_lthcs_score, not here.

    Kept as a discrete stage purely for the eight-line operator log.
    """
    print(
        "✓ Stage 5: Modifiers will be applied in Stage 6 by "
        "compute_lthcs_score (macro, sector stub, volatility)"
    )
    return True


def stage_6_compute_final_scores(state: PipelineState) -> bool:
    peer_vols = [
        v for v in state.volatility_by_ticker.values() if v is not None
    ]
    state.snapshot_rows = []

    for sym in state.scored_tickers:
        entry = state.by_ticker.get(sym, {})
        pillars = state.pillar_results.get(sym) or {}
        subs = {
            name: float((pillars.get(name) or {}).get("sub_score", 50.0))
            for name in score.PILLAR_ORDER
        }
        flags = list(state.data_quality_flags.get(sym, []))
        try:
            row = score.compute_lthcs_score(
                ticker=sym,
                sector=entry.get("sector", ""),
                maturity_stage=entry.get("maturity_stage", DEFAULT_WEIGHTS_PROFILE),
                pillar_subscores=subs,
                weights_config=state.weights_config,
                ten_y_30d_change_bp=state.macro_inputs.get("ten_y_30d_change_bp"),
                ticker_volatility=state.volatility_by_ticker.get(sym),
                universe_volatilities=peer_vols,
                data_quality_flags=flags,
            )
        except Exception as exc:
            print("  warning: scoring failed for %s: %s" % (sym, exc))
            continue
        state.snapshot_rows.append(row)

    # Band distribution
    counts = {b: 0 for b, _ in _BAND_ORDER_DISPLAY}
    for row in state.snapshot_rows:
        b = row.get("band")
        if b in counts:
            counts[b] += 1
    distribution = ", ".join(
        "%s %d" % (label, counts[key]) for key, label in _BAND_ORDER_DISPLAY
    )
    print("✓ Stage 6: Band distribution: %s" % distribution)
    return True


def stage_7_generate_narratives(state: PipelineState) -> bool:
    state.narrative_rows = []
    use_llm = os.getenv("LTHCS_NARRATIVES_LLM_ENABLED") == "1"
    if use_llm:
        # Opt-in LLM path. Falls back per-ticker on any failure so a bad
        # API key / network blip never breaks the daily pipeline.
        try:
            from lthcs.narratives_llm import generate_universe_narratives

            variable_detail_by_ticker: Dict[str, List[Dict[str, Any]]] = {}
            for sym, pillars in state.pillar_results.items():
                variable_detail_by_ticker[sym] = [
                    {
                        "ticker": sym,
                        "pillar": pillar_name,
                        "components": dict(result.get("components") or {}),
                        "sub_score": float(result.get("sub_score", 50.0)),
                        "data_quality": dict(result.get("data_quality") or {}),
                    }
                    for pillar_name, result in pillars.items()
                ]
            llm_results = generate_universe_narratives(
                snapshot_rows=state.snapshot_rows,
                variable_detail_by_ticker=variable_detail_by_ticker,
                insider_by_ticker=state.insider_by_ticker,
                holdings_by_ticker=state.holdings_by_ticker,
                macro_breadth=state.breadth_snapshot,
                model=os.getenv("LTHCS_NARRATIVES_LLM_MODEL", "claude-sonnet-4-5"),
            )
            state.narrative_rows = [
                llm_results[row["ticker"]]
                for row in state.snapshot_rows
                if row.get("ticker") in llm_results
            ]
            fallback_count = sum(1 for n in state.narrative_rows if n.get("fallback"))
            print(
                "✓ Stage 7: Generated %d LLM narratives (%d fell back to template)"
                % (len(state.narrative_rows), fallback_count)
            )
            return True
        except Exception as exc:
            print("! Stage 7 LLM path failed (%s); falling back to template" % exc)
            state.narrative_rows = []

    for row in state.snapshot_rows:
        try:
            narr = narratives.generate_narratives(row)
        except Exception:
            continue
        state.narrative_rows.append(narr)
    print(
        "✓ Stage 7: Generated %d templated narratives"
        % len(state.narrative_rows)
    )
    return True


def stage_7p5_compute_index(state: PipelineState) -> bool:
    """Compute the LTHCS Composite Index (whale-style universe aggregate).

    Pure compute on already-built state; no I/O. Persisted in Stage 8.
    Failures here must not break the daily run.
    """
    try:
        state.lthcs_index = compute_lthcs_index(
            state.snapshot_rows,
            variable_detail_rows=state.variable_detail_rows,
            insider_by_ticker=state.insider_by_ticker,
            holdings_by_ticker=state.holdings_by_ticker,
            breadth_snapshot=state.breadth_snapshot,
            breadth_sentiment_snapshot=state.breadth_sentiment_snapshot,
            sector_strength=state.sector_strength,
            as_of=state.calc_date,
        )
        print(
            "✓ Stage 7.5: LTHCS Composite Index = %+d (%s)"
            % (state.lthcs_index["score"], state.lthcs_index["label"])
        )
    except Exception as exc:
        print("! Stage 7.5: index compute failed: %s" % exc)
        state.lthcs_index = None
    return True


def stage_8_persist(state: PipelineState) -> bool:
    # Build variable_detail rows -- one per (ticker, pillar) for V1.
    state.variable_detail_rows = []
    for sym, pillars in state.pillar_results.items():
        for pillar_name, result in pillars.items():
            state.variable_detail_rows.append(
                {
                    "ticker": sym,
                    "pillar": pillar_name,
                    "components": dict(result.get("components") or {}),
                    "sub_score": float(result.get("sub_score", 50.0)),
                    "data_quality": dict(result.get("data_quality") or {}),
                }
            )

    if state.args.dry_run:
        print(
            "✓ Stage 8 (dry-run): would have written %d snapshots"
            % len(state.snapshot_rows)
        )
        return True

    persist = state.persist
    if persist is None:
        # Defensive: stage 1 builds the persistor, but tests may inject.
        persist = LthcsPersist()
        state.persist = persist

    # Pre-flight existence check so we can fail with a clear hint before
    # write_snapshot raises FileExistsError mid-write.
    if not state.args.force and persist.snapshot_exists(state.calc_date):
        print(
            "✗ Stage 8: snapshot for %s already exists. "
            "Re-run with --force to overwrite." % state.calc_date
        )
        return False

    try:
        persist.write_snapshot(
            state.calc_date,
            MODEL_VERSION,
            DEFAULT_WEIGHTS_PROFILE,
            state.snapshot_rows,
            overwrite=state.args.force,
        )
        persist.write_variable_detail(
            state.calc_date,
            MODEL_VERSION,
            state.variable_detail_rows,
            overwrite=state.args.force,
        )
        persist.write_narratives(
            state.calc_date,
            MODEL_VERSION,
            state.narrative_rows,
            overwrite=state.args.force,
        )
        # Forward-fill any missed days BEFORE writing today's row so the
        # synthetic entries land between the previous real snapshot and
        # today's new one. No-op when no gap exists.
        if state.args.catch_up:
            synthetic_total = persist.fill_history_gaps(today=state.calc_date)
            if state.args.verbose and synthetic_total:
                print("  catch-up filled %d synthetic entries" % synthetic_total)
        history_count = persist.rebuild_history_for_all_tickers(
            state.snapshot_rows, state.calc_date, MODEL_VERSION
        )
        persist.rebuild_index(MODEL_VERSION)
        # Macro / regime snapshots: simple dated JSON dumps. Additive layer;
        # not part of the canonical scored-rows path so failures are logged
        # but don't fail Stage 8.
        macro_dir = persist.data_root / "macro"
        try:
            macro_dir.mkdir(parents=True, exist_ok=True)
            if state.breadth_snapshot is not None:
                (macro_dir / ("breadth_%s.json" % state.calc_date)).write_text(
                    json.dumps(state.breadth_snapshot, indent=2, sort_keys=True)
                )
            if state.sector_strength is not None:
                (macro_dir / ("sector_strength_%s.json" % state.calc_date)).write_text(
                    json.dumps(state.sector_strength, indent=2, sort_keys=True)
                )
            if state.breadth_sentiment_snapshot is not None:
                (macro_dir / ("breadth_sentiment_%s.json" % state.calc_date)).write_text(
                    json.dumps(state.breadth_sentiment_snapshot, indent=2, sort_keys=True)
                )
            # Per-ticker analyst-rating breadth: separate dir to keep the
            # macro/ tree limited to true system-wide signals.
            if state.analyst_breadth_by_ticker:
                analyst_dir = persist.data_root / "analyst_breadth"
                analyst_dir.mkdir(parents=True, exist_ok=True)
                (analyst_dir / ("%s.json" % state.calc_date)).write_text(
                    json.dumps(state.analyst_breadth_by_ticker, indent=2, sort_keys=True)
                )
            # Per-ticker SEC Form 4 insider transactions. Same shape:
            # one daily JSON map keyed by ticker.
            if state.insider_by_ticker:
                insider_dir = persist.data_root / "insider"
                insider_dir.mkdir(parents=True, exist_ok=True)
                (insider_dir / ("%s.json" % state.calc_date)).write_text(
                    json.dumps(state.insider_by_ticker, indent=2, sort_keys=True)
                )
            # Per-ticker 13F institutional holdings (quarterly cadence,
            # daily persist so the dashboard's detail-modal can read).
            if state.holdings_by_ticker:
                holdings_dir = persist.data_root / "holdings"
                holdings_dir.mkdir(parents=True, exist_ok=True)
                (holdings_dir / ("%s.json" % state.calc_date)).write_text(
                    json.dumps(state.holdings_by_ticker, indent=2, sort_keys=True)
                )
            # LTHCS Composite Index (universe-level ±100 read). Persisted
            # under data/lthcs/index/<date>.json so the front-end can
            # fetch it the same way as the daily snapshot.
            if state.lthcs_index is not None:
                index_dir = persist.data_root / "index"
                index_dir.mkdir(parents=True, exist_ok=True)
                (index_dir / ("%s.json" % state.calc_date)).write_text(
                    json.dumps(state.lthcs_index, indent=2, sort_keys=True)
                )
        except Exception as exc:
            if state.args.verbose:
                print("  macro snapshot persist failed: %s" % exc)
    except FileExistsError as exc:
        print(
            "✗ Stage 8: %s — re-run with --force to overwrite." % exc
        )
        return False
    except Exception as exc:
        print("✗ Stage 8: persist error: %s" % exc)
        return False

    print(
        "✓ Stage 8: Wrote snapshot (%d rows), variable_detail (%d rows), "
        "narratives (%d rows); updated %d history files"
        % (
            len(state.snapshot_rows),
            len(state.variable_detail_rows),
            len(state.narrative_rows),
            history_count,
        )
    )
    return True


# Module-level stage list so tests can import + reorder + introspect.
STAGES: List[Callable[[PipelineState], bool]] = [
    stage_1_load_config,
    stage_2_fetch_data,
    stage_3_quality_checks,
    stage_4_compute_subscores,
    stage_5_apply_modifiers,
    stage_6_compute_final_scores,
    stage_7_generate_narratives,
    stage_7p5_compute_index,
    stage_8_persist,
]


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    state = PipelineState(args=args)
    for fn in STAGES:
        if not fn(state):
            # snapshot-exists collision deserves a distinct exit code so
            # cron / wrapping scripts can detect "needs --force" vs other
            # failures.
            if fn is stage_8_persist and state.persist is not None:
                if (
                    not args.force
                    and not args.dry_run
                    and state.persist.snapshot_exists(state.calc_date)
                ):
                    return 2
            return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - thin entry point
    raise SystemExit(main())
