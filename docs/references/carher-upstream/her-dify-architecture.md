# Snapshot Note

Copied into `carher-admin` from `../CarHer/docs/her/her-dify-architecture.md` during the her-266 H75/Dify session artifact cleanup. Treat this as a read-only reference snapshot; refresh intentionally from upstream instead of editing `../CarHer` for rollout/runbook work.

# Her + Dify 架构设计文档

**版本**: v2.0 (full-fleet validated)
**日期**: 2026-05-26
**实证基础**: 199 + 200 + 13 + 75 + 14 全链 Group J PASS, OpenClaw + Hermes 两个引擎都通过
**runtime ref**: `1803fe144e46` (dev HEAD 2026-05-25 ~05:00 UTC, codex-only fallbacks=[] enforced)
**image digests**:
- 199, 200: `ghcr.io/buyitsydney/carher-runtime@sha256:ad87a5b974b247f41400b554f72f0381e57f245586d419d8f7fc7479f202cb0f`
- 13: `ghcr.io/buyitsydney/carher-runtime@sha256:43cf30dd51be285992ddc39a768438b6fdeba044443c47f1128596c64f3bab44`
- 75: `ghcr.io/buyitsydney/carher-runtime@sha256:78a73b1d6750c3a420ea9b8500522197ce8da193d571d4aafc129a973cf55374`
- 14: `ghcr.io/buyitsydney/carher-runtime@sha256:5a9480610787f24351526d94e6e0113d8a7657c49fd165b200080a32477f76f4`

---

## 1. 目标

让 1000 个独立 her bot 用同一个 dify 后台,各自有 workspace 物理隔离,user 在群里发"用 dify 给我自建工作流"她真自建 workflow 跑通 — zero 手工配置,走正规 CICD 即可。

5 硬约束:

1. 1 个共享 dify 后台 (S2 10.68.13.187)
2. 1000 个独立 her bot 复用
3. 数据隔离 (per-bot tenant_id 物理分区, per-user account 跨 bot 不复用)
4. 账号隔离 (per-workspace api_key / lifecycle_token / admin 不共享)
5. 绝对开源免费 (dify Community Apache 2.0)

---

## 2. 架构总览

```
┌────────────────────────────────────────────────────────────────┐
│  1000 飞书 user (open_id × 1000)                                │
└────────────────────────────────────────────────────────────────┘
                              │ @bot in 群 / DM /dify
                              ▼
┌────────────────────────────────────────────────────────────────┐
│  1000 个 her bot 容器 (S1/S2/S3, hermestest-N)                  │
│  ├─ engine: openclaw 或 hermes (dual-image)                     │
│  ├─ skills baked in image:                                      │
│  │    /app/skills/her-workflow-dify-creator/SKILL.md            │
│  │    /app/skills/her-workflow-dify-mcp/SKILL.md                │
│  ├─ helper binaries:                                            │
│  │    /data/.openclaw/local/bin/her-workflow-dify-creator       │
│  │    /data/.openclaw/local/bin/her-workflow-dify-mcp           │
│  └─ /data/.openclaw/workflow/dify-config.json                   │
│       { workspace_id, dify_base_url, api_key,                   │
│         lifecycle_base_url, lifecycle_token,                    │
│         codex_model, codex_base_url }                           │
└────────────────────────────────────────────────────────────────┘
                              │ HTTP
                              ▼
┌────────────────────────────────────────────────────────────────┐
│  S2 (10.68.13.187, /Data 622G)                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ dify-bootstrap (python:3.12-slim, restart=unless-stopped)│  │
│  │ http://10.68.13.187:5688                                  │  │
│  │   POST /v1/bootstrap/carher-bot                           │  │
│  │   {GET,POST,PATCH,DELETE} /v1/lifecycle/<bot_id>/<path>   │  │
│  │   GET  /v1/lifecycle/<bot>/health   (boundary-safe)       │  │
│  │   POST /v1/user-login/<bot>/issue   (per-user dify acct)  │  │
│  │   GET  /v1/exchange?t=<nonce>   (first-redeemer cookie)   │  │
│  │   GET  /auto?t=<nonce>          (browser autologin)       │  │
│  └──────────────────────────────────────────────────────────┘  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ dify v1.4.2 docker compose                                │  │
│  │ http://10.68.13.187:5680  (nginx → api 5001, web 3000)    │  │
│  │   db (postgres) + redis + sandbox + plugin_daemon +       │  │
│  │   weaviate + ssrf_proxy + worker + api + web + nginx      │  │
│  │   持久卷 /Data/dify/dify/volumes                          │  │
│  └──────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
                              │ HTTPS (LLM nodes only)
                              ▼
┌────────────────────────────────────────────────────────────────┐
│  https://litellm.carher.net/v1                                  │
│    Codex / chatgpt-gpt-5.5 (OpenAI-compatible)                  │
│    key: CARHER_PROD_KEY (env, redacted)                         │
│    NO fallback to openrouter (codex-only strict mode PR #61)    │
└────────────────────────────────────────────────────────────────┘
```

