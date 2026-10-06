"""Fixtures shared by the LTHCS tests."""
from __future__ import annotations

import pytest

import lthcs.persist
import lthcs.sources.thesis_rotation


@pytest.fixture(autouse=True)
def _isolate_lthcs_data_root(tmp_path, monkeypatch):
    """Default LTHCS data root -> tmp_path, for every LTHCS test.

    ``LthcsPersist()`` and ``ThesisRotation()`` built without an explicit
    ``data_root`` resolve to the committed ``data/lthcs``. The daily-pipeline
    tests build them that way (stage 2 creates the rotation manager from
    ``state.persist``, which is often unset), and each run overwrote
    ``data/lthcs/sentiment/{AAPL,LCID}.json`` and ``thesis_rotation.json``
    with test values.

    Tests that pass a ``data_root`` are unaffected. The two tests that check
    the default really points into the repo import the function by name, so
    they still see the real one.
    """
    root = tmp_path / "lthcs_data_root"
    monkeypatch.setattr(lthcs.persist, "get_default_data_root", lambda: root)
    monkeypatch.setattr(
        lthcs.sources.thesis_rotation, "get_default_data_root", lambda: root)
    return root
