---
name: carher-feishu-bench-regression
version: 1.0.3
description: "Use when a CarHer Feishu bot does not reply, /hermes or /openclaw says 无法识别飞书 chat_id, a Her instance needs Feishu channel registration, or the user asks for Feishu bench/regression/long-message/long-task pressure testing after a rollout."
metadata:
  requires:
    bins: ["kubectl", "jq", "lark-cli", "node"]
---

# CarHer Feishu Bench Regression

> Run a deployment-only Feishu regression for CarHer Her instances, fix missing channel registration, and write a bench report without touching source code unless explicitly asked.

## Use With

- Load `carher-upgrade-flow` first when this bench follows an upgrade or rollout.
- Load `carher-k8s-ops` before K8s access.
- Load `check-instance-status` when comparing HerInstance and pod health.
- Load `lark-im` before sending or searching Feishu messages.
- Load `lark-doc` before creating or updating the final Feishu document.

## Golden Rules

- Do deployment/runtime fixes only by default: HerInstance annotations, Deployment env, rollout, session-state backup/cleanup.
- Do not change source code when the user says source-code changes are out of scope.
- Never paste secrets, full tokens, or raw ConfigMaps into the final report. Redact `sk-*`, `oc_*`, `ou_*`, and `om_*` unless the user explicitly needs the exact id.
- Before sending Feishu test messages, the user must have already named the recipient group/chat and test intent. If not, ask once.
- Treat `lark-cli im +messages-send` JSON as authoritative: it can return `ok:false` with process exit `0`. Parse `.ok == true` before polling for a reply. Keep `--idempotency-key` short (for example `b68lm123456`); long keys can return Feishu `99992402 field validation failed`.
- Check `lark-cli im +messages-send --help` on the executor. Some deployed lark-cli versions emit JSON by default but do not support `--format` on send; use the default JSON output or `-q` there. If the send command is malformed, classify it as `automation_failed` and do not poll.
- Do not delete pods. Use rollout/readiness and reversible runtime patches.
- Never assume the home channel is the user's named regression group. For `无法识别飞书 chat_id`, the correct channel is the chat where the failing switch message was produced, often a bot P2P chat.
- Do not accept historical local group files as the final home channel when newer logs show a different conversation. `feishu-groups/index.json` and `feishu-sent-messages.json` are fallback evidence only; the latest failing switch card, latest slash command, or latest `received message ... in <chat>` log wins.
- Do not auto-patch home channel just because the latest sent-message chat differs from the current home channel. That is only a stale-home candidate; many Her instances legitimately reply in multiple chats. Require current failure evidence in that chat.
- Do not end a post-upgrade bench at basic replies. Include `/hermes`, `/openclaw`, Dify health/run when enabled, A2A when exposed, and long-message/long-task pressure cases.
- When the user explicitly asks for smoke-only or no pressure tests, still run deployment-health gates and report long-message/long-task/switch-pressure as `skipped_by_user`. Do not silently turn smoke-only into no validation.
- A clean pod scan proves deployment health, not Feishu group reply. Keep `deployment_health_pass`, `feishu_smoke_pass`, and `not_self_tested` as separate outcomes.
- For H75 upgrade fallout, prefer `scripts/h75-upgrade-repair-suite.py` collections over ad hoc pod scans. `collection-current-pod-anomalies.tsv` is the current-service failure gate; `collection-stale-pod-anomalies.tsv` is cleanup evidence only.

## Batch Bench Discipline

For multi-Her upgrades, every message test must be target-isolated and machine-readable:

- One row per target/scenario: `her_id`, `chat_id`, `app_id`, `bot_open_id`, `marker`, `send_ok`, `reply_ok`, `elapsed_s`, `active_engine`, `pod`, `notes`.
- Short idempotency keys only, such as `b67s123456`; Feishu may reject long keys with `99992402 field validation failed`.
- Parse `lark-cli im +messages-send` output and require `.ok == true` before polling. A process exit `0` is not enough.
- Search reply results by both marker and target sender `app_id`; otherwise the user's own test message can be misread as a pass.
- If a target is not in `openclaw` before OpenClaw scenarios, restore it first and mark the earlier failure as engine-state contamination.
- After any `/hermes` test, explicitly restore and re-smoke OpenClaw before moving to the next target.
- For `/hermes`, wait for Hermes Feishu WS `connected to wss` before sending the marker smoke. The active marker can change several seconds before Hermes is actually able to receive Feishu messages.
- If a batch only has K8s visibility and no exact bot-visible chat, mark Feishu scenarios `not_self_tested/no_home_channel_or_operator_not_in_chat`; do not count Deployment `2/2 Running` as a Feishu pass.

Minimal send/poll contract:

```bash
send_json="$(lark-cli im +messages-send --as user --chat-id "$CHAT_ID" \
  --msg-type text --content "$CONTENT_JSON" --idempotency-key "$SHORT_KEY")"
printf '%s\n' "$send_json" | jq -e '.ok == true' >/dev/null

lark-cli im +messages-search --as user --query "$MARKER" --chat-id "$CHAT_ID" \
  --page-limit 1 --page-size 20 --format json \
  | jq -e --arg app "$APP_ID" --arg marker "$MARKER" \
      '.data.messages[]? | select(((.sender.id // .sender.sender_id.app_id // "") == $app) and ((.content|tostring)|contains($marker)))'
```

