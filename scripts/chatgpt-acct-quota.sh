#!/usr/bin/env bash
# chatgpt-acct-quota.sh — 198 prod chatgpt-acct 池 5h/wk 配额实时视图
#
# 数据源：188 (JSZX-AI-03) /home/cltx/.chatgpt-quota/state/state.json
# 该文件由 cron 每 5min 运行的 quota-rebalance.py 维护，是 ChatGPT 上游
# 配额（5h% / wk%）的唯一可信来源——它直接 exec 进 198 K3s svc 读 auth.json
# 走 chatgpt.com/backend-api/codex/usage 抓真数据。
#
# 与已废弃的 chatgpt-acct-usage.sh 区别：
#   - usage.sh 按 188 docker / aliyun K8s / MY SSH 三分支独立探测；拓扑迁完后
#     188 容器 24h 内停服、aliyun kubeconfig context 错乱、MY 已废弃，全错
#   - quota.sh 只读 state.json，一切都对：单条 jms ssh，无需 IP 探测，0 误判
#
# 用法：
#   ./scripts/chatgpt-acct-quota.sh         # 表格
#   ./scripts/chatgpt-acct-quota.sh --json  # 透传 state.json
set -euo pipefail

JSON=0
[[ "${1:-}" == "--json" ]] && JSON=1

if [[ $JSON -eq 1 ]]; then
  exec jms ssh JSZX-AI-03 "cat /home/cltx/.chatgpt-quota/state/state.json"
fi

jms ssh JSZX-AI-03 "python3 << 'PY'
import json, time
d=json.load(open('/home/cltx/.chatgpt-quota/state/state.json'))
now=time.time()
def hm(epoch):
  if not epoch: return '—'
  s=int(epoch-now)
  if s<=0: return 'past'
  h,rem=divmod(s,3600); m=rem//60
  return f'{h}h{m:02d}m'
def dhm(epoch):
  # 距离 reset 的剩余时间：>=24h 用 d+h，否则 hh:mm。上游 /codex/usage 给的窗口归零时间
  if not epoch: return '—'
  s=int(epoch-now)
  if s<=0: return 'past'
  d,rem=divmod(s,86400); h,rem=divmod(rem,3600); m=rem//60
  if d: return f'{d}d{h:02d}h'
  return f'{h}h{m:02d}m'
def status_emoji(v):
  if v.get('manual_offline'): return '⛔ OFFLINE'
  if v.get('paused'): return '⏸ PAUSED'
  return '✅ ONLINE'
def takes_traffic(v):
  # 可接流量 = router 路由组当前会真发请求过去：未撞限、未 manual_offline、未 paused
  if v.get('manual_offline') or v.get('paused'): return False
  p5=v.get('primary_pct'); wk=v.get('weekly_pct')
  if p5 is None or wk is None: return False
  return p5 < 95 and wk < 95

rows=[]
for k,v in sorted(d.items(), key=lambda x: int(x[0].split('-')[1])):
  rows.append((
    k,
    '✅' if takes_traffic(v) else ' ',
    v.get('tier','—'),
    v.get('primary_pct',''),
    dhm(v.get('primary_reset_at')),
    v.get('weekly_pct',''),
    dhm(v.get('weekly_reset_at')),
    hm(v.get('restore_at',0)),
    status_emoji(v),
    v.get('cause',''),
  ))

print(f\"{'acct':9s} {'take':>4s} {'tier':>16s} {'5h%':>5s} {'5h_reset':>12s} {'wk%':>5s} {'wk_reset':>12s} {'restore':>9s} {'status':>11s}  cause\")
print('-'*135)
for r in rows: print(f'{r[0]:9s} {r[1]:>4s} {str(r[2]):>16s} {str(r[3]):>5s} {r[4]:>12s} {str(r[5]):>5s} {r[6]:>12s} {r[7]:>9s} {r[8]:>11s}  {r[9]}')

takers=[k for k,v in d.items() if takes_traffic(v)]
online=[k for k,v in d.items() if not v.get('paused') and not v.get('manual_offline')]
paused=[k for k,v in d.items() if v.get('paused') and not v.get('manual_offline')]
offline=[k for k,v in d.items() if v.get('manual_offline')]
def srt(lst): return sorted(lst, key=lambda s: int(s.split('-')[1]))
print()
print(f'✅ 可接流量 ={len(takers):2d}  {srt(takers)}')
print(f'   online   ={len(online):2d}  {srt(online)}')
print(f'⏸  paused   ={len(paused):2d}  {srt(paused)} (5h/wk 撞限自动恢复)')
print(f'⛔ offline  ={len(offline):2d}  {srt(offline)} (manual_offline；不自动恢复)')

# Last-probe staleness check
stale=[]
for k,v in d.items():
  ts=v.get('ts',0)
  if v.get('manual_offline') or v.get('paused'): continue  # paused 故意不探测，非 stale
  if ts and now-ts > 1800:
    stale.append((k, int((now-ts)/60)))
if stale:
  print()
  print('⚠ stale (last probe >30min):')
  for k,m in sorted(stale, key=lambda x: int(x[0].split('-')[1])):
    print(f'  {k}: {m}min ago')
PY"
