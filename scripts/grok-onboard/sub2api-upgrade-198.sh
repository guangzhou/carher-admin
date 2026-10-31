#!/bin/bash
# 在本机 Mac 上发起 198 的 sub2api 升级。所有 docker/kubectl 都在 198 上跑,本机一条 docker 都不碰。
#
#   scripts/grok-onboard/sub2api-upgrade-198.sh sync              # 只做三方核 sha + 同步脚本/补丁到 198
#   scripts/grok-onboard/sub2api-upgrade-198.sh go                # sync 后在 198 后台跑 `go`(停在切换闸门前)
#   scripts/grok-onboard/sub2api-upgrade-198.sh status            # 看 198 上那次 go 跑到哪
#   scripts/grok-onboard/sub2api-upgrade-198.sh run <子命令…>     # sync 后前台跑任意子命令,如 cutover --yes / verify
#
# 为什么有这层(10-31):198 上常驻的 sub2api-upgrade.py 落后仓库一个提交(生产 secret 改名 -r2 没同步过去),
#   直接跑探针会读不到 master key。同步这一步以前靠人记得,现在每次发起前都自动做,并核三方 sha:
#   git HEAD / 工作区 / 198 —— 只核后两个等于没核(工作区可能是未提交改动)。
# 退出码: 0=成功;2=用法;3=三方 sha 对不上且不是「工作区=HEAD」(有未提交改动,先提交);4=远端失败
set -uo pipefail
H198=${H198:-cltx@10.68.13.198}
OPS=/Data/sub2api-ops
LOG=/root/sub2api-backup/go-$(date +%Y%m%d).log
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
REL=scripts/grok-onboard
SSH=(ssh -n -o BatchMode=yes -o ConnectTimeout=30 -o ClearAllForwardings=yes "$H198")

sha() { shasum -a 256 | cut -c1-64; }

sync198() {
  local f head work
  # 相对 $REL 展开,不吃调用者 cwd(10-31 首跑在 repo 根展开成字面 patches/*.patch ⇒ 假 STOP)
  for f in sub2api-upgrade.py $(cd "$REPO/$REL" && ls patches/*.patch); do
    head=$(git -C "$REPO" show "HEAD:$REL/$f" 2>/dev/null | sha)
    work=$(sha < "$REPO/$REL/$f")
    if [ "${head}" != "${work}" ]; then
      echo "STOP $REL/$f 工作区≠git HEAD(有未提交改动),先提交再升级" >&2; return 3
    fi
  done
  # 远端:逐个比 sha,不同才备份+替换(备份带日期,不覆盖已有备份)
  local tmp=/tmp/.s2a-sync-$$
  "${SSH[@]}" "mkdir -p $tmp/patches" || return 4
  scp -q -o ClearAllForwardings=yes "$REPO/$REL/sub2api-upgrade.py" "$H198:$tmp/" || return 4
  scp -q -o ClearAllForwardings=yes "$REPO/$REL"/patches/*.patch "$H198:$tmp/patches/" || return 4
  "${SSH[@]}" "sudo -n bash -c '
    set -e; cd $tmp; d=\$(date +%Y%m%d%H%M)
    for f in sub2api-upgrade.py patches/*.patch; do
      if ! cmp -s \$f $OPS/\$f 2>/dev/null; then
        [ -f $OPS/\$f ] && cp -p $OPS/\$f $OPS/\$f.bak-\$d && echo \"backup $OPS/\$f.bak-\$d\"
        install -D -m 755 \$f $OPS/\$f && echo \"synced \$f\"
      fi
    done
    cd / && rm -rf $tmp
    sha256sum $OPS/sub2api-upgrade.py $OPS/patches/*.patch'" || return 4
  echo "local  $(sha < "$REPO/$REL/sub2api-upgrade.py")  $REL/sub2api-upgrade.py"
}

case "${1:-}" in
  sync) sync198 ;;
  go)
    sync198 || exit $?
    # [s]ub2api:pgrep -f 会匹配到这条 ssh 自己的 bash -c 命令行,方括号让模式不匹配自身
    if "${SSH[@]}" 'pgrep -f "[s]ub2api-upgrade.py go" >/dev/null'; then
      echo "STOP 198 上已有一个 go 在跑,先 status" >&2; exit 4
    fi
    # setsid + </dev/null:ssh 立即返回,远端进程不挂在这条连接上
    "${SSH[@]}" "sudo -n setsid bash -c 'cd /root && exec python3 -u $OPS/sub2api-upgrade.py go > $LOG 2>&1' </dev/null >/dev/null 2>&1 &" || exit 4
    echo "started on 198, log $LOG ;下一步: $0 status" ;;
  status)
    "${SSH[@]}" "pgrep -f '[s]ub2api-upgrade.py go' >/dev/null && echo RUNNING || echo NOT-RUNNING;
      sudo -n bash -c 'l=\$(ls -t /root/sub2api-backup/go-*.log 2>/dev/null | head -1); [ -n \"\$l\" ] && echo \"== \$l\" && tail -n 25 \"\$l\"'" ;;
  run)
    shift; [ $# -ge 1 ] || { sed -n '2,8p' "$0"; exit 2; }
    sync198 || exit $?
    q=$(printf '%q ' "$@")
    ssh -o BatchMode=yes -o ClearAllForwardings=yes "$H198" "sudo -n bash -c 'cd /root && python3 -u $OPS/sub2api-upgrade.py $q'" </dev/null ;;
  *) sed -n '2,13p' "$0"; exit 2 ;;
esac
