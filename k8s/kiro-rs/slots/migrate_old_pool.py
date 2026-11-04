"""Copy kiro accounts from the OLD instance (:30890) into the SLOTS instance (:30891), deduped by email.

Run on 198 as cltx:
    python3 migrate_old_pool.py scan                 # read-only: per-account balance on the OLD side
    python3 migrate_old_pool.py plan  [scan.tsv]     # what would be imported, and why each one is skipped
    python3 migrate_old_pool.py apply [scan.tsv] --limit N   # import (disabled) one by one, OK/STOP per account

This is a COPY, not a move: nothing on the old instance is ever modified or deleted. The old instance is
still serving (`carher-hangzhou`, `claude-code-buyitian`) and is the slots instance's LiteLLM fallback.

Hard rules, each one bought with a bug:
  🔴 Dedup on **lowercased email**, never on id. The two instances both number ids from 1 and **174 ids
     collide**; the 4 emails that exist on both sides already carry different ids (276→158 etc).
  🔴 Never send `id`. `POST /credentials` has no id field — the slots instance assigns the next one.
     Sending one risks overwriting a serving account.
  🔴 Accounts with a blank email (9 on the old side) are NEVER auto-imported: no email means no dedup key.
  🔴 `apiRegion` must be `us-east-1`; an exported `eu-central-1` makes the balance read 403
     (kiro-account-manager-ops skill).
  🔴 Import must end up DISABLED, and `POST /credentials` **silently ignores a `disabled` field**
     (10-31 closed loop on one account: sent disabled=true, readback said disabled=false ⇒ batching 283
     would have put them all in ENABLED and let reconcile pull 0-credit accounts onto live slots).
     ⇒ every create is followed by `POST /credentials/<id>/disabled {"disabled":true}` and the row is
     re-read; if it is still enabled the account is reported STOP-UNDISABLED and the batch halts.
  🔴 `apiRegion`/`authRegion` come back None for every account in this build (all 184) — region lives in
     config.json, not per credential. Do not "fix" that; the balance read works (id 184 = remaining 40.88,
     same as the old instance reported for old id 9).
  🔴 Skip accounts whose refresh token is revoked (`invalid_grant`, surfaced as 502 by the balance read):
     they import fine and then sit in the pool as fake spares.
  🔴 One account per POST, failures do not abort the batch, every account prints OK/STOP + reason.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

K = ["sudo", "k3s", "kubectl", "-n", "kiro-rs"]
OLD, NEW = "kiro-rs", "kiro-rs-slots"
OLD_URL, NEW_URL = "http://127.0.0.1:30890/api/admin", "http://127.0.0.1:30891/api/admin"
# fields the POST /credentials form sends; `id` is deliberately absent
COPY = ["email", "authMethod", "refreshToken", "clientId", "clientSecret",
        "authRegion", "machineId", "priority", "profileArn", "subscriptionTitle"]


def pod(lbl):
    return subprocess.run(K + ["get", "pod", "-l", "app=" + lbl, "-o",
                               "jsonpath={.items[0].metadata.name}"],
                          capture_output=True, text=True, check=True).stdout.strip()


def read_json(lbl, name):
    raw = subprocess.run(K + ["exec", pod(lbl), "-c", "kiro-rs", "--", "cat",
                              "/app/config/" + name], capture_output=True, text=True, check=True).stdout
    return json.loads(raw)


def admin_key(lbl):
    return read_json(lbl, "config.json")["adminApiKey"]


def api(base, key, method, path, body=None, timeout=90):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"x-api-key": key, "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:220]
    except Exception as e:
        return "ERR", "%s: %s" % (type(e).__name__, str(e)[:160])


def norm(e):
    return (e or "").strip().lower()


def candidates():
    """old-only accounts, keyed by email, plus the two rejected buckets."""
    old, new = read_json(OLD, "credentials.json"), read_json(NEW, "credentials.json")
    have = {norm(x.get("email")) for x in new} - {""}
    blank = [x for x in old if not norm(x.get("email"))]
    dup = [x for x in old if norm(x.get("email")) in have]
    cand = [x for x in old if norm(x.get("email")) and norm(x.get("email")) not in have]
    # an email duplicated inside the old list itself: keep the highest id (newest login)
    best = {}
    for x in cand:
        e = norm(x["email"])
        if e not in best or x["id"] > best[e]["id"]:
            best[e] = x
    return sorted(best.values(), key=lambda x: x["id"]), blank, dup, old, new


def cmd_scan(a):
    key = admin_key(OLD)
    cand, blank, dup, old, new = candidates()
    print("# old=%d new=%d  candidates=%d  blank_email=%d  already_on_new=%d"
          % (len(old), len(new), len(cand), len(blank), len(dup)), flush=True)
    print("# id\ttier\tauthMethod\thttp\tremaining\tusageLimit", flush=True)
    for x in cand:
        st, b = api(OLD_URL, key, "GET", "/credentials/%s/balance" % x["id"])
        if st == 200 and isinstance(b, dict):
            print("%s\t%s\t%s\t200\t%s\t%s" % (x["id"], x.get("subscriptionTitle"),
                                               x.get("authMethod"), b.get("remaining"), b.get("usageLimit")), flush=True)
        else:
            print("%s\t%s\t%s\t%s\t%s" % (x["id"], x.get("subscriptionTitle"),
                                          x.get("authMethod"), st, str(b)[:120]), flush=True)
        time.sleep(0.6)
    print("DONE", flush=True)
    return 0


def load_scan(path):
    """id -> (http, remaining). Missing file ⇒ empty, and then nothing is importable."""
    out = {}
    if not path or not os.path.exists(path):
        return out
    for line in open(path):
        line = line.rstrip("\n")
        if not line or line.startswith("#") or line.startswith("DONE") or line.startswith("candidates"):
            continue
        f = line.split("\t")
        if len(f) < 4:
            continue
        try:
            out[int(f[0])] = (f[3], float(f[4]) if len(f) > 4 and f[4] not in ("", "None") else None)
        except ValueError:
            continue
    return out


def decide(x, scan):
    """(import?, reason)"""
    st, rem = scan.get(x["id"], (None, None))
    if st is None:
        return False, "no scan row ⇒ 状态未知，先跑 scan"
    if str(st) != "200":
        return False, "凭据死/读不到额度 (http %s)" % st
    if x.get("authMethod") == "idc" and not (x.get("clientId") and x.get("clientSecret")):
        return False, "idc 缺 clientId/clientSecret"
    if x.get("authMethod") == "social" and not x.get("profileArn"):
        return False, "social 缺 profileArn"
    if not x.get("refreshToken"):
        return False, "没有 refreshToken"
    return True, "ok (remaining=%s，11-01 重置)" % rem


def build(x):
    body = {k: x[k] for k in COPY if x.get(k) not in (None, "")}
    body["apiRegion"] = "us-east-1"          # 🔴 eu-central-1 ⇒ balance 403
    body.setdefault("authRegion", "us-east-1")
    body["disabled"] = True                   # 🔴 进冷备，不自动上槽
    body["priority"] = 0
    body.pop("subscriptionTitle", None)       # 由新池自己探，别带旧值
    return body


def cmd_plan(a, apply=False):
    cand, blank, dup, old, new = candidates()
    scan = load_scan(a.scan)
    nkey = admin_key(NEW)
    before = read_json(NEW, "credentials.json")
    bmax = max(x["id"] for x in before)
    print("old=%d  new=%d (max id %d)  candidates=%d  blank_email=%d  already_on_new=%d  scan_rows=%d"
          % (len(old), len(new), bmax, len(cand), len(blank), len(dup), len(scan)))
    for x in dup:
        print("SKIP 已在新池  %s (old id %s)" % (x.get("email"), x["id"]))
    for x in blank:
        print("SKIP 邮箱为空  old id %s tier=%s ⇒ 无法判重，人工确认" % (x["id"], x.get("subscriptionTitle")))
    todo = []
    for x in cand:
        ok, why = decide(x, scan)
        if ok:
            todo.append(x)
        else:
            print("SKIP %-28s old id %-4s %s" % (x.get("email"), x["id"], why))
    print("\n可导入 %d 个（全部以 disabled 进冷备，不上槽）" % len(todo))
    if a.limit:
        todo = todo[:a.limit]
        print("--limit %d ⇒ 本次只导 %d 个" % (a.limit, len(todo)))
    if not apply:
        for x in todo[:10]:
            print("  WOULD ADD %-30s %-8s %s" % (x.get("email"), x.get("authMethod"), x.get("subscriptionTitle")))
        if len(todo) > 10:
            print("  … 共 %d 个" % len(todo))
        print("\ndry-run，没有写入。apply 才会导入。")
        return 0

    bk = os.path.expanduser("~/kiro-px-egress/slots-credentials-before-migrate-%s.json"
                            % time.strftime("%Y%m%dT%H%M%S"))
    json.dump(before, open(bk, "w"), ensure_ascii=False, indent=1)
    os.chmod(bk, 0o600)
    print("backup %s (%d 个原有号)" % (bk, len(before)))

    added, failed = [], []
    for x in todo:
        st, resp = api(NEW_URL, nkey, "POST", "/credentials", build(x))
        if str(st) not in ("200", "201"):
            failed.append((x, st, resp))
            print("STOP %-30s old id %-4s http %s %s" % (x.get("email"), x["id"], st, str(resp)[:110]))
            time.sleep(0.8)
            continue
        nid = resp.get("credentialId") if isinstance(resp, dict) else None
        if nid is None:
            failed.append((x, st, "no credentialId in response"))
            print("STOP %-30s old id %-4s 建好了但没回 id ⇒ 停批，人工核对" % (x.get("email"), x["id"]))
            break
        # 🔴 create ignores `disabled` ⇒ disable explicitly, then PROVE it took
        dst, dresp = api(NEW_URL, nkey, "POST", "/credentials/%s/disabled" % nid, {"disabled": True})
        row = next((r for r in read_json(NEW, "credentials.json") if r["id"] == nid), None)
        if not (row and row.get("disabled")):
            failed.append((x, st, "still enabled after disable (http %s)" % dst))
            print("STOP-UNDISABLED %-24s new id %-4s 禁用没生效 ⇒ 停批（它会被 reconcile 拉上槽）"
                  % (x.get("email"), nid))
            break
        added.append((x, nid))
        print("OK   %-30s old id %-4s → new id %-4s disabled=True" % (x.get("email"), x["id"], nid))
        time.sleep(0.8)

    # 🔴 serving accounts rotate accessToken/expiresAt by themselves (~hourly). A raw byte compare
    # reads that as "the import changed them" (10-31: ids 155/164/165). Compare identity/credentials/
    # enabled state and report rotation separately.
    VOLATILE = {"accessToken", "expiresAt"}
    after = read_json(NEW, "credentials.json")
    by_id = {x["id"]: x for x in after}
    touched = []
    for o in before:
        n = by_id.get(o["id"])
        if n is None:
            touched.append((o["id"], "消失了")); continue
        diff = {k for k in set(o) | set(n) if o.get(k) != n.get(k)} - VOLATILE
        if diff:
            touched.append((o["id"], sorted(diff)))
    untouched = not touched
    for i, what in touched:
        print("  🔴 原有 id %s 被改了: %s" % (i, what))
    new_rows = [x for x in after if x["id"] not in {o["id"] for o in before}]
    n_dis = sum(1 for x in new_rows if x.get("disabled"))
    print("\n原有 %d 个号逐字节未变: %s" % (len(before), untouched))
    print("新增 %d 行，id %s" % (len(new_rows), sorted(x["id"] for x in new_rows)[:12]))
    print("新增里 disabled=true 的: %d/%d%s" % (n_dis, len(new_rows),
                                            "  🔴 有号是启用状态，会被拉上槽！" if n_dis != len(new_rows) else ""))
    print("在役槽的号有没有变: 见下")
    print("导入 OK %d / STOP %d" % (len(added), len(failed)))
    for x, st, why in failed:
        print("  FAILED %-30s %s" % (x.get("email"), why if isinstance(why, str) else st))
    return 0 if untouched and not failed and n_dis == len(new_rows) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["scan", "plan", "apply"])
    ap.add_argument("scan", nargs="?")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.cmd == "scan":
        return cmd_scan(a)
    return cmd_plan(a, apply=(a.cmd == "apply"))


if __name__ == "__main__":
    sys.exit(main())
