# 新增 ChatGPT 账户到 198 池 —— 上手手册（照做即可，不走弯路）

> 面向下次直接执行的操作手册。先读完这一页，再动手。
> 配套脚本：`grinder.sh`（同目录）。
> 本目录 `scripts/add-acct-198/` = 198 增容的**入口**（grinder + 本手册）。
> grinder 依赖的浏览器脚本（`chatgpt-litellm-oauth.py`、`chatgpt-enable-codex-toggle.py`、`re-oauth.sh`）
> 仍在 `scripts/chatgpt-onboard/`（被多处复用，未移动）；grinder 会自动引用，你不用管。
> 深度原理/踩坑史：skill `add-chatgpt-acct-198`（v2.1.2+）+ memory `project_patchright_fullauto_us_proxy_onboard_2026_07_21`。

> ## ⚠️ 先读这一段：拿 token 已不走 188
>
> 本手册第 3 节那条"一条命令"是**188 出口跑 OAuth**的老路径，188 已被 CF 限流很重。
> **当前首选**：在**阿里云新加坡 EIP 节点**跑 toggle + OAuth 拿到 `auth.json`，
> 再让 grinder 走"已有有效 token → 跳过 OAuth 直接 finalize"分支入池
> （见 skill `add-chatgpt-acct-198` §v2.5，2026-08-05 acct-140~144 实证 5/5 全自动）：
>
> ```bash
> CSV=/tmp/grind-creds-<批次>.csv        # ⚠️ 别用固定名, 见第 2 节
> for N in <批次>; do                    # ⚠️⚠️ 严格一个一个来: 并发上限是「每 EIP 节点 1 个」
>   SRC_CM=cgpt-onboard-src-a4df8c71tgl bash scripts/aliyun-eip-onboard-via-jms.sh toggle $N $CSV
>   SRC_CM=cgpt-onboard-src-a4df8c71tgl bash scripts/aliyun-eip-onboard-via-jms.sh oauth  $N $CSV
>   bash scripts/aliyun-eip-onboard-watch.sh $N oauth 60      # 取回 /tmp/auth-acct-$N.json
> done
> # auth.json 送 188 /tmp/auth-acct-<N>.json(先清 root 属主残留 + 核 sha), 再跑第 3 节的 grinder
> ```
>
> 本手册其余部分（CSV 格式、去重纪律、监控纪律、finalize/验收）**全部仍然有效**。

---

## 0. 这套东西是干什么的

把一批 ChatGPT 订阅号（飞书表里的邮箱+密码）**端到端全自动**接入 198 K3s `litellm-product` 生产池：
OAuth 拿 token → 写进 K8s PVC → 注册 6 个模型到 quota-rebalance → smoke 验证 200。

**一个脚本 + 一个 CSV 搞定。** 中途 CF 拦截、toggle 没开、pod 覆写空壳等坑，脚本都自动处理。

---

## 1. 前置检查（第一次用/换机器时确认，平时可跳过）

```bash
# jms 两个别名能连(这是全流程的通道)
jms ssh AIYJY-litellm "echo ok198"    # 198 kube host(跑 kubectl)
jms ssh JSZX-AI-03    "echo ok188"    # 188 docker host(跑 patchright 浏览器)

# 188 上 patchright 镜像在
jms ssh JSZX-AI-03 "docker image inspect mcr.microsoft.com/playwright/python:v1.60.0-noble >/dev/null && echo image-ok"

# 188 上 re-oauth.sh 在
jms ssh JSZX-AI-03 "ls -l /Data/chatgpt-auth/re-oauth.sh"
```

三条都 ok 就能开跑。任何一条不 ok → 先解决通道/镜像，别硬跑。

---

## 2. 准备 CSV（唯一要手动填的东西）

新建 `/tmp/grind-creds.csv`，**每行一个号，4 列逗号分隔**，顺序固定：

```
账号编号,邮箱,邮箱密码,GPT密码
```

例：

```
100,SomeUser@mail.com,mailpw123,gptpw456
101,AnotherUser@mail.com,mailpw789,gptpwABC
# 这行以 # 开头会被忽略
```

**注意事项：**
- 编号接着现有最大号往后排（现在已到 99，下一个从 100 起）。别和已存在的号冲突。
- **编号前必须去重**：飞书表 `acct` 字段为空 **≠ 该邮箱没接入**——很可能接入过但没回写表。直接按"空=新号"分配会造 dup（同账号两 pod 互相轮换刷 token 掉线）。**分配前先 dump 集群现有邮箱交叉核对**：
  ```bash
  jms ssh JSZX-AI-03 "for d in /Data/chatgpt-auth/acct-*/; do n=\$(basename \$d); e=\$(grep -h '^email=' \$d/.creds 2>/dev/null|cut -d= -f2-); echo \$n \$e; done" | sort -t- -k2 -n
  ```
  待接入邮箱若已在列表里 → 跳过接入，只把已有编号回写飞书表即可。判 dup 用 pod 内 auth.json 的 `account_id`（不是编号）。
