// Behavioral checks for scripts/daily_audit_ux.mjs probeBadText.
//
// The 2026-10-25 daily audit (AMBER) raised three P1s because the headline
// "...Infinity Ward Veteran Says" matched /\bInfinity\b/ in #overviewNewsHost,
// #newsFeed, and #researchNewsHost. A metric whose text is the token itself
// (or that token beside a sign, currency symbol, or unit) must still be flagged.
//
// Run: node tests/check_daily_audit_bad_text.mjs
// (pytest tests/test_daily_audit_ux_bad_text.py shells out to this file.)

import assert from 'node:assert/strict';
import { probeBadText } from '../scripts/daily_audit_ux.mjs';

const HEADLINE = 'rs Have Pushed to Cut Discs Since 2007, Infinity Ward Veteran Says';

function closest(sel) {
  const parts = sel.split(',').map((s) => s.trim());
  for (let n = this; n; n = n.parentElement) {
    if (n.nodeType !== 1) continue;
    for (const p of parts) {
      if (p === '[id]') {
        if (n.id) return n;
      } else if (p.toLowerCase() === n.tagName.toLowerCase()) return n;
    }
  }
  return null;
}

function el(tag, props, kids) {
  const node = {
    nodeType: 1,
    tagName: String(tag).toUpperCase(),
    id: (props && props.id) || '',
    style: (props && props.style) || {},
    childNodes: [],
    parentElement: null,
    closest,
    getBoundingClientRect() {
      if (this.style.box === 'empty') return { width: 0, height: 0 };
      return { width: 40, height: 12 };
    },
  };
  for (const k of kids) {
    if (typeof k === 'string') node.childNodes.push({ nodeType: 3, nodeValue: k, parentElement: node });
    else {
      k.parentElement = node;
      node.childNodes.push(k);
    }
  }
  return node;
}

function install(body) {
  globalThis.NodeFilter = { SHOW_TEXT: 4 };
  globalThis.getComputedStyle = (element) => ({
    visibility: element.style.visibility || 'visible',
    display: element.style.display || 'block',
    opacity: element.style.opacity == null ? '1' : String(element.style.opacity),
  });
  globalThis.document = {
    body,
    querySelector(sel) {
      if (!sel) return null;
      if (sel.startsWith('#')) {
        const id = sel.slice(1);
        const stack = [body];
        while (stack.length) {
          const n = stack.shift();
          if (n.id === id) return n;
          for (const c of n.childNodes || []) if (c.nodeType === 1) stack.push(c);
        }
        return null;
      }
      return null;
    },
    createTreeWalker(root) {
      const found = [];
      (function walk(n) {
        if (n.nodeType === 3) found.push(n);
        for (const c of n.childNodes || []) walk(c);
      })(root);
      let i = -1;
      return { nextNode() { i += 1; return found[i] || null; } };
    },
  };
}

function matchesOf(body, rootSelector = null) {
  install(body);
  return probeBadText(rootSelector).map((b) => b.match);
}

function findings(body, rootSelector = null) {
  install(body);
  return probeBadText(rootSelector);
}

// The predicate has to travel with the function Playwright stringifies.
assert.match(probeBadText.toString(), /function isRenderedNumberGlitch/);
assert.match(probeBadText.toString(), /Infinity Ward/);

// --- the 2026-10-25 false positive: one headline, three surfaces ---
const headline = HEADLINE;
const page = el('div', {}, [
  el('div', { id: 'overviewNewsHost' }, [
    el('a', {}, [el('div', {}, [headline])]),
  ]),
  el('div', { id: 'newsFeed' }, [
    el('a', {}, [el('div', {}, [headline])]),
  ]),
  el('div', { id: 'researchNewsHost' }, [
    el('a', {}, [el('div', {}, [headline])]),
  ]),
]);
assert.deepEqual(matchesOf(page), [], 'Infinity Ward headline is prose on every news surface');

