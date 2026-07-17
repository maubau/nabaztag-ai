# ojn-plugin-events

Tiny OpenJabNab plugin: on **button click** and **RFID read** it fires a GET to a per-bunny
webhook URL. This is the event-egress path of the nabaztag-ai project (Gate G0 decision: the
stock `callurl` fallback is disqualified — its `CU` packet reaches the rabbit but the OJN
bootcode never performs the HTTP request; server-side egress is the only reliable option).

## License — read this before touching the code

**GNU GPL v2, same terms as OpenJabNab** ([LICENSE](LICENSE) is copied verbatim from the
OpenJabNab repository's `COPYING`). This plugin compiles against OpenJabNab's headers and is a
derivative work of it. It is deliberately isolated from the rest of nabaztag-ai (Apache-2.0):
**no code may be shared between this directory and `brain/`/`mcp/`** — the brain interacts
with the plugin only over HTTP. Preferred endgame: upstream this as a PR to OpenJabNab.

## Build

Compiled into the Docker image by `ojn/docker/Dockerfile` (copied into
`server/plugins/events/` and added to the plugins `SUBDIRS` before qmake). No manual step.

## Enable (per bunny, one time)

```bash
TOKEN=...   # /ojn_api/accounts/auth
B="http://127.0.0.1/ojn_api/bunny/<sn>"
curl -sG "$B/registerPlugin"        --data-urlencode "name=events" --data-urlencode "token=$TOKEN"
curl -sG "$B/setSingleClickPlugin"  --data-urlencode "name=events" --data-urlencode "token=$TOKEN"
curl -sG "$B/setDoubleClickPlugin"  --data-urlencode "name=events" --data-urlencode "token=$TOKEN"
curl -sG "$B/events/setWebhook" --data-urlencode "url=http://127.0.0.1:8091/event" --data-urlencode "token=$TOKEN"
```

(Clicks are only delivered to the plugin selected via `setSingle/DoubleClickPlugin`; RFID goes
to every registered bunny plugin until one handles it — unregister `callurl` to avoid it
swallowing mapped tags.)

## Webhook format

```
GET <url>?bunny=<sn>&event=click&value=single|double
GET <url>?bunny=<sn>&event=rfid&value=<tag hex>
```

The brain-side listener is `rabbit_brain.body.events_server.EventListener` (default
`127.0.0.1:8091` — the daemon runs with host networking, so localhost reaches the brain).
