"""The TSA probe in api_status.py must use the HTTP client fetch_tsa.py uses.

PR #43 found that tsa.gov's Akamai edge answers urllib requests from GitHub
Actions with "403 Access Denied" but serves `requests`, so fetch_tsa.py reads
tsa.gov with `requests`. The status page kept probing with urllib, so its "TSA
passenger volumes" row would read "blocked" while the feed worked. TSA now opts
in with ``"client": "requests"``; every other target stays on urllib, and the
opt-in falls back to urllib when `requests` is not installed.

All offline: `requests` and urlopen are replaced with fakes.
"""
from __future__ import annotations

import io
import urllib.error
from pathlib import Path

import pytest
import yaml

import api_status

REPO_ROOT = Path(__file__).resolve().parent.parent
TSA = next(t for t in api_status.TARGETS if t["label"] == "TSA passenger volumes")
TSA_UA = TSA["headers"]["User-Agent"]


class _FakeRaw:
    """The urllib3 response behind requests' ``Response.raw``."""

    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def read(self, amt=None, decode_content=None):
        return self._buf.read(amt)


class _FakeRequestsResp:
    def __init__(self, status=200, reason="OK", body=b""):
        self.status_code, self.reason = status, reason
        self.raw = _FakeRaw(body)
        self.closed = False

    def close(self):
        self.closed = True


class FakeRequests:
    """Stands in for the `requests` module; records each get()."""

    def __init__(self, *answers):
        self.answers, self.calls, self.responses = list(answers), [], []

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append({"url": url, "headers": dict(headers or {}),
                           "timeout": timeout, "stream": stream})
        item = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(item, BaseException):
            raise item
        self.responses.append(item)
        return item


class _FakeUrlopenResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeUrlopen:
    """Stands in for urllib.request.urlopen; records each Request."""

    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def __call__(self, req, timeout=None, context=None):
        self.calls.append({"url": req.full_url,
                           "ua": req.get_header("User-agent"),
                           "timeout": timeout})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return _FakeUrlopenResp(b"x")


def _http_error(code: int, reason: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(TSA["url"], code, reason, {}, None)


@pytest.fixture
def net(monkeypatch):
    """Install fakes for both clients. Returns a function that sets them."""
    def install(requests_fake, urlopen_fake):
        monkeypatch.setattr(api_status, "requests", requests_fake)
        monkeypatch.setattr(api_status.urllib.request, "urlopen", urlopen_fake)
        return requests_fake, urlopen_fake
    return install


# ==========================================================================
# Which targets use which client
# ==========================================================================

def test_only_the_tsa_target_opts_into_requests():
    opted = {t["label"]: t["client"] for t in api_status.TARGETS if "client" in t}
    assert opted == {"TSA passenger volumes": "requests"}


def test_tsa_probe_goes_through_requests(net):
    fake, urlopen = net(FakeRequests(_FakeRequestsResp(200)),
                        FakeUrlopen(AssertionError("urllib must not be used for TSA")))

    row = api_status._probe_one(TSA, timeout=7, attempts=1)

    assert urlopen.calls == []
    assert fake.calls == [{"url": TSA["url"], "headers": {"User-Agent": TSA_UA},
                           "timeout": 7, "stream": True}]
    assert fake.responses[0].closed, "the streamed response must be closed"
    assert (row["status"], row["verdict"], row["reachable"], row["note"]) == \
        (200, "up", True, "")


def test_other_targets_still_use_urllib(net):
    fake, urlopen = net(FakeRequests(AssertionError("requests must not be used")),
                        FakeUrlopen())
    others = [t for t in api_status.TARGETS if "client" not in t]
    assert others, "expected urllib targets"

    for target in others:
        row = api_status._probe_one(target, timeout=3, attempts=1)
        assert row["verdict"] == "up", (target["label"], row)

    assert fake.calls == []
    assert [c["url"] for c in urlopen.calls] == [t["url"] for t in others]


def test_tsa_falls_back_to_urllib_without_requests(net):
    _, urlopen = net(None, FakeUrlopen())

    row = api_status._probe_one(TSA, timeout=5, attempts=1)

    assert urlopen.calls == [{"url": TSA["url"], "ua": TSA_UA, "timeout": 5}]
    assert (row["status"], row["verdict"]) == (200, "up")


# ==========================================================================
# Same verdicts and same row shape whichever client answered
# ==========================================================================

@pytest.mark.parametrize("code,reason,verdict", [
    (403, "Forbidden", "blocked"),           # the Akamai answer to urllib
    (429, "Too Many Requests", "rate_limited"),
    (503, "Service Unavailable", "degraded"),
])
def test_requests_error_status_maps_like_urllib(net, code, reason, verdict):
    net(FakeRequests(_FakeRequestsResp(code, reason)), FakeUrlopen())
    via_requests = api_status._probe_one(TSA, timeout=1, attempts=2)

    net(None, FakeUrlopen(_http_error(code, reason)))
    via_urllib = api_status._probe_one(TSA, timeout=1, attempts=2)

    for row in (via_requests, via_urllib):
        assert row["verdict"] == verdict
        assert row["status"] == code
        assert row["note"] == reason
        assert row["retries"] == 0, "an HTTP reply is an answer, not a retry"
    assert via_requests.keys() == via_urllib.keys()
    drop = ("latency_ms",)
    assert {k: v for k, v in via_requests.items() if k not in drop} == \
        {k: v for k, v in via_urllib.items() if k not in drop}


def test_requests_connection_failure_is_retried_then_down(net):
    fake, _ = net(FakeRequests(ConnectionError("reset by peer")), FakeUrlopen())

    row = api_status._probe_one(TSA, timeout=1, attempts=2)

    assert len(fake.calls) == 2
    assert (row["status"], row["verdict"], row["retries"]) == (None, "down", 2)
    assert row["note"].startswith("ConnectionError: reset by peer")


def test_requests_transient_failure_then_success_is_up(net):
    net(FakeRequests(TimeoutError("timed out"), _FakeRequestsResp(200)), FakeUrlopen())

    row = api_status._probe_one(TSA, timeout=1, attempts=2)

    assert (row["status"], row["verdict"], row["retries"], row["note"]) == \
        (200, "up", 1, "")


def test_requests_path_honours_body_check(net):
    target = dict(TSA, body_check="arcgis")
    body = b'{"error":{"code":499,"message":"Token Required"}}'
    net(FakeRequests(_FakeRequestsResp(200, body=body)), FakeUrlopen())

    row = api_status._probe_one(target, timeout=1, attempts=1)

    assert (row["status"], row["verdict"]) == (200, "blocked")
    assert "499" in row["note"]


# ==========================================================================
# The deploy step that runs the probe has `requests` installed
# ==========================================================================

def test_pages_installs_requests_before_the_probe_step():
    steps = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "pages.yml").read_text()
    )["jobs"]["build"]["steps"]
    runs = [s.get("run") or "" for s in steps]
    probe = next(i for i, r in enumerate(runs) if "api_status.py" in r)
    installs = [r for r in runs[:probe]
                if "pip install -r requirements.txt" in r
                or ("pip install" in r and "requests" in r)]
    assert installs, "no step before the API probe installs requests"
    reqs = (REPO_ROOT / "requirements.txt").read_text().splitlines()
    assert any(line.startswith("requests==") for line in reqs)
