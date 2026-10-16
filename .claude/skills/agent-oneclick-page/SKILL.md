---
name: agent-oneclick-page
description: 给同事的「Cursor · Codex · Claude Code 一键配置」单页网页(https://chat.auto-link.com.cn/oneclick/)的改版、构建、上线 SOP——模板 oneclick-all.html、内嵌 10 个文件(安装+自检)的 build_html.py、发版总入口 release_all.sh(网页上线 + 飞书三篇换附件 + 门禁),以及和三篇飞书详细文档的关系。Use when 用户要改/重新发「一键配置」页面、页面上的下载/命令/文案、换了安装器要同步到页面、同事说页面链接点不了/下载不了/下错文件、要把页面挂到别处或「上传到飞书」。
---

# 一键配置单页(给同事的唯一入口)

**线上**:https://chat.auto-link.com.cn/oneclick/ (可加 `#codex` / `#claude` 直达某工具)
**为什么有它**:三篇飞书文档(Cursor / Codex / Claude Code)同事经常下错文件、看错篇;10-15 用户要求合成一页。
三篇飞书文档仍保留作详细版(生成器 `apply.py --all`),但**发给同事的是这个网址**。

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
