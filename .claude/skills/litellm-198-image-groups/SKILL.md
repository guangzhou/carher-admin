---
name: litellm-198-image-groups
description: >-
  198（ns litellm-product）上出图组（`gpt-image-2` / `image-2` / `image-2-zero` /
  grok 的 `sa-grok-imagine*`）的运维：坏了怎么分诊、腿怎么补、名单从哪儿来、怎么验收。
  **核心真因：腿的 `model` 填 `openai/chatgpt-image` 就走 CustomLLM 打 codex images
  端点，token 一失效就是恒定 ~72s H500（重试 5 次）；填 `openai/chatgpt-gpt-5.5`
  走 chat 通道自带出图，同一个号就是好的。** 另有三个会骗人的名单来源（飞书表
  status=ONLINE 是 chat 探针 / `chatgpt-gpt-5.5` 组成员 / deployment READY）——
  出图名单唯一合法来源是逐叶实打。Use when 用户说"出图不能用了"/"gpt-image-2 报错"/
  "画图很慢/卡住"/"把出图的腿补上"/"grok 能不能出图"/"出图组加个兜底"。
---

# 198 出图组运维

> 作用域：**198，ns `litellm-product`**。所有 kubectl 走 `sudo kubectl -n litellm-product`。
> 脚本：`scripts/image-groups-selfcall-repair.py`（本体）、
> `scripts/image-groups-userlayer-probe.py`（§F③ 用户层探针）、
> `scripts/image-2-acct-cutover.py`（历史 cutover，其 `add-acct-all` 已标注别用）。
> ⚠️ 198 上 `/tmp` 写不进去（Permission denied），落盘一律用 `~/imgfix/`。

## ⚡ 分诊：先看耗时，它直接分病

| 现象 | 病 | 去哪节 |
|------|----|--------|
| `probe-accts` 打出 `NODNS` / 全 0 | **探针没出门**（在 host 上跑了），不是号坏 | §B |
| 恒定 ~72s 然后 H500 | 腿指了 `openai/chatgpt-image`，codex token 失效 + 重试 5 次 | §A |
| 0~1s 就 H401 `invalidated oauth token` | 那个号的 token 被作废了（真死号） | §C |
| **组级时好时坏**（如 7/8、间歇 0s H401） | 死腿还在组里被轮到。**不是"坏了"也不是"好着"** | §C' |
| H429 `throttling_error` | 号在冷却，不是死 | §C |
| H400 `No connected db.` | **key 填错了**（母路由那把打叶子），假红 | §B |
| 16~22s 后 OK | 正常。出图本来就这么慢 | — |
| 组级绿但某个号坏 | 组级 200 被别的活腿盖住了，答不了单号死活 | §C |
| `prune-dead` 清完但 verify 仍间歇失败 | 残留「5.5 认着但出图 401」的腿 | §D' |

**判绿只认「拿到 b64 或 url」，不认 HTTP 200。** 也不能只认 b64 ——
有的上游返 url（2026-09-28 踩过：只量 b64 把健康的 `sa-grok-imagine` 判成假绿，
差点把一个能用的组排除出兜底名单）。

## §A 真因：`chatgpt-image` vs `chatgpt-gpt-5.5`（2026-09-28 定论）

- **假设**：坏腿的 `litellm_params.model` 是 `openai/chatgpt-image`，走 CustomLLM
  `/app/chatgpt_images.py`（挂在 ConfigMap `chatgpt-images-handler`）打
  `https://chatgpt.com/backend-api/codex/images/generations`；该号 codex token 已失效。
  好腿的 `model` 是 `openai/chatgpt-gpt-5.5`，走普通 chat 通道（chat 自带出图），
  根本不经过那个端点。
- **证伪条件**：若假设错，同一个号在两个组名下应同生同死；我那条两组完全相同的
  自调用腿也应表现一致。
