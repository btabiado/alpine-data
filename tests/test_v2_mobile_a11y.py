"""Contract tests for the V2 phone-and-keyboard fixes from the site audit.

The user reads this dashboard PRIMARILY ON A PHONE. Every defect below was
measured in headless Chromium at a 360x740 viewport with a coarse pointer,
and every one of them is the kind that silently regresses the next time
somebody tunes a font size or a padding. So they are pinned here.

Two layers, mirroring tests/test_v2_freshness.py:

1. **The shipped CSS/HTML** — asserted against the inline template string in
   ``v2/app.py``, which is what actually reaches the browser.
2. **The shipped JavaScript** — ``compositeHistoryChart`` is extracted
   verbatim and executed in V8, so the axis assertions run against the code
   that ships, not a paraphrase of it.

Covered:
  S1/B8 iOS Safari zooms the page on every text-entry tap. V2 raised exactly
        two controls; EIGHT others were still under 16px. Now a blanket rule,
        byte-identical to V1's.
  B7    Chart.js was a parser-blocking third-party tag in <head>, so a stalled
        CDN held domInteractive/DCL/FCP for as long as it liked (13,239ms
        measured at a 13,000ms stall). Now an injected async script behind a
        whenChartsReady() gate that resolves on load, error, or a 1.2s budget.
  S2/B9 the whole honesty story lived in title=, which touch cannot reach —
        and then V1 and V2 each built a DIFFERENT control for that one
        requirement. V2 now runs V1's, asserted byte-for-byte, plus the shared
        defect both had: the note outlived the chip that owned it.
  S3    the history-chart entry point was a 22px tap target
  V2-B  empty states promised a refresh that may never come
  V2-C  travel sub-nav scrolled away; its buttons were 28px
  V2-D  the composite history y-axis had no floor, so 51->53 filled the frame
  V2-E  the modal never trapped focus; two inputs had no focus ring

Wherever a defect is shared between the two frontends, the assertion here is
BYTE-IDENTITY against app.py rather than a paraphrase of the behaviour. A
paraphrase is how the two drifted apart in the first place: both files passed
their own tests while doing different things.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
V2_APP = ROOT / "v2" / "app.py"


@pytest.fixture(scope="module")
def v2_js() -> str:
    if not V2_APP.exists():  # pragma: no cover - repo layout guard
        pytest.skip("v2/app.py not present")
    src = V2_APP.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    i = src.index(marker)
    return src[i + len(marker):]


def extract_function(js: str, name: str) -> str:
    """Full source of a ``function name(...){...}``, brace-matched.

    Indent-tolerant: several of these live inside the ``V2`` module IIFE
    rather than at the top level of the script.
    """
    m = re.search(r"^[ \t]*function %s\s*\(" % re.escape(name), js, re.M)
    assert m, f"function {name}() not found in the V2 template"
    start = m.start()
    i = js.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[start:j + 1]
    raise AssertionError(f"unbalanced braces extracting {name}()")  # pragma: no cover


def strip_comment_lines(js: str) -> str:
    return "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))


def touch_block(js: str) -> str:
    """The trailing `@media (pointer:coarse),(max-width:860px)` block."""
    m = re.search(r"@media \(pointer:coarse\),\(max-width:860px\)\{", js)
    assert m, "the coarse-pointer touch block is gone"
    i = m.end() - 1
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    raise AssertionError("unbalanced braces in the touch block")  # pragma: no cover


# ------------------------------------------- S1: no zoom on a search tap ---
#
# Mobile Safari zooms the viewport whenever a focused form control's font is
# under 16px and leaves the reader pinched in and scrolled sideways. V2 used
# to raise exactly TWO controls and left the rest, so the fix held on the two
# fields it had been demonstrated against and nowhere else.
#
# MEASURED on the built page in headless Chromium at 360x740 with a coarse
# pointer, computed font-size per control — EIGHT still zoomed:
#   #shareHost 11px, #shareNewUrl 11px, #shareLabel 12px, #pasteText 12px,
#   #travelSearch 12px, #travelSort 12px, #shareDays 13.33px,
#   #pasteAsset 13.33px.   After: 10 of 10 controls at 16px, and desktop
#   (1280x800, fine pointer) unchanged at 11-13.33px.


BLANKET_RULE = ('@media (pointer:coarse),(max-width:640px){\n  input[type="text"]',
                'input:not([type]),textarea,select{font-size:16px !important}\n}')


def test_every_text_entry_control_reaches_16px_on_touch(v2_js):
    """A list of two ids is not a fix, it is two fixes. This has to be a rule
    about text-entry controls, not about the two the audit happened to name."""
    block = shared_block(v2_js, *BLANKET_RULE)
    for t in ("text", "search", "email", "url", "number", "tel", "password", "date"):
        assert 'input[type="%s"]' % t in block, t
    assert "input:not([type])" in block
    assert "textarea" in block and "select" in block
    assert "font-size:16px !important" in block, \
        "several of these carry an inline `font:12px …` shorthand; !important is required"


def test_checkboxes_and_radios_are_left_alone(v2_js):
    """They have no text and no zoom trigger; resizing them is a layout change
    dressed up as an accessibility fix."""
    block = shared_block(v2_js, *BLANKET_RULE)
    assert "checkbox" not in block and "radio" not in block


def test_the_blanket_rule_is_the_same_rule_v1_ships(v2_js, v1_js):
    """Same defect, same rule, both frontends — byte for byte."""
    assert shared_block(v2_js, *BLANKET_RULE) == shared_block(v1_js, *BLANKET_RULE)


def test_the_two_named_inputs_are_still_covered(v2_js):
    """The old two-id rules stay: they also set width/min-height, and losing
    them would be a regression dressed up as a simplification."""
    block = touch_block(v2_js)
    assert re.search(r"header #symbolSearchInput\{[^}]*font-size:16px !important", block)
    assert re.search(r"\.chat-form input#chatInput\{[^}]*font-size:16px !important", block)


def test_pinch_zoom_is_never_disabled(v2_js):
    """The tempting non-fix. Killing pinch-zoom "solves" the symptom by
    removing an accessibility affordance from everyone — and modern iOS
    ignores it anyway, so it does not even work."""
    meta = re.search(r'<meta name="viewport" content="([^"]*)"', v2_js)
    assert meta, "viewport meta missing"
    content = meta.group(1)
    assert "maximum-scale" not in content
    assert "user-scalable" not in content
    assert content == "width=device-width, initial-scale=1"


# ------------------------------------------------- B7: the critical path ---
#
# MEASURED on the built V2 page, headless Chromium at 360x740, with the CDN
# request held and then answered:
#            CDN held    domInteractive     DCL         FCP
#   before   13,000ms       13,239ms     13,239ms    13,172ms
#   before    6,000ms        6,224ms      6,224ms     6,156ms
#   before    refused          243ms        244ms       172ms
#   after    13,000ms          193ms        193ms       132ms
#   after     6,000ms          207ms        208ms       152ms
#   after     refused          249ms        250ms       192ms
# The document was ready in ~240ms and then sat waiting on somebody else's
# server — including, on V2, the FIRST PAINT. These are cheap static guards
# so the structural property those timings depend on cannot silently regress.

THIRD_PARTY_ORIGINS = ("fonts.googleapis.com", "fonts.gstatic.com",
                       "cdn.jsdelivr.net", "unpkg.com")


@pytest.fixture(scope="module")
def v2_markup(v2_js: str) -> str:
    """Template with HTML comments removed — commented-out tags are not tags."""
    return re.sub(r"<!--.*?-->", "", v2_js, flags=re.S)


def test_no_parser_blocking_third_party_script(v2_markup):
    """A classic `script src` with neither async nor defer blocks the parser
    AND holds DOMContentLoaded. `defer` is not a fix either: deferred scripts
    still run before DCL fires, so it would have kept all 13.2 seconds."""
    offenders = []
    for m in re.finditer(r"<script\b([^>]*)\bsrc=[\"']([^\"']+)[\"']([^>]*)>",
                         v2_markup, re.I):
        attrs = (m.group(1) + " " + m.group(3)).lower()
        if "//" not in m.group(2):
            continue
        if "async" not in attrs:
            offenders.append(m.group(2))
    assert offenders == [], (
        "third-party script tags that gate DOMContentLoaded: " + repr(offenders))


def test_no_third_party_stylesheet(v2_markup):
    """A stylesheet blocks rendering wherever it appears — head OR body."""
    sheets = [m.group(1) for m in re.finditer(
        r"<link\b[^>]*\brel=[\"']?stylesheet[^>]*\bhref=[\"']([^\"']+)", v2_markup, re.I)]
    assert [x for x in sheets if "//" in x] == []


@pytest.mark.parametrize("origin", THIRD_PARTY_ORIGINS)
def test_no_third_party_origin_is_reached_from_static_markup(v2_markup, origin):
    """Every remaining third-party fetch must be made by JS at a moment of its
    choosing, never by a tag the parser trips over. preconnect/dns-prefetch
    hints are allowed — they open a socket and gate nothing."""
    for m in re.finditer(r"<(script|link)\b([^>]*)>", v2_markup, re.I):
        if origin not in m.group(2):
            continue
        assert re.search(r"rel=[\"']?(preconnect|dns-prefetch)", m.group(2), re.I), (
            f"{origin} is referenced by a parser-visible tag: {m.group(0)[:160]}")


def test_chart_js_is_loaded_async_and_gated(v2_js):
    """Chart.js still ships (SRI pin intact) but via an injected async script,
    and the first render waits on whenChartsReady() rather than on the CDN."""
    assert "s.async = true;" in v2_js
    assert "sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g" in v2_js
    assert "window.whenChartsReady = function(cb)" in v2_js
    assert "whenChartsReady(function(){" in v2_js


def test_the_gate_resolves_on_a_budget_not_only_on_the_network(v2_js):
    """load OR error OR timeout, whichever is first. A gate that only resolves
    on the network is the parser-blocking tag with extra steps."""
    assert "var BUDGET_MS = 1200;" in v2_js
    assert "s.onerror = function(){ libDone = true; flush(); };" in v2_js
    assert "setTimeout(function(){ expired = true; flush(); }, BUDGET_MS);" in v2_js


def test_chart_fallback_is_installed_by_the_gate_not_at_parse_time(v2_js):
    """With an async loader, "Chart is undefined right now" means "still in
    flight", not "failed". Installing the no-op stub at parse time would paint
    'Chart library unavailable' over charts that were about to work."""
    assert "window.__installChartFallback = function()" in v2_js
    assert "window.__installChartFallback();" in v2_js
    # …and it still must beat the first real `new Chart(` in source order.
    code = re.sub(r"^\s*//.*$", "", v2_js, flags=re.M)
    stub = code.index("function ChartUnavailable(canvas)")
    first = re.search(r"new Chart\(", code)
    assert first and stub < first.start()


def test_the_stub_is_byte_identical_to_v1s(v2_js, v1_js):
    STUB = ("window.__installChartFallback = function(){",
            "window.Chart.__unavailable = true;\n}\n};")
    assert shared_block(v2_js, *STUB) == shared_block(v1_js, *STUB)


def test_a_late_arrival_is_still_adopted(v2_js):
    """A CDN that answers after the budget must repaint, not leave the reader
    with empty chart frames until they happen to touch a tab."""
    assert "window.__chartCdn.state = 'loaded-late';" in v2_js


def test_the_late_arrival_guard_reads_a_binding_that_actually_resolves(v2_js):
    """THE bug not to copy. `state` is declared `const state = {…}` at the top
    level of a classic script, which puts it in the global LEXICAL environment
    — never on the global OBJECT. `window.state` is therefore undefined
    forever, and a recovery guarded on it is dead code. VERIFIED in a browser:
    with the CDN answering at 3,000ms (past the 1,200ms budget) the built page
    reports __chartCdn.state === 'loaded-late', window.Chart real, and the
    active tab's canvas repainted."""
    loader = shared_block(v2_js, "<script>\n(function(){\n  var CDN = ", "})();\n</script>")
    code = strip_comment_lines(loader)     # the comment explains the trap by name
    assert "window.state" not in code
    assert "typeof state === 'object'" in code
    assert "state && state.tab) selectTab(state.tab);" in loader


