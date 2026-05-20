# LTHCS — GitHub Actions SHA Pinning

**Date:** 2026-05-20
**Owner:** Security hardening (P4 #25)
**Scope:** `.github/workflows/*.yml`

## Why SHA-pin?

A `uses:` line like `actions/checkout@v4` references a **mutable** Git tag.
The Action's maintainer (or anyone who compromises their account) can
re-point `v4` at a different commit, and the next CI run silently
executes new code with full repo-write secrets in scope.

The supply-chain attack on the `tj-actions/changed-files` Action in
March 2025 is the canonical example: a single tag move exfiltrated
secrets from thousands of CI pipelines before anyone noticed.

A 40-character commit SHA is **immutable**. Once we pin
`actions/checkout@34e11487...`, no upstream tag move can change what
runs in our workflows. The attacker would need to compromise GitHub's
content-addressable store itself — a much higher bar.

## What changed

Every `uses:` reference in `.github/workflows/*.yml` was rewritten from
`<owner>/<repo>@<tag>` to `<owner>/<repo>@<40-char-sha> # <tag>`.

### Sample

**Before:**
```yaml
- uses: actions/checkout@v4
```

**After:**
```yaml
- uses: actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5 # v4
```

The trailing `# v4` comment is the human-readable hint — it tells you
what tag the SHA resolved to at pinning time, so future readers can
audit whether the SHA is still on the current `v4` branch without
having to look it up.

### Pinned actions

| Action                              | Tag | SHA                                      |
|-------------------------------------|-----|------------------------------------------|
| `actions/checkout`                  | v4  | `34e114876b0b11c390a56381ad16ebd13914f8d5` |
| `actions/setup-python`              | v5  | `a26af69be951a213d495a4c3e4e4022e16d87065` |
| `actions/cache`                     | v4  | `0057852bfaa89a56745cba8c7296529d2fc39830` |
| `actions/upload-artifact`           | v4  | `ea165f8d65b6e75b540449e92b4886f43607fa02` |
| `actions/upload-pages-artifact`     | v3  | `56afc609e74202658d3ffba0e8f6dda462b719fa` |
| `actions/deploy-pages`              | v4  | `d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e` |
| `github/codeql-action/{init,autobuild,analyze}` | v3 | `458d36d7d4f47d0dd16ca424c1d3cda0060f1360` |

### Exception: `trufflesecurity/trufflehog@main`

The Trufflehog secret-scanner is intentionally left at `@main`.
Trufflehog's value is its detector ruleset, which the vendor actively
curates; pinning a SHA freezes the rules and degrades scanner
effectiveness over time. We accept the supply-chain risk for this
single Action because:

1. It runs weekly in a sandboxed `permissions: contents: read` job
2. Its only output is a step-summary message + exit code
3. There is no secret exposure beyond `GITHUB_TOKEN` (read-only)

Revisit this decision if `trufflesecurity/trufflehog` is hijacked
upstream or if we ever pass it scoped credentials.

## How Dependabot keeps SHAs current

`.github/dependabot.yml` already enrolls the `github-actions`
ecosystem:

```yaml
- package-ecosystem: "github-actions"
  directory: "/"
  schedule: { interval: "weekly", ... }
```

With SHA pins in place, Dependabot will open a PR every time the
upstream tag (`v4`, `v5`, etc.) moves to a newer commit. The PR body
will include the changelog, and merging it updates both the SHA and
the trailing `# vN` comment.

**Trade-off:** more PR noise than tag pins (one PR per upstream
release vs. zero). That's the cost of an immutable supply chain.

## How to verify a SHA at the upstream

```bash
gh api "/repos/actions/checkout/commits/v4" --jq '.sha'
```

The output should match the SHA in the `uses:` line. If it doesn't,
the upstream tag has moved since we pinned — Dependabot should have
already filed a PR, but it's worth confirming there's no security
advisory you need to act on first.

## Future maintenance

- **Adding a new Action:** resolve its SHA with `gh api` and add the
  `# vN` comment. Don't paste raw `@v4` references — they bypass the
  whole point of this exercise.
- **Dependabot PR review:** the SHA changing is expected; verify the
  changelog has no surprises before merging.
- **Forking an Action:** if you need to vendor an Action that doesn't
  publish to GitHub Releases, pin to the SHA of the last reviewed
  commit on your fork.
