---
name: claude-oneclick-rollout
description: 给同事铺 Claude Code(命令行 + 桌面 App)接 198 网关的一键脚本 SOP——claude-mac.command / claude-windows.bat,自动装 Node/claude、合并 settings.json、配桌面 App 三方模式、预置 App 钉死的 Claude Code 组件、198 /ccdl/ 国内镜像(每日自动跟版)。Use when 改 claude-oneclick 下任何脚本 / 同事说 Claude App「App unavailable」「发消息很慢不回」「只能选一个模型」/ 新开窗口报 Model "claude-router-…" isn't recognized / 要更新《Claude Code 一键配置》文档 / App 自动升级后卡住。
---

# Claude Code 一键配置(CLI + 桌面 App)

用户文档:飞书《Claude Code 一键配置》`MeLad7oMiobwf6xnpA2cWy0nnSg`;正文由 `scripts/agent-oneclick-docs/` 生成(三篇同骨架),
收口门禁 `scripts/agent-oneclick-docs-audit.py`。同族:Cursor(skill `cursor-client-bundle-patch`)、Codex(skill `codex-oneclick-rollout`)。
三个 agent 每人一把 cursor key 共用。
**给同事的入口 = 单页 https://chat.auto-link.com.cn/oneclick/**(skill `agent-oneclick-page`):重建了本工具的安装器后,必须再跑 `bash scripts/agent-oneclick-docs/publish_198.sh`,否则页面里内嵌的还是旧文件。

**自检(doctor,10-16)**:`src/doctor-{mac.sh,windows.ps1}` → build 出 `claude-doctor-{mac.command,windows.bat}`,单页「出问题了」里可下载。只读;查 settings.json(BASE/API_KEY/AUTH_TOKEN 并存/发现开关/claude-router- 旧名)、rc 与当前终端的覆盖变量、桌面 App 3P 配置与两把 key 是否一致、App 钉死版本的组件是否预置 + 198 镜像有没有这版、App 日志 `Host Claude Code binary not available`、代理、key 实打 /v1/messages。回归 `tests/test_doctor.py`;Windows 版 App 日志路径按 App 代码=`%LOCALAPPDATA%\Claude-3p\logs`(未实机验)。

## 1. 文件与发版

```
claude-oneclick/
  src/install-mac.sh  src/install-windows.ps1   ← 只改这里
  tests/                                         ← 离线回归(build.sh 先跑,不过不出产物)
  build.sh                                       → claude-mac.command / claude-windows.bat
  ccdl-sync.sh                                   ← 198 镜像跟版(已装成 /usr/local/bin/carher-ccdl-sync.sh,cron 06:17)
```
发版:`bash claude-oneclick/build.sh` → 干净 HOME 真跑
(`printf '%s\n' "$KEY" | env -i HOME=$T PATH=/opt/homebrew/bin:/usr/bin:/bin TERM=xterm TMPDIR=$T bash claude-mac.command`,
看到「✔ 桌面 App 组件就绪」「✔ 通了」)→ 换飞书附件(+media-insert → 下载回来比 sha → move_after → 删旧)→ 跑门禁。

## 2. 桌面 App 的三个坑(都从 App 自身 asar 读出来,App 升级后要复核)

| 症状 | 真因 | 处置 |
|---|---|---|
| 「App unavailable / only in certain regions」 | App 不读 `~/.claude/settings.json`;它有独立三方模式 `<userData>-3p/configLibrary/`(`_meta.json` appliedId + `<uuid>.json` inferenceProvider=gateway…) | 安装器写这两个文件 |
| 发消息很慢/一直不回,日志 `still initializing; queued message`、`downloads.claude.ai … Stalled` | App 每开会话先下载它**钉死版本**的 Claude Code;国内连不上 | 198 `/ccdl/claude-code-releases/<ver>/` 镜像 + 安装器按 `storageDir/<ver>/` + `.verified`(=manifest sha)预置 |
| 🔴 **App 自动升级后又卡**(10-14 本机:1.15962 → 1.46388.4,钉 2.1.260,镜像没有) | 每次升级都换钉死版本 | `ccdl-sync.sh` 每天跟官方最新 App;同事 App 是旧版就 `sudo carher-ccdl-sync.sh --cc <ver>` 补;本机用安装器同款逻辑预置后**完全退出重开** |

读钉死版本:`app.asar` 里 `JSON.parse('{"version":…,"manifest":{…}}')` 的 `manifest.version`。
⚠ `grep` 大文件会被 ugrep 复杂度限制误报 0 命中 ⇒ 用 python 读 bytes 数。
仍在反复下载的 `vms/linux/<arch>/<sha>/rootfs.img.zst`(1.2G)是 Cowork VM 用的,Code 标签页不依赖,未镜像。

## 3. 命令行的坑

- `ANTHROPIC_AUTH_TOKEN` 能聊天但拉不到网关模型列表 ⇒ 只留 `ANTHROPIC_API_KEY`;两个同时在启动就告警。
  老终端窗口还带着改配置前的环境变量 ⇒ **关掉旧窗口新开**(10-13 本机 00:10 开的标签页一直报)。
- `.zshrc` 里旧 `ANTHROPIC_MODEL=anthropic.claude-opus-5` 等会盖过 settings.json ⇒ 安装器注释掉并备份。
- 🔴 **新开窗口报 `Model "claude-router-<hex>" isn't recognized`(10-14)**:旧版 Claude Code 把非 claude 网关模型显示成
  `claude-router-<hex>` 并存进 `"model"`;2.1.29x 起直接认真名。安装器现在:① 把 `claude-router-<hex>` 还原成真名
  ② 拉 `/v1/models`,非 claude 文本模型写进 `modelPicker.options`(`behavesAs: claude-sonnet-5-5`,出图名/codex-auto-review 不进)
  ⇒ 不告警、菜单里就是真名。`claude --model gpt-5.6-terra` 直接可用(无 picker 时只打一行警告,照样能回)。
- 验菜单长什么样:tmux 里起 `claude` 再发 `/model`,**HOME 指干净安装目录 + unset 本机 ANTHROPIC_***,否则看到的是你自己的旧变量。
  首次进目录有「信任此文件夹」默认选 No ⇒ 先 Down 再 Enter。`.claude.json` 没有 `hasCompletedOnboarding` 会卡在 OAuth 登录页。

## 4. 文档里别写的

`claude-router-` 编码表(旧版残留)、手动 npm 安装命令(CLI 和 App 是并列两个选项,命令行由脚本自动装)、
`~/Downloads/` 这种写死目录的终端命令、要用户手填的网关地址。门禁 stale 列表已拦。
