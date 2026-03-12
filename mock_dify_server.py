from __future__ import annotations

import json
import os
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


HOST = os.getenv("MOCK_DIFY_HOST", "127.0.0.1")
PORT = int(os.getenv("MOCK_DIFY_PORT", "18080"))
CHUNK_DELAY_SECONDS = float(os.getenv("MOCK_DIFY_CHUNK_DELAY_SECONDS", "0.05"))
CHUNK_SIZE = int(os.getenv("MOCK_DIFY_CHUNK_SIZE", "4"))
REQUIRE_AUTH = os.getenv("MOCK_DIFY_REQUIRE_AUTH", "true").lower() == "true"

WORKFLOW_ID = "mock-workflow"
API_PREFIX = "/v1"


def _now_ts() -> int:
    return int(time.time())


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE) -> list[str]:
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)] or [""]


def _extract_query(inputs: Any) -> str:
    if not isinstance(inputs, dict):
        return ""
    preferred_keys = ("query", "text", "message", "user_query")
    for key in preferred_keys:
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for value in inputs.values():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _scenario_from_query(query: str) -> tuple[str, str]:
    stripped = query.strip()
    if not stripped:
        return "default", ""
    first_token, _, rest = stripped.partition(" ")
    if first_token in {"/empty", "/fail", "/close", "/slow", "/long"}:
        return first_token[1:], rest.strip()
    return "default", stripped


def _build_answer_text(query: str, user: str, inputs: Any, scenario: str) -> str:
    normalized_query = query or "（空消息）"
    pretty_inputs = json.dumps(inputs, ensure_ascii=False, sort_keys=True)
    base = (
        "这是本地 mock Dify 的流式响应。\n"
        f"user: {user}\n"
        f"query: {normalized_query}\n"
        f"inputs: {pretty_inputs}\n\n"
        "如果你在飞书里看到了这段内容，说明消息转发、SSE 解析和飞书消息更新链路都已经通了。"
    )
    if scenario == "long":
        return "\n\n".join([base] * 8)
    return base


