---
name: claude-oneclick-rollout
description: 给同事铺 Claude Code(命令行 + 桌面 App)接 198 网关的一键脚本 SOP——claude-mac.command / claude-windows.bat,自动装 Node/claude、合并 settings.json、配桌面 App 三方模式、预置 App 钉死的 Claude Code 组件、198 /ccdl/ 国内镜像(每日自动跟版)。Use when 改 claude-oneclick 下任何脚本 / 同事说 Claude App「App unavailable」「发消息很慢不回」「只能选一个模型」/ /model 菜单重复、太多、有画图模型或 Fable / App 里 npm install 失败、设置页「严格沙箱 · 组织开启」/ 新开窗口报 Model "claude-router-…" isn't recognized / 要更新《Claude Code 一键配置》文档 / App 自动升级后卡住。
---

# Claude Code 一键配置(CLI + 桌面 App)

用户文档:飞书《Claude Code 一键配置》`MeLad7oMiobwf6xnpA2cWy0nnSg`;正文由 `scripts/agent-oneclick-docs/` 生成(三篇同骨架),
收口门禁 `scripts/agent-oneclick-docs-audit.py`。同族:Cursor(skill `cursor-client-bundle-patch`)、Codex(skill `codex-oneclick-rollout`)。
三个 agent 每人一把 cursor key 共用。
**给同事的入口 = 单页 https://chat.auto-link.com.cn/oneclick/**(skill `agent-oneclick-page`):重建了本工具的安装器后,必须再跑 `bash scripts/agent-oneclick-docs/publish_198.sh`,否则页面里内嵌的还是旧文件。

**画图(10-16)**:安装器第 4d 步装 `~/.claude/carher/carher_image.js`(源在 codex-oneclick/src,build.sh 的 @@EMBED 从那里取)+ `skills/carher-image` + `~/.claude/CLAUDE.md` 里 `CARHER-IMAGE` 标记块。🔴 只放 skill 不够:实测 `claude -p "画一张…"` 直接写 SVG,不调 skill;加了常驻说明块才真出图。
**发版**:改完跑 `bash scripts/agent-oneclick-docs/release_all.sh`(网页 + 飞书附件 + 门禁一起)。
**自检(doctor,10-16)**:`src/doctor-{mac.sh,windows.ps1}` → build 出 `claude-doctor-{mac.command,windows.bat}`,单页「出问题了」里可下载。只读;查 settings.json(BASE/API_KEY/AUTH_TOKEN 并存/发现开关/claude-router- 旧名)、rc 与当前终端的覆盖变量、桌面 App 3P 配置与两把 key 是否一致、App 钉死版本的组件是否预置 + 198 镜像有没有这版、App 日志 `Host Claude Code binary not available`、代理、key 实打 /v1/messages。回归 `tests/test_doctor.py`;Windows 版 App 日志路径按 App 代码=`%LOCALAPPDATA%\Claude-3p\logs`(未实机验)。

## 对外域名 = chat.auto-link.com.cn(2026-10-29)

- 安装器写 `https://chat.auto-link.com.cn/model`(settings.json + App 3P 配置),组件镜像走 `chat…/ccdl/claude-code-releases`;
  新地址检查 key 不是 200/401/403、旧 `cc…/pro` 是 200 ⇒ 这次先写旧的。
- **老用户迁移在 `carher_models_refresh.js`**(开会话 hook + 「刷模型」都走它):`env.ANTHROPIC_BASE_URL` 精确等于旧地址 ⇒ 用这把 key 真打新地址 `/v1/models`
  (`fetchIdsLive`,不吃 `CARHER_MODELS` 测试注入),拿到列表才改;App 那条(CarHer + 同 key)的 `inferenceGatewayBaseUrl` 同步换,App 要完全退出重开。
  不通 ⇒ 原样,`refresh.log` 记 `keep old base`。别家网关/本机代理/形状不同的地址一律不碰。回归 `tests/test_domain_migrate.py`(含阳性对照)。
- doctor:旧地址只报⚠️。

## /model 菜单 = 只放我们按 key 生成的那份(10-25 定型)

**菜单是三拨叠加的**:Claude Code 自带(Default/Opus/Fable/Sonnet/Haiku)+ 网关发现(`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`,
标「From gateway」)+ `settings.json` 的 `modelPicker`(标「Custom model」)。10-25 用户截图 74 行(10+64)而脚本说 26 ——
数 JSON 永远对不上屏幕,**判菜单只用 `scripts/cc-model-menu.sh`**(真起 claude、按 /model 滚到底、报总行数和重复)。

