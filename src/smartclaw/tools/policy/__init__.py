"""兼容别名：宿主命令策略已迁至 ``smartclaw.exec_policy``。"""

import warnings

from smartclaw.exec_policy import (
    ExecutionLayer,
    PolicyAction,
    PolicyResult,
    ToolPolicy,
    ToolPolicyConfig,
    check_command,
    get_default_policy,
)

warnings.warn(
    "smartclaw.tools.policy 已更名为 smartclaw.exec_policy（宿主 Shell 命令策略），请更新 import。",
    DeprecationWarning,
    stacklevel=2,
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
