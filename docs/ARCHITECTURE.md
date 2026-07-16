# Nabaztag AI Revival — Architecture v2.2 (Non-Invasive Track)

## Claude Code Handoff Spec

**Project codename:** `rabbit-brain` **Owner:** Maurizio Caporali **Version:** 2.2 — July 2026 (supersedes v2.1) **Status:** approved for use as `docs/ARCHITECTURE.md` and as the operational brief for Claude Code. **Confirmed configuration:** Stock Nabaztag:tag (V2, electronics untouched) \+ self-hosted OpenJabNab \+ UDOO Bolt (brain) \+ reSpeaker Flex XVF3800 Circular-4 (ears/audio in)

**Changelog**

- **v2.1 → v2.2:** new **§4.1 Network foundation (Setup S0)** — the legacy Wi-Fi segment is promoted from a risk-table line to a first-class, blocking setup task with a concrete `hostapd` recipe, because the rabbit provably cannot join a modern WPA2-only AP (encountered on a FRITZ\!Box 4060). Phases renumbered to start at **S0**. Residual task L1 removed; hardware-table note on the plugin corrected; sentence-level MP3 queueing reconciled with the Non-Goals; repo layout aligned with the hygiene rules (`*.example.yaml` committed, real configs and RFID UIDs gitignored); `PlaybackHandle` \+ `BodyCapabilities` defined so the controller can never promise a preemption the body cannot honor.  
- **v2.0 → v2.1:** Phase 0 feasibility gate; choreography plugin made conditional on `packet/sendMessage` verification; `BodyController` arbitration layer; direct OJN server config primary (DNS override demoted to fallback); Deepgram pinned to configurable `nova-3`; plugin directory inherits OJN's GPL; kickoff prompt verifies OJN endpoints instead of assuming them.  
- **v1.0 → v2.0:** switched from the TagTagTag retrofit track to the **non-invasive** track (stock electronics \+ OpenJabNab \+ external mic).

---

## 1\. Problem Statement & Concept

Revive a stock 2006 Nabaztag:tag as the body of a modern AI assistant **without opening it or replacing its electronics**. The rabbit remains 100% original — output only (speech, LEDs, ears, plus RFID/button as input events) via the OpenJabNab community server. Modern audio input comes from an external reSpeaker 4-mic array. The brain (STT → Claude with tool use → TTS) runs on a UDOO Bolt, a board co-created by the owner — closing a personal 20-year loop between the first consumer connected device and modern edge AI.

Deliverables: working system, public repo, YouTube video, and a comparison protocol vs Reachy Mini (same brain, different body).

## 2\. Goals

1. Full voice loop: wake word → STT → Claude → TTS played through the rabbit's own speaker; p50 latency wake→first audio ≤ 4.0 s (looser than v1.0: OJN adds a hop).  
2. Embodied tool use: the LLM decides ear positions and LED moods itself via tool calls.  
3. Direction-of-arrival trick: rabbit turns its ears toward the speaker using reSpeaker DoA.  
4. RFID as physical input: tag → event → agent action (e.g., "read me Physical AI Atlas news").  
5. Dual STT profile switchable by config: `cloud` (Deepgram primary — credits available; OpenAI Whisper API fallback) and `local` (faster-whisper on the Bolt CPU).  
6. MCP server exposing the rabbit to Claude Desktop / Claude Code.  
7. `BodyAdapter` abstraction so the same brain later drives Reachy Mini (and, further out, an on-device profile on Arduino VENTUNO Q).

## 3\. Non-Goals (v1)

- No hardware modification of the Nabaztag (that is the whole point of this track).  
- No true streaming audio through the rabbit speaker. V1 may use sentence-level complete MP3 files queued sequentially (see §6.2.6); sub-sentence or continuous audio streaming is P2.  
- No local LLM (Bolt runs local STT; LLM stays on Claude API — the local-LLM chapter belongs to the VENTUNO Q profile later).  
- No barge-in: half-duplex only — the system does not listen while the rabbit speaks (mitigates missing AEC reference, see Risk R2).  
- No multi-rabbit support.

