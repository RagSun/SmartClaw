# -*- coding: utf-8 -*-
"""完整 Agent 回合真实验证（命令行可复现，无需飞书真人）。

复用生产同一套加载逻辑（``server.load_agents`` / ``server.start_agents``），构造出
与线上一致的 AgentRunner，然后直接调用 ``runner.process_message(...)`` 发一条消息，
证明完整闭环：LLM(qwen-plus) → 自主调用 MCP 工具 factory__get_line_status → 汇总回答。

前置：
  - 工厂 MCP server 已在 127.0.0.1:18081 运行；
  - 已 `smartclaw mcp on` + `mcp add factory` + `agent mcp enable bot_dept_a factory`；
  - bot_dept_a 已配置可用模型（qwen-plus）。

用法：
    $env:PYTHONPATH="src"; $env:SMARTCLAW_HOME="D:\\hmw_course"
    python scripts/verify_agent_turn.py "2号线现在状态？"
"""

from __future__ import annotations

import asyncio
import sys

import smartclaw.server as server
from smartclaw.interfaces import ChannelType


async def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "2号线现在状态？"

    await server.load_agents()
    await server.start_agents()

    keys = list(server._agents.keys())
    print("已加载 Agent:", keys)

    key = next((k for k in keys if "bot_dept_a" in k), None)
    if key is None:
        print("未找到 bot_dept_a，请先创建并启用 factory MCP")
        return
    runner = server._agents[key]

    print(f"\n==================== 用户提问 ====================\n{question}\n")
    resp = await runner.process_message(
        user_id="ou_demo_user",
        channel=ChannelType.FEISHU,
        content=question,
        tenant_id="dept_a",
    )
    print("==================== Agent 回答 ====================")
    print(resp)

    try:
        await runner.stop()
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
