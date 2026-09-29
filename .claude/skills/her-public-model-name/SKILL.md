---
name: her-public-model-name
description: >-
  给**全部** her（阿里云 `carher-*` 420 把 / 198 `litellm-product` 的 `carher-*` 227 把）
  增加 / 改名 / **改映射** / 摘掉一个**对外模型名**，用 per-key `aliases` 把它映射到真实组
  （如 198 `her-pro`→`sa-grok-4.7` 兜底 deepseek-v4.1-flash、`her-flash`→`openrouter-deepseek-v4.1-flash`）。
  含 198 前缀扫描会误伤 hangzhou/测试 key ⇒ `--only-file` 精确名单、兜底挂在组上不在对外名上、
  `--clone-key` 探针（算术判据，grok-4.7 拒复述 nonce）+ SpendLogs 判落点。含目标组不存在时先建组
  （CM 外科式 splice + rollout + 逐 pod sha 门禁）、改名走加法（add→verify→cutover→remove）、
  两个 proxy pod 的 key 元数据缓存差约 2 分钟、回归矩阵（pod×key×模型 + 阴性对照）、
  快照 drift 复核。Use when 用户说"给所有 her 的 key 加一个模型名 X 映射到 Y"/
  "把 X 改名成 Z"/"把 X 从所有 her 上摘掉"/"her 上暴露一个对外名指向某个组"。
  ⚠️ 名字加上了 ≠ her 下拉菜单里看得到（那要改实例 openclaw.json，见末节）。
---

# 给全部 her 加一个对外模型名

## 三个必须先分清的对象

| 概念 | 存在哪 | 作用 |
|---|---|---|
| **对外名**（`her-pro`） | key 的 `models` + `aliases` 的**键** | 用户/客户端发出去的字符串 |
| **真实组**（`grok-4.6`） | 阿里云 `cm/litellm-config` 的 `model_name` | 路由器能解析的名字 |
| **上游名**（`custom_openai/sa-grok-4.6`） | 那条 entry 的 `litellm_params.model` | 落点，**不是**组名 |

⛔ **上游名不能当 alias 目标。** 2026-09-17 用户要求映射到 `sa-grok-4.6`——
阿里云根本没有这个组（`LiteLLM_ProxyModelTable` 18 行 0 命中、CM 167 个 `model_name` 也没有），
它只是 `grok-4.6` 那条 entry 的 `litellm_params.model`。写字面 `sa-grok-4.6`
会**过了准入闸门然后 400**（[[feedback_allowlist_name_without_alias_is_400]]）。
组名在 198 存在不代表在阿里云存在——**每次都去目标集群查一遍**，别跨集群搬名字。

判据（在 226 上）：

```bash
kubectl -n carher get cm litellm-config -o go-template='{{index .data "config.yaml"}}' \
  | grep -c 'model_name:'                       # 现有条目数，改动前后要能对上
kubectl -n carher get cm litellm-config -o go-template='{{index .data "config.yaml"}}' \
  | grep -n 'model_name: <候选目标>'            # 空 = 这个组不存在，要先走步骤 0
```

## 前置

her 的 key **只在阿里云**（[[feedback_her_litellm_keys_live_on_aliyun_not_198]]），198 上同名的
`carher-*` 是死账。堡垒机与隧道按 skill `k8s-via-bastion`：`scripts/jms ssh k8s-work-226`。

```bash
export LITELLM_MASTER_KEY=$(kubectl -n carher get secret litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
kubectl -n carher port-forward svc/litellm-proxy 14000:4000 &
export LITELLM_BASE=http://127.0.0.1:14000
```

provider 的凭据在 secret **`carher-env-keys`**（`OPENROUTER_API_KEY` / `WANGSU_*` …），
不在 `litellm-secrets`。psql 的角色是 **`litellm`**（不是 `llmproxy`），
字面量一律 `$$...$$` 美元引用（[[feedback_litellm_db_sql_via_direct_ssh_dollar_quoting]]）。

