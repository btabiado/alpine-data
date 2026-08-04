"""Strip API keys out of anything on its way to a log or a diagnostics record.

WHY THIS EXISTS
---------------
CodeQL flagged seven high-severity "clear-text logging of sensitive
information" paths, and it was right. Every keyed city adapter passes its
credential as a QUERY PARAMETER:

    city/census.py     ...&key=<CENSUS_API_KEY>
    city/airnow.py     ...&API_KEY=<AIRNOW_API_KEY>
    city/fbi.py        ...&API_KEY=<FBI_CDE_API_KEY>

and several adapters then embed the full request URL in their exception
message so a human can see what was called:

    raise FBIError(f"CDE request to {url} failed: {exc}")

`build_context` and `_fetch_feed_series` catch those, take ``str(e)[:300]``,
print it to stderr and store it in the diagnostics list that the build summary
re-prints. The repository is PUBLIC and GitHub Actions logs are world-readable,
so a single 403 from Census would publish a working credential.

GitHub's own log masking is not a defence here. It only masks values registered
as secrets in the workflow that is running, it does not cover a local run, and
it is explicitly documented as best-effort. Treat it as a safety net, never a
licence.

TWO INDEPENDENT PASSES, because either alone has a gap:

1. BY PATTERN — any ``key=``/``api_key=``/``token=`` style parameter is blanked
   whatever its value. Catches a credential this module has never heard of, and
   catches one that reached the string from somewhere other than the
   environment.

2. BY VALUE — the literal contents of the known key variables are replaced
   wherever they appear. Catches a credential that arrives in a shape the
   pattern does not match: a JSON body, a header echoed back by an upstream, a
   `Bearer <token>`, or a URL-encoded copy.

Both are deliberately conservative: they never fail, and they never return
None, because a redactor that throws inside an error handler would convert a
recoverable degradation into a lost diagnostic.
"""
from __future__ import annotations

import os
import re
from urllib.parse import quote

MASK = "***REDACTED***"

# Every environment variable this repo treats as a credential. Kept broader
# than city/ alone so the helper is safe to reuse: a name listed here that is
# unset simply contributes nothing.
SECRET_ENV_NAMES: tuple[str, ...] = (
    "CENSUS_API_KEY", "AIRNOW_API_KEY", "FBI_CDE_API_KEY", "BLS_API_KEY",
    "SOCRATA_APP_TOKEN", "FRED_API_KEY", "EIA_API_KEY", "ALPHA_VANTAGE_API_KEY",
    "FINNHUB_API_KEY", "CRYPTOCOMPARE_API_KEY", "GLASSNODE_API_KEY",
    "COINMETRICS_API_KEY", "ETHERSCAN_API_KEY", "COINGECKO_API_KEY",
    "OPENSKY_CLIENT_SECRET", "REDDIT_CLIENT_SECRET", "ANTHROPIC_API_KEY",
    "R2_SECRET_ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "GH_TOKEN",
)

# `?key=abc`, `&API_KEY=abc`, `&access_token=abc`, `apikey=abc` ...
# The value runs to the next separator; quotes and whitespace end it too so a
# key embedded in JSON or a shell echo is still caught.
_PARAM_RE = re.compile(
    # The leading delimiter is its own group and includes start-of-string:
    # requiring a literal ? or & missed a parameter that opens the message,
    # e.g. a log line that begins "token=abc&other=1". A credential does not
    # stop being a credential because of where the quoting happened to start.
    r"(?i)(^|[?&;,\s])"
    r"((?:api[-_]?key|key|apikey|access[-_]?token|auth[-_]?token"
    r"|token|secret|password|passwd|pwd)\s*=)"
    r"([^&\s\"'<>]+)",
    re.MULTILINE,
)

# `"API_KEY": "abc"` / `'key': 'abc'` — the JSON/dict spelling.
_JSON_RE = re.compile(
    r"(?i)([\"'](?:api[-_]?key|key|apikey|access[-_]?token|auth[-_]?token"
    r"|token|secret|password)[\"']\s*:\s*[\"'])([^\"']+)([\"'])"
)

# `Authorization: Bearer abc`, `Token abc`
_BEARER_RE = re.compile(r"(?i)\b(bearer|token)\s+([A-Za-z0-9._\-]{8,})")

# A value shorter than this is not credential-shaped, and blanking it would
# corrupt ordinary text — e.g. a key legitimately set to "1" would otherwise
# turn every "1" in a message into a mask.
_MIN_SECRET_LEN = 8


def _known_secret_values() -> list[str]:
    """Present, plausibly-secret values from the environment, longest first.

    Longest first matters: if two variables share a prefix, replacing the
    shorter one first would leave the tail of the longer one exposed.
    """
    vals = []
    for name in SECRET_ENV_NAMES:
        v = (os.environ.get(name) or "").strip()
        if len(v) >= _MIN_SECRET_LEN:
            vals.append(v)
    return sorted(set(vals), key=len, reverse=True)


def redact(text: object, extra: "tuple[str, ...] | list[str]" = ()) -> str:
    """Return ``text`` as a string with credentials masked.

    Never raises and never returns None — this runs inside exception handlers,
    where failing would turn a degraded feed into a lost diagnostic.
    """
    try:
        s = text if isinstance(text, str) else str(text)
    except Exception:            # a __str__ that throws
        return MASK
    try:
        s = _PARAM_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, s)
        s = _JSON_RE.sub(lambda m: m.group(1) + MASK + m.group(3), s)
        s = _BEARER_RE.sub(lambda m: m.group(1) + " " + MASK, s)
        for val in list(_known_secret_values()) + [
                v for v in extra if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN]:
            if val in s:
                s = s.replace(val, MASK)
            # A key that travelled through a URL builder is percent-encoded, so
            # the raw comparison above would miss it.
            enc = quote(val, safe="")
            if enc != val and enc in s:
                s = s.replace(enc, MASK)
        return s
    except Exception:
        # Redaction itself failed. Emit nothing rather than risk emitting the
        # unredacted string: a lost error message is recoverable, a published
        # credential is not.
        return MASK
