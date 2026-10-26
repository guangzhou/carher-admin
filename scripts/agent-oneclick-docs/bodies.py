"""第四版:三份文档(Cursor / Codex / Claude Code)同一骨架、同一措辞。⟦X⟧ = 原有图片/附件放回的位置。

统一骨架:
  🚀 一句话
  1. 准备        拿 key → 装客户端/App →(可选)命令行手动装法
  2. 一键配置    Mac/Linux 下载双击 · Windows 下载双击 · 粘 key · 打不开怎么办
  3. 开始用      命令行 · 桌面 App · 模型(Cursor:本机 · 远程服务器 · 模型)
  4. 出问题了    「先 X」一句话 + 现象|怎么办 表 + 「还不行」一句话
  5. 更新与卸载
  (Codex 另有 6. 其他工具)
  互链在 🚀 里:「本文只配 X。用别的看:…」(防下错文件)
"""

# ── 共用小积木 ──────────────────────────────────────────────
KEYLINK = ('<a href="https://t83dfrspj4.feishu.cn/share/base/query/shrcnf5OElDguABGM3ZZDVoO3cb"><b><u>点击查看</u></b></a>'
           '<b>cursor 对应的 key</b>（没有就<a href="https://t83dfrspj4.feishu.cn/share/base/shrcnfnACoTPTWiZv6BD1Lz6Ufe"><u>点此申请</u></a>）')
KEY_LI = f'<b>拿 key</b>：{KEYLINK}。Cursor、Codex、Claude Code 用同一把。'
QUIT = '完全退出（Mac 按 <code>Cmd + Q</code>；Windows 右键任务栏图标 → 退出）'
WIN_WARN = '<p>Windows 弹出蓝色安全提示：点「更多信息」→「仍要运行」。</p>'
APPS = "https://cc.auto-link.com.cn/ccdl/apps"   # 10-26 起 Cursor/Codex 安装包也走 198 镜像(app-mirror-sync.sh 每天跟版)
CCDL = "https://cc.auto-link.com.cn/ccdl/desktop"


def pre(*lines):
    return '<pre lang="Bash"><code>' + "<br/>".join(lines) + "</code></pre>"


def ph(k):
    return f"<p>⟦{k}⟧</p>"


def codes(*names):
    return "、".join(f"<code>{n}</code>" for n in names)


def ol(*items):
    return "<ol>" + "".join(
        f'<li{" seq=\"1\"" if i == 0 else ""} seq-marker="{i + 1}. ">{x}</li>' for i, x in enumerate(items)) + "</ol>"


def table(rows, head=True):
    out = "<table>"
    for i, r in enumerate(rows):
        out += "<tr>" + "".join(f"<td>{'<b>' + c + '</b>' if head and i == 0 else c}</td>" for c in r) + "</tr>"
    return out + "</table>"


OPEN_FAIL = "双击打不开：Mac 右键文件 → 打开 → 再点「打开」；Windows 点「更多信息」→「仍要运行」；Linux 在终端里输入 <code>bash </code>（后面有个空格），把文件拖进终端窗口，回车。"
STILL_BAD = "还不行：把安装窗口的整屏文字发给运维（不含 key）。"


L_CURSOR = '<a href="https://t83dfrspj4.feishu.cn/docx/OQCPdLd4MovEVoxGzdMcD3CJnCf">Cursor 一键配置</a>'
L_CODEX = '<a href="https://t83dfrspj4.feishu.cn/wiki/BzVZwGMSOiSYOykc6UYcN2opn3b">Codex 一键配置</a>'
L_CLAUDE = '<a href="https://t83dfrspj4.feishu.cn/docx/MeLad7oMiobwf6xnpA2cWy0nnSg">Claude Code 一键配置</a>'


def callout(txt):
    return ('<callout emoji="🚀" background-color="rgb(240,244,255)" border-color="rgb(130,167,252)"><p>'
            + txt + "</p></callout>")


def p(txt):
    return "<p>" + txt + "</p>"


