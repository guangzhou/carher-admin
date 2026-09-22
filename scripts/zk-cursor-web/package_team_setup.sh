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

# 远程窗口直通(09-21 加)。本机包装好之后 BYOK 地址是 127.0.0.1:8788,而远程窗口里的
# 127.0.0.1 是**远端那台机** ⇒ 模型全报连不上。这个引擎往 ~/.ssh/config 加一条反向
# 隧道把它转回来。独立成一个文件而不是塞进 cursor_team_setup.js:它只对"要连远程
# 服务器"的同事有意义,大多数人一辈子不会双击它,不该拖着主安装流程一起变复杂。
cp "$HERE/cursor_remote_ssh.js"  "$STAGE/"
cp "$HERE/cursor_remote_ssh.sh"  "$STAGE/"
# Windows 启动器**从主启动器生成**,不另抄一份。那里面找 Cursor.exe 的六段逻辑
# (固定路径/注册表 App Paths/卸载项/开始菜单/手输)是 09-03 同事踩坑后补的,
# 手抄第二份 = 下次再补一段时两边悄悄分叉。
sed 's/cursor_team_setup\.js/cursor_remote_ssh.js/g; s/cursor_team_setup\.cmd/cursor_remote_ssh.cmd/g' \
    "$HERE/cursor_team_setup.cmd" > "$STAGE/cursor_remote_ssh.cmd"
grep -q 'cursor_remote_ssh.js' "$STAGE/cursor_remote_ssh.cmd" || {
  echo "FATAL: 生成 cursor_remote_ssh.cmd 时没替换到 JS 名(主启动器结构变了?)" >&2; exit 1; }

# zk-delta 本机小代理。相对结构必须保住：sidecar.js 里写的是 require('../common/framing')，
# 装到 ~/.zk-delta 之后也是这个结构。少一个文件安装器会当场拒绝装（不半装）。
ZKD_SRC="$(cd "$HERE/../../zk-delta" && pwd)"
mkdir -p "$STAGE/zk-delta/sidecar" "$STAGE/zk-delta/common"
cp "$ZKD_SRC/sidecar/sidecar.js"  "$STAGE/zk-delta/sidecar/"
cp "$ZKD_SRC/common/framing.js"   "$STAGE/zk-delta/common/"
# 指纹打进包里：同事报问题时先对这个数，能立刻分清"他装的是哪一版"。
( cd "$STAGE/zk-delta" && shasum -a 256 sidecar/sidecar.js common/framing.js > SHA256SUMS.txt )
echo "zk-delta 源码指纹:"; sed 's/^/  /' "$STAGE/zk-delta/SHA256SUMS.txt"

# lark-* skills 09-03 起**不随包**(用户:同事被飞书那步搞糊涂)。要带上:LARK_SKILLS=1 sh package_team_setup.sh,
# 且安装器要显式 --lark 才会用到它。
if [ "${LARK_SKILLS:-0}" = "1" ]; then
  LARK_SK_SRC="${LARK_SKILLS_SRC:-$HOME/.agents/skills}"; mkdir -p "$STAGE/lark-skills"; n=0
  for d in "$LARK_SK_SRC"/lark-*; do [ -d "$d" ] || continue; cp -RL "$d" "$STAGE/lark-skills/"; n=$((n+1)); done
  echo "lark-skills: $n 个"
fi

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

# 双击升级器(Mac):老用户升级 —— **一个字都不问 Key**。
# 为什么不让他直接跑 INSTALL:INSTALL 会在 TTY 上停下来问"粘贴 API Key",老用户的 Key
# 早就在库里了,那一问只有两个结果 —— 他找不到 Key 卡在那儿,或者他随手回车(没坏,但又
# 多了一次"这步是干啥的"的来回)。--upgrade 直接跳过那一问,并顺手把下架的模型名
# 从他库里摘掉,让他菜单里的名字和文档那份一致。
cat > "$STAGE/UPGRADE-Mac.command" <<'MACUP'
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "升级 __MODEL_PREFIX__:同步模型菜单(加新名 / 摘掉已下架的名字)+ 重打 Cursor 补丁 + 更新小代理。"
echo "不会问你要 Key —— 你之前配过的 Key 原样保留。"
echo "（Cursor 必须先完全退出：Cmd+Q）"
echo ""
sh "$DIR/cursor_team_setup.sh" --upgrade
echo ""
printf "按回车键关闭本窗口…"; read _
MACUP

