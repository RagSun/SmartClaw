"""
SmartClaw FastAPI 服务模块

提供 webhook 接口，接收飞书和企业微信的回调消息，
并路由到对应的 Agent 进行处理。
"""

from contextlib import asynccontextmanager
import json
from typing import Any, AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from smartclaw.agent.router import AgentRouter
from smartclaw.agent.runner import AgentRunner
from smartclaw.agent.policy import PolicyManager
from smartclaw.agent.session import SessionManager
from smartclaw.audit.logger import feishu_inbound
from smartclaw.auth.policy_manager import AuthPolicyManager
from smartclaw.auth.tool_gate import resolve_feishu_roles
from smartclaw.channel.base import OutboundMessage
from smartclaw.channel.feishu import FeishuAdapter
from smartclaw.channel.wecom import WeComAdapter
from smartclaw.config.loader import get_config, tenant_llm_config_as_merge_dict
from smartclaw.auth.platform import PlatformAuthAdapter
from smartclaw.console import error, info, warning
from smartclaw.interfaces import AgentConfig, ChannelType, SandboxBackend
from smartclaw.logging_utils import safe_preview
from smartclaw.llm.base import (
    merge_agent_llm_with_global,
    normalize_agent_llm_dict,
    resolved_model_name_from_llm_dict,
)
from smartclaw.server_monitoring import router as monitoring_router
from smartclaw.tenancy.api import router as tenant_admin_router
from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

# 飞书 HTTP 路由与策略（Harness L2，与 feishu_ws_server 语义对齐）
_feishu_router = AgentRouter()
_http_policy_manager = PolicyManager()
_agents: dict[str, AgentRunner] = {}
_session_managers: dict[str, SessionManager] = {}
_agent_to_app_id: dict[str, str] = {}
_channel_adapters: dict[str, Any] = {}
# 飞书 HTTP：支持多 app_id（多机器人），与 channels.feishu.accounts 一致
_feishu_adapters: list[FeishuAdapter] = []
_feishu_by_app_id: dict[str, FeishuAdapter] = {}


def _resolve_feishu_http_adapter(
    body: dict[str, Any],
    decrypt_hit: FeishuAdapter | None,
) -> FeishuAdapter | None:
    """按解密命中或 header.app_id 选择发信用 FeishuAdapter；仅一个机器人时可省略 app_id。"""
    if decrypt_hit is not None:
        return decrypt_hit
    aid = str((body.get("header") or {}).get("app_id") or "").strip()
    if aid and aid in _feishu_by_app_id:
        return _feishu_by_app_id[aid]
    if len(_feishu_adapters) == 1:
        return _feishu_adapters[0]
    return _channel_adapters.get("feishu")


