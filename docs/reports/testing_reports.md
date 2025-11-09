# Testing Reports

## Inspector + Everything Server

- **Environment**
  - Location: `/Users/tt/Desktop/mcp-proxy`
  - Downstream server: `/Users/tt/cobo/servers/src/everything` (built via `npm install && npm run build`)
  - Python 3.9, Node.js (npx) available on PATH
- **Command**
  ```bash
  npx -y @modelcontextprotocol/inspector@latest --cli -- \
    --method tools/list \
    python3 -m mcp_proxy.main --config /Users/tt/Desktop/mcp-proxy/configs/config.everything.json
  ```
- **Observed Behavior**
  - Inspector successfully connects to the proxy, the proxy launches everything server using newline framing, and the tool list contains prefixed entries such as `everything__echo`, `everything__add`, …
  - Resources and prompts are also visible, confirming end-to-end handling of MCP capabilities (`resources/list`, `prompts/list`).
  - No timeouts or framing errors; proxy logs show upstream initialization, and Inspector returns JSON with the aggregated tools.
- **Conclusion**
  - The proxy is protocol-compatible with both the Inspector client and the “everything” reference server, covering tools, prompts, resources, and logging.

## Codex MCP Integration

- **Configuration Command**
  ```bash
  codex mcp add --env PYTHONPATH=/Users/tt/Desktop/mcp-proxy proxy -- \
    python3 -m mcp_proxy.main --config /Users/tt/Desktop/mcp-proxy/configs/config.everything.json
  ```
  This ensures the proxy package is on `PYTHONPATH` even when Codex launches it from another directory.
- **Verification Steps**
  1. `codex mcp list` shows `proxy` in `enabled` status with a full tool/resource inventory.
  2. Launching a Codex session exposes the aggregated tools and the earlier `400 Invalid tool name` error disappears because tool names now use `everything__*` (underscore separator) to satisfy Codex’s regex restriction.
  3. Codex MCP panel reports resources and prompts, proving the proxy handles Codex’s stdio client correctly.
- **Conclusion**
  - Codex can hand-shake with the proxy, enumerate everything server’s tools/resources, and invoke them via MCP after the naming and PYTHONPATH adjustments.
