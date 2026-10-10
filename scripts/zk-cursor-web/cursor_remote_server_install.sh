#!/usr/bin/env bash
# cursor_remote_server_install.sh —— 一键让「本机 Cursor 连远程 Linux 改代码」能用。
#
# 用户面症状：Cursor 左下角连远程服务器，转半天 → "Download failed" /
# "Setting up SSH Host ... failed" / 一直卡在 Downloading，最后连不上。
#
# ── 这件事的机制（全部是 2026-09-21 从本机扩展包和真机实测抠出来的，不是猜）──
# Cursor 的远程开发跟 VS Code Remote-SSH 是同一套：本机只是个壳，真正干活的是
# 一个叫 **REH**（Remote Extension Host）的 Node 服务，它要装在**远程那台 Linux**上。
# 连接时 Cursor 通过 ssh 在远端跑一段安装脚本，那段脚本做的事是：
#
#   SERVER_DIR="$HOME/.cursor-server/bin/<platform>-<arch>/<commit>"
#   若 "$SERVER_DIR/bin/cursor-server" 不存在或是空文件：
#       用**远端自己的 wget/curl** 去下
#       https://cursor.blob.core.windows.net/remote-releases/<commit>/vscode-reh-<platform>-<arch>.tar.gz
#       然后 cd "$SERVER_DIR" && tar -xf <包> --strip-components 1
#   否则：echo "Server script already installed"，直接跳过下载。
#
# ⇒ 所以**只要我们提前把那个目录铺好，Cursor 连上去就什么都不下**。这不是 hack，
#   是它自己脚本里的既有分支（"already installed"）。本脚本就是铺这个目录。
#
# 🔴 <commit> 是**本机 Cursor 的版本指纹**，不是随便一个版本：
#    本机 /Applications/Cursor.app/.../product.json 里的 commit。
#    **本机 Cursor 一升级，commit 就变，远端要重铺一份**（旧的不会自动清）。
#    所以这个脚本设计成可以反复跑、对已铺好的是 no-op。
#
# ── 为什么同事会失败，本脚本按顺序实测三条下载腿 ────────────────────────────
# ⚠️ 我**没有**同事那台机器的数据，所以不预设是哪条腿断的。脚本每条都当场量：
#   腿1 远端直连  ：在目标机上 curl -sI 那个 URL 看是不是 200
#   腿2 本机中转  ：Mac 上下好再 scp 过去（远端不通公网时用）
#   腿3 指定中转机：--relay user@host，本机也不通时从它那儿下（我们这边 188 实测通）
# 三条都断才算真没辙，且会把每条断在哪原样印出来，不吞错。
#
# ── 动了什么 / 备份在哪 / 怎么回滚 ──────────────────────────────────────────
#   动：只在**目标机**上新建一个目录 $HOME/.cursor-server/bin/<平台>-<架构>/<commit>/
#       并把官方 tar 包解进去。**不改 ~/.ssh、不改任何已有文件、不装系统包**。
#   备份：不需要 —— 全是新增。已存在且完好的安装**跳过不碰**（除非 --force）。
#   回滚：ssh 到目标机 `rm -rf ~/.cursor-server/bin/<平台>-<架构>/<commit>`。
#         整个功能不要了就 `rm -rf ~/.cursor-server`（那目录只属于 Cursor 远程开发）。
#
# 用法：
#   ./cursor_remote_server_install.sh cltx@10.68.13.188            # 装
#   ./cursor_remote_server_install.sh --check cltx@10.68.13.188    # 只体检不装
#   ./cursor_remote_server_install.sh --relay cltx@10.68.13.188 me@my-box
#   ./cursor_remote_server_install.sh host-a host-b host-c         # 一次铺多台
#   ./cursor_remote_server_install.sh --force host-a               # 重装(先删再铺)
#
# 装完在 Cursor 里：⌘⇧P → "Remote-SSH: Connect to Host" → 选那台机器。
set -uo pipefail

APP="${CURSOR_APP:-/Applications/Cursor.app}"
CACHE="${CURSOR_REH_CACHE:-$HOME/.cache/cursor-reh}"
URL_TMPL="https://cursor.blob.core.windows.net/remote-releases/%s/vscode-reh-%s-%s.tar.gz"

CHECK=no; FORCE=no; RELAY=""; COMMIT=""; HOSTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --check)  CHECK=yes; shift ;;
    --force)  FORCE=yes; shift ;;
    --relay)  RELAY="${2:-}"; shift 2 ;;
    --commit) COMMIT="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,60p' "$0"; exit 0 ;;
    -*) echo "!! 不认识的参数:$1"; exit 2 ;;
    *)  HOSTS+=("$1"); shift ;;
  esac
