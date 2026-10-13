"""Click CLI that maps directly onto Sub2API's administrator HTTP API."""

from __future__ import annotations

import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import click

from cli_anything.sub2api import __version__
from cli_anything.sub2api.core.client import ConnectionConfig, Sub2APIClient, Sub2APIError
from cli_anything.sub2api.core.operations import (
    CRUD_RESOURCES,
    account_list_query,
    build_grok_extra,
    build_grok_oauth_credentials,
    is_sensitive_export_path,
    parse_ids,
    parse_json_data,
    parse_params,
    write_json_file,
)
from cli_anything.sub2api.core.profile import DEFAULT_PROFILE_PATH, load_profile, save_profile
from cli_anything.sub2api.utils.repl_skin import ReplSkin


@dataclass
class Runtime:
    """Request-scoped CLI configuration; this never writes credentials to disk."""

    config: ConnectionConfig
    use_json: bool
    dry_run: bool
    repl_mode: bool = False

    @property
    def client(self) -> Sub2APIClient:
        return Sub2APIClient(self.config)


def _runtime(ctx: click.Context) -> Runtime:
    runtime = ctx.find_object(Runtime)
    if runtime is None:
        raise click.ClickException("CLI runtime was not initialized")
    return runtime


def _safe_base_url(value: str) -> str:
    parsed = urlsplit(value)
    netloc = parsed.hostname or parsed.netloc
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _emit(ctx: click.Context, data: Any, *, title: str | None = None) -> None:
    """Emit JSON for agents and compact native data for a human terminal."""
    runtime = _runtime(ctx)
    if runtime.use_json:
        click.echo(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str))
        return
    if title:
        click.echo(f"{title}:")
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            else:
                rendered = str(value)
            click.echo(f"  {key}: {rendered}")
    elif isinstance(data, list):
        click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif data is None:
        click.echo("ok")
    else:
        click.echo(str(data))


