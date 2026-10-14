"""把 bodies.py 的正文套到飞书文档上;原有图片/附件原样保留,挪回 ⟦X⟧ 占位处。

用法:
  python3 scripts/agent-oneclick-docs/apply.py --all          # 五篇都套(改完 bodies.py 后用这个)
  python3 scripts/agent-oneclick-docs/apply.py <doc_id>        # 只套一篇
改完一律跑 scripts/agent-oneclick-docs-audit.py 收口。
⚠️ 只换正文文字;附件(zip/安装器)换新版本另外按 skill 里的「换附件」步骤做。
"""
import json, re, subprocess, sys
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bodies  # noqa: E402

DOCS = {
    "OQCPdLd4MovEVoxGzdMcD3CJnCf": "cursor",     # Cursor 一键配置 A
    "FapXdudPLoTTRLxWGJQcqITpn9g": "cursor",     # Cursor 一键配置 B(同名,正文须逐字相同)
    "Ni2hdvtbgoqBFMxrAu5ckjPrnbg": "codex",      # Codex 一键配置(wiki Q0pJwvdG…)
    "ZYNsdZCJIoCJEJxeMPGcl7jDn3f": "codex-ext",  # Codex 一键配置 外部分享(不带内部互链)
    "MeLad7oMiobwf6xnpA2cWy0nnSg": "claude",     # Claude Code 一键配置
}
if len(sys.argv) > 1 and sys.argv[1] == "--all":
    for d in DOCS:
        subprocess.run([sys.executable, __file__, d], check=False)
    sys.exit(0)

H = Path(__file__).resolve().parents[2] / ".work" / "agent-oneclick-docs"   # 前后快照(不进 git)
H.mkdir(parents=True, exist_ok=True)
DOC = sys.argv[1]
KIND = sys.argv[2] if len(sys.argv) > 2 else DOCS[DOC]
BODY = {"cursor": bodies.CURSOR, "codex": bodies.codex(False), "codex-ext": bodies.codex(True), "claude": bodies.CLAUDE}[KIND]
FIG = {"cursor-g-setup.zip": "ZIP", "install-mac.command": "MAC", "install-windows.bat": "WIN",
       "codex_20260831.dmg": "DMG", "Codex-Windows-x64-20260831.msix": "MSIX"}
ET.fromstring("<r>" + BODY + "</r>")  # 先确认 XML 合法


def lark(*a):
    return json.loads(subprocess.run(["lark-cli", *a], capture_output=True, text=True).stdout)


def fetch():
    return lark("docs", "+fetch", "--doc", DOC, "--detail", "with-ids")["data"]["document"]["content"]


def upd(cmd, **kw):
    a = ["docs", "+update", "--doc", DOC, "--command", cmd]
    for k, v in kw.items():
        a += ["--" + k.replace("_", "-"), v]
    d = lark(*a)
    r = d.get("data", {}).get("result")
    if r != "success":
        print("   !!", cmd, json.dumps(d, ensure_ascii=False)[:300])
    return r


def top(xml):
    return [e for e in ET.fromstring("<r>" + xml + "</r>") if e.tag != "title"]


xml = fetch()
(H / f"{DOC}.before.xml").write_text(xml)
els = top(xml)
keep = {}
for e in els:
    if e.tag == "figure":
        keep[FIG[e.find("source").get("name")]] = e.get("id")
imgs = [e.get("id") for e in els if e.tag == "img"]
for i, x in enumerate(imgs):
    keep[f"IMG{i}"] = x
want = set(re.findall(r"⟦(\w+)⟧", BODY))
assert want == set(keep), (sorted(want), sorted(keep))
kids = list(keep.values())

# 1) 保留块挪到最后
last = els[-1].get("id") or [li.get("id") for li in els[-1]][-1]   # 列表容器本身没有 id,用最后一个 li
if [e.get("id") for e in els[-len(kids):]] != kids:
    upd("block_move_after", block_id=last, src_block_ids=",".join(kids))
# 2) 其余整段替换
ids = []
for e in top(fetch()):
    if e.tag in ("ol", "ul"):
        ids += [li.get("id") for li in e]
    elif e.get("id") not in kids:
        ids.append(e.get("id"))
(H / "body.xml").write_text(BODY)
assert upd("block_replace", block_id=",".join(ids), content=f"@{H / 'body.xml'}") == "success"
# 3) 保留块各回各位,删占位段
xml = fetch()
for k, bid in keep.items():
    m = re.search(r'<p id="([^"]+)"[^>]*>⟦%s⟧</p>' % k, xml)
    upd("block_move_after", block_id=m.group(1), src_block_ids=bid)
    upd("block_delete", block_id=m.group(1))
    xml = fetch()
assert "⟦" not in xml
(H / f"{DOC}.after.xml").write_text(xml)
print(DOC[:8], KIND, "H1:", [re.sub(r"<[^>]+>", "", h) for h in re.findall(r"<h1[^>]*>(.*?)</h1>", xml)],
      "fig", xml.count("<figure"), "img", xml.count("<img"), "pre", xml.count("<pre"))
