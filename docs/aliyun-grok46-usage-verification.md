# 阿里云 grok-4.6 / 上游 sa-grok-4.6 的 usage 返回验证

- 脚本：`scripts/aliyun-grok46-usage-probe.py`（只读，不写任何配置）
- 验证时间：2026-09-19
- 起因：同事报「sa-grok-4.6 每次请求返回没有 usage」
- 结论：**阿里云这一跳没有复现。** 该说法最可能的解释是客户端流式请求没带
  `stream_options.include_usage` —— 那种情况下没有 usage 是协议规定，不是故障。
- 未测：**198 那一跳没测**（缺桥凭据，见文末）。所以本文只能否掉「阿里云丢 usage」，
  不能否掉整条链路的所有可能。

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

## 实测结果

`--hop aliyun`，单轮：

| 用例 | 判定 | 读数 |
|---|---|---|
| A 非流式 | OK | `prompt=641 completion=1 total=719 [cached=512 reasoning=77]` |
| B 流式+include_usage | OK | `prompt=641 total=642 [cached=640 reasoning=245] sse_chunks=15` |
| C 流式裸 | MISSING | `sse_chunks=13`（协议正确） |
| D 流式+false | MISSING | `sse_chunks=14`（协议正确） |

`--repeat 3`：A.1–A.3 全 OK（`cached=640`，reasoning 67/181/72）；
B.1–B.3 全 OK（reasoning 150/162/187）；C/D 六次全 MISSING，一致。

`--long`：A OK `prompt=4647 [cached=512 reasoning=210]`；
B OK `prompt=4647 [cached=4608 reasoning=118] sse_chunks=39`；C/D MISSING。

12/12 一致，且 `prompt_tokens` 随 prompt 长度变化（641 → 4647）——
是真计数，不是写死的常量。

## 结论与客户端改法

- **阿里云一跳没有 usage 丢失。** A/B（协议要求必须有 usage 的两个用例）
  每次都返回真实计数，且 `cached_tokens` / `reasoning_tokens` 有值，
  说明上游到 litellm 这段也在报。
- 同事看到的「每次都没有」，形状与 C/D 完全一致。客户端改一行即可：

```json
{"model": "grok-4.6", "stream": true,
 "stream_options": {"include_usage": true}}
```

## 还没查的部分

**198 那一跳没测。** 它的凭据是桥 key（`PRO198_BRIDGE_API_KEY`），
不是 carher key，取自阿里云 litellm 的 Secret。脚本在缺这个变量时明确跳过并说明，
不假装测过。要补：

```bash
export PRO198_BRIDGE_API_KEY=...   # 取自阿里云 litellm Secret
python3 scripts/aliyun-grok46-usage-probe.py --hop 198 --repeat 3
```

如果 198 那跳也全绿，则整条链路都不丢 usage，问题 100% 在客户端请求形状；
如果 198 那跳有缺失，那就是阿里云侧补齐了什么、需要另查。
