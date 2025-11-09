# MCP Proxy 聚合器

该项目实现了一个符合 Model Context Protocol (MCP) 规范的中间代理层。代理作为单一 MCP Server 提供给客户端 (如 Cursor、Claude Desktop、Inspector CLI)，在后台同时连接多个不同的 MCP Server，并聚合它们的工具、资源与提示能力。

## 功能特性

- **多 Server 聚合**：在 `config.json` 中配置多个下游 Server，代理负责统一初始化、心跳和关闭。
- **工具/资源/提示统一命名**：自动为工具与提示加上 `serverId::` 前缀，资源 URI 转换成 `proxy://resource/...`，客户端可以无缝调用。
- **JSON-RPC 路由**：代理实现 `initialize`、`tools/*`、`resources/*`、`prompts/*`、`logging/setLevel` 等核心方法，同时支持下游 Server 主动向客户端发起请求或通知。
- **分页游标**：聚合多个 Server 返回的数据后，按照 `cursor`/`limit` 参数完成二次分页；游标使用 base64 编码的 offset，客户端可透明续读。
- **协议透传**：未知通知自动广播至所有下游 Server，下游发起的请求/通知会被追加元数据后回流给客户端，确保兼容性。
- **可选鉴权与限流**：通过配置 `auth_token` 与 `rate_limit_per_minute`，可在代理层启用共享 token 校验与简单速率限制，后续可替换为更复杂实现。
- **结构化日志**：`structured_logging` 设为 `true` 时，所有日志会以 JSON 输出，便于集中式日志系统解析。

## 快速开始

1. **准备配置**

```json
{
  "log_level": "INFO",
  "auth_token": "optional-shared-secret",
  "rate_limit_per_minute": 60,
  "structured_logging": true,
  "healthcheck_interval": 30,
  "healthcheck_timeout": 5,
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

   如果需要直接接入本地下载的官方 `everything` Server，可使用 `configs/config.everything.json`，但要先在 `/Users/tt/cobo/servers/src/everything` 执行 `npm install` 与 `npm run build` 生成 `dist/index.js`：

   ```bash
   cd /Users/tt/cobo/servers/src/everything
   npm install
   npm run build
   ```

   然后：

   ```bash
   python -m mcp_proxy.main --config configs/config.everything.json
   ```

   Inspector 客户端同理，进入 `/Users/tt/cobo/inspector` 执行 `npm install && npm run build` 后，运行：

   ```bash
   node cli/build/cli.js --cli "python3 -m mcp_proxy.main --config configs/config.everything.json" --method tools/list
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

- **单元/集成测试**：`python3 tests/run_proxy_test.py`，其中包含两个最小 server 和一个会主动发起 `roots/list` 请求的 server，用于验证工具/资源/提示以及下游主动请求的转发流程。
- **Inspector 自检**：

  ```bash
  AUTH_TOKEN=demo-token npx -y @modelcontextprotocol/inspector@latest --cli -- \
    --method tools/list \
    python3 -m mcp_proxy.main --config configs/config.everything.json
  ```

  重点检查 `initialize` 返回的 `capabilities`、工具/资源命名空间是否正确，以及 prompts/resources 能否在实际 server 上调用。主动请求（如 `roots/list`）也会在日志中打印，由测试脚本自动响应。

## 安全配置

- **鉴权**：在配置中添加 `"auth_token": "..."` 后，客户端需要在每次请求时将 token 放在 `params.proxy.authToken` 字段，代理会先校验再继续处理。
- **限流**：设置 `"rate_limit_per_minute": <int>` 可开启简单的每分钟限流策略，键为 auth token（未配置 token 时默认 `anonymous`）。超限请求会收到 `Rate limit exceeded` 错误。
- **结构化日志**：设置 `"structured_logging": true` 后将启用 JSON 日志格式，默认情况下沿用传统文本格式。
- **健康检查**：配置 `"healthcheck_interval"`/`"healthcheck_timeout"` 后，代理会定期对下游 server 发起心跳，异常时自动重启。
- 以上机制都通过 `AuthManager`、`RateLimiter`、健康检查以及可插拔日志模块实现，后续可替换为更复杂的存储或多租户方案。

## Roadmap

### P0 / 高优先级
- **观察性指标**：在 `ProxyRouter` 的请求入口埋点，记录 `tools/*`、`resources/*` 等调用的耗时和返回码，并在 `UpstreamServer` 层统计重启次数/健康状态，统一输出到结构化日志或 Prometheus 采集端点。
- **细粒度权限**：扩展 `AuthManager`，允许读取配置中针对 serverId/工具名/资源前缀的 ACL 规则，对多租户环境执行拒绝或脱敏策略。

### P1 / 中优先级
- **客户端通知策略**：新增可配置的通知路由表，将 `logging/*`、`roots/list` 等主动请求按 server 精确投递，避免无关广播。
- **动态注册 / Reload**：为 `config` 模块加入文件监控或控制面指令，运行时增删下游 server，并触发 `UpstreamServer` 的热重启。
- **增强限流**：在现有速率限制器中引入租户 ID、工具 ID 维度的速率桶，支持突发/平滑窗口混合策略。

### P2 / 长期规划
- **资源模板扩展**：补齐 `resources/templates/*` API，允许代理托管模板创建、订阅与权限审核。
- **控制面 API**：暴露 HTTP/gRPC 控制面查询下游健康、聚合指标，并支持手动触发重启或刷新命令。
- **更丰富认证机制**：在 `security` 模块实现 pluggable provider，支持 OAuth2 client credentials、HMAC 签名或 mTLS，以替换简单的静态 token。
