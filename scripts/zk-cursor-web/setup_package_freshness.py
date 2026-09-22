#!/usr/bin/env python3
# 判据:交付出去的 cursor-g-setup.zip 里的字节,和仓库工作副本逐成员 sha256 相等。
#
# 存在理由(2026-09-21):同事问"脚本是最新的吗?怎么和我之前的大小不一样",
# 当时是手搓 python 一次性比出来的。mtime 更新 ≠ 内容是新的,zip 更大 ≠ 装的东西变了;
# 唯一能下结论的尺子是**逐成员 sha256** + **逐成员 size diff vs 上一次发出去的那份**。
# 所以把那次的手工步骤固化成一条能红的腿。
#
# 用法:
#   ./setup_package_freshness.py                        # 只比 zip vs 工作副本
#   ./setup_package_freshness.py --against /tmp/old.zip # 再比一遍"上次发出去的那份"
#   ./setup_package_freshness.py --selftest             # 阳性对照:尺子自己必须能变红
#
# 退出码:0 = PASS,1 = FAIL(有腿变红),2 = 用法/环境错(尺子坏了,不是结论)。
import argparse
import hashlib
import io
import os
import re
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ZIP = os.path.join(BASE, "cursor-g-setup.zip")
# zip 成员一律带这层前缀。忘了剥它会让每个成员都读成 "工作副本里没有这个文件",
# 那是**假绿**(全 NOSRC ⇒ 一个都没比,却没有一条腿变红)。2026-09-21 踩过。
ZIP_PREFIX = "cursor-g-setup/"
# 这些只由 package_team_setup.sh 现生成,仓库目录里本来就没有 —— 不是"缺文件"。
# 写成显式名单而不是"忽略找不到的",这样少了一个双击器能变红。
GENERATED_ONLY = {
    "INSTALL-Mac.command", "UPGRADE-Mac.command", "REPAIR-Mac.command",
    "UNINSTALL-Mac.command", "TURN-OFF-DELTA-Mac.command",
    "INSTALL-Windows.cmd", "UPGRADE-Windows.cmd", "REPAIR-Windows.cmd",
    "UNINSTALL-Windows.cmd",
    # 09-21 加:远程窗口直通。两个双击启动器是 package 里 heredoc 生成的;
    # `cursor_remote_ssh.cmd` 是**从 cursor_team_setup.cmd sed 生成**的(找 Cursor.exe
    # 那六段逻辑单一真源) ⇒ 工作副本里本来就不该有这三个文件。
    # ⚠️ `cursor_remote_ssh.js` / `.sh` **故意不在这里**:那两个是真源文件,
    #    漏改就该让这道门禁红,而不是被当成"生成物"放行。
    "REMOTE-SSH-Mac.command", "REMOTE-SSH-Windows.cmd", "cursor_remote_ssh.cmd",
    "README.txt",
    "zk-delta/common/framing.js", "zk-delta/sidecar/sidecar.js",
    "zk-delta/SHA256SUMS.txt",
}
JS_MEMBER = "cursor_team_setup.js"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def short(b):
    return sha(b)[:16]


def zip_members(path):
    """{成员相对路径: bytes},已剥掉 ZIP_PREFIX。目录条目丢掉。"""
    if not os.path.exists(path):
        sys.exit("ENV: zip 不存在: %s" % path)
    out = {}
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            if not name.startswith(ZIP_PREFIX):
                sys.exit("ENV: 成员 %r 没有预期前缀 %r —— 打包脚本改过结构,先看 "
                         "package_team_setup.sh" % (name, ZIP_PREFIX))
            out[name[len(ZIP_PREFIX):]] = z.read(info)
    if not out:
        sys.exit("ENV: zip 里一个文件都没有")
    return out


def extract_models(js):
    """有界地抠 DEFAULT_MODELS。

    2026-09-21 的坑:从 js.index('DEFAULT_MODELS') 松散切到第一个 `];`,读出 36 条,
    里面混着 'use strict' / 'path' / 'crypto' / 'darwin' / path.join 的碎片 —— 得出的
    "缺失名单" 全是垃圾。所以这里用带界的正则 + 断言条数,尺子坏了当场停,而不是报结论。
    """
    m = re.search(r"const DEFAULT_MODELS\s*=\s*\[(.*?)\n\];", js, re.S)
    if not m:
        sys.exit("ENV: 没找到有界的 DEFAULT_MODELS 数组 —— 抠法失效,停")
    body = re.sub(r"//[^\n]*", "", m.group(1))
    return re.findall(r'"([^"]+)"', body)


def leg_zip_vs_worktree(members):
    """第①腿:zip 里每个成员 == 仓库工作副本(逐成员 sha256)。"""
    fail = 0
    same = drift = gen = 0
    for name in sorted(members):
        disk = os.path.join(BASE, name)
        if not os.path.exists(disk):
            if name in GENERATED_ONLY:
                gen += 1
                continue
            fail = 1
            print("❌ %-34s zip 里有、工作副本里没有(且不在 GENERATED_ONLY 名单里)" % name)
            continue
        disk_bytes = io.open(disk, "rb").read()
        if sha(disk_bytes) == sha(members[name]):
            same += 1
        else:
            fail = 1
            drift += 1
            print("❌ %-34s zip %d B/%s  ≠  工作副本 %d B/%s —— zip 是旧的,重跑 "
                  "package_team_setup.sh" % (name, len(members[name]),
                                             short(members[name]),
                                             len(disk_bytes), short(disk_bytes)))
    # 反向:仓库里有、zip 里漏了的核心文件
    for name in (JS_MEMBER, "cursor_team_setup.sh", "cursor_team_setup.cmd"):
        if name not in members:
            fail = 1
            print("❌ 核心文件 %r 不在 zip 里" % name)
    if not fail:
        print("✅ 第①腿 zip vs 工作副本:%d 个逐字节相同,%d 个由打包脚本现生成(不在仓库)"
              % (same, gen))
    else:
        print("   第①腿汇总:相同 %d,漂移 %d,现生成 %d" % (same, drift, gen))
    return fail


