---
name: litellm-passthrough-endpoint
description: >-
  上游是 OpenAI-compatible 但 **LiteLLM 的那条标准通路把请求改坏了**时的逃生门：
  `POST /config/pass_through_endpoint` 建一条纯配置反代，零代码、零重启、零 apply。
  首个实例 = sub2api 的 grok video（`sa-grok-imagine-video`）：LiteLLM `/v1/videos`
  无条件发 multipart 而 sub2api 只吃 JSON，**且这是 upstream PR #38104 故意为之
  ⇒ 升级永远修不好**。涵盖「怎么证明是通路而不是上游坏了」的四发探针矩阵、
  `include_subpath`/`auth` 两个不变量、三段验收梯子、以及**不进 SpendLogs 这个代价**。
  Use when 某个模型 `/model/new` 返 200、`/model/info` 读得到、但一发打不通；
  或"接入的是异步任务型 API（提交→轮询→取产物）"；或"上游返 415 / LiteLLM 侧
  pydantic ValidationError"；或用户说"grok 出图能用出视频不能用"/"视频模型打不通"。
---

# LiteLLM pass-through endpoint —— 标准通路改坏请求时的逃生门

## 何时该怀疑是通路而不是上游

形状是**同一把 key、同一个上游，A 能用 B 不能用**，而且 B 在 LiteLLM 里
「注册成功、读得到、就是打不通」：

| 症状 | 含义 |
|---|---|
| `/model/new` 返 200、`/model/info` 也读得到，一发也打不通 | 写入和调用是两条路，200 只证明写了一行 |
| 上游返 **415 Unsupported Media Type** | LiteLLM 换了 `Content-Type`（multipart ↔ JSON） |
| LiteLLM 侧 pydantic `ValidationError: Field required` | 上游返的 body 不符合 LiteLLM 的强类型模型 |
| 上游日志里 `body_bytes` 只有十几个字节 | multipart 的内容不在 `data` 里，上游按 JSON 解只看到空壳 |

⛔ **别先去修上游、别先怀疑账号/余额/限流。** 先跑下面那个矩阵。

## 第 1 步：四发探针矩阵 —— 证明「编码是唯一变量」

**绕开 LiteLLM，直打上游。同一把 key、同一个 body、只变编码和路径。**
这四发是「LiteLLM 打不通」的唯一直接证据，比读源码有力（代码存在 ≠ 该路径被执行）。

```bash
# 198 上，脚本已内建这一步
python3 scripts/litellm-198-sub2api-video-passthrough.py probe
```

2026-09-20 对 sub2api（NodePort `:31880`）实测：

| 请求 | 结果 |
|---|---|
| `POST /v1/videos` **JSON** | **200** `{"request_id": …}` |
| `POST /v1/videos` multipart | 415 `xAI upstream returned status 415` |
| `POST /v1/videos/generations` **JSON** | **200** |
| `POST /v1/videos/generations` multipart | 415 |

⇒ 上游是好的，**是编码被改了**。`bodies` 必须只有一种，否则这个矩阵证明不了
编码是变量（脚本的单测锁了这条）。

## 第 2 步：确认「升级修不好」再决定方案

⚠️ **这一步别省。** 如果 upstream 已经修了，正解是升级而不是加一层反代；
如果 upstream 是**故意这么改的**，等升级就是永久等待。

看外网必走 188（本机对 github/docs 的 connect 挂住）：

```bash
ssh cltx@10.68.13.188 'curl -s "https://api.github.com/search/issues?q=repo:BerriAI/litellm+videos+multipart" | head -c 3000'
ssh cltx@10.68.13.188 'curl -s https://api.github.com/repos/BerriAI/litellm/pulls/38104 | python3 -c "import json,sys;d=json.load(sys.stdin);print(d[\"title\"],d[\"merged_at\"],d[\"base\"][\"ref\"])"'
```

video 这一例的结论（存档，不用重查）：

- **PR #38104**「fix: match OpenAI SDK wire format on image/video routes」，
  **2026-08-24 合入** `litellm_internal_staging`。它**故意**把 `/v1/videos` 从 JSON
  改成**无条件 multipart**，去对齐官方 OpenAI SDK（`Videos.create` 无条件设
  `Content-Type: multipart/form-data`）。**早于我们跑的 v1.100.1**
  ⇒ multipart 是 intended behavior，**升级只会让它更牢固**。
- **issue #36493**（仍 open，报在 v1.95.0）正是这一类：自建的 OpenAI-compatible
  image/video 后端过不了 LiteLLM 的标准 route，列了 4+1 个缺口。
- issue #36487（已关）是 `/v1/videos/edits` 的 `RuntimeError: Stream consumed`，
  相邻但不是这个故障，别混。

