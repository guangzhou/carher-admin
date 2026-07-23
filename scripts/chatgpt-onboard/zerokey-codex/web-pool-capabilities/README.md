# Web-only zerokey pod capabilities (tool_call via exec-harvest + injection; native gpt-5.6)

> **This folder** (`zerokey-codex/web-pool-capabilities/`) is the hub for making a
> web-only zerokey pod match the codex pool. Contents:
> - `README.md` — this doc (mechanisms, deploy, rollback, 5.6)
> - `deploy.sh` — push route patches to EXISTING 225 pods (CM + startup cp + rollout)
> - `new-pod.sh` — build a BRAND-NEW pod for an onboarded acct (capture → seed → deploy → register)
> - `register-web-pool.py` — register pods into LiteLLM zerokey-pool groups (5.5/5.6-sol/terra/luna)
> - `refresh-225.sh` — 188 cron: re-capture web sessions → kubectl-cp seed → rollout (keeps sessions fresh)
> - route patches themselves live in `../zerokey-patch/routes/{web-tools,raw,responses}.js`
>   (kept with the serve route overlay, not copied here, to avoid duplication)

Three capabilities that make a **web-only** zerokey pod (no `CODEX_TOKEN_DIR`)
functionally match the codex pool. All ship via the same routes patch + deploy.

1. **tool_call — exec-harvest** (primary, for shell tools) + **envelope injection**
   (fallback, for non-shell tools). See below.
2. **native gpt-5.6 sol/terra/luna** — plain-slug passthrough (never `-wm`).
3. Works with the standard deploy + register flow; sessions kept fresh by `refresh-225.sh`.

## Tool calls — the core problem and the two mechanisms

The ChatGPT **web** backend (`/backend-api/f/conversation`) does NOT accept a
`tools` field or emit native tool_calls, AND the model has a **server-side
code-interpreter it prefers** — so naive prompt-injection is preempted (the model
just runs `ls`/`git status` in its own sandbox and answers in prose). Two
mechanisms, chosen automatically in `raw.js`/`responses.js`:

### A. exec-harvest (PRIMARY — when the caller has a shell-like tool)
**Don't fight the built-in code-interpreter; harvest it.** The model reliably
emits its shell command as a `container.exec` code message in the SSE (e.g.
`bash -lc git status`) *before* running it. We capture that first command and
re-emit it as the caller's shell tool_call — the caller runs it on the REAL
machine. Near-native because it's the model's preferred behavior.
- `web-tools.js`: `detectShellTool` (matches shell/bash/exec/terminal/run_* and
  its command param) / `execCommandFromData` (pull the command from the SSE) /
  `execToToolCall` (map to the caller schema: string param → `bash -lc <cmd>`;
  **array param → `["bash","-lc",<cmd>]`** = codex/cursor shell schema).
- `raw.js`/`responses.js`: when a shell tool is present, send a light nudge
  ("use your shell"), capture the first `container.exec` in `onData`, finish
  immediately, emit as the caller's shell tool_call / function_call.
- **Measured ~65-75%** E2E via LiteLLM (was ~0 with injection). Correct commands
  (git status / ls / grep / cat / printf > file). Remaining misses = occasional
  outright refusals (per-request variance).
- ⚠️ Each exec spins a code-interpreter sandbox → **rate-limited**; do NOT hammer
  one acct fast (burst → 429/empty). Real spread-out traffic is fine.

### B. envelope injection (FALLBACK — non-shell / custom tools)
sub2api-style: inject the caller's tool catalog + a reframe prompt, parse the
model's `{"tool_calls":[...]}` back out. Prompt in `web-tools.js:
buildToolInstructions()`. Key reframe (Codex paradigm + our innovation): frame
the model as a **pure request→JSON translator with NO executor** (the "you have a
runtime" wording invites self-execution) + Codex persistence/anti-refusal
("acting is the only acceptable output; never say you lack access; never ask the
user to paste files"). Best-effort; per-account variance; a `tool_choice:required`
escalated retry fires on the first refusal.

**Priority (automatic):** codex tokens present → codex-pool native; else shell
tool present → exec-harvest; else → envelope injection. Probe with real coding
tools, never weather/math (those trip the web search widget — worst test case).

## Files (in zerokey-patch/routes/)

- `web-tools.js` — NEW. normalizeToolDefs / buildToolInstructions / extractToolCalls / newCallId.
- `raw.js` — chat/completions path: inject on `tools[]`, parse → `tool_calls`.
- `responses.js` — responses path: activates when `tools[] && !hasTokens()`,
  parse → `function_call` items (stream + non-stream).

Routing gate (automatic priority):
```
tools[] present?
 ├─ hasTokens()               → handleCodex()      (codex-pool native, preferred)
 ├─ !hasTokens() + shell tool → exec-harvest       (primary; ~65-75%)
 └─ !hasTokens() + non-shell  → envelope injection (fallback; best-effort)
