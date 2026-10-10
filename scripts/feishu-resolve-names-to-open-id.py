#!/usr/bin/env python3
"""把一批姓名解析成**本 app 视角**的 open_id —— 群发前的收件人解析。

为什么需要它：多维表里那列文本 `open_id` 往往是**别的 app 写进去的**，直接拿来发消息会
`99992361 / 99992351`。2026-10-10 在「组织关系确认」表上实测 6/6 全不同。
人员字段（type 11 User）才给本 app 视角的 id，但那张表 148 行里只有 6 行填了人员字段。

所以路径是：
  人员字段有值        → 直接用（它已经是本 app 视角）
  只有姓名            → lark-cli 用户身份搜通讯录 → user_id → bot 自己换 open_id → 回读校验

输出 JSON：{"resolved":{open_id:姓名}, "fail":[[姓名,原因]], "dup":[[姓名,几个同名]]}
同名多人一律进 fail，不猜 —— 发错人无法撤回。

用法：
  export FEISHU_APP_ID=... FEISHU_APP_SECRET=...
  python3 scripts/feishu-resolve-names-to-open-id.py --names 刘国现,冯宏强
  python3 scripts/feishu-resolve-names-to-open-id.py --names-file names.txt -o out.json

退出码：0 全部解析成功 / 1 有失败（out.json 里有清单） / 2 参数错 / 3 量具坏（换不到 token）
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

OPEN = "https://open.feishu.cn"


def feishu(path: str, token: str) -> dict:
    req = urllib.request.Request(OPEN + path)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"code": e.code, "msg": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"code": -2, "msg": f"network: {e}"}


def cli_json(*args: str) -> dict | None:
    """跑 lark-cli 并解析 JSON。拿不到 JSON 返回 None —— 别让一次抽风炸掉整批。

    🔴 lark-cli 把**错误响应写到 stderr**，stdout 是空的。只读 stdout 会把
    「41050 no user authority」这种有明确错误码的失败，误报成「没返回 JSON」，
    于是「跨租户查不了」和「lark-cli 没登录」混成一个桶。所以两路都收。
    """
    try:
        p = subprocess.run(["lark-cli", *args, "--as", "user"],
                           capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for blob in (p.stdout, p.stderr):
        if not blob or not blob.strip():
            continue
        try:
            return json.loads(blob)
        except ValueError:
            continue
    return None


def cli_err(d: dict) -> str:
    e = d.get("error") or {}
    code = e.get("code")
    return f"[{code}] {str(e.get('message', ''))[:60]}" if code else str(e.get("message", ""))[:60]


def search_user(name: str) -> tuple[list[dict], str]:
    d = cli_json("contact", "+search-user", "--query", name)
    if d is None:
        return [], "通讯录搜索没返回任何输出（lark-cli 未登录？）"
    if not d.get("ok", True):
        return [], f"搜索失败：{cli_err(d)}"
    users = (d.get("data") or {}).get("users") or []
    exact = [u for u in users if u.get("localized_name") == name or u.get("name") == name]
    if not exact:
        return [], "通讯录里没有同名的人"
    return exact, ""


def admin_open_id_to_user_id(open_id: str) -> tuple[str, str]:
    d = cli_json("api", "GET", f"/open-apis/contact/v3/users/{open_id}",
                 "--params", '{"user_id_type":"open_id"}')
    if d is None:
        return "", "查 user_id 没返回任何输出"
    if not d.get("ok", True):
        err = cli_err(d)
        # 41050 no user authority = 对方在**另一个租户**里，本租户查不到它的 user_id。
        # 这不是「人找不到」，也不是「我没权限设置错了」，是跨租户的硬边界。
        if "41050" in err:
            return "", "跨租户用户，本租户查不到 user_id（is_cross_tenant）"
        return "", f"查 user_id 失败 {err}"
    data = d.get("data") or {}
    user = (data.get("data") or {}).get("user") or data.get("user") or {}
    uid = user.get("user_id")
    return (uid, "") if uid else ("", "查不到 user_id")


def user_id_to_bot_open_id(user_id: str, token: str) -> tuple[str, str, str]:
    d = feishu(f"/open-apis/contact/v3/users/{user_id}?user_id_type=user_id", token)
    if d.get("code") != 0:
        return "", "", f"bot 换 open_id 失败 [{d.get('code')}] {str(d.get('msg'))[:60]}"
    u = (d.get("data") or {}).get("user") or {}
    return u.get("open_id", ""), u.get("name", ""), ""


def post(path: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(OPEN + path, data=json.dumps(body).encode(), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"code": e.code, "msg": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"code": -2, "msg": f"network: {e}"}


def person_field_index(spec: str, token: str) -> tuple[dict[str, set[str]], str]:
    """读一张表的人员字段，建「姓名 → {本 app open_id}」索引。"""
    try:
        base, table, field = spec.split("/", 2)
    except ValueError:
        return {}, "--from-person-field 形如 BASE/TABLE/字段名"
    index: dict[str, set[str]] = {}
    page_token = ""
    for _ in range(100):
        q = {"page_size": "500", "user_id_type": "open_id"}
        if page_token:
            q["page_token"] = page_token
        d = post(f"/open-apis/bitable/v1/apps/{base}/tables/{table}/records/search?"
                 + urllib.parse.urlencode(q), token, {"field_names": [field]})
        if d.get("code") != 0:
            return {}, f"读参照表失败 [{d.get('code')}] {str(d.get('msg'))[:60]}"
        data = d.get("data") or {}
        for rec in data.get("items") or []:
            for p in (rec.get("fields") or {}).get(field) or []:
                nm = p.get("name")
                if nm and p.get("id"):
                    index.setdefault(nm, set()).add(p["id"])
        if not data.get("has_more"):
            return index, ""
        page_token = data.get("page_token") or ""
        if not page_token:
            break
    return {}, "参照表分页没有正常结束"


def pick_resolvable(ids: set[str], token: str) -> tuple[str, str]:
    """一个姓名对应多个 open_id 时，只认在本租户查得到的那一个。

    2026-10-10 实测：杜小畔 / 刘咏强 各有两个 id，其中一个是**跨租户同名**，
    查它返回 41050。恰好一个可查 ⇒ 采用；零个或多个 ⇒ 不猜，留给人工。
    """
    alive = []
    for oid in sorted(ids):
        d = feishu(f"/open-apis/contact/v3/users/{oid}?user_id_type=open_id", token)
        if d.get("code") == 0:
            alive.append(oid)
    if len(alive) == 1:
        return alive[0], ""
    if not alive:
        return "", f"{len(ids)} 个同名 id 在本租户全都查不到（都跨租户？）"
    return "", f"{len(alive)} 个同名 id 都可查，无法判断是哪一个"


def resolve(names: list[str], token: str, person_index: dict[str, set[str]] | None = None) -> dict:
    resolved: dict[str, str] = {}
    fail: list[list[str]] = []
    dup: list[list] = []
    cache: dict[str, str] = {}
    for name in names:
        name = name.strip()
        if not name:
            continue
        if name in cache:
            resolved[cache[name]] = name
            continue
        # 路径 A：参照表的人员字段 —— 它给的就是本 app 视角 open_id，不碰通讯录。
        if person_index and name in person_index:
            ids = person_index[name]
            if len(ids) == 1:
                oid = next(iter(ids))
                cache[name] = oid
                resolved[oid] = name
                continue
            dup.append([name, len(ids)])
            oid, err = pick_resolvable(ids, token)
            if oid:
                cache[name] = oid
                resolved[oid] = name
            else:
                fail.append([name, err])
            continue
        # 路径 B：通讯录搜索 → user_id → bot 换 open_id
        users, err = search_user(name)
        if err:
            fail.append([name, err])
            continue
        if len(users) > 1:
            # 同名不猜：发错人撤不回
            dup.append([name, len(users)])
            fail.append([name, f"通讯录有 {len(users)} 个同名，需人工指定"])
            continue
        uid, err = admin_open_id_to_user_id(users[0]["open_id"])
        if err:
            fail.append([name, err])
            continue
        oid, back_name, err = user_id_to_bot_open_id(uid, token)
        if err:
            fail.append([name, err])
            continue
        if back_name and back_name != name:
            # 回读姓名对不上 ⇒ 解析链某一环搭错了人，宁可失败
            fail.append([name, f"回读姓名不符：{back_name}"])
            continue
        cache[name] = oid
        resolved[oid] = name
    return {"resolved": resolved, "fail": fail, "dup": dup}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--names", help="逗号分隔的姓名")
    g.add_argument("--names-file", help="每行一个姓名")
    ap.add_argument("-o", "--out", default="", help="输出 JSON 文件（默认打到 stdout）")
    ap.add_argument("--from-person-field", metavar="BASE/TABLE/FIELD",
                    help="优先从一张表的**人员字段**按姓名交叉匹配（如 "
                         "DlT9bsrwMad12VsogEpcK9Ptncc/tblJT2s6Y6xjYj5A/姓名）。"
                         "人员字段给的就是本 app 视角 open_id，不走通讯录、不受跨租户限制。"
                         "一个姓名对应多个 id 时，只有恰好一个在本租户可查才采用，否则留给人工。")
    a = ap.parse_args()

    token_env = (os.environ.get("FEISHU_APP_ID"), os.environ.get("FEISHU_APP_SECRET"))
    if not all(token_env):
        print("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET", file=sys.stderr)
        return 2
    req = urllib.request.Request(OPEN + "/open-apis/auth/v3/tenant_access_token/internal",
                                 data=json.dumps({"app_id": token_env[0],
                                                  "app_secret": token_env[1]}).encode())
    req.add_header("Content-Type", "application/json")
    try:
        tok = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception as e:  # noqa: BLE001
        print(f"换 token 失败：{e}", file=sys.stderr)
        return 3
    if tok.get("code") != 0:
        print(f"换 token 失败：[{tok.get('code')}] {tok.get('msg')}", file=sys.stderr)
        return 3

    if a.names:
        names = a.names.split(",")
    else:
        with open(a.names_file, encoding="utf-8") as fh:
            names = fh.read().splitlines()

    token = tok["tenant_access_token"]
    person_index = None
    if a.from_person_field:
        person_index, err = person_field_index(a.from_person_field, token)
        if err:
            print(f"参照表读取失败：{err}", file=sys.stderr)
            return 3
        print(f"参照表人员字段索引：{len(person_index)} 个姓名", file=sys.stderr)

    rep = resolve(names, token, person_index)
    rep["input_count"] = len([n for n in names if n.strip()])
    blob = json.dumps(rep, ensure_ascii=False, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(blob)
        print(f"解析 {rep['input_count']} 个姓名 → 成功 {len(rep['resolved'])}，"
              f"失败 {len(rep['fail'])}（同名 {len(rep['dup'])}）→ {a.out}")
    else:
        print(blob)
    return 1 if rep["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
