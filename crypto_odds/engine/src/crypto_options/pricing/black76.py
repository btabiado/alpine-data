"""Black-76 pricing on a forward, implied-vol inversion and the constant-vol terminal probability.

Conventions (all prices are per one unit of underlying, in the numeraire of the forward):
    F  forward for the option's expiry
    K  strike
    T  year fraction to expiry (ACT/365 by default, see `year_fraction`)
    D  discount factor to expiry, D = exp(-r T)
    sigma  annualised lognormal volatility

    call = D [F N(d1) - K N(d2)],  put = D [K N(-d2) - F N(-d1)]
    d1 = [ln(F/K) + sigma^2 T / 2] / (sigma sqrt T),  d2 = d1 - sigma sqrt T

Under the lognormal (constant-vol) measure Q, Q(S_T > K) = N(d2). This is the
*benchmark* only: plugging each strike's own IV into N(d2) ignores the smile's
strike dependence and is not the smile-consistent probability (brief s.6).
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

SECONDS_PER_YEAR = 365.0 * 24 * 3600


def year_fraction(start: datetime, end: datetime) -> float:
    """ACT/365 year fraction between two timezone-aware UTC datetimes."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("datetimes must be timezone-aware (UTC)")
    return (end - start).total_seconds() / SECONDS_PER_YEAR


def _d1_d2(F, K, T, sigma):
    F, K, T, sigma = map(np.asarray, (F, K, T, sigma))
    if np.any(F <= 0) or np.any(K <= 0):
        raise ValueError("forward and strike must be positive")
    if np.any(T <= 0):
        raise ValueError("time to expiry must be positive")
    if np.any(sigma <= 0):
        raise ValueError("volatility must be positive")
    sd = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * sd * sd) / sd
    return d1, d1 - sd


def price(F, K, T, sigma, D=1.0, call=True):
    """Undiscounted-forward Black-76 price times D. Vectorised over all arguments."""
    d1, d2 = _d1_d2(F, K, T, sigma)
    D = np.asarray(D, dtype=float)
    if call:
        return D * (F * norm.cdf(d1) - K * norm.cdf(d2))
    return D * (K * norm.cdf(-d2) - F * norm.cdf(-d1))


def exceedance_probability(F, K, T, sigma):
    """Constant-vol risk-neutral Q(S_T > K) = N(d2)."""
    _, d2 = _d1_d2(F, K, T, sigma)
    return norm.cdf(d2)


def vega(F, K, T, sigma, D=1.0):
    d1, _ = _d1_d2(F, K, T, sigma)
    return np.asarray(D) * F * norm.pdf(d1) * np.sqrt(T)


def implied_vol(px, F, K, T, D=1.0, call=True, lo=1e-4, hi=10.0, tol=1e-10):
    """Scalar implied volatility by bracketed root finding.

    Returns NaN when the price violates the no-arbitrage bounds
    [D*max(F-K,0), D*F] for calls or [D*max(K-F,0), D*K] for puts, or when the
    root lies outside [lo, hi]. NaN is a quality signal for the caller, never 0.
    """
    if not (np.isfinite(px) and px > 0 and F > 0 and K > 0 and T > 0 and D > 0):
        return float("nan")
    intrinsic = D * max(F - K, 0.0) if call else D * max(K - F, 0.0)
    upper = D * F if call else D * K
    if px <= intrinsic or px >= upper:
        return float("nan")
    f = lambda s: float(price(F, K, T, s, D, call)) - px
    if f(lo) > 0 or f(hi) < 0:
        return float("nan")
    return brentq(f, lo, hi, xtol=tol, maxiter=200)


def parity_gap(call_px, put_px, F, K, D=1.0):
    """C - P - D (F - K): zero under put-call parity on a forward."""
    return call_px - put_px - D * (F - K)
