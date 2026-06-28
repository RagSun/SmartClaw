import smartclaw.paths as paths
"""
SmartClaw CLI 入口模块

提供所有命令行接口，包括：
- smartclaw --help
- smartclaw init
- smartclaw start
- smartclaw status
- smartclaw doctor（闭环：含 --tenant LLM 合并探活、网关/模型自检）
- smartclaw llm-test（含 --tenant，与飞书 WS 合并链一致）
- smartclaw config show/set/edit/shell-allowlist（含 import-json 批量）
- smartclaw config langsmith（LangSmith API Key / 启用禁用 / project）
- smartclaw channel setup / channel add-feishu / channel bind-feishu
- smartclaw agent add
- smartclaw mcp add/list/test/remove/on/off；smartclaw agent mcp list/enable/disable
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from smartclaw.config.loader import Config

import click
import typer
from rich.table import Table
from typer.core import TyperGroup

from smartclaw import __version__
from smartclaw.console import (
    console,
    error,
    info,
    print_panel,
    print_table,
    success,
    title,
    warning,
)


# ── 加载 .env 文件（12-factor 配置注入） ──
def _load_dotenv() -> None:
    """将项目 .env 文件加载到 os.environ，供 ConfigLoader 覆盖使用。"""
    try:
        from dotenv import load_dotenv as _load
    except ImportError:
        return  # python-dotenv 未安装，静默跳过

    candidates: list[Path] = []
    # 1. 环境变量显式指定路径
    explicit = os.environ.get("SMARTCLAW_DOTENV_PATH")
    if explicit:
        candidates.append(Path(explicit))
    # 2. 项目根目录（cli.py 上溯三层：cli.py → smartclaw → src → 项目根）
    candidates.append(Path(__file__).resolve().parent.parent.parent / ".env")
    # 3. 当前工作目录
    candidates.append(Path.cwd() / ".env")

    for candidate in candidates:
        if candidate.exists():
            _load(str(candidate))
            return


_load_dotenv()

# 创建主应用
app = typer.Typer(
    name="smartclaw",
    help="SmartClaw - 生产级企业 AI Agent 平台",
    add_completion=False,
    no_args_is_help=True,
)

# 创建子命令组
config_app = typer.Typer(help="配置管理命令")
agent_app = typer.Typer(help="Agent 管理命令")
auth_app = typer.Typer(help="权限管理命令（用户角色、工具角色门禁）")
auth_roles_app = typer.Typer(help="维护 config.toml [auth.feishu_open_id_roles_by_tenant]")
auth_tool_app = typer.Typer(help="维护 config.toml [auth.tool_required_roles_any]")
auth_users_app = typer.Typer(help="从审计/会话记录查看最近飞书用户")
mcp_app = typer.Typer(help="MCP Server 注册与联调命令")
config_shell_allowlist_app = typer.Typer(
    help="维护全局 config.toml [execution] 宿主 exec 白名单（shell_allowlist / path）",
)
agent_shell_allowlist_app = typer.Typer(
    help="维护某 Agent 的 agent.json shell_allowlist 相关字段",
)
agent_mcp_app = typer.Typer(help="维护某 Agent 的 MCP Server 与 allowed_tools")
docker_app = typer.Typer(help="Docker 容器管理命令")
channel_app = typer.Typer(help="渠道配置命令")
skills_app = typer.Typer(help="Skills 管理命令")

# 注册子命令组
app.add_typer(config_app, name="config")
app.add_typer(agent_app, name="agent")
app.add_typer(auth_app, name="auth")
auth_app.add_typer(auth_roles_app, name="roles")
auth_app.add_typer(auth_tool_app, name="tool")
auth_app.add_typer(auth_users_app, name="users")
app.add_typer(mcp_app, name="mcp")
config_app.add_typer(config_shell_allowlist_app, name="shell-allowlist")


def _token_looks_like_langsmith_key(token: str) -> bool:
    """识别 CLI 误写成的「直接把 key 当子命令」形式（如 lsv2_pt_…）。"""
    t = (token or "").strip()
    if not t or t.startswith("-"):
        return False
    return t.startswith("lsv2_") and len(t) >= 16


class LangSmithCliGroup(TyperGroup):
    """首个参数若为 LangSmith API key（lsv2_*），自动按 set-api-key 处理。"""

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[Optional[str], Optional[click.Command], list[str]]:
        if (
            args
            and _token_looks_like_langsmith_key(args[0])
            and self.get_command(ctx, "set-api-key") is not None
        ):
            return super().resolve_command(ctx, ["set-api-key", *args])
        return super().resolve_command(ctx, args)


config_langsmith_app = typer.Typer(
    cls=LangSmithCliGroup,
    help=(
        "LangSmith / LangChain 追踪（写入 [langsmith] 并设置 LANGCHAIN_*，不覆盖已有环境变量）。"
        " 也可直接: smartclaw config langsmith <lsv2_... 密钥>（等同于 set-api-key）。"
    ),
)
config_app.add_typer(config_langsmith_app, name="langsmith")
agent_app.add_typer(agent_shell_allowlist_app, name="shell-allowlist")
agent_app.add_typer(agent_mcp_app, name="mcp")
app.add_typer(channel_app, name="channel")
app.add_typer(docker_app, name="docker")
app.add_typer(skills_app, name="skills")


def version_callback(value: bool) -> None:
    """显示版本信息"""
    if value:
        print_panel(f"SmartClaw v{__version__}", title_str="版本", style="cyan")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="显示版本信息",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """
    SmartClaw - 生产级企业 AI Agent 平台

    每个 Agent 运行在独立 microVM 中，实现硬件级隔离。
    支持飞书和企业微信双渠道。
    """
    pass


# ==================== init 命令 ====================


def _resolve_init_workspace_base(project_path: Path, workspace_base_opt: Optional[str]) -> str:
    """统一工作区基目录：默认可为 <project>/workspace，或 CLI 显式传入的路径。"""
    if workspace_base_opt and str(workspace_base_opt).strip():
        return Path(str(workspace_base_opt).strip()).expanduser().resolve().as_posix()
    return (project_path / "workspace").resolve().as_posix()


def _merge_unified_agent_workspace_base(
    config_path: Path,
    resolved_base: str,
    *,
    overwrite: bool,
) -> None:
    """写入 [smartclaw].agent_workspace_base，使 init 与脚手架落在同一棵目录树下。"""
    import tomllib as tomli
    import tomli_w

    if not config_path.is_file():
        return
    try:
        with open(config_path, "rb") as f:
            doc = tomli.load(f)
    except Exception as e:
        warning(f"  读取配置以写入 agent_workspace_base 失败: {e}")
        return
    heim_raw = doc.setdefault("smartclaw", {})
    if isinstance(heim_raw, dict):
        heim = heim_raw
    else:
        heim = {}
        doc["smartclaw"] = heim
    current = (heim.get("agent_workspace_base") or "").strip()
    if not overwrite and current:
        return
    heim["agent_workspace_base"] = resolved_base
    try:
        with open(config_path, "wb") as f:
            tomli_w.dump(doc, f)
    except Exception as e:
        warning(f"  写入 agent_workspace_base 失败: {e}")


@app.command("init")
def init_command(
    path: Optional[str] = typer.Option(
        None,
        "--path",
        "-p",
        help=f"项目初始化路径，默认为 {paths.INSTALL_ROOT}（无权限时自动切换到 ~/.smartclaw）",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="强制覆盖已存在的配置",
    ),
    force_workspace: bool = typer.Option(
        False,
        "--force-workspace",
        help="标准 MD 模板：覆盖工作区内已存在的同名文件（默认仅补缺、不覆盖用户修改）",
    ),
    workspace_base: Optional[str] = typer.Option(
        None,
        "--workspace-base",
        help="Agent 执行工作区基目录；默认 <初始化路径>/workspace，并写入 config.toml",
    ),
) -> None:
    """
    初始化 SmartClaw 项目

    创建必要的目录结构和默认配置文件。
    每次 init 结束时会对 default Agent 解析执行工作区并同步标准 Markdown（缺则建；默认不覆盖已有）。
    默认将 [smartclaw].agent_workspace_base 设为 <path>/workspace，目录自洽便于教学与运维。
    """
    from pathlib import Path

    project_path = Path(path) if path else paths.INSTALL_ROOT

    # 检查是否有写权限，如果没有则自动切换到用户目录
    if not path and project_path == paths.INSTALL_ROOT:
        try:
            # 尝试检测父目录是否有写权限
            if not project_path.exists():
                test_path = project_path.parent
                if not os.access(str(test_path), os.W_OK):
                    project_path = paths.USER_HOME
                    info(f"无权限写入 {paths.INSTALL_ROOT}，自动切换到用户目录")
        except Exception:
            project_path = paths.USER_HOME

    title(f"初始化 SmartClaw 项目: {project_path}")

    # 检查目录是否存在
    if project_path.exists() and not force:
        warning(f"目录已存在: {project_path}")
        if not typer.confirm("是否继续？"):
            raise typer.Abort()

    # 创建目录结构
    directories = [
        "config",
        "logs",
        "data",
        "sandboxes",
        "data/agents",
        "data/sessions",
        "workspace",
    ]

    for dir_name in directories:
        dir_path = project_path / dir_name
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
            info(f"  创建目录: {dir_name}")
        except PermissionError:
            error(f"无权限创建目录: {dir_path}")
            error("请使用 --path 指定其他路径，或设置 SMARTCLAW_HOME 环境变量")
            raise typer.Exit(1)

    # 创建默认配置文件
    config_path = project_path / "config" / "config.toml"
    config_created_or_refreshed = False
    if not config_path.exists() or force:
        _create_default_config(config_path)
        success("  创建配置: config/config.toml")
        config_created_or_refreshed = True
    else:
        warning("  配置已存在，跳过: config/config.toml")

    resolved_wb = _resolve_init_workspace_base(project_path, workspace_base)
    _merge_unified_agent_workspace_base(
        config_path,
        resolved_wb,
        overwrite=config_created_or_refreshed or force,
    )
    info(f"  统一工作区基目录: {resolved_wb}")

    # 创建默认 Agent + 工作区脚手架（每次 init 都执行 scaffold）
    init_default_agent(project_path, force_workspace=force_workspace)
    success("  默认 Agent: default（配置与工作区已就绪）")
    
    success(f"初始化完成: {project_path}")
    info(f"提示: 运行/运维时请将 SMARTCLAW_HOME 环境变量指向上述路径，保证与本次 init 为同一棵树")
    info("下一步: 运行 'smartclaw config show' 查看配置")



def _scaffold_from_agent_json(
    agent_json_path: Path,
    *,
    force_overwrite: bool = False,
    config: Optional["Config"] = None,
) -> None:
    """
    读取 agent.json，解析执行工作区并同步标准 MD。
    force_overwrite=True 时用模板覆盖已存在的同名 .md。
    config 优先使用调用方传入的本项目 config.toml，避免误用全局其它路径下的配置。
    """
    import json

    from smartclaw.agent.workspace import resolve_agent_workspace_dir, scaffold_agent_workspace
    from smartclaw.config.loader import get_config

    if not agent_json_path.is_file():
        return
    try:
        data = json.loads(agent_json_path.read_text(encoding="utf-8"))
    except Exception:
        warning(f"  无法解析 {agent_json_path}，跳过工作区脚手架")
        return
    if not isinstance(data, dict):
        return
    logical_name = str(data.get("name", agent_json_path.parent.name))
    cfg = config if config is not None else get_config()
    ws = resolve_agent_workspace_dir(logical_name, data, cfg)
    written = scaffold_agent_workspace(ws, skip_existing=not force_overwrite)
    if force_overwrite and written:
        info(f"  工作区模板: {ws}（已写入/覆盖 {len(written)} 个 Markdown）")
    elif written:
        info(f"  工作区模板: {ws}（新建 {len(written)} 个 Markdown，已存在文件未覆盖）")
    else:
        info(f"  工作区模板: {ws}（标准 MD 已齐，未改动；可用 init --force-workspace 覆盖）")


def _derive_default_channel(config_path: Path) -> str:
    """从 config.toml [channels] 推导默认渠道：取首个 enabled 渠道（feishu 优先），皆未启用回落 feishu。"""
    try:
        if config_path.is_file():
            from smartclaw.config.loader import ConfigLoader

            cfg = ConfigLoader(config_path=config_path).load()
            ch = getattr(cfg, "channels", None)
            if ch is not None:
                if getattr(getattr(ch, "feishu", None), "enabled", False):
                    return "feishu"
                if getattr(getattr(ch, "wecom", None), "enabled", False):
                    return "wecom"
    except Exception:
        pass
    return "feishu"


def init_default_agent(project_path: Path, *, force_workspace: bool = False) -> None:
    """创建或保留全局 default Agent，并始终同步工作区标准 Markdown。

    新建的 agent.json 不写死厂商/模型名，llm 为空对象，合并时完全继承 config.toml [llm]，
    直至用户执行 agent set-llm 或在 agent.json 中显式填写 llm。
    """
    import json

    from smartclaw.agent.naming import canonical_display_name
    from smartclaw.config.loader import ConfigLoader

    agents_dir = project_path / "data" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    default_agent_dir = agents_dir / "default"
    default_agent_dir.mkdir(parents=True, exist_ok=True)

    cfg_path = project_path / "config" / "config.toml"
    channel = _derive_default_channel(cfg_path)
    fdn = canonical_display_name("default", channel)

    # 不在此预填 llm（避免盖住 config.toml [llm]）；需 Agent 专属模型时用 agent set-llm 或在 agent.json 中显式填写 llm。
    config = {
        "name": "default",
        "display_name": fdn,
        "aliases": [fdn],
        "channel": channel,
        "description": "系统默认全局 Agent；未配置 agent.json.llm 时沿用 config.toml [llm]；识图沿用 [vision] 与租户配置。",
        "enabled": True,
        "llm": {},
        "sandbox": {
            "enabled": False,
        },
    }
    # 飞书渠道预留 per-agent 凭证占位；wecom 渠道凭证为全局配置（config.toml [channels.wecom]）
    if channel == "feishu":
        config["feishu"] = {"app_id": "", "app_secret": ""}

    config_file = default_agent_dir / "agent.json"
    if not config_file.exists():
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        success("  已创建 data/agents/default/agent.json")

    cfg_scaffold = None
    if cfg_path.is_file():
        cfg_scaffold = ConfigLoader(config_path=cfg_path).load()

    _scaffold_from_agent_json(config_file, force_overwrite=force_workspace, config=cfg_scaffold)


def _create_default_config(config_path: "Path") -> None:
    """创建默认配置文件：以项目内静态模板 ``config/config.toml`` 复制生成。

    ``agent_workspace_base`` 由 init 随后通过 ``_merge_unified_agent_workspace_base``
    按项目路径重写，故模板中携带的机器路径不会污染新项目。
    """
    import shutil

    # 唯一静态模板来源：项目内 config/config.toml
    template = Path(__file__).resolve().parent.parent.parent / "config" / "config.toml"
    if not template.is_file():
        error(f"静态模板不存在: {template}")
        raise typer.Exit(1)
    shutil.copy(template, config_path)

# ==================== install 命令 ====================


@app.command("install")
def install_command(
    force: bool = typer.Option(False, "--force", "-f", help="强制覆盖已存在的配置"),
) -> None:
    """
    自动安装 SmartClaw（创建目录、复制模板、初始化配置）
    """
    import shutil
    from pathlib import Path
    
    title("SmartClaw 安装向导")
    
    source_dir = Path(__file__).parent.parent.parent
    install_config_dir = paths.CONFIG_DIR
    agents_dir = Path.home() / ".smartclaw" / "agents"
    
    info("[1/4] 创建目录结构...")
    install_config_dir.mkdir(parents=True, exist_ok=True)
    Path.home() / ".smartclaw".mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    success("  目录创建完成")
    
    info("[2/4] 初始化全局配置...")
    config_template = source_dir / "config" / "config.toml"
    config_target = install_config_dir / "config.toml"
    if config_target.exists() and not force:
        info("  全局配置已存在 (跳过)")
    else:
        if config_template.exists():
            shutil.copy(config_template, config_target)
        else:
            error(f"静态模板不存在: {config_template}")
            raise typer.Exit(1)
        success(f"  全局配置已创建: {config_target}")
    
    info("[3/4] 创建默认 Agent...")
    default_agent_target = agents_dir / "default" / "agent.json"
    if default_agent_target.exists() and not force:
        info("  默认 Agent 已存在 (跳过写入 agent.json)")
    else:
        import json

        from smartclaw.agent.naming import canonical_display_name

        default_agent_target.parent.mkdir(parents=True, exist_ok=True)
        channel = _derive_default_channel(config_target)
        fdn = canonical_display_name("default", channel)
        stub = {
            "name": "default",
            "display_name": fdn,
            "aliases": [fdn],
            "channel": channel,
            "enabled": True,
            "llm": {},
            "sandbox": {"enabled": True, "memory_mb": 128, "cpu_count": 1},
        }
        if channel == "feishu":
            stub["feishu"] = {"app_id": "", "app_secret": ""}
        default_agent_target.write_text(
            json.dumps(stub, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        success(f"  默认 Agent 已创建: {default_agent_target}")

    if default_agent_target.exists():
        _scaffold_from_agent_json(default_agent_target, force_overwrite=force)
    
    info("[4/4] 设置权限...")
    import os
    if config_target.exists():
        os.chmod(config_target, 0o600)
    if default_agent_target.exists():
        os.chmod(default_agent_target, 0o600)
    success("  权限设置完成 (600)")
    
    from rich.panel import Panel
    from smartclaw.agent.naming import canonical_display_name

    _channel_def = _derive_default_channel(config_target)
    _fd_def = canonical_display_name("default", _channel_def)
    _channel_label = "飞书" if _channel_def == "feishu" else "企业微信"
    _start_hint = "smartclaw start --feishu --multi-process" if _channel_def == "feishu" else "smartclaw start"
    console.print(Panel(
        f"安装完成！\n\n"
        f"默认 Agent 渠道: {_channel_label}；「机器人/应用名称」约定为: {_fd_def}（与 agent.json display_name 一致）。\n\n"
        "下一步操作:\n"
        "1. 编辑配置: smartclaw config edit\n"
        f"2. 启动服务: {_start_hint}",
        title="安装成功", style="green"
    ))



# ==================== start 命令 ====================


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _smartclaw_home_required_messages() -> list[str]:
    """飞书启动前未设置 SMARTCLAW_HOME 时的分平台说明。"""
    import sys

    lines = [
        "飞书服务要求显式设置 SMARTCLAW_HOME，避免 init 路径与运行时路径分叉。",
        f"当前进程将使用 INSTALL_ROOT={paths.INSTALL_ROOT}（未设置 SMARTCLAW_HOME 时）。",
    ]
    if sys.platform == "win32":
        lines.append(
            '请设置为你 init 的目录，例如: $env:SMARTCLAW_HOME="D:\\hmw"'
        )
    else:
        lines.append(
            '请设置为你 init 的目录，例如: export SMARTCLAW_HOME="/home/you/hmw"'
            ' 或 export SMARTCLAW_HOME="$HOME/.smartclaw"'
        )
    lines.append(
        "如确认使用系统默认安装目录，可设置 SMARTCLAW_ALLOW_IMPLICIT_HOME=1 后再启动。"
    )
    return lines


def _runtime_path_preflight(*, feishu_enabled: bool) -> None:
    """Print and enforce the storage roots used by this process."""
    if feishu_enabled and not os.environ.get("SMARTCLAW_HOME") and not _env_truthy(
        "SMARTCLAW_ALLOW_IMPLICIT_HOME"
    ):
        for line in _smartclaw_home_required_messages():
            error(line)
        raise typer.Exit(1)

    try:
        from smartclaw.agent.workspace import default_agent_workspace_base
        from smartclaw.config.loader import get_config

        cfg = get_config()
        workspace_base = default_agent_workspace_base(cfg)
    except Exception as exc:
        warning(f"运行时路径预检读取配置失败: {exc}")
        workspace_base = Path(
            os.environ.get("SMARTCLAW_AGENT_WORKSPACE_BASE")
            or paths.USER_HOME / "workspace"
        )

    info(
        "[runtime paths] SMARTCLAW_HOME="
        + (os.environ.get("SMARTCLAW_HOME") or "<unset>")
    )
    info(f"[runtime paths] install_root={paths.INSTALL_ROOT}")
    info(f"[runtime paths] config_file={paths.get_config_file()}")
    info(f"[runtime paths] workspace_base={workspace_base}")
    info(f"[runtime paths] session_dir={paths.SESSION_DIR}")
    info(f"[runtime paths] memory_base={paths.default_memory_data_dir('default')}")
    info(f"[runtime paths] temp_dir={paths.TEMP_DIR}")


@app.command("start")
def start_command(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="监听地址"),
    port: int = typer.Option(8000, "--port", "-p", help="监听端口"),
    workers: int = typer.Option(1, "--workers", "-w", help="工作进程数"),
    reload: bool = typer.Option(False, "--reload", help="开发模式自动重载"),
    feishu: bool = typer.Option(True, "--feishu/--no-feishu", help="启动飞书服务"),
    multi_process: bool = typer.Option(False, "--multi-process/--single-process", help="多进程模式（每个 App 独立进程）"),
    http: bool = typer.Option(True, "--http/--no-http", help="启动 HTTP API 服务"),
    daemon: bool = typer.Option(False, "--daemon", "-d", help="后台守护进程模式运行"),
) -> None:
    """
    启动 SmartClaw 服务

    启动 HTTP API 服务和可选的飞书长连接服务。
    默认同时启动 HTTP API (端口 8000) 和飞书长连接。

    示例:
        smartclaw start                    # 启动全部服务
        smartclaw start --feishu         # 只启动飞书服务
        smartclaw start --http           # 只启动 HTTP 服务
        smartclaw start --reload         # 开发模式（代码自动重载）
        smartclaw start --daemon         # 后台守护进程模式运行
    """
    import os
    import sys
    import threading
    from pathlib import Path

    # 初始化日志配置（解决日志未写入文件的问题）
    log_dir = paths.get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    from smartclaw.console import configure_logging
    configure_logging(str(log_dir / "smartclaw.log"), enabled=True)

    _runtime_path_preflight(feishu_enabled=feishu)

    # PID 文件路径
    run_dir = paths.get_run_dir()
    if not run_dir.exists():
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            run_dir = Path.home() / ".smartclaw" / "run"
            run_dir.mkdir(parents=True, exist_ok=True)
            
    pid_file = run_dir / "smartclaw.pid"
    
    if pid_file.exists():
        from smartclaw.pid_check import pid_is_running

        try:
            old_pid = int(pid_file.read_text().strip())
        except ValueError:
            pid_file.unlink()
        else:
            if pid_is_running(old_pid):
                error(f"SmartClaw 服务已在运行 (PID: {old_pid})")
                raise typer.Exit(1)
            pid_file.unlink()

    if daemon:
        title("启动 SmartClaw 服务 (后台模式)")
        import subprocess
        
        # 准备新进程的命令
        cmd = [sys.executable, "-m", "smartclaw.cli", "start"]
        if host != "0.0.0.0": cmd.extend(["--host", host])
        if port != 8000: cmd.extend(["--port", str(port)])
        if workers != 1: cmd.extend(["--workers", str(workers)])
        if reload: cmd.append("--reload")
        if not feishu: cmd.append("--no-feishu")
        if not http: cmd.append("--no-http")
        
        log_dir = paths.get_log_dir()
        if not log_dir.exists():
            log_dir = Path.home() / ".smartclaw" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            
        out_log = open(log_dir / "smartclaw.out", "a")
        err_log = open(log_dir / "smartclaw.err", "a")
        
        # 启动子进程
        process = subprocess.Popen(
            cmd,
            stdout=out_log,
            stderr=err_log,
            start_new_session=True  # 脱离当前终端
        )
        
        pid_file.write_text(str(process.pid))
        success(f"SmartClaw 服务已在后台启动，PID: {process.pid}")
        info(f"输出日志: {out_log.name}")
        info(f"错误日志: {err_log.name}")
        return

    title("启动 SmartClaw 服务")
    
    # 写入当前 PID
    pid_file.write_text(str(os.getpid()))

    info(f"HTTP API: {'启用' if http else '禁用'} (端口 {port})")
    info(f"飞书长连接: {'启用' if feishu else '禁用'}")
    info(f"工作进程: {workers}")

    if reload:
        warning("开发模式已启用，代码变更将自动重载")

    def run_http():
        """启动 HTTP 服务"""
        if not http:
            return
        import uvicorn

        uvicorn.run(
            "smartclaw.server:app",
            host=host,
            port=port,
            workers=workers if not reload else 1,
            reload=reload,
        )

    def run_feishu():
        """启动飞书长连接服务"""
        if not feishu:
            return
        
        if multi_process:
            # 多进程模式
            info("使用多进程飞书服务架构")
            from smartclaw.feishu_multiprocess import start_service, stop_service
            import signal
            
            # 写入 PID 文件
            pid_file.write_text(str(os.getpid()))
            
            # 注册信号处理
            def signal_handler(sig, frame):
                info("收到停止信号...")
                if pid_file.exists():
                    pid_file.unlink()
                stop_service()
                sys.exit(0)
            
            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)
            
            # 启动服务（阻塞）
            service = start_service()
            try:
                while True:
                    import time
                    time.sleep(1)
            except KeyboardInterrupt:
                stop_service()
        else:
            # 单进程模式（原有逻辑）
            import asyncio
            from smartclaw.feishu_ws_server import main as feishu_main
            asyncio.run(feishu_main())

    try:
        if feishu and http:
            # 同时启动两个服务
            http_thread = threading.Thread(target=run_http, daemon=True)
            http_thread.start()
            run_feishu()
        elif feishu:
            run_feishu()
        else:
            run_http()

    except KeyboardInterrupt:
        info("服务已停止")
    except Exception as e:
        error(f"启动失败: {e}")
        raise typer.Exit(1)


# ==================== status 命令 ====================


@app.command("status")
def status_command() -> None:
    """
    显示 SmartClaw 运行状态

    包括服务状态、Agent 数量、沙箱池状态等。
    """
    from pathlib import Path  # 确保 Path 可用
    import os
    
    title("SmartClaw 状态")

    # 读取 PID 文件判断服务状态
    run_dir = paths.get_run_dir()
    pid_file = run_dir / "smartclaw.pid"
    
    service_status = "[yellow]未启动[/yellow]"
    service_detail = "-"
    sandbox_status = "[yellow]未初始化[/yellow]"
    active_agents = "[dim]0[/dim]"
    active_sessions = "[dim]0[/dim]"
    
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            import os
            if os.path.exists(f"/proc/{pid}"):
                service_status = "[green]运行中[/green]"
                service_detail = f"PID: {pid}"
            else:
                service_status = "[red]已停止[/red]"
                service_detail = "PID 文件过期"
        except Exception:
            service_status = "[red]错误[/red]"
            service_detail = "PID 文件损坏"
    
    # 读取 Agent 列表获取活跃 Agent 数量（支持 default 与 tenant/agent 两种布局）
    agents_count = 0
    agents_dirs = paths.get_agents_dirs()
    for ad in agents_dirs:
        if ad.exists():
            config_files = list(ad.glob("*/agent.json")) + list(ad.glob("*/*/agent.json"))
            count = len({str(cf.parent) for cf in config_files})
            if count > agents_count:
                agents_count = count
    
    # 统计活跃会话数量（default: sessions/<agent>；非 default: sessions/<tenant>/<agent>）
    sessions_count = 0
    sessions_dir = paths.SESSION_DIR
    if sessions_dir.exists():
        sessions_count += sum(1 for sf in sessions_dir.glob("*/*.json"))
        sessions_count += sum(1 for sf in sessions_dir.glob("*/*/*.json"))
    
    # 统计沙箱数量
    sandbox_dir = paths.SANDBOX_DIR
    sandbox_count = 0
    running_sandbox_count = 0
    if sandbox_dir.exists():
        sandbox_count = len([d for d in sandbox_dir.iterdir() if d.is_dir()])
        running_sandbox_count = len([d for d in sandbox_dir.iterdir() if d.is_dir() and (d / "api.sock").exists()])
    
    if agents_count > 0:
        active_agents = f"[green]{agents_count}[/green]"
    else:
        active_agents = "[dim]0[/dim]"
    
    sandbox_detail = f"[dim]{running_sandbox_count} / {sandbox_count}[/dim]" if sandbox_count > 0 else "-"
    sandbox_status = "[green]运行中[/green]" if running_sandbox_count > 0 else "[yellow]未初始化[/yellow]"
    sessions_detail = f"[green]{sessions_count}[/green]" if sessions_count > 0 else "[dim]0[/dim]"
    
    # 服务状态
    table = Table(title="服务状态", show_header=True, header_style="cyan bold")
    table.add_column("项目")
    table.add_column("状态")
    table.add_column("详情")
    # 获取运行中的 Agent 名称
    running_agents = ""
    if service_status == "[green]运行中[/green]":
        agent_names = []
        for ad in agents_dirs:
            if ad.exists():
                for d in ad.iterdir():
                    if d.is_dir() and (d / "agent.json").exists():
                        agent_names.append(d.name)
        running_agents = ", ".join(agent_names) if agent_names else "-"
    
    table.add_row("服务", service_status, service_detail)
    table.add_row("沙箱后端", sandbox_status, sandbox_detail)
    table.add_row("预热池", "[dim]0 / 5[/dim]", "-")
    table.add_row("活跃 Agent", active_agents, running_agents if running_agents else "-")
    table.add_row("活跃会话", sessions_detail, "-")

    console.print(table)

    # 渠道状态 - 读取真实配置
    channel_table = Table(title="渠道状态", show_header=True, header_style="cyan bold")
    channel_table.add_column("渠道")
    channel_table.add_column("状态")
    channel_table.add_column("配置")

    # 读取配置：用 loader 的 get_config()，确保与运行时一致（含 .env 覆盖与 accounts 结构）
    from smartclaw.config.loader import get_config

    feishu_status = "[yellow]未配置[/yellow]"
    feishu_config = "-"

    try:
        feishu = get_config().channels.feishu
        # 优先看多账号结构；兼容旧版顶层 app_id
        acc = feishu.get_default_account() if hasattr(feishu, "get_default_account") else None
        app_id = (acc.app_id if acc else "") or (getattr(feishu, "app_id", "") or "")
        if app_id:
            feishu_status = "[green]已配置[/green]"
            feishu_config = app_id[:15] + "..."
    except Exception:
        pass

    channel_table.add_row("飞书", feishu_status, feishu_config)
    channel_table.add_row("企业微信", "[yellow]未配置[/yellow]", "-")

    console.print(channel_table)


# ==================== doctor 命令 ====================


@app.command("doctor")
def doctor_command(
    skip_llm: bool = typer.Option(
        False,
        "--skip-llm",
        help="跳过 LLM 实际请求（不产生 Token 费用）",
    ),
    skip_http: bool = typer.Option(
        False,
        "--skip-http",
        help="跳过本机 HTTP /health 检查",
    ),
    tenant: str = typer.Option(
        "default",
        "--tenant",
        "-t",
        help="LLM 探活时的租户 ID（默认 default），合并 tenants.<id>.llm（与飞书一致）",
    ),
    agent: Optional[str] = typer.Option(
        None,
        "--agent",
        "-a",
        help="用于 LLM 探活的 Agent（短名 + --tenant；或直接使用 tenant/agent 全称）",
    ),
    deep: bool = typer.Option(
        False,
        "--deep",
        help="联网校验飞书 tenant_access_token（需已配置 app_id/app_secret）",
    ),
    llm_prompt: str = typer.Option(
        "请只回复一个字：好",
        "--llm-prompt",
        help="doctor 中 LLM 探活使用的短提示（控制成本）",
    ),
) -> None:
    """
    闭环诊断：本机环境、配置、HTTP 服务、LLM、（可选）飞书凭证。

    LLM 探活与 ``llm-test`` 一致：**租户 + config.toml [llm] + agent.json`` 三层合并，
    使用 ``--tenant``（默认 ``default``）与 ``--agent``（默认 ``default``）选择目标，
    亦支持 ``--agent tenant/name``。

    会对全局 [llm] 以及各 Agent 合并后的网关/模型做启发式自检（典型错配会直接标红）。
    默认会发起小额 LLM 请求，可用 ``--skip-llm`` 关闭。
    """
    import asyncio
    import json
    import platform
    import shutil
    from pathlib import Path

    from smartclaw import diagnostics as dx

    title("SmartClaw 闭环诊断")

    checks: list[tuple[str, str, str]] = []

    # Python 版本检查
    py_version = platform.python_version()
    py_ok = tuple(map(int, py_version.split("."))) >= (3, 11, 0)
    checks.append(
        (
            "环境: Python",
            "[green]OK[/green]" if py_ok else "[red]需要 3.12+[/red]",
            py_version,
        )
    )

    kvm_st, kvm_det = dx.check_kvm_environment()
    checks.append(("环境: KVM", kvm_st, kvm_det))

    fc_st, fc_det = dx.check_firecracker_binary()
    checks.append(("环境: Firecracker", fc_st, fc_det))

    docker_st, docker_det = dx.check_docker_daemon()
    checks.append(("环境: Docker", docker_st, docker_det))

    config_path = paths.get_config_file()
    checks.append(
        (
            "配置: 文件路径",
            "[green]OK[/green]" if config_path.exists() else "[yellow]未创建[/yellow]",
            str(config_path) if config_path.exists() else "运行 smartclaw init",
        )
    )

    cfg, cfg_loaded_from = dx.load_fresh_config()
    checks.append(
        (
            "配置: 解析",
            "[green]OK[/green]",
            str(cfg_loaded_from) if cfg_loaded_from else "默认内置（未找到 toml）",
        )
    )

    sb_name, sb_st, sb_det = dx.sandbox_backend_doctor_check(cfg)
    checks.append((sb_name, sb_st, sb_det))

    from smartclaw.config.loader import global_llm_config_as_merge_dict
    from smartclaw.llm.base import normalize_agent_llm_dict
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id

    glob_llm_flat = normalize_agent_llm_dict(global_llm_config_as_merge_dict(cfg.llm))
    g_war, g_err = dx.llm_endpoint_model_alignment_issues(
        str(glob_llm_flat.get("model_name", "")),
        str(glob_llm_flat.get("base_url", "")),
    )
    for gw in g_war:
        checks.append(("配置: 全局 [llm] 网关/模型", "[yellow]提示[/yellow]", gw))
    for ge_line in g_err:
        checks.append(("配置: 全局 [llm] 网关/模型", "[red]不一致[/red]", ge_line))

    scan_bulk: list[str] = []
    scan_has_error = False
    for qname, adata in dx.iter_agent_json_configs():
        raw_tid = str(adata.get("tenant_id") or "").strip()
        if raw_tid:
            rel_tid = normalize_tenant_id(raw_tid)
        elif "/" in qname:
            rel_tid = normalize_tenant_id(qname.split("/", 1)[0])
        else:
            rel_tid = normalize_tenant_id(DEFAULT_TENANT_ID)
        try:
            mbd = dx.merged_llm_blob_for_feishu_style(
                adata,
                cfg,
                tenant_id_for_merge=rel_tid,
            )
        except Exception as ex:
            scan_has_error = True
            scan_bulk.append(f"{qname}: LLM 合并/解密失败 — {ex}")
            continue
        w_scan, e_scan = dx.llm_endpoint_model_alignment_issues(
            str(mbd.get("model_name", "")),
            str(mbd.get("base_url", "")),
        )
        for line in e_scan:
            scan_has_error = True
            scan_bulk.append(f"{qname}: {line}")
        for line in w_scan[:1]:
            scan_bulk.append(f"{qname}（提示）: {line}")
    if scan_bulk:
        snippet = "; ".join(scan_bulk[:15])
        if len(scan_bulk) > 15:
            snippet += f" …(+{len(scan_bulk) - 15})"
        scan_st = "[red]不一致[/red]" if scan_has_error else "[yellow]提示[/yellow]"
        checks.append(("配置: 租户+Agent LLM 合并后自检", scan_st, snippet))

    host_bind = cfg.server.host
    port = cfg.server.port
    checks.append(
        (
            "配置: server",
            "[green]OK[/green]",
            f"host={host_bind} port={port}",
        )
    )

    feishu_st, feishu_det = dx.feishu_config_summary(cfg)
    checks.append(("渠道: 飞书", feishu_st, feishu_det))

    listening = dx.tcp_port_open("127.0.0.1", port)
    checks.append(
        (
            "服务: 端口监听",
            "[green]是[/green]" if listening else "[yellow]否[/yellow]",
            f"127.0.0.1:{port} {'可连接' if listening else '无进程监听（服务可能未启动）'}",
        )
    )

    run_dir = paths.get_run_dir()
    pid_file = run_dir / "smartclaw.pid"
    proc_detail = "无 PID 文件"
    proc_ok = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            proc_ok = dx.pid_is_running(pid)
            proc_detail = f"PID {pid} " + ("运行中" if proc_ok else "不存在或已退出")
        except Exception as e:
            proc_detail = f"PID 文件异常: {e}"
    checks.append(
        (
            "服务: 进程",
            "[green]OK[/green]" if proc_ok else "[yellow]未知[/yellow]" if pid_file.exists() else "[yellow]跳过[/yellow]",
            proc_detail,
        )
    )

    http_st = "[yellow]跳过[/yellow]"
    http_det = "已 --skip-http"
    if not skip_http:
        if listening:
            http_ok, http_msg = asyncio.run(dx.http_health_check(f"http://127.0.0.1:{port}"))
            http_st = "[green]OK[/green]" if http_ok else "[red]失败[/red]"
            http_det = http_msg if http_ok else http_msg
        else:
            http_st = "[yellow]跳过[/yellow]"
            http_det = "端口未监听，无法探测 /health"
    checks.append(("闭环: HTTP /health", http_st, http_det))

    from smartclaw.tenant import tenant_agent_key

    t_probe, a_probe = dx.resolve_llm_probe_target(agent, tenant_opt=tenant)
    probe_display = tenant_agent_key(a_probe, t_probe)

    agent_path = dx.discover_agent_json_path(a_probe, t_probe)

    llm_blob: dict = {}
    agent_data: dict = {}
    if not agent_path:
        checks.append(
            (
                "闭环: LLM Agent",
                "[red]失败[/red]",
                f"未找到 {probe_display} 的 agent.json（短名时请同时指定 --tenant，例如 --tenant dept_a -a bot_dept_a）",
            )
        )
    else:
        try:
            with open(agent_path, encoding="utf-8") as f:
                agent_data = json.load(f)
        except Exception as e:
            agent_data = {}
            checks.append(
                ("闭环: LLM Agent", "[red]失败[/red]", f"读取失败: {e}"),
            )

        if agent_path and agent_data:
            try:
                llm_blob = dx.merged_llm_blob_for_feishu_style(
                    agent_data,
                    cfg,
                    tenant_id_for_merge=t_probe,
                )
            except Exception as ex:
                llm_blob = {}
                checks.append(
                    (
                        "闭环: LLM 合并",
                        "[red]失败[/red]",
                        f"租户+全局+Agent 合并或解密 api_key 失败: {ex}",
                    )
                )
            else:
                pwarn, perr = dx.llm_endpoint_model_alignment_issues(
                    str(llm_blob.get("model_name", "")),
                    str(llm_blob.get("base_url", "")),
                )
                for pl in pwarn:
                    checks.append(("闭环: LLM 网关/模型", "[yellow]提示[/yellow]", pl))
                for pl in perr:
                    checks.append(("闭环: LLM 网关/模型", "[red]不一致[/red]", pl))

                checks.append(
                    (
                        "闭环: LLM 配置",
                        "[green]OK[/green]"
                        if llm_blob.get("api_key")
                        else "[red]缺少 api_key[/red]",
                        f"tenant={t_probe} agent={probe_display} model={llm_blob.get('model_name', '-')} "
                        "（合并: agent > tenants.<id>.llm > [llm]）",
                    ),
                )

    if skip_llm:
        checks.append(
            (
                "闭环: LLM 请求",
                "[yellow]跳过[/yellow]",
                "已 --skip-llm",
            )
        )
    elif not agent_path:
        checks.append(
            (
                "闭环: LLM 请求",
                "[red]跳过[/red]",
                "无 Agent 配置无法探活",
            )
        )
    elif not llm_blob.get("api_key"):
        checks.append(
            (
                "闭环: LLM 请求",
                "[red]失败[/red]",
                "缺少 llm.api_key（agent / 租户 / 全局 [llm] 合并后仍无密钥）",
            )
        )
    elif agent_path and agent_data and llm_blob.get("api_key"):
        lcfg = dx.llm_config_from_agent_llm_blob(llm_blob)
        ok, msg = asyncio.run(
            dx.probe_llm_chat(lcfg, llm_prompt, max_tokens=32, timeout=90.0)
        )
        checks.append(
            (
                "闭环: LLM 请求",
                "[green]OK[/green]" if ok else "[red]失败[/red]",
                msg,
            )
        )

    from smartclaw.agent.workspace import default_agent_workspace_base, resolve_agent_workspace_dir

    _agent_cfgs = dx.iter_agent_json_configs()
    _ws_root = default_agent_workspace_base(cfg)
    if _ws_root.exists():
        checks.append(
            (
                "Agent: workspace 根",
                "[green]OK[/green]",
                str(_ws_root),
            )
        )
    else:
        checks.append(
            (
                "Agent: workspace 根",
                "[red]失败[/red]",
                f"未发现agent工作目录（{_ws_root}）",
            )
        )
    if not _agent_cfgs:
        checks.append(
            (
                "Agent: 解析工作区",
                "[yellow]跳过[/yellow]",
                "未发现 agent.json",
            )
        )
    else:
        for aname, adata in _agent_cfgs:
            rw = resolve_agent_workspace_dir(aname, adata, cfg)
            det = str(rw)
            ovr = (adata.get("workspace") or "").strip()
            if ovr:
                det += f"  (agent.json workspace={ovr!r})"
            checks.append(
                (
                    f"Agent:{aname} 解析工作区",
                    "[green]OK[/green]",
                    det,
                )
            )

    if deep:
        ch = cfg.channels.feishu
        acc = ch.get_default_account()
        if acc and str(acc.app_id).strip() and str(acc.app_secret).strip():
            ft_ok, ft_msg = asyncio.run(
                dx.probe_feishu_tenant_token(str(acc.app_id), str(acc.app_secret))
            )
            checks.append(
                (
                    "闭环: 飞书 token",
                    "[green]OK[/green]" if ft_ok else "[red]失败[/red]",
                    ft_msg,
                )
            )
        else:
            checks.append(
                (
                    "闭环: 飞书 token",
                    "[yellow]跳过[/yellow]",
                    "未配置完整 app_id/app_secret",
                )
            )

    print_table("诊断结果", [list(c) for c in checks], ["检查项", "状态", "详情"])

    ok_count = sum(1 for c in checks if "[green]" in c[1])
    total = len(checks)
    hard_fail = any("[red]" in c[1] for c in checks)
    if hard_fail:
        warning(f"存在失败项: 绿色 {ok_count}/{total}，请查看上表 [red]失败[/red] 行")
        raise typer.Exit(1)
    if ok_count == total:
        success(f"闭环检查通过: {ok_count}/{total}")
    else:
        warning(f"部分为警告/跳过: 绿色 {ok_count}/{total}")


@app.command("llm-test")
def llm_test_command(
    message: str = typer.Argument(
        ...,
        help="发给模型的用户消息（将产生少量 Token）",
    ),
    tenant: str = typer.Option(
        "default",
        "--tenant",
        "-t",
        help="飞书租户 ID（默认 default）；与飞书链路一致，合并 tenants.<id>.llm",
    ),
    agent: Optional[str] = typer.Option(
        None,
        "--agent",
        "-a",
        help="Agent 名称：短名需配合 --tenant；或直接使用 tenant/agent 全称（如 dept_a/bot_dept_a）；默认 default",
    ),
    max_tokens: int = typer.Option(
        256,
        "--max-tokens",
        help="生成上限（控制费用）",
    ),
) -> None:
    """对 Agent 合并后的 LLM 配置发起一次请求（租户 + 全局 + agent.json，与 WS 链路一致）。"""
    import asyncio
    import json

    from smartclaw import diagnostics as dx
    from smartclaw.tenant import tenant_agent_key

    title("SmartClaw LLM 探活")

    t_probe, a_probe = dx.resolve_llm_probe_target(agent, tenant_opt=tenant)
    probe_display = tenant_agent_key(a_probe, t_probe)

    agent_path = dx.discover_agent_json_path(a_probe, t_probe)
    if not agent_path:
        error(
            f"未找到 {probe_display} 的 agent.json；"
            "短名时请指定 --tenant（例：--tenant dept_a -a bot_dept_a），"
            "或直接使用 -a dept_a/bot_dept_a"
        )
        raise typer.Exit(1)

    cfg, cfg_loaded_from = dx.load_fresh_config()
    if cfg_loaded_from:
        info(f"配置来源: [dim]{cfg_loaded_from}[/dim]")

    with open(agent_path, encoding="utf-8") as f:
        agent_data = json.load(f)

    llm_blob = dx.merged_llm_blob_for_feishu_style(
        agent_data,
        cfg,
        tenant_id_for_merge=t_probe,
    )

    lw, le = dx.llm_endpoint_model_alignment_issues(
        str(llm_blob.get("model_name", "")),
        str(llm_blob.get("base_url", "")),
    )
    for line in lw:
        warning(line)
    if le:
        for line in le:
            error(line)
        raise typer.Exit(1)

    if not llm_blob.get("api_key"):
        error(f"{probe_display} 合并后无可用 llm.api_key（请在 agent / 租户 / config.toml [llm] 配置）")
        raise typer.Exit(1)

    lcfg = dx.llm_config_from_agent_llm_blob(llm_blob)
    info(
        f"使用 [cyan]{probe_display}[/cyan] tenant={t_probe} "
        f"model={lcfg.model_name} provider={lcfg.provider.value} "
        f"base_url={lcfg.base_url or '(默认)'} "
        "(合并: agent > tenants.<id>.llm > [llm])"
    )

    ok, msg = asyncio.run(dx.probe_llm_chat(lcfg, message, max_tokens=max_tokens))
    if ok:
        success("模型响应:")
        print_panel(msg, title_str="回复", style="green")
    else:
        error(msg)
        raise typer.Exit(1)


# ==================== config 子命令 ====================


def _mask_secret_for_display(value: str, head: int = 4, tail: int = 4) -> str:
    t = (value or "").strip()
    if not t:
        return ""
    if len(t) <= head + tail + 3:
        return "***"
    return f"{t[:head]}...{t[-tail:]}"


def _redact_config_doc_secrets(doc: dict[str, Any]) -> dict[str, Any]:
    import copy

    out = copy.deepcopy(doc)
    ls = out.get("langsmith")
    if isinstance(ls, dict):
        ak = ls.get("api_key")
        if isinstance(ak, str) and ak.strip():
            ls["api_key"] = _mask_secret_for_display(ak)
    return out


def _langsmith_optional_label(value: str, *, default_hint: str = "使用 LangChain / LangSmith 默认") -> str:
    """空配置项的统一展示文案（config 与进程环境一致）。"""
    if (value or "").strip():
        return value.strip()
    return f"未设置（{default_hint}）"


def _langsmith_doc_has_api_key(doc: dict[str, Any]) -> bool:
    ls = doc.get("langsmith")
    if not isinstance(ls, dict):
        return False
    return bool(str(ls.get("api_key") or "").strip())


def _langsmith_process_has_api_key() -> bool:
    import os

    return bool((os.environ.get("LANGCHAIN_API_KEY") or "").strip())


def _langsmith_ready_for_tracing(doc: dict[str, Any] | None = None) -> bool:
    """配置文件或当前进程是否具备可用的 LangSmith API Key。"""
    if doc is not None and _langsmith_doc_has_api_key(doc):
        return True
    return _langsmith_process_has_api_key()


@config_app.command("show")
def config_show(
    key: Optional[str] = typer.Argument(None, help="配置键名，如 llm.model"),
) -> None:
    """
    显示当前生效配置（含 .env 环境变量覆盖后的结果）
    """
    from smartclaw.config.loader import ConfigLoader

    title("配置信息")

    # 通过 ConfigLoader 加载配置，确保 .env 覆盖生效
    loader = ConfigLoader()
    cfg = loader.load()

    # 查找实际使用的 TOML 文件路径（用于参考）
    config_path = loader._find_config_file()

    # 序列化为可显示的 dict
    display: dict[str, Any] = {
        "llm": {
            "provider": cfg.llm.provider,
            "model": cfg.llm.model_name,
            "base_url": cfg.llm.base_url,
            "api_key": _mask_secret_for_display(cfg.llm.api_key) if cfg.llm.api_key else "",
            "max_tokens": cfg.llm.max_tokens,
            "temperature": cfg.llm.temperature,
        },
        "vision": {
            "enabled": cfg.vision.enabled,
            "model": cfg.vision.model,
            "base_url": cfg.vision.base_url,
            "api_key": _mask_secret_for_display(cfg.vision.api_key) if cfg.vision.api_key else "",
            "timeout": cfg.vision.timeout,
            "max_retries": cfg.vision.max_retries,
        },
        "channels": {
            "feishu": {
                "enabled": cfg.channels.feishu.enabled,
                "default": cfg.channels.feishu.default,
                "accounts": {
                    name: {
                        "app_id": acc.app_id,
                        "app_secret": _mask_secret_for_display(acc.app_secret) if acc.app_secret else "",
                        "name": acc.name,
                        "enabled": acc.enabled,
                    }
                    for name, acc in cfg.channels.feishu.accounts.items()
                },
            },
            "wecom": {
                "corp_id": cfg.channels.wecom.corp_id,
                "agent_id": cfg.channels.wecom.agent_id,
                "secret": _mask_secret_for_display(cfg.channels.wecom.secret) if cfg.channels.wecom.secret else "",
            },
        },
        "sandbox": {
            "enabled": cfg.sandbox.enabled,
            "memory_mb": cfg.sandbox.memory_mb,
            "cpu_count": cfg.sandbox.cpu_count,
        },
        "server": {
            "host": cfg.server.host,
            "port": cfg.server.port,
            "workers": cfg.server.workers,
        },
        "logging": {
            "level": cfg.logging.level,
            "file": cfg.logging.file_path,
        },
        "auth": {
            "tenant_default": cfg.auth.tenant_default,
            "tenant_by_app_id": cfg.auth.tenant_by_app_id,
            "tenant_trust_header": cfg.auth.tenant_trust_header,
            "monitoring_require_auth": cfg.auth.monitoring_require_auth,
            "monitoring_bearer_token": _mask_secret_for_display(cfg.auth.monitoring_bearer_token) if cfg.auth.monitoring_bearer_token else "",
            "admin_require_auth": cfg.auth.admin_require_auth,
            "admin_bearer_token": _mask_secret_for_display(cfg.auth.admin_bearer_token) if cfg.auth.admin_bearer_token else "",
            "feishu_webhook_secret": _mask_secret_for_display(cfg.auth.feishu_webhook_secret) if cfg.auth.feishu_webhook_secret else "",
            "feishu_decrypt_webhook": cfg.auth.feishu_decrypt_webhook,
            "audit_jsonl_enabled": cfg.auth.audit_jsonl_enabled,
            "webhook_replay_ttl_seconds": cfg.auth.webhook_replay_ttl_seconds,
        },
    }

    # 聚合 Agent 信息（与 agent list 同源：data/agents/*/agent.json）
    import json as _json
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

    agents_summary: dict[str, dict[str, Any]] = {}
    for agents_dir in paths.get_agents_dirs():
        if not agents_dir.exists():
            continue
        for agent_json_path in (
            list(agents_dir.glob("*/agent.json")) + list(agents_dir.glob("*/*/agent.json"))
        ):
            try:
                agent_data = _json.loads(agent_json_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(agent_data, dict):
                continue
            name = agent_data.get("name", agent_json_path.parent.name)
            raw_tid = str(agent_data.get("tenant_id") or "").strip()
            if raw_tid:
                tenant_id = normalize_tenant_id(raw_tid)
            else:
                rel_parts = agent_json_path.relative_to(agents_dir).parts
                tenant_id = normalize_tenant_id(rel_parts[0]) if len(rel_parts) == 3 else DEFAULT_TENANT_ID
            qname = tenant_agent_key(name, tenant_id)
            agents_summary[qname] = {
                "name": name,
                "tenant": tenant_id,
                "enabled": agent_data.get("enabled", True),
                "channel": agent_data.get("channel", "-"),
                "model": (agent_data.get("llm") or {}).get("model_name", "(继承全局)"),
                "sandbox_enabled": (agent_data.get("sandbox") or {}).get("enabled", False),
            }

    if agents_summary:
        display["agents"] = {
            "count": len(agents_summary),
            "list": agents_summary,
        }

    if key:
        # 显示指定键（支持点号路径，如 llm.model / agents / agents.list.<agent_name>）
        keys = key.split(".")
        value: Any = display
        try:
            for k in keys:
                value = value[k]
            if isinstance(value, (dict, list)):
                console.print_json(data=value)
            else:
                console.print(f"[cyan]{key}[/cyan] = [green]{value}[/green]")
        except (KeyError, TypeError):
            error(f"配置键不存在: {key}")
            raise typer.Exit(1)
    else:
        config_json = _json.dumps(display, indent=2, ensure_ascii=False)
        source_note = (
            f"TOML: {config_path}" if config_path and config_path.exists()
            else "使用默认配置"
        )
        print_panel(
            config_json,
            title_str=f"当前生效配置（{source_note}）",
            style="cyan",
        )


@config_langsmith_app.command("status")
def config_langsmith_status() -> None:
    """查看 [langsmith] 与当前进程中的 LANGCHAIN_*（密钥脱敏）。"""
    import os

    from smartclaw.config.loader import get_config

    title("LangSmith / LangChain 追踪")
    ls = get_config().langsmith
    env_trace_raw = (os.environ.get("LANGCHAIN_TRACING_V2") or "").strip()
    env_key = (os.environ.get("LANGCHAIN_API_KEY") or "").strip()
    env_proj_raw = (os.environ.get("LANGCHAIN_PROJECT") or "").strip()
    env_ep_raw = (os.environ.get("LANGCHAIN_ENDPOINT") or "").strip()

    cfg_key_disp = (
        _mask_secret_for_display(ls.api_key) if ls.api_key.strip() else "未设置"
    )
    env_key_disp = _mask_secret_for_display(env_key) if env_key else "未设置"
    cfg_proj_disp = _langsmith_optional_label(ls.project, default_hint="默认项目名由 LangChain 决定")
    env_proj_disp = _langsmith_optional_label(env_proj_raw, default_hint="默认项目名由 LangChain 决定")
    cfg_ep_disp = _langsmith_optional_label(ls.endpoint)
    env_ep_disp = _langsmith_optional_label(env_ep_raw)
    env_trace_disp = env_trace_raw if env_trace_raw else "未设置"

    tbl = Table(show_header=False)
    tbl.add_row("config [langsmith].enabled", str(ls.enabled))
    tbl.add_row("config [langsmith].api_key", cfg_key_disp)
    tbl.add_row("config [langsmith].project", cfg_proj_disp)
    tbl.add_row("config [langsmith].endpoint", cfg_ep_disp)
    tbl.add_row(
        "进程 LANGCHAIN_TRACING_V2",
        env_trace_disp if env_trace_disp != "未设置" else "未设置（未开启进程内追踪）",
    )
    tbl.add_row("进程 LANGCHAIN_API_KEY", env_key_disp)
    tbl.add_row("进程 LANGCHAIN_PROJECT", env_proj_disp)
    tbl.add_row("进程 LANGCHAIN_ENDPOINT", env_ep_disp)
    console.print(tbl)

    if ls.enabled and not _langsmith_ready_for_tracing():
        warning(
            "已启用 [langsmith] 但未配置 api_key，且当前进程无 LANGCHAIN_API_KEY；"
            "请运行: smartclaw config langsmith set-api-key"
        )
    elif ls.enabled and cfg_key_disp != "未设置" and env_key_disp == "未设置":
        warning(
            "配置文件已有 api_key，但当前进程未注入 LANGCHAIN_API_KEY；"
            "请在本 shell 重新执行 set-api-key / enable，或启动新终端后再运行 agent。"
        )
    elif ls.project.strip() and env_proj_raw and ls.project.strip() != env_proj_raw:
        warning(
            "进程 LANGCHAIN_PROJECT 与 config 中 project 不一致（环境变量优先，不会被 config 覆盖）。"
        )
    elif ls.enabled and _langsmith_ready_for_tracing():
        success("追踪配置就绪：config 与当前进程均已具备上报条件。")

    info(
        "说明：LangSmith 接收 LLM 链路追踪；本地控制台详细日志由 "
        "SMARTCLAW_DEEPAGENTS_DEBUG 单独控制，二者互不替代。"
    )


@config_langsmith_app.command("enable")
def config_langsmith_enable() -> None:
    """启用 [langsmith].enabled（需已配置 api_key 或环境中已有 LANGCHAIN_API_KEY）。"""
    from smartclaw.config.loader import reload_config

    config_path, doc = _cli_load_config_toml_or_exit()
    doc.setdefault("langsmith", {})
    ls = doc["langsmith"]
    was_enabled = bool(ls.get("enabled"))
    ls["enabled"] = True
    _cli_save_config_toml(config_path, doc)
    reload_config()
    if was_enabled:
        info("[langsmith] 本来就是启用状态。")
    else:
        success("已启用 [langsmith]。")
    if _langsmith_ready_for_tracing(doc):
        info("API Key 已就绪；可运行: smartclaw config langsmith status")
    else:
        warning(
            "尚未配置 API Key（config 与当前进程均无 LANGCHAIN_API_KEY）；"
            "请运行: smartclaw config langsmith set-api-key"
        )


@config_langsmith_app.command("disable")
def config_langsmith_disable() -> None:
    """关闭 [langsmith].enabled（不删除已保存的 api_key）。"""
    from smartclaw.config.loader import reload_config

    config_path, doc = _cli_load_config_toml_or_exit()
    doc.setdefault("langsmith", {})
    doc["langsmith"]["enabled"] = False
    _cli_save_config_toml(config_path, doc)
    reload_config()
    success("已禁用配置文件中的 LangSmith 追踪。")
    warning("当前进程若已设置 LANGCHAIN_* 环境变量，仍会继续上报，直到重启进程或清除环境变量。")


@config_langsmith_app.command("set-api-key")
def config_langsmith_set_api_key(
    api_key: Optional[str] = typer.Argument(
        None,
        help="LangSmith API Key；省略则交互输入（不回显）",
    ),
) -> None:
    """写入 config.toml [langsmith].api_key 并启用追踪。"""
    import getpass

    from smartclaw.config.loader import reload_config

    config_path, doc = _cli_load_config_toml_or_exit()
    doc.setdefault("langsmith", {})
    ls = doc["langsmith"]
    key = (api_key or "").strip()
    if not key:
        key = getpass.getpass("LangSmith API Key (LANGCHAIN_API_KEY): ").strip()
    if not key:
        error("未提供 API Key")
        raise typer.Exit(1)
    ls["api_key"] = key
    ls["enabled"] = True
    _cli_save_config_toml(config_path, doc)
    reload_config()
    success("已保存 API Key 并启用 [langsmith]。")
    info(
        "已尝试将 LANGCHAIN_* 写入当前进程（仅补齐空的环境变量，不覆盖已有 export）。"
        "可运行: smartclaw config langsmith status 核对。"
    )


@config_langsmith_app.command("set-project")
def config_langsmith_set_project(
    project: str = typer.Argument(..., help="LANGCHAIN_PROJECT 名称"),
) -> None:
    from smartclaw.config.loader import reload_config

    config_path, doc = _cli_load_config_toml_or_exit()
    doc.setdefault("langsmith", {})
    proj = project.strip()
    doc["langsmith"]["project"] = proj
    _cli_save_config_toml(config_path, doc)
    reload_config()
    success(f"已设置 project = {proj}")
    import os

    env_proj = (os.environ.get("LANGCHAIN_PROJECT") or "").strip()
    if env_proj and env_proj != proj:
        warning(
            f"当前进程 LANGCHAIN_PROJECT 仍为 {env_proj!r}（已有环境变量，config 不会覆盖）。"
            " 若需生效请 unset LANGCHAIN_PROJECT 后重新 enable，或在新终端启动服务。"
        )
    elif not env_proj:
        info("当前进程将在下次 reload / 新终端中采用该项目名。")


@config_langsmith_app.command("set-endpoint")
def config_langsmith_set_endpoint(
    endpoint: str = typer.Argument(
        ...,
        help="例如 https://api.smith.langchain.com 或欧盟区 endpoint",
    ),
) -> None:
    from smartclaw.config.loader import reload_config

    config_path, doc = _cli_load_config_toml_or_exit()
    doc.setdefault("langsmith", {})
    doc["langsmith"]["endpoint"] = endpoint.strip()
    _cli_save_config_toml(config_path, doc)
    reload_config()
    success("已设置 endpoint。")


@config_langsmith_app.command("clear-api-key")
def config_langsmith_clear_api_key(
    yes: bool = typer.Option(False, "--yes", "-y", help="确认从配置文件删除 api_key"),
) -> None:
    """从 config.toml 删除 [langsmith].api_key（并关闭 enabled）。"""
    from smartclaw.config.loader import reload_config

    if not yes:
        if not typer.confirm("确定从配置文件删除 LangSmith API Key 并关闭 [langsmith]？"):
            raise typer.Abort()
    config_path, doc = _cli_load_config_toml_or_exit()
    doc.setdefault("langsmith", {})
    doc["langsmith"]["api_key"] = ""
    doc["langsmith"]["enabled"] = False
    _cli_save_config_toml(config_path, doc)
    reload_config()
    success("已清除配置文件中的 api_key 并禁用 [langsmith]。")
    warning("运行中的进程若已通过环境变量设置 LANGCHAIN_API_KEY，请自行重启或 unset 相关变量。")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="配置键名，如 server.port"),
    value: str = typer.Argument(..., help="配置值"),
) -> None:
    """
    设置配置项

    示例: smartclaw config set server.port 8080
    """
    from pathlib import Path

    config_path = paths.get_config_file()

    if not config_path.exists():
        error("配置文件不存在，请先运行 'smartclaw init'")
        raise typer.Exit(1)

    import tomllib as tomli
    import tomli_w

    with open(config_path, "rb") as f:
        config = tomli.load(f)

    # 解析键路径并设置值
    keys = key.split(".")
    current = config
    for k in keys[:-1]:
        if k not in current:
            current[k] = {}
        current = current[k]

    # 尝试转换值类型
    parsed_value: Any = value
    try:
        if value.lower() == "true":
            parsed_value = True
        elif value.lower() == "false":
            parsed_value = False
        elif value.isdigit():
            parsed_value = int(value)
        elif value.replace(".", "").isdigit():
            parsed_value = float(value)
        else:
            parsed_value = value
    except Exception:
        parsed_value = value

    current[keys[-1]] = parsed_value

    with open(config_path, "wb") as f:
        tomli_w.dump(config, f)

    success(f"已设置: {key} = {parsed_value}")


@config_app.command("edit")
def config_edit() -> None:
    """
    使用编辑器编辑配置文件

    使用 EDITOR 环境变量指定的编辑器，默认使用 nano。
    """
    import os
    import subprocess
    from pathlib import Path

    config_path = paths.get_config_file()

    if not config_path.exists():
        error("配置文件不存在，请先运行 'smartclaw init'")
        raise typer.Exit(1)

    editor = os.getenv("EDITOR", "nano")

    try:
        subprocess.run([editor, str(config_path)], check=True)
        success("配置已更新")
    except subprocess.CalledProcessError:
        error("编辑器退出异常")
        raise typer.Exit(1)
    except FileNotFoundError:
        error(f"编辑器不存在: {editor}")
        raise typer.Exit(1)


def _cli_normalize_inline_allowlist_raw(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if isinstance(raw, str):
        return [
            ln.strip()
            for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
    return []


def _cli_ensure_execution_table(doc: dict[str, Any]) -> dict[str, Any]:
    ex = doc.setdefault("execution", {})
    if not isinstance(ex, dict):
        doc["execution"] = {}
        ex = doc["execution"]
    return ex


def _cli_load_config_toml_or_exit() -> tuple[Path, dict[str, Any]]:
    import tomllib as tomli

    config_path = paths.get_config_file()
    if not config_path.exists():
        error("配置文件不存在，请先运行 'smartclaw init'")
        raise typer.Exit(1)
    with open(config_path, "rb") as f:
        return config_path, tomli.load(f)


def _cli_save_config_toml(config_path: Path, doc: dict[str, Any]) -> None:
    import tomli_w

    with open(config_path, "wb") as f:
        tomli_w.dump(doc, f)
    try:
        from smartclaw.config.loader import reload_config

        reload_config()
    except Exception:
        pass


def _cli_ensure_auth_table(doc: dict[str, Any]) -> dict[str, Any]:
    auth = doc.setdefault("auth", {})
    if not isinstance(auth, dict):
        doc["auth"] = {}
        auth = doc["auth"]
    return auth


def _cli_parse_roles(raw: str) -> list[str]:
    roles = [item.strip() for item in (raw or "").replace(";", ",").split(",") if item.strip()]
    return list(dict.fromkeys(roles))


def _cli_auth_roles_table(auth: dict[str, Any], tenant: str) -> dict[str, list[str]]:
    tenants = auth.setdefault("feishu_open_id_roles_by_tenant", {})
    if not isinstance(tenants, dict):
        auth["feishu_open_id_roles_by_tenant"] = {}
        tenants = auth["feishu_open_id_roles_by_tenant"]
    table = tenants.setdefault(tenant, {})
    if not isinstance(table, dict):
        tenants[tenant] = {}
        table = tenants[tenant]
    return table


def _cli_auth_tool_roles_table(auth: dict[str, Any]) -> dict[str, list[str]]:
    table = auth.setdefault("tool_required_roles_any", {})
    if not isinstance(table, dict):
        auth["tool_required_roles_any"] = {}
        table = auth["tool_required_roles_any"]
    return table


def _cli_ensure_mcp_table(doc: dict[str, Any]) -> dict[str, Any]:
    mcp = doc.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        doc["mcp"] = {}
        mcp = doc["mcp"]
    mcp.setdefault("enabled", True)
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        mcp["servers"] = {}
    return mcp


def _cli_mcp_server_table(doc: dict[str, Any]) -> dict[str, Any]:
    mcp = _cli_ensure_mcp_table(doc)
    servers = mcp.setdefault("servers", {})
    if not isinstance(servers, dict):
        mcp["servers"] = {}
        servers = mcp["servers"]
    return servers


def _cli_mcp_registry_tool_name(server_name: str, raw_tool_name: str) -> str:
    def safe(value: str) -> str:
        out = "".join(c if c.isalnum() or c == "_" else "_" for c in (value or ""))
        return out.strip("_") or "tool"

    return f"{safe(server_name)}__{safe(raw_tool_name)}"


async def _cli_mcp_list_tool_names(server_cfg: dict[str, Any]) -> list[str]:
    try:
        from fastmcp import Client
    except ImportError as exc:
        raise RuntimeError("缺少 fastmcp 依赖，请先 uv pip install -e \".[mcp]\" 或 uv pip install fastmcp") from exc

    transport = str(server_cfg.get("transport") or "sse").strip().lower()
    if transport not in {"sse", "http"}:
        raise RuntimeError(f"暂不支持测试 transport={transport!r}（当前支持 sse/http）")
    url = str(server_cfg.get("url") or "").strip()
    if not url:
        raise RuntimeError("MCP server 缺少 url")
    async with Client(url) as client:
        tools = await client.list_tools()
    names: list[str] = []
    for tool in tools or []:
        raw = getattr(tool, "name", None)
        if raw is None and isinstance(tool, dict):
            raw = tool.get("name")
        if raw:
            names.append(str(raw))
    return names


def _cli_roles_for_user(auth: dict[str, Any], tenant: str, user: str) -> list[str]:
    table = _cli_auth_roles_table(auth, tenant)
    roles = table.get(user) or table.get("*") or ["default"]
    return list(roles) if isinstance(roles, list) else [str(roles)]


def _cli_collect_recent_auth_users(tenant: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Collect recent Feishu users from audit jsonl files and session files."""
    import json
    from pathlib import Path

    rows: dict[tuple[str, str], dict[str, Any]] = {}

    def add(row: dict[str, Any]) -> None:
        user = str(row.get("user_open_id") or row.get("user_id") or "").strip()
        ten = str(row.get("tenant_id") or "default").strip() or "default"
        if not user or (tenant and ten != tenant):
            return
        key = (ten, user)
        ts = str(row.get("ts") or row.get("updated_at") or "")
        old = rows.get(key)
        if old is None or ts >= str(old.get("last_seen") or ""):
            rows[key] = {
                "tenant_id": ten,
                "user_open_id": user,
                "last_seen": ts,
                "agent_id": row.get("agent_id") or row.get("agent_name") or "",
                "chat_id": row.get("chat_id") or "",
                "source": row.get("source") or "",
                "roles_seen": row.get("roles") or [],
            }

    audit_dir = Path.home() / ".smartclaw" / "audit"
    for name in ("feishu-inbound.jsonl", "tool-invoke.jsonl"):
        path = audit_dir / name
        if not path.is_file():
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        item = json.loads(line)
                    except Exception:
                        continue
                    item["source"] = name
                    add(item)
        except Exception:
            continue

    sessions_dir = paths.SESSION_DIR
    if sessions_dir.exists():
        for path in sessions_dir.rglob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            data["source"] = "session"
            data["ts"] = str(data.get("updated_at") or "")
            data["user_open_id"] = data.get("user_id")
            add(data)

    out = sorted(rows.values(), key=lambda r: str(r.get("last_seen") or ""), reverse=True)
    return out[: max(1, limit)]


