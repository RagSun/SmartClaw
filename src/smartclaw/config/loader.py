"""
配置加载器模块

支持飞书凭证三种写法（解析后统一为 accounts 字典）：

1) 易用扁平（单机器人，与 OpenClaw 向导式仅凭证一致）：
[channels.feishu]
enabled = true
app_id = "cli_xxx"
app_secret = "yyy"
encrypt_key = ""  # 可选

2) 多账号（显式键名）：
[channels.feishu]
enabled = true
default = "pm1"

[channels.feishu.accounts.pm1]
app_id = "xxx"
app_secret = "yyy"
name = "产品经理1号"
enabled = true

3) 多账号（仅填飞书控制台 ID/Secret，键名自动生成 acc_<app_id 后 8 位>）：
[channels.feishu]
enabled = true

[[channels.feishu.accounts]]
app_id = "cli_xxx"
app_secret = "yyy"

[[channels.feishu.accounts]]
app_id = "cli_yyy"
app_secret = "zzz"
"""

import smartclaw.paths as paths
from pathlib import Path
from typing import Any, Optional

import tomli_w
try:
    import tomllib as tomli
except ImportError:
    import tomli

from pydantic import BaseModel, Field

from smartclaw.console import warning


# ==================== 飞书 accounts 键名（多账号易用） ====================


def feishu_account_key_from_app_id(app_id: str, used_keys: set[str]) -> str:
    """
    根据 app_id 生成 channels.feishu.accounts 的稳定键名：
    acc_<app_id 后 8 字符>（仅保留字母数字与下划线）；冲突时为 acc_*_2、acc_*_3 ...
    """
    aid = (app_id or "").strip()
    tail = aid[-8:] if len(aid) >= 8 else aid
    slug = "".join(
        c if c.isalnum() or c == "_" else "_" for c in tail
    ).strip("_") or "app"
    base = f"acc_{slug}"
    key = base
    n = 2
    while key in used_keys:
        key = f"{base}_{n}"
        n += 1
    used_keys.add(key)
    return key


def _feishu_accounts_list_to_dict(items: list[Any]) -> dict[str, dict[str, Any]]:
    """将 TOML [[channels.feishu.accounts]] 数组项转为具名字典。"""
    out: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("app_id") or "").strip()
        sec = str(item.get("app_secret") or "").strip()
        if not aid or not sec:
            warning("跳过无效的飞书 accounts 表项（缺少 app_id 或 app_secret）")
            continue
        key = feishu_account_key_from_app_id(aid, used)
        out[key] = {
            "app_id": aid,
            "app_secret": sec,
            "name": str(item.get("name") or key).strip() or key,
            "enabled": bool(item.get("enabled", True)),
            "encrypt_key": str(item.get("encrypt_key") or "").strip(),
            "verification_token": str(item.get("verification_token") or "").strip(),
        }
    return out


# ==================== 飞书多账号配置 ====================


class FeishuAccountConfig(BaseModel):
    """单个飞书账号配置"""

    app_id: str = Field(default="", description="App ID")
    app_secret: str = Field(default="", description="App Secret")
    name: str = Field(default="", description="账号名称")
    enabled: bool = Field(default=False, description="是否启用")
    encrypt_key: str = Field(default="", description="加密 Key")
    verification_token: str = Field(default="", description="验证 Token")


class FeishuChannelConfig(BaseModel):
    """飞书渠道配置（多账号）"""

    enabled: bool = Field(default=False, description="是否启用")
    default: str = Field(default="", description="默认账号名称")
    accounts: dict[str, FeishuAccountConfig] = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True

    def get_default_account(self) -> Optional[FeishuAccountConfig]:
        """获取默认账号"""
        if not self.default and self.accounts:
            self.default = next(iter(self.accounts))
        if self.default and self.default in self.accounts:
            return self.accounts[self.default]
        return None

    def get_account(self, name: str) -> Optional[FeishuAccountConfig]:
        """获取指定账号"""
        return self.accounts.get(name)

    def list_enabled_accounts(self) -> list[str]:
        """列出所有已启用的账号"""
        return [name for name, acc in self.accounts.items() if acc.enabled]


# ==================== 其他配置类 ====================


class WecomChannelConfig(BaseModel):
    """企业微信渠道配置"""

    enabled: bool = Field(default=False, description="是否启用")
    corp_id: str = Field(default="", description="企业 ID")
    agent_id: str = Field(default="", description="应用 Agent ID")
    secret: str = Field(default="", description="应用 Secret")
    token: str = Field(default="", description="Token")
    encoding_aes_key: str = Field(default="", description="EncodingAESKey")


class ChannelsConfig(BaseModel):
    """渠道配置"""

    feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)
    wecom: WecomChannelConfig = Field(default_factory=WecomChannelConfig)

    class Config:
        arbitrary_types_allowed = True


class ServerConfig(BaseModel):
    """服务器配置"""

    host: str = Field(default="0.0.0.0", description="监听地址")
    port: int = Field(default=8000, description="监听端口")
    workers: int = Field(default=1, description="工作进程数")
    max_request_bytes: int = Field(
        default=1048576,
        ge=0,
        description=(
            "HTTP 请求体大小上限（字节），超过即 413。边缘防护：在 JSON 解析 / "
            "飞书解密等昂贵操作前挡掉超大 payload，抑制内存放大型 DoS。0=不限制。"
            "默认 1 MiB（飞书事件远小于此）。"
        ),
    )


class SandboxConfig(BaseModel):
    """沙箱配置"""

    enabled: bool = Field(default=True, description="是否启用沙箱")
    backend: str = Field(default="firecracker", description="沙箱后端")
    warm_pool_size: int = Field(default=5, description="预热池大小")
    max_instances: int = Field(default=100, description="最大实例数")
    memory_mb: int = Field(default=128, description="内存 MB")
    cpu_count: int = Field(default=1, description="CPU 核数")


