"""Critical-path, mobile-layout and touch-reachability guards for V1.

`app.py` builds the root `dashboard.html` that pages.yml deploys — the page
the user actually looks at, and looks at ON A PHONE. A site audit measured, in
headless Chromium:

  * domInteractive / DOMContentLoaded 12,811-13,033ms at every viewport, with
    First Contentful Paint at a healthy 396-448ms. The document was ready in
    under half a second and then sat waiting on third parties: four
    render-blocking requests across three origins (fonts.googleapis.com CSS,
    cdn.jsdelivr.net Chart.js, unpkg.com Leaflet CSS + JS). The single slowest
    resource was the webfont stylesheet at 12,697ms, started at t=26ms.
  * 148px of header + 91px of tab bar = 239px of chrome at BOTH 360x740 and
    390x844 — 32% of the viewport before a single number is visible.
  * Two adjacent buttons both labelled "Share".
  * Both text inputs under 16px, which makes iOS Safari zoom on focus and
    strands the reader pinched in.
  * The only entry point to the composite history charts a 186x22 tap target.
  * Every freshness explanation locked inside a `title=` attribute, which does
    not exist on a touchscreen.

These are cheap static guards so the fixes cannot silently regress. The
timings themselves are measured in a browser, not asserted here — what IS
asserted is the structural property the timings depend on: nothing
third-party may sit on the critical path.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
V1_APP = ROOT / "app.py"

THIRD_PARTY_ORIGINS = (
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "cdn.jsdelivr.net",
    "unpkg.com",
)


def _template(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    i = src.index(marker)
    return src[i + len(marker):]


def _strip_html_comments(html: str) -> str:
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


@pytest.fixture(scope="module")
def v1_tpl() -> str:
    return _template(V1_APP)


@pytest.fixture(scope="module")
def v1_markup(v1_tpl: str) -> str:
    """Template with HTML comments removed — commented-out tags are not tags."""
    return _strip_html_comments(v1_tpl)


# ------------------------------------------------------------ P1: critical path


def test_no_parser_blocking_third_party_script(v1_markup):
    """A classic `script src` with neither async nor defer blocks the parser
    AND holds DOMContentLoaded. `defer` is not a fix either: deferred scripts
    still run before DCL fires."""
    offenders = []
    for m in re.finditer(r"<script\b([^>]*)\bsrc=[\"']([^\"']+)[\"']([^>]*)>",
                         v1_markup, re.I):
        attrs = (m.group(1) + " " + m.group(3)).lower()
        url = m.group(2)
        if "//" not in url:
            continue
        if "async" not in attrs:
            offenders.append(url)
    assert offenders == [], (
        "third-party script tags that gate DOMContentLoaded: " + repr(offenders))


def test_no_third_party_stylesheet(v1_markup):
    """A stylesheet blocks rendering wherever it appears — head OR body. The
    webfont one was measured at 12,697ms."""
    sheets = [
        m.group(1)
        for m in re.finditer(r"<link\b[^>]*\brel=[\"']?stylesheet[^>]*\bhref=[\"']([^\"']+)",
                             v1_markup, re.I)
    ] + [
        m.group(1)
        for m in re.finditer(r"<link\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*\brel=[\"']?stylesheet",
                             v1_markup, re.I)
    ]
    remote = [s for s in sheets if "//" in s]
    assert remote == [], "render-blocking remote stylesheet(s): " + repr(remote)


@pytest.mark.parametrize("origin", THIRD_PARTY_ORIGINS)
def test_no_third_party_origin_is_reached_from_static_markup(v1_markup, origin):
    """Every remaining third-party fetch must be made by JS at a moment of its
    choosing (async Chart.js, lazy Leaflet), never by a tag the parser trips
    over. `preconnect`/`dns-prefetch` hints are allowed — they open a socket
    and gate nothing."""
    for m in re.finditer(r"<(script|link)\b([^>]*)>", v1_markup, re.I):
        attrs = m.group(2)
        if origin not in attrs:
            continue
        assert re.search(r"rel=[\"']?(preconnect|dns-prefetch)", attrs, re.I), (
            f"{origin} is referenced by a parser-visible tag: {m.group(0)[:160]}")


def test_webfont_is_gone_entirely(v1_tpl):
    """Dropped for the system stack rather than made non-blocking: the UI is
    data-dense, Plex was referenced by one tab's tokens and the chart axes,
    and a font that is already installed paints on the first frame."""
    assert "fonts.googleapis.com/css2" not in v1_tpl
    assert 'family:"IBM Plex Mono"' not in v1_tpl
    assert "--mono:ui-monospace" in v1_tpl


def test_chart_js_is_loaded_async_and_gated(v1_tpl):
    """Chart.js still ships (SRI pin intact) but via an injected async script,
    and the first render waits on whenChartsReady() rather than on the CDN."""
    assert "s.async = true;" in v1_tpl
    assert "sha384-e6nUZLBkQ86NJ6TVVKAeSaK8jWa3NhkYWZFomE39AvDbQWeie9PlQqM3pmYW5d1g" in v1_tpl
    assert "window.whenChartsReady = function(cb)" in v1_tpl
    assert "whenChartsReady(function(){" in v1_tpl


def test_chart_fallback_is_installed_by_the_gate_not_at_parse_time(v1_tpl):
    """With an async loader, "Chart is undefined right now" means "still in
    flight", not "failed". Installing the no-op stub at parse time would paint
    'Chart library unavailable' over charts that were about to work."""
    assert "window.__installChartFallback = function()" in v1_tpl
    assert "window.__installChartFallback();" in v1_tpl
    # …and it still must beat the first real `new Chart(` in source order.
    code = re.sub(r"^\s*//.*$", "", v1_tpl, flags=re.M)
    stub = code.index("function ChartUnavailable(canvas)")
    first = re.search(r"new Chart\(", code)
    assert first and stub < first.start()


def test_leaflet_loads_only_when_a_map_is_opened(v1_tpl):
    """Two requests on a third origin for a sub-view most sessions never open."""
    assert "function ensureLeaflet(cb)" in v1_tpl
    assert "function map(){ if(drawn.map) return; ensureLeaflet(_mapDraw); }" in v1_tpl
    # SRI pins carried over to the injected tags.
    assert "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" in v1_tpl
    assert "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" in v1_tpl


def test_connection_hints_only_where_a_request_remains(v1_markup):
    """Chart.js is the one request that always happens, so it gets the one
    hint. Hinting an origin that may never be contacted is just noise."""
    hints = re.findall(r"<link\b[^>]*rel=[\"']?(?:preconnect|dns-prefetch)[^>]*href=[\"']([^\"']+)",
                       v1_markup, re.I)
    assert hints == ["https://cdn.jsdelivr.net"], hints


# --------------------------------------------------- P2/P4/P5: header + nav


def test_mobile_header_drops_the_decorative_tagline(v1_tpl):
    assert "header .tagline{display:none}" in v1_tpl


def test_the_tab_strip_is_one_scrolling_row(v1_tpl):
    """Seven top-level items need 554px at 360px, and 44px touch targets mean
    wrapping costs two full rows. The strip scrolls INSIDE itself, so the
    document never scrolls sideways. This half of the compaction was fine and
    stays."""
    assert re.search(r"\.tabs\{[^}]*flex-wrap:nowrap", v1_tpl, re.S)


def test_header_controls_are_never_hidden_behind_a_horizontal_scroll(v1_tpl):
    """THE B5 REGRESSION GUARD.

    The header controls were briefly made a `flex-wrap:nowrap; overflow-x:auto`
    row to buy height. Measured at 360x740 with a coarse pointer, that put 2 of
    5 controls (◉ Data Sources, 🦈 Shark Tank) behind 177px of horizontal
    scroll whose only cue was a 22px gradient mask — 3 of 7 in server mode, 2
    of 7 at 320px. Height bought by hiding controls is not a saving, it is a
    hiding.

    The controls now dissolve into the header's own flex context
    (`display:contents`) so the <h1> and the buttons pack onto shared wrapped
    lines: 105px header / 150px chrome at 360x740 with 5/5 (static) and 7/7
    (server) controls reachable, measured in headless Chromium.
    """
    mobile = v1_tpl.split("@media (max-width:480px){", 1)[1]
    assert "header .controls{display:contents}" in mobile
    # The exact shape of the regression must not come back.
    assert "header .controls{flex:1 1 100%;flex-wrap:nowrap" not in v1_tpl
    # ...nor the fake affordance that was standing in for reachability.
    assert "mask-image:linear-gradient(to right,#000 calc(100% - 22px)" not in v1_tpl
    # Every control keeps its words. Hiding the .hbl labels was tried before
    # and left a row of unlabelled glyphs, so the rule keeping them is load
    # bearing.
    assert "header .controls .hbl{display:inline}" in v1_tpl


def test_header_controls_keep_the_touch_floor(v1_tpl):
    """Nothing may be shrunk below 44px to make the new packing fit."""
    m = re.search(r"header \.controls \.btn\{([^}]*)\}", v1_tpl, re.S)
    assert m, "no mobile rule for header control buttons"
    assert "min-height:44px" in m.group(1) and "min-width:44px" in m.group(1)


def test_dropdown_menus_escape_the_scrolling_strip(v1_tpl):
    """An overflow container clips an absolutely-positioned child, so the
    mobile panels are fixed and JS supplies the top from the button."""
    assert "--tabmenu-top" in v1_tpl
    assert "function positionTabMenu(btn)" in v1_tpl


def test_the_two_nav_models_are_visually_distinguished(v1_tpl):
    """Dropdown groups are outlined pills with a caret; direct jumps are flat
    tabs; a rule separates the two clusters."""
    assert '<span class="tabnav-rule"' in v1_tpl
    assert '<span class="tabnav-jumps"' in v1_tpl
    assert ".tabgroup:not(.tabgroup--solo) .tabgroup-btn{" in v1_tpl


def test_summit_announces_that_it_leaves_the_dashboard(v1_tpl):
    """It runs `window.location.href = 'landscape/…'` — it is a link, not a
    tab, and must say so before the tap, not after."""
    m = re.search(r'<div class="tab tab--solo tab--exit" data-tab="summit"(.*?)</div>',
                  v1_tpl, re.S)
    assert m, "summit exit markup not found"
    block = m.group(1)
    assert 'role="link"' in block
    assert "aria-selected" not in block
    assert "leaves this dashboard" in block
    assert 'class="exitmark"' in block
    assert ".tab--exit{" in v1_tpl and "border-bottom-style:dashed" in v1_tpl


# ------------------------------------------------------------- P3: two Shares


def test_the_two_share_controls_do_not_both_say_share(v1_tpl):
    """#shareBtn MINTS an expiring read-only link (server call); #webShareBtn
    hands the current URL to the OS share sheet (client only). They sat
    adjacent, both reading 'Share'."""
    mint = re.search(r'<button class="btn" id="shareBtn"(.*?)</button>', v1_tpl, re.S).group(0)
    web = re.search(r'<button class="btn" id="webShareBtn"(.*?)</button>', v1_tpl, re.S).group(0)
    assert "New link" in mint and "Send" in web
    assert ">Share<" not in mint and "> Share<" not in mint
    assert "Share</span>" not in web
    # distinct accessible names, which is what a screen reader reads out
    assert 'aria-label="Create a new read-only share link"' in mint
    assert "aria-label=\"Send this page to another app" in web


# ------------------------------------------------------- S1: iOS focus zoom


def test_touch_inputs_are_at_least_16px(v1_tpl):
    assert "@media (pointer:coarse),(max-width:640px){" in v1_tpl
    block = v1_tpl.split("@media (pointer:coarse),(max-width:640px){", 1)[1][:900]
    assert "font-size:16px !important" in block
    assert "textarea" in block and "select" in block


def test_viewport_meta_still_allows_pinch_zoom(v1_tpl):
    """Disabling zoom 'fixes' the symptom by breaking accessibility for
    everyone — and modern iOS ignores it anyway."""
    m = re.search(r'<meta name="viewport" content="([^"]+)"', v1_tpl)
    assert m, "viewport meta missing"
    content = m.group(1)
    assert content == "width=device-width, initial-scale=1", content
    assert "maximum-scale" not in content
    assert "user-scalable" not in content


def test_no_mobile_rule_pushes_a_text_input_back_under_16px(v1_tpl):
    """The old `header #symbolSearchInput{…font-size:11px}` would have undone
    the whole S1 fix for the one input the reader touches most."""
    assert "header #symbolSearchInput{width:140px !important;min-height:44px" in v1_tpl
    assert "header #symbolSearchInput{width:84px !important;font-size:11px" not in v1_tpl


# ------------------------------------------------- S2: touch-reachable honesty


def test_freshness_chips_are_activatable_not_hover_only(v1_tpl):
    """Every explanation of a stamp lived in `title=`, which has no activation
    on a touchscreen — so on the phone this is mostly read on, the tersest and
    most alarming states ('as of —', a red chip) were the least explainable."""
    assert "(function freshnessNotes(){" in v1_tpl
    assert "function enhanceFreshnessChips()" in v1_tpl
    # keyboard + focus, not just tap
    assert "el.setAttribute('role', 'button');" in v1_tpl
    assert "el.setAttribute('tabindex', '0');" in v1_tpl
    assert ".v2-fresh[title]:focus-visible{" in v1_tpl
    # dismissible, and explicitly not a native alert()
    assert ".v2-freshnote" in v1_tpl
    body = v1_tpl.split("(function freshnessNotes(){", 1)[1].split("\n})();", 1)[0]
    code = "\n".join(l for l in body.splitlines() if not l.strip().startswith("//"))
    assert "alert(" not in code


def test_freshness_title_is_kept_for_desktop_hover(v1_tpl):
    """The tap affordance is additive. Removing `title` would trade one
    platform's access for another's."""
    assert "' title=\"' + escapeHtml(o.title + ' Not the page build time.') + '\"'" in v1_tpl


def test_the_tap_affordance_keys_off_the_attribute_not_a_class(v1_tpl):
    """paintFreshness() reassigns className and textContent on every repaint,
    so a class or an appended marker node would be wiped; the attribute and
    its ::after survive."""
    assert '.v2-fresh[title]::after{content:"\\00a0\\24D8"' in v1_tpl


def test_aviation_refusal_is_still_a_refusal(v1_tpl):
    """Rule 4: no honest date renders 'as of —', and the reason must now be
    readable on a phone rather than hidden in a hover."""
    fn = v1_tpl.split("function aviationFreshness(){", 1)[1].split("\n}", 1)[0]
    assert "prose vintage" in fn
    assert "Date.now" not in fn and "generated_at" not in fn


# --------------------------------------------------------- S3: history target


def test_history_button_meets_the_touch_target_floor(v1_tpl):
    """186x22 was the ONLY way into the composite history charts."""
    assert re.search(r"\.histbtn\{[^}]*min-height:32px", v1_tpl, re.S)
    assert ".histbtn::after{content:\"\";position:absolute" in v1_tpl
    coarse_blocks = v1_tpl.split("@media (pointer:coarse),(max-width:640px){")
    assert any("min-height:44px" in b[:600] and ".histbtn{" in b[:600]
               for b in coarse_blocks[1:]), "no coarse-pointer 44px rule for .histbtn"


# ===========================================================================
# Parity blockers: things V2 fixed and V1 had not (B1-B4, B6).
#
# The previous round split the work so that app.py got the performance and
# header fixes while v2/app.py got the y-axis, the focus trap and the sub-tab
# sizing — and each was separately told to do the "shared" items. The result
# was a gate full of "V2 fixed this, V1 did not". These guards exist so the
# two frontends cannot drift apart again silently.
# ===========================================================================


V2_APP = ROOT / "v2" / "app.py"


@pytest.fixture(scope="module")
def v2_tpl() -> str:
    if not V2_APP.exists():  # pragma: no cover - repo layout guard
        pytest.skip("v2/app.py not present")
    return _template(V2_APP)


# ------------------------------------------------- B2: history focus trap


def test_history_dialog_traps_focus(v1_tpl):
    """`aria-modal="true"` is a promise to assistive tech that the rest of the
    page is inert. Without a trap it is a lie: measured, ONE Tab put focus
    back on the page behind the open dialog (Shift+Tab leaked the same way),
    with nothing to tell the reader they had left."""
    assert "// FOCUS TRAP" in v1_tpl
    trap = v1_tpl.split("// FOCUS TRAP", 1)[1].split("})();", 1)[0]
    # Tab is intercepted only while the dialog is actually open...
    assert "if (e.key !== 'Tab') return;" in trap
    assert "modal.classList.contains('hidden')" in trap
    # ...focus wraps at BOTH ends...
    assert "e.shiftKey && document.activeElement === first" in trap
    assert "!e.shiftKey && document.activeElement === last" in trap
    # ...and focus that is already outside gets pulled back in.
    assert "!modal.contains(document.activeElement)" in trap


def test_history_dialog_moves_focus_in_and_gives_it_back(v1_tpl):
    """A trap without an entry and an exit is a cage."""
    op = v1_tpl.split("function openCompositeHistory(key){", 1)[1].split("\n}", 1)[0]
    assert "_compositeHistoryReturnFocus" in op and "closeBtn.focus()" in op
    cl = v1_tpl.split("function closeCompositeHistory(){", 1)[1].split("\n}", 1)[0]
    assert "back.focus()" in cl


def test_focus_trap_matches_v2(v1_tpl, v2_tpl):
    def trap(src: str) -> str:
        block = src.split("// FOCUS TRAP", 1)[1].split("})();", 1)[0]
        return "\n".join(l.strip() for l in block.splitlines()
                         if l.strip() and not l.strip().startswith("//"))
    assert trap(v1_tpl) == trap(v2_tpl), "the focus trap has drifted from V2"


# ------------------------------------------------- B3: travel sub-tabs


def test_travel_subtabs_meet_the_touch_floor(v1_tpl):
    """Measured at 360x740, coarse pointer: 69.4x28, 72.6x28, 72.6x28,
    93.9x28, 83.8x28. The <=480px block was making them SMALLER (padding
    7px 10px, font-size 11px) rather than larger."""
    touch = v1_tpl.split("@media (pointer:coarse),(max-width:860px){", 1)
    assert len(touch) == 2, "no coarse-pointer touch block in V1"
    block = touch[1].split("\n}\n", 1)[0]
    assert re.search(r"\.travel-subtab\{[^}]*min-height:44px", block, re.S)
    # The <=480px block sets `padding:7px 10px`; the touch block must beat it
    # on BOTH axes or min-height fights the padding.
    assert re.search(r"\.travel-subtab\{[^}]*padding:0 6px", block, re.S)


def test_every_travel_subtab_is_tappable_without_scrolling_sideways(v1_tpl):
    """B5's lesson, applied one level down: "the strip scrolls" is not the
    same as "you can reach it". With V2's 10px button padding the strip
    measured 412px of content in a 340px box and the fifth sub-view
    (Terrorism) was not hit-testable at all — its centre sat past the viewport
    edge. 6px padding + a 3px gap brings it to 368/340: all five directly
    tappable at 360x740, smallest button 61.4x44."""
    block = v1_tpl.split("@media (pointer:coarse),(max-width:860px){", 1)[1]
    m = re.search(r"\.travel-subtabs\{([^}]*)\}", block, re.S)
    assert m and "gap:3px" in m.group(1)
    assert re.search(r"\.travel-subtab\{[^}]*padding:0 6px", block, re.S)


def test_travel_subnav_is_sticky(v1_tpl):
    """The sub-nav scrolled off the top of a very tall tab and never came
    back, so the five sub-views were unreachable for most of the scroll."""
    block = v1_tpl.split("@media (pointer:coarse),(max-width:860px){", 1)[1]
    m = re.search(r"\.travel-subtabs\{([^}]*)\}", block, re.S)
    assert m, "no sticky rule for .travel-subtabs"
    rule = m.group(1)
    assert "position:sticky" in rule
    # V1 has no sticky chrome above it, so the anchor is the viewport top.
    # (V2 sticks under its preview banner — that difference is deliberate.)
    assert "top:0" in rule
    assert "overflow-x:auto" in rule and "flex-wrap:nowrap" in rule


def test_travel_subtabs_stay_a_single_row(v1_tpl):
    """Five buttons need 392px of content at 360px. Wrapping to a second row
    would cost 44 more pixels of every screenful of a sticky element."""
    block = v1_tpl.split("@media (pointer:coarse),(max-width:860px){", 1)[1]
    assert re.search(r"\.travel-subtab\{flex:0 0 auto\}", block)


# ------------------------------------------------- B4: late-CDN recovery


def _onload_code(tpl: str) -> str:
    """The CDN onload handler with `//` comments stripped — the comments here
    quote the very identifiers these guards forbid."""
    block = tpl.split("s.onload = function(){", 1)[1].split("\n  };", 1)[0]
    return "\n".join(l for l in block.splitlines()
                     if not l.strip().startswith("//"))


def test_chart_late_arrival_recovery_is_not_dead_code(v1_tpl):
    """`state` is declared as a top-level `const` in a classic script, so it
    lives in the global LEXICAL environment and never becomes a property of
    `window`. The guard used to read `window.state`, which is therefore
    permanently undefined — measured directly in Chromium: `'state' in window`
    is false — so the whole recovery branch never ran and a slow CDN that
    finally answered repainted nothing."""
    assert re.search(r"^const state = \{", v1_tpl, re.M), (
        "state is no longer a top-level const; re-check this guard")
    onload = _onload_code(v1_tpl)
    assert "window.state" not in onload, (
        "window.state is always undefined for a top-level const — the "
        "recovery branch is dead code again")
    assert "typeof state === 'object'" in onload
    assert "selectTab(state.tab)" in onload


def test_the_late_arrival_branch_still_requires_a_real_chart(v1_tpl):
    """Repainting with the no-op stub still installed would just redraw the
    same empty frames."""
    onload = _onload_code(v1_tpl)
    assert "!window.Chart.__unavailable" in onload
    assert "late &&" in onload


# ------------------------------------------------- B6: freshness popover


def test_freshness_popover_cannot_outlive_its_owner(v1_tpl):
    """`.v2-freshnote` is position:fixed on <body>, so nothing in normal flow
    takes it away. Opened on #etf and then routed to #social it stayed on
    screen at z-index 400, over the nav, while its owning chip sat inside a
    display:none panel. The dashboard's own routing does this: a deep link, an
    in-page anchor, or the browser Back button."""
    notes = v1_tpl.split("(function freshnessNotes(){", 1)[1]
    # Owner liveness is what makes the guarantee, not a list of event names.
    assert "function ownerLive(chip)" in notes
    assert "document.contains(chip)" in notes
    assert "r.width > 0 || r.height > 0" in notes
    # Every route into a new tab.
    assert "window.addEventListener('hashchange'" in notes
    assert "window.addEventListener('popstate'" in notes
    assert "window.__closeFreshnessNote" in notes
    # Scrolling the owner away closes it rather than dragging the note along.
    scroll = notes.split("window.addEventListener('scroll'", 1)[1].split("});", 1)[0]
    assert "closeIfOffScreen()" in scroll
    # A re-render that removes or hides the chip closes it too.
    assert "closeIfDead()" in notes


def test_selectTab_tears_down_both_fixed_overlays(v1_tpl):
    """selectTab already closed .modal-bg dialogs on a tab change. The
    freshness note and the composite-history dialog are the same bug class —
    position:fixed, so the panel going display:none does not take them."""
    fn = v1_tpl.split("function selectTab(t){", 1)[1][:3000]
    assert "window.__closeFreshnessNote()" in fn
    assert "closeCompositeHistory()" in fn


def test_the_popover_still_closes_the_ordinary_ways(v1_tpl):
    """A lifecycle guard that closed it eagerly would pass every orphaning
    check and still be broken. The normal dismissals must survive."""
    notes = v1_tpl.split("(function freshnessNotes(){", 1)[1]
    assert "if (e.key === 'Escape'){ close(true); return; }" in notes
    assert "x.addEventListener('click'" in notes
    assert "close(false)" in notes
