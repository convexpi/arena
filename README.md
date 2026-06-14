# convexpi-arena

Discrete-time limit-order-book exchange simulator for quantitative finance education and research.

```bash
pip install convexpi-arena
```

Part of the [ConvexPi](https://convexpi.ai) platform. See also [convexpi-lab](https://github.com/convexpi/lab) for the daily-data research harness.

## Quick start

```python
from convexpi.arena import Market, Agent, MarketState
import random

class MyAgent(Agent):
    def on_tick(self, state: MarketState):
        if state.mid and random.random() < 0.1:
            return [self.limit('buy', round(state.mid) - 5, 10)]
        return []

market = Market(n_background_agents=20)
market.run(ticks=1000, agents=[MyAgent(cash=10_000)])
```

## Run the Arena server

```bash
convexpi-server                          # WebSocket on :8765
convexpi-server --tick-interval 0.2     # faster
convexpi-server --admin-token secret    # instructor console
```

Connect a remote agent:

```python
from convexpi.arena import RemoteAgent

class MyAgent(RemoteAgent):
    def on_tick(self, state):
        if state.mid and state.position < 50:
            return [self.limit('buy', round(state.mid) - 5, 5)]
        return []

MyAgent().run('ws://localhost:8765', name='my-agent')
```

## Features

- Price-time priority matching engine
- Background agent population: noise traders, market makers, momentum, informed trader
- Avellaneda-Stoikov optimal market making
- TWAP execution agent
- Risk engine: drawdown limits, position limits, force liquidation
- Crypto L2 replay (Binance / Coinbase candle data)
- WebSocket server with admin-triggered volatility shocks
- Rich terminal visualizer

## License

MIT © Shane Conway
