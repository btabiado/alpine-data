# LTHCS Threat Model

**Date:** 2026-05-20
**Owner:** Bryan (single maintainer)
**Scope:** LTHCS tab of the BTC/ETH ETF Dashboard repo and its public deployment

This document captures what threats we defend against, what we explicitly do
not, and where the trust boundaries sit. It is meant to orient a stranger
picking up the project cold and to anchor quarterly security reviews.

---

## 1. System overview

LTHCS (Long-Term Holder Conviction Score) is a single-user, public, read-only
market dashboard tab served from GitHub Pages at
`https://btabiado.github.io/alpine-data/lthcs/`. A daily GitHub
Actions pipeline pulls public market and macro data (Yahoo Finance, Finnhub,
SEC EDGAR, FRED, and similar) on a schedule, scores it against an in-tree
model, writes JSON snapshots and a static HTML/JS frontend, and pushes the
output back to `main` via an automated bot (LTHCS-bot). There are no end
users, no accounts, no PII, and no write paths from the public site back into
the system. The Anthropic API is used by gated *shadow* stages only; LLM
output is never wired into the production score.

---

## 2. Trust boundaries

Each arrow crosses a trust boundary; assume the other side is hostile until
proven otherwise.

- **Public internet → GitHub Pages CDN.** Read-only. The CDN serves static
  HTML/JSON. There is no server-side code path to attack.
- **GitHub Actions runners → external data APIs** (Yahoo, Finnhub, SEC EDGAR,
  FRED, Alpha Vantage, Anthropic, etc.). Each upstream is treated as
  untrusted input. Runners hold short-lived API keys via `${{ secrets.X }}`.
- **Anthropic API → LLM responses → shadow stages.** LLM output crosses into
  our process boundary but is gated and never reaches the production score
  surface.
- **Bryan's laptop → `origin/main`.** SSH-signed commits over SSH. The laptop
  is the only human-authored push path.
- **LTHCS-bot → `origin/main`.** Workflow-authored, unsigned commits.
  Provenance is verifiable via the workflow source in-tree and the run logs
  on the Actions tab.
- **Repo secrets store → workflow env.** Secrets are only exposed to
  workflows that explicitly request them; never logged in plaintext (Actions
  redacts known secret values).

---

## 3. Threats considered (STRIDE)

| Threat | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Spoofing** — attacker impersonates Bryan or LTHCS-bot to push code | Low | High | SSH-signed commits from Bryan (P1 #8); branch protection on `main` (no force push, no delete, linear history); GitHub auth + 2FA assumed on the account; bot commits are limited to LTHCS paths and traceable to a specific workflow file in-tree. |
| **Tampering** — malicious change in an upstream Python dependency | Low–Med | High | Pinned versions in `requirements.txt` (P0); Dependabot weekly PRs; CodeQL on push + weekly; Trufflehog weekly; GitHub Actions SHA pinning in progress (P4 #25). |
| **Repudiation** — disputed authorship of a change | n/a | n/a | Not applicable. Single-user project, no audit-log requirement. Signed commits + workflow run logs are sufficient. |
| **Information disclosure** — API keys leaked via logs, commits, or build output | Low | Med–High | `.env` gitignored; secrets only via `${{ secrets.X }}` in workflows; Actions log masking on known secret values; weekly Trufflehog; public-manifest audit run on push (CLEAN as of 2026-05-20); secret-scanning push protection enabled (P3 #15). |
| **Denial of service** — flood of requests against the public site | Low | Low | Served behind GitHub's CDN; static content only; no origin to exhaust. Not our problem. |
| **Elevation of privilege** — attacker gains repo push access without authorization | Low | High | GitHub auth + 2FA; branch protection caps damage to a single revertable commit; Private Vulnerability Reporting enabled (P3 #16); no third-party collaborators. |
| **Prompt injection in LLM shadow** — news article tries to override the system prompt | Med | Low | Input sanitization + delimiter wrapping + structured output validation (P1 #5); shadow output is gated and never reaches the production score. |
| **Public site abuse** — scraping, hot-linking, or hammering the public JSON endpoints | High | None | All published data is public; CDN absorbs load; no privacy interest. Not a real threat. |

---

## 4. Explicitly OUT of scope

- **Multi-user and authenticated workflows.** There is one user (Bryan).
  Anything that requires login, role-based access, or session management is
  not a goal.
- **Regulatory compliance** (SOC 2, ISO 27001, PCI-DSS, GDPR, HIPAA). No
  user data, no enterprise customers, no regulated workloads.
- **Real-money or brokerage integration.** Paper-money only. No order entry,
  no custody, no exchange API keys.
- **Privacy of LLM API queries.** Anthropic's standard data policies apply;
  no special handling is layered on top.
- **Nation-state-grade attackers.** Impractical to defend against at this
  scale.
- **Insider threat.** Bryan *is* the insider; there is no team to model
  against.

---

## 5. Accepted risks

- **LTHCS-bot commits are unsigned.** Provenance is provable via the
  in-tree workflow source plus the corresponding Actions run; we accept this
  rather than provisioning a bot signing key.
- **pytrends rate-limiting** causes occasional small lags in Phase 2 Trends
  data. This is a data-freshness annoyance, not a security issue.
- **Alpha Vantage `NEWS_SENTIMENT` free-tier multi-ticker AND-filter** drove
  the migration to Finnhub for news. The quirk is documented in user memory
  and Thesis stays neutral 50 in the V1 daily pipeline.
- **LLM shadow output may contain hallucinated narratives.** Gated shadow
  only; never surfaced as production score input.
- **A compromised dependency could land before Dependabot's weekly PR opens.**
  Acceptable time-to-detect at this scale; CodeQL + Trufflehog provide a
  second net.

---

## 6. Defenses in place

Current inventory of deployed controls:

- Dependabot, weekly cadence (P0)
- CodeQL on every push and weekly scheduled scan (P1 #7)
- Trufflehog weekly secret scan (P1 #7)
- Secret-scanning push protection (P3 #15)
- Private Vulnerability Reporting (P3 #16)
- Branch protection on `main`: linear history, force push blocked, delete
  blocked
- SSH-signed commits from Bryan's laptop (P1 #8)
- Pinned Python dependencies in `requirements.txt` (P0)
- LLM input sanitization, delimiter wrapping, and output validation (P1 #5)
- CSP + security meta-headers on the LTHCS tab (P2 #9)
- Public manifest audit on every push (CLEAN as of 2026-05-20)

---

## 7. Review cadence

- **Quarterly review** every 90 days, anchored at 2026-08-15 per the
  calendar reminder.
- **Phase 3 quality re-audit** around 2026-05-26, after ~30 days of
  post-Finnhub news data.
- **β IC verdict** runs ~2026-06-17 via the monthly cron.
- **Ad-hoc review** triggered by a major refactor or any new data source.

---

## 8. Open questions and known gaps

- **API key rotation cadence** (P1 #4) — deferred; no current schedule.
- **Workflow run-log retention** is GitHub's default 90 days. Could be
  tightened (P3 #19).
- **OS-level laptop hardening** (P4 #33) is Bryan's responsibility on the
  endpoint, not the repo.
- **`robots.txt`** is currently absent (P3 #20). Low priority — site is
  intentionally public.
