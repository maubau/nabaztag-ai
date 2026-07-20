"""Agent loop with a fake LLM provider (no real API calls in CI)."""

import asyncio

from rabbit_brain.body.types import ChorCommand, EarsCommand
from rabbit_brain.llm import AgentConfig, AgentLoop, BodyTools, LLMResult, ToolCall
from rabbit_brain.llm.base import ToolTurn, UserTurn

SYSTEM = "you are a rabbit"


class FakeLLM:
    """Returns scripted LLMResults in sequence; records what it was asked."""

    def __init__(self, *results, raises: Exception | None = None):
        self._results = list(results)
        self._raises = raises
        self.calls = 0
        self.last_history = None
        self.last_tools = None

    async def respond(self, system, history, tools, on_text_delta=None, on_output_delta=None):
        self.calls += 1
        self.last_history = list(history)
        self.last_tools = list(tools)
        if self._raises is not None:
            raise self._raises
        result = self._results.pop(0) if self._results else LLMResult(text="")
        # a real provider signals first-output for tool arguments too, not
        # just visible text (that's the whole point of on_output_delta)
        if on_output_delta and (result.text or result.tool_calls):
            on_output_delta()
        if on_text_delta and result.text:
            r = on_text_delta(result.text)
            if r is not None:
                await r
        return result


class FakeSpeaker:
    def __init__(self, raises: Exception | None = None):
        self.spoken = []
        self.languages = []
        self._raises = raises

    async def speak(self, text, priority=None, language=None, on_checkpoint=None):
        if self._raises is not None:
            raise self._raises
        self.spoken.append(text)
        self.languages.append(language)
        if on_checkpoint is not None:
            for name in ("tts_start", "tts_first_chunk_ready", "tts_complete", "all_submitted"):
                on_checkpoint(name)
        return 1.5


class RecordingController:
    def __init__(self):
        self.submitted = []

    async def submit(self, cmd, priority, deadline=None):
        self.submitted.append((cmd, priority))

    def snapshot(self):
        from rabbit_brain.body.types import BodyState

        return BodyState(ears=(0, 0), leds={}, playing=False)


def make_agent(llm, speaker=None, controller=None, **cfg):
    ctrl = controller or RecordingController()
    return AgentLoop(
        provider=llm,
        tools=BodyTools(ctrl),
        system_prompt=SYSTEM,
        speaker=speaker,
        config=AgentConfig(**cfg),
    )


def tool_call(name, cid="c1", **args):
    return ToolCall(call_id=cid, name=name, arguments=args)


async def test_transcript_to_speaker():
    llm = FakeLLM(LLMResult(text="Ciao! Sto bene, grazie."))
    speaker = FakeSpeaker()
    agent = make_agent(llm, speaker)
    out = await agent.handle("Nabaztag, come stai oggi?")
    assert out == "Ciao! Sto bene, grazie."
    assert speaker.spoken == ["Ciao! Sto bene, grazie."]
    # the transcript entered the history as a user turn
    assert isinstance(agent.history[0], UserTurn)


async def test_function_call_reaches_controller():
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(tool_calls=[tool_call("gesture_ears", left=2, right=2)]),
        LLMResult(text="Fatto!"),
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    await agent.handle("muovi le orecchie")
    # the gesture reached the body as a ChorCommand (never EarsCommand)
    cmds = [c for c, _ in ctrl.submitted]
    assert any(isinstance(c, ChorCommand) for c in cmds)
    assert not any(isinstance(c, EarsCommand) for c in cmds)
    # and the tool result was fed back to the model (second call happened)
    assert llm.calls == 2
    assert any(isinstance(m, ToolTurn) for m in agent.history)


async def test_multiple_tool_calls_in_one_turn():
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(
            tool_calls=[
                tool_call("set_mood_lights", cid="a", mood="happy"),
                tool_call("gesture_ears", cid="b", left=0, right=0),
            ]
        ),
        LLMResult(text="Ecco!"),
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    await agent.handle("festeggia")
    assert len(ctrl.submitted) == 2  # both tools ran
    tool_turn = next(m for m in agent.history if isinstance(m, ToolTurn))
    assert len(tool_turn.results) == 2


async def test_expressive_tool_with_text_skips_followup_round():
    """Hardware round, July 2026: a gesture the model didn't need a reply
    about was costing a whole extra OpenAI round-trip (~4-5s). If the model
    already gives final text alongside a purely expressive tool call
    (gesture_ears/set_mood_lights/play_gesture), execute the tool and use
    that text directly — no second call."""
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(text="Ecco le orecchie!", tool_calls=[tool_call("gesture_ears", left=2, right=2)])
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    out = await agent.handle("muovi le orecchie")
    assert out == "Ecco le orecchie!"
    assert llm.calls == 1  # no follow-up round
    cmds = [c for c, _ in ctrl.submitted]
    assert any(isinstance(c, ChorCommand) for c in cmds)  # the gesture still ran
    assert any(isinstance(m, ToolTurn) for m in agent.history)  # result still recorded


