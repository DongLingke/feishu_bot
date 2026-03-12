from __future__ import annotations

import atexit
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import lark_oapi as lark
from lark_oapi.api.cardkit.v1 import (
    Card,
    ContentCardElementRequest,
    ContentCardElementRequestBody,
    CreateCardRequest,
    CreateCardRequestBody,
    UpdateCardRequest,
    UpdateCardRequestBody,
)
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    DeleteMessageRequest,
    Emoji,
    P2ImMessageMessageReadV1,
    P2ImMessageReceiveV1,
    P2ImMessageReactionCreatedV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    UpdateMessageRequest,
    UpdateMessageRequestBody,
)

import config


TEXT_MENTION_PATTERN = re.compile(r"@_user_\d+\s*")
FEISHU_REPLY_IN_THREAD = False
FEISHU_ACK_REACTION_EMOJI_TYPE = "DONE"
FEISHU_PLACEHOLDER_TEXT = "正在处理中，请稍候..."
FEISHU_EMPTY_RESULT_TEXT = "工作流已结束，但没有返回可展示的文本结果。"
FEISHU_MAX_MESSAGE_CHARS = 28000
FEISHU_TEXT_MESSAGE_TYPE = "text"
FEISHU_CARD_MESSAGE_TYPE = "interactive"
FEISHU_PARTIAL_CARD_ELEMENT_ID = "markdown_1"
FEISHU_PARTIAL_CARD_STATUS_ELEMENT_ID = "markdown_2"

LOCAL_DIFY_MOCK_HOST = "127.0.0.1"
LOCAL_DIFY_MOCK_PORT = 18080
LOCAL_DIFY_MOCK_BASE_URL = f"http://{LOCAL_DIFY_MOCK_HOST}:{LOCAL_DIFY_MOCK_PORT}"
LOCAL_DIFY_MOCK_HEALTH_URL = f"{LOCAL_DIFY_MOCK_BASE_URL}/healthz"

MESSAGE_MAX_AGE_MS = 120000
MESSAGE_CACHE_SIZE = 1000
STREAM_QUEUE_MIN_CHARS = 10
STREAM_QUEUE_INTERVAL_SECONDS = 0.2
PARTIAL_CARD_MAX_CHARS = 20 * 1024
API_RETRY_TIMES = 3
API_RETRY_BASE_SECONDS = 1.0
MAX_ERROR_BODY_CHARS = 1500
PREFERRED_OUTPUT_KEYS = ("answer", "text", "output", "result", "final_text")

PROCESSED_MESSAGES: "OrderedDict[str, float]" = OrderedDict()
PROCESSED_MESSAGES_LOCK = threading.Lock()
LOCAL_MOCK_PROCESS: subprocess.Popen[Any] | None = None


class BotRuntimeError(Exception):
    pass


class DifyRequestError(BotRuntimeError):
    def __init__(self, summary: str, detail: str = "") -> None:
        super().__init__(summary)
        self.summary = summary
        self.detail = detail


class FeishuRequestError(BotRuntimeError):
    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = str(code or "")


@dataclass
class ReplyMessageState:
    message_id: str
    chat_id: str
    mode: str
    card_id: str = ""
    sequence: int = 1