done
[ ${#HOSTS[@]} -gt 0 ] || { echo "用法:$0 [--check|--force] [--relay user@host] user@host ..."; exit 2; }

# ── 本机 commit：远端装哪一版由它唯一决定 ──────────────────────────────────
if [ -z "$COMMIT" ]; then
  PJ="$APP/Contents/Resources/app/product.json"
  [ -f "$PJ" ] || { echo "!! 找不到 $PJ —— 本机没装 Cursor？用 --commit 手工指定"; exit 2; }
  COMMIT="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["commit"])' "$PJ")"
fi
case "$COMMIT" in
  [0-9a-f][0-9a-f]*) : ;;
  *) echo "!! commit 看着不对:'$COMMIT'"; exit 2 ;;
esac
echo "本机 Cursor commit = $COMMIT"
echo "目标机 $((${#HOSTS[@]})) 台：${HOSTS[*]}"
echo

SSH="ssh -o ConnectTimeout=15 -o BatchMode=yes"

# 在目标机上量平台/架构。判据抄自 Cursor 自己的安装脚本，**不是我另发明的一套**：
#   alpine 走 musl 包;glibc 落在 [2.17,2.28) 走 linux-legacy（常规包里的 Node 22 要 glibc>=2.28）;
#   其余 linux。认不出一律 fail-open 回常规 linux 包 —— 跟它一致。
probe_remote() {
  $SSH "$1" 'bash -s' <<'REMOTE'
set -u
ARCH=""; case "$(uname -m)" in x86_64|amd64) ARCH=x64;; arm64|aarch64) ARCH=arm64;; esac
PLAT=linux
case "$(uname -s)" in Linux|FreeBSD) :;; *) PLAT="";; esac
ID="$(grep -i '^ID=' /etc/os-release 2>/dev/null | sed 's/^ID=//i; s/"//g')"
[ "$ID" = alpine ] && PLAT=alpine
if [ "$PLAT" = linux ]; then
  L="$(LC_ALL=C ldd --version 2>/dev/null | head -1)"
  case "$L" in *"GNU libc"*|*GLIBC*|*glibc*)
      V="$(printf '%s' "$L" | awk '{print $NF}')"
      case "$V" in 2.1[7-9]|2.2[0-7]) PLAT=linux-legacy;; esac ;;
  esac
fi
echo "arch=$ARCH"
echo "platform=$PLAT"
echo "home=$HOME"
echo "tar=$(command -v tar || echo -)"
echo "curl=$(command -v curl || echo -)"
echo "wget=$(command -v wget || echo -)"
echo "free_mb=$(df -Pm "$HOME" | tail -1 | awk '{print $4}')"
REMOTE
}

