"""Single persistent voice runtime (§6, §8):

    python -m rabbit_brain.runtime --config config.yaml

One process owns exactly one of each: OjnAdapter, BodyController, Mp3Server
(:8090), EventListener (:8091), STT provider, TTS/Speaker, LLM provider,
AgentLoop and VoicePipeline. Full pipeline:

    reSpeaker → openWakeWord → silero VAD → Deepgram nova-3 (multi) → OpenAI
    Responses → ElevenLabs → local MP3 → OpenJabNab → Nabaztag speaker.

IMPORTANT: do NOT run this and the MCP server at the same time — both bind
:8090/:8091 and each creates its own BodyController, breaking single-ownership
of the body. Pick one for a session (README). A future MCP-as-thin-client over
local IPC would let both coexist.

Half-duplex (§6.2.7): while the rabbit speaks (agent reply or wake beep) the
VoicePipeline gates the mic and holds openWakeWord in reset for the playback
timer (+guard), then returns to listening automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

import yaml

from .audio.capture import AlsaCapture
from .audio.doa import make_doa
from .audio.pipeline import VoicePipeline
from .audio.vad import DEFAULT_END_OF_SPEECH_MS, SileroProbe
from .audio.wake import OpenWakeWordDetector
from .body.controller import BodyController
from .body.events_server import EventListener
from .body.mock_ojn import MOCK_SERIAL, MOCK_VAPI_TOKEN, MockOjnServer
from .body.ojn_adapter import OjnAdapter
from .body.types import PlayAudioCommand
from .llm import AgentConfig, AgentLoop, BodyTools, make_llm_provider
from .stt import make_stt
from .tts import build_speech_stack

log = logging.getLogger(__name__)

SYSTEM_PROMPT_PATH = Path("prompts/system.md")
TEARDOWN_STEP_TIMEOUT_S = 5.0


async def _teardown_step(name: str, coro) -> None:
    """One shutdown step, bounded so a stuck one (a hung network close, a
    task ignoring cancellation) can't block the rest of teardown or the final
    'nabaztag runtime stopped' log — hardware round, July 2026: the process
    never printed it after several Ctrl-C."""
    try:
        await asyncio.wait_for(coro, TEARDOWN_STEP_TIMEOUT_S)
    except TimeoutError:
        log.warning("teardown step %r exceeded %.0fs, continuing", name, TEARDOWN_STEP_TIMEOUT_S)
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("teardown step %r failed, continuing", name)


def _load_yaml(path: str) -> dict:
    return yaml.safe_load(_read_text(path)) or {}


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_env_file(path: str | Path) -> int:
    """Load KEY=VALUE lines from a .env file into os.environ (existing
    variables win, so an exported shell env still overrides the file).
    Returns the number of keys set. Values are never logged."""
    p = Path(path)
    if not p.exists():
        return 0
    loaded = 0
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _capture_from_config(audio_cfg: dict) -> AlsaCapture:
    kwargs = {}
    if "capture_queue_blocks" in audio_cfg:
        kwargs["queue_blocks"] = audio_cfg["capture_queue_blocks"]
    return AlsaCapture(
        device=audio_cfg.get(
            "capture_device_index", audio_cfg.get("capture_device", "hw:CARD=C16K6Ch,DEV=0")
        ),
        sample_rate=audio_cfg.get("sample_rate", 16_000),
        channels=audio_cfg.get("channels", 6),
        selected_channel=audio_cfg.get("selected_channel", 0),
        **kwargs,
    )


async def run(config_path: str, moods_path: str, system_prompt_path: str) -> None:
    config = _load_yaml(config_path)
    moods = _load_yaml(moods_path)
    audio_cfg = config.get("audio", {})
    wake_cfg = config.get("wake", {})
    beep_cfg = config.get("wake_beep", {})
    system_prompt = _read_text(system_prompt_path)

    if os.environ.get("NABAZTAG_MOCK_OJN"):
        mock = MockOjnServer()
        await mock.start()
        base_url, serial, token = mock.base_url, MOCK_SERIAL, MOCK_VAPI_TOKEN
    else:
        mock = None
        base_url = os.environ.get("OJN_BASE_URL", "http://127.0.0.1")
        serial, token = os.environ["RABBIT_SERIAL"], os.environ["OJN_VAPI_TOKEN"]

    stop = asyncio.Event()
    async with OjnAdapter(base_url, serial, token) as adapter:
        listener = EventListener(
            adapter.push_event,
            port=int(os.environ.get("NABAZTAG_EVENTS_PORT", "8091")),
            serial=serial,
        )
        await listener.start()
        controller = BodyController(adapter)
        controller_task = asyncio.create_task(controller.run())

        protected = {Path(beep_cfg["mp3"]).name} if beep_cfg.get("mp3") else None
        speech = await build_speech_stack(controller, protected_assets=protected)
        if speech.speaker is None:
            log.warning("no TTS_PROFILE set — the rabbit will stay silent")

        wake_beep = None
        if beep_cfg.get("enabled") and beep_cfg.get("mp3") and speech.mp3_server is not None:
            url = speech.mp3_server.url_for(Path(beep_cfg["mp3"]))
            wake_beep = PlayAudioCommand((url,), beep_cfg.get("duration_ms", 250) / 1000)

        llm = make_llm_provider(config)
        agent = AgentLoop(
            provider=llm,
            tools=BodyTools(controller, get_direction=lambda: pipeline.last_doa_deg),
            system_prompt=system_prompt,
            speaker=speech.speaker,
            config=AgentConfig(
                max_history_turns=config.get("llm", {}).get("max_history_turns", 20),
                max_tool_rounds=config.get("llm", {}).get("max_tool_rounds", 4),
            ),
        )

        async def on_transcript(text: str) -> None:
            # forward the STT-detected utterance language for TTS voice routing
            await agent.handle(text, language=pipeline.last_stt_language)

        pipeline = VoicePipeline(
            capture=_capture_from_config(audio_cfg),
            wake=OpenWakeWordDetector(models=tuple(wake_cfg.get("models", ["hey_jarvis"]))),
            probe_factory=SileroProbe,
            stt=make_stt(config),
            controller=controller,
            on_transcript=on_transcript,
            doa=make_doa(config),
            doa_moods=moods.get("doa", {}),
            wake_threshold=wake_cfg.get("threshold", 0.5),
            recorder_kwargs={
                "end_of_speech_ms": audio_cfg.get("vad_end_of_speech_ms", DEFAULT_END_OF_SPEECH_MS)
            },
            wake_beep=wake_beep,
            processing_indicator=config.get("leds", {}).get("processing_indicator", False),
        )

        async def watch_events() -> None:
            async for _event in adapter.events():
                pass  # RFID intents (§6.2.9) land here in a later phase

        events_task = asyncio.create_task(watch_events())
        pipeline_task = asyncio.create_task(pipeline.run())
        pipeline_task.add_done_callback(lambda _t: stop.set())

        force_stop = asyncio.Event()

        def _on_signal() -> None:
            if stop.is_set():
                log.warning("second interrupt received, forcing an immediate exit")
                force_stop.set()
            else:
                log.info("stopping (interrupt again to force-quit)")
                stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _on_signal)

        log.info("nabaztag runtime listening (say the wake word; Ctrl-C to stop)")
        await stop.wait()

        async def _teardown() -> None:
            # ordered teardown: stop input, then body, then servers/sessions.
            # Every step is bounded (_teardown_step) so one stuck step (a hung
            # network close, a task ignoring cancellation) can't swallow the
            # rest or the final log line.
            pipeline_task.cancel()
            await _teardown_step("pipeline_task", pipeline_task)
            await _teardown_step("pipeline.aclose", pipeline.aclose())
            events_task.cancel()
            controller_task.cancel()
            await _teardown_step("events_task", events_task)
            await _teardown_step("controller_task", controller_task)
            await _teardown_step("listener.stop", listener.stop())
            await _teardown_step("speech.aclose", speech.aclose())
            if hasattr(llm, "aclose"):
                await _teardown_step("llm.aclose", llm.aclose())
            if mock is not None:
                await _teardown_step("mock.stop", mock.stop())
            log.info("nabaztag runtime stopped")

        teardown_task = asyncio.ensure_future(_teardown())
        force_task = asyncio.ensure_future(force_stop.wait())
        try:
            done, _pending = await asyncio.wait(
                {teardown_task, force_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if force_task in done and teardown_task not in done:
                log.warning("teardown did not finish before the forced exit — skipping the rest")
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(1)  # tee/pipes may buffer; a clean return can't be guaranteed here
        finally:
            force_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await force_task
        sys.stdout.flush()
        sys.stderr.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Nabaztag voice runtime")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--moods", default="moods.yaml")
    parser.add_argument("--system-prompt", default=str(SYSTEM_PROMPT_PATH))
    parser.add_argument(
        "--env-file",
        default=".env",
        help="KEY=VALUE file loaded into the environment (shell env wins); '' to disable",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper())
    if args.env_file:
        loaded = load_env_file(args.env_file)
        if loaded:
            log.info("loaded %d variable(s) from %s", loaded, args.env_file)
    asyncio.run(run(args.config, args.moods, args.system_prompt))


if __name__ == "__main__":
    main()
