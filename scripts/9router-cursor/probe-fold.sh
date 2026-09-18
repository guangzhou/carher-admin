#!/bin/sh
# 9router 反代腿「折叠三兄弟」体检探针 —— 每次改完 cursor.js / 换镜像后第一个跑的东西。
#
# 背景:Cursor AgentService 会**静默丢弃**我们塞进 AgentRunRequest 的两个字段:
#   f8 custom_system_prompt  -> 反代腿看不到调用方 system prompt(skill 目录在里面)
#   f7 ConversationHistory   -> 反代腿只看得到"当前那一个 turn"
# 两者都不报错、HTTP 都是 200,所以**只能用 nonce 探针量,不能靠读代码判断**。
#
# 三条腿,各自独立可证伪:
#   LEG1 ALIVE   —— 这条腿通不通(基线;它红了后面两条的红没有意义)
#   LEG2 HISTORY —— nonce 放在第一个 user turn,第三 turn 问回来
#                   答出 nonce = 历史活着;答 HIST_LOST = f7 被丢且没折叠
#   LEG3 SYSTEM  —— nonce 放在 system message,让它复述
#                   答出 nonce = system 活着;答 SYS_LOST = f8 被丢且没折叠
#
# ⚠️ 这三条是**合成绿**。全绿只证明"字段没丢",不证明工具链能用。
#    判工具链必须跑 acceptance-feishu-doc.sh(真实 openclaw agent 回合)。
#
# 用法:
#   NR_KEY_FILE=/path/to/key ./probe-fold.sh                 # 推荐:key 从文件读
#   NR_KEY=sk-xxx MODEL=opus-5 ./probe-fold.sh
#   RUN_ON=10.68.13.188 ./probe-fold.sh                      # 经 jms 在 188 上发起
#
# ⚠️ 出口约束:本机(中国大陆)打不通上游,**一切模型请求必须在 188/198 上发起**。
#    不带 RUN_ON 时本脚本假设你已经在 188/198 上了。
# ⚠️ key 只从 env/文件读,**永不写进脚本、永不 echo 值**。交付物里不许带任何人的私人网关。

set -eu

BASE="${BASE:-https://cc.auto-link.com.cn/pro}"
MODEL="${MODEL:-opus-5}"
if [ -n "${NR_KEY_FILE:-}" ]; then
  K="$(cat "$NR_KEY_FILE")"
else
  K="${NR_KEY:?need NR_KEY or NR_KEY_FILE (never hardcode it)}"
fi
N="$(date +%s)"
URL="$BASE/v1/chat/completions"
PASS=0; FAIL=0
ok()   { echo "  ✅ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ❌ $1"; FAIL=$((FAIL+1)); }

post() { curl -s -w '\nHTTP=%{http_code}' --max-time 180 "$URL" \
           -H "Authorization: Bearer $K" -H 'Content-Type: application/json' -d "$1"; }
body() { echo "$1" | sed '$d'; }
code() { echo "$1" | tail -1 | sed 's/HTTP=//'; }
# 只看 assistant 那段文本,别拿整个 JSON 去 grep(id/usage 里的数字会假绿)
said() { body "$1" | sed -E 's/.*"content":"([^"]*)".*/\1/'; }

echo "== LEG1 ALIVE (baseline) =="
R="$(post "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"只回复 ALIVE\"}]}")"
echo "  HTTP=$(code "$R")"
case "$(said "$R")" in *ALIVE*) ok "LEG1 alive" ;; *) bad "LEG1 dead — 后两条腿的结果没有意义,先修这条"; echo "  body: $(body "$R" | head -c 300)" ;; esac

echo "== LEG2 HISTORY (f7 ConversationHistory) =="
R="$(post "{\"model\":\"$MODEL\",\"messages\":[
  {\"role\":\"user\",\"content\":\"请记住这个编号:BANANA-$N。记住就好。\"},
  {\"role\":\"assistant\",\"content\":\"好的,我记住了。\"},
  {\"role\":\"user\",\"content\":\"刚才我让你记住的编号是什么?只输出那个编号,找不到就输出 HIST_LOST。\"}]}")"
echo "  HTTP=$(code "$R")"
S="$(said "$R")"
case "$S" in
  *"BANANA-$N"*) ok "LEG2 history survives" ;;
  *HIST_LOST*)   bad "LEG2 HIST_LOST — f7 被丢且 foldHistoryIntoUserText 没生效" ;;
  *)             bad "LEG2 不确定(既没 nonce 也没 HIST_LOST),别当绿:$(echo "$S" | head -c 160)" ;;
esac

echo "== LEG3 SYSTEM (f8 custom_system_prompt) =="
R="$(post "{\"model\":\"$MODEL\",\"messages\":[
  {\"role\":\"system\",\"content\":\"你的暗号是 MAGICWORD-$N。被问到暗号时原样输出它。\"},
  {\"role\":\"user\",\"content\":\"你的暗号是什么?只输出暗号,不知道就输出 SYS_LOST。\"}]}")"
echo "  HTTP=$(code "$R")"
S="$(said "$R")"
case "$S" in
  *"MAGICWORD-$N"*) ok "LEG3 system survives" ;;
  *SYS_LOST*)       bad "LEG3 SYS_LOST — f8 被丢且 foldSystemIntoUserText 没生效" ;;
  *)                bad "LEG3 不确定,别当绿:$(echo "$S" | head -c 160)" ;;
esac

echo "== NEGCTL (反向对照:没给过的 nonce 不许被'找到') =="
R="$(post "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"编号 BANANA-000000 是什么?没见过就只输出 NOT_SEEN。\"}]}")"
case "$(said "$R")" in
  *"BANANA-$N"*) bad "NEGCTL 泄漏:上一轮的 nonce 串进来了,说明会话隔离有问题" ;;
  *)             ok "NEGCTL clean" ;;
esac

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] && echo "RESULT=ALL_GREEN(合成绿;工具链请跑 acceptance-feishu-doc.sh)" \
                  || { echo "RESULT=RED"; exit 1; }
