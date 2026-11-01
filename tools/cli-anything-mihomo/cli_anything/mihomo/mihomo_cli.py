from __future__ import annotations

import functools
import json
import shlex

import click

from cli_anything.mihomo import __version__
from cli_anything.mihomo.core import binary, config, exits, instance, shellcrash, subscription
from cli_anything.mihomo.utils import remote198
from cli_anything.mihomo.utils.repl_skin import ReplSkin

ERRORS = (remote198.RemoteError, subscription.SubscriptionError, config.ConfigError,
          binary.BinaryError, instance.InstanceError)


class Context:
    def __init__(self, json_output: bool, name: str, account: str):
        self.json_output = json_output
        self.name = name
        self.account = account


def _emit(ctx: Context, value: object, human: str | None = None) -> None:
    if ctx.json_output or human is None:
        click.echo(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
        return
    click.echo(human)


def _guard(fn):
    @functools.wraps(fn)
    def wrapper(ctx: Context, *a, **kw):
        try:
            return fn(ctx, *a, **kw)
        except ERRORS as exc:
            payload = {"ok": False, "error": str(exc), "type": type(exc).__name__}
            stderr = getattr(exc, "stderr", "")
            if stderr:
                payload["stderr"] = stderr
            if ctx.json_output:
                raise click.ClickException(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            raise click.ClickException(str(exc) + (f"\n{stderr}" if stderr else ""))
    return wrapper


def _table(rows: list[dict[str, object]], cols: list[str]) -> str:
    if not rows:
        return "(none)"
    w = {c: max(len(c), *(len(str(r.get(c, "") if r.get(c) is not None else "-")) for r in rows)) for c in cols}
    fmt = lambda r, c: str(r.get(c) if r.get(c) is not None else "-").ljust(w[c])  # noqa: E731
    return "\n".join(["  ".join(c.ljust(w[c]) for c in cols)] + ["  ".join(fmt(r, c) for c in cols) for r in rows])


@click.group(invoke_without_command=True)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--name", "-n", help="Instance name on 198 (default: the account name).")
@click.option("--account", "-a", help="iGuge account (one subscription each): default -> ~/.config/cli-anything-mihomo, "
              "X -> ~/.config/cli-anything-mihomo-X. Overrides MIHOMO_STATE_DIR.")
@click.version_option(__version__)
@click.pass_context
def cli(click_ctx: click.Context, json_output: bool, name: str | None, account: str | None) -> None:
    """mihomo (ShellCrash's engine) on 198: subscription -> safe config -> 127.0.0.1-only proxy -> pick exits."""
    try:
        subscription.use_account(account)
    except subscription.SubscriptionError as exc:
        raise click.UsageError(str(exc))
    acct = subscription.account()
    click_ctx.obj = Context(json_output, name or acct, acct)
    if click_ctx.invoked_subcommand is None:
        click_ctx.invoke(repl)


# ---------------------------------------------------------------- sub
@cli.group(name="sub")
def sub_group() -> None:
    """Subscription (Clash YAML). Fetched through 188: 198 gets 403 from the feed."""


@sub_group.command("set-url")
@click.option("--from-file", type=click.Path(exists=True, dir_okay=False), help="Read the URL from a file.")
@click.pass_obj
@_guard
def sub_set_url(ctx: Context, from_file: str | None) -> None:
    """Store the subscription URL (0600). Without --from-file it is prompted, not echoed."""
    url = open(from_file).read().strip() if from_file else click.prompt("subscription URL", hide_input=True)
    subscription.set_url(url)
    _emit(ctx, {"ok": True, "stored": subscription.sub_url_file()}, f"stored in {subscription.sub_url_file()}")


@sub_group.command("fetch")
@click.option("--direct", is_flag=True, help="Do not use the 188 SOCKS proxy.")
@click.pass_obj
@_guard
def sub_fetch(ctx: Context, direct: bool) -> None:
    """Download the subscription and print a secret-free summary."""
    r = subscription.fetch(None if direct else subscription.FETCH_PROXY)
    _emit(ctx, r, f"{r['proxies']} nodes  {r['by_region']}\ntypes {r['by_type']}\n{len(r['groups'])} groups, {r['rules']} rules")


@sub_group.command("info")
@click.pass_obj
@_guard
def sub_info(ctx: Context) -> None:
    """Summary of the cached subscription + traffic quota/expiry from the feed header."""
    r = {**subscription.summary(subscription.load()), "quota": subscription.quota()}
    q = r["quota"]
    groups = "\n".join(f"  {g['type']:9s} {g['members']:3d}  {g['name']}" for g in r["groups"])
    _emit(ctx, r, f"{r['proxies']} nodes  {r['by_region']}\nquota  {q['used_gb']} / {q['total_gb']} GB "
                  f"({q['used_pct']}%), expires {q['expire_date']}\ngroups:\n{groups}")


@sub_group.command("quota")
@click.option("--all-accounts", "all_accounts", is_flag=True, help="Every local account with a stored URL.")
@click.option("--cached", is_flag=True, help="Read the last fetch's header instead of re-fetching.")
@click.option("--warn-pct", type=float, default=80, show_default=True)
@click.option("--warn-days", type=int, default=30, show_default=True)
@click.pass_obj
@_guard
def sub_quota(ctx: Context, all_accounts: bool, cached: bool, warn_pct: float, warn_days: int) -> None:
    """iGuge traffic used/left/total, next monthly reset, expiry. Re-fetches by default (the feed header is the source;
    same numbers as the extension's 「客户端下载与订阅」 page). At 200GB the client account pauses until the reset day.

    Exit 1 if any account is >= --warn-pct used or expires within --warn-days.
    """
    import time as _t
    names = subscription.accounts() if all_accounts else [ctx.account]
    rows, bad = [], False
    for a in names:
        subscription.use_account(a)
        try:
            if not cached:
                subscription.fetch()
            q = subscription.quota()
        except subscription.SubscriptionError as exc:
            rows.append({"account": a, "error": str(exc), "status": "STOP"})
            bad = True
            continue
        days = int((q["expire"] - _t.time()) // 86400) if q.get("expire") else None
        warn = (q["used_pct"] or 0) >= warn_pct or (days is not None and days < warn_days)
        bad |= warn
        rows.append({"account": a, **q, "days_left": days, "status": "WARN" if warn else "OK",
                     "age_s": int(_t.time()) - q["headers_mtime"]})
    subscription.use_account(ctx.account)
    human = _table([{**r, "used": f"{r.get('used_gb')} / {r.get('total_gb')} GB ({r.get('used_pct')}%)"} for r in rows],
                   ["status", "account", "account_status", "used", "left_gb", "reset_date", "expire_date", "days_left", "error"])
    _emit(ctx, rows, human)
    if bad:
        raise SystemExit(1)


@sub_group.command("nodes")
@click.option("--region", help="Two-letter tag, e.g. US, JP, HK.")
@click.pass_obj
@_guard
def sub_nodes(ctx: Context, region: str | None) -> None:
    """Nodes in the subscription (no servers/passwords)."""
    rows = [n for n in subscription.nodes(subscription.load()) if not region or n["region"] == region.upper()]
    _emit(ctx, rows, _table(rows, ["region", "type", "name"]))


@cli.command("tunnel")
@click.pass_obj
@_guard
def tunnel(ctx: Context) -> None:
    """Make sure the 188 SOCKS tunnel (GitHub + feed access) is listening; start it if not."""
    r = subscription.ensure_tunnel()
    _emit(ctx, r, f"{r['proxy']}: {'started' if r.get('started') else 'already up'}")


# ---------------------------------------------------------------- binary
@cli.group(name="binary")
def binary_group() -> None:
    """The real mihomo binary (MetaCubeX release, sha256 from the release API)."""


@binary_group.command("fetch")
@click.option("--version", default="latest", show_default=True)
@click.pass_obj
@_guard
def binary_fetch(ctx: Context, version: str) -> None:
    """Download locally via 188 and verify the digest."""
    r = binary.fetch(version)
    _emit(ctx, r, f"{r['asset']}  sha256 {r['sha256'][:16]}… OK -> {r['local']}")


@binary_group.command("install")
@click.option("--version", required=True)
@click.pass_obj
@_guard
def binary_install(ctx: Context, version: str) -> None:
    """Copy to 198 ~/mihomo-harness/bin/, re-check sha256 there, print `mihomo -v`."""
    r = binary.install(version)
    _emit(ctx, r, f"{r['remote']}\n{r['reports']}")


@binary_group.command("list")
@click.pass_obj
@_guard
def binary_list(ctx: Context) -> None:
    """Versions installed on 198."""
    r = binary.installed()
    _emit(ctx, r, "\n".join(r) or "(none)")


# ---------------------------------------------------------------- config
@cli.group(name="config")
def config_group() -> None:
    """This instance's local settings (ports, pins, url-test interval) and the rendered config."""


@config_group.command("show")
@click.pass_obj
@_guard
def config_show(ctx: Context) -> None:
    """Instance settings (local JSON; no secrets in it)."""
    st = config.load_state(ctx.name)
    pins = "\n".join(f"  pin {p['port']} -> {p['target']}" for p in st["pins"]) or "  (no pins)"
    _emit(ctx, st, f"instance {ctx.name}\n  mixed  127.0.0.1:{st['mixed_port']} (rules, default group {st['default_group']})\n"
                   f"  ctl    127.0.0.1:{st['ctl_port']}\n  url-test interval {st['url_test_interval']}s\n"
                   f"  binary {st.get('version') or '(newest on 198)'}\n{pins}")


@config_group.command("set")
@click.option("--mixed-port", type=int)
@click.option("--ctl-port", type=int)
@click.option("--url-test-interval", type=click.IntRange(0, 86400))
@click.option("--default-group")
@click.option("--version", help="Pin the binary version (e.g. v1.19.33).")
@click.pass_obj
@_guard
def config_set(ctx: Context, **kw) -> None:
    """Change settings (validated against the cached subscription). Apply with `instance reload`."""
    st = config.load_state(ctx.name)
    for k, v in kw.items():
        if v is not None:
            st[k] = v
    config.build(subscription.load(), st)  # validate before saving
    config.save_state(ctx.name, st)
    _emit(ctx, st, "saved; apply with `instance reload` (ports/ctl change need `instance stop && instance start`)")


@config_group.command("pin")
@click.argument("port", type=int)
@click.argument("target")
@click.pass_obj
@_guard
def config_pin(ctx: Context, port: int, target: str) -> None:
    """Make PORT always exit via TARGET (a node or a group). Apply with `instance reload`."""
    st = config.load_state(ctx.name)
    st["pins"] = [p for p in st["pins"] if p["port"] != port] + [{"port": port, "target": target}]
    st["pins"].sort(key=lambda p: p["port"])
    config.build(subscription.load(), st)
    config.save_state(ctx.name, st)
    _emit(ctx, st["pins"], f"pin {port} -> {target}; apply with `instance reload`")


@config_group.command("unpin")
@click.argument("port", type=int)
@click.pass_obj
@_guard
def config_unpin(ctx: Context, port: int) -> None:
    st = config.load_state(ctx.name)
    before = len(st["pins"])
    st["pins"] = [p for p in st["pins"] if p["port"] != port]
    if len(st["pins"]) == before:
        raise config.ConfigError(f"no pin on port {port}")
    config.save_state(ctx.name, st)
    _emit(ctx, st["pins"], f"removed pin {port}; apply with `instance reload`")


@config_group.command("render")
@click.option("--audit-only", is_flag=True, help="Only print the safety audit.")
@click.pass_obj
@_guard
def config_render(ctx: Context, audit_only: bool) -> None:
    """The exact config that would be pushed (node credentials included: it is printed locally only)."""
    cfg, text = instance.render(ctx.name)
    keys = {k: cfg.get(k) for k in ("mixed-port", "allow-lan", "bind-address", "external-controller", "listeners")}
    if audit_only or ctx.json_output:
        _emit(ctx, {"audit": "OK", **keys}, "audit OK\n" + json.dumps(keys, ensure_ascii=False, indent=2))
        return
    click.echo(text)


# ---------------------------------------------------------------- instance
@cli.group(name="instance")
def instance_group() -> None:
    """The running mihomo on 198 (as cltx, 127.0.0.1 only, no sudo, no iptables)."""


@instance_group.command("start")
@click.pass_obj
@_guard
def instance_start(ctx: Context) -> None:
    r = instance.start(ctx.name)
    _emit(ctx, r, f"{ctx.name} pid {r['pid']}  ports {r['ports']}  ctl {r['ctl_listening']}")


@instance_group.command("stop")
@click.pass_obj
@_guard
def instance_stop(ctx: Context) -> None:
    r = instance.stop(ctx.name)
    _emit(ctx, r, f"{ctx.name}: {r['result']}")


@instance_group.command("status")
@click.pass_obj
@_guard
def instance_status(ctx: Context) -> None:
    r = instance.status(ctx.name)
    human = (f"{ctx.name}: {'running pid ' + str(r['pid']) if r['running'] else 'not running'}\n"
             f"  ports {r['ports']}  ctl {r['ctl_listening']}\n  exposed beyond 127.0.0.1: {r['exposed'] or 'none'}")
    _emit(ctx, r, human)
    if r["exposed"]:
        raise SystemExit(2)


@instance_group.command("reload")
@click.pass_obj
@_guard
def instance_reload(ctx: Context) -> None:
    """Re-render from the cached subscription + settings and hot-apply (selections are kept)."""
    r = instance.reload(ctx.name)
    _emit(ctx, r, f"reloaded {ctx.name}: {r['config']['test']}")


@instance_group.command("list")
@click.pass_obj
@_guard
def instance_list(ctx: Context) -> None:
    """Every mihomo instance on 198 (all accounts): owner, running, listening ports."""
    rows = instance.list_remote()
    for r in rows:
        r["ports"] = " ".join(a.rsplit(":", 1)[-1] for a in r["listening"])
        r["exposed"] = [a for a in r["listening"] if not a.startswith("127.0.0.1:")]
    _emit(ctx, rows, _table(rows, ["instance", "account", "running", "ports", "exposed"]))
    if any(r["exposed"] for r in rows):
        raise SystemExit(2)


@instance_group.command("logs")
@click.option("--lines", type=int, default=50, show_default=True)
@click.pass_obj
@_guard
def instance_logs(ctx: Context, lines: int) -> None:
    out = instance.logs(ctx.name, lines)
    _emit(ctx, {"log": out}, out)


@instance_group.command("destroy")
@click.option("--yes", is_flag=True, required=True, help="Required: removes ~/mihomo-harness/<name> on 198.")
@click.pass_obj
@_guard
def instance_destroy(ctx: Context, yes: bool) -> None:
    r = instance.destroy(ctx.name)
    _emit(ctx, r, f"destroyed {ctx.name} ({r['result']})")


# ---------------------------------------------------------------- exit
@cli.group(name="exit")
def exit_group() -> None:
    """Which node each port exits through; switch, pin, measure, verify."""


@exit_group.command("ports")
@click.pass_obj
@_guard
def exit_ports(ctx: Context) -> None:
    """Every port and the node it exits through right now."""
    rows = exits.ports(ctx.name)
    _emit(ctx, rows, "\n".join(f"{r['port']}  {r['kind']:15s}  {' -> '.join(r['chain'])}" for r in rows))


@exit_group.command("groups")
@click.pass_obj
@_guard
def exit_groups(ctx: Context) -> None:
    rows = exits.groups(ctx.name)
    _emit(ctx, rows, _table(rows, ["type", "members", "now", "fixed", "leaf", "name"]))


@exit_group.command("nodes")
@click.option("--region")
@click.pass_obj
@_guard
def exit_nodes(ctx: Context, region: str | None) -> None:
    """Nodes as the running mihomo sees them (alive + last measured delay)."""
    rows = [n for n in exits.nodes(ctx.name) if not region or n["region"] == region.upper()]
    _emit(ctx, rows, _table(rows, ["region", "type", "alive", "last_delay", "name"]))


@exit_group.command("select")
@click.argument("target")
@click.option("--group", help="Selector to change (default: the instance's default group).")
@click.pass_obj
@_guard
def exit_select(ctx: Context, target: str, group: str | None) -> None:
    """Switch GROUP to TARGET now (no reload). On a url-test group this FIXES it until the node dies."""
    r = exits.select(ctx.name, target, group)
    _emit(ctx, r, f"{r['group']} -> {' -> '.join(r['chain'][1:])}")


@exit_group.command("unfix")
@click.argument("group")
@click.pass_obj
@_guard
def exit_unfix(ctx: Context, group: str) -> None:
    """Hand a url-test/fallback group back to automatic choice."""
    r = exits.unfix(ctx.name, group)
    _emit(ctx, r, f"{r['group']} auto again, now {r['now']}")


@exit_group.command("measure")
@click.argument("targets", nargs=-1)
@click.option("--region", help="Measure all nodes of this region.")
@click.option("--all", "all_nodes", is_flag=True, help="Measure every node.")
@click.option("--url", default=exits.SCHOLAR, show_default=True)
@click.option("--timeout-ms", type=click.IntRange(500, 30000), default=5000, show_default=True)
@click.pass_obj
@_guard
def exit_measure(ctx: Context, targets: tuple[str, ...], region: str | None, all_nodes: bool, url: str, timeout_ms: int) -> None:
    """mihomo URL test per node/group (in parallel). Default URL is Scholar, not generate_204."""
    names = list(targets)
    if all_nodes or region:
        names += [n["name"] for n in exits.nodes(ctx.name) if all_nodes or n["region"] == region.upper()]
    if not names:
        raise click.UsageError("give node/group names, --region XX, or --all")
    rows = exits.measure(ctx.name, names, url, timeout_ms)
    ok = [r for r in rows if r["delay_ms"] is not None]
    _emit(ctx, {"url": url, "ok": len(ok), "total": len(rows), "results": rows},
          _table(rows, ["delay_ms", "region", "name", "error"]) + f"\n{len(ok)}/{len(rows)} reachable via {url}")


@exit_group.command("verify")
@click.option("--port", "port_list", type=int, multiple=True, help="Default: every port of the instance.")
@click.pass_obj
@_guard
def exit_verify(ctx: Context, port_list: tuple[int, ...]) -> None:
    """Through each port: real exit IP + a real Scholar search with result count. Exit 1 if any fails."""
    rows = exits.verify(ctx.name, list(port_list) or None)
    human = "\n".join(f"{r['port']}  {'OK  ' if r['ok'] else 'FAIL'}  {r['exit'].get('ip')} {r['exit'].get('country')} "
                      f"{r['exit'].get('org')}  scholar {r['scholar_status']} results={r['scholar_results']} {r['scholar_ms']}ms"
                      for r in rows)
    _emit(ctx, rows, human)
    if not all(r["ok"] for r in rows):
        raise SystemExit(1)


@exit_group.command("table")
@click.option("--samples", type=click.IntRange(1, 9), default=3, show_default=True)
@click.pass_obj
@_guard
def exit_table(ctx: Context, samples: int) -> None:
    """Per port: exit node, real exit IP + country (ipinfo), median time to Kiro. The slot-handoff table."""
    rows = exits.table(ctx.name, samples)
    for r in rows:
        r["where"] = " ".join(x for x in (r["country"], r["city"]) if x) or "-"
    _emit(ctx, rows, _table(rows, ["port", "exit_ip", "where", "org", "kiro_ms", "kiro_ok", "exit_node"]))


@exit_group.command("geo")
@click.option("--all", "all_nodes", is_flag=True, help="Every node of the subscription (temporary instance).")
@click.option("--region", help="Only nodes whose name starts with this tag (still verified by IP).")
@click.option("--base-port", type=int, default=47000, show_default=True)
@click.option("--keep", is_flag=True, help="Do not destroy the temporary instance afterwards.")
@click.pass_obj
@_guard
def exit_geo(ctx: Context, all_nodes: bool, region: str | None, base_port: int, keep: bool) -> None:
    """Real exit country of every node: a throwaway instance pins one port per node, asks ipinfo serially.

    Node names lie (e.g. "DE - 德国2线" exits in France); this is how to answer "which countries are there".
    """
    if not (all_nodes or region):
        raise click.UsageError("--all or --region XX")
    data = subscription.load()
    names = [n["name"] for n in subscription.nodes(data) if all_nodes or n["region"] == region.upper()]
    tmp = f"{ctx.account}-geo"
    st = {**config.DEFAULT_STATE, "mixed_port": base_port - 1, "ctl_port": base_port - 2,
          "version": config.load_state(ctx.name).get("version"),
          "pins": [{"port": base_port + i, "target": n} for i, n in enumerate(names)]}
    config.save_state(tmp, st)
    try:
        instance.start(tmp)
        rows = exits.geo(tmp, [(base_port + i, n) for i, n in enumerate(names)])
    finally:
        if not keep:
            instance.destroy(tmp)
    by: dict[str, set[str]] = {}
    for r in rows:
        if r["ok"]:
            by.setdefault(str(r["country"]), set()).add(str(r["ip"]))
    summary = {c: len(ips) for c, ips in sorted(by.items(), key=lambda x: -len(x[1]))}
    mismatch = [r for r in rows if r["ok"] and r["label_region"] not in ("OTHER", r["country"], "UK" if r["country"] == "GB" else "")]
    _emit(ctx, {"account": ctx.account, "countries_distinct_ips": summary, "nodes": rows, "label_mismatch": mismatch},
          _table(rows, ["label_region", "country", "city", "ip", "org", "node", "error"])
          + f"\n\n{sum(r['ok'] for r in rows)}/{len(rows)} nodes answered; distinct exit IPs per country: {summary}"
          + ("".join(f"\nlabel says {r['label_region']} but exits {r['country']}: {r['node']}" for r in mismatch)))


# ---------------------------------------------------------------- shellcrash
@cli.group(name="shellcrash")
def shellcrash_group() -> None:
    """What ShellCrash would do to 198, read from its source (why this harness skips it)."""


@shellcrash_group.command("modes")
@click.pass_obj
def shellcrash_modes(ctx: Context) -> None:
    r = {"source": shellcrash.SOURCE_REV, "firewall_areas": shellcrash.FIREWALL_AREAS, "redir_modes": shellcrash.REDIR_MODES}
    _emit(ctx, r, f"source {shellcrash.SOURCE_REV}\n"
                  + _table(shellcrash.FIREWALL_AREAS, ["area", "label", "hijacks", "on_198"])
                  + "\n\nrouting (areas 1-3,5 only):\n" + _table(shellcrash.REDIR_MODES, ["mode", "tcp", "udp"]))


@shellcrash_group.command("facts")
@click.pass_obj
def shellcrash_facts(ctx: Context) -> None:
    _emit(ctx, shellcrash.FACTS, "\n".join(f"- {f['fact']}\n    {f['where']}" for f in shellcrash.FACTS))


@shellcrash_group.command("equivalents")
@click.pass_obj
def shellcrash_equivalents(ctx: Context) -> None:
    _emit(ctx, shellcrash.EQUIVALENTS, _table(shellcrash.EQUIVALENTS, ["shellcrash", "here"]))


# ---------------------------------------------------------------- repl
@cli.command()
@click.pass_obj
def repl(ctx: Context) -> None:
    """Interactive loop over the same commands."""
    skin = ReplSkin("mihomo", version=__version__)
    skin.print_banner()
    while True:
        try:
            line = input(f"mihomo[{ctx.account}/{ctx.name}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            return
        if not line:
            continue
        if line in {"exit", "quit"}:
            skin.print_goodbye()
            return
        try:
            cli.main(args=[*(["--json"] if ctx.json_output else []), "--account", ctx.account, "--name", ctx.name,
                           *shlex.split(line)], standalone_mode=False)
        except (click.ClickException, click.UsageError) as exc:
            skin.error(str(exc))
        except SystemExit:
            pass


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
