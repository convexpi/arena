"""
Arena demo — runs a full 2000-tick session in-process.

Population: 8 noise traders, 1 naive market maker, 2 momentum traders,
1 informed trader, and 1 student example agent.

Scenario: volatility triples and jump risk spikes at tick 1200.

Run:
    python examples/arena_demo.py
"""

from convexpi.arena import Agent, Market, Side
from convexpi.arena.agents import InformedTrader, MomentumTrader, NaiveMarketMaker, NoiseTrader


class StudentMeanReverter(Agent):
    """Fade large deviations from a rolling mean with a hard inventory limit."""

    def __init__(self, agent_id, seed=0, lookback=50, entry_bps=25, size=5, max_pos=60):
        super().__init__(agent_id, seed)
        self.lookback, self.entry_bps = lookback, entry_bps
        self.size, self.max_pos = size, max_pos
        self.mids = []

    def on_tick(self, state):
        if state.mid is None:
            return []
        self.mids.append(state.mid)
        if len(self.mids) < self.lookback:
            return []
        mean = sum(self.mids[-self.lookback:]) / self.lookback
        dev_bps = (state.mid / mean - 1) * 1e4
        if dev_bps > self.entry_bps and state.position > -self.max_pos:
            return [self.market(Side.SELL, self.size)]
        if dev_bps < -self.entry_bps and state.position < self.max_pos:
            return [self.market(Side.BUY, self.size)]
        return []


def vol_shock(market):
    print(">>> SCENARIO at tick 1200: volatility x3 + jump risk x5 <<<")
    market.fundamental.vol_bps *= 3
    market.fundamental.jump_prob *= 5


def main():
    agents = (
        [NoiseTrader(f"noise_{i}", seed=10 + i) for i in range(8)]
        + [NaiveMarketMaker("market_maker", seed=42)]
        + [MomentumTrader(f"momentum_{i}", seed=77 + i) for i in range(2)]
        + [InformedTrader("informed", seed=99)]
        + [StudentMeanReverter("STUDENT_meanrev", seed=7)]
    )

    market = Market(agents, n_ticks=2000, seed=1).at_tick(1200, vol_shock)
    market.run(verbose_every=400)

    print("\n=== FINAL LEADERBOARD (mark-to-market PnL, $) ===")
    print(f"{'agent':<20}{'PnL ($)':>12}{'final position':>16}")
    for agent_id, pnl, pos in market.leaderboard():
        print(f"{agent_id:<20}{pnl:>12,.2f}{pos:>16}")

    market.write_telemetry("snapshots.csv", "trades.csv")
    n = len(market.engine.trades)
    print(f"\nTelemetry written: snapshots.csv (per-tick), trades.csv ({n} trades)")


if __name__ == "__main__":
    main()
