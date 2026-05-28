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
    fred_tier2,
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
        "--news-only",
        dest="news_only",
        action="store_true",
        help=(
            "Refresh news-derived inputs only (Finnhub recommendations, "
            "SEC 8-K, Yahoo earnings, sector RSS). Skip FRED, EIA, Google "
            "Trends, SEC 13F, SEC Form 4, SEC EDGAR XBRL fundamentals. "
            "Reuses today's existing sub-scores for Adoption / Institutional / "
            "Financial / DES, recomputes Thesis, re-blends composite, "
            "and re-emits today's snapshot / variable_detail / narratives. "
            "Requires today's snapshot to already exist (run the full pipeline "
            "first). Skips the history append (today's entry is added by "
            "the morning's first full run). Mutually exclusive with "
            "--catch-up and --as-of."
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
        "--candidate-universe",
        dest="candidate_universe",
        default=None,
        help=(
            "Path to a candidate universe JSON file (same schema as "
            "data/lthcs/universe.json). When set, the pipeline reads "
            "this file INSTEAD of the production universe, writes all "
            "outputs under data/lthcs/candidate_run/<calc_date>/, and "
            "force-skips the persist/history stage. Used by "
            "scripts/lthcs_universe_scaletest.py to validate an "
            "expanded universe without touching production snapshots."
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
            # ArgumentParser.error() raises SystemExit; assignment below never runs.
            p.error("--as-of %r is not a real calendar date" % args.as_of)
        else:
            if as_of_dt > date.today():
                p.error(
                    "--as-of %s is in the future (today=%s)"
                    % (args.as_of, date.today().isoformat())
                )
        if args.catch_up:
            p.error("--as-of and --catch-up are mutually exclusive")
        if args.news_only:
            p.error("--as-of and --news-only are mutually exclusive")

    if args.news_only and args.catch_up:
        p.error("--news-only and --catch-up are mutually exclusive")

    # --- Validate --candidate-universe ---
    if args.candidate_universe is not None:
        candidate_path = Path(args.candidate_universe)
        if not candidate_path.is_file():
            p.error(
                "--candidate-universe path does not exist: %s"
                % args.candidate_universe
            )
        if args.news_only:
            p.error("--candidate-universe and --news-only are mutually exclusive")
        if args.catch_up:
            p.error("--candidate-universe and --catch-up are mutually exclusive")
        # Force-skip persistence: candidate runs are dry-runs by design.
        # We don't error if --dry-run wasn't passed; we just promote it.
        args.dry_run = True

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
    # FRED Tier-2 macro snapshot (Brent crude, gasoline crack, ISM PMI
    # proxy via INDPRO, housing starts, UMICH consumer sentiment, U-6
    # unemployment). Sector-scaled refinement (±5 max) layered onto the
    # DES Tier-1 sub-score by stage_4. Cyclical sectors get the full
    # effect, defensive sectors damped. Persisted to
    # data/lthcs/macro/fred_tier2_<date>.json.
    tier2_macro: Optional[Dict[str, Any]] = None
    # Per-ticker sector-RSS aggregate (FDA/EIA/Fed) across pharma /
    # energy / financials. Populated alongside the Thesis supplement
    # cascade so Stage 4 can surface ``has_sector_rss`` in the Thesis
    # pillar's data_quality block. Map of ticker → aggregate dict with
    # ``event_count``, ``event_titles``, ``sectors_matched``.
    sector_rss_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
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
    # Per-ticker Finnhub recommendation signal — the PRIMARY Thesis input
    # since the dead-pillar fix (May 2026). Populated by Stage 2 from
    # finnhub.get_recommendation_trends + parse_recommendation_signal
    # regardless of --skip-thesis (the AV rotation has no historical archive
    # so the supplement-cascade gate left Thesis dead on 88/90 backfilled
    # dates; Finnhub has monthly history with as_of support so it gives real
    # signal across the full window). Stage 4 reads this first, falling back
    # to stored AV sentiment when Finnhub has no coverage.
    recommendation_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Per-ticker Finnhub /news-sentiment signal -- the SECONDARY Thesis
    # base, used to fill the analyst-coverage gap. Populated by Stage 2
    # from finnhub.get_news_sentiment + parse_thesis_signal ONLY for
    # tickers where recommendation_by_ticker has <3 analysts (saves
    # ~85% of news-sentiment calls; primary path already covers ~145/167).
    # Stage 4 cascades reco -> news_sentiment -> stored_av -> neutral.
    # Skipped entirely in --as-of (backfill) mode since /news-sentiment
    # has no historical archive.
    news_sentiment_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Event-driven Thesis refinement signals — populated in Stage 2 across
    # the whole active universe (not gated on _has_fresh_sentiment) so the
    # Stage 4 refinement runs even when a Finnhub base already exists.
    # Maps ticker -> output of sec_8k.event_signal_for_ticker /
    # yahoo_events.summarize_earnings_for_thesis.
    sec_8k_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    yahoo_earnings_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    rotation: Optional[ThesisRotation] = None
    rotation_scored_today: List[str] = field(default_factory=list)
    rotation_failures: List[str] = field(default_factory=list)
    rev_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    gp_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    ocf_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    # Gross-margin fallback rows -- populated for every ticker so the
    # Financial pillar can walk the fallback chain
    # (SalesRevenueGross -> Revenue - CostOfRevenue -> OperatingIncomeLoss)
    # when GrossProfit is missing (P3 audit fix-up, May 2026). Empty
    # list when the ticker has no usable filings for that concept.
    sales_revenue_gross_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    cost_of_revenue_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    operating_income_by_ticker: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
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
    # LLM sentiment SHADOW run (Tier 5 #28, spec docs/lthcs-llm-sentiment-
    # shadow-spec.md). Populated only when LTHCS_LLM_SENTIMENT_ENABLED=1
    # AND --as-of is unset (no historical news reconstruction). Stamped
    # onto variable_detail.components.llm_sentiment_shadow_* in Stage 8;
    # production Thesis sub_score is byte-untouched.
    llm_sentiment_shadow_by_ticker: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    llm_sentiment_shadow_meta: Optional[Dict[str, Any]] = None
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

    # Stage 7.5b — LLM narratives SHADOW (Tier 5 #23, spec docs/lthcs-llm-
    # narratives-spec.md). Populated only when LTHCS_LLM_NARRATIVES_ENABLED=1
    # AND --as-of is unset. Written to data/lthcs/narratives_llm/<date>.json;
    # production narrative_rows (Stage 7 templated) are byte-untouched.
    llm_narrative_shadow_rows: List[Dict[str, Any]] = field(default_factory=list)
    llm_narrative_shadow_meta: Optional[Dict[str, Any]] = None

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


def _load_recent_dated_json(
    state: "PipelineState",
    subdir: str,
    *,
    max_age_days: int = 7,
) -> Optional[Dict[str, Any]]:
    """Find the most-recent ``data/lthcs/<subdir>/<YYYY-MM-DD>.json`` file.

    Used as a fallback for the insider/holdings fetches when today's SEC
    fetch yielded nothing (rate-limit, transient 5xx, or any
    ``--skip-thesis``-era backfill snapshot). The Form 4 conviction window
    is 90d and 13F is quarterly, so a 7-day-stale file is still a
    materially better signal than collapsing the Institutional pillar to
    pure momentum.

    Returns ``{"data": <parsed_json>, "date": "YYYY-MM-DD", "age_days":
    int}`` or ``None`` if no acceptable file exists within
    ``[calc_date - max_age_days, calc_date - 1]``. ``calc_date`` itself is
    never returned (the caller already tried the live fetch for that date).
    """
    if state.persist is None:
        return None
    base = state.persist.data_root / subdir
    if not base.is_dir():
        return None
    try:
        anchor = date.fromisoformat(state.calc_date)
    except (TypeError, ValueError):
        return None
    best_date: Optional[date] = None
    for entry in base.iterdir():
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            d = date.fromisoformat(entry.stem)
        except ValueError:
            continue
        # Strictly before calc_date (calc_date itself was just tried).
        if d >= anchor:
            continue
        age = (anchor - d).days
        if age > max_age_days:
            continue
        if best_date is None or d > best_date:
            best_date = d
    if best_date is None:
        return None
    path = base / ("%s.json" % best_date.isoformat())
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    return {
        "data": payload,
        "date": best_date.isoformat(),
        "age_days": (anchor - best_date).days,
    }


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def stage_1_load_config(state: PipelineState) -> bool:
    candidate_path = getattr(state.args, "candidate_universe", None)
    universe_source = Path(candidate_path) if candidate_path else UNIVERSE_PATH
    try:
        state.universe = json.loads(universe_source.read_text())
        state.weights_config = json.loads(WEIGHTS_PATH.read_text())
        state.sector_weights = json.loads(SECTOR_WEIGHTS_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print("✗ Stage 1: config load failed: %s" % exc)
        return False

    if candidate_path:
        print(
            "  candidate-universe mode: reading %s (production universe untouched)"
            % universe_source
        )

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
        # In candidate-universe mode redirect ALL persist writes to a
        # sandbox dir so production snapshots / history / variable_detail
        # are never touched. The pipeline still wraps everything in a
        # dry-run guard (set by parse_args), but the redirected data_root
        # is belt-and-braces: even a future stage that forgets to honor
        # dry_run cannot pollute production.
        candidate_root = None
        if getattr(state.args, "candidate_universe", None):
            candidate_root = (
                REPO_ROOT
                / "data"
                / "lthcs"
                / "candidate_run"
                / state.calc_date
            )
            candidate_root.mkdir(parents=True, exist_ok=True)
        state.persist = LthcsPersist(data_root=candidate_root)

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

        # Gross-margin XBRL fallback chain (P3 audit fix-up, May 2026):
        # fetch the three alternative concept families so the Financial
        # pillar can derive a margin proxy when ``GrossProfit`` is
        # missing. Each fetch is independently try-wrapped so one bad
        # concept doesn't poison the others. Cache-hit on the same
        # ``companyfacts`` payload makes these cheap (one HTTP call
        # already covers all concepts the company files).
        try:
            srg = sec_edgar.get_sales_revenue_gross_history(sym, **as_of_kw)
            state.sales_revenue_gross_by_ticker[sym] = srg or []
            if srg:
                counts["sec_srg_ok"] = counts.get("sec_srg_ok", 0) + 1
        except Exception:
            state.sales_revenue_gross_by_ticker[sym] = []
        try:
            cor = sec_edgar.get_cost_of_revenue_history(sym, **as_of_kw)
            state.cost_of_revenue_by_ticker[sym] = cor or []
            if cor:
                counts["sec_cor_ok"] = counts.get("sec_cor_ok", 0) + 1
        except Exception:
            state.cost_of_revenue_by_ticker[sym] = []
        try:
            op_inc = sec_edgar.get_operating_income_history(sym, **as_of_kw)
            state.operating_income_by_ticker[sym] = op_inc or []
            if op_inc:
                counts["sec_op_inc_ok"] = counts.get("sec_op_inc_ok", 0) + 1
        except Exception:
            state.operating_income_by_ticker[sym] = []

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

    # FRED Tier-2 macro snapshot (Brent crude, gasoline crack, ISM PMI
    # proxy via INDPRO, housing starts, UMICH consumer sentiment, U-6
    # unemployment).  Wired into DES via stage_4 (sector-scaled ±5
    # refinement). Module always returns a dict — never raises — so a
    # single bad series counts as ``sources_failed`` rather than
    # collapsing the snapshot.  Forwards ``as_of`` for historical
    # backfill (FRED supports as-of on every Tier-2 series).
    try:
        state.tier2_macro = fred_tier2.fetch_tier2_macro_snapshot(**as_of_kw)
        ok = state.tier2_macro.get("data_quality", {}).get("sources_ok", 0)
        counts["fred_tier2_ok"] = ok
    except Exception as exc:
        if state.args.verbose:
            print("  fred_tier2 fetch failed: %s" % exc)
        state.tier2_macro = None
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

    # --- Finnhub analyst-recommendation consensus (PRIMARY Thesis input) ---
    # Runs OUTSIDE the --skip-thesis gate so the backfill orchestrator
    # (which always passes --skip-thesis to avoid burning the AV token on
    # snapshots AV can't reconstruct anyway) still produces a real Thesis
    # signal. The fetch is cache-warm-friendly: get_recommendation_trends
    # uses a 7d FileCache so post-prewarm there's no network cost; misses
    # gracefully degrade (FinnhubAPIKeyMissing => break; rate-limit => break;
    # any other error => continue with next ticker).
    #
    # Output is parked on state.recommendation_by_ticker for Stage 4 to
    # consume as the BASE Thesis signal. The supplement-cascade Step 1
    # below is now a side-effect that only matters when --skip-thesis is
    # off (live mode) and the rotation file isn't already fresh from AV.
    finnhub_keyless = False
    if state.active_tickers:
        for sym in state.active_tickers:
            if finnhub_keyless:
                break
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
            reco_signal = finnhub.parse_recommendation_signal(
                reco_history, **as_of_kw
            )
            state.recommendation_by_ticker[sym] = reco_signal

    # --- Finnhub /news-sentiment (SECONDARY Thesis base, gap-filler) -------
    # Covers the ~22 tickers in our 167-name universe with no Finnhub
    # /stock/recommendation coverage (consumer staples / utilities /
    # industrials the sell side doesn't actively rate). Free-tier
    # universal, native bullish/bearish percentages, 24h cache.
    #
    # Only fetched when /stock/recommendation didn't produce a usable
    # signal (consensus None or total < 3) — saves ~85% of calls and
    # respects the 60 req/min free-tier budget. Skipped entirely in
    # --as-of (backfill) mode: /news-sentiment has no historical archive
    # so a 2026-04-15 backfill would otherwise be polluted with
    # 2026-05-19's news.
    if state.active_tickers and not as_of and not finnhub_keyless:
        news_keyless = False
        for sym in state.active_tickers:
            if news_keyless:
                break
            # Skip when /stock/recommendation already produced a usable
            # primary signal (>=3 analysts with a real consensus).
            reco = state.recommendation_by_ticker.get(sym) or {}
            try:
                tot = int(reco.get("total_analysts") or 0)
            except (TypeError, ValueError):
                tot = 0
            if reco.get("consensus_score") is not None and tot >= 3:
                continue
            try:
                raw = finnhub.get_news_sentiment(sym)
            except finnhub.FinnhubAPIKeyMissing:
                news_keyless = True
                break
            except finnhub.FinnhubRateLimit:
                break
            except Exception:
                continue
            if not raw:
                continue
            sig = finnhub.parse_thesis_signal(raw)
            # Only park signals that actually carry information so Stage 4
            # cleanly falls through to the stored-AV fallback otherwise.
            if (
                sig
                and sig.get("mean_sentiment_score") is not None
                and int(sig.get("article_count") or 0) > 0
            ):
                state.news_sentiment_by_ticker[sym] = sig
        counts["finnhub_news_sentiment_covered"] = len(
            state.news_sentiment_by_ticker
        )

    # --- Event-driven Thesis refinement collection (universe-wide) ---
    # Populates state.sec_8k_by_ticker and state.yahoo_earnings_by_ticker
    # for EVERY active ticker, regardless of whether a fresh sentiment
    # file already exists. Stage 4 needs the per-ticker signal to refine
    # the Finnhub base sub_score even when the supplement-cascade below
    # would have skipped the ticker (because Finnhub already won).
    #
    # Runs OUTSIDE --skip-thesis: backfill mode passes --skip-thesis to
    # bypass Alpha Vantage, but 8-K and Yahoo earnings have historical
    # `as_of` support so refinement still applies across the backfill
    # window. Failures degrade gracefully — any per-ticker exception is
    # swallowed and the ticker simply gets no refinement entry.
    if state.active_tickers:
        for sym in state.active_tickers:
            try:
                sig = sec_8k.event_signal_for_ticker(sym, days=90, **as_of_kw)
            except Exception:
                sig = None
            if sig and int(sig.get("article_count") or 0) > 0:
                state.sec_8k_by_ticker[sym] = sig

        for sym in state.active_tickers:
            try:
                earnings = yahoo_events.get_earnings_dates(sym, limit=4, **as_of_kw)
                sig = yahoo_events.summarize_earnings_for_thesis(earnings)
            except Exception:
                sig = None
            if (
                sig
                and sig.get("mean_sentiment_score") is not None
                and int(sig.get("article_count") or 0) > 0
            ):
                state.yahoo_earnings_by_ticker[sym] = sig

        counts["sec_8k_refinement"] = len(state.sec_8k_by_ticker)
        counts["yahoo_earnings_refinement"] = len(state.yahoo_earnings_by_ticker)

        # --- Sector RSS aggregate (FDA + EIA + Fed) — un-gated -------------
        # Free RSS feeds, no auth. Used by Stage 4 to stamp ``has_sector_rss``
        # on the Thesis pillar's data_quality block for the 36 ticker-keyword-
        # mapped names in pharma / energy / financials.
        #
        # CRITICAL: hoisted out of the --skip-thesis gate below. Same P0
        # pattern that un-gated Form 4 / 13F: before this fix, daily-cron +
        # backfill (both pass --skip-thesis) silently produced
        # ``has_sector_rss=False`` on every ticker even though P4 was
        # supposed to populate it. The supplement-write loop (which writes
        # to data/lthcs/sentiment/) stays inside the --skip-thesis gate
        # below; only the aggregate + state assignment is hoisted.
        try:
            state.sector_rss_by_ticker = sector_rss.aggregate_sector_events(
                state.active_tickers
            )
        except Exception as exc:
            if state.args.verbose:
                print("  sector-RSS aggregate failed: %s" % exc)
            state.sector_rss_by_ticker = {}
        counts["sector_rss_refinement"] = sum(
            1 for ev in state.sector_rss_by_ticker.values()
            if int((ev or {}).get("event_count") or 0) > 0
        )

    # --- SEC Form 4 insider transactions (per-ticker, 90d window) ---
    # Reuses sec_edgar's session/headers + its own 24h submissions cache +
    # 30d XML cache. First-day run is slow (universe-wide backfill);
    # subsequent runs hit cache.
    #
    # CRITICAL: This block intentionally runs OUTSIDE the --skip-thesis
    # gate. Form 4 / 13F are independent of Alpha Vantage sentiment (they
    # feed the Institutional pillar, not Thesis). Before May 18 2026 they
    # were nested inside `if not state.args.skip_thesis` which silently
    # collapsed the Institutional pillar to pure momentum on every cron
    # run that passed --skip-thesis (the data audit's P0 regression).
    if state.active_tickers:
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

        # Fallback to most-recent on-disk snapshot (<=7d stale) when
        # today's fetch yielded nothing. SEC EDGAR rate-limits or transient
        # 5xx errors must not collapse the workhorse pillar to pure momentum;
        # Form 4 conviction windows are 90d so a 7-day-stale signal is
        # still informative.
        if not state.insider_by_ticker:
            fallback = _load_recent_dated_json(
                state, "insider", max_age_days=7
            )
            if fallback:
                state.insider_by_ticker = fallback["data"]
                counts["insider_covered"] = len(state.insider_by_ticker)
                counts["insider_fallback_age_days"] = fallback["age_days"]
                if state.args.verbose:
                    print(
                        "  sec_form4 today empty; using fallback "
                        "insider/%s.json (%d days stale)"
                        % (fallback["date"], fallback["age_days"])
                    )

        # --- SEC 13F institutional holdings (quarterly cadence) ---
        # Aggregates across 21 tracked managers. First-run is slow (~8min
        # cold cache fetching ~600MB of 13F XMLs); cached extractions are
        # tiny (~18MB) and quarter-stable so re-runs within the quarter
        # are near-instant. Also un-gated from --skip-thesis (see Form 4
        # comment above).
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

        if not state.holdings_by_ticker:
            fallback = _load_recent_dated_json(
                state, "holdings", max_age_days=7
            )
            if fallback:
                state.holdings_by_ticker = fallback["data"]
                counts["holdings_covered"] = sum(
                    1 for v in state.holdings_by_ticker.values()
                    if isinstance(v, dict) and v.get("manager_count", 0) > 0
                )
                counts["holdings_fallback_age_days"] = fallback["age_days"]
                if state.args.verbose:
                    print(
                        "  sec_13f today empty; using fallback "
                        "holdings/%s.json (%d days stale)"
                        % (fallback["date"], fallback["age_days"])
                    )

    if not state.args.skip_thesis and state.active_tickers:
        # --- Step 1 (legacy supplement write): Finnhub -> sentiment file ---
        # Stage 4 now reads state.recommendation_by_ticker directly, so the
        # supplement write here is only useful when somebody re-reads the
        # rotation cache out-of-band (browser tab, MCP). Keep it to preserve
        # the audit trail in data/lthcs/sentiment/<TICKER>.json.
        for sym, reco_signal in state.recommendation_by_ticker.items():
            if _has_fresh_sentiment(sym):
                continue
            consensus = reco_signal.get("consensus_score")
            total = reco_signal.get("total_analysts", 0)
            if consensus is None or total < 3:
                continue
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
        # active universe. Sources from ``state.sec_8k_by_ticker``
        # (populated universe-wide earlier) so we don't refetch.
        for sym, sig in state.sec_8k_by_ticker.items():
            if _has_fresh_sentiment(sym):
                continue
            if sig.get("article_count", 0) <= 0:
                continue
            if _write_supplement(sym, sig):
                sec_8k_supplement_count += 1

        # --- Step 2: Yahoo earnings + analyst actions ---
        # Earnings surprise gives concrete sentiment direction; analyst
        # actions are recency-weighted broker signal. Earnings come from
        # ``state.yahoo_earnings_by_ticker``; analyst actions still
        # fetched on-demand (not part of the Stage 4 refinement path).
        for sym in state.active_tickers:
            if _has_fresh_sentiment(sym):
                continue
            sig = state.yahoo_earnings_by_ticker.get(sym)
            if sig and sig.get("mean_sentiment_score") is not None:
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

        # --- Step 4: Sector RSS supplement writes -----------------------
        # The aggregate fetch + ``state.sector_rss_by_ticker`` assignment
        # are hoisted ABOVE the --skip-thesis gate (P0-pattern un-gating).
        # Only the supplement write (data/lthcs/sentiment/<TICKER>.json)
        # stays here, since those files belong to the Thesis sentiment
        # rotation cache.
        for sym, ev in state.sector_rss_by_ticker.items():
            if (ev or {}).get("event_count", 0) <= 0:
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

            # Production path: engagement heuristic populates the Thesis
            # rotation cache via _write_supplement. LLM sentiment is wired
            # below as a SHADOW (separate dir, separate field, never read
            # by Stage 4) per docs/lthcs-llm-sentiment-shadow-spec.md.
            for sym, news_dict in state.ai_news_by_ticker.items():
                if news_dict.get("total_mentions", 0) < 3:
                    continue
                if _has_fresh_sentiment(sym):
                    continue
                sig = ai_news.compute_thesis_signal_from_news(news_dict)
                if _write_supplement(sym, sig):
                    ai_news_supplement_count += 1

        # --- Step 5.5: LLM sentiment SHADOW run (Tier 5 #28) -----------
        # Decoupled from the Thesis rotation cache: writes to
        # data/lthcs/llm_sentiment/ (NOT data/lthcs/sentiment/) and
        # exposes per-ticker results on state for Stage 8 to stamp onto
        # variable_detail.components.llm_sentiment_shadow_*. Production
        # Thesis sub_score is byte-untouched.
        #
        # Skipped in --as-of (backfill) mode because HN Algolia + RSS
        # feeds expose only "now" -- the same constraint that gates the
        # ai_news fetch above. Also a no-op when
        # LTHCS_LLM_SENTIMENT_ENABLED is not "1" (default).
        if not as_of:
            try:
                from lthcs.sources import llm_sentiment as _llm_sent
                if _llm_sent.is_enabled():
                    # Merge inputs per spec §2: ai_news headlines (the
                    # only "now"-shaped source we have without burning
                    # Finnhub headline calls here), capped per ticker
                    # by the module's DEFAULT_MAX_NEWS_ITEMS.
                    shadow_news: Dict[str, List[Dict[str, Any]]] = {}
                    for sym, nd in (state.ai_news_by_ticker or {}).items():
                        if not isinstance(nd, dict):
                            continue
                        items = nd.get("sample_titles_full") or [
                            {"title": t, "source": "ai_news"}
                            for t in (nd.get("sample_titles") or [])
                        ]
                        if items:
                            shadow_news[sym] = items
                    if shadow_news:
                        shadow_out = _llm_sent.score_universe(
                            shadow_news,
                            calc_date=str(state.calc_date),
                        )
                        if shadow_out:
                            state.llm_sentiment_shadow_by_ticker = (
                                shadow_out.get("results") or {}
                            )
                            state.llm_sentiment_shadow_meta = (
                                shadow_out.get("meta") or {}
                            )
                            counts["llm_sentiment_shadow_covered"] = len(
                                state.llm_sentiment_shadow_by_ticker
                            )
                            counts["llm_sentiment_shadow_persisted"] = int(
                                bool(shadow_out.get("persisted"))
                            )
            except Exception as exc:
                if state.args.verbose:
                    print("  LLM sentiment shadow run failed: %s" % exc)

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

        # NOTE: sec_form4 + sec_13f fetches moved above (outside the
        # --skip-thesis gate) so the Institutional pillar always has
        # smart-money inputs. See the un-gated block earlier in this stage.

    # --- Google Trends acceleration (cache-read-only) ---
    # Pulled OUTSIDE the --skip-thesis gate: this is a pure filesystem
    # read of the most recent data/lthcs/trends/<YYYY-Www>.json snapshot
    # written by scripts/lthcs_trends_weekly.py. The Adoption pillar
    # (not Thesis) consumes it, so gating it on --skip-thesis silently
    # left has_trends=False on every snapshot for backfill runs and any
    # --skip-thesis daily run — the exact failure mode the 2026-05-18
    # audit flagged (0/167 has_trends=true across 91 days).
    if state.active_tickers:
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
    counts["sector_rss_supplement"] = sector_rss_supplement_count
    counts["sector_rss_covered"] = sum(
        1 for ev in state.sector_rss_by_ticker.values()
        if isinstance(ev, dict) and ev.get("event_count", 0) > 0
    )

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
        # Thesis: ticker is "available" iff we have any of:
        #   (a) Finnhub /stock/recommendation with >=3 analysts,
        #   (b) Finnhub /news-sentiment with article_count>0,
        #   (c) fresh stored AV-rotation sentiment.
        # The two Finnhub paths run regardless of --skip-thesis so
        # backfills (which always pass --skip-thesis to bypass AV) still
        # produce real signal across the universe. /news-sentiment is
        # only populated in non-backfill mode (it has no as_of archive).
        thesis_available = False
        reco_signal = state.recommendation_by_ticker.get(sym)
        if reco_signal is not None:
            consensus = reco_signal.get("consensus_score")
            total = int(reco_signal.get("total_analysts") or 0)
            if consensus is not None and total >= 3:
                thesis_available = True
        if not thesis_available:
            news_sig = state.news_sentiment_by_ticker.get(sym)
            if news_sig is not None:
                nm = news_sig.get("mean_sentiment_score")
                nc = int(news_sig.get("article_count") or 0)
                if nm is not None and nc > 0:
                    thesis_available = True
        if not thesis_available and state.rotation is not None and not state.args.skip_thesis:
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

    # Sector lookup for Adoption's sector-relative revenue rank (audit fix
    # 2026-05-18). The Adoption pillar applies its own _MIN_SECTOR_COHORT
    # gate; we just hand it the full {ticker: sector} map and let it
    # decide whether to use the sector cohort or fall back to the
    # maturity-stage / universe distribution.
    peer_sectors: Dict[str, str] = {}
    for sym in state.scored_tickers:
        sec = (state.by_ticker.get(sym, {}) or {}).get("sector")
        if isinstance(sec, str) and sec:
            peer_sectors[sym] = sec

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
                sector=sector if isinstance(sector, str) else None,
                peer_sectors=peer_sectors,
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
                # Gross-margin fallback chain inputs (P3 audit fix-up,
                # May 2026). Non-bank standard path uses these to walk
                # SalesRevenueGross / CostOfRevenue / OperatingIncomeLoss
                # when canonical GrossProfit is missing.
                sales_revenue_gross_rows=state.sales_revenue_gross_by_ticker.get(sym, []),
                cost_of_revenue_rows=state.cost_of_revenue_by_ticker.get(sym, []),
                operating_income_rows=state.operating_income_by_ticker.get(sym, []),
            )
        except Exception:
            fin = _neutral_pillar_result(sym, "financial_evolution")

        # Thesis: PRIMARY input is Finnhub analyst-recommendation consensus
        # (populated in Stage 2 regardless of --skip-thesis). Falls back to
        # the AV-rotation sentiment file when Finnhub has no usable signal
        # for this ticker (no analyst coverage, missing API key, rate-limit
        # break partway through the universe). The fallback still respects
        # --skip-thesis so test paths can short-circuit.
        #
        # REFINEMENT layer: 8-K material events + Yahoo earnings surprises
        # are blended into the base sub_score (default w=0.25). Refinement
        # signals are collected universe-wide in Stage 2 so they apply even
        # when Finnhub already produced a base. See
        # ``thesis.compute_thesis_with_refinement`` for the math.
        th = None
        reco_signal = state.recommendation_by_ticker.get(sym)
        sec_8k_sig = state.sec_8k_by_ticker.get(sym)
        yahoo_sig = state.yahoo_earnings_by_ticker.get(sym)
        news_sent_sig = state.news_sentiment_by_ticker.get(sym)

        # Optional stored-sentiment fallback for the refinement helper.
        # Only loaded when neither Finnhub source produced a usable base
        # — saves disk reads in the common case.
        stored_sent = None
        finnhub_usable = False
        if reco_signal is not None:
            consensus = reco_signal.get("consensus_score")
            total = int(reco_signal.get("total_analysts") or 0)
            finnhub_usable = (consensus is not None and total >= 3)
        news_usable = False
        if news_sent_sig is not None:
            nm = news_sent_sig.get("mean_sentiment_score")
            nc = int(news_sent_sig.get("article_count") or 0)
            news_usable = (nm is not None and nc > 0)
        if (
            not finnhub_usable
            and not news_usable
            and not state.args.skip_thesis
            and state.rotation is not None
        ):
            try:
                stored_sent = state.rotation.read_sentiment(sym)
            except Exception:
                stored_sent = None

        try:
            th = thesis.compute_thesis_with_refinement(
                sym,
                reco_signal,
                sec_8k_signal=sec_8k_sig,
                yahoo_earnings_signal=yahoo_sig,
                news_sentiment_signal=news_sent_sig,
                stored_sentiment=stored_sent,
                today=state.calc_date,
            )
        except Exception:
            th = _neutral_thesis(sym)

        try:
            de = des.compute_des(
                ticker=sym,
                sector=sector,
                macro_inputs=state.macro_inputs,
                sector_weights=state.sector_weights,
                tier2_macro=state.tier2_macro,
            )
        except Exception:
            de = _neutral_pillar_result(sym, "des")

        # Stamp per-ticker sector-RSS coverage on the Thesis pillar so
        # variable_detail surfaces it for the ~30 mapped pharma/energy/
        # financials tickers. Always present (False when ticker isn't
        # in a mapped sector OR has 0 events this window).
        ev = state.sector_rss_by_ticker.get(sym) or {}
        ev_count = int(ev.get("event_count") or 0)
        if isinstance(th, dict):
            dq = th.setdefault("data_quality", {})
            dq["has_sector_rss"] = ev_count > 0
            if ev_count > 0:
                dq["sector_rss_event_count"] = ev_count
                sectors_matched = ev.get("sectors_matched") or []
                if sectors_matched:
                    dq["sector_rss_sectors"] = list(sectors_matched)

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

    # Look up prior scores from each ticker's history file BEFORE Stage 8
    # rewrites it with today's entry. Drives drift_{1d,7d,30d,90d} via
    # compute_lthcs_score -> compute_drift. Without this, every drift
    # window was 0.0 universe-wide (Phase 3 hotfix).
    persist = state.persist
    drift_lookups = 0

    for sym in state.scored_tickers:
        entry = state.by_ticker.get(sym, {})
        pillars = state.pillar_results.get(sym) or {}
        subs = {
            name: float((pillars.get(name) or {}).get("sub_score", 50.0))
            for name in score.PILLAR_ORDER
        }
        flags = list(state.data_quality_flags.get(sym, []))

        prior_scores: Optional[Dict[str, Optional[float]]] = None
        if persist is not None:
            try:
                prior_scores = persist.read_prior_scores(sym, state.calc_date)
            except Exception:
                prior_scores = None
            if prior_scores and any(v is not None for v in prior_scores.values()):
                drift_lookups += 1

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
                prior_scores=prior_scores,
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
    if state.args.verbose:
        print(
            "  drift priors loaded for %d/%d tickers"
            % (drift_lookups, len(state.scored_tickers))
        )
    return True


def stage_7_generate_narratives(state: PipelineState) -> bool:
    """Stage 7: always-templated narratives (production source).

    The LLM narratives shadow runs as Stage 7.5b -- see
    :func:`stage_7p5b_llm_narratives_shadow`. Per
    ``docs/lthcs-llm-narratives-spec.md`` §3 the templated path is the
    canonical production source. The LLM path writes to a sibling
    ``data/lthcs/narratives_llm/`` directory and the UI flips between
    them with a localStorage toggle.
    """
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


def stage_7p5b_llm_narratives_shadow(state: PipelineState) -> bool:
    """Stage 7.5b: LLM narratives SHADOW run (Tier 5 #23).

    Decoupled from Stage 7: writes to ``data/lthcs/narratives_llm/``
    (NOT ``data/lthcs/narratives/``) and exposes per-ticker results on
    ``state.llm_narrative_shadow_rows`` for Stage 8 to persist.
    Production templated narratives (Stage 7) are byte-untouched.

    Gated by ``LTHCS_LLM_NARRATIVES_ENABLED=1`` (legacy
    ``LTHCS_NARRATIVES_LLM_ENABLED=1`` honored for one release). Skipped
    in ``--as-of`` (backfill) mode -- the prior-day composite + insider
    history isn't always available for arbitrary backfill dates.
    Failures here never break the daily pipeline; templated narratives
    are the user-visible default anyway.
    """
    state.llm_narrative_shadow_rows = []
    state.llm_narrative_shadow_meta = None

    if getattr(state.args, "as_of", None):
        return True

    try:
        from lthcs import narratives_llm as _nllm
    except Exception as exc:  # pragma: no cover
        print("! Stage 7.5b: narratives_llm import failed (%s); skipping" % exc)
        return True

    if not _nllm.is_enabled():
        return True

    try:
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
        shadow_out = _nllm.score_universe(
            snapshot_rows=state.snapshot_rows,
            variable_detail_by_ticker=variable_detail_by_ticker,
            calc_date=str(state.calc_date),
            insider_by_ticker=state.insider_by_ticker,
            holdings_by_ticker=state.holdings_by_ticker,
            macro_breadth=state.breadth_snapshot,
            # No persist here -- Stage 8 owns the on-disk write via
            # LthcsPersist.write_narratives_llm. We just collect rows.
            persist=False,
        )
    except Exception as exc:
        print("! Stage 7.5b: LLM narratives shadow failed (%s); skipping" % exc)
        return True

    if not shadow_out:
        return True

    results = shadow_out.get("results") or {}
    meta = shadow_out.get("meta") or {}
    state.llm_narrative_shadow_meta = meta

    # Order rows to match the snapshot order so the shadow file aligns
    # with the templated one row-for-row.
    ordered: List[Dict[str, Any]] = []
    for row in state.snapshot_rows:
        sym = row.get("ticker")
        if sym in results:
            rec = dict(results[sym])
            rec.setdefault("calc_date", str(state.calc_date))
            ordered.append(rec)
    state.llm_narrative_shadow_rows = ordered

    fallback_count = int(meta.get("fallback_count") or 0)
    cost = float(meta.get("total_cost_usd") or 0.0)
    cap_hit = bool(meta.get("cost_cap_hit"))
    print(
        "✓ Stage 7.5b: Generated %d LLM shadow narratives "
        "(%d fellback, est=$%.4f%s)"
        % (
            len(ordered),
            fallback_count,
            cost,
            " — cost cap hit" if cap_hit else "",
        )
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
    shadow_by_ticker = state.llm_sentiment_shadow_by_ticker or {}
    for sym, pillars in state.pillar_results.items():
        for pillar_name, result in pillars.items():
            components = dict(result.get("components") or {})
            # Stamp the LLM sentiment SHADOW signal onto the Thesis row
            # (display only -- production sub_score is byte-untouched).
            if pillar_name == "thesis":
                shadow_sig = shadow_by_ticker.get(sym)
                if isinstance(shadow_sig, dict):
                    components["llm_sentiment_shadow_polarity"] = (
                        shadow_sig.get("mean_sentiment_score")
                    )
                    components["llm_sentiment_shadow_label"] = shadow_sig.get("label")
                    components["llm_sentiment_shadow_confidence"] = (
                        shadow_sig.get("polarity_confidence")
                    )
                    components["llm_sentiment_shadow_rationale"] = (
                        shadow_sig.get("rationale")
                    )
                    components["llm_sentiment_shadow_fallback"] = bool(
                        shadow_sig.get("fallback")
                    )
            state.variable_detail_rows.append(
                {
                    "ticker": sym,
                    "pillar": pillar_name,
                    "components": components,
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
        # Tier 5 #23 — LLM narratives SHADOW. Decoupled from the templated
        # path above so a flag-on run writes a sibling file the UI can opt
        # into. Skipped if Stage 7.5b produced no rows (flag off, --as-of
        # backfill, cost-cap hit, or no SDK).
        if state.llm_narrative_shadow_rows:
            try:
                meta = state.llm_narrative_shadow_meta or {}
                persist.write_narratives_llm(
                    state.calc_date,
                    str(meta.get("model") or MODEL_VERSION),
                    state.llm_narrative_shadow_rows,
                    meta=meta,
                    overwrite=state.args.force,
                )
            except Exception as exc:  # pragma: no cover - filesystem edge
                print("! Stage 8: write_narratives_llm failed (%s)" % exc)
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
            if state.tier2_macro is not None:
                (macro_dir / ("fred_tier2_%s.json" % state.calc_date)).write_text(
                    json.dumps(state.tier2_macro, indent=2, sort_keys=True)
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

    # Optional crypto extension. Default OFF; flip LTHCS_CRYPTO_ENABLED=1
    # to score BTC/ETH/SOL via the crypto pillars (Tier 5 #27). Never
    # fails Stage 8 — the equity pipeline is the contract here.
    if os.getenv("LTHCS_CRYPTO_ENABLED") == "1":
        try:
            from scripts.lthcs_crypto_daily import run as run_crypto
            run_crypto([])
        except Exception as exc:
            if state.args.verbose:
                print("  crypto pillar skipped: %s" % exc)
    return True


def stage_9_build_public_manifest(state: PipelineState) -> bool:
    """Refresh the public read-only data manifest (Phase 4 follow-on).

    Emits ``data/lthcs/public/manifest.json`` (discoverable index of the
    JSON files this site already serves under ``/data/lthcs/``) and
    mirrors today's snapshot under the stable filename
    ``public/latest_snapshot.json`` so external consumers can hard-code
    a single URL.

    Never fails the pipeline — the equity write path in Stage 8 is the
    contract here. A bad manifest just means yesterday's stays live.
    """
    if state.args.dry_run:
        print("✓ Stage 9 (dry-run): would have refreshed public manifest")
        return True
    try:
        from scripts.lthcs_build_public_manifest import build_and_write
        data_root = state.persist.data_root if state.persist is not None else None
        manifest_path, snap_path = build_and_write(data_root=data_root)
        print(
            "✓ Stage 9: wrote public manifest (%s%s)"
            % (
                manifest_path.name,
                ", +latest_snapshot.json" if snap_path is not None else "",
            )
        )
    except Exception as exc:
        if state.args.verbose:
            print("  public manifest skipped: %s" % exc)
    return True


def stage_10_build_universe_csv(state: PipelineState) -> bool:
    """Refresh the public bulk-CSV export of the universe (Phase 5 ETA).

    Emits ``data/lthcs/public/universe.csv`` — a flat, spreadsheet-ready
    file with one row per ticker for today's snapshot. Power users want
    this for offline analysis (Excel / pandas / Google Sheets / R).

    Never fails the pipeline — like the public manifest in Stage 9, the
    equity write path in Stage 8 is the contract here. A bad CSV just
    means yesterday's stays live until the next run.
    """
    if state.args.dry_run:
        print("✓ Stage 10 (dry-run): would have refreshed universe.csv")
        return True
    try:
        from scripts.lthcs_export_csv import build_and_write
        data_root = state.persist.data_root if state.persist is not None else None
        csv_path, calc_date = build_and_write(data_root=data_root)
        print(
            "✓ Stage 10: wrote universe CSV (%s, calc_date=%s)"
            % (csv_path.name, calc_date)
        )
    except Exception as exc:
        if state.args.verbose:
            print("  universe CSV skipped: %s" % exc)
    return True


# ---------------------------------------------------------------------------
# News-only refresh path (hourly cadence)
# ---------------------------------------------------------------------------
#
# `--news-only` re-fetches only the news-derived inputs (Finnhub analyst
# recommendations, SEC 8-K material events, Yahoo earnings, sector RSS) and
# re-emits today's snapshot/variable_detail/narratives with a refreshed
# Thesis sub-score and recomputed composite. Adoption / Institutional /
# Financial / DES are read straight from today's existing snapshot — those
# pillars depend on slow data (FRED, EIA, SEC EDGAR XBRL fundamentals, SEC
# 13F, SEC Form 4, Google Trends) that doesn't move hour-to-hour.
#
# Runtime profile: ~30-90 sec for a 167-ticker universe (Finnhub 7d cache
# hits dominate; 8-K + Yahoo earnings fetches are fast). Compared to the
# 3-7 min full pipeline, this is the hourly-cron-friendly path.
#
# Cron interaction:
#   * 23:00 UTC daily cron runs the FULL pipeline (this function is NOT
#     used there). That run writes today's snapshot.
#   * Hourly cron runs this function. If today's snapshot doesn't exist
#     yet (e.g. 00:00 UTC, before the 23:00 daily cron has caught up to
#     the new UTC date), the function exits with a clear error code and
#     no partial writes.

# Narratives are regenerated only when the Thesis sub-score moves by ≥
# this many points hour-over-hour. Keeps the data/lthcs/narratives/*.json
# diff small in normal hours (most hours see no Thesis movement worth
# rewriting prose for) while still capturing meaningful sentiment shifts.
_NEWS_ONLY_NARRATIVE_DELTA_THRESHOLD = 5.0


def _build_news_only_pillar_results(
    state: PipelineState,
    prior_variable_detail_by_ticker: Dict[str, Dict[str, Dict[str, Any]]],
) -> bool:
    """Build pillar_results for the news-only path.

    For each scored ticker, recompute Thesis from the freshly-fetched
    Finnhub / 8-K / Yahoo earnings inputs, and reuse the prior
    variable_detail block verbatim for the other four pillars. This is
    the seam where "only Thesis changes" gets enforced.
    """
    state.pillar_results = {}
    for sym in state.scored_tickers:
        prior_pillars = prior_variable_detail_by_ticker.get(sym, {})
        if not prior_pillars:
            # No prior pillar data for this ticker — skip rather than
            # synthesize neutral results (would otherwise pollute the
            # snapshot with phantom 50s).
            continue

        # Recompute Thesis from fresh inputs.
        reco_signal = state.recommendation_by_ticker.get(sym)
        sec_8k_sig = state.sec_8k_by_ticker.get(sym)
        yahoo_sig = state.yahoo_earnings_by_ticker.get(sym)
        try:
            th = thesis.compute_thesis_with_refinement(
                sym,
                reco_signal,
                sec_8k_signal=sec_8k_sig,
                yahoo_earnings_signal=yahoo_sig,
                stored_sentiment=None,
                today=state.calc_date,
            )
        except Exception:
            th = _neutral_thesis(sym)

        # Stamp per-ticker sector-RSS coverage onto Thesis pillar.
        ev = state.sector_rss_by_ticker.get(sym) or {}
        ev_count = int(ev.get("event_count") or 0)
        if isinstance(th, dict):
            dq = th.setdefault("data_quality", {})
            dq["has_sector_rss"] = ev_count > 0
            if ev_count > 0:
                dq["sector_rss_event_count"] = ev_count
                sectors_matched = ev.get("sectors_matched") or []
                if sectors_matched:
                    dq["sector_rss_sectors"] = list(sectors_matched)

        pillars: Dict[str, Dict[str, Any]] = {}
        for pillar_name in score.PILLAR_ORDER:
            if pillar_name == "thesis_integrity":
                pillars[pillar_name] = th
                continue
            prior = prior_pillars.get(pillar_name)
            if not prior:
                # Missing prior pillar for this ticker — fall back to
                # neutral so compute_lthcs_score still has a value.
                pillars[pillar_name] = _neutral_pillar_result(sym, pillar_name)
                continue
            pillars[pillar_name] = {
                "ticker": sym,
                "sub_score": float(prior.get("sub_score", 50.0)),
                "components": dict(prior.get("components") or {}),
                "data_quality": dict(prior.get("data_quality") or {}),
            }
        state.pillar_results[sym] = pillars
    return True


def run_news_only(args: argparse.Namespace) -> int:
    """Hourly news-only refresh path. See module-level comment block above.

    Returns the exit code (0 = success, 1 = failure, 2 = today's snapshot
    missing). Does NOT touch history files — today's history entry is the
    morning full-run's responsibility.
    """
    print("[news-only] Starting hourly news-derived refresh.")
    state = PipelineState(args=args)
    # Reuse Stage 1 verbatim — it just loads config + decides calc_date +
    # builds the universe list. No network calls.
    if not stage_1_load_config(state):
        return 1

    persist = state.persist
    if persist is None:
        # Stage 1 builds one, but defensive null guard for type-checker.
        persist = LthcsPersist()
        state.persist = persist

    # --- Preflight: today's snapshot must already exist ---
    if not persist.snapshot_exists(state.calc_date):
        print(
            "✗ [news-only] no snapshot exists for %s. "
            "Run the full pipeline first (python lthcs_daily.py --force) "
            "before requesting a news-only refresh." % state.calc_date
        )
        return 2

    # --- Load prior snapshot + variable_detail + narratives ---
    try:
        prior_snapshot = persist.read_snapshot(state.calc_date)
    except Exception as exc:
        print("✗ [news-only] failed to read today's snapshot: %s" % exc)
        return 1
    try:
        prior_variable_detail = json.loads(
            persist.variable_detail_path(state.calc_date).read_text()
        )
    except Exception as exc:
        print("✗ [news-only] failed to read today's variable_detail: %s" % exc)
        return 1
    try:
        prior_narratives = json.loads(
            persist.narratives_path(state.calc_date).read_text()
        )
    except Exception:
        # Narratives are nice-to-have for the gating step; if absent,
        # we regenerate them all. Don't fail the run.
        prior_narratives = {"narratives": []}

    prior_scores_by_ticker = {
        row.get("ticker"): row
        for row in prior_snapshot.get("scores", [])
        if isinstance(row, dict) and row.get("ticker")
    }
    # Build {ticker -> {pillar -> variable_detail_row}}
    prior_variable_detail_by_ticker: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in prior_variable_detail.get("variables", []):
        if not isinstance(row, dict):
            continue
        sym = row.get("ticker")
        pillar = row.get("pillar")
        if not isinstance(sym, str) or not isinstance(pillar, str):
            continue
        prior_variable_detail_by_ticker.setdefault(sym, {})[pillar] = row
    _ = {
        n.get("ticker"): n
        for n in prior_narratives.get("narratives", [])
        if isinstance(n, dict) and n.get("ticker")
    }

    # Only refresh tickers that were already in today's snapshot — keeps
    # the snapshot roster stable across the day.
    state.scored_tickers = [
        sym for sym in state.active_tickers if sym in prior_scores_by_ticker
    ]
    print(
        "[news-only] Stage 1: %d tickers in today's snapshot will be refreshed."
        % len(state.scored_tickers)
    )

    # --- Fetch news-derived inputs ONLY ---
    # No FRED, no EIA, no SEC EDGAR XBRL, no 13F, no Form 4, no trends.
    # No Alpha Vantage either (we want hourly cadence but stay under the
    # 25/day quota; the daily pipeline owns AV rotation).
    state.persist = persist
    finnhub_keyless = False
    n = len(state.scored_tickers)
    n_finnhub = 0
    n_sec_8k = 0
    n_yahoo = 0
    for sym in state.scored_tickers:
        # Finnhub recommendations (PRIMARY Thesis input).
        if not finnhub_keyless:
            try:
                reco_history = finnhub.get_recommendation_trends(sym)
            except finnhub.FinnhubAPIKeyMissing:
                finnhub_keyless = True
                reco_history = None
            except finnhub.FinnhubRateLimit:
                # Stop hammering Finnhub for the rest of this run; reuse
                # nothing rather than risk a per-ticker stutter.
                finnhub_keyless = True
                reco_history = None
            except Exception:
                reco_history = None
            if reco_history:
                try:
                    reco_signal = finnhub.parse_recommendation_signal(reco_history)
                    state.recommendation_by_ticker[sym] = reco_signal
                    n_finnhub += 1
                except Exception:
                    pass
        # SEC 8-K material events.
        try:
            sig = sec_8k.event_signal_for_ticker(sym, days=90)
        except Exception:
            sig = None
        if sig and int(sig.get("article_count") or 0) > 0:
            state.sec_8k_by_ticker[sym] = sig
            n_sec_8k += 1
        # Yahoo earnings.
        try:
            earnings = yahoo_events.get_earnings_dates(sym, limit=4)
            ysig = yahoo_events.summarize_earnings_for_thesis(earnings)
        except Exception:
            ysig = None
        if (
            ysig
            and ysig.get("mean_sentiment_score") is not None
            and int(ysig.get("article_count") or 0) > 0
        ):
            state.yahoo_earnings_by_ticker[sym] = ysig
            n_yahoo += 1

    # Sector RSS (universe-wide; not per-ticker network calls).
    try:
        sector_events = sector_rss.aggregate_sector_events(state.scored_tickers)
    except Exception as exc:
        if args.verbose:
            print("  [news-only] sector_rss fetch failed: %s" % exc)
        sector_events = {}
    state.sector_rss_by_ticker = sector_events
    n_rss = sum(
        1 for ev in sector_events.values()
        if isinstance(ev, dict) and ev.get("event_count", 0) > 0
    )
    print(
        "[news-only] Stage 2: news fetched — finnhub=%d/%d, sec_8k=%d, yahoo_earnings=%d, sector_rss=%d"
        % (n_finnhub, n, n_sec_8k, n_yahoo, n_rss)
    )

    # --- Recompute Thesis + reuse other pillars ---
    _build_news_only_pillar_results(state, prior_variable_detail_by_ticker)
    print(
        "[news-only] Stage 3: rebuilt pillar_results for %d tickers (Thesis recomputed, others reused)"
        % len(state.pillar_results)
    )

    # --- Recompute composite using current weights.json ---
    # Mirrors stage_6_compute_final_scores but pulls macro modifiers, vol
    # modifiers, and dropped-pillar flags from the prior snapshot row so
    # we don't refetch FRED / Yahoo prices just to recompute them.
    #
    # IMPORTANT: We seed with EVERY ticker from today's prior snapshot so
    # the snapshot roster stays stable across the day. The CI workflow
    # never passes --tickers, so in production this is moot, but the
    # smoke-test path with --tickers AAPL,NVDA used to truncate today's
    # snapshot down to 2 rows. Now non-scored tickers pass through
    # untouched (no Thesis refresh, no composite re-blend, just preserved).
    refreshed: Dict[str, Dict[str, Any]] = {}
    thesis_delta_by_ticker: Dict[str, float] = {}
    for sym in state.scored_tickers:
        prior_row = prior_scores_by_ticker.get(sym)
        if not prior_row:
            continue
        entry = state.by_ticker.get(sym, {})
        pillars = state.pillar_results.get(sym) or {}
        subs = {
            name: float((pillars.get(name) or {}).get("sub_score", 50.0))
            for name in score.PILLAR_ORDER
        }
        flags = list(prior_row.get("data_quality_flags") or [])

        # Macro and vol modifiers carry over from the prior row — we
        # intentionally don't refetch FRED 10y or Yahoo vol on the hourly
        # path. Re-blend the composite arithmetically using the same
        # modifiers the daily pipeline already locked in.
        prior_macro = float((prior_row.get("modifiers") or {}).get("macro_adj", 0.0))
        prior_sector_adj = float((prior_row.get("modifiers") or {}).get("sector_adj", 0.0))
        prior_vol = float((prior_row.get("modifiers") or {}).get("volatility_mod", 0.0))

        try:
            row = score.compute_lthcs_score(
                ticker=sym,
                sector=entry.get("sector", ""),
                maturity_stage=entry.get("maturity_stage", DEFAULT_WEIGHTS_PROFILE),
                pillar_subscores=subs,
                weights_config=state.weights_config,
                ten_y_30d_change_bp=None,  # macro_adj is overridden below
                ticker_volatility=None,    # vol modifier is overridden below
                universe_volatilities=None,
                sector_adjustment_override=prior_sector_adj,
                data_quality_flags=flags,
            )
        except Exception as exc:
            if args.verbose:
                print("  [news-only] scoring failed for %s: %s" % (sym, exc))
            continue

        # Substitute the prior-row macro + vol modifiers so the final
        # composite reflects "today's news + yesterday's macro snapshot"
        # rather than a re-derived (and possibly stale-zeroed) version.
        old_macro = float(row["modifiers"]["macro_adj"])
        old_vol = float(row["modifiers"]["volatility_mod"])
        delta = (prior_macro - old_macro) + (prior_vol - old_vol)
        new_score = max(0.0, min(100.0, float(row["lthcs_score"]) + delta))
        new_score = round(new_score, 1)
        row["modifiers"]["macro_adj"] = prior_macro
        row["modifiers"]["volatility_mod"] = prior_vol
        row["lthcs_score"] = new_score
        row["band"] = score.assign_band(
            new_score, state.weights_config.get("score_bands", {})
        )
        # Preserve drift values from prior row — they're 1d/7d/30d/90d
        # vs prior dates, not vs prior hour. Hourly news shouldn't smear
        # the drift columns.
        for k in ("drift_1d", "drift_7d", "drift_30d", "drift_90d"):
            row[k] = prior_row.get(k, 0.0)

        refreshed[sym] = row

        prior_thesis = float(
            (prior_row.get("subscores") or {}).get("thesis_integrity", 50.0)
        )
        new_thesis = float(subs.get("thesis_integrity", 50.0))
        thesis_delta_by_ticker[sym] = new_thesis - prior_thesis

    # Preserve roster order from the prior snapshot; substitute refreshed
    # rows in place, pass through unchanged rows verbatim.
    state.snapshot_rows = [
        refreshed.get(row.get("ticker"), row)
        for row in prior_snapshot.get("scores", [])
        if isinstance(row, dict)
    ]

    print(
        "[news-only] Stage 4: composite recomputed for %d of %d rows; "
        "%d tickers moved Thesis >= %.1f pts"
        % (
            len(refreshed),
            len(state.snapshot_rows),
            sum(1 for d in thesis_delta_by_ticker.values()
                if abs(d) >= _NEWS_ONLY_NARRATIVE_DELTA_THRESHOLD),
            _NEWS_ONLY_NARRATIVE_DELTA_THRESHOLD,
        )
    )

    # --- Variable detail rebuild (cheap; pure compute) ---
    # Same roster-preservation rule as the snapshot: for tickers we did
    # NOT refresh this run, pass through prior variable_detail rows
    # verbatim. Only the refreshed subset gets new pillar rows.
    refreshed_vd: Dict[str, List[Dict[str, Any]]] = {}
    for sym, pillars in state.pillar_results.items():
        rows: List[Dict[str, Any]] = []
        for pillar_name, result in pillars.items():
            rows.append(
                {
                    "ticker": sym,
                    "pillar": pillar_name,
                    "components": dict(result.get("components") or {}),
                    "sub_score": float(result.get("sub_score", 50.0)),
                    "data_quality": dict(result.get("data_quality") or {}),
                }
            )
        refreshed_vd[sym] = rows

    state.variable_detail_rows = []
    seen_refreshed: set = set()
    for row in prior_variable_detail.get("variables", []):
        if not isinstance(row, dict):
            continue
        sym = row.get("ticker")
        if sym in refreshed_vd:
            if sym not in seen_refreshed:
                state.variable_detail_rows.extend(refreshed_vd[sym])
                seen_refreshed.add(sym)
            # Skip the prior row — its refreshed counterpart already went in.
            continue
        state.variable_detail_rows.append(row)

    # --- Narrative regen ONLY for tickers whose Thesis moved >= 5 pts ---
    # Otherwise keep the prior narrative verbatim. Keeps the diff small.
    # We walk the prior narratives order so the on-disk file's order is
    # stable across hourly runs (avoids spurious git churn).
    new_narratives: List[Dict[str, Any]] = []
    refreshed_rows_by_ticker = {
        r.get("ticker"): r for r in state.snapshot_rows if isinstance(r, dict)
    }
    n_regen = 0
    for prior_n in prior_narratives.get("narratives", []):
        if not isinstance(prior_n, dict):
            continue
        sym = prior_n.get("ticker")
        row = refreshed_rows_by_ticker.get(sym)
        delta = abs(thesis_delta_by_ticker.get(sym, 0.0))
        # Reuse prior narrative when (a) the ticker wasn't refreshed this
        # run, or (b) it was refreshed but Thesis barely moved.
        if (
            sym not in refreshed_vd
            or delta < _NEWS_ONLY_NARRATIVE_DELTA_THRESHOLD
        ):
            existing = dict(prior_n)
            if row is not None:
                # Refresh confidence_level cheaply.
                existing["confidence_level"] = row.get(
                    "confidence_level", existing.get("confidence_level")
                )
            new_narratives.append(existing)
            continue
        try:
            narr = narratives.generate_narratives(row)
            new_narratives.append(narr)
            n_regen += 1
        except Exception:
            # Defensive — fall back to prior narrative if regen fails.
            new_narratives.append(dict(prior_n))
    state.narrative_rows = new_narratives
    print(
        "[news-only] Stage 5: %d narratives total (%d regenerated, %d reused from prior)"
        % (len(new_narratives), n_regen, len(new_narratives) - n_regen)
    )

    # --- Persist (snapshot + variable_detail + narratives only) ---
    # Critical: do NOT call rebuild_history_for_all_tickers — the morning's
    # full run already appended today's history entry, and we don't want
    # 24 hourly entries per ticker. Also do not regenerate the LTHCS index
    # (it's a daily/weekly aggregate; consumers of data/lthcs/index/<date>
    # expect the morning composite).
    try:
        persist.write_snapshot(
            state.calc_date,
            MODEL_VERSION,
            DEFAULT_WEIGHTS_PROFILE,
            state.snapshot_rows,
            overwrite=True,  # news-only always overwrites today's snapshot
        )
        persist.write_variable_detail(
            state.calc_date,
            MODEL_VERSION,
            state.variable_detail_rows,
            overwrite=True,
        )
        persist.write_narratives(
            state.calc_date,
            MODEL_VERSION,
            state.narrative_rows,
            overwrite=True,
        )
        persist.rebuild_index(MODEL_VERSION)
    except Exception as exc:
        print("✗ [news-only] persist error: %s" % exc)
        return 1

    print(
        "✓ [news-only] Wrote snapshot (%d rows), variable_detail (%d rows), "
        "narratives (%d rows). History append skipped (owned by daily run)."
        % (
            len(state.snapshot_rows),
            len(state.variable_detail_rows),
            len(state.narrative_rows),
        )
    )
    return 0


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
    stage_7p5b_llm_narratives_shadow,
    stage_8_persist,
    stage_9_build_public_manifest,
    stage_10_build_universe_csv,
]


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if getattr(args, "news_only", False):
        return run_news_only(args)
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
