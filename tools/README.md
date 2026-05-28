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
template placeholders, gross brace/paren imbalance, duplicate keys in
the `const state = { ... }` object, and SIDECARS↔SIDECAR_FOR_TAB coverage
(every tab-sidecar pointer must have a corresponding manifest entry — the
class of bug that bricked V1 on 2026-05-27 when new tabs were ported
without their manifest keys). To add a new check, write a
`check_…(js: str) -> bool` function that logs one PASS/FAIL line via `_log`
and append it to the `results` list in `validate()` — exit code 1 fires if
any check returns `False`. Keep checks regex-shaped and bounded; this script
is intentionally not a JS parser.

## `validate_dashboard.py`

Thin wrapper around `validate_v2_dashboard.py` that exposes the same suite
under a path-agnostic name. Use this entrypoint for V1:

```
python tools/validate_dashboard.py dashboard.html       # V1
python tools/validate_dashboard.py v2/dashboard.html    # V2 (same checks)
python tools/validate_dashboard.py                      # defaults to v2/dashboard.html
```

`pages.yml` calls it after both the V1 and the V2 generate steps. Renaming
the underlying module was avoided to keep the diff surgical while a parallel
agent is also editing `validate_v2_dashboard.py`.
