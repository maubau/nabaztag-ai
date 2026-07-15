# Contributing

Thanks for your interest! You **do not need a physical Nabaztag** to contribute — everything runs against the mock-OJN simulator.

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e "brain[dev]"
pre-commit install        # gitleaks secret scan on every commit
```

## Running tests

```bash
pytest          # unit tests, all against --mock-ojn (no hardware, no network)
ruff check .    # lint
ruff format --check .
```

CI runs exactly these on every PR.

## PR conventions

- **Conventional commits** (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).
- One logical change per PR; include tests for behavior changes.
- Anything touching the OJN protocol must reference the capability matrix in `docs/OJN_API_NOTES.md` — endpoints are verified, never assumed.
- Never commit secrets, real `config.yaml`/`intents.yaml` (RFID UIDs are personal data), or audio files. The gitleaks hook and CI will catch keys, but don't rely on it.

## Licensing

Root is Apache-2.0. `ojn/plugin_choreo/` (if it ever exists) is GPL, inherited from OpenJabNab — keep the two sides separated; no shared code.
