"""The health page must not be able to lie about the user's API keys again.

WHAT WENT WRONG
---------------
`api_status.py` probes ~50 upstreams and names an env var (`key_env`) for each
key-gated one. `pages.yml` ran that probe with an `env:` block listing FIVE of
the fourteen names it used. The other nine never reached the process, so
`os.environ.get(name)` returned nothing, so the snapshot recorded
`key_present: false`, so /health/apis.html printed "no key" next to Socrata,
Census, BLS, AirNow, FBI CDE, OpenSky and Reddit — for secrets that were
correctly configured in the repository the whole time.

The page was reporting the contents of a workflow env block while phrasing it as
a statement about the user's settings. "I'm looking at the status pages, they
used to be green, what the hell?" was a correct reading of an incorrect page.

WHAT MAKES IT NOT COME BACK
---------------------------
Three things, and this file tests all three:

1. `pages.yml` passes every key_env the probe names.  The list is re-derived
   here from `api_status.KEY_ENVS`, so the two cannot drift — which is the only
   durable fix, since the original bug was precisely a hand-copied list falling
   behind.
2. `api_status.key_state()` distinguishes "the variable arrived empty" (an
   unset secret — the user's problem) from "the variable never arrived at all"
   (a workflow bug — ours). Before this, both rendered as "no key", and that
   ambiguity is why nine broken rows looked like nine unconfigured sources.
3. `health/index.html` renders the third state differently from the second, so
   the distinction survives all the way to the reader.

Also covers the two other pages.yml commitments made alongside it: the
post-build `--mode built` health judgement, and committing the API status record
back to git instead of destroying it with the runner.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PAGES_YML = WORKFLOWS / "pages.yml"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def api_status():
    return _load("_api_status_under_test", REPO_ROOT / "api_status.py")


@pytest.fixture(scope="module")
def pages():
    return yaml.safe_load(PAGES_YML.read_text())


@pytest.fixture(scope="module")
def build_steps(pages):
    return pages["jobs"]["build"]["steps"]


def _step_running(steps, needle: str) -> dict:
    """The single build step whose `run:` invokes ``needle``."""
    hits = [s for s in steps if needle in (s.get("run") or "")]
    assert hits, f"no step in pages.yml runs {needle!r}"
    assert len(hits) == 1, f"{len(hits)} steps run {needle!r}; expected one"
    return hits[0]


# ==========================================================================
# 1. THE DEFECT ITSELF — every named key must be passed to the probe
# ==========================================================================

def test_pages_passes_every_key_env_api_status_names(api_status, build_steps):
    """THE regression test for the bug the user reported.

    Fails if api_status.py names a key_env that the workflow step running it
    does not map into `env:`. Derived from the module, never hand-listed.
    """
    step = _step_running(build_steps, "api_status.py")
    passed = set(step.get("env") or {})
    named = set(api_status.KEY_ENVS)
    missing = sorted(named - passed)
    assert not missing, (
        "api_status.py names these key_env vars but the 'Probe upstream API "
        f"reachability' step never passes them: {missing}. Every one will "
        "report key_present=false on /health/apis.html no matter how the "
        "repository secret is configured. Add them to that step's env: block."
    )


def test_the_probe_step_maps_each_key_from_the_matching_secret(build_steps):
    """`FOO: ${{ secrets.BAR }}` would be wired but wrong — the probe would
    report on a key the fetchers never use. Names must line up."""
    step = _step_running(build_steps, "api_status.py")
    for name, expr in (step.get("env") or {}).items():
        assert f"secrets.{name}" in expr, (
            f"{name} is mapped from {expr!r}, not from secrets.{name}")


def test_no_key_env_is_named_without_a_probe_that_uses_it(api_status):
    """KEY_ENVS is derived, not maintained. Guards against someone reverting it
    to a hand-kept list, which is the shape the original bug had."""
    derived = sorted({t["key_env"] for t in api_status.TARGETS if t.get("key_env")})
    assert api_status.KEY_ENVS == derived


def test_retired_keys_are_no_longer_claimed_by_the_probe(api_status):
    """Three keys unlocked nothing and were removed from TARGETS.

    SANTIMENT_API_KEY existed in this repo *only* as a key_env here — no
    workflow, no secret, and fetch_market.santiment_metrics() is keyless — so
    its whole effect was printing "auth required" forever for a key that gated
    nothing. COINGLASS/SOSOVALUE point at a dead ETF-flow fallback and a
    decommissioned host. Naming any of them again would drag them back into the
    workflow env block via the wiring test above, which is exactly backwards.
    """
    for dead in ("SANTIMENT_API_KEY", "COINGLASS_API_KEY", "SOSOVALUE_API_KEY"):
        assert dead not in api_status.KEY_ENVS, (
            f"{dead} is retired; naming it as a key_env re-creates a phantom "
            f"key and forces a pointless secret into pages.yml")


# ==========================================================================
# 2. THE SELF-DETECTION — unset and unwired must not look alike
# ==========================================================================

def test_key_state_separates_unset_from_never_wired(api_status, monkeypatch):
    """The observable is MEMBERSHIP, not truthiness.

    GitHub Actions materialises `FOO: ${{ secrets.FOO }}` as an env var set to
    the empty string when the secret does not exist. So an empty-but-present
    variable means "wired, secret not configured", and an absent variable means
    "nobody passed it" — a workflow bug. Collapsing both into False is what made
    the original defect invisible.
    """
    monkeypatch.setenv("PROBE_TEST_SET", "abc123")
    monkeypatch.setenv("PROBE_TEST_EMPTY", "")
    monkeypatch.setenv("PROBE_TEST_BLANK", "   \n")
    monkeypatch.delenv("PROBE_TEST_ABSENT", raising=False)

    assert api_status.key_state("PROBE_TEST_SET") == api_status.KEY_SET
    assert api_status.key_state("PROBE_TEST_EMPTY") == api_status.KEY_UNSET
    # Whitespace-only is not a key; matches check_secrets_present.py's .strip().
    assert api_status.key_state("PROBE_TEST_BLANK") == api_status.KEY_UNSET
    assert api_status.key_state("PROBE_TEST_ABSENT") == api_status.KEY_NOT_WIRED
    # A keyless source has nothing to classify.
    assert api_status.key_state(None) is None
    # The three states must be genuinely distinct strings.
    assert len({api_status.KEY_SET, api_status.KEY_UNSET,
                api_status.KEY_NOT_WIRED}) == 3


def test_key_present_still_answers_the_old_question(api_status, monkeypatch):
    """Kept for back-compat with already-deployed snapshots, but it must remain
    a strict summary of key_state, never an independent second opinion."""
    monkeypatch.setenv("PROBE_TEST_SET", "abc123")
    monkeypatch.setenv("PROBE_TEST_EMPTY", "")
    monkeypatch.delenv("PROBE_TEST_ABSENT", raising=False)
    for var, expected in (("PROBE_TEST_SET", True), ("PROBE_TEST_EMPTY", False),
                          ("PROBE_TEST_ABSENT", False)):
        assert (api_status.key_state(var) == api_status.KEY_SET) is expected


def test_a_probed_row_carries_the_state_and_the_variable_name(api_status,
                                                              monkeypatch):
    """The row must name WHICH variable is missing. 'some key is missing' sends
    the reader back to guessing, which is the state this whole change exits."""
    monkeypatch.delenv("PROBE_TEST_ABSENT", raising=False)
    target = {"label": "T", "category": "C",
              "url": "https://127.0.0.1:9/never", "key_env": "PROBE_TEST_ABSENT"}
    # timeout/attempts pinned low: this must not touch the network.
    row = api_status._probe_one(target, timeout=0.01, attempts=1)
    assert row["key_state"] == api_status.KEY_NOT_WIRED
    assert row["key_env"] == "PROBE_TEST_ABSENT"
    assert row["key_present"] is False
    assert row["needs_key"] is True


def test_a_keyless_row_makes_no_key_claim_at_all(api_status):
    target = {"label": "T", "category": "C",
              "url": "https://127.0.0.1:9/never", "key_env": None}
    row = api_status._probe_one(target, timeout=0.01, attempts=1)
    assert row["key_state"] is None
    assert row["key_present"] is None
    assert row["needs_key"] is False


def test_snapshot_summary_counts_and_names_the_unwired(api_status, monkeypatch):
    """A reader must be able to answer 'did my keys arrive?' from the summary,
    and 'which ones didn't?' without opening the table."""
    monkeypatch.setattr(api_status, "TARGETS", [
        {"label": "A", "category": "X", "url": "https://127.0.0.1:9/a",
         "key_env": "PROBE_TEST_SET"},
        {"label": "B", "category": "X", "url": "https://127.0.0.1:9/b",
         "key_env": "PROBE_TEST_EMPTY"},
        {"label": "C", "category": "X", "url": "https://127.0.0.1:9/c",
         "key_env": "PROBE_TEST_ABSENT"},
        {"label": "D", "category": "X", "url": "https://127.0.0.1:9/d",
         "key_env": None},
    ])
    monkeypatch.setenv("PROBE_TEST_SET", "abc123")
    monkeypatch.setenv("PROBE_TEST_EMPTY", "")
    monkeypatch.delenv("PROBE_TEST_ABSENT", raising=False)

    snap = api_status.probe_all(timeout=0.01, max_workers=4)
    s = snap["summary"]
    assert (s["keyed"], s["keys_set"], s["keys_unset"], s["keys_not_wired"]) \
        == (3, 1, 1, 1)
    assert snap["unwired_key_envs"] == ["PROBE_TEST_ABSENT"]
    assert "ci" in snap
    # JSON-serialisable: this is written to disk and fetched by the page.
    json.dumps(snap)


