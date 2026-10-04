// lthcs-freshness.js
// ---------------------------------------------------------------------------
// Shared data-freshness stamps for every LTHCS page.
//
// This is a PORT, not a new dialect. The reference implementation lives in
// v2/app.py (`freshness` / `freshnessDayUTC` / `freshnessYmd` / `fDay` /
// `fLast` / `fMin` / `fMax`) and V1 app.py carries a byte-equivalent copy.
// The thresholds, the "as of <YYYY-MM-DD> (Nd ago)" wording, the "as of —"
// unavailable state, the UTC-midnight age arithmetic, the future-date floor,
// and the "· N of M cached" stale suffix are all identical here. If you change
// a rule, change it in all three places or the frontends drift apart.
//
// THE HONESTY CONTRACT
//
//  1. A stamp reports the age of the DATA, never the BUILD and never the
//     FETCH. `new Date()` appears exactly once below — to compute *today* so
//     an age can be subtracted from it. It is never rendered as a date.
//
//  2. A composite of N inputs is only as fresh as its OLDEST input. Use
//     `fMin` / `composite()`, never `fMax`. `fMax` exists only for
//     "newest item in a feed" surfaces, of which LTHCS has none.
//
//  3. Entries flagged stale / carried forward / dropped must be COUNTED and
//     DISCLOSED next to the date. A bare date is still a partial lie: the
//     fresh rows keep the headline date current while the stale ones
//     silently freeze their contribution.
//
//  4. No honest date ⇒ an explicit unavailable state ("as of —"), never a
//     silent fallback to build/fetch/clock time.
//
//  5. Age is computed and TINTED, not printed bare.
//
// `freshness()` is the single source of truth for text + tone; every other
// function here just decides WHICH date to feed it.
// ---------------------------------------------------------------------------

'use strict';

// isoDate: 'YYYY-MM-DD' or ISO datetime or null/undefined
// opts: {warnDays=7, badDays=21, label='as of', stale=null, total=null,
//        staleNoun='cached'}
// returns {text, tone, ageDays}   tone in 'ok'|'warn'|'bad'|'none'
//
// `staleNoun` is the only addition over the v2/app.py signature and it
// defaults to 'cached', so an unset call is byte-for-byte identical to the
// reference. LTHCS needs it because its stale entries are *dropped pillars*,
// not cached quotes — calling them "cached" would be its own small lie.
export function freshness(isoDate, opts) {
  const o = opts || {};
  const warnDays = (typeof o.warnDays === 'number' && isFinite(o.warnDays)) ? o.warnDays : 7;
  const badDays = (typeof o.badDays === 'number' && isFinite(o.badDays)) ? o.badDays : 21;
  const label = (o.label == null) ? 'as of' : String(o.label);
  const dayMs = freshnessDayUTC(isoDate);
  if (dayMs == null) return { text: label + ' —', tone: 'none', ageDays: null };
  // Both operands are midnight UTC, so the difference is a whole number of
  // days no matter what timezone the viewer is in. Computing from local
  // midnight (or from Date.now() directly) is what produces "-1d ago" for
  // anyone east of UTC in the early hours.
  const now = new Date();
  const todayMs = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  let ageDays = Math.round((todayMs - dayMs) / 86400000);
  if (!isFinite(ageDays)) return { text: label + ' —', tone: 'none', ageDays: null };
  // A future-dated observation is a data bug, not negative age. Floor at 0
  // so no stamp can ever read "(-1d ago)".
  if (ageDays < 0) ageDays = 0;
  const tone = ageDays <= warnDays ? 'ok'
    : ageDays <= badDays ? 'warn'
      : 'bad';
  let text = label + ' ' + freshnessYmd(dayMs) + ' (' + ageDays + 'd ago)';
  const staleN = Number(o.stale);
  if (isFinite(staleN) && staleN > 0) {
    const noun = (o.staleNoun == null) ? 'cached' : String(o.staleNoun);
    const totalN = Number(o.total);
    text += (isFinite(totalN) && totalN > 0)
      ? ' · ' + staleN + ' of ' + totalN + ' ' + noun
      : ' · ' + staleN + ' ' + noun;
  }
  return { text: text, tone: tone, ageDays: ageDays };
}

