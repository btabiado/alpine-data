"""Tests for ``scripts.lthcs_export_csv``.

The script is pure-I/O: fixture a synthetic ``data/lthcs/`` tree under
``tmp_path``, run the export, and assert on the resulting CSV. We
never touch the committed CSV (``data/lthcs/public/universe.csv``).
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from scripts import lthcs_export_csv as mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _score_row(
    ticker: str,
    *,
    sector: str = "Technology",
    band: str = "constructive",
    confidence: str = "high",
    score: float = 60.0,
    maturity: str = "mature_compounder",
    sub: dict | None = None,
    flags: list | None = None,
    dropped: list | None = None,
    effective_weights: list | None = None,
    drifts: tuple = (0.0, 0.0, 0.0, 0.0),
) -> dict:
    return {
        "ticker": ticker,
        "lthcs_score": score,
        "band": band,
        "drift_1d": drifts[0],
        "drift_7d": drifts[1],
        "drift_30d": drifts[2],
        "drift_90d": drifts[3],
        "confidence_level": confidence,
        "data_quality_flags": flags or [],
        "subscores": sub
        or {
            "adoption_momentum": 50.0,
            "institutional_confidence": 50.0,
            "financial_evolution": 50.0,
            "thesis_integrity": 50.0,
            "des": 50.0,
        },
        "modifiers": {"macro_adj": 0.0, "sector_adj": 0.0, "volatility_mod": 0.0},
        "maturity_stage": maturity,
        "weights_used": [0.2, 0.2, 0.2, 0.2, 0.2],
        "effective_weights": effective_weights
        if effective_weights is not None
        else [0.2, 0.2, 0.2, 0.2, 0.2],
        "dropped_pillars": dropped or [],
        "weighted_components": [10.0, 10.0, 10.0, 10.0, 10.0],
        "sector": sector,
    }


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    """Synthetic data/lthcs/ with three dated snapshots."""
    root = tmp_path / "lthcs"
    # 2026-05-15 (older) — single ticker.
    _write_json(
        root / "snapshots" / "2026-05-15.json",
        {
            "calc_date": "2026-05-15",
            "model_version": "v1.1.0",
            "scores": [_score_row("AAPL", score=55.0)],
        },
    )
    # 2026-05-16 (older) — single ticker.
    _write_json(
        root / "snapshots" / "2026-05-16.json",
        {
            "calc_date": "2026-05-16",
            "model_version": "v1.1.0",
            "scores": [_score_row("AAPL", score=56.0)],
        },
    )
    # 2026-05-17 (latest) — two tickers, with flags + dropped pillars on one.
    _write_json(
        root / "snapshots" / "2026-05-17.json",
        {
            "calc_date": "2026-05-17",
            "model_version": "v1.1.0",
            "scores": [
                _score_row(
                    "AAPL",
                    score=54.2,
                    sub={
                        "adoption_momentum": 28.1,
                        "institutional_confidence": 68.7,
                        "financial_evolution": 67.5,
                        "thesis_integrity": 73.1,
                        "des": 43.5,
                    },
                    drifts=(0.1, -0.5, 1.2, 3.4),
                ),
                _score_row(
                    "BKNG",
                    sector="Consumer Discretionary",
                    band="monitor",
                    confidence="medium",
                    score=42.0,
                    flags=["thesis_unavailable", "low_breadth"],
                    dropped=["thesis_integrity"],
                    # Effective weights renormalized away thesis (was 0.2).
                    # Each remaining pillar absorbs 0.05 → 0.25.
                    effective_weights=[0.25, 0.25, 0.25, 0.0, 0.25],
                ),
            ],
        },
    )
    # Decoy file under snapshots/ — must not be picked as latest.
    _write_json(root / "snapshots" / "index.json", {"dates": []})
    return root


def _parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    return rows[0], rows[1:]


# ---------------------------------------------------------------------------
# Column contract
# ---------------------------------------------------------------------------


def test_csv_header_matches_base_columns(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    header, _ = _parse_csv(out.read_text())
    assert header == list(mod.BASE_COLUMNS)


def test_csv_header_with_effective_weights_appends_5_columns(data_root: Path):
    out, _ = mod.build_and_write(data_root, include_effective_weights=True)
    header, _ = _parse_csv(out.read_text())
    assert header[: len(mod.BASE_COLUMNS)] == list(mod.BASE_COLUMNS)
    assert header[len(mod.BASE_COLUMNS) :] == list(mod.EFFECTIVE_WEIGHT_COLUMNS)
    assert len(header) == len(mod.BASE_COLUMNS) + 5


def test_csv_column_order_is_the_contract():
    # ALPHA's button + downstream consumers depend on this exact order.
    # Lock it down so a refactor accidentally swapping two columns
    # immediately fails the test.
    assert mod.BASE_COLUMNS == (
        "ticker",
        "calc_date",
        "lthcs_score",
        "band",
        "confidence_level",
        "adoption_momentum",
        "institutional_confidence",
        "financial_evolution",
        "thesis_integrity",
        "des",
        "dropped_pillars",
        "data_quality_flags",
        "drift_1d",
        "drift_7d",
        "drift_30d",
        "drift_90d",
        "sector",
        "maturity_stage",
    )


# ---------------------------------------------------------------------------
# Latest-snapshot resolution
# ---------------------------------------------------------------------------


def test_latest_snapshot_is_picked_by_default(data_root: Path):
    out, calc_date = mod.build_and_write(data_root)
    assert calc_date == "2026-05-17"
    header, rows = _parse_csv(out.read_text())
    # Two tickers in 2026-05-17 fixture.
    assert len(rows) == 2
    # calc_date column reflects the chosen date.
    date_col = header.index("calc_date")
    assert all(r[date_col] == "2026-05-17" for r in rows)


def test_asof_picks_historical_snapshot(data_root: Path):
    out, calc_date = mod.build_and_write(data_root, asof="2026-05-15")
    assert calc_date == "2026-05-15"
    header, rows = _parse_csv(out.read_text())
    assert len(rows) == 1
    # 2026-05-15 fixture has a single AAPL row.
    ticker_col = header.index("ticker")
    date_col = header.index("calc_date")
    score_col = header.index("lthcs_score")
    assert rows[0][ticker_col] == "AAPL"
    assert rows[0][date_col] == "2026-05-15"
    assert rows[0][score_col] == "55.0"


def test_asof_missing_snapshot_raises(data_root: Path):
    with pytest.raises(FileNotFoundError):
        mod.build_and_write(data_root, asof="2020-01-01")


# ---------------------------------------------------------------------------
# Row content
# ---------------------------------------------------------------------------


def test_row_flattens_subscores_to_top_level(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    header, rows = _parse_csv(out.read_text())
    by_ticker = {r[header.index("ticker")]: r for r in rows}
    aapl = by_ticker["AAPL"]
    assert aapl[header.index("adoption_momentum")] == "28.1"
    assert aapl[header.index("institutional_confidence")] == "68.7"
    assert aapl[header.index("financial_evolution")] == "67.5"
    assert aapl[header.index("thesis_integrity")] == "73.1"
    assert aapl[header.index("des")] == "43.5"


def test_row_carries_drifts_and_metadata(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    header, rows = _parse_csv(out.read_text())
    by_ticker = {r[header.index("ticker")]: r for r in rows}
    aapl = by_ticker["AAPL"]
    assert aapl[header.index("drift_1d")] == "0.1"
    assert aapl[header.index("drift_7d")] == "-0.5"
    assert aapl[header.index("drift_30d")] == "1.2"
    assert aapl[header.index("drift_90d")] == "3.4"
    assert aapl[header.index("sector")] == "Technology"
    assert aapl[header.index("maturity_stage")] == "mature_compounder"
    assert aapl[header.index("band")] == "constructive"
    assert aapl[header.index("confidence_level")] == "high"


def test_rows_are_sorted_by_ticker(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    header, rows = _parse_csv(out.read_text())
    tickers = [r[header.index("ticker")] for r in rows]
    assert tickers == sorted(tickers)
    # Specifically AAPL before BKNG.
    assert tickers == ["AAPL", "BKNG"]


# ---------------------------------------------------------------------------
# Multi-value list fields → semicolon-joined, CSV-safe
# ---------------------------------------------------------------------------


def test_flags_column_joins_multi_flag_lists_with_semicolons(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    header, rows = _parse_csv(out.read_text())
    by_ticker = {r[header.index("ticker")]: r for r in rows}
    bkng = by_ticker["BKNG"]
    # Two flags joined with semicolons (NOT commas — that would break CSV).
    assert bkng[header.index("data_quality_flags")] == "thesis_unavailable;low_breadth"
    # Dropped pillars likewise joined.
    assert bkng[header.index("dropped_pillars")] == "thesis_integrity"


def test_flags_column_is_empty_string_when_no_flags(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    header, rows = _parse_csv(out.read_text())
    by_ticker = {r[header.index("ticker")]: r for r in rows}
    aapl = by_ticker["AAPL"]
    assert aapl[header.index("data_quality_flags")] == ""
    assert aapl[header.index("dropped_pillars")] == ""


def test_flags_with_embedded_comma_would_be_csv_quoted(tmp_path: Path):
    # Defensive: even if a flag string ever contains a comma, csv.writer
    # quotes the cell. Verify the round-trip survives.
    snapshot = {
        "calc_date": "2026-05-17",
        "scores": [
            _score_row(
                "TEST",
                flags=["flag,with,comma", "normal_flag"],
            )
        ],
    }
    text = mod.build_csv_text(snapshot)
    header, rows = _parse_csv(text)
    assert rows[0][header.index("data_quality_flags")] == "flag,with,comma;normal_flag"


# ---------------------------------------------------------------------------
# Empty snapshot → header-only CSV
# ---------------------------------------------------------------------------


def test_empty_snapshot_produces_header_only_csv(tmp_path: Path):
    root = tmp_path / "lthcs"
    _write_json(
        root / "snapshots" / "2026-05-17.json",
        {"calc_date": "2026-05-17", "scores": []},
    )
    out, calc_date = mod.build_and_write(root)
    text = out.read_text()
    header, rows = _parse_csv(text)
    assert calc_date == "2026-05-17"
    assert header == list(mod.BASE_COLUMNS)
    assert rows == []


def test_no_snapshots_at_all_raises(tmp_path: Path):
    root = tmp_path / "lthcs"
    root.mkdir()
    with pytest.raises(FileNotFoundError):
        mod.build_and_write(root)


# ---------------------------------------------------------------------------
# Renormalized weights
# ---------------------------------------------------------------------------


def test_renormalized_effective_weights_export_correctly(data_root: Path):
    out, _ = mod.build_and_write(data_root, include_effective_weights=True)
    header, rows = _parse_csv(out.read_text())
    by_ticker = {r[header.index("ticker")]: r for r in rows}
    # BKNG had thesis dropped; effective weights renormalized to
    # [0.25, 0.25, 0.25, 0.0, 0.25].
    bkng = by_ticker["BKNG"]
    assert bkng[header.index("effective_weight_adoption_momentum")] == "0.25"
    assert bkng[header.index("effective_weight_institutional_confidence")] == "0.25"
    assert bkng[header.index("effective_weight_financial_evolution")] == "0.25"
    assert bkng[header.index("effective_weight_thesis_integrity")] == "0.0"
    assert bkng[header.index("effective_weight_des")] == "0.25"
    # AAPL has all pillars present — equal weights.
    aapl = by_ticker["AAPL"]
    for col in mod.EFFECTIVE_WEIGHT_COLUMNS:
        assert aapl[header.index(col)] == "0.2"


def test_effective_weights_absent_by_default(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    header, _ = _parse_csv(out.read_text())
    for col in mod.EFFECTIVE_WEIGHT_COLUMNS:
        assert col not in header


# ---------------------------------------------------------------------------
# Output path + CLI
# ---------------------------------------------------------------------------


def test_default_output_path_is_public_universe_csv(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    assert out == data_root / "public" / "universe.csv"
    assert out.is_file()


def test_out_override_writes_to_custom_path(data_root: Path, tmp_path: Path):
    custom = tmp_path / "custom" / "my.csv"
    out, _ = mod.build_and_write(data_root, out_path=custom)
    assert out == custom
    assert custom.is_file()


def test_cli_smoke(data_root: Path, capsys):
    rc = mod.main(
        [
            "--data-root",
            str(data_root),
            "--include-effective-weights",
        ]
    )
    assert rc == 0
    assert (data_root / "public" / "universe.csv").is_file()
    out = capsys.readouterr().out
    assert "wrote" in out
    assert "calc_date=2026-05-17" in out


def test_cli_asof_missing_returns_nonzero(data_root: Path, capsys):
    rc = mod.main(
        [
            "--data-root",
            str(data_root),
            "--asof",
            "2020-01-01",
        ]
    )
    assert rc == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_build_is_idempotent(data_root: Path):
    out1, _ = mod.build_and_write(data_root)
    first = out1.read_bytes()
    out2, _ = mod.build_and_write(data_root)
    assert out2.read_bytes() == first


def test_line_terminator_is_lf_only(data_root: Path):
    out, _ = mod.build_and_write(data_root)
    raw = out.read_bytes()
    # No CRLF — cross-platform stable.
    assert b"\r\n" not in raw
    assert b"\n" in raw
