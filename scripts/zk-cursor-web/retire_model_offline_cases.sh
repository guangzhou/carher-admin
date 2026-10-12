#!/bin/sh
# retire_model_offline_cases.sh —— 模型清单**整表赋值**(EXACT)+ --upgrade 档的离线台架。
#
# 为什么要单独一条腿:`mergeConfig` 的 dedup 原来是**纯并集**,没有删除路径 ——
# 从 DEFAULT_MODELS 里删掉一个名字只影响新装机器,已装的同事库里那个名字会**永远留着**
# (2026-09-20 下架 cr-g-5.6-pro 时就是这个形状:文档说没了,他菜单里还在,一点就报错)。
# 所以判据不能是"常量里没有它了",必须是**真跑一遍安装器、再把库读出来看那个名字在不在**。
#
# 2026-09-20 第二轮语义翻转:用户点名「除文档里那份之外全部去掉,包括他自己配的乱七八糟的」。
# 于是 mergeConfig 从并集改成**整表赋值**:userAddedModels / modelOverrideEnabled 直接等于
# DEFAULT_MODELS(顺序也一样)。**case ⑤ 的期望因此反过来了** —— 从"第三方名一个都不许动"
# 变成"第三方名必须被清掉、钉在第三方名上的功能位必须改回 DEFAULT_MODEL"。
# 保留这条历史注释是为了:下次看到 git blame 里 ⑤ 被反转,别以为是谁写错了想"修回去"。
#
# 全程只碰临时目录:假 app(CURSOR_APP_ROOT)+ 假 user dir(CURSOR_USER_DIR)+ 假 HOME。
# live Cursor / 真 state.vscdb / 真备份目录一个字节都不动。
#
#   sh retire_model_offline_cases.sh
#
# case:
#   ① 退役名在 userAddedModels 里 → 跑完必须没了
#   ② 退役名在 modelOverrideEnabled 里 → 跑完必须没了
#   ③ 退役名正被 composer 选中 → modelName 必须改回 DEFAULT_MODEL(不是留着一个点了报错的名字)
#   ④ 退役名被非 composer 功能位(deep-search)选中 → 一样要改回来(按值扫,不只扫 composer/cmd-k)
#   ⑤ 同事自己加的第三方名 → **必须被清掉**(整表赋值),钉着它的功能位改回 DEFAULT_MODEL
#   ⑥ --upgrade 不问 Key、不覆盖已有 Key,且新名全部进库(幂等重跑无变化)
#   ⑦ 两个数组 == DEFAULT_MODELS **逐字且按序**(菜单顺序 = 数组顺序,用户点名 Grok 排最前)
#   ⑧ 写库**之前**必须落一份 `<ver>-<ts>-preconfig/applicationUser.blob.json`
#      (破坏性那一步的正前方要有快照,否则旧清单只存在过内存里)
#   ⑨ --uninstall 必须把他装前自己加的名字**还回去**。整表赋值之后"库里留下的就是他的"
#      这个前提不成立了(装的时候就清了),只 dropOurs 会让他卸完剩一份空清单 ——
#      而他以为"恢复原状"了。判据 = 卸完之后第三方名回来了、我们的名字没了。
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
# RETIRE_JS= 指向一个改坏的副本 = 阳性对照(把摘除那段删掉,本台架必须变红)。
# 没有这个钩子的话"全绿"只证明这一版是绿的,证不出台架有判别力。
JS="${RETIRE_JS:-$HERE/cursor_team_setup.js}"
APP="${CURSOR_APP:-/Applications/Cursor.app}"
LIVE_RES="$APP/Contents/Resources/app"
TMP="$(mktemp -d)"
FAIL=0
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
ok()  { echo "  PASS  $*"; }
bad() { echo "  FAIL  $*"; FAIL=$((FAIL+1)); }

CXNODE=""
if [ -x "$APP/Contents/MacOS/Cursor" ]; then CXNODE="$APP/Contents/MacOS/Cursor"
else for c in /usr/share/cursor/cursor /opt/cursor/cursor; do [ -x "$c" ] && CXNODE="$c" && break; done; fi
if [ -z "$CXNODE" ]; then
  echo "!! 找不到 Cursor 可执行文件(CURSOR_APP=$APP)——本台架要用它的 node:sqlite 读写 state.vscdb" >&2
  exit 1
