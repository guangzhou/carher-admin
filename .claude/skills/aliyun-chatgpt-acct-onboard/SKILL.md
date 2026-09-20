---
name: aliyun-chatgpt-acct-onboard
description: >-
  把新的 ChatGPT Pro 号端到端接进**阿里云 ACK carher ns 的 chatgpt 池**：EIP 节点腾盘 →
  OAuth 拿 auth.json → 建 PVC/Deployment/Service 灌回 → 续订 → 双 ConfigMap(prod+canary)
  入池 → 逐号验收。Use when 用户说"把 acct-N 加到阿里云/加到阿里云的 litellm/新号入池/
  重新认证后灌回"，or 提到 aliyun-eip-onboard.sh / aliyun-grinder.sh / aliyun-cm-add-acct.py /
  chatgpt-acct-verify-aliyun.sh / litellm-config-canary。续订那一段的细节在姊妹 skill
  chatgpt-sub-renew-eip(§0 判据铁律必读)。
---

# 阿里云 ChatGPT 号池：端到端接入

2026-09-09 acct-209~213 五个号全程跑通后沉淀。**本机 kubectl 能直连阿里云时走这条(A 路)**，
别走 jms 中转(那套要把 142KB 的 oauth.py 分块 base64 推过去，~5 次往返/号，抖动全从那来)。

## 顺序(不许换)

```
0 腾盘   ./scripts/aliyun-eip-node-reclaim.sh
1 认证   ./scripts/aliyun-eip-onboard.sh oauth <N> <email> <mail_pw> <gpw> [totp]
         ./scripts/aliyun-onboard-workspace.sh auth <N>        # → /tmp/auth-acct-<N>.json(带自检)
2 建实例 GRIND_ACCTS="<N>" GRIND_SKIP_OAUTH=1 GRIND_SKIP_CM=1 \
           GRIND_CREDS=/tmp/grind-creds-<N>.csv bash scripts/aliyun-grinder.sh
3 续订   ./scripts/aliyun-eip-onboard.sh renew <N> <email> <mail_pw> <gpw>
         → 判据只认 live will_renew，见 skill chatgpt-sub-renew-eip §0
4 入池   双 CM patch(见下) + 两个 proxy rollout
5 验收   ./scripts/chatgpt-acct-verify-aliyun.sh <N...>
6 清理   见「收尾」——别用宽选择器
```

## 各段要点

### 0 腾盘 —— 「号有问题」十有八九是节点盘不够

两个 CF-clean EIP 节点 nodefs 常年贴着 kubelet 硬驱逐阈值 **31646917660 字节** 飘。
判据**不能看 `DiskPressure` 条件**(滞后布尔量，acct-210 两次被驱逐时它都读 False)，要看
kubelet `stats/summary` 的 `nodefs.availableBytes` 减阈值。能回收的只有退出态容器 + journald
归档，合计 3~5G；`crictl rmi --prune` 实测 **0 字节**。只有 **.122** 有 ssh 资产(jms `dify`)
能真回收 ⇒ 首选落脚点。细节见 `aliyun-eip-node-reclaim.sh` 顶注。

### 0.5 凭据体检 —— 跑 Job 之前先做，别让配置错冒充账号故障

```bash
lark-cli sheets +csv-get --spreadsheet-token FRVJsbGsTh9kWNtp7uycrPSYnzc \
    --sheet-id 0MAGgd --range A1:H200 --format csv > /tmp/sheet-acct.csv
python3 scripts/chatgpt-creds-audit.py /tmp/grind-creds-<N>.csv --sheet-csv /tmp/sheet-acct.csv
```

2026-09-12 acct-173/174 连报数轮 `mail.com login failed`，一路被当"邮箱 flaky，重跑"。真因是
本地 .creds 里填的不是邮箱密码。三条硬规矩：

- ⛔ **别拿「mail_pw == gpt_pw 字面量相同」当判据，判别力是零**：实测 21 行全部两栏相同，
  其中 19 个号照常登进 mail.com。卖号商给一个通用密码是**正常形态**。
- ✅ 唯一有判别力的是**按邮箱**跟飞书表的「邮箱密码」列比对。
- ⚠️ **join key 只能是邮箱，不能是编号**：173/174 的邮箱在飞书记在 acct-163/164 名下。
  且这张表**不是全量**(覆盖 81~244 但 175~194 整段缺失)，查不到 ≠ 号有问题。
