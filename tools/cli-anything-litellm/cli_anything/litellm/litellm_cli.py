from __future__ import annotations

import json
import shlex
from dataclasses import asdict, replace
from pathlib import Path
from urllib.parse import urlencode

import click

from cli_anything.litellm import __version__
from cli_anything.litellm.core import db as db_core
from cli_anything.litellm.core import patches as patches_core
from cli_anything.litellm.core import source as source_core
from cli_anything.litellm.core.config import inspect_config
from cli_anything.litellm.core.session import Session, load_session, normalize_base_url, save_session
from cli_anything.litellm.utils import remote198
from cli_anything.litellm.utils.litellm_backend import BackendError, LiteLLMBackend
from cli_anything.litellm.utils.repl_skin import ReplSkin


class Context:
    def __init__(
        self,
        session_path: Path,
        json_output: bool,
        dry_run: bool,
        api_key: str | None,
        timeout: float,
    ):
        self.session_path = session_path
        self.session = load_session(session_path)
        self.json_output = json_output
        self.dry_run = dry_run
        self.api_key = api_key
        self.timeout = timeout
        self.backend = LiteLLMBackend()


def _emit(ctx: Context, value: object, human: str | None = None) -> None:
    if ctx.json_output:
        click.echo(json.dumps(value, indent=2, sort_keys=True))
        return
    click.echo(human if human is not None else json.dumps(value, indent=2, sort_keys=True))


def _fail(ctx: Context, exc: Exception) -> None:
    payload: dict[str, object] = {"ok": False, "error": str(exc), "type": type(exc).__name__}
    if isinstance(exc, BackendError):
        payload = {**payload, "status_code": exc.status_code, "response": exc.response}
    if isinstance(exc, remote198.RemoteError) and exc.stderr:
        payload = {**payload, "stderr": exc.stderr}
    if ctx.json_output:
        raise click.ClickException(json.dumps(payload, sort_keys=True))
    raise click.ClickException(str(exc))


@click.group(invoke_without_command=True)
@click.option(
    "--session",
    "session_path",
    type=click.Path(path_type=Path),
    default=Path.home() / ".cli-anything" / "litellm.json",
    show_default=True,
)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--dry-run", is_flag=True, help="Do not persist profile mutations.")
@click.option("--api-key", envvar="LITELLM_PROXY_API_KEY", help="Proxy key; never saved to session.")
@click.option("--timeout", type=click.FloatRange(min=0.1), default=30.0, show_default=True)
@click.version_option(__version__)
@click.pass_context
def cli(
    click_ctx: click.Context,
    session_path: Path,
    json_output: bool,
    dry_run: bool,
    api_key: str | None,
    timeout: float,
) -> None:
    """Agent-friendly CLI for the real LiteLLM package and proxy API."""
    try:
        click_ctx.obj = Context(session_path, json_output, dry_run, api_key, timeout)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if click_ctx.invoked_subcommand is None:
        click_ctx.invoke(repl)


@cli.group()
def backend() -> None:
    """Inspect the installed LiteLLM backend."""


@backend.command("status")
@click.pass_obj
def backend_status(ctx: Context) -> None:
    value = ctx.backend.status()
    _emit(ctx, value, f"LiteLLM {value['version'] or 'not installed'}")


@cli.group()
def profile() -> None:
    """Manage non-secret defaults persisted for agents."""


@profile.command("show")
@click.pass_obj
def profile_show(ctx: Context) -> None:
    _emit(ctx, asdict(ctx.session))


@profile.command("set")
@click.option("--base-url")
@click.option("--model")
@click.pass_obj
def profile_set(ctx: Context, base_url: str | None, model: str | None) -> None:
    if base_url is None and model is None:
        raise click.UsageError("provide --base-url and/or --model")
    try:
        updated = replace(
            ctx.session,
            base_url=ctx.session.base_url if base_url is None else normalize_base_url(base_url),
            model=ctx.session.model if model is None else model or None,
        )
        if not ctx.dry_run:
            save_session(ctx.session_path, updated)
        ctx.session = updated
        _emit(ctx, {**asdict(updated), "saved": not ctx.dry_run})
    except ValueError as exc:
        _fail(ctx, exc)


@profile.command("reset")
@click.pass_obj
def profile_reset(ctx: Context) -> None:
    updated = Session()
    if not ctx.dry_run:
        save_session(ctx.session_path, updated)
    ctx.session = updated
    _emit(ctx, {**asdict(updated), "saved": not ctx.dry_run})