## Deployment-Health Scan Contract

Use this after any batch upgrade or runtime fix, even when Feishu smoke is out of scope.

For H75, use the unified suite when possible:

```bash
python3 scripts/h75-upgrade-repair-suite.py --mode audit --skip-runtime-repair
```

Required outputs:

- `total`: number of `carher-user` pods.
- `2of2_running`: number of pods with `READY=2/2` and `STATUS=Running`.
- `not_ok`: any pod not `2/2 Running`.
- `failure_states`: rows matching `CrashLoopBackOff`, `Error`, `Pending`, `ImagePullBackOff`, `ErrImagePull`, `Init`, `PostStartHookError`, `CreateContainerConfigError`, `ContainerCreating`, or `Terminating`.

Commands:

```bash
kubectl -n carher get pods -l app=carher-user --no-headers \
  | awk '$2!="2/2" || $3!="Running" {print}'

kubectl -n carher get pods -l app=carher-user --no-headers \
  | awk '$3 ~ /CrashLoopBackOff|Error|Pending|ImagePullBackOff|ErrImagePull|Init|PostStartHookError|CreateContainerConfigError|ContainerCreating|Terminating/ {print}'

kubectl -n carher get pods -l app=carher-user --no-headers \
  | awk '{total++; if ($2=="2/2" && $3=="Running") ok++; else bad++}
         END {print "total=" total; print "2of2_running=" ok+0; print "not_ok=" bad+0}'
```

Pass criteria:

- `not_ok=0`.
- Failure-state scan returns no rows.
- For a full Feishu regression, this is only the deployment-health prerequisite; the target still needs a real marker reply from the target `app_id`.

## Fast Path: `无法识别飞书 chat_id`

When a Feishu card says `❌ 切换失败:无法识别飞书 chat_id。`, do this exact sequence before any broader bench work:

1. **Find the real failing chat and sender app**

   ```bash
   export NS=carher
   export QUERY='无法识别飞书 chat_id'
   lark-cli im +messages-search --as user --query "$QUERY" --page-limit 3 --page-size 20 --format json \
     | jq -r '.. | objects | select(has("message_id")) | [.create_time,.message_id,(.chat_id//""),(.sender.id//.sender.sender_id.open_id//""),((.content|tostring)|gsub("\n";" ")|.[0:240])] | @tsv'
   ```

   Capture:
   - `CHAT_ID`: the chat_id on the newest relevant failure row.
   - `APP_ID`: the sender id when it is `cli_*`; this maps to the Her instance.

   If global Feishu search cannot see the relevant failure because the operator is outside the chat, inspect the target pod logs and local message caches:

   ```bash
   POD="$(kubectl get pods -n "$NS" -l "user-id=$HER_ID" -o jsonpath='{.items[0].metadata.name}')"
   kubectl logs -n "$NS" "$POD" -c carher --since=24h \
     | rg -i '/hermes|/openclaw|无法识别|received message|conversation=| chat '
   kubectl exec -n "$NS" "$POD" -c carher -- sh -lc '
     test -f /data/.openclaw/feishu-groups/index.json && cat /data/.openclaw/feishu-groups/index.json
     test -f /data/.openclaw/feishu-sent-messages.json && cat /data/.openclaw/feishu-sent-messages.json
   '
   ```

   Use precedence:
   `latest failing switch card chat_id` > `latest slash-command/received-message conversation in pod logs` > `latest sent-message chat for that exact app` > `historical group index`.

2. **Resolve the Her instance from the sender**

   ```bash
   export APP_ID='<cli_xxx-from-search>'
   kubectl get her -n "$NS" -o json \
     | jq -r --arg app "$APP_ID" '.items[] | select(.spec.appId==$app) | [.metadata.name,.spec.userId,.spec.name,(.metadata.annotations["carher.io/feishu-home-channel"]//""),.status.phase,.status.feishuWS] | @tsv'
   ```

   If the search result is a P2P chat, still use that `CHAT_ID`; do not replace it with a regression group unless the failing card came from that group.

3. **Register the exact failing chat as home channel**

   ```bash
   export HER_ID='<resolved-user-id>'
   export CHAT_ID='<chat-id-from-failure>'
   kubectl annotate her "her-$HER_ID" -n "$NS" \
     carher.io/feishu-home-channel="$CHAT_ID" \
     carher.io/force-reconcile="$(date -u +%Y%m%dT%H%M%SZ)" \
     carher.io/reconcile-poke="$(date +%s)" \
     --overwrite
   ```