## 步骤 0：目标组不存在时先建组（只有这一步会碰 CM）

先拿 provider 凭据从 226 直打一发上游，确认 slug 真实存在——**前置探针**比事后猜 400 便宜得多。

```bash
scripts/litellm-aliyun-cm-add-model.py --ns carher \
  --model-name openrouter-deepseek-v4.1-flash \
  --upstream-model openrouter/deepseek/deepseek-v4.1-flash \
  --api-key-env OPENROUTER_API_KEY --model-id openrouter/deepseek-v4.1-flash \
  --price-in 3.0e-07 --price-out 1.2e-06 --price-cache-read 6.0e-09 \
  --max-input-tokens 1048576 --max-output-tokens 384000        # ① 默认 plan，只打印
# ② 真写 + 重启 + 等逐 pod sha 收敛
... --apply --rollout --backup-dir /root/<task>
```

脚本锁死的硬规则（都是踩过的）：

- ⛔ **禁 `kubectl apply` litellm-proxy 的 manifest**——会把别人未提交的编辑一起推上去。
  唯一合法路径是 读 live → splice → `kubectl patch cm --type=merge --patch-file`。
- 插入点是 `model_list` 之后**第一个顶层键**（`litellm_settings:`）之前；
  `new.replace(entry,"",1) == old` 是"没碰别的字节"的断言，条目数必须 +1。
- 定价**双写** `litellm_params` 和 `model_info`：计费只读前者
  （[[feedback_litellm_cost_fields_live_in_litellm_params]]）。OpenRouter 的 deepseek-v4.1-flash
  是**按 UTC 时段分档**（峰 $0.30/$1.20 每 M，谷价减半）⇒ 按**峰价**写死，宁多勿少。
- **收敛判据只有一个**：每个 ready pod 容器内 `/app/config.yaml` 的 sha256 == 新 CM 的 sha256，
  外加 `grep -c model_name:` 对上。`rollout status` 在这个 Deployment 上吐
  "exceeded its progress deadline" 是**正常输出**，既不是失败也不是成功。
  实测走完约 **16 分钟**，全程 ready≥1（零中断）。
- CM 里**禁出现含 `*` 的 `model_name`**（[[project_aliyun_pro198_access_group_2026_09_09]]）。

**没重启前新名字必然 400**——那不是 alias 写错。

## 步骤 1：写 key（对外名 + alias 一次写完）

用仓库脚本 `scripts/litellm-198-key-allowlist.py`（名字带 198，但 `LITELLM_BASE`
指哪就打哪；纪律全在 skill `litellm-198-key-allowlist`，此处只列 her 的特化）：

```bash
S=~/litellm-key-allowlist-<sid>.py       # ⚠️ 文件名带会话号，226 上多会话并行
python3 $S --prefix carher- --add-model her-flash \
   --alias her-flash=openrouter-deepseek-v4.1-flash                       # ① dry-run
python3 $S --prefix carher- --add-model her-flash --alias her-flash=... \
   --limit 1 --apply --backup /root/<task>/canary-$(date +%Y%m%dT%H%M%S).json   # ② 金丝雀
python3 $S --prefix carher- --add-model her-flash --alias her-flash=... \
   --apply --backup /root/<task>/full-$(date +%Y%m%dT%H%M%S).json               # ③ 全量
python3 $S --prefix carher- --add-model her-flash --alias her-flash=...          # ④ 跑到 planned=0
```

- **写之前先防撞**：`ListAgents` + `ls -lt ~/*.py` + `updated_at` 直方图带
  `count(distinct cardinality(models))`（并发 read-merge-write 会静默压回别人的成果，
  两边都返 200，见 [[feedback_concurrent_bulk_key_writes_silently_overwrite]]）。
  跑完要**反向核**对方动过的名字还在不在。
  ⛔ 但 `updated_at` **不能反过来用来给某次配置改动定日期**：LiteLLM 每次 spend 写回都会刷它，
  按分钟做直方图量到的是流量不是配置变更。防撞靠的是那个 `distinct cardinality`，不是时间本身。
