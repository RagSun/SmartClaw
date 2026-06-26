"""
DeepAgents 包装器

集成 DeepAgents 框架，提供 LLM Agent 能力。

环境变量（可选）:
- SMARTCLAW_DEEPAGENTS_DEBUG: 仅当设为 1/true/on/yes 时开启 create_deep_agent(debug=…)
  与 LangChain set_debug，打印 LangGraph/LangChain 完整链路。**默认关闭**，控制台只保留 invoke 起止等摘要。
- SMARTCLAW_DEEPAGENTS_INVOKE_HEARTBEAT_SEC: 大于 0 时，invoke 期间每隔 N 秒打印一次已运行时长；
  默认 10 秒；设为 0/false/off 关闭。
- SMARTCLAW_DEEPAGENTS_RECURSION_LIMIT: 非空时覆盖 config [execution] deepagents_recursion_limit；
  设为 0 表示不限制（沿用 LangGraph 默认）。未设环境变量时用配置文件（默认 128 图步，防与 OpenClaw 类似的长时间工具空转）。
- SMARTCLAW_BG_EXECUTE: 长驻服务自动后台见 agent/bg_execute.py；0/false/off 可关闭。
- SMARTCLAW_WORKSPACE_WRITE_GUARD_RETRY: 落盘意图下若第一轮 **零工具**，是否自动追加一轮 Human 续跑（默认 1）；0/false/off 关闭。

说明：重复工具/Shell 死循环由 agent/tools/loop_detector 在 ToolRegistry 与各 DeepAgents
Shell Backend 上拦截；与 create_deep_agent(interrupt_on=…) 的人机确认（HITL）不同，二者可并存。

多 Agent：ToolRegistry 沙箱指针已不再由 Runner 写入；exec 使用任务级 ``sandbox_context``。
DeepAgentsBackend 在构造时绑定本 Runner 的 ``sandbox_backend/instance_id``。
"""

import asyncio
import json
import os
import re
import sys
import threading
from pathlib import Path
import time
import traceback
from typing import Any, Optional

from deepagents import create_deep_agent
from langchain_openai import ChatOpenAI

from smartclaw.agent.control_flow import is_application_delivery_request
from smartclaw.agent.runtime_trace import is_deepagents_verbose
from smartclaw.agent.system_prompt import SYSTEM_PROMPT
from smartclaw.agent.firecracker_deepagents_backend import FirecrackerDeepAgentsBackend
from smartclaw.agent.docker_deepagents_backend import DockerDeepAgentsBackend
from smartclaw.console import info, error
from smartclaw.debug_session_log import debug_ndjson


def _deepagents_debug_flag() -> bool:
    return is_deepagents_verbose()


def _invoke_heartbeat_interval_sec() -> float:
    """长 invoke 心跳间隔（秒）；默认开启，0 表示关闭。"""
    raw = (os.environ.get("SMARTCLAW_DEEPAGENTS_INVOKE_HEARTBEAT_SEC") or "").strip()
    if not raw:
        return 10.0
    if raw.lower() in {"0", "false", "off", "no"}:
        return 0.0
    try:
        v = float(raw)
        return v if v > 0 else 0.0
    except ValueError:
        return 0.0


def _invoke_recursion_limit() -> int | None:
    """
    LangGraph invoke 的 recursion_limit（单轮图步上限）。
    参考 OpenClaw 等 harness：对外层/工具环有硬预算与断路思路；此处对齐为可配置步数上限。
    返回 None 表示不设置（LangGraph 环境默认，通常极大）。
    """
    raw = (os.environ.get("SMARTCLAW_DEEPAGENTS_RECURSION_LIMIT") or "").strip()
    if raw:
        try:
            env_v = int(raw)
            if env_v <= 0:
                return None
            return max(8, min(env_v, 10_000))
        except ValueError:
            pass
    try:
        from smartclaw.config.loader import get_config

        cfg_v = int(get_config().execution.deepagents_recursion_limit)
        if cfg_v <= 0:
            return None
        return max(8, min(cfg_v, 10_000))
    except Exception:
        return 128


_BOGUS_LINUX_WORKSPACE_ROOT = "/root/smartclaw_workspace"

_USER_ASKS_WORKSPACE_FACTS_RE = re.compile(
    r"(工作区|根目录|完整路径|目录在哪|当前目录"
    r"|项目在.*哪儿|项目在.*哪里|在哪个盘"
    r"|\bpwd\b|\bcwd\b|current\s+directory|\bworkspace\s+root\b"
    r"|where\s+is\s+(the\s+)?(project|workspace|repo))",
    re.I | re.UNICODE,
)

_USER_REQUESTS_WORKSPACE_DISK_WRITE_RE = re.compile(
    r"(保存到|保存至|存储在|存储到|存到|存入|放到|生成到|输出到|写到|创建工作区.{0,8}文件"
    r"|写入\s*(?:文件|目录|路径|代码|配置|硬盘|磁盘)|写入\s+.{1,120}?\.(?:md|txt|py|json|yaml|yml)\b"
    r"|在项目(里|中).{0,12}写|创建.{0,6}目录|创建.{0,6}文件夹"
    r"|\bmkdir\b|\btouch\b"
    r"|写入.{0,20}\.(?:md|txt|py|json|yaml|yml)\b"
    r"|docs\s*(目录|下|里|中|/|\\|$))",
    re.I | re.UNICODE,
)


