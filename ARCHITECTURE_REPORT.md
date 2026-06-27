# SmartClaw 架构与业务处理流程报告

> 生成日期: 2026-06-27 | 基于 src/smartclaw/ 全面梳理

---

## 一、项目总览

SmartClaw 是一个**生产级企业 AI Agent 平台**，每个 Agent 运行在独立 microVM (Firecracker/Docker) 中实现硬件级隔离，支持飞书和企业微信双渠道。技术栈: Python >= 3.12 + Typer CLI + FastAPI + DeepAgents (LangGraph) + asyncio。

```
总 Python 文件: ~140 个
核心包: 24 个一级模块
入口: smartclaw = "smartclaw.cli:app" (CLI) + FastAPI app (HTTP)
```

---

## 二、系统分层架构

```
                         ┌──────────────────────────────────┐
                         │        外部入口 (Entry)            │
                         │  CLI (cli.py)  │  HTTP (server.py) │
                         │  typer.App     │  FastAPI          │
                         └───────┬────────┴────────┬─────────┘
                                 │                  │
              ┌──────────────────┼───────┐  ┌───────┼──────────────┐
              │  WebSocket 多进程服务     │  │  HTTP Webhook 服务      │
              │  feishu_multiprocess.py  │  │  server.py              │
              │  (mp.Process per App)    │  │  (单进程 asyncio)        │
              └──────────┬──────────────┘  └───────┬──────────────────┘
                         │                         │
              ┌──────────┴─────────────────────────┴──────────┐
              │          渠道适配层 (Channel Layer)             │
              │  FeishuAdapter │ WeComAdapter                  │
              │  feishu_ws.py (WebSocket)                      │
              │  feishu_runtime.py / feishu_context.py         │
              └──────────────────┬──────────────────────────────┘
                                 │
              ┌──────────────────┴──────────────────────────────┐
              │          鉴权/准入层 (Auth & Governance)         │
              │  PlatformAuthAdapter   │  TenantGovernor         │
              │  AuthPolicyManager     │  限流/配额/并发控制      │
              │  ToolSecurityContext   │  governance/store       │
              └──────────────────┬──────────────────────────────┘
                                 │
              ┌──────────────────┴──────────────────────────────┐
              │          路由层 (Routing Layer)                  │
              │  AgentRouter (agent/router.py)                   │
              │  @提及路由 / 关键词路由 / 用户绑定 / 群绑定      │
              └──────────────────┬──────────────────────────────┘
                                 │
              ┌──────────────────┴──────────────────────────────┐
              │         Agent 运行器 (Agent Runner)              │
              │  agent/runner.py - 核心编排引擎                  │
              │  ┌───────────────────────────────────────────┐  │
              │  │  UnifiedExecutionEngine (统一执行引擎)     │  │
              │  │  Planner → DeepAgents → ReAct → LLM+工具  │  │
              │  └───────────────────────────────────────────┘  │
              └──────┬────────────┬─────────────┬───────────────┘
                     │            │              │
    ┌────────────────┴───┐ ┌──────┴──────┐ ┌────┴──────────────┐
    │  沙箱层 (Sandbox)   │ │ 记忆层      │ │ 技能层 (Skills)   │
    │  sandbox/ + dockerimpl│ │ memory/    │ │ skills/           │
    │  Firecracker/Docker  │ │ MemoryMgr  │ │ SkillRegistry     │
    └─────────────────────┘ └────────────┘ └───────────────────┘
```

---

## 三、核心业务流程 — 消息处理完整数据流

### 3.1 请求入口 (两种模式)

```
模式 A: HTTP Webhook (server.py)
─────────────────────────────────
飞书/企微 POST → /webhook/feishu 或 /webhook/wecom
  → 签名校验 (PlatformAuthAdapter)
  → event 解密 (feishu_payload)
  → URL 验证 / 重放检测 / 租户校验
  → 提取消息上下文 (feishu_context.parse_feishu_event_body)
  → Agent 路由匹配
  → runner.process_message(...)

模式 B: WebSocket 多进程 (feishu_multiprocess.py)
─────────────────────────────────────────────────
每个飞书 App = 独立 mp.Process (FeishuWorker)
  → WebSocket 长连接 (FeishuWebSocketAdapter)
  → 持续接收事件流
  → 直接调用 runner.process_message(...)
```

