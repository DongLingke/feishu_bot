from __future__ import annotations

import json
import re
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


def normalize_lark_md(text: str) -> str:
    """
    将 Markdown 调整成更适合飞书 lark_md 的写法。

    处理原则：
    1. 尽量保留上游原始 Markdown，不主动改写标题、引用、列表等结构。
    2. 只做最轻量的清洗，避免把原始语义改坏。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized_lines: list[str] = []
    in_code_block = False

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        stripped = line.strip()

        if stripped.startswith("```"):
            normalized_lines.append(line)
            in_code_block = not in_code_block
            continue

        if in_code_block:
            normalized_lines.append(raw_line)
            continue

        normalized_lines.append(line)

    normalized_text = "\n".join(normalized_lines)
    normalized_text = re.sub(r"\n{4,}", "\n\n\n", normalized_text).strip("\n")
    return normalized_text


def prepare_streaming_preview_text(text: str) -> str:
    """流式中途若代码块未闭合，则临时补齐结束符。"""
    if text.count("```") % 2 == 1:
        return f"{text}\n```"
    return text


def build_lark_md_card_json(text: str, max_chars: int) -> str:
    """构造飞书 interactive + lark_md 卡片内容。"""
    final_text = trim_message_text(normalize_lark_md(text), max_chars)
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
