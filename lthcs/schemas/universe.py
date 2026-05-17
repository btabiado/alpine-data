"""Schema for data/lthcs/universe.json."""

from __future__ import annotations

from datetime import date
from typing import List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MaturityStage = Literal[
    "pre_revenue",
    "pre_profit_growth",
    "path_to_profitability",
    "profitability_inflection",
    "standard_compounder",
    "recovery_stabilization",
    "recovery_operational",
    "recovery_earnings",
    "recovery_rerating",
]

Exchange = Literal["NYSE", "NASDAQ", "AMEX"]


class UniverseEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=10)
    name: str = Field(min_length=1)
    exchange: Exchange
    index_membership: List[str] = Field(default_factory=list)
    sector: str
    industry: str
    maturity_stage: MaturityStage = "standard_compounder"
    active: bool = True

    @field_validator("ticker")
    @classmethod
    def _ticker_uppercase(cls, v: str) -> str:
        if v != v.upper():
            raise ValueError(f"ticker must be uppercase: {v!r}")
        return v


class Universe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    last_updated: date
    description: str = ""
    tickers: List[UniverseEntry]

    @field_validator("tickers")
    @classmethod
    def _unique_tickers(cls, v: List[UniverseEntry]) -> List[UniverseEntry]:
        seen: set[str] = set()
        dupes: list[str] = []
        for entry in v:
            if entry.ticker in seen:
                dupes.append(entry.ticker)
            seen.add(entry.ticker)
        if dupes:
            raise ValueError(f"duplicate tickers in universe: {dupes}")
        return v
