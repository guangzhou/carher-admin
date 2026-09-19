# 阿里云 grok-4.6 / 上游 sa-grok-4.6 的 usage 返回验证

- 脚本：`scripts/aliyun-grok46-usage-probe.py`（只读，不写任何配置）
- 验证时间：2026-09-19
- 起因：同事报「sa-grok-4.6 每次请求返回没有 usage」
- **结论：同事说的是真事，而且已修。** 根因不是模型，是两个集群的一个配置差异：
  **198 有 `general_settings.always_include_stream_usage: true`，阿里云没有**
  ⇒ 同一个客户端同一份代码，裸流式打 198 有 usage、打阿里云没有。
  2026-09-19 已给阿里云补上该键，两跳行为现已一致。

> **订正**：本文初稿写的是「阿里云不复现，让客户端加 `include_usage` 即可」。
> 那句话在 198 成立、在阿里云不成立 —— 当时只测了阿里云一跳就外推了。
> **两个集群同名模型、同版本镜像（都是 v1.90.2），也能因为一个
> `general_settings` 键的有无给出相反行为。** 跨集群的「有没有 X」两边都得实测。

## 先分清是哪个「没有 usage」

这两件事都能同时为真，量的东西完全不同，别混：

| | 问的是什么 | 在哪里看 | 本文测的 |
|---|---|---|---|
| (1) 上游 → litellm | 上游有没有把 token 数报给 litellm | DB `LiteLLM_SpendLogs` 的 `cached_tokens` / `reasoning_tokens` | 否 |
| (2) litellm → 客户端 | 响应体里有没有 usage 字段 | HTTP 响应 body / SSE 末帧 | **是** |

同事描述的是 (2)。之前查 SpendLogs 得到的结论属于 (1)，不能拿来回答这个问题，
反之也不行。

## 名字对不上：阿里云上没有 `sa-grok-4.6`

按名字直接打会 404 —— `sa-grok-4.6` 是**上游名**，不是阿里云对外名。实际是两跳：

```
客户端
  └─> 阿里云 litellm (ns carher)         模型名 grok-4.6
        └─> custom_openai/sa-grok-4.6    api_base https://cc.auto-link.com.cn/pro/v1  (=198)
              └─> 真实上游
```

阿里云 CM `litellm-config` 里的条目：

```yaml
model_name: grok-4.6
litellm_params:
  model: custom_openai/sa-grok-4.6
  api_key: os.environ/PRO198_BRIDGE_API_KEY
  api_base: https://cc.auto-link.com.cn/pro/v1
model_info:
  id: pro198/grok-4.6
  access_groups: [pro198]
```

两个后果：

1. 测阿里云要用 `grok-4.6`，测 198 要用 `sa-grok-4.6`。搞错就是 404，
   而 404 会被误读成「模型坏了」。
2. usage 可能在任意一跳丢。**只测一跳分不清是哪跳丢的** —— 所以脚本默认 `--hop both`。
   `custom_openai/` 这个 provider 前缀的响应处理路径和原生 `openai/` 不同，
   是最值得怀疑的一段，但「值得怀疑」不是证据，得实测。

## 四个用例，以及为什么 C/D 判缺失不算 bug

OpenAI 兼容协议下，流式响应的 usage 在一个 `choices` 为空的末帧里，
**且只在请求带了 `stream_options.include_usage: true` 时才发**。
所以期望值得按协议写，不能按愿望写：

| 用例 | 请求形状 | 协议期望 | 缺 usage 算 bug 吗 |
|---|---|---|---|
| A | 非流式 | 有 usage | **算** |
| B | 流式 + `include_usage: true` | 有 usage | **算** |
| C | 流式 + 不带 `include_usage` | 没有 usage | 不算 |
| D | 流式 + `include_usage: false` | 没有 usage | 不算 |

脚本只在 A/B 缺 usage 时以 exit code 1 报真故障；C/D 缺失是记录，不是告警。

## 判据：ZERO 也算缺失

`classify_usage()` 把结果分成 `OK / MISSING / ZERO / MALFORMED`。
关键是 **`prompt_tokens=0` 的空壳单独成一档**：litellm 拿不到上游 usage 时，
会返回一个计数全 0 的 usage 对象。

**「字段存在」不是判据。** 对计费和 OWUI 的 compaction 阈值来说，
`{"prompt_tokens":0,"completion_tokens":0}` 和完全没有 usage 是一回事。
只看 `if "usage" in body` 会把这种情况读成绿。

「真 usage」的加强判据：`prompt_tokens_details.cached_tokens` 和
`completion_tokens_details.reasoning_tokens` 有值 —— 这两个 litellm 本地算不出来，
有值就证明上游确实报了。

## 先跑量具自检（第 0 步）

全绿的探测结果**不能自证量具有效**：一个永远返回 OK 的 `classify_usage`
也会给出同样漂亮的 12/12。所以先喂已知坏形状：

```bash
python3 scripts/aliyun-grok46-usage-probe.py --selftest
```

实测 `PASS`：4 种坏形状（无字段 / 0-0 空壳 / 非对象 / 三计数全缺）全部被抓到，
真计数不误报，SSE 提取器带 usage 抓到、不带的报 None。这一步不打任何真实请求。

## 怎么跑真实探测

