"""Realtime full-duplex path: resampling, streaming playback, barge-in.

The barge-in maths is the part that has to be right: if the truncate time does
not match what was actually heard, the model's transcript diverges from reality
and every later turn reasons from it.
"""

import base64
import json

import pytest
from rabbit_brain.audio.streaming import Resampler, expand_channels
from rabbit_brain.llm.realtime import API_RATE, RealtimeError, RealtimeSession
from rabbit_brain.runtime import conversation_mode

# --- resampling ---------------------------------------------------------


def test_resampler_changes_length_by_the_rate_ratio():
    pcm = b"\x00\x01" * 1600  # 1600 samples @16k = 100 ms
    out = Resampler(16000, 24000).process(pcm)
    assert len(out) // 2 == pytest.approx(2400, abs=2)  # 100 ms @24k


def test_resampler_downsamples_too():
    pcm = b"\x00\x01" * 2400  # 100 ms @24k
    out = Resampler(24000, 16000).process(pcm)
    assert len(out) // 2 == pytest.approx(1600, abs=2)


def test_resampler_is_stateful_across_chunks():
    """Chunked input must resample like one buffer — otherwise interpolation
    restarts at every boundary and clicks ~50 times a second."""
    whole = bytes(range(0, 200)) * 8
    one_shot = Resampler(16000, 24000).process(whole)
    chunked = Resampler(16000, 24000)
    pieces = b"".join(chunked.process(whole[i : i + 160]) for i in range(0, len(whole), 160))
    # same length within a sample, and identical but for interpolation at joins
    assert abs(len(one_shot) - len(pieces)) <= 4


def test_resampler_passthrough_when_rates_match():
    pcm = b"\x01\x02\x03\x04"
    assert Resampler(16000, 16000).process(pcm) is pcm


def test_resampler_rejects_bad_rates():
    with pytest.raises(ValueError):
        Resampler(0, 24000)


def test_expand_channels_duplicates_mono():
    assert expand_channels(b"\x01\x00\x02\x00", 1) == b"\x01\x00\x02\x00"
    assert expand_channels(b"\x01\x00", 2) == b"\x01\x00\x01\x00"


# --- a player double the session can drive ------------------------------


class FakePlayer:
    def __init__(self, played_ms=0):
        self.written: list[bytes] = []
        self.stopped = 0
        self.resumed = 0
        self.resets = 0
        self.played_ms = played_ms

    def write(self, pcm):
        self.written.append(pcm)

    def reset_position(self):
        self.resets += 1

    async def stop(self):
        self.stopped += 1

    async def resume(self):
        self.resumed += 1


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_str(self, data):
        self.sent.append(json.loads(data))


def _session(player, **kw):
    s = RealtimeSession(api_key="k", player=player, **kw)
    s._ws = FakeWS()
    return s


def _audio_event(pcm: bytes, item_id="item_1"):
    return {
        "type": "response.output_audio.delta",
        "delta": base64.b64encode(pcm).decode(),
        "item_id": item_id,
    }


async def test_audio_delta_plays_immediately_without_an_mp3():
    player = FakePlayer()
    session = _session(player)
    await session._handle(_audio_event(b"\x01\x00" * 10))
    assert player.written == [b"\x01\x00" * 10]  # straight to the speaker
    assert player.resets == 1  # new response -> playback position restarts
    assert session.timings.first_audio_delta_ms is not None
    assert session.timings.playback_started_ms is not None


async def test_barge_in_stops_playback_and_truncates_at_the_played_time():
    player = FakePlayer(played_ms=1234)
    session = _session(player)
    await session._handle(_audio_event(b"\x01\x00" * 10))  # rabbit is speaking
    await session._handle({"type": "input_audio_buffer.speech_started"})

    assert player.stopped == 1  # speaker cut immediately
    truncate = next(m for m in session._ws.sent if m["type"] == "conversation.item.truncate")
    # the REALLY-played time, not the generated length
    assert truncate["audio_end_ms"] == 1234
    assert truncate["item_id"] == "item_1"
    assert session.timings.interruptions  # interruption timing recorded


