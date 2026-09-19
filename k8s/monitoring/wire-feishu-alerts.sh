#!/usr/bin/env bash
# 接通「Grafana 告警 -> 飞书群」这条投递腿。
#
# 为什么需要这个脚本：告警规则现在评估正确了，但送不到任何人手上。
# 2026-09-19 实测三处全断：
#   1. deploy/alert-to-feishu spec.replicas=0，generation=1 —— 从未被改过，
#      不是被谁关掉的，是当年建到这一步就停住了；
#   2. 它依赖的 Secret feishu-alert-webhook 不存在，所以起副本必然 CrashLoop
#      （alert2feishu.py:45 是 os.environ["FEISHU_WEBHOOK"]，缺了直接 KeyError）；
#   3. Grafana 根路由 receiver 是 grafana-default-email，飞书联系点建好了但没人指。
# 有依赖顺序：必须先有 webhook 值，再起副本，最后切策略。顺序颠倒会把全部
# 告警打进一个 CrashLoop 的服务，比现在（至少 Grafana UI 里能看到）更糟。
#
# 唯一缺的东西是飞书群机器人的 webhook URL —— 那只有群管理员能拿到：
#   飞书群 -> 设置 -> 群机器人 -> 添加机器人 -> 自定义机器人 -> 复制 webhook 地址
# 形如 https://open.feishu.cn/open-apis/bot/v2/hook/<一串 uuid>
# 若机器人开了「签名校验」，把签名密钥也一起填（不开就留空）。
#
# 用法（值走隐藏 stdin，不进命令行、不进 shell history、不进日志）：
#   ./wire-feishu-alerts.sh
# 脚本会依次提示输入 webhook 与签名密钥，两者都不回显。
#
# 动了什么 / 备份在哪 / 怎么回滚：
#   动：创建 Secret monitoring/feishu-alert-webhook；
#       deploy/alert-to-feishu 副本 0 -> 1；
#       Grafana 根路由 receiver 改为「feishu-群机器人」。
#   备份：运行前自动导出 deploy 与当前通知策略到
#       /home/cltx/a2f-wire-<时间戳>/（0600）。
#   回滚：kubectl -n monitoring scale deploy alert-to-feishu --replicas=0
#         kubectl -n monitoring delete secret feishu-alert-webhook
#         用备份里的 policies.before.json 走 PUT /api/v1/provisioning/policies 还原
#         （根路由不是文件式 provisioning 管的，所以 API 改得动，也不需要 restart）。
#
# 已做过的端到端自检（2026-09-19，用假 webhook 指向 .invalid 地址，绝不外发）：
#   副本起得来（1/1 Running，越过了第 45 行）；
#   合成告警解析成功、投递如实失败，计数器
#     alert2feishu_received_total{status="firing"}=1
#     alert2feishu_sent_total=0
#     alert2feishu_delivery_failures_total{reason="exception"}=1
#   自检完立刻缩回 0 并删掉假 Secret —— 不留一个指向 .invalid 的 Secret，
#   否则将来有人起副本会以为已经配好了。
#   => 除 webhook 值本身，整条链路已验证可用。
set -euo pipefail

NS=monitoring
BK="/home/cltx/a2f-wire-$(date -u +%Y%m%dT%H%M%SZ)"

red() { printf '\033[31mRED: %s\033[0m\n' "$*" >&2; exit 1; }
ok()  { printf '  %s\n' "$*"; }

command -v kubectl >/dev/null || red "找不到 kubectl"

kubectl -n "$NS" get deploy alert-to-feishu >/dev/null \
  || red "deploy/alert-to-feishu 不存在，先确认命名空间对不对"

mkdir -p "$BK"; chmod 700 "$BK"
kubectl -n "$NS" get deploy alert-to-feishu -o yaml > "$BK/deploy-alert-to-feishu.before.yaml"

GP="$(kubectl -n "$NS" get pods -l app=grafana -o jsonpath='{.items[0].metadata.name}')"
[ -n "$GP" ] || red "找不到 grafana pod"
# Grafana admin 密码是 pod 内的 env，只在容器内用，绝不落到宿主命令行
kubectl -n "$NS" exec "$GP" -- sh -c \
  'wget -qO- "http://admin:$GF_SECURITY_ADMIN_PASSWORD@127.0.0.1:3000/api/v1/provisioning/policies"' \
  > "$BK/policies.before.json"