4. **Verify it actually reached the running pod**

   ```bash
   kubectl get deploy "carher-$HER_ID" -n "$NS" -o json \
     | jq -r '.spec.template.spec.containers[] | select(.name=="carher") | .env[]? | select(.name=="FEISHU_HOME_CHANNEL") | .value'
   ```

   If empty, metadata annotation did not trigger Deployment regeneration. Patch runtime env and roll:

   ```bash
   kubectl set env deployment/"carher-$HER_ID" -n "$NS" -c carher FEISHU_HOME_CHANNEL="$CHAT_ID"
   kubectl rollout status deployment/"carher-$HER_ID" -n "$NS" --timeout=900s
   ```

   Also set the Redis group mode for the same exact chat before proving the fix:

   ```bash
   POD="$(kubectl get pods -n "$NS" -l "user-id=$HER_ID" -o jsonpath='{.items[0].metadata.name}')"
   kubectl exec -i -n "$NS" "$POD" -c carher -- python3 - <<'PY'
   import os, socket, json
   from urllib.parse import urlparse
   chat=os.environ["FEISHU_HOME_CHANNEL"]
   app=os.environ["FEISHU_APP_ID"]
   p=urlparse(os.environ.get("REDIS_URL","redis://carher-redis.carher.svc:6379"))
   host=p.hostname or "carher-redis.carher.svc"; port=p.port or 6379
   payload=json.dumps({"mode":"group-at","context":"group-at runtime state for footer/gate parsers; ascii-only","set_by":"codex-upgrade-flow"}, ensure_ascii=True)
   def enc(*parts):
       out=[f"*{len(parts)}\r\n".encode()]
       for part in parts:
           b=str(part).encode(); out.append(f"${len(b)}\r\n".encode()+b+b"\r\n")
       return b"".join(out)
   def cmd(*parts):
       with socket.create_connection((host,port),timeout=5) as s:
           s.sendall(enc(*parts)); return s.recv(65536)
   print(cmd("SET", f"group:mode:{chat}:{app}", payload))
   print(cmd("SADD", f"group:tracked:{app}", chat))
   PY
   ```

5. **Prove the fix with the same chat**

   ```bash
   POD="$(kubectl get pods -n "$NS" -l "user-id=$HER_ID" -o jsonpath='{.items[0].metadata.name}')"
   kubectl exec -n "$NS" "$POD" -c carher -- sh -lc 'echo home=${FEISHU_HOME_CHANNEL:-}; curl -fsS -m 10 http://127.0.0.1:18789/healthz'
   lark-cli im +messages-send --as user --chat-id "$CHAT_ID" --text '/hermes'
   sleep 45
   kubectl exec -n "$NS" "$POD" -c carher -- cat /data/.engine/active
   lark-cli im +messages-send --as user --chat-id "$CHAT_ID" --text '/openclaw'
   sleep 45
   kubectl exec -n "$NS" "$POD" -c carher -- cat /data/.engine/active
   ```

   Pass criteria: no new `无法识别飞书 chat_id` card in that same chat, `/data/.engine/active` follows the requested engine, and target readiness appears in logs (`connected to wss` for Hermes, `gateway ready` + `ws client ready` for OpenClaw).

## Fast Path: active engine is Hermes and Feishu stops replying

If a Her instance replies before `/hermes`, then stops replying to all Feishu messages after switching:

1. **Confirm the active engine and Feishu adapter state**

   ```bash
   export NS=carher
   export HER_ID=<her-id>
   POD="$(kubectl get pods -n "$NS" -l "user-id=$HER_ID" -o jsonpath='{.items[0].metadata.name}')"
   kubectl exec -n "$NS" "$POD" -c carher -- sh -lc 'cat /data/.engine/active; ps -ef | sed -n "1,80p"'
   kubectl logs -n "$NS" "$POD" -c carher --since=20m \
     | rg -i 'Hermes Gateway|No adapter available for feishu|FEISHU_APP_ID/SECRET|lark-oapi|ModuleNotFoundError|ws client ready|event-dispatch'
   kubectl exec -n "$NS" "$POD" -c carher -- \
     /opt/hermes/.venv/bin/python3 -c 'import lark_oapi, aiohttp_socks' || true
   ```

   Bad state:
   - `/data/.engine/active` is `hermes`.
   - Logs say `No adapter available for feishu` or `FEISHU_APP_ID/SECRET not set`.
   - Env contains `FEISHU_APP_ID` and `FEISHU_APP_SECRET`, but Python cannot import `lark_oapi`.
   - Feishu messages get no card reply.

2. **Restore service first: switch marker back to OpenClaw and roll**

   ```bash
   kubectl exec -n "$NS" "$POD" -c carher -- sh -lc 'echo openclaw > /data/.engine/active; cat /data/.engine/active'
   kubectl rollout restart deployment/"carher-$HER_ID" -n "$NS"
   kubectl rollout status deployment/"carher-$HER_ID" -n "$NS" --timeout=900s
   ```

3. **Wait for real OpenClaw readiness**

   ```bash
   POD="$(kubectl get pods -n "$NS" -l "user-id=$HER_ID" -o jsonpath='{.items[0].metadata.name}')"
   kubectl exec -n "$NS" "$POD" -c carher -- sh -lc '
     echo home=${FEISHU_HOME_CHANNEL:-}
     echo engine=$(cat /data/.engine/active 2>/dev/null || true)
     curl -fsS -m 10 http://127.0.0.1:18789/healthz
   '
   kubectl logs -n "$NS" "$POD" -c carher --since=10m | rg -i 'exec openclaw gateway|http server listening|feishu\\[default\\]|ws client ready|gateway ready'
   ```

