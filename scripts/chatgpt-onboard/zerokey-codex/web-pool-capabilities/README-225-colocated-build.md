# 225 co-located capture — 给 198 zerokey web 池加号 / 复活

**问题**:198 zerokey web 池(`zero-N` on 225,`pool=zerokey-web`)给号建 session 时,
"188 抓 / 225 serve" 跨节点有两个坑:
1. 188 出口 IP 被反复自动登录打热 → CF 登录页不放行,capture 卡等 email 框超时。
2. 抓/serve 跨出口 IP → 部分号 serve 报 `Sentinel 401 token_expired`(session/cf_clearance
   绑抓取出口 IP)。**实测:阿里云新加坡抓 → 225 serve 也 token_expired(跨地域更不容忍);
   同网络 188↔225 才容忍。**

**正解 = capture 直接在 225 同节点跑**(全新且与 serve 同出口 IP,根治两坑)。

## 一次性准备:capture 镜像导入 225 containerd
188 无 registry insecure config 推不了 198:5000;198→225 SSH 可用(standby 密码,
225 `sudo k3s ctr/kubectl` 免密)。流式导入(不落中间盘):
```bash
# 在 188 上:
docker save zerokey-capture:latest | gzip -c | \
  ssh cltx@10.68.13.198 "sshpass -p '<225pw>' ssh cltx@10.68.13.225 \
  'gunzip -c | sudo k3s ctr -n k8s.io images import -'"
# 落 225 containerd 名: docker.io/library/zerokey-capture:latest
```
另需 litellm-product ns 里有 `zerokey-capture-src` CM(patched cap.py):
```bash
kubectl -n litellm-product create configmap zerokey-capture-src \
  --from-file=cap.py=scripts/chatgpt-onboard/zerokey-codex/capture/zerokey-web-capture.py \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 每号建/复活:`build225.sh <N>`(在 198 跑,免密 sudo k3s kubectl)
前置:litellm-product ns 有 `zerokey-acct-<N>-creds`(MAIL_USER/MAIL_PW/CHATGPT_PW,
mail_pw=**邮箱登录密码**),同目录有 `gen-zero-deploy.sh`(生成 serve deploy)。
```bash
bash build225.sh <N>   # 建 capture Job(225 nodeName,非 hostNetwork,dnsConfig 1.1.1.1,
                       # 挂 CM 脚本+creds+hostPath /Data/zerokey-sessions/zero-N)
                       # → 等 Job Succeeded → apply serve deploy → scale 1 → 验 1/1 ready
```
capture 写 users.json 到 **serve 同一 hostPath**(同节点同出口)→ serve 直接读 → 无 token_expired。
建成后注册:`188:/Data/zkcaps/zk-register-one.py <N>`(mode=responses),再 `zk-backfill.py --apply`
补齐 5.4/5.6-sol/terra/luna + image-2 全组。

## capture 脚本关键修复(已并入 zerokey-web-capture.py)
- **TOTP/MFA**:`_totp_now()` + `handle_mfa_challenge()`(纯 stdlib,input.fill 清旧值)。
- **OTP code 框定位**:email-verification 页可能有 Email+Code 两个 input,
  `locator("input").first` 会命中 Email 框 → OTP 打错位置 → "code required"。
  已改为优先 `input[autocomplete='one-time-code']`/numeric,退回最后一个可见 input + fill。
- **QQ 邮箱**:脚本自动识别 `@qq.com` 走 imap_qq(imap.qq.com:993),但 mail_pw 需是
  **QQ IMAP 授权码**(非登录密码),否则 `OTP fetch failed`。

## 判活口诀
- capture 在**干净 225 IP** 都失败 = 账号 mail 死(account-level,别再刷,伤号)。
- 池内 pod 1/1 但**直连 5.5 空返** = 订阅失效(摘除)。
- **别从几个失败外推整批**(实测 78/79 在 66/73/74/76 全失败后仍成功复活)。

## 掉号(codex token 死)复活:re-OAuth
症状:chatgpt-acct-N codex/usage 401(非 403)、refresh-grant 401、7d 额度冻结(用不掉/
不重置)。auth.json 本地 expires_at/sub_until 看着正常也可能 token 被服务端踢。
若 `sub_until` 未过期 → 订阅活,可 re-oauth 救活:
```bash
# 阿里云新加坡 EIP 节点(干净 IP 过 CF),需阿里云隧道 + 正确凭证
bash scripts/aliyun-eip-onboard.sh oauth <N> <email> <mailbox_pw> <gpt_pw> [totp]
# → auth.json 落阿里云 PVC chatgpt-onboard-work → 取回 → kubectl cp 进 198
#   chatgpt-acct-N 的 /chatgpt-auth/auth.json(PVC chatgpt-acct-N-auth,可写)→ rollout restart
# 验证:refresh-grant 从 401→200 = 复活
```
凭证格式(飞书"账户"表 / 用户给):`email----gpt密码----邮箱密码[----TOTP]`。
