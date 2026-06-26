"""Agent 执行工作区路径解析与标准 Markdown 脚手架（OpenClaw 风格）。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import smartclaw.paths as paths
from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_scoped_child

if TYPE_CHECKING:
    from smartclaw.config.loader import Config

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "agent_workspace"

# 配置指向的工作区根 → 实际可用根 的记忆表：可写性探测只在首次解析时执行一次，
# 避免在 resolve_agent_workspace_for_tools 等热路径上重复 mkdir/探测。
_workspace_base_cache: dict[str, Path] = {}


def default_agent_workspace_base(cfg: Config | None = None) -> Path:
    """全局默认的「每 Agent 一层子目录」的根路径。"""
    raw = ""
    if cfg is not None and getattr(cfg, "smartclaw", None):
        raw = (getattr(cfg.smartclaw, "agent_workspace_base", None) or "").strip()
    if not raw:
        raw = (os.environ.get("SMARTCLAW_AGENT_WORKSPACE_BASE") or "").strip()
    if not raw:
        return (paths.USER_HOME / "workspace").resolve()

    cached = _workspace_base_cache.get(raw)
    if cached is not None:
        return cached

    resolved = Path(os.path.expanduser(raw)).expanduser().resolve()
    # 与 AgentManager._preferred_new_agent_config_dir 一致：配置指向的根不可写时
    # （典型场景：SMARTCLAW_HOME=/opt/smartclaw 由 root 拥有，当前用户无写权限），
    # 退回用户目录，避免 scaffold_agent_workspace 在 mkdir 时抛 PermissionError。
    chosen = resolved
    try:
        resolved.mkdir(parents=True, exist_ok=True)
        if not os.access(resolved, os.W_OK):
            raise PermissionError(str(resolved))
    except OSError:
        chosen = (paths.USER_HOME / "workspace").resolve()
    _workspace_base_cache[raw] = chosen
    return chosen


def tenant_agent_workspace_base(tenant_id: str | None, cfg: Config | None = None) -> Path:
    """Return the workspace base for a tenant.

    A tenant may define its own ``agent_workspace_base``. When absent, the
    global base is used. The default tenant keeps the historical layout.
    """
    tenant = normalize_tenant_id(tenant_id)
    if tenant != DEFAULT_TENANT_ID and cfg is not None:
        tc = getattr(cfg, "tenants", {}).get(tenant)
        raw = (getattr(tc, "agent_workspace_base", "") or "").strip() if tc else ""
        if raw:
            return Path(os.path.expanduser(raw)).expanduser().resolve()
    return default_agent_workspace_base(cfg)


def merge_workspace_resolution_snap(
    full_agent_config: dict[str, Any] | None,
    agent_profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    与 Runner.start 中 DeepAgents 工作区一致：磁盘 agent.json ∪ 运行时 profile。
    workspace 字段：profile **未声明** workspace 时用磁盘；
    profile **声明了** workspace 仅当去空白非空时才覆盖磁盘，否则回退磁盘值（避免空串静默抹掉自定义子目录）。
    """
    full = dict(full_agent_config or {})
    prof = dict(agent_profile or {})
    snap: dict[str, Any] = {**full, **prof}
    # profile 显式写入空串时退回磁盘配置（与「清空=误配」的常见运维语义一致）
    if "workspace" in prof:
        pv = prof.get("workspace")
        stripe = "" if pv is None else str(pv).strip()
        snap["workspace"] = stripe if stripe else full.get("workspace", "")
    else:
        snap["workspace"] = full.get("workspace", "")
    return snap


def file_tool_workspace_roots_detail() -> tuple[list[Path], str | None]:
    """
    read_file/write_file 允许路径根列表，及「Agent 盘根不可用」时的说明。

    第二项在非 None 时表示：配置未能加载或无法在「带 agent 的对话上下文」解析工作区，
    此时仅 `/tmp`、`/root`（解析后）可用，其它路径易被拒——便于工业排障而非静默失效。
    """
    roots_base = [Path("/tmp").resolve(), Path("/root").resolve()]
    wr = resolve_agent_workspace_for_tools()
    if wr is not None:
        return [*roots_base, wr.resolve()], None

    hint: str | None = None
    try:
        from smartclaw.auth.tool_gate import get_tool_security_context

        ctx = get_tool_security_context()
        if ctx is not None and getattr(ctx, "agent_id", None):
            hint = (
                "无法解析当前 Agent 工作区根路径（通常为配置加载失败、或未绑定 Runner "
                "工作区快照）。仅允许写入/读取位于解析后的系统临时路径下的文件；如需 "
                "使用工作区内相对路径请先修复服务端配置并重启。"
            )
    except Exception:
        pass

    return list(roots_base), hint