def test_the_late_arrival_takes_down_the_stubs_notes(v2_js):
    """MEASURED: with a 3,000ms arrival the page repainted a real chart and
    left the stub's sentence "Chart library unavailable" visible underneath
    it on the active tab. A caption that lies and a stamp that lies are the
    same defect. (V1 needs this too — latent there only because V1's default
    tab instantiates no Chart.)"""
    assert "window.__clearChartFallbackNotes = function()" in v2_js
    loader = shared_block(v2_js, "<script>\n(function(){\n  var CDN = ", "})();\n</script>")
    assert "window.__clearChartFallbackNotes();" in loader
    fn = v2_js[v2_js.index("window.__clearChartFallbackNotes = function()"):]
    fn = fn[:fn.index("\n};")]
    # The guard flag must be cleared too, or a genuine LATER failure could not
    # re-announce itself: the wrap would still be marked as already-failed.
    assert "data-chart-failed" in fn
    # Targets the note by CLASS, not by matching its caption text.
    #
    # This assertion previously required the literal "Chart library
    # unavailable" inside the clear function, which pinned a substring match
    # against user-facing copy: editing that caption — a normal, harmless
    # thing to do — would have silently orphaned every note while this test
    # stayed green. The stub now tags each note with .chart-unavailable-note
    # and the clear function selects on that, so the two cannot drift apart.
    assert "chart-unavailable-note" in fn
    assert "Chart library unavailable" in v2_js, "the caption itself must still exist"
    # ...and the stub must actually apply the class the clear function seeks.
    stub = v2_js[v2_js.index("window.Chart = function ChartUnavailable"):]
    assert "chart-unavailable-note" in stub[:stub.index("__unavailable = true")]


