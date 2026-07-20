#!/usr/bin/env python3
"""LLM latency/correctness A/B benchmark (§6.2.5).

Hardware round, July 2026: wake -> audio queued was ~14s, with OpenAI
first-token (~3.3-5s) as one of the two big costs (Deepgram TTS synthesis is
the other — measured separately, DeepgramTTS now logs its own timing
breakdown). This script isolates the LLM side — no TTS, no real rabbit, a
mock OJN server so gesture tools still execute (correctness matters as much
as speed) — and compares model/reasoning_effort combos on the SAME fixed
prompts. It only reports numbers and the raw replies/tool calls for a human
to judge; it never auto-picks a "winner" — check language, correctness, and
whether the model actually skipped the follow-up round for pure gestures.

Needs OPENAI_API_KEY (never logged). Usage:

    OPENAI_API_KEY=... python brain/scripts/llm-bench.py \\
        --models gpt-5.4-mini,gpt-5.4-nano --efforts none,low --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
from dataclasses import dataclass
from pathlib import Path

from rabbit_brain.body.controller import BodyController
from rabbit_brain.body.mock_ojn import MOCK_SERIAL, MOCK_VAPI_TOKEN, MockOjnServer
from rabbit_brain.body.ojn_adapter import OjnAdapter
from rabbit_brain.llm import AgentLoop, BodyTools
from rabbit_brain.llm.openai_provider import OpenAIProvider

SYSTEM_PROMPT_PATH = Path("prompts/system.md")

# A plain reply, one that should trigger an expressive gesture (exercises the
# round-skip optimization — watch `calls` below: 1 means it worked), and one
# that needs an informational tool (get_direction/body_state; `calls` should
# stay 2 there — that follow-up round is still required).
DEFAULT_PROMPTS = [
    "Ciao, come va?",
    "Saluta muovendo le orecchie.",
    "Da che direzione ti sto parlando?",
]


@dataclass
class RunResult:
    model: str
    effort: str
    prompt: str
    text: str
    calls: int
    tool_rounds: int
    to_first_token_ms: int | None
    to_final_text_ms: int | None


async def _one_run(model: str, effort: str, prompt: str, system_prompt: str) -> RunResult:
    mock = MockOjnServer()
    await mock.start()
    try:
        async with OjnAdapter(mock.base_url, MOCK_SERIAL, MOCK_VAPI_TOKEN) as adapter:
            controller = BodyController(adapter)
            controller_task = asyncio.create_task(controller.run())
            calls = 0
            provider = OpenAIProvider(model=model, reasoning_effort=effort)
            real_respond = provider.respond

            async def counting_respond(*a, **kw):
                nonlocal calls
                calls += 1
                return await real_respond(*a, **kw)

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
                    text=text,
                    calls=calls,
                    tool_rounds=t.get("tool_rounds", 0),
                    to_first_token_ms=t.get("to_first_token_ms"),
                    to_final_text_ms=t.get("to_final_text_ms"),
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--models", default="gpt-5.4-mini,gpt-5.4-nano")
    parser.add_argument("--efforts", default="none,low")
    parser.add_argument("--runs", type=int, default=3, help="repeats per (model, effort, prompt)")
    parser.add_argument("--prompts", default=None, help="one prompt per line, in a text file")
    parser.add_argument("--system-prompt", default=str(SYSTEM_PROMPT_PATH))
    return parser.parse_args()


async def main(
    models: list[str], efforts: list[str], prompts: list[str], system_prompt: str, runs: int
) -> None:
    results: list[RunResult] = []
    for model in models:
        for effort in efforts:
            for prompt in prompts:
                for _ in range(runs):
                    r = await _one_run(model, effort, prompt, system_prompt)
                    results.append(r)
                    print(
                        f"[{model}/{effort}] {prompt!r} -> {r.text!r} "
                        f"(calls={r.calls}, tool_rounds={r.tool_rounds}, "
                        f"first_token={r.to_first_token_ms}ms, final={r.to_final_text_ms}ms)"
                    )

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
    print(
        "\nJudge quality/language/tool-correctness yourself from the raw replies above — "
        "this script never picks a winner."
    )


if __name__ == "__main__":
    cli_args = _parse_args()
    cli_prompts = DEFAULT_PROMPTS
    if cli_args.prompts:
        cli_prompts = [
            line.strip() for line in Path(cli_args.prompts).read_text().splitlines() if line.strip()
        ]
    cli_system_prompt = Path(cli_args.system_prompt).read_text(encoding="utf-8")
    asyncio.run(
        main(
            models=[m.strip() for m in cli_args.models.split(",") if m.strip()],
            efforts=[e.strip() for e in cli_args.efforts.split(",") if e.strip()],
            prompts=cli_prompts,
            system_prompt=cli_system_prompt,
            runs=cli_args.runs,
        )
    )
