"""Tests for lthcs.sources.fred.

All HTTP is mocked — no live network calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources._cache import FileCache
from lthcs.sources._ratelimit import TokenBucket


# ---- Helpers ----------------------------------------------------------------


def fake_response(json_data: Any, status: int = 200) -> MagicMock:
    m = MagicMock()
    m.status_code = status
    m.json.return_value = json_data
    m.ok = status == 200
    m.text = str(json_data)
    return m


def _sample_payload() -> Dict[str, Any]:
    """Realistic FRED response with a missing-value marker."""
    return {
        "observations": [
            {
                "date": "2026-01-01",
                "value": "300.50",
                "realtime_start": "2026-01-15",
                "realtime_end": "2026-05-16",
            },
            {
                "date": "2026-02-01",
                "value": "301.10",
                "realtime_start": "2026-02-15",
                "realtime_end": "2026-05-16",
            },
            {
                "date": "2026-03-01",
                "value": ".",  # missing
                "realtime_start": "2026-03-15",
                "realtime_end": "2026-05-16",
            },
            {
                "date": "2026-04-01",
                "value": "302.75",
                "realtime_start": "2026-04-15",
                "realtime_end": "2026-05-16",
            },
        ]
    }


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def fred_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Import the module fresh with API key set and a tmp_path cache root.

    Rebinds the module-level ``_cache`` to a tmp-rooted FileCache and
    swaps the rate limiter for a fast one so tests don't sleep.
    """
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    from lthcs.sources import fred  # noqa: WPS433 (intentional inline import)

    # Redirect the cache to a per-test temp dir so tests are isolated.
    monkeypatch.setattr(fred, "_cache", FileCache("fred", root=tmp_path))
    # A fat token bucket so acquire() never blocks during tests.
    monkeypatch.setattr(
        fred, "_bucket", TokenBucket(capacity=10_000, refill_rate=10_000.0)
    )
    return fred


# ---- get_series -------------------------------------------------------------