fi
# node:sqlite 是 Cursor 自带 Electron 才有的;下面所有读库/跑安装器都走它。
NODE="env ELECTRON_RUN_AS_NODE=1 $CXNODE"

# 常量从安装器本体抠(不写死 —— 写死的话下次改名这台架会对着旧真相全绿)。
# 注意:**每一次调 Cursor 可执行文件都必须带 ELECTRON_RUN_AS_NODE=1**,漏一次就不是跑 node
# 而是**弹一个 Cursor 窗口然后永远挂住**(第一版就这么挂的,120s 超时零输出)。
RET1=$(env ELECTRON_RUN_AS_NODE=1 CX_DUMP_CONSTANTS=1 "$CXNODE" "$JS" 2>/dev/null \
  | env ELECTRON_RUN_AS_NODE=1 "$CXNODE" -e '
let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
  try{process.stdout.write((JSON.parse(s).RETIRED_MODELS||[])[0]||"")}catch(e){}});')
DEF=$(grep -m1 '^const DEFAULT_MODEL = ' "$JS" | sed 's/.*"\(.*\)".*/\1/')
# ⑦ 要按序比,所以把整份 DEFAULT_MODELS 也抠出来(同样不写死,走 CX_DUMP_CONSTANTS)。
MODELS_JSON=$(env ELECTRON_RUN_AS_NODE=1 CX_DUMP_CONSTANTS=1 "$CXNODE" "$JS" 2>/dev/null \
  | env ELECTRON_RUN_AS_NODE=1 "$CXNODE" -e '
let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
  try{process.stdout.write(JSON.stringify(JSON.parse(s).DEFAULT_MODELS||[]))}catch(e){}});')
[ -n "$RET1" ] && [ -n "$DEF" ] || { echo "!! 抠不到 RETIRED_MODELS / DEFAULT_MODEL" >&2; exit 1; }
# 这把尺子自己先得量得到东西:空数组会让 ⑦ 变成"两个空的相等"= 恒绿。
case "$MODELS_JSON" in [][]|"") echo "!! 抠不到 DEFAULT_MODELS(拿到 $MODELS_JSON)——⑦ 会恒绿,拒绝往下跑" >&2; exit 1 ;; esac
echo "退役名=$RET1  默认名=$DEF  菜单=$(echo "$MODELS_JSON" | tr -cd ',' | wc -c | tr -d ' ') 个逗号"
echo ""

# ── 造一个假 app:bundle 必须是**真 bundle 的副本** ──
# 不能拿空文件糊弄:空壳 → 锚点命中=0 → applyPatchesToText 直接 `process.exit(2)`,
# **根本走不到 mergeConfig**,于是本台架每一条都红,红的理由还跟被测行为无关
# (第一版就是这个形状:12 项全红,日志里只有"AST 定位命中=0 → 拒绝动手")。
# 真 bundle 复制进来后:pristine → 正常打补丁;已打过 → planBundle 返回 null 跳过。两种都能往下走。
# 三个 case 共用同一个假 app:第一次跑把补丁打进副本,后面几次自然走"已打过"分支,省两次 AST 解析。
FAKE="$TMP/app"
mkdir -p "$FAKE/out/vs/workbench"
V="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$APP/Contents/Resources/app/package.json" | head -1)"
echo "{\"version\":\"${V:-0.0.0}\"}" > "$FAKE/package.json"
for b in workbench.desktop.main.js workbench.glass.main.js; do
  [ -f "$LIVE_RES/out/vs/workbench/$b" ] || { echo "!! 找不到 $LIVE_RES/out/vs/workbench/$b" >&2; exit 1; }
  cp "$LIVE_RES/out/vs/workbench/$b" "$FAKE/out/vs/workbench/$b"
done

