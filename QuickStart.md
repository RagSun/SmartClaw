# SmartClaw 快速启动指南

## 📋 目录

- [项目介绍](#项目介绍)
- [环境要求](#环境要求)
- [安装部署](#安装部署)
- [配置说明](#配置说明)
- [启动服务](#启动服务)
- [命令参考](#命令参考)
- [常见问题](#常见问题)
- [生产部署](#生产部署)

---

## 🎯 项目介绍

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
- **Python 版本**：Python 3.12
- **内存**：至少 2GB RAM
- **磁盘空间**：至少 5GB 可用空间

### 可选环境

- **Docker**：用于容器隔离（推荐）
- **KVM 支持**：用于 Firecracker microVM（高级功能）

---

## 🚀 安装部署

### 方法一：使用 uv（推荐）

[uv](https://docs.astral.sh/uv/) 是极速的 Python 包管理器，推荐用于 SmartClaw 环境管理。

#### 1. 安装 uv

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 或使用 pip 安装
pip install uv

# 重新加载 shell 配置
source ~/.bashrc
```

#### 2. 克隆项目并安装(推荐)

```bash
# 克隆仓库
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
# 进入项目目录
cd smartclaw

# 创建虚拟环境
python3.12 -m venv venv

# 激活虚拟环境
source venv/bin/activate

# 安装 uv（在虚拟环境内）
pip install uv

# 安装项目
uv pip install -e .
```

### 方法三：使用 Conda

```bash
# 创建 conda 环境
conda create -n smartclaw python=3.12 -y

# 激活环境
conda activate smartclaw

# 安装 uv
pip install uv

# 安装项目
uv pip install -e .

# 安装开发依赖（可选）
uv pip install pytest pytest-asyncio pytest-cov ruff black mypy
```

### 验证安装

```bash
# 激活虚拟环境
source .venv/bin/activate

# 设置环境变量（个人开发环境）
export SMARTCLAW_HOME="$HOME/.smartclaw"
export SMARTCLAW_AGENT_WORKSPACE_BASE="$HOME/.smartclaw/workspace"
source ~/.bashrc

# 查看版本
smartclaw --version

# 查看帮助
smartclaw --help

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

### 开发模式启动

```bash
# 单进程启动（开发调试）
smartclaw start

# 自动重载启动（开发时推荐）
smartclaw start --reload
```

### 生产模式启动

#### 1. 快速配置TOML（单机生产配置脚本）

```bash
# 复制示例shell脚本文件
cp setup.sh.example setup.sh

# 直接编辑 setup.sh 中的配置参数
vi setup.sh

# 运行配置脚本（将配置写入 /opt/smartclaw/config/config.toml，并自动写入 ~/.bashrc）
sudo bash setup.sh

# 使环境变量生效
source ~/.bashrc

# 增加用户权限
sudo chown -R $USER:$USER /opt/smartclaw
```

> **.env 与 config.toml 的覆盖关系**：
>
> **config.toml 搜索顺序**（按优先级从高到低，找到第一个即停止）：
>
> | 优先级 | 路径 | 说明 |
> |:---:|------|------|
> | 1 | `/opt/smartclaw/config/config.toml` | 系统安装路径（`setup.sh` 写入位置） |
> | 2 | `~/.smartclaw/config/config.toml` | 用户安装路径（`config set` 默认写入） |
> | 3 | `~/.smartclaw/config.toml` | 旧版扁平路径（兼容） |
> | 4 | `<项目根>/config/config.toml` | 开发模式 |
> | 5 | `./config.toml` | 当前工作目录 |
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
>     > config.toml（上述 5 个路径中第一个存在的文件）
>       > Pydantic 模型默认值
> ```
>
> **实践建议**：先用 `setup.sh` 生成 `/opt/smartclaw/config/config.toml` 模板（需 root），再用 `.env` 覆盖敏感凭证（API Key 等，无需 root）。这样敏感信息不会写入 TOML 文件，便于权限管理和版本控制。

#### 2. 启动生产服务

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

### 停止服务

```bash
smartclaw stop        # 正常停止
smartclaw stop -f     # 强制终止
```

### 验证服务状态

```bash
# 检查服务状态
smartclaw status

# 检查健康状态
curl http://localhost:8000/health

# 查看日志
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
| `smartclaw stop` | 停止服务 | `smartclaw stop -f` |
| `smartclaw status` | 显示状态 | `smartclaw status` |
| `smartclaw doctor` | 环境诊断 | `smartclaw doctor` |

### 配置管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw config show` | 显示配置 | `smartclaw config show` |
| `smartclaw config set` | 设置配置项 | `smartclaw config set llm.api_key your_key` |
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
| `smartclaw agent add` | 创建 Agent（飞书） | `smartclaw agent add my-agent --channel feishu -i cli_xxx -s xxx` |
| `smartclaw agent add` | 创建 Agent（企业微信） | `smartclaw agent add my-wecom --channel wecom` |
| `smartclaw agent list` | 列出 Agent | `smartclaw agent list` |
| `smartclaw agent delete` | 删除 Agent | `smartclaw agent delete my-agent` |

### 渠道配置命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw channel setup feishu` | 配置飞书 | `smartclaw channel setup feishu` |
| `smartclaw channel setup wecom` | 配置企业微信 | `smartclaw channel setup wecom` |

### Docker 管理命令

| 命令 | 说明 | 示例 |
|------|------|------|
| `smartclaw docker list` | 列出容器 | `smartclaw docker list` |
| `smartclaw docker stats` | 容器统计 | `smartclaw docker stats` |
| `smartclaw docker inspect` | 容器详情 | `smartclaw docker inspect <project_name>` |
| `smartclaw docker logs` | 容器日志 | `smartclaw docker logs <project_name> -n 50` |
| `smartclaw docker cleanup` | 清理空闲容器 | `smartclaw docker cleanup --force` |

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
| `smartclaw tool create` | 创建工具 | `smartclaw tool create my-tool` |
| `smartclaw tool install` | 安装工具 | `smartclaw tool install /path/to/tool` |
| `smartclaw tool list` | 列出工具 | `smartclaw tool list` |
| `smartclaw tool uninstall` | 卸载工具 | `smartclaw tool uninstall my-tool` |

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

**Q: 权限不足？**

```bash
# 解决方法：使用 sudo 或调整权限
sudo smartclaw start

# 或创建必要的目录
sudo mkdir -p /opt/smartclaw/{config,data,logs,sandboxes,workspace}
sudo chown -R $USER:$USER /opt/smartclaw
```

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

---

## 🏭 生产部署

### systemd 服务部署

#### 1. 创建服务文件

```bash
# 复制服务文件
sudo cp deploy/smartclaw.service /etc/systemd/system/
```

#### 2. 配置服务

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

# 资源限制
MemoryMax=2G
CPUQuota=200%
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

#### 3. 启动服务

```bash
# 重新加载 systemd
sudo systemctl daemon-reload

# 启动服务
sudo systemctl start smartclaw

# 查看状态
sudo systemctl status smartclaw

# 开机自启
sudo systemctl enable smartclaw

# 查看日志
sudo journalctl -u smartclaw -f
```

### Docker 部署

#### 1. 构建镜像

```bash
# 构建镜像
docker build -t smartclaw:latest .

# 或使用 docker-compose
docker compose up -d
```

#### 2. 运行容器

```bash
# 运行容器
docker run -d \
  --name smartclaw \
  -p 8000:8000 \
  -v /opt/smartclaw/config:/app/config \
  -v /opt/smartclaw/data:/app/data \
  -v /opt/smartclaw/logs:/app/logs \
  smartclaw:latest
```

### Nginx 反向代理

#### 1. 安装 Nginx

```bash
sudo apt-get update
sudo apt-get install nginx -y
```

#### 2. 配置反向代理

创建 `/etc/nginx/sites-available/smartclaw`：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # 日志
    access_log /var/log/nginx/smartclaw_access.log;
    error_log /var/log/nginx/smartclaw_error.log;

    # 代理配置
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

        # 超时设置
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
```

#### 3. 启用配置

```bash
# 创建软链接
sudo ln -s /etc/nginx/sites-available/smartclaw /etc/nginx/sites-enabled/

# 测试配置
sudo nginx -t

# 重启 Nginx
sudo systemctl restart nginx
```

### 监控和日志

#### 1. 日志轮转

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

#### 2. 监控 API

```bash
# 健康检查
curl http://localhost:8000/health

# Token 使用统计
curl http://localhost:8000/api/monitoring/token-stats

# 每日使用量
curl http://localhost:8000/api/monitoring/daily-usage

# Agent 使用统计
curl http://localhost:8000/api/monitoring/agent-usage/default
```

### 备份和恢复

#### 备份

```bash
# 备份配置和数据
tar czf smartclaw-backup-$(date +%Y%m%d).tar.gz \
  /opt/smartclaw/config \
  /opt/smartclaw/data

# 或使用脚本
./scripts/backup.sh
```

#### 恢复

```bash
# 恢复备份
tar xzf smartclaw-backup-20240101.tar.gz -C /

# 重启服务
sudo systemctl restart smartclaw
```

---

## 🔧 常见问题

### 权限不足：`/opt/smartclaw/data` Permission denied

**现象**：以普通用户运行 `smartclaw agent add ...` 时报错：

```
PermissionError: [Errno 13] Permission denied: '/opt/smartclaw/data'
```

Agent 配置目录会自动回退到 `~/.smartclaw/data/agents/`，但工作区、数据库、日志等路径仍指向 `/opt/smartclaw/...`，而该目录默认由 root 拥有，普通用户无写权限。

**原因**：`SMARTCLAW_HOME`（默认 `/opt/smartclaw`）指向的安装根目录不属于当前用户。

**解决（推荐）**：将安装根目录所有权交给当前用户，使原始路径直接可写，无需改动任何配置：

```bash
# 创建所需子目录（若尚不存在）
sudo mkdir -p /opt/smartclaw/{config,data,logs,sandboxes,run,tmp}

# 将整个安装根目录归属改为当前用户
sudo chown -R $USER:$USER /opt/smartclaw

# 验证可写
ls -ld /opt/smartclaw/data && touch /opt/smartclaw/data/.write_probe && rm /opt/smartclaw/data/.write_probe
```

完成后重新运行即可：

```bash
smartclaw agent add my-agent -i cli_xxx -s xxx
```

> **替代方案**：若不便使用 sudo，可将 `~/.bashrc` 中的 `SMARTCLAW_HOME` 改为 `~/.smartclaw`，并将 `.env` 里的 `DATABASE_PATH`/`LOG_FILE`/`DATA_DIR`/`LOG_DIR` 一并指向 `~/.smartclaw/...`，采用纯用户目录部署。


**祝您使用愉快！**