- **数据**：acct-167 的两条腿逐字段 diff **只差这一个字段**；去掩码日志给
  `litellm.aimage_generation(model=openai/chatgpt-image)` →
  `codex images 上游 401: token_invalidated`，栈顶 `/app/chatgpt_images.py:139`，
  `LiteLLM Retried: 5 times`（5×14s ≈ 那个恒定的 72s）；两组自调用腿
  `litellm_params` 逐键相同，按 `model_id` 直打都 17~21s OK。

⇒ **腿的 `model` 一律填 `openai/chatgpt-gpt-5.5`，永不填 `chatgpt-image`。**

🔴 **上游没有 `gpt-image-2` / `image-2` 这个模型**：直打 Codex 端点返
`The 'gpt-image-2' model is not supported when using Codex with a ChatGPT account`。
出图是 gpt-5.x 自带能力，这两个名字是我们自己的组名。

## §B 两把 master key 别混（混了是假红）

| 打谁 | secret | 本脚本的 env |
|------|--------|--------------|
| 母路由 `litellm-proxy` | `litellm-secrets` | `LITELLM_MASTER_KEY` |
| 叶子 `chatgpt-acct-N` | `chatgpt-pool-master-key` | `POOL_MASTER_KEY` |

拿母路由那把打叶子 → `H400 {"error":{"message":"No connected db.","type":"no_db_connection"}}`。
**这句话看着像数据库挂了，其实只是认证没过。**

```bash
MK=$(sudo kubectl -n litellm-product get secret litellm-secrets           -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
PK=$(sudo kubectl -n litellm-product get secret chatgpt-pool-master-key  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
```

🔴 叶子 Service 只在集群内可达 ⇒ 探叶子的脚本必须 `kubectl cp` 进 pod 里跑，
不能在 198 host 上跑。

🔴 **在 198 host 上跑 `probe-accts` 会打出「0/40 全败」，那是尺子坏了不是号坏。**
2026-10-10 实证：host 上 40 行全是
`ERR 0s URLError(gaierror(-3, 'Temporary failure in name resolution'))` ——
`*.svc.cluster.local` 在 host 的 resolv.conf 里解析不了。同一份脚本 `kubectl cp`
进 proxy pod 再跑，立刻分出面：15 个 OK（16~22s 真出图）、25 个 H401/H403。
**分辨这两种 0 的判据是错误字串，不是通过数**：`gaierror` / `name resolution`
= 探针根本没出门；`H401 ChatgptException ... authentication token` = 号真死。
可抄：

```bash
POD=$(sudo kubectl -n litellm-product get pods \
  -l carher.net/litellm-production-route=enabled -o jsonpath='{.items[0].metadata.name}')
sudo kubectl -n litellm-product cp scripts/image-groups-selfcall-repair.py $POD:/tmp/igr.py
sudo kubectl -n litellm-product exec $POD -- \
  env LITELLM_MASTER_KEY="$MK" POOL_MASTER_KEY="$PK" python3 /tmp/igr.py probe-accts
```

## §C 🔴 出图名单的三个假来源（全部实测踩过）

| 来源 | 为什么不能用 |
|------|-------------|
| 飞书 acct 池表 `status=ONLINE` | 那是 **chat** 探针。2026-09-28：43 个 ONLINE 里 165-178 全 ONLINE，codex 出图全 401 |
| `chatgpt-gpt-5.5` 组成员 | 配额治理筛的是**聊天**活号。同日 33 个成员逐叶实打只 25 个真出图，差集 8 个全 401 |
| deployment `READY>0` | 209-213 / 226-229 表上 ONLINE，实为 `0/0`，直打 `Connection refused` |

第四个假来源：**`prune-dead` 清完 ≠ 组里没死腿**（它只认 5.5 组已摘），见 §D'。

**唯一合法来源 = 逐叶实打 `/v1/images/generations` 拿到 b64 或 url：**

```bash
python3 scripts/image-groups-selfcall-repair.py probe-accts        # 产名单（pod 内）
python3 scripts/image-groups-selfcall-repair.py add-accts --accts <名单> --apply
```