# ── 造 state.vscdb 的工具函数 ──
seed_db() {  # $1=userdir  $2=applicationUser blob JSON
  mkdir -p "$1/globalStorage"
  rm -f "$1/globalStorage/state.vscdb"
  BLOB="$2" DBP="$1/globalStorage/state.vscdb" $NODE -e '
    const {DatabaseSync}=require("node:sqlite");
    const db=new DatabaseSync(process.env.DBP);
    db.exec("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)");
    db.prepare("INSERT INTO ItemTable(key,value) VALUES(?,?)").run(
      "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser",
      process.env.BLOB);
    db.close();'
}
read_blob() {  # $1=userdir -> stdout: blob JSON
  DBP="$1/globalStorage/state.vscdb" $NODE -e '
    const {DatabaseSync}=require("node:sqlite");
    const db=new DatabaseSync(process.env.DBP);
    const r=db.prepare("SELECT value FROM ItemTable WHERE key=?").get(
      "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser");
    process.stdout.write(r?String(r.value):"");db.close();'
}
jq_get() {  # $1=blob json  $2=js 表达式(以 d 为根)
  BLOB="$1" EXPR="$2" $NODE -e '
    const d=JSON.parse(process.env.BLOB);
    const v=eval(process.env.EXPR);
    process.stdout.write(typeof v==="string"?v:JSON.stringify(v));'
}
run_installer() {  # $1=userdir  $2..=args
  UD="$1"; shift
  env ELECTRON_RUN_AS_NODE=1 HOME="$TMP/home" CURSOR_APP_ROOT="$FAKE" CURSOR_USER_DIR="$UD" \
      CX_SKIP_RUNNING_CHECK=1 "$CXNODE" "$JS" "$@" --no-zk-delta --force-version
}
mkdir -p "$TMP/home"

# ── ①②③④⑤ 一次种入,一次跑完,分别判 ──
UD="$TMP/u1"
seed_db "$UD" "$(cat <<JSON
{"openAIBaseUrl":"https://cc.auto-link.com.cn/pro/v1","useOpenAIKey":true,
 "aiSettings":{
   "userAddedModels":["$RET1","$DEF","my-own-gpt5","claude-opus-4.6"],
   "modelOverrideEnabled":["$RET1","$DEF","my-own-gpt5"],
   "modelConfig":{
     "composer":{"modelName":"$RET1","maxMode":true,
       "selectedModels":[{"modelId":"$RET1","parameters":[]}]},
     "cmd-k":{"modelName":"$DEF","selectedModels":[{"modelId":"$DEF","parameters":[]}]},
     "deep-search":{"modelName":"$RET1","selectedModels":[{"modelId":"$RET1","parameters":[]}]},
     "spec":{"modelName":"default","selectedModels":[{"modelId":"default","parameters":[]}]},
     "plan-execution":{"modelName":"my-own-gpt5",
       "selectedModels":[{"modelId":"my-own-gpt5","parameters":[]}]}
   }}}
JSON
)"
LOG="$TMP/run1.log"
run_installer "$UD" --apply >"$LOG" 2>&1 || true
B="$(read_blob "$UD")"
if [ -z "$B" ]; then bad "①-⑤ 跑完读不到 blob(见 $LOG)"; cp "$LOG" /tmp/retire-run1.log; else

UAM="$(jq_get "$B" 'd.aiSettings.userAddedModels')"
OVE="$(jq_get "$B" 'd.aiSettings.modelOverrideEnabled')"
case "$UAM" in *"\"$RET1\""*) bad "① userAddedModels 里还留着退役名 $RET1:$UAM" ;;
  *) ok "① userAddedModels 已摘掉 $RET1" ;; esac
case "$OVE" in *"\"$RET1\""*) bad "② modelOverrideEnabled 里还留着 $RET1:$OVE" ;;
  *) ok "② modelOverrideEnabled 已摘掉 $RET1" ;; esac

CN="$(jq_get "$B" 'd.aiSettings.modelConfig.composer.modelName')"
CS="$(jq_get "$B" 'd.aiSettings.modelConfig.composer.selectedModels')"
[ "$CN" = "$DEF" ] && ok "③ composer 选中位 $RET1 → $DEF" || bad "③ composer 还钉着 $CN(菜单里已经没这个名字了)"
case "$CS" in *"\"$RET1\""*) bad "③ composer.selectedModels 里还留着 $RET1:$CS" ;;
  *) ok "③ composer.selectedModels 已换成 $DEF" ;; esac