def resolve_agent_workspace_for_tools(
    cfg: Config | None = None,
    *,
    tool_security_agent_id: str | None = None,
    tool_security_tenant_id: str | None = None,
) -> Path | None:
    """
    Registry 内置工具（read_file / write_file 等）与 Runner/DeepAgents 使用同一磁盘根。

    优先顺序：
    1. ``get_workspace_resolution_snap()``（Runner 在消息 / DeepAgents / 同步工具调用路径注入的合并快照）；
    2. exec 快照 ``get_agent_config_for_exec``（通常为磁盘 agent.json）；
    3. AgentManager._read_config（需具备 ToolSecurityContext.agent_id）。

    ``tenant_id``：与 Runner 对齐——若存在 ToolSecurityContext，优先其 ``tenant_id``，
    其次快照 / 配置文件中的 ``tenant_id``。
    """
    cfg = cfg or _load_runtime_config_for_workspace_tools()
    if cfg is None:
        return None
    merged: dict[str, Any] | None = None
    try:
        from smartclaw.agent.tools.exec_context import (
            get_agent_config_for_exec,
            get_workspace_resolution_snap,
        )

        ws_snap = get_workspace_resolution_snap()
        if ws_snap is not None:
            merged = dict(ws_snap)
        else:
            exec_snap = get_agent_config_for_exec()
            if exec_snap:
                merged = dict(exec_snap)
    except Exception:
        merged = None

    from smartclaw.auth.tool_gate import get_tool_security_context
    from smartclaw.agent.manager import AgentManager

    tctx_agent = tool_security_agent_id
    tctx_tenant = tool_security_tenant_id
    if tctx_agent is None or tctx_tenant is None:
        ctx = get_tool_security_context()
        if ctx:
            tctx_agent = tctx_agent or getattr(ctx, "agent_id", None)
            tctx_tenant = tctx_tenant or getattr(ctx, "tenant_id", None)

    raw_tid = None
    if tctx_tenant is not None and str(tctx_tenant).strip():
        raw_tid = str(tctx_tenant).strip()
    elif merged:
        mf = merged.get("tenant_id")
        if mf is not None and str(mf).strip():
            raw_tid = str(mf).strip()
    tenant = normalize_tenant_id(raw_tid)

    logical = ""
    if merged:
        logical = str(merged.get("name") or "").strip()
    if not logical:
        logical = str(tctx_agent or "").strip() or "default"

    disk: dict[str, Any] | None = None
    if not merged and tctx_agent:
        disk = AgentManager()._read_config(tctx_agent, tenant_id=tenant) or {}

    snap = merged if merged else (disk if disk is not None else {})
    return resolve_agent_workspace_dir(logical, snap, cfg, tenant_id=tenant)


def _load_runtime_config_for_workspace_tools():  # Config | None
    try:
        from smartclaw.config.loader import get_config as _gc

        return _gc()
    except Exception:
        return None


def resolve_agent_workspace_dir(
    agent_id: str,
    agent_config: dict[str, Any] | None,
    cfg: Config | None = None,
    tenant_id: str | None = None,
) -> Path:
    """
    解析 DeepAgents / Skills 使用的磁盘根目录。

    - agent.json 中 ``workspace`` 为空：default 租户为 ``{base}/{agent_id}``
      ，非 default 租户为 ``{base}/{tenant_id}/{agent_id}``
    - 绝对路径：直接使用
    - 相对路径：相对租户工作区 base 拼接
    """
    agent_config = agent_config or {}
    tenant = normalize_tenant_id(tenant_id or agent_config.get("tenant_id"))
    override = (agent_config.get("workspace") or "").strip()
    base = tenant_agent_workspace_base(tenant, cfg)
    if not override:
        return tenant_scoped_child(base, agent_id, tenant).resolve()
    p = Path(os.path.expanduser(override)).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (base / override).resolve()


def scaffold_agent_workspace(workspace_root: Path, *, skip_existing: bool = True) -> list[str]:
    """
    将包内 ``templates/agent_workspace`` 同步到 workspace_root：

    - 根目录 ``*.md``（AGENTS / SOUL / …）
    - ``skills/``、``tools/`` 子树（含 README、SHELL_ALLOWLIST 示例）

    返回本次新建或覆盖的相对路径列表（POSIX 风格字符串）。
    """
    workspace_root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    if not _TEMPLATE_DIR.is_dir():
        return created

    for src in sorted(_TEMPLATE_DIR.glob("*.md")):
        dst = workspace_root / src.name
        if dst.exists() and skip_existing:
            continue
        shutil.copyfile(src, dst)
        created.append(src.name)

    for sub in ("skills", "tools"):
        src_sub = _TEMPLATE_DIR / sub
        if not src_sub.is_dir():
            (workspace_root / sub).mkdir(parents=True, exist_ok=True)
            continue
        for src in sorted(src_sub.rglob("*")):
            if not src.is_file():
                continue
            rel = src.relative_to(src_sub)
            dst = workspace_root / sub / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and skip_existing:
                continue
            shutil.copyfile(src, dst)
            created.append(f"{sub}/{rel.as_posix()}")

    return created


__all__ = [
    "default_agent_workspace_base",
    "file_tool_workspace_roots_detail",
    "merge_workspace_resolution_snap",
    "resolve_agent_workspace_dir",
    "resolve_agent_workspace_for_tools",
    "scaffold_agent_workspace",
    "tenant_agent_workspace_base",
]
