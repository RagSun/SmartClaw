from smartclaw.channel import feishu_runtime


def test_build_feishu_session_id_private_includes_app_id():
    assert (
        feishu_runtime.build_feishu_session_id(
            user_open_id="ou_123",
            app_id="cli_abc",
            chat_id="oc_group",
            is_group=False,
        )
        == "ou_123_cli_abc"
    )


def test_build_feishu_session_id_group_uses_chat_id():
    assert (
        feishu_runtime.build_feishu_session_id(
            user_open_id="ou_123",
            app_id="cli_abc",
            chat_id="oc_group",
            is_group=True,
        )
        == "oc_group_cli_abc"
    )


def test_feishu_download_dir_uses_runtime_temp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_runtime.paths, "TEMP_DIR", tmp_path)

    out = feishu_runtime.feishu_download_dir("bot/dept:a", "dept/a")

    assert out == tmp_path / "feishu" / "dept_a" / "bot_dept_a" / "downloads"