def test_get_series_parses_observations(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        series = fred_module.get_series("CPIAUCSL")

    assert len(series) == 4
    assert series[0] == {"date": "2026-01-01", "value": 300.50}
    assert series[1] == {"date": "2026-02-01", "value": 301.10}
    # "." value normalized to None.
    assert series[2] == {"date": "2026-03-01", "value": None}
    assert series[3] == {"date": "2026-04-01", "value": 302.75}


def test_get_series_values_are_floats(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        series = fred_module.get_series("CPIAUCSL")

    for row in series:
        assert row["value"] is None or isinstance(row["value"], float)


def test_get_series_missing_dot_becomes_none(fred_module) -> None:
    payload = {
        "observations": [
            {"date": "2026-01-01", "value": "."},
            {"date": "2026-02-01", "value": "1.23"},
        ]
    }
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(payload)
        series = fred_module.get_series("X")

    assert series[0]["value"] is None
    assert series[1]["value"] == 1.23


def test_get_series_sorted_ascending(fred_module) -> None:
    # Feed observations out of order; expect ascending output.
    payload = {
        "observations": [
            {"date": "2026-03-01", "value": "3.0"},
            {"date": "2026-01-01", "value": "1.0"},
            {"date": "2026-02-01", "value": "2.0"},
        ]
    }
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(payload)
        series = fred_module.get_series("X")

    assert [r["date"] for r in series] == [
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
    ]


def test_get_series_passes_observation_start(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response({"observations": []})
        fred_module.get_series("CPIAUCSL", observation_start="2025-01-01")

    assert mock_get.call_count == 1
    _, kwargs = mock_get.call_args
    params = kwargs["params"]
    assert params["series_id"] == "CPIAUCSL"
    assert params["observation_start"] == "2025-01-01"
    assert params["file_type"] == "json"
    assert params["api_key"] == "test-key"


def test_get_series_omits_observation_start_when_none(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response({"observations": []})
        fred_module.get_series("CPIAUCSL")

    _, kwargs = mock_get.call_args
    params = kwargs["params"]
    assert "observation_start" not in params


# ---- Convenience wrappers ---------------------------------------------------


@pytest.mark.parametrize(
    ("fn_name", "series_id"),
    [
        ("get_cpi", "CPIAUCSL"),
        ("get_fed_funds", "FEDFUNDS"),
        ("get_ten_year_yield", "DGS10"),
        ("get_unemployment_rate", "UNRATE"),
        ("get_retail_sales", "RSXFS"),
    ],
)
def test_convenience_wrappers_use_correct_series_id(
    fred_module, fn_name: str, series_id: str
) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response({"observations": []})
        getattr(fred_module, fn_name)()

    _, kwargs = mock_get.call_args
    assert kwargs["params"]["series_id"] == series_id


# ---- get_latest_value -------------------------------------------------------


def test_get_latest_value_returns_last_non_null(fred_module) -> None:
    # Last observation is null; should fall back to the one before it.
    payload = {
        "observations": [
            {"date": "2026-01-01", "value": "1.0"},
            {"date": "2026-02-01", "value": "2.0"},
            {"date": "2026-03-01", "value": "."},
        ]
    }
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(payload)
        latest = fred_module.get_latest_value("X")

    assert latest == {"date": "2026-02-01", "value": 2.0}


def test_get_latest_value_returns_none_when_all_null(fred_module) -> None:
    payload = {
        "observations": [
            {"date": "2026-01-01", "value": "."},
            {"date": "2026-02-01", "value": "."},
        ]
    }
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(payload)
        latest = fred_module.get_latest_value("X")

    assert latest is None


def test_get_latest_value_returns_none_for_empty_series(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response({"observations": []})
        latest = fred_module.get_latest_value("X")

    assert latest is None


# ---- Cache behavior ---------------------------------------------------------


def test_cache_hit_avoids_second_http_call(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())

        first = fred_module.get_series("CPIAUCSL")
        second = fred_module.get_series("CPIAUCSL")

    assert mock_get.call_count == 1
    assert first == second


def test_cache_separates_by_observation_start(fred_module) -> None:
    # Different observation_start values are independent cache entries.
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())

        fred_module.get_series("CPIAUCSL")
        fred_module.get_series("CPIAUCSL", observation_start="2025-01-01")

    assert mock_get.call_count == 2


# ---- Auth -------------------------------------------------------------------


def test_missing_api_key_raises_at_first_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)

    # Importing the module without the key set must NOT raise.
    from lthcs.sources import fred

    monkeypatch.setattr(fred, "_cache", FileCache("fred", root=tmp_path))
    monkeypatch.setattr(
        fred, "_bucket", TokenBucket(capacity=10_000, refill_rate=10_000.0)
    )

    with pytest.raises(RuntimeError, match="FRED_API_KEY"):
        fred.get_series("CPIAUCSL")


# ---- Error handling ---------------------------------------------------------


def test_non_200_response_raises(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(
            {"error_message": "Bad Request"}, status=400
        )
        with pytest.raises(Exception) as excinfo:
            fred_module.get_series("BADID")

    msg = str(excinfo.value)
    assert "400" in msg


def test_non_200_response_does_not_retry(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response({"err": "x"}, status=500)
        with pytest.raises(Exception):
            fred_module.get_series("X")

    # No retry — exactly one HTTP call.
    assert mock_get.call_count == 1


def test_non_200_response_skips_cache_write(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response({"err": "x"}, status=500)
        with pytest.raises(Exception):
            fred_module.get_series("X")

        # Second call should also raise (not return a cached error).
        mock_get.return_value = fake_response({"err": "x"}, status=500)
        with pytest.raises(Exception):
            fred_module.get_series("X")

    assert mock_get.call_count == 2


# ---- as_of: historical filtering -------------------------------------------


def test_get_series_as_of_none_returns_full_series(fred_module) -> None:
    # as_of=None must be byte-identical to the no-arg call.
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        no_as_of = fred_module.get_series("CPIAUCSL")

    # Reset cache so the as_of=None call really hits parse/cache logic again.
    fred_module._cache.clear()
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        with_explicit_none = fred_module.get_series("CPIAUCSL", as_of=None)

    assert with_explicit_none == no_as_of
    assert len(with_explicit_none) == 4


def test_get_series_as_of_filters_to_subset(fred_module) -> None:
    # Sample payload runs Jan -> Apr 2026.  as_of=2026-02-15 should keep
    # only Jan + Feb.
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        series = fred_module.get_series("CPIAUCSL", as_of="2026-02-15")

    assert [r["date"] for r in series] == ["2026-01-01", "2026-02-01"]


def test_get_series_as_of_before_any_data_returns_empty(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        series = fred_module.get_series("CPIAUCSL", as_of="2020-01-01")

    assert series == []


def test_get_series_as_of_after_latest_returns_full_series(fred_module) -> None:
    # as_of past the most-recent observation should behave like "today".
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        series = fred_module.get_series("CPIAUCSL", as_of="2099-01-01")

    assert len(series) == 4
    assert series[-1]["date"] == "2026-04-01"


def test_get_series_as_of_exact_match_is_included(fred_module) -> None:
    # The 2026-02-01 observation lives exactly on the cutoff. ``<=`` means
    # it's IN, not out.
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        series = fred_module.get_series("CPIAUCSL", as_of="2026-02-01")

    assert [r["date"] for r in series] == ["2026-01-01", "2026-02-01"]


def test_get_series_as_of_isolates_cache(fred_module) -> None:
    # Different as_of values must produce independent cache entries —
    # otherwise a "latest" call could return a trimmed historical view.
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        fred_module.get_series("CPIAUCSL")
        fred_module.get_series("CPIAUCSL", as_of="2026-02-15")
        fred_module.get_series("CPIAUCSL", as_of="2026-03-15")

    assert mock_get.call_count == 3


def test_get_latest_value_as_of_returns_latest_before_cutoff(fred_module) -> None:
    # Sample has Jan/Feb/Apr non-null (Mar is "."); as_of=2026-03-15 should
    # land on Feb because Mar is null and Apr is past the cutoff.
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        latest = fred_module.get_latest_value("CPIAUCSL", as_of="2026-03-15")

    assert latest == {"date": "2026-02-01", "value": 301.10}


def test_get_latest_value_as_of_before_any_data_returns_none(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        latest = fred_module.get_latest_value("CPIAUCSL", as_of="2020-01-01")

    assert latest is None


def test_get_latest_value_as_of_exact_match_is_included(fred_module) -> None:
    with patch("lthcs.sources.fred.requests.get") as mock_get:
        mock_get.return_value = fake_response(_sample_payload())
        latest = fred_module.get_latest_value("CPIAUCSL", as_of="2026-04-01")

    # 2026-04-01 has a real value; should be returned exactly.
    assert latest == {"date": "2026-04-01", "value": 302.75}
