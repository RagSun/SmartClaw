"""
SmartClaw 统一路径常量

所有路径通过此模块获取，确保跨平台兼容。
优先级：环境变量 > 默认路径
"""
import os
import sys
from pathlib import Path

# ==================== 根目录 ====================

def _get_install_root() -> Path:
    """
    获取 SmartClaw 安装根目录
    
    优先级：
    1. 环境变量 SMARTCLAW_HOME
    2. Linux: /opt/smartclaw
    3. macOS: ~/Library/Application Support/smartclaw
    4. Windows: %APPDATA%/smartclaw
    5. Fallback: ~/.smartclaw
    """
    env = os.environ.get("SMARTCLAW_HOME")
    if env:
        return Path(env)
    
    if sys.platform == "linux":
        return Path("/opt/smartclaw")
    elif sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "smartclaw"
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", str(Path.home()))
        return Path(appdata) / "smartclaw"
    else:
        return Path.home() / ".smartclaw"


def _get_user_home() -> Path:
    """获取用户配置根目录 (~/.smartclaw)"""
    return Path.home() / ".smartclaw"


def _get_env_path(name: str) -> Path | None:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def _get_session_dir() -> Path:
    """获取会话持久化目录。"""
    override = _get_env_path("SMARTCLAW_SESSION_DIR")
    if override is not None:
        return override
    return INSTALL_ROOT / "data" / "sessions"


def _get_temp_dir() -> Path:
    """获取临时目录。"""
    override = _get_env_path("SMARTCLAW_TEMP_DIR")
    if override is not None:
        return override
    return INSTALL_ROOT / "tmp"


# ==================== 公开常量 ====================

# 安装根目录
INSTALL_ROOT = _get_install_root()

# 用户配置根目录
USER_HOME = _get_user_home()


def default_docker_workspace_parent() -> Path:
    """
    宿主机上 Docker 工作区的默认父目录（用户可写）。

    非 root 用户无法写入 ``/root/smartclaw_workspace``，故默认落到当前用户家目录下。
    可通过环境变量 ``SMARTCLAW_DOCKER_WORKSPACE_PARENT`` 覆盖。
    供 sandbox/docker.py 与 core/dockerimpl 共用，避免宿主侧工作区路径硬编码。
    """
    raw = (os.environ.get("SMARTCLAW_DOCKER_WORKSPACE_PARENT") or "").strip()
    if raw:
        return Path(os.path.expanduser(raw)).expanduser().resolve()
    return (Path.home() / ".smartclaw" / "docker_workspace").resolve()

# --- 配置 ---
CONFIG_DIR = INSTALL_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "config.toml"

# --- Agent ---
AGENTS_DIR = INSTALL_ROOT / "data" / "agents"
USER_AGENTS_DIR = USER_HOME / "data" / "agents"  # 修改为 data/agents 保持一致

# --- 沙箱 ---
SANDBOX_DIR = INSTALL_ROOT / "sandboxes"
IMAGES_DIR = INSTALL_ROOT / "images"
ROOTFS_PATH = IMAGES_DIR / "rootfs.ext4"
KERNEL_PATH = IMAGES_DIR / "vmlinux"

# --- 运行时 ---
RUN_DIR = INSTALL_ROOT / "run"
PID_FILE = RUN_DIR / "smartclaw.pid"
USER_RUN_DIR = USER_HOME / "run"

# --- 日志 ---
LOG_DIR = INSTALL_ROOT / "logs"
LOG_FILE = LOG_DIR / "smartclaw.log"
USER_LOG_DIR = USER_HOME / "logs"

# --- 会话 ---
SESSION_DIR = _get_session_dir()

# --- 临时 ---
TEMP_DIR = _get_temp_dir()




def get_agents_dirs() -> list[Path]:
    """
    获取 Agent 配置目录列表（按优先级查找）

    优先级：
    1. /opt/smartclaw/data/agents（系统安装）
    2. ~/.smartclaw/data/agents（用户安装）
    3. ~/.smartclaw/agents（兼容旧版）
    """
    return [
        AGENTS_DIR,                    # /opt/smartclaw/data/agents
        USER_AGENTS_DIR,               # ~/.smartclaw/data/agents
        USER_HOME / "agents",          # ~/.smartclaw/agents (兼容旧版)
    ]


