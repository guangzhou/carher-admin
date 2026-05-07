#!/usr/bin/env bash
# carher-config-protect.template.sh
#
# 硬拦"agent 不该直接改"的 config 文件。命中即 exit 2，没有重试机会。
# 来源：affaan-m/everything-claude-code/scripts/hooks/config-protection.js
# 改造：加了 carher prod config（k8s yaml / cloudflare tunnels）
#
# 安装：
#   cp this-file .cursor/hooks/carher-config-protect.sh
#   chmod +x .cursor/hooks/carher-config-protect.sh
#
# 干跑测试：
#   echo '{"tool_name":"Edit","tool_input":{"file_path":"/path/to/.markdownlint.json"}}' \
#     | bash .cursor/hooks/carher-config-protect.sh
#   echo $?  # 期望 2

set -euo pipefail

# 读 stdin（cursor / claude hook 都用 stdin 传 JSON）
INPUT_JSON=$(cat)

# 没输入直接放行
if [ -z "$INPUT_JSON" ]; then
  exit 0
fi

# 解析 file_path（用 python 而不是 jq，因为 jq 不一定在所有环境）
FILE_PATH=$(echo "$INPUT_JSON" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get("tool_input", {}).get("file_path", ""))
except Exception:
    print("")
' 2>/dev/null || echo "")

if [ -z "$FILE_PATH" ]; then
  exit 0
fi

BASENAME=$(basename "$FILE_PATH")

# ─────────────────────────────────────────────
# 死保护清单 1：lint / formatter config（按 basename 匹配）
# ─────────────────────────────────────────────
PROTECTED_BASENAMES=(
  # ESLint
  ".eslintrc" ".eslintrc.js" ".eslintrc.cjs" ".eslintrc.json"
  ".eslintrc.yml" ".eslintrc.yaml"
  "eslint.config.js" "eslint.config.mjs" "eslint.config.cjs"
  "eslint.config.ts" "eslint.config.mts" "eslint.config.cts"
  # Prettier
  ".prettierrc" ".prettierrc.js" ".prettierrc.cjs" ".prettierrc.json"
  ".prettierrc.yml" ".prettierrc.yaml"
  "prettier.config.js" "prettier.config.cjs" "prettier.config.mjs"
  # Biome
  "biome.json" "biome.jsonc"
  # Ruff (注意：pyproject.toml 不进来，因为同时是依赖管理)
  ".ruff.toml" "ruff.toml"
  # Shell / Style / Markdown
  ".shellcheckrc"
  ".stylelintrc" ".stylelintrc.json" ".stylelintrc.yml"
  ".markdownlint.json" ".markdownlint.yaml" ".markdownlintrc"
  # Test config（agent 经常想改这些来"过测试"）
  "pytest.ini"
  ".coveragerc"
)

for protected in "${PROTECTED_BASENAMES[@]}"; do
  if [ "$BASENAME" = "$protected" ]; then
    cat >&2 <<EOF
BLOCKED: Modifying $BASENAME is not allowed.

Reason: agents often change linter/formatter configs to make checks pass
instead of fixing the actual code. This hook redirects you to the source.

If this is a legitimate config change (e.g. adopting a new rule), do one of:
  1. Disable temporarily:  CARHER_PROTECT_CONFIG=0 <retry>
  2. Edit the file manually outside the agent loop
  3. Add the rule to the carher-config-protect allowlist with explicit reason
EOF
    [ "${CARHER_PROTECT_CONFIG:-1}" = "1" ] && exit 2 || exit 0
  fi
done

# ─────────────────────────────────────────────
# 死保护清单 2：carher prod config（按路径片段匹配，不是 basename）
# ─────────────────────────────────────────────
case "$FILE_PATH" in
  */k8s/litellm-proxy.yaml \
  |*/k8s/litellm-proxy.yml \
  |*/cloudflare/tunnels/*.json \
  |*/k8s/her-instance-template.yaml)
    cat >&2 <<EOF
BLOCKED: $FILE_PATH is a production resource definition.

Reason: changing prod probes / resource limits / tunnel routing without
fact-forcing investigation has caused outages before.

Allowed paths to make this change:
  1. First read the current state via kubectl describe / kubectl get
  2. Use the carher-fact-force hook (which will demand the same investigation)
  3. Or temporarily disable: CARHER_PROTECT_CONFIG=0
     (you'll be reminded — this is intentional friction, not a real lock)

See skill: agent-safety-hooks → references/protected-files-catalog.md
EOF
    [ "${CARHER_PROTECT_CONFIG:-1}" = "1" ] && exit 2 || exit 0
    ;;
esac

# 默认放行
exit 0
