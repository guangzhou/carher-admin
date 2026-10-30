---
name: litellm-198-sec-redact
description: >-
  198 LiteLLM 网信出口脱敏插件 sec_redact 的运维与改动：现状/开关/回滚、改规则走 单测→全链T0→gray→prod、
  影子命中报告、误报判断、带外装/换 callback 后回填升级快照。用户说「脱敏」「网信」「敏感信息」「sec_redact」
  「密钥被替换」「[密钥凭证脱敏]」「手机号被打码」「加/改/关脱敏规则」时用。
---

# sec_redact（198 出口脱敏）

规范：飞书 `WHYUdeTnCo0TZQxWCqHcFzOTnBe`；方案 + 影子报告：飞书 `SXegdiYvXoEdXnxClUWcpQ02nfi`（第 8 节）。
代码 `k8s/litellm-callbacks/sec_redact.py`，测试同目录 `tests/`。

## 现状（10-29 15:00 起）

| 车道 | 装没装 | 模式 |
|---|---|---|
| prod `litellm-proxy`（30402） | ✅ | **替换**（无 SHADOW env） |
| gray `litellm-proxy-gray`（30405） | ✅ | 替换 |
| guarded-old（30406，保险丝） | ❌ | 不装（升级出事才用，不接流量） |

- **全部规则都是替换，不阻断**（用户 10-29 定）。请求方向发厂商前替换；响应方向回员工前替换，后文照放。
- agent key（别名前缀 `cursor-` `claude-code-` `codex-`）只跑 `3-*` 凭证规则，IP/手机号原样（模型会把占位符写进代码）。
- 现量：`kubectl exec <pod> -- sh -c 'sha256sum /app/sec_redact.py; printenv SEC_REDACT_SHADOW'` + 日志 `grep -c 'action=replace'`。
  pod 日志滚得快，看不到 `installed` 行 ≠ 没装。

## 开关 / 回滚（都是 `kubectl set env` 滚动，零中断）

| 要什么 | 做法 |
|---|---|
| 退回只记录 | `set env deploy/<d> -c litellm SEC_REDACT_SHADOW=1` |
| 整个关掉 | `SEC_REDACT_DISABLED=1` |
| 代码回退 | `scripts/litellm-198-lane-callback-replace.py rollback <deploy> /root/cb-add-backup/<deploy>.<ts>.json` |

改完 env **必须回填升级快照**（见下「回填」），否则下次 helm 会把 env 带回/带走。

## 改规则 / 改代码：固定四步，一步红就停

1. **单测** `python3 -m pytest k8s/litellm-callbacks/tests/test_sec_redact.py`。新行为配一条「去掉修复必红」的测试（先回退修复跑一次确认红）。
2. **全链 T0**（198 `/home/cltx/t0-sec-redact`，容器 `t0sec-*`：真 litellm 镜像 + gray 全部 CM + mock 上游）：
   `cp sec_redact.py mounts/` → `bash t0_matrix.sh`。判据：ON 全 PASS、SHADOW 全 PASS、**DISABLED 必须大量 FAIL（阳性对照）**、
   SpendLogs ON=`body=redacted` / DISABLED=`body=RAW`、proxy 日志 0 原文 0 Traceback。
   环境没了就重搭：`scripts/t0/`（`build_t0.py <gray-deploy.json> sec_redact.py` 从 gray Deployment 照抄挂载 →
   `compose2run.py` 转 docker run；`mock_upstream.py` 支持 `ECHO_KEY/PHONE/IP` 和 `/midfail/`；`t0_matrix.sh` 三模式矩阵），
   连同 `k8s/litellm-callbacks/tests/t0_sec_redact.py` 拷到 198 那个目录。
3. **gray**（split=0 时零用户流量）：`gray_guard snapshot` → `lane-callback-replace.py replace litellm-proxy-gray <file>` →
   `gray_guard verify-replace` → 真上游 `LANE_UNDER_TEST=gray python3 tests/live_sec_redact_gray.py`（三线型 33 项）。