4. **Smoke test normal message replies before trying engine switching again**

   ```bash
   export CHAT_ID='<home-channel-chat-id>'
   export EXPECT="CARHER_SMOKE_OK_$(date -u +%H%M%S)"
   lark-cli im +messages-send --as user --chat-id "$CHAT_ID" --text "请只回复 $EXPECT"
   # Poll recent messages for EXPECT.
   ```

   Do not run `/hermes` again until the Hermes Feishu adapter issue is fixed or explicitly accepted as the next test target. Otherwise the instance can be switched back into a no-reply state.

5. **If the missing piece is Hermes Feishu Python deps**

   Compare with S3 `hermestest-75`: that reference has `lark_oapi=OK`. On ACK H75 images that do not, the temporary deployment/runtime repair is:

   ```bash
   export PY_TARGET=/data/.openclaw/local/hermes-python-packages
   kubectl exec -n "$NS" "$POD" -c carher -- env PY_TARGET="$PY_TARGET" sh -lc '
     set -e
     LARK_OAPI_VERSION="${LARK_OAPI_VERSION:-1.6.7}"
     rm -rf "$PY_TARGET"
     mkdir -p "$PY_TARGET"
     uv pip install --target "$PY_TARGET" --link-mode=copy \
       "lark-oapi==$LARK_OAPI_VERSION" "aiohttp-socks==0.11.0"
     PYTHONPATH="$PY_TARGET" /opt/hermes/.venv/bin/python3 -c "import lark_oapi, aiohttp_socks"
   '
   kubectl set env deployment/"carher-$HER_ID" -n "$NS" -c carher PYTHONPATH="$PY_TARGET"
   ```

   This is not a durable fleet fix: `/data/.openclaw/local` can be rebuilt on a new Pod. It is acceptable for a current-pod recovery or proof, but before upgrading many Her instances rebuild the runtime image/profile with the deps baked in.
   If `/data/.openclaw/.hermes/python-deps` already contains a newer `lark_oapi` version, prefer installing that same `lark-oapi` version into the persistent `PY_TARGET` and add `aiohttp-socks==0.11.0`. Always rerun the import check after the final rollout/restart, not only before the pod changes.

6. **If Hermes WS connects but Hermes still does not reply**

   Search logs around the marker:

   ```bash
   kubectl logs -n "$NS" "$POD" -c carher --since=10m \
     | rg -i "$MARKER|API call failed|TypeError|NoneType|provider|model|LiteLLM|ERROR"
   ```

   If the failure is provider/model-side, for example `TypeError: 'NoneType' object is not iterable`, Feishu ingress is fixed but Hermes message generation is not. Restore OpenClaw and report Hermes as partial instead of continuing the wave.

### Fast Path: Hermes sees Feishu messages but silently drops group `@`

Symptom:
- Active engine is `hermes`.
- Hermes Feishu WS is connected and `feishu_seen_message_ids.json` contains the user's message id.
- `gateway.log` has no `Received raw message` / `Inbound group message received` / model call for that message.
- After setting the same chat to `group-at`, the same marker replies normally.

First response:
- If the user is reporting no-reply and Hermes is not explicitly the desired final state, restore OpenClaw first: write `/data/.engine/active=openclaw`, rollout, wait for OpenClaw `/healthz`, Feishu WS ready, and then run the same-chat marker smoke.
- Only after OpenClaw service is restored should you continue Hermes-specific admission debugging.
- If the latest seen message chat differs from home, change home only when `messages-mget` or pod logs prove the no-reply messages are in that chat.

Root cause seen on ACK H75: Hermes group mode defaults to `owner-at`. In `_admit`, group messages from a sender Hermes does not recognize as owner are rejected before content extraction, and the reject is only logged at debug level. This looks like "no reply" even though Feishu ingress is working.

Runtime fix for a known home chat:

```bash
HER_ID=236
POD="$(kubectl -n carher get pod -l user-id=$HER_ID -o jsonpath='{.items[0].metadata.name}')"
kubectl -n carher exec "$POD" -c carher -- sh -lc '
python3 - <<PY
import os, socket, json
from urllib.parse import urlparse
chat=os.environ["FEISHU_HOME_CHANNEL"]
app=os.environ["FEISHU_APP_ID"]
redis_url=os.environ.get("REDIS_URL", "redis://carher-redis:6379/0")
parsed=urlparse(redis_url)
payload=json.dumps({"mode":"group-at","set_by":"ops-runtime-fix"}, ensure_ascii=False)
def resp(*parts):
    out=[f"*{len(parts)}\\r\\n".encode()]
    for part in parts:
        raw=str(part).encode()
        out.append(f"${len(raw)}\\r\\n".encode()); out.append(raw+b"\\r\\n")
    return b"".join(out)
for cmd in [("SET", f"group:mode:{chat}:{app}", payload), ("SADD", f"group:tracked:{app}", chat)]:
    with socket.create_connection((parsed.hostname or "carher-redis", int(parsed.port or 6379)), timeout=2) as s:
        s.sendall(resp(*cmd)); print(s.recv(256).decode("utf-8","replace").strip())
PY
'
```

Then send a real group `@` marker and require:
- `gateway.log` shows `Received raw message`, `Inbound group message received`, and `response ready`.
- Feishu search finds a reply from the target app id with the marker.

