# MCP Server Proxy 方案说明

## 1. MCP 协议流程回顾

1. **传输层**：MCP 基于 JSON-RPC2，常用 STDIO / WebSocket / HTTP(SSE)。STDIO 采用 LSP 风格的 `Content-Length` 帧。
2. **初始化**：客户端向 Server 发送 `initialize`，包含 `clientInfo`、`capabilities` 等；Server 返回自身能力 (`capabilities`) 与 `serverInfo`。
3. **常规请求**
   - `tools/list`/`tools/call`：列出工具、调用工具，参数遵循 JSON Schema。
   - `resources/list`/`resources/read`/`resources/templates/list`。
   - `prompts/list`/`prompts/get`：提示模板相关。
   - `logging/setLevel`、`ping` 等管理指令。
4. **双向通信**：Server 可主动向客户端发起请求（例如 `roots/list` 索取工作区信息）或发送通知；客户端需要回包。
5. **关闭**：`shutdown` 请求+进程退出，保证资源释放。

## 2. Proxy 架构

```
┌─────────────┐    JSON-RPC    ┌───────────────────┐
│ MCP Client  │◀──────────────▶│ Proxy (this repo) │
└─────────────┘  (STDIO)       └─────┬─────────────┘
                                      │spawn/stdio
             ┌────────────────────────┼────────────────────────┐
             ▼                        ▼                        ▼
        Upstream A               Upstream B               Upstream C
```

### 模块拆分

| 模块 | 主要职责 |
| --- | --- |
| `framing.JsonRpcStream` | 解析/封装带 `Content-Length` 的 JSON-RPC 报文 |
| `config` | 读取 JSON 配置，描述多个 downstream server 的命令、环境、超时 |
| `upstream.UpstreamServer` | 管理单个下游 MCP 进程：启动、初始化、请求/响应映射、stderr 日志泵 |
| `proxy.ProxyRouter` | 面向客户端的聚合逻辑，路由请求、合并工具/资源/提示，维护名称/URI/游标映射 |
| `main` | CLI 入口：接管 STDIO，初始化日志并运行 Proxy |

### 关键数据结构

- **Tool/Prompt 命名**：`<serverId>::<originalName>`，同时写入 `metadata.proxy`，用于反向路由。
- **资源 URI**：编码成 `proxy://resource/<base64({"server": "...", "uri": "..."})>`，避免不同 Server 的 URI 冲突。
- **游标**：以 `{ "offset": <int> }` JSON 结构的 base64 表示，兼容 `cursor`/`limit` 参数。
- **请求路由表**：`server_request_router` 记录下游→上游的请求 ID 映射，确保客户端的响应可以转回对应 Server。

## 3. 聚合策略

1. **initialize**
   - 并行启动所有下游 Server，透传客户端的 `capabilities`，但将 `clientInfo` 改写为 `*-through-proxy`，便于定位。
   - 收集下游返回的 `capabilities`，按类别求并集后向客户端汇报。

2. **tools/list**
   - 顺序调用每个下游 Server，同步其返回的工具列表。
   - 生成带命名空间的新工具名；缓存 `tool_registry` 供后续 `tools/call` 反查。
   - 二次分页：聚合后按照 `cursor`/`limit` 切片，返回 `tools` + `nextCursor`。

3. **tools/call**
   - 解析 `serverId` 与真实 `tool.name`，仅把原始请求转发给命中的下游 Server。
   - 响应内容不做修改直接返回。

4. **resources/***
   - `list`：包装 URI、写入 `resource_registry`，并将 `metadata.proxy.originalUri` 记录下来。
   - `read`：根据代理 URI 或实时解析 `proxy://resource/...` 还原为真实 URI，再转发请求。
   - `templates/list`：同工具一样做聚合，必要时可扩展 `templates/create`。

5. **prompts/***
   - 与 tools 相同的命名空间处理方式，复用 `prompt_registry`。

6. **通知 & 反向请求**
   - 下游通知：附加 `params.proxy.server` 后原样回传给客户端，方便调试。
   - 下游请求：为客户端生成新的字符串 ID (`serverId:<seq>`)，保存映射；待客户端回应后改写为原 ID 并发回下游。
   - 客户端的通知默认广播到所有下游，避免遗漏日志/心跳。

## 4. 运行与测试

1. **配置** `config.json`，每个 server 指定：
   - `id`：唯一命名空间
   - `command`：启动命令数组（如 `["npx","@modelcontextprotocol/server-everything"]`）
   - 可选 `env`、超时时间
2. **启动代理**：`python -m mcp_proxy.main --config config.json`
3. **客户端测试**：
   - `npx @modelcontextprotocol/inspector --cli <proxy command> --method tools/list`
   - 在 UI 中观察工具命名是否带 `serverId::`
   - 调用工具、读取资源、拉取提示，确认路由正确
   - 从任一下游触发 `roots/list` 等请求，验证客户端能收到并回复

## 5. 扩展方向

- **健康检查**：为下游 Server 增加重试/心跳，防止单个下游挂掉导致整个代理阻塞。
- **动态注册**：监听配置文件变更或提供管理 API，允许运行时增删下游 Server。
- **权限隔离**：基于配置限制某些客户端只能访问指定命名空间或工具子集。
- **观测性**：暴露 Prometheus 指标（请求耗时、下游状态）与结构化日志，方便生产运维。