async def load_agents() -> None:
    """
    加载所有 Agent 配置

    从 agents 目录加载所有 Agent
    """
    import json

    import smartclaw.paths as paths

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

                # 解密 LLM 后合并 config.toml 全局/tenant [llm]
                llm_cfg = dict(agent_data.get("llm") or {})
                api_key = llm_cfg.get("api_key", "")
                if api_key and api_key.startswith("ENC:"):
                    from smartclaw.agent.manager import AgentManager

                    llm_cfg["api_key"] = AgentManager()._decrypt(api_key[4:])

                g_llm = tenant_llm_config_as_merge_dict(cfg, tenant_id)
                agent_data["llm"] = normalize_agent_llm_dict(
                    merge_agent_llm_with_global(llm_cfg, g_llm)
                )

                # 创建 AgentConfig
                config = AgentConfig(
                    name=agent_name,
                    description=agent_data.get("description", ""),
                    channel=ChannelType(agent_data.get("channel", "feishu")),
                    model_provider=agent_data.get("llm", {}).get("provider", "openai"),
                    model_name=resolved_model_name_from_llm_dict(
                        agent_data.get("llm", {}), "gpt-4"
                    ),
                    sandbox_enabled=agent_data.get("sandbox", {}).get("enabled", True),
                    sandbox_backend_type=SandboxBackend(agent_data.get("sandbox", {}).get("type", "firecracker")),
                )

                # 创建会话管理器
                session_manager = SessionManager(agent_id=agent_name, tenant_id=tenant_id)
                _session_managers[qualified_name] = session_manager

                # 创建 Agent Runner
                runner = AgentRunner(
                    agent_id=agent_name,
                    config=config,
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
                feishu_cfg = agent_data.get("feishu", {}) or {}
                app_id = str(feishu_cfg.get("app_id") or "").strip()
                if app_id:
                    _agent_to_app_id[qualified_name] = app_id

                info(f"加载 Agent: {qualified_name} (channel={config.channel.value})")

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


def init_channel_adapters() -> None:
    """初始化渠道适配器"""
    _feishu_adapters.clear()
    _feishu_by_app_id.clear()
    # 从配置加载飞书适配器（多账号 / 多机器人）
    try:
        config = get_config()
        if hasattr(config, "channels") and hasattr(config.channels, "feishu"):
            feishu = config.channels.feishu
            for acc in feishu.accounts.values():
                aid = (acc.app_id or "").strip()
                sec = (acc.app_secret or "").strip()
                if not aid or not sec:
                    continue
                ad = FeishuAdapter(
                    {
                        "app_id": aid,
                        "app_secret": sec,
                        "encrypt_key": (acc.encrypt_key or "").strip(),
                    }
                )
                if ad.is_configured():
                    _feishu_by_app_id[aid] = ad
            _feishu_adapters.clear()
            _feishu_adapters.extend(_feishu_by_app_id.values())
            default_acc = feishu.get_default_account()
            if default_acc and default_acc.app_id.strip() in _feishu_by_app_id:
                _channel_adapters["feishu"] = _feishu_by_app_id[default_acc.app_id.strip()]
            elif _feishu_by_app_id:
                _channel_adapters["feishu"] = next(iter(_feishu_by_app_id.values()))
            if _feishu_by_app_id:
                info(f"飞书适配器已配置 ({len(_feishu_by_app_id)} 个 app_id)")
    except Exception as e:
        error(f"加载飞书适配器失败: {e}")

    # 从配置加载企业微信适配器
    try:
        config = get_config()
        wecom_config = {}
        if hasattr(config, "channels") and hasattr(config.channels, "wecom"):
            wecom = config.channels.wecom
            wecom_config = {
                "corp_id": getattr(wecom, "corp_id", ""),
                "agent_id": getattr(wecom, "agent_id", ""),
                "secret": getattr(wecom, "secret", ""),
            }

        wecom_adapter = WeComAdapter(**wecom_config)
        if wecom_adapter.is_configured:
            _channel_adapters["wecom"] = wecom_adapter
            info("企业微信适配器已配置")
    except Exception as e:
        warning(f"企业微信未配置或加载失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    应用生命周期管理

    启动时初始化资源，关闭时清理资源。
    """
    info("SmartClaw 服务启动中...")

    # 初始化渠道适配器
    init_channel_adapters()

    # 加载 Agent
    await load_agents()

    # 启动 Agent
    await start_agents()

    info(f"SmartClaw 服务已就绪 ({len(_agents)} Agent)")

    yield

    info("SmartClaw 服务关闭中...")

    # 停止 Agent
    await stop_agents()

    info("SmartClaw 服务已停止")


# 创建 FastAPI 应用
app = FastAPI(
    title="SmartClaw",
    description="生产级企业 AI Agent 平台",
    version="0.1.0",
    lifespan=lifespan,
)


def _max_request_bytes() -> int:
    try:
        return int(getattr(get_config().server, "max_request_bytes", 0) or 0)
    except Exception:
        return 0


def _body_too_large(raw_body: bytes) -> bool:
    """已读入的 body 是否超过 ``server.max_request_bytes``（0=不限制）。"""
    limit = _max_request_bytes()
    return limit > 0 and len(raw_body) > limit


@app.middleware("http")
async def _limit_request_body(request: Request, call_next):
    """边缘防护：基于 Content-Length 的请求体大小上限（413）。

    在路由处理（JSON 解析 / 飞书解密 / Agent 主流程）之前，凭 Content-Length 快速
    挡掉声明超大的 payload，抑制内存放大型 DoS——零 body 读取，不触碰 ASGI receive
    流，因此绝不影响下游 ``await request.body()``。上限来自 ``server.max_request_bytes``
    （0=不限制）。无 Content-Length（chunked）的请求由各 webhook 处理器在读入 body
    后用 :func:`_body_too_large` 兜底校验。
    """
    max_bytes = _max_request_bytes()
    if max_bytes > 0:
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > max_bytes:
                    return Response(status_code=413, content="Request entity too large")
            except ValueError:
                return Response(status_code=400, content="Invalid Content-Length")
    return await call_next(request)


# 注册监控路由
app.include_router(monitoring_router)
# 注册租户生命周期管理路由（/api/admin/tenants）
app.include_router(tenant_admin_router)


@app.get("/")
async def root() -> dict[str, Any]:
    """健康检查端点"""
    return {
        "name": "SmartClaw",
        "version": "0.1.0",
        "status": "running",
        "agents": len(_agents),
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    """健康检查端点"""
    return {"status": "healthy"}


# ==================== 飞书 Webhook ====================


@app.post("/webhook/feishu")
async def feishu_webhook(request: Request) -> Response:
    """
    飞书 Webhook 回调端点

    接收飞书推送的消息事件，路由到对应 Agent 处理。
    """
    try:
        raw_body = await request.body()
    except Exception as e:
        error(f"读取请求体失败: {e}")
        return Response(status_code=400)
    # 边缘防护兜底：无 Content-Length（chunked）时按实际读入字节校验上限。
    if _body_too_large(raw_body):
        error("飞书 Webhook 请求体超过 server.max_request_bytes 上限")
        return Response(status_code=413)
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception as e:
        error(f"解析请求体失败: {e}")
        return Response(status_code=400)

    # URL 验证 - 飞书配置回调 URL 时会发送
    if body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        info(f"飞书 URL 验证: {challenge}")
        return JSONResponse({"challenge": challenge})

    cfg = get_config()
    q_token = request.query_params.get("token")
    hdr_token = request.headers.get("X-SmartClaw-Webhook-Token")
    if not PlatformAuthAdapter.verify_feishu_webhook(q_token, hdr_token, cfg):
        error("飞书 Webhook 鉴权失败")
        return Response(status_code=401)

    if not _feishu_adapters:
        return JSONResponse(
            {"error": "飞书未配置"},
            status_code=503,
        )

    enc_keys = [getattr(a, "encrypt_key", "") or "" for a in _feishu_adapters]
    ok_sig, sig_reason = PlatformAuthAdapter.verify_lark_webhook_signature_if_present(
        raw_body, request.headers, enc_keys
    )
    if not ok_sig:
        error(f"飞书 Webhook 签名校验失败: {sig_reason}")
        return Response(status_code=401)

    try:
        from smartclaw.auth.feishu_payload import decrypt_feishu_body_try_adapters
        from smartclaw.channel.feishu_context import (
            parse_feishu_event_body,
            resolve_agent_for_http_webhook,
            strip_feishu_mentions_for_model,
        )
        from smartclaw.feishu.formatter import format_feishu_card

        decrypt_hit: FeishuAdapter | None = None
        if cfg.auth.feishu_decrypt_webhook:
            body_dec, decrypt_hit = decrypt_feishu_body_try_adapters(body, True, _feishu_adapters)
            if (
                isinstance(body, dict)
                and body.get("encrypt")
                and isinstance(body_dec, dict)
                and body_dec.get("encrypt")
            ):
                error("飞书事件解密失败，请核对各账号 encrypt_key")
                return Response(status_code=400)
            body = body_dec
        else:
            default_ad = _channel_adapters.get("feishu")
            enc_key_fallback = getattr(default_ad, "encrypt_key", "") if default_ad else ""
            body = PlatformAuthAdapter.maybe_decrypt_feishu_body(body, enc_key_fallback, cfg)

        adapter = _resolve_feishu_http_adapter(body, decrypt_hit)
        if not adapter:
            error("无法匹配飞书应用（多机器人时请确认事件 header.app_id，或仅用单机器人）")
            return JSONResponse(
                {"error": "飞书应用未匹配：多 app 时需 header.app_id"},
                status_code=503,
            )

        if not PlatformAuthAdapter.check_webhook_not_replay(body, cfg):
            error("飞书 Webhook 疑似重放")
            return Response(status_code=401)

        declared_tenant = request.headers.get("X-SmartClaw-Tenant-Id")
        tenant_id = AuthPolicyManager.resolve_tenant_for_feishu(
            getattr(adapter, "app_id", None) or None,
            cfg,
        )
        ok_t, reason_t = AuthPolicyManager.verify_declared_tenant(declared_tenant, tenant_id, cfg)
        if not ok_t:
            error(f"租户校验失败: {reason_t}")
            return Response(status_code=403)

        ctx = parse_feishu_event_body(body)
        if ctx is None:
            error("飞书消息解析失败")
            return Response(status_code=400)

        app_id = getattr(adapter, "app_id", "") or ""
        agent_names = [
            name for name, aid in _agent_to_app_id.items()
            if aid == app_id and getattr(_agents.get(name), "tenant_id", tenant_id) == tenant_id
        ]
        if not agent_names:
            agent_names = [
                name for name, runner in _agents.items()
                if getattr(runner, "tenant_id", "default") == tenant_id
            ]
        if not agent_names:
            return Response(status_code=503)

        has_m = AuthPolicyManager.compute_feishu_has_mention(ctx, agent_names)

        if ctx.is_group and not has_m:
            ack_text = "💬 收到消息，请在消息前 @ 我以唤醒助手"
            ack_card = format_feishu_card(ack_text, agent_name="Bot")
            ack_out = OutboundMessage(
                chat_id=ctx.chat_id,
                content=ack_card,
                message_type="interactive",
            )
            await adapter.send_message(ack_out)
            return JSONResponse({"status": "ignored", "reason": "mention_required"})

        agent_name = resolve_agent_for_http_webhook(
            ctx, _feishu_router, agent_names, has_m, tenant_id=tenant_id
        )
        if not agent_name:
            feishu_inbound(
                transport="webhook",
                tenant_id=tenant_id,
                feishu_app_id=getattr(adapter, "app_id", "") or "",
                agent_name="",
                user_open_id=ctx.user_open_id,
                chat_id=ctx.chat_id,
                is_group=ctx.is_group,
                action="routing_ambiguous",
                detail=f"candidates={len(agent_names)}",
            )
            return JSONResponse(
                {
                    "error": "ambiguous_routing",
                    "message": "无法唯一确定要使用的 Agent，请在消息中 @ 对应助手或缩小候选范围。",
                    "candidates": sorted(set(agent_names)),
                },
                status_code=409,
            )

        if not AuthPolicyManager.should_dispatch_feishu_agent(
            _http_policy_manager, agent_name, ctx, has_m
        ):
            feishu_inbound(
                transport="webhook",
                tenant_id=tenant_id,
                feishu_app_id=getattr(adapter, "app_id", "") or "",
                agent_name=agent_name,
                user_open_id=ctx.user_open_id,
                chat_id=ctx.chat_id,
                is_group=ctx.is_group,
                action="policy_blocked",
                detail="agent_policy",
                roles=resolve_feishu_roles(tenant_id, ctx.user_open_id, cfg),
            )
            return JSONResponse({"status": "ignored", "reason": "agent_policy"})

        runner = _agents.get(agent_name)
        if not runner:
            return JSONResponse({"error": f"Agent 不存在: {agent_name}"}, status_code=404)

        content_for_llm = strip_feishu_mentions_for_model(ctx.content_text)
        session_key = ctx.chat_id if ctx.is_group else ctx.user_open_id
        trace_id = (ctx.message_id or session_key or "webhook")[:16]

        info(
            "[Feishu Webhook] dispatch | "
            f"trace={trace_id} app_id={app_id[:10]}... tenant={tenant_id} "
            f"agent={agent_name} user={ctx.user_open_id} session={session_key} "
            f"group={ctx.is_group} has_mention={has_m} candidates={len(agent_names)} "
            f"content_preview={safe_preview(content_for_llm, 100)!r}"
        )

        roles = resolve_feishu_roles(tenant_id, ctx.user_open_id, cfg)
        feishu_inbound(
            transport="webhook",
            tenant_id=tenant_id,
            feishu_app_id=getattr(adapter, "app_id", "") or "",
            agent_name=agent_name,
            user_open_id=ctx.user_open_id,
            chat_id=ctx.chat_id,
            is_group=ctx.is_group,
            action="dispatch",
            roles=roles,
        )

        response_text = await runner.process_message(
            user_id=ctx.user_open_id,
            channel=ChannelType.FEISHU,
            content=content_for_llm,
            session_id=session_key,
            is_group=ctx.is_group,
            tenant_id=tenant_id,
        )

        receive_id = ctx.chat_id if ctx.is_group else ctx.user_open_id
        outbound = OutboundMessage(
            chat_id=receive_id,
            content=response_text,
        )

        success = await adapter.send_message(outbound)

        if success:
            info(
                "[Feishu Webhook] reply sent | "
                f"trace={trace_id} agent={agent_name} reply_len={len(str(response_text or ''))}"
            )
            return JSONResponse({"status": "ok"})
        return JSONResponse({"error": "发送失败"}, status_code=500)

    except Exception as e:
        error(f"处理飞书消息失败: {e}")
        return Response(status_code=500)


@app.post("/webhook/wecom")
async def wecom_webhook(request: Request) -> Response:
    """
    企业微信 Webhook 回调端点

    接收企业微信推送的消息事件。
    """
    adapter = _channel_adapters.get("wecom")

    if not adapter:
        return JSONResponse(
            {"error": "企业微信未配置"},
            status_code=503,
        )

    # 验证请求
    if not await adapter.verify_webhook(request):
        return JSONResponse(
            {"error": "验证失败"},
            status_code=401,
        )

    # 解析消息
    try:
        inbound_msg = await adapter.parse_message(request)
    except Exception as e:
        error(f"解析企业微信消息失败: {e}")
        return Response(status_code=400)

    # URL 验证
    if inbound_msg.message_type == "url_verification":
        return Response(content=inbound_msg.content, media_type="text/plain")

    # 查找对应的 Agent：wecom 为全局单 App，按消息正文 @提及 路由到 wecom 渠道 Agent；
    # 无 @ 或未命中时回落到 default（若 default 为 wecom 渠道）或首个 wecom Agent。
    wecom_qnames = [
        qn for qn, r in _agents.items()
        if getattr(r.config, "channel", None) == ChannelType.WECOM
        and getattr(r, "tenant_id", "default") == "default"
    ]
    if not wecom_qnames:
        return Response(status_code=200)  # 无 wecom 渠道 Agent，静默

    resolved = _feishu_router.route_with_mentions(
        inbound_msg.content,
        inbound_msg.user_id,
        None,
        is_group=False,
        tenant_id="default",
    )
    agent_name = next((n for n in resolved if n in wecom_qnames), None)
    if not agent_name:
        agent_name = "default" if "default" in wecom_qnames else wecom_qnames[0]
    runner = _agents.get(agent_name)

    if not runner:
        return Response(status_code=200)

    # 处理消息
    try:
        response = await runner.process_message(
            user_id=inbound_msg.user_id,
            channel=ChannelType.WECOM,
            content=inbound_msg.content,
        )

        # 发送响应

        sessions = await runner.session_manager.list_active()
        user_sessions = [s for s in sessions if s.user_id == inbound_msg.user_id]

        if user_sessions:
            session = user_sessions[-1]
            await adapter.send_message(
                session.to_context(),
                response,
            )

        return Response(status_code=200)

    except Exception as e:
        error(f"处理企业微信消息失败: {e}")
        return Response(status_code=500)


# ==================== Agent 管理 API ====================


@app.get("/api/agents")
async def list_agents() -> dict[str, Any]:
    """列出所有 Agent"""
    agents = []
    for name, runner in _agents.items():
        agents.append(
            {
                "name": name,
                "status": runner.status.value,
                "channel": runner.config.channel.value,
            }
        )

    return {
        "agents": agents,
        "total": len(agents),
    }


@app.get("/api/agents/{agent_name}")
async def get_agent(agent_name: str) -> dict[str, Any]:
    """获取 Agent 详情"""
    runner = _agents.get(agent_name)

    if not runner:
        return JSONResponse(
            {"error": f"Agent 不存在: {agent_name}"},
            status_code=404,
        )

    return {
        "name": agent_name,
        "status": runner.status.value,
        "channel": runner.config.channel.value,
        "model": f"{runner.config.model_provider}/{runner.config.model_name}",
    }


# ==================== 会话管理 API ====================


@app.get("/api/sessions")
async def list_sessions(agent_name: str = "") -> dict[str, Any]:
    """列出活跃会话"""
    sessions = []

    for name, manager in _session_managers.items():
        if agent_name and name != agent_name:
            continue

        active = await manager.list_active()
        for session in active:
            sessions.append(
                {
                    "session_id": session.session_id,
                    "agent_id": session.agent_id,
                    "user_id": session.user_id,
                    "status": session.status.value,
                }
            )

    return {
        "sessions": sessions,
        "total": len(sessions),
    }


# ==================== 服务启动入口 ====================


def run_server(host: str = "0.0.0.0", port: int = 8000, workers: int = 1) -> None:
    """
    启动 SmartClaw 服务

    参数:
        host: 监听地址
        port: 监听端口
        workers: 工作进程数
    """
    import uvicorn

    info("启动 SmartClaw 服务")
    info(f"监听地址: {host}:{port}")
    info(f"工作进程: {workers}")

    uvicorn.run(
        "smartclaw.server:app",
        host=host,
        port=port,
        workers=workers,
        reload=False,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="SmartClaw 服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--workers", type=int, default=1, help="工作进程数")

    args = parser.parse_args()

    run_server(host=args.host, port=args.port, workers=args.workers)
