"""Gateway 全局常量。改这里 = 改 deployment.yaml + uvicorn 启动参数。"""
from __future__ import annotations

import os

# ---- 网络 ----
GATEWAY_LISTEN_HOST = "0.0.0.0"
GATEWAY_LISTEN_PORT = int(os.environ.get("GATEWAY_PORT", "8080"))

# 上游 (chatgpt.com 或 mock)
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://chatgpt.com")
UPSTREAM_RESPONSES_PATH = os.environ.get(
    "UPSTREAM_RESPONSES_PATH", "/backend-api/codex/responses"
)  # mock 用 /v1/responses
UPSTREAM_WHAM_USAGE_PATH = os.environ.get(
    "UPSTREAM_WHAM_USAGE_PATH", "/backend-api/wham/usage"
)
UPSTREAM_TOKEN_PATH = os.environ.get(
    "UPSTREAM_TOKEN_PATH", "https://auth.openai.com/oauth/token"
)

# 内部链路 (LiteLLM -> gateway)，绝不暴露到外网
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "sk-pool-internal")

# ---- 并发 / 超时 ----
UVICORN_LIMIT_CONCURRENCY = 300  # 超过返 429 让 LiteLLM 走 fallback
HTTPX_MAX_CONNECTIONS = 200
HTTPX_MAX_KEEPALIVE = 50
HTTPX_KEEPALIVE_EXPIRY_S = 30
HTTPX_CONNECT_TIMEOUT_S = 5.0
HTTPX_WRITE_TIMEOUT_S = 10.0
HTTPX_POOL_TIMEOUT_S = 5.0
# read=None：SSE 流不能加 read 超时

# ---- TCP keepalive (httpx socket_options 默认无) ----
TCP_KEEPIDLE_S = 60
TCP_KEEPINTVL_S = 30
TCP_KEEPCNT = 3

# ---- Quota probe ----
WHAM_PROBE_INTERVAL_S = 60         # 后台 tick，绝不进请求路径
WHAM_PROBE_TIMEOUT_S = 8
COOLDOWN_AFTER_429_S = 30          # LiteLLM cooldown 配合
TOKEN_REFRESH_MIN_INTERVAL_S = 60  # 同账号 refresh 间隔下界

# ---- 状态机阈值 ----
CONSECUTIVE_401_THRESHOLD = 3      # 超过即标 token_invalidated
PRIMARY_WINDOW_BLOCK_PCT = 100.0   # >= 视为 rate-limited
SECONDARY_WINDOW_BLOCK_PCT = 100.0

# ---- 落盘 ----
REGISTRY_DB_PATH = os.environ.get("REGISTRY_DB", "/data/gateway-registry.db")
AUTH_FILES_DIR = os.environ.get("AUTH_FILES_DIR", "/data/auth")

# ---- 内存预算 ----
PROCESS_MAX_HEAP_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB 软警戒线
