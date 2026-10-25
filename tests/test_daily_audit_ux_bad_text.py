"""The UX bad-text probe must not treat a headline word as a broken number.

Daily audit 2026-10-25 (AMBER, three P1s) flagged "Infinity" in
#overviewNewsHost, #newsFeed, and #researchNewsHost. The visible text was the
headline fragment "Infinity Ward Veteran Says" (Infinity Ward is a studio).
A metric that renders Infinity / -Infinity / Infinity% / $Infinity / NaN (and
undefined / null the same way) is still a P1.

The probe runs in the browser via Playwright's page.evaluate, so this test
executes the shipped function under Node with a small DOM stand-in. See
tests/check_daily_audit_bad_text.mjs for the cases.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / "tests" / "check_daily_audit_bad_text.mjs"


def test_infinity_in_a_headline_is_not_a_glitch_and_a_metric_still_is():
    node = shutil.which("node")
    if not node:
        pytest.fail("node is required to run scripts/daily_audit_ux.mjs probeBadText")
    proc = subprocess.run(
        [node, str(CHECK)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "bad-text probe: ok" in proc.stdout
