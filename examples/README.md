# Arena example agents

Starter agents you can copy and modify. Each subclasses `RemoteAgent` (see
`src/convexpi/arena/client.py`), overrides `on_tick`, and connects to a running Arena server.

```bash
pip install convexpi-arena        # or, from this repo: pip install -e .
python examples/market_maker.py my-handle --server wss://arena-production-e3f1.up.railway.app
```

| File | Strategy | Teaches |
|---|---|---|
| `market_maker.py` | Quote both sides around the mid; skew quotes against inventory | Spread capture, maker rebate, inventory risk, adverse selection, queue position |

The `client.py` module also ships a `MeanReverterAgent` (run it directly) as a directional baseline.

See the lesson at **https://convexpi.ai/lessons/market-making** and the matching-engine explainer at
**https://convexpi.ai/exchange**. Real-order-book competition: **/compete/arena-book**.