## 第 3 步：三处不兼容各自独立 —— 只修一处没用

sub2api video 这一例，`/app/.venv/lib/python3.13/site-packages/litellm` 里实读：

| # | 位置 | 内容 | 后果 |
|---|---|---|---|
| 1 | `llms/openai/videos/transformation.py:106` | `use_multipart_form_data()` 恒 `True` | 415 |
| 2 | 同文件 `get_complete_url()` | 硬编码 `f"{api_base.rstrip('/')}/videos"` | 真身是 `/v1/videos/generations`（裸 `/v1/videos` 恰好也通，所以这条次要）|
| 3 | 同文件 `transform_video_create_response` | `VideoObject.model_validate(raw_response.json())`，强制 `id`+`object`+`status` | 即使编码修好，sub2api 返 `{"request_id": …}` 仍必炸 |

发送分支在 `llms/custom_httpx/llm_http_handler.py:7110`（异步 7217）。
provider 全表在 `utils.py:9109 get_provider_video_config`：`openai`/`azure`/`gemini`/
`vertex_ai`/`runwayml`/`hosted_vllm`。**六个全查过，没有一个能借**：
`hosted_vllm` 直接 `class HostedVLLMVideoConfig(OpenAIVideoConfig)`，docstring 自己写着
"requires multipart/form-data"；其余四个是厂商私有协议。
⇒ **不存在可用的 JSON video config**，写一个 provider config 是唯一的"改代码"路，
而 pass-through 不用改代码。

## 第 4 步：建 pass-through

```bash
# 198 上
MK=$(sudo kubectl -n litellm-product get secret litellm-secrets \
      -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
export LITELLM_MASTER_KEY=$MK

python3 scripts/litellm-198-sub2api-video-passthrough.py plan          # 只读，看现状+漂移
python3 scripts/litellm-198-sub2api-video-passthrough.py apply         # dry-run
python3 scripts/litellm-198-sub2api-video-passthrough.py apply --apply
```

payload 的形状（`PassThroughGenericEndpoint`）：

```json
{"path": "/sa-video",
 "target": "http://sub2api.litellm-dev.svc.cluster.local:8080/v1/videos",
 "headers": {"Authorization": "Bearer <上游 key>", "Content-Type": "application/json"},
 "include_subpath": true,
 "auth": true}
```

### 两个不变量（写错各有一个静默后果）

- 🔴 **`include_subpath: true`** —— 异步任务型 API 是三段式，`/generations`、
  `/{rid}`、`/{rid}/content` 三条子路径都得转发。写 false 只有根路径通，
  子路径全 **404**，形状和"上游没这个接口"一模一样。
- 🔴 **`auth: true`** —— 保住 LiteLLM 侧的 key 认证。写 false 这条路就是一个
  **匿名开放口**，而且它背后挂着一把真的上游 key。

模型的其余字段：`default_query_params` `{}`、`cost_per_request` `0.0`、
`timeout` `null`、`guardrails` `null`、`is_from_config` `false`、`methods` `null`。

### 凭据纪律

上游 key **只从上游自己的库/env 现取，不进仓库、不进 SKILL、不打全文**：

```
sub2api: postgres api_keys.key where name='grok-litellm'   ⚠️ 列名是 `key` 不是 `key_value`
```

⛔ `os.environ.get("K","<真key>")` 那种兜底默认值等于**已提交的凭据**，
一旦推上 origin 只有轮转能解。脚本的单测按**反模式**锁了这条（不是按 key 的值搜）。
打印一律走 `redact_endpoint()`。

## 第 5 步：三段验收梯子 —— 缺一段都不算通

```bash
python3 scripts/litellm-198-sub2api-video-passthrough.py verify   # 跑一发真片子，会真花钱
```

它做的事，和每一段的坏尺子：

| 段 | 判据 | ⛔ 坏尺子 |
|---|---|---|
| ① `POST /sa-video/generations` | 200 **且拿到非空 `request_id`** | 空 id 往下轮询会得到一排 `404 Video request not found`，**形状和 pass-through 坏了一模一样**。09-20 我就是被自己的 shell 嵌套引号吞掉了 `request_id`，白查了 14 发 |
| ② `GET /sa-video/generations/{rid}` | 轮询到 **`status=done`** | `202 pending` 不是绿，它可能永远不 done |
| ③ `GET /sa-video/{rid}/content` | 200 + **body 前 16 字节里有 `ftyp`** + 字节数够大 | `Content-Type: video/mp4` 是上游自报的，不看 magic 等于没验 |