class FeishuStreamSender:
    _FLUSH = object()
    _STOP = object()

    def __init__(self, reply_state: ReplyMessageState) -> None:
        self.reply_state = reply_state
        self.sent_update_count = 0
        self.update_error_note = ""

        self._queue: "queue.Queue[object]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._last_enqueue_at = time.time()
        self._last_error: Exception | None = None
        self._last_msg_content = ""
        self._flushed = False
        self._length_limit_holder = ""
        self._stopped = False

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"feishu-stream-{self.reply_state.message_id}",
            daemon=True,
        )
        self._thread.start()

    def add(self, text: str, no_waiting: bool = False) -> None:
        if self._last_error is not None or self._stopped:
            return
        self._last_msg_content = text

        if no_waiting or (
            len(text) >= STREAM_QUEUE_MIN_CHARS
            and (time.time() - self._last_enqueue_at) >= STREAM_QUEUE_INTERVAL_SECONDS
        ):
            self._queue.put(text)
            self._last_enqueue_at = time.time()

    def finish(self, text: str) -> None:
        if self._last_error is not None or self._stopped:
            return
        self._last_msg_content = text
        self._queue.put(self._FLUSH)

    def cancel(self) -> None:
        if self._stopped:
            return
        self._queue.put(self._STOP)
        self._stopped = True

    def close(self) -> None:
        if self._stopped:
            return
        self._queue.put(self._STOP)
        self._stopped = True

    def wait(self, raise_on_error: bool = True) -> None:
        if self._thread is not None:
            self._thread.join()
        if raise_on_error and self._last_error is not None:
            raise self._last_error

    def _run(self) -> None:
        self._last_enqueue_at = time.time()
        self._length_limit_holder = ""

        while True:
            item = self._queue.get()
            if item is self._STOP:
                break

            if item is self._FLUSH:
                if not self._flushed and self._last_msg_content:
                    final_text = self._last_msg_content
                    if self.update_error_note:
                        final_text = f"{final_text}\n\n[提示] {self.update_error_note}"
                    safe_text = self._get_safe_send_text(final_text)
                    self._finish_message(safe_text)
                    self._flushed = True
                    self._length_limit_holder = ""
                continue

            if not isinstance(item, str) or not item:
                continue

            text = self._get_safe_send_text(item)
            text = _prepare_streaming_preview_text(text)

            try:
                started_at = time.time()
                self._send_or_update_message(text)
                elapsed_ms = int((time.time() - started_at) * 1000)
                self.sent_update_count += 1
                lark.logger.info(
                    "stream flush sent: reply_message_id=%s total_len=%s elapsed_ms=%s count=%s",
                    self.reply_state.message_id,
                    len(text),
                    elapsed_ms,
                    self.sent_update_count,
                )
                self._flushed = False
            except Exception as exc:
                self._last_error = exc
                break

        if self._last_error is None and not self._flushed and self._last_msg_content:
            try:
                final_text = self._last_msg_content
                if self.update_error_note:
                    final_text = f"{final_text}\n\n[提示] {self.update_error_note}"
                safe_text = self._get_safe_send_text(final_text)
                self._finish_message(safe_text)
                self._flushed = True
            except Exception as exc:
                self._last_error = exc

    def _send_or_update_message(self, text: str) -> None:
        if self.reply_state.mode == "card":
            if not self.reply_state.card_id:
                _create_additional_partial_card_message(self.reply_state)
            _update_partial_card_text(self.reply_state.card_id, text, self.reply_state.sequence)
            self.reply_state.sequence += 1
            return

        _update_text_message(self.reply_state.message_id, text)

    def _finish_message(self, text: str) -> None:
        if self.reply_state.mode == "card" and self.reply_state.card_id:
            _finish_partial_card_text(self.reply_state.card_id, text, self.reply_state.sequence)
            self.reply_state.sequence += 1
            self.reply_state.card_id = ""
            return

        _update_text_message(self.reply_state.message_id, text)

    def _get_safe_send_text(self, data: str) -> str:
        text = data

        while len(text) > PARTIAL_CARD_MAX_CHARS:
            if self._length_limit_holder and text.startswith(self._length_limit_holder):
                text = text[len(self._length_limit_holder) :]

            if len(text) <= PARTIAL_CARD_MAX_CHARS:
                break

            index = text.rfind("\n\n", 0, PARTIAL_CARD_MAX_CHARS)
            if index <= 0:
                index = PARTIAL_CARD_MAX_CHARS
            else:
                index += 2

            split = text[:index]
            if split.count("```") % 2 == 1:
                index2 = text.rfind("```", 0, PARTIAL_CARD_MAX_CHARS)
                if index2 > 0 and len(text) - index2 < PARTIAL_CARD_MAX_CHARS:
                    index = index2
                    split = text[:index]

            self._finish_message(split)
            self._length_limit_holder += split
            text = text[index:]

        text = re.sub(r"(?m)^\s+(?=```)", "", text)
        return re.sub(r"!\[(.*?)\]\((.*?)\)", r"\1 \2", text)


def _lark_log_level() -> Any:
    return getattr(lark.LogLevel, config.FEISHU_LOG_LEVEL, lark.LogLevel.DEBUG)


def _trim_text(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...[truncated]..."


def _trim_message_text(text: str) -> str:
    if len(text) <= FEISHU_MAX_MESSAGE_CHARS:
        return text
    suffix = "\n\n[内容过长，已截断]"
    allowed = max(0, FEISHU_MAX_MESSAGE_CHARS - len(suffix))
    return text[:allowed] + suffix


def _normalize_lark_md(text: str) -> str:
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


def _json_text_message(text: str) -> str:
    return json.dumps({"text": _trim_message_text(text)}, ensure_ascii=False)


def _json_lark_md_card_message(text: str) -> str:
    return _json_final_card_data(text)


def _card_markdown_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized or FEISHU_PLACEHOLDER_TEXT


def _card_summary_text(text: str) -> str:
    body_text = _card_markdown_text(text)
    for raw_line in body_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        plain = re.sub(r"[*`>#]", "", line).strip()
        if plain:
            return _trim_text(plain, 60)
    return FEISHU_PLACEHOLDER_TEXT


def _prepare_streaming_preview_text(text: str) -> str:
    if text.count("```") % 2 == 1:
        return f"{text}\n```"
    return text


def _json_card_id_message(card_id: str) -> str:
    return json.dumps({"type": "card", "data": {"card_id": card_id}}, ensure_ascii=False)


def _json_final_card_data(text: str) -> str:
    final_text = _trim_message_text(_normalize_lark_md(text))
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


def _json_partial_card_data() -> str:
    card = {
        "schema": "2.0",
        "config": {
            "streaming_mode": True,
            "width_mode": "fill",
            "summary": {"content": "[思考中]"},
            "streaming_config": {
                "print_frequency_ms": {"default": 25},
                "print_step": {"default": 1},
                "print_strategy": "fast",
            },
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "",
                    "element_id": FEISHU_PARTIAL_CARD_ELEMENT_ID,
                },
                {
                    "tag": "markdown",
                    "content": "<text_tag color='green'>思考中</text_tag>",
                    "element_id": FEISHU_PARTIAL_CARD_STATUS_ELEMENT_ID,
                },
            ]
        },
    }
    return json.dumps(card, ensure_ascii=False)