Batch audit after H75 upgrades:
- Query Redis key `group:mode:<home_chat_id>:<app_id>` for every upgraded Her with a home channel.
- `group-at` is safe for group `@` smoke.
- Missing key, `owner-at`, or legacy `discussion` can reproduce this no-reply symptom after `/hermes`.
- `78/169`-style targets with no home channel cannot be fixed by group mode; register a real home chat first.

## Core Workflow

1. **Snapshot and identify targets**

   ```bash
   export NS=carher
   export HER_ID=<her-id>
   export RUN_DIR=.her-feishu-bench-$(date -u +%Y%m%dT%H%M%SZ)
   mkdir -p "$RUN_DIR"

   kubectl get her "her-$HER_ID" -n "$NS" -o yaml > "$RUN_DIR/herinstance.yaml"
   kubectl get deploy "carher-$HER_ID" -n "$NS" -o yaml > "$RUN_DIR/deployment.yaml"
   kubectl get pods -n "$NS" -l "user-id=$HER_ID" -o wide > "$RUN_DIR/pods.txt"
   ```

   Capture: HerInstance UID, image, `carher.io/runtime-profile`, `carher.io/feishu-home-channel`, `status.phase`, `status.feishuWS`, and current ready pod.

2. **Map Feishu chat, bot, and Her instance**

   ```bash
   export CHAT_NAME='<group-or-chat-name>'
   export CHAT_ID="$(lark-cli im +chat-search --as user --query "$CHAT_NAME" --format json | jq -r '.data.chats[0].chat_id')"
   lark-cli im chats get --as user --params "{\"chat_id\":\"$CHAT_ID\"}" --format json
   lark-cli im chat.members bots --as user --params "{\"chat_id\":\"$CHAT_ID\"}" --format json
   kubectl get her -n "$NS" -o json | jq -r '.items[] | [.metadata.name,.spec.userId,.spec.appId,.spec.name,(.metadata.annotations["carher.io/feishu-home-channel"]//""),.status.phase,.status.feishuWS] | @tsv'
   ```

   Notes:
   - A P2P bot conversation can show `chat_mode=p2p` and still be a valid home channel.
   - If the bot is not in the named group, group `@` cannot work there; report it as channel membership/config, not model failure.

3. **Fix missing `chat_id` recognition**

   Symptom:
   - Feishu card says `❌ 切换失败:无法识别飞书 chat_id。`
   - Logs show `engine-swap command intercepted` but no usable group/chat conversation.
   - HerInstance has no `carher.io/feishu-home-channel`, or Deployment has no `FEISHU_HOME_CHANNEL`.

   Persistent source of truth:

   ```bash
   kubectl annotate her "her-$HER_ID" -n "$NS" \
     carher.io/feishu-home-channel="$CHAT_ID" \
     carher.io/force-reconcile="$(date -u +%Y%m%dT%H%M%SZ)" \
     carher.io/reconcile-poke="$(date +%s)" \
     --overwrite
   ```

   Verify whether operator injected it:

   ```bash
   kubectl get deploy "carher-$HER_ID" -n "$NS" -o json \
     | jq -r '.spec.template.spec.containers[] | select(.name=="carher") | .env[]? | select(.name=="FEISHU_HOME_CHANNEL") | .value'
   ```

   If the HerInstance annotation is present but Deployment env is still empty, metadata-only annotation may not have triggered a reconcile. Apply a reversible runtime patch and keep the HerInstance annotation for persistence:

   ```bash
   kubectl set env deployment/"carher-$HER_ID" -n "$NS" -c carher FEISHU_HOME_CHANNEL="$CHAT_ID"
   kubectl rollout status deployment/"carher-$HER_ID" -n "$NS" --timeout=900s
   ```

4. **Wait for H75/OpenClaw runtime readiness**

   H75 pods can become Kubernetes Ready before the gateway listens because entrypoint syncs runtime plugins and extensions first. Wait on actual ports and logs.

   ```bash
   POD="$(kubectl get pods -n "$NS" -l "user-id=$HER_ID" -o jsonpath='{.items[0].metadata.name}')"
   kubectl exec -n "$NS" "$POD" -c carher -- sh -lc '
     echo home=${FEISHU_HOME_CHANNEL:-}
     echo engine=$(cat /data/.engine/active 2>/dev/null || true)
     curl -fsS -m 10 http://127.0.0.1:18789/healthz
     curl -fsS -m 10 http://127.0.0.1:18800/.well-known/agent-card.json | head -c 300
   '
   kubectl get her "her-$HER_ID" -n "$NS" -o jsonpath='{.status.phase}{" "}{.status.feishuWS}{" "}{.status.lastHealthCheck}{"\n"}'
   ```

   Treat stale `.status.message=CrashLoopBackOff` as suspicious but not decisive; prefer pod restart counts, current container state, gateway health, Feishu WS, and logs.

