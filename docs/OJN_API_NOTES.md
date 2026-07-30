# OpenJabNab API Notes — capability reference & operating rules

What a stock Nabaztag:tag can and cannot be made to do through a self-hosted OpenJabNab, and
the rules that follow. Source-verified against the OpenJabNab repository
(github.com/OpenJabNab/OpenJabNab, `master`, July 2026) and confirmed on a real rabbit where
marked. File references are to the OJN repo.

> **Scope.** This is the *reference*: the API surface, the capability matrix, and the numbered
> rules the codebase cites. The detailed findings log behind them — the measurements, the
> hypotheses that turned out wrong, and how each rule was arrived at — is being written up
> separately and will be published progressively. Numbering here is stable and matches the
> citations in the source (`OJN_API_NOTES #8`, `§2`, …).

## 1. Entry points & auth

| Surface | Where | Notes |
| :--- | :--- | :--- |
| Daemon "HTTP" port | `localhost:8080` (config `OpenJabNabServers/ListeningHttpPort`) | ⚠️ **Not plain HTTP**: it speaks OJN's internal framing (`pack("LCa*")` length+type+payload — see `openjabnab.php`). Never `curl` 8080 directly; every HTTP test goes through Apache on :80 |
| Rabbit XMPP | `:5222` on all interfaces (`ListeningXmppPort`) | V2 rabbits speak XMPP to the server; commands are pushed, not polled — good for latency |
| Admin/plugin API | `GET /ojn_api/<call>` | Router: `server/lib/apimanager.cpp:32` (`httphandler.cpp:38` strips the prefix) |
| Violet-compatible API (VAPI) | `GET /ojn/FR/api.jsp` and `/ojn/FR/api_stream.jsp` | `bunny.cpp:55 ProcessVioletApiCall`. Auth: `sn=<serial>&token=<vapi token>` |

- **Account token** (for `/ojn_api/...`): `GET /ojn_api/accounts/auth?login=..&pass=..` → token
  (`accountmanager.cpp:206`). Most other calls take `&token=...`; without it you are the Guest
  account. Several calls require admin.
- **VAPI token** (per bunny): enable + read via
  `/ojn_api/bunny/<bunnyid>/enableVAPI`, `getVAPIToken`, `setVAPIToken?tk=..`,
  `setPublicVAPI?public=..` (`bunny.cpp:692-698`).
- Plugin-per-bunny calls (`/ojn_api/bunny/<id>/<plugin>/<fn>`) require the plugin registered to
  that bunny first: `/ojn_api/bunny/<id>/registerPlugin?name=<plugin>` (`apimanager.cpp:154`
  exempts only System/Required plugins).
- All API answers are XML (`<api>...</api>`; VAPI answers are `<rsp>...</rsp>`).

## 2. Capability matrix (Gate G0)

