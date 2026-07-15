# nabaztag-ai 🐰

> AI brain for the original Nabaztag rabbit — LLM tool-use embodiment, voice pipeline, MCP server. Physical AI, 2006 edition.

*(demo GIF — coming with Phase 1)*

## What & why

In 2006 the Nabaztag:tag was the first consumer connected device — a Wi-Fi rabbit that read the news and wiggled its ears. Twenty years later, this project revives a **stock, unopened** Nabaztag:tag as the body of a modern AI assistant: wake word → speech-to-text → Claude with tool use → text-to-speech, played through the rabbit's own speaker, with the LLM deciding ear positions and LED moods itself.

The rabbit stays 100% original. It talks to a self-hosted [OpenJabNab](https://github.com/OpenJabNab/OpenJabNab) server; modern audio input comes from an external reSpeaker 4-mic array; the brain runs on a UDOO Bolt — a board co-created by the project's owner, closing a personal 20-year loop between the first connected device and modern edge AI.

Full spec: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Hardware BOM

| Item | Role | ~Price |
| :--- | :--- | :--- |
| Nabaztag:tag (V2), stock | Body: speaker, ears, LEDs, RFID, button | ~€70 (second-hand) |
| reSpeaker Flex XVF3800 Circular-4 | Ears-in: 4-mic array, beamforming, DoA | ~€50 |
| UDOO Bolt (or any Linux PC/SBC) | Brain: OJN server, rabbit-brain, MCP | you probably have one |
| USB Wi-Fi dongle (AP-mode capable) | Dedicated legacy WPA-TKIP AP (§4.1) | ~€10 |

## Quickstart (no hardware needed)

```bash
git clone https://github.com/<you>/nabaztag-ai && cd nabaztag-ai
python -m venv .venv && source .venv/bin/activate
pip install -e "brain[dev]"
pytest                      # runs against the mock-OJN simulator
python -m rabbit_brain.body.demo --mock-ojn   # drive a simulated rabbit
```

With real hardware, start from **Gate S0** (the rabbit's legacy Wi-Fi segment — see [docs/ARCHITECTURE.md §4.1](docs/ARCHITECTURE.md) and [ojn/](ojn/)).

## Architecture

```
Nabaztag:tag ◄─Violet proto─► OpenJabNab (self-hosted) ◄─REST─► rabbit-brain
   stock, WPA-TKIP segment         on the Bolt                wake→VAD→STT→Claude(tools)→TTS
                                                                    │
reSpeaker XVF3800 ──USB audio + DoA──────────────────────► BodyController (arbiter)
                                                                    ▲
                                                     nabaztag-mcp ──┘ → Claude Desktop/Code
```

All body output funnels through the `BodyController` (single owner of the body, priority queue); bodies are swappable behind the `BodyAdapter` protocol.

## Roadmap

- **v1** — Nabaztag:tag via OpenJabNab (this repo, phases S0 → 4)
- **P1** — `ReachyMiniAdapter`: same brain, different body (comparison protocol)
- **P2** — `VentunoQLocalProfile`: fully local STT/TTS/LLM on Arduino VENTUNO Q

## License

Apache-2.0 (root, covers `brain/`, `mcp/`, `demos/`, docs, configs). If Gate G0 ever requires the OpenJabNab choreography plugin, it will live in `ojn/plugin_choreo/` under **OpenJabNab's GPL** (derivative work), cleanly separated — the brain talks to it only over HTTP.

*Nabaztag is a trademark of its respective owner; this is an independent community project, not affiliated with Violet/Aldebaran.*
