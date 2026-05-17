"""Tests for lthcs.sources.eia.

All HTTP calls are mocked via ``unittest.mock.patch`` — no live network
traffic. The module-level ``_cache`` singleton is replaced with a
``FileCache`` rooted at ``tmp_path`` so each test gets isolated storage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import pytest

from lthcs.sources import eia
from lthcs.sources._cache import FileCache


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap the module-level FileCache for a per-test, tmp_path-rooted one."""
    monkeypatch.setattr(eia, "_cache", FileCache("eia", root=tmp_path))


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: a valid API key is set. Individual tests can override."""
    monkeypatch.setenv("EIA_API_KEY", "test-key")


def _mock_response(
    status_code: int = 200,
    json_body: Optional[Dict[str, Any]] = None,
    text: str = "",
) -> MagicMock:
    """Build a fake ``requests.Response``-shaped MagicMock."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text if text else ""
    if json_body is not None:
        resp.json.return_value = json_body
    else:
        resp.json.side_effect = ValueError("no body")
    return resp


def _eia_body(rows: List[Tuple[str, Any]]) -> Dict[str, Any]:
    """Wrap (period, value) tuples in the EIA v2 response envelope."""
    return {
        "response": {
            "data": [{"period": p, "value": v} for (p, v) in rows],
            "total": str(len(rows)),
        }
    }


# ---------------------------------------------------------------------------
# get_series — parsing, sorting, coercion
# ---------------------------------------------------------------------------


def test_get_series_parses_period_and_value() -> None:
    body = _eia_body([("2026-05-14", 78.42), ("2026-05-13", 77.10)])
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        rows = eia.get_series("petroleum/pri/spt")

    assert rows == [
        {"date": "2026-05-13", "value": 77.10},
        {"date": "2026-05-14", "value": 78.42},
    ]


def test_get_series_coerces_value_to_float() -> None:
    body = _eia_body([("2026-05-14", "78.42"), ("2026-05-13", 77)])
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        rows = eia.get_series("petroleum/pri/spt")

    assert all(isinstance(r["value"], float) for r in rows)
    assert rows[0]["value"] == 77.0
    assert rows[1]["value"] == 78.42


def test_get_series_sorted_ascending_regardless_of_api_order() -> None:
    # API returns mixed/desc order — we always sort ascending.
    body = _eia_body(
        [
            ("2026-05-10", 70.0),
            ("2026-05-14", 78.0),
            ("2026-05-12", 74.0),
            ("2026-05-11", 72.0),
            ("2026-05-13", 76.0),
        ]
    )
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        rows = eia.get_series("petroleum/pri/spt")

    dates = [r["date"] for r in rows]
    assert dates == sorted(dates)
    assert dates[-1] == "2026-05-14"  # newest last


def test_get_series_drops_rows_with_missing_or_bad_values() -> None:
    body = {
        "response": {
            "data": [
                {"period": "2026-05-14", "value": 78.42},
                {"period": "2026-05-13", "value": None},
                {"period": "2026-05-12", "value": "not-a-number"},
                {"period": None, "value": 1.0},
                {"period": "2026-05-11", "value": 75.0},
            ]
        }
    }
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        rows = eia.get_series("petroleum/pri/spt")

    assert [r["date"] for r in rows] == ["2026-05-11", "2026-05-14"]


# ---------------------------------------------------------------------------
# get_series — HTTP behavior (params, errors)
# ---------------------------------------------------------------------------


def test_get_series_sends_expected_params() -> None:
    body = _eia_body([("2026-05-14", 78.42)])
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        eia.get_series(
            "petroleum/pri/spt",
            frequency="daily",
            facets={"product": ["EPCWTI"]},
        )

    assert mock_get.call_count == 1
    args, kwargs = mock_get.call_args
    # URL is positional or in kwargs depending on the call style.
    url = args[0] if args else kwargs.get("url")
    assert url == "https://api.eia.gov/v2/petroleum/pri/spt/data/"

    params = kwargs["params"]
    # Params is a list of tuples; turn into a multi-map for easier asserts.
    param_pairs = list(params)
    assert ("api_key", "test-key") in param_pairs
    assert ("frequency", "daily") in param_pairs
    assert ("data[]", "value") in param_pairs
    assert ("facets[product][]", "EPCWTI") in param_pairs
    # And a sort directive is sent.
    sort_columns = [v for (k, v) in param_pairs if k == "sort[0][column]"]
    assert sort_columns == ["period"]


