---
name: agent-oneclick-page
description: 给同事的「Cursor · Codex · Claude Code 一键配置」单页网页(https://chat.auto-link.com.cn/oneclick/)的改版、构建、上线 SOP——模板 oneclick-all.html、内嵌 10 个文件(安装+自检)的 build_html.py、发版总入口 release_all.sh(网页上线 + 飞书三篇换附件 + 门禁),以及和三篇飞书详细文档的关系。Use when 用户要改/重新发「一键配置」页面、页面上的下载/命令/文案、换了安装器要同步到页面、同事说页面链接点不了/下载不了/下错文件、要把页面挂到别处或「上传到飞书」。
---

# 一键配置单页(给同事的唯一入口)

**线上**:https://chat.auto-link.com.cn/oneclick/ (可加 `#codex` / `#claude` 直达某工具)
**为什么有它**:三篇飞书文档(Cursor / Codex / Claude Code)同事经常下错文件、看错篇;10-15 用户要求合成一页。
三篇飞书文档仍保留作详细版(生成器 `apply.py --all`),但**发给同事的是这个网址**。

## 模型清单不写死(2026-10-19)

页面已删掉「全部 39 个模型」那张表,提示卡加了第三个词「刷模型」。`build_html.py` 会拦住:页面又写出「全部 N 个模型」,
或者「刷模型」不见了,都会报红。三个工具的菜单都按 key 现拉,见 skill `cursor-client-bundle-patch` /
`codex-oneclick-rollout` / `claude-oneclick-rollout` 里的「刷模型」一节。

## 三个桌面 App 安装包都在 198 镜像(2026-10-26)

| App | 稳定链接(页面/飞书只链这些) | 谁维护 |
|---|---|---|
| Claude | `cc.auto-link.com.cn/ccdl/desktop/Claude-{mac.zip,windows-x64.msix,windows-arm64.msix}` | `claude-oneclick/ccdl-sync.sh`,cron 06:17 |
| Cursor | `…/ccdl/apps/Cursor-{mac.dmg,windows-x64.exe,windows-arm64.exe}` | `scripts/agent-oneclick-docs/app-mirror-sync.sh`(198 `/usr/local/bin/carher-app-mirror-sync.sh`),cron 06:37 |
| Codex | `…/ccdl/apps/Codex-{mac.dmg,windows-x64.msix,windows-arm64.msix}` | 同上 |

- 版本号:`/ccdl/apps/versions.json`;日志 198 `/var/log/carher-app-mirror-sync.log`。手动跟版:`sudo carher-app-mirror-sync.sh [cursor|codex]`。
- Cursor 跟 **latest 更新通道**(同事自动升级拿的就是它),所以镜像可能比补丁台架验过的版本新;每日巡检同样看 latest。
- Codex Windows 只有微软商店:`msstore_msix_urls.py 9PLM9XGG6VKS` 走微软 displaycatalog + FE3 拿官方 msix 直链。
  坑:第三方 rg-adguard 有 Cloudflare 人机验证;FE3 证书是 Windows 私有根(只对该主机跳过校验);
  一个更新回主包 + 1MB BlockMap cab 两条地址,必须按 FileDigest 取主包;文件名是随机 GUID,按大小对上架构。
- 校验:字节数对官方 Content-Length / 微软目录;msix 另比 SHA1;全过才切软链。下载回本机 `spctl -a -t open --context context:primary-signature` 应为 Notarized Developer ID。
- `build_html.py` 门禁:App 链接回到官方直链(downloads.cursor.com / persistent.oaistatic.com / get.microsoft.com)即红;镜像链接逐个 HEAD 须 200 且 >10MB(离线构建设 `CX_SKIP_MIRROR_HEAD=1`)。
- Codex 飞书文档原有 8-31 的 dmg/msix 附件已删(`apply.py` 的 RETIRED 集合)。
- **Linux(10-26)**:页面顶部「我用的是」= 另外两个系统的按钮,三选一(UA 认 Linux)。CSS:`mac-only` / `win-only` / `linux-only`,
  **`unix-only` = Mac+Linux 共用**(终端命令、`-mac` 脚本、`~/` 路径、doctor)。只有 dmg / 拖进应用程序 / Cmd+Q / `~/Library` 才是 mac-only。
  Linux 一键配置**就用 -mac 脚本**(三个都认 `uname -s` = Linux)。
  安装包:Cursor `Cursor-linux-{amd64,arm64}.deb` + `{x86_64,aarch64}.rpm`(⛔不发 AppImage:每次解到随机 /tmp/.mount_*,
  安装器找不到也打不进补丁;官方 API 没有 deb/rpm 平台名,用 AppImage 那条的 commit 拼 `production/<commit>/linux/x64/deb/amd64/deb/…`)·
  Codex `Codex-linux-{amd64,arm64}.deb` + `{x86_64,aarch64}.rpm` · Claude `Claude-linux-{amd64,arm64}.deb`(只有 Debian 系)。
- 🔴 Linux 真跑(188 Ubuntu 22.04,空 HOME)踩到并修了:① `curl | grep | head -n1` + pipefail ⇒ SIGPIPE 假失败「取不到 Node 版本列表(网络?)」
  (mac 碰巧不炸);② Cursor 没打开过 ⇒ 没有 state.vscdb ⇒ @vscode/sqlite3 原生 abort(exit 134,try 接不住)⇒ openDb 先判文件。
  验页面三种系统:headless Chrome 末尾注入「点 .os-pick + 展开 details」再截图(localStorage 注入在 file:// 下不生效)。

## 文件

```
scripts/agent-oneclick-docs/
  oneclick-all.html   ← 模板(唯一要手改的);{{文件名}} = 内嵌下载占位,{{BUILD}} = 版本戳
  img/codex-login.jpg ← 唯一保留的截图(Codex「使用其他方式登录」)
  build_html.py       ← 内嵌 10 个文件(安装+自检) + 截图 → .work/oneclick-all/AI编程一键配置.html;--check 只核对
  publish_198.sh      ← build → jms scp → 198 /var/www/oneclick/index.html → 比线上 sha
  swap_attachment.py  ← 飞书附件换新版:插入 → 回读比 sha → 挪到旧块后 → 删旧(失败不删旧;已是新版就跳过)
  release_all.sh      ← **发版总入口**:重建 Codex/Claude → 网页上线 → 飞书三篇换附件 → 门禁
  bodies.py / apply.py← 三篇飞书详细文档(另一条线,改文案时两边一起改)
```

内嵌的 10 个文件(6 个安装 + 4 个自检 `*-doctor-*`)来自各自构建产物;自检放在 Codex / Claude「出问题了」折叠里,标题写「先跑自检，一键出报告」。安装:`scripts/zk-cursor-web/cursor-{mac,windows}.zip`、
`codex-oneclick/codex-{mac.command,windows.bat}`、`claude-oneclick/claude-{mac.command,windows.bat}`。
**换了安装器 ⇒ 先按各自 skill 重建(cursor-client-bundle-patch / codex-oneclick-rollout / claude-oneclick-rollout)⇒ 再 `publish_198.sh`**。

## 改版 → 上线

🔴 **一律用 `bash scripts/agent-oneclick-docs/release_all.sh`**(网页和飞书是同一批文件的两个出口)。
10-16 只跑了 publish_198.sh,飞书 Codex 文档里还挂着旧脚本,收尾门禁才抓到。下面是单步命令:

```bash
# 改 scripts/agent-oneclick-docs/oneclick-all.html 后
python3 scripts/agent-oneclick-docs/build_html.py --check   # 占位/文件/内部词/明文key/39模型对齐/chmod 门禁
bash scripts/agent-oneclick-docs/publish_198.sh             # 上线并比 sha;上一版留 index.html.prev
```

本地预览:`open .work/oneclick-all/AI编程一键配置.html`;截图自查用 headless Chrome(`--window-size=1440,…` 和 `400,…` 各一张)。

页面上的「意见反馈 / 报问题」按钮和各处「还不行」链到 https://chat.auto-link.com.cn/feedback/ ,那条通道见 skill `feedback-intake`。

## 198 布置(10-15 已做好,一般不用再碰)

- 静态文件 `/var/www/oneclick/index.html`
- nginx `/etc/nginx/sites-enabled/chat.auto-link.com.cn.conf` 里 `# >>> carher-oneclick >>>` 一段:
  `/oneclick/` 走 alias 静态、只放 GET/HEAD、`no-cache`;**其余路径仍反代 Open WebUI**
- 备份 `/etc/nginx/backup-oneclick/`;**回滚 = 删这一段 → `nginx -t` → `systemctl reload nginx`**
- 公网可访问;内嵌脚本无 key(build 门禁扫过),但含网关地址——用户 10-15 已知情同意

## 设计定稿(用户多轮否决后,别再回退)

| 决定 | 原因(用户原话/被否方案) |
|---|---|
| 左侧编号竖线:①拿 key ②三张工具大卡 ③起该工具步骤,编号接着数 | 「电脑和工具放一行没层次、放两行别扭」「key 藏得深用户懵」 |
| 电脑系统**自动识别**,一行小字「下面按 Mac 显示 · 我用的是 Windows」可切 | 不让用户先做两道选择 |
| 宽 960 | 740 窄版「太窄怪怪的」,1180 宽版「太乱」 |
| CLI / App **不分**:装 App(可跳过)→ 一键配置 → 开始用(命令行 \| App 两栏) | 「一次全部搞定」,曾加过 CLI/App 切换被撤 |
| 出问题/更新卸载/Remote-SSH/39 模型 折叠在「需要时再看」 | 主线只留必做步骤 |
| 不写「不要开 VPN」「CLI 会自动装」「装过的跳过」类说明句 | 「不需要写就不要放,避免困扰」 |
| 小标题叫「一键配置」不叫「一键安装」 | 用户 10-15 改名(三篇飞书同步改了) |

## 坑(每条都踩过)

- 🔴 **Mac 一律给终端代码块,不让双击**:浏览器下载丢执行位 ⇒ `cd ~/Downloads` + `chmod +x …` + `bash ./…`;
  Cursor 是 zip ⇒ 多一行 `[ -d cursor-mac ] || unzip -q cursor-mac.zip`(Safari 会自动解压,幂等)且 `chmod +x *.command *.sh`(INSTALL 会调同目录 .sh)。build 门禁已拦「有 bash ./ 没 chmod」。
- 🔴 代码块里**不写行尾 `# 注释`**:macOS 默认 zsh 交互模式不认,粘进去第一行就报错 ⇒ 注释放代码块下面的小字。
- 每个 `<pre>` 由页面脚本自动挂「复制」按钮(clipboard 失败回退 execCommand,再失败提示 Cmd/Ctrl+C)。
- 门禁词 `~/Downloads/`(带斜杠)仍禁:写成 `cd ~/Downloads` 再 `cd cursor-mac` 两行。
- 🔴 **「上传到飞书」三种理解,只有原样传云空间文件对**,但**飞书文件预览禁一切跳转/下载**(改 `target=_blank` 无效,10-15 实测),
  所以最后用户放弃飞书承载,改挂 198。别再做:新建文档挂附件、`drive +import` 转在线文档(丢样式/脚本/图片/内嵌下载,图片变「无法导入该图片」)。
- claude.ai Artifact 沙箱拦 `data:` 下载,也不适合承载。
- Codex App 下载用官方直链(国内可达,10-15 实测):Mac `persistent.oaistatic.com/codex-app-prod/Codex.dmg`,
  Win `get.microsoft.com/installer/download/9PLM9XGG6VKS?cid=website_cta_psi`;`openai.com/codex` 国内 403。
  飞书附件直链(/file/<token>)不登录 302 到登录页、且文档未开链接分享 ⇒ 别用。
- lark-cli 没有 `space:document:delete`,误建的飞书文档删不掉 ⇒ 别先建再说,建前想清楚。

## 页面上的终端命令(10-26 重写,用户 review 抓出来的)

模板(6 段都是这个形状,别再写死文件名):
```
d=$(xdg-user-dir DOWNLOAD 2>/dev/null); [ -d "$d" ] && [ "$d" != "$HOME" ] || d=~/Downloads; cd "$d"
f=$(ls -t | grep '^codex-mac.*command$' | head -n 1)          # Cursor:z=$(ls -t | grep '^cursor-mac' | head -n 1)
if [ -n "$f" ]; then bash "$f"; else echo "没找到 …"; fi       # Cursor:是 zip 就 unzip -q -o 覆盖,再 sh "$z/INSTALL-Mac.command"
```
- 为什么:旧的 `[ -d cursor-mac ] || unzip` 会**跑旧文件夹里的旧脚本**;写死名字遇浏览器 `(1)` 就找不到;中文 Linux 叫 ~/下载;
  `chmod +x` 对 `sh/bash x` 无意义。xdg-user-dir 没配时返回 **$HOME 本身**(不是空)⇒ 必须判 `!= "$HOME"`。
- 改命令必跑 `python3 scripts/agent-oneclick-docs/test_page_terminal_blocks.py`(release_all 已接):抠页面原文真跑,
  6 个场景 × zsh/bash × xdg 两种;拿旧命令跑同一套会全红(阳性对照)。Linux 机器上也要跑一次(188),本机 Mac 测不到 xdg 那支。
- 代码块字体关连字(`!=` 会被 JetBrains Mono 显示成 ≠,用户照抄会错)。

## 三个 App 里的「刷模型」(10-27 逐个真验)

| App | 菜单从哪来 | 「刷模型」怎么触发 | 验法 |
|---|---|---|---|
| Claude App | App 配置 `inferenceModels`(写了就不再自己拉) | skill `carher-refresh-models-claude` + `~/.claude/CLAUDE.md` 块 | 改配置 → 完全退出重开 → main.log `picker = N (inferenceModels)` |
| Codex App | 自带 codex 的 app-server `model/list` ⇒ `config.toml` 的 `model_catalog_json` | `~/.codex/AGENTS.md` 的 `CARHER-REFRESH` 块 | 跑 `<App>/Contents/Resources/codex-cli/bin/codex app-server` 发 `model/list` 数条数 |
| Cursor App | Cursor 设置库 userAddedModels | skill `carher-refresh-models-cursor`(worker 自动退出→写→重开) | `cursor-agent -p "刷模型"` + `~/.carher/cursor-refresh.log` |

- 🔴 **skill 不许同名**:Cursor 的 agent 也读 `~/.claude/skills`,两边都叫 `carher-refresh-models` 时它挑了 Claude 那份,
  Cursor 菜单根本没动。现在两个名字带工具后缀,描述写「仅限 X」。新加跨工具 skill 都按这个规矩。
- Cursor 退出检测只认可执行文件本身是 Cursor 的进程(`isCursorExe`);整行匹配会把命令行里带 Cursor 路径的 shell 当 GUI,等 53 秒。
- Codex 隔离测:`CODEX_HOME=<临时目录>` 跑安装器会**覆盖全局 LaunchAgent `net.carher.codex-models-sync`**(label 全局),测完 bootout 并删 plist。