@auth_roles_app.command("set")
def auth_roles_set(
    user: str = typer.Argument(..., help="飞书 open_id；可用 * 表示租户默认角色"),
    roles: str = typer.Option(..., "--roles", "-r", help="逗号分隔角色，如 tenant_admin,developer"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="租户 ID"),
) -> None:
    """设置某个飞书 open_id 在指定 tenant 下的角色列表。"""
    parsed = _cli_parse_roles(roles)
    if not parsed:
        error("roles 不能为空")
        raise typer.Exit(1)
    config_path, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    table = _cli_auth_roles_table(auth, tenant)
    table[user] = parsed
    _cli_save_config_toml(config_path, doc)
    success(f"已设置 tenant={tenant} user={user} roles={parsed}")


@auth_roles_app.command("grant")
def auth_roles_grant(
    user: str = typer.Argument(..., help="飞书 open_id；可用 * 表示租户默认角色"),
    role: str = typer.Argument(..., help="要授予的角色"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="租户 ID"),
) -> None:
    """给某个用户追加一个角色（去重）。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    table = _cli_auth_roles_table(auth, tenant)
    current = _cli_parse_roles(",".join(table.get(user, []) if isinstance(table.get(user), list) else []))
    if role not in current:
        current.append(role)
    table[user] = current
    _cli_save_config_toml(config_path, doc)
    success(f"已授予 tenant={tenant} user={user} role={role}")


@auth_roles_app.command("revoke")
def auth_roles_revoke(
    user: str = typer.Argument(..., help="飞书 open_id；可用 * 表示租户默认角色"),
    role: str = typer.Argument(..., help="要移除的角色"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="租户 ID"),
) -> None:
    """从某个用户移除一个角色。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    table = _cli_auth_roles_table(auth, tenant)
    current = table.get(user, [])
    if not isinstance(current, list):
        current = []
    table[user] = [r for r in current if r != role]
    _cli_save_config_toml(config_path, doc)
    success(f"已移除 tenant={tenant} user={user} role={role}")