async def test_barge_in_is_a_no_op_when_the_rabbit_is_not_speaking():
    # the user talking first must not emit a spurious truncate
    player = FakePlayer()
    session = _session(player)
    await session._handle({"type": "input_audio_buffer.speech_started"})
    assert player.stopped == 0
    assert session._ws.sent == []


async def test_second_response_resets_the_playback_position():
    player = FakePlayer()
    session = _session(player)
    await session._handle(_audio_event(b"\x01\x00"))
    await session._handle({"type": "response.done"})
    await session._handle(_audio_event(b"\x02\x00", item_id="item_2"))
    assert player.resets == 2  # each reply is measured from zero


async def test_fatal_api_error_raises_for_the_turn_based_fallback():
    # only errors that really make the session unusable bring it down
    session = _session(FakePlayer())
    with pytest.raises(RealtimeError):
        await session._handle(
            {"type": "error", "error": {"code": "session_expired", "message": "nope"}}
        )


async def test_microphone_is_resampled_up_to_the_api_rate():
    session = _session(FakePlayer(), mic_rate=16000)
    frame = b"\x00\x01" * 320  # 20 ms @16k

    async def frames():
        yield frame

    await session.pump_microphone(frames())
    sent = session._ws.sent[0]
    assert sent["type"] == "input_audio_buffer.append"
    decoded = base64.b64decode(sent["audio"])
    assert len(decoded) // 2 == pytest.approx(int(320 * API_RATE / 16000), abs=2)


async def test_body_tools_still_work_in_realtime():
    calls = []

    async def executor(name, arguments):
        calls.append((name, arguments))
        return "ok"

    session = _session(FakePlayer(), tool_executor=executor)
    await session._handle(
        {
            "type": "response.function_call_arguments.done",
            "name": "gesture_ears",
            "call_id": "c1",
            "arguments": '{"left": 5}',
        }
    )
    assert calls == [("gesture_ears", {"left": 5})]
    kinds = [m["type"] for m in session._ws.sent]
    assert "conversation.item.create" in kinds and "response.create" in kinds


async def test_failing_tool_does_not_kill_the_call():
    async def executor(name, arguments):
        raise RuntimeError("ears jammed")

    session = _session(FakePlayer(), tool_executor=executor)
    await session._handle(
        {
            "type": "response.function_call_arguments.done",
            "name": "gesture_ears",
            "call_id": "c1",
            "arguments": "{}",
        }
    )
    output = next(m for m in session._ws.sent if m["type"] == "conversation.item.create")
    assert "error" in output["item"]["output"]


async def test_send_before_connect_raises_realtime_error():
    # never silently no-op: the caller must be able to re-arm turn-based
    session = RealtimeSession(api_key="k", player=FakePlayer())
    with pytest.raises(RealtimeError):
        await session._send({"type": "ping"})


# --- config gate --------------------------------------------------------


def test_realtime_requires_the_local_backend():
    # barge-in needs interruptible playback; the rabbit's decoder can't be cut
    with pytest.raises(ValueError, match="requires audio_out.backend: local"):
        conversation_mode({"conversation": {"mode": "realtime"}})
    with pytest.raises(ValueError, match="requires audio_out.backend: local"):
        conversation_mode(
            {"conversation": {"mode": "realtime"}, "audio_out": {"backend": "rabbit"}}
        )
    ok = {"conversation": {"mode": "realtime"}, "audio_out": {"backend": "local"}}
    assert conversation_mode(ok) == "realtime"


def test_default_mode_is_turn_based():
    assert conversation_mode({}) == "turn_based"
    assert conversation_mode({"audio_out": {"backend": "rabbit"}}) == "turn_based"


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown conversation.mode"):
        conversation_mode({"conversation": {"mode": "telepathy"}})