## 4\. Hardware & Base Software

| Item | Choice | Notes |
| :---- | :---- | :---- |
| Body | Nabaztag:tag (V2) stock | WPA-capable (unlike V1). Speaker, ear motors, LEDs, RFID, button all used via OJN |
| Server | OpenJabNab, **self-hosted on the Bolt** | github.com/OpenJabNab/OpenJabNab (PHP \+ C++/Qt daemon). Self-host rather than use a public instance: full API control, LAN latency, source-level inspection, raw-frame experiments, and optional custom plugins if Gate G0 requires them |
| Brain host | UDOO Bolt, Linux (Ubuntu 22.04+) | Runs OJN, rabbit-brain, MCP server, local Whisper — one box |
| Audio in | reSpeaker Flex XVF3800 Circular-4, USB mode | On-chip beamforming/NS/AGC/de-reverb \+ DoA. Mic disc placed at the rabbit's base, core board hidden |
| Network | **Dedicated legacy AP \+ direct OJN server config** | See §4.1 — this is a blocking prerequisite, not a detail |

### 4.1 Network foundation (Setup S0 — **blocking, do this first**)

The Nabaztag:tag speaks **802.11b/g on 2.4 GHz with WPA-TKIP only**. It cannot associate to a WPA2-only or WPA2/WPA3-mixed AP. This was confirmed in practice: a FRITZ\!Box 4060 (Wi-Fi 6\) offers WPA2 and WPA2+WPA3 mixed mode, but no pure WPA/TKIP — the rabbit sits at all-orange, failing authentication. **No amount of downstream work matters until the rabbit is on the network**, so this is task S0 and it gates everything.

**Do not downgrade the main network.** The right move is a dedicated legacy segment for the rabbit, isolated from the home LAN, with the Bolt bridging between them. Architecturally this is also cleaner: the rabbit ends up on a segment that talks only to its own brain.

