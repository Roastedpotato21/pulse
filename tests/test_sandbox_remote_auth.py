import asyncio
import os
import random

import pytest
import websockets

from pulse.sandbox.remote.client import RemoteClient
from pulse.sandbox.remote.server import RemoteServer


@pytest.fixture
async def remote_server():
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
    _, port = remote_server
    client = RemoteClient(endpoint_url=f"ws://127.0.0.1:{port}", auth_token="test-token1")
    await client.connect()
    # If connect succeeds without raising, we are good
    await client.disconnect()

@pytest.mark.asyncio
async def test_auth_failure(remote_server):
    _, port = remote_server
    client = RemoteClient(endpoint_url=f"ws://127.0.0.1:{port}", auth_token="wrong-token")
    with pytest.raises(websockets.exceptions.InvalidStatus) as exc:
        await client.connect()
    assert exc.value.response.status_code == 401

@pytest.mark.asyncio
async def test_multi_tenant(remote_server):
    _, port = remote_server
    client1 = RemoteClient(endpoint_url=f"ws://127.0.0.1:{port}", auth_token="test-token1")
    client2 = RemoteClient(endpoint_url=f"ws://127.0.0.1:{port}", auth_token="test-token2")
    await client1.connect()
    await client2.connect()
    await client1.disconnect()
    await client2.disconnect()

@pytest.mark.asyncio
async def test_server_refuses_non_loopback_without_mtls():
    server = RemoteServer(host="0.0.0.0", port=12345, auth_token="test")
    os.environ.pop("PULSE_TLS_CERT", None)
    os.environ.pop("PULSE_TLS_KEY", None)
    os.environ.pop("PULSE_TLS_CA", None)
    
    with pytest.raises(RuntimeError) as exc_info:
        await server.start()
    assert "strictly required for non-loopback" in str(exc_info.value)
