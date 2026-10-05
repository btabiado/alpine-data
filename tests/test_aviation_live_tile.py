"""Aviation summary tile "Airborne now" must read the live OpenSky snapshot.

It showed ``D.live.seed.airborne`` (4,990 aircraft, captured once on
2026-06-01 and baked into data-aviation.json) labelled "live OpenSky sample",
while the hourly cron-committed data-opensky.json held a current count. Only
the Live sub-view ever fetched that file.

Pinned here: one shared loader fetches data-opensky.json for both the tile
and the Live sub-view; the tile shows the snapshot's own timestamp; the seed
is used only as a fallback and is then labelled as a dated, stale sample.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def av() -> str:
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    i = src.index("function openSky(){")
    j = src.index("const V={pilots,sport,fleet,models,macro,airtravel,safety,tsa,live,map,used,calc,sources};", i)
    return src[i:j]


def test_seed_is_never_labelled_live():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'val:fmt(D.live.seed.airborne),s:"live OpenSky sample"' not in src
    assert "live OpenSky sample" not in src


def test_one_shared_fetch_of_the_cron_snapshot(av):
    assert av.count('fetch("data-opensky.json"') == 1
    loader = av[:av.index("function avSeedAsOf")]
    assert "Promise.race" in loader, "a hung fetch would leave the tile loading forever"


def test_summary_tile_is_filled_from_the_snapshot_with_its_timestamp(av):
    summary = av[av.index("function summary(){"):]
    assert '{v:"live",t:"Airborne now",val:"…",s:"loading OpenSky snapshot"}' in summary
    assert "openSky().then(s=>{" in summary
    assert 'fmt(Number(s.airborne))' in summary
    assert '"OpenSky · "+(s.tstr' in summary


def test_seed_fallback_carries_an_explicit_stale_as_of_label(av):
    summary = av[av.index("function summary(){"):]
    assert '"stale seed sample · as of "+avSeedAsOf()' in summary
    live = av[av.index("function live(){"):av.index("function summary(){")]
    assert "openSky().then(s=>s?apply(s,false):apply(D.live.seed,true));" in live
    assert '"stale seed sample · as of "+avSeedAsOf()' in live


def test_seed_as_of_comes_from_the_seed_timestamp(av):
    assert 'String((D.live&&D.live.seed&&D.live.seed.tstr)||"").slice(0,10)' in av