# ───────────────────────── Cursor ─────────────────────────
CURSOR = "".join([
    callout("<b>下载一个包 → 解压 → 双击 → 粘一次 key。</b>本机和远程服务器（Remote-SSH）都能用。"
            "<br/>本文只配 Cursor。用别的看：" + L_CODEX + " · " + L_CLAUDE),
    "<h1>1. 准备</h1>",
    p("<b>① 拿 key</b>：" + KEYLINK + "。Cursor、Codex、Claude Code 用同一把。"),
    p("<b>② 装 Cursor</b>（装过的跳过），按你的电脑选一个："),
    table([["电脑", "下载", "装法"],
           ["Mac", f'<a href="{APPS}/Cursor-mac.dmg"><b><u>点这里下载</u></b></a>', "打开 .dmg，把 Cursor 拖进「应用程序」"],
           ["Windows", f'<a href="{APPS}/Cursor-windows-x64.exe"><b><u>点这里下载</u></b></a>（ARM 电脑用<a href="{APPS}/Cursor-windows-arm64.exe">这个</a>）', "双击 .exe 安装"],
           ["Linux", f'<a href="{APPS}/Cursor-linux-amd64.deb"><b><u>.deb</u></b></a>(Ubuntu/Debian)· <a href="{APPS}/Cursor-linux-x86_64.rpm"><b><u>.rpm</u></b></a>(Fedora/RHEL);ARM 用 <a href="{APPS}/Cursor-linux-arm64.deb">.deb</a> / <a href="{APPS}/Cursor-linux-aarch64.rpm">.rpm</a>', "<code>sudo apt install ./文件名</code> 或 <code>sudo dnf install ./文件名</code>;一键配置用 Mac 那个包"],
           ["其他", '<a href="https://cursor.com/download"><u>官网下载页</u></a>', "—"]]),
    p("<b>③ 注册并登录 Cursor 账号</b>（已登录的跳过）：打开 Cursor，点 <b>Sign Up</b> 用邮箱注册（已有账号点 <b>Log In</b>），"
      "在浏览器里完成后回到 Cursor。<b>免费账号就够</b>，不用买会员。不登录用不了我们的模型。"),
    "<h1>2. 一键配置</h1>",
    p("按你的电脑下载一个包，解压。<b>Mac / Linux</b>（解压得到 <code>cursor-mac</code> 文件夹）："),
    ph("ZIP"),
    p("<b>Windows</b>（解压得到 <code>cursor-windows</code> 文件夹）："),
    ph("ZIPW"),
    ol(f"把 Cursor {QUIT}。",
       "打开解压出来的文件夹，<b>Mac</b> 双击 <code>INSTALL-Mac.command</code>；<b>Windows</b> 双击 <code>INSTALL-Windows.cmd</code>；"
       "<b>Linux</b> 在这个文件夹里打开终端，运行 <code>sh ./INSTALL-Mac.command</code>（Cursor 要先打开过一次并登录）。",
       "出现提示时粘贴 key，回车，看到「完成」。"),
    p("双击打不开：Mac 右键文件 → 打开 → 再点「打开」；Windows 点「更多信息」→「仍要运行」。"),
    ph("IMG0"),
    p("如果最后提示「还差一步」：打开 Cursor → <b>Settings → Models</b> → <b>OpenAI API Key</b>，粘贴 key，点 <b>Verify</b>，再完全退出重开。"
      "（key 去 key 表复制整格，别手敲。）"),
    "<h1>3. 开始用</h1>",
    p("<b>本机</b>：打开 Cursor，默认模型 <code>grok-4.7</code>，直接用。装好后 Cursor <b>不会自动升级</b>（自动升级会让配置失效）。"),
    p("<b>连远程服务器（Remote-SSH）</b>，每台服务器做一次："),
    ol("用 Cursor 连一次这台服务器（左下角 → Connect to Host），等显示已连接。",
       "双击 <code>REMOTE-SSH-Mac.command</code>（Windows：<code>REMOTE-SSH-Windows.cmd</code>），输入服务器名（照列出来的填，或 <code>用户名@IP</code>），回车；再输入 <code>yes</code>，回车。",
       "看到「✅ 已补好」，关掉远程窗口重连，就能用了。"),
    p("⚠️ 更新 Cursor 版本后，先连一次服务器，再双击一次 <code>REMOTE-SSH</code>。"),
    p("<b>模型</b>：菜单就是你的 key 能用的模型，拿不定主意就用默认的 <code>grok-4.7</code>；key 开通了新模型，在 Cursor 里发「刷模型」。"),
    p("<b>画图</b>：直接说「画一张……」，图片存在当前项目文件夹。"),
    table([["分类", "模型"],
           ["Grok / Gemini（快）",
            codes("grok-4.7", "grok-4.6", "grok-4.6-latest", "grok-4.5-latest", "grok-4.5", "grok-4.3", "grok-build-0.1",
                  "grok-4.20", "grok-4.20-0309-reasoning", "gemini-3.8-flash", "composer-2.5-fast", "composer-2.5")],
           ["GPT（稳）",
            codes("gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra",
                  "gpt-5.5", "gpt-5.4", "gpt-5.2", "codex-auto-review")],
           ["Claude",
            codes("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5", "claude-opus-5", "claude-sonnet-5",
                  "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")],
           ["国产 / 其他", codes("deepseek-v4-flash", "kimi-k2.7-code", "glm-5.3-flash", "qwen3-coder-next", "jev")],
           ["muse", "<code>muse-spark</code>、<code>muse-image</code>（画图）"]]),
    ph("IMG1"),
    "<h1>4. 出问题了</h1>",
    p("<b>先对照下表自己试</b>；不行就双击 <code>DOCTOR</code>，把窗口里的整屏文字发给运维（不含 key；别只发截图）。"),
    table([["现象", "怎么办"],
           ["报 <code>401</code>，或 key 没写上", "key 错了或没写上：Settings → Models → OpenAI API Key，粘贴 key（去 key 表复制整格，别手敲）→ 点 Verify → 完全退出重开"],
           ["报 <code>key not allowed to access model</code>", "你的 key 没开通这个模型：换一个，或找运维开通（muse 是新加的，很多 key 还没开）"],
           ["报 <code>An unexpected error occurred</code> / <code>does not support custom API keys</code>；看不到 High / Fast；菜单里有第 3 节模型表以外的模型",
            "下载最新的包，退出 Cursor，双击 <code>UPGRADE</code>"],
           ["模型不见了", f"Cursor {QUIT} → 双击 <code>REPAIR</code> → 重开"],
           ["REPAIR 提示「拒绝动手」", "Cursor 出了还没适配的新版本，没有改动你的 Cursor，把窗口文字发给运维"],
           ["模型一直转圈 / 报 <code>502</code>", "换回 <code>grok-4.7</code>，把模型名和时间发给运维"],
           ["远程窗口模型报错 / 每轮都在压缩上下文", "按第 3 节再双击一次 <code>REMOTE-SSH</code>"],
           ["REMOTE-SSH 提示「8788 端口被别的程序占着」",
            "请服务器管理员让出这个端口；或 Cursor 完全退出后双击 <code>TURN-OFF-DELTA-Mac.command</code>，再连服务器"]]),
    p("提示没权限： 慎！！！"),
    ph("IMG2"),
    "<h1>5. 更新与卸载</h1>",
    p("<b>更新</b>：第 2 节的包有新版时，重新下载你那个系统的包、解压 → 把 Cursor 完全退出 → 双击 <code>UPGRADE-Mac.command</code>"
      "（Windows：<code>UPGRADE-Windows.cmd</code>）。不用再输 key，聊天记录和设置不动；连远程服务器的，再双击一次 <code>REMOTE-SSH</code>。"),
    p("更新会把模型菜单换成你的 key 能用的模型，你自己加的模型名会被清掉。"),
    p(f"<b>卸载</b>：把 Cursor {QUIT} → 双击 <code>UNINSTALL-Mac.command</code>（Windows：<code>UNINSTALL-Windows.cmd</code>）→ 输入 <code>yes</code>。聊天记录不动。"),
])


