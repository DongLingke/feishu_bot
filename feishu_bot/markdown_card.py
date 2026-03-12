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
    1. 尽量保留原始 Markdown，避免把本来能渲染的语法改坏。
    2. 只对飞书已知不稳定的部分做兼容处理。
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized_lines: list[str] = []
    quote_lines: list[str] = []
    in_code_block = False

    def flush_quote_lines() -> None:
        nonlocal quote_lines
        if not quote_lines:
            return
        if normalized_lines and normalized_lines[-1] != "":
            normalized_lines.append("")
        normalized_lines.append("<note>")
        normalized_lines.extend(quote_lines)
        normalized_lines.append("</note>")
        normalized_lines.append("")
        quote_lines = []

    def ensure_blank_line() -> None:
        if normalized_lines and normalized_lines[-1] != "":
            normalized_lines.append("")

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        stripped = line.strip()

        if stripped.startswith("```"):
            flush_quote_lines()
            normalized_lines.append(stripped)
            in_code_block = not in_code_block
            continue

        if in_code_block:
            normalized_lines.append(raw_line)
            continue

        quote_match = re.match(r"^\s*>\s?(.*)$", line)
        if quote_match:
            quote_lines.append(quote_match.group(1))
            continue

        flush_quote_lines()

        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            title = heading_match.group(2).strip()
            ensure_blank_line()
            if level <= 2:
                normalized_lines.append(f"{'#' * level} {title}")
            else:
                normalized_lines.append(f"**{title}**" if title else "")
            normalized_lines.append("")
            continue

        bullet_match = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if bullet_match:
            item = bullet_match.group(1).strip()
            normalized_lines.append(f"- {item}" if item else "")
            continue

        ordered_match = re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        if ordered_match:
            index = ordered_match.group(1)
            item = ordered_match.group(2).strip()
            normalized_lines.append(f"{index}. {item}" if item else "")
            continue

        if re.match(r"^\s*([-*_])\1{2,}\s*$", stripped):
            ensure_blank_line()
            normalized_lines.append("---")
            normalized_lines.append("")
            continue

        normalized_lines.append(raw_line)

    flush_quote_lines()

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
