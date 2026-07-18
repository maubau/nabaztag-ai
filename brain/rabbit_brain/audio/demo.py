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
from ..stt import make_stt
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
        )
        print("listening… say the wake word (Ctrl-C to quit)")
        try:
            await pipeline.run()
        finally:
            runner.cancel()
            if mock is not None:
                await mock.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
