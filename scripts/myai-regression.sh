#!/usr/bin/env bash
# myai-regression.sh —— 合入前的回归门禁：全量 pytest 与基线比"失败集合不新增"，
# 并按业务分组出结果；真库那批必须跑，跑不了就显式报告跳过了几条。
#
# 用法:
#   scripts/myai-regression.sh gate                      # 当前 worktree 的 HEAD（默认动作）
#   scripts/myai-regression.sh gate feat/alipay-qr-manual   # 指定分支（自动找它的 worktree）
#   scripts/myai-regression.sh gate --all                # 4 条并行分支 + 主分支逐个过
#   scripts/myai-regression.sh baseline                  # 重算基线（写 BASELINE，默认 ../baseline.txt）
#   scripts/myai-regression.sh pg up|down|status         # 起/停/看本地测试 PG（billing + ops 两个库）
#
#   可调: REPO=<myai 仓根，默认 .work/myai-admin-perf/ai_token_dashboard>
#         VENV=<python，默认 $REPO/.venv/bin/python>   BASELINE=<默认 $REPO/../baseline.txt>
#         OUT=<JSON 输出目录，默认 $REPO/../.regression>
#         BILLING_TEST_DATABASE_URL / OPS_TEST_DATABASE_URL  已设则直接用，不去碰 docker
#         REQUIRE_PG=0  显式允许"没有真库也算过"（默认 1：缺真库 = 门禁不过）
#
# 判据（照 myai-release.sh 的写法，别重造）:
#   - pytest 有失败就 rc=1，pipefail + set -e 会把脚本静默杀掉 ⇒ rc 一律吞掉，
#     但输出里必须有 "N passed"，否则是 pytest 自己没跑起来（这个要算失败）。
#   - 过线条件是"失败集合不比基线多"，不是全绿：基线里的已知失败不拦发布。
#   - 🔴 真库测试默认 skipif（BILLING_TEST_DATABASE_URL / OPS_TEST_DATABASE_URL 为空就整模块跳过）。
#     85 条资金/运维桶用例会静默变成 skipped，总结里只看 "0 failed" 就是假绿 ——
#     2026-10-31 实测主分支：不配 PG 是 15 failed，配上真库是 35 failed，多出来的 20 条
#     全在 tests/test_billing_routes.py。所以这里把 skip 数单独拎出来报，缺真库默认直接判不过。
#
# 退出码: 0 = 过；1 = 有新增失败 / 真库缺失（REQUIRE_PG=1）；2 = 用法或环境错误（pytest 没跑起来等）
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="${REPO:-$ROOT/.work/myai-admin-perf/ai_token_dashboard}"
BASELINE="${BASELINE:-$REPO/../baseline.txt}"
OUT="${OUT:-$REPO/../.regression}"
REQUIRE_PG="${REQUIRE_PG:-1}"
PG_BILLING_PORT="${PG_BILLING_PORT:-15442}"
PG_OPS_PORT="${PG_OPS_PORT:-15443}"
PG_BILLING_NAME=myai-billing-test-pg
PG_OPS_NAME=myai-ops-test-pg
PG_IMAGE=postgres:16-alpine

# 分组：名字 => pytest 的文件选择模式。资金/运维/同步/前端四类，剩下的归 other。
GROUP_NAMES=(money ops sync frontend)
group_glob() {
  case "$1" in
    money)    echo "tests/test_billing_*.py tests/test_organization_*.py tests/test_alipay_*.py tests/test_customer_organization_*.py" ;;
    ops)      echo "tests/test_ops_*.py" ;;
    sync)     echo "tests/test_usage_*.py" ;;
    frontend) echo "tests/test_frontend_*.py" ;;
  esac
}

die()  { echo "ERROR: $*" >&2; exit 2; }
note() { echo "[regression] $*"; }

command -v git >/dev/null || die "缺 git"
[ -d "$REPO/.git" ] || die "没有本地仓库 $REPO"

# ---------- 本地测试 PG ----------
# 🔴 端口不能只看 docker：15432 曾被别的会话的 ssh -L 隧道占着，docker 的监听排在后面，
#    连上去是那条隧道的对端 ⇒ asyncpg 报 role "postgres" does not exist，看着像库坏了。
#    所以 up 之前先验端口没被别人占，且只绑 127.0.0.1。
port_taken_by_other() {
  local port="$1"
  lsof -nP -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $1 !~ /^com\.docke/ {print $1; exit}'
}

