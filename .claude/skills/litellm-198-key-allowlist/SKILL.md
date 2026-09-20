---
name: litellm-198-key-allowlist
description: >-
  批量改 198 LiteLLM 的 cursor-* / claude-* / carher-* key 的 `models` 白名单
  和 per-key `aliases`（新模型上线要"让大家都能用"、下线要"从所有人那儿摘掉"、
  把一堆内部名收敛成一个对外名再映射到真实组）。含 read-merge-write 纪律、
  `models=[]` 是全放行不能收窄、删+加+映射合成一次写、金丝雀+双对照验收、
  跑到收敛、换尺子回读、快照回滚、并发写的开跑前防撞与事后取证、
  **撤权限必须 `--include-blocked`**（blocked 只挡认证不清白名单，解封会静默复活）、
  白名单里有名字 ≠ 能用（缺 alias 的裸名过了闸门照样 400）。
  Use when 用户说"把这个模型给所有 cursor/claude key 都加上"/"让同事们都能用 X"/
  "把 X 从白名单里摘了"/"把 X 的权限都删除了"/"只保留一个名字 Y 映射到 Z"/"谁能调这个模型"。
  ⚠️ 白名单 ≠ 引流：要让流量自动打过去是 fallback，不是这个 skill。
---

# 198 Key 白名单 + per-key alias 批量编辑

## 先分清三件事（点错了就白干）

| 想要的效果 | 该动什么 | 本 skill |
|---|---|---|
| 让某些人**能调**某模型 | key 的 `models` 白名单 | ✅ |
| 让某人的请求**自动改道**到别的组 | per-key `aliases` | ✅（单人切供应商仍看 `litellm-key-provider-swap`）|
| 主力挂了**自动兜底**到备选 | 全局 `router_settings.fallbacks` | ❌ 见 `litellm-ops` |

**白名单只发通行证，不发流量。** 加进白名单的模型，只有客户端主动选它才会被调用。
所以往白名单里放一个额度稀缺的模型是安全的；把它放进 fallback 链**不安全**——
主力挂一次就会自动打过去，几分钟内把额度抽干，而且是静默的。

## 白名单闸门查的是**改写前**的名字（2026-09-08 实测）

临时 key `models=["kimi-k3"]` + `aliases={"kimi-k3":"sa-kimi-k3-responses"}`，
目标名**不在**白名单里，照样 200。所以「对外只暴露一个公开名、背后接私有组」的做法是：

- `models` 里**只放公开名**（`/v1/models` 直接返回白名单原文，用户在 Cursor 里就只看到它）
- `--alias 公开名=真实组` 做改写

目标名不必进白名单，放进去反而会把内部名暴露给用户。

## 脚本

`scripts/litellm-198-key-allowlist.py`（单测 `backend/tests/test_litellm_198_key_allowlist.py`，13 个）。
在 198 本机跑（`127.0.0.1:30402` 是 litellm-product 的 NodePort）：

```bash
# 投递 —— ⚠️ 文件名带上会话号，198 上多会话并行，同名路径会互相覆盖
scp scripts/litellm-198-key-allowlist.py cltx@10.68.13.198:~/litellm-198-key-allowlist-<sid>.py
# 用之前 sha256sum 对一遍，确认跑的是自己那份

# 198 上
export LITELLM_MASTER_KEY=$(sudo kubectl -n litellm-product get secret litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

S=~/litellm-198-key-allowlist-<sid>.py
python3 $S --add-model <M>                                     # ① 默认 dry-run
python3 $S --add-model <M> --limit 1 --apply --backup ~/<M>-canary-$(date +%Y%m%dT%H%M%S).json
python3 $S --add-model <M>          --apply --backup ~/<M>-full-$(date +%Y%m%dT%H%M%S).json
python3 $S --restore ~/<M>-full-<ts>.json --apply               # ④ 回滚(models+aliases 一起)
```

开关：`--add-model` / `--rm-model` / `--alias src=dst` / `--rm-alias src`（都可重复）、
`--prefix carher-`（换范围，默认 `cursor-` + `claude-`）、`--only <alias>`、`--limit N`、
`--include-blocked`（**撤权限时必须带**，见下面「blocked key」一节）。
`--model` 是 `--add-model` 的旧拼法，配 `--remove` 等于 `--rm-model`。

