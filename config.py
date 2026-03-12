"""项目运行配置。

公开仓库中的配置读取逻辑统一放在这里：
1. 优先读取当前目录下的 `.env`。
2. 如果 `.env` 没有对应字段，再读取系统环境变量。
3. 如果都没有，则使用公开占位值。

真实的私有配置应该只保留在 `.env` 中，不要直接写进仓库文件。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _load_dotenv() -> dict[str, str]:
    """读取项目根目录下的 .env 文件。"""
    dotenv_path = Path(__file__).with_name(".env")
    if not dotenv_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key:
            values[key] = value
    return values


_DOTENV = _load_dotenv()


def _env(name: str, default: str) -> str:
    return _DOTENV.get(name, os.getenv(name, default))


def _env_int(name: str, default: int) -> int:
    value = _env(name, str(default)).strip()
    return int(value)


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw_value = _env(name, ",".join(default))
    return tuple(item.strip() for item in raw_value.split(",") if item.strip())


def _env_json_dict(name: str, default: dict[str, object]) -> dict[str, object]:
    raw_value = _env(name, json.dumps(default, ensure_ascii=False))
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 必须是合法的 JSON 对象") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} 必须是 JSON 对象")
    return parsed


# 飞书应用配置
FEISHU_APP_ID = _env("FEISHU_APP_ID", "REPLACE_WITH_FEISHU_APP_ID")
FEISHU_APP_SECRET = _env("FEISHU_APP_SECRET", "REPLACE_WITH_FEISHU_APP_SECRET")
FEISHU_LOG_LEVEL = _env("FEISHU_LOG_LEVEL", "INFO").upper()

# Dify 配置
DIFY_APP_TYPE = _env("DIFY_APP_TYPE", "chat")
DIFY_APP_PAGE_URL = _env("DIFY_APP_PAGE_URL", "https://your-dify.example.com/app/your-app-id")
DIFY_API_BASE_URL = _env("DIFY_API_BASE_URL", "https://your-dify.example.com/v1")
DIFY_API_PATH = _env("DIFY_API_PATH", "/chat-messages")
DIFY_API_KEY = _env("DIFY_API_KEY", "REPLACE_WITH_DIFY_API_KEY")
DIFY_QUERY_INPUT_KEYS = _env_csv("DIFY_QUERY_INPUT_KEYS", ("query", "text", "message", "user_query"))
DIFY_FIXED_INPUTS = _env_json_dict("DIFY_FIXED_INPUTS", {})
DIFY_REQUEST_TIMEOUT_SECONDS = _env_int("DIFY_REQUEST_TIMEOUT_SECONDS", 180)
