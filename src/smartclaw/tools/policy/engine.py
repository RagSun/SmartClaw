"""兼容别名：实现已迁至 ``smartclaw.exec_policy.engine``。"""

import warnings

from smartclaw.exec_policy.engine import *  # noqa: F403

warnings.warn(
    "smartclaw.tools.policy.engine 已迁至 smartclaw.exec_policy.engine，请更新 import。",
    DeprecationWarning,
    stacklevel=2,
)
