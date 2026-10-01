#!/usr/bin/env python3
"""把 Antigravity `:fetchAvailableModels` 返回的**全部**模型名原样打出来（198 上跑）。

存在的理由：`cliproxy-antigravity-model-matrix.py --quota` 只打印我们**已经配置过**
的那十来个名字（它按 `MODELS` 常量遍历），上游新出的名字根本不会出现在输出里 ——
拿它回答「上游有没有 gemini-4」会恒答「没有」，而那是尺子的盲区不是上游的事实。
这个脚本不带任何预设名单，只枚举上游返回的 key。

🔴 UA 版本号是自变量：`antigravity/1.0.0 windows/amd64` 只能拿到 27 个旧目录，
hub/2.12.2 拿 33 个。所以**要判「上游到底有没有某个新名字」必须用网关当前默认 UA
（hub manifest 现拉的最新版），用老 UA 量到的「没有」不作数**。
默认就从 hub manifest 拉最新版，`--ua` 可覆盖。

只打 `:fetchAvailableModels`，一发生成请求都不发，不花任何额度。

用法：
    sudo python3 antigravity-catalog-dump.py                 # 全部号，打全名单
    sudo python3 antigravity-catalog-dump.py --grep gemini-4 # 只报命中（含阳性对照计数）
"""
import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_matrix_mod():
    """复用 matrix 脚本的 refresh / fetch_models / load_accounts / hub_user_agent。

    不 copy 一份实现：那会在 CLIProxyAPI 改 body 形状时静默漂移。
    """
    for cand in (os.path.join(HERE, "cliproxy-antigravity-model-matrix.py"),
                 os.path.expanduser("~/cliproxy-antigravity-model-matrix.py")):
        if os.path.exists(cand):
            spec = importlib.util.spec_from_file_location("agmatrix", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    sys.exit("找不到 cliproxy-antigravity-model-matrix.py（本脚本复用它的 refresh/fetch）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ua", help="覆盖 UA（默认 hub manifest 现拉的最新版）")
    ap.add_argument("--only", help="逗号分隔 email")
    ap.add_argument("--grep", help="只报含该子串的名字，并打阳性对照（总数 + gemini 名数）")
    ap.add_argument("--json", metavar="PATH", help="落盘 {email: [模型名...]}")
    args = ap.parse_args()

    m = load_matrix_mod()
    ua = args.ua or m.hub_user_agent()
    only = set(x.strip() for x in (args.only or "").split(",") if x.strip())
    print("UA = %s" % ua)

    out, ok_accts = {}, 0
    for path, creds in m.load_accounts():
        email = creds.get("email") or os.path.basename(path)
        if only and email not in only:
            continue
        at, err = m.refresh(creds)
        if not at:
            print("\n=== %s\n  ❌ %s" % (email, err))
            continue
        st, ml = m.fetch_models(at, ua, creds.get("project_id") or creds.get("project"))
        if st != 200 or not isinstance(ml, dict):
            print("\n=== %s\n  ❌ fetchAvailableModels http=%s %s"
                  % (email, st, json.dumps(ml)[:200]))
            continue
        # `models` 是 dict（modelId -> 详情），不是 list
        names = sorted((ml.get("models") or {}).keys())
        dep = set(ml.get("deprecatedModelIds") or [])
        out[email] = names
        ok_accts += 1
        print("\n=== %s  (%d 个模型, deprecated %d)" % (email, len(names), len(dep)))
        if args.grep:
            hit = [n for n in names if args.grep.lower() in n.lower()]
            gem = [n for n in names if "gemini" in n.lower()]
            # 阳性对照：gemini 名数必须 >0，否则这一行的「没命中」是尺子坏了
            print("  grep %r -> %s   [对照: gemini 名 %d 个 / 共 %d]"
                  % (args.grep, hit or "无命中", len(gem), len(names)))
        else:
            for n in names:
                print("    %s%s" % (n, "  ⚠️DEPRECATED" if n in dep else ""))

    if not ok_accts:
        print("\n🔴 零个号量到了目录 —— 这一轮的「上游没有 X」不作数（尺子没跑起来）")
        return 2
    print("\n量到目录的号: %d" % ok_accts)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
        print("已落盘 %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
