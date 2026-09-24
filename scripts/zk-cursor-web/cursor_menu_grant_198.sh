#!/usr/bin/env bash
# cursor_menu_grant_198.sh —— 一键：把装机包菜单那 30 个名字配齐到 198 上全部 cursor-* key。
#
# 用户面症状：同事菜单里看得见模型，一点就 403 / 400。
# 根因是两道**互相独立**的闸：allowlist 缺 → 403；per-key alias 缺 → 400。
# 这个脚本两件一起办，且**默认只看不改**。
#
#   ./cursor_menu_grant_198.sh             # dry-run：印出每把 key 要补什么
#   ./cursor_menu_grant_198.sh --apply     # 真写
#   ./cursor_menu_grant_198.sh --apply --limit 5   # 先拿 5 把试点
#
# 动了什么 / 备份在哪 / 怎么回滚：
#   动：198 生产库 LiteLLM_VerificationToken 里未 blocked 的 cursor-* key，
#       给它们的 `models` 补缺名、给 3 个 alias-only 名字补 `aliases`。
#       **不删任何已有的名字或 alias**（读旧 → 并集 → 整份写回）。
#   备份：改动前的 (models, aliases) 整份落在 198 的 /tmp/cursor_menu_grant_backup.json，
#         跑完自动拉回本机 ./cursor_menu_grant_backup-<UTC>.json。
#   回滚：拿那份快照逐把 POST /key/update 写回 models_before / aliases_before。
#
# 名单来源是 cursor_team_setup.js 的 DEFAULT_MODELS（单一真源），带 ==30 断言：
# 手抄一份名单到这里 = 下次改装机包时两边悄悄分叉。
set -euo pipefail

HOST="${HOST:-cltx@10.68.13.198}"
NS=litellm-product
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_JS="$HERE/cursor_team_setup.js"
GRANT_PY="$HERE/cursor_menu_grant.py"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

PASSTHRU=()
for a in "$@"; do PASSTHRU+=("$a"); done
APPLY=no
for a in "${PASSTHRU[@]:-}"; do [ "$a" = "--apply" ] && APPLY=yes; done

MODE_LABEL=DRY-RUN; [ "$APPLY" = "yes" ] && MODE_LABEL=APPLY
[ -f "$SETUP_JS" ] || { echo "!! 找不到 $SETUP_JS"; exit 1; }
[ -f "$GRANT_PY" ] || { echo "!! 找不到 $GRANT_PY"; exit 1; }

echo "--- 1) 从装机包取菜单名单(单一真源 + 数量断言) ---"
python3 - "$SETUP_JS" > "/tmp/cursor_menu_names.txt" <<'PY'
import io, re, sys
js = io.open(sys.argv[1], encoding="utf-8").read()
m = re.search(r'const DEFAULT_MODELS\s*=\s*\[(.*?)\n\];', js, re.S)
assert m, "没找到 DEFAULT_MODELS —— 装机包结构变了,先去看它,别在这里放宽正则"
body = re.sub(r'//[^\n]*', '', m.group(1))      # 去掉行注释,否则注释里的字符串会被抠出来
names = re.findall(r'"([^"]+)"', body)
# 断言而不是"能抠多少算多少":松散抠出来的垃圾名会被原样授权进生产 key。
assert len(names) == 30, "DEFAULT_MODELS 抠出 %d 个,期望 30 —— 装机包改了就同步改这个断言" % len(names)
assert len(set(names)) == len(names), "名单里有重名"
print("\n".join(names))
PY
echo "   菜单名 $(wc -l < /tmp/cursor_menu_names.txt) 个"

echo "--- 2) 导出未 blocked 的 cursor-* key(alias + token hash) ---"
scp -q "/tmp/cursor_menu_names.txt" "$HOST:/tmp/cursor_menu_names.txt"
scp -q "$GRANT_PY" "$HOST:/tmp/cursor_menu_grant.py"

# shellcheck disable=SC2087  # 变量要在本地展开
ssh "$HOST" "bash -s" <<REMOTE
set -euo pipefail
# ⚠️ 写 -F'\t' 时传给 psql 的是字面两个字符 反斜杠+t,不是制表符,
#    后果是导出看着有 667 行、下游按真 TAB 切一行都切不出来 ⇒ "key 清单为空"(踩过)。
#    所以用 printf 在远端生成真 TAB。
#    ⚠️ 这段注释在**未引号 heredoc**里,不许写反引号 —— bash 会当命令替换执行(也踩过)。
sudo kubectl -n $NS exec litellm-db-0 -- psql -U litellm -d litellm -t -A -F"\$(printf '\t')" -c \
  "select key_alias, token from \"LiteLLM_VerificationToken\"
   where key_alias like 'cursor%' and coalesce(blocked,false)=false
   order by key_alias" > /tmp/cursor_keys.tsv
nkeys=\$(grep -cP '\t' /tmp/cursor_keys.tsv || true)
echo "   导出 \$(wc -l < /tmp/cursor_keys.tsv) 行,其中含真 TAB 的 \$nkeys 行"
[ "\$nkeys" -gt 0 ] || { echo "!! 导出的行里没有 TAB —— 分隔符坏了,停"; exit 1; }

POD=\$(sudo kubectl -n $NS get pod -l carher.net/litellm-production-route=enabled --no-headers -o custom-columns=N:.metadata.name,S:.status.phase \
      | awk '\$2=="Running"{print \$1; exit}')
[ -n "\$POD" ] || { echo "!! 找不到带路由标签的 Running proxy pod —— 判据失效,停"; exit 1; }
echo "   proxy pod = \$POD"

MK=\$(sudo kubectl -n $NS get secret litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
[ -n "\$MK" ] || { echo "!! 读不到 LITELLM_MASTER_KEY"; exit 1; }

# 名单和 key 表送进 pod(master key 不出集群)
sudo kubectl -n $NS cp /tmp/cursor_menu_names.txt \$POD:/tmp/names30.txt
sudo kubectl -n $NS cp /tmp/cursor_keys.tsv      \$POD:/tmp/keys.tsv
sudo kubectl -n $NS cp /tmp/cursor_menu_grant.py \$POD:/tmp/grant.py

echo "--- 3) 跑授权('$MODE_LABEL') ---"
sudo kubectl -n $NS exec \$POD -- env LITELLM_MASTER_KEY="\$MK" \
  python3 /tmp/grant.py --names-file /tmp/names30.txt --keys-file /tmp/keys.tsv \
  --backup-out /tmp/cursor_menu_grant_backup.json ${PASSTHRU[*]:-}
rc=\$?

sudo kubectl -n $NS cp \$POD:/tmp/cursor_menu_grant_backup.json /tmp/cursor_menu_grant_backup.json || true
exit \$rc
REMOTE
rc=$?

echo "--- 4) 拉回改动前快照 ---"
if scp -q "$HOST:/tmp/cursor_menu_grant_backup.json" "$HERE/cursor_menu_grant_backup-$STAMP.json"; then
  echo "   -> $HERE/cursor_menu_grant_backup-$STAMP.json"
  echo "   回滚：拿它逐把 POST /key/update 写回 models_before / aliases_before"
else
  echo "   (没有快照 —— 说明这一轮没有 key 需要改)"
fi

if [ "$APPLY" = "no" ]; then
  echo
  echo "以上是 DRY-RUN，一个字都没写。确认后重跑并加 --apply。"
fi
exit $rc
