# 全自动把 ChatGPT 号接入阿里云 ACK chatgpt 池（照做即可）

> 入口脚本：`scripts/aliyun-grinder.sh`（+ `scripts/aliyun-cm-add-acct.py`）
> 深度原理/踩坑史：skill `aliyun-chatgpt-acct-add`
> 198 池走另一条线：`scripts/add-acct-198/`（grinder.sh），**别混用**

## 0. 这套东西干什么

把飞书表里的号（邮箱+密码[+2FA密钥]）端到端接入**阿里云 ACK `carher` ns**：
OAuth 拿 token（188 跑浏览器）→ 建 PVC/Deployment/Service → auth.json 落 PVC
→ 双 ConfigMap（prod + canary）patch → rollout → smoke 200。

## 1. 前置检查

**第 0 步永远是判通道**（决定后面走哪一列命令，2026-08-04 起本地隧道仍是死的）：

```bash
kubectl get ns carher            # 通 → A 路(本地 kubectl)
scripts/jms ssh k8s-work-226 'echo ok'          # 不带 --tty；挂住/No route to host = relay 死
scripts/jms ssh --tty k8s-work-226 'echo ok'    # 这条通 → B 路(在 226 上跑 kubectl)
```

| | A 路：本地 kubectl 通 | B 路：只有 `--tty` 通（现状） |
|---|---|---|
| 拿 token | `aliyun-eip-onboard.sh` | `aliyun-eip-onboard-via-jms.sh` + `aliyun-eip-onboard-watch.sh` |
| 建资源/落 auth/回归 | `aliyun-grinder.sh` | `push226.sh` + `aliyun-acct-finalize-on226.sh` |
| 入池（双 CM） | 同上（grinder 的 E 段） | `push226.sh` + `aliyun-acct-join-pool-on226.sh` |

`aliyun-grinder.sh` / `aliyun-batch-add-accts.sh` **硬依赖本地 kubectl**，B 路下用不了，
别在那儿反复重启 `jms proxy`（relay 坏死不是慢，重试是 reroll 同一个信号）。

```bash
ssh cltx@10.68.13.188 'df -h /Data'        # ≥20G 空闲, 满了 OAuth 必失败（仅 188 路径需要）
ssh cltx@10.68.13.188 'docker image inspect mcr.microsoft.com/playwright/python:v1.60.0-noble >/dev/null && echo ok'
```

`/Data` 满了就回收（**只删悬空镜像，安全**）：
```bash
ssh cltx@10.68.13.188 'docker image prune -f; docker builder prune -f'
```

## 2. 填 CSV（唯一手工步骤）

`/tmp/grind-creds-aliyun.csv`，每行一个号，**逗号分隔，顺序固定**：

```
编号,邮箱,邮箱密码,GPT密码[,2FA密钥]
```

- 第 5 列是飞书表「2FA密钥」（base32），**有就必须填**，否则该号卡在 authenticator 挑战页。
- 编号前**必须三处交叉核对**（飞书 `acct` 列为空 ≠ 没接入）：

  ```bash
  # ① 飞书账户表(唯一登记处) RP2dbYyyxa2lQqsTRbKckQdBn6d / tblpqA4qwCTvW4Nd
  lark-cli api POST "/open-apis/bitable/v1/apps/RP2dbYyyxa2lQqsTRbKckQdBn6d/tables/tblpqA4qwCTvW4Nd/records/search?page_size=500" \
    --as bot --data - <<< '{}' | jq -r '.data.items[].fields | "\(.acct[0].text) \(.邮箱[0].text)"'
  # ② 188 上的历史号
  ssh cltx@10.68.13.188 'for d in /Data/chatgpt-auth/acct-*/; do n=$(basename $d); \
    e=$(grep -h "^email=" $d/.creds 2>/dev/null|cut -d= -f2-|tr -d "\047"); echo "$n $e"; done'
  # ③ 阿里云集群上真实存在的编号(含 scale=0 的,所以列 deploy 不列 pod)
  scripts/jms ssh --tty k8s-work-226 'kubectl -n carher get pvc,svc,deploy -o name \
    | grep -oE "chatgpt-acct-[0-9]+" | sort -uV | tr "\n" " "'
  ```

  邮箱命中任一处就**别再分配新编号**（会造成同号两 pod 互刷 token 掉线）。

