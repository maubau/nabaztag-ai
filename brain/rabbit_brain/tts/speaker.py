"""Speaker — TTS → local MP3 → BodyController playback (§6.2.6).

Replaces OJN's dead server-side TTS: text is synthesized locally, served by
the Mp3Server, and queued on the rabbit as a single urlList call. Long texts
are split into sentence MP3s to cut time-to-first-audio; the measured ~1.7 s
inter-file gap is accounted for by OjnAdapter.play_audio.
"""

from __future__ import annotations

import re

from ..body.controller import BodyController
from ..body.types import PlayAudioCommand, Priority
from .base import TTSProvider
from .mp3_server import Mp3Server

# Below this length a reply stays a single MP3: with a ~1.7s gap per boundary,
# splitting short texts hurts more than it helps (see OJN_API_NOTES findings).
SINGLE_FILE_MAX_CHARS = 200

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…:;])\s+")


def split_sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_SPLIT.split(text.strip()) if s]


class Speaker:
    def __init__(self, controller: BodyController, provider: TTSProvider, mp3_server: Mp3Server):
        self._controller = controller
        self._provider = provider
        self._mp3 = mp3_server

    async def speak(self, text: str, priority: Priority = Priority.USER_SPEECH_SYNC) -> float:
        """Synthesize and queue `text`; returns the summed MP3 duration in seconds
        (excluding inter-file gaps, which the adapter adds to its estimate)."""
        chunks = [text] if len(text) <= SINGLE_FILE_MAX_CHARS else split_sentences(text)
        results = [await self._provider.synth(chunk) for chunk in chunks]
        urls = tuple(self._mp3.url_for(r.path) for r in results)
        total = sum(r.duration_s for r in results)
        await self._controller.submit(PlayAudioCommand(urls, total), priority)
        return total
