"""
Agent 展示名（display_name / aliases）的渠道感知命名约定。

不同渠道对「机器人名称」的约定不同：
- 飞书：开放平台「机器人名称」建议带 ``SmartClaw-`` 前缀，与 ``parse_mentions`` / 别名解析对齐。
- 企业微信：应用名由后台配置，路由仅按 ``display_name``/``aliases`` 字符串匹配，
  无前缀要求，故默认使用裸逻辑名（与 ``agent_admin_tool`` 一致）。

路由本身（``router._alias_keys_for_config``）只做字符串匹配，前缀并非语义必需——
此处仅为各渠道提供合理的默认值，用户始终可通过 ``--display-name`` 覆盖。
"""

from __future__ import annotations

# 飞书后台「机器人名称」建议前缀（仅飞书渠道默认使用）
FEISHU_DISPLAY_NAME_PREFIX = "SmartClaw"


def canonical_display_name(logical_agent_name: str, channel: str = "feishu") -> str:
    """
    由 Agent 逻辑名生成建议的 display_name / aliases，按渠道区分。

    - ``feishu``（默认）: ``SmartClaw-<logical>``，例如 default → ``SmartClaw-default``。
    - ``wecom``: 裸逻辑名 ``<logical>``，例如 default → ``default``。
    """
    n = (logical_agent_name or "").strip() or "agent"
    if (channel or "").strip().lower() == "wecom":
        return n
    return f"{FEISHU_DISPLAY_NAME_PREFIX}-{n}"


def canonical_feishu_display_name(logical_agent_name: str) -> str:
    """``canonical_display_name`` 的飞书渠道别名（向后兼容）。"""
    return canonical_display_name(logical_agent_name, "feishu")
