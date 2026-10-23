"""Miami-Dade 311 publishes one frozen year. The pulse baseline is the 12
months before the latest month, so December 2023 has no baseline and z is
null. An 11-month window would compare that December to a mean that includes
the summer peak and excludes the previous December, which is not the
trailing year the other cities are scored on. The page says so.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APP = REPO / "app.py"


def _function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.index(marker)
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
    raise AssertionError(f"unterminated {name}")


def _call(fn_src: str, fn: str, arg):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to run the city-tab helper")
    prog = (
        fn_src
        + "\nprocess.stdout.write(JSON.stringify("
        + fn
        + "(...JSON.parse(process.argv[1]))));"
    )
    out = subprocess.run(
        [node, "-e", prog, json.dumps([arg])],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_miami_311_null_baseline_renders_as_insufficient_history():
    src = APP.read_text(encoding="utf-8")
    helper = _function(src, "cityMissingBaselineText")
    status = _function(src, "cityFeedStatusText")
    both = helper + "\n" + status

    miami = {
        "label": "Miami-Dade 311",
        "status": "stale",
        "z": None,
        "baseline_mean": None,
        "baseline_std": None,
        "note": "STALE: frozen 2023 snapshot",
    }
    assert _call(both, "cityMissingBaselineText", miami) == "insufficient history"
    # The metric cell uses the helper first, so the phrase wins over the note.
    # The note stays on its own row (the renderer concatenates it separately).
    assert "const missing = cityMissingBaselineText(f);" in src
    assert "const statusTxt = missing || cityFeedStatusText(f);" in src
    assert "missing && f && f.note" in src

    # A scored feed keeps its number. The helper returns null and the cell
    # falls through to the signed formatter, which is how every other city
    # already renders.
    scored = {"status": "ok", "z": 0.14, "baseline_mean": 5806.0}
    assert _call(both, "cityMissingBaselineText", scored) is None

    # Chicago's towed-vehicles row is an extended feed. Its chip stays
    # "Building 12-mo baseline"; this helper is not that chip.
    assert "case 'insufficient_history': return 'Building 12-mo baseline';" in _function(
        src, "cityExtStatusText")

    # not_published is a claim about the publisher, not about the window.
    unpublished = {"status": "not_published", "z": None, "baseline_mean": None}
    assert _call(both, "cityMissingBaselineText", unpublished) is None
    assert _call(both, "cityFeedStatusText", unpublished) == "Not published by this city"


def test_miami_snapshot_is_a_single_year_so_the_baseline_cannot_be_formed():
    data = json.loads((REPO / "data-city.json").read_text(encoding="utf-8"))
    miami = next(c for c in data["cities"] if c["id"] == "miami")
    feed = next(
        f for pl in miami["pulse"]["pillars"] for f in pl["feeds"]
        if f["label"] == "Miami-Dade 311"
    )
    months = [p["month"] for p in feed["series"]]
    assert months == [
        "2023-01", "2023-02", "2023-03", "2023-04", "2023-05", "2023-06",
        "2023-07", "2023-08", "2023-09", "2023-10", "2023-11", "2023-12",
    ]
    assert feed["status"] == "stale"
    assert feed["recent_period"] == "2023-12"
    assert feed["z"] is None
    assert feed["baseline_mean"] is None
    assert feed["baseline_std"] is None
    # The other two Miami pillars, and every other city's scored feed, still
    # carry a real z. This change is the label for a missing baseline.
    for city in data["cities"]:
        for pl in city["pulse"]["pillars"]:
            for other in pl["feeds"]:
                if other is feed or other.get("status") != "ok":
                    continue
                assert isinstance(other["z"], (int, float))
                assert isinstance(other["baseline_mean"], (int, float))
