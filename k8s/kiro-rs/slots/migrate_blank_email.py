"""Import the OLD pool's blank-email accounts into the slots instance, by explicit id list.

  python3 migrate_blank_email.py plan
  python3 migrate_blank_email.py apply

The main migration (migrate_old_pool.py) refuses these on purpose: no email = no dedup key. They are
still worth having — 10-31 balance read says 8 of the 9 authenticate fine, including two KIRO PRO MAX
(5000 limit each). So they are handled here, deliberately and by id, instead of loosening that rule.

Why dedup is safe here: every one of the 183 pre-existing accounts on the slots side has a non-empty
email (verified 10-31), so a blank-email account cannot be a duplicate of one of them. To stay safe if
that ever changes, this script re-checks that invariant and STOPs if the new side has any blank email.

Each imported row gets `email = "old#<id>@no-email.invalid"` so it is identifiable and the pool keeps a
unique dedup key; the real account is traceable by machineId, which is preserved. 🔴 A later run of
migrate_old_pool.py will see these synthetic emails and will NOT re-import them.

Same gotchas as the main script:
  🔴 `POST /credentials` ignores `disabled` ⇒ follow with /disabled and re-read, or they get pulled
     onto a live slot by reconcile.
  🔴 Never send `id` (174 ids collide across the two instances).
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/tmp")
K = ["sudo", "k3s", "kubectl", "-n", "kiro-rs"]
OLD, NEW = "kiro-rs", "kiro-rs-slots"
OLD_URL, NEW_URL = "http://127.0.0.1:30890/api/admin", "http://127.0.0.1:30891/api/admin"
COPY = ["authMethod", "refreshToken", "clientId", "clientSecret", "authRegion", "machineId", "profileArn"]


def pod(lbl):
    return subprocess.run(K + ["get", "pod", "-l", "app=" + lbl, "-o",
                               "jsonpath={.items[0].metadata.name}"],
                          capture_output=True, text=True, check=True).stdout.strip()


def read_json(lbl, name):
    raw = subprocess.run(K + ["exec", pod(lbl), "-c", "kiro-rs", "--", "cat", "/app/config/" + name],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(raw)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["plan", "apply"])
    a = ap.parse_args()

    okey, nkey = read_json(OLD, "config.json")["adminApiKey"], read_json(NEW, "config.json")["adminApiKey"]
    old, new = read_json(OLD, "credentials.json"), read_json(NEW, "credentials.json")

    blank_new = [x for x in new if not (x.get("email") or "").strip()]
    if blank_new:
        print("STOP: 新池里已经有 %d 个空邮箱号，按 id 判重的前提不成立 ⇒ 人工处理" % len(blank_new))
        return 1
    have_machine = {(x.get("machineId") or "") for x in new if x.get("machineId")}

    blanks = [x for x in old if not (x.get("email") or "").strip()]
    print("老池空邮箱号: %d  新池现有: %d（全部非空邮箱，判重前提成立）" % (len(blanks), len(new)))

    todo = []
    for x in blanks:
        st, b = api(OLD_URL, okey, "GET", "/credentials/%s/balance" % x["id"])
        alive = str(st) == "200" and isinstance(b, dict)
        tier = x.get("subscriptionTitle")
        if not alive:
            print("SKIP old id %-4s %-14s 凭据死 (http %s)" % (x["id"], tier, st))
            continue
        if x.get("machineId") and x["machineId"] in have_machine:
            print("SKIP old id %-4s %-14s machineId 已在新池 ⇒ 疑似重复" % (x["id"], tier))
            continue
        if not x.get("refreshToken"):
            print("SKIP old id %-4s %-14s 没有 refreshToken" % (x["id"], tier))
            continue
        print("TODO old id %-4s %-14s %-7s limit=%s remaining=%s（11-01 重置）"
              % (x["id"], tier, x.get("authMethod"), b.get("usageLimit"), b.get("remaining")))
        todo.append(x)
        time.sleep(0.5)

    print("\n可导入 %d 个" % len(todo))
    if a.cmd == "plan":
        print("dry-run，没有写入。")
        return 0

    before = read_json(NEW, "credentials.json")
    added, failed = [], []
    for x in todo:
        body = {k: x[k] for k in COPY if x.get(k) not in (None, "")}
        body["email"] = "old#%d@no-email.invalid" % x["id"]
        body["priority"] = 0
        st, resp = api(NEW_URL, nkey, "POST", "/credentials", body)
        if str(st) not in ("200", "201") or not isinstance(resp, dict) or resp.get("credentialId") is None:
            failed.append((x["id"], st, resp))
            print("STOP old id %-4s http %s %s" % (x["id"], st, str(resp)[:110]))
            continue
        nid = resp["credentialId"]
        api(NEW_URL, nkey, "POST", "/credentials/%s/disabled" % nid, {"disabled": True})
        row = next((r for r in read_json(NEW, "credentials.json") if r["id"] == nid), None)
        if not (row and row.get("disabled")):
            failed.append((x["id"], st, "still enabled after disable"))
            print("STOP-UNDISABLED old id %-4s new id %-4s 禁用没生效 ⇒ 停批" % (x["id"], nid))
            break
        added.append((x["id"], nid))
        print("OK   old id %-4s → new id %-4s %-14s disabled=True" % (x["id"], nid, x.get("subscriptionTitle")))
        time.sleep(0.8)

    # 🔴 "逐字节未变" is too strict: serving accounts rotate accessToken/expiresAt on their own roughly
    # hourly (10-31: ids 155/164/165 moved mid-run and the raw compare read as "I changed them").
    # What must not change is identity, credentials and enabled state — compare those, and report
    # token rotation separately instead of as a failure.
    VOLATILE = {"accessToken", "expiresAt"}
    after = read_json(NEW, "credentials.json")
    by_id = {r["id"]: r for r in after}
    bad, rotated = [], []
    for o in before:
        n = by_id.get(o["id"])
        if n is None:
            bad.append((o["id"], "消失了"))
            continue
        diff = {k for k in set(o) | set(n) if o.get(k) != n.get(k)}
        if diff - VOLATILE:
            bad.append((o["id"], sorted(diff - VOLATILE)))
        elif diff:
            rotated.append(o["id"])
    untouched = not bad
    print("\n原有 %d 个号：身份/凭据/启用状态未被改动: %s" % (len(before), untouched))
    if rotated:
        print("  （%d 个在役号自己轮换了 accessToken/expiresAt，正常）" % len(rotated))
    for i, what in bad:
        print("  🔴 id %s 被改了: %s" % (i, what))
    print("导入 OK %d / STOP %d" % (len(added), len(failed)))
    for i, st, why in failed:
        print("  FAILED old id %s: %s" % (i, why if isinstance(why, str) else st))
    return 0 if untouched and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