def test_the_first_render_is_the_only_one_that_waits(v2_js):
    """Later renders must not re-pay the gate: by then the library has long
    since resolved, and re-gating them would put the CDN back on the path of
    every tab click."""
    tail = v2_js[v2_js.index("function _tabFromHash()"):]
    assert "whenChartsReady(function(){\n  selectTab(_tabFromHash() || 'overview');\n  renderAll();\n});" in tail
    hashchange = tail[tail.index("window.addEventListener('hashchange'"):]
    assert "whenChartsReady" not in hashchange


def test_connection_hints_only_where_a_request_remains(v2_markup):
    """Chart.js is the one third-party request V2 ever makes — no webfont, and
    unlike V1 no Leaflet, because V2 has no map. Hinting an origin that is
    never contacted is just noise."""
    hints = re.findall(
        r"<link\b[^>]*rel=[\"']?(?:preconnect|dns-prefetch)[^>]*href=[\"']([^\"']+)",
        v2_markup, re.I)
    assert hints == ["https://cdn.jsdelivr.net"], hints


def test_v2_really_has_no_map_to_lazy_load(v2_js):
    """V1 additionally defers Leaflet behind ensureLeaflet() because one V1
    sub-view draws a map. V2 has no map at all, so there is nothing to defer —
    asserted rather than assumed, so a future map tab has to make a decision."""
    assert "leaflet" not in v2_js.lower()