pg_dsn_billing() { echo "postgresql://postgres:testpw@127.0.0.1:$PG_BILLING_PORT/billing_test"; }
pg_dsn_ops()     { echo "postgresql://postgres:testpw@127.0.0.1:$PG_OPS_PORT/ops_test"; }

# 🔴 这里一律 return 非 0，不要 die：调用方 ensure_pg 要能"起不来就降级报告"。
#    die 会把整个门禁打成 rc=2（看起来像环境坏了），而真正该说的是
#    "真库缺失，这一轮结果不足以判回归"。
pg_up() {
  command -v docker >/dev/null || { echo "起不了测试 PG：缺 docker（也可自己配好 *_TEST_DATABASE_URL 再跑）" >&2; return 1; }
  docker info >/dev/null 2>&1 || { echo "起不了测试 PG：docker daemon 没起来（Docker Desktop 要先打开）" >&2; return 1; }
  local blocker spec port rest name db existing published
  for spec in "$PG_BILLING_PORT:$PG_BILLING_NAME:billing_test" "$PG_OPS_PORT:$PG_OPS_NAME:ops_test"; do
    port="${spec%%:*}"; rest="${spec#*:}"; name="${rest%%:*}"; db="${rest#*:}"
    blocker="$(port_taken_by_other "$port")"
    [ -z "$blocker" ] || { echo "起不了测试 PG：端口 ${port} 被 ${blocker} 占着（不是 docker）；换 PG_BILLING_PORT/PG_OPS_PORT 再跑" >&2; return 1; }
    existing="$(docker ps -aq -f "name=^${name}$")"
    if [ -n "$existing" ]; then
      # 复用旧容器前先核对它发布的端口就是这轮要用的那个：docker start 按容器创建时的
      # 端口起来，和 PG_*_PORT 不一致时 DSN 指向的根本不是它。
      published="$(docker inspect -f '{{range $conf := .HostConfig.PortBindings}}{{range $conf}}{{.HostPort}}{{end}}{{end}}' "$name" 2>/dev/null)"
      if [ "$published" != "$port" ]; then
        docker rm -f "$name" >/dev/null 2>&1; existing=""
      else
        docker start "$name" >/dev/null 2>&1 || true
      fi
    fi
    if [ -z "$existing" ]; then
      docker run -d --name "$name" -e POSTGRES_PASSWORD=testpw -e POSTGRES_DB="$db" \
        -p "127.0.0.1:$port:5432" "$PG_IMAGE" >/dev/null \
        || { echo "起不了测试 PG：docker run ${name} 失败（端口 ${port} 可能不可用）" >&2; return 1; }
    fi
  done
  # 等到真的能连（pg_isready 在容器里，本机不一定有 psql）
  local i
  for i in $(seq 1 40); do
    if docker exec "$PG_BILLING_NAME" pg_isready -U postgres -d billing_test >/dev/null 2>&1 \
       && docker exec "$PG_OPS_NAME" pg_isready -U postgres -d ops_test >/dev/null 2>&1; then
      note "测试 PG 就绪：$PG_BILLING_PORT(billing) / $PG_OPS_PORT(ops)"; return 0
    fi
    sleep 1
  done
  echo "测试 PG 起了但连不上（看 docker logs ${PG_BILLING_NAME}）" >&2
  return 1
}

pg_down()   { docker rm -f "$PG_BILLING_NAME" "$PG_OPS_NAME" >/dev/null 2>&1; note "测试 PG 已删"; }
pg_status() { docker ps -a --filter "name=myai-.*-test-pg" --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true; }

