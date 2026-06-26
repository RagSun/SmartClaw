"""
飞书 WebSocket 长连接服务

独立的飞书长连接服务，支持 1 Agent = 1 Bot。
"""

import smartclaw.paths as paths
import asyncio
import json
from typing import Optional

from smartclaw.agent.policy import PolicyManager
from smartclaw.agent.router import AgentRouter
from smartclaw.agent.runner import AgentRunner
from smartclaw.agent.control_flow import is_auth_or_role_query
from smartclaw.agent.session import SessionManager
from smartclaw.audit.logger import feishu_inbound
from smartclaw.auth.policy_manager import AuthPolicyManager
from smartclaw.auth.tool_gate import resolve_feishu_roles
from smartclaw.channel.base import InboundMessage, OutboundMessage
from smartclaw.channel.feishu_context import (
    feishu_inbound_context_from_ws,
    format_feishu_ambiguous_routing_hint,
    format_feishu_unresolved_mentions_hint,
    merge_feishu_mention_tokens,
    resolve_agent_for_http_webhook,
    strip_feishu_mentions_for_model,
    try_feishu_single_hint_slot,
)
from smartclaw.channel.feishu_runtime import (
    build_feishu_session_id,
    feishu_download_dir,
    feishu_reply_chat_id,
)
from smartclaw.channel.feishu_ws import FeishuWebSocketAdapter
from smartclaw.config.loader import get_config, tenant_llm_config_as_merge_dict
from smartclaw.feishu.formatter import format_feishu_card
from smartclaw.console import error, info, warning
from smartclaw.interfaces import AgentConfig, ChannelType, SandboxBackend
from smartclaw.logging_utils import safe_preview
from smartclaw.llm.base import (
    merge_agent_llm_with_global,
    normalize_agent_llm_dict,
    resolved_model_name_from_llm_dict,
)
from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

# 全局状态
_agents: dict[str, AgentRunner] = {}
_session_managers: dict[str, SessionManager] = {}
_router: Optional[AgentRouter] = None

# app_id -> adapter 映射（避免同一个 app_id 创建多个 adapter）
_app_id_to_adapter: dict[str, FeishuWebSocketAdapter] = {}
# agent_name -> app_id 映射
_agent_to_app_id: dict[str, str] = {}
_ws_policy_manager = PolicyManager()


