#!/usr/bin/env bash
# check-no-personal-paths.sh
#
# 防止把本机绝对路径带进可分发的 docs / skills / rules / k8s / backend。
# 直接可用，不需要改名，挂到 GitHub Actions / pre-commit / 本地 git hook。
#
# 来源：affaan-m/everything-claude-code/scripts/ci/validate-no-personal-paths.js
# 改造：扫描清单换成 carher-admin 的目录布局
#
# 用法：
#   bash .cursor/skills/agent-safety-hooks/scripts/check-no-personal-paths.sh
#
# 退出码：
#   0 = OK
#   1 = 命中个人路径（命中文件 + 行号会写到 stderr）

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"

# 要扫描的目标
TARGETS=(
  "docs"
  ".cursor/skills"
  ".cursor/rules"
  "k8s"
  "backend/tests"
  "AGENTS.md"
  "README.md"
  "CLAUDE.md"
)

# 个人路径模式（按需加）
PATTERNS=(
  '/Users/Liuguoxian'
  '/Users/liuguoxian'
  '/home/liuguoxian'
)

# 只检查这些扩展名
EXT_REGEX='\.(md|json|js|ts|py|sh|toml|yml|yaml)$'

# 自身豁免：本脚本和 agent-safety-hooks skill 内部文档需要把"个人路径模式"
# 作为字符串字面量保存（PATTERNS 数组、举例描述），这些是元数据不是 leak。
# 注意：豁免严格限定在 agent-safety-hooks/ 子树内，不会泄露到其他 skill。
SELF_EXEMPT_REGEX='\.cursor/skills/agent-safety-hooks/'

fail=0
checked=0

if ! command -v rg >/dev/null 2>&1; then
  echo "ERROR: ripgrep (rg) not found. Install via: brew install ripgrep" >&2
  exit 2
fi

for target in "${TARGETS[@]}"; do
  full="$ROOT/$target"
  [ -e "$full" ] || continue
  for pattern in "${PATTERNS[@]}"; do
    # rg --files-with-matches 会列出命中的文件
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      # 只看目标扩展名
      [[ "$f" =~ $EXT_REGEX ]] || continue
      # 跳过 .git / node_modules / .venv
      [[ "$f" =~ /(\.git|node_modules|\.venv|\.tox|__pycache__)/ ]] && continue
      # 跳过本 skill 自身（PATTERNS 数组定义和设计文档不能算 leak）
      [[ "$f" =~ $SELF_EXEMPT_REGEX ]] && continue
      # 显示命中行号
      rg -n --no-heading "$pattern" "$f" 2>/dev/null | while IFS= read -r line; do
        echo "ERROR: personal path in $(realpath --relative-to="$ROOT" "$f" 2>/dev/null || echo "$f"): $line"
      done
      fail=$((fail + 1))
    done < <(rg -l --no-ignore-vcs "$pattern" "$full" 2>/dev/null || true)
  done
  checked=$((checked + 1))
done

if [ "$fail" -gt 0 ]; then
  echo "" >&2
  echo "Found $fail file(s) with personal absolute paths." >&2
  echo "Replace them with one of:" >&2
  echo "  - relative paths (e.g. 'docs/foo.md' instead of '/Users/.../carher-admin/docs/foo.md')" >&2
  echo "  - environment variable placeholders (e.g. \$CARHER_ROOT)" >&2
  echo "  - generic examples (e.g. '/path/to/carher-admin')" >&2
  exit 1
fi

echo "OK: scanned $checked target(s), no personal absolute paths found"
exit 0
