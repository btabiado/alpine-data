"""fetch_tsa.py: direct tsa.gov fetch, then a GitHub mirror, then the Internet Archive.

www.tsa.gov's Akamai edge 403s GitHub Actions runners by IP, so the daily
aviation-tsa.yml run failed for ~107 days and data-tsa.json froze at
2026-06-17. The fix reads the Wayback Machine's raw copy of the same page when
the direct fetch fails. These tests pin that fallback, the provenance it
records, and the "both failed" path, which must leave the old file alone and
say so loudly.

All network access is faked at ``urllib.request.urlopen`` (and, for the
tsa.gov request when `requests` is installed, at ``fetch_tsa.requests``), so
the real fetch_html / fetch_mirror / archive_get / URL-building code runs.
Nothing here touches the network.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

import fetch_tsa

FIXTURE = Path(__file__).parent / "fixtures" / "tsa_passenger_volumes.html"
HTML = FIXTURE.read_text()

AVAIL_TS = "20261003081514"
SNAPSHOT = ("https://web.archive.org/web/20261003081514id_/"
            "https://www.tsa.gov/travel/passenger-volumes")


def _availability(ts=AVAIL_TS, status="200", available=True):
    return json.dumps({
        "url": "tsa.gov/travel/passenger-volumes",
        "archived_snapshots": {"closest": {
            "status": status,
            "available": available,
            "url": f"http://web.archive.org/web/{ts}/https://www.tsa.gov/travel/passenger-volumes",
            "timestamp": ts,
        }},
    })


class _Resp(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body.encode("utf-8"))
        self._url = url

    def geturl(self):
        return self._url


def _http_error(url, code, msg):
    return urllib.error.HTTPError(url, code, msg, {}, io.BytesIO(b"denied"))


class FakeNet:
    """Routes urlopen() by host. Each handler is a list consumed per request
    (the last entry repeats); an entry is a str body, a (body, final_url)
    tuple for a redirect, or an exception to raise. The GitHub mirror answers
    404 unless a test says otherwise, so archive tests still reach the archive."""

    def __init__(self, tsa, avail=None, snap=None, mirror=None):
        if mirror is None:
            mirror = [_http_error(fetch_tsa.MIRROR_URL, 404, "Not Found")]
        self.routes = {"www.tsa.gov": tsa, "archive.org": avail,
                       "web.archive.org": snap,
                       "raw.githubusercontent.com": mirror}
        self.calls = []  # (url, headers, timeout)

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append((url, dict(req.header_items()), timeout))
        host = urllib.parse.urlsplit(url).netloc
        if host not in self.routes or self.routes[host] is None:
            raise AssertionError(f"unexpected request to {url}")
        handler = self.routes[host]
        n = sum(1 for u, _, _ in self.calls
                if urllib.parse.urlsplit(u).netloc == host) - 1
        item = handler[min(n, len(handler) - 1)]
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, tuple):
            return _Resp(*item)
        return _Resp(item, url)

    def urls(self):
        return [u for u, _, _ in self.calls]


@pytest.fixture
def env(tmp_path, monkeypatch):
    out = tmp_path / "data-tsa.json"
    monkeypatch.setattr(fetch_tsa, "OUT", str(out))
    sleeps = []
    monkeypatch.setattr(fetch_tsa.time, "sleep", sleeps.append)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    # Exercise the urllib path for tsa.gov by default; the `requests` path has
    # its own tests below.
    monkeypatch.setattr(fetch_tsa, "requests", None)

    def install(net):
        monkeypatch.setattr(fetch_tsa.urllib.request, "urlopen", net)
        return net

    return out, sleeps, install


FORBIDDEN = _http_error(fetch_tsa.URL, 403, "Forbidden")


# --- parser against the fixture ---------------------------------------------

def test_parse_rows_reads_only_the_data_table():
    rows = fetch_tsa.parse_rows(HTML)
    assert len(rows) == 35
    assert rows[0] == ("10/2/2026", 2722200)   # newest first, as published
    assert rows[-1] == ("8/29/2026", 2129212)
    # Stray dates/numbers in the prose and the footer table are ignored.
    assert ("1/1/2020", 9999999) not in rows


# --- direct success ----------------------------------------------------------

def test_direct_success_never_touches_the_archive(env):
    out, sleeps, install = env
    net = install(FakeNet(tsa=[HTML]))

    assert fetch_tsa.main() == 0

    assert net.urls() == [fetch_tsa.URL]
    data = json.loads(out.read_text())
    assert data["source"] == "tsa.gov"
    assert data["src"] == fetch_tsa.SRC_DIRECT
    assert data["snapshot_url"] is None and data["snapshot_timestamp"] is None
    assert data["as_of"] == "2026-10-02"
    assert data["latest"] == {"date": "10/2/2026", "vol": 2722200}
    assert data["avg7"] == 2574559
    assert len(data["series"]) == fetch_tsa.KEEP
    assert data["series"][-1] == {"d": "10/2/2026", "v": 2722200}
    assert sleeps == []


def test_direct_request_is_unchanged(env):
    # The direct tsa.gov request keeps its pre-existing headers and timeout;
    # the fallback must not alter what is sent there.
    _, _, install = env
    net = install(FakeNet(tsa=[HTML]))
    fetch_tsa.main()
    (url, headers, timeout), = net.calls
    assert headers["User-agent"] == fetch_tsa.UA
    assert timeout == 45


# --- direct failure -> Wayback ----------------------------------------------

@pytest.mark.parametrize("direct", [
    FORBIDDEN,
    urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer")),
    "<html><body>Access Denied</body></html>",  # 200 but no table
], ids=["http-403", "conn-reset", "no-rows"])
def test_direct_failure_falls_back_to_wayback(env, direct):
    out, sleeps, install = env
    net = install(FakeNet(tsa=[direct], avail=[_availability()], snap=[HTML]))

    assert fetch_tsa.main() == 0

    # tsa.gov is tried exactly once (no retry), then the mirror (404 here),
    # then availability, then the raw capture.
    assert net.urls() == [fetch_tsa.URL, fetch_tsa.MIRROR_URL,
                          fetch_tsa.AVAILABILITY_URL, SNAPSHOT]
    data = json.loads(out.read_text())
    assert data["source"] == "web.archive.org"
    assert data["src"] == fetch_tsa.SRC_ARCHIVE
    assert data["snapshot_url"] == SNAPSHOT
    assert data["snapshot_timestamp"] == "2026-10-03T08:15:14Z"
    # as_of is the newest TABLE date, not the capture date or the clock.
    assert data["as_of"] == "2026-10-02"
    # The dashboard's fields are all still there with the same shapes.
    assert data["generated"]
    assert data["latest"] == {"date": "10/2/2026", "vol": 2722200}
    assert data["avg7"] == 2574559
    assert all(set(p) == {"d", "v"} for p in data["series"])
    assert sleeps == []


def test_archive_requests_identify_the_project(env):
    _, _, install = env
    net = install(FakeNet(tsa=[FORBIDDEN], avail=[_availability()], snap=[HTML]))
    fetch_tsa.main()
    # Match on the parsed host: a substring test would also accept
    # "archive.org.example.com" or a path that merely mentions it.
    archive_calls = [c for c in net.calls
                     if urllib.parse.urlsplit(c[0]).hostname in ("archive.org", "web.archive.org")]
    assert len(archive_calls) == 2
    for _, headers, timeout in archive_calls:
        assert "+https://github.com/btabiado/alpine-data" in headers["User-agent"]
        assert timeout == fetch_tsa.ARCHIVE_TIMEOUT


def test_stale_snapshot_cannot_pass_as_fresh(env):
    # A capture taken today of a table that ends in June must report June.
    out, _, install = env
    old_table = HTML.replace("/2026", "/2025")
    install(FakeNet(tsa=[FORBIDDEN], avail=[_availability()], snap=[old_table]))

    assert fetch_tsa.main() == 0

    data = json.loads(out.read_text())
    assert data["snapshot_timestamp"] == "2026-10-03T08:15:14Z"
    assert data["as_of"] == "2025-10-02"
    assert data["latest"]["date"] == "10/2/2025"


def test_availability_api_down_uses_nearest_capture_redirect(env):
    # archive.org's availability API rate-limits shared IPs (429). Wayback
    # itself redirects a "now" timestamp to the newest capture; the recorded
    # timestamp must be the one actually served, not the one asked for.
    out, sleeps, install = env
    served = ("https://web.archive.org/web/20261002230101id_/"
              "https://www.tsa.gov/travel/passenger-volumes")
    net = install(FakeNet(
        tsa=[FORBIDDEN],
        avail=[_http_error(fetch_tsa.AVAILABILITY_URL, 429, "Too Many Requests")],
        snap=[(HTML, served)]))

    assert fetch_tsa.main() == 0

    asked = net.urls()[-1]
    assert asked.startswith("https://web.archive.org/web/")
    assert asked.endswith("id_/https://www.tsa.gov/travel/passenger-volumes")
    data = json.loads(out.read_text())
    assert data["snapshot_url"] == served
    assert data["snapshot_timestamp"] == "2026-10-02T23:01:01Z"
    assert sleeps == []  # a 429 is not retried


@pytest.mark.parametrize("closest", [
    {},                                             # nothing archived
    {"status": "302", "available": True, "timestamp": AVAIL_TS},
], ids=["empty", "non-200-capture"])
def test_unusable_availability_answer_falls_through(env, closest):
    out, _, install = env
    body = json.dumps({"archived_snapshots": {"closest": closest} if closest else {}})
    served = SNAPSHOT
    install(FakeNet(tsa=[FORBIDDEN], avail=[body], snap=[(HTML, served)]))
    assert fetch_tsa.main() == 0
    assert json.loads(out.read_text())["source"] == "web.archive.org"


def test_transient_archive_error_is_retried_once_after_a_pause(env):
    out, sleeps, install = env
    net = install(FakeNet(
        tsa=[FORBIDDEN], avail=[_availability()],
        snap=[_http_error(SNAPSHOT, 503, "Service Unavailable"), HTML]))

    assert fetch_tsa.main() == 0

    assert net.urls().count(SNAPSHOT) == 2
    assert sleeps == [fetch_tsa.ARCHIVE_RETRY_PAUSE]
    assert json.loads(out.read_text())["source"] == "web.archive.org"


# --- both sources fail -------------------------------------------------------

PRIOR = {"generated": "2026-06-18T17:22:09Z",
         "latest": {"date": "6/17/2026", "vol": 2638190}, "avg7": 2725437,
         "series": [{"d": "6/17/2026", "v": 2638190}],
         "src": fetch_tsa.SRC_DIRECT}


def _both_fail(install):
    reset = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
    return install(FakeNet(tsa=[FORBIDDEN], avail=[_availability()], snap=[reset]))


def test_both_fail_preserves_file_and_warns(env, monkeypatch, capsys):
    out, sleeps, install = env
    out.write_text(json.dumps(PRIOR, indent=1))
    before = out.read_bytes()
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    net = _both_fail(install)

    assert fetch_tsa.main() == 1

    assert out.read_bytes() == before  # prior data not clobbered
    assert net.urls().count(SNAPSHOT) == 2  # one retry, no loop
    assert sleeps == [fetch_tsa.ARCHIVE_RETRY_PAUSE]
    stdout = capsys.readouterr().out
    warnings = [ln for ln in stdout.splitlines() if ln.startswith("::warning")]
    assert len(warnings) == 1
    w = warnings[0]
    assert w.startswith("::warning title=TSA not refreshed::")
    assert "tsa.gov: HTTPError: HTTP Error 403: Forbidden" in w
    assert "web.archive.org: URLError:" in w and "Connection reset" in w


def test_both_fail_with_no_prior_file_writes_nothing(env, capsys):
    out, _, install = env
    _both_fail(install)
    assert fetch_tsa.main() == 1
    assert not out.exists()
    captured = capsys.readouterr()
    assert "::warning" not in captured.out  # not in Actions -> no annotation
    assert "NOT refreshed" in captured.err


def test_annotation_escapes_percent_cr_lf(env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    fetch_tsa.warn_not_refreshed("100% blocked\r\nsecond line")
    out = capsys.readouterr().out
    assert out == "::warning title=TSA not refreshed::100%25 blocked%0D%0Asecond line\n"


def test_gh_escape_encodes_percent_first():
    # Encoding % first keeps an already-escaped-looking %0A literal.
    assert fetch_tsa._gh_escape("%0A\n") == "%250A%0A"


# --- never step backwards / no churn ----------------------------------------

def test_older_snapshot_never_replaces_newer_data(env):
    out, _, install = env
    newer = dict(PRIOR, latest={"date": "10/3/2026", "vol": 2600000})
    out.write_text(json.dumps(newer, indent=1))
    before = out.read_bytes()
    install(FakeNet(tsa=[FORBIDDEN], avail=[_availability()], snap=[HTML]))

    assert fetch_tsa.main() == 0
    assert out.read_bytes() == before


def test_newer_capture_of_identical_table_is_not_rewritten(env):
    out, _, install = env
    install(FakeNet(tsa=[FORBIDDEN], avail=[_availability()], snap=[HTML]))
    assert fetch_tsa.main() == 0
    first = out.read_bytes()

    later = "20261004081000"
    install(FakeNet(tsa=[FORBIDDEN], avail=[_availability(ts=later)],
                    snap=[(HTML, SNAPSHOT.replace(AVAIL_TS, later))]))
    assert fetch_tsa.main() == 0
    assert out.read_bytes() == first  # no churn commit for a re-capture


# --- `requests` for tsa.gov ----------------------------------------------------

class _FakeRequestsResp:
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} Client Error: Forbidden")


class FakeRequests:
    """Stands in for the `requests` module; records each get()."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {}), timeout))
        item = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def test_direct_uses_requests_when_installed(env, monkeypatch):
    out, _, install = env
    fake = FakeRequests(_FakeRequestsResp(HTML))
    monkeypatch.setattr(fetch_tsa, "requests", fake)
    net = install(FakeNet(tsa=[AssertionError("urllib must not be used for tsa.gov")]))

    assert fetch_tsa.main() == 0

    assert fake.calls == [(fetch_tsa.URL, {"User-Agent": fetch_tsa.UA}, 45)]
    assert net.calls == []
    data = json.loads(out.read_text())
    assert data["source"] == "tsa.gov"
    assert data["latest"] == {"date": "10/2/2026", "vol": 2722200}


