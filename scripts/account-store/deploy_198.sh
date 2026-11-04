#!/usr/bin/env bash
# 账户库部署到 198 k3s(节点 aiyjy-vllm-standby = 10.68.10.37)。幂等,可重跑。设计见 k8s/account-store.yaml 头注释。
#
#   37   /Data/account-store/            库目录(挂载盘 vdb),0700 归 10110
#        /etc/account-store/kek.bin      主密钥(root 盘),0400 归 10110;已存在就沿用,绝不覆盖
#   198  /etc/account-store/kek.escrow   主密钥托管副本(root 0400,和备份不在一块盘);37 整机丢了靠它解备份
#        /etc/account-store/clients/*.token   各调用方 token 明文(root 0600);k8s Secret 里只放 sha256
#        /Data/backup/account-store/     每日备份落点(CronJob,只有密文)
#        /usr/local/sbin/account-store-reveal  管理员取 key(带 --reason,走 HTTP,审计 actor=sudo 的人)
#
# 用法: bash scripts/account-store/deploy_198.sh            部署/更新服务(不迁移)
#       MIGRATE=dry bash scripts/account-store/deploy_198.sh   + 飞书全量 dry-run(只读飞书,不写库)
#       MIGRATE=apply bash scripts/account-store/deploy_198.sh + 真写库(单事务,对账不过整体回滚)
# 🔴 飞书表只读(records/search),本脚本不改、不删、不清空任何列。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
MIGRATE="${MIGRATE-}"
case "$MIGRATE" in ""|dry|apply) ;; *) echo "MIGRATE 只能是 空/dry/apply" >&2; exit 1 ;; esac
J=scripts/jms
NODE_ASSET=AIYJY-vllm-standby
H198=AIYJY-litellm

# 0) 本地门禁
PY="${PYTHON:-.work/ai-portal/account-store/.venv/bin/python}"; [ -x "$PY" ] || PY=python3
"$PY" -m pytest scripts/account-store -q -p no:cacheprovider

STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
cp scripts/account-store/{account_store.py,server.py,migrate_from_lark.py} k8s/account-store.yaml "$STAGE/"

# 1) 37:目录 + 主密钥(只出指纹)
cat > "$STAGE/node.sh" <<'NODE'
set -euo pipefail
S="sudo -n"
$S install -d -m 0700 /Data/account-store && $S chown 10110:10110 /Data/account-store
$S install -d -m 0700 /etc/account-store && $S chown 10110:10110 /etc/account-store
if ! $S test -f /etc/account-store/kek.bin; then
  $S python3 -c '