| Capability | Verdict | How |
| :--- | :--- | :--- |
| TTS (server-generated) | **works-native** (source) | VAPI `api.jsp?...&tts=<text>[&voice=..]` (`bunny.cpp:159`) or `/ojn_api/bunny/<id>/tts/say?text=..` (`plugin_tts.cpp:24`). Sends `MU <file>\nPL 3\nMW\n`. **Not usable in practice** — the backends are 2010-era endpoints and are dead; we synthesize our own audio |
| **MP3 by URL, queued** | **works-native — HARDWARE CONFIRMED** | VAPI `api_stream.jsp?...&urlList=url1\|url2\|url3` → `ST url\nMW\nST url\nMW\n` (`bunny.cpp:67-74`). The `\|`-separated list *is* the queue — no plugin needed. **No cancel and no finished-callback exist anywhere** → `can_cancel_audio=False`, playback duration must be estimated by timer. Inter-file gap ≈ **1.7 s**: add it per boundary when estimating a queue's duration |
| **Arbitrary ear positions 0–16** | **works-native — HARDWARE CONFIRMED** | VAPI `api.jsp?...&posleft=0..16&posright=0..16` → `AmbientPacket::SetEarsPosition` (`bunny.cpp:138-153`), range-checked 0–16. **But see rule #7 — do not use this path for reflex motion** |
| Per-LED RGB | **works-native via chor — HARDWARE CONFIRMED** | No standalone LED call, but VAPI `chor=` compiles a Violet `.chor` binary server-side and pushes `CH <path>` (`bunny.cpp:168-204`). A 1-action chor sets one LED. LEDs: `0=bottom, 1=left, 2=middle, 3=right, 4=top` (`choregraphy.h:13`). True RGB on all five, verified |
| Timed choreography (ears+LEDs) | **works-native — HARDWARE CONFIRMED** | Same `chor=` param. Text format (`choregraphy.cpp:73 Parse`): `tempo,{time,motor,ear,angle,0,dir \| time,led,led#,r,g,b},...` — tempo in ms/tick (10..2550, stored /10), `time` in ticks from sequence start, motor: `ear` 0=left 1=right, `angle` in degrees (encoded /18 → 0..16 steps of 18°), `dir` 0=fwd 1=back. VAPI answers CHORSENT; the rabbit then fetches the generated `.chor` over HTTP and plays it |
| Sleep / wake | **works-native** (source) | VAPI `api.jsp?...&action=13` (wake) / `action=14` (sleep) (`bunny.cpp:120-127`) |
| Raw frames | **works-native** (source) | `/ojn_api/bunny/<id>/packet/sendPacket?data=<hex>` and `packet/sendMessage?msg=<text>` (`plugin_packet.cpp`). Message verbs seen in plugin sources: `MU <path/url>` play MP3, `ST <url>` stream, `PL <n>` playlist/jingle, `MW` wait-end-of-playback, `CH <chor path>` run choreography, `CU <url>` make the rabbit call a URL |
| **RFID / button events egress** | **needs-plugin — built; clicks HARDWARE CONFIRMED** | No webhook and no event-polling endpoint exist upstream: `getlast/getlasts` (`bunny.cpp:961-989`) expose only connection metadata, admin-only, and events are dispatched to C++ plugins (`OnClick`, `OnRFID` — `plugininterface.h:47-49`). **The stock `callurl` fallback does NOT work:** OJN sends the `CU` packet (visible and decoded on the XMPP wire) but the bootcode never performs the request — zero DNS/TCP toward the target, even with an IPv4 literal on port 80. Since RFID/callurl share that final `CU` leg, the fallback is disqualified. Resolution: **`ojn/plugin_events/`** (GPL, ~100 lines) fires a server-side GET to a per-bunny webhook; brain side is `rabbit_brain.body.events_server.EventListener` (default 127.0.0.1:8091) |
| Ears/LED state readback | **not-possible** (source) | The VAPI `ears` param answers a hardcoded `POSITIONEAR 0,0` TODO (`bunny.cpp:164-167`). The caller must track body state itself |

## 3. Gate G0 verdict

**The choreography plugin is NOT needed.** Native VAPI covers arbitrary ears, per-LED RGB,
timed choreographies, queued MP3-by-URL and sleep/wake; `packet/sendMessage` covers anything
exotic left over. `ojn/plugin_choreo/` stays uncreated.

The only genuine gap is **event egress**, filled by `ojn/plugin_events/` (GPL, cleanly
separated per ARCHITECTURE §11.1).

### 3.1 Operating rules

Numbered findings, stated as the rules they produced. Each was established on the real rabbit
unless marked *(source)*.

1. **Audio queueing is one native call.** Pass sentence MP3 URLs as a single `urlList`; add
   ~1.7 s per boundary to any duration estimate.
2. **Playback duration is an estimate, always.** No finished-callback exists; `wait_finished`
   is a timer over summed MP3 durations plus a guard.
3. **Ear range 0–16 is real and enforced** server-side. Positions outside it are rejected, not
   clamped.
4. **`chor=` is the expressive surface.** Per-LED RGB and timed ear+LED sequences both go
   through it; the generated `.chor` is fetched by the rabbit over HTTP, so the shared
   `RealHttpRoot` mount must be reachable.
5. **Event egress needs the plugin.** `callurl` is disqualified (see §2). Clicks are confirmed
   end-to-end; RFID rides the same hook.