class LoggingConfig(BaseModel):
    """日志配置"""

    level: str = Field(default="INFO", description="日志级别")
    file_enabled: bool = Field(default=True, description="是否启用文件日志")
    file_path: str = Field(default="logs/smartclaw.log", description="日志文件路径")
    console_enabled: bool = Field(default=True, description="是否启用控制台日志")


class LangSmithConfig(BaseModel):
    """LangSmith / LangChain 追踪（config.toml [langsmith]）"""

    enabled: bool = Field(default=False, description="是否启用 LangSmith 追踪上报")
    api_key: str = Field(
        default="",
        description="与 LANGCHAIN_API_KEY 相同（LangSmith）；写入配置文件时请限制文件权限",
    )
    project: str = Field(default="", description="可选，对应 LANGCHAIN_PROJECT")
    endpoint: str = Field(
        default="",
        description="可选 LANGCHAIN_ENDPOINT；留空则用 LangChain 默认（通常为 LangSmith 云端）",
    )


class AppConfig(BaseModel):
    """应用配置"""

    name: str = Field(default="SmartClaw", description="应用名称")
    version: str = Field(default="0.1.0", description="版本号")
    environment: str = Field(default="development", description="运行环境")
    agent_workspace_base: str = Field(
        default="",
        description="Agent 执行工作区根目录（DeepAgents / Skills），空则 ~/.smartclaw/workspace；可被环境变量 SMARTCLAW_AGENT_WORKSPACE_BASE 覆盖",
    )


class ExecutionConfig(BaseModel):
    """统一执行引擎与 Planner 编排配置"""

    use_unified_engine: bool = Field(
        default=True,
        description="经 UnifiedExecutionEngine 编排主循环（关闭时回退 runner 内联循环，便于应急回滚）",
    )
    deepagents_enabled: bool = Field(default=True, description="优先使用 DeepAgents/LangGraph 主路径")
    react_fallback_enabled: bool = Field(default=True, description="DeepAgents 失败时降级 ReAct")
    llm_tool_fallback_enabled: bool = Field(default=True, description="ReAct 失败时降级 LLM+工具轮次")
    planner_first_enabled: bool = Field(default=True, description="所有路径前先跑统一 Planner")
    emit_structured_trace: bool = Field(
        default=True,
        description="是否写入 ~/.smartclaw/execution-trace 审计 JSONL",
    )
    deepagents_recursion_limit: int = Field(
        default=128,
        ge=0,
        description=(
            "DeepAgents/LangGraph 单轮 invoke 的 recursion_limit（图步数上限，抑制模型-工具长时间空转）；"
            "0 表示不写入该限制（沿用 LangGraph 默认，通常极大）。"
            "环境变量 SMARTCLAW_DEEPAGENTS_RECURSION_LIMIT 非空时优先于此项。"
        ),
    )
    shell_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "全局 exec Shell 白名单（非空时与 Agent/workspace 合并后生效）。"
            "规则：前缀或首词精确；fnmatch（含 * ? [）；单独一项 * 或 ** 表示本层全放行（危险/Elevated 仍由 Tool Policy 处理）。"
            "空列表表示本层不追加规则。"
        ),
    )
    shell_allowlist_path: str = Field(
        default="",
        description="可选：每行一条规则（同 shell_allowlist 语义，支持 * 与 fnmatch），路径支持 ~ 展开",
    )


class PlatformAuthConfig(BaseModel):
    """平台鉴权（HTTP / 监控 / 租户映射）"""

    monitoring_require_auth: bool = Field(
        default=False,
        description="监控 /api/monitoring 下除 health 外是否要求 Bearer 或 JWT",
    )
    monitoring_bearer_token: str = Field(default="", description="监控 API 共享 Bearer（空则配合 require 时拒绝）")
    monitoring_jwt_enabled: bool = Field(default=False, description="监控接口优先使用 JWT（与 Bearer 二选一，JWT 优先）")
    monitoring_jwt_algorithm: str = Field(default="HS256", description="JWT 算法 HS256/RS256 等")
    monitoring_jwt_secret: str = Field(default="", description="HS256 对称密钥")
    monitoring_jwt_audience: str = Field(default="", description="JWT aud 校验，空则跳过")
    monitoring_jwt_issuer: str = Field(default="", description="JWT iss 校验，空则跳过")
    admin_require_auth: bool = Field(
        default=True,
        description=(
            "管理面 /api/admin/* 是否要求鉴权。**默认 True（secure by default）**："
            "即便监控鉴权关闭，租户开通/停用/删除等高危管理接口仍默认拒绝匿名访问。"
        ),
    )
    admin_bearer_token: str = Field(
        default="",
        description=(
            "管理面专用 Bearer（与监控凭证隔离，最小权限）。"
            "未设置且 admin_require_auth=True 时：若监控鉴权已开启则回退复用监控凭证，"
            "否则**关闭管理面**（拒绝一切），直到运维显式配置。"
        ),
    )
    feishu_webhook_secret: str = Field(
        default="",
        description="飞书 Webhook 可选共享密钥，非空时要求 query token= 或 X-SmartClaw-Webhook-Token",
    )
    feishu_decrypt_webhook: bool = Field(
        default=True,
        description="若 body 含 encrypt 且配置了 encrypt_key，则尝试 AES 解密",
    )
    tenant_default: str = Field(default="default", description="默认租户 ID")
    tenant_trust_header: bool = Field(
        default=False,
        description="为 True 时若请求带 X-SmartClaw-Tenant-Id 必须与解析出的租户一致",
    )
    tenant_by_app_id: dict[str, str] = Field(
        default_factory=dict,
        description="飞书 app_id → tenant_id 映射",
    )
    feishu_open_id_roles_by_tenant: dict[str, dict[str, list[str]]] = Field(
        default_factory=dict,
        description='租户 → 飞书 open_id → 角色列表；缺省用户可用键 \"*\"',
    )
    tool_required_roles_any: dict[str, list[str]] = Field(
        default_factory=dict,
        description="工具名 → 调用者需具备的任一角色（与解析角色求交集）；未列出的工具不拦截",
    )
    tenant_integration_env: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description="tenant_id → 键值，供工具执行时 get_tenant_integration_env() 读取（敏感值勿明文入库）",
    )
    audit_jsonl_enabled: bool = Field(
        default=True,
        description="是否写入 ~/.smartclaw/audit/*.jsonl 审计行",
    )
    webhook_replay_ttl_seconds: int = Field(
        default=0,
        description="Webhook 事件防重放窗口秒数，0 表示关闭",
    )


