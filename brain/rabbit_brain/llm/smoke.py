"""OpenAI reachability smoke test (no rabbit, no mic):

    python -m rabbit_brain.llm.smoke --config config.yaml ["your prompt"]

Builds the configured LLM provider and runs one turn with no tools, printing
the reply. Needs OPENAI_API_KEY in the environment. Never prints the key.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import yaml

from . import make_llm_provider
from .base import UserTurn

DEFAULT_PROMPT = "Nabaztag, come stai oggi?"
FALLBACK_SYSTEM = "You are a friendly rabbit. Reply in the user's language, briefly."


def _read(path: str, fallback: str = "") -> str:
    p = Path(path)
    return p.read_text() if p.exists() else fallback


async def _run(config: dict, system: str, prompt: str) -> None:
    provider = make_llm_provider(config)
    try:
        result = await provider.respond(system, [UserTurn(prompt)], [])
        print(result.text or "(empty response)")
    finally:
        if hasattr(provider, "aclose"):
            await provider.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI smoke test")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--system", default="prompts/system.md")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    args = parser.parse_args()
    config = yaml.safe_load(_read(args.config, "{}")) or {}
    system = _read(args.system, FALLBACK_SYSTEM)
    asyncio.run(_run(config, system, args.prompt))


if __name__ == "__main__":
    main()