no tools → plain web replay (unchanged)
```

## Verified end-to-end (2026-07-22, throwaway web-only container on 188:8299, NO codex tokens)

- chat/completions + tools → `finish_reason:tool_calls`, real `tool_calls` ✅
- responses + tools (non-stream) → `function_call` output item ✅
- responses + tools (stream) → `created → output_item.added → function_call_arguments.delta/done → output_item.done → completed` ✅
- no-tool question → stays plain text, no false tool call ✅
- no-tools regression → unchanged ✅

## Deploy to a real web-only pod

### 225 K3s web-only pods (zero-87..99) — ACTUAL DEPLOY MECHANISM (verified 2026-07-22, live)

The pods do NOT bake routes into the image. Startup command copies patch files
from ConfigMap `zk-image-patch` (mounted at `/patch`) into `/app` before `node`.
So deploying = update the CM + extend the copy command. Access 225 via 198's
`sudo k3s kubectl -n litellm-product` (direct kubectl context is aliyun, not this).

```bash
# 1. push the 3 files to 198 (scp broken → base64 pipe)
for f in web-tools.js raw.js responses.js; do
  base64 -i ../zerokey-patch/routes/$f | ssh cltx@10.68.13.198 "mkdir -p ~/zk-webtools && base64 -d > ~/zk-webtools/$f"
done

# 2. merge-patch the CM (preserves other keys: api.js/chatgpt.js/codex-pool.js/images.js/zerokey-serve-codex.js)
ssh cltx@10.68.13.198 'python3 - <<PY
import json
data={k:open(f"/home/cltx/zk-webtools/{k}").read() for k in ["web-tools.js","raw.js","responses.js"]}
open("/tmp/cm-patch.json","w").write(json.dumps({"data":data}))
PY
sudo k3s kubectl -n litellm-product patch cm zk-image-patch --type merge --patch-file /tmp/cm-patch.json'

# 3. first-time only: extend each deploy's startup command to copy the new routes.
#    Original args copy 3 files; add web-tools.js + raw.js + responses.js, then
#    patch container "zerokey" args[1] and rollout (canary zero-93 first):
#      cp /patch/zerokey-serve-codex.js /app/zerokey-serve-codex.js
#      cp /patch/images.js /app/routes/images.js
#      cp /patch/api.js /app/core/chatgpt/api.js
#      cp /patch/web-tools.js /app/routes/web-tools.js       # NEW
#      cp /patch/raw.js /app/routes/raw.js                   # NEW
#      cp /patch/responses.js /app/routes/responses.js       # NEW
#      exec node /app/zerokey-serve-codex.js
#    After that first args change, later CM-only tweaks just need: rollout restart.

# 4. smoke each pod (no curl in pod → hit podIP:8200 from the 198 host)
IP=$(ssh cltx@10.68.13.198 "sudo k3s kubectl -n litellm-product get pod -o jsonpath='{range .items[*]}{.metadata.name} {.status.podIP}{\"\n\"}{end}' | grep '^zero-93-' | awk '{print \$2}'")
ssh cltx@10.68.13.198 "curl -s http://$IP:8200/v1/chat/completions -H 'Authorization: Bearer raw' -H 'Content-Type: application/json' \
  -d '{\"model\":\"gpt-5.5\",\"messages\":[{\"role\":\"user\",\"content\":\"read src/config.js\"}],\"tools\":[{\"type\":\"function\",\"function\":{\"name\":\"read_file\",\"parameters\":{\"type\":\"object\",\"properties\":{\"path\":{\"type\":\"string\"}},\"required\":[\"path\"]}}}],\"stream\":false}'" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["choices"][0]["finish_reason"])'   # → tool_calls
