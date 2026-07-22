# OpenJabNab API Notes — Phase 0, software half

Source-verified against the OpenJabNab repository (github.com/OpenJabNab/OpenJabNab, `master`,
July 2026). **"Verified (source)" means the endpoint exists and its behavior is readable in the
daemon code; "hardware-pending" means the real rabbit still has to confirm it (Gate G0's
hardware half, run by Maurizio).** File references are to the OJN repo.

## 1. Entry points & auth

| Surface | Where | Notes |
| :--- | :--- | :--- |
| Daemon "HTTP" port | `localhost:8080` (config `OpenJabNabServers/ListeningHttpPort`) | ⚠️ **Not plain HTTP** (S1 field finding): it speaks OJN's internal framing (`pack("LCa*")` length+type+payload — see `openjabnab.php`). Never `curl` 8080 directly; every HTTP test goes through Apache on :80 |
| Rabbit XMPP | `:5222` on all interfaces (`ListeningXmppPort`) | V2 rabbits speak XMPP to the server; commands are pushed, not polled — good for latency |
| Admin/plugin API | `GET /ojn_api/<call>` | Router: `server/lib/apimanager.cpp:32` (`httphandler.cpp:38` strips the prefix) |
| Violet-compatible API (VAPI) | `GET /ojn/FR/api.jsp` and `/ojn/FR/api_stream.jsp` | `bunny.cpp:55 ProcessVioletApiCall`. Auth: `sn=<serial>&token=<vapi token>` |

- **Account token** (for `/ojn_api/...`): `GET /ojn_api/accounts/auth?login=..&pass=..` → token
  (`accountmanager.cpp:206`; password hashing details to confirm at runtime). Most other calls
  take `&token=...`; without it you are the Guest account. Several calls require admin.
- **VAPI token** (per bunny): enable + read via
  `/ojn_api/bunny/<bunnyid>/enableVAPI`, `getVAPIToken`, `setVAPIToken?tk=..`,
  `setPublicVAPI?public=..` (`bunny.cpp:692-698`).
- Plugin-per-bunny calls (`/ojn_api/bunny/<id>/<plugin>/<fn>`) require the plugin registered to
  that bunny first: `/ojn_api/bunny/<id>/registerPlugin?name=<plugin>` (`apimanager.cpp:154`
  exempts only System/Required plugins).
- All API answers are XML (`<api>...</api>`; VAPI answers are `<rsp>...</rsp>`).

## 2. Capability matrix (Gate G0, software half)

| Capability | Verdict | How |
| :--- | :--- | :--- |
| TTS (server-generated) | **works-native** (source) | VAPI `api.jsp?...&tts=<text>[&voice=..]` (`bunny.cpp:159`) or `/ojn_api/bunny/<id>/tts/say?text=..` (`plugin_tts.cpp:24`). Sends `MU <file>\nPL 3\nMW\n`. We won't use OJN TTS in v1 (ElevenLabs/Piper instead) but it's the S1/S2 smoke test |
| **MP3 by URL, queued** | **works-native — HARDWARE CONFIRMED (July 2026)** | VAPI `api_stream.jsp?...&urlList=url1|url2|url3` → `ST url\nMW\nST url\nMW\n` (`bunny.cpp:67-74`). **Sentence-level MP3 queueing (§6.2.6) is a single native call** — the `|`-separated list *is* the queue. Verified on the real rabbit: two local MP3s requested and played in order (access log 15:02:14 → 15:02:19). No cancel, no finished-callback anywhere → `can_cancel_audio = False`, duration-timer approach confirmed. **No plugin needed for the audio queue** |
| **Arbitrary ear positions 0–16** | **works-native — HARDWARE CONFIRMED (S2, July 2026)** | VAPI `api.jsp?...&posleft=0..16&posright=0..16` → `AmbientPacket::SetEarsPosition` (`bunny.cpp:138-153`). Range-checked 0–16. Verified on the real rabbit right after registration. **No plugin needed** |
| Per-LED RGB | **works-native via chor — HARDWARE CONFIRMED (July 2026)** | No standalone LED call, but VAPI `chor=` compiles a Violet `.chor` binary server-side and pushes `CH <path>` (`bunny.cpp:168-204`). A 1-action chor sets one LED. LEDs: `0=bottom, 1=left, 2=middle, 3=right, 4=top` (`choregraphy.h:13`). Verified on the real rabbit: bottom red, left green, middle blue, right yellow, top cyan, then all off — true RGB on all 5 LEDs |
| Timed choreography (ears+LEDs) | **works-native — HARDWARE CONFIRMED (July 2026)** | Same `chor=` param. Text format (`choregraphy.cpp:73 Parse`): `tempo,{time,motor,ear,angle,0,dir | time,led,led#,r,g,b},...` — tempo in ms/tick (10..2550, stored /10), `time` in ticks relative to sequence start, motor: `ear` 0=left 1=right, `angle` in degrees (encoded /18 → 0..16 steps of 18°), `dir` 0=fwd 1=back. Verified end-to-end: VAPI answers CHORSENT, the rabbit fetches the generated `.chor` over HTTP (200 OK via the shared http-wrapper mount) and plays the timed sequence |
| Sleep / wake | **works-native** (source) | VAPI `api.jsp?...&action=13` (wake) / `action=14` (sleep) (`bunny.cpp:120-127`) |
| Raw frames | **works-native** (source) | `/ojn_api/bunny/<id>/packet/sendPacket?data=<hex>` (raw bytes) and `packet/sendMessage?msg=<text>` (wrapped in a MessagePacket) (`plugin_packet.cpp`). Known message verbs from plugin sources: `MU <path/url>` play MP3, `ST <url>` stream, `PL <n>` playlist/jingle choice, `MW` wait-end-of-playback, `CH <chor path>` run choreography, `CU <url>` make the rabbit call a URL |
| **RFID / button events egress** | **needs-plugin — plugin built and HARDWARE CONFIRMED for clicks (July 2026)** | Real single and double clicks delivered end-to-end (rabbit → OJN → `ojn-plugin-events` → webhook on 127.0.0.1:8091). RFID rides the same `OnRFID` hook but awaits a physical tag to verify. Background: no webhook and no event-polling endpoint exist upstream. `getlast/getlasts` (`bunny.cpp:961-989`) only expose connection metadata, admin-only. Events are dispatched to C++ plugins (`OnClick`, `OnRFID` — `plugininterface.h:47-49`). **The stock `callurl` fallback FAILED on hardware:** the click reaches OJN and callurl sends the `CU <url>` packet (confirmed and decoded in the XMPP traffic), but the OJN bootcode never performs the HTTP request — zero DNS/TCP toward the target, even with an IPv4 literal on port 80. Since RFID/callurl shares the same final `CU` leg, the fallback is disqualified. **Resolution: `ojn/plugin_events/`** (GPL, ~100 lines) fires a server-side GET to a per-bunny webhook on `OnClick`/`OnRFID`; brain side is `rabbit_brain.body.events_server.EventListener` (default 127.0.0.1:8091). Note this is NOT the choreography plugin — see verdict below |
| Ears/LED state readback | **not-possible** (source) | VAPI `ears` param answers a hardcoded `POSITIONEAR 0,0` TODO (`bunny.cpp:164-167`). BodyController must own state-tracking (it already does by design) |