class MockDifyHandler(BaseHTTPRequestHandler):
    server_version = "MockDify/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        message = format % args
        print(f"[mock-dify] {self.address_string()} - {message}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/healthz"}:
            self._send_json(HTTPStatus.OK, {"status": "ok", "service": "mock-dify"})
            return
        if parsed.path == "/mock-workflow":
            self._send_json(
                HTTPStatus.OK,
                {
                    "workflow_id": WORKFLOW_ID,
                    "message": "This is a local mock workflow page placeholder.",
                },
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if REQUIRE_AUTH and not self.headers.get("Authorization", "").startswith("Bearer "):
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"code": "unauthorized", "message": "Authorization header is required"},
            )
            return

        if parsed.path == f"{API_PREFIX}/workflows/run":
            self._handle_workflow_run()
            return
        if parsed.path == f"{API_PREFIX}/chat-messages":
            self._handle_chat_messages()
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"code": "not_found", "message": "Not found"})

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"request body is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _handle_workflow_run(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"code": "invalid_param", "message": str(exc)})
            return

        inputs = payload.get("inputs") or {}
        user = str(payload.get("user") or "mock-user")
        response_mode = str(payload.get("response_mode") or "blocking")
        query = _extract_query(inputs)
        scenario, scenario_query = _scenario_from_query(query)
        visible_query = scenario_query if scenario != "default" else query
        answer_text = _build_answer_text(visible_query, user, inputs, scenario)

        workflow_run_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        created_at = _now_ts()

        if response_mode == "blocking":
            time.sleep(CHUNK_DELAY_SECONDS)
            finished_at = _now_ts()
            self._send_json(
                HTTPStatus.OK,
                {
                    "workflow_run_id": workflow_run_id,
                    "task_id": task_id,
                    "data": {
                        "id": workflow_run_id,
                        "workflow_id": WORKFLOW_ID,
                        "status": "succeeded",
                        "created_at": created_at,
                        "finished_at": finished_at,
                        "outputs": {"answer": answer_text},
                        "error": "",
                        "elapsed_time": round(finished_at - created_at, 3),
                        "total_tokens": max(1, len(answer_text) // 4),
                        "total_steps": 3,
                    },
                },
            )
            return

        self._send_sse_headers()
        started_at = time.time()

        self._send_sse_event(
            "workflow_started",
            {
                "event": "workflow_started",
                "workflow_run_id": workflow_run_id,
                "task_id": task_id,
                "data": {
                    "id": workflow_run_id,
                    "workflow_id": WORKFLOW_ID,
                    "status": "running",
                    "created_at": created_at,
                },
            },
        )
        self._sleep_for_scenario(scenario)
        self._send_sse_event("node_started", {"event": "node_started", "task_id": task_id, "data": {"node_id": "mock-answer-node"}})

        if scenario != "empty":
            for chunk in _chunk_text(answer_text):
                self._send_sse_event(
                    "text_chunk",
                    {
                        "event": "text_chunk",
                        "workflow_run_id": workflow_run_id,
                        "task_id": task_id,
                        "data": {
                            "text": chunk,
                            "from_variable_selector": ["answer"],
                        },
                    },
                )
                self._sleep_for_scenario(scenario)

        self._send_sse_event("node_finished", {"event": "node_finished", "task_id": task_id, "data": {"node_id": "mock-answer-node"}})

        if scenario == "close":
            self.wfile.flush()
            return

        finished_at = _now_ts()
        status = "failed" if scenario == "fail" else "succeeded"
        error = "mock workflow failed on purpose" if scenario == "fail" else ""
        outputs = {} if scenario == "empty" else {"answer": answer_text}

        self._send_sse_event(
            "workflow_finished",
            {
                "event": "workflow_finished",
                "workflow_run_id": workflow_run_id,
                "task_id": task_id,
                "data": {
                    "id": workflow_run_id,
                    "workflow_id": WORKFLOW_ID,
                    "status": status,
                    "created_at": created_at,
                    "finished_at": finished_at,
                    "outputs": outputs,
                    "error": error,
                    "elapsed_time": round(time.time() - started_at, 3),
                    "total_tokens": max(1, len(answer_text) // 4),
                    "total_steps": 3,
                },
            },
        )
        self._send_sse_done()

    def _handle_chat_messages(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"code": "invalid_param", "message": str(exc)})
            return

        query = str(payload.get("query") or "")
        user = str(payload.get("user") or "mock-user")
        conversation_id = str(payload.get("conversation_id") or uuid.uuid4())
        message_id = str(uuid.uuid4())
        scenario, scenario_query = _scenario_from_query(query)
        visible_query = scenario_query if scenario != "default" else query
        answer_text = _build_answer_text(visible_query, user, payload.get("inputs") or {}, scenario)

        self._send_sse_headers()
        for chunk in _chunk_text(answer_text):
            self._send_sse_event(
                "message",
                {
                    "event": "message",
                    "conversation_id": conversation_id,
                    "message_id": message_id,
                    "data": {
                        "outputs": {
                            "answer": chunk,
                        }
                    },
                },
            )
            self._sleep_for_scenario(scenario)

        self._send_sse_event(
            "message_end",
            {
                "event": "message_end",
                "conversation_id": conversation_id,
                "message_id": message_id,
                "data": {
                    "outputs": {
                        "answer": answer_text,
                    }
                },
            },
        )
        self._send_sse_done()

    def _sleep_for_scenario(self, scenario: str) -> None:
        delay = CHUNK_DELAY_SECONDS * (2 if scenario == "slow" else 1)
        time.sleep(delay)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _send_sse_event(self, event_name: str, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        message = f"event: {event_name}\ndata: {data}\n\n".encode("utf-8")
        try:
            self.wfile.write(message)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_sse_done(self) -> None:
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), MockDifyHandler)
    print(f"[mock-dify] listening on http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[mock-dify] stopped")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