**Recommended: the Bolt itself is the legacy AP** (via a USB Wi-Fi dongle in AP mode, or the Bolt's own radio if the main link is wired). One box, no extra hardware, fully reproducible from the repo — and it makes the whole setup scriptable for anyone reproducing the project.

`ojn/deploy.sh` provisions it. Sketch of the `hostapd` config (`ojn/network/hostapd.conf`):

interface=wlan1              \# the dongle; keep the main interface for LAN/uplink

driver=nl80211

ssid=nabaztag-legacy

hw\_mode=g                    \# 2.4 GHz, 802.11g (rabbit is b/g only)

channel=11                   \# fixed; avoid auto-selection

ieee80211n=0                 \# disable 11n — do not let the AP negotiate beyond the rabbit

wpa=1                        \# WPA (not WPA2). wpa=2 would lock the rabbit out

wpa\_key\_mgmt=WPA-PSK

wpa\_pairwise=TKIP            \# TKIP only — the rabbit does not do AES/CCMP

wpa\_passphrase=\<passphrase\>  \# from .env / not committed

eapol\_version=1              \# KNOWN ISSUE: some Nabaztags fail EAPOL handshake without this

The `eapol_version=1` line is the one that bites: community reports show Nabaztags timing out on the EAPOL key exchange against default hostapd settings. If association still fails, capture `hostapd -dd` logs — an `EAPOL-Key timeout` there confirms it.

Plus, on the Bolt:

- `dnsmasq` serving DHCP on the legacy interface, with a **static lease for the rabbit's MAC** (a stable rabbit IP simplifies everything downstream).  
- **Isolation:** the legacy segment is its own subnet; firewall rules allow the rabbit ⇄ Bolt (OJN \+ the MP3 HTTP server) and nothing else. No route from the legacy segment to the home LAN or the internet. The rabbit's Wi-Fi is 2006-grade crypto — treat it as untrusted and contain it.  
- The same `dnsmasq` instance is where the **DNS-override fallback** lives if the "Violet Platform address" field turns out to be ignored by the firmware.

**Fallback if the Bolt can't host an AP:** a spare old router flashed/configured as a WPA-TKIP-only AP on an isolated VLAN or the FRITZ\!Box guest network, with the Bolt reachable from it via controlled routing. Functionally equivalent, less reproducible for others.

✅ **Gate S0:** rabbit associates, gets its static lease, and demonstrably talks to the Bolt; the rabbit cannot reach the home LAN or the internet; the main Wi-Fi remains WPA2/WPA3.

> **Field note (S0 run, July 2026):** the stock V2 firmware does **not** answer ICMP ping or arping, so "pingable" is the wrong liveness test. Evidence of life is the DHCP lease plus the rabbit's own HTTP traffic — on boot it GETs `/vl/bc.jsp?v=<fw>&m=<mac>...` (Violet bootcode request, port 80) from its platform address. `ojn/deploy.sh verify` checks the neighbor table and points at the tcpdump one-liner instead.

## 5\. Architecture

                     ┌──────────────────────────── UDOO Bolt ────────────────────────────┐

┌───────────────┐WiFi│ ┌────────────┐  REST (localhost)  ┌────────────────────────────┐ │

│ Nabaztag:tag  │◄───┼─┤ OpenJabNab │◄───────────────────┤  rabbit-brain              │ │

│ (stock, WPA)  │Viol│ │ self-hosted│ native \+ raw frames │  wake→VAD→STT→Claude(tools)│ │

│ spk/LED/ears/ │proto│ │(+choreo    │ (packet/sendMessage)│         │        │  ↓TTS    │ │

│ RFID/button   │───►│ │ plugin IF   │◄───────────────────┤  BodyController (arbiter)  │ │

└───────────────┘evt │ │ Phase0 says)│  ears/leds/audio    │   ▲   priority queue       │ │

                     │ └────────────┘  events (hook/poll)  │   │ submit()  ┌──────────┐ │ │

                     │ ┌────────────┐ USB audio \+ DoA       │   ├───────────┤ agent    │ │ │

                     │ │ reSpeaker  ├───────────────────────┘   ├─ DoA      │ RFID     │ │ │

                     │ │ Flex XVF38 │                           └─ idle     └──────────┘ │ │

                     │ └────────────┘                    ┌──────────────────┐            │ │

                     │                                   │ nabaztag-mcp     │──► Claude  │ │

                     │                                   │ (stdio) →Controller  Desktop/ │ │

                     └───────────────────────────────────┴──────────────────┴──Code─────┘

Note: the `+choreo plugin` block is built **only if the Phase 0 feasibility gate** shows native OJN \+ raw `packet/sendMessage` frames are insufficient. All body output funnels through the `BodyController`.

Audio out path: brain generates TTS → writes MP3 → serves it over local HTTP → calls OJN API to make the rabbit stream that URL. Audio in path: reSpeaker USB → ALSA → brain (never through the rabbit).

## 6\. Components

### 6.1 OpenJabNab deployment \+ choreography capability

- Deploy OJN on the Bolt (Apache/Lighttpd \+ PHP wrapper \+ C++ daemon). Register the rabbit, verify built-ins: `tts/say`, MP3 stream, sleep/wake, ear presets, RFID and button events.

**Feasibility gate before any plugin work (see Phase 0).** OJN already exposes a low-level `packet/sendMessage` endpoint that injects raw Violet-protocol frames to the rabbit. Before deciding a custom plugin is needed, empirically determine — on the real rabbit — how much expressivity is reachable through native OJN \+ hand-crafted `packet/sendMessage` frames:

- Can arbitrary ear positions (0–16 per ear) be driven by sending the right choreography/ear frame directly?  
- Can individual LEDs be set to arbitrary RGB via a raw frame?  
- Document the exact frame formats that work in `docs/OJN_API_NOTES.md`.

Only if native \+ raw frames prove insufficient or too awkward to sequence do we build the plugin:

- **T1 (conditional) — `ojn-plugin-choreo` (C++/Qt OJN plugin):** wraps the verified raw frames into clean HTTP endpoints:  
  - `ears?left=0..16&right=0..16` — absolute ear positioning;  
  - `leds?spec=...` — per-LED RGB (bottom, left, right, nose, top) with optional pulse;  
  - `chor?seq=...` — timed ears \+ LEDs sequence for speech-synced body language.  
  - Reference material: OJN wiki protocol pages (v1/v2 frames), existing plugin sources, and the frame formats proven in Phase 0\. **Time-box 3 days;** if blocked, ship v1 with whatever Phase 0 proved reachable (at minimum ear presets \+ nose LED), keep T1 open.  
- **Decision rule:** raw-frame path viable → skip the plugin for v1, expose the frames through `OjnAdapter` directly. Raw-frame path too limited/fragile → build T1. This keeps the plugin an outcome of evidence, not an assumption.  
- Event egress: expose RFID/button events to rabbit-brain. Preferred: small OJN plugin posting webhooks; acceptable v1 fallback: brain polls an OJN endpoint at 1 Hz. (Which one is feasible is also settled in Phase 0.)

### 6.2 `rabbit-brain` (Python, asyncio)

1. **Audio front-end:** ALSA capture from reSpeaker (USB Audio Class). Read DoA angle via the XVF3800 USB control interface (Seeed provides a Python usb tuning/control utility — vendor lib, wrap it in `audio/doa.py`).  
2. **Wake word:** openWakeWord on Bolt CPU.  
3. **VAD:** silero-vad; end-of-speech at 700 ms silence.  
4. **STT — dual profile behind `STTProvider` interface, selected in `config.yaml`:**  
   - `cloud`: **Deepgram streaming** (primary — credits available; model `nova-3`, set in `config.yaml` as `deepgram.model` so it can be swapped without code changes; language auto it/en). **OpenAI Whisper API** as `cloud_fallback` (credits available; non-streaming, acceptable for short utterances).  
   - `local`: faster-whisper (CTranslate2) on Bolt CPU, model `small` or `medium` int8 — benchmark both (T5) and record RTF for the video's local-vs-cloud segment.  
5. **Agent loop:** Claude API (`claude-sonnet-4-6`), streaming, tools below, rolling history, personality system prompt (`prompts/system.md`).  
6. **TTS:** ElevenLabs (primary, Italian voice) or Piper on Bolt (local profile). Output MP3 to `www/audio/`, served by a tiny HTTP server; then OJN call to play the URL on the rabbit. Because playback is whole-file, split long replies into sentence-level MP3s queued sequentially to cut time-to-first-audio.  
7. **Half-duplex gate:** mic pipeline pauses from OJN play command until estimated playback end (+300 ms guard).  
8. **DoA behavior:** on wake word, read DoA angle → map to ear gesture "turning toward the speaker" (both ears biased toward source side) before listening pose. Config in `moods.yaml`.  
9. **Event handlers:** RFID tag ID → named intents (`intents.yaml`), e.g. atlas card → fetch Physical AI Atlas RSS/JSON → summarize via Claude → speak.

### 6.3 Tools (Claude API) — unchanged from v1.0

`set_ears(left, right)`, `set_mood_lights(mood, pulse)`, plus `get_direction()` (returns last DoA). Same design principle: the model improvises body language; nothing scripted.

### 6.4 `BodyController` (arbitration layer — sits between callers and the adapter)

Multiple sources want to move the body concurrently: the agent loop's tool calls, the DoA "turn toward speaker" reflex, RFID-triggered reactions, idle/ambient behavior, and the MCP server. Letting them all hit the `BodyAdapter` directly causes contention (OJN is a slow, single rabbit) and physically incoherent motion (ears yanked between two goals). The `BodyController` is the single owner of the body and mediates access.

Responsibilities:

- **Serialization:** one command in flight to OJN at a time; the rabbit and OJN cannot handle concurrent frames.  
- **Priority:** `SAFETY/SYSTEM > USER_SPEECH_SYNC > AGENT_EXPRESSION > DOA_REFLEX > AMBIENT_IDLE`. A higher-priority command preempts/queues lower ones (e.g. speech-synced body language overrides idle Tai-Chi).  
- **Coalescing & debounce:** collapse rapid successive `set_ears` targets to the latest (the model may emit several tool calls in one turn); debounce LED spam.  
- **Queuing with deadlines:** expression commands tied to a spoken sentence carry the sentence's playback window; if they can't execute in time (OJN lag), they're dropped rather than fired late and out of sync.  
- **Interruptibility:** a new user utterance (wake word) cancels pending AMBIENT/AGENT gestures so the rabbit "snaps to attention."  
- **State model:** tracks current ear positions and LED state; suppresses redundant no-op commands to save OJN round-trips.

class BodyController:

    def \_\_init\_\_(self, adapter: BodyAdapter): ...

    async def submit(self, cmd: BodyCommand, priority: Priority, deadline: float | None \= None) \-\> None

    async def run(self) \-\> None          \# single consumer loop draining the priority queue → adapter

    def snapshot(self) \-\> BodyState      \# current ears/leds, for redundancy suppression & get\_direction

All callers (agent loop, DoA reflex, RFID handlers, MCP server, idle behavior) go through `submit`; none touch the `BodyAdapter` directly. This is also what makes the Reachy swap clean — the controller logic is body-agnostic; only the adapter under it changes.

### 6.5 `nabaztag-mcp`

MCP server (Python SDK, stdio) wrapping rabbit-brain's internal API via the `BodyController`: `move_ears`, `set_leds`, `speak(text)`, `play_choreography(name)`, `last_rfid()`. Used for the Claude Desktop demo and as Claude Code's physical test harness during development. MCP commands enter at `AGENT_EXPRESSION` priority so a live conversation still takes precedence.

### 6.6 `BodyAdapter` interface (architectural insurance)

class BodyAdapter(Protocol):

    async def set\_ears(left: int, right: int) \-\> None

    async def set\_leds(spec: LedSpec) \-\> None

    async def play\_audio(url\_or\_path: str) \-\> PlaybackHandle

    async def events() \-\> AsyncIterator\[BodyEvent\]   \# button, rfid, ...

    @property

    def capabilities(self) \-\> BodyCapabilities        \# what this body can actually do (from Gate G0)

class PlaybackHandle(Protocol):

    async def wait\_started(self) \-\> None: ...

    async def wait\_finished(self) \-\> None: ...

    async def cancel(self) \-\> None: ...

    @property

    def estimated\_duration\_s(self) \-\> float | None: ...

`PlaybackHandle` is what makes the half-duplex gate, speech-synced gestures, and preemption implementable rather than guessed. OJN likely offers **no true cancel** and no playback-finished callback: in that case `OjnAdapter` declares `capabilities.can_cancel_audio = False`, derives `estimated_duration_s` from the MP3's own duration, and implements `wait_finished` as a timer (+ guard). **The `BodyController` must consult `capabilities` and never promise a preemption the body cannot physically honor** — where cancel is unavailable it degrades to "let the current utterance finish, drop the queued ones." Implementations: `OjnAdapter` (v1, this spec) → `ReachyMiniAdapter` (P1: SDK client to the robot's FastAPI daemon over LAN, :8000) → `VentunoQLocalProfile` (P2: same brain deployed on Arduino VENTUNO Q, local STT/TTS/LLM — placeholder module \+ README only for now). The `BodyController` (§6.4) sits above whichever adapter is active and is body-agnostic — swapping bodies means swapping only the adapter beneath it.

