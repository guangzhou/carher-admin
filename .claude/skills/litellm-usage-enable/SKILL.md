---
name: litellm-usage-enable
description: >-
  排查并修复「LiteLLM 响应里没有 usage / token 数」——先用四档用例分清
  「协议正确的没有」和「真 bug 的没有」，再在服务端打开
  general_settings.always_include_stream_usage 让客户端不用改代码，
  最后用 --mode after 做可证伪的验收。适用于任何一套 LiteLLM Proxy
  （自建 / 本仓 198 / 阿里云 carher 都一样）。
  Use when the user mentions "litellm" + 没有 usage / usage 缺失 /
  拿不到 token 数 / token 统计不准 / prompt_tokens 是 0 / 上下文显示
  "?/400k" / SpendLogs 少行 / stream_options / include_usage /
  always_include_stream_usage / 流式没有 usage。
---

# LiteLLM：把 usage 打开

## 一句话

绝大多数「LiteLLM 每次请求都不返回 usage」的报告，**不是故障**，是
OpenAI 兼容协议的规定行为：流式请求不带 `stream_options.include_usage`
就本来没有 usage。修法是在 **Proxy 服务端**打开一个开关，客户端一行不用改。

但**不许**跳过诊断直接改开关 —— 有一小部分是真 bug（上游把 usage 吞了），
那种情况开关打开也没用，而你会因为"改了个配置"误以为修好了。

## 第 0 步：先分清在问哪个「没有 usage」

这两件事都能同时为真，量的东西完全不同，混了必然误判：

| | 问的是什么 | 在哪里看 |
|---|---|---|
| (1) 上游 → LiteLLM | 上游有没有把 token 数报给 LiteLLM | DB `LiteLLM_SpendLogs` 的 `prompt_tokens` / `cached_tokens` |
| (2) LiteLLM → 客户端 | HTTP 响应体里有没有 `usage` 字段 | 响应 body / SSE 末帧 |

**同事说"看不到 token 数"时，99% 说的是 (2)。** 拿 SpendLogs 的查询结果
去回答 (2) 是答非所问，反之亦然。本 skill 全程只处理 (2)。

## 第 1 步：跑探针，四档一起打

`scripts/litellm-usage-probe.py`（只读，不碰任何配置）。

```bash
export LITELLM_KEY=sk-...
scripts/litellm-usage-probe.py --selftest            # 先验尺子，不打真实请求
scripts/litellm-usage-probe.py \
  --base https://your-proxy.example.com \
  --model <该 proxy 上真实存在的对外名>
```

它打四档，因为「没有 usage」这句话在四档下含义完全不同：

| 用例 | 请求形状 | 应然 | 没有 usage 意味着 |
|---|---|---|---|
| A | 非流式 | **必须有** | 真 bug |
| B | 流式 + `include_usage:true` | **必须有** | 真 bug |
| C | 流式 + 不带 `stream_options` | 没有 | **协议正确**，不是故障 |
| D | 流式 + `include_usage:false` | 没有 | 协议正确 |

只发一发请求你分不清自己在哪一档，于是要么把 C 误判成 bug，要么把真 bug
当成"客户端没传参数"糊弄过去。

**判据只认数值不认字段存在**：LiteLLM 拿不到上游 usage 时会回
`prompt_tokens=0` 的空壳。脚本把它单列为 `ZERO`，**不算 OK** —— 那种形状
对计费和对上下文窗口显示来说和没有 usage 完全一样。

### 分岔

- **A 或 B 缺** → 真 bug，开关救不了你。往上游查：换同一 proxy 上别的
  模型跑一遍做对照，分清是「这个 deployment/provider 吞 usage」还是
  「整个 proxy 坏了」。
- **只有 C/D 缺** → 协议正确。想让所有客户端无条件拿到 usage，进第 2 步。

## 第 2 步：服务端打开开关

在 LiteLLM Proxy 的 `config.yaml` 里，**`general_settings:` 下面**：

```yaml
general_settings:
  always_include_stream_usage: true
```

LiteLLM 会在每一次流式上游调用上自动注入 `stream_options.include_usage=true`。

三个位置陷阱：

- 🔴 **必须在 `general_settings:` 下，不是 `litellm_settings:`**。放错地方
  不报错，静默无效 —— 形状和"根本没配"一模一样。
- 🔴 **LiteLLM 在 `function_setup` + `pre_call_hook` 之前应用这个 flag**
  ⇒ 想用自定义 CustomLogger 的 pre-call hook 去注入 `stream_options`
  是**改不动的**（见 skill `litellm-hook-dev`）。这是唯一可靠的注入点。
- ⚠️ 本仓 198 是 **YAML-as-source-of-truth**：改 ConfigMap 后走
  `kubectl set image` / `patch` + `rollout status`，**禁 `kubectl apply`**
  （见 skill `litellm-ops`）。自建的话按你们自己的重启方式来。

改完必须让进程重新加载配置（重启 / 滚动更新）。

## 第 3 步：验收（这一步不许省）

```bash
scripts/litellm-usage-probe.py --base ... --model ... --mode after
```

**验收判据只有一条：用例 C 从 MISSING 变成 OK。**

下面这三件事**都不是**生效的证据，一件都不算：

- 配置文件里有那一行 —— 可能放错了 section，可能挂载的不是这份
- pod 重启过 / rollout 完成了 —— 证明进程起来了，不证明读到了这份配置
- 日志没报错 —— 放错 section 本来就不报错

验收不过时按顺序核：section 位置 → 进程是否真的重载 → **容器内实际文件内容**
（`kubectl exec ... cat /app/config.yaml`，不是仓库里的文件、不是 ConfigMap 的 YAML）。

## 已实测的事实（2026-09-21，打 198）

在一台 `always_include_stream_usage: true` 已生效的 proxy 上，
`deepseek-v4-flash` 四档实测：

```
A 非流式                    -> OK       prompt=33 completion=16
B 流式 + include_usage:true -> OK       prompt=33 completion=16
C 流式 + 不带 stream_options -> OK       prompt=33 completion=16   <== 开关生效的证据
D 流式 + include_usage:false -> MISSING  响应体里没有 usage 字段
```

- **D 仍然 MISSING** ⇒ 这个开关**不覆盖客户端显式写的 `false`**。
- 这条同时是 C 的**证伪对照**：如果 C 的 usage 来自"上游无条件返回"，
  那 D 也该有。D 没有 ⇒ C 的 usage 确实是注入来的。
  （所以别拿单独一个 C=OK 就下结论，要有 D 做对照。）

## 相关

- `scripts/litellm-usage-probe.py` —— 通用探针（任意 proxy）
- `scripts/aliyun-grok46-usage-probe.py` —— 阿里云↔198 两跳专用版
- `docs/aliyun-grok46-usage-verification.md` —— 2026-09-19 那次的完整取证
- skill `litellm-hook-dev` —— 为什么 pre-call hook 改不了 streaming 注入
- skill `litellm-ops` —— 198 改配置的部署纪律（禁 apply）

## 跨集群纪律

2026-09-19 吃过一次：198 有这个键、阿里云没有，**同名模型、同版本镜像
（都是 v1.90.2），行为相反**。初稿因为只测了一跳就外推，写出了错误结论。

⇒ 「这套 proxy 有没有 X」**每一套都要各自实测**，任何一套的结论都不能外推。