验收另有一支尺子，**不要手搓 SQL**（手搓极易漏掉 blocked，见下）：

```bash
scp scripts/litellm-198-key-allowlist-verify.sh cltx@10.68.13.198:~/kav-<sid>.sh
ssh cltx@10.68.13.198 'bash ~/kav-<sid>.sh claude-deepseek-v4-pro deepseek-v4-pro'
```

**改名/收敛要在一条命令里同时删旧、加新、写 alias**，它们会合成**一次** `/key/update`：

```bash
python3 $S --prefix cursor- \
  --rm-model sa-kimi-k3 --rm-model sa-kimi-k3-responses \
  --add-model kimi-k3 --alias kimi-k3=sa-kimi-k3-responses --apply --backup ...
```

分两次写会留一个窗口：旧名已删、新名还没到，那段时间用户无模型可用。

## 五条不许违反的纪律

1. **`/key/update` 是整字段替换，不是 merge**——`models` 和 `aliases` 都是。必须读回当前值
   再拼回去。直接 `{"models":["新模型"]}` = 把那把 key 其它几十个模型全部抹掉；
   直接 `{"aliases":{"a":"b"}}` = 把那把 key 其余映射全清空（198 上 cursor 平均 9 条 alias）。
   ⚠️ 但**没送的字段不会被动**——只送 `models` 不会清掉 `aliases`（1256 把实测）。
2. **`models == []` 表示「不限制、全部放行」，不许写 `models`。** 给这种 key 写白名单是**收窄权限**。
   脚本 `plan_key` 对空列表恒不产出 `models` 补丁。alias 可以照写（映射不收窄任何东西）。
   198 上就 4 把：`claude-code-canary-bn-{full,quota}-{1787146596,1787148171}`
   （08-19 建、`created_by=default_user_id`、`expires` 空=永不过期、近 30 天 0 请求）。
   ⚠️ **撤模型时这 4 把摘不掉**——它们没有显式白名单可减，所以任何「已全部撤销」的结论
   都要把它们单列出来讲，别报成 0 残留。
3. **金丝雀先行。** `--limit 1` 打一把、验通了再全量。全量 1256 把耗时约 1 分 13 秒。
4. **必须留快照。** `--apply` 强制 `--backup`；连续 2 次写失败自动中止。
5. **跑到收敛，别信单轮的 `applied_ok`。** 同一条命令重复跑，直到 `planned=0` 且 psql 复核为准。
   见下一节。

## 并发写会静默吃掉你的改动（2026-09-08 实测）

198 是**多 Claude 会话并行作业**的机器。两个会话同时对同一批 key 做 read-merge-write 时，
后写的那个用的是自己开跑前读到的快照，会把先写的成果**整片压回去**——而且
两边的 `/key/update` 都返回 200，`applied_ok` 全绿。

当天实测：cursor 632 把写入 `applied_ok=632/632`，几分钟后 psql 查库只剩 **115** 把生效，
518 把回到旧名字；对方的改动完好无损，损伤是单向的。

**识别形状**（`applied_ok` 高 + psql 数远低于它 + 数字随时间往回掉）时的三段式取证：

| 假设 | 证伪条件 | 怎么取数 |
|---|---|---|
| 我的写失败了 | `applied_ok` 应该有 fail | 看脚本输出 |
| 被流量回写覆盖 | 有流量组和无流量组的保住率应有差异 | 关联 30min 内 `LiteLLM_SpendLogs.api_key`，两组比例一样就证伪 |
| **被并发会话覆盖** | `/home/cltx` 下应有别人新投的脚本；`updated_at` 应密集落在我的写入窗口 | `ls -lt ~/*.py`、按分钟聚合 `updated_at` × 是否含新名字；再 `ListAgents` 找 busy 会话直接问 |

**处置**：先 `ListAgents` 联系对方确认它写完了，再重跑到收敛。**不要盲目重写**，会再撞一次。

### 开跑前的四步防撞（2026-09-09 实测跑通，成本约 2 分钟）

