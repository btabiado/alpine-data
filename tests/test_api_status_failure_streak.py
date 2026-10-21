"""One-off connection failures must not page the daily audit as P1.

Alpine Data daily audit 2026-10-21, Top Issue 2: FRED ENPLANE flipped to
``down`` on a read timeout (both in-run attempts, ~24s) and the next probe
run was HTTP 200. BLS had the same timeout signature the day before and
recovered. ``down`` is an audit P1; ``degraded`` is a P2.

Contract, with HTTP mocked:
  * a single failed run is ``degraded``, not ``down``
  * the second consecutive failed run is ``down``
  * a success resets the counter
  * the counter lives in the snapshot file the probe already writes
  * blocked 403, paid-only 402, auth_required, and retired rows are unchanged
"""
from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from pathlib import Path

import api_status


class _Resp(io.BytesIO):
    def __init__(self, status=200, body=b"{}"):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url: str, code: int, reason: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, reason, {}, None)


def _target(label="Example", url="https://example.test/probe", **extra):
    row = {"label": label, "category": "Test", "url": url, "key_env": None}
    row.update(extra)
    return row


def _install(monkeypatch, handler):
    """Replace urlopen and sleep. ``handler(url)`` returns a response or raises."""
    calls = []
    sleeps = []

    def urlopen(req, timeout=None, context=None):
        calls.append({"url": req.full_url, "timeout": timeout})
        result = handler(req.full_url)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(api_status.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(api_status.time, "sleep", lambda s: sleeps.append(s))
    return calls, sleeps


def _write_prior(path, sources):
    path.write_text(json.dumps({
        "generated_at": "2026-10-21T13:01:00+00:00",
        "sources": sources,
    }))


def _probe(monkeypatch, tmp_path, handler, prior_sources=None, targets=None):
    path = tmp_path / "api_status.json"
    monkeypatch.setattr(api_status, "OUT_PATH", path)
    if prior_sources is not None:
        _write_prior(path, prior_sources)
    monkeypatch.setattr(api_status, "TARGETS", targets or [_target()])
    calls, sleeps = _install(monkeypatch, handler)
    snap = api_status.probe_all(timeout=1, max_workers=2)
    return snap, calls, sleeps, path


def _row(snap, label="Example"):
    return next(s for s in snap["sources"] if s["label"] == label)


def test_single_connection_failure_is_degraded_not_down(monkeypatch, tmp_path):
    snap, calls, sleeps, path = _probe(
        monkeypatch, tmp_path,
        lambda url: TimeoutError("The read operation timed out"),
    )
    row = _row(snap)
    assert row["verdict"] == "degraded"
    assert row["verdict"] != "down"
    assert row["status"] is None
    assert row["consecutive_failures"] == 1
    assert row["retries"] == 2
    assert "TimeoutError: The read operation timed out" in row["note"]
    assert "not marked down until 2" in row["note"]
    assert snap["summary"]["down"] == 0
    assert snap["summary"]["degraded"] == 1
    # One retry, one short backoff, two HTTP attempts. No file yet, so this
    # is the first failed run.
    assert len(calls) == 2
    assert sleeps == [api_status.PROBE_BACKOFF_S]
    assert not path.exists()


def test_second_consecutive_failure_is_down(monkeypatch, tmp_path):
    snap, calls, sleeps, path = _probe(
        monkeypatch, tmp_path,
        lambda url: TimeoutError("The read operation timed out"),
        prior_sources=[{
            "label": "Example",
            "verdict": "degraded",
            "consecutive_failures": 1,
            "note": "TimeoutError: The read operation timed out "
                    "(1 consecutive probe failure; not marked down until 2)",
        }],
    )
    row = _row(snap)
    assert row["verdict"] == "down"
    assert row["consecutive_failures"] == 2
    assert row["status"] is None
    assert "2 consecutive probe failures" in row["note"]
    assert snap["summary"]["down"] == 1
    assert snap["summary"]["degraded"] == 0
    assert len(calls) == 2
    assert sleeps == [api_status.PROBE_BACKOFF_S]
    # The prior file is what the run read; the caller (main) writes the new
    # snapshot. Round-trip that write and the count must still be there.
    path.write_text(json.dumps(snap))
    stored = json.loads(path.read_text())
    assert stored["sources"][0]["consecutive_failures"] == 2
    assert stored["sources"][0]["verdict"] == "down"


def test_recovery_resets_the_counter(monkeypatch, tmp_path):
    snap, calls, sleeps, _path = _probe(
        monkeypatch, tmp_path,
        lambda url: _Resp(200),
        prior_sources=[{
            "label": "Example",
            "verdict": "down",
            "consecutive_failures": 2,
            "note": "TimeoutError: timed out (2 consecutive probe failures)",
        }],
    )
    row = _row(snap)
    assert row["verdict"] == "up"
    assert row["status"] == 200
    assert row["consecutive_failures"] == 0
    assert row["note"] == ""
    assert row["reachable"] is True
    assert snap["summary"]["up"] == 1
    assert snap["summary"]["down"] == 0
    assert calls and sleeps == []


def test_streak_round_trip_through_the_status_file(monkeypatch, tmp_path):
    """The count the next run sees is whatever the previous snapshot wrote."""
    path = tmp_path / "api_status.json"
    monkeypatch.setattr(api_status, "OUT_PATH", path)
    monkeypatch.setattr(api_status, "TARGETS", [_target()])
    monkeypatch.setattr(api_status.time, "sleep", lambda _s: None)

    def fail(req, timeout=None, context=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(api_status.urllib.request, "urlopen", fail)
    first = api_status.probe_all(timeout=1, max_workers=1)
    assert _row(first)["verdict"] == "degraded"
    assert _row(first)["consecutive_failures"] == 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(first))

    second = api_status.probe_all(timeout=1, max_workers=1)
    assert _row(second)["verdict"] == "down"
    assert _row(second)["consecutive_failures"] == 2
    path.write_text(json.dumps(second))

    def ok(req, timeout=None, context=None):
        return _Resp(200)

    monkeypatch.setattr(api_status.urllib.request, "urlopen", ok)
    third = api_status.probe_all(timeout=1, max_workers=1)
    assert _row(third)["verdict"] == "up"
    assert _row(third)["consecutive_failures"] == 0


def test_in_run_success_after_retry_is_not_a_failed_run(monkeypatch, tmp_path):
    answers = iter([TimeoutError("timed out"), _Resp(200)])

    def handler(_url):
        return next(answers)

    snap, calls, sleeps, _path = _probe(
        monkeypatch, tmp_path, handler,
        prior_sources=[{"label": "Example", "verdict": "degraded",
                        "consecutive_failures": 1}],
    )
    row = _row(snap)
    assert row["verdict"] == "up"
    assert row["consecutive_failures"] == 0
    assert row["retries"] == 1
    assert len(calls) == 2
    assert sleeps == [api_status.PROBE_BACKOFF_S]


def test_legacy_down_without_a_counter_counts_as_one_failure(monkeypatch, tmp_path):
    """Snapshots from before this field existed: a stored down is one strike."""
    snap, _calls, _sleeps, _path = _probe(
        monkeypatch, tmp_path,
        lambda url: TimeoutError("timed out"),
        prior_sources=[{"label": "Example", "verdict": "down", "note": "TimeoutError"}],
    )
    row = _row(snap)
    assert row["verdict"] == "down"
    assert row["consecutive_failures"] == 2


def test_http_errors_are_not_retried_or_debounced(monkeypatch, tmp_path):
    """402 and 403 are permanent answers, not connection failures."""
    bridges = next(t for t in api_status.TARGETS if t["label"] == "DeFiLlama bridges")
    farside = next(t for t in api_status.TARGETS if t["label"] == "Farside (ETF flows)")

    def handler(url):
        if "bridges.llama.fi" in url:
            return _http_error(url, 402, "Payment Required")
        if "farside.co.uk" in url:
            return _http_error(url, 403, "Forbidden")
        raise AssertionError(url)

    snap, calls, sleeps, _path = _probe(
        monkeypatch, tmp_path, handler,
        prior_sources=[
            {"label": "DeFiLlama bridges", "verdict": "degraded",
             "consecutive_failures": 1},
            {"label": "Farside (ETF flows)", "verdict": "blocked",
             "consecutive_failures": 4},
        ],
        targets=[bridges, farside],
    )
    paid = _row(snap, "DeFiLlama bridges")
    blocked = _row(snap, "Farside (ETF flows)")
    assert (paid["status"], paid["verdict"], paid["consecutive_failures"]) == \
        (402, "degraded", 0)
    assert paid["note"] == "Payment Required"
    assert "consecutive" not in paid["note"]
    assert (blocked["status"], blocked["verdict"], blocked["consecutive_failures"]) == \
        (403, "blocked", 0)
    assert blocked["note"] == "Forbidden"
    assert sleeps == []
    assert len(calls) == 2
    assert snap["summary"]["down"] == 0
    assert snap["summary"]["blocked"] == 1
    assert snap["summary"]["degraded"] == 1


def test_auth_required_is_not_debounced(monkeypatch, tmp_path):
    airnow = next(t for t in api_status.TARGETS if t["label"] == "EPA AirNow")
    snap, calls, sleeps, _path = _probe(
        monkeypatch, tmp_path,
        lambda url: _http_error(url, 401, "Unauthorized"),
        prior_sources=[{"label": "EPA AirNow", "verdict": "down",
                        "consecutive_failures": 2}],
        targets=[airnow],
    )
    row = _row(snap, "EPA AirNow")
    assert row["verdict"] == "auth_required"
    assert row["status"] == 401
    assert row["consecutive_failures"] == 0
    assert row["reachable"] is True
    assert sleeps == []
    assert len(calls) == 1


def test_retired_row_is_not_probed_and_resets_any_streak(monkeypatch, tmp_path):
    retired = next(t for t in api_status.TARGETS if t["label"] == "bitinfocharts")

    def refuse(_url):
        raise AssertionError("retired host was probed")

    snap, calls, sleeps, _path = _probe(
        monkeypatch, tmp_path, refuse,
        prior_sources=[{"label": "bitinfocharts", "verdict": "down",
                        "consecutive_failures": 3}],
        targets=[retired],
    )
    row = _row(snap, "bitinfocharts")
    assert row["verdict"] == "retired"
    assert row["consecutive_failures"] == 0
    assert row["status"] is None
    assert "Not probed" in row["note"]
    assert calls == []
    assert sleeps == []
    assert snap["summary"]["retired"] == 1
    assert snap["summary"]["down"] == 0


def test_audit_raises_p1_only_after_the_second_failed_run(monkeypatch, tmp_path):
    """The daily audit pages ``down`` as P1 and ``degraded`` as P2."""
    spec = importlib.util.spec_from_file_location(
        "_dar_streak",
        Path(__file__).resolve().parent.parent / "scripts" / "daily_audit_report.py",
    )
    dar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dar)

    first, _c, _s, path = _probe(
        monkeypatch, tmp_path, lambda url: TimeoutError("timed out"))
    path.write_text(json.dumps(first))
    second = api_status.probe_all(timeout=1, max_workers=1)

    def severities(snap):
        verdicts = {s["label"]: s["verdict"] for s in snap["sources"]}
        probs = dar.data_problems({"api_status": {"status": "ok", "verdicts": verdicts}})
        return {p["id"]: p["severity"] for p in probs}

    assert severities(first) == {"api:Example": "P2"}
    assert severities(second) == {"api:Example": "P1"}


def test_no_backoff_when_the_only_attempt_fails(monkeypatch, tmp_path):
    path = tmp_path / "api_status.json"
    monkeypatch.setattr(api_status, "OUT_PATH", path)
    sleeps = []
    monkeypatch.setattr(api_status.time, "sleep", lambda s: sleeps.append(s))

    def urlopen(req, timeout=None, context=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(api_status.urllib.request, "urlopen", urlopen)
    row = api_status._probe_one(_target(), timeout=1, attempts=1)
    assert row["verdict"] == "down"
    assert row["retries"] == 1
    assert sleeps == []
