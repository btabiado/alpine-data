"""Schema for data/lthcs/universe.json."""

from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Banks / insurers / exchanges / asset managers. weights.json has had
    # this profile since v1.3.0; the schema lagged behind it.
    "financial",
]

# CBOE: Cboe Global Markets lists on its own exchange (S&P 500 member).
Exchange = Literal["NYSE", "NASDAQ", "AMEX", "CBOE"]

TechSubBucket = Literal["Hardware", "Semiconductors", "Software", "IT Services"]

# The indexes the universe tracks in ``index_membership``.
TrackedIndex = Literal["S&P 500", "S&P 100", "NASDAQ-100", "DJIA"]


class IndexDrop(BaseModel):
    """One sourced index exit: which index, the effective date, the evidence."""

    model_config = ConfigDict(extra="forbid")

    index: TrackedIndex
    # Effective date of the index change per ``source``; null when the
    # source does not establish it. Never a guess.
    dropped_on: Optional[date] = None
    source: str = Field(min_length=1)


class IndexExile(BaseModel):
    """Marker for an "Index Exile".

    Owner's rule: a ticker that was in a tracked index (S&P 500, S&P 100,
    NASDAQ-100, DJIA) and has since left all of them stays ``active`` and is
    scored daily like any other name. It is grouped as an Index Exile and
    is NOT a member of any index, so index-level aggregates skip it (its
    ``index_membership`` is empty). Deactivation is only for delisting /
    no data (inactive_reason), never for leaving an index.
    """

    model_config = ConfigDict(extra="forbid")

    former_indexes: List[TrackedIndex] = Field(min_length=1)
    # Date it left its LAST tracked index (the day it became an exile), per
    # ``source``. Null when no source establishes the date.
    dropped_on: Optional[date] = None
    # Universe sync run that found the ticker without any index tag.
    detected_on: date
    source: str = Field(min_length=1)
    # Per-index exits with their own dates and evidence, when known.
    drops: List[IndexDrop] = Field(default_factory=list)
    note: Optional[str] = None

    @model_validator(mode="after")
    def _consistent(self) -> "IndexExile":
        if self.dropped_on and self.dropped_on > self.detected_on:
            raise ValueError("index_exile.dropped_on is after detected_on")
        for d in self.drops:
            if d.index not in self.former_indexes:
                raise ValueError(f"index_exile.drops names {d.index!r}, not in former_indexes")
        return self


class IndexHistoryEvent(BaseModel):
    """One entry of a ticker's exile / rejoin log (written by the sync)."""

    model_config = ConfigDict(extra="forbid")

    event: Literal["exiled", "rejoined"]
    # Run date of the sync that recorded the event.
    date: date
    # exiled: the indexes it left; rejoined: the indexes it is in again.
    indexes: List[TrackedIndex] = Field(default_factory=list)
    source: str = Field(min_length=1)
    # rejoined: the exile marker that was cleared, kept for the record.
    exile: Optional[IndexExile] = None


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
    # Technology-sector cohort split used by lthcs.peer_groups (spec §5).
    tech_sub_bucket: Optional[TechSubBucket] = None
    # Provenance fields written by the universe-expansion tooling
    # (scripts/lthcs_universe_expand.py, scripts/lthcs_universe_sp500_sync.py).
    # Documentation only; scoring reads peer groups from peer_groups.json.
    sector_group: Optional[str] = None
    cik: Optional[str] = Field(default=None, pattern=r"^\d{10}$")
    source: Optional[str] = None
    # UTC date this entry was added to universe.json, written by the tool
    # that added it (the commit that added it is the evidence). The daily
    # snapshot for that same date may have been cut before the change landed
    # (2026-10-05's was: snapshot 01:59Z, S&P 500 sync 02:08Z), so validators
    # expect the ticker from the NEXT snapshot date on. Null means "in the
    # universe before this field existed": validators then expect the ticker
    # from the first snapshot that scored it.
    added_on: Optional[date] = None
    # Index Exile marker (see IndexExile). Present only while the ticker is
    # in no tracked index; cleared (and logged in index_history) on rejoin.
    index_exile: Optional[IndexExile] = None
    index_history: List[IndexHistoryEvent] = Field(default_factory=list)

    @field_validator("ticker")
    @classmethod
    def _ticker_uppercase(cls, v: str) -> str:
        if v != v.upper():
            raise ValueError(f"ticker must be uppercase: {v!r}")
        return v

    @model_validator(mode="after")
    def _exile_has_no_index(self) -> "UniverseEntry":
        if self.index_exile is not None and self.index_membership:
            raise ValueError(
                f"{self.ticker}: index_exile set but index_membership is "
                f"{self.index_membership}; an exile is in no tracked index")
        return self

    @property
    def is_index_exile(self) -> bool:
        return self.index_exile is not None


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
