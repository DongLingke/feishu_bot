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
    """将常见 Markdown 语法调整为飞书 lark_md 更稳定的写法。"""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized_lines: list[str] = []
    in_code_block = False

    for raw_line in lines:
        line = raw_line.rstrip()

        if line.startswith("```"):
            in_code_block = not in_code_block
            normalized_lines.append(line)
            continue

        if in_code_block:
            normalized_lines.append(line)
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if heading_match:
            title = heading_match.group(2).strip()
            normalized_lines.append(f"**{title}**" if title else "")
            continue

        bullet_match = re.match(r"^(\s*)[-*+]\s+(.+)$", line)
        if bullet_match:
            indent = bullet_match.group(1)
            item = bullet_match.group(2).strip()
            normalized_lines.append(f"{indent}• {item}" if item else "")
            continue

        ordered_match = re.match(r"^(\s*)(\d+)\.\s+(.+)$", line)
        if ordered_match:
            indent = ordered_match.group(1)
            index = ordered_match.group(2)
            item = ordered_match.group(3).strip()
            normalized_lines.append(f"{indent}{index}. {item}" if item else "")
            continue

        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            normalized_lines.append("----------")
            continue

        normalized_lines.append(line)

    normalized_text = "\n".join(normalized_lines)
    normalized_text = re.sub(r"\n{3,}", "\n\n", normalized_text).strip()
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
