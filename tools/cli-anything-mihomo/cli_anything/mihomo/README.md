# cli-anything-mihomo

CLI harness for mihomo (the core inside ShellCrash) on 198: Clash subscription → audited
127.0.0.1-only config → real mihomo process → switch / pin / measure / verify exits.

## Install

```bash
uv tool install --editable tools/cli-anything-mihomo      # from the carher-admin repo root
cli-anything-mihomo --help
```

Dependencies (hard, no fallbacks): ssh key access to `cltx@10.68.13.198`; the 188 SOCKS tunnel
for GitHub + the feed (`ssh -fN -D 18880 cltx@10.68.13.188`); the mihomo binary, installed on
198 by `binary fetch` + `binary install`.

Local state, one dir per iGuge account (`--account X`, default `default`):
`~/.config/cli-anything-mihomo[-X]/` (0700: `sub.url`, `sub.yaml`, `instances/<name>.json`); binaries are
cached once in `~/.config/cli-anything-mihomo/`. On 198: `~/mihomo-harness/{bin,<name>}` (0700; `secret`
stays there; `account` = owner marker, other accounts are refused).

## Use

```bash
cli-anything-mihomo sub set-url                 # prompts, hidden
cli-anything-mihomo sub fetch && cli-anything-mihomo sub info
cli-anything-mihomo instance start
cli-anything-mihomo exit ports                  # port -> group -> node
cli-anything-mihomo exit select "♻️ 自动选择 - JP"
cli-anything-mihomo exit measure --all          # Scholar delay per node
cli-anything-mihomo config pin 17891 "US - 美国西雅图 - 05" && cli-anything-mihomo instance reload
cli-anything-mihomo exit verify                 # exit IP + real Scholar results per port
cli-anything-mihomo instance stop
cli-anything-mihomo instance list               # all instances on 198, all accounts
cli-anything-mihomo -a acct2 exit table         # port -> exit IP + country + Kiro latency
cli-anything-mihomo -a acct2 exit geo --all     # real exit country of every node (throwaway instance)
```

One-step wrapper (prints OK/STOP per step): `scripts/mihomo-198.sh quota|status|verify|countries|handoff`.
Quota of every iGuge account: `cli-anything-mihomo sub quota --all-accounts`.

Slot handoff from live numbers: `scripts/kiro-slots-egress-handoff.py --account acct2 --ports 18891-18895`.

Every command takes `--json`; `--name` selects an instance; no subcommand = REPL.
`cli-anything-mihomo shellcrash modes|facts|equivalents` = what ShellCrash would do instead.

## Tests

```bash
cd /tmp && CLI_ANYTHING_FORCE_INSTALLED=1 \
  $(uv tool dir)/cli-anything-mihomo/bin/python -m pytest <repo>/tools/cli-anything-mihomo/cli_anything/mihomo/tests -v
```

E2E runs a separate instance `e2e` (ports 27890/27891/29090) on 198 and destroys it.
