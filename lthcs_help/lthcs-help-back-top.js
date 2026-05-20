// Tiny UI affordance: show the back-to-top button after the user scrolls
// past ~600px. Extracted from an inline <script> in lthcs_help/index.html
// (2026-05-20) so the page can run under a strict CSP with
// `script-src 'self'` (no 'unsafe-inline').
//
// No data, no fetches, no state.
(function () {
  var btn = document.getElementById('lhlp-back-top');
  if (!btn) return;
  var onScroll = function () {
    if (window.scrollY > 600) {
      btn.classList.add('is-visible');
    } else {
      btn.classList.remove('is-visible');
    }
  };
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
})();