def test_get_series_non_200_raises() -> None:
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(
            status_code=403, text="Forbidden: bad api key"
        )
        with pytest.raises(eia.EIAError) as excinfo:
            eia.get_series("petroleum/pri/spt")

    msg = str(excinfo.value)
    assert "403" in msg
    assert "Forbidden" in msg


def test_missing_api_key_raises_clear_error_at_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        with pytest.raises(eia.EIAError) as excinfo:
            eia.get_series("petroleum/pri/spt")
    assert "EIA_API_KEY" in str(excinfo.value)
    # Should fail *before* any HTTP call is made.
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Cache behavior
# ---------------------------------------------------------------------------


def test_cache_hit_avoids_second_http_call() -> None:
    body = _eia_body([("2026-05-14", 78.42), ("2026-05-13", 77.10)])
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        first = eia.get_series("petroleum/pri/spt", facets={"product": ["EPCWTI"]})
        second = eia.get_series("petroleum/pri/spt", facets={"product": ["EPCWTI"]})

    assert mock_get.call_count == 1
    assert first == second


# ---------------------------------------------------------------------------
# get_wti / get_brent / get_gasoline — delegate with hardcoded params
# ---------------------------------------------------------------------------


def test_get_wti_uses_hardcoded_params() -> None:
    with patch("lthcs.sources.eia.get_series") as mock_series:
        mock_series.return_value = [{"date": "2026-05-14", "value": 78.42}]
        out = eia.get_wti()

    assert mock_series.call_count == 1
    _, kwargs = mock_series.call_args
    assert kwargs["route"] == "petroleum/pri/spt"
    assert kwargs["frequency"] == "daily"
    facets = kwargs["facets"]
    assert "EPCWTI" in facets["product"]
    assert "RWTC" in facets.get("series", [])
    assert out == [{"date": "2026-05-14", "value": 78.42}]


def test_get_brent_uses_hardcoded_params() -> None:
    with patch("lthcs.sources.eia.get_series") as mock_series:
        mock_series.return_value = [{"date": "2026-05-14", "value": 82.10}]
        eia.get_brent()

    _, kwargs = mock_series.call_args
    assert kwargs["route"] == "petroleum/pri/spt"
    assert kwargs["frequency"] == "daily"
    facets = kwargs["facets"]
    assert "EPCBRENT" in facets["product"]
    assert "RBRTE" in facets.get("series", [])


def test_get_gasoline_uses_hardcoded_params() -> None:
    with patch("lthcs.sources.eia.get_series") as mock_series:
        mock_series.return_value = [{"date": "2026-05-12", "value": 3.45}]
        eia.get_gasoline()

    _, kwargs = mock_series.call_args
    assert kwargs["route"] == "petroleum/pri/gnd"
    assert kwargs["frequency"] == "weekly"
    facets = kwargs["facets"]
    assert "NUS" in facets["duoarea"]
    assert "EPMR" in facets["product"]


# ---------------------------------------------------------------------------
# get_latest_value
# ---------------------------------------------------------------------------


def test_get_latest_value_returns_last_entry_from_get_wti() -> None:
    body = _eia_body(
        [
            ("2026-05-14", 78.42),
            ("2026-05-13", 77.10),
            ("2026-05-12", 76.05),
        ]
    )
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=body)
        latest = eia.get_latest_value("wti")
        wti = eia.get_wti()

    assert latest is not None
    assert latest == wti[-1]
    assert latest["date"] == "2026-05-14"
    assert latest["value"] == 78.42


def test_get_latest_value_returns_none_when_empty() -> None:
    with patch("lthcs.sources.eia.requests.get") as mock_get:
        mock_get.return_value = _mock_response(json_body=_eia_body([]))
        assert eia.get_latest_value("brent") is None


def test_get_latest_value_rejects_unknown_route_key() -> None:
    with pytest.raises(eia.EIAError):
        eia.get_latest_value("natgas")
