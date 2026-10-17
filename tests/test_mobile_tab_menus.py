"""Phone/tablet nav: the grouped dropdown menus must actually paint.

At widths <= 860px the V1 tab strip is one horizontally scrolling row and the
Crypto / Markets / Macro / Explore panels switch to ``position:fixed`` so the
scroll container's overflow clip cannot hide them. A ``mask-image`` on
``.tabs`` (added for the right-edge "more this way" fade) undid that: a mask
clips EVERYTHING the element paints, fixed-position descendants included, so
each menu opened invisibly (aria-expanded="true", nothing on screen) and a tap
landed on the page underneath. 19 of 21 V1 tabs were unreachable on a phone.

These checks are static (no browser in CI). The live check that motivated
them: Chromium at 390px and 768px, each of the 19 menu items visible and
``document.elementFromPoint`` at its centre returning the item itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
V1_APP = ROOT / "app.py"

# Properties that make an element clip or re-anchor its position:fixed
# descendants (mask/clip-path clip the painted output; transform, filter,
# perspective, contain and will-change make it their containing block).
TRAPPING = re.compile(
    r"(?:^|;|\s)(?:-webkit-)?(?:mask(?:-image)?|clip-path|transform|filter|"
    r"backdrop-filter|perspective|contain|will-change)\s*:", re.I)


def _template(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    marker = 'HTML_TEMPLATE = r"""'
    return src[src.index(marker) + len(marker):]


def _css_rules(tpl: str):
    """Yield (selector, declarations) for every flat CSS rule in <style>."""
    for style in re.findall(r"<style[^>]*>(.*?)</style>", tpl, re.S):
        style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", style):
            yield m.group(1).strip(), m.group(2)


def _is_menu_ancestor(selector: str) -> bool:
    """True when a selector targets .tabs or .tabgroup ITSELF (not a child and
    not a pseudo-element), i.e. an element that contains .tabgroup-menu."""
    for sel in selector.split(","):
        last = re.split(r"[\s>+~]+", sel.strip())[-1]
        if "::" in last:
            continue
        if re.fullmatch(r"\.(?:tabs|tabgroup)(?:[.:\[][^\s]*)?", last) and \
                not last.startswith(".tabgroup-"):
            return True
    return False


@pytest.fixture(scope="module")
def v1_tpl() -> str:
    return _template(V1_APP)


def test_no_rule_on_a_menu_ancestor_clips_fixed_descendants(v1_tpl):
    offenders = [
        (sel, decl.strip()[:120])
        for sel, decl in _css_rules(v1_tpl)
        if _is_menu_ancestor(sel) and TRAPPING.search(decl)
    ]
    assert not offenders, (
        "a rule on .tabs/.tabgroup would clip or re-anchor the position:fixed "
        f"dropdown panels on phones: {offenders}")


def test_the_mobile_strip_has_no_mask(v1_tpl):
    mobile = v1_tpl.split("@media (max-width:860px){", 1)[1]
    m = re.search(r"\.tabs\{([^}]*)\}", mobile)
    assert m, "no mobile .tabs rule found"
    assert "mask" not in m.group(1)


def test_the_edge_fade_is_an_overlay_that_ignores_taps(v1_tpl):
    """The fade survives, drawn by a sticky pseudo-element that can never
    swallow a tap meant for the last visible tab."""
    rules = {sel: decl for sel, decl in _css_rules(v1_tpl)}
    decl = rules.get(".tabs::after")
    assert decl, "the right-edge fade overlay (.tabs::after) is missing"
    assert "pointer-events:none" in decl.replace(" ", "")
    assert "position:sticky" in decl.replace(" ", "")
    assert "linear-gradient" in decl


def test_the_menus_are_still_fixed_on_phones(v1_tpl):
    mobile = v1_tpl.split("@media (max-width:860px){", 1)[1]
    m = re.search(r"\.tabgroup-menu,\.tabgroup:last-child \.tabgroup-menu\{([^}]*)\}", mobile)
    assert m and "position:fixed" in m.group(1)


