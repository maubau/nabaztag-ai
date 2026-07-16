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
| **RFID / button events egress** | **needs-plugin (small) — with two native fallbacks** | No webhook and no event-polling endpoint exist. `getlast/getlasts` (`bunny.cpp:961-989`) only expose connection metadata (LastIP etc.), admin-only. Events are dispatched to C++ plugins (`OnClick`, `OnRFID` — `plugininterface.h:47-49`). Options: **(a)** tiny `ojn-plugin-events` posting webhooks — clean, ~50 lines, but it is a plugin; **(b)** `callurl` plugin (stock): map RFID tag → URL via `/ojn_api/bunny/<id>/callurl/addrfid?tag=<hex>&url=..` — on tag read the **rabbit itself** fetches the URL (`CU` verb, `plugin_callurl.cpp:28`), so pointing it at the brain's HTTP server turns a GET-from-rabbit-IP into the event, zero C++; **(c)** single/double-click → `setSingleClickPlugin` to a plugin we control. **Decision: try (b) on hardware first; build (a) only if (b) proves unreliable.** Note this is NOT the choreography plugin — see verdict below |
| Ears/LED state readback | **not-possible** (source) | VAPI `ears` param answers a hardcoded `POSITIONEAR 0,0` TODO (`bunny.cpp:164-167`). BodyController must own state-tracking (it already does by design) |

## 3. Gate G0 verdict (software half)

**The T1 choreography plugin is NOT needed.** Native VAPI covers arbitrary ears, per-LED RGB,
timed choreographies, queued MP3-by-URL, and sleep/wake; `packet/sendMessage` covers anything
exotic left over. `ojn/plugin_choreo/` stays uncreated (per §11.4 / Gate G0 rules).

The only gap is **event egress**, which is a different, much smaller problem with a stock-plugin
fallback (`callurl`) that requires no C++ at all.

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
5. **OPEN** — `callurl/addrfid` → rabbit GETs the brain URL on tag read: reliable? latency?
6. **OPEN** — precise round-trip latency of a VAPI ear command (feeds BodyController deadlines
   and the p50 budget).

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
