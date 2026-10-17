"""Daily 30-day outlook for /crypto-odds/: what the research models expect each coin to do next.

For BTC, ETH, XRP and LINK this fetches fresh daily spot closes (the same venues as the backtest's spot cohort),
runs the base rate and every price-history model from the vendored research engine at today's origin, and writes
outlook.json with, per model:
    P(down more than 10%), P(within +/-10%), P(up more than 10%) over the next 30 days, and an 80% price range.

The headline model per asset is the one with the best skill on the sealed holdout's spot 30-day cell
(crypto_scorecard/scorecard.json, the only fair test). If no model beat the base rate there, the base rate is the
headline. The call is UP or DOWN only when the headline model estimates direction (base rate, drift, filtered
historical simulation) and its P(up) and P(down) differ by 5 points or more; otherwise FLAT. Zero-drift models
(lognormal, GARCH) have no view on direction by design: their P(down) exceeds P(up) only because a zero expected
price change puts the median below today's price, so their up/down gap is never read as a call.
Each day's calls are appended to outlook_history.json and graded once 30 days have passed.

Exits non-zero (leaving the last-good outlook.json in place) if no asset could be forecast.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "1")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "engine" / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.stats import norm  # noqa: E402

from crypto_options.adapters import history  # noqa: E402
from crypto_options.backtest import models  # noqa: E402
from crypto_options.backtest.panel import Series  # noqa: E402
from crypto_options.config import load_config  # noqa: E402
from crypto_options.pandas_compat import utc_ns  # noqa: E402

H = 30
THRESHOLD = 0.10
LEAN_MIN = 0.05
Z80 = norm.ppf(0.9)
ASSETS = ["BTC", "ETH", "XRP", "LINK"]
LIVE_MODELS = ["climatology", "lognormal_rv90", "lognormal_ewma", "lognormal_ewma_drift", "fhs_ewma", "garch_t"]
NEUTRAL = {"lognormal_rv90", "lognormal_ewma", "garch_t"}     # zero expected price change: no directional view
RULE = (f"Headline model: the best skill on the sealed final test (spot prices, {H} days) for that asset, or the base "
        f"rate if no model beat it. UP or DOWN only when the headline model estimates direction and its chances of a "
        f"move up or down of more than {THRESHOLD:.0%} differ by {LEAN_MIN * 100:.0f} points or more; otherwise FLAT. "
        f"Zero-drift volatility models have no view on direction, so they always give FLAT.")


def spot_series(asset: str) -> Series:
    df = history.spot_closes(asset, None)
    return Series(asset, "spot", df["source"].iat[0], pd.DatetimeIndex(utc_ns(df["date"])),
                  pd.DatetimeIndex(utc_ns(df["available_time"])), df["price"].to_numpy(float), None)


def ranges(s: Series, bt: dict) -> dict:
    """80% range of the 30-day log return per model, at the last origin, with the same inputs as models.forecast."""
    r = s.log_returns()
    n = len(s)
    warm = models._warm(s, bt)
    v_ewma = models.ewma_var(r, bt["ewma_lambda"])
    rv = pd.Series(r).rolling(bt["rv_window"], min_periods=int(0.8 * bt["rv_window"])).var().to_numpy()
    mu = pd.Series(r).rolling(bt["drift_window"], min_periods=int(0.8 * bt["drift_window"])).mean().to_numpy()
    # GARCH: reproduce the backtest's refit schedule; only the last refit (on all data to that point) matters today
    start = int(np.flatnonzero(warm)[0])
    last_refit = start + ((n - 1 - start) // bt["garch_refit_days"]) * bt["garch_refit_days"]
    w2 = np.zeros(n, bool)
    w2[last_refit:] = True
    vg = models.garch_hday_variance(r, w2, [H], bt["garch_refit_days"])[H][-1]

    def normal(m, V):
        return (m - Z80 * np.sqrt(V), m + Z80 * np.sqrt(V))
    out = {"lognormal_rv90": normal(-0.5 * rv[-1] * H, rv[-1] * H),
           "lognormal_ewma": normal(-0.5 * v_ewma[-1] * H, v_ewma[-1] * H),
           "lognormal_ewma_drift": normal(mu[-1] * H, v_ewma[-1] * H),
           "garch_t": normal(-0.5 * vg, vg)}
    # empirical: past 30-day returns whose outcome is already known (climatology), and the same returns
    # standardised by the EWMA vol of their day and rescaled to today's (filtered historical simulation)
    jo = s.outcome_index(H)
    known = (jo >= 0) & (jo <= n - 1)
    R = np.where(known, np.log(s.price[np.maximum(jo, 0)] / s.price), np.nan)
    ok = np.isfinite(R)
    out["climatology"] = tuple(np.quantile(R[ok], [0.1, 0.9]))
    sd = np.sqrt(v_ewma)
    z = R / (sd * np.sqrt(H))
    zok = np.isfinite(z)
    q = np.quantile(z[zok], [0.1, 0.9]) * sd[-1] * np.sqrt(H)
    out["fhs_ewma"] = (float(q[0]), float(q[1]))
    return {m: (float(lo), float(hi)) for m, (lo, hi) in out.items()}


def headline(asset: str, sc: dict) -> dict:
    cols = sc["promotion"]["columns"]
    rows = [dict(zip(cols, r)) for r in sc["promotion"]["rows"]]
    cell = [r for r in rows if r["cohort"] == "spot" and r["asset"] == asset and int(r["h"]) == H and r["testable"]
            and r.get("bss_vs_climatology") is not None]
    if not cell:
        return {"model": "climatology", "basis": "untestable", "best": None}
    best = max(cell, key=lambda r: r["bss_vs_climatology"])
    pick = lambda r: {"model": r["model"], "skill": r["bss_vs_climatology"], "ci": [r["bss_ci_lo"], r["bss_ci_hi"]],
                      "reliable": bool(r["bss_ci_lo"] is not None and r["bss_ci_lo"] > 0)}
    if best["bss_vs_climatology"] <= 0:
        return {"model": "climatology", "basis": "no model beat the base rate", "best": pick(best)}
    if best["model"] in LIVE_MODELS:
        return {"model": best["model"], "basis": "best on the final test", "best": pick(best)}
    live = [r for r in cell if r["model"] in LIVE_MODELS and r["bss_vs_climatology"] > 0]
    if live:   # the overall best (ML or foundation model) is not run live; use the best price-history model
        b2 = max(live, key=lambda r: r["bss_vs_climatology"])
        return {"model": b2["model"], "basis": "best live-runnable model on the final test", "best": pick(b2),
                "overall_best": pick(best)}
    return {"model": "climatology", "basis": "best model is not run live", "best": pick(best)}


def call(p: dict, model: str) -> dict:
    lean = p["up"] - p["down"]
    directional = model not in NEUTRAL
    label = ("UP" if lean >= LEAN_MIN else "DOWN" if lean <= -LEAN_MIN else "FLAT") if directional else "FLAT"
    return {"label": label, "directional_model": directional, "lean_pts": round(lean * 100, 1),
            "within": p["flat"], "big_move": round(p["up"] + p["down"], 4), "range_bound": p["flat"] >= 0.5}


def signals(per: dict) -> dict:
    """Two direction readings that disagree often: long-run history (base rate) and the past year's trend."""
    g = lambda m: round((per[m]["up"] - per[m]["down"]) * 100, 1) if m in per else None
    return {"history_lean_pts": g("climatology"), "trend_lean_pts": g("lognormal_ewma_drift")}