## 7\. Repository Layout

nabaztag-ai/

├── ojn/                  \# OJN deployment scripts \+ custom plugins

│   ├── deploy.sh         \# install OJN on the Bolt; provisions the legacy AP (§4.1)

│   ├── network/          \# hostapd.conf.example, dnsmasq.conf.example, nftables rules

│   └── plugin\_choreo/    \# T1: created ONLY if Gate G0 requires it (GPL, see §11.1)

├── brain/

│   └── rabbit\_brain/

│       ├── audio/        \# alsa capture, wakeword, vad, doa.py

│       ├── stt/          \# deepgram.py, openai\_whisper.py, local\_whisper.py

│       ├── llm/          \# agent loop, tools

│       ├── tts/          \# elevenlabs.py, piper.py, mp3 server

│       └── body/         \# BodyController, BodyAdapter, ojn\_adapter.py, reachy\_adapter.py (P1)

├── mcp/

├── demos/                \# scripted video scenarios incl. RFID \+ DoA tricks

├── config.example.yaml   \# committed. stt\_profile: cloud|local, deepgram.model: nova-3,

│                         \# tts\_profile, ojn host. Real config.yaml is gitignored

├── intents.example.yaml  \# committed: placeholder RFID tag IDs → intent mapping

├── moods.yaml            \# committed: mood → LED/ear mapping (no secrets, tweakable on camera)