@cli.group(name="config")
def config_group() -> None:
    """Inspect LiteLLM proxy YAML safely."""


@config_group.command("inspect")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_obj
def config_inspect(ctx: Context, path: Path) -> None:
    try:
        _emit(ctx, inspect_config(path))
    except ValueError as exc:
        _fail(ctx, exc)


@cli.group()
def proxy() -> None:
    """Call the real LiteLLM proxy HTTP API."""


@proxy.command("health")
@click.option("--ready", is_flag=True, help="Use /health/readiness instead of /health.")
@click.pass_obj
def proxy_health(ctx: Context, ready: bool) -> None:
    try:
        value = ctx.backend.request(
            "GET",
            ctx.session.base_url,
            "/health/readiness" if ready else "/health",
            ctx.api_key,
            None,
            ctx.timeout,
        )
        _emit(ctx, value)
    except BackendError as exc:
        _fail(ctx, exc)


@proxy.command("models")
@click.pass_obj
def proxy_models(ctx: Context) -> None:
    try:
        _emit(
            ctx,
            ctx.backend.request("GET", ctx.session.base_url, "/v1/models", ctx.api_key, None, ctx.timeout),
        )
    except BackendError as exc:
        _fail(ctx, exc)


@proxy.command("chat")
@click.argument("prompt")
@click.option("--model")
@click.option("--system")
@click.option("--temperature", type=float)
@click.option("--max-tokens", type=int)
@click.pass_obj
def proxy_chat(
    ctx: Context,
    prompt: str,
    model: str | None,
    system: str | None,
    temperature: float | None,
    max_tokens: int | None,
) -> None:
    selected_model = model or ctx.session.model
    if selected_model is None:
        raise click.UsageError("provide --model or save one with profile set --model")
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": prompt}
    ]
    payload: dict[str, object] = {"model": selected_model, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    try:
        _emit(
            ctx,
            ctx.backend.request(
                "POST",
                ctx.session.base_url,
                "/v1/chat/completions",
                ctx.api_key,
                payload,
                ctx.timeout,
            ),
        )
    except BackendError as exc:
        _fail(ctx, exc)


@proxy.command("request")
@click.argument("method")
@click.argument("path")
@click.option("--json-body", "json_body")
@click.pass_obj
def proxy_request(ctx: Context, method: str, path: str, json_body: str | None) -> None:
    """Send a bounded JSON request to a proxy API endpoint."""
    payload = None
    if json_body is not None:
        try:
            value = json.loads(json_body)
        except json.JSONDecodeError as exc:
            raise click.UsageError(f"--json-body must be valid JSON: {exc}") from exc
        if value is not None and not isinstance(value, dict):
            raise click.UsageError("--json-body must contain a JSON object")
        payload = value
    if ctx.dry_run:
        _emit(ctx, {"dry_run": True, "method": method.upper(), "path": path, "body": payload})
        return
    try:
        _emit(ctx, ctx.backend.request(method.upper(), ctx.session.base_url, path, ctx.api_key, payload, ctx.timeout))
    except BackendError as exc:
        _fail(ctx, exc)


@cli.group()
def organization() -> None:
    """Manage LiteLLM organizations with a proxy admin key."""


@organization.command("list")
@click.option("--alias", "organization_alias")
@click.option("--id", "organization_id")
@click.pass_obj
def organization_list(ctx: Context, organization_alias: str | None, organization_id: str | None) -> None:
    query = urlencode(
        {
            key: value
            for key, value in (("org_alias", organization_alias), ("org_id", organization_id))
            if value is not None
        }
    )
    path = "/organization/list" + (f"?{query}" if query else "")
    try:
        _emit(ctx, ctx.backend.request("GET", ctx.session.base_url, path, ctx.api_key, None, ctx.timeout))
    except BackendError as exc:
        _fail(ctx, exc)


@organization.command("create")
@click.argument("alias")
@click.option("--model", "models", multiple=True)
@click.option("--max-budget", type=click.FloatRange(min=0))
@click.pass_obj
def organization_create(ctx: Context, alias: str, models: tuple[str, ...], max_budget: float | None) -> None:
    payload: dict[str, object] = {"organization_alias": alias}
    if models:
        payload["models"] = list(models)
    if max_budget is not None:
        payload["max_budget"] = max_budget
    if ctx.dry_run:
        _emit(ctx, {"dry_run": True, "request": payload})
        return
    try:
        _emit(
            ctx,
            ctx.backend.request(
                "POST",
                ctx.session.base_url,
                "/organization/new",
                ctx.api_key,
                payload,
                ctx.timeout,
            ),
        )
    except BackendError as exc:
        _fail(ctx, exc)


LANE_OPTION = click.option(
    "--lane", type=click.Choice(sorted(remote198.LANES)), default="prod", show_default=True,
    help="198 lane (ns litellm-product).",
)
# src commands: a lane picks the tag (= its live version). An explicit --tag skips that lookup
# unless --lane was also given explicitly, in which case the two must agree.
SRC_LANE_OPTION = click.option(
    "--lane", type=click.Choice(sorted(remote198.LANES)), default=None,
    help="198 lane whose live version picks the tag [default: prod when --tag is absent].",
)


def _src_tag(tag: str | None, lane: str | None) -> str:
    if lane is None and tag is None:
        lane = "prod"
    resolved, _ = source_core.resolve_tag(lane, tag)  # lane given => tag must equal its live version
    return resolved


@cli.group(name="src")
def src_group() -> None:
    """Read the upstream LiteLLM source a 198 lane is built from (fork tag, read-only)."""


@src_group.command("path")
@click.pass_obj
def src_path(ctx: Context) -> None:
    """The request path through LiteLLM and which of our patches hook each step."""
    rows = list(source_core.REQUEST_PATH)
    human = "\n".join(
        f"{i}. {r['step']}\n   where: {r['where']}\n   does:  {r['does']}"
        + ("".join(f"\n   hook:  {h}" for h in r["hooks"]) if r["hooks"] else "")
        for i, r in enumerate(rows, 1)
    )
    _emit(ctx, rows, human)


@src_group.command("find")
@click.argument("symbol")
@click.option("--tag", help="Fork tag; defaults to the lane's live version.")
@click.option("--path", "path_glob", help="Limit to one file or pathspec, e.g. litellm/router.py")
@SRC_LANE_OPTION
@click.pass_obj
def src_find(ctx: Context, symbol: str, tag: str | None, path_glob: str | None, lane: str | None) -> None:
    """Find `def SYMBOL` / `class SYMBOL` (or Class.method) in the tag."""
    try:
        tag = _src_tag(tag, lane)
        hits = source_core.find_symbol(tag, symbol, path_glob)
        _emit(ctx, {"tag": tag, "symbol": symbol, "hits": hits},
              "\n".join(f"{tag}:{h['path']}:{h['line']}  {h['text']}" for h in hits) or f"{symbol}: not found in {tag}")
    except remote198.RemoteError as exc:
        _fail(ctx, exc)


@src_group.command("show")
@click.argument("location")
@click.option("--tag", help="Fork tag; defaults to the lane's live version.")
@click.option("--max-lines", type=click.IntRange(1, 2000), default=200, show_default=True)
@SRC_LANE_OPTION
@click.pass_obj
def src_show(ctx: Context, location: str, tag: str | None, max_lines: int, lane: str | None) -> None:
    """Print the def/class at PATH:LINE (as printed by `src find`)."""
    path, _, line = location.rpartition(":")
    if not path or not line.isdigit():
        raise click.UsageError("LOCATION must be PATH:LINE, e.g. litellm/router.py:7915")
    try:
        tag = _src_tag(tag, lane)
        body = source_core.show_symbol(tag, path, int(line), max_lines)
        _emit(ctx, {"tag": tag, "path": path, "line": int(line), "source": body}, f"# {tag}:{path}\n{body}")
    except remote198.RemoteError as exc:
        _fail(ctx, exc)


@cli.group(name="patches")
def patches_group() -> None:
    """Which of our patches own which upstream symbols on a live 198 lane."""


@patches_group.command("targets")
@LANE_OPTION
@click.pass_obj
def patches_targets(ctx: Context, lane: str) -> None:
    """Probe a live pod: for each patched upstream symbol, who replaced it (by object identity)."""
    try:
        data = patches_core.probe_lane(lane)
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    rows = [{"symbol": k, "owner": patches_core.owner_of(k, t), **t} for k, t in data["targets"].items()]
    lines = [f"lane={lane} pod={data['pod']} litellm={data['live_version']} callbacks={len(data['callbacks'])}"]
    for e in data["import_errors"]:
        lines.append(f"  IMPORT ERROR {e['callback']}: {e['error']}")
    for r in rows:
        lines.append(f"  {r['symbol']:62s} {r['owner']}")
    _emit(ctx, {**data, "rows": rows}, "\n".join(lines))
    if data["import_errors"]:
        raise click.exceptions.Exit(1)


@patches_group.command("compare")
@click.pass_obj
def patches_compare(ctx: Context) -> None:
    """Probe all three lanes and show where their patch ownership differs."""
    try:
        probes = [patches_core.probe_lane(l) for l in ("prod", "gray", "guarded-old")]
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    rows = patches_core.compare_lanes(probes)
    versions = {p["lane"]: p["live_version"] for p in probes}
    lines = [f"versions: {versions}"]
    for r in rows:
        if r["same"]:
            lines.append(f"  {r['symbol']:62s} {r['owners']['prod']}")
        else:
            lines.append(f"  {r['symbol']:62s} DIFFERS " + " | ".join(f"{k}={v}" for k, v in r["owners"].items()))
    lines.append(f"{sum(r['same'] for r in rows)}/{len(rows)} symbols identical across lanes")
    _emit(ctx, {"versions": versions, "rows": rows}, "\n".join(lines))


@patches_group.command("purpose")
@click.argument("name", required=False)
@click.pass_obj
def patches_purpose(ctx: Context, name: str | None) -> None:
    """One-line purpose of each patch module (catalog, not a ruler)."""
    items = patches_core.PURPOSE if name is None else {name: patches_core.PURPOSE.get(name, "(not in catalog)")}
    _emit(ctx, items, "\n".join(f"{k:36s} {v}" for k, v in items.items()))


@cli.group(name="db")
def db_group() -> None:
    """Read-only views of the 198 production Postgres (ns litellm-product)."""


@db_group.command("anchor")
@click.pass_obj
def db_anchor(ctx: Context) -> None:
    """Prove you are on the prod DB (dev has same-named objects) and show connection headroom."""
    try:
        a = db_core.anchor()
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    _emit(ctx, a, "\n".join(f"{k:18s} {v}" for k, v in a.items()))


@db_group.command("catalog")
@click.option("--tag", help="Schema tag; defaults to prod's live version.")
@click.pass_obj
def db_catalog(ctx: Context, tag: str | None) -> None:
    """schema.prisma of the tag vs the live DB: missing tables, extras, largest, migrations."""
    try:
        tag, _ = source_core.resolve_tag("prod" if tag is None else None, tag)
        c = db_core.catalog(tag)
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    lines = [
        f"schema {tag}: {c['schema_models']} models; missing in DB: {c['missing_in_db'] or 'none'}",
        f"migrations: {c['migrations']}",
        f"views (created by proxy at startup): {len(c['views'])}",
        f"non-schema tables in public (ops backups etc.): {len(c['non_schema_tables'])}",
        f"other schemas: {c['other_schemas']}",
        "largest:",
    ] + [f"  {r['table']:40s} rows~{r['est_rows']:>10,}  {r['bytes'] / 2**30:8.2f} GiB" for r in c["largest"]]
    _emit(ctx, c, "\n".join(lines))


@db_group.command("config-owner")
@click.pass_obj
def db_config_owner(ctx: Context) -> None:
    """For router/general settings: does config.yaml or the DB row own each key (1.105 rule: config wins)."""
    try:
        o = db_core.config_ownership()
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    lines = []
    for section, items in o.items():
        lines.append(f"[{section}]")
        lines += [f"  {i['key']:44s} {i['owner']:22s} {i['value']}" for i in items]
    _emit(ctx, o, "\n".join(lines))


@db_group.command("spend-reconcile")
@click.argument("date")
@click.option("--model-group", "model_group_like", help="Filter, * as wildcard, e.g. 'kiro-claude-*'")
@click.option("--limit", type=click.IntRange(1, 200), default=15, show_default=True)
@click.pass_obj
def db_spend_reconcile(ctx: Context, date: str, model_group_like: str | None, limit: int) -> None:
    """DailyUserSpend requests vs SpendLogs rows per key for DATE (UTC). Positive gap = dropped rows."""
    try:
        r = db_core.spend_reconcile(date, model_group_like, limit)
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    t = r["totals"]
    lines = [f"{date} {model_group_like or 'all groups'}: daily={t.get('daily_requests')} spendlogs={t.get('spendlog_rows')}",
             f"  {'key':10s} {'alias':40s} {'daily':>7s} {'logs':>7s} {'gap':>6s}"]
    lines += [f"  {k['key_prefix']:10s} {k['key_alias'][:40]:40s} {k['daily_requests']:>7s} {k['spendlog_rows']:>7s} {k['gap']:>6s}"
              for k in r["keys"]]
    _emit(ctx, r, "\n".join(lines))


@db_group.command("retention")
@click.pass_obj
def db_retention(ctx: Context) -> None:
    """How far back SpendLogs (detail) and DailyUserSpend (aggregate) reach."""
    try:
        r = db_core.spendlog_retention()
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    _emit(ctx, r, "\n".join(f"{k:18s} {v}" for k, v in r.items()))


@db_group.command("req")
@click.argument("req_id")
@click.option("--hours", type=click.IntRange(1, 168), default=48, show_default=True)
@click.pass_obj
def db_req(ctx: Context, req_id: str, hours: int) -> None:
    """Real error behind a client's 'API 异常 (req: xxxxxxxx)' (8-hex prefix or full id)."""
    try:
        rows = db_core.request_lookup(req_id, hours)
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    if not rows:
        human = (f"no SpendLogs row for {req_id} in the last {hours}h — widen --hours, or the row was dropped "
                 "(null-byte sanitizer drops ~1.3%; check DailyUserSpend), or it never reached this proxy")
    else:
        human = "\n".join("\n".join(f"{k:22s} {v}" for k, v in r.items()) for r in rows)
    _emit(ctx, rows, human)


@db_group.command("failure-rate")
@click.argument("groups", nargs=-1, required=True)
@click.option("--minutes", type=click.IntRange(5, 90), default=60, show_default=True,
              help="Window. model_group/status have no index: 60min ~100s, 3h and 5h timed out (10-20).")
@click.option("--bucket", type=click.IntRange(1, 60), default=10, show_default=True, help="Bucket size in minutes.")
@click.option("--needle", default="overloaded", show_default=True, help="Count failures whose error text contains this.")
@click.pass_obj
def db_failure_rate(ctx: Context, groups: tuple[str, ...], minutes: int, bucket: int, needle: str) -> None:
    """Requests / failures / NEEDLE-failures / rescued-by-other-group per bucket, keyed by the group the client asked for."""
    try:
        rows = db_core.failure_rate(list(groups), minutes, bucket, needle)
    except remote198.RemoteError as exc:
        _fail(ctx, exc)
        return
    lines = [f"  {'time(UTC)':12s} {'n':>6s} {'fail':>5s} {'%':>5s} {needle[:10]:>10s} {'rescued':>8s}"]
    for r in rows:
        n, f = int(r["n"]), int(r["fail"])
        lines.append(f"  {r['t']:12s} {n:6d} {f:5d} {100 * f / n if n else 0:5.1f} {r['matched']:>10s} {r['rescued_elsewhere']:>8s}")
    _emit(ctx, rows, "\n".join(lines))


@cli.group(context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
def native() -> None:
    """Invoke upstream LiteLLM executables without a shell."""


def _native_command(name: str):
    @native.command(name, context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
    @click.argument("args", nargs=-1, type=click.UNPROCESSED)
    @click.pass_obj
    def command(ctx: Context, args: tuple[str, ...]) -> None:
        try:
            result = ctx.backend.run_native("lite" if name == "lite" else "litellm", args, ctx.timeout)
            _emit(ctx, result, str(result["stdout"]) + str(result["stderr"]))
            if result["returncode"] != 0:
                raise click.exceptions.Exit(int(result["returncode"]))
        except BackendError as exc:
            _fail(ctx, exc)

    return command


_native_command("lite")
_native_command("server")


@cli.command()
@click.pass_obj
def repl(ctx: Context) -> None:
    """Start an interactive command loop."""
    skin = ReplSkin("litellm", version=__version__)
    skin.print_banner()
    while True:
        try:
            line = input("litellm> ").strip()
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            return
        if not line:
            continue
        if line in {"exit", "quit"}:
            skin.print_goodbye()
            return
        try:
            args = shlex.split(line)
            cli.main(
                args=[
                    "--session",
                    str(ctx.session_path),
                    *(["--json"] if ctx.json_output else []),
                    *args,
                ],
                standalone_mode=False,
            )
        except (click.ClickException, click.UsageError) as exc:
            skin.error(str(exc))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