@auth_roles_app.command("list")
def auth_roles_list(
    tenant: str = typer.Option("default", "--tenant", "-t", help="租户 ID"),
) -> None:
    """列出指定 tenant 下的用户角色映射。"""
    _, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    table = _cli_auth_roles_table(auth, tenant)
    tbl = Table(title=f"Auth Roles: tenant={tenant}", show_header=True, header_style="cyan bold")
    tbl.add_column("open_id")
    tbl.add_column("roles")
    for user, roles in sorted(table.items()):
        tbl.add_row(str(user), ", ".join(roles) if isinstance(roles, list) else str(roles))
    console.print(tbl)


@auth_app.command("whoami")
def auth_whoami(
    user: str = typer.Argument(..., help="飞书 open_id"),
    tenant: str = typer.Option("default", "--tenant", "-t", help="租户 ID"),
) -> None:
    """查看某个用户在 tenant 下解析出的角色。"""
    _, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    roles = _cli_roles_for_user(auth, tenant, user)
    console.print_json(data={"tenant": tenant, "user": user, "roles": roles})


@auth_users_app.command("recent")
def auth_users_recent(
    tenant: str = typer.Option("", "--tenant", "-t", help="租户 ID；不传则列出全部"),
    limit: int = typer.Option(20, "--limit", "-n", help="最多显示多少个用户"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """从审计日志和会话文件列出最近出现过的飞书用户 open_id。"""
    _, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    users = _cli_collect_recent_auth_users(tenant=tenant, limit=limit)
    for row in users:
        row["configured_roles"] = _cli_roles_for_user(
            auth,
            str(row.get("tenant_id") or "default"),
            str(row.get("user_open_id") or ""),
        )
    if json_output:
        console.print_json(data={"users": users, "count": len(users)})
        return
    tbl = Table(title="Recent Feishu Users", show_header=True, header_style="cyan bold")
    tbl.add_column("tenant")
    tbl.add_column("open_id")
    tbl.add_column("roles")
    tbl.add_column("last_seen")
    tbl.add_column("agent")
    tbl.add_column("source")
    for row in users:
        tbl.add_row(
            str(row.get("tenant_id") or ""),
            str(row.get("user_open_id") or ""),
            ", ".join(row.get("configured_roles") or []),
            str(row.get("last_seen") or ""),
            str(row.get("agent_id") or ""),
            str(row.get("source") or ""),
        )
    console.print(tbl)


@auth_app.command("current-user")
def auth_current_user(
    tenant: str = typer.Option("default", "--tenant", "-t", help="租户 ID"),
    user: str = typer.Option("", "--user", "-u", help="指定飞书 open_id；不传则取该 tenant 最近用户"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """
    查看当前/最近飞书用户的 open_id 与角色。

    CLI 没有实时飞书上下文；不传 --user 时，会从最近审计/会话记录中取该 tenant 最新用户。
    """
    _, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    resolved_user = (user or "").strip()
    recent: dict[str, Any] = {}
    if not resolved_user:
        users = _cli_collect_recent_auth_users(tenant=tenant, limit=1)
        if users:
            recent = users[0]
            resolved_user = str(recent.get("user_open_id") or "")
    if not resolved_user:
        error("未找到最近用户；请先让用户给机器人发送一条消息，或传入 --user ou_xxx")
        raise typer.Exit(1)
    roles = _cli_roles_for_user(auth, tenant, resolved_user)
    payload = {
        "tenant": tenant,
        "user_open_id": resolved_user,
        "roles": roles,
        "last_seen": recent.get("last_seen", ""),
        "source": recent.get("source", ""),
        "agent_id": recent.get("agent_id", ""),
        "note": "CLI 无实时飞书上下文；未传 --user 时显示最近用户。",
    }
    if json_output:
        console.print_json(data=payload)
    else:
        title("当前/最近飞书用户")
        console.print_json(data=payload)


@auth_tool_app.command("require")
def auth_tool_require(
    tool_name: str = typer.Argument(..., help="工具名，如 agent_create"),
    roles: str = typer.Option(..., "--roles", "-r", help="逗号分隔角色，如 tenant_admin,platform_admin"),
) -> None:
    """设置某个工具调用所需的任一角色。"""
    parsed = _cli_parse_roles(roles)
    if not parsed:
        error("roles 不能为空")
        raise typer.Exit(1)
    config_path, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    table = _cli_auth_tool_roles_table(auth)
    table[tool_name] = parsed
    _cli_save_config_toml(config_path, doc)
    success(f"已设置 tool={tool_name} required_roles_any={parsed}")


@auth_tool_app.command("clear")
def auth_tool_clear(
    tool_name: str = typer.Argument(..., help="工具名"),
) -> None:
    """删除某个工具的显式角色要求（仍可能受默认高风险角色限制）。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    table = _cli_auth_tool_roles_table(auth)
    table.pop(tool_name, None)
    _cli_save_config_toml(config_path, doc)
    success(f"已删除 tool={tool_name} 的显式角色要求")


@auth_tool_app.command("list")
def auth_tool_list() -> None:
    """列出工具角色要求（显式配置 + 默认高风险角色）。"""
    _, doc = _cli_load_config_toml_or_exit()
    auth = _cli_ensure_auth_table(doc)
    explicit = _cli_auth_tool_roles_table(auth)
    from smartclaw.auth.tool_gate import DEFAULT_HIGH_RISK_TOOL_ROLES

    names = sorted(set(DEFAULT_HIGH_RISK_TOOL_ROLES) | set(explicit))
    tbl = Table(title="Tool Required Roles", show_header=True, header_style="cyan bold")
    tbl.add_column("tool")
    tbl.add_column("roles")
    tbl.add_column("source")
    for name in names:
        if name in explicit:
            roles = explicit[name]
            source = "config.toml"
        else:
            roles = DEFAULT_HIGH_RISK_TOOL_ROLES[name]
            source = "default"
        tbl.add_row(name, ", ".join(roles) if isinstance(roles, list) else str(roles), source)
    console.print(tbl)


@mcp_app.command("add")
def mcp_add(
    name: str = typer.Argument(..., help="MCP Server 注册名，如 factory"),
    url: str = typer.Option(..., "--url", "-u", help="MCP Server SSE/HTTP URL"),
    server_name: str = typer.Option("", "--name", help="工具命名前缀；空则使用注册名"),
    transport: str = typer.Option("sse", "--transport", help="传输类型：sse/http"),
    timeout_ms: int = typer.Option(30000, "--timeout-ms", help="工具调用超时毫秒"),
    risk_level: str = typer.Option("low", "--risk-level", help="默认风险等级"),
    tenant_scope: str = typer.Option("tenant", "--tenant-scope", help="默认租户作用域"),
    requires_confirmation: bool = typer.Option(False, "--requires-confirmation/--no-requires-confirmation", help="是否默认二次确认"),
    context_argument: str = typer.Option("", "--context-argument", help="可选：注入 SmartClaw 上下文的参数名"),
    enabled: bool = typer.Option(True, "--enabled/--disabled", help="是否启用"),
) -> None:
    """注册一个 MCP Server 到 config.toml。"""
    key = name.strip()
    if not key:
        error("name 不能为空")
        raise typer.Exit(1)
    config_path, doc = _cli_load_config_toml_or_exit()
    servers = _cli_mcp_server_table(doc)
    servers[key] = {
        "name": (server_name or key).strip(),
        "transport": transport.strip().lower(),
        "url": url.strip(),
        "enabled": bool(enabled),
        "timeout_ms": int(timeout_ms),
        "risk_level": risk_level.strip() or "low",
        "tenant_scope": tenant_scope.strip() or "tenant",
        "requires_confirmation": bool(requires_confirmation),
    }
    if context_argument.strip():
        servers[key]["context_argument"] = context_argument.strip()
    _cli_save_config_toml(config_path, doc)
    success(f"已注册 MCP Server: {key} -> {url.strip()}")


@mcp_app.command("list")
def mcp_list(
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """列出 config.toml 中注册的 MCP Server。"""
    _, doc = _cli_load_config_toml_or_exit()
    mcp = _cli_ensure_mcp_table(doc)
    servers = _cli_mcp_server_table(doc)
    rows = []
    for key, cfg in sorted(servers.items()):
        if not isinstance(cfg, dict):
            cfg = {}
        rows.append({
            "key": key,
            "name": cfg.get("name") or key,
            "transport": cfg.get("transport") or "sse",
            "url": cfg.get("url") or "",
            "enabled": cfg.get("enabled", True),
            "risk_level": cfg.get("risk_level") or "low",
            "requires_confirmation": cfg.get("requires_confirmation", False),
        })
    if json_output:
        console.print_json(data={"enabled": bool(mcp.get("enabled", False)), "servers": rows})
        return
    tbl = Table(title=f"MCP Servers (enabled={bool(mcp.get('enabled', False))})", show_header=True, header_style="cyan bold")
    tbl.add_column("key")
    tbl.add_column("name")
    tbl.add_column("transport")
    tbl.add_column("enabled")
    tbl.add_column("risk")
    tbl.add_column("url")
    for row in rows:
        tbl.add_row(
            str(row["key"]),
            str(row["name"]),
            str(row["transport"]),
            str(row["enabled"]),
            str(row["risk_level"]),
            str(row["url"]),
        )
    console.print(tbl)


@mcp_app.command("remove")
def mcp_remove(
    name: str = typer.Argument(..., help="MCP Server 注册名"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认删除"),
) -> None:
    """从 config.toml 删除一个 MCP Server 注册。"""
    if not yes:
        error("请加 --yes 确认删除")
        raise typer.Exit(1)
    config_path, doc = _cli_load_config_toml_or_exit()
    servers = _cli_mcp_server_table(doc)
    if name not in servers:
        error(f"MCP Server 不存在: {name}")
        raise typer.Exit(1)
    servers.pop(name, None)
    _cli_save_config_toml(config_path, doc)
    success(f"已删除 MCP Server: {name}")


@mcp_app.command("test")
def mcp_test(
    name: str = typer.Argument(..., help="MCP Server 注册名"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """连接 MCP Server 并列出远端 tools。"""
    import asyncio

    _, doc = _cli_load_config_toml_or_exit()
    servers = _cli_mcp_server_table(doc)
    server_cfg = servers.get(name)
    if not isinstance(server_cfg, dict):
        error(f"MCP Server 不存在: {name}")
        raise typer.Exit(1)
    try:
        raw_tools = asyncio.run(_cli_mcp_list_tool_names(server_cfg))
    except Exception as exc:
        error(f"MCP Server 测试失败: {exc}")
        raise typer.Exit(1)
    server_name = str(server_cfg.get("name") or name)
    registry_tools = [_cli_mcp_registry_tool_name(server_name, raw) for raw in raw_tools]
    payload = {"server": name, "raw_tools": raw_tools, "registry_tools": registry_tools}
    if json_output:
        console.print_json(data=payload)
        return
    title(f"MCP Server 测试成功: {name}")
    for raw, reg in zip(raw_tools, registry_tools):
        info(f"{raw} -> {reg}")


@mcp_app.command("on")
def mcp_global_on() -> None:
    """开启全局 MCP 总闸：config.toml [mcp].enabled = true（仅影响是否加载远端 MCP 工具）。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    mcp = _cli_ensure_mcp_table(doc)
    mcp["enabled"] = True
    _cli_save_config_toml(config_path, doc)
    success("已开启全局 MCP 总闸 [mcp].enabled = true")


@mcp_app.command("off")
def mcp_global_off() -> None:
    """关闭全局 MCP 总闸：config.toml [mcp].enabled = false。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    mcp = _cli_ensure_mcp_table(doc)
    mcp["enabled"] = False
    _cli_save_config_toml(config_path, doc)
    success("已关闭全局 MCP 总闸 [mcp].enabled = false")


def _cli_filter_allowed_remove_mcp_tools(
    ac: dict[str, Any], remove_names: set[str]
) -> bool:
    """从 agent 配置的 allowed / allowed_tools 中移除指定工具名。返回是否发生过修改。"""
    changed = False
    tools_cfg = ac.get("tools")
    if isinstance(tools_cfg, dict):
        for key in ("allowed", "allowed_tools"):
            cur = tools_cfg.get(key)
            if not isinstance(cur, list):
                continue
            new_list = [str(x) for x in cur if str(x) not in remove_names]
            if len(new_list) != len(cur):
                tools_cfg[key] = new_list
                changed = True
    top = ac.get("allowed_tools")
    if isinstance(top, list):
        new_top = [str(x) for x in top if str(x) not in remove_names]
        if len(new_top) != len(top):
            ac["allowed_tools"] = new_top
            changed = True
    return changed


def _cli_merge_allowlist_ordered(existing: list[str], incoming: list[str]) -> list[str]:
    """保序合并：incoming 中未出现过的条目追加到 existing 后。"""
    seen = set(existing)
    out = list(existing)
    for x in incoming:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _cli_load_json_allowlist_payload(payload: str) -> list[str]:
    """
    解析 JSON 数组字符串；若以 @ 开头则视为 UTF-8 文件路径，读取文件内容再解析。
    元素转为 strip 后的 str，空串与 # 开头项丢弃。
    """
    import json

    raw = (payload or "").strip()
    if not raw:
        error("参数为空：请传入 JSON 数组或 @路径")
        raise typer.Exit(1)
    if raw.startswith("@"):
        p = Path(os.path.expanduser(raw[1:].strip()))
        if not p.is_file():
            error(f"文件不存在: {p}")
            raise typer.Exit(1)
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as e:
            error(f"读取失败: {e}")
            raise typer.Exit(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        error(f"JSON 解析失败: {e}")
        raise typer.Exit(1)
    if not isinstance(data, list):
        error('JSON 顶层必须是数组，例如 ["ls","git"]')
        raise typer.Exit(1)
    out: list[str] = []
    for item in data:
        s = str(item).strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


@config_shell_allowlist_app.command("list")
def config_shell_allowlist_list() -> None:
    """列出全局 [execution].shell_allowlist 与 shell_allowlist_path。"""
    _, doc = _cli_load_config_toml_or_exit()
    ex = doc.get("execution") or {}
    if not isinstance(ex, dict):
        ex = {}
    items = _cli_normalize_inline_allowlist_raw(ex.get("shell_allowlist"))
    path_v = str(ex.get("shell_allowlist_path") or "").strip()
    title("全局 exec 白名单（config.toml [execution]）")
    info(f"shell_allowlist_path: {path_v or '（空）'}")
    info(f"shell_allowlist 条数: {len(items)}")
    for i, p in enumerate(items, 1):
        info(f"  {i}. {p}")


@config_shell_allowlist_app.command("add")
def config_shell_allowlist_add(
    pattern: str = typer.Argument(..., help="前缀或首词，与文档 shell_allowlist 规则一致"),
) -> None:
    """在全局 shell_allowlist 末尾追加一条（去重）。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    ex = _cli_ensure_execution_table(doc)
    cur = _cli_normalize_inline_allowlist_raw(ex.get("shell_allowlist"))
    p = pattern.strip()
    if not p or p.startswith("#"):
        error("无效规则：空或以 # 开头")
        raise typer.Exit(1)
    if p in cur:
        warning(f"已存在，跳过: {p}")
        return
    cur.append(p)
    ex["shell_allowlist"] = cur
    _cli_save_config_toml(config_path, doc)
    success(f"已追加全局规则: {p}")


@config_shell_allowlist_app.command("remove")
def config_shell_allowlist_remove(
    pattern: str = typer.Argument(..., help="与列表项完全匹配（strip 后）则删除"),
) -> None:
    """从全局 shell_allowlist 删除匹配项（可删多条相同文本）。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    ex = _cli_ensure_execution_table(doc)
    cur = _cli_normalize_inline_allowlist_raw(ex.get("shell_allowlist"))
    p = pattern.strip()
    new_list = [x for x in cur if x != p]
    removed = len(cur) - len(new_list)
    if removed == 0:
        warning(f"未找到与 {p!r} 完全一致的项")
        raise typer.Exit(1)
    ex["shell_allowlist"] = new_list
    _cli_save_config_toml(config_path, doc)
    success(f"已删除 {removed} 条: {p!r}")


@config_shell_allowlist_app.command("clear")
def config_shell_allowlist_clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="确认清空内联列表（不影响 path 文件）"),
) -> None:
    """清空全局 [execution].shell_allowlist 内联数组（不修改 shell_allowlist_path）。"""
    if not yes:
        error("请加 --yes 确认清空内联 shell_allowlist")
        raise typer.Exit(1)
    config_path, doc = _cli_load_config_toml_or_exit()
    ex = _cli_ensure_execution_table(doc)
    ex["shell_allowlist"] = []
    _cli_save_config_toml(config_path, doc)
    success("已清空全局 shell_allowlist 内联列表")


@config_shell_allowlist_app.command("path-show")
def config_shell_allowlist_path_show() -> None:
    """显示 shell_allowlist_path。"""
    _, doc = _cli_load_config_toml_or_exit()
    ex = doc.get("execution") or {}
    if not isinstance(ex, dict):
        ex = {}
    path_v = str(ex.get("shell_allowlist_path") or "").strip()
    console.print(path_v or "（空）")


@config_shell_allowlist_app.command("path-set")
def config_shell_allowlist_path_set(
    path: str = typer.Argument(
        ...,
        help="外挂白名单文件路径（可含 ~）",
    ),
) -> None:
    """设置 shell_allowlist_path。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    ex = _cli_ensure_execution_table(doc)
    raw = path.strip()
    if not raw or raw == "-":
        error("请使用子命令 path-clear 清空路径，或提供有效路径")
        raise typer.Exit(1)
    ex["shell_allowlist_path"] = raw
    _cli_save_config_toml(config_path, doc)
    success(f"已设置 shell_allowlist_path = {raw}")


@config_shell_allowlist_app.command("path-clear")
def config_shell_allowlist_path_clear() -> None:
    """清空 shell_allowlist_path。"""
    config_path, doc = _cli_load_config_toml_or_exit()
    ex = _cli_ensure_execution_table(doc)
    ex["shell_allowlist_path"] = ""
    _cli_save_config_toml(config_path, doc)
    success("已清空 shell_allowlist_path")


@config_shell_allowlist_app.command("import-json")
def config_shell_allowlist_import_json(
    json_payload: str = typer.Argument(
        ...,
        help='JSON 数组，如 ["ls","git"]；或以 @C:\\path\\bins.json 从文件读取',
    ),
    merge: bool = typer.Option(
        False,
        "--merge",
        "-m",
        help="与当前内联 shell_allowlist 保序合并去重；默认替换内联列表",
    ),
) -> None:
    """从 JSON 数组批量写入 [execution].shell_allowlist（OpenClaw safeBins 风格）。"""
    incoming = _cli_load_json_allowlist_payload(json_payload)
    if not incoming:
        error("解析后无任何有效规则")
        raise typer.Exit(1)
    config_path, doc = _cli_load_config_toml_or_exit()
    ex = _cli_ensure_execution_table(doc)
    cur = _cli_normalize_inline_allowlist_raw(ex.get("shell_allowlist"))
    if merge:
        ex["shell_allowlist"] = _cli_merge_allowlist_ordered(cur, incoming)
    else:
        ex["shell_allowlist"] = incoming
    _cli_save_config_toml(config_path, doc)
    if merge:
        success(
            f"已合并：原有 {len(cur)} 条 + 导入 {len(incoming)} 条 → "
            f"共 {len(ex['shell_allowlist'])} 条"
        )
    else:
        success(f"已替换内联 shell_allowlist，共 {len(incoming)} 条")


def _agent_read_shell_list(ac: dict[str, Any]) -> list[str]:
    aj = ac.get("shell_allowlist")
    return _cli_normalize_inline_allowlist_raw(aj)


@agent_shell_allowlist_app.command("list")
def agent_shell_allowlist_list(
    name: str = typer.Argument(..., help="Agent 名称（agent.json name）"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
) -> None:
    """列出某 Agent agent.json 中的 shell_allowlist 与 include-workspace 开关。"""
    from smartclaw.agent.manager import AgentManager

    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)
    items = _agent_read_shell_list(ac)
    inc = ac.get("shell_allowlist_include_workspace_file", True)
    title(f"Agent {name} — shell 白名单（agent.json）")
    info(f"shell_allowlist_include_workspace_file: {bool(inc)}")
    info(f"shell_allowlist 条数: {len(items)}")
    for i, p in enumerate(items, 1):
        info(f"  {i}. {p}")


@agent_shell_allowlist_app.command("add")
def agent_shell_allowlist_add(
    name: str = typer.Argument(..., help="Agent 名称"),
    pattern: str = typer.Argument(..., help="前缀或首词"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
) -> None:
    """在 agent.json shell_allowlist 追加一条（去重；统一写为 JSON 数组）。"""
    from smartclaw.agent.manager import AgentManager

    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)
    p = pattern.strip()
    if not p or p.startswith("#"):
        error("无效规则：空或以 # 开头")
        raise typer.Exit(1)
    cur = _agent_read_shell_list(ac)
    if p in cur:
        warning(f"已存在，跳过: {p}")
        return
    cur.append(p)
    ac["shell_allowlist"] = cur
    if not manager._write_config(ac.get("name", name), ac, tenant_id=ac.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)
    success(f"Agent {name} 已追加: {p}")


@agent_shell_allowlist_app.command("remove")
def agent_shell_allowlist_remove(
    name: str = typer.Argument(..., help="Agent 名称"),
    pattern: str = typer.Argument(..., help="与列表项 strip 后完全一致则删除"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
) -> None:
    """从 agent.json shell_allowlist 删除匹配项。"""
    from smartclaw.agent.manager import AgentManager

    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)
    cur = _agent_read_shell_list(ac)
    p = pattern.strip()
    new_list = [x for x in cur if x != p]
    removed = len(cur) - len(new_list)
    if removed == 0:
        warning(f"未找到与 {p!r} 完全一致的项")
        raise typer.Exit(1)
    ac["shell_allowlist"] = new_list
    if not manager._write_config(ac.get("name", name), ac, tenant_id=ac.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)
    success(f"Agent {name} 已删除 {removed} 条: {p!r}")


@agent_shell_allowlist_app.command("clear")
def agent_shell_allowlist_clear(
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认清空"),
) -> None:
    """清空 agent.json 中的 shell_allowlist（写为 []）。"""
    if not yes:
        error("请加 --yes 确认清空")
        raise typer.Exit(1)
    from smartclaw.agent.manager import AgentManager

    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)
    ac["shell_allowlist"] = []
    if not manager._write_config(ac.get("name", name), ac, tenant_id=ac.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)
    success(f"Agent {name} 已清空 shell_allowlist")


@agent_shell_allowlist_app.command("include-workspace")
def agent_shell_allowlist_include_workspace(
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    enable: bool = typer.Option(
        True,
        "--on/--off",
        help="是否读取工作区 tools/SHELL_ALLOWLIST.txt",
    ),
) -> None:
    """设置 shell_allowlist_include_workspace_file（默认 --on）。"""
    from smartclaw.agent.manager import AgentManager

    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)
    ac["shell_allowlist_include_workspace_file"] = enable
    if not manager._write_config(ac.get("name", name), ac, tenant_id=ac.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)
    success(
        f"Agent {name} shell_allowlist_include_workspace_file = {enable}"
    )


@agent_shell_allowlist_app.command("import-json")
def agent_shell_allowlist_import_json(
    name: str = typer.Argument(..., help="Agent 名称"),
    json_payload: str = typer.Argument(
        ...,
        help='JSON 数组；或 @路径 从文件读取',
    ),
    merge: bool = typer.Option(
        False,
        "--merge",
        "-m",
        help="与当前 agent.json shell_allowlist 保序合并；默认整表替换",
    ),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
) -> None:
    """批量写入 agent.json 的 shell_allowlist（JSON 数组）。"""
    from smartclaw.agent.manager import AgentManager

    incoming = _cli_load_json_allowlist_payload(json_payload)
    if not incoming:
        error("解析后无任何有效规则")
        raise typer.Exit(1)
    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)
    cur = _agent_read_shell_list(ac)
    if merge:
        ac["shell_allowlist"] = _cli_merge_allowlist_ordered(cur, incoming)
    else:
        ac["shell_allowlist"] = incoming
    if not manager._write_config(ac.get("name", name), ac, tenant_id=ac.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)
    if merge:
        success(
            f"Agent {name} 已合并：原 {len(cur)} 条 → 共 {len(ac['shell_allowlist'])} 条"
        )
    else:
        success(f"Agent {name} 已写入 shell_allowlist，共 {len(incoming)} 条")


@agent_mcp_app.command("list")
def agent_mcp_list(
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    json_output: bool = typer.Option(False, "--json", help="输出 JSON"),
) -> None:
    """查看某 Agent 启用的 MCP Server 与 allowed MCP tools。"""
    from smartclaw.agent.manager import AgentManager

    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)
    mcp_cfg = ac.get("mcp") if isinstance(ac.get("mcp"), dict) else {}
    servers = mcp_cfg.get("servers") or []
    tools_cfg = ac.get("tools") if isinstance(ac.get("tools"), dict) else {}
    allowed = tools_cfg.get("allowed") or tools_cfg.get("allowed_tools") or ac.get("allowed_tools") or []
    mcp_allowed = [str(x) for x in allowed if "__" in str(x)]
    payload = {
        "agent": ac.get("name", name),
        "tenant": ac.get("tenant_id", tenant or "default"),
        "mcp_servers": servers,
        "mcp_allowed_tools": mcp_allowed,
        "enforce_allowed_tools": bool(tools_cfg.get("enforce_allowed_tools") or ac.get("enforce_allowed_tools", False)),
    }
    if json_output:
        console.print_json(data=payload)
        return
    title(f"Agent {name} MCP")
    console.print_json(data=payload)