4. **prod** 同 3（`LANE_UNDER_TEST=prod`）。切后量：`scripts/fail-vs-hits.py`（改里面的切换时间 `S`）—— **被替换过的请求失败率** vs 没命中的；
   前后 30 分钟按错误类型拆（权限 403 / 鉴权 / 上游）再下结论。

守卫脚本：`scripts/litellm-198-gray-change-guard.py`（snapshot / verify-pre / verify-post / verify-replace / verify-env，
断言只变了声明的那一项）。

## 回填升级快照（每次带外改完都做）

```bash
R=/root/l3/up1064-20261025T164519Z   # 当前升级 run；以后换成新 run
bash  litellm-gray-rollout/scripts/run/render-lane-check.sh prod $R          # 先看 DIFF 清单
python3 litellm-gray-rollout/scripts/run/snap-backfill-from-live.py check prod --run-dir $R --file sec_redact.py --env SEC_REDACT_SHADOW
python3 litellm-gray-rollout/scripts/run/snap-backfill-from-live.py apply prod ...   # 备份 <inputs>/backfill-<ts>-live
bash  litellm-gray-rollout/scripts/run/render-lane-check.sh prod $R          # 必须 RENDER == LIVE
```

- **只点名自己改的东西**。DIFF 里别的会话的改动（10-30 prod 的 `video_id_alias_auth` / `error_sanitize` / config）不替它们回填，告诉用户。
- 回填别人的改动前先证明三车道 live 一致（脚本对 --file/--config 自动比 prod vs gray）。

## 影子命中报告 / 误报判断

`scripts/shadow-report.py <since RFC3339> [--json out]`（198 sudo；按 方向×规则×家族、不同取值数、掩码样本、每请求命中数）。
- **先剔探针**：master key（`-`）、`*-shadowreg-*`、`*-secprobe-*`、`*-t0sec-*`。10-29 手机号 167 次命中全是探针，真实 0。
- 看「不同取值」不看「命中次数」：agent 每轮重发整段历史，一个 JWT 被 112 个请求命中 167 次。
- `3-赋值型密钥` 是启发式（≥16 位、字符杂、非占位符），日志里只有 sha/掩码，**判不了误报**，只能找 key 主人抽查。

## 坑（都踩过）

- **callback 里禁 `@dataclass`**：litellm 加载 callback 不登记 `sys.modules` ⇒ import 期崩、proxy 起不来。单测 loader 也别登记，否则盖住。
- Responses 流整段原文副本到处都是：`response.completed` 顶层 `text`、`response.choices[0].message.content`、
  `*.done` 的 `text`/`part.text`/`item.content[].text`、最后一个 delta 的 `text` ⇒ done 类事件**整树扫**；尾巴事件用最近的 delta 作模板，不复制 done 事件。
- 流式尾巴 `_HOLDBACK=160`：私钥远超这个长度 ⇒ 私钥规则在无 END 时**连同已到的 base64 行一起命中**，切点退到 BEGIN 前。
- 审计日志每段最多 20 行 + 1 行 `hit-summary`（461 行/请求时 +100ms，封顶后 +10ms）。
- 插件日志 `req=` 是 **litellm_call_id**；SpendLogs `request_id` 常是上游 `msg_…` ⇒ 对账用 `metadata->>'litellm_call_id'`（第一次对出交集 0 是量具错）。
- deepseek-v4.1-flash 推理先吃 token：探针 `max_tokens` 至少 1500，否则正文为空被误判。
- SpendLogs 落库体要脱敏：上游在 pre_call_hook 后自己会刷 `refresh_proxy_server_request_body_snapshot`（函数名带 `_snapshot`）。

## 已知不做（方案里写明）

词库类 2.1/5.2/7.2（等网信给词库）、组合类、语义类；流式 tool_calls 参数、图片/文件、base64 内容。

相关：[[litellm-198-version-upgrade]]（带外改动 / 快照）、[[litellm-hook-dev]]（hook 通用坑）、[[litellm-gray-key-rollout]]。