---

## 3. Bootstrap 流程 (per-bot 自动)

容器启动时 `dual-entrypoint.sh` 内 `bootstrap_dify_workflow_config_if_needed` hook 自动调:

```
POST http://10.68.13.187:5688/v1/bootstrap/carher-bot
Authorization: Bearer ${CARHER_DIFY_BOOTSTRAP_TOKEN}
body: { "bot_id": "<HERMESTEST_CARHER_ID>" }
```

S2 bootstrap endpoint 处理流程:

```
1. 检查 /Data/dify-bootstrap/tenants/<bot_id>.json 是否存在
   ├─ 存在  → 返回缓存配置 (idempotent), 任意重启 / redeploy 都走这条
   └─ 不存在 → 进入首次 bootstrap
2. master admin login 拿 console access_token
3. docker exec docker-api-1 python3 -c "
       TenantService.create_tenant(name=her-<bot_id>-workspace, is_from_dashboard=True)
       TenantService.create_tenant_member(tenant, master_account, role='owner')
4. 切换 master account 到这个 tenant
5. 安装 openai_api_compatible plugin (/Data/dify-bootstrap/oaic.difypkg)
6. 注册 carher-pro/chatgpt-gpt-5.5 model + LITELLM_BASE + CARHER_PROD_KEY
7. 生成 placeholder app + api_key
8. 生成 lifecycle_token = "lct_" + token_urlsafe(28)
9. 持久化 /Data/dify-bootstrap/tenants/<bot_id>.json
10. 返回 { workspace_id, dify_base_url, api_key, lifecycle_base_url,
          lifecycle_token, codex_model, codex_base_url }
```

容器侧 `dify-bootstrap-init` 写入 `/data/.openclaw/workflow/dify-config.json` (`0600 hermes:hermes`),启动完成。

---

## 4. Lifecycle Proxy (per-bot 隔离访问)

bot 端 helper (`her-workflow-dify-creator`) 不直连 dify console API,而是经 S2 lifecycle proxy:

```
ANY /v1/lifecycle/<bot_id>/<dify-console-path>
Authorization: Bearer <per-bot lifecycle_token>
```

S2 proxy 行为:
1. 验证 `lifecycle_token` 在 `tenants/<bot_id>.json` 中 — 跨 bot token 一律 401
2. 校验 `subpath` 在 allowlist 内 (`apps/*`, `workspaces/current/models/*`, `workspaces/current/model-providers`, `apps/imports`, `health` 短路) — 越权 403
3. 用 master_token 切换到 `<bot_id>` 的 tenant (`_master_switch_lock` 序列化, 避免 thread-interleave 数据泄漏)
4. 用 master_token 透传请求到 dify console

**核心隔离保证**: bot 端永远拿不到 dify master credentials,且无法读取/写入其他 bot 的 workspace。J9 audit 实证:

```
14 token → /v1/lifecycle/carher-14/health = 200 (workspace 638ef5e1)
14 token → /v1/lifecycle/carher-13/health = 401 unauthorized
14 token → /v1/lifecycle/carher-75/health = 401 unauthorized
```

---

## 5. Per-User Dify Account (PR #47 加固)

`/dify` 命令的人端浏览器登录走独立通道:

```
POST /v1/user-login/<bot>/issue
Authorization: Bearer <lifecycle_token>
body: { "requester_email", "requester_open_id", "requester_name" }
→ { login_url, expires_in: 900, email }
```

Account email 派生:

```
bot-<bot_id>-<safe-local>-<sha256(normalized_email)[:8]>@her.local
```

- 跨 bot 同人不会复用 — 每个 bot 独立 dify account → 独立 current_tenant
- 跨 domain 同 local-part 不会撞 — `alice@orgA` vs `alice@orgB` 在同 bot 也是两个 account

