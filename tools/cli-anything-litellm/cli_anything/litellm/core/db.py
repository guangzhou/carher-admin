"""Read-only views of the 198 production LiteLLM Postgres, plus the schema it should match.

Rules baked in (all from incidents, see the skill):
  * prod is ns ``litellm-product``; the dev ns has same-named objects and a near-empty DB.
    ``anchor()`` reads structural counts that tell the two apart.
  * the DB is shared (casdoor/owui/myai) with max_connections=100 -> one psql per call.
  * SpendLogs is ~75GB: only query it through its indexes (``startTime``, ``api_key,startTime``,
    ``request_id`` pkey, ``session_id``, ``litellm_call_id``, ``end_user``).
  * SpendLogs keeps ~6 days (two-tier retention); history lives in the Daily* tables.
"""
from __future__ import annotations

import re
from typing import Final

from cli_anything.litellm.utils import remote198

SCHEMA_PATH: Final = "litellm-proxy-extras/litellm_proxy_extras/schema.prisma"
_IDENT_RE: Final = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _lit(value: str, pattern: re.Pattern[str] = _IDENT_RE) -> str:
    """Quote a user value as a SQL literal after a strict allow-list check (no free text reaches SQL)."""
    if not pattern.match(value):
        raise remote198.RemoteError(f"refusing value {value!r} (allowed: {pattern.pattern})")
    return "'" + value + "'"


def anchor() -> dict[str, str]:
    """Structural counts that distinguish prod from dev (prod: hundreds of model rows, a fallbacks list)."""
    rows = remote198.psql(
        "select current_database() as db,"
        " (select count(*) from \"LiteLLM_ProxyModelTable\") as proxy_model_rows,"
        " (select coalesce(jsonb_array_length(param_value::jsonb->'fallbacks'),0) from \"LiteLLM_Config\" where param_name='router_settings') as db_fallbacks,"
        " (select count(*) from pg_stat_activity) as connections,"
        " current_setting('max_connections') as max_connections,"
        " pg_size_pretty(pg_database_size(current_database())) as size"
    )
    return rows[0] if rows else {}


def schema_models(tag: str) -> dict[str, dict[str, object]]:
    """Prisma models in the tag: name -> {table, fields, relations}."""
    src = remote198.fork_show(tag, SCHEMA_PATH)
    models: dict[str, dict[str, object]] = {}
    for name, body in re.findall(r"^model (\w+) \{(.*?)^\}", src, re.S | re.M):
        lines = [l.strip() for l in body.strip().splitlines() if l.strip() and not l.strip().startswith(("//", "@@"))]
        mapped = re.search(r'@@map\("([^"]+)"\)', body)
        models[name] = {
            "table": mapped.group(1) if mapped else name,
            "fields": [l.split()[0] for l in lines],
            "relations": sorted({l.split()[1].rstrip("?[]") for l in lines if "@relation" in l}),
        }
    return models


def live_relations() -> dict[str, dict[str, object]]:
    rows = remote198.psql(
        "select n.nspname as schema, c.relname as name, c.relkind as kind, greatest(c.reltuples,0)::bigint as est_rows,"
        " pg_total_relation_size(c.oid) as bytes"
        " from pg_class c join pg_namespace n on n.oid=c.relnamespace"
        " where n.nspname not in ('pg_catalog','information_schema','pg_toast') and c.relkind in ('r','v','m','p')"
    )
    return {f"{r['schema']}.{r['name']}": r for r in rows}


def catalog(tag: str) -> dict[str, object]:
    """Schema vs live: missing tables, our extra tables (backups), views, biggest tables, migrations."""
    models = schema_models(tag)
    live = live_relations()
    public = {k.split(".", 1)[1]: v for k, v in live.items() if k.startswith("public.")}
    want = {m["table"] for m in models.values()}
    missing = sorted(want - set(public))
    extra = sorted(t for t in set(public) - want if not t.startswith("_prisma"))
    mig = remote198.psql(
        "select count(*) as applied, count(*) filter (where finished_at is null) as unfinished,"
        " count(*) filter (where rolled_back_at is not null) as rolled_back,"
        " max(migration_name) as latest from _prisma_migrations"
    )
    by_size = sorted(public.items(), key=lambda kv: -int(kv[1]["bytes"]))
    other_schemas: dict[str, int] = {}
    for k in live:
        s = k.split(".", 1)[0]
        if s != "public":
            other_schemas[s] = other_schemas.get(s, 0) + 1
    return {
        "tag": tag,
        "schema_models": len(models),
        "missing_in_db": missing,
        "views": sorted(t for t in extra if public[t]["kind"] == "v"),
        "non_schema_tables": sorted(t for t in extra if public[t]["kind"] == "r"),
        "other_schemas": other_schemas,
        "largest": [{"table": t, "est_rows": int(v["est_rows"]), "bytes": int(v["bytes"])} for t, v in by_size[:12]],
        "migrations": mig[0] if mig else {},
    }