- **判"我上轮的改动还在不在"只认 `token`**：key 会被同名重发（删了重建，`key_alias` 不变
  但 `token` 变），模型数看着"变回默认"时，"被覆盖"和"被重发"处置完全相反。对 `--backup`
  快照里的 `token` 比 live 行。见 [[feedback_same_key_alias_can_be_a_new_row]]。
- **改名走加法**：先 `--add-model 新名 --alias 新名=组` 验通，**再单独一轮**
  `--rm-model 旧名 --rm-alias 旧名`。合成一次写也行，但分两轮更好回滚；
  ⛔ 绝不能先删后加（中间那段用户无名字可用）。见 [[feedback_rename_is_add_verify_cutover_then_remove]]。
- `models == []` 是**全放行**，脚本恒不写它——那种 key 上不该期待看到新名字。
- **改映射（retarget，名字不变只换背后组）是静默换上游**：用户那边零报错、只是换了模型。
  ① 先确认新组在**本集群** `/model_group/info` 里；② 先回答「新组的兜底是不是用户要的」
  （见「已知终态」下面那段）；③ 金丝雀后必须看 SpendLogs 落点，不能只看 200。
  add 与 retarget 能合成一条命令：`--add-model X --alias X=新组` 对已有 X 的 key 只改 alias、
  对没有的 key 两样都加（`plan_key` 幂等）。

**198 的形状**（09-29 实跑）：

```bash
S=~/litellm-198-key-allowlist-<sid>.py; D=~/<task>-<sid>; mkdir -p $D   # 198 上
# 名单先导出到 $D/targets.txt（见「198 的三处与阿里云不同」第 3 条）
python3 $S --prefix carher- --only-file $D/targets.txt --add-model her-pro --alias her-pro=sa-grok-4.7
python3 $S ... --limit 1 --apply --backup $D/canary-$(date +%Y%m%dT%H%M%S).json
python3 $S ... --apply --backup $D/full-$(date +%Y%m%dT%H%M%S).json
python3 $S ...                                                     # 跑到 planned=0
```

## 步骤 2：验收（三把不同的尺子，缺一把都是合成绿）

**① 换尺子查库**（不能信脚本自己的 readback，同一个 API 不能自证）：

```sql
select count(*) total,
  count(*) filter (where $$her-flash$$ = ANY(models))            has_name,
  count(*) filter (where aliases ? $$her-flash$$)                has_alias,
  count(*) filter (where $$auto$$ = ANY(models))                 still_old
from "LiteLLM_VerificationToken" where key_alias like $$carher-%$$;
```

**② drift 复核**——只数"新名字有没有到"证明不了"别的字段没被写坏"：

```bash
scripts/litellm-key-drift-verify.py --snapshot /root/<task>/full-<ts>.json \
  --add-model her-flash --alias her-flash=openrouter-deepseek-v4.1-flash
```

它逐把断言 `live == snapshot | 本次改动`，把结果分成 **missing**（我的没落地 ⇒ 重跑到收敛）
和 **drift**（别人的被我压了 ⇒ **停手先找那个写手**）。两者含义不同，不许合并成一个"坏"计数。
`ok=0` 报 FAIL——空样本是失败不是通过。

**198 上加两个参数**，否则它红得毫无道理：`--ns litellm-product`（默认是阿里云的 `carher`，
不改会去读一个同名但内容不同的 ns），且它 shell 出去调的是**裸 `kubectl`**（不带 sudo、
不读你的 env override），198 上要先放一个 PATH shim：

```bash
mkdir -p ~/bin-0a && printf '#!/bin/sh\nexec sudo /usr/local/bin/kubectl "$@"\n' > ~/bin-0a/kubectl
chmod +x ~/bin-0a/kubectl && export PATH=~/bin-0a:$PATH
```

