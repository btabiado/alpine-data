#!/usr/bin/env node
// daily_audit_ux.mjs — the UX half of the daily audit (.github/workflows/daily-audit.yml).
//
// Loads the public dashboard pages in a real browser at a phone (390px) and a
// desktop (1440px) viewport and records what a visitor would actually hit:
//
//   * console errors and uncaught exceptions (attributed to the step that
//     raised them: initial load, or the tab that was being opened)
//   * failed SAME-ORIGIN requests (HTTP 4xx/5xx and network failures)
//   * horizontal overflow at 390px (documentElement.scrollWidth > viewport),
//     with the outermost offending elements named
//   * visible text matching NaN / undefined / [object Object] / Infinity
//   * page weight (encoded bytes) and request count for the initial load
//   * for V1 (/): every top-level tab is opened the way a user
//     would — on the phone by tapping the dropdown button first, then tapping
//     the item. Before each tap the item's centre is hit-tested with
//     document.elementFromPoint; if something else is on top, the tab is
//     reported UNREACHABLE (and then opened programmatically anyway, so its
//     content is still audited for errors).
//
// Output: one JSON document (see --out). The report builder
// (scripts/daily_audit_report.py) turns it into problems + regressions; this
// script only observes and never decides severity.
//
// It always exits 0 once it has written its JSON — including when the browser
// cannot start (the JSON then carries `fatal`). A crashed audit must surface
// in the report, not as a red step that skips the report.
//
// Usage:
//   node scripts/daily_audit_ux.mjs --out ux.json [--base URL] [--budget-s 540]
//
// Environment:
//   PLAYWRIGHT_MODULE  path to playwright's index.mjs (set by the workflow)
//   PW_CHROMIUM_ARGS   extra Chromium flags, whitespace separated (e.g. to
//                      trust a local TLS-intercepting proxy's CA). Unset in CI.

import fs from 'node:fs';
import path from 'node:path';
import { execSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const DEFAULT_BASE = 'https://btabiado.github.io/alpine-data';

export const PAGES = [
  { key: 'v1', path: '/', tabs: true },
  { key: 'summit', path: '/summit/' },
  { key: 'health', path: '/health/' },
  { key: 'real-estate', path: '/real-estate/' },
  { key: 'lthcs', path: '/lthcs/' },
];

export const VIEWPORTS = [
  { key: 'phone', width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
  { key: 'desktop', width: 1440, height: 900, isMobile: false, hasTouch: false, deviceScaleFactor: 1 },
];

const NAV_TIMEOUT_MS = 45_000;
const IDLE_WAIT_MS = 8_000;       // best-effort network-idle after load
const TAB_SETTLE_MS = 2_500;      // best-effort network-idle after a tab opens
const MAX_LIST = 15;              // cap on any per-page list in the output

function parseArgs(argv) {
  const out = { base: DEFAULT_BASE, out: null, budgetS: 540 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--base') out.base = argv[++i];
    else if (a === '--out') out.out = argv[++i];
    else if (a === '--budget-s') out.budgetS = Number(argv[++i]);
  }
  out.base = out.base.replace(/\/+$/, '');
  return out;
}

// Resolution order: $PLAYWRIGHT_MODULE (path to playwright/index.mjs — what
// the workflow sets, so nothing is installed into the repo), a resolvable
// `playwright` package, then a global npm install.
async function loadPlaywright() {
  if (process.env.PLAYWRIGHT_MODULE) {
    return await import(pathToFileURL(path.resolve(process.env.PLAYWRIGHT_MODULE)).href);
  }
  try {
    return await import('playwright');
  } catch (_) { /* fall through to a global install */ }
  const root = execSync('npm root -g', { encoding: 'utf8' }).trim();
  return await import(pathToFileURL(path.join(root, 'playwright', 'index.mjs')).href);
}

const clip = (s, n = 300) => {
  s = String(s ?? '');
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
};

function pushCapped(list, item) {
  if (list.length < MAX_LIST) list.push(item);
}

// ---------------------------------------------------------------------------
// In-page probes (run via page.evaluate; must be self-contained)
// ---------------------------------------------------------------------------

function probeBadText(rootSelector) {
  const re = /\bNaN\b|\bundefined\b|\[object Object\]|\bInfinity\b/;
  const root = rootSelector ? document.querySelector(rootSelector) : document.body;
  if (!root) return [];
  const out = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode()) && out.length < 15) {
    const text = node.nodeValue;
    if (!text || !re.test(text)) continue;
    const el = node.parentElement;
    if (!el || el.closest('script,style,noscript,template,textarea,code,pre')) continue;
    const rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
    const m = text.match(re);
    const start = Math.max(0, m.index - 40);
    const snippet = text.slice(start, m.index + m[0].length + 40).replace(/\s+/g, ' ').trim();
    const anchor = el.closest('[id]');
    out.push({ match: m[0], text: snippet, where: anchor ? '#' + anchor.id : el.tagName.toLowerCase() });
  }
  return out;
}