2026-09-20 实测通过：`request_id=17f75b02-…`，progress 1%→10%→75%→done，
`duration=8`，`cost_in_usd_ticks=6400000000`（= $0.64；另一发 8s 片子是 4e9 = $0.40，
所以 tick 是 `1e10 ticks = $1`），content 返 **5,150,228 bytes**，头
`0000 0020 6674 7970 6973 6f6d`（`ftypisom`）。

## 代价（必须写进交付说明，别让人以为免费）

🔴 **pass-through 不进 LiteLLM 计费 / SpendLogs。** `cost_per_request` 只有
「每发定额」这一种形状，而 video 是**按秒**计费 ⇒ 填它只会记错，不填就是不记。
账在上游自己的 ledger 上（sub2api `usage_logs.total_cost` + 响应里的
`usage.cost_in_usd_ticks`）。对账要去上游查，不是查 `LiteLLM_SpendLogs`。

其余边界：

- pass-through 的 `path` 是**顶层路由**，不是 model_group ⇒ `/v1/models` 里
  **看不到**它，per-key `models` 白名单也**管不到**它（准入只有 `auth: true` 那道）。
- 因此它也**不参与** `model_group_alias` / `fallbacks` / router 那一整套。
- 想收回：`remove --apply`（先 dry-run 看要删什么）。回滚 = 重跑 `apply --apply`，
  凭据从上游库现取，不依赖备份文件。

## 配套：model_list 里那两行死条目要删掉

`mode=video_generation` **写得进 DB**（返 200、`/model/info` 读得到），
所以它会以「一行永远打不通的条目」长期存在，还会被下一个人当成"已接入"。
09-20 已删，并在 builder 里改成**显式 raise**，防止再写进去：

```
# 备份（两行 LiteLLM_ProxyModelTable 的 row_to_json，2 行 / 2122 字节）
198:~/grok-onboard/backups/video-models-20260920-140413.json
# 删除
POST /model/delete  {"id": "sa/grok-imagine-video"} / {"id": "sa/grok-imagine-video-1.5"}
# 判据：DB 里 imagine-video 计数 0，`sa-grok%` 仍 22 行；逐 pod /model/info 里 visible=0
```

⚠️ **删完必须做用户面回归**，而且**带唯一 nonce**（LiteLLM 响应缓存是开的，
逐字节相同的枪不到上游）。09-20 跑的那组：`sa-grok-4.6`（**cursor gpt 家族的线上
fallback 落点，最要紧的一条**）、`sa-grok-4.5`、`sa-grok-4.3`、`sa-composer-2.5`
全 200 且回显 nonce，`sa-grok-imagine` 出图 200 返真 `imgen.x.ai` URL。

⚠️ 清点剩余条目时**别用 `startswith("sa-grok")`** —— `sa-composer-2.5` 没有 grok 前缀，
会被漏掉读成"少了一个"。用 `startswith("sa-")`。

## 常见踩坑

| 症状 | 根因 | 解 |
|---|---|---|
| `/model/new` 200、`/model/info` 有、一发打不通 | 写入和调用两条路 | 跑上面那个四发矩阵，先分清是通路还是上游 |
| 上游 415，且日志里 `body_bytes` 只十几字节 | LiteLLM 发了 multipart，上游按 JSON 解只看到空壳 | pass-through |
| 「等下个版本修」 | 该行为是 upstream **故意**改的（#38104） | 先查 PR 的 `merged_at` 和我们的版本号谁先谁后 |
| 建完 pass-through，根路径通、子路径全 404 | `include_subpath` 是 false | 改 true |
| 轮询一排 `404 Video request not found` | **`request_id` 没抓到就开始轮询**（shell 嵌套引号吞掉了管道）| 先单独打 POST 确认拿到真 id，再轮询 |
| `Content-Type: video/mp4` 但文件打不开 | 那个头是上游自报的 | 判 body 前 16 字节有没有 `ftyp` |
| 上了 pass-through 之后 SpendLogs 里查不到这笔账 | **pass-through 不进计费**，这是设计 | 去上游 ledger 对账；`cost_per_request` 是定额，按秒计费的场景填了只会记错 |
| 在 litellm pod 里 `curl` 报 not found | 镜像没装 curl | `python3 -c` + `urllib.request` |
| 查 `/config/pass_through_endpoint` 读出"什么都没有" | 查错 namespace | 生产是 **`litellm-product`**（NodePort 30402）；`litellm-dev`（30400）是 dev，sub2api 本身住在那 |

## 相关 skill

- `sub2api-grok-ops` —— grok 腿的健康度 / park / 充值 / 建号
- `add-litellm-model` —— 走 model_list 的正常接入流水线
- `litellm-198-key-allowlist` —— 新名字要不要进 per-key 白名单
  （pass-through 的 `path` **不受**白名单管，只有 model_group 才受）