overall=0
# Remote-SSH 的 "Connect to Host" 下拉**只列 ~/.ssh/config 里的 Host**。
# 服务端铺好了但这里没条目 ⇒ 同事在菜单里根本找不到那台机，症状看着像"还是不能用"。
# 只**读**、只提示，不替他改 ~/.ssh/config（那是他的常驻配置）。
ssh_config_hint() {
  [ "$SSHCFG" = no ] || return 0
  echo "  ⚠️ ~/.ssh/config 里没有 $HN 的条目 —— Cursor 的 \"Connect to Host\" 下拉只列这个文件里的机器，"
  echo "     没条目就在菜单里找不到它。把下面这段贴进 ~/.ssh/config（我不替你改这个文件）："
  echo "         Host ${HN}"
  echo "           HostName ${HN}"
  echo "           User ${RUSER}"
}
for H in "${HOSTS[@]}"; do
  echo "══════ $H ══════"
  HN="${H##*@}"
  case "$H" in *@*) RUSER="${H%@*}" ;; *) RUSER="$USER" ;; esac
  if [ -f "$HOME/.ssh/config" ] && grep -qiE "^[[:space:]]*(Host|HostName)([[:space:]]|.*[[:space:]])${HN}([[:space:]]|$)" "$HOME/.ssh/config"; then
    SSHCFG=yes
  else
    SSHCFG=no
  fi
  if ! info="$(probe_remote "$H" 2>&1)"; then
    echo "  ❌ ssh 连不上或远端 bash 跑不了，原样贴出来："
    printf '     %s\n' "$info"
    # ssh 都不通就别谈装服务端了 —— 先修 ssh（免密/跳板/防火墙），那是另一件事。
    overall=1; continue
  fi
  eval "$(printf '%s\n' "$info" | sed 's/^/R_/')"
  echo "  远端：$R_platform-$R_arch  home=$R_home  空闲 ${R_free_mb}MB  tar=$R_tar curl=$R_curl wget=$R_wget"
  if [ -z "${R_arch:-}" ] || [ -z "${R_platform:-}" ]; then
    echo "  ❌ 平台/架构认不出（只支持 Linux x64/arm64）"; overall=1; continue
  fi
  if [ "$R_tar" = "-" ]; then echo "  ❌ 远端没有 tar，装不了"; overall=1; continue; fi
  if [ "${R_free_mb:-0}" -lt 800 ]; then
    # 包 119MB，解开约 400MB，再留点余量。盘满时 tar 会解出**半个**目录，
    # 而半个目录照样能通过 Cursor 的 -f 检查 ⇒ 变成"连上了但服务起不来"的怪症状。
    echo "  ❌ $R_home 只剩 ${R_free_mb}MB，至少要 800MB（盘满会解出半个目录，症状更难查）"
    overall=1; continue
  fi

  SDIR="$R_home/.cursor-server/bin/$R_platform-$R_arch/$COMMIT"
  URL="$(printf "$URL_TMPL" "$COMMIT" "$R_platform" "$R_arch")"

  # 已经装好了？判据跟 Cursor 自己一样：bin/cursor-server 存在**且非空**。
  if $SSH "$H" "[ -s '$SDIR/bin/cursor-server' ]" 2>/dev/null; then
    if [ "$FORCE" = no ]; then
      echo "  ✅ 已装好：$SDIR  （Cursor 连上去会直接跳过下载）"
      ssh_config_hint
      continue
    fi
    echo "  --force：先删 $SDIR"
    [ "$CHECK" = yes ] || $SSH "$H" "rm -rf '$SDIR'"
  fi

  echo "  需要：$SDIR"
  echo "  包  ：$URL"

  # 腿1：远端能不能自己下。能的话根本不需要这个脚本 —— 但也顺手替它下了，
  #       因为 Cursor 内置那次下载超时很短（--timeout=5 --tries=2），网络抖一下就红。
  leg1=no
  # 测试钩子：置 1 可强制走腿2/腿3（scp 那条路），用来给「远端不通公网」那个分支做阳性对照。
  # 没有它的话那条分支只能靠"读着像对"发布，而它恰恰是给同事用得最多的一条。
  if [ "${CX_SKIP_REMOTE_DOWNLOAD:-}" = 1 ]; then
    echo "  （CX_SKIP_REMOTE_DOWNLOAD=1：跳过腿1，强制走 scp）"
  elif [ "$R_curl" != "-" ] || [ "$R_wget" != "-" ]; then
    if $SSH "$H" "curl -sfI --max-time 25 '$URL' >/dev/null 2>&1 || wget -q --spider --timeout=25 '$URL' >/dev/null 2>&1"; then
      leg1=yes
    fi
  fi
  echo "  腿1 远端直连公网：$([ $leg1 = yes ] && echo 通 || echo '不通/没有 curl+wget/被跳过')"

  if [ "$CHECK" = yes ]; then
    echo "  （--check：只体检，不装）"
    continue
  fi

  if [ "$leg1" = yes ]; then
    echo "  → 让远端自己下（约 114MB，通常 30~90 秒；断点续传，比 Cursor 内置的 5s 超时宽松）"
    # ⚠️ 不带静默开关时 curl 的进度条经 ssh 回来是一大坨 \r 拼成的长行，
    #    真正的报错会被冲得看不见。--no-progress-meter 太新的话回落 -s。
    if $SSH "$H" "bash -s" <<EOS
set -euo pipefail
mkdir -p '$SDIR'
cd '$SDIR'
if command -v curl >/dev/null; then
  Q=--no-progress-meter; curl --help all 2>/dev/null | grep -q -- --no-progress-meter || Q=-s
  curl -fL \$Q --show-error --retry 3 --retry-delay 2 -C - -o reh.tar.gz '$URL'
else
  wget -q -c -O reh.tar.gz '$URL'
