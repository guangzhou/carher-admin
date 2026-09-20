#!/usr/bin/env bash
# chatgpt-acct-quota-refresh-lark.sh — 一条命令刷新「ChatGPT acct 上游配额」多维表格
#
#   ./chatgpt-acct-quota-refresh-lark.sh
#
# 表: https://t83dfrspj4.feishu.cn/base/YdrZb5xaNaziDHssawGcDpd1nqf?table=tblsw3E1zZ2IuBbW
# 实测整链 ~70s（两侧并发取数 ~65s + 写表 ~5s），185 行 = 198:171 + aliyun:14。
#
# 这层包装只做三件 chatgpt_acct_quota_to_lark.py 不替你做的事:
#
# ① **强制两侧都取数**（`--run-quota --run-quota-aliyun`）。漏掉 aliyun 那个 flag 时
#    脚本只 warn 一句就照写，而写表是 **delete-all 然后 recreate** —— 结果是把 14 行
#    阿里云记录**从表里抹掉**，且不报错。这是真丢数据，不是少写。
# ② **先删 /tmp rows 缓存**。不删的话上一轮的 json 还在，取数腿失败时会拿陈旧数据写表，
#    表上时间戳看着新、内容是旧的。
# ③ 打印 wall time，和历史基线摆一起 —— 好判断"是不是又开始空烧了"
#    （耗时贴着超时常量且方差 <5s = 定长等待，见 scripts/jms 里 sentinel 那段注释）。
#
# 需要: 本机 jms 凭证（~/.config/jms/key.json）+ 198 直连 ssh。两侧取数并发跑，
# 任一侧失败就**不碰表**（python 脚本自己保证），所以失败是安全的，重跑即可。
set -euo pipefail

cd "$(dirname "$0")"

R198="/tmp/chatgpt-acct-quota-rows.json"
RALI="/tmp/chatgpt-acct-quota-aliyun-rows.json"
rm -f "$R198" "$RALI"

echo "# 刷新 ChatGPT acct 配额表（两侧并发取数，约 70s）..."
S=$(date +%s)
# `|| RC=$?`：不能写成裸调用 + 下一行 `RC=$?` —— set -e 会在 python 非零时直接退出，
# 那行 RC 永远是 0，且失败时连耗时都打不出来（失败恰恰是最需要看耗时的时候）。
RC=0
python3 chatgpt_acct_quota_to_lark.py --run-quota --run-quota-aliyun "$@" || RC=$?
E=$(date +%s)
echo "=== wall=$((E-S))s rc=$RC  (基线 68/69/72s；>200s 说明有腿在空等超时) ==="
exit $RC
