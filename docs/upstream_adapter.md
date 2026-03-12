# 上游服务适配器说明

## 为什么要抽这一层

飞书机器人本身只关心两件事：

1. 用户问了什么
2. 上游流式回了什么

至于上游到底是 Dify、内部模型网关，还是其他第三方服务，主程序不应该知道太多细节。

所以项目里把这部分统一抽象成了“上游适配器”。

## 标准请求格式

当前服务发给上游适配器的是统一对象：

```python
UpstreamRequest(
    query="用户当前问题",
    user_id="当前用户标识",
    conversation_id="上下文会话 ID，可为空",
)
```

字段说明：

- `query`: 当前轮用户输入
- `user_id`: 当前用户标识
- `conversation_id`: 上下文会话标识，没有就传空字符串

## 标准流式响应格式

适配器返回给主程序的是统一对象：

```python
UpstreamStreamEvent(
    event="message",
    text="本次增量文本",
    replace_text=False,
    conversation_id="上游返回的会话 ID",
    finished=False,
    finish_status="",
    outputs=None,
    raw_event={},
)
```

字段说明：

- `event`: 上游原始事件名
- `text`: 当前事件携带的文本内容
- `replace_text`: 是否直接替换历史文本
- `conversation_id`: 上游返回的上下文 ID
- `finished`: 是否为结束事件
- `finish_status`: 结束状态，例如 `succeeded`、`failed`
- `outputs`: 结束事件里的完整输出对象
- `raw_event`: 原始事件内容，便于日志排查

## Dify 请求格式

### Chat 模式

当前服务发送给 Dify 的 JSON：

```json
{
  "inputs": {},
  "query": "用户问题",
  "response_mode": "streaming",
  "user": "用户标识",
  "conversation_id": "可选，会话 ID"
}
```

### Workflow 模式

当前服务发送给 Dify 的 JSON：

```json
{
  "inputs": {
    "query": "用户问题",
    "text": "用户问题",
    "message": "用户问题",
    "user_query": "用户问题"
  },
  "response_mode": "streaming",
  "user": "用户标识"
}
```

## Dify 返回格式

常见的 SSE 事件包括：

- `message`
- `agent_message`
- `message_replace`
- `message_end`
- `text_chunk`
- `workflow_finished`
- `error`

适配器会把这些原始事件映射成统一的 `UpstreamStreamEvent`，主程序只消费统一格式。

## 如何接一个新的后端

复制 `feishu_bot/upstream/template.py`，然后完成这四件事：

1. 定义你的 `request_schema`
2. 定义你的 `response_schema`
3. 实现 `build_request_payload`
4. 实现 `stream`

如果你的上游是 SSE、WebSocket、普通轮询接口，都可以在适配器层自行转换，只要最后 yield 出统一的 `UpstreamStreamEvent` 即可。

## 设计目标

这层抽象的目的是：

- 让主程序不依赖某个具体厂商
- 让接入新后端时改动范围尽量小
- 让请求和响应格式足够清晰，便于排查和二次开发