// Parse an ISO date / datetime to midnight-UTC epoch ms. null when the
// input is missing or not a real calendar date. Rejects rollovers
// ('2026-02-31' → Date.UTC gives Mar 3) by round-tripping the components,
// so a malformed date renders "unavailable" rather than a wrong day.
export function freshnessDayUTC(isoDate) {
  if (isoDate == null) return null;
  const s = String(isoDate).trim();
  if (!s) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s);
  if (m) {
    const y = Number(m[1]), mo = Number(m[2]), d = Number(m[3]);
    if (!(mo >= 1 && mo <= 12) || !(d >= 1 && d <= 31)) return null;
    const t = Date.UTC(y, mo - 1, d);
    if (!isFinite(t)) return null;
    const chk = new Date(t);
    if (chk.getUTCFullYear() !== y || chk.getUTCMonth() !== mo - 1 || chk.getUTCDate() !== d) return null;
    return t;
  }
  const parsed = Date.parse(s);
  if (!isFinite(parsed)) return null;
  const dt = new Date(parsed);
  return Date.UTC(dt.getUTCFullYear(), dt.getUTCMonth(), dt.getUTCDate());
}

export function freshnessYmd(dayMs) {
  const d = new Date(dayMs);
  const p2 = (n) => (n < 10 ? '0' : '') + n;
  return d.getUTCFullYear() + '-' + p2(d.getUTCMonth() + 1) + '-' + p2(d.getUTCDate());
}

// --- date plumbing ---------------------------------------------------------
// fDay: normalise anything date-ish to 'YYYY-MM-DD' (or null). Everything
// below compares normalised strings, which sorts correctly by calendar day.
export function fDay(v) {
  const ms = freshnessDayUTC(v);
  return ms == null ? null : freshnessYmd(ms);
}

// Last usable observation date in a [{date, ...}] series. Scans BACKWARDS so
// a trailing null-dated point can't blank the answer. `key` defaults to 'date'.
export function fLast(series, key) {
  if (!Array.isArray(series)) return null;
  const k = key || 'date';
  for (let i = series.length - 1; i >= 0; i--) {
    const d = fDay(series[i] && series[i][k]);
    if (d) return d;
  }
  return null;
}

// OLDEST of a set of dates — the composite-freshness rule. Nulls dropped;
// null when nothing usable is left.
export function fMin(dates) {
  let out = null;
  (dates || []).forEach((v) => {
    const d = fDay(v);
    if (d && (out === null || d < out)) out = d;
  });
  return out;
}

// NEWEST of a set of dates. Only correct for "latest item in a feed"
// surfaces (news, advisories) — never for a composite index. Exported for
// parity with the reference family; LTHCS has no legitimate caller.
export function fMax(dates) {
  let out = null;
  (dates || []).forEach((v) => {
    const d = fDay(v);
    if (d && (out === null || d > out)) out = d;
  });
  return out;
}