上面那张表是**事后取证**。撞了再查代价很大（要双向核损伤、还要重跑），所以写之前先走这四步：

1. **`ListAgents`** —— 有 busy 的会话就当作有并发写手，别赌。
2. **`ls -lt ~/*.py`** 看 198 上有没有别人新投的脚本，文件名和 mtime 就是对方的作业指纹。
3. **`updated_at` 直方图**——按分钟聚合最近 45 分钟，**并且带上形状列**：

   ```sql
   select date_trunc('minute',updated_at) mb, count(*) n,
          count(distinct cardinality(models)) distinct_len
   from "LiteLLM_VerificationToken" where updated_at > now()-interval '45 minutes'
   group by 1 order by 1 desc;
   ```

   **只看 `n` 会误判**：正常流量刷 spend 也会顶到 60~70/min。真正的判据是
   `distinct_len` —— 批量写是**同一形状**（少数几个长度），流量是**参差**（十几个长度）。
   09-09 我看到 71/min 一度以为有人在写，`distinct_len=12` 证伪了它，是流量。
4. **`SendMessage` 直接问**对方三件事：还在写吗 / 何时收敛 / **动的是哪些字段和模型名**。
   第三问最关键——如果和你要动的名字零交集，两边可以并行，不必排队。

**收尾必须反向核对方的字段**：跑完拿对方告诉你的模型名去 psql 数一遍，确认它的成果
还在（09-09 我核了对方的 `gemini-3.8-flash` / `grok-4.6` 各 633/633）。
只核自己那半边 = 只证明了「我没被覆盖」，没证明「我没覆盖别人」。损伤是单向的，
所以两个方向都要查。

脚本已按 token 去重（`/key/list` 分页在写入期间理论上可能让行在页间跳，造成同一把写两次、
另一把一次没写）。⚠️ 但**在无并发写的窗口里两轮实测重复数为 0**，一轮就 518/518 全中——
所以「分页丢行」这个机制在我手上**没有证据**，别拿它当默认解释。要坐实它，判据是：
在确认没有任何并发写的窗口跑一次全量，仍出现重复 token 或 psql 数低于 `planned`。

## 验收：正反双对照 + 换尺子

脚本自带的 readback 用的是**同一个 API**，不能自证。两件事必须另外做：

**① 端到端双对照。** 用户 key 在库里是哈希存的（`token` 字段就是哈希），
**拿它当 Bearer 认证必然 401**——那是打错凭据不是灰度坏了。明文拿不到，
所以造一把**和目标 key 同形状**的临时 key 来验：

```bash
MK=$(sudo kubectl -n litellm-product get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
NK=$(curl -s -X POST http://127.0.0.1:30402/key/generate -H "Authorization: Bearer $MK" \
  -H 'Content-Type: application/json' \
  -d '{"key_alias":"tmp-probe","models":["<公开名>"],"aliases":{"<公开名>":"<真实组>"},"duration":"20m"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')

# 阳性：白名单内 → 应正常回 nonce（客户端会用的每个面都要打：chat / responses / messages）
# 阴性：白名单外（比如刚摘掉的旧名字）→ 必须 403 key_model_access_denied
# /v1/models：应当只看得到公开名，看不到真实组
curl -s -X POST http://127.0.0.1:30402/key/delete -H "Authorization: Bearer $MK" \
  -H 'Content-Type: application/json' -d "{\"keys\":[\"$NK\"]}"
```

只做阳性 = 合成绿。阴性那一枪证明的是「白名单闸门是活的」。
⚠️ 解析 `/v1/messages` 的返回时 `content[0]` 可能是 `thinking` 块（没有 `text` 字段），
文本在 `content[1]`——按 `content[0]["text"]` 取会得到 `None`，那是提取写错不是模型没回。

**② 换尺子查库**（psql 双引号会被当标识符，必须写成 `.sql` 文件再 `kubectl cp`；
角色是 `-U litellm -d litellm`，不是 `llmproxy`）：