# 双击升级器(Windows)
cat > "$STAGE/UPGRADE-Windows.cmd" <<'WINUP'
@echo off
chcp 65001 >nul
echo 升级 __MODEL_PREFIX__:同步模型菜单(加新名 / 摘掉已下架的名字)+ 重打 Cursor 补丁。
echo 不会问你要 Key —— 你之前配过的 Key 原样保留。
echo （Cursor 必须先完全退出：右键托盘图标 Quit）
echo.
call "%~dp0cursor_team_setup.cmd" --upgrade
echo.
pause
WINUP

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

# 双击「远程开发也能用我们的模型」(Mac):先体检列出可选主机 → 让他输一个 → 预演 → yes 才写。
# 为什么不静默一把梭:它改的是 ~/.ssh/config —— 同事的常驻配置,里面可能有跳板机、
# 公司内网规则。所以照 UNINSTALL 的老规矩,先把要加的行原样摆出来给他看。
cat > "$STAGE/REMOTE-SSH-Mac.command" <<'MACRS'
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "让「Cursor 连远程服务器」的窗口里也能用 __MODEL_PREFIX__ 的模型。"
echo ""
echo "为什么需要这一步:装完之后你的模型地址是 127.0.0.1:8788(你自己这台电脑上的小代理),"
echo "而远程窗口里的 127.0.0.1 指的是**远端那台服务器**,那边没有这个东西 ⇒ 模型全连不上。"
echo "这个脚本把远端的这个地址转回你本机。Cursor 里一个设置都不用改。"
echo ""
sh "$DIR/cursor_remote_ssh.sh"
echo ""
printf "要配哪台?(照着上面的名字填,或 用户名@IP;直接回车=退出): "
read HOSTNAME_IN
if [ -z "$HOSTNAME_IN" ]; then
  echo "已退出,什么都没动。"
  printf "按回车键关闭本窗口…"; read _; exit 0
fi
echo ""
echo "===== 先预演一遍,只显示要往 ssh 配置里加什么,不落盘 ====="
sh "$DIR/cursor_remote_ssh.sh" --host "$HOSTNAME_IN"
echo ""
printf "确认执行?输入 yes 回车(其它任何输入=取消): "
read ans
if [ "$ans" = "yes" ]; then
  sh "$DIR/cursor_remote_ssh.sh" --host "$HOSTNAME_IN" --apply
else
  echo "已取消,什么都没动。"
fi
echo ""
printf "按回车键关闭本窗口…"; read _
MACRS

# 双击「远程开发也能用我们的模型」(Windows):同上
cat > "$STAGE/REMOTE-SSH-Windows.cmd" <<'WINRS'
@echo off
chcp 65001 >nul
echo 让「Cursor 连远程服务器」的窗口里也能用 __MODEL_PREFIX__ 的模型。
echo.
call "%~dp0cursor_remote_ssh.cmd"
echo.
set /p HOSTNAME_IN=要配哪台?(照着上面的名字填,或 用户名@IP;直接回车=退出):
if "%HOSTNAME_IN%"=="" (
  echo 已退出,什么都没动。
  pause
  exit /b 0
)
echo.
echo ===== 先预演一遍,只显示要往 ssh 配置里加什么,不落盘 =====
call "%~dp0cursor_remote_ssh.cmd" --host "%HOSTNAME_IN%"
echo.
set /p ans=确认执行?输入 yes 回车(其它任何输入=取消):
if /i "%ans%"=="yes" (
  call "%~dp0cursor_remote_ssh.cmd" --host "%HOSTNAME_IN%" --apply
) else (
  echo 已取消,什么都没动。
)
echo.
pause
WINRS

