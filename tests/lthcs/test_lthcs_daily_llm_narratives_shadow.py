"""Tier 5 #23 — Stage 7.5b LLM narratives SHADOW integration tests.

The shadow stage runs AFTER Stage 7 (templated narratives). It produces
:attr:`state.llm_narrative_shadow_rows` from the snapshot via a mocked
Anthropic client, and Stage 8 persists them to a sibling
``data/lthcs/narratives_llm/<date>.json`` file. Production
``state.narrative_rows`` must remain byte-identical between flag-off
and flag-on runs.

These tests reuse the patched-config / patched-persist / patched-sources
fixtures from :mod:`tests.lthcs.test_daily` via standard pytest fixture
collection in ``tests/lthcs/__init__.py`` -- but we redefine the local
needed ones here to keep this file self-contained for the swarm.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

import lthcs_daily
from lthcs import narratives_llm
from lthcs.persist import LthcsPersist


# ---------------------------------------------------------------------------
# Light fixtures — reuse the same approach as test_daily.py
# ---------------------------------------------------------------------------

# Reuse the existing test_daily fixtures via pytest's plugin/conftest
# lookup. The fixtures are defined at module level in tests/lthcs/test_daily.py,
# so they are NOT auto-shared. Import the constants here and rebuild the
# minimal fixture chain we need.

from tests.lthcs.test_daily import (  # type: ignore
    _UNIVERSE_FIXTURE,
    _WEIGHTS_FIXTURE,
    _SECTOR_WEIGHTS_FIXTURE,
)


@pytest.fixture
def patched_configs(monkeypatch, tmp_path):
    universe_path = tmp_path / "universe.json"
    weights_path = tmp_path / "weights.json"
    sector_path = tmp_path / "sector_des_weights.json"
    universe_path.write_text(json.dumps(_UNIVERSE_FIXTURE))
    weights_path.write_text(json.dumps(_WEIGHTS_FIXTURE))
    sector_path.write_text(json.dumps(_SECTOR_WEIGHTS_FIXTURE))
    monkeypatch.setattr(lthcs_daily, "UNIVERSE_PATH", universe_path)
    monkeypatch.setattr(lthcs_daily, "WEIGHTS_PATH", weights_path)
    monkeypatch.setattr(lthcs_daily, "SECTOR_WEIGHTS_PATH", sector_path)
    return universe_path, weights_path, sector_path


def _good_llm_json() -> str:
    return json.dumps(
        {
            "todays_take": "A.",
            "why_changed": "B.",
            "why_not_to_sell": "C.",
            "what_would_break": "D.",
            "confidence_level": "medium",
        }
    )


class _FakeUsage:
    def __init__(self, input_tokens=247, output_tokens=198, cache_read=1100):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = cache_read
        self.cache_creation_input_tokens = 0


class _FakeBlock:
    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()


class _FakeMessages:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(_good_llm_json())


class _FakeAnthropicClient:
    def __init__(self, **_):
        self.messages = _FakeMessages()


def _build_state_through_stage_7(args_argv, tmp_path) -> "lthcs_daily.PipelineState":
    """Build a real-ish pipeline state up through Stage 7 (templated narratives).

    We bypass Stage 2's network calls by skipping them: the shadow
    stage only needs ``snapshot_rows`` + ``pillar_results`` to be
    populated, and we synthesize those directly to keep this test
    fast and hermetic.
    """
    args = lthcs_daily.parse_args(args_argv)
    state = lthcs_daily.PipelineState(args=args)
    state.calc_date = "2026-05-19"
    state.persist = LthcsPersist(data_root=tmp_path)
    # Two scored tickers.
    state.scored_tickers = ["AAPL", "LCID"]
    state.active_tickers = ["AAPL", "LCID"]
    for sym in state.scored_tickers:
        state.pillar_results[sym] = {
            pillar: {
                "sub_score": 55.0,
                "components": {"x": 1},
                "data_quality": {"has_insider": True},
            }
            for pillar in (
                "adoption_momentum",
                "institutional_confidence",
                "financial_evolution",
                "thesis_integrity",
                "des",
            )
        }
        state.snapshot_rows.append(
            {
                "ticker": sym,
                "lthcs_score": 55.0,
                "band": "weakening",
                "drift_1d": 0.0,
                "drift_30d": 0.0,
                "confidence_level": "high",
                "subscores": {
                    "adoption_momentum": 50.0,
                    "institutional_confidence": 55.0,
                    "financial_evolution": 58.0,
                    "thesis_integrity": 60.0,
                    "des": 52.0,
                },
                "sector": "Technology",
            }
        )
    # Run Stage 7 (templated) so narrative_rows is populated.
    assert lthcs_daily.stage_7_generate_narratives(state) is True
    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_stage_7p5b_noop_when_flag_off(monkeypatch, patched_configs, tmp_path):
    """Flag off (default) -> shadow stage is a clean no-op."""
    monkeypatch.delenv("LTHCS_LLM_NARRATIVES_ENABLED", raising=False)
    monkeypatch.delenv("LTHCS_NARRATIVES_LLM_ENABLED", raising=False)
    state = _build_state_through_stage_7(["--tickers", "AAPL,LCID"], tmp_path)
    templated_snapshot = list(state.narrative_rows)

    assert lthcs_daily.stage_7p5b_llm_narratives_shadow(state) is True
    assert state.llm_narrative_shadow_rows == []
    assert state.llm_narrative_shadow_meta is None
    # Production narrative_rows unchanged.
    assert state.narrative_rows == templated_snapshot


def test_stage_7p5b_writes_shadow_when_flag_on(monkeypatch, patched_configs, tmp_path):
    """Flag on + mocked Anthropic -> shadow rows populated and persisted."""
    monkeypatch.setenv("LTHCS_LLM_NARRATIVES_ENABLED", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    # Inject a fake anthropic SDK so score_universe doesn't try to import
    # the real one. score_universe will fall through to a real client
    # construction; we instead patch the lthcs.narratives_llm import
    # paths it uses.
    fake_anthropic_module = MagicMock()
    fake_anthropic_module.Anthropic = _FakeAnthropicClient
    monkeypatch.setattr(narratives_llm, "_import_anthropic", lambda: fake_anthropic_module)

    state = _build_state_through_stage_7(["--tickers", "AAPL,LCID"], tmp_path)
    templated_snapshot = list(state.narrative_rows)

    assert lthcs_daily.stage_7p5b_llm_narratives_shadow(state) is True
    assert len(state.llm_narrative_shadow_rows) == 2
    for rec in state.llm_narrative_shadow_rows:
        assert rec["fallback"] is False
        assert rec["todays_take"] == "A."
    # Stage 7 production output is byte-identical between flag-off and on.
    assert state.narrative_rows == templated_snapshot

    # Stage 8 persists the shadow file to data/lthcs/narratives_llm/.
    # We invoke Stage 8 via the persist instance directly to avoid the
    # full Stage 8 side effects (variable_detail, history, etc.).
    persist = state.persist
    assert persist is not None
    persist.write_narratives_llm(
        state.calc_date,
        str((state.llm_narrative_shadow_meta or {}).get("model") or "claude-haiku-4-5"),
        state.llm_narrative_shadow_rows,
        meta=state.llm_narrative_shadow_meta or {},
    )
    shadow_path = tmp_path / "narratives_llm" / "2026-05-19.json"
    assert shadow_path.exists()
    payload = json.loads(shadow_path.read_text())
    assert payload["calc_date"] == "2026-05-19"
    assert len(payload["narratives"]) == 2
    # Templated file dir is separate and was not written by this stage.
    templated_path = tmp_path / "narratives" / "2026-05-19.json"
    assert not templated_path.exists()


def test_stage_7p5b_skipped_on_as_of_backfill(monkeypatch, patched_configs, tmp_path):
    """--as-of (backfill) mode skips the shadow stage even when flag is on."""
    monkeypatch.setenv("LTHCS_LLM_NARRATIVES_ENABLED", "1")
    state = _build_state_through_stage_7(
        ["--tickers", "AAPL,LCID", "--as-of", "2026-04-01"], tmp_path
    )
    assert lthcs_daily.stage_7p5b_llm_narratives_shadow(state) is True
    assert state.llm_narrative_shadow_rows == []


def test_stage_7p5b_handles_missing_sdk(monkeypatch, patched_configs, tmp_path):
    """Flag on but no SDK + no key -> all rows fall back to template; no crash."""
    monkeypatch.setenv("LTHCS_LLM_NARRATIVES_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(narratives_llm, "_import_anthropic", lambda: None)

    state = _build_state_through_stage_7(["--tickers", "AAPL,LCID"], tmp_path)
    assert lthcs_daily.stage_7p5b_llm_narratives_shadow(state) is True
    # score_universe returns results with fallback=True for each ticker.
    assert len(state.llm_narrative_shadow_rows) == 2
    for rec in state.llm_narrative_shadow_rows:
        assert rec["fallback"] is True
        assert rec["fallback_reason"] == "missing_api_key"


def test_stage_7p5b_registered_in_stage_list():
    assert lthcs_daily.stage_7p5b_llm_narratives_shadow in lthcs_daily.STAGES
    # Order: after Stage 7 (templated) and Stage 7.5 (index), before Stage 8.
    stages = lthcs_daily.STAGES
    i_7 = stages.index(lthcs_daily.stage_7_generate_narratives)
    i_7p5b = stages.index(lthcs_daily.stage_7p5b_llm_narratives_shadow)
    i_8 = stages.index(lthcs_daily.stage_8_persist)
    assert i_7 < i_7p5b < i_8