**③ 真流量矩阵**（唯一能证明"能用"的尺子）：

```bash
scripts/litellm-her-key-model-regress.sh --uids 425,1000 \
  --models her-pro,her-flash,deepseek-v4-flash --negative auto
```

它做的事，每一条都对应一次假红/假绿：

| 形状 | 为什么必须这样 |
|---|---|
| **两个 pod 都打** | 两个 proxy 各自缓存 key 元数据，写完后**另一个 pod 要约 2 分钟**才认。当场探到 403 **不是写失败**，等 2 分钟重探。 |
| 明文 key 从 `cm carher-<uid>-user-config` 的 `openclaw.json` 里取 | 库里 `token` 是哈希，拿它当 Bearer 必 401——打错凭据不是 alias 坏了 |
| ≥2 把 key | 一把 key 通了只证明那一把 |
| 判据是解析后的 `choices[0].message.content` 含 nonce | grep 整个 body 会把回显的 prompt 读成成功 |
| `max_tokens ≥ 256` | her-flash 是 reasoning 模型，给小了 `finish_reason=length` 且 content 空 |
| **阴性对照**（刚摘掉的旧名 / 没给的名字必须 403） | 全阳性分不清"alias 生效"和"这把 key 全放行" |
| 存量模型抽一个一起打 | 证明建组的 rollout 没改坏既有路由 |
| 顺带先比逐 pod config sha | 老 config 的 pod 会给出一个与 key 无关的 400 |

**既存故障别背在自己头上**：`carher-425` 对 `deepseek-v4-flash` 403 是改动前就有的——
判据是改动前那份快照里它 `n_models=31` 且不含该名字。没有改动前快照就没法分辨，
所以 `--backup` 是强制的。

**198 上用 `scripts/litellm-198-her-name-probe.py`**（上面那个 `.sh` 是阿里云两 pod 的形状）：

```bash
# 198 上；首选 --clone-key：克隆目标 key 的 models+aliases 起 20m 临时 key，finally 里删并回查
export LITELLM_MASTER_KEY=$(sudo kubectl -n litellm-product get secret litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
ARGS=$(sudo kubectl -n litellm-product get pods -l carher.net/litellm-production-route=enabled \
  -o jsonpath='{range .items[*]}--pod-ip {.status.podIP} {end}')
python3 litellm-198-her-name-probe.py --clone-key carher-75 $ARGS --model her-pro --model her-flash
# 手里有明文 key 时：--key '<明文>' 代替 --clone-key
```

`--pod-ip` / `--model` 都是 `append`；`--model` 默认 `her-pro her-flash`；`--negative` 默认
`her-nonexistent-control`。逐 pod IP 直打（**不打 VIP**），判据是解析后的
`choices[0].message.content`，每 pod 一发阴性对照必须 ≥400，末尾打印 `temp_key ... deleted=True`。

🔴 **默认判据是随机算术题（`--check arith`），不是「原样复述 nonce」。** 2026-09-29 grok-4.7
对 4 个 pod 的「Reply with exactly this token」**全部拒答**（"I can't comply with requests to
output an exact token"）——HTTP 200、落点对、nonce 判据读成 8/8 FAIL。旧形状保留在 `--check nonce`，
换了上游模型想用它，先拿一发看模型肯不肯复述。

🔴 **本探针不证落点**：198 的 `x-litellm-model-id` 是哈希（`4e698ca6f80f`），不是 `sa/grok-4.7`。
落点去 SpendLogs 按脚本打印的临时 key alias 查（`metadata->>'user_api_key_alias'`，看
`model_group` / `model` 两列；写入有延迟，隔 ~20s 再查）。**改映射（retarget）这一步必查**——
改前改后都是 200，只有落点能区分。

