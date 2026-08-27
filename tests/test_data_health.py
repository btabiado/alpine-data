"""Watchdog honesty tests (``scripts/data_health.py``, ``build_health_status``).

The thing being guarded here is not "are the feeds fresh today" — that changes
hourly and is the monitor's job to report, not a test's job to assert. It is
"can the monitor still SEE every feed", which is the property that actually
failed: MUFON, stock money-flow and Summit news froze for two months while the
watchdog printed `OK: no unexpected staleness` and exited 0, purely because
they were absent from a hand-typed list.

So the load-bearing tests here are the coverage regression (a feed cannot leave
the manifest quietly) and the suppression expiry (a mute cannot be renewed by
inattention). Everything else defends a specific bug that has already shipped
at least once.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load(name: str):
    """Load a script by path — scripts/ is not a package."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bhs():
    return _load("build_health_status")


@pytest.fixture(scope="module")
def dh():
    return _load("data_health")


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc).timestamp()


def _write(tmp_path: Path, name: str, payload) -> Path:
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return p


# ==========================================================================
# C. the age parser — every entry below is a key that was invisible in prod
# ==========================================================================

def test_compiled_at_is_recognised(bhs, tmp_path):
    """data/ai_curated.json carries ONLY `compiled_at`. Before this fix it
    resolved to None, which the old watchdog scored as 'undetermined' and then
    exited 0 on — an 80-day-old feed reported as not-a-problem."""
    p = _write(tmp_path, "ai_curated.json", {"compiled_at": "2026-05-15T00:00:00Z"})
    probe = bhs._content_age_probe(p, NOW)
    assert probe.key == "compiled_at"
    assert probe.age_h == pytest.approx(80 * 24 + 12, abs=1)


def test_camelcase_keys_resolve_like_snake_case(bhs, tmp_path):
    for spelling in ("as_of", "asOf", "AsOf", "as-of"):
        p = _write(tmp_path, f"{spelling.replace('-', '_')}.json",
                   {spelling: "2026-08-01T12:00:00Z"})
        probe = bhs._content_age_probe(p, NOW)
        assert probe.key == spelling, f"{spelling} not recognised"
        assert probe.age_h == pytest.approx(48, abs=0.1)


def test_prose_date_resolves_to_unknown_not_a_wrong_date(bhs, tmp_path):
    """data-aviation.json's real value. A lenient parser would pull "Dec 31
    2025" out of this and publish a date the file never asserted. A wrong date
    is worse than no date: wrong gets believed, missing gets investigated."""
    prose = ("FAA airman data Dec 31 2025 · FAA aircraft registry late "
             "May 2026 · market snapshot late May 2026")
    p = _write(tmp_path, "aviation.json", {"asOf": prose, "registryTotal": 306985})
    probe = bhs._content_age_probe(p, NOW)
    assert probe.age_h is None
    assert probe.key is None
    # ...and it must SAY the key was found but unusable, not just shrug.
    assert "asOf" in probe.note and "unparseable" in probe.note


def test_prose_is_rejected_even_when_it_starts_with_a_year(bhs, tmp_path):
    p = _write(tmp_path, "x.json", {"as_of": "2026 vintage, refreshed sometimes"})
    assert bhs._content_age_probe(p, NOW).age_h is None


def test_allowlist_drift_reports_the_key_it_did_not_know(bhs, tmp_path):
    """The self-detection clause: an unrecognised date key must name itself."""
    p = _write(tmp_path, "drifted.json", {"harvested_on": "2026-01-01T00:00:00Z"})
    probe = bhs._content_age_probe(p, NOW)
    assert probe.age_h is None
    assert probe.drift_keys == ["harvested_on"]
    assert "harvested_on" in probe.note


def test_drift_scan_reaches_nested_keys(bhs, tmp_path):
    """data/ai_curated_wiki.json's only date lives at <Company>.fetched_at."""
    p = _write(tmp_path, "wiki.json", {"Anthropic": {"fetched_at": "2026-08-02T19:00:00Z"}})
    probe = bhs._content_age_probe(p, NOW)
    assert probe.age_h is None
    assert probe.drift_keys == ["Anthropic.fetched_at"]


def test_no_drift_report_when_a_recognised_key_worked(bhs, tmp_path):
    """A healthy file must not emit drift noise just because it has extra dates."""
    p = _write(tmp_path, "ok.json", {"generated_at": "2026-08-03T00:00:00Z",
                                     "some_other_date": "2020-01-01"})
    probe = bhs._content_age_probe(p, NOW)
    assert probe.key == "generated_at"
    assert probe.drift_keys == []


