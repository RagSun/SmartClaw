"""Deterministic control-flow guards before model-driven execution."""

from __future__ import annotations

import re
from dataclasses import dataclass

from smartclaw.auth.tool_gate import (
    ToolSecurityContext,
    check_shell_capability_allowed,
    check_tool_allowed,
)
from smartclaw.config.loader import get_config


_AUTH_QUERY_RE = re.compile(
    r"(auth[_\s-]*current[_\s-]*user|current[_\s-]*user|whoami|我是谁|当前用户"
    r"|roles?\b|我的?权限|权限(是|有|列表|范围)?|你的?roles?|我的?角色|角色(是|有|列表|范围)?)",
    re.I | re.UNICODE,
)
_CLOUD_DOC_RE = re.compile(
    r"(飞书(云)?文档|飞书表格|飞书多维表格|在线飞书文档|在线文档|云文档|docx|document_id)",
    re.I | re.UNICODE,
)
_WORKSPACE_FILE_RE = re.compile(
    r"(保存到|保存至|存储在|存储到|存到|存入|放到|生成到|输出到|写入|写到).{0,24}"
    r"(docs|目录|文件|文件夹|工作区|workspace|\.py|\.js|\.ts|\.tsx|\.jsx|\.html|\.css|\.txt|\.md|\.json|\.ya?ml|\.toml|\.conf)"
    r"|docs\s*(目录|下|里|中|/|\\|$)"
    r"|\bwrite_file\b|\bedit_file\b|\bmkdir\b|\btouch\b"
    r"|\b(Dockerfile|docker-compose\.ya?ml|requirements\.txt|package\.json|nginx\.conf)\b",
    re.I | re.UNICODE,
)
_APP_FRAMEWORK_RE = re.compile(
    r"(streamlit|stremlit|flask|fastapi|django|uvicorn|gunicorn|vite|react|vue|next\.?js"
    r"|node\.?js|express|spring\s*boot)",
    re.I | re.UNICODE,
)
_APP_CONTEXT_RE = re.compile(
    r"(应用|项目|网站|服务|api|接口|前端|后端|全栈|可视化|交互前端)",
    re.I | re.UNICODE,
)
_APP_ACTION_RE = re.compile(
    r"(创建|开发|搭建|生成|编写|构建|配置|部署|运行|启动)",
    re.I | re.UNICODE,
)
_OPS_CONTEXT_RE = re.compile(
    r"(nginx|docker|docker\s+compose|systemctl|apt|apt-get|yum|dnf|apk|反向代理|容器|部署|上线)",
    re.I | re.UNICODE,
)
_RUNTIME_SERVICE_RE = re.compile(
    r"(启动|运行|部署|监听|开放|暴露).{0,12}(服务|端口|url|链接|地址)"
    r"|(服务|端口).{0,12}(启动|运行|监听|开放|暴露)"
    r"|端口\s*(在|为|是|:|：)?\s*\d{2,5}"
    r"|返回.{0,8}(url|URL|链接|地址)"
    r"|https?://",
    re.I | re.UNICODE,
)
_COMMAND_ACTION_RE = re.compile(r"(执行|运行|启动|安装|部署|构建|打包|测试|验证|发布)", re.I | re.UNICODE)
_COMMAND_CONTEXT_RE = re.compile(
    r"(\bexecute\b|\bexec\b|python\s+|pip\s+|conda\s+|npm\s+|pnpm\s+|yarn\s+|node\s+"
    r"|uv\s+|poetry\s+|apt\s+|apt-get\s+|yum\s+|dnf\s+|apk\s+|systemctl\s+|service\s+"
    r"|streamlit|stremlit|flask|fastapi|django|uvicorn|gunicorn|nginx|docker|docker\s+compose"
    r"|vite|react|vue|next\.?js|express)",
    re.I | re.UNICODE,
)
_APPLICATION_DELIVERY_RE = re.compile(
    r"(应用|项目|网站|服务|api|接口|前端|后端|全栈|部署|上线|容器|反向代理"
    r"|streamlit|stremlit|flask|fastapi|django|uvicorn|gunicorn|nginx|docker|docker\s+compose"
    r"|vite|react|vue|next\.?js|node\.?js|express|spring\s*boot)"
    r".{0,100}(创建|开发|搭建|生成|编写|运行|启动|部署|安装|配置|构建|打包|验证|端口|url|访问)",
    re.I | re.UNICODE,
)
_NO_FILE_DELIVERY_RE = re.compile(
    r"((不需要|无需|不要|不用|别|免).{0,16}(单独)?(保存|存储|落盘|写入).{0,16}(文件|docs|目录)?"
    r"|((直接|只要|仅).{0,12}(发给我|回复|回答|在聊天|在消息|给我看)))",
    re.I | re.UNICODE,
)


@dataclass(frozen=True)
class TaskIntent:
    kind: str
    delivery_target: str = "unknown"
    required_tools: tuple[str, ...] = ()
    confidence: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class CapabilityPreflight:
    allowed: bool
    required_tools: tuple[str, ...] = ()
    denied_reasons: tuple[str, ...] = ()

    @property
    def reply(self) -> str:
        tools = "、".join(_escape_feishu_markdown(x) for x in self.required_tools) if self.required_tools else "受限工具"
        reasons = "\n".join(f"- {_escape_feishu_markdown(x)}" for x in self.denied_reasons)
        return (
            "当前请求需要调用受保护能力，但当前飞书用户角色不足，系统已在进入模型执行前停止，"
            "避免工具循环或误报成功。\n\n"
            f"需要能力：{tools}\n\n"
            f"拒绝原因：\n{reasons}\n\n"
            "请让管理员授予当前 open\\_id developer、tenant\\_admin 或 platform\\_admin 后重试。"
        )


