"""
SmartClaw - 生产级企业 AI Agent 平台

每个 Agent 运行在独立 microVM 中，实现硬件级隔离。
支持飞书和企业微信双渠道。
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

# 版本号以 pyproject.toml 为唯一来源，通过安装元数据读取，避免硬编码漂移。
try:
    __version__ = _pkg_version("smartclaw")
except PackageNotFoundError:  # 未安装（如直接源码运行）时的兜底
    __version__ = "0.0.0+local"

__author__ = "SmartClaw Team"