URL 复用规则:
- 15 分钟内首次浏览器打开后 `Set-Cookie: dlbind_<hash>; HttpOnly; Path=/v1/exchange`
- 同浏览器内可重复 redeem (reload `/auto?t=<nonce>` OK)
- 转发到其他浏览器 → 410 `redeemed_from_another_browser`
- 15 分钟后 → 410 `nonce_expired`

---

## 6. CICD 全链流程

```
            dev branch (carher-runtime repo)
                                            │
                                            ▼
                  Docker Release workflow (push or manual workflow_dispatch)
                                            │
                                            ▼
                  ghcr.io/buyitsydney/carher-runtime@sha256:...
                                            │
                                            ▼
                  Deploy workflow (target=s1-canary / s1-prod-13 / s3-prod-N)
                                            │
                                            ▼
                  bot 容器启动 → entrypoint → dify-bootstrap-init → cached config / fresh tenant
                                            │
                                            ▼
                  Group J stress (dify) + Groups A-I stress (her runtime)
```

任意新 bot (75/14/1001/...) 升级:

```bash
# 1. release for that target
gh workflow run "CarHer Runtime Docker Release" \
  --repo buyitsydney/carher-runtime --ref dev \
  -f target=s3-prod-N -f push_image=true

# 2. deploy with returned digest + artifact
gh workflow run "CarHer Runtime Deploy" \
  --repo buyitsydney/carher-runtime --ref dev \
  -f release_run_id=<id> \
  -f artifact_name=carher-runtime-release-bundle-<sha> \
  -f target=s3-prod-N \
  -f image_digest=ghcr.io/buyitsydney/carher-runtime@sha256:<digest> \
  -f dry_run=false
```

启动后自动:
1. `bootstrap_dify_workflow_config_if_needed` hook 调 S2 拿 per-bot 配置
2. `/data/.openclaw/workflow/dify-config.json` 落地
3. her bot 加载 `her-workflow-dify-creator` skill (`workflow / 工作流 / 流程自动化` 关键字命中)
4. user 群里发"用 dify 给我自建工作流" → 自动识别 + 真自建跑通

**zero 手工**。

---

## 7. Codex-only Strict Mode (PR #61)

每个 `deploy/bots/carher-<N>.{s1,s3}.json` manifest enforce:

```json5
{
  "codex_oauth": {
    "enabled": true,
    "route": "carher-pro",
    "provider_id": "carher-pro",
    "fallbacks": []        // 强制空, 无 openrouter 兜底
  },
  "dify": {                // PR #57 加入 (13/14/75 manifests)
    "enabled": true,
    "base_url": "http://10.68.13.187:5680",
    "bootstrap_url": "http://10.68.13.187:5688/v1/bootstrap/carher-bot",
    "bootstrap_token_env": "CARHER_DIFY_BOOTSTRAP_TOKEN",
    "model": "chatgpt-gpt-5.5",
    "codex_base_url": "https://litellm.carher.net/v1",
    "codex_key_env": "CARHER_PROD_KEY",
    "workspace_slug": "carher-<N>"
  }
}
```

`scripts/render-bot-runtime.py --write` 把 manifest 渲染到 `deploy/carher-<N>/compose.cicd-<N>.yaml` + `openclaw.runtime.json5`,两边 SHA 比对验证。运行时 `/data/.openclaw/openclaw.json` 同时 enforce `fallbacks=[]`。Embedded agent + dify workflow LLM node 都用 `chatgpt-gpt-5.5` 走 `https://litellm.carher.net/v1`。

---

## 8. 实证证据 (199 + 200 + 13 + 75 + 14 全链)

### 8.1 部署链时间线 (2026-05-25 → 26)

| Bot | Server | Image | Deploy Time | Group J |
|---|---|---|---|---|
| 199 | S1 (10.68.13.186) | `sha256:ad87a5b9` | 2026-05-25 ~09:46 UTC | OpenClaw + Hermes FULL PASS |
| 200 | S1 (10.68.13.186) | `sha256:ad87a5b9` | 2026-05-25 ~09:46 UTC | OpenClaw + Hermes FULL PASS |
| 13 | S1 (10.68.13.186) | `sha256:43cf30dd` | 2026-05-25 ~18:53 UTC | OpenClaw + Hermes FULL PASS |
| 75 | S3 (10.68.13.188) | `sha256:78a73b1d` | 2026-05-25 ~20:09 UTC | OpenClaw + Hermes FULL PASS |
| 14 | S3 (10.68.13.188) | `sha256:5a948061` | 2026-05-25 ~20:39 UTC | OpenClaw + Hermes FULL PASS |