def _decode_bytes(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value or "")


def _response_detail(response: Any) -> str:
    raw = getattr(getattr(response, "raw", None), "content", b"")
    return _trim_text(_decode_bytes(raw), MAX_ERROR_BODY_CHARS)


def _is_message_edit_limit_error(exc: Exception) -> bool:
    return isinstance(exc, FeishuRequestError) and exc.code == "230072"


def _is_local_mock_target() -> bool:
    parsed = urlparse(config.DIFY_API_BASE_URL)
    return parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port == LOCAL_DIFY_MOCK_PORT


def _probe_local_mock() -> bool:
    request = urllib.request.Request(LOCAL_DIFY_MOCK_HEALTH_URL, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return 200 <= getattr(response, "status", 0) < 300
    except Exception:
        return False


def _stop_local_mock() -> None:
    global LOCAL_MOCK_PROCESS
    if LOCAL_MOCK_PROCESS is None:
        return
    if LOCAL_MOCK_PROCESS.poll() is None:
        LOCAL_MOCK_PROCESS.terminate()
        try:
            LOCAL_MOCK_PROCESS.wait(timeout=3)
        except subprocess.TimeoutExpired:
            LOCAL_MOCK_PROCESS.kill()
            LOCAL_MOCK_PROCESS.wait(timeout=3)
    LOCAL_MOCK_PROCESS = None


def _ensure_local_mock_running() -> None:
    global LOCAL_MOCK_PROCESS

    if not config.USE_LOCAL_DIFY_MOCK or not config.AUTO_START_LOCAL_DIFY_MOCK:
        return
    if not _is_local_mock_target():
        return
    if _probe_local_mock():
        return
    if LOCAL_MOCK_PROCESS is not None and LOCAL_MOCK_PROCESS.poll() is None:
        return

    mock_script = os.path.join(os.path.dirname(__file__), "mock_dify_server.py")
    env = os.environ.copy()
    env.setdefault("MOCK_DIFY_HOST", LOCAL_DIFY_MOCK_HOST)
    env.setdefault("MOCK_DIFY_PORT", str(LOCAL_DIFY_MOCK_PORT))
    env.setdefault("MOCK_DIFY_REQUIRE_AUTH", "true")

    lark.logger.info(
        "local mock dify is not running, starting %s on %s",
        mock_script,
        LOCAL_DIFY_MOCK_BASE_URL,
    )
    LOCAL_MOCK_PROCESS = subprocess.Popen(
        [sys.executable, mock_script],
        env=env,
        cwd=os.path.dirname(__file__),
    )

    for _ in range(20):
        if _probe_local_mock():
            atexit.register(_stop_local_mock)
            lark.logger.info("local mock dify is ready: %s", LOCAL_DIFY_MOCK_BASE_URL)
            return
        if LOCAL_MOCK_PROCESS.poll() is not None:
            break
        time.sleep(0.25)

    raise BotRuntimeError(f"failed to start local mock dify at {LOCAL_DIFY_MOCK_BASE_URL}")


def _raise_for_lark_failure(action: str, response: Any) -> Any:
    if response.success():
        return response
    raise FeishuRequestError(
        f"{action} failed: code={getattr(response, 'code', '')}, "
        f"msg={getattr(response, 'msg', '')}, "
        f"log_id={getattr(response, 'get_log_id', lambda: '')()} "
        f"body={_response_detail(response)}",
        code=str(getattr(response, "code", "") or ""),
    )


def _retry_lark_call(action: str, func):
    last_error: Exception | None = None
    for attempt in range(1, API_RETRY_TIMES + 1):
        try:
            return func()
        except Exception as exc:
            if _is_message_edit_limit_error(exc):
                raise exc
            last_error = exc
            lark.logger.error(
                "%s failed on attempt %s/%s: %s",
                action,
                attempt,
                API_RETRY_TIMES,
                exc,
                exc_info=True,
            )
            if attempt < API_RETRY_TIMES:
                time.sleep(API_RETRY_BASE_SECONDS * attempt)
    if last_error is None:
        raise FeishuRequestError(f"{action} failed with unknown error")
    raise last_error


def _should_skip_message(message_id: str, create_time_ms: int | None) -> bool:
    now_ms = int(time.time() * 1000)
    if create_time_ms and now_ms - create_time_ms > MESSAGE_MAX_AGE_MS:
        lark.logger.info("skip old message: %s", message_id)
        return True

    with PROCESSED_MESSAGES_LOCK:
        if message_id in PROCESSED_MESSAGES:
            lark.logger.info("skip duplicated message: %s", message_id)
            return True

        PROCESSED_MESSAGES[message_id] = time.time()
        while len(PROCESSED_MESSAGES) > MESSAGE_CACHE_SIZE:
            PROCESSED_MESSAGES.popitem(last=False)

    return False


def _normalize_query(raw_text: str) -> str:
    text = TEXT_MENTION_PATTERN.sub("", raw_text or "")
    return text.replace("\u00a0", " ").strip()


def _parse_text_message(message: Any) -> str:
    if message.message_type != "text":
        return ""
    try:
        content = json.loads(message.content or "{}")
    except json.JSONDecodeError as exc:
        raise BotRuntimeError(f"message content is not valid JSON: {exc}") from exc
    raw_text = content.get("text", "")
    if not isinstance(raw_text, str):
        raise BotRuntimeError(f"message content text field is invalid: {content}")
    return _normalize_query(raw_text)


def _sender_user_id(data: P2ImMessageReceiveV1) -> str:
    sender_id = getattr(getattr(data.event, "sender", None), "sender_id", None)
    for key in ("open_id", "user_id", "union_id"):
        value = getattr(sender_id, key, None)
        if value:
            return value
    return "unknown-user"


def _build_workflow_inputs(query: str) -> dict[str, object]:
    inputs: dict[str, object] = dict(config.DIFY_FIXED_INPUTS)
    for key in config.DIFY_QUERY_INPUT_KEYS:
        inputs[key] = query
    return inputs


def _dify_app_type() -> str:
    return str(config.DIFY_APP_TYPE or "workflow").strip().lower()


def _build_dify_request_body(query: str, user_identifier: str) -> dict[str, object]:
    if _dify_app_type() == "chat":
        return {
            "inputs": dict(config.DIFY_FIXED_INPUTS),
            "query": query,
            "response_mode": "streaming",
            "user": user_identifier,
        }

    return {
        "inputs": _build_workflow_inputs(query),
        "response_mode": "streaming",
        "user": user_identifier,
    }


def _format_dify_http_error(status: int, body: str) -> str:
    code = ""
    message = ""
    try:
        payload = json.loads(body)
        if isinstance(payload, dict):
            code = str(payload.get("code") or "")
            message = str(payload.get("message") or "")
    except json.JSONDecodeError:
        pass

    detail = f"HTTP {status}"
    if code:
        detail += f", code={code}"
    if message:
        detail += f", message={message}"
    if body:
        detail += f", body={_trim_text(body, MAX_ERROR_BODY_CHARS)}"
    return detail


def _iter_sse_events(response) -> Any:
    event_name = ""
    data_lines: list[str] = []

    while True:
        raw_line = response.readline()
        if not raw_line:
            if event_name or data_lines:
                yield _parse_sse_event(event_name, data_lines)
            break

        line = raw_line.decode("utf-8", errors="ignore").rstrip("\r\n")
        if not line:
            if event_name or data_lines:
                yield _parse_sse_event(event_name, data_lines)
                event_name = ""
                data_lines = []
            continue

        if line.startswith(":"):
            continue

        field, _, value = line.partition(":")
        value = value.lstrip(" ")
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)


