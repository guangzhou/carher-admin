#!/bin/sh
# crg_spend_window.sh —— 查一个时间窗口内 cr-g-* / cursor-g-* 的 SpendLogs 行。
#
# 为什么需要它：门③原来的阳性对照判据是「nonce 出现在 pod 日志里」。2026-09-02 实测
# **这条判据对当前流量形状不成立**——nonce 只在 pod 把用户文本打出来的那些分支才可见
# （`[chat-only] greeting/ping "ZK-…"` 打前 24 字符），走 proto2/handshake 分支时一个字
# 都不打。于是「0 命中」既可能是"键击没发出去"，也可能是"发出去了但日志不打文本"，
# 两种情况都退出 3，红得没有信息量。
#
# SpendLogs 是 LiteLLM 自己写的，**被验对象（Cursor / 模型 / lane pod）伪造不了**，
# 而且一行里同时给出三件事：请求到没到、落在哪个 model_group、成功还是失败为什么失败。
#
# ⚠️ `messages` 列在 198 上恒为 `{}`（不存 prompt），所以**不能靠它按 nonce 精确对齐**
#    ——阳性对照实测过（pos=0，连成功行也是 `{}`）。这里只能按时间窗口对齐。
#    窗口对齐在单人测试时够用，但如果同时有别人的流量，model_group 会掺——所以输出里
#    把 model_group 逐行打出来，由调用方判断，而不是在这里聚合成一个"通过/不通过"。
#
# 用法（本机 Mac 上）：
#   ssh -o BatchMode=yes cltx@10.68.13.198 "T0='2026-09-02 11:26:00' T1='2026-09-02 11:31:00' sh -s" \
#       < scripts/zk-cursor-web/crg_spend_window.sh
#
# ⚠️ 经 `sh -s` 从 stdin 喂脚本时，脚本里每个可能读 stdin 的命令都必须 `</dev/null`，
#    否则它会把「脚本自己剩下的部分」当输入吃掉。实测症状：`get secret` 返回空 →
#    脚本报「拿不到 DB 密码」，看起来像权限/密钥名问题，其实是 stdin 被抽干。
#
# 时区：SpendLogs 的 startTime 是 UTC。调用方传 UTC 字符串（北京时间 −8h）。
set -u
: "${T0:?需要 T0（UTC，'YYYY-MM-DD HH:MM:SS'）}"
: "${T1:?需要 T1（UTC，'YYYY-MM-DD HH:MM:SS'）}"

DBPOD=$(sudo -n kubectl -n litellm-product get pod --no-headers </dev/null | awk '/^litellm-db/{print $1; exit}')
[ -n "$DBPOD" ] || { echo "找不到 litellm-db pod"; exit 4; }
# 密码是**可选**的：在 litellm-db-0 容器内用 unix socket 连本地库走的是 trust 认证，
# PGPASSWORD 为空照样连得上（实测）。所以这里绝不能因为"取不到密码"就退出——
# 那会把一把好用的尺子自己挡死（2026-09-02 踩过：litellm-secrets 里根本没有
# POSTGRES_PASSWORD 这个键，而在此之前的查询一直是拿空密码跑通的）。
PW=$(sudo -n kubectl -n litellm-product get secret litellm-db-credentials \
     -o jsonpath='{.data.password}' </dev/null 2>/dev/null | base64 -d 2>/dev/null)

# -A -F'|' 出机器可读的管道分隔；调用方按列解析。只回显 user/db，不回显密码。
sudo -n kubectl -n litellm-product exec "$DBPOD" -- sh -c "PGPASSWORD='$PW' psql -U litellm -d litellm -P pager=off -A -F'|' -t -c \"
select to_char(\\\"startTime\\\",'YYYY-MM-DD HH24:MI:SS'),
       status,
       model_group,
       model_id,
       coalesce(metadata->'error_information'->>'error_class',''),
       replace(left(coalesce(metadata->'error_information'->>'error_message',''),220), chr(10), ' ')
from \\\"LiteLLM_SpendLogs\\\"
where \\\"startTime\\\" >= '$T0' and \\\"startTime\\\" <= '$T1'
  and (model_id like '%cr-g%' or model_id like '%cursor-g%')
order by \\\"startTime\\\";\"" </dev/null
