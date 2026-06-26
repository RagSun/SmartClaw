"""
根据 config.toml [langsmith] 设置 LangChain / LangSmith 追踪所需的环境变量。

仅当配置中 enabled 且存在有效 api_key 时生效。已存在的环境变量不会被覆盖（便于在 shell / CI 中显式覆盖）。
与 SMARTCLAW_DEEPAGENTS_DEBUG 独立：前者把轨迹发到 LangSmith；后者控制本地控制台详细日志。
"""

from __future__ import annotations

import os
from typing import Optional

from smartclaw.config.loader import Config, LangSmithConfig


def apply_langsmith_env(langsmith: Optional[LangSmithConfig]) -> None:
    """按 langsmith 配置补齐 LANGCHAIN_*（不覆盖已有环境变量）。"""
    if langsmith is None:
        return
    if not langsmith.enabled:
        return
    key = (langsmith.api_key or "").strip()
    if not key:
        return
    if not (os.environ.get("LANGCHAIN_TRACING_V2") or "").strip():
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
    if not (os.environ.get("LANGCHAIN_API_KEY") or "").strip():
        os.environ["LANGCHAIN_API_KEY"] = key
    proj = (langsmith.project or "").strip()
    if proj and not (os.environ.get("LANGCHAIN_PROJECT") or "").strip():
        os.environ["LANGCHAIN_PROJECT"] = proj
    ep = (langsmith.endpoint or "").strip()
    if ep and not (os.environ.get("LANGCHAIN_ENDPOINT") or "").strip():
        os.environ["LANGCHAIN_ENDPOINT"] = ep


def apply_langsmith_env_from_config(config: Config) -> None:
    apply_langsmith_env(config.langsmith)
