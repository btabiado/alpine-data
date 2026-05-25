#!/usr/bin/env python3
"""Structural validator for v2/dashboard.html inline JS.

Background
----------
v2/app.py renders a single 1.5MB+ HTML file with a giant inline <script>
block (~11k lines of dashboard JS). If that JS fails to parse, the page
still loads (static markup + tab bar) but every interactive feature is
dead. We've shipped that exact regression to production before — three
duplicate `const SIDECAR_FOR_TAB` lines and an `if (state.tab === ...)`
block accidentally nested inside a `V2.empty({...})` call literal,
both surviving a "take-both" git conflict resolution.

Nothing in CI parsed the rendered JS, so the deploy went green and the
breakage was only spotted live. This validator closes that hole.

Design
------
Pure stdlib, no JS parser. Targeted regex checks against the largest
<script> block in the freshly-built v2/dashboard.html:

  1. Top-level `const NAME = ...` declarations are unique
     (catches SIDECAR_FOR_TAB-style duplicates).
  2. No `V2.empty({...})` call contains a literal `if (state.tab` inside
     its object argument (catches the take-both conflict pattern).
  3. No git conflict markers (<<<<<<<, =======, >>>>>>>) anywhere
     in the JS.
  4. No `__DATA_JSON__` placeholder left over (would mean the template
     substitution silently failed).
  5. Brace and paren balance — informational diff, only fails on
     wildly-off (>50) deltas to avoid false positives from strings.
  6. The `const state = { ... }` object literal has unique top-level
     keys.

Each check prints a one-line PASS/FAIL so CI logs are actually useful.
Exit code 0 on full pass, 1 on any failure.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Allow override for the synthetic-regression test or callers that want to
# point at a non-default path. Default matches the location v2/app.py writes.
DEFAULT_PATH = Path(__file__).resolve().parent.parent / "v2" / "dashboard.html"


def _log(ok: bool, name: str, detail: str = "") -> None:
    """One line per check so CI logs are scan-friendly."""
    tag = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"[{tag}] {name}{suffix}")


def extract_largest_script(html: str) -> tuple[str, int]:
    """Return (script_body, start_offset_in_html) for the longest inline JS.

    The page has two <script> tags: the Chart.js CDN include (src=...) and
    the inline app JS. We deliberately pick by body length so a future
    extra <script> doesn't trick us into validating the wrong one.
    """
    # Non-greedy match of any <script ...>...</script>. Captures the inner
    # body so we can rank by length.
    pat = re.compile(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.DOTALL)
    best_body = ""
    best_start = -1
    for m in pat.finditer(html):
        body = m.group("body")
        if len(body) > len(best_body):
            best_body = body
            # Start offset of the body itself, useful if a future check
            # wants to report line numbers in the original file.
            best_start = m.start("body")
    return best_body, best_start


def check_no_duplicate_top_level_consts(js: str) -> bool:
    """Line-anchored `const NAME = ...` declarations must be unique.

    Rough on purpose — we only flag declarations that start at column 0,
    which is how every top-level const in this file is formatted. Indented
    consts inside functions/blocks are correctly ignored (they live in
    their own scope and re-declarations there are not the bug class we
    care about).
    """
    pat = re.compile(r"^const\s+([A-Za-z_$][\w$]*)\s*=", re.MULTILINE)
    seen: dict[str, int] = {}
    for m in pat.finditer(js):
        name = m.group(1)
        seen[name] = seen.get(name, 0) + 1
    dupes = {n: c for n, c in seen.items() if c > 1}
    if dupes:
        details = ", ".join(f"{n} x{c}" for n, c in sorted(dupes.items()))
        _log(False, "No duplicate top-level `const`", details)
        return False
    _log(True, "No duplicate top-level `const`", f"{len(seen)} unique names")
    return True


def check_no_state_tab_in_v2_empty(js: str) -> bool:
    """`V2.empty({ ... if (state.tab === ... ) ... })` is the take-both bug.

    The legitimate pattern is `V2.empty({ icon, title, sub, warm })` — a
    plain object literal. A take-both conflict from rebasing two tabs
    landed an `if` statement *inside* the object body, which is a syntax
    error that bricks the entire script.
    """
    # Match V2.empty( … ) non-greedily; scan the inner text for
    # `if (state.tab`. We use a small lookahead window (4KB) to keep the
    # regex bounded — every legitimate V2.empty call is a one-liner or
    # short multi-line object.
    pat = re.compile(r"V2\.empty\s*\((.{0,4096}?)\)", re.DOTALL)
    offenders = []
    for m in pat.finditer(js):
        inner = m.group(1)
        if re.search(r"\bif\s*\(\s*state\.tab\b", inner):
            offenders.append(m.start())
    if offenders:
        _log(
            False,
            "No `if (state.tab` inside V2.empty({...})",
            f"{len(offenders)} match(es) at script offsets {offenders[:3]}",
        )
        return False
    _log(True, "No `if (state.tab` inside V2.empty({...})")
    return True


def check_no_conflict_markers(js: str) -> bool:
    """git rerere never sleeps. Conflict markers in shipped JS = SyntaxError."""
    markers = ["<<<<<<<", "=======", ">>>>>>>"]
    found: list[str] = []
    for mark in markers:
        # Anchor `=======` and `>>>>>>>` to a line start to avoid matching
        # `// =======================================` decorative banners
        # that ride along inside JS comments. `<<<<<<<` is rare enough in
        # legit code to flag wherever it appears.
        if mark == "<<<<<<<":
            if mark in js:
                found.append(mark)
        else:
            if re.search(rf"^{re.escape(mark)}", js, re.MULTILINE):
                found.append(mark)
    if found:
        _log(False, "No git conflict markers", f"found {found}")
        return False
    _log(True, "No git conflict markers")
    return True


def check_no_placeholder_remnant(js: str) -> bool:
    """`__DATA_JSON__` (or any `__FOO__` template token) must be substituted."""
    # The historical placeholder was __DATA_JSON__; widen the net to any
    # leading-and-trailing double-underscore identifier to also catch
    # future template tokens that someone forgot to replace.
    pat = re.compile(r"__[A-Z][A-Z0-9_]{2,}__")
    hits = sorted(set(pat.findall(js)))
    # Be conservative — also flag the specific historical token even if
    # the general regex evolves.
    if "__DATA_JSON__" in hits or any(h == "__DATA_JSON__" for h in hits):
        _log(False, "No `__DATA_JSON__` placeholder", "literal token still present")
        return False
    # If other __FOO__ tokens exist they may be legitimate (e.g. a magic
    # string inside a string literal), so just note them informationally.
    if hits:
        print(f"[info ] Template-like tokens present (not failing): {hits[:5]}")
    _log(True, "No `__DATA_JSON__` placeholder")
    return True


def check_brace_paren_balance(js: str) -> tuple[bool, dict]:
    """Informational. Strings and regex literals will skew the count, so we
    only HARD FAIL if the imbalance is wildly off (>50). The point is to
    surface ballpark sanity, not pretend to be a tokenizer."""
    counts = {
        "{": js.count("{"),
        "}": js.count("}"),
        "(": js.count("("),
        ")": js.count(")"),
        "[": js.count("["),
        "]": js.count("]"),
    }
    deltas = {
        "{}": counts["{"] - counts["}"],
        "()": counts["("] - counts[")"],
        "[]": counts["["] - counts["]"],
    }
    wild = {k: v for k, v in deltas.items() if abs(v) > 50}
    if wild:
        _log(False, "Brace/paren balance roughly sane", f"deltas {deltas}")
        return False, deltas
    _log(True, "Brace/paren balance roughly sane", f"deltas {deltas}")
    return True, deltas


def check_state_keys_unique(js: str) -> bool:
    """`const state = { ... }` — top-level keys must not collide.

    We slice from the start of `const state = {` to the *matching* closing
    brace using a depth counter (cheap and reliable for object literals
    that don't contain inline regex). Then split on top-level commas to
    pull out `key:` pairs.
    """
    m = re.search(r"^const\s+state\s*=\s*\{", js, re.MULTILINE)
    if not m:
        _log(False, "state object found", "no `const state = {` declaration")
        return False
    start = m.end() - 1  # position of the opening `{`
    depth = 0
    end = -1
    in_str: str | None = None
    in_line_comment = False
    in_block_comment = False
    i = start
    while i < len(js):
        ch = js[i]
        nxt = js[i + 1] if i + 1 < len(js) else ""
        # Comment handling first so strings inside comments don't toggle in_str.
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
        elif in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
        elif in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == in_str:
                in_str = None
        else:
            if ch == "/" and nxt == "/":
                in_line_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_block_comment = True
                i += 2
                continue
            if ch in ("'", '"', "`"):
                in_str = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        i += 1
    if end < 0:
        _log(False, "state object balanced", "could not find matching closing brace")
        return False
    body = js[start + 1 : end]

    # Walk the body splitting on top-level commas (depth-aware, comment-aware)
    # to extract the `key:` of each pair.
    keys: list[str] = []
    parts: list[str] = []
    depth = 0
    in_str = None
    in_line_comment = False
    in_block_comment = False
    buf: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            buf.append(ch)
            i += 1
            continue
        if in_block_comment:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                in_block_comment = False
                buf.append(nxt)
                i += 2
                continue
            i += 1
            continue
        if in_str:
            buf.append(ch)
            if ch == "\\":
                if i + 1 < len(body):
                    buf.append(body[i + 1])
                    i += 2
                    continue
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch == "/" and nxt == "/":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            buf.append(ch)
            i += 1
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        parts.append("".join(buf))

    # For each part, strip leading comments/whitespace and grab the
    # `identifier:` at the start. Computed keys (`[expr]:`) and string
    # keys (`"x":`) are uncommon in this codebase but we skip them
    # safely rather than misreport.
    key_pat = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*:")
    for part in parts:
        # Drop /* ... */ and // ... line comments at the head so the
        # key regex can latch onto the actual identifier.
        cleaned = re.sub(r"/\*.*?\*/", "", part, flags=re.DOTALL)
        cleaned = re.sub(r"^\s*(//[^\n]*\n)+", "", cleaned)
        km = key_pat.match(cleaned)
        if km:
            keys.append(km.group(1))

    seen: dict[str, int] = {}
    for k in keys:
        seen[k] = seen.get(k, 0) + 1
    dupes = {n: c for n, c in seen.items() if c > 1}
    if dupes:
        details = ", ".join(f"{n} x{c}" for n, c in sorted(dupes.items()))
        _log(False, "state object has unique keys", details)
        return False
    _log(True, "state object has unique keys", f"{len(keys)} keys")
    return True


def validate(path: Path) -> int:
    if not path.exists():
        print(f"[FAIL] dashboard file missing: {path}")
        return 1
    print(f"[info ] Validating {path} ({path.stat().st_size:,} bytes)")
    html = path.read_text(encoding="utf-8")
    js, _start = extract_largest_script(html)
    if not js.strip():
        print("[FAIL] no inline <script> body found")
        return 1
    print(f"[info ] Largest inline script body: {len(js):,} chars, {js.count(chr(10)):,} lines")

    results = [
        check_no_duplicate_top_level_consts(js),
        check_no_state_tab_in_v2_empty(js),
        check_no_conflict_markers(js),
        check_no_placeholder_remnant(js),
        check_brace_paren_balance(js)[0],
        check_state_keys_unique(js),
    ]
    if all(results):
        print("[ok   ] All v2/dashboard.html JS structural checks passed.")
        return 0
    failed = sum(1 for r in results if not r)
    print(f"[ERROR] {failed} check(s) failed — see above. Refusing to deploy.")
    return 1


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else DEFAULT_PATH
    return validate(path)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