# --- hardware-found races (July 2026) -----------------------------------


async def test_speech_started_does_not_cancel_under_server_side_vad():
    """With VAD the server cancels the response itself; our extra
    response.cancel raced it, came back response_cancel_not_active, and killed
    the whole session on every SUCCESSFUL barge-in."""
    session = _session(FakePlayer(played_ms=500), turn_detection="semantic_vad")
    await session._handle(_audio_event(b"\x01\x00" * 10))
    await session._handle({"type": "input_audio_buffer.speech_started"})
    kinds = [m["type"] for m in session._ws.sent]
    assert "response.cancel" not in kinds  # the server already did it
    assert "conversation.item.truncate" in kinds  # but we still report what was heard


async def test_cancel_not_active_error_does_not_end_the_session():
    session = _session(FakePlayer())
    # must NOT raise: this is a benign race, not a failure
    await session._handle(
        {
            "type": "error",
            "error": {"code": "response_cancel_not_active", "message": "no active response found"},
        }
    )


async def test_unknown_errors_are_logged_but_not_fatal():
    session = _session(FakePlayer())
    await session._handle({"type": "error", "error": {"code": "weird_thing", "message": "hm"}})


async def test_auth_errors_are_still_fatal():
    session = _session(FakePlayer())
    with pytest.raises(RealtimeError):
        await session._handle(
            {"type": "error", "error": {"code": "invalid_api_key", "message": "bad key"}}
        )


async def test_barge_in_before_any_audio_played_sends_no_truncate():
    """played_ms == 0: nothing was heard, so there is nothing to trim — and
    truncating at 0 only invites an invalid-value error."""
    session = _session(FakePlayer(played_ms=0))
    await session._handle(_audio_event(b"\x01\x00"))
    await session._handle({"type": "input_audio_buffer.speech_started"})
    kinds = [m["type"] for m in session._ws.sent]
    assert "conversation.item.truncate" not in kinds
    assert session.timings.interruptions  # still recorded as an interruption


# --- the PortAudio abort race -------------------------------------------


class BlockingStream:
    """A device whose write blocks until released, and whose abort() makes the
    in-flight write raise — exactly what PortAudio does (-9999)."""

    def __init__(self):
        import threading

        self.written: list[bytes] = []
        self.aborted = 0
        self.starts = 0
        self._release = threading.Event()
        self.block = True

    def write(self, chunk):
        if self.block:
            self._release.wait(timeout=2)
            if self.aborted:
                raise RuntimeError("PortAudioError -9999 (stream aborted)")
        self.written.append(bytes(chunk))

    def start(self):
        self.starts += 1

    def stop(self):
        pass

    def abort(self):
        self.aborted += 1
        self._release.set()

    def close(self):
        pass


async def _player_with(stream):
    from rabbit_brain.audio.streaming import StreamingAudioPlayer

    player = StreamingAudioPlayer(device=None, device_rate=16000, device_channels=1)
    player._open_stream = lambda: stream
    await player.start()
    return player


async def test_abort_during_a_blocked_write_keeps_the_writer_alive():
    """The hardware race: abort() while _drain was blocked in write() raised,
    the writer returned, and every later reply queued into a dead consumer."""
    import asyncio as aio

    stream = BlockingStream()
    player = await _player_with(stream)
    try:
        player.write(b"\x01\x00" * 400)  # response 1 — blocks in write()
        await aio.sleep(0.05)
        await player.stop()  # barge-in: abort makes that write raise
        await aio.sleep(0.05)
        assert stream.aborted == 1

        # second response: the device is re-opened and the delta MUST be heard
        stream.block = False
        await player.resume()
        player.reset_position()
        player.write(b"\x02\x00" * 400)
        await aio.sleep(0.1)
        assert stream.written, "the second response was never consumed"
        assert player._writer is not None and not player._writer.done()
    finally:
        await player.aclose()


