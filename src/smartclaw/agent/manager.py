"""
Agent 配置管理器

提供 Agent 的 CRUD 操作、配置验证、敏感信息加密存储。
"""

import json
import os
import re
import sys
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel

import smartclaw.paths as paths
from smartclaw.console import warning
from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

# ==================== 枚举和模型 ====================


class ValidationStatus(str, Enum):
    PASS = "✅"
    FAIL = "❌"
    WARN = "⚠️"


class ValidationResult(BaseModel):
    """验证结果"""
    field: str
    status: ValidationStatus
    message: str


class AgentInfo(BaseModel):
    """Agent 信息"""
    name: str
    tenant_id: str = DEFAULT_TENANT_ID
    qualified_name: str = ""
    display_name: str
    description: str = ""
    channel: str = "feishu"
    enabled: bool = True
    app_id: str = ""
    app_secret_encrypted: str = ""  # 加密后的存储
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_api_key_encrypted: str = ""
    sandbox_enabled: bool = True
    sandbox_type: str = "docker"
    config_path: str = ""


class CreateAgentRequest(BaseModel):
    """创建 Agent 请求"""
    name: str
    tenant_id: str = DEFAULT_TENANT_ID
    display_name: str
    description: str = ""
    # 渠道：feishu（默认，per-agent app_id/app_secret）/ wecom（全局单 App，凭证在 config.toml [channels.wecom]）
    channel: str = "feishu"
    # 飞书渠道凭证（channel=="feishu" 时必填，由调用方校验）；wecom 渠道忽略
    app_id: str = ""
    app_secret: str = ""
    llm_model: str = "glm-5"
    # 与 `agent set-llm --provider` 相同关键字；留空则按模型名推断网关（qwen-*→百炼，glm-*→智谱 coding 等）
    llm_provider: str = ""
    llm_api_key: str = ""
    sandbox_enabled: bool = True
    workspace: str = ""  # 可选：执行工作区根目录（绝对路径或相对 agent_workspace_base）


def resolve_llm_scaffold_for_new_agent(
    llm_model: str,
    *,
    llm_provider_hint: str = "",
) -> tuple[str, str]:
    """
    新建 Agent 写入 agent.json 时的 (provider, base_url)，与 CLI `agent set-llm` 预设网关一致。

    历史行为：未显式指定厂商且模型名无法推断时，仍使用智谱 OpenAI 兼容 coding 网关
    （与此前写死 bigmodel coding 一致）。
    """
    preset_urls: dict[str, str] = {
        "openai": "https://api.openai.com/v1",
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "bigmodel": "https://open.bigmodel.cn/api/coding/paas/v4",
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "kimi": "https://api.moonshot.cn/v1",
        "deepseek": "https://api.deepseek.com/v1",
    }
    hp = (llm_provider_hint or "").strip().lower()
    lm = (llm_model or "").strip().lower()

    alias: dict[str, str] = {
        "glm": "bigmodel",
        "chatglm": "bigmodel",
        "moonshot": "kimi",
    }
    known = frozenset(preset_urls.keys())

    def infer_from_model() -> str:
        if lm.startswith("qwen") or "qwen-" in lm or "-vl-" in lm or lm.startswith("qwen3"):
            return "qwen"
        if "deepseek" in lm:
            return "deepseek"
        if lm.startswith("gpt-") or lm.startswith("o1") or lm.startswith("o3"):
            return "openai"
        if "moonshot" in lm or "kimi" in lm:
            return "kimi"
        if "glm" in lm:
            return "bigmodel"
        return "bigmodel"

    if hp:
        slot = alias.get(hp, hp)
        if slot not in known:
            slot = infer_from_model()
    else:
        slot = infer_from_model()

    base_url = preset_urls[slot]
    if slot == "qwen":
        provider = "qwen"
    elif slot == "deepseek":
        provider = "deepseek"
    elif slot == "openai":
        provider = "openai"
    elif slot == "kimi":
        provider = "openai"
    elif slot == "zhipu":
        provider = "glm"
    else:
        provider = "openai"
    return provider, base_url