每 bot 都自动 bootstrap 出独立 workspace, 全程 zero 手工配置。

### 8.2 Workspace 隔离

| Bot | workspace_id |
|---|---|
| 199 | `c8becea9-fcbd-4c2e-9d61-ef81964ea2e3` |
| 200 | `33fefc72-54a5-43c7-9522-7fc5e57bf186` |
| 13  | `ae45ed57-87c2-4ebe-82e5-2b4167ec975f` |
| 75  | `93f273d1-ef22-4b63-9bfd-f478d7f8efda` |
| 14  | `638ef5e1-31b7-4e58-b62f-c5368f716152` |

跨 bot lifecycle_token 测试矩阵 (J9): 对角线 200,off-diagonal 全 401。

### 8.3 Group J 矩阵证据 (每 bot 每引擎)

完整矩阵: 5 bots × 2 engines × 9 cases = **90 cases all PASS**。

代表 workflow_run 证据 (节选):

| bot/engine | WF1 (HN) | WF2 (Reddit) | WF3 (PyPI+GitHub) | J7 CRUD |
|---|---|---|---|---|
| 13 hermes | `676a5676` / run `f6eaf220` 11n 3LLM 64.3s | `898cf096` / `eeaad714` 7n 88s | `d43b867d` / `1f020b9e` 9n 47.5s | `676a5676` rerun `f448480b` |
| 13 openclaw | `35acf3b0` / `daf1935f` 11n 11API | `eecfe6b9` / `8552faaf` 7n 65s | `7b954e2c` / `f3434713` 9n 37.5s | `35acf3b0` rerun `61e34d5d` |
| 75 hermes | `c79cb631` / `3ad9b953` 16n 173s | `ff3ad66f` / `f01d4888` 8n 133s | `699345ef` / `286baeeb` 9n 169s | `c79cb631` rerun `afa4b255` |
| 75 openclaw | `d453bbc8` / `8f77f2a6` 9n 30.4s | `87b2d3c8` / `6c5d0669` 8n 113s | `2cd35a2d` / `6c6156b6` 9n | `d453bbc8` rerun `2670bbf3` |
| 14 hermes | `5eacedfe` / `e5fc5ed4` 18n 11API | `fa07505d` / `f2e8a38f` 7n | `77158488` / `4e670d91` 9n 16.5k tok | `5eacedfe` rerun `520e2a98` |
| 14 openclaw | `bf600abe` / `cfd1be16` 18n 17k tok | `651c7e33` / `d0582c2a` 7n 76s | `b5854a17` / `233f4e26` 9n 42.6s | `ec78531c` rerun `5cc716aa` |

每个 workflow ≥ 5 节点,≥ 3 LLM 节点,真公开 API 上下游 (HackerNews / Reddit / PyPI / GitHub / USAspending / WorldBank / SEC EDGAR / NHTSA), 所有 LLM 节点都走 `chatgpt-gpt-5.5` via `carher-pro`,无 openrouter fallback。

### 8.4 J3/J4/J5 cookie binding (PR #47 验证)

```
J3 first redeemer:   HTTP 200 + Set-Cookie: dlbind_<hash12>; HttpOnly; Path=/v1/exchange
J3 same browser ×2:  HTTP 200 (cookie valid, repeat redeem OK)
J4 second browser:   HTTP 410 redeemed_from_another_browser
J5 after 15 min:     HTTP 410 nonce_expired
```

5 bots × 2 engines 全部通过。

---

## 9. 过夜修复链 (PR #47 → #61)

完整 14-PR fix-forward chain (2026-05-25 overnight,所有 PR 已 merge):

