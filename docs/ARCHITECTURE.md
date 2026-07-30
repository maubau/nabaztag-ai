# Nabaztag AI Revival — Architecture v2.3 (Non-Invasive Track)

## Handoff Spec

**Project codename:** `rabbit-brain` **Owner:** Maurizio Caporali **Version:** 2.3 — July 2026 (supersedes v2.2) **Status:** `docs/ARCHITECTURE.md` — the project description of record. **Confirmed configuration:** Stock Nabaztag:tag (V2, electronics untouched) \+ self-hosted OpenJabNab \+ UDOO Bolt (brain) \+ reSpeaker Flex XVF3800 Circular-4 (ears/audio in/audio out)

> **How to read this document.** v2.2 and earlier were a *plan*. From v2.3 on this describes a system that is **built and running on real hardware**, so the design has been reconciled with what the rabbit actually turned out to do. `docs/OJN_API_NOTES.md` holds the capability reference and the numbered rules the code cites; this document holds the shape of the system.

**Changelog**

- **v2.2 → v2.3:** reconciled with the built system. **Barge-in is no longer a non-goal** (§3): a full-duplex **realtime mode** ships alongside the turn-based path (§6.2.10), which required audio output to become **switchable** between the rabbit's speaker and a local device (§6.2.6) — the 2006 decoder cannot be interrupted. Conversely "no streaming audio through the rabbit" is now a *proven* limit rather than a caution (§3, Gate L3). The LLM is **OpenAI** (§6.2.5 — the interface was always provider-neutral; the earlier text had simply not caught up); STT is **Deepgram Flux** with provider-side end-of-turn (§6.2.4); TTS is **self-hosted Piper** (§6.2.6). The choreography plugin foreseen in §6.1 proved **unnecessary** (Gate G0). Runtime ownership rules, systemd units and recovery documented (§6.7); phase statuses updated; §10 marked historical.
- **v2.1 → v2.2:** new **§4.1 Network foundation (Setup S0)** — the legacy Wi-Fi segment is promoted from a risk-table line to a first-class, blocking setup task with a concrete `hostapd` recipe, because the rabbit provably cannot join a modern WPA2-only AP (encountered on a FRITZ\!Box 4060). Phases renumbered to start at **S0**. Residual task L1 removed; hardware-table note on the plugin corrected; sentence-level MP3 queueing reconciled with the Non-Goals; repo layout aligned with the hygiene rules (`*.example.yaml` committed, real configs and RFID UIDs gitignored); `PlaybackHandle` \+ `BodyCapabilities` defined so the controller can never promise a preemption the body cannot honor.  
- **v2.0 → v2.1:** Phase 0 feasibility gate; choreography plugin made conditional on `packet/sendMessage` verification; `BodyController` arbitration layer; direct OJN server config primary (DNS override demoted to fallback); Deepgram pinned to configurable `nova-3`; plugin directory inherits OJN's GPL; kickoff prompt verifies OJN endpoints instead of assuming them.  
- **v1.0 → v2.0:** switched from the TagTagTag retrofit track to the **non-invasive** track (stock electronics \+ OpenJabNab \+ external mic).

---

## 1\. Problem Statement & Concept

Revive a stock 2006 Nabaztag:tag as the body of a modern AI assistant **without opening it or replacing its electronics**. The rabbit remains 100% original — output only (speech, LEDs, ears, plus RFID/button as input events) via the OpenJabNab community server. Modern audio input comes from an external reSpeaker 4-mic array. The brain (STT → LLM with tool use → TTS) runs on a UDOO Bolt, a board co-created by the owner — closing a personal 20-year loop between the first consumer connected device and modern edge AI.

Deliverables: working system, public repo, YouTube video, and a comparison protocol vs Reachy Mini (same brain, different body).

## 2\. Goals

1. Full voice loop: wake word → STT → LLM → TTS played through the rabbit's own speaker; p50 latency wake→first audio ≤ 4.0 s (looser than v1.0: OJN adds a hop). **Met and closed for v1** — see §6.2.11 for the measured breakdown and the decision to stop optimising.  
2. Embodied tool use: the LLM decides ear positions and LED moods itself via tool calls.  
3. Direction-of-arrival trick: rabbit turns its ears toward the speaker using reSpeaker DoA.  
4. RFID as physical input: tag → event → agent action (e.g., "read me Physical AI Atlas news").  
5. STT profile switchable by config: `flux` (Deepgram Flux, provider-side end-of-turn — **production**), `cloud` (nova-3 \+ a local Silero window, fallback), `local` (faster-whisper on the Bolt CPU). OpenAI Whisper API backs up the cloud profiles.  
6. MCP server exposing the rabbit to Claude Desktop / Claude Code.  
7. `BodyAdapter` abstraction so the same brain later drives Reachy Mini (and, further out, an on-device profile on Arduino VENTUNO Q).

