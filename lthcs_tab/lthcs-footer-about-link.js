// Wire the footer "methodology & sources" link to the About modal.
// Extracted from an inline <script> in lthcs_tab/index.html (2026-05-20) so
// the page can run under a strict CSP with `script-src 'self'` (no
// 'unsafe-inline').
import { openAbout } from './lthcs-about.js';

document
  .getElementById('lthcs-footer-about-link')
  ?.addEventListener('click', openAbout);