# --------------------------- S2: the explanation must survive a touchscreen -
#
# ONE REQUIREMENT, ONE BEHAVIOUR. The previous round let V1 and V2 each build
# their own disclosure for this single spec: V1 keyed off `.v2-fresh[title]`,
# marked the chip with an `::after " ⓘ"`, and opened a fixed overlay dismissed
# by a "Got it" button; V2 keyed off a derived `data-fresh-explain`, marked it
# with a `::before`, and spliced an inline panel in next to the chip with an
# "×". Same requirement, two controls, so a reader moving between the two
# frontends had to learn the affordance twice. V1 is what pages.yml deploys,
# so V2 was converged onto V1's implementation — and the assertions below are
# byte-identity against app.py rather than a paraphrase, because a paraphrase
# is exactly how the two drifted apart in the first place.


def shared_block(js: str, start: str, end: str) -> str:
    i = js.index(start)
    j = js.index(end, i) + len(end)
    return js[i:j]


@pytest.fixture(scope="module")
def v1_js() -> str:
    v1 = ROOT / "app.py"
    if not v1.exists():  # pragma: no cover - repo layout guard
        pytest.skip("app.py not present")
    src = v1.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    return src[src.index(marker) + len(marker):]


FRESHNOTE_JS = ("// S2 — THE HONESTY STORY MUST BE REACHABLE WITHOUT A MOUSE", "\n})();")
FRESHNOTE_CSS = ("/* ---- S2: a freshness chip that CARRIES an explanation",
                 ".v2-freshnote__x:focus-visible{outline:2px solid var(--purple);outline-offset:2px}")


def test_the_disclosure_is_the_same_code_in_both_frontends(v2_js, v1_js):
    """The gate blocker in one assertion. Not "V2 also has a popover" — the
    SAME popover, byte for byte, so no future edit can fix one and leave the
    other. If this fails, decide which behaviour is right and change BOTH."""
    assert shared_block(v2_js, *FRESHNOTE_JS) == shared_block(v1_js, *FRESHNOTE_JS)


def test_the_disclosure_styling_is_the_same_in_both_frontends(v2_js, v1_js):
    assert shared_block(v2_js, *FRESHNOTE_CSS) == shared_block(v1_js, *FRESHNOTE_CSS)


def test_v2_no_longer_carries_its_own_dialect(v2_js):
    """The old V2-only implementation, named so it cannot creep back."""
    for gone in ("data-fresh-explain", "v2-fresh-pop", "upgradeFreshnessChips",
                 "toggleFreshnessExplain", "closeFreshnessExplain", "freshPopAnchor",
                 "wireFreshnessExplain", "freshExplainText", "FRESH_EXPLAIN_SUFFIX"):
        assert gone not in v2_js, gone


def test_freshness_chips_are_activatable_without_a_mouse(v2_js):
    """27 title attributes in V2, and `title` does not exist on a phone. The
    chip has to be reachable by tap AND by keyboard."""
    assert "(function freshnessNotes(){" in v2_js
    assert "function enhanceFreshnessChips()" in v2_js
    assert "el.setAttribute('role', 'button');" in v2_js
    assert "el.setAttribute('tabindex', '0');" in v2_js
    assert "el.setAttribute('aria-expanded', 'false');" in v2_js


def test_the_upgrade_is_one_shared_pass_not_per_call_site(v2_js):
    """The audit's explicit instruction: do this ONCE. The pass keys off the
    `title` attribute both writers already emit, so no renderer needs to know
    it exists and a future writer is covered for free."""
    fn = extract_function(v2_js, "enhanceFreshnessChips")
    assert "querySelectorAll('.v2-fresh[title]:not([data-fx])')" in fn
    assert v2_js.count("data-fx=") == 0, \
        "a call site is emitting the marker itself; it belongs in the one pass"


