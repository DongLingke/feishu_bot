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


def _is_markdown_hr(line: str) -> bool:
    return bool(re.match(r"^\s*([-*_])\1{2,}\s*$", line.strip()))


def _soften_heading_sizes(content: str) -> str:
    """把较大的 Markdown 标题降一级，避免飞书里视觉过重。"""
    lines = content.split("\n")
    softened_lines: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if not heading_match:
            softened_lines.append(line)
            continue

        title = heading_match.group(2).strip()
        softened_lines.append(f"**{title}**" if title else "")

    return "\n".join(softened_lines)


def _flush_markdown_block(elements: list[dict[str, Any]], lines: list[str]) -> None:
    content = "\n".join(lines).strip()
    if not content:
        return
    content = _soften_heading_sizes(content)
    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": content,
            },
        }
    )


def build_lark_md_card_elements(text: str) -> list[dict[str, Any]]:
    """
    构造更贴近原始 Markdown 结构的卡片元素。

    这里尽量保留原始段落，只把 Markdown 分隔线转换成真实卡片分隔线，
    避免所有内容挤在一个大块里，导致版式和原始样式偏差太大。
    """
    lines = normalize_lark_md(text).split("\n")
    elements: list[dict[str, Any]] = []
    current_block: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        stripped = line.strip()

        if _is_markdown_hr(stripped):
            _flush_markdown_block(elements, current_block)
            current_block = []
            if elements and elements[-1].get("tag") != "hr":
                elements.append({"tag": "hr"})
            continue

        if stripped == "":
            if current_block and current_block[-1] != "":
                current_block.append("")
            continue

        current_block.append(line)

    _flush_markdown_block(elements, current_block)

    if elements and elements[-1].get("tag") == "hr":
        elements.pop()

    if not elements:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "",
                },
            }
        )

    return elements


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
            "elements": build_lark_md_card_elements(final_text)
        },
    }
    return json.dumps(card, ensure_ascii=False)
