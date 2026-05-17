"""Schema for data/lthcs/universe.json."""

from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

MaturityStage = Literal[
    "pre_revenue",
    "pre_profit_growth",
    "path_to_profitability",
    "profitability_inflection",
    # Compounder family — split in v1.1.0 so peer-relative percentiles
    # benchmark like-for-like instead of mixing AAPL's +6% growth with
    # NVDA's +65% AI-cycle growth in one pool.
    "standard_compounder",
    "mature_compounder",   # added v1.1.0
    "growth_compounder",   # added v1.1.0
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
    # Free-text note explaining why a ticker was deactivated (e.g. taken
    # private, acquired, delisted). Required to be readable in the JSON
    # file, so retained as an optional field rather than a sidecar log.
    inactive_reason: Optional[str] = None
    # Free-text note explaining a non-default maturity_stage choice — e.g.
    # "growth_compounder: AI capex (+65%)" for NVDA. Documentation only;
    # not consumed by scoring code.
    maturity_note: Optional[str] = None

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