## 3\. Non-Goals (v1)

- No hardware modification of the Nabaztag (that is the whole point of this track).  
- **No true streaming audio through the rabbit speaker — a PROVEN limit, not a precaution.** The MTL decoder buffers each file to EOF: audio starts only once the transfer completes, and the delivery path was cleared of blame (`OJN_API_NOTES` #21). Replies are therefore complete MP3s, optionally sentence-level and queued (§6.2.6). **Do not build a progressive/streaming path for the rabbit speaker.**  
- No local LLM (Bolt runs local STT; the LLM is a cloud API — the local-LLM chapter belongs to the VENTUNO Q profile later).  
- ~~No barge-in: half-duplex only.~~ **No longer a non-goal (v2.3).** It rested on "there is no AEC reference for the rabbit's speaker" — still true *for that speaker*. Routing output through the reSpeaker instead gives the XVF3800 the far-end reference over USB, so its on-chip AEC can remove it; that was verified on hardware (`brain/scripts/aec-probe.py`) before anything depended on it. Full-duplex realtime mode followed (§6.2.10). **Half-duplex remains the default** and the automatic fallback: barge-in is available only with `audio_out.backend: local`.  
- No multi-rabbit support.

## 4\. Hardware & Base Software

| Item | Choice | Notes |
| :---- | :---- | :---- |
| Body | Nabaztag:tag (V2) stock | WPA-capable (unlike V1). Speaker, ear motors, LEDs, RFID, button all used via OJN |
| Server | OpenJabNab, **self-hosted on the Bolt** | github.com/OpenJabNab/OpenJabNab (PHP \+ C++/Qt daemon). Self-host rather than use a public instance: full API control, LAN latency, source-level inspection, raw-frame experiments, and optional custom plugins if Gate G0 requires them |
| Brain host | UDOO Bolt, Linux (Ubuntu 22.04+) | Runs OJN, rabbit-brain, MCP server, local Whisper, Piper — one box |
| Audio in | reSpeaker Flex XVF3800 Circular-4, USB mode | On-chip beamforming/NS/AGC/de-reverb \+ DoA. Mic disc placed at the rabbit's base, core board hidden. Hardware-verified: ALSA card `C16K6Ch`, **16 kHz / 6 channels**, ch0 the clean one (`channels: 6, selected_channel: 0`) |
| Audio out | **Switchable: the rabbit's speaker (default) or a Bolt device** | `audio_out.backend: rabbit \| local` (§6.2.6). The rabbit is the charm; the local path is the one that can be *cancelled*, which is what barge-in needs. Prefer the reSpeaker's own output (3.5 mm jack, or JST for an amplified speaker — up to 10 W into 4 Ω): the XVF3800 takes the far-end reference over USB, so its AEC can cancel it from the mics |
| Network | **Dedicated legacy AP \+ direct OJN server config** | See §4.1 — this is a blocking prerequisite, not a detail |

### 4.1 Network foundation (Setup S0 — **blocking, do this first**)

The Nabaztag:tag speaks **802.11b/g on 2.4 GHz with WPA-TKIP only**. It cannot associate to a WPA2-only or WPA2/WPA3-mixed AP. This was confirmed in practice: a FRITZ\!Box 4060 (Wi-Fi 6\) offers WPA2 and WPA2+WPA3 mixed mode, but no pure WPA/TKIP — the rabbit sits at all-orange, failing authentication. **No amount of downstream work matters until the rabbit is on the network**, so this is task S0 and it gates everything.

**Do not downgrade the main network.** The right move is a dedicated legacy segment for the rabbit, isolated from the home LAN, with the Bolt bridging between them. Architecturally this is also cleaner: the rabbit ends up on a segment that talks only to its own brain.

**Recommended: the Bolt itself is the legacy AP, using its own M.2 (key E 2230) Wi-Fi module** — confirmed AP-mode capable and running the WPA1/TKIP segment on this build, with the uplink on wired Ethernet. **No USB dongle is needed**; one is only a fallback if your board's module refuses AP mode. One box, no extra hardware, fully reproducible from the repo.

`ojn/deploy.sh` provisions it. Sketch of the `hostapd` config (`ojn/network/hostapd.conf`):

interface=wlan1              \# the Bolt's M.2 radio; keep Ethernet for LAN/uplink

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

│ (stock, WPA)  │Viol│ │ self-hosted│ native \+ raw frames │  wake→STT→LLM(tools)→TTS    │ │

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

Note: the `+choreo plugin` block was **NOT built** — Gate G0 found native VAPI sufficient (see §6.1). The one plugin that does exist is `ojn/plugin_events/`, for event *egress*, because the stock `callurl` path proved broken on hardware. All body output funnels through the `BodyController`.

**Audio out path (turn-based):** brain generates TTS → writes MP3 → **Apache** serves it → OJN API tells the rabbit to stream that URL. *The MP3 must be served by Apache, not by the brain's own aiohttp server:* the MTL decoder GETs an aiohttp-served file, receives 200 OK — and plays nothing. Same file via Apache plays (`OJN_API_NOTES` #12). The built-in `Mp3Server` therefore runs storage-only in production.

**Audio out path (realtime / local backend):** PCM deltas from the model go straight to a PortAudio device on the Bolt — no MP3, no OJN, no decoder. Ears and LEDs still play on the rabbit, so it stays animated while the sound comes from elsewhere.

**Audio in path:** reSpeaker USB → ALSA → brain. Never through the rabbit; the 2006 hardware has no usable microphone path for this.

## 6\. Components

### 6.1 OpenJabNab deployment \+ choreography capability

> **RESOLVED (Gate G0, July 2026): the choreography plugin is NOT needed, and the event plugin IS.** Native VAPI `chor=` covers arbitrary ear positions, per-LED true RGB on all five LEDs, and timed ear+LED sequences — all hardware-confirmed. What native OJN could *not* do was let events out: the stock `callurl` fallback failed on hardware (OJN sends the `CU` packet, the bootcode never performs the HTTP request — zero DNS/TCP toward the target), so `ojn/plugin_events/` (~100 lines, GPL v2) posts a webhook on `OnClick`/`OnRFID` instead. Two further field rules came out of bring-up: OJN is Qt4 code that does not build on Qt5, so the daemon runs in a pinned **Debian buster container**; and the rabbit's bootcode needs a **hostname** for the XMPP server (an IPv4 literal is resolved literally), which dnsmasq provides as `ojn.local`.

- Deploy OJN on the Bolt (Apache/Lighttpd \+ PHP wrapper \+ C++ daemon). Register the rabbit, verify built-ins: MP3 stream, sleep/wake, ear presets, RFID and button events. (`tts/say` is *not* one of them — OJN's own TTS backends are long dead; the audio smoke test is `api_stream.jsp`.)

**Feasibility gate before any plugin work (see Phase 0) — kept for the record; this is the reasoning that produced the verdict above.** OJN already exposes a low-level `packet/sendMessage` endpoint that injects raw Violet-protocol frames to the rabbit. Before deciding a custom plugin is needed, empirically determine — on the real rabbit — how much expressivity is reachable through native OJN \+ hand-crafted `packet/sendMessage` frames:

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
3. **VAD:** silero-vad; end-of-speech at 1600 ms silence (700 then 1200 ms proved too eager for natural mid-sentence pauses on hardware). **Used only by the `cloud`/`local` STT profiles** — with `flux` the provider decides and this window does not exist.
4. **STT — three profiles behind the `STTProvider` interface, selected in `config.yaml`:**  
   - `flux` (**production**): **Deepgram Flux**, which does recognition *and* end-of-turn detection in one pass, replacing the client-side silence window entirely. Providers advertise `detects_end_of_turn` and the pipeline branches on it, so the two endpointing styles are not tangled together. `EagerEndOfTurn` is recorded for diagnostics but **never acted on** — no speculative dispatch.  
   - `cloud`: nova-3 (`deepgram.model`, swappable without code changes) \+ the local Silero window. Kept as the fallback profile, not deleted.  
   - `local`: faster-whisper (CTranslate2) on Bolt CPU, `small` or `medium` int8, RTF logged for the video's local-vs-cloud segment.  
   - **`language: multi` is deliberate.** Automatic it/en code-switching is the desired behaviour, not a misdetection to be fixed: a transcript that came back "English" was in fact an English phrase.  
5. **Agent loop:** LLM behind a provider-neutral `LLMProvider` interface (`llm/base.py`). **OpenAI is the active provider** (Responses API, streaming \+ function calling; model configurable via `llm.model`, never hardcoded) — the earlier "Claude API" wording was a leftover from v1.0; the interface is what keeps the choice swappable. Production is **`gpt-5.4-mini` \+ `reasoning_effort: low`**, picked by A/B (`brain/scripts/llm-bench.py`). **Decide on `final_text`, not on first token:** TTS cannot start until the text is complete, so a faster first token buys nothing here. Rolling history (`llm.max_history_turns`), personality prompt (`prompts/system.md`), tool rounds capped (`llm.max_tool_rounds`). `OPENAI_API_KEY` comes only from the environment and is never logged.  
6. **TTS: self-hosted Piper (production), Deepgram Aura as automatic fallback.** Italian `it_IT-paola-medium` at `length_scale` **1.25** and English `en_GB-alba-medium` at **1.0**, chosen by listening *through the rabbit* per language — the only test that counts on a small speaker and an ancient decoder. Voice is routed by **the STT's own detected language**, never by text heuristics. Piper runs as a persistent HTTP server per language; the engine is GPL-3.0 and stays an external localhost process, with no code vendored. Alba is CC BY 4.0: **attribution is required** (Alba dataset, University of Edinburgh). Output is a complete MP3 in `www/audio/`, served by Apache (§5); long replies may be split into sentence-level files queued sequentially.  
7. **Half-duplex gate:** mic pipeline pauses from the OJN play command until estimated playback end (+300 ms guard), keyed on `BodyController.audio_busy` so queued-but-not-yet-started audio counts too. Capture is **drained, not paused**, during processing — an unread queue starves the pipeline.  
8. **DoA behavior:** on wake word, read DoA angle → map to an ear gesture "turning toward the speaker" before the listening pose. **Choreography-only, never `posleft`/`posright`** — that path makes the firmware play a long carillon (`OJN_API_NOTES` #7). DoA reads are time-bounded so a USB stall cannot freeze the feedback, and fail open.  
9. **Event handlers:** RFID tag ID → named intents (`intents.yaml`), e.g. atlas card → fetch Physical AI Atlas RSS/JSON → summarize → speak. *(Next phase — see §8.)*  
10. **Realtime mode (`conversation.mode: realtime`) — full-duplex, the v2.3 addition.** One WebSocket (OpenAI Realtime) carries audio both ways: the wake word opens a **continuous conversation** that is not re-armed per turn, and the user can talk over the rabbit. **Requires `audio_out.backend: local`**, and the runtime refuses the combination on the rabbit backend rather than half-supporting it. On `speech_started` the local stream is cut and `conversation.item.truncate` reports how much was *actually heard* — get that wrong and the model's transcript keeps words the user never heard, and every later turn reasons from fiction. **What it costs:** the model generates its own voice, so Paola/Alba are not used — which is exactly why this is a mode, not a replacement. Any WebSocket/API failure re-arms `turn_based` in place, without restarting the service.
11. **Latency: accepted and CLOSED for v1.** The wake→first-audio budget is met and the residual variability is LLM-dominated. Explicitly not to be built — speculative/anticipatory TTS, progressive MP3 (hardware-rejected, §3), further LLM benchmarking. Re-open only if a hardware or firmware assumption changes.

### 6.3 Body tools (LLM)

The model improvises body language; nothing is scripted. Production tool set is **`express(spoken_text, ears?, gesture?, mood?)`**, plus `get_direction()` and `body_state()`.

`express` carries the reply *inside* the tool call, and that is a finding rather than a preference: the model does **not** reliably emit free text alongside a separate gesture call, so asking for both cost an extra round-trip on every turn. `gesture_ears` / `set_mood_lights` / `play_gesture` are no longer offered to the LLM but remain executable for MCP. **All body tools are choreography-only** (see §6.2.8).

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

> **Hardware constraint that shapes every indicator built on top of this (`OJN_API_NOTES` #8).** `submit()` confirms **local enqueue only** — nothing reports execution on the rabbit — and a new `chor=` does **not** replace the running one: it **queues behind it**, with no cancel or replace anywhere in the API.
>
> The rules that follow are load-bearing: indicators are **event-driven**, at most one short choreography per state transition, never on a timer; the single repeating indicator (the rabbit speaking) uses a tick *shorter* than its own period, so at most one is ever in flight; cosmetic commands carry **deadlines** and are dropped rather than fired late into a state they no longer describe; and teardown drops everything still queued locally before sending the neutral terminator **above** the cosmetic priority. Since what has already reached the rabbit cannot be recalled, the only available lever is refusing to make that remote queue longer.

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

`PlaybackHandle` is what makes the half-duplex gate, speech-synced gestures, and preemption implementable rather than guessed. **Confirmed on hardware:** OJN offers no true cancel and no playback-finished callback, exactly as this section anticipated. `OjnAdapter` declares `can_cancel_audio=False`, `has_playback_events=False`, `can_read_body_state=False`, `has_per_led_rgb=True`, `ear_range=(0,16)`; `estimated_duration_s` comes from the MP3's own duration and `wait_finished` is a timer (+ guard). This is also the deeper reason barge-in needed a different output path entirely (§3): you cannot interrupt a body that cannot be interrupted. **The `BodyController` must consult `capabilities` and never promise a preemption the body cannot physically honor** — where cancel is unavailable it degrades to "let the current utterance finish, drop the queued ones." Implementations: `OjnAdapter` (v1, this spec) → `ReachyMiniAdapter` (P1: SDK client to the robot's FastAPI daemon over LAN, :8000) → `VentunoQLocalProfile` (P2: same brain deployed on Arduino VENTUNO Q, local STT/TTS/LLM — placeholder module \+ README only for now). The `BodyController` (§6.4) sits above whichever adapter is active and is body-agnostic — swapping bodies means swapping only the adapter beneath it.

### 6.7 Runtime, deployment and recovery (new in v2.3)

**One process owns the conversation.** `python -m rabbit_brain.runtime --config config.yaml` creates exactly one `BodyController`, one `Mp3Server` (:8090), one `EventListener` (:8091), one STT/TTS/LLM stack and one `VoicePipeline`. **The runtime and the MCP server must not run at the same time** — both bind those ports and each would create a second `BodyController`, i.e. two owners of a body whose whole design assumes one.

**Exactly one consumer of the microphone**, always. Every failure path in the realtime branch is bounded for this reason and no other: a teardown step that hangs stops the capture being drained, and the run is over. Where a bounded wait is not enough — an audio stream that will not release the device — the runtime **terminates** and lets systemd's `Restart=always` rebuild the process, because the alternative is worse than a restart: the wake word fires, the LEDs light, the reply is silent, and every log line says the system is healthy.

**systemd:** `deploy/nabaztag-runtime.service` and `deploy/nabaztag-recovery.service`, installed and enabled at boot. (`StartLimitIntervalSec`/`StartLimitBurst` belong to `[Unit]`, not `[Service]` — systemd ignores them silently in the wrong section.)

**Recovery — deliberately narrow.** `deploy/rabbit_recovery.py` restarts **hostapd and nothing else**, and only when three signals coincide (rabbit not associated \+ last HTTP old \+ stale `:5222` socket), with a cooldown, a per-outage cap and an hourly hold so it cannot loop when the rabbit is simply switched off. **It never restarts OJN and never reboots the host** — neither addresses the failure it targets (`OJN_API_NOTES` #13). The decision logic is a pure function with no I/O, so it is fully testable; "last HTTP" is parsed from the newest Apache log line whose client is the rabbit's IP, because file mtime is not a valid proxy.

## 7\. Repository Layout

nabaztag-ai/

├── ojn/                  \# OJN deployment scripts \+ the one plugin we needed

│   ├── deploy.sh         \# install OJN on the Bolt; provisions the legacy AP (§4.1)

│   ├── docker/           \# Debian buster image (OJN is Qt4; won't build on Qt5)

│   ├── apache/           \# PHP http-wrapper vhost \+ the /brain-audio/ alias (§5)

│   ├── network/          \# hostapd.conf.example, dnsmasq.conf.example, nftables rules

│   ├── piper/            \# install-piper.sh: pinned engine \+ voices, systemd units

│   └── plugin\_events/    \# GPL v2: webhook egress for click/RFID (callurl is broken)

├── brain/

│   └── rabbit\_brain/

│       ├── audio/        \# capture, wakeword, vad, doa, pipeline, output, streaming

│       ├── stt/          \# flux.py, deepgram.py, openai\_whisper.py, local\_whisper.py

│       ├── llm/          \# agent loop, tools, realtime.py (full-duplex session)

│       ├── tts/          \# piper\_tts.py, deepgram\_tts.py, elevenlabs.py, mp3 server

│       ├── body/         \# BodyController, BodyAdapter, ojn\_adapter.py, chor.py

│       └── runtime.py    \# THE single-owner process (§6.7)

├── deploy/               \# systemd units, install-runtime.sh, rabbit\_recovery.py

├── mcp/

├── demos/                \# scripted video scenarios incl. RFID \+ DoA tricks

├── config.example.yaml   \# committed. stt\_profile, tts\_profile, conversation.mode,

│                         \# audio\_out.backend, ojn host. Real config.yaml is gitignored

├── intents.example.yaml  \# committed: placeholder RFID tag IDs → intent mapping

├── moods.yaml            \# committed: mood → LED/ear mapping (no secrets, tweakable on camera)

└── .env.example          \# committed. OPENAI\_API\_KEY, DEEPGRAM\_API\_KEY, PIPER\_URL\_IT/\_EN,

                          \# OJN\_TOKEN, RABBIT\_MAC, RABBIT\_IP

**Gitignored (never committed):** `config.yaml`, `intents.yaml` (contains real RFID tag UIDs — personal data, and a physical-access credential of sorts), `.env`, `www/audio/`, model weights.

*Consequence worth stating, because it bit:* since `config.yaml` is gitignored, `git pull` can never migrate it, so a config created from an older example silently keeps stale settings. `brain/scripts/config-doctor.py` reports the drift and `--fix` rewrites present-but-stale keys in place; CI runs it too.

## 8\. Phases & Acceptance Criteria

**S0 — Network foundation (manual, BLOCKING).** Per §4.1: legacy WPA/TKIP 2.4 GHz AP on the Bolt (`hostapd` \+ `dnsmasq`), isolated subnet, static lease for the rabbit. ✅ **Gate S0:** rabbit associated, static lease held, and its HTTP bootcode requests observed on the Bolt (the firmware does not answer ping — see §4.1 field note); rabbit isolated from home LAN/internet; main Wi-Fi untouched. *Nothing downstream can start until this passes.* **Status: PASSED (July 2026)** — the Bolt's own M.2 radio running a channel-11 WPA1/TKIP AP; `GET /vl/bc.jsp?...` seen from the rabbit's static IP.

**S1/S2 — OJN bring-up (manual):** OJN deployed on the Bolt; rabbit's "Violet Platform address" pointed at the Bolt's OJN instance (DNS override via the same dnsmasq only as fallback, §4.1); rabbit registered. ✅ `tts/say` from the OJN web UI makes the rabbit speak; button \+ RFID events visible in OJN logs. **Status: PASSED (July 2026)** — Qt4 daemon containerized (OJN doesn't build on Qt5; see `ojn/docker/`), XMPP session established, rabbit registered to a persistent account, VAPI ear commands verified on hardware. Field notes: the bootcode requires a **hostname** (not IPv4 literal) as XMPP server — dnsmasq resolves `ojn.local`; OJN's own TTS backends are dead, so the audio smoke test is `api_stream.jsp`, not `tts/say`; button/RFID event visibility moves to Phase 0's remaining probes.

**Phase 0 — Feasibility gate (real hardware, no product code yet).** Empirically map what the *actual* rabbit can do through OpenJabNab. This phase de-risks every phase after it and its findings decide whether T1 is even needed. Probe, on the physical rabbit, and record results in `docs/OJN_API_NOTES.md`:

- native OJN built-ins actually confirmed working (TTS, MP3-by-URL, ear presets, sleep/wake, nose LED, RFID/button events);  
- raw `packet/sendMessage`: can arbitrary ear positions be driven? individual LED RGB? what exact frames work?;  
- event egress path that works (webhook plugin vs polling) and its real latency;  
- OJN round-trip latency for a command (feeds the p50 budget and BodyController deadlines). ✅ **Gate G0:** a written capability matrix (works / works-via-raw-frame / needs-plugin / not-possible) for ears, LEDs, audio-out, events. The plugin decision (§6.1) and the latency targets are set from this matrix, not assumed. **If a load-bearing capability is impossible even via plugin, the architecture is revised here — cheaply — before anything depends on it.** **Status: PASSED (July 2026)** — matrix in `OJN_API_NOTES`; verdict: no choreography plugin, but an events plugin (§6.1).

**Phase 1 — Body control \+ BodyController \+ MCP:** `OjnAdapter` over whatever Phase 0 proved (built-ins and/or raw frames); `BodyController` arbitration layer; MCP server on top of the controller. ✅ From Claude Desktop: rabbit speaks an arbitrary sentence and moves its ears; two competing commands submitted at once resolve by priority (no contention, no incoherent motion). Mock-OJN mode (`--mock-ojn`) covers adapter \+ controller in unit tests. **Status: PASSED on hardware (July 2026)** — ears, LEDs, choreography and click events, all through MCP.

**Phase T1 (conditional, parallel) — Choreography plugin:** only if Phase 0 flagged `needs-plugin`. **Status: SKIPPED — native VAPI sufficed (§6.1).** Do not create `ojn/plugin_choreo/`.

**Phase 2 — Voice pipeline:** reSpeaker capture, wake word, VAD, STT profiles, TTS→MP3→OJN playback, half-duplex gate. ✅ Full it/en conversation; p50 wake→first-audio within the budget set by Phase 0's measured OJN latency (target ≤ 4.0 s) over 20 runs; `stt_profile: local` works end-to-end with measured RTF logged. **Status: PASSED (July 2026)**, then superseded in its details by the latency campaign (§6.2.4, §6.2.11).

**Phase 3 — Embodiment \+ DoA \+ RFID:** tools in agent loop (all body output via `BodyController`), DoA ear-turn reflex on wake, RFID intents. ✅ ≥ 8/10 varied prompts show spontaneous plausible body language; rabbit visibly orients ears toward speaker positioned 90° off-axis; Atlas RFID card triggers spoken news summary; DoA reflex and agent expression never fight (controller priority verified). **Status: agent loop, embodiment and DoA PASSED; RFID intents still open** (Maurizio has no physical tag yet — the egress path itself is confirmed for clicks).

**Phase 3.5 — Full-duplex realtime \+ runtime hardening (added in v2.3).** `conversation.mode`, switchable audio output, AEC verification, systemd \+ boot, hostapd recovery. ✅ **PASSED on hardware (July 2026):** wake opens a continuous conversation, the user can interrupt mid-sentence, the body animates for as long as the reply is actually playing, cleanup leaves no residue, and a second session works after the first. **Closed; no further cosmetic optimisation for now.**

**Phase 4 — Features & demo:** RFID intents, news/briefing, timers; 3 repeatable demo scripts; latency dashboard; `ReachyMiniAdapter` skeleton \+ docs. ✅ Two clean consecutive runs of each demo; ARCHITECTURE.md documents the Reachy and VENTUNO Q profiles. **Status: current phase.**

> **Parked, on purpose:** the custom **"Nabaztag" wake word**. The training pipeline is finished and reproducible (pinned openWakeWord \+ sample-generator pair, ONNX-only, inside a pinned Python 3.10 container because `piper-phonemize` has no 3.12 wheel), but training itself is a laptop-side task and does not block anything. `hey_jarvis` stays in the meantime — it was always a smoke-test placeholder, and it is worth saying out loud in any demo that the rabbit is not yet answering to its own name.

## 9\. Risks

| \# | Risk | Mitigation |
| :---- | :---- | :---- |
| R1 | OJN choreography protocol under-documented | **CLOSED by Gate G0.** Native VAPI `chor=` proved sufficient; formats documented in `OJN_API_NOTES` §2. No plugin |
| R2 | No AEC reference for rabbit speaker → echo | **Still true for the rabbit's speaker** — the half-duplex gate (§6.2.7) remains the mitigation there. **Solved for the local path:** routing output through the reSpeaker gives the XVF3800 the far-end reference over USB; measured, not assumed (§3). Barge-in is enabled only on that path |
| R3 | OJN event latency (rabbit polls server on Violet-protocol ping interval) | Tune ping interval frame; accept 1–2 s event latency for RFID/button (not in the voice path) |
| R4 | Stock V2 Wi-Fi link quality/stability once associated | Dedicated 2.4 GHz SSID, no band steering, fixed channel, rabbit close to AP; static DHCP lease. **Materialised as a slow disassociation-for-inactivity** — see the narrow hostapd recovery in §6.7 |
| R5 | MP3-per-sentence gaps sound choppy | Pre-generate next sentence while current plays; tune queue. Measured gap between queued files: **~1.7 s** |
| R8 | *(v2.3)* A cosmetic body command outliving the moment it describes | The rabbit's choreography queue cannot be cancelled (§6.4). Event-driven indicators, deadlines on cosmetics, terminator above cosmetic priority — never a timer loop for a long-lived state |
| R9 | *(v2.3)* An audio backend that is held but silent, while logs look healthy | Bounded teardown everywhere; if the device cannot be reclaimed the runtime **exits** and systemd rebuilds it (§6.7). Never re-arm on a dead backend |
| R6 | **V2 cannot join WPA2-only modern Wi-Fi** (802.11b/g \+ WPA-TKIP only; confirmed on a FRITZ\!Box 4060\) | Dedicated legacy AP per **§4.1** (hostapd on the Bolt: `wpa=1`, `wpa_pairwise=TKIP`, `eapol_version=1`), isolated subnet. **Blocking: Gate S0, before everything** |
| R7 | The rabbit's segment uses broken 2006-era crypto (TKIP) — an attack surface on the home network | Segment isolation is part of the mitigation, not an optional extra: separate subnet, no route to home LAN or internet, firewall limited to rabbit ⇄ Bolt (§4.1) |

## 10\. Kickoff Prompt — HISTORICAL

> Kept verbatim as a record of how the project was started; it is **not** current instructions. The bootstrap it describes is done, Gate G0 answered its central question (no choreography plugin), and §6/§8 above describe what actually exists. Its one instruction that aged well is worth carrying forward: *do not assume any OJN endpoint exists* — nearly every hardware round in `OJN_API_NOTES` began with an assumption that turned out to be wrong.

Read docs/ARCHITECTURE.md. First execute §11.4 (open-source repo bootstrap), including `ojn/network/` with the `hostapd`/`dnsmasq`/firewall example configs from §4.1 (passphrases come from `.env`, never committed) and a `deploy.sh` that provisions the legacy AP and OJN on the Bolt. Note that Gate S0 (rabbit on the network) and the hardware half of Phase 0 are Maurizio's to run — your job is to make them scriptable and reproducible.

**Do not assume any OJN endpoint exists.** Before writing the adapter, clone and study the OpenJabNab repo (github.com/OpenJabNab/OpenJabNab): find the *actual* API surface (the real path for TTS, MP3-by-URL, ear/LED control, the `packet/sendMessage` raw-frame endpoint, and how events are exposed). Record exactly what exists, with real paths and payloads, in `docs/OJN_API_NOTES.md`. This is the software half of the Phase 0 feasibility gate; together with the hardware probing it produces the capability matrix (Gate G0) that decides whether the choreography plugin is needed.

Then implement Phase 1 against the *verified* surface: `brain/rabbit_brain/body/ojn_adapter.py` (only endpoints you confirmed), the `BodyAdapter` protocol with `PlaybackHandle` and `BodyCapabilities` (§6.6), and the `BodyController` arbitration layer (§6.4) with its priority queue and single-consumer loop. The controller must consult `capabilities` and never promise a preemption the body cannot honor. Provide a `--mock-ojn` simulator and unit tests covering adapter and controller (priority preemption, coalescing, deadline drops, and the no-cancel degradation path). Finally scaffold `mcp/` exposing speak/move\_ears/set\_leds **through the BodyController**, not the adapter directly. Do not implement audio capture yet.

## 11\. Open Source Release

This project is public on GitHub **from the first commit** (building-in-public: each completed phase is content for LinkedIn/YouTube).

### 11.1 Licensing structure

- Root `LICENSE`: **Apache-2.0** — covers `brain/`, `mcp/`, `demos/`, docs, configs.  
- `ojn/plugin_events/LICENSE`: **OJN's GPL v2, copied verbatim.** The events plugin compiles against OpenJabNab's codebase and is a derivative work, so it inherits OJN's license — the exact SPDX identifier and text are copied from the OJN repository, since `GPL-2.0-only` and `GPL-2.0-or-later` are different licenses and the choice is not ours to make. The directory stays cleanly separated: no code shared with `brain/`; the brain talks to it only over HTTP, keeping the Apache-2.0 side unencumbered. (`plugin_choreo/` was never created — Gate G0.)  
- Preferred endgame for the plugin: upstream it as a PR to OpenJabNab; keep only deployment scripts here.  
- **Third-party terms that bind us in practice:** Piper's engine is **GPL-3.0**, so it runs as an external localhost process and no code is vendored; the `en_GB-alba-medium` voice is **CC BY 4.0** and requires attribution (Alba dataset, University of Edinburgh). The `respeaker/reSpeaker_Flex` repo declares **no license at all** — never vendor its code; DoA uses either the vendor tool as an external one-shot command or our own PyUSB client.  
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