function probeOverflow(vw) {
  const describe = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (typeof el.className === 'string' ? el.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
    if (cls.length) s += '.' + cls.join('.');
    return s;
  };
  const limit = Math.min(window.innerWidth, vw);
  const sw = Math.max(document.documentElement.scrollWidth, document.body ? document.body.scrollWidth : 0);
  const overflow = sw > limit + 1;
  const offenders = [];
  if (overflow && document.body) {
    const all = document.body.querySelectorAll('*');
    const n = Math.min(all.length, 40000);
    for (let i = 0; i < n && offenders.length < 5; i++) {
      const el = all[i];
      const r = el.getBoundingClientRect();
      if (!r.width || r.right <= limit + 1) continue;
      if (offenders.some((o) => o.el.contains(el))) continue;
      let p = el.parentElement;
      let contained = false;
      while (p && p !== document.body && p !== document.documentElement) {
        if (/(auto|scroll|hidden|clip)/.test(getComputedStyle(p).overflowX)) { contained = true; break; }
        p = p.parentElement;
      }
      if (contained) continue;
      if (getComputedStyle(el).position === 'fixed' && r.left >= limit) continue; // off-canvas drawer
      offenders.push({ el, right: Math.round(r.right) });
    }
  }
  return {
    viewport: limit,
    scroll_width: sw,
    overflow,
    offenders: offenders.map((o) => ({ element: describe(o.el), right: o.right })),
  };
}

function probeTabs() {
  return [...document.querySelectorAll('.tabs [data-tab]')].map((el) => ({
    id: el.dataset.tab,
    label: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 40),
    exit: el.classList.contains('tab--exit') || el.getAttribute('role') === 'link',
  }));
}

