#!/usr/bin/env bash
# litellm-198-router-patch-install.sh
#
# 在 198 母 router（ns litellm-product）上安装 / 校验 / 回滚一个
# **Router 内部猴补丁模块**（不是普通 CustomLogger hook）。
#
# 为什么需要这个脚本：198 装一个新补丁是**三步**，少任何一步都是静默失效——
#
#   1) `litellm-callbacks` CM 加一条 data
#   2) Deployment 的 containers[0].volumeMounts **加一条 subPath 挂载**
#      ← 最容易漏。母 router 每个补丁文件都是独立 subPath 挂进 /app/ 的，
#        只加 CM 的话容器内根本不存在这个文件，且不会有任何报错
#   3) `litellm-config` 里 config.yaml 的 litellm_settings.callbacks 加一行
#
# 并且 198 **禁止 kubectl apply**（仓库 manifest 陈旧，apply 会回退 image + 内嵌 CM），
# 只能用 patch / set image / rollout restart。本脚本全程只用 patch。
#
# 用法（在 198 上跑）：
#   ./litellm-198-router-patch-install.sh install <本地模块.py> <callbacks 条目>
#   ./litellm-198-router-patch-install.sh verify  <模块文件名.py>
#   ./litellm-198-router-patch-install.sh rollback <模块文件名.py> <callbacks 条目>
#
# 例：
#   ./litellm-198-router-patch-install.sh install \
#        k8s/litellm-callbacks/dead_deployment_retry.py \
#        dead_deployment_retry.dead_deployment_retry
#
# 退出码：0=成功  1=校验不通过  2=环境/参数错误
set -uo pipefail

NS="${NS:-litellm-product}"
DEPLOY="${DEPLOY:-litellm-proxy}"
CB_CM="${CB_CM:-litellm-callbacks}"
CFG_CM="${CFG_CM:-litellm-config}"
BACKUP_DIR="${BACKUP_DIR:-/root/ddr-backup}"

if [ -n "${KUBECTL:-}" ]; then
  read -r -a KUBECTL_CMD <<<"$KUBECTL"
elif command -v kubectl >/dev/null 2>&1; then
  KUBECTL_CMD=(kubectl)
else
  KUBECTL_CMD=(sudo k3s kubectl)
fi
k() { "${KUBECTL_CMD[@]}" "$@"; }

die() { echo "ERROR: $*" >&2; exit "${2:-2}"; }

pods() { k -n "$NS" get pod -l app="$DEPLOY" -o jsonpath='{.items[*].metadata.name}'; }

backup() {
  mkdir -p "$BACKUP_DIR"
  local ts; ts=$(date +%Y%m%d-%H%M%S)
  k -n "$NS" get cm "$CB_CM"  -o yaml > "$BACKUP_DIR/$CB_CM.$ts.yaml"  || die "备份 $CB_CM 失败"
  k -n "$NS" get cm "$CFG_CM" -o yaml > "$BACKUP_DIR/$CFG_CM.$ts.yaml" || die "备份 $CFG_CM 失败"
  k -n "$NS" get deploy "$DEPLOY" -o yaml > "$BACKUP_DIR/$DEPLOY.$ts.yaml" || die "备份 deploy 失败"
  echo "备份写入 $BACKUP_DIR（时间戳 $ts）"
}

# ---------------------------------------------------------------- install
do_install() {
  local src="$1" entry="$2"
  [ -f "$src" ] || die "找不到模块文件 $src"
  local base; base=$(basename "$src")
  local want; want=$(sha256sum "$src" | awk '{print $1}')
  echo "模块 $base  sha256=$want"
  echo "callbacks 条目 $entry"
  echo

  backup

  # --- 1) CM 加 data ---
  python3 - "$src" "$base" <<'PY' > /tmp/_rp_cm.json
import json, sys
src, base = sys.argv[1], sys.argv[2]
json.dump({"data": {base: open(src).read()}}, sys.stdout)
PY
  k -n "$NS" patch cm "$CB_CM" --type merge --patch-file /tmp/_rp_cm.json >/dev/null \
    || die "patch $CB_CM 失败"
  local in_cm
  in_cm=$(k -n "$NS" get cm "$CB_CM" -o json | python3 -c \
    "import json,sys,hashlib;print(hashlib.sha256(json.load(sys.stdin)['data']['$base'].encode()).hexdigest())")
  [ "$in_cm" = "$want" ] || die "CM 内 sha 不一致：$in_cm != $want" 1
  echo "[1/3] CM data ✅ sha 一致"

  # --- 2) Deployment 加 subPath volumeMount ---
  local has
  has=$(k -n "$NS" get deploy "$DEPLOY" -o json | python3 -c \
    "import json,sys;c=json.load(sys.stdin)['spec']['template']['spec']['containers'][0];print(any(m.get('subPath')=='$base' for m in c.get('volumeMounts',[])))")
  if [ "$has" = "True" ]; then
    echo "[2/3] volumeMount 已存在，跳过"
  else
    cat > /tmp/_rp_mount.json <<JEOF
[{"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-","value":{"name":"callbacks","mountPath":"/app/$base","subPath":"$base"}}]
JEOF
    k -n "$NS" patch deploy "$DEPLOY" --type json --patch-file /tmp/_rp_mount.json >/dev/null \
      || die "patch deploy volumeMount 失败"
    echo "[2/3] volumeMount ✅ 已加"
  fi

  # --- 3) config.yaml callbacks 加一行 ---
  python3 - "$NS" "$CFG_CM" "$entry" <<'PY' || die "patch config.yaml 失败"
import json, subprocess, sys, os
ns, cm_name, entry = sys.argv[1], sys.argv[2], sys.argv[3]
kubectl = os.environ.get("KUBECTL", "kubectl").split()
raw = subprocess.check_output(kubectl + ["-n", ns, "get", "cm", cm_name, "-o", "json"])
cm = json.loads(raw)
s = cm["data"]["config.yaml"]
line = "  - %s\n" % entry
if line in s:
    print("[3/3] callbacks 已含该条目，跳过"); sys.exit(0)
i = s.index("  callbacks:\n")
j = s.index("\n", i) + 1
# 追加到 callbacks 列表末尾：从第一条开始，吃掉所有连续的 "  - " 行
while s.startswith("  - ", j):
    j = s.index("\n", j) + 1
s = s[:j] + line + s[j:]
json.dump({"data": {"config.yaml": s}}, open("/tmp/_rp_cfg.json", "w"))
subprocess.check_call(kubectl + ["-n", ns, "patch", "cm", cm_name,
                                 "--type", "merge", "--patch-file", "/tmp/_rp_cfg.json"],
                      stdout=subprocess.DEVNULL)
print("[3/3] callbacks ✅ 已加")
PY

  echo
  echo "滚动中（patch deploy 已自动触发；仍显式 restart 保证 CM 变更被吃到）..."
  k -n "$NS" rollout restart deploy/"$DEPLOY" >/dev/null
  k -n "$NS" rollout status deploy/"$DEPLOY" --timeout=600s || die "rollout 未完成" 1
  echo
  do_verify "$base"
}

