"""City labels must describe the data, not the build's cutoff.

Audit 2026-10-04: NYC NYPD `complete_through` read "2026-08" while the source
ends 2026-06-30; Miami's frozen 2023 311 snapshot read "2026-09"; extended
feed notes carried recon-time claims ("max inspection_date 2026-05-29",
"Latest complete month 2026-04") that contradicted the live series.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import fetch_city
from city import pulse

REPO_ROOT = Path(__file__).resolve().parent.parent


def _months(start_y, start_m, n, v=100):
    out, y, m = [], start_y, start_m
    for _ in range(n):
        out.append({"month": f"{y:04d}-{m:02d}", "n": v})
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def test_complete_through_is_last_data_month_not_cutoff():
    # NYPD-like: data through June, build cutoff August (lagging feed).
    f = pulse.score_feed(_months(2024, 7, 24), polarity=-1, as_of="2026-09",
                         label="NYPD Complaints", dataset="x",
                         complete_through="2026-08")
    assert f["complete_through"] == "2026-06"
    assert f["recent_period"] == "2026-06"


def test_frozen_snapshot_reports_its_own_last_month():
    f = pulse.score_feed(_months(2023, 1, 12), polarity=-1, as_of="2026-09",
                         label="Miami-Dade 311", dataset="x")
    assert f["complete_through"] == "2023-12"


def test_cutoff_still_caps_complete_through():
    # Data runs into the incomplete current month; the cutoff wins.
    f = pulse.score_feed(_months(2025, 1, 21), polarity=1, as_of="2026-08",
                         label="Permits", dataset="x")
    assert f["complete_through"] == "2026-08"


def test_no_data_means_no_complete_month():
    f = pulse.score_feed([], polarity=1, as_of="2026-09", label="x", dataset="x")
    assert f["complete_through"] is None


def test_display_note_strips_recon_date_claims():
    note = ("CONFIRMED live (max inspection_date 2026-05-29; ~1.4k/mo). Polarity 0: "
            "volume is workload. Latest complete month 2026-04.")
    assert fetch_city._display_note(note) == "Polarity 0: volume is workload."
    assert fetch_city._display_note(None) is None
    assert fetch_city._display_note("excludes last ~7 days") == "excludes last ~7 days"


_RECON = re.compile(r"(max \w+ \d{4}-\d{2}-\d{2}|Latest complete month \d{4}-\d{2}"
                    r"|data_as_of \d{4}-\d{2}-\d{2})", re.I)


def test_nypd_note_documents_the_quarterly_cadence_and_still_lags():
    """The registry note is the city-card label. It has to keep the words
    the lagging-feed cutoff keys on, and it must not pin a calendar date
    the next quarter would contradict. The committed payload carries the
    same sentence."""
    reg = json.loads((REPO_ROOT / "docs/city/city_registry.resolved.json").read_text())
    nyc = next(c for c in reg["cities"] if c["id"] == "nyc")
    feed = next(f for f in nyc["feeds"] if f["dataset"] == "5uac-w243")
    note = feed["note"]
    assert "complete quarters" in note
    assert "5uac-w243" in note
    assert "source publish lag" in note.lower()
    assert fetch_city._is_lagging(feed)
    assert not re.search(r"20\d\d-\d\d-\d\d", note)

    payload = json.loads((REPO_ROOT / "data-city.json").read_text())
    city = next(c for c in payload["cities"] if c["id"] == "nyc")
    live = next(f for p in city["pulse"]["pillars"] for f in p["feeds"]
                if f.get("dataset") == "5uac-w243")
    assert live["note"] == note
    assert f"NYPD Complaints: {note}" in city["disclosures"]


def test_extended_registry_notes_carry_no_recon_dates():
    d = json.loads((REPO_ROOT / "docs/city/city_registry.extended.json").read_text())
    bad = [(city, f.get("label"), _RECON.search(f.get("note") or "").group(0))
           for city, feeds in d["extended_feeds"].items() for f in feeds
           if _RECON.search(f.get("note") or "")]
    assert bad == []
