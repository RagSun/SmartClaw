"""
飞书 WebSocket 长连接适配器

使用飞书官方 SDK 建立长连接，无需配置 Webhook URL。
"""

import asyncio
import json
import threading
import time
import traceback
from typing import Any, Callable, Optional

from smartclaw.channel.base import ChannelAdapter, InboundMessage, OutboundMessage
from smartclaw.console import error, info, warning
from smartclaw.interfaces import ChannelType

# 飞书 SDK
try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    LARK_AVAILABLE = True
except ImportError:
    LARK_AVAILABLE = False
    error("飞书 SDK 未安装，请运行: uv pip install lark-oapi")


def _collect_image_keys_from_feishu_payload(obj: Any) -> list[str]:
    """从飞书 message.content 解析后的对象中递归收集 image_key（含 post 富文本 img 节点）。"""
    out: list[str] = []
    seen: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            ik = x.get("image_key")
            if isinstance(ik, str) and ik.strip():
                s = ik.strip()
                if s not in seen:
                    seen.add(s)
                    out.append(s)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for it in x:
                walk(it)

    walk(obj)
    return out


def _extract_text_from_feishu_post_elements(obj: Any) -> str:
    """从 post 段落结构中抽取 tag=text 的文案（多段用换行拼接）。"""
    parts: list[str] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if x.get("tag") == "text":
                t = x.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for it in x:
                walk(it)

    walk(obj)
    return "\n".join(parts) if parts else ""


def _resolve_feishu_user_visible_text(content_obj: dict[str, Any]) -> str:
    """
    解析用户侧可见正文：顶层 text、或 post（zh_cn/en_us/ja_jp）标题+段落文字。
    不含「收到图片」等系统占位符。
    """
    content_text = str(content_obj.get("text") or "").strip()
    if content_text:
        return content_text
    for lang_key in ("zh_cn", "en_us", "ja_jp"):
        block = content_obj.get(lang_key)
        if not isinstance(block, dict):
            continue
        chunks: list[str] = []
        title = block.get("title")
        if isinstance(title, str) and title.strip():
            chunks.append(title.strip())
        inner = block.get("content")
        post_txt = _extract_text_from_feishu_post_elements(inner)
        if post_txt:
            chunks.append(post_txt)
        if chunks:
            return "\n".join(chunks)
    return ""


def _lark_model_to_dict(obj: Any) -> dict[str, Any]:
    """将 lark-oapi 模型尽量转为 dict，便于读取 message_id 等字段。"""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if LARK_AVAILABLE:
        try:
            raw = lark.JSON.marshal(obj)
            if isinstance(raw, str):
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    return parsed
            elif isinstance(raw, dict):
                return raw
        except Exception:
            pass
    return {}


def _event_header_event_id(envelope: Any) -> str:
    hdr = getattr(envelope, "header", None)
    if not hdr:
        return ""
    d = _lark_model_to_dict(hdr)
    v = d.get("event_id") or getattr(hdr, "event_id", None)
    return str(v).strip() if v is not None and str(v).strip() else ""


def _resolve_feishu_ws_message_id(msg: Any, envelope: Any) -> str:
    """
    从 Message 模型解析 message_id；部分 SDK/事件形态下该字段为空，
    则用 envelope.header.event_id 生成稳定合成 ID，避免静默丢弃消息。
    """
    d = _lark_model_to_dict(msg)
    for key in ("message_id", "messageId"):
        v = d.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
        alt = getattr(msg, key, None)
        if alt is not None and str(alt).strip():
            return str(alt).strip()

    eid = _event_header_event_id(envelope)
    chat_hint = str(d.get("chat_id") or getattr(msg, "chat_id", None) or "").strip()
    ct = str(d.get("create_time") or getattr(msg, "create_time", None) or "").strip()
    if eid:
        synthetic = f"evt-{eid}"
    elif chat_hint and ct:
        synthetic = f"ct-{chat_hint}-{ct}"
    else:
        synthetic = f"ws-{time.time_ns()}"

    warning(
        "[Feishu WS] message_id 在模型中为空，已用合成 ID（可能影响「正在输入」等依赖 message_id 的接口）: "
        f"{synthetic[:64]}"
    )
    return synthetic