def _planned(method: str, path: str, payload: Any = None, query: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {"dry_run": True, "method": method.upper(), "path": path}
    if query:
        result["query"] = query
    if payload is not None:
        result["payload"] = payload
    return result


def _mutate(
    ctx: click.Context,
    method: str,
    path: str,
    *,
    payload: Any = None,
    headers: dict[str, str] | None = None,
) -> Any:
    runtime = _runtime(ctx)
    if runtime.dry_run:
        return _planned(method, path, payload=payload)
    return runtime.client.admin(method, path, payload=payload, extra_headers=headers)


def _list_resource(ctx: click.Context, resource: str, query: dict[str, Any] | None = None) -> None:
    _emit(ctx, _runtime(ctx).client.admin("GET", CRUD_RESOURCES[resource], query=query), title=resource)


@click.group(invoke_without_command=True)
@click.option("--json", "use_json", is_flag=True, help="Emit machine-readable JSON.")
@click.option("--base-url", help="Sub2API root URL; overrides SUB2API_BASE_URL and the saved profile.")
@click.option("--target", type=click.Choice(["198", "198-dev"], case_sensitive=False), help="Known deployment target; 198 resolves to the 198 dev NodePort.")
@click.option("--admin-api-key", help="Administrator API key; defaults to SUB2API_ADMIN_API_KEY.")
@click.option("--jwt", help="Administrator JWT; defaults to SUB2API_JWT.")
@click.option(
    "--profile-file",
    type=click.Path(path_type=Path),
    default=DEFAULT_PROFILE_PATH,
    show_default=True,
    help="Non-secret profile file containing only the base URL.",
)
@click.option("--timeout", type=click.FloatRange(min=0.1), default=30.0, show_default=True)
@click.option("--dry-run", is_flag=True, help="Print mutating API requests without sending them.")
@click.version_option(__version__)
@click.pass_context
def cli(
    ctx: click.Context,
    use_json: bool,
    base_url: str | None,
    target: str | None,
    admin_api_key: str | None,
    jwt: str | None,
    profile_file: Path,
    timeout: float,
    dry_run: bool,
) -> None:
    """Operate a deployed Sub2API instance through its native administrator API."""
    ctx.obj = Runtime(
        config=ConnectionConfig.from_sources(
            base_url=base_url,
            target=target,
            admin_api_key=admin_api_key,
            jwt=jwt,
            timeout=timeout,
            profile_path=profile_file,
        ),
        use_json=use_json,
        dry_run=dry_run,
    )
    if ctx.invoked_subcommand is None:
        ctx.invoke(repl)


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Verify native health and, when authenticated, report the deployed version."""
    runtime = _runtime(ctx)
    runtime.config.validate(require_auth=False)
    health = runtime.client.health()
    result: dict[str, Any] = {
        "base_url": _safe_base_url(runtime.config.base_url),
        "profile_path": str(runtime.config.profile_path),
        "auth_configured": runtime.config.auth_scheme is not None,
        "auth_scheme": runtime.config.auth_scheme,
        "health": health,
    }
    if runtime.config.auth_scheme:
        result["system"] = runtime.client.admin("GET", "/admin/system/version")
    _emit(ctx, result, title="Sub2API status")


@cli.group()
def profile() -> None:
    """Manage the non-secret local connection profile."""


@profile.command("show")
@click.pass_context
def profile_show(ctx: click.Context) -> None:
    """Show the saved base URL without reading or displaying credentials."""
    path = _runtime(ctx).config.profile_path
    data = load_profile(path)
    _emit(ctx, {"profile_path": str(path), "base_url": data.get("base_url")}, title="Profile")


@profile.command("set")
@click.argument("base_url")
@click.option("--target", type=click.Choice(["198", "198-dev"], case_sensitive=False))
@click.pass_context
def profile_set(ctx: click.Context, base_url: str, target: str | None) -> None:
    """Save a base URL locally; API keys and JWTs are deliberately excluded."""
    try:
        result = save_profile(base_url, _runtime(ctx).config.profile_path, target=target)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    _emit(ctx, result, title="Profile saved")


@cli.group()
def accounts() -> None:
    """Inspect and operate upstream provider accounts through admin account routes."""


@accounts.command("list")
@click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--page-size", type=click.IntRange(min=1, max=500), default=200, show_default=True)
@click.option("--search")
@click.option("--platform")
@click.option("--type", "account_type")
@click.option("--status")
@click.option("--group")
@click.option("--privacy-mode")
@click.option("--sort-by", default="name", show_default=True)
@click.option("--sort-order", type=click.Choice(["asc", "desc"]), default="asc", show_default=True)
@click.option("--lite/--full", default=True, show_default=True)
@click.pass_context
def accounts_list(
    ctx: click.Context,
    page: int,
    page_size: int,
    search: str | None,
    platform: str | None,
    account_type: str | None,
    status: str | None,
    group: str | None,
    privacy_mode: str | None,
    sort_by: str,
    sort_order: str,
    lite: bool,
) -> None:
    """List native account records with the same filters as the admin console."""
    query = account_list_query(
        page=page,
        page_size=page_size,
        search=search,
        platform=platform,
        account_type=account_type,
        status=status,
        group=group,
        privacy_mode=privacy_mode,
        sort_by=sort_by,
        sort_order=sort_order,
        lite=lite,
    )
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/accounts", query=query), title="Accounts")


@accounts.command("get")
@click.argument("account_id", type=int)
@click.pass_context
def accounts_get(ctx: click.Context, account_id: int) -> None:
    """Fetch one upstream account by ID."""
    _emit(ctx, _runtime(ctx).client.admin("GET", f"/admin/accounts/{account_id}"), title="Account")


@accounts.command("create")
@click.option("--data", help="Account create JSON.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def accounts_create(ctx: click.Context, data: str | None, file_path: Path | None) -> None:
    """Create an account using the native account payload schema."""
    payload = parse_json_data(data, file_path)
    _emit(ctx, _mutate(ctx, "POST", "/admin/accounts", payload=payload), title="Account created")


@accounts.command("update")
@click.argument("account_id", type=int)
@click.option("--data", help="Partial account update JSON.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def accounts_update(ctx: click.Context, account_id: int, data: str | None, file_path: Path | None) -> None:
    """Apply a partial update to one account with the backend's native semantics."""
    payload = parse_json_data(data, file_path)
    _emit(ctx, _mutate(ctx, "PUT", f"/admin/accounts/{account_id}", payload=payload), title="Account updated")


@accounts.command("delete")
@click.argument("account_id", type=int)
@click.confirmation_option(prompt="Delete this account?")
@click.pass_context
def accounts_delete(ctx: click.Context, account_id: int) -> None:
    """Delete one account after explicit terminal confirmation."""
    _emit(ctx, _mutate(ctx, "DELETE", f"/admin/accounts/{account_id}"), title="Account deleted")


def _account_read_command(name: str, suffix: str, help_text: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
    def decorator(_: Callable[..., None]) -> Callable[..., None]:
        @accounts.command(name)
        @click.argument("account_id", type=int)
        @click.pass_context
        def command(ctx: click.Context, account_id: int) -> None:
            """Read a native account diagnostic resource."""
            _emit(ctx, _runtime(ctx).client.admin("GET", f"/admin/accounts/{account_id}/{suffix}"), title=help_text)

        return command

    return decorator


@_account_read_command("usage", "usage", "Account usage")
def _accounts_usage() -> None:
    pass


@_account_read_command("stats", "stats", "Account statistics")
def _accounts_stats() -> None:
    pass


@_account_read_command("today-stats", "today-stats", "Account daily statistics")
def _accounts_today_stats() -> None:
    pass


@_account_read_command("models", "models", "Available account models")
def _accounts_models() -> None:
    pass


@accounts.command("set-schedulable")
@click.argument("account_id", type=int)
@click.argument("enabled", type=click.BOOL)
@click.pass_context
def accounts_set_schedulable(ctx: click.Context, account_id: int, enabled: bool) -> None:
    """Enable or disable an account for scheduler selection."""
    _emit(
        ctx,
        _mutate(ctx, "POST", f"/admin/accounts/{account_id}/schedulable", payload={"schedulable": enabled}),
        title="Account scheduling updated",
    )


@accounts.command("set-status")
@click.argument("account_id", type=int)
@click.argument("status")
@click.pass_context
def accounts_set_status(ctx: click.Context, account_id: int, status: str) -> None:
    """Set the backend account status field."""
    _emit(ctx, _mutate(ctx, "PUT", f"/admin/accounts/{account_id}", payload={"status": status}), title="Account status updated")


@accounts.command("action")
@click.argument("account_id", type=int)
@click.argument(
    "action",
    type=click.Choice(
        ["clear-error", "clear-rate-limit", "recover-state", "reset-quota", "refresh", "test", "sync-models"],
        case_sensitive=False,
    ),
)
@click.pass_context
def accounts_action(ctx: click.Context, account_id: int, action: str) -> None:
    """Run a supported native account action without inventing business behavior."""
    path_action = "models/sync-upstream" if action == "sync-models" else action
    _emit(ctx, _mutate(ctx, "POST", f"/admin/accounts/{account_id}/{path_action}"), title=f"Account {action}")


@accounts.command("bulk-update")
@click.option("--ids", required=True, help="Comma-separated account IDs.")
@click.option("--data", help="Fields to update as JSON.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def accounts_bulk_update(ctx: click.Context, ids: str, data: str | None, file_path: Path | None) -> None:
    """Update many accounts through the native bulk-update endpoint."""
    fields = parse_json_data(data, file_path)
    if not isinstance(fields, dict):
        raise click.ClickException("Account bulk update data must be a JSON object")
    _emit(
        ctx,
        _mutate(ctx, "POST", "/admin/accounts/bulk-update", payload={"account_ids": parse_ids(ids), **fields}),
        title="Accounts bulk updated",
    )


@cli.group()
def grok() -> None:
    """Operate Grok/xAI OAuth administration routes."""


@grok.group()
def oauth() -> None:
    """Inspect Grok OAuth capabilities and perform one-shot authorization."""


@oauth.command("capabilities")
@click.pass_context
def grok_oauth_capabilities(ctx: click.Context) -> None:
    """Read whether optional Grok password authorization is enabled."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/grok/oauth/capabilities"), title="Grok OAuth capabilities")


@grok.command("import-password")
@click.option("--email", required=True, help="Grok account email.")
@click.option("--password-stdin", is_flag=True, help="Read the password from stdin; never pass it in argv.")
@click.option("--name", help="Sub2API account display name; defaults to the returned email.")
@click.option("--group-ids", help="Optional comma-separated Sub2API group IDs.")
@click.option("--concurrency", type=click.IntRange(min=0), default=1, show_default=True)
@click.option("--priority", type=int, default=0, show_default=True)
@click.pass_context
def grok_import_password(
    ctx: click.Context,
    email: str,
    password_stdin: bool,
    name: str | None,
    group_ids: str | None,
    concurrency: int,
    priority: int,
) -> None:
    """Convert email/password to Grok OAuth and create one account.

    The password is accepted only from stdin, used for the one authorization
    request, and never included in output or the persisted account payload.
    The server-side password authorization feature must already be enabled.
    """
    if not password_stdin:
        raise click.ClickException("Refusing password on argv; pass --password-stdin and pipe it securely.")
    password = click.get_text_stream("stdin").read().rstrip("\r\n")
    if not password:
        raise click.ClickException("Password stdin was empty")
    parsed_group_ids = parse_ids(group_ids) if group_ids else []
    if _runtime(ctx).dry_run:
        _emit(
            ctx,
            {
                "dry_run": True,
                "steps": [
                    _planned("POST", "/admin/grok/oauth/password", payload={"email": email, "password": "<stdin>"}),
                    _planned("POST", "/admin/accounts", payload={"name": name or email, "platform": "grok", "type": "oauth", "group_ids": parsed_group_ids}),
                ],
            },
            title="Grok import plan",
        )
        return
    token_info = _runtime(ctx).client.admin(
        "POST", "/admin/grok/oauth/password", payload={"email": email, "password": password}
    )
    if not isinstance(token_info, dict) or not token_info.get("access_token"):
        raise click.ClickException("Grok OAuth authorization returned no access token")
    account_payload = {
        "name": name or token_info.get("email") or email,
        "platform": "grok",
        "type": "oauth",
        "credentials": build_grok_oauth_credentials(token_info),
        "extra": build_grok_extra(token_info),
        "concurrency": concurrency,
        "priority": priority,
        "group_ids": parsed_group_ids,
    }
    created = _runtime(ctx).client.admin("POST", "/admin/accounts", payload=account_payload)
    if isinstance(created, dict):
        created = {key: created.get(key) for key in ("id", "name", "platform", "type", "status") if key in created}
    _emit(ctx, {"created": True, "account": created}, title="Grok account created")


@accounts.command("export")
@click.option("--output-file", type=click.Path(dir_okay=False, path_type=Path), required=True)
@click.option("--ids", help="Optional comma-separated account IDs.")
@click.option("--platform")
@click.option("--type", "account_type")
@click.option("--status")
@click.option("--group")
@click.option("--include-proxies", type=click.BOOL)
@click.pass_context
def accounts_export(
    ctx: click.Context,
    output_file: Path,
    ids: str | None,
    platform: str | None,
    account_type: str | None,
    status: str | None,
    group: str | None,
    include_proxies: bool | None,
) -> None:
    """Export sensitive credentials only to an explicit local file."""
    query: dict[str, Any] = {
        "ids": ",".join(map(str, parse_ids(ids))) if ids else None,
        "platform": platform,
        "type": account_type,
        "status": status,
        "group": group,
        "include_proxies": include_proxies,
    }
    runtime = _runtime(ctx)
    if runtime.dry_run:
        _emit(ctx, _planned("GET", "/admin/accounts/data", query=query), title="Sensitive account export")
        return
    _emit(ctx, write_json_file(output_file, runtime.client.admin("GET", "/admin/accounts/data", query=query)), title="Sensitive export saved")


@cli.group()
def users() -> None:
    """Manage Sub2API panel users through documented user administration routes."""


@users.command("list")
@click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--page-size", type=click.IntRange(min=1, max=500), default=50, show_default=True)
@click.option("--status")
@click.option("--role")
@click.option("--search")
@click.option("--group-name")
@click.pass_context
def users_list(ctx: click.Context, page: int, page_size: int, status: str | None, role: str | None, search: str | None, group_name: str | None) -> None:
    """List panel users with common native query filters."""
    _list_resource(ctx, "users", {"page": page, "page_size": page_size, "status": status, "role": role, "search": search, "group_name": group_name})


@users.command("get")
@click.argument("user_id", type=int)
@click.pass_context
def users_get(ctx: click.Context, user_id: int) -> None:
    """Fetch one user by ID."""
    _emit(ctx, _runtime(ctx).client.admin("GET", f"/admin/users/{user_id}"), title="User")


@users.command("create")
@click.option("--data")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def users_create(ctx: click.Context, data: str | None, file_path: Path | None) -> None:
    """Create a panel user using the server's native schema."""
    _emit(ctx, _mutate(ctx, "POST", "/admin/users", payload=parse_json_data(data, file_path)), title="User created")


@users.command("update")
@click.argument("user_id", type=int)
@click.option("--data")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def users_update(ctx: click.Context, user_id: int, data: str | None, file_path: Path | None) -> None:
    """Apply a partial user update."""
    _emit(ctx, _mutate(ctx, "PUT", f"/admin/users/{user_id}", payload=parse_json_data(data, file_path)), title="User updated")


@users.command("delete")
@click.argument("user_id", type=int)
@click.confirmation_option(prompt="Delete this user?")
@click.pass_context
def users_delete(ctx: click.Context, user_id: int) -> None:
    """Delete one user after explicit confirmation."""
    _emit(ctx, _mutate(ctx, "DELETE", f"/admin/users/{user_id}"), title="User deleted")


@users.command("balance")
@click.argument("user_id", type=int)
@click.argument("operation", type=click.Choice(["set", "add", "subtract"]))
@click.argument("amount", type=click.FloatRange(min=0, min_open=True))
@click.option("--notes", default="", help="Native balance-history note.")
@click.pass_context
def users_balance(ctx: click.Context, user_id: int, operation: str, amount: float, notes: str) -> None:
    """Change the internal ledger balance through the dedicated balance route.

    Writing `balance` via `users update` is not the audited path; this route
    records balance history and enforces set/add/subtract semantics.
    """
    payload: dict[str, Any] = {"balance": amount, "operation": operation}
    if notes:
        payload["notes"] = notes
    _emit(ctx, _mutate(ctx, "POST", f"/admin/users/{user_id}/balance", payload=payload), title="User balance updated")


@cli.group("audit-logs")
def audit_logs() -> None:
    """Read native admin audit logs (who called which admin route, and when)."""


@audit_logs.command("list")
@click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--page-size", type=click.IntRange(1, 500), default=50, show_default=True)
@click.option("--param", "params", multiple=True, help="Extra native filter in key=value form.")
@click.pass_context
def audit_logs_list(ctx: click.Context, page: int, page_size: int, params: tuple[str, ...]) -> None:
    """List audit-log rows, newest first."""
    query: dict[str, Any] = {"page": page, "page_size": page_size, **dict(parse_params(params))}
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/audit-logs", query=query), title="Audit logs")


def _resource_commands(group: click.Group, resource: str, display: str) -> None:
    """Register consistent list/get/create/update/delete commands for resource APIs."""

    @group.command("list")
    @click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
    @click.option("--page-size", type=click.IntRange(min=1, max=500), default=100, show_default=True)
    @click.option("--platform", help="Filter records by platform (native query).")
    @click.option("--status", help="Filter records by native status value.")
    @click.option("--search", help="Free-text native search filter.")
    @click.pass_context
    def list_command(ctx: click.Context, page: int, page_size: int, platform: str | None, status: str | None, search: str | None) -> None:
        """List records from the native resource route."""
        _list_resource(ctx, resource, {"page": page, "page_size": page_size, "platform": platform, "status": status, "search": search})

    @group.command("all")
    @click.option("--platform", help="Filter records by platform (native query).")
    @click.option("--include-inactive", is_flag=True, help="Include inactive records where supported.")
    @click.pass_context
    def all_command(ctx: click.Context, platform: str | None, include_inactive: bool) -> None:
        """List all records where Sub2API exposes an /all route."""
        query = {"platform": platform, "include_inactive": "true" if include_inactive else None}
        _emit(ctx, _runtime(ctx).client.admin("GET", f"{CRUD_RESOURCES[resource]}/all", query=query), title=f"All {display}")

    @group.command("get")
    @click.argument("resource_id", type=int)
    @click.pass_context
    def get_command(ctx: click.Context, resource_id: int) -> None:
        """Fetch one record by ID."""
        _emit(ctx, _runtime(ctx).client.admin("GET", f"{CRUD_RESOURCES[resource]}/{resource_id}"), title=display)

    @group.command("create")
    @click.option("--data")
    @click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.pass_context
    def create_command(ctx: click.Context, data: str | None, file_path: Path | None) -> None:
        """Create a record with native JSON data."""
        _emit(ctx, _mutate(ctx, "POST", CRUD_RESOURCES[resource], payload=parse_json_data(data, file_path)), title=f"{display} created")

    @group.command("update")
    @click.argument("resource_id", type=int)
    @click.option("--data")
    @click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
    @click.pass_context
    def update_command(ctx: click.Context, resource_id: int, data: str | None, file_path: Path | None) -> None:
        """Apply a partial update to a record."""
        _emit(ctx, _mutate(ctx, "PUT", f"{CRUD_RESOURCES[resource]}/{resource_id}", payload=parse_json_data(data, file_path)), title=f"{display} updated")

    @group.command("delete")
    @click.argument("resource_id", type=int)
    @click.confirmation_option(prompt=f"Delete this {display.lower()}?")
    @click.pass_context
    def delete_command(ctx: click.Context, resource_id: int) -> None:
        """Delete a record after explicit confirmation."""
        _emit(ctx, _mutate(ctx, "DELETE", f"{CRUD_RESOURCES[resource]}/{resource_id}"), title=f"{display} deleted")


@cli.group()
def groups() -> None:
    """Manage API consumer groups through native group endpoints."""


_resource_commands(groups, "groups", "Group")


@cli.group()
def proxies() -> None:
    """Manage upstream proxy records through native proxy endpoints."""


_resource_commands(proxies, "proxies", "Proxy")


@proxies.command("export")
@click.option("--output-file", type=click.Path(dir_okay=False, path_type=Path), required=True)
@click.pass_context
def proxies_export(ctx: click.Context, output_file: Path) -> None:
    """Export sensitive proxy credentials only to an explicit local file."""
    runtime = _runtime(ctx)
    if runtime.dry_run:
        _emit(ctx, _planned("GET", "/admin/proxies/data"), title="Sensitive proxy export")
        return
    _emit(ctx, write_json_file(output_file, runtime.client.admin("GET", "/admin/proxies/data")), title="Sensitive export saved")


@cli.group("redeem-codes")
def redeem_codes() -> None:
    """Inspect and issue native Sub2API redemption codes."""


@redeem_codes.command("list")
@click.option("--page", type=click.IntRange(min=1), default=1, show_default=True)
@click.option("--page-size", type=click.IntRange(min=1, max=500), default=200, show_default=True)
@click.option("--type")
@click.option("--status")
@click.option("--search")
@click.pass_context
def redeem_codes_list(ctx: click.Context, page: int, page_size: int, type: str | None, status: str | None, search: str | None) -> None:
    """List redemption codes using the native pagination endpoint."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/redeem-codes", query={"page": page, "page_size": page_size, "type": type, "status": status, "search": search}), title="Redeem codes")


@redeem_codes.command("get")
@click.argument("code_id", type=int)
@click.pass_context
def redeem_codes_get(ctx: click.Context, code_id: int) -> None:
    """Fetch one redemption code by ID."""
    _emit(ctx, _runtime(ctx).client.admin("GET", f"/admin/redeem-codes/{code_id}"), title="Redeem code")


@redeem_codes.command("stats")
@click.pass_context
def redeem_codes_stats(ctx: click.Context) -> None:
    """Fetch redemption code totals from the native stats endpoint."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/redeem-codes/stats"), title="Redeem code stats")


@redeem_codes.command("generate")
@click.option("--data")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--idempotency-key")
@click.pass_context
def redeem_codes_generate(ctx: click.Context, data: str | None, file_path: Path | None, idempotency_key: str | None) -> None:
    """Generate codes; pass a stable idempotency key for payment-related flows."""
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    _emit(ctx, _mutate(ctx, "POST", "/admin/redeem-codes/generate", payload=parse_json_data(data, file_path), headers=headers), title="Redeem codes generated")


@redeem_codes.command("expire")
@click.argument("code_id", type=int)
@click.pass_context
def redeem_codes_expire(ctx: click.Context, code_id: int) -> None:
    """Expire an existing redemption code."""
    _emit(ctx, _mutate(ctx, "POST", f"/admin/redeem-codes/{code_id}/expire"), title="Redeem code expired")


@cli.group()
def dashboard() -> None:
    """Read native Sub2API dashboard metrics without screen-scraping the UI."""


@dashboard.command("stats")
@click.pass_context
def dashboard_stats(ctx: click.Context) -> None:
    """Fetch global dashboard statistics."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/dashboard/stats"), title="Dashboard stats")


@dashboard.command("snapshot")
@click.option("--start-date")
@click.option("--end-date")
@click.option("--granularity", type=click.Choice(["day", "hour"]))
@click.pass_context
def dashboard_snapshot(ctx: click.Context, start_date: str | None, end_date: str | None, granularity: str | None) -> None:
    """Fetch the v2 dashboard snapshot with optional native date filters."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/dashboard/snapshot-v2", query={"start_date": start_date, "end_date": end_date, "granularity": granularity}), title="Dashboard snapshot")


