#!/usr/bin/env python3
"""LLM latency/correctness A/B benchmark (§6.2.5).

Hardware round, July 2026: wake -> audio queued was ~14s, with OpenAI
first-token (~3.3-5s) as one of the two big costs (Deepgram TTS synthesis is
the other — measured separately, DeepgramTTS now logs its own timing
breakdown). This script isolates the LLM side — no TTS, no real rabbit, a
mock OJN server so gesture tools still execute (correctness matters as much
as speed) — and compares model/reasoning_effort combos on the SAME fixed
prompts. It reports numbers, per-round tool calls/text, and flags a handful
of hard correctness invariants (expected call count, spoken text never
empty) — it never auto-picks a "winner" on QUALITY: judge language and
naturalness of the replies yourself from the printed transcripts.

Needs OPENAI_API_KEY (never logged). Usage:

    OPENAI_API_KEY=... python brain/scripts/llm-bench.py \\
        --models gpt-5.4-mini,gpt-5.4-nano --efforts none,low --runs 3

Real result, July 2026 (this benchmark's first run): gpt-5.4-nano ruled out
for this agent/tool loop (median final-text 3.5-5.5s vs mini's 2.2-3.1s,
plus extra tool rounds); the `express` tool (single-call speak+gesture) was
added in response to `single-round-with-tools=0/3` at the time — the model
was not reliably combining free text with a separate gesture tool call.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
from dataclasses import dataclass, field
from pathlib import Path

from rabbit_brain.body.controller import BodyController
from rabbit_brain.body.mock_ojn import MOCK_SERIAL, MOCK_VAPI_TOKEN, MockOjnServer
from rabbit_brain.body.ojn_adapter import OjnAdapter
from rabbit_brain.llm import AgentLoop, BodyTools
from rabbit_brain.llm.openai_provider import OpenAIProvider

SYSTEM_PROMPT_PATH = Path("prompts/system.md")

# A plain reply (no tools expected), one that should trigger an expressive
# gesture via express() (exercises the round-skip optimization), and one
# that needs an informational tool (get_direction). expected_calls is the
# correctness invariant checked below — None for a custom --prompts file
# (no invariant configured, the check is skipped for those).
PROMPT_SPECS: list[tuple[str, int | None]] = [
    ("Ciao, come va?", 1),
    ("Saluta muovendo le orecchie.", 1),
    ("Da che direzione ti sto parlando?", 2),
]


@dataclass
class RoundLog:
    round: int
    tool_names: list[str]
    text_len: int


@dataclass
class RunResult:
    model: str
    effort: str
    prompt: str
    expected_calls: int | None
    text: str
    calls: int
    tool_rounds: int
    to_first_token_ms: int | None
    to_final_text_ms: int | None
    rounds: list[RoundLog] = field(default_factory=list)

    def violations(self) -> list[str]:
        problems = []
        if not self.text.strip():
            problems.append("spoken text is empty")
        if self.expected_calls is not None and self.calls != self.expected_calls:
            problems.append(f"expected {self.expected_calls} call(s), got {self.calls}")
        return problems


async def _one_run(
    model: str, effort: str, prompt: str, expected_calls: int | None, system_prompt: str
) -> RunResult:
    mock = MockOjnServer()
    await mock.start()
    try:
        async with OjnAdapter(mock.base_url, MOCK_SERIAL, MOCK_VAPI_TOKEN) as adapter:
            controller = BodyController(adapter)
            controller_task = asyncio.create_task(controller.run())
            calls = 0
            rounds: list[RoundLog] = []
            provider = OpenAIProvider(model=model, reasoning_effort=effort)
            real_respond = provider.respond

            async def counting_respond(system, history, tools, on_text_delta=None):
                nonlocal calls
                calls += 1
                res = await real_respond(system, history, tools, on_text_delta)
                rounds.append(
                    RoundLog(
                        round=calls,
                        tool_names=[c.name for c in res.tool_calls],
                        text_len=len(res.text.strip()),
                    )
                )
                return res

            provider.respond = counting_respond  # type: ignore[method-assign]
            try:
                agent = AgentLoop(
                    provider=provider,
                    tools=BodyTools(controller),
                    system_prompt=system_prompt,
                    speaker=None,  # isolate LLM latency; TTS is measured separately
                )
                text = await agent.handle(prompt)
                t = agent.last_timings.as_dict() if agent.last_timings else {}
                return RunResult(
                    model=model,
                    effort=effort,
                    prompt=prompt,
                    expected_calls=expected_calls,
                    text=text,
                    calls=calls,
                    tool_rounds=t.get("tool_rounds", 0),
                    to_first_token_ms=t.get("to_first_token_ms"),
                    to_final_text_ms=t.get("to_final_text_ms"),
                    rounds=rounds,
                )
            finally:
                await provider.aclose()
                controller_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await controller_task
    finally:
        await mock.stop()


def _fmt_ms(values: list[int]) -> str:
    if not values:
        return "n/a"
    return f"{statistics.median(values):.0f}ms (min {min(values)}, max {max(values)})"


def _fmt_rounds(rounds: list[RoundLog]) -> str:
    return " | ".join(
        f"r{r.round}: tools={r.tool_names or '-'} text_len={r.text_len}" for r in rounds
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", default="gpt-5.4-mini,gpt-5.4-nano")
    parser.add_argument("--efforts", default="none,low")
    parser.add_argument("--runs", type=int, default=3, help="repeats per (model, effort, prompt)")
    parser.add_argument(
        "--prompts", default=None, help="one prompt per line, in a text file (no invariant checks)"
    )
    parser.add_argument("--system-prompt", default=str(SYSTEM_PROMPT_PATH))
    return parser.parse_args()


async def main(
    models: list[str],
    efforts: list[str],
    prompt_specs: list[tuple[str, int | None]],
    system_prompt: str,
    runs: int,
) -> None:
    results: list[RunResult] = []
    for model in models:
        for effort in efforts:
            for prompt, expected_calls in prompt_specs:
                for _ in range(runs):
                    r = await _one_run(model, effort, prompt, expected_calls, system_prompt)
                    results.append(r)
                    flags = f" !! {', '.join(r.violations())}" if r.violations() else ""
                    print(
                        f"[{model}/{effort}] {prompt!r} -> {r.text!r} "
                        f"(calls={r.calls}, tool_rounds={r.tool_rounds}, "
                        f"first_token={r.to_first_token_ms}ms, final={r.to_final_text_ms}ms)"
                        f"{flags}"
                    )
                    print(f"    {_fmt_rounds(r.rounds)}")

    print("\n--- summary (median first-token / final-text latency per model+effort) ---")
    for model in models:
        for effort in efforts:
            subset = [r for r in results if r.model == model and r.effort == effort]
            ft = [r.to_first_token_ms for r in subset if r.to_first_token_ms is not None]
            fin = [r.to_final_text_ms for r in subset if r.to_final_text_ms is not None]
            skipped = sum(1 for r in subset if r.calls == 1 and r.tool_rounds >= 1)
            print(
                f"{model:16s} {effort:8s} first_token={_fmt_ms(ft):32s} "
                f"final_text={_fmt_ms(fin):32s} single-round-with-tools={skipped}/{len(subset)}"
            )

    all_violations = [(r, v) for r in results for v in r.violations()]
    print(f"\n--- correctness invariants: {len(all_violations)} violation(s) ---")
    for r, v in all_violations:
        print(f"  [{r.model}/{r.effort}] {r.prompt!r}: {v}")
    print(
        "\nJudge quality/language/naturalness yourself from the raw replies above — "
        "this script never picks a winner on that."
    )


if __name__ == "__main__":
    cli_args = _parse_args()
    cli_prompt_specs = PROMPT_SPECS
    if cli_args.prompts:
        cli_prompt_specs = [
            (line.strip(), None)
            for line in Path(cli_args.prompts).read_text().splitlines()
            if line.strip()
        ]
    cli_system_prompt = Path(cli_args.system_prompt).read_text(encoding="utf-8")
    asyncio.run(
        main(
            models=[m.strip() for m in cli_args.models.split(",") if m.strip()],
            efforts=[e.strip() for e in cli_args.efforts.split(",") if e.strip()],
            prompt_specs=cli_prompt_specs,
            system_prompt=cli_system_prompt,
            runs=cli_args.runs,
        )
    )
