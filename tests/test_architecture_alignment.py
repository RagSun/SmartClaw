"""EventBus 与 Harness 对齐的冒烟测试。"""

import asyncio
import tempfile
from pathlib import Path

from smartclaw.core.event_bus import Event, EventBus, EventLevel, EventType


def test_event_bus_emit_execution_event():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "eb"
        bus = EventBus(base)

        async def run():
            await bus.emit(
                Event(
                    type=EventType.EXECUTION_TURN_START,
                    level=EventLevel.INFO,
                    agent_id="agent1",
                    session_key="s1",
                    run_id="r1",
                    data={"tenant_id": "default"},
                )
            )

        asyncio.run(run())
        log = base / "agent1.jsonl"
        assert log.exists()
        text = log.read_text(encoding="utf-8").strip()
        assert "execution.turn_start" in text