@agent_mcp_app.command("enable")
def agent_mcp_enable(
    name: str = typer.Argument(..., help="Agent 名称"),
    server: str = typer.Argument(..., help="已注册的 MCP Server 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    add_tools: bool = typer.Option(True, "--add-tools/--no-add-tools", help="连接 MCP 并把发现到的工具加入 allowed_tools"),
    enforce_allowed_tools: bool = typer.Option(True, "--enforce-allowed-tools/--no-enforce-allowed-tools", help="是否开启 Agent allowed_tools 强制模式"),
) -> None:
    """为某 Agent 启用 MCP Server，并可自动加入 allowed_tools。"""
    import asyncio

    from smartclaw.agent.manager import AgentManager

    _, doc = _cli_load_config_toml_or_exit()
    server_cfg = _cli_mcp_server_table(doc).get(server)
    if not isinstance(server_cfg, dict):
        error(f"MCP Server 未注册: {server}；请先运行 smartclaw mcp add")
        raise typer.Exit(1)

    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)

    mcp_cfg = ac.setdefault("mcp", {})
    if not isinstance(mcp_cfg, dict):
        ac["mcp"] = {}
        mcp_cfg = ac["mcp"]
    servers = mcp_cfg.setdefault("servers", [])
    if not isinstance(servers, list):
        servers = [str(servers)] if servers else []
        mcp_cfg["servers"] = servers
    if server not in servers:
        servers.append(server)

    added_tools: list[str] = []
    if add_tools:
        try:
            raw_tools = asyncio.run(_cli_mcp_list_tool_names(server_cfg))
            server_name = str(server_cfg.get("name") or server)
            added_tools = [_cli_mcp_registry_tool_name(server_name, raw) for raw in raw_tools]
        except Exception as exc:
            warning(f"MCP 工具发现失败，仅启用 server，不修改 allowed_tools: {exc}")

    tools_cfg = ac.setdefault("tools", {})
    if not isinstance(tools_cfg, dict):
        ac["tools"] = {}
        tools_cfg = ac["tools"]
    if enforce_allowed_tools:
        tools_cfg["enforce_allowed_tools"] = True
    if added_tools:
        allowed = tools_cfg.setdefault("allowed", [])
        if not isinstance(allowed, list):
            allowed = [str(allowed)] if allowed else []
            tools_cfg["allowed"] = allowed
        for tool_name in added_tools:
            if tool_name not in allowed:
                allowed.append(tool_name)

    if not manager._write_config(ac.get("name", name), ac, tenant_id=ac.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)
    success(
        f"Agent {name} 已启用 MCP Server {server}"
        + (f"，并加入 {len(added_tools)} 个 allowed_tools" if added_tools else "")
    )


