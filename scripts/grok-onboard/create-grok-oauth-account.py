#!/usr/bin/env python3
"""create-grok-oauth-account.py — 手里已有 xAI 设备码 OAuth bundle 时，直接建 sub2api 账号。

在 198 上跑（需要 sudo kubectl 读 ADMIN_PASSWORD / psql 回读）。
这是 skill `sub2api-grok-ops` §C1→§C3 那条路的收尾一步：
§C0 的 `sso-to-oauth` 用不了时，凭据只能自己在 225 WARP 上抓（首选
`xai_sso_device_oauth.py`，纯 HTTP，不用 Playwright），再由本脚本写进库。

🔴 为什么那条主路不通（2026-09-28 定因，别再归因到 sub2api 版本）：
`accounts.x.ai` 按**出口 IP** 挡机房。阳性对照，同一个 sso、同一分钟——
sub2api pod（172.235.204.67）打 `accounts.x.ai/api/auth/session` 得 403+CF HTML；
225 WARP（104.28.238.115）打**同一个 URL** 得 200 authenticated；
而 `auth.x.ai/oauth2/device/code` **两侧都 200**。只有出口这一个变量在动。

用法:
    python3 create-grok-oauth-account.py .newacct-enkk.json [.newacct-mwhu.json ...]

payload 文件形状（由 225 上的抓取流程生成，0600）:
    {"name": "<email>", "platform": "grok", "type": "oauth",
     "credentials": {access_token, refresh_token, token_type, scope, expires_at,
                     client_id, base_url, email, sub, team_id,
                     subscription_tier, _token_version},
     "concurrency": 200, "priority": 1, "rate_multiplier": 1,
     "auto_pause_on_expired": true, "group_ids": [7], "status": "active"}

做的事:
  1. 拒重复: 同 name 已有未删账号 ⇒ 跳过（⛔ 别建第二条腿指向同一个 xAI 号）
  2. 补 `model_mapping`: 从池子里一条健康腿抄（缺它新腿认不出 grok-4.6 这些名）
  3. POST /api/v1/admin/accounts
  4. psql 回读 accounts + account_groups，确认出现 `<acct>|7|1`
⛔ 全程不打印任何 token。
"""
import json, os, subprocess, sys

REMOTE_DIR = os.path.dirname(os.path.abspath(__file__))
PG_LABEL = "app=sub2api-postgres"
TEMPLATE_ACCT = os.environ.get("MODEL_MAP_FROM", "61")
GROUP_ID = 7


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def pg_pod():
    rc, out, err = sh(f"sudo kubectl -n litellm-dev get pod -l {PG_LABEL} "
                      "--field-selector=status.phase=Running "
                      "-o jsonpath='{.items[0].metadata.name}'")
    assert rc == 0 and out, (rc, out, err)
    return out


def psql(pod, sql, tuples=True):
    flags = "-t -A" if tuples else ""
    q = sql.replace('"', '\\"')
    rc, out, err = sh(f'sudo kubectl -n litellm-dev exec {pod} -- '
                      f'psql -U sub2api -d sub2api {flags} -c "{q}"')
    assert rc == 0, (rc, out, err)
    return out