def test_tstr_keeps_its_clock_time(bhs, tmp_path):
    """`2026-08-02 21:13 UTC` used to fall through to a date-only parse, which
    back-dated it to midnight and could add ~24 phantom hours to an HOURLY feed
    measured against a 24h threshold — a false alarm made by the parser."""
    p = _write(tmp_path, "opensky.json", {"tstr": "2026-08-02 21:13 UTC"})
    probe = bhs._content_age_probe(p, NOW)
    assert probe.age_h == pytest.approx(14.78, abs=0.05)


def test_drift_scan_is_bounded_on_a_huge_payload(bhs, tmp_path):
    """data-mufon.json holds 144k records; an unbounded walk would turn a cheap
    check into a minute of CPU on every run."""
    payload = {"records": [{"id": i, "seen": "2026-01-0" + str(i % 9 + 1)}
                           for i in range(20000)]}
    p = _write(tmp_path, "big.json", payload)
    started = datetime.now()
    bhs._content_age_probe(p, NOW)
    assert (datetime.now() - started).total_seconds() < 5


def test_non_dict_and_broken_json_say_why(bhs, tmp_path):
    assert bhs._content_age_probe(_write(tmp_path, "a.json", [1, 2]), NOW).note
    p = tmp_path / "b.json"
    p.write_text("{not json")
    assert bhs._content_age_probe(p, NOW).note


def test_csv_age_comes_from_the_last_row(bhs, tmp_path):
    p = tmp_path / "flows.csv"
    p.write_text("date,Total\n2026-07-01,10\n2026-08-01,20\n")
    probe = bhs._content_age_probe(p, NOW)
    assert probe.age_h == pytest.approx(60, abs=0.1)


def test_content_age_h_wrapper_still_returns_a_float_or_none(bhs, tmp_path):
    """Back-compat: build_health_status.scan() and any external caller."""
    p = _write(tmp_path, "c.json", {"generated_at": "2026-08-03T00:00:00Z"})
    assert isinstance(bhs._content_age_h(p, NOW), float)
    assert bhs._content_age_h(_write(tmp_path, "d.json", {}), NOW) is None


# ==========================================================================
# C (live): the two files named in the bug report, against the real tree
# ==========================================================================

def test_real_ai_curated_is_now_visible(bhs):
    p = REPO_ROOT / "data" / "ai_curated.json"
    if not p.exists():
        pytest.skip("data/ai_curated.json not checked out")
    probe = bhs._content_age_probe(p, datetime.now(timezone.utc).timestamp())
    assert probe.age_h is not None, (
        "ai_curated.json resolved to no date again — its compiled_at stamp is "
        "the only signal it has")


def test_real_aviation_is_now_dateable_without_guessing(bhs):
    """UPDATED BY THE DATA LANE. This used to assert `probe.age_h is None` —
    that data-aviation.json stays UNKNOWN because its only date signal is the
    prose `asOf`. The SUPPRESSIONS note in data_health.py already named the
    right resolution ("the fix is a machine-readable stamp in the file"), and
    that stamp has now landed: the file carries `data_date`, the MIN of the
    three component vintages named in the prose. The min-not-max derivation is
    pinned separately in tests/test_aviation_vintage.py.

    What must NOT regress is the reason the old assertion existed: the prose is
    still refused outright, so nothing is fishing "Dec 31 2025" out of the
    middle of a sentence. The date now comes from a field that asserts one.

    FOR THE MONITOR LANE: the status moves UNKNOWN -> STALE (~215d) — still
    failing, but actionable. FAA Civil Airmen Statistics is an ANNUAL
    publication, so this feed wants a `limit_h` (~400d) in MANIFEST; on the
    default threshold it is a permanent red for reasons no fetcher can fix.
    """
    p = REPO_ROOT / "data-aviation.json"
    if not p.exists():
        pytest.skip("data-aviation.json not checked out")
    probe = bhs._content_age_probe(p, datetime.now(timezone.utc).timestamp())
    assert probe.age_h is not None, (
        f"data-aviation.json lost its machine-readable stamp: {probe.note}")
    assert probe.key == "data_date", (
        f"dated from {probe.key!r}; expected the explicit `data_date` field")
    # The prose vintage is still not a date, and must never become one.
    assert bhs._parse_date_value(json.loads(p.read_text())["asOf"]) is None, (
        "the prose `asOf` started parsing — a lenient parser here publishes a "
        "date the file never asserts")


# ==========================================================================
# D. nested dates — container freshness is not contents freshness
# ==========================================================================

def test_nested_path_selection_syntax(dh):
    data = {"cities": [{"h": {"u": "a"}}, {"h": {"u": "b"}}],
            "sources": {"x": {"f": "c"}, "y": {"f": "d"}},
            "flat": "e"}
    assert dh._select(data, ["cities[]", "h", "u"]) == ["a", "b"]
    assert sorted(dh._select(data, ["sources", "*", "f"])) == ["c", "d"]
    assert dh._select(data, ["flat"]) == ["e"]
    assert dh._select(data, ["nope"]) == []
    assert dh._select(data, ["cities[]", "missing"]) == []


