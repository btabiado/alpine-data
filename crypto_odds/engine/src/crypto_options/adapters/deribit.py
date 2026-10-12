"""Deribit public-API adapter: listings, one-call-per-currency chain snapshots, index prices.

Each asset is fetched independently; a failure for one asset or currency becomes an explicit
status entry and never blocks the others (brief s.1, s.9).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .http import JsonClient, RawArchive

INSTRUMENT_TYPE = {"inverse": "reversed", "linear": "linear"}


@dataclass
class ChainSnapshot:
    asset: str
    family: str
    role: str
    currency: str
    instruments: list = field(default_factory=list)   # get_instruments rows for this asset/family
    summaries: list = field(default_factory=list)     # get_book_summary rows for those instruments
    retrieved_at: object = None
    raw_paths: list = field(default_factory=list)
    status: str = "ok"
    error: str | None = None


def make_client(cfg: dict, archive: RawArchive | None, session=None) -> JsonClient:
    v = cfg["venues"]["deribit"]
    return JsonClient(v["base_url"], archive, "deribit", v["min_interval_s"], v["max_retries"],
                      v["backoff_s"], v["timeout_s"], session=session)


def _result(fr):
    if not fr.ok:
        return None, fr.error
    data = fr.data
    if isinstance(data, dict) and "error" in data:
        return None, f"API error: {data['error']}"
    return data.get("result"), None


def snapshot_chains(cfg: dict, assets: list[str], client: JsonClient) -> list[ChainSnapshot]:
    chains_cfg = cfg["venues"]["deribit"]["chains"]
    wanted = [(a, c) for a in assets for c in chains_cfg.get(a, [])]
    by_currency: dict[str, dict] = {}
    for currency in sorted({c["currency"] for _, c in wanted}):
        inst_fr = client.get("public/get_instruments", {"currency": currency, "kind": "option", "expired": "false"})
        book_fr = client.get("public/get_book_summary_by_currency", {"currency": currency, "kind": "option"})
        insts, e1 = _result(inst_fr)
        books, e2 = _result(book_fr)
        by_currency[currency] = {
            "instruments": insts, "books": books, "error": e1 or e2,
            # quotes and forwards come from the single book-summary response
            "retrieved_at": book_fr.retrieved_at,
            "raw_paths": [p for p in (inst_fr.raw_path, book_fr.raw_path) if p],
        }

    out = []
    for asset, c in wanted:
        snap = ChainSnapshot(asset, c["family"], c["role"], c["currency"])
        got = by_currency[c["currency"]]
        snap.retrieved_at, snap.raw_paths = got["retrieved_at"], got["raw_paths"]
        if got["error"] or got["instruments"] is None or got["books"] is None:
            snap.status, snap.error = "failed", got["error"] or "empty response"
            out.append(snap)
            continue
        itype = INSTRUMENT_TYPE[c["family"]]
        snap.instruments = [i for i in got["instruments"]
                            if i.get("base_currency") == c["base"] and i.get("instrument_type") == itype]
        names = {i["instrument_name"] for i in snap.instruments}
        snap.summaries = [b for b in got["books"] if b.get("instrument_name") in names]
        if not snap.instruments:
            snap.status, snap.error = "unavailable", f"no listed {c['family']} {c['base']} options"
        out.append(snap)
    return out


def index_prices(cfg: dict, assets: list[str], client: JsonClient) -> dict:
    """Spot index prices for each USD/USDC pair, used for the depeg check and proportional targets."""
    out = {}
    for asset in assets:
        for name in cfg["venues"]["deribit"]["depeg_pairs"].get(asset, []):
            fr = client.get("public/get_index_price", {"index_name": name})
            res, err = _result(fr)
            out[name] = {"price": None if err else res.get("index_price"), "error": err,
                         "retrieved_at": fr.retrieved_at.isoformat() if fr.retrieved_at else None,
                         "raw_path": fr.raw_path}
    return out


def discover(cfg: dict, assets: list[str], client: JsonClient) -> list[dict]:
    """Programmatic listing discovery: every option base currency Deribit lists, per requested asset."""
    fr = client.get("public/get_instruments", {"currency": "any", "kind": "option", "expired": "false"})
    res, err = _result(fr)
    rows = []
    for asset in assets:
        if err:
            rows.append({"asset": asset, "status": "failed", "detail": err})
            continue
        mine = [i for i in res if i.get("base_currency") == asset]
        fams = sorted({i["instrument_type"] for i in mine})
        expiries = sorted({i["expiration_timestamp"] for i in mine})
        rows.append({"asset": asset, "status": "listed" if mine else "unavailable",
                     "instrument_types": fams, "n_instruments": len(mine), "n_expiries": len(expiries),
                     "detail": "" if mine else cfg.get("unavailable_reasons", {}).get(asset, "no listed options")})
    return rows
