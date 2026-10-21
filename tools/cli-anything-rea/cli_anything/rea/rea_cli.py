from __future__ import annotations

import functools
import json
import shlex

import click

from cli_anything.rea import __version__
from cli_anything.rea.core import catalog, deps, runs, source
from cli_anything.rea.core import env as env_core
from cli_anything.rea.utils.repl_skin import ReplSkin


class Context:
    def __init__(self, json_output: bool):
        self.json_output = json_output


def _emit(ctx: Context, value: object, human: str | None = None) -> None:
    if ctx.json_output or human is None:
        click.echo(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))
        return
    click.echo(human)


def _guard(fn):
    """ReaError → non-zero exit with REA's own stderr/details attached."""

    @functools.wraps(fn)
    def wrapper(ctx: Context, *a, **kw):
        try:
            return fn(ctx, *a, **kw)
        except env_core.ReaError as exc:
            payload = {"ok": False, "error": str(exc), "stderr": exc.stderr}
            if ctx.json_output:
                raise click.ClickException(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            raise click.ClickException(str(exc) + (f"\n{exc.stderr}" if exc.stderr else ""))
    return wrapper


def _table(rows: list[dict[str, object]], cols: list[str]) -> str:
    if not rows:
        return "(none)"
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    head = "  ".join(c.ljust(w[c]) for c in cols)
    return "\n".join([head] + ["  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols) for r in rows])


@click.group(invoke_without_command=True)
@click.option("--json", "json_output", is_flag=True, help="Emit machine-readable JSON.")
@click.version_option(__version__)
@click.pass_context
def cli(click_ctx: click.Context, json_output: bool) -> None:
    """REA (morluto/rea) on this Mac: read its source, list its MCP tools, check deps, run it for real."""
    click_ctx.obj = Context(json_output)
    if click_ctx.invoked_subcommand is None:
        click_ctx.invoke(repl)


# ---------------------------------------------------------------- src
@cli.group(name="src")
def src_group() -> None:
    """Source at an explicit git ref of ~/codes/rea (default HEAD = what dist/ was built from)."""


@src_group.command("head")
@click.pass_obj
@_guard
def src_head(ctx: Context) -> None:
    """Which commit the clone (and its build) is at; references/ docs are keyed by its sha7."""
    h = env_core.git_head()
    ref = source.reference_dir()
    docs = sorted(p.name for p in ref.iterdir()) if ref else []
    match = f"main-{h['sha'][:7]}" in docs
    r = h | {"reference_docs": docs, "docs_match_head": match}
    _emit(ctx, r, f"{h['sha'][:12]}  {h['date']}  {h['subject']}\nreferences: {', '.join(docs) or '(none)'}"
          + ("" if match else f"\n⚠ no references/main-{h['sha'][:7]}: docs were written at another commit; re-check file:line"))


@src_group.command("layers")
@click.pass_obj
@_guard
def src_layers(ctx: Context) -> None:
    """The layers, what each owns, and which reference doc reads it line by line."""
    rows = [{"layer": a, "where": b, "owns": c, "doc": d} for a, b, c, d in source.LAYERS]
    _emit(ctx, rows, _table(rows, ["layer", "where", "owns", "doc"]))


@src_group.command("find")
@click.argument("symbol")
@click.option("--ref", default="HEAD", show_default=True)
@click.option("--path", help="Limit to a path, e.g. src/ghidra")
@click.option("--tests", is_flag=True, help="Include *.test.ts and tests/.")
@click.pass_obj
@_guard
def src_find(ctx: Context, symbol: str, ref: str, path: str | None, tests: bool) -> None:
    """Where SYMBOL is declared (function/class/const/type/method)."""
    hits = source.find(symbol, ref, path, tests)
    _emit(ctx, hits, "\n".join(f"{h['path']}:{h['line']}  [{h['kind']}] {h['text']}" for h in hits) or "no declaration")


@src_group.command("show")
@click.argument("location")
@click.option("--ref", default="HEAD", show_default=True)
@click.option("-n", "--lines", "context", default=40, show_default=True)
@click.pass_obj
@_guard
def src_show(ctx: Context, location: str, ref: str, context: int) -> None:
    """PATH:LINE and the following lines at REF."""
    r = source.show(location, ref, context)
    _emit(ctx, r, f"{r['path']} @ {ref}\n" + "\n".join(r["lines"]))  # type: ignore[arg-type]


@src_group.command("grep")
@click.argument("pattern")
@click.option("--ref", default="HEAD", show_default=True)
@click.option("--path", "paths", multiple=True, default=("src",), show_default=True)
@click.option("-F", "fixed", is_flag=True, help="Fixed string, not regex.")
@click.option("--limit", default=200, show_default=True)
@click.pass_obj
@_guard
def src_grep(ctx: Context, pattern: str, ref: str, paths: tuple[str, ...], fixed: bool, limit: int) -> None:
    hits = source.grep(pattern, ref, paths, fixed, limit)
    _emit(ctx, hits, "\n".join(f"{h['path']}:{h['line']}  {h['text']}" for h in hits) or "no match")


@src_group.command("env-vars")
@click.option("--ref", default="HEAD", show_default=True)
@click.pass_obj
@_guard
def src_env_vars(ctx: Context, ref: str) -> None:
    """Every REA_* name in non-test source (the config surface), by how many files read it."""
    rows = source.env_vars(ref)
    _emit(ctx, rows, f"{len(rows)} names\n" + _table(rows, ["name", "files"]))


# ---------------------------------------------------------------- tools
@cli.group(name="tools")
def tools_group() -> None:
    """The MCP tool catalog: source table vs what `rea mcp` actually advertises."""


@tools_group.command("list")
@click.option("--destructive", is_flag=True, help="Only tools REA marks destructive.")
@click.option("--grep", "needle", help="Substring of the tool name.")
@click.pass_obj
@_guard
def tools_list(ctx: Context, destructive: bool, needle: str | None) -> None:
    """From src/contracts/toolEffects.ts (no server start)."""
    rows = catalog.source_catalog()
    if destructive:
        rows = [r for r in rows if r["destructive"]]
    if needle:
        rows = [r for r in rows if needle in str(r["name"])]
    for r in rows:
        r["flags_s"] = ",".join(r["flags"])  # type: ignore[arg-type]
    _emit(ctx, rows, f"{len(rows)} tools\n" + _table(rows, ["name", "destructive", "readOnly", "flags_s"]))


@tools_group.command("verify")
@click.pass_obj
@_guard
def tools_verify(ctx: Context) -> None:
    """Start `rea mcp`, page tools/list, compare names + destructive/readOnly with the source table."""
    rt = catalog.runtime_catalog()
    d = catalog.diff(catalog.source_catalog(), rt)
    rtools = rt["tools"]  # type: ignore[index]
    d["runtime_destructive"] = sum(1 for t in rtools if t["annotations"].get("destructiveHint"))  # type: ignore[union-attr]
    d["runtime_read_only"] = sum(1 for t in rtools if t["annotations"].get("readOnlyHint"))  # type: ignore[union-attr]
    d["server"] = rt.get("server")
    human = (f"source {d['source']}  runtime {d['runtime']}  destructive {d['runtime_destructive']}  "
             f"readOnly {d['runtime_read_only']}\nonly in source: {d['only_in_source'] or '-'}\n"
             f"only in runtime: {d['only_in_runtime'] or '-'}\nannotation mismatch: {d['annotation_mismatch'] or '-'}\n"
             + ("OK" if d["ok"] else "STOP: catalog drift"))
    _emit(ctx, d, human)
    if not d["ok"]:
        raise SystemExit(1)


# ---------------------------------------------------------------- deps
@cli.group(name="deps")
def deps_group() -> None:
    """Local dependencies: what is installed, from where, and whether it executes."""


@deps_group.command("status")
@click.pass_obj
@_guard
def deps_status(ctx: Context) -> None:
    """Run each tool's version command with rea.env applied. Also lists what is deliberately absent."""
    rows = deps.probe()
    absent = [{"tool": a, "why": b} for a, b in deps.NOT_INSTALLED]
    ok = all(r["ok"] for r in rows)
    _emit(ctx, {"ok": ok, "tools": rows, "not_installed": absent},
          _table(rows, ["tool", "ok", "version", "unlocks"]) + "\n\nnot installed:\n"
          + "\n".join(f"  {a['tool']}: {a['why']}" for a in absent))
    if not ok:
        raise SystemExit(1)


@deps_group.command("env")
@click.pass_obj
@_guard
def deps_env(ctx: Context) -> None:
    """The variables ~/codes/rea-deps/rea.env sets (paths only; it holds no credentials)."""
    d = env_core.declared()
    d.pop("PATH", None)
    _emit(ctx, {"file": str(env_core.env_file()), "vars": d},
          f"{env_core.env_file()}\n" + "\n".join(f"  {k}={v}" for k, v in sorted(d.items())))


@deps_group.command("unkill")
@click.option("--dry-run", is_flag=True)
@click.pass_obj
@_guard
def deps_unkill(ctx: Context, dry_run: bool) -> None:
    """Replace node_modules Mach-O files that Huorong SIGKILLs (exit 137) with fresh-inode copies."""
    rows = deps.unkill_node_modules(dry_run=dry_run)
    _emit(ctx, rows, _table(rows, ["path", "before", "after"]) if rows else "nothing killed")


@deps_group.command("build")
@click.pass_obj
@_guard
def deps_build(ctx: Context) -> None:
    """npm ci (when node_modules is absent) → unkill → npm run build:cached."""
    r = deps.build()
    _emit(ctx, r, "\n".join(f"{s['step']}: {s.get('exit', s.get('fixed'))}" for s in r["steps"])  # type: ignore[union-attr]
          + f"\n{'OK' if r['ok'] else 'FAILED'}  log: {r['log']}")
    if not r["ok"]:
        raise SystemExit(1)


# ---------------------------------------------------------------- run
@cli.group(name="run")
def run_group() -> None:
    """Real REA runs. Full Evidence is saved under ~/codes/rea-deps/runs/; output is a summary."""


@run_group.command("doctor")
@click.pass_obj
@_guard
def run_doctor(ctx: Context) -> None:
    """`rea doctor --json`, engines vs agent registrations split (registrations are off by design)."""
    r = runs.doctor()
    eng = [c for c in r["checks"] if c["group"] == "engine"]  # type: ignore[union-attr]
    _emit(ctx, r, f"engines {r['engines_ok']}/{r['engines']} ok   agents registered: {r['agents_registered']}\n"
          + _table(eng, ["name", "ok", "detail"]))


@run_group.command("providers")
@click.pass_obj
@_guard
def run_providers(ctx: Context) -> None:
    _emit(ctx, runs.providers())


@run_group.command("decompile")
@click.argument("path")
@click.argument("procedure")
@click.option("--provider", default="ghidra", show_default=True, type=click.Choice(runs.DEEP_PROVIDERS))
@click.pass_obj
@_guard
def run_decompile(ctx: Context, path: str, procedure: str, provider: str) -> None:
    """Decompile one procedure (name or 0xaddr). Mach-O C symbols carry a leading underscore."""
    r = runs.decompile(path, procedure, provider)
    _emit(ctx, r, f"{r['provider']} {r['provider_version']}  {r['wall_s']}s  {r['confidence']}/{r['authority']}"
          f"  saved {r['saved']}\n{r['pseudocode']}")


@run_group.command("analyze-js")
@click.argument("path")
@click.option("--record-integrity", is_flag=True, help="REA --integrity-policy record-and-continue (signed apps).")
@click.pass_obj
@_guard
def run_analyze_js(ctx: Context, path: str, record_integrity: bool) -> None:
    """Static JS/Electron reconstruction of a directory or .asar (never executes the target)."""
    r = runs.analyze_js(path, record_integrity)
    _emit(ctx, r, json.dumps({k: r[k] for k in ("wall_s", "statistics", "summary", "integrity_contradictions",
                                                 "result_limitations", "saved", "saved_bytes")},
                             ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- repl
@cli.command()
@click.pass_obj
def repl(ctx: Context) -> None:
    """Interactive loop over the same commands."""
    skin = ReplSkin("rea", version=__version__)
    skin.print_banner()
    while True:
        try:
            line = input("rea> ").strip()
        except (EOFError, KeyboardInterrupt):
            skin.print_goodbye()
            return
        if not line:
            continue
        if line in {"exit", "quit"}:
            skin.print_goodbye()
            return
        try:
            cli.main(args=[*(["--json"] if ctx.json_output else []), *shlex.split(line)], standalone_mode=False)
        except (click.ClickException, click.UsageError) as exc:
            skin.error(str(exc))
        except SystemExit:
            pass


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