def test_nested_age_takes_the_oldest_not_the_newest(dh, tmp_path):
    """Rule 2 of the freshness contract, applied to a container's contents."""
    p = _write(tmp_path, "city.json", {"cities": [
        {"data_health": {"last_updated": "2026-08-01T00:00:00+00:00"}},
        {"data_health": {"last_updated": "2023-12-01T00:00:00+00:00"}},
    ]})
    age, where = dh.nested_age_h(p, NOW, ("cities[].data_health.last_updated",))
    assert where == "cities[].data_health.last_updated"
    assert age == pytest.approx(976.5 * 24, abs=24)


def test_container_fresh_contents_stale_reports_the_contents(dh, tmp_path,
                                                             monkeypatch):
    """THE bug: data-city.json reads 13.6h fresh from its top-level
    generated_at while every city inside it stopped updating months ago.
    Identical in shape to the breadth freeze the dashboard just fixed."""
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    _write(tmp_path, "data-city.json", {
        "generated_at": "2026-08-03T00:00:00Z",
        "cities": [{"data_health": {"last_updated": "2026-04-01T00:00:00+00:00"}}],
    })
    results = {r.path: r for r in dh.evaluate(dh.COMMITTED, now_ts=NOW)}
    city = results["data-city.json"]
    assert city.status == dh.STALE
    assert city.age_h > 120 * 24
    assert "only as fresh as its oldest input" in city.detail


def test_a_feed_whose_only_nested_date_is_a_clock_reports_unknown(
        dh, tmp_path, monkeypatch):
    """data/ai_curated_wiki.json has no top-level stamp, and its only nested
    candidate is `fetched_at` — a CLOCK, written by wiki_enrich.py from
    datetime.fromtimestamp(now_epoch) on every run regardless of whether any
    data moved.

    An earlier revision wired that path so the feed would resolve to OK, on the
    reasoning that some signal beats none. It does not. A clock makes the feed
    permanently, confidently fresh: it can never age, so it can never alarm,
    so the monitor is actively lying about it rather than merely silent.

    UNKNOWN is the honest answer, and UNKNOWN fails the run (hole #4) — so the
    feed still gets attention, it just gets it under a true label.
    """
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    # Deliberately ancient contents with a brand-new fetch clock: the exact
    # shape the removed path would have reported as 6 hours fresh.
    _write(tmp_path, "data/ai_curated_wiki.json",
           {"OpenAI": {"fetched_at": "2026-08-03T06:00:00Z", "summary": "..."},
            "Anthropic": {"fetched_at": "2026-08-03T06:00:00Z"}})
    results = {r.path: r for r in dh.evaluate(dh.BUILT, now_ts=NOW)}
    row = results["data/ai_curated_wiki.json"]
    assert row.status == dh.UNKNOWN, (
        "a fetch clock must not be accepted as a freshness signal")
    assert row.fails, "UNKNOWN must fail the run, not pass quietly"
    assert "data/ai_curated_wiki.json" not in dh.NESTED_DATE_PATHS


def test_nested_date_sweep_is_documented(dh):
    """A sweep of every feed for nested dates found ten candidates. Five are
    wired; the rest are deliberately NOT, for two distinct reasons.

    Some are real-world EVENT dates (when an advisory was issued, when a
    funding round closed, when an article was published) rather than
    provenance. Wiring those would alarm on data that is perfectly correct —
    the opposite failure, but still a monitor nobody trusts.

    Others are FETCH CLOCKS. Those are worse than useless as a freshness
    signal: a clock advances on every run whether or not the data moved, so
    wiring one makes the feed permanently, confidently fresh. Two were wired
    and a gate caught both; see the entries below.

    This test pins the decision so a future sweep does not silently re-add
    them, and so the reasoning is in the repo rather than in a PR comment.
    """
    assert set(dh.NESTED_DATE_PATHS) == {
        "data-city.json",
        "data-mufon.json",
        "data-whale.json",
        "data-travel-fetch-status.json",
        "snowflake_summit/vendors.json",
    }
    deliberately_excluded = {
        "data-travel.json": "advisories[].date — issue date of an advisory",
        "data/ai_curated.json": "top_funded_companies[].last_round_date",
        "snowflake_summit/news.json": "items[].date — article publication date",
        "data-aviation.json": "live.seed.tstr — one component of a "
                              "multi-vintage file; the FAA airman roll is older",
        # Both REMOVED after a gate proved they were fetch clocks, not data
        # dates. Rule 1 is not negotiable even when a clock is the only thing
        # on offer: no signal reports UNKNOWN, a clock reports a lie.
        "data/real_estate.json":
            "sources.*.fetched_at — fetch_real_estate.py writes "
            "zillow.fetched_at and redfin.fetched_at from two _now_iso() "
            "calls in the same payload literal, unconditionally, alongside "
            "the top-level generated_at. They cannot diverge from the "
            "container stamp, so the 'notices one source stale-keeping' "
            "justification was provably false.",
        "data/ai_curated_wiki.json":
            "*.fetched_at — wiki_enrich.py writes it from "
            "datetime.fromtimestamp(now_epoch) every run. It was this feed's "
            "ONLY nested signal, so resolve_age took it outright rather than "
            "as a floor, and the feed could never age.",
    }
    for rel in deliberately_excluded:
        assert rel not in dh.NESTED_DATE_PATHS
        assert rel in dh.MANIFEST, f"{rel} must still be watched at top level"


