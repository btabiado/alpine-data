# LTHCS test suite — performance notes

Snapshot: 2026-05-19 (post swarm-test-perf2 trim). Suite runs
**1739 passed + 1 skipped in ~32s** on a quiet machine.

## Root cause of the per-test creep

The pre-swarm 32s baseline was real. The 60s number Bryan was seeing
came from `tests/lthcs/test_daily.py::patched_sources` falling further
and further behind production: every time a Tier 2/3/5 batch added a new
network source to `stage_2_fetch_data` (Finnhub recommendations, SEC
Form 4, SEC 13F, sector_rss aggregate, ai_news aggregate, fred_breadth,
fred_tier2, breadth_sentiment, sector_etf, analyst_breadth,
google_trends), the fixture didn't grow with it. Those calls would hit
the real network whenever the per-test cache happened to be cold.

Most of the time they were ~free because `data/lthcs/` and `.cache/lthcs/`
had warm responses. But on a cold cache (or after a `_cache.clear()` from
a sibling test, or a real upstream rate-limit), `test_source_exception_does_not_abort`
in particular blew up to **multi-minute hangs** on real Akamai (SEC EDGAR)
connections because it didn't even override `thesis_rotation.get_default_data_root`.

The fix in this commit:
1. `tests/lthcs/test_daily.py::patched_sources` now stubs every Stage 2
   network source `lthcs_daily` imports. New entries in the return dict
   keep the existing assertions working unchanged.
2. `test_source_exception_does_not_abort` now gets a `tmp_path`/`monkeypatch`
   that pin the rotation/persist root the same way its peers do.
3. `tests/lthcs/test_daily_des_tier2_and_sector_rss.py::patched_sources`
   got the same treatment — it was missing finnhub, sec_8k, yahoo_events
   (earnings + analyst actions), ai_news, analyst_breadth, google_trends.

No production code changed. Test count unchanged (1739 → 1739).

## Top 5 "intentionally slow" tests — leave these alone

| Test | Wall | Why it's slow | Why it stays |
|---|---|---|---|
| `tests/lthcs/test_sec_13f.py::test_fetch_universe_full_path` | ~0.80s | End-to-end 13F: two managers × two quarters × index.json + primary_doc.xml + form13fInfoTable.xml, all routed through a URL-dispatching `requests.get` patch and parsed by the real `sec_13f` pipeline. | Integration test for the 13F Phase 2 AUM-weighted path. Mocking the parser would defeat the test. |
| `tests/lthcs/test_sec_13f.py::test_fetch_universe_as_of_none_preserves_existing_behavior` | ~0.70s | Same machinery as above plus `as_of=None` regression branch. | Guards a P0 backfill regression; integration coverage is the point. |
| `tests/lthcs/test_sec_13f.py::test_fetch_universe_as_of_2026_05_17_includes_q1_filings` | ~0.69s | Full universe walk with historical `as_of` slicing across two quarters of holdings XML. | The as-of slicing is the bug-prone code path. Test exists because Phase 2 shipped without it. |
| `tests/lthcs/test_sec_edgar.py::test_cache_miss_refetches` | ~0.41s | Calls `sec_edgar._cache.clear()` mid-test — that's a real filesystem walk + unlink across the SEC EDGAR cache directory (~3-4k JSON files on a warm dev box). | Testing the cache layer's actual disk behavior is the whole point; replacing it with an in-memory stub would test nothing. |
| `tests/lthcs/test_sec_form4.py::test_fetch_insider_transactions_cluster_buying_detected` | ~0.49s | Parses three real Form 4 XML fixtures via lxml, then runs cluster-detection over the parsed transactions. | Form 4 XML parsing is fragile; the test was added when cluster_buying shipped. Don't mock the parser.|

Everything else after that runs in <0.5s. The `test_daily_des_tier2_and_sector_rss` integration tests are now ~0.4s (down from 6.3s) because they no longer fall through to real Finnhub/SEC after the fixture fix.

## Maintenance rule

When `lthcs_daily.stage_2_fetch_data` (or any later stage) calls a NEW
`lthcs.sources.*` function, add a corresponding `monkeypatch.setattr`
entry to BOTH `patched_sources` fixtures:
- `tests/lthcs/test_daily.py`
- `tests/lthcs/test_daily_des_tier2_and_sector_rss.py`

A return value of `{}` or `[]` is fine — the integration tests that need
non-trivial returns set `.return_value` themselves. Without the entry,
the next time the per-test SEC/Finnhub cache is cold, the suite walks
off a cliff.