前提：阿里云 kubectl 通道（jms 隧道）

```bash
kubectl get ns >/dev/null 2>&1 || \
  nohup scripts/jms proxy laoyang 16443 172.16.1.163 6443 > /tmp/jms-proxy.log 2>&1 &
```

```bash
# 阿里云一跳，四个用例各一次
python3 scripts/aliyun-grok46-usage-probe.py --hop aliyun

# 同事说「每次」—— 重复 3 轮查是否间歇
python3 scripts/aliyun-grok46-usage-probe.py --hop aliyun --repeat 3

# 长 prompt，看 cached_tokens 是否也透传
python3 scripts/aliyun-grok46-usage-probe.py --hop aliyun --long
```

key 取自 HerInstance CRD 的 `{.spec.litellmKey}`（`her-1000`，即 carher-1000）。
脚本只打印长度，不回显值。

## 实测结果：改前，两跳同窗口各 3 轮

关键差异在 C 那一行：

| 用例 | 阿里云 | 198 |
|---|---|---|
| A 非流式 | OK `prompt=641 [cached=640]` | OK `prompt=641 [cached=640]` |
| B 流式+`include_usage:true` | OK `sse_chunks=14` | OK `sse_chunks=14` |
| **C 流式裸** | **MISSING** `sse_chunks=13/14` | **OK** `sse_chunks=15` |
| D 流式+`include_usage:false` | MISSING | MISSING |

两边 3 轮各 12 次，分野稳定、没有间歇。`prompt_tokens` 随 prompt 长度
从 641 变到 4647（`--long`），是真计数不是写死的常量。

补充证据：阿里云 SpendLogs 里那几次裸流式 `prompt_tokens=641` **是对的** ——
litellm 手里有数，只是没写进给客户端的响应体。所以这不是「拿不到」，是「没回传」。

## 根因：一个 `general_settings` 键

```yaml
# 198 有，阿里云原来整个 general_settings 键都不存在
general_settings:
  always_include_stream_usage: true
```

消费它的代码在 `proxy/common_request_processing.py:1113`（两边都是 v1.90.2，
已在阿里云 pod 内 grep 确认该版本确实读这个键）：

- `stream_options` 缺失 → 补 `{"include_usage": True}`
- `stream_options` 在但没有 `include_usage` 键 → 补上
- **显式 `include_usage: false` → 不覆盖**

第三条正好解释了 D：D 在两边都仍然没有 usage。**这是个天然的阴性对照** ——
如果我读错了代码路径，D 不会这么听话。所以这不是「强制开启」，只是补默认。

排除过的其它可能：两侧 config 里都没有任何 `include_usage` / `stream_options`
字样（`grep` 命中 0）；198 的 33 个 callback 里也没有注入它的（唯一命中是
`opus_47_fix.py` 的一句注释，内容恰好是说这件事必须走 config 而非 pre-call hook）。

## 修复（2026-09-19，已上线）

给阿里云 CM `litellm-config` 的 `config.yaml` 末尾**纯追加** 9 行（含注释）。

- 改前 sha256 `eb77593b2e6bb931` → 改后 `b8da8ad91f722595`
- 备份：`/tmp/usage-fix-20260919-100642/litellm-config.BEFORE.yaml`（sha `53d88ad0901d629c`）
- 自检：`model_list` 168 个逐条不变、`litellm_settings` / `router_settings`
  逐字节相同、`diff` 只有 9 个 `>` 零个 `<`
- 没走 YAML round-trip —— 2388 行重新 dump 会洗掉注释和引号风格，
  那是个比目标大得多的改动面

**⛔ 该 CM 是 `subPath` 挂载，改完不会自动同步进容器，必须 rollout。**
Deployment 是 RollingUpdate / 2 副本 / `maxUnavailable=1` ⇒ 零中断，
全程有 pod 在服务。判据是**容器内** `sha256 /app/config.yaml`，两个 pod
都是 `b8da8ad91f722595`（CM 里对了不算，得容器里对）。

回滚：删掉 `config.yaml` 末尾那 9 行，再 rollout 一次。

### 改后验证

同窗口两跳各 3 轮，24/24 对齐：

| 用例 | 阿里云 | 198 |
|---|---|---|
| A 非流式 | OK ×3 | OK ×3 |
| B 流式+`include_usage:true` | OK ×3 | OK ×3 |
| **C 流式裸** | **OK ×3**（翻了） | OK ×3 |
| D 流式+`include_usage:false` | MISSING ×3 | MISSING ×3 |

生产回归（改动影响每一个流式请求，所以判据是真实流量不是我的探针）：
切换后 322 次调用，`prompt_tokens=0` 的行数 1 条、与改前 1020 次里的 1 条持平，
平均 `prompt_tokens` 58814 → 63888 同量级。新 pod 日志里唯一的 ERROR 是
既存的 key 白名单形状，与 stream/usage 无关。

## 客户端侧

现在两边都不需要改客户端了。但显式带上仍然是好习惯（不依赖服务端配置）：

```json
{"model": "grok-4.6", "stream": true,
 "stream_options": {"include_usage": true}}
```

注意反过来：**显式写 `include_usage: false` 的客户端拿不到 usage**，
服务端这个旋钮不会覆盖它 —— 这是设计如此，不是漏修。
