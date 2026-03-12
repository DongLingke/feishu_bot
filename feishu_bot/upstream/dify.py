from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Iterator

from feishu_bot.errors import DifyRequestError
from feishu_bot.markdown_card import trim_text
from feishu_bot.upstream.base import UpstreamAdapter, UpstreamRequest, UpstreamStreamEvent


class DifyAdapter(UpstreamAdapter):
    """Dify 上游适配器。"""

    def __init__(
        self,
        *,
        app_type: str,
        api_base_url: str,
        api_path: str,
        api_key: str,
        query_input_keys: tuple[str, ...],
        fixed_inputs: dict[str, object],
        request_timeout_seconds: int,
        max_error_body_chars: int,
        preferred_output_keys: tuple[str, ...],
    ) -> None:
        self.app_type = str(app_type or "workflow").strip().lower()
        self.api_base_url = api_base_url
        self.api_path = api_path
        self.api_key = api_key
        self.query_input_keys = tuple(query_input_keys)
        self.fixed_inputs = dict(fixed_inputs)
        self.request_timeout_seconds = int(request_timeout_seconds)
        self.max_error_body_chars = int(max_error_body_chars)
        self.preferred_output_keys = tuple(preferred_output_keys)

    @property
    def request_schema(self) -> dict[str, Any]:
        if self.app_type == "chat":
            return {
                "inputs": {},
                "query": "string",
                "response_mode": "streaming",
                "user": "string",
                "conversation_id": "string | 可选",
            }
        return {
            "inputs": {key: "string" for key in self.query_input_keys},
            "response_mode": "streaming",
            "user": "string",
        }

    @property
    def response_schema(self) -> dict[str, Any]:
        if self.app_type == "chat":
            return {
                "event": "message | agent_message | message_replace | message_end | error",
                "conversation_id": "string",
                "answer": "string | 可选",
                "data": {"outputs": {"answer": "string | 可选"}},
            }
        return {
            "event": "text_chunk | workflow_finished | error",
            "workflow_run_id": "string",
            "task_id": "string",
            "data": {"text": "string | 可选", "outputs": {"answer": "string | 可选"}},
        }

    def build_request_payload(self, request: UpstreamRequest) -> dict[str, Any]:
        if self.app_type == "chat":
            body: dict[str, Any] = {
                "inputs": dict(self.fixed_inputs),
                "query": request.query,
                "response_mode": "streaming",
                "user": request.user_id,
            }
            if request.conversation_id:
                body["conversation_id"] = request.conversation_id
            return body

        inputs: dict[str, object] = dict(self.fixed_inputs)
        for key in self.query_input_keys:
            inputs[key] = request.query
        return {
            "inputs": inputs,
            "response_mode": "streaming",
            "user": request.user_id,
        }

    def stream(self, request: UpstreamRequest) -> Iterator[UpstreamStreamEvent]:
        if not self.api_key:
            raise DifyRequestError("DIFY_API_KEY is empty", "config.py 中没有配置 Dify API Key")

        url = self.api_base_url.rstrip("/") + "/" + self.api_path.lstrip("/")
        payload = self.build_request_payload(request)
        encoded_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        http_request = urllib.request.Request(
            url,
            data=encoded_body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(http_request, timeout=self.request_timeout_seconds) as response:
                for raw_event in self._iter_sse_events(response):
                    yield self._convert_event(raw_event)
        except urllib.error.HTTPError as exc:
            body = self._decode_bytes(exc.read())
            raise DifyRequestError(
                f"Dify {self.app_type} HTTP error",
                self._format_http_error(exc.code, body),
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise DifyRequestError(
                    f"Dify {self.app_type} timeout",
                    f"socket timeout after {self.request_timeout_seconds}s",
                ) from exc
            raise DifyRequestError(f"Dify {self.app_type} request failed", str(exc.reason)) from exc
        except socket.timeout as exc:
            raise DifyRequestError(
                f"Dify {self.app_type} timeout",
                f"socket timeout after {self.request_timeout_seconds}s",
            ) from exc

    def _convert_event(self, event: dict[str, Any]) -> UpstreamStreamEvent:
        event_name = str(event.get("event") or "")
        conversation_id = self._extract_event_conversation_id(event)
        text, replace_text = self._extract_stream_text(event_name, event)

        if event_name == "error":
            raise DifyRequestError(
                f"Dify {self.app_type} stream error",
                self._format_stream_error_event(event),
            )

        if event_name in {"workflow_finished", "message_end"}:
            data = event.get("data") if isinstance(event.get("data"), dict) else event
            outputs = data.get("outputs") if isinstance(data, dict) else None
            if event_name == "workflow_finished" and not text:
                text = self._extract_best_output_text(outputs)
            if event_name == "message_end" and not text:
                text = self._extract_chat_event_text(event)
            return UpstreamStreamEvent(
                event=event_name,
                text=text,
                replace_text=replace_text,
                conversation_id=conversation_id,
                finished=True,
                finish_status=str(data.get("status") or "") if isinstance(data, dict) else "",
                outputs=outputs,
                raw_event=event,
            )

        return UpstreamStreamEvent(
            event=event_name,
            text=text,
            replace_text=replace_text,
            conversation_id=conversation_id,
            raw_event=event,
        )

    def _format_http_error(self, status: int, body: str) -> str:
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
            detail += f", body={trim_text(body, self.max_error_body_chars)}"
        return detail

    def _format_stream_error_event(self, event: dict[str, Any]) -> str:
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
            pieces.append(trim_text(event.get("_raw") or event, self.max_error_body_chars))
        return ", ".join(pieces)

    def _iter_sse_events(self, response: Any) -> Iterator[dict[str, Any]]:
        event_name = ""
        data_lines: list[str] = []

        while True:
            raw_line = response.readline()
            if not raw_line:
                if event_name or data_lines:
                    yield self._parse_sse_event(event_name, data_lines)
                break

            line = self._decode_bytes(raw_line).rstrip("\r\n")
            if not line:
                if event_name or data_lines:
                    yield self._parse_sse_event(event_name, data_lines)
                    event_name = ""
                    data_lines = []
                continue

            if line.startswith(":"):
                continue

            if line.startswith("event:"):
                event_name = line[6:].strip()
                continue

            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
                continue

    def _parse_sse_event(self, event_name: str, data_lines: list[str]) -> dict[str, Any]:
        payload_text = "\n".join(data_lines).strip()
        if not payload_text:
            return {"event": event_name}
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

    def _extract_best_output_text(self, outputs: Any) -> str:
        if isinstance(outputs, str):
            return outputs.strip()
        if isinstance(outputs, list):
            parts = [self._extract_best_output_text(item) for item in outputs]
            parts = [part for part in parts if part]
            return "\n".join(parts)
        if isinstance(outputs, dict):
            for key in self.preferred_output_keys:
                if key in outputs:
                    preferred = self._extract_best_output_text(outputs[key])
                    if preferred:
                        return preferred
            if len(outputs) == 1:
                only_value = next(iter(outputs.values()))
                return self._extract_best_output_text(only_value)
            return json.dumps(outputs, ensure_ascii=False, indent=2)
        return ""

    def _extract_chat_event_text(self, event: dict[str, Any]) -> str:
        direct_text = event.get("answer")
        if isinstance(direct_text, str) and direct_text:
            return direct_text

        data = event.get("data")
        if isinstance(data, dict):
            data_answer = data.get("answer")
            if isinstance(data_answer, str) and data_answer:
                return data_answer

            outputs = data.get("outputs")
            text = self._extract_best_output_text(outputs)
            if text:
                return text

        outputs = event.get("outputs")
        text = self._extract_best_output_text(outputs)
        if text:
            return text

        return ""

    def _extract_event_conversation_id(self, event: dict[str, Any]) -> str:
        value = event.get("conversation_id")
        if isinstance(value, str) and value:
            return value

        data = event.get("data")
        if isinstance(data, dict):
            nested_value = data.get("conversation_id")
            if isinstance(nested_value, str) and nested_value:
                return nested_value

        return ""

    def _extract_stream_text(self, event_name: str, event: dict[str, Any]) -> tuple[str, bool]:
        if event_name == "text_chunk":
            data = event.get("data") or {}
            text = data.get("text")
            return (text, False) if isinstance(text, str) else ("", False)

        if event_name in {"message", "agent_message"}:
            text = self._extract_chat_event_text(event)
            return (text, False) if text else ("", False)

        if event_name == "message_replace":
            text = self._extract_chat_event_text(event)
            return (text, True) if text else ("", False)

        return "", False

    @staticmethod
    def _decode_bytes(value: Any) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value or "")