```

Do NOT set `CODEX_TOKEN_DIR` on these pods — that switches them to the codex path.
These 13 pods are ALREADY members of LiteLLM group `zerokey-pool-gpt-5.5`
(alongside `188:8200` codex-pool), so the fix takes effect for prod traffic
immediately after rollout — no LiteLLM re-registration needed.

### 188 docker (throwaway/test or standalone per-acct web pod)
Mount the three files over the image and run WITHOUT `CODEX_TOKEN_DIR`:
```bash
for f in web-tools.js raw.js responses.js; do
  base64 -i ../zerokey-patch/routes/$f | ssh cltx@10.68.13.188 "mkdir -p ~/zk-webtools-patch && base64 -d > ~/zk-webtools-patch/$f"
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

## Reliability (measured across the fleet, 2026-07-22)

Best-effort, **per-account** variance — this is the crux. The web endpoint wraps
the model in ChatGPT's consumer harness, and some accounts' sessions land in a
stricter A/B bucket where the model refuses ("I don't have a read_file tool in
this chat") even under escalation. With the hardened prompt + one internal
escalated retry (`buildToolInstructions(..., escalate=true)` on no-envelope):

- ~8/13 accounts (zero-87/88/90/91/92/94/95/97) hit reliably.
- ~5/13 (zero-89/93/96/98/99) still refuse a large fraction of the time.

This is the documented ceiling of prompt-injection tool calls (same as sub2api).
Levers, in order:
1. Internal retry — already automatic (escalated STRICT-mode re-send).
2. `tool_choice: "required"` from the caller — forces the "REQUIRES a call" line.
3. Keep tool schemas simple; avoid weather/math prompts (trip the search widget).
4. **For guaranteed native tool_call, use the codex-pool path** (a pod WITH
   `CODEX_TOKEN_DIR`, hitting `/backend-api/codex/responses`). Web injection is a
   best-effort *capacity supplement*, not a native replacement. The stubborn
   accounts are best used for no-tool chat traffic or given codex tokens.

## gpt-5.6 sol/terra/luna on web pods (native, plain-slug passthrough)

The ChatGPT web backend serves the sol/terra/luna tunings **natively** — send the
PLAIN slug `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` (inline streaming,
`stream_handoff=false`, real content). Verified live on aliyun serve-70 + 225.

**Never append `-wm`.** `gpt-5.6-sol-wm` is the *with-memory* variant; it streams
via a conduit `stream_handoff` (the first `/f/conversation` POST only returns a
`resume_conversation_token` JWT; the body streams from an internal conduit our
stateless replay can't follow) → empty. The `GET /backend-api/conversation/resume`
endpoint exists but chasing it is a dead end — the plain slug just works.

Debug rule: **HTTP 200 ≠ real content.** To confirm 5.6 actually worked, check the
stream has `stream_handoff=false` AND non-empty content — not the status code.

Wiring (already in the routes patch):
- `raw.js` `resolveModel`: `gpt-5.6-{sol,terra,luna}` pass through verbatim;
  `chatgpt-gpt-5.6-{sol,terra,luna}` → stripped to the plain slug; generic
  `gpt-5.6` → `gpt-5-6-thinking`, `gpt-5.6-pro` → `gpt-5-6-pro`.
- Register the 14 pods into the 5.6 groups:
  ```bash
  python3 ./register-web-pool.py --variants 5.6-sol 5.6-terra 5.6-luna
  # or all groups (5.5 + 3x 5.6):  python3 ./register-web-pool.py
  # remove:                         python3 ./register-web-pool.py --delete
  ```
  Each group ends with 15 members = 14 web (`zk-N-gpt-5.6-<v>`) + 1 `188` codex.

## Rollback

Revert the CM keys (or the deploy args) → pods return to plain web replay.
Remove LiteLLM entries with `register-web-pool.py --delete`.
Zero effect on codex-pool pods (they never hit this branch).


