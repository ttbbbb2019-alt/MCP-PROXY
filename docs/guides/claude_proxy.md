# Using the Proxy with Claude Desktop

This guide explains how to run the proxy against the `everything` MCP server and expose it to Claude Desktop.

## 1. Build the downstream MCP server

```bash
cd /Users/tt/cobo/servers/src/everything
npm install
npm run build
```

This produces `dist/index.js`, which the proxy launches via stdio.

## 2. Update the proxy configuration

`configs/config.everything.json` already references the built server:

```json
{
  "log_level": "INFO",
  "response_timeout": 30,
  "servers": [
    {
      "id": "everything",
      "command": [
        "node",
        "/Users/tt/cobo/servers/src/everything/dist/index.js"
      ],
      "env": {
        "LOG_LEVEL": "info"
      },
      "startup_timeout": 30,
      "shutdown_grace": 5,
      "stdio_mode": "newline"
    }
  ]
}
```

The `stdio_mode` flag tells the proxy to speak newline-delimited JSON with this server (matching the upstream implementation).

## 3. Smoke-test with Inspector

From `/Users/tt/Desktop/mcp-proxy`:

```bash
npx -y @modelcontextprotocol/inspector@latest --cli -- \
  --method tools/list \
  python3 -m mcp_proxy.main --config configs/config.everything.json
```

You should see the proxied tool names such as `everything::echo` and `everything::add`.

## 4. Point Claude Desktop at the proxy

Add a server entry to `claude_desktop_config.json` (or via the MCP UI) that launches the proxy:

```json
{
  "mcpServers": {
    "proxy": {
      "command": "python3",
      "args": [
        "-m",
        "mcp_proxy.main",
        "--config",
        "/Users/tt/Desktop/mcp-proxy/configs/config.everything.json"
      ],
      "cwd": "/Users/tt/Desktop/mcp-proxy"
    }
  }
}
```

Restart Claude Desktop (or reload MCP servers). The UI will list the aggregated tools with the `everything::` prefix. Invoking them now forwards through the proxy into the `everything` server.