- **登记**要在接入前先写飞书表（`acct/邮箱/邮箱密码/GPT密码/2FA密钥/2FA接码地址/服务器=阿里云`）：
  `lark-cli` 的 `--data` **不吃绝对路径**，只能 `--data - < /tmp/x.json`；
  `2FA接码地址` 是 Url 类型，值要写 `{"text":"2fa.fun","link":"https://2fa.fun"}`。

## 3. 拿 token（首选）：阿里云 EIP 节点

188 出口已被 CF 限流（toggle 常撞"正在进行安全验证"）。改在**阿里云新加坡 EIP 节点**跑：

```bash
# toggle 与 oauth 分两步, 同一批号可并行(偶数号钉 .86 / 奇数号钉 .122)
./scripts/aliyun-eip-onboard.sh toggle <N> <email> <mail_pw> <gpt_pw> [totp]
./scripts/aliyun-eip-onboard.sh oauth  <N> <email> <mail_pw> <gpt_pw> [totp]

# 跟进
kubectl -n carher logs -f job/cgpt-onboard-toggle-<N>      # 认 RESULT=ENABLED
kubectl -n carher logs -f job/cgpt-onboard-oauth-<N>       # 认 access_token 出现
```

产物 `auth.json` 在 RWX PVC `chatgpt-onboard-work`，用临时 busybox pod 取回：

```bash
kubectl -n carher run authpull --restart=Never --image=busybox \
  --overrides='{"spec":{"nodeName":"ap-southeast-1.172.16.0.86","containers":[{"name":"c","image":"busybox","command":["sh","-c","sleep 300"],"volumeMounts":[{"name":"w","mountPath":"/work"}]}],"volumes":[{"name":"w","persistentVolumeClaim":{"claimName":"chatgpt-onboard-work"}}]}}'
kubectl -n carher wait --for=condition=Ready pod/authpull --timeout=90s
kubectl -n carher cp authpull:/work/auth-acct-<N>.json /tmp/auth-acct-<N>.json
```

**要点**
- 必须 `hostNetwork` + 钉 EIP 节点（脚本已内置）。普通 pod 走共享 NAT
  `47.84.112.136` = **线上 codex 出口**，撞 CF 会污染生产。
- **别 `pip install patchright`**：镜像自带 chromium 1.60.0，装 1.60.1 会找不到 chromium。
- 偶发 `TargetClosedError`（浏览器崩）= 同节点并行争抢，**单独重跑即过**。
- 2FA 号**完全不需要邮箱取码**（TOTP 直接过 `/mfa-challenge`）。
- 同节点别并两个 Job（偶数号 `.86` / 奇数号 `.122` 已分摊；同 parity 的号串行跑）。

### 3b. B 路（本地 kubectl 不通时）：kubectl 在 226 上跑

```bash
# 提交 Job(creds 从 CSV 读, 走 Secret 注入; 脚本会回查 Job/Secret 是否真建成)
bash scripts/aliyun-eip-onboard-via-jms.sh toggle <N> /tmp/grind-creds-aliyun.csv
bash scripts/aliyun-eip-onboard-watch.sh    <N> toggle 1200     # 认 RESULT=ENABLED
bash scripts/aliyun-eip-onboard-via-jms.sh oauth  <N> /tmp/grind-creds-aliyun.csv
bash scripts/aliyun-eip-onboard-watch.sh    <N> oauth  1500     # 自动取回 /tmp/auth-acct-<N>.json
```

⚠️ **改了 `chatgpt-enable-codex-toggle.py` / `chatgpt-litellm-oauth.py` 不会自动生效**：
via-jms 跑的是集群里 `SRC_CM`（默认 `cgpt-onboard-src-131`，2026-07-26 的快照）里的副本。
改完源码必须先重建 CM，再用新 CM 名跑：

```bash
bash scripts/aliyun-refresh-src-cm.sh            # 只推改过的 toggle.py + 回查 CM 内 sha
PUSH_OAUTH=1 bash scripts/aliyun-refresh-src-cm.sh   # 改过 oauth.py 才加(52KB/18 块, 链路塌陷时很慢)
SRC_CM=cgpt-onboard-src-<TAG> bash scripts/aliyun-eip-onboard-via-jms.sh toggle <N> <csv>
```