### 3.2 消息处理主流程 (AgentRunner.process_message)

```
用户消息到达
  │
  ├─[1] 租户级准入 (TenantGovernor.admit)
  │     ├── 限流检查 (rate_limit)
  │     ├── 每日 Token 配额检查
  │     └── 拒绝 → 返回限流提示
  │
  ├─[2] 用户级准入 (TenantGovernor.admit_user)
  │     └── 同租户单用户公平性约束
  │
  ├─[3] 视觉服务同步 (_apply_vision_service_for_current_agent)
  │     └── 按租户+Agent 合并 [vision] 配置
  │
  ├─[4] 飞书工具凭证同步 (_sync_feishu_tool_credentials)
  │     └── 为 feishu_doc_tool 注入当前 Agent 的 app_id/app_secret
  │
  ├─[5] 会话管理 (SessionManager)
  │     ├── 获取或创建 Session
  │     ├── 群聊: 会话按 chat_id 复用, 不保持历史
  │     ├── 私聊: 按 user_id 创建/复用会话
  │     └── 写入用户消息到会话历史
  │
  ├─[6] 安全上下文注入 (ToolSecurityContext)
  │     ├── tenant_id, feishu_open_id
  │     ├── roles (权限角色)
  │     ├── integration_env (集成环境变量)
  │     └── 持续整个请求生命周期
  │
  ├─[7] 能力预检 (preflight_capabilities)
  │     └── 检查所需工具是否被允许/拒绝
  │
  ├─[8] 并发控制
  │     ├── 租户级并发 acquire → 释放
  │     └── 用户级并发 acquire → 释放
  │
  ├─[9] 记忆管理器 (MemoryManager.add_message)
  │     └── 写入 SQLite + 存储记忆要点
  │
  ├─[10] 执行循环 (_execute_loop → UnifiedExecutionEngine.execute_turn)
  │      │
  │      ├───[10a] 加载历史消息 (SessionManager.get_messages)
  │      │
  │      ├───[10b] 注入记忆上下文 (MemoryManager.get_context_for_llm)
  │      │        ├── FTS5 全文检索 ([记忆检索] 块)
  │      │        ├── [对话摘要] (LLM 自动生成)
  │      │        └── [长期记忆] (daily/longterm)
  │      │
  │      ├───[10c] Planner 规划 (可选, execution.planner_first_enabled)
  │      │        └── Planner.plan() → ExecutionPlan
  │      │
  │      ├───[10d] DeepAgents 执行 (主路径, execution.deepagents_enabled)
  │      │        ├── history 压缩 (clip_history_for_deepagents, max 50条)
  │      │        ├── DeepAgentsWrapper.run()  (LangGraph + TodoListMiddleware)
  │      │        ├── 后端选择: FirecrackerDeepAgentsBackend / DockerDeepAgentsBackend
  │      │        └── 沙箱中执行 Shell/文件工具
  │      │
  │      ├───[10e] ReAct 引擎回退 (execution.react_fallback_enabled)
  │      │        └── ReActEngine.execute()  (结构化推理循环)
  │      │
  │      └───[10f] LLM + 工具降级 (终极回退)
  │               └── LLM 调用 → 工具执行 → 递归
  │
  ├─[11] 写入助手消息 (SessionManager + MemoryManager)
  │
  └─[12] 后台记忆维护 (debounced, run_post_turn_memory_maintenance)
         ├── 自动摘要 (消息 > 50 条, 距上次 > 20 条)
         └── 事件提取与长期记忆写入
```

### 3.3 执行引擎级联回退链

