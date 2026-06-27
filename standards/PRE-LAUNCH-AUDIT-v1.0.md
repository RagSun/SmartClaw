# 上线前极客级别代码审计报告 (Pre-launch Audit Report)

**初版审计日期**: 2026-03-25  
**复核修订日期**: 2026-04-30（对照当前 `src/smartclaw` 实现修订 **已过时的结论**，下文以 **当前代码** 为准）

**审计目标**: `smartclaw` 项目核心代码库 (`src/smartclaw/` 及 `tests/`)

**复核结论摘要**: 原报告中 **「Event Bus 虚假集成」已不再成立**；飞书 Webhook **平台签名校验（Lark 请求头）**、健康检查真实探测、Firecracker 快照等 **仍为高风险或未闭合项**。整体是否可上线需按部署形态（仅长连接 / 是否暴露 HTTP Webhook）单独评估。

---

## 修订说明（为何更新本文档）

- 初版部分条目依赖当时代码快照；后续已合并 **Harness 对齐**、**EventBus 可配置启用**、**飞书 HTTP 多 app_id / 扁平凭证配置** 等改动。
- 本文件用于 **审计可追溯**：**作废**的条目明确标注；**仍有效**的条目保留或更新说明。

---

## 🛑 1. 致命级安全漏洞 (Critical Security Issues)

### 1.1 飞书 Webhook 签名验证缺失 (CWE-345) — **仍有效**

- **代码位置**: `src/smartclaw/channel/feishu.py`（`parse_message` 内约 122–125 行：`# TODO: 实现签名验证` + `pass`）；`src/smartclaw/server.py` 的 `/webhook/feishu` 路径 **未见** `X-Lark-Signature` / `X-Lark-Request-Timestamp` / `X-Lark-Request-Nonce` 校验。
- **说明**: 当前已支持 **自建 Webhook Token**、**encrypt 体解密（含多账号 encrypt_key 尝试）**、**可选防重放**；上述措施 **不能替代** 飞书开放平台对 HTTP 回调定义的 **请求签名**（若开发者后台策略要求）。
- **业务影响**: 在 **暴露公网 Webhook** 且仅依赖弱校验时，存在伪造回调风险。
- **具体解决方案**（对照官方文档，而非仅内部 Token）:
  1. 参阅飞书开放平台 **[接收事件回调](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/callback-subscription/receive-and-handle-callbacks)**，实现请求体与请求头联合校验。
  2. 按文档使用 `X-Lark-Request-Timestamp`、`X-Lark-Request-Nonce` 与 **`X-Lark-Signature`**（算法以官方为准）。
  3. **加密推送（`encrypt` 字段）** 与 **HTTP 签名校验** 为不同维度；若同时启用则需同时实现。

### 1.2 沙箱文件系统越权风险与执行崩溃 (CWE-22) — ✅ 已于 2026-03-25 修复

（初版描述保留作历史记录）原 `docker_deepagents_backend` / `sandbox/docker` 路径问题已按初版修复方案处理。**回归验证**应在当前分支上持续执行。

---

## ⚠️ 2. 核心架构功能断层 (Architectural Breakages)

### 2.1 Event Bus 集成 — **原「虚假集成」结论已作废（2026-04 复核）**

- **原问题（初版）**: `runner.py` 注释写「暂时禁用 EventBus」，易被理解为未接入主链路。
- **当前代码行为**:
  - `AgentRunner.start()` 在 `get_config().platform.event_bus_enabled` 为 **true** 时创建 `smartclaw.core.event_bus.EventBus` 实例并挂载（见 `runner.py` 约 299–311 行）。
  - `UnifiedExecutionEngine` 可在执行节点向 EventBus 发射 **`execution.*`** 类事件（与 Harness L1 设计一致）；默认配置通常为 **`event_bus_enabled = false`**，属渐进上线策略，而非「未实现」。