# ==========================================================================
# A/F. coverage — THE regression. A feed must not be able to leave quietly.
# ==========================================================================

def _tracked_data_artifacts() -> set[str]:
    """Data artifacts according to git, not according to the manifest.

    Deriving the expectation from `git ls-files` is the whole point: a list
    that both sides maintain by hand cannot detect the two of them drifting
    apart, which is exactly how MUFON went unwatched.
    """
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    keep: set[str] = set()
    for rel in out:
        if rel.startswith("data/.stale/") or rel.startswith("data/lthcs/"):
            continue  # delegated directories, declared as such
        if re.fullmatch(r"data-[a-z0-9_-]+\.json", rel):
            keep.add(rel)
        elif re.fullmatch(r"data/[a-z0-9_]+\.(json|csv)", rel):
            keep.add(rel)
        elif re.fullmatch(r"snowflake_summit/[a-z0-9_]+\.json", rel):
            keep.add(rel)
        elif rel.startswith("data/composites/"):
            keep.add("data/composites/")
    return keep


def test_every_committed_data_artifact_is_in_the_manifest(dh):
    """FAILS if a feed silently leaves the manifest, or arrives without one.

    This is the test that would have caught the June freeze cluster on the day
    those feeds were added, instead of eight weeks later by hand.
    """
    missing = sorted(_tracked_data_artifacts() - set(dh.MANIFEST))
    assert not missing, (
        "these committed data artifacts are not classified in "
        f"data_health.MANIFEST: {missing}. An unclassified feed is "
        "indistinguishable from a healthy one.")


def test_manifest_does_not_reference_vanished_files(dh):
    """The mirror image: a manifest entry for a file git no longer has is an
    alarm that can never clear, and a permanently-red monitor gets ignored."""
    tracked = _tracked_data_artifacts()
    for rel, feed in dh.MANIFEST.items():
        if feed.kind in (dh.BUILT, dh.DELEGATED):
            continue  # gitignored by design / directory
        assert rel in tracked or (REPO_ROOT / rel).exists(), (
            f"{rel} is declared {feed.kind} but is neither tracked by git nor "
            f"present on disk")


def test_discover_finds_new_data_subdirectories(dh, tmp_path, monkeypatch):
    """A whole new directory of feeds escaping coverage is the same hole as one
    file escaping it, only bigger."""
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    (tmp_path / "data" / "brandnew").mkdir(parents=True)
    (tmp_path / "data" / "brandnew" / "x.json").write_text("{}")
    assert "data/brandnew/" in dh.discover()
    unwatched = [r for r in dh.evaluate(dh.COMMITTED, now_ts=NOW)
                 if r.status == dh.UNWATCHED and r.path == "data/brandnew/"]
    assert unwatched, "a new data/ subdirectory must trip the unwatched check"


