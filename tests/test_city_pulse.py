"""Tests for city.pulse -- the City Pulse scorer (canonical methodology v1.0).

The scorer is the component most likely to be *silently* wrong (a bad formula
produces a plausible-but-misleading civic score), so this suite is exhaustive
about the math: the incomplete-period guard, sigma==0, insufficient history,
both polarities, context-only feeds, coverage honesty (missing != 0), z
clipping, YoY sign/magnitude, the C->Pulse map endpoints, and full schema
conformance against docs/city/data-city.schema.json.

Pure synthetic series; no network/IO. Runs on stdlib + pytest (+ jsonschema if
installed; schema test degrades to a structural check otherwise).
"""
from __future__ import annotations

import json
import os
import sys

import pytest

# Make the repo root importable so `import city.pulse` works regardless of cwd
# (mirrors tests/conftest.py, but self-contained so this file stands alone).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from city import pulse  # noqa: E402

AS_OF = "2026-04"
SCHEMA_PATH = os.path.join(ROOT, "docs", "city", "data-city.schema.json")


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------
def _month_minus(m, k):
    return pulse._month_minus(m, k)


def series_constant(end_month, *, value, length=13):
    """A contiguous monthly series of `length` months ending at `end_month`,
    every n == value. length=13 gives Recent + a full 12-month baseline."""
    out = []
    for k in range(length - 1, -1, -1):
        out.append({"month": _month_minus(end_month, k), "n": value})
    return out


def series_from_baseline(end_month, *, baseline, recent):
    """Build a 13-row series: 12 baseline months (oldest->newest are
    `baseline`, which must have len 12) immediately preceding `end_month`,
    then `end_month` itself = `recent`.

    baseline[0] is Recent-12 (== the YoY anchor); baseline[-1] is Recent-1.
    """
    assert len(baseline) == 12
    rows = []
    for i, val in enumerate(baseline):
        # baseline[0] -> Recent-12, baseline[11] -> Recent-1
        rows.append({"month": _month_minus(end_month, 12 - i), "n": val})
    rows.append({"month": end_month, "n": recent})
    return rows


def varied_baseline(mean=100, spread=10):
    """A 12-month baseline with nonzero variance, centered on `mean`
    (pstdev == `spread`). Use when a test needs a real (non-flat) z so the
    directional contribution d is nonzero -- a flat baseline correctly yields
    z=0 per spec, which would make sign assertions meaningless."""
    return [mean - spread, mean + spread] * 6


def one_feed_city(feed_obj, *, pillar_key="public_safety"):
    """Wrap a single scored feed into a single-pillar city (for end-to-end)."""
    pillar = pulse.score_pillar(pillar_key, pulse.PILLAR_NAMES[pillar_key], [feed_obj])
    return pulse.score_city(
        id="chicago", name="Chicago", scope="city",
        pillar_objs=[pillar], disclosures=[],
    )


# ===========================================================================
# 1. incomplete-period guard
# ===========================================================================
def test_incomplete_period_dropped_recent_is_last_complete():
    """A trailing month > as_of is dropped; Recent = last month <= as_of."""
    s = series_constant(AS_OF, value=100, length=13)
    # Append an incomplete future month (May 2026) that must be ignored.
    s = s + [{"month": "2026-05", "n": 999}]
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF,
                         label="Crimes", dataset="ijzp-q8t2")
    assert f["status"] == "ok"
    assert f["recent_period"] == AS_OF          # not 2026-05
    assert f["recent"] == 100                   # not 999


def test_per_feed_complete_through_overrides_as_of():
    """Chicago crime lags ~a month: complete_through pulls Recent back even
    though later complete-looking months exist in the global window."""
    s = series_constant("2026-04", value=100, length=14)  # through 2026-04
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF,
                         label="Crimes", dataset="ijzp-q8t2",
                         complete_through="2026-03")
    assert f["recent_period"] == "2026-03"
    assert f["complete_through"] == "2026-03"


def test_no_complete_month_in_range_is_insufficient():
    """Series entirely after the cutoff -> nothing scoreable."""
    s = [{"month": "2026-05", "n": 10}, {"month": "2026-06", "n": 11}]
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="Permits",
                         dataset="ydr8-5enu")
    assert f["status"] == "insufficient_history"
    assert f["recent"] is None and f["z"] is None and f["d"] is None


