"""``canonical_display_name`` 渠道感知命名约定测试。"""

from smartclaw.agent.naming import canonical_display_name, canonical_feishu_display_name


def test_feishu_channel_uses_prefix() -> None:
    assert canonical_display_name("sales", "feishu") == "SmartClaw-sales"
    # 默认 channel 为 feishu
    assert canonical_display_name("default") == "SmartClaw-default"


def test_wecom_channel_uses_bare_name() -> None:
    assert canonical_display_name("sales", "wecom") == "sales"
    assert canonical_display_name("default", "wecom") == "default"


def test_empty_name_falls_back_to_agent() -> None:
    assert canonical_display_name("", "feishu") == "SmartClaw-agent"
    assert canonical_display_name(None, "wecom") == "agent"  # type: ignore[arg-type]


def test_channel_is_case_insensitive() -> None:
    assert canonical_display_name("sales", "WECOM") == "sales"
    assert canonical_display_name("sales", "Feishu") == "SmartClaw-sales"


def test_legacy_alias_matches_feishu_channel() -> None:
    assert canonical_feishu_display_name("bot") == "SmartClaw-bot"
    assert canonical_feishu_display_name("bot") == canonical_display_name("bot", "feishu")