def is_auth_or_role_query(text: str) -> bool:
    return bool(_AUTH_QUERY_RE.search((text or "").strip()))


def is_application_delivery_request(text: str) -> bool:
    msg = (text or "").strip()
    if not msg:
        return False
    return bool(_APPLICATION_DELIVERY_RE.search(msg))


def _escape_feishu_markdown(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("_", "\\_").replace("*", "\\*")


def _dedupe(items: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _is_application_delivery(msg: str) -> bool:
    return bool(_APPLICATION_DELIVERY_RE.search(msg)) or (
        bool(_APP_ACTION_RE.search(msg))
        and (bool(_APP_FRAMEWORK_RE.search(msg)) or bool(_APP_CONTEXT_RE.search(msg)))
    )


def _is_ops_deployment(msg: str) -> bool:
    return bool(_OPS_CONTEXT_RE.search(msg)) and bool(_COMMAND_ACTION_RE.search(msg))


def _needs_runtime_service(msg: str) -> bool:
    return bool(_RUNTIME_SERVICE_RE.search(msg)) and (
        bool(_APP_FRAMEWORK_RE.search(msg))
        or bool(_APP_CONTEXT_RE.search(msg))
        or bool(_OPS_CONTEXT_RE.search(msg))
    )


def _needs_command_execution(msg: str) -> bool:
    return bool(_COMMAND_ACTION_RE.search(msg)) and bool(_COMMAND_CONTEXT_RE.search(msg))


def classify_task_intent(text: str) -> TaskIntent:
    msg = (text or "").strip()
    if not msg:
        return TaskIntent(kind="unknown", reason="empty message")

    cloud_doc = bool(_CLOUD_DOC_RE.search(msg))
    app_delivery = _is_application_delivery(msg)
    ops_deployment = _is_ops_deployment(msg)
    explicit_no_file = bool(_NO_FILE_DELIVERY_RE.search(msg))
    workspace_file = bool(_WORKSPACE_FILE_RE.search(msg)) and not cloud_doc
    runtime_service = _needs_runtime_service(msg)
    command_execution = _needs_command_execution(msg)

    # 飞书文档是云端交付目标；即便用户追问「注意是保存到飞书文档」，
    # 也应走 create_feishu_doc，而不是误判为工作区 write_file。
    if cloud_doc:
        tools: list[str] = ["create_feishu_doc"]
        if runtime_service and (app_delivery or ops_deployment):
            tools.extend(["write_file", "execute", "background_task"])
        return TaskIntent(
            kind="cloud_doc_delivery",
            delivery_target="feishu_doc",
            required_tools=_dedupe(tools),
            confidence=0.96,
            reason="user explicitly requested Feishu/online document delivery",
        )

    if explicit_no_file and not app_delivery and not ops_deployment and not runtime_service:
        return TaskIntent(
            kind="chat_only",
            delivery_target="chat",
            required_tools=(),
            confidence=0.95,
            reason="user explicitly asked for an in-chat answer without saving to files",
        )

    if app_delivery:
        return TaskIntent(
            kind="ops_deployment" if ops_deployment else "app_delivery",
            delivery_target="runtime_url" if runtime_service else "workspace",
            required_tools=("write_file", "execute", "background_task"),
            confidence=0.92,
            reason="application delivery requires source/config files, command execution, and service lifecycle handling",
        )

    if workspace_file:
        return TaskIntent(
            kind="file_delivery",
            delivery_target="workspace",
            required_tools=("write_file",),
            confidence=0.9,
            reason="user explicitly requested workspace file or directory output",
        )

    if command_execution:
        tools = ["execute"]
        if runtime_service:
            tools.append("background_task")
        return TaskIntent(
            kind="command_execution",
            delivery_target="runtime_url" if runtime_service else "command_result",
            required_tools=_dedupe(tools),
            confidence=0.82,
            reason="user requested command execution with an explicit command/framework context",
        )
    return TaskIntent(
        kind="unknown",
        delivery_target="unknown",
        required_tools=(),
        confidence=0.5,
        reason="no protected capability detected",
    )


def requested_capabilities(text: str) -> tuple[str, ...]:
    return classify_task_intent(text).required_tools


def preflight_capabilities(text: str, ctx: ToolSecurityContext) -> CapabilityPreflight:
    required = requested_capabilities(text)
    if not required:
        return CapabilityPreflight(allowed=True)

    cfg = get_config()
    denied: list[str] = []
    for tool_name in required:
        if tool_name == "execute":
            ok, reason = check_shell_capability_allowed(ctx, cfg)
        else:
            ok, reason = check_tool_allowed(tool_name, ctx, cfg)
        if not ok:
            denied.append(reason)

    return CapabilityPreflight(
        allowed=not denied,
        required_tools=required,
        denied_reasons=tuple(denied),
    )

