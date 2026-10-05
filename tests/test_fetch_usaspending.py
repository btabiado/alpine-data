"""scripts/fetch_usaspending.py — lag months must not read as real zeros.

DoD contract actions reach USAspending on a 90-day delay and the current
month is always partial, so the raw series ends in near-zero months
(2026-10-04: DoD Jul $2.6B, Aug $22M, Sep $74M, Oct $0). Offline.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "fetch_usaspending", REPO_ROOT / "scripts" / "fetch_usaspending.py")
fu = importlib.util.module_from_spec(_spec)
sys.modules["fetch_usaspending"] = fu
_spec.loader.exec_module(fu)


def test_dod_lag_window_and_partial_month_flagged():
    series = [{"month": m, "totalAmount": v} for m, v in [
        ("2026-05", 4.99e10), ("2026-06", 4.32e10), ("2026-07", 2.6e9),
        ("2026-08", 2.2e7), ("2026-09", 7.4e7), ("2026-10", 0.0)]]
    meta = fu.mark_incomplete(series, "month", date(2026, 10, 4), 90)
    assert meta["complete_through"] == "2026-06"
    assert meta["incomplete_periods"] == ["2026-07", "2026-08", "2026-09", "2026-10"]
    by = {r["month"]: r for r in series}
    assert by["2026-06"]["incomplete"] is False
    assert by["2026-07"]["incomplete_reason"] == "inside 90-day reporting lag"
    assert by["2026-10"]["incomplete_reason"] == "current partial month"
    assert by["2026-10"]["totalAmount"] == 0.0     # value kept, but flagged


def test_grants_only_current_month_is_partial():
    series = [{"period": "2026-09", "obligated": 5.3e10},
              {"period": "2026-10", "obligated": 0.0}]
    meta = fu.mark_incomplete(series, "period", date(2026, 10, 4), 0)
    assert meta["complete_through"] == "2026-09"
    assert meta["incomplete_periods"] == ["2026-10"]


def test_main_writes_flags(tmp_path, monkeypatch):
    def fake_post(filters):
        if "agencies" in filters:
            return [{"time_period": {"fiscal_year": "2026", "month": "9"},
                     "aggregated_amount": 4.3e10},     # Jun 2026
                    {"time_period": {"fiscal_year": "2027", "month": "1"},
                     "aggregated_amount": 0}]          # Oct 2026
        return [{"time_period": {"fiscal_year": "2026", "month": "12"},
                 "Grant_Obligations": 5.3e10},          # Sep 2026
                {"time_period": {"fiscal_year": "2027", "month": "1"},
                 "Grant_Obligations": 0}]               # Oct 2026

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 4, 13, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(fu, "post_spending_over_time", fake_post)
    monkeypatch.setattr(fu, "datetime", _FixedDT)
    monkeypatch.chdir(tmp_path)
    assert fu.main() == 0
    out = json.loads((tmp_path / "data-usaspending.json").read_text())
    assert out["dod_contracts"]["complete_through"] == "2026-06"
    assert out["dod_contracts"]["series"][-1]["incomplete"] is True
    assert out["grants"]["incomplete_periods"] == ["2026-10"]
