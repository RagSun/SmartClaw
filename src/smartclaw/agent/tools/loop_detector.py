"""
LoopDetector - 命令重复执行检测器

检测 Agent 是否陷入重复执行相同命令的循环，
类似 OpenClaw 的三次重复检测机制。

工作原理：
- 记录最近 N 次工具调用的命令签名（含工具名）。
- **执行前** `check_proposed`：若历史中**尾部已连续**同一签名 `max_repeat` 次（默认计成功+失败，
  避免「命令 exit=0 但无进展」仍死磕），则阻断下一次原样重试，并返回结构化反思提示。
- **失败后** `check`：仍用于「连续失败」导致的循环检测（如反思门闩）。
- Shell 签名前会将 `.smartclaw_bg/bg_<hex>.log` 路径统一为占位符，避免 Agent 每轮读不同 detached
  日志文件时无法累计「相同模式」的连续次数。

环境变量:
- SMARTCLAW_LOOP_DETECT: 设为 0/false/off 关闭；否则开启（默认开启）。
- SMARTCLAW_LOOP_MAX_REPEAT: 阈值，默认 3（连续相同签名达到该次数后，下一次将被阻断）。
- SMARTCLAW_LOOP_STREAK_FAILURE_ONLY: 设为 1/true/on 时，`check_proposed` **仅统计连续失败**
  （恢复旧行为）；默认 false（连续相同命令无论成败都计入）。
"""

import hashlib
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from smartclaw.debug_session_log import debug_ndjson

MAX_DETECTOR_KEYS = 128
TOOL_DEEPAGENTS_SHELL = "deepagents_shell"


@dataclass
class ToolCall:
    """工具调用记录"""
    command: str      # 命令内容
    tool_name: str   # 工具名称
    timestamp: float  # 调用时间
    success: bool    # 是否成功
    error: str = ""  # 失败信息或成功时的输出摘要（由 record 传入，便于阻断时展示）


@dataclass
class LoopDetectionResult:
    """循环检测结果"""
    is_loop: bool
    repeated_count: int  # 连续重复次数
    suggested_action: str  # 建议的行动