# 双击恢复原状(Mac):bundle 还原 + BYOK 配置/Key 清掉 + 卸小代理 + 解升级锁。
# 先跑一遍**不带 --apply 的预演**给他看清要动什么,再让他打 yes —— 这一步是不可逆的
# (bundle 会被同版本备份盖回去),所以不做静默执行。
cat > "$STAGE/UNINSTALL-Mac.command" <<'MACU'
#!/bin/sh
DIR="$(cd "$(dirname "$0")" && pwd)"
echo "把 Cursor 恢复成装 __MODEL_PREFIX__ 之前的样子。"
echo "（Cursor 必须先完全退出：Cmd+Q）"
echo ""
echo "===== 先预演一遍,只显示要动什么,不落盘 ====="
sh "$DIR/cursor_team_setup.sh" --uninstall
echo ""
printf "确认执行?输入 yes 回车(其它任何输入=取消): "
read ans
if [ "$ans" = "yes" ]; then
  sh "$DIR/cursor_team_setup.sh" --uninstall --apply
else
  echo "已取消,什么都没动。"
fi
echo ""
printf "按回车键关闭本窗口…"; read _
MACU

# 双击恢复原状(Windows):同上
cat > "$STAGE/UNINSTALL-Windows.cmd" <<'WINU'
@echo off
chcp 65001 >nul
echo 把 Cursor 恢复成装 __MODEL_PREFIX__ 之前的样子。
echo （Cursor 必须先完全退出：右键托盘图标 Quit）
echo.
echo ===== 先预演一遍,只显示要动什么,不落盘 =====
call "%~dp0cursor_team_setup.cmd" --uninstall
echo.
set /p ans=确认执行?输入 yes 回车(其它任何输入=取消):
if /i "%ans%"=="yes" (
  call "%~dp0cursor_team_setup.cmd" --uninstall --apply
) else (
  echo 已取消,什么都没动。
)
echo.
pause
WINU

chmod +x "$STAGE/INSTALL-Mac.command" "$STAGE/UPGRADE-Mac.command" "$STAGE/REPAIR-Mac.command" "$STAGE/TURN-OFF-DELTA-Mac.command" "$STAGE/REMOTE-SSH-Mac.command" "$STAGE/UNINSTALL-Mac.command" "$STAGE/cursor_team_setup.sh" "$STAGE/cursor_remote_ssh.sh"

cat > "$STAGE/README.txt" <<'RD'
__MODEL_PREFIX__ 一键安装(零依赖,不用装 Python / Node)

━━━ 已经装过的同事:双击 UPGRADE(不是 INSTALL,也不是 REPAIR)━━━
  Mac     :双击 UPGRADE-Mac.command
  Windows :双击 UPGRADE-Windows.cmd
  它**不会问你要 Key** —— 你之前配过的 Key 原样保留(一个字节都不动)。要先完全退出 Cursor(Cmd+Q)。
  做四件事:
  1) 模型菜单**整份换成**下面那份清单(不是"缺的补上":清单之外的名字会被清掉)
  2) 所以你自己手工加过的第三方模型名(以及历史上装过的旧名)会一起被清掉。
     ⚠️ 这一步是**破坏性**的,所以动手之前脚本会先把你旧的那份清单备份到
        ~/.cursor-team-setup-backup/<版本>-<时间>-preconfig/applicationUser.blob.json
        (备份写不下去就直接中止,不会带着"没有退路"改你的库)。
        窗口里会**逐个列出**加了哪些、删了哪些,你可以对着看。
        想整份还回去:双击 UNINSTALL,或找管理员用 `--revert` 从那个备份恢复。
     如果你某个功能位(Chat / Cmd-K / Deep Search…)正好钉着一个被清掉的名字,
     会自动改回 __DEFAULT_MODEL__ —— 否则菜单里找不到它,你也没法自己换回来。
  3) 重打 Cursor 界面补丁(等于 REPAIR 那一步)。**本次(09-22)新增一条**:
     修掉「上下文百分比显示成 8146%、每问一句都在压缩历史」那个毛病。
     原因是 Cursor 服务端从 9 月 20 号前后开始,把上下文窗口大小回成了"500"这种
     档位数字而不是真实的 500000,小了 1000 倍,于是 Cursor 认为你永远超限、每轮都要
     压缩。**所有模型都中,不是某一个模型的问题**,也不是你 Key 或设置的问题。
     补丁把这个数字还原成真实值:该压的时候(用到 90%)照样压,没到就不压。
  4) 更新省流量的小代理(REPAIR 不做这一步)
  你的 Key、聊天记录、编辑器设置一个字节不动 —— 换的只有"模型菜单"这一份清单。

  三个按钮的区别,一句话:
    INSTALL = 第一次装(会问 Key)   UPGRADE = 已装过要更新(不问 Key)
    REPAIR  = Cursor 升级后模型没了,只重打补丁(不改菜单、不更新小代理)

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

