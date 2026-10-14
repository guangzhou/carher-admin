---
name: cli-anything-sub2api
description: >-
  用 cli-anything-sub2api 操作 198 sub2api(litellm-dev,NodePort :31880)的 admin API:
  看账号/分组/用户/并发/审计、清 park、调 schedulable、充值、dry-run 预演写操作。
  入口 `scripts/sub2api-cli.sh`(自动取密码换 JWT,默认 --json)。
  Use when 用户说"查 sub2api 账号/看 grok 池子状态/sub2api 审计谁动的/给 sub2api 充值/
  清 sub2api 账号错误/sub2api admin API 怎么调/sub2api 现在什么版本"。grok 加号/reauth/rescue
  走 `sub2api-grok-ops`,并发闸诊断走 `sub2api-concurrency-gate`,本 skill 只管通用 admin 读写。
---

# cli-anything-sub2api

## 版本现状(2026-10-13 实测)

| | 版本 | 怎么量 |
|---|---|---|
| **198 线上** | **v0.2.16**(`127.0.0.1:5000/sub2api:20261013-v0.2.16`,10-13 23:26 切) | `$S versions`(READY=1 那行)/ `$S status` 的 `system.version` |
| 上游最新 | v0.2.16(10-14 现量,198 已同版) | `$S versions` 末段 |
| 源码 clone | `~/codes/sub2api` = origin/main(v0.2.16),全 tag 已拉 | |

198 升级史(rs 镜像 tag):v0.2.7(09-20) → v0.2.8(09-24) → v0.2.11(10-01) → v0.2.13(10-02) → v0.2.15(10-12) → v0.2.16(10-13)。
备份/探针在 198 `/root/sub2api-backup/`(10-12 那轮:dump + `probe-pre/post.json` 全 ok,8 条腿)。
⚠️ **别信记忆/skill 里写的"当前线上版本"**(10-13 发现停在 v0.2.11 十二天、漏了两次升级),每次现量:

```bash
scripts/sub2api-cli.sh versions   # 198 rs 镜像历史(READY=1 那行=线上)+ health + 上游最新 tag
```

**读源码对 198 行为要看部署那个 tag**:`git -C ~/codes/sub2api show v0.2.16:<path>`,别看工作区。
本地到 GitHub **时通时不通**(10-13 同一天通过又断;断时 `git fetch` 静默挂几分钟,`versions` 限 20s)
⇒ 不通走 188:`ssh cltx@10.68.13.188 'curl -sL https://raw.githubusercontent.com/Wei-Shaw/sub2api/<tag>/<path>'`。

## 工具

- **CLI 真身**:`tools/cli-anything-sub2api/`(CLI-Anything 方法生成;08-19 生成于 `~/codes/sub2api/agent-harness`
  未跟踪目录,10-13 迁入本仓,补了 `users balance` / `audit-logs list`)。
- **安装**:`scripts/sub2api-cli.sh install`(uv tool editable,指向仓内;改代码即生效)。
- **包装**:`scripts/sub2api-cli.sh <子命令…>` = ssh 198 `sudo -n` 读 `litellm-dev/sub2api-secrets` 的
  `ADMIN_PASSWORD` → `POST /api/v1/auth/login` 换 JWT(24h)→ `cli-anything-sub2api --target 198 --json …`。
  密码不落盘不进 argv;每跑一次 `audit_logs` 多一行 `auth.login`。
  已设 `SUB2API_JWT`/`SUB2API_ADMIN_API_KEY` 跳过登录;`S2A_HUMAN=1` 出人读格式。
- 兼容性:harness 用到的 admin 路由 v0.2.15 / v0.2.16 全在;**v0.2.15→v0.2.16 admin 路由零变化**,
  只有 group 多了可选 `i18n` 字段 ⇒ 198 升 v0.2.16 后 CLI 不用改。
- cli-anything 本体 = Codex skill `~/.codex/skills/cli-anything`(不是 PATH 命令);
  上游 `HKUDS/CLI-Anything` 10-13 对比:HARNESS.md / repl_skin / 模板一致,只有 `skill_generator.py` 上游更新(正则→AST)。
  生成器产出的 SKILL.md 是通用模板(含 project/undo 等不适用段落),**本 skill 手写,不用它覆盖**。

## 198 升级之后要做的(否则下一个会话读错版本)

1. `$S versions` 确认 READY=1 那行是新 tag;`$S status` 自报一致。
2. 对 harness 路由:`git -C ~/codes/sub2api diff <旧tag> <新tag> -- backend/internal/server/routes/admin.go`
   ——有删/改名的路由就改 `tools/cli-anything-sub2api` 并补测试;只新增可不动。
3. 改「版本现状」表、本 skill 里所有 `show v0.2.15:` 的 tag;同步改 `sub2api-grok-ops` §G 的「当前线上」、
   记忆 `project_sub2api_198_upgrade_v023_2026_09_08`(description + 末节)和 `topic_grok_index` 首节。
4. 「写接口会骗人」表每一行都标了源码出处,升级跨了相关文件就按新 tag 复核一遍。

## 常用命令

