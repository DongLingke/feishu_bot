# 上游服务适配器说明

## 目标

本项目不再把 Dify 请求逻辑直接写死在主程序中，而是通过“上游服务适配器”来对接不同后端。

当前标准入口在：

- `/Users/donglingke/Documents/Code/dify_feishu_notify/feishu_bot/upstream/base.py`
- `/Users/donglingke/Documents/Code/dify_feishu_notify/feishu_bot/upstream/dify.py`

## 当前服务发给上游服务的数据格式

主程序先把飞书消息转换成统一的 `UpstreamRequest`：

```python
UpstreamRequest(
    query="用户当前问题",
    user_id="当前用户标识",
    conversation_id="上下文会话 ID，可为空",
)
```

字段说明：

- `query`: 当前轮用户输入
- `user_id`: 用户唯一标识，默认取飞书 `open_id/user_id/union_id`
- `conversation_id`: 上下文会话标识，由上游服务返回并在后续请求中复用

## 上游服务回给当前服务的数据格式

适配器需要把上游原始响应转换成统一的 `UpstreamStreamEvent`：

```python
UpstreamStreamEvent(
    event="message",
    text="本次增量文本",
    replace_text=False,
    conversation_id="上游返回的会话 ID",
    finished=False,
    finish_status="",
    outputs=None,
    raw_event={"上游原始事件": "..."},
)
```

字段说明：

- `event`: 上游原始事件名
- `text`: 当前事件带回的文本
- `replace_text`: 是否用本次文本替换历史累计文本
- `conversation_id`: 当前会话 ID
- `finished`: 是否已经结束
- `finish_status`: 结束状态，例如 `succeeded`、`failed`
- `outputs`: 结束事件中的完整输出对象
- `raw_event`: 原始事件内容，便于日志和排错

## Dify 适配规则

### chat 模式请求体

```json
{
  "inputs": {},
  "query": "用户问题",
  "response_mode": "streaming",
  "user": "用户标识",
  "conversation_id": "可选，会话 ID"
}
```

### workflow 模式请求体

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

### Dify 流式事件映射

- `message` / `agent_message` -> `UpstreamStreamEvent.text`
- `message_replace` -> `UpstreamStreamEvent.text` 且 `replace_text=True`
- `message_end` / `workflow_finished` -> `finished=True`
- `conversation_id` -> 统一提取并回写到会话缓存

## 新接一个上游服务要做什么

1. 新建一个适配器文件，例如 `feishu_bot/upstream/xxx.py`
2. 继承 `UpstreamAdapter`
3. 实现：
   - `request_schema`
   - `response_schema`
   - `build_request_payload`
   - `stream`
4. 在主程序里替换当前 `DifyAdapter` 实例

这样主程序无需关心后端细节，只消费统一的 `UpstreamRequest` 和 `UpstreamStreamEvent`。