6. **VAPI command round-trip latency** feeds `BodyController` deadlines — measure it in your
   own deployment; it is a LAN round-trip plus OJN's own queueing.
7. **Reflex motion is choreography-only — NEVER `posleft`/`posright`.** That path
   (`AmbientPacket`) makes the firmware play a long carillon: `info.mtl newInfoUpdate` sees the
   ear keys change and calls `controlsound midi_communion` **and** `earsGoToRefPos`. So a
   plain ear command costs an unwanted jingle *and* an ear reset. Choreography carries no
   audio at all (`choregraphy.cpp` emits only opcode `0x07` LED / `0x08` motor), which is
   exactly why chor-only feedback is silent. Keep `set_ears` for explicit, user-requested
   poses only. Motor semantics *(source)*: `time,motor,<ear>,<angle°>,0,<dir>`, `ear`
   0=left/1=right, `dir` 0=forward/1=backward, `angle` encoded `/18` → 0..16 steps; 288° is
   the exact maximum. Sending both ears to the same target with OPPOSITE `dir` flags is what
   produces visible counter-rotation.
8. **Choreography QUEUES — it does not replace, and nothing can cancel it.** A new `chor=`
   submitted while one is playing is appended; OJN/VAPI expose no cancel and no replace, and a
   local `submit()` confirms local enqueue ONLY — nothing reports execution on the rabbit.
   Consequences, all load-bearing:
   - **Never drive a long-lived indicator from a timer.** Each cycle lengthens a queue that
     cannot be shortened, and a terminator sent afterwards lands behind all of it.
   - Indicators are **event-driven**: at most one SHORT choreography per state transition.
   - The one repeating indicator (the rabbit speaking) uses a tick **shorter than its own
     period**, so at most one is ever in flight and nothing accumulates.
   - Cosmetic commands carry **deadlines** — one that cannot run promptly is dropped rather
     than fired late into a state it no longer describes.
   - On teardown, drop everything still queued LOCALLY and send the neutral terminator ABOVE
     the cosmetic priority. What already reached the rabbit cannot be recalled; the only lever
     is refusing to make that remote queue longer.
9. **Bilingual STT: keep `language: multi`.** Automatic it/en code-switching is the desired
   behaviour, not a misdetection. Add `endpointing: 100` for nova-3 multilingual. Model,
   language and endpointing stay configuration, never hardcoded.