@agent_mcp_app.command("disable")
def agent_mcp_disable(
    name: str = typer.Argument(..., help="Agent 名称"),
    server: str = typer.Argument(..., help="要从该 Agent 移除的 MCP Server 注册名"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    remove_tools: bool = typer.Option(
        True,
        "--remove-tools/--no-remove-tools",
        help="若为 true：从 config.toml 解析该 server，并在 Agent 曾启用它时从 tools.allowed / allowed_tools 移除对应前缀工具名",
    ),
) -> None:
    """从 agent.json 移除某 MCP Server 引用；可选同步从 allowed 列表剔除该 Server 映射的工具名。"""
    import asyncio

    from smartclaw.agent.manager import AgentManager

    _, doc = _cli_load_config_toml_or_exit()
    servers_table = _cli_mcp_server_table(doc)
    server_cfg = servers_table.get(server)

    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)

    mcp_cfg = ac.get("mcp") if isinstance(ac.get("mcp"), dict) else {}
    srv_list_raw = mcp_cfg.get("servers")
    srv_list: list[str] = []
    if isinstance(srv_list_raw, list):
        srv_list = [str(x).strip() for x in srv_list_raw if str(x).strip()]
    elif isinstance(srv_list_raw, str) and srv_list_raw.strip():
        srv_list = [s.strip() for s in srv_list_raw.split(",") if s.strip()]

    originally_had = server in srv_list
    changed_servers = False
    if originally_had:
        new_srv = [s for s in srv_list if s != server]
        merged_mcp = dict(mcp_cfg)
        merged_mcp["servers"] = new_srv
        ac["mcp"] = merged_mcp
        changed_servers = True

    changed_allowed = False
    removed_tool_names: set[str] = set()
    if remove_tools and originally_had:
        if not isinstance(server_cfg, dict):
            warning(
                f"MCP Server 未在 config.toml 登记: {server}，无法推导 registry 工具名，"
                "allowed 列表未自动清理（可手动编辑 agent.json）。"
            )
        else:
            try:
                raw_tools = asyncio.run(_cli_mcp_list_tool_names(server_cfg))
                server_name = str(server_cfg.get("name") or server)
                removed_tool_names = {
                    _cli_mcp_registry_tool_name(server_name, raw) for raw in raw_tools
                }
                if removed_tool_names:
                    changed_allowed = _cli_filter_allowed_remove_mcp_tools(ac, removed_tool_names)
                    info(
                        "已从允许列表移除: "
                        + ", ".join(sorted(removed_tool_names))
                    )
            except Exception as exc:
                warning(f"连接 MCP 拉取工具列表失败，allowed 未自动清理: {exc}")

    if not changed_servers and not changed_allowed:
        warning(f"未发现变更：Agent 未启用 MCP {server!r}（或未清理到任何 allowed 项）")
        return

    if not manager._write_config(ac.get("name", name), ac, tenant_id=ac.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)

    msg_parts: list[str] = []
    if changed_servers:
        msg_parts.append(f"已从 mcp.servers 移除 {server!r}")
    if changed_allowed:
        msg_parts.append("已同步清理 allowed / allowed_tools 中的 MCP 工具名")
    success("；".join(msg_parts))


# ==================== agent 子命令 ====================


@agent_app.command("list")
def agent_list(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="显示详细信息"),
    show_secrets: bool = typer.Option(False, "--show-secrets", help="显示完整 AppSecret (慎用)"),
) -> None:
    """
    列出所有 Agent 及其配置状态
    
    示例:
        smartclaw agent list              # 简洁列表
        smartclaw agent list -v           # 详细列表
        smartclaw agent list --show-secrets  # 显示完整密钥（慎用）
    """
    import json
    import re as re_module
    from pathlib import Path
    from rich.table import Table
    from rich.panel import Panel

    title("Agent 列表")

    from smartclaw.agent.workspace import resolve_agent_workspace_dir
    from smartclaw.config.loader import get_config
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

    _cfg = get_config()

    # 使用统一的路径查找函数
    agents_dirs = paths.get_agents_dirs()

    agents = []
    seen_names = set()  # 避免重复
    agents_detail = {}  # 存储详细信息用于 verbose 模式
    
    for agents_dir in agents_dirs:
        if not agents_dir.exists():
            continue
        config_files = list(agents_dir.glob("*/agent.json")) + list(
            agents_dir.glob("*/*/agent.json")
        )
        for config_file in config_files:
            rel_parts = config_file.relative_to(agents_dir).parts
            path_tenant = DEFAULT_TENANT_ID if len(rel_parts) == 2 else normalize_tenant_id(rel_parts[0])
            agent_dir_name = rel_parts[-2]
            if config_file.exists():
                try:
                    with open(config_file, encoding="utf-8") as f:
                        config = json.load(f)
                except:
                    continue
                name = config.get("name", agent_dir_name)
                tenant_id = normalize_tenant_id(config.get("tenant_id", path_tenant))
                qname = tenant_agent_key(name, tenant_id)
                if qname in seen_names:
                    continue
                seen_names.add(qname)
                
                # 获取模型信息
                llm_cfg = config.get("llm", {})
                model_name = llm_cfg.get("model_name", "-")
                
                # 获取 display_name
                display = config.get("display_name", name)
                
                # 获取飞书配置：agent.json 为空时回退全局 default 账号（与 loader/运行时一致）
                feishu_cfg = config.get("feishu", {})
                _gf = getattr(getattr(_cfg, "channels", None), "feishu", None)
                _g_acc = _gf.get_default_account() if _gf is not None and hasattr(_gf, "get_default_account") else None
                app_id = feishu_cfg.get("app_id", "") or (getattr(_g_acc, "app_id", "") if _g_acc else "")
                app_secret = feishu_cfg.get("app_secret", "") or (getattr(_g_acc, "app_secret", "") if _g_acc else "")
                
                # 掩码处理
                app_id_display = app_id[:10] + "..." if len(app_id) > 10 else (app_id or "❌ 未配置")
                if app_secret:
                    app_secret_display = app_secret[:6] + "***" + app_secret[-4:] if len(app_secret) > 12 else "***"
                else:
                    app_secret_display = "❌ 未配置"
                
                # 验证状态
                app_id_valid = bool(re_module.match(r'^cli_[a-zA-Z0-9]+$', app_id)) if app_id else False
                
                app_id_status = "[green]✅[/green]" if app_id_valid else "[red]❌[/red]"
                
                # 获取沙箱状态
                sandbox_enabled = config.get("sandbox", {}).get("enabled", False)
                sandbox_type = config.get("sandbox", {}).get("type", "docker")
                sandbox_str = f"[green]✅ {sandbox_type}[/green]" if sandbox_enabled else "[dim]❌ 禁用[/dim]"
                
                # 判断 Agent 是否正在运行
                import os
                is_running = False
                run_dir = paths.get_run_dir()
                if run_dir.exists():
                    pid_file = run_dir / "smartclaw.pid"
                    if pid_file.exists():
                        try:
                            pid = int(pid_file.read_text().strip())
                            if os.path.exists(f"/proc/{pid}"):
                                is_running = True
                        except:
                            pass
                
                # 状态：运行中 > 配置完整(✅已配置) > 缺失字段(❌missing …)。
                # 字段完整性按「运行时生效值」判断：agent.json 的 llm 块为空的字段会
                # 经 merge_agent_llm_with_global 回退到全局 [llm]（与 Runner 实际行为一致），
                # 因此依赖全局 api_key 的 Agent 不会误报 missing。
                from smartclaw.llm.base import merge_agent_llm_with_global

                _gllm = _cfg.llm
                _global_llm_dict = {
                    "model_name": getattr(_gllm, "model_name", ""),
                    "base_url": getattr(_gllm, "base_url", ""),
                    "api_key": getattr(_gllm, "api_key", ""),
                    "provider": getattr(_gllm, "provider", ""),
                }
                _eff_llm = merge_agent_llm_with_global(llm_cfg, _global_llm_dict)
                # 显示用「运行时生效模型」：agent.json llm 为空时回退全局 [llm]
                _eff_model = str(_eff_llm.get("model_name") or "").strip()
                if _eff_model:
                    model_name = _eff_model
                missing_fields = [
                    label
                    for field, label in (
                        ("model_name", "model_name"),
                        ("base_url", "base_url"),
                        ("api_key", "api_key"),
                    )
                    if not str(_eff_llm.get(field) or "").strip()
                ]

                if is_running:
                    status_str = "[green]🟢 运行中[/green]"
                elif missing_fields:
                    status_str = "[red]❌ missing " + ", ".join(missing_fields) + "[/red]"
                else:
                    status_str = "[green]✅ 已配置[/green]"

                resolved_workspace = str(resolve_agent_workspace_dir(name, config, _cfg, tenant_id=tenant_id))
                
                # 基本模式
                if not verbose:
                    agents.append([
                        qname,
                        display,
                        model_name,
                        app_id_status,
                        resolved_workspace,
                        status_str,
                    ])
                else:
                    # 详细模式
                    agents_detail[qname] = {
                        "name": qname,
                        "tenant_id": tenant_id,
                        "display": display,
                        "description": config.get("description", "-"),
                        "model_name": model_name,
                        "app_id": app_id,
                        "app_id_display": app_id_display,
                        "app_id_valid": app_id_valid,
                        "app_secret": app_secret if show_secrets else app_secret_display,
                        "app_secret_valid": len(app_secret) >= 16 if app_secret else False,
                        "enabled": config.get("enabled", True),
                        "sandbox_enabled": sandbox_enabled,
                        "sandbox_type": sandbox_type,
                        "is_running": is_running,
                        "llm_missing": missing_fields,
                        "config_path": str(config_file),
                        "resolved_workspace": resolved_workspace,
                        "workspace_raw": (config.get("workspace") or "").strip(),
                    }
    
    if not verbose:
        # 简洁模式
        if agents:
            print_table("Agent 列表", agents, ["名称", "飞书名", "模型", "AppID", "解析工作区", "状态"])
        else:
            info("暂无 Agent")
    else:
        # 详细模式
        if not agents_detail:
            info("暂无 Agent")
            return
        
        for name, detail in agents_detail.items():
            if detail["is_running"]:
                status_icon = "[green]🟢[/green]"
            elif detail.get("llm_missing"):
                status_icon = "[red]❌[/red]"
            else:
                status_icon = "[green]✅[/green]"

            # 构建详细面板内容
            content_lines = [
                f"[bold]描述:[/bold] {detail['description']}",
                f"[bold]租户:[/bold] {detail['tenant_id']}",
                f"[bold]模型:[/bold] {detail['model_name']}",
                f"[bold]LLM 配置:[/bold] "
                + (
                    "[green]✅ 完整[/green]"
                    if not detail.get("llm_missing")
                    else f"[red]❌ missing {', '.join(detail['llm_missing'])}[/red]"
                ),
                f"",
                f"[bold cyan]飞书配置:[/bold cyan]",
                f"  AppID:     {detail['app_id_display']} {('[green]✅[/green]' if detail['app_id_valid'] else '[red]❌[/red]')}",
                f"  AppSecret: {detail['app_secret']} {('[green]✅[/green]' if detail['app_secret_valid'] else '[red]❌[/red]')}",
                f"",
                f"[bold magenta]沙箱:[/bold magenta]",
                f"  {'✅ 启用' if detail['sandbox_enabled'] else '❌ 禁用'} ({detail['sandbox_type']})",
                f"",
                f"[bold]解析工作区:[/bold] {detail['resolved_workspace']}",
                f"[dim]agent.json workspace:[/dim] {detail['workspace_raw'] or '(默认布局)'}",
                f"",
                f"[bold]配置文件:[/bold] {detail['config_path']}",
            ]
            
            panel_content = "\n".join(content_lines)
            panel = Panel(
                panel_content,
                title=f"{status_icon} {name} (显示名: {detail['display']})",
                style="cyan",
            )
            console.print(panel)
            console.print()




@agent_app.command("validate")
def agent_validate(
    agent_name: str = typer.Argument(None, help="指定 Agent 名称（不指定则验证所有）"),
    fix: bool = typer.Option(False, "--fix", "-f", help="自动修复可修复的问题"),
) -> None:
    """
    验证 Agent 配置的完整性和正确性
    
    验证规则:
    - AppID 格式必须为 cli_ 开头
    - AppSecret 长度 >= 16
    - Agent 名称只能包含字母、数字、下划线
    - Display Name 不能为空
    - LLM API Key 不能为空
    - 配置文件必须存在且可读
    
    示例:
        smartclaw agent validate              # 验证所有 Agent
        smartclaw agent validate coder_heima  # 验证指定 Agent
        smartclaw agent validate --fix        # 验证并自动修复
    """
    import json
    import re as re_module
    from pathlib import Path
    from typing import NamedTuple

    class ValidationResult(NamedTuple):
        agent_name: str
        field: str
        status: str  # "✅", "❌", "⚠️"
        message: str

    title("Agent 配置验证")

    # 使用统一的路径查找函数
    agents_dirs = paths.get_agents_dirs()

    all_results: list[ValidationResult] = []
    agents_found = set()

    for agents_dir in agents_dirs:
        if not agents_dir.exists():
            continue
        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
                
            agent_name_str = agent_dir.name
            
            # 如果指定了名称，跳过不匹配的
            if agent_name and agent_name_str != agent_name:
                continue
            
            config_file = agent_dir / "agent.json"
            
            # 跳过没有 agent.json 的目录（如 memory、data 等系统目录）
            if not config_file.exists():
                continue
            
            # 读取配置
            try:
                with open(config_file, encoding="utf-8") as f:
                    config = json.load(f)
            except json.JSONDecodeError as e:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "配置文件",
                    "❌",
                    f"JSON 解析失败: {e}"
                ))
                continue
            except Exception as e:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "配置文件",
                    "❌",
                    f"读取失败: {e}"
                ))
                continue
            
            agents_found.add(agent_name_str)
            
            # 1. 验证 Agent 名称
            name_in_config = config.get("name", "")
            if not re_module.match(r'^[a-zA-Z0-9_]{2,32}$', name_in_config):
                all_results.append(ValidationResult(
                    agent_name_str,
                    "Agent 名称",
                    "❌",
                    f"无效的名称: '{name_in_config}' (应为 2-32 位字母、数字、下划线)"
                ))
            else:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "Agent 名称",
                    "✅",
                    f"有效: {name_in_config}"
                ))
            
            # 2. 验证 Display Name
            display_name = config.get("display_name", "")
            if not display_name or len(display_name.strip()) == 0:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "Display Name",
                    "⚠️",
                    "未设置，将使用 Agent 名称"
                ))
            else:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "Display Name",
                    "✅",
                    f"有效: {display_name}"
                ))
            
            # 3. 验证飞书 AppID
            feishu_cfg = config.get("feishu", {})
            app_id = feishu_cfg.get("app_id", "")
            if not app_id:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "AppID",
                    "❌",
                    "未配置飞书 AppID"
                ))
            elif not re_module.match(r'^cli_[a-zA-Z0-9]+$', app_id):
                all_results.append(ValidationResult(
                    agent_name_str,
                    "AppID",
                    "❌",
                    f"格式错误: {app_id} (应以 cli_ 开头)"
                ))
            else:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "AppID",
                    "✅",
                    f"格式正确: {app_id[:15]}..."
                ))
            
            # 4. 验证飞书 AppSecret
            app_secret = feishu_cfg.get("app_secret", "")
            if not app_secret:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "AppSecret",
                    "❌",
                    "未配置飞书 AppSecret"
                ))
            elif len(app_secret) < 16:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "AppSecret",
                    "❌",
                    f"长度不足: {len(app_secret)} < 16"
                ))
            else:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "AppSecret",
                    "✅",
                    f"长度: {len(app_secret)} 位"
                ))
            
            # 5. 验证 LLM API Key（合并 config.toml [llm]）
            from smartclaw.agent.manager import AgentManager
            from smartclaw.config.loader import get_config, global_llm_config_as_merge_dict
            from smartclaw.llm.base import merge_agent_llm_with_global, normalize_agent_llm_dict

            llm_raw = dict(config.get("llm", {}))
            ak = llm_raw.get("api_key", "")
            if ak and str(ak).startswith("ENC:"):
                llm_raw["api_key"] = AgentManager()._decrypt(str(ak)[4:])
            llm_cfg = normalize_agent_llm_dict(
                merge_agent_llm_with_global(
                    llm_raw, global_llm_config_as_merge_dict(get_config().llm)
                )
            )
            api_key = llm_cfg.get("api_key", "")
            if not api_key:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "LLM API Key",
                    "❌",
                    "未配置 LLM API Key"
                ))
            elif len(api_key) < 10:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "LLM API Key",
                    "⚠️",
                    f"长度可疑: {len(api_key)} < 10"
                ))
            else:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "LLM API Key",
                    "✅",
                    f"已配置 ({len(api_key)} 位)"
                ))
            
            # 6. 验证 LLM 模型
            model_name = llm_cfg.get("model_name", "")
            if not model_name:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "LLM 模型",
                    "⚠️",
                    "未指定模型"
                ))
            else:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "LLM 模型",
                    "✅",
                    f"使用: {model_name}"
                ))
            
            # 7. 验证 enabled 状态
            enabled = config.get("enabled", True)
            if not enabled:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "启用状态",
                    "⚠️",
                    "Agent 已禁用"
                ))
            else:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "启用状态",
                    "✅",
                    "正常启用"
                ))
            
            # 8. 验证沙箱配置
            sandbox_cfg = config.get("sandbox", {})
            sandbox_enabled = sandbox_cfg.get("enabled", False)
            sandbox_type = sandbox_cfg.get("type", "docker")
            valid_types = ["docker", "firecracker", "process"]
            if sandbox_enabled and sandbox_type not in valid_types:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "沙箱类型",
                    "⚠️",
                    f"未知类型: {sandbox_type}"
                ))
            else:
                all_results.append(ValidationResult(
                    agent_name_str,
                    "沙箱配置",
                    "✅",
                    f"{'启用' if sandbox_enabled else '禁用'} ({sandbox_type})"
                ))

    # 如果指定了名称但没找到
    if agent_name and agent_name not in agents_found:
        error(f"Agent 不存在: {agent_name}")
        # 搜索相似名称
        console.print("\n[dim]可用 Agent:[/dim]")
        for ad in agents_dirs:
            if ad.exists():
                for d in ad.iterdir():
                    if d.is_dir():
                        console.print(f"  - {d.name}")
        raise typer.Exit(1)

    # 输出结果
    if not all_results:
        info("没有找到需要验证的 Agent")
        return

    # 按 Agent 分组显示
    from collections import defaultdict
    by_agent = defaultdict(list)
    for r in all_results:
        by_agent[r.agent_name].append(r)

    for agent_n, results in by_agent.items():
        console.print(f"\n[bold cyan]▸ {agent_n}[/bold cyan]")
        for r in results:
            status_color = "green" if r.status == "✅" else ("red" if r.status == "❌" else "yellow")
            console.print(f"  {r.status} [{status_color}]{r.field}[/{status_color}]: {r.message}")

    # 汇总统计
    total = len(all_results)
    passed = sum(1 for r in all_results if r.status == "✅")
    warnings = sum(1 for r in all_results if r.status == "⚠️")
    failed = sum(1 for r in all_results if r.status == "❌")

    console.print()
    summary = f"总计: [green]✅ {passed}[/green] | [yellow]⚠️ {warnings}[/yellow] | [red]❌ {failed}[/red]"
    console.print(summary)

    if failed > 0:
        console.print("\n[red]❌ 验证失败，请修复上述错误后再启动服务[/red]")
        raise typer.Exit(1)
    elif warnings > 0:
        console.print("\n[yellow]⚠️ 验证通过但有警告，建议检查[/yellow]")
    else:
        console.print("\n[green]✅ 所有检查通过！[/green]")



# ==================== Agent CRUD 命令 ====================


@agent_app.command("add")
def agent_add(
    name: str = typer.Argument(..., help="Agent 名称（字母、数字、下划线）"),
    tenant: str = typer.Option("default", "--tenant", help="租户 ID；默认 default"),
    channel: str = typer.Option("feishu", "--channel", help="渠道类型: feishu / wecom（默认 feishu）"),
    display_name: str = typer.Option(
        None,
        "--display-name",
        "-d",
        help="机器人名称/群内 @ 文案；省略则按渠道自动生成（feishu: SmartClaw-<name>，wecom: <name>）",
    ),
    description: str = typer.Option("", "--description", help="Agent 描述"),
    app_id: str = typer.Option("", "--app-id", "-i", help="飞书 AppID (cli_xxx，仅 --channel feishu 必填)"),
    app_secret: str = typer.Option("", "--app-secret", "-s", help="飞书 AppSecret（仅 --channel feishu 必填）"),
    llm_provider: str = typer.Option(
        "",
        "--provider",
        "-p",
        help="LLM 厂商（与 agent set-llm 一致：qwen/deepseek/glm/bigmodel/zhipu/kimi/openai）；留空则按模型名自动选择网关",
    ),
    llm_model: Optional[str] = typer.Option(
        None,
        "--llm-model",
        "-m",
        help="LLM 模型；留空则回落到全局 .env/config.toml 的 llm.model_name，仍为空时用 glm-5",
    ),
    llm_api_key: str = typer.Option("", "--llm-api-key", "-k", help="LLM API Key"),
    sandbox: bool = typer.Option(True, "--sandbox/--no-sandbox", help="启用/禁用沙箱"),
    workspace: str = typer.Option("", "--workspace", "-w", help="可选：执行工作区根目录（绝对路径或相对 agent_workspace_base）"),
) -> None:
    """
    创建新 Agent（敏感信息自动加密存储）

    **渠道**：`--channel feishu`（默认）需 per-agent 飞书 AppID/AppSecret；`--channel wecom`
    无需 per-agent 凭证（企业微信为全局单 App，凭证在 config.toml [channels.wecom]）。

    **展示名约定**：未传 `-d` 时，`display_name` 与 `aliases` 按渠道自动生成——
    feishu → ``SmartClaw-<name>``（请在飞书开放平台将「机器人名称」设为完全相同字符串），
    wecom → 裸 ``<name>``（请在企业微信后台配置应用可见范围/名称）。

    `-m qwen-plus` 等会自动选用百炼 compatible-mode 网关；智谱 glm 默认仍为 coding OpenAI 兼容地址。

    示例:
        smartclaw agent add myagent -i cli_xxx -s xxx -m glm-5 -k <智谱KEY>
        smartclaw agent add myagent -i cli_xxx -s xxx -m qwen-plus -k <百炼KEY>
        smartclaw agent add mywecom --channel wecom -m glm-5 -k <KEY>
    """
    from smartclaw.agent.naming import canonical_display_name
    from smartclaw.agent.manager import AgentManager, CreateAgentRequest

    title(f"创建 Agent: {name}")

    # 未显式指定 -m 时，回落到全局 .env/config.toml 的 llm.model_name；
    # 仍取不到则保留历史默认 glm-5。
    if not llm_model:
        try:
            from smartclaw.config.loader import get_config

            global_model = (getattr(get_config().llm, "model_name", "") or "").strip()
        except Exception:
            global_model = ""
        if global_model:
            llm_model = global_model
            info(f"未指定 -m，使用全局 llm.model_name: [cyan]{global_model}[/cyan]")
        else:
            llm_model = "glm-5"
            info("未指定 -m 且全局 llm.model_name 为空，使用默认: [cyan]glm-5[/cyan]")

    if channel not in ("feishu", "wecom"):
        error(f"不支持的渠道类型: {channel}（仅 feishu / wecom）")
        raise typer.Exit(1)

    if channel == "feishu" and (not app_id or not app_secret):
        error("飞书渠道必填 --app-id/-i 与 --app-secret/-s；wecom 渠道请用 --channel wecom")
        raise typer.Exit(1)

    manager = AgentManager()

    if not display_name:
        display_name = canonical_display_name(name, channel)
    
    # 创建请求
    request = CreateAgentRequest(
        name=name,
        tenant_id=tenant,
        display_name=display_name,
        description=description,
        channel=channel,
        app_id=app_id,
        app_secret=app_secret,
        llm_provider=llm_provider,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        sandbox_enabled=sandbox,
        workspace=workspace,
    )
    
    ok, msg, agent_info = manager.create_agent(request)
    
    if ok:
        success(msg)
        info(f"配置文件: {agent_info.config_path}")
        if channel == "feishu":
            info(f"飞书开放平台「机器人名称」请与此完全一致: [cyan]{display_name}[/cyan]")
        else:
            info(f"企业微信应用「可见范围/名称」请在企业微信后台配置；群内 @ 文案: [cyan]{display_name}[/cyan]")
        from smartclaw.agent.workspace import resolve_agent_workspace_dir
        from smartclaw.config.loader import get_config

        written = manager._read_config(name, tenant_id=tenant) or {}
        ws = resolve_agent_workspace_dir(name, written, get_config(), tenant_id=tenant)
        info(f"执行工作区: {ws}")
        compile_ref = agent_info.qualified_name or name
        info(f"可运行 [cyan]smartclaw agent compile {compile_ref}[/cyan] 将工作区 Markdown 编译进运行时人格（可选）。")
    else:
        error(f"创建失败: {msg}")
        raise typer.Exit(1)