# ===========================================================================
# 2. sigma == 0 (flat baseline)
# ===========================================================================
def test_flat_baseline_gives_zero_z_no_zero_division():
    s = series_constant(AS_OF, value=50, length=13)  # perfectly flat
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="311",
                         dataset="v6vf-nfxy")
    assert f["status"] == "ok"
    assert f["baseline_std"] == 0
    assert f["z"] == 0.0
    assert f["d"] == 0.0
    assert "flat baseline" in (f["note"] or "")


def test_flat_baseline_note_appends_to_existing_note():
    s = series_constant(AS_OF, value=7, length=13)
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="x", dataset="d",
                         note="2019 RMS change")
    assert "2019 RMS change" in f["note"] and "flat baseline" in f["note"]


# ===========================================================================
# 3. insufficient_history (<12 prior months)
# ===========================================================================
def test_insufficient_history_when_fewer_than_12_prior_months():
    # Recent + only 11 prior months = 12 rows total -> baseline short by one.
    s = series_constant(AS_OF, value=100, length=12)
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="x", dataset="d")
    assert f["status"] == "insufficient_history"
    assert f["recent_period"] == AS_OF      # Recent still identified
    assert f["recent"] == 100
    assert f["z"] is None and f["d"] is None and f["yoy_pct"] is None


def test_gap_in_baseline_is_insufficient_history():
    """A hole in the trailing 12 (non-contiguous) -> insufficient_history."""
    s = series_constant(AS_OF, value=100, length=13)
    # Drop one interior baseline month (Recent-5).
    hole = _month_minus(AS_OF, 5)
    s = [row for row in s if row["month"] != hole]
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="x", dataset="d")
    assert f["status"] == "insufficient_history"


def test_exactly_12_prior_months_is_ok():
    s = series_constant(AS_OF, value=100, length=13)  # Recent + 12 baseline
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="x", dataset="d")
    assert f["status"] == "ok"


def test_insufficient_feed_excluded_from_pillar_mean():
    """An insufficient_history feed must NOT drag the pillar toward 0."""
    good = pulse.score_feed(
        series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=115),
        polarity=1, as_of=AS_OF, label="permits", dataset="A")
    bad = pulse.score_feed(
        series_constant(AS_OF, value=100, length=6),  # too short
        polarity=1, as_of=AS_OF, label="licenses", dataset="B")
    assert bad["status"] == "insufficient_history"
    assert good["d"] == pytest.approx(1.5)   # genuinely nonzero
    pillar = pulse.score_pillar("development_economy", "Dev", [good, bad])
    # score_d == good's d alone (bad excluded), NOT (1.5 + 0)/2 == 0.75.
    assert pillar["score_d"] == pytest.approx(1.5)


# ===========================================================================
# 4. polarity -1 favorable (crime)
# ===========================================================================
def test_crime_falling_raises_score_above_50():
    """Crime below its baseline (favorable) -> positive d -> Pulse > 50."""
    # baseline mean 100, pstdev 10; recent 85 -> z = -1.5 -> d = +1.5.
    s = series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=85)
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="Crimes", dataset="C")
    assert f["recent"] < f["baseline_mean"]
    assert f["z"] == pytest.approx(-1.5)
    assert f["d"] == pytest.approx(1.5)   # polarity(-1) * negative z = positive
    city = one_feed_city(f)
    assert city["pulse"]["score"] == 75   # 50 + (1.5/3)*50
    assert city["pulse"]["score"] > 50
    assert city["pulse"]["glyph"] == "up"


def test_crime_rising_drops_score_below_50():
    s = series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=115)  # up
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="Crimes", dataset="C")
    assert f["z"] == pytest.approx(1.5)
    assert f["d"] == pytest.approx(-1.5)
    city = one_feed_city(f)
    assert city["pulse"]["score"] == 25
    assert city["pulse"]["score"] < 50
    assert city["pulse"]["glyph"] == "down"


# ===========================================================================
# 5. polarity +1 (permits)
# ===========================================================================
def test_permits_rising_raises_score_above_50():
    s = series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=115)  # up
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="Permits", dataset="P")
    assert f["z"] == pytest.approx(1.5)
    assert f["d"] == pytest.approx(1.5)
    city = one_feed_city(f, pillar_key="development_economy")
    assert city["pulse"]["score"] == 75
    assert city["pulse"]["score"] > 50