def test_the_parity_locked_writers_were_not_edited(v2_js):
    """paintFreshness/freshnessHtml are under a byte-identical contract with
    V1 (tests/test_v2_freshness.py). Adding touch affordances inside either
    body would break one frontend away from the other — which is exactly the
    half-fix this repo keeps shipping. Hence the separate pass."""
    for name in ("paintFreshness", "freshnessHtml"):
        body = strip_comment_lines(extract_function(v2_js, name))
        assert "data-fx" not in body
        assert "tabindex" not in body


def test_hover_text_and_tap_text_are_the_same_string(v2_js):
    """The note renders the `title` verbatim — no V2-only transform between
    what a mouse reads and what a finger reads."""
    show = v2_js[v2_js.index("  function show(chip){"):]
    show = show[:show.index("\n  }")]
    assert "var text = chip.getAttribute('title') || '';" in show
    assert "body.textContent = text;" in show


def test_the_title_is_kept_so_desktop_hover_still_works(v2_js):
    """The tap affordance is additive. Removing `title` would trade one
    platform's access for another's."""
    assert "' title=\"' + escapeHtml(o.title + ' Not the page build time.') + '\"'" in v2_js


def test_the_tap_affordance_keys_off_the_attribute_not_a_class(v2_js):
    """paintFreshness() reassigns className and textContent on every repaint,
    so a class or an appended marker node would be wiped; the attribute and
    its ::after survive."""
    assert '.v2-fresh[title]::after{content:"\\00a0\\24D8"' in v2_js


def test_the_disclosure_is_not_a_native_alert(v2_js):
    body = shared_block(v2_js, *FRESHNOTE_JS)
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("//"))
    assert "alert(" not in code
    assert ".v2-freshnote" in v2_js
    assert "x.textContent = 'Got it';" in body


def test_the_disclosure_is_dismissible_every_way(v2_js):
    """Tap the chip again, press the Got it button, tap outside, or Escape."""
    body = shared_block(v2_js, *FRESHNOTE_JS)
    assert "if (open && open.chip === chip){ close(true); return; }" in body   # tap again
    assert "x.addEventListener('click', function(e){ e.stopPropagation(); close(true); });" in body
    assert "if (open && !(e.target.closest && e.target.closest('.v2-freshnote'))) close(false);" in body
    assert "if (e.key === 'Escape'){ close(true); return; }" in body


def test_chips_answer_enter_and_space(v2_js):
    body = shared_block(v2_js, *FRESHNOTE_JS)
    assert "e.key !== 'Enter' && e.key !== ' '" in body


def test_chips_have_a_focus_ring(v2_js):
    assert re.search(r"\.v2-fresh\[title\]:focus-visible\{outline:2px solid", v2_js)


def test_the_disclosure_cannot_widen_a_360px_card(v2_js):
    """It is position:fixed on <body>, so it is outside every card's flex/grid
    flow by construction, and JS clamps it to the viewport."""
    rule = v2_js.split(".v2-freshnote{")[1].split("}")[0]
    assert "position:fixed" in rule
    assert "max-width:min(340px,calc(100vw - 16px))" in rule
    assert "overflow-wrap:anywhere" in v2_js.split(".v2-freshnote__body{")[1].split("}")[0]


# --- S2 (shared defect): the note must not outlive the chip that owns it ---


def test_the_note_does_not_survive_hash_navigation(v2_js):
    """MEASURED before the fix, on the built V2 page at 360x740: open the note
    on the ETF tab's stamp, set location.hash to #travel, and the note was
    still present and visible (position:fixed, z-index 400, parented to
    <body>) while its owning chip sat in a display:none panel. An explanation
    of ETF freshness floating over Travel data is a freshness claim attached
    to the wrong numbers."""
    body = shared_block(v2_js, *FRESHNOTE_JS)
    assert "window.addEventListener('hashchange', function(){ close(false); });" in body
    assert "window.addEventListener('popstate', function(){ close(false); });" in body


def test_a_tab_click_closes_it_even_though_it_fires_no_navigation(v2_js):
    """selectTab syncs the URL with history.replaceState (deliberately — a
    location.hash write would re-enter selectTab). So a tab CLICK produces no
    hashchange at all and needs the direct call."""
    assert "window.__closeFreshnessNote = function(){ close(false); };" in v2_js
    fn = extract_function(v2_js, "selectTab")
    assert "window.__closeFreshnessNote()" in fn


def test_an_orphaned_note_is_reaped_on_the_next_repaint(v2_js):
    """A renderer can replace the card under an open note with innerHTML, with
    no navigation involved at all — so liveness is re-checked on every DOM
    mutation pass, not only on the events a reader generates."""
    body = shared_block(v2_js, *FRESHNOTE_JS)
    assert "function ownerLive(chip)" in body
    assert "if (!document.contains(chip)) return false;" in body
    assert "function closeIfDead()" in body
    assert "closeIfDead();" in body


