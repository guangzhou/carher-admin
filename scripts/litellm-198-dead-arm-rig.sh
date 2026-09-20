#!/usr/bin/env bash
# litellm-198-dead-arm-rig.sh
#
# 作用域：**只在 198（ns litellm-product）**。阿里云那套不适用，别拿去跑。
#
# 造一个「一条死腿 + 一条活腿」的临时模型组，用来验证母 router 在某条腿
# 返回 400 `There are no healthy deployments for this model` 时到底会不会换腿。
# 全程不启动任何真实 ChatGPT 账号、不产生任何上游费用。
#
# 为什么要这个装置：
#   死号的故障形状是「叶子 k8s ready、但一个 model 都没加载 → 回 400 + 那句判别串」。
#   拿真死号复现要先把号停掉，代价大且不可控；拿健康叶子发个不存在的模型名**复现不出来**
#   （健康叶子回的是 `Invalid model name passed in model=...`，形状不对，尺子是坏的）。
#   所以用一个字节级可控的 stub，三个端口三种叶子形状：
#     4000 dead      = 400 marker + `/v1/models` 回空      （真过期号的形状）
#     4001 good      = 正常响应   + `/v1/models` 有 model
#     4002 transient = 400 marker + `/v1/models` 有 12 个   （瞬态，**不该**被自动摘）
#     4003 nodb      = 400 marker + `/v1/models` 也 400     （key 拿错，判 unknown 不判死）
#
# ⚠️ 两个必须遵守的测量纪律（都是踩过的坑）：
#   1. **每一枪的 body 必须不一样**。LiteLLM 开着响应缓存，body 相同的请求根本不会
#      到达上游 —— 会看到「32/32 成功、stub 计数 0」这种毫无意义的绿。
#   2. **默认测 /v1/responses，不是 /v1/chat/completions**。同一套装置两条路行为不同
#      （见 skill litellm-198-router-patch）。生产 acct 走的是 responses。
#
# 用法（在 198 上跑）：
#   ./litellm-198-dead-arm-rig.sh up                 # 建 stub + 临时组 + 临时 key
#   ./litellm-198-dead-arm-rig.sh probe [路径] [组数] [每组枪数]
#         路径 = responses（默认）| chat | both
#   ./litellm-198-dead-arm-rig.sh retire                # 自动摘号判定四情形+真删
#   ./litellm-198-dead-arm-rig.sh status
#   ./litellm-198-dead-arm-rig.sh down               # 全删，跑完必须执行
#
# 退出码：0=成功  1=装置/校验异常  2=环境/参数错误
set -uo pipefail

NS="${NS:-litellm-product}"
DEPLOY="${DEPLOY:-litellm-proxy}"
GROUP="${GROUP:-carher-deadtest}"
DEAD_ARM="deadtest-dead-arm"
LIVE_ARM="deadtest-live-arm"
KEY_ALIAS="deadtest-rig-probe"
LABEL="carher.io/temporary=dead-arm-rig"
KEYFILE="${KEYFILE:-/tmp/deadtest-rig.key}"

if [ -n "${KUBECTL:-}" ]; then
  read -r -a KUBECTL_CMD <<<"$KUBECTL"
elif command -v kubectl >/dev/null 2>&1; then
  KUBECTL_CMD=(kubectl)
else
  KUBECTL_CMD=(sudo k3s kubectl)
fi
k() { "${KUBECTL_CMD[@]}" "$@"; }
die() { echo "ERROR: $*" >&2; exit "${2:-2}"; }

proxy_pod() { k -n "$NS" get pod -l app="$DEPLOY" -o jsonpath='{.items[0].metadata.name}'; }
master_key() { k -n "$NS" exec "$(proxy_pod)" -- printenv LITELLM_MASTER_KEY 2>/dev/null; }

# 用母 router 自己的镜像跑 stub：节点本地已有，IfNotPresent 不触发任何拉取，
# 也就不违反「K8s 镜像必须走 ACR VPC」那条（根本没有 pull 动作）。
stub_image() {
  k -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].image}'
}
stub_node() {
  k -n "$NS" get pod -l app="$DEPLOY" -o jsonpath='{.items[0].spec.nodeName}'
}