5. **Verify engine switching**

   Send `/hermes`, then `/openclaw`, to the same Feishu chat that is registered as home channel. Poll the pod marker, restart count, gateway logs, and Feishu WS readiness. Do not call a switch passed just because `/data/.engine/active` changed.

   ```bash
   lark-cli im +messages-send --as user --chat-id "$CHAT_ID" --text '/hermes'
   kubectl logs -n "$NS" "$POD" -c carher --previous --since=5m \
     | rg 'engine-swap|command intercepted|process.exit|swap-card started'
   kubectl logs -n "$NS" "$POD" -c carher --since=5m \
     | rg 'active engine: hermes|exec hermes|Hermes Gateway Starting|connected to wss|No adapter available for feishu|FEISHU_APP_ID/SECRET'
   kubectl exec -n "$NS" "$POD" -c carher -- cat /data/.engine/active
   lark-cli im +messages-search --as user --query '切换失败' --chat-id "$CHAT_ID" --page-limit 1 --format json

   lark-cli im +messages-send --as user --chat-id "$CHAT_ID" --text '/openclaw'
   kubectl logs -n "$NS" "$POD" -c carher --previous --since=5m \
     | rg 'engine-swap|command intercepted|process.exit|swap-card started'
   kubectl logs -n "$NS" "$POD" -c carher --since=5m \
     | rg 'active engine: openclaw|exec openclaw|http server listening|gateway] ready|ws client ready'
   kubectl exec -n "$NS" "$POD" -c carher -- cat /data/.engine/active
   ```

   Pass criteria: no new `无法识别飞书 chat_id`, active engine changes to the requested engine, and the target engine reaches its real message ingress state:
   - Hermes: `connected to wss://msg-frontier.feishu.cn`.
   - OpenClaw: `http server listening`, `gateway ready`, and `[ws] ws client ready`.

### Switch Latency Bench: compare against S3/hermestest

When the user says switching is slow or asks to compare with S3/hermestest:

1. **Measure from the command path, not from pod creation**
   - Start time: the Feishu command send timestamp or log line `command intercepted`.
   - Engine handoff: `process.exit(0)`.
   - Ready time:
     - Hermes: first `connected to wss://msg-frontier.feishu.cn`.
     - OpenClaw: first `[ws] ws client ready`.
   - Also report restart count and whether this is a cold new Pod or a hot restart in the same Pod.

2. **Use second-run hot measurements**
   - A new Pod has empty `emptyDir` caches, so first start measures cache fill.
   - After one successful OpenClaw and one successful Hermes start in the same Pod, run the switch again and use that as the hot-path number.
   - Do not compare first-run ACK cold boot directly to S3 hot/container-local startup.

3. **Compare storage and file-operation signatures**

   ```bash
   kubectl exec -n "$NS" "$POD" -c carher -- sh -c '
     mount | grep -E "(/data/.openclaw|/opt/data|/carher-fastbin)" | sed -E "s#(//[^/ ]+)#[redacted-host]#g"
     printf "PATH=%s\n" "$PATH"
     command -v cp; command -v rm; command -v chown; command -v chmod || true
   '
   kubectl logs -n "$NS" "$POD" -c carher --since=10m --timestamps \
     | rg 'seeding baked ACP|ACP toolchain ready|synced official lark-cli skills|syncing baked shared skills|reconciling image-managed|ran [0-9]+ hermes patch|ran [0-9]+ runtime glue|exec hermes|exec openclaw|connected to wss|ws client ready'
   ```

   S3 `hermestest-75` reference from 2026-06-01:
   - `/data` and `/opt/data` were local ext4.
   - Image entrypoint hash matched ACK, so same `/entrypoint.sh` does not prove the same runtime profile.
   - OpenClaw -> Hermes: script start -> Feishu WS connected was about 45s; process start -> Feishu WS connected was about 39s.
   - Hermes -> OpenClaw: script start -> OpenClaw WS ready was about 30s; OpenClaw gateway init was about 18s.

   ACK/H75 reference from 2026-06-01:
   - Before optimization, `carher-1000` took about 148s for OpenClaw -> Hermes and about 143s for Hermes -> OpenClaw.
   - Main repeated costs were ACP bootstrap, official lark skill sync, shared skill sync, Hermes patches, runtime plugin refresh, gateway init, and K8s restart delay.
   - After deployment-only H75 fast-cache/prewarm, a new Hermes pod reached Feishu WS in about 31s from first log.
   - If only one direction was measured after the latest patch, say that explicitly and do not claim both directions are fixed.

4. **Deployment-only H75 fast-cache pattern**

   Use this when the user forbids source-code changes or S3 proves the image entrypoint is equivalent.

   - Add an initContainer that prewarms ACP adapters/wrappers, Claude state, lark-cli skills, shared skills, OpenClaw lark extension, and runtime plugins into local `emptyDir`.
   - Mount local state for `/data/.claude` and `/data/.acpx` before disabling ACP bootstrap.
   - Set main-container env only after prewarm exists:
     - `CARHER_RUNTIME_PLUGINS_REFRESH=0`
     - `CARHER_LARK_CLI_SKILLS_BUNDLE_DIR=/opt/carher-runtime/empty/lark-cli-skills`
     - `CARHER_SHARED_SKILLS_BUNDLE_DIR=/opt/carher-runtime/empty/shared-skills`
     - `CARHER_ACP_ENABLED=0` only when ACP wrappers/state are already present.
   - Clear mount contents with `find ... -exec rm -rf`, not `rm -rf <mountpoint>`.
   - Do not use `trap ERR` in `/bin/sh` init scripts.
   - Avoid brittle shell logic like `[ "$base" = "." ] || [ "$base" = ".." ] && continue` under `set -e`; use `case`.

   Pass criteria for the "40s acceptable" target:
   - Both `/hermes` and `/openclaw` are measured from command interception to real target readiness.
   - Target ready is Hermes Feishu WS connected, or OpenClaw HTTP + gateway + WS ready.
   - Hot-path switch is <= 40s when feasible; <= 45s is S3-parity tolerance.
   - No `无法识别飞书 chat_id`, no `No adapter available for feishu`, no restart loop, and a real message smoke passes in the same chat.