def config_ownership() -> dict[str, list[dict[str, str]]]:
    """Which router/general settings come from DB vs config.yaml, as the proxy itself resolves them.

    1.105 rule (config_resolvers/settings_store.py): runtime > config.yaml > DB. A key present
    in config.yaml shadows the DB value entirely; everything else is owned by the DB row.
    We read both sides here and apply that rule; `routing` below cross-checks with the live API.
    """
    import json

    import yaml

    pod = remote198.lane_pod("prod")
    yaml_text = remote198.run(remote198.KUBE + f"kubectl exec -n {remote198.NS} {pod} -- cat /app/config.yaml")
    y = yaml.safe_load(yaml_text) or {}
    rows = remote198.psql(
        "select param_name, param_value::text as v from \"LiteLLM_Config\""
        " where param_name in ('router_settings','general_settings')"
    )
    out: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        db = json.loads(r["v"]) if r["v"] else {}
        ys = y.get(r["param_name"]) or {}
        items = []
        for k in sorted(set(db) | set(ys)):
            if k in ys and k in db:
                src = "config (db shadowed)" if db[k] != ys[k] else "config (= db)"
            elif k in ys:
                src = "config"
            else:
                src = "db"
            if k in ("master_key", "database_url"):
                size = "redacted"
            else:
                v = ys.get(k, db.get(k))
                size = f"{len(v)} entries" if isinstance(v, (list, dict)) else json.dumps(v)[:40]
            items.append({"key": k, "owner": src, "value": size})
        out[r["param_name"]] = items
    return out


def spend_reconcile(date: str, model_group_like: str | None = None, limit: int = 15) -> dict[str, object]:
    """Per key: requests in DailyUserSpend vs rows in SpendLogs for one UTC date.

    A large positive gap = SpendLogs rows were dropped (detail lost, billing kept). ±1 is the
    day boundary (daily buckets by request date, SpendLogs by startTime).
    """
    d = _lit(date, _DATE_RE)
    mg = ""
    if model_group_like:
        mg_lit = _lit(model_group_like.replace("*", "%"), re.compile(r"^[A-Za-z0-9_.%:-]{1,80}$"))
        mg = f" and model_group like {mg_lit}"
    # Bounded on purpose. An unfiltered `group by api_key` over a day of SpendLogs walks the whole
    # (api_key, startTime) index (startTime is its 2nd column) and blew the 40s timeout on
    # 2026-10-20. So: pick the busiest `limit` keys from the small daily table, then count each
    # key's SpendLogs rows with a LATERAL point lookup that the index answers directly.
    sql = (
        f"with d as (select api_key, sum(api_requests) req, round(sum(spend)::numeric,2) spend from \"LiteLLM_DailyUserSpend\""
        f" where date={d}{mg} group by 1 order by 2 desc limit {int(limit)})"
        " select left(d.api_key,10) as key_prefix, coalesce(t.key_alias,'') as key_alias, d.req as daily_requests,"
        " s.n as spendlog_rows, d.req-s.n as gap, d.spend"
        " from d cross join lateral (select count(*) n from \"LiteLLM_SpendLogs\" l where l.api_key=d.api_key"
        f" and l.\"startTime\" >= {d}::date and l.\"startTime\" < {d}::date + 1{mg.replace('model_group', 'l.model_group')}) s"
        " left join \"LiteLLM_VerificationToken\" t on t.token=d.api_key"
        " order by gap desc"
    )
    rows = remote198.psql(sql, timeout_ms=40000)
    totals = remote198.psql(
        f"select (select coalesce(sum(api_requests),0) from \"LiteLLM_DailyUserSpend\" where date={d}{mg}) as daily_requests,"
        f" (select count(*) from \"LiteLLM_SpendLogs\" where \"startTime\" >= {d}::date and \"startTime\" < {d}::date + 1{mg}) as spendlog_rows",
        timeout_ms=40000,
    )
    return {"date": date, "model_group_like": model_group_like, "totals": totals[0] if totals else {}, "keys": rows}


def spendlog_retention() -> dict[str, str]:
    rows = remote198.psql('select min("startTime") as oldest, max("startTime") as newest from "LiteLLM_SpendLogs"')
    daily = remote198.psql('select min(date) as oldest, max(date) as newest from "LiteLLM_DailyUserSpend"')
    return {"spendlogs_oldest": rows[0]["oldest"], "spendlogs_newest": rows[0]["newest"],
            "daily_oldest": daily[0]["oldest"], "daily_newest": daily[0]["newest"]}
