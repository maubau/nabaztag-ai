import asyncio

import pytest
from rabbit_brain.body.mock_ojn import MOCK_SERIAL
from rabbit_brain.body.ojn_adapter import (
    INTER_URL_GAP_S,
    OjnAdapter,
    OjnError,
    led_spec_to_chor,
)
from rabbit_brain.body.types import EarsCommand, LedSpec


async def test_set_ears_sends_vapi_positions(adapter, mock_ojn):
    await adapter.set_ears(3, 14)
    calls = mock_ojn.calls_of("ears")
    assert len(calls) == 1
    assert calls[0].params == {"posleft": "3", "posright": "14"}
    assert mock_ojn.ears == (3, 14)


async def test_bad_token_raises(mock_ojn):
    async with OjnAdapter(mock_ojn.base_url, MOCK_SERIAL, "wrong-token") as a:
        with pytest.raises(OjnError, match="NOGOODTOKENORSERIAL"):
            await a.set_ears(0, 0)


async def test_ears_command_validates_range():
    with pytest.raises(ValueError):
        EarsCommand(17, 0)
    with pytest.raises(ValueError):
        EarsCommand(0, -1)


async def test_set_leds_compiles_valid_chor(adapter, mock_ojn):
    await adapter.set_leds(LedSpec.from_dict({"nose": (255, 128, 0)}))
    calls = mock_ojn.calls_of("chor")
    assert len(calls) == 1
    # nose is LED index 2 (choregraphy.h)
    assert calls[0].params["chor"] == "10,0,led,2,255,128,0"


def test_led_spec_to_chor_pulse_adds_off_and_on_frames():
    chor = led_spec_to_chor(LedSpec.from_dict({"top": (0, 0, 255)}, pulse=True))
    fields = chor.split(",")
    assert (len(fields) - 1) % 6 == 0  # Choregraphy::Parse validity rule
    assert fields[0] == "10"
    assert chor.count("led") == 3  # on, off, on


def test_led_spec_rejects_unknown_led_and_bad_rgb():
    with pytest.raises(ValueError, match="unknown LED"):
        LedSpec.from_dict({"tail": (0, 0, 0)})
    with pytest.raises(ValueError, match="RGB out of range"):
        LedSpec.from_dict({"nose": (0, 300, 0)})


async def test_play_audio_sends_urllist_and_times_out_playback(adapter, mock_ojn):
    urls = ("http://bolt:8090/a.mp3", "http://bolt:8090/b.mp3")
    handle = await adapter.play_audio(urls, duration_s=0.05)
    assert mock_ojn.calls_of("stream")[0].params["urlList"] == "|".join(urls)
    await asyncio.wait_for(handle.wait_started(), 1)
    assert not handle.finished
    # wall-time estimate = MP3 duration + one inter-URL gap (measured ~1.7s)
    assert handle.estimated_duration_s == pytest.approx(0.05 + INTER_URL_GAP_S)
    await asyncio.wait_for(handle.wait_finished(), 4)  # + 0.3s guard


async def test_playback_handle_cannot_cancel(adapter):
    handle = await adapter.play_audio(("http://x/a.mp3",), duration_s=0.05)
    assert adapter.capabilities.can_cancel_audio is False
    with pytest.raises(NotImplementedError):
        await handle.cancel()
    await handle.wait_finished()


async def test_say_uses_ojn_tts_plugin(adapter, mock_ojn):
    handle = await adapter.say("ciao mondo")
    assert mock_ojn.calls_of("tts_say")[0].params["text"] == "ciao mondo"
    assert handle.estimated_duration_s >= 2.0


async def test_sleep_wake_actions(adapter, mock_ojn):
    await adapter.sleep()
    assert mock_ojn.sleeping is True
    await adapter.wake()
    assert mock_ojn.sleeping is False
    assert [c.params["action"] for c in mock_ojn.calls_of("action")] == ["14", "13"]


async def test_capabilities_match_gate_g0_matrix(adapter):
    caps = adapter.capabilities
    assert caps.can_cancel_audio is False
    assert caps.has_playback_events is False
    assert caps.can_read_body_state is False
    assert caps.has_per_led_rgb is True
    assert caps.ear_range == (0, 16)
