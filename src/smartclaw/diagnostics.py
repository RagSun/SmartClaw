"""
闭环诊断：配置、HTTP 健康检查、LLM 探活、飞书凭证（可选深度校验）。

供 `smartclaw doctor` 与 `smartclaw llm-test` 复用，不依赖全局 LLM 注册表。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import httpx

import smartclaw.paths as paths
from smartclaw.pid_check import pid_is_running
from smartclaw.config.loader import Config, ConfigLoader
from smartclaw.llm.base import LLMConfig, LLMProvider, Message
from smartclaw.llm.providers import create_adapter


def discover_agent_json_path(agent_name: str, tenant_id: str) -> Optional[Path]:
    """Locate ``agent.json`` on disk（与 AgentManager._agent_config_path 布局一致）。"""
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id

    tenant = normalize_tenant_id(tenant_id)
    name = str(agent_name or "").strip()
    if not name:
        return None
    for ad in paths.get_agents_dirs():
        if not ad.exists():
            continue
        if tenant == DEFAULT_TENANT_ID:
            candidate = ad / name / "agent.json"
        else:
            candidate = ad / tenant / name / "agent.json"
        if candidate.is_file():
            return candidate
    return None


def resolve_llm_probe_target(
    agent_opt: Optional[str],
    *,
    tenant_opt: str,
) -> tuple[str, str]:
    """解析探活目标的 (tenant_id, agent_name)。

    - ``agent_opt`` 为 ``tenant/name`` 时以引用中的租户为准，忽略 ``tenant_opt``；
    - 否则使用 ``tenant_opt``（默认租户）与短名称拼路径。
    - 均未指定时使用 ``default`` 租户下的 ``default`` Agent。
    """
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

    raw = (agent_opt or "").strip()
    t_cli = normalize_tenant_id(tenant_opt or DEFAULT_TENANT_ID)

    if not raw:
        return normalize_tenant_id(t_cli), "default"

    if "/" in raw:
        t_part, a_part = raw.split("/", 1)
        tenant = normalize_tenant_id(t_part)
        agent_name = str(a_part or "").strip()
        if not agent_name:
            return tenant, ""
        return tenant, agent_name

    return t_cli, raw


def merged_llm_blob_for_feishu_style(
    agent_data: dict[str, Any],
    cfg: Config,
    *,
    tenant_id_for_merge: str,
    decrypt_llm_api_key_fn: Optional[Any] = None,
) -> dict[str, Any]:
    """与 ``feishu_ws_server`` 一致：租户层 ``[tenants.<id>.llm]`` + 全局后再叠 Agent."""
    from smartclaw.config.loader import global_llm_config_as_merge_dict, tenant_llm_config_as_merge_dict
    from smartclaw.llm.base import merge_agent_llm_with_global, normalize_agent_llm_dict
    from smartclaw.tenant import normalize_tenant_id

    llm_raw = dict(agent_data.get("llm") or {})
    api_key_raw = llm_raw.get("api_key", "")
    if api_key_raw and str(api_key_raw).startswith("ENC:"):
        decrypt = decrypt_llm_api_key_fn
        if decrypt is None:
            from smartclaw.agent.manager import AgentManager

            decrypt = AgentManager()._decrypt
        try:
            llm_raw["api_key"] = decrypt(str(api_key_raw)[4:])
        except Exception as ex:
            raise RuntimeError(
                f"无法解密 agent llm.api_key（密钥是否更换过？）: {ex}"
            ) from ex

    tenant_key = normalize_tenant_id(tenant_id_for_merge)
    g_llm = tenant_llm_config_as_merge_dict(cfg, tenant_key)
    return normalize_agent_llm_dict(merge_agent_llm_with_global(llm_raw, g_llm))


def llm_endpoint_model_alignment_issues(
    model_name: str,
    base_url: str,
) -> tuple[list[str], list[str]]:
    """根据模型名片段与网关 host 做启发式校验。

    Returns:
        ``(warnings, errors)`` — errors 供 doctor / 自检标红。
    """
    from urllib.parse import urlparse

    warnings: list[str] = []
    errors: list[str] = []

    bu_raw = str(base_url or "").strip()
    m = str(model_name or "").strip().lower()
    if not bu_raw:
        warnings.append("未配置 base_url，部分云厂商无法在本地推断线路")
        return warnings, errors

    try:
        host = urlparse(bu_raw).netloc.lower()
    except ValueError:
        host = ""

    dash_host = ("dashscope" in host) or (
        "aliyuncs.com" in host and "compatible" in bu_raw.lower()
    )
    bigmodel_host = "bigmodel.cn" in host
    deepseek_host = "deepseek" in host
    moon_host = "moonshot" in host

    qwen_style = bool(m.startswith("qwen")) or ("qwen-" in m)
    glm_style = ("glm-" in m or (m.startswith("glm") and not qwen_style)) or m.startswith(
        "chatglm"
    )
    deepseek_style = bool("deepseek" in m)
    kimi_style = "kimi" in m or "moonshot" in m or m.startswith("moonshot")

    # 交叉明显错误（避免等到 401 才察觉）
    if qwen_style and bigmodel_host:
        errors.append("模型名为通义 qwen，但 base_url 指向智谱(bigmodel)，请改用 dashscope compatible-mode/v1")

    if qwen_style and deepseek_host:
        errors.append("模型名为通义 qwen，但 base_url 指向 DeepSeek API")

    if glm_style and dash_host:
        errors.append("模型名像智谱 GLM，但 base_url 指向阿里云百炼，请对齐厂商")

    if glm_style and deepseek_host:
        errors.append("模型名像智谱 GLM，但 base_url 指向 DeepSeek")

    if deepseek_style and dash_host:
        errors.append("模型名包含 deepseek，但 base_url 指向百炼 DashScope")

    if deepseek_style and bigmodel_host:
        errors.append("模型名包含 deepseek，但 base_url 指向智谱 bigmodel")

    if deepseek_style and not deepseek_host:
        warnings.append("模型名暗示 DeepSeek，但 base_url host 不包含 deepseek，请核对")

    if kimi_style and not moon_host and not dash_host and not bigmodel_host:
        warnings.append("模型名暗示 Moonshot/Kimi，但 base_url 非 api.moonshot.cn，若使用代理请忽略")

    if qwen_style and not dash_host and not bigmodel_host:
        warnings.append("模型名为 qwen，但 base_url 非百炼 DashScope/智谱，若不使用这两家请忽略")

    if glm_style and not bigmodel_host:
        warnings.append("模型名为 GLM 系但 base_url 未指向 bigmodel.cn，若自建转发请忽略")

    return warnings, errors


def load_fresh_config() -> tuple[Config, Optional[Path]]:
    """加载配置（不使用 get_config() 缓存）。"""
    loader = ConfigLoader()
    path = loader._find_config_file()
    return loader.load(), path


def tcp_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def http_health_check(
    base: str,
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """请求 /health，期望 JSON 含 status healthy。"""
    url = base.rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return False, f"HTTP {r.status_code}"
            data = r.json()
            if data.get("status") == "healthy":
                return True, "ok"
            return False, f"unexpected body: {data!r}"
    except httpx.ConnectError as e:
        return False, f"连接失败: {e}"
    except Exception as e:
        return False, str(e)


def find_agent_json(agent_name: str) -> Optional[Path]:
    for ad in paths.get_agents_dirs():
        p = ad / agent_name / "agent.json"
        if p.exists():
            return p
    return None


def list_agent_names() -> list[str]:
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

    names: set[str] = set()
    for ad in paths.get_agents_dirs():
        if not ad.exists():
            continue
        for cf in list(ad.glob("*/agent.json")) + list(ad.glob("*/*/agent.json")):
            rel = cf.relative_to(ad).parts
            tenant = DEFAULT_TENANT_ID if len(rel) == 2 else normalize_tenant_id(rel[0])
            names.add(tenant_agent_key(rel[-2], tenant))
    return sorted(names)


def iter_agent_json_configs() -> list[tuple[str, dict[str, Any]]]:
    """
    扫描所有 agent.json，返回 (逻辑 name, 配置 dict) 列表。
    与 CLI agent list 一致：按 name 去重，优先级为 get_agents_dirs() 顺序。
    """
    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key

    for ad in paths.get_agents_dirs():
        if not ad.exists():
            continue
        for cf in list(ad.glob("*/agent.json")) + list(ad.glob("*/*/agent.json")):
            if not cf.is_file():
                continue
            try:
                data = json.loads(cf.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            rel = cf.relative_to(ad).parts
            tenant = DEFAULT_TENANT_ID if len(rel) == 2 else normalize_tenant_id(rel[0])
            name = str(data.get("name", rel[-2]))
            qname = tenant_agent_key(name, data.get("tenant_id", tenant))
            if qname in seen:
                continue
            seen.add(qname)
            out.append((qname, data))
    return out


def resolve_agent_name(
    preferred: Optional[str],
) -> tuple[Optional[str], Optional[Path]]:
    if preferred:
        p = find_agent_json(preferred)
        return (preferred, p)
    if find_agent_json("default"):
        return ("default", find_agent_json("default"))
    all_names = list_agent_names()
    if not all_names:
        return (None, None)
    n = all_names[0]
    return (n, find_agent_json(n))


def normalize_llm_provider(provider_raw: str) -> LLMProvider:
    """与 agent.json / set-llm 常见别名对齐。"""
    s = (provider_raw or "openai").strip().lower()
    aliases: dict[str, LLMProvider] = {
        "zhipu": LLMProvider.GLM,
        "bigmodel": LLMProvider.GLM,
        "glm": LLMProvider.GLM,
        "moonshot": LLMProvider.OPENAI,
        "kimi": LLMProvider.OPENAI,
    }
    if s in aliases:
        return aliases[s]
    try:
        return LLMProvider(s)
    except ValueError:
        return LLMProvider.OPENAI


def llm_config_from_agent_llm_blob(blob: dict[str, Any]) -> LLMConfig:
    provider = normalize_llm_provider(str(blob.get("provider", "openai")))
    return LLMConfig(
        provider=provider,
        model_name=str(blob.get("model_name", "gpt-4")),
        api_key=blob.get("api_key") or None,
        base_url=blob.get("base_url") or None,
        temperature=float(blob.get("temperature", 0.7)),
        max_tokens=int(blob.get("max_tokens", 2048)),
    )


async def probe_llm_chat(
    cfg: LLMConfig,
    user_text: str,
    *,
    max_tokens: int = 64,
    timeout: float = 120.0,
) -> tuple[bool, str]:
    """
    发起一次最小 chat 请求。
    成功 (True, 回复摘要)；失败 (False, 错误说明，不含密钥)。
    """
    adapter = create_adapter(cfg)
    try:
        if not adapter.is_available:
            return False, "LLM 不可用（例如缺少 API Key 或本地地址）"
        messages = [Message.user(user_text)]
        # 控制单次测试成本
        resp = await asyncio.wait_for(
            adapter.chat(messages, max_tokens=max_tokens),
            timeout=timeout,
        )
        text = (resp.content or "").strip()
        if not text:
            return False, "模型返回空内容"
        preview = text[:500] + ("…" if len(text) > 500 else "")
        meta = f"tokens≈{resp.total_tokens} latency_ms={resp.latency_ms}"
        return True, f"{preview}  ({meta})"
    except asyncio.TimeoutError:
        return False, "请求超时"
    except Exception as e:
        return False, _safe_llm_error(e)
    finally:
        close = getattr(adapter, "close", None)
        if close:
            try:
                await close()
            except Exception:
                pass


def _safe_llm_error(e: Exception) -> str:
    s = str(e)
    for needle in ("api_key", "Api-Key", "Authorization", "Bearer "):
        if needle.lower() in s.lower():
            return type(e).__name__ + ": 响应含敏感信息，已省略"
    if len(s) > 300:
        return type(e).__name__ + ": " + s[:300] + "…"
    return type(e).__name__ + ": " + s


async def probe_feishu_tenant_token(app_id: str, app_secret: str) -> tuple[bool, str]:
    """调用飞书获取 tenant_access_token（不写日志明文）。"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                url,
                json={"app_id": app_id, "app_secret": app_secret},
            )
            data = r.json()
            if data.get("code") == 0 and data.get("tenant_access_token"):
                exp = data.get("expire", "?")
                return True, f"token OK, expire={exp}s"
            msg = data.get("msg") or data.get("message") or r.text[:200]
            return False, f"code={data.get('code')!r} {msg}"
    except Exception as e:
        return False, str(e)


