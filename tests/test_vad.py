from rabbit_brain.audio.vad import VAD_CHUNK_SAMPLES, UtteranceRecorder

CHUNK = VAD_CHUNK_SAMPLES * 2  # bytes per VAD chunk (s16le)
SILENCE = b"\x00\x00" * VAD_CHUNK_SAMPLES
SPEECH = b"\x00\x10" * VAD_CHUNK_SAMPLES


def loud_probe(chunk: bytes) -> float:
    return 1.0 if any(chunk) else 0.0


def make_recorder(**kwargs) -> UtteranceRecorder:
    defaults = dict(
        probe=loud_probe,
        end_of_speech_ms=96,  # 3 chunks @ 32 ms
        start_timeout_s=0.32,  # 10 chunks
        max_utterance_s=1.0,
        pre_roll_ms=64,  # 2 chunks
    )
    defaults.update(kwargs)
    return UtteranceRecorder(**defaults)


def feed(recorder, chunks):
    emitted, done = [], False
    for chunk in chunks:
        out, done = recorder.push(chunk)
        emitted.extend(out)
        if done:
            break
    return emitted, done


def test_utterance_with_pre_roll_and_end_of_speech():
    rec = make_recorder()
    chunks = [SILENCE] * 5 + [SPEECH] * 8 + [SILENCE] * 10
    emitted, done = feed(rec, chunks)
    assert done and rec.got_speech
    # 2 pre-roll chunks + 8 speech + 3 closing silence (the end-of-speech run)
    assert len(emitted) == 2 + 8 + 3
    assert emitted[0] == SILENCE and emitted[2] == SPEECH


def test_start_timeout_without_speech():
    rec = make_recorder()
    emitted, done = feed(rec, [SILENCE] * 20)
    assert done and not rec.got_speech
    assert emitted == []


def test_max_utterance_cut():
    rec = make_recorder(max_utterance_s=0.32)  # 10 chunks
    emitted, done = feed(rec, [SPEECH] * 100)
    assert done and rec.got_speech
    assert len(emitted) <= 11


def test_brief_pause_does_not_end_utterance():
    rec = make_recorder()
    chunks = [SPEECH] * 3 + [SILENCE] * 2 + [SPEECH] * 3 + [SILENCE] * 5
    emitted, done = feed(rec, chunks)
    assert done
    # both speech bursts and the bridging pause are in the emitted stream
    assert emitted.count(SPEECH) == 6


def test_push_accepts_arbitrary_block_sizes():
    rec = make_recorder()
    stream = b"".join([SPEECH] * 4 + [SILENCE] * 6)
    emitted, done = [], False
    step = 700  # not a multiple of the chunk size
    for i in range(0, len(stream), step):
        out, done = rec.push(stream[i : i + step])
        emitted.extend(out)
        if done:
            break
    assert done and rec.got_speech
    assert b"".join(emitted).startswith(SPEECH)


def test_default_end_of_speech_is_1200ms():
    rec = UtteranceRecorder(probe=loud_probe)
    assert rec._silence_chunks == round(1200 / 32)
