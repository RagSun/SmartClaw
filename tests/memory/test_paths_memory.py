"""记忆相关路径单测。"""

from pathlib import Path

import pytest

import smartclaw.paths as paths


def test_default_memory_data_dir_uses_install_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SMARTCLAW_MEMORY_DATA_DIR", raising=False)
    install_root = tmp_path / "hmw"
    monkeypatch.setattr(paths, "INSTALL_ROOT", install_root)
    d = paths.default_memory_data_dir("my_agent")
    assert d == install_root / "data" / "memory" / "my_agent"


def test_default_memory_data_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "memroot"
    monkeypatch.setenv("SMARTCLAW_MEMORY_DATA_DIR", str(root))
    d = paths.default_memory_data_dir("x")
    assert d == root / "x"


def test_default_memory_data_dir_tenant_scoped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SMARTCLAW_MEMORY_DATA_DIR", raising=False)
    install_root = tmp_path / "hmw"
    monkeypatch.setattr(paths, "INSTALL_ROOT", install_root)
    d = paths.default_memory_data_dir("bot_dept_a", tenant_id="dept_a")
    assert d == install_root / "data" / "memory" / "dept_a" / "bot_dept_a"
