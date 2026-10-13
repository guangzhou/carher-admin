# Claude Code 一键配置(CLI + 桌面 App)

给同事的文档：飞书《Claude Code 一键配置》`MeLad7oMiobwf6xnpA2cWy0nnSg`。
与 Cursor / Codex 共用同一把 **cursor key**(权限模板 `cursor-liuguoxian-std`)。

```
claude-oneclick/
  src/install-mac.sh        ← 改这里(macOS / Linux)
  src/install-windows.ps1   ← 改这里(Windows)
  tests/                    ← 离线回归,每条对应一个实测坑
  build.sh                  ← 跑测试 → 产出下面两个
  install-mac.command       ← 给用户
  install-windows.bat       ← 给用户(bat 头纯 ASCII+CRLF + PowerShell 正文)
```

## 发版

1. 只改 `src/`,`bash claude-oneclick/build.sh`(测试不过不出产物,末尾打两个 sha256)
2. 干净 HOME 真跑(**PATH 里不要有 node/claude/zstd**,才测得到全新机器):
   ```bash
   printf '%s\n' "$KEY" | env -i HOME=$T PATH=/usr/bin:/bin TERM=xterm TMPDIR=$T bash install-mac.command
   ```
   看到「✔ 桌面 App 组件就绪」和「✔ 通了」才算。
3. 传飞书:`+media-insert` → **下载回来比 sha** → `block_move_after` 到「macOS / Linux 下载：」「Windows 下载：」
   两段后(10-13 起是「👇 macOS / Linux 下载：」「👇 Windows 下载：」,在第 7 节)→ `block_delete` 旧 figure。`block_move_after` 回 1011 不一定是失败,重 fetch 看顺序。
4. 收口:`python3 scripts/agent-oneclick-docs-audit.py`(三个 agent 四篇文档一起验,全绿才算发出去)

## 桌面 App 的两个机制(都是从 App 自身 asar 读出来的,App 升级后要复核)

| 症状 | 真因 | 脚本做了什么 |
|---|---|---|
| 「App unavailable / only in certain regions」 | App **不读** `~/.claude/settings.json` 切网关;它有独立的三方模式,配置在 `<userData>-3p/configLibrary/` | 写 `_meta.json`(appliedId)+ `<uuid>.json`(inferenceProvider=gateway …),只追加一条 CarHer,保留用户已有条目 |
| 发消息不回复,日志 `Host Claude Code binary not available` | App 每开会话先从 `downloads.claude.ai` 拉它**钉死版本**的 Claude Code(host + Linux VM),国内超时 | 从 198 镜像 `https://cc.auto-link.com.cn/ccdl/claude-code-releases/<ver>/` 取官方原样文件,sha 对 App 内嵌 manifest,按 `storageDir/<ver>/` + `.verified` 放好 |

### 镜像(198 nginx)

- 目录 `/var/www/ccdl/claude-code-releases/<ver>/`,只读 `location ^~ /ccdl/`(GET/HEAD,不列目录)。
  配置段带 `# >>> carher-ccdl >>>` 标记,回滚 = 删这一段 + `nginx -t` + `nginx -s reload`。
- **App 升级换了钉死版本** ⇒ 安装器会报「组件没准备好: HTTP 404」⇒ 在 198 上补那个版本:
  ```bash
  V=<新版本>; B=https://downloads.claude.ai/claude-code-releases/$V; D=/var/www/ccdl/claude-code-releases/$V
  curl -fsSL -A Mozilla/5.0 $B/manifest.json | sudo tee $D/manifest.json >/dev/null
  for p in darwin-arm64/claude.app.tar.zst darwin-x64/claude.app.tar.zst darwin-arm64/claude.zst darwin-x64/claude.zst \
           linux-x64/claude.zst linux-arm64/claude.zst linux-x64-musl/claude.zst linux-arm64-musl/claude.zst \
           win32-x64/claude.exe.zst win32-arm64/claude.exe.zst; do
    sudo mkdir -p $D/$(dirname $p); sudo curl -fsSL -A Mozilla/5.0 -o $D/$p $B/$p; done
  sudo chmod -R a+rX /var/www/ccdl
  ```
  (198 自己能直连 `downloads.claude.ai`。sha 由安装器对 App 内嵌 manifest 校验,不对就拒装。)
- **App 安装包也镜像在这里**(10-13 加):`claude.ai/download` 国内跳「地区不可用」,官方 API 对国内/198 的
  mac dmg 都回 403,只有 `RELEASES.json` + `downloads.claude.ai/releases/…` 能直取。文档链接用稳定名
  `/ccdl/desktop/Claude-mac.zip`、`Claude-windows-x64.msix`、`Claude-windows-arm64.msix`(软链到 `<ver>/` 下原样文件)。
  10-13 镜像的是 **2.31226.1**(mac zip 已验 codesign + Notarized Anthropic PBC),它钉的 Claude Code 是
  **2.1.295** ⇒ 该版本的 `claude-code-releases/2.1.295/` 已同步补上(装新 App 的人预置才不 404)。
  🔴 **换 App 镜像版本 = 两件事一起做**:更新 desktop 软链 **+** 补它钉的 Claude Code 版本(从新 App asar 读)。
  ```bash
  # 198 上;V/H 从 RELEASES.json 读
  curl -s https://downloads.claude.ai/releases/darwin/universal/RELEASES.json   # currentRelease + url 里的 hash
  D=/var/www/ccdl/desktop/$V; sudo mkdir -p $D && cd $D
  for p in darwin/universal win32/x64 win32/arm64; do ext=$([ $p = darwin/universal ] && echo zip || echo msix)
    sudo curl -fsSL -o "$(echo $p|tr / -)-Claude-$H.$ext" https://downloads.claude.ai/releases/$p/$V/Claude-$H.$ext; done
  cd .. && sudo ln -sfn $V/darwin-universal-Claude-$H.zip Claude-mac.zip   # 另两个同理
  ```
- ⛔ 备份 nginx 配置**别放进 `sites-enabled/`**:nginx 会把那份也加载,`nginx -t` 直接报 duplicate。放 `/etc/nginx/backup-ccdl/`。

## 已知限制

- **Windows 没有真机实测**:App 安装目录探测、二进制预置两段是按 App 代码推导的。
- 会盖过配置的旧 shell 变量(`ANTHROPIC_MODEL` 等)会被**注释掉**并备份成 `.bak-carher-*`,不会删。
