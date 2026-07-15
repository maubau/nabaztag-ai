# nabaztag-mcp

MCP server (stdio) exposing the rabbit to Claude Desktop / Claude Code, **through the
`BodyController`** — never the adapter directly — at `AGENT_EXPRESSION` priority, so a live
voice conversation always wins over a Desktop command.

Tools: `speak(text)`, `move_ears(left, right)`, `set_leds({led: [r,g,b]}, pulse)`,
`play_choreography(name)`, `last_rfid()`, `body_state()`.

## Install & try (no hardware)

```bash
pip install -e brain -e mcp
NABAZTAG_MOCK_OJN=1 nabaztag-mcp
```

## Claude Desktop config (real rabbit)

```json
{
  "mcpServers": {
    "nabaztag": {
      "command": "nabaztag-mcp",
      "env": {
        "OJN_BASE_URL": "http://bolt:8080",
        "RABBIT_SERIAL": "<your rabbit serial>",
        "OJN_VAPI_TOKEN": "<from /ojn_api/bunny/<sn>/getVAPIToken>"
      }
    }
  }
}
```
