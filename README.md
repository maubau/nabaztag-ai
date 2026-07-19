# nabaztag-ai 🐰

[![CI](https://github.com/maubau/nabaztag-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/maubau/nabaztag-ai/actions/workflows/ci.yml)

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

On the Bolt, the audio-in pipeline (reSpeaker capture, openWakeWord, silero-vad, DoA) needs the extras and the XVF3800 udev rule:

```bash
sudo apt install libportaudio2       # sounddevice's runtime library
brain/scripts/install-audio.sh       # add --stt-local for stt_profile: local
sudo install -m 644 brain/udev/70-respeaker-flex.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
python -m rabbit_brain.audio.demo --config config.yaml --mock-ojn   # mic smoke test
```

`config.yaml` is gitignored, so `git pull` never updates it — a config created from an older example can silently keep stale settings (e.g. a missing `deepgram.endpointing`). Check it against the current shape after pulling:

```bash
brain/scripts/config-doctor.py config.yaml         # report drift
brain/scripts/config-doctor.py config.yaml --fix   # rewrite present-but-stale keys in place
```

(The script exists because openWakeWord's Linux dependency on tflite-runtime has no Python 3.12 wheel; we only use its ONNX backend, so it is installed `--no-deps` on top of the `brain[audio]` extra. CI runs the same script on Ubuntu 24.04/3.12.)

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

Apache-2.0 (root, covers `brain/`, `mcp/`, `demos/`, docs, configs) — with one exception:
[ojn/plugin_events/](ojn/plugin_events/) is an OpenJabNab plugin (webhook egress for
button/RFID events) and, as a derivative work of OpenJabNab, is licensed under **OpenJabNab's
GPL v2** (its LICENSE is OJN's COPYING, copied verbatim). It is cleanly separated: no code is
shared with `brain/` or `mcp/` — the brain talks to OJN only over HTTP. The choreography
plugin foreseen in the architecture turned out to be unnecessary (Gate G0).

*Nabaztag is a trademark of its respective owner; this is an independent community project, not affiliated with Violet/Aldebaran.*