# 真库 DSN：已给就用给的；否则本机起一套。拿不到就按 REQUIRE_PG 决定是否算不过。
PG_READY=0
ensure_pg() {
  if [ -n "${BILLING_TEST_DATABASE_URL:-}" ] && [ -n "${OPS_TEST_DATABASE_URL:-}" ]; then
    PG_READY=1; note "用环境里给的 *_TEST_DATABASE_URL"; return 0
  fi
  if pg_up; then
    export BILLING_TEST_DATABASE_URL="$(pg_dsn_billing)"
    export OPS_TEST_DATABASE_URL="$(pg_dsn_ops)"
    PG_READY=1; return 0
  fi
  # 起失败就确保两个 DSN 都是空的：半个 DSN 会让 skipif 只跳一半，
  # 结果看着"有在跑真库"其实没有。
  unset BILLING_TEST_DATABASE_URL OPS_TEST_DATABASE_URL
  PG_READY=0
  return 1
}

# 🔴 真库开关不止我知道的那两个：分支会自带新的。feat/org-settlement 就引入了
#    ORG_SETTLEMENT_TEST_DSN，不认它就有 7 条日结用例默默跳过 —— 又一次"默认 skip
#    导致假绿"。所以这里从目标分支自己的 tests/ 里把所有 *_DSN / *_DATABASE_URL
#    开关扫出来，缺哪个就点名哪个，而不是只检查硬编码的两个。
# $1 工作目录；输出到全局 DSN_GATES_MISSING（空格分隔）
scan_dsn_gates() {
  local wd="$1" name
  DSN_GATES_MISSING=""
  for name in $(grep -rhoE 'os\.getenv\("[A-Z0-9_]*(DSN|DATABASE_URL)[A-Z0-9_]*"' "$wd/tests" 2>/dev/null \
                 | sed -E 's/.*"([A-Z0-9_]+)"/\1/' | sort -u); do
    [ -n "$(eval "printf '%s' \"\${$name:-}\"")" ] || DSN_GATES_MISSING="${DSN_GATES_MISSING:+$DSN_GATES_MISSING }$name"
  done
  # ORG_SETTLEMENT_TEST_DSN 要的是"可写的空库"，本机测试库已经起着，直接给它一个独立库。
  local g
  for g in $DSN_GATES_MISSING; do
    case "$g" in
      ORG_SETTLEMENT_TEST_DSN)
        if [ "$PG_READY" = 1 ] && [ -n "$(docker ps -q -f "name=^${PG_BILLING_NAME}$" 2>/dev/null)" ]; then
          docker exec "$PG_BILLING_NAME" psql -U postgres -q -c 'CREATE DATABASE org_settlement_test' >/dev/null 2>&1 || true
          export ORG_SETTLEMENT_TEST_DSN="postgresql://postgres:testpw@127.0.0.1:$PG_BILLING_PORT/org_settlement_test"
          note "补上 ORG_SETTLEMENT_TEST_DSN（org_settlement_test 库）"
        fi ;;
    esac
  done
  # 补过之后重算一遍，剩下的才是真缺
  DSN_GATES_MISSING=""
  for name in $(grep -rhoE 'os\.getenv\("[A-Z0-9_]*(DSN|DATABASE_URL)[A-Z0-9_]*"' "$wd/tests" 2>/dev/null \
                 | sed -E 's/.*"([A-Z0-9_]+)"/\1/' | sort -u); do
    [ -n "$(eval "printf '%s' \"\${$name:-}\"")" ] || DSN_GATES_MISSING="${DSN_GATES_MISSING:+$DSN_GATES_MISSING }$name"
  done
}

