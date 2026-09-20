---
name: chatgpt-pool-198-pause-resume
description: |
  198 LiteLLM(ns litellm-product)ChatGPT 号池的**批量暂停 / 单号回池**双向 runbook。
  用途:把一批 chatgpt-acct 停机 + 从循环池摘出(可逆,不碰 PVC/auth),或把暂停过的号装回池。
  含四段固定顺序(state 先写、arms 后删)及其理由、跨 cron 周期复查的强制步骤、
  回池三个必踩的坑(缺 imghandler 挂载伪装成镜像问题 / AUTO_SCALE_ON_PAUSE=0 让 scale 变 no-op /
  cron 读-改-写吞掉 state)、以及一串会骗人的量具(Succeeded 残留 pod、/model/delete 的 400、
  母 router key 打叶子)。判据全部来自 2026-09-12 生产实测(40 个号暂停 + 19 个号回池),不是推理。
---

# 198 号池:批量暂停 / 单号回池

```bash
# 暂停(批量)
P198='..' P188='..' scripts/chatgpt-pool-198-pause.sh          115 116 ...   # dry-run
P198='..' P188='..' scripts/chatgpt-pool-198-pause.sh --apply   115 116 ...
sleep 400
P188='..'           scripts/chatgpt-pool-198-pause.sh --verify  115 116 ...  # ← 不许省

# 回池(单号,串行,一号一跑)
P198='..' P188='..' scripts/chatgpt-pool-198-rejoin.sh <N> [/tmp/auth-acct-<N>.json]
```

两个方向都**只动「副本数 + router 成员关系 + governor 状态」**,PVC / auth.json / deployment
本体一概不碰 ⇒ 完全可逆。彻底删号是另一件事,走 skill `chatgpt-acct-audit-and-retire` §5 的五层。

## 「在池」是三个独立的层,缺一层就是假绿

| 层 | 判据 | 单独成立时意味着什么 |
|---|---|---|
| ① pod 活着 | `endpoints` 有 IP + 叶子流式出字 | **只证明 pod 活着**,不证明它在转 |
| ② 是 router 成员 | 母 router `/model/info` 里有它的 `model_info.id` | 这才叫"在轮转里" |
| ③ governor 认它健康 | 188 `state.json` 的 `tier/paused/manual_offline` | 这决定它**留不留得住** |

暂停要把三层全按下去,回池要把三层全抬起来。只做 ②(摘 arms)= governor 下一轮给你加回来;
只做 ③ = 号还在池里吃流量;只做 ①(scale=0)= router 照投,打到空 svc。

## 暂停:顺序不许换

```
①标 state(paused+manual_offline) → ②删 arms → ③逐 pod 收敛复核 → ④scale=0 → ⑤跨轮复查
```

**为什么 state 必须最先**:188 的 quota governor 每 5min 一轮,会把它认为 ONLINE 的号用
`/model/new` **加回 router**。先删 arms 再标 state,中间那段窗口它能把刚删的原样塞回去。

**⑤ 不是装饰,是这个体系的硬性质**:governor 是**读-改-写整份 state.json** —— 本轮开头把旧
state 读进内存,结尾整份写回。你在这中间写的东西会被**静默还原**。
2026-09-12 acct-176 在 12:56 写完 `HEALTHY`,被 12:55 启动那轮在 12:57 整份写回,13:25 复查已变回
`SCALED_DOWN/manual_offline`。⇒ **写完 state.json 必须跨 ≥6min 复查一次**,翻回去就补写再等一轮。

## 回池:三个必踩的坑

**① 缺 `chatgpt-images-handler` 挂载 ⇒ 一起来就 CrashLoop,而且长得像镜像问题。**
共享 CM `chatgpt-pool-config` 的 `custom_handler` 指向 `chatgpt_images.chatgpt_images_llm`,
那模块**不在镜像里**,是另一个 CM 以 subPath 挂成 `/app/chatgpt_images.py`。
handler 上线之前建的老 deploy(长期 0 副本所以从没暴露)全缺这个卷,scale=1 直接
`ImportError: Could not import chatgpt_images_llm from chatgpt_images`。
⚠️ 我第一反应是刚钉的 acct-82 digest 干的,**回退旧 tag 照样崩**才定位到卷 —— 这条证伪腿省不得。
判据 = 拿一个**正在跑的**号(如 acct-172)`-o jsonpath` 出 volumes+volumeMounts 逐项对。

**② `AUTO_SCALE_ON_PAUSE=0` 让 `resume_acct()` 的 scale=1 整段变 no-op,但它照样注册 arms。**
`quota-rebalance.py` 的 `scale_deploy()` 开头就 `if not AUTO_SCALE_ON_PAUSE: return True`,
而 188 的 `/home/cltx/.chatgpt-quota/env` 第 7 行正是 `AUTO_SCALE_ON_PAUSE=0`。
于是它以为"scale + 等 endpoint ready"成功了,在 **0 副本、endpoint 无 IP** 的情况下把 8 条 arms
写进 router,router 照样 shuffle 选中它。
⇒ **自己 scale=1 + 等 endpoint 真有 IP + 叶子流式跑通,再调 `resume_acct`。**