现行做法(`src/carher_models_refresh.js`,安装器、「刷模型」、开会话 hook 三处共用;导出 `applyModels`/`pickerRows`/`hookEntry`/`approveApiKey`/`syncAppModels`/`appConfigEntries`):
- `modelPicker.options` = 这把 key **全部可聊天模型**:`claude-*` 在前(fable→opus→sonnet→haiku,同家族版本新→旧,
  **不写 label** ⇒ Claude Code 自己显示 Opus 5.5);非 claude 带 `behavesAs: claude-sonnet-5-5`(不写就每次启动告警按 200k 算)。
  出图/视频/音乐、`codex-auto-review` 不进。
- `replaceBuiltInOptions: true` ⇒ 菜单只剩「Default + 这些行」,自带行和网关发现全隐藏(官方 settings-reference#modelpicker)。
  本机实测 37 行无重复、无出图、无 Fable;grok/kimi/gpt/haiku-5.5/sonnet 实打 OK 无告警。
- ⛔ **别改成「只靠网关发现」**(用户 10-25 提过,对照否了):198 对 claude-cli UA 把非 claude 模型改名成 `claude-router-<hex>`
  (发现只收含 claude 的 id),显示乱码 + 每次启动 `unrecognized_model` 告警;出图模型进菜单;自带 Fable 打出去 403。
- **自动刷新**:安装器在 `hooks.SessionStart` 挂 `{matcher:"startup", command:<node绝对路径>, args:[脚本,"--quiet"], async:true, timeout:60}`。
  exec 形式不经 shell(路径带空格、Windows 无 Git Bash 都行,需 2.1.139+);`--quiet` 不出声、失败退 0、只写
  `~/.claude/carher/refresh.log`;先写临时文件再 rename(Claude Code 监视 settings.json)。重跑幂等,用户自己的 hook 不动。
  实测:菜单删掉 kimi-k3 + 塞假名 ⇒ 开一次会话自动恢复。「刷模型」skill(`carher-refresh-models-claude`)+ `CLAUDE.md` 的 `CARHER-REFRESH` 块保留当「立刻刷」。
- 当前 `model` 是网关名且已没权限 ⇒ 删掉回默认(`opus` 别名、`[1m]` 后缀不动)。拉不到列表就不改;改前备份 `.bak-carher-refresh`。
- **haiku**:写 `env.ANTHROPIC_DEFAULT_HAIKU_MODEL` = key 里最新的 `claude-haiku-*`(版本按数字比);我们写过的老 haiku 会升,
  用户指到非 haiku 的不动。10-25 网关已上 `claude-haiku-5-5`(Claude Code 默认要它,之前 403)。
- doctor 报「菜单去重 / 开机自动刷菜单 / 后台小任务模型」。回归:`tests/test_install_mac.py`(去重、hook 幂等、quiet 失败退 0)。

## 桌面 App 菜单 + 「Not logged in」(10-27,王德升)

- **App 的模型菜单不读 settings.json**:App 启动时自己拉一次网关 `/v1/models`,结果只在内存里。App 配置(configLibrary/<id>.json)
  写了 `inferenceModels` ⇒ App 不再自己拉、菜单 = 这份(日志 `Model discovery: skipped (inferenceModels obviates discovery); picker = N`)。
  `carher_models_refresh.js` 的 `syncAppModels` 把 key 里的 `claude-*` 写进**我们那条**(_meta 里叫 CarHer、且 key 一样);
  App 网关模式只收 Anthropic 名(`wCe`:不像 Anthropic 模型就拒),所以非 claude 不写。key 里没 claude ⇒ 删掉这项交回 App。
  ⚠️ App 要**完全退出再开**才读新配置。验:真 App 改配置 → 重启 → 看 main.log 那行 + Code 标签页右下角模型名。
- **「Not logged in · Run /login」**:首次启动「Do you want to use this API key?」默认 **No**,回车就写进 `~/.claude.json`
  (`CLAUDE_CONFIG_DIR` 下则是那里的 `.claude.json`)的 `customApiKeyResponses.rejected`,按 key **末 20 位**比较。
  安装器和刷模型都调 `approveApiKey` 记成 approved。doctor 报「这把 key 已同意使用」。
- 同事 403「key_model_access_denied」= key 白名单没那个模型(王德升只有 deepseek-v4.1-flash / claude-haiku-5-5),不是脚本问题;开权限要用户定。

## 1. 文件与发版

```
claude-oneclick/
  src/install-mac.sh  src/install-windows.ps1   ← 只改这里
  tests/                                         ← 离线回归(build.sh 先跑,不过不出产物)
  build.sh                                       → claude-mac.command / claude-windows.bat
  ccdl-sync.sh                                   ← 198 镜像跟版(已装成 /usr/local/bin/carher-ccdl-sync.sh,cron 06:17)
  src/carher_models_refresh.js                   ← 菜单 + haiku + 自动刷新 hook(两个安装器 @@EMBED)
.claude/skills/claude-oneclick-rollout/scripts/
  cc-model-menu.sh      ← 真实 /model 菜单(行数/重复),可指隔离 CLAUDE_CONFIG_DIR
  app-model-menu.sh     ← 桌面 App 当前菜单从哪来(inferenceModels / 自己拉)+ 最近一次 Model discovery 日志行
  app-sandbox-ab.sh     ← App 严格沙箱对照(--managed-settings 喂 App 那份 vs 不加)
```
发版:`bash claude-oneclick/build.sh` → 干净 HOME 真跑
(`printf '%s\n' "$KEY" | env -i HOME=$T PATH=/opt/homebrew/bin:/usr/bin:/bin TERM=xterm TMPDIR=$T bash claude-mac.command`,
看到「✔ 桌面 App 组件就绪」「✔ 通了」)→ 换飞书附件(+media-insert → 下载回来比 sha → move_after → 删旧)→ 跑门禁。
- 在用户本机跑真安装器(10-25 做过两次):key 从 settings.json 读、经 stdin 喂(`printf '%s\n' "$K" | bash claude-mac.command`),
  先把 settings.json / zshrc / bashrc / `Claude-3p/configLibrary` 备份进 `.work/pre-install-<ts>/`,跑完逐键 diff
  (只该变 hooks/modelPicker,env 必须原样)。已有 `~/.claude/CLAUDE.md` 会被追加标记块,不会覆盖。
- 发完验「真的上线了」:下载 https://chat.auto-link.com.cn/oneclick/ ,解出 `data:` 内嵌的 claude-*.command/.bat,数关键串(sha 对上 + 新逻辑在)。

## 2. 桌面 App 的四个坑(都从 App 自身 asar 读出来,App 升级后要复核)

| 症状 | 真因 | 处置 |
|---|---|---|
| 「App unavailable / only in certain regions」 | App 不读 `~/.claude/settings.json`;它有独立三方模式 `<userData>-3p/configLibrary/`(`_meta.json` appliedId + `<uuid>.json` inferenceProvider=gateway…) | 安装器写这两个文件 |
| 发消息很慢/一直不回,日志 `still initializing; queued message`、`downloads.claude.ai … Stalled` | App 每开会话先下载它**钉死版本**的 Claude Code;国内连不上 | 198 `/ccdl/claude-code-releases/<ver>/` 镜像 + 安装器按 `storageDir/<ver>/` + `.verified`(=manifest sha)预置 |
| App 的 Code/Cowork 里 `npm install` 失败,设置页「严格沙箱 · 组织开启」,开 bypass 也没用,终端正常(10-25 mac 同事) | 3P 配置没写 `coworkEgressAllowedHosts` ⇒ App 按 failClosed 当 `[]` ⇒ 给会话下发 `sandbox{enabled,allowUnsandboxedCommands:false,network.allowedDomains:[],allowManagedDomainsOnly}`;对照:`claude -p --managed-settings '<那份>'` ⇒ npm E403 `user denied`、curl 000。只放 npm 源也不够(沙箱挡 `~/.npm` 写,EPERM) | 安装器写 `coworkEgressAllowedHosts:["*"]`(官方:含 `*` 且无 `allowedWorkspaceFolders` ⇒ 两种沙箱都不开);用户自己写过非空列表不动;doctor 报「App 外网放行」。改后要**完全退出 App 重开** |
| 🔴 **App 自动升级后又卡**(10-14 本机:1.15962 → 1.46388.4,钉 2.1.260,镜像没有) | 每次升级都换钉死版本 | `ccdl-sync.sh` 每天跟官方最新 App;同事 App 是旧版就 `sudo carher-ccdl-sync.sh --cc <ver>` 补;本机用安装器同款逻辑预置后**完全退出重开** |
| 🔴 **组件目录换布局**(10-28 复核 4.29 发现;本机 3.1623.4 已是) | 本机那份从 `claude-code/<ver>/` 改成 `claude-code/<ver>/<sha 前 12 位>/`(App 代码 `installDirFor`;老目录当 legacy 启动时搬走);VM 那份 `claude-code-vm/<ver>/` 不变。只写老目录 ⇒ App 一搬走 doctor 就误报「组件没准备好」、重跑又下 100MB | 安装器按 asar 里有没有 `it cannot name an install folder` 判新/老布局;doctor 两种目录都认;回归 `tests/test_desktop_app.py -k hashed`(真跑预置脚本 + 本地假镜像) |
| Linux App(deb `/usr/lib/claude-desktop`)| 10-28 起有官方 Linux 版,同样每开会话下组件;本机那份是**单个 `claude` 二进制**(useBundle 只在 darwin),userData = `~/.config/Claude-3p` | 安装器 Linux 分支同样预置(不下 VM 那份,Linux 是否起 VM 没实测) |

- 组件下载(mac ~100MB、win ~300MB)**偶发 `socket hang up`**(10-28 干净 HOME 实测 1 次,随后连跑 4 次都好)⇒
  两个安装器都已加「断了自动重试 3 次、4xx 不重试」。用户看到「组件没准备好:socket hang up」时让他重跑一次即可。

**App 升级后复核流程**(10-28 跑过):从镜像下 mac zip / win msix / linux deb 各解出 app.asar →
① manifest 正则能读到 ② `Claude-3p`/`coworkEgressAllowedHosts`/`inferenceModels` 键还在 ③ 读 `installDirFor`/`getHostTarget` 看目录规则 →
把安装器里的预置 JS 切出来对三个 asar 真跑(`CARHER_ASAR/CARHER_UD/CARHER_OS/CARHER_ARCH`),目录名要等于 manifest sha 前 12 位 → 干净 HOME 真装 + doctor。

真 App 验沙箱(10-25 本机做过):`claude://code/new?folder=<编码目录>&q=<编码提示>` 深链开 Code 会话 → `osascript` 回车发送 →
让命令把结果写到文件再读(截图会被同屏终端遮住)。改 configLibrary 后必须**确认旧 App 进程退干净**(`osascript quit` 后轮询 pgrep)再开。
读钉死版本:`app.asar` 里 `JSON.parse('{"version":…,"manifest":{…}}')` 的 `manifest.version`。
⚠ `grep` 大文件会被 ugrep 复杂度限制误报 0 命中 ⇒ 用 python 读 bytes 数。
仍在反复下载的 `vms/linux/<arch>/<sha>/rootfs.img.zst`(1.2G)是 Cowork VM 用的,Code 标签页不依赖,未镜像。

## 3. 命令行的坑

- 🔴 **大 MCP 工具包撑爆上下文(10-25 本机 rea 210 工具≈1MB)**:Claude Code 2.1.304 前走网关**不**按需加载 MCP 工具,老会话也按老规矩 ⇒ 每句整份发。安装器默认写 `env.ENABLE_TOOL_SEARCH="true"`(用户自己写过含 false 不动)。
- `ANTHROPIC_AUTH_TOKEN` 能聊天但拉不到网关模型列表 ⇒ 只留 `ANTHROPIC_API_KEY`;两个同时在启动就告警。
  老终端窗口还带着改配置前的环境变量 ⇒ **关掉旧窗口新开**(10-13 本机 00:10 开的标签页一直报)。
- `.zshrc` 里旧 `ANTHROPIC_MODEL=anthropic.claude-opus-5` 等会盖过 settings.json ⇒ 安装器注释掉并备份。
- 🔴 **新开窗口报 `Model "claude-router-<hex>" isn't recognized`(10-14)**:旧版 Claude Code 把非 claude 网关模型显示成
  `claude-router-<hex>` 并存进 `"model"`;2.1.29x 起直接认真名。安装器现在:① 把 `claude-router-<hex>` 还原成真名
  ② 拉 `/v1/models`,非 claude 文本模型写进 `modelPicker.options`(`behavesAs: claude-sonnet-5-5`,出图名/codex-auto-review 不进)
  ⇒ 不告警、菜单里就是真名。10-25 起菜单整份由我们生成并 `replaceBuiltInOptions`,见上「/model 菜单」节。
- 验菜单长什么样:`scripts/cc-model-menu.sh [CLAUDE_CONFIG_DIR]`(内部是 tmux 起 claude 发 /model 滚到底)。隔离验时
  CLAUDE_CONFIG_DIR 指一个只放 settings.json + `.claude.json`(hasCompletedOnboarding、projects 信任)的目录;本机 rc 的旧变量会串进来。
  首次进目录有「信任此文件夹」默认选 No ⇒ 先 Down 再 Enter。`.claude.json` 没有 `hasCompletedOnboarding` 会卡在 OAuth 登录页。

## 4. 文档里别写的

`claude-router-` 编码表(旧版残留)、手动 npm 安装命令(CLI 和 App 是并列两个选项,命令行由脚本自动装)、
`~/Downloads/` 这种写死目录的终端命令、要用户手填的网关地址。门禁 stale 列表已拦。