```
UnifiedExecutionEngine 的优先级链（从高到低）:

1. Planner 规划（可选前置）  → 生成 ExecutionPlan
      ↓ (失败则跳过)
2. DeepAgents (LangGraph)    → 最优先执行引擎 (depth==0 时)
      ↓ (失败/未配置则回退)
3. ReAct 引擎                → 结构化推理循环 (Thought→Action→Observation)
      ↓ (失败/未配置则回退)
4. LLM + 工具递归            → 直接调用 LLM, 解析 tool_calls, 递归执行
      ↓ (达到 max_depth 则终止)
5. LLM 直接响应              → 纯文本回复, 无工具调用
```

---

## 四、模块间数据流动图

```
                    ┌──────────────────────┐
                    │  config.toml + .env   │
                    │  config/loader.py     │──→ get_config() 全局单例
                    └──────────┬───────────┘
                               │ 读取配置
            ┌──────────────────┼──────────────────┐
            ↓                  ↓                   ↓
   ┌─────────────┐   ┌──────────────┐   ┌────────────────┐
   │ auth/        │   │ channels/     │   │ execution/    │
   │ platform.py  │   │ config        │   │ memory/       │
   │ tool_gate.py │   │ (feishu/wecom)│   │ governance/   │
   └─────────────┘   └──────────────┘   └────────────────┘

渠道消息 (feishu/wecom)
  │
  ↓
agent/router.py  ←→  bindings.json  (用户绑定/群绑定)
  │
  ↓ Agent 名称
agent/runner.py (AgentRunner)
  │
  ├── agent/session.py         → Session ↔ JSONL 持久化 (sessions/)
  │                                 ↑↓
  ├── memory/manager.py        → SQLite / PostgreSQL 存储
  │   ├── memory/storage/sqlite_store.py     (默认)
  │   ├── memory/storage/postgres_store.py   (可选)
  │   ├── memory/storage/auto_summary.py     (LLM摘要)
  │   ├── memory/embeddings.py               (向量嵌入)
  │   ├── memory/budget.py                   (Token预算)
  │   ├── memory/session.py                  (会话记忆)
  │   ├── memory/daily.py                    (日记)
  │   └── memory/longterm.py                 (长期MEMORY.md)
  │
  ├── agent/deepagents_wrapper.py  → DeepAgents (LangGraph) 执行
  │   ├── agent/docker_deepagents_backend.py   → 沙箱: 工具在容器中执行
  │   └── agent/firecracker_deepagents_backend.py → 沙箱: 工具在 microVM 中执行
  │       └── agent/bg_execute.py (后台任务执行)
  │       └── agent/bg_probe_decl.py (后台探测)
  │
  ├── agent/react.py          → ReActEngine (回退引擎)
  │
  ├── agent/tools/registry.py → ToolRegistry (工具注册表)
  │   ├── exec_tool.py        (Shell 命令执行)
  │   ├── read_tool.py        (文件读取)
  │   ├── write_tool.py       (文件写入)
  │   ├── subagent_tool.py    (子Agent派生)
  │   ├── docker_tool.py      (Docker管理)
  │   ├── feishu_doc_tool.py  (飞书文档)
  │   ├── background_task_tool.py (后台任务)
  │   ├── memory_tool.py      (记忆工具)
  │   ├── workspace_tool_admin.py/loader.py (工作区工具)
  │   ├── loop_detector.py    (循环检测)
  │   ├── interactive_shell.py (交互式Shell)
  │   └── builtin_registration.py (批量注册)
  │
  ├── skills/registry.py      → SkillRegistry (技能注册表)
  │   ├── loader.py           (加载工作区技能)
  │   ├── governance.py       (审批/提升/回滚)
  │   ├── validate.py         (校验)
  │   ├── testing.py          (测试)
  │   ├── storage.py          (存储)
  │   └── scaffold.py         (脚手架)
  │
  ├── sandbox/                → 沙箱层
  │   ├── docker.py           (DockerSandboxBackend)
  │   ├── firecracker.py      (FirecrackerBackend)
  │   └── pool.py             (WarmPool 预热池)
  │
  ├── mcp/                    → MCP 工具集成
  │   └── mcp_registry_bridge.py → register_mcp_tools_for_agent()
  │
  ├── governance/             → 资源治理
  │   ├── governor.py         (TenantGovernor: 限流/配额/并发)
  │   └── store.py            (InMemoryStore / RedisStore)
  │
  ├── core/                   → 跨切面基础设施
  │   ├── event_bus.py        (EventBus: JSONL持久化事件)
  │   ├── subagent_spawn.py   (子Agent派生)
  │   └── subagent_registry.py (子Agent状态注册表)
  │
  ├── llm/                    → LLM 适配
  │   ├── registry.py         (LLM注册表)
  │   ├── base.py             (LLMProvider / LLMConfig / Message)
  │   ├── providers.py        (提供商配置)
  │   └── openai_compatible.py (OpenAI兼容)
  │
  ├── auth/                   → 鉴权
  │   ├── platform.py         (PlatformAuthAdapter)
  │   ├── policy_manager.py   (AuthPolicyManager)
  │   ├── tool_gate.py        (ToolSecurityContext + 工具门禁)
  │   └── feishu_payload.py   (飞书解密)
  │
  ├── tenancy/                → 多租户
  │   ├── registry.py         (租户注册表)
  │   ├── api.py              (租户CRUD API)
  │   └── redis_registry.py   (Redis租户注册表)
  │
  ├── monitoring/             → 可观测性
  │   ├── metrics.py          (指标: token_usage, execution_path)
  │   └── execution_trace.py  (执行追踪)
  │
  └── vision/                 → 视觉服务
      ├── service.py          (VisionService)
      └── tool.py             (视觉工具)
```