```sql
select case when key_alias like 'cursor-%' then 'cursor' else 'claude' end fam,
       count(*) filter (where '<新名>' = ANY(models))      as has_new,
       count(*) filter (where '<旧名>' = ANY(models))      as still_old,
       count(*) filter (where aliases ? '<新名>')          as has_alias,
       count(*) filter (where cardinality(models)=0)       as unrestricted,
       count(*) as total
from "LiteLLM_VerificationToken"
where (key_alias like 'cursor-%' or key_alias like 'claude-%')
  and (blocked is null or blocked=false) group by 1;

-- 误伤：范围外的 key 不该出现这个模型
select count(*) from "LiteLLM_VerificationToken"
 where '<新名>' = ANY(models) and key_alias not like 'cursor-%' and key_alias not like 'claude-%';

-- 既有 alias 有没有被整片抹掉：改动前后 avg(jsonb_object_keys 数) 应只 +1
-- 无关模型有没有被殃及：随手挑一个（如 kimi-k2.7-code）比对改动前后计数
```

还要看**白名单长度分布**：正常应该是整体平移（+1/-1）。出现别的长度 = 有 key 被写坏了。

## blocked key：加模型时跳过，**撤模型时必须带上**

脚本默认跳过 `blocked=true` 的 key（198 上 38 把：19 把 `cursor-*` + 19 把 `claude-code-*`，
同一批人）。铺新模型时这是对的——它们连认证都过不了，写了是噪音。

**但撤模型时默认值是错的。** blocked 只挡认证，不清白名单：那 38 把 key 的 `models` 原封不动
留着你刚撤掉的模型，将来谁把人解封，权限就跟着静默复活。所以撤权限要显式加
`--include-blocked`：

```bash
python3 $S --prefix cursor- --prefix claude- --include-blocked \
  --rm-model <M> --rm-alias <M> --apply --backup ...
```

输出会打 `include_blocked=` 和 `blocked_in_scope=` 两个数，用来确认范围真的扩到了。
不带这个 flag 时行为与旧版完全一致。

判「撤干净了」的 psql 判据**不能过滤 blocked**，否则 38 把残留正好落在你的尺子外面：

```sql
-- 对的：全库扫，一行都不该回
select m, count(*) from "LiteLLM_VerificationToken" t, unnest(t.models) m
where m = '<撤掉的名字>' group by 1;
```

## 198 现状基线（2026-09-09 收盘实测，用来判漂移）

| 家族 | 未 blocked | blocked | `models=[]` 全放行 | 平均 models | 平均 alias |
|---|---|---|---|---|---|
| `cursor-*` | 633 | 19 | 0 | 50.9 | 9.8 |
| `claude-*`（含 `claude-code-*`）| 629 | 19 | 4 | 22.7 | 17.1 |
| `carher-*` | 227 | 0 | 0 | 34.6 | 5.1 |

`scoped_keys`：默认 1262（cursor+claude 未 blocked）· 加 `--carher-` 1489 ·
再加 `--include-blocked` **1527**。三个数对不上就是范围点错了。

全量耗时约 1 分 13 秒（1452 把）。

**2026-09-10 增量**：给 `gpt-image-2` 铺白名单，终态 `cursor-* 634/634`（当天只差 1 把）·
`carher-* 227/227`。全库 1853 活跃 = 1486 有权限 + 285 `models=[]` 全放行 +
**82 把故意不给**（`wa-*` 44 / `tmpreg-*` 10 / `probe-*` / `diag-*` 等内部探针）。
链路上下文见 skill `codex-oneclick-rollout`。

⚠️ 平均值是**判写坏的尺子**，改动前后应当整体平移 ±1；出现别的位移 = 有 key 被写坏。
09-09 撤 4 个 deepseek pro 名字后 cursor 51.94→50.89（-1.05）、claude 23.61→22.65（-0.96），
正好等于人均摘掉的模型数，这才叫干净。


## 命名惯例

同一个后端常有多个前缀名，**Cursor 和 Claude Code 发的名字不一样**，铺的时候别只铺一个：

- 裸名 `grok-4.6` / cursor 系 `cursor-grok-4.6` / claude 系 `claude-grok-4.6` / sub2api 系 `sa-grok-4.6`
- 若客户端可能发多个名字，要么全部加进白名单，要么用 per-key `aliases` 把它们映射到同一组
  （见 `litellm-key-provider-swap` 的「双 alias 全量同步」——只改裸名漏掉前缀名，日志会显示
  已改、实际仍走旧上游）。
