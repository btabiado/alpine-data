"""Tests for lthcs.normalize."""

from __future__ import annotations

import math

import pytest

from lthcs.normalize import (
    bounded_linear,
    peer_relative_percentile,
    percentile_rank,
    slope,
    z_score,
    z_to_0_100,
)


# --- percentile_rank --------------------------------------------------------


def test_percentile_rank_mid_value() -> None:
    # value=5 in [1..10]: 4 below, 1 equal, N=10 -> (4 + 0.5)/10 * 100 = 45
    result = percentile_rank(5, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert result == pytest.approx(45.0)
    assert type(result) is float


def test_percentile_rank_min_value() -> None:
    # value=1 in [1..10]: 0 below, 1 equal -> 5.0
    assert percentile_rank(1, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == pytest.approx(5.0)


def test_percentile_rank_max_value() -> None:
    # value=10 in [1..10]: 9 below, 1 equal -> (9 + 0.5)/10 * 100 = 95.0
    assert percentile_rank(10, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == pytest.approx(95.0)


def test_percentile_rank_above_all() -> None:
    # value above the whole distribution -> 100
    assert percentile_rank(11, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == pytest.approx(100.0)


def test_percentile_rank_below_all() -> None:
    # value below the whole distribution -> 0
    assert percentile_rank(0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == pytest.approx(0.0)


def test_percentile_rank_all_ties() -> None:
    # value=5 in [5,5,5,5,5]: 0 below, 5 equal -> 50
    assert percentile_rank(5, [5, 5, 5, 5, 5]) == pytest.approx(50.0)


def test_percentile_rank_empty_distribution() -> None:
    result = percentile_rank(42.0, [])
    assert result == pytest.approx(50.0)
    assert type(result) is float


def test_percentile_rank_ignores_nans_in_distribution() -> None:
    # NaNs ignored; effectively percentile_rank(5, [1..10]).
    dist = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, float("nan"), float("nan")]
    assert percentile_rank(5, dist) == pytest.approx(45.0)


def test_percentile_rank_nan_value_returns_nan() -> None:
    result = percentile_rank(float("nan"), [1, 2, 3, 4, 5])
    assert math.isnan(result)
    assert type(result) is float


def test_percentile_rank_all_nan_distribution_neutral() -> None:
    # After cleaning, distribution is empty.
    assert percentile_rank(3.0, [float("nan"), float("nan")]) == pytest.approx(50.0)


# --- z_score ----------------------------------------------------------------


def test_z_score_known_values() -> None:
    # [1,2,3,4,5]: mean=3, sample_stdev=sqrt(2.5)
    # z(5) = (5-3)/sqrt(2.5)
    expected = (5 - 3) / math.sqrt(2.5)
    result = z_score(5, [1, 2, 3, 4, 5])
    assert result == pytest.approx(expected)
    assert type(result) is float


def test_z_score_at_mean_is_zero() -> None:
    assert z_score(3, [1, 2, 3, 4, 5]) == pytest.approx(0.0)


def test_z_score_zero_stdev_returns_zero() -> None:
    result = z_score(5, [5, 5, 5, 5])
    assert result == 0.0
    assert type(result) is float


def test_z_score_too_few_values_returns_zero() -> None:
    assert z_score(5, []) == 0.0
    assert z_score(5, [3]) == 0.0


def test_z_score_nan_value_returns_nan() -> None:
    result = z_score(float("nan"), [1, 2, 3, 4, 5])
    assert math.isnan(result)


def test_z_score_ignores_nans_in_distribution() -> None:
    # Equivalent to z_score(5, [1,2,3,4,5])
    expected = (5 - 3) / math.sqrt(2.5)
    result = z_score(5, [1, 2, 3, float("nan"), 4, 5, float("nan")])
    assert result == pytest.approx(expected)


# --- z_to_0_100 -------------------------------------------------------------


def test_z_to_0_100_at_zero_is_fifty() -> None:
    result = z_to_0_100(0.0)
    assert result == pytest.approx(50.0)
    assert type(result) is float


def test_z_to_0_100_at_positive_clip_is_100() -> None:
    assert z_to_0_100(3.0) == pytest.approx(100.0)


def test_z_to_0_100_at_negative_clip_is_zero() -> None:
    assert z_to_0_100(-3.0) == pytest.approx(0.0)


def test_z_to_0_100_clips_above() -> None:
    assert z_to_0_100(5.0) == pytest.approx(100.0)


def test_z_to_0_100_clips_below() -> None:
    assert z_to_0_100(-5.0) == pytest.approx(0.0)


def test_z_to_0_100_nan_is_neutral() -> None:
    assert z_to_0_100(float("nan")) == pytest.approx(50.0)


def test_z_to_0_100_custom_clip() -> None:
    # clip=2 -> z=1 maps to 75
    assert z_to_0_100(1.0, clip=2.0) == pytest.approx(75.0)


def test_z_to_0_100_invalid_clip_raises() -> None:
    with pytest.raises(ValueError):
        z_to_0_100(0.0, clip=0.0)
    with pytest.raises(ValueError):
        z_to_0_100(0.0, clip=-1.0)


# --- bounded_linear ---------------------------------------------------------


def test_bounded_linear_identity() -> None:
    # value=50 in [0, 100] -> 50.0
    result = bounded_linear(50.0, 0.0, 100.0)
    assert result == pytest.approx(50.0)
    assert type(result) is float


def test_bounded_linear_at_low_is_zero() -> None:
    assert bounded_linear(0.0, 0.0, 100.0) == pytest.approx(0.0)


def test_bounded_linear_at_high_is_100() -> None:
    assert bounded_linear(100.0, 0.0, 100.0) == pytest.approx(100.0)


def test_bounded_linear_clips_above() -> None:
    assert bounded_linear(200.0, 0.0, 100.0) == pytest.approx(100.0)


def test_bounded_linear_clips_below() -> None:
    assert bounded_linear(-50.0, 0.0, 100.0) == pytest.approx(0.0)


def test_bounded_linear_inverted() -> None:
    # low=0, high=100, invert=True. value=0 -> 100, value=100 -> 0, value=25 -> 75.
    assert bounded_linear(0.0, 0.0, 100.0, invert=True) == pytest.approx(100.0)
    assert bounded_linear(100.0, 0.0, 100.0, invert=True) == pytest.approx(0.0)
    assert bounded_linear(25.0, 0.0, 100.0, invert=True) == pytest.approx(75.0)


def test_bounded_linear_inverted_clipping() -> None:
    # Out-of-bounds still clips before inversion.
    assert bounded_linear(-10.0, 0.0, 100.0, invert=True) == pytest.approx(100.0)
    assert bounded_linear(200.0, 0.0, 100.0, invert=True) == pytest.approx(0.0)


def test_bounded_linear_nan_is_neutral() -> None:
    assert bounded_linear(float("nan"), 0.0, 100.0) == pytest.approx(50.0)
    assert bounded_linear(float("nan"), 0.0, 100.0, invert=True) == pytest.approx(50.0)


def test_bounded_linear_low_ge_high_raises() -> None:
    with pytest.raises(ValueError):
        bounded_linear(5.0, 10.0, 10.0)
    with pytest.raises(ValueError):
        bounded_linear(5.0, 20.0, 10.0)


def test_bounded_linear_non_unit_range() -> None:
    # Range [10, 30]: value=20 -> 50, value=15 -> 25, value=25 -> 75.
    assert bounded_linear(20.0, 10.0, 30.0) == pytest.approx(50.0)
    assert bounded_linear(15.0, 10.0, 30.0) == pytest.approx(25.0)
    assert bounded_linear(25.0, 10.0, 30.0) == pytest.approx(75.0)


# --- peer_relative_percentile ----------------------------------------------


def test_peer_relative_percentile_default_excludes_self() -> None:
    # peers = [1..9] (everyone else); value=10 is above them all.
    # 9 below, 0 equal, N=9 -> 100.0
    result = peer_relative_percentile(10, [1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert result == pytest.approx(100.0)
    assert type(result) is float


def test_peer_relative_percentile_include_self_changes_result() -> None:
    # With include_self=True, value=10 joins the universe.
    # universe = [1..9, 10]: 9 below, 1 equal, N=10 -> 95.0
    result_with = peer_relative_percentile(
        10, [1, 2, 3, 4, 5, 6, 7, 8, 9], include_self=True
    )
    result_without = peer_relative_percentile(
        10, [1, 2, 3, 4, 5, 6, 7, 8, 9], include_self=False
    )
    assert result_without == pytest.approx(100.0)
    assert result_with == pytest.approx(95.0)
    assert result_with != result_without


def test_peer_relative_percentile_include_self_at_extreme_low() -> None:
    # universe = [5..10, 1] -> 0 below 1, 1 equal -> 0.5/7 * 100 ≈ 7.142857
    result = peer_relative_percentile(
        1, [5, 6, 7, 8, 9, 10], include_self=True
    )
    assert result == pytest.approx(0.5 / 7 * 100)


def test_peer_relative_percentile_empty_peers() -> None:
    # With no peers and include_self=False -> neutral 50.0.
    assert peer_relative_percentile(10, []) == pytest.approx(50.0)


def test_peer_relative_percentile_nan_value_returns_nan() -> None:
    result = peer_relative_percentile(float("nan"), [1, 2, 3], include_self=True)
    assert math.isnan(result)


# --- slope ------------------------------------------------------------------


def test_slope_linear_series() -> None:
    # y = 2x + 1 at x=1..5 -> [3, 5, 7, 9, 11]; slope should be 2.0
    result = slope([3, 5, 7, 9, 11])
    assert result == pytest.approx(2.0)
    assert type(result) is float


def test_slope_negative_trend() -> None:
    # Strictly decreasing -> negative slope.
    result = slope([10, 8, 6, 4, 2])
    assert result is not None
    assert result < 0
    assert result == pytest.approx(-2.0)


def test_slope_noisy_but_positive() -> None:
    # Noisy upward series; sign should still be positive.
    result = slope([1, 3, 2, 5, 4, 7, 6, 9])
    assert result is not None
    assert result > 0


def test_slope_flat_series_is_zero() -> None:
    result = slope([5, 5, 5, 5, 5])
    assert result == pytest.approx(0.0)


def test_slope_insufficient_data_returns_none() -> None:
    assert slope([]) is None
    assert slope([42.0]) is None
    # Single valid point after NaN-filtering -> None.
    assert slope([float("nan"), 1.0, float("nan")]) is None


def test_slope_ignores_nans() -> None:
    # y = 2x + 1 with one NaN gap; with NaN dropped (and its x dropped too),
    # the remaining points still lie on the same line -> slope == 2.0.
    result = slope([3, 5, float("nan"), 9, 11])
    assert result == pytest.approx(2.0)


def test_slope_returns_python_float() -> None:
    result = slope([1.0, 2.0, 3.0])
    assert result is not None
    assert type(result) is float
