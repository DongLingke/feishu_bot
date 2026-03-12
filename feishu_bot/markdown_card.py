from __future__ import annotations

import json
from typing import Any


def trim_text(value: Any, limit: int) -> str:
    """将任意值转成文本，并按指定长度截断。"""
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...[truncated]..."


def trim_message_text(text: str, max_chars: int) -> str:
    """按飞书消息长度限制裁剪正文。"""
    if len(text) <= max_chars:
        return text
    suffix = "\n\n[内容过长，已截断]"
    allowed = max(0, max_chars - len(suffix))
    return text[:allowed] + suffix


def prepare_streaming_preview_text(text: str) -> str:
    """直接透传上游返回的 Markdown，不做格式修正。"""
    return text


def build_lark_md_card_json(text: str, max_chars: int) -> str:
    """构造飞书 interactive + lark_md 卡片内容。"""
    final_text = trim_message_text(text, max_chars)
    card = {
        "schema": "2.0",
        "config": {
            "width_mode": "fill",
        },
        "body": {
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": final_text,
                    },
                }
            ]
        },
    }
    return json.dumps(card, ensure_ascii=False)
