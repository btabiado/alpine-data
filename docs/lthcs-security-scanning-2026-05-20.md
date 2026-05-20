# Security scanning CI — 2026-05-20

Adds two scheduled CI workflows under `.github/workflows/`:

## `trufflehog-weekly.yml`
- **Schedule:** Sunday 03:00 UTC (also `workflow_dispatch` for manual runs)
- **Scope:** `git file://. --only-verified --since-commit=HEAD~200` — last ~200 commits
- **Fail-loud:** Job goes red and posts a Job Summary if a verified secret is found
- **Why bounded scope:** Full-history scan is slow; rolling 200-commit window keeps weekly run fast while covering all recent activity. Initial baseline scan should be one-off `workflow_dispatch` without `--since-commit`.
- **Origin:** DDD's optional follow-up from review SHA `a00bd91` — user approved adding it.

## `codeql.yml`
- **Schedule:** Push to `main`, PRs to `main`, weekly Sunday 04:00 UTC
- **Language:** Python only (entire codebase)
- **Query suite:** `security-and-quality` — catches injection, deserialization, path traversal, plus general code-quality smells
- **Origin:** Security audit P1 item #7 — static analysis was a gap.

## Why both?
- Trufflehog catches **secrets** (API keys, tokens) committed by mistake — runtime data leakage.
- CodeQL catches **vulnerable patterns** in source (e.g., SQL injection sinks, `pickle.loads` on untrusted input) — code-shape issues.
- Different classes of risk → both warranted.

## Operational notes
- Both run on schedule, so cost is bounded (2 runs/week + CodeQL on push).
- Findings land in **Security** tab of the GitHub repo (CodeQL) and Actions logs (Trufflehog).
- No app code touched, no app workflow touched — purely additive CI.
