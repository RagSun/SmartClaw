"""
飞书消息格式化模块
将 LLM 响应转换为飞书 Interactive Card 格式

与按行拆成多个 `markdown` 元素不同，正文使用单个 `div` + `lark_md`
整块渲染，Markdown 表格、列表、标题才能正确显示（对齐 smartclawd 做法）。
"""

from __future__ import annotations

import json
import re

# 飞书 lark_md 单块不宜过大，留出余量避免整段 JSON 超限导致发送失败
_LARK_MD_MAX_LEN = 7500
_HEADER_TITLE_MAX = 100


def format_feishu_card(
    content: str,
    agent_name: str = "SmartClaw",
) -> str:
    """
    将文本内容转换为飞书 Interactive Card 格式的 JSON 字符串。

    若正文以 Markdown ATX 标题开头（# ～ ######），则提取为卡片 header 标题，
    正文不再重复该标题行。
    """
    raw = content if isinstance(content, str) else str(content or "")
    title, body = _extract_heading_title_and_body(raw)

    if not body.strip():
        body = " "
    elif len(body) > _LARK_MD_MAX_LEN:
        body = body[: _LARK_MD_MAX_LEN - 48].rstrip() + "\n\n_（内容过长已截断）_"

    header_title = f"🤖 {title}" if title else f"🤖 {agent_name}"
    if len(header_title) > _HEADER_TITLE_MAX:
        header_title = header_title[: _HEADER_TITLE_MAX - 1] + "…"

    footer = f"SmartClaw · {agent_name}"
    if len(footer) > 120:
        footer = footer[:117] + "…"

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": "turquoise",
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": body,
                },
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": footer,
                    }
                ],
            },
        ],
    }

    return json.dumps(card, ensure_ascii=False)


def _extract_heading_title_and_body(text: str) -> tuple[str, str]:
    """
    若首行（忽略前置空行）为 # / ## … 则返回 (标题纯文本, 剩余正文)。
    否则返回 ("", 全文)。
    """
    stripped = text.strip("\n")
    if not stripped:
        return "", stripped

    lines = stripped.split("\n")
    first = lines[0].strip()
    m = re.match(r"^#{1,6}\s+(.+)$", first)
    if not m:
        return "", stripped

    title = m.group(1).strip()
    rest = "\n".join(lines[1:])
    return title, rest.lstrip("\n")


def format_simple_text(content: str) -> str:
    """简单文本消息"""
    return json.dumps({"text": content}, ensure_ascii=False)
