"""The in-process simulator must import and run without the networking optional deps.

Regression guard: `convexpi.arena`'s `__init__` used to eagerly import `RemoteAgent` from
`client`, which hard-requires `websockets`. That made a bare (numpy-only) install unable to
even `import convexpi.arena`, despite the package advertising local in-process agents as a
first-class use case. `RemoteAgent` is now imported lazily (PEP 562), so:

  - importing the package must NOT import `websockets`, and
  - the in-process Market must build and run,

both regardless of whether `websockets` happens to be installed in the test environment.
"""
import sys
import importlib


def test_import_does_not_pull_in_websockets():
    # Drop any pre-imported copies so we observe what `import convexpi.arena` actually triggers.
    for mod in [m for m in sys.modules if m == "websockets" or m.startswith("websockets.")]:
        del sys.modules[mod]
    for mod in [m for m in list(sys.modules) if m == "convexpi.arena" or m.startswith("convexpi.arena.")]:
        del sys.modules[mod]

    import convexpi.arena  # noqa: F401  (importing is the thing under test)

    assert "websockets" not in sys.modules, (
        "importing convexpi.arena pulled in websockets — the networking layer must stay lazy"
    )


def test_in_process_market_runs_without_networking():
    from convexpi.arena import Market, NoiseTrader, NaiveMarketMaker, MomentumTrader

    agents = [NoiseTrader(f"n{i}", seed=i) for i in range(4)]
    agents += [NaiveMarketMaker("mm", seed=1), MomentumTrader("m", seed=2)]
    m = Market(agents, n_ticks=200, seed=0)
    m.run()
    assert len(m.leaderboard()) == len(agents)


def test_remote_agent_is_lazy():
    import convexpi.arena as arena

    # Whether or not websockets is installed, accessing RemoteAgent must resolve to a class
    # (websockets present) or raise a clean ImportError (absent) — never a SystemExit, and
    # never at package-import time.
    try:
        import websockets  # noqa: F401
    except ImportError:
        import pytest
        with pytest.raises(ImportError):
            arena.RemoteAgent
    else:
        assert isinstance(arena.RemoteAgent, type)
