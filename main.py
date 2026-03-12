from __future__ import annotations

import atexit
from concurrent.futures import Future, ThreadPoolExecutor
import contextlib
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
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    Emoji,
    P2ImMessageMessageReadV1,
    P2ImMessageReceiveV1,
    P2ImMessageReactionCreatedV1,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

import config

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


TEXT_MENTION_PATTERN = re.compile(r"@_user_\d+\s*")
FEISHU_REPLY_IN_THREAD = False
FEISHU_ACK_REACTION_EMOJI_TYPE = "DONE"
FEISHU_PLACEHOLDER_TEXT = "正在处理中，请稍候..."
FEISHU_EMPTY_RESULT_TEXT = "工作流已结束，但没有返回可展示的文本结果。"
FEISHU_MAX_MESSAGE_CHARS = 28000
FEISHU_CARD_MESSAGE_TYPE = "interactive"

LOCAL_DIFY_MOCK_HOST = "127.0.0.1"
LOCAL_DIFY_MOCK_PORT = 18080
LOCAL_DIFY_MOCK_BASE_URL = f"http://{LOCAL_DIFY_MOCK_HOST}:{LOCAL_DIFY_MOCK_PORT}"
LOCAL_DIFY_MOCK_HEALTH_URL = f"{LOCAL_DIFY_MOCK_BASE_URL}/healthz"

MESSAGE_MAX_AGE_MS = 120000
MESSAGE_CACHE_SIZE = 1000
STREAM_QUEUE_MIN_CHARS = 10
STREAM_QUEUE_INTERVAL_SECONDS = 0.2
API_RETRY_TIMES = 3
API_RETRY_BASE_SECONDS = 1.0
MAX_ERROR_BODY_CHARS = 1500
PREFERRED_OUTPUT_KEYS = ("answer", "text", "output", "result", "final_text")
MESSAGE_WORKER_COUNT = 3
CONVERSATION_CACHE_SIZE = 1000
RESET_CONTEXT_COMMANDS = {"/reset", "/new", "/clear", "清空上下文", "重置上下文", "新建会话"}

PROCESSED_MESSAGES: "OrderedDict[str, float]" = OrderedDict()
PROCESSED_MESSAGES_LOCK = threading.Lock()
LOCAL_MOCK_PROCESS: subprocess.Popen[Any] | None = None
MESSAGE_EXECUTOR = ThreadPoolExecutor(max_workers=MESSAGE_WORKER_COUNT, thread_name_prefix="message-worker")
MESSAGE_FUTURES: set[Future[Any]] = set()
MESSAGE_FUTURES_LOCK = threading.Lock()
INSTANCE_LOCK_FILE = os.path.join(os.path.dirname(__file__), "bot.lock")
INSTANCE_LOCK_HANDLE: Any | None = None
CONVERSATION_CACHE: "OrderedDict[str, str]" = OrderedDict()
CONVERSATION_CACHE_LOCK = threading.Lock()


def _current_build_id() -> str:
    try:
        output = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        build_id = output.strip()
        return build_id or "unknown"
    except Exception:
        return "unknown"


def _shutdown_message_executor() -> None:
    try:
        MESSAGE_EXECUTOR.shutdown(wait=False)
    except Exception:
        pass