脚本已内置这几道门禁（2026-10-10 加，此前只在本文档里口述）：
`probe-accts` 全是 NODNS 时拒绝产名单并打印进 pod 的命令 · 0 个通过时拒绝给名单
（逼你先读错误字串分病）· `add-accts` 收到空 `--accts` 直接退出
（否则铺 0 条腿却打印「全副本可见 ✅」的假绿）· `verify` 失败非零退出。

🔴 **名单会过期，而且很快。** 2026-09-28 同一天内：上午实测 25/33 通过并据此补腿，
几小时后重跑同一把尺子只剩 14/33 —— 183~189、200 这批在这期间被作废。
判据不是猜的：acct-183 当时拿到 780~905 KB 的图，之后同一路径同一 key 返
`invalidated oauth token` **0 秒**（真出图要 16~22s，0 秒是被明确拒绝，不是抖动）。
⇒ **补腿前必须现跑 `probe-accts`，别复用任何过去的名单**（包括这份文档里的例子）。

## §C' 用户报"不可用"而组级连打全绿时，去量**失败率**不是量通过

🔴 **`verify --times 3` 全绿 ≠ 用户不在报错。** 死腿还在组里，轮到它那一次就 0s H401；
3 次抽不到、8 次抽到 1 次。**这个形状既不是"坏了"也不是"好着"，是「组里混着死腿」。**

2026-10-10 实证（用户反馈 `gpt-image-2` 不可用）：
- `verify --times 3` → 3/3 全绿，14~23s，b64 1.3MB。按这个读数会回"没问题"。
- `verify --times 8` → **7/8**，第 1 次 `401 authentication_error` **0.0s**。
- 同时腿层逐叶实打 → **15/40**，两组 25 条 acct 腿里只 4 个号（196/206/207/208）还活。
- ⇒ 结论不是"出图坏了"，是"25 条腿 21 条是死的，冗余只够盖住大部分请求"。

**所以听到"出图不可用"的第一反应是跑 `--times 8` 以上 + pod 内 `probe-accts`，
不是 `--times 3` 看见绿就收工。** 恢复判据同理：修完要连打 ≥6 次零失败，
`audit` 的僵腿行要是 `0: []`。

`token_invalidated` 要 re-OAuth 才能救，不是重启能解决的 —— 见
`chatgpt-acct-token-expiry-triage`。⛔ **别去重启在服务的 acct pod**，可能永久打死它。

🔴 **「坏号面」和「用户面」是两个数，禁止合并成一个百分比。** 同一时刻实测：
腿层 14/33 通过（19 个号死了），用户层组级 `gpt-image-2` 5/5、`image-2` 5/5 全绿
（18s 上下）—— 冗余把坏号吸收掉了。所以：

- 看到腿层一片 401 **不等于**用户在报错，别去惊动生产；
- 反过来，用户层全绿**也不等于**号都好着 —— 组级 200 会被活腿盖住。
- 报结论时两个数都要给，并说清各自口径。

## §D 两种腿的形状

**自调用腿（推荐，不会漂）**

```json
{"model_name": "gpt-image-2",
 "litellm_params": {
   "model": "openai/chatgpt-gpt-5.5",
   "api_base": "http://litellm-proxy.litellm-product.svc.cluster.local:4000/v1",
   "api_key": "<母路由 master key>",
   "input_cost_per_token": 0, "output_cost_per_token": 0},
 "model_info": {"id": "selfcall-gpt-image-2", "mode": "image_generation"}}
```

复用 5.5 组现成的调度/配额治理，号死了跟着 5.5 一起摘。**188 governor 也碰不到它**：
`pause_acct` 按 acct 的 `api_base` 匹配删 entry，自调用腿的 `api_base` 不是
`chatgpt-acct-N`，天然在匹配范围外。

**acct 腿（逐号铺，扛并发）**：`api_base` 换成
`http://chatgpt-acct-N.litellm-product.svc.cluster.local:4000`、`api_key` 换成**池** key。

🔴 **两种腿都必须 `input_cost_per_token: 0` / `output_cost_per_token: 0`** ——
内层已按 `chatgpt-gpt-5.5` 记账，外层不置 0 就同一次出图记两遍。
验收：`LiteLLM_SpendLogs` 里外层组的行 `spend` 应全为 0，钱落在 `chatgpt-gpt-5.5` 上。