// A real glitch inside a news container is still a glitch. Skipping the
// container would hide it; skipping one headline string would too.
const newsWithStat = el('div', { id: 'newsFeed' }, [
  el('a', {}, [el('div', {}, [headline])]),
  el('span', { id: 'newsStat' }, ['NaN']),
]);
assert.deepEqual(matchesOf(newsWithStat), ['NaN']);
assert.deepEqual(findings(newsWithStat).map((b) => b.where), ['#newsStat']);

// --- metrics that must still fail ---
const metrics = [
  'Infinity',
  '-Infinity',
  '\u2212Infinity',
  'Infinity%',
  '$Infinity',
  '€Infinity',
  'NaN',
  '$NaN',
  'NaN%',
  'Flow NaN%',
  'undefined',
  'null',
  '(Infinity)',
  'score: Infinity',
  'score: NaN',
  '  Infinity  ',
  '\u00a0NaN\u00a0',
  'Infinity bps',
  'NaN px',
  'NaN ms',
  '[object Object]',
  'see [object Object] here',
  '2007 Infinity',
  'Infinity-3',
  '($Infinity)',
];
for (const text of metrics) {
  const got = matchesOf(el('div', {}, [el('span', {}, [text])]));
  assert.ok(got.length > 0, `expected a glitch in ${JSON.stringify(text)}, got none`);
}

const metricBoard = el('div', {}, [
  el('td', {}, ['Infinity']),
  el('td', {}, ['-Infinity']),
  el('td', {}, ['Infinity%']),
  el('td', {}, ['$Infinity']),
  el('td', {}, ['NaN']),
]);
assert.deepEqual(matchesOf(metricBoard), ['Infinity', 'Infinity', 'Infinity', 'Infinity', 'NaN']);

// --- ordinary prose, including the sibling tokens ---
const prose = [
  headline,
  'Publishers Have Pushed to Cut Discs Since 2007, Infinity Ward Veteran Says',
  'undefined behavior in the market',
  'the null hypothesis holds',
  'as of undefined',
  'returned a NaN today',
  'well-Infinity',
  'Infinity-based studio',
  'nullable column',
  'Infinities',
  'to Infinity and beyond',
  'value: Infinity Ward announced a delay',
  // A word on the left and no unit/currency/digit on the right is prose,
  // even when the token is NaN. The glitch shape is the metric itself.
  'Infinity Ward posted a score of NaN',
];
for (const text of prose) {
  assert.deepEqual(matchesOf(el('p', {}, [text])), [], `prose was flagged: ${JSON.stringify(text)}`);
}

// One text node can carry a name and a real glitch. Only the glitch is reported.
assert.deepEqual(
  matchesOf(el('p', {}, ['Infinity Ward posted a score of NaN%'])),
  ['NaN'],
);
assert.deepEqual(
  matchesOf(el('p', {}, ['Call it Infinity Ward, printed $NaN'])),
  ['NaN'],
);

// Non-visible and non-content containers stay quiet, same as before.
assert.deepEqual(matchesOf(el('div', {}, [
  el('span', { style: { visibility: 'hidden' } }, ['Infinity']),
  el('span', { style: { display: 'none' } }, ['NaN']),
  el('span', { style: { opacity: '0' } }, ['undefined']),
  el('span', { style: { box: 'empty' } }, ['null']),
  el('code', {}, ['Infinity']),
  el('pre', {}, ['NaN']),
  el('script', {}, ['undefined']),
  el('style', {}, ['null']),
  el('textarea', {}, ['Infinity']),
  el('noscript', {}, ['NaN']),
  el('template', {}, ['[object Object]']),
])), []);

// where: an id on an ancestor, otherwise the tag.
const located = findings(el('section', { id: 'tab-etf' }, [
  el('span', {}, ['Flow NaN%']),
]));
assert.equal(located.length, 1);
assert.equal(located[0].match, 'NaN');
assert.equal(located[0].where, '#tab-etf');
assert.ok(located[0].text.includes('NaN%'));

const bare = findings(el('strong', {}, ['$Infinity']));
assert.equal(bare[0].where, 'strong');

console.log('daily_audit_ux bad-text probe: ok');
