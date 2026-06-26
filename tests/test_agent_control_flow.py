from types import SimpleNamespace

from smartclaw.agent import control_flow
from smartclaw.auth.tool_gate import ToolSecurityContext


def _ctx(roles=("default",)):
    return ToolSecurityContext(
        tenant_id="dept_b",
        feishu_open_id="ou_test",
        roles=tuple(roles),
        agent_id="bot_dept_b",
        session_id="s1",
    )


def test_auth_or_role_query_catches_natural_permission_questions():
    assert control_flow.is_auth_or_role_query("我现在有什么权限和角色")
    assert control_flow.is_auth_or_role_query("show my roles")


def test_preflight_denies_write_and_execute_before_agent(monkeypatch):
    monkeypatch.setattr(
        control_flow,
        "get_config",
        lambda: SimpleNamespace(auth=SimpleNamespace(tool_required_roles_any={})),
    )
    monkeypatch.setattr(
        control_flow,
        "check_tool_allowed",
        lambda tool, ctx, cfg: (
            False,
            f"工具 {tool} 需要以下角色之一: ['developer']",
        )
        if tool == "write_file"
        else (True, ""),
    )
    monkeypatch.setattr(
        control_flow,
        "check_shell_capability_allowed",
        lambda ctx, cfg: (
            False,
            "Shell 能力需要以下角色之一 ['developer']",
        ),
    )

    result = control_flow.preflight_capabilities(
        "请创建一个 Streamlit 小应用并启动服务，保存到 docs 目录", _ctx()
    )

    assert not result.allowed
    assert result.required_tools == ("write_file", "execute", "background_task")
    assert "write\\_file" in result.reply
    assert "进入模型执行前停止" in result.reply


def test_application_delivery_detection_is_generic():
    assert control_flow.is_application_delivery_request("请创建一个 Flask API 并启动服务")
    assert control_flow.is_application_delivery_request("帮我安装 nginx 并配置反向代理访问网站")
    assert control_flow.is_application_delivery_request("使用 Docker 部署一个前后端项目并给出 URL")


def test_generic_application_delivery_requires_write_execute_and_background():
    assert control_flow.requested_capabilities("请创建一个 Flask API 并启动服务") == (
        "write_file",
        "execute",
        "background_task",
    )
    assert control_flow.requested_capabilities("帮我安装 nginx 并配置反向代理访问网站") == (
        "write_file",
        "execute",
        "background_task",
    )
    assert control_flow.requested_capabilities("使用 Docker 部署一个项目并给出 URL") == (
        "write_file",
        "execute",
        "background_task",
    )


def test_chat_only_negative_file_delivery_does_not_require_write_file():
    intent = control_flow.classify_task_intent("你帮我写一篇发给我 不需要单独保存到文件")

    assert intent.kind == "chat_only"
    assert intent.required_tools == ()
    assert control_flow.requested_capabilities("你帮我写一篇发给我 不需要单独保存到文件") == ()


def test_explicit_docs_save_still_requires_write_file():
    intent = control_flow.classify_task_intent("帮我写一篇关于具身智能文章 500字并保存docs")

    assert intent.kind == "file_delivery"
    assert intent.required_tools == ("write_file",)


def test_application_delivery_still_requires_files_even_with_url_response():
    intent = control_flow.classify_task_intent(
        "帮我模拟20条高考成绩数据，采用flask帮我构建一个可视化前端，启动端口在8691，完成后返回对应的url地址。"
    )

    assert intent.kind == "app_delivery"
    assert intent.required_tools == ("write_file", "execute", "background_task")


def test_misspelled_streamlit_runtime_app_delivery_requires_runtime_tools():
    intent = control_flow.classify_task_intent(
        "帮我模拟20条采购数据，采用stremlit帮我构建一个可视化交互前端，启动端口在8692，完成后返回对应的url地址。"
    )

    assert intent.kind == "app_delivery"
    assert intent.delivery_target == "runtime_url"
    assert intent.required_tools == ("write_file", "execute", "background_task")


def test_feishu_doc_delivery_uses_cloud_doc_tool_not_workspace_write():
    intent = control_flow.classify_task_intent(
        "帮我模拟20条采购数据，采用stremlit帮我构建一个可视化交互前端 保存到在线飞书文档"
    )

    assert intent.kind == "cloud_doc_delivery"
    assert intent.delivery_target == "feishu_doc"
    assert intent.required_tools == ("create_feishu_doc",)


def test_followup_feishu_doc_correction_uses_cloud_doc_tool():
    intent = control_flow.classify_task_intent("注意是保存到飞书文档")

    assert intent.kind == "cloud_doc_delivery"
    assert intent.delivery_target == "feishu_doc"
    assert intent.required_tools == ("create_feishu_doc",)


def test_plain_content_words_do_not_trigger_command_permissions():
    assert control_flow.requested_capabilities("帮我写一篇关于客户服务的文章") == ()
    assert control_flow.requested_capabilities("请解释 URL 是什么") == ()
    assert control_flow.requested_capabilities("写一段关于端口贸易的介绍") == ()


def test_preflight_allows_developer(monkeypatch):
    monkeypatch.setattr(
        control_flow,
        "get_config",
        lambda: SimpleNamespace(auth=SimpleNamespace(tool_required_roles_any={})),
    )
    monkeypatch.setattr(control_flow, "check_tool_allowed", lambda tool, ctx, cfg: (True, ""))
    monkeypatch.setattr(
        control_flow,
        "check_shell_capability_allowed",
        lambda ctx, cfg: (True, ""),
    )

    result = control_flow.preflight_capabilities(
        "请创建 Flask 小应用并启动", _ctx(("developer",))
    )

    assert result.allowed