def check_kvm_environment() -> tuple[str, str]:
    """检测 KVM（/dev/kvm），返回 doctor 表格用的 (状态列, 详情列)。"""
    kvm_path = Path("/dev/kvm")
    if sys.platform == "win32":
        if kvm_path.exists() and kvm_path.is_char_device():
            return "[green]OK[/green]", "/dev/kvm 可用（常见于 WSL2）"
        return (
            "[yellow]不适用[/yellow]",
            "Windows 原生无 KVM；建议使用 sandbox.backend=docker，或在 WSL2 内运行",
        )
    if sys.platform == "darwin":
        if kvm_path.exists() and kvm_path.is_char_device():
            return "[green]OK[/green]", "/dev/kvm 可用"
        return (
            "[yellow]未检测到[/yellow]",
            "macOS 通常无 /dev/kvm；Firecracker 受限，建议 docker/process 沙箱",
        )
    # Linux 及其它类 Unix
    if kvm_path.exists() and kvm_path.is_char_device():
        return "[green]OK[/green]", "/dev/kvm 可用（硬件虚拟化）"
    if kvm_path.exists():
        return "[red]不可用[/red]", "/dev/kvm 存在但类型异常"
    return (
        "[yellow]未检测到[/yellow]",
        "无 /dev/kvm（虚拟机需嵌套虚拟化）；Firecracker 将降级，可改用 sandbox.backend=docker",
    )