└── .env.example          \# committed. ANTHROPIC\_API\_KEY, DEEPGRAM\_API\_KEY, OPENAI\_API\_KEY,

                          \# ELEVENLABS\_API\_KEY, OJN\_TOKEN, RABBIT\_MAC

**Gitignored (never committed):** `config.yaml`, `intents.yaml` (contains real RFID tag UIDs — personal data, and a physical-access credential of sorts), `.env`, `www/audio/`, model weights.

## 8\. Phases & Acceptance Criteria

**S0 — Network foundation (manual, BLOCKING).** Per §4.1: legacy WPA/TKIP 2.4 GHz AP on the Bolt (`hostapd` \+ `dnsmasq`), isolated subnet, static lease for the rabbit. ✅ **Gate S0:** rabbit associated, static lease held, and its HTTP bootcode requests observed on the Bolt (the firmware does not answer ping — see §4.1 field note); rabbit isolated from home LAN/internet; main Wi-Fi untouched. *Nothing downstream can start until this passes.* **Status: PASSED (July 2026)** — AC 3168 AP on `wlp3s0`, channel 11, WPA1/TKIP; `GET /vl/bc.jsp?...` seen from the rabbit's static IP.

**S1/S2 — OJN bring-up (manual):** OJN deployed on the Bolt; rabbit's "Violet Platform address" pointed at the Bolt's OJN instance (DNS override via the same dnsmasq only as fallback, §4.1); rabbit registered. ✅ `tts/say` from the OJN web UI makes the rabbit speak; button \+ RFID events visible in OJN logs.

