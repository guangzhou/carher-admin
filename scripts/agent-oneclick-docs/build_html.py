"""把 oneclick-all.html 模板里的 {{文件}} 换成内嵌 data URI,生成一个可直接发给同事的单文件页面。

用法:
  python3 scripts/agent-oneclick-docs/build_html.py          # 输出 .work/oneclick-all/AI编程一键配置.html
  python3 scripts/agent-oneclick-docs/build_html.py --check  # 只核对:占位符都有对应文件、无明文 key/内部词

小文件(一键脚本、zip、截图)内嵌进页面,点按钮直接下载;大的 App 安装包走官方/198 直链。
上线 = bash scripts/agent-oneclick-docs/publish_198.sh(→ https://chat.auto-link.com.cn/oneclick/)。
⚠️ 只能用浏览器打开真网址:飞书文件预览禁一切跳转(链接全点不了),claude.ai Artifact 沙箱拦 data: 下载。
改了任一安装器 → 先按各自 skill 重新构建 → 再跑 publish_198.sh。
Mac 运行一律写 `cd ~/Downloads` + `chmod +x` + `bash ./x`:浏览器下载会丢执行位,zsh 不认行尾 # 注释。
"""
import base64, datetime, hashlib, mimetypes, re, sys
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
BANNED = ["kiro", "cc.auto-link.com.cn/pro", "claude-router-", "~/Downloads/"]
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
    # 10-19 起三个工具的模型菜单都按 key 动态拉取,页面里不许再列写死的模型清单
    if "全部 39 个模型" in tpl or re.search(r"全部 \d+ 个模型", tpl):
        red.append("页面里又写死了模型清单(菜单已按 key 动态拉取,发「刷模型」更新)")
    if "刷模型" not in tpl:
        red.append("页面里没有「刷模型」提示")
    # Mac 终端命令必须先 chmod 再 bash ./
    for blk in re.findall(r"<pre[^>]*><code>(.*?)</code></pre>", tpl, re.S):
        if "bash ./" in blk and "chmod +x" not in blk:
            red.append(f"代码块缺 chmod +x: {blk.splitlines()[-1]}")
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