5. **Avoid measurement footguns**
   - macOS `date +%s%3N` prints a literal `N`; use seconds or Python/Node for milliseconds.
   - If a timing script fails after sending `/hermes` or `/openclaw`, do not resend immediately. Continue by reading logs from the already-triggered switch.
   - Kill stale local poll scripts before starting a new run; duplicate pollers can make restart counts look confusing.
   - Redact `oc_*`, `ou_*`, `om_*`, `sk-*`, and Feishu WS `access_key` in notes and reports.

6. **Verify H75/Dify linkage**

   When comparing against S3 internal Dify, use `hermestest-14` as the known-good reference:
   - S3 base URL: `http://10.68.13.187:5680`
   - S3 bootstrap/lifecycle control URL: `http://10.68.13.187:5688`
   - `workflow/dify-config.json` lifecycle health must return HTTP 200.

   ACK must keep public and Her runtime URLs separate:
   - Her workflow API URL: `http://dify-nginx.dify.svc.cluster.local`
   - In-pod bootstrap URL: `http://dify-bootstrap.dify.svc.cluster.local:5688/v1/bootstrap/carher-bot`
   - Generated lifecycle URL: `http://dify-bootstrap.dify.svc.cluster.local:5688/v1/lifecycle/carher-<ID>`
   - Public/browser URL, if needed, is separate: `https://dify-k8s.carher.net`. Do not use it as `workflow/dify-config.json.dify_base_url` from a Her pod.

   ```bash
   kubectl exec -n "$NS" "$POD" -c carher -- sh -lc '
     test "${CARHER_DIFY_ENABLED:-}" = "1"
     test -x /data/.openclaw/local/bin/dify-bootstrap-init
     test -x /data/.openclaw/local/bin/her-workflow-dify-creator
     test -x /data/.openclaw/local/bin/her-workflow-dify-mcp
     /data/.openclaw/local/bin/her-workflow-dify-creator config \
       | jq -r ".config | {bot_id,dify_base_url,lifecycle_base_url,codex_model,workspace_id}"
     /data/.openclaw/local/bin/her-workflow-dify-creator health
   '
   ```

   If lifecycle health fails with Cloudflare `403` / `1010` while direct `http://dify-bootstrap.dify.svc.cluster.local:5688/v1/lifecycle/<bot>/health` succeeds with the same token, this is a generated config/profile URL bug, not a model or workspace failure. Back up `workflow/dify-config.json`, switch `lifecycle_base_url` to the internal bootstrap URL, and rerun `her-workflow-dify-creator health`.

   If `dify_setup_status=403`, or `publish` / `new-key` succeeds but `run` returns Cloudflare `403` / `1010`, fix `dify_base_url` as well: patch both live `workflow/dify-config.json.dify_base_url` and Deployment env `CARHER_DIFY_BASE_URL=http://dify-nginx.dify.svc.cluster.local`, roll the target, then require `dify_setup_status=200`, `lifecycle_status=200`, and a successful `her-workflow-dify-creator run`.

   Creator import trap: Dify `CURRENT_DSL_VERSION` is `0.3.0` on ACK/S3. A DSL with `version: 0.3.1` returns `202 pending` and `app_id=null` by design; it is not an async worker. Either use `version: 0.3.0` for immediate `200 completed`, or immediately call `POST /apps/imports/<import_id>/confirm` through lifecycle within 10 minutes. `GET /apps/imports/<id>` returns `404` in this Dify build and should not be used as a status poll.

   If `run` returns HTTP 500 while config still points at internal `dify-nginx` / `dify-bootstrap`, inspect `dify-api` logs before changing the Her. A `psycopg2.OperationalError: server closed the connection unexpectedly` during `api_tokens.last_used_at` is a Dify API/DB transient; retry once and only repair Dify API/DB if the 500 persists.

7. **Run Feishu bench**

   Prefer existing local bench scripts when present, and pass discovered ids via env vars. Do not hardcode live ids in the skill.

   Basic group/DM smoke:

   ```bash
   export OUT_JSON="$RUN_DIR/basic-regression.json"
   CHAT_ID="$CHAT_ID" \
   BETA_BOT="$BETA_BOT" \
   RELEASE_BOT="$RELEASE_BOT" \
   SAMPLES=3 DM_SAMPLES=1 TIMEOUT_MS=180000 POLL_MS=2000 \
   OUT_JSON="$OUT_JSON" \
   node .her266-h75-state/feishu-regression-benchmark.mjs
   ```

   Long message and long task pressure:

   ```bash
   export OUT_JSON="$RUN_DIR/long-stress.json"
   CHAT_ID="$CHAT_ID" \
   BETA_BOT="$BETA_BOT" RELEASE_BOT="$RELEASE_BOT" \
   BETA_SENDER_IDS="$BETA_APP_ID" RELEASE_SENDER_IDS="$RELEASE_APP_ID" \
   TARGETS='beta,release' \
   SCENARIOS='long8k,long32k,longtask,burst' \
   TIMEOUT_MS=600000 POLL_MS=3000 \
   OUT_JSON="$OUT_JSON" \
   node .her266-h75-state/feishu-long-stress.mjs
   ```

   Minimum report metrics:
   - basic group `@`: passed/total and p50/p95 latency.
   - DM: passed/total, and API errors such as `230001` separately marked as Feishu API unsupported.
   - long stress: `long8k`, `long32k`, `longtask`, `burst1..3`, passed/total and max latency.
   - switch commands: `/hermes` and `/openclaw` pass/fail.
   - infra: image/profile/home-channel/env/WS/healthz/A2A.