def test_permits_falling_drops_score_below_50():
    s = series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=85)
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="Permits", dataset="P")
    assert f["d"] == pytest.approx(-1.5)
    city = one_feed_city(f, pillar_key="development_economy")
    assert city["pulse"]["score"] == 25
    assert city["pulse"]["score"] < 50


# ===========================================================================
# 6. polarity 0 (context-only) -- reported but excluded from math
# ===========================================================================
def test_polarity_zero_reported_but_excluded_from_d():
    s = series_from_baseline(AS_OF, baseline=[100] * 12, recent=200)
    f = pulse.score_feed(s, polarity=0, as_of=AS_OF, label="311 volume", dataset="V")
    assert f["status"] == "ok"            # still reported
    assert f["z"] is not None             # z still computed for display
    assert f["d"] is None                 # but no directional contribution


def test_polarity_zero_does_not_affect_pillar_or_composite():
    directional = pulse.score_feed(
        series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=85),
        polarity=-1, as_of=AS_OF, label="backlog", dataset="B")
    context = pulse.score_feed(
        series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=130),
        polarity=0, as_of=AS_OF, label="volume", dataset="V")
    assert directional["d"] == pytest.approx(1.5)   # nonzero, so assertion bites
    pillar = pulse.score_pillar("city_services", "City Services",
                                [directional, context])
    # score_d driven solely by the directional feed (context's z excluded).
    assert pillar["score_d"] == pytest.approx(1.5)


def test_pillar_all_context_only_is_absent():
    """A pillar whose only feeds are context-only has no usable signal."""
    c1 = pulse.score_feed(series_constant(AS_OF, value=100, length=13),
                          polarity=0, as_of=AS_OF, label="v1", dataset="A")
    pillar = pulse.score_pillar("city_services", "City Services", [c1])
    assert pillar["score_d"] is None


# ===========================================================================
# 7. coverage honesty -- 2 of 3 pillars present, missing pillar score_d null
# ===========================================================================
def test_two_of_three_pillars_present_missing_is_null_not_zero():
    safety = pulse.score_pillar(
        "public_safety", "Public Safety",
        [pulse.score_feed(series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=80),
                          polarity=-1, as_of=AS_OF, label="Crime", dataset="C")])
    dev = pulse.score_pillar(
        "development_economy", "Dev",
        [pulse.score_feed(series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=115),
                          polarity=1, as_of=AS_OF, label="Permits", dataset="P")])
    # City services: feed not published -> pillar absent.
    services = pulse.score_pillar(
        "city_services", "City Services",
        [{"label": "311", "dataset": "X", "polarity": -1, "status": "not_published",
          "recent": None, "recent_period": None, "baseline_mean": None,
          "baseline_std": None, "z": None, "yoy_pct": None, "d": None,
          "complete_through": None, "note": "Pending feed"}])
    city = pulse.score_city(id="miami", name="Miami", scope="county",
                            pillar_objs=[safety, dev, services], disclosures=[])
    assert city["pulse"]["pillars_present"] == 2
    assert city["pulse"]["pillars_total"] == 3
    assert services["score_d"] is None            # missing, NOT 0
    # Present pillars carry real, distinct, nonzero signal (d=+2 and d=+1.5),
    # so a buggy "treat missing as 0" would visibly move the composite.
    assert safety["score_d"] == pytest.approx(2.0)
    assert dev["score_d"] == pytest.approx(1.5)
    # Composite is the mean of ONLY the two present pillars: (2.0 + 1.5)/2.
    expected_c = (safety["score_d"] + dev["score_d"]) / 2   # 1.75
    assert city["pulse"]["composite_c"] == pytest.approx(expected_c)
    # If the missing pillar were wrongly counted as 0, C would be 1.75/1.5...
    # -> 1.166..., a different score. Guard against that explicitly.
    assert city["pulse"]["composite_c"] != pytest.approx((2.0 + 1.5 + 0) / 3)
    assert city["pulse"]["score"] == 79            # round(50 + (1.75/3)*50)


def test_no_pillars_present_yields_null_pulse():
    services = pulse.score_pillar(
        "city_services", "City Services",
        [{"label": "311", "dataset": "X", "polarity": -1, "status": "not_published",
          "recent": None, "recent_period": None, "baseline_mean": None,
          "baseline_std": None, "z": None, "yoy_pct": None, "d": None,
          "complete_through": None, "note": None}])
    city = pulse.score_city(id="miami", name="Miami", scope="county",
                            pillar_objs=[services], disclosures=[])
    assert city["pulse"]["composite_c"] is None
    assert city["pulse"]["score"] is None
    assert city["pulse"]["pillars_present"] == 0