# ---------------------------------------------------------------- up
do_up() {
  local img node
  img=$(stub_image); node=$(stub_node)
  [ -n "$img" ] || die "取不到 $DEPLOY 的 image"
  echo "stub 镜像 = $img"
  echo "stub 节点 = $node"

  cat <<STUB | k apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: deadstub-code
  namespace: $NS
  labels: { ${LABEL%%=*}: "${LABEL##*=}" }
data:
  stub.py: |
    import json, threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    DEAD = json.dumps({"error": {"message": (
        "You passed in model=deadtest-stub. "
        "There are no healthy deployments for this model"),
        "type": None, "param": None, "code": "400"}}).encode()

    GOOD = json.dumps({
        "id": "chatcmpl-goodstub", "object": "chat.completion",
        "created": 1788700000, "model": "goodstub",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }).encode()

    GOOD_RESP = json.dumps({
        "id": "resp_goodstub", "object": "response", "created_at": 1788700000,
        "status": "completed", "model": "goodstub",
        "output": [{"type": "message", "id": "msg_goodstub", "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
        "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
    }).encode()

    # /v1/models 的三种形状 —— 这是自动摘号那道实探闸门读的东西。
    # 真过期号的形状（2026-09-06 实测）：completions 回 400，但 /v1/models 回 200 且 data 为空。
    # 所以 dead 端口**不能**对 /v1/models 也回 400，否则形状不对、尺子是坏的。
    MODELS_EMPTY = json.dumps({"object": "list", "data": []}).encode()
    MODELS_ONE = json.dumps({"object": "list", "data": [
        {"id": "deadtest-stub", "object": "model"}]}).encode()
    MODELS_TWELVE = json.dumps({"object": "list", "data": [
        {"id": "m%d" % i, "object": "model"} for i in range(12)]}).encode()
    # 拿错 key 时健康叶子的真实回复（09-07 实测）：/v1/models 也回 400。
    # 这一档必须判 unknown —— 归 alive 会清零计数，归 dead 会误删健康号。
    MODELS_NODB = json.dumps({"error": {"message": "No connected db.",
        "type": "no_db_connection", "param": None, "code": "400"}}).encode()

    def make(status, body, tag, models_body):
        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def _r(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n: self.rfile.read(n)
                except Exception:
                    pass
                p = self.path.rstrip("/")
                if p.endswith("/models"):
                    st, out = (400, models_body) if models_body is MODELS_NODB else (200, models_body)
                else:
                    st, out = status, body
                    if status == 200 and p.endswith("responses"):
                        out = GOOD_RESP
                self.send_response(st)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            do_GET = do_POST = _r
            def log_message(self, fmt, *a):
                print("STUB[%s] %s %s" % (tag, self.command, self.path), flush=True)
        return H

    def serve(port, status, body, tag, models_body):
        print("stub[%s] listening on 0.0.0.0:%d" % (tag, port), flush=True)
        # 必须 ThreadingHTTPServer：单线程 + HTTP/1.1 keep-alive 会把并发连接堵住，
        # 制造 30~60s 的假延迟，看起来像"上游慢"，其实是量具自己的问题。
        ThreadingHTTPServer(("0.0.0.0", port), make(status, body, tag, models_body)).serve_forever()

    # 4000 dead      = 死号形状：400 marker + /v1/models 空       → 该被自动摘掉
    # 4001 good      = 健康号：正常响应 + /v1/models 有 1 个
    # 4002 transient = 瞬态形状：400 marker + /v1/models 有 12 个 → **不该**被摘（veto）
    # 4003 nodb      = 拿错 key 形状：400 marker + /v1/models 也 400 → unknown（hold，且计数不清零）
    threading.Thread(target=serve, args=(4001, 200, GOOD, "good", MODELS_ONE), daemon=True).start()
    threading.Thread(target=serve, args=(4002, 400, DEAD, "transient", MODELS_TWELVE), daemon=True).start()
    threading.Thread(target=serve, args=(4003, 400, DEAD, "nodb", MODELS_NODB), daemon=True).start()
    serve(4000, 400, DEAD, "dead", MODELS_EMPTY)
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: deadstub
  namespace: $NS
  labels: { app: deadstub, ${LABEL%%=*}: "${LABEL##*=}" }
spec:
  replicas: 1
  selector: { matchLabels: { app: deadstub } }
  template:
    metadata:
      labels: { app: deadstub, ${LABEL%%=*}: "${LABEL##*=}" }
    spec:
      nodeName: $node
      containers:
        - name: stub
          image: $img
          imagePullPolicy: IfNotPresent
          command: ["python3", "/stub/stub.py"]
          ports: [{ containerPort: 4000 }, { containerPort: 4001 }, { containerPort: 4002 }, { containerPort: 4003 }]
          volumeMounts: [{ name: code, mountPath: /stub }]
          resources:
            requests: { cpu: 10m, memory: 32Mi }
            limits:   { cpu: 100m, memory: 128Mi }
      volumes:
        - name: code
          configMap: { name: deadstub-code }
---
apiVersion: v1
kind: Service
metadata:
  name: deadstub
  namespace: $NS
  labels: { app: deadstub, ${LABEL%%=*}: "${LABEL##*=}" }
spec:
  selector: { app: deadstub }
  ports:
    - { name: dead, port: 4000, targetPort: 4000 }
    - { name: good, port: 4001, targetPort: 4001 }
    - { name: transient, port: 4002, targetPort: 4002 }
    - { name: nodb, port: 4003, targetPort: 4003 }
STUB
  k -n "$NS" rollout status deploy/deadstub --timeout=180s || die "stub 起不来" 1

  local mk; mk=$(master_key)
  [ -n "$mk" ] || die "取不到 master key"

  k -n "$NS" exec -i "$(proxy_pod)" -- python3 - "$mk" "$NS" "$GROUP" "$DEAD_ARM" "$LIVE_ARM" "$KEY_ALIAS" <<'PY' | tee "$KEYFILE.raw"
import json, sys, urllib.request, urllib.error
mk, ns, group, dead, live, alias = sys.argv[1:7]

def post(path, body):
    r = urllib.request.Request("http://localhost:4000" + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + mk, "Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=30).read().decode())
    except urllib.error.HTTPError as e:
        return {"_http": e.code, "_body": e.read().decode()[:200]}

# 两条腿形状照抄生产 acct arm（mode=responses / max_input_tokens=922000），
# 只把 api_base 换成 stub 的两个端口。weight 相同 → WA 会把 session 大致对半钉。
for arm, port in ((dead, 4000), (live, 4001)):
    print(arm, post("/model/new", {
        "model_name": group,
        "litellm_params": {
            "model": "openai/deadtest-stub",
            "api_base": "http://deadstub.%s.svc.cluster.local:%d" % (ns, port),
            "api_key": "dummy",
            "weight": 1,
        },
        "model_info": {"id": arm, "base_model": "gpt-5.6-luna",
                       "mode": "responses", "max_input_tokens": 922000},
    }))

res = post("/key/generate", {"key_alias": alias, "models": [group], "duration": "24h"})
print("KEY=" + res.get("key", "GENERATE_FAILED:" + json.dumps(res)[:200]))
PY

  grep -o 'KEY=sk-[A-Za-z0-9_-]*' "$KEYFILE.raw" | head -1 | cut -d= -f2 > "$KEYFILE"
  if [ -s "$KEYFILE" ]; then
    echo "临时 key 写入 $KEYFILE（24h 过期，只能访问 $GROUP）"
  else
    die "临时 key 生成失败，看 $KEYFILE.raw" 1
  fi
  echo "装置就绪。跑完务必执行：$0 down"
}

# ---------------------------------------------------------------- probe
do_probe() {
  local path="${1:-responses}" nsess="${2:-16}" nshot="${3:-2}"
  [ -s "$KEYFILE" ] || die "找不到临时 key（$KEYFILE），先跑 $0 up"
  local key; key=$(cat "$KEYFILE")
  local pod; pod=$(proxy_pod)

  case "$path" in
    responses|chat) local paths=("$path") ;;
    both) local paths=(responses chat) ;;
    *) die "路径只能是 responses / chat / both" ;;
  esac

  cat > /tmp/_rig_probe.py <<'PY'
import json, sys, time, urllib.request, urllib.error
KEY, GROUP, KIND, NSESS, NSHOT, TAG = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]
url = "http://localhost:4000/v1/" + ("responses" if KIND == "responses" else "chat/completions")
to = tf = te = 0
for s in range(NSESS):
    sid = "%s-s%02d" % (TAG, s)
    ok = fail = err = 0; d = []
    for i in range(NSHOT):
        # 每枪 content 都不同 —— 否则命中 LiteLLM 响应缓存，请求根本不出门
        uniq = "say ok #%d-%s" % (i, sid)
        body = ({"model": GROUP, "input": uniq, "max_output_tokens": 16, "temperature": 0.7}
                if KIND == "responses" else
                {"model": GROUP, "messages": [{"role": "user", "content": uniq}],
                 "max_tokens": 8, "temperature": 0.7})
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + KEY,
            "x-litellm-session-id": sid})   # 不给 session id 会退化成 key 级钉子，全部钉同一条腿
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                r.read(); ok += 1; d.append("OK/%.2f" % (time.time() - t0))
        except urllib.error.HTTPError as e:
            fail += 1; d.append("H%s/%.2f" % (e.code, time.time() - t0))
        except Exception:
            err += 1; d.append("ERR/%.2f" % (time.time() - t0))
    to += ok; tf += fail; te += err
    print("  %s OK=%d FAIL=%d ERR=%d  %s" % (sid, ok, fail, err, " ".join(d)))
