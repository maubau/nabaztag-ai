"""Local audio output backend (audio_out.backend: local) — no real device.

The point of this backend is that the output becomes SWAPPABLE without a second
pipeline: the controller keeps its audio lane, half-duplex gate and PLAYING
state, and only the thing that renders the sound changes.
"""

import asyncio
from pathlib import Path

import pytest
from rabbit_brain.audio.output import (
    LocalAudioPlayer,
    LocalPlaybackHandle,
    local_path_for,
    resolve_output_device,
)
from rabbit_brain.body.controller import BodyController
from rabbit_brain.body.types import BodyCapabilities, PlayAudioCommand, Priority


def test_resolve_output_device_matches_card_token():
    devices = [
        {"name": "HDA Intel PCH", "max_input_channels": 2, "max_output_channels": 0},
        {"name": "reSpeaker C16K6Ch", "max_input_channels": 6, "max_output_channels": 2},
    ]
    # ALSA-style string reduced to its card token, output-capable device wins
    assert resolve_output_device(devices, "hw:CARD=C16K6Ch,DEV=0") == 1
    assert resolve_output_device(devices, "respeaker") == 1
    assert resolve_output_device(devices, "nope") is None


def test_resolve_output_device_ignores_capture_only_devices():
    # the reSpeaker's CAPTURE endpoint must not be mistaken for an output
    devices = [{"name": "C16K6Ch capture", "max_input_channels": 6, "max_output_channels": 0}]
    assert resolve_output_device(devices, "C16K6Ch") is None


def test_local_path_for_maps_mp3_url_back_to_disk():
    audio_dir = Path("/srv/audio")
    # the Speaker hands out Mp3Server URLs (the RABBIT fetches them over HTTP);
    # played locally we want the file itself
    assert local_path_for("http://192.168.66.1/brain-audio/utt.mp3", audio_dir) == (
        audio_dir / "utt.mp3"
    )
    assert local_path_for("utt.mp3", audio_dir) == audio_dir / "utt.mp3"
    # percent-encoding survives the round trip
    assert local_path_for("http://h/a%20b.mp3", audio_dir) == audio_dir / "a b.mp3"


async def test_play_audio_rejects_missing_file(tmp_path):
    player = LocalAudioPlayer(tmp_path)
    with pytest.raises(FileNotFoundError):
        await player.play_audio(("http://h/nope.mp3",), 1.0)


async def test_handle_reports_real_completion():
    # unlike the rabbit's estimated timer, the handle finishes with the audio
    done = asyncio.Event()

    async def fake_play():
        await done.wait()

    handle = LocalPlaybackHandle(asyncio.create_task(fake_play()), asyncio.Event())
    waiter = asyncio.create_task(handle.wait_finished())
    await asyncio.sleep(0)
    assert not waiter.done()  # still playing
    done.set()
    await asyncio.wait_for(waiter, timeout=1)


async def test_cancel_signals_and_awaits_the_player():
    # instant cancel is what makes barge-in possible (OJN cannot cancel audio)
    cancel = asyncio.Event()
    stopped = []

    async def fake_play():
        await cancel.wait()
        stopped.append(True)

    handle = LocalPlaybackHandle(asyncio.create_task(fake_play()), cancel)
    await asyncio.wait_for(handle.cancel(), timeout=1)
    assert stopped == [True]


class _DoneHandle:
    async def wait_finished(self):
        return None

    async def cancel(self):
        return None


class _MinimalAdapter:
    """Just the audio slice the controller touches."""

    def __init__(self):
        self.capabilities = BodyCapabilities(
            can_cancel_audio=False,
            has_playback_events=False,
            can_read_body_state=False,
            has_per_led_rgb=True,
        )
        self.played: list[tuple[str, ...]] = []

    async def play_audio(self, urls, duration_s):
        self.played.append(urls)
        return _DoneHandle()


class _RecordingPlayer:
    """Stands in for LocalAudioPlayer: duck-types the adapter's audio slice."""

    def __init__(self):
        self.played: list[tuple[str, ...]] = []

    async def play_audio(self, urls, duration_s):
        self.played.append(urls)
        return _DoneHandle()


async def _run_one_audio_command(audio_player):
    adapter = _MinimalAdapter()
    controller = BodyController(adapter, audio_player=audio_player)
    task = asyncio.create_task(controller.run())
    try:
        await controller.submit(
            PlayAudioCommand(("http://h/a.mp3",), 1.0), Priority.USER_SPEECH_SYNC
        )
        await asyncio.wait_for(controller.wait_idle(), timeout=2)
    finally:
        task.cancel()
    return adapter, controller


async def test_controller_routes_audio_to_the_local_player():
    """The design claim: with a local player the controller renders audio there
    instead of on the rabbit, and nothing else changes."""
    player = _RecordingPlayer()
    adapter, _ = await _run_one_audio_command(player)
    assert player.played == [("http://h/a.mp3",)]
    assert adapter.played == []  # the rabbit's speaker was NOT used


async def test_controller_without_player_still_uses_the_rabbit():
    adapter, _ = await _run_one_audio_command(None)
    assert adapter.played == [("http://h/a.mp3",)]


def test_local_backend_enables_cancel():
    # OJN can't cancel audio; a local device can, so barge-in is possible
    # exactly when the local backend is in use
    assert BodyController(_MinimalAdapter()).can_cancel_audio is False
    assert BodyController(_MinimalAdapter(), audio_player=_RecordingPlayer()).can_cancel_audio


def test_runtime_config_selects_the_backend():
    from rabbit_brain.runtime import _audio_player_from_config

    assert _audio_player_from_config({}) is None  # default: the rabbit
    assert _audio_player_from_config({"audio_out": {"backend": "rabbit"}}) is None
    player = _audio_player_from_config({"audio_out": {"backend": "local", "device": "C16K6Ch"}})
    assert isinstance(player, LocalAudioPlayer)
    with pytest.raises(ValueError, match="unknown audio_out.backend"):
        _audio_player_from_config({"audio_out": {"backend": "carrier-pigeon"}})


def test_audio_player_argument_is_additive():
    # existing call sites (MCP, tests) construct BodyController(adapter) only
    assert BodyController.__init__.__defaults__ == (None,)