export function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// --- composite resolution --------------------------------------------------
// Rule 2 made concrete. `components` is [{label, date, note, contributes}] —
// one entry per input behind the number on screen. Returns the OLDEST
// contributing date plus everything a caller needs to disclose disagreement:
//
//   { date, oldest, newest, spreadDays, missing, total, rows, detail }
//
// `date` is null when NO component carried a usable date → the caller renders
// "as of —". A component present in the list but with date:null is counted in
// `missing` and disclosed, never silently dropped: an input whose date we
// cannot read is a *worse* case than an old one, not a better one.
//
// `contributes: false` marks an input that is ON THE PAGE'S SHOPPING LIST but
// does NOT move today's number. It is kept in `rows` so its real date is still
// rendered, but excluded from `fMin`. There are two very different reasons an
// input lands here, and conflating them would itself be misleading:
//
//   severe: true   The input SHOULD have counted and did not — LTHCS's thesis
//                  pillar, which the daily pipeline drops to effective weight
//                  0.0 when its sentiment inputs are unavailable. Folding its
//                  ancient date into the headline would overstate the
//                  composite's age exactly as badly as hiding it understates
//                  it; both are lies, so it is disclosed instead, tinted bad,
//                  and it forces the breakdown row open.
//
//   severe: false  The input was never meant to age the number — a reference
//                  roster, an optional pipeline stage, a user-chosen diff
//                  baseline. Shown for context, tinted by its own real age,
//                  and it does not on its own force the breakdown open.
//
// `tag` is the short word rendered after the date ("dropped", "ref",
// "baseline", …). It defaults to "dropped" only when the input is severe,
// because calling a reference roster "dropped" would be its own small lie.
export function composite(components) {
  const list = Array.isArray(components) ? components.filter(Boolean) : [];
  const rows = list.map((c) => {
    const contributes = c.contributes !== false;
    const severe = c.severe === true;
    return {
      label: String(c.label == null ? '?' : c.label),
      date: fDay(c.date),
      note: c.note == null ? '' : String(c.note),
      contributes,
      severe,
      tag: c.tag != null ? String(c.tag) : (severe ? 'dropped' : ''),
    };
  });
  const dated = rows.filter((r) => r.date);
  const weighted = dated.filter((r) => r.contributes);
  const oldest = fMin(weighted.map((r) => r.date));
  const newest = fMax(weighted.map((r) => r.date));
  let spreadDays = null;
  if (oldest && newest) {
    spreadDays = Math.round((freshnessDayUTC(newest) - freshnessDayUTC(oldest)) / 86400000);
  }
  // Only weighted inputs can be "missing" in the sense that matters: a
  // zero-weight input with no date changes nothing about today's number.
  const missing = rows.filter((r) => r.contributes && !r.date).length;
  // Sort the disclosure oldest-first: the thing dragging the stamp down is
  // the thing a reader needs to see, so it goes at the top.
  const sorted = rows.slice().sort((a, b) => {
    if (!a.date) return -1;
    if (!b.date) return 1;
    return a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
  });
  const detail = sorted
    .map((r) => r.label + ': ' + (r.date || 'no date')
      + (r.contributes ? '' : ' [does not age the stamp]')
      + (r.note ? ' (' + r.note + ')' : ''))
    .join('\n');
  return {
    date: oldest,
    oldest: oldest,
    newest: newest,
    spreadDays: spreadDays,
    missing: missing,
    total: rows.length,
    rows: sorted,
    detail: detail,
  };
}

// --- DOM writers -----------------------------------------------------------
// Tone arrives as a CSS class (never an inline colour) so the tints stay
// anchored to the LTHCS design tokens in lthcs.css.
// opts.baseClass preserves an existing utility class on the element.
// opts.title sets the hover detail.
export function paintFreshness(el, isoDate, opts) {
  if (!el) return null;
  const o = opts || {};
  const f = freshness(isoDate, o);
  el.textContent = f.text;
  const base = (o.baseClass == null) ? 'lthcs-meta-value' : String(o.baseClass);
  el.className = (base ? base + ' ' : '') + 'lthcs-fresh lthcs-fresh--' + f.tone;
  el.style.color = '';
  if (o.title) el.setAttribute('title', o.title); else el.removeAttribute('title');
  return f;
}

// String variant of paintFreshness for renderers that build markup with
// innerHTML. Same helper, same thresholds, same tint classes.
export function freshnessHtml(isoDate, opts) {
  const o = opts || {};
  const f = freshness(isoDate, o);
  const title = o.title
    ? ' title="' + escapeHtml(o.title + ' Not the page build time.') + '"'
    : '';
  return '<div class="lthcs-fresh lthcs-fresh--' + f.tone + '"' + title + '>'
    + escapeHtml(f.text) + '</div>';
}

