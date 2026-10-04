"""data-aviation.json's machine-readable vintage must stay honest.

The file's `asOf` is prose — "FAA airman data Dec 31 2025 · FAA aircraft
registry late May 2026 · market snapshot late May 2026" — which no parser may
guess at (build_health_status._parse_date_value refuses it on purpose, and
app.py's aviationFreshness() refuses it a second time). So the file now carries
`data_date` alongside the prose.

`data_date` is hand-maintained, because data-aviation.json has no fetcher: it is
a curated, committed sidecar. Hand-maintained is exactly the kind of stamp that
rots — someone refreshes the registry section, updates the prose, and forgets
the machine date, or updates one component and takes the MAX by reflex. These
tests are the thing that notices.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AVIATION = ROOT / "data-aviation.json"

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def av() -> dict:
    if not AVIATION.exists():
        pytest.skip("data-aviation.json not present")
    return json.loads(AVIATION.read_text(encoding="utf-8"))


def test_has_a_machine_readable_date(av):
    """Rule 4: the monitor must be able to evaluate this file at all.

    Without a parseable top-level date the watchdog reports UNKNOWN forever,
    which is indistinguishable from "we stopped being able to see this feed".
    """
    assert ISO.match(str(av.get("data_date", ""))), (
        f"data-aviation.json has no ISO `data_date` (got "
        f"{av.get('data_date')!r}); the prose `asOf` is not machine-readable")


def test_data_date_is_the_oldest_component_not_the_newest(av):
    """Rule 2: a composite of N inputs is only as fresh as its OLDEST input.

    Taking the max here would advertise the file as ~2 months old when its
    pilot counts are from an annual publication stamped the previous Dec 31.
    """
    comps = av.get("asOfComponents")
    assert isinstance(comps, dict) and comps, "asOfComponents missing or empty"
    dates = []
    for name, c in comps.items():
        assert isinstance(c, dict), f"{name} is not an object"
        d = str(c.get("date", ""))
        assert ISO.match(d), f"{name}.date is not an ISO day: {d!r}"
        dates.append(d)
    assert av["data_date"] == min(dates), (
        f"data_date {av['data_date']} != MIN(components) {min(dates)}. If the "
        f"newest one looks right, re-read rule 2: a composite is as old as its "
        f"oldest input, and max() is how a stale leg hides behind a fresh one.")
    if len(dates) > 1:
        assert av["data_date"] != max(dates), (
            "every component shares one date — if that is genuinely true the "
            "prose should say so, otherwise a component was updated without "
            "its date")


def test_every_prose_vintage_has_a_component(av):
    """The prose and the machine dates must describe the SAME three sources.

    A component quietly dropped from `asOfComponents` would drop straight out
    of the min() and silently make the file look fresher than it is.
    """
    prose = str(av.get("asOf", ""))
    parts = [p.strip() for p in prose.split("·") if p.strip()]
    comps = av.get("asOfComponents") or {}
    assert len(parts) == len(comps), (
        f"`asOf` names {len(parts)} source vintage(s) {parts} but "
        f"asOfComponents has {len(comps)}: {sorted(comps)}")
    claimed = {str((c or {}).get("prose", "")).strip() for c in comps.values()}
    assert claimed == set(parts), (
        f"prose vintages {sorted(set(parts))} do not match the component "
        f"`prose` fields {sorted(claimed)}")


def test_no_component_is_dated_in_the_future(av):
    """A vintage newer than today is a typo, and a typo here reads as
    'freshly observed' on the dashboard."""
    today = date.today().isoformat()
    for name, c in (av.get("asOfComponents") or {}).items():
        assert str(c.get("date")) <= today, (
            f"{name} is dated {c.get('date')}, which is in the future")


def test_the_monitor_can_actually_read_it(av):
    """End-to-end against the real probe, not against our own reimplementation.

    This is the assertion that would have caught the original defect: the file
    could look perfectly well-stamped to a human and still resolve to UNKNOWN
    because the key name is not one the probe knows.
    """
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    bhs = pytest.importorskip("build_health_status")
    import time
    probe = bhs._content_age_probe(AVIATION, time.time())
    assert probe.age_h is not None, (
        f"build_health_status still cannot date data-aviation.json: "
        f"{probe.note}")
    assert probe.key is not None
    # And it must have resolved to the OLDEST component, not to something else
    # that happens to parse.
    resolved = bhs._parse_date_value(av[probe.key]).date().isoformat()
    assert resolved == av["data_date"], (
        f"the probe dated the file from {probe.key!r} -> {resolved}, but "
        f"data_date says {av['data_date']}")