// Geometry + hit test for one tab. Returns where its centre is and what the
// browser would actually deliver a tap to there.
function probeTabHit(id) {
  const el = document.querySelector(`.tabs [data-tab="${CSS.escape(id)}"]`);
  if (!el) return { exists: false };
  const r = el.getBoundingClientRect();
  const visible = r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  const inViewport = cx >= 0 && cy >= 0 && cx <= window.innerWidth && cy <= window.innerHeight;
  let hit = null;
  let ok = false;
  if (visible && inViewport) {
    const h = document.elementFromPoint(cx, cy);
    ok = !!h && (h === el || el.contains(h));
    if (h && !ok) {
      let s = h.tagName.toLowerCase();
      if (h.id) s += '#' + h.id;
      const cls = (typeof h.className === 'string' ? h.className : '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
      if (cls.length) s += '.' + cls.join('.');
      hit = s;
    }
  }
  // The dropdown button that reveals this tab, if it lives in a menu.
  const group = el.closest('.tabgroup');
  let opener = group ? group.querySelector('.tabgroup-btn, [aria-haspopup]') : null;
  if (!opener) {
    // Generic fallback: the nearest ancestor (inside .tabs) that also holds a
    // popup button.
    let p = el.parentElement;
    while (p && !p.classList.contains('tabs')) {
      const b = p.querySelector(':scope > [aria-haspopup], :scope > [aria-expanded]');
      if (b && !b.contains(el)) { opener = b; break; }
      p = p.parentElement;
    }
  }
  return { exists: true, visible, inViewport, x: cx, y: cy, hitOk: ok, coveredBy: hit, hasOpener: !!opener };
}

function openerCenter(id) {
  const el = document.querySelector(`.tabs [data-tab="${CSS.escape(id)}"]`);
  if (!el) return null;
  const group = el.closest('.tabgroup');
  let opener = group ? group.querySelector('.tabgroup-btn, [aria-haspopup]') : null;
  if (!opener) {
    let p = el.parentElement;
    while (p && !p.classList.contains('tabs')) {
      const b = p.querySelector(':scope > [aria-haspopup], :scope > [aria-expanded]');
      if (b && !b.contains(el)) { opener = b; break; }
      p = p.parentElement;
    }
  }
  if (!opener) return null;
  opener.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  const r = opener.getBoundingClientRect();
  const x = r.left + r.width / 2;
  const y = r.top + r.height / 2;
  const h = document.elementFromPoint(x, y);
  return { x, y, ok: !!h && (h === opener || opener.contains(h)), expanded: opener.getAttribute('aria-expanded') };
}

function scrollTabIntoView(id) {
  const el = document.querySelector(`.tabs [data-tab="${CSS.escape(id)}"]`);
  if (el) el.scrollIntoView({ block: 'nearest', inline: 'center' });
}

function activateTabProgrammatically(id) {
  const el = document.querySelector(`.tabs [data-tab="${CSS.escape(id)}"]`);
  if (el) el.click();
}

function probeTabActive(id) {
  const el = document.querySelector(`.tabs [data-tab="${CSS.escape(id)}"]`);
  if (!el) return { active: false, panel: null };
  const active = el.classList.contains('active') || el.getAttribute('aria-selected') === 'true';
  const panel = document.getElementById('tab-' + id);
  let panelVisible = null;
  if (panel) panelVisible = panel.getClientRects().length > 0 && getComputedStyle(panel).display !== 'none';
  return { active, panel: panel ? '#tab-' + id : null, panelVisible };
}

// ---------------------------------------------------------------------------
// One page at one viewport
// ---------------------------------------------------------------------------

async function settle(page, ms) {
  try {
    await page.waitForLoadState('networkidle', { timeout: ms });
  } catch (_) { /* best effort: long-polling pages never go idle */ }
}

async function tapAt(page, vp, x, y) {
  if (vp.hasTouch) await page.touchscreen.tap(x, y);
  else await page.mouse.click(x, y);
}

async function auditTabs(page, vp, rec, state, deadline) {
  const tabs = await page.evaluate(probeTabs);
  rec.tabs = [];
  for (const t of tabs) {
    const entry = { id: t.id, label: t.label };
    rec.tabs.push(entry);
    if (t.exit) { entry.skipped = 'navigates away from the dashboard'; continue; }
    if (Date.now() > deadline) { entry.skipped = 'time budget exhausted'; continue; }
    state.where = `tab:${t.id}`;
    try {
      await page.keyboard.press('Escape').catch(() => {});
      let hit = await page.evaluate(probeTabHit, t.id);
      if (!hit.exists) { entry.reachable = false; entry.problem = 'tab element disappeared'; continue; }
      entry.via = 'direct';
      if (!hit.visible && hit.hasOpener) {
        entry.via = 'menu';
        const op = await page.evaluate(openerCenter, t.id);
        if (op) {
          if (!op.ok) entry.opener_covered = true;
          await tapAt(page, vp, op.x, op.y);
          // Wait (briefly) for the menu item to get a box.
          const t0 = Date.now();
          do {
            await page.waitForTimeout(120);
            hit = await page.evaluate(probeTabHit, t.id);
          } while (!hit.visible && Date.now() - t0 < 1500);
        }
      }
      if (hit.visible && !hit.inViewport) {
        entry.via = entry.via === 'menu' ? 'menu' : 'scroll';
        await page.evaluate(scrollTabIntoView, t.id);
        await page.waitForTimeout(150);
        hit = await page.evaluate(probeTabHit, t.id);
      }
      entry.reachable = !!(hit.visible && hit.inViewport && hit.hitOk);
      if (!entry.reachable) {
        if (!hit.visible) entry.problem = entry.via === 'menu' ? 'menu item not visible after opening its menu' : 'tab not visible';
        else if (!hit.inViewport) entry.problem = 'tab outside the viewport';
        else entry.problem = 'tap target covered';
        if (hit.coveredBy) entry.covered_by = hit.coveredBy;
        // Still open it (programmatically) so the content gets audited.
        await page.evaluate(activateTabProgrammatically, t.id);
        entry.opened = 'programmatic-fallback';
      } else {
        await tapAt(page, vp, hit.x, hit.y);
        entry.opened = 'tap';
      }
      await page.waitForTimeout(300);
      await settle(page, TAB_SETTLE_MS);
      const act = await page.evaluate(probeTabActive, t.id);
      entry.switched = act.active && act.panelVisible !== false;
      if (act.panel) entry.panel = act.panel;
      const bad = await page.evaluate(probeBadText, act.panelVisible ? act.panel : null);
      for (const b of bad) {
        const key = b.match + '|' + b.where + '|' + b.text;
        if (!state.badSeen.has(key)) { state.badSeen.add(key); pushCapped(rec.bad_text, { ...b, step: state.where }); }
      }
      if (vp.key === 'phone') {
        const ov = await page.evaluate(probeOverflow, vp.width);
        if (ov.overflow) { entry.overflow = ov; }
      }
    } catch (e) {
      entry.error = clip(e && e.message, 200);
    }
  }
  state.where = 'after-tabs';
}

async function auditPage(browser, base, pg, vp, deadline) {
  const url = base + pg.path;
  const origin = new URL(base).origin;
  const rec = {
    page: pg.key, path: pg.path, viewport: vp.key, url,
    console_errors: [], page_errors: [], failed_requests: [], bad_text: [],
  };
  const state = { where: 'load', badSeen: new Set(), requests: 0, bytes: 0, sizePromises: [], largest: [] };
  const ctx = await browser.newContext({
    viewport: { width: vp.width, height: vp.height },
    isMobile: vp.isMobile, hasTouch: vp.hasTouch, deviceScaleFactor: vp.deviceScaleFactor,
    userAgent: vp.isMobile
      ? 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1 alpine-data-daily-audit'
      : undefined,
  });
  const page = await ctx.newPage();
  const sameOrigin = (u) => { try { return new URL(u).origin === origin; } catch (_) { return false; } };

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    // Same-origin HTTP failures are recorded (with URL + status) below;
    // third-party resource failures are out of scope.
    if (/^Failed to load resource/.test(text)) return;
    pushCapped(rec.console_errors, { text: clip(text), step: state.where });
  });
  page.on('pageerror', (err) => {
    pushCapped(rec.page_errors, { text: clip(err && (err.message || String(err))), step: state.where });
  });
  page.on('response', (resp) => {
    const st = resp.status();
    if (st >= 400 && sameOrigin(resp.url())) {
      pushCapped(rec.failed_requests, { url: resp.url(), status: st, step: state.where });
    }
  });
  page.on('requestfailed', (req) => {
    const f = req.failure();
    const why = f ? f.errorText : 'failed';
    if (/ERR_ABORTED/.test(why) || !sameOrigin(req.url())) return;
    pushCapped(rec.failed_requests, { url: req.url(), status: null, error: why, step: state.where });
  });
  page.on('requestfinished', (req) => {
    state.requests += 1;
    const p = req.sizes().then((s) => {
      const b = (s.responseBodySize || 0) + (s.responseHeadersSize || 0);
      state.bytes += b;
      state.largest.push({ url: req.url(), bytes: b });
    }).catch(() => {});
    state.sizePromises.push(p);
  });

  try {
    const t0 = Date.now();
    let resp = await page.goto(url, { waitUntil: 'load', timeout: NAV_TIMEOUT_MS });
    if (resp && resp.status() >= 500) {
      // GitHub Pages hands out the odd transient 503. One retry, recorded, so
      // a blip is visible in the JSON but does not read as an outage.
      rec.retried_after = resp.status();
      rec.failed_requests = rec.failed_requests.filter((f) => f.url !== url);
      await page.waitForTimeout(5000);
      resp = await page.goto(url, { waitUntil: 'load', timeout: NAV_TIMEOUT_MS });
    }
    rec.status = resp ? resp.status() : null;
    rec.load_ms = Date.now() - t0;
    await settle(page, IDLE_WAIT_MS);
    await Promise.allSettled(state.sizePromises);
    state.largest.sort((a, b) => b.bytes - a.bytes);
    rec.weight = {
      requests: state.requests,
      bytes: state.bytes,
      largest: state.largest.slice(0, 3),
    };
    rec.overflow = await page.evaluate(probeOverflow, vp.width);
    for (const b of await page.evaluate(probeBadText, null)) {
      const key = b.match + '|' + b.where + '|' + b.text;
      if (!state.badSeen.has(key)) { state.badSeen.add(key); pushCapped(rec.bad_text, { ...b, step: 'load' }); }
    }
    if (pg.tabs) await auditTabs(page, vp, rec, state, deadline);
  } catch (e) {
    rec.error = clip(e && e.message, 300);
  } finally {
    await Promise.allSettled(state.sizePromises);
    rec.total = { requests: state.requests, bytes: state.bytes };
    await ctx.close().catch(() => {});
  }
  return rec;
}