fi
tar -xf reh.tar.gz --strip-components 1
rm -f reh.tar.gz
EOS
    then :; else echo "  ❌ 远端下载/解压失败（上面是原始输出）"; overall=1; continue; fi
  else
    # 腿2/腿3：本地或中转机拿到包，再 scp 过去。本地缓存一份，铺第二台时不重下。
    # 腿3 优先于腿2：给了 --relay 就先用它。
    # 🔴 这个顺序是实测定的，不是偏好：2026-09-21 本机 Mac 直连 Azure blob 实测
    #    只有约 45 KB/s（114MB 要 40 分钟以上，中途还会停），而 188 上同一个 URL
    #    实测 4 MB/s。本机出网慢在这台 Mac 上是已知老问题，不是偶发。
    #    所以本机那条腿加了失速熔断（20 秒内低于 50KB/s 就断，别挂在那儿耗着）。
    LOCAL="$CACHE/vscode-reh-$R_platform-$R_arch-$COMMIT.tar.gz"
    mkdir -p "$CACHE"
    if [ ! -s "$LOCAL" ]; then
      got=no
      if [ -n "$RELAY" ]; then
        echo "  腿3 中转机 $RELAY 下载 → $LOCAL"
        if $SSH "$RELAY" "curl -fL --retry 3 --silent --show-error '$URL'" > "$LOCAL.part"; then
          got=yes
        else
          echo "  ⚠️ 中转机下不动（上面是它的原始报错），回落本机直连"
          rm -f "$LOCAL.part"
        fi
      fi
      if [ "$got" = no ]; then
        echo "  腿2 本机下载 → $LOCAL（本机出网慢时会在 20 秒内熔断）"
        if curl -fL --retry 3 -C - --speed-time 20 --speed-limit 50000 -o "$LOCAL.part" "$URL"; then
          got=yes
        else
          rm -f "$LOCAL.part"
        fi
      fi
      if [ "$got" = no ]; then
        echo "  ❌ 远端不通、本机不通${RELAY:+、中转机也不通}。"
        [ -n "$RELAY" ] || echo "     下一步：找一台能上外网的机器 X，重跑并加 --relay user@X"
        overall=1; continue
      fi
      mv "$LOCAL.part" "$LOCAL"
    else
      echo "  （用本机缓存 $LOCAL）"
    fi
    # 包完整性：tar 自己校一遍再传，省得把半个包 scp 过去，在远端解出个残缺目录。
    if ! tar -tzf "$LOCAL" >/dev/null 2>&1; then
      echo "  ❌ 缓存包坏了，已删除，请重跑：$LOCAL"; rm -f "$LOCAL"; overall=1; continue
    fi
    echo "  → scp 到 $H 并解压（$(du -h "$LOCAL" | awk '{print $1}')）"
    if ! $SSH "$H" "mkdir -p '$SDIR'"; then echo "  ❌ 建目录失败"; overall=1; continue; fi
    if ! scp -q "$LOCAL" "$H:$SDIR/reh.tar.gz"; then echo "  ❌ scp 失败"; overall=1; continue; fi
    if ! $SSH "$H" "cd '$SDIR' && tar -xf reh.tar.gz --strip-components 1 && rm -f reh.tar.gz"; then
      echo "  ❌ 远端解压失败"; overall=1; continue
    fi
  fi

  # ── 验收。三条都过才算装上，不许"命令返回 0 就报绿" ──────────────────────
  #   1) bin/cursor-server 非空   —— Cursor 自己的 already-installed 判据，必须满足
  #   2) out/server-main.js 非空  —— 真正被 node 执行的那个文件
  #   3) 自带 node 跑得起来       —— glibc 选错档时正是这里炸（能解压、起不来）
  v="$($SSH "$H" "bash -s" <<EOS
[ -s '$SDIR/bin/cursor-server' ] && echo "script=ok" || echo "script=MISSING"
[ -s '$SDIR/out/server-main.js' ] && echo "main=ok" || echo "main=MISSING"
'$SDIR/node' --version 2>&1 | head -1 | sed 's/^/node=/'
EOS
)"
  printf '     %s\n' $v
  if printf '%s' "$v" | grep -q MISSING || ! printf '%s' "$v" | grep -q 'node=v'; then
    echo "  ❌ 验收没过 —— 已装的残件在 $SDIR，回滚：ssh $H \"rm -rf '$SDIR'\""
    if [ "$R_platform" = linux ]; then
      echo "     若 node 报 GLIBC 相关错，说明这台的 glibc 比检测到的旧，"
      echo "     重跑时加 --commit 不变、把远端 /etc/os-release 和 ldd --version 贴给我。"
    fi
    overall=1; continue
  fi
  echo "  ✅ 装好了：$SDIR"
  ssh_config_hint
done

echo
if [ "$CHECK" = yes ]; then
  echo "以上是体检，一个字都没往目标机写。去掉 --check 才真装。"
else
  echo "下一步（在本机 Cursor 里）：⌘⇧P → Remote-SSH: Connect to Host → 选这台机器。"
  echo "🔴 本机 Cursor 升级后 commit 会变，届时要对每台目标机重跑一次本脚本。"
fi
exit $overall
