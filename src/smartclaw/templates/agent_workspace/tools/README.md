# tools — 工作区工具约定

本目录用于放 **与本 Agent 工作区绑定的工具包与策略**，与进程级内置 Tool 注册表（Python 实现）是不同层级：

| 层级 | 含义 |
|------|------|
| 内置 Tools | `ToolRegistry` 中的 exec / read_file / … |
| `workspace/tools/` | 工作区自定义工具包、约束说明、`SHELL_ALLOWLIST.txt` 等 |

编辑 **`SHELL_ALLOWLIST.txt`** 可为本 Agent 增加 **exec Shell 白名单**（与全局 `execution.shell_allowlist`、`agent.json` 的 `shell_allowlist` 合并；均为空时不启用白名单层）。

**规则摘要**：前缀或首词精确；`fnmatch`（如 `python*`）；单独一行 `*` / `**` 表示本层全放行（高危仍由上层 Tool Policy / 角色门禁处理）。详见源码 `smartclaw/agent/shell_allowlist.py` 模块文档字符串。

## 自定义工具包格式

若要让 Agent 创建“真正可调用”的工作区工具，不要放到 `skills/`，而是在本目录创建：

```text
tools/
  my_tool/
    tool.json
    handler.py
```

`tool.json` 示例：

```json
{
  "name": "my_tool",
  "description": "一句话说明这个工具做什么",
  "entry": "handler.py",
  "entry_function": "handler",
  "enabled": true,
  "risk_level": "high",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "description": "查询词"}
    },
    "required": ["query"]
  }
}
```

`handler.py` 中提供同名入口函数，例如：

```python
def handler(query: str) -> str:
    return f"result: {query}"
```

创建或修改后，调用 **`reload_workspace_tools`**，系统会把通过安全检查的 manifest 注册到 `ToolRegistry`。API key / secret 必须从环境变量或 tenant 配置读取，禁止硬编码在源码或 `SKILL.md` 中。