def test_requests_403_falls_back_to_the_mirror(env, monkeypatch):
    out, _, install = env
    monkeypatch.setattr(fetch_tsa, "requests",
                        FakeRequests(_FakeRequestsResp("Access Denied", status=403)))
    install(FakeNet(tsa=[], mirror=[MIRROR_CSV]))
    assert fetch_tsa.main() == 0
    assert json.loads(out.read_text())["source"] == "github.com/bcantoni/tsa-data"


# --- GitHub mirror ---------------------------------------------------------------

def _mirror_csv(rows):
    """parse_rows() output -> the mirror's date,passengers CSV (oldest first)."""
    lines = ["date,passengers"]
    for d, n in sorted(rows, key=lambda r: fetch_tsa.date_key(r[0])):
        lines.append(f"{fetch_tsa.date_key(d).isoformat()},{n}")
    return "\n".join(lines) + "\n"


MIRROR_CSV = _mirror_csv(fetch_tsa.parse_rows(HTML))


def test_mirror_used_when_direct_fails(env):
    out, sleeps, install = env
    net = install(FakeNet(tsa=[FORBIDDEN], mirror=[MIRROR_CSV]))

    assert fetch_tsa.main() == 0

    assert net.urls() == [fetch_tsa.URL, fetch_tsa.MIRROR_URL]  # archive untouched
    data = json.loads(out.read_text())
    assert data["source"] == "github.com/bcantoni/tsa-data"
    assert data["src"] == fetch_tsa.SRC_MIRROR
    assert data["snapshot_url"] == fetch_tsa.MIRROR_URL
    assert data["snapshot_timestamp"] is None
    # Same table as tsa.gov's -> same numbers, in the dashboard's M/D/YYYY form.
    assert data["as_of"] == "2026-10-02"
    assert data["latest"] == {"date": "10/2/2026", "vol": 2722200}
    assert data["avg7"] == 2574559
    assert data["series"][-1] == {"d": "10/2/2026", "v": 2722200}
    assert sleeps == []