# ===========================================================================
# 8. z clipping -- extreme deviation clips to +-3, score stays in [0,100]
# ===========================================================================
def test_extreme_positive_deviation_clips_z_to_plus_3():
    s = series_from_baseline(AS_OF, baseline=[100] * 11 + [101], recent=10 ** 9)
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="x", dataset="d")
    assert f["z"] == 3                    # clipped
    city = one_feed_city(f, pillar_key="development_economy")
    assert city["pulse"]["score"] == 100
    assert 0 <= city["pulse"]["score"] <= 100


def test_extreme_negative_deviation_clips_z_to_minus_3():
    s = series_from_baseline(AS_OF, baseline=[100] * 11 + [101], recent=0)
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="x", dataset="d")
    assert f["z"] == -3
    city = one_feed_city(f, pillar_key="development_economy")
    assert city["pulse"]["score"] == 0


def test_unclipped_z_has_exact_known_magnitude():
    """baseline mean=100, pstdev=10; recent=115 -> z = 1.5 exactly (no clip)."""
    baseline = [90, 110] * 6                       # mean 100, pstdev 10
    s = series_from_baseline(AS_OF, baseline=baseline, recent=115)
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="x", dataset="d")
    assert f["baseline_mean"] == pytest.approx(100.0)
    assert f["baseline_std"] == pytest.approx(10.0)
    assert f["z"] == pytest.approx(1.5)
    assert f["d"] == pytest.approx(1.5)


def test_population_std_convention_ddof0():
    """Confirm population std (ddof=0), not sample std (ddof=1)."""
    import statistics
    baseline = [90, 110] * 6
    s = series_from_baseline(AS_OF, baseline=baseline, recent=100)
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="x", dataset="d")
    assert f["baseline_std"] == pytest.approx(statistics.pstdev(baseline))
    assert f["baseline_std"] != pytest.approx(statistics.stdev(baseline))


# ===========================================================================
# 9. YoY % -- correct sign and magnitude on known fixtures
# ===========================================================================
def test_yoy_positive_known_magnitude():
    """Recent 120 vs same-month-last-year 100 -> +20.0%."""
    baseline = [100] + [105] * 11          # baseline[0] = Recent-12 = 100
    s = series_from_baseline(AS_OF, baseline=baseline, recent=120)
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="x", dataset="d")
    assert f["yoy_pct"] == pytest.approx(20.0)


def test_yoy_negative_known_magnitude():
    """Recent 80 vs same-month-last-year 100 -> -20.0%."""
    baseline = [100] + [90] * 11
    s = series_from_baseline(AS_OF, baseline=baseline, recent=80)
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="x", dataset="d")
    assert f["yoy_pct"] == pytest.approx(-20.0)


def test_yoy_null_when_anchor_missing():
    """If the month 12-before-Recent is absent, yoy is null (here the whole
    baseline is present so status stays ok, but we delete only the anchor's
    value by zeroing it -> treated as null per spec)."""
    baseline = [0] + [100] * 11            # Recent-12 == 0 -> can't divide
    s = series_from_baseline(AS_OF, baseline=baseline, recent=120)
    f = pulse.score_feed(s, polarity=1, as_of=AS_OF, label="x", dataset="d")
    assert f["status"] == "ok"
    assert f["yoy_pct"] is None


# ===========================================================================
# 9b. per-feed trend series (additive, methodology 1.1)
# ===========================================================================
def test_series_carried_through_for_ok_feed():
    """A normal multi-month feed carries an ascending {month, n} series whose
    last point is Recent; <=36 points; each n numeric."""
    s = series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=85)
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="Crimes", dataset="C")
    assert f["status"] == "ok"
    series = f["series"]
    assert isinstance(series, list)
    assert len(series) <= 36
    # Each element is {month: 'YYYY-MM' str, n: number}.
    for pt in series:
        assert set(pt) == {"month", "n"}
        assert isinstance(pt["month"], str) and len(pt["month"]) == 7
        assert pt["month"][4] == "-"
        assert isinstance(pt["n"], (int, float)) and not isinstance(pt["n"], bool)
    # Ascending by month key.
    months = [pt["month"] for pt in series]
    assert months == sorted(months, key=pulse._parse_month)
    # Last point is the feed's Recent period.
    assert series[-1]["month"] == f["recent_period"]