# ───────────────────────── Codex ─────────────────────────
def codex(external):
    return "".join([
        callout("<b>下载一个文件 → 双击 → 粘一次 key。</b>命令行 <code>codex</code> 和桌面 App 一起配好，Mac、Windows、Linux 都行。"
                '<b><span background-color="rgb(247,105,100)">不要开 VPN</span></b>（也别用 TC）。'
                + ("" if external else "<br/>本文只配 Codex。用别的看：" + L_CURSOR + " · " + L_CLAUDE)),
        "<h1>1. 准备</h1>",
        p("<b>① 拿 key</b>：" + KEYLINK + "。Cursor、Codex、Claude Code 用同一把。"),
        p("<b>② 选客户端</b>：命令行和桌面 App 用哪个都行，也可以都用，一键脚本会把两个一起配好。"),
        table([["客户端", "怎么装"],
               ["命令行 <code>codex</code>", "不用装，一键脚本发现没有会自动装"],
               ["桌面 App", f"装下面的安装包（装过的跳过）。装好先别打开，已经开着的{QUIT}"]]),
        p(f'Mac：<a href="{APPS}/Codex-mac.dmg"><b><u>点这里下载官方安装包</u></b></a>，双击 .dmg，把 Codex 拖进「应用程序」。'),
        p(f'Windows：<a href="{APPS}/Codex-windows-x64.msix"><b><u>点这里下载官方安装包</u></b></a>（ARM 电脑用<a href="{APPS}/Codex-windows-arm64.msix">这个</a>），双击 .msix 按提示安装。装不上找<cite type="user" user-id="ou_36efe139cd762579ae266081764b59fc" user-name="宋力"></cite>开权限，'
          '或看 <cite doc-id="La8twWEToibR9Jk6yZucTz78nDc" file-type="wiki" title="Codex 配置" type="doc"></cite>。'),
        p(f'Linux：<a href="{APPS}/Codex-linux-amd64.deb"><b><u>Ubuntu/Debian x64</u></b></a> · <a href="{APPS}/Codex-linux-arm64.deb">arm64</a>'
          f'　<a href="{APPS}/Codex-linux-x86_64.rpm"><b><u>Fedora/RHEL x64</u></b></a> · <a href="{APPS}/Codex-linux-aarch64.rpm">arm64</a>，'
          '用 <code>sudo apt install ./文件名</code> 或 <code>sudo dnf install ./文件名</code> 安装。'),
        "<h1>2. 一键配置</h1>",
        p("按你的电脑下载一个，双击。<b>Mac / Linux</b>（文件名带 <code>-mac</code>）："),
        ph("MAC"),
        p("<b>Windows</b>（文件名带 <code>-windows</code>）："),
        ph("WIN"),
        p("出现 <code>key:</code> 时粘贴 key，回车；看到「全部搞定！」就好了（第一次约 2~5 分钟）。"),
        p(OPEN_FAIL),
        "<h1>3. 开始用</h1>",
        p("<b>命令行</b>：新开终端（Windows 开 PowerShell），进项目文件夹："),
        pre("cd 你的项目文件夹", "codex"),
        p("<b>桌面 App</b>：打开 App（装脚本前就开着的，要先完全退出再开，只关窗口不行）。出现登录页就点「使用其他方式登录」，再粘贴 key："),
        ph("IMG0"),
        ph("IMG1"),
        p("<b>模型</b>：默认 <code>gpt-5.6-terra</code>（你的 key 没开它就自动用你能用的第一个）。换模型：命令行输入 <code>/model</code>；App 点底部的模型选择器。列表就是你的 key 能用的全部模型，每天自动更新。"),
        p("<b>画图</b>：直接说「画一张……」，图片存在当前文件夹。别同时开多个 Codex，可能互相踢下线。"),
        "<h1>4. 出问题了</h1>",
        p("<b>先重新双击一次脚本</b>，再新开终端 / 退出重开 App，大多数问题都能好。"),
        table([["现象", "怎么办"],
               ["报 <code>401</code>", "key 错了：去 key 表复制整格重新粘，别手敲"],
               ["报 <code>key not allowed to access model</code>", "key 没开通这个模型：换一个或找运维开通"],
               ["终端找不到 <code>codex</code>", "关掉终端新开一个"],
               ["App 没生效，或报错地址是 <code>api.openai.com</code>", "把 App 完全退出再打开"],
               ["模型一直转圈 / 报 <code>502</code>", "换回 <code>gpt-5.6-terra</code>，把模型名和时间发给运维"],
               ["选择器里只有几个 GPT，找不到 key 里的模型", "下载最新的脚本重新双击一次"],
               ["每一轮都报 <code>403</code>", "默认模型你的 key 没开：下载最新的脚本重新双击一次（会自动换成你能用的），或 <code>/model</code> 手动选"],
               ["不知道哪里坏了", "下载 <code>codex-doctor</code> 自检文件跑一次，把报告发给运维"]]),
        p(STILL_BAD),
        "<h1>5. 更新与卸载</h1>",
        p("<b>更新 / 换 key / 换电脑</b>：重新下载最新脚本双击一次就行，原配置会自动备份。"),
        p("<b>还原</b>：脚本改配置前会把原文件备份到 <code>~/.codex/</code> 里 <code>backup-carher-</code> 开头的文件夹，想还原就把里面的文件拷回 <code>~/.codex/</code>。"),
        "<h1>6. 其他工具</h1>",
        "<ul><li>飞书和 codex 打通：对 codex 说「安装 lark cli」。</li>"
        "<li>高效使用 codex：https://zhuanlan.zhihu.com/p/2067598557742469475</li>"
        '<li>AI 写飞书文档：her（找<cite type="user" user-id="ou_b8e5bb0cc5c19b5059266d7409eabeb4" user-name="符金冬"></cite>申请），'
        '或 <a href="https://chat.auto-link.com.cn/">openchat</a>（免申请，快，支持飞书文档读写、群消息总结、PPT、画图）。</li></ul>',
    ])