class LoopDetector:
    """
    命令执行循环检测

    - check_proposed：默认统计尾部**连续相同签名**（含成功），防止无进展重复占用 LangGraph 步数。
    - check：仍面向「连续失败」同一命令的反思判断。
    """
    
    def __init__(
        self,
        max_history: int = 10,        # 最多记录多少次调用
        max_repeat: int = 3,         # 超过这个次数判定为循环
        cooldown_seconds: float = 5.0, # 同一命令的最小时间间隔
    ):
        self.max_history = max_history
        self.max_repeat = max_repeat
        self.cooldown_seconds = cooldown_seconds
        
        # 调用历史（滑动窗口）
        self._history: deque[ToolCall] = deque(maxlen=max_history)
        
        # 上次触发循环警告的时间
        self._last_loop_warning: float = 0

    @staticmethod
    def _normalize_command_for_loop_signature(command: str) -> str:
        """命令签名前的归一化。

        现有：
        - bg log 路径 ``.smartclaw_bg/bg_<hex>.log`` → 占位符（避免每轮日志名漂移）。

        新增（保守、可单独关闭，``SMARTCLAW_LOOP_NORMALIZE_HEREDOC=0`` 关闭）：
        - **heredoc 体**：``cat > f << 'EOF' ... EOF`` / ``python << 'PY' ... PY`` 之间
          的内容统一替换为 ``<HEREDOC_BODY>``。这条非常关键 —— 防止模型"反复写微调过
          的 Flask 代码到同名文件"被误判为不同命令。
        - **行尾注释 / 多余空白**：``# ...`` 至行尾 → 空，多空格/换行 → 单空格。
          这两条只在归一化时使用，**不影响**真实执行命令的字面文本。
        """
        import os as _os
        import re

        if not command or not isinstance(command, str):
            return command or ""
        s = command
        s = re.sub(
            r"(?i)(\.smartclaw_bg|smartclaw_bg)[\\/]bg_[a-f0-9]{6,64}\.log",
            ".smartclaw_bg/bg_<BGLOG>.log",
            s,
        )

        enabled = (
            _os.environ.get("SMARTCLAW_LOOP_NORMALIZE_HEREDOC") or "1"
        ).strip().lower() not in ("0", "false", "no", "off")
        if not enabled:
            return s

        # heredoc 体抹除：``<<-? ['"]?MARK['"]? ...换行... MARK`` → 占位符。
        # 使用 DOTALL 保证 ``.`` 能跨行。``MARK`` 必须以独立一行（前后可空白）出现才算闭合，
        # 与 bash 自身的 heredoc 终结符语义一致；未闭合的 heredoc（被 escape 成单行的退化场景）
        # 也用同一占位符抹除，让"反复重试相同形态 cat heredoc"被识别成同一签名。
        def _strip_heredoc(text: str) -> str:
            pat = re.compile(
                r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(.*?)(?:(?<=\n)\s*\2\s*(?=\n|$))",
                re.DOTALL,
            )
            text2 = pat.sub(r"<< \2 <HEREDOC_BODY> \2", text)
            # 未闭合（heredoc body 被 escape 成单行）兜底：仅替换起始那段
            text2 = re.sub(
                r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?![A-Za-z0-9_])",
                r"<< \2 <HEREDOC_BODY>",
                text2,
            )
            return text2

        s = _strip_heredoc(s)

        # 行尾注释抹除（保守：仅当 ``#`` 前有空白时；行首 ``#!`` shebang 不动）
        s = re.sub(r"(?m)(?<=\s)#.*$", "", s)

        # 多余空白归一
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _call_signature(self, tool_name: str, command: str) -> str:
        """工具名 + 命令正文，避免不同工具相同参数字符串误判。"""
        import re

        command = self._normalize_command_for_loop_signature(command)
        raw = f"{tool_name}\t{command}"
        normalized = re.sub(r"\d{10,}", "<TIMESTAMP>", raw)
        normalized = re.sub(r"[a-f0-9]{32,}", "<HASH>", normalized)
        normalized = re.sub(r"[a-zA-Z0-9+/]{20,}={0,2}", "<TOKEN>", normalized)
        normalized = " ".join(normalized.split())
        return hashlib.md5(normalized.encode()).hexdigest()[:16]

    def _get_command_signature(self, command: str) -> str:
        """兼容旧逻辑：仅对 command 做签名（不推荐，请用 _call_signature）。"""
        return self._call_signature("", command)

    def record(
        self,
        command: str,
        tool_name: str,
        success: bool,
        error: str = "",
    ) -> None:
        """记录一次工具调用"""
        call = ToolCall(
            command=command,
            tool_name=tool_name,
            timestamp=time.time(),
            success=success,
            error=error,
        )
        self._history.append(call)
    
    def check(self) -> LoopDetectionResult:
        """
        检查是否存在循环
        
        Returns:
            LoopDetectionResult: 包含是否循环、重复次数、建议行动
        """
        if len(self._history) < 2:
            return LoopDetectionResult(is_loop=False, repeated_count=0, suggested_action="")
        
        # 获取最近连续失败的相同命令
        last_call = self._history[-1]
        if last_call.success:
            return LoopDetectionResult(is_loop=False, repeated_count=0, suggested_action="")

        repeated_count = 1
        cmd_sig = self._call_signature(last_call.tool_name, last_call.command)

        for i in range(len(self._history) - 2, -1, -1):
            prev_call = self._history[i]
            if prev_call.success:
                break
            if self._call_signature(prev_call.tool_name, prev_call.command) == cmd_sig:
                repeated_count += 1
            else:
                break

        if repeated_count >= self.max_repeat:
            # 生成反思提示
            last_failed = last_call.command
            error_summary = last_call.error[:100] if last_call.error else "未知错误"
            
            suggested_action = (
                f"反思策略：连续 {repeated_count} 次执行相同命令均失败。\n"
                f"最后失败命令: {last_failed[:100]}\n"
                f"错误信息: {error_summary}\n\n"
                f"建议重新分析：\n"
                f"1. 确认文件/目录是否真的存在\n"
                f"2. 如果文件不存在，先创建文件再运行\n"
                f"3. 检查依赖是否已安装\n"
                f"4. 考虑使用不同的命令或参数"
            )
            
            return LoopDetectionResult(
                is_loop=True,
                repeated_count=repeated_count,
                suggested_action=suggested_action,
            )
        
        return LoopDetectionResult(is_loop=False, repeated_count=0, suggested_action="")

    def check_proposed(self, tool_name: str, command: str) -> LoopDetectionResult:
        """
        执行前检查：尾部「连续相同签名」已达 max_repeat 次则阻断下一次原样调用。

        默认：连续次数**含成功**（避免 exit=0 仍无效重复）；可通过环境变量仅计失败，见 loop_streak_failure_only。
        command 建议为稳定序列化后的参数字符串（如 JSON）或 shell 命令全文。
        """
        if len(self._history) < 1:
            return LoopDetectionResult(is_loop=False, repeated_count=0, suggested_action="")

        prop_sig = self._call_signature(tool_name, command)
        failure_only = loop_streak_failure_only()
        streak_calls: list[ToolCall] = []
        for i in range(len(self._history) - 1, -1, -1):
            c = self._history[i]
            if self._call_signature(c.tool_name, c.command) != prop_sig:
                break
            if failure_only and c.success:
                break
            streak_calls.append(c)

        streak = len(streak_calls)
        if (
            tool_name == TOOL_DEEPAGENTS_SHELL
            and isinstance(command, str)
            and (
                "smartclaw_bg" in command
                or (".log" in command and "type" in command.lower())
            )
        ):
            # region agent log
            debug_ndjson(
                "H3",
                "loop_detector.py:check_proposed",
                "proposed shell streak",
                {
                    "sig_md5_16": prop_sig,
                    "streak": streak,
                    "max_repeat": self.max_repeat,
                    "will_block": streak >= self.max_repeat,
                    "failure_only": failure_only,
                    "cmd_preview": command[:160].replace("\n", " "),
                },
            )
            # endregion
        if streak < self.max_repeat:
            return LoopDetectionResult(is_loop=False, repeated_count=0, suggested_action="")

        successes = sum(1 for x in streak_calls if x.success)
        failures = streak - successes
        preview = command[:220].replace("\n", " ")
        preview_lines: list[str] = []
        for idx, x in enumerate(streak_calls[:5]):
            tail = (x.error or "").strip().replace("\n", " ")
            if len(tail) > 150:
                tail = tail[:150] + "…"
            mark = "OK" if x.success else "FAIL"
            preview_lines.append(f"  [{idx + 1}] {mark}: {tail or '(无输出摘要)'}")

        mode_note = "（仅统计连续失败）" if failure_only else "（含成功与失败；避免无进展重复）"
        suggested_action = (
            f"[循环检测] 已连续 {streak} 次执行**相同**调用{mode_note}，"
            f"已阻断第 {streak + 1} 次原样重试。\n"
            f"工具: {tool_name}\n"
            f"调用摘要: {preview}\n"
            f"本段统计: 成功 {successes} / 失败 {failures}\n"
            f"最近输出摘要（新→旧）:\n"
            + "\n".join(preview_lines)
            + "\n\n请**整体反思**后再行动：改命令/参数、优先用 background_task 查询后台任务、检查端口与路径、"
            "拆分任务或向用户说明卡点；**勿再原样重复同一调用**。"
        )
        return LoopDetectionResult(
            is_loop=True,
            repeated_count=streak,
            suggested_action=suggested_action,
        )

    def should_trigger_reflection(self) -> bool:
        """是否应该触发反思模式"""
        result = self.check()
        if not result.is_loop:
            return False
        
        # 冷却时间检查（避免反复触发）
        now = time.time()
        if now - self._last_loop_warning < 60:  # 60秒内不重复触发
            return False
        
        self._last_loop_warning = now
        return True
    
    def get_summary(self) -> str:
        """获取当前历史摘要"""
        lines = ["[LoopDetector] 最近调用记录:"]
        for i, call in enumerate(list(self._history)[-5:]):
            status = "✅" if call.success else "❌"
            cmd_preview = call.command[:60].replace('\n', ' ')
            lines.append(f"  {status} [{call.tool_name}] {cmd_preview}...")
        return "\n".join(lines)