def test_the_snapshot_never_carries_a_secret_value(api_status, monkeypatch):
    """Load-bearing: this file is committed to a public repo and published to
    GitHub Pages. Names and presence only, exactly like check_secrets_present."""
    canary = "PLEASE-DO-NOT-PUBLISH-ME-4d19bc"
    monkeypatch.setattr(api_status, "TARGETS", [
        {"label": "A", "category": "X", "url": "https://127.0.0.1:9/a",
         "key_env": "PROBE_TEST_SET"},
    ])
    monkeypatch.setenv("PROBE_TEST_SET", canary)
    snap = api_status.probe_all(timeout=0.01, max_workers=2)
    assert canary not in json.dumps(snap)
    assert snap["sources"][0]["key_present"] is True


# ==========================================================================
# 3. THE DISTINCTION HAS TO REACH THE READER
# ==========================================================================

def test_health_page_renders_all_three_key_states_distinctly():
    """A per-row tag and a page-level banner. If the page collapsed 'not wired'
    back into the amber 'no key', the snapshot would be honest and the product
    would still be lying — which is the only version of this that matters to
    the person looking at it."""
    page = (REPO_ROOT / "health" / "index.html").read_text()
    assert "key_state" in page, "the page still reads only the old key_present"
    assert "not_wired" in page
    assert "keytag unwired" in page
    assert ".keytag.unwired" in page, "the third state needs its own styling"
    assert "unwired_key_envs" in page, "the page must name the missing variables"
    assert "renderKeyWiringBanner" in page