# ---------- 跑一次 pytest，回显 "失败集合 / 计数" ----------
# $1 工作目录  $2.. pytest 选择参数（空 = tests 全量）
# 输出到全局：RUN_FAILED_SET / RUN_PASSED / RUN_FAILED / RUN_SKIPPED / RUN_TAIL
run_pytest() {
  local wd="$1"; shift
  local -a sel=()
  if [ "$#" -gt 0 ]; then
    # glob 要在目标 worktree 里展开；没匹配到文件的模式直接丢掉，否则 pytest 报 4
    local pat f
    for pat in "$@"; do
      for f in $(cd "$wd" && ls -d $pat 2>/dev/null); do sel+=("$f"); done
    done
    [ "${#sel[@]}" -gt 0 ] || { RUN_FAILED_SET=""; RUN_PASSED=0; RUN_FAILED=0; RUN_SKIPPED=0; RUN_TAIL="(该分组没有匹配的用例文件)"; return 0; }
  else
    sel=(tests)
  fi
  local out
  # 🔴 rc 一律吞掉（见文件头判据）；真正的"没跑起来"由下面的 passed 行判。
  out="$(cd "$wd" && "$VENV" -m pytest "${sel[@]}" -q -p no:cacheprovider 2>&1 || true)"
  RUN_TAIL="$(printf '%s\n' "$out" | tail -3)"
  if ! printf '%s\n' "$out" | grep -qE "[0-9]+ (passed|skipped)"; then
    RUN_FAILED_SET=""; RUN_PASSED=-1; RUN_FAILED=-1; RUN_SKIPPED=-1
    RUN_TAIL="$(printf '%s\n' "$out" | tail -8)"
    return 1
  fi
  # 🔴 不能只匹配 ^(FAILED|ERROR)：应用日志里的 "ERROR    ai-token-dashboard:main.py:..."
  #    也顶格以 ERROR 开头，且带 trade_no/user_id 这类每次都变的串 ⇒ 永远匹配不上基线，
  #    会被当成"新增失败"，让门禁对 4 条分支一律判不过（实测每条都多报 22 条）。
  #    这里要求后面跟 pytest 的用例标识 tests/...::，只收真正的用例结果行。
  RUN_FAILED_SET="$(printf '%s\n' "$out" | grep -E '^(FAILED|ERROR) tests/[^[:space:]]*::' | sed 's/ - .*//' | sort -u)"
  RUN_PASSED="$(printf  '%s\n' "$out" | grep -oE '[0-9]+ passed'  | tail -1 | grep -oE '[0-9]+' || echo 0)"
  RUN_SKIPPED="$(printf '%s\n' "$out" | grep -oE '[0-9]+ skipped' | tail -1 | grep -oE '[0-9]+' || echo 0)"
  RUN_FAILED="$(printf '%s\n' "$RUN_FAILED_SET" | grep -c . || true)"
  return 0
}

json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | tr -d '\r' | awk '{printf "%s%s", (NR>1 ? "\\n" : ""), $0}'; }
json_arr() {  # 每行一个元素
  local first=1 line
  printf '['
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    [ $first -eq 1 ] || printf ','
    printf '"%s"' "$(json_str "$line")"; first=0
  done
  printf ']'
}

