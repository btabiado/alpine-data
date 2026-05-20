# LTHCS — Content-Security-Policy + Subresource-Integrity rollout

**Date:** 2026-05-20
**Ticket:** P2 #9 — security hardening for dashboard pages
**Author:** swarm-csp-sri worktree

## Why

The dashboard is served by GitHub Pages as static HTML/JS/CSS. GitHub Pages
does **not** allow setting arbitrary HTTP response headers — so we can't ship
HSTS, X-Frame-Options, X-Content-Type-Options, or CSP as server headers. The
next-best option is `<meta http-equiv>` tags inside every page's `<head>`.

Concrete threats this defends against:

| Threat                                 | Defense                                   |
| -------------------------------------- | ----------------------------------------- |
| Injected `<script>` from a stored XSS  | `script-src 'self'`                       |
| Click-jacking via `<iframe>`           | `frame-ancestors 'none'`                  |
| Exfiltrating fetched data to attacker  | `connect-src 'self'`                      |
| Form-submission hijack                 | `form-action 'self'`                      |
| `<base href>` rewrite trick            | `base-uri 'self'`                         |
| MIME-sniffing tricks                   | `X-Content-Type-Options: nosniff`         |
| Referer leakage on outbound links      | `Referrer-Policy: strict-origin-when-cross-origin` |

## The policy

Every LTHCS page now carries:

```html
<meta http-equiv="Content-Security-Policy" content="
  default-src 'self';
  script-src 'self';
  style-src 'self' 'unsafe-inline';
  img-src 'self' data:;
  font-src 'self' data:;
  connect-src 'self';
  frame-ancestors 'none';
  base-uri 'self';
  form-action 'self'
" />
<meta http-equiv="X-Content-Type-Options" content="nosniff" />
<meta name="referrer" content="strict-origin-when-cross-origin" />
```

### Directive choices

- **`script-src 'self'`** — strict. No `'unsafe-inline'`, no `'unsafe-eval'`,
  no CDNs. The three pages that previously had inline `<script>` blocks
  (`lthcs_tab/index.html`, `lthcs_health/quality.html`, `lthcs_help/index.html`)
  were refactored to load external files (`lthcs-footer-about-link.js`,
  `lthcs-quality.js`, `lthcs-help-back-top.js`).
- **`style-src 'self' 'unsafe-inline'`** — `'unsafe-inline'` retained because
  many elements use `style="text-decoration:none"` and SVG charts emit inline
  styles. Dropping it would require a sweeping refactor that's out of scope.
- **`img-src 'self' data:`** — allows tiny inline `data:` icons (currently
  unused, but reserved for future use).
- **`font-src 'self' data:`** — same rationale; no external font CDN.
- **`connect-src 'self'`** — restricts `fetch()` to same origin. All JSON
  fetches go to `../data/lthcs/...`, which is same-origin in both dev and
  prod.
- **`frame-ancestors 'none'`** — fully replaces the legacy
  `X-Frame-Options: DENY`. Modern browsers honor it.
- **`form-action 'self'`** + **`base-uri 'self'`** — closes two common XSS
  exfiltration vectors.

## Why SRI was skipped

Subresource Integrity (`integrity="sha384-..."`) protects against tampered
**third-party** scripts/stylesheets. Today every `<script src>` and
`<link href>` in the dashboard is a **relative same-origin path** — there
are no CDN references. SRI on same-origin resources is redundant; if the
attacker can swap our same-origin file they can also swap the page that
declares the integrity hash.

If a future change introduces an external CDN reference, add SRI then —
[MDN: SRI](https://developer.mozilla.org/en-US/docs/Web/Security/Subresource_Integrity)
has the recipe (`openssl dgst -sha384 -binary file | openssl base64 -A`).

## Files touched

- 15 dashboard pages got the three security meta tags:
  - `lthcs_tab/index.html`
  - `lthcs_tab/heatmap/index.html`
  - `lthcs_health/index.html`, `quality.html`, `pipeline.html`
  - `lthcs_backtest/index.html`, `ab.html`
  - `lthcs_crypto/index.html`
  - `lthcs_position/index.html`
  - `lthcs_public/index.html`
  - `lthcs_diff/index.html`
  - `lthcs_history/index.html`
  - `lthcs_leaderboards/index.html`
  - `lthcs_help/index.html`
  - `lthcs_table/index.html`
- 3 inline `<script>` blocks refactored to external files:
  - `lthcs_tab/lthcs-footer-about-link.js` (new)
  - `lthcs_health/lthcs-quality.js` (new)
  - `lthcs_help/lthcs-help-back-top.js` (new)
- `lthcs_tab/mockups/...` were intentionally left alone — they're
  design-exploration artifacts, not production routes.

## How to test for CSP violations

1. Open any LTHCS page in Chrome or Safari.
2. Open DevTools → **Console**.
3. CSP violations log as red errors like:
   `Refused to execute inline script because it violates the following
   Content Security Policy directive: "script-src 'self'".`
4. The page still renders, but the offending script/style is silently blocked.
5. Click around: hit Refresh, open the About modal, open the detail modal for
   a ticker, navigate to /health/quality, scroll the /help page to verify the
   back-to-top button appears past 600px. If any of those break, a CSP
   regression is the likely cause.

You can also enable Chrome's
[Issues panel](chrome://flags/#enable-experimental-web-platform-features) for
a structured CSP-violation report.

## How to relax the policy if a future change needs it

Prefer **refactoring the change** over relaxing the policy. The current
strictness on `script-src` blocks the largest XSS attack surface and we should
guard it.

If you absolutely need to add an external CDN script:

1. Add the host to `script-src`, **not** `'unsafe-inline'`. e.g.
   `script-src 'self' https://cdn.jsdelivr.net`.
2. Add an `integrity="sha384-..."` attribute to the `<script>` tag.
3. Add `crossorigin="anonymous"` so SRI works.
4. Update this doc with the rationale.

If you need inline event handlers (`onclick="foo()"`), DON'T add
`'unsafe-inline'`. Use `element.addEventListener('click', foo)` in an
external module instead.

## Future improvements

- Consider adding `Permissions-Policy` once GitHub Pages supports it (or
  switch to a Cloudflare-fronted setup with full header control).
- Look into a CSP **report-uri** endpoint so we get told about violations in
  the wild instead of only catching them in DevTools.
- Move to `style-src 'self'` (drop `'unsafe-inline'`) after a sweep that
  replaces `style="..."` attributes with utility CSS classes.
