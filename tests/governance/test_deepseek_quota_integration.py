"""真实 DeepSeek 集成测试：用真实 token 用量驱动租户配额闭环。

闭环验证：
  真实 DeepSeek 调用 → 真实 token 数 → governor.record_tokens → admit 因配额耗尽而拒绝。

运行方式（key 仅经环境变量注入，严禁写入代码/文档）：
  $env:SMARTCLAW_TEST_DEEPSEEK_API_KEY="sk-..."
  pytest tests/governance/test_deepseek_quota_integration.py -q -s

未设置该环境变量时自动跳过；不依赖 pytest-asyncio（内部用 asyncio.run）。
"""

from __future__ import annotations

import asyncio
import os

import pytest

_KEY = os.environ.get("SMARTCLAW_TEST_DEEPSEEK_API_KEY", "").strip()


@pytest.mark.skipif(not _KEY, reason="需设置 SMARTCLAW_TEST_DEEPSEEK_API_KEY 才运行真实 DeepSeek 测试")
def test_real_deepseek_usage_drives_tenant_quota():
    from smartclaw.config.loader import Config, GovernanceConfig
    from smartclaw.governance.governor import TenantGovernor
    from smartclaw.governance.store import InMemoryStore
    from smartclaw.llm.base import LLMConfig, LLMProvider, Message
    from smartclaw.llm.openai_compatible import OpenAICompatibleAdapter

    tenant = "acme"
    # 故意把配额设得极小（1 token），任何一次真实调用都会超过它。
    cfg = Config(governance=GovernanceConfig(enabled=True, default_daily_token_quota=1))
    gov = TenantGovernor(store=InMemoryStore(), config_provider=lambda: cfg)

    # 调用前：额度未用，放行
    assert gov.admit(tenant).allowed is True

    async def _call() -> int:
        adapter = OpenAICompatibleAdapter(
            LLMConfig(
                provider=LLMProvider.DEEPSEEK,
                model_name="deepseek-chat",
                api_key=_KEY,
                max_tokens=64,
                temperature=0.0,
            )
        )
        try:
            resp = await adapter.chat([Message.user("用一句话介绍你自己")])
        finally:
            await adapter.close()
        return resp.total_tokens

    total_tokens = asyncio.run(_call())
    print(f"\n[DeepSeek] 真实返回 total_tokens={total_tokens}")
    assert total_tokens > 0  # 确认确实是真实调用，拿到真实用量

    # 累计真实用量
    gov.record_tokens(tenant, total_tokens)

    # 配额=1，已消耗 > 1 → 现在应被拒，且拒因为 quota_exceeded
    adm = gov.admit(tenant)
    assert adm.allowed is False
    assert adm.reason == "quota_exceeded"

    # 快照应反映真实用量
    snap = gov.snapshot(tenant)
    assert snap["used_today"] == total_tokens