def test_unwatched_file_fails_the_run(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    _write(tmp_path, "data-brandnew.json", {"generated_at": "2026-08-03T00:00:00Z"})
    results = dh.evaluate(dh.COMMITTED, now_ts=NOW)
    row = next(r for r in results if r.path == "data-brandnew.json")
    assert row.status == dh.UNWATCHED and row.fails


def test_manifest_is_structurally_sound(dh):
    assert dh.verify_manifest() == []


def test_static_and_delegated_entries_must_justify_themselves(dh):
    """An exemption without a written reason is an exemption nobody can audit."""
    for rel, feed in dh.MANIFEST.items():
        if feed.kind in (dh.STATIC, dh.DELEGATED):
            assert len(feed.justification.strip()) > 40, (
                f"{rel} is exempt from monitoring with no real justification")


def test_us_states_is_static_not_a_daily_feed(dh):
    """Regression on a manifest error that was live in the draft: marked as a
    city-daily.yml feed, data-us_states.json would have reported UNKNOWN
    forever. city-daily.yml commits only data-city.json, and the file holds SVG
    path strings with no date field because it has no refresh cadence."""
    assert dh.MANIFEST["data-us_states.json"].kind == dh.STATIC


# ==========================================================================
# F. suppressions expire — a mute is a loan, not a grant
# ==========================================================================

def test_no_suppression_is_past_its_expiry(dh):
    """FAILS THE BUILD the day a mute runs out.

    That failure is the feature. `KNOWN_BLOCKED` in the old watchdog muted the
    crypto ETF CSVs with "needs COINGLASS_API_KEY; free mirror is dead" — a
    blocker PR #23 removed by shipping a keyless Farside fetcher. Nothing
    re-examined it, so the mute would have gone on hiding failures of the NEW
    fetcher indefinitely. A suppression that cannot expire is a permanent
    blind spot with a comment attached.
    """
    today = date.today()
    expired = {rel: s.until.isoformat()
               for rel, s in dh.SUPPRESSIONS.items() if s.until < today}
    assert not expired, (
        f"expired suppressions: {expired}. Re-validate the blocker, then fix "
        f"it or consciously extend the date — do not extend it reflexively.")


def test_suppressions_are_not_open_ended(dh):
    """A mute five years out is an un-expiring mute with extra steps."""
    horizon = date.today() + timedelta(days=180)
    for rel, s in dh.SUPPRESSIONS.items():
        assert s.until <= horizon, f"{rel} is muted until {s.until} — too far out"
        assert len(s.reason.strip()) > 40, f"{rel} has no real reason"


def test_expired_suppression_fails_instead_of_muting(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    monkeypatch.setitem(dh.SUPPRESSIONS, "data-tsa.json",
                        dh.Suppression(reason="x" * 50, until=date(2026, 1, 1)))
    _write(tmp_path, "data-tsa.json", {"generated": "2026-01-01T00:00:00Z"})
    row = next(r for r in dh.evaluate(dh.COMMITTED, today=date(2026, 8, 3), now_ts=NOW)
               if r.path == "data-tsa.json")
    assert row.status == dh.EXPIRED and row.fails
    assert "SUPPRESSION EXPIRED" in row.detail


def test_live_suppression_reports_without_failing(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    monkeypatch.setitem(dh.SUPPRESSIONS, "data-tsa.json",
                        dh.Suppression(reason="x" * 50, until=date(2026, 12, 1)))
    _write(tmp_path, "data-tsa.json", {"generated": "2026-01-01T00:00:00Z"})
    row = next(r for r in dh.evaluate(dh.COMMITTED, today=date(2026, 8, 3), now_ts=NOW)
               if r.path == "data-tsa.json")
    assert row.status == dh.SUPPRESSED and not row.fails


def test_every_suppression_names_a_real_feed(dh):
    for rel in dh.SUPPRESSIONS:
        assert rel in dh.MANIFEST


# ==========================================================================
# A. the four other holes in the old watchdog
# ==========================================================================

def test_unknown_is_a_failure(dh, tmp_path, monkeypatch):
    """Hole #4. A feed whose date signal has become unreadable is the exact
    symptom of an upstream schema change, and it used to exit 0."""
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    _write(tmp_path, "data-mufon.json", {"total_records": 5})
    row = next(r for r in dh.evaluate(dh.COMMITTED, now_ts=NOW)
               if r.path == "data-mufon.json")
    assert row.status == dh.UNKNOWN and row.fails


def test_built_artifacts_are_checked_in_built_mode(dh, tmp_path, monkeypatch):
    """Hole #3. 'Fresh by construction' was backwards: fetch_market rewrites
    these hourly while the series inside them stale-keep."""
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    _write(tmp_path, "data-cpi.json", {"generated_at": "2026-01-01T00:00:00Z"})
    committed = [r for r in dh.evaluate(dh.COMMITTED, now_ts=NOW)
                 if r.path == "data-cpi.json"]
    assert committed == [], "BUILT files must not be judged against a placeholder"
    built = next(r for r in dh.evaluate(dh.BUILT, now_ts=NOW)
                 if r.path == "data-cpi.json")
    assert built.status == dh.STALE and built.fails


def test_series_feed_is_judged_on_its_newest_member(dh, tmp_path, monkeypatch):
    """data/composites/ rots by simply not gaining a new file."""
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    for day, gen in (("2026-06-01", "2026-06-01T00:00:00Z"),
                     ("2026-08-03", "2026-08-03T06:00:00Z")):
        _write(tmp_path, f"data/composites/{day}.json", {"generated_at": gen})
    row = next(r for r in dh.evaluate(dh.COMMITTED, now_ts=NOW)
               if r.path == "data/composites/")
    assert row.status == dh.OK
    assert "2026-08-03.json" in row.source


def test_empty_series_directory_is_unknown_not_ok(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    (tmp_path / "data" / "composites").mkdir(parents=True)
    row = next(r for r in dh.evaluate(dh.COMMITTED, now_ts=NOW)
               if r.path == "data/composites/")
    assert row.fails


def test_missing_committed_file_fails(dh, tmp_path, monkeypatch):
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    statuses = {r.path: r.status for r in dh.evaluate(dh.COMMITTED, now_ts=NOW)}
    assert statuses["data-tsa.json"] == dh.MISSING


def test_json_report_is_machine_readable(dh, capsys):
    assert dh.main(["--report", "json"]) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "committed"
    assert isinstance(payload["healthy"], bool)
    assert all({"path", "status", "age_h", "owner"} <= set(r)
               for r in payload["results"])


def test_issue_body_names_what_broke(dh, capsys):
    dh.main(["--report", "issue"])
    body = capsys.readouterr().out
    assert "| feed | status | age | limit | owner |" in body
    assert "data-health.yml" in body


def test_exit_code_tracks_the_verdict(dh, tmp_path, monkeypatch, capsys):
    """Hole #5's precondition: the alarm has to fire at all."""
    monkeypatch.setattr(dh, "REPO_ROOT", tmp_path)
    (tmp_path / "data").mkdir()
    assert dh.main([]) == 1
    assert "FAIL:" in capsys.readouterr().out


# ==========================================================================
# B. the retirement, and the workflow that replaces it
# ==========================================================================

def test_the_old_watchdog_is_gone_and_unreferenced():
    """Two watchdogs that can disagree about 'stale' is worse than one."""
    assert not (SCRIPTS / "check_data_freshness.py").exists()
    assert not (WORKFLOWS / "data-freshness-check.yml").exists()
    hits = subprocess.run(
        ["git", "grep", "-l", "check_data_freshness", "--",
         ":!scripts/data_health.py", ":!tests/test_data_health.py"],
        cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()
    assert hits == [], f"still referenced by {hits}"


def test_data_health_workflow_is_valid_and_wired():
    wf = yaml.safe_load((WORKFLOWS / "data-health.yml").read_text())
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = wf.get("on", wf.get(True))
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    # The issue IS the alarm channel; without this permission it silently
    # degrades back into a red check on a page nobody opens.
    assert wf["permissions"]["issues"] == "write"
    steps = wf["jobs"]["check"]["steps"]
    body = "\n".join(s.get("run", "") for s in steps)
    assert "scripts/data_health.py" in body
    # The check step must not hard-fail, or the reporting steps never run.
    check = next(s for s in steps if s.get("id") == "check")
    assert "set +e" in check["run"]


def test_no_workflow_still_calls_the_retired_script():
    for wf in WORKFLOWS.glob("*.yml"):
        assert "check_data_freshness" not in wf.read_text(), wf.name


# ==========================================================================
# E. secrets — presence only, never a value
# ==========================================================================

REQUIRED_NEW_SECRETS = [
    "COINGECKO_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
    "OPENSKY_CLIENT_ID", "OPENSKY_CLIENT_SECRET", "ANTHROPIC_API_KEY",
    "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME", "SEC_USER_AGENT",
    "SECURITY_AUDIT_TOKEN",
]


@pytest.fixture(scope="module")
def secrets_mod():
    return _load("check_secrets_present")


def test_all_referenced_secrets_are_audited(secrets_mod):
    known = {name for name, _, _ in secrets_mod.KEYS}
    assert set(REQUIRED_NEW_SECRETS) <= known


def test_secrets_check_workflow_maps_every_audited_key(secrets_mod):
    """A key the audit knows about but the workflow does not map reads MISSING
    whether or not it is set — a confidently wrong answer."""
    text = (WORKFLOWS / "secrets-check.yml").read_text()
    for name, _, _ in secrets_mod.KEYS:
        assert f"{name}:" in text, f"{name} is audited but never mapped into env"


def _workflows_wiring(name: str) -> set[str]:
    """Workflow stems that actually WIRE ``name``, ignoring prose about it.

    The naive version of this — `name in wf.read_text()` — cannot tell a live
    `FOO: ${{ secrets.FOO }}` mapping from a comment explaining why FOO was
    retired. That matters, because the comment explaining a removal is the most
    valuable thing on a removed key, and a check that forbids writing one
    pressures the next person to delete the explanation instead of the key.
    Full-line comments are stripped; secrets-check.yml is excluded because it
    maps every audited key by definition.
    """
    out = set()
    for wf in WORKFLOWS.glob("*.yml"):
        if wf.stem == "secrets-check":
            continue
        code = "\n".join(ln for ln in wf.read_text().splitlines()
                         if not ln.lstrip().startswith("#"))
        if name in code:
            out.add(wf.stem)
    return out


def test_wiring_scan_ignores_comments_but_not_code():
    """The helper above is load-bearing for the annotation test; prove it can
    still SEE a real mapping, or the annotation test passes vacuously."""
    assert "pages" in _workflows_wiring("FRED_API_KEY")
    # And a key named only in prose is not counted as wired.
    #
    # This used to assert COINGECKO_API_KEY was wired NOWHERE, which stopped
    # being true once it was genuinely wired into pages.yml and
    # lthcs-crypto-daily.yml. CRYPTOCOMPARE_API_KEY is the live example of the
    # same property, and a sharper one: lthcs-crypto-daily.yml names it in the
    # comment explaining why it was removed, while mapping it nowhere. So the
    # scanner must report `pages` (where it IS mapped) and must NOT report
    # lthcs-crypto-daily — proving it reads the env block, not the prose above
    # it, within a single file.
    cryptocompare = _workflows_wiring("CRYPTOCOMPARE_API_KEY")
    assert "pages" in cryptocompare
    assert "lthcs-crypto-daily" not in cryptocompare


def test_secret_workflow_annotations_match_reality(secrets_mod):
    """Re-derives the 'expected by' column from the workflow files.

    The column's only value is being true. An UNWIRED_PREFIXES annotation —
    '(not yet wired…)' or '(retired…)' — must mean the key appears in no
    workflow except secrets-check.yml itself; anything else must name workflows
    that really reference it.

    Reads the prefixes from the module rather than hard-coding them, so adding a
    new annotation class cannot leave this check silently matching nothing.
    """
    for name, _, where in secrets_mod.KEYS:
        users = _workflows_wiring(name)
        if secrets_mod._is_unwired(where):
            assert not users, (
                f"{name} is annotated {where!r} but {sorted(users)} reference "
                f"it — either re-wire the annotation or drop the reference")
        else:
            claimed = {w.strip() for w in where.split(",")}
            assert claimed <= users, (
                f"{name} claims {sorted(claimed - users)}, which do not "
                f"reference it. Real users: {sorted(users)}")


def test_retired_keys_are_labelled_retired_not_pending(secrets_mod):
    """'(not yet wired)' and '(retired)' are different promises.

    The first says "wait, this is coming". The second says "stop waiting". Three
    keys — COINGECKO, COINGLASS, SOSOVALUE — sat in the first category for
    months while being, in fact, in the second: COINGECKO was passed by
    lthcs-crypto-daily.yml to code that reads no env var at all, and the two ETF
    keys point at a fallback path and a decommissioned host. Mislabelling a dead
    key as pending is how a user keeps a useless secret rotated for years.

    COINGECKO_API_KEY is deliberately NOT in the list below any more. It was
    genuinely dead when this test was written; it has since been wired for real
    — crypto_data.py and fetch_market.py read it and send x-cg-demo-api-key, and
    both pages.yml and lthcs-crypto-daily.yml map it. Un-retirement is the one
    legitimate way off this list, and it is a code change, not a relabel:
    test_secret_workflow_annotations_match_reality re-derives the column from
    the workflow files, so a key cannot claim a workflow that does not map it.
    The mirror-image error is now the live risk — leaving a working key labelled
    dead tells the reader to stop waiting for something already running.
    """
    for name in ("COINGLASS_API_KEY", "SOSOVALUE_API_KEY"):
        where = next(w for n, _, w in secrets_mod.KEYS if n == name)
        assert where.startswith("(retired"), (
            f"{name} is a dead key and must be annotated '(retired…)', not "
            f"{where!r}")


def test_the_documented_false_alarms_stay_documented(secrets_mod):
    """These three ARE set and DO work; they just arrive under another name.

    Renaming a secret or 'fixing' the workflow to pass the literal name the code
    appears to want would break a working upload / a working audit in order to
    quiet a report that was never describing a real problem. The alias note is
    the only thing standing between a reader and that mistake, so it is a test,
    not a comment.
    """
    for name in ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME",
                 "SECURITY_AUDIT_TOKEN"):
        assert name in secrets_mod.ALIASES, (
            f"{name} reaches its consumer under a different env name; without "
            f"an ALIASES note a MISSING row sends the reader to re-paste a "
            f"secret that was never the problem")
    assert "AWS_SECRET_ACCESS_KEY" in secrets_mod.ALIASES["R2_SECRET_ACCESS_KEY"]
    assert "GH_TOKEN" in secrets_mod.ALIASES["SECURITY_AUDIT_TOKEN"]
    # And the reasoning lives in the file, where someone about to "fix" it looks.
    src = (SCRIPTS / "check_secrets_present.py").read_text()
    assert "FALSE ALARMS" in src


def test_secret_values_are_never_printed(secrets_mod, capsys, monkeypatch,
                                         tmp_path):
    """Load-bearing: workflow logs are world-readable on a public repo."""
    canary = "PLEASE-DO-NOT-PRINT-ME-9f3a1c"
    for name, _, _ in secrets_mod.KEYS:
        monkeypatch.setenv(name, canary)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    secrets_mod.main()
    out = capsys.readouterr().out
    assert canary not in out
    assert canary not in summary.read_text()
    # ...and it must still report the observable it exists to report.
    assert f"{len(canary)}" in out


def test_secrets_check_is_a_diagnostic_not_a_gate(secrets_mod, monkeypatch):
    for name, _, _ in secrets_mod.KEYS:
        monkeypatch.delenv(name, raising=False)
    assert secrets_mod.main() == 0


def test_no_echo_of_secret_expressions_in_the_workflow():
    """A future 'quick debug' echo here would leak into a public log."""
    text = (WORKFLOWS / "secrets-check.yml").read_text()
    run_lines = [ln for ln in text.splitlines()
                 if re.search(r"\b(echo|printf|cat)\b", ln)]
    for ln in run_lines:
        assert "secrets." not in ln, f"possible secret echo: {ln.strip()}"


# ==========================================================================
# The end-to-end claim: this monitor sees what the old one did not
# ==========================================================================

def test_monitor_sees_the_feeds_the_old_list_never_covered(dh):
    """MUFON, stock money-flow and Summit news froze for two months precisely
    because they were absent from the old hand-typed TRACKED dict."""
    for rel in ("data-mufon.json", "data-stock-money-flow.json",
                "snowflake_summit/news.json"):
        assert rel in dh.MANIFEST
        assert dh.MANIFEST[rel].kind == dh.COMMITTED


def test_remediation_refuses_to_guess_a_refresher(dh):
    """data-aviation.json has no confirmed owner script. Running the wrong
    fetcher against a feed is worse than running none — it can overwrite good
    data with a plausible-looking wrong shape."""
    r = dh.Result("data-aviation.json", dh.STALE, 999.0, 24.0)
    notes = dh.remediate([r])
    assert notes == ["data-aviation.json: no safe automatic retry; needs a human."]


def test_remediation_records_a_failed_retry_instead_of_swallowing_it(dh,
                                                                    monkeypatch):
    # A real executable, not the shell builtin `exit` this used to use:
    # remediate() runs refreshers WITHOUT a shell, so a builtin is not
    # runnable. Every refresher in MANIFEST is a plain argv line, so this
    # fixture now matches how they are actually invoked.
    monkeypatch.setitem(
        dh.MANIFEST, "data-tsa.json",
        dh.Feed(dh.COMMITTED, "test", f"{sys.executable} -c 'import sys; sys.exit(7)'"))
    notes = dh.remediate([dh.Result("data-tsa.json", dh.STALE, 999.0, 24.0)])
    assert any("rc=7" in n for n in notes)


def test_remediation_survives_a_refresher_that_cannot_start(dh, monkeypatch):
    """An unrunnable refresher must be reported, not raised.

    With no shell, a missing binary raises FileNotFoundError instead of
    returning rc=127. Uncaught, that would abort the entire health run — so
    one feed with a typo'd refresher would mean no report at all, which is
    precisely the silence this monitor exists to end.
    """
    monkeypatch.setitem(dh.MANIFEST, "data-tsa.json",
                        dh.Feed(dh.COMMITTED, "test", "definitely-not-a-real-binary-xyz"))
    notes = dh.remediate([dh.Result("data-tsa.json", dh.STALE, 999.0, 24.0)])
    assert any("could not start" in n for n in notes), notes


def test_remediation_ignores_healthy_and_suppressed_feeds(dh):
    assert dh.remediate([dh.Result("data-tsa.json", dh.OK),
                         dh.Result("data-tsa.json", dh.SUPPRESSED)]) == []


def test_health_status_rows_carry_their_date_provenance(bhs, tmp_path):
    """`date_key: null` plus `date_drift` on a /health/ row is the visible
    symptom of the next allowlist drift, rather than a silently mtime-derived
    age that looks fine."""
    _write(tmp_path, "good.json", {"generated_at": "2026-08-03T00:00:00Z"})
    _write(tmp_path, "drifted.json", {"harvested_on": "2026-01-01T00:00:00Z"})
    rows = {r["name"]: r for r in bhs.scan(tmp_path, tmp_path)}
    assert rows["good.json"]["date_key"] == "generated_at"
    assert "date_drift" not in rows["good.json"]
    assert rows["drifted.json"]["date_key"] is None
    assert rows["drifted.json"]["date_drift"] == ["harvested_on"]


def test_real_repo_run_is_honest_about_the_current_tree(dh):
    """Not asserting a specific count — that changes as feeds are fixed. The
    invariant is that the monitor produces a verdict for every declared feed
    and never silently drops one."""
    results = dh.evaluate(dh.COMMITTED)
    reported = {r.path for r in results}
    for rel, feed in dh.MANIFEST.items():
        if feed.kind == dh.BUILT:
            continue
        assert rel in reported, f"{rel} declared but produced no verdict"