**③** 同上「⑤跨轮复查」。

## 会骗人的量具

- ⛔ **`Succeeded` 相的残留 pod 不是"排水中"**。2026-09-12 停完 40 个号后有 16 个 pod 还在,
  查出来全是 8-25 / 9-06 / 9-08 历史 scale-down 留下的 `Succeeded` 对象,`ready=false`、
  无 `deletionTimestamp`,与本轮无关。**判"停干净了"只数 `--field-selector=status.phase=Running`。**
  (真在排水的那种 `deletionTimestamp` 是截止时刻不是删除时刻,容器 Running = 正常,禁 force-delete。)
- ⛔ **正则圈号会多圈**:`1[3-5][0-9]` 把不在名单里的 132/133/134 也圈进来了 ⇒ 先看清楚是谁再下结论。
- ⛔ **`/model/delete` 返回 400 也可能已经成功**,判据永远是 readback 不是状态码。
- ⛔ **归因只能按 unique `model_info.id`**:`/model/info` 是 alias 展开的,按行数算"每号几条"必错。
  实测每号 **8 条** arms(`CHATGPT_MODELS + _56 + _REVIEW`)。
- ⛔ **收敛必须逐 proxy pod 复核**,4 个副本各自持有 router 内存态,只打 NodePort 会漏分歧。
- ⛔ **母 router 的 key 打叶子回 400 `No connected db.`**;叶子要它自己的 key
  (Secret `chatgpt-pool-master-key` / `LITELLM_MASTER_KEY`)。
  叶子模型名还带前缀:`chatgpt-gpt-5.6-terra`,裸 `gpt-5.6-terra` 回 400 Invalid model name。
- ⛔ **HTTP 429 是池子范围的常态噪声**,不是坏号的证据 —— 要拿同时段在池老号做基线对照才有意义。
- ⛔ **"全 0"和"脚本坏了"长得一样**:dry-run 读出"没有目标在池"时,必须拿**仍在池的号**再跑一次
  当阳性对照(实测 175/176/194 → 3 号 24 条),否则你验的是自己的 bug。

## 两个 shell 层的坑(`sudo -S` 远程执行)

glob 和 `<` 重定向都由**非 root 那层 shell** 展开,它读不了 `/Data/rancher/storage`:

```bash
sudo -S ls -d /Data/...*        # ✗ 静默空结果 ⇒ 误报"找不到 PVC 目录"
sudo -S wc -c < path            # ✗ Permission denied ⇒ 误报"PVC 写入未确认"
sudo -S sh -c 'ls -d /Data/...*'   # ✓
```

另:`kx()` 这类 helper 自己就套了一层单引号,再往里塞带引号的 JSON(patch body)必然塌陷,
那一步得直接走 `s198` + `'\''` 转义。`resume_acct` 要传**内联 meta dict**
(`POOL_ACCOUNTS` 只有 15 条,查表必 KeyError;location=198 时 `port` 字段实际不用但要有)。

## PVC 直写比 busybox cp pod 简单

这批 PV 全是 local-path 且落在控制节点 198 本机,真实路径
`/Data/rancher/storage/<pv>_litellm-product_chatgpt-acct-<N>-auth/auth.json`,`scale=0` 后 `sudo cp` 即可。

## 换 auth 前:身份审计不许跳

编号↔账号的映射历史上漂过(acct-173/174 的邮箱记在 163/164 名下)。换 auth 前必须确认
新 auth 的 `chatgpt_account_id` == 该 N 的 PVC 里原来那个,三条腿:批内互不重复、与在跑 pod 零碰撞、
与各自 N 的 PVC 原身份逐一吻合。装错 = 两个 pod 共用一个 refresh_token 互相作废。

## 战果基线

- **2026-09-12 暂停 40 个号**(115~121/127~131/135~162):25 个在池持 **200 条 arms** 全删,
  4 个 proxy 副本读数一致,33 个 deploy scale 到 0(另 7 个本就是 0),跨一轮 cron 复查 **40/40 站住**、
  零 arms 回流。池子 **50 → 25 个号**。
- **2026-09-12 回池 19 个号**(175~178/180~194):串行零失败,router **31 → 50**,19×8=152 条 arms,
  4 副本一致;12 分钟真实流量里 19 个号全部在收请求,200/429 比例与在池老号同形。

## 相关 memory

`feedback_198_pool_rejoin_needs_imghandler_mount_and_manual_scale` ·
`project_acct_173_194_renew_reoauth_2026_09_12` · `feedback_alive_pod_pulled_from_router_needs_resume_not_reoauth` ·
`feedback_pod_deletiontimestamp_is_deadline_not_delete_time` · `feedback_all_acct_images_must_match_acct82_by_digest` ·
`feedback_disk_authjson_token_is_stale_not_death_proof` · `topic_chatgpt_acct_pool_index`