🔴 **188 governor 的 `models_for()` 不含任何 image 模型** ⇒ 逐 acct 腿被
`pause_acct` 删掉后，`resume` 永远不会重建。这是逐 acct 腿必漂的第二个理由。

## §D' 两条摘腿判据不是一回事，**两个都要跑**

| 命令 | 判据 | 摘的是 |
|------|------|--------|
| `prune-dead` | CARRIER(`chatgpt-gpt-5.5`) 组已摘这个号 | 跟着 5.5 一起退役的 |
| `prune-by-probe` | 这个号**出图实打**不通过 | 5.5 还认着、但出图已 401 的 |

🔴 **`prune-dead` 摘不干净。** 2026-10-10 实证：修复前 25 条腿里 21 条的号出不了图，
`prune-dead` 只摘掉 15 条（那 15 个 5.5 也摘了）；剩 **6 条（182/183/185/195/198/205）
5.5 认着、出图 0s H401**，`prune-dead --apply` 跑完它们仍在组里 ——
**这就是 `verify --times 8` 里那 1 次失败的来源**。补跑 `prune-by-probe --apply`
摘掉这 6 条后，两组各 16 腿、`verify` **8/8 + 8/8 全绿**。

```bash
# 必须在 pod 内跑（探叶子要集群 DNS）；dry-run 先看
python3 /tmp/igr.py prune-by-probe            # dry-run
python3 /tmp/igr.py prune-by-probe --apply
```

内置两道护栏：全是 NODNS 时拒绝执行（否则在 host 上跑会把活腿全判死摘光）；
摘完某组不足 2 腿时跳过该组（先补腿再来）。

## §E grok 也能出图，而且快 3~4 倍

四个组全活（2026-09-28 实测 ×2），`api_base` 全指 sub2api
`http://sub2api.litellm-dev.svc.cluster.local:8080/v1`，每组 1 条腿：

| 组 | 载荷 | 耗时 |
|----|------|------|
| `sa-grok-imagine` | **url** | 4~5s |
| `sa-grok-imagine-image` | **url** | 6s |
| `sa-grok-imagine-image-2.0` | b64 107~130 KB | 10~11s |
| `sa-grok-imagine-image-quality` | b64 246~252 KB | 4~5s |
| `ag-gemini-3.1-flash-image` | — | **H400 ×2，死的** |

🔴 **「1 条腿」不等于「1 个账号」**：这 4 条腿背后是 sub2api 那个池子在内部轮转
（group 7「grok账号分组」，2026-09-28 有 47 个号、`schedulable=true` 31 个）。
⇒ 想知道 grok 出图容量，去 sub2api 现读，**不能从 LiteLLM 的腿数推**。
而且 `schedulable=true` 也是**聊天**口径 —— sub2api 对「哪条腿认哪些 model」是
每腿一份缓存快照，出图模型可能只在一部分腿上认（这一项 09-28 尚未逐号实测）。
sub2api 侧运维见 `sub2api-grok-ops`。

## §F 验收：三层，缺一层不算修好

```bash
# ① 配置层：每个副本自己读到了吗（写 DB 后 4 副本刷新有延迟）
#    「写完读一次」不是判据 —— 必须连续 3 次读到（脚本里的 wait_visible）
for p in $(sudo kubectl -n litellm-product get pods \
    -l carher.net/litellm-production-route=enabled -o name); do ... done

# ② 组级：连打量失败率。默认已是 8 次，别往下调（3~4 次抽不到死腿）
#    失败一次就非零退出 —— 间歇失败不是「基本可用」，是组里还有死腿
python3 scripts/image-groups-selfcall-repair.py verify
#    腿层单条：probe-accts（pod 内）才答得了「这一个号活不活」

# ③ 用户层：克隆真 key 的白名单起临时 key，打组名 + 一个白名单外的阴性对照
#    master key 绕过白名单，用它验等于没验用户那条路
#    脚本（别再手搓）：参数 1 = 逗号分隔白名单，参数 2 = 要打的模型名（含阴性对照）
ML=$(sudo kubectl -n litellm-product exec -i litellm-db-0 -- psql -U litellm -d litellm \
  -t -A -c "SELECT array_to_string(models,chr(44)) FROM \"LiteLLM_VerificationToken\"
            WHERE key_alias='carher-75' LIMIT 1;" | tr -d ' \r')
python3 scripts/image-groups-userlayer-probe.py "$ML" "gpt-image-2,gpt-image-2,nonexistent-neg"
```

