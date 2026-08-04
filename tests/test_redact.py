"""A credential must never reach a log, a diagnostic, or an exception message.

CodeQL flagged seven high-severity "clear-text logging of sensitive
information" paths in the city pipeline and every one of them was real. The
shape was always the same: a keyed adapter puts its credential in a QUERY
PARAMETER, embeds the request URL in its exception message so a human can see
what was called, and `build_context` / `_fetch_feed_series` then take
``str(e)[:300]`` and print it. This repository is public and Actions logs are
world-readable, so one 403 from Census published a working key.

These tests pin the redactor AND the call sites, because a redactor nobody
calls is worse than none — it looks like protection.
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from city.redact import MASK, redact, safe_url

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk_live_A1b2C3d4E5f6G7h8i9J0"


# --------------------------------------------------------------------------
# the redactor itself
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text,why", [
    ("403 Client Error: Forbidden for url: "
     "https://api.census.gov/data/2022/acs/acs5?get=B01003_001E&key=" + SECRET,
     "requests' HTTPError stringifies with the full URL"),
    ("CDE request to https://cde.ucr.cjis.gov/x?API_KEY=" + SECRET + " failed",
     "city/fbi.py embeds the URL in its exception"),
    ("AirNow returned HTTP 401: "
     "https://airnowapi.org/aq/observation/?API_KEY=" + SECRET,
     "airnow's key param is spelled differently again"),
    ('{"API_KEY": "' + SECRET + '", "state": "FL"}', "a JSON request body"),
    ("Authorization: Bearer " + SECRET, "an auth header echoed back"),
    ("token=" + SECRET + "&other=1", "leading param, no ? or &"),
    ("...&access_token=" + SECRET, "an alternate parameter name"),
])
def test_known_leak_shapes_are_masked(text, why):
    out = redact(text)
    assert SECRET not in out, f"leaked via: {why}"
    assert MASK in out


def test_a_secret_value_is_masked_wherever_it_appears(monkeypatch):
    """Pattern matching alone is not enough: a credential can arrive in a shape
    no parameter regex anticipates. The value itself is also replaced."""
    monkeypatch.setenv("CENSUS_API_KEY", SECRET)
    out = redact("upstream said: the token " + SECRET + " is not valid")
    assert SECRET not in out and MASK in out


def test_percent_encoded_secret_is_masked(monkeypatch):
    """A key that went through a URL builder arrives encoded, so a raw
    comparison would miss it."""
    raw = "abc/def+ghi=jkl"
    monkeypatch.setenv("CENSUS_API_KEY", raw)
    importlib.reload(importlib.import_module("city.redact"))
    from city.redact import redact as r
    out = r("https://x/?key=abc%2Fdef%2Bghi%3Djkl")
    assert "abc%2Fdef" not in out


def test_ordinary_text_is_untouched():
    msg = "census failed: connection reset by peer after 3 retries"
    assert redact(msg) == msg


def test_a_short_env_value_does_not_shred_the_message(monkeypatch):
    """A key set to something tiny (or to a placeholder like '1') must not turn
    every occurrence of that text into a mask."""
    monkeypatch.setenv("CENSUS_API_KEY", "1")
    out = redact("1 of 6 cities failed, 1 feed lost")
    assert out == "1 of 6 cities failed, 1 feed lost"


def test_redact_never_raises_and_never_returns_none():
    class Exploding:
        def __str__(self):  # noqa: D105
            raise RuntimeError("boom")
    assert redact(Exploding()) == MASK
    assert redact(None) == "None"
    assert isinstance(redact(12345), str)


# --------------------------------------------------------------------------
# the call sites — a redactor nobody calls is not protection
# --------------------------------------------------------------------------

def _src(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", [
    "city/fbi.py", "city/census.py", "city/arcgis.py",
    "city/airnow.py", "city/bls.py",
])
def test_adapters_never_put_a_raw_url_into_an_exception(rel):
    """Every `raise XError(f"... {url} ...")` must be wrapped in redact()."""
    src = _src(rel)
    for m in re.finditer(r"raise \w*Error\((f[\"'].*?)\)", src, re.S):
        frag = m.group(1)
        if "{url}" in frag or "{body}" in frag:
            start = src.rfind("raise", 0, m.start())
            stmt = src[start:m.end()]
            assert "redact(" in stmt, (
                f"{rel}: this raise embeds a URL or body without redact():\n{stmt[:200]}")


def test_context_note_redacts_before_printing_and_before_storing():
    src = _src("city/context.py")
    body = src[src.index("def _note("):]
    body = body[:body.index("\ndef ")]
    assert "redact(detail)" in body, "_note must redact"
    # and it must redact BEFORE the diagnostics append, or the value is stored
    # raw and re-printed later by the build summary
    assert body.index("redact(detail)") < body.index("diagnostics.append")


def test_fetch_city_redacts_at_both_the_sink_and_the_summary():
    src = _src("fetch_city.py")
    fail = src[src.index("def _fail("):]
    fail = fail[:fail.index("\n    if adapter")]
    assert "redact(reason)" in fail
    assert fail.index("redact(reason)") < fail.index("diagnostics.append")
    # defence in depth: the summary printer re-prints stored details
    assert src.count('redact(d["detail"])') == 2


# --------------------------------------------------------------------------
# safe_url — the credential never enters the message in the first place
# --------------------------------------------------------------------------

def test_safe_url_drops_the_query_entirely():
    out = safe_url("https://api.census.gov/data/2022/acs/acs5?get=X&key=" + SECRET)
    assert out == "https://api.census.gov/data/2022/acs/acs5"
    assert SECRET not in out and "key=" not in out


def test_safe_url_drops_userinfo_credentials():
    out = safe_url("https://user:" + SECRET + "@example.gov/path?a=1")
    assert SECRET not in out
    assert out == "https://example.gov/path"


def test_safe_url_keeps_the_part_an_operator_needs():
    """Dropping the query must not drop WHICH endpoint failed."""
    assert safe_url("https://cde.ucr.cjis.gov/LATEST/agency/byStateAbbr/FL") == \
        "https://cde.ucr.cjis.gov/LATEST/agency/byStateAbbr/FL"


def test_safe_url_degrades_to_the_mask_not_to_the_input():
    class Exploding:
        def __str__(self):  # noqa: D105
            raise RuntimeError("boom")
    assert safe_url(Exploding()) == MASK


@pytest.mark.parametrize("rel", ["city/fbi.py", "city/census.py", "city/arcgis.py"])
def test_adapters_build_messages_from_safe_url_not_the_raw_url(rel):
    """redact() after the fact works, but the key is briefly assembled into the
    string. safe_url() means it is never there — belt as well as braces, and
    the form a static analyser can actually follow."""
    src = (REPO_ROOT / rel).read_text(encoding="utf-8")
    for m in re.finditer(r'f"[^"]*\{url\}[^"]*"', src):
        raise AssertionError(
            f"{rel}: message interpolates the RAW url, use safe_url(url): {m.group(0)[:90]}")
