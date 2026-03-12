MODE = 0  # 0 表示测试环境，1 表示正式环境


# 测试环境配置
TEST_CONFIG = {
    "use_local_dify_mock": True,
    "auto_start_local_dify_mock": True,
    "feishu_app_id": "cli_a93bb2832db85bd9",
    "feishu_app_secret": "R1cBtuseOzWwtwbdggINJgHvEEl6tlsB",
    "feishu_log_level": "DEBUG",
    "dify_app_type": "workflow",
    "dify_app_page_url": "http://127.0.0.1:18080/mock-workflow",
    "dify_api_base_url": "http://127.0.0.1:18080/v1",
    "dify_api_path": "/workflows/run",
    "dify_api_key": "local-test-key",
    "dify_query_input_keys": ("query", "text", "message", "user_query"),
    "dify_fixed_inputs": {},
    "dify_request_timeout_seconds": 180,
}


# 正式环境配置
ONLINE_CONFIG = {
    "use_local_dify_mock": False,
    "auto_start_local_dify_mock": False,
    "feishu_app_id": TEST_CONFIG["feishu_app_id"],
    "feishu_app_secret": TEST_CONFIG["feishu_app_secret"],
    "feishu_log_level": "INFO",
    "dify_app_type": "chat",
    "dify_app_page_url": "https://dify.mjutech.com/app/97a55098-0e74-42ed-bcc8-7af172e74d73/workflow",
    "dify_api_base_url": "https://dify.mjutech.com/v1",
    "dify_api_path": "/chat-messages",
    "dify_api_key": "app-Zfp3Q1wt0Xo2gMJwwmTk7hk2",
    "dify_query_input_keys": ("query", "text", "message", "user_query"),
    "dify_fixed_inputs": {},
    "dify_request_timeout_seconds": 180,
}


# 根据模式导出当前生效配置
MODE_CONFIG_MAP = {
    0: TEST_CONFIG,
    1: ONLINE_CONFIG,
}

if MODE not in MODE_CONFIG_MAP:
    raise ValueError(f"MODE 只能是 0 或 1，当前值为: {MODE}")

ACTIVE_CONFIG = MODE_CONFIG_MAP[MODE]


# 飞书配置
USE_LOCAL_DIFY_MOCK = ACTIVE_CONFIG["use_local_dify_mock"]
AUTO_START_LOCAL_DIFY_MOCK = ACTIVE_CONFIG["auto_start_local_dify_mock"]
FEISHU_APP_ID = ACTIVE_CONFIG["feishu_app_id"]
FEISHU_APP_SECRET = ACTIVE_CONFIG["feishu_app_secret"]
FEISHU_LOG_LEVEL = ACTIVE_CONFIG["feishu_log_level"].upper()


# Dify 配置
DIFY_APP_TYPE = ACTIVE_CONFIG["dify_app_type"]
DIFY_APP_PAGE_URL = ACTIVE_CONFIG["dify_app_page_url"]
DIFY_API_BASE_URL = ACTIVE_CONFIG["dify_api_base_url"]
DIFY_API_PATH = ACTIVE_CONFIG["dify_api_path"]
DIFY_API_KEY = ACTIVE_CONFIG["dify_api_key"]
DIFY_QUERY_INPUT_KEYS = tuple(ACTIVE_CONFIG["dify_query_input_keys"])
DIFY_FIXED_INPUTS: dict[str, object] = dict(ACTIVE_CONFIG["dify_fixed_inputs"])
DIFY_REQUEST_TIMEOUT_SECONDS = ACTIVE_CONFIG["dify_request_timeout_seconds"]
