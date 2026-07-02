"""The realistic (GARCH) fundamental is opt-in: the default 'gauss' process is unchanged, and the
'garch' process adds volatility clustering. Clustering is tested with jumps OFF so the (iid, large)
jump component doesn't dilute the squared-return autocorrelation we're measuring."""
import numpy as np
from convexpi.arena.market import FundamentalValue


def _path(fund, n):
    return [fund.step() for _ in range(n)]   # step ONE fundamental n times (not n fresh ones)


def _r2_autocorr(vals):
    r = np.diff(np.log(np.asarray(vals, dtype=float)))
    x = r**2 - (r**2).mean()
    denom = np.sum(x * x)
    return float(np.sum(x[1:] * x[:-1]) / denom) if denom > 0 else 0.0


def test_default_is_deterministic():
    a = _path(FundamentalValue(seed=3), 500)
    b = _path(FundamentalValue(seed=3), 500)
    assert a == b
    assert all(v > 0 for v in a)


def test_garch_adds_clustering():
    gauss = _path(FundamentalValue(seed=1, process="gauss", jump_prob=0.0), 6000)
    garch = _path(FundamentalValue(seed=1, process="garch", jump_prob=0.0, horizon=8000), 6000)
    ac_g, ac_c = _r2_autocorr(gauss), _r2_autocorr(garch)
    assert np.isfinite(ac_g) and np.isfinite(ac_c)
    assert ac_c > 0.02              # GARCH squared-return autocorrelation is structurally positive
    assert ac_c > ac_g + 0.02       # ... and clearly above the constant-vol Gaussian baseline
    assert all(v > 0 for v in garch)