10. **A wake beep must ride the audio lane** (`MU/PL/MW`) as an MP3-by-URL, and the half-duplex
    gate must drop mic frames while it plays (there is no AEC for the rabbit's speaker). A
    ~120 ms / ~1 KB MP3 is **inaudible** — too short for the decoder; use ~200–300 ms. Static
    assets in the MP3 server directory must be protected from the retention purge.
11. **There is no way to trigger a resident firmware sound on its own** *(source review of OJN
    + the MTL bootcode)*. Short resident MIDIs exist (`midi_ack`, single notes, …) but only
    `controlsound` plays them and the bootcode calls it internally; OJN's packet builders emit
    only `MU/PL/MW`, `CH` and the AmbientPacket. An external MP3 is the only controllable
    sound. (Rule #7 is the same finding seen from the other side: the ambient-ear path is the
    one thing that *does* reach a resident sound, which is precisely why it must be avoided.)
12. **Rabbit-facing MP3s MUST be served by Apache, not by an aiohttp server.** The same file
    plays through Apache on :80 and stays SILENT when served by aiohttp on :8090 — the rabbit
    issues `GET … HTTP/1.0`, receives 200 OK, and nothing sounds. Decoder, MP3, TTS and
    speaker are all fine; the difference is in the response the old HTTP/1.0 client gets. Use
    a dedicated alias (`ojn/apache/brain-audio.conf.example`) and run the brain's MP3 server
    storage-only. The alias alone returns 403: `www-data` also needs `+x` traversal on every
    parent directory up to the home dir — the conf example carries the targeted `setfacl`
    commands that fix it.
13. **A long silence is usually the AP, not the rabbit.** Symptoms that look like a wedged XMPP
    session — ghost `ESTAB` sockets with a stuck Send-Q, a stale neighbour entry — are
    *symptoms*. The cause observed here is the rabbit being disassociated for inactivity while
    `hostapd` still reports `active` and `iw` still reports `type AP`, with the SSID no longer
    visible and interface counters frozen. **Restarting hostapd alone** restores it: the rabbit
    re-associates in ~2 s, refetches its bootcode and opens a fresh XMPP. **Never auto-restart
    OJN and never reboot the host** — neither addresses this. Any automated recovery must
    require several independent signals to coincide, and must have a cooldown, a per-outage cap
    and a retry-hold so it cannot loop when the rabbit is simply switched off. Judge "last seen"
    from the newest Apache log line whose client is the rabbit's IP — **file mtime is not a
    valid proxy**, since any other request refreshes it.
14. **One consumer of the microphone, always**, and enforce it rather than assume it. A capture
    iterator that goes unconsumed for a few seconds fills its queue and starts dropping blocks.
15. **The half-duplex gate must cover the command round-trip, not just active playback.** A
    gate keyed only on "audio playing" opens during the OJN round-trip — after the queue entry
    is taken and before a playback handle exists — and the rabbit hears itself.
16. **Skip the second LLM round when nothing needs it.** Mark informational tools separately;
    a round that already has final text and only non-informational tool calls needs no
    follow-up request.
17. **Do not expect free text alongside a tool call.** The model reliably returns a bare tool
    call with empty text, so a design that needs both in one response will silently cost an
    extra round-trip every turn. Carry the reply INSIDE the tool call's arguments instead, and
    read it back from the raw arguments so a validation failure in the tool cannot silence the
    reply.
18. **For a voice loop, optimise `final_text`, not first token.** Speech synthesis cannot start
    until the reply text is complete, so a faster first token buys nothing on its own.
19. **Provider-side end-of-turn removes the whole client silence window.** With a local VAD the
    pipeline closes the stream; with provider endpointing the stream stays open and the
    provider calls back. Let providers advertise which they do and branch on it rather than
    tangling the two. Act on the final end-of-turn only — never dispatch speculatively on an
    eager one. Always keep a client-side ceiling so a turn that never ends re-arms cleanly.
20. **Answer "can it stream?" with a probe before designing for it** — see #21.
21. **NO progressive/streaming audio: the MTL decoder buffers to EOF.** A file dribbled to the
    rabbit over ~7.8 s produced sound only *after* the transfer completed; `tcpdump` confirmed
    Apache delivered it progressively (data packets ~250 ms apart), so the buffering is the
    decoder's. Spread it wider still and there is no sound at all. **Keep complete MP3s served
    statically.** Since delivery cannot be overlapped, the only latency levers left are
    producing the text and its audio faster, and shorter replies.
22. **Local TTS must be an HTTP client of a PERSISTENT server, not a CLI per utterance** —
    spawning the binary reloads the model every time and measures a deliberately inefficient
    path. Route the voice by the STT's **detected language**, never by text heuristics, and
    require both languages explicitly rather than silently reusing one voice for the other.
    **Benchmarking hazard worth stating:** if a fallback provider is configured, a failing
    local synth returns the fallback's audio under the local label — enough to promote the
    wrong engine by accident. Disable fallback while benchmarking and tag results with the
    backend that actually produced them.
23. **Judge a voice through the rabbit's own speaker, per language** — never on a synthetic
    real-time factor. Pace is part of the verdict: the production Italian voice needed
    `length_scale` 1.25, the English one 1.0. Volume is too: a voice that measures well can be
    unusably quiet on 2006 hardware.
24. **A body state must follow the SPEAKER, not the server.** A "generation finished" event
    arrives while seconds of audio are still queued, so any indicator driven by it goes dark
    while the rabbit is visibly still talking. Drive it from real playback events, and debounce
    the drain — a gap between chunks is not the end of an utterance. Corollary for barge-in:
    when the user interrupts, report how much was **actually heard**, scoped to the item the
    truncation names, or the model's transcript keeps words that were never spoken aloud.

## Build & deployment

- **OJN master is Qt4-era code and does not build against Qt5+** (Ubuntu 24.04): removing
  `-Werror` is not enough — `QHttp` (removed in Qt5), `QString::toAscii()` and other API/ABI
  breaks remain. Porting is out of scope.
- **Deployment shape:** the daemon is built and run in a locally-built **Debian buster
  container** (last Debian shipping Qt4), pinned to OJN commit `640257f3` — `ojn/docker/`.
  No third-party OJN images. Host networking (HTTP API on 127.0.0.1:8080; XMPP on :5222);
  state in `/var/lib/openjabnab` (bind-mounted at `/data`).
- The **PHP http-wrapper stays on host Apache** (vhost in `ojn/apache/`, DocumentRoot =
  `<OJN_DIR>/http-wrapper`, `AllowOverride All` + `mod_rewrite`); `openjabnab.php` reaches the
  daemon at 127.0.0.1:8080, and the daemon's `RealHttpRoot` points at the same
  `http-wrapper/ojn_local/` via bind mount so chor/broadcast files land where Apache serves them.
- **The XMPP server must be a hostname.** The MTL bootcode does not special-case an IPv4
  literal — it hands the string to its DNS resolver. Serve a name from dnsmasq and announce it
  in `openjabnab.ini` (`PingServer`/`BroadServer`/`XmppServer`).
- **The V2 firmware answers neither ICMP ping nor arping.** Liveness = the DHCP lease plus the
  rabbit's own traffic; its first request on boot is `GET /vl/bc.jsp?v=<fw>&m=<mac>...` on port
  80, which the `http-wrapper` `.htaccess` rewrites to the static bootcode.
- **PHP 8.3 fixes** (apply idempotently after every pinned checkout): `php-xml` package;
  `session_start('openJabNab')` → `session_name(...)` + `session_start()`; `split()` →
  `explode()` in the cinema admin plugin.
- **First-account bootstrap:** the built-in `admin/admin` lives only in memory and is never
  saved (`accountmanager.cpp:111`). Use it once to register a real account — OJN auto-promotes
  the first registered account to admin (`accountmanager.cpp:253`) and persists it — then
  restart the daemon so the default evaporates. Keep `AllowAnonymousRegistration = false`.
- **Stray `bunnies/.dat`:** `BunnyManager::GetBunny` (`bunnymanager.cpp:81`) auto-creates a
  Bunny for *any* unknown serial — including an empty one — before any token check. Any VAPI
  probe without a valid `sn` creates it. Don't smoke-test VAPI with bogus serials.

### Security notes (worth knowing before you deploy this)

- **Upstream NetworkDump leaks credentials.** `NetworkDump::Log("Api Call", GetRawURI())`
  (`httphandler.cpp:40`) appends every raw API URI — including `pass=`, `token=` and `tk=` —
  to `dump.log` in cleartext, with no off switch upstream. Our image replaces `netdump.cpp`
  (`ojn/docker/patches/`): off by default, opt-in with `[Log] NetworkDump = true`, and
  pass/token/tk redacted even when enabled. The file lives in the container's ephemeral layer.
- **Apache's stock `combined` log format also carries them**, via `%r` (the full request line,
  query string included). Use a dedicated LogFormat with `%m %U %H` — `%U` excludes the query
  string. If you ran the stock format first, rotate the password and truncate the old log.
- The rabbit's own segment uses 2006-era crypto (WPA/TKIP). Treat it as untrusted: separate
  subnet, no route to the home LAN or the internet, firewall limited to rabbit ⇄ host.

## 4. Consequences for `OjnAdapter`

- Primary surface = **VAPI** (`api.jsp` / `api_stream.jsp`) plus a handful of
  `/ojn_api/bunny/...` calls (VAPI enablement, plugin registration). `packet/sendMessage` is
  kept as an escape hatch behind a config flag.
- `BodyCapabilities`: `can_cancel_audio=False`, `has_playback_events=False`,
  `can_read_body_state=False`, `has_per_led_rgb=True`, `ear_range=(0,16)`.
- `PlaybackHandle.wait_finished` = timer from summed MP3 durations + guard.
- Audio queue: pass sentence MP3 URLs as one `urlList` call when they are ready together;
  otherwise sequential calls with duration-timer pacing.