# ---------------------------------------------------------------- verify
do_verify() {
  local base="$1" bad=0 n=0
  local mod="${base%.py}"
  echo "逐副本校验 /app/$base"
  for p in $(pods); do
    n=$((n + 1))
    local sha inst mism
    sha=$(k -n "$NS" exec "$p" -- sha256sum "/app/$base" 2>/dev/null | awk '{print $1}')
    inst=$(k -n "$NS" logs "$p" 2>/dev/null | grep -c "$mod: installed")
    mism=$(k -n "$NS" logs "$p" 2>/dev/null | grep -ci "$mod.*mismatch")
    printf '  %-34s sha=%s installed=%s mismatch=%s\n' "$p" "${sha:0:16}" "$inst" "$mism"
    [ -n "$sha" ] || bad=1
    [ "$inst" -ge 1 ] || bad=1
    [ "$mism" -eq 0 ] || bad=1
  done
  [ "$n" -gt 0 ] || die "没找到任何 $DEPLOY 副本" 2
  if [ "$bad" -ne 0 ]; then
    echo "❌ 有副本没装上 / 指纹不匹配 —— 回滚"; return 1
  fi
  echo "✅ $n 个副本全部：文件在、install 日志有、指纹不匹配 0 次"
}

# ---------------------------------------------------------------- rollback
do_rollback() {
  local base="$1" entry="$2"
  echo "回滚 $base / $entry"
  python3 - "$NS" "$CFG_CM" "$entry" <<'PY'
import json, subprocess, sys, os
ns, cm_name, entry = sys.argv[1], sys.argv[2], sys.argv[3]
kubectl = os.environ.get("KUBECTL", "kubectl").split()
cm = json.loads(subprocess.check_output(kubectl + ["-n", ns, "get", "cm", cm_name, "-o", "json"]))
s = cm["data"]["config.yaml"]
line = "  - %s\n" % entry
if line not in s:
    print("callbacks 里没有该条目，跳过")
else:
    json.dump({"data": {"config.yaml": s.replace(line, "")}}, open("/tmp/_rp_cfg_rb.json", "w"))
    subprocess.check_call(kubectl + ["-n", ns, "patch", "cm", cm_name,
                                     "--type", "merge", "--patch-file", "/tmp/_rp_cfg_rb.json"],
                          stdout=subprocess.DEVNULL)
    print("callbacks 条目已删")
PY
  # volumeMount 按 subPath 精确定位下标后删除（不能写死下标）
  local idx
  idx=$(k -n "$NS" get deploy "$DEPLOY" -o json | python3 -c \
    "import json,sys;c=json.load(sys.stdin)['spec']['template']['spec']['containers'][0]['volumeMounts'];print(next((i for i,m in enumerate(c) if m.get('subPath')=='$base'),-1))")
  if [ "$idx" -ge 0 ]; then
    k -n "$NS" patch deploy "$DEPLOY" --type json \
      -p "[{\"op\":\"remove\",\"path\":\"/spec/template/spec/containers/0/volumeMounts/$idx\"}]" >/dev/null \
      && echo "volumeMount[$idx] 已删"
  else
    echo "volumeMount 不存在，跳过"
  fi
  # CM 里的 data 留着无害（没挂载就不会被 import），但也一并清掉更干净
  k -n "$NS" patch cm "$CB_CM" --type json \
    -p "[{\"op\":\"remove\",\"path\":\"/data/${base//./~1}\"}]" >/dev/null 2>&1 \
    && echo "CM data 已删" || echo "CM data 未删（可能本来就没有）"

  k -n "$NS" rollout restart deploy/"$DEPLOY" >/dev/null
  k -n "$NS" rollout status deploy/"$DEPLOY" --timeout=600s
}

case "${1:-}" in
  install)  [ $# -eq 3 ] || die "用法: $0 install <本地模块.py> <callbacks 条目>"; do_install "$2" "$3" ;;
  verify)   [ $# -eq 2 ] || die "用法: $0 verify <模块文件名.py>"; do_verify "$2" ;;
  rollback) [ $# -eq 3 ] || die "用法: $0 rollback <模块文件名.py> <callbacks 条目>"; do_rollback "$2" "$3" ;;
  *) die "用法: $0 {install|verify|rollback} ..." ;;
esac