**首选克隆而非明文**：198 只存 `sha256(key)`，真 key 在 188 容器 env 里（见下），
而克隆出的临时 key 路由字段与目标 key 逐字相同，还顺带证明「这把 key 原有的其他名字还能打」
（再多给一个 `--model <它原有的一个名字>` 当回归对象）。

## 已知终态（用来判漂移）

**阿里云（ns `carher`）2026-09-17**：420 把 `carher-*`：`her-pro` → `grok-4.6`
（落点 `pro198/grok-4.6`）、`her-flash` → `openrouter-deepseek-v4.1-flash`
（落点 `openrouter/deepseek-v4.1-flash`）；中途叫过的 `auto` 已摘净。CM 168 条。
详见 [[project_aliyun_her_key_auto_alias_grok46_2026_09_17]]。

**198（ns `litellm-product`）**：09-20 给 227 把 `carher-*` 加了同样两个对外名；
**09-29 `her-pro` 改指 `sa-grok-4.7`**（224 把 retarget + 补 `carher-13/14/75`，共 227 把，
与 09-20 那 227 把不是同一集合）。**alias 目标是 198 的组名，不是阿里云那两个**：

| 对外名 | 阿里云 alias 目标 | **198 alias 目标** | 198 落点 | 198 兜底（DB router_settings） |
|---|---|---|---|---|
| `her-pro` | `grok-4.6` | **`sa-grok-4.7`**（09-29 前 `sa-grok-4.6`） | `openai/grok-4.7`，`model_info.id=sa/grok-4.7` | `openrouter-deepseek-v4.1-flash` → `deepseek-v4-flash-responses` |
| `her-flash` | `openrouter-deepseek-v4.1-flash` | 同名 | `openrouter/deepseek/deepseek-v4.1-flash` | — |

**兜底挂在组上，不在对外名上**：`her-pro` 走 per-key alias 改写成 `sa-grok-4.7` 后，
路由器按 `sa-grok-4.7` 查 fallbacks。所以「对外名 X 映射 A、兜底 B」= 先看 A 组的 DB fallback
是不是已经是 B（判生效只认 DB，见 [[feedback_db_router_settings_overlays_config_yaml]]）；
是就只写 key，不是才动兜底（skill `litellm-198-fallback-chain`）。09-29 那轮 `sa-grok-4.7`
的链首本来就是 deepseek-v4.1-flash ⇒ 零兜底改动、零重启。

09-29 备份 198 `~/herpro47-5e/{canary-20260929T153336,full-20260929T153611}.json`，
回滚：先 `--restore full-… --apply` 再 `--restore canary-… --apply`（金丝雀那 1 把只在 canary 里）。

⛔ **`grok-4.6` 这个裸名在 198 不是组**（`ProxyModelTable` 零命中，也无全局 alias）——
照抄阿里云的 alias 值会过了准入闸门然后 400。每次都查目标集群，别跨集群搬组名。
详见 [[project_198_her_pro_flash_keys_2026_09_20]]。

## 198 的三处与阿里云不同（做之前先对一遍）

1. **目标组两个都已存在 ⇒ 纯 key 写入，不碰 CM、不 rollout**。阿里云那轮要 splice CM +
   16 分钟 rollout 是因为它当时没有 OpenRouter deepseek 组；198 早有（09-16 建）。
   判据：`select model_name from "LiteLLM_ProxyModelTable" where model_name in (...)` 两行都回。
2. **要打的是每个在服务的 pod，不是 2 个**。198 生产流量走带路由标签的车道：`svc/litellm-proxy` 与
   NodePort 30402 的 selector 是 `carher.net/litellm-production-route=enabled`，09-24 起命中的是
   `litellm-proxy` 自己的 4 副本（09-24 前带标签的是 `litellm-proxy-gray`，车道搬过家）。**按标签取 pod，别按名字**：
   拿 `kubectl get pods -l carher.net/litellm-production-route=enabled -o custom-columns=...:.status.podIP`
   取 IP，逐 pod 直打（打 VIP 会负载均衡，掩盖是哪个 pod 不认）。