def test_series_trimmed_to_last_36_months():
    """A long series (>36 months) is trimmed to the trailing 36, ending at
    Recent; nothing later than complete_through leaks in."""
    s = series_constant(AS_OF, value=100, length=50)  # 50 contiguous months
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="x", dataset="d")
    series = f["series"]
    assert len(series) == 36
    assert series[-1]["month"] == AS_OF
    assert series[0]["month"] == _month_minus(AS_OF, 35)
    months = [pt["month"] for pt in series]
    assert months == sorted(months, key=pulse._parse_month)
    # No month later than the cutoff/Recent.
    assert all(pulse._parse_month(m) <= pulse._parse_month(AS_OF) for m in months)


def test_series_excludes_incomplete_trailing_month():
    """Months after complete_through are dropped from the series too."""
    s = series_constant(AS_OF, value=100, length=13)
    s = s + [{"month": "2026-05", "n": 999}]  # incomplete future month
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="x", dataset="d")
    months = [pt["month"] for pt in f["series"]]
    assert "2026-05" not in months
    assert f["series"][-1]["month"] == AS_OF


def test_series_null_when_fewer_than_two_months():
    """A single eligible point (or none) yields a null/absent series."""
    one = pulse.score_feed([{"month": AS_OF, "n": 42}],
                           polarity=1, as_of=AS_OF, label="x", dataset="d")
    assert one.get("series") is None
    none = pulse.score_feed([], polarity=1, as_of=AS_OF, label="x", dataset="d")
    assert none.get("series") is None


def test_series_present_even_when_insufficient_history():
    """Trends are useful even when not scored: a short (<12 baseline) but
    >=2-point series still carries a series despite insufficient_history."""
    s = series_constant(AS_OF, value=100, length=6)  # too short to score
    f = pulse.score_feed(s, polarity=-1, as_of=AS_OF, label="x", dataset="d")
    assert f["status"] == "insufficient_history"
    assert isinstance(f["series"], list) and len(f["series"]) == 6
    assert f["series"][-1]["month"] == AS_OF


# ===========================================================================
# 10. map endpoints -- C=+3 -> 100, C=-3 -> 0, C=0 -> 50
# ===========================================================================
def test_pulse_map_endpoints_unit():
    assert pulse._pulse_score(3.0) == 100
    assert pulse._pulse_score(-3.0) == 0
    assert pulse._pulse_score(0.0) == 50
    assert pulse._pulse_score(1.5) == 75
    assert pulse._pulse_score(-1.5) == 25
    assert pulse._pulse_score(None) is None


def test_pulse_map_clips_beyond_endpoints():
    # Composite shouldn't exceed +-3 in practice, but the map must clip anyway.
    assert pulse._pulse_score(5.0) == 100
    assert pulse._pulse_score(-5.0) == 0


def test_composite_equal_weights_default():
    p1 = {"key": "public_safety", "name": "PS", "score_d": 2.0, "feeds": []}
    p2 = {"key": "development_economy", "name": "DE", "score_d": -1.0, "feeds": []}
    assert pulse._composite([p1, p2], None) == pytest.approx(0.5)


def test_composite_skips_null_pillars():
    p1 = {"key": "public_safety", "name": "PS", "score_d": 2.0, "feeds": []}
    p2 = {"key": "city_services", "name": "CS", "score_d": None, "feeds": []}
    assert pulse._composite([p1, p2], None) == pytest.approx(2.0)


# ===========================================================================
# 11. glyph thresholds
# ===========================================================================
def test_glyph_thresholds():
    assert pulse._glyph_label(0.2) == ("up", "Improving")
    assert pulse._glyph_label(-0.2) == ("down", "Worsening")
    assert pulse._glyph_label(0.0) == ("flat", "On baseline")
    assert pulse._glyph_label(0.15) == ("flat", "On baseline")   # boundary inclusive
    assert pulse._glyph_label(-0.15) == ("flat", "On baseline")
    assert pulse._glyph_label(None)[0] == "flat"