@cli.group()
def settings() -> None:
    """Inspect or update native administrator settings."""


@settings.command("get")
@click.pass_context
def settings_get(ctx: click.Context) -> None:
    """Read the current administrator settings."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/settings"), title="Settings")


@settings.command("update")
@click.option("--data")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def settings_update(ctx: click.Context, data: str | None, file_path: Path | None) -> None:
    """Apply a partial native administrator-settings update."""
    _emit(ctx, _mutate(ctx, "PUT", "/admin/settings", payload=parse_json_data(data, file_path)), title="Settings updated")


@cli.group()
def ops() -> None:
    """Read operational signals directly from the native administration API."""


@ops.command("concurrency")
@click.pass_context
def ops_concurrency(ctx: click.Context) -> None:
    """Fetch real-time concurrency statistics."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/ops/concurrency"), title="Concurrency")


@ops.command("realtime-traffic")
@click.pass_context
def ops_realtime_traffic(ctx: click.Context) -> None:
    """Fetch the real-time traffic summary."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/ops/realtime-traffic"), title="Realtime traffic")


@ops.command("ingress-rejections")
@click.pass_context
def ops_ingress_rejections(ctx: click.Context) -> None:
    """Fetch bounded ingress-admission rejection aggregates."""
    _emit(ctx, _runtime(ctx).client.admin("GET", "/admin/ops/ingress-rejections"), title="Ingress rejections")


@cli.command("api")
@click.argument("method", type=click.Choice(["GET", "POST", "PUT", "PATCH", "DELETE"], case_sensitive=False))
@click.argument("path")
@click.option("--data", help="Optional JSON request body.")
@click.option("--file", "file_path", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Optional JSON request body file.")
@click.option("--param", "params", multiple=True, help="Repeatable native query parameter in key=value form.")
@click.option("--output-file", type=click.Path(dir_okay=False, path_type=Path), help="Required for known sensitive exports; saves response without printing it.")
@click.pass_context
def api(
    ctx: click.Context,
    method: str,
    path: str,
    data: str | None,
    file_path: Path | None,
    params: tuple[str, ...],
    output_file: Path | None,
) -> None:
    """Call a supported admin endpoint not yet given a dedicated command."""
    payload = parse_json_data(data, file_path, required=False)
    query = parse_params(params)
    if is_sensitive_export_path(path) and output_file is None:
        raise click.ClickException("This endpoint can expose credentials or secrets; pass --output-file to save it safely.")
    runtime = _runtime(ctx)
    if method.upper() != "GET" and runtime.dry_run:
        _emit(ctx, _planned(method, path, payload=payload, query=query), title="Dry run")
        return
    result = runtime.client.admin(method, path, payload=payload, query=query)
    if output_file:
        _emit(ctx, write_json_file(output_file, result), title="Response saved")
    else:
        _emit(ctx, result, title=f"API {method.upper()} {path}")


@cli.command(hidden=True)
@click.pass_context
def repl(ctx: click.Context) -> None:
    """Interactive shell; one-shot API semantics remain unchanged."""
    runtime = _runtime(ctx)
    runtime.repl_mode = True
    skin = ReplSkin("sub2api", version=__version__)
    skin.print_banner()
    skin.status_block(
        {
            "Base URL": _safe_base_url(runtime.config.base_url) if runtime.config.base_url else "not configured",
            "Authentication": runtime.config.auth_scheme or "not configured",
            "Profile": str(runtime.config.profile_path),
        },
        title="Connection",
    )
    skin.hint("Set SUB2API_ADMIN_API_KEY or SUB2API_JWT in the environment; credentials are never stored in the profile.")
    prompt_session = skin.create_prompt_session()
    while True:
        try:
            line = skin.get_input(prompt_session, context="admin")
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            return
        if not line:
            continue
        if line in {"exit", "quit"}:
            skin.print_goodbye()
            return
        if line == "help":
            skin.help(
                {
                    "status": "verify health and server version",
                    "accounts list": "list upstream accounts",
                    "users list": "list panel users",
                    "groups all": "list groups",
                    "dashboard stats": "read dashboard statistics",
                    "api GET /admin/...": "call an uncovered admin route",
                    "quit": "exit the shell",
                }
            )
            continue
        try:
            cli.main(args=shlex.split(line), prog_name="cli-anything-sub2api", standalone_mode=False)
        except (click.ClickException, click.Abort, Sub2APIError) as exc:
            skin.error(str(exc))
        except SystemExit as exc:
            if exc.code not in (0, None):
                skin.error(f"Command exited with status {exc.code}")
        except ValueError as exc:
            skin.error(str(exc))


def main() -> None:
    """Console-script entry point with safe, concise API error rendering."""
    try:
        cli.main(prog_name="cli-anything-sub2api")
    except Sub2APIError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