```bash
S=scripts/sub2api-cli.sh
$S versions                                 # 现量版本(不登录)
$S status                                   # 阳性对照:auth_scheme=bearer-jwt + system.version
$S accounts list --platform grok            # 默认 lite、page_size 200;token 被服务端掩成空串
$S accounts get 42
$S accounts today-stats 42
$S api get /admin/accounts/42/temp-unschedulable   # park 状态 {"active":false}
$S groups list
$S users get 1                              # balance / concurrency(user 级并发闸)
$S ops concurrency                          # account/group/platform 三层 in_use/max
$S audit-logs list --page-size 50           # 谁在什么时候打了哪个 admin 路由
$S dashboard stats

# 写操作:先 --dry-run 看 payload,再去掉
$S --dry-run accounts action 42 clear-error
$S --dry-run users balance 1 add 100000 --notes "topup"
$S --dry-run api post /admin/accounts/bulk-update --data '{"account_ids":[42],"schedulable":true}'
```

`api` 逃生口:`api {get|post|put|patch|delete} /admin/... [--data JSON|--file F] [--param k=v]`。
`/accounts/data`、`/proxies/data`、`/settings/admin-api-key` 强制 `--output-file`,不打印凭据。

## grok 422 一键分诊(10-14)

面板 `grok-4.7` `/v1/responses` 502(上游 422)、5 腿全 422 ⇒ 先看是不是同 IP 每 ~30s 一簇 6 条(=LiteLLM 重试放大),
再按 skill `sub2api-grok-ops` §⚡ 的 422 段拿 A/B 探针 `scripts/grok-422/probe_agent_message.py` 定 item type。
sub2api v0.2.16 的 grok responses 路径**不存 422 响应体**(`openai_gateway_grok.go` 无 `Detail:`),面板只会显示「422」。

## 写接口会骗人(收尾回读只认下表的尺子)

| 操作 | 坑 | 正确做法 |
|---|---|---|
| 放腿 | `accounts update {"schedulable":true}` 返 200 **静默丢弃**(v0.2.15 `UpdateAccountRequest` 无此字段) | `accounts set-schedulable 42 true` 或 `bulk-update {"account_ids":[..],"schedulable":true}` |
| 清 park | `clear-rate-limit` / `recover-state` / `reset-quota` 返 200 一位没动 | 只有 `accounts action 42 clear-error` 清 `temp_unschedulable_until` |
| 充值 | `users update` 塞 balance 返 200 被丢弃(v0.2.15 `admin_user.go` `UpdateUser` 无 balance 分支) | `users balance <id> set\|add\|subtract <n>`(专用路由,`gt=0`),写完 `users get` 回读 |
| 删号 | `accounts delete` 是**软删**(`SoftDeleteMixin`),API 回 404 但行还在 | 查库必带 `deleted_at IS NULL`;回滚=清 `deleted_at` |
| 换 token | GET 回来 `access_token`/`refresh_token` 全是空串 ⇒ 比 token 恒"没变" | 看 `credentials.expires_at` / `_token_version` |
| 判腿在不在服务 | `schedulable`/`status`/quota 端点都不算数 | 只认 postgres `usage_logs.account_id`(max(id) 水位线),见 `sub2api-grok-ops` |

**有第二只手**:池子还有别人/别的会话在动,所有人共用 `admin@sub2api.local`(本脚本也是)。
放完腿又被关,先 `audit-logs list` 按时间排查,报给用户时点名这件事。

## grok 403 park 可按账号配(仅读源码,198 未实测)

`v0.2.15:backend/internal/service/openai_gateway_grok.go` `handleGrokAccountUpstreamError` 默认:
403→30min、401→10min、402→30min、5xx→2min。但 403 先走 `applyGrokForbiddenPolicy`
(`grok_upstream_errors.go`,自 v0.1.163):账号 `credentials` 里
`temp_unschedulable_enabled=true` + `temp_unschedulable_rules=[{error_code:403,keywords:[..],duration_minutes:N}]`
命中关键词 ⇒ 用规则替代默认 30min。

- **v0.2.15**:规则命中时若请求模型已知且非 401 ⇒ **只冷却该 (账号,模型)**,不停整号;grok 入口是否带模型进来未核。
- **v0.2.16**(10-13 已部署)规则多一个 `scope: account|model`;不写 = 旧行为(模型已知且非 401 走模型级)。
- 10-13 查 198 的 55 个 grok 号:**全没配**。`pool_mode` 只对 apikey 号生效,grok oauth 号用不上。
- ⚠️ 写 credentials 前先确认 PUT 不会把 GET 时被掩成空串的 token 写回去 —— 没验证前别动。

## 别用它做的事

- grok 加号 / reauth / rescue:`scripts/grok-onboard/sub2api-grok-onboard.py`(skill `sub2api-grok-ops`),
  已焊好上面这些尺子,别拿 CLI 手搓。
- 并发闸(user vs account):skill `sub2api-concurrency-gate`。
- 升级镜像:`scripts/grok-onboard/sub2api-upgrade.py`(198 `/Data/sub2api-ops/` 那份与仓内 sha256 逐字相等,10-13 核过)。

## 改 harness

```bash
cd tools/cli-anything-sub2api
uv run --with pytest --with click --with prompt-toolkit python -m pytest cli_anything/sub2api/tests -q
```

E2E 用 `tests/conftest.py` 的假后端;加命令就同时加假路由+测试。改完不需重装(editable)。
包内 `cli_anything/sub2api/skills/SKILL.md` 是随包分发的英文版(给装了 CLI 的别的 agent 用),
加命令时两份都改;⛔ 别用上游 `skill_generator.py` 重生成覆盖它 —— 10-13 试过,产出的是通用模板
(`project new` / undo-redo 等 sub2api 没有的命令),会把正确内容冲掉。
