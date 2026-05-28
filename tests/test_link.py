"""Tests for LINK-specific behavior — DVOL absence, compute_all key, upload-csv rejection."""
from __future__ import annotations

import pandas as pd

import signals
from server import flask_app


def _build_link_payload(n_days: int = 260, with_dvol: bool = False):
    """Build a synthetic payload with link market data and optional DVOL."""
    dates = pd.date_range("2023-01-01", periods=n_days, freq="D")
    prices = [10.0 + i * 0.05 for i in range(n_days)]

    price_rows = [
        {"date": d.strftime("%Y-%m-%d"), "value": float(p)}
        for d, p in zip(dates, prices)
    ]
    funding_rows = [{"date": d.strftime("%Y-%m-%d"), "rate": 0.0} for d in dates]
    fng_rows = [
        {"date": d.strftime("%Y-%m-%d"), "value": 50, "label": "Neutral"} for d in dates
    ]
    dvol_rows = (
        [{"date": d.strftime("%Y-%m-%d"), "dvol": 50.0} for d in dates] if with_dvol else []
    )

    return {
        "market": {
            "link": {
                "price": price_rows,
                "funding": funding_rows,
                "dvol": dvol_rows,
            },
            "btc": {"price": price_rows, "funding": funding_rows, "dvol": dvol_rows},
            "eth": {"price": price_rows, "funding": funding_rows, "dvol": dvol_rows},
            "fear_greed": fng_rows,
        },
        # No 'link' key at top level — LINK has no ETF flows
        "btc": {"daily": []},
        "eth": {"daily": []},
    }


def test_signals_compute_link_with_no_dvol():
    """LINK signal should compute without a DVOL component when dvol is empty."""
    payload = _build_link_payload(n_days=260, with_dvol=False)
    out = signals.compute_signal("link", payload)
    assert out is not None
    assert isinstance(out["score"], int)
    assert -100 <= out["score"] <= 100
    assert out["label"] in {"STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"}
    # Components must not include any DVOL-related entry
    component_names = " ".join(c["name"].lower() for c in out["components"])
    assert "dvol" not in component_names
    # History should still be present
    assert isinstance(out["history"], list)
    assert len(out["history"]) > 0


def test_signals_compute_all_returns_link_key():
    """compute_all(payload) must return a dict that includes the 'link' key."""
    payload = _build_link_payload(n_days=260, with_dvol=False)
    out = signals.compute_all(payload)
    assert isinstance(out, dict)
    assert "link" in out
    # The link entry should be a non-None signal dict given valid synthetic data
    assert out["link"] is not None
    assert "score" in out["link"]


def test_upload_csv_link_rejected():
    """POST /api/upload-csv?asset=link should be rejected — LINK has no ETF flows."""
    client = flask_app.test_client()
    # CSRF mitigation in server.py requires this header on POST/DELETE.
    client.environ_base["HTTP_X_REQUESTED_WITH"] = "XMLHttpRequest"
    resp = client.post(
        "/api/upload-csv?asset=link",
        data=b"date,Total\n2024-01-11,100\n",
        content_type="text/csv",
    )
    assert resp.status_code == 400, (
        f"Expected 400 for asset=link upload, got {resp.status_code}: {resp.get_data(as_text=True)}"
    )
    body = resp.get_json()
    assert body is not None
    assert body.get("ok") is False
