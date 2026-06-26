"""飞书卡片 JSON：与 smartclawd 一致，单块 lark_md 保留表格等排版。"""

import json

from smartclaw.feishu.formatter import format_feishu_card


def test_card_uses_single_lark_md_block():
    md = "| a | b |\n| - | - |\n| 1 | 2 |\n\n第二段"
    blob = format_feishu_card(md, agent_name="bot_a")
    card = json.loads(blob)
    assert card["config"]["wide_screen_mode"] is True
    assert card["header"]["template"] == "turquoise"
    els = card["elements"]
    assert len(els) == 3
    assert els[0]["tag"] == "div"
    assert els[0]["text"]["tag"] == "lark_md"
    assert "| a | b |" in els[0]["text"]["content"]
    assert "\n| 1 | 2 |" in els[0]["text"]["content"]
    assert els[1]["tag"] == "hr"
    assert els[2]["tag"] == "note"


def test_atx_heading_becomes_header_title_and_is_stripped_from_body():
    content = "## 2号线状态\n\n| 项 | 值 |\n| - | - |\n| x | y |"
    blob = format_feishu_card(content, agent_name="dept_a")
    card = json.loads(blob)
    assert "2号线状态" in card["header"]["title"]["content"]
    body = card["elements"][0]["text"]["content"]
    assert "## 2号线状态" not in body
    assert "| 项 | 值 |" in body


def test_without_heading_uses_agent_in_header():
    blob = format_feishu_card("plain only", agent_name="Bot")
    card = json.loads(blob)
    assert "Bot" in card["header"]["title"]["content"]
    assert card["elements"][0]["text"]["content"].strip() == "plain only"


def test_footer_contains_agent_name():
    blob = format_feishu_card("hi", agent_name="bot_dept_a")
    card = json.loads(blob)
    note = card["elements"][2]["elements"][0]["content"]
    assert "bot_dept_a" in note


def test_very_long_body_truncated():
    long_text = "x" * 9000
    blob = format_feishu_card(long_text, agent_name="a")
    body = json.loads(blob)["elements"][0]["text"]["content"]
    assert len(body) < 8000
    assert "截断" in body