class FeishuWebSocketAdapter(ChannelAdapter):
    """
    飞书 WebSocket 长连接适配器

    使用飞书官方 SDK 建立长连接，自动接收消息。
    """

    channel_type = ChannelType.FEISHU
    base_url = "https://open.feishu.cn/open-apis"

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        """
        初始化飞书 WebSocket 适配器

        参数:
            config: 配置字典，包含:
                - app_id: 应用 ID
                - app_secret: 应用密钥
        """
        config = config or {}
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")

        if not LARK_AVAILABLE:
            raise ImportError("飞书 SDK 未安装")

        if not self.app_id or not self.app_secret:
            raise ValueError("飞书配置不完整，需要 app_id 和 app_secret")

        # 初始化飞书客户端
        self.client = (
            lark.Client.builder()
            .app_id(self.app_id)
            .app_secret(self.app_secret)
            .build()
        )

        self._ws_client = None
        self._ws_thread = None
        self._running = False
        self._message_callback: Optional[Callable] = None
        # start_listening 所在协程的事件循环（feishu_main / asyncio.run），用于从 WS 线程投递回调
        self._handler_loop: Optional[asyncio.AbstractEventLoop] = None

        # 消息去重缓存
        self._processed_messages: dict[str, float] = {}
        self._cache_expire_seconds = 3600

        info(f"飞书 WebSocket 适配器初始化完成，App ID: {self.app_id[:10]}...")

    def is_configured(self) -> bool:
        """是否已配置"""
        return bool(self.app_id and self.app_secret)

    def get_callback_url(self) -> str:
        """获取回调 URL（长连接模式不需要）"""
        return ""

    async def start_listening(
        self,
        message_callback: Callable[[InboundMessage], Any],
    ) -> None:
        """
        启动 WebSocket 长连接

        参数:
            message_callback: 消息回调函数
        """
        self._message_callback = message_callback
        self._running = True
        self._handler_loop = asyncio.get_running_loop()
        info(
            "[Feishu WS] message_callback 已绑定到服务主事件循环 "
            f"(loop_id={id(self._handler_loop)}, "
            f"thread={threading.current_thread().name})"
        )

        # 创建事件处理器
        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message_event)
            .build()
        )

        # 创建 WebSocket 客户端
        self._ws_client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        info("飞书 WebSocket 连接启动中...")

        # 在单独的线程中运行 WebSocket，每个连接使用独立的 event loop
        def run_ws():
            try:
                import asyncio
                # 为这个连接创建独立的 event loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # 运行事件循环来处理 WebSocket
                loop.run_until_complete(self._ws_client.start())
            except Exception as e:
                error(f"飞书 WebSocket 异常: {e}")

        self._ws_thread = threading.Thread(target=run_ws, daemon=True)
        self._ws_thread.start()

        info("飞书 WebSocket 连接已启动（后台线程）")

        # 保持运行
        while self._running:
            await asyncio.sleep(1)

    def _schedule_message_callback(self, inbound_msg: InboundMessage) -> None:
        """从飞书 SDK/WS 线程将异步 handler 投递到服务主事件循环。"""
        if not self._message_callback:
            warning("[Feishu WS] 跳过投递: 未注册 message_callback")
            return

        hl = self._handler_loop
        if not hl or hl.is_closed():
            warning(
                "[Feishu WS] handler 事件循环未就绪或已关闭，无法投递 message_callback "
                "（请确认通过 feishu_ws_server.main / adapter.start_listening 启动）"
            )
            return

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._message_callback(inbound_msg), hl
            )

            def _log_callback_result(fut: Any) -> None:
                try:
                    fut.result()
                except Exception as exc:
                    error(f"[Feishu WS] message_callback 执行异常: {exc}")

            future.add_done_callback(_log_callback_result)
            info(
                "[Feishu WS] 已投递 message_callback 至主循环 "
                f"(msg_id={inbound_msg.message_id[:24]}..., "
                f"chat={inbound_msg.chat_id[:20] if inbound_msg.chat_id else ''}..., "
                f"content_len={len(inbound_msg.content or '')})"
            )
        except Exception as e:
            error(f"[Feishu WS] run_coroutine_threadsafe 失败: {e}")

    def _handle_message_event(self, data: Any) -> None:
        """
        处理飞书消息事件

        参数:
            data: 飞书事件数据
        """
        try:
            ws_thread = threading.current_thread().name
            info(
                f"[Feishu WS] 收到事件 data_type={type(data).__name__} "
                f"thread={ws_thread}"
            )
            info(f"[Feishu WS] data 摘要: {str(data)[:500]}")
            if hasattr(data, "event"):
                info(f"[Feishu WS] data.event={data.event}")
            if hasattr(data, "header"):
                info(f"[Feishu WS] data.header={data.header}")

            if not data.event or not data.event.message:
                info("[Feishu WS] 跳过: 无有效事件或消息")
                return

            msg = data.event.message

            message_id = _resolve_feishu_ws_message_id(msg, data)

            if self._is_duplicate_message(message_id):
                info(f"重复消息已忽略: {message_id[:20]}...")
                return

            # 标记消息已处理
            self._mark_message_processed(message_id)

            # 清理过期缓存
            self._cleanup_message_cache()

            # 解析消息
            sender = getattr(data.event, "sender", None)
            user_id = ""
            user_name_val = ""
            if sender is not None:
                sid = getattr(sender, "sender_id", None)
                if sid is not None:
                    user_id = str(getattr(sid, "open_id", "") or "").strip()
                    user_name_val = str(getattr(sid, "user_id", "") or "").strip()

            # 解析消息内容
            content = msg.content
            content_obj: dict[str, Any] = {}
            if isinstance(content, str):
                try:
                    raw = json.loads(content)
                    content_obj = raw if isinstance(raw, dict) else {}
                    image_keys_found = _collect_image_keys_from_feishu_payload(
                        raw if isinstance(raw, (dict, list)) else {}
                    )
                    if image_keys_found:
                        if not hasattr(msg, "image_keys"):
                            msg.image_keys = []
                        for ik in image_keys_found:
                            if ik not in msg.image_keys:
                                msg.image_keys.append(ik)
                        info(f"[DEBUG] 收集 image_keys={msg.image_keys}")

                    content_text = (
                        _resolve_feishu_user_visible_text(content_obj)
                        if content_obj
                        else ""
                    )
                    if not content_text:
                        if "file_key" in content_obj:
                            if "media_key" in content_obj or "video_key" in content_obj:
                                content_text = f"[收到视频/音频文件: {content_obj.get('file_name', 'unknown')}]"
                            else:
                                content_text = f"[收到文件: {content_obj.get('file_name', 'unknown')}]"
                        elif image_keys_found:
                            content_text = (
                                "[收到一张图片]"
                                if len(image_keys_found) == 1
                                else f"[收到 {len(image_keys_found)} 张图片]"
                            )
                        elif "media_key" in content_obj:
                            content_text = "[收到一个富媒体(视频/音频)文件]"
                        else:
                            content_text = "[收到一条非文本消息]"
                except Exception:
                    content_text = content
                    content_obj = {}
            else:
                content_text = str(content)

            # 【关键修复1】解析 mentions 并替换文本中的占位符
            mention_names = []
            if hasattr(msg, "mentions") and msg.mentions:
                for m in msg.mentions:
                    key = getattr(m, "key", "")
                    name = getattr(m, "name", "")
                    if key and name:
                        # 把类似 "@_user_1" 替换成真实的 "@SmartClaw"
                        content_text = content_text.replace(key, f"@{name}")
                        mention_names.append(name)

            # 【关键修复2】确定 chat_type (非常重要)
            chat_type = getattr(msg, "chat_type", "")
            if not chat_type:
                chat_type = "group" if (msg.chat_id and msg.chat_id.startswith("oc_")) else "p2p"

            # 创建消息对象
            # 【关键修复】传递 image_keys（飞书图片需要通过 image_key 下载）
            inbound_msg = InboundMessage(
                message_id=message_id,
                chat_id=msg.chat_id or "",
                user_id=user_id,
                user_name=user_name_val or None,
                content=content_text,
                message_type="text",
                chat_type=chat_type,
                mentions=mention_names,
                timestamp=time.time(),
                raw_data={"event": data.event, "content_obj": content_obj},
                image_keys=getattr(msg, 'image_keys', []),
            )

            info(
                "[Feishu WS] 解析完成，准备投递: "
                f"message_id={message_id[:28]}..., chat_type={chat_type}, "
                f"user_id={user_id[:16] if user_id else ''}..., "
                f"text_preview={(content_text or '')[:60]!r}"
            )
            self._schedule_message_callback(inbound_msg)

        except Exception as e:
            error(f"处理飞书消息失败: {e}\n{traceback.format_exc()}")

    async def send_message(self, message: OutboundMessage) -> bool:
        """
        发送飞书消息

        支持 text 和 interactive (卡片) 两种类型
        """
        try:
            chat_id = message.chat_id
            is_group = chat_id.startswith("oc_")
            receive_type = "chat_id" if is_group else "open_id"

            # 根据消息类型选择格式
            if message.message_type == "interactive":
                # 卡片消息：content 已经是 JSON 字符串
                msg_type = "interactive"
                content = (
                    message.content
                    if isinstance(message.content, str)
                    else json.dumps(message.content)
                )
            else:
                # 文本消息
                msg_type = "text"
                if isinstance(message.content, str):
                    content = json.dumps({"text": message.content})
                else:
                    content = json.dumps(message.content)

            # 创建消息请求
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(chat_id)
                    .msg_type(msg_type)
                    .content(content)
                    .build()
                )
                .build()
            )

            # 发送消息
            response = self.client.im.v1.message.create(request)

            if response.success():
                info("飞书消息发送成功")
                return True
            else:
                error(f"飞书消息发送失败: code={response.code}, msg={response.msg}")
                return False

        except Exception as e:
            error(f"发送飞书消息异常: {e}")
            return False

    async def send_card(self, user_id: str, card: dict[str, Any]) -> bool:
        """发送卡片消息"""
        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id" if user_id.startswith("ou_") else "chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(user_id)
                    .msg_type("interactive")
                    .content(json.dumps(card))
                    .build()
                )
                .build()
            )

            response = self.client.im.v1.message.create(request)
            return response.success()

        except Exception as e:
            error(f"发送飞书卡片失败: {e}")
            return False

    async def get_user_info(self, user_id: str) -> dict[str, Any]:
        """获取用户信息"""
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest

            request = GetUserRequest.builder().user_id(user_id).build()

            response = self.client.contact.v3.user.get(request)

            if response.success():
                info("飞书消息发送成功")
                return response.data.user
            else:
                return {}

        except Exception as e:
            error(f"获取飞书用户信息失败: {e}")
            return {}

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        """获取会话信息"""
        try:
            from lark_oapi.api.im.v1 import GetChatRequest

            request = GetChatRequest.builder().chat_id(chat_id).build()

            response = self.client.im.v1.chat.get(request)

            if response.success():
                info("飞书消息发送成功")
                return response.data
            else:
                return {}

        except Exception as e:
            error(f"获取飞书会话信息失败: {e}")
            return {}

    async def verify_webhook(self, request: Any) -> bool:
        """验证 Webhook（长连接模式不需要）"""
        return True

    async def parse_message(self, request: Any) -> Optional[InboundMessage]:
        """解析消息（长连接模式自动处理）"""
        return None

    async def close(self) -> None:
        """关闭连接"""
        self._running = False
        # 飞书 SDK 暂不支持优雅关闭
        info("飞书 WebSocket 适配器已停止")



    async def add_typing_indicator(self, message_id: str) -> Optional[str]:
        """
        添加 Typing Indicator（emoji reaction）

        参数:
            message_id: 消息 ID

        返回:
            reaction_id 或 None
        """
        try:
            from lark_oapi.api.im.v1 import CreateMessageReactionRequest

            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    {
                        "reaction_type": {
                            "emoji_type": "Typing"  # 使用 Typing emoji
                        }
                    }
                )
                .build()
            )

            response = self.client.im.v1.message_reaction.create(request)

            if response.success():
                reaction_id = getattr(response.data, 'reaction_id', None) if hasattr(response, 'data') else None
                info(f"添加 Typing Indicator 成功: {reaction_id}")
                return reaction_id
            else:
                error(f"添加 Typing Indicator 失败: code={response.code}, msg={response.msg}")
                return None

        except Exception as e:
            error(f"添加 Typing Indicator 异常: {e}")
            return None

    async def remove_typing_indicator(self, message_id: str, reaction_id: str) -> bool:
        """
        移除 Typing Indicator

        参数:
            message_id: 消息 ID
            reaction_id: Reaction ID

        返回:
            是否成功
        """
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

            request = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )

            response = self.client.im.v1.message_reaction.delete(request)

            if response.success():
                info(f"移除 Typing Indicator 成功")
                return True
            else:
                error(f"移除 Typing Indicator 失败: code={response.code}, msg={response.msg}")
                return False

        except Exception as e:
            error(f"移除 Typing Indicator 异常: {e}")
            return False

    
    async def download_resource(self, message_id: str, file_key: str, resource_type: str, save_path: str) -> bool:
        """
        下载消息中的图片或文件资源
        
        参数:
            message_id: 消息ID
            file_key: 文件/图片KEY
            resource_type: 资源类型 'image' 或 'file'
            save_path: 保存的本地路径
            
        返回:
            是否成功
        """
        try:
            from lark_oapi.api.im.v1 import GetMessageResourceRequest
            
            request = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(file_key)
                .type(resource_type)
                .build()
            )
            
            response = self.client.im.v1.message_resource.get(request)
            
            if response.success():
                import os
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(response.file.read())
                info(f"资源下载成功: {save_path}")
                return True
            else:
                error(f"资源下载失败: {response.code} - {response.msg}")
                return False
                
        except Exception as e:
            error(f"下载资源异常: {e}")
            return False

    def _is_duplicate_message(self, message_id: str) -> bool:
        """检查消息是否重复"""
        return message_id in self._processed_messages

    def _mark_message_processed(self, message_id: str) -> None:
        """标记消息已处理"""
        self._processed_messages[message_id] = time.time()

    def _cleanup_message_cache(self) -> None:
        """清理过期缓存"""
        current = time.time()
        expired = [
            msg_id
            for msg_id, timestamp in self._processed_messages.items()
            if current - timestamp > self._cache_expire_seconds
        ]
        for msg_id in expired:
            del self._processed_messages[msg_id]

    async def send_message_to_user(
        self,
        user_id: str,
        content: str,
    ) -> bool:
        """
        发送消息给用户

        参数:
            user_id: 用户 ID (open_id)
            content: 消息内容

        返回:
            是否发送成功
        """
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            request = (
                CreateMessageRequest.builder()
                .receive_id_type("open_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(user_id)
                    .msg_type("text")
                    .content(json.dumps({"text": content}))
                    .build()
                )
                .build()
            )

            response = self.client.im.v1.message.create(request)

            if response.success():
                info("飞书消息发送成功")
                info(f"消息发送成功: {user_id}")
                return True
            else:
                error(f"消息发送失败: code={response.code}, msg={response.msg}")
                return False

        except Exception as e:
            error(f"发送消息异常: {e}")
            return False
