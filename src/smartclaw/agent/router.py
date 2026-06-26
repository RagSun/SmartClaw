"""
Agent 路由器 v2.0

支持：
1. 私聊路由
2. 群聊单 Agent 路由
3. 群聊 @提及多 Agent 路由（v0.2）
4. 关键词路由（预留）
"""

import json
from smartclaw.console import error
import re
from pathlib import Path
from typing import Any, Optional


class AgentRouter:
    """
    Agent 路由器 v2.0

    支持多种路由模式：
    - 私聊：用户绑定 > 默认
    - 群聊单 Agent：群绑定 > 默认
    - 群聊多 Agent：@提及路由 + 群绑定 + 默认
    """

    def __init__(self) -> None:
        """初始化路由器"""
        self._bindings_dir = Path.home() / ".smartclaw" / "bindings"
        self._bindings_dir.mkdir(parents=True, exist_ok=True)

        # 绑定配置文件
        self._bindings_file = self._bindings_dir / "bindings.json"

        # 群组多 Agent 配置
        self._groups_config_file = self._bindings_dir / "groups.json"

        # 加载绑定配置
        self._bindings: dict[str, str] = {}
        self._group_configs: dict[str, dict] = {}
        self._load_bindings()
        self._load_group_configs()

    @staticmethod
    def _scoped_key(kind: str, raw_id: str, tenant_id: str = "default") -> str:
        """Build a tenant-aware binding key while preserving default semantics."""
        tenant = (tenant_id or "default").strip() or "default"
        if tenant == "default":
            return f"{kind}:{raw_id}"
        return f"{tenant}:{kind}:{raw_id}"

    def _load_bindings(self) -> None:
        """加载绑定配置"""
        if self._bindings_file.exists():
            try:
                with open(self._bindings_file, encoding="utf-8") as f:
                    self._bindings = json.load(f)
            except Exception:
                self._bindings = {}

    def _save_bindings(self) -> None:
        """保存绑定配置"""
        try:
            with open(self._bindings_file, "w", encoding="utf-8") as f:
                json.dump(self._bindings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            error(f"保存绑定配置失败: {e}")

    def _load_group_configs(self) -> None:
        """加载群组多 Agent 配置"""
        if self._groups_config_file.exists():
            try:
                with open(self._groups_config_file, encoding="utf-8") as f:
                    self._group_configs = json.load(f)
            except Exception:
                self._group_configs = {}

    def _save_group_configs(self) -> None:
        """保存群组多 Agent 配置"""
        try:
            with open(self._groups_config_file, "w", encoding="utf-8") as f:
                json.dump(self._group_configs, f, indent=2, ensure_ascii=False)
        except Exception as e:
            error(f"保存群组配置失败: {e}")

    def parse_mentions(self, content: str) -> list[str]:
        """
        解析消息中的 @提及

        参数:
            content: 消息内容

        返回:
            被 @ 的 Agent 名称列表
        """
        # 匹配 @显示名（含连字符与中文字符，如 @SmartClaw-部门A）
        pattern = r"@([-\w\u4e00-\u9fff]+)"
        mentions = re.findall(pattern, content)
        return mentions

    def route(
        self,
        user_id: str,
        chat_id: Optional[str] = None,
        is_group: bool = False,
        tenant_id: str = "default",
    ) -> str:
        """
        路由到对应的 Agent（单 Agent 模式）

        参数:
            user_id: 用户 ID
            chat_id: 会话 ID（群聊时为群 ID）
            is_group: 是否群聊

        返回:
            Agent 名称
        """
        # 群聊优先级：群绑定 > 默认
        if is_group and chat_id:
            group_key = self._scoped_key("group", chat_id, tenant_id)
            if group_key in self._bindings:
                return self._bindings[group_key]
            legacy_group_key = f"group:{chat_id}"
            if legacy_group_key in self._bindings:
                return self._bindings[legacy_group_key]

        # 私聊优先级：用户绑定 > 默认
        user_key = self._scoped_key("user", user_id, tenant_id)
        if user_key in self._bindings:
            return self._bindings[user_key]
        legacy_user_key = f"user:{user_id}"
        if legacy_user_key in self._bindings:
            return self._bindings[legacy_user_key]

        # 返回默认 Agent
        scoped_default = f"{tenant_id}:default" if tenant_id and tenant_id != "default" else "default"
        return self._bindings.get(scoped_default, self._bindings.get("default", "default"))

    def route_with_mentions(
        self,
        content: str,
        user_id: str,
        chat_id: Optional[str] = None,
        is_group: bool = False,
        tenant_id: str = "default",
        mention_tokens: Optional[list[str]] = None,
    ) -> list[str]:
        """
        路由到对应的 Agent（@提及多 Agent 模式）

        参数:
            content: 消息内容
            user_id: 用户 ID
            chat_id: 会话 ID（群聊时为群 ID）
            is_group: 是否群聊
            mention_tokens: 可选 — 与正文合并的 @ 片段（如飞书 SDK 结构化 mentions 中的显示名）

        返回:
            Agent 名称列表（按优先级排序）
        """
        agents: list[str] = []
        seen: set[str] = set()

        parsed = self.parse_mentions(content)
        if mention_tokens:
            merged: list[str] = []
            mer_seen: set[str] = set()
            for m in list(mention_tokens) + parsed:
                s = str(m).strip()
                if not s:
                    continue
                lk = s.lower()
                if lk not in mer_seen:
                    mer_seen.add(lk)
                    merged.append(s)
            mentions = merged
        else:
            mentions = parsed

        # 仅有飞书占位符、正文尚未展开成 @显示名 时，不得回落到会话 default（多 Bot 抢答）。
        if not mentions and content and re.search(r"<@_user_\d+>", content):
            mentions = ["__lark_mention_pending__"]

        for mention in mentions:
            agent = self._find_agent_by_name(mention)
            if agent and agent not in seen:
                agents.append(agent)
                seen.add(agent)

        if agents:
            return agents

        # 正文或结构化列表里出现过 @token，但无法映射到任何 Agent：不得退回「会话
        # default」，否则多 Worker 下只有 default 命中 mention_targets，表现为「@ 部门
        # 却只 default 回」。
        if mentions:
            return []

        default_agent = self.route(user_id, chat_id, is_group, tenant_id=tenant_id)
        if default_agent not in seen:
            agents.append(default_agent)

        return agents

    @staticmethod
    def _alias_keys_for_config(data: dict[str, Any], path_leaf: str, logical: str) -> set[str]:
        """归一化小写键：逻辑名、目录名、display_name、aliases、feishu.display_name。"""
        keys: set[str] = set()
        for v in (logical, path_leaf, data.get("display_name"), data.get("name")):
            if v and str(v).strip():
                keys.add(str(v).strip().lower())
        for a in data.get("aliases") or []:
            if a and str(a).strip():
                keys.add(str(a).strip().lower())
        fe = data.get("feishu")
        if isinstance(fe, dict) and fe.get("display_name"):
            keys.add(str(fe["display_name"]).strip().lower())
        return keys

    def _resolve_by_alias_exact(self, name_clean: str) -> tuple[Optional[str], bool]:
        """返回 (唯一逻辑名, 是否因多候选而歧义)。无匹配时 (None, False)。"""
        name_lower = name_clean.lower()
        from smartclaw.paths import get_agents_dirs

        hits: list[str] = []
        logical_seen: set[str] = set()
        for agents_dir in get_agents_dirs():
            if not agents_dir.exists():
                continue
            for cf in list(agents_dir.glob("*/agent.json")) + list(
                agents_dir.glob("*/*/agent.json")
            ):
                try:
                    rel = cf.relative_to(agents_dir).parts
                    path_leaf = rel[-2] if len(rel) >= 2 else ""
                    try:
                        data = json.loads(cf.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    logical = str(data.get("name") or path_leaf).strip() or path_leaf
                    keys = self._alias_keys_for_config(data, path_leaf, logical)
                    if name_lower in keys and logical not in logical_seen:
                        logical_seen.add(logical)
                        hits.append(logical)
                except Exception:
                    continue
        if len(hits) > 1:
            return None, True
        if len(hits) == 1:
            return hits[0], False
        return None, False

    def _find_agent_by_name(self, name: str) -> Optional[str]:
        """
        根据名称查找 Agent（精确优先，其次模糊；与 get_agents_dirs 布局一致）。

        参数:
            name: Agent 名称或 @后的名字

        返回:
            agent.json 中的逻辑 name；未找到或多义时返回 None
        """
        name_clean = (name or "").strip().lstrip("@")
        if not name_clean:
            return None

        from smartclaw.paths import get_agents_dirs

        roots = get_agents_dirs()

        def _read_logical_name(config_path: Path, fallback: str) -> str:
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
                return str(data.get("name") or fallback).strip() or fallback
            except Exception:
                return fallback

        for agents_dir in roots:
            if not agents_dir.exists():
                continue
            p_default = agents_dir / name_clean / "agent.json"
            if p_default.is_file():
                return _read_logical_name(p_default, name_clean)
            for tenant_dir in agents_dir.iterdir():
                if not tenant_dir.is_dir() or tenant_dir.name.startswith("."):
                    continue
                p_t = tenant_dir / name_clean / "agent.json"
                if p_t.is_file():
                    return _read_logical_name(p_t, name_clean)

        hit, ambiguous = self._resolve_by_alias_exact(name_clean)
        if ambiguous:
            return None
        if hit:
            return hit

        name_lower = name_clean.lower()
        matches: list[str] = []
        seen: set[str] = set()
        for agents_dir in roots:
            if not agents_dir.exists():
                continue
            for cf in list(agents_dir.glob("*/agent.json")) + list(
                agents_dir.glob("*/*/agent.json")
            ):
                try:
                    rel = cf.relative_to(agents_dir).parts
                    path_leaf = rel[-2] if len(rel) >= 2 else ""
                    logical = _read_logical_name(cf, path_leaf)
                    if not logical:
                        continue
                    ln = logical.lower()
                    pl = (path_leaf or "").lower()
                    hit = (
                        name_lower == ln
                        or name_lower == pl
                        or name_lower in ln
                        or ln in name_lower
                        or name_lower in pl
                        or pl in name_lower
                    )
                    if hit and logical not in seen:
                        seen.add(logical)
                        matches.append(logical)
                except Exception:
                    continue

        if len(matches) == 1:
            return matches[0]
        return None

    def route_by_keywords(
        self,
        content: str,
        user_id: str,
        chat_id: Optional[str] = None,
        is_group: bool = False,
    ) -> list[str]:
        """
        根据关键词路由（预留功能）

        参数:
            content: 消息内容
            user_id: 用户 ID
            chat_id: 会话 ID
            is_group: 是否群聊

        返回:
            Agent 名称列表
        """
        # 预留：关键词路由功能
        # 从群组配置中读取关键词配置
        if is_group and chat_id:
            group_config = self._group_configs.get(chat_id, {})
            keyword_routing = group_config.get("keyword_routing", {})

            content_lower = content.lower()
            matched_agents: list[str] = []
            seen: set[str] = set()

            for agent_name, keywords in keyword_routing.items():
                for keyword in keywords:
                    if keyword.lower() in content_lower:
                        if agent_name not in seen:
                            matched_agents.append(agent_name)
                            seen.add(agent_name)
                        break

            if matched_agents:
                return matched_agents

        # 回退到默认路由
        default_agent = self.route(user_id, chat_id, is_group)
        return [default_agent]

    # ==================== 绑定管理 ====================

    def bind_user(self, user_id: str, agent_name: str, tenant_id: str = "default") -> None:
        """绑定用户到 Agent"""
        self._bindings[self._scoped_key("user", user_id, tenant_id)] = agent_name
        self._save_bindings()

    def bind_group(self, chat_id: str, agent_name: str, tenant_id: str = "default") -> None:
        """绑定群聊到 Agent（单 Agent 模式）"""
        self._bindings[self._scoped_key("group", chat_id, tenant_id)] = agent_name
        self._save_bindings()

    def unbind_user(self, user_id: str, tenant_id: str = "default") -> None:
        """解绑用户"""
        self._bindings.pop(self._scoped_key("user", user_id, tenant_id), None)
        self._save_bindings()

    def unbind_group(self, chat_id: str, tenant_id: str = "default") -> None:
        """解绑群聊"""
        self._bindings.pop(self._scoped_key("group", chat_id, tenant_id), None)
        self._save_bindings()

    def set_default(self, agent_name: str, tenant_id: str = "default") -> None:
        """设置默认 Agent"""
        key = f"{tenant_id}:default" if tenant_id and tenant_id != "default" else "default"
        self._bindings[key] = agent_name
        self._save_bindings()

    def get_bindings(self) -> dict[str, str]:
        """获取所有绑定"""
        return self._bindings.copy()

    def clear_bindings(self) -> None:
        """清空所有绑定"""
        self._bindings.clear()
        self._save_bindings()

    # ==================== 群组多 Agent 配置 ====================

    def configure_group_multi_agent(
        self,
        chat_id: str,
        mode: str = "mention",
        agents: list[str] = None,
        default: str = None,
        keyword_routing: dict[str, list[str]] = None,
    ) -> None:
        """
        配置群组多 Agent

        参数:
            chat_id: 群 ID
            mode: 路由模式 ("mention" | "keyword" | "both")
            agents: 可用的 Agent 列表
            default: 默认 Agent
            keyword_routing: 关键词路由配置
        """
        self._group_configs[chat_id] = {
            "mode": mode,
            "agents": agents or [],
            "default": default,
            "keyword_routing": keyword_routing or {},
        }
        self._save_group_configs()

    def get_group_config(self, chat_id: str) -> Optional[dict]:
        """获取群组配置"""
        return self._group_configs.get(chat_id)

    def remove_group_config(self, chat_id: str) -> None:
        """移除群组配置"""
        self._group_configs.pop(chat_id, None)
        self._save_group_configs()

    def get_all_group_configs(self) -> dict[str, dict]:
        """获取所有群组配置"""
        return self._group_configs.copy()