③ 的阴性对照应返 `H403 key_model_access_denied`。临时 key 用完即删（脚本 finally 里删）。

🔴 **③ 只能证"白名单里有这个名字的 key 走得通"，证不了别的 key。**
实测 `carher-75` 的白名单里有 `gpt-image-2` 但**没有** `image-2` ——
拿它打 `image-2` 返 `H403 key_model_access_denied`，这是**白名单问题不是组故障**，
和阴性对照一模一样的报错。两组都要验用户层的话，得各找一把真有那个名字的 key。
分母参考（2026-10-10）：`gpt-image-2` 1233 把 key、`image-2` 516 把。

⚠️ ssh 外层会吞掉内联 heredoc 里的单引号/中文，手搓 `python3 -c` 会炸成
`NameError: name '无' is not defined` 这种与业务无关的报错 ⇒ 探针**落文件再 scp**，
别内联。

2026-09-28 那轮的实际读数（形状参考，数字别复用）：逐腿 52/52、组级用户 key 12/12
（850 KB 上下、17s 上下）、阴性对照正确 403、外层 22 行 spend 全 0、内层 12 行 $0.0073。

## §G 2026-10-10 那轮的处置序列（形状参考，名单别复用）

用户反馈"gpt-image-2 不可用" → 实际是 21/25 条 acct 腿已死、冗余只盖住 7/8：

```
audit                            两组各 26 腿、自调用腿在位、僵腿 15
verify --times 8                 gpt-image-2 7/8（1 次 0s H401）、image-2 8/8
probe-accts（pod 内跑）           15/40 真能出图
add-accts --accts <15> --apply   纯加法补 11×2=22 条新腿 → 全副本可见 ✅
verify --times 6                 12/12 全绿（绿了才允许进下一步）
prune-dead --apply               摘 15×2=30 条僵腿 → 连读 3 次收敛到 22/22
verify --times 6                 12/12 全绿 —— 但这是 6 次抽样，没抽到残留死腿
prune-by-probe --apply           ⬅ 补这一步：又摘 6×2 条「5.5 认着但出图 401」
verify（8 次）                    **16 腿 8/8 + 8/8**、exit=0 ← 真正的收口
image-groups-userlayer-probe     3/3 OK 14~16s、阴性对照正确 403
spend 核对                        90 分钟内外层 image-2 行 spend=0（没重复计费）
```

🔴 **只跑到 `prune-dead` 会停在 7/8。** 第一轮我报的"修好了"是 22 腿 + `verify 6/6`，
但那 6 次恰好没抽到残留的 6 条死腿 —— 正确的收口是 `prune-by-probe` 之后的
16 腿 8/8。**抽样次数不够时"全绿"会盖住没做完的工作**，与 §C' 同一个病。

⚠️ `prune-*  --apply` 刚跑完回读还会带着已删腿（副本刷新延迟），
**连读 3 次收敛才算摘干净**，别当成"删失败了"再删一遍。

## §H 已知未查清（别当成已知结论去讲）

- **原先那 12 条 acct 腿是谁删的**：188 governor 日志今天 0 次 `image-2` 命中、
  无 pause/resume；`LiteLLM_AuditLog` 整张表空的（审计没开）；proxy pod 只有
  9 条 `POST /model/delete 200` 且不记对象名。**没有数据 ⇒ 不下结论。**
- **grok 那 31 个可调度号里，几个真能出图**：未逐号实测。
