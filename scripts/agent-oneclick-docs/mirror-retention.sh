#!/usr/bin/env bash
# 镜像盘的「搬到挂载盘」+「每个产品只留最近 N 个版本」。被 app-mirror-sync.sh / ccdl-sync.sh source。
#
# 为什么(2026-11-04,用户:「每天更新的 app 包括 cursor claude 和 codex 都要放在挂载盘上,
#   另外只能保存最近两个版本,老版本要删除了。不然磁盘容易撑爆」):
#   · 现量:/var/www/ccdl 在**系统盘** /dev/vda3(98G,已用 54%)上占 12G,而且两个每日任务都是「只增不删」。
#     一版 Cursor ≈1.9G、一版 Codex mac ≈3.0G、一版 Claude Code ≈1G ⇒ 每周新版一来就涨好几 G。
#     系统盘被写满时,198 上的 k3s / nginx / LiteLLM 会一起出问题 —— 爆的是整台机器,不是一个下载页。
#   · 挂载盘 /Data(/dev/vdb 492G,fstab 持久挂载)还剩 148G。
#
# 🔴 删除的唯一硬约束:**稳定名软链指着的版本永远不删**。
#   「最近 2 个」按**版本号**排序,不按修改时间(重下一次旧版就会被当成最新);
#   而稳定名可能故意指着旧版(新版校验没过、没切过去)⇒ 那一版必须留,哪怕它不在最近 2 个里。
#   Claude Code 组件还要再保一层:**桌面 App 钉死的那个版本**(同事机器上的 App 每开一个会话都按它去下),
#   删了 = 那批同事发消息不回。见 ccdl-sync.sh。

MIRROR_DATA_ROOT="${MIRROR_DATA_ROOT:-/Data/ccdl}"     # 挂载盘上的真身
MIRROR_WEB_ROOT="${MIRROR_WEB_ROOT:-/var/www/ccdl}"    # nginx alias 的位置(保持不变,改成指过去的软链)
KEEP_VERSIONS="${KEEP_VERSIONS:-2}"

# 把 /var/www/ccdl 整体搬到挂载盘,原位置换成软链。幂等:已经是指向挂载盘的软链就什么都不做。
#   · 先拷后换:rsync 到挂载盘 → 校验文件数和字节数一致 → 原子地把旧目录挪开、软链换上 → 才删旧目录。
#     中途任何一步失败,对外的仍是原目录,不会出现「一半在这、一半在那」。
#   · nginx 的 alias 不用改:alias 指的是 /var/www/ccdl/,它变成软链后 nginx 照样跟(没设 disable_symlinks)。
mirror_relocate_to_data() {
  local web="$MIRROR_WEB_ROOT" data="$MIRROR_DATA_ROOT"
  if [ -L "$web" ]; then
    [ "$(readlink -f "$web")" = "$(readlink -f "$data")" ] && { echo "  = 镜像已在挂载盘($data)"; return 0; }
    echo "✗ $web 是软链但不指向 $data(指向 $(readlink "$web")),不敢动,请人工看一下" >&2; return 1
  fi
  mountpoint -q "$(dirname "$data")" || { echo "✗ $(dirname "$data") 不是挂载点(挂载盘没挂上?),不搬" >&2; return 1; }
  [ -d "$web" ] || mkdir -p "$web"
  mkdir -p "$data"
  echo "→ 把 $web 搬到挂载盘 $data"
  rsync -a "$web/" "$data/"
  local a b
  # 比「文件数 + 各文件大小之和」,不比 du -sb(它含目录块,两个文件系统块大小不同会差几 KB ⇒ 假红)
  a="$(find "$web" -type f -printf '%s\n' | awk '{s+=$1} END{print NR, s+0}')"
  b="$(find "$data" -type f -printf '%s\n' | awk '{s+=$1} END{print NR, s+0}')"
  [ "$a" = "$b" ] || { echo "✗ 拷贝后对不上(原 $a / 新 $b),不切换" >&2; return 1; }
  local old="$web.old-$(date +%Y%m%d%H%M%S)"
  mv "$web" "$old" && ln -s "$data" "$web" || { echo "✗ 切软链失败,还原" >&2; [ -e "$web" ] || mv "$old" "$web"; return 1; }
  rm -rf "$old"
  echo "  ✔ 已搬到挂载盘($b),$web → $data"
}

# 某目录下所有「当前被引用的版本目录名」:扫 <links_dir> 里的软链,取 <subdir>/<版本>/… 的那一级。
#   用法:mirror_pinned <links_dir> <subdir>   例:mirror_pinned "$ROOT" cursor  ⇒ 3.28.7
#         subdir 为空 ⇒ 软链形如 <版本>/文件(desktop 目录就是这样)
mirror_pinned() {
  local dir="$1" sub="$2" l t
  for l in "$dir"/*; do
    [ -L "$l" ] || continue
    t="$(readlink "$l")"
    if [ -n "$sub" ]; then
      case "$t" in "$sub"/*) t="${t#"$sub"/}"; printf '%s\n' "${t%%/*}" ;; esac
    else
      printf '%s\n' "${t%%/*}"
    fi
  done | sort -u
}

# 只留最近 N 个版本 + 所有被钉住的版本,其余删掉。
#   用法:mirror_prune <版本目录的父目录> <钉住的版本列表(换行分隔)> [版本前缀]
#   版本前缀:Codex 的 mac 包放在 codex/mac-<版本>/,和 Windows 的 codex/<版本>/ 并列 ⇒ 分开各留 N 个,
#            传 "mac-" 只管 mac-*;传 "" 只管**不带** mac- 前缀的。
#   KEEP_VERSIONS 以外、又没被钉住的才删。删之前打印删了什么、腾出多少。
mirror_prune() {
  local parent="$1" pinned="$2" prefix="${3-}" keep="$KEEP_VERSIONS"
  [ -d "$parent" ] || return 0
  [ "$keep" -ge 1 ] 2>/dev/null || { echo "✗ KEEP_VERSIONS=$keep 不合法,不删" >&2; return 1; }
  local all
  all="$(cd "$parent" && for d in */; do d="${d%/}"; [ -L "$d" ] && continue
           if [ -n "$prefix" ]; then case "$d" in "$prefix"*) printf '%s\n' "$d" ;; esac
           else case "$d" in mac-*) ;; *) printf '%s\n' "$d" ;; esac; fi
         done)"
  [ -n "$all" ] || return 0
  # 按版本号从新到旧(sort -V 认 3.28.7 / 26.1029.61003 / 4.16336.0 这类点分数字;前缀一起比不影响顺序)
  local newest; newest="$(printf '%s\n' "$all" | sort -rV | head -n "$keep")"
  local d freed=0 sz
  for d in $(printf '%s\n' "$all" | sort -rV); do
    if printf '%s\n' "$newest" | grep -qxF "$d"; then continue; fi
    if printf '%s\n' "$pinned" | grep -qxF "$d"; then echo "  · 保留 $parent/$d(受保护:稳定链接指着它,或最近还有人在下载)"; continue; fi
    sz="$(du -sb "$parent/$d" 2>/dev/null | cut -f1)"; freed=$((freed + ${sz:-0}))
    rm -rf "${parent:?}/$d"
    echo "  − 删掉旧版本 $parent/$d"
  done
  if [ "$freed" -gt 0 ]; then echo "  ✔ $parent 腾出 $((freed / 1048576)) MB(留最近 $keep 个 + 被引用的)"; fi
  return 0
}