@agent_app.command("update")
def agent_update(
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    channel: str = typer.Option(None, "--channel", help="切换渠道: feishu / wecom（留空不更新）"),
    display_name: str = typer.Option(
        None,
        "--display-name",
        "-d",
        help="机器人名称；与群内 @ 文案一致。默认约定见 `agent add`（feishu: SmartClaw-<name>，wecom: <name>）",
    ),
    description: str = typer.Option(None, "--description", help="Agent 描述"),
    app_id: str = typer.Option(None, "--app-id", "-i", help="飞书 AppID"),
    app_secret: str = typer.Option(None, "--app-secret", "-s", help="飞书 AppSecret（留空不更新）"),
    llm_model: str = typer.Option(None, "--llm-model", "-m", help="LLM 模型"),
    llm_api_key: str = typer.Option(None, "--llm-api-key", "-k", help="LLM API Key"),
    sandbox: bool = typer.Option(None, "--sandbox/--no-sandbox", help="启用/禁用沙箱"),
    enable: bool = typer.Option(None, "--enable/--disable", help="启用/禁用 Agent"),
    workspace: Optional[str] = typer.Option(
        None,
        "--workspace",
        "-w",
        help="执行工作区根目录（绝对路径或相对 agent_workspace_base）；不设表示不修改该项",
    ),
    clear_workspace: bool = typer.Option(
        False,
        "--clear-workspace",
        help="移除自定义 workspace，恢复为默认 ~/.smartclaw/workspace/<name>（或全局 base）",
    ),
) -> None:
    """
    更新 Agent 配置（敏感信息自动加密存储）
    
    示例:
        smartclaw agent update myagent -d "My Bot" -s new_secret
        smartclaw agent update myagent --disable
    """
    from smartclaw.agent.manager import AgentManager, UpdateAgentRequest
    
    title(f"更新 Agent: {name}")
    
    manager = AgentManager()

    if clear_workspace and workspace is not None:
        error("不能同时使用 --workspace 与 --clear-workspace")
        raise typer.Exit(1)
    
    # 构建更新请求
    req_kwargs: dict[str, Any] = dict(
        display_name=display_name,
        description=description,
        channel=channel,
        app_id=app_id,
        app_secret=app_secret if app_secret else None,  # None = 不更新
        llm_model=llm_model,
        llm_api_key=llm_api_key if llm_api_key else None,
        sandbox_enabled=sandbox,
        enabled=enable,
        tenant_id=tenant or None,
    )
    if clear_workspace:
        req_kwargs["workspace"] = ""
    elif workspace is not None:
        req_kwargs["workspace"] = workspace
    request = UpdateAgentRequest(**req_kwargs)
    
    ok, msg = manager.update_agent(name, request)
    
    if ok:
        success(msg)
    else:
        error(f"更新失败: {msg}")
        raise typer.Exit(1)


@agent_app.command("scaffold")
def agent_scaffold(
    name: str = typer.Argument(..., help="Agent 名称（与 agent.json 中 name 一致）"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    force: bool = typer.Option(False, "--force", "-f", help="覆盖已存在的标准 Markdown"),
) -> None:
    """
    为 Agent 解析执行工作区并写入 OpenClaw 风格标准 MD（AGENTS / SOUL / TOOLS …）。
    """
    from smartclaw.agent.manager import AgentManager
    from smartclaw.agent.workspace import resolve_agent_workspace_dir, scaffold_agent_workspace
    from smartclaw.config.loader import get_config

    title(f"工作区脚手架: {name}")
    manager = AgentManager()
    cfg = manager._read_config(name, tenant_id=tenant or None)
    if not cfg:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)
    ws = resolve_agent_workspace_dir(cfg.get("name", name), cfg, get_config(), tenant_id=cfg.get("tenant_id"))
    created = scaffold_agent_workspace(ws, skip_existing=not force)
    success(f"工作区: {ws}")
    if created:
        info("新建: " + ", ".join(created))
    else:
        info("无新建文件（已存在或模板缺失）；使用 --force 覆盖")


@agent_app.command("permissions")
def agent_permissions(
    name: str = typer.Argument(..., help="Agent 名称（与 agent.json 中 name 一致）"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
) -> None:
    """
    打印某 Agent 的「有效权限」视图：内置工具名+角色门禁、Skills 摘要、宿主命令白名单合并结果。

    说明：工具名（如 exec）与宿主 shell 命令是两层；exec 先过工具门禁，再过 Tool Policy 与白名单。
    """
    from smartclaw.agent.manager import AgentManager
    from smartclaw.agent.shell_allowlist import collect_effective_patterns
    from smartclaw.agent.tools import get_tool_registry
    from smartclaw.agent.tools.builtin_registration import register_builtin_tools
    from smartclaw.agent.workspace import resolve_agent_workspace_dir
    from smartclaw.config.loader import get_config
    from smartclaw.skills.registry import SkillRegistry

    title(f"权限视图: {name}")
    manager = AgentManager()
    ac = manager._read_config(name, tenant_id=tenant or None)
    if not ac:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)

    cfg = get_config()
    ws = resolve_agent_workspace_dir(ac.get("name", name), ac, cfg, tenant_id=ac.get("tenant_id"))
    register_builtin_tools()
    registry = get_tool_registry()
    req = getattr(cfg.auth, "tool_required_roles_any", None) or {}

    info("[内置工具 / LLM function 名] 调用前若 config.toml 中配置了 auth.tool_required_roles_any[工具名]，则需对应飞书角色。")
    for d in sorted(registry.list_all(), key=lambda x: x.name):
        roles = req.get(d.name)
        line = f"  • {d.name}"
        if roles:
            line += f"  → 角色(任一): {sorted(roles)}"
        info(line)

    info("[宿主命令 / exec] 在工具门禁通过后，依次：① 宿主命令策略（smartclaw.exec_policy）② 合并 Shell 白名单（空则本层不限制）。")
    ex = getattr(cfg, "execution", None)
    if ex:
        path_opt = getattr(ex, "shell_allowlist_path", None) or ""
        if str(path_opt).strip():
            info(f"  execution.shell_allowlist_path: {path_opt}")
        inline = getattr(ex, "shell_allowlist", None) or []
        if inline:
            info(f"  execution.shell_allowlist 条数: {len(inline)}")
    info(f"  解析后工作区: {ws}")
    shell_file = ws / "tools" / "SHELL_ALLOWLIST.txt"
    info(f"  workspace/tools/SHELL_ALLOWLIST.txt: 存在={shell_file.is_file()}")

    patterns = collect_effective_patterns(cfg=cfg, agent_config=ac, workspace_root=ws)
    if patterns:
        info(f"  合并白名单规则数: {len(patterns)}（展示前 40 条）")
        for p in patterns[:40]:
            info(f"    – {p}")
        if len(patterns) > 40:
            info(f"    … 其余 {len(patterns) - 40} 条省略")
    else:
        info("  合并白名单: （空）— 仅此层不拦截宿主命令（仍受 Tool Policy 约束）")

    view = SkillRegistry(str(ws), config=cfg).build()
    info("[Skills] 当前工作区 eligible 摘要：")
    if view.eligible_skills:
        for s in view.eligible_skills:
            sk = s.get("skill_key") or s.get("name")
            info(f"  • {sk}  risk={s.get('risk_level')!r}")
    else:
        info("  （无 eligible skill 或未配置 skills 目录）")


@agent_app.command("delete")
def agent_delete(
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    force: bool = typer.Option(False, "--force", "-f", help="跳过确认直接删除"),
) -> None:
    """
    删除 Agent
    
    示例:
        smartclaw agent delete myagent
        smartclaw agent delete myagent --force
    """
    from smartclaw.agent.manager import AgentManager
    
    title(f"删除 Agent: {name}")
    
    manager = AgentManager()
    
    # 确认删除
    if not force:
        warning(f"即将删除 Agent '{name}'，此操作不可恢复！")
        if not typer.confirm("确认删除？"):
            info("已取消")
            return
    
    ref = f"{tenant}/{name}" if tenant else name
    ok, msg = manager.delete_agent(ref)
    
    if ok:
        success(msg)
    else:
        error(f"删除失败: {msg}")
        raise typer.Exit(1)


@agent_app.command("encrypt")
def agent_encrypt(
    name: str = typer.Argument(None, help="Agent 名称（不指定则加密所有）"),
    all: bool = typer.Option(False, "--all", "-a", help="加密所有 Agent"),
) -> None:
    """
    将敏感信息加密存储
    
    示例:
        smartclaw agent encrypt myagent     # 加密指定 Agent
        smartclaw agent encrypt --all       # 加密所有 Agent
    """
    from smartclaw.agent.manager import AgentManager
    
    title("敏感信息加密")
    
    manager = AgentManager()
    
    if all or not name:
        # 加密所有
        success_count, fail_count = manager.encrypt_all()
        console.print(f"\n[bold]加密完成:[/bold]")
        console.print(f"  [green]✅ 成功: {success_count}[/green]")
        console.print(f"  [red]❌ 失败: {fail_count}[/red]")
    else:
        # 加密指定
        ok, msg = manager.encrypt_existing(name)
        if ok:
            success(msg)
        else:
            error(f"加密失败: {msg}")
            raise typer.Exit(1)



# ==================== channel 子命令 ====================


@channel_app.command("setup")
def channel_setup(
    channel: str = typer.Argument(..., help="渠道类型: feishu / wecom"),
) -> None:
    """
    配置渠道

    交互式引导配置飞书或企业微信。
    """
    if channel not in ("feishu", "wecom"):
        error(f"不支持的渠道类型: {channel}")
        raise typer.Exit(1)

    title(f"配置渠道: {channel}")

    if channel == "feishu":
        _setup_feishu()
    else:
        _setup_wecom()


def _mutate_channels_feishu_account(
    cfg: Any,
    aid: str,
    sec: str,
    *,
    encrypt_key: Optional[str],
    verification_token: Optional[str],
    set_default: bool,
) -> str:
    """
    在内存中更新 cfg.channels.feishu（不写盘）。
    返回本次账号对应的 TOML 键名。
    """
    from smartclaw.config.loader import FeishuAccountConfig, feishu_account_key_from_app_id

    ch = cfg.channels.feishu
    updated_key: Optional[str] = None

    for k, acc in ch.accounts.items():
        if acc.app_id.strip() == aid:
            ek = encrypt_key if encrypt_key is not None else acc.encrypt_key
            vt = (
                verification_token
                if verification_token is not None
                else acc.verification_token
            )
            ch.accounts[k] = FeishuAccountConfig(
                app_id=aid,
                app_secret=sec,
                name=acc.name or k,
                enabled=True,
                encrypt_key=ek or "",
                verification_token=vt or "",
            )
            updated_key = k
            info(f"config.toml: 已更新账号键 [cyan]{k}[/cyan]（同 app_id）")
            break

    if updated_key is None:
        new_k = feishu_account_key_from_app_id(aid, set(ch.accounts.keys()))
        ch.accounts[new_k] = FeishuAccountConfig(
            app_id=aid,
            app_secret=sec,
            name=new_k,
            enabled=True,
            encrypt_key=encrypt_key or "",
            verification_token=verification_token or "",
        )
        updated_key = new_k
        info(f"config.toml: 已新增账号键 [cyan]{new_k}[/cyan]（由 app_id 自动生成）")

    ch.enabled = True
    if set_default or not (ch.default and ch.default in ch.accounts):
        ch.default = str(updated_key)

    return str(updated_key)


@channel_app.command("add-feishu")
def channel_add_feishu(
    app_id: Optional[str] = typer.Option(
        None,
        "--app-id",
        "-i",
        help="飞书 App ID（cli_ 开头）",
    ),
    app_secret: Optional[str] = typer.Option(
        None,
        "--app-secret",
        "-s",
        help="飞书 App Secret",
    ),
    encrypt_key: Optional[str] = typer.Option(
        None,
        "--encrypt-key",
        help="事件/消息 Encrypt Key（可选）",
    ),
    verification_token: Optional[str] = typer.Option(
        None,
        "--verification-token",
        help="验证 Token（可选）",
    ),
    set_default: bool = typer.Option(
        True,
        "--set-default/--no-set-default",
        help="将此账号写入 channels.feishu.default（多机器人时可用 --no-set-default）",
    ),
) -> None:
    """
    将飞书机器人凭证写入 config.toml（多账号键名自动生成 acc_<app_id 后 8 位>）。

    已存在相同 app_id 时更新 Secret，不新增键。
    """
    from smartclaw.config.loader import (
        Config,
        ConfigLoader,
        reload_config,
    )

    title("添加飞书账号到 config.toml")

    aid = (app_id or typer.prompt("飞书 App ID")).strip()
    sec = (app_secret or typer.prompt("飞书 App Secret", hide_input=True)).strip()
    if not aid.startswith("cli_"):
        warning("App ID 通常以 cli_ 开头，请确认未输错")

    config_path = paths.get_config_file()
    loader_read = ConfigLoader(config_path=config_path)
    if config_path.exists():
        cfg = loader_read.load()
    else:
        cfg = Config()

    _mutate_channels_feishu_account(
        cfg,
        aid,
        sec,
        encrypt_key=encrypt_key,
        verification_token=verification_token,
        set_default=set_default,
    )

    loader = ConfigLoader(config_path=config_path)
    loader.save(cfg, path=config_path)
    success(f"已写入 {config_path}")
    info(f"当前 default 账号键: [cyan]{cfg.channels.feishu.default}[/cyan]")
    try:
        reload_config()
    except Exception:
        pass


@channel_app.command("bind-feishu")
def channel_bind_feishu(
    agent: str = typer.Argument(..., help="Agent 名称（如 default）"),
    app_id: Optional[str] = typer.Option(
        None,
        "--app-id",
        "-i",
        help="飞书 App ID（cli_ 开头）",
    ),
    app_secret: Optional[str] = typer.Option(
        None,
        "--app-secret",
        "-s",
        help="飞书 App Secret",
    ),
    encrypt_key: Optional[str] = typer.Option(
        None,
        "--encrypt-key",
        help="事件/消息 Encrypt Key（可选）",
    ),
    verification_token: Optional[str] = typer.Option(
        None,
        "--verification-token",
        help="验证 Token（可选）",
    ),
    set_default: bool = typer.Option(
        True,
        "--set-default/--no-set-default",
        help="将此 app 设为 channels.feishu.default（多机器人时可关闭）",
    ),
) -> None:
    """
    一键：写入 config.toml（HTTP 飞书适配器）并同步到指定 Agent 的 agent.json。

    等价于依次执行 channel add-feishu 与 agent update（仅飞书字段）。
    """
    from smartclaw.agent.manager import AgentManager, UpdateAgentRequest
    from smartclaw.config.loader import (
        Config,
        ConfigLoader,
        reload_config,
    )

    title(f"一键绑定飞书 → config.toml + Agent [cyan]{agent}[/cyan]")

    aid = (app_id or typer.prompt("飞书 App ID")).strip()
    sec = (app_secret or typer.prompt("飞书 App Secret", hide_input=True)).strip()
    if not aid.startswith("cli_"):
        warning("App ID 通常以 cli_ 开头，请确认未输错")

    manager = AgentManager()
    if manager.get_agent(agent) is None:
        error(f"Agent 不存在: {agent}（请先 smartclaw agent add …）")
        raise typer.Exit(1)

    config_path = paths.get_config_file()
    loader_read = ConfigLoader(config_path=config_path)
    if config_path.exists():
        cfg = loader_read.load()
    else:
        cfg = Config()

    toml_key = _mutate_channels_feishu_account(
        cfg,
        aid,
        sec,
        encrypt_key=encrypt_key,
        verification_token=verification_token,
        set_default=set_default,
    )

    loader = ConfigLoader(config_path=config_path)
    loader.save(cfg, path=config_path)
    success(f"已写入 config.toml: {config_path}")
    info(f"channels.feishu.default = [cyan]{cfg.channels.feishu.default}[/cyan]（本账号键 [cyan]{toml_key}[/cyan]）")

    try:
        reload_config()
    except Exception:
        pass

    req = UpdateAgentRequest(
        app_id=aid,
        app_secret=sec,
    )
    ok, msg = manager.update_agent(agent, req)
    if ok:
        success(msg)
        info("飞书已同步：HTTP 适配器（config.toml）与 Agent 凭证（agent.json）一致。")
    else:
        error(f"config.toml 已保存，但 Agent 更新失败: {msg}")
        raise typer.Exit(1)


def _setup_feishu() -> None:
    """配置飞书渠道"""
    info("一键（推荐，config.toml + agent.json）：")
    info("  smartclaw channel bind-feishu default --app-id cli_xxx --app-secret <密钥>")
    info("")
    info("仅写 config.toml（HTTP 适配器）：")
    info("  smartclaw channel add-feishu --app-id cli_xxx --app-secret <密钥>")
    info("")
    info("多机器人：对每个 Agent 执行一次 bind-feishu <agent> …；或 add-feishu + agent update。")
    info("")

    # 生成回调 URL
    callback_url = "https://your-domain.com/webhook/feishu"

    info("")
    info(f"回调 URL 示例: {callback_url}")
    info("")
    info("下一步:")
    info("  1. 将回调 URL 配置到飞书开放平台")
    info("  2. 运行 'smartclaw start' 启动服务")


def _setup_wecom() -> None:
    """配置企业微信渠道"""
    info("企业微信配置向导")
    info("")
    info("请准备以下信息:")
    info("  1. 企业 ID (Corp ID)")
    info("  2. 应用 Agent ID")
    info("  3. 应用 Secret")
    info("")

    typer.prompt("请输入企业 ID")
    typer.prompt("请输入应用 Agent ID")
    typer.prompt("请输入应用 Secret", hide_input=True)

    # 生成回调 URL
    callback_url = "https://your-domain.com/webhook/wecom"

    info("")
    success("配置完成!")
    info(f"回调 URL: {callback_url}")
    info("")
    info("下一步:")
    info("  1. 将回调 URL 配置到企业微信管理后台")
    info("  2. 运行 'smartclaw start' 启动服务")





# ==================== tool 子命令 ====================

tool_app = typer.Typer(help="工具管理命令")
app.add_typer(tool_app, name="tool")


@tool_app.command("install")
def tool_install(
    source: str = typer.Argument(..., help="工具源（本地路径/Git URL/PyPI 包名）"),
) -> None:
    """
    安装工具

    支持三种安装方式：
    - 本地目录: smartclaw tool install /path/to/tool
    - Git 仓库: smartclaw tool install https://github.com/xxx/tool
    - PyPI 包: smartclaw tool install smartclaw-tool-xxx
    """
    from smartclaw.tool_packages.manager import get_tool_manager

    manager = get_tool_manager()
    success = manager.install(source)

    if not success:
        raise typer.Exit(1)


@tool_app.command("uninstall")
def tool_uninstall(
    name: str = typer.Argument(..., help="工具名称"),
) -> None:
    """
    卸载工具
    """
    from smartclaw.tool_packages.manager import get_tool_manager

    manager = get_tool_manager()
    success = manager.uninstall(name)

    if not success:
        raise typer.Exit(1)


@tool_app.command("list")
def tool_list() -> None:
    """
    列出已安装的工具
    """
    from rich.table import Table

    from smartclaw.tool_packages.manager import get_tool_manager

    manager = get_tool_manager()
    tools = manager.list()

    table = Table(title="已安装工具", show_header=True, header_style="cyan bold")
    table.add_column("名称")
    table.add_column("版本")
    table.add_column("描述")
    table.add_column("函数数")
    table.add_column("状态")

    for tool in tools:
        status = "[green]启用[/green]" if tool.enabled else "[dim]禁用[/dim]"
        table.add_row(
            tool.name,
            tool.version,
            (
                tool.description[:30] + "..."
                if len(tool.description) > 30
                else tool.description
            ),
            str(len(tool.functions)),
            status,
        )

    if not tools:
        info("暂无已安装的工具")
    else:
        console.print(table)


@tool_app.command("info")
def tool_info(
    name: str = typer.Argument(..., help="工具名称"),
) -> None:
    """
    显示工具详细信息
    """

    from smartclaw.tool_packages.manager import get_tool_manager

    manager = get_tool_manager()
    tool = manager.get(name)

    if not tool:
        error(f"工具不存在: {name}")
        raise typer.Exit(1)

    # 显示工具信息
    info_text = f"""名称: {tool.name}
版本: {tool.version}
路径: {tool.path}
状态: {"启用" if tool.enabled else "禁用"}

描述:
{tool.description}

函数列表:"""

    for func in tool.functions:
        info_text += (
            f"\n  - {func.get('name', 'unknown')}: {func.get('description', '')}"
        )

    print_panel(info_text, title_str=f"工具信息: {name}")


@tool_app.command("enable")
def tool_enable(
    name: str = typer.Argument(..., help="工具名称"),
) -> None:
    """启用工具"""
    from smartclaw.tool_packages.manager import get_tool_manager

    manager = get_tool_manager()
    if manager.enable(name):
        success(f"已启用工具: {name}")
    else:
        error(f"工具不存在: {name}")
        raise typer.Exit(1)


@tool_app.command("disable")
def tool_disable(
    name: str = typer.Argument(..., help="工具名称"),
) -> None:
    """禁用工具"""
    from smartclaw.tool_packages.manager import get_tool_manager

    manager = get_tool_manager()
    if manager.disable(name):
        success(f"已禁用工具: {name}")
    else:
        error(f"工具不存在: {name}")
        raise typer.Exit(1)


@tool_app.command("create")
def tool_create(
    name: str = typer.Argument(..., help="工具名称"),
    path: str = typer.Option(".", "--path", "-p", help="创建路径"),
) -> None:
    """
    创建工具模板

    创建一个新的工具包模板，包含必要的文件结构。
    """
    import json
    from pathlib import Path

    tool_dir = Path(path) / f"smartclaw-tool-{name}"

    if tool_dir.exists():
        error(f"目录已存在: {tool_dir}")
        raise typer.Exit(1)

    tool_dir.mkdir(parents=True)

    # 创建 tool.json
    tool_json = {
        "name": name,
        "version": "1.0.0",
        "description": f"{name} 工具",
        "entry": "main.py",
        "functions": [
            {
                "name": f"{name}_example",
                "description": "示例函数",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string", "description": "输入参数"}
                    },
                    "required": ["input"],
                },
                "timeout_ms": 5000,
            }
        ],
    }

    with open(tool_dir / "tool.json", "w", encoding="utf-8") as f:
        json.dump(tool_json, f, indent=2, ensure_ascii=False)

    # 创建 main.py
    main_py = f'''"""
{name} 工具实现
"""


def {name}_example(input: str) -> str:
    """
    示例函数

    参数:
        input: 输入参数

    返回:
        处理结果
    """
    return f"处理结果: {{input}}"
'''

    with open(tool_dir / "main.py", "w", encoding="utf-8") as f:
        f.write(main_py)

    # 创建 SKILL.md
    skill_md = f"""# {name} 工具

## 概述

{tool_json["description"]}

## 安装

```bash
smartclaw tool install /path/to/smartclaw-tool-{name}
```

## 函数

### {name}_example

示例函数

**参数:**
- input (string): 输入参数

**返回:**
- 处理结果字符串

## 使用示例

```python
# 在 Agent 中使用
# 工具会自动加载到 Agent 的工具注册表
```

## 版本历史

### v1.0.0
- 初始版本
"""

    with open(tool_dir / "SKILL.md", "w", encoding="utf-8") as f:
        f.write(skill_md)

    success(f"工具模板创建成功: {tool_dir}")
    info("下一步:")
    info(f"  1. 编辑 {tool_dir}/main.py 实现功能")
    info(f"  2. 编辑 {tool_dir}/tool.json 添加更多函数")
    info(f"  3. 安装: smartclaw tool install {tool_dir}")


# ==================== skills 子命令 ====================


def _load_skills_report() -> dict[str, Any]:
    from pathlib import Path

    from smartclaw.config.loader import get_config
    from smartclaw.skills.status import build_workspace_skill_status

    config = get_config()
    workspace_dir = str(Path.cwd())
    return build_workspace_skill_status(workspace_dir, config=config)


def _runtime_workspace_and_config() -> tuple[str, Any]:
    from pathlib import Path
    from smartclaw.config.loader import get_config

    return str(Path.cwd()), get_config()


