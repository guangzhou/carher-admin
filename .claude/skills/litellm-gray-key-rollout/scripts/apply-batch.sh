#!/usr/bin/env bash
# 在 198 上串行下发一批 key 到 force-gray。
#
# 用法（在 198 上跑）:
#   apply-batch.sh <run_id> <keys-file> [logfile]
# 例:
#   apply-batch.sh litellm-198-v195-20260914 /root/l3/h50.keys
#
# 设计约束（都是踩出来的，别"优化"掉）:
#   - key 只走隐藏 stdin（printf | bash），永不进 argv / history / 日志
#   - 一把一次调用、一次 nginx reload —— 脚本本身没有批量模式
#   - 失败即停（break），好定位。恢复时从 FAIL 行的 N+1 续跑，别整个文件重跑
#   - 约 1 把/秒，300 把以上必然超前台超时，用后台任务跑
set -uo pipefail

RUN_ID="${1:-}"
KEYS="${2:-}"
LOG="${3:-}"

if [[ -z "$RUN_ID" || -z "$KEYS" ]]; then
  echo "用法: $0 <run_id> <keys-file> [logfile]" >&2
  exit 2
fi

RUN="/root/litellm-gray-run/$RUN_ID"
ENVF="$RUN/nginx/frozen-env.sh"
S="$RUN/src-3914155/litellm-gray-rollout/scripts/gray-key-route.sh"
LOG="${LOG:-/root/l3/$(basename "$KEYS" .keys).applylog}"

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
  if printf "%s" "$k" | bash "$S" force-gray >>"$LOG" 2>&1; then
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
