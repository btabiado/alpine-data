# Security audit pass 2 — git history + workflow logs

**Date:** 2026-05-20
**Scope:** Audit-only; no code changes.
**Trigger:** P0 security pass follow-on (after `e679ad4` + `f57b20e`: pin tightening, SECURITY.md, manifest audit, dependabot).
**Repo:** `btc-eth-etf-dashboard` (public).

---

## Verdicts

| Surface | Verdict |
| --- | --- |
| Git history (all 402 commits, all refs) | **CLEAN** |
| Recent workflow logs (10 latest runs across `tests`, `pages`, `Automatic Dependency Submission`) | **CLEAN** |

No secrets, tokens, or sensitive values were found in either surface.

---

## Part 1 — Git history scan

### Methodology

1. **`trufflehog` availability check** — `which trufflehog` → not installed (skipped per spec; install is heavy).
2. **Fallback: regex sweep across all commits / all refs:**
   ```
   git log --all --full-history -p | grep -iE '(api[_-]?key|secret[_-]?key|access[_-]?token|client[_-]?secret|password|bearer\s+[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9-]{20,}|ghp_[A-Za-z0-9]{36,}|AKIA[0-9A-Z]{16})'
   ```
3. **High-confidence vendor key-prefix sweep** (real-value tells):
   ```
   grep -E 'sk-ant-[A-Za-z0-9_-]{10,}'                  # Anthropic
   grep -E 'ghp_[A-Za-z0-9]{36,}'                       # GitHub PAT
   grep -E 'AKIA[0-9A-Z]{16}'                           # AWS access key ID
   grep -E 'xox[bp]-[0-9]+'                             # Slack
   grep -E 'AIza[A-Za-z0-9_-]{35}'                      # Google
   ```
   → **Zero hits** across all five vendor prefixes.
4. **Targeted file audits:**
   - `.env` — `git log --all -- .env` returns empty; never committed. `.gitignore` blocks it.
   - `requirements.txt` — only package names + version specifiers; no `api_key`/`token`/`password` literals.
   - `data/lthcs/*.json` — `grep -ilrE '(Authorization|Bearer )' data/lthcs/` returns nothing.
   - `.github/workflows/*.yml` — every secret reference uses `${{ secrets.X }}`. No hardcoded values. Confirmed across history.

### Match triage

The broad regex returned ~80 lines. All fall into safe categories:

| Category | Example | Safety |
| --- | --- | --- |
| `${{ secrets.X }}` workflow refs | `ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}` | Safe — placeholder |
| Env var **names** in code | `ENV_API_KEY = "ANTHROPIC_API_KEY"` | Safe — string literal of the var name |
| Env var **reads** | `os.getenv("FRED_API_KEY")` | Safe — code reads value at runtime |
| Test placeholders | `monkeypatch.setenv("ANTHROPIC_API_KEY", "test-`...`")` | Safe — `test-`... not a real key |
| Doc / `.env.example` entries | `FRED_API_KEY=xxx`, `ANTHROPIC_API_KEY=sk-a`...`-...` | Safe — `xxx` / `...` placeholders |
| Commit-message text | `"USER ACTION: add ANTHROPIC_API_KEY to repo secrets"` | Safe — narrative only |

No commit, no file, no diff contains an actual secret value.

---

## Part 2 — Workflow logs scan

### Methodology

Pulled the last 10 completed runs via `gh run list --limit 30 --status completed` (skipping `in_progress`), spanning `tests`, `pages`, and `Automatic Dependency Submission (Python)`:

```
26156489932 26156446754 26156444588 26156443053 26156440257
26156435786 26156432532 26156426928 26156396539 26156396575
```

For each: `gh run view <id> --log | grep -iE '(API_KEY|SECRET|TOKEN|Bearer|Authorization)'`.

### Findings

Every secret-shaped value is properly masked by GitHub Actions:

```
build  Fetch live data + generate dashboard.html  FRED_API_KEY: ***
build  Fetch live data + generate dashboard.html  GLASSNODE_API_KEY:        (empty — not configured)
build  Fetch live data + generate dashboard.html  CRYPTOCOMPARE_API_KEY: ***
... checkout  AUTHORIZATION: basic ***
... checkout  token: ***
```

Non-secret matches (false positives, safe to log):
- `TokenBucket` rate-limit class names in pytest output (`test_token_bucket_exhaustion_raises_rate_limit`).
- Env var **names** echoed by `Set up job` group headers (`GITHUB_TOKEN Permissions`).
- `Secret source: Actions` / `Dependabot` — GitHub Actions infrastructure label, not a value.

No raw key bytes, no `sk-ant-`/`ghp_`/`AKIA`/`Bearer <token>` strings, no echoed `print(os.environ["X"])` leaks.

---

## Actionable findings

**None.** Both surfaces are clean.

## Recommended follow-up

1. **Optional:** add `trufflehog` to CI as a scheduled weekly job. Low priority — current grep + masking is sufficient given the secret hygiene already in place.
2. **Keep doing what's already working:** `${{ secrets.X }}` everywhere, `.env` gitignored, `.env.example` only ever ships placeholder `=xxx` or `=sk-ant-...` doc snippets.
3. Continue using GitHub Actions native secret masking — confirmed working across the audited runs.

---

## Cross-reference

- Sibling pass (CCC, shipped): `e679ad4` (pin tightening, SECURITY.md, manifest audit) + `f57b20e` (dependabot).
- This pass (DDD): audit-only, no code touched.
- Active siblings on adjacent surfaces: EPSILON, BBB.
