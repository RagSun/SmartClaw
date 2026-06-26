"""将内置工具注册到全局 ToolRegistry（runner 与 CLI 等共用）。"""

from __future__ import annotations

from smartclaw.agent.tools.metadata import metadata_for_tool
from smartclaw.agent.tools.registry import get_tool_registry
from smartclaw.console import info


def register_builtin_tools() -> None:
    """注册所有内置工具到全局注册表。"""
    from smartclaw.agent.tools.exec_tool import exec_handler
    from smartclaw.agent.tools.read_tool import read_handler
    from smartclaw.agent.tools.write_tool import write_handler
    from smartclaw.agent.todos.types import get_write_todos_definition
    from smartclaw.agent.todos.tool_handler import write_todos_handler

    registry = get_tool_registry()

    registry.register(
        name="exec",
        description="执行 Shell 命令并返回输出结果",
        handler=exec_handler,
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
                "timeout": {
                    "type": "integer",
                    "description": "超时时间(秒)",
                    "default": 30,
                },
                "cwd": {"type": "string", "description": "工作目录", "default": "/tmp"},
            },
            "required": ["command"],
        },
        timeout_ms=60000,
        metadata=metadata_for_tool("exec"),
    )

    registry.register(
        name="read_file",
        description="读取文件内容",
        handler=read_handler,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "limit": {
                    "type": "integer",
                    "description": "最多读取行数",
                    "default": 0,
                },
            },
            "required": ["path"],
        },
        timeout_ms=10000,
        metadata=metadata_for_tool("read_file"),
    )

    registry.register(
        name="write_file",
        description="写入内容到文件",
        handler=write_handler,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "内容"},
            },
            "required": ["path", "content"],
        },
        timeout_ms=10000,
        metadata=metadata_for_tool("write_file"),
    )

    write_todos_def = get_write_todos_definition()
    registry.register(
        name=write_todos_def["name"],
        description=write_todos_def["description"],
        handler=write_todos_handler,
        parameters=write_todos_def["parameters"],
        timeout_ms=5000,
        metadata=metadata_for_tool(write_todos_def["name"]),
    )
    from smartclaw.agent.tools.agent_admin_tool import (
        AGENT_CREATE_TOOL_DEFINITION,
        AGENT_STATUS_TOOL_DEFINITION,
        AGENT_UPDATE_FEISHU_TOOL_DEFINITION,
        agent_create_handler,
        agent_status_handler,
        agent_update_feishu_handler,
    )
    from smartclaw.agent.tools.workspace_tool_admin import (
        RELOAD_WORKSPACE_TOOLS_DEFINITION,
        WORKSPACE_TOOL_STATUS_DEFINITION,
        reload_workspace_tools_handler,
        workspace_tool_status_handler,
    )

    for definition, handler in (
        (AGENT_CREATE_TOOL_DEFINITION, agent_create_handler),
        (AGENT_UPDATE_FEISHU_TOOL_DEFINITION, agent_update_feishu_handler),
        (AGENT_STATUS_TOOL_DEFINITION, agent_status_handler),
        (RELOAD_WORKSPACE_TOOLS_DEFINITION, reload_workspace_tools_handler),
        (WORKSPACE_TOOL_STATUS_DEFINITION, workspace_tool_status_handler),
    ):
        registry.register(
            name=definition["name"],
            description=definition["description"],
            handler=handler,
            parameters=definition["parameters"],
            timeout_ms=30000,
            metadata=metadata_for_tool(definition["name"]),
        )
        info(f"注册工具: {definition['name']} (async=True)")
    from smartclaw.agent.tools.upload_tool import get_upload_image_definition, upload_image_handler
    from smartclaw.agent.tools.feishu_doc_tool import (
        FEISHU_DOC_TOOL_DEFINITION,
        WRITE_FEISHU_DOC_TOOL_DEFINITION,
        feishu_doc_handler,
        write_feishu_doc_content_handler,
    )

    upload_def = get_upload_image_definition()
    registry.register(
        name=upload_def["name"],
        description=upload_def["description"],
        handler=upload_image_handler,
        parameters=upload_def["parameters"],
        timeout_ms=10000,
        metadata=metadata_for_tool(upload_def["name"]),
    )
    info(f"注册工具: {upload_def['name']} (async=False)")

    registry.register(
        name=FEISHU_DOC_TOOL_DEFINITION["name"],
        description=FEISHU_DOC_TOOL_DEFINITION["description"],
        handler=feishu_doc_handler,
        parameters=FEISHU_DOC_TOOL_DEFINITION["parameters"],
        timeout_ms=30000,
        metadata=metadata_for_tool(FEISHU_DOC_TOOL_DEFINITION["name"]),
    )
    info(f"注册工具: {FEISHU_DOC_TOOL_DEFINITION['name']} (async=False)")

    registry.register(
        name=WRITE_FEISHU_DOC_TOOL_DEFINITION["name"],
        description=WRITE_FEISHU_DOC_TOOL_DEFINITION["description"],
        handler=write_feishu_doc_content_handler,
        parameters=WRITE_FEISHU_DOC_TOOL_DEFINITION["parameters"],
        timeout_ms=60000,
        metadata=metadata_for_tool(WRITE_FEISHU_DOC_TOOL_DEFINITION["name"]),
    )
    info(f"注册工具: {WRITE_FEISHU_DOC_TOOL_DEFINITION['name']} (async=False)")

    from smartclaw.agent.tools.integration_http_tool import integration_http_request_handler

    registry.register(
        name="integration_http_request",
        description=(
            "对给定 URL 发起 HTTP 请求；自动附带当前租户在 tenant_integration_env "
            "中配置的请求头（键名即 Header 名，如 Authorization）。可选 headers_json 合并额外头。"
        ),
        handler=integration_http_request_handler,
        parameters={
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "description": "HTTP 方法：GET、POST、PUT、PATCH、DELETE",
                    "default": "GET",
                },
                "url": {"type": "string", "description": "完整 URL"},
                "headers_json": {
                    "type": "string",
                    "description": "可选，JSON 对象字符串，如 {\"X-Request-Id\":\"1\"}",
                    "default": "",
                },
                "body": {
                    "type": "string",
                    "description": "POST/PUT/PATCH 的原文 body",
                    "default": "",
                },
                "timeout_seconds": {
                    "type": "number",
                    "description": "超时（秒）",
                    "default": 30.0,
                },
            },
            "required": ["url"],
        },
        timeout_ms=60000,
        metadata=metadata_for_tool("integration_http_request"),
    )
    info("注册工具: integration_http_request (async=True)")

    from smartclaw.agent.tools.background_task_tool import (
        BACKGROUND_TASK_TOOL_DEFINITION,
        background_task_handler,
    )
    from smartclaw.agent.tools.subagent_tool import (
        SPAWN_SUBAGENT_TOOL_DEFINITION,
        SUBAGENT_CANCEL_TOOL_DEFINITION,
        SUBAGENT_STATUS_TOOL_DEFINITION,
        spawn_subagent_handler,
        subagent_cancel_handler,
        subagent_status_handler,
    )

    registry.register(
        name=BACKGROUND_TASK_TOOL_DEFINITION["name"],
        description=BACKGROUND_TASK_TOOL_DEFINITION["description"],
        handler=background_task_handler,
        parameters=BACKGROUND_TASK_TOOL_DEFINITION["parameters"],
        timeout_ms=10000,
        metadata=metadata_for_tool(BACKGROUND_TASK_TOOL_DEFINITION["name"]),
    )
    info(f"注册工具: {BACKGROUND_TASK_TOOL_DEFINITION['name']} (async=False)")

    for definition, handler in (
        (SPAWN_SUBAGENT_TOOL_DEFINITION, spawn_subagent_handler),
        (SUBAGENT_STATUS_TOOL_DEFINITION, subagent_status_handler),
        (SUBAGENT_CANCEL_TOOL_DEFINITION, subagent_cancel_handler),
    ):
        registry.register(
            name=definition["name"],
            description=definition["description"],
            handler=handler,
            parameters=definition["parameters"],
            timeout_ms=10000,
            metadata=metadata_for_tool(definition["name"]),
        )
        info(f"注册工具: {definition['name']} (async=True)")

    from smartclaw.agent.tools.memory_tool import (
        MEMORY_GET_TOOL_DEFINITION,
        MEMORY_SEARCH_TOOL_DEFINITION,
        MEMORY_WRITE_TOOL_DEFINITION,
        memory_get_handler,
        memory_search_handler,
        memory_write_handler,
    )

    for definition, handler in (
        (MEMORY_SEARCH_TOOL_DEFINITION, memory_search_handler),
        (MEMORY_GET_TOOL_DEFINITION, memory_get_handler),
        (MEMORY_WRITE_TOOL_DEFINITION, memory_write_handler),
    ):
        registry.register(
            name=definition["name"],
            description=definition["description"],
            handler=handler,
            parameters=definition["parameters"],
            timeout_ms=30000,
            metadata=metadata_for_tool(definition["name"]),
        )
        info(f"注册工具: {definition['name']} (async=False)")

    from smartclaw.agent.tools.skill_audit_tool import (
        SKILL_AUDIT_TOOL_DEFINITION,
        skill_audit_handler,
    )

    registry.register(
        name=SKILL_AUDIT_TOOL_DEFINITION["name"],
        description=SKILL_AUDIT_TOOL_DEFINITION["description"],
        handler=skill_audit_handler,
        parameters=SKILL_AUDIT_TOOL_DEFINITION["parameters"],
        timeout_ms=10000,
        metadata=metadata_for_tool(SKILL_AUDIT_TOOL_DEFINITION["name"]),
    )
    info(f"注册工具: {SKILL_AUDIT_TOOL_DEFINITION['name']} (async=False)")

    from smartclaw.agent.tools.tool_audit_tool import (
        TOOL_AUDIT_TOOL_DEFINITION,
        tool_audit_handler,
    )

    registry.register(
        name=TOOL_AUDIT_TOOL_DEFINITION["name"],
        description=TOOL_AUDIT_TOOL_DEFINITION["description"],
        handler=tool_audit_handler,
        parameters=TOOL_AUDIT_TOOL_DEFINITION["parameters"],
        timeout_ms=10000,
        metadata=metadata_for_tool(TOOL_AUDIT_TOOL_DEFINITION["name"]),
    )
    info(f"注册工具: {TOOL_AUDIT_TOOL_DEFINITION['name']} (async=False)")

    from smartclaw.agent.tools.docker_tool import DOCKER_TOOL_DEFINITION, docker_project_handler

    registry.register(
        name=DOCKER_TOOL_DEFINITION["name"],
        description=DOCKER_TOOL_DEFINITION["description"],
        handler=docker_project_handler,
        parameters=DOCKER_TOOL_DEFINITION["parameters"],
        timeout_ms=30000,
        metadata=metadata_for_tool(DOCKER_TOOL_DEFINITION["name"]),
    )
    info(f"注册工具: {DOCKER_TOOL_DEFINITION['name']} (async=False)")

    from smartclaw.agent.tools.snapshot_tool import SNAPSHOT_TOOL_DEFINITION, snapshot_handler

    registry.register(
        name=SNAPSHOT_TOOL_DEFINITION["name"],
        description=SNAPSHOT_TOOL_DEFINITION["description"],
        handler=snapshot_handler,
        parameters=SNAPSHOT_TOOL_DEFINITION["parameters"],
        timeout_ms=60000,
        metadata=metadata_for_tool(SNAPSHOT_TOOL_DEFINITION["name"]),
    )
    snapshot_name = SNAPSHOT_TOOL_DEFINITION["name"]
    info(f"注册工具: {snapshot_name} (async=False)")

    from smartclaw.agent.tools.monitor_tool import MONITOR_TOOL_DEFINITION, monitor_handler

    registry.register(
        name=MONITOR_TOOL_DEFINITION["name"],
        description=MONITOR_TOOL_DEFINITION["description"],
        handler=monitor_handler,
        parameters=MONITOR_TOOL_DEFINITION["parameters"],
        timeout_ms=30000,
        metadata=metadata_for_tool(MONITOR_TOOL_DEFINITION["name"]),
    )
    monitor_name = MONITOR_TOOL_DEFINITION["name"]
    info(f"注册工具: {monitor_name} (async=False)")


__all__ = ["register_builtin_tools"]
