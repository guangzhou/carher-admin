---
name: chatgpt-acct-token-expiry-triage
description: >-
  「有几个 acct token 过期了/帮我重新认证一下」——先把过期号**按 refresh grant 的返回码分流**
  再动手：HTTP 200 的几十秒白捡回来，401 refresh_token_reused 的才值得烧 EIP OAuth。含飞书 view
  清点、12 号分流实测、重新 OAuth 后装回的门禁(比 hash 不比长度)、镜像漂移必须单独一轮收。
  Use when 用户说"看看几个 acct token 过期了""重新认证""token 失效/401/DEVICE_CODE_HELL"，
  or 提到 chatgpt-acct-refresh-grant.py / aliyun-acct-reauth-install.sh。
  ⛔「这号掉 free 了」先跑 refresh grant 拿真档位再决定烧不烧 OAuth：id_token 的 plan 是
  签发快照，2026-10-10 据它误报 acct-227 掉 free，实为 pro(200/订阅到11-09/配额0%)；含 198 侧单号跑法。
  端到端接新号在姊妹 skill aliyun-chatgpt-acct-onboard；续订在 chatgpt-sub-renew-eip；
  198 上的号「开了 pro 要加回 litellm」走 chatgpt-pool-198-add-acct(一条命令含本页分流)。
---

# ChatGPT acct token 过期：分流 → 重新认证 → 装回

2026-09-28 阿里云一轮 12 个过期号全程跑通后沉淀。**核心是别把「token 过期」当一个桶。**
那轮 12 个号分流出两类，处置成本差两个数量级：3 个几十秒白捡回来，9 个得烧浏览器 OAuth。

## ⛔ 「这号掉 free 了」不是烧 OAuth 的理由 —— 先 refresh grant（2026-10-11）

2026-10-10 我看 acct-227 pod 内 id_token 写着 `chatgpt_plan_type=free`，报「已掉 free」。
用户纠正「227 是 pro」。**那个 claim 是 10-05 签发时刻的冻结快照** —— token 过期后它
永久停在那个值，而实探拿不到档位，于是快照成了唯一可见的"档位"。

10-11 对 227 单号跑 refresh grant：**HTTP 200 / `plan=pro` / `sub_plan_live=chatgptpro`
/ `has_active=true` / 订阅到 2026-11-09 / 7d 配额 0% / `is_deactivated=false`**。
号完全健康，**几十秒、零成本救回**，表上那一行从 `UPSTREAM:401 需re-OAuth` 翻成 `OK`。
唯一真问题是 `will_renew=false`（到期不自动续订，11-09 前要人工处理）。

**我差点因此白烧 9 轮 OAuth。** 当时还找到一条"更有力"的反面证据：这 9 个号 pod 里
`.bak-*` 旧快照（9-21~9-29）全是 `pro`，而 10-06 重登后那份变成 `free` ⇒ 看着像「换了
新凭据仍是 free，真掉档了」。**227 正是这个形状，实探却是 pro** —— 所以这条推理也是错的。

| 想当判据的东西 | 实测结论 |
|---|---|
| id_token 的 `chatgpt_plan_type` | 签发快照，token 过期即冻结。**不是现状** |
| SpendLogs 里 gpt-6 族有 success | 只是排序信号 —— 实探确认 free 的 acct-120 也有 591 次 |
| 旧 bak 是 pro、新凭据是 free | **不是判据**，227 反例 |

⇒ **顺序永远是：先 refresh grant 拿真档位，再决定要不要烧 OAuth。**
批量找同形号（只读）：`python3 scripts/chatgpt-acct-plan-misverdict-audit.py`
（10-11 扫出 35 个 JWT 说 free + 实探 401 的号）。

### 198 侧单号跑法（本页其余部分是阿里云口径）

198 的凭据在 **PVC**（`chatgpt-acct-<N>-auth`）不是 Secret，`/chatgpt-auth` 可写，
pod 出口 CF-clean，所以直接 exec 进去跑权威脚本即可，不需要 EIP 节点：

```bash
cd scripts
B64=$(base64 < chatgpt-acct-refresh-grant.py | tr -d '\n')   # docstring 带反引号/$，必须 b64
ssh cltx@10.68.13.198 "export KUBECONFIG=~/.kube/config
POD=\$(kubectl -n litellm-product get pod -l app=chatgpt-acct-<N> -o jsonpath='{.items[0].metadata.name}')
kubectl -n litellm-product exec \$POD -- python3 -c \"import base64,sys;sys.argv=['rg','/chatgpt-auth/auth.json'];exec(compile(base64.b64decode('$B64'),'rg','exec'))\""
```