def test_health_page_still_renders_pre_change_snapshots():
    """The deployed page fetches a JSON that may predate this change (CDN cache,
    or a browser holding an old copy). It must not render blank rows for it."""
    page = (REPO_ROOT / "health" / "index.html").read_text()
    assert "src.key_state || (src.key_present ? 'set' : 'unset')" in page


# ==========================================================================
# 4. --mode built, wired into the deploy at last
# ==========================================================================

def test_built_mode_runs_in_pages_after_the_builds(build_steps):
    """`--mode built` judges the artifacts the build just produced, rather than
    their committed placeholders. It existed, worked, and was reachable only by
    workflow_dispatch — which is how the crypto breadth chart stayed frozen at
    2026-06-09 for three months while its file was rewritten hourly."""
    names = [s.get("name", "") for s in build_steps]
    step = _step_running(build_steps, "--mode built")
    idx = build_steps.index(step)

    def index_of(fragment):
        return next(i for i, n in enumerate(names) if fragment in n)

    # After both dashboards are generated — otherwise it judges last run's files.
    assert idx > index_of("Fetch live data"), "runs before the V1 build"
    assert idx > index_of("Generate V2 dashboard"), "runs before the V2 build"
    # Before staging, so the verdict is about what is actually being shipped.
    assert idx < index_of("Stage site directory")


def test_built_mode_cannot_block_the_deploy(build_steps):
    """A stale upstream must not withhold the whole site: the page is built to
    show a feed's real age, and serving the previous deploy instead is strictly
    more stale and strictly less honest."""
    step = _step_running(build_steps, "--mode built")
    assert step.get("continue-on-error") is True


def test_built_mode_is_loud_in_every_channel_that_survives_a_soft_failure(
        build_steps):
    """continue-on-error plus a log line is the diary entry nobody reads — the
    exact reporting channel that swallowed 30 consecutive TSA failures."""
    run = _step_running(build_steps, "--mode built")["run"]
    assert "::warning" in run, "no annotation; a soft-failed step's log is unread"
    assert "GITHUB_STEP_SUMMARY" in run, "no run-summary report"
    assert "exit $rc" in run, (
        "the real exit code must reach the step so it renders as a visible "
        "soft failure rather than an unbroken row of green ticks")


