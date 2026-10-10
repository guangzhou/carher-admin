#!/usr/bin/env bash
# 第 1 步：只加 provider 凭据，不碰 model_list。
#
# 做什么：
#   1. 备份 carher-env-keys 当前全文到 ~/bge-sf-<ts>/
#   2. 往 carher-env-keys 加一个 key：SILICONFLOW_API_KEY（值从 stdin 读，不落盘、不进 history）
#   3. 滚动重启 litellm-proxy，让 envFrom 带上这个新 env
#   4. 在 pod 内用这个 env 实打 siliconflow，确认 provider 层立住
#
# 不做什么：不改 ConfigMap、不改 DB、不动任何 model_list 条目。
# 这一步跑完，线上路由与跑之前完全一致（bge-m3 仍然 100% 走 OpenRouter）。
#
# 用法（在 198 上）：
#   printf '%s' "$SF_KEY" | ./bge-m3-siliconflow-step1-provider.sh
set -euo pipefail

NS=litellm-product
SECRET=carher-env-keys
ENVKEY=SILICONFLOW_API_KEY
TS=$(date -u +%Y%m%dT%H%M%SZ)
BK="$HOME/bge-sf-$TS"
# 198 上 kubectl 存在但读不到 /etc/rancher/k3s/k3s.yaml（permission denied），
# 所以不能用 `command -v kubectl` 判定 —— 要实际跑一次看能不能读到集群。
if kubectl -n "$NS" get deploy litellm-proxy >/dev/null 2>&1; then
  K=kubectl
elif sudo kubectl -n "$NS" get deploy litellm-proxy >/dev/null 2>&1; then
  K="sudo kubectl"
else
  echo "FATAL: kubectl 和 sudo kubectl 都读不到 $NS/litellm-proxy，停手"
  exit 1
fi
echo "== 使用: $K"

SF_KEY=$(cat)
[ -n "$SF_KEY" ] || { echo "FATAL: 没有从 stdin 读到 key"; exit 1; }
case "$SF_KEY" in sk-*) ;; *) echo "FATAL: key 不以 sk- 开头，拒绝写入"; exit 1 ;; esac

mkdir -p "$BK"; chmod 700 "$BK"
echo "== 备份目录: $BK"

# ---- 1. 备份 ----
$K -n "$NS" get secret "$SECRET" -o yaml > "$BK/secret-$SECRET.before.yaml"
chmod 600 "$BK/secret-$SECRET.before.yaml"
echo "   secret 备份: $(wc -c < "$BK/secret-$SECRET.before.yaml") bytes"
$K -n "$NS" get deploy litellm-proxy -o yaml > "$BK/deploy-litellm-proxy.before.yaml"
echo "   deploy 备份: $(wc -c < "$BK/deploy-litellm-proxy.before.yaml") bytes"

# 记下改前的 key 清单和副本数，验收时对账
$K -n "$NS" get secret "$SECRET" -o json \
  | python3 -c 'import sys,json; print("\n".join(sorted(json.load(sys.stdin)["data"])))' \
  > "$BK/secret-keys.before.txt"
BEFORE_N=$(wc -l < "$BK/secret-keys.before.txt" | tr -d ' ')
REPLICAS=$($K -n "$NS" get deploy litellm-proxy -o jsonpath='{.spec.replicas}')
echo "   改前 key 数: $BEFORE_N   litellm-proxy 副本数: $REPLICAS"

if grep -qx "$ENVKEY" "$BK/secret-keys.before.txt"; then
  echo "   注意: $ENVKEY 已存在，本次为覆盖写"
fi

# ---- 2. 写 secret ----
# immutable 的 secret 不能 patch；先判定再决定做法。
IMMUT=$($K -n "$NS" get secret "$SECRET" -o jsonpath='{.immutable}')
if [ "$IMMUT" = "true" ]; then
  echo "FATAL: $SECRET 是 immutable。删重建会让期间重启的 pod 拿不到 env，"
  echo "       而这个 secret 被几十个 Deployment envFrom 共享。停手，需人工决策。"
  exit 1
fi

B64=$(printf '%s' "$SF_KEY" | base64 | tr -d '\n')
$K -n "$NS" patch secret "$SECRET" --type merge \
  -p "{\"data\":{\"$ENVKEY\":\"$B64\"}}" >/dev/null
unset B64
echo "== secret 已写入 $ENVKEY"

# 回读：只核长度和前缀，不打印 key
$K -n "$NS" get secret "$SECRET" -o jsonpath="{.data.$ENVKEY}" | base64 -d \
  | python3 -c 'import sys; v=sys.stdin.read(); print("   回读 len=%d prefix=%s..." % (len(v), v[:6]))'