import os, secrets
fd = os.open("/etc/account-store/kek.bin", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
os.write(fd, secrets.token_bytes(32)); os.fsync(fd); os.close(fd)
os.chown("/etc/account-store/kek.bin", 10110, 10110)'
  echo "kek: 新生成"
else
  echo "kek: 沿用已有"
fi
$S stat -c '%a %u:%g %s' /etc/account-store/kek.bin
$S python3 -c 'import hashlib;print("kek_id=kek-"+hashlib.sha256(b"account-store kek id|"+open("/etc/account-store/kek.bin","rb").read()).hexdigest()[:12])'
findmnt -n -o SOURCE,FSTYPE --target /Data/account-store
NODE
$J scp "$STAGE/node.sh" $NODE_ASSET:/tmp/acct-node.sh
$J ssh $NODE_ASSET 'bash /tmp/acct-node.sh; rc=$?; rm -f /tmp/acct-node.sh; exit $rc'

# 托管副本:37 → 198(不经过本机落盘;base64 只在管道里)
if ! $J ssh $H198 'sudo -n test -s /etc/account-store/kek.escrow' >/dev/null 2>&1; then
  $J ssh $NODE_ASSET 'sudo -n base64 -w0 /etc/account-store/kek.bin' | tr -d '\r\n' \
    | $J ssh $H198 'sudo -n install -d -m 0700 /etc/account-store && umask 077 && sudo -n sh -c "base64 -d > /etc/account-store/kek.escrow.part && test \$(stat -c %s /etc/account-store/kek.escrow.part) = 32 && chmod 0400 /etc/account-store/kek.escrow.part && mv /etc/account-store/kek.escrow.part /etc/account-store/kek.escrow"'
fi

# 2) 198:token、ConfigMap、apply、验收
COPYFILE_DISABLE=1 tar -C "$STAGE" -czf "$STAGE/as.tgz" account_store.py server.py migrate_from_lark.py account-store.yaml
$J scp "$STAGE/as.tgz" $H198:/tmp/as.tgz
cat > "$STAGE/remote.sh" <<'REMOTE'
set -euo pipefail
S="sudo -n"; K="sudo -n kubectl"
MIGRATE="$1"
W=$(mktemp -d); trap 'rm -rf "$W" /tmp/as.tgz' EXIT
tar -C "$W" -xzf /tmp/as.tgz
python3 -m py_compile "$W/account_store.py" "$W/server.py" "$W/migrate_from_lark.py"
$S python3 -c 'import hashlib;print("escrow kek_id=kek-"+hashlib.sha256(b"account-store kek id|"+open("/etc/account-store/kek.escrow","rb").read()).hexdigest()[:12])'
$S install -d -m 0700 /Data/backup/account-store && $S chown 10110:10110 /Data/backup/account-store
$S install -d -m 0700 /etc/account-store/clients
# token:有就沿用(换了 = 门户等调用方失效);没有才生成。明文只在 198 root 0600 文件里
for c in portal admin backup report; do
  $S test -s /etc/account-store/clients/$c.token || \
    $S sh -c "umask 077; python3 -c 'import secrets;print(\"ast_\"+secrets.token_urlsafe(32))' > /etc/account-store/clients/$c.token"
done
$S python3 - > "$W/tokens.json" <<'PY'
import hashlib, json
scopes = {"portal": ["read", "reveal:self"], "admin": ["read", "reveal:any", "backup"],
          "backup": ["backup"], "report": ["read"]}
out = {c: {"sha256": hashlib.sha256(open(f"/etc/account-store/clients/{c}.token").read().strip().encode()).hexdigest(),
           "scopes": s} for c, s in scopes.items()}
print(json.dumps({"clients": out}))
PY
$K create namespace account-store --dry-run=client -o yaml | $K apply -f - >/dev/null
$K -n account-store create secret generic account-store-tokens --from-file=tokens.json="$W/tokens.json" \
   --dry-run=client -o yaml | $K apply -f - >/dev/null
$S sh -c "printf %s \"\$(cat /etc/account-store/clients/backup.token)\" > $W/token"
$K -n account-store create secret generic account-store-backup-token --from-file=token="$W/token" \
   --dry-run=client -o yaml | $K apply -f - >/dev/null
$S rm -f "$W/token" "$W/tokens.json"
$K -n account-store create configmap account-store-src --from-file="$W/account_store.py" --from-file="$W/server.py" \
   --from-file="$W/migrate_from_lark.py" --dry-run=client -o yaml | $K apply -f - >/dev/null
OLD_CM=$($K -n account-store get deploy account-store -o jsonpath='{.spec.template.metadata.annotations.account-store/src-sha}' 2>/dev/null || true)
NEW_CM=$(cat "$W/account_store.py" "$W/server.py" "$W/migrate_from_lark.py" | sha256sum | cut -c1-16)
$K apply -f "$W/account-store.yaml"
[ "$OLD_CM" = "$NEW_CM" ] || $K -n account-store patch deploy account-store --type merge \
   -p "{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"account-store/src-sha\":\"$NEW_CM\"}}}}}"
$K -n account-store rollout status deploy/account-store --timeout=600s
$K -n account-store get pods -o wide
IP=$($K -n account-store get svc account-store -o jsonpath='{.spec.clusterIP}')
for i in $(seq 1 20); do curl -fsS -m 3 "http://$IP/healthz" >/dev/null 2>&1 && break; sleep 2; done
echo "healthz(从 198 宿主机): $(curl -sS -m 5 "http://$IP/healthz")"
RT=$($S cat /etc/account-store/clients/report.token)
echo "401(无 token): $(curl -s -o /dev/null -w %{http_code} -m 5 "http://$IP/v1/stats")"
echo "stats: $(curl -sS -m 10 -H "Authorization: Bearer $RT" "http://$IP/v1/stats")"
unset RT

# 管理员取 key 的入口
$S tee /usr/local/sbin/account-store-reveal >/dev/null <<'CLI'
#!/usr/bin/env python3
"""sudo account-store-reveal --account-id N [--kind api_key] --reason "为什么取"   (每次都进审计,actor=sudo 的人)"""
import argparse, json, os, subprocess, sys, urllib.request
ap = argparse.ArgumentParser(); ap.add_argument("--account-id", type=int, required=True)
ap.add_argument("--kind", default="api_key"); ap.add_argument("--reason", required=True)
a = ap.parse_args()
actor = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"
ip = subprocess.check_output(["kubectl", "-n", "account-store", "get", "svc", "account-store", "-o",
                              "jsonpath={.spec.clusterIP}"], text=True).strip()
tok = open("/etc/account-store/clients/admin.token").read().strip()
req = urllib.request.Request(f"http://{ip}/v1/accounts/{a.account_id}/reveal", method="POST",
                             data=json.dumps({"kind": a.kind, "reason": a.reason, "actor": actor + "@198"}).encode(),
                             headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
try:
    print(json.loads(urllib.request.urlopen(req, timeout=10).read())["key"])
except urllib.error.HTTPError as e:
    sys.exit(f"{e.code} {e.read().decode()}")
CLI
$S chmod 0700 /usr/local/sbin/account-store-reveal

# 3) 迁移(可选):飞书只读 → 库。在 pod 里跑(只有它能开库和主密钥);凭据和网关 hash 只落 pod 的内存盘
if [ -n "$MIGRATE" ]; then
  POD=$($K -n account-store get pod -l app=account-store -o jsonpath='{.items[0].metadata.name}')
  # 飞书凭据:carher-236(门户同一个后端身份,能搜 key 表),每次现读,核对 app id
  $K -n open-webui get deploy open-webui -o json | $S python3 -c '
import json,sys,subprocess,base64
c=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0]
env={x["name"]:x for x in c["env"]}
def val(n):
    x=env[n]
    if "value" in x: return x["value"]
    r=x["valueFrom"]["secretKeyRef"]
    s=json.loads(subprocess.check_output(["kubectl","-n","open-webui","get","secret",r["name"],"-o","json"]))
    return base64.b64decode(s["data"][r["key"]]).decode()
cid=val("FEISHU_CLIENT_ID")
if cid!="cli_aa8505afb0f89cb2": sys.exit("STOP: FEISHU_CLIENT_ID 不是 carher-236")
print(json.dumps({"app_id":cid,"app_secret":val("FEISHU_CLIENT_SECRET")}))' \
    | $K -n account-store exec -i "$POD" -- sh -c 'umask 077; cat > /tmp/lark.json'
  # 网关 token hash(只读事务)
  $K exec -i -n litellm-product litellm-db-0 -- sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -X -q -At -v ON_ERROR_STOP=1 -c "SET default_transaction_read_only=on" -c "select token from \"LiteLLM_VerificationToken\""' \
    | grep -E '^[0-9a-f]{64}$' | $K -n account-store exec -i "$POD" -- sh -c 'umask 077; cat > /tmp/gw.txt; wc -l < /tmp/gw.txt'
  FLAG=""; [ "$MIGRATE" = apply ] && FLAG="--apply"
  set +e
  $K -n account-store exec "$POD" -- python3 -B /opt/account-store/migrate_from_lark.py --from-lark --lark-creds /tmp/lark.json \
     --db /data/accounts.db --kek-file /kek/kek.bin --gateway-hashes /tmp/gw.txt $FLAG
  RC=$?
  set -e
  $K -n account-store exec "$POD" -- rm -f /tmp/lark.json /tmp/gw.txt
  echo "migrate rc=$RC"
  if [ "$MIGRATE" = apply ]; then
    $K -n account-store exec "$POD" -- python3 -B /opt/account-store/account_store.py verify --db /data/accounts.db --kek-file /kek/kek.bin
  fi
  exit $RC
fi
REMOTE
$J scp "$STAGE/remote.sh" $H198:/tmp/as-remote.sh
$J ssh $H198 "bash /tmp/as-remote.sh '$MIGRATE'; rc=\$?; rm -f /tmp/as-remote.sh; exit \$rc"
