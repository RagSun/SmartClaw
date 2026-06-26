"""
AuthPolicyManager — 平台入口租户与鉴权上下文，并组合 AgentResponsePolicy。
与 agent/policy.py 中「是否 @ 才响应」等业务策略分离。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from smartclaw.agent.policy import PolicyManager

if TYPE_CHECKING:
    from smartclaw.config.loader import Config
    from smartclaw.channel.feishu_context import FeishuInboundContext


@dataclass
class InboundAuthContext:
    channel: str
    tenant_id: str
    user_id: str
    chat_id: str
    is_group: bool
    has_mention: bool
    declared_tenant_header: Optional[str] = None


class AuthPolicyManager:
    """解析租户、校验声明租户，并调用 PolicyManager.should_respond。"""

    @staticmethod
    def resolve_tenant_for_feishu(feishu_app_id: Optional[str], cfg: "Config") -> str:
        tid = (cfg.auth.tenant_default or "default").strip() or "default"
        # 1) 优先查租户注册表（运营化真相源，可在线开通而无需改配置）。
        if feishu_app_id:
            try:
                from smartclaw.tenancy.registry import get_tenant_registry

                resolved = get_tenant_registry().resolve_by_app_id(feishu_app_id)
                if resolved:
                    return resolved
            except Exception:
                pass
        # 2) 回退到 config.toml 的静态映射（兼容既有部署）。
        if feishu_app_id and cfg.auth.tenant_by_app_id:
            return cfg.auth.tenant_by_app_id.get(feishu_app_id, tid)
        return tid

    @staticmethod
    def verify_declared_tenant(
        declared: Optional[str],
        resolved: str,
        cfg: "Config",
    ) -> tuple[bool, str]:
        if not cfg.auth.tenant_trust_header:
            return True, ""
        if declared is None or str(declared).strip() == "":
            return True, ""
        if str(declared).strip() == resolved:
            return True, ""
        return False, "X-SmartClaw-Tenant-Id does not match resolved tenant"

    @staticmethod
    def compute_feishu_has_mention(ctx: "FeishuInboundContext", valid_agent_names: list[str]) -> bool:
        from smartclaw.channel.feishu_context import feishu_placeholder_mention_detected

        if not ctx.is_group:
            return True
        if feishu_placeholder_mention_detected(ctx.content_text):
            return True
        # 任意 @ 片段即视为用户已尝试唤醒。飞书展示名常为「SmartClaw-部门B」，与目录名 bot_dept_b
        # 不一致；不得在此处用 valid_agent_names 强匹配，否则 has_m=False 会误发「请先 @ 我」。
        # 谁来应答由 route_with_mentions / mention_targets / resolve_agent_for_http_webhook 决定。
        if any(str(m).strip() for m in (ctx.mentions or [])):
            return True
        return False

    @staticmethod
    def should_dispatch_feishu_agent(
        policy_manager: PolicyManager,
        agent_name: str,
        ctx: "FeishuInboundContext",
        has_mention: bool,
    ) -> bool:
        return policy_manager.should_respond(
            agent_name,
            ctx.is_group,
            user_id=ctx.user_open_id,
            group_id=ctx.chat_id or None,
            has_mention=has_mention,
        )
