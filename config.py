"""项目运行配置。"""

# 飞书配置
FEISHU_APP_ID = "cli_a93bb2832db85bd9"
FEISHU_APP_SECRET = "R1cBtuseOzWwtwbdggINJgHvEEl6tlsB"
FEISHU_LOG_LEVEL = "INFO"

# Dify 配置
DIFY_APP_TYPE = "chat"
DIFY_APP_PAGE_URL = "https://dify.mjutech.com/app/97a55098-0e74-42ed-bcc8-7af172e74d73/workflow"
DIFY_API_BASE_URL = "https://dify.mjutech.com/v1"
DIFY_API_PATH = "/chat-messages"
DIFY_API_KEY = "app-Zfp3Q1wt0Xo2gMJwwmTk7hk2"
DIFY_QUERY_INPUT_KEYS = ("query", "text", "message", "user_query")
DIFY_FIXED_INPUTS: dict[str, object] = {}
DIFY_REQUEST_TIMEOUT_SECONDS = 180
