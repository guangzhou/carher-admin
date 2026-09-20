---
name: litellm-wa-flush
version: 1.0.0
description: >-
  Flush the 198 pro pool's WA (weighted-affinity) session pins so that a
  router weight change actually takes effect. Use when the user says "flush"
  / "重新 flush 一遍" / "改了权重但流量不跟权重走" / traffic stays on the
  old high-traffic acct after a desired_weight change on the 198 ChatGPT Pro
  pool. Runs scripts/litellm-wa-flush-affinity.py inside the litellm-proxy pod
  (delete weighted_affinity:v2:* only; never FLUSHALL, keep fail-marks).
metadata:
  requires:
    bins: ["python3", "kubectl"]
    repo_files:
      - "scripts/litellm-wa-flush-affinity.py"
      - "scripts/jms"
    siblings:
      - "chatgpt-pro-litellm"
      - "chatgpt-quota-rebalance"
      - "litellm-encrypted-content-affinity"
---

# WA session-affinity flush (198 pro pool)

一句话:198 pro 池挂了 WA 亲和 hook,把**存量会话钉在"改权重之前"服务它的 deployment 上**
(Redis `weighted_affinity:v2:*`,120s 滑动 TTL)。weight(simple-shuffle)**只决定新/冷启会话**落哪。
所以改完 `desired_weight`,存量长会话(Codex 多轮)继续压老号 → 看着"流量不跟权重走"。
**删 v2 pin → 存量会话失 pin → 下个请求按 weight 重路由并重建 pin。**

机制细节见 sibling `chatgpt-pro-litellm` §Pool Weight Rebalance 与
`litellm-encrypted-content-affinity`。本 skill 只管"执行 flush + 验收 + 别乱 flush"。

## 先判该不该 flush(三条独立证据,别只凭"流量看着对/不对")

1. **权重层**:`GET /model/info` 逐号核 `litellm_params.weight` == desired_weight。先排除权重本身没写对。
2. **路由层**:下面命令的 **DRY-RUN**,看 `pins by pinned deployment` 是否偏斜。
3. **数据层**:acct pod 真实请求分布(高权号成功数 >> weight-1 号)。

**只有 ①权重对 + ②路由层仍偏斜 → 才 flush。** 若 ② 已清一色高权号、无偏斜,**flush 收益为零**,
不要做(见下"别乱 flush")。

## 执行

```bash
SCRIPT_DIR=/Users/Liuguoxian/codes/carher-admin/scripts
# ① 取 proxy pod 名(pod 频繁 roll,每次都要重取)
POD=$("$SCRIPT_DIR/jms" ssh AIYJY-litellm "kubectl -n litellm-product get pods -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}'" 2>/dev/null)
# ② 重新 scp + cp 脚本到当前 pod(/tmp 随 pod 销毁,不能复用旧 pod 的副本)
"$SCRIPT_DIR/jms" scp "$SCRIPT_DIR/litellm-wa-flush-affinity.py" "AIYJY-litellm:/tmp/wa-flush.py" >/dev/null 2>&1
"$SCRIPT_DIR/jms" ssh AIYJY-litellm "kubectl -n litellm-product cp /tmp/wa-flush.py $POD:/tmp/wa-flush.py -c litellm"

# ③ DRY-RUN(默认,不删):看 model_group / pinned deployment 分布,先判偏斜
"$SCRIPT_DIR/jms" ssh AIYJY-litellm "kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-flush.py 2>&1 | grep -v sitecustomize"

# ④ 真 flush(全部 v2 pin)
"$SCRIPT_DIR/jms" ssh AIYJY-litellm "kubectl -n litellm-product exec $POD -c litellm -- python3 /tmp/wa-flush.py --apply 2>&1 | grep -v sitecustomize"
```

- 只 flush 某组:`--model-group chatgpt-gpt-5.6-terra --apply`
- 连 fail-mark 一起清(少用,确认坏号已修好):`--apply --include-fail-marks`
- 报告口径:`deleted_v2_pins` / `residual_v2`(期望 0) / `fail_marks_left`(保留) / `dbsize before/after`。
- 报告偏斜时注意:脚本只打 **top 15**。pin 总数 >15 时你看不到尾部明细,只能说"**头部**清一色高权号",
  不能说"XX% 都在高权号上"——后者需要全量统计,别把 top15 的占比当全池占比说出口。

### flush 后 pin 重建速度(2026-09-07 实测,给"pin 总数是流量计"补锚点)

同一天同一池,三次快照:

| 时刻 | v2 pins | 说明 |
|------|---------|------|
| 改权重前 | 763 | 攒了数小时的存量会话 |
| flush 当刻 | 573 → 0 | `deleted_v2_pins=573`,`residual_v2=0` |
| flush 后 ~2.5min | 21 | 从零重建 |
| flush 后 ~15min | 48 | |
| flush 后 ~25min | 66 | |

**从 573 掉到 48 不是"亲和失效",是刚清空后正在重建 + 低谷期。** 判据永远是分布,不是总数。

## 硬红线

- **只删 `weighted_affinity:v2:*`**。默认**保留** `weighted_affinity:fail:v1:*`(避开坏号的保护键,
  180s 自动过期;删了会把量灌向正在 429 的号)。其它一切 Redis key(预算/spend/router 状态,占 dbsize
  大头)**绝不碰,永不 FLUSHALL** —— 同一 Redis DB 混放。
- 给死号(paused/OFFLINE/scale0)设高权 flush 也没用:weight-align 跳过它们,恒 0 流量。先
  `chatgpt-acct-quota.sh` 确认 `take=✅ ONLINE`。

## 别乱 flush(2026-08-26 连做 8 次实证)

- **flush 是一次性纠偏,不是可反复拧的旋钮**。②路由层已无偏斜后再 flush 收益为零,反而把几百个
  活跃 Codex 长会话打回冷启、砸掉 encrypted-content 缓存命中。
- **pin 总数是实时流量计,不是池子固定属性**:低谷 37 → 爬坡 452 几分钟翻十几倍全是真流量涨。
  看到某刻 pin 少,别断言"亲和空转/flush 没用"—— 判据是**分布偏斜**,不是 pin 总数。
- 别拿一张低谷快照过度外推成"flush 有害/无意义"的常驻结论;要判就重新测 DRY-RUN 的分布。
- 用户反复要 flush 但分布已无偏斜时:先回问背后困扰的具体现象(某号/某组不接量、延迟、报错),
  那才是正题;flush 这侧数据若已证伪,别陪着反复删 pin。
- **但用户重申"强制 flush 一次"= 决定已下,照做,别再劝第二轮**(2026-09-07)。先把 DRY-RUN
  的证据摆出来、说清代价(存量会话冷启),用户仍要 → 直接 `--apply` 并如实报数。
  反复劝阻是拿我的判断压用户的决定。
- 旁证(独立线):失败若全是 HTTP **400** 且高低权号都出现 = 客户端 malformed payload(请求侧),
  与 flush/weight/号健康无关,别归错。

关联记忆:`feedback_pool_weight_change_needs_wa_affinity_flush`、
`feedback_weight_align_only_patches_probed_accts`。