print("TOTAL %s sessions=%d shots=%d OK=%d FAIL=%d ERR=%d" % (KIND, NSESS, NSESS * NSHOT, to, tf, te))
PY
  k -n "$NS" cp /tmp/_rig_probe.py "$pod":/tmp/_rig_probe.py >/dev/null || die "cp probe 失败"

  for kind in "${paths[@]}"; do
    echo
    echo "=== $kind  ($nsess session × $nshot 枪) ==="
    # 等 cooldown（60s）过期，避免上一轮的拉黑污染这一轮
    sleep 65
    local t0; t0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    k -n "$NS" exec "$pod" -- python3 /tmp/_rig_probe.py \
      "$key" "$GROUP" "$kind" "$nsess" "$nshot" "rig-${kind}" 2>/dev/null
    sleep 2
    local log; log=$(k -n "$NS" logs deploy/deadstub --since-time="$t0" 2>/dev/null)
    echo "  stub 命中：dead=$(echo "$log" | grep -c 'STUB\[dead\]')  good=$(echo "$log" | grep -c 'STUB\[good\]')"
    echo "  母 router 补丁日志："
    k -n "$NS" logs "$pod" --since-time="$t0" 2>/dev/null \
      | grep -o 'dead_deployment_retry: [a-z-]* [a-z-]*' | sort | uniq -c | sed 's/^/    /' || true
  done
  echo
  echo "判读："
  echo "  stub dead 命中数 == 客户端失败数  → 一枪一次，零重试（坏）"
  echo "  stub dead 命中数很少 + 客户端全成功 → 拉黑 + 换腿生效（好）"
  echo "  stub dead=0 good=0 但客户端全成功  → 全是缓存，这轮数据作废"
}

