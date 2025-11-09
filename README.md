# MCP Proxy 聚合器

该项目实现了一个符合 Model Context Protocol (MCP) 规范的中间代理层。代理作为单一 MCP Server 提供给客户端 (如 Cursor、Claude Desktop、Inspector CLI)，在后台同时连接多个不同的 MCP Server，并聚合它们的工具、资源与提示能力。

## 功能特性

- **多 Server 聚合**：在 `config.json` 中配置多个下游 Server，代理负责统一初始化、心跳和关闭。
- **工具/资源/提示统一命名**：自动为工具与提示加上 `serverId::` 前缀，资源 URI 转换成 `proxy://resource/...`，客户端可以无缝调用。
- **JSON-RPC 路由**：代理实现 `initialize`、`tools/*`、`resources/*`、`prompts/*`、`logging/setLevel` 等核心方法，同时支持下游 Server 主动向客户端发起请求或通知。
- **分页游标**：聚合多个 Server 返回的数据后，按照 `cursor`/`limit` 参数完成二次分页；游标使用 base64 编码的 offset，客户端可透明续读。
- **协议透传**：未知通知自动广播至所有下游 Server，下游发起的请求/通知会被追加元数据后回流给客户端，确保兼容性。
- **可选鉴权与限流**：通过配置 `auth_token` 与 `rate_limit_per_minute`，可在代理层启用共享 token 校验与简单速率限制，后续可替换为更复杂实现。

## 快速开始

1. **准备配置**

```json
{
  "log_level": "INFO",
  "auth_token": "optional-shared-secret",
  "rate_limit_per_minute": 60,
  "response_timeout": 30,
  "servers": [
    {
         "id": "everything",
         "command": ["npx", "@modelcontextprotocol/server-everything"],
         "env": {},
         "startup_timeout": 20,
         "shutdown_grace": 3
       }
     ]
}
```

2. **运行代理**

   ```bash
   python -m mcp_proxy.main --config /path/to/config.json
```

   MCP 客户端连接时只需把代理进程当作普通 Server，即可看到 `everything::search`、`everything::weather` 等聚合后的工具。

   如果需要直接接入本地下载的官方 `everything` Server，可使用 `config.everything.json`，但要先在 `/Users/tt/cobo/servers/src/everything` 执行 `npm install` 与 `npm run build` 生成 `dist/index.js`：

   ```bash
   cd /Users/tt/cobo/servers/src/everything
   npm install
   npm run build
   ```

   然后：

   ```bash
   python -m mcp_proxy.main --config config.everything.json
   ```

   Inspector 客户端同理，进入 `/Users/tt/cobo/inspector` 执行 `npm install && npm run build` 后，运行：

   ```bash
   node cli/build/cli.js --cli "python3 -m mcp_proxy.main --config config.everything.json" --method tools/list
   ```

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `mcp_proxy/config.py` | 解析 JSON 配置并转为数据类 |
| `mcp_proxy/framing.py` | Content-Length 帧的读写封装 |
| `mcp_proxy/jsonrpc.py` | JSON-RPC 辅助结构与工具方法 |
| `mcp_proxy/upstream.py` | 单个下游 Server 的进程与请求管理 |
| `mcp_proxy/proxy.py` | 聚合/路由核心逻辑 |
| `mcp_proxy/main.py` | 命令行入口，桥接 STDIO |
| `docs/` | 方案与设计文档 |

## 命名与游标策略

- 工具/提示名称：`<serverId>::<originalName>`
- 资源 URI：`proxy://resource/<base64({"server": "...", "uri": "..."})>`
- 分页游标：`base64({"offset": <int>})`

## 测试建议

1. 准备至少两个 MCP Server（例如本地 `everything` 与自定义 Server），在配置中写好命令。
2. 使用 `@modelcontextprotocol/inspector` CLI：

   ```bash
   npx @modelcontextprotocol/inspector --cli "python mcp_proxy/main.py --config config.json" --method tools/list
   ```

3. 重点验证：
   - `initialize` 返回的 capabilities 覆盖了工具/资源/提示
   - `tools/call` 等请求正确路由，响应体与原始 Server 一致
   - 资源读写与提示获取都会回落到正确 Server
- Server 主动请求（如 workspace roots）能被转发给客户端再返回

## 安全配置

- **鉴权**：在配置中添加 `"auth_token": "..."` 后，客户端需要在每次请求时将 token 放在 `params.proxy.authToken` 字段，代理会先校验再继续处理。
- **限流**：设置 `"rate_limit_per_minute": <int>` 可开启简单的每分钟限流策略，键为 auth token（未配置 token 时默认 `anonymous`）。超限请求会收到 `Rate limit exceeded` 错误。
- 以上机制都通过 `AuthManager`、`RateLimiter` 以模块化方式实现，后续可替换为更复杂的存储或多租户方案。

## 限制与后续工作

- 目前聚合游标基于 offset，未对不同服务的 `nextCursor` 做细粒度协调；下游存在大量数据时可考虑懒加载策略。
- 未实现 `resources/templates/create`、`subscriptions` 等扩展方法，后续可按 MCP Spec 补全。
- 未包含自动健康检查/重启，可在 `UpstreamServer` 增加探活与指数退避策略。