**Phase 0 — Feasibility gate (real hardware, no product code yet).** Empirically map what the *actual* rabbit can do through OpenJabNab. This phase de-risks every phase after it and its findings decide whether T1 is even needed. Probe, on the physical rabbit, and record results in `docs/OJN_API_NOTES.md`:

- native OJN built-ins actually confirmed working (TTS, MP3-by-URL, ear presets, sleep/wake, nose LED, RFID/button events);  
- raw `packet/sendMessage`: can arbitrary ear positions be driven? individual LED RGB? what exact frames work?;  
- event egress path that works (webhook plugin vs polling) and its real latency;  
- OJN round-trip latency for a command (feeds the p50 budget and BodyController deadlines). ✅ **Gate G0:** a written capability matrix (works / works-via-raw-frame / needs-plugin / not-possible) for ears, LEDs, audio-out, events. The plugin decision (§6.1) and the latency targets are set from this matrix, not assumed. **If a load-bearing capability is impossible even via plugin, the architecture is revised here — cheaply — before anything depends on it.**

**Phase 1 — Body control \+ BodyController \+ MCP:** `OjnAdapter` over whatever Phase 0 proved (built-ins and/or raw frames); `BodyController` arbitration layer; MCP server on top of the controller. ✅ From Claude Desktop: rabbit speaks an arbitrary sentence and moves its ears; two competing commands submitted at once resolve by priority (no contention, no incoherent motion). Mock-OJN mode (`--mock-ojn`) covers adapter \+ controller in unit tests.

**Phase T1 (conditional, parallel) — Choreography plugin:** only if Phase 0 flagged `needs-plugin`. Arbitrary ears \+ per-LED control as clean HTTP endpoints. ✅ `ears?left=3&right=14` produces the asymmetric pose; timed sequence syncs a 3-step LED/ear choreography. Skipped entirely if raw frames sufficed.