# 兄弟字段不许被顺手抹掉(实测 cmd-k 有 maxMode 这类同级设置)
MM="$(jq_get "$B" 'String(d.aiSettings.modelConfig.composer.maxMode)')"
[ "$MM" = "true" ] && ok "③ composer.maxMode 兄弟字段保住了" || bad "③ composer.maxMode 被抹了(=$MM)"

DN="$(jq_get "$B" 'd.aiSettings.modelConfig["deep-search"].modelName')"
[ "$DN" = "$DEF" ] && ok "④ deep-search(非 composer/cmd-k)也改回 $DEF" || bad "④ deep-search 还钉着 $DN —— 只扫了 composer/cmd-k"

# ⑤ 整表赋值:第三方名必须被清掉(09-20 用户点名,期望与上一版相反)
case "$UAM" in *'"my-own-gpt5"'*) bad "⑤ 第三方名 my-own-gpt5 还在库里(整表赋值没生效):$UAM" ;;
  *) ok "⑤ 第三方名 my-own-gpt5 已清掉" ;; esac
case "$UAM" in *'"claude-opus-4.6"'*) bad "⑤ 第三方名 claude-opus-4.6 还在库里:$UAM" ;;
  *) ok "⑤ 第三方名 claude-opus-4.6 已清掉" ;; esac
case "$OVE" in *'"my-own-gpt5"'*) bad "⑤ modelOverrideEnabled 里还留着 my-own-gpt5:$OVE" ;;
  *) ok "⑤ modelOverrideEnabled 里第三方名也清掉了" ;; esac
# 清掉名字但功能位还钉着它 = 他一发消息就报错,且菜单里找不到那个名字 ⇒ 必须 re-point。
PN="$(jq_get "$B" 'd.aiSettings.modelConfig["plan-execution"].modelName')"
[ "$PN" = "$DEF" ] && ok "⑤ plan-execution 钉的第三方名已改回 $DEF" \
  || bad "⑤ plan-execution 还钉着 $PN —— 名字被清掉了却没 re-point,点了就报错"
# 新名必须真进库(否则"删对了"可能只是因为整段没跑)。
# 🔴 2026-09-22:这三个名字原来是**写死**的(sa-grok-4.6-latest / sa-composer-2.5-fast /
#    qwen3-coder-next)。菜单切裸名那天它们当场变成两条红 —— 红的理由是台架陈旧,不是产品坏。
#    写死名字的台架只在"常量没变"时是对的,而它存在的意义恰恰是守常量变更。改成从
#    MODELS_JSON 里取首/中/末三个,常量怎么改都跟得上。
SAMPLE3="$(echo "$MODELS_JSON" | env ELECTRON_RUN_AS_NODE=1 "$CXNODE" -e '
let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
  const a=JSON.parse(s);
  process.stdout.write([a[0],a[Math.floor(a.length/2)],a[a.length-1]].join(" "))});')"
for m in $SAMPLE3; do
  case "$UAM" in *"\"$m\""*) ok "① 新名 $m 已进库" ;; *) bad "① 新名 $m 没进库:$UAM" ;; esac
done
# ⑦ 逐字且按序 == DEFAULT_MODELS。顺序是用户的显式要求(Grok + sa-composer 排最前),
#    只比集合会让"顺序被打乱"这种回归静默通过。
[ "$UAM" = "$MODELS_JSON" ] && ok "⑦ userAddedModels 逐字按序 == DEFAULT_MODELS" \
  || bad "⑦ userAddedModels 与 DEFAULT_MODELS 不是逐字按序相等
        实际=$UAM
        期望=$MODELS_JSON"
[ "$OVE" = "$MODELS_JSON" ] && ok "⑦ modelOverrideEnabled 逐字按序 == DEFAULT_MODELS" \
  || bad "⑦ modelOverrideEnabled 与 DEFAULT_MODELS 不是逐字按序相等
        实际=$OVE"
