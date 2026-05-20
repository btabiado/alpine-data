# LTHCS Parking Lot

Items consciously deferred — with rationale + revisit date.

Last updated: 2026-05-20

---

## 📅 Time-gated (data needs to accumulate first)

| Item | Revisit | Why |
|---|---|---|
| **Re-run Phase 3 audits** with post-Finnhub data | ~2026-05-26 | Today's audit was on pre-Finnhub data; Thesis IC understated. ~7 days of Finnhub-driven snapshots needed for stable re-audit. |
| **Risk & Observability tab** at `/lthcs/risk/` | ~2026-05-27 | Wait until after the Phase 3 re-audit so we know what concretely belongs in the view. Glue layer over Dependabot + CodeQL + Trufflehog + LLM-guardrail logs + data-quality deltas. Don't build a parallel scanner — aggregate existing tools. |
| **Universe doubling** (S&P 500 expansion, ~167 → ~500 in 3 waves) | 2026-05-27 (Wave A) | Scalability prep infrastructure ships TODAY (script + scaletest + seed + plan doc) but NO production universe change until calibration window closes 2026-05-26. Then 3 waves: +50 → verify → +100 → verify → +183. Plan in `docs/lthcs-universe-expansion-plan-2026-05-27.md`. |
| **Adoption β IC re-validation** | ~2026-06-17 | Needs ~30 days of forward data after `333e5dd`. β-verdict-monthly cron auto-fires June 1. |
| **#24 Phase 4 verdict promotion** + **#25 Adaptive Weights V2 SHIP** | ~July 2026 | Plumbing built in `306176a`. Promotion gates when 20 OOS observations accumulate at h=21d. Verdict flips HOLD→SHIP automatically via existing cron. |
| **Calibration tweak**: `mature_compounder` Thesis 0.20→0.25 | After 2026-05-26 re-audit | A/B run already validated direction (+0.184 Sharpe on pre-Finnhub data — should be stronger post-Finnhub). |

---

## ⏸️ User-judgment deferred (call when ready)

| Item | Notes |
|---|---|
| **P1 #4 API key rotation** (Anthropic, Finnhub, AV, FRED, etc.) | Revisit in 90-180 days. No keys auto-expire. Quarterly calendar reminder set for 2026-08-15. |
| **LLM sentiment production gate flip** | Shadow data accumulating. Flip the `LTHCS_LLM_SENTIMENT_PROMOTE_TO_PROD` (or equivalent) once ~30 days of shadow data clears and metrics confirm. |
| **P3 #19 workflow log retention** | Could tighten from default 90d to 30d in Settings → Actions. ~30 seconds. |
| **P3 #20 robots.txt** | Currently search engines may index `/data/lthcs/*.json`. Add `robots.txt` at site root if you want only HTML pages indexed. |
| **P4 #33 OS hardening on laptop** | FileVault, firmware password, auto-lock, separate admin account. ~30 min one-time. Genuine defensive value. |

---

## 🚫 Explicitly decided NOT to ship

| Item | Why we're not doing it |
|---|---|
| Time-windowed service blackouts (9pm-6am access cut) | Mostly security theater; better to pause specific noisy crons during dev (already an option) than ACL by time |
| Parallel "threat scanner" agent | Would duplicate Dependabot/CodeQL/Trufflehog; build risk-aggregation tab instead (parked above) |
| #16 Sector-relative Institutional pillar (audit-flagged "don't fix") | Peer-group audit recommended KEEP universe-relative |
| P4 #31 Penetration test ($1k–$10k) | Overkill for public read-only dashboard with no user data |
| P4 #32 SOC 2 / ISO 27001 certification | N/A — not selling to enterprises |
| P4 #34 Bug bounty program | Cost > benefit at this scale |

---

## How to use this doc

When picking up after time gates clear:
1. Check the date column → if today ≥ "Revisit", that item is ripe
2. Read the rationale (the "Why" column) before re-engaging — context decays fast
3. Move items from "Time-gated" or "User-judgment" to a new top-of-file "Recently shipped" section once done

When a new "we should do this someday" idea appears: park it here with a revisit date + rationale. Don't just open an issue and forget.
