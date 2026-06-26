# SmartClaw `standards/` 索引（与代码同步说明）

本目录为项目级规范与审计材料。**与实现强相关的结论**以 `src/smartclaw` 当前代码为准，并配合仓库根目录 **`ARCHITECTURE.md`**（Harness 六层对照）阅读。

**最近与代码对齐复核**：2026-04-30（EventBus / 飞书 HTTP 多 app、扁平凭证、PRE-LAUNCH 修订等）。

---

## 必读（安全与上线）

| 文档 | 用途 |
|------|------|
| [PRE-LAUNCH-AUDIT-v1.0.md](PRE-LAUNCH-AUDIT-v1.0.md) | 上线前审计；含飞书 Lark 签名校验缺口、健康检查、Firecracker 等待办项（已作废「EventBus 虚假集成」旧结论） |

---

## 架构与模块

| 文档 | 用途 | 代码对照提示 |
|------|------|----------------|
| 根目录 [../ARCHITECTURE.md](../ARCHITECTURE.md) | **Harness L1–L6 与路径、入站顺序** | 优先于本目录旧版架构叙述 |
| [ARCHITECTURE-v1.0.md](ARCHITECTURE-v1.0.md) | DeepAgents / LangGraph 等技术栈说明 | 版本号以运行环境与 `pyproject.toml` 为准（`requires-python = ">=3.12"`） |
| [EVENT_BUS_ARCHITECTURE-v1.0.md](EVENT_BUS_ARCHITECTURE-v1.0.md) | Event Bus 能力与设计 | 文首含 **2026-04 与代码对齐**：`platform.event_bus_enabled`、`SubagentSpawner` 当前**不**注入 `event_bus` |
| [MULTI-PROCESS-ARCHITECTURE-v1.0.md](MULTI-PROCESS-ARCHITECTURE-v1.0.md) | 多进程 | 与飞书长连接进程模型相关时查阅 |
| [DOCKER-SANDBOX-ARCHITECTURE-v2.0.md](DOCKER-SANDBOX-ARCHITECTURE-v2.0.md) | Docker 沙箱 | 表格内「100%」等为该文档内检查项统计，不等同于全仓库功能完成度 |
| [SANDBOX-v1.0.md](SANDBOX-v1.0.md) | 沙箱通用约定 | 与 `sandbox/` 目录对照 |

---

## 状态与决策

| 文档 | 用途 |
|------|------|
| [PROJECT-STATUS-v1.0.md](PROJECT-STATUS-v1.0.md) | 完成度看板（已更新 EventBus / 渠道等表述） |
| [PROJECT-DECISION-LOG-v1.0.md](PROJECT-DECISION-LOG-v1.0.md) | 历史决策记录 |
| [EPIC-DYNAMIC-PROGRESS-CARD-PLAN.md](EPIC-DYNAMIC-PROGRESS-CARD-PLAN.md) | 飞书动态卡片预研；已标注与当前 EventBus 状态一致 |

---

## 工程规范

| 文档 | 用途 |
|------|------|
| [DEVELOPMENT-NORM-v1.0.md](DEVELOPMENT-NORM-v1.0.md) | 开发规范 |
| [CODE-STYLE-v1.0.md](CODE-STYLE-v1.0.md) | 代码风格 |
| [CODE-REVIEW-CHECKLIST-v1.0.md](CODE-REVIEW-CHECKLIST-v1.0.md) | Code Review |
| [MODULE-INTERFACE-STANDARD-v1.0.md](MODULE-INTERFACE-STANDARD-v1.0.md) | 模块接口 |
| [SPEC-TEMPLATE-v1.0.md](SPEC-TEMPLATE-v1.0.md) | 规格模板 |
| [CLI-COMMANDS-v1.0.md](CLI-COMMANDS-v1.0.md) | CLI 命令清单 |
| [AGENT-MANAGEMENT-v1.0.md](AGENT-MANAGEMENT-v1.0.md) | Agent 管理 |

---

## 专项

| 文档 | 用途 |
|------|------|
| [WRITE_TODOS-v1.0.md](WRITE_TODOS-v1.0.md) | write_todos 工具约定 |
| [VSOCK-TROUBLESHOOTING-v1.0.md](VSOCK-TROUBLESHOOTING-v1.0.md) | vsock 排障 |
| [KNOWLEDGE-CHECK-CHECKLIST-v1.0.md](KNOWLEDGE-CHECK-CHECKLIST-v1.0.md) | 知识检查清单 |
| [PROMPT-LIBRARY-v1.0.md](PROMPT-LIBRARY-v1.0.md) | 提示库 |
| [DEEPAGENTS_ANALYSIS-v1.0.md](DEEPAGENTS_ANALYSIS-v1.0.md) | DeepAgents 分析 |
| [SUBMODULE-CREATION-TEMPLATE-v1.0.md](SUBMODULE-CREATION-TEMPLATE-v1.0.md) | 子模块模板 |
| [EPIC-DYNAMIC-PROGRESS-CARD-PLAN.md](EPIC-DYNAMIC-PROGRESS-CARD-PLAN.md) | 飞书进度卡片 |

---

## 新会话建议携带上下文

1. 根目录 `ARCHITECTURE.md` + `standards/PRE-LAUNCH-AUDIT-v1.0.md`  
2. `standards/PROJECT-STATUS-v1.0.md`（看板）  
3. `standards/PROJECT-DECISION-LOG-v1.0.md`（最近若干条）  
4. `standards/CLI-COMMANDS-v1.0.md`（需要终端命令时）