// ---------------------------------------------------------------------------

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const started = new Date();
  const deadline = Date.now() + args.budgetS * 1000;
  const result = {
    kind: 'ux',
    base: args.base,
    started_at: started.toISOString(),
    budget_s: args.budgetS,
    viewports: VIEWPORTS.map((v) => ({ key: v.key, width: v.width, height: v.height })),
    pages: [],
  };
  let browser = null;
  try {
    const { chromium } = await loadPlaywright();
    const extra = (process.env.PW_CHROMIUM_ARGS || '').split(/\s+/).filter(Boolean);
    browser = await chromium.launch({ args: extra });
    for (const pg of PAGES) {
      for (const vp of VIEWPORTS) {
        if (Date.now() > deadline) {
          result.pages.push({ page: pg.key, path: pg.path, viewport: vp.key, skipped: 'time budget exhausted' });
          continue;
        }
        process.stderr.write(`[ux] ${pg.path} @ ${vp.key}\n`);
        result.pages.push(await auditPage(browser, args.base, pg, vp, deadline));
      }
    }
  } catch (e) {
    result.fatal = clip(e && (e.stack || e.message), 600);
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  result.finished_at = new Date().toISOString();
  result.duration_s = Math.round((Date.now() - started.getTime()) / 1000);
  const json = JSON.stringify(result, null, 2);
  if (args.out) {
    fs.mkdirSync(path.dirname(path.resolve(args.out)), { recursive: true });
    fs.writeFileSync(args.out, json + '\n');
  } else {
    process.stdout.write(json + '\n');
  }
}

main();
