# syntax=docker/dockerfile:1.6
# chatgpt-pool-gateway image
# 构建: jms ssh k8s-work-226 'cd /root/chatgpt-pool-gateway && nerdctl build -t IMG -f Dockerfile .'
# 必须在 47.84.112.136 (k8s-work-226) 上构建; 禁本地 Mac (架构不匹配, CLAUDE.md)

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# jemalloc: 长跑 SSE worker 抗 glibc malloc 碎片
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjemalloc2 ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

ENV LD_PRELOAD=/usr/lib/x86_64-linux-gnu/libjemalloc.so.2

WORKDIR /app

# 先装依赖 (layer cache); pyproject 在 ./gateway/
COPY gateway/pyproject.toml ./pyproject.toml
RUN pip install --upgrade pip && \
    pip install \
        "fastapi>=0.115" "uvicorn[standard]>=0.32" "curl_cffi>=0.7" \
        "httpx>=0.27" "prometheus-client>=0.21" "pydantic>=2.9"

# 拷代码
COPY gateway/app/ ./app/
COPY gateway/tests/ ./tests/

# 运行期目录
RUN mkdir -p /data/auth && chmod 0750 /data

EXPOSE 8080

# uvicorn workers=1 + asyncio loop=uvloop (uvicorn[standard] 自带)
# limit-concurrency 见 config.UVICORN_LIMIT_CONCURRENCY
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8080", \
     "--workers", "1", \
     "--limit-concurrency", "300", \
     "--timeout-keep-alive", "30", \
     "--log-level", "info"]
