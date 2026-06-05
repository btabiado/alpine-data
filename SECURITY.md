# Security Policy

## Reporting a vulnerability

If you discover a security issue, please report it privately via GitHub
Security Advisory at https://github.com/btabiado/alpine-data/security/advisories/new
or email btabiado@gmail.com (replace with the actual repo owner email if different).

Please include:
- A description of the issue and its potential impact
- Steps to reproduce
- Any proof-of-concept code or screenshots
- Your assessment of severity (Critical / High / Medium / Low)

We aim to acknowledge within 48 hours and triage within 7 days.

## Supported versions

The dashboard is a single-user public read-only mirror. Only `main` is
supported. There is no LTS, no backports, no point releases.

## What is and isn't in scope

**In scope**:
- Code execution via dependencies (supply-chain)
- API key leaks in workflow logs, commits, or page output
- Cross-site scripting in the rendered HTML
- Path-traversal or injection in any committed scripts

**Out of scope**:
- DoS / rate limit abuse on the public Pages mirror
- Issues in upstream APIs (Yahoo, Finnhub, Alpha Vantage, CoinGecko, etc.)
- Issues with the GitHub Actions runtime
- Speculative supply-chain risk without an exploit
- Vulnerabilities in user-side browsers / extensions

## Responsible disclosure

We won't pursue legal action against good-faith researchers. Please give
us a reasonable window (typically 30 days) before public disclosure.