def test_built_mode_evaluates_exactly_once(build_steps):
    """Two evaluations can disagree, and then the annotations and the summary
    tell different stories about the same run. data-health.yml documents this
    trap; do not re-open it here."""
    run = _step_running(build_steps, "--mode built")["run"]
    # Count INVOCATIONS, not mentions — the step also imports the module to
    # borrow FAILING_STATUSES, which is a read, not a second verdict.
    invocations = [ln for ln in run.splitlines()
                   if ln.strip().startswith("python") and "data_health.py" in ln]
    assert len(invocations) == 1, invocations
    assert "--remediate" not in run, (
        "remediation re-runs fetchers, which would race the artifacts this job "
        "is about to stage")


def test_built_mode_explains_why_it_does_not_block(build_steps):
    step = _step_running(build_steps, "--mode built")
    blob = PAGES_YML.read_text()
    assert "NON-BLOCKING, AND HERE IS WHY" in blob, (
        "the non-blocking choice is the surprising part; it needs its reason "
        "written next to it")
    assert step.get("name")


def test_built_mode_borrows_the_failing_statuses_it_judges_by(build_steps):
    """Re-typing the failing-status list would let a newly added status go
    silently un-annotated — two monitors quietly disagreeing again."""
    run = _step_running(build_steps, "--mode built")["run"]
    assert "FAILING_STATUSES" in run


# ==========================================================================
# 5. the API status record survives the runner
# ==========================================================================

def _commit_step(steps) -> dict:
    hits = [s for s in steps if s.get("name") == "Commit the API status record"]
    assert hits, "pages.yml no longer commits data/health/api_status.json"
    return hits[0]


def test_api_status_record_is_committed_back(build_steps):
    """Written every build, staged to the site, then destroyed with the runner.
    The authoritative per-API status was unreadable from git, so nobody could
    answer 'when did this start failing?' after the fact."""
    step = _commit_step(build_steps)
    run = step["run"]
    assert "api-commit.sh" in run, "must use the signed commit helper"
    assert "[skip ci]" in run, "an unmarked commit would retrigger pages.yml"
    assert step.get("continue-on-error") is True, "must never block the deploy"
    assert "pull_request" in (step.get("if") or ""), \
        "a PR build must not commit to the default branch"


def test_the_commit_is_guarded_against_clobbering_a_good_record(build_steps):
    """The guard is the point: a failed or empty probe must never replace a real
    record with nothing."""
    run = _commit_step(build_steps)["run"]
    assert "summary.total" in run or "total < 1" in run
    assert "structurally empty" in run
    assert "unreadable" in run
    # And it must NOT require that something came back up: a sweep where every
    # upstream is down is the most interesting record there is.
    assert "reachable" not in run


def test_the_commit_does_not_churn_hourly(build_steps):
    """generated_at moves every run, so committing on any diff would be 24
    no-op commits a day — the noise that gets a bot's commits filtered out and
    then ignored."""
    run = _commit_step(build_steps)["run"]
    assert "substance" in run
    assert "MAX_RECORD_AGE_H" in run, (
        "without a heartbeat the committed generated_at drifts into meaning "
        "'when it last changed' rather than 'when we last checked'")


def test_api_status_json_is_un_ignored_deliberately():
    """data/health/ is gitignored as a build-cache directory; the record inside
    it is an exception, and git cannot re-include a path under an excluded
    DIRECTORY — the rule has to be the glob form for the carve-out to work."""
    ignore = (REPO_ROOT / ".gitignore").read_text()
    assert "data/health/*" in ignore, (
        "a trailing-slash `data/health/` rule makes the carve-out below it "
        "silently inert — git never descends into an excluded directory")
    assert "!data/health/api_status.json" in ignore

    def ignored(rel: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", rel],
                              cwd=REPO_ROOT).returncode == 0

    assert not ignored("data/health/api_status.json"), \
        "the record is still ignored; it would never be committed"
    # The sibling build cache stays ignored — it is recomputed from files
    # already in the repo, so committing it would just store a derivation.
    assert ignored("data/health/status.json")


def test_the_record_is_still_published_to_the_site(build_steps):
    """Committing it must not have replaced staging it: /health/ reads the
    deployed copy, not the git one."""
    stage = _step_running(build_steps, "cp dashboard.html _site/index.html")
    assert "_site/data/health/api_status.json" in stage["run"]
