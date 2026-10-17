"""scripts/fetch_cfpb.py — product taxonomy names must be current.

The CFPB API returns a count of 0 (not an error) for a retired product name,
so "Credit card or prepaid card" silently published zeros. Offline: the
network call is stubbed with counts shaped like the live API (2026-10-04).

A read timeout is also covered here. On 2026-10-12 (run 38277091475)
urllib raised TimeoutError from getresponse(), which is not a URLError, and
the uncaught 45s stall exited before data-cfpb.json was written. That day's
commit is the gap data-health run 38971776049 reported.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
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


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _no_sleep(monkeypatch):
    monkeypatch.setattr(cfpb, "_backoff", lambda attempt: None)


def test_read_timeout_is_retried_then_counted(monkeypatch):
    """The 2026-10-12 failure: TimeoutError on the status-line read."""
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def urlopen(req, timeout=None):
        calls["n"] += 1
        assert timeout == cfpb.TIMEOUT_S
        if calls["n"] < 3:
            raise TimeoutError("The read operation timed out")
        return _Body({"hits": {"total": {"value": 4083}}})

    monkeypatch.setattr(cfpb.urllib.request, "urlopen", urlopen)
    assert cfpb.cfpb_count("Student loan", "2026-07-14", "2026-10-12") == 4083
    assert calls["n"] == 3


def test_integer_total_and_server_error_are_retried(monkeypatch):
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                req.full_url, 503, "unavailable", hdrs=None, fp=None)
        return _Body({"hits": {"total": 12}})

    monkeypatch.setattr(cfpb.urllib.request, "urlopen", urlopen)
    assert cfpb.cfpb_count("Mortgage", "2026-07-14", "2026-10-12") == 12
    assert calls["n"] == 2


def test_client_error_is_not_retried(monkeypatch):
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    def urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            req.full_url, 400, "bad request", hdrs=None, fp=None)

    monkeypatch.setattr(cfpb.urllib.request, "urlopen", urlopen)
    try:
        cfpb.cfpb_count("Student loan", "2026-07-14", "2026-10-12")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    else:
        raise AssertionError("a 400 must not be swallowed as a count")
    assert calls["n"] == 1


def test_persistent_timeout_fails_student_loan_without_writing(tmp_path, monkeypatch, capsys):
    """A day the API never answers must exit 1, not publish a partial file."""
    _no_sleep(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def urlopen(req, timeout=None):
        raise TimeoutError("The read operation timed out")

    monkeypatch.setattr(cfpb.urllib.request, "urlopen", urlopen)
    assert cfpb.main() == 1
    err = capsys.readouterr().err
    assert "FATAL: student_loan fetch failed" in err
    assert "timed out" in err
    assert not (tmp_path / "data-cfpb.json").exists()


def test_optional_product_timeout_still_publishes_student_loan(tmp_path, monkeypatch, capsys):
    _no_sleep(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def urlopen(req, timeout=None):
        if "Mortgage" in req.full_url:
            raise TimeoutError("The read operation timed out")
        return _Body({"hits": {"total": {"value": 5}}})

    monkeypatch.setattr(cfpb.urllib.request, "urlopen", urlopen)
    assert cfpb.main() == 0
    err = capsys.readouterr().err
    assert "warn: mortgage fetch failed" in err
    out = json.loads((tmp_path / "data-cfpb.json").read_text())
    assert "student_loan" in out["products"]
    assert "mortgage" not in out["products"]