---

## 五、弃用/未使用的功能模块

### 5.1 确认已弃用的兼容垫片（已有 DeprecationWarning）

| 模块路径 | 说明 | 实际代码位置 |
|---------|------|------------|
| `tool/` (3 个文件, ~20 行) | 兼容别名，全部 re-export 自 `tool_packages/` | `tool_packages/` |
| `tools/policy/` (2 个文件, ~20 行) | 兼容别名，全部 re-export 自 `exec_policy/` | `exec_policy/` |

### 5.2 确认无效/未使用的模块（零引用）

| 模块路径 | 行数 | 说明 |
|---------|------|------|
| **`agent/firecracker_backend.py`** | 151 | `FirecrackerSandboxBackend` 类。实现了 deepagents 协议，但从未被任何文件 import。功能已被 `sandbox/firecracker.py` 中的 `FirecrackerBackend` 取代。 |
| **`agent/base_backend.py`** | ~40 | `BaseBackend` ABC + `ExecuteResponse`/`ServiceInfo` 数据类。旧版后端接口，未使用。被 `sandbox/base.py` 中的 `SandboxBackend` Protocol 取代。 |
| **`agent/docker_backend.py`** | ~250 | `DockerBackend(BaseBackend)`。旧版 Docker 后端实现，未使用。被 `sandbox/docker.py` (DockerSandboxBackend) 取代。 |
| **`core/integration_example.py`** | 260 | 示例/测试代码，从未被任何文件 import。典型开发残留。 |
| **`sandbox/docker_secure.py`** | 279 | `SandboxConfig` / `SandboxDockerConfig` / `DEFAULT_SANDBOX_CONFIG`。安全沙箱配置，从未被任何外部文件 import。 |
| **`sandbox/secure_executor.py`** | 314 | `SecureSandboxExecutor` 类。仅内部 import 了 `docker_secure.py`，但自身从不被外部 import。 |
| **`sandbox/vsock_agent.py`** | 15 | microVM 内 vsock agent 入口脚本。仅被自身 `__main__` 调用，未被项目代码 import。 |
| **`sandbox/vsock/`** (3 文件) | 589 | `VsockServer`/`VsockClient`/`VsockManager`。VSOCK 通信协议实现。`firecracker.py:558` 有 vsock 相关日志，但无实际 import。从未接入执行通路。 |

### 5.3 弃用模块总结

