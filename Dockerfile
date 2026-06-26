# SmartClaw 容器镜像 — 基线使用 Python 3.12-slim
FROM python:3.12-slim

LABEL maintainer="smartclaw@example.com"
LABEL description="SmartClaw - 生产级企业 AI Agent 平台"

# 安装系统依赖 + uv（替代 pip 的快速包管理器）
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install uv

# 设置工作目录
WORKDIR /app

# 先装运行时依赖闭包（Agent 执行内核 + Redis），单独一层便于缓存
COPY requirements-runtime.txt /app/requirements-runtime.txt
RUN uv pip install --system --no-cache-dir -r /app/requirements-runtime.txt

# 复制项目
COPY . /app/

# 安装本包（核心 Web/CLI 依赖）
RUN uv pip install --system --no-cache-dir -e .

# 创建必要目录
RUN mkdir -p /opt/smartclaw/{config,logs,data,sandboxes}

# 暴露端口
EXPOSE 8000

# 启动
CMD ["smartclaw", "start", "--host", "0.0.0.0"]
