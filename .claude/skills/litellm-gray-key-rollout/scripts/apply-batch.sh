#!/usr/bin/env bash
# 在 198 上串行下发一批 key 到 force-gray（或 protect-prod / remove / force-prod）。
#
# 用法（在 198 上跑）:
#   apply-batch.sh <keys-file> [action=force-gray] [logfile] ["<签字依据>"]
# 例:
#   apply-batch.sh /root/l3/h50.keys force-gray
# run 目录取 ${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}（10-17 起的 run/ 操作层布局：$D/target.env 的 SRC、$D/nginx/frozen-env.sh、$D/sign.sh）。
#
# 设计约束（都是踩出来的，别"优化"掉）:
#   - key 只走隐藏 stdin（printf | bash），永不进 argv / history / 日志
#   - 一把一次调用、一次 nginx reload —— 脚本本身没有批量模式
#   - 失败即停（break），好定位。恢复时从 FAIL 行的 N+1 续跑，别整个文件重跑
#   - 约 1 把/秒，300 把以上必然超前台超时，用后台任务跑
#   - 🔴 每个名单事务都轮转 generation ⇒ force-gray（以及 split≠0 时的 remove）每把前都要按当前 generation 重签 key_pilot_entry
#     （10-26 只签一次 ⇒ 第 2 把就 `invalid key_pilot_entry gate evidence: generation mismatch`）
set -uo pipefail

KEYS="${1:-}"
ACTION="${2:-force-gray}"
LOG="${3:-}"
BASIS="${4:-指名 key 试点（名单与选人依据见 run 目录 pilot/）}"

if [[ -z "$KEYS" ]]; then
  echo "用法: $0 <keys-file> [force-gray|protect-prod|remove|force-prod] [logfile] [\"<签字依据>\"]" >&2
  exit 2
fi
case "$ACTION" in force-gray|protect-prod|remove|force-prod) ;; *) echo "action 不认识: $ACTION" >&2; exit 2 ;; esac

RUN=$(cat "${GRAY_RUN_POINTER:-/root/l3/.gray-run-latest}")
# shellcheck disable=SC1091
. "$RUN/target.env"
ENVF="$RUN/nginx/frozen-env.sh"
S="$SRC/scripts/gray-key-route.sh"
LOG="${LOG:-$RUN/pilot/$(basename "$KEYS" .keys).applylog}"

[[ -f "$ENVF" ]] || { echo "找不到 $ENVF —— run_id 对吗？" >&2; exit 1; }
[[ -x "$S" || -f "$S" ]] || { echo "找不到 $S" >&2; exit 1; }
[[ -f "$KEYS" ]] || { echo "找不到 $KEYS" >&2; exit 1; }

# 不 source 这个，下面全盘失败，且报错会误导成"状态坏了"
set -a
# shellcheck disable=SC1090
. "$ENVF"
set +a

# 冒烟：list 能跑通才算环境对
if ! bash "$S" list >/dev/null 2>&1; then
  echo "冒烟失败: gray-key-route.sh list 跑不通。" >&2
  echo "  如果报 'invalid active generation'，说明 GRAY_ROOT 没生效，不是状态坏了。" >&2
  exit 1
fi

# 下发前格式校验：一把不合格就整批不发
bad=$(awk '{if(length($0)<20||substr($0,1,3)!="sk-")b++}END{print b+0}' "$KEYS")
if [[ "$bad" != "0" ]]; then
  echo "格式异常 $bad 行，整批不发" >&2
  exit 1
fi
total=$(wc -l < "$KEYS")
echo "待下发: $total 把   日志: $LOG"

: > "$LOG"
chmod 600 "$LOG"

n=0; ok=0; fail=0
while IFS= read -r k; do
  [[ -n "$k" ]] || continue
  n=$((n+1))
  # force-prod / protect-prod 永不设闸；force-gray 与 remove 要当前 generation 的 key_pilot_entry
  if [[ "$ACTION" == force-gray || "$ACTION" == remove ]]; then
    bash "$RUN/sign.sh" key_pilot_entry "$BASIS；第 $n 把" >/dev/null || { echo "签 key_pilot_entry 失败（signer.env？）" >&2; break; }
  fi
  if printf "%s" "$k" | bash "$S" "$ACTION" >>"$LOG" 2>&1; then
    ok=$((ok+1))
  else
    fail=$((fail+1))
    echo "FAIL at line $n" >>"$LOG"
    echo "第 $n 行失败，已停。看 $LOG 末尾定位，恢复时从第 $((n+1)) 行续跑。" >&2
    break
  fi
  if (( n % 50 == 0 )); then
    echo "progress: $n/$total ok=$ok fail=$fail"
  fi
done < "$KEYS"

echo "done: total=$n ok=$ok fail=$fail"

# 自检：渲染成功次数应该等于成功下发数
pass=$(grep -c '"status":"PASS"' "$LOG" 2>/dev/null || echo 0)
echo "自检: PASS 渲染=$pass  成功下发=$ok"
if [[ "$pass" != "$ok" ]]; then
  echo "⚠️ 两者不等 —— 有 key 命中了 already-present 的 unchanged 分支（无害），或渲染有异常。查日志。" >&2
fi

if (( fail == 0 )); then
  echo
  echo "下一步:"
  echo "  1. 对账:  bash $S list | sed -n 's/.*sid=\\([0-9a-f]*\\).*/\\1/p' | sort > /root/l3/live.sids"
  echo "            然后跟 <out>_expect.json 逐条比（判据是每个 expect sid 都在 live 里，"
  echo "            不是 sids == 原有+本批 —— 重叠的 key 返回 unchanged 不新增）"
  echo "  2. 验真流量: 见 SKILL.md §6 尺子二"
  echo "  3. 清明文:  shred -u $KEYS"
fi

exit $(( fail > 0 ? 1 : 0 ))