def test_scrolling_the_owner_away_dismisses_rather_than_drags(v2_js):
    """A fixed-position note re-placed on every scroll event stays glued to
    the screen after its chip has scrolled off it — the same "explanation
    detached from its data" failure, without any navigation."""
    body = shared_block(v2_js, *FRESHNOTE_JS)
    assert "function ownerOnScreen(chip)" in body
    assert "function closeIfOffScreen()" in body
    assert "closeIfOffScreen();" in body


def test_a_backgrounded_tab_does_not_keep_the_note(v2_js):
    body = shared_block(v2_js, *FRESHNOTE_JS)
    assert "visibilitychange" in body


# --------------------------------- S3: the history entry point was 22px ----


def test_history_button_meets_the_44px_touch_floor(v2_js):
    """Measured 186x22. It is the ONLY way into the composite history charts,
    a feature that had just shipped."""
    block = touch_block(v2_js)
    assert re.search(r"\.v2-histbtn\{[^}]*min-height:44px", block)


def test_travel_subtabs_meet_the_44px_touch_floor(v2_js):
    """Measured 69x28."""
    block = touch_block(v2_js)
    assert re.search(r"\.travel-subtab\{[^}]*min-height:44px", block)


def test_touch_sizes_are_gated_so_desktop_is_untouched(v2_js):
    assert "@media (pointer:coarse),(max-width:860px){" in v2_js


# ------------------------ V2-A: the 15-tab strip had no honest affordance ---


def test_the_edge_fade_is_directional_not_permanent(v2_js):
    """The old rule faded the right edge unconditionally, so it kept claiming
    "more tabs this way" after the user had already reached the end. An
    affordance that lies is worse than no affordance."""
    fn = extract_function(v2_js, "updateTabScrollAffordance")
    assert "v2-tabs--more-left" in fn and "v2-tabs--more-right" in fn
    assert "scrollLeft" in fn
    # the mask only exists behind those classes
    assert ".tabs.v2-tabs--more-right{" in v2_js
    assert ".tabs.v2-tabs--more-left{" in v2_js
    assert not re.search(r"\.tabs\{[^}]*mask-image", v2_js)


def test_tab_strip_snaps_but_not_mandatorily(v2_js):
    """`mandatory` + centre alignment can make the first and last tabs
    unreachable at 360px."""
    assert "scroll-snap-type:x proximity" in v2_js
    assert "scroll-snap-type:x mandatory" not in v2_js


def test_keyboard_focus_drags_the_strip_with_it(v2_js):
    """Tabbing to an off-screen tab used to paint a focus ring the user could
    not see."""
    assert re.search(r"b\.addEventListener\('focus'", v2_js)


# ------------------------ V2-B: absence is not a delay ---------------------


def test_feed_empty_only_claims_loading_when_a_fetch_is_in_flight(v2_js):
    fn = extract_function(v2_js, "feedEmpty")
    assert "SIDECAR_STATE" in fn
    assert "'loading'" in fn
    assert "inFlight" in fn


def test_feed_empty_never_promises_a_refresh_it_cannot_keep(v2_js):
    fn = extract_function(v2_js, "feedEmpty")
    absent = fn[fn.index("Not in flight"):]
    assert "refresh in a moment" not in absent
    assert "reloading serves" in absent


def test_feed_empty_says_absence_not_zero(v2_js):
    """Honesty rule 5: a real 0 is a reading, a missing value is not."""
    fn = extract_function(v2_js, "feedEmpty")
    assert "not a count of zero" in fn


def test_feed_empty_reports_the_age_it_already_knows(v2_js):
    """"where a freshness resolver already knows the feed's age, say so
    instead of promising a refresh"."""
    fn = extract_function(v2_js, "feedEmpty")
    assert "opts.freshness" in fn
    assert "freshnessHtml(f.date" in fn


def test_the_named_headline_state_uses_it(v2_js):
    """The audit's worked example: "Headlines warming up / No top news yet -
    refresh in a moment" over a feed that may be permanently absent."""
    fn = strip_comment_lines(extract_function(v2_js, "renderOverviewNews"))
    assert "Headlines warming up" not in fn
    assert "No top news yet" not in fn
    assert "V2.feedEmpty(" in fn
    assert "overviewFreshness" in fn


def test_no_news_surface_still_promises_a_refresh(v2_js):
    """Code only — the comments above these renderers quote the old copy on
    purpose, so the next reader learns what not to reintroduce."""
    for name in ("renderOverviewNews", "renderNews"):
        fn = strip_comment_lines(extract_function(v2_js, name))
        assert "refresh in a moment" not in fn, name
        assert "next refresh" not in fn, name


