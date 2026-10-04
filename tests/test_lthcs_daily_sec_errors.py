"""SEC EDGAR failures in lthcs_daily must say why they failed.

The revenue fetch used to sit in a bare ``except Exception:`` that logged
nothing. From 2026-06-05 every ticker came back ``sec_unavailable`` and the
financial pillar was a constant 50 for months, and no log line distinguished
"SEC_USER_AGENT is not set" from "SEC answered 403". These tests pin the
reporting that replaced that silence.
"""

from __future__ import annotations

import lthcs_daily
from lthcs.sources.sec_edgar import SECEdgarError

MISSING_UA = (
    "SEC_USER_AGENT env var is not set. SEC requires a custom User-Agent "
    "containing a real contact email, e.g. 'Acme Research bryan@example.com'. "
    "Set SEC_USER_AGENT in your environment or .env file."
)


def _http_403(cik: int) -> SECEdgarError:
    return SECEdgarError(
        "SEC EDGAR request to https://data.sec.gov/api/xbrl/companyfacts/"
        "CIK%010d.json failed with status 403: <html>Request ID 4815162342</html>"
        % cik
    )


def test_one_cause_across_every_ticker_collapses_to_one_key():
    # The real failure mode: the same rejection, once per ticker, each with a
    # different CIK in the URL. Unnormalized, that is 215 "distinct" errors and
    # the summary is as unreadable as no summary at all.
    errors: dict = {}
    for cik in range(215):
        lthcs_daily._note_sec_error(errors, _http_403(cik))
    assert len(errors) == 1
    (key, count), = errors.items()
    assert count == 215
    assert "status 403" in key, key  # the part that tells you what to do


def test_url_rule_collapses_short_ciks_the_digit_rule_misses():
    # companyfacts URLs zero-pad the CIK to 10 digits, which the long-digit
    # rule would collapse on its own. Other EDGAR endpoints use the UNPADDED
    # CIK (CIK 1750 is four digits), and those only collapse because the whole
    # URL is normalized. This is the case that keeps the URL rule honest:
    # remove it and these become 5 "distinct" causes.
    errors: dict = {}
    for cik in (1750, 2488, 3116, 4962, 8670):
        lthcs_daily._note_sec_error(
            errors,
            SECEdgarError(
                "SEC EDGAR request to https://www.sec.gov/cgi-bin/browse-edgar"
                "?action=getcompany&CIK=%d failed with status 403: denied" % cik
            ),
        )
    assert len(errors) == 1, sorted(errors)


def test_missing_user_agent_message_survives_intact():
    # This is the cause most likely to be live, so normalization must not
    # mangle it. In particular the quoted example UA must not be mistaken for
    # a ticker and rewritten.
    errors: dict = {}
    for _ in range(215):
        lthcs_daily._note_sec_error(errors, SECEdgarError(MISSING_UA))
    (key, count), = errors.items()
    assert count == 215
    assert "SEC_USER_AGENT env var is not set" in key
    assert "'<sym>'" not in key


def test_cik_lookup_failures_collapse_across_tickers():
    errors: dict = {}
    for sym in ("AAPL", "BRK.B", "MSFT"):
        lthcs_daily._note_sec_error(
            errors,
            SECEdgarError("Could not resolve ticker %r to a CIK via SEC tickers file." % sym),
        )
    assert len(errors) == 1


def test_distinct_causes_stay_distinct():
    errors: dict = {}
    lthcs_daily._note_sec_error(errors, SECEdgarError(MISSING_UA))
    lthcs_daily._note_sec_error(errors, _http_403(1))
    lthcs_daily._note_sec_error(errors, ConnectionError("tunnel closed"))
    assert len(errors) == 3


def test_annotation_only_inside_actions(monkeypatch, capsys):
    errors = {"SECEdgarError: boom": 3}
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    lthcs_daily._report_sec_errors(errors, 215)
    assert "::warning" not in capsys.readouterr().out

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    lthcs_daily._report_sec_errors(errors, 215)
    out = capsys.readouterr().out
    assert out.startswith("::warning title=SEC EDGAR unavailable::")
    assert "3/215" in out


def test_annotation_is_one_line_even_with_newlines_and_percents(monkeypatch, capsys):
    # An unescaped newline ends a workflow command early, silently dropping
    # the cause; an unescaped % can be read as an escape sequence.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    lthcs_daily._report_sec_errors({"SECEdgarError: 50% off\nsecond line": 1}, 1)
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("::warning")]
    assert len(lines) == 1
    assert "%25" in lines[0] and "%0A" in lines[0]


def test_no_errors_prints_nothing(capsys):
    lthcs_daily._report_sec_errors({}, 215)
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_user_agent_value_is_never_logged(monkeypatch, capsys):
    # The UA carries a contact email and this repo's logs are public. None of
    # SECEdgarError's messages include it today; this guards the reporter
    # against a future message that would.
    ua = "Alpine Data secret-contact@example.org"
    monkeypatch.setenv("SEC_USER_AGENT", ua)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    errors: dict = {}
    lthcs_daily._note_sec_error(errors, _http_403(320193))
    lthcs_daily._report_sec_errors(errors, 1)
    captured = capsys.readouterr()
    assert "secret-contact@example.org" not in captured.out + captured.err


def test_stage_2_actually_routes_revenue_failures_through_the_reporter():
    # The helpers above can pass while the call site has quietly gone back to
    # a bare ``except Exception: ... = []`` -- which is exactly the state that
    # hid this failure for four months. Read the source and pin the wiring.
    # Same approach as test_secrets_check_coverage: a regression here is a
    # one-line revert that no behavioural test of the helpers would notice.
    import inspect
    import re

    src = inspect.getsource(lthcs_daily.stage_2_fetch_data)
    rev_block = re.search(
        r"get_revenue_history\(.*?except Exception(.*?)\n\s*try:", src, re.S
    )
    assert rev_block, "could not locate the revenue fetch's try/except"
    handler = rev_block.group(1)
    assert "as e" in handler, "revenue except no longer binds the exception"
    assert "_note_sec_error(sec_errors, e)" in handler, (
        "revenue failures are being swallowed again -- sec_unavailable will "
        "go back to having no stated cause"
    )
    assert src.index("_report_sec_errors(sec_errors, n)") < src.index("Stage 2: Fetched"), (
        "the SEC cause summary must print before the Stage 2 line it explains"
    )