8. **Handle bench failures**

   For no reply or missing expected text:

   ```bash
   lark-cli im +chat-messages-list --as user --chat-id "$CHAT_ID" --page-size 100 --format json
   kubectl logs -n "$NS" "$POD" -c carher --since=30m \
     | sed -E 's/(sk-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+/\1***/g; s/(oc_[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+/\1***/g; s/(ou_[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+/\1***/g; s/(om_[A-Za-z0-9_-]{8})[A-Za-z0-9_-]+/\1***/g' \
     | rg -i 'raw inbound|dispatch returned|deliverFired|compaction|timeout|error|Feishu card stream ready'
   ```

   If logs show `deliverFired=false` with repeated compaction and a huge target group session, back up and clear only that chat session key:

   ```bash
   kubectl exec -n "$NS" "$POD" -c carher -- env TARGET_CHAT_ID="$CHAT_ID" sh -lc '
     set -e
     f=/data/.openclaw/agents/main/sessions/sessions.json
     b=$f.bak-feishu-bench-$(date -u +%Y%m%dT%H%M%SZ)
     cp "$f" "$b"
     node - <<NODE
   const fs = require("fs");
   const f = "/data/.openclaw/agents/main/sessions/sessions.json";
   const chat = process.env.TARGET_CHAT_ID;
   if (!chat) throw new Error("missing TARGET_CHAT_ID");
   const data = JSON.parse(fs.readFileSync(f, "utf8"));
   let removed = 0;
   for (const key of Object.keys(data)) {
     if (key.includes("feishu:group") && key.includes(chat)) {
       delete data[key];
       removed += 1;
     }
   }
   fs.writeFileSync(f, JSON.stringify(data, null, 2) + "\n", { mode: 0o600 });
   console.log(JSON.stringify({ removed }));
   NODE
     echo backup=$b
   '
   ```

   Then rerun the failed target/scenario only before claiming fixed.

9. **Write the Feishu document**

   Use `lark-doc` and create a concise report with:
   - Test window and target instances.
   - Deployment/config changes made.
   - Pass/fail table for basic, switch, H75 runtime, A2A/Dify, long stress.
   - Failure analysis and runtime fix.
   - Residual risks and next recommendations.
   - Artifact paths under `$RUN_DIR`, with sensitive ids redacted.

   Prefer `docs +create --api-version v2 --doc-format markdown --content @report.md` if the report is already Markdown.

## Common Mistakes

- **Only annotating HerInstance**: metadata annotations may not update Deployment env immediately. Verify `FEISHU_HOME_CHANNEL` inside the Deployment and pod.
- **Using the wrong chat**: the home channel must match the chat where switch commands arrive. A bot P2P chat can be the correct channel.
- **Calling a missing bot a model failure**: if the bot is not in the group, group `@` cannot work until membership/channel registration is fixed.
- **Stopping at Kubernetes Ready**: H75 startup can still be syncing plugins. Wait for `/healthz`, A2A agent-card, Feishu WS, and logs showing gateway ready.
- **Calling marker change a switch pass**: `/data/.engine/active=hermes` only proves the marker changed. Hermes is not usable until Feishu WS connects; OpenClaw is not usable until gateway + WS are ready.
- **Sending Hermes smoke too early**: if the marker is sent between `active=hermes` and Hermes `connected to wss`, the message can be missed. Rerun after WS ready instead of calling Hermes failed.
- **Comparing cold ACK Pod to hot S3 container**: first start fills `emptyDir`; use second-run hot switch numbers for latency conclusions.
- **Using public Dify from a Her pod**: `https://dify-k8s.carher.net` can hit Cloudflare `403` / `1010`. Her pods should call internal `dify-nginx.dify.svc.cluster.local` for workflow runs and internal `dify-bootstrap.dify.svc.cluster.local:5688` for lifecycle.
- **Treating any Dify 500 as Her config drift**: check Dify API logs. DB connection resets can make a single run fail while the immediate retry and health pass.
- **Misreading Dify import `202 pending`**: `version: 0.3.1` DSL imports require `/apps/imports/<import_id>/confirm` within 10 minutes, or use `version: 0.3.0`. Do not wait on `/apps/imports/<id>`; it is not a status API here.
- **Mislabeling Feishu API `230001`**: user-to-bot DM may be unsupported for that bot/API path; record it separately from service failures.
- **Clearing all sessions**: only remove the exact target Feishu group session after backing up `sessions.json`.
