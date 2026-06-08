# SPEC — Money Flow Index (MFX)

A composite **−100…+100 money-flow gauge** for the 3 major US equity indices (Dow / S&P 500 / Nasdaq),
fusing ETF flows + a buy/sell-pressure ratio + money-market "cash on the sidelines" + mutual-fund flows.
Lives on V1 (`app.py`/`dashboard.html`), under the **Markets** tab group.

## Decisions (locked with user 2026-06-07)
- **Legs (all four):** (1) ETF flows [per-index], (2) Buy/sell ratio MFI/CMF [per-index], (3) Money-market/cash [market-wide, inverted], (4) Mutual-fund flows [market-wide, weak/netted].
- **Output:** per-index sub-gauges (Dow/SPX/Nasdaq) **and** a blended market-wide headline gauge. Per-index is first-class.
- **Scope:** full build in one pass.
- **Keys:** source cash + MF legs from the **keyless ICI weekly `.xls`** so they render in prod with NO FRED key. FRED `WRMFNS` is an *optional* confirmer that activates only if `FRED_API_KEY` is set (neutral-fallback otherwise).

## Data sources (verified live by research 2026-06-07)
| Leg | Source | Endpoint | Scope | Cadence | Notes |
|---|---|---|---|---|---|
| ETF flow | ΔSharesOutstanding × NAV | SPY/DIA/QQQ via Yahoo `quoteSummary` (crumb-gated) + issuer files | per-index | daily | needs SO **history** to z-score → accumulate; seed from issuer if possible |
| Buy/sell | MFI / Chaikin MF / OBV | computed from SPY/DIA/QQQ OHLCV | per-index | daily | **zero new feed**; needs `yahoo_chart_history()` widened to keep O/H/L |
| Cash/MMF | ICI weekly MMF | `https://www.ici.org/mm_summary_data_2026.xls` (200, 48KB) | market-wide | weekly | total $7.89T; use **WoW change**, enter **inverted**, short z-window (rate drift) |
| Cash/MMF (opt) | FRED `WRMFNS` retail MMF | FRED API series_map | market-wide | weekly | only if `FRED_API_KEY` set; else neutral |
| MF flow | ICI weekly equity flows | `https://www.ici.org/flows_data_2026.xls` | market-wide | weekly | Domestic/World equity rows; **net against ETF flow** (MF→ETF migration); first-print z to avoid revision look-ahead |

(URLs bake in the year → roll each Jan. SKIP: discontinued FRED WIMFSL/WRMFSL/IMFSL; frozen cdn.cboe.com put/call CSV.)

## Composite method (CNN Fear & Greed template on existing LTHCS scorer)
Reuse `lthcs/normalize.py` (`z_score` → `z_to_0_100`) and `lthcs/index_aggregate.py` (capped contributions, band labels).
- Each component → trailing **z-score** (rolling window) → centered to ±, equal-weight unless noted.
- **Per-index sub-score** `i ∈ {DIA→Dow, SPY→SPX, QQQ→Nasdaq}` = mean(z(ETF flow), z(MFI−50)) → ±100.
- **Market headline** = blend of [dollar-weighted mean of the 3 sub-scores] + [ICI equity-MF-flow z (market-wide)] + [inverted MMF WoW z (market-wide)], capped ±100, band-labeled.

## Guardrails (invariants)
- **No double-counting:** ETF-flow ΔSO×NAV is per-index ONLY; ICI MF flow is market-wide ONLY. Never sum SPY flow into the ICI aggregate.
- **Cadence:** daily legs drive movement; weekly ICI/MMF forward-filled w/ decay so the gauge doesn't lurch on Wed releases.
- **Revision/look-ahead:** snapshot ICI first-prints; z-score on first-prints; show revised separately.
- **Drift:** z-score *changes* of MMF/flows on a rolling window, never raw levels; MMF enters **inverted** (unit-test the sign).
- **Graceful degradation:** missing leg → neutral 50/0, renormalize remaining weights (no blank gauge).

## File plan
- NEW `money_flow.py` — `mfi()`, `cmf()`, `obv()`, `build_money_flow_index(payload)` → MFX dict.
- NEW `fetch_equity_etf_flows.py` — SPY/DIA/QQQ ΔSO×NAV → `data/equity_etf_flows.csv` + payload block.
- NEW `fetch_money_flows.py` — ICI MMF + ICI equity flows → `data-mmf.json`, `data-mf-flows.json`.
- EDIT `fetch_market.py` — widen `yahoo_chart_history()` to keep O/H/L; add `retail_mmf: WRMFNS` to FRED `series_map`; wire new fetchers into `fetch_all()` payload (`market.money_flow`).
- EDIT `app.py` — new Money Flow sub-tab under Markets; render headline + 3 sub-gauges + stacked-flow bar + component sidebar; call `money_flow.build_money_flow_index`.
- NEW CI `.github/workflows/money-flow-daily.yml` — daily ETF-flow snapshot that commits `data/equity_etf_flows.csv` (the one file needing cross-build accumulation for ΔSO×NAV). The ICI MMF/MF `.xls` legs + the composite are recomputed fresh in the existing pages build (`app.py --fetch-market` → `build_money_flow_payload`); their `data-*.json` sidecars are gitignored & regenerated, not committed.
- TESTS `tests/test_money_flow.py` — MFI math, inversion sign, neutral-fallback, double-counting scope.

## Phases
0. Data layer (this build): the 3 new modules, live-verified to produce real numbers.
1. Integration: fetch_market wiring + composite.
2. UI: tab + gauges + sidebar.
3. CI + tests + local render verify. **Push gated — do not push without user OK.**
