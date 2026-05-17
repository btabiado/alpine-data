"""Schema for data/lthcs/weights.json."""

from __future__ import annotations

from datetime import date
from typing import Dict, List

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
        covered: list[tuple[int, int]] = sorted(
            (b.min, b.max) for b in self.score_bands.values()
        )
        if not covered:
            raise ValueError("score_bands is empty")
        if covered[0][0] != 0:
            raise ValueError(f"score_bands do not start at 0 (start={covered[0][0]})")
        if covered[-1][1] != 100:
            raise ValueError(f"score_bands do not end at 100 (end={covered[-1][1]})")
        for (a_min, a_max), (b_min, b_max) in zip(covered, covered[1:]):
            if b_min != a_max + 1:
                raise ValueError(
                    f"score_bands gap or overlap between [{a_min},{a_max}] "
                    f"and [{b_min},{b_max}]"
                )
        return self