```
总计弃用代码行数: ~1,918 行

目录分布:
  agent/firecracker_backend.py ........ 151 行  (死代码)
  agent/base_backend.py ................ ~40 行  (死代码)
  agent/docker_backend.py .............. ~250 行 (死代码)
  core/integration_example.py .......... 260 行  (死代码)
  sandbox/docker_secure.py ............ 279 行  (死代码)
  sandbox/secure_executor.py .......... 314 行  (死代码)
  sandbox/vsock_agent.py .............. 15 行   (死代码)
  sandbox/vsock/server.py ............. 270 行  (死代码)
  sandbox/vsock/client.py ............. 219 行  (死代码)
  sandbox/vsock/manager.py ............ 100 行  (死代码)
  tool/ (3 files) ..................... ~15 行  (已弃用垫片)
  tools/policy/ (2 files) ............. ~5 行   (已弃用垫片)

总计: 12 个文件/模块，~1,918 行冗余代码
```

---

## 六、关键内部数据流详解

### 6.1 消息生命周期

```
外部消息 (JSON)
  │
  ▼
渠道适配器解析 → InboundMessage
  │
  ▼
AgentRouter.route_with_mentions() → Agent 名称 (str)
  │
  ▼
AgentRunner.process_message(user_id, channel, content, session_id, is_group, tenant_id)
  │
  ▼
Session.add_message("user", content) → JSONL 持久化
  │
  ▼
MemoryManager.add_message("user", content) → SQLite messages 表
  │
  ▼
UnifiedExecutionEngine.execute_turn(session)
  │  ┌─ Planner.plan(user_message, tools) → ExecutionPlan (可选)
  │  ├─ DeepAgents.run(history, thread_id) → str (首选)
  │  ├─ ReActEngine.execute(user_message, context) → ExecutionResult (回退)
  │  └─ LLM.chat(history, tools) → tool_calls → 工具递归 (终极回退)
  │
  ▼ Session.add_message("assistant", response)
  │ MemoryManager.add_message("assistant", response)
  │
  ▼ run_post_turn_memory_maintenance() (后台debounce)
  │  ├─ 自动摘要 (auto_summary)
  │  ├─ 事件提取
  │  └─ 长期记忆写入
  │
  ▼
渠道适配器发送回复 → OutboundMessage
  │
  ▼
外部用户收到回复
```

### 6.2 配置加载链

```
config.toml (优先级从高到低):
  1. /opt/smartclaw/config/config.toml   (系统安装)
  2. ~/.smartclaw/config/config.toml     (用户安装)
  3. <repo>/config/config.toml           (开发态)
  4. ./config.toml                       (当前工作目录)

加载流程:
  tomllib.load() → pydantic 验证 → SmartClawConfig(BaseModel)
    ├── [server]         → 服务配置 (host/port/max_request_bytes)
    ├── [channels]       → 飞书/企微多账号
    ├── [auth]           → 鉴权策略 (tenant_by_app_id, feishu_decrypt_webhook...)
    ├── [execution]      → 执行引擎开关 (deepagents/react/planner...)
    ├── [memory]         → 记忆存储 (sqlite/postgres_dsn)
    ├── [platform]       → 平台配置 (event_bus_enabled...)
    ├── [governance]     → 治理配置 (rate_limit, token_quota...)
    ├── [sandbox]        → 沙箱配置 (container_workspace...)
    ├── [vision]         → 视觉服务配置
    └── [llm]            → 全局 LLM 配置 (provider/model/api_key/base_url)
         ↓
  Agent 级覆盖: 每个 agent.json 的 llm 字段与全局 llm 合并
         ↓
  get_config() → 全局单例 (pydantic 模型)
```

### 6.3 Agent 配置与运行时

