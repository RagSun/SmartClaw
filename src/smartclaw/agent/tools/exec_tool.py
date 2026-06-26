"""
Shell 执行工具

允许 Agent 执行系统命令。

宿主命令策略（本工具内）统一经 host_command_gate：先 Tool Policy，再合并 Shell 白名单。
工具名「exec」另受 auth.tool_required_roles_any 门禁（在 ToolRegistry.execute）。
"""

import asyncio
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from smartclaw.agent.host_command_gate import (
    HostCommandVerdict,
    build_exec_tool_denial_dict,
    evaluate_host_command,
)
from smartclaw.auth.tool_gate import get_tenant_integration_env
from smartclaw.config.loader import get_config
from smartclaw.console import info
from smartclaw.debug_session_log import debug_ndjson


class ExecTool:
    """Shell 执行工具"""

    def __init__(self):
        self.name = "exec"
        self.description = "执行 Shell 命令并返回输出结果"
        self.parameters = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 Shell 命令"},
                "timeout": {
                    "type": "integer",
                    "description": "超时时间（秒），默认 30",
                    "default": 30,
                },
                "cwd": {
                    "type": "string",
                    "description": "工作目录，默认系统临时目录（跨平台）",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        timeout: int = 30,
        cwd: str | None = None,
        *,
        host_verdict: HostCommandVerdict | None = None,
    ) -> dict[str, Any]:
        """执行 Shell 命令"""
        cfg = get_config()
        if host_verdict is not None:
            verdict = host_verdict
            if not verdict.allowed:
                return build_exec_tool_denial_dict(command, verdict)
            policy_result = verdict.policy_result
        else:
            verdict = evaluate_host_command(command, cfg=cfg)
            policy_result = verdict.policy_result

            if not verdict.allowed:
                return build_exec_tool_denial_dict(command, verdict)

        # ========== Step 3: 文件存在性预检 ==========
        pre_check_match = re.match(r'(?:python3?\s+)(.+?\.py)(?:\s|$)', command.strip())
        if pre_check_match:
            script_path = pre_check_match.group(1).strip()
            if not os.path.isabs(script_path):
                script_abs = os.path.join(os.getcwd(), script_path)
            else:
                script_abs = script_path
            
            if not os.path.exists(script_abs):
                err_msg = (
                    "错误：文件不存在 \"" + script_abs + "\"\n\n"
                    "建议：先使用 write_file 工具创建文件，再运行。"
                )
                return {
                    "success": False,
                    "output": err_msg,
                    "exit_code": 127,
                    "error": "FILE_NOT_FOUND",
                }

        # ========== Step 4: 命令执行 ==========
        # 拦截 claude/gemini 等交互式命令，自动后台执行
        cmd_lower = command.lower().strip()
        block_patterns = [r"^claude", r"^gemini"]
        if any(re.search(p, cmd_lower) for p in block_patterns):
            if not command.strip().endswith("--yes") and not command.strip().endswith("&"):
                safe_cmd = f"{command.rstrip()} --yes"
            else:
                safe_cmd = command
            log_file = "./smartclaw_workspace/cli_agent.log"
            safe_cmd = f"nohup {safe_cmd} > {log_file} 2>&1 &"
            command = safe_cmd

        try:
            work_dir = Path(cwd)
            work_dir.mkdir(parents=True, exist_ok=True)

            tenant_env = get_tenant_integration_env()
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env={
                    **os.environ,
                    **tenant_env,
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                },
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )

                output = stdout.decode("utf-8", errors="replace")
                error = stderr.decode("utf-8", errors="replace")

                out_payload = {
                    "success": proc.returncode == 0,
                    "output": output,
                    "error": error if proc.returncode != 0 else "",
                    "exit_code": proc.returncode,
                    "policy": str(policy_result),
                }
                # region agent log
                debug_ndjson(
                    "H-D",
                    "exec_tool.py:ExecTool.execute_done",
                    "local subprocess finished",
                    {
                        "cwd": str(work_dir),
                        "exit_code": proc.returncode,
                        "out_head": (output[:320].replace("\n", "\\n")),
                        "err_head": (error[:200].replace("\n", "\\n")) if error else "",
                    },
                )
                # endregion
                return out_payload

            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {
                    "success": False,
                    "error": f"命令执行超时 ({timeout}秒)",
                    "output": "",
                    "exit_code": -1,
                    "policy": str(policy_result),
                }

        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "output": "",
                "exit_code": -1,
                "policy": str(policy_result),
            }


async def exec_handler(command: str, timeout: int = 30, cwd: str = "/tmp") -> str:
    """exec 工具的处理函数"""
    from smartclaw.agent.host_command_gate import (
        evaluate_host_command,
        format_exec_handler_denial_string,
    )
    from smartclaw.agent.sandbox_context import get_runner_sandbox
    from smartclaw.agent.tools import get_tool_registry
    
    cfg = get_config()
    verdict = evaluate_host_command(command, cfg=cfg)
    if not verdict.allowed:
        return format_exec_handler_denial_string(command, verdict)

    registry = get_tool_registry()

    active = get_runner_sandbox()
    if active:
        sandbox_backend, sandbox_instance_id = active
    else:
        sandbox_backend = registry.sandbox_backend
        sandbox_instance_id = registry.sandbox_instance_id

    # region agent log
    debug_ndjson(
        "H-A",
        "exec_tool.py:exec_handler_branch",
        "sandbox vs local exec routing",
        {
            "runner_sandbox_active": bool(active),
            "use_sandbox_execute": bool(sandbox_backend and sandbox_instance_id),
            "sandbox_backend": type(sandbox_backend).__name__ if sandbox_backend else None,
            "sandbox_instance_id_preview": (
                (str(sandbox_instance_id)[:48] + "…")
                if sandbox_instance_id and len(str(sandbox_instance_id)) > 48
                else sandbox_instance_id
            ),
            "command_preview": command[:260].replace("\n", " "),
        },
    )
    # endregion
    
    # 宿主策略已与本地路径对齐；此处优先沙箱执行
    if sandbox_backend and sandbox_instance_id:
        try:
            auth_env = {
                k: os.environ[k]
                for k in ["ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY"]
                if k in os.environ
            }
            auth_env.update(get_tenant_integration_env())
            
            # 通过沙箱执行
            result = await sandbox_backend.execute(
                instance_id=sandbox_instance_id,
                command=command,
                timeout_ms=timeout * 1000,
            )
            
            info(f"[EXEC-SANDBOX] 在沙箱 {sandbox_instance_id} 执行: {command[:100]}...")
            
            if result.exit_code == 0:
                return result.stdout
            else:
                return f"[沙箱错误 {result.exit_code}]\n{result.stderr}\n{result.stdout}"
                
        except Exception as e:
            info(f"[EXEC-SANDBOX] 沙箱执行失败，回退到本地: {e}")
    
    # 本地执行 (Fallback)
    info(f"[EXEC-LOCAL] 执行命令: {command[:200]}")
    
    tool = ExecTool()
    result = await tool.execute(command, timeout, cwd, host_verdict=verdict)
    if result["success"]:
        return result["output"]
    else:
        return f"错误: {result['error']}\n{result.get('output', '')}"
