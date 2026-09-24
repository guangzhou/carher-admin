# 杭州网关（gw.carher.net）生成 carher key 的 API 文档

适用对象：阿里云杭州 `vllm`（47.96.23.21）上的 `llm-gateway` 这一套 LiteLLM。
**这不是新加坡那套 `litellm-proxy.carher.svc`，也不是 198**，三者的 key 约定互不相同，
见文末「§7 不要和另外两套混」。

采集时间 2026-09-24，读取部分全部为只读（`/openapi.json`、`/v1/models`、`/team/info`、
Postgres `SELECT`）。写操作已用废弃 uid `bot-999999` 在该网关上实测并删除。

> ✅ **日常开号不要手搓这些 curl**，用脚本：
> `scripts/litellm-her-key-from-template.py`（机上副本 `/opt/llm-gateway/` 同名文件）。
> 它每次都现读参照实例 `bot-1000` 再复制，避免把模型配置和 `team_id` 写死。
> 配套 SOP（含判据、退出码、回滚）：
> <https://t83dfrspj4.feishu.cn/docx/BvtXdtgbSoBRVCxpqaEcwe3mngd>
> 本文只保留裸接口口径，供脚本不可用时兜底。

---

## 1. 入口与凭据

| 项 | 值 |
|---|---|
| 机内地址 | `http://127.0.0.1:4000` |
| 公网直连 | `http://47.96.23.21:4000`（compose 里 `ports: 4000:4000` 绑 `0.0.0.0`，实际可达性取决于阿里云安全组） |
| 域名入口 | `https://gw.carher.net`（cloudflared named tunnel `llm-gateway` → `127.0.0.1:4000`） |
| admin key | `LITELLM_MASTER_KEY`，存放于机上 `/opt/llm-gateway/.env`，**本文不落值** |
| 后端 DB | 同机容器 `llm-gateway-postgres-1`，库 `litellm` |

取 admin key（不回显到别处）：

```bash
scripts/jms ssh vllm 'grep ^LITELLM_MASTER_KEY= /opt/llm-gateway/.env'
```

在脚本里用它的正确姿势是 source 而不是复制粘贴：

```bash
scripts/jms ssh vllm 'cd /opt/llm-gateway && set -a && . ./.env && set +a && \
  curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" http://127.0.0.1:4000/v1/models'
```

> ⛔ **master key 绝不能下发给 bot 实例**。bot 只拿 `/key/generate` 产出的 virtual key。
> 这条在 `/opt/llm-gateway/README.md` 的 client contract 里也写着。
> ⛔ `LITELLM_SALT_KEY` 在有 key/model 落库后**永不可改**，改了等于全表解不开。

---

## 2. 这台机上「carher key」的现行形状

线上 358 把 key，`SELECT` 出来的约定是统一的：

| 字段 | 取值 | 说明 |
|---|---|---|
| `key_alias` | `bot-<N>` | N = 实例号；344 把是这个形状 |
| `team_id` | `45504b92-286f-40fc-9ae0-dedd5d7f7c4e` | team_alias = `carher-bot`，351 把 key 挂在它下面 |
| `models` | `["all-team-models"]` | 353 把都是它；不是逐个模型名列举 |
| `aliases` | `{}` | 本网关**不用** per-key alias |
| `max_budget` / `tpm_limit` / `rpm_limit` / `expires` | 全部 `null` | 当前不设预算与限速 |
| `metadata` | `{}` | 当前不带元数据 |

team `carher-bot` 自身 `models = ["all-proxy-models"]`、无预算无限速，
所以 `all-team-models` 实际展开 = 本网关全部 31 个模型。

---

## 3. 生成一把 carher key

`POST /key/generate`，Bearer 用 admin key。
请求体 schema = `GenerateKeyRequest`（该版本共 52 个字段，下表只列本场景用得到的）。

| 字段 | 类型 | 本场景取值 | 必填 |
|---|---|---|---|
| `key_alias` | string | `bot-<N>` | 是（唯一，重复会 400） |
| `team_id` | string | `45504b92-286f-40fc-9ae0-dedd5d7f7c4e` | 按舰队惯例填（352 把里 350 把有）。**不填不会坏**：实测无 team 的 `all-team-models` 落到不限制、31 个模型全可见；代价是逃掉未来任何 team 级预算/限速 |
| `models` | string[] | `["all-team-models"]` | 是 |
| `user_id` | string | 省略（现网都是空） | 否 |
| `metadata` | object | 省略 | 否 |
| `max_budget` | float | 省略 = 不限 | 否 |
| `budget_duration` | string | 如 `30d`，配合 `max_budget` 用 | 否 |
| `tpm_limit` / `rpm_limit` | int | 省略 = 不限 | 否 |
| `duration` | string | 省略 = 永不过期；填 `30d` 则写入 `expires` | 否 |
| `key` | string | 省略 = 由服务端生成 `sk-...`；**只有迁移场景才自带** | 否 |