| PR | 主题 | 触发原因 |
|---|---|---|
| #47 | S2: per-bot dify account + 15min reusable URL + first-redeemer cookie | per-bot 账号 + 浏览器绑定加固 |
| #48 | Runtime: 15-minute reusable Dify login cards | 卡片文案 + helper 一致 |
| #49 | S2: run Dify deploy workflow on CarHer runner | ubuntu-latest 不可用 |
| #50 | S2: fix Dify deploy compose invocation | compose 引用 bug |
| #51 | Runtime: dify-login-card.py open-id fallback when hermes-user has no_token | hermes user 无 lark-cli token |
| #52 | S2: keep lifecycle health probe local | allowlist 缺 `health` → 403 |
| #53 | S2: use buildx --metadata-file for digest | GHCR anonymous imagetools 401 |
| #54 | S2: noninteractive bounded docker pull | docker pull hang |
| #55 | S2: allow lifecycle model discovery paths | allowlist 缺 `models`, `model-types` |
| #56 | S2: lifecycle metadata + path normalization | 缺 `model-providers` + 防 path traversal |
| #57 | Deploy: enable Dify on carher-13/14/75 manifests | 13/75/14 manifest 缺 `dify` block |
| #58 | Deploy: inject CARHER_DIFY_BOOTSTRAP_TOKEN to deploy env | secret 不在 deploy workflow |
| #59 | Deploy: host-bind fallback when in-container backup fails | restart-loop 容器 docker exec 137 |
| #60 | Deploy: sudo install/tar for host-bind backup | data-home 权限不足 |
| #61 | Deploy: codex-only fallbacks=[] for 13/14/75 | manifest 残留 openrouter fallback |

每个 PR 都遵守 5-step TDD (reproduce → failing test → fix → all green → only then redeploy)。

---

## 10. 持久化布局 (S2)

```
/Data/dify/dify/                                  # dify v1.4.2 source + compose
  docker/docker-compose.yaml                       # 9 个 dify 容器 + dify-bootstrap (compose.override)
  docker/.env                                      # SECRET_KEY / DB_PASSWORD / URL ports
  volumes/                                         # postgres / redis / storage / private RSA keys
/Data/dify-bootstrap/                              # bootstrap state (0600 root)
  bootstrap.py                                     # Flask app on :5688 (now image-baked)
  oaic.difypkg                                     # openai_api_compatible plugin pkg
  master-admin.env                                 # superuser@dify.carher.local creds
  carher-codex.env                                 # CARHER_PROD_KEY + LITELLM_BASE
  bootstrap-token.env                              # CARHER_DIFY_BOOTSTRAP_TOKEN
  .password-key                                    # Fernet key for per-user pwd encryption
  tenants/
    carher-199.json  carher-200.json  carher-13.json  carher-75.json  carher-14.json
  users/                                           # per-bot per-user account records
    bot-carher-<N>-<safe-local>-<sha256[:8]>@her.local.json
  audit/
    YYYY-MM-DD.jsonl                               # bootstrap / login_issued / login_bound / login_exchanged
```

`docker compose down -v` 不影响 `/Data/dify-bootstrap/` (只删 compose-managed volumes)。
全停 docker → 重启 → 所有 bot 重 bootstrap-init → 拿到 cached config → zero downtime。

---

## 11. 1000-bot 推广步骤

对任意新 bot N:

1. 加 `deploy/bots/carher-N.s{1,3}.json` 含 `codex_oauth.fallbacks=[]` + `dify.enabled=true` block (复制 199 模板, 改 workspace_slug)
2. 加 `deploy/targets/sX-prod-N.json` 指向 host_class + container_name
3. 跑 `scripts/render-bot-runtime.py deploy/bots/carher-N.* --write` 渲染 compose + runtime config
4. PR 走 dev review → merge
5. 触发 Docker Release (target=sX-prod-N) → 拿 image_digest + artifact_name
6. 触发 Deploy (同 target) → 容器启动 → bootstrap-init → cached / fresh 配置
7. Group J stress (按 `.cursor/skills/her-feishu-e2e-test/SKILL.md`) → 都过即接入

---

## 12. References

- **runtime PRs**: [#39](https://github.com/buyitsydney/carher-runtime/pull/39) [#40](https://github.com/buyitsydney/carher-runtime/pull/40) [#41](https://github.com/buyitsydney/carher-runtime/pull/41) [#47-61](https://github.com/buyitsydney/carher-runtime/pulls?q=is%3Apr+is%3Aclosed)
- **CarHer PR**: [#19](https://github.com/buyitsydney/CarHer/pull/19) (e2e SKILL 群+bot id) and [#20](https://github.com/buyitsydney/CarHer/pull/20) (this doc)
- **carher-cicd SKILL**: `.cursor/skills/carher-cicd/SKILL.md`
- **her-feishu-e2e-test SKILL**: `.cursor/skills/her-feishu-e2e-test/SKILL.md` (Group J dify stress 矩阵 + Stress Order rule)
- **dify v1.4.2 source**: https://github.com/langgenius/dify
- **dify-official-plugins source**: https://github.com/langgenius/dify-official-plugins
- **深度研究1 群 chat_id**: `oc_24f93dcf5e05d025b6cf12a204b1bd8f`
