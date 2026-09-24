#!/usr/bin/env bash
# kiro 18 条道回归压测 —— 加号/摘号/换镜像之后的那一步，一条命令跑完。
#
# 为什么要有这个脚本（每条都是 §E 踩过的坑，手搓时每次都要重新想起来）
# ------------------------------------------------------------------
# 1. 🔴 **车道名必须从线上 CM 现取，且 grep 不能加 `^\s*- ` 前缀锚。**
#    CM 里的 YAML 已被 LiteLLM 规范化（键排序、`model_name` 不再是列表首键），
#    带锚的写法抓到 **0 行**，bench 于是打印「全局 0/0 成功」——
#    那是**空集假绿/假红**，极容易读成"18 条道全挂了"。
#    ⇒ 本脚本先断言车道数，数不对**直接退出，不跑**。
# 2. key 的环境变量名是 **`MK`**（bench 的 `--key-env` 默认值）。
#    写成 `LITELLM_MASTER_KEY` 会得到"环境变量 MK 没设"。
# 3. key 的来源是 **`secret/litellm-probe-key` 的 `PROBE_KEY`**。
#    ns `litellm-product` 里**没有** `secret/litellm-secrets`，也没有 `LITELLM_MASTER_KEY` 这个键。
# 4. 🔴 **验收判据是 `input_tokens` 分层，不是 200。** 各家上游注入的系统前导大小不同。
#    ⚠️ 两条收窄（都是 09-24 实测逼出来的，别再按老说法写门禁）：
#    a) 「两条道 in_tok 相同 = 静默降级」不成立 —— 健康态天然就有同族相同
#       （gpt 三道 429~430、opus-4-5/sonnet-4-6/haiku-4-5 都挤在 4110 附近）。
#    b) 「与上轮逐字吻合」也不成立 —— 同一 prompt 连跑两轮，同一条道自己就抖
#       （haiku 4144→4110、minimax-m2.1 3742→3694，最大 1.3%）。
#    ⇒ 本脚本落基线 + 按 **±5% 容差带** 判，只抓"跨族塌陷"这种数量级位移。
# 5. ⚠️ 单发长尾（如 `minimax-m2.1` 14s）是抖动不是坏道，别拿 1 发的 max 定性。
#
# 用法
#   scripts/kiro-lane-regress.sh                 # 跑 10 轮，与上一轮基线对比
#   scripts/kiro-lane-regress.sh --rounds 3      # 快跑
#   scripts/kiro-lane-regress.sh --save-baseline # 把本轮 in_tok 存成新基线
#
# 基线文件：本仓 `scripts/kiro-lane-baseline.tsv`（`模型<TAB>in_tok`），随代码走。
set -euo pipefail

HOST=cltx@10.68.13.198
NS=litellm-product
EXPECT_LANES=${EXPECT_LANES:-18}
ROUNDS=10
SAVE=0
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASELINE="$REPO/scripts/kiro-lane-baseline.tsv"

while [ $# -gt 0 ]; do
  case "$1" in
    --rounds) ROUNDS="$2"; shift 2 ;;
    --save-baseline) SAVE=1; shift ;;
    -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
    *) echo "未知参数 $1"; exit 2 ;;
  esac
done

echo "# 1/4 推 bench 脚本到 198"
scp -q "$REPO/scripts/litellm-lane-bench.py" "$HOST:/tmp/"

echo "# 2/4 从线上 CM 现取车道名（不加前缀锚）并断言条数"
MODELS=$(ssh "$HOST" "sudo kubectl -n $NS get cm litellm-config -o jsonpath='{.data.config\.yaml}' \
  | grep -oE 'model_name: kiro-[a-zA-Z0-9._-]+' | sed 's/model_name: //' | sort -u | paste -sd,")
N=$(printf '%s' "$MODELS" | tr ',' '\n' | grep -c .)
echo "#   车道 $N 条：$MODELS"
if [ "$N" -ne "$EXPECT_LANES" ]; then
  echo "🔴 车道数 $N ≠ 预期 $EXPECT_LANES ⇒ **不跑**。"
  echo "   要么 CM 真的加/减了道（改 EXPECT_LANES=$N 再跑），要么 grep 又被 YAML 规范化坑了。"
  echo "   ⛔ 别在这里往下走：条数不对时 bench 的「全绿」是空集假绿。"
  exit 1
fi

echo "# 3/4 跑 $ROUNDS 轮（轮转不连发，自带阴性对照，成功判据=文本非空）"
OUT=/tmp/kiro-regress.$(date +%Y%m%d-%H%M%S)
ssh "$HOST" "set -e
  MK=\$(sudo kubectl -n $NS get secret litellm-probe-key -o jsonpath='{.data.PROBE_KEY}' | base64 -d) \
  python3 /tmp/litellm-lane-bench.py --models '$MODELS' --rounds $ROUNDS --out $OUT.jsonl" \
  | tee "$OUT.log"

