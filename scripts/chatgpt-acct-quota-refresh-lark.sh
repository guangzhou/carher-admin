#!/usr/bin/env bash
# chatgpt-acct-quota-refresh-lark.sh — 一条命令刷新「ChatGPT acct 上游配额」多维表格
#
#   ./chatgpt-acct-quota-refresh-lark.sh             # 取数 + 写表
#   ./chatgpt-acct-quota-refresh-lark.sh --dry-run   # 取数 + 自检，不动表（其余参数原样透传）
#   ./chatgpt-acct-quota-refresh-lark.sh --find 227  # 只读表：227 在不在、被哪个视图藏了
#
# 表: https://t83dfrspj4.feishu.cn/base/YdrZb5xaNaziDHssawGcDpd1nqf?table=tblsw3E1zZ2IuBbW
# 实测（2026-10-07）整链 ~95s，190 行 = 198:171 + aliyun:19。
#
# 读表提示：同一个 acct 可能有两行（site=198 / site=aliyun）。迁到 198 的号，阿里云那行
# verdict 写 `OFFLINE(198在服)` —— 那是有意停掉的旧副本，不是号坏了，看 198 那行。
#
# ⚠「某个 acct 不在表里」先用 `--find <N>` 判，别重跑刷新。2026-10-10 实证 acct-227：
#   全表 190 行里它有两行，是**视图**滤掉的 ——
#     · `online`（15 行）filter = status==ONLINE AND sub_left!=expired
#       → 227 的 198 行 status=ONLINE 但订阅真过期，被第二条滤掉
#     · `预下线`（10 行）filter = 7d% > 90
#       → 227 的 `7d%` 是**空**（实探 401 时三分支逻辑刻意置空），空格子不满足
#         任何数值比较 ⇒ 这类行（2026-10-10 共 65 个）对该视图结构性不可见
#   **视图不是表，空格子在视图里和「不存在」同形。** 刷新一轮只会写回同样的行，
#   回答不了这个问题。现在每轮刷新都会印 `[views]` 段把各视图藏了多少行报出来。
#
# 行宇宙是**动态**的，没有任何硬编码 acct 名单（查过三个文件）：
#   198   = state.json 的 key ∪ `kubectl -n litellm-product get deploy` 名字
#           （正则 ^chatgpt-acct-(\d+)$，2026-10-10: state 170 ∪ deploy 165 = 171 行）
#   aliyun = `kubectl -n carher get deploy` 同正则（19 行）
# 硬编码的只有 base_token / table_id / 两台主机 IP / 采样常量。新加的号下一轮自动出现。
#
# 这层包装做 chatgpt_acct_quota_to_lark.py 不替你做的几件事:
#
# ① **强制两侧都取数**（`--run-quota --run-quota-aliyun`）。漏掉 aliyun 那个 flag 时
#    脚本只 warn 一句就照写，而写表是 **delete-all 然后 recreate** —— 结果是把阿里云
#    记录**从表里抹掉**，且不报错。这是真丢数据，不是少写。
# ② **先删 /tmp rows 缓存**。不删的话上一轮的 json 还在，取数腿失败时会拿陈旧数据写表，
#    表上时间戳看着新、内容是旧的。
# ③ **互斥锁**。两轮重叠时各自 delete-all 再 recreate，后删的那轮删不掉先写的那轮刚建的行
#    ⇒ 表里每个号两行。锁用 mkdir（macOS 没有 flock），持锁进程死了会按 PID 判活自动接管。
# ④ 预检本机依赖，缺了在取数前就退（不白等 90s）。
# ⑤ 整轮输出存一份到 $LOG（stderr 里的 [sub]/[auth]/[aliyun] 自检是要回头看的）。
# ⑥ 打印 wall time，和历史基线摆一起 —— 好判断"是不是又开始空烧了"
#    （耗时贴着超时常量且方差 <5s = 定长等待，见 scripts/jms 里 sentinel 那段注释）。
#
# 需要: 本机 jms 凭证（~/.config/jms/key.json）+ 198 直连 ssh + lark-cli 已登录。
# 任一侧取数失败就**不碰表**（python 脚本自己保证），所以失败是安全的，重跑即可。
set -euo pipefail

cd "$(dirname "$0")"

# `--find` 是纯读路径：不取数、不动表 ⇒ 不该抢锁（否则正在跑的刷新会把查询挡掉，
# 而查询恰恰是刷新进行中最想做的事），也不该删 /tmp 缓存。直接转交 python。
for a in "$@"; do
  if [[ "$a" == "--find" ]]; then
    exec python3 chatgpt_acct_quota_to_lark.py "$@"
  fi
done

R198="/tmp/chatgpt-acct-quota-rows.json"
RALI="/tmp/chatgpt-acct-quota-aliyun-rows.json"
LOCK="/tmp/chatgpt-acct-quota-refresh-lark.lock"
LOG="/tmp/chatgpt-acct-quota-refresh-lark.$(date +%Y%m%d-%H%M%S).log"

# ④ 预检
miss=()
for c in python3 ssh lark-cli; do command -v "$c" >/dev/null || miss+=("$c"); done
[[ -f "$HOME/.config/jms/key.json" ]] || miss+=("~/.config/jms/key.json")
if ((${#miss[@]})); then
  echo "!!! 缺依赖: ${miss[*]} —— 没取数、没动表" >&2
  exit 2
fi

# ③ 互斥锁
if ! mkdir "$LOCK" 2>/dev/null; then
  holder=$(cat "$LOCK/pid" 2>/dev/null || true)
  if [[ -n "$holder" ]] && kill -0 "$holder" 2>/dev/null; then
    echo "!!! 另一轮刷新在跑 (pid=$holder) —— 重叠会让表里每个号出现两行，本轮退出" >&2
    exit 3
  fi
  echo "# 接管残留锁 (pid=${holder:-?} 已不在)" >&2
  rm -rf "$LOCK"; mkdir "$LOCK"
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# ② 删缓存
rm -f "$R198" "$RALI"

echo "# 刷新 ChatGPT acct 配额表（两侧并发取数，约 95s）... 日志 → $LOG"
S=$(date +%s)
# `|| RC=$?`：不能写成裸调用 + 下一行 `RC=$?` —— set -e 会在 python 非零时直接退出，
# 那行 RC 永远是 0，且失败时连耗时都打不出来（失败恰恰是最需要看耗时的时候）。
# PIPESTATUS[0] 取的是 python 的退出码，不是 tee 的。
RC=0
python3 chatgpt_acct_quota_to_lark.py --run-quota --run-quota-aliyun "$@" 2>&1 \
  | tee "$LOG" || RC=${PIPESTATUS[0]}
E=$(date +%s)
echo "=== wall=$((E-S))s rc=$RC  (基线 93s@10-07；>200s 说明有腿在空等超时)  log=$LOG ===" \
  | tee -a "$LOG"
exit "$RC"
