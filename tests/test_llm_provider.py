"""OpenAI provider translation helpers — no real API calls."""

import json
from types import SimpleNamespace

import pytest
from rabbit_brain.llm import make_llm_provider
from rabbit_brain.llm.base import AssistantTurn, ToolCall, ToolResult, ToolSpec, ToolTurn, UserTurn
from rabbit_brain.llm.openai_provider import (
    OpenAIProvider,
    _from_final_response,
    _to_input_items,
    _to_tool_param,
)


def test_history_translates_to_responses_items():
    history = [
        UserTurn("ciao"),
        AssistantTurn(text="penso", tool_calls=(ToolCall("c1", "gesture_ears", {"left": 2}),)),
        ToolTurn((ToolResult("c1", "ears -> (2, 2)"),)),
        UserTurn("grazie"),
    ]
    items = _to_input_items(history)
    assert items[0] == {"role": "user", "content": "ciao"}
    assert items[1] == {"role": "assistant", "content": "penso"}
    assert items[2]["type"] == "function_call"
    assert items[2]["call_id"] == "c1"
    assert json.loads(items[2]["arguments"]) == {"left": 2}
    assert items[3] == {"type": "function_call_output", "call_id": "c1", "output": "ears -> (2, 2)"}
    assert items[4] == {"role": "user", "content": "grazie"}


def test_tool_param_shape():
    spec = ToolSpec("t", "desc", {"type": "object", "properties": {}})
    assert _to_tool_param(spec) == {
        "type": "function",
        "name": "t",
        "description": "desc",
        "parameters": {"type": "object", "properties": {}},
    }


def test_from_final_response_extracts_text_and_calls():
    final = SimpleNamespace(
        output_text="  Ciao!  ",
        output=[
            SimpleNamespace(type="message"),
            SimpleNamespace(
                type="function_call",
                call_id="c9",
                name="gesture_ears",
                arguments='{"left": 3, "right": 3}',
            ),
            SimpleNamespace(type="function_call", call_id="c10", name="body_state", arguments=""),
        ],
    )
    result = _from_final_response(final)
    assert result.text == "Ciao!"
    assert [c.name for c in result.tool_calls] == ["gesture_ears", "body_state"]
    assert result.tool_calls[0].arguments == {"left": 3, "right": 3}
    assert result.tool_calls[1].arguments == {}  # empty args tolerated


def test_from_final_response_tolerates_bad_json():
    bad = SimpleNamespace(type="function_call", call_id="c", name="t", arguments="{not json")
    final = SimpleNamespace(output_text="", output=[bad])
    result = _from_final_response(final)
    assert result.tool_calls[0].arguments == {}  # never raises


def test_make_llm_provider_openai_and_unknown():
    provider = make_llm_provider({"llm": {"provider": "openai", "model": "gpt-5.4-mini"}})
    assert isinstance(provider, OpenAIProvider)
    assert make_llm_provider({}).__class__ is OpenAIProvider  # defaults to openai
    with pytest.raises(ValueError):
        make_llm_provider({"llm": {"provider": "telepathy"}})
