"""
飞书 WebSocket 长连接服务 - 多进程架构

每个飞书 App 在独立进程中运行，避免 event loop 冲突。
工业级方案：支持多 Agent + 多飞书 App 真正并行。
"""

import smartclaw.paths as paths
import asyncio
import json
import multiprocessing as mp
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
import base64
import re
from pathlib import Path
from typing import Any, Optional

# 设置启动方法为 spawn（避免 fork 问题）
try:
    mp.set_start_method('spawn', force=True)
except RuntimeError:
    pass  # 已设置

from smartclaw.agent.router import AgentRouter
from smartclaw.agent.policy import PolicyManager
from smartclaw.audit.logger import feishu_inbound
from smartclaw.auth.policy_manager import AuthPolicyManager
from smartclaw.interfaces import AgentConfig, ChannelType, SandboxBackend
from smartclaw.agent.runner import AgentRunner
from smartclaw.agent.control_flow import is_auth_or_role_query
from smartclaw.agent.session import SessionManager
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
from smartclaw.console import error, info, warning
from smartclaw.auth.tool_gate import resolve_feishu_roles
from smartclaw.feishu.formatter import format_feishu_card
from smartclaw.logging_utils import safe_preview
from smartclaw.llm.base import (
    merge_agent_llm_with_global,
    normalize_agent_llm_dict,
    resolved_model_name_from_llm_dict,
)
from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key


def _is_auth_current_user_command(text: str) -> bool:
    return is_auth_or_role_query(text)


@dataclass
class AgentInfo:
    """Agent 信息"""
    name: str
    tenant_id: str
    display_name: str  # 对外展示的名称（飞书显示名）
    app_id: str
    app_secret: str
    llm_config: dict
    sandbox_enabled: bool
    sandbox_type: str  # "firecracker", "docker", "process"
    workspace: str = ""  # agent.json「执行工作区」覆盖；空则默认 ~/.smartclaw/workspace/<name>
    # OpenClaw 风格安全配置
    sandbox_security_mode: bool = True
    sandbox_network_mode: str = "bridge"
    sandbox_container_user: str = "1000:1000"
    sandbox_read_only_root: bool = True
    sandbox_pids_limit: int = 256