```
Agent 目录布局:
  {agents_dir}/
    ├── {tenant_id}/           ← 多租户 (default 租户可省略)
    │   └── {agent_name}/
    │       ├── agent.json     ← Agent 配置 (必须)
    │       ├── .compiled/     ← 编译产物
    │       │   └── agent.compiled.json  (system_prompt)
    │       └── skills/        ← 工作区技能
    └── {agent_name}/          ← default 租户的 Agent (兼容)

agent.json 结构:
  {
    "name": "...",
    "display_name": "...",
    "description": "...",
    "enabled": true,
    "channel": "feishu",
    "tenant_id": "...",
    "feishu": {"app_id": "...", "app_secret": "ENC:..."},
    "llm": {"provider": "...", "model_name": "...", "api_key": "ENC:..."},
    "sandbox": {"enabled": true, "type": "firecracker"},
    "workspace": "...",
    "system_prompt": "...",
    "aliases": [...],
    "mcp_servers": [...]
  }

AgentRunner 启动流程:
  1. _initialize_sandbox()     → 创建 microVM/Docker 容器
  2. _initialize_llm()         → LLM adapter 注册
  3. _apply_vision_service()   → 视觉配置合并
  4. 读取 agent.json 完整配置
  5. 解析工作区路径
  6. 初始化 MemoryManager
  7. 初始化 EventBus (可选)
  8. 初始化 SubagentSpawner
  9. 注册 workspace tools / MCP tools
  10. 构建 SkillRegistry → skills_prompt
  11. 初始化 DeepAgentsWrapper → create_deep_agent()
  12. 初始化 ReActEngine (回退)
  13. AgentStatus → RUNNING
```

### 6.4 工具执行安全门禁

```
ToolRegistry.execute(tool_name, parameters)
  │
  ├─[门禁 A] ToolSecurityContext 是否存在?
  │   ├─ 是 → 继续门禁链
  │   └─ 否 → 直接执行 (CLI 模式/无安全上下文)
  │
  ├─[门禁 B] agent.json denied/enforce 列表
  │   └─ 检查 tool_name 是否在 deny_list 或 enforce_list
  │
  ├─[门禁 C] AuthPolicyManager.check_tool_allowed()
  │   └─ 按用户角色/租户策略检查工具权限
  │
  ├─[门禁 D] 二次确认 (高危工具)
  │   └─ 需要用户显式确认才执行
  │
  ├─[门禁 E] host_command_gate (仅 Shell 命令)
  │   ├─ Tool Policy (exec_policy/engine.py)
  │   └─ Shell 白名单 (agent/shell_allowlist.py)
  │
  ├─[执行] handler(**parameters)
  │   ├─ 沙箱执行 (如果有 sandbox context)
  │   │   └─ sandbox_backend.execute(command, timeout_ms)
  │   └─ 本地执行 (无沙箱)
  │
  ├─[审计] audit_tool() 记录工具调用
  │
  └─[循环检测] loop_detector 检查重复调用
```

---

## 七、模块职责矩阵