# ⑧ 破坏性那一步的正前方要有快照。目录名带 -preconfig,HOME 已经指到 $TMP/home。
PRE="$(ls -d "$TMP/home/.cursor-team-setup-backup/"*-preconfig 2>&1 | head -1)"
if [ -d "$PRE" ] && [ -s "$PRE/applicationUser.blob.json" ]; then
  # 快照必须是**旧**清单(含第三方名),否则"备份了"等于备份了改完的结果。
  if grep -q 'my-own-gpt5' "$PRE/applicationUser.blob.json"; then
    ok "⑧ 写库前的快照存在且是旧清单($PRE)"
  else
    bad "⑧ 快照在但不含旧的第三方名 —— 备份的是改完之后的样子,还不回去"
  fi
else
  bad "⑧ 找不到写库前的 *-preconfig 快照($PRE)—— 整表替换没有退路"
fi
fi

# ── ⑥ --upgrade:不问 Key、不覆盖、幂等 ──
echo ""
UD2="$TMP/u2"
seed_db "$UD2" "$(cat <<JSON
{"openAIBaseUrl":"https://cc.auto-link.com.cn/pro/v1","useOpenAIKey":true,
 "aiSettings":{"userAddedModels":["$RET1","$DEF"],"modelOverrideEnabled":["$RET1"],
 "modelConfig":{"composer":{"modelName":"$DEF","selectedModels":[{"modelId":"$DEF","parameters":[]}]}}}}
JSON
)"
# 库里放一条假的 Key secret:--upgrade 必须读到它、报"原样保留",且**不能覆盖**
KEYROW='{"type":"Buffer","data":[1,2,3]}'
DBP="$UD2/globalStorage/state.vscdb" KR="$KEYROW" $NODE -e '
  const {DatabaseSync}=require("node:sqlite");
  const db=new DatabaseSync(process.env.DBP);
  db.prepare("INSERT INTO ItemTable(key,value) VALUES(?,?)").run("secret://cursorAuth/openAIKey",process.env.KR);
  db.close();'
U1="$TMP/up1.log"; U2="$TMP/up2.log"
# 关键:**不给 --apply、不给 --key、stdin 关掉**。--upgrade 自己就该落盘且一个字不问。
run_installer "$UD2" --upgrade </dev/null >"$U1" 2>&1 || true
grep -q '库里已有 Key,原样保留' "$U1" && ok "⑥ --upgrade 读到已有 Key,明说保留" || { bad "⑥ --upgrade 没报出「已有 Key」(见 $U1)"; cp "$U1" /tmp/retire-up1.log; }
grep -q '请粘贴你的 API Key' "$U1" && bad "⑥ --upgrade 还在问 Key" || ok "⑥ --upgrade 全程没问 Key"
grep -q '\[dry-run\]' "$U1" && bad "⑥ --upgrade 只预演没落盘(应隐含 --apply)" || ok "⑥ --upgrade 隐含 --apply(真落盘)"
B2="$(read_blob "$UD2")"
UAM2="$(jq_get "$B2" 'd.aiSettings.userAddedModels')"
case "$UAM2" in *"\"$RET1\""*) bad "⑥ --upgrade 没摘掉 $RET1:$UAM2" ;; *) ok "⑥ --upgrade 摘掉了 $RET1" ;; esac
KEYNOW="$(DBP="$UD2/globalStorage/state.vscdb" $NODE -e '
  const {DatabaseSync}=require("node:sqlite");
  const db=new DatabaseSync(process.env.DBP);
  const r=db.prepare("SELECT value FROM ItemTable WHERE key=?").get("secret://cursorAuth/openAIKey");
  process.stdout.write(r?String(r.value):"");db.close();')"
[ "$KEYNOW" = "$KEYROW" ] && ok "⑥ 已有 Key 一个字节没动" || bad "⑥ Key 被改了:$KEYNOW"
# 幂等:再跑一次,blob 必须逐字节相同
run_installer "$UD2" --upgrade </dev/null >"$U2" 2>&1 || true
B3="$(read_blob "$UD2")"
[ "$B2" = "$B3" ] && ok "⑥ 重跑幂等(blob 逐字节相同)" || bad "⑥ 重跑改了东西(不幂等)"

