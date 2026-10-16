"""把飞书文档里某个附件换成本地新构建的版本:插入 → 下载回来比 sha → 挪到旧块后面 → 删旧块。

用法:
  python3 scripts/agent-oneclick-docs/swap_attachment.py <doc_id> <本地文件> [<本地文件> ...]
  例:python3 scripts/agent-oneclick-docs/swap_attachment.py HMj6d1ttlorc5KxbFnlcVuopntf \
        codex-oneclick/codex-mac.command codex-oneclick/codex-windows.bat
附件按**文件名**匹配(文档里必须恰好有一个同名附件)。换完跑 scripts/agent-oneclick-docs-audit.py 收口。

为什么要这么多步:`+media-insert` 只能插到文档末尾;直接删旧再插会让附件跑到文末、位置全乱;
上传可能被截断/转码,不比 sha 就会把坏文件发给同事(09-29 踩过)。任何一步失败都**不删旧块**。
"""
import hashlib, json, re, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / ".work" / "agent-oneclick-docs"


def lark(*a):
    r = subprocess.run(["lark-cli", *a], capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.exit(f"lark-cli 输出不是 JSON:{r.stdout[:200]} {r.stderr[:200]}")


def fetch(doc):
    return lark("docs", "+fetch", "--doc", doc, "--detail", "with-ids")["data"]["document"]["content"]


def upd(doc, cmd, **kw):
    a = ["docs", "+update", "--doc", doc, "--command", cmd]
    for k, v in kw.items():
        a += ["--" + k.replace("_", "-"), v]
    return lark(*a).get("data", {}).get("result") == "success"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def figures(xml, name):
    """返回 [(figure 块 id, file token)],只要 source name == name 的。"""
    out = []
    for m in re.finditer(r'<figure id="([^"]+)"[^>]*>(.*?)</figure>', xml, re.S):
        s = re.search(r'<source[^>]*name="([^"]+)"[^>]*token="([^"]+)"', m.group(2))
        if s and s.group(1) == name:
            out.append((m.group(1), s.group(2)))
    return out


def swap(doc, local):
    local = Path(local)
    name, want = local.name, sha(local)
    old = figures(fetch(doc), name)
    if len(old) != 1:
        sys.exit(f"❌ {name}:文档里有 {len(old)} 个同名附件,应为 1,不动")
    old_fig, old_tok = old[0]
    dl = WORK / f"swap_{name}"
    WORK.mkdir(parents=True, exist_ok=True)
    lark("docs", "+media-download", "--token", old_tok, "--output", str(dl), "--overwrite")
    if dl.exists() and sha(dl) == want:
        print(f"= {name}:文档里已经是这一版({want[:12]}),跳过")
        return
    r = lark("docs", "+media-insert", "--doc", doc, "--type", "file", "--file", str(local), "--file-view", "card")
    if not r.get("ok"):
        sys.exit(f"❌ {name}:上传失败 {json.dumps(r, ensure_ascii=False)[:300]}")
    new_blk = r["data"]["block_id"]
    # 回读新附件比 sha(新块是 file 块,外层 figure id 要从 xml 里找)
    new = [f for f in figures(fetch(doc), name) if f[0] != old_fig]
    if len(new) != 1:
        sys.exit(f"❌ {name}:上传后找不到唯一的新附件块(新 block {new_blk}),旧块未删,请手动检查")
    new_fig, new_tok = new[0]
    dl.unlink(missing_ok=True)
    lark("docs", "+media-download", "--token", new_tok, "--output", str(dl), "--overwrite")
    if not dl.exists() or sha(dl) != want:
        sys.exit(f"❌ {name}:新附件下载回来 sha 不对,旧块未删;新块 {new_fig} 请手动删")
    if not upd(doc, "block_move_after", block_id=old_fig, src_block_ids=new_fig):
        sys.exit(f"❌ {name}:新附件挪位置失败,旧块未删")
    if not upd(doc, "block_delete", block_id=old_fig):
        sys.exit(f"❌ {name}:旧块删除失败,文档里现在有两个 {name}")
    dl.unlink(missing_ok=True)
    print(f"✅ {name}:已换成 {want[:12]}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    for f in sys.argv[2:]:
        swap(sys.argv[1], f)