## 3. Gate G0 verdict (software half)

**The T1 choreography plugin is NOT needed.** Native VAPI covers arbitrary ears, per-LED RGB,
timed choreographies, queued MP3-by-URL, and sleep/wake; `packet/sendMessage` covers anything
exotic left over. `ojn/plugin_choreo/` stays uncreated (per §11.4 / Gate G0 rules).

The only gap is **event egress**: the stock `callurl` fallback was disqualified on hardware
(the bootcode ignores `CU` — see the matrix row), so the small webhook plugin
`ojn/plugin_events/` (GPL, cleanly separated per ARCHITECTURE §11.1) fills it. The T1
choreography plugin remains unnecessary.

Hardware half — status on the real rabbit:

1. ~~`tts/say` audible~~ → replaced: OJN's TTS backends are dead 2010 endpoints; audio is
   smoke-tested with `api_stream.jsp` instead (see S1/S2 findings below).
2. **DONE** — `api_stream.jsp` with a 2-MP3 `urlList` from a local server: rabbit fetches and
   plays both in order (access log 15:02:14 → 15:02:19). **Inter-sentence gap ≈ 1.7 s**
   (first MP3 is 3.344 s, second request ~5 s later; approximate — Apache logs have 1 s
   resolution). Consequences: (a) when estimating playback duration for an N-URL queue, add
   ~1.7 s per boundary (`TimedPlaybackHandle` callers / TTS layer); (b) R5 ("choppy" gaps) is
   real — worth re-measuring precisely with real TTS sentence pairs and considering fewer,
   longer MP3s per reply.
3. **DONE** — arbitrary `posleft/posright` confirmed on hardware (S2). Motion time still to measure.
4. **DONE** — per-LED RGB + timed choreography confirmed: 5-LED color sequence (red, green,
   blue, yellow, cyan) then off; CHORSENT answered and `.chor` downloaded by the rabbit with
   HTTP 200 (validates the shared RealHttpRoot bind mount end-to-end).
