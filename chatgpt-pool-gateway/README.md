# chatgpt-pool-gateway

198 ChatGPT Pro 账号池 Gateway。把 37 个 per-account LiteLLM Pod (~40 GiB 内存) 收敛到 2 个 FastAPI Pod (active=198 / backup=225)。

## 状态

- Phase 1 ✅ mock-chatgpt-upstream（litellm-dev/aiyjy-litellm-standby）
- Phase 2 🚧 gateway 骨架 + SQLite registry + picker + 状态机（本地仓库内开发，不上 dev 集群）

## 边界（必须遵守）

- **dev 沙箱命名空间 `litellm-dev` only**：开发期间任何 K8s 资源都不允许进 `litellm-product`
- **写盘只走 K8s Secret + tmpfile + os.replace**：refresh_token rotation 原子化，不允许跨进程同时持有同一账号 token
- **出向调 chatgpt.com 一律 curl_cffi.AsyncSession(impersonate="chrome120")**：raw httpx 会被 CF JA3/JA4 拦
- **内部链路（LiteLLM ↔ gateway）走 httpx**：K8s 集群内无 CF，且需要 socket_options keepalive
- **绝不依赖 mid-stream fallback**：fail-fast at connect，第一个字节 commit 后绝不切上游
- **绝不在请求路径里做 wham/usage 探针**：60s 后台 tick + readiness 读内存 dict

## 内存预算（强制）

| 项 | 值 | 依据 |
|----|----|------|
| requests.memory | 2 Gi | 单 gateway pod baseline |
| limits.memory | 3 Gi | 留 50% 抗碎片 |
| uvicorn workers | 1 | async 多 worker 会乘上连接池/内存 |
| --limit-concurrency | 300 | 超过返 429 让 LiteLLM fallback |
| max_connections | 200 | httpx upstream pool |
| max_keepalive_connections | 50 | 防 dead-conn leak |
| keepalive_expiry | 30s | 同上 |
| Python 内存分配器 | jemalloc preload | 抗 long-running 碎片 |
| daily rollout | cron | 兜底 Python TLS leak (cpython#34745) |

整 198 节点所有 Pod limits 总和必须 ≤ 节点可分配 80%（physical 63 GiB → cap 50 GiB）。MVP 上线后立刻砍 37 acct Pod = 立省 ~37 GiB headroom。

## 测试

```bash
cd chatgpt-pool-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest gateway/tests -v
```

## 目录

```
gateway/
  app/
    config.py        # 全局常量（内存/并发/超时）
    state.py         # 账号状态机 (healthy/cooling/offline/token_invalidated/disabled)
    registry.py      # SQLite WAL 账号注册表
    picker.py        # 账号选择算法
    auth.py          # refresh_token rotation 锁 + 原子写
    upstream.py      # curl_cffi 出向客户端
    sse.py           # \n\n 边界 buffer / event 解析
    convert.py       # chat/completions <-> responses 转换
    routes/
      chat.py        # POST /v1/chat/completions
      health.py      # /health/live, /health/ready
      admin.py       # internal admin endpoints
    main.py          # FastAPI app
  tests/             # pytest 单测（纯函数 + fake registry）
mock/                # mock chatgpt.com upstream（已部署 litellm-dev）
manifests/           # dev K8s manifest（已 apply）
scripts/             # 运维脚本（后续）
```
