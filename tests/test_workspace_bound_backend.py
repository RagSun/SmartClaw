"""Agent 工作区绑定后端：路径扫描与 virtual 根行为。"""

import os

import pytest

from smartclaw.agent.tools import read_tool, write_tool
from smartclaw.agent.tools.read_tool import ReadTool
from smartclaw.agent.tools.registry import ToolRegistry
from smartclaw.agent.tools.write_tool import WriteTool
from smartclaw.agent.workspace_bound_backend import (
    WorkspaceBoundLocalShellBackend,
    _scan_command_for_escape,
    normalize_workspace_tool_path,
)
from smartclaw.auth.tool_gate import (
    ToolSecurityContext,
    get_tool_security_context,
    reset_tool_security_context,
    set_tool_security_context,
)


@pytest.fixture
def root(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return d.resolve()


def test_blocks_windows_abs_outside(root):
    if os.name != "nt":
        pytest.skip("Windows 盘符路径扫描")
    msg = _scan_command_for_escape("mkdir Z:\\smartclaw_escape_probe", root)
    assert msg is not None
    assert "工作区外" in msg or "外" in msg


def test_allows_path_under_root_windows(root):
    if os.name != "nt":
        pytest.skip("Windows 路径")
    sub = str(root / "中国红")
    cmd = f'mkdir "{sub}"'
    assert _scan_command_for_escape(cmd, root) is None


def test_blocks_cd_parent(root):
    msg = _scan_command_for_escape("cd .. && dir", root)
    assert msg is not None
    msg2 = _scan_command_for_escape("cd ..", root)
    assert msg2 is not None


def test_allows_relative_only(root):
    assert _scan_command_for_escape("mkdir 子目录", root) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/workspace/docs/readme.md", "/docs/readme.md"),
        ("workspace/docs/readme.md", "docs/readme.md"),
        ("file:///workspace/docs/readme.md", "/docs/readme.md"),
        ("@/workspace/docs/readme.md", "@/docs/readme.md"),
        ("/docs/readme.md", "/docs/readme.md"),
    ],
)
def test_normalize_workspace_tool_path(raw, expected):
    assert normalize_workspace_tool_path(raw) == expected


def test_write_maps_workspace_pseudoroot_to_real_workspace(root):
    backend = WorkspaceBoundLocalShellBackend(root_dir=root)

    result = backend.write("/workspace/docs/readme.md", "ok")

    assert not getattr(result, "error", None)
    assert (root / "docs" / "readme.md").read_text(encoding="utf-8") == "ok"
    assert not (root / "workspace" / "docs" / "readme.md").exists()
    assert backend.write_records[-1]["host_path"] == str(root / "docs" / "readme.md")


def test_deepagents_write_file_denied_without_required_role(root):
    backend = WorkspaceBoundLocalShellBackend(root_dir=root)
    tok = set_tool_security_context(
        ToolSecurityContext(
            tenant_id="dept_a",
            feishu_open_id="ou_test",
            roles=("default",),
            agent_id="bot_dept_a",
            session_id="s1",
        )
    )
    try:
        result = backend.write("docs/blocked.txt", "nope")
    finally:
        reset_tool_security_context(tok)

    assert getattr(result, "error", None)
    assert "权限门禁拒绝" in result.error
    assert not (root / "docs" / "blocked.txt").exists()


@pytest.mark.asyncio
async def test_registry_write_maps_workspace_pseudoroot(root, monkeypatch):
    roots = [root]
    monkeypatch.setattr(write_tool, "_workspace_roots_detail", lambda: (roots, None))
    result = await WriteTool().execute("/workspace/docs/registry.txt", "ok")

    assert result["success"]
    assert (root / "docs" / "registry.txt").read_text(encoding="utf-8") == "ok"
    assert not (root / "workspace" / "docs" / "registry.txt").exists()


@pytest.mark.asyncio
async def test_registry_read_maps_workspace_pseudoroot(root, monkeypatch):
    (root / "docs").mkdir()
    (root / "docs" / "registry.txt").write_text("ok", encoding="utf-8")
    roots = [root]
    monkeypatch.setattr(read_tool, "_workspace_roots_detail", lambda: (roots, None))
    result = await ReadTool().execute("/workspace/docs/registry.txt")

    assert result["success"]
    assert result["content"] == "ok"


@pytest.mark.asyncio
async def test_registry_sync_tool_preserves_security_context_in_executor():
    registry = ToolRegistry()

    def handler():
        ctx = get_tool_security_context()
        return {"has_context": ctx is not None, "roles": list(ctx.roles) if ctx else []}

    registry.register("ctx_probe", "context probe", handler)
    tok = set_tool_security_context(
        ToolSecurityContext(
            tenant_id="dept_a",
            feishu_open_id="ou_test",
            roles=("developer",),
            agent_id="bot_dept_a",
            session_id="s1",
        )
    )
    try:
        result = await registry.execute("ctx_probe", {})
    finally:
        reset_tool_security_context(tok)

    assert result.success
    assert result.result == {"has_context": True, "roles": ["developer"]}
