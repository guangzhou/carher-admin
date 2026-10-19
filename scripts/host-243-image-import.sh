#!/usr/bin/env bash
# host-243-image-import.sh —— 把镜像 tar 导进 243 的 k3s containerd，并 pin 住（image GC 不删）。
#
# 用法:
#   HOST243_PW='...' scripts/host-243-image-import.sh /Data/migration/image-fix-9f250f8.tar [额外tag ...]
#     tar 必须已经在 243 的 /Data 下（从 188 搬：ssh 188 'cat <tar>' | ssh 243 'cat > /Data/migration/<name>.tar'）
#     额外 tag 例：ai-token-dashboard:latest —— 把 tar 里第一个镜像再打上这个名字（--force 覆盖旧指向）
#
# 为什么要 pin：243 的 myai 镜像是本地导入 + imagePullPolicy=Never。2026-10-17 根盘到 95%，
# kubelet image GC 把它们当"未使用镜像"删了，pod 全部 ErrImageNeverPull，停机 19h。
# 🔴 tar 别放根盘再删：放 /Data/migration 留着，下次被删可以直接重导。
#
# 足迹: containerd 新增/覆盖镜像引用 + label io.cri-containerd.pinned=pinned。不重启任何 pod。
# 回滚: S k3s ctr -n k8s.io images rm <ref>（被 pod 用着的 ref 别删）。
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
[ $# -ge 1 ] || { echo "用法: $0 <243上的tar路径> [额外tag ...]" >&2; exit 2; }
TAR="$1"; shift
EXTRA="$*"
case "$TAR" in /Data/*) ;; *) echo "tar 请放在 /Data 下（根盘只有 38G）: $TAR" >&2; exit 2;; esac

"$DIR/host-243-run.sh" TAR="$TAR" EXTRA="$EXTRA" - <<'EOF'
set -eu
S test -f "$TAR" || { echo "243 上没有 $TAR" >&2; exit 2; }
# tar 里的镜像名（docker save 的 manifest.json / OCI 的 index.json annotation 二选一）
S test -r "$TAR" || { echo "root 也读不了 $TAR" >&2; exit 2; }
REFS=$(S tar xOf "$TAR" manifest.json 2>/dev/null | python3 -c '
import sys,json
for m in json.load(sys.stdin):
  for t in m.get("RepoTags") or []:
    print(t if "/" in t.split(":")[0] else "docker.io/library/"+t)' 2>/dev/null || true)
[ -n "$REFS" ] || REFS=$(S tar xOf "$TAR" index.json | python3 -c '
import sys,json
for m in json.load(sys.stdin)["manifests"]:
  n=m.get("annotations",{}).get("io.containerd.image.name")
  if n: print(n)')
[ -n "$REFS" ] || { echo "读不出 tar 里的镜像名" >&2; exit 1; }
echo "tar 里: $REFS"
S k3s ctr -n k8s.io images import "$TAR" | tail -1
FIRST=$(echo "$REFS" | head -1)
for t in $EXTRA; do
  case "$t" in */*) full="docker.io/$t";; *) full="docker.io/library/$t";; esac
  S k3s ctr -n k8s.io images tag --force "$FIRST" "$full" >/dev/null && echo "tag $FIRST -> $full"
  REFS="$REFS
$full"
done
for r in $REFS; do
  S k3s ctr -n k8s.io images label "$r" io.cri-containerd.pinned=pinned >/dev/null
done
# 回读：每个 ref 都在且 pinned 才算完
S k3s crictl images -o json | python3 -c '
import sys,json
want=sys.argv[1].split()
imgs=json.load(sys.stdin)["images"]
bad=0
for r in want:
  m=[x for x in imgs if r in x["repoTags"]]
  st="missing" if not m else ("pinned" if m[0].get("pinned") else "UNPINNED")
  print("%-9s %s  %s" % (st, r, m[0]["id"][:19] if m else ""))
  bad|= st!="pinned"
sys.exit(bad)' "$(echo $REFS)" || exit 1
# 导入只换了镜像，正在跑的 pod 不会自己换。列出用到这些镜像的 Deployment，提醒要 rollout 哪几个
# （🔴 myai 有两份：ai-token-dashboard 和 ai-token-dashboard-chat，换 latest 时两份都要）
short=$(for r in $REFS; do echo "${r#docker.io/library/}"; done)
# 🔴 `cond && echo` 作为循环最后一条、且最后一行不匹配时，循环 rc=1 ⇒ set -e 直接把脚本杀掉、什么都不打（10-19 实测）
users=$(k3s kubectl get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{" "}{.spec.template.spec.containers[*].image}{"\n"}{end}' \
  | while read -r d imgs; do for i in $imgs; do if echo "$short" | grep -qxF "$i"; then echo "$d"; break; fi; done; done || true)
if [ -n "$users" ]; then
  echo "用到这些镜像的 Deployment（要生效需逐个 rollout restart）:"
  echo "$users" | sed 's/^/  /'
fi
EOF
