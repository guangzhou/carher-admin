# Web tool-injection layer (sub2api-style) for zerokey web-only pods

## What this is

Lets a **web-only** zerokey-serve pod (one with **no `CODEX_TOKEN_DIR`**, i.e.
the aliyun / 225 "web-for-all" blueprint) still serve arbitrary caller-defined
`tools[]` and emit real OpenAI `tool_calls` / Responses `function_call`, instead
of silently degrading agentic traffic to plain chat.

Mechanism = the same technique every ChatGPT-web-subscription bridge
(sub2api / chat2api) uses:

1. Inject the caller's tool catalog into the prompt as text.
2. Instruct the model to emit a single fenced `{"tool_calls":[...]}` JSON block.
3. Parse that envelope back into OpenAI `tool_calls` (chat) or `function_call`
   output items + `response.function_call_arguments.*` events (responses).

## The one reliability trick that makes it work (measured 2026-07-22)

The web endpoint (`/backend-api/f/conversation`) wraps the model in ChatGPT's
**consumer harness** (system prompt + auto web-search + answer widgets). The
naive "you have a real runtime, call these tools" framing FAILS — the model
refuses ("I don't have access to that runtime") or answers naturally, and
weather/math even trigger its built-in search widgets.

The framing that works reliably (4/4, multi-turn, coding tools): **reframe the
task as *authoring the JSON for the next action*** — the model is a
planner/translator that never executes and never needs file access. That prompt
lives in `routes/web-tools.js: buildToolInstructions()`.

Best-effort, not native. Simple coding tools (read/edit/shell) are reliable;
keep schemas simple. **Whenever Codex tokens exist, the codex-pool path is
strictly better and is used automatically** — this layer only activates when
`hasTokens() === false`.

## Files (in zerokey-patch/routes/)

- `web-tools.js` — NEW. normalizeToolDefs / buildToolInstructions / extractToolCalls / newCallId.
- `raw.js` — chat/completions path: inject on `tools[]`, parse → `tool_calls`.
- `responses.js` — responses path: activates when `tools[] && !hasTokens()`,
  parse → `function_call` items (stream + non-stream).

Routing gate (unchanged priority):
```
tools[] present?
 ├─ hasTokens()  → handleCodex()        (native, preferred)
 └─ !hasTokens() → web tool-injection   (this layer, best-effort)
no tools → plain web replay (unchanged)
```

## Verified end-to-end (2026-07-22, throwaway web-only container on 188:8299, NO codex tokens)

- chat/completions + tools → `finish_reason:tool_calls`, real `tool_calls` ✅
- responses + tools (non-stream) → `function_call` output item ✅
- responses + tools (stream) → `created → output_item.added → function_call_arguments.delta/done → output_item.done → completed` ✅
- no-tool question → stays plain text, no false tool call ✅
- no-tools regression → unchanged ✅

## Deploy to a real web-only pod

### 188 docker (throwaway/test or per-acct web pod)
Mount the three files over the image and run WITHOUT `CODEX_TOKEN_DIR`:
```bash
# push files (scp broken on 188 → base64 pipe)
for f in web-tools.js raw.js responses.js; do
  base64 -i zerokey-patch/routes/$f | ssh cltx@10.68.13.188 "mkdir -p ~/zk-webtools-patch && base64 -d > ~/zk-webtools-patch/$f"
done
ZKIMG=$(ssh cltx@10.68.13.188 'docker inspect zerokey-codex-pool --format "{{.Config.Image}}"')
ssh cltx@10.68.13.188 "docker run -d --name zk-web-acct-N --restart always --network host \
  -e PORT=<port> -e ZK_USER=acct-N -e ZK_DEFAULT_MODEL=gpt-5-5 \
  -v ~/zk-webtools-patch/web-tools.js:/app/routes/web-tools.js:ro \
  -v ~/zk-webtools-patch/raw.js:/app/routes/raw.js:ro \
  -v ~/zk-webtools-patch/responses.js:/app/routes/responses.js:ro \
  -v <acct users.json>:/app/temp/users.json:ro \
  $ZKIMG"
```

### 225 K3s web-only pods (zero-N, blueprint = aliyun-zerokey-pool.yaml)
The image is baked from the same source. Rebuild the image with these three
routes files updated (build on 226 per repo red-lines), OR bind-mount via a
ConfigMap of the three JS files onto `/app/routes/*.js`. Do NOT set
`CODEX_TOKEN_DIR` on these pods — that would switch them to the codex path.

Smoke (from inside cluster or via svc):
```bash
curl -s http://<pod>:8200/v1/chat/completions -H 'Authorization: Bearer raw' \
  -H 'Content-Type: application/json' \
  -d '{"model":"gpt-5.5","messages":[{"role":"user","content":"read src/config.js"}],
       "tools":[{"type":"function","function":{"name":"read_file","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}}],"stream":false}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["choices"][0]["finish_reason"])'   # → tool_calls
```

## Rollback

Remove the three bind-mounts (or revert the image) → pod returns to plain web
replay. Zero effect on codex-pool pods (they never hit this branch).