class UpdateAgentRequest(BaseModel):
    """更新 Agent 请求"""
    tenant_id: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    channel: Optional[str] = None  # None 不更新；feishu / wecom
    app_id: Optional[str] = None
    app_secret: Optional[str] = None  # None 表示不更新
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_key: Optional[str] = None
    sandbox_enabled: Optional[bool] = None
    sandbox_type: Optional[str] = None
    enabled: Optional[bool] = None
    workspace: Optional[str] = None  # None 不更新；"" 清空为默认布局


# ==================== 加密引擎（模块级单例，进程安全） ====================

_encryption_key: Optional[bytes] = None


@contextmanager
def _exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    """
    跨平台排他文件锁，保证多进程下密钥读写的原子性。

    - Windows: msvcrt.locking（字节区间锁）
    - Unix / macOS: fcntl.flock
    - 无 fcntl 的罕见环境: 仅告警后无锁执行（极小概率竞态）
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if sys.platform == "win32":
        import msvcrt

        fp = open(lock_path, "a+b")
        try:
            fp.seek(0)
            if fp.read(1) == b"":
                fp.write(b"\0")
                fp.truncate(1)
                fp.flush()
                os.fsync(fp.fileno())
            fp.seek(0)
            # LK_LOCK：阻塞直至可锁；适合密钥一次性初始化场景
            msvcrt.locking(fp.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            fp.seek(0)
            try:
                msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            fp.close()
        return

    try:
        import fcntl
    except ImportError:
        warning(
            "当前 Python 无 fcntl，加密密钥初始化无法使用文件锁；"
            "请避免多进程同时首次启动。"
        )
        yield
        return

    fp = open(lock_path, "a+b")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        fp.close()


def _get_encryption_key() -> bytes:
    """
    获取加密密钥（进程安全，单例模式）
    
    使用文件锁确保多进程环境下密钥生成/读取的原子性。
    """
    global _encryption_key

    if _encryption_key is not None:
        return _encryption_key

    key_file = Path.home() / ".smartclaw" / ".key"
    lock_file = Path.home() / ".smartclaw" / ".key.lock"

    key_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        with _exclusive_file_lock(lock_file):
            if key_file.exists():
                with open(key_file, "rb") as f:
                    _encryption_key = f.read()
            else:
                _encryption_key = Fernet.generate_key()
                with open(key_file, "wb") as f:
                    f.write(_encryption_key)
                try:
                    os.chmod(key_file, 0o600)
                except (OSError, NotImplementedError, AttributeError):
                    # Windows / 部分 FS 对 chmod 语义有限，忽略即可
                    pass
    except (PermissionError, OSError) as e:
        # Windows 上偶发 .key.lock 被其他进程短暂占用会抛 PermissionError。
        # 已有 key 时只读加载是安全的，避免凭证解密因锁竞争拖垮 Agent 启动。
        if key_file.exists():
            warning(f"加密密钥锁不可用，降级为只读加载 .key: {e}")
            with open(key_file, "rb") as f:
                _encryption_key = f.read()
        else:
            raise

    return _encryption_key


def _get_fernet() -> Fernet:
    """获取 Fernet 加密实例"""
    return Fernet(_get_encryption_key())


# ==================== 解密错误（自定义异常） ====================


class DecryptionError(Exception):
    """解密失败异常"""
    pass


# ==================== AgentManager 类 ====================


class AgentManager:
    """
    Agent 配置管理器
    
    功能：
    - 配置验证（AppID、AppSecret 格式）
    - CRUD 操作（创建、读取、更新、删除）
    - 敏感信息加密存储
    """

    def __init__(self, agents_dir: Optional[Path] = None):
        """
        初始化 AgentManager
        
        Args:
            agents_dir: Agent 配置目录，默认为 ~/.smartclaw/agents
        """
        # 支持多个 agents 目录
        self._agents_dirs = []
        if agents_dir:
            self._agents_dirs = [agents_dir]
        else:
            self._agents_dirs = paths.get_agents_dirs()
            # 确保 ~/.smartclaw/agents 也在列表中
            legacy = Path.home() / ".smartclaw" / "agents"
            if legacy not in self._agents_dirs:
                self._agents_dirs.append(legacy)

    def _iter_all_agents_dirs(self):
        """迭代所有 agents 目录"""
        for d in self._agents_dirs:
            if d.exists():
                yield d

    def _agent_config_path(self, agents_dir: Path, name: str, tenant_id: str | None = None) -> Path:
        """Return the config path for a tenant-scoped agent.

        The default tenant keeps the historical ``agents/{name}/agent.json``
        path. Non-default tenants use ``agents/{tenant_id}/{name}/agent.json``
        so two tenants can both own an agent named ``coder``.
        """
        tenant = normalize_tenant_id(tenant_id)
        if tenant == DEFAULT_TENANT_ID:
            return agents_dir / name / "agent.json"
        return agents_dir / tenant / name / "agent.json"

    def _split_agent_ref(self, name: str, tenant_id: str | None = None) -> tuple[str, str]:
        """Split ``tenant/agent`` references while preserving old plain names."""
        if tenant_id:
            return normalize_tenant_id(tenant_id), name
        raw = (name or "").strip()
        if "/" in raw:
            tenant, agent = raw.split("/", 1)
            return normalize_tenant_id(tenant), agent
        return DEFAULT_TENANT_ID, raw

    def _preferred_new_agent_config_dir(self, agent_name: str, tenant: str) -> Path:
        """新建 ``agent.json`` 时的目录：优先安装根 ``SMARTCLAW_HOME/data/agents``，不可用时退回用户目录。

        与 ``get_agents_dirs()`` 读取优先级一致，避免安装根与用户目录两处分裂。
        """
        install_dir = self._agent_config_path(paths.AGENTS_DIR, agent_name, tenant).parent
        user_dir = self._agent_config_path(paths.USER_AGENTS_DIR, agent_name, tenant).parent
        try:
            if install_dir.resolve() == user_dir.resolve():
                return user_dir
            install_dir.mkdir(parents=True, exist_ok=True)
            probe = install_dir / ".smartclaw_write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
            return install_dir
        except OSError as e:
            warning(
                f"无法写入安装根 Agent 目录 {install_dir}（{e}），改用用户目录 {user_dir}"
            )
            return user_dir

    # ==================== 加密/解密（使用模块级加密引擎） ====================

    def _encrypt(self, value: str) -> str:
        """加密字符串"""
        if not value:
            return ""
        return _get_fernet().encrypt(value.encode()).decode()

    def _decrypt(self, encrypted: str) -> str:
        """
        解密字符串
        
        Raises:
            DecryptionError: 解密失败时抛出（而非静默返回空字符串）
        """
        if not encrypted:
            return ""
        try:
            return _get_fernet().decrypt(encrypted.encode()).decode()
        except (InvalidToken, ValueError) as e:
            raise DecryptionError(f"解密失败: {e}") from e
        except Exception as e:
            raise DecryptionError(f"未知解密错误: {e}") from e

    # ==================== 验证方法 ====================

    @staticmethod
    def validate_app_id(app_id: str) -> ValidationResult:
        """验证 AppID 格式"""
        if not app_id:
            return ValidationResult(
                field="AppID",
                status=ValidationStatus.FAIL,
                message="AppID 不能为空"
            )
        if not re.match(r'^cli_[a-zA-Z0-9]+$', app_id):
            return ValidationResult(
                field="AppID",
                status=ValidationStatus.FAIL,
                message=f"格式错误: {app_id} (应以 cli_ 开头)"
            )
        return ValidationResult(
            field="AppID",
            status=ValidationStatus.PASS,
            message=f"格式正确: {app_id[:15]}..."
        )

    @staticmethod
    def validate_app_secret(app_secret: str) -> ValidationResult:
        """验证 AppSecret 格式"""
        if not app_secret:
            return ValidationResult(
                field="AppSecret",
                status=ValidationStatus.FAIL,
                message="AppSecret 不能为空"
            )
        if len(app_secret) < 16:
            return ValidationResult(
                field="AppSecret",
                status=ValidationStatus.FAIL,
                message=f"长度不足: {len(app_secret)} < 16"
            )
        return ValidationResult(
            field="AppSecret",
            status=ValidationStatus.PASS,
            message=f"长度: {len(app_secret)} 位"
        )

    @staticmethod
    def validate_agent_name(name: str) -> ValidationResult:
        """验证 Agent 名称"""
        if not name:
            return ValidationResult(
                field="Agent Name",
                status=ValidationStatus.FAIL,
                message="Agent 名称不能为空"
            )
        if not re.match(r'^[a-zA-Z0-9_]{2,32}$', name):
            return ValidationResult(
                field="Agent Name",
                status=ValidationStatus.FAIL,
                message="名称只能包含字母、数字、下划线，2-32位"
            )
        return ValidationResult(
            field="Agent Name",
            status=ValidationStatus.PASS,
            message=f"有效: {name}"
        )

    def validate_all(self, name: str) -> list[ValidationResult]:
        """验证指定 Agent 的所有配置"""
        results = []

        # 验证名称
        name_result = self.validate_agent_name(name)
        if name_result.status != ValidationStatus.PASS:
            results.append(name_result)
            return results  # 名称无效，后续验证无意义

        results.append(name_result)

        # 读取配置
        config = self._read_config(name)
        if not config:
            results.append(ValidationResult(
                field="配置文件",
                status=ValidationStatus.FAIL,
                message="配置文件不存在"
            ))
            return results

        # 验证 Display Name
        display_name = config.get("display_name", "")
        if not display_name:
            results.append(ValidationResult(
                field="Display Name",
                status=ValidationStatus.WARN,
                message="未设置"
            ))
        else:
            results.append(ValidationResult(
                field="Display Name",
                status=ValidationStatus.PASS,
                message=f"有效: {display_name}"
            ))

        # 渠道凭证验证：飞书验证 per-agent app_id/app_secret；wecom 凭证为全局配置（config.toml [channels.wecom]）
        channel = (config.get("channel") or "feishu").strip().lower()
        if channel == "feishu":
            feishu_cfg = config.get("feishu", {})
            app_id = feishu_cfg.get("app_id", "")
            results.append(self.validate_app_id(app_id))

            app_secret = feishu_cfg.get("app_secret", "")
            if not app_secret:
                results.append(ValidationResult(
                    field="AppSecret",
                    status=ValidationStatus.FAIL,
                    message="未配置"
                ))
            elif app_secret.startswith("ENC:"):
                # 已加密，尝试解密后验证
                try:
                    decrypted = self._decrypt(app_secret[4:])
                    results.append(self.validate_app_secret(decrypted))
                except DecryptionError as e:
                    results.append(ValidationResult(
                        field="AppSecret",
                        status=ValidationStatus.FAIL,
                        message=f"解密失败: {e}"
                    ))
            else:
                # 未加密
                results.append(self.validate_app_secret(app_secret))
        else:
            results.append(ValidationResult(
                field="渠道凭证",
                status=ValidationStatus.PASS,
                message=f"渠道 {channel}：凭证为全局配置（config.toml [channels.{channel}]）",
            ))

        # 验证 LLM 配置
        llm_cfg = config.get("llm", {})
        api_key = llm_cfg.get("api_key", "")
        if api_key:
            if api_key.startswith("ENC:"):
                try:
                    decrypted_key = self._decrypt(api_key[4:])
                    results.append(ValidationResult(
                        field="LLM API Key",
                        status=ValidationStatus.PASS,
                        message=f"已加密存储 ({len(decrypted_key)} 位)"
                    ))
                except DecryptionError as e:
                    results.append(ValidationResult(
                        field="LLM API Key",
                        status=ValidationStatus.FAIL,
                        message=f"解密失败: {e}"
                    ))
            else:
                results.append(ValidationResult(
                    field="LLM API Key",
                    status=ValidationStatus.WARN,
                    message="未加密存储"
                ))
        else:
            results.append(ValidationResult(
                field="LLM API Key",
                status=ValidationStatus.FAIL,
                message="未配置"
            ))

        model_name = llm_cfg.get("model_name", "")
        if model_name:
            results.append(ValidationResult(
                field="LLM 模型",
                status=ValidationStatus.PASS,
                message=f"使用: {model_name}"
            ))
        else:
            results.append(ValidationResult(
                field="LLM 模型",
                status=ValidationStatus.WARN,
                message="未指定"
            ))

        # 验证沙箱配置
        sandbox_cfg = config.get("sandbox", {})
        sandbox_enabled = sandbox_cfg.get("enabled", False)
        sandbox_type = sandbox_cfg.get("type", "docker")
        valid_types = ["docker", "firecracker", "process"]

        if sandbox_enabled and sandbox_type not in valid_types:
            results.append(ValidationResult(
                field="沙箱配置",
                status=ValidationStatus.WARN,
                message=f"未知类型: {sandbox_type}"
            ))
        else:
            results.append(ValidationResult(
                field="沙箱配置",
                status=ValidationStatus.PASS,
                message=f"{'启用' if sandbox_enabled else '禁用'} ({sandbox_type})"
            ))

        return results

    # ==================== CRUD 操作 ====================

    def _read_config(self, name: str, tenant_id: str | None = None) -> Optional[dict]:
        """读取 Agent 配置（搜索所有目录，支持 tenant/name 引用）"""
        tenant, agent_name = self._split_agent_ref(name, tenant_id)
        for agents_dir in self._iter_all_agents_dirs():
            config_file = self._agent_config_path(agents_dir, agent_name, tenant)
            if config_file.exists():
                try:
                    with open(config_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        data.setdefault("tenant_id", tenant)
                        return data
                except json.JSONDecodeError as e:
                    warning(f"Agent {tenant_agent_key(agent_name, tenant)} JSON 解析失败: {e}")
                    return None
                except Exception as e:
                    warning(f"读取 Agent {tenant_agent_key(agent_name, tenant)} 配置失败: {e}")
                    return None
        return None

    def _write_config(self, name: str, config: dict, tenant_id: str | None = None) -> bool:
        """写入 Agent 配置：已存在则原地更新；新建则优先落在安装根 ``data/agents``。"""
        tenant, agent_name = self._split_agent_ref(name, tenant_id or config.get("tenant_id"))
        config["tenant_id"] = tenant
        # 优先写入已存在的配置目录（任意搜索路径），否则新建优先安装根见 _preferred_new_agent_config_dir
        target_dir = None
        for agents_dir in self._iter_all_agents_dirs():
            existing = self._agent_config_path(agents_dir, agent_name, tenant)
            if existing.exists():
                target_dir = existing.parent
                break

        if target_dir is None:
            target_dir = self._preferred_new_agent_config_dir(agent_name, tenant)

        config_file = target_dir / "agent.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)

        # 写入前备份
        if config_file.exists():
            backup_file = config_file.with_suffix('.json.bak')
            try:
                import shutil
                shutil.copy2(config_file, backup_file)
            except Exception as e:
                warning(f"备份配置文件失败: {e}")

        try:
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            warning(f"写入配置文件失败: {e}")
            return False

    def list_agents(self) -> list[AgentInfo]:
        """列出所有 Agent"""
        agents = []
        seen_names = set()  # 避免重复

        for agents_dir in self._iter_all_agents_dirs():
            if not agents_dir.exists():
                continue
            config_files = list(agents_dir.glob("*/agent.json")) + list(
                agents_dir.glob("*/*/agent.json")
            )
            for config_file in config_files:
                if not config_file.exists():
                    continue
                rel_parts = config_file.relative_to(agents_dir).parts
                if len(rel_parts) == 2:
                    tenant_id = DEFAULT_TENANT_ID
                    agent_dir_name = rel_parts[0]
                elif len(rel_parts) == 3:
                    tenant_id = normalize_tenant_id(rel_parts[0])
                    agent_dir_name = rel_parts[1]
                else:
                    continue

                config = self._read_config(agent_dir_name, tenant_id=tenant_id)
                if not config:
                    continue

                name = config.get("name", agent_dir_name)
                tenant_id = normalize_tenant_id(config.get("tenant_id", tenant_id))
                qualified_name = tenant_agent_key(name, tenant_id)
                if qualified_name in seen_names:
                    continue
                seen_names.add(qualified_name)

                feishu_cfg = config.get("feishu", {})
                llm_cfg = config.get("llm", {})
                sandbox_cfg = config.get("sandbox", {})

                agents.append(AgentInfo(
                    name=name,
                    tenant_id=tenant_id,
                    qualified_name=qualified_name,
                    display_name=config.get("display_name", name),
                    description=config.get("description", ""),
                    channel=config.get("channel", "feishu"),
                    enabled=config.get("enabled", True),
                    app_id=feishu_cfg.get("app_id", ""),
                    app_secret_encrypted=feishu_cfg.get("app_secret", ""),
                    llm_provider=llm_cfg.get("provider", "openai"),
                    llm_model=llm_cfg.get("model_name", ""),
                    llm_api_key_encrypted=llm_cfg.get("api_key", ""),
                    sandbox_enabled=sandbox_cfg.get("enabled", True),
                    sandbox_type=sandbox_cfg.get("type", "docker"),
                    config_path=str(config_file),
                ))

        return agents

    def get_agent(self, name: str) -> Optional[AgentInfo]:
        """获取指定 Agent"""
        tenant, agent_name = self._split_agent_ref(name)
        for agent in self.list_agents():
            if agent.name == agent_name and agent.tenant_id == tenant:
                return agent
        return None

    def create_agent(self, request: CreateAgentRequest) -> tuple[bool, str, Optional[AgentInfo]]:
        """创建新 Agent"""
        tenant_id = normalize_tenant_id(request.tenant_id)
        # 验证名称
        name_result = self.validate_agent_name(request.name)
        if name_result.status != ValidationStatus.PASS:
            return False, name_result.message, None

        # 检查是否已存在（与 _write_config 搜索路径一致）
        for ad in self._iter_all_agents_dirs():
            if not ad.exists():
                continue
            cf = self._agent_config_path(ad, request.name, tenant_id)
            if cf.exists():
                return False, f"Agent '{tenant_agent_key(request.name, tenant_id)}' 已存在", None

        # 飞书渠道：验证 per-agent AppID/AppSecret 并查重；wecom 渠道凭证为全局配置，跳过
        if request.channel == "feishu":
            app_id_result = self.validate_app_id(request.app_id)
            if app_id_result.status != ValidationStatus.PASS:
                return False, app_id_result.message, None

            app_secret_result = self.validate_app_secret(request.app_secret)
            if app_secret_result.status != ValidationStatus.PASS:
                return False, app_secret_result.message, None

            # 检查 AppID 是否已被其他 Agent 使用
            for agent in self.list_agents():
                if agent.app_id == request.app_id:
                    return False, f"AppID '{request.app_id[:15]}...' 已被 Agent '{agent.name}' 使用", None

        llm_prov, llm_base = resolve_llm_scaffold_for_new_agent(
            request.llm_model,
            llm_provider_hint=request.llm_provider,
        )

        from smartclaw.agent.naming import canonical_display_name

        disp = (request.display_name or "").strip() or canonical_display_name(request.name, request.channel)

        # 构建配置（敏感信息加密存储）
        config = {
            "name": request.name,
            "tenant_id": tenant_id,
            "description": request.description,
            "display_name": disp,
            "channel": request.channel,
            "enabled": True,
            "aliases": [disp] if disp else [],
            "llm": {
                "provider": llm_prov,
                "model_name": request.llm_model,
                "base_url": llm_base,
                "api_key": f"ENC:{self._encrypt(request.llm_api_key)}" if request.llm_api_key else "",
                "temperature": 0.7,
                "max_tokens": 4096,
            },
            "sandbox": {
                "enabled": request.sandbox_enabled,
                "type": "docker",
                "memory_mb": 128,
                "cpu_count": 1,
            },
            "tools": {
                "enforce_allowed_tools": False,
                "allowed": [],
                "denied": [],
            },
        }
        if request.channel == "feishu":
            config["feishu"] = {
                "app_id": request.app_id,
                "app_secret": f"ENC:{self._encrypt(request.app_secret)}",
            }
        if (request.workspace or "").strip():
            config["workspace"] = request.workspace.strip()

        if not self._write_config(request.name, config, tenant_id=tenant_id):
            return False, "写入配置文件失败", None

        from smartclaw.agent.workspace import resolve_agent_workspace_dir, scaffold_agent_workspace
        from smartclaw.config.loader import get_config

        ws_root = resolve_agent_workspace_dir(request.name, config, get_config(), tenant_id=tenant_id)
        scaffold_agent_workspace(ws_root, skip_existing=True)

        config_path = ""
        for agents_dir in self._iter_all_agents_dirs():
            cf = self._agent_config_path(agents_dir, request.name, tenant_id)
            if cf.is_file():
                config_path = str(cf)
                break
        if not config_path:
            config_path = str(self._agent_config_path(paths.USER_AGENTS_DIR, request.name, tenant_id))

        agent_info = AgentInfo(
            name=request.name,
            tenant_id=tenant_id,
            qualified_name=tenant_agent_key(request.name, tenant_id),
            display_name=disp,
            description=request.description,
            app_id=request.app_id,
            sandbox_enabled=request.sandbox_enabled,
            llm_model=request.llm_model,
            config_path=config_path,
        )

        return True, f"Agent '{tenant_agent_key(request.name, tenant_id)}' 创建成功", agent_info

    def update_agent(self, name: str, request: UpdateAgentRequest) -> tuple[bool, str]:
        """更新 Agent 配置"""
        tenant, agent_name = self._split_agent_ref(name, request.tenant_id)
        config = self._read_config(agent_name, tenant_id=tenant)
        if not config:
            return False, f"Agent '{tenant_agent_key(agent_name, tenant)}' 不存在"

        if request.display_name is not None:
            config["display_name"] = request.display_name
            dn = (request.display_name or "").strip()
            if dn:
                al = list(config.get("aliases") or [])
                lows = {str(a).lower() for a in al if a}
                if dn.lower() not in lows:
                    al.insert(0, dn)
                config["aliases"] = al

        if request.description is not None:
            config["description"] = request.description

        if request.enabled is not None:
            config["enabled"] = request.enabled

        if request.workspace is not None:
            ws = request.workspace.strip()
            if ws:
                config["workspace"] = ws
            else:
                config.pop("workspace", None)

        # 渠道更新
        effective_channel = (request.channel or config.get("channel") or "feishu").strip().lower()
        if request.channel is not None:
            config["channel"] = request.channel.strip()

        # 飞书配置更新（仅飞书渠道；wecom 渠道凭证为全局配置，不在 agent.json 维护）
        if effective_channel == "feishu":
            feishu_cfg = config.get("feishu", {})
            if request.app_id is not None:
                app_id_result = self.validate_app_id(request.app_id)
                if app_id_result.status != ValidationStatus.PASS:
                    return False, app_id_result.message
                feishu_cfg["app_id"] = request.app_id

            if request.app_secret is not None:
                app_secret_result = self.validate_app_secret(request.app_secret)
                if app_secret_result.status != ValidationStatus.PASS:
                    return False, app_secret_result.message
                feishu_cfg["app_secret"] = f"ENC:{self._encrypt(request.app_secret)}"

            config["feishu"] = feishu_cfg
        elif request.app_id is not None or request.app_secret is not None:
            warning("当前 Agent 渠道为 wecom，app_id/app_secret 由全局 config.toml [channels.wecom] 维护，已忽略")

        # LLM 配置更新
        llm_cfg = config.get("llm", {})
        if request.llm_provider is not None:
            llm_cfg["provider"] = request.llm_provider
        if request.llm_model is not None:
            llm_cfg["model_name"] = request.llm_model
        if request.llm_api_key is not None:
            llm_cfg["api_key"] = f"ENC:{self._encrypt(request.llm_api_key)}"
        config["llm"] = llm_cfg

        # 沙箱配置更新
        sandbox_cfg = config.get("sandbox", {})
        if request.sandbox_enabled is not None:
            sandbox_cfg["enabled"] = request.sandbox_enabled
        if request.sandbox_type is not None:
            sandbox_cfg["type"] = request.sandbox_type
        config["sandbox"] = sandbox_cfg

        if not self._write_config(agent_name, config, tenant_id=tenant):
            return False, "写入配置文件失败"

        return True, f"Agent '{tenant_agent_key(agent_name, tenant)}' 更新成功"

    def delete_agent(self, name: str) -> tuple[bool, str]:
        """删除 Agent"""
        tenant, agent_name = self._split_agent_ref(name)
        config = self._read_config(agent_name, tenant_id=tenant)
        if not config:
            return False, f"Agent '{tenant_agent_key(agent_name, tenant)}' 不存在"

        agent_dir = None
        for agents_dir in self._iter_all_agents_dirs():
            cf = self._agent_config_path(agents_dir, agent_name, tenant)
            if cf.exists():
                agent_dir = cf.parent
                break

        if not agent_dir:
            return False, f"Agent '{tenant_agent_key(agent_name, tenant)}' 目录不存在"

        try:
            import shutil
            shutil.rmtree(agent_dir)
            return True, f"Agent '{tenant_agent_key(agent_name, tenant)}' 已删除"
        except Exception as e:
            return False, f"删除失败: {e}"

    def encrypt_existing(self, name: str) -> tuple[bool, str]:
        """将现有 Agent 的敏感信息加密存储"""
        config = self._read_config(name)
        if not config:
            return False, f"Agent '{name}' 不存在"

        modified = False
        errors = []

        # 加密 AppSecret
        feishu_cfg = config.get("feishu", {})
        app_secret = feishu_cfg.get("app_secret", "")
        if app_secret and not app_secret.startswith("ENC:"):
            try:
                feishu_cfg["app_secret"] = f"ENC:{self._encrypt(app_secret)}"
                modified = True
            except Exception as e:
                errors.append(f"AppSecret: {e}")

        # 加密 LLM API Key
        llm_cfg = config.get("llm", {})
        api_key = llm_cfg.get("api_key", "")
        if api_key and not api_key.startswith("ENC:"):
            try:
                llm_cfg["api_key"] = f"ENC:{self._encrypt(api_key)}"
                modified = True
            except Exception as e:
                errors.append(f"LLM API Key: {e}")

        if errors:
            return False, f"加密失败: {', '.join(errors)}"

        if not modified:
            return True, f"Agent '{name}' 无需加密（已加密或无敏感信息）"

        config["feishu"] = feishu_cfg
        config["llm"] = llm_cfg
        if not self._write_config(name, config):
            return False, "写入配置文件失败"
        return True, f"Agent '{name}' 敏感信息已加密"

    def encrypt_all(self) -> tuple[int, int]:
        """加密所有 Agent 的敏感信息"""
        success = 0
        fail = 0
        for agent in self.list_agents():
            ok, _ = self.encrypt_existing(agent.qualified_name or agent.name)
            if ok:
                success += 1
            else:
                fail += 1
        return success, fail


# 全局实例（不推荐使用，每次都应创建新实例以保证进程隔离）
_manager: Optional[AgentManager] = None


def get_agent_manager() -> AgentManager:
    """获取全局 AgentManager 实例"""
    global _manager
    if _manager is None:
        _manager = AgentManager()
    return _manager
