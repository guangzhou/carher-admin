#!/usr/bin/env bash
# infra-doc-facts-check.sh —— 复核六份 AI Infra 飞书文档引用的核心数字是否仍然成立
#
# 背景：2026-08 写的六份文档（架构/指标/介绍/评估/商业化/团队）都标注了实测日期。
#      仓库里一份 2026-05 的文档曾因数据过期把人带偏（写「阿里云无公网入口」，实测早已不成立）。
#      这个脚本把那些数字变成可复跑的断言，避免文档静默腐烂。
#
# 用法：
#   bash scripts/infra-doc-facts-check.sh            # 只跑本地可测项
#   bash scripts/infra-doc-facts-check.sh --remote   # 含需要堡垒机的线上项（需 scripts/jms 可用）
#
# 退出码：0=全部符合，1=有偏差需更新文档
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

DRIFT=0
ok(){   printf '  \033[32m✓\033[0m %-46s %s\n' "$1" "$2"; }
bad(){  printf '  \033[31m✗\033[0m %-46s %s\n' "$1" "$2"; DRIFT=1; }
note(){ printf '  \033[33m·\033[0m %-46s %s\n' "$1" "$2"; }

# 断言：实际值落在容差区间内（规模类数字每天都在变，用区间而非等值）
chk(){ # name actual lo hi doc_value
  if [ "$2" -ge "$3" ] && [ "$2" -le "$4" ]; then
    ok "$1" "实际 $2（文档 $5，容差 $3-$4）"
  else
    bad "$1" "实际 $2，超出容差 $3-$4 —— 文档写的是 $5，需更新"
  fi
}

echo "=== 1. 本地可测：代码库规模（文档四·当前状态 / 文档六·规模基线）==="
COMMITS=$(git log --since=90.days --oneline --all 2>/dev/null | wc -l | tr -d ' ')
AUTHORS=$(git log --since=90.days --pretty='%an' --all 2>/dev/null | sort -u | wc -l | tr -d ' ')
AI=$(git log --since=90.days --all --grep='Co-Authored-By' --oneline 2>/dev/null | wc -l | tr -d ' ')
SCRIPTS=$(git ls-tree -r --name-only main -- scripts 2>/dev/null | grep -cE '\.(py|sh)$')
LINES=$(git ls-tree -r --name-only main -- scripts 2>/dev/null | grep -E '\.(py|sh)$' \
        | while read -r f; do git show "main:$f" 2>/dev/null; done | wc -l | tr -d ' ')

chk "近90天提交数（全ref）"      "$COMMITS" 150 230 "180"
chk "scripts 脚本数（main tracked）" "$SCRIPTS" 200 260 "224"
chk "scripts 行数（main tracked）"   "$LINES"  45000 62000 "52,373"
if [ "$AUTHORS" -eq 1 ]; then
  bad "提交者身份数" "仍为 1 —— bus factor 未改善（文档四的核心论据仍成立）"
else
  ok  "提交者身份数" "已增至 $AUTHORS —— bus factor 已改善，请更新文档四"
fi
[ "$COMMITS" -gt 0 ] && note "其中带 AI 协作标记" "$AI 次（约 $((AI*100/COMMITS))%，文档写 79%）"

echo
echo "=== 2. 本地可测：仓库 drift 台账（文档一·2.8）==="
if grep -rqE 'chatgpt-acct-(7[0-8])\.carher\.svc' k8s/litellm-proxy.yaml 2>/dev/null; then
  bad "litellm-proxy.yaml 的 acct entry" "仍指向 acct-70~78（线上实际为 122/124/125/126）—— drift 未修"
else
  ok  "litellm-proxy.yaml 的 acct entry" "已不含 acct-70~78，drift 可能已修复，请复核文档一"
fi
CB_SYNC=$(python3 - <<'PYEOF' 2>/dev/null
import re,sys
try: s=open("scripts/sync-litellm-callbacks.py").read()
except Exception: print(0); sys.exit()
m=re.search(r"CALLBACK_FILES[^=]*=\s*\((.*?)\)", s, re.S)
print(len(re.findall(r'"[^"]+\.py"', m.group(1))) if m else 0)
PYEOF
); CB_SYNC=${CB_SYNC:-0}
if [ "${CB_SYNC}" -eq 9 ]; then
  ok  "callback 同步脚本管辖文件数" "${CB_SYNC} 个（与文档一一致；YAML 内有 11 个 inline block，差额即未纳管的 2 个）"
else
  bad "callback 同步脚本管辖文件数" "${CB_SYNC} 个，文档一记 9 —— 二者已不一致，需复核"
fi

echo
echo "=== 3. 需要线上核对的项（本地无法判定）==="
if [ "${1:-}" = "--remote" ] && [ -x scripts/jms ]; then
  echo "  （--remote 模式：以下需人工在堡垒机执行并比对）"
else
  note "跳过线上项" "加 --remote 查看清单"
fi
cat <<'REMOTE'
  待核对清单（对应文档中的承重数字）：
    · 阿里云 55 模型组 / 99 entry / 6 通路      → kubectl get cm litellm-config -n carher
    · 198   130 模型组 / 1111 entry / 12 通路   → curl /v1/model_info（198 侧）
    · 账号池 69 Deployment（54 跑）/ 阿里云 8（4 跑）
    · fallback 33 条中 16 条目标缺失            → 文档一·2.8、文档二·O1 的承重数据
    · gpt 类流量 83.6% 来自消费级订阅池          → 文档五的地基数字，口径见该文第一节
    · 无冗余模型组 87% / 81%                    → 文档二·2.1
REMOTE

echo
if [ "$DRIFT" -eq 1 ]; then
  echo "结论：检出偏差，相关文档需要更新。"; exit 1
else
  echo "结论：本地可测项全部符合文档记载。"; exit 0
fi