def _parse_sse_event(event_name: str, data_lines: list[str]) -> dict[str, Any]:
    payload_text = "\n".join(data_lines).strip()
    if not payload_text:
        return {"event": event_name, "_raw": ""}
    if payload_text == "[DONE]":
        return {"event": event_name or "done", "_raw": payload_text}

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return {"event": event_name, "_raw": payload_text}

    if isinstance(payload, dict):
        if event_name and "event" not in payload:
            payload["event"] = event_name
        payload["_raw"] = payload_text
        return payload

    return {"event": event_name, "data": payload, "_raw": payload_text}


def _extract_best_output_text(outputs: Any) -> str:
    if isinstance(outputs, str):
        return outputs.strip()
    if isinstance(outputs, list):
        parts = [_extract_best_output_text(item) for item in outputs]
        parts = [part for part in parts if part]
        return "\n".join(parts)
    if isinstance(outputs, dict):
        for key in PREFERRED_OUTPUT_KEYS:
            if key in outputs:
                preferred = _extract_best_output_text(outputs[key])
                if preferred:
                    return preferred
        if len(outputs) == 1:
            only_value = next(iter(outputs.values()))
            return _extract_best_output_text(only_value)
        return json.dumps(outputs, ensure_ascii=False, indent=2)
    return ""