# ===========================================================================
# 12. data_health
# ===========================================================================
def test_data_health_counts_ok_feeds_and_latest_period():
    ok1 = pulse.score_feed(series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=85),
                           polarity=-1, as_of=AS_OF, label="Crime", dataset="C")
    ok2 = pulse.score_feed(series_from_baseline("2026-03", baseline=varied_baseline(100), recent=115),
                           polarity=1, as_of=AS_OF, label="Permits", dataset="P",
                           complete_through="2026-03")
    notpub = {"label": "311", "dataset": "X", "polarity": -1,
              "status": "not_published", "recent": None, "recent_period": None,
              "baseline_mean": None, "baseline_std": None, "z": None,
              "yoy_pct": None, "d": None, "complete_through": None, "note": None}
    safety = pulse.score_pillar("public_safety", "PS", [ok1])
    dev = pulse.score_pillar("development_economy", "DE", [ok2])
    services = pulse.score_pillar("city_services", "CS", [notpub])
    city = pulse.score_city(id="chicago", name="Chicago", scope="city",
                            pillar_objs=[safety, dev, services], disclosures=[])
    assert city["data_health"]["feeds_ok"] == 2
    assert city["data_health"]["feeds_total"] == 3
    # latest_updated reflects the max recent_period (2026-04) start-of-month UTC.
    assert city["data_health"]["last_updated"].startswith("2026-04-01T00:00:00")


def test_context_is_none_in_p0():
    city = one_feed_city(
        pulse.score_feed(series_from_baseline(AS_OF, baseline=[100] * 12, recent=70),
                         polarity=-1, as_of=AS_OF, label="Crime", dataset="C"))
    assert city["context"] is None


# ===========================================================================
# 13. payload wrapper
# ===========================================================================
def test_payload_shape_and_defaults():
    city = one_feed_city(
        pulse.score_feed(series_from_baseline(AS_OF, baseline=[100] * 12, recent=70),
                         polarity=-1, as_of=AS_OF, label="Crime", dataset="C"))
    payload = pulse.score_payload([city], as_of=AS_OF,
                                  methodology_disclosures=["Not a cross-city ranking."])
    assert payload["methodology_version"] == "1.1"  # 1.1 adds additive feed.series
    assert payload["as_of"] == AS_OF
    assert payload["cities"] == [city]
    assert "generated_at" in payload and payload["generated_at"]  # auto-filled


def test_payload_explicit_generated_at_preserved():
    payload = pulse.score_payload([], as_of=AS_OF, methodology_disclosures=[],
                                  generated_at="2026-05-31T12:00:00+00:00")
    assert payload["generated_at"] == "2026-05-31T12:00:00+00:00"


# ===========================================================================
# 13b. JSON serialization guard -- the build emits valid JSON (no NaN/Inf)
# ===========================================================================
def test_payload_serializes_with_allow_nan_false():
    """fetch_city writes the artifact with json.dumps(..., allow_nan=False) so a
    stray NaN/Infinity fails the build LOUDLY instead of shipping non-JSON that
    JSON.parse would reject. A real, fully-scored payload must serialize cleanly
    under that strict mode (i.e. it contains no NaN/Infinity)."""
    payload = pulse.score_payload(
        [_build_full_city()], as_of=AS_OF,
        methodology_disclosures=["Not a cross-city ranking."])
    # Must NOT raise -- mirrors the fetch_city.py write path exactly.
    json.dumps(payload, indent=2, allow_nan=False)


def test_injected_nan_raises_under_allow_nan_false():
    """Prove the guard bites: a deliberately-injected NaN in a series point
    raises ValueError under allow_nan=False (and would silently emit the
    literal `NaN` token under the permissive default)."""
    payload = pulse.score_payload(
        [_build_full_city()], as_of=AS_OF, methodology_disclosures=[])
    # Find a real series point and poison its count with NaN.
    poisoned = False
    for pillar in payload["cities"][0]["pulse"]["pillars"]:
        for feed in pillar["feeds"]:
            if feed.get("series"):
                feed["series"][0]["n"] = float("nan")
                poisoned = True
                break
        if poisoned:
            break
    assert poisoned, "fixture should contain at least one feed with a series"
    with pytest.raises(ValueError):
        json.dumps(payload, allow_nan=False)