# ── ⑥b 没配过 Key 的机器跑 --upgrade:必须明说,不许只写"完成" ──
UD3="$TMP/u3"
seed_db "$UD3" "{\"aiSettings\":{\"userAddedModels\":[\"$DEF\"]}}"
U3="$TMP/up3.log"
run_installer "$UD3" --upgrade </dev/null >"$U3" 2>&1 || true
grep -q '库里\*\*没有\*\* Key' "$U3" && ok "⑥b 没 Key 的机器明确报出来" || { bad "⑥b 没 Key 却没报(见 $U3)"; cp "$U3" /tmp/retire-up3.log; }
grep -q 'Settings → Models → OpenAI API Key' "$U3" && ok "⑥b 并告诉他下一步怎么做" || bad "⑥b 没给下一步"

# ── ⑨ --uninstall 把他装前自己加的名字还回去 ──
# 直接复用 u1(①-⑧ 那台):它已经装过一遍,库里现在只剩我们那 20 个,
# 他的 my-own-gpt5 / claude-opus-4.6 只存在于 -preconfig 快照里。
echo ""
UN="$TMP/un1.log"
run_installer "$UD" --uninstall --apply </dev/null >"$UN" 2>&1 || true
B9="$(read_blob "$UD")"
if [ -z "$B9" ]; then bad "⑨ 卸完读不到 blob(见 $UN)"; cp "$UN" /tmp/retire-un1.log; else
UAM9="$(jq_get "$B9" 'd.aiSettings.userAddedModels')"
case "$UAM9" in *'"my-own-gpt5"'*) ok "⑨ 卸载把他装前的 my-own-gpt5 还回来了" ;;
  *) bad "⑨ 卸完他自己加的 my-own-gpt5 没回来(他会以为恢复原状了):$UAM9" ;; esac
case "$UAM9" in *'"claude-opus-4.6"'*) ok "⑨ 卸载把 claude-opus-4.6 还回来了" ;;
  *) bad "⑨ 卸完 claude-opus-4.6 没回来:$UAM9" ;; esac
# 还回去不等于把我们的名字也留着 —— 那就不是卸载了。
# 🔴 这里原来写死 sa-grok-4.6-latest:菜单切裸名后那个名字根本不会被装,
#    于是"没找到"恒真、这条腿退化成恒绿。改成查当前的 DEFAULT_MODEL($DEF)。
case "$UAM9" in *"\"$DEF\""*) bad "⑨ 卸完我们的名字($DEF)还在库里:$UAM9" ;;
  *) ok "⑨ 我们的名字($DEF)摘干净了" ;; esac
UB9="$(jq_get "$B9" 'String(d.useOpenAIKey)')"
[ "$UB9" = "false" ] && ok "⑨ useOpenAIKey 回到 false" || bad "⑨ useOpenAIKey=$UB9"
fi

# ── ⑩ --bare-sa:菜单去掉 sa- 前缀 ──────────────────────────────────────────
# 这条腿的存在理由:`--bare-sa` 随包发给 686 个人,而**能不能用是 key 属性不是名字属性**。
# 它必须做到两件事,少一件就是给人一个点了报错的菜单:
#   · sa-* 那几个真的被换成裸名(而不是"加了裸名、sa- 还留着"——那样菜单里一个模型两份)
#   · **非 sa- 的名字一个都不许动**(cr-g-*/gpt-* 跟这个旗标无关,被顺手改了就是越界)
# 还有一件在这里量不到、由 setup_impl_parity.py 守着:它不许改 DEFAULT_MODELS 常量本身
# (改了就是把全员默认也翻了,而不是本机开关)。
echo ""
UD10="$TMP/u10"
DEF_BARE="$(printf '%s' "$DEF" | sed 's/^sa-//')"
seed_db "$UD10" '{"openAIBaseUrl":"https://cc.auto-link.com.cn/pro/v1","useOpenAIKey":true,
 "aiSettings":{"userAddedModels":[],"modelOverrideEnabled":[],"modelConfig":{}}}'
