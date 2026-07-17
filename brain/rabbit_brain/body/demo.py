"""Drive a (mock or real) rabbit through the BodyController.

python -m rabbit_brain.body.demo --mock-ojn
python -m rabbit_brain.body.demo --ojn http://127.0.0.1 --serial <sn> --vapi-token <tk>

(--ojn points at the Apache wrapper on port 80; never :8080, which speaks
OJN's internal binary framing, not HTTP.)
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .controller import BodyController
from .mock_ojn import MOCK_SERIAL, MOCK_VAPI_TOKEN, MockOjnServer
from .ojn_adapter import OjnAdapter
from .types import EarsCommand, LedsCommand, LedSpec, PlayAudioCommand, Priority


async def demo(adapter: OjnAdapter, mock: MockOjnServer | None) -> None:
    controller = BodyController(adapter)
    runner = asyncio.create_task(controller.run())

    print("• idle pose (AMBIENT_IDLE) + two competing ear targets (agent wins, idle coalesced)")
    await controller.submit(EarsCommand(16, 16), Priority.AMBIENT_IDLE)
    await controller.submit(EarsCommand(8, 8), Priority.AMBIENT_IDLE)
    await controller.submit(EarsCommand(2, 14), Priority.AGENT_EXPRESSION)
    await controller.wait_idle()
    print(f"  controller state: ears={controller.snapshot().ears}")

    print("• mood lights (nose orange, pulsing)")
    await controller.submit(
        LedsCommand(LedSpec.from_dict({"nose": (255, 128, 0)}, pulse=True)),
        Priority.AGENT_EXPRESSION,
    )
    await controller.wait_idle()

    print("• queued sentence MP3s (one urlList call) + interrupt: queued audio drops,")
    print("  current utterance finishes (OJN cannot cancel — §6.6 degradation)")
    await controller.submit(
        PlayAudioCommand(("http://bolt:8090/s1.mp3", "http://bolt:8090/s2.mp3"), duration_s=0.5),
        Priority.USER_SPEECH_SYNC,
    )
    await controller.submit(
        PlayAudioCommand(("http://bolt:8090/idle-tai-chi.mp3",), duration_s=0.5),
        Priority.AMBIENT_IDLE,
    )
    await asyncio.sleep(0.1)
    controller.interrupt()
    await controller.wait_idle()

    if mock is not None:
        print("\nmock OJN received:")
        for call in mock.calls:
            print(f"  {call.endpoint:7s} {call.params}")

    runner.cancel()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock-ojn", action="store_true", help="run against the in-process mock")
    parser.add_argument(
        "--ojn", default="http://127.0.0.1", help="OJN base URL (Apache wrapper, port 80)"
    )
    parser.add_argument("--serial", default=MOCK_SERIAL, help="rabbit serial (sn)")
    parser.add_argument("--vapi-token", default=MOCK_VAPI_TOKEN, help="VAPI token")
    args = parser.parse_args()

    mock = None
    base_url = args.ojn
    if args.mock_ojn:
        mock = MockOjnServer()
        await mock.start()
        base_url = mock.base_url
        print(f"mock OJN listening on {base_url}\n")

    async with OjnAdapter(base_url, args.serial, args.vapi_token) as adapter:
        await demo(adapter, mock)

    if mock is not None:
        await mock.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