def _release_instance_lock() -> None:
    global INSTANCE_LOCK_HANDLE
    if INSTANCE_LOCK_HANDLE is None:
        return
    if fcntl is not None:
        with contextlib.suppress(Exception):
            fcntl.flock(INSTANCE_LOCK_HANDLE.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(Exception):
        INSTANCE_LOCK_HANDLE.close()
    INSTANCE_LOCK_HANDLE = None


atexit.register(_shutdown_message_executor)
atexit.register(_release_instance_lock)


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
        _patch_lark_md_card_message(self.reply_state.message_id, text)

    def _finish_message(self, text: str) -> None:
        _patch_lark_md_card_message(self.reply_state.message_id, text)

    def _get_safe_send_text(self, data: str) -> str:
        text = re.sub(r"(?m)^\s+(?=```)", "", data)
        text = re.sub(r"!\[(.*?)\]\((.*?)\)", r"\1 \2", text)
        return _trim_message_text(text)


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


def _message_reply_uuid(message_id: str) -> str:
    return f"{message_id}-reply"


def _json_lark_md_card_message(text: str) -> str:
    return _json_final_card_data(text)


def _prepare_streaming_preview_text(text: str) -> str:
    if text.count("```") % 2 == 1:
        return f"{text}\n```"
    return text


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


def _acquire_instance_lock() -> None:
    global INSTANCE_LOCK_HANDLE
    if INSTANCE_LOCK_HANDLE is not None:
        return

    lock_handle = open(INSTANCE_LOCK_FILE, "a+", encoding="utf-8")
    if fcntl is None:
        INSTANCE_LOCK_HANDLE = lock_handle
        return

    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_handle.close()
        raise BotRuntimeError(f"another bot instance is already running: {INSTANCE_LOCK_FILE}") from exc

    lock_handle.seek(0)
    lock_handle.truncate()
    lock_handle.write(f"pid={os.getpid()}\n")
    lock_handle.flush()
    INSTANCE_LOCK_HANDLE = lock_handle


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


def _conversation_session_key(message: Any, user_identifier: str) -> str:
    chat_id = getattr(message, "chat_id", "") or "unknown-chat"
    return f"{chat_id}:{user_identifier}"


def _get_conversation_id(session_key: str) -> str:
    with CONVERSATION_CACHE_LOCK:
        conversation_id = CONVERSATION_CACHE.get(session_key, "")
        if conversation_id:
            CONVERSATION_CACHE.move_to_end(session_key)
        return conversation_id


def _set_conversation_id(session_key: str, conversation_id: str) -> None:
    if not conversation_id:
        return
    with CONVERSATION_CACHE_LOCK:
        CONVERSATION_CACHE[session_key] = conversation_id
        CONVERSATION_CACHE.move_to_end(session_key)
        while len(CONVERSATION_CACHE) > CONVERSATION_CACHE_SIZE:
            CONVERSATION_CACHE.popitem(last=False)


def _clear_conversation_id(session_key: str) -> bool:
    with CONVERSATION_CACHE_LOCK:
        return CONVERSATION_CACHE.pop(session_key, None) is not None


def _build_workflow_inputs(query: str) -> dict[str, object]:
    inputs: dict[str, object] = dict(config.DIFY_FIXED_INPUTS)
    for key in config.DIFY_QUERY_INPUT_KEYS:
        inputs[key] = query
    return inputs


def _dify_app_type() -> str:
    return str(config.DIFY_APP_TYPE or "workflow").strip().lower()


def _build_dify_request_body(query: str, user_identifier: str, conversation_id: str = "") -> dict[str, object]:
    if _dify_app_type() == "chat":
        body: dict[str, object] = {
            "inputs": dict(config.DIFY_FIXED_INPUTS),
            "query": query,
            "response_mode": "streaming",
            "user": user_identifier,
        }
        if conversation_id:
            body["conversation_id"] = conversation_id
        return body

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


def _dify_stream(query: str, user_identifier: str, conversation_id: str = "") -> Any:
    app_type = _dify_app_type()
    if not config.DIFY_API_KEY:
        raise DifyRequestError(
            "DIFY_API_KEY is empty",
            "config.py 中没有配置 Dify API Key",
        )

    url = config.DIFY_API_BASE_URL.rstrip("/") + "/" + config.DIFY_API_PATH.lstrip("/")
    request_body = _build_dify_request_body(query, user_identifier, conversation_id)
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


def _extract_event_conversation_id(event: dict[str, Any]) -> str:
    value = event.get("conversation_id")
    if isinstance(value, str) and value:
        return value

    data = event.get("data")
    if isinstance(data, dict):
        nested_value = data.get("conversation_id")
        if isinstance(nested_value, str) and nested_value:
            return nested_value

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


def _reply_lark_md_card(message: Any, text: str, uuid_suffix: str) -> str:
    content = _json_lark_md_card_message(text)
    reply_uuid = _message_reply_uuid(message.message_id or "")

    def _reply() -> str:
        response = client.im.v1.message.reply(
            ReplyMessageRequest.builder()
            .message_id(message.message_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .content(content)
                .msg_type(FEISHU_CARD_MESSAGE_TYPE)
                .reply_in_thread(FEISHU_REPLY_IN_THREAD)
                .uuid(reply_uuid)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("reply lark_md card message", response)
        return getattr(response.data, "message_id", "") or ""

    return _retry_lark_call("reply lark_md card message", _reply)


def _reply_user_message(message: Any, text: str, uuid_suffix: str) -> str:
    return _reply_lark_md_card(message, text, uuid_suffix)


def _patch_lark_md_card_message(message_id: str, text: str) -> None:
    content = _json_lark_md_card_message(text)

    def _patch() -> None:
        response = client.im.v1.message.patch(
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(
                PatchMessageRequestBody.builder()
                .content(content)
                .build()
            )
            .build()
        )
        _raise_for_lark_failure("patch lark_md card message", response)

    _retry_lark_call("patch lark_md card message", _patch)


def _reply_stream_message(message: Any, text: str, uuid_suffix: str) -> ReplyMessageState:
    message_id = _reply_lark_md_card(message, text, f"{uuid_suffix}-final-card")
    return ReplyMessageState(message_id=message_id, chat_id=message.chat_id)


def _update_reply_message(reply_state: ReplyMessageState, text: str, streaming_mode: bool) -> None:
    _patch_lark_md_card_message(reply_state.message_id, text)


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
    session_key: str,
    extra_notice: str = "",
) -> None:
    app_type = _dify_app_type()
    accumulated_text = ""
    stream_finished = False
    stream_sender = FeishuStreamSender(reply_state)
    stream_sender.start()
    current_conversation_id = _get_conversation_id(session_key) if app_type == "chat" else ""

    try:
        for event in _dify_stream(query, user_identifier, current_conversation_id):
            event_name = str(event.get("event") or "")
            lark.logger.debug("dify stream event: %s", event_name or event)

            new_conversation_id = _extract_event_conversation_id(event)
            if app_type == "chat" and new_conversation_id and new_conversation_id != current_conversation_id:
                current_conversation_id = new_conversation_id
                _set_conversation_id(session_key, current_conversation_id)
                lark.logger.info("dify conversation updated: session=%s conversation_id=%s", session_key, current_conversation_id)

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


def _on_message_task_done(future: Future[Any]) -> None:
    with MESSAGE_FUTURES_LOCK:
        MESSAGE_FUTURES.discard(future)
        remaining = len(MESSAGE_FUTURES)

    try:
        future.result()
    except Exception as exc:
        lark.logger.error("background message task failed: %s", exc, exc_info=True)
    else:
        lark.logger.info("background message task finished: remaining=%s", remaining)


def _submit_message_task(data: P2ImMessageReceiveV1) -> None:
    try:
        future = MESSAGE_EXECUTOR.submit(_handle_received_message, data)
    except RuntimeError:
        lark.logger.warning("message executor unavailable, fallback to synchronous handling")
        _handle_received_message(data)
        return

    with MESSAGE_FUTURES_LOCK:
        MESSAGE_FUTURES.add(future)
        active_count = len(MESSAGE_FUTURES)

    future.add_done_callback(_on_message_task_done)
    lark.logger.info("message task submitted: active=%s max_workers=%s", active_count, MESSAGE_WORKER_COUNT)


def _handle_received_message(data: P2ImMessageReceiveV1) -> None:
    message = data.event.message
    reaction_error = ""
    try:
        _add_ack_reaction(message.message_id or "")
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

    user_identifier = _sender_user_id(data)
    session_key = _conversation_session_key(message, user_identifier)

    if _dify_app_type() == "chat" and query.strip() in RESET_CONTEXT_COMMANDS:
        existed = _clear_conversation_id(session_key)
        reset_text = "已清空当前会话上下文。接下来我会从新的对话开始回答。"
        if not existed:
            reset_text = "当前没有可清空的会话上下文。接下来我会从新的对话开始回答。"
        if reaction_error:
            reset_text += f"\n\n[提示] 表情回复失败: {_trim_text(reaction_error, 300)}"
        _reply_user_message(message, reset_text, "reset-context")
        return

    reply_state = _reply_stream_message(message, FEISHU_PLACEHOLDER_TEXT, "placeholder")
    if not reply_state.message_id:
        raise FeishuRequestError("placeholder message created but message_id is empty")

    try:
        _stream_dify_to_message(
            reply_state,
            query,
            user_identifier,
            session_key,
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

    _submit_message_task(data)


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
    _acquire_instance_lock()
    _ensure_local_mock_running()
    if (
        config.TEST_CONFIG["feishu_app_id"] == config.ONLINE_CONFIG["feishu_app_id"]
        and config.TEST_CONFIG["feishu_app_secret"] == config.ONLINE_CONFIG["feishu_app_secret"]
    ):
        raise BotRuntimeError(
            "TEST 和 ONLINE 不能共用同一个飞书应用。"
            "请为测试环境和正式环境配置不同的 feishu_app_id / feishu_app_secret，"
            "否则无法保证不会重复消费同一条消息。"
        )
    lark.logger.info("bot build id: %s", _current_build_id())
    lark.logger.info("starting Feishu bot with dify page %s", config.DIFY_APP_PAGE_URL)
    ws_client.start()


if __name__ == "__main__":
    main()
