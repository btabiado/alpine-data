"""`.github/scripts/json_content_changed.py`: skip bot commits that only move
run clocks (real-estate-daily committed 27 of 30 days with nothing but
generated_at / fetched_at changed)."""
from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "json_content_changed.py"
KEYS = ["--ignore-key", "generated_at", "--ignore-key", "fetched_at"]


@pytest.fixture(scope="module")
def jcc():
    spec = importlib.util.spec_from_file_location("json_content_changed", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.invalid")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "data").mkdir()
    doc = {"generated_at": "2026-10-05T06:00:00Z",
           "sources": {"zillow": {"fetched_at": "2026-10-05T06:00:01Z", "rows": 3}},
           "metros": [{"name": "A", "value": 1.5}]}
    (tmp_path / "data" / "re.json").write_text(json.dumps(doc))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _rewrite(repo: Path, **changes) -> None:
    p = repo / "data" / "re.json"
    doc = json.loads(p.read_text())
    doc["generated_at"] = "2026-10-06T06:00:00Z"
    doc["sources"]["zillow"]["fetched_at"] = "2026-10-06T06:00:02Z"
    for k, v in changes.items():
        doc[k] = v
    p.write_text(json.dumps(doc, separators=(",", ":")))   # formatting may differ too


def test_only_clocks_moved_means_skip(jcc, repo):
    _rewrite(repo)
    assert jcc.main(["data/re.json", *KEYS]) == 1


def test_a_real_value_change_means_commit(jcc, repo):
    _rewrite(repo, metros=[{"name": "A", "value": 1.6}])
    assert jcc.main(["data/re.json", *KEYS]) == 0


def test_without_ignore_keys_a_clock_change_is_a_change(jcc, repo):
    _rewrite(repo)
    assert jcc.main(["data/re.json"]) == 0


def test_identical_file_means_skip(jcc, repo):
    assert jcc.main(["data/re.json", *KEYS]) == 1


@pytest.mark.parametrize("case", ["new", "deleted", "not_json"])
def test_anything_unjudgeable_counts_as_changed(jcc, repo, case):
    if case == "new":
        (repo / "data" / "new.json").write_text("{}")
        assert jcc.main(["data/new.json", *KEYS]) == 0
        return
    if case == "deleted":
        (repo / "data" / "re.json").unlink()
    else:
        (repo / "data" / "re.json").write_text("{not json")
    assert jcc.main(["data/re.json", *KEYS]) == 0


def test_real_estate_workflow_skips_clock_only_commits():
    wf = yaml.safe_load((REPO_ROOT / ".github/workflows/real-estate-daily.yml").read_text())
    runs = "\n".join(s.get("run") or "" for s in wf["jobs"]["run"]["steps"])
    guard = runs.index("json_content_changed.py data/real_estate.json")
    assert "--ignore-key generated_at" in runs and "--ignore-key fetched_at" in runs
    # The guard runs before anything is staged or committed.
    assert guard < runs.index("git add data/real_estate.json") < runs.index("api-commit.sh")
