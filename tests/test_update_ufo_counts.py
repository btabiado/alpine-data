"""The manual UAP update: one saved file in, counts out, raw file deleted.

The sample page is fake. Nothing here contacts nuforc.org.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "ufo_counts_sample.html"
SCRIPT = ROOT / "scripts" / "update_ufo_counts.py"
ATTRIBUTION = (
    "Data used on this dashboard was provided by the National UFO Reporting "
    "Center (nuforc.org) and is used with NUFORC's special permission"
)
LEAKS = (
    "UNIQUE_REPORT_TEXT_SHOULD_NOT_LEAK",
    "ANOTHER_SECRET_NARRATIVE",
    "silver saucer",
    "999001",
    "old note that is not a count",
)


def _load():
    spec = importlib.util.spec_from_file_location("update_ufo_counts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_one_command_updates_counts_and_deletes_the_saved_file(tmp_path):
    saved = tmp_path / "saved-page.html"
    saved.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "data-mufon.json"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(saved), "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert not saved.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    for leak in LEAKS:
        assert leak not in blob
    assert payload["total_records"] == 3
    assert payload["date_range"] == ["1906-11-11", "2026-06-01"]
    assert payload["by_state_year"]["RI"]["2026"] == 1
    assert payload["by_state_year"]["TX"]["2024"] == 1
    assert "England" not in payload["by_state_year"]
    assert payload["totals_by_month"]["2026-06"] == 1
    assert payload["totals_by_month"]["1906-11"] == 1
    shapes = {row["shape"]: row["count"] for row in payload["shape_totals"]}
    assert shapes["disk"] == 1 and shapes["light"] == 1 and shapes["unknown"] == 1
    assert payload["top_cities_by_state"]["RI"] == [{"city": "Portsmouth", "count": 1}]
    assert payload["live_refresh"]["manual"] is True
    assert payload["source_url"] == "https://nuforc.org/subndx/?id=all"
    assert "summary" not in payload


def test_csv_paste_is_accepted_and_drops_the_narrative(tmp_path):
    mod = _load()
    saved = tmp_path / "copied-table.csv"
    saved.write_text(
        "Occurred,City,State,Country,Shape,Summary\n"
        "01/15/2020 22:00,Denver,CO,USA,Triangle,SECRET_CSV_STORY about a triangle\n",
        encoding="utf-8",
    )
    out = tmp_path / "counts.json"
    assert mod.main([str(saved), "--out", str(out)]) == 0
    assert not saved.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "SECRET_CSV_STORY" not in out.read_text(encoding="utf-8")
    assert payload["total_records"] == 1
    assert payload["by_state_year"]["CO"]["2020"] == 1
    assert payload["shape_totals"][0]["shape"] == "triangle"


def test_partial_file_does_not_replace_history_or_get_deleted(tmp_path):
    mod = _load()
    saved = tmp_path / "partial.html"
    saved.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    out = tmp_path / "data-mufon.json"
    out.write_text(json.dumps({"total_records": 10000, "by_state_year": {"CA": {"2020": 1}}}),
                   encoding="utf-8")
    before = out.read_text(encoding="utf-8")
    code = mod.main([str(saved), "--out", str(out)])
    assert code == 1
    assert saved.exists()
    assert out.read_text(encoding="utf-8") == before


def test_empty_saved_page_is_kept_and_reported(tmp_path, capsys):
    mod = _load()
    saved = tmp_path / "shell.html"
    saved.write_text("<html><body><table><tr><th>Occurred</th></tr></table></body></html>",
                     encoding="utf-8")
    out = tmp_path / "counts.json"
    assert mod.main([str(saved), "--out", str(out)]) == 1
    assert saved.exists()
    assert not out.exists()
    assert "No report rows" in capsys.readouterr().err


def test_dropped_file_in_incoming_is_the_default_input(tmp_path, monkeypatch):
    mod = _load()
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    saved = incoming / "page.txt"
    saved.write_text(
        "Occurred\tCity\tState\tShape\tSummary\n"
        "03/03/2019 01:00\tBoise\tID\tOrb\tshould not be stored\n",
        encoding="utf-8",
    )
    out = tmp_path / "counts.json"
    monkeypatch.setattr(mod, "INCOMING_DIR", incoming)
    assert mod.main(["--out", str(out)]) == 0
    assert not saved.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["by_state_year"]["ID"]["2019"] == 1
    assert "should not be stored" not in out.read_text(encoding="utf-8")


def test_raw_paths_are_gitignored_and_month_caches_are_gone():
    def ignored(rel: str) -> bool:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", rel], cwd=ROOT, check=False)
        return proc.returncode == 0

    assert ignored("incoming/nuforc/saved-page.html")
    assert ignored("incoming/nuforc/copied-table.csv")
    assert ignored("data/.stale/nuforc_subndx_202606.json")
    assert ignored("nuforc_subndx_201407.json")
    assert not ignored("incoming/nuforc/README.md")
    assert not ignored("scripts/update_ufo_counts.py")
    assert not ignored("data-mufon.json")
    assert not ignored("tests/fixtures/ufo_counts_sample.html")
    tracked = subprocess.run(
        ["git", "ls-files", "data/.stale"], cwd=ROOT,
        capture_output=True, text=True, check=True).stdout
    assert "nuforc" not in tracked
    assert list((ROOT / "data" / ".stale").glob("nuforc_subndx_*.json")) == []


def test_no_workflow_or_update_script_requests_nuforc():
    src = SCRIPT.read_text(encoding="utf-8")
    assert "urlopen" not in src
    assert "requests" not in src
    assert "https://nuforc.org/subndx/?id=all" in src
    for path in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = path.read_text(encoding="utf-8")
        assert "nuforc.org" not in text, path.name
        assert "update_ufo_counts" not in text, path.name


def test_ufo_tab_shows_the_attribution():
    html = (ROOT / "app.py").read_text(encoding="utf-8")
    tab = html.split('id="tab-mufon"', 1)[1].split('id="tab-city"', 1)[0]
    assert ATTRIBUTION in tab


@pytest.mark.parametrize("rel", ["README.md", "incoming/nuforc/README.md"])
def test_readme_tells_the_owner_the_one_command(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert "https://nuforc.org/subndx/?id=all" in text
    assert "python3 scripts/update_ufo_counts.py" in text
    assert "scrap" in text.lower()