# ───────────────────────── Claude Code ─────────────────────────
CLAUDE = "".join([
    callout("<b>下载一个文件 → 双击 → 粘一次 key。</b>命令行 <code>claude</code> 和桌面 App 一起配好，Mac、Windows、Linux 都行。"
            "<br/>本文只配 Claude Code。用别的看：" + L_CURSOR + " · " + L_CODEX),
    "<h1>1. 准备</h1>",
    p("<b>① 拿 key</b>：" + KEYLINK + "。Cursor、Codex、Claude Code 用同一把。"),
    p("<b>② 选客户端</b>：命令行和桌面 App 用哪个都行，也可以都用，一键脚本会把两个一起配好。"),
    table([["客户端", "怎么装"],
           ["命令行 <code>claude</code>", "不用装，一键脚本发现没有会自动装"],
           ["桌面 App",
            f'<a href="{CCDL}/Claude-mac.zip"><b><u>Mac 下载</u></b></a>（解压后拖进「应用程序」）　'
            f'<a href="{CCDL}/Claude-windows-x64.msix"><b><u>Windows 下载</u></b></a>（ARM 电脑用<a href="{CCDL}/Claude-windows-arm64.msix">这个</a>）。'
            f'<a href="{APPS}/Claude-linux-amd64.deb"><b><u>Linux 下载</u></b></a>（Ubuntu/Debian,arm64 用<a href="{APPS}/Claude-linux-arm64.deb">这个</a>，<code>sudo apt install ./文件名</code>）。'
            f"装好<b>不用登录</b>，先别打开，已经开着的{QUIT}"]]),
    "<h1>2. 一键配置</h1>",
    p("按你的电脑下载一个，双击。<b>Mac / Linux</b>（文件名带 <code>-mac</code>）："),
    ph("MAC"),
    p("<b>Windows</b>（文件名带 <code>-windows</code>）："),
    ph("WIN"),
    p("出现 <code>key:</code> 时粘贴 key，回车；看到「通了」和「完成！」就好了（第一次约 2~5 分钟）。"),
    p(OPEN_FAIL),
    "<h1>3. 开始用</h1>",
    p("<b>命令行</b>：新开终端（Windows 开 PowerShell），进项目文件夹："),
    pre("cd 你的项目文件夹", "claude"),
    p("<b>桌面 App</b>：打开 App（不用登录，装脚本前就开着的要先完全退出再开），左边点 <b>Code</b>，选一个项目文件夹，直接说你要做什么。"),
    p("<b>模型</b>：默认 <code>claude-opus-5-5</code>。换模型：命令行输入 <code>/model</code>；App 点输入框旁边的模型名。列表就是你的 key 能用来聊天的全部模型，每个只出现一次；key 开通了新模型，下次打开 Claude Code 会自动出现（想马上看到就发「刷模型」）。"),
    p("<b>画图</b>：直接说「画一张……」，图片存在当前文件夹（命令行和桌面 App 都行）。"),
    "<h1>4. 出问题了</h1>",
    p("<b>先把 App 和终端都关掉 → 重新双击一次安装脚本 → 再打开</b>，大多数问题都能好。"),
    table([["现象", "怎么办"],
           ["报 <code>401</code>", "key 错了：去 key 表复制整格，重新双击脚本粘一次，别手敲"],
           ["报 <code>403</code> 或 <code>key not allowed to access model</code>", "key 没开通这个模型：换一个或找运维开通"],
           ["App 提示「App unavailable」，或发消息很慢、一直不回（常见于 App 自动升级后）",
            "把 App 完全退出 → 重新双击一次安装脚本 → 再打开；还不行找运维（新版 App 要的组件还没准备好）"],
           ["App 里只能选一个模型", "把 App 完全退出再打开"],
           ["<code>/model</code> 里只有几个 Claude", "关掉终端新开一个"],
           ["模型一直转圈 / 报 <code>502</code>", "换回 <code>claude-opus-5-5</code>，把模型名和时间发给运维"]]),
    p(STILL_BAD),
    "<h1>5. 更新与卸载</h1>",
    p("<b>更新 / 换 key</b>：重新下载最新脚本双击一次就行，原设置会自动备份。"),
    p("<b>卸载</b>：命令行删掉 <code>~/.claude/settings.json</code> 里 <code>env</code> 那几行；桌面 App 删掉下面的文件夹，就回到官方默认："),
    p("Mac："),
    pre("~/Library/Application Support/Claude-3p"),
    p("Windows："),
    pre("%LOCALAPPDATA%\\Claude-3p"),
    # 10-15 用户手工加的效果图,放回文末
    p("Claude 效果图："),
    ph("IMG0"),
])
