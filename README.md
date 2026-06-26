# SmartClaw

生产级企业 AI Agent 平台

## 📋 目录

- [概述](#概述)
- [环境要求](#环境要求)
- [安装部署](#安装部署)
- [配置说明](#配置说明)
- [启动服务](#启动服务)
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

- **操作系统**：Linux（推荐 Ubuntu 22.04+ 或 CentOS 7+）
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

# 查看版本
smartclaw --version

# 环境诊断
smartclaw doctor
```

---

## ⚙️ 配置说明

SmartClaw 使用 **.env 文件** 作为项目环境配置入口（12-factor 原则）。
启动时 `.env` 会被自动加载到环境变量，并覆盖 `config.toml` 中的对应字段。

> **配置优先级**：`.env` 环境变量 > `config.toml` > Pydantic 模型默认值

### 创建 .env 配置文件

```bash
cp .env.example .env
vi .env
```

### 必需配置项

#### LLM 配置（必需）

```bash
# deepseek AI
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

### 验证配置

```bash
smartclaw config show
smartclaw doctor
```

### config.toml 搜索顺序

| 优先级 | 路径 | 说明 |
|:---:|------|------|
| 1 | `/opt/smartclaw/config/config.toml` | 系统安装路径 |
| 2 | `~/.smartclaw/config/config.toml` | 用户安装路径 |
| 3 | `~/.smartclaw/config.toml` | 旧版扁平路径（兼容） |
| 4 | `<项目根>/config/config.toml` | 开发模式 |
| 5 | `./config.toml` | 当前工作目录 |

---

## 🎬 启动服务

### 初始化

```bash
# 设置运行时根目录（飞书/多进程启动必需）
export SMARTCLAW_HOME="$HOME/.smartclaw"

# 初始化项目
smartclaw init
```

### 开发模式

```bash
# 单进程启动
smartclaw start

# 自动重载（开发时推荐）
smartclaw start --reload
```

### 快速配置TOML（单机生产）

```bash
cp setup.sh.example setup.sh
vi setup.sh                    # 编辑配置参数
sudo bash setup.sh             # 写入 /opt/smartclaw/config/config.toml
source ~/.bashrc
```

### 生产模式

```bash
# 飞书渠道，多进程
smartclaw start --feishu --multi-process

# 企业微信渠道，多进程
smartclaw start --wecom --multi-process

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

---

## 📚 命令参考

### 全局命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw --version` | 显示版本 | `smartclaw -v` |
| `smartclaw --help` | 显示帮助 | `smartclaw -h` |
| `smartclaw init` | 初始化项目 | `smartclaw init --force` |
| `smartclaw start` | 启动服务 | `smartclaw start --feishu` |
| `smartclaw status` | 显示状态 | `smartclaw status` |
| `smartclaw doctor` | 环境诊断 | `smartclaw doctor` |

### 配置管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw config show` | 显示配置 | `smartclaw config show` |
| `smartclaw config set <key> <value>` | 设置配置项 | `smartclaw config set llm.api_key your_key` |
| `smartclaw config edit` | 编辑配置文件 | `smartclaw config edit` |

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

### Docker 管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw docker list` | 列出容器 | `smartclaw docker list` |
| `smartclaw docker stop <id>` | 停止容器 | `smartclaw docker stop <container_id>` |
| `smartclaw docker clean` | 清理容器 | `smartclaw docker clean --all` |

### 监控统计命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw monitoring token-stats` | Token 统计 | `smartclaw monitoring token-stats --days 30` |
| `smartclaw monitoring daily-usage` | 每日使用量 | `smartclaw monitoring daily-usage --days 7` |
| `smartclaw monitoring clear-old` | 清理旧记录 | `smartclaw monitoring clear-old --days 90` |

### 工具管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw tool create <name>` | 创建工具 | `smartclaw tool create my-tool` |
| `smartclaw tool install <path>` | 安装工具 | `smartclaw tool install /path/to/tool` |
| `smartclaw tool list` | 列出工具 | `smartclaw tool list` |
| `smartclaw tool uninstall <name>` | 卸载工具 | `smartclaw tool uninstall my-tool` |

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
git clone https://github.com/DaTingLi/smartclaw.git
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

### 环境问题

**Q: uv 命令不存在？**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
```

**Q: Python 版本不兼容？**

```bash
conda create -n smartclaw python=3.12 -y
conda activate smartclaw
# 或
uv venv --python 3.12
source .venv/bin/activate
```

### 配置问题

**Q: 配置文件找不到？**

```bash
smartclaw init --force
# 或手动创建
mkdir -p /opt/smartclaw/config
cp config/config.toml /opt/smartclaw/config/
```

### 启动问题

**Q: 端口被占用？**

```bash
netstat -tlnp | grep 8000
smartclaw start --port 8080
```

**Q: 权限不足？（`/opt/smartclaw/data` Permission denied）**

以普通用户运行 `smartclaw agent add` 等命令时报 `PermissionError: [Errno 13] Permission denied: '/opt/smartclaw/data'`。原因是安装根目录 `/opt/smartclaw` 默认由 root 拥有，而工作区、数据库、日志等路径均指向其下。将安装根归属改为当前用户即可（推荐，无需改动配置）：

```bash
sudo mkdir -p /opt/smartclaw/{config,data,logs,sandboxes,run,tmp}
sudo chown -R $USER:$USER /opt/smartclaw
```

> 替代方案：不便使用 sudo 时，可把 `~/.bashrc` 的 `SMARTCLAW_HOME` 改为 `~/.smartclaw`，并将 `.env` 中 `DATABASE_PATH`/`LOG_FILE`/`DATA_DIR`/`LOG_DIR` 一并指向 `~/.smartclaw/...`，改为纯用户目录部署。

### 性能问题

**Q: 响应速度慢？**

```bash
smartclaw start --workers 8
smartclaw config set sandbox.pool_enabled true
smartclaw config set sandbox.pool_size 5
```

---

## 架构说明

与 **Agent = Model + Harness** 平台图一致的分层说明、入口顺序与配置约定见项目根目录 [ARCHITECTURE.md](ARCHITECTURE.md)。要点：

- **L1**：FastAPI / CLI、`PlatformAuthAdapter`（Bearer/JWT/Webhook 解密/防重放）、可选 **EventBus**（`[platform]`）。
- **L2**：渠道适配器、`AgentRouter`、`AuthPolicyManager`（租户 + `AgentResponsePolicy`）。
- **L3**：`AgentRunner`、`UnifiedExecutionEngine`、Planner、SkillRegistry、ToolRegistry。
- **L4–L6**：记忆/会话、沙箱、LLM/SQLite/监控。

生产飞书 HTTP 回调与长连接服务共用路由与 @ 语义；租户通过 `auth.tenant_by_app_id` 与可选请求头 `X-SmartClaw-Tenant-Id`（`tenant_trust_header=true` 时强校验）闭环。