看到"完成"就装好了:启动 Cursor,模型菜单默认就是 __DEFAULT_MODEL__,直接用。

模型清单(本版会把你的菜单**整份换成**这份,顺序就是你在菜单里看到的顺序)
__MODEL_LIST__
  已下架:__RETIRED_MODELS__ —— 装过旧版的机器上,UPGRADE 会自动摘掉。
  注:sa-grok-imagine 已从菜单去掉。它是**出图专用**,在 Cursor 的聊天线型上直接
     报错(实测 400 invalid_request_error),留在菜单里只会让人点了报错。
  注:cr-g-5.6-thinking-min/high/max、cr-g-5.6-luna-min/high/max 是**实验档**
     (只换了 reasoning 档位,载体和已验过的那条一样)。

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

要用 Cursor 连远程服务器改代码?(左下角那个绿色按钮)
  症状:本地窗口好好的,一连上远程服务器,模型全报连不上 / Connection error。
  原因:上面那个小代理跑在**你自己这台电脑**上(127.0.0.1:8788)。远程窗口里的
       127.0.0.1 指的是**远端那台服务器**,它那边没有这个东西,所以必然连不上。
  一键解决:双击 REMOTE-SSH-Mac.command(Windows: REMOTE-SSH-Windows.cmd)
       它会列出你 ssh 里已有的服务器,你挑一台,**先预演给你看要加什么**,
       你输入 yes 才真的改。改的只有一行,加在 ~/.ssh/config 里:
           RemoteForward 8788 127.0.0.1:8788
       作用是:你 ssh 连过去的时候,顺手把远端的 8788 转回你本机的 8788。
       这样两边的地址都成立 —— **Cursor 里一个设置都不用改**,你的 Key 也始终
       只待在自己电脑上(不会落到那台服务器上,那种机器通常是多人共用的)。
  改完:完全退出 Cursor(Cmd+Q)重开,再连远程,发一条消息试试。
  撤掉:同一个按钮里没有撤销;要撤跑 `sh cursor_remote_ssh.sh --host <那台> --remove --apply`,
       或直接还原它给你留的备份 ~/.ssh/config.bak-<时间戳>(它写之前一定先备份)。
  两个常见误会:
   · 连第二个窗口时 ssh 可能提示 "remote port forwarding failed for listen port 8788"
     —— **无害,别去修**。那是第一个窗口已经占着了,模型照样能用。
   · 如果你的模型地址不是 127.0.0.1(比如 Windows 上就是直连公网),
     脚本会当场告诉你"不需要做这件事",然后一个字都不改。

Cursor 升级后 __MODEL_PREFIX__ 的模型不见了怎么办?
  不用回滚升级。双击 REPAIR-Mac.command(Windows: REPAIR-Windows.cmd)一键修复,
  重启 Cursor 即可。你的 Key 和配置都还在,修复只重打补丁。

不想用了 / 要把 Cursor 恢复成装之前的样子
  先完全退出 Cursor(Mac: Cmd+Q;Windows: 右键托盘图标退出),然后
  Mac     :双击 UNINSTALL-Mac.command
  Windows :双击 UNINSTALL-Windows.cmd
  它会**先预演一遍**列出要动什么,你输入 yes 才真的执行。做四件事:
  1) 把 Cursor 的界面文件从备份还原(只用与当前 Cursor 同版本的备份 —— 版本不对它会
     拒绝,并让你用官方安装包覆盖安装一次,那样也能回原厂,聊天记录和设置不会丢)
  2) 关掉 BYOK(地址清空、开关关掉)、摘掉 __MODEL_PREFIX__ 那些模型名,
     并把你装之前自己选的模型还回去(从备份里取,不是一律重置)
  3) 从钥匙串/库里删掉写进去的 API Key
  4) 卸掉本机小代理、去掉升级锁
  你自己加的第三方模型、你的聊天记录、你的编辑器设置都不动。
  备份目录(~/.cursor-team-setup-backup)也不删,想装回来再双击 INSTALL 就行。

