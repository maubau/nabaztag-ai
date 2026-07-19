"""Deepgram streaming STT — cloud primary (§6.2.4).

Live websocket API: PCM chunks go out as they are captured, final transcript
segments come back; total latency ≈ network + endpointing, not utterance
length. Model/language are config (`deepgram.model: nova-3`), never hardcoded
call sites, so they can be swapped without code changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator

import aiohttp

from .base import STTResult

log = logging.getLogger(__name__)

DEFAULT_WS_BASE = "wss://api.deepgram.com/v1/listen"
DEFAULT_TIMEOUT_S = 30.0


class DeepgramSTT:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "nova-3",
        language: str = "multi",
        endpointing_ms: int = 100,
        ws_base: str = DEFAULT_WS_BASE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self._api_key = api_key or os.environ["DEEPGRAM_API_KEY"]
        self._model = model
        self._language = language
        # Deepgram's own end-of-speech endpointing; 100 ms is their recommended
        # value for nova-3 multilingual it/en code-switching.
        self._endpointing_ms = endpointing_ms
        self._ws_base = ws_base
        self._timeout_s = timeout_s

    async def transcribe(self, chunks: AsyncIterator[bytes], sample_rate: int) -> STTResult:
        params = {
            "model": self._model,
            "language": self._language,
            "endpointing": str(self._endpointing_ms),
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "channels": "1",
            "smart_format": "true",
        }
        url = self._ws_base + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        async with asyncio.timeout(self._timeout_s):
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    url, headers={"Authorization": f"Token {self._api_key}"}
                ) as ws:
                    sender = asyncio.create_task(self._send(ws, chunks))
                    try:
                        text = await self._receive(ws)
                    finally:
                        sender.cancel()
                    return STTResult(text=text, provider="deepgram")

    async def _send(
        self, ws: aiohttp.ClientWebSocketResponse, chunks: AsyncIterator[bytes]
    ) -> None:
        async for chunk in chunks:
            await ws.send_bytes(chunk)
        await ws.send_str(json.dumps({"type": "CloseStream"}))

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> str:
        finals: list[str] = []
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            data = json.loads(msg.data)
            if data.get("type") == "Results":
                alt = data["channel"]["alternatives"][0]
                if data.get("is_final") and alt.get("transcript"):
                    finals.append(alt["transcript"])
            elif data.get("type") == "Metadata":
                # sent after CloseStream: the stream is fully processed
                break
        return " ".join(finals).strip()
