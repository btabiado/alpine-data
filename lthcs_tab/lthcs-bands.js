// lthcs-bands.js
// Single front-end source for LTHCS score-band cutoffs.
//
// The live cutoffs are data/lthcs/weights.json -> score_bands (integer,
// inclusive min/max). This module fetches that file once on import and
// exposes synchronous helpers that read the loaded config, falling back to
// DEFAULT_SCORE_BANDS until (or if) the fetch completes. A band
// recalibration (scripts/lthcs_calibrate_bands.py --write) therefore
// re-tints every legend, chart guide and color without editing JS.
//
// Usage:
//   import { bandsReady, bandKeyForScore, bandList } from './lthcs-bands.js';
//   await bandsReady;            // optional: wait for weights.json
//   bandKeyForScore(57.4)        // -> 'constructive' (snapshot key, live bands)

'use strict';

export const BAND_ORDER = [
  'elite', 'high_confidence', 'constructive', 'monitor', 'weakening', 'review',
];

// Fallback only (used until / unless weights.json loads). Mirrors
// weights.json score_bands as calibrated 2026-10-06; a test
// (tests/lthcs/test_calibrate_bands.py) keeps the two equal, so update this
// together with a recalibration.
export const DEFAULT_SCORE_BANDS = {
  elite:           { min: 70, max: 100, color: '#1F3A5F', label: 'Elite Confidence Hold' },
  high_confidence: { min: 63, max: 69,  color: '#4A8F5F', label: 'High Confidence Hold' },
  constructive:    { min: 52, max: 62,  color: '#C9A227', label: 'Constructive Hold' },
  monitor:         { min: 42, max: 51,  color: '#D89148', label: 'Monitor Closely' },
  weakening:       { min: 33, max: 41,  color: '#B85A3E', label: 'Confidence Weakening' },
  review:          { min: 0,  max: 32,  color: '#7A2E1F', label: 'Structural Review Required' },
};

let current = DEFAULT_SCORE_BANDS;

function sane(bands) {
  if (!bands || typeof bands !== 'object') return false;
  return BAND_ORDER.every((k) => bands[k]
    && Number.isFinite(Number(bands[k].min)) && Number.isFinite(Number(bands[k].max)));
}

const WEIGHTS_URL = new URL('../data/lthcs/weights.json', import.meta.url).href;

/** Resolves (never rejects) once weights.json has been tried. */
export const bandsReady = (typeof fetch === 'function'
  ? fetch(WEIGHTS_URL, { cache: 'no-cache' })
      .then((r) => (r.ok ? r.json() : null))
      .then((cfg) => {
        const b = cfg && cfg.score_bands;
        if (sane(b)) {
          const out = {};
          for (const k of BAND_ORDER) {
            out[k] = { ...DEFAULT_SCORE_BANDS[k], ...b[k], min: Number(b[k].min), max: Number(b[k].max) };
          }
          current = out;
        }
        return current;
      })
      .catch(() => current)
  : Promise.resolve(current));

/** Current band config ({key: {min, max, color, label}}). */
export function scoreBands() {
  return current;
}

/** [{key, min, max, color, label}] highest band first. */
export function bandList() {
  return BAND_ORDER.map((key) => ({ key, ...current[key] }));
}

/**
 * Snapshot band key for a composite score. Matches lthcs.score.assign_band:
 * the score is floored and looked up against inclusive integer ranges.
 */
export function bandKeyForScore(score) {
  if (typeof score !== 'number' || !Number.isFinite(score)) return null;
  const f = Math.floor(Math.max(0, Math.min(100, score)));
  for (const key of BAND_ORDER) {
    const b = current[key];
    if (f >= b.min && f <= b.max) return key;
  }
  return 'review';
}

/** Lower cutoffs of every band above review, ascending (for chart guides). */
export function bandCutoffs() {
  return BAND_ORDER.slice(0, -1).map((k) => current[k].min).sort((a, b) => a - b);
}

/** "52–62" style range string for a band key. */
export function bandRangeText(key, dash = '–') {
  const b = current[key];
  return b ? `${b.min}${dash}${b.max}` : '';
}
