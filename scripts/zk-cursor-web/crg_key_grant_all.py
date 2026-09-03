#!/usr/bin/env python3
"""crg_key_grant_all.py —— 把 14 个 cr-g 池名批量授权给**全部** cursor-* key（2026-09-03 用户指令）。
在 litellm-proxy pod 内跑（要 LITELLM_MASTER_KEY + localhost:4000）：
    kubectl -n litellm-product exec -i <proxy-pod> -- python3 - [--apply] < crg_key_grant_all.py < tokens
  tokens 经 stdin 之后的 argv? 不行 —— 这里用 env TOKENS_FILE 指向 pod 内文件（每行 `alias<TAB>token`，token 是 DB 里的 64 位 hash）。
规矩（同 crg_key_grant.py）：/key/update 的 models 整表覆盖 → 先 /key/info 读旧表、本地合并、整份写回；
写完 GET 回读逐名核对；改前所有 key 的旧 models 整份落一个备份 JSON（回滚 = 逐把写回）。
跳过：models 为空（= 全部可用，加名单反而收窄）。
"""
import json, os, sys, time, urllib.request, urllib.error

BASE = "http://localhost:4000"
MK = os.environ["LITELLM_MASTER_KEY"]
GRANT = [
    "cr-g-5.6", "cr-g-5.6-instant", "cr-g-5.6-mini", "cr-g-5.6-t-mini",
    "cr-g-5.6-pro", "cr-g-research", "cr-g-5.6-thinking", "cr-g-5.6-thinking-min",
    "cr-g-5.6-thinking-high", "cr-g-5.6-thinking-max",
    "cr-g-5.6-luna", "cr-g-5.6-luna-min", "cr-g-5.6-luna-high", "cr-g-5.6-luna-max",
]
APPLY = "--apply" in sys.argv
OUT = os.environ.get("BACKUP_OUT", "/tmp/crg_grant_all_backup.json")

def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + MK, "Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        return {"HTTP_ERROR": e.code, "body": e.read()[:300].decode("utf-8", "replace")}

rows = [l.rstrip("\n").split("\t") for l in open(os.environ["TOKENS_FILE"]) if "\t" in l]
print("待处理 key: %d  (%s)" % (len(rows), "APPLY" if APPLY else "dry-run"), flush=True)
backup, stats = [], {"ok": 0, "skip_null": 0, "skip_done": 0, "fail": 0}
t0 = time.time()
for i, (alias, tok) in enumerate(rows, 1):
    info = call("GET", "/key/info?key=" + tok)
    if "HTTP_ERROR" in info:
        stats["fail"] += 1; print("  ❌ %s info %s" % (alias, info["HTTP_ERROR"]), flush=True); continue
    old = (info.get("info") or {}).get("models")
    if not old:
        stats["skip_null"] += 1; print("  ⏭  %s models 为空(=全部可用),跳过" % alias, flush=True); continue
    missing = [m for m in GRANT if m not in old]
    if not missing:
        stats["skip_done"] += 1; continue
    backup.append({"key_alias": alias, "token_prefix": tok[:12], "models_before": old})
    if not APPLY:
        stats["ok"] += 1; continue
    new = list(old) + missing
    r = call("POST", "/key/update", {"key": tok, "models": new})
    if "HTTP_ERROR" in r:
        stats["fail"] += 1; print("  ❌ %s update %s %s" % (alias, r["HTTP_ERROR"], r["body"][:120]), flush=True); continue
    back = call("GET", "/key/info?key=" + tok)
    got = (back.get("info") or {}).get("models") or []
    if all(m in got for m in GRANT) and len(got) == len(new):
        stats["ok"] += 1
    else:
        stats["fail"] += 1; print("  ❌ %s 回读不符: %d 个, 缺 %s" % (alias, len(got), [m for m in GRANT if m not in got]), flush=True)
    if i % 50 == 0:
        print("  … %d/%d  %.0fs" % (i, len(rows), time.time() - t0), flush=True)

json.dump(backup, open(OUT, "w"), ensure_ascii=False)
print("备份写到 %s (%d 把)" % (OUT, len(backup)))
print("SUMMARY %s: 成功/将改 %d, 已全有 %d, 空跳过 %d, 失败 %d, 用时 %.0fs" % (
    "APPLY" if APPLY else "dry-run", stats["ok"], stats["skip_done"], stats["skip_null"], stats["fail"], time.time() - t0))
sys.exit(1 if stats["fail"] else 0)