**Phase 2 — Voice pipeline:** reSpeaker capture, wake word, VAD, both STT profiles (`nova-3` cloud / faster-whisper local), TTS→MP3→OJN playback, half-duplex gate. ✅ Full it/en conversation; p50 wake→first-audio within the budget set by Phase 0's measured OJN latency (target ≤ 4.0 s, cloud) over 20 runs; `stt_profile: local` works end-to-end with measured RTF logged.

**Phase 3 — Embodiment \+ DoA \+ RFID:** tools in agent loop (all body output via `BodyController`), DoA ear-turn reflex on wake, RFID intents. ✅ ≥ 8/10 varied prompts show spontaneous plausible body language; rabbit visibly orients ears toward speaker positioned 90° off-axis; Atlas RFID card triggers spoken news summary; DoA reflex and agent expression never fight (controller priority verified).

**Phase 4 — Demo & comparison prep:** 3 repeatable demo scripts; latency dashboard; `ReachyMiniAdapter` skeleton \+ docs. ✅ Two clean consecutive runs of each demo; ARCHITECTURE.md documents the Reachy and VENTUNO Q profiles.

## 9\. Risks

| \# | Risk | Mitigation |
| :---- | :---- | :---- |
| R1 | OJN choreography protocol under-documented | **Phase 0 feasibility gate settles this empirically before dependent work.** Then: raw `packet/sendMessage` frames if sufficient, else time-boxed plugin (3 days) \+ preset fallback; OJN wiki frames \+ community forum threads as sources |
| R2 | No AEC reference for rabbit speaker → echo | Half-duplex gate (§6.2.7); reSpeaker NS helps with residual room noise |
| R3 | OJN event latency (rabbit polls server on Violet-protocol ping interval) | Tune ping interval frame; accept 1–2 s event latency for RFID/button (not in the voice path) |
| R4 | Stock V2 Wi-Fi link quality/stability once associated | Dedicated 2.4 GHz SSID, no band steering, fixed channel, rabbit close to AP; static DHCP lease |
| R5 | MP3-per-sentence gaps sound choppy | Pre-generate next sentence while current plays; tune queue |
| R6 | **V2 cannot join WPA2-only modern Wi-Fi** (802.11b/g \+ WPA-TKIP only; confirmed on a FRITZ\!Box 4060\) | Dedicated legacy AP per **§4.1** (hostapd on the Bolt: `wpa=1`, `wpa_pairwise=TKIP`, `eapol_version=1`), isolated subnet. **Blocking: Gate S0, before everything** |
| R7 | The rabbit's segment uses broken 2006-era crypto (TKIP) — an attack surface on the home network | Segment isolation is part of the mitigation, not an optional extra: separate subnet, no route to home LAN or internet, firewall limited to rabbit ⇄ Bolt (§4.1) |

## 10\. Claude Code Kickoff Prompt

Read docs/ARCHITECTURE.md. First execute §11.4 (open-source repo bootstrap), including `ojn/network/` with the `hostapd`/`dnsmasq`/firewall example configs from §4.1 (passphrases come from `.env`, never committed) and a `deploy.sh` that provisions the legacy AP and OJN on the Bolt. Note that Gate S0 (rabbit on the network) and the hardware half of Phase 0 are Maurizio's to run — your job is to make them scriptable and reproducible.

**Do not assume any OJN endpoint exists.** Before writing the adapter, clone and study the OpenJabNab repo (github.com/OpenJabNab/OpenJabNab): find the *actual* API surface (the real path for TTS, MP3-by-URL, ear/LED control, the `packet/sendMessage` raw-frame endpoint, and how events are exposed). Record exactly what exists, with real paths and payloads, in `docs/OJN_API_NOTES.md`. This is the software half of the Phase 0 feasibility gate; together with the hardware probing it produces the capability matrix (Gate G0) that decides whether the choreography plugin is needed.