def _user_asks_workspace_facts(text: str) -> bool:
    if not (text or "").strip():
        return False
    return bool(_USER_ASKS_WORKSPACE_FACTS_RE.search(text.strip()))


def _user_requests_workspace_disk_write(text: str) -> bool:
    if not (text or "").strip():
        return False
    return bool(_USER_REQUESTS_WORKSPACE_DISK_WRITE_RE.search(text.strip()))


def _execution_plan_requests_workspace_disk_write(plan: Optional[dict[str, Any]]) -> bool:
    if not isinstance(plan, dict) or not plan:
        return False
    try:
        body = json.dumps(plan, ensure_ascii=False)
    except TypeError:
        body = str(plan)
    return _user_requests_workspace_disk_write(body) or bool(
        re.search(r"\b(write_file|edit_file|mkdir|touch|docs[/\\]?)\b", body, re.I)
    )


def _workspace_write_guard_retry_enabled() -> bool:
    raw = (os.environ.get("SMARTCLAW_WORKSPACE_WRITE_GUARD_RETRY") or "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _workspace_write_followup_human(host_root: Optional[str], *, docker_mode: bool) -> str:
    """第一轮零工具时追加的 Human，强制模型进入工具链。"""
    parts = [
        "[SmartClaw｜系统续跑 — 落盘复核]",
        "上一轮在对话轨迹中**未检测到** `write_file` / `edit_file` / shell **`execute`** 的成功返回。",
        "你必须在本轮内：**至少一次**使用 **`write_file`**（或 **`edit_file`**）将源码写入工作区磁盘；"
        "再用 **`execute`** 完成依赖安装与服务启动（若任务需要）。",
        "**禁止**仅用自然语言声称「已完成」「已保存」；必须以工具输出为准。",
    ]
    if docker_mode:
        parts.append(
            "**Docker 沙箱**：`execute` 在容器内运行，工作区挂载为 **`/root/workspace`**；"
            "请 `cd /root/workspace` 后使用相对路径，或使用 **`/root/workspace/…`**。"
        )
    if host_root:
        parts.append(f"宿主工作区根（read/write_file 语义）：`{host_root}`。")
    return "\n".join(parts)


def _scrub_linux_workspace_hallucination(reply: str, host_root: Optional[str]) -> str:
    """若模型仍输出文档占位路径，而宿主工作区明显非该 Linux 路径，则替换为真实根。"""
    if not reply or not host_root or _BOGUS_LINUX_WORKSPACE_ROOT not in reply:
        return reply
    norm = host_root.replace("\\", "/")
    if norm.startswith("/root"):
        return reply
    return reply.replace(_BOGUS_LINUX_WORKSPACE_ROOT, host_root)


def _agent_reply_as_str(body: Any) -> str:
    """将 invoke 末条 AI 消息的 content（可能为多段）规整为单行文本。"""
    if body is None:
        return ""
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        parts: list[str] = []
        for part in body:
            if isinstance(part, dict):
                t = part.get("text")
                if t is None:
                    t = part.get("content")
                parts.append("" if t is None else str(t))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(body)


_WORKSPACE_WRITE_GUARD_MARK = "[SmartClaw｜落盘复核]"


def _tool_message_text_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                t = part.get("text")
                if t is None:
                    t = part.get("content")
                parts.append("" if t is None else str(t))
            else:
                parts.append(str(part))
        return "\n".join(parts)
    return str(content)


def _tool_payload_suggests_write_failure(body: str) -> bool:
    if not body:
        return True
    b = body.strip()
    low = b.lower()
    fail_markers = (
        "[工具失败]",
        "requires_confirmation",
        "路径访问被拒绝",
        "permission denied",
        "access is denied",
        "错误:",
        "error:",
        "command failed",
        "non-zero exit",
    )
    if any(m in b for m in fail_markers):
        return True
    if any(m in low for m in fail_markers):
        return True
    compact = "".join(low.split())
    if '"success":false' in compact or "'success':false" in compact:
        return True
    return False


def _tool_payload_suggests_write_success(tool_name: str | None, body: str) -> bool:
    """判断是否出现「工作区写入类」工具的成功返回（用于压「假成功」话术）。"""
    if _tool_payload_suggests_write_failure(body):
        return False
    n = (tool_name or "").strip().lower()
    compact_lower = "".join(body.lower().split())

    # Registry write_handler / DeepAgents filesystem 常见成功形态
    if n in {"write_file", "edit_file"}:
        if "文件已写入" in body:
            return True
        low = body.lower()
        if "successfully updated" in low or "successfully wrote" in low:
            return True
        if "file written" in low or "saved to" in low:
            return True
        if '"success":true' in compact_lower or "'success':true" in compact_lower:
            return True
        bl = body.lower()
        if "bytes" in bl and ("wrote" in bl or "written" in bl):
            return True
        # edit_file 有时会返回片段 diff / ok 文本且无 error
        if n == "edit_file" and len(body) > 20 and "error" not in bl[:160]:
            return True
        low_snip = low[:480]
        if any(
            x in low_snip
            for x in (
                "cannot ",
                "unable to ",
                " failed",
                "not found",
                "不存在",
                "无法写入",
                "无法创建",
                "enoent",
            )
        ):
            return False
        # DeepAgents/filesystem：无显式失败且返回体有足够内容时视为已落盘回调（减压「假阴性」复盘尾）
        st = body.strip()
        if len(st) >= 8 and not st.lower().startswith("error") and not st.startswith("[error"):
            return True
        return False

    # execute/bash：仅在明显为写盘命令且看起来像成功时计数（保守以减少误报）
    if n and any(k in n for k in ("execute", "bash", "shell", "terminal", "run_terminal_cmd")):
        low = body.lower()
        wrote_hint = any(
            k in low
            for k in (
                "set-content",
                "out-file",
                "add-content",
                "new-item",
                "ni ",
                "> ",
                "tee ",
                "echo ",
                "printf ",
                "mkdir",
                "touch ",
                "cp ",
                "mv ",
            )
        )
        ok_hint = (
            "exit code: 0" in low
            or "exit code 0" in low
            or "exitcode 0" in low.replace(" ", "")
            or "return code 0" in low
            or "returncode 0" in low.replace(" ", "")
            or "退出码：0" in body
            or "退出码: 0" in body
        )
        if wrote_hint and ok_hint:
            return True

    return False


def messages_indicate_workspace_write_success(messages: list[Any] | None) -> bool:
    """遍历 invoke 返回的消息链，是否至少有一条工具返回像「写文件成功」。"""
    if not messages:
        return False
    for m in messages:
        if type(m).__name__ != "ToolMessage" and getattr(m, "type", None) not in {"tool"}:
            continue
        name = getattr(m, "name", None)
        body = _tool_message_text_content(getattr(m, "content", None))
        if _tool_payload_suggests_write_success(name, body):
            return True
    return False


def _workspace_write_failure_reply(
    host_root: Optional[str],
    *,
    n_tool_msgs: int | None = None,
    has_execute_name: bool | None = None,
) -> str:
    root_line = (
        f"当前解析的工作区根为：`{host_root}`。\n" if host_root else ""
    )
    trace_line = ""
    if n_tool_msgs is not None:
        trace_line = (
            f"本轮工具消息数：`{n_tool_msgs}`；"
            f"是否检测到 execute 工具名：`{bool(has_execute_name)}`。\n"
        )
    return (
        f"{_WORKSPACE_WRITE_GUARD_MARK} "
        "**本轮用户要求工作区磁盘落盘，但系统未验证到文件成功写入。**\n\n"
        "我已拦截模型原始回复中的「已成功创建/已保存」结论，原因是执行记录中没有发现 "
        "`write_file` / `edit_file` 的成功工具返回值（或等价写盘命令的成功输出）。"
        "请以工具返回值或磁盘实际文件为准。\n"
        + root_line
        + trace_line
        + "如需继续，请再次发起请求；系统会要求模型必须调用 **`write_file` 或 `execute`**，并且只有观测到明确成功才会回复完成。\n"
    )


def _append_workspace_write_truth_guard(reply: str, host_root: Optional[str]) -> str:
    """兼容旧测试/调用名；现在 fail-closed，不再保留模型假成功正文。"""
    if reply and _WORKSPACE_WRITE_GUARD_MARK in reply:
        return reply
    return _workspace_write_failure_reply(host_root)


def _scrub_workspace_pseudopaths(reply: str, host_root: Optional[str]) -> str:
    """最终回复中将 /workspace 或 /docs 伪路径改写为宿主根下的真实路径提示。"""
    if not reply or not host_root:
        return reply
    root = host_root.rstrip("\\/")

    def repl_workspace(m: re.Match[str]) -> str:
        rel = (m.group(1) or "").replace("/", "\\").lstrip("\\")
        return f"{root}\\{rel}" if rel else root

    reply = re.sub(
        r"(?<![\w:])(?:file://)?/workspace/([^\s`'\"，。；；、)）\]]+)",
        repl_workspace,
        reply,
    )

    def repl_docs(m: re.Match[str]) -> str:
        rel = (m.group(1) or "").replace("/", "\\").lstrip("\\")
        return f"{root}\\docs\\{rel}" if rel else f"{root}\\docs"

    reply = re.sub(
        r"(?<![\w:])/docs/([^\s`'\"，。；；、)）\]]+)",
        repl_docs,
        reply,
    )
    return reply


class DeepAgentsWrapper:
    """DeepAgents LLM Agent 包装器"""

    def __init__(
        self,
        model_name: str = "glm-5",
        base_url: str = "https://open.bigmodel.cn/api/coding/paas/v4",
        api_key: str = None,
        agent_name: str = "SmartClaw",
        skills_prompt: str = "",
        workspace_dir: Optional[str] = None,
        compiled_prompt: str = "",
        sandbox_backend=None,
        sandbox_instance_id: Optional[str] = None,
    ) -> None:
        self.model_name = model_name
        self.base_url = base_url
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.agent_name = agent_name
        self.skills_prompt = skills_prompt
        self._workspace_dir = workspace_dir
        self._compiled_prompt = (compiled_prompt or "").strip()
        self._sandbox_backend_explicit = sandbox_backend
        self._sandbox_instance_id_explicit = sandbox_instance_id
        self._agent = None
        self._agent_lock = asyncio.Lock()
        self._init_task = None
        self._workspace_root_disp: Optional[str] = None
        self._workspace_root_posix: Optional[str] = None
        self._backend: Any = None

    def _resolved_workspace_root_for_prompt(self) -> Optional[str]:
        if self._workspace_root_disp:
            return self._workspace_root_disp
        if self._workspace_dir:
            try:
                return str(
                    Path(
                        os.path.abspath(os.path.expanduser(self._workspace_dir))
                    ).resolve()
                )
            except Exception:
                pass
        try:
            wb = Path(os.path.expanduser("~/.smartclaw/workspace")).resolve()
            return str((wb / self.agent_name).resolve())
        except Exception:
            return None

    async def initialize(self):
        """异步初始化 Agent"""
        async with self._agent_lock:
            if self._agent is not None:
                return

            llm = ChatOpenAI(
                model=self.model_name,
                base_url=self.base_url,
                api_key=self.api_key,
                temperature=0.7,
                max_tokens=8192,
            )

            from smartclaw.agent.tools import get_tool_registry
            registry = get_tool_registry()

            sb_backend = self._sandbox_backend_explicit or registry.sandbox_backend
            sb_inst = (
                self._sandbox_instance_id_explicit
                if self._sandbox_instance_id_explicit is not None
                else registry.sandbox_instance_id
            )

            # 工作区根目录（与 agent.json workspace + 全局 agent_workspace_base 一致）
            if self._workspace_dir:
                project_dir = os.path.abspath(os.path.expanduser(self._workspace_dir))
            else:
                workspace_base = os.path.expanduser("~/.smartclaw/workspace")
                project_dir = os.path.join(workspace_base, self.agent_name)
            os.makedirs(project_dir, exist_ok=True)

            if sb_backend and sb_inst:
                backend_type = getattr(sb_backend, 'backend_type', 'firecracker')

                if backend_type == 'docker':
                    backend = DockerDeepAgentsBackend(
                        root_dir=project_dir,
                        docker_backend=sb_backend,
                        instance_id=sb_inst,
                        env=os.environ.copy(),
                    )
                    info(f"[DeepAgentsBackend] sandbox=docker workspace={project_dir}")
                else:
                    backend = FirecrackerDeepAgentsBackend(
                        root_dir=project_dir,
                        sandbox_backend=sb_backend,
                        instance_id=sb_inst,
                        env=os.environ.copy(),
                        virtual_mode=False,
                    )
                    info(f"[DeepAgentsBackend] sandbox=firecracker workspace={project_dir}")
            else:
                from smartclaw.agent.workspace_bound_backend import WorkspaceBoundLocalShellBackend

                backend = WorkspaceBoundLocalShellBackend(
                    root_dir=project_dir,
                    env=os.environ.copy(),
                    inherit_env=False,
                )
                info(
                    f"[DeepAgentsBackend] sandbox=local workspace={project_dir}"
                )

            root_disp = str(Path(project_dir).resolve())
            root_posix = Path(project_dir).resolve().as_posix()
            self._workspace_root_disp = root_disp
            self._workspace_root_posix = root_posix
            workspace_facts = (
                "\n\n## 当前运行环境与工作区（必须遵守，禁止捏造路径）\n"
                f"- **本 Agent 的 execute / read_file / write_file 所绑定的工作区根目录（宿主绝对路径）**：`{root_disp}`\n"
                f"- 跨平台文档引用可用 POSIX 形式：`{root_posix}`\n"
                "- **`/root/smartclaw_workspace`、`/tmp/...` 仅为 Linux 容器或历史文档中的占位符**；当前进程将工作区绑定在上述 **绝对路径**。\n"
                "- 回答用户「工作区在哪」「完整目录」时必须给出该路径（同上），不得用占位路径冒充真实根目录。\n"
                "- 在项目内创建文件：优先 **write_file** 或 **execute** 使用**相对路径**（相对于上述根）；不要随意声称文件在 `/tmp/` 除非你真的把文件写入了当前系统允许访问的临时目录且对用户有意义。\n"
            )
            if sb_backend and sb_inst and getattr(sb_backend, "backend_type", "") == "docker":
                workspace_facts += (
                    "- **Docker 沙箱**：**execute（Shell）在容器内运行**，该目录在容器中挂载为 **`/root/workspace`**（与上述宿主根目录内容一致）。"
                    " Shell 中请 **`cd /root/workspace`** 后使用相对路径，或使用 **`/root/workspace/...`**。"
                    " 宿主绝对路径在容器内默认不存在；运行时会把常见的宿主工作区前缀改写为 `/root/workspace`，仍建议直接在 Shell 里用容器路径或相对路径。\n"
                    "- **read_file / write_file** 仍作用于宿主侧同一工作区；向用户说明「项目文件位置」时继续使用 **`"
                    f"{root_disp}"
                    "`**。\n"
                )

            # 根据 agent_name 生成专属 system prompt
            agent_system_prompt = SYSTEM_PROMPT.format(agent_name=self.agent_name)
            agent_system_prompt = f"{agent_system_prompt}{workspace_facts}"
            if self.skills_prompt:
                agent_system_prompt = f"{agent_system_prompt}\n\n{self.skills_prompt}"
            if self._compiled_prompt:
                agent_system_prompt = (
                    f"{agent_system_prompt}\n\n## 工作区人格与约束（已编译 Markdown）\n"
                    f"{self._compiled_prompt}"
                )
            from smartclaw.agent.deepagents_registry_tools import registry_tools_for_deepagents

            try:
                extra_tools = registry_tools_for_deepagents(registry)
            except Exception as e:
                error(
                    f"[DeepAgentsWrapper] 扩展工具挂载失败（将仅使用 DeepAgents 内置工具）: {e}"
                )
                extra_tools = []

            dag_debug = _deepagents_debug_flag()
            if dag_debug:
                try:
                    from langchain_core.globals import set_debug

                    set_debug(True)
                    info("[DeepAgentsWrapper] LangChain set_debug(True)（完整链路日志）")
                except Exception as ex:
                    info(f"[DeepAgentsWrapper] LangChain set_debug 跳过: {ex}")
            else:
                try:
                    from langchain_core.globals import set_debug

                    set_debug(False)
                except Exception:
                    pass

            agent_kwargs: dict[str, Any] = {
                "model": llm,
                "backend": backend,
                "system_prompt": agent_system_prompt,
                "debug": dag_debug,
            }
            if extra_tools:
                agent_kwargs["tools"] = extra_tools
            self._backend = backend
            self._agent = create_deep_agent(**agent_kwargs)
            info(
                f"[DeepAgentsWrapper] 就绪 agent={self.agent_name} extra_tools={len(extra_tools)} "
                f"lc_debug={'on' if dag_debug else 'off'}"
            )

    def _ensure_initialized(self):
        """确保 Agent 已初始化"""
        if self._agent is None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Already in async context - just ensure the coroutine runs
                    if not hasattr(self, '_init_task') or self._init_task is None or self._init_task.done():
                        self._init_task = asyncio.ensure_future(self.initialize())
                        # Don't block - let it run in background
                        if _deepagents_debug_flag():
                            info("[DeepAgentsWrapper] 初始化已在后台启动")
                else:
                    loop.run_until_complete(self.initialize())
            except RuntimeError:
                asyncio.run(self.initialize())
            except Exception as e:
                error(f"[DeepAgentsWrapper] 初始化异常: {e}")

    def _analyze_invoke_tool_trace(
        self, result: Any, write_record_start: int
    ) -> tuple[bool, int, bool, int]:
        """解析单次 invoke：是否观测到落盘、ToolMessage 数量、是否含 execute、messages 长度。"""
        workspace_write_observed = False
        try:
            workspace_write_observed = (
                len(getattr(self._backend, "write_records", []) or [])
                > write_record_start
            )
        except Exception:
            workspace_write_observed = False
        n_tool_msgs = 0
        has_execute_name = False
        msg_len = 0
        if isinstance(result, dict) and "messages" in result:
            _ims = result.get("messages") or []
            msg_len = len(_ims) if isinstance(_ims, list) else 0
            workspace_write_observed = workspace_write_observed or messages_indicate_workspace_write_success(
                _ims if isinstance(_ims, list) else None
            )
            try:
                from langchain_core.messages import ToolMessage

                for _m in _ims:
                    if isinstance(_m, ToolMessage):
                        n_tool_msgs += 1
                    nm = getattr(_m, "name", None) or ""
                    if nm and "execute" in str(nm).lower():
                        has_execute_name = True
            except Exception:
                for _m in _ims:
                    t = getattr(_m, "type", None)
                    if t == "tool" or type(_m).__name__ == "ToolMessage":
                        n_tool_msgs += 1
                    nm = getattr(_m, "name", None) or ""
                    if nm and "execute" in str(nm).lower():
                        has_execute_name = True
        return workspace_write_observed, n_tool_msgs, has_execute_name, msg_len

    @staticmethod
    def _emit_invoke_tool_trace_ndjson(
        *,
        disk_write_intent: bool,
        workspace_write_observed: bool,
        n_tool_msgs: int,
        has_execute_name: bool,
        msg_len: int,
        note: Optional[str] = None,
    ) -> None:
        payload: dict[str, Any] = {
            "n_tool_messages": n_tool_msgs,
            "has_named_execute_tool": has_execute_name,
            "n_messages": msg_len,
            "workspace_write_observed": workspace_write_observed,
            "user_disk_write_intent": disk_write_intent,
        }
        if note:
            payload["note"] = note
        debug_ndjson(
            "H-E",
            "deepagents_wrapper.py:invoke_tool_trace",
            "tool / shell invocation summary",
            payload,
        )

    def execute(
        self,
        user_message: str,
        history: list = None,
        execution_plan: Optional[dict[str, Any]] = None,
        thread_id: Optional[str] = None,
    ) -> str:
        """执行 Agent"""
        if _deepagents_debug_flag():
            info(f"[DeepAgentsWrapper] execute msg[:30]={user_message[:30]!r}…")
        self._ensure_initialized()
        if self._agent is None:
            error("[DeepAgentsWrapper.execute] _agent still None after _ensure_initialized!")
            return "Agent 初始化失败"
        
        # 组装 messages
        messages = []
        if history:
            messages.extend(history)
        
        # 【关键】在用户消息前加强身份约束，防止模型忽略 system prompt
        identity_prefix = f"[重要身份规则：你的名字是 {self.agent_name}，不是 SmartClaw或其他名字。你必须以你的真实名字自我介绍。]"
        plan_prefix = ""
        if execution_plan:
            try:
                ep = dict(execution_plan) if isinstance(execution_plan, dict) else {}
                env_one = (ep.pop("environment_plan", None) or "").strip()
                body = json.dumps(ep, ensure_ascii=False)
                if len(body) > 8000:
                    body = body[:8000] + "\n…(plan JSON 已截断)"
                plan_prefix = f"[Unified execution plan]\n{body}\n\n"
                if env_one:
                    plan_prefix = (
                        "[一次性环境/依赖策略 — 严格优先：先写全量 requirements.txt（或 environment.yml），"
                        "再用尽可能少的 shell（prefer 1～2 条）完成创建环境+安装；"
                        "禁止多轮单包 pip/反复 conda create]\n"
                        f"{env_one}\n\n"
                    ) + plan_prefix
            except Exception:
                plan_prefix = ""

        host_root = self._resolved_workspace_root_for_prompt()
        disk_write_intent = (
            _user_requests_workspace_disk_write(user_message)
            or _execution_plan_requests_workspace_disk_write(execution_plan)
            or is_application_delivery_request(user_message)
        )
        message_inject = ""
        if host_root and _user_asks_workspace_facts(user_message):
            message_inject += (
                "[SmartClaw｜本条与「工作区 / 当前目录 / 路径」相关：回答时必须使用下列宿主绝对路径原文，"
                "禁止仅用 `/root/smartclaw_workspace` 等占位路径冒充真实根。]\n"
                f"- 工作区根：`{host_root}`\n\n"
            )
        if host_root and disk_write_intent:
            message_inject += (
                "[SmartClaw｜本条要求在工作区磁盘落盘：必须先调用「write_file」或「execute」并完成实际写入，"
                "以工具返回值为准；禁止仅列步骤、禁止未调用工具就声称已成功。"
                "路径必须使用 `docs/xxx` 等相对路径；禁止使用 `/workspace/...`、`workspace/...` 或 `/docs/...` 作为落盘目标。]\n\n"
            )
        if is_application_delivery_request(user_message):
            message_inject += (
                "[SmartClaw｜通用应用交付快路径：目标是在 20～50 graph steps 内闭环。"
                "先判断应用类型（Flask/FastAPI/Streamlit/Vite/React/Nginx/Docker 等），只做必要动作："
                "1) 写入最小可运行源码/配置（如 `app.py`、`package.json`、`Dockerfile`、`nginx.conf`）；"
                "2) 写入一次性依赖清单（如 `requirements.txt` / `package.json`）；"
                "3) 用尽可能少的 `execute` 完成安装/构建/启动；"
                "4) 长驻服务直接前台命令交给平台自动后台化，收到 `[bg]` / pid / URL 后立即汇报并停止。"
                "禁止反复 `dir`/`ls`/读日志/单包安装/多轮 tool_audit；只有启动失败时才用 `background_task` 或一次性日志排错。]\n\n"
            )

        enhanced_message = (
            f"{identity_prefix}\n\n{message_inject}{plan_prefix}{user_message}"
        )
        
        # 添加当前用户消息
        if user_message:
            messages.append(("human", enhanced_message))

        invoke_config: dict[str, Any] = {
            "configurable": {"thread_id": thread_id or "smartclaw-default"},
        }
        rec_lim = _invoke_recursion_limit()
        if rec_lim is not None:
            invoke_config["recursion_limit"] = rec_lim
        beat_sec = _invoke_heartbeat_interval_sec()
        write_record_start = 0
        try:
            write_record_start = len(getattr(self._backend, "write_records", []) or [])
        except Exception:
            write_record_start = 0
        t0 = time.perf_counter()
        try:
            info(
                "[DeepAgentsWrapper] invoke ↓ "
                f"n_msg={len(messages)} "
                f"recursion_limit={rec_lim if rec_lim is not None else 'off'} "
                f"beat={beat_sec if beat_sec > 0 else 'off'}"
            )
            # region agent log
            debug_ndjson(
                "H1",
                "deepagents_wrapper.py:invoke_pre",
                "invoke start",
                {
                    "thread_id": str(invoke_config.get("configurable", {}).get("thread_id") or ""),
                    "n_msg": len(messages),
                    "recursion_limit": rec_lim,
                },
            )
            # endregion
            sys.stdout.flush()
            sys.stderr.flush()

            stop_beat = threading.Event()

            def _heartbeat_loop() -> None:
                while not stop_beat.wait(timeout=beat_sec):
                    info(
                        "[DeepAgentsWrapper] invoke 心跳 "
                        f"已运行 {time.perf_counter() - t0:.1f}s "
                        f"n_msg={len(messages)} thread_id={thread_id or 'smartclaw-default'} …"
                    )
                    sys.stdout.flush()

            beat_th: threading.Thread | None = None
            if beat_sec > 0:
                beat_th = threading.Thread(
                    target=_heartbeat_loop,
                    daemon=True,
                    name="smartclaw-deepagents-invoke-heartbeat",
                )
                beat_th.start()

            try:
                result = self._agent.invoke({"messages": messages}, config=invoke_config)
            finally:
                stop_beat.set()

            elapsed = time.perf_counter() - t0
            rk = list(result.keys()) if isinstance(result, dict) else None
            info(f"[DeepAgentsWrapper] invoke ↑ {elapsed:.2f}s keys={rk}")
            # region agent log
            debug_ndjson(
                "H1",
                "deepagents_wrapper.py:invoke_ok",
                "invoke finished ok",
                {"elapsed_s": round(elapsed, 3), "result_keys": rk},
            )
            workspace_write_observed, n_tool_msgs, has_execute_name, msg_len = (
                self._analyze_invoke_tool_trace(result, write_record_start)
            )
            missing_msgs = not (
                isinstance(result, dict) and "messages" in result and result.get("messages") is not None
            )
            self._emit_invoke_tool_trace_ndjson(
                disk_write_intent=disk_write_intent,
                workspace_write_observed=workspace_write_observed,
                n_tool_msgs=n_tool_msgs,
                has_execute_name=has_execute_name,
                msg_len=msg_len,
                note="no messages in invoke result dict" if missing_msgs else None,
            )
            retried = False
            if (
                _workspace_write_guard_retry_enabled()
                and disk_write_intent
                and (user_message or "").strip()
                and not workspace_write_observed
                and n_tool_msgs == 0
            ):
                retried = True
                info(
                    "[DeepAgentsWrapper] workspace write guard 续跑 | "
                    "第一轮零工具，追加 Human 强制工具链 …"
                )
                try:
                    write_record_start_2 = len(
                        getattr(self._backend, "write_records", []) or []
                    )
                except Exception:
                    write_record_start_2 = 0
                docker_mode = isinstance(self._backend, DockerDeepAgentsBackend)
                follow_h = _workspace_write_followup_human(
                    host_root, docker_mode=docker_mode
                )
                t_retry = time.perf_counter()
                result = self._agent.invoke(
                    {"messages": [("human", follow_h)]},
                    config=invoke_config,
                )
                elapsed_retry = time.perf_counter() - t_retry
                info(
                    "[DeepAgentsWrapper] invoke ↑ "
                    f"retry {elapsed_retry:.2f}s "
                    f"keys={list(result.keys()) if isinstance(result, dict) else None}"
                )
                obs2, nt2, hex2, msg_len2 = self._analyze_invoke_tool_trace(
                    result, write_record_start_2
                )
                workspace_write_observed = workspace_write_observed or obs2
                n_tool_msgs += nt2
                has_execute_name = has_execute_name or hex2
                msg_len = msg_len2
                self._emit_invoke_tool_trace_ndjson(
                    disk_write_intent=disk_write_intent,
                    workspace_write_observed=workspace_write_observed,
                    n_tool_msgs=n_tool_msgs,
                    has_execute_name=has_execute_name,
                    msg_len=msg_len,
                    note="after workspace-write-guard retry",
                )
            # endregion
            if disk_write_intent:
                hint = ""
                if not workspace_write_observed:
                    hint = (
                        " | hint=未观测到 write_file/edit_file 或 execute 类工具调用；"
                        "若需真实落盘请显式 write_file，并用 exec/background_task 启动服务"
                    )
                info(
                    "[DeepAgentsWrapper] workspace write guard | "
                    f"intent={disk_write_intent} observed={workspace_write_observed} "
                    f"n_tool_msgs={n_tool_msgs} has_execute={has_execute_name} "
                    f"workspace={host_root or '-'}{hint}"
                    f"{' | phase=retry' if retried else ''}"
                )
            sys.stdout.flush()
            # DeepAgents 返回格式可能是 {'messages': [...]} 或 {'output': '...'}
            raw_reply: Any = None
            if isinstance(result, dict):
                if "output" in result:
                    raw_reply = result.get("output", "")
                elif "messages" in result:
                    msgs = result["messages"]
                    if msgs and len(msgs) > 0:
                        last_msg = msgs[-1]
                        if hasattr(last_msg, "content"):
                            raw_reply = getattr(last_msg, "content")
                        elif isinstance(last_msg, dict):
                            raw_reply = last_msg.get("content")
                    else:
                        raw_reply = ""
            if raw_reply is None:
                raw_reply = result
            reply_str = _agent_reply_as_str(raw_reply)
            if (
                (user_message or "").strip()
                and disk_write_intent
                and not workspace_write_observed
            ):
                reply_str = _workspace_write_failure_reply(
                    host_root,
                    n_tool_msgs=n_tool_msgs,
                    has_execute_name=has_execute_name,
                )
            reply_str = _scrub_linux_workspace_hallucination(reply_str, host_root)
            reply_str = _scrub_workspace_pseudopaths(reply_str, host_root)
            return reply_str
        except Exception as e:
            elapsed = time.perf_counter() - t0
            try:
                from langgraph.errors import GraphRecursionError

                if isinstance(e, GraphRecursionError):
                    lim = rec_lim if rec_lim is not None else "(未设置，可能为 LangGraph 默认值)"
                    msg = (
                        "本轮 Agent 执行已达到 LangGraph 图步数上限（recursion_limit），"
                        "已停止以防模型与工具长时间空转。**若本轮大量时间在 conda/pip 安装**，请调高 "
                        "`deepagents_recursion_limit` 或拆分为两步（先只做环境+依赖，再做应用）；"
                        "并检查是否反复单包安装——应用优先 `requirements.txt` + 一条合并命令。\n"
                        f"当前上限: {lim}。可在 config.toml [execution] deepagents_recursion_limit 调高，"
                        "或设环境变量 SMARTCLAW_DEEPAGENTS_RECURSION_LIMIT；设为 0 表示不限制（慎用）。\n"
                        f"原始错误: {e}"
                    )
                    error(
                        f"[DeepAgentsWrapper] recursion_limit 触发（invoke 已运行 {elapsed:.2f}s）: {e}"
                    )
                    # region agent log
                    debug_ndjson(
                        "H1",
                        "deepagents_wrapper.py:invoke_recursion_limit",
                        "GraphRecursionError",
                        {
                            "elapsed_s": round(elapsed, 3),
                            "recursion_limit": rec_lim,
                            "err_type": type(e).__name__,
                        },
                    )
                    # endregion
                    return msg
            except ImportError:
                pass
            err_msg = f"[DeepAgentsWrapper] 执行错误（invoke 已运行 {elapsed:.2f}s）: {e}"
            error(f"{err_msg}\n{traceback.format_exc()}")
            # region agent log
            debug_ndjson(
                "H1",
                "deepagents_wrapper.py:invoke_error",
                "invoke exception",
                {
                    "elapsed_s": round(elapsed, 3),
                    "err_type": type(e).__name__,
                },
            )
            # endregion
            return f"执行错误: {e}"

    def run(
        self,
        history: list = None,
        execution_plan: Optional[dict[str, Any]] = None,
        thread_id: Optional[str] = None,
    ) -> str:
        """运行 Agent（runner.py 调用入口）

        Args:
            history: 消息历史 [{"role": "user", "content": "..."}, ...]
            execution_plan: 统一 Planner 产物的可序列化字典（可选）
            thread_id: LangGraph 配置 thread_id，供 DeepAgents 摘要卸载等使用（建议用 session_id）
        Returns:
            Agent 响应文本
        """
        if _deepagents_debug_flag():
            info(f"[DeepAgentsWrapper] run history_n={len(history) if history else 0}")
        
        if not history:
            if _deepagents_debug_flag():
                info("[DeepAgentsWrapper] run 历史为空")
            return ""

        # 从历史中提取最后一条用户消息，前面的全部作为历史
        last_msg = history[-1]
        user_message = last_msg.get("content", "")
        if isinstance(user_message, list):
            user_message = "\n".join(
                item.get("text", str(item)) if isinstance(item, dict) else str(item)
                for item in user_message
            )

        chat_history = []
        for msg in history[:-1]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            # 处理 content 为 None 或非字符串类型的情况
            if content is None:
                content = ""
            elif isinstance(content, list):
                content = "\n".join(
                    item.get("text", str(item)) if isinstance(item, dict) else str(item)
                    for item in content
                )
            elif not isinstance(content, str):
                content = str(content)
            
            # 跳过空内容的消息
            if not content.strip():
                continue
            
            # LangChain 兼容 2-tuple；system 需显式传入以便 [团队知识]/[我的记忆]/[对话摘要] 进入模型上下文
            if role in ["user", "human"]:
                chat_history.append(("human", content))
            elif role in ["assistant", "ai"]:
                chat_history.append(("ai", content))
            elif role == "system":
                chat_history.append(("system", content))

        if _deepagents_debug_flag():
            info(f"[DeepAgentsWrapper] run user[:50]={user_message[:50]!r}…")
        return self.execute(
            user_message,
            history=chat_history,
            execution_plan=execution_plan,
            thread_id=thread_id,
        )


__all__ = ["DeepAgentsWrapper"]
