#!/usr/bin/env bash
# crg_gate12_run.sh —— cr-g 22 名菜单在 lane 82 的**真 Cursor** 门①门②验收，一条命令。
#
# 为什么是脚本不是我手点：2026-09-02 01:03 这台 Mac 处于锁屏，osascript 的键击全打在
# 锁屏上，lane 82 六分钟零 [PROMPT] —— 阳性对照红了，且红得有原因。屏幕一解锁跑这个即可。
#
# 顺序是硬的：**先阳性对照，复现不出就退出，不许接着出数**（合成绿与合成红同样不可信）。
#   ① 锁屏门     —— 锁着就退出 2，不装作跑过
#   ② composer 门 —— 选中模型必须是 $EXPECT_MODEL，否则测的是别的东西
#      默认 cr-g-5.6（**池名**，09-02 池化后改的）。它会落到 84/135~140 中的一条，
#      不会落到 82 —— 82 只有 -82 直连名，是 canary。
#      要验 canary：EXPECT_MODEL=cr-g-5.6-luna-82 bash 本脚本。
#   ③ 阳性对照   —— hi 一发，nonce 必须在**某条** lane 的 pod 日志里出现，否则退出 3
#      （池化后落哪条腿由 key 级亲和决定，所以"落在哪条"是打印出来的观测值，不是前提）
#   ④ conv8      —— 一条 chat 连问 8 轮（你好/ls/快排/归并/ls|head/建飞书文档/发链接/pwd）
#   ⑤ 判据三处对齐：pod 日志(门① execenv-strip 后的字符数、会话复用 convId 唯一值)
#                    + Cursor **客户端自己的执行账本**(门② nal.tool_call.*)
#                    + 飞书那篇文档逐 nonce 精确直搜(泛搜 15 条分页会撒谎)
#
# 用法: bash scripts/zk-cursor-web/crg_gate12_run.sh
set -u
cd "$(dirname "$0")/../.."
EXPECT_MODEL=${EXPECT_MODEL:-cr-g-5.6}
DRV=scripts/zk-cursor-web/cursor_gui_e2e_driver.py
COR=scripts/zk-cursor-web/cursor_gui_e2e_correlate.py
LOG=/tmp/crg_gate_pod.log
SSH="ssh -o BatchMode=yes -o ConnectTimeout=25 cltx@10.68.13.198"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

say "① 锁屏门"
if ioreg -n Root -d1 -a 2>/dev/null | grep -A1 CGSSessionScreenIsLocked | grep -q '<true/>'; then
  echo "❌ 屏幕锁着。GUI 驱动的键击会全部打到锁屏上，测出来的红是假红。"
  echo "   解锁后重跑本脚本。"
  exit 2
fi
echo "✅ 已解锁"

say "② composer 选中模型门（**两个**存储面都要对）"
# 只读 applicationUser 那一个面 = 这道门能被绕过。2026-09-02 实测：
# 草稿 composerData:empty-state-draft 里的模型名在新开 chat 时**赢**，并反写回全局键；
# 安装器只写全局键、不动草稿，于是"门绿了、发出去的却是另一个名字"。
python3 - "$EXPECT_MODEL" <<'PY' || exit 2
import json, os, sqlite3, sys
want = sys.argv[1]
DB = os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
K = ("src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
     ".persistentStorage.applicationUser")
con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
got = {}
d = json.loads(con.execute("SELECT value FROM ItemTable WHERE key=?", (K,)).fetchone()[0])
got["applicationUser.composer.modelName"] = (
    ((d["aiSettings"].get("modelConfig") or {}).get("composer") or {}).get("modelName"))
row = con.execute("SELECT value FROM cursorDiskKV WHERE key='composerData:empty-state-draft'").fetchone()
if row is None:
    got["draft"] = "(没有草稿，跳过)"
else:
    raw = row[0].decode("utf8") if isinstance(row[0], bytes) else row[0]
    mc = (json.loads(raw).get("modelConfig") or {})
    got["draft.modelConfig.modelName"] = mc.get("modelName")
    for i, sm in enumerate(mc.get("selectedModels") or []):
        if isinstance(sm, dict):
            got["draft.selectedModels[%d].modelId" % i] = sm.get("modelId")
bad = []
for k, v in got.items():
    flag = "  " if (v == want or str(v).startswith("(")) else "❌"
    print("  %s %-42s = %r" % (flag, k, v))
    if flag == "❌":
        bad.append(k)
print("  期望 = %r" % want)
if bad:
    print("❌ 这些面不是 %s —— 真 Cursor 发出去的名字由草稿那面决定，测的不是这轮的东西。" % want)
    print("   改法（两面一起改 + 重启后复查）：")
    print("     python3 scripts/zk-cursor-web/crg_family_gate.py --swaptest %s" % want)
    sys.exit(1)
