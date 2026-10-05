"""tools/security_audit.py: the CodeQL check must fail on blocking alerts
AND say which alerts they are (offline: gh_api is stubbed)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import security_audit as sa  # noqa: E402


def _alert(num, rule_id, *, sev=None, sec=None, path="x.py", line=3):
    return {
        "number": num,
        "rule": {"id": rule_id, "severity": sev, "security_severity_level": sec},
        "most_recent_instance": {"location": {"path": path, "start_line": line}},
    }


def test_quality_error_alert_fails_and_is_named(monkeypatch):
    alerts = [
        _alert(7, "py/uninitialized-local-variable", sev="error",
               path="tests/test_x.py", line=42),
        _alert(8, "py/unused-import", sev="note"),
    ]
    monkeypatch.setattr(sa, "gh_api", lambda path: alerts)
    r = sa.check_codeql("o/r")
    assert r.ok is False
    blocking = [v for row in r.rows for k, v in row if k == "value" and v.startswith("#")]
    assert blocking == ["#7 error py/uninitialized-local-variable @ tests/test_x.py:42"]


def test_high_security_alert_fails_medium_does_not(monkeypatch):
    monkeypatch.setattr(sa, "gh_api", lambda path: [
        _alert(1, "py/path-injection", sev="error", sec="high")])
    assert sa.check_codeql("o/r").ok is False
    monkeypatch.setattr(sa, "gh_api", lambda path: [
        _alert(2, "py/stack-trace-exposure", sev="error", sec="medium"),
        _alert(3, "py/unused-local-variable", sev="note")])
    r = sa.check_codeql("o/r")
    assert r.ok is True and "open=2" in r.detail


def test_no_alerts_passes(monkeypatch):
    monkeypatch.setattr(sa, "gh_api", lambda path: [])
    r = sa.check_codeql("o/r")
    assert r.ok is True and r.detail.startswith("open=0")