class PlatformConfig(BaseModel):
    """平台级开关（Event Bus 等）"""

    event_bus_enabled: bool = Field(default=False, description="是否启用 EventBus 写入与订阅")
    event_bus_dir: str = Field(default="", description="EventBus 根目录，空为 ~/.smartclaw/event-bus")


class TenantGovernanceLimit(BaseModel):
    """单租户限额覆盖；字段为 None 表示继承全局默认，0 表示该维度不限。"""

    rate_per_min: Optional[int] = Field(default=None, description="每分钟请求数上限")
    burst: Optional[int] = Field(default=None, description="令牌桶容量（突发额度），空则取 rate_per_min")
    daily_token_quota: Optional[int] = Field(default=None, description="每日 token 配额")
    max_concurrency: Optional[int] = Field(default=None, description="在途请求并发上限")


class GovernanceConfig(BaseModel):
    """租户资源治理（限流 / 配额 / 并发）。

    ``enabled=False`` 时治理层完全旁路，对既有行为零影响。
    每维度 0 表示不限；per_tenant 中的 None 表示继承同名默认值。
    """

    enabled: bool = Field(default=False, description="是否启用租户级治理（限流/配额/并发）")
    store: str = Field(default="memory", description="状态后端：memory（进程内）；redis 为后续增量")
    redis_url: str = Field(default="", description="store=redis 时的连接串（预留，见 progress.md P0-2）")
    default_rate_per_min: int = Field(default=0, ge=0, description="默认每分钟请求数上限，0=不限")
    default_burst: int = Field(default=0, ge=0, description="默认令牌桶容量，0=取 rate_per_min")
    default_daily_token_quota: int = Field(default=0, ge=0, description="默认每日 token 配额，0=不限")
    default_max_concurrency: int = Field(default=0, ge=0, description="默认并发上限，0=不限")
    per_tenant: dict[str, TenantGovernanceLimit] = Field(
        default_factory=dict, description="按 tenant_id 覆盖默认限额"
    )
    # ---- 用户级配额（纯增量；全为 0/空时整层惰性旁路，行为与历史一致）----
    # 「租户管钱（天花板）+ 用户管公平（防单人刷爆）」，判定自顶向下、任一超限即拒。
    default_user_rate_per_min: int = Field(
        default=0, ge=0, description="默认每用户每分钟请求数上限，0=不限"
    )
    default_user_daily_token_quota: int = Field(
        default=0, ge=0, description="默认每用户每日 token 配额，0=不限"
    )
    default_user_max_concurrency: int = Field(
        default=0, ge=0, description="默认每用户在途并发上限，0=不限"
    )
    per_user_by_tenant: dict[str, dict[str, TenantGovernanceLimit]] = Field(
        default_factory=dict,
        description="按 tenant_id -> 飞书 open_id 覆盖用户限额；None=继承默认，0=不限",
    )


class McpServerConfig(BaseModel):
    """MCP Server 注册配置。"""

    name: str = Field(default="", description="MCP server 命名空间；空则使用表名")
    transport: str = Field(default="sse", description="传输类型：sse/http/stdio（当前优先支持 sse/http）")
    url: str = Field(default="", description="SSE/HTTP MCP Server URL")
    command: str = Field(default="", description="stdio MCP server 启动命令（预留）")
    args: list[str] = Field(default_factory=list, description="stdio MCP server 启动参数（预留）")
    enabled: bool = Field(default=True, description="是否启用")
    timeout_ms: int = Field(default=30000, ge=1000, description="工具调用超时")
    risk_level: str = Field(default="low", description="该 server 默认工具风险等级")
    tenant_scope: str = Field(default="tenant", description="该 server 默认租户作用域")
    requires_confirmation: bool = Field(default=False, description="该 server 工具是否默认二次确认")
    required_roles_any: list[str] = Field(default_factory=list, description="该 server 默认角色要求（文档/审计元数据）")
    context_argument: str = Field(
        default="",
        description="可选：调用 MCP tool 时注入 SmartClaw 上下文的参数名，空则不注入",
    )


class McpConfig(BaseModel):
    """MCP 工具发现与注册配置。"""

    enabled: bool = Field(default=False, description="是否启用 MCP Tool Provider")
    servers: dict[str, McpServerConfig] = Field(default_factory=dict, description="MCP server 注册表")


class GlobalLLMConfig(BaseModel):
    """config.toml [llm]：全局默认大语言模型（Agent 未显式配置的字段以此回退）。"""

    provider: str = Field(default="openai", description="提供商别名，与 agent.json llm.provider 一致")
    model_name: str = Field(default="", description="模型 id；TOML 可用 model 或 model_name")
    base_url: str = Field(default="", description="OpenAI 兼容 API Base URL")
    api_key: str = Field(default="", description="API Key")
    max_tokens: int = Field(default=4096, ge=1)
    temperature: float = Field(default=0.7)
    top_p: float = Field(default=1.0)