PY
pgrep -x Cursor >/dev/null || { echo "❌ Cursor 没在跑"; exit 2; }
echo "✅ Cursor 在跑"

# 池化之后不能只抓一条腿。抓全部 Running 的 lane,每行前缀打上 lane 号,
# 这样"落在哪条腿"本身就是判据的一部分(而不是先假设它落在哪)。
PODS=$($SSH 'sudo -n kubectl -n litellm-product get pod --no-headers --field-selector=status.phase=Running' \
      | awk '/^zero-cursor-bpi/{print $1}')
[ -n "$PODS" ] || { echo "❌ 一条 lane pod 都没找到"; exit 3; }
echo "纳入日志的 lane pod:"; echo "$PODS" | sed 's/^/  /'

lane_of() {  # zero-cursor-bpi-135-xxx-yyy -> 135 ; zero-cursor-bpi-xxx-yyy -> 101(旧方案那条)
  # 纯 shell,不用 sed —— BSD sed 不支持 `t` 后跟同行 label,而这脚本跑在 Mac 上。
  # 两条判据必须同时成立,否则算 101:
  #   ① 首段全是数字(101 的 hash 是 99fcc795,以数字开头但含字母 → 排除)
  #   ② 剥前缀后是 3 段(lane-rshash-podsuffix);101 没有 lane 段只有 2 段
  n=${1#zero-cursor-bpi-}
  [ "$n" = "$1" ] && { echo 101; return; }
  first=${n%%-*}
  rest=${n#*-}
  case "$first" in ''|*[!0-9]*) echo 101; return ;; esac
  case "$rest" in *-*) echo "$first" ;; *) echo 101 ;; esac
}

pull_log() {
  : > "$LOG"
  for p in $PODS; do
    L=$(lane_of "$p")
    $SSH "sudo -n kubectl -n litellm-product logs $p --tail=-1 --since=${1:-20m} --timestamps" 2>/dev/null \
      | sed "s/^/[lane=$L] /" >> "$LOG"
  done
}

say "③ 阳性对照：hi 一发"
python3 "$DRV" reset >/dev/null
python3 "$DRV" hi 1 22
NONCE=$(tail -1 /tmp/e2e_manifest.jsonl | python3 -c 'import sys,json;print(json.load(sys.stdin)["nonce"])')
echo "nonce = ${NONCE}，等 50s 让它落地"
sleep 50
pull_log 6m
# nonce 在 pod 日志里的可见性是**有条件的**：只有把用户文本打出来的分支才有
# （`[chat-only] greeting/ping "ZK-…"` 打前 24 字符）；走 proto2/handshake 分支一个字不打。
# 所以它只能当线索，不能当判据 —— 2026-09-02 拿它当判据，把"请求到了但落在别的
# model_group 且那条 lane 上游 401"误报成"键击没发出去"。
HITS=$(grep -c "$NONCE" "$LOG" 2>/dev/null | head -1 || true); HITS=${HITS:-0}
echo "线索（不是判据）：pod 日志命中 $NONCE 共 $HITS 行"
[ "$HITS" -gt 0 ] && { echo "  落点 lane:"; grep "$NONCE" "$LOG" 2>/dev/null | sed -E 's/^\[lane=([0-9]+)\].*/    \1/' | sort -u; }

# 判据 = SpendLogs 窗口。LiteLLM 自己写的行，Cursor / 模型 / lane pod 都伪造不了，
# 且一行同时回答三件事：到没到、落在哪个 model_group、成/败与败因。
python3 - "$EXPECT_MODEL" <<'PY' || exit $?
import json, subprocess, sys, time
want = sys.argv[1]
man = [json.loads(l) for l in open("/tmp/e2e_manifest.jsonl")]
fire = [m for m in man if m.get("case") == "hi"][-1]["fire_ts"]
fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))   # SpendLogs 是 UTC
t0, t1 = fmt(fire - 5), fmt(fire + 90)
print("SpendLogs 窗口（UTC）：%s ~ %s" % (t0, t1))
out = subprocess.run(
    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25", "cltx@10.68.13.198",
     "T0='%s' T1='%s' sh -s" % (t0, t1)],
    stdin=open("scripts/zk-cursor-web/crg_spend_window.sh"),
    capture_output=True, text=True).stdout
rows = [r.split("|", 5) for r in out.splitlines() if r.count("|") >= 5]
for r in rows:
    print("  %s  %-8s %-24s %s %s" % (r[0], r[1], r[2], r[4], r[5][:110]))

if not rows:
    print("❌ 窗口内一条 SpendLogs 都没有 —— 这一发**没到 LiteLLM**。")
    print("   查这三样：屏幕焦点是不是在 Cursor / 模型名在不在客户端菜单 / key 有没有授权这个名字。")
    sys.exit(3)

