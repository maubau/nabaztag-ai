import asyncio

import pytest
from rabbit_brain.body.controller import BodyController
from rabbit_brain.body.mock_ojn import MOCK_SERIAL, MOCK_VAPI_TOKEN, MockOjnServer
from rabbit_brain.body.ojn_adapter import OjnAdapter


@pytest.fixture
async def mock_ojn():
    server = MockOjnServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
async def adapter(mock_ojn):
    async with OjnAdapter(mock_ojn.base_url, MOCK_SERIAL, MOCK_VAPI_TOKEN) as a:
        yield a


@pytest.fixture
async def controller(adapter):
    c = BodyController(adapter)
    task = asyncio.create_task(c.run())
    yield c
    task.cancel()