5. **DONE for clicks; RFID awaits a tag** — callurl FAILED (the bootcode ignores the `CU`
   verb: the packet is sent and decoded on the XMPP wire, but the rabbit performs no
   DNS/TCP/HTTP). Event egress = `ojn/plugin_events/` webhook plugin: **real single and
   double clicks verified end-to-end** on 127.0.0.1:8091 (July 2026, `python3 -m http.server`
   as receiver — the brain-side `EventListener` needs the venv on the Bolt, not yet created).
   RFID rides the same `OnRFID` hook; verify when a physical tag is available. Event latency
   still to measure (risk R3 expects 1–2 s from the rabbit's ping interval).
6. **OPEN** — precise round-trip latency of a VAPI ear command (feeds BodyController deadlines
   and the p50 budget).
7. **RESOLVED — long jingle came from `posleft/posright` (July 2026).** During real
   audio-in tests the rabbit played a long carillon although the pipeline sent no audio.
   Confirmed on hardware: with the DoA wake reflex routed exclusively through `chor=` the
   jingle disappears completely; the earlier `posleft/posright` path
   (`AmbientPacket::SetEarsPosition`) reproduced it every time. **Rule: the wake feedback
   and the DoA reflex are choreography-only — never use `posleft/posright` for reflex
   motion** (`OjnAdapter.set_ears` stays only for explicit MCP/agent ear poses). This also
   sidesteps `EarsCommand` coalescing (two same-priority ear commands collapse to the last,
   which had silently dropped the DoA bias). The brain's wake ack is a single ~500 ms chor
   (all LEDs green + both ears forward); persistent LISTENING feedback then pulses all five
   LEDs magenta while the ears counter-rotate through the VAPI range. A PROCESSING pulse is
   likewise chor-only and optional; looping states are resubmitted until their stop event.
   **Motor-direction semantics (source-verified against `choregraphy.cpp`/`.h`, July 2026):**
   the chor field order is `time,motor,<ear>,<angle°>,0,<dir>`; `ear` 0=left/1=right,
   `dir` 0=forward/1=backward (`Direction` enum), `angle` encoded `/18` → 0..16 steps.
   The LISTENING counter-rotation sends BOTH ears to the same target (288° = 16 steps, the
   exact VAPI maximum — never out of range) with OPPOSITE `dir` flags, so one turns forward
   and the other backward. The 0..16 position range is hardware-confirmed (S2 posleft/posright);
   the one thing only the real rabbit finalizes is whether "backward to 288°" spins the long
   way as intended (stock ears are continuous-rotation, so expected) vs the shortest path.
   At end-of-speech the stop chor also returns both ears to the neutral listen pose
   (`build_leds_off_chor(ears_pose=...)`).
8. **OPEN — chor-interrupts-chor semantics.** Does submitting a new choreography while one
   is playing REPLACE it immediately, or queue behind it? The looping LISTENING/PROCESSING
   indicators and their all-off terminator assume prompt replacement so the stop is
   instant; if OJN queues instead, the stop lands one cycle late (cosmetic). The pipeline's
   WakeTimings log records `wake_to_scanner_stop_enqueued_ms` (when the LEDs-off chor was
   SUBMITTED, ≈ end-of-speech) — but wire execution needs a BodyController completion/ack to
   measure; submit() only enqueues (hardware finding: the old `scanner_stop` metric was set
   after awaiting STT, so it always equalled `stt_final`; now it is stamped at the enqueue
   inside `_listening_feedback`, distinguishing enqueue from wire).
9. **Deepgram bilingual it/en via `language: multi` + `endpointing: 100` (July 2026).** The
   desired behavior is automatic it/en code-switching (an "English" transcript was in fact
   an English phrase, not a misdetection). Keep `language: multi`; add `endpointing: 100`
   (Deepgram's recommendation for nova-3 multilingual code-switching). Optional fixed `it`/
   `en` profiles per session if one language must be forced. `model`/`language`/`endpointing`
   stay config, never hardcoded.
10. **Wake beep plays on the RABBIT via the audio lane (`MU/PL/MW`), off by default.** The
    beep is a `PlayAudioCommand` (MP3-by-URL) at USER_SPEECH_SYNC; the half-duplex gate in
    the record loop drops mic frames while it plays (no AEC), tying the guard to the real
    playback timer. Before enabling: confirm Silero does not classify it as speech / it never
    leaks into a transcript. Hardware finding (July 2026): the software+network path is
    confirmed (rabbit issues `GET /wake.mp3` → 200 on every wake), but a ~120 ms / ~1 KB MP3
    is inaudible — likely too short for the MTL decoder. Fallback: a slightly longer, valid
    MP3 (~200–300 ms). Static beep assets served from the Mp3Server dir must be `protected`
    from the retention purge (it deletes `*.mp3`/`*.wav` older than retention).
11. **No independent resident-sound command exists (source review of OJN + MTL bootcode,
    commit 640257f3, July 2026).** Goal was a short firmware wake tone (no HTTP, no motion,
    no long jingle). Findings:
    - **The long jingle IS firmware-resident and needs no HTTP.** `info.mtl newInfoUpdate`
      parses the AmbientPacket services; when the ear keys (4=MoveLeftEar, 5=MoveRightEar)
      change it calls `controlsound midi_communion` (a ~155-byte resident MIDI) **and**
      `earsGoToRefPos`. So `posleft/posright` (AmbientPacket, `ambientpacket.cpp
      SetEarsPosition`) always triggers the carillon + an ear reset — confirming probe #7
      from source (a tcpdump would show the jingle with zero GET). Never use it for reflex.
    - **Choreography carries no audio.** `choregraphy.cpp` emits only opcode `0x07` (LED) and
      `0x08` (motor); the rabbit plays a `CH <file>` with just those. This is why chor-only
      feedback is silent — the desired property, kept.
    - **Resident short sounds exist but are not remotely triggerable.** `const_data.mtl` holds
      `midi_ack`, `midi_acquired`, `midi_ministop`, `midi_abort`, `midi_start/endInteractive`,
      `midi_ears`, single notes `midi_1noteA4…` — all played by `controlsound`, which the
      bootcode only calls internally (button/interactive/ambient). OJN's packet builders
      (`bunny.cpp`) emit only `MU/PL/MW` (MP3 audio), `CH` (chor) and the AmbientPacket; there
      is **no ping command to play a resident MIDI on its own**.
    - **Decision:** a resident wake tone would need either bootcode modification (add a
      play-sound ping command — invasive, out of scope) or the ambient-ear path (long jingle +
      ear reset — unacceptable). So the external MP3 on the audio lane stays the only
      controllable wake sound; probe #10's longer-MP3 fallback is the path if a beep is wanted.
12. **The MTL decoder does not play audio served by the aiohttp Mp3Server — serve MP3s via
    Apache (hardware finding, July 2026).** The SAME MP3 played fine through Apache/OJN on
    :80, but served by aiohttp on :8090 the rabbit issues `GET … HTTP/1.0`, receives 200 OK,
    and stays silent — MP3/decoder/TTS/speaker are all fine; the difference is in the HTTP
    response the old HTTP/1.0 MTL client gets from aiohttp. Resolution: rabbit-facing audio
    is delivered by Apache via a dedicated alias (`ojn/apache/brain-audio.conf.example`,
    `Alias /brain-audio/ → www/audio/`); the brain's Mp3Server runs storage-only
    (`NABAZTAG_MP3_SERVE_HTTP=0`, `NABAZTAG_MP3_BASE_URL=http://192.168.66.1/brain-audio`).
    Addendum (hardware round, July 2026): the alias alone 403s — `chmod` on the repo/audio dir
    is not enough because www-data also needs +x traversal on every parent directory up to the
    home dir. Fixed with a targeted ACL (`setfacl -m u:www-data:--x` on the home + repo dirs,
    `setfacl -R -m u:www-data:rX` + a default ACL on `www/audio`); commands are in the conf
    example. Confirmed end-to-end: Deepgram Aura → Apache → Nabaztag speaker, hardware.
13. **OPEN — XMPP connection can wedge with a persistent Send-Q.** After a test session the
    rabbit's XMPP socket sat ESTAB with Send-Q≈846 stuck bytes: OJN kept answering CHORSENT
    but the rabbit no longer fetched `.chor` files. Restarting the OJN container did NOT make
    the rabbit reconnect; only a physical power-cycle of the Nabaztag recovered it (LEDs red
    until reboot). Ideas: a health check watching `ss` for a non-draining Send-Q on :5222
    and/or the age of the last `.chor`/audio GET in the Apache log, alerting (or restarting
    OJN + prompting a power cycle); the bootcode's own reconnect behavior is out of our reach.
14. **Capture draining held up through on_transcript but the real XVF3800 still logged
    "capture queue full, dropping blocks" during/after playback (hardware round, July 2026,
    runtime a4fd7f7).** Static review of the drain chain (LISTENING → PROCESSING → PLAYING →
    REARMED, `VoicePipeline._handle_wake`) found no gap where the mic iterator goes
    unconsumed — `_drain_frames`/the run() loop both call `anext()` every step — so the
    residual stall (block=32ms, old queue=64 blocks/~2s buffer; hundreds of drops implies
    several seconds of non-consumption) was not reproduced from the code alone. Response:
    (a) the PROCESSING/PLAYING drain was consolidated into one continuous, instrumented chain
    with an explicit bounded flush before rearm and per-state frame counters + transition logs
    (`pipeline state -> LISTENING/PROCESSING/PLAYING/REARMED`), so the next hardware run pins
    down exactly where any remaining stall sits; (b) `AlsaCapture`'s buffer was bumped
    64→300 blocks (~2s→~9.6s, ~1.6 MB) as cheap insurance against event-loop scheduling
    jitter, independent of the root cause; (c) `AlsaCapture.frames()` now raises if called
    twice (single-consumer enforcement, was already true structurally but is now enforced).
    Needs a hardware re-test with the new logs to confirm resolved or to localize further.
15. **Root cause of #14 found and fixed, via the new instrumentation (hardware round, July
    2026, runtime 336cffb).** The new logs pinned it exactly: `PROCESSING (discarded=222)` then
    straight to `PLAYING (discarded=0)` / `REARMED (discarded=0, flushed=3)` — the PLAYING
    drain ran zero iterations. Root cause: `BodyController._audio_loop` pops the entry off
    `_audio_pending` BEFORE `await adapter.play_audio(...)`, and only assigns
    `_current_playback` AFTER that round-trip returns. During the round-trip itself
    `_audio_pending` is already empty and `_current_playback` is still `None`, so `audio_busy`
    read `False` and the half-duplex gate opened mid-turn. Fixed with an explicit
    `_audio_inflight` flag, set the moment an entry is popped and cleared only once a playback
    handle is assigned or the attempt fails; `audio_busy` now also checks it. Covered by
    `test_audio_busy_true_during_play_audio_round_trip` (unit) and
    `test_playing_drain_spans_slow_ojn_round_trip` (integration, `MockOjnServer(latency_s=...)`
    forcing the round-trip open).
    Also this round: the runtime didn't print "nabaztag runtime stopped" after several Ctrl-C
    (piped through `tee`) — teardown steps now run under `asyncio.wait_for` (5s each, logged
    and skipped on timeout rather than blocking the rest) and a second interrupt during
    shutdown forces an immediate `os._exit(1)` rather than waiting indefinitely.
    Latency at this hardware round: wake→STT final 3.87s, STT final→LLM final 4.01s (OpenAI
    first-token ~3.3s of that), Deepgram TTS synth 2.90s, wake→audio-queued ~10.8s total.
    Streaming the first LLM sentence would only save ~0.7s here (first-token→final is short);
    the real cost centers are OpenAI's first-token latency and Deepgram synthesis time — a
    faster/smaller OpenAI model is worth trying, `DEEPGRAM_TTS_GAIN_DB` volume is still low at
    +3dB (try +6dB; the post-processing now chains a peak limiter so a higher gain can't clip).
16. **Dedicated latency round opened (hardware round, July 2026, runtime 74aaa80): correctness
    is done (#15), the target is EOS → first audio < 4s on simple conversational turns; last
    measured wake→audio-queued ~14.3s.** Changes so far, targeted at the two named costs
    (OpenAI first-token, a second OpenAI round-trip for tool calls the model didn't need a
    reply for):
    - `ToolSpec.informational` (get_direction/body_state=True; the three body-gesture tools
      default False). `AgentLoop._run_rounds` now skips the follow-up LLM call when a round
      already has final text AND every tool call in it is non-informational — the tool still
      executes, the round-1 text is used directly. The system prompt now tells the model to
      give the gesture + the reply in the SAME response instead of waiting on the gesture's
      (empty) result.
    - `llm.reasoning_effort` was already config-driven (not new); documented `none` as a valid
      value and the latency rationale in config.example.yaml. `max_output_tokens` 220→150 and
      the prompt now asks for ONE short sentence, not "1-2".
    - `DeepgramTTS.synth` now logs a request→headers→first-byte→last-byte→gain breakdown
      (chars and duration only, never text content) to localize whether TTS time is network,
      download, or the ffmpeg gain/limiter pass.
    - New `brain/scripts/llm-bench.py`: A/B harness (no real rabbit, a mock OJN server so tool
      calls still execute) comparing model/reasoning_effort combos on fixed prompts — reports
      timings and raw replies for a human to judge quality/language/tool-correctness; never
      auto-picks a model. Needs `OPENAI_API_KEY` and real hardware-adjacent judgement, so the
      actual gpt-5.4-mini vs gpt-5.4-nano vs effort comparison is a to-do for the next round,
      not run from here.
    Streaming the first LLM sentence into TTS before the full response completes was
    considered again and still deferred (tool-call-vs-partial-speech risk, needs its own pass).
17. **First real llm-bench.py run (hardware round, July 2026, runtime 60106fb) ruled out
    gpt-5.4-nano and exposed the round-skip's real failure mode.** Results: gpt-5.4-mini/none
    median first-token 1952ms, final 2248ms; mini/low 1904ms/3129ms; nano/none 3084ms/3483ms
    (often 3 calls / 2 tool rounds); nano/low 4955ms/5499ms. nano is marketed as the fast model
    for simple tasks but was slower AND less tool-efficient on this agent/tool loop — mini
    stays the model, `reasoning_effort: none` is now the config default (still provisional,
    needs a clean none-vs-low re-run after the fix below).
    `single-round-with-tools=0/3` in every combo: the round-skip from #16 depended on the model
    producing free text alongside a tool call in the SAME response, and it didn't — round 1 was
    reliably a bare tool call with empty text, forcing round 2 every time. Fixed by NOT relying
    on that: a new `express(spoken_text, ears?, gesture?, mood?)` tool carries the reply INSIDE
    the tool call's own arguments. `AgentLoop._run_rounds` reads `spoken_text` back out of the
    raw `ToolCall.arguments` (works even if the tool's own execution fails validation — a bad
    gesture must not silence the reply) and skips the follow-up round exactly as before.
    `get_direction`/`body_state` are unaffected (still `informational=True`, still force a
    round). Also: `make_llm_provider`'s fallback defaults were stale (300 tokens, "low") despite
    config.example.yaml already saying 150/none — a gitignored config.yaml missing the new keys
    would have silently kept the old behavior; fixed, and config-doctor now migrates
    `reasoning_effort: low`→`none` and `max_output_tokens: 300|220`→`150` (present-and-stale
    only, same pattern as the provider/model migration). llm-bench.py now logs per-round tool
    names + text length and flags two hard invariants (expected call count per prompt, spoken
    text never empty) instead of just latency numbers.
    Next: re-run the none-vs-low A/B now that `express` removes the confound, on hardware.
18. **Decisive LLM A/B (July 2026, runtime 5d15ce2, 3 runs/scenario): gpt-5.4-mini +
    `reasoning_effort: low`, 0 correctness violations.** mini/low median final_text 1615ms
    (min 1203, max 3324) vs mini/none 2162ms (min 1045, max 2756) — low is ~547ms/25% faster on
    the median. **final_text is THE metric for this voice loop**: Deepgram TTS cannot start
    until the reply text is complete, so "none"'s faster first token buys nothing. This
    REVERSES the provisional "none" from #17 (which came from the pre-`express`, confounded
    run); config.example.yaml, `make_llm_provider`, `OpenAIProvider.DEFAULT_MAX_OUTPUT_TOKENS`
    (was still 300 — llm-bench.py builds a provider directly, so it had been benchmarking a
    token budget the runtime never used) and config-doctor were all realigned to low/150,
    with the migration now running none→low.
    `express` confirmed working on all three scenarios: plain greeting 1 call, greeting+ears
    1 call, direction question 2 calls, spoken text never empty. `single-round-with-tools=5/9`
    is correct, not a regression — a plain greeting can legitimately come back as text with no
    tool call at all (still one call); the metric only counts runs that used tools.
    Metric gap found and fixed: `to_first_token_ms` was None for every `express`-answered turn
    (the reply is in the function call's arguments, so no `output_text.delta` ever fires). New
    `to_first_output_ms` covers the first delta of ANY kind, text or tool arguments.
    Personality: unprompted pet names ("tesoro", "piccolino", "piccola voce") read as affected
    and repetitive — the system prompt now forbids terms of endearment outright.
    Next: single-turn hardware run to measure the new OpenAI time and, above all, the residual
    ~3-5s of Deepgram TTS (the per-phase timing log from #16 is already in place for that).

19. **Latency gates L1/L2 shipped, awaiting hardware (July 2026, runtime 05b1e1a).** Target:
    end of speech → first audio < 4s; last measured wake→audio-queued ~14.3s.
    - **L1, Deepgram Flux (`stt_profile: flux`)**: recognition AND end-of-turn detection in one
      pass, replacing the ~1875 ms fixed cost of the old path (1600 ms Silero silence + ~275 ms
      nova-3 finalisation). This inverts who owns the turn: with nova-3 the pipeline's local VAD
      CLOSED the chunk stream; with Flux the stream stays open and the provider calls back on
      EndOfTurn while frames keep flowing. Providers advertise which model they use via
      `detects_end_of_turn`, and `VoicePipeline` picks the matching loop
      (`_record_provider_endpointed` vs `_record_vad_endpointed`). nova-3 + Silero is retained
      as the `cloud` profile, not deleted. Only `EndOfTurn` is acted on; `EagerEndOfTurn` is
      timestamped for diagnostics but never dispatched on (no speculative LLM this round).
      Half-duplex and the drain chain are untouched. A client-side `turn_timeout_s` abandons the
      turn and re-arms if EndOfTurn never arrives.
      **HARDWARE-CONFIRMED (July 2026, runtime 2487e04), three turns**:
      speech_end→EndOfTurn 226/214/222 ms (vs the ~1875 ms it replaces); end_of_turn_confidence
      0.89/0.73/0.78; audio cursor populated (3.04/5.52/4.96 s); language detected correctly
      it/en/en; no timeout, no error. flux-general-multi DOES report language, so the L1
      regression risk (English spoken in the Italian voice) did not materialise. The wire schema
      matched the parser.
      **Correction to the earlier "unknown schema → Whisper fallback" claim, which was NOT
      actually guaranteed by the code** (spotted post-hardware): an all-unknown-events stream was
      only ignored until the pipeline's turn-timeout cancelled the task — which abandons the turn
      with NO fallback. Fixed: `FluxSTT` now raises `FluxSchemaError` when it recognises zero
      TurnInfo (both on socket close and EARLY, after `SCHEMA_MISMATCH_THRESHOLD` unrecognised
      messages, so it beats the timeout), and `FallbackSTT` catches it and replays the buffered
      audio to Whisper, firing end-of-turn so the pipeline closes cleanly instead of stalling.
    - **L2, smaller LLM input**: `express` subsumes gesture_ears/set_mood_lights/play_gesture,
      so those three are no longer sent to the LLM (`VOICE_AGENT_TOOLS`) — the voice agent sees
      only express/get_direction/body_state. They remain fully executable for MCP and for
      replaying older histories. New per-turn diagnostics: `tool_schema_count`,
      `tool_schema_chars`, and the provider's own `input_tokens` / `cached_input_tokens` /
      `output_tokens` (summed across rounds, counts only — never content).
    - **PROCESSING indicator** now spans the real dead-air gap: from end-of-turn until the
      rabbit ACTUALLY starts speaking (`current_playback` set after the OJN round-trip), not
      merely until the reply was queued. Bounded, and returns immediately when a turn queues no
      audio. Still opt-in (`leds.processing_indicator`), worth enabling during latency tests.
    - config-doctor gained an `stt_profile: cloud → flux` nudge. Doing so surfaced a latent bug:
      `_rewrite_in_section` only ever handled NESTED keys, so a top-level scalar would have been
      reported and then silently not rewritten by `--fix`; `_rewrite_top_level` fixes that.
    - Not done, deliberately: **Gate L3** (progressive/streaming MP3) is a hardware PROBE first —
      does the MTL decoder start playing before the HTTP response completes? No new TTS
      architecture until that question is answered on the rabbit.
20. **Gate L3 is now the main residual cost, and the probe to answer it is ready (July 2026,
    runtime 2487e04).** Hardware timing pinned the bottleneck: Deepgram TTS total_http_ms
    3923/4176/2780, of which request→headers 640-699 ms, headers→first-byte ≈ 0, and
    first-byte→last-byte 2119-3476 ms. So the FIRST MP3 chunk is already in hand at ~0.7 s, but
    `DeepgramTTS.synth()` accumulates every chunk, writes the whole file, applies the ffmpeg
    gain, and only THEN queues a static URL — the multi-second first→last-byte window is dead
    air the rabbit could in principle already be playing. Whether it CAN depends on one
    unknown: does the MTL decoder play as bytes arrive, or buffer to EOF?
    `brain/scripts/mp3-progressive-probe.py` (+ `ojn/apache/mp3-probe.conf.example`) answers it
    without touching the production path: a localhost server dribbles a known MP3 out in small
    `audio/mpeg` chunks over a controlled spread, logging connect/first-byte/last-byte, reached
    through Apache (mandatory front, #12) via a reverse proxy and queued on the rabbit with
    api_stream.jsp. Maurizio listens: sound at the first chunk → streaming works, build a
    progressive path; sound only near last-byte → MTL buffers, keep the static file; no sound →
    framing issue (the probe's own first/last-byte timestamps separate an Apache buffering
    problem from an MTL one). **Do NOT change the production TTS path until the probe result is
    in.** Target architecture IF it passes: random job URL → Apache reverse proxy → Deepgram
    REST/WS stream → optional streaming ffmpeg gain+limiter → MTL, with the static path kept as
    fallback.
21. **Gate L3 HARDWARE-REJECTED (July 2026): the MTL decoder buffers to EOF — no progressive
    TTS.** Probe results:
    - Baseline `/fast.mp3` (31,341 bytes in one shot): GET 200, audio plays correctly.
    - `/slow.mp3`, same file dribbled over 7.776 s: probe upstream first byte +0 ms, last byte
      +3762 ms; audio heard at ~5305 ms — i.e. AFTER the transfer completed, not during.
    - tcpdump confirmed Apache is NOT the culprit: Apache→rabbit was genuinely progressive
      (data packets ~250 ms apart), and the final data packet + FIN (18:37:10.731) lined up with
      the probe's own last byte. So Apache does not buffer the proxied body; the decoder does.
    - `--spread 20 s`: no audio at all (likely a decoder timeout / insufficient throughput).
    **Verdict: MTL substantially requires the complete file / EOF before it plays.** Do NOT build
    a progressive TTS path; keep the complete MP3 served statically by Apache (the current
    production path — unchanged, this gate never touched it). `brain/scripts/
    mp3-progressive-probe.py` and `ojn/apache/mp3-probe.conf.example` are retained as
    reproducibility tooling only, banner-marked hardware-rejected so they're never mistaken for a
    live path (re-run only if firmware/decoder assumptions ever change).
    **Latency work continues on the two remaining levers — both about producing the reply text
    and its audio faster, since delivery can't be overlapped:** (a) LLM + TOTAL TTS synthesis
    time — benchmark TTS providers/voices, cloud vs local (Piper has no network round-trip), via
    `brain/scripts/tts-bench.py`; (b) shorter spoken replies; (c) anticipatory synthesis of the
    first sentence ONLY where semantically safe (no later tool call could contradict it) — a
    design-heavy change, not started, needs its own pass.
22. **TTS provider decision + Piper redesign (July 2026, hardware A/B).** Deepgram Aura STAYS the
    production TTS. ElevenLabs A/B: synthetic bench 970ms but ~3700ms median IN the real
    conversation (short fixed phrases mispredicted it — synth time scales with chars, so
    tts-bench now uses full reply-length sentences and reports chars), Italian markedly worse and
    very quiet, English better, no perceptible whole-conversation latency win → not promoted.
    tts-bench gained --keep-audio/--output-dir (labelled MP3s per synth, same texts across
    profiles for A/B listening) and a per-profile×LANGUAGE breakdown (it/en compared separately);
    ElevenLabsTTS logs chars/time/duration like DeepgramTTS.
    **Before Piper can even be benchmarked, it was redesigned** (the old provider was unfit):
    - It used ONE `PIPER_MODEL` and dropped `language` → regressed the bilingual it/en
      requirement. Now `PIPER_URL_IT`/`PIPER_URL_EN`, routed by the Flux-detected language; both
      required (a missing language raises rather than silently reusing the other voice).
    - It spawned the CLI per synth, reloading the model — Piper's own docs call that slow and
      steer to the HTTP server for repeated use, so benchmarking the CLI would measure a
      deliberately inefficient path. Now an HTTP CLIENT of a PERSISTENT local Piper server (one
      warm server per language); WAV→MP3 via ffmpeg (rabbit streams MP3). Optional Deepgram
      fallback on timeout/error.
    - **Licence/provenance**: the Piper engine (OHF-Voice/piper1-gpl, release 1.4.2) is GPL-3.0.
      It runs ONLY as an external localhost process — NO Piper code is imported, vendored, or
      copied into this Apache-2.0 tree; the brain talks to it over HTTP exactly like Deepgram/
      OpenAI (clean process boundary, not a derivative work). Candidate IT voice
      it_IT-paola-medium (22.05 kHz, medium; CC0 training dataset); a licence-compatible EN voice
      must still be chosen and documented before promotion.
    - Piper HTTP request shape CONFIRMED against piper1-gpl 1.4.2 (review): POST JSON
      {"text": text} → WAV body. `PiperTTS._request_wav` matches; the "verify" hedge is lifted.
    - EN voice chosen: en_US-sam-medium (Apache-2.0 dataset, recommended); alternative
      en_GB-alba-medium (CC BY 4.0 — requires attribution). IT stays it_IT-paola-medium (CC0).
    - **Benchmark-contamination blocker fixed (review): on the Bolt DEEPGRAM_API_KEY is present,
      so a failing Piper would return a Deepgram clip recorded under the "piper" label — could
      promote Deepgram by accident.** Two guards: (a) `PIPER_FALLBACK_DEEPGRAM` env flag (default
      on) that the factory honours; tts-bench forces it to `0`, so a Piper failure is recorded AS
      a failure. (b) `TTSResult.provider` now carries the real backend; the fallback keeps ITS
      OWN tag ("deepgram"), and tts-bench discards any row whose provider ≠ the profile benched.
    - **Runtime fallback now inherits `DEEPGRAM_TTS_GAIN_DB`** (review): the factory builds the
      Piper→Deepgram fallback through the same `_make_deepgram`, so a fallback utterance is not
      suddenly quieter than the boosted production voice.
    - **Pinned Bolt install script shipped**: `ojn/piper/install-piper.sh` {install|units|smoke}
      — piper-tts[http]==1.4.2 in a SEPARATE venv (GPL boundary), the two voices, systemd unit
      files (written, NOT auto-enabled — same caution as the runtime unit), and a health-check +
      POST→WAV smoke. Does not touch TTS_PROFILE.
    Piper is a CANDIDATE, not production: promote only if it wins BOTH latency AND on-Nabaztag
    listening (per-language) — never on the synthetic RTF alone. Next: run the server smoke, then
    `tts-bench --profiles deepgram,piper --keep-audio` (fallback auto-disabled) and listen.

Record answers here, then stamp the matrix rows hardware-confirmed.

### Build & deployment findings (Gate S1)

- **OJN master is Qt4-era code and does not build against Qt5+** (Ubuntu 24.04): removing
  `-Werror` is not enough — `QHttp` (removed in Qt5), `QString::toAscii()` and other API/ABI
  breaks remain. Porting is out of scope.
- **Deployment shape:** the daemon is built and run in a locally-built **Debian buster
  container** (last Debian shipping Qt4), pinned to OJN commit `640257f3` — `ojn/docker/`
  (Dockerfile + entrypoint + tuned `openjabnab.ini`). No third-party OJN images from Docker Hub.
  Container runs with **host networking** (HTTP API binds 127.0.0.1:8080; XMPP binds :5222 for
  the rabbit); state lives in `/var/lib/openjabnab` (bind-mounted at `/data`; the daemon keeps
  ini/bunnies/ztamps/accounts next to its binary, the entrypoint symlinks them into `/data`).
- The **PHP http-wrapper stays on host Apache** (vhost in `ojn/apache/`, DocumentRoot =
  `<OJN_DIR>/http-wrapper`, `AllowOverride All` + `mod_rewrite`); `openjabnab.php` reaches the
  daemon at 127.0.0.1:8080, and the daemon's `RealHttpRoot` points at the same `http-wrapper/
  ojn_local/` via bind mount so chor/broadcast files land where Apache serves them.
- OJN's own TTS backends (acapela/google, 2010-era endpoints) are presumed dead — the `tts/say`
  smoke test may fail for that reason alone; use `api_stream.jsp` with a local MP3 URL as the
  S1/S2 audio check instead.

### S1/S2 field findings (PASSED, July 2026)

Container daemon + Apache wrapper + bootcode + API + XMPP all working on the Bolt; rabbit
registered to a persistent account; XMPP session ESTAB between 192.168.66.1:5222 and the
rabbit; boot completes (ears initialize) and a VAPI `posleft/posright` command moves the real
ears. Lessons baked into `deploy.sh ojn`:

- **XMPP server must be a hostname.** The MTL bootcode does not special-case an IPv4 literal —
  it hands the string to its DNS resolver. Fix: dnsmasq serves `address=/ojn.local/192.168.66.1`
  and `openjabnab.ini` announces `PingServer/BroadServer/XmppServer = ojn.local` (deploy.sh
  migrates older INIs automatically).
- **Port 8080 is not HTTP** (see entry-points table above) — smoke tests go through Apache :80.
- **PHP 8.3 fixes** (patched idempotently after every pinned checkout): `php-xml` package;
  `session_start('openJabNab')` → `session_name(...)`; `session_start()`; `split()` → `explode()`
  in the cinema admin plugin.
- **`ojn_admin/include/common.php` is generated by deploy.sh** from `common-def.php`
  (`OJN_ADMIN_HOST`/`OJN_ADMIN_EMAIL` overridable) — the checkout is never made
  Apache-writable, and `install.php` is never needed.
- **First-account bootstrap:** the built-in `admin/admin` lives only in memory (never saved,
  `accountmanager.cpp:111`). `./ojn/deploy.sh account <login>` (password prompted with a
  hidden read and sent via `curl --data-urlencode pass@-` — never in argv, `ps`, shell
  history, or unencoded in the URL) uses it once: OJN
  auto-promotes the first registered account to admin (`accountmanager.cpp:253`) and persists
  it; the daemon is then restarted so the default admin evaporates.
  `AllowAnonymousRegistration` stays `false`.
- **Security: upstream NetworkDump leaks credentials.** `NetworkDump::Log("Api Call",
  GetRawURI())` (`httphandler.cpp:40`) appends every raw API URI — including `pass=`, `token=`
  and `tk=` — to `dump.log` in cleartext, with no off switch upstream. Our image replaces
  `netdump.cpp` (`ojn/docker/patches/`): the dump is **off by default** (opt-in with
  `[Log] NetworkDump = true`) and pass/token/tk are **redacted** even when enabled. `dump.log`
  lives in the container's ephemeral layer (not `/data`), so it dies with the container.
  Credentials that hit the pre-patch log were rotated (July 2026).
- **Security: Apache access log also carried credentials.** The vhost initially used the stock
  `combined` format, which logs `%r` — the full request line including OJN's `pass`/`token`/`tk`
  query parameters. The vhost now defines a dedicated `ojn_noquery` LogFormat using
  `%m %U %H` (`%U` excludes the query string) for `ojn-access.log`. After deploying the updated
  vhost: rotate the account password again and truncate the old access log
  (`sudo truncate -s0 /var/log/apache2/ojn-access.log`, or logrotate + delete).
- **Stray `bunnies/.dat` file explained:** `BunnyManager::GetBunny` (`bunnymanager.cpp:81`)
  auto-creates a Bunny for *any* unknown serial — including the empty one — before any token
  check, and saving writes `<serial>.dat` (empty serial → `.dat`). Any VAPI probe without a
  valid `sn` triggers it. Cleanup is safe with the daemon stopped:
  `sudo systemctl stop nabaztag-ojn && sudo rm '/var/lib/openjabnab/bunnies/.dat' && sudo systemctl start nabaztag-ojn`.
  Don't smoke-test VAPI endpoints with bogus serials; the segment is isolated, so outside
  scanners can't reach it.

### Hardware findings so far

- **Gate S0: PASSED (July 2026).** Intel AC 3168 radio (`wlp3s0`) runs the WPA1/TKIP AP fine
  (channel 11); the rabbit associates and holds its static lease (192.168.66.10, MAC in `.env`).
- **The V2 firmware answers neither ICMP ping nor arping.** Liveness must be judged from the
  DHCP lease + the rabbit's own traffic; `deploy.sh verify` was updated accordingly.
- **First rabbit request observed: `GET /vl/bc.jsp?v=0.0.0.10&m=<mac>...`** — the Violet
  bootcode fetch, plain HTTP port 80 (firmware reports `v=0.0.0.10`). OJN's `http-wrapper`
  handles exactly this: its `.htaccess` rewrites `^vl/bc.jsp$` to the static
  `ojn_local/bootcode/bootcode.default`, and proxies every other rabbit path to the daemon on
  127.0.0.1:8080 via `openjabnab.php`. **S1/S2 therefore needs Apache with `mod_rewrite` +
  `AllowOverride` on a vhost rooted at `http-wrapper/`, port 80** — the rabbit is already
  knocking on the right door.

## 4. Consequences for `OjnAdapter`

- Primary surface = **VAPI** (`api.jsp` / `api_stream.jsp`) + a handful of `/ojn_api/bunny/...`
  calls (VAPI enablement, callurl RFID mapping, plugin registration). `packet/sendMessage` kept
  as an escape hatch behind a config flag.
- `BodyCapabilities`: `can_cancel_audio=False`, `has_playback_events=False`,
  `can_read_body_state=False`, `has_per_led_rgb=True`, `ear_range=(0,16)`.
- `PlaybackHandle.wait_finished` = timer from summed MP3 durations + guard (spec §6.6 predicted
  exactly this).
- Audio queue: pass sentence MP3 URLs as one `urlList` call when they're ready together;
  otherwise sequential calls with duration-timer pacing.
