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
    (`NABAZTAG_MP3_SERVE_HTTP=0`, `NABAZTAG_MP3_BASE_URL=http://192.168.66.1/brain-audio`),
    keeping the audio dir, retention purge, protected assets and URL building.
13. **OPEN — XMPP connection can wedge with a persistent Send-Q.** After a test session the
    rabbit's XMPP socket sat ESTAB with Send-Q≈846 stuck bytes: OJN kept answering CHORSENT
    but the rabbit no longer fetched `.chor` files. Restarting the OJN container did NOT make
    the rabbit reconnect; only a physical power-cycle of the Nabaztag recovered it (LEDs red
    until reboot). Ideas: a health check watching `ss` for a non-draining Send-Q on :5222
    and/or the age of the last `.chor`/audio GET in the Apache log, alerting (or restarting
    OJN + prompting a power cycle); the bootcode's own reconnect behavior is out of our reach.

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
