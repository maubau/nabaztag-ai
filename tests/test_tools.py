"""Body tools: choreography-only, validated, never posleft/posright."""

import asyncio
import json

from rabbit_brain.body.types import ChorCommand, EarsCommand, LedsCommand
from rabbit_brain.llm import BodyTools, ToolCall


class RecordingController:
    def __init__(self):
        self.submitted = []

    async def submit(self, cmd, priority, deadline=None):
        self.submitted.append((cmd, priority))

    def snapshot(self):
        from rabbit_brain.body.types import BodyState

        return BodyState(ears=(4, 4), leds={"nose": (0, 255, 0)}, playing=False)


def call(name, **args):
    return ToolCall(call_id="c1", name=name, arguments=args)


async def test_gesture_ears_is_choreography_only():
    ctrl = RecordingController()
    tools = BodyTools(ctrl)
    res = await tools.execute(call("gesture_ears", left=3, right=14))
    assert "error" not in res.output
    ((cmd, _prio),) = ctrl.submitted
    # ear movement must be a ChorCommand (motor choreography), NEVER EarsCommand
    assert isinstance(cmd, ChorCommand)
    assert not isinstance(cmd, EarsCommand)
    assert "motor,0,54" in cmd.chor and "motor,1,252" in cmd.chor  # 3*18, 14*18


async def test_gesture_ears_out_of_range_reports_error():
    ctrl = RecordingController()
    tools = BodyTools(ctrl)
    res = await tools.execute(call("gesture_ears", left=3, right=99))
    assert res.output.startswith("error")
    assert ctrl.submitted == []  # nothing sent to the body


async def test_set_mood_lights_valid_and_invalid():
    ctrl = RecordingController()
    tools = BodyTools(ctrl)
    ok = await tools.execute(call("set_mood_lights", mood="happy", pulse=True))
    assert "happy" in ok.output
    ((cmd, _),) = ctrl.submitted
    assert isinstance(cmd, LedsCommand) and cmd.spec.pulse
    bad = await tools.execute(call("set_mood_lights", mood="ultraviolet"))
    assert bad.output.startswith("error")
    assert len(ctrl.submitted) == 1  # invalid mood not sent


async def test_play_gesture_valid_and_invalid():
    ctrl = RecordingController()
    tools = BodyTools(ctrl)
    ok = await tools.execute(call("play_gesture", preset="nod"))
    assert "nod" in ok.output
    ((cmd, _),) = ctrl.submitted
    assert isinstance(cmd, ChorCommand)
    bad = await tools.execute(call("play_gesture", preset="backflip"))
    assert bad.output.startswith("error")


async def test_get_direction_and_body_state():
    ctrl = RecordingController()
    tools = BodyTools(ctrl, get_direction=lambda: 271)
    assert "271" in (await tools.execute(call("get_direction"))).output
    tools_none = BodyTools(ctrl, get_direction=lambda: None)
    assert (await tools_none.execute(call("get_direction"))).output == "unknown"
    state = json.loads((await tools.execute(call("body_state"))).output)
    assert state["ears"] == [4, 4]


async def test_unknown_tool_reports_error():
    tools = BodyTools(RecordingController())
    res = await tools.execute(call("launch_rocket"))
    assert res.output.startswith("error")


async def test_specs_are_valid_json_schema():
    specs = BodyTools(RecordingController()).specs()
    names = {s.name for s in specs}
    assert names == {
        "express",
        "gesture_ears",
        "set_mood_lights",
        "play_gesture",
        "get_direction",
        "body_state",
    }
    informational = {s.name for s in specs if s.informational}
    assert informational == {"get_direction", "body_state"}
    for spec in specs:
        assert spec.parameters["type"] == "object"
        assert isinstance(spec.description, str) and spec.description


async def test_express_speaks_and_gestures_in_one_call():
    ctrl = RecordingController()
    tools = BodyTools(ctrl)
    res = await tools.execute(
        call(
            "express",
            spoken_text="Ciao!",
            ears={"left": 3, "right": 3},
            gesture="wiggle",
            mood="happy",
        )
    )
    assert "error" not in res.output
    cmds = [c for c, _ in ctrl.submitted]
    assert len(cmds) == 3  # ears (chor), gesture (chor), mood (leds)
    assert all(isinstance(c, ChorCommand | LedsCommand) for c in cmds)
    assert not any(isinstance(c, EarsCommand) for c in cmds)


async def test_express_spoken_text_only_no_gesture():
    ctrl = RecordingController()
    tools = BodyTools(ctrl)
    res = await tools.execute(call("express", spoken_text="Solo una risposta."))
    assert res.output == "said"
    assert ctrl.submitted == []


async def test_express_requires_spoken_text():
    ctrl = RecordingController()
    tools = BodyTools(ctrl)
    res = await tools.execute(call("express", gesture="nod"))
    assert res.output.startswith("error")
    assert ctrl.submitted == []


async def test_express_invalid_field_is_atomic_nothing_submitted():
    # valid ears + an invalid gesture must not leave a half-applied body
    # state — everything is validated before anything is submitted
    ctrl = RecordingController()
    tools = BodyTools(ctrl)
    res = await tools.execute(
        call("express", spoken_text="Ciao!", ears={"left": 2, "right": 2}, gesture="backflip")
    )
    assert res.output.startswith("error")
    assert ctrl.submitted == []


async def test_gesture_ears_reaches_ojn_as_chor_not_ears(controller, mock_ojn):
    """Through the REAL BodyController + mock OJN: the agent's ear movement is a
    chor call, with ZERO posleft/posright ear calls (which would jingle)."""
    tools = BodyTools(controller)
    await tools.execute(call("gesture_ears", left=8, right=2))
    await asyncio.wait_for(controller.wait_idle(), 2)
    assert len(mock_ojn.calls_of("chor")) == 1
    assert mock_ojn.calls_of("ears") == []