3. **范围不能再用 `--prefix carher-` 裸扫。** `aliyun-carher-*` 那 2 把桥 key 被 `startswith`
   天然排除（别手写 `%carher%` SQL 去核，会多捞 2 行），但 09-29 `carher-` 前缀已是 **237 把**，
   多出的 10 把不该写：`carher-hangzhou`（杭州 gw 上游凭据，[[reference_carher_hangzhou_key_is_the_hangzhou_gw_upstream_credential]]）
   + 09-28 的测试 key（`carher-14-probe-*` / `carher-iso-*` / `carher-slow8-*` / `carher-landing8-*`）。
   ⇒ **先 psql 导出精确名单再 `--only-file`**：

   ```sql
   -- 例：已有该名的 + 用户点名要补的
   select key_alias from "LiteLLM_VerificationToken" where key_alias like 'carher-%'
     and ('her-pro'=ANY(models) or key_alias in ('carher-13','carher-14','carher-75')) order by 1;
   ```
   写进 `targets.txt` 后 `--prefix carher- --only-file targets.txt`。脚本打 `only_requested=N only_matched=N`，
   **任一名字没匹配上（拼错 / blocked / 前缀外）直接拒跑**，不再静默少写一把。
   ⛔ 别拼几百个 `--only` 进 `ssh '…'`：远端 shell 重切分后 argparse 静默丢参。
   先 `grep -cE 'probe|iso|slow|landing|hangzhou' targets.txt` 必须为 0。

⚠️ 旧记忆说「198 的 `carher-*` key 无效不被使用」**已过时**：09-20 实测近 14 天有 8 把在
真实出活（`carher-75` 26768 行 / `carher-14` 5285 / `carher-13` 2822 / `carher-1` 1618 /
`carher-221` 733）。它们是 188 docker 实例和老杨那条线，不是 k8s her pod ⇒
**198 上动 `carher-*` key 有真实受害面**，不是刷死账。

**198 的真 key 在哪**（不在 k8s CM 里，别去 `carher-<uid>-user-config` 找，那是阿里云的形状）：
188 容器的 env，`docker inspect hermestest-<uid> --format '{{range .Config.Env}}{{println .}}{{end}}'`
里的 `CARHER_PROD_KEY`；`openclaw.runtime.json5` 里写的是 `${CARHER_PROD_KEY}` 占位符，
且那文件是 JSON5 带 `//` 注释，`json.load` 直接喂会 `PARSE_FAIL`。
端到端入口是桥 `https://cc.auto-link.com.cn/pro/v1`。

## 两件本 skill **不做**的事（收工时必须主动说）

1. **her 的下拉菜单里看不到这个名字。** key 能调 ≠ UI 能选：模型选单来自实例的
   `openclaw.json`（`cm carher-<uid>-user-config`，由 `backend/config_gen.py` 生成），
   要出现在菜单里得另改实例配置 + base-config，见 skill `add-litellm-model` 步骤 3a/3b。
   只有客户端**直接指名**才打得到。
2. **新建的 key 会漏这条 alias。** 全量写是一次性快照，
   `backend/litellm_ops.py::_BASE_MODELS` 不含这些对外名 ⇒ 以后新建的 her 拿不到
   （[[feedback_fleet_wide_key_alias_is_diluted_by_new_keys]]）。要么改 `_BASE_MODELS`，
   要么定期重跑本流程。

## 相关

- `add-litellm-model` —— 端到端接入新模型（含 base-config / operator 那两步和 `pro198` 组标签捷径）
- `litellm-198-key-allowlist` —— 批量 key 写入的全部纪律（并发、blocked、收敛）
- `litellm-key-provider-swap` —— 单把 key 改道
- `k8s-via-bastion` —— 226 隧道
