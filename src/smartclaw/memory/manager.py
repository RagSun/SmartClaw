"""
记忆管理器 v2.0

工业级记忆系统：
- SQLite 存储：原子写入，崩溃恢复
- 自动摘要：消息 > 50 条自动压缩
- 智能事件提取：自动识别重要信息
- Token 预算管理：动态分配
"""

import json
import math
from pathlib import Path
from typing import Any, Optional

from smartclaw.console import debug
from smartclaw.config.loader import get_config, tenant_memory_embedding_config
from smartclaw.logging_utils import safe_preview
from smartclaw.memory.budget import ContextBudget
from smartclaw.memory.embeddings import EmbeddingError, embed_texts_openai_compatible
from smartclaw.memory.fts_query import fts5_phrase_query
from smartclaw.memory.storage.auto_summary import AutoSummary
from smartclaw.memory.storage.factory import create_memory_store
from smartclaw.memory.session import SessionMemory
from smartclaw.memory.daily import DailyMemory
from smartclaw.memory.longterm import LongTermMemory


class MemoryManager:
    """
    记忆管理器 v2.0

    整合 4 层记忆系统 + SQLite 存储 + 自动摘要

    数据流：
    消息 → SQLite →（回合结束）session_maintainer 按需 LLM 摘要 → summaries 表
    → get_context_for_llm 注入 [对话摘要]；记忆要点规则抽取 → memory_notes 表

    与 AgentRunner 集成时，对话逐条 transcript 以 SessionManager 为 SSOT；get_context_for_llm
    默认不再附带 SQLite 逐条消息（include_stored_transcript=False），仅注入长期记忆与
    [对话摘要]，避免双通道重复。若 SQLite 编译了 FTS5，可传 retrieval_query 注入本会话
    messages + 本用户 memory_notes（记忆要点）的 [记忆检索]（OpenClaw 式「当地纯文本检索」，无额外依赖）。

    持久化根目录 data_dir：memory/*.db、memory/promoted_hashes.sha256、longterm/MEMORY.md。
    运行时默认由 ``smartclaw.paths.default_memory_data_dir`` 指向
    ``SMARTCLAW_HOME/data/memory``（可用 SMARTCLAW_MEMORY_DATA_DIR 覆盖）。
    """

    def __init__(
        self,
        agent_id: str,
        session_id: str,
        channel: str,
        user_id: str,
        max_session_messages: int = 1000,
        daily_retention_days: int = 30,
        session_retention_days: int = 7,
        max_tokens: int = 128000,
        data_dir: Optional[Path] = None,
    ):
        self.agent_id = agent_id
        self.session_id = session_id
        self.channel = channel
        self.user_id = user_id
        self.tenant_id = "default"
        self.max_tokens = max_tokens

        # 数据目录
        if data_dir is None:
            data_dir = Path.home() / ".smartclaw" / "data"
        self.data_dir = Path(data_dir)

        # 记忆数据面后端（按配置选 sqlite / postgres，调用方零改动）。
        # SQLite 与长期 Markdown 同挂 data_dir（longterm/MEMORY.md + memory/*.db）。
        self._memory_subdir = self.data_dir / "memory"
        self._memory_subdir.mkdir(parents=True, exist_ok=True)
        try:
            _cfg = get_config()
            _store_kind = getattr(_cfg.memory, "store", "sqlite")
            _pg_dsn = getattr(_cfg.memory, "postgres_dsn", "")
            self._enable_user_longterm = bool(
                getattr(_cfg.memory, "enable_user_longterm", True)
            )
            self._shared_lt_max_chars = int(
                getattr(_cfg.memory, "shared_longterm_max_chars", 4000) or 0
            )
            self._user_lt_max_chars = int(
                getattr(_cfg.memory, "user_longterm_max_chars", 2000) or 0
            )
        except Exception:
            _store_kind, _pg_dsn = "sqlite", ""
            self._enable_user_longterm = True
            self._shared_lt_max_chars = 4000
            self._user_lt_max_chars = 2000
        self._store = create_memory_store(
            agent_id=agent_id,
            memory_subdir=self._memory_subdir,
            store=_store_kind,
            postgres_dsn=_pg_dsn,
        )

        # 自动摘要器
        self._auto_summary = AutoSummary(
            message_threshold=50,
            token_threshold=32000,
        )

        # Token 预算管理器
        self._budget = ContextBudget(max_tokens=max_tokens)

        # 多层记忆系统（延迟初始化，在 set_session 时创建）
        self._session_memory: Optional[Any] = None
        # bug 修复：之前未把 self.data_dir 透传，DailyMemory 始终落到 ~/.smartclaw/agents/...，
        # 违反 ``MemoryManager(data_dir=...)`` 的接口契约（测试与多租户隔离都依赖它）。
        # 这里改用与 SQLite 同源的 ``data_dir``；DailyMemory 自身仍允许通过 memory_dir
        # 直接覆盖，向后完全兼容。
        self._daily_memory = DailyMemory(
            agent_id=agent_id,
            memory_dir=self.data_dir / "agents" / agent_id / "memory",
            retention_days=daily_retention_days,
        )
        # 团队层（共享）：每 (租户, Agent) 一份，沉淀客观事实/团队共识。
        self._lt_dir = self.data_dir / "longterm"
        self._lt_dir.mkdir(parents=True, exist_ok=True)
        self._longterm_memory = LongTermMemory(
            agent_id=agent_id,
            memory_file=self._lt_dir / "MEMORY.md",
        )
        # 个人层（按飞书 open_id 隔离）：懒构造并缓存，因为同一 MemoryManager 实例
        # 会被 unified_execution 在不同用户间复用（每轮直接改 self.user_id）。
        self._longterm_user_cache: dict[str, LongTermMemory] = {}

    @property
    def fts_ready(self) -> bool:
        """当前 agent 的 SQLite 是否已启用 FTS5 记忆索引。"""
        return bool(getattr(self._store, "_fts_ready", False))

    # ----- 长期记忆个人层（纯增量；默认开，个人层空时不注入≈历史行为）----- #
    @staticmethod
    def _safe_user_part(user_id: str) -> str:
        """把飞书 open_id 规整成安全目录名（避免路径穿越/非法字符）。"""
        cleaned = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in (user_id or ""))
        return cleaned.strip("._") or "unknown_user"

    def user_longterm_file(self, user_id: str) -> Path:
        """某用户个人长期记忆文件路径（团队层同目录下的 users/<open_id>/MEMORY.md）。"""
        return self._lt_dir / "users" / self._safe_user_part(user_id) / "MEMORY.md"

    def user_longterm_for(self, user_id: str) -> Optional[LongTermMemory]:
        """按显式 user_id 懒解析+缓存个人层；未开启 / 无 user_id 时返回 None。

        显式传 user_id（而非依赖 self.user_id）以适配后台记忆维护：promotion 针对
        某个具体发言人，不应被实例上被复用的 self.user_id 串掉。
        """
        if not self._enable_user_longterm:
            return None
        uid = (user_id or "").strip()
        if not uid:
            return None
        cached = self._longterm_user_cache.get(uid)
        if cached is not None:
            return cached
        lt = LongTermMemory(agent_id=self.agent_id, memory_file=self.user_longterm_file(uid))
        self._longterm_user_cache[uid] = lt
        return lt

    def _user_longterm(self) -> Optional[LongTermMemory]:
        """按当前 self.user_id 懒解析个人层（注入上下文用）。"""
        return self.user_longterm_for(self.user_id or "")

    @staticmethod
    def _cap_head(text: str, max_chars: int) -> str:
        """头部保留 + 截断（0=不限）。长期记忆头部为核心定位/画像，价值更高。"""
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "\n…(长期记忆已按上限截断)"

    def delete_user_longterm(self, user_id: str) -> bool:
        """合规：删除某用户在本 (租户, Agent) 下的个人长期记忆文件。

        仅删个人层 Markdown；结构化的 memory_notes/profile/embeddings 已按 user_id 隔离，
        可由调用方按需另行清理。返回是否删除了文件。
        """
        uid = (user_id or "").strip()
        if not uid:
            return False
        self._longterm_user_cache.pop(uid, None)
        f = self.user_longterm_file(uid)
        try:
            if f.exists():
                f.unlink()
                return True
        except OSError:
            return False
        return False

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na <= 0 or nb <= 0:
            return 0.0
        return dot / (na * nb)

    def _embedding_cfg(self) -> Any:
        """Return tenant-aware memory embedding configuration."""
        return tenant_memory_embedding_config(get_config(), self.tenant_id)

    def _ensure_embeddings_for_records(
        self,
        records: list[dict[str, Any]],
        *,
        model: str,
        api_key: str,
        base_url: str,
        dimensions: int,
        timeout_seconds: float,
    ) -> None:
        missing: list[dict[str, Any]] = []
        for rec in records:
            body = (rec.get("body") or "").strip()
            if not body:
                continue
            existing = self._store.get_embedding(
                source_kind=rec["source_kind"],
                source_id=rec["source_id"],
                embedding_model=model,
            )
            if existing and existing.get("content_hash") == self._store.content_hash(body):
                continue
            missing.append(rec)

        if not missing:
            return

        # 保守分批，避免单次请求过大。
        batch_size = 16
        for i in range(0, len(missing), batch_size):
            batch = missing[i : i + batch_size]
            vectors = embed_texts_openai_compatible(
                texts=[str(x.get("body") or "") for x in batch],
                api_key=api_key,
                base_url=base_url,
                model=model,
                dimensions=dimensions,
                timeout_seconds=timeout_seconds,
            )
            for rec, vec in zip(batch, vectors):
                self._store.upsert_embedding(
                    source_kind=rec["source_kind"],
                    source_id=rec["source_id"],
                    tenant_id=rec.get("tenant_id") or self.tenant_id,
                    user_id=rec.get("user_id") or self.user_id or "",
                    agent_id=rec.get("agent_id") or self.agent_id,
                    content=str(rec.get("body") or ""),
                    embedding_model=model,
                    vector=vec,
                )

    def search_memory_hybrid(
        self,
        query: str,
        *,
        limit: int = 5,
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """FTS + embedding hybrid 检索，返回带 citation 的片段。"""
        q = (query or "").strip()
        if not q:
            return []

        sid = session_id or self.session_id
        tid = tenant_id or self.tenant_id
        uid = user_id or self.user_id
        if not sid:
            return []

        fts_hits: list[dict[str, Any]] = []
        mq = fts5_phrase_query(q)
        if mq:
            fts_hits = self._store.search_memory_fts(
                match_query=mq,
                session_id=sid,
                tenant_id=tid,
                user_id=uid or "",
                limit=max(limit * 4, 8),
                raw_query=q,
            )

        cfg = self._embedding_cfg()
        if not getattr(cfg, "enabled", False):
            return self._decorate_fts_hits(fts_hits, limit=limit)

        records = self._store.get_memory_source_records(
            session_id=sid,
            tenant_id=tid,
            user_id=uid or "",
            agent_id=self.agent_id,
            limit=int(getattr(cfg, "max_index_records", 500) or 500),
        )
        try:
            self._ensure_embeddings_for_records(
                records,
                model=cfg.model,
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                dimensions=cfg.dimensions,
                timeout_seconds=cfg.timeout_seconds,
            )
            q_vec = embed_texts_openai_compatible(
                texts=[q],
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                model=cfg.model,
                dimensions=cfg.dimensions,
                timeout_seconds=cfg.timeout_seconds,
            )[0]
        except (EmbeddingError, IndexError) as e:
            debug(f"[memory.hybrid] embedding unavailable, fallback fts: {e}")
            return self._decorate_fts_hits(fts_hits, limit=limit)

        by_key: dict[str, dict[str, Any]] = {}
        for h in fts_hits:
            key = f"{h.get('kind')}:{h.get('ref_id')}"
            by_key[key] = {
                "kind": h.get("kind"),
                "source_id": str(h.get("ref_id") or ""),
                "body": h.get("body") or "",
                "citation": f"{h.get('kind')}#{h.get('ref_id')}",
                "text_score": 1.0,
                "vector_score": 0.0,
            }

        for rec in records:
            emb = self._store.get_embedding(
                source_kind=rec["source_kind"],
                source_id=rec["source_id"],
                embedding_model=cfg.model,
            )
            if not emb:
                continue
            try:
                vec = json.loads(emb.get("embedding_json") or "[]")
            except json.JSONDecodeError:
                continue
            score = self._cosine(q_vec, [float(x) for x in vec])
            key = f"{rec['source_kind']}:{rec['source_id']}"
            row = by_key.setdefault(
                key,
                {
                    "kind": rec["source_kind"],
                    "source_id": rec["source_id"],
                    "body": rec.get("body") or "",
                    "citation": rec.get("citation") or key,
                    "text_score": 0.0,
                    "vector_score": 0.0,
                },
            )
            row["vector_score"] = max(float(row.get("vector_score") or 0.0), score)

        vw = float(getattr(cfg, "vector_weight", 0.7) or 0.7)
        tw = float(getattr(cfg, "text_weight", 0.3) or 0.3)
        results = []
        for row in by_key.values():
            row["score"] = vw * float(row.get("vector_score") or 0.0) + tw * float(row.get("text_score") or 0.0)
            results.append(row)
        results.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
        return results[:limit]

    @staticmethod
    def _decorate_fts_hits(hits: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for h in hits[:limit]:
            out.append({
                "kind": h.get("kind"),
                "source_id": str(h.get("ref_id") or ""),
                "body": h.get("body") or "",
                "citation": f"{h.get('kind')}#{h.get('ref_id')}",
                "score": h.get("score", 0),
                "text_score": h.get("score", 0),
                "vector_score": 0.0,
            })
        return out

    def add_message(
        self,
        role: str,
        content: str,
        user_id: Optional[str] = None,
    ) -> None:
        """
        添加消息到记忆

        参数:
            role: 角色 (user/assistant/system/tool)
            content: 消息内容
            user_id: 用户 ID
        """
        if not self.session_id:
            return

        # 写入 SQLite（滚动摘要由 session_maintainer 在回合结束后触发 LLM）
        self._store.add_message(
            session_id=self.session_id,
            role=role,
            content=content,
            tenant_id=self.tenant_id,
        )

    def get_context_for_llm(
        self,
        max_messages: int = 50,
        *,
        include_stored_transcript: bool = False,
        retrieval_query: Optional[str] = None,
        fts_top_k: int = 4,
    ) -> list[dict[str, str]]:
        """
        获取用于 LLM 的上下文（多层记忆整合）

        参数:
            max_messages: 最大消息数（仅当 include_stored_transcript=True 时用于 SQLite 拉取）
            include_stored_transcript: 是否附带 SQLite 逐条消息。默认 False：与 SessionManager
                并行时以 Session 为 transcript SSOT。独立脚本 / 仅 SQLite 场景可传 True。
            retrieval_query: 若提供且 FTS 可用，从本会话消息与 memory_notes（记忆要点）中检索 top-k 片段注入。
            fts_top_k: FTS 命中条数上限。

        返回:
            消息历史列表 [{"role": "...", "content": "..."}]
        """
        if not self.session_id:
            return []

        context = []

        # 1. 长期记忆 —— 双层：团队层（共享，客观事实/共识）+ 个人层（按 user 隔离，偏好/归属）。
        #    顺序「团队在前、个人在后」=个人可覆盖风格类；空层（仅模板，<400 字）不注入，
        #    故个人层默认开但首轮无内容时行为与历史一致。
        try:
            shared = self._longterm_memory.get_content()
            if shared and len(shared.strip()) > 400:
                context.append({
                    "role": "system",
                    "content": f"[团队知识] {self._cap_head(shared, self._shared_lt_max_chars)}"
                })
        except Exception:
            pass

        try:
            user_lt = self._user_longterm()
            if user_lt is not None:
                personal = user_lt.get_content()
                if personal and len(personal.strip()) > 400:
                    rule = (
                        "（记忆优先级：以下为当前用户的个人记忆；与[团队知识]冲突时，"
                        "事实/合规以团队为准，风格/格式/称呼/通知渠道以个人为准）\n"
                    )
                    context.append({
                        "role": "system",
                        "content": "[我的记忆] " + rule
                        + self._cap_head(personal, self._user_lt_max_chars)
                    })
        except Exception:
            pass

        # 2. 检查是否有摘要
        latest_summary = self._store.get_latest_summary(self.session_id, tenant_id=self.tenant_id)
        if latest_summary:
            context.append({
                "role": "system",
                "content": f"[对话摘要] {latest_summary['summary']}"
            })

        hits: list[dict[str, Any]] = []
        mq = fts5_phrase_query(retrieval_query) if retrieval_query else None
        if retrieval_query and not mq:
            debug(
                "[memory.ctx] retrieval skip | phrase too short | "
                f"raw_preview={safe_preview(retrieval_query or '', 96)!r} "
                f"session={self.session_id!r}"
            )
        if retrieval_query:
            hits = self.search_memory_hybrid(
                retrieval_query,
                limit=max(1, fts_top_k),
                session_id=self.session_id,
                tenant_id=self.tenant_id,
                user_id=self.user_id or "",
            )
            if hits:
                chunks: list[str] = []
                for h in hits:
                    role = (h.get("kind") or "?")[:12]
                    body = (h.get("body") or "").strip().replace("\n", " ")
                    if len(body) > 480:
                        body = body[:480] + "…"
                    cite = h.get("citation") or ""
                    chunks.append(f"- [{role}] {body}" + (f" (source: {cite})" if cite else ""))
                context.append({
                    "role": "system",
                    "content": "[记忆检索]\n" + "\n".join(chunks),
                })

        if retrieval_query:
            injected = any(
                (c.get("content") or "").startswith("[记忆检索]")
                for c in context
            )
            debug(
                "[memory.ctx] retrieval summary | "
                f"fts_ready={self.fts_ready} mq_built={mq is not None} "
                f"hits={len(hits)} injected_retrieval={injected} "
                f"session={self.session_id!r} tenant={self.tenant_id!r} "
                f"user_id={(self.user_id or '')!r} "
                f"raw_preview={safe_preview(retrieval_query or '', 96)!r}"
            )

        # 3. 获取消息（限制数量）— 可与 Session 重复，按需关闭
        if include_stored_transcript:
            messages = self._store.get_messages(self.session_id, limit=max_messages, tenant_id=self.tenant_id)

            for msg in messages:
                role = msg.get("role", "user")
                if role in ("user", "assistant", "system"):
                    context.append({
                        "role": role,
                        "content": msg.get("content", ""),
                    })

        return context

    def extract_important_note(
        self,
        content: str,
        note_kind: str = "重要记录",
        importance: int = 5,
    ) -> None:
        """
        提取重要记忆要点到长期记忆

        参数:
            content: 要点内容
            note_kind: 要点类别
            importance: 重要性 (1-10)
        """
        self._store.add_memory_note(
            note_kind=note_kind,
            content=content,
            importance=importance,
            user_id=self.user_id,
            agent_id=self.agent_id,
            tenant_id=self.tenant_id,
        )

    def auto_extract_memory_notes(
        self,
        *,
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        user_id: Optional[str] = None,
        messages_limit: int = 100,
    ) -> int:
        """
        自动从消息中提取记忆要点

        参数:
            messages_limit: 参与规则抽取的最近消息条数（群聊宜用小窗口，如 8）。

        返回:
            新写入 memory_notes 表的条数（dedupe 按规范化内容哈希）
        """
        sid = session_id or self.session_id
        tid = tenant_id or self.tenant_id
        uid = user_id or self.user_id
        if not sid:
            return 0

        messages = self._store.get_messages(
            sid, limit=messages_limit, tenant_id=tid
        )
        notes = self._auto_summary.extract_memory_notes(messages)

        inserted = 0
        for note in notes:
            row_id = self._store.add_memory_note(
                note_kind=note["kind"],
                content=note["content"],
                importance=note["importance"],
                user_id=uid,
                agent_id=self.agent_id,
                dedupe=True,
                tenant_id=tid,
            )
            if row_id:
                inserted += 1

        return inserted

    def create_summary(
        self,
        summary_content: str,
        *,
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> None:
        """
        创建会话摘要

        参数:
            summary_content: 摘要内容
            session_id / tenant_id: 可选；用于后台任务避免与共享 MemoryManager 竞态
        """
        sid = session_id or self.session_id
        tid = tenant_id or self.tenant_id
        if not sid:
            return

        msg_count = self._store.get_message_count(sid, tenant_id=tid)
        self._store.add_summary(
            session_id=sid,
            summary=summary_content,
            original_count=msg_count,
            summary_type="auto",
            tenant_id=tid,
        )

    def get_user_profile(self) -> dict[str, str]:
        """获取用户画像"""
        return self._store.get_profile(
            self.user_id, self.agent_id, tenant_id=self.tenant_id
        )

    def update_user_profile(
        self,
        key: str,
        value: str,
        confidence: int = 5,
    ) -> None:
        """更新用户画像"""
        self._store.set_profile(
            user_id=self.user_id,
            agent_id=self.agent_id,
            key=key,
            value=value,
            confidence=confidence,
            tenant_id=self.tenant_id,
        )

    def get_usage_report(self) -> dict[str, Any]:
        """获取记忆使用报告"""
        msg_count = 0
        if self.session_id:
            msg_count = self._store.get_message_count(self.session_id, tenant_id=self.tenant_id)

        latest_summary = None
        if self.session_id:
            latest_summary = self._store.get_latest_summary(
                self.session_id, tenant_id=self.tenant_id
            )

        return {
            "session_id": self.session_id,
            "message_count": msg_count,
            "is_summarized": latest_summary is not None,
            "latest_summary_at": (latest_summary or {}).get("created_at"),
            "budget_usage": self._budget.usage_stats,
        }

    def cleanup_expired(self) -> dict[str, int]:
        """清理过期数据（未来实现）"""
        return {"cleaned": 0}

    def session_lock(
        self,
        *,
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ):
        """返回会话级临界区上下文管理器（防多实例交叉写同一对话）。

        - PostgreSQL 后端：``pg_advisory_lock``，跨实例串行化同一会话、不同会话并行。
        - SQLite 后端：单文件单进程已天然串行，返回 no-op 上下文。
        把对话「读历史→推理→追加消息→摘要」整段包进来即可获得多实例安全。
        """
        sid = session_id or self.session_id
        tid = tenant_id or self.tenant_id
        locker = getattr(self._store, "session_lock", None)
        if callable(locker) and sid:
            return locker(tid, sid)
        from contextlib import nullcontext

        return nullcontext()

    def close(self) -> None:
        """关闭底层存储连接（测试/进程退出时释放资源）。"""
        self._store.close()

    @property
    def promoted_hashes_file(self) -> Path:
        """已晋升到 MEMORY.md 的内容指纹（sha256 十六进制，每行一条）。"""
        return self._memory_subdir / "promoted_hashes.sha256"