输出一行：`<file> 200 <email> plan=pro until=... rt_rotated=yes`。

**写后必须回读断言**（RT 只发一次，拿到 200 没写回 = 烧掉凭据）：
`refresh_token`/`access_token`/`id_token` 三个 sha 都要变、`account_id` 保留、
`auth.json.bak-<epoch>` 存在。再用 skill `chatgpt-acct-quota-lark` 里的实探确认
`plan_type`，最后跑一轮刷新让表反映真相。

⚠ **一次只做一个号**，不要 9 个一起上：grant 是不可逆凭据写入，单号的信息量最大、
可退性最高。227 这一枪的全部代价 = 几十秒 + 一份 bak。

## 顺序(不许跳第 1 步)

```
0 清点   飞书 view → 只留 site=aliyun 且真有凭据的
1 分流   in-pod refresh grant，**按返回码分类**        ← 省钱全在这一步
2a 200   已自动写回,直接验收
2b 401   找更新的副本(198/共享 PVC),没有才烧 OAuth
3 装回   ./scripts/aliyun-acct-reauth-install.sh <N...>
4 验收   ./scripts/chatgpt-acct-verify-aliyun.sh <N...>
5 镜像   有 DRIFT 的**单独一轮** kubectl set image
6 收尾   按显式名单删 job + 清本地明文工件
```

## 0 清点 —— view ≠ 整表，且"过期"里混着空壳

```bash
lark-cli api GET /open-apis/bitable/v1/apps/<APP>/tables/<TBL>/records \
  --params '{"view_id":"<VIEW>","page_size":500}'     # 记录在 data.items
```

- ⛔ `lark-cli base +record-search --json '{"view_id":...}'` 会 `800010701`：它**强制要 `keyword`
  + `search_fields`**。想按 view 拉全量就走上面这条 raw API。
- 2026-09-28 实测：190 行里 171 个 `site=198`、**19 个 `site=aliyun`**，其中只有 **12 个真有凭据**，
  另 7 个是 `replicas=0` + 无凭据的空壳。**空壳不算"过期"**，混进去会把工作量算大一倍。
- ⚠️ view 里的 `sub_until` 来自**已失效的 token**，不能当订阅判据(见 §4)。

## 1 分流 —— 这一步决定了要不要烧 OAuth

在 **pod 内**打 refresh grant(pod 出口是 CF-clean 的，本机 Mac 不是)：

```
POST https://auth.openai.com/oauth/token
grant_type=refresh_token  client_id=app_EMoamEEZ73f0CkXaXp7hrann
```

`scripts/chatgpt-acct-refresh-grant.py` 是这套逻辑的权威实现，但它吃**本地** auth.json；
要在 pod 内跑就把探针 base64 送进去(它的 docstring 带反引号和 `$`，直接 heredoc 会被 shell 吃掉)：

```bash
B64=$(base64 < /tmp/probe.py | tr -d '\n')
kubectl -n carher exec "$POD" -c litellm -- env DO_WRITE=1 \
  python3 -c "import base64;exec(compile(base64.b64decode('$B64'),'p','exec'))"
```

**返回码就是分流表**(2026-09-28 实测 12/12)：

| 返回 | 含义 | 数量 | 处置 |
|---|---|---|---|
| **200** | RT 还有效，账号活着 | 122/124/125 | 自动写回，几十秒收工，**不用碰 OAuth** |
| **401 `refresh_token_reused`** | 这把 RT 被**别处**轮转掉了 | 209-213 / 226-229 | 找更新副本 → 没有才烧 OAuth |

- **`refresh_token_reused` 不是号死了**，字面意思是"你这把 RT 已经被换过一次"。
  RT 每次刷新都轮转，两处同时持有同一把就会互相作废 —— 那轮真因是 09-18 一次轮转把 RT 用掉了，
  新的那份从没落到阿里云盘上。唯一能证明号死的是 OpenAI 自己返回 `account_deactivated`。
- 写回**必须同轮完成**：备份 → 写 `.tmp` → `os.replace` → 回读断言 `rt/at` 都换了。
  拿到 200 却没写回 = 把凭据烧掉(那把新 RT 只发一次)。

### 200 不等于能用 —— 凭据和订阅是两件事

122/124/125 三个 refresh 200、凭据救回来了，但验收 `RENEW=NO`、`UNTIL` 停在 2026-09-07/09-08，
smoke 报 `The model 'gpt-5.5' does not exist or you do not have access to it`。
**凭据恢复 ≠ entitlement 恢复**。这三个本来也不在 `cm/litellm-config` 里(0 条腿)、没在服务用户，
所以它们是**要不要花钱**的问题，不是技术问题 —— 过期的 plan 属于重新购买而非续订，交给用户定。

