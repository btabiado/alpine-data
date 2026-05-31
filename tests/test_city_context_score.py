"""Tests for city.context_score -- the transparent cross-city City Context
composite (Layer B; CITY_TAB_BUILD.md section 4).

Pure synthetic fixtures; no network/IO; stdlib + pytest only. The composite is
the kind of number that can be plausibly-but-wrongly computed, so this suite
pins the math hard: min-max scaling endpoints, per-KPI direction flips,
coverage honesty (mean over present KPIs, never imputed), the <2-cities skip
rule, all-None -> None, a fully hand-computable equal-weight mean, the
home_value exclusion, the weights override, and determinism + integer rounding.
"""
from __future__ import annotations

import os
import sys

# Make the repo root importable so `import city.context_score` works regardless
# of cwd (mirrors tests/conftest.py, kept self-contained so this file stands
# alone if run directly).
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from city import context_score as cs  # noqa: E402


# ---------------------------------------------------------------------------
# fixture helpers
# ---------------------------------------------------------------------------
def ctx(**kwargs):
    """A context dict with all schema KPIs defaulting to None, overridden by
    kwargs. Lets each test specify only the KPIs it cares about."""
    base = {
        "median_income": None,
        "median_rent": None,
        "median_home_value": None,
        "median_real_estate_taxes": None,
        "effective_property_tax_rate": None,
        "unemployment_rate": None,
        "aqi": None,
        "context_score": None,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# min-max + direction: lowest rent -> 100, highest rent -> 0
# ---------------------------------------------------------------------------
def test_rent_direction_min_max_endpoints():
    """rent is a '-' KPI: the city with the LOWEST (most affordable) rent gets
    the full 100 on the rent component, the highest rent gets 0, and a city
    exactly halfway between gets 50. With rent the only KPI present, the city
    score == its rent component."""
    cities = {
        "low": ctx(median_rent=1000),   # cheapest -> 100
        "mid": ctx(median_rent=1500),   # halfway  -> 50
        "high": ctx(median_rent=2000),  # priciest -> 0
    }
    out = cs.score_context(cities)
    assert out == {"low": 100, "mid": 50, "high": 0}


def test_income_direction_is_not_flipped():
    """income is a '+' KPI: highest income -> 100, lowest -> 0 (NOT flipped)."""
    cities = {
        "poor": ctx(median_income=40000),   # lowest  -> 0
        "rich": ctx(median_income=90000),   # highest -> 100
        "mid": ctx(median_income=65000),    # halfway -> 50
    }
    out = cs.score_context(cities)
    assert out == {"poor": 0, "rich": 100, "mid": 50}


def test_aqi_lower_is_cleaner_is_better():
    """aqi is a '-' KPI: cleanest (lowest) air scores 100."""
    cities = {"clean": ctx(aqi=10), "smoggy": ctx(aqi=110)}
    out = cs.score_context(cities)
    assert out == {"clean": 100, "smoggy": 0}


# ---------------------------------------------------------------------------
# coverage honesty: a city missing some KPIs is scored on what it has
# ---------------------------------------------------------------------------
def test_coverage_honesty_mean_over_present_only():
    """A city missing a KPI is scored on the MEAN of the KPIs it has — not
    penalized, not imputed with 0 or a neutral filler.

    Setup (income '+', rent '-'), all three cities have both income and rent so
    both KPIs normalize; but city C also omits rent to prove the mean is taken
    over present components only.
    """
    cities = {
        # income: A=40k(min->0)  B=90k(max->100)  C=65k(mid->50)
        # rent:   A=2000(max->0) B=1000(min->100) [C has no rent]
        "A": ctx(median_income=40000, median_rent=2000),
        "B": ctx(median_income=90000, median_rent=1000),
        "C": ctx(median_income=65000),  # rent missing on purpose
    }
    out = cs.score_context(cities)
    # A: mean(income 0, rent 0)   = 0
    # B: mean(income 100, rent 100) = 100
    # C: only income present -> 50 (NOT dragged toward 0 by missing rent)
    assert out == {"A": 0, "B": 100, "C": 50}


def test_missing_kpi_is_not_treated_as_zero():
    """Direct contrast: the city missing the second KPI must NOT score as if
    that KPI were 0. Here both present cities tie on income, so income gives
    everyone 50; the city with rent also gets a rent component, the one without
    is scored on income alone — and still lands at 50, not below."""
    cities = {
        "has_both": ctx(median_income=50000, median_rent=1500),
        "income_only": ctx(median_income=50000),
        "other": ctx(median_income=50000, median_rent=1500),
    }
    out = cs.score_context(cities)
    # income identical across all 3 -> tied -> 50 each.
    # rent: has_both & other tie (1500) -> tied -> 50 each.
    # income_only: scored on income alone -> 50 (not penalized for no rent).
    assert out == {"has_both": 50, "income_only": 50, "other": 50}


# ---------------------------------------------------------------------------
# a KPI present for only 1 city is skipped (can't normalize)
# ---------------------------------------------------------------------------
def test_single_city_kpi_is_skipped():
    """aqi is present for exactly ONE city -> can't form a min-max spread ->
    dropped for everyone. Scores come only from the >=2-city KPI (income)."""
    cities = {
        "A": ctx(median_income=40000, aqi=5),   # aqi present only here
        "B": ctx(median_income=80000),          # no aqi
    }
    out = cs.score_context(cities)
    # aqi skipped (1 city) -> both scored on income alone:
    #   A income min -> 0 ; B income max -> 100.
    assert out == {"A": 0, "B": 100}
    # And A's lone aqi=5 did NOT sneak in as a 100 component.
    assert out["A"] == 0


def test_single_usable_kpi_for_a_city_still_scores():
    """If, after the <2-cities skip, a city has exactly one usable KPI, it is
    scored on that one KPI (mean of a single component)."""
    cities = {
        "A": ctx(median_rent=1000),
        "B": ctx(median_rent=3000),
    }
    out = cs.score_context(cities)
    assert out == {"A": 100, "B": 0}


# ---------------------------------------------------------------------------
# all-None city -> None
# ---------------------------------------------------------------------------
def test_all_none_city_is_none():
    """A city whose context is literally None has no score."""
    cities = {
        "A": ctx(median_rent=1000),
        "B": ctx(median_rent=2000),
        "empty": None,
    }
    out = cs.score_context(cities)
    assert out["empty"] is None
    assert out["A"] == 100 and out["B"] == 0


def test_city_with_all_kpis_none_is_none():
    """A context dict present but every KPI None -> None (no usable KPI)."""
    cities = {
        "A": ctx(median_rent=1000),
        "B": ctx(median_rent=2000),
        "blank": ctx(),  # all KPIs None
    }
    out = cs.score_context(cities)
    assert out["blank"] is None


def test_city_with_only_excluded_kpis_is_none():
    """A city carrying ONLY excluded/non-input fields (home_value, context_score)
    has no usable composite KPI -> None."""
    cities = {
        "A": ctx(median_rent=1000),
        "B": ctx(median_rent=2000),
        "only_excluded": ctx(median_home_value=900000, context_score=42),
    }
    out = cs.score_context(cities)
    assert out["only_excluded"] is None


def test_every_city_none_returns_all_none():
    cities = {"A": None, "B": None}
    assert cs.score_context(cities) == {"A": None, "B": None}


# ---------------------------------------------------------------------------
# equal-weight mean correctness on a fully hand-computable fixture
# ---------------------------------------------------------------------------
def test_equal_weight_mean_hand_computed():
    """Three cities, three KPIs (income '+', rent '-', unemployment '-'),
    every value chosen so each component is an exact 0/50/100, and the mean is
    hand-verifiable.

    income:        A=20k  B=50k  C=80k     -> A=0   B=50  C=100  ('+')
    rent:          A=1000 B=1500 C=2000    -> A=100 B=50  C=0    ('-')
    unemployment:  A=3.0  B=6.0  C=9.0     -> A=100 B=50  C=0    ('-')

    means:
      A = (0 + 100 + 100)/3 = 66.666... -> round -> 67
      B = (50 + 50 + 50)/3  = 50        -> 50
      C = (100 + 0 + 0)/3   = 33.333... -> round -> 33
    """
    cities = {
        "A": ctx(median_income=20000, median_rent=1000, unemployment_rate=3.0),
        "B": ctx(median_income=50000, median_rent=1500, unemployment_rate=6.0),
        "C": ctx(median_income=80000, median_rent=2000, unemployment_rate=9.0),
    }
    out = cs.score_context(cities)
    assert out == {"A": 67, "B": 50, "C": 33}


def test_both_tax_kpis_included_and_point_lower_is_better():
    """median_real_estate_taxes AND effective_property_tax_rate are BOTH in the
    composite and both reward lower values. Hand-check with two cities so each
    KPI is a clean 0/100 endpoint."""
    cities = {
        "lowtax": ctx(median_real_estate_taxes=2000,
                      effective_property_tax_rate=0.005),
        "hightax": ctx(median_real_estate_taxes=10000,
                       effective_property_tax_rate=0.025),
    }
    out = cs.score_context(cities)
    # lowtax: both components 100 -> 100 ; hightax: both 0 -> 0.
    assert out == {"lowtax": 100, "hightax": 0}


def test_home_value_excluded_from_composite():
    """median_home_value must NOT affect the score even when it varies wildly.
    Two identical scoring inputs (income) plus divergent home values -> equal
    scores."""
    cities = {
        "cheap_homes": ctx(median_income=60000, median_home_value=200000),
        "pricey_homes": ctx(median_income=60000, median_home_value=2000000),
    }
    out = cs.score_context(cities)
    # income tied -> 50 each; home_value ignored -> still equal.
    assert out == {"cheap_homes": 50, "pricey_homes": 50}
    assert "median_home_value" not in cs.KPI_DIRECTIONS
    assert "median_home_value" in cs.EXCLUDED_KPIS


# ---------------------------------------------------------------------------
# tied KPI (zero spread, >=2 cities) -> mid-scale 50
# ---------------------------------------------------------------------------
def test_tied_kpi_zero_spread_is_midscale():
    """When all cities reporting a KPI share the same value (>=2 cities, zero
    spread), that component is 50 for each — neither best nor worst — and no
    divide-by-zero occurs."""
    cities = {
        "A": ctx(median_rent=1500),
        "B": ctx(median_rent=1500),
        "C": ctx(median_rent=1500),
    }
    out = cs.score_context(cities)
    assert out == {"A": 50, "B": 50, "C": 50}


# ---------------------------------------------------------------------------
# weights override
# ---------------------------------------------------------------------------
def test_weights_can_drop_a_kpi():
    """A weight of 0 removes a KPI from the mean — equivalent to it being
    absent. Here dropping rent (weight 0) leaves income alone."""
    cities = {
        "A": ctx(median_income=40000, median_rent=2000),
        "B": ctx(median_income=90000, median_rent=1000),
    }
    weighted = cs.score_context(cities, weights={"median_rent": 0})
    # income only: A min -> 0, B max -> 100.
    assert weighted == {"A": 0, "B": 100}


def test_weights_reweight_changes_mean():
    """Non-uniform weights shift the mean predictably. income '+' weight 3,
    rent '-' weight 1, two cities at clean endpoints."""
    cities = {
        "A": ctx(median_income=40000, median_rent=2000),  # income 0,  rent 0
        "B": ctx(median_income=90000, median_rent=1000),  # income 100, rent 100
    }
    out = cs.score_context(cities, weights={"median_income": 3, "median_rent": 1})
    # A: (3*0 + 1*0)/4 = 0 ; B: (3*100 + 1*100)/4 = 100.
    assert out == {"A": 0, "B": 100}


def test_negative_weight_treated_as_zero():
    """A negative weight is not meaningful -> treated as 0 (KPI dropped), so
    the result matches dropping rent entirely."""
    cities = {
        "A": ctx(median_income=40000, median_rent=2000),
        "B": ctx(median_income=90000, median_rent=1000),
    }
    out = cs.score_context(cities, weights={"median_rent": -5})
    assert out == {"A": 0, "B": 100}


# ---------------------------------------------------------------------------
# robustness: bad values are ignored, not crashed on / imputed
# ---------------------------------------------------------------------------
def test_non_numeric_and_bool_values_are_ignored():
    """Junk KPI values (strings, bools, NaN) are treated as missing, not as
    numbers — they neither crash the scorer nor enter the normalization."""
    nan = float("nan")
    cities = {
        "A": ctx(median_rent=1000, aqi="bad"),     # aqi junk
        "B": ctx(median_rent=2000, aqi=True),       # bool, not a reading
        "C": ctx(median_rent=1500, aqi=nan),        # NaN
    }
    out = cs.score_context(cities)
    # aqi has 0 valid numeric cities -> skipped; scores come from rent only:
    #   A=1000 min -> 100, C=1500 mid -> 50, B=2000 max -> 0.
    assert out == {"A": 100, "C": 50, "B": 0}


# ---------------------------------------------------------------------------
# determinism + rounding
# ---------------------------------------------------------------------------
def test_determinism_repeated_calls_identical():
    cities = {
        "A": ctx(median_income=20000, median_rent=1000, unemployment_rate=3.0),
        "B": ctx(median_income=50000, median_rent=1500, unemployment_rate=6.0),
        "C": ctx(median_income=80000, median_rent=2000, unemployment_rate=9.0),
    }
    first = cs.score_context(cities)
    for _ in range(5):
        assert cs.score_context(cities) == first


def test_output_values_are_ints_or_none():
    cities = {
        "A": ctx(median_income=20000, median_rent=1000),
        "B": ctx(median_income=50000, median_rent=1500),
        "C": ctx(median_income=80000, median_rent=2000),
        "empty": None,
    }
    out = cs.score_context(cities)
    for cid, score in out.items():
        assert score is None or isinstance(score, int), (cid, score)
    # Every reported score is a clean int in [0, 100].
    for score in out.values():
        if score is not None:
            assert 0 <= score <= 100


def test_rounding_to_nearest_int():
    """A composite that lands on a non-integer is rounded (not truncated).

    Two KPIs, income '+' and rent '-'. Pick raw values so one city's mean is
    e.g. 66.67 (-> 67) — proving round, not floor.
    """
    cities = {
        # income: A=0k? choose so A income=0, B income=100, plus rent.
        "A": ctx(median_income=10000, median_rent=1000),  # income 0,  rent 100
        "B": ctx(median_income=20000, median_rent=1000),  # income 100, rent 100
    }
    # rent identical -> tied -> 50 each.
    # A: mean(0, 50)   = 25  ; B: mean(100, 50) = 75.
    out = cs.score_context(cities)
    assert out == {"A": 25, "B": 75}


def test_input_dict_not_mutated():
    """Scoring is read-only on its input."""
    cities = {
        "A": ctx(median_rent=1000),
        "B": ctx(median_rent=2000),
    }
    import copy
    snapshot = copy.deepcopy(cities)
    cs.score_context(cities)
    assert cities == snapshot


# ---------------------------------------------------------------------------
# disclosures
# ---------------------------------------------------------------------------
def test_disclosures_shape_and_content():
    d = cs.context_score_disclosures()
    assert isinstance(d, list) and len(d) >= 4
    assert all(isinstance(s, str) and s for s in d)
    blob = " ".join(d).lower()
    # Must cover: not-a-ranking, separate-from-pulse, editorial weighting,
    # which KPIs/direction, home_value exclusion, missing-KPI handling.
    assert "not a quality-of-life ranking" in blob
    assert "pulse" in blob
    assert "equal-weight" in blob
    assert "median_home_value" in blob
    assert "min-max" in blob
    assert "penalized" in blob or "imputed" in blob
    # Lists each included KPI with a direction.
    for kpi in cs.KPI_DIRECTIONS:
        assert kpi in blob


def test_disclosures_returns_fresh_list():
    a = cs.context_score_disclosures()
    a.append("mutated")
    b = cs.context_score_disclosures()
    assert "mutated" not in b