| 模块 | 职责 | 关键类/函数 |
|------|------|-----------|
| `cli.py` | CLI 命令定义 (~95 commands) | `app = typer.Typer()` |
| `server.py` | FastAPI HTTP 服务 + webhook | `lifespan()`, `feishu_webhook()` |
| `feishu_multiprocess.py` | WebSocket 多进程服务 | `FeishuWorker(mp.Process)` |
| `feishu_ws_server.py` | WebSocket 单进程服务 | `feishu_main()` |
| `agent/runner.py` | Agent 生命周期 + 消息编排 | `AgentRunner` |
| `agent/unified_execution.py` | 统一执行引擎 | `UnifiedExecutionEngine` |
| `agent/router.py` | Agent 路由匹配 | `AgentRouter` |
| `agent/session.py` | 会话 CRUD + JSONL 持久化 | `SessionManager`, `Session`, `Message` |
| `agent/deepagents_wrapper.py` | DeepAgents (LangGraph) 集成 | `DeepAgentsWrapper` |
| `agent/react.py` | ReAct 推理引擎 | `ReActEngine` |
| `agent/planner.py` | 任务规划器 | `Planner`, `ExecutionPlan` |
| `agent/tools/registry.py` | 工具注册表 + 门禁 | `ToolRegistry` |
| `agent/tools/exec_tool.py` | Shell 命令工具 | `exec_command_handler` |
| `agent/tools/builtin_registration.py` | 所有内置工具注册 | `register_builtin_tools()` |
| `memory/manager.py` | 记忆系统 (SQLite + LLM摘要/检索) | `MemoryManager` |
| `skills/registry.py` | 技能注册表 (工作区技能) | `SkillRegistry` |
| `sandbox/docker.py` | Docker 沙箱后端 | `DockerSandboxBackend` |
| `sandbox/firecracker.py` | Firecracker 沙箱后端 | `FirecrackerBackend` |
| `sandbox/pool.py` | 沙箱预热池 | `WarmPool` |
| `core/dockerimpl/` | Docker 容器池管理 | `ContainerPool`, `ProjectManager` |
| `core/event_bus.py` | JSONL 事件总线 | `EventBus` |
| `core/subagent_spawn.py` | 子Agent 分派 | `SubagentSpawner` |
| `config/loader.py` | 配置加载 + pydantic 模型 | `get_config()`, `SmartClawConfig` |
| `llm/` | LLM 适配器注册表 | `LLMRegistry`, `LLMProvider` |
| `channel/` | 飞书/企微渠道适配 | `FeishuAdapter`, `WeComAdapter` |
| `auth/` | 鉴权 + 工具门禁 | `PlatformAuthAdapter`, `AuthPolicyManager` |
| `governance/` | 租户资源治理 | `TenantGovernor` (限流/配额/并发) |
| `tenancy/` | 多租户注册表 | Redis/SQL-based tenant CRUD |
| `mcp/` | MCP 协议桥接 | `register_mcp_tools_for_agent()` |
| `monitoring/` | 可观测性 (指标 + 追踪) | `record_token_usage()`, `record_execution_event()` |
| `vision/` | 视觉服务 | `VisionService` |
| `exec_policy/` | Shell 命令安全策略 | `check_command()` |
| `audit/` | 审计日志 | `feishu_inbound()`, `audit_tool()` |
| `paths.py` | 统一路径常量 | `INSTALL_ROOT`, `AGENTS_DIR`, ... |
| `tenant.py` | 租户工具函数 | `normalize_tenant_id()`, `tenant_agent_key()` |
| `interfaces.py` | 核心接口/协议/数据模型 | `AgentConfig`, `Message`, Protocols |

---

## 八、两个被保留的死亡模块 (需特别说明)

`tool/` 和 `tools/policy/` 两个包已经明确被标记为兼容垫片:
- `tool/` → 所有内容 re-export 自 `tool_packages/`，`import warnings` + `DeprecationWarning`
- `tools/policy/` → 所有内容 re-export 自 `exec_policy/`，`import warnings` + `DeprecationWarning`

这两个模块**确认为弃用但有意保留**（平滑迁移窗口）。其余 8 个未使用模块（~1,878 行）是真正的死代码。

---

## 九、总结

SmartClaw 的业务处理流程遵循: **渠道接入 → 鉴权准入 → Agent 路由 → 会话管理 → 记忆注入 → 多引擎级联执行 (DeepAgents → ReAct → LLM+工具) → 沙箱隔离 → 记忆维护 → 渠道回复**。

数据以 **config.toml (全局) + agent.json (Agent级)** 两层配置为依托，**SessionManager (JSONL)** 和 **MemoryManager (SQLite/PostgreSQL)** 为持久化主干，**ToolRegistry** 为工具编排中心，**TenantGovernor** 为多租户资源控制面。

发现 **8 个弃用/无效模块**（共约 1,878 行死代码），另有 **2 个已标记 DeprecationWarning 的兼容垫片**（有意保留）。

建议在后续版本中移除这 8 个死代码模块以减轻维护负担：
- `agent/firecracker_backend.py`
- `agent/base_backend.py`
- `agent/docker_backend.py`
- `core/integration_example.py`
- `sandbox/docker_secure.py`
- `sandbox/secure_executor.py`
- `sandbox/vsock_agent.py`
- `sandbox/vsock/` (整个目录)