## 2b 烧 OAuth 之前：先找有没有更新的副本

`refresh_token_reused` 说明"更新的那份存在于别处"。找一圈比烧一轮浏览器便宜：

```bash
./scripts/aliyun-onboard-workspace.sh ls <N>              # 共享 PVC /work/auth-acct-<N>.json
kubectl -n carher exec $POD -c litellm -- ls /chatgpt-auth/   # pod 内历史 .bak-*
ssh cltx@10.68.13.198 'ls -la /Data/rancher/storage/*_litellm-product_chatgpt-acct-<N>-auth/'
```

比的是 `expires_at`，**不是 mtime**。那轮三处全是更老的副本 ⇒ 9 个号确实只能重烧。
⚠️ 顺带复核：198 的 acct-210 PVC 里是 `joe.hahn@`，阿里云是 `hellen.marquardt@` —— **编号在两侧漂过**，
所以 join 只能按邮箱，绝不能按编号(和 `aliyun-chatgpt-acct-onboard` §0.5 同一条教训)。

### 凭据权威源

**邮箱密码认表格 `FRVJsbGsTh9kWNtp7uycrPSYnzc` sheet `数据表`**(形状 `Mail-<N>-xxxx`)，
bitable 那列是旧的。`+csv-get` 吐 JSON，真表在 `data.annotated_csv`，每行带 `[row=N] ` 前缀。
那 9 个号 **2FA 密钥全为空 ⇒ 不需要 TOTP**。

### 烧 OAuth

```bash
FORCE_OTP=1 ./scripts/aliyun-eip-onboard.sh oauth <N> <email> <Mail-N-pw> <gpt_pw>
```

- 节点按编号奇偶钉：偶 → `ap-southeast-1.172.16.0.86`，奇 → **`ap-southeast-1.172.16.16.122`**。
  ⚠️ 是 `172.16.16.122`，**不是** `172.16.0.122`(后者这个节点根本不存在，`kubectl get node` 会空返回)。
- 两条路径都正常，别把快的那条当异常：命中 `/choose-an-account`(已有 session)时日志打
  `[5] Need OTP: False`，直接跳过 OTP；没 session 的走 mail.com 取码。209-211 走前者，212/213/226-229 走后者。
- **launcher 脚本退出 ≠ job 完成**。`aliyun-eip-onboard.sh` 发完 Job 就 return 0，
  后台任务通知"completed"指的是它自己。判 job 只认
  `kubectl -n carher get job cgpt-onboard-oauth-<N> -o jsonpath='{.status.succeeded}'`。
- 同理 `-o jsonpath='{.status.succeeded}/{.status.failed}'` 读到 `/` 可能只是**落地前的瞬间**，
  日志里已有 `token_len` 而 job 还没标成功属正常，隔几秒再读。
- 并发 3~4 个是舒服的量(每号约 6 分钟)。别一次开 9 个抢 EIP 出口。

### 手机墙 `/add-phone` —— 用密码表「卡密」列自动接码(2026-10-06)

```bash
SMSBEE_KEY='<卡密列的值>' FORCE_OTP=1 ./scripts/aliyun-eip-onboard.sh oauth <N> <email> <Mail-N-pw> <gpt_pw>
```

- 「卡密」= smsbee.cc 卡密(「短信取号」列只写了首页)。oauth.py `SmsBee` 撞到 /add-phone 才买美国号、轮询短信码、
  没码换号(最多 4 个)。10-06 198 一轮 43 个号全靠它过墙。
- 🔴 **卡一次性**:出码即 `used`。卡少于号时,先给在路由里的号用;卡可以借别的号的(死号的卡最好用)。
- 没走到手机墙就失败的,卡没扣,回读 `panel-login` 的 `keyStatus=verified` 再挪给别人。
- user_code 只活 15min,OTP+换号可能耗光 ⇒ oauth.py 在 [6] 前剩 <4min 自动重申(否则 poll 恒 `Device authorization code has expired`)。
- free 号补腿只补 `gpt-6-luna`:`ONLY=gpt-6-luna arms.py clone <N> <REF> ... --apply`;sol/6.1-sol/astra/5.4 上游对 free 400。

## 3 装回 —— `./scripts/aliyun-acct-reauth-install.sh <N...>`

```bash
EXPECT_226=uxaylntbvsxfp@mail.com ./scripts/aliyun-acct-reauth-install.sh 226 227 228 229
DRY_RUN=1 ./scripts/aliyun-acct-reauth-install.sh 226      # 只跑取件+门禁
```

