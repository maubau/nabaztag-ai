import asyncio

import aiohttp
import pytest
from rabbit_brain.body.events_server import EventListener
from rabbit_brain.body.mock_ojn import MOCK_SERIAL
from rabbit_brain.body.ojn_adapter import OjnAdapter
from rabbit_brain.body.types import BodyEvent


@pytest.fixture
async def listener_setup():
    events: list[BodyEvent] = []
    listener = EventListener(events.append, port=0, serial=MOCK_SERIAL)
    # port=0: grab the ephemeral port after start
    await listener.start()
    port = listener._runner.addresses[0][1]
    url = f"http://127.0.0.1:{port}/event"
    async with aiohttp.ClientSession() as session:
        yield events, url, session
    await listener.stop()


async def test_rfid_webhook_becomes_body_event(listener_setup):
    events, url, session = listener_setup
    async with session.get(
        url, params={"bunny": MOCK_SERIAL, "event": "rfid", "value": "d0021a0533268a"}
    ) as resp:
        assert resp.status == 200
    assert events[0].kind == "rfid"
    assert events[0].data == "d0021a0533268a"
    assert events[0].timestamp > 0


async def test_click_webhooks_become_body_events(listener_setup):
    events, url, session = listener_setup
    for value in ("single", "double"):
        async with session.get(
            url, params={"bunny": MOCK_SERIAL, "event": "click", "value": value}
        ) as resp:
            assert resp.status == 200
    assert [e.kind for e in events] == ["single_click", "double_click"]


async def test_unknown_bunny_and_malformed_events_rejected(listener_setup):
    events, url, session = listener_setup
    async with session.get(
        url, params={"bunny": "ffffffffffff", "event": "rfid", "value": "aa"}
    ) as resp:
        assert resp.status == 403
    async with session.get(url, params={"bunny": MOCK_SERIAL, "event": "teleport"}) as resp:
        assert resp.status == 400
    async with session.get(url, params={"bunny": MOCK_SERIAL, "event": "rfid"}) as resp:
        assert resp.status == 400  # rfid without a tag
    assert events == []


async def test_listener_feeds_adapter_event_stream(mock_ojn):
    """End-to-end brain side: webhook -> push_event -> BodyAdapter.events()."""
    async with OjnAdapter(mock_ojn.base_url, MOCK_SERIAL, "mock-vapi-token") as adapter:
        listener = EventListener(adapter.push_event, port=0, serial=MOCK_SERIAL)
        await listener.start()
        port = listener._runner.addresses[0][1]
        try:
            async with aiohttp.ClientSession() as session:
                await session.get(
                    f"http://127.0.0.1:{port}/event",
                    params={"bunny": MOCK_SERIAL, "event": "rfid", "value": "beef"},
                )
            event = await asyncio.wait_for(anext(adapter.events()), 2)
            assert event.kind == "rfid"
            assert event.data == "beef"
        finally:
            await listener.stop()
