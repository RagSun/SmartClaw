# -*- coding: utf-8 -*-
"""嵌入模型「完整闭环」真实验证（命令行可复现，跨平台）。

走项目自身代码路径，证明配置的 embedding 模型真实可用且已接入记忆检索主路径：
  L1  provider 直连：``embed_texts_openai_compatible`` 真实产出向量（维度=配置 dimensions）。
  L2  语义排序：用项目自带 ``MemoryManager._cosine`` 验证「语义相近 > 语义无关」。
  L3  端到端：``MemoryManager.add_message`` 写记忆 → ``search_memory_hybrid`` 检索
      （FTS 关键词 + 向量语义双路召回）→ 向量落 ``memory_embeddings`` 表。

前置：
  - 已 ``smartclaw config set memory.embedding.*``（enabled/provider/model/base_url/api_key/dimensions）。
  - ``SMARTCLAW_HOME`` 指向与配置同一棵安装树。

用法：
  $env:SMARTCLAW_HOME="D:\\hmw"; python scripts/verify_embedding.py
"""

from __future__ import annotations

import glob
import os
import sqlite3
from pathlib import Path

from smartclaw.config.loader import get_config, tenant_memory_embedding_config
from smartclaw.memory.embeddings import embed_texts_openai_compatible
from smartclaw.memory.manager import MemoryManager
from smartclaw.paths import INSTALL_ROOT


def main() -> int:
    tenant = os.environ.get("HMC_VERIFY_TENANT", "default")
    agent = os.environ.get("HMC_VERIFY_AGENT", "default")

    e = tenant_memory_embedding_config(get_config(), tenant)
    print(f"=== 配置(租户={tenant}) === enabled={e.enabled} provider={e.provider} "
          f"model={e.model} dim={e.dimensions} base_url={e.base_url}")
    if not e.enabled:
        print("[FAIL] memory.embedding.enabled=false，请先按课件 §6.2 配置 embedding")
        return 2
    if not (e.api_key or "").strip():
        print("[FAIL] 未配置 memory.embedding.api_key")
        return 2

    docs = ["2号线今天发生设备短暂停机，导致落后约180件", "周末我想去爬山，顺便喝杯热咖啡"]
    query = "哪条产线停机了，原因是什么"

    print("\n=== L1: provider 直连真实产出向量 ===")
    vd = embed_texts_openai_compatible(
        texts=docs, api_key=e.api_key, base_url=e.base_url,
        model=e.model, dimensions=e.dimensions, timeout_seconds=e.timeout_seconds,
    )
    qv = embed_texts_openai_compatible(
        texts=[query], api_key=e.api_key, base_url=e.base_url,
        model=e.model, dimensions=e.dimensions, timeout_seconds=e.timeout_seconds,
    )[0]
    print(f"返回向量数={len(vd)} 维度={len(vd[0])} (期望 {e.dimensions})")
    ok_dim = len(vd[0]) == int(e.dimensions)

    print("\n=== L2: 语义排序(项目自带余弦) ===")
    c0 = MemoryManager._cosine(qv, vd[0])
    c1 = MemoryManager._cosine(qv, vd[1])
    print(f"cos(查询, 文档A=2号线停机)={c0:.4f}")
    print(f"cos(查询, 文档B=爬山咖啡)  ={c1:.4f}")
    ok_sem = c0 > c1

    print("\n=== L3: 端到端(写记忆→语义检索→向量落库) ===")
    data_dir = INSTALL_ROOT / "data"
    sess = "emb_verify_sess"
    mm = MemoryManager(agent_id=agent, session_id=sess, channel="feishu",
                       user_id="ou_emb_user", data_dir=data_dir)
    mm.tenant_id = tenant
    mm.add_message("user", docs[0])
    mm.add_message("user", docs[1])
    hits = mm.search_memory_hybrid(
        "哪条产线停机了", limit=3, session_id=sess, tenant_id=tenant, user_id="ou_emb_user",
    )
    print(f"检索命中数={len(hits)}")
    top_body = ""
    for h in hits:
        body = str(h.get("body") or h.get("snippet") or h.get("content") or "")
        print(f"  HIT score={h.get('score')} | {body[:36]}")
        if not top_body:
            top_body = body
    ok_e2e = bool(hits) and ("停机" in top_body)

    rows = 0
    db_paths = sorted({Path(p).resolve() for p in
                       glob.glob(str(data_dir / "memory" / "**" / "*.db"), recursive=True)})
    for db in db_paths:
        con = sqlite3.connect(db)
        try:
            n = con.execute("select count(*) from memory_embeddings").fetchone()[0]
            if n:
                row = con.execute(
                    "select embedding_model, length(embedding_json) from memory_embeddings limit 1"
                ).fetchone()
                print(f"  向量落库: {os.path.basename(db)} -> rows={n} "
                      f"model={row[0]} json_len={row[1]}")
                rows += n
        except Exception:
            pass
        finally:
            con.close()
    ok_persist = rows > 0

    print("\n=== 判据 ===")
    print(f"L1 维度={e.dimensions}: {'OK' if ok_dim else 'FAIL'}")
    print(f"L2 语义 A>B: {'OK' if ok_sem else 'FAIL'}")
    print(f"L3 端到端命中停机文档: {'OK' if ok_e2e else 'FAIL'}")
    print(f"L3 向量落库 memory_embeddings: {'OK' if ok_persist else 'FAIL'}")
    all_ok = ok_dim and ok_sem and ok_e2e and ok_persist
    print("\n[EMBEDDING CLOSED-LOOP OK]" if all_ok else "\n[EMBEDDING CLOSED-LOOP FAILED]")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