# ---------------------------------------------------------------- retire
# 直接在 proxy pod 内对 _auto_retire 打四种情形。为什么不走端到端发枪：
# 阈值是 10 且拉黑 60s ⇒ 一个 replica 攒到 10 次至少要 10 分钟，端到端测一轮太慢；
# 而这四种情形里最危险的是「该不该删」的判定，正好可以直接喂进去测。
# 端到端只需另外确认「计数器在数」——看 cooling down 日志里的 streak=N/10 就够。
do_retire() {
  [ -s "$KEYFILE" ] || die "先跑 $0 up"
  local pod; pod=$(proxy_pod)
  local mk; mk=$(master_key)

  cat > /tmp/_rig_retire.py <<'PY'
import json, logging, sys, urllib.request
sys.path.insert(0, "/app")
NS, GROUP, DEAD_ARM = sys.argv[1], sys.argv[2], sys.argv[3]
import dead_deployment_retry as D
from litellm._logging import verbose_router_logger

seen = []
class Cap(logging.Handler):
    def emit(self, rec):
        try: seen.append(rec.getMessage() % rec.args if rec.args else rec.getMessage())
        except Exception: seen.append(str(rec.msg))
verbose_router_logger.addHandler(Cap())
verbose_router_logger.setLevel(logging.DEBUG)

def base(port):
    return "http://deadstub.%s.svc.cluster.local:%d" % (NS, port)

class FakeRouter:
    def __init__(self, arms):
        self.model_list = arms

def arm(mid, port, group=None):
    return {"model_name": group or GROUP,
            "litellm_params": {"api_base": base(port), "api_key": "dummy"},
            "model_info": {"id": mid}}

def pad(n, port=4001):
    return [arm("pad-%d" % i, port) for i in range(n)]

fails = []
def case(name, router, mid, want_substr, want_absent=(), want_streak=None):
    seen.clear()
    D._dead_streak[mid] = 99
    D._delete_attempted.add(mid)
    D._auto_retire(router, mid)
    blob = "\n".join(seen)
    ok = want_substr in blob and not any(w in blob for w in want_absent)
    if want_streak is not None and D._dead_streak.get(mid) != want_streak:
        ok = False
        print("        ! 计数 = %r，期望 %r" % (D._dead_streak.get(mid), want_streak))
    print(("  ok   " if ok else "  FAIL ") + name)
    if not ok:
        fails.append(name)
        for line in seen: print("        | " + line[:200])

# 1) 瞬态形状：400 marker 但 /v1/models 有 12 个 model → 必须 veto，绝不能删
case("瞬态叶子(12 models) -> veto 不删",
     FakeRouter([arm("t-arm", 4002)] + pad(10)), "t-arm",
     "AUTO-RETIRE veto", want_absent=("deleted",))
# 2) 连不上 → hold，不判死（网络问题不许当账号死）
case("实探连不上 -> hold 不判死",
     FakeRouter([arm("u-arm", 4099)] + pad(10)), "u-arm",
     "AUTO-RETIRE hold", want_absent=("deleted", "veto"), want_streak=99)
# 2b) 拿错 key 的健康叶子形状（/v1/models 也 400）→ unknown → hold，**且计数不许清零**
#     旧代码把这一档当 veto 清零 ⇒ 永远升级不了，日志还谎称"瞬态"。这是 09-07 阳性对照抓到的。
case("实探回 400 No-connected-db -> hold 且计数不清零",
     FakeRouter([arm("k-arm", 4003)] + pad(10)), "k-arm",
     "AUTO-RETIRE hold", want_absent=("deleted", "veto"), want_streak=99)
# 2c) 这条腿没有 api_key → abort（认不过去，不判死）
case("腿上没有 api_key -> abort",
     FakeRouter([{"model_name": GROUP,
                  "litellm_params": {"api_base": base(4000)},
                  "model_info": {"id": "nokey-arm"}}] + pad(10)), "nokey-arm",
     "AUTO-RETIRE abort", want_absent=("deleted", "veto", "hold"))
# 3) 确认 0 models，但组只剩 3 条腿 → 护栏拦住
case("0 models 但组低于下限 -> BLOCKED",
     FakeRouter([arm("b-arm", 4000)] + pad(2)), "b-arm",
     "AUTO-RETIRE BLOCKED", want_absent=("deleted",))
# 4) model_list 里找不到这条腿（已被删）→ abort
case("腿不在 model_list -> abort",
     FakeRouter(pad(10)), "ghost-arm",
     "AUTO-RETIRE abort", want_absent=("deleted",))

# 5) 真删：真实临时腿 + 0 models + 组内腿数padding够（护栏输入由 3) 单独验过）
print("  --- 真删（对临时腿 %s 动手）---" % DEAD_ARM)
seen.clear()
D._dead_streak[DEAD_ARM] = 99
D._delete_attempted.add(DEAD_ARM)
D._auto_retire(FakeRouter([arm(DEAD_ARM, 4000)] + pad(10)), DEAD_ARM)
blob = "\n".join(seen)
for line in seen: print("        | " + line[:220])
if "AUTO-RETIRE deleted" not in blob:
    fails.append("真删未发生"); print("  FAIL 真删未发生")
elif "manifest" not in blob:
    fails.append("缺备份 manifest 行"); print("  FAIL 缺备份 manifest 行")
else:
    print("  ok   真删发生且留了 manifest 备份行")

print()
print("RETIRE-TEST %s (fails=%d)" % ("FAILED" if fails else "PASSED", len(fails)))
sys.exit(1 if fails else 0)
PY
  k -n "$NS" cp /tmp/_rig_retire.py "$pod":/tmp/_rig_retire.py >/dev/null || die "cp 失败"
  echo "=== _auto_retire 四种情形 + 真删 ==="
  k -n "$NS" exec "$pod" -- env LITELLM_MASTER_KEY="$mk" \
    python3 /tmp/_rig_retire.py "$NS" "$GROUP" "$DEAD_ARM"
  local rc=$?
  echo
  echo "提示：真删只删了临时腿；跑完仍要执行 $0 down 收尾。"
  return $rc
}