### 请求（已实测）

```bash
scripts/jms ssh vllm 'cd /opt/llm-gateway && set -a && . ./.env && set +a && \
  curl -s -X POST http://127.0.0.1:4000/key/generate \
    -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
    -H "Content-Type: application/json" \
    -d "{
      \"key_alias\": \"bot-999\",
      \"team_id\": \"45504b92-286f-40fc-9ae0-dedd5d7f7c4e\",
      \"models\": [\"all-team-models\"]
    }"'
```

### 响应

```json
{
  "key": "sk-<新密钥明文，只在这一次返回>",
  "key_alias": "bot-999",
  "team_id": "45504b92-286f-40fc-9ae0-dedd5d7f7c4e",
  "models": ["all-team-models"],
  "expires": null,
  "token_id": "..."
}
```

> 🔴 **`key` 明文只出现在这一次响应里**。库里存的是 hash，`key_name` 只保留
> `sk-...` 前后各几位（如 `sk-...4oOw`）。没接住就只能 `/key/regenerate` 重发一把。

### 带预算的变体 `[未实测]`

> 现网 352 把 bot key 全部不限额，下面这个形状偏离统一约定，只在明确要单独设闸时用。

```json
{
  "key_alias": "bot-999",
  "team_id": "45504b92-286f-40fc-9ae0-dedd5d7f7c4e",
  "models": ["all-team-models"],
  "max_budget": 50,
  "budget_duration": "30d"
}
```

---

## 4. 验证（只读，可放心跑）

```bash
# 按 alias 查（唯一可用的读法）
curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  "http://127.0.0.1:4000/key/list?key_alias=bot-999&return_full_object=true&size=5"

# 列出 team 下全部 key
curl -s -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  "http://127.0.0.1:4000/key/list?team_id=45504b92-286f-40fc-9ae0-dedd5d7f7c4e&size=500"
```

> 🔴 **不要用 `/key/info?key_alias=`**，本版对它返 404（实测 2026-09-24，新加坡那套同样 404），
> 看着像「这把 key 不存在」。能按 alias 读出完整行的只有 `/key/list` 加 `return_full_object=true`。
> ⚠️ `/key/list` 默认分页会静默只回一页。要做「某 alias 不存在」这种否定结论，
> 必须显式带 `key_alias` 过滤或直接走 DB，别拿一页的结果下结论。

DB 侧兜底核对：

```bash
scripts/jms ssh vllm 'nerdctl exec llm-gateway-postgres-1 sh -c \
  "psql -U \$POSTGRES_USER -d \$POSTGRES_DB -x -c \
   \"select key_alias,key_name,models,team_id,max_budget,expires from \\\"LiteLLM_VerificationToken\\\" where key_alias='\''bot-999'\'';\""'
```

**真正的可用性验收只有一条：拿新 key 打一次推理**，`/key/info` 查得到不等于能用。

```bash
curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-<新key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"reply with exactly: pong"}],"max_tokens":512}'
```

> 🔴 **`max_tokens` 给小了会造出假红**：菜单里多是推理模型，预算先被 reasoning token 吃光，
> 结果是 HTTP 200 但 `content` 为空字符串，看着像 key 没权限。判据先看
> `usage.completion_tokens_details.reasoning_tokens` —— 它约等于 `completion_tokens` 就是预算不足。
> 实测 `max_tokens=32` 假红、`512` 正常。
> 再补一条阴性对照：拿不存在的模型名打，应返回 400 `Invalid model name`。

---

## 5. 改 / 删 / 封 / 换发

| 动作 | 端点 | 说明 |
|---|---|---|
| 改策略 | `POST /key/update` | body 带 `key`（明文）或 `key_alias` + 要改的字段 |
| 删除 | `POST /key/delete` | `{"key_aliases": ["bot-999"]}` 或 `{"keys": ["sk-..."]}` |
| 临时封停 | `POST /key/block` / `POST /key/unblock` | 置 `blocked`，比删除可逆 |
| 泄露后换发 | `POST /key/regenerate` | 保留 alias/预算，换掉密钥本身 |
| 清零花费 | `POST /key/{key}/reset_spend` | 只清计数，不动权限 |

改名 / 换映射按加法顺序做：**add → verify → cutover → 最后 remove**，不要先删旧的。

---

## 6. key 能调的模型（live 31 个，2026-09-24）

`models: ["all-team-models"]` 展开后即下列全部：

