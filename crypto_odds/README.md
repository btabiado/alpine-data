# /crypto-odds/

Option-implied (risk-neutral) settlement probabilities for BTC, ETH and XRP, plus a summary of the walk-forward
evaluation. Source research: the `Back_Test_Model` repo (not public).

- `index.html`: the page (static, dark, no external libraries).
- `odds.json`: refreshed daily by `.github/workflows/crypto-odds-daily.yml` via `build_odds.py`.
- `findings.json`: evaluation summary, updated by hand when the research changes.
- `engine/`: vendored copy of the research engine. Do not edit here. Re-vendor with
  `scripts/vendor_to_alpine.sh <this checkout>` from the research repo. `engine/VENDORED_FROM` holds the source commit.