带下面那条 toggle 补丁的 CM 是 **`cgpt-onboard-src-a4df8c71tgl`**（2026-08-04 建并验证过）；
via-jms 的默认值仍是旧的 `-131`，**要新代码必须显式传 `SRC_CM=`**。

**toggle 报 `ENABLE_FAILED` 先别判死号**：看日志里 `matched Codex/device-code switch <idx>`
的序号和 `switches settled: <n>`。序号很小（2）且 `04b-security-bottom.txt` 只有 ~1.2KB
停在标题处 = 面板半渲染，句柄失效，**重跑即过**（2026-08-04 acct-132/134 实证；
对照组 133 渲染完时是 switch 5，一次过）。现在脚本内置**第二轮重开面板 + 重新定位**，
理论上不再需要人工重跑；真到第二轮还失败才考虑账号侧原因。

## 4. 入池

```bash
# 落阿里云池: auth.json 先放好, 跳过 OAuth 段
GRIND_ACCTS="<N...>" GRIND_CREDS=/tmp/grind-creds-aliyun.csv GRIND_SKIP_OAUTH=1 \
  nohup bash scripts/aliyun-grinder.sh > /tmp/aliyun-grind.log 2>&1 &

# 落 198 池: 先送 188(⚠️先清 root 属主残留), grinder 见 access_len>1000 会自动跳过 OAuth
jms ssh JSZX-AI-03 'docker run --rm -v /tmp:/t busybox rm -f /t/auth-acct-<N>.json'
cat /tmp/auth-acct-<N>.json | jms ssh JSZX-AI-03 \
  "cat > /tmp/auth-acct-<N>.json && cp /tmp/auth-acct-<N>.json /Data/chatgpt-auth/acct-<N>/auth.json"
GRIND_ACCTS="<N...>" GRIND_CREDS=/tmp/grind-creds.csv \
  nohup bash scripts/add-acct-198/grinder.sh > /tmp/grind-198.log 2>&1 &
```

### 4b. B 路：分两步——先回归，回归过了再入池

grinder 是"建资源 + 入池"一把梭；B 路拆成两个脚本，正好对上「先验号、验过再放流量」：

```bash
# ① 把三个脚本 + 各号 auth.json 推到 226(每次都 sha256 断言, 不符自动整段重传)
bash scripts/push226.sh scripts/aliyun-acct-finalize-on226.sh /tmp/aliyun-acct-finalize.sh
bash scripts/push226.sh scripts/aliyun-cm-add-acct.py         /tmp/aliyun-cm-add-acct.py
bash scripts/push226.sh scripts/aliyun-acct-join-pool-on226.sh /tmp/aliyun-acct-join-pool.sh
for N in <N...>; do bash scripts/push226.sh /tmp/auth-acct-$N.json /tmp/auth-acct-$N.json; done

# ② 建 PVC/Deploy/Svc → auth 落 PVC → rollout → pod 内 access_len 校验 → 直连流式 smoke
scripts/jms ssh --tty --timeout 900 k8s-work-226 'bash /tmp/aliyun-acct-finalize.sh <N...>'
#    认: 每号 "pod auth 有效 (access_len=1xxx)" + "SMOKE acct-N ready=true STATUS 200 ... HASCONTENT 1"

# ③ 回归全绿后才入池: 双 CM patch → rollout → 复核 CM/路由表 → 入口 smoke
scripts/jms ssh --tty --timeout 1200 k8s-work-226 'bash /tmp/aliyun-acct-join-pool.sh <N...>'
```

③ 的输出怎么读（2026-08-04 acct-132/133/134 实测基线）：

| 看到 | 含义 |
|---|---|
| canary 报 `no marker for chatgpt-gpt-5.6-{sol,terra,luna}` | **预期**：canary 只有 4 个组，prod 才有 7 个 |
| `acct-N: 7 entries (baseline 7)` / `4 entries (baseline 4)` | 双 CM 都对齐存量基线 |
| `rollout status deploy/litellm-proxy` → `timed out waiting for the condition` | **不一定是失败**：第二个副本启动慢。复查 `get deploy` 是否 2/2 + 两 pod 同属新 RS + 再跑一次 status；是就别回滚 |
| `router chatgpt-acct entries: {…: 11, …}` | 7 真 entry + 4 条 alias 镜像行，全员同值即正常 |
| 入口 smoke 8 发全落同一个 acct | `deployment_affinity` 预期行为，不是 LB 坏；各号可用性由 ② 的直连 smoke 证 |

