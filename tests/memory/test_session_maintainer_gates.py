"""session_maintainer 闸门与维护行为单测。"""

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from smartclaw.memory.manager import MemoryManager
from smartclaw.memory.session_maintainer import run_post_turn_memory_maintenance


def test_maint_extract_runs_when_load_history_false_summary_skipped():
    """群聊 load_history=False：不调用 LLM 摘要，但仍执行周期事件抽取。"""

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mm = MemoryManager(
                agent_id="gate_agent",
                session_id="s_gate",
                channel="feishu",
                user_id="u_gate",
                data_dir=Path(tmp),
            )
            mm.tenant_id = "default"
            for _i in range(11):
                mm.add_message("user", "ping")
                mm.add_message("assistant", "pong")
            mm.add_message("user", "记住我喜欢闸门测试")
            mm.add_message("assistant", "好的")

            assert mm._store.get_message_count("s_gate", tenant_id="default") % 12 == 0

            with patch(
                "smartclaw.memory.session_maintainer.maybe_refresh_session_summary_with_llm",
                new_callable=AsyncMock,
            ) as mock_sum, patch(
                "smartclaw.memory.session_maintainer.promote_notes_to_longterm_md",
                return_value=0,
            ):
                await run_post_turn_memory_maintenance(
                    memory_manager=mm,
                    adapter_name="some-adapter",
                    agent_id="gate_agent",
                    session_id="s_gate",
                    tenant_id="default",
                    user_id="u_gate",
                    load_history=False,
                )
                mock_sum.assert_not_called()

            ev = mm._store.get_memory_notes(user_id="u_gate", agent_id="gate_agent", limit=50)
            assert len(ev) >= 1
            mm.close()

    asyncio.run(_run())


def test_maint_summary_called_when_load_history_true():
    """私聊 load_history=True 且 adapter 存在：会尝试摘要（mock，不连真 LLM）。"""

    async def _run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mm = MemoryManager(
                agent_id="gate_agent2",
                session_id="s2",
                channel="feishu",
                user_id="u2",
                data_dir=Path(tmp),
            )
            with patch(
                "smartclaw.memory.session_maintainer.maybe_refresh_session_summary_with_llm",
                new_callable=AsyncMock,
                return_value=False,
            ) as mock_sum:
                await run_post_turn_memory_maintenance(
                    memory_manager=mm,
                    adapter_name="ad",
                    agent_id="gate_agent2",
                    session_id="s2",
                    tenant_id="default",
                    user_id="u2",
                    load_history=True,
                )
                mock_sum.assert_awaited_once()
            mm.close()

    asyncio.run(_run())
