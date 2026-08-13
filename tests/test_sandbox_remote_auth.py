import asyncio
import os

import pytest
import websockets

from pulse.sandbox.remote.client import RemoteClient
from pulse.sandbox.remote.server import RemoteServer


import random

@pytest.fixture
async def remote_server():
    os.environ["PULSE_REMOTE_INSECURE"] = "1"
    port = random.randint(20000, 60000)
    server = RemoteServer(port=port, auth_token="test-token1,test-token2")
    task = asyncio.create_task(server.start())
    # give it a moment to start
    await asyncio.sleep(0.1)
    yield server, port
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
@pytest.mark.asyncio
async def test_auth_success(remote_server):
    server, port = remote_server
    client = RemoteClient(endpoint_url=f"ws://127.0.0.1:{port}", auth_token="test-token1")
    await client.connect()
    # If connect succeeds without raising, we are good
    await client.disconnect()

@pytest.mark.asyncio
async def test_auth_failure(remote_server):
    server, port = remote_server
    client = RemoteClient(endpoint_url=f"ws://127.0.0.1:{port}", auth_token="wrong-token")
    with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
        await client.connect()
    assert exc.value.response.status_code == 401

@pytest.mark.asyncio
async def test_multi_tenant(remote_server):
    server, port = remote_server
    client1 = RemoteClient(endpoint_url=f"ws://127.0.0.1:{port}", auth_token="test-token1")
    client2 = RemoteClient(endpoint_url=f"ws://127.0.0.1:{port}", auth_token="test-token2")
    await client1.connect()
    await client2.connect()
    await client1.disconnect()
    await client2.disconnect()
