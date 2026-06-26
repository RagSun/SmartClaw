"""项目根级 pytest fixtures：把测试默认隔离到临时目录。

背景
====
生产代码里仍有少量"直接 ``Path.home() / .smartclaw/...`` 落盘"的旧代码路径（例如
``memory/daily.py`` 在不传 ``memory_dir`` 时、历史版本的 ``audit/logger.py`` 等）。
如果用户家目录里已存在一份由 ``sudo`` 或别的账户创建、当前用户写不进去的
``~/.smartclaw``（实测里就是这种情况），整套 memory / audit 相关测试就会全军
覆没在 ``PermissionError`` 上——而它**只是测试运行环境的副作用**，并非被测代码
真的不工作。

策略
====
- **autouse fixture**：每个测试 session 启动前，自动把以下路径环境变量指向
  ``tmp_path_factory`` 拨给我们的临时根，离开作用域后由 pytest 自己清理：
    * ``SMARTCLAW_HOME``      → 由 ``smartclaw.paths`` 读取，决定 ``INSTALL_ROOT``
    * ``SMARTCLAW_AUDIT_DIR`` → 审计 JSONL 落盘根
    * ``HOME``                → 兜底，让 ``Path.home() / ".smartclaw"`` 也走 tmp
- 仅在 ``os.environ`` **未显式设置**时才注入；显式设置过的开发者本地 env 保留。
- 不强制 ``monkeypatch`` 任何生产模块；纯粹通过 env 改向，零侵入。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


_ISOLATED_ROOT_ENV_KEYS = (
    "SMARTCLAW_HOME",
    "SMARTCLAW_AUDIT_DIR",
    "SMARTCLAW_MEMORY_DATA_DIR",
    "SMARTCLAW_SESSION_DIR",
    "SMARTCLAW_TEMP_DIR",
    "EVENT_BUS_DIR",
    "SUBAGENT_STATE_DIR",
)


@pytest.fixture(autouse=True, scope="session")
def _isolate_user_paths_for_tests(tmp_path_factory: pytest.TempPathFactory):
    """session 级隔离：将 ``HOME`` / ``SMARTCLAW_*`` 改向临时根。

    - 仅在 env 未显式提供时设置；不覆盖开发者手动指定的值；
    - session 结束时恢复原值。
    """
    isolation_root: Path = tmp_path_factory.mktemp("smartclaw-test-root")

    saved: dict[str, str | None] = {}

    def _setdefault_env(key: str, value: str) -> None:
        saved[key] = os.environ.get(key)
        if not os.environ.get(key):
            os.environ[key] = value

    # HOME 兜底（任何残留的 Path.home() / ".smartclaw/..." 都会落到这里）
    _setdefault_env("HOME", str(isolation_root))

    # paths.py 暴露的所有 env 入口
    _setdefault_env("SMARTCLAW_HOME", str(isolation_root))
    _setdefault_env("SMARTCLAW_AUDIT_DIR", str(isolation_root / "audit"))
    _setdefault_env("SMARTCLAW_MEMORY_DATA_DIR", str(isolation_root / "memory"))
    _setdefault_env("SMARTCLAW_SESSION_DIR", str(isolation_root / "sessions"))
    _setdefault_env("SMARTCLAW_TEMP_DIR", str(isolation_root / "tmp"))
    _setdefault_env("EVENT_BUS_DIR", str(isolation_root / "event-bus"))
    _setdefault_env("SUBAGENT_STATE_DIR", str(isolation_root / "subagent-state"))

    yield isolation_root

    # 恢复原状（即使中途有测试改了，也以原值为准）
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