- **仍建议改进（文档/可读性）**:
  1. `runner.py` 顶部与 `__init__` 中注释已与上述行为对齐（2026-04-30）。
  2. 在 `ARCHITECTURE.md` / `config.toml` 中明确：**默认关闭**、开启方式与 JSONL 落盘路径。
  3. 多进程 / 多副本下 EventBus 文件写入一致性依赖部署验证（见 `event_bus` 实现与运维约定）。

### 2.2 LLM 提供商与「Claude 流式未实现」的准确含义

- **注册表**: `llm/providers.py` 中 `PROVIDER_ADAPTERS` 包含 **OpenAI、Claude、GLM、DeepSeek、Qwen、vLLM、Ollama**；`llm/base.py` 枚举另有 **GEMINI、CUSTOM**，未映射专属类时走 **`OpenAICompatibleAdapter`**（任意 OpenAI 兼容端点 + `model_name`）。
- **`NotImplementedError("Claude 流式输出暂未实现")`** 位置：`ClaudeAdapter.chat_stream`（`providers.py`）。含义是：**仅 Anthropic Messages API 这条适配器的 `chat_stream` 未实现**，不是「只缺某个 Claude 模型名」。
- **非 Claude 提供商**：`OpenAICompatibleAdapter`（含 GLM / DeepSeek / Qwen / OpenAI / 多数兼容网关）已实现 **`chat_stream`**（见 `openai_compatible.py`）。
- **Claude 非流式**：`ClaudeAdapter.chat`（同步单次 `messages` 请求）已实现；`MODEL_MAPPING` 覆盖文档字符串所列别名，亦可传入 API 接受的 `model_name` 字符串。
- **与架构图**: 图中列出 OpenAI、智谱等；代码中另有 Claude、DeepSeek、Qwen、本地 vLLM/Ollama 等——**以 `LLMProvider` + 适配器为准**做对外说明。

### 2.3 硬件沙箱（Firecracker）功能残缺 — **仍有效**

- **代码位置**: `src/smartclaw/sandbox/firecracker.py`（快照创建/恢复、暂停/恢复等 TODO）。
- **建议**: 补齐 Firecracker 官方 **[snapshotting](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting.md)** 相关能力，或在上层明确 **NotImplementedError + 降级**，避免静默失败。

---

## 🐞 3. 运行时稳定性与监控 (Stability & Monitoring)

### 3.1 监控探针 — **仍有效**

- **代码位置**: `src/smartclaw/server_monitoring.py` 等处仍存在健康检查 **TODO**（真实 DB / LLM 探测）。
- **建议**: 实现轻量探针或依赖下游超时，避免 K8s 就绪探针永久「假健康」。

### 3.2 异常吞没 — **仍建议治理**

- 全局 `except Exception: pass` 等模式仍建议按初版方案改为可观测告警。

---

## 📊 4. 工程质量与测试 (Engineering Quality)

- 初版覆盖率数据可能已变化；**以当前 CI**（如 `.github/workflows/execution-regression.yml`）与本地 `pytest` 为准定期更新。
- `ruff` 等静态检查债务建议分项消减。

### 行动建议 (Action Plan) — 2026-04 复核版

1. **P0**: 若生产暴露 **飞书 HTTP Webhook**：实现 **官方请求签名校验**（Lark 头）；保留或加强自建 Token / 解密 / 防重放。
2. **P0**: 沙箱与多租户边界：按部署场景复查隔离与路径安全。
3. **P1**: 健康检查与 LLM/存储真实探测。
4. **P1**: Firecracker 快照/生命周期 **实现或显式降级**。
5. **P2**: Claude **`chat_stream`**（若产品需要流式 UX）；否则文档标明「Claude 仅非流式」。
6. **P2**: 提升核心链路自动化测试覆盖率。

### 能否完整正常运转？

单路径（配置正确、Happy Path、不一定使用 Webhook 签名全开或 Firecracker 快照）可调通；**直接作为公网生产** 仍需逐项关闭上述 **P0/P1**。本报告不替代正式红队与合规评估。