- **同名不同后端要当心**：`claude-kimi-k3` 走 kimi-proxy、`cursor-kimi-k3` 走 cursor-agents-shim、
  `sa-kimi-k3*` 走 sub2api，名字像但是三条独立的路。摘名字前先查它的 `litellm_params.model`
  和 `api_base`，别把「统一命名」做成「把用户从一条能用的路挪到另一条」。

### 白名单里有这个名字 ≠ 它能用

白名单只管**准入**，解析是另一道。一个名字要真能调通，得满足其一：
它是 `LiteLLM_ProxyModelTable` 里的真实组、或有全局 `model_group_alias`、或有 per-key `aliases`。
**三样都没有 = 过了准入闸门然后 400。**

09-09 实测（两把同形状临时 key 打真流量）：

| key | 结果 |
|---|---|
| `models=["grok-4.6"]`，**无 alias** | **400** |
| `models=["grok-4.6"]` + `aliases={"grok-4.6":"sa-grok-4.6"}` | **200**，原样回 nonce |

裸名 `grok-4.6` 不在 `ProxyModelTable`（真实组是 `cursor-grok-4.6` / `sa-grok-4.6`），
也没有全局 alias，`/v1/models` 拿 master key 查根本列不出它。
成因是路 A `grok-proxy` 09-08 被删，名字留在了 1200+ 把 key 的白名单里。

⇒ **清点「谁能用 X」时，只数 `models` 会高估。** 判据是
`count(*) filter (where 'X' = ANY(models) and (aliases ? 'X' or X 是真实组))`。
198 上现存这种半截状态：**19 把 blocked `cursor-*` 有 `grok-4.6` 无 alias**
（用户 09-09 裁决保持现状，解封前要记得补 alias 否则打了就 400）。


## 铺之前先算一道除法

给 1200+ 把 key 开一个模型之前，先看它背后的额度是**独享**还是**共享一个上游账号**。

2026-09-08 `sa-kimi-k3` 就是反面教材：背后是**一个** Kimi Allegro 会员，
**100 次/5h 且 100 次/周**，1258 把 key 共用——全公司一周总共 100 次。
铺是照做了（用户拍板），但必须同时讲清它只能是「尝鲜/低频备选」，
且**绝不进 fallback 链**。详见 memory `project_kimi_allegro_sub2api_198_2026_09_08`。

## 相关

- `litellm-key-provider-swap` —— per-key alias 改道 / 单人切供应商
- `her-public-model-name` —— 同一支脚本打**阿里云**（`LITELLM_BASE` 指哪打哪）给 420 把
  `carher-*` 铺对外名的特化流程：目标组要先在阿里云存在（跨集群搬组名会 400）、
  两个 proxy pod 的 key 元数据缓存差约 2 分钟、`scripts/litellm-key-drift-verify.py`
  把「我的没落地」和「别人的被我压了」分开报。
- `sub2api-grok-ops` —— sa-* 系列 entry 的后端（sub2api）运维
- 历史一次性脚本 `scripts/litellm-cursor-add-astra.py` 是本脚本的前身；
  **白名单/alias 批量增删一律用本脚本**，别再复制新的一次性版本。
  ⚠️ 2026-09-10 我又犯了一次：先手搓 `/tmp/batch.py` 铺完 227 把 carher key，
  事后才发现本脚本早就支持 `--prefix carher- --add-model gpt-image-2`。
  手搓那版能跑，但**没有 `--backup` 快照、没有 `--restore`、没有 `--limit` 灰度**——
  真写坏了只能靠 DB 备份回滚。**动 key 之前先 `ls scripts/litellm-198-key-*`。**
- 跨机传脚本**必核 sha256 再执行**：09-10 `scp` 静默产出过同字节数（1925）但开头一片 `\x00`
  的坏文件。比指纹要用同一把尺子（mac `shasum -a 256` 与 linux `sha256sum` 输出格式不同，
  我因此先误判成"传坏了"），统一 `openssl dgst -sha256`；更稳的传法是 base64 管道。
