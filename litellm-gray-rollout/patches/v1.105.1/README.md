# v1.105.1 升级基线（2026-10-16 夜 ~ 10-17）

上一代 `../v1.103.2/`。198 现场工作目录：`/root/l3/up1051-20261016T161310Z/`（root 0700）。

## 镜像

| | tag | digest |
|---|---|---|
| vanilla | `127.0.0.1:5000/litellm-carher:vanilla-v1.105.1` | `sha256:b4239fbb9be0d915c312632ccdcccea441db9994a341536fdbf32cae82e15f9c`（= ghcr 官方 amd64 manifest） |
| 烘焙 | `vanilla-v1.105.1.capacity.sse-fix-bare.alias-price.keyalias-auth-20261017-022526` | `sha256:d8a6bfbc8b3fa9b170badb6cdf1278b3892bc94fe178b1c20d78723d5c50698a` |

md5 join：两边各 20575 个文件、零增删，只 5 个文件不同（下表四个补丁动到的文件）。

## 四个烘焙补丁（Dockerfile 里每个都有 grep 门禁）

| 脚本 | 做什么 | 1.105.1 上的状态 |
|---|---|---|
| `litellm-patch-capacity-ratelimit.py` | 上游 "selected model is at capacity" → 429 | 与 v1.103.2 逐字节相同，锚点 1 命中 |
| `litellm-patch-sse-lifecycle.py` | Responses 流补齐生命周期事件 + 裸 response 对象 | 两个 loop-tail 锚点重定（上游 `_yielded_first_chunk=True` → `_note_yielded_event(result)`）；新增 `_carher_adopt_bare_terminal`：1.105 流结束时没有 completed_response 会判「提前结束」报错，裸 response 被包成终止事件后补记，保持 1.103.2 的正常结束 |
| `litellm-patch-stream-alias-pricing.py` | 上游 #44341 原样回移（1.103.3 有、1.105.1 没有）：流式经别名少计费 | 阴性对照：vanilla 1.105.1 流式 `claude-opus-4.8-<digits>` 别名 cost=0.0；打上后 = 同 deployment 的正常价；其余 11 组不变 |
| `litellm-patch-key-alias-auth-compat.py` | 上游 #43049 只在**鉴权**上回退 | 不回退 ⇒ 2116 把 key 里 1323 把对白名单内的名字 403（灰度并排克隆真 key 实测）。路由改写不经过该函数，不受影响 |

## 整文件覆盖件（overlays/，按 1.105.1 重做）

| 文件 | 我们的改动 | 重放 |
|---|---|---|
| `litellm_core_utils/streaming_handler.py` | +7/-0 midstream 前触发 cooldown | `patch` 干净打上 |
| `proxy/pass_through_endpoints/streaming_handler.py` | +18/-4 首字时间不被空帧/心跳钉 | 手工重放（上游在两个调用点旁加了 TinyFish 计费） |
| `.../anthropic_passthrough_logging_handler.py` | +14/-0 网宿 GLM server_tool_use dict → SDK 对象 | `patch` 干净打上（上游此文件 1.103.2→1.105.1 未变） |

## callback / config 变化

- 下线 `midstream_fallback_loop`：1.105.1 上游已有多跳 midstream（#42283/#43133/#43238），老补丁指纹仍命中会覆盖上游实现。
- `streaming_bridge.py` / `anthropic_bridge_error_surface.py`：先 import `pass_through`（#43329 改名），回落 `experimental_pass_through`。
  streaming_bridge 的 `patched_init` 改为先调上游 `__init__`（1.103+ 有 `completion_start_time`）。
  bridge_error_surface 在 1.105.1 上指纹不符自动拒装，用上游自带的流内 error 事件（#43126/#41826）。
- config：`litellm_settings.budget_exceeded_status_code: 429`（1.104+ 默认 422）。

## DB

备份 `/Data/backup/litellm-prod-pre-v1105-20261016T170735Z/`（81 MB，sha256 `cab60246…`）。
clone A/B/C 四条兼容腿全 PASS 后生产迁移：21 s，185→196 条，零表重写、零列丢失；旧 1.103.2 照常读写。
⚠️ `kubectl exec -i` 往 pod 灌 80 MB 会卡死 apiserver websocket ⇒ 校验 dump 用 `kubectl cp`。