_detectors_lock = threading.Lock()
_detectors: dict[str, LoopDetector] = {}


def loop_detection_enabled() -> bool:
    raw = (os.environ.get("SMARTCLAW_LOOP_DETECT") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    return True


def loop_streak_failure_only() -> bool:
    """为 True 时 check_proposed 仅在「连续失败」的尾部上计数（与旧版一致）。"""
    raw = (os.environ.get("SMARTCLAW_LOOP_STREAK_FAILURE_ONLY") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def loop_max_repeat() -> int:
    raw = (os.environ.get("SMARTCLAW_LOOP_MAX_REPEAT") or "").strip()
    if not raw:
        return 3
    try:
        v = int(raw)
        return max(2, min(v, 50))
    except ValueError:
        return 3


def _loop_context_key() -> str:
    try:
        from smartclaw.auth.tool_gate import get_tool_security_context

        tctx = get_tool_security_context()
        if tctx:
            return (
                f"{tctx.tenant_id}\x1f{tctx.agent_id}\x1f"
                f"{getattr(tctx, 'feishu_open_id', '') or ''}"
            )
    except Exception:
        pass
    return "_default"


def get_loop_detector() -> Optional[LoopDetector]:
    if not loop_detection_enabled():
        return None
    key = _loop_context_key()
    with _detectors_lock:
        d = _detectors.get(key)
        if d is None:
            while len(_detectors) >= MAX_DETECTOR_KEYS:
                _detectors.pop(next(iter(_detectors)))
            d = LoopDetector(
                max_history=30,
                max_repeat=loop_max_repeat(),
                cooldown_seconds=5.0,
            )
            _detectors[key] = d
        return d


def format_tool_invocation_line(name: str, parameters: dict[str, Any]) -> str:
    """Registry 工具：稳定序列化参数，供循环检测签名使用。"""
    try:
        body = json.dumps(parameters or {}, sort_keys=True, ensure_ascii=False, default=str)
    except TypeError:
        body = str(parameters)
    return body