async def load_agents(default_feishu_config: dict) -> None:
    """加载所有 Agent 配置并初始化其对应的飞书 Adapter"""
    agents_dirs = paths.get_agents_dirs()
    seen_names: set[str] = set()

    for agents_dir in agents_dirs:
        if not agents_dir.exists():
            continue

        config_files = list(agents_dir.glob("*/agent.json")) + list(
            agents_dir.glob("*/*/agent.json")
        )
        for config_file in config_files:
            rel_parts = config_file.relative_to(agents_dir).parts
            path_tenant = DEFAULT_TENANT_ID if len(rel_parts) == 2 else normalize_tenant_id(rel_parts[0])
            path_agent_name = rel_parts[-2]

            try:
                with open(config_file, encoding="utf-8") as f:
                    agent_data = json.load(f)

                if not agent_data.get("enabled", True):
                    continue

                agent_name = agent_data.get("name", path_agent_name)

                cfg = get_config()
                feishu_raw = agent_data.get("feishu", {}) or {}
                tenant_id = (
                    agent_data.get("tenant_id")
                    or (getattr(cfg.auth, "tenant_by_app_id", {}) or {}).get(feishu_raw.get("app_id", ""))
                    or getattr(cfg.auth, "tenant_default", "default")
                    or path_tenant
                    or "default"
                )
                tenant_id = normalize_tenant_id(tenant_id)
                agent_data["tenant_id"] = tenant_id

                qualified_name = tenant_agent_key(agent_name, tenant_id)
                if qualified_name in seen_names:
                    continue
                seen_names.add(qualified_name)

                llm_raw = dict(agent_data.get("llm") or {})
                api_key_raw = llm_raw.get("api_key", "")
                if api_key_raw and str(api_key_raw).startswith("ENC:"):
                    from smartclaw.agent.manager import AgentManager

                    llm_raw["api_key"] = AgentManager()._decrypt(str(api_key_raw)[4:])

                g_llm = tenant_llm_config_as_merge_dict(cfg, tenant_id)
                agent_data["llm"] = normalize_agent_llm_dict(
                    merge_agent_llm_with_global(llm_raw, g_llm)
                )

                # 1. 创建会话管理器
                session_manager = SessionManager(agent_id=agent_name, tenant_id=tenant_id)
                _session_managers[qualified_name] = session_manager

                # 2. 创建 Agent Runner
                runner = AgentRunner(
                    agent_id=agent_name,
                    config=AgentConfig(
                        name=agent_name,
                        description=agent_data.get("description", ""),
                        channel=ChannelType.FEISHU,
                        model_provider=agent_data.get("llm", {}).get("provider", "zhipu"),
                        model_name=resolved_model_name_from_llm_dict(
                            agent_data.get("llm", {}), "glm-4"
                        ),
                        sandbox_enabled=agent_data.get("sandbox", {}).get("enabled", True),
                        sandbox_backend_type=SandboxBackend(agent_data.get("sandbox", {}).get("type", "firecracker")),
                    ),
                    session_manager=session_manager,
                    llm_config=agent_data.get("llm", {}),
                    agent_profile={
                        "display_name": agent_data.get("display_name", agent_name),
                        "tenant_id": tenant_id,
                        "workspace": agent_data.get("workspace", ""),
                        "description": agent_data.get("description", ""),
                    },
                )
                _agents[qualified_name] = runner

                # 3. 处理独立飞书配置
                feishu_cfg = agent_data.get("feishu", {})
                app_id = feishu_cfg.get("app_id")
                app_secret = feishu_cfg.get("app_secret")

                # 解密 ENC: 前缀的加密值；失败时回退全局凭证，不能让整个 Agent 加载失败。
                if app_secret and app_secret.startswith("ENC:"):
                    from smartclaw.agent.manager import AgentManager
                    try:
                        app_secret = AgentManager()._decrypt(app_secret[4:])
                    except Exception as ex:
                        warning(
                            f"Agent {qualified_name} 飞书 app_secret 解密失败，"
                            f"尝试使用全局飞书凭证: {ex}"
                        )
                        app_secret = ""

                if not app_id or not app_secret:
                    app_id = default_feishu_config.get("app_id")
                    app_secret = default_feishu_config.get("app_secret")

                if app_id and app_secret:
                    _agent_to_app_id[qualified_name] = app_id
                    if app_id not in _app_id_to_adapter:
                        adapter = FeishuWebSocketAdapter({
                            "app_id": app_id,
                            "app_secret": app_secret
                        })
                        _app_id_to_adapter[app_id] = adapter

                info(f"加载 Agent: {qualified_name} (App ID: {app_id})")

            except Exception as e:
                error(f"加载 Agent {config_file} 失败: {e}")

    if not _agents:
        info("暂无 Agent 配置")


async def start_agents() -> None:
    """启动所有 Agent"""
    for agent_name, runner in _agents.items():
        try:
            await runner.start()
        except Exception as e:
            error(f"启动 Agent {agent_name} 失败: {e}")


async def stop_agents() -> None:
    """停止所有 Agent"""
    for agent_name, runner in _agents.items():
        try:
            await runner.stop()
        except Exception as e:
            error(f"停止 Agent {agent_name} 失败: {e}")



async def reload_agents() -> None:
    """重新加载所有 Agent 并重启服务"""
    info("检测到配置更新，正在热重载 Agent 设定...")
    # 停止当前 Agent
    await stop_agents()
    
    # 清空旧数据
    _agents.clear()
    _agent_to_app_id.clear()
    
    config = get_config()
    default_feishu_config = {}
    if hasattr(config, "channels") and hasattr(config.channels, "feishu"):
        feishu = config.channels.feishu
        if hasattr(feishu, "accounts") and feishu.accounts:
            default_account = feishu.get_default_account()
            if default_account:
                default_feishu_config = {
                    "app_id": default_account.app_id,
                    "app_secret": default_account.app_secret,
                }
        elif hasattr(feishu, "app_id"):
            default_feishu_config = {
                "app_id": feishu.app_id,
                "app_secret": feishu.app_secret,
            }
            
    # 重新加载
    await load_agents(default_feishu_config)
    await start_agents()
    _ws_policy_manager.reload()
    info(f"Agent 热重载完成，当前在线 {len(_agents)} 个")

