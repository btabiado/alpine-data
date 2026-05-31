"""City Pulse scorer — the math brain of the City tab (Layer A).

PURE Python: no network, no file I/O, stdlib only. This module implements the
*canonical* scoring methodology from ``docs/city/CITY_TAB_BUILD.md`` section 2
and emits objects matching ``docs/city/data-city.schema.json``.

Core principle (from the spec): **each city is scored against its own trailing
history, never against other cities.** A Pulse of 50 means "on this city's own
baseline"; >50 is favorable momentum, <50 is unfavorable. Every feed is a
deviation (z-score) from that city's trailing-12-month baseline, signed by an
editorial *polarity* so that "favorable" always points the same way across
feeds (crime down is good, permits up is good).

Conventions chosen here (documented because the spec lets the implementer pick):

* **Standard deviation: population std (ddof=0).** The baseline is treated as
  the full population of "this city's recent normal", not a sample we want to
  generalize from, so we divide by N (=12), not N-1. ``statistics.pstdev``.
* **Baseline window: the 12 *calendar* months immediately preceding Recent**
  (Recent-1 .. Recent-12), looked up by month key. We require *all twelve* of
  those calendar slots to be present in the series, otherwise the feed is
  ``insufficient_history``. This is the strict, contiguous-trailing-year
  reading of "require >=12 prior months" and it keeps the YoY anchor
  (Recent-12) inside the baseline.
* **YoY headline: Recent vs the month exactly 12 before Recent** (same period
  last year), per the spec's year-over-year framing. ``null`` if that month is
  missing or its value is 0 (can't divide).
* **Glyph thresholds: +-0.15 on the composite C.** C is a mean of z-scores
  (roughly standard-deviation units), so 0.15 sigma is a small-but-real nudge
  off baseline -- below it we call the city "On baseline" (flat) to avoid
  over-reading noise; above it "Improving" (up), below -0.15 "Worsening"
  (down). 0.15 sigma maps to ~+-2.5 Pulse points around 50.

All scored objects are schema-exact: with ``additionalProperties: false`` we
emit only the allowed keys.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone

__all__ = [
    "score_feed",
    "score_pillar",
    "score_city",
    "score_payload",
]

# --- pillar display names (keys are the schema enum) ------------------------
PILLAR_NAMES = {
    "public_safety": "Public Safety",
    "development_economy": "Development & Economy",
    "city_services": "City Services",
}

# --- glyph thresholds on the composite C (std-deviation units) --------------
# C > +GLYPH_BAND  -> improving; |C| <= GLYPH_BAND -> on baseline;
# C < -GLYPH_BAND  -> worsening. See module docstring for the rationale.
GLYPH_BAND = 0.15

METHODOLOGY_VERSION = "1.0"
PILLARS_TOTAL = 3


# ---------------------------------------------------------------------------
# month-key arithmetic (YYYY-MM strings, no calendar lib needed)
# ---------------------------------------------------------------------------
def _parse_month(m):
    """'YYYY-MM' -> integer month-index (year*12 + (month-1))."""
    year_s, mon_s = m.split("-")
    return int(year_s) * 12 + (int(mon_s) - 1)


def _format_month(idx):
    """Inverse of :func:`_parse_month`."""
    year, mon0 = divmod(idx, 12)
    return "{:04d}-{:02d}".format(year, mon0 + 1)


def _month_minus(m, k):
    """Month key ``m`` shifted back ``k`` months."""
    return _format_month(_parse_month(m) - k)


# ---------------------------------------------------------------------------
# per-feed scoring  ->  matches #/definitions/feed
# ---------------------------------------------------------------------------
def score_feed(series, *, polarity, as_of, label, dataset, baseline_months=12,
               z_clip=3, note=None, complete_through=None):
    """Score one feed's monthly series into a schema ``feed`` object.

    Parameters
    ----------
    series : list[dict]
        ``[{"month": "YYYY-MM", "n": int}, ...]`` ascending. Order and
        duplicates are tolerated (we index by month key; last value wins).
    polarity : int
        ``+1`` up-is-favorable, ``-1`` down-is-favorable, ``0`` context-only.
    as_of : str
        Global most-recent-complete month (YYYY-MM). Upper bound on Recent
        unless ``complete_through`` overrides it for this feed.
    label, dataset : str
        Carried straight onto the feed object.
    baseline_months : int
        Trailing baseline length (default 12).
    z_clip : int | float
        z is clipped to +-z_clip (default 3).
    note : str | None
        Editorial note (continuity break, lag, etc.).
    complete_through : str | None
        Latest COMPLETE month for *this* feed (e.g. Chicago crime lags a
        month). Defaults to ``as_of``. Recent is the latest series month
        ``<= complete_through`` -- trailing incomplete months are dropped.

    Returns
    -------
    dict
        Schema-exact feed: always carries ``recent`` / ``recent_period`` /
        ``baseline_mean`` / ``baseline_std`` / ``z`` / ``yoy_pct`` / ``d`` /
        ``complete_through`` / ``note`` (null where not applicable) plus the
        required ``label`` / ``dataset`` / ``polarity`` / ``status``.
    """
    cutoff = complete_through or as_of

    # Build a month -> n map (drop malformed / null rows; last value wins on dup).
    by_month = {}
    for row in series or []:
        m = row.get("month")
        n = row.get("n")
        if m is None or n is None:
            continue
        by_month[m] = n

    # Recent = latest COMPLETE month: the max month key <= cutoff.
    # Anything strictly after cutoff is an incomplete trailing period -> dropped.
    eligible = [m for m in by_month if _parse_month(m) <= _parse_month(cutoff)]

    base = {
        "label": label,
        "dataset": dataset,
        "polarity": polarity,
        "complete_through": cutoff,
        "note": note,
    }

    def _result(status, **extra):
        out = dict(base)
        out["status"] = status
        # Fill every optional numeric/string slot so the shape is stable.
        out.setdefault("recent", None)
        out.setdefault("recent_period", None)
        out.setdefault("baseline_mean", None)
        out.setdefault("baseline_std", None)
        out.setdefault("z", None)
        out.setdefault("yoy_pct", None)
        out.setdefault("d", None)
        out.update(extra)
        return out

    if not eligible:
        # No complete period at all in range -> nothing to score.
        return _result("insufficient_history")

    recent_period = _format_month(max(_parse_month(m) for m in eligible))
    recent = by_month[recent_period]

    # Baseline = the `baseline_months` calendar months immediately preceding
    # Recent (Recent-1 .. Recent-baseline_months). Require ALL of them present.
    baseline_keys = [_month_minus(recent_period, k)
                     for k in range(1, baseline_months + 1)]
    baseline_vals = [by_month[k] for k in baseline_keys if k in by_month]

    if len(baseline_vals) < baseline_months:
        # <12 contiguous prior months -> can't form a trustworthy baseline.
        return _result(
            "insufficient_history",
            recent=recent,
            recent_period=recent_period,
        )

    mu = statistics.fmean(baseline_vals)
    sigma = statistics.pstdev(baseline_vals)  # population std, ddof=0

    note_out = note
    if sigma == 0:
        # Flat baseline: deviation is undefined; treat as exactly on-baseline.
        z = 0.0
        flat_msg = "flat baseline"
        note_out = flat_msg if not note else "{}; {}".format(note, flat_msg)
    else:
        raw_z = (recent - mu) / sigma
        z = max(-z_clip, min(z_clip, raw_z))  # clip to +-z_clip
        z = float(z)

    # YoY: Recent vs same period last year (the month 12 before Recent).
    yoy_anchor = _month_minus(recent_period, 12)
    syl = by_month.get(yoy_anchor)
    if syl is None or syl == 0:
        yoy_pct = None
    else:
        yoy_pct = (recent - syl) / syl * 100.0

    # Directional contribution. Polarity 0 = context-only -> no d (excluded
    # from pillar math), but the feed is still reported with status ok.
    d = None if polarity == 0 else float(polarity) * z

    return _result(
        "ok",
        recent=recent,
        recent_period=recent_period,
        baseline_mean=mu,
        baseline_std=sigma,
        z=z,
        yoy_pct=yoy_pct,
        d=d,
        note=note_out,
    )


# ---------------------------------------------------------------------------
# per-pillar scoring  ->  matches #/definitions/pillar
# ---------------------------------------------------------------------------
def _scored_ds(feed_objs):
    """Directional contributions of feeds that count toward pillar math:
    status == 'ok' AND polarity != 0 AND d is not None."""
    return [f["d"] for f in feed_objs
            if f.get("status") == "ok"
            and f.get("polarity") not in (0, None)
            and f.get("d") is not None]


def score_pillar(key, name, feed_objs):
    """Aggregate already-scored feeds into a schema ``pillar`` object.

    ``score_d`` = mean of the directional contributions ``d`` over feeds that
    are ``ok`` and have polarity != 0. ``None`` if no such feed exists -- the
    pillar is *absent* (coverage honesty: missing is never treated as 0).
    """
    ds = _scored_ds(feed_objs)
    score_d = statistics.fmean(ds) if ds else None
    return {
        "key": key,
        "name": name,
        "score_d": score_d,
        "feeds": list(feed_objs),
    }


# ---------------------------------------------------------------------------
# per-city scoring  ->  pulse matches #/definitions/pulse, plus data_health
# ---------------------------------------------------------------------------
def _glyph_label(c):
    """Map composite C -> (glyph, label) using the +-GLYPH_BAND thresholds."""
    if c is None:
        # No pillars: neutral presentation (score itself is null).
        return "flat", "No data"
    if c > GLYPH_BAND:
        return "up", "Improving"
    if c < -GLYPH_BAND:
        return "down", "Worsening"
    return "flat", "On baseline"


def _composite(pillar_objs, weights):
    """Weighted mean of present (non-null score_d) pillars' score_d.

    Equal weights by default. Returns None if no pillar is present.
    """
    present = [p for p in pillar_objs if p.get("score_d") is not None]
    if not present:
        return None
    num = 0.0
    den = 0.0
    for p in present:
        w = 1.0 if weights is None else float(weights.get(p["key"], 1))
        num += w * p["score_d"]
        den += w
    if den == 0:
        return None
    return num / den


def _pulse_score(c):
    """C (-3..3) -> Pulse 0..100 via 50 + (C/3)*50, clipped. None if C None."""
    if c is None:
        return None
    raw = 50.0 + (c / 3.0) * 50.0
    clipped = max(0.0, min(100.0, raw))
    return int(round(clipped))


def score_city(*, id, name, scope, pillar_objs, disclosures, weights=None):
    """Assemble a city's pillars into a schema-exact city object.

    Returns a dict with ``id`` / ``name`` / ``scope`` / ``pulse`` /
    ``context`` (None in P0) / ``disclosures`` / ``data_health``.

    ``data_health.feeds_ok`` counts feeds with status ``ok`` across all
    pillars; ``feeds_total`` is fixed at 3 (the backbone: one feed per
    pillar). ``last_updated`` is the max feed ``recent_period`` rendered as an
    ISO8601 UTC timestamp (start of that month), or ``generated`` now if no
    feed has scored.
    """
    pillar_objs = list(pillar_objs)
    c = _composite(pillar_objs, weights)
    glyph, label = _glyph_label(c)
    pillars_present = sum(1 for p in pillar_objs if p.get("score_d") is not None)

    pulse = {
        "score": _pulse_score(c),
        "glyph": glyph,
        "label": label,
        "pillars_present": pillars_present,
        "pillars_total": PILLARS_TOTAL,
        "composite_c": c,
        "pillars": pillar_objs,
    }

    # data_health: count ok feeds and find the latest scored period.
    feeds_ok = 0
    latest_period = None
    for p in pillar_objs:
        for f in p.get("feeds", []):
            if f.get("status") == "ok":
                feeds_ok += 1
            rp = f.get("recent_period")
            if rp is not None and (latest_period is None
                                   or _parse_month(rp) > _parse_month(latest_period)):
                latest_period = rp

    if latest_period is not None:
        idx = _parse_month(latest_period)
        year, mon0 = divmod(idx, 12)
        last_updated = datetime(year, mon0 + 1, 1, tzinfo=timezone.utc).isoformat()
    else:
        last_updated = datetime.now(timezone.utc).isoformat()

    data_health = {
        "feeds_ok": feeds_ok,
        "feeds_total": PILLARS_TOTAL,
        "last_updated": last_updated,
    }

    return {
        "id": id,
        "name": name,
        "scope": scope,
        "pulse": pulse,
        "context": None,  # P0: Layer B not built yet.
        "disclosures": list(disclosures),
        "data_health": data_health,
    }


# ---------------------------------------------------------------------------
# top-level payload  ->  matches the schema root
# ---------------------------------------------------------------------------
def score_payload(cities, *, as_of, methodology_disclosures, generated_at=None):
    """Wrap scored city objects into the top-level payload.

    ``generated_at`` defaults to ``datetime.now(timezone.utc)`` ISO8601.
    ``methodology_version`` is pinned to "1.0" (the frozen contract).
    """
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "generated_at": generated_at,
        "methodology_version": METHODOLOGY_VERSION,
        "as_of": as_of,
        "methodology_disclosures": list(methodology_disclosures),
        "cities": list(cities),
    }
