# LTHCS public manifest security audit — 2026-05-20

**Auditor**: swarm agent CCC (security pass)
**Scope**: `data/lthcs/public/manifest.json` and every endpoint it exposes.
**Verdict**: **CLEAN** — no leaks found.

## Files reviewed

Direct:
- `data/lthcs/public/manifest.json`
- `data/lthcs/public/latest_snapshot.json`

Endpoints referenced by manifest (`data_endpoints[]`):
- `/data/lthcs/snapshots/2026-05-18.json` — full equity-universe snapshot (168 tickers)
- `/data/lthcs/snapshots_crypto/2026-05-18.json` — crypto-universe snapshot (10 tickers)
- `/data/lthcs/history/by_ticker/<TICKER>.json` — per-ticker history (templated path)
- `/data/lthcs/narratives/2026-05-18.json` — 167 templated narratives
- `/data/lthcs/narratives_llm/2026-05-18.json` — gated; not present today (expected)
- `/data/lthcs/variable_detail/2026-05-18.json` — per-(ticker, pillar) breakdown
- `/data/lthcs/backtest/<RUN_ID>/equity_curve.json` — templated path
- `/data/lthcs/weights.json` — pillar weights by maturity stage

## Methodology

1. Listed all endpoints declared in `manifest.json` and verified each concrete
   file exists (templated paths skipped; `narratives_llm` is intentionally
   gated and absent today).
2. Ran a multi-pattern regex sweep across all files looking for:
   - OpenAI / Anthropic-style keys (`sk-...`, `Bearer ...`)
   - Strings like `api_key`, `secret`, `password`, `token`
   - Local filesystem paths (`/Users/`, `/home/`)
   - Email addresses
   - Internal hostnames / private IP ranges (`localhost`, `127.0.0.1`,
     `192.168.`, `10.0.`)
   - Engineering markers (`TODO`, `FIXME`, `XXX`, `HACK`, `internal`,
     `proprietary`)
3. Programmatically inspected JSON structure (top-level keys + array sample
   shapes) to confirm payload is bounded, numeric, and matches the documented
   public schema.
4. Spot-read narrative text (highest-risk for free-form proprietary commentary)
   and confirmed it is templated, formulaic, and does not expose model
   internals or any non-public reasoning.

## Findings

- **No API keys, tokens, or bearer credentials.**
- **No filesystem paths or internal hostnames.**
- **No email addresses or PII.**
- **No "internal" / "proprietary" markers in payloads.**
- **No engineering TODO / FIXME / HACK comments leaking into JSON.**
- Narrative content is fully templated ("AAPL is showing Weakening at 54.2…")
  and exposes only band names, sub-score numerics, and pillar names that are
  already documented in the manifest and the public README.
- Schema is consistent with the manifest's declared shape.
- `latest_snapshot.json` is a verbatim copy of the latest dated snapshot —
  exposes nothing the dated file doesn't.

## Recommendations

None for this pass. Re-audit if:
- The narrative pipeline ever switches from templated to LLM-authored on the
  public mirror (LLM SHADOW gated path) — re-run this sweep on the first
  `narratives_llm/<date>.json` shipped publicly.
- Any new endpoint is added to `manifest.json`'s `data_endpoints` list.
- The pipeline ever emits free-form fields sourced from upstream APIs without
  a sanitization step.