def _on_config_changed_hook(path):
    """配合 config loader 的钩子"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(reload_agents())
    except Exception as e:
        error(f"热重载触发异常: {e}")


def _is_auth_current_user_command(text: str) -> bool:
    return is_auth_or_role_query(text)


def create_message_handler(app_id: str):
    """闭包：为每个 Adapter 创建绑定的消息处理器"""
    
    async def handler(message: InboundMessage) -> None:
        global _router
        mid = getattr(message, "message_id", "") or ""
        info(
            f"[Feishu WS handler] 进入 handler app_id={app_id[:10]}... "
            f"message_id={mid[:30] + ('...' if len(mid) > 30 else '')} "
            f"chat_id={message.chat_id or '(空)'} chat_type={message.chat_type}"
        )
        adapter = _app_id_to_adapter.get(app_id)
        if not adapter:
            warning(f"[Feishu WS handler] 未找到 adapter，app_id={app_id}")
            return

        try:
            user_id = message.user_id
            chat_id = message.chat_id
            content = message.content
            is_group = message.chat_type == "group"

            cfg = get_config()
            tenant_id = AuthPolicyManager.resolve_tenant_for_feishu(app_id, cfg)
            ok_t, reason_t = AuthPolicyManager.verify_declared_tenant(None, tenant_id, cfg)
            if not ok_t:
                error(f"飞书 WS 租户校验失败: {reason_t}")
                feishu_inbound(
                    transport="websocket",
                    tenant_id=tenant_id,
                    feishu_app_id=app_id,
                    agent_name="",
                    user_open_id=user_id,
                    chat_id=chat_id,
                    is_group=is_group,
                    action="tenant_rejected",
                    detail=reason_t,
                )
                return

            structured = list(getattr(message, "mentions", None) or [])
            merged_tokens = merge_feishu_mention_tokens(structured, content, _router)
            valid_agents = [ag for ag, ap in _agent_to_app_id.items() if ap == app_id]
            if not valid_agents:
                error(f"没有找到绑定 App ID {app_id} 的 Agent")
                return

            ctx = feishu_inbound_context_from_ws(
                user_open_id=user_id,
                chat_id=chat_id,
                is_group=is_group,
                content_text=content,
                mentions=list(merged_tokens),
                message_id=getattr(message, "message_id", None),
            )
            has_m = AuthPolicyManager.compute_feishu_has_mention(ctx, valid_agents)

            import re

            content_clean = strip_feishu_mentions_for_model(content) or re.sub(
                r"<@_user_\d+>", "", content
            ).strip()
            trace_id = (getattr(message, "message_id", None) or chat_id or user_id or "ws")[:16]

            info(
                "[Feishu WS] inbound | "
                f"trace={trace_id} app_id={app_id[:10]}... tenant={tenant_id} "
                f"user={user_id} chat={chat_id} group={is_group} has_mention={has_m} "
                f"mentions={len(merged_tokens)} candidates={len(valid_agents)} "
                f"content_preview={safe_preview(content, 100)!r}"
            )

            if ctx.is_group and not has_m:
                ack_text = "💬 收到消息，请在消息前 @ 我以唤醒助手"
                ack_card = format_feishu_card(ack_text, agent_name="Bot")
                ack_outbound = OutboundMessage(
                    chat_id=chat_id,
                    content=ack_card,
                    message_type="interactive",
                )
                await adapter.send_message(ack_outbound)
                feishu_inbound(
                    transport="websocket",
                    tenant_id=tenant_id,
                    feishu_app_id=app_id,
                    agent_name="",
                    user_open_id=user_id,
                    chat_id=chat_id,
                    is_group=is_group,
                    action="mention_required_ack",
                )
                info("群聊未 @Bot，已发送提示消息")
                return

            # 一群多机器人：各 App 的 WS 都会收到同一条群消息，仅与 @ 目标匹配的 Agent 应处理。
            if ctx.is_group and has_m:
                routed_logical = _router.route_with_mentions(
                    content,
                    user_id,
                    chat_id or None,
                    True,
                    tenant_id=tenant_id,
                    mention_tokens=merged_tokens,
                )
                mention_targets = {
                    tenant_agent_key(str(r).strip(), tenant_id)
                    for r in (routed_logical or [])
                    if str(r).strip()
                }
                if not mention_targets:
                    feishu_inbound(
                        transport="websocket",
                        tenant_id=tenant_id,
                        feishu_app_id=app_id,
                        agent_name="",
                        user_open_id=user_id,
                        chat_id=chat_id,
                        is_group=is_group,
                        action="mention_unresolved",
                        detail=f"tokens={merged_tokens!r}",
                    )
                    if try_feishu_single_hint_slot(
                        str(getattr(message, "message_id", "") or "")
                    ):
                        hint_u = format_feishu_unresolved_mentions_hint(merged_tokens)
                        un_out = OutboundMessage(
                            chat_id=chat_id,
                            content=format_feishu_card(hint_u, agent_name="SmartClaw"),
                            message_type="interactive",
                        )
                        await adapter.send_message(un_out)
                    info(
                        "[Feishu WS] 群 @ 无法映射到 Agent，跳过 | "
                        f"trace={trace_id} tokens={merged_tokens!r}"
                    )
                    return
                if not (set(valid_agents) & mention_targets):
                    feishu_inbound(
                        transport="websocket",
                        tenant_id=tenant_id,
                        feishu_app_id=app_id,
                        agent_name="",
                        user_open_id=user_id,
                        chat_id=chat_id,
                        is_group=is_group,
                        action="mention_other_agent_skip",
                        detail=f"targets={sorted(mention_targets)!r}",
                    )
                    info(
                        "[Feishu WS] 群消息 @ 目标非本 App 下 Agent，跳过 | "
                        f"app_id={app_id[:10]}... targets={sorted(mention_targets)!r} "
                        f"valid={valid_agents!r}"
                    )
                    return

            agent_name = resolve_agent_for_http_webhook(
                ctx, _router, valid_agents, has_m, tenant_id=tenant_id
            )
            if not agent_name:
                error(
                    "WS 无法路由到 Agent "
                    f"(trace={trace_id} app_id={app_id[:10]}..., "
                    f"content_preview={safe_preview(content or '', 80)!r})"
                )
                try:
                    hint_t = format_feishu_ambiguous_routing_hint(
                        valid_agents,
                        tenant_id=tenant_id,
                        app_id_short=str(app_id[:16]),
                    )
                    amb_out = OutboundMessage(
                        chat_id=chat_id,
                        content=format_feishu_card(hint_t, agent_name="SmartClaw"),
                        message_type="interactive",
                    )
                    await adapter.send_message(amb_out)
                    feishu_inbound(
                        transport="websocket",
                        tenant_id=tenant_id,
                        feishu_app_id=app_id,
                        agent_name="",
                        user_open_id=user_id,
                        chat_id=chat_id,
                        is_group=is_group,
                        action="routing_ambiguous",
                        detail=f"candidates={len(valid_agents)}",
                    )
                except Exception as _ex:
                    warning(f"[Feishu WS] ambiguous routing 提示发送失败: {_ex}")
                return

            if not AuthPolicyManager.should_dispatch_feishu_agent(
                _ws_policy_manager, agent_name, ctx, has_m
            ):
                feishu_inbound(
                    transport="websocket",
                    tenant_id=tenant_id,
                    feishu_app_id=app_id,
                    agent_name=agent_name,
                    user_open_id=user_id,
                    chat_id=chat_id,
                    is_group=is_group,
                    action="policy_blocked",
                    detail="agent_policy",
                    roles=resolve_feishu_roles(tenant_id, user_id, cfg),
                )
                info(f"WS 消息被策略忽略: agent={agent_name}")
                return

            runner = _agents.get(agent_name)
            if not runner:
                error(f"Agent 不存在: {agent_name}")
                return

            roles = resolve_feishu_roles(tenant_id, user_id, cfg)
            if _is_auth_current_user_command(content_clean):
                session_id = build_feishu_session_id(
                    user_open_id=user_id,
                    app_id=app_id,
                    chat_id=chat_id,
                    is_group=is_group,
                )
                response_text = (
                    "当前飞书用户身份：\n\n"
                    f"- tenant: `{tenant_id}`\n"
                    f"- agent: `{agent_name}`\n"
                    f"- open_id: `{user_id}`\n"
                    f"- roles: `{', '.join(roles) if roles else 'default'}`\n"
                    f"- session_id: `{session_id}`\n"
                    f"- app_id: `{app_id[:10]}...`\n\n"
                    "授予管理员角色示例：\n"
                    f"`smartclaw auth roles set {user_id} --tenant {tenant_id} --roles tenant_admin,developer`"
                )
                outbound = OutboundMessage(
                    chat_id=feishu_reply_chat_id(
                        user_open_id=user_id,
                        chat_id=chat_id,
                        is_group=is_group,
                    ),
                    content=format_feishu_card(response_text, agent_name=agent_name),
                    message_type="interactive",
                )
                await adapter.send_message(outbound)
                feishu_inbound(
                    transport="websocket",
                    tenant_id=tenant_id,
                    feishu_app_id=app_id,
                    agent_name=agent_name,
                    user_open_id=user_id,
                    chat_id=chat_id,
                    is_group=is_group,
                    action="auth_current_user",
                    roles=roles,
                )
                info(
                    "[Feishu WS] auth_current_user replied | "
                    f"trace={trace_id} tenant={tenant_id} agent={agent_name} user={user_id}"
                )
                return

            session_id = build_feishu_session_id(
                user_open_id=user_id,
                app_id=app_id,
                chat_id=chat_id,
                is_group=is_group,
            )
            info(
                "[Feishu WS] dispatch | "
                f"trace={trace_id} tenant={tenant_id} agent={agent_name} "
                f"user={user_id} session={session_id}"
            )

            typing_reaction_id = None
            if message.message_id:
                try:
                    typing_reaction_id = await adapter.add_typing_indicator(message.message_id)
                except Exception:
                    pass

            import base64

            from smartclaw.vision.service import configure_vision_for_merged_llm, get_vision_service
            from smartclaw.config.loader import tenant_vision_config

            # 与 feishu_multiprocess 一致：先同步当前 Agent 的视觉配置，再识图
            cfg = get_config()
            configure_vision_for_merged_llm(
                runner._llm_config,
                tenant_vision_config(cfg, getattr(runner, "tenant_id", tenant_id)),
                log_tag=agent_name,
                verbose=False,
            )
            vision_service = get_vision_service()

            content_obj = message.raw_data.get("content_obj", {})
            image_keys = list(getattr(message, "image_keys", None) or [])
            if not image_keys and content_obj.get("image_key"):
                ik = content_obj.get("image_key")
                if ik:
                    image_keys = [str(ik)]

            download_dir = feishu_download_dir(agent_name, tenant_id)
            download_dir.mkdir(parents=True, exist_ok=True)

            image_desc_lines: list[str] = []
            path_lines: list[str] = []

            if image_keys:
                for resource_key in image_keys:
                    save_path = str(download_dir / str(resource_key))
                    success = await adapter.download_resource(
                        message.message_id,
                        str(resource_key),
                        "image",
                        save_path,
                    )
                    if success and vision_service.is_enabled():
                        try:
                            with open(save_path, "rb") as f:
                                img_b64 = base64.b64encode(f.read()).decode()
                            desc = await vision_service.understand_image(
                                image_data=img_b64,
                                prompt="请描述这张图片的内容",
                                agent_id=agent_name,
                            )
                            if desc and str(desc).strip():
                                image_desc_lines.append(
                                    f"[图片描述: {str(desc).strip()}]"
                                )
                            else:
                                image_desc_lines.append("[图片无法识别]")
                        except Exception as e:
                            warning(f"[WS][{agent_name}] 视觉理解失败: {e}")
                            image_desc_lines.append(
                                f"[图片理解失败: {str(e)[:50]}]"
                            )
                    if success:
                        path_lines.append(
                            "[用户发送了一个image，已保存在本地路径: "
                            f"{save_path}，你可以使用工具读取它]"
                        )
                    else:
                        path_lines.append("[用户发送了一个image，但系统下载失败]")

            elif any(k in content_obj for k in ["file_key", "media_key"]):
                resource_type = "file"
                resource_key = content_obj.get("file_key") or content_obj.get(
                    "media_key"
                )
                save_path = str(download_dir / str(resource_key))
                success = await adapter.download_resource(
                    message.message_id,
                    str(resource_key),
                    resource_type,
                    save_path,
                )
                if success:
                    path_lines.append(
                        f"[用户发送了一个{resource_type}，已保存在本地路径: "
                        f"{save_path}，你可以使用工具读取它]"
                    )
                else:
                    path_lines.append(
                        f"[用户发送了一个{resource_type}，但系统下载失败]"
                    )

            image_urls = list(getattr(message, "image_urls", None) or [])
            if image_urls and vision_service.is_enabled():
                for img_url in image_urls:
                    try:
                        desc = await vision_service.understand_image(
                            image_data=img_url,
                            prompt="请描述这张图片的内容",
                            agent_id=agent_name,
                        )
                        image_desc_lines.append(f"[图片描述: {desc}]")
                    except Exception as e:
                        image_desc_lines.append(
                            f"[图片理解失败: {str(e)[:50]}]"
                        )

            extras = [*image_desc_lines, *path_lines]
            if extras:
                content_clean = "\n".join(extras) + "\n" + content_clean
                info(
                    "[Feishu WS] attachment context added | "
                    f"trace={trace_id} agent={agent_name} images={len(image_desc_lines)} "
                    f"files={len(path_lines)}"
                )

            feishu_inbound(
                transport="websocket",
                tenant_id=tenant_id,
                feishu_app_id=app_id,
                agent_name=agent_name,
                user_open_id=user_id,
                chat_id=chat_id,
                is_group=is_group,
                action="dispatch",
                roles=roles,
            )

            response_text = await runner.process_message(
                user_id=user_id,
                channel=ChannelType.FEISHU,
                is_group=is_group,
                content=content_clean,
                session_id=session_id,
                tenant_id=tenant_id,
            )

            if hasattr(response_text, 'content'):
                response_text = getattr(response_text, 'content', str(response_text))
            elif not isinstance(response_text, str):
                response_text = str(response_text)

            info(
                "[Feishu WS] agent completed | "
                f"trace={trace_id} agent={agent_name} reply_len={len(response_text or '')}"
            )

            # 发送回复
            card_content = format_feishu_card(response_text, agent_name=agent_name)
            outbound = OutboundMessage(
                chat_id=feishu_reply_chat_id(
                    user_open_id=user_id,
                    chat_id=chat_id,
                    is_group=is_group,
                ),
                content=card_content,
                message_type="interactive",
            )

            if message.message_id and typing_reaction_id:
                try:
                    await adapter.remove_typing_indicator(message.message_id, typing_reaction_id)
                except Exception:
                    pass

            success = await adapter.send_message(outbound)
            if success:
                info(
                    "[Feishu WS] reply sent | "
                    f"trace={trace_id} agent={agent_name} "
                    f"preview={safe_preview(response_text or '', 80)!r}"
                )
            else:
                error(
                    f"[Feishu WS handler] send_message 返回 False "
                    f"agent={agent_name} chat_id={chat_id}"
                )

        except Exception as e:
            msg_id = message.message_id if hasattr(message, "message_id") else "unknown"
            error(f"处理飞书消息失败: {e}, msg_id={msg_id}")
            import traceback
            traceback.print_exc()

    return handler


async def main() -> None:
    """主函数"""
    global _router

    info("SmartClaw 飞书多 Agent 长连接服务启动中...")
    _router = AgentRouter()
    config = get_config()

    default_feishu_config = {}
    if hasattr(config, "channels") and hasattr(config.channels, "feishu"):
        feishu = config.channels.feishu
        if hasattr(feishu, "accounts") and feishu.accounts:
            default_account = feishu.get_default_account()
            if default_account:
                default_feishu_config = {
                    "app_id": default_account.app_id,
                    "app_secret": default_account.app_secret,
                }
        elif hasattr(feishu, "app_id"):
            default_feishu_config = {
                "app_id": feishu.app_id,
                "app_secret": feishu.app_secret,
            }

    # 加载 Agent 并初始化对应的 Adapter
    await load_agents(default_feishu_config)

    if not _agents:
        error("没有可用的 Agent，请先创建 Agent: smartclaw agent create <name>")
        return
        
    if not _app_id_to_adapter:
        error("没有任何有效的飞书 App ID 配置。请在 global 或 agent.json 中配置。")
        return

    
    # 启动配置热重载
    from smartclaw.config.loader import start_config_watcher
    from smartclaw.config.watcher import get_watcher
    start_config_watcher()
    
    # 注入业务层的重载钩子
    watcher = get_watcher()
    # 保存原始的 _on_config_file_changed (它负责清空 _config 缓存)
    original_callback = watcher.callback
    
    def wrapped_callback(path):
        if original_callback:
            original_callback(path)
        _on_config_changed_hook(path)
        
    watcher.set_callback(wrapped_callback)
    
# 启动 Agent
    await start_agents()
    _ws_policy_manager.reload()

    info(f"服务已就绪: {len(_agents)} 个 Agent，{len(_app_id_to_adapter)} 个飞书连接")

    try:
        # 启动所有 Adapter 的长连接
        start_tasks = []
        for app_id, adapter in _app_id_to_adapter.items():
            handler = create_message_handler(app_id)
            start_tasks.append(adapter.start_listening(handler))
            
        await asyncio.gather(*start_tasks)
        
    except KeyboardInterrupt:
        info("收到停止信号...")
    finally:
        await stop_agents()
        for adapter in _app_id_to_adapter.values():
            await adapter.close()
        info("SmartClaw 服务已停止")


if __name__ == "__main__":
    asyncio.run(main())
