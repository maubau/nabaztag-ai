"""Speaker — TTS → local MP3 → BodyController playback (§6.2.6).

Replaces OJN's dead server-side TTS: text is synthesized locally, served by
the Mp3Server, and queued on the rabbit as a single urlList call. Long texts
are split into sentence MP3s to cut time-to-first-audio; the measured ~1.7 s
inter-file gap is accounted for by OjnAdapter.play_audio.
"""

from __future__ import annotations

import re
from collections.abc import Callable

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

    async def speak(
        self,
        text: str,
        priority: Priority = Priority.USER_SPEECH_SYNC,
        language: str | None = None,
        on_checkpoint: Callable[[str], None] | None = None,
    ) -> float:
        """Synthesize and queue `text`; returns the summed MP3 duration in seconds
        (excluding inter-file gaps, which the adapter adds to its estimate).
        `language` is the STT-detected utterance language, passed to the TTS
        provider for voice routing (Deepgram it/en); never guessed from text.

        Time-to-first-audio: the first sentence is submitted as soon as it is
        synthesized; the remaining sentences are synthesized while it plays and
        queued as a second urlList (the controller's audio lane sequences them).

        `on_checkpoint`, if given, is called with named timing markers
        ("tts_start", "tts_first_chunk_ready", "first_chunk_submitted",
        "tts_complete", "all_submitted") so a caller (AgentLoop) can break the
        "transcript -> audio queued" span down instead of one opaque number
        (hardware round, July 2026: separate LLM/TTS/OJN-submit latency).
        """

        def checkpoint(name: str) -> None:
            if on_checkpoint is not None:
                on_checkpoint(name)

        chunks = [text] if len(text) <= SINGLE_FILE_MAX_CHARS else split_sentences(text)
        if not chunks:
            return 0.0
        checkpoint("tts_start")
        first = await self._provider.synth(chunks[0], language=language)
        checkpoint("tts_first_chunk_ready")
        await self._controller.submit(
            PlayAudioCommand((self._mp3.url_for(first.path),), first.duration_s), priority
        )
        checkpoint("first_chunk_submitted")
        total = first.duration_s
        if len(chunks) > 1:
            rest = [await self._provider.synth(chunk, language=language) for chunk in chunks[1:]]
            checkpoint("tts_complete")
            rest_total = sum(r.duration_s for r in rest)
            await self._controller.submit(
                PlayAudioCommand(tuple(self._mp3.url_for(r.path) for r in rest), rest_total),
                priority,
            )
            total += rest_total
        else:
            checkpoint("tts_complete")
        checkpoint("all_submitted")
        return total
