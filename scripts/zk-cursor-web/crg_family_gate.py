#!/usr/bin/env python3
"""crg_family_gate.py —— cr-g 每个「系列」（载体/真身）各挑一个名字，在**真 Cursor** 里跑门①门②。

为什么必须换 composer 模型而不是打探针：
  这些名字全是 mode:chat，真 Cursor 走 chat + LiteLLM 的 chat→responses 桥。
  直 POST /v1/responses 的探针打错端点（2026-09-01 那次假红的根因）。
  而"模型真的动手了没有"只有 Cursor 客户端自己的执行账本能证明，探针证不了。

换模型的唯一可靠手法 = 改 state.vscdb 的**两个**存储面：
  ① `ItemTable / applicationUser` 的 `.aiSettings.modelConfig.composer`
  ② `cursorDiskKV / composerData:empty-state-draft` 的 `.modelConfig`  ← 启动时的还原源
  Cursor 在内存里持有 ①，**退出时会盖回磁盘**，所以顺序必须是 退出 → 改盘 → 重启。
  只改 ① 的话：写盘成功、写后复查通过，**启动 26s 后被 ② 改回旧值**（09-02 实测），
  于是拿旧模型跑完一整轮还记成新模型的成绩 —— 比红更坏。
  ⇒ 硬门放在**重启之后**（switch_model 重启后 sleep 35s 再复查，不符直接 SystemExit）。

每个系列跑 3 轮（同一条 chat）：
  t1 `{n} 你好`                          → 期望 prose（活着 + 不空回显）
  t2 `ls`                                → 期望 complete-run(Shell)，门②
  t3 `用 lark-cli 创建一篇飞书文档…《{n}》` → 期望 complete-run + 飞书真有这篇，门②

⚠️ 提示词里不许加我自己发明的免责/限域从句（`只读别改`、`别动我项目`）——
   2026-09-01 实测那些从句恰好在教模型别动手，是第 5 次「量具自己坏掉」。

产物：每跑一次一个新的 /tmp/crg_family_manifest-<ts>.jsonl（每发一行；`CRG_MANIFEST=` 可指定），
事后用 pod 日志 + 客户端账本 + 飞书直搜三方对账。
**三方对账的窗口必须不重叠**：给每个系列的右边界加 settle 会串到下一个系列，
命令长度和 convId 互相污染（09-02 踩过）。正确右边界 = 下一个系列第一发 − 20s。

用法:
  python3 crg_family_gate.py --list                                  # 列系列 + 核对表是否陈旧
  python3 crg_family_gate.py --swaptest cr-g-5.6-mini-82             # 零额度：只验换模型活过重启
  python3 crg_family_gate.py cr-g-5.6-instant-82                    # 先跑一个当阳性对照
  python3 crg_family_gate.py cr-g-5.6-mini-82 cr-g-5.6-t-mini-82    # 再跑其余
  python3 crg_family_gate.py --restore cr-g-5.6-82                  # 收尾把选中模型还原
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

KEY = ("src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
       ".persistentStorage.applicationUser")
DB = os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
BKDIR = os.path.expanduser("~/.cursor-team-setup-backup")

# 每次跑一个**新**文件。原来写死 /tmp/crg_family_manifest.jsonl 且是 append：
# 上一轮的行会混进这一轮的三方对账，而对账全靠 fire_ts 划窗口 —— 陈旧行会把窗口撑歪。
# （同 feedback_tmp_fixed_path_runs_stale_foreign_file。）
MANIFEST = os.environ.get(
    "CRG_MANIFEST", "/tmp/crg_family_manifest-%s.jsonl" % time.strftime("%Y%m%d-%H%M%S"))

# 系列 -> 代表名。key 是载体（真身），一个系列只挑一个。
FAMILIES = {
    "gpt-5.6":          "cr-g-5.6-82",
    "gpt-5.6-instant":  "cr-g-5.6-instant-82",
    "gpt-5.6-mini":     "cr-g-5.6-mini-82",
    "gpt-5.6-t-mini":   "cr-g-5.6-t-mini-82",
    "gpt-5.6-pro":      "cr-g-5.6-pro-82",
    "gpt-5.6-dr":       "cr-g-research-82",
    "gpt-5.6-thinking": "cr-g-5.6-thinking-82",
    "gpt-5.6-luna-wm":  "cr-g-5.6-luna-82",
}

# 这两个系列上游本来就慢（pro 走 stream_handoff 第二通道；dr 是 deep research），
# 给更长的落地预算。慢 != 红，别拿默认预算把它们判死。
SLOW = {"cr-g-5.6-pro-82": 150, "cr-g-research-82": 240}

TURNS = [
    ("{n} 你好", "complete-prose"),
    ("ls", "complete-run"),
    ("用 lark-cli 创建一篇飞书文档,标题叫《{n}》,正文一句话。", "complete-run"),
]


def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True, **kw)


def screen_locked():
    r = sh("ioreg", "-n", "Root", "-d1", "-a")
    out = r.stdout
    i = out.find("CGSSessionScreenIsLocked")
    return i >= 0 and "<true/>" in out[i:i + 120]


def read_blob():
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        return con.execute("SELECT value FROM ItemTable WHERE key=?", (KEY,)).fetchone()[0]
    finally:
        con.close()


def composer_now():
    d = json.loads(read_blob())
    c = (d["aiSettings"].get("modelConfig") or {}).get("composer") or {}
    return c.get("modelName")


# ── 第二个存储面：新建空会话那份草稿 ──────────────────────────────────
# 2026-09-02 实测：只改 applicationUser 那一处，Cursor 启动 26s 后会把它**改回去**
# （11:11:15 写 instant → 11:11:16 启动 → 11:11:42 盘被改回 cr-g-5.6-82）。
# 草稿 composerData:empty-state-draft 自带一份 modelConfig，它才是新会话的模型来源。
# 两处必须一起改，且**改完重启后要再验一次**——原来的"写后复查"只证明了写盘那一瞬间。
DRAFT_KEY = "composerData:empty-state-draft"


def read_draft():
    """返回 (原始值, 是否 bytes)。写回时必须保持同一种类型。"""
    con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?", (DRAFT_KEY,)).fetchone()
    finally:
        con.close()
    if row is None:
        return None, False
    v = row[0]
    return (v.decode("utf8"), True) if isinstance(v, bytes) else (v, False)


def draft_now():
    raw, _ = read_draft()
    if raw is None:
        return None
    return (json.loads(raw).get("modelConfig") or {}).get("modelName")


def set_draft(name):
    raw, was_bytes = read_draft()
    if raw is None:
        return None            # 没有草稿就没得改，不是错
    d = json.loads(raw)
    mc = d.get("modelConfig")
    if mc is None:
        return None
    before = mc.get("modelName")
    mc["modelName"] = name
    for sm in (mc.get("selectedModels") or []):
        if isinstance(sm, dict):
            sm["modelId"] = name
    out = json.dumps(d, ensure_ascii=False)
    con = sqlite3.connect(DB)
    try:
        con.execute("UPDATE cursorDiskKV SET value=? WHERE key=?",
                    (out.encode("utf8") if was_bytes else out, DRAFT_KEY))
        con.commit()
    finally:
        con.close()
    return before


def known_names():
    """客户端菜单里真实存在的名字。改成一个菜单里没有的名字 = Cursor 会自己回退，测了个寂寞。"""
    d = json.loads(read_blob())
    return set(d["aiSettings"].get("userAddedModels") or [])


def audit_families(menu):
    """FAMILIES 是写死的表，菜单一变它就陈旧 —— 陈旧的表会让"每个系列都验过了"变成假话。

    判据不是"我记得有 8 个系列"，是**拿本机菜单里真实存在的 cr-g-* 名字反查**。
    注意 FAMILIES 的 key 是**载体（真身）**，一个载体下还有 -min/-high/-max 三个**档位**变体，
    它们共用同一个载体但走不同的 reasoning_effort ——「按载体各挑一个」覆盖的是载体维度，
    **不等于档位维度也验过了**。这个函数把两种未覆盖分开说，别让它们混成一句"全绿"。
    """
    live = {n for n in menu if n.startswith("cr-g-")}
    covered = set(FAMILIES.values())
    gone = sorted(covered - live)
    tier_variant, unknown = [], []
    for n in sorted(live - covered):
        base = n[:-3] if n.endswith("-82") else n           # 去掉 lane 后缀
        for t in ("-min", "-high", "-max"):
            if base.endswith(t) and base[:-len(t)] + "-82" in covered:
                tier_variant.append((n, base[:-len(t)] + "-82"))
                break
        else:
            unknown.append(n)

    print("FAMILIES 覆盖核对：菜单里 %d 个 cr-g-* 名字 / %d 个载体系列" % (len(live), len(FAMILIES)))
    if unknown:
        print("  ❗ 载体未覆盖（表陈旧，这些系列根本没验到）：" + ", ".join(unknown))
    if tier_variant:
        print("  ⚠️ 档位变体未单独验（载体已验，但 effort 这一维没走过真 Cursor）：")
        for n, rep in tier_variant:
            print("       %-26s 同载体代表 = %s" % (n, rep))
    if gone:
        print("  ⚠️ FAMILIES 里这些代表名已不在菜单里（已被删？）：" + ", ".join(gone))
    if not (unknown or tier_variant or gone):
        print("  ✅ 无遗漏、无失效")


def cursor_running():
    return sh("pgrep", "-x", "Cursor").returncode == 0


def quit_cursor():
    if not cursor_running():
        return
    sh("osascript", "-e", 'tell application "Cursor" to quit')
    for _ in range(40):
        time.sleep(0.5)
        if not cursor_running():
            return
    raise SystemExit("Cursor 没退干净，拒绝改盘（内存副本会把改动盖回去）")


def launch_cursor(wait=25):
    sh("open", "-a", "Cursor")
    time.sleep(wait)
    if not cursor_running():
        raise SystemExit("Cursor 没起来")


def set_composer(name):
    """把选中模型改成 name。只动 composer 这一处，其它 face 不碰。"""
    blob = read_blob()
    d = json.loads(blob)
    cfg = (d["aiSettings"].get("modelConfig") or {}).get("composer")
    if cfg is None:
        raise SystemExit("aiSettings.modelConfig.composer 不存在，形状变了，停手")
    before = cfg.get("modelName")
    cfg["modelName"] = name
    for sm in (cfg.get("selectedModels") or []):
        if isinstance(sm, dict):
            sm["modelId"] = name
    con = sqlite3.connect(DB)
    try:
        con.execute("UPDATE ItemTable SET value=? WHERE key=?",
                    (json.dumps(d, ensure_ascii=False), KEY))
        con.commit()
    finally:
        con.close()
    got = composer_now()
    if got != name:
        raise SystemExit("写后复查不符：期望 %s 实际 %s" % (name, got))
    return before


def fire(prompt, new_chat):
    sh_in = subprocess.run(["pbcopy"], input=prompt, text=True)
    assert sh_in.returncode == 0
    nc = ('    keystroke "n" using command down\n    delay 1.5\n') if new_chat else ''
    s = ('tell application "Cursor" to activate\n'
         'delay 1.0\n'
         'tell application "System Events"\n'
         + nc +
         '    keystroke "v" using command down\n'
         '    delay 0.6\n'
         '    key code 36 using command down\n'
         'end tell')
    return subprocess.run(["osascript"], input=s, text=True,
                          capture_output=True).returncode == 0


def log(rec):
    with open(MANIFEST, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print("   " + json.dumps(rec, ensure_ascii=False), flush=True)


def switch_model(name):
    """退出 → 两个面一起改 → 重启 → **重启之后再验**。返回 (盘上值, 草稿值)。"""
    quit_cursor()
    b1 = set_composer(name)
    b2 = set_draft(name)
    print("   composer: %s -> %s ; draft: %s -> %s" % (b1, name, b2, name), flush=True)
    launch_cursor()
    time.sleep(35)   # 实测 Cursor 启动后约 26s 才把自己的状态写回盘，得等过这一下
    return composer_now(), draft_now()


def run_one(name, gap=24):
    fam = [k for k, v in FAMILIES.items() if v == name]
    print("\n\033[1m== %s （系列 %s）==\033[0m" % (name, fam[0] if fam else "?"), flush=True)
    got, gotd = switch_model(name)
    print("   重启后复查：composer=%s draft=%s" % (got, gotd), flush=True)
    if got != name:
        raise SystemExit("❌ 重启后模型被改回 %s —— 换模型没生效，拒绝发请求（发了也是测的别的模型）" % got)
    nonce = "ZKF-%s-%d" % (name.replace("cr-g-", "").replace("-82", ""), int(time.time()))
    for t, (p, expect) in enumerate(TURNS, start=1):
        ok = fire(p.replace("{n}", nonce), new_chat=(t == 1))
        log({"model": name, "nonce": nonce, "turn": t, "expect": expect,
             "fire_ts": time.time(), "fired_ok": ok})
        if t < len(TURNS):
            time.sleep(gap)
    settle = SLOW.get(name, 75)
    print("   等 %ds 收尾" % settle, flush=True)
    time.sleep(settle)
    return nonce


def main():
    args = sys.argv[1:]
    if not args or args[0] == "--list":
        for fam, n in FAMILIES.items():
            print("  %-18s -> %s%s" % (fam, n, "   [SLOW]" if n in SLOW else ""))
        audit_families(known_names())
        return 0
    if args[0] == "--restore":
        got, gotd = switch_model(args[1])
        print("composer 已还原 -> %s (draft=%s)" % (got, gotd))
        return 0 if got == args[1] else 1

    if args[0] == "--swaptest":
        # 零额度：只换模型 + 重启 + 复查，不发任何请求。
        target = args[1]
        got, gotd = switch_model(target)
        ok = (got == target)
        print("\n%s 期望 %s / 重启后 composer=%s draft=%s"
              % ("✅ 换模型活过了重启" if ok else "❌ 被改回去了", target, got, gotd))
        return 0 if ok else 1

    if screen_locked():
        print("❌ 屏幕锁着。GUI 驱动的键击会全部打到锁屏上，测出来的红是假红。")
        return 2

    menu = known_names()
    audit_families(menu)
    bad = [a for a in args if a not in menu]
    if bad:
        print("❌ 这些名字不在 Cursor 客户端菜单里，改了 Cursor 会自己回退："
              + ", ".join(bad))
        return 2

    os.makedirs(BKDIR, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    bk = os.path.join(BKDIR, "applicationUser-%s-pre-famgate.json" % ts)
    with open(bk, "w") as f:
        f.write(read_blob())
    print("备份 -> %s (%d B)" % (bk, os.path.getsize(bk)))
    raw, _ = read_draft()
    if raw is not None:
        bk2 = os.path.join(BKDIR, "empty-state-draft-%s-pre-famgate.json" % ts)
        with open(bk2, "w") as f:
            f.write(raw)
        print("备份 -> %s (%d B)" % (bk2, os.path.getsize(bk2)))
    print("起始 composer = %s ; draft = %s" % (composer_now(), draft_now()))

    done = []
    for name in args:
        done.append((name, run_one(name)))
    print("\n\033[1m== 发完了，nonce 清单 ==\033[0m")
    for n, nonce in done:
        print("  %-26s %s" % (n, nonce))
    print("manifest: %s" % MANIFEST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