async def test_informational_tool_still_forces_followup_round():
    """get_direction/body_state need their result fed back — the model can't
    answer without it, so the follow-up round must still happen even if the
    model also produced some text in the same response."""
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(text="Vediamo...", tool_calls=[tool_call("get_direction")]),
        LLMResult(text="Sei a nord."),
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    out = await agent.handle("da dove ti parlo?")
    assert out == "Sei a nord."
    assert llm.calls == 2


async def test_mixed_expressive_and_informational_forces_followup_round():
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(
            text="Un attimo...",
            tool_calls=[
                tool_call("gesture_ears", cid="a", left=1, right=1),
                tool_call("get_direction", cid="b"),
            ],
        ),
        LLMResult(text="Sei a nord, e ho mosso le orecchie."),
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    out = await agent.handle("dimmi da dove e muovi le orecchie")
    assert out == "Sei a nord, e ho mosso le orecchie."
    assert llm.calls == 2


async def test_express_tool_carries_reply_skips_followup_round():
    """The benchmark (July 2026) showed the model does NOT reliably produce
    free text alongside a separate gesture tool call in the same response —
    the realistic case is EMPTY free text plus an express() call carrying the
    reply. AgentLoop must still resolve in one round by reading spoken_text
    out of the tool call's own arguments, not the response's free-text field."""
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(
            tool_calls=[tool_call("express", spoken_text="Ecco le orecchie!", gesture="wiggle")]
        )
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    out = await agent.handle("saluta muovendo le orecchie")
    assert out == "Ecco le orecchie!"
    assert llm.calls == 1
    cmds = [c for c, _ in ctrl.submitted]
    assert any(isinstance(c, ChorCommand) for c in cmds)


async def test_express_with_bad_gesture_still_speaks():
    """A tool-level validation failure (bad gesture/mood) must not silence
    the reply — spoken_text is pulled from the raw arguments independently of
    whether the tool execution itself succeeded."""
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(
            tool_calls=[tool_call("express", spoken_text="Ciao comunque!", gesture="backflip")]
        )
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    out = await agent.handle("fai un salto e saluta")
    assert out == "Ciao comunque!"
    assert ctrl.submitted == []  # the bad gesture never reached the body


async def test_express_with_informational_tool_still_forces_followup():
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(
            tool_calls=[
                tool_call("express", cid="a", spoken_text="Un attimo..."),
                tool_call("get_direction", cid="b"),
            ]
        ),
        LLMResult(text="Sei a nord."),
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    out = await agent.handle("da dove mi parli?")
    assert out == "Sei a nord."
    assert llm.calls == 2


async def test_invalid_tool_call_recovers():
    ctrl = RecordingController()
    llm = FakeLLM(
        LLMResult(tool_calls=[tool_call("gesture_ears", left=0, right=99)]),  # out of range
        LLMResult(text="Scusa, riprovo."),
    )
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl)
    out = await agent.handle("muovi")
    assert out == "Scusa, riprovo."
    assert ctrl.submitted == []  # bad gesture never reached the body
    tool_turn = next(m for m in agent.history if isinstance(m, ToolTurn))
    assert tool_turn.results[0].output.startswith("error")


async def test_stt_language_forwarded_to_tts():
    # the STT-detected language reaches the speaker for voice routing —
    # never inferred from the reply text
    speaker = FakeSpeaker()
    agent = make_agent(FakeLLM(LLMResult(text="I'm great!")), speaker)
    await agent.handle("how are you?", language="en")
    assert speaker.languages == ["en"]
    agent.provider = FakeLLM(LLMResult(text="Benissimo!"))
    await agent.handle("come stai?", language="it")
    assert speaker.languages == ["en", "it"]


async def test_responds_in_italian():
    speaker = FakeSpeaker()
    agent = make_agent(FakeLLM(LLMResult(text="Oggi mi sento benissimo!")), speaker)
    await agent.handle("come stai?")
    assert speaker.spoken == ["Oggi mi sento benissimo!"]


async def test_responds_in_english():
    speaker = FakeSpeaker()
    agent = make_agent(FakeLLM(LLMResult(text="I feel great today!")), speaker)
    await agent.handle("how are you?")
    assert speaker.spoken == ["I feel great today!"]


