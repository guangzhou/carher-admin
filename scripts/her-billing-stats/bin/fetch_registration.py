#!/usr/bin/env python3
"""
Fetch the 'her 注册表' from Lark Wiki-hosted Base and build uid -> person_id map.
Writes /tmp/<out>/reg.json (raw) and /tmp/<out>/uid_to_person.json (map).

Registration table: Lark wiki node DFqqwIMsIiLUWdkTfs4c1VqLnnh -> bitable obj_token,
                    table tblcvJPRIFV91yHy
Fields used: ID (uid), 姓名 (person object with open_id)

Dependencies: lark-cli on PATH, authenticated as a user with base read access.
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

WIKI_TOKEN = "DFqqwIMsIiLUWdkTfs4c1VqLnnh"
REG_TABLE_ID = "tblcvJPRIFV91yHy"


def lark_api(method: str, url: str, params: dict | None = None, data: dict | None = None, as_identity: str = "bot"):
    cmd = ["lark-cli", "api", method, url, "--as", as_identity]
    if params is not None:
        cmd += ["--params", json.dumps(params)]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        return json.loads(p.stdout)
    except Exception:
        raise RuntimeError(f"lark-cli stdout not JSON: {p.stdout[:400]} stderr={p.stderr[:400]}")


def resolve_wiki_token(wiki_token: str, as_identity: str) -> str:
    url = "/open-apis/wiki/v2/spaces/get_node"
    resp = lark_api("GET", url, params={"token": wiki_token}, as_identity=as_identity)
    if resp.get("code") != 0:
        raise RuntimeError(f"wiki resolve failed: {resp}")
    node = resp["data"]["node"]
    if node["obj_type"] != "bitable":
        raise RuntimeError(f"wiki node is {node['obj_type']}, not bitable")
    return node["obj_token"]


def fetch_all_records(base_token: str, table_id: str, as_identity: str) -> list[dict]:
    url = f"/open-apis/bitable/v1/apps/{base_token}/tables/{table_id}/records"
    out: list[dict] = []
    token = ""
    while True:
        params = {"page_size": "500"}
        if token:
            params["page_token"] = token
        resp = lark_api("GET", url, params=params, as_identity=as_identity)
        if resp.get("code") != 0:
            raise RuntimeError(f"fetch records failed: {resp}")
        out.extend(resp["data"]["items"])
        if not resp["data"].get("has_more"):
            break
        token = resp["data"].get("page_token", "")
    return out


def build_uid_to_person(items: list[dict]) -> dict[str, str]:
    m: dict[str, str] = {}
    for it in items:
        f = it.get("fields", {})
        uid = f.get("ID")
        if isinstance(uid, list):
            u0 = uid[0] if uid else {}
            uid = u0.get("text", "") if isinstance(u0, dict) else str(u0)
        uid = str(uid or "").strip()
        if not uid:
            continue
        person = f.get("姓名")
        if not isinstance(person, list) or not person:
            continue
        p0 = person[0]
        pid = p0.get("id") or p0.get("open_id")
        if pid:
            m[uid] = pid
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--as", dest="identity", default="bot", choices=["bot", "user"])
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    base_token = resolve_wiki_token(WIKI_TOKEN, args.identity)
    print(f"resolved base_token={base_token}", file=sys.stderr)

    items = fetch_all_records(base_token, REG_TABLE_ID, args.identity)
    print(f"fetched {len(items)} registration rows", file=sys.stderr)
    (out / "reg.json").write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2))

    u2p = build_uid_to_person(items)
    print(f"uid_to_person entries: {len(u2p)}", file=sys.stderr)
    (out / "uid_to_person.json").write_text(json.dumps(u2p, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