**CM 是 `yaml.safe_dump` 整份重写的**，所以 apply 后要断言"除 `model_list` 外逐键相等"
（脚本已备份到节点 `/tmp/cmbak-<CM>-<时间戳>.yaml`，回滚就是 `create cm --from-file` 那份）。
当前两个 CM `comments=0 anchors=0`，重写无损；哪天 CM 里有注释/anchor 了要重新评估。

## 5. Fallback：188 直跑（仅 EIP 不可用时）


```bash
cd ~/codes/carher-admin
GRIND_ACCTS="122 123" GRIND_CREDS=/tmp/grind-creds-aliyun.csv \
  nohup bash scripts/aliyun-grinder.sh > /tmp/aliyun-grind.log 2>&1 &
tail -f /tmp/aliyun-grind.log     # 只看本地日志, 别穿隧道盯屏
```

**先跑 1 个号验证流程**，再批量。**严禁与 toggle 批处理并发**（见 §7）。

| 日志里看到 | 含义 |
|---|---|
| `settle 60s (让本次验证码先到)` → `refreshed=True; settle another 60s` | 取码 settle 规则生效 |
| `✅ OTP=xxxxxx (try 1/3)` | 一次取码成功 |
| `[totp] code=xxxxxx (window 1/3)` | 2FA 号本地算码生效 |
| `[4.5] push-auth detected` → `fell back to email OTP` | 手机批准页已退回邮箱验证码 |
| `✓ pod auth 有效 (access_len=1736)` | 空壳兜底通过 |
| `entry 数与基线一致` | 双 CM patch 校验通过 |
| `-> HTTP 200` + `GRINDER DONE` | 完事 |

## 6. 需要介入的情况

**A. consent 页报 `Enable device code authorization`** — 该号 Codex toggle 没开。
单独开（**必须等 grinder 停了再跑**）：
```bash
ssh cltx@10.68.13.188 'OAUTH_PROXY=socks5://10.68.13.236:17890 \
  bash /tmp/run-enable-codex-toggle-188.sh acct-N'
```
看到 `RESULT=ENABLED` 再回来重跑该号的 grinder。

**B. 某号一直失败** — 看远端归档日志（脚本每轮自动存 `.prev`，不会被下一轮冲掉）：
```bash
ssh cltx@10.68.13.188 'tail -40 /tmp/oauth-acct-N.log.prev'
ssh cltx@10.68.13.188 'ls -t /tmp/screenshots-acct-N.prev/'   # 先看截图再猜
```

**C. `ACCOUNT_DEACTIVATED`** — 账号级失效，重试无意义，脚本会自动 skip。

## 7. 铁律

1. **串行（仅 188 路径）**：188 只有一个浏览器出口。走阿里云 EIP 路径**可并行**（两节点分摊）。grinder 会按镜像名 `docker kill` 清残留容器，
   **与 toggle 批处理并发会互杀**，制造假失败（`password field did not appear`）。
2. **只看本地日志**，别穿 jms 隧道盯屏（隧道 2min 超时）。
3. **CSV 密码不去引号**：含 `& $ % # ! *` 等元字符，脚本已用单引号包裹写入 `.creds`。
4. **prod + canary 必须都 patch**：her 默认走 prod，只改 canary = 新号 idle 不进轮询。
5. **别拿「要落 198 的号」去试跑 aliyun-grinder**：它会在阿里云建 PVC/Deploy/Svc，
   造出该号在两个池都有资源的假象（我验证时踩过；好在带了 `GRIND_SKIP_CM=1`，
   两个 CM 没被污染、无流量流入，删掉 3 个资源即恢复）。
   干跑校验用 `GRIND_SKIP_CM=1` **且**挑一个本来就属于阿里云池的号。
6. **别硬编码 entry 数**：prod 7 组 / canary 4 组，脚本按同 CM 存量 acct 众数自适应。
