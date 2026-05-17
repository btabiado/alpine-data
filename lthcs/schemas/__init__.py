"""Pydantic schemas for every JSON file under data/lthcs/."""

from __future__ import annotations

from lthcs.schemas.universe import Universe, UniverseEntry
from lthcs.schemas.weights import Weights, ScoreBand, ModifierConfig

__all__ = [
    "Universe",
    "UniverseEntry",
    "Weights",
    "ScoreBand",
    "ModifierConfig",
]
