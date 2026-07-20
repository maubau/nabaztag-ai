"""llm-bench.py: LLM latency/correctness A/B benchmark script (fake provider, no API calls)."""

import importlib.util
import sys
from pathlib import Path

from rabbit_brain.llm.base import LLMResult

_SCRIPT = Path(__file__).parent.parent / "brain" / "scripts" / "llm-bench.py"


def _load():
    # the script's filename has a hyphen; load it by path and register it in
    # sys.modules so its @dataclass can resolve its own module (see
    # test_config_doctor.py for the same pattern)
    spec = importlib.util.spec_from_file_location("llm_bench", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llm_bench"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeProvider:
    """Stands in for OpenAIProvider — no real API calls in CI."""

    def __init__(self, model, reasoning_effort):
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def respond(self, system, history, tools, on_text_delta=None):
        return LLMResult(text=f"reply from {self.model}/{self.reasoning_effort}")

    async def aclose(self):
        pass


async def test_one_run_reports_result(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "OpenAIProvider", FakeProvider)
    result = await mod._one_run("gpt-x", "none", "ciao", "you are a rabbit")
    assert result.text == "reply from gpt-x/none"
    assert result.model == "gpt-x"
    assert result.effort == "none"
    assert result.calls == 1  # counting wrapper saw exactly one respond() call


async def test_one_run_counts_a_follow_up_round(monkeypatch):
    from rabbit_brain.llm.base import ToolCall

    mod = _load()

    class TwoRoundProvider(FakeProvider):
        def __init__(self, model, reasoning_effort):
            super().__init__(model, reasoning_effort)
            self._n = 0

        async def respond(self, system, history, tools, on_text_delta=None):
            self._n += 1
            if self._n == 1:
                return LLMResult(
                    tool_calls=[ToolCall(call_id="c1", name="get_direction", arguments={})]
                )
            return LLMResult(text="fatto")

    monkeypatch.setattr(mod, "OpenAIProvider", TwoRoundProvider)
    result = await mod._one_run("gpt-x", "low", "da che direzione?", "you are a rabbit")
    assert result.calls == 2
    assert result.text == "fatto"


def test_fmt_ms_empty_and_populated():
    mod = _load()
    assert mod._fmt_ms([]) == "n/a"
    formatted = mod._fmt_ms([100, 300, 200])
    assert "ms" in formatted and "min 100" in formatted and "max 300" in formatted
