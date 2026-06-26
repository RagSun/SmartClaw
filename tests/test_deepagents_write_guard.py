"""deepagents_wrapper 落盘复核：工具链检测 helpers 单测."""

from langchain_core.messages import ToolMessage

from smartclaw.agent.control_flow import is_application_delivery_request
from smartclaw.agent.deepagents_wrapper import (
    _append_workspace_write_truth_guard,
    _execution_plan_requests_workspace_disk_write,
    _scrub_workspace_pseudopaths,
    _tool_payload_suggests_write_success,
    _user_requests_workspace_disk_write,
    messages_indicate_workspace_write_success,
)


def test_registry_write_success_string():
    assert _tool_payload_suggests_write_success(
        "write_file", "文件已写入: D:\\a.txt (42 bytes)"
    )


def test_write_file_failure_via_tool_marker():
    assert not _tool_payload_suggests_write_success(
        "write_file",
        "[工具失败] 需要 developer 角色",
    )


def test_messages_chain_detects_toolmessage():
    msgs = [
        ToolMessage(content="文件已写入: ./docs/x.md (10 bytes)", name="write_file", tool_call_id="1"),
    ]
    assert messages_indicate_workspace_write_success(msgs)


def test_messages_empty_no_success():
    assert not messages_indicate_workspace_write_success([])


def test_append_guard_idempotent_marker():
    s = "ok"
    twice = _append_workspace_write_truth_guard(
        _append_workspace_write_truth_guard(s, host_root=r"C:\w"),
        host_root=r"C:\w",
    )
    assert twice.count("[SmartClaw｜落盘复核]") == 1
    assert "ok" not in twice
    assert "未验证到文件成功写入" in twice


def test_disk_write_intent_matches_storage_docs_phrase():
    assert _user_requests_workspace_disk_write("帮我写一首 中国长城 的诗 存储在docs目录下")


def test_execution_plan_write_intent_matches_write_file():
    assert _execution_plan_requests_workspace_disk_write(
        {"steps": [{"tool": "write_file", "path": "docs/x.md"}]}
    )


def test_execution_plan_execute_alone_is_not_write_intent():
    assert not _execution_plan_requests_workspace_disk_write(
        {"steps": [{"tool": "execute", "description": "列出 factory 相关工具"}]}
    )


def test_scrub_workspace_pseudopaths_to_host_root():
    out = _scrub_workspace_pseudopaths(
        "已保存到 `/workspace/docs/a.md`，另见 `/docs/b.md`。",
        host_root=r"D:\hmw\workspace\dept_b\bot_dept_b",
    )
    assert r"D:\hmw\workspace\dept_b\bot_dept_b\docs\a.md" in out
    assert r"D:\hmw\workspace\dept_b\bot_dept_b\docs\b.md" in out


def test_execute_touch_with_exit_zero_counts():
    body = "touch app.py\n\nExit code: 0\n"
    assert _tool_payload_suggests_write_success("execute", body)


def test_flask_demo_port_triggers_application_delivery():
    msg = "你帮我实现一个flask应用demo，端口为8955"
    assert is_application_delivery_request(msg)


def test_bare_write_substring_not_disk_intent_regex():
    """收窄「写入」：孤立两字不再触发落盘意图正则。"""
    assert not _user_requests_workspace_disk_write("请写入内存缓冲区演示")