# ===========================================================================
# 14. schema conformance (jsonschema if available; structural fallback)
# ===========================================================================
def _build_full_city():
    """A realistic 3-pillar city exercising ok / context-only / not_published."""
    crime = pulse.score_feed(
        series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=82),
        polarity=-1, as_of=AS_OF, label="Crimes 2001-present", dataset="ijzp-q8t2",
        complete_through="2026-03", note="excludes last ~7 days")
    permits = pulse.score_feed(
        series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=118),
        polarity=1, as_of=AS_OF, label="Building Permits", dataset="ydr8-5enu")
    backlog = pulse.score_feed(
        series_from_baseline(AS_OF, baseline=varied_baseline(100), recent=92),
        polarity=-1, as_of=AS_OF, label="311 Service Requests", dataset="v6vf-nfxy")
    volume = pulse.score_feed(
        series_from_baseline(AS_OF, baseline=varied_baseline(300, spread=30), recent=400),
        polarity=0, as_of=AS_OF, label="311 volume (context)", dataset="v6vf-nfxy")
    safety = pulse.score_pillar("public_safety", pulse.PILLAR_NAMES["public_safety"],
                                [crime])
    dev = pulse.score_pillar("development_economy",
                             pulse.PILLAR_NAMES["development_economy"], [permits])
    services = pulse.score_pillar("city_services",
                                  pulse.PILLAR_NAMES["city_services"],
                                  [backlog, volume])
    return pulse.score_city(
        id="chicago", name="Chicago", scope="city",
        pillar_objs=[safety, dev, services],
        disclosures=["Not a cross-city ranking.", "Chicago crime excludes last ~7 days."])


def test_full_payload_conforms_to_schema():
    city = _build_full_city()
    payload = pulse.score_payload(
        [city], as_of=AS_OF,
        methodology_disclosures=["Not a cross-city ranking.",
                                 "Polarity is an editorial choice."])

    with open(SCHEMA_PATH) as fh:
        schema = json.load(fh)

    try:
        import jsonschema
    except ImportError:
        jsonschema = None

    if jsonschema is not None:
        jsonschema.validate(instance=payload, schema=schema)
    else:
        # Structural fallback: required keys present, no stray keys at the
        # additionalProperties:false levels.
        for k in schema["required"]:
            assert k in payload
        c = payload["cities"][0]
        city_allowed = set(schema["definitions"]["city"]["properties"])
        assert set(c) == set(schema["definitions"]["city"]["required"]) <= city_allowed
        assert set(c).issubset(city_allowed)
        pulse_allowed = set(schema["definitions"]["pulse"]["properties"])
        assert set(c["pulse"]).issubset(pulse_allowed)
        feed_allowed = set(schema["definitions"]["feed"]["properties"])
        for p in c["pulse"]["pillars"]:
            assert set(p).issubset(set(schema["definitions"]["pillar"]["properties"]))
            for f in p["feeds"]:
                assert set(f).issubset(feed_allowed)


def test_no_extra_keys_at_each_level():
    """Belt-and-suspenders: additionalProperties:false means we emit ONLY the
    schema-allowed keys (caught even if jsonschema isn't installed)."""
    with open(SCHEMA_PATH) as fh:
        schema = json.load(fh)
    city = _build_full_city()

    city_allowed = set(schema["definitions"]["city"]["properties"])
    assert set(city).issubset(city_allowed), set(city) - city_allowed

    pulse_allowed = set(schema["definitions"]["pulse"]["properties"])
    assert set(city["pulse"]).issubset(pulse_allowed)

    feed_allowed = set(schema["definitions"]["feed"]["properties"])
    pillar_allowed = set(schema["definitions"]["pillar"]["properties"])
    dh_allowed = set(schema["definitions"]["data_health"]["properties"])
    assert set(city["data_health"]).issubset(dh_allowed)
    for p in city["pulse"]["pillars"]:
        assert set(p).issubset(pillar_allowed)
        for f in p["feeds"]:
            assert set(f).issubset(feed_allowed), set(f) - feed_allowed


def test_insufficient_history_feed_conforms_to_schema():
    """The insufficient_history branch must also be schema-valid (nulls ok)."""
    bad = pulse.score_feed(series_constant(AS_OF, value=100, length=6),
                           polarity=-1, as_of=AS_OF, label="x", dataset="d")
    pillar = pulse.score_pillar("public_safety", "PS", [bad])
    city = pulse.score_city(id="sf", name="San Francisco", scope="city",
                            pillar_objs=[pillar], disclosures=[])
    payload = pulse.score_payload([city], as_of=AS_OF, methodology_disclosures=[])
    jsonschema = None
    try:
        import jsonschema
    except ImportError:
        pass
    if jsonschema is None:
        pytest.skip("jsonschema not installed")
    jsonschema.validate(instance=payload, schema=schema_loaded())


def schema_loaded():
    with open(SCHEMA_PATH) as fh:
        return json.load(fh)