def check_firecracker_binary() -> tuple[str, str]:
    """检测 firecracker 可执行文件是否在 PATH 中。"""
    fc_path = shutil.which("firecracker")
    if fc_path:
        return "[green]OK[/green]", fc_path
    return "[yellow]未安装[/yellow]", "可选；backend=firecracker 且无二进制时将降级"


def check_docker_daemon() -> tuple[str, str]:
    """检测 Docker CLI 与 daemon 是否可用。"""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return "[yellow]未安装[/yellow]", "未找到 docker；sandbox.backend=docker 时不可用"
    try:
        proc = subprocess.run(
            [docker_bin, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "[red]不可用[/red]", "docker info 超时（daemon 可能未启动）"
    except OSError as exc:
        return "[yellow]未知[/yellow]", str(exc)[:120]
    if proc.returncode == 0:
        ver = (proc.stdout or "").strip() or "unknown"
        return "[green]OK[/green]", f"daemon 可用, server={ver}"
    err = (proc.stderr or proc.stdout or "docker info 失败").strip().replace("\n", " ")
    return "[red]不可用[/red]", err[:160]


def sandbox_backend_doctor_check(cfg: Config) -> tuple[str, str, str]:
    """根据 config [sandbox].backend 与主机能力给出 doctor 一行。"""
    backend = (cfg.sandbox.backend or "firecracker").strip().lower()
    enabled = bool(cfg.sandbox.enabled)
    base = f"backend={backend}, enabled={enabled}"

    kvm_st, _ = check_kvm_environment()
    docker_st, docker_det = check_docker_daemon()
    fc_st, _ = check_firecracker_binary()
    kvm_ok = kvm_st.startswith("[green]")
    docker_ok = docker_st.startswith("[green]")
    fc_ok = fc_st.startswith("[green]")

    notes: list[str] = []
    if backend == "firecracker":
        if not kvm_ok:
            notes.append("无 KVM")
        if not fc_ok:
            notes.append("无 firecracker 命令")
        if notes:
            return (
                "配置: 沙箱后端",
                "[yellow]注意[/yellow]",
                f"{base}；{ '、'.join(notes) }，运行可能降级；可改为 sandbox.backend=docker",
            )
    elif backend == "docker":
        if not docker_ok:
            return (
                "配置: 沙箱后端",
                "[red]不匹配[/red]",
                f"{base}；{docker_det}",
            )
    elif backend not in ("process", "docker", "firecracker"):
        return (
            "配置: 沙箱后端",
            "[yellow]未知[/yellow]",
            f"{base}；合法值: docker, firecracker, process",
        )

    return ("配置: 沙箱后端", "[green]OK[/green]", base)


def feishu_config_summary(cfg: Config) -> tuple[str, str]:
    """(状态列, 详情列) 用于 doctor 表格。"""
    ch = cfg.channels.feishu
    if not ch.enabled:
        return "[yellow]未启用[/yellow]", "channels.feishu.enabled=false"
    acc = ch.get_default_account()
    if not acc:
        return "[red]无默认账号[/red]", "请配置 accounts 或扁平 app_id/app_secret"
    has_id = bool(str(acc.app_id or "").strip())
    has_sec = bool(str(acc.app_secret or "").strip())
    if has_id and has_sec:
        return "[green]凭证已填[/green]", f"default={ch.default or 'default'}"
    return "[red]凭证不全[/red]", "需要 app_id 与 app_secret"
