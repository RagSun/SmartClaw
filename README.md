# SmartClaw

生产级企业 AI Agent 平台

## 📋 目录

- [概述](#概述)
- [环境要求](#环境要求)
- [安装部署](#安装部署)
- [配置说明](#配置说明)
- [启动服务](#启动服务)
- [多租户管理](#多租户管理)
- [命令参考](#命令参考)
- [项目结构](#项目结构)
- [开发](#开发)
- [生产部署](#生产部署)
- [常见问题](#常见问题)
- [架构说明](#架构说明)

---

## 概述

**SmartClaw** 是一个生产级的企业 AI Agent 平台，具备以下核心特性：

- **🔒 硬件级隔离**：每个 Agent/会话运行在独立的 microVM（Firecracker）中
- **📱 双渠道支持**：同时支持飞书和企业微信
- **⚡ CLI 驱动**：所有操作通过命令行完成，简单高效
- **🚀 高可用架构**：预热池、快照恢复、资源限流等企业级特性
- **🔧 灵活扩展**：支持自定义工具和插件

---

## 💻 环境要求

### 必需环境

- **操作系统**：Linux（推荐 Ubuntu 22.04+）
- **Python 版本**：Python 3.12+
- **内存**：至少 2GB RAM
- **磁盘空间**：至少 5GB 可用空间

### 可选环境

- **Docker**：用于容器隔离（推荐）
- **KVM 支持**：用于 Firecracker microVM（高级功能）

---

## 🚀 安装部署

[uv](https://docs.astral.sh/uv/) 是极速的 Python 包管理器，推荐用于 SmartClaw 环境管理。

### 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

### 方法一：使用 uv（推荐）

```bash
# 使用 uv 安装项目（自动创建虚拟环境）
cd smartclaw
uv sync

# 或手动创建虚拟环境再安装
uv venv
source .venv/bin/activate
uv pip install -e .

# 安装开发依赖（可选）
uv pip install -e ".[dev]"
```

### 方法二：使用虚拟环境 (venv)

```bash
cd smartclaw
python3.12 -m venv venv
source venv/bin/activate
pip install uv
uv pip install -e .
```

### 方法三：使用 Conda

```bash
conda create -n smartclaw python=3.12 -y
conda activate smartclaw
pip install uv
uv pip install -e .
```

### 验证安装

```bash
# 激活虚拟环境
source .venv/bin/activate

# 设置环境变量
export SMARTCLAW_HOME="$HOME/.smartclaw"
export SMARTCLAW_AGENT_WORKSPACE_BASE="$HOME/.smartclaw/workspace"
source ~/.bashrc

# 查看版本
smartclaw --version

# 环境诊断
smartclaw doctor
```

---

## ⚙️ 配置说明

SmartClaw 使用 **.env 文件** 作为项目环境配置入口（12-factor 原则）。
启动时 `.env` 会被自动加载到环境变量，并覆盖 `config.toml` 中的对应字段。

> `.env` 文件在 `smartclaw` CLI 启动时自动加载（通过 python-dotenv）。
> 加载顺序：`$SMARTCLAW_DOTENV_PATH` → 项目根目录 `.env` → 当前目录 `.env`。
> **配置优先级**：`.env` 环境变量 > `config.toml` > Pydantic 模型默认值。
> 即 `.env` 中设置的值会覆盖 TOML 文件中同名配置项。

> **.env 与 config.toml 的覆盖关系**：
>
> **config.toml 搜索顺序**（按优先级从高到低，找到第一个即停止）：
>
> | 优先级 | 路径 | 说明 |
> |:---:|------|------|
> | 1 | `/opt/smartclaw/config/config.toml` | 系统安装路径（`setup.sh` 写入位置） |
> | 2 | `~/.smartclaw/config/config.toml` | 用户安装路径（`config set` 默认写入） |
> | 3 | `<项目根>/config/config.toml` | 开发模式 |
> | 4 | `./config.toml` | 当前工作目录 |
>
> **.env 文件搜索顺序**（按优先级从高到低，找到第一个即停止）：
>
> | 优先级 | 路径 | 说明 |
> |:---:|------|------|
> | 1 | `$SMARTCLAW_DOTENV_PATH` | 环境变量显式指定 |
> | 2 | `<项目根>/.env` | 项目根目录 |
> | 3 | `./.env` | 当前工作目录 |
>
> **最终生效优先级**（高到低）：
> ```
> 专用环境变量 (SMARTCLAW_GOVERNANCE_*, SMARTCLAW_MEMORY_* 等)
>   > .env 中的变量 (LLM_API_KEY, FEISHU_APP_ID 等)
>     > config.toml（上述 4 个路径中第一个存在的文件）
>       > Pydantic 模型默认值
> ```
>
> **实践建议**：先用 `setup.sh` 生成 `/opt/smartclaw/config/config.toml` 模板（需 root），再用 `.env` 覆盖敏感凭证（API Key 等，无需 root）。这样敏感信息不会写入 TOML 文件，便于权限管理和版本控制。

### 1. 创建 .env 配置文件

```bash
# 复制示例配置文件
cp .env.example .env

# 编辑配置文件，填写实际密钥
vi .env
```

### 2. 必需配置项

#### LLM 配置（必需）

```bash
# deepseek AI（示例）
LLM_PROVIDER=deepseek
LLM_API_KEY=your_api_key_here
LLM_MODEL=deepseek-v4-flash
LLM_BASE_URL=https://api.deepseek.com/v1

# 或 OpenAI
LLM_PROVIDER=openai
LLM_API_KEY=sk-your_openai_key_here
LLM_MODEL=gpt-4
LLM_BASE_URL=https://api.openai.com/v1
```

#### 飞书配置（使用飞书时必需）

```bash
# 获取方式：https://open.feishu.cn/app
FEISHU_APP_ID=cli_your_app_id
FEISHU_APP_SECRET=your_app_secret
```

#### 企业微信配置（使用企业微信时必需）

```bash
# 获取方式：https://work.weixin.qq.com/
WECOM_CORP_ID=your_corp_id
WECOM_AGENT_ID=your_agent_id
WECOM_APP_SECRET=your_app_secret
```

### 3. 验证配置

```bash
# 查看当前生效配置（含 .env 覆盖后的结果）
smartclaw config show

# 测试配置
smartclaw doctor
```

---

## 🎬 启动服务

### 开发模式

```bash
# 初始化项目
smartclaw init

# 单进程启动
smartclaw start

# 自动重载（开发时推荐）
smartclaw start --reload
```

### 生产模式

#### 1. 快速配置TOML（单机生产配置脚本）

```bash
cp setup.sh.example setup.sh
vi setup.sh                    # 编辑配置参数
sudo bash setup.sh             # 写入 /opt/smartclaw/config/config.toml
source ~/.bashrc
sudo chown -R $USER:$USER /opt/smartclaw  # 增加用户权限
```

#### 2. 启动生产服务
```bash
# 飞书渠道，多进程
smartclaw start --feishu --multi-process

# 企业微信渠道，多进程
smartclaw start --no-feishu --multi-process

# 自定义端口和工作进程数
smartclaw start --port 8080 --workers 4

# 后台运行
nohup smartclaw start --feishu --multi-process > /dev/null 2>&1 &
```

### 验证服务

```bash
smartclaw status
curl http://localhost:8000/health
tail -f /opt/smartclaw/logs/smartclaw.log
```

### 停止服务

```bash
smartclaw stop        # 正常停止
smartclaw stop -f     # 强制终止
```

---

## 🏢 多租户管理

SmartClaw 支持在**同一个实例**内为不同部门/团队提供完全隔离的 Agent 空间。

### 概念

- **租户（Tenant）**：SmartClaw 内部的逻辑隔离单元，拥有独立的 Agent 集合、角色权限、配额限制
- **飞书应用（Feishu App）**：飞书开放平台创建的机器人应用，有唯一的 `app_id`
- **关系**：多个飞书应用可指向同一租户，但一个 `app_id` 只能归属一个租户

```
飞书应用 A (cli_aaa) ──┐
                        ├──→ 租户 tenant_A  ──→ Agent tenant_A/bot_dept_a
飞书应用 B (cli_bbb) ──┘                       Agent tenant_A/bot_dept_b

飞书应用 C (cli_ccc) ────→ 租户 tenant_B  ──→ Agent tenant_B/bot_dept_c
```

系统收到飞书消息时，从消息中提取 `app_id`，查找租户映射表得到 `tenant_id`，然后将消息路由到该租户下的 Agent。

**路由优先级**（由高到低）：

1. **Tenancy Registry**（SQLite 持久化，运行时增删改查，无需重启）
2. **`auth.tenant_by_app_id`**（config.toml 静态映射）
3. **`auth.tenant_default`**（全局默认值，默认 `"default"`）

### 配置租户路由

#### 方式一：config.toml 静态映射

```bash
# 将飞书应用 cli_aaa 的消息路由到租户 tenant_A
smartclaw config set auth.tenant_by_app_id.cli_aaa tenant_A

# 将飞书应用 cli_bbb 的消息路由到租户 tenant_B
smartclaw config set auth.tenant_by_app_id.cli_bbb tenant_B
```

这会生成：

```toml
[auth.tenant_by_app_id]
cli_aaa = "tenant_A"
cli_bbb = "tenant_B"
```

#### 方式二：Tenancy Registry（推荐，API 层）

Tenancy Registry 是租户管理的持久化后端（SQLite/Redis），支持完整的租户生命周期管理（开通/停用/配额/app_id 路由）。该模块通过 `/api/tenants` HTTP API 和程序接口调用，修改后实时生效无需重启。

> Tenancy Registry 的 CLI 命令将在后续版本中提供。当前可通过 `smartclaw config set` 配置 `tenant_by_app_id` 静态映射，或通过 HTTP API 直接操作租户注册表。

### 租户级 Agent 管理

所有 Agent 相关命令均支持 `--tenant`（或 `-t`）参数：

```bash
# 在 tenant_A 下创建 Agent
smartclaw agent add bot_dept --tenant tenant_A --channel feishu -i cli_aaa -s <secret>

# 查看所有租户的 Agent
smartclaw agent list

# 查看 Agent 详细信息
smartclaw agent list -v

# 删除 tenant_A 下的 Agent
smartclaw agent delete bot_dept --tenant tenant_A

# 为租户 Agent 设置 LLM
smartclaw agent set-llm bot_dept --tenant tenant_A -m glm-5 -k <key>
```

Agent 数据目录结构（多租户）：
```
~/.smartclaw/data/agents/
├── default/              # 默认租户
│   └── default/agent.json
├── tenant_A/             # 租户 tenant_A
│   └── bot_dept/agent.json
└── tenant_B/             # 租户 tenant_B
    └── bot_dept/agent.json
```

### 租户级角色管理

每个租户有独立的角色映射表，通过 `--tenant` 指定：

```bash
# 设置用户在 tenant_A 下的角色
smartclaw auth roles set ou_xxx --roles admin,developer --tenant tenant_A

# 授予单个角色
smartclaw auth roles grant ou_xxx platform_admin --tenant tenant_A

# 移除角色
smartclaw auth roles revoke ou_xxx developer --tenant tenant_A

# 列出 tenant_A 的所有角色映射
smartclaw auth roles list --tenant tenant_A

# 查看某用户在 tenant_A 下的角色
smartclaw auth whoami ou_xxx --tenant tenant_A
```

### 租户级诊断与调试

```bash
# 针对特定租户的 Agent 做 LLM 探活
smartclaw doctor --tenant tenant_A --agent bot_dept

# 对特定租户的 Agent 发起测试请求
smartclaw llm-test "你好" --tenant tenant_A --agent bot_dept

# 查看租户级会话
smartclaw session-list --tenant tenant_A
```

### 验证

```bash
# 查看所有 Agent（含租户信息）
smartclaw agent list

# 查看详细信息（含租户路径）
smartclaw agent list -v

# 查看配置中的租户映射
smartclaw config show | grep -A 10 tenant_by_app_id
```

### 相关配置项

| 配置键 | 说明 | 默认值 |
|--------|------|--------|
| `auth.tenant_by_app_id` | 飞书 app_id → tenant_id 静态映射 | `{}` |
| `auth.tenant_default` | 未匹配时的兜底租户 | `"default"` |
| `auth.tenant_trust_header` | 是否校验 `X-SmartClaw-Tenant-Id` 请求头 | `false` |
| `auth.feishu_open_id_roles_by_tenant` | 租户 → open_id → 角色列表 | `{}` |
| `auth.tenant_integration_env` | 租户级集成环境变量 | `{}` |
| `governance.rate_per_min` | 租户级每分钟请求上限 | 继承全局默认 |
| `governance.daily_token_quota` | 租户级每日 Token 配额 | 继承全局默认 |

---

## 📚 命令参考

### 全局命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw --version` | 显示版本 | `smartclaw -v` |
| `smartclaw --help` | 显示帮助 | `smartclaw -h` |
| `smartclaw init` | 初始化项目 | `smartclaw init --force` |
| `smartclaw start` | 启动服务 | `smartclaw start --feishu` |
| `smartclaw stop` | 停止服务 | `smartclaw stop -f` |
| `smartclaw status` | 显示状态 | `smartclaw status` |
| `smartclaw doctor` | 环境诊断 | `smartclaw doctor` |

### 配置管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw config show` | 显示配置 | `smartclaw config show` |
| `smartclaw config set <key> <value>` | 设置配置项 | `smartclaw config set llm.api_key your_key` |
| `smartclaw config edit` | 编辑配置文件 | `smartclaw config edit` |

**配置示例：**

```bash
# 设置 LLM 配置
smartclaw config set llm.api_key your_api_key
smartclaw config set llm.model deepseek
smartclaw config set llm.base_url https://api.deepseek.com/v1

# 设置飞书配置
smartclaw config set channels.feishu.app_id your_app_id
smartclaw config set channels.feishu.app_secret your_app_secret

# 设置服务器配置
smartclaw config set server.host 0.0.0.0
smartclaw config set server.port 8000
```

### Agent 管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw agent add <name>` | 创建 Agent（飞书） | `smartclaw agent add my-bot --channel feishu -i cli_xxx -s xxx` |
| `smartclaw agent add <name>` | 创建 Agent（企业微信） | `smartclaw agent add my-bot --channel wecom` |
| `smartclaw agent list` | 列出 Agent | `smartclaw agent list` |
| `smartclaw agent delete <name>` | 删除 Agent | `smartclaw agent delete my-bot` |

### 渠道配置命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw channel setup feishu` | 配置飞书 | `smartclaw channel setup feishu` |
| `smartclaw channel setup wecom` | 配置企业微信 | `smartclaw channel setup wecom` |

### Auth / 租户管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw auth whoami <open_id>` | 查看用户角色 | `smartclaw auth whoami ou_xxx --tenant default` |
| `smartclaw auth current-user` | 查看当前/最近用户 | `smartclaw auth current-user -t default` |
| `smartclaw auth users recent` | 最近活跃用户 | `smartclaw auth users recent -n 10` |
| `smartclaw auth roles set` | 设置用户角色 | `smartclaw auth roles set ou_xxx --roles admin --tenant dept_A` |
| `smartclaw auth roles grant` | 授予单个角色 | `smartclaw auth roles grant ou_xxx developer --tenant dept_A` |
| `smartclaw auth roles revoke` | 移除单个角色 | `smartclaw auth roles revoke ou_xxx developer --tenant dept_A` |
| `smartclaw auth roles list` | 列出角色映射 | `smartclaw auth roles list --tenant default` |
| `smartclaw auth tool require` | 设置工具角色要求 | `smartclaw auth tool require agent_create --roles admin` |
| `smartclaw auth tool clear <tool>` | 删除工具角色要求 | `smartclaw auth tool clear agent_create` |
| `smartclaw auth tool list` | 列出工具角色要求 | `smartclaw auth tool list` |

**多租户配置**（飞书 app_id → tenant_id 映射）：

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw config set auth.tenant_by_app_id.<app_id>` | 设置飞书应用到租户的映射 | `smartclaw config set auth.tenant_by_app_id.cli_xxx tenant_A` |

> 收到飞书消息时，系统根据消息中的 `app_id` 查找 `auth.tenant_by_app_id`，将消息路由到对应租户的 Agent。

### Docker 管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw docker list` | 列出容器 | `smartclaw docker list` |
| `smartclaw docker stats` | 容器统计 | `smartclaw docker stats` |

### 监控统计命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw monitoring token-stats` | Token 统计 | `smartclaw monitoring token-stats --days 30` |
| `smartclaw monitoring daily-usage` | 每日使用量 | `smartclaw monitoring daily-usage --days 7` |
| `smartclaw monitoring clear-old` | 清理旧记录 | `smartclaw monitoring clear-old --days 90` |

**监控示例：**

```bash
# 查看 Token 使用统计
smartclaw monitoring token-stats

# 过滤特定 Agent
smartclaw monitoring token-stats --agent default

# 过滤特定提供商
smartclaw monitoring token-stats --provider deepseek

# 查看最近 30 天数据
smartclaw monitoring token-stats --days 30

# 查看每日使用量
smartclaw monitoring daily-usage --days 7

# 清理 90 天前的旧记录
smartclaw monitoring clear-old --days 90
```

### 工具管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw tool create <name>` | 创建工具模板 | `smartclaw tool create my-tool` |
| `smartclaw tool install <path>` | 安装工具 | `smartclaw tool install /path/to/tool` |
| `smartclaw tool list` | 列出已安装工具 | `smartclaw tool list` |
| `smartclaw tool info <name>` | 查看工具详情 | `smartclaw tool info my-tool` |
| `smartclaw tool enable <name>` | 启用工具 | `smartclaw tool enable my-tool` |
| `smartclaw tool disable <name>` | 禁用工具 | `smartclaw tool disable my-tool` |
| `smartclaw tool uninstall <name>` | 卸载工具 | `smartclaw tool uninstall my-tool` |

### MCP Server 管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw mcp add <name>` | 注册 MCP Server | `smartclaw mcp add factory --url http://localhost:8080/sse` |
| `smartclaw mcp list` | 列出所有 MCP Server | `smartclaw mcp list` |
| `smartclaw mcp test <name>` | 连接并列出远端 tools | `smartclaw mcp test factory` |
| `smartclaw mcp remove <name>` | 删除 MCP Server | `smartclaw mcp remove factory --yes` |
| `smartclaw mcp on` | 开启全局 MCP 总闸 | `smartclaw mcp on` |
| `smartclaw mcp off` | 关闭全局 MCP 总闸 | `smartclaw mcp off` |
| `smartclaw agent mcp list <agent>` | 查看 Agent 启用的 MCP | `smartclaw agent mcp list default` |
| `smartclaw agent mcp enable <agent> <server>` | 启用 MCP Server 到 Agent | `smartclaw agent mcp enable default factory` |
| `smartclaw agent mcp disable <agent> <server>` | 从 Agent 移除 MCP Server | `smartclaw agent mcp disable default factory` |

### Skills 管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw skills list` | 列出当前工作区 skills | `smartclaw skills list --eligible` |
| `smartclaw skills check` | 检查可用性与缺失依赖 | `smartclaw skills check` |
| `smartclaw skills info <name>` | 查看单个 skill 详情 | `smartclaw skills info my-skill` |
| `smartclaw skills create <name>` | 创建 skill 脚手架 | `smartclaw skills create my-skill -d "描述" --owner me --reviewer you` |
| `smartclaw skills validate` | 校验 schema/目录完整性 | `smartclaw skills validate` |
| `smartclaw skills lint` | 命名/描述 lint 规则 | `smartclaw skills lint` |
| `smartclaw skills test` | 运行 smoke test | `smartclaw skills test` |
| `smartclaw skills gate` | CI 质量门禁（validate + lint + test） | `smartclaw skills gate` |
| `smartclaw skills install <name>` | 安装 skill | `smartclaw skills install my-skill` |
| `smartclaw skills uninstall <name>` | 卸载 skill | `smartclaw skills uninstall my-skill` |
| `smartclaw skills repair <name>` | 修复 skill（卸载再安装） | `smartclaw skills repair my-skill` |
| `smartclaw skills enable <name>` | 启用 skill | `smartclaw skills enable my-skill` |
| `smartclaw skills disable <name>` | 禁用 skill | `smartclaw skills disable my-skill` |
| `smartclaw skills approve <name>` | 环境级审批 | `smartclaw skills approve my-skill --env staging` |
| `smartclaw skills promote <name>` | 发布到指定环境 | `smartclaw skills promote my-skill --to production` |
| `smartclaw skills rollback <name>` | 回滚到更低环境 | `smartclaw skills rollback my-skill --to staging` |
| `smartclaw skills registry` | 查看安装注册表 | `smartclaw skills registry` |
| `smartclaw skills events` | 查看生命周期事件 | `smartclaw skills events -n 30` |
| `smartclaw skills snapshot` | 刷新并输出 snapshot | `smartclaw skills snapshot` |
| `smartclaw skills watch` | 监听变化自动刷新 | `smartclaw skills watch` |
| `smartclaw skills releases` | 查看发布轨迹 | `smartclaw skills releases` |
| `smartclaw skills deprecate <name>` | 标记 skill 废弃 | `smartclaw skills deprecate my-skill --reason "已替换"` |
| `smartclaw skills approvals` | 查看审批记录 | `smartclaw skills approvals` |

### 服务生命周期 & 诊断命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw install` | 自动安装（创建目录/配置） | `smartclaw install --force` |
| `smartclaw restart` | 重启服务 | `smartclaw restart -f` |
| `smartclaw pid` | 查看进程 PID | `smartclaw pid` |
| `smartclaw log` | 查看服务日志 | `smartclaw log -n 100 -f` |
| `smartclaw task-status` | 子任务执行状态 | `smartclaw task-status --all` |
| `smartclaw llm-test <msg>` | 对 Agent 发起 LLM 探活请求 | `smartclaw llm-test "你好" -t default -a default` |

### Agent 扩展管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw agent validate` | 验证 Agent 配置完整性 | `smartclaw agent validate --fix` |
| `smartclaw agent update <name>` | 更新 Agent 配置 | `smartclaw agent update my-agent -d "新名" --enable` |
| `smartclaw agent scaffold <name>` | 为 Agent 生成工作区标准 MD | `smartclaw agent scaffold default -f` |
| `smartclaw agent permissions <name>` | 打印有效权限视图 | `smartclaw agent permissions default` |
| `smartclaw agent encrypt <name>` | 加密敏感信息 | `smartclaw agent encrypt --all` |
| `smartclaw agent clear-history <name>` | 清除 Agent 历史会话 | `smartclaw agent clear-history default` |
| `smartclaw agent compile <name>` | 编译 Markdown→JSON 配置 | `smartclaw agent compile --force` |
| `smartclaw agent set-llm <name>` | 设置 Agent LLM 模型 | `smartclaw agent set-llm default -m glm-5 -k <key> -p zhipu` |
| `smartclaw agent set-vision <name>` | 设置 Agent 视觉配置 | `smartclaw agent set-vision default --enable -m glm-4v` |
| `smartclaw agent set-policy <name>` | 设置 Agent 响应策略 | `smartclaw agent set-policy default --mode mention --scope both` |
| `smartclaw agent show-policy <name>` | 显示 Agent 响应策略 | `smartclaw agent show-policy default` |

### LangSmith 追踪配置

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw config langsmith status` | 查看追踪状态与密钥脱敏 | `smartclaw config langsmith status` |
| `smartclaw config langsmith enable` | 启用追踪 | `smartclaw config langsmith enable` |
| `smartclaw config langsmith disable` | 关闭追踪 | `smartclaw config langsmith disable` |
| `smartclaw config langsmith set-api-key` | 写入 API Key | `smartclaw config langsmith set-api-key lsv2_xxx` |
| `smartclaw config langsmith set-project` | 设置项目名 | `smartclaw config langsmith set-project my-project` |
| `smartclaw config langsmith set-endpoint` | 设置 endpoint | `smartclaw config langsmith set-endpoint https://api.smith.langchain.com` |
| `smartclaw config langsmith clear-api-key` | 删除 API Key | `smartclaw config langsmith clear-api-key --yes` |

### Shell 白名单管理（全局配置级）

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw config shell-allowlist list` | 列出全局白名单 | `smartclaw config shell-allowlist list` |
| `smartclaw config shell-allowlist add <pattern>` | 追加规则 | `smartclaw config shell-allowlist add ls` |
| `smartclaw config shell-allowlist remove <pattern>` | 删除规则 | `smartclaw config shell-allowlist remove ls` |
| `smartclaw config shell-allowlist clear` | 清空内联白名单 | `smartclaw config shell-allowlist clear --yes` |
| `smartclaw config shell-allowlist path-show` | 显示外挂白名单路径 | `smartclaw config shell-allowlist path-show` |
| `smartclaw config shell-allowlist path-set <path>` | 设置外挂白名单路径 | `smartclaw config shell-allowlist path-set /etc/smartclaw/allowlist.txt` |
| `smartclaw config shell-allowlist path-clear` | 清空外挂白名单路径 | `smartclaw config shell-allowlist path-clear` |
| `smartclaw config shell-allowlist import-json` | 从 JSON 数组批量导入 | `smartclaw config shell-allowlist import-json '["ls","git"]'` |

### Agent 级 Shell 白名单

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw agent shell-allowlist list <agent>` | 列出 Agent 白名单 | `smartclaw agent shell-allowlist list default` |
| `smartclaw agent shell-allowlist add <agent> <pattern>` | 追加规则 | `smartclaw agent shell-allowlist add default ls` |
| `smartclaw agent shell-allowlist remove <agent> <pattern>` | 删除规则 | `smartclaw agent shell-allowlist remove default ls` |
| `smartclaw agent shell-allowlist clear <agent>` | 清空白名单 | `smartclaw agent shell-allowlist clear default --yes` |
| `smartclaw agent shell-allowlist include-workspace <agent>` | 控制是否读取工作区 SHELL_ALLOWLIST.txt | `smartclaw agent shell-allowlist include-workspace default --on` |
| `smartclaw agent shell-allowlist import-json <agent>` | 从 JSON 批量导入 | `smartclaw agent shell-allowlist import-json default '["ls"]'` |

### 渠道扩展命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw channel add-feishu` | 写入飞书凭证到 config.toml | `smartclaw channel add-feishu -i cli_xxx -s <secret>` |
| `smartclaw channel bind-feishu <agent>` | 一键绑定飞书：config.toml + agent.json | `smartclaw channel bind-feishu default -i cli_xxx -s <secret>` |

### Agent 绑定管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw bindings bind-user <user_id>` | 绑定用户到 Agent | `smartclaw bindings bind-user ou_xxx -a default` |
| `smartclaw bindings bind-group <chat_id>` | 绑定群聊到 Agent | `smartclaw bindings bind-group oc_xxx -a default` |
| `smartclaw bindings unbind-user <user_id>` | 解绑用户 | `smartclaw bindings unbind-user ou_xxx` |
| `smartclaw bindings unbind-group <chat_id>` | 解绑群聊 | `smartclaw bindings unbind-group oc_xxx` |
| `smartclaw bindings set-default <agent>` | 设置默认 Agent | `smartclaw bindings set-default default` |
| `smartclaw bindings list` | 列出所有绑定 | `smartclaw bindings list` |
| `smartclaw bindings clear` | 清空所有绑定 | `smartclaw bindings clear --yes` |

---



### 常见问题

**Q: 如何实现多部门 Agent 隔离？**

为每个部门创建一个租户，将各自的飞书应用 `app_id` 映射到对应租户，Agent 数据、会话、角色均按租户隔离。

**Q: `tenant_by_app_id` 和 Tenancy Registry 有什么区别？**

| | tenant_by_app_id | Tenancy Registry |
|------|------|------|
| 存储 | config.toml | SQLite/Redis |
| 修改生效 | 需重启服务 | 实时生效 |
| 租户生命周期 | 无 | active/suspended |
| 配额管理 | 无 | 支持 |
| 路由优先级 | 第二优先 | 第一优先 |

> **推荐**：生产环境使用 Tenancy Registry，可以动态开通/停用租户，无需重启服务。

---

## 📁 项目结构

```
smartclaw/
├── src/smartclaw/        # 源代码
│   ├── cli.py            # CLI 入口
│   ├── server.py         # FastAPI 服务
│   ├── console.py        # 控制台输出
│   ├── interfaces.py     # 接口定义
│   └── config/           # 配置模块
├── tests/                # 测试
├── standards/            # 开发规范
├── config/               # 配置文件
├── logs/                 # 日志
├── data/                 # 数据
└── sandboxes/            # 沙箱实例
```

规范与审计文档索引见 [standards/README-v1.0.md](standards/README-v1.0.md)（与当前代码同步说明）。

---

## 🔧 开发

### 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) 包管理器
- KVM 支持（可选，用于 microVM 隔离）
- Firecracker（可选，用于 microVM）

### 开发安装

```bash
cd smartclaw
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# 运行测试
pytest

# 代码检查
ruff check src/
mypy src/
```

### Docker 开发环境

```bash
docker compose up -d --build
curl http://localhost:8000/api/monitoring/health
```

---

## 🏭 生产部署

### systemd 服务

```bash
sudo cp deploy/smartclaw.service /etc/systemd/system/
```

编辑 `/etc/systemd/system/smartclaw.service`：

```ini
[Unit]
Description=SmartClaw AI Agent Platform
After=network.target

[Service]
Type=simple
User=smartclaw
WorkingDirectory=/opt/smartclaw
Environment="PATH=/opt/smartclaw/venv/bin"
ExecStart=/opt/smartclaw/venv/bin/smartclaw start --feishu --multi-process --workers 4
Restart=always
RestartSec=10
MemoryMax=2G
CPUQuota=200%
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl start smartclaw
sudo systemctl enable smartclaw
sudo journalctl -u smartclaw -f
```

### Docker 部署

```bash
docker build -t smartclaw:latest .

docker run -d \
  --name smartclaw \
  -p 8000:8000 \
  -v /opt/smartclaw/config:/app/config \
  -v /opt/smartclaw/data:/app/data \
  -v /opt/smartclaw/logs:/app/logs \
  smartclaw:latest
```

### Nginx 反向代理

```nginx
server {
    listen 80;
    server_name your-domain.com;

    access_log /var/log/nginx/smartclaw_access.log;
    error_log /var/log/nginx/smartclaw_error.log;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket 支持
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/smartclaw /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### 日志轮转

创建 `/etc/logrotate.d/smartclaw`：

```
/opt/smartclaw/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0640 smartclaw smartclaw
}
```

### 备份与恢复

```bash
# 备份
tar czf smartclaw-backup-$(date +%Y%m%d).tar.gz \
  /opt/smartclaw/config \
  /opt/smartclaw/data

# 恢复
tar xzf smartclaw-backup-20240101.tar.gz -C /
sudo systemctl restart smartclaw
```

---

## 🔍 常见问题

### 1. 环境问题

**Q: uv 命令不存在？**

```bash
# 解决方法：安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc

# 或使用 pip 安装
pip install uv
```

**Q: Python 版本不兼容？**

```bash
# 解决方法：使用正确的 Python 版本
conda create -n smartclaw python=3.12 -y
conda activate smartclaw

# 或使用 uv 创建虚拟环境
uv venv --python 3.12
source .venv/bin/activate
```

### 2. 配置问题

**Q: 配置文件找不到？**

```bash
# 解决方法：初始化配置
smartclaw init --force

# 或手动创建配置目录
mkdir -p /opt/smartclaw/config
cp config/config.toml /opt/smartclaw/config/
```

**Q: API Key 无效？**

```bash
# 解决方法：检查配置
smartclaw config show

# 重新设置 API Key
smartclaw config set llm.api_key your_correct_api_key
```

### 3. 启动问题

**Q: 端口被占用？**

```bash
# 解决方法：检查端口占用
netstat -tlnp | grep 8000

# 或使用其他端口
smartclaw start --port 8080
```

**Q: 权限不足？（`/opt/smartclaw/data` Permission denied）**

以普通用户运行 `smartclaw agent add` 等命令时报 `PermissionError: [Errno 13] Permission denied: '/opt/smartclaw/data'`。原因是安装根目录 `/opt/smartclaw` 默认由 root 拥有，而工作区、数据库、日志等路径均指向其下。将安装根归属改为当前用户即可（推荐，无需改动配置）：

```bash
sudo mkdir -p /opt/smartclaw/{config,data,logs,sandboxes,run,tmp}
sudo chown -R $USER:$USER /opt/smartclaw
```

> 替代方案：不便使用 sudo 时，可把 `~/.bashrc` 的 `SMARTCLAW_HOME` 改为 `~/.smartclaw`，并将 `.env` 中 `DATABASE_PATH`/`LOG_FILE`/`DATA_DIR`/`LOG_DIR` 一并指向 `~/.smartclaw/...`，改为纯用户目录部署。

### 4. 渠道问题

**Q: 飞书连接失败？**

```bash
# 检查飞书配置
smartclaw config show | grep feishu

# 验证 App ID 和 Secret
# 确保在飞书开放平台配置了正确的回调地址
```

**Q: 企业微信消息发送失败？**

```bash
# 检查企业微信配置
smartclaw config show | grep wecom

# 验证 CorpID、AgentID 和 Secret
# 确保应用已启用且权限配置正确
```

### 5. 性能问题

**Q: 响应速度慢？**

```bash
# 解决方法：增加工作进程数
smartclaw start --workers 8

# 或启用容器预热池
smartclaw config set sandbox.pool_enabled true
smartclaw config set sandbox.pool_size 5
```

**Q: 内存占用高？**

```bash
# 解决方法：限制容器资源
smartclaw config set sandbox.memory_mb 64
smartclaw config set sandbox.cpu_count 1
```

### 6. 日志问题

**Q: 如何查看详细日志？**

```bash
# 修改日志级别
smartclaw config set logging.level DEBUG

# 查看日志文件
tail -f /opt/smartclaw/logs/smartclaw.log

# 或查看系统日志
journalctl -u smartclaw -f
```

### 7. 租户问题

**Q: 多个飞书应用（多部门/多租户）如何隔离？**

为每个部门创建独立的飞书应用，通过 `tenant_by_app_id` 将各自的 `app_id` 映射到不同租户。Agent 数据、会话、角色均按租户隔离，彼此不可见。

**Q: `--tenant` 参数和 `tenant/name` 格式有什么区别？**

两者等价。`smartclaw agent add bot --tenant tenant_A` 等同于 `smartclaw agent add tenant_A/bot`。大多数支持 `--tenant` 的命令也接受 `tenant/name` 格式。

---

## 架构说明

与 **Agent = Model + Harness** 平台图一致的分层说明、入口顺序与配置约定见项目根目录 [ARCHITECTURE.md](ARCHITECTURE.md)。要点：

- **L1**：FastAPI / CLI、`PlatformAuthAdapter`（Bearer/JWT/Webhook 解密/防重放）、可选 **EventBus**（`[platform]`）。
- **L2**：渠道适配器、`AgentRouter`、`AuthPolicyManager`（租户 + `AgentResponsePolicy`）。
- **L3**：`AgentRunner`、`UnifiedExecutionEngine`、Planner、SkillRegistry、ToolRegistry。
- **L4–L6**：记忆/会话、沙箱、LLM/SQLite/监控。

生产飞书 HTTP 回调与长连接服务共用路由与 @ 语义；租户通过 `auth.tenant_by_app_id` 与可选请求头 `X-SmartClaw-Tenant-Id`（`tenant_trust_header=true` 时强校验）闭环。
