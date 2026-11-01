# MIHOMO.md — analysis & SOP

Sources read (2026-10-28): `MetaCubeX/mihomo@v1.19.33` (3f27ef39), `juewuy/ShellCrash@master` (b6e4c629).
Local clones: `.work/shellcrash/src/{mihomo,ShellCrash}` (not in git).

## What ShellCrash actually is

ShellCrash = ~12.5k lines of POSIX sh menus around **one binary it calls CrashCore**, which is
mihomo (or sing-box). Its own value is two things:

1. **Config assembly** (`starts/clash_modify.sh`): merge subscription + its `set.yaml`
   (ports, `allow-lan: true`, `external-controller: :9999`, secret) + dns/hosts/rule-providers.
2. **Traffic hijacking** (`starts/fw_start.sh`, `fw_iptables.sh`, `fw_nftables.sh`): five
   `firewall_area`s × four `redir_mod`s (Redir/Mix/Tproxy/Tun).

Everything else (node switching, delay tests, panel) is mihomo's REST API.
`cli-anything-mihomo shellcrash modes|facts` prints the table with file:line refs.

### Why not ShellCrash's other modes on 198

198 is the k3s master (litellm-product, kiro-rs, flannel). Areas 1/3/5 rewrite PREROUTING and
persist `ip_forward=1` into `/etc/sysctl.conf`; area 2/3 hijack OUTPUT, i.e. every pod's egress,
and insert `nameserver 127.0.0.1` into `/etc/resolv.conf`. Only area 4 (纯净模式) is harmless, and
area 4 is exactly "run mihomo with a mixed port". So this harness keeps mihomo and drops the rest.

## mihomo backend map (hub/route/*.go)

| need | API | note |
|---|---|---|
| list nodes/groups | `GET /proxies` | groups have `now`, `all`, `fixed` (url-test) |
| switch exit | `PUT /proxies/{group}` `{"name":X}` → 204 | Selector: free choice among `all`. **URLTest: fixes X until it fails the test** (`urltest.go fast()`); `DELETE` un-fixes |
| persist choice | `profile.store-selected: true` → `cache.db` in `-d` dir | survives `PUT /configs` and restart (measured) |
| delay of a node | `GET /proxies/{n}/delay?url=&timeout=` | timeout parsed as int16 ⇒ ≤ 32767 ms |
| delay of a group | `GET /group/{n}/delay` | **side effect: un-fixes a url-test group** (`groups.go`) — harness doesn't use it |
| hot reload | `PUT /configs?force=true` `{"path": abs}` | path must be inside `-d` home or `SAFE_PATHS` (`IsSafePath`) |
| fixed exit per port | top-level `listeners: [{type: mixed, port, listen, proxy: X}]` | `proxy:` bypasses rules entirely |
| auth | `Authorization: Bearer <secret>`; **empty secret ⇒ no auth at all** (`server.go` router) | harness injects `CLASH_OVERRIDE_SECRET` env at start, never in the file |
| validate | `mihomo -t -d DIR -f FILE` | used before every push |

## The iGuge feed (measured)

- Clash YAML only for a Clash User-Agent; header `subscription-userinfo` carries quota/expiry.
- **198 → feed.iggv5.com = 403**; fetch via 188 SOCKS and push.
- 62 nodes (http/socks5/vmess/tuic/anytls), 13 groups; `url-test` interval **3600 s** (harness: 300).
- Feed config ships `external-controller: ':9090'` (all interfaces) and `allow-lan: false`; harness rewrites both.
- Scholar reachability 2026-10-28: 48/62; all TW, IN, RU, TR, `US - 纽约 - 10` failed.
- **Node names lie about country**; only the exit IP counts (`DE - 德国2线` exits in France). Two accounts
  share node names/order and mostly the same exit IPs. Measured countries (both accounts, 10-28):
  US HK JP SG TW DE FR GB KR MY PH TR. No IN/VN/CA/AU; the IN node's entry (Guangzhou) resets the handshake.

## Multi-account / multi-instance (v1.1)

- `--account X` = its own state dir (subscription, instance settings). Binary cache is shared.
- Instance dirs on 198 are shared by all accounts; `<dir>/account` marks the owner and every mutating
  call (`start/stop/reload/destroy/push`, controller access) checks it. Added after the first v1.1 cut
  let `--account acct2 --name default instance stop` stop the default instance.
- `exit table` / `exit geo` probe **serially** (one worker): parallel probes through one node skew it.

## SOP

```bash
cli-anything-mihomo sub set-url --from-file <file>     # once
cli-anything-mihomo sub fetch                           # needs ssh -fN -D 18880 cltx@10.68.13.188
cli-anything-mihomo binary fetch && cli-anything-mihomo binary install --version v1.19.33
cli-anything-mihomo config pin 17891 "♻️ 自动选择 - US" # optional fixed-exit ports
cli-anything-mihomo instance start
cli-anything-mihomo exit verify                          # the only "works" that counts
```

Limits: the instance is a user process on 198 (no restart on reboot, not reachable from pods —
127.0.0.1 only). Serving pods needs a k3s Deployment + ClusterIP; that is a separate, reviewed step.
