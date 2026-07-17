# nabaztag-mcp

MCP server (stdio) exposing the rabbit to Claude Desktop / Claude Code, **through the
`BodyController`** — never the adapter directly — at `AGENT_EXPRESSION` priority, so a live
voice conversation always wins over a Desktop command.

Tools: `speak(text)`, `move_ears(left, right)`, `set_leds({led: [r,g,b]}, pulse)`,
`play_choreography(name)`, `last_rfid()`, `body_state()`.

The lifespan also starts the **EventListener** (default `127.0.0.1:8091`) that receives
`ojn-plugin-events` webhooks, so `last_rfid()` reflects real tag reads.

## Install & try (no hardware)

```bash
pip install -e brain -e mcp
NABAZTAG_MOCK_OJN=1 nabaztag-mcp
```

## Environment (real rabbit — MCP runs ON the Bolt)

```
OJN_BASE_URL=http://127.0.0.1     # the Apache wrapper, port 80.
                                  # NEVER :8080 — that is OJN's internal binary
                                  # framing, not HTTP (docs/OJN_API_NOTES.md §1)
RABBIT_SERIAL=<sn>                # the rabbit's MAC without colons
OJN_VAPI_TOKEN=<token>            # from /ojn_api/bunny/<sn>/getVAPIToken
NABAZTAG_EVENTS_PORT=8091         # optional; must match events/setWebhook
```

The MCP server must run **on the Bolt**: the events webhook targets `127.0.0.1:8091` from the
OJN daemon (host network), so the listener has to live on the same machine.

## Claude Desktop (runs on the Mac) → SSH launcher

Claude Desktop speaks stdio, so it launches the server over SSH (needs key-based auth to the
Bolt). Put the env vars in the Bolt's `~/nabaztag-ai/.env` and use:

```json
{
  "mcpServers": {
    "nabaztag": {
      "command": "ssh",
      "args": [
        "-T", "maurizio@udoo-bolt",
        "cd ~/nabaztag-ai && set -a && . ./.env && exec .venv/bin/nabaztag-mcp"
      ]
    }
  }
}
```

(`set -a && . ./.env` exports the variables to the server process; `exec` keeps stdio clean.)