class FeishuWorker(mp.Process):
    """
    飞书 Worker 进程
    
    每个进程独立运行一个 Feishu App + Agent Runner
    """

    def __init__(self, agent_info: AgentInfo):
        super().__init__(daemon=True)
        self.agent_info = agent_info
        self._running = mp.Value('i', 0)

    def run(self) -> None:
        """在独立进程中运行"""
        import asyncio
        
        info(f"[Worker {self.agent_info.name}] 启动，App ID: {self.agent_info.app_id[:10]}...")

        # 创建独立的 event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._running.value = 1

        try:
            loop.run_until_complete(self._run())
        except KeyboardInterrupt:
            info(f"[Worker {self.agent_info.name}] 被中断")
        except Exception as e:
            error(f"[Worker {self.agent_info.name}] 运行异常: {e}")
        finally:
            self._running.value = 0
            loop.close()
            info(f"[Worker {self.agent_info.name}] 已停止")

    async def _run(self) -> None:
        """异步主循环"""
        # 初始化子进程日志（写入文件）
        from smartclaw.console import configure_logging
        from smartclaw import paths as paths_mod
        log_dir = paths_mod.get_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        configure_logging(str(log_dir / f"worker_{self.agent_info.name}.log"), enabled=True)

        # 初始化全局视觉服务（[vision] + agent llm.vision，与 AgentRunner / WS / HTTP 一致）
        from smartclaw.vision.service import configure_vision_for_merged_llm, get_vision_service
        from smartclaw.config.loader import get_config, tenant_vision_config
        from smartclaw.agent.tools.feishu_doc_tool import set_feishu_credentials

        config = get_config()
        configure_vision_for_merged_llm(
            self.agent_info.llm_config,
            tenant_vision_config(config, self.agent_info.tenant_id),
            log_tag=self.agent_info.name,
            verbose=True,
        )
        if get_vision_service().is_enabled():
            info(f"[Worker {self.agent_info.name}] 全局视觉服务已启用")
        
        # 创建 Agent
        session_manager = SessionManager(
            agent_id=self.agent_info.name,
            tenant_id=self.agent_info.tenant_id,
        )

        # 设置飞书凭证（供 feishu_doc_tool 使用）
        set_feishu_credentials(self.agent_info.app_id, self.agent_info.app_secret)

        # 创建 Agent Runner
        
        runner = AgentRunner(
            agent_id=self.agent_info.name,
            config=AgentConfig(
                name=self.agent_info.name,
                description=f"{self.agent_info.name} Agent",
                channel=ChannelType.FEISHU,
                model_provider=self.agent_info.llm_config.get("provider", "zhipu"),
                model_name=resolved_model_name_from_llm_dict(
                    self.agent_info.llm_config, "glm-4"
                ),
                sandbox_enabled=self.agent_info.sandbox_enabled,
                sandbox_backend_type=SandboxBackend(getattr(self.agent_info, 'sandbox_type', "docker")),
            ),
            session_manager=session_manager,
            llm_config=self.agent_info.llm_config,
            agent_profile={
                "display_name": self.agent_info.display_name,
                "tenant_id": self.agent_info.tenant_id,
                "workspace": self.agent_info.workspace,
            },
        )

        # 启动 Agent
        await runner.start()
        info(f"[Worker {self.agent_info.name}] Agent 已启动")

        # 创建飞书适配器
        adapter = FeishuWebSocketAdapter({
            "app_id": self.agent_info.app_id,
            "app_secret": self.agent_info.app_secret,
        })
        self._ws_adapter = adapter  # 保存引用用于下载图片

        router = AgentRouter()
        ws_policy_manager = PolicyManager()

        # 创建消息处理器
        async def message_handler(message: InboundMessage) -> None:
            try:
                trace_id = (message.message_id or message.chat_id or message.user_id or self.agent_info.name)[:16]
                info(
                    f"[Worker {self.agent_info.name}] inbound | "
                    f"trace={trace_id} tenant={self.agent_info.tenant_id} "
                    f"user={message.user_id} chat={message.chat_id} type={message.chat_type} "
                    f"content_preview={safe_preview(message.content, 100)!r}"
                )

                # 【关键】处理图片（如果消息包含图片）
                content = message.content
                image_keys = getattr(message, 'image_keys', [])
                image_urls = getattr(message, 'image_urls', [])
                
                if image_keys or image_urls:
                    vision_service = get_vision_service()
                    if vision_service.is_enabled():
                        try:
                            image_descriptions = []
                            
                            # 处理飞书 image_keys（需要下载）
                            for img_key in image_keys:
                                try:
                                    # 下载图片
                                    import os
                                    
                                    # 使用 adapter 下载（如果可用）
                                    if hasattr(self, '_ws_adapter') and self._ws_adapter:
                                        tmp_dir = feishu_download_dir(
                                            self.agent_info.name,
                                            self.agent_info.tenant_id,
                                        )
                                        tmp_dir.mkdir(parents=True, exist_ok=True)
                                        tmp_path = str(tmp_dir / f"vision_{img_key}.jpg")
                                        success = await self._ws_adapter.download_resource(
                                            message_id=message.message_id,
                                            file_key=img_key,
                                            resource_type="image",
                                            save_path=tmp_path
                                        )
                                        if success and os.path.exists(tmp_path):
                                            with open(tmp_path, 'rb') as f:
                                                img_b64 = base64.b64encode(f.read()).decode()
                                            desc = await vision_service.understand_image(
                                                image_data=img_b64,
                                                prompt="请描述这张图片的内容",
                                                agent_id=self.agent_info.name
                                            )
                                            info(
                                                f"[Worker {self.agent_info.name}] vision done | "
                                                f"trace={trace_id} desc_len={len(desc) if desc else 0} "
                                                f"desc_preview={safe_preview(desc or '', 80)!r}"
                                            )
                                            if desc and desc.strip():
                                                image_descriptions.append(f"[图片描述: {desc.strip()}]")
                                            else:
                                                image_descriptions.append("[图片无法识别]")
                                            os.remove(tmp_path)
                                        else:
                                            image_descriptions.append("[图片下载失败]")
                                except Exception as e:
                                    warning(f"[Worker {self.agent_info.name}] 下载图片失败: {e}")
                                    image_descriptions.append(f"[图片处理失败: {str(e)[:50]}]")
                            
                            # 处理外部 image_urls
                            for img_url in image_urls:
                                try:
                                    desc = await vision_service.understand_image(
                                        image_data=img_url,
                                        prompt="请描述这张图片的内容",
                                        agent_id=self.agent_info.name
                                    )
                                    image_descriptions.append(f"[图片描述: {desc}]")
                                except Exception as e:
                                    image_descriptions.append(f"[图片理解失败: {str(e)[:50]}]")
                            
                            if image_descriptions:
                                content = "\n".join(image_descriptions) + "\n" + content
                                info(
                                    f"[Worker {self.agent_info.name}] attachment context added | "
                                    f"trace={trace_id} images={len(image_descriptions)}"
                                )
                        except Exception as e:
                            warning(f"[Worker {self.agent_info.name}] 视觉理解失败: {e}")

                cfg = get_config()
                tenant_id = AuthPolicyManager.resolve_tenant_for_feishu(self.agent_info.app_id, cfg)
                ok_t, reason_t = AuthPolicyManager.verify_declared_tenant(None, tenant_id, cfg)
                is_group = message.chat_type == "group"

                if not ok_t:
                    error(f"[Worker {self.agent_info.name}] 租户校验失败: {reason_t}")
                    feishu_inbound(
                        transport="websocket_worker",
                        tenant_id=tenant_id,
                        feishu_app_id=self.agent_info.app_id,
                        agent_name="",
                        user_open_id=message.user_id,
                        chat_id=message.chat_id or "",
                        is_group=is_group,
                        action="tenant_rejected",
                        detail=reason_t[:500],
                    )
                    return

                qualified_agent = tenant_agent_key(self.agent_info.name, tenant_id)
                valid_agents = [qualified_agent]

                content_raw = message.content or ""
                structured = list(getattr(message, "mentions", None) or [])
                merged_tokens = merge_feishu_mention_tokens(
                    structured, content_raw, router
                )
                ctx = feishu_inbound_context_from_ws(
                    user_open_id=message.user_id,
                    chat_id=message.chat_id or "",
                    is_group=is_group,
                    content_text=content_raw,
                    mentions=list(merged_tokens),
                    message_id=getattr(message, "message_id", None),
                )
                has_m = AuthPolicyManager.compute_feishu_has_mention(ctx, valid_agents)

                info(
                    f"[Worker {self.agent_info.name}] inbound ctx | "
                    f"trace={trace_id} app_id={self.agent_info.app_id[:10]}... tenant={tenant_id} "
                    f"group={is_group} has_mention={has_m} mentions={len(merged_tokens)} "
                    f"candidates={len(valid_agents)} "
                    f"content_preview={safe_preview(content_raw, 100)!r}"
                )

                if ctx.is_group and not has_m:
                    ack_text = "💬 收到消息，请在消息前 @ 我以唤醒助手"
                    ack_card = format_feishu_card(ack_text, agent_name="Bot")
                    ack_outbound = OutboundMessage(
                        chat_id=message.chat_id,
                        content=ack_card,
                        message_type="interactive",
                    )
                    await adapter.send_message(ack_outbound)
                    feishu_inbound(
                        transport="websocket_worker",
                        tenant_id=tenant_id,
                        feishu_app_id=self.agent_info.app_id,
                        agent_name="",
                        user_open_id=message.user_id,
                        chat_id=message.chat_id or "",
                        is_group=is_group,
                        action="mention_required_ack",
                    )
                    info(f"[Worker {self.agent_info.name}] 群聊未 @Bot，已发送提示")
                    return

                # 一群多机器人：飞书会把同一条群消息推给每个 App 的 WS，仅被 @ 的助手应回复。
                if ctx.is_group and has_m:
                    routed_logical = router.route_with_mentions(
                        content_raw,
                        message.user_id,
                        message.chat_id or None,
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
                            transport="websocket_worker",
                            tenant_id=tenant_id,
                            feishu_app_id=self.agent_info.app_id,
                            agent_name=qualified_agent,
                            user_open_id=message.user_id,
                            chat_id=message.chat_id or "",
                            is_group=is_group,
                            action="mention_unresolved",
                            detail=f"tokens={merged_tokens!r}",
                        )
                        if try_feishu_single_hint_slot(
                            str(getattr(message, "message_id", "") or "")
                        ):
                            hint_u = format_feishu_unresolved_mentions_hint(merged_tokens)
                            un_out = OutboundMessage(
                                chat_id=message.chat_id,
                                content=format_feishu_card(
                                    hint_u, agent_name=self.agent_info.name
                                ),
                                message_type="interactive",
                            )
                            await adapter.send_message(un_out)
                        info(
                            f"[Worker {self.agent_info.name}] 群 @ 无法映射到 Agent，已静默跳过 | "
                            f"trace={trace_id} tokens={merged_tokens!r}"
                        )
                        return
                    if qualified_agent not in mention_targets:
                        feishu_inbound(
                            transport="websocket_worker",
                            tenant_id=tenant_id,
                            feishu_app_id=self.agent_info.app_id,
                            agent_name=qualified_agent,
                            user_open_id=message.user_id,
                            chat_id=message.chat_id or "",
                            is_group=is_group,
                            action="mention_other_agent_skip",
                            detail=f"targets={sorted(mention_targets)!r}",
                        )
                        info(
                            f"[Worker {self.agent_info.name}] 群消息 @ 目标非本 Agent，跳过 | "
                            f"self={qualified_agent!r} targets={sorted(mention_targets)!r}"
                        )
                        return

                agent_resolved = resolve_agent_for_http_webhook(
                    ctx, router, valid_agents, has_m, tenant_id=tenant_id
                )
                if not agent_resolved:
                    error(
                        f"[Worker {self.agent_info.name}] routing ambiguous | "
                        f"trace={trace_id} candidates={valid_agents!r}"
                    )
                    try:
                        hint_t = format_feishu_ambiguous_routing_hint(
                            valid_agents,
                            tenant_id=tenant_id,
                            app_id_short=str(self.agent_info.app_id[:16]),
                        )
                        amb_out = OutboundMessage(
                            chat_id=message.chat_id,
                            content=format_feishu_card(hint_t, agent_name="SmartClaw"),
                            message_type="interactive",
                        )
                        await adapter.send_message(amb_out)
                        feishu_inbound(
                            transport="websocket_worker",
                            tenant_id=tenant_id,
                            feishu_app_id=self.agent_info.app_id,
                            agent_name="",
                            user_open_id=message.user_id,
                            chat_id=message.chat_id or "",
                            is_group=is_group,
                            action="routing_ambiguous",
                            detail=f"candidates={len(valid_agents)}",
                        )
                    except Exception as _ex:
                        warning(
                            f"[Worker {self.agent_info.name}] ambiguous 提示发送失败: {_ex}"
                        )
                    return

                if not AuthPolicyManager.should_dispatch_feishu_agent(
                    ws_policy_manager, self.agent_info.name, ctx, has_m
                ):
                    feishu_inbound(
                        transport="websocket_worker",
                        tenant_id=tenant_id,
                        feishu_app_id=self.agent_info.app_id,
                        agent_name=agent_resolved,
                        user_open_id=message.user_id,
                        chat_id=message.chat_id or "",
                        is_group=is_group,
                        action="policy_blocked",
                        detail="agent_policy",
                        roles=resolve_feishu_roles(tenant_id, message.user_id, cfg),
                    )
                    info(f"[Worker {self.agent_info.name}] 策略拦截 agent_policy")
                    return

                content_clean = strip_feishu_mentions_for_model(content) or re.sub(
                    r"<@_user_\d+>", "", content
                ).strip()
                roles = resolve_feishu_roles(tenant_id, message.user_id, cfg)

                if _is_auth_current_user_command(content_clean):
                    session_id_combo = build_feishu_session_id(
                        user_open_id=message.user_id,
                        app_id=self.agent_info.app_id,
                        chat_id=message.chat_id or "",
                        is_group=is_group,
                    )
                    response_text = (
                        "当前飞书用户身份：\n\n"
                        f"- tenant: `{tenant_id}`\n"
                        f"- agent: `{agent_resolved}`\n"
                        f"- open_id: `{message.user_id}`\n"
                        f"- roles: `{', '.join(roles) if roles else 'default'}`\n"
                        f"- session_id: `{session_id_combo}`\n"
                        f"- app_id: `{self.agent_info.app_id[:10]}...`\n\n"
                        "授予管理员角色示例：\n"
                        f"`smartclaw auth roles set {message.user_id} "
                        f"--tenant {tenant_id} --roles tenant_admin,developer`"
                    )
                    outbound = OutboundMessage(
                        chat_id=feishu_reply_chat_id(
                            user_open_id=message.user_id,
                            chat_id=message.chat_id or "",
                            is_group=is_group,
                        ),
                        content=format_feishu_card(response_text, agent_name=self.agent_info.name),
                        message_type="interactive",
                    )
                    await adapter.send_message(outbound)
                    feishu_inbound(
                        transport="websocket_worker",
                        tenant_id=tenant_id,
                        feishu_app_id=self.agent_info.app_id,
                        agent_name=agent_resolved,
                        user_open_id=message.user_id,
                        chat_id=message.chat_id or "",
                        is_group=is_group,
                        action="auth_current_user",
                        roles=roles,
                    )
                    info(
                        f"[Worker {self.agent_info.name}] auth_current_user replied | "
                        f"trace={trace_id} tenant={tenant_id} user={message.user_id}"
                    )
                    return

                typing_id = None
                if message.message_id:
                    try:
                        typing_id = await adapter.add_typing_indicator(message.message_id)
                    except Exception:
                        pass

                session_id = build_feishu_session_id(
                    user_open_id=message.user_id,
                    app_id=self.agent_info.app_id,
                    chat_id=message.chat_id or "",
                    is_group=is_group,
                )

                info(
                    f"[Worker {self.agent_info.name}] dispatch | "
                    f"trace={trace_id} tenant={tenant_id} agent={agent_resolved} "
                    f"user={message.user_id} session={session_id} group={is_group}"
                )
                feishu_inbound(
                    transport="websocket_worker",
                    tenant_id=tenant_id,
                    feishu_app_id=self.agent_info.app_id,
                    agent_name=agent_resolved,
                    user_open_id=message.user_id,
                    chat_id=message.chat_id or "",
                    is_group=is_group,
                    action="dispatch",
                    roles=roles,
                )

                response = await runner.process_message(
                    user_id=message.user_id,
                    channel=ChannelType.FEISHU,
                    content=content_clean,
                    session_id=session_id,
                    is_group=is_group,
                    tenant_id=tenant_id,
                )
                info(
                    f"[Worker {self.agent_info.name}] agent completed | "
                    f"trace={trace_id} reply_len={len(response) if response else 0}"
                )
                
                # 移除 Typing Indicator
                if typing_id:
                    try:
                        await adapter.remove_typing_indicator(typing_id)
                    except Exception:
                        pass
                
                # 发送响应
                if response:
                    response_text = (
                        getattr(response, "content", str(response))
                        if not isinstance(response, str)
                        else response
                    )
                    outbound = OutboundMessage(
                        chat_id=feishu_reply_chat_id(
                            user_open_id=message.user_id,
                            chat_id=message.chat_id or "",
                            is_group=is_group,
                        ),
                        content=format_feishu_card(
                            response_text,
                            agent_name=self.agent_info.name,
                        ),
                        message_type="interactive",
                    )
                    send_ok = await adapter.send_message(outbound)
                    if send_ok:
                        info(
                            f"[Worker {self.agent_info.name}] reply sent | "
                            f"trace={trace_id} preview={safe_preview(response, 80)!r}"
                        )
                    else:
                        error(f"[Worker {self.agent_info.name}] 消息回复失败")
                else:
                    info(f"[Worker {self.agent_info.name}] 消息处理返回空响应")
            except Exception as e:
                error(f"[Worker {self.agent_info.name}] 消息处理异常: {e}")

        # 启动飞书 WebSocket
        await adapter.start_listening(message_handler)
        info(f"[Worker {self.agent_info.name}] 飞书 WebSocket 已连接")

        # 保持运行
        while self._running.value == 1:
            await asyncio.sleep(1)

        # 停止
        await adapter.stop()
        await runner.stop()


