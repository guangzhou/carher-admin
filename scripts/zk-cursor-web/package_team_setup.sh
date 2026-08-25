#!/bin/sh
# 打包同事分发用的 cursor-g 安装包(零依赖跨平台)。产物:cursor-g-setup.zip
# 内含:引擎(js+sh+cmd)+ 双击安装器(Mac .command / Windows .cmd)+ README。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
STAGE="$(mktemp -d)/cursor-g-setup"
mkdir -p "$STAGE"

cp "$HERE/cursor_team_setup.js"  "$STAGE/"
cp "$HERE/cursor_team_setup.sh"  "$STAGE/"
cp "$HERE/cursor_team_setup.cmd" "$STAGE/"

# 双击安装器(Mac):调用引擎并带 --apply,跑完暂停等回车
cat > "$STAGE/INSTALL-Mac.command" <<'MAC'
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
sh "$DIR/cursor_team_setup.sh" --apply
echo ""
printf "按回车键关闭本窗口…"; read _
MAC
chmod +x "$STAGE/INSTALL-Mac.command" "$STAGE/cursor_team_setup.sh"

# 双击安装器(Windows):调用引擎并带 --apply,跑完 pause
cat > "$STAGE/INSTALL-Windows.cmd" <<'WIN'
@echo off
chcp 65001 >nul
call "%~dp0cursor_team_setup.cmd" --apply
echo.
pause
WIN

cat > "$STAGE/README.txt" <<'RD'
cursor-g 一键安装(零依赖,不用装 Python / Node)

准备:
  1) 先拿到你的 API Key(找管理员,或用飞书「个人账户」表查)。
  2) 完全退出 Cursor(Mac 按 Cmd+Q;Windows 右键任务栏图标→退出。只关窗口不算)。

安装:
  Mac     :双击 INSTALL-Mac.command
           (若提示"无法打开、来自身份不明的开发者",右键该文件→打开→再点"打开")
  Windows :双击 INSTALL-Windows.cmd
           (若弹安全提示,选"仍要运行")

看到"完成"就装好了。最后一步在 Cursor 里:
  设置 → Models → OpenAI API Key,粘贴你的 key,点 Verify。
  然后模型菜单里选 cursor-g-5.6-sol 即可用。

出问题看飞书文档的"常见问题",或找管理员。
RD

( cd "$(dirname "$STAGE")" && zip -r -X "$HERE/cursor-g-setup.zip" "cursor-g-setup" >/dev/null )
echo "built: $HERE/cursor-g-setup.zip"
unzip -l "$HERE/cursor-g-setup.zip"