def get_agents_dir() -> Path:
    """获取第一个存在的 Agent 目录，若都不存在则返回用户目录"""
    for d in get_agents_dirs():
        if d.exists():
            return d
    return USER_AGENTS_DIR


def get_run_dir() -> Path:
    """获取运行时目录（PID 文件等）。

    PID 等是「节点本地运行期产物」，多副本共享卷部署时必须各副本独立，
    否则会误判「服务已在运行」。可用 ``SMARTCLAW_RUN_DIR`` 覆盖到副本本地路径
    （与 ``SMARTCLAW_SESSION_DIR`` / ``SMARTCLAW_TEMP_DIR`` 同款约定）。
    """
    override = _get_env_path("SMARTCLAW_RUN_DIR")
    if override is not None:
        return override
    if os.access(str(RUN_DIR.parent), os.W_OK):
        return RUN_DIR
    return USER_RUN_DIR


def get_log_dir() -> Path:
    """获取日志目录"""
    if os.access(str(LOG_DIR.parent), os.W_OK):
        return LOG_DIR
    return USER_LOG_DIR


def default_memory_data_dir(agent_id: str, tenant_id: str = "default") -> Path:
    """
    MemoryManager SQLite 等持久化目录（跨平台）。

    优先级：环境变量 SMARTCLAW_MEMORY_DATA_DIR（若为目录则其下按租户/agent 分子目录）>
    SMARTCLAW_HOME/data/memory/{tenant_id}/{agent_id}。

    为保持兼容，default 租户仍使用历史布局：
    {memory_base}/{agent_id}。
    """
    from smartclaw.tenant import tenant_scoped_child

    base = _get_env_path("SMARTCLAW_MEMORY_DATA_DIR")
    if base is None:
        base = INSTALL_ROOT / "data" / "memory"
    return tenant_scoped_child(base, agent_id, tenant_id)


def get_config_search_paths() -> list[Path]:
    """
    配置文件搜索路径（按优先级，供读取与 CLI/运行时对齐）。

    1. /opt/smartclaw/config/config.toml（系统安装）
    2. ~/.smartclaw/config/config.toml（用户安装，``config set`` 默认写入）
    3. 项目源码 config/config.toml（开发态，仅当在仓库内安装时存在）
    4. 当前工作目录 config.toml
    """
    repo_config = Path(__file__).resolve().parent.parent.parent / "config" / "config.toml"
    return [
        CONFIG_FILE,
        USER_HOME / "config" / "config.toml",
        repo_config,
        Path.cwd() / "config.toml",
    ]


def get_default_config_write_path() -> Path:
    """新建配置文件时的默认路径（与 ``get_config_file`` 无文件时一致）。"""
    return USER_HOME / "config" / "config.toml"


def get_config_file() -> Path:
    """
    获取当前生效的配置文件路径（按优先级查找第一个已存在文件）。

    若均不存在，返回用户目录下的默认写入路径。
    """
    for candidate in get_config_search_paths():
        if candidate.exists():
            return candidate
    return get_default_config_write_path()


def get_event_bus_dir() -> Path:
    """
    EventBus 持久化根目录。

    优先级：环境变量 ``EVENT_BUS_DIR`` > ``~/.smartclaw/event-bus``。
    """
    override = _get_env_path("EVENT_BUS_DIR")
    if override is not None:
        return override
    return USER_HOME / "event-bus"


def get_subagent_state_dir() -> Path:
    """
    子 Agent 注册表状态目录。

    优先级：环境变量 ``SUBAGENT_STATE_DIR`` > ``~/.smartclaw/subagent-state``。
    """
    override = _get_env_path("SUBAGENT_STATE_DIR")
    if override is not None:
        return override
    return USER_HOME / "subagent-state"
