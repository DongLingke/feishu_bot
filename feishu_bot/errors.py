from __future__ import annotations


class BotRuntimeError(Exception):
    """机器人运行过程中的基础异常。"""


class DifyRequestError(BotRuntimeError):
    """上游 Dify 请求或流式响应异常。"""

    def __init__(self, summary: str, detail: str = "") -> None:
        super().__init__(summary)
        self.summary = summary
        self.detail = detail


class FeishuRequestError(BotRuntimeError):
    """飞书开放平台请求异常。"""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = str(code or "")