# ---------- 找分支的 worktree ----------
# 分支可能 checkout 在别的 worktree 里（本仓就是这么用的）。git 不允许同一分支被两处 checkout，
# 所以这里只找、不切：找不到就报错让人自己建，绝不在别人的 worktree 上动手。
worktree_for() {
  local branch="$1" wt
  wt="$(git -C "$REPO" worktree list --porcelain | awk -v b="refs/heads/$branch" '
    /^worktree /{wt=substr($0,10)} /^branch /{if (substr($0,8)==b) {print wt; exit}}')"
  [ -z "$wt" ] || { printf '%s\n' "$wt"; return 0; }
  # 没有以该分支 checkout 的 worktree 时，退一步找一个 HEAD 正好等于该分支提交的
  # worktree（通常是 detached 的那个）。只读、不切、不建分支 —— 内容逐字节一致，
  # 用来跑测试等价。
  local want cand dirty_cand
  want="$(git -C "$REPO" rev-parse --verify -q "$branch^{commit}")" || return 1
  # 优先挑干净的：带未提交改动的 worktree 跑出来的结果不代表这个提交。
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    if [ -z "$(git -C "$cand" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
      printf '%s\n' "$cand"; return 0
    fi
    dirty_cand="${dirty_cand:-$cand}"
  done <<EOF_WT
$(git -C "$REPO" worktree list --porcelain | awk -v want="$want" '
    /^worktree /{wt=substr($0,10)} /^HEAD /{if (substr($0,6)==want) print wt}')
EOF_WT
  [ -z "$dirty_cand" ] || { printf '%s\n' "$dirty_cand"; return 0; }
  return 1
}

# ---------- 门禁主体 ----------
gate_one() {
  local branch="$1" wd rc=0
  if [ "$branch" = "@HEAD" ]; then
    wd="$(pwd)"
    git -C "$wd" rev-parse --git-dir >/dev/null 2>&1 || wd="$REPO"
    branch="$(git -C "$wd" rev-parse --abbrev-ref HEAD)"
  else
    wd="$(worktree_for "$branch")"
    [ -n "$wd" ] || { echo "!!! 分支 $branch 没有对应的 worktree；先 git worktree add 再跑（本脚本不切别人的工作区）" >&2; return 2; }
  fi
  local sha; sha="$(git -C "$wd" rev-parse --short HEAD)"
  local dirty=false
  [ -z "$(git -C "$wd" status --porcelain --untracked-files=no)" ] || dirty=true
  note "=== $branch @ $sha  ($wd)${dirty:+ }$([ "$dirty" = true ] && echo '⚠️ 有未提交改动')"

  # 这个分支自带哪些真库开关？缺的要点名（见 scan_dsn_gates 的说明）
  scan_dsn_gates "$wd"

  # 全量
  if ! run_pytest "$wd"; then
    echo "!!! pytest 没跑起来：" >&2; printf '%s\n' "$RUN_TAIL" >&2; return 2
  fi
  local all_set="$RUN_FAILED_SET" all_passed="$RUN_PASSED" all_failed="$RUN_FAILED" all_skipped="$RUN_SKIPPED"

  # 与基线比：只看"新增"，基线里的已知失败不拦
  local extra="" base_count=0
  if [ -f "$BASELINE" ]; then
    base_count="$(grep -cE '^(FAILED|ERROR) tests/[^[:space:]]*::' "$BASELINE" || true)"
    extra="$(comm -13 <(grep -E '^(FAILED|ERROR) tests/[^[:space:]]*::' "$BASELINE" | sed 's/ - .*//' | sort -u) \
                     <(printf '%s\n' "$all_set" | grep -v '^$' | sort -u) || true)"
  else
    note "⚠️ 缺基线 ${BASELINE}（跑 baseline 动作生成）；这一轮只报失败数，不做新增比对"
  fi

  # 分组
  local group_json="" g
  for g in "${GROUP_NAMES[@]}"; do
    run_pytest "$wd" $(group_glob "$g") || true
    printf -v _ '' 2>/dev/null || true
    note "  [$g] passed=$RUN_PASSED failed=$RUN_FAILED skipped=$RUN_SKIPPED"
    group_json="$group_json$([ -n "$group_json" ] && echo ,)\"$g\":{\"passed\":$RUN_PASSED,\"failed\":$RUN_FAILED,\"skipped\":$RUN_SKIPPED,\"failures\":$(printf '%s\n' "$RUN_FAILED_SET" | json_arr)}"
  done

  # 结论
  local verdict=pass reason=""
  if [ "$all_failed" -gt 0 ] && [ -f "$BASELINE" ] && [ -n "$extra" ]; then
    verdict=fail; reason="新增失败 $(printf '%s\n' "$extra" | grep -c .) 条"
  elif [ ! -f "$BASELINE" ] && [ "$all_failed" -gt 0 ]; then
    verdict=fail; reason="无基线且有 $all_failed 条失败"
  fi
  if [ "$PG_READY" != 1 ]; then
    if [ "$REQUIRE_PG" = 1 ]; then
      verdict=fail; reason="${reason:+${reason}；}没有真库：资金/运维桶用例被 skipif 跳过，这一轮结果不足以判回归"
    else
      reason="${reason:+${reason}；}REQUIRE_PG=0，真库缺失仅告警"
    fi
  elif [ -n "${DSN_GATES_MISSING:-}" ]; then
    # 真库起着，但这个分支还要别的库开关（如 ORG_SETTLEMENT_TEST_DSN）⇒ 那批用例在跳过
    if [ "$REQUIRE_PG" = 1 ]; then
      verdict=fail; reason="${reason:+${reason}；}缺真库开关 ${DSN_GATES_MISSING}，对应用例被 skipif 跳过"
    else
      reason="${reason:+${reason}；}缺真库开关 ${DSN_GATES_MISSING}（REQUIRE_PG=0，仅告警）"
    fi
  fi

  echo "    全量: passed=${all_passed} failed=${all_failed} skipped=${all_skipped}（基线 ${base_count} 条已知失败）"
  if [ "$all_skipped" -gt 0 ]; then
    # 🔴 skip 必须说清是什么被跳过了，不能让它静静地变成"0 failed"
    if [ -n "${DSN_GATES_MISSING:-}" ]; then
      echo "    ⚠️ 跳过 ${all_skipped} 条；缺这些真库开关：${DSN_GATES_MISSING}"
    else
      echo "    ⚠️ 跳过 ${all_skipped} 条；真库开关都已配（跳过的是别的原因，用 pytest -rs 看）"
    fi
  fi
  # printf '%s\n' $(...) 会按空白再切一次词，把 "FAILED tests/x.py::y" 拆成两行；用 while 逐行读
  [ -z "$extra" ] || { echo "    新增失败："; printf '%s\n' "$extra" | grep -v '^$' | while IFS= read -r l; do echo "      $l"; done; }
  echo "    结论: ${verdict}${reason:+（${reason}）}"

  mkdir -p "$OUT"
  local jf="$OUT/$(printf '%s' "$branch" | tr '/' '_').json"
  cat > "$jf" <<JSON
{
  "branch": "$(json_str "$branch")",
  "commit": "$sha",
  "worktree": "$(json_str "$wd")",
  "dirty": $dirty,
  "ranAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "realDatabase": { "ready": $([ "$PG_READY" = 1 ] && echo true || echo false),
                    "required": $([ "$REQUIRE_PG" = 1 ] && echo true || echo false),
                    "billingDsnConfigured": $([ -n "${BILLING_TEST_DATABASE_URL:-}" ] && echo true || echo false),
                    "opsDsnConfigured": $([ -n "${OPS_TEST_DATABASE_URL:-}" ] && echo true || echo false),
                    "missingDsnGates": $(printf '%s\n' ${DSN_GATES_MISSING:-} | json_arr) },
  "total": { "passed": $all_passed, "failed": $all_failed, "skipped": $all_skipped },
  "baseline": { "path": "$(json_str "$BASELINE")", "present": $([ -f "$BASELINE" ] && echo true || echo false), "knownFailures": $base_count },
  "failures": $(printf '%s\n' "$all_set" | json_arr),
  "newFailures": $(printf '%s\n' "$extra" | json_arr),
  "groups": { $group_json },
  "verdict": "$verdict",
  "reason": "$(json_str "$reason")"
}
JSON
  note "  JSON: $jf"
  [ "$verdict" = pass ] || rc=1
  return $rc
}

make_baseline() {
  local wd="${1:-$REPO}"
  run_pytest "$wd" || die "pytest 没跑起来：$RUN_TAIL"
  { printf '%s passed, %s failed, %s skipped  (生成于 %s, %s @ %s)\n' \
      "$RUN_PASSED" "$RUN_FAILED" "$RUN_SKIPPED" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "$(git -C "$wd" rev-parse --abbrev-ref HEAD)" "$(git -C "$wd" rev-parse --short HEAD)"
    printf '%s\n' "$RUN_FAILED_SET"; } > "$BASELINE"
  note "基线已写 ${BASELINE}（${RUN_FAILED} 条已知失败）"
}

ACTION="${1:-gate}"; shift || true
VENV="${VENV:-$REPO/.venv/bin/python}"
[ -x "$VENV" ] || die "没有可执行的 python：$VENV"

case "$ACTION" in
  pg)
    case "${1:-status}" in
      up) pg_up ;; down) pg_down ;; status) pg_status ;;
      *) die "用法: $0 pg up|down|status" ;;
    esac ;;
  baseline)
    ensure_pg || note "⚠️ 没有真库，基线里不会包含资金/运维桶用例的结果"
    make_baseline "$(worktree_for "${1:-}" 2>/dev/null || echo "$REPO")" ;;
  gate)
    ensure_pg || true
    rc=0
    if [ "${1:-}" = "--all" ]; then
      for b in perf/admin-board-latency feat/alipay-qr-manual feat/org-settlement \
               feat/ops-trends-p1-upstream feat/ops-alerts-p2; do
        gate_one "$b" || rc=$?
      done
    else
      gate_one "${1:-@HEAD}" || rc=$?
    fi
    echo
    if [ $rc -eq 0 ]; then note "门禁通过"; else note "门禁未通过（rc=${rc}），详见上面的新增失败与 ${OUT}/*.json"; fi
    exit $rc ;;
  *) die "用法: $0 gate [<分支>|--all] | baseline [<分支>] | pg up|down|status" ;;
esac
