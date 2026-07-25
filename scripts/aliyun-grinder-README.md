# 全自动把 ChatGPT 号接入阿里云 ACK chatgpt 池（照做即可）

> 入口脚本：`scripts/aliyun-grinder.sh`（+ `scripts/aliyun-cm-add-acct.py`）
> 深度原理/踩坑史：skill `aliyun-chatgpt-acct-add`
> 198 池走另一条线：`scripts/add-acct-198/`（grinder.sh），**别混用**

## 0. 这套东西干什么

把飞书表里的号（邮箱+密码[+2FA密钥]）端到端接入**阿里云 ACK `carher` ns**：
OAuth 拿 token（188 跑浏览器）→ 建 PVC/Deployment/Service → auth.json 落 PVC
→ 双 ConfigMap（prod + canary）patch → rollout → smoke 200。

## 1. 前置检查

```bash
kubectl get ns carher                      # 隧道通不通(必须走 k8s-work-226/227)
ssh cltx@10.68.13.188 'df -h /Data'        # ≥20G 空闲, 满了 OAuth 必失败
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
- 编号前**必须去重**：飞书 `acct` 列为空 ≠ 没接入。先 dump 集群现有邮箱交叉核对：
  ```bash
  ssh cltx@10.68.13.188 'for d in /Data/chatgpt-auth/acct-*/; do n=$(basename $d); \
    e=$(grep -h "^email=" $d/.creds 2>/dev/null|cut -d= -f2-|tr -d "\047"); echo "$n $e"; done'
  ```

## 3. 跑

```bash
cd ~/codes/carher-admin
GRIND_ACCTS="122 123" GRIND_CREDS=/tmp/grind-creds-aliyun.csv \
  nohup bash scripts/aliyun-grinder.sh > /tmp/aliyun-grind.log 2>&1 &
tail -f /tmp/aliyun-grind.log     # 只看本地日志, 别穿隧道盯屏
```

**先跑 1 个号验证流程**，再批量。**严禁与 toggle 批处理并发**（见 §5）。

| 日志里看到 | 含义 |
|---|---|
| `settle 60s (让本次验证码先到)` → `refreshed=True; settle another 60s` | 取码 settle 规则生效 |
| `✅ OTP=xxxxxx (try 1/3)` | 一次取码成功 |
| `[totp] code=xxxxxx (window 1/3)` | 2FA 号本地算码生效 |
| `[4.5] push-auth detected` → `fell back to email OTP` | 手机批准页已退回邮箱验证码 |
| `✓ pod auth 有效 (access_len=1736)` | 空壳兜底通过 |
| `entry 数与基线一致` | 双 CM patch 校验通过 |
| `-> HTTP 200` + `GRINDER DONE` | 完事 |

## 4. 需要介入的情况

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

## 5. 铁律

1. **串行**：188 上只有一个浏览器出口。grinder 会按镜像名 `docker kill` 清残留容器，
   **与 toggle 批处理并发会互杀**，制造假失败（`password field did not appear`）。
2. **只看本地日志**，别穿 jms 隧道盯屏（隧道 2min 超时）。
3. **CSV 密码不去引号**：含 `& $ % # ! *` 等元字符，脚本已用单引号包裹写入 `.creds`。
4. **prod + canary 必须都 patch**：her 默认走 prod，只改 canary = 新号 idle 不进轮询。
5. **别硬编码 entry 数**：prod 7 组 / canary 4 组，脚本按同 CM 存量 acct 众数自适应。
