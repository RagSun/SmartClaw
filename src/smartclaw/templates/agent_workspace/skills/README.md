# skills — Agent 专属 Skills

在此目录下为每个 Skill 建子文件夹，并在子文件夹内放置 **`SKILL.md`**（含 frontmatter）。
与全局 `~/.smartclaw/skills`、配置 `skills.load.extra_dirs` 中的条目合并后注入系统提示。

Skills 的本质是“怎么做”的流程说明、经验规范和能力摘要。若要创建模型可直接调用的正式工具，请使用 `tools/<tool>/tool.json + handler.py`，并调用 `reload_workspace_tools` 注册；不要把可执行工具伪装成 Skill。

示例：

```
skills/
  my_tool/
    SKILL.md
```

可运行 `smartclaw skills ...`（若已安装）管理生命周期。