chmod 600 "$BK"/*
ok "备份已写到 $BK（0600）"

printf '飞书群机器人 webhook 地址（不回显）: ' >&2
IFS= read -rs WEBHOOK; echo >&2
[ -n "$WEBHOOK" ] || red "webhook 为空，什么都没改"
case "$WEBHOOK" in
  https://open.feishu.cn/open-apis/bot/v2/hook/*) ;;
  https://open.larksuite.com/open-apis/bot/v2/hook/*) ;;
  *) red "地址形状不对，应以 https://open.feishu.cn/open-apis/bot/v2/hook/ 开头" ;;
esac

printf '签名密钥（机器人没开签名校验就直接回车，不回显）: ' >&2
IFS= read -rs SIGN; echo >&2

ok "webhook len=${#WEBHOOK} sha256head=$(printf '%s' "$WEBHOOK" | sha256sum | cut -c1-12)"
ok "签名密钥: $([ -n "$SIGN" ] && echo "已填 len=${#SIGN}" || echo "留空（不校验）")"

# 1) Secret：值走 stdin 文件，不经命令行参数
TMP="$(mktemp -d)"; chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT
printf '%s' "$WEBHOOK" > "$TMP/url"
ARGS=(--from-file=url="$TMP/url")
if [ -n "$SIGN" ]; then printf '%s' "$SIGN" > "$TMP/secret"; ARGS+=(--from-file=secret="$TMP/secret"); fi
kubectl -n "$NS" create secret generic feishu-alert-webhook "${ARGS[@]}" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "Secret feishu-alert-webhook 已就位"

# 2) 起副本，并确认它真的越过了 os.environ["FEISHU_WEBHOOK"]
kubectl -n "$NS" scale deploy alert-to-feishu --replicas=1 >/dev/null
kubectl -n "$NS" rollout status deploy/alert-to-feishu --timeout=120s \
  || red "副本没起来，看 kubectl -n $NS logs -l app=alert-to-feishu；Secret 已建但策略未切，现状无害"
AP="$(kubectl -n "$NS" get pods -l app=alert-to-feishu \
      --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')"
[ -n "$AP" ] || red "没有 Running 的 pod"
ok "转换层已运行: $AP"

# 3) 先打一条合成告警验证真能送达，送不到就不切策略
kubectl -n "$NS" exec "$GP" -- sh -c 'cat > /tmp/a2f-probe.json <<JSON
{"status":"firing","alerts":[{"status":"firing","labels":{"alertname":"[接线自检] 请忽略","severity":"info"},"annotations":{"summary":"告警投递腿接通自检，非真实故障"},"startsAt":"1970-01-01T00:00:00Z","valueString":"[ var=B value=1 ]"}],"groupLabels":{"alertname":"[接线自检] 请忽略"},"commonLabels":{"alertname":"[接线自检] 请忽略"},"title":"[接线自检] 请忽略","message":"看到这条说明投递腿通了"}
JSON
wget -qO- --post-file=/tmp/a2f-probe.json --header="Content-Type: application/json" \
  http://alert-to-feishu.monitoring.svc.cluster.local:8080/grafana >/dev/null 2>&1
rm -f /tmp/a2f-probe.json' || true

# 判据用计数器，不用 HTTP 码：飞书会 200 + body code!=0 地假装成功。
# 抓一次存下来再解析 —— 抓两次会读到两个不同时刻的快照；
# 且不吞 stderr：抓不到指标和「送不出去」是两件事，静默成空串会把前者报成后者。
MET="$TMP/metrics.txt"
kubectl -n "$NS" exec "$GP" -- sh -c \
  'wget -qO- http://alert-to-feishu.monitoring.svc.cluster.local:9110/metrics' > "$MET" \
  || red "抓不到转换层的 /metrics —— 这是量具坏了，不代表告警没送出去。
       Secret 与副本已就位，策略未切，现状无害。"
[ -s "$MET" ] || red "/metrics 返回空 —— 同上，是量具问题不是投递问题"
SENT="$(awk '/^alert2feishu_sent_total /{print $2}' "$MET")"
FAIL="$(awk '/^alert2feishu_delivery_failures_total/{s+=$2} END{print s+0}' "$MET")"
[ -n "$SENT" ] || red "/metrics 里没有 alert2feishu_sent_total 这个计数器 —— 量具变了，先核对 alert2feishu.py"
ok "自检计数: sent=$SENT failures=$FAIL"
case "$SENT" in
  0|0.0|"") red "合成告警没送出去（failures=$FAIL）。飞书群里应该收到一条「[接线自检] 请忽略」；
       没收到就检查 webhook 是否失效、机器人是否开了签名但密钥留空。
       Secret 与副本已就位，但通知策略未切 —— 现状与运行前一致，无害。" ;;
esac
ok "飞书群里应已收到一条「[接线自检] 请忽略」"

# 4) 最后才切策略。根路由不是文件式 provisioning，API 改得动，不需要 restart。
# 用 curl 不用 wget：grafana pod 里的 wget 是 BusyBox，没有 --method，发不了 PUT。
# -sS 保留错误输出（不吞 stderr），--fail 让 4xx/5xx 变成非 0 退出码。
kubectl -n "$NS" exec "$GP" -- sh -c 'cat > /tmp/pol.json <<JSON
{"receiver":"feishu-群机器人","group_by":["grafana_folder","alertname"]}
JSON
curl -sS --fail -X PUT -H "Content-Type: application/json" --data-binary @/tmp/pol.json \
  "http://admin:$GF_SECURITY_ADMIN_PASSWORD@127.0.0.1:3000/api/v1/provisioning/policies" >/dev/null
rc=$?; rm -f /tmp/pol.json; exit $rc' \
  || red "切策略的 PUT 失败。Secret、副本、合成告警都已通，只差这一步；
       现状：告警仍走 email，与运行前一致。可在 Grafana UI 里手动把
       Notification policies 的默认 receiver 改成「feishu-群机器人」。"

NOW="$(kubectl -n "$NS" exec "$GP" -- sh -c \
  'wget -qO- "http://admin:$GF_SECURITY_ADMIN_PASSWORD@127.0.0.1:3000/api/v1/provisioning/policies"' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("receiver",""))')"
[ "$NOW" = "feishu-群机器人" ] || red "策略没切成功，现在是 $NOW；用 $BK/policies.before.json 还原"
ok "通知策略已指向 feishu-群机器人"
echo
echo "接通完成。真实告警从下一个评估周期起会进群。"
echo "回滚见本脚本头部注释；备份在 $BK"
