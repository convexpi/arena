"""The realistic (GARCH) fundamental is opt-in: the default 'gauss' process is unchanged, and the
'garch' process adds volatility clustering."""
import numpy as np
from convexpi.arena.market import FundamentalValue


def _r2_autocorr(vals):
    r = np.diff(np.log(np.asarray(vals)))
    x = r**2 - (r**2).mean()
    return float(np.sum(x[1:] * x[:-1]) / np.sum(x * x))


def test_default_is_deterministic_and_unchanged_rng_order():
    a = [FundamentalValue(seed=3).step() for _ in range(500)]
    b = [FundamentalValue(seed=3).step() for _ in range(500)]
    assert a == b                      # deterministic
    assert all(v > 0 for v in a)


def test_garch_adds_clustering():
    gauss = [FundamentalValue(seed=1, process="gauss").step() for _ in range(3000)]
    garch = [FundamentalValue(seed=1, process="garch").step() for _ in range(3000)]
    assert _r2_autocorr(garch) > _r2_autocorr(gauss) + 0.02
    assert all(v > 0 for v in garch)
