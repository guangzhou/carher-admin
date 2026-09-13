# v1.100.1 目标镜像补丁

这个目录装的是**实际烘出灰度目标镜像的那两个脚本**，加上能复现它的 Dockerfile。
上一级的 `patches/sse-lifecycle-gapfiller-and-bare-handler.v1.90.2.diff` 是**线上现役
v1.90.2 那一版**的记录，锚点形状不同，不能拿来打 v1.100.1。

## 两条补丁分别修什么

| 文件 | 补丁 | 修的问题 |
|---|---|---|
| `litellm_core_utils/exception_mapping_utils.py` | capacity-ratelimit | 上游偶发 400 + `Selected model is at capacity`，vanilla 落成 `BadRequestError`，router 不 cooldown 不 fallback 不重试，直接穿透到用户。升格成 `RateLimitError`。 |
| `responses/streaming_iterator.py` | sse-lifecycle（含 bare-handler） | 上游只吐 `output_text.delta` + `response.completed`，缺 lifecycle 包装事件；以及 acct pod 吐裸 response 对象（有 `object`/`status`、没有 `type`）。Codex CLI 认不出流结束，报 `stream closed before response.completed` 然后 Reconnecting 1/5…5/5。 |

## v1.90.2 → v1.100.1 的锚点漂移（这是补丁必须重写的原因）

- `exception_mapping_utils.py`：v1.100.1 把整条 OpenAI 分支链抽成模块级 helper，缩进
  从 16 空格变成 4 空格，`elif` 条件压回一行。旧锚点在新 base 上匹配 **0 次**——门禁
  按设计报红，不是脚本坏了。同时 `exception_mapping_worked` 这个局部变量在新 helper
  里已经不存在，补丁块里必须去掉，否则 `NameError`。
- `streaming_iterator.py`：顶部 import 形状每版都在变（v1.100.1 把 `Optional/Dict/List`
  换成 `X | None`，`ResponsesAPIStreamEvents` 改成惰性 `_get_openai_response_types()`）。
  所以补丁自带的 import 独立成块放在模块中部，不去动文件顶部那几行。

## 上游修没修（2026-09-13 查证）

`Selected model is at capacity` 在 LiteLLM 全仓库 **0 条** issue/PR。v1.100.1 的
`ExceptionCheckers.is_error_str_rate_limit` 只认三种形状（被 status_code 门住的裸 429、
`rate[\s_-]*limit`、Mistral 整句 `service tier capacity exceeded`），这句一条都不匹配。
想扩短语的 PR #38706 仍 open。

⚠️ 不要改成「靠 body 里的裸数字判限流」——那正是上游 #36705 修掉的坑。

## 复现与验证

```
docker build -f Dockerfile -t 127.0.0.1:5000/litellm-carher:vanilla-v1.100.1.capacity.sse-fix-bare-<ts> .
```

判据不是 tag 名字，是**两个镜像 /app 全量 md5 对比**：必须只有上表那两个文件不同，
文件数不变（19524）。做法（不需要 docker 权限，198 上没有）：起一个 pod 跑
`cd /app && find . -type f -print0 | sort -z | xargs -0 md5sum`，两边 `join` 对比。
pod 必须带 `litellm.carher.io/role: version-test` 标签，否则 NetworkPolicy 不给 DNS。

已验证的一对：

```
base    vanilla-v1.100.1                                       sha256:d85c0b36e2fdd64719e7fc2692431cd5aee0814925e96c2892d1491b13189c23
target  vanilla-v1.100.1.capacity.sse-fix-bare-20260913-163142 sha256:abdf613339ad36c21fe8b03e53792795fd1f9969ac1d4f390e37735cdb52325e
```

差异面：`exception_mapping_utils.py`（+15/-0）、`streaming_iterator.py`（+394/-8），
无文件增删。老补丁加进去的每一个符号在新补丁产物里都还在（逐符号 `comm` 对比，空集）。
唯一的内容差异是一行注释里的 issue 引用 `(issue #20975)` 掉了，无行为影响。
