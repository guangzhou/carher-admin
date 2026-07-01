"""出向上游客户端。

两层 HTTP client 必须明确分开：
- 调 chatgpt.com / auth.openai.com：curl_cffi.AsyncSession(impersonate="chrome120")
  （JA3/JA4 指纹检测，raw httpx 会被 CF 拦，openai/codex#17860/#18688）
- 调 LiteLLM <-> gateway 内部链路：httpx.AsyncClient + socket_options keepalive
  （K8s 集群内无 CF；keepalive 必须 day-1 设，避免 dead-conn leak）

MVP 不引入连接池池化的并发抢占 —— 单 AsyncSession instance 全 gateway 共享。
"""
from __future__ import annotations

import socket
from contextlib import asynccontextmanager
from typing import Any

import httpx

from .config import (
    HTTPX_CONNECT_TIMEOUT_S,
    HTTPX_KEEPALIVE_EXPIRY_S,
    HTTPX_MAX_CONNECTIONS,
    HTTPX_MAX_KEEPALIVE,
    HTTPX_POOL_TIMEOUT_S,
    HTTPX_WRITE_TIMEOUT_S,
    TCP_KEEPCNT,
    TCP_KEEPIDLE_S,
    TCP_KEEPINTVL_S,
)


def make_httpx_internal_client() -> httpx.AsyncClient:
    """gateway 内部链路（暴露给 LiteLLM 那一面 + 走 K8s svc DNS 的反向调用）。"""
    keepalive_opts = [
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    ]
    # darwin / linux 部分常量存在性差异，按需附加
    if hasattr(socket, "TCP_KEEPIDLE"):
        keepalive_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_KEEPIDLE_S))
    elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS
        keepalive_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, TCP_KEEPIDLE_S))
    if hasattr(socket, "TCP_KEEPINTVL"):
        keepalive_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_KEEPINTVL_S))
    if hasattr(socket, "TCP_KEEPCNT"):
        keepalive_opts.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_KEEPCNT))

    transport = httpx.AsyncHTTPTransport(socket_options=keepalive_opts)
    return httpx.AsyncClient(
        transport=transport,
        limits=httpx.Limits(
            max_connections=HTTPX_MAX_CONNECTIONS,
            max_keepalive_connections=HTTPX_MAX_KEEPALIVE,
            keepalive_expiry=HTTPX_KEEPALIVE_EXPIRY_S,
        ),
        timeout=httpx.Timeout(
            connect=HTTPX_CONNECT_TIMEOUT_S,
            read=None,                      # SSE 流不能加 read 超时
            write=HTTPX_WRITE_TIMEOUT_S,
            pool=HTTPX_POOL_TIMEOUT_S,
        ),
    )


@asynccontextmanager
async def cf_impersonating_session():
    """yield curl_cffi.AsyncSession(impersonate=chrome120)。

    懒导入：本地开发不一定安装 curl_cffi（pyproject.toml 声明了，但 CI 环境可能裸 pytest）。
    单测里不应当真的拨号上游，因此把 import 推迟到第一次调用。
    """
    try:
        from curl_cffi.requests import AsyncSession  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "curl_cffi missing — gateway 进程必须装 curl_cffi 才能调 chatgpt.com / auth.openai.com"
        ) from e

    session: Any = AsyncSession(impersonate="chrome120")
    try:
        yield session
    finally:
        await session.close()