Then implement Phase 1 against the *verified* surface: `brain/rabbit_brain/body/ojn_adapter.py` (only endpoints you confirmed), the `BodyAdapter` protocol with `PlaybackHandle` and `BodyCapabilities` (§6.6), and the `BodyController` arbitration layer (§6.4) with its priority queue and single-consumer loop. The controller must consult `capabilities` and never promise a preemption the body cannot honor. Provide a `--mock-ojn` simulator and unit tests covering adapter and controller (priority preemption, coalescing, deadline drops, and the no-cancel degradation path). Finally scaffold `mcp/` exposing speak/move\_ears/set\_leds **through the BodyController**, not the adapter directly. Do not implement audio capture yet.

## 11\. Open Source Release

This project is public on GitHub **from the first commit** (building-in-public: each completed phase is content for LinkedIn/YouTube).

### 11.1 Licensing structure

- Root `LICENSE`: **Apache-2.0** — covers `brain/`, `mcp/`, `demos/`, docs, configs.  
- `ojn/plugin_choreo/LICENSE`: **OJN's GPL, copied verbatim.** The choreography plugin (if Gate G0 requires it) compiles against OpenJabNab's codebase and is a derivative work, so it inherits OJN's license. Copy the **exact SPDX identifier and license text from the OJN repository** — `GPL-2.0-only` and `GPL-2.0-or-later` are different licenses and the choice is not ours to make. This directory stays cleanly separated: no code shared with `brain/`; the brain interacts with it only over HTTP, keeping the Apache-2.0 side unencumbered.  
- Preferred endgame for the plugin: upstream it as a PR to OpenJabNab; keep only deployment scripts here.  
- README must state the dual-license layout explicitly.  
- Trademark note in README: "Nabaztag is a trademark of its respective owner; this is an independent community project, not affiliated with Violet/Aldebaran."

### 11.2 Repo naming & discoverability

- Repo name: `nabaztag-ai`. `rabbit-brain` remains the name of the brain component/package inside the repo regardless.  
- GitHub **topics** (these drive discovery more than the name): `physical-ai`, `robotics`, `embodied-ai`, `nabaztag`, `voice-assistant`, `llm`, `claude`, `mcp`, `iot`, `retro-tech`.  
- Repo description (≤ 120 chars, keyword-dense): "AI brain for the original Nabaztag rabbit — LLM tool-use embodiment, voice pipeline, MCP server. Physical AI, 2006 edition."

### 11.3 Repo hygiene (non-negotiable, from commit \#1)

- `.gitignore`: `.env`, `config.yaml` (ship `config.example.yaml`), `intents.yaml` (ship `intents.example.yaml`; real file holds RFID UIDs), `www/audio/`, model weights, `*.mp3`.  
- **Secrets:** pre-commit hook with `gitleaks`; keys only in `.env` (never committed). CI also runs gitleaks on push.  
- `README.md` top-to-bottom: demo GIF → what/why (the 20-year loop story) → hardware BOM with prices (\~€70 rabbit \+ \~€50 reSpeaker \+ a PC/SBC) → quickstart with `--mock-ojn` (no hardware needed) → architecture diagram → roadmap (Reachy Mini, VENTUNO Q profiles).  
- `CONTRIBUTING.md`: dev setup on mock, how to run tests, PR conventions. `CODE_OF_CONDUCT.md` (Contributor Covenant).  
- GitHub Actions CI: lint (ruff) \+ unit tests against `--mock-ojn` on every PR — contributors never need a physical rabbit.  
- Conventional commits; tagged releases per phase (`v0.1-phase1`, ...), each with a short demo video/GIF in the release notes.

### 11.4 Bootstrap task for Claude Code (do this first)

Initialize the repo with: root Apache-2.0 LICENSE, `.gitignore`, `config.example.yaml`, `intents.example.yaml`, `.env.example`, README skeleton per §11.3, CONTRIBUTING.md, gitleaks pre-commit config, and a GitHub Actions workflow running ruff \+ pytest (mock mode).

Do not create `ojn/plugin_choreo/` during bootstrap. Create it only if Gate G0 determines that the choreography plugin is required; when created, include the exact OpenJabNab-compatible GPL license from the first commit — copy the precise SPDX identifier and license text found in the OJN repository (GPL-2.0-**only** and GPL-2.0-**or-later** are different licenses; do not guess between them).  
