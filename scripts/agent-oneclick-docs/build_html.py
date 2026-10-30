"""把 oneclick-all.html 模板里的 {{文件}} 换成内嵌 data URI,生成一个可直接发给同事的单文件页面。

用法:
  python3 scripts/agent-oneclick-docs/build_html.py          # 输出 .work/oneclick-all/AI编程一键配置.html
  python3 scripts/agent-oneclick-docs/build_html.py --check  # 只核对:占位符都有对应文件、无明文 key/内部词

小文件(一键脚本、zip、截图)内嵌进页面,点按钮直接下载;大的 App 安装包走官方/198 直链。
上线 = bash scripts/agent-oneclick-docs/publish_198.sh(→ https://chat.auto-link.com.cn/oneclick/)。
⚠️ 只能用浏览器打开真网址:飞书文件预览禁一切跳转(链接全点不了),claude.ai Artifact 沙箱拦 data: 下载。
改了任一安装器 → 先按各自 skill 重新构建 → 再跑 publish_198.sh。
Mac/Linux 终端命令 = 进「下载」文件夹(Linux 先问 xdg-user-dir)→ 取最新那份 → sh/bash 直接跑;改完跑 test_page_terminal_blocks.py。zsh 不认行尾 # 注释。
"""
import base64, datetime, hashlib, mimetypes, os, re, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TPL = HERE / "oneclick-all.html"
OUT = ROOT / ".work" / "oneclick-all" / "AI编程一键配置.html"

FILES = {
    "cursor-mac.zip": ROOT / "scripts/zk-cursor-web/cursor-mac.zip",
    "cursor-windows.zip": ROOT / "scripts/zk-cursor-web/cursor-windows.zip",
    "codex-mac.command": ROOT / "codex-oneclick/codex-mac.command",
    "codex-windows.bat": ROOT / "codex-oneclick/codex-windows.bat",
    "claude-mac.command": ROOT / "claude-oneclick/claude-mac.command",
    "claude-windows.bat": ROOT / "claude-oneclick/claude-windows.bat",
    "codex-doctor-mac.command": ROOT / "codex-oneclick/codex-doctor-mac.command",
    "codex-doctor-windows.bat": ROOT / "codex-oneclick/codex-doctor-windows.bat",
    "claude-doctor-mac.command": ROOT / "claude-oneclick/claude-doctor-mac.command",
    "claude-doctor-windows.bat": ROOT / "claude-oneclick/claude-doctor-windows.bat",
    "img/codex-login.jpg": HERE / "img/codex-login.jpg",
}
MIME = {".command": "application/octet-stream", ".bat": "application/octet-stream", ".zip": "application/zip"}
# 用户页面里不许出现的东西(同 agent-oneclick-docs-audit.py 的口径)
# 10-29 起对外只用 chat.auto-link.com.cn:旧 cc 域名整个不许出现在页面上(含 /ccdl 下载链接)
BANNED = ["kiro", "//cc.auto-link.com.cn", "claude-router-", "~/Downloads/"]
KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,}")


def data_uri(p: Path) -> str:
    mime = MIME.get(p.suffix) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()


