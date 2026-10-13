#!/usr/bin/env python3
"""三个 agent 一键配置的飞书文档收口门禁(只读)。

改完 Cursor / Codex / Claude Code 任何一篇文档或附件后跑一次,全绿才算发出去:

  python3 scripts/agent-oneclick-docs-audit.py            # 真跑(要 lark-cli 已登录)
  python3 scripts/agent-oneclick-docs-audit.py --selftest # 阳性对照:每条判据各喂一个坏样本必须红

判据(每条都对应 10-12 / 10-13 实际踩过的坑):
  ① 附件 = 本地构建产物:下载回来逐个比 sha256(只传不回环 = 不知道传没传对)
  ② 两篇同名 Cursor 文档**正文逐字相同**(只改一篇 = 新旧调位且不报错;按条数对账是假绿)
  ③ 陈旧词残留 0(已下架的名字 / 不存在的短名 / 旧版前提 / 手动配置步骤)
  ④ 每篇的附件/图片块数不变(块操作删错、整篇 overwrite 弄丢画板都会体现在这里)
  ⑤ 正文里不许出现明文 key
只读:不改文档。下载件落在 .work/doc-audit/(仓库内,不进 git)。
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".work" / "doc-audit"

CURSOR_A, CURSOR_B = "OQCPdLd4MovEVoxGzdMcD3CJnCf", "FapXdudPLoTTRLxWGJQcqITpn9g"
ZIP = ROOT / "scripts/zk-cursor-web/cursor-g-setup.zip"

# 陈旧词:全文出现即红。「已下架」说明里允许提到的旧名放 ALLOW_IN 里按句豁免。
COMMON_STALE = ["cr-g-5.6", "sa-grok-4.6-latest", "3.20.21", "3.21.18"]
DOCS = {
    CURSOR_A: dict(name="Cursor-A", figures=1, imgs=3,
                   attach={"cursor-g-setup.zip": ZIP},
                   stale=COMMON_STALE + ["whiteboard"]),
    CURSOR_B: dict(name="Cursor-B", figures=1, imgs=3,
                   attach={"cursor-g-setup.zip": ZIP},
                   stale=COMMON_STALE + ["whiteboard"]),
    "Ni2hdvtbgoqBFMxrAu5ckjPrnbg": dict(
        name="Codex", figures=4, imgs=2,
        attach={"install-mac.command": ROOT / "codex-oneclick/install-mac.command",
                "install-windows.bat": ROOT / "codex-oneclick/install-windows.bat"},
        # 10-13 起脚本自动装 codex 命令行 ⇒「先装好 codex」的前提是旧真相
        stale=COMMON_STALE + ["已经装好 codex", "第一步：下载脚本"]),
    "MeLad7oMiobwf6xnpA2cWy0nnSg": dict(
        name="ClaudeCode", figures=2, imgs=0,
        attach={"install-mac.command": ROOT / "claude-oneclick/install-mac.command",
                "install-windows.bat": ROOT / "claude-oneclick/install-windows.bat"},
        # 一键之前的手动步骤;AUTH_TOKEN 能聊天但拉不到 /model 列表
        stale=COMMON_STALE + ["ANTHROPIC_AUTH_TOKEN", "GATEWAY_MODEL_DISCOVERY", "settings.json 里用的是"]),
}
# 「已下架」那句提示里点名旧名是对的,不算残留
ALLOW_IN = ("已下架", "已整段下架")
KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{16,}")


# ── 纯函数:判据本体(selftest 直接喂它们) ─────────────────────────────
def body_text(xml: str) -> str:
    """正文归一成逐行文本:去标题块、图片 alt(AI 生成描述,两篇不同属正常)、所有标签。"""
    x = re.sub(r"<title.*?</title>", "", xml, flags=re.S)
    x = re.sub(r'alt="[^"]*"', "", x)
    x = re.sub(r"</(p|li|h\d|tr|td|pre|callout)>", "\n", x)
    x = re.sub(r"<[^>]+>", "", x)
    return "\n".join(l.strip() for l in x.split("\n") if l.strip())


def stale_hits(xml: str, words):
    hits = []
    for line in body_text(xml).split("\n"):
        if any(a in line for a in ALLOW_IN):
            continue
        hits += [w for w in words if w in line]
    # whiteboard 是块标签不是正文,单独查原始 xml
    if "whiteboard" in words and "<whiteboard" in xml:
        hits.append("whiteboard")
    return sorted(set(hits))


def sources(xml: str):
    out = []
    for m in re.finditer(r"<source[^>]*>", xml):
        s = m.group(0)
        n, t = re.search(r'name="([^"]+)"', s), re.search(r'token="([^"]+)"', s)
        if n and t:
            out.append((n.group(1), t.group(1)))
    return out


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check_doc(xml, cfg, fetch_file):
    """返回红项列表。fetch_file(token, dest) 负责把附件拉到 dest。"""
    red = []
    if (n := xml.count("<figure")) != cfg["figures"]:
        red.append(f"附件块 {n} 个,应为 {cfg['figures']}")
    if (n := xml.count("<img")) != cfg["imgs"]:
        red.append(f"图片 {n} 张,应为 {cfg['imgs']}")
    if h := stale_hits(xml, cfg["stale"]):
        red.append(f"陈旧词残留:{h}")
    if KEY_RE.search(body_text(xml)):
        red.append("正文里有明文 key")
    srcs = sources(xml)
    for name, local in cfg["attach"].items():
        toks = [t for n, t in srcs if n == name]
        if len(toks) != 1:
            red.append(f"附件 {name} 有 {len(toks)} 个,应为 1")
            continue
        dest = OUT / f"{cfg['name']}_{name}"
        fetch_file(toks[0], dest)
        if not dest.exists():
            red.append(f"附件 {name} 下载失败")
        elif sha256(dest) != sha256(local):
            red.append(f"附件 {name} 与本地构建不同(文档 {sha256(dest)[:12]} ≠ 本地 {sha256(local)[:12]})")
    return red


def twin_diff(xml_a, xml_b):
    a, b = body_text(xml_a).split("\n"), body_text(xml_b).split("\n")
    if a == b:
        return []
    only_a = [l for l in a if l not in b][:3]
    only_b = [l for l in b if l not in a][:3]
    return [f"两篇 Cursor 文档正文不同:仅 A 有 {only_a} / 仅 B 有 {only_b}"]


# ── 真跑 ─────────────────────────────────────────────────────────────
def lark(*args):
    r = subprocess.run(["lark-cli", *args], capture_output=True, text=True)
    return r.stdout


def fetch_xml(doc):
    d = json.loads(lark("docs", "+fetch", "--doc", doc, "--detail", "with-ids"))
    return d["data"]["document"]["content"]


def fetch_file(token, dest):
    dest.unlink(missing_ok=True)
    lark("docs", "+media-download", "--token", token, "--output", str(dest), "--overwrite")


def run():
    OUT.mkdir(parents=True, exist_ok=True)
    xmls, bad = {}, 0
    for doc, cfg in DOCS.items():
        xmls[doc] = xml = fetch_xml(doc)
        heads = [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h1[^>]*>(.*?)</h1>", xml)]
        red = check_doc(xml, cfg, fetch_file)
        bad += bool(red)
        print(f"{'❌' if red else '✅'} {cfg['name']:<10} 章节 {heads}")
        for r in red:
            print(f"     · {r}")
    red = twin_diff(xmls[CURSOR_A], xmls[CURSOR_B])
    bad += bool(red)
    print(f"{'❌' if red else '✅'} 两篇 Cursor 文档正文逐字相同")
    for r in red:
        print(f"     · {r}")
    print("\nVERDICT:", "FAIL" if bad else "PASS")
    return 1 if bad else 0


def selftest():
    """阳性对照:每条判据喂一个坏样本必须红,好样本必须绿。尺子只会报绿 = 没有尺子。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(dir=ROOT / ".work" if (ROOT / ".work").exists() else None))
    good_file, bad_file = tmp / "good", tmp / "bad"
    good_file.write_bytes(b"v2"), bad_file.write_bytes(b"v1")
    cfg = dict(name="T", figures=1, imgs=0, attach={"x.zip": good_file}, stale=["cr-g-5.6", "whiteboard"])
    ok_xml = '<p>装好了</p><p>已下架:cr-g-5.6</p><figure><source name="x.zip" token="T1"/></figure>'
    up = lambda content: (lambda t, d: d.write_bytes(content))
    global OUT
    OUT = tmp
    cases = [
        ("好样本全绿", ok_xml, up(b"v2"), False),
        ("附件内容旧了", ok_xml, up(b"v1"), True),
        ("陈旧名出现在正文", ok_xml.replace("装好了", "选 cr-g-5.6"), up(b"v2"), True),
        ("多出一个附件块", ok_xml + '<figure><source name="y" token="T2"/></figure>', up(b"v2"), True),
        ("留着画板", ok_xml + "<whiteboard/>", up(b"v2"), True),
        ("正文有明文 key", ok_xml.replace("装好了", "sk-ABCDEFGHIJKLMNOPQRS"), up(b"v2"), True),
    ]
    fails = 0
    for label, xml, fetch, want_red in cases:
        got_red = bool(check_doc(xml, cfg, fetch))
        fine = got_red == want_red
        fails += not fine
        print(f"  {'PASS' if fine else 'FAIL'}  {label}(期望{'红' if want_red else '绿'})")
    for label, b, want_red in [("两篇相同", ok_xml, False), ("B 少一段", ok_xml.replace("<p>装好了</p>", ""), True),
                               ("只差图片 alt 不算不同", ok_xml + '<img alt="甲"/>', None)]:
        a = ok_xml + ('<img alt="乙"/>' if want_red is None else "")
        got_red = bool(twin_diff(a, b))
        exp = False if want_red is None else want_red
        fails += got_red != exp
        print(f"  {'PASS' if got_red == exp else 'FAIL'}  {label}(期望{'红' if exp else '绿'})")
    print("\nSELFTEST:", "FAIL" if fails else "PASS")
    return 1 if fails else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    sys.exit(selftest() if ap.parse_args().selftest else run())
