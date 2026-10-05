// Fills the "The 6 bands" score ranges on the help page from the live
// data/lthcs/weights.json `score_bands` (via lthcs-bands.js), so the help
// text tracks a band recalibration. The static HTML ranges are a fallback
// for when the fetch fails. External file to keep the strict CSP
// (`script-src 'self'`, no 'unsafe-inline').
import { bandsReady, bandRangeText } from '../lthcs_tab/lthcs-bands.js';

const UI_TO_KEY = { high: 'high_confidence' };

bandsReady.then(() => {
  document.querySelectorAll('.lhlp-band[data-band]').forEach((node) => {
    const ui = node.getAttribute('data-band');
    const range = bandRangeText(UI_TO_KEY[ui] || ui);
    const out = node.querySelector('.lhlp-band-range');
    if (range && out) out.textContent = range;
  });
});