⛔ **`aliyun-acct-install-auth-on226.sh` 对当前 oauth.py 已经失效**：它从 job 日志 grep
`B64BEGIN/B64END`，而 `chatgpt-onboard/chatgpt-litellm-oauth.py` 现在不再打标记块
(2026-09-28 grep 计数 0)⇒ 必然 `FAIL: 日志无 B64END`。新脚本改走 RWX PVC 工件。

三道装回前门禁(都实测过负例 exit=1，干净工件 exit=0 作阳性对照)：
`access_token` ≥1000 字符 · `account_id` == pod 内现值 · 工件**不带** `device_code_requested_at`。

🔴 **回读断言只许比 hash，不许比长度。** 209/210/211 三号新旧 `access_token` 长度**逐一恰好相等**，
212/213 两号 `token_len` 都是 1685 —— 全是巧合。长度相等被当成"没装进去"或"装进去了"都会翻车，
唯一有判别力的是 `sha256(rt)` / `sha256(at)` 变了没有。

### `device_code_requested_at` 就是看板上的 DEVICE_CODE_HELL

in-image `litellm/llms/chatgpt/authenticator.py`：AT 过期 → `_refresh_tokens` 失败 →
`_login_device_code()` → `_record_device_code_request()` 写下 `auth_data["device_code_requested_at"]`。
`chatgpt_acct_quota_aliyun_view.py` 看到这个字段非空就渲染 `DEVICE_CODE_HELL`(排在 `REFRESH_DYING` 之上)。
那轮 9 个旧 auth.json **全部**带这个字段、新的全部不带 —— 这既是修好的标志，也解释了它们为什么卡死。
所以装回必须清掉它，光换 token 不清等于留着死循环状态。

## 4 验收 —— 唯一判据是 live will_renew

```bash
./scripts/chatgpt-acct-verify-aliyun.sh 209 210 211 212 213 226 227 228 229
```

要求 `NEED_RENEW=（none）` 且 `BAD=（none）`。2026-09-28 结果：
209-213 `UNTIL=2026-10-16`、226-229 `UNTIL=2026-10-18`，九个全 `RENEW=YES PASS/2 same`。

⚠️ **别拿飞书表里的 `renews_at` 对答案**：226-229 表上记的是 `2026-09-18`，live 读到的是 `10-18`,
因为表里那个值是用**已失效的 token** 探出来的。表比 live 旧是常态，不是矛盾。

## 5 镜像漂移 —— **单独一轮，别和 token 混**

227/228/229 当时跑在 `ghcr.io/berriai/litellm:v1.90.2`(公网仓库，违反 ACR VPC 铁律)，
verify 会标 `DRIFT` 并计入 `BAD`。spec diff 确认**只差 image 这一项**后：

```bash
REF=$(kubectl -n carher get deploy chatgpt-acct-226 \
        -o jsonpath='{.spec.template.spec.containers[0].image}')   # 现读参照,不许写死
kubectl -n carher set image deploy/chatgpt-acct-<N> litellm="$REF"
```

- **必须等 token 装完、验收过了再做这一轮**。混在同一次滚更里，出问题分不清是 token 还是镜像。
- 先确认参照 digest 已缓存在目标节点(`kubectl get node <node> -o json` 的 `status.images`)，
  缓存命中就不必拉网。
- 禁 `kubectl apply` 改这些 Deployment，只用 `set image`/`patch`。

## 6 收尾

```bash
for n in <N...>; do
  [ "$(kubectl -n carher get job cgpt-onboard-oauth-$n -o jsonpath='{.status.succeeded}')" = 1 ] \
    && kubectl -n carher delete job cgpt-onboard-oauth-$n
done
```

⛔ 绝不用 `delete job -l job-name`(每个 Job 都带这标签 = 删光整个 ns)，也不用 `cgpt-*` 通配
(会扫掉别的会话在跑的 job，acct-229 的 renew job 就这么被删过)。**只按显式编号点删，且只删 succeeded=1 的。**
本地 `/tmp/auth-acct-*.json` 是明文 token，删掉；zsh 里一条 `rm -f a* b*` 任一 glob 无匹配则整条不执行，
`no matches found` 时"清理跑过了"是假的 —— 分行删并 `ls` 复核。

## 相关 memory

`feedback_disk_authjson_token_is_stale_not_death_proof` · `feedback_renew_status_judge_is_live_will_renew_not_token_or_banner` ·
`feedback_all_acct_images_must_match_acct82_by_digest` · `feedback_acct_pool_cm_is_not_the_only_config_source` ·
`project_acct_175_pulled_from_pool_2026_09_26` · `topic_chatgpt_acct_pool_index`