# ---------------------------------------------------------------- status / down
do_status() {
  k -n "$NS" get all,cm -l "$LABEL" 2>/dev/null || true
  local mk; mk=$(master_key)
  k -n "$NS" exec -i "$(proxy_pod)" -- python3 - "$mk" "$GROUP" <<'PY' 2>/dev/null
import json, sys, urllib.request
mk, group = sys.argv[1], sys.argv[2]
r = urllib.request.Request("http://localhost:4000/model/info", headers={"Authorization": "Bearer " + mk})
d = json.load(urllib.request.urlopen(r, timeout=30))
print("组 %s 的腿:" % group, [m["model_info"].get("id") for m in d["data"] if m.get("model_name") == group])
PY
}

do_down() {
  local mk; mk=$(master_key)
  k -n "$NS" exec -i "$(proxy_pod)" -- python3 - "$mk" "$DEAD_ARM" "$LIVE_ARM" "$(cat "$KEYFILE" 2>/dev/null)" <<'PY' 2>/dev/null
import json, sys, urllib.request, urllib.error
mk, dead, live, key = sys.argv[1], sys.argv[2], sys.argv[3], (sys.argv[4] if len(sys.argv) > 4 else "")
def post(path, body):
    r = urllib.request.Request("http://localhost:4000" + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + mk, "Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(r, timeout=30).read().decode()[:120]
    except urllib.error.HTTPError as e:
        return "HTTP%s %s" % (e.code, e.read().decode()[:120])
for arm in (dead, live):
    print(arm, "->", post("/model/delete", {"id": arm}))
if key:
    print("key ->", post("/key/delete", {"keys": [key]}))
PY
  k -n "$NS" delete deploy/deadstub svc/deadstub cm/deadstub-code --ignore-not-found
  rm -f "$KEYFILE" "$KEYFILE.raw"
  echo
  echo "注意：/model/info 里两条腿不会立刻消失 —— 4 个副本靠轮询收敛，"
  echo "      90 秒后再查一次；别把中间态当成删除失败。"
}

case "${1:-}" in
  up)     do_up ;;
  probe)  shift; do_probe "$@" ;;
  retire) do_retire ;;
  status) do_status ;;
  down)   do_down ;;
  *) die "用法: $0 {up|probe [responses|chat|both] [组数] [每组枪数]|retire|status|down}" ;;
esac