def test_mirror_request_identifies_the_project(env):
    _, _, install = env
    net = install(FakeNet(tsa=[FORBIDDEN], mirror=[MIRROR_CSV]))
    fetch_tsa.main()
    (url, headers, timeout), = [c for c in net.calls
                                if urllib.parse.urlsplit(c[0]).hostname == "raw.githubusercontent.com"]
    assert "+https://github.com/btabiado/alpine-data" in headers["User-agent"]
    assert timeout == fetch_tsa.MIRROR_TIMEOUT


@pytest.mark.parametrize("bad", [
    "day,count\n2026-10-02,1\n",            # wrong columns
    "date,passengers\nnot-a-date,abc\n",    # no usable rows
], ids=["wrong-columns", "no-rows"])
def test_bad_mirror_falls_through_to_wayback(env, bad):
    out, _, install = env
    install(FakeNet(tsa=[FORBIDDEN], mirror=[bad], avail=[_availability()], snap=[HTML]))
    assert fetch_tsa.main() == 0
    assert json.loads(out.read_text())["source"] == "web.archive.org"


def test_stale_mirror_never_replaces_newer_data(env):
    out, _, install = env
    newer = dict(PRIOR, latest={"date": "10/3/2026", "vol": 2600000})
    out.write_text(json.dumps(newer, indent=1))
    before = out.read_bytes()
    install(FakeNet(tsa=[FORBIDDEN], mirror=[MIRROR_CSV]))
    assert fetch_tsa.main() == 0
    assert out.read_bytes() == before


def test_empty_archive_page_is_named_in_the_warning(env, monkeypatch, capsys):
    # A capture of Akamai's denial page parses to nothing; the annotation must
    # say what the page was so the next failure is diagnosable from the log.
    _, _, install = env
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    denied = "<html><head><title>Access Denied</title></head><body>Reference #18</body></html>"
    install(FakeNet(tsa=[FORBIDDEN], avail=[_availability()], snap=[denied]))

    assert fetch_tsa.main() == 1

    w = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("::warning")][0]
    assert "mirror: HTTPError: HTTP Error 404" in w
    assert f"no rows parsed from snapshot {AVAIL_TS}" in w
    assert "Access Denied" in w
