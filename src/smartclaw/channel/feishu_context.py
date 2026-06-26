"""
飞书 HTTP Webhook 事件解析 — 与 WebSocket 入站语义对齐。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class FeishuInboundContext:
    """HTTP 长连接/回调通用字段（供 AuthPolicyManager 与路由使用）。"""

    user_open_id: str
    chat_id: str
    is_group: bool
    content_raw: str
    content_text: str
    mentions: list[str]
    message_id: Optional[str] = None
    event_id: Optional[str] = None


def _parse_message_content(content: Any) -> str:
    if isinstance(content, str):
        try:
            content_obj = json.loads(content)
            return str(content_obj.get("text", "") or "")
        except Exception:
            return content
    return str(content)


def parse_feishu_event_body(body: dict[str, Any]) -> Optional[FeishuInboundContext]:
    """
    从飞书开放平台事件 JSON 解析上下文（im.message.receive_v1 等）。
    """
    if not body or body.get("type") == "url_verification":
        return None

    event = body.get("event") or {}
    message = event.get("message") or {}
    sender = event.get("sender") or {}

    user_id = (sender.get("sender_id") or {}).get("open_id", "") or ""
    content = message.get("content", "")
    content_text = _parse_message_content(content)

    chat_id = str(message.get("chat_id", "") or "")
    chat_type = str(message.get("chat_type", "") or "").lower()
    is_group = chat_type == "group"

    from smartclaw.agent.router import AgentRouter

    router = AgentRouter()

    header = body.get("header") or {}
    ev = header.get("event_id") or event.get("event_id") or body.get("event_id")
    event_id = str(ev) if ev else None

    message_id = str(message.get("message_id", "") or "") or None

    structured: list[str] = []
    for m in message.get("mentions") or []:
        if isinstance(m, dict):
            n = m.get("name") or m.get("tenant_key")
            if n and str(n).strip():
                structured.append(str(n).strip())

    if not user_id:
        return None
    if not content_text.strip() and not message.get("message_id"):
        return None

    mentions = merge_feishu_mention_tokens(structured, content_text, router)

    return FeishuInboundContext(
        user_open_id=user_id,
        chat_id=chat_id,
        is_group=is_group,
        content_raw=str(content) if not isinstance(content, str) else content,
        content_text=content_text,
        mentions=mentions,
        message_id=message_id,
        event_id=event_id,
    )


def feishu_placeholder_mention_detected(content_text: str) -> bool:
    """飞书 <@_user_xxx> 占位符是否视为 @ 机器人。"""
    return bool(re.search(r"<@_user_\d+>", content_text))


def strip_feishu_mentions_for_model(content_text: str) -> str:
    return re.sub(r"<@_user_\d+>", "", content_text).strip()


def feishu_inbound_context_from_ws(
    *,
    user_open_id: str,
    chat_id: str,
    is_group: bool,
    content_text: str,
    mentions: list[str],
    message_id: Optional[str] = None,
) -> FeishuInboundContext:
    """从 WebSocket 路径的 InboundMessage 字段构造与 HTTP 对齐的上下文。"""
    return FeishuInboundContext(
        user_open_id=user_open_id,
        chat_id=chat_id,
        is_group=is_group,
        content_raw=content_text,
        content_text=content_text,
        mentions=list(mentions),
        message_id=message_id,
        event_id=None,
    )


def merge_feishu_mention_tokens(
    structured: Optional[list[str]],
    content_text: str,
    router: Any,
) -> list[str]:
    """合并飞书 SDK 给出的 @ 显示名与正文正则解析结果，去重保序。"""
    parsed = router.parse_mentions(content_text)
    out: list[str] = []
    seen: set[str] = set()
    for m in list(structured or []) + parsed:
        s = str(m).strip()
        if not s:
            continue
        k = s.lower()
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def try_feishu_single_hint_slot(message_id: str) -> bool:
    """
    多 Worker 同收一条群消息时，仅占位一次（创建独占 lock 文件）。
    用于「@ 无法解析」类提示，避免三条机器人各发一张卡。
    """
    from smartclaw.paths import USER_HOME

    mid = (message_id or "").strip()
    if not mid:
        return False
    d = USER_HOME / "run" / "feishu_unresolved_hint"
    d.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in mid)[:180]
    p = d / f"{safe}.lock"
    try:
        with p.open("x", encoding="utf-8"):
            pass
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


def format_feishu_unresolved_mentions_hint(mention_tokens: list[str]) -> str:
    """@ 片段无法映射到任何已加载 Agent 时的说明。"""
    show = ", ".join(f"`@{m}`" for m in (mention_tokens or [])[:8])
    return (
        "**无法将 @ 映射到已配置的助手**（请检查 `agent.json` 的 `name` / `display_name` / `aliases`）。\n\n"
        f"- 本次解析片段: {show or '（空）'}\n"
        "- 请在各 Agent 的 **`aliases`** 中加入飞书里的 **机器人显示名**（例如 `SmartClaw-部门B`），"
        "然后重启服务。\n"
    )


def resolve_agent_for_http_webhook(
    ctx: FeishuInboundContext,
    router: Any,
    agent_names: list[str],
    has_mention: bool,
    tenant_id: str = "default",
) -> Optional[str]:
    """与 feishu_ws_server / server 语义对齐：群聊无 @ 返回 None；路由歧义时返回 None。

    群聊且已判定含 @（占位或 mentions）时，不得仅凭 ``len(agent_names)==1`` 回退到唯一候选，
    必须能从正文解析到候选之一（多进程下每 Worker 仅此自身一个候选时不抢答）。
    """
    def _resolve_candidate(candidate: str) -> Optional[str]:
        """Resolve plain agent names against tenant-qualified loaded names."""
        if candidate in agent_names:
            return candidate
        cand = (candidate or "").strip().lower()
        matches = [
            name for name in agent_names
            if name.rsplit("/", 1)[-1].lower() == cand
        ]
        return matches[0] if len(matches) == 1 else None

    if not agent_names:
        return None
    if ctx.is_group and not has_mention:
        return None
    mt = ctx.mentions if ctx.mentions else None
    if ctx.is_group and has_mention:
        routed = router.route_with_mentions(
            ctx.content_text,
            ctx.user_open_id,
            ctx.chat_id or None,
            True,
            tenant_id=tenant_id,
            mention_tokens=mt,
        )
        for r in routed:
            resolved = _resolve_candidate(r)
            if resolved:
                return resolved
    routed = router.route_with_mentions(
        ctx.content_text,
        ctx.user_open_id,
        ctx.chat_id or None,
        ctx.is_group,
        tenant_id=tenant_id,
        mention_tokens=mt,
    )
    for r in routed:
        resolved = _resolve_candidate(r)
        if resolved:
            return resolved
    if len(agent_names) == 1:
        # 群聊且已视为带 @：必须从正文路由到候选之一，禁止「仅此一个 Worker」就抢答。
        # 多进程飞书下一群多 App 时，各进程候选只有自身，盲回会导致 @ 单个机器人却全员回复。
        if ctx.is_group and has_mention:
            return None
        return agent_names[0]
    return None


def format_feishu_ambiguous_routing_hint(
    valid_agents: list[str],
    *,
    tenant_id: str,
    app_id_short: str = "",
) -> str:
    """飞书侧路由歧义时给用户看的说明（卡片正文 Markdown）。"""
    uniq = sorted(set(valid_agents))
    head = (
        "**无法唯一确定要使用的助手**（routing ambiguous）。\n\n"
        f"- tenant: `{tenant_id}`\n"
    )
    if app_id_short:
        head += f"- app\\_id: `{app_id_short}`\n"
    head += (
        "\n请在消息中 **明确 @** 以下显示名之一（或联系管理员：建议 **一飞书应用绑定一个主 Agent**，并配置群/用户绑定）。\n\n"
        "**当前候选：**\n"
    )
    for q in uniq:
        short = q.rsplit("/", 1)[-1]
        head += f"- `@{short}` （qualified: `{q}`）\n"
    return head.rstrip()
