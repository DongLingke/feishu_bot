# Feishu Dify Workflow Bot

## What changed

- All runtime configuration is centralized in `config.py`.
- The bot now uses the new Feishu app credentials you provided.
- Incoming messages are forwarded to the new Dify workflow API.
- The bot acknowledges receipt with a message reaction instead of a "收到" text message.
- Dify workflow output is pushed back to Feishu incrementally by updating one reply message.
- Error messages now include more detail for Dify HTTP errors, stream issues, workflow failure states, and Feishu API failures.

## Files

- `config.py`: all configurable values in one place.
- `main.py`: Feishu event handling, Dify workflow streaming, and message updates.
- `requirements.txt`: Python dependency list.
- `bootstrap.sh` / `bootstrap.bat`: install dependencies and start the bot.

## Important config

- By default, the bot now uses the built-in local mock Dify on `http://127.0.0.1:18080` and auto-starts it when needed.
- `DIFY_API_KEY` defaults to `local-test-key` when local mock mode is enabled.
- To use a real Dify environment, set `USE_LOCAL_DIFY_MOCK=false` and provide `DIFY_API_BASE_URL`, `DIFY_WORKFLOW_PAGE_URL`, and `DIFY_API_KEY`.
- If your workflow input variable name is not one of `query,text,message,user_query`, update `DIFY_QUERY_INPUT_KEYS` in `config.py`.
- The default reaction emoji type is `DONE`. If your tenant expects another emoji code, change `FEISHU_ACK_REACTION_EMOJI_TYPE` in `config.py`.

## Required Feishu permissions

- `im:message`
- `im:message:write`
- `im:message.reactions:write_only`

## Start

macOS / Linux:

```bash
./bootstrap.sh
```

Windows:

```bat
bootstrap.bat
```

## Local mock Dify

If you do not have a real Dify environment, this repo now includes a local mock server that exposes the same API shape used by the bot:

- `POST /v1/workflows/run`
- `POST /v1/chat-messages`
- streaming response via `text/event-stream`

One-command local run on macOS / Linux:

```bash
./bootstrap-local.sh
```

One-command local run on Windows:

```bat
bootstrap-local.bat
```

Or run the mock server manually:

```bash
python3 mock_dify_server.py
```

Then point the bot to the local mock:

```bash
DIFY_API_BASE_URL=http://127.0.0.1:18080/v1 \
DIFY_WORKFLOW_PAGE_URL=http://127.0.0.1:18080/mock-workflow \
DIFY_API_KEY=local-test-key \
python3 main.py
```

The mock server sends SSE events compatible with the current bot implementation, including `workflow_started`, `node_started`, `text_chunk`, `node_finished`, and `workflow_finished`.

The bot now flushes stream updates more aggressively by default so Feishu replies look smoother. If you need to tune this further, set `STREAM_UPDATE_INTERVAL_SECONDS` and `STREAM_UPDATE_MIN_CHARS`.

Useful test inputs in Feishu:

- `你好，帮我测试一下`
- `/slow 这条消息会更慢地流式返回`
- `/empty 测试空结果`
- `/fail 测试工作流失败`
- `/close 测试流在 workflow_finished 之前关闭`
- `/long 测试超长文本`
