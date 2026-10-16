"""Schema for data/lthcs/weights.json."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PILLAR_ORDER = [
    "adoption_momentum",
    "institutional_confidence",
    "financial_evolution",
    "thesis_integrity",
    "des",
]

WEIGHT_SUM_TOLERANCE = 1e-6


class ScoreBand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: int = Field(ge=0, le=100)
    max: int = Field(ge=0, le=100)
    color: str
    label: str

    @model_validator(mode="after")
    def _min_le_max(self) -> "ScoreBand":
        if self.min > self.max:
            raise ValueError(f"band min ({self.min}) > max ({self.max})")
        return self


class ScoreBandsCalibration(BaseModel):
    """Provenance for data-driven score_bands (scripts/lthcs_calibrate_bands.py)."""

    model_config = ConfigDict(extra="allow")

    calibrated_at: date
    calibrated_from: List[str]
    ticker_count: int = Field(ge=0)
    method: str
    targets_pct: Dict[str, float]
    # Cutoffs this calibration replaced. Must match the last closed
    # score_bands_history regime (checked on Weights).
    previous_cutoffs: Optional[Dict[str, List[int]]] = None


def _bands_tile_0_to_100(bands: Dict[str, ScoreBand], where: str) -> None:
    covered: list[tuple[int, int]] = sorted((b.min, b.max) for b in bands.values())
    if not covered:
        raise ValueError(f"{where} is empty")
    if covered[0][0] != 0:
        raise ValueError(f"{where} do not start at 0 (start={covered[0][0]})")
    if covered[-1][1] != 100:
        raise ValueError(f"{where} do not end at 100 (end={covered[-1][1]})")
    for (a_min, a_max), (b_min, b_max) in zip(covered, covered[1:]):
        if b_min != a_max + 1:
            raise ValueError(
                f"{where} gap or overlap between [{a_min},{a_max}] "
                f"and [{b_min},{b_max}]"
            )


class ScoreBandRegime(BaseModel):
    """Cutoffs in effect on ``[effective_from, effective_until)``.

    ``effective_from`` is null on the earliest regime. ``effective_until``
    is exclusive: that date is scored under the next regime, or under the
    live ``score_bands`` once history ends. Threshold changes are not
    restated into older snapshots (docs/lthcs-tuning-kit.md).
    """

    model_config = ConfigDict(extra="forbid")

    effective_from: Optional[date] = None
    effective_until: date
    reason: str = ""
    score_bands: Dict[str, ScoreBand]

    @model_validator(mode="after")
    def _bounds(self) -> "ScoreBandRegime":
        if self.effective_from is not None and self.effective_until <= self.effective_from:
            raise ValueError(
                f"score band regime effective_until ({self.effective_until}) "
                f"must be after effective_from ({self.effective_from})"
            )
        _bands_tile_0_to_100(self.score_bands, "score_bands_history regime")
        return self


class ModifierRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trigger: str
    magnitude: float
    applies_to: str


class ModifierConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    macro_adjustment: ModifierRule
    volatility_modifier: ModifierRule
    sector_adjustment_table: str


class Weights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    last_updated: date
    description: str = ""
    pillar_order: List[str]
    profiles: Dict[str, List[float]]
    score_bands: Dict[str, ScoreBand]
    modifiers: ModifierConfig
    # Walk-forward tuned weights (lthcs/adaptive_weights.py). Free-form;
    # score.py validates the shape it consumes and falls back when malformed.
    adaptive_overrides: Optional[Dict[str, Any]] = None
    # Provenance written by scripts/lthcs_calibrate_bands.py --write.
    score_bands_calibration: Optional[ScoreBandsCalibration] = None
    # Closed cutoffs that produced older snapshots. Append-only: a --write
    # closes the outgoing live bands into this list before replacing them.
    score_bands_history: Optional[List[ScoreBandRegime]] = None

    @field_validator("pillar_order")
    @classmethod
    def _pillar_order_matches(cls, v: List[str]) -> List[str]:
        if v != PILLAR_ORDER:
            raise ValueError(
                f"pillar_order must be {PILLAR_ORDER}, got {v}"
            )
        return v

    @model_validator(mode="after")
    def _profiles_well_formed(self) -> "Weights":
        for name, weights in self.profiles.items():
            if len(weights) != len(PILLAR_ORDER):
                raise ValueError(
                    f"profile {name!r} has {len(weights)} weights, "
                    f"expected {len(PILLAR_ORDER)}"
                )
            if any(w < 0 or w > 1 for w in weights):
                raise ValueError(f"profile {name!r} has out-of-range weight: {weights}")
            total = sum(weights)
            if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
                raise ValueError(
                    f"profile {name!r} weights sum to {total}, expected 1.0"
                )
        return self

    @model_validator(mode="after")
    def _bands_cover_zero_to_hundred(self) -> "Weights":
        _bands_tile_0_to_100(self.score_bands, "score_bands")
        return self

    @model_validator(mode="after")
    def _history_matches_calibration(self) -> "Weights":
        history = list(self.score_bands_history or [])
        cal = self.score_bands_calibration
        if not history:
            if cal is not None:
                raise ValueError(
                    "score_bands_calibration is set but score_bands_history is empty; "
                    "closed regimes must be kept so older snapshots stay checkable"
                )
            return self
        if history[0].effective_from is not None:
            raise ValueError(
                "first score_bands_history regime must have effective_from null"
            )
        prev_until: Optional[date] = None
        for regime in history:
            if prev_until is not None and regime.effective_from != prev_until:
                raise ValueError(
                    "score_bands_history regimes must be contiguous "
                    f"(expected effective_from {prev_until}, "
                    f"got {regime.effective_from})"
                )
            prev_until = regime.effective_until
        if cal is None:
            return self
        if prev_until != cal.calibrated_at:
            raise ValueError(
                f"last score_bands_history effective_until ({prev_until}) must equal "
                f"score_bands_calibration.calibrated_at ({cal.calibrated_at})"
            )
        prev = cal.previous_cutoffs or {}
        last = history[-1].score_bands
        if set(prev) != set(last):
            raise ValueError(
                "score_bands_calibration.previous_cutoffs keys do not match "
                "the last closed regime"
            )
        for name, band in last.items():
            if prev.get(name) != [band.min, band.max]:
                raise ValueError(
                    f"score_bands_calibration.previous_cutoffs[{name!r}] does not "
                    f"match the last closed regime {[band.min, band.max]}"
                )
        return self