def check(tpl: str) -> list:
    red = []
    want = set(re.findall(r"\{\{([^}]+)\}\}", tpl)) - {"BUILD"}
    if miss := want - set(FILES):
        red.append(f"模板占位符没有对应文件: {sorted(miss)}")
    if unused := set(FILES) - want:
        red.append(f"文件没在模板里用到: {sorted(unused)}")
    for k, p in FILES.items():
        if not p.exists():
            red.append(f"缺文件 {k}: {p}")
    if hits := [w for w in BANNED if w in tpl]:
        red.append(f"页面里有内部词: {hits}")
    # 10-26 起三个桌面 App 的安装包都走 198 镜像(/ccdl/desktop = Claude、/ccdl/apps = Cursor/Codex)。
    #   官方直链会过期(Cursor 写死 3.24.12 那次)或在国内卡住(Codex 商店在线安装器),不许再回来。
    if hits := re.findall(r'href="(https://(?:downloads\.cursor\.com|persistent\.oaistatic\.com|get\.microsoft\.com)[^"]*)"', tpl):
        red.append(f"App 下载又指向官方直链(应走 chat.auto-link.com.cn/ccdl/apps): {hits[:3]}")
    # 镜像链接必须真能下(HEAD 200 且有字节数);离线构建设 CX_SKIP_MIRROR_HEAD=1 跳过,但发版脚本不设
    if not os.environ.get("CX_SKIP_MIRROR_HEAD"):
        for u in sorted(set(re.findall(r'href="(https://chat\.auto-link\.com\.cn/ccdl/(?:apps|desktop)/[^"]+)"', tpl))):
            try:
                r = urllib.request.urlopen(urllib.request.Request(u, method="HEAD"), timeout=30)
                n = int(r.headers.get("Content-Length") or 0)
                if r.status != 200 or n < 10_000_000:
                    red.append(f"镜像链接下不了或太小: {u} (HTTP {r.status}, {n} 字节)")
            except Exception as e:
                red.append(f"镜像链接下不了: {u} ({e})")
    # 10-19 起三个工具的模型菜单都按 key 动态拉取,页面里不许再列写死的模型清单
    if "全部 39 个模型" in tpl or re.search(r"全部 \d+ 个模型", tpl):
        red.append("页面里又写死了模型清单(菜单已按 key 动态拉取,发「刷模型」更新)")
    if "刷模型" not in tpl:
        red.append("页面里没有「刷模型」提示")
    # 10-28:「Cursor 不会自己升级」**不加限定**是不实的 —— 升级锁 10-13 才默认开,
    #   更早装的人恰恰被自动升级冲掉了补丁、开始每条消息都压缩(同事说「比 Claude 慢」的根因)。
    #   必须带上时间限定或「重跑 UPGRADE 就会锁住」,否则拦下。
    #   🔴 豁免只看**同一句**(到下一个句号为止)里有没有限定。第一版看「后 120 字有没有 UPGRADE」,
    #      结果原句后面紧跟的「双击 UPGRADE」把它豁免了 —— 塞回旧句子门禁照样绿,阳性对照才抓出来。
    for m in re.finditer(r"Cursor[^。<>]{0,6}不会(?:自己|自动)升级", tpl):
        a = max(tpl.rfind("。", 0, m.start()), tpl.rfind(">", 0, m.start())) + 1
        e = tpl.find("。", m.end()); e = len(tpl) if e < 0 else e
        sent = tpl[a:e]
        if not re.search(r"10 月 13 日|13 日之前|13 日之后", sent):
            red.append(f"又写了不加限定的「Cursor 不会自动升级」(对 10-13 前装的人不成立): {sent.strip()[:70]}")
    # 排障表必须有「慢」这一行 —— 那是同事真实的说法,也是最常见的那个病
    if "每发一句都要等很久" not in tpl:
        red.append("Cursor 排障表里没有「每发一句都要等很久」那一行(最常见的那个病)")
    # 10-26 起终端命令:自动找「下载」文件夹里最新那份,用 sh/bash 直接跑(不写死文件名、不 chmod、不 cd ~/Downloads)。
    #   旧写法三种坏形状(旧文件夹里的旧脚本被执行 / 第二次下载成 (1) 找不到 / 中文 Linux 叫 ~/下载)全由
    #   test_page_terminal_blocks.py 在假下载文件夹里真跑来抓;这里只拦「又写回旧模板」。
    for blk in re.findall(r"<pre[^>]*><code>(.*?)</code></pre>", tpl, re.S):
        if "cd ~/Downloads" in blk or "chmod +x" in blk or re.search(r"bash \./[\w-]+\.command", blk) or "[ -d cursor-mac ] ||" in blk:
            red.append(f"终端命令又写回了写死文件名的旧模板: {blk.splitlines()[-1][:80]}")
    if KEY_RE.search(tpl) or any(KEY_RE.search(p.read_bytes().decode("latin-1")) for p in FILES.values() if p.exists()):
        red.append("页面或内嵌文件里有明文 key")
    return red


def main():
    tpl = TPL.read_text()
    red = check(tpl)
    for r in red:
        print("❌", r)
    if red:
        sys.exit(1)
    if "--check" in sys.argv:
        print("✅ 模板与文件一致")
        return
    h = hashlib.sha256(b"".join(FILES[k].read_bytes() for k in sorted(FILES))).hexdigest()[:8]
    build = f"{datetime.date.today():%Y-%m-%d} · {h}"
    html = tpl.replace("{{BUILD}}", build)
    for k, p in FILES.items():
        html = html.replace("{{" + k + "}}", data_uri(p))
    # 独立打开时也要有 doctype / charset / hidden 规则(Artifact 发布会自动补,这里自己补)
    html = ('<!doctype html>\n<html lang="zh-CN"><meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
            '<style>[hidden]{display:none!important}body{margin:0}</style>\n') + html + "\n</html>\n"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html)
    print(f"✅ {OUT}  {OUT.stat().st_size / 1024:.0f} KB  版本 {build}")
    for k, p in FILES.items():
        print(f"   {k:<22} {p.stat().st_size / 1024:>6.0f} KB  sha {hashlib.sha256(p.read_bytes()).hexdigest()[:12]}")


if __name__ == "__main__":
    main()