class MultiProcessFeishuService:
    """
    多进程飞书服务
    
    管理多个 Worker 进程，每个进程运行一个飞书 App + Agent
    """

    def __init__(self):
        self.workers: dict[str, FeishuWorker] = {}
        self._manager = None
        self._shared_state = None

    def _init_manager(self):
        """延迟初始化 Manager"""
        if self._manager is None:
            self._manager = mp.Manager()
            self._shared_state = self._manager.dict()

    def _load_agent_configs(self) -> list[AgentInfo]:
        """加载所有 Agent 配置"""
        agents = []
        seen_names: set[str] = set()

        # 使用统一的路径查找
        agents_dirs = paths.get_agents_dirs()

        # 如果都不存在则提示
        if not any(d.exists() for d in agents_dirs):
            warning("未找到 Agent 配置目录")
            return agents

        # 加载全局飞书配置
        config = get_config()
        default_feishu = {}
        if hasattr(config, "channels") and hasattr(config.channels, "feishu"):
            feishu = config.channels.feishu
            # 优先从多账号结构取默认账号（与 loader/server 单进程路径一致）；
            # 兼容旧版顶层 app_id/app_secret 字段。
            acc = feishu.get_default_account() if hasattr(feishu, "get_default_account") else None
            if acc and acc.app_id:
                default_feishu = {"app_id": acc.app_id, "app_secret": acc.app_secret}
            elif hasattr(feishu, "app_id") and feishu.app_id:
                default_feishu = {"app_id": feishu.app_id, "app_secret": feishu.app_secret}

        # 解密函数（在循环外定义，只导入一次）
        _decrypt_cache: dict[str, str] = {}

        def _decrypt_if_needed(value: str) -> str:
            """解密 ENC: 前缀的敏感信息（带缓存）"""
            if not value:
                return ""
            if value.startswith("ENC:"):
                if value in _decrypt_cache:
                    return _decrypt_cache[value]
                try:
                    from smartclaw.agent.manager import AgentManager
                    mgr = AgentManager()
                    decrypted = mgr._decrypt(value[4:])
                    _decrypt_cache[value] = decrypted
                    return decrypted
                except Exception as e:
                    warning(f"解密失败，返回空值以触发全局凭证回退: {e}")
                    return ""
            return value

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
                        data = json.load(f)

                    if not data.get("enabled", True):
                        continue

                    agent_name = data.get("name", path_agent_name)

                    # 获取飞书配置
                    feishu_cfg = data.get("feishu", {})
                    app_id = feishu_cfg.get("app_id") or default_feishu.get("app_id")
                    agent_secret = feishu_cfg.get("app_secret")
                    default_secret = default_feishu.get("app_secret")
                    app_secret = agent_secret or default_secret

                    # 解密
                    app_secret = _decrypt_if_needed(app_secret)
                    if not app_secret and default_secret and default_secret != agent_secret:
                        app_secret = _decrypt_if_needed(default_secret)

                    if not app_id or not app_secret:
                        warning(f"Agent {agent_name} 缺少飞书配置，跳过")
                        continue

                    cfg = get_config()
                    tenant_id = (
                        data.get("tenant_id")
                        or (getattr(cfg.auth, "tenant_by_app_id", {}) or {}).get(app_id)
                        or getattr(cfg.auth, "tenant_default", "default")
                        or path_tenant
                        or "default"
                    )
                    tenant_id = normalize_tenant_id(tenant_id)
                    qualified_name = tenant_agent_key(agent_name, tenant_id)
                    if qualified_name in seen_names:
                        continue
                    seen_names.add(qualified_name)

                    # 构建 llm_config：先解密 Agent 密钥，再与 config.toml [llm] 合并
                    llm_cfg = dict(data.get("llm") or {})
                    api_key = llm_cfg.get("api_key", "")
                    if api_key:
                        llm_cfg["api_key"] = _decrypt_if_needed(api_key)
                    g_llm = tenant_llm_config_as_merge_dict(cfg, tenant_id)
                    llm_cfg = merge_agent_llm_with_global(llm_cfg, g_llm)
                    llm_cfg["display_name"] = data.get("display_name", agent_name)
                    llm_cfg = normalize_agent_llm_dict(llm_cfg)

                    sandbox_cfg = data.get("sandbox", {})
                    agent_info = AgentInfo(
                        name=agent_name,
                        tenant_id=tenant_id,
                        display_name=data.get("display_name", agent_name),
                        app_id=app_id,
                        app_secret=app_secret,
                        llm_config=llm_cfg,
                        sandbox_enabled=sandbox_cfg.get("enabled", True),
                        sandbox_type=sandbox_cfg.get("type", "docker"),
                        # OpenClaw 风格安全配置
                        sandbox_security_mode=sandbox_cfg.get("security_mode", True),
                        sandbox_network_mode=sandbox_cfg.get("network_mode", "none"),
                        sandbox_container_user=sandbox_cfg.get("user", "1000:1000"),
                        sandbox_read_only_root=sandbox_cfg.get("read_only_root", True),
                        sandbox_pids_limit=sandbox_cfg.get("pids_limit", 256),
                        workspace=str(data.get("workspace") or ""),
                    )
                    agents.append(agent_info)
                    info(f"加载 Agent: {qualified_name} (App: {app_id[:10]}...)")

                except json.JSONDecodeError as e:
                    error(f"Agent {config_file} 配置文件格式错误: {e}")
                except Exception as e:
                    error(f"加载 Agent {config_file} 失败: {e}")

        return agents

    def start(self) -> None:
        """启动所有 Worker"""
        agents = self._load_agent_configs()

        if not agents:
            warning("没有 Agent 需要启动")
            return

        info(f"准备启动 {len(agents)} 个 Worker 进程...")

        for agent_info in agents:
            # 检查是否已有相同 app_id 的 worker
            existing = [w for w in self.workers.values() 
                       if w.agent_info.app_id == agent_info.app_id]
            if existing:
                info(f"跳过 {agent_info.name}，App {agent_info.app_id[:10]}... 已存在 Worker")
                continue

            # 创建并启动 Worker
            worker = FeishuWorker(agent_info)
            worker.start()
            self.workers[agent_info.name] = worker
            info(f"Worker {agent_info.name} 已启动 (PID: {worker.pid})")

        info(f"服务已就绪，共 {len(self.workers)} 个 Worker 运行中")

    def stop(self) -> None:
        """停止所有 Worker"""
        info("正在停止所有 Worker...")
        
        for name, worker in self.workers.items():
            try:
                worker._running.value = 0
                worker.terminate()
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.kill()
                info(f"Worker {name} 已停止")
            except Exception as e:
                error(f"停止 Worker {name} 失败: {e}")

        self.workers.clear()
        info("所有 Worker 已停止")

    def status(self) -> dict:
        """获取服务状态"""
        return {
            "workers": len(self.workers),
            "running": [name for name, w in self.workers.items() if w.is_alive()],
            "dead": [name for name, w in self.workers.items() if not w.is_alive()],
        }


# 全局实例
_service: Optional[MultiProcessFeishuService] = None


def start_service() -> MultiProcessFeishuService:
    """启动多进程服务"""
    global _service
    
    if _service is not None:
        warning("服务已在运行")
        return _service

    _service = MultiProcessFeishuService()
    _service.start()
    return _service


def stop_service() -> None:
    """停止服务"""
    global _service
    
    if _service is None:
        return
    
    _service.stop()
    _service = None


def get_service() -> Optional[MultiProcessFeishuService]:
    """获取服务实例"""
    return _service


async def main() -> None:
    """主入口"""
    service = start_service()
    
    # 等待中断信号
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        info("收到中断信号")
    finally:
        stop_service()


if __name__ == "__main__":
    asyncio.run(main())