@skills_app.command("list")
def skills_list(
    eligible: bool = typer.Option(False, "--eligible", help="仅显示可用 skills"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """列出当前工作区可见的 skills"""
    report = _load_skills_report()
    skills = report["skills"]
    if eligible:
        skills = [s for s in skills if s.get("eligible")]

    if json_output:
        import json

        console.print_json(json.dumps({"skills": skills, "total": len(skills)}, ensure_ascii=False))
        return

    if not skills:
        info("暂无可见 skills")
        return

    table = Table(title="Skills 列表", show_header=True, header_style="cyan bold")
    table.add_column("Key")
    table.add_column("名称")
    table.add_column("版本")
    table.add_column("风险")
    table.add_column("状态")
    table.add_column("来源")
    table.add_column("主环境变量")

    for skill in skills:
        status = "[green]ready[/green]" if skill.get("eligible") else "[yellow]blocked[/yellow]"
        table.add_row(
            skill.get("skill_key", ""),
            skill.get("name", ""),
            skill.get("version", ""),
            skill.get("risk_level", ""),
            status,
            skill.get("source", ""),
            skill.get("primary_env") or "-",
        )
    console.print(table)


@skills_app.command("check")
def skills_check(
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """检查 skills 可用性与缺失依赖"""
    report = _load_skills_report()
    if json_output:
        import json

        console.print_json(json.dumps(report, ensure_ascii=False))
        return

    total = report.get("total", 0)
    eligible_count = report.get("eligible", 0)
    info(f"Skills 可用性: {eligible_count}/{total}")

    blocked = [s for s in report.get("skills", []) if not s.get("eligible")]
    if not blocked:
        success("所有 skills 已就绪")
        return

    table = Table(title="阻塞 Skills", show_header=True, header_style="yellow bold")
    table.add_column("名称")
    table.add_column("原因")
    table.add_column("缺失 bins")
    table.add_column("缺失 env")
    for item in blocked:
        table.add_row(
            item.get("name", ""),
            item.get("blocked_reason") or "-",
            ", ".join(item.get("missing_bins", [])) or "-",
            ", ".join(item.get("missing_env", [])) or "-",
        )
    console.print(table)


@skills_app.command("info")
def skills_info(
    name: str = typer.Argument(..., help="Skill 名称"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """查看单个 skill 详情"""
    report = _load_skills_report()
    target = None
    for skill in report.get("skills", []):
        if skill.get("name", "").lower() == name.lower():
            target = skill
            break

    if not target:
        error(f"Skill 不存在: {name}")
        raise typer.Exit(1)

    if json_output:
        import json

        console.print_json(json.dumps(target, ensure_ascii=False))
        return

    print_panel(
        (
            f"Key: {target.get('skill_key')}\n"
            f"名称: {target.get('name')}\n"
            f"版本: {target.get('version')}\n"
            f"风险等级: {target.get('risk_level')}\n"
            f"来源: {target.get('source')}\n"
            f"描述: {target.get('description')}\n"
            f"启用: {target.get('enabled')}\n"
            f"可用: {target.get('eligible')}\n"
            f"主环境变量: {target.get('primary_env') or '-'}\n"
            f"缺失 bins: {', '.join(target.get('missing_bins', [])) or '-'}\n"
            f"缺失 env: {', '.join(target.get('missing_env', [])) or '-'}\n"
            f"阻塞原因: {target.get('blocked_reason') or '-'}"
        ),
        title_str=f"Skill 详情: {target.get('name')}",
        style="cyan",
    )


@skills_app.command("create")
def skills_create(
    name: str = typer.Argument(..., help="Skill 名称（建议小写）"),
    description: str = typer.Option(..., "--description", "-d", help="Skill 描述"),
    owner: str = typer.Option(..., "--owner", help="Owner"),
    reviewer: str = typer.Option(..., "--reviewer", help="Reviewer"),
    risk_level: str = typer.Option("info", "--risk", help="风险等级: info/warn/high/critical"),
    install_method: str = typer.Option("none", "--install-method", help="安装策略"),
    install_spec: str = typer.Option("", "--install-spec", help="安装目标"),
    requires_bins: str = typer.Option("", "--requires-bins", help="逗号分隔 bin 依赖"),
    requires_env: str = typer.Option("", "--requires-env", help="逗号分隔 env 依赖"),
) -> None:
    """创建标准化 skill 脚手架"""
    from smartclaw.skills.scaffold import create_skill_scaffold

    workspace_dir, _ = _runtime_workspace_and_config()
    result = create_skill_scaffold(
        workspace_dir,
        name=name,
        description=description,
        owner=owner,
        reviewer=reviewer,
        risk_level=risk_level,
        install_method=install_method,
        install_spec=install_spec,
        requires_bins=[x.strip() for x in requires_bins.split(",") if x.strip()],
        requires_env=[x.strip() for x in requires_env.split(",") if x.strip()],
    )
    success(f"Skill 脚手架已创建: {result['skill_dir']}")


@skills_app.command("validate")
def skills_validate(
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """校验 schema/目录/字段完整性"""
    from smartclaw.skills.validate import validate_workspace_skills

    workspace_dir, config = _runtime_workspace_and_config()
    report = validate_workspace_skills(workspace_dir, config=config)
    if json_output:
        import json
        console.print_json(json.dumps(report, ensure_ascii=False))
        return

    if report["global_errors"]:
        for item in report["global_errors"]:
            error(item)
    for item in report["skills"]:
        if item["errors"]:
            error(f"{item['skill_key']}: {'; '.join(item['errors'])}")
        elif item["warnings"]:
            warning(f"{item['skill_key']}: {'; '.join(item['warnings'])}")
        else:
            success(f"{item['skill_key']}: ok")
    if not report["ok"]:
        raise typer.Exit(1)


@skills_app.command("lint")
def skills_lint(
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """执行命名/描述等 lint 规则"""
    from smartclaw.skills.validate import lint_workspace_skills
    import json

    workspace_dir, config = _runtime_workspace_and_config()
    report = lint_workspace_skills(workspace_dir, config=config)
    if json_output:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return

    for item in report["skills"]:
        if item["issues"]:
            warning(f"{item['skill_key']}: {'; '.join(item['issues'])}")
        else:
            success(f"{item['skill_key']}: clean")
    if not report["ok"]:
        raise typer.Exit(1)


@skills_app.command("test")
def skills_test(
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """运行每个 skill 的 smoke test"""
    from smartclaw.skills.testing import run_workspace_skill_tests
    import json

    workspace_dir, config = _runtime_workspace_and_config()
    report = run_workspace_skill_tests(workspace_dir, config=config)
    if json_output:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return
    for item in report["skills"]:
        if item["ok"]:
            success(f"{item['skill_key']}: smoke passed")
        else:
            error(f"{item['skill_key']}: {item.get('reason') or item.get('output', '')}")
    if not report["ok"]:
        raise typer.Exit(1)


@skills_app.command("gate")
def skills_gate() -> None:
    """CI 质量门禁：validate + lint + test"""
    info("running: skills validate")
    try:
        skills_validate(json_output=False)
    except typer.Exit as e:
        error("skills validate failed")
        raise e
    info("running: skills lint")
    try:
        skills_lint(json_output=False)
    except typer.Exit as e:
        error("skills lint failed")
        raise e
    info("running: skills test")
    try:
        skills_test(json_output=False)
    except typer.Exit as e:
        error("skills test failed")
        raise e
    success("skills gate passed")


@skills_app.command("install")
def skills_install(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
    force: bool = typer.Option(False, "--force", help="忽略审批/critical 阻断"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """安装 skill（含安全扫描和审计）"""
    from smartclaw.skills.lifecycle import install_skill
    import json

    workspace_dir, config = _runtime_workspace_and_config()
    result = install_skill(workspace_dir, name=name, config=config, force=force)
    if json_output:
        console.print_json(json.dumps(result, ensure_ascii=False))
        if not result["ok"]:
            raise typer.Exit(1)
        return
    if result["ok"]:
        success(f"安装成功: {result['skill_key']}")
    else:
        error(f"安装失败: {result.get('reason') or result.get('logs', '')}")
        raise typer.Exit(1)


@skills_app.command("uninstall")
def skills_uninstall(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """卸载 skill 并记录生命周期状态"""
    from smartclaw.skills.lifecycle import uninstall_skill
    import json

    workspace_dir, config = _runtime_workspace_and_config()
    result = uninstall_skill(workspace_dir, name=name, config=config)
    if json_output:
        console.print_json(json.dumps(result, ensure_ascii=False))
        if not result["ok"]:
            raise typer.Exit(1)
        return
    if result["ok"]:
        success(f"卸载成功: {result['skill_key']}")
    else:
        error(f"卸载失败: {result.get('logs', '')}")
        raise typer.Exit(1)


@skills_app.command("repair")
def skills_repair(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
    force: bool = typer.Option(False, "--force", help="忽略审批/critical 阻断"),
) -> None:
    """修复 skill（先卸载再安装）"""
    from smartclaw.skills.lifecycle import repair_skill

    workspace_dir, config = _runtime_workspace_and_config()
    result = repair_skill(workspace_dir, name=name, config=config, force=force)
    if result["ok"]:
        success(f"修复成功: {name}")
    else:
        error(f"修复失败: {result}")
        raise typer.Exit(1)


@skills_app.command("snapshot")
def skills_snapshot(
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """刷新并输出 skills snapshot version"""
    from smartclaw.skills.watch import refresh_workspace_snapshot
    import json

    workspace_dir, config = _runtime_workspace_and_config()
    snap = refresh_workspace_snapshot(workspace_dir, config=config)
    if json_output:
        console.print_json(json.dumps(snap, ensure_ascii=False))
        return
    info(f"snapshot version: {snap['version']}")
    info(f"skills count: {snap['count']}")


@skills_app.command("watch")
def skills_watch(
    interval: float = typer.Option(1.0, "--interval", help="轮询秒数"),
) -> None:
    """监听 skills 变化并自动刷新 snapshot"""
    from smartclaw.skills.watch import watch_workspace_skills

    workspace_dir, config = _runtime_workspace_and_config()
    info("开始监听 skills 目录变化，按 Ctrl+C 停止")
    try:
        for event in watch_workspace_skills(workspace_dir, config=config, interval_seconds=interval):
            success(
                f"skills changed: {event['old_version'][:8]} -> {event['new_version'][:8]} (count={event['count']})"
            )
    except KeyboardInterrupt:
        info("已停止监听")


@skills_app.command("registry")
def skills_registry(
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """查看安装生命周期注册表"""
    from smartclaw.skills.storage import read_registry
    import json

    registry = read_registry()
    if json_output:
        console.print_json(json.dumps(registry, ensure_ascii=False))
        return
    installed = registry.get("installed", {})
    if not installed:
        info("暂无安装记录")
        return
    table = Table(title="Skills 安装注册表", show_header=True, header_style="cyan bold")
    table.add_column("Skill Key")
    table.add_column("版本")
    table.add_column("状态")
    table.add_column("方法")
    table.add_column("最近操作时间")
    for key, item in installed.items():
        table.add_row(
            key,
            str(item.get("version", "")),
            str(item.get("status", "")),
            str(item.get("method", "")),
            str(item.get("last_operation_at", "")),
        )
    console.print(table)


@skills_app.command("events")
def skills_events(
    lines: int = typer.Option(30, "--lines", "-n", help="显示最近 N 条"),
) -> None:
    """查看 skills 生命周期事件日志"""
    from smartclaw.skills.storage import get_events_file

    event_file = get_events_file()
    if not event_file.exists():
        info("暂无 events 记录")
        return
    content = event_file.read_text(encoding="utf-8").splitlines()
    for row in content[-max(lines, 1):]:
        console.print(row)


@skills_app.command("approve")
def skills_approve(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
    env: str = typer.Option("staging", "--env", help="审批环境: development/staging/production"),
    note: str = typer.Option("", "--note", help="审批备注"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """环境级审批（staging/prod 分离）"""
    import json
    from smartclaw.skills.governance import approve_skill

    workspace_dir, config = _runtime_workspace_and_config()
    result = approve_skill(workspace_dir, name=name, env=env, note=note, config=config)
    if json_output:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    success(f"审批通过: {result['skill_key']} ({result['env']})")


@skills_app.command("approvals")
def skills_approvals(
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """查看审批记录"""
    import json
    from smartclaw.skills.storage import read_approvals

    report = read_approvals()
    if json_output:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return
    approvals = report.get("approvals", {})
    if not approvals:
        info("暂无审批记录")
        return
    table = Table(title="Skills 审批记录", show_header=True, header_style="cyan bold")
    table.add_column("Skill Key")
    table.add_column("环境")
    table.add_column("审批人")
    table.add_column("审批时间")
    table.add_column("备注")
    for skill_key, item in approvals.items():
        # 兼容旧格式
        if isinstance(item, dict) and "approved" in item:
            table.add_row(
                skill_key,
                "legacy",
                str(item.get("operator", "")),
                str(item.get("approved_at", "")),
                str(item.get("note", "")),
            )
            continue
        for env, row in item.items():
            table.add_row(
                skill_key,
                str(env),
                str(row.get("operator", "")),
                str(row.get("approved_at", "")),
                str(row.get("note", "")),
            )
    console.print(table)


@skills_app.command("promote")
def skills_promote(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
    to: str = typer.Option(..., "--to", help="目标环境: development/staging/production"),
    note: str = typer.Option("", "--note", help="发布备注"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """发布 skill 到指定环境（提测/灰度/全量）"""
    import json
    from smartclaw.skills.governance import promote_skill

    workspace_dir, config = _runtime_workspace_and_config()
    result = promote_skill(workspace_dir, name=name, env=to, note=note, config=config)
    if json_output:
        console.print_json(json.dumps(result, ensure_ascii=False))
        if not result.get("ok"):
            raise typer.Exit(1)
        return
    if not result.get("ok"):
        error(f"发布失败: {result.get('reason')}")
        raise typer.Exit(1)
    success(f"发布成功: {result['skill_key']} -> {result['env']} ({result['version']})")


@skills_app.command("rollback")
def skills_rollback(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
    to: str = typer.Option(..., "--to", help="回滚目标环境: development/staging"),
    note: str = typer.Option("", "--note", help="回滚备注"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """回滚 skill 到更低环境"""
    import json
    from smartclaw.skills.governance import rollback_skill

    workspace_dir, config = _runtime_workspace_and_config()
    result = rollback_skill(workspace_dir, name=name, to_env=to, note=note, config=config)
    if json_output:
        console.print_json(json.dumps(result, ensure_ascii=False))
        if not result.get("ok"):
            raise typer.Exit(1)
        return
    if not result.get("ok"):
        error(f"回滚失败: {result.get('reason')}")
        raise typer.Exit(1)
    warning(f"已回滚: {result['skill_key']} {result['from_env']} -> {result['to_env']} ({result['version']})")


@skills_app.command("releases")
def skills_releases(
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """查看发布轨迹"""
    import json
    from smartclaw.skills.storage import read_releases

    report = read_releases()
    if json_output:
        console.print_json(json.dumps(report, ensure_ascii=False))
        return
    releases = report.get("releases", {})
    if not releases:
        info("暂无发布记录")
        return
    table = Table(title="Skills 发布状态", show_header=True, header_style="cyan bold")
    table.add_column("Skill Key")
    table.add_column("当前环境")
    table.add_column("当前版本")
    table.add_column("Deprecated")
    for skill_key, item in releases.items():
        table.add_row(
            skill_key,
            str(item.get("current_env", "-")),
            str(item.get("current_version", "-")),
            "yes" if item.get("deprecated") else "no",
        )
    console.print(table)


@skills_app.command("deprecate")
def skills_deprecate(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
    reason: str = typer.Option(..., "--reason", help="废弃原因"),
    json_output: bool = typer.Option(False, "--json", help="JSON 输出"),
) -> None:
    """标记 skill 废弃并记录通知信息"""
    import json
    from smartclaw.skills.governance import deprecate_skill

    workspace_dir, config = _runtime_workspace_and_config()
    result = deprecate_skill(workspace_dir, name=name, reason=reason, config=config)
    if json_output:
        console.print_json(json.dumps(result, ensure_ascii=False))
        return
    warning(f"已废弃: {result['skill_key']} ({result['reason']})")


@skills_app.command("enable")
def skills_enable(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
) -> None:
    """启用 skill（写入 config.skills.entries）"""
    from smartclaw.config.loader import ConfigLoader, SkillEntryConfig, get_config, reload_config
    from smartclaw.skills.loader import load_workspace_skill_entries
    from smartclaw.skills.storage import record_event

    workspace_dir, config = _runtime_workspace_and_config()
    entries = load_workspace_skill_entries(workspace_dir, config=config)
    target = None
    for entry in entries:
        skill_key = entry.metadata.skill_key or entry.name
        if name.lower() in {entry.name.lower(), skill_key.lower()}:
            target = entry
            break
    if not target:
        error(f"Skill 不存在: {name}")
        raise typer.Exit(1)

    skill_key = target.metadata.skill_key or target.name
    runtime = get_config()
    existing = runtime.skills.entries.get(skill_key)
    if existing is None:
        runtime.skills.entries[skill_key] = SkillEntryConfig(enabled=True)
    else:
        existing.enabled = True
    ConfigLoader().save(runtime)
    reload_config()
    record_event("enable", skill_key, {"enabled": True})
    success(f"已启用: {skill_key}")


@skills_app.command("disable")
def skills_disable(
    name: str = typer.Argument(..., help="Skill 名称或 skill_key"),
) -> None:
    """禁用 skill（写入 config.skills.entries）"""
    from smartclaw.config.loader import ConfigLoader, SkillEntryConfig, get_config, reload_config
    from smartclaw.skills.loader import load_workspace_skill_entries
    from smartclaw.skills.storage import record_event

    workspace_dir, config = _runtime_workspace_and_config()
    entries = load_workspace_skill_entries(workspace_dir, config=config)
    target = None
    for entry in entries:
        skill_key = entry.metadata.skill_key or entry.name
        if name.lower() in {entry.name.lower(), skill_key.lower()}:
            target = entry
            break
    if not target:
        error(f"Skill 不存在: {name}")
        raise typer.Exit(1)

    skill_key = target.metadata.skill_key or target.name
    runtime = get_config()
    existing = runtime.skills.entries.get(skill_key)
    if existing is None:
        runtime.skills.entries[skill_key] = SkillEntryConfig(enabled=False)
    else:
        existing.enabled = False
    ConfigLoader().save(runtime)
    reload_config()
    record_event("disable", skill_key, {"enabled": False})
    success(f"已禁用: {skill_key}")


# ==================== monitoring 子命令 ====================

monitoring_app = typer.Typer(help="监控和统计命令")
app.add_typer(monitoring_app, name="monitoring")


@monitoring_app.command("token-stats")
def monitoring_token_stats(
    agent_id: Optional[str] = typer.Option(None, "--agent", "-a", help="过滤 Agent ID"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="过滤提供商"),
    days: int = typer.Option(7, "--days", "-d", help="统计最近多少天"),
) -> None:
    """
    显示 token 使用统计
    """
    from datetime import datetime, timedelta

    from rich.table import Table

    from smartclaw.monitoring.metrics import get_token_tracker

    tracker = get_token_tracker()

    start_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    end_date = datetime.now().strftime("%Y-%m-%d")

    stats = tracker.get_stats(
        agent_id=agent_id,
        provider=provider,
        start_date=start_date,
        end_date=end_date,
    )

    # 总体统计
    table = Table(title=f"Token 使用统计（最近 {days} 天）")
    table.add_column("指标")
    table.add_column("值")

    table.add_row("请求次数", str(stats["request_count"]))
    table.add_row("总输入 Token", f"{stats['total_prompt_tokens']:,}")
    table.add_row("总输出 Token", f"{stats['total_completion_tokens']:,}")
    table.add_row("总 Token", f"{stats['total_tokens']:,}")
    table.add_row("平均延迟", f"{stats['avg_latency_ms']}ms")
    table.add_row("最小延迟", f"{stats['min_latency_ms']}ms")
    table.add_row("最大延迟", f"{stats['max_latency_ms']}ms")

    console.print(table)

    # 按提供商统计
    if stats["by_provider"]:
        provider_table = Table(title="按提供商统计")
        provider_table.add_column("提供商")
        provider_table.add_column("请求次数")
        provider_table.add_column("Token 数")

        for item in stats["by_provider"]:
            provider_table.add_row(
                item["provider"],
                str(item["count"]),
                f"{item['tokens']:,}",
            )

        console.print(provider_table)

    # 按模型统计
    if stats["by_model"]:
        model_table = Table(title="按模型统计（Top 10）")
        model_table.add_column("模型")
        model_table.add_column("请求次数")
        model_table.add_column("Token 数")

        for item in stats["by_model"]:
            model_table.add_row(
                item["model"],
                str(item["count"]),
                f"{item['tokens']:,}",
            )

        console.print(model_table)


@monitoring_app.command("daily-usage")
def monitoring_daily_usage(
    agent_id: Optional[str] = typer.Option(None, "--agent", "-a", help="过滤 Agent ID"),
    days: int = typer.Option(7, "--days", "-d", help="查询最近多少天"),
) -> None:
    """
    显示每日使用量
    """
    from rich.table import Table

    from smartclaw.monitoring.metrics import get_token_tracker

    tracker = get_token_tracker()
    usage = tracker.get_daily_usage(agent_id=agent_id, days=days)

    if not usage:
        info("暂无使用记录")
        return

    table = Table(title=f"每日使用量（最近 {days} 天）")
    table.add_column("日期")
    table.add_column("请求次数")
    table.add_column("Token 数")
    table.add_column("平均延迟")

    for item in usage:
        table.add_row(
            item["date"],
            str(item["request_count"]),
            f"{item['total_tokens']:,}",
            f"{item['avg_latency_ms']}ms",
        )

    console.print(table)


@monitoring_app.command("clear-old")
def monitoring_clear_old(
    days: int = typer.Option(90, "--days", "-d", help="保留最近多少天的数据"),
) -> None:
    """
    清理旧的使用记录
    """
    from smartclaw.monitoring.metrics import get_token_tracker

    tracker = get_token_tracker()
    deleted = tracker.clear_old_records(days)

    success(f"已清理 {deleted} 条旧记录（保留 {days} 天内数据）")


# ==================== 长连接服务命令 ====================


# ==================== Agent 绑定命令 ====================

bindings_app = typer.Typer(help="Agent 绑定管理")
app.add_typer(bindings_app, name="bindings")


@bindings_app.command("bind-user")
def bind_user(
    user_id: str = typer.Argument(..., help="用户 ID"),
    agent: str = typer.Option("default", "--agent", "-a", help="Agent 名称"),
) -> None:
    """绑定用户到 Agent"""
    from smartclaw.agent.router import AgentRouter

    router = AgentRouter()
    router.bind_user(user_id, agent)

    success(f"已绑定用户 {user_id} 到 Agent {agent}")


@bindings_app.command("bind-group")
def bind_group(
    chat_id: str = typer.Argument(..., help="群聊 ID"),
    agent: str = typer.Option("default", "--agent", "-a", help="Agent 名称"),
) -> None:
    """绑定群聊到 Agent"""
    from smartclaw.agent.router import AgentRouter

    router = AgentRouter()
    router.bind_group(chat_id, agent)

    success(f"已绑定群聊 {chat_id} 到 Agent {agent}")


@bindings_app.command("unbind-user")
def unbind_user(user_id: str = typer.Argument(..., help="用户 ID")) -> None:
    """解绑用户"""
    from smartclaw.agent.router import AgentRouter

    router = AgentRouter()
    router.unbind_user(user_id)

    success(f"已解绑用户 {user_id}")


@bindings_app.command("unbind-group")
def unbind_group(chat_id: str = typer.Argument(..., help="群聊 ID")) -> None:
    """解绑群聊"""
    from smartclaw.agent.router import AgentRouter

    router = AgentRouter()
    router.unbind_group(chat_id)

    success(f"已解绑群聊 {chat_id}")


@bindings_app.command("set-default")
def set_default_agent(
    agent: str = typer.Argument(..., help="Agent 名称"),
) -> None:
    """设置默认 Agent"""
    from smartclaw.agent.router import AgentRouter

    router = AgentRouter()
    router.set_default(agent)

    success(f"已设置默认 Agent: {agent}")


@bindings_app.command("list")
def list_bindings() -> None:
    """列出所有绑定"""
    from rich.table import Table

    from smartclaw.agent.router import AgentRouter

    router = AgentRouter()
    bindings = router.get_bindings()

    if not bindings:
        info("暂无绑定")
        return

    table = Table(title="Agent 绑定列表")
    table.add_column("类型")
    table.add_column("ID")
    table.add_column("Agent")

    for key, agent in bindings.items():
        if key == "default":
            table.add_row("默认", "-", agent)
        elif key.startswith("user:"):
            table.add_row("用户", key[5:], agent)
        elif key.startswith("group:"):
            table.add_row("群聊", key[6:], agent)

    console.print(table)


@bindings_app.command("clear")
def clear_bindings(
    confirm: bool = typer.Option(False, "--yes", "-y", help="确认清空"),
) -> None:
    """清空所有绑定"""
    if not confirm:
        warning("使用 --yes 确认清空所有绑定")
        return

    from smartclaw.agent.router import AgentRouter

    router = AgentRouter()
    router.clear_bindings()

    success("已清空所有绑定")


# ==================== 会话管理命令 ====================


def _split_tenant_agent_ref(agent: str, tenant: str = "") -> tuple[str, str]:
    from smartclaw.tenant import normalize_agent_name, normalize_tenant_id

    if "/" in agent:
        raw_tenant, raw_agent = agent.split("/", 1)
        return normalize_tenant_id(raw_tenant), normalize_agent_name(raw_agent)
    return normalize_tenant_id(tenant or "default"), normalize_agent_name(agent)


def _session_agent_dir(agent: str, tenant: str = "") -> Path:
    from smartclaw.agent.session import default_session_data_dir

    tenant_id, agent_name = _split_tenant_agent_ref(agent, tenant)
    return default_session_data_dir(agent_name, tenant_id)


@app.command("session-list", hidden=True)
def session_list(
    tenant: str = typer.Option("", "--tenant", help="租户 ID；不传则列出全部"),
    agent: str = typer.Option("", "--agent", "-a", help="Agent 名称；也可用 tenant/agent"),
) -> None:
    """列出所有会话"""
    from rich.table import Table
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

    sessions_dir = paths.SESSION_DIR

    if not sessions_dir.exists():
        info("暂无会话记录")
        return

    table = Table(title="会话列表")
    table.add_column("Tenant")
    table.add_column("Agent")
    table.add_column("会话文件")

    rows = 0
    if agent:
        tenant_id, agent_name = _split_tenant_agent_ref(agent, tenant)
        dirs = [(tenant_id, agent_name, _session_agent_dir(agent_name, tenant_id))]
    elif tenant:
        tenant_id = normalize_tenant_id(tenant)
        base = sessions_dir if tenant_id == DEFAULT_TENANT_ID else sessions_dir / tenant_id
        dirs = [(tenant_id, d.name, d) for d in base.iterdir() if d.is_dir()] if base.exists() else []
    else:
        dirs = []
        for d in sessions_dir.iterdir():
            if not d.is_dir():
                continue
            direct_files = list(d.glob("*.json"))
            if direct_files:
                dirs.append((DEFAULT_TENANT_ID, d.name, d))
            for child in d.iterdir():
                if child.is_dir() and list(child.glob("*.json")):
                    dirs.append((normalize_tenant_id(d.name), child.name, child))

    for tenant_id, agent_name, agent_dir in dirs:
        if not agent_dir.exists():
            continue
        for session_file in agent_dir.glob("*.json"):
            table.add_row(tenant_id, tenant_agent_key(agent_name, tenant_id), session_file.name)
            rows += 1

    if rows == 0:
        info("暂无会话记录")
        return

    console.print(table)


@app.command("session-clear-all", hidden=True)
def session_clear_all(
    tenant: str = typer.Option("", "--tenant", help="只清除指定租户；不传则清除全部"),
    confirm: bool = typer.Option(False, "--yes", "-y", help="确认清除"),
) -> None:
    """清除所有会话"""
    import shutil
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id

    if not confirm:
        target = f"租户 {tenant} 的" if tenant else "所有"
        warning(f"使用 --yes 确认清除{target}会话")
        return

    tenant_id = normalize_tenant_id(tenant) if tenant else ""
    sessions_dir = paths.SESSION_DIR
    target_dir = (
        sessions_dir
        if not tenant_id
        else sessions_dir if tenant_id == DEFAULT_TENANT_ID else sessions_dir / tenant_id
    )

    if target_dir.exists():
        if tenant_id == DEFAULT_TENANT_ID:
            for agent_dir in list(sessions_dir.iterdir()):
                if agent_dir.is_dir() and list(agent_dir.glob("*.json")):
                    shutil.rmtree(agent_dir)
            success("已清除 default 租户会话")
        else:
            shutil.rmtree(target_dir)
            success(f"已清除{('租户 ' + tenant_id + ' 的') if tenant_id else '所有'}会话")
    else:
        info("暂无会话记录")


# ==================== 编译命令 ====================



@agent_app.command("clear-history")
def agent_clear_history(
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
) -> None:
    """清除指定 Agent 的历史会话"""
    import shutil

    sessions_dir = _session_agent_dir(name, tenant)
    tenant_id, agent_name = _split_tenant_agent_ref(name, tenant)
    if sessions_dir.exists():
        shutil.rmtree(sessions_dir)
        success(f"已清除 Agent {tenant_id}/{agent_name} 的所有会话历史")
    else:
        info(f"Agent {tenant_id}/{agent_name} 暂无会话记录")

@agent_app.command("compile")
def agent_compile(
    agent_name: Optional[str] = typer.Argument(
        None, help="Agent 名称（不指定则编译所有）"
    ),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    force: bool = typer.Option(False, "--force", "-f", help="强制重新编译"),
    watch: bool = typer.Option(False, "--watch", "-w", help="监听模式（自动编译）"),
) -> None:
    """
    编译 Agent 配置

    将 Markdown 配置编译为高性能的 JSON 配置。

    示例:
        smartclaw agent compile              # 编译所有 agent
        smartclaw agent compile my-agent     # 编译指定 agent
        smartclaw agent compile --force      # 强制重新编译
        smartclaw agent compile --watch      # 监听模式
    """
    import asyncio
    from pathlib import Path

    from smartclaw.config.compiler import ConfigCompiler

    # 使用统一的路径查找
    agents_dir = paths.get_agents_dir()

    if not agents_dir.exists():
        error("Agents 目录不存在，请先运行 'smartclaw init' 初始化")
        raise typer.Exit(1)

    compiler = ConfigCompiler(agents_dir)

    if watch:
        # 监听模式
        title("监听模式")
        info("监听配置文件变化...")
        info("按 Ctrl+C 退出")

        async def watch_and_compile():
            import asyncio

            try:
                while True:
                    # 初始编译
                    if agent_name:
                        ref = f"{tenant}/{agent_name}" if tenant else agent_name
                        await compiler.compile_agent(ref, force=True)
                    else:
                        results = await compiler.compile_all(force=force)
                        success_count = sum(1 for v in results.values() if v)
                        total_count = len(results)
                        info(f"编译完成: {success_count}/{total_count}")

                    # 每 5 秒检查一次
                    await asyncio.sleep(5)
            except KeyboardInterrupt:
                info("停止监听")

        try:
            asyncio.run(watch_and_compile())
        except KeyboardInterrupt:
            pass
    else:
        # 单次编译
        if agent_name:
            # 编译单个 agent
            ref = f"{tenant}/{agent_name}" if tenant else agent_name
            success_flag = asyncio.run(compiler.compile_agent(ref, force))
            if not success_flag:
                raise typer.Exit(1)
        else:
            # 编译所有 agent
            title("编译所有 Agent")
            results = asyncio.run(compiler.compile_all(force))

            # 显示结果
            success_count = sum(1 for v in results.values() if v)
            total_count = len(results)

            if total_count == 0:
                warning("没有找到需要编译的 Agent")
                return

            # 打印结果表格
            table = Table(title=f"编译结果 ({success_count}/{total_count})")
            table.add_column("Agent", style="cyan")
            table.add_column("状态", style="magenta")

            for name, result in results.items():
                status = "✅ 成功" if result else "❌ 失败"
                table.add_row(name, status)

            console.print(table)

            if success_count < total_count:
                raise typer.Exit(1)


@agent_app.command("set-llm")
def agent_set_llm(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    provider: str = typer.Option("openai", "--provider", "-p", help="模型提供商 (openai, zhipu, qwen, kimi, deepseek等)"),
    model: str = typer.Option(..., "--model", "-m", help="模型名称"),
    api_key: str = typer.Option(..., "--api-key", "-k", help="API Key（必填）"),
    base_url: Optional[str] = typer.Option(None, "--base-url", "-b", help="自定义 Base URL"),
) -> None:
    """
    设置 Agent 的 LLM 模型配置
    
    示例:
        smartclaw agent set-llm default -m glm-5 -k xxx -b https://open.bigmodel.cn/api/coding/paas/v4
        smartclaw agent set-llm coder_heima -m glm-4 -k xxx -p openai -b https://open.bigmodel.cn/api/coding/paas/v4
    """
    from smartclaw.agent.manager import AgentManager

    manager = AgentManager()
    config = manager._read_config(name, tenant_id=tenant or None)
    if not config:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)

    # 预设厂商配置
    preset_urls = {
        "openai": "https://api.openai.com/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "kimi": "https://api.moonshot.cn/v1",
        "deepseek": "https://api.deepseek.com/v1",
        "bigmodel": "https://open.bigmodel.cn/api/coding/paas/v4",
    }

    # 如果没有提供 base_url，使用预设或提示错误
    if not base_url:
        base_url = preset_urls.get(provider.lower(), "")
    
    if not base_url:
        error(f"未知的 provider '{provider}'，请通过 --base-url 指定 API 地址")
        raise typer.Exit(1)

    if not api_key or api_key == "xxx":
        error("API Key 不能为空，请通过 --api-key 指定")
        raise typer.Exit(1)

    from smartclaw.llm.base import normalize_agent_llm_dict

    prev = dict(config.get("llm") or {})
    config["llm"] = normalize_agent_llm_dict(
        {
            **prev,
            "provider": provider,
            "model_name": model,
            "api_key": api_key,
            "base_url": base_url,
            "temperature": prev.get("temperature", 0.7),
            "max_tokens": prev.get("max_tokens", 8192),
        }
    )

    if not manager._write_config(config.get("name", name), config, tenant_id=config.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)

    success(f"已更新 Agent {name} 的 LLM 配置")
    info(f"  提供商: {provider}")
    info(f"  llm.model_name（与 --model 一致）: {config['llm'].get('model_name')}")
    info(f"  Base URL: {base_url}")

@agent_app.command("set-vision")
def agent_set_vision(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    enabled: bool = typer.Option(False, "--enable/--disable", help="是否启用视觉理解"),
    model: str = typer.Option(None, "--model", "-m", help="视觉模型名称 (如 glm-4v)"),
    api_key: str = typer.Option(None, "--api-key", "-k", help="API Key（与 LLM 相同可省略）"),
    base_url: Optional[str] = typer.Option(None, "--base-url", "-b", help="自定义 Base URL"),
) -> None:
    """
    设置 Agent 的视觉理解配置（可选，覆盖全局配置）
    
    示例:
        smartclaw agent set-vision default --enable -m glm-4v -k xxx
        smartclaw agent set-vision coder_heima --disable
    """
    from smartclaw.agent.manager import AgentManager

    manager = AgentManager()
    config = manager._read_config(name, tenant_id=tenant or None)
    if not config:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)

    # 读取 LLM 的 api_key 作为默认值
    llm_api_key = api_key or config.get("llm", {}).get("api_key", "")
    
    vision_config = {
        "enabled": enabled,
        "model": model or "glm-4v",
        "api_key": llm_api_key,
        "base_url": base_url or config.get("llm", {}).get("base_url", "https://open.bigmodel.cn/api/coding/paas/v4"),
    }
    
    llm_cfg = dict(config.get("llm") or {})
    llm_cfg["vision"] = vision_config
    config["llm"] = llm_cfg
    if not manager._write_config(config.get("name", name), config, tenant_id=config.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)

    status = "已启用" if enabled else "已禁用"
    success(f"已更新 Agent {name} 的视觉配置: {status}")
    if enabled:
        info(f"  模型: {vision_config['model']}")
        info(f"  Base URL: {vision_config['base_url']}")


@agent_app.command("set-policy")
def agent_set_policy(
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
    mode: str = typer.Option(
        "mention", "--mode", "-m", help="响应模式: mention/open/disabled"
    ),
    scope: str = typer.Option(
        "both", "--scope", "-s", help="作用范围: private/group/both"
    ),
    allow_users: bool = typer.Option(
        True, "--allow-users/--no-allow-users", help="是否允许所有用户"
    ),
    allow_groups: bool = typer.Option(
        True, "--allow-groups/--no-allow-groups", help="是否允许所有群"
    ),
) -> None:
    """
    设置 Agent 的响应策略

    示例:
        # 群聊只响应 @，私聊正常响应
        smartclaw agent set-policy my-agent --mode mention --scope both

        # 只允许私聊
        smartclaw agent set-policy my-agent --mode open --scope private

        # 禁用群聊
        smartclaw agent set-policy my-agent --scope private
    """
    from smartclaw.agent.manager import AgentManager

    # 验证参数
    if mode not in ("mention", "open", "disabled"):
        error(f"无效的响应模式: {mode}")
        raise typer.Exit(1)

    if scope not in ("private", "group", "both"):
        error(f"无效的作用范围: {scope}")
        raise typer.Exit(1)

    manager = AgentManager()
    config = manager._read_config(name, tenant_id=tenant or None)
    if not config:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)

    # 更新策略
    config["policy"] = {
        "mode": mode,
        "scope": scope,
        "allow_all_users": allow_users,
        "allow_all_groups": allow_groups,
        "whitelist_users": config.get("policy", {}).get("whitelist_users", []),
        "whitelist_groups": config.get("policy", {}).get("whitelist_groups", []),
    }

    if not manager._write_config(config.get("name", name), config, tenant_id=config.get("tenant_id")):
        error("写入 agent.json 失败")
        raise typer.Exit(1)

    success(f"已更新 Agent {name} 的策略")
    info(f"  响应模式: {mode}")
    info(f"  作用范围: {scope}")
    info(f"  允许所有用户: {allow_users}")
    info(f"  允许所有群: {allow_groups}")


@agent_app.command("show-policy")
def agent_show_policy(
    name: str = typer.Argument(..., help="Agent 名称"),
    tenant: str = typer.Option("", "--tenant", help="租户 ID；也可用 tenant/name"),
) -> None:
    """
    显示 Agent 的响应策略
    """
    from smartclaw.agent.manager import AgentManager
    from rich.table import Table

    manager = AgentManager()
    config = manager._read_config(name, tenant_id=tenant or None)
    if not config:
        error(f"Agent 不存在: {name}")
        raise typer.Exit(1)

    policy = config.get("policy", {})

    # 显示策略
    table = Table(title=f"Agent {name} 响应策略")
    table.add_column("配置项")
    table.add_column("值")
    table.add_column("说明")

    mode = policy.get("mode", "mention")
    mode_desc = {"mention": "@提及才响应", "open": "响应所有人", "disabled": "禁用"}
    table.add_row("响应模式", mode, mode_desc.get(mode, ""))

    scope = policy.get("scope", "both")
    scope_desc = {"private": "只私聊", "group": "只群聊", "both": "私聊+群聊"}
    table.add_row("作用范围", scope, scope_desc.get(scope, ""))

    allow_users = policy.get("allow_all_users", True)
    table.add_row("允许所有用户", str(allow_users), "")

    allow_groups = policy.get("allow_all_groups", True)
    table.add_row("允许所有群", str(allow_groups), "")

    whitelist_users = policy.get("whitelist_users", [])
    table.add_row(
        "用户白名单",
        str(len(whitelist_users)),
        ", ".join(whitelist_users) if whitelist_users else "-",
    )

    whitelist_groups = policy.get("whitelist_groups", [])
    table.add_row(
        "群白名单",
        str(len(whitelist_groups)),
        ", ".join(whitelist_groups) if whitelist_groups else "-",
    )

    console.print(table)


# ==================== 服务生命周期命令 ====================


@app.command("stop")
def stop_command(
    force: bool = typer.Option(False, "--force", "-f", help="强制终止"),
) -> None:
    """
    停止 SmartClaw 服务

    示例:
        smartclaw stop        # 正常停止
        smartclaw stop -f    # 强制终止
    """
    import os
    import signal
    from pathlib import Path

    title("停止 SmartClaw 服务")

    run_dir = paths.get_run_dir()
    if not run_dir.exists():
        run_dir = Path.home() / ".smartclaw" / "run"
        
    pid_file = run_dir / "smartclaw.pid"
    
    if not pid_file.exists():
        info("未找到 PID 文件，服务可能未在运行")
        return
        
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        error("PID 文件格式错误")
        pid_file.unlink()
        return

    try:
        if force:
            os.kill(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGTERM)
        success(f"已发送停止信号给进程 (PID: {pid})")
        pid_file.unlink(missing_ok=True)
    except ProcessLookupError:
        warning(f"进程 {pid} 不存在，清理失效的 PID 文件")
        pid_file.unlink()
    except PermissionError:
        from smartclaw.pid_check import pid_is_running

        if not pid_is_running(pid):
            warning(f"进程 {pid} 已不存在，清理失效的 PID 文件")
            pid_file.unlink()
            return
        hint = (
            f"没有权限停止进程 {pid}。可尝试以**管理员**打开终端后执行 "
            f"`smartclaw stop -f` 或 `taskkill /PID {pid} /F /T`。"
            if os.name == "nt"
            else f"没有权限停止进程 {pid}，请使用足够权限的用户运行（如 root / sudo）。"
        )
        error(hint)
        raise typer.Exit(1)


@app.command("restart")
def restart_command(
    force: bool = typer.Option(False, "--force", "-f", help="强制终止"),
) -> None:
    """
    重启 SmartClaw 服务

    示例:
        smartclaw restart      # 正常重启
        smartclaw restart -f   # 强制重启
    """
    import os
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    from smartclaw.pid_check import pid_is_running

    title("重启 SmartClaw 服务")

    run_dir = paths.get_run_dir()
    if not run_dir.exists():
        run_dir = Path.home() / ".smartclaw" / "run"
        
    pid_file = run_dir / "smartclaw.pid"
    
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if force:
                os.kill(pid, signal.SIGKILL)
            else:
                os.kill(pid, signal.SIGTERM)
            info(f"已停止旧服务 (PID: {pid})")
            pid_file.unlink(missing_ok=True)
            
            # 等待进程结束
            for _ in range(50):
                if not pid_is_running(pid):
                    break
                time.sleep(0.1)
        except (ValueError, ProcessLookupError):
            warning("清理失效的 PID 文件")
            pid_file.unlink(missing_ok=True)
        except PermissionError:
            error(f"没有权限停止进程 {pid}，请使用 root 权限运行")
            raise typer.Exit(1)

    info("启动新服务...")
    # 继承当前进程的环境变量启动新服务 (使用 daemon 模式)
    cmd = [sys.executable, "-m", "smartclaw.cli", "start", "--daemon"]
    try:
        subprocess.run(cmd, check=True)
        success("服务已重启!")
    except subprocess.CalledProcessError:
        error("启动新服务失败")
        raise typer.Exit(1)


@app.command("pid")
def pid_command() -> None:
    """
    查看 SmartClaw 服务进程 ID

    示例:
        smartclaw pid
    """
    import subprocess

    from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS

    result = subprocess.run(
        ["pgrep", "-f", "smartclaw"],
        capture_output=True,
        text=True,
        **SUBPROCESS_TEXT_KWARGS,
    )
    pids = result.stdout.strip().split("\n")

    if not pids or not pids[0]:
        info("没有运行中的 SmartClaw 服务")
        return

    console.print("\n[bold]运行中的 SmartClaw 进程:[/bold]")
    for pid in pids:
        if pid.isdigit():
            console.print(f"  PID: {pid}")


@app.command("log")
def log_command(
    lines: int = typer.Option(50, "--lines", "-n", help="显示最近 N 行日志"),
    follow: bool = typer.Option(False, "--follow", "-f", help="实时跟踪日志"),
) -> None:
    """
    查看 SmartClaw 服务日志

    示例:
        smartclaw log              # 查看最近 50 行
        smartclaw log -n 100      # 查看最近 100 行
        smartclaw log -f          # 实时跟踪日志
    """
    import subprocess

    from smartclaw.subprocess_io import SUBPROCESS_TEXT_KWARGS
    log_paths = [
        Path("/tmp/smartclaw.log"),
        Path.home() / ".smartclaw" / "logs" / "smartclaw.log",
        paths.get_log_dir() / "smartclaw.log",
    ]

    log_file = None
    for path in log_paths:
        if path.exists():
            log_file = path
            break

    if not log_file:
        info("未找到日志文件")
        info("日志可能写入 stdout/stderr，请使用 'smartclaw log -f' 实时查看")
        return

    title(f"SmartClaw 日志: {log_file}")

    if follow:
        # 实时跟踪
        subprocess.run(["tail", "-n", str(lines), "-f", str(log_file)])
    else:
        # 显示最近 N 行
        result = subprocess.run(
            ["tail", "-n", str(lines), str(log_file)],
            capture_output=True,
            text=True,
            **SUBPROCESS_TEXT_KWARGS,
        )
        if result.stdout:
            console.print(result.stdout)
        if result.stderr:
            error(result.stderr)



@app.command("task-status")
def task_status(
    session_key: Optional[str] = typer.Option(None, "--session", "-s", help="过滤特定会话的任务"),
    all: bool = typer.Option(False, "--all", "-a", help="显示所有任务（包括已完成/失败的）"),
) -> None:
    """
    查看当前子任务(Subagent)的执行状态
    """
    from smartclaw.core.subagent_registry import SubagentRegistry, SubagentStatus
    from rich.table import Table
    from pathlib import Path

    from smartclaw.paths import get_subagent_state_dir

    registry_dir = get_subagent_state_dir()
    if not registry_dir.exists():
        info("暂无任务记录")
        return

    registry = SubagentRegistry(state_dir=str(registry_dir))
    
    if session_key:
        runs = registry.list_for_requester(session_key)
    elif not all:
        runs = registry.list_active()
    else:
        runs = list(registry._runs.values())

    if not runs:
        info("暂无符合条件的任务")
        return

    table = Table(title="Subagent 任务状态", show_header=True, header_style="cyan bold")
    table.add_column("Run ID", style="dim")
    table.add_column("状态")
    table.add_column("模型")
    table.add_column("任务描述")
    table.add_column("会话 ID")

    def _sort_time(run) -> str:
        ts = run.started_at or run.completed_at
        return ts.isoformat() if ts else ""

    for run in sorted(runs, key=_sort_time, reverse=True)[:50]:
        status_color = "green" if run.status == SubagentStatus.COMPLETED else (
            "yellow" if run.status in (SubagentStatus.RUNNING, SubagentStatus.PENDING) else "red"
        )
        session_key = run.requester_session_key or "-"
        table.add_row(
            run.run_id[:8] + "...",
            f"[{status_color}]{run.status.value}[/{status_color}]",
            run.model or "默认",
            (run.task[:30] + "...") if len(run.task) > 30 else run.task,
            session_key[:10] + "..." if len(session_key) > 10 else session_key,
        )

    console.print(table)


# ==================== docker 命令 ====================

@docker_app.command("list")
def docker_list_command():
    """
    列出所有 Docker 容器
    """
    from smartclaw.core.dockerimpl import get_container_pool, get_port_pool
    
    pool = get_container_pool()
    port_pool = get_port_pool()
    
    stats = pool.get_stats()
    by_status = stats.get("by_status", {})
    
    info(f"Docker 容器池状态")
    console.print(f"  最大容器数: {stats['max']}")
    console.print(f"  空闲超时: {pool.idle_timeout}s")
    console.print(f"  端口范围: {port_pool.port_range.start}-{port_pool.port_range.stop - 1}")
    console.print("")
    
    table = Table(title="容器列表", show_header=True, header_style="cyan bold")
    table.add_column("项目名")
    table.add_column("状态")
    table.add_column("镜像")
    table.add_column("端口")
    table.add_column("运行时间")
    
    containers = stats.get("containers", {})
    if not containers:
        info("暂无容器")
        return
    
    for name, info_dict in containers.items():
        status = info_dict.get("status", "UNKNOWN")
        status_color = "green" if status == "RUNNING" else "yellow"
        image = info_dict.get("image", "-")
        ports = info_dict.get("host_ports", {})
        ports_str = ",".join([f"{k}->{v}" for k, v in ports.items()]) if ports else "-"
        uptime = info_dict.get("uptime", "-")
        
        table.add_row(name, f"[{status_color}]{status}[/{status_color}]", image, ports_str, uptime)
    
    console.print(table)


@docker_app.command("stats")
def docker_stats_command():
    """
    显示 Docker 容器统计信息
    """
    from smartclaw.core.dockerimpl import get_container_pool

    pool = get_container_pool()
    stats = pool.get_stats()

    info("Docker 容器统计")
    console.print(f"  总容器数: {stats['total']}")
    console.print(f"  最大容量: {stats['max']}")
    console.print(f"  空闲超时: {stats['idle_timeout_seconds']}s")
    console.print("")

    by_status = stats.get("by_status", {})
    if by_status:
        console.print("[bold]按状态统计:[/bold]")
        for status, count in by_status.items():
            console.print(f"  {status}: {count}")


if __name__ == "__main__":
    app()