- 顺序**不能错**：第 3 列是邮箱密码，第 4 列是 GPT 密码。填反了登录必失败。
- ⚠️ **别用固定文件名 `/tmp/grind-creds.csv`**：上一批的号还留在里面，`GRIND_ACCTS` 写漏一个
  就会把旧号重跑一遍（`/tmp` 固定名读到陈旧文件，见 memory
  `feedback_tmp_fixed_path_runs_stale_foreign_file`）。每批用 `/tmp/grind-creds-<批次>.csv`，
  写完先 `awk` 打一遍确认行数和字段。用完即删（里面是明文密码）。
- 从飞书表复制出来自己整理成这 4 列。不确定就把表格内容发给我，我帮你转。

---

## 3. 一条命令跑起来

```bash
cd ~/codes/carher-admin
GRIND_ACCTS="100 101" GRIND_CREDS=/tmp/grind-creds.csv \
  nohup bash scripts/add-acct-198/grinder.sh > /tmp/grind-main.log 2>&1 &
```

- `GRIND_ACCTS="100 101"` = 只跑这两个号；**不写这个变量就跑 CSV 里所有号**。
- `nohup ... &` = 后台跑，串行处理，关终端也不断。
- 每号最多重试 10 次（3 个出口 IP 轮换过 CF）。

**建议：第一次先跑 1 个号**（`GRIND_ACCTS="100"`），确认流程顺，再批量。

---

## 4. 监控（铁律：只看本地日志文件，别自己穿 jms 隧道盯屏）

```bash
tail -f /tmp/grind-main.log
```

> ⚠️ **别用 `sleep + jms ssh` 循环去远端拉日志**。jms 隧道频繁 2 分钟超时，盯屏纯浪费时间。
> grinder 的成败信号全写在**本地** `/tmp/grind-main.log`。

看这几个关键信号：

| 日志里看到 | 含义 |
|-----------|------|
| `auth_valid=1` | 这个号 OAuth 成功，token 拿到了 |
| `[两步法] acct-N 疑似 Codex toggle off` | 自动去开 toggle 了（正常，别慌）|
| `✓ acct-N pod auth 有效 (access_len=1745)` | 空壳兜底校验通过，PVC 里 token 是好的 |
| `⚠ acct-N pod auth 被覆写成空壳 ... 重写 PVC` | 踩到 acct-98 那个坑，脚本自动修（正常）|
| `resume: 6` | 6 个模型注册成功 |
| `chatgpt-acct-N-gpt-5.5 -> HTTP 200` | smoke 通过，真能用了 |
| `GRINDER DONE ok=100 101` | 全部完成，收工 |

---

## 5. 脚本自动处理的坑（你不用管）

| 坑 | 脚本自动做什么 |
|----|--------------|
| CF Turnstile 拦浏览器 | 3 个出口 IP 轮换（美×2 + 日×1）重试 |
| 密码+验证码号 Codex toggle 默认关 | 连续失败时自动先开 toggle 再继续 OAuth（两步法）|
| pod 首启把 auth.json 覆写成空壳 | scale=1 后校验，空壳自动重写 PVC 再重启（最多 2 轮）|
| 写 PVC（local-path node-bound）| hostPath busybox 中转写入 |
| 注册模型 + state HEALTHY + smoke | 全自动 |

---

## 6. 需要你介入的情况（只有这几种）

### A. 某号 `!!!! acct-N FAILED after 10`
10 次都没成功。去看远端最后一次卡在哪：

```bash
jms ssh JSZX-AI-03 "docker logs \$(docker ps -alq) 2>&1 | tail -40"
```

常见原因：
- **密码错**（CSV 填错，或飞书表里就是错的）→ 日志停在密码页反复。
- **邮箱收不到验证码** → 日志 `device OTP fetch failed` 反复。换个时间重试或查邮箱本身。
- **CF 一直拦** → 罕见，通常轮换能过；连续 10 次都被拦说明 IP 都被限了，等会儿再跑。

### B. 全跑完但某号 smoke 不是 200
先确认 pod 和 auth：

```bash
# 查 pod 内 auth.json 有没有真 token(access_len 应 >1000, 不是空壳)
jms ssh AIYJY-litellm "POD=\$(kubectl -n litellm-product get pod -l account=N -o jsonpath='{.items[0].metadata.name}'); kubectl -n litellm-product exec \$POD -- python3 -c \"import json;print('access_len',len(json.load(open('/chatgpt-auth/auth.json')).get('access_token','')))\""
```

- `access_len=0` → 空壳没被兜底住（极少），手动救：见下方"手动救空壳"。
- `access_len=1745` 但 smoke 非 200 → 可能 litellm 还没 reload，等 1-2 分钟；或直接问我。

