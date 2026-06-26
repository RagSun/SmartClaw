# TOOLS — 工具与 Shell 约定

## 核心工具
- **exec**（Registry）与运行时 **execute**（DeepAgents 内置 Shell）为**同一 Shell 能力**：宿主白名单、Tool Policy、`agent.json` 对 `exec`/`execute` 的 allow/deny/enforce、以及 `auth` 中两名的角色要求（若都配置则分别满足）均适用；不得以为只有 `exec` 才过门禁
- **read_file / write_file**: 在工作区读写文件
- **agent_create / agent_update_feishu / agent_status**: 创建或查看租户 Agent，敏感凭证加密落盘且不回显；新 Feishu App 通常需要服务重启/重载后接收事件
- **reload_workspace_tools / workspace_tool_status**: 把 `tools/<name>/tool.json + handler.py` 注册为正式 ToolRegistry 工具，并查询注册状态
- **MCP 工具**: 由 `agent.json` 的 `mcp.servers` 启用后自动注册到 ToolRegistry，命名形如 `factory__get_line_status`
- **memory_search / memory_get / memory_write**: 检索、读取、写入记忆；涉及历史事项、偏好、既往决策或待办时先查记忆
- **create_feishu_doc / write_feishu_doc_content**: 创建飞书文档/表格/多维表格，并把 Markdown 正文写入已有 docx
- **spawn_subagent / subagent_status / subagent_cancel**: 多 Agent 场景下派生后台子任务、查询、取消；继承当前 tenant、飞书身份、角色与集成环境。`agent_id` 仅允许本租户已存在的 Agent，省略则当前 Agent
- **tool_audit**: 查看工具 metadata、参数 schema、风险等级、审计状态和当前租户角色策略
- **skill_audit**: 查看 skill 安全扫描、版本/owner/risk/test/tenant 状态，并按需读取完整 `SKILL.md`
- **Skills**: 见 `skills/` 下各 `SKILL.md`

## 使用原则
- **长驻服务**（Streamlit / Jupyter / uvicorn 等）：`execute` 常**自动后台**，返回单行 `[bg] id=bg_xxx | log=.smartclaw_bg/…`；优先用 `background_task` 查询 `status` / `log` / `list` / `kill`。未命中自动规则时在命令末加 **`&`** 或 **`nohup … &`**。
- 其它长耗时任务仍应用后台（nohup 或 &）
- 不确定路径时先列出目录再操作
- 与 `tools/SHELL_ALLOWLIST.txt` 及全局白名单一致时再使用受限命令
- 创建正式工具时必须放在 `tools/<tool>/tool.json + handler.py` 并调用 `reload_workspace_tools`；`skills/` 只放方法论和使用说明，不能宣称已注册为 Tool
- MCP 工具由平台配置发现和注册，不要在 prompt 中伪造；需要确认 MCP 工具名、参数或风险等级时使用 `tool_audit`
- 高风险工具（如 `exec`、`integration_http_request`、`agent_create`、`reload_workspace_tools`）需要管理员/开发者角色，且默认需要 `confirm=true` 二次确认；业务工具应从 ToolSecurityContext 读取 tenant，不接受模型传入 tenant

## 工具限制
- 不在策略禁止的目录外随意 destructive 删除
- 交互式 CLI（需终端）改为非交互参数或后台