def test_no_surface_anywhere_still_promises_a_refresh(v2_js):
    """The audit named the headline case, but "refresh in a moment" over a
    feed that may be permanently absent was the whole class. Comments are
    exempt — one of them quotes the old copy on purpose."""
    offenders = [
        (n, line.strip()[:90])
        for n, line in enumerate(v2_js.splitlines(), 1)
        if not line.lstrip().startswith("//")
        and ("refresh in a moment" in line or "next refresh" in line)
        and "is a PROMISE" not in line
    ]
    assert not offenders, offenders


def test_a_failed_sidecar_re_renders_instead_of_spinning_forever(v2_js):
    """Found by walking all 15 tabs against a build whose DefiLlama sidecar
    errored. selectTab only re-rendered when the fetch SUCCEEDED, so a
    failure left the DOM exactly as the in-flight pass had painted it:
    "Loading DeFi data… usually under a second" on screen permanently, with
    no later render to take it down. The purest form of the V2-B defect."""
    fn = extract_function(v2_js, "selectTab")
    assert "loadSidecar(_sc).then(() => { if (state.tab === t) renderAll(); });" in fn, \
        "the re-render is gated on success again"


def test_a_failed_sidecar_says_so(v2_js):
    fn = extract_function(v2_js, "sidecarFailureHtml")
    assert "failed fetch, not a slow one" in fn
    assert "no reading" in fn, "absence must not read as zero"


def test_an_empty_object_is_not_data(v2_js):
    """loadSidecar leaves `{}` behind on some failure paths. A truthy `{}`
    passed the old `!DATA[name]` guard, so a hard failure rendered as a wall
    of em-dashes that a reader can take for measured nulls."""
    fn = extract_function(v2_js, "sidecarFailed")
    assert "Object.keys(d).length === 0" in fn
    assert "Array.isArray(d)" in fn


def test_every_lazy_tab_gates_on_failure_not_just_on_loading(v2_js):
    """All six sidecar-backed tabs, or the next one added copies the bug."""
    for name in ("defi", "travel", "cpi", "supplies", "metals", "mufon"):
        assert f"const {name}Failed = sidecarFailed('{name}');" in v2_js, name
        assert f"{name}Loading.innerHTML = sidecarFailureHtml('{name}');" in v2_js, name


def test_absent_and_warming_are_not_the_same_element(v2_js):
    assert ".v2-empty.v2-empty--absent{" in v2_js
    empty_fn_src = v2_js[v2_js.index("  function empty(opts){"):]
    empty_fn_src = empty_fn_src[:empty_fn_src.index("\n  }")]
    assert "v2-empty--absent" in empty_fn_src


# ------------------------ V2-C: the scroll marathon ------------------------


def test_travel_subnav_is_reachable_for_the_whole_13k_px_tab(v2_js):
    block = touch_block(v2_js)
    assert re.search(r"\.travel-subtabs\{[^}]*position:sticky", block)


def test_the_sticky_preview_banner_is_opaque(v2_js):
    """Found while verifying the sticky sub-nav, and pre-existing: both tint
    tokens are 14%-alpha rgba and the shorthand left no base colour, so the
    banner had nothing opaque behind it. Scrolling the 13.6k-px Travel tab
    ran advisory text straight through the words "PRODUCTION DASHBOARD IS
    UNCHANGED"."""
    m = re.search(r"\.v2-banner\{(.*?)\}", v2_js, re.S)
    assert m, "the banner rule is gone"
    bg = re.search(r"background:linear-gradient\(90deg,[^;]*;", m.group(1))
    assert bg, "banner background declaration is gone"
    assert "var(--panel)" in bg.group(0), \
        "the banner needs an opaque colour under its translucent tint"


def test_the_sticky_offset_is_measured_not_guessed(v2_js):
    """The preview banner is itself position:sticky;top:0. A hard-coded
    offset hides the sub-nav under it the moment the banner rewraps."""
    assert "--v2-banner-h" in v2_js
    assert "syncBannerHeight" in v2_js


# ------------------------ V2-D: an axis that exaggerates is a chart that lies


@pytest.fixture(scope="module")
def chart_ctx(v2_js):
    py_mini_racer = pytest.importorskip(
        "py_mini_racer", reason="V8 needed to execute the shipped JS")
    ctx = py_mini_racer.MiniRacer()
    bodies = "\n".join(
        [re.search(r"^const COMPOSITE_HISTORY_MIN_TREND_POINTS\s*=.*?;\s*$",
                   v2_js, re.M).group(0),
         re.search(r"^const COMPOSITE_HISTORY_GAP_BREAK_DAYS\s*=.*?;\s*$",
                   v2_js, re.M).group(0)]
        + [extract_function(v2_js, n) for n in
           ("escapeHtml", "freshnessDayUTC", "freshnessYmd", "compositeHistoryChart")])
    ctx.eval(bodies + "\nfunction __chart(pts){ return compositeHistoryChart(pts); }")
    return lambda points: ctx.call("__chart", points)