```
BAAI/bge-m3            claude-haiku-4-5        claude-opus-5           claude-sonnet-5
codex-auto-review      composer-2.5            composer-2.5-fast       deepseek-v4-flash
deepseek-v4-pro        deepseek-v4.1-flash     glm-5.3-flash           gpt-5.2
gpt-5.4                gpt-5.5                 gpt-5.6-luna            gpt-5.6-sol
gpt-5.6-terra          gpt-6-astra             gpt-image-2             grok-4.20
grok-4.20-0309-reasoning  grok-4.5             grok-4.5-latest         grok-4.6
grok-4.6-latest        grok-4.7                grok-imagine-image-2.0  kimi-k2.7-code
local-deepseek-v4-flash   perplexity-sonar     qwen3-coder-next
```

来源有两层，**改的时候要先分清动哪一层**：

1. **配置文件层** —— `/opt/llm-gateway/litellm/config.yaml` 里只有 4 行：
   `deepseek-v4-pro`、`deepseek-v4-flash`、`deepseek-v4.1-flash` 走官方
   `api.deepseek.com`；`local-deepseek-v4-flash` 走 `http://36.151.241.10:8000/v1`
   （堡垒机资产 `local-gpu`）。改这里要重建容器。
2. **DB 镜像层** —— `general_settings.store_model_in_db: true`，另外 27 个是
   `/opt/llm-gateway/sync/sync_models.py` 由 systemd timer `litellm-model-sync.timer`
   每天 03:30 从 `https://cc.auto-link.com.cn/pro/v1/models` 镜像下来的，
   `api_base` 统一指向 `https://cc.auto-link.com.cn/pro/v1`。
   **手工往 DB 加的镜像模型会在下次 sync 被对账掉**；`deepseek-v4-flash` 因为与本地行
   重名，sync 会打印 `skipped local collisions` 主动跳过。

兜底链（config.yaml）：`deepseek-v4-pro → deepseek-v4-flash`，
`default_fallbacks: [deepseek-v4-flash]`。

---

## 7. 不要和另外两套混

| | 本文（杭州 gw） | 新加坡舰队 | 198 |
|---|---|---|---|
| 生成方 | 手工 / 本文 API | `backend/litellm_ops.py::generate_key()` | 各 skill |
| `key_alias` | `bot-<N>` | `carher-<uid>` | 按族前缀 |
| `models` | `["all-team-models"]` | 逐个列举 `ALL_MODELS`（~40 个具体名） | 按 allowlist |
| `aliases` | `{}` | `deepseek-v4-flash → local-deepseek-v4-flash` 等 per-key alias | 大量 per-key alias |
| `team_id` | 固定 `carher-bot` | 不设 | 不设 |
| `router_settings` | 不带 | 随 key 下发 fallback 表 | 全局 |

`backend/litellm_ops.py` 的 `LITELLM_PROXY_URL` 默认是
`http://litellm-proxy.carher.svc:4000`，**它不会打到杭州这台**。
想让 admin 后端给杭州发 key，得另配 URL + master key，那是另一件事，不在本文范围。

---

## 8. 已知坑

1. **时间全是 UTC**。`LiteLLM_SpendLogs.startTime`、DB `now()` 都是 UTC，机器本地是 CST(+8)。
   直接拿时间戳当本地时间读，会把「刚刚」读成「8 小时前没流量」。
2. **litellm 容器每 1~2 小时重启一次**，这是 compose 里
   `--max_requests_before_restart 10000` 的设计行为（exit=0、无内核 OOM、重启间隔随流量
   伸缩：夜间低谷 6 小时才攒够 1 万次）。新发的 key 恰好撞上 recycle 窗口时，
   首次调用可能吃一个连接错误，重试即可。
3. **cloudflared 是裸进程不是 systemd 服务**（unit 文件躺在
   `/opt/llm-gateway/cloudflared/` 但没装）。机器重启后 `gw.carher.net` 不会自己回来，
   届时只有 `47.96.23.21:4000` 这条路。
4. **`bot-313` 与 `bot-313-migration-poc` 两把 key 没有 `team_id`**（09-21 建），
   是偏离舰队惯例的漂移，但**不影响可用性**——实测这种 key 仍能看到全部 31 个模型并正常调用。
   真正的代价是它逃掉 team 级预算/限速（当前 team 也没设，所以暂无实际影响）。
5. 日志里有一类与本流程无关的既存报错：
   `'utf-8' codec can't encode character '\ud83e': surrogates not allowed`，
   emoji 代理对在 fallback 路径上会把请求打成 500。量级未统计。
6. **管理台会自建 key**。库里有一类 `key_alias` 为空、挂 team `litellm-dashboard`、
   `max_budget=1`、`models={}` 的行，约每天一把，来自有人登录 Web 管理台
   （日志里能看到 `login_v2` 失败尝试，成功登录不写日志、`LiteLLM_AuditLog` 未开启）。
   做「key 总数对不对」这种核对时必须把它们排除，否则会把它误判成试跑残留或有人偷偷建号。
7. `/opt/llm-gateway` **不是 git 仓**，只有一堆 `config.yaml.bak.*` 时间戳备份。
   改配置前自己先 `cp` 一份，回滚只能靠这些 bak。