class TenantConfig(BaseModel):
    """单个租户的默认配置。

    Tenant 级配置位于全局配置和 Agent 配置之间，用于隔离组织/团队级的
    模型、工作区、预算和策略。未设置的字段继续回退到全局默认值。
    """

    display_name: str = Field(default="", description="租户显示名")
    enabled: bool = Field(default=True, description="是否启用该租户")
    llm: GlobalLLMConfig = Field(default_factory=GlobalLLMConfig, description="租户默认 LLM")
    vision: dict[str, Any] = Field(default_factory=dict, description="租户视觉模型覆盖配置")
    memory: dict[str, Any] = Field(default_factory=dict, description="租户记忆系统覆盖配置")
    agent_workspace_base: str = Field(default="", description="该租户的 Agent 工作区根目录")


def global_llm_config_as_merge_dict(cfg: GlobalLLMConfig) -> dict[str, Any]:
    """将 GlobalLLMConfig 转为 merge_agent_llm_with_global 可用的 llm dict（仅含有效项）。"""
    out: dict[str, Any] = {}
    p = (cfg.provider or "").strip()
    if p:
        out["provider"] = p
    m = (cfg.model_name or "").strip()
    if m:
        out["model_name"] = m
    bu = (cfg.base_url or "").strip()
    if bu:
        out["base_url"] = bu
    ak = (cfg.api_key or "").strip()
    if ak:
        out["api_key"] = ak
    out["max_tokens"] = int(cfg.max_tokens)
    out["temperature"] = float(cfg.temperature)
    if float(cfg.top_p) != 1.0:
        out["top_p"] = float(cfg.top_p)
    return out


def tenant_llm_config_as_merge_dict(config: "Config", tenant_id: str | None) -> dict[str, Any]:
    """Return global + tenant LLM defaults as a merge-ready dict.

    The returned dictionary is intended to be passed as the fallback side of
    ``merge_agent_llm_with_global(agent_llm, fallback_llm)``. Agent-level values
    still win over tenant defaults, and tenant defaults win over global values.
    """
    from smartclaw.llm.base import merge_agent_llm_with_global
    from smartclaw.tenant import normalize_tenant_id

    base = global_llm_config_as_merge_dict(config.llm)
    tenant = config.tenants.get(normalize_tenant_id(tenant_id))
    if not tenant:
        return base
    return merge_agent_llm_with_global(
        global_llm_config_as_merge_dict(tenant.llm),
        base,
    )