出问题看飞书文档的"常见问题",或找管理员。
RD

# README.txt 里的模型名**不写死** —— 从安装器常量里抠。写死的后果是换代之后
# 说明书对同事报旧真相(2026-09-02 就踩到:菜单已是 cr-g-*,README 还写 cursor-g-5.6-sol)。
DEF_MODEL=$(grep -m1 '^const DEFAULT_MODEL = ' "$HERE/cursor_team_setup.js" | sed 's/.*"\(.*\)".*/\1/')
MODEL_PFX=$(grep -m1 '^const MODEL_PREFIXES = ' "$HERE/cursor_team_setup.js" | sed 's/.*\["\([^"]*\)".*/\1/')
if [ -z "$DEF_MODEL" ] || [ -z "$MODEL_PFX" ]; then
  echo "FATAL: 从 cursor_team_setup.js 抠不到 DEFAULT_MODEL / MODEL_PREFIX(常量改名了?)" >&2; exit 1
fi
# 模型清单 / 下架名单同理从常量抠。抠不到就**中止打包**,不许默默出一份没有清单的 README
# ——"README 里这一节是空的"同事不会来问,他只会按旧印象用。
awk '/^const DEFAULT_MODELS = \[/{f=1;next} f&&/^\];/{exit} f' "$HERE/cursor_team_setup.js" \
  | tr ',' '\n' | sed -n 's/.*"\([^"]*\)".*/  - \1/p' > "$STAGE/.models.txt"
RETIRED=$(awk '/^const RETIRED_MODELS = \[/{print;exit}' "$HERE/cursor_team_setup.js" \
  | sed 's/.*\[//; s/\].*//; s/"//g; s/, */, /g')
if [ ! -s "$STAGE/.models.txt" ] || [ -z "$RETIRED" ]; then
  echo "FATAL: 抠不到 DEFAULT_MODELS / RETIRED_MODELS(常量改名或换行了?)" >&2; exit 1
fi
echo "模型清单 $(wc -l < "$STAGE/.models.txt" | tr -d ' ') 个,下架:$RETIRED"
# __MODEL_LIST__ 是多行,走 sed 的 r(插入文件)+ d(删占位行);单行的走下面那个通用循环。
sed -e "/__MODEL_LIST__/r $STAGE/.models.txt" -e "/__MODEL_LIST__/d" \
    "$STAGE/README.txt" > "$STAGE/README.new" && mv "$STAGE/README.new" "$STAGE/README.txt"
rm -f "$STAGE/.models.txt"
# 占位符要**扫整个 stage**,不只 README —— UNINSTALL 启动器里也写了 __MODEL_PREFIX__,
# 只替 README 的话同事双击看到的是字面量 "__MODEL_PREFIX__"(改这里时实测)。
for f in "$STAGE/README.txt" "$STAGE"/*.command "$STAGE"/*.cmd; do
  [ -f "$f" ] || continue
  sed -e "s/__DEFAULT_MODEL__/$DEF_MODEL/g" -e "s/__MODEL_PREFIX__/${MODEL_PFX%-}/g" \
      -e "s/__RETIRED_MODELS__/$RETIRED/g" \
      "$f" > "$f.new" && mv "$f.new" "$f"
done
chmod +x "$STAGE"/*.command
LEFT=$(grep -rl "__DEFAULT_MODEL__\|__MODEL_PREFIX__\|__RETIRED_MODELS__\|__MODEL_LIST__" "$STAGE" 2>/dev/null | tr '\n' ' ')
if [ -n "$LEFT" ]; then
  echo "FATAL: 占位符没替换干净:$LEFT" >&2; exit 1
fi
echo "占位符替换完成:默认模型=$DEF_MODEL 前缀=${MODEL_PFX%-}"

# 先删旧包。zip -r 是**往已有归档里追加**，不是重建：改过文件名之后旧名字会留在包里，
# 同事解出来会多一个乱码文件(第一次改名时实测踩到)。
rm -f "$HERE/cursor-g-setup.zip"
( cd "$(dirname "$STAGE")" && zip -r -X "$HERE/cursor-g-setup.zip" "cursor-g-setup" >/dev/null )
echo "built: $HERE/cursor-g-setup.zip"
unzip -l "$HERE/cursor-g-setup.zip"
