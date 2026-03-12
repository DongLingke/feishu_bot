from __future__ import annotations

from typing import Any, Iterator

from feishu_bot.upstream.base import UpstreamAdapter, UpstreamRequest, UpstreamStreamEvent


class ExampleUpstreamAdapter(UpstreamAdapter):
    """
    上游适配器模板。

    用法：
    1. 按你的上游接口格式修改 `request_schema`
    2. 在 `build_request_payload` 中拼出真实请求体
    3. 在 `stream` 中把上游返回的数据统一转换成 `UpstreamStreamEvent`
    """

    @property
    def request_schema(self) -> dict[str, Any]:
        return {
            "query": "string",
            "user": "string",
            "conversation_id": "string | 可选",
            "extra": {},
        }

    @property
    def response_schema(self) -> dict[str, Any]:
        return {
            "event": "message | replace | end | error",
            "conversation_id": "string | 可选",
            "text": "string | 可选",
            "status": "succeeded | failed | 可选",
            "raw": {},
        }

    def build_request_payload(self, request: UpstreamRequest) -> dict[str, Any]:
        payload = {
            "query": request.query,
            "user": request.user_id,
        }
        if request.conversation_id:
            payload["conversation_id"] = request.conversation_id
        return payload

    def stream(self, request: UpstreamRequest) -> Iterator[UpstreamStreamEvent]:
        """
        这里写你的上游调用逻辑。

        你最终只需要持续 yield `UpstreamStreamEvent`，主程序就能复用。
        """
        yield UpstreamStreamEvent(
            event="message",
            text="这里换成你的增量文本",
            replace_text=False,
            conversation_id=request.conversation_id,
            raw_event={"demo": True},
        )

        yield UpstreamStreamEvent(
            event="end",
            finished=True,
            finish_status="succeeded",
            conversation_id=request.conversation_id,
            outputs={"answer": "这里换成你的最终输出"},
            raw_event={"demo": True},
        )