def _merge_explicit_fields(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge override values while treating empty strings as inherited values."""
    out = dict(base)
    for key, value in (override or {}).items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        out[key] = value
    return out


class VisionConfig(BaseModel):
    """全局视觉理解配置"""
    enabled: bool = Field(default=False, description="是否启用全局视觉理解")
    model: str = Field(default="glm-4v", description="视觉模型名称")
    api_key: str = Field(default="", description="API Key")
    base_url: str = Field(
        default="https://open.bigmodel.cn/api/coding/paas/v4",
        description="API Base URL"
    )
    timeout: int = Field(default=60, description="超时时间（秒）")
    max_retries: int = Field(default=3, description="最大重试次数")


def tenant_vision_config(config: "Config", tenant_id: str | None) -> VisionConfig:
    """Return global vision config with optional tenant override applied."""
    from smartclaw.tenant import normalize_tenant_id

    data = config.vision.model_dump()
    tenant = config.tenants.get(normalize_tenant_id(tenant_id))
    if tenant and isinstance(tenant.vision, dict):
        data = _merge_explicit_fields(data, tenant.vision)
    return VisionConfig(**data)


class SkillEntryConfig(BaseModel):
    """单个 Skill 配置"""

    enabled: bool | None = Field(default=None, description="是否启用")
    api_key: str = Field(default="", description="Skill API Key")
    tenant_allowlist: list[str] = Field(default_factory=list, description="允许使用该 skill 的 tenant_id 列表")
    tenant_blocklist: list[str] = Field(default_factory=list, description="禁止使用该 skill 的 tenant_id 列表")
    env: dict[str, str] = Field(default_factory=dict, description="Skill 环境变量")
    config: dict[str, object] = Field(default_factory=dict, description="Skill 自定义配置")


class SkillsLoadConfig(BaseModel):
    """Skill 加载配置"""

    extra_dirs: list[str] = Field(default_factory=list, description="额外 skills 目录")
    watch: bool = Field(default=False, description="是否监听 skills 目录变化")
    watch_debounce_ms: int = Field(default=250, description="监听去抖毫秒")


class SkillsInstallConfig(BaseModel):
    """Skill 安装策略配置"""

    prefer_brew: bool = Field(default=True, description="是否优先 brew")
    node_manager: str = Field(default="npm", description="Node 包管理器")


class SkillsLimitsConfig(BaseModel):
    """Skill 加载与提示词限制"""

    max_skills_in_prompt: int = Field(default=80, description="提示词包含的最大 skill 数")
    max_skills_prompt_chars: int = Field(default=20000, description="提示词最大字符数")
    max_skill_file_bytes: int = Field(default=256000, description="SKILL.md 最大字节数")


class SkillsConfig(BaseModel):
    """Skills 全局配置"""

    allow_bundled: list[str] = Field(default_factory=list, description="允许的 bundled skills")
    allowlist: list[str] = Field(default_factory=list, description="显式允许的 skill_key 列表（为空表示全部）")
    blocked_risk_levels: list[str] = Field(default_factory=list, description="阻断风险等级，如 critical")
    require_approval_for: list[str] = Field(default_factory=lambda: ["high", "critical"], description="需要审批的风险等级")
    security_allowlist_skill_keys: list[str] = Field(default_factory=list, description="安全扫描白名单")
    tenant_allowlist_by_skill: dict[str, list[str]] = Field(default_factory=dict, description="skill_key → 允许租户")
    tenant_blocklist_by_skill: dict[str, list[str]] = Field(default_factory=dict, description="skill_key → 禁止租户")
    load: SkillsLoadConfig = Field(default_factory=SkillsLoadConfig)
    install: SkillsInstallConfig = Field(default_factory=SkillsInstallConfig)
    limits: SkillsLimitsConfig = Field(default_factory=SkillsLimitsConfig)
    entries: dict[str, SkillEntryConfig] = Field(default_factory=dict, description="按 skill key 配置")


class MemoryEmbeddingConfig(BaseModel):
    """记忆 embedding / hybrid 检索配置"""

    enabled: bool = Field(default=False, description="是否启用 embedding 语义检索")
    provider: str = Field(default="dashscope", description="embedding 提供商")
    model: str = Field(default="text-embedding-v4", description="embedding 模型")
    api_key: str = Field(default="", description="API Key；为空时读取 DASHSCOPE_API_KEY")
    base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        description="OpenAI 兼容 embedding API Base URL",
    )
    dimensions: int = Field(default=1024, ge=1, description="text-embedding-v4 输出维度")
    timeout_seconds: float = Field(default=30.0, gt=0, description="请求超时秒数")
    max_index_records: int = Field(default=500, ge=10, description="每次按需索引的最大记录数")
    vector_weight: float = Field(default=0.7, ge=0.0, le=1.0, description="向量分权重")
    text_weight: float = Field(default=0.3, ge=0.0, le=1.0, description="FTS 分权重")


class MemoryConfig(BaseModel):
    """记忆系统配置"""

    embedding: MemoryEmbeddingConfig = Field(default_factory=MemoryEmbeddingConfig)
    store: str = Field(
        default="sqlite",
        description="记忆数据面后端：sqlite（按 agent 分文件，默认）| postgres（共享库，多实例一致）",
    )
    postgres_dsn: str = Field(
        default="",
        description="store=postgres 时的连接串，如 postgresql://user:pwd@host:5432/smartclaw",
    )
    # ---- 长期记忆双层化（团队管沉淀 + 用户管个人偏好/归属）----
    # 默认开（secure-by-default）：个人偏好不再广播给同 Agent 的其他用户。
    # 个人层为空（仅模板）时不注入，故首轮行为与历史一致。
    enable_user_longterm: bool = Field(
        default=True,
        description="开启用户级长期记忆（个人偏好按飞书 open_id 隔离，不广播）；关闭=历史单层行为",
    )
    shared_longterm_max_chars: int = Field(
        default=4000,
        ge=0,
        description="注入上下文时[团队知识]的字符上限（头部保留+截断），0=不限",
    )
    user_longterm_max_chars: int = Field(
        default=2000,
        ge=0,
        description="注入上下文时[我的记忆]的字符上限（头部保留+截断），0=不限",
    )


def tenant_memory_embedding_config(
    config: "Config",
    tenant_id: str | None,
) -> MemoryEmbeddingConfig:
    """Return global memory.embedding with optional tenant override applied."""
    from smartclaw.tenant import normalize_tenant_id

    data = config.memory.embedding.model_dump()
    tenant = config.tenants.get(normalize_tenant_id(tenant_id))
    if tenant and isinstance(tenant.memory, dict):
        embedding = tenant.memory.get("embedding")
        if isinstance(embedding, dict):
            data = _merge_explicit_fields(data, embedding)
    return MemoryEmbeddingConfig(**data)


class Config(BaseModel):
    """完整配置模型"""

    smartclaw: AppConfig = Field(default_factory=AppConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    llm: GlobalLLMConfig = Field(default_factory=GlobalLLMConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    auth: PlatformAuthConfig = Field(default_factory=PlatformAuthConfig)
    platform: PlatformConfig = Field(default_factory=PlatformConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    langsmith: LangSmithConfig = Field(default_factory=LangSmithConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)
    tenants: dict[str, TenantConfig] = Field(default_factory=dict)

    class Config:
        arbitrary_types_allowed = True


# ==================== 配置加载器 ====================


class ConfigLoader:
    """配置加载器"""

    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path

    def load(self) -> Config:
        """加载配置"""
        # 查找配置文件
        config_file = self._find_config_file()

        if not config_file or not config_file.exists():
            warning("配置文件不存在，使用默认配置")
            cfg = Config()
            self._apply_langsmith_env(cfg)
            self._apply_governance_env(cfg)
            self._apply_memory_env(cfg)
            self._apply_admin_auth_env(cfg)
            self._apply_dotenv_overrides(cfg)
            return cfg

        # 解析配置
        try:
            with open(config_file, "rb") as f:
                raw_config = tomli.load(f)
        except Exception as e:
            warning(f"配置文件解析失败: {e}，使用默认配置")
            cfg = Config()
            self._apply_langsmith_env(cfg)
            self._apply_governance_env(cfg)
            self._apply_memory_env(cfg)
            self._apply_admin_auth_env(cfg)
            self._apply_dotenv_overrides(cfg)
            return cfg

        # 转换为嵌套对象
        cfg = self._parse_config(raw_config)
        self._apply_langsmith_env(cfg)
        self._apply_governance_env(cfg)
        self._apply_memory_env(cfg)
        self._apply_admin_auth_env(cfg)
        self._apply_dotenv_overrides(cfg)
        return cfg

    def _apply_langsmith_env(self, cfg: Config) -> None:
        from smartclaw.langsmith_env import apply_langsmith_env_from_config

        apply_langsmith_env_from_config(cfg)

    @staticmethod
    def _apply_governance_env(cfg: Config) -> None:
        """治理配置的环境变量覆盖（12-factor，便于容器/compose 注入）。

        - ``SMARTCLAW_GOVERNANCE_ENABLED``：``1/true/yes/on`` 启用，``0/false`` 关闭
        - ``SMARTCLAW_GOVERNANCE_STORE``：``memory`` | ``redis``
        - ``SMARTCLAW_GOVERNANCE_REDIS_URL``：如 ``redis://redis:6379/0``

        仅当对应环境变量存在时才覆盖；未设置则保留 config.toml / 默认值。
        """
        import os

        enabled = os.environ.get("SMARTCLAW_GOVERNANCE_ENABLED")
        if enabled is not None:
            cfg.governance.enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
        store = os.environ.get("SMARTCLAW_GOVERNANCE_STORE")
        if store is not None and store.strip():
            cfg.governance.store = store.strip()
        redis_url = os.environ.get("SMARTCLAW_GOVERNANCE_REDIS_URL")
        if redis_url is not None and redis_url.strip():
            cfg.governance.redis_url = redis_url.strip()

    @staticmethod
    def _apply_memory_env(cfg: Config) -> None:
        """记忆数据面后端的环境变量覆盖（12-factor，便于容器/compose 注入）。

        - ``SMARTCLAW_MEMORY_STORE``：``sqlite`` | ``postgres``
        - ``SMARTCLAW_MEMORY_POSTGRES_DSN``：如 ``postgresql://user:pwd@host:5432/smartclaw``
        """
        import os

        store = os.environ.get("SMARTCLAW_MEMORY_STORE")
        if store is not None and store.strip():
            cfg.memory.store = store.strip()
        dsn = os.environ.get("SMARTCLAW_MEMORY_POSTGRES_DSN")
        if dsn is not None and dsn.strip():
            cfg.memory.postgres_dsn = dsn.strip()

    @staticmethod
    def _apply_admin_auth_env(cfg: Config) -> None:
        """管理面鉴权的环境变量覆盖（12-factor，凭证经 env 注入，勿入库）。

        - ``SMARTCLAW_ADMIN_TOKEN``：管理面专用 Bearer（非空即覆盖）
        - ``SMARTCLAW_ADMIN_REQUIRE_AUTH``：``1/true/yes/on`` 开启，``0/false`` 关闭
        """
        import os

        token = os.environ.get("SMARTCLAW_ADMIN_TOKEN")
        if token is not None and token.strip():
            cfg.auth.admin_bearer_token = token.strip()
        require = os.environ.get("SMARTCLAW_ADMIN_REQUIRE_AUTH")
        if require is not None and require.strip():
            cfg.auth.admin_require_auth = require.strip().lower() in (
                "1", "true", "yes", "on"
            )

    @staticmethod
    def _apply_dotenv_overrides(cfg: Config) -> None:
        """将 .env 环境变量覆盖到 Config 对象（12-factor 配置注入）。

        环境变量命名约定与 .env.example 一致：
        - ``LLM_API_KEY`` → ``[llm] api_key``
        - ``FEISHU_APP_ID`` → ``[channels.feishu]`` 默认账号 ``app_id``
        - ``SERVER_PORT`` → ``[server] port``
        等等。

        仅当对应环境变量存在且非空时才覆盖；未设置则保留 config.toml / 默认值。
        布尔值支持：1/true/yes/on → True，0/false/no/off → False。
        """
        import os

        def _env(name: str) -> str | None:
            val = os.environ.get(name)
            if val is not None and val.strip():
                return val.strip()
            return None

        def _env_bool(name: str) -> bool | None:
            v = _env(name)
            if v is None:
                return None
            return v.lower() in ("1", "true", "yes", "on")

        def _env_int(name: str) -> int | None:
            v = _env(name)
            if v is None:
                return None
            try:
                return int(v)
            except ValueError:
                return None

        def _env_float(name: str) -> float | None:
            v = _env(name)
            if v is None:
                return None
            try:
                return float(v)
            except ValueError:
                return None

        # ── [llm] ──
        v = _env("LLM_PROVIDER")
        if v:
            cfg.llm.provider = v
        v = _env("LLM_API_KEY")
        if v:
            cfg.llm.api_key = v
        v = _env("LLM_MODEL")
        if v:
            cfg.llm.model_name = v
        v = _env("LLM_BASE_URL")
        if v:
            cfg.llm.base_url = v
        v = _env_int("LLM_MAX_TOKENS")
        if v is not None:
            cfg.llm.max_tokens = v
        v = _env_float("LLM_TEMPERATURE")
        if v is not None:
            cfg.llm.temperature = v

        # ── [vision] ──
        v = _env_bool("VISION_ENABLED")
        if v is not None:
            cfg.vision.enabled = v
        v = _env("VISION_API_KEY")
        if v:
            cfg.vision.api_key = v
        v = _env("VISION_MODEL")
        if v:
            cfg.vision.model = v
        v = _env("VISION_BASE_URL")
        if v:
            cfg.vision.base_url = v
        v = _env_int("VISION_TIMEOUT")
        if v is not None:
            cfg.vision.timeout = v
        v = _env_int("VISION_MAX_RETRIES")
        if v is not None:
            cfg.vision.max_retries = v

        # ── [channels.feishu] ──
        feishu_app_id = _env("FEISHU_APP_ID")
        feishu_app_secret = _env("FEISHU_APP_SECRET")
        if feishu_app_id or feishu_app_secret:
            if not cfg.channels.feishu.accounts:
                # 无已有账号 → 创建一个默认账号
                cfg.channels.feishu.accounts["default"] = FeishuAccountConfig(
                    app_id=feishu_app_id or "",
                    app_secret=feishu_app_secret or "",
                    name="default",
                    enabled=True,
                )
                cfg.channels.feishu.enabled = True
                if not cfg.channels.feishu.default:
                    cfg.channels.feishu.default = "default"
            else:
                default = cfg.channels.feishu.get_default_account()
                if default is not None:
                    if feishu_app_id:
                        default.app_id = feishu_app_id
                    if feishu_app_secret:
                        default.app_secret = feishu_app_secret
        v = _env("FEISHU_ENCRYPT_KEY")
        if v:
            if cfg.channels.feishu.accounts:
                default = cfg.channels.feishu.get_default_account()
                if default is not None:
                    default.encrypt_key = v
        v = _env("FEISHU_VERIFICATION_TOKEN")
        if v:
            if cfg.channels.feishu.accounts:
                default = cfg.channels.feishu.get_default_account()
                if default is not None:
                    default.verification_token = v

        # ── [channels.wecom] ──
        v = _env("WECOM_CORP_ID")
        if v:
            cfg.channels.wecom.corp_id = v
        v = _env("WECOM_APP_SECRET")
        if v:
            cfg.channels.wecom.secret = v
        v = _env("WECOM_AGENT_ID")
        if v:
            cfg.channels.wecom.agent_id = v

        # ── [sandbox] ──
        v = _env_bool("SANDBOX_ENABLED")
        if v is not None:
            cfg.sandbox.enabled = v
        v = _env_int("SANDBOX_MEMORY_MB")
        if v is not None:
            cfg.sandbox.memory_mb = v
        v = _env_int("SANDBOX_CPU_COUNT")
        if v is not None:
            cfg.sandbox.cpu_count = v

        # ── [server] ──
        v = _env("SERVER_HOST")
        if v:
            cfg.server.host = v
        v = _env_int("SERVER_PORT")
        if v is not None:
            cfg.server.port = v
        v = _env_int("SERVER_WORKERS")
        if v is not None:
            cfg.server.workers = v

        # ── [logging] ──
        v = _env("LOG_LEVEL")
        if v:
            cfg.logging.level = v.upper()
        v = _env("LOG_FILE")
        if v:
            cfg.logging.file_path = v

    def _find_config_file(self) -> Optional[Path]:
        """查找配置文件（与 ``paths.get_config_file`` 搜索顺序一致）。"""
        if self.config_path:
            return self.config_path

        for path in paths.get_config_search_paths():
            if path.exists():
                return path

        return None

    @staticmethod
    def _parse_global_llm_config(raw_llm: Any) -> GlobalLLMConfig:
        if not isinstance(raw_llm, dict):
            raw_llm = {}
        llm_model = str(
            raw_llm.get("model_name") or raw_llm.get("model") or ""
        ).strip()
        return GlobalLLMConfig(
            provider=str(raw_llm.get("provider", "openai") or "openai"),
            model_name=llm_model,
            base_url=str(raw_llm.get("base_url") or "").strip(),
            api_key=str(raw_llm.get("api_key") or "").strip(),
            max_tokens=int(raw_llm.get("max_tokens", 4096)),
            temperature=float(raw_llm.get("temperature", 0.7)),
            top_p=float(raw_llm.get("top_p", 1.0)),
        )

    @staticmethod
    def _parse_tenants_config(raw_tenants: Any) -> dict[str, TenantConfig]:
        if not isinstance(raw_tenants, dict):
            return {}
        tenants: dict[str, TenantConfig] = {}
        for tenant_id, tenant_raw in raw_tenants.items():
            key = str(tenant_id).strip()
            if not key or not isinstance(tenant_raw, dict):
                continue
            raw_llm = tenant_raw.get("llm")
            llm_cfg = (
                ConfigLoader._parse_global_llm_config(raw_llm)
                if isinstance(raw_llm, dict)
                else GlobalLLMConfig()
            )
            tenants[key] = TenantConfig(
                display_name=str(tenant_raw.get("display_name") or "").strip(),
                enabled=bool(tenant_raw.get("enabled", True)),
                llm=llm_cfg,
                vision=dict(tenant_raw.get("vision") or {})
                if isinstance(tenant_raw.get("vision"), dict)
                else {},
                memory=dict(tenant_raw.get("memory") or {})
                if isinstance(tenant_raw.get("memory"), dict)
                else {},
                agent_workspace_base=str(
                    tenant_raw.get("agent_workspace_base") or ""
                ).strip(),
            )
        return tenants

    def _parse_config(self, raw: dict) -> Config:
        """解析配置字典"""
        # 解析飞书多账号配置
        feishu_raw = raw.get("channels", {}).get("feishu", {})
        if not isinstance(feishu_raw, dict):
            feishu_raw = {}

        accounts_src = feishu_raw.get("accounts")
        if isinstance(accounts_src, list):
            accounts_raw = _feishu_accounts_list_to_dict(accounts_src)
        elif isinstance(accounts_src, dict):
            accounts_raw = {
                str(k): v for k, v in accounts_src.items() if isinstance(v, dict)
            }
        else:
            accounts_raw = {}
        flat_id = str(feishu_raw.get("app_id") or "").strip()
        flat_secret = str(feishu_raw.get("app_secret") or "").strip()
        if flat_id and flat_secret and not accounts_raw:
            accounts_raw["default"] = {
                "app_id": flat_id,
                "app_secret": flat_secret,
                "encrypt_key": str(feishu_raw.get("encrypt_key") or "").strip(),
                "verification_token": str(feishu_raw.get("verification_token") or "").strip(),
                "enabled": bool(feishu_raw.get("enabled", True)),
                "name": str(feishu_raw.get("name") or "default"),
            }

        feishu_accounts = {}
        for name, acc_data in accounts_raw.items():
            if isinstance(acc_data, dict):
                feishu_accounts[str(name)] = FeishuAccountConfig(**acc_data)

        default_name = str(feishu_raw.get("default") or "").strip()
        if not default_name and feishu_accounts:
            default_name = "default" if "default" in feishu_accounts else next(iter(feishu_accounts))

        feishu_enabled = feishu_raw.get("enabled", bool(feishu_accounts))

        feishu_config = FeishuChannelConfig(
            enabled=bool(feishu_enabled),
            default=default_name,
            accounts=feishu_accounts,
        )

        # 解析其他渠道
        wecom_raw = raw.get("channels", {}).get("wecom", {})
        wecom_config = WecomChannelConfig(**wecom_raw)

        channels_config = ChannelsConfig(
            feishu=feishu_config,
            wecom=wecom_config,
        )

        raw_ls = raw.get("langsmith") or {}
        if not isinstance(raw_ls, dict):
            raw_ls = {}
        langsmith_cfg = LangSmithConfig(
            enabled=bool(raw_ls.get("enabled", False)),
            api_key=str(raw_ls.get("api_key") or "").strip(),
            project=str(raw_ls.get("project") or "").strip(),
            endpoint=str(raw_ls.get("endpoint") or "").strip(),
        )

        raw_mcp = raw.get("mcp") or {}
        if not isinstance(raw_mcp, dict):
            raw_mcp = {}
        raw_mcp_servers = raw_mcp.get("servers") or {}
        if not isinstance(raw_mcp_servers, dict):
            raw_mcp_servers = {}
        mcp_servers: dict[str, McpServerConfig] = {}
        for key, server_raw in raw_mcp_servers.items():
            if not isinstance(server_raw, dict):
                continue
            server_key = str(key).strip()
            if not server_key:
                continue
            payload = dict(server_raw)
            payload["name"] = str(payload.get("name") or server_key)
            mcp_servers[server_key] = McpServerConfig(**payload)
        mcp_cfg = McpConfig(
            enabled=bool(raw_mcp.get("enabled", False)),
            servers=mcp_servers,
        )

        llm_global = self._parse_global_llm_config(raw.get("llm"))
        tenants_cfg = self._parse_tenants_config(raw.get("tenants"))

        # 构建完整配置
        return Config(
            smartclaw=AppConfig(**raw.get("smartclaw", {})),
            server=ServerConfig(**raw.get("server", {})),
            sandbox=SandboxConfig(**raw.get("sandbox", {})),
            channels=channels_config,
            logging=LoggingConfig(**raw.get("logging", {})),
            llm=llm_global,
            vision=VisionConfig(**raw.get("vision", {})),
            skills=SkillsConfig(**raw.get("skills", {})),
            execution=ExecutionConfig(**raw.get("execution", {})),
            auth=PlatformAuthConfig(**raw.get("auth", {})),
            platform=PlatformConfig(**raw.get("platform", {})),
            governance=GovernanceConfig(**raw.get("governance", {})),
            langsmith=langsmith_cfg,
            mcp=mcp_cfg,
            tenants=tenants_cfg,
            memory=MemoryConfig(**(raw.get("memory") or {})),
        )

    def save(self, config: Config, path: Optional[Path] = None) -> None:
        """保存配置"""
        config_file = path or self.config_path or paths.get_config_file()
        config_file.parent.mkdir(parents=True, exist_ok=True)

        raw = self._serialize_config(config)

        with open(config_file, "wb") as f:
            tomli_w.dump(raw, f)

    def _serialize_config(self, config: Config) -> dict:
        """序列化配置为字典"""
        result = {
            "smartclaw": config.smartclaw.model_dump(),
            "server": config.server.model_dump(),
            "sandbox": config.sandbox.model_dump(),
            "logging": config.logging.model_dump(),
            "llm": {
                "provider": config.llm.provider,
                "model": config.llm.model_name,
                "base_url": config.llm.base_url,
                "api_key": config.llm.api_key,
                "max_tokens": config.llm.max_tokens,
                "temperature": config.llm.temperature,
                "top_p": config.llm.top_p,
            },
            "vision": config.vision.model_dump(),
            "memory": config.memory.model_dump(exclude_none=True),
            "skills": config.skills.model_dump(exclude_none=True),
            "execution": config.execution.model_dump(exclude_none=True),
            "auth": config.auth.model_dump(exclude_none=True),
            "platform": config.platform.model_dump(exclude_none=True),
            "governance": config.governance.model_dump(exclude_none=True),
            "langsmith": config.langsmith.model_dump(),
            "mcp": config.mcp.model_dump(exclude_none=True),
            "tenants": {
                name: tenant.model_dump(exclude_none=True)
                for name, tenant in config.tenants.items()
            },
            "channels": {
                "feishu": {
                    "enabled": config.channels.feishu.enabled,
                    "default": config.channels.feishu.default,
                    "accounts": {},
                },
                "wecom": config.channels.wecom.model_dump(),
            },
        }

        # 序列化飞书账号
        for name, acc in config.channels.feishu.accounts.items():
            result["channels"]["feishu"]["accounts"][name] = acc.model_dump()

        return result


# ==================== 全局配置 ====================


_config: Optional[Config] = None


def get_config() -> Config:
    """获取全局配置实例"""
    global _config

    if _config is None:
        loader = ConfigLoader()
        _config = loader.load()

    return _config


def reload_config() -> Config:
    """重新加载配置"""
    global _config
    _config = None
    return get_config()


# ==================== 热重载集成 ====================


_watcher_started = False


def _on_config_file_changed(path: "Path"):
    """配置文件变化时的回调"""
    from smartclaw.console import info

    global _config
    info(f"配置文件已变更: {path}，重新加载配置...")
    _config = None

    # 重新加载
    loader = ConfigLoader()
    _config = loader.load()
    info("配置已重新加载")


def start_config_watcher():
    """
    启动配置热重载监听器

    自动监听默认配置路径，支持配置文件变更时自动重载。
    """
    global _watcher_started

    if _watcher_started:
        return

    

    from smartclaw.config.watcher import start_watcher
    from smartclaw.console import info

    # 监听所有默认配置路径
    watch_paths = [
        paths.CONFIG_DIR,
        Path.home() / ".smartclaw",
    ]

    # 只监听存在的路径
    existing_paths = [p for p in watch_paths if p.exists()]

    if existing_paths:
        start_watcher(
            watch_paths=existing_paths,
            extensions={".toml", ".yaml", ".yml", ".json"},
            callback=_on_config_file_changed,
        )
        _watcher_started = True
        info(f"配置热重载监听已启动，监听 {len(existing_paths)} 个路径")


def stop_config_watcher():
    """停止配置热重载监听器"""
    global _watcher_started

    if _watcher_started:
        from smartclaw.config.watcher import stop_watcher
        from smartclaw.console import info

        stop_watcher()
        _watcher_started = False
        info("配置热重载监听已停止")