groups = sorted({r[2] for r in rows})
if groups != [want]:
    print("❌ 请求落在 %s，不是 %s —— **测的不是这轮要测的名字**。" % (", ".join(groups), want))
    print("   门②只看存储面，而真正发出去的名字由 Cursor 决定；这一行才是实发名字的判据。")
    sys.exit(2)

ok = [r for r in rows if r[1] == "success"]
if not ok:
    print("❌ 名字对、请求也到了，但这条 lane 全部失败（%d/%d）。上面那列是 LiteLLM 记的败因。"
          % (len(rows), len(rows)))
    print("   这是**真缺陷**，不是尺子问题：先修那条 lane，再谈门①门②。")
    sys.exit(3)
print("✅ 阳性对照复现：%d/%d 成功，model_group 全是 %s" % (len(ok), len(rows), want))
PY

say "④ conv8：一条 chat 连问 8 轮"
python3 "$DRV" conv8 1 24
echo "等 120s 收尾"
sleep 120
pull_log 25m

say "⑤-A 网关侧判据（门① + 会话复用）"
python3 - "$LOG" <<'PY'
import re, sys, json
raw = open(sys.argv[1]).read().splitlines()
man = [json.loads(l) for l in open("/tmp/e2e_manifest.jsonl")]

# 跨腿聚合的日志里混着别的 lane 的无关流量(101 / 82 canary / 同事)。不圈定落点 lane
# 就解析,会把别人的 convId 数进来,让"convId 唯一值应当=1"变成常态红(假红)。
# 圈法：本轮 conv8 的 nonce 出现在哪条 lane，就只认那条 lane 的行。
nonces = [m["nonce"] for m in man if m.get("case") == "conv8"]
lanes = set()
for ln in raw:
    if any(nc in ln for nc in nonces):
        m = re.match(r'\[lane=(\d+)\]', ln)
        if m:
            lanes.add(m.group(1))
if lanes:
    log = [l for l in raw if re.match(r'\[lane=(%s)\]' % "|".join(sorted(lanes)), l)]
    print("本轮 conv8 的落点 lane = %s（只解析这些腿的行；其余 lane 是无关流量）"
          % ", ".join(sorted(lanes)))
else:
    log = raw
    print("⚠️ 用 nonce 圈不出落点 lane —— 退化成解析全部 lane，下面的数掺了别人的流量，"
          "只能当线索不能当判据")

convs = set(re.findall(r'\[conv\] saved items=\d+ conv=(\S+)', "\n".join(log)))
strip = [(int(a), int(b)) for a, b in re.findall(r'\[execenv-strip\] (\d+) -> (\d+) chars', "\n".join(log))]
print("会话复用：日志里出现的 convId 唯一值 = %d  %s" % (len(convs), sorted(convs)))
print("  判据：conv8 全程应当只有 1 个 convId；>1 = 中途断链重开会话")
print("门①（execenv-strip 之后实发上游的字符数，这个数才是判据，不是 delta send 那个）：")
for i, (a, b) in enumerate(strip, 1):
    print("  第%-2d次  %6d -> %6d chars" % (i, a, b))
if strip:
    big = [b for _, b in strip if b > 200000]
    print("  超 20 万字符（会触发文件上传悬崖）的次数 = %d" % len(big))
PY
python3 "$COR" "$LOG" conv8 2>/dev/null | tail -40

say "⑤-B 门②判据：Cursor 客户端自己的执行账本（模型伪造不了）"
python3 - <<'PY'
import glob, os, re, time, json
base = os.path.expanduser("~/Library/Application Support/Cursor/logs")
files = sorted(glob.glob(base + "/*/window*/exthost/anysphere.cursor-always-local/*.log"),
               key=os.path.getmtime)[-6:]
cut = time.time() - 60 * 40
n = 0
for f in files:
    if os.path.getmtime(f) < cut:
        continue
    for ln in open(f, errors="replace"):
        if "nal.tool_call" in ln:
            n += 1
            if n <= 20:
                print("  " + ln.strip()[:180])
print("近 40 分钟 nal.tool_call.* 记录数 = %d" % n)
print("  判据：conv8 里第 2/5/8 轮是 shell、第 6 轮建飞书文档 —— 该有的执行条数一条都不能少")
PY

say "⑤-C 飞书文档：逐 nonce 精确直搜（禁止泛搜，15 条分页会撒谎）"
python3 - <<'PY'
import json
man = [json.loads(l) for l in open("/tmp/e2e_manifest.jsonl")]
for m in man:
    if m["case"] == "conv8" and m["turn"] == 6:
        print("  lark-cli drive files search --query '%s' --as user" % m["nonce"])
        print("  （每个 nonce 单独直搜；搜不到 = 门②那一项红）")
PY

say "跑完了。三处判据对不上就判红，别只看其中一处。"
