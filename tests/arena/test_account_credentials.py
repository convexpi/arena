import asyncio
import json
import pytest
from convexpi.arena.server import ArenaServer


class Socket:
    def __init__(self):
        self.sent = []
        self.messages = asyncio.Queue()
        self.closed = None

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self, code, reason):
        self.closed = code
        await self.messages.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.messages.get()
        if message is None:
            raise StopAsyncIteration
        return json.dumps(message)


@pytest.fixture
def server():
    s = object.__new__(ArenaServer)
    s._lock = asyncio.Lock()
    s._remote = {}
    s.initial_cash = 10000
    s.market = type('Market', (), {'accounts': {'background': object()}})()
    s._activity_event = asyncio.Event()
    s.tick_interval = 1
    s.response_deadline = .5
    s.risk = None
    return s


@pytest.mark.asyncio
async def test_impersonation_rejected_and_authenticated_reconnect_preserves_account(server):
    original = Socket()
    task = asyncio.create_task(server._handle_agent(original, {'agent_id': 'alice'}))
    await asyncio.sleep(0)
    credential = original.sent[0]['reconnect_token']
    server.market.accounts['alice'].cash = 12345
    for token in (None, 'wrong'):
        attacker = Socket()
        await server._handle_agent(attacker, {'agent_id': 'alice', 'reconnect_token': token})
        assert attacker.closed == 1008
        assert server._remote['alice']['ws'] is original
        assert not attacker.sent
    resumed = Socket()
    replacement = asyncio.create_task(server._handle_agent(resumed, {'agent_id': 'alice', 'reconnect_token': credential}))
    await asyncio.sleep(0)
    await task  # the old socket's finally must not remove the new connection
    assert server._remote['alice']['ws'] is resumed
    assert server.market.accounts['alice'].cash == 12345
    await resumed.messages.put({'type': 'orders', 'orders': [{'side': 'buy', 'qty': 1, 'price': 100}]})
    await asyncio.sleep(0)
    assert (await server._remote['alice']['queue'].get())[0].agent_id == 'alice'
    await resumed.close(1000, 'done')
    await replacement


@pytest.mark.asyncio
async def test_background_accounts_cannot_be_claimed(server):
    attacker = Socket()
    await server._handle_agent(attacker, {'agent_id': 'background'})
    assert attacker.closed == 1008
    assert 'background' not in server._remote
