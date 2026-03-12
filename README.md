# 飞书知识库机器人

这是一个面向飞书群聊/私聊场景的机器人项目。它负责接收飞书消息，将问题转发给上游大模型服务，并将流式结果以飞书 Markdown 卡片的形式回写给用户。

当前默认已经实现：

- 飞书消息接入
- 上游 Dify Chat / Workflow 对接
- 流式回答回写
- 会话上下文延续
- 本地 mock Dify 测试
- 3 路并行处理消息

## 项目结构

```text
.
├── config.py                     # 配置入口，切换测试/正式模式
├── main.py                       # 飞书事件入口与主流程编排
├── mock_dify_server.py           # 本地 Dify mock 服务
├── feishu_bot/
│   ├── errors.py                 # 异常定义
│   ├── markdown_card.py          # 飞书 Markdown 卡片相关工具
│   └── upstream/
│       ├── base.py               # 上游服务适配器协议
│       ├── dify.py               # Dify 适配器实现
│       └── template.py           # 新上游服务适配模板
├── docs/
│   └── upstream_adapter.md       # 上游请求/响应格式说明
├── run.sh                        # 杀旧进程并后台启动
├── update.sh                     # 拉最新代码、切模式并重启
└── requirements.txt             # Python 依赖
```

## 工作流程

1. 飞书通过长连接把消息事件推给机器人。
2. 机器人解析文本内容，并为消息添加表情确认。
3. 主程序将消息转换成统一的 `UpstreamRequest`。
4. `feishu_bot/upstream/dify.py` 把统一请求映射成 Dify 请求体。
5. Dify 以 SSE 流式返回事件。
6. 适配器将 Dify 事件统一转换成 `UpstreamStreamEvent`。
7. 主程序将增量文本持续 patch 到同一条飞书 Markdown 卡片。

## 上游服务适配

本项目已经把“对接上游服务”的逻辑单独抽象出来了。

核心文件：

- [feishu_bot/upstream/base.py](/Users/donglingke/Documents/Code/dify_feishu_notify/feishu_bot/upstream/base.py)
- [feishu_bot/upstream/dify.py](/Users/donglingke/Documents/Code/dify_feishu_notify/feishu_bot/upstream/dify.py)
- [feishu_bot/upstream/template.py](/Users/donglingke/Documents/Code/dify_feishu_notify/feishu_bot/upstream/template.py)
- [docs/upstream_adapter.md](/Users/donglingke/Documents/Code/dify_feishu_notify/docs/upstream_adapter.md)

统一请求模型：

```python
UpstreamRequest(
    query="用户问题",
    user_id="用户标识",
    conversation_id="上下文会话 ID，可为空",
)
```

统一流式事件模型：

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

如果你要接新的后端服务，直接复制 [feishu_bot/upstream/template.py](/Users/donglingke/Documents/Code/dify_feishu_notify/feishu_bot/upstream/template.py) 并实现自己的适配器即可，主程序不需要改数据流模型。

## Dify 请求格式

### Chat 模式

发送给 Dify：

```json
{
  "inputs": {},
  "query": "用户问题",
  "response_mode": "streaming",
  "user": "飞书用户标识",
  "conversation_id": "可选，会话 ID"
}
```

### Workflow 模式

发送给 Dify：

```json
{
  "inputs": {
    "query": "用户问题",
    "text": "用户问题",
    "message": "用户问题",
    "user_query": "用户问题"
  },
  "response_mode": "streaming",
  "user": "飞书用户标识"
}
```

### Dify 流式返回

常见 SSE 事件：

- `message`
- `agent_message`
- `message_replace`
- `message_end`
- `text_chunk`
- `workflow_finished`
- `error`

这些事件最终都会被适配器转成统一的 `UpstreamStreamEvent`。

## 配置

配置在 [config.py](/Users/donglingke/Documents/Code/dify_feishu_notify/config.py)。

目前通过：

```python
MODE = 0
```

切换运行模式：

- `0`：测试环境
- `1`：正式环境

注意：

- 测试和正式环境不要共用同一个飞书应用，否则会重复消费同一条消息
- 正式环境当前默认走 Dify Chat API
- 测试环境默认走本地 mock Dify

## 启动

### macOS / Linux

```bash
./bootstrap.sh
```

### Windows

```bat
bootstrap.bat
```

## 本地测试

启动本地 mock：

```bash
python3 mock_dify_server.py
```

或者直接一键启动：

```bash
./bootstrap-local.sh
```

## 运维脚本

后台启动：

```bash
./run.sh
```

拉最新代码并切模式重启：

```bash
./update.sh
```

## 飞书权限

至少需要：

- `im:message`
- `im:message:write`
- `im:message:reaction`

## 上下文会话

Chat 模式下，项目会按“聊天会话 + 用户”缓存 Dify `conversation_id`，因此同一个用户连续追问时会自动带上上下文。

手动清空上下文命令：

- `/reset`
- `/new`
- `/clear`
- `清空上下文`
- `重置上下文`
- `新建会话`
