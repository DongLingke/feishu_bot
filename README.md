# 飞书知识库机器人

这是一个面向正式环境的飞书机器人项目。它接收飞书消息，将问题转发给上游大模型服务，并将流式回答以飞书 Markdown 卡片的形式回写给用户。

## 当前能力

- 飞书长连接消息接入
- Dify Chat 模式对接
- 流式 Markdown 卡片回复
- 上下文会话延续
- 3 路并行处理消息

## 项目结构

```text
.
├── config.py                     # 单一运行配置
├── main.py                       # 飞书事件入口与主流程编排
├── feishu_bot/
│   ├── errors.py                 # 异常定义
│   ├── markdown_card.py          # 飞书 Markdown 卡片工具
│   └── upstream/
│       ├── base.py               # 上游服务适配协议
│       ├── dify.py               # Dify 适配器
│       └── template.py           # 新上游服务适配模板
├── docs/
│   └── upstream_adapter.md       # 上游请求/响应格式说明
├── bootstrap.sh                  # 前台启动
├── bootstrap.bat                 # Windows 前台启动
├── run.sh                        # 杀旧进程并后台启动
├── update.sh                     # 拉最新代码并重启
└── requirements.txt              # Python 依赖
```

## 运行流程

1. 飞书通过长连接推送消息事件。
2. 主程序将消息转换成统一的 `UpstreamRequest`。
3. `feishu_bot/upstream/dify.py` 负责构造 Dify 请求并解析 SSE 响应。
4. 主程序把流式结果 patch 到同一条飞书 Markdown 卡片。

## 配置

项目只保留一套正式环境配置，位于 [config.py](/Users/donglingke/Documents/Code/dify_feishu_notify/config.py)：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `DIFY_APP_TYPE`
- `DIFY_APP_PAGE_URL`
- `DIFY_API_BASE_URL`
- `DIFY_API_PATH`
- `DIFY_API_KEY`

当前默认配置为 Dify Chat API。

## 启动

macOS / Linux:

```bash
./bootstrap.sh
```

Windows:

```bat
bootstrap.bat
```

后台重启：

```bash
./run.sh
```

拉取最新代码并重启：

```bash
./update.sh
```

## 上游服务适配

如果后续需要接其他后端服务，直接参考：

- [feishu_bot/upstream/base.py](/Users/donglingke/Documents/Code/dify_feishu_notify/feishu_bot/upstream/base.py)
- [feishu_bot/upstream/template.py](/Users/donglingke/Documents/Code/dify_feishu_notify/feishu_bot/upstream/template.py)
- [docs/upstream_adapter.md](/Users/donglingke/Documents/Code/dify_feishu_notify/docs/upstream_adapter.md)

## 上下文会话

Chat 模式下，项目会按“聊天会话 + 用户”缓存 Dify `conversation_id`。

手动清空上下文命令：

- `/reset`
- `/new`
- `/clear`
- `清空上下文`
- `重置上下文`
- `新建会话`
