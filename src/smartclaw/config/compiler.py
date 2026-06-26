"""
配置编译器

将 Markdown 配置编译为高性能的 JSON 配置
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from smartclaw.config.markdown_parser import MarkdownParser
from smartclaw.console import error, info, success, warning
from smartclaw.tenant import DEFAULT_TENANT_ID, normalize_tenant_id, tenant_agent_key


class ConfigCompiler:
    """配置编译器（Markdown 自 **执行工作区** 读取，与 DeepAgents/skills 根目录对齐）"""

    def __init__(self, agents_dir: Path):
        """
        初始化编译器

        参数:
            agents_dir: agents 目录路径（仅存 agent.json 与 .compiled/）
        """
        self.agents_dir = agents_dir
        self.compiled_dir_name = ".compiled"

    def _split_agent_ref(self, agent_name: str) -> tuple[str, str]:
        """Split tenant/agent references while preserving old default names."""
        if "/" in agent_name:
            tenant, name = agent_name.split("/", 1)
            return normalize_tenant_id(tenant), name
        return DEFAULT_TENANT_ID, agent_name

    def _agent_dir_for_ref(self, agent_name: str) -> Path:
        """Return the on-disk agent directory for a default or tenant-scoped ref."""
        tenant, name = self._split_agent_ref(agent_name)
        if tenant == DEFAULT_TENANT_ID:
            return self.agents_dir / name
        return self.agents_dir / tenant / name

    def _agent_ref_for_dir(self, agent_dir: Path) -> str:
        """Return a logical tenant/agent ref for a discovered agent directory."""
        rel = agent_dir.relative_to(self.agents_dir)
        if len(rel.parts) >= 2:
            return tenant_agent_key(rel.parts[-1], rel.parts[-2])
        return rel.parts[0]

    def _workspace_root_for_agent(self, agent_dir_name: str, base_config: dict[str, Any]) -> Path:
        from smartclaw.agent.workspace import resolve_agent_workspace_dir
        from smartclaw.config.loader import get_config

        logical = str(base_config.get("name", agent_dir_name))
        return resolve_agent_workspace_dir(
            logical,
            base_config,
            get_config(),
            tenant_id=base_config.get("tenant_id"),
        )

    async def compile_agent(self, agent_name: str, force: bool = False) -> bool:
        """
        编译单个 agent 的配置

        参数:
            agent_name: Agent 名称
            force: 是否强制重新编译

        返回:
            是否编译成功
        """
        agent_dir = self._agent_dir_for_ref(agent_name)
        if not agent_dir.exists():
            error(f"Agent 目录不存在: {agent_dir}")
            return False

        # 检查是否需要重新编译
        if not force and not self._needs_recompile(agent_dir):
            info(f"Agent {agent_name} 配置未变化，跳过编译")
            return True

        info(f"开始编译 Agent: {agent_name}")

        # 加载基础配置（agent.json）
        base_config = self._load_base_config(agent_dir)
        if not base_config:
            error(f"加载基础配置失败: {agent_dir / 'agent.json'}")
            return False

        workspace_root = self._workspace_root_for_agent(agent_dir.name, base_config)

        # 解析 Markdown 配置（来自执行工作区根目录）
        parser = MarkdownParser(workspace_root)
        markdown_config = await self._parse_markdown_configs(parser)

        # 合并配置
        compiled_config = self._merge_configs(base_config, markdown_config, workspace_root)

        # 保存编译后的配置
        if not self._save_compiled_config(agent_dir, compiled_config):
            error(f"保存编译配置失败: {agent_dir}")
            return False

        # 保存哈希
        self._save_hashes(agent_dir)

        success(f"Agent {agent_name} 编译完成")
        return True

    async def compile_all(self, force: bool = False) -> dict[str, bool]:
        """
        编译所有 agent

        参数:
            force: 是否强制重新编译

        返回:
            Agent 名称到编译结果的映射
        """
        results = {}

        config_files = list(self.agents_dir.glob("*/agent.json")) + list(
            self.agents_dir.glob("*/*/agent.json")
        )
        for config_file in sorted(config_files):
            agent_dir = config_file.parent
            if agent_dir.name.startswith(".") or agent_dir.name == self.compiled_dir_name:
                continue
            agent_ref = self._agent_ref_for_dir(agent_dir)
            results[agent_ref] = await self.compile_agent(agent_ref, force)

        return results

    async def _parse_markdown_configs(self, parser: MarkdownParser) -> dict[str, Any]:
        """解析所有 Markdown 配置"""
        config = {}

        # 解析 SOUL.md
        soul_config = parser.parse_soul()
        if soul_config:
            config["soul"] = soul_config.to_dict()

        # 解析 TOOLS.md
        tools_config = parser.parse_tools()
        if tools_config:
            config["tools"] = tools_config.to_dict()

        # 解析 IDENTITY.md
        identity_config = parser.parse_identity()
        if identity_config:
            config["identity"] = identity_config.to_dict()

        # 解析 USER.md
        user_config = parser.parse_user()
        if user_config:
            config["user"] = user_config.to_dict()

        return config

    def _load_base_config(self, agent_dir: Path) -> Optional[dict[str, Any]]:
        """加载基础配置（agent.json）"""
        config_file = agent_dir / "agent.json"
        if not config_file.exists():
            return None

        try:
            return json.loads(config_file.read_text(encoding="utf-8"))
        except Exception as e:
            error(f"解析 agent.json 失败: {e}")
            return None

    def _merge_configs(
        self,
        base_config: dict[str, Any],
        markdown_config: dict[str, Any],
        workspace_root: Path,
    ) -> dict[str, Any]:
        """合并基础配置和 Markdown 配置"""
        merged = {
            "metadata": base_config,
            "compiled_at": datetime.now().isoformat(),
            "version": "1.0",
        }

        # 添加 Markdown 配置
        merged.update(markdown_config)

        # 生成系统提示（基于 Markdown 配置）
        merged["system_prompt"] = self._generate_system_prompt(markdown_config, workspace_root)

        return merged

    def _generate_system_prompt(
        self, markdown_config: dict[str, Any], workspace_root: Path
    ) -> str:
        """生成系统提示"""
        prompts = []

        # 加载 AGENTS.md（如果有）
        agents_md_path = workspace_root / "AGENTS.md"
        if agents_md_path.exists():
            try:
                agents_content = agents_md_path.read_text(encoding="utf-8")
                # 只取关键部分（前 2000 字符）
                if len(agents_content) > 2000:
                    agents_content = agents_content[:2000] + "\n...\n(内容过长已截断)"
                prompts.append(agents_content)
            except Exception:
                pass

        # 身份信息
        if "identity" in markdown_config:
            identity = markdown_config["identity"]
            prompts.append(f"你是 {identity.get('name', 'SmartClaw Agent')}")
            if identity.get("creature"):
                prompts.append(f"身份: {identity['creature']}")
            if identity.get("atmosphere"):
                prompts.append(f"氛围: {identity['atmosphere']}")
            if identity.get("introduction"):
                prompts.append(identity["introduction"])

        # 核心定位
        if "soul" in markdown_config:
            soul = markdown_config["soul"]
            if soul.get("core_positioning"):
                prompts.append(f"\n核心定位:\n{soul['core_positioning']}")

            if soul.get("core_capabilities"):
                prompts.append("\n核心能力:")
                for cap in soul["core_capabilities"]:
                    prompts.append(f"- {cap['category']}: {cap['description']}")

        # 工具使用规范
        if "tools" in markdown_config:
            tools = markdown_config["tools"]
            if tools.get("usage_principles"):
                prompts.append("\n工具使用原则:")
                for principle in tools["usage_principles"]:
                    prompts.append(f"- {principle}")

        # 用户信息
        if "user" in markdown_config:
            user = markdown_config["user"]
            if user.get("name"):
                prompts.append(f"\n用户: {user['name']}")
            if user.get("preferences"):
                prompts.append("用户偏好:")
                for key, value in user["preferences"].items():
                    prompts.append(f"- {key}: {value}")

        # 添加记忆和任务规划指南
        prompts.append("\n\n## 记忆规则")
        prompts.append("重要：你有义务记住用户告诉你的信息，并在适当时机更新记忆：")
        prompts.append("- 用户说「记住...」「以后都...」时，立即调用 update_memory")
        prompts.append("- 完成重要决策 → 记录到 memory")
        prompts.append("- 遇到错误但解决 → 记录解决方案")
        prompts.append("- 绝不能存储 API Keys、密码等敏感信息")
        prompts.append("- 开始任务前可以调用 read_memory 查看已记住的信息")

        prompts.append("\n\n## 任务规划指南")
        prompts.append("对于复杂任务，你应该使用 write_todos 工具创建和管理任务列表：")
        prompts.append("- 复杂多步骤任务（>=3步）必须使用 write_todos")
        prompts.append("- 任务状态：pending（待处理）、in_progress（进行中）、completed（已完成）")
        prompts.append("- 开始任务前立即标记为 in_progress")
        prompts.append("- 完成任务后立即标记为 completed")
        prompts.append(
            "- 长驻服务：平台常自动后台（返回 [bg] id=bg_xxx | log=.smartclaw_bg/…）；排错优先用 background_task 的 status/log/list/kill，未命中则命令末尾加 & 或 nohup"
        )

        return "\n".join(prompts)

    def _needs_recompile(self, agent_dir: Path) -> bool:
        """检查是否需要重新编译"""
        base = self._load_base_config(agent_dir)
        if not base:
            return True
        workspace_root = self._workspace_root_for_agent(agent_dir.name, base)

        compiled_dir = agent_dir / self.compiled_dir_name
        if not compiled_dir.exists():
            return True

        # 加载保存的哈希
        hashes_file = compiled_dir / "hashes.json"
        if not hashes_file.exists():
            return True

        try:
            saved_hashes = json.loads(hashes_file.read_text(encoding="utf-8"))
        except Exception:
            return True

        # 计算当前文件的哈希
        current_hashes = self._calculate_hashes(agent_dir, workspace_root)

        # 比较哈希
        return saved_hashes != current_hashes

    def _calculate_hashes(self, agent_dir: Path, workspace_root: Path) -> dict[str, str]:
        """计算配置文件的哈希（agent.json + 工作区根目录下标准 MD + shell 清单）"""
        hashes = {}

        # agent.json
        agent_json = agent_dir / "agent.json"
        if agent_json.exists():
            hashes["agent.json"] = self._file_hash(agent_json)

        for md_file in [
            "AGENTS.md",
            "SOUL.md",
            "TOOLS.md",
            "IDENTITY.md",
            "USER.md",
            "MEMORY.md",
            "BOOTSTRAP.md",
            "HEARTBEAT.md",
        ]:
            file_path = workspace_root / md_file
            if file_path.exists():
                hashes[f"ws/{md_file}"] = self._file_hash(file_path)

        shell_allow = workspace_root / "tools" / "SHELL_ALLOWLIST.txt"
        if shell_allow.exists():
            hashes["ws/tools/SHELL_ALLOWLIST.txt"] = self._file_hash(shell_allow)

        return hashes

    def _file_hash(self, file_path: Path) -> str:
        """计算文件哈希"""
        content = file_path.read_bytes()
        return hashlib.md5(content).hexdigest()

    def _save_compiled_config(self, agent_dir: Path, config: dict[str, Any]) -> bool:
        """保存编译后的配置"""
        compiled_dir = agent_dir / self.compiled_dir_name
        compiled_dir.mkdir(exist_ok=True)

        config_file = compiled_dir / "agent.compiled.json"

        try:
            config_file.write_text(
                json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return True
        except Exception as e:
            error(f"保存编译配置失败: {e}")
            return False

    def _save_hashes(self, agent_dir: Path) -> None:
        """保存文件哈希"""
        compiled_dir = agent_dir / self.compiled_dir_name
        compiled_dir.mkdir(exist_ok=True)

        base = self._load_base_config(agent_dir) or {}
        ws = self._workspace_root_for_agent(agent_dir.name, base)
        hashes = self._calculate_hashes(agent_dir, ws)
        hashes_file = compiled_dir / "hashes.json"

        try:
            hashes_file.write_text(
                json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            warning(f"保存哈希失败: {e}")


def create_incremental_compiler(agents_dir: Path):
    """
    创建增量编译器回调函数

    参数:
        agents_dir: agents 目录路径

    返回:
        回调函数，接收 Path 参数
    """
    import asyncio

    compiler = ConfigCompiler(agents_dir)

    def on_markdown_changed(path: Path) -> None:
        """Markdown 文件变化时的回调"""
        # 从路径推断 agent 名称
        # path 格式: /path/to/agents/{agent_name}/SOUL.md
        try:
            agent_name = path.parent.name
            info(f"检测到 Markdown 配置变化: {path}，重新编译 {agent_name}...")

            # 异步执行编译
            asyncio.run(compiler.compile_agent(agent_name, force=True))
            success(f"{agent_name} 重新编译完成")
        except Exception as e:
            error(f"增量编译失败: {e}")

    return on_markdown_changed