def main():
    paths = sys.argv[1:]
    if not paths:
        print(__doc__)
        sys.exit(2)

    pw = os.environ.get("S2A_PW_FILE")
    if not pw:
        # NOT /tmp: 198 runs fs.protected_regular=2, which forbids root from
        # O_CREAT-opening an EXISTING file in a sticky world-writable dir that
        # another uid owns.  A /tmp/.s2apw-create left behind by cltx on a
        # previous run therefore fails as "Permission denied" under sudo, which
        # reads exactly like a broken sudo and is not one -- writing a *new*
        # /tmp file from the same root shell succeeds.  Keeping the file beside
        # the script sidesteps the whole class.
        pw = os.path.join(REMOTE_DIR, ".s2apw-create")
        rc, out, err = sh("umask 077 && sudo kubectl -n litellm-dev get secret "
                          "sub2api-secrets -o jsonpath='{.data.ADMIN_PASSWORD}' "
                          f"| base64 -d > {pw} && chmod 600 {pw}")
        assert rc == 0, (rc, err)
        os.environ["S2A_PW_FILE"] = pw

    helper = os.path.join(REMOTE_DIR, "sub2api_admin.py")
    g = {}
    exec(open(helper).read().rsplit("if __name__", 1)[0], g)
    call, TOK = g["call"], g["TOK"]

    pod = pg_pod()

    # Assert the template BEFORE trusting it.  A leg with no model_mapping (acct
    # 18 is one) silently yields {} here, and {} creates legs that cannot answer
    # to grok-4.6 etc -- a blind spot that no readback below would show.
    # jsonb_typeof, not jsonb_array_length: model_mapping is an OBJECT, and
    # array_length on it errors with "cannot get array length of a non-array".
    tinfo = psql(pod, "select coalesce(jsonb_typeof(credentials->'model_mapping'),"
                      "'absent'), coalesce(length(credentials->>'model_mapping'),0),"
                      f" deleted_at is null from accounts where id={TEMPLATE_ACCT};")
    parts = [x.strip() for x in tinfo.split("|")] if tinfo else []
    if len(parts) != 3:
        sys.exit(f"template acct {TEMPLATE_ACCT} not found in accounts")
    ttype, tlen, talive = parts[0], int(parts[1] or 0), parts[2] == "t"
    if ttype != "object" or tlen == 0:
        sys.exit(f"template acct {TEMPLATE_ACCT}: model_mapping is {ttype} "
                 f"(len={tlen}) -- pick a leg that HAS one (MODEL_MAP_FROM=<id>); "
                 f"judge with jsonb_typeof(...)='object'. Copying an empty "
                 f"mapping would leave the new legs unable to serve grok-4.6.")
    if not talive:
        print(f"[warn] template acct {TEMPLATE_ACCT} is soft-deleted "
              f"(deleted_at set); its mapping may be stale", flush=True)

    mm = psql(pod, f"select credentials->'model_mapping' from accounts "
                   f"where id={TEMPLATE_ACCT};")
    model_mapping = json.loads(mm) if mm and mm != "" else {}
    if not model_mapping:
        sys.exit(f"template acct {TEMPLATE_ACCT} read back an empty mapping "
                 f"despite typeof=object -- refusing to create blind legs")
    print(f"[model_mapping] {len(model_mapping)} names, {tlen}B "
          f"(from acct {TEMPLATE_ACCT})", flush=True)

    existing = psql(pod, "select lower(name) from accounts "
                         "where platform='grok' and deleted_at is null;")
    have = {l.strip() for l in existing.splitlines() if l.strip()}

    created = []
    for p in paths:
        body = json.load(open(p))
        name = body["name"]
        if name.lower() in have:
            print(f"SKIP {name}: already an account", flush=True)
            continue
        body["credentials"].setdefault("model_mapping", model_mapping)
        st, j = call("POST", "/api/v1/admin/accounts", body, TOK)
        if st not in (200, 201):
            print(f"FAIL {name}: http={st} {json.dumps(j)[:300]}", flush=True)
            continue
        aid = (j.get("data") or {}).get("id")
        print(f"OK {name}: http={st} id={aid}", flush=True)
        created.append((aid, name))

    if not created:
        print("\nnothing created")
        sys.exit(1)

    ids = ",".join(str(a) for a, _ in created)
    print("\n=== readback: accounts ===")
    print(psql(pod, f"select id,name,platform,type,status,schedulable,concurrency,"
                    f"priority,credentials->>'subscription_tier' tier "
                    f"from accounts where id in ({ids}) order by id;", tuples=False))
    print("=== readback: account_groups (expect <acct>|%s|...) ===" % GROUP_ID)
    print(psql(pod, f"select * from account_groups where account_id in ({ids});",
               tuples=False))

    # The mapping is the thing most likely to be silently missing, so compare it
    # against the template byte-for-byte rather than assuming the POST kept it.
    print(f"=== readback: model_mapping vs template acct {TEMPLATE_ACCT} ===")
    print(psql(pod, f"select id, name, jsonb_typeof(credentials->'model_mapping') "
                    f"mm_type, length(credentials->>'model_mapping') mm_len, "
                    f"length(credentials->>'model_mapping') = {tlen} matches_template "
                    f"from accounts where id in ({ids}) order by id;", tuples=False))

    bad_mm = psql(pod, f"select count(*) from accounts where id in ({ids}) and "
                       f"(jsonb_typeof(credentials->'model_mapping') is distinct "
                       f"from 'object' or length(credentials->>'model_mapping') "
                       f"is distinct from {tlen});")
    if bad_mm.strip() not in ("0", ""):
        print(f"\n!! {bad_mm.strip()} leg(s) did NOT get the template mapping -- "
              f"fix before calling this done")
        sys.exit(1)

    grp = psql(pod, f"select count(*) from account_groups where account_id in "
                    f"({ids}) and group_id={GROUP_ID};")
    if grp.strip() != str(len(created)):
        print(f"\n!! group {GROUP_ID} rows: {grp.strip()}/{len(created)} -- "
              f"an unbound leg is never offered a request")
        sys.exit(1)

    print(f"\n{len(created)}/{len(created)} created, bound to group {GROUP_ID}, "
          f"mapping matches template.")
    print("⚠️  This is NOT proof they serve.  `schedulable=t` is a bad ruler; "
          "only usage_logs rows are.\n"
          "    Next: sub2api-grok-onboard.py regress --minutes 15, then health, "
          "and compare the\n"
          "    'N of M legs actually served traffic' line against the baseline "
          "you took BEFORE this run.")


if __name__ == "__main__":
    main()