// Composite stamp + disagreement disclosure in one call.
//
//   el         the element that carries the headline stamp
//   components [{label, date, note}] — every contributing input
//   opts       passed through to freshness() (label / stale / total / …),
//              plus:
//                spreadWarnDays  surface the per-component breakdown once the
//                                oldest and newest inputs differ by more than
//                                this many days (default 3)
//                detailEl        element to receive the breakdown text; when
//                                omitted only the hover title carries it
//                what            noun for the composite, used in the title
//
// Returns the freshness() result extended with the composite() fields.
export function paintComposite(el, components, opts) {
  const o = opts || {};
  const c = composite(components);
  const spreadWarnDays = (typeof o.spreadWarnDays === 'number') ? o.spreadWarnDays : 3;
  const what = o.what || 'This composite';
  const titleLines = [
    what + ' is stamped with its OLDEST contributing input, not its newest.',
    '',
    c.detail,
  ];
  if (c.missing > 0) {
    titleLines.push('', c.missing + ' of ' + c.total + ' inputs carry no readable date.');
  }
  // A caller-supplied title is extra context, not a replacement — appending
  // keeps the per-component breakdown reachable on every surface.
  if (o.title) titleLines.push('', String(o.title));
  titleLines.push('', 'This is the age of the DATA, not of the page build.');
  const f = paintFreshness(el, c.date, Object.assign({}, o, { title: titleLines.join('\n') }));

  // Surface the breakdown only when it says something the headline cannot:
  // the inputs disagree, a weighted input has no readable date, an input that
  // should have counted was dropped, or an optional input produced nothing.
  // A stale COUNT alone does not open the row — that number is already in the
  // headline, so repeating it under a one-pill list would be pure noise.
  const dropped = c.rows.filter((r) => r.severe);
  const absentOptional = c.rows.filter((r) => !r.contributes && !r.severe && !r.date);
  const disagrees = (c.spreadDays != null && c.spreadDays > spreadWarnDays)
    || c.missing > 0
    || dropped.length > 0
    || absentOptional.length > 0
    || o.forceDetail === true;
  if (o.detailEl) {
    if (!disagrees) {
      o.detailEl.textContent = '';
      o.detailEl.className = 'lthcs-freshnote';
      o.detailEl.hidden = true;
    } else {
      const parts = c.rows.map((r) => {
        // A severely-excluded input is always tinted bad regardless of its
        // age: "this should have counted and did not" is the worst state
        // there is. Everything else is tinted by its own real age.
        const tone = r.severe ? 'bad' : (r.date ? freshness(r.date, o).tone : 'none');
        const suffix = r.tag ? ' ' + r.tag : '';
        return '<span class="lthcs-freshnote__pill lthcs-fresh--' + tone + '">'
          + escapeHtml(r.label) + ' ' + escapeHtml(r.date || '—')
          + escapeHtml(suffix) + '</span>';
      });
      const leads = [];
      if (c.spreadDays != null && c.spreadDays > spreadWarnDays) {
        leads.push('inputs span ' + c.spreadDays + 'd');
      }
      if (c.missing > 0) leads.push(c.missing + ' undated');
      if (dropped.length > 0) leads.push(dropped.length + ' dropped');
      if (absentOptional.length > 0) leads.push(absentOptional.length + ' not produced');
      if (!leads.length) leads.push('inputs');
      o.detailEl.innerHTML = '<span class="lthcs-freshnote__lead">'
        + escapeHtml(leads.join(' · ')) + '</span>' + parts.join('');
      o.detailEl.className = 'lthcs-freshnote';
      o.detailEl.hidden = false;
    }
  }
  return Object.assign({}, f, c);
}