- ⚠️ `lark-cli sheets +csv-get` 尽管叫 csv，**吐的是 JSON**，真表在 `data.annotated_csv`
  且每行带 `[row=N] ` 前缀。直接喂 `csv.DictReader` 不报错，**静默读出零行** ⇒ 假结论
  "表里没这个号"。audit 脚本两种格式都吃。

### 1 认证

- 出口隔离铁律：`hostNetwork` + `nodeName` 钉 EIP 节点。普通 pod 走共享 NAT `47.84.112.136`
  —— 那是**线上 9 个 codex acct 的出口**，在普通 pod 里跑浏览器撞 CF 会污染生产出口 IP。
- `FORCE_OTP=1`：卖号商那**一个**密码常只对 mail.com 有效，走邮箱验证码更稳。
  ⚠️ `toggle.py` 和 `oauth.py` **都**认 `FORCE_OTP_LOGIN` —— 旧版只给 oauth 接了线，
  于是 toggle 明明能切 OTP 却照样去撞密码，白烧一封 OTP 后停在误导性的
  `RESULT=ERROR detail=OTP input not found`(其实是密码错)。
- **`advanced=False` 不能直接定性成 OTP 限流**：它同时可能是 OpenAI 已停用账号。
  两者日志完全同形，旧代码只有截图能分：续订图在 `/work/bill-ss-<N>/`，用
  `aliyun-onboard-workspace.sh ls <N> bill` / `png <N> <name> bill`；出现
  `error_code: account_deactivated` 就是死号，别重跑。oauth.py 已补页面探测，命中后日志会直接打
  `[BILLING] DEAD account_deactivated`，但前提是本轮 SRC_CM 真装了新版源码，先核 sha256。
- Job 跑完 `kubectl logs job/...` 就 `timed out waiting for the condition` 了。证据全在 RWX PVC
  `chatgpt-onboard-work` 上，用 `aliyun-onboard-workspace.sh log|ls|png|cat|auth` 取。
  **读取 pod 不要钉 EIP 节点**(它只读 NAS，钉上去只会被驱逐，然后 `cat` 会**成功返回一个空文件**)。
- `workspace.sh auth` 自带硬自检(JSON 可解析 + `access_token` ≥1000 字符)，不自检就会拿空壳去
  `kubectl cp`，最后在 pod 里表现为 `access_len=0` 的"半接入"。
- **走 jms 那条旧路(`reoauth-one-shot.par.sh`)时，`TRANSFER_CHECK_FAIL` / rc=4 基本不是账号失败。**
  2026-09-12 一轮 18 个号整列报这个，而远端 `RH_HASB64=1 / RH_DEACT=0 / access_len≈1686` ——
  **全部成功**，整列假红。真因是 **jms PTY 会丢中间块**(acct-178 只收到 2700/3024 字符)，
  而旧守卫只防"尾部粘 marker 残渣"。看到 rc=4 **先去看远端 `RH_HASB64`，别重烧一遍 OAuth**。
  已修：校验不过自动改走 `jms scp` 取远端明文 `/tmp/rh-auth-<N>.json`，终判只认本地 auth.json 自检。
  通用教训：**PTY 是不可靠的二进制通道**，凡经 jms 传文件/大载荷一律 `jms scp`，别用 `--tty` + 标记块。

### 2 建实例

`aliyun-grinder.sh` 的 C/D/F 段：建 PVC + Deployment + Service → auth.json 落 PVC → rollout →
pod 内 auth 校验 → 直连流式 smoke。`GRIND_SKIP_OAUTH=1` 跳过 188 那段(auth.json 已在本地)，
`GRIND_SKIP_CM=1` 把入池留到第 4 步单独做。

**镜像铁律**：新号镜像**不许写死**，从参照实例 `chatgpt-acct-226` 现读 digest —— 既保证走
ACR VPC 内网，又保证与 acct-82 那份字节一致(memory `feedback_all_acct_images_must_match_acct82_by_digest`)。
`aliyun-grinder.sh` / `aliyun-acct-finalize-on226.sh` / `aliyun-batch-add-accts.sh` 三份都已改成现读，
后两份 2026-09-09 才修掉写死的 `ghcr.io/berriai/litellm:v1.85.0|v1.90.2`。

### 3 续订

