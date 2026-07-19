"""Hardware smoke test for the audio-in pipeline on the Bolt.

python -m rabbit_brain.audio.demo --config config.yaml            # real mic + real OJN (env creds)
python -m rabbit_brain.audio.demo --config config.yaml --mock-ojn # real mic, mock body
python -m rabbit_brain.audio.demo --config config.yaml --wav f.wav --mock-ojn  # no mic at all

Prints wake detections and transcripts; the DoA ear reflex runs if
doa.enabled. Needs the `audio` extra (and `stt-local` for stt_profile:
local). OJN credentials come from the environment (OJN_BASE_URL,
RABBIT_SERIAL, OJN_VAPI_TOKEN) as everywhere else.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import yaml

from ..body.controller import BodyController
from ..body.mock_ojn import MOCK_SERIAL, MOCK_VAPI_TOKEN, MockOjnServer
from ..body.ojn_adapter import OjnAdapter
from ..body.types import PlayAudioCommand
from ..stt import make_stt
from ..tts import Mp3Server
from .capture import AlsaCapture, MicCapture, WavCapture
from .doa import make_doa
from .pipeline import VoicePipeline
from .vad import SileroProbe
from .wake import OpenWakeWordDetector


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="config.yaml path")
    parser.add_argument("--moods", default="moods.yaml", help="moods.yaml path (doa sectors)")
    parser.add_argument("--wav", help="replay a WAV file instead of the microphone")
    parser.add_argument("--mock-ojn", action="store_true", help="mock body instead of real OJN")
    args = parser.parse_args()

    config = yaml.safe_load(Path(args.config).read_text())
    moods = yaml.safe_load(Path(args.moods).read_text())
    asyncio.run(run(args, config, moods))


async def run(args: argparse.Namespace, config: dict, moods: dict) -> None:
    audio_cfg = config.get("audio", {})
    wake_cfg = config.get("wake", {})

    capture: MicCapture
    if args.wav:
        capture = WavCapture(
            args.wav, selected_channel=audio_cfg.get("selected_channel", 0), realtime=True
        )
    else:
        capture = AlsaCapture(
            device=audio_cfg.get(
                "capture_device_index", audio_cfg.get("capture_device", "hw:CARD=C16K6Ch,DEV=0")
            ),
            sample_rate=audio_cfg.get("sample_rate", 16_000),
            channels=audio_cfg.get("channels", 6),
            selected_channel=audio_cfg.get("selected_channel", 0),
        )

    mock = None
    if args.mock_ojn:
        mock = MockOjnServer()
        await mock.start()
        base_url, serial, token = mock.base_url, MOCK_SERIAL, MOCK_VAPI_TOKEN
    else:
        base_url = os.environ.get("OJN_BASE_URL", "http://127.0.0.1")
        serial, token = os.environ["RABBIT_SERIAL"], os.environ["OJN_VAPI_TOKEN"]

    async def on_transcript(text: str) -> None:
        print(f"\n>>> {text}\n")

    # Optional wake beep played ON THE RABBIT: serve a short MP3 the rabbit can
    # fetch (same Mp3Server the TTS layer uses; base_url must be the Bolt's
    # legacy-segment IP, not localhost). Off unless wake_beep.enabled + mp3.
    beep_cfg = config.get("wake_beep", {})
    mp3_cfg = config.get("mp3_server", {})
    wake_beep = None
    beep_server = None
    if beep_cfg.get("enabled", False) and beep_cfg.get("mp3"):
        beep_path = Path(beep_cfg["mp3"])
        beep_server = Mp3Server(
            beep_path.parent,
            host=mp3_cfg.get("host", "0.0.0.0"),
            port=mp3_cfg.get("port", 8090),
            base_url=mp3_cfg.get("base_url"),
            protected={beep_path.name},  # static asset: never purged by retention
        )
        await beep_server.start()
        wake_beep = PlayAudioCommand(
            (beep_server.url_for(beep_path),), beep_cfg.get("duration_ms", 120) / 1000
        )

    async with OjnAdapter(base_url, serial, token) as adapter:
        controller = BodyController(adapter)
        runner = asyncio.create_task(controller.run())
        pipeline = VoicePipeline(
            capture=capture,
            wake=OpenWakeWordDetector(models=tuple(wake_cfg.get("models", ["hey_jarvis"]))),
            probe_factory=SileroProbe,
            stt=make_stt(config),
            controller=controller,
            on_transcript=on_transcript,
            doa=make_doa(config),
            doa_moods=moods.get("doa", {}),
            wake_threshold=wake_cfg.get("threshold", 0.5),
            recorder_kwargs={
                "end_of_speech_ms": audio_cfg.get("vad_end_of_speech_ms", 700),
            },
            wake_beep=wake_beep,
            processing_indicator=config.get("leds", {}).get("processing_indicator", False),
        )
        print("listening… say the wake word (Ctrl-C to quit)")
        try:
            await pipeline.run()
        finally:
            runner.cancel()
            if beep_server is not None:
                await beep_server.stop()
            if mock is not None:
                await mock.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
