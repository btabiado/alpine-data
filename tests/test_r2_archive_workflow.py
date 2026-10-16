"""The pages upload and the r2-backfill dispatch that can refill 2026-10-07..today.

The 2026-10-15 audit's 81 missing copies are the retired v2/ keys. The
backfill cannot rebuild those keys from git (they were deploy-time outputs).
Delta mode for that range uploads any committed data-*.json a missed pages
run never archived, and it must fail the job when an upload fails instead of
logging a warning and exiting 0.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
BACKFILL = ROOT / ".github" / "workflows" / "r2-backfill.yml"
PAGES = ROOT / ".github" / "workflows" / "pages.yml"

# Exact dropdown values. A typo here dispatches the wrong mode.
DELTA = "delta (only upload missing files — fast on re-runs)"
CUSTOM = "custom (use start_date/end_date below)"


def _backfill_script() -> str:
    text = BACKFILL.read_text(encoding="utf-8")
    body = text.split("python3 - <<'PYEOF'", 1)[1].split("PYEOF", 1)[0]
    return textwrap.dedent(body)


def test_backfill_dispatch_covers_2026_10_07_through_the_present():
    text = BACKFILL.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    # PyYAML 1.1 parses the bare key `on` as boolean True.
    trigger = doc.get("on", doc.get(True))
    inputs = trigger["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["default"] == DELTA
    assert CUSTOM in inputs["preset"]["options"]
    assert "start_date" in inputs and "end_date" in inputs
    # The header is the operator instructions. These are the inputs for the
    # range the 2026-10-15 audit named, through the day this fix was written.
    for line in (
        "mode:       delta (only upload missing files — fast on re-runs)",
        "preset:     custom (use start_date/end_date below)",
        "start_date: 2026-10-07",
        "end_date:   2026-10-16",
        "max_days:   2000",
    ):
        assert line in text
    assert "Do not select refresh for this range." in text
    script = _backfill_script()
    compile(script, "r2-backfill.yml", "exec")
    assert "while cur <= end:" in script
    assert 'env["R2_SKIP_IF_PRESENT"] = "true"' in script
    assert "if failed:" in script and "sys.exit(1)" in script
    fail_lines = [ln for ln in script.splitlines() if "upload_to_r2.py exited" in ln]
    assert fail_lines and all("::error" in ln for ln in fail_lines)


def test_coverage_step_keeps_a_failed_scan_red_without_blocking_deploy():
    steps = yaml.safe_load(PAGES.read_text(encoding="utf-8"))["jobs"]["build"]["steps"]
    step = next(s for s in steps if s.get("name") == "Build R2 coverage dashboard")
    assert step.get("continue-on-error") is True
    assert 'exit "$cov_rc"' in step["run"]
    stage = next(i for i, s in enumerate(steps) if s.get("name") == "Stage site directory")
    assert steps.index(step) > stage


def test_coverage_script_exits_nonzero_when_the_bucket_is_unset(tmp_path, monkeypatch):
    import importlib.util
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("R2_BUCKET_NAME", raising=False)
    spec = importlib.util.spec_from_file_location(
        "build_r2_coverage_under_test", ROOT / "health" / "build_r2_coverage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1
    written = json.loads((tmp_path / "health" / "r2-coverage.json").read_text(encoding="utf-8"))
    assert written["empty_reason"]
