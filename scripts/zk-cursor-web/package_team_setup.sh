#!/bin/sh
# 打包同事分发用的 cursor-g 安装包(零依赖跨平台)。产物:cursor-g-setup.zip
# 内含:引擎(js+sh+cmd)+ 双击安装器(Mac .command / Windows .cmd)+ 双击修复器 + README。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
STAGE="$(mktemp -d)/cursor-g-setup"
mkdir -p "$STAGE"

cp "$HERE/cursor_team_setup.js"  "$STAGE/"
cp "$HERE/cursor_team_setup.sh"  "$STAGE/"
cp "$HERE/cursor_team_setup.cmd" "$STAGE/"

# zk-delta 本机小代理。相对结构必须保住：sidecar.js 里写的是 require('../common/framing')，
# 装到 ~/.zk-delta 之后也是这个结构。少一个文件安装器会当场拒绝装（不半装）。
ZKD_SRC="$(cd "$HERE/../../zk-delta" && pwd)"
mkdir -p "$STAGE/zk-delta/sidecar" "$STAGE/zk-delta/common"
cp "$ZKD_SRC/sidecar/sidecar.js"  "$STAGE/zk-delta/sidecar/"
cp "$ZKD_SRC/common/framing.js"   "$STAGE/zk-delta/common/"
# 指纹打进包里：同事报问题时先对这个数，能立刻分清"他装的是哪一版"。
( cd "$STAGE/zk-delta" && shasum -a 256 sidecar/sidecar.js common/framing.js > SHA256SUMS.txt )
echo "zk-delta 源码指纹:"; sed 's/^/  /' "$STAGE/zk-delta/SHA256SUMS.txt"

# 双击安装器(Mac):调用引擎并带 --apply(装完会提示粘一次 Key),跑完暂停等回车
cat > "$STAGE/INSTALL-Mac.command" <<'MAC'
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
sh "$DIR/cursor_team_setup.sh" --apply
echo ""
printf "按回车键关闭本窗口…"; read _
MAC

# 双击安装器(Windows):调用引擎并带 --apply,跑完 pause
cat > "$STAGE/INSTALL-Windows.cmd" <<'WIN'
@echo off
chcp 65001 >nul
call "%~dp0cursor_team_setup.cmd" --apply
echo.
pause
WIN

# 双击修复器(Mac):Cursor 升级后 cursor-g 没了,双击它一键重打补丁(不碰配置/Key)
cat > "$STAGE/REPAIR-Mac.command" <<'MACR'
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
sh "$DIR/cursor_team_setup.sh" --repair
echo ""
printf "按回车键关闭本窗口…"; read _
MACR

# 双击修复器(Windows)
cat > "$STAGE/REPAIR-Windows.cmd" <<'WINR'
@echo off
chcp 65001 >nul
call "%~dp0cursor_team_setup.cmd" --repair
echo.
pause
WINR

# 双击关掉增量传输(Mac):万一小代理出问题,不用找管理员,双击退回公网直连。
# 文件名用 ASCII —— 中文名在 zip 里跨平台会乱码(实测 unzip -l 显示成 ?��??)。
cat > "$STAGE/TURN-OFF-DELTA-Mac.command" <<'MACZ'
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "把 Cursor 退回「直连公网」——不再经过本机小代理。"
echo "（Cursor 必须先完全退出：Cmd+Q）"
sh "$DIR/cursor_team_setup.sh" --apply --no-zk-delta --zk-delta-only
echo ""
printf "按回车键关闭本窗口…"; read _
MACZ

chmod +x "$STAGE/INSTALL-Mac.command" "$STAGE/REPAIR-Mac.command" "$STAGE/TURN-OFF-DELTA-Mac.command" "$STAGE/cursor_team_setup.sh"

cat > "$STAGE/README.txt" <<'RD'
cursor-g 一键安装(零依赖,不用装 Python / Node)

━━━ 已经装过的同事:请双击一次 REPAIR-Mac.command(Windows: REPAIR-Windows.cmd)━━━
  8-31 那版里有一个"链式增量"的实验补丁,它的服务端那半已经下线了,留在客户端会让
  机房那边少收到一部分对话上下文(不报错、但模型可能忘事)。本版会自动把它摘掉。
  你的 Key、配置、选中的模型都不动;摘完重启 Cursor 即可。要先完全退出 Cursor。

准备:
  1) 先拿到你的 API Key(找管理员,或用飞书「个人账户」表查)。
  2) 完全退出 Cursor(Mac 按 Cmd+Q;Windows 右键任务栏图标→退出。只关窗口不算)。

安装:
  Mac     :双击 INSTALL-Mac.command
           (若提示"无法打开、来自身份不明的开发者",右键该文件→打开→再点"打开")
  Windows :双击 INSTALL-Windows.cmd
           (若弹安全提示,选"仍要运行")

  安装过程中窗口会让你「粘贴 API Key 后回车」——粘一次就好,脚本自动写进去。
  (Windows 上若自动写 Key 跳过了,按提示在 Cursor 设置里手动粘一次即可。)

看到"完成"就装好了:启动 Cursor,模型菜单默认就是 cursor-g-5.6-sol,直接用。

关于"增量传输"(装完自动开着,Mac 才有)
  装完之后你的 Cursor 不再直接连公网,而是先连本机一个小程序(127.0.0.1:8788),
  由它只把**这一轮新增的那几条消息**发出去,机房那边补回完整的对话再交给模型。
  - 模型看到的内容一个字都没变,回答质量不受影响。省的是你这边上传的流量:
    实测长对话省 86%,对话越长省得越多。
  - 那个小程序用 Cursor 自带的运行时跑,你不用装任何东西。
  - 它**不会**把你的对话内容存到你的磁盘上。
  - 机房那边万一挂了,它自动退回直连,你不会有感觉。它自己万一挂了,系统会自动拉起来。
  - 想看省了多少:浏览器打开 http://127.0.0.1:8788/metrics.json
  - 不想用了:双击 TURN-OFF-DELTA-Mac.command(要先退出 Cursor),立刻退回直连公网。

Cursor 升级后 cursor-g 不见了怎么办?
  不用回滚升级。双击 REPAIR-Mac.command(Windows: REPAIR-Windows.cmd)一键修复,
  重启 Cursor 即可。你的 Key 和配置都还在,修复只重打补丁。

出问题看飞书文档的"常见问题",或找管理员。
RD

# 先删旧包。zip -r 是**往已有归档里追加**，不是重建：改过文件名之后旧名字会留在包里，
# 同事解出来会多一个乱码文件(第一次改名时实测踩到)。
rm -f "$HERE/cursor-g-setup.zip"
( cd "$(dirname "$STAGE")" && zip -r -X "$HERE/cursor-g-setup.zip" "cursor-g-setup" >/dev/null )
echo "built: $HERE/cursor-g-setup.zip"
unzip -l "$HERE/cursor-g-setup.zip"
