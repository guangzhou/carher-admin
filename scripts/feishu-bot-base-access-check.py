#!/usr/bin/env python3
"""判一个自建 bot 能不能读到某张多维表的「行」—— 只读，不改任何权限。

为什么需要它（2026-10-01 实测）：bot 读不到行时飞书一律 `code 0` + 0 行，不报错，
而且「不是协作者」和「是可阅读协作者但被高级权限的行规则滤光」现象完全一样。
「已加协作者」不等于「能读到行」。

四层逐层查，每层都用 bot 自己的 tenant token，最后用你本人的身份（lark-cli --as user）
读同一视图做阳性对照：
  L1 token          bot 凭据能不能换 token
  L2 表元信息        is_advanced（高级权限开没开）
  L3 字段            能不能读 —— 读不到 ⇒ 不是协作者
  L4 行              bot 读到几行
  CTRL 用户身份行数   对照；L4 == CTRL 才算通

用法：
  export FEISHU_APP_ID=cli_xxx FEISHU_APP_SECRET=xxx
  python3 scripts/feishu-bot-base-access-check.py --base DlT9bsrwMad12VsogEpcK9Ptncc \\
      --table tblJT2s6Y6xjYj5A --view vewISEKPg7
  python3 scripts/feishu-bot-base-access-check.py --wiki UvIxwsWLQivn1RkrdPTcVGGnnze --table ...

退出码：0 bot 读到的行数 == 对照 / 1 读不全或 0 行（stderr 有诊断） /
       2 参数错 / 3 量具自己坏了（换不到 token、对照读不出来）—— 3 时 L4 的结论不可信。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

OPEN = "https://open.feishu.cn"


def http(method, path, token=None, body=None):
    req = urllib.request.Request(OPEN + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"code": e.code, "msg": f"HTTP {e.code}"}


def count_rows(fetch_page) -> int | None:
    n, tok = 0, ""
    for _ in range(200):
        d = fetch_page(tok)
        if d is None or d.get("code", 0) != 0:
            return None
        data = d.get("data") or {}
        n += len(data.get("items") or [])
        if not data.get("has_more"):
            return n
        tok = data.get("page_token") or ""
        if not tok:
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--base", help="base token（bitable app_token）")
    g.add_argument("--wiki", help="wiki 节点 token，自动换成 base token（走你本人身份）")
    ap.add_argument("--table", required=True)
    ap.add_argument("--view", default="")
    ap.add_argument("--no-control", action="store_true", help="不跑用户身份对照（没有 lark-cli 时）")
    a = ap.parse_args()

    app_id, secret = os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not secret:
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET", file=sys.stderr)
        return 2
    has_cli = shutil.which("lark-cli") is not None

    def cli(*args):
        out = subprocess.run(["lark-cli", *args, "--as", "user"], capture_output=True, text=True).stdout
        try:
            d = json.loads(out)
        except ValueError:
            return None
        # lark-cli api 有时把飞书原始响应包一层
        return d if "data" in d and isinstance(d.get("data"), dict) and "code" in d else \
            {"code": d.get("code", 0), "data": d.get("data", d)}

    base = a.base
    if a.wiki:
        if not has_cli:
            print("--wiki 需要 lark-cli；改传 --base", file=sys.stderr)
            return 2
        d = cli("wiki", "+node-get", "--node-token", a.wiki)
        node = ((d or {}).get("data") or {})
        node = node.get("node", node)
        base = node.get("obj_token")
        if not base or node.get("obj_type") != "bitable":
            print(f"wiki 节点不是多维表：{node.get('obj_type')}", file=sys.stderr)
            return 2

    rep = {"base": base, "table": a.table, "view": a.view or None}

    tok = http("POST", "/open-apis/auth/v3/tenant_access_token/internal",
               body={"app_id": app_id, "app_secret": secret})
    if tok.get("code") != 0:
        rep["L1_token"] = f"FAIL [{tok.get('code')}] {tok.get('msg')}"
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 3
    T = tok["tenant_access_token"]
    rep["L1_token"] = "ok"

    meta = http("GET", f"/open-apis/bitable/v1/apps/{base}", T)
    adv = ((meta.get("data") or {}).get("app") or {}).get("is_advanced") if meta.get("code") == 0 else None
    rep["L2_meta"] = "ok" if meta.get("code") == 0 else f"FAIL [{meta.get('code')}] {meta.get('msg')}"
    rep["is_advanced"] = adv

    f = http("GET", f"/open-apis/bitable/v1/apps/{base}/tables/{a.table}/fields?page_size=1", T)
    fields_ok = f.get("code") == 0
    rep["L3_fields"] = f"ok (total {(f.get('data') or {}).get('total')})" if fields_ok else \
        f"FAIL [{f.get('code')}] {f.get('msg')}"

    body = {"view_id": a.view} if a.view else {}

    def bot_page(pt):
        q = {"page_size": "500"} | ({"page_token": pt} if pt else {})
        return http("POST", f"/open-apis/bitable/v1/apps/{base}/tables/{a.table}/records/search?"
                    + urllib.parse.urlencode(q), T, body)
    bot_rows = count_rows(bot_page)
    rep["L4_bot_rows"] = bot_rows

    ctrl = None
    if not a.no_control and has_cli:
        def user_page(pt):
            p = {"page_size": 500} | ({"page_token": pt} if pt else {})
            return cli("api", "POST", f"/open-apis/bitable/v1/apps/{base}/tables/{a.table}/records/search",
                       "--params", json.dumps(p), "--data", json.dumps(body))
        ctrl = count_rows(user_page)
    rep["CTRL_user_rows"] = ctrl if not a.no_control else "skipped"

    if bot_rows is None and not fields_ok:
        verdict, rc = "bot 连字段都读不到 ⇒ 不是协作者，或 table id 写错（看 L3 的错误码）", 1
    elif bot_rows is None:
        verdict, rc = "bot 读行接口报错或分页没结束", 1
    elif not a.no_control and ctrl is None:
        verdict, rc = "对照读不出来（lark-cli 没登录 / 你本人也没权限）⇒ bot 的行数没法判", 3
    elif not a.no_control and bot_rows == ctrl and ctrl > 0:
        verdict, rc = f"通：bot 读到 {bot_rows} 行，与你本人一致", 0
    elif not fields_ok:
        verdict, rc = "bot 连字段都读不到 ⇒ 不是协作者。多维表「…→ 添加文档应用」加上它", 1
    elif bot_rows == 0 and adv:
        verdict, rc = ("bot 是协作者但被高级权限的行规则滤光 ⇒ 升「可管理」，或在高级权限里给它单建"
                       "「所有行可读」的角色（改共享表权限，先问表的所有者）"), 1
    elif a.no_control:
        verdict, rc = f"bot 读到 {bot_rows} 行（没跑对照，不判是否读全）", 0 if bot_rows else 1
    else:
        verdict, rc = f"bot 读到 {bot_rows} 行，你本人 {ctrl} 行 ⇒ 读不全，查视图筛选或角色行规则", 1
    rep["verdict"] = verdict
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return rc


if __name__ == "__main__":
    sys.exit(main())