def grade(hist: list, prices: dict) -> list:
    """Fill in the realised outcome for logged calls whose 30-day window has closed."""
    for e in hist:
        if e.get("outcome") or e["asset"] not in prices:
            continue
        px = prices[e["asset"]]
        target = utc_ns(pd.Timestamp(e["date"], tz="UTC") + pd.Timedelta(days=H))
        if target in px.index:
            ret = float(px[target] / e["price"] - 1)
            e["realized_return"] = round(ret, 4)
            e["outcome"] = "UP" if ret > THRESHOLD else "DOWN" if ret < -THRESHOLD else "FLAT"
            e["hit"] = e["outcome"] == e["call"]
    return hist


def main() -> int:
    cfg = load_config(HERE / "engine" / "config" / "config.yaml")
    bt = cfg["backtest"]
    sc = json.loads((HERE.parent / "crypto_scorecard" / "scorecard.json").read_text())
    out, prices = [], {}
    for asset in ASSETS:
        try:
            s = spot_series(asset)
            prices[asset] = pd.Series(s.price, index=s.dates)
            fc = models.forecast(s, [H], [THRESHOLD, -THRESHOLD], bt)
            last = fc[fc["origin_idx"] == len(s) - 1]
            rg = ranges(s, bt)
            per = {}
            for m in LIVE_MODELS:
                up = float(last[(last["model"] == m) & (last["target"] > 0)]["p"].iat[0])
                dn = float(last[(last["model"] == m) & (last["target"] < 0)]["p"].iat[0])
                if not (np.isfinite(up) and np.isfinite(dn)):
                    continue
                lo, hi = rg[m]
                per[m] = {"down": round(dn, 4), "flat": round(max(0.0, 1 - up - dn), 4), "up": round(up, 4),
                          "range": [round(s.price[-1] * float(np.exp(lo)), 6), round(s.price[-1] * float(np.exp(hi)), 6)]}
            hd = headline(asset, sc)
            if hd["model"] not in per:
                hd = {**hd, "model": "climatology", "basis": hd["basis"] + "; headline model unavailable today"}
            out.append({"asset": asset, "price": float(s.price[-1]), "price_date": str(s.dates[-1].date()),
                        "source": s.source, "models": per, "headline": hd, "call": call(per[hd["model"]], hd["model"]),
                        "signals": signals(per)})
            print(f"[outlook] {asset}: {out[-1]['call']['label']} via {hd['model']} {per[hd['model']]}")
        except Exception as exc:   # one venue failing must not block the others
            print(f"[outlook] {asset}: failed ({exc})", file=sys.stderr)
    if not out:
        print("[outlook] no asset could be forecast; keeping last-good outlook.json", file=sys.stderr)
        return 1
    hist_path = HERE / "outlook_history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else []
    seen = {(e["date"], e["asset"]) for e in hist}
    for a in out:
        if (a["price_date"], a["asset"]) not in seen:
            p = a["models"][a["headline"]["model"]]
            hist.append({"date": a["price_date"], "asset": a["asset"], "price": a["price"], "model": a["headline"]["model"],
                         "call": a["call"]["label"], "down": p["down"], "flat": p["flat"], "up": p["up"]})
    hist = grade(hist, prices)
    hist_path.write_text(json.dumps(hist, separators=(",", ":")))
    graded = [e for e in hist if e.get("outcome")]
    doc = {"as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "horizon_days": H,
           "threshold": THRESHOLD, "lean_min": LEAN_MIN, "rule": RULE, "assets": out,
           "track_record": {"logged": len(hist), "first_logged": min((e["date"] for e in hist), default=None),
                            "graded": len(graded), "hits": sum(1 for e in graded if e["hit"])},
           "engine_revision": (HERE / "engine" / "VENDORED_FROM").read_text().strip()}
    (HERE / "outlook.json").write_text(json.dumps(doc, separators=(",", ":")))
    print(f"[outlook] wrote outlook.json for {', '.join(a['asset'] for a in out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