$K -n "$NS" get secret "$SECRET" -o json \
  | python3 -c 'import sys,json; print("\n".join(sorted(json.load(sys.stdin)["data"])))' \
  > "$BK/secret-keys.after.txt"
AFTER_N=$(wc -l < "$BK/secret-keys.after.txt" | tr -d ' ')
echo "   改后 key 数: $AFTER_N (改前 $BEFORE_N)"
echo "   差集:"; diff "$BK/secret-keys.before.txt" "$BK/secret-keys.after.txt" || true

# ---- 3. 滚动重启 ----
echo "== 滚动重启 litellm-proxy（不 delete pod，走 rollout）"
$K -n "$NS" rollout restart deploy/litellm-proxy
$K -n "$NS" rollout status deploy/litellm-proxy --timeout=600s
READY=$($K -n "$NS" get deploy litellm-proxy -o jsonpath='{.status.readyReplicas}')
echo "   readyReplicas=$READY / spec.replicas=$REPLICAS"
[ "$READY" = "$REPLICAS" ] || { echo "FATAL: 副本没全 Ready，停手"; exit 1; }

# ---- 4. 在 pod 内验 provider ----
echo "== 逐 pod 验证 env 到位 + 实打 siliconflow"
cat > /tmp/sf-verify-$TS.py <<'PY'
import os, json, time, urllib.request, urllib.error
k = os.environ.get("SILICONFLOW_API_KEY") or ""
if not k:
    print("  FAIL: env SILICONFLOW_API_KEY 不存在"); raise SystemExit(1)
print("  env len=%d prefix=%s..." % (len(k), k[:6]))
ok = True
for m in ["Pro/BAAI/bge-m3", "BAAI/bge-m3"]:
    body = json.dumps({"model": m, "input": "provider verification probe"}).encode()
    req = urllib.request.Request("https://api.siliconflow.cn/v1/embeddings", data=body,
        headers={"Authorization": "Bearer " + k, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=30)
        d = json.loads(r.read())
        v = d["data"][0]["embedding"]
        print("  %-18s %sms HTTP %s dims=%d model=%s usage=%s"
              % (m, int((time.time()-t0)*1000), r.status, len(v), d.get("model"), d.get("usage")))
        if len(v) != 1024:
            print("  FAIL: 维度不是 1024"); ok = False
    except urllib.error.HTTPError as e:
        try: det = e.read().decode()[:160]
        except Exception: det = ""
        print("  %-18s HTTP %s ERR %s" % (m, e.code, det)); ok = False
    except Exception as e:
        print("  %-18s NET %s" % (m, str(e)[:160])); ok = False
raise SystemExit(0 if ok else 1)
PY

# 🔴 枚举范围只能取 30402 Service 的真实 endpoints，不能按 pod 名字前缀 grep。
# 2026-10-08 实测：按 `^litellm-proxy-[0-9a-f]` grep 会捞到旧 ReplicaSet 遗留的
# pod（b5c55fb47-4m25x，3d22h），它不在 endpoints 里、不吃生产流量、本来就没有
# 新 env ⇒ 整轮被判 FAIL，而服务中的 4 个副本其实全 PASS。假红。
PODS=$($K -n "$NS" get endpoints litellm-proxy \
         -o jsonpath='{range .subsets[*].addresses[*]}{.targetRef.name}{"\n"}{end}' \
       | grep -v '^$' | sort -u)
[ -n "$PODS" ] || { echo "FATAL: 取不到 litellm-proxy 的 endpoints，拒绝用名字前缀兜底"; exit 1; }
echo "   服务中的副本（取自 Service endpoints，非名字前缀）:"
echo "$PODS" | sed 's/^/     /'

FAIL=0
for POD in $PODS; do
  ST=$($K -n "$NS" get pod "$POD" -o jsonpath='{.status.phase}')
  [ "$ST" = "Running" ] || continue
  echo "-- $POD"
  $K -n "$NS" exec -i "$POD" -- sh -c 'cat > /tmp/sf-verify.py' < /tmp/sf-verify-$TS.py
  $K -n "$NS" exec "$POD" -- python3 /tmp/sf-verify.py || FAIL=1
  $K -n "$NS" exec "$POD" -- rm -f /tmp/sf-verify.py || true
done
rm -f /tmp/sf-verify-$TS.py

echo
if [ "$FAIL" = "0" ]; then
  echo "== 第 1 步 PASS：provider 层立住，每个 prod 副本都能用 env 里的 key 打通 siliconflow"
  echo "   线上路由未变（bge-m3 仍 100% 走 OpenRouter）。备份在 $BK"
  echo "   下一步才是改 model_list。"
else
  echo "== 第 1 步 FAIL：provider 没立住，不要进第 2 步。备份在 $BK"
  exit 1
fi
