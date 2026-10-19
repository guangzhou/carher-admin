"""Read-only ops CLI for myai.carher.net (ai_token_dashboard on the 243 K3s host)."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

import click

from cli_anything.myai import __version__
from cli_anything.myai.core import diagnose, health, queries, upstream
from cli_anything.myai.core.remote import COMPONENTS, PROBE_POD, Remote, RemoteConfig, RemoteError, probe_pod_manifest
from cli_anything.myai.utils.repl_skin import ReplSkin


@dataclass
class Runtime:
    config: RemoteConfig
    use_json: bool
    remote: Remote


def _rt(ctx: click.Context) -> Runtime:
    runtime = ctx.find_object(Runtime)
    if runtime is None:
        raise click.ClickException("CLI runtime was not initialized")
    return runtime


def _emit(ctx: click.Context, data: Any, *, title: str | None = None) -> None:
    """JSON for agents; tables for list-of-dicts, key/value for dicts otherwise."""
    if _rt(ctx).use_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return
    if title:
        click.echo(f"{title}:")
    if isinstance(data, list) and data and all(isinstance(row, dict) for row in data):
        headers = list(data[0].keys())
        widths = {h: min(48, max(len(h), *(len(str(r.get(h, ""))) for r in data))) for h in headers}
        click.echo("  " + "  ".join(h.ljust(widths[h]) for h in headers))
        for row in data:
            click.echo("  " + "  ".join(str(row.get(h, ""))[: widths[h]].ljust(widths[h]) for h in headers))
    elif isinstance(data, list):
        click.echo("  (empty)" if not data else json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif isinstance(data, dict):
        for key, value in data.items():
            rendered = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)
            click.echo(f"  {key}: {rendered}")
    else:
        click.echo(str(data))


def _sql(ctx: click.Context, statement: str, title: str) -> None:
    _emit(ctx, _rt(ctx).remote.sql(statement), title=title)


@click.group(invoke_without_command=True)
@click.option("--json", "use_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--host", help="ssh target (default cltx@10.68.13.243 or MYAI_HOST).")
@click.option("--namespace", help="K8s namespace (default ai-token-dashboard).")
@click.option("--public-url", help="Public base URL (default https://chat.auto-link.com.cn/myai; CF 腿用 https://myai.carher.net).")
@click.option("--timeout", type=click.FloatRange(min=1), default=180.0, show_default=True)
@click.option("--probe-pod", "use_probe_pod", is_flag=True, help="Run upstream probes in the isolated pod/myai-probe (see `probe-pod up`).")
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context, use_json: bool, host: str | None, namespace: str | None, public_url: str | None, timeout: float, use_probe_pod: bool) -> None:
    """Inspect myai.carher.net: health, sync pipeline, refresh queue, billing, upstream.

    Every command is read-only. SSH auth: MYAI_SSH_PASSWORD (via sshpass env) or ssh keys.
    """
    config = RemoteConfig.from_env(
        host=host, namespace=namespace, public_url=public_url, timeout=timeout,
        probe_target=f"pod/{PROBE_POD}" if use_probe_pod else None,
    )
    ctx.obj = Runtime(config=config, use_json=use_json, remote=Remote(config))
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


# ---------------------------------------------------------------- status

@cli.command()
@click.option("--raw", is_flag=True, help="Print the full /api/health body instead of the summary.")
@click.pass_context
def status(ctx: click.Context, raw: bool) -> None:
    """Public /api/health (no auth) summarized into actionable findings."""
    body = health.fetch(_rt(ctx).config.public_url)
    _emit(ctx, body if raw else health.summarize(body), title="myai health")


@cli.command()
@click.pass_context
def pods(ctx: click.Context) -> None:
    """Deployments / statefulsets / pods / PVCs in the namespace."""
    out = _rt(ctx).remote.kubectl("get", "deploy,sts,pods,pvc", "-o", "wide")
    if _rt(ctx).use_json:
        _emit(ctx, {"kubectl": out})
    else:
        click.echo(out)


@cli.command()
@click.argument("component", type=click.Choice(sorted(COMPONENTS)))
@click.option("--since", default="30m", show_default=True, help="kubectl --since window.")
@click.option("--grep", "pattern", help="Only lines containing this text (case-insensitive).")
@click.option("--noise/--no-noise", default=False, help="Keep per-request HTTP/litellm lines (dropped by default).")
@click.option("--tail", type=int, default=200, show_default=True)
@click.pass_context
def logs(ctx: click.Context, component: str, since: str, pattern: str | None, noise: bool, tail: int) -> None:
    """Component logs, with per-request chatter filtered out by default."""
    out = _rt(ctx).remote.kubectl("logs", COMPONENTS[component], f"--since={since}")
    lines = out.splitlines()
    if not noise:
        lines = [l for l in lines if "HTTP Request:" not in l and "litellm request backend=" not in l and "/api/health" not in l]
    if pattern:
        lines = [l for l in lines if pattern.lower() in l.lower()]
    lines = lines[-tail:]
    if _rt(ctx).use_json:
        _emit(ctx, {"component": component, "lines": lines})
    else:
        click.echo("\n".join(lines))


# ---------------------------------------------------------------- sync

@cli.group()
def sync() -> None:
    """Usage sync pipeline: worker state, runs, errors, coverage."""


@sync.command("state")
@click.pass_context
def sync_state(ctx: click.Context) -> None:
    """Worker heartbeat + last ok + last snapshot publish."""
    _sql(ctx, queries.sync_state(), "sync state")


@sync.command("runs")
@click.option("--days", type=int, default=7, show_default=True)
@click.option("--recent", type=int, help="List the N most recent runs instead of the per-day rollup.")
@click.pass_context
def sync_runs(ctx: click.Context, days: int, recent: int | None) -> None:
    """Run outcomes per day (ok / partial / failed / skipped / running)."""
    _sql(ctx, queries.sync_runs_recent(recent) if recent else queries.sync_runs_by_day(days), "sync runs")


@sync.command("errors")
@click.option("--days", type=int, default=3, show_default=True)
@click.pass_context
def sync_errors(ctx: click.Context, days: int) -> None:
    """Most frequent error_message values (quality-gate reasons show up here)."""
    _sql(ctx, queries.sync_errors(days), "sync errors")


@sync.command("coverage")
@click.option("--days", type=int, default=14, show_default=True)
@click.pass_context
def sync_coverage(ctx: click.Context, days: int) -> None:
    """Which backend last synced each usage_date."""
    _sql(ctx, queries.coverage(days), "sync coverage")


@cli.command("queue")
@click.option("--all", "include_done", is_flag=True, help="Include completed requests.")
@click.pass_context
def queue(ctx: click.Context, include_done: bool) -> None:
    """Durable refresh queue (usage_refresh_requests)."""
    _sql(ctx, queries.refresh_queue(include_done), "refresh queue")


@cli.command("realtime")
@click.pass_context
def realtime(ctx: click.Context) -> None:
    """Per-backend realtime settlement and lag."""
    _sql(ctx, queries.realtime_settlement(), "realtime settlement")


# ---------------------------------------------------------------- usage / identity

@cli.group()
def usage() -> None:
    """Published usage snapshot (usage_daily)."""


@usage.command("daily")
@click.option("--days", type=int, default=14, show_default=True)
@click.option("--snapshot-only", is_flag=True, help="Raw usage_daily (historical snapshot) instead of the dashboard view.")
@click.pass_context
def usage_daily(ctx: click.Context, days: int, snapshot_only: bool) -> None:
    """Users / requests / spend per day per backend, as the dashboard sees it."""
    _sql(ctx, queries.usage_daily_totals(days, snapshot_only), "usage daily")


@usage.command("frontier")
@click.pass_context
def usage_frontier(ctx: click.Context) -> None:
    """Newest usage_date in the historical snapshot, per backend."""
    _sql(ctx, queries.snapshot_frontier(), "snapshot frontier")


@cli.group()
def identity() -> None:
    """Upstream account → person directory."""


@identity.command("find")
@click.argument("text")
@click.pass_context
def identity_find(ctx: click.Context, text: str) -> None:
    """Search usage_identity_directory by user id, name or email (substring)."""
    _sql(ctx, queries.identity_find(text), "identity")


# ---------------------------------------------------------------- billing

@cli.group()
def billing() -> None:
    """Orders (personal + organization) and the upstream sync outbox."""


@billing.command("orders")
@click.option("--status", help="Filter by order status.")
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
def billing_orders(ctx: click.Context, status: str | None, limit: int) -> None:
    """Personal and organization orders, newest first."""
    _sql(ctx, queries.billing_orders(status, limit), "billing orders")


@billing.command("outbox")
@click.option("--all", "include_sent", is_flag=True, help="Include sent rows.")
@click.pass_context
def billing_outbox(ctx: click.Context, include_sent: bool) -> None:
    """customer_outbox rows not yet sent (failed ones carry last_error)."""
    _sql(ctx, queries.outbox(include_sent), "outbox")


# ---------------------------------------------------------------- upstream

@cli.group("upstream")
def upstream_group() -> None:
    """Probe upstream LiteLLM from inside the sync worker (uses the pod's own keys)."""


@upstream_group.command("daily")
@click.option("--backend", "backends", multiple=True, type=click.Choice(sorted(upstream.BACKEND_ENV)), help="Repeatable; default both.")
@click.option("--days", type=int, default=14, show_default=True)
@click.option("--end", "end_date", type=click.DateTime(["%Y-%m-%d"]), help="Last day (default today).")
@click.pass_context
def upstream_daily(ctx: click.Context, backends: tuple[str, ...], days: int, end_date) -> None:
    """Global requests/spend per day as upstream reports them."""
    script = upstream.daily_activity_script(list(backends) or sorted(upstream.BACKEND_ENV), upstream.day_list(days, end_date.date() if end_date else None))
    data = upstream.parse_script_output(_rt(ctx).remote.worker_python(script))
    if _rt(ctx).use_json:
        _emit(ctx, data)
        return
    for name, entry in data["backends"].items():
        _emit(ctx, entry["days"], title=f"{name} ({entry['baseUrl'] or 'not configured'})")


@upstream_group.command("keys")
@click.option("--days", type=int, default=7, show_default=True)
@click.option("--end", "end_date", type=click.DateTime(["%Y-%m-%d"]), help="Last day (default today).")
@click.option("--top", type=int, default=15, show_default=True)
@click.pass_context
def upstream_keys(ctx: click.Context, days: int, end_date, top: int) -> None:
    """primary keys with spend, classed by whether their owner is in LiteLLM_UserTable."""
    day_list = upstream.day_list(days, end_date.date() if end_date else None)
    data = upstream.parse_script_output(_rt(ctx).remote.worker_python(upstream.keys_script(day_list[0], day_list[-1])))
    _emit(ctx, upstream.summarize_keys(data, top), title="upstream keys")


# ---------------------------------------------------------------- diagnose

@cli.group("diagnose")
def diagnose_group() -> None:
    """Composite checks that answer one ops question each."""


@diagnose_group.command("sync-stuck")
@click.option("--days", type=int, default=7, show_default=True, help="Each day costs one GET per her account (~300).")
@click.option("--end", "end_date", type=click.DateTime(["%Y-%m-%d"]), help="Last day (default today); pick a busy day as a positive control.")
@click.option("--ratio", type=float, default=diagnose.DEFAULT_RATIO, show_default=True, help="USAGE_SYNC_FALLBACK_MIN_NONEMPTY_RATIO in effect.")
@click.pass_context
def diagnose_sync_stuck(ctx: click.Context, days: int, end_date, ratio: float) -> None:
    """Is the her sparse-activity gate what keeps lastSuccessAt from moving?

    Replays the gate's own measurement per single day: carher-* accounts with
    any per-user daily-activity row, versus max(1, int(accounts × ratio)).
    """
    remote = _rt(ctx).remote
    probe = upstream.parse_script_output(remote.worker_python(diagnose.her_gate_script(upstream.day_list(days, end_date.date() if end_date else None))))
    if not probe.get("configured"):
        raise click.ClickException("sync worker 没有配置 HER_LITELLM_*;her 不参与同步,门禁不适用")
    report = diagnose.gate_report(int(probe["attemptedUsers"]), probe["days"], ratio)
    report["herBaseUrl"] = probe.get("baseUrl")
    report["recentErrors"] = remote.sql(queries.sync_errors(3))
    _emit(ctx, report, title="sync-stuck")


@diagnose_group.command("truncation")
@click.option("--backend", type=click.Choice(sorted(upstream.BACKEND_ENV)), default="primary", show_default=True)
@click.option("--days", type=int, default=14, show_default=True)
@click.option("--end", "end_date", type=click.DateTime(["%Y-%m-%d"]), help="Last day (default today).")
@click.pass_context
def diagnose_truncation(ctx: click.Context, backend: str, days: int, end_date) -> None:
    """Did the dashboard publish a day cut off at upstream's 10000-row spend-log cap?

    Upstream /spend/logs/v2 reports total<=10000 (total_pages<=100) however many
    rows exist; the log scan trusts total_pages and calls the window complete.
    """
    day_list = upstream.day_list(days, end_date.date() if end_date else None)
    remote = _rt(ctx).remote
    up = upstream.parse_script_output(remote.worker_python(upstream.daily_activity_script([backend], day_list)))
    view = remote.sql(queries.view_daily_by_backend(backend, day_list[0], day_list[-1]))
    _emit(ctx, diagnose.truncation_report(up["backends"][backend]["days"], view), title="truncation")


@diagnose_group.command("coverage")
@click.option("--days", type=int, default=14, show_default=True)
@click.option("--end", "end_date", type=click.DateTime(["%Y-%m-%d"]), help="Last day (default yesterday).")
@click.option("--threshold-pct", type=float, default=1.0, show_default=True)
@click.pass_context
def diagnose_coverage(ctx: click.Context, days: int, end_date, threshold_pct: float) -> None:
    """Per day: dashboard spend vs upstream global spend for primary.

    Catches every way usage goes missing — capped log scans, pruned raw logs
    published as zero, keys outside LiteLLM_UserTable — without knowing which.
    Upstream is UTC-dated; days the dashboard filled from the log scan are
    Beijing-dated, so a few % is boundary noise, not loss.
    """
    from datetime import date, timedelta

    end = end_date.date() if end_date else date.today() - timedelta(days=1)
    day_list = upstream.day_list(days, end)
    remote = _rt(ctx).remote
    up = upstream.parse_script_output(remote.worker_python(upstream.daily_activity_script(["primary"], day_list)))
    shown = remote.sql(queries.view_daily_by_attribution("primary", day_list[0], day_list[-1]))
    _emit(ctx, diagnose.coverage_report(up["backends"]["primary"]["days"], shown, threshold_pct), title="coverage")


# ---------------------------------------------------------------- probe pod

@cli.group("probe-pod")
def probe_pod() -> None:
    """Isolated pod for upstream probes (the only command group that creates anything)."""


@probe_pod.command("up")
@click.option("--image", default=None, help="Image tag (default: whatever usage-sync-worker runs).")
@click.option("--memory", default="1Gi", show_default=True)
@click.option("--ttl", "ttl_seconds", type=int, default=7200, show_default=True, help="Pod exits after this many seconds.")
@click.pass_context
def probe_pod_up(ctx: click.Context, image: str | None, memory: str, ttl_seconds: int) -> None:
    """Create pod/myai-probe with the sync worker's env; replaces an existing one."""
    remote = _rt(ctx).remote
    image = image or remote.kubectl("get", "deploy/usage-sync-worker", "-o", "jsonpath={.spec.template.spec.containers[0].image}").strip()
    remote.kubectl("delete", f"pod/{PROBE_POD}", "--ignore-not-found", "--wait=true")
    remote.run(
        f"kubectl -n {remote.config.namespace} apply -f -",
        stdin=probe_pod_manifest(image, memory, ttl_seconds),
    )
    remote.kubectl("wait", "--for=condition=Ready", f"pod/{PROBE_POD}", "--timeout=90s")
    _emit(ctx, {"pod": PROBE_POD, "image": image, "memory": memory, "ttlSeconds": ttl_seconds, "use": "add --probe-pod to upstream/diagnose commands"})


@probe_pod.command("down")
@click.pass_context
def probe_pod_down(ctx: click.Context) -> None:
    """Delete pod/myai-probe."""
    _rt(ctx).remote.kubectl("delete", f"pod/{PROBE_POD}", "--ignore-not-found", "--wait=false")
    _emit(ctx, {"pod": PROBE_POD, "deleted": True})


# ---------------------------------------------------------------- sql / db

@cli.group()
def db() -> None:
    """Direct read-only access to the usage Postgres."""


@db.command("tables")
@click.pass_context
def db_tables(ctx: click.Context) -> None:
    """Tables by size with live row estimates."""
    _sql(ctx, queries.table_sizes(), "tables")


@db.command("sql")
@click.argument("statement")
@click.pass_context
def db_sql(ctx: click.Context, statement: str) -> None:
    """Run one SELECT/WITH/EXPLAIN in a read-only transaction."""
    try:
        body = queries.validate_adhoc(statement)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    _sql(ctx, body, "sql")


# ---------------------------------------------------------------- repl

@cli.command()
@click.pass_context
def repl(ctx: click.Context) -> None:
    """Interactive shell over the same subcommands."""
    runtime = _rt(ctx)
    skin = ReplSkin("myai", version=__version__)
    skin.print_banner()
    skin.status_block(
        {"Host": runtime.config.host, "Namespace": runtime.config.namespace, "Public": runtime.config.public_url, "SSH auth": runtime.config.auth_mode},
        title="Connection",
    )
    session = skin.create_prompt_session()
    while True:
        try:
            line = skin.get_input(session, context="ops")
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            return
        if not line:
            continue
        if line in {"exit", "quit"}:
            skin.print_goodbye()
            return
        if line == "help":
            skin.help({
                "status": "public health summary",
                "diagnose sync-stuck": "why lastSuccessAt is not moving",
                "sync runs / errors / state": "sync pipeline",
                "queue": "refresh queue",
                "billing orders / outbox": "billing",
                "upstream daily": "upstream LiteLLM per-day totals",
                "db sql \"SELECT ...\"": "ad-hoc read-only SQL",
                "quit": "exit",
            })
            continue
        args = shlex.split(line)
        if runtime.use_json and "--json" not in args:
            args = ["--json", *args]
        try:
            cli.main(args=args, prog_name="cli-anything-myai", standalone_mode=False)
        except (click.ClickException, click.Abort, RemoteError, ValueError) as exc:
            skin.error(str(exc))
        except SystemExit as exc:
            if exc.code not in (0, None):
                skin.error(f"exit {exc.code}")


def main() -> None:
    try:
        cli(prog_name="cli-anything-myai")
    except RemoteError as exc:
        raise SystemExit(f"remote error: {exc}") from exc


if __name__ == "__main__":
    main()