L10="$TMP/run10.log"
run_installer "$UD10" --bare-sa --apply </dev/null >"$L10" 2>&1 || true
B10="$(read_blob "$UD10")"
if [ -z "$B10" ]; then bad "⑩ --bare-sa 跑完读不到 blob(见 $L10)"; cp "$L10" /tmp/retire-run10.log; else
UAM10="$(jq_get "$B10" 'd.aiSettings.userAddedModels')"
# 尺子自检:清单为空的话下面"没有 sa-"会恒绿。
N10="$(jq_get "$B10" 'String(d.aiSettings.userAddedModels.length)')"
if [ "${N10:-0}" -lt 10 ]; then bad "⑩ 菜单只有 $N10 个 —— 下面几条会恒绿,拒绝当绿看(见 $L10)"; else
  ok "⑩ 菜单写进去了($N10 个)"
  case "$UAM10" in *'"sa-'*) bad "⑩ 还留着 sa- 开头的名字(应全部去前缀):$UAM10" ;;
    *) ok "⑩ 菜单里 sa- 前缀一个不剩" ;; esac
  case "$UAM10" in *"\"$DEF_BARE\""*) ok "⑩ 裸名 $DEF_BARE 进了菜单" ;;
    *) bad "⑩ 裸名 $DEF_BARE 没进菜单:$UAM10" ;; esac
  case "$UAM10" in *'"composer-2.5-fast"'*) ok "⑩ 裸名 composer-2.5-fast 进了菜单" ;;
    *) bad "⑩ composer-2.5-fast 没去掉前缀:$UAM10" ;; esac
  # 越界检查:跟 sa- 无关的名字必须原样还在。
  # 🔴 2026-10-12 换过一次金丝雀:原来用 `cr-g-5.6`,而那一轮整个 cr-g 段被点名下架 ⇒
  #    它不在菜单里了,这条腿于是**恒红且理由指错方向**(报成"--bare-sa 越界",
  #    真相是金丝雀自己退役了)。金丝雀必须挑一个**当前菜单里真有、且不以 sa- 开头**的名字。
  case "$UAM10" in *'"claude-opus-5-5"'*) ok "⑩ 非 sa- 名字(claude-opus-5-5)没被顺手动过" ;;
    *) bad "⑩ claude-opus-5-5 丢了 —— --bare-sa 越界改了跟它无关的名字:$UAM10" ;; esac
  case "$UAM10" in *'"qwen3-coder-next"'*) ok "⑩ 非 sa- 名字(qwen3-coder-next)没被顺手动过" ;;
    *) bad "⑩ qwen3-coder-next 丢了:$UAM10" ;; esac
  # 选中位必须钉在**裸的**默认名上,否则装完第一下就点在一个菜单里没有的名字上
  CN10="$(jq_get "$B10" 'd.aiSettings.modelConfig.composer.modelName')"
  [ "$CN10" = "$DEF_BARE" ] && ok "⑩ composer 选中位 = $DEF_BARE" \
    || bad "⑩ composer 钉着 $CN10,而菜单里只有裸名 —— 装完就点不动"
fi
fi

# ── ⑪ 老用户走 INSTALL(而不是 UPGRADE):上一代 sa-* 菜单必须被整份换掉 ──────────
# 用户点名(09-22):「除了 UPGRADE 脚本更新模型,老用户如果用 install 脚本,也要做到完整
# 用我们的新列表,新列表里不要有原来的模型。」
# 这条腿和 ①-⑤ 不是一回事:①-⑤ 种的是"第三方名 + 当代名",证的是 EXACT 语义;
# 这里种的是**上一代我们自己的菜单**(`sa-grok-*` / `sa-composer-*`),证的是
# **换代**那一刀在 INSTALL 路径上也落下来了。两者会因为不同的 bug 分开红:
# 比如有人为了"别动老用户的名字"给 sa- 开头的名字加一条豁免,①-⑤ 照样全绿,这条会红。
# 判据三条:① 库里 `"sa-` 一个不剩 ② 两个数组逐字按序 == DEFAULT_MODELS
#          ③ 钉着 sa- 名的功能位改回了 $DEF(不然他一发消息就点在一个菜单里没有的名字上)
echo ""
UD11="$TMP/u11"
OLDGEN='"sa-grok-4.6-latest","sa-grok-4.6","sa-grok-4.5-latest","sa-grok-4.5","sa-grok-4.20","sa-composer-2.5-fast"'
seed_db "$UD11" "{\"openAIBaseUrl\":\"https://cc.auto-link.com.cn/pro/v1\",\"useOpenAIKey\":true,
 \"aiSettings\":{
   \"userAddedModels\":[$OLDGEN,\"cr-g-5.6\"],
   \"modelOverrideEnabled\":[$OLDGEN,\"cr-g-5.6\"],
   \"modelConfig\":{
     \"composer\":{\"modelName\":\"sa-grok-4.6-latest\",\"selectedModels\":[{\"modelId\":\"sa-grok-4.6-latest\",\"parameters\":[]}]},
     \"cmd-k\":{\"modelName\":\"sa-composer-2.5-fast\",\"selectedModels\":[{\"modelId\":\"sa-composer-2.5-fast\",\"parameters\":[]}]}
   }}}"