async def test_history_is_trimmed():
    speaker = FakeSpeaker()
    # each turn returns text, no tools → one user + one assistant per turn
    llm = FakeLLM(*[LLMResult(text=f"r{i}") for i in range(10)])
    agent = make_agent(llm, speaker, max_history_turns=3)
    for i in range(10):
        await agent.handle(f"msg {i}")
    user_turns = [m for m in agent.history if isinstance(m, UserTurn)]
    assert len(user_turns) == 3  # only the last 3 exchanges kept
    assert user_turns[0].text == "msg 7"


async def test_llm_error_recovers_and_rearms():
    speaker = FakeSpeaker()
    agent = make_agent(FakeLLM(raises=TimeoutError("openai timeout")), speaker)
    out = await agent.handle("ciao")
    assert out == ""  # empty turn, no crash
    assert speaker.spoken == []
    # the runtime is still usable: a healthy provider works next
    agent.provider = FakeLLM(LLMResult(text="Eccomi!"))
    assert await agent.handle("ci sei?") == "Eccomi!"
    assert speaker.spoken == ["Eccomi!"]


async def test_tts_error_recovers():
    speaker = FakeSpeaker(raises=RuntimeError("elevenlabs down"))
    agent = make_agent(FakeLLM(LLMResult(text="Ciao")), speaker)
    out = await agent.handle("ciao")  # must not raise
    assert out == "Ciao"  # the reply text is still produced


async def test_max_tool_rounds_caps_loops():
    ctrl = RecordingController()
    # the model keeps calling a tool forever; the cap must stop it
    always_tool = [
        LLMResult(tool_calls=[tool_call("get_direction", cid=f"c{i}")]) for i in range(10)
    ]
    llm = FakeLLM(*always_tool)
    agent = make_agent(llm, FakeSpeaker(), controller=ctrl, max_tool_rounds=2)
    await agent.handle("dove sono?")
    # request rounds are capped: max_tool_rounds+1 provider calls at most
    assert llm.calls == 3


async def test_timings_recorded():
    agent = make_agent(FakeLLM(LLMResult(text="ok")), FakeSpeaker())
    await agent.handle("ciao")
    t = agent.last_timings.as_dict()
    assert t["to_request_ms"] is not None
    assert t["to_final_text_ms"] is not None
    assert t["to_audio_queued_ms"] is not None


async def test_first_output_recorded_when_express_emits_no_visible_text():
    """A turn answered through `express` streams only function-call argument
    deltas — no visible text — so to_first_token_ms is legitimately None.
    to_first_output_ms must still be populated, or the very turns we care
    about most have no time-to-first-anything metric (hardware round, July
    2026)."""
    llm = FakeLLM(LLMResult(tool_calls=[tool_call("express", spoken_text="Ciao!")]))
    agent = make_agent(llm, FakeSpeaker())
    await agent.handle("ciao")
    t = agent.last_timings.as_dict()
    assert t["to_first_token_ms"] is None  # no visible text ever streamed
    assert t["to_first_output_ms"] is not None
    assert t["to_first_output_ms"] <= t["to_final_text_ms"]


async def test_tts_checkpoints_break_down_audio_queued():
    """LLM-final vs TTS-synth vs OJN-submit must be separable, not one opaque
    'audio queued' span (hardware round, July 2026: 5.1-9.2s transcript ->
    audio queued with no way to tell where the time went)."""
    agent = make_agent(FakeLLM(LLMResult(text="ok")), FakeSpeaker())
    await agent.handle("ciao")
    t = agent.last_timings.as_dict()
    for name in ("tts_start", "tts_first_chunk_ready", "tts_complete", "all_submitted"):
        assert t[f"to_{name}_ms"] is not None
    assert t["to_tts_start_ms"] <= t["to_all_submitted_ms"]


async def test_agent_expression_never_hits_posleft_posright(controller, mock_ojn):
    """End-to-end through the REAL BodyController + mock OJN: autonomous agent
    body language reaches OJN as chor only — zero posleft/posright ear calls."""
    llm = FakeLLM(
        LLMResult(tool_calls=[tool_call("gesture_ears", left=5, right=11)]),
        LLMResult(text="Ecco le orecchie!"),
    )
    agent = AgentLoop(
        provider=llm,
        tools=BodyTools(controller),
        system_prompt=SYSTEM,
        speaker=FakeSpeaker(),
    )
    await agent.handle("muovi le orecchie")
    await asyncio.wait_for(controller.wait_idle(), 2)
    assert len(mock_ojn.calls_of("chor")) == 1
    assert mock_ojn.calls_of("ears") == []