### C. 拿不准根因
别自己瞎试。直接说 **"acct-N 卡了/报 X，帮我按三段式查"**，我来定位。

### D. 某号 smoke 500 或 400，但 OAuth 明明成功了（deploy 没建成）
症状：grinder finalize 段出现 `error: no objects passed to scale` + `deployments.apps "chatgpt-acct-N" not found`，却仍打了 `resume: 6`；final smoke 该号 HTTP **500**（过一会儿被 quota-rebalance 摘 entry 后变 **400** `Invalid model name`）。

根因：建 deploy 那步的 `kubectl apply` 撞了 jms 隧道瞬态抖动 → deploy/svc/pvc 没建成，但 OAuth 已拿到 token、router 也注册了（指向不存在的 svc）。**grinder v2.2 起已用 `japply`（重试+presence 校验）修掉，建不成会 skip 该号而非假成功**；老日志/老脚本仍可能中招。

修复（**不用重 OAuth**，188 上 token 还在且有效）：直接对该号重跑 grinder 即可——
```bash
GRIND_ACCTS="N" GRIND_CREDS=/tmp/grind-creds.csv nohup bash scripts/add-acct-198/grinder.sh >> /tmp/grind-main.log 2>&1 &
```
grinder 会重建 deploy（这次带重试）。若想省掉重 OAuth 用现成 token 手动补：建资源(japply) → 等 PVC 绑定 → scale=0 → hostPath busybox 写 auth 进 PVC → scale=1 → 校验 access_len>1000 → `resume_acct` 重注册 → reset state.json HEALTHY 清 probe 计数 → rollout litellm-proxy → smoke。

---

## 7. 手动救空壳（万一 #11 兜底没生效时的应急 SOP）

症状：quota 探针报 `ValueError: no access_token`（**这不是 401**，别被误导），pod 却 1/1 Running，pod 内 auth.json 只有 `{"device_code_requested_at":...}`。

原因：pod 首启抢在写 PVC 前，自己发 device_code 把好 token 覆写成空壳。

修复（188 中转文件若还有效，直接复用，不用重 OAuth）：

```bash
N=100   # 改成实际编号
# 1) 确认 188 中转 auth 还有效(access_len>1000, expires 未过期)
jms ssh JSZX-AI-03 "python3 -c \"import json,time;d=json.load(open('/Data/chatgpt-auth/acct-$N/auth.json'));print('access',len(d.get('access_token','')),'valid' if d.get('expires_at',0)>time.time() else 'EXPIRED')\""
# 2) 送到 198 + scale=0 停覆写者 + hostPath 写回 + scale=1
#    —— 这套就是 grinder finalize 的手法，嫌麻烦直接对这个号重跑 grinder 即可:
GRIND_ACCTS="$N" GRIND_CREDS=/tmp/grind-creds.csv nohup bash scripts/add-acct-198/grinder.sh > /tmp/grind-$N.log 2>&1 &
```

> 最省事：**对单个坏号直接重跑 grinder**，它 finalize 会重写 PVC + 兜底校验，一遍过。

---

## 8. 收尾核对（全批跑完后一次性确认）

```bash
# 所有号 1/1 Running
jms ssh AIYJY-litellm "for N in 100 101; do echo acct-\$N: \$(kubectl -n litellm-product get deploy chatgpt-acct-\$N -o jsonpath='{.status.readyReplicas}/{.spec.replicas}' 2>/dev/null); done"
```

看到全 `1/1` + 日志里全 `HTTP 200` + `GRINDER DONE` = 完事。

---

## 速查卡（TL;DR）

```bash
# 1. 填 CSV: 编号,邮箱,邮箱密码,GPT密码
vi /tmp/grind-creds.csv

# 2. 跑(先单个验证)
cd ~/codes/carher-admin
GRIND_ACCTS="100" GRIND_CREDS=/tmp/grind-creds.csv \
  nohup bash scripts/add-acct-198/grinder.sh > /tmp/grind-main.log 2>&1 &

# 3. 盯本地日志(别穿隧道)
tail -f /tmp/grind-main.log
#   看 auth_valid=1 / HTTP 200 / GRINDER DONE

# 4. 顺了就批量
GRIND_ACCTS="101 102 103" GRIND_CREDS=/tmp/grind-creds.csv \
  nohup bash scripts/add-acct-198/grinder.sh >> /tmp/grind-main.log 2>&1 &
```

**记住三条铁律：**
1. CSV 4 列顺序别填反（邮箱密码在前，GPT 密码在后）。
2. 监控只看本地 `/tmp/grind-main.log`，别自己穿 jms 隧道盯屏。
3. 探针报 `no access_token` 是**空壳覆写，不是 401**；单号重跑 grinder 即修复。
