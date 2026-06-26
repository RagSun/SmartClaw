"""
宿主命令执行策略（Exec Policy）

与 ``agent/tools``（LLM 可调用的业务工具实现）及 ``tool_packages``（外置扩展包安装）区分：
本模块只对「Shell 命令字符串」做白/黑名单与危险模式判定，供 host_command_gate 等调用。
"""

from .engine import (
    ExecutionLayer,
    PolicyAction,
    PolicyResult,
    ToolPolicy,
    ToolPolicyConfig,
    check_command,
    get_default_policy,
)

__all__ = [
    "ToolPolicy",
    "ToolPolicyConfig",
    "PolicyResult",
    "PolicyAction",
    "ExecutionLayer",
    "check_command",
    "get_default_policy",
]
