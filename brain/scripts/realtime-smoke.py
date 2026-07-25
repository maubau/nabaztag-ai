#!/usr/bin/env python3
"""Optional ONLINE smoke for the Realtime session — never runs in normal CI.

Everything else about this path is covered offline with fakes; the one thing
fakes cannot tell us is whether our `session.update` still matches the live API
schema (the audio format moved from a bare "pcm16" string to an object). This
opens a real WebSocket, sends the session config, and checks the server accepts
it — a few seconds and a negligible amount of billed time, no audio exchanged.

    export OPENAI_API_KEY=...
    python brain/scripts/realtime-smoke.py                     # default model
    python brain/scripts/realtime-smoke.py --turn-detection server_vad

Exit 0 = the schema was accepted. Run it after any OpenAI API change, and
before trusting a hardware session.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from rabbit_brain.llm.realtime import DEFAULT_MODEL, DEFAULT_VOICE, RealtimeSession


class _NullPlayer:
    """No audio is exchanged; the session only needs the interface to exist."""

    def write(self, pcm):  # pragma: no cover - never called in the smoke
        pass

    def reset_position(self):
        pass

    async def stop(self):
        pass

    async def resume(self):
        pass


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--voice", default=DEFAULT_VOICE)
    parser.add_argument("--turn-detection", default="semantic_vad")
    parser.add_argument("--timeout", type=float, default=15.0)
    args = parser.parse_args()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 2

    session = RealtimeSession(
        api_key=key,
        player=_NullPlayer(),
        instructions="Smoke test: do not respond.",
        model=args.model,
        voice=args.voice,
        turn_detection=args.turn_detection,
        close_player=False,
    )
    print(f"connecting: model={args.model} turn_detection={args.turn_detection}")
    try:
        await session.connect()
        print(f"connected in {session.timings.session_open_ms} ms; session.update sent")
        # The server answers session.created/updated — or an error if our schema
        # is stale. That answer is the entire point of this smoke.
        deadline = asyncio.get_running_loop().time() + args.timeout
        while asyncio.get_running_loop().time() < deadline:
            msg = await asyncio.wait_for(session._ws.receive(), timeout=args.timeout)
            if msg.data is None:
                break
            event = json.loads(msg.data)
            kind = event.get("type", "")
            print(f"  <- {kind}")
            if kind == "error":
                print(f"\nREJECTED: {event.get('error')}", file=sys.stderr)
                return 1
            if kind in ("session.updated", "session.created"):
                if kind == "session.updated":
                    print("\nOK: the live API accepted our session schema.")
                    return 0
    except Exception as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        await session.aclose()
    print("\nINCONCLUSIVE: no session.updated before the timeout.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