见 skill `chatgpt-sub-renew-eip`。一句话：`[BILLING-RESULT]` / `✅ RENEW ENABLED` 是**页面读数，
两个方向都会骗**，唯一判据是 `chatgpt-acct-verify-aliyun.sh` 的 RENEW 列(live `will_renew`)。

### 4 入池(prod + canary 双 ConfigMap)

```bash
kubectl -n carher get cm litellm-config        -o jsonpath='{.data.config\.yaml}' > /tmp/prod.yaml
kubectl -n carher get cm litellm-config-canary -o jsonpath='{.data.config\.yaml}' > /tmp/canary.yaml
python3 scripts/aliyun-cm-add-acct.py /tmp/prod.yaml   /tmp/prod.new.yaml   209 210 211 212 213
python3 scripts/aliyun-cm-add-acct.py /tmp/canary.yaml /tmp/canary.new.yaml 209 210 211 212 213
# ⚠ apply 前先对着**已有 acct** 数条数：每号该加几条是从现存 acct 推出来的，不许硬编码
```

- 2026-09-09 实测基线：`litellm-config` 每号 **12 条**、`litellm-config-canary` 每号 **6 条**。
  这个数字是**当时**从存量 acct 推出来的，会随模型组增减而变 —— 每次都要重新数，别抄。
- 改完 CM 必须两个 proxy 都 rollout。**litellm-proxy 禁 `kubectl apply` 改 Deployment**
  (只用 `set image`/`patch`；CM apply + `rollout restart` 是既定路径)。
- **滚动时新 pod 卡 Pending + `didn't have free ports for the requested pod ports` 是正常的**：
  proxy 用 hostPort，新 pod 要等旧 pod 走完 600s 排水。别 force-delete
  (memory `feedback_pod_deletiontimestamp_is_deadline_not_delete_time`)，等就是了；
  Deployment 报 `exceeded its progress deadline` 卡在 1/2 也是这个原因。
- 收尾用 live proxy 的 `/model/info` 数每号条数，别只看 CM(CM 对了不等于 proxy 加载了)。

### 5 验收

```bash
EXPECT_209=isabella.trantow@mail.com ./scripts/chatgpt-acct-verify-aliyun.sh 209 210 211 212 213
```

四件事逐号自证：**ident**(编号↔邮箱在飞书/188/PVC 三处都漂过，权威 = PVC 内 auth.json 的 id_token)、
**renew**(live `will_renew`)、**smoke**(流式且断言 chars>0 && response.completed)、
**image**(必须 == 参照 acct 的 digest)。`VERIFY_NO_SMOKE=1` 可省额度 —— 探针会花被测号的真实额度，
别拿它当 liveness 轮询。

### 6 收尾清理 —— **别用宽选择器**(2026-09-09 踩过删库级 footgun)

- ⛔ `kubectl -n carher delete job -l job-name` = **删掉整个 ns 的所有 Job**(`job-name` 标签每个 Job 都有)。
  按名字点删：`kubectl -n carher delete job cgpt-onboard-oauth-$N --ignore-not-found`。
- ⛔ `cgpt-*-creds-*` / `cgpt-onboard-src-*` 通配会扫掉历史所有编号，以及**别的会话**在用的
  非数字命名(如 `-topclick`/`-typekey`)。只删本轮的 N。
- ⛔ zsh 里一条 `rm -f a* b* c*`，**任一 glob 无匹配则整条不执行**(`no matches found`) ⇒
  "清理跑过了"是假的。分行删，删完 `ls` 复核。
- 该删的：本轮 Secret/CM/Job、`aliyun-onboard-workspace.sh rm`、本地
  `/tmp/grind-creds-*.csv` `/tmp/creds-*.txt` `/tmp/auth-acct-*.json` `/tmp/ss-*.png`。
- 198 侧同名 `chatgpt-acct-<N>` deployment 必须保持 **replicas=0**：两个 pod 共用一个 refresh_token
  会互相作废(refresh_token 每次刷新都轮换)。

## 相关 memory

`feedback_all_acct_images_must_match_acct82_by_digest` · `feedback_renew_status_judge_is_live_will_renew_not_token_or_banner` ·
`feedback_pod_deletiontimestamp_is_deadline_not_delete_time` · `feedback_helper_2devnull_hides_the_error_and_fakes_success` ·
`feedback_disk_authjson_token_is_stale_not_death_proof` · `topic_chatgpt_acct_pool_index` · `topic_litellm_ops_index`