echo "# 4/4 input_tokens 分层比对（判静默降级的唯一尺子）"
CUR="$OUT.tsv"
# 汇总表最后一列是 in_tok；只取以 kiro- 开头的行
grep -E '^kiro-' "$OUT.log" | awk '{print $1"\t"$NF}' | sort -u > "$CUR"
lanes_with_tok=$(grep -c . "$CUR" || true)
if [ "$lanes_with_tok" -ne "$EXPECT_LANES" ]; then
  echo "🔴 只解析出 $lanes_with_tok 条道的 in_tok（预期 $EXPECT_LANES）⇒ 这把尺子自己坏了，"
  echo "   先看 $OUT.log 的汇总表格式，别据此下「无降级」的结论。"
  exit 1
fi
dupes=$(awk -F'\t' '{c[$2]=c[$2]" "$1} END{for(t in c){n=split(c[t],a," "); if(n>1) print t": "c[t]}}' "$CUR")
if [ -n "$dupes" ]; then
  # ⚠️ **相同 in_tok 本身不是红。** 09-24 实测健康态就有三组天然相同：
  #    glm-5 / minimax-m2.1 = 3742，三条 gpt 道 = 429~430，opus-4-5 / sonnet-4-6 = 4110
  #    —— 同一家上游注入同样的系统前导，本来就该一样。
  #    ⇒ 把"有重复"当红会每轮都喊狼来了。真尺子是下面的**基线逐道 diff**：
  #      某条道的 in_tok **变成了另一条道的值**才是静默降级。
  echo "#   in_tok 相同的组（天然同族即正常，只在与基线相比**新出现**时才可疑）："
  echo "$dupes" | sed 's/^/#     /'
fi
# 🔴 `kiro-auto` 排除在基线比对之外：它是**路由道**，上游自己挑落点 ⇒ in_tok 天然会漂
#    （09-24 单发读到 5903，介于 opus-4-7 5974 与 opus-4-8 6484 之间）。
#    把它纳进 diff 会让每一轮都报差，再一次把尺子变成喊狼来了。它的值只打出来供参考。
CMP="$OUT.cmp.tsv"
grep -v '^kiro-auto' "$CUR" > "$CMP"
echo "#   kiro-auto（路由道，不进基线比对）in_tok = $(awk -F'\t' '$1=="kiro-auto"{print $2}' "$CUR")"
if [ -f "$BASELINE" ]; then
  # 🔴 **不能用 `diff` 做逐字相等比对。** 09-24 连跑两轮同一 prompt，同一条道的 in_tok
  #    自己就在抖：haiku-4-5 4144→4110、minimax-m2.1 3742→3694（最大 1.3%）、
  #    gpt 道 429↔430。抖动来源没查（不下归因），但**"逐字吻合"根本不成立** ⇒
  #    用相等当门禁 = 每轮都报差，尺子作废。
  #    这里按**容差带**判：偏离基线 >TOL% 才算红。真正的静默降级是数量级的位移
  #    （某条道从 6765 塌到 4110 是 −39%），1% 的抖动淹不掉它。
  # ⚠️ **已知盲区**：in_tok 分不开相邻的 Claude 变体（opus-4-5 4111 / sonnet-4-6 4110 /
  #    haiku-4-5 4110 本来就挤在一起）⇒ 这把尺子只抓"跨族塌陷"，抓不了"同族串道"。
  TOL=${TOL:-5}
  bad=$(awk -F'\t' -v tol="$TOL" '
    NR==FNR {b[$1]=$2; next}
    {
      if (!($1 in b)) { print "  🆕 " $1 " 不在基线里（新道？）"; next }
      d = ($2 - b[$1]) / b[$1] * 100
      if (d < 0) d = -d
      if (d > tol) printf "  🔴 %-26s 基线 %-7s 本轮 %-7s 偏离 %.1f%%\n", $1, b[$1], $2, d
    }' "$BASELINE" "$CMP")
  if [ -z "$bad" ]; then
    echo "✅ in_tok 全部在基线 ±${TOL}% 内（$(grep -c . "$CMP") 条，不含 kiro-auto）⇒ 无跨族降级"
  else
    echo "🔴 有道偏离基线超过 ${TOL}%："
    echo "$bad"
    echo "   ⇒ 先查是不是静默降级（§G），确认是上游改了系统前导再 --save-baseline"
    RED=1
  fi
else
  echo "⚠️ 还没有基线文件 $BASELINE —— 本轮结果可作首个基线（--save-baseline）"
fi
# ⛔ 不写 `[ "$SAVE" = 1 ] && cp …`：SAVE=0 时整行返回非零，`set -e` 会让脚本在这里
#    静默退出 1 —— 看上去像"回归失败"，其实只是没存基线。
if [ "$SAVE" = 1 ]; then
  cp "$CMP" "$BASELINE"
  echo "# 基线已更新 -> $BASELINE"
fi

echo
echo "本轮原始数据：198:$OUT.log / $OUT.jsonl"
echo "⚠️ 判据是 in_tok 分层 + 文本非空，不是 200。单发长尾是抖动不是坏道。"
# ⛔ 红要在 --save-baseline 之后才退出：否则"确认属实后 --save-baseline"这条出路
#    永远走不到（脚本在判红那一步就死了）。
exit "${RED:-0}"
