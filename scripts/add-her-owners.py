#!/usr/bin/env python3
"""
Batch-add owners to existing CarHer (her) instances.

Workflow per instance:
  1. Pull spec.appId / spec.appSecret / spec.owner from K8s.
  2. Resolve each name → union_id via `lark-cli api .../contact/v3/users/search`
     (exact match on meta_data.i18n_names.zh_cn). Ambiguous / not-found names
     are skipped with a warning so the rest of the batch still runs.
  3. Get tenant_access_token from each instance's own app credentials.
  4. Convert union_id → per-app open_id via
     `/contact/v3/users/{uid}?user_id_type=union_id`.
  5. Merge with existing owner list, deduplicated, original order preserved.
  6. `kubectl patch herinstance her-{id}` — operator rewrites the ConfigMap
     and the in-pod reloader sidecar hot-loads within ~5s. No pod restart
     needed (see memory: project_owner_crd_change_no_pod_restart).
  7. Verify ConfigMap channels.feishu.dm.allowFrom count matches expected.

USAGE
  # YAML file (recommended for batch)
  scripts/add-her-owners.py --file owners.yaml

  # Inline single instance
  scripts/add-her-owners.py --id 180 --names "刘晓龙,金志刚,吕丹萍"

  # Stdin shorthand (one instance per line: "id: name1、name2、name3")
  printf '180: 刘晓龙、金志刚\n185: 张三、李四\n' | scripts/add-her-owners.py

  # Dry-run: resolve everything and print plan, do NOT patch CRDs
  scripts/add-her-owners.py --file owners.yaml --dry-run

YAML format
  - id: 180
    add: [刘晓龙, 金志刚, 吕丹萍]
    # Optional: pin ambiguous names by lark-cli user_id (跨 app 不变)
    user_ids: {刘强: a1b2c3d4}
  - id: 185
    add: [张三, 李四]

EXIT CODE
  0 = all instances patched (or already up-to-date) without errors
  1 = at least one instance failed (lookup miss / ambiguous name / patch failure)
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable

FEISHU_BASE = "https://open.feishu.cn"
NS = "carher"
CONFIGMAP_SETTLE_SECONDS = 8


# ─── Data classes ──────────────────────────────────────────────────────────


@dataclass
class InstancePlan:
    her_id: int
    add_names: list[str]
    user_id_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class InstanceResult:
    her_id: int
    before_count: int = 0
    after_count: int = 0
    added: list[str] = field(default_factory=list)
    skipped_already_owner: list[str] = field(default_factory=list)
    skipped_unresolved: list[tuple[str, str]] = field(default_factory=list)  # (name, reason)
    error: str | None = None
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and not self.skipped_unresolved


# ─── Shelling helpers ──────────────────────────────────────────────────────


def run(cmd: list[str], *, input_bytes: bytes | None = None, timeout: int = 60) -> str:
    """Run a subprocess, return stdout. Raise RuntimeError on non-zero exit."""
    proc = subprocess.run(
        cmd, input=input_bytes, capture_output=True, timeout=timeout
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {' '.join(cmd)}\n"
            f"stderr: {proc.stderr.decode(errors='replace')[:600]}"
        )
    return proc.stdout.decode(errors="replace")


# ─── Feishu API ────────────────────────────────────────────────────────────


def feishu_post(path: str, body: dict, *, token: str | None = None) -> dict:
    req = urllib.request.Request(
        f"{FEISHU_BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def feishu_get(path: str, *, token: str) -> dict:
    req = urllib.request.Request(
        f"{FEISHU_BASE}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


_TENANT_TOKEN_CACHE: dict[str, str] = {}


def get_tenant_token(app_id: str, app_secret: str) -> str:
    key = f"{app_id}:{app_secret}"
    if key in _TENANT_TOKEN_CACHE:
        return _TENANT_TOKEN_CACHE[key]
    d = feishu_post(
        "/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    tok = d.get("tenant_access_token")
    if not tok:
        raise RuntimeError(f"tenant_access_token request failed: {d}")
    _TENANT_TOKEN_CACHE[key] = tok
    return tok


# ─── Union ID resolution via lark-cli ──────────────────────────────────────


def search_users_by_name(name: str) -> list[dict]:
    """Return raw items[] from contact/v3/users/search for `name`."""
    out = run(
        [
            "lark-cli", "api", "POST", "/open-apis/contact/v3/users/search",
            "--params", '{"user_id_type":"union_id","page_size":10}',
            "--data", json.dumps({"query": name}),
        ],
        timeout=30,
    )
    try:
        d = json.loads(out)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"lark-cli returned non-JSON for query '{name}': {out[:200]} (err: {e})")
    return d.get("data", {}).get("items", [])


def resolve_union_id(name: str, user_id_override: str | None = None) -> tuple[str | None, str | None]:
    """
    Return (union_id, error_reason). union_id is non-None on success.
    Strategy:
      - If user_id_override given, resolve via lark-cli +get-user
      - Else search by name with exact zh_cn match
      - If multiple exact matches → ambiguous (caller must pass user_id_override)
    """
    if user_id_override:
        try:
            out = run(
                ["lark-cli", "contact", "+get-user",
                 "--user-id", user_id_override, "--user-id-type", "user_id"],
                timeout=20,
            )
            u = json.loads(out).get("data", {}).get("user", {})
            uid = u.get("union_id")
            if uid:
                return uid, None
            return None, f"user_id={user_id_override} → no union_id in response (got {u})"
        except Exception as e:
            return None, f"user_id lookup failed: {e}"

    items = search_users_by_name(name)
    exact = [i for i in items if i.get("meta_data", {}).get("i18n_names", {}).get("zh_cn") == name]
    if len(exact) == 1:
        return exact[0]["id"], None
    if len(exact) > 1:
        emails = [i.get("meta_data", {}).get("enterprise_mail_address") or i.get("meta_data", {}).get("mail_address") for i in exact]
        return None, f"AMBIGUOUS ({len(exact)} exact matches; pass user_ids: {{{name}: <user_id>}} to disambiguate). Candidates: {emails}"
    if items:
        zh_names = [i.get("meta_data", {}).get("i18n_names", {}).get("zh_cn") for i in items[:5]]
        return None, f"no exact match for '{name}'. Fuzzy candidates: {zh_names}"
    return None, "NOT FOUND"


# ─── K8s helpers ───────────────────────────────────────────────────────────


def get_herinstance(her_id: int) -> dict:
    out = run(["kubectl", "get", "herinstance", f"her-{her_id}", "-n", NS, "-o", "json"])
    return json.loads(out)


def get_app_secret_value(secret_ref: str) -> str:
    out = run(["kubectl", "get", "secret", "-n", NS, secret_ref, "-o", "json"])
    data = json.loads(out).get("data", {})
    # Support both naming conventions
    for k in ("app_secret", "appSecret"):
        if k in data:
            return base64.b64decode(data[k]).decode()
    raise RuntimeError(f"secret {secret_ref} has neither 'app_secret' nor 'appSecret' key: {list(data)}")


def patch_owner(her_id: int, owner_str: str) -> None:
    patch = json.dumps({"spec": {"owner": owner_str}})
    run([
        "kubectl", "patch", "herinstance", f"her-{her_id}", "-n", NS,
        "--type=merge", "-p", patch,
    ])


def verify_configmap_count(her_id: int, expected: int) -> int:
    """Return actual allowFrom count from ConfigMap. Polls a couple times."""
    actual = -1
    for _ in range(3):
        time.sleep(CONFIGMAP_SETTLE_SECONDS // 3 + 1)
        try:
            out = run([
                "kubectl", "get", "cm", "-n", NS,
                f"carher-{her_id}-user-config",
                "-o", "jsonpath={.data.openclaw\\.json}",
            ])
            d = json.loads(out)
            actual = len(d["channels"]["feishu"]["dm"]["allowFrom"])
            if actual == expected:
                return actual
        except Exception:
            pass
    return actual


# ─── Per-instance pipeline ─────────────────────────────────────────────────


def process_instance(plan: InstancePlan, *, dry_run: bool) -> InstanceResult:
    res = InstanceResult(her_id=plan.her_id, dry_run=dry_run)
    try:
        spec = get_herinstance(plan.her_id)["spec"]
    except Exception as e:
        res.error = f"failed to fetch HerInstance her-{plan.her_id}: {e}"
        return res

    app_id = spec["appId"]
    secret_ref = spec.get("appSecretRef", f"carher-{plan.her_id}-secret")
    try:
        app_secret = get_app_secret_value(secret_ref)
    except Exception as e:
        res.error = f"failed to read secret {secret_ref}: {e}"
        return res

    current_str = spec.get("owner", "") or ""
    existing = [o for o in current_str.split("|") if o]
    res.before_count = len(existing)

    try:
        token = get_tenant_token(app_id, app_secret)
    except Exception as e:
        res.error = f"tenant_access_token fetch failed for {app_id}: {e}"
        return res

    new_oids: list[str] = []
    new_names_kept: list[str] = []
    for name in plan.add_names:
        override = plan.user_id_overrides.get(name)
        union_id, err = resolve_union_id(name, user_id_override=override)
        if not union_id:
            res.skipped_unresolved.append((name, err or "unknown"))
            continue
        try:
            d = feishu_get(
                f"/open-apis/contact/v3/users/{union_id}?user_id_type=union_id",
                token=token,
            )
        except urllib.error.HTTPError as e:
            res.skipped_unresolved.append((name, f"HTTP {e.code} converting union_id to open_id"))
            continue
        oid = d.get("data", {}).get("user", {}).get("open_id")
        if not oid:
            res.skipped_unresolved.append((name, f"open_id missing in app {app_id} response: {d}"))
            continue
        if oid in existing:
            res.skipped_already_owner.append(name)
            continue
        new_oids.append(oid)
        new_names_kept.append(name)

    # Dedupe within new_oids too (handles duplicate names in input)
    seen, dedup_new = set(), []
    for oid, nm in zip(new_oids, new_names_kept):
        if oid not in seen and oid not in existing:
            seen.add(oid)
            dedup_new.append((oid, nm))

    merged = existing + [oid for oid, _ in dedup_new]
    res.added = [nm for _, nm in dedup_new]
    res.after_count = len(merged)

    if dry_run:
        return res

    if not dedup_new:
        # Nothing to patch — all names already owners or unresolved
        return res

    try:
        patch_owner(plan.her_id, "|".join(merged))
    except Exception as e:
        res.error = f"kubectl patch failed: {e}"
        return res

    actual = verify_configmap_count(plan.her_id, res.after_count)
    if actual != res.after_count:
        res.error = (
            f"ConfigMap verification mismatch: expected {res.after_count}, got {actual} "
            f"after {CONFIGMAP_SETTLE_SECONDS}s — operator may still be reconciling"
        )
    return res


# ─── Input parsing ─────────────────────────────────────────────────────────


_NAME_SPLIT_RE = re.compile(r"[,，、]")


def split_names(raw: str) -> list[str]:
    """Split a comma/中文-comma/、 separated string into clean names."""
    return [p.strip() for p in _NAME_SPLIT_RE.split(raw) if p.strip()]


def parse_stdin_shorthand(text: str) -> list[InstancePlan]:
    plans: list[InstancePlan] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        m = re.match(r"^(\d+)\s*[:\s]\s*(.+)$", ln)
        if not m:
            raise ValueError(f"unrecognized stdin line: {ln!r} (expected 'ID: name1、name2')")
        plans.append(InstancePlan(her_id=int(m.group(1)), add_names=split_names(m.group(2))))
    return plans


def parse_yaml_file(path: str) -> list[InstancePlan]:
    try:
        import yaml  # type: ignore
    except ImportError:
        sys.exit("--file requires PyYAML: pip install pyyaml")
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, list):
        sys.exit(f"YAML root must be a list of instance plans, got {type(raw).__name__}")
    plans = []
    for entry in raw:
        her_id = int(entry["id"])
        add = entry.get("add") or []
        if isinstance(add, str):
            add = split_names(add)
        plans.append(InstancePlan(
            her_id=her_id,
            add_names=[str(n).strip() for n in add if str(n).strip()],
            user_id_overrides={str(k): str(v) for k, v in (entry.get("user_ids") or {}).items()},
        ))
    return plans


# ─── Reporting ─────────────────────────────────────────────────────────────


def print_report(results: list[InstanceResult]) -> int:
    rc = 0
    for r in results:
        tag = "DRY-RUN" if r.dry_run else ("OK" if r.ok else "ERR")
        print(f"\n=== her-{r.her_id} [{tag}] ===")
        print(f"  before={r.before_count}  after={r.after_count}  added={len(r.added)}  "
              f"already-owner={len(r.skipped_already_owner)}  unresolved={len(r.skipped_unresolved)}")
        if r.added:
            print(f"  + added: {', '.join(r.added)}")
        if r.skipped_already_owner:
            print(f"  · already owner (skipped): {', '.join(r.skipped_already_owner)}")
        if r.skipped_unresolved:
            print("  ⚠ unresolved (NOT added):")
            for name, reason in r.skipped_unresolved:
                print(f"      - {name}: {reason}")
            rc = 1
        if r.error:
            print(f"  ✗ ERROR: {r.error}")
            rc = 1
    print()
    return rc


# ─── Main ──────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Batch-add owners to existing CarHer (her) instances",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file", help="YAML file with list of {id, add, user_ids?} entries")
    src.add_argument("--id", type=int, help="Single instance ID")
    ap.add_argument("--names", help="Comma/中文-comma separated names (with --id)")
    ap.add_argument("--user-id", action="append", default=[],
                    help="Disambiguate a name: --user-id 刘强=a1b2c3d4 (repeatable)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve and print plan, do NOT patch CRDs")
    args = ap.parse_args()

    # Build plans
    if args.file:
        plans = parse_yaml_file(args.file)
    elif args.id:
        if not args.names:
            sys.exit("--id requires --names")
        overrides = {}
        for kv in args.user_id:
            if "=" not in kv:
                sys.exit(f"--user-id must be 'name=user_id', got {kv!r}")
            k, v = kv.split("=", 1)
            overrides[k.strip()] = v.strip()
        plans = [InstancePlan(her_id=args.id, add_names=split_names(args.names),
                              user_id_overrides=overrides)]
    else:
        if sys.stdin.isatty():
            ap.print_usage()
            sys.exit("no input: pass --file, --id, or pipe stdin")
        plans = parse_stdin_shorthand(sys.stdin.read())

    if not plans:
        print("nothing to do (empty input)")
        return 0

    print(f"Processing {len(plans)} instance(s){' [DRY-RUN]' if args.dry_run else ''}...",
          file=sys.stderr)
    results = [process_instance(p, dry_run=args.dry_run) for p in plans]
    return print_report(results)


if __name__ == "__main__":
    sys.exit(main())