def leg_vs_previous(members, old_path, new_path):
    """第②腿:与"上次发出去的那份"逐成员 diff。

    这条腿**不返红** —— 两份不一样是预期的(那就是本次升级)。它的产出是一张
    "到底变了哪几个文件、模型清单增删了什么" 的账,用来回答同事"为什么大小变了"。
    结论不许是 "mtime 新所以是最新的",必须是这张账加起来刚好等于 size 差。
    """
    old = zip_members(old_path)
    print("\n— 第②腿 vs 上次发出去的那份(%s,%d B)—" % (old_path, os.path.getsize(old_path)))
    changed, identical = [], 0
    for name in sorted(set(members) | set(old)):
        a, b = old.get(name), members.get(name)
        if a is None:
            changed.append(("新增", name, 0, len(b)))
        elif b is None:
            changed.append(("删除", name, len(a), 0))
        elif sha(a) != sha(b):
            changed.append(("改动", name, len(a), len(b)))
        else:
            identical += 1
    for kind, name, na, nb in changed:
        print("  %s %-34s %d → %d B (%+d)" % (kind, name, na, nb, nb - na))
    print("  完全相同:%d 个" % identical)
    delta = sum(nb - na for _, _, na, nb in changed)
    print("  改动成员未压缩净增:%+d B(zip 总大小差 %+d B,压缩率不同属正常)"
          % (delta, os.path.getsize(new_path) - os.path.getsize(old_path)))

    if JS_MEMBER in members and JS_MEMBER in old:
        new_names = extract_models(members[JS_MEMBER].decode("utf-8"))
        old_names = extract_models(old[JS_MEMBER].decode("utf-8"))
        added = [n for n in new_names if n not in old_names]
        removed = [n for n in old_names if n not in new_names]
        print("  菜单:%d → %d 个;新增 %r;删除 %r" %
              (len(old_names), len(new_names), added, removed))
    return 0


def leg_menu_shape(members, expect_n):
    """第③腿:zip 里那份 js 的菜单条数 == 期望值,且抠法本身没坏。"""
    if JS_MEMBER not in members:
        print("❌ 第③腿:zip 里没有 %s" % JS_MEMBER)
        return 1
    names = extract_models(members[JS_MEMBER].decode("utf-8"))
    if len(names) != expect_n:
        print("❌ 第③腿:zip 里的菜单是 %d 个而不是 %d —— 要么发错版本,要么 --expect "
              "该更新了。名单:%r" % (len(names), expect_n, names))
        return 1
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        print("❌ 第③腿:菜单有重名 %r" % dup)
        return 1
    print("✅ 第③腿 zip 内菜单 %d 个、无重名" % len(names))
    return 0


def selftest():
    """阳性对照:合成一个"改过一个字节"的 zip,第①腿必须变红。

    第 0 步永远是阳性对照 —— 一把只会绿的尺子不是门。这里同时验 extract_models
    的界:给它一段带 'use strict' 的假 js,抠出来必须仍是引号里的模型名。
    """
    import tempfile
    rc = 0
    members = zip_members(DEFAULT_ZIP)
    with tempfile.TemporaryDirectory() as td:
        bad = os.path.join(td, "bad.zip")
        with zipfile.ZipFile(bad, "w") as z:
            for name, data in members.items():
                if name == JS_MEMBER:
                    data = data + b"\n// selftest tamper\n"
                z.writestr(ZIP_PREFIX + name, data)
        print("— 阳性对照:篡改 %s 后跑第①腿 —" % JS_MEMBER)
        got = leg_zip_vs_worktree(zip_members(bad))
        if got == 0:
            print("❌ 阳性对照没红 —— 这把尺子量不出漂移,不许拿它下结论")
            rc = 1
        else:
            print("✅ 阳性对照按预期变红")

    fake = 'const X = "use strict";\nconst DEFAULT_MODELS = [\n  "a-1", // c\n  "b-2",\n];\n'
    got = extract_models(fake)
    if got != ["a-1", "b-2"]:
        print("❌ extract_models 抠错:%r(松散切法会把 'use strict' 也读进来)" % got)
        rc = 1
    else:
        print("✅ extract_models 有界抠取正确:%r" % got)
    print("SELFTEST:", "FAIL" if rc else "PASS")
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default=DEFAULT_ZIP)
    ap.add_argument("--against", help="上次发出去的那份 zip(飞书文档附件用 "
                                      "`lark-cli docs +media-download` 拉,"
                                      "drive +download 会静默不落盘)")
    ap.add_argument("--expect", type=int, default=30, help="期望菜单条数")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()

    members = zip_members(a.zip)
    print("zip: %s  %d B  sha256_16=%s  %d 个文件"
          % (a.zip, os.path.getsize(a.zip), short(io.open(a.zip, "rb").read()),
             len(members)))
    fail = leg_zip_vs_worktree(members)
    fail |= leg_menu_shape(members, a.expect)
    if a.against:
        leg_vs_previous(members, a.against, a.zip)
    print("\nVERDICT:", "FAIL" if fail else "PASS")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