def _dify_stream(query: str, user_identifier: str) -> Any:
    app_type = _dify_app_type()
    if not config.DIFY_API_KEY:
        raise DifyRequestError(
            "DIFY_API_KEY is empty",
            "config.py 中没有配置 Dify API Key",
        )

    url = config.DIFY_API_BASE_URL.rstrip("/") + "/" + config.DIFY_API_PATH.lstrip("/")
    request_body = _build_dify_request_body(query, user_identifier)
    encoded_body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded_body,
        headers={
            "Authorization": f"Bearer {config.DIFY_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    lark.logger.info("start dify %s stream: url=%s body=%s", app_type, url, request_body)

    try:
        with urllib.request.urlopen(request, timeout=config.DIFY_REQUEST_TIMEOUT_SECONDS) as response:
            for event in _iter_sse_events(response):
                yield event
    except urllib.error.HTTPError as exc:
        body = _decode_bytes(exc.read())
        raise DifyRequestError(
            f"Dify {app_type} HTTP error",
            _format_dify_http_error(exc.code, body),
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.timeout):
            raise DifyRequestError(
                f"Dify {app_type} timeout",
                f"socket timeout after {config.DIFY_REQUEST_TIMEOUT_SECONDS}s",
            ) from exc
        raise DifyRequestError(f"Dify {app_type} request failed", str(exc.reason)) from exc
    except socket.timeout as exc:
        raise DifyRequestError(
            f"Dify {app_type} timeout",
            f"socket timeout after {config.DIFY_REQUEST_TIMEOUT_SECONDS}s",
        ) from exc


def _format_error_message(summary: str, detail: str = "") -> str:
    text = f"[错误] {summary}"
    if detail:
        text += f"\n{_trim_text(detail, MAX_ERROR_BODY_CHARS)}"
    return text


def _format_finished_status(data: dict[str, Any]) -> str:
    status = str(data.get("status") or "unknown")
    error = str(data.get("error") or "").strip()
    elapsed = data.get("elapsed_time")
    total_tokens = data.get("total_tokens")
    pieces = [f"status={status}"]
    if elapsed is not None:
        pieces.append(f"elapsed_time={elapsed}")
    if total_tokens is not None:
        pieces.append(f"total_tokens={total_tokens}")
    if error:
        pieces.append(f"error={error}")
    return ", ".join(pieces)


def _format_stream_error_event(event: dict[str, Any]) -> str:
    code = str(event.get("code") or "")
    message = str(event.get("message") or "").strip()
    status = str(event.get("status") or "").strip()
    pieces = []
    if status:
        pieces.append(f"status={status}")
    if code:
        pieces.append(f"code={code}")
    if message:
        pieces.append(f"message={message}")
    if not pieces:
        pieces.append(_trim_text(event.get("_raw") or event, MAX_ERROR_BODY_CHARS))
    return ", ".join(pieces)


def _extract_chat_event_text(event: dict[str, Any]) -> str:
    direct_text = event.get("answer")
    if isinstance(direct_text, str) and direct_text:
        return direct_text

    data = event.get("data")
    if isinstance(data, dict):
        data_answer = data.get("answer")
        if isinstance(data_answer, str) and data_answer:
            return data_answer

        outputs = data.get("outputs")
        text = _extract_best_output_text(outputs)
        if text:
            return text

    outputs = event.get("outputs")
    text = _extract_best_output_text(outputs)
    if text:
        return text

    return ""


def _extract_stream_text(event_name: str, event: dict[str, Any]) -> tuple[str, bool]:
    if event_name == "text_chunk":
        data = event.get("data") or {}
        text = data.get("text")
        return (text, False) if isinstance(text, str) else ("", False)

    if event_name in {"message", "agent_message"}:
        text = _extract_chat_event_text(event)
        return (text, False) if text else ("", False)

    if event_name == "message_replace":
        text = _extract_chat_event_text(event)
        return (text, True) if text else ("", False)

    return "", False


def _reply_text(message: Any, text: str, uuid_suffix: str) -> str:
    content = _json_text_message(text)

    def _reply() -> str:
        response = client.im.v1.message.reply(
            ReplyMessageRequest.builder()
            .message_id(message.message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type(FEISHU_TEXT_MESSAGE_TYPE)
                .reply_in_thread(FEISHU_REPLY_IN_THREAD)
                .uuid(f"{message.message_id}-{uuid_suffix}")
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("reply text message", response)
        return getattr(response.data, "message_id", "") or ""

    try:
        return _retry_lark_call("reply text message", _reply)
    except Exception as reply_error:
        lark.logger.error("reply message failed, fallback to chat create: %s", reply_error, exc_info=True)

    def _create() -> str:
        response = client.im.v1.message.create(
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(message.chat_id)
                .msg_type(FEISHU_TEXT_MESSAGE_TYPE)
                .content(content)
                .uuid(f"{message.message_id}-{uuid_suffix}-create")
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("create text message", response)
        return getattr(response.data, "message_id", "") or ""

    return _retry_lark_call("create text message", _create)


def _reply_lark_md_card(message: Any, text: str, uuid_suffix: str) -> str:
    content = _json_lark_md_card_message(text)

    def _reply() -> str:
        response = client.im.v1.message.reply(
            ReplyMessageRequest.builder()
            .message_id(message.message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type(FEISHU_CARD_MESSAGE_TYPE)
                .reply_in_thread(FEISHU_REPLY_IN_THREAD)
                .uuid(f"{message.message_id}-{uuid_suffix}")
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("reply lark_md card message", response)
        return getattr(response.data, "message_id", "") or ""

    try:
        return _retry_lark_call("reply lark_md card message", _reply)
    except Exception as reply_error:
        lark.logger.warning("reply lark_md card message failed, fallback to text: %s", reply_error, exc_info=True)
        return _reply_text(message, text, f"{uuid_suffix}-text-fallback")


def _reply_user_message(message: Any, text: str, uuid_suffix: str) -> str:
    return _reply_lark_md_card(message, text, uuid_suffix)


def _update_text_message(message_id: str, text: str) -> None:
    content = _json_text_message(text)

    def _update() -> None:
        response = client.im.v1.message.update(
            UpdateMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                UpdateMessageRequestBody.builder()
                .msg_type(FEISHU_TEXT_MESSAGE_TYPE)
                .content(content)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("update text message", response)

    _retry_lark_call("update text message", _update)


def _create_text_message(chat_id: str, text: str, uuid_suffix: str) -> str:
    content = _json_text_message(text)

    def _create() -> str:
        response = client.im.v1.message.create(
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(FEISHU_TEXT_MESSAGE_TYPE)
                .content(content)
                .uuid(uuid_suffix)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("create text message", response)
        return getattr(response.data, "message_id", "") or ""

    return _retry_lark_call("create text message", _create)


def _create_lark_md_card_message(chat_id: str, text: str, uuid_suffix: str) -> str:
    content = _json_lark_md_card_message(text)

    def _create() -> str:
        response = client.im.v1.message.create(
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(FEISHU_CARD_MESSAGE_TYPE)
                .content(content)
                .uuid(uuid_suffix)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("create lark_md card message", response)
        return getattr(response.data, "message_id", "") or ""

    return _retry_lark_call("create lark_md card message", _create)


def _create_partial_card_message() -> str:
    data = _json_partial_card_data()

    def _create() -> str:
        response = client.cardkit.v1.card.create(
            CreateCardRequest.builder()
            .request_body(
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(data)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("create partial card", response)
        return getattr(response.data, "card_id", "") or ""

    return _retry_lark_call("create partial card", _create)


def _reply_card_id_message(message: Any, card_id: str, uuid_suffix: str) -> str:
    content = _json_card_id_message(card_id)

    def _reply() -> str:
        response = client.im.v1.message.reply(
            ReplyMessageRequest.builder()
            .message_id(message.message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type(FEISHU_CARD_MESSAGE_TYPE)
                .reply_in_thread(FEISHU_REPLY_IN_THREAD)
                .uuid(f"{message.message_id}-{uuid_suffix}")
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("reply card_id message", response)
        return getattr(response.data, "message_id", "") or ""

    try:
        return _retry_lark_call("reply card_id message", _reply)
    except Exception as reply_error:
        lark.logger.error("reply card_id message failed, fallback to chat create: %s", reply_error, exc_info=True)

    def _create() -> str:
        response = client.im.v1.message.create(
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(message.chat_id)
                .msg_type(FEISHU_CARD_MESSAGE_TYPE)
                .content(content)
                .uuid(f"{message.message_id}-{uuid_suffix}-create")
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("create card_id message", response)
        return getattr(response.data, "message_id", "") or ""

    return _retry_lark_call("create card_id message", _create)


def _create_card_id_message(chat_id: str, card_id: str, uuid_suffix: str) -> str:
    content = _json_card_id_message(card_id)

    def _create() -> str:
        response = client.im.v1.message.create(
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(FEISHU_CARD_MESSAGE_TYPE)
                .content(content)
                .uuid(uuid_suffix)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("create card_id message", response)
        return getattr(response.data, "message_id", "") or ""

    return _retry_lark_call("create card_id message", _create)


def _update_partial_card_text(card_id: str, text: str, sequence: int) -> None:
    content = _card_markdown_text(text)

    def _update() -> None:
        response = client.cardkit.v1.card_element.content(
            ContentCardElementRequest.builder()
            .card_id(card_id)
            .element_id(FEISHU_PARTIAL_CARD_ELEMENT_ID)
            .request_body(
                ContentCardElementRequestBody.builder()
                .content(content)
                .sequence(sequence)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("update partial card text", response)

    _retry_lark_call("update partial card text", _update)


def _finish_partial_card_text(card_id: str, text: str, sequence: int) -> None:
    final_card_data = _json_final_card_data(text)

    def _finish() -> None:
        response = client.cardkit.v1.card.update(
            UpdateCardRequest.builder()
            .card_id(card_id)
            .request_body(
                UpdateCardRequestBody.builder()
                .card(
                    Card.builder()
                    .type("card_json")
                    .data(final_card_data)
                    .build()
                )
                .sequence(sequence)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("finish partial card text", response)

    _retry_lark_call("finish partial card text", _finish)


def _switch_reply_to_text(reply_state: ReplyMessageState, text: str) -> None:
    old_message_id = reply_state.message_id
    new_message_id = _create_text_message(
        reply_state.chat_id,
        text,
        f"{old_message_id}-text-fallback",
    )
    reply_state.message_id = new_message_id
    reply_state.mode = "text"
    reply_state.card_id = ""
    try:
        _delete_message(old_message_id)
    except Exception as exc:
        lark.logger.warning("delete card message after text fallback failed: message_id=%s err=%s", old_message_id, exc)


def _replace_reply_with_lark_md_card(reply_state: ReplyMessageState, text: str) -> None:
    old_message_id = reply_state.message_id
    new_message_id = _create_lark_md_card_message(
        reply_state.chat_id,
        text,
        f"{old_message_id}-card-fallback",
    )
    reply_state.message_id = new_message_id
    reply_state.mode = "final_card"
    reply_state.card_id = ""
    try:
        _delete_message(old_message_id)
    except Exception as exc:
        lark.logger.warning("delete old reply after lark_md card replacement failed: message_id=%s err=%s", old_message_id, exc)


def _reply_stream_message(message: Any, text: str, uuid_suffix: str) -> ReplyMessageState:
    try:
        card_id = _create_partial_card_message()
        if not card_id:
            raise FeishuRequestError("create partial card succeeded but card_id is empty")
        message_id = _reply_card_id_message(message, card_id, uuid_suffix)
        return ReplyMessageState(
            message_id=message_id,
            chat_id=message.chat_id,
            mode="card",
            card_id=card_id,
            sequence=1,
        )
    except Exception as exc:
        lark.logger.warning("reply partial card message failed, fallback to text: %s", exc, exc_info=True)

    message_id = _reply_text(message, text, uuid_suffix)
    return ReplyMessageState(message_id=message_id, chat_id=message.chat_id, mode="text")


def _create_additional_partial_card_message(reply_state: ReplyMessageState) -> None:
    card_id = _create_partial_card_message()
    if not card_id:
        raise FeishuRequestError("create additional partial card succeeded but card_id is empty")

    message_id = _create_card_id_message(
        reply_state.chat_id,
        card_id,
        f"{reply_state.message_id}-segment-{reply_state.sequence}",
    )
    reply_state.message_id = message_id
    reply_state.card_id = card_id


def _update_reply_message(reply_state: ReplyMessageState, text: str, streaming_mode: bool) -> None:
    if reply_state.mode == "card":
        try:
            if not reply_state.card_id:
                _create_additional_partial_card_message(reply_state)

            if streaming_mode:
                _update_partial_card_text(reply_state.card_id, text, reply_state.sequence)
            else:
                _finish_partial_card_text(reply_state.card_id, text, reply_state.sequence)
                reply_state.card_id = ""
            reply_state.sequence += 1
            return
        except Exception as exc:
            lark.logger.warning(
                "update partial card message failed, fallback to text: message_id=%s err=%s",
                reply_state.message_id,
                exc,
                exc_info=True,
            )
            if streaming_mode:
                _switch_reply_to_text(reply_state, text)
            else:
                _replace_reply_with_lark_md_card(reply_state, text)
            return

    _update_text_message(reply_state.message_id, text)


def _delete_message(message_id: str) -> None:
    def _delete() -> None:
        response = client.im.v1.message.delete(
            DeleteMessageRequest.builder()
            .message_id(message_id)
            .build()
        )
        _raise_for_lark_failure("delete message", response)

    _retry_lark_call("delete message", _delete)


def _add_ack_reaction(message_id: str) -> None:
    def _create_reaction() -> None:
        response = client.im.v1.message_reaction.create(
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(
                    Emoji.builder()
                    .emoji_type(FEISHU_ACK_REACTION_EMOJI_TYPE)
                    .build()
                )
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("create message reaction", response)

    _retry_lark_call("create message reaction", _create_reaction)


def _stream_dify_to_message(
    reply_state: ReplyMessageState,
    query: str,
    user_identifier: str,
    extra_notice: str = "",
) -> None:
    app_type = _dify_app_type()
    accumulated_text = ""
    stream_finished = False
    stream_sender = FeishuStreamSender(reply_state)
    stream_sender.start()

    try:
        for event in _dify_stream(query, user_identifier):
            event_name = str(event.get("event") or "")
            lark.logger.debug("dify stream event: %s", event_name or event)

            if event_name == "error":
                raise DifyRequestError(f"Dify {app_type} stream error", _format_stream_error_event(event))

            chunk_text, replace_text = _extract_stream_text(event_name, event)
            if chunk_text:
                if replace_text:
                    accumulated_text = chunk_text
                else:
                    accumulated_text += chunk_text
                lark.logger.info(
                    "stream chunk received: reply_message_id=%s chunk_len=%s total_len=%s replace=%s",
                    reply_state.message_id,
                    len(chunk_text),
                    len(accumulated_text),
                    replace_text,
                )
                stream_sender.add(accumulated_text)
                continue

            if event_name in {"workflow_finished", "message_end"}:
                stream_finished = True
                data = event.get("data") or event
                if event_name == "workflow_finished" and not accumulated_text:
                    accumulated_text = _extract_best_output_text(data.get("outputs"))
                if event_name == "message_end" and not accumulated_text:
                    accumulated_text = _extract_chat_event_text(event)

                final_text = accumulated_text or FEISHU_EMPTY_RESULT_TEXT
                if event_name == "workflow_finished" and str(data.get("status") or "") != "succeeded":
                    final_text = (
                        f"{final_text}\n\n"
                        f"{_format_error_message('Dify workflow ended abnormally', _format_finished_status(data))}"
                    ).strip()
                if extra_notice:
                    final_text = f"{final_text}\n\n[提示] {extra_notice}"

                stream_sender.finish(final_text)
                stream_sender.close()
                stream_sender.wait()

                lark.logger.info(
                    "dify stream finished: app_type=%s reply_message_id=%s event=%s status=%s total_len=%s updates=%s",
                    app_type,
                    reply_state.message_id,
                    event_name,
                    data.get("status"),
                    len(final_text),
                    stream_sender.sent_update_count,
                )
                break

            if event_name in {
                "workflow_started",
                "node_started",
                "node_finished",
                "agent_thought",
                "message_file",
                "ping",
                "tts_message",
                "tts_message_end",
            }:
                continue

            lark.logger.warning("unknown dify event: %s", event.get("_raw") or event)

        if not stream_finished:
            stream_sender.close()
            stream_sender.wait(raise_on_error=False)
            raise DifyRequestError(
                "Dify stream closed unexpectedly",
                "stream ended before final event",
            )
    except Exception:
        if not stream_finished:
            stream_sender.close()
            stream_sender.wait(raise_on_error=False)
        raise


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    message_id = message.message_id or ""
    create_time_ms = None
    try:
        create_time_ms = int(message.create_time)
    except (TypeError, ValueError):
        create_time_ms = None

    lark.logger.info(
        "receive message: message_id=%s chat_id=%s type=%s",
        message_id,
        message.chat_id,
        message.message_type,
    )

    if not message_id or _should_skip_message(message_id, create_time_ms):
        return

    reaction_error = ""
    try:
        _add_ack_reaction(message_id)
    except Exception as exc:
        reaction_error = str(exc)
        lark.logger.error("ack reaction failed: %s", reaction_error, exc_info=True)

    try:
        query = _parse_text_message(message)
    except Exception as exc:
        error_text = _format_error_message("消息解析失败", str(exc))
        if reaction_error:
            error_text += f"\n[提示] 表情回复失败: {_trim_text(reaction_error, 300)}"
        _reply_user_message(message, error_text, "parse-error")
        return

    if message.message_type != "text":
        text = _format_error_message("暂不支持该消息类型", f"message_type={message.message_type}")
        if reaction_error:
            text += f"\n[提示] 表情回复失败: {_trim_text(reaction_error, 300)}"
        _reply_user_message(message, text, "unsupported")
        return

    if not query:
        text = _format_error_message("消息内容为空", "未转发到 Dify")
        if reaction_error:
            text += f"\n[提示] 表情回复失败: {_trim_text(reaction_error, 300)}"
        _reply_user_message(message, text, "empty")
        return

    reply_state = _reply_stream_message(message, FEISHU_PLACEHOLDER_TEXT, "placeholder")
    if not reply_state.message_id:
        raise FeishuRequestError("placeholder message created but message_id is empty")

    user_identifier = _sender_user_id(data)

    try:
        _stream_dify_to_message(
            reply_state,
            query,
            user_identifier,
            reaction_error,
        )
    except DifyRequestError as exc:
        lark.logger.error("dify request failed: %s | %s", exc.summary, exc.detail)
        error_text = _format_error_message(exc.summary, exc.detail)
        if reaction_error:
            error_text += f"\n[提示] 表情回复失败: {_trim_text(reaction_error, 300)}"
        _update_reply_message(reply_state, error_text, streaming_mode=False)
    except Exception as exc:
        lark.logger.error("unexpected handler error: %s", exc, exc_info=True)
        error_text = _format_error_message("处理消息时发生未预期错误", str(exc))
        if reaction_error:
            error_text += f"\n[提示] 表情回复失败: {_trim_text(reaction_error, 300)}"
        _update_reply_message(reply_state, error_text, streaming_mode=False)


def do_p2_im_message_reaction_created_v1(data: P2ImMessageReactionCreatedV1) -> None:
    event = data.event
    reaction_type = getattr(getattr(event, "reaction_type", None), "emoji_type", "")
    lark.logger.debug(
        "ignore reaction event: message_id=%s emoji=%s operator_type=%s",
        getattr(event, "message_id", ""),
        reaction_type,
        getattr(event, "operator_type", ""),
    )


def do_p2_im_message_message_read_v1(data: P2ImMessageMessageReadV1) -> None:
    event = data.event
    message_id_list = getattr(event, "message_id_list", None) or []
    lark.logger.debug("ignore message_read event: count=%s", len(message_id_list))


event_handler = (
    lark.EventDispatcherHandler.builder("", "")
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1)
    .register_p2_im_message_reaction_created_v1(do_p2_im_message_reaction_created_v1)
    .register_p2_im_message_message_read_v1(do_p2_im_message_message_read_v1)
    .build()
)


client = (
    lark.Client.builder()
    .app_id(config.FEISHU_APP_ID)
    .app_secret(config.FEISHU_APP_SECRET)
    .log_level(_lark_log_level())
    .build()
)

ws_client = lark.ws.Client(
    config.FEISHU_APP_ID,
    config.FEISHU_APP_SECRET,
    event_handler=event_handler,
    log_level=_lark_log_level(),
)


def main() -> None:
    _ensure_local_mock_running()
    lark.logger.info("starting Feishu bot with dify page %s", config.DIFY_APP_PAGE_URL)
    ws_client.start()


if __name__ == "__main__":
    main()
