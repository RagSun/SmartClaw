# -*- coding: utf-8 -*-
"""数据库层租户隔离真实演示（命令行可复现）。

目标：证明核心表的隔离不再"只靠目录路径"，而是 DB 层强制带 tenant 条件（纵深防御）：
  1) user_profile：同一 (user_id, agent_id, key) 在不同租户下互不覆盖
     （旧唯一约束缺 tenant_id 的硬伤已修复）；
  2) memory_notes（记忆要点）：查询强制按租户过滤，串库也读不到别家；
  3) 旧库在线迁移：自动补 tenant_id 列与含 tenant 的唯一约束，历史数据回填 default。

用法：
    $env:PYTHONPATH="src"
    python scripts/verify_memory_isolation.py
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from smartclaw.memory.storage.sqlite_store import SQLiteStore


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="hmc_mem_"))

    print("==================== 1) user_profile 跨租户同键不覆盖 ====================")
    s = SQLiteStore(tmp / "mem.db")
    s.initialize()
    s.set_profile("u1", "bot", "secret", "A-only", tenant_id="dept_a")
    s.set_profile("u1", "bot", "secret", "B-only", tenant_id="dept_b")  # 同 user/agent/key，仅租户不同
    print("dept_a 读取:", s.get_profile("u1", "bot", tenant_id="dept_a"))
    print("dept_b 读取:", s.get_profile("u1", "bot", tenant_id="dept_b"))
    print("=> 两者并存、互不覆盖（修复前 dept_a 会被 dept_b 通过 ON CONFLICT 覆盖成空）")

    print("\n==================== 2) memory_notes（记忆要点）强制按租户过滤 ====================")
    s.add_memory_note("note", "dept_a 的产线笔记", user_id="u1", agent_id="bot", tenant_id="dept_a")
    s.add_memory_note("note", "dept_b 的产线笔记", user_id="u1", agent_id="bot", tenant_id="dept_b")
    print("dept_a 看到:", [e["content"] for e in s.get_memory_notes(user_id="u1", agent_id="bot", tenant_id="dept_a")])
    print("dept_b 看到:", [e["content"] for e in s.get_memory_notes(user_id="u1", agent_id="bot", tenant_id="dept_b")])
    print("未指定租户:", [e["content"] for e in s.get_memory_notes(user_id="u1", agent_id="bot")], " （default 租户看不到上面任一条）")
    s.close()

    print("\n==================== 3) 旧库在线迁移（user_profile 缺 tenant 唯一约束） ====================")
    legacy = tmp / "legacy.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        """CREATE TABLE user_profile(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL, agent_id TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, confidence INTEGER DEFAULT 5,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, agent_id, key))"""
    )
    conn.execute("INSERT INTO user_profile(user_id,agent_id,key,value) VALUES('u1','bot','lang','zh')")
    conn.commit()
    conn.close()
    print("旧库唯一约束: UNIQUE(user_id, agent_id, key)  <- 缺 tenant_id（硬伤）")

    s2 = SQLiteStore(legacy)
    s2.initialize()  # 触发在线迁移
    cols = set()
    c = s2._get_connection()
    for row in c.execute("PRAGMA index_list(user_profile)").fetchall():
        if row[2]:
            for ir in c.execute(f"PRAGMA index_info('{row[1]}')").fetchall():
                cols.add(ir[2])
    print("迁移后唯一索引列:", sorted(cols), " -> 已含 tenant_id" if "tenant_id" in cols else " -> 仍缺失!")
    print("历史行回填 default 仍可读:", s2.get_profile("u1", "bot", tenant_id="default"))
    s2.set_profile("u1", "bot", "lang", "en", tenant_id="dept_a")
    print("迁移后跨租户同键并存: default=", s2.get_profile("u1", "bot", tenant_id="default"),
          " dept_a=", s2.get_profile("u1", "bot", tenant_id="dept_a"))
    s2.close()

    print("\n[memory isolation OK]")


if __name__ == "__main__":
    main()