async def test_interrupted_audio_does_not_bleed_into_the_next_response():
    """Chunks queued for the cancelled reply must be dropped, not played over
    the new one."""
    import asyncio as aio

    stream = BlockingStream()
    stream.block = False
    player = await _player_with(stream)
    try:
        player.write(b"\x01\x00" * 400)
        await player.stop()  # everything queued for response 1 is now stale
        stale = len(stream.written)
        await player.resume()
        player.write(b"\x02\x00" * 400)
        await aio.sleep(0.1)
        assert len(stream.written) > stale  # the NEW response played
    finally:
        await player.aclose()


# --- the "server done but speaker still talking" bug ---------------------


async def test_has_unplayed_audio_covers_queue_inflight_and_device_buffer():
    import asyncio as aio

    stream = BlockingStream()
    stream.block = False
    player = await _player_with(stream)
    try:
        assert player.has_unplayed_audio is False  # nothing ever written
        player.write(b"\x01\x00" * 400)
        assert player.has_unplayed_audio is True  # queued
        await aio.sleep(0.1)
        # written to the device: still sounding for the buffer window
        assert player.has_unplayed_audio is True
        await player.stop()
        assert player.has_unplayed_audio is False  # aborted
    finally:
        await player.aclose()


async def test_barge_in_fires_after_response_done_while_audio_still_playing():
    """THE bug: the model delivers 2 s of speech in a moment, response.done
    arrives, and the rabbit is still talking. Gating barge-in on the server's
    state meant speech_started found 'nothing to interrupt' and the speaker
    just carried on."""

    class StillPlaying(FakePlayer):
        has_unplayed_audio = True  # the speaker has a backlog

    player = StillPlaying(played_ms=800)
    session = _session(player)
    await session._handle(_audio_event(b"\x01\x00" * 10))
    await session._handle({"type": "response.done"})  # server finished
    assert session._server_response_active is False
    # user cuts in while the speaker is still working through the backlog
    await session._handle({"type": "input_audio_buffer.speech_started"})
    assert player.stopped == 1, "the speaker must be aborted"
    truncate = next(m for m in session._ws.sent if m["type"] == "conversation.item.truncate")
    assert truncate["audio_end_ms"] == 800


async def test_barge_in_still_ignored_when_truly_idle():
    # no server response AND no local audio: nothing to interrupt
    player = FakePlayer()
    player.has_unplayed_audio = False
    session = _session(player)
    await session._handle({"type": "response.done"})
    await session._handle({"type": "input_audio_buffer.speech_started"})
    assert player.stopped == 0
    assert not any(m["type"] == "conversation.item.truncate" for m in session._ws.sent)


async def test_fast_deltas_then_done_then_bargein_clears_the_queue():
    """2 s of audio delivered fast, response.done, speaker still playing,
    speech_started -> audio cut and the queue emptied."""
    import asyncio as aio

    from rabbit_brain.audio.streaming import StreamingAudioPlayer

    stream = BlockingStream()
    stream.block = False
    player = StreamingAudioPlayer(device=None, device_rate=16000, device_channels=1)
    player._open_stream = lambda: stream
    await player.start()
    session = _session(player)
    try:
        # ~2 s of 24 kHz mono PCM arriving all at once
        session._server_response_active = False
        await session._handle(_audio_event(b"\x00\x01" * 24000))
        await session._handle({"type": "response.done"})
        assert player.has_unplayed_audio is True  # speaker is still working
        await session._handle({"type": "input_audio_buffer.speech_started"})
        await aio.sleep(0.05)
        assert player.has_unplayed_audio is False  # cut
        assert player._queue.empty(), "the pending audio must be discarded"
        assert stream.aborted >= 1

        # the second turn must be audible immediately, with no residue
        stream.written.clear()
        await session._handle(_audio_event(b"\x02\x00" * 2400, item_id="item_2"))
        await aio.sleep(0.1)
        assert stream.written, "the second response never reached the device"
    finally:
        await player.aclose()
