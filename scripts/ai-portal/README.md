# ai-portal 后端

`https://chat.auto-link.com.cn/ai/` 的自助门户后端：登录、查自己的 key、申请、一次性安装码、反馈。

两种登录，落成同一种会话（会话里记 `login=oauth|magic` 和 `app_id`）：
- **内部人**：飞书网页 OAuth（只认本租户；外部公司 / 个人版飞书会报 20010）。
- **外部人**：机器人在私聊里发一次性登录链接（magic link），这是外部人唯一的入口。
只用 Python 标准库（http.server + sqlite3），前端静态文件在 `static/`（另一位同事维护，后端只负责出文件）。

## 本地跑

```bash
cd scripts/ai-portal
cat > ai-portal.env <<'EOF'
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
SESSION_SECRET=<python3 -c 'import secrets;print(secrets.token_urlsafe(48))'>
EOF
python3 server.py --env ai-portal.env      # 默认 127.0.0.1:8793,路径前缀 /ai
```

`SESSION_SECRET` 没配（或短于 32 字符）进程直接退出（退出码 2），没有默认值。
`ai-portal.env` 里是真凭据，别提交。

## 测试（离线，假飞书）

```bash
python3 -m pytest scripts/ai-portal -q
```

## 接口

| 方法 | 路径 | 登录 | 说明 |
|---|---|---|---|
| GET | `/ai/` | 否 | `static/index.html` |
| GET | `/ai/auth/login?redirect=/ai/...` | 否 | 302 去飞书授权页；`redirect`（或 `next`）只认 `/ai/` 下的相对路径 |
| GET | `/ai/auth/callback?code&state` | 否 | 换 token → user_info → 建会话 → 302 回 `next` |
| GET | `/ai/auth/magic?t=` | 否 | **只出页面，不消费 token**（飞书链接预览会先 GET） |
| POST | `/ai/auth/magic` `{t}` | 否 | 要 `X-Requested-With: ai-portal` + 本站 Origin；成功 `{ok:true}` + 会话 cookie；失败 `{error: expired\|used\|invalid, message}` |
| POST | `/ai/api/internal/magic-link` `{union_id, open_id, tenant_key, name}` | `Authorization: Bearer $INTERNAL_SECRET` | 机器人调；`{url, expires_in}`；token 10 分钟、一次性、库里只存哈希；没配 `INTERNAL_SECRET` ⇒ 404 |
| GET / POST | `/ai/auth/logout?redirect=/ai/` | 是 | 删服务端会话（GET 302 回 redirect；POST 要本站 Origin） |
| GET | `/ai/api/me` | 是 | union_id / open_id / tenant_key / name / avatar / email / external |
| GET | `/ai/api/keys` | 是 | `{keys:[{record_id, account_type, account_types, status, current, masked}]}`；整个响应按 `sk-` 正则打码 |
| POST | `/ai/api/keys/reveal` `{record_id}` | 是 | `API Key` 列明文；只在自己的行里找；已禁用/无 key ⇒ 403；每次记审计（`reveal_audit` 表，不记明文） |
| POST | `/ai/api/keys/rotate` | 是 | 501「需要换 key 请提反馈」（poller 没有轮换路径） |
| POST | `/ai/api/apply` `{account_type?}` | 是 | 固定 Cursor；先精确查，已有 current ⇒ `{existing:true}` 不写；生成中 ⇒ `{pending:true}`；默认 dry-run |
| POST | `/ai/api/install-code` `{tool}` | 是 | `tool`=cursor/codex/claude/**all**（all = 一个码装三个，安装器依次装 Codex → Claude Code → Cursor），三个工具共用同一把 current 的 Cursor key；`{code, expires_in, tool}`；码形如 `XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX`（base32，160 bit），10 分钟，用一次作废 |
| POST | `/ai/api/install-code/redeem` `{code}` | **否** | 脚本用；`{key, account_type, tool}`（tool 原样回签发时的值，可能是 `all`）；不分大小写、横线可省；按 IP + 全局限流 |
| POST | `/ai/api/feedback` `{type, tool, title, detail, report, os, page}` | 是 | 写「反馈」表；字段校验和 key 打码复用 `scripts/feedback/feedback_server.py`；自检报告并进「详细描述」 |
| GET | `/ai/healthz` | 否 | 健康检查 |

POST 都要 `Content-Type: application/json`；除 redeem 外都要本站 `Origin`（或本站 `Referer`）。
出错统一回 `{error, message}`（前端读 `message`）。`static/` 只出白名单后缀（`dev_mock.py` 不会被当静态文件出）。

## key 表规则（数据组实测）

- 状态按前缀映射：`有效*`/`已完成` → 可用；`已禁用*` → 已禁用；`生成失败*` 原样；`生成中*` 或 `API Key=__provisioning__` → 生成中；其它原样。
- `current`：每人只一行 = 状态以「有效」开头 + 账户类型含 Cursor + `API Key` 以 `sk-` 开头，取 `申请时间` 最新。
- 客户批量 key 不算本人：`组织名称` ∈ `EXCLUDE_ORGS`，或一串**连续**生成的行（相邻间隔 ≤ `BATCH_GAP_MS`，默认 60 秒）合计 ≥ `BATCH_MIN_ROWS` 行。没有申请时间的老行不参与聚类。
  （11-04 用真表复核推翻了「同一自然分钟 ≥N 行」：批量会跨分钟边界被切开，尾巴漏过后会被选成本人的当前 key；没时间的老行会被凑成一批整组藏掉。）
- 明文 key 还在 `体检链接` / `点我体检` / `Cursor Key` / `Claude Code Key`：返回前整行按 `sk-` 打码，只有 reveal 出 `API Key` 明文。
- 搜索返回 `1254018 InvalidFilter` ⇒ 当查无记录（记一条日志）；其它错误码 ⇒ 502，不当「没有 key」。
- 后端身份只能用 carher-236（`cli_aa8505afb0f89cb2`），配别的 `FEISHU_APP_ID` 拒绝启动（carher-1000 只有 view 权限，搜出来 total=0）。

## env

| 变量 | 默认 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 必填 | 必须是 carher-236（OAuth 和 bot 读写表用同一个） |
| `REQUIRE_APP_ID` | `cli_aa8505afb0f89cb2` | 置空可关掉上面那条校验（只给测试/别的环境用） |
| `SESSION_SECRET` | **必填，无默认** | ≥32 字符；会话签名、安装码 / state / magic token 哈希都从它派生 |
| `INTERNAL_SECRET` | 空 = magic-link 签发接口关闭 | ≥32 字符，不能和 `SESSION_SECRET` 相同；只给机器人 |
| `MAGIC_TTL` | `600` | 秒 |
| `LISTEN` | `127.0.0.1:8793` | |
| `PUBLIC_ORIGIN` | `https://chat.auto-link.com.cn` | Origin 校验和回调地址都用它 |
| `BASE_PATH` | `/ai` | nginx 原样转发，不剥前缀 |
| `STATIC_DIR` | `./static` | |
| `DB_PATH` | `./ai_portal.db` | 会话 / state / 安装码 / 申请锁 |
| `FEISHU_OPEN_BASE` | `https://open.feishu.cn/open-apis` | |
| `FEISHU_AUTHORIZE_URL` | `https://accounts.feishu.cn/open-apis/authen/v1/authorize` | ⚠️ 待确认 |
| `FEISHU_TOKEN_PATH` | `/authen/v2/oauth/token` | ⚠️ 待确认 |
| `FEISHU_USERINFO_PATH` | `/authen/v1/user_info` | |
| `FEISHU_SCOPE` | 空 | |
| `OUR_TENANT_KEY` | `1088406f1c155740` | 不同 ⇒ `external=true` |
| `KEY_BASE_TOKEN` / `KEY_TABLE_ID` | key 表 | |
| `KEY_PERSON_FIELD` / `KEY_FIELD` / `KEY_TYPE_FIELD` / `KEY_STATUS_FIELD` | `姓名` / `API Key` / `账户类型` / `状态` | |
| `KEY_TIME_FIELD` / `KEY_ORG_FIELD` | `申请时间` / `组织名称` | |
| `EXCLUDE_ORGS` | `巧虎,美团,北汽研究院` | 逗号分隔。表里真实选项：无锡车联、上海车联、北汽研究院、奇瑞、美团、巧虎、北汽集团、妙趣童年、零跑汽车（11-04 读）。批量 key 主要靠聚类规则兜，这里只放「整个组织都是客户批量」的那几家 |
| `BATCH_GAP_MS` | `60000` | 批量是每 ~3 秒一把（最长一次间隔 27 秒），本人手动申请两次以天计 |
| `BATCH_MIN_ROWS` | `5` | |
| `APPLY_DRY_RUN` | `1` | `0` 才真写表 |
| `APPLY_ORG_NAME` | 空 | 非空则写进「组织名称」，必须是表里已有选项（本服务不校验） |
| `INSTALL_CODE_TTL` | `600` | 秒 |
| `REDEEM_PER_IP_PER_MIN` / `REDEEM_GLOBAL_PER_MIN` | `10` / `120` | |
| `FEEDBACK_BASE_TOKEN` / `FEEDBACK_TABLE_ID` | 同 feedback 服务的「反馈」表 | |
| `FEEDBACK_PER_MIN` | `3` | |
| `SESSION_TTL` / `STATE_TTL` | `43200` / `600` | 秒 |
| `TRUST_PROXY` | `0` | `1` = 对端是本机时信 `X-Real-IP`（nginx 必须 `proxy_set_header X-Real-IP $remote_addr` 覆盖） |

## 部署

待定。

## 外部人（其他公司 / 飞书个人版）

- 登录：网页 OAuth 只认本租户（外部 20010 ⇒ 后端回跳 `/ai/?login_error=external`）。外部人唯一入口 = 给机器人「ai管理的her」发「登录」⇒ 一次性链接。
- 申请：`APPLY_ALLOW_EXTERNAL=1`（现网）⇒ 和内部一样真写 key 表、poller 发；`=0` ⇒ 只登记待审核。
- 待审核的最小流程（`APPLY_ALLOW_EXTERNAL=0` 时）：
  1. 门户写一条反馈「外部合作方申请 key:…」，详细描述里有 union_id 和处理步骤。反馈表写不进时只在本地库：`python3 server.py --env /etc/carher-ai-portal.env --list-apply-requests`。
  2. 管理员在 key 表新建一行：姓名 = 这个人、账户类型 = Cursor、组织名称 = 他公司（已有选项），API Key 留空 ⇒ poller 约 1 分钟发 key。
  3. 外部人收不到 poller 的群通知，也收不到反馈服务的私信（那个服务用另一个飞书应用）⇒ 页面承诺的是「重新发『登录』进来看」，必要时由对接人告诉他。
- 外部人打不开的东西：key 表分享链接（`share_entity=same_tenant`、`external_access_entity=closed`）、内部群、`/feedback/`（要 openchat 登录）。`/oneclick/` 和 `/ccdl/` 下载能打开，但里面的「查看 key」打不开 ⇒ 页面让他在门户第 1 步复制 key。