def pt(as_of, score):
    return {"as_of": as_of, "score": score, "stale": False, "snapshot": as_of,
            "label": None, "note": None}


def _plot_span_px(svg: str) -> float:
    ys = [float(m) for m in re.findall(r'<circle cx="[\d.]+" cy="([\d.]+)"', svg)]
    assert ys, "no points plotted"
    return max(ys) - min(ys)


def test_a_trivial_move_looks_trivial(chart_ctx):
    """THE defect. These indexes are bounded scores. With no minimum span the
    axis fits itself to the observed range, so a composite that crept from 51
    to 53 was drawn across the whole chart height.

    Measured before the fix: 127.6px of a 166px plot area — 76.9%, pixel for
    pixel identical to a real -80 -> +75 swing."""
    svg = chart_ctx([pt("2026-07-01", 51), pt("2026-07-02", 52), pt("2026-07-03", 53)])
    span = _plot_span_px(svg)
    assert span < 25, f"a 2-point move still fills {span:.1f}px of the plot"


def test_a_real_swing_is_not_compressed(chart_ctx):
    """The floor may only ever WIDEN the domain. If it narrowed one, it would
    be hiding movement instead of exaggerating it — the opposite lie."""
    svg = chart_ctx([pt("2026-07-01", -80), pt("2026-07-02", 0), pt("2026-07-03", 75)])
    assert _plot_span_px(svg) > 120


def test_a_trivial_move_and_a_real_swing_no_longer_look_identical(chart_ctx):
    tiny = _plot_span_px(chart_ctx(
        [pt("2026-07-01", 51), pt("2026-07-02", 52), pt("2026-07-03", 53)]))
    real = _plot_span_px(chart_ctx(
        [pt("2026-07-01", -80), pt("2026-07-02", 0), pt("2026-07-03", 75)]))
    assert real > tiny * 4


def test_a_flat_series_stays_flat(chart_ctx):
    svg = chart_ctx([pt("2026-07-01", 60), pt("2026-07-02", 60), pt("2026-07-03", 60)])
    assert _plot_span_px(svg) == 0


def test_the_floor_is_declared_where_it_is_used(v2_js):
    """Kept local to compositeHistoryChart so the V8 harnesses that extract
    that one function keep working."""
    fn = extract_function(v2_js, "compositeHistoryChart")
    assert "MIN_SPAN" in fn
    assert "const MIN_SPAN" in fn


# ------------------------ V2-E: the two that actually block use ------------


def test_the_modal_traps_focus(v2_js):
    """It declared role="dialog" aria-modal="true" and then let one Tab put
    the user back on the page behind it. aria-modal is a promise to the
    assistive tech; the trap is what makes it true."""
    wire = v2_js[v2_js.index("function wireCompositeHistory"):]
    wire = wire[:wire.index("\n})();")]
    assert "e.key !== 'Tab'" in wire
    assert "e.shiftKey" in wire
    assert "modal.contains(document.activeElement)" in wire


def test_the_modal_still_moves_and_restores_focus(v2_js):
    opened = extract_function(v2_js, "openCompositeHistory")
    assert "closeBtn.focus()" in opened
    closed = extract_function(v2_js, "closeCompositeHistory")
    assert "back.focus()" in closed


def test_the_modal_closes_on_escape(v2_js):
    wire = v2_js[v2_js.index("function wireCompositeHistory"):]
    assert "if (e.key === 'Escape') closeCompositeHistory();" in wire


def test_both_text_inputs_have_a_visible_focus_indicator(v2_js):
    """Every other interactive element on the page had one; these two
    computed outline:none with no box-shadow."""
    m = re.search(r"#symbolSearchInput:focus-visible,\s*"
                  r"\.chat-form input#chatInput:focus-visible\{([^}]*)\}", v2_js)
    assert m, "the focus rule for the two inputs is gone"
    assert "outline:2px solid" in m.group(1)
    assert "!important" in m.group(1), \
        "#symbolSearchInput carries outline:none in an inline style attribute"


def test_every_canvas_gets_an_accessible_name(v2_js):
    """34 of 34 canvases were announced as nothing at all."""
    fn = extract_function(v2_js, "wireChartAndPanelSemantics")
    assert "querySelectorAll('canvas')" in fn
    assert "aria-label" in fn


def test_a_canvas_that_cannot_be_named_is_left_alone(v2_js):
    """A made-up label is a new inaccuracy, not an improvement."""
    fn = extract_function(v2_js, "wireChartAndPanelSemantics")
    assert "if (!title) return;" in fn


def test_the_tab_pattern_is_wired_to_its_panels(v2_js):
    """20 role="tab" elements, 0 role="tabpanel", 0 aria-controls."""
    fn = extract_function(v2_js, "wireChartAndPanelSemantics")
    assert "'aria-controls'" in fn
    assert "'tabpanel'" in fn
    assert "'aria-labelledby'" in fn
