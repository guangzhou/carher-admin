# aliyun-probe

Internal probe service running inside aliyun `carher` ns to expose chatgpt-acct
pool state (5h%/7d%/auth/spend) to acct-admin (188) over HTTPS via cloudflared.

## Endpoints

- `GET /healthz` — cache meta, no bearer needed
- `GET /probe` — cached pool state (5min TTL bg refresh), bearer required
- `GET /probe?live=1` — force a fresh probe now (30s throttle), bearer required

## Env

| Var | Default | Purpose |
|-----|---------|---------|
| `PROBE_BEARER` | (required) | shared secret with acct-admin |
| `PROBE_NS` | `carher` | ns to list pods + exec |
| `PROBE_POD_LABEL` | `pool=chatgpt-acct` | label selector |
| `PROBE_DB_POD` | `litellm-db-0` | postgres pod for spend |
| `PROBE_DB_PWD` | (empty) | postgres pwd; empty → skip spend |
| `PROBE_CACHE_TTL` | `300` | bg refresh cadence in seconds |
| `PROBE_LIVE_THROTTLE` | `30` | min seconds between live probes |
| `PROBE_EXEC_TIMEOUT` | `25` | per-exec timeout in seconds |
| `PROBE_WORKERS` | `4` | concurrent kubectl-exec workers |

## RBAC

ServiceAccount `chatgpt-acct-probe` needs:
- `pods` get/list in ns `carher`
- `pods/exec` create in ns `carher` (for both pool=chatgpt-acct and litellm-db-0)

## Build

```
# on 47.84.112.136 build host (per CLAUDE.md)
nerdctl build -t cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/chatgpt-acct-probe:v0.1.0 .
nerdctl push  cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/chatgpt-acct-probe:v0.1.0
```
