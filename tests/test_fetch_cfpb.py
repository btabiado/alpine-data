"""scripts/fetch_cfpb.py — product taxonomy names must be current.

The CFPB API returns a count of 0 (not an error) for a retired product name,
so "Credit card or prepaid card" silently published zeros. Offline: the
network call is stubbed with counts shaped like the live API (2026-10-04).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "fetch_cfpb", REPO_ROOT / "scripts" / "fetch_cfpb.py")
cfpb = importlib.util.module_from_spec(_spec)
sys.modules["fetch_cfpb"] = cfpb
_spec.loader.exec_module(cfpb)

LIVE_12MO = {"Student loan": 19473, "Mortgage": 32019,
             "Credit card": 92619, "Prepaid card": 6119}


def test_retired_combined_product_name_is_not_queried():
    names = [p for _k, p in cfpb.PRODUCTS]
    assert "Credit card or prepaid card" not in names
    assert "Credit card" in names and "Prepaid card" in names


def test_payload_keeps_schema_and_reports_nonzero_card_counts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfpb, "cfpb_count",
                        lambda product, a, b: LIVE_12MO.get(product, 0))
    assert cfpb.main() == 0
    out = json.loads((tmp_path / "data-cfpb.json").read_text())
    cc = out["products"]["credit_card"]
    assert set(cc) == {"product", "complaints_recent_3mo", "complaints_trailing_12mo",
                       "window_recent", "window_baseline"}
    assert cc["product"] == "Credit card"
    assert cc["complaints_trailing_12mo"] == 92619
    assert out["products"]["prepaid_card"]["complaints_trailing_12mo"] == 6119


def test_zero_count_is_flagged_on_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cfpb, "cfpb_count",
                        lambda product, a, b: 0 if product == "Mortgage" else 5)
    assert cfpb.main() == 0
    assert "mortgage" in capsys.readouterr().err
