"""V2 (/v2/ and /lthcs/v2/) is retired; V1 no longer depends on its build.

The V2 build was the only thing that wrote data-cpi.json, data-supplies.json,
data-metals.json and data-stock-prices.json, and it rebuilt data-mufon.json by
trying to scrape nuforc.org every hour. These tests pin the replacement: V1's
own deploy produces the sidecars, MUFON serves the committed frozen cache, and
nothing V2-shaped (build, secrets, NUFORC commit-back, audit page) comes back.
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
PAGES_YML = ROOT / ".github" / "workflows" / "pages.yml"
SIDECARS = ("cpi", "supplies", "metals", "stock-prices")


@pytest.fixture(scope="module")
def steps():
    return yaml.safe_load(PAGES_YML.read_text(encoding="utf-8"))["jobs"]["build"]["steps"]


def _idx(steps, pred):
    return next(i for i, s in enumerate(steps) if pred(s))


def test_v2_sources_are_gone():
    for d in ("v2", "lthcs_tab_v2"):
        tracked = subprocess.run(["git", "ls-files", d], cwd=ROOT, capture_output=True,
                                 text=True, check=True).stdout.strip()
        assert not tracked, f"{d}/ is still tracked: {tracked.splitlines()[:3]}"


def test_pages_runs_no_v2_build_and_no_nuforc_scrape():
    text = PAGES_YML.read_text(encoding="utf-8")
    for gone in ("v2/app.py", "validate_v2_dashboard", "fetch_mufon", "nuforc_subndx",
                 "v2/dashboard.html", "v2/data-"):
        assert gone not in text, gone


def test_only_secrets_a_step_reads_are_passed():
    """The V2 step was handed eight secrets its code never read. Three of them
    (EIA, Alpha Vantage, Finnhub) are read only under lthcs/, which no pages
    step imports, so they must not appear in pages.yml at all."""
    text = PAGES_YML.read_text(encoding="utf-8")
    for key in ("EIA_API_KEY", "ALPHA_VANTAGE_API_KEY", "FINNHUB_API_KEY"):
        assert key not in text, key


def test_v1_deploy_fetches_the_sidecars_v2_used_to_write(steps):
    i = _idx(steps, lambda s: "fetch_cpi.py" in (s.get("run") or ""))
    step = steps[i]
    run = step["run"]
    for name in SIDECARS:
        script = "fetch_stock_prices.py" if name == "stock-prices" else f"fetch_{name}.py"
        assert re.search(rf"{script} --out data-{name}\.json\b", run), name
    assert "--market-json data/market.json" in run
    assert set(step["env"]) == {"FRED_API_KEY"}
    assert step.get("continue-on-error") is True and step.get("timeout-minutes")
    assert "timeout 150 python" in run and "::warning" in run
    v1 = _idx(steps, lambda s: "app.py --fetch-market" in (s.get("run") or ""))
    assert v1 < i
    for later in ("Build dashboard health status JSON", "Stage site directory",
                  "Upload daily snapshot to ADW R2 archive"):
        assert i < _idx(steps, lambda s, n=later: s.get("name") == n), later
    assert i < _idx(steps, lambda s: "data_health.py --mode built" in (s.get("run") or ""))


def test_old_v2_addresses_redirect(steps):
    stage = steps[_idx(steps, lambda s: s.get("name") == "Stage site directory")]["run"]
    assert "for d in _site/v2 _site/lthcs/v2; do" in stage
    assert 'cp site_static/v2-redirect.html "$d/index.html"' in stage
    page = (ROOT / "site_static" / "v2-redirect.html").read_text(encoding="utf-8")
    # "../" is / from /v2/ and /lthcs/ from /lthcs/v2/.
    assert 'content="0; url=../"' in page and "location.replace('../'" in page
    assert 'href="../"' in page


def test_lthcs_v2_is_not_a_staged_subpage():
    spec = importlib.util.spec_from_file_location("_stage", ROOT / "scripts" / "stage_lthcs_site.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "lthcs_tab_v2" not in mod.SUBPAGES and "v2" not in mod.SUBPAGES.values()


@pytest.mark.parametrize("mod,out", [
    ("fetch_cpi", "data-cpi.json"), ("fetch_supplies", "data-supplies.json"),
    ("fetch_metals", "data-metals.json"), ("fetch_stock_prices", "data-stock-prices.json"),
    ("fetch_mufon", "data-mufon.json"), ("fetch_advisories", "data-travel.json"),
])
def test_fetchers_default_to_the_repo_root(mod, out):
    m = importlib.import_module(mod)
    assert m.DEFAULT_OUT == ROOT / out
    assert getattr(m, "DEFAULT_OUT_V1", "") == ""     # the V1 mirror is off by default


def test_daily_audit_no_longer_visits_v2():
    ux = (ROOT / "scripts" / "daily_audit_ux.mjs").read_text(encoding="utf-8")
    assert "'/v2/'" not in ux
    report = (ROOT / "scripts" / "daily_audit_report.py").read_text(encoding="utf-8")
    assert 'TABBED_PAGES = ("v1",)' in report


def test_monitor_judges_mufon_from_the_committed_file():
    spec = importlib.util.spec_from_file_location("_dh_v2", ROOT / "scripts" / "data_health.py")
    dh = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = dh
    spec.loader.exec_module(dh)
    feed = dh.MANIFEST["data-mufon.json"]
    assert feed.source == dh.REPO and feed.built_path is None and feed.refresher is None
    assert dh.MANIFEST["data-stock-prices.json"].kind == dh.BUILT
    assert not [p for p in dh.verify_manifest() if "mufon" in p or "stock-prices" in p]
