# tools/

Pure-stdlib build-side utilities that run in CI before deploy.

## `validate_v2_dashboard.py`

Structural lint for `v2/dashboard.html` — specifically the giant inline
`<script>` block rendered out of `v2/app.py`'s `HTML_TEMPLATE`. Catches the
class of bug that bricked V2 in production on 2026-05-24, when a "take-both"
git conflict resolution silently shipped (a) three duplicate
`const SIDECAR_FOR_TAB` declarations and (b) an `if (state.tab === ...)`
block nested inside a `V2.empty({...})` object literal — both JS
`SyntaxError`s that kill all client-side interactivity but leave the static
markup intact, so the page looks loaded while every tab is dead. The
validator runs after `python v2/app.py --no-open` in `pages.yml` and **fails
the build** on regression, so a broken V2 cannot reach `/v2/`.

Current checks: duplicate top-level `const NAME = ...` declarations,
`if (state.tab` literals inside `V2.empty({...})`, git conflict markers
(`<<<<<<<` / `=======` / `>>>>>>>`), un-substituted `__DATA_JSON__`-style
template placeholders, gross brace/paren imbalance, and duplicate keys in
the `const state = { ... }` object. To add a new check, write a
`check_…(js: str) -> bool` function that logs one PASS/FAIL line via `_log`
and append it to the `results` list in `validate()` — exit code 1 fires if
any check returns `False`. Keep checks regex-shaped and bounded; this script
is intentionally not a JS parser.