L11="$TMP/run11.log"
# **不带 --upgrade** —— 走的就是 INSTALL-Mac.command / INSTALL-Windows.cmd 那条路(`--apply`)。
run_installer "$UD11" --apply </dev/null >"$L11" 2>&1 || true
B11="$(read_blob "$UD11")"
if [ -z "$B11" ]; then bad "⑪ INSTALL 跑完读不到 blob(见 $L11)"; cp "$L11" /tmp/retire-run11.log; else
UAM11="$(jq_get "$B11" 'd.aiSettings.userAddedModels')"
OVE11="$(jq_get "$B11" 'd.aiSettings.modelOverrideEnabled')"
N11="$(jq_get "$B11" 'String(d.aiSettings.userAddedModels.length)')"
# 尺子自检:清单为空会让"没有 sa-"恒绿。
if [ "${N11:-0}" -lt 10 ]; then bad "⑪ 菜单只有 $N11 个 —— 下面几条会恒绿,拒绝当绿看(见 $L11)"; else
  ok "⑪ INSTALL 把菜单写进去了($N11 个)"
  case "$UAM11" in *'"sa-'*) bad "⑪ 老用户走 INSTALL 之后库里还留着上一代 sa- 名:$UAM11" ;;
    *) ok "⑪ 上一代 sa- 名在 userAddedModels 里一个不剩" ;; esac
  case "$OVE11" in *'"sa-'*) bad "⑪ modelOverrideEnabled 里还留着 sa- 名:$OVE11" ;;
    *) ok "⑪ modelOverrideEnabled 里 sa- 名也清干净了" ;; esac
  [ "$UAM11" = "$MODELS_JSON" ] && ok "⑪ INSTALL 后逐字按序 == DEFAULT_MODELS(完整用新列表)" \
    || bad "⑪ INSTALL 后与 DEFAULT_MODELS 不是逐字按序相等
        实际=$UAM11"
  CN11="$(jq_get "$B11" 'd.aiSettings.modelConfig.composer.modelName')"
  [ "$CN11" = "$DEF" ] && ok "⑪ composer 从 sa-grok-4.6-latest 改回 $DEF" \
    || bad "⑪ composer 还钉着 $CN11 —— 那个名字菜单里已经没有了,他一发消息就报错"
  KN11="$(jq_get "$B11" 'd.aiSettings.modelConfig["cmd-k"].modelName')"
  [ "$KN11" = "$DEF" ] && ok "⑪ cmd-k 从 sa-composer-2.5-fast 改回 $DEF" \
    || bad "⑪ cmd-k 还钉着 $KN11"
fi
fi

echo ""
if [ "$FAIL" -gt 0 ]; then echo "❌ $FAIL 项失败"; exit 1; fi
echo "✅ 全过(整表赋值:清单逐字按序 == DEFAULT_MODELS · 退役名/第三方名/上一代 sa- 名全摘净且选中位 re-point · INSTALL 与 UPGRADE 两条路都换代 · 写库前有快照 · --upgrade 不问 Key 且幂等 · --bare-sa 只动 sa- 那几个)"
