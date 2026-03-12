from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass
class UpstreamRequest:
    """
    当前服务发给上游服务的标准请求对象。

    字段说明：
    - query: 当前轮用户问题
    - user_id: 当前用户标识
    - conversation_id: 上下文会话标识，可为空
    """

    query: str
    user_id: str
    conversation_id: str = ""


@dataclass
class UpstreamStreamEvent:
    """
    上游服务回给当前服务的标准流式事件对象。

    字段说明：
    - event: 上游原始事件名称
    - text: 本次事件携带的增量文本
    - replace_text: 是否用 text 替换已累计内容
    - conversation_id: 上游返回的会话标识
    - finished: 是否为结束事件
    - finish_status: 结束状态，例如 succeeded / failed
    - outputs: 上游结束事件中的完整输出
    - raw_event: 原始事件对象，便于排查
    """

    event: str
    text: str = ""
    replace_text: bool = False
    conversation_id: str = ""
    finished: bool = False
    finish_status: str = ""
    outputs: Any = None
    raw_event: dict[str, Any] | None = None


class UpstreamAdapter(ABC):
    """
    上游服务适配器模板。

    若后续需要接入新的大模型后端，只需要实现：
    1. `build_request_payload`: 当前服务发给上游服务的数据格式
    2. `stream`: 上游服务回给当前服务的数据格式，统一转成 UpstreamStreamEvent
    """

    @property
    @abstractmethod
    def request_schema(self) -> dict[str, Any]:
        """描述当前服务发给上游服务的数据结构。"""

    @property
    @abstractmethod
    def response_schema(self) -> dict[str, Any]:
        """描述上游服务回给当前服务的数据结构。"""

    @abstractmethod
    def build_request_payload(self, request: UpstreamRequest) -> dict[str, Any]:
        """将标准请求对象转换成上游服务实际需要的请求体。"""

    @abstractmethod
    def stream(self, request: UpstreamRequest) -> Iterator[UpstreamStreamEvent]:
        """向上游发起流式请求，并将返回结果统一转换成标准事件。"""
