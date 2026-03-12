# Feishu Knowledge Bot

一个面向飞书场景的知识库机器人。

它做的事情很简单：

1. 接收飞书消息
2. 把用户问题转发给上游大模型服务
3. 把流式结果持续回写到同一条飞书 Markdown 卡片

当前仓库默认内置了 Dify 适配器，但整体结构已经抽象成了“上游适配器”模式，所以后续接别的后端也比较直接。

## 特性

- 支持飞书长连接事件接入
- 支持 Dify Chat 模式流式输出
- 回复统一使用飞书 Markdown 卡片
- 支持按“会话 + 用户”延续上下文
- 默认支持 3 路并行处理消息
- 上游协议已模块化，方便扩展到其他模型服务

## 项目结构

```text
.
├── .env.example               # 环境变量模板
├── config.py                  # 配置加载器（读取 .env / 环境变量）
├── main.py                    # 主入口，负责飞书事件与消息编排
├── feishu_bot/
│   ├── errors.py              # 异常定义
│   ├── markdown_card.py       # 飞书 Markdown 卡片构造与文本处理
│   └── upstream/
│       ├── base.py            # 上游适配器协议
│       ├── dify.py            # Dify 适配器实现
│       └── template.py        # 新上游服务适配模板
├── docs/
│   └── upstream_adapter.md    # 上游请求/响应格式说明
├── bootstrap.sh               # 前台启动
├── bootstrap.bat              # Windows 前台启动
├── run.sh                     # 后台启动
├── update.sh                  # 拉最新代码并重启
└── requirements.txt           # 依赖列表
```

## 工作流程

1. 飞书通过长连接把消息事件推给机器人
2. 机器人解析消息文本，并给原消息加确认表情
3. 主程序把消息转换成统一的 `UpstreamRequest`
4. 上游适配器负责把标准请求转换成目标后端需要的请求体
5. 上游流式返回事件后，再统一转换成 `UpstreamStreamEvent`
6. 主程序通过 CardKit 流式更新卡片元素内容，并在结束时更新成最终 Markdown 卡片

## 快速开始

### 1. 安装依赖

macOS / Linux:

```bash
./bootstrap.sh
```

Windows:

```bat
bootstrap.bat
```

### 2. 配置参数

先复制模板文件：

```bash
cp .env.example .env
```

然后打开 `.env`，把下面这些占位值换成你自己的配置：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `DIFY_APP_PAGE_URL`
- `DIFY_API_BASE_URL`
- `DIFY_API_KEY`

默认配置是 Dify Chat API：

```env
DIFY_APP_TYPE=chat
DIFY_API_PATH=/chat-messages
```

### 3. 启动服务

前台运行：

```bash
python3 main.py
```

后台运行：

```bash
./run.sh
```

拉取最新代码并重启：

```bash
./update.sh
```

## 飞书权限

至少需要这些权限：

- `im:message`
- `im:message:write`
- `im:message:reaction`
- `cardkit:card:write`
- `cardkit:card:read`

如果你使用的是企业内部应用，还需要在飞书后台把机器人加到对应群聊或可见范围中。

## 上下文会话

Chat 模式下，项目会缓存上游返回的 `conversation_id`，因此同一个用户连续追问时会自动带上上下文。

支持手动清空上下文：

- `/reset`
- `/new`
- `/clear`
- `清空上下文`
- `重置上下文`
- `新建会话`

## 扩展上游服务

如果你不想用 Dify，而是要接别的后端，可以直接参考这几个文件：

- `feishu_bot/upstream/base.py`
- `feishu_bot/upstream/template.py`
- `docs/upstream_adapter.md`

你只需要做两件事：

1. 把标准请求转换成目标上游的请求体
2. 把目标上游的响应转换成统一的流式事件

主程序不需要重新设计消息流转逻辑。

## 当前限制

- 真实配置应只保存在 `.env` 中，公开仓库里的 `config.py` 和 `.env.example` 都是安全的模板文件
- 当前默认并发数为 3，如果你的上游后端吞吐更高，可以在代码里调大
- 当前仓库默认以 Dify Chat 为主，虽然适配器层兼容更多协议，但还没有内置其他后端实现

## 适合谁

如果你想要一个：

- 结构尽量简单
- 能直接跑起来
- 能继续扩展上游后端
- 适合二次开发的飞书机器人

那这个项目应该是一个不错的起点。
