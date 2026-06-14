#!/usr/bin/env python3
"""
Align CarHer her instance owners with the her-registry bitable.

Source of truth: the her-registry bitable
  base_token = B6kubcpAfaW6ktsEOyGcrur3ncg
  table_id   = tblcvJPRIFV91yHy
where each row's `姓名` (申请人) and `使用用户` are person-fields keyed by `ID`.

Approach: for each K8s HerInstance, use that her's OWN app_access_token to
read the bitable filtered by ID. Bitable's person-field rewrites the `id`
into the caller-app's per-app open_id automatically — so what we pull
back is directly usable as `spec.owner`. Zero cross-app id math.

Reference: her-owner-resolution skill, Path C.

POLICY (default, configurable via flags):
  - Source set      = base 姓名 ∪ 使用用户 (per-app open_ids via path C)
  - Patch direction = ONLY add, never delete (preserve K8s extras like history)
  - Empty base set  = skip with "no source" report
  - Missing K8s    = report and skip (orphan in base)
  - Missing base   = report and skip (K8s-only, no source)

USAGE
  scripts/align-her-owners-from-base.py             # dry-run by default
  scripts/align-her-owners-from-base.py --apply     # actually patch CRDs
  scripts/align-her-owners-from-base.py --apply --only 180,234,235  # subset
  scripts/align-her-owners-from-base.py --apply --replace  # mirror mode
                                                          # (deletes K8s extras)
  scripts/align-her-owners-from-base.py --report-only > /tmp/diff.md
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

NS = "carher"
BASE_TOKEN = "B6kubcpAfaW6ktsEOyGcrur3ncg"
TABLE_ID = "tblcvJPRIFV91yHy"
SETTLE_SECONDS = 8


# ─── Helpers ───────────────────────────────────────────────────────────────


def kubectl_json(args: list[str]) -> dict:
    p = subprocess.run(["kubectl", *args, "-o", "json"],
                       capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {p.stderr[:300]}")
    return json.loads(p.stdout)


def kubectl_run(args: list[str]) -> str:
    p = subprocess.run(["kubectl", *args], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {p.stderr[:300]}")
    return p.stdout


def http_post(url: str, body: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def http_get(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


# ─── Data classes ──────────────────────────────────────────────────────────


@dataclass
class Diff:
    her_id: int
    crd_name: str
    display_name: str
    existing: list[str] = field(default_factory=list)
    base_target: list[str] = field(default_factory=list)
    name_lookup: dict[str, str] = field(default_factory=dict)  # open_id → 姓名
    extras_audit: dict[str, str] = field(default_factory=dict)  # K8s extras → name (round-trip)
    to_add: list[str] = field(default_factory=list)
    to_remove: list[str] = field(default_factory=list)
    skip_reason: str | None = None
    error: str | None = None
    patched: bool = False
    after_count_actual: int = -1
    after_count_expected: int = -1


# ─── App credential cache ─────────────────────────────────────────────────


_APP_TOKEN_CACHE: dict[str, str] = {}


def get_app_access_token(app_id: str, app_secret: str) -> str:
    key = f"{app_id}:{app_secret}"
    if key in _APP_TOKEN_CACHE:
        return _APP_TOKEN_CACHE[key]
    d = http_post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    tok = d.get("app_access_token")
    if not tok:
        raise RuntimeError(f"app_access_token failed for {app_id}: {d}")
    _APP_TOKEN_CACHE[key] = tok
    return tok


def get_app_secret_value(secret_ref: str) -> str:
    d = kubectl_json(["get", "secret", "-n", NS, secret_ref])
    data = d.get("data", {})
    for k in ("app_secret", "appSecret"):
        if k in data:
            return base64.b64decode(data[k]).decode()
    raise RuntimeError(f"secret {secret_ref}: no app_secret/appSecret key")


# ─── Path C: read bitable with her's own credentials ──────────────────────


def fetch_per_app_owners(app_id: str, app_secret: str, her_id: int) -> tuple[list[str], dict[str, str]]:
    """Returns (ordered list of per-app open_ids, {open_id: name} lookup)."""
    tok = get_app_access_token(app_id, app_secret)
    url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BASE_TOKEN}"
           f"/tables/{TABLE_ID}/records/search")
    body = {
        "filter": {
            "conjunction": "and",
            "conditions": [{"field_name": "ID", "operator": "is", "value": [str(her_id)]}],
        }
    }
    d = http_post(url, body, {"Authorization": f"Bearer {tok}"})
    if d.get("code") != 0:
        raise RuntimeError(f"bitable search failed: code={d.get('code')} msg={d.get('msg')}")
    items = d.get("data", {}).get("items", [])
    seen, ordered = set(), []
    name_lookup: dict[str, str] = {}
    for it in items:
        f = it.get("fields", {})
        for field_name in ("姓名", "使用用户"):
            for p in (f.get(field_name) or []):
                oid = p.get("id")
                nm = p.get("name", "")
                if oid and oid not in seen:
                    seen.add(oid)
                    ordered.append(oid)
                    name_lookup[oid] = nm
    return ordered, name_lookup


_TENANT_TOKEN_CACHE: dict[str, str] = {}


def get_tenant_token(app_id: str, app_secret: str) -> str:
    key = f"tenant:{app_id}"
    if key in _TENANT_TOKEN_CACHE:
        return _TENANT_TOKEN_CACHE[key]
    d = http_post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    tok = d.get("tenant_access_token")
    if not tok:
        raise RuntimeError(f"tenant_access_token failed for {app_id}: {d}")
    _TENANT_TOKEN_CACHE[key] = tok
    return tok


def round_trip_lookup(app_id: str, app_secret: str, open_id: str) -> str:
    """Return the user's `name` as seen by `app_id`, or '' if lookup fails / not found."""
    try:
        tok = get_tenant_token(app_id, app_secret)
        d = http_get(
            f"https://open.feishu.cn/open-apis/contact/v3/users/{open_id}?user_id_type=open_id",
            headers={"Authorization": f"Bearer {tok}"},
        )
        if d.get("code") != 0:
            return f"⚠ {d.get('code')}: {d.get('msg', '')[:40]}"
        return d.get("data", {}).get("user", {}).get("name", "")
    except Exception as e:
        return f"⚠ lookup err: {str(e)[:40]}"


# ─── Per-instance ──────────────────────────────────────────────────────────


def process_one(her: dict, *, replace: bool, audit_extras: bool) -> Diff:
    spec = her.get("spec", {})
    her_id = int(spec.get("userId"))
    crd_name = her.get("metadata", {}).get("name", f"her-{her_id}")
    display_name = spec.get("name", "")
    diff = Diff(her_id=her_id, crd_name=crd_name, display_name=display_name)

    diff.existing = [o for o in (spec.get("owner") or "").split("|") if o]

    app_id = spec.get("appId")
    secret_ref = spec.get("appSecretRef", f"carher-{her_id}-secret")
    if not app_id or not secret_ref:
        diff.error = "missing appId/appSecretRef in spec"
        return diff
    try:
        app_secret = get_app_secret_value(secret_ref)
    except Exception as e:
        diff.error = f"secret {secret_ref} read failed: {e}"
        return diff

    try:
        target, name_lookup = fetch_per_app_owners(app_id, app_secret, her_id)
    except Exception as e:
        diff.error = f"bitable read failed: {e}"
        return diff

    diff.base_target = target
    diff.name_lookup = name_lookup

    if not target:
        diff.skip_reason = "base has no row for this ID, or row has empty 姓名/使用用户"
        return diff

    existing_set = set(diff.existing)
    target_set = set(target)
    diff.to_add = [o for o in target if o not in existing_set]
    extras = [o for o in diff.existing if o not in target_set]
    diff.to_remove = extras if replace else []

    # Audit extras: round-trip each "K8s has but base doesn't" to identify ghosts
    if audit_extras and extras:
        for oid in extras:
            diff.extras_audit[oid] = round_trip_lookup(app_id, app_secret, oid)

    return diff


# ─── Patching ──────────────────────────────────────────────────────────────


def apply_patch(diff: Diff, *, replace: bool) -> None:
    if not diff.to_add and not (replace and diff.to_remove):
        return  # no-op

    if replace:
        merged = diff.base_target  # mirror mode = exact target
    else:
        seen, merged = set(), []
        for o in (diff.existing + diff.to_add):
            if o and o not in seen:
                seen.add(o); merged.append(o)

    diff.after_count_expected = len(merged)
    patch = json.dumps({"spec": {"owner": "|".join(merged)}})
    try:
        kubectl_run(["patch", "her", diff.crd_name, "-n", NS, "--type=merge", "-p", patch])
    except Exception as e:
        diff.error = f"kubectl patch failed: {e}"
        return

    # Verify ConfigMap settled
    for _ in range(3):
        time.sleep(SETTLE_SECONDS // 3 + 1)
        try:
            d = kubectl_json(["get", "cm", "-n", NS, f"carher-{diff.her_id}-user-config"])
            cfg = json.loads(d["data"]["openclaw.json"])
            actual = len(cfg["channels"]["feishu"]["dm"]["allowFrom"])
            diff.after_count_actual = actual
            if actual == diff.after_count_expected:
                diff.patched = True
                return
        except Exception:
            continue
    diff.error = (f"ConfigMap verify mismatch: expected {diff.after_count_expected}, "
                  f"got {diff.after_count_actual}")


# ─── Reporting ─────────────────────────────────────────────────────────────


def render_report(diffs: list[Diff], *, apply_mode: bool, replace: bool) -> str:
    lines: list[str] = []
    head = f"# her owner alignment {'(APPLIED)' if apply_mode else '(DRY-RUN)'}"
    if replace: head += " [MIRROR mode — extras will be deleted]"
    lines.append(head)
    lines.append("")

    # Summary
    total = len(diffs)
    no_change = sum(1 for d in diffs if not d.error and not d.skip_reason and not d.to_add and not d.to_remove)
    will_add = sum(1 for d in diffs if d.to_add)
    will_remove = sum(1 for d in diffs if d.to_remove)
    skipped = sum(1 for d in diffs if d.skip_reason)
    errored = sum(1 for d in diffs if d.error)
    patched = sum(1 for d in diffs if d.patched)
    total_add = sum(len(d.to_add) for d in diffs)
    total_rm = sum(len(d.to_remove) for d in diffs)

    lines.append(f"**Summary** ({total} instances)")
    lines.append("")
    lines.append(f"- 已对齐 (no change): **{no_change}**")
    lines.append(f"- 待追加 owner: **{will_add}** ({total_add} owners total)")
    if replace:
        lines.append(f"- 待删除 owner: **{will_remove}** ({total_rm} owners total)")
    lines.append(f"- 跳过 (无 base 数据): **{skipped}**")
    lines.append(f"- 错误: **{errored}**")
    if apply_mode:
        lines.append(f"- 已 patch: **{patched}**")
    lines.append("")

    # Errors first
    errs = [d for d in diffs if d.error]
    if errs:
        lines.append("## ❌ 错误")
        lines.append("")
        for d in errs:
            lines.append(f"- **her-{d.her_id}** ({d.display_name}): {d.error}")
        lines.append("")

    # Skipped (no base)
    skips = [d for d in diffs if d.skip_reason and not d.error]
    if skips:
        lines.append("## ⏭ 跳过 (无 base 来源)")
        lines.append("")
        for d in skips:
            lines.append(f"- **her-{d.her_id}** ({d.display_name}): {d.skip_reason}")
        lines.append("")

    # Changes
    changes = [d for d in diffs if (d.to_add or d.to_remove or d.extras_audit) and not d.error]
    if changes:
        lines.append("## 🔧 变更")
        lines.append("")
        for d in sorted(changes, key=lambda x: x.her_id):
            tag = "✅ patched" if d.patched else ("⚠ pending" if apply_mode else "DRY-RUN")
            add_n = len(d.to_add); rm_n = len(d.to_remove); audit_n = len(d.extras_audit)
            ttl = (f"+{add_n}" if add_n else "") + (f" -{rm_n}" if rm_n else "")
            if audit_n and not (add_n or rm_n): ttl = f"audit {audit_n} extras"
            lines.append(f"### her-{d.her_id} ({d.display_name}) — {ttl} [{tag}]")
            lines.append(f"- 当前 owner ({len(d.existing)}): {len(d.existing)} 个")
            lines.append(f"- base 目标 ({len(d.base_target)}): {len(d.base_target)} 个")
            if d.to_add:
                lines.append(f"- ➕ 追加 {add_n}:")
                for oid in d.to_add:
                    lines.append(f"  - `{oid}` ({d.name_lookup.get(oid, '?')})")
            if d.to_remove:
                lines.append(f"- ➖ 删除 {rm_n} (mirror 模式):")
                for oid in d.to_remove:
                    lines.append(f"  - `{oid}`")
            if d.extras_audit:
                # Highlight ghost owners — ones whose round-trip name doesn't match any base name
                base_names = set(d.name_lookup.values())
                lines.append(f"- 🔍 K8s 多余 owner 审计 ({audit_n} 个):")
                for oid, rt_name in d.extras_audit.items():
                    is_ghost = rt_name and not rt_name.startswith("⚠") and rt_name not in base_names
                    flag = " 🚨 GHOST" if is_ghost else ""
                    lines.append(f"  - `{oid}` → {rt_name or '(空)'}{flag}")
            lines.append("")

    # No change (collapsed)
    if no_change:
        lines.append(f"## ✓ 已对齐 ({no_change} 个，无需变更)")
        lines.append("")
        ids = sorted([d.her_id for d in diffs if not d.error and not d.skip_reason
                      and not d.to_add and not d.to_remove])
        # Compact print
        compact = ", ".join(f"her-{i}" for i in ids[:30])
        if len(ids) > 30: compact += f", ...+{len(ids)-30} more"
        lines.append(compact)

    return "\n".join(lines)


# ─── Main ──────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Actually patch CRDs. Default is dry-run.")
    ap.add_argument("--replace", action="store_true",
                    help="Mirror mode: K8s owner = base target exactly. Removes "
                         "K8s extras not in base. Default is add-only.")
    ap.add_argument("--only", default="",
                    help="Comma-separated her IDs to process (default: all).")
    ap.add_argument("--report-only", action="store_true",
                    help="Print report without per-instance progress logs.")
    ap.add_argument("--workers", type=int, default=4,
                    help="Concurrent fetch workers (default 4, kept low for "
                         "kubectl/feishu rate limits).")
    ap.add_argument("--audit-extras", action="store_true",
                    help="For each K8s owner not in base, round-trip lookup the "
                         "name to detect ghost/wrong owners. Slower (extra API "
                         "call per extra). Useful before deciding mirror mode.")
    args = ap.parse_args()

    only = {int(x) for x in args.only.split(",") if x.strip()} if args.only else None

    # Fetch all HerInstances
    print("fetching K8s HerInstances...", file=sys.stderr)
    hers = kubectl_json(["get", "her", "-n", NS]).get("items", [])
    if only:
        hers = [h for h in hers if int(h.get("spec", {}).get("userId", -1)) in only]
    hers.sort(key=lambda h: int(h.get("spec", {}).get("userId", 0)))
    print(f"  → {len(hers)} instances{' (filtered by --only)' if only else ''}",
          file=sys.stderr)

    # Resolve diffs (parallelizable, read-only)
    print(f"resolving via path C with {args.workers} workers"
          f"{' + audit-extras' if args.audit_extras else ''}...", file=sys.stderr)
    def _process(h): return process_one(h, replace=args.replace, audit_extras=args.audit_extras)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        diffs = list(ex.map(_process, hers))

    progress_total_changes = sum(1 for d in diffs if (d.to_add or d.to_remove) and not d.error)
    print(f"  → {progress_total_changes} instances need changes",
          file=sys.stderr)

    # Apply if requested (sequential to avoid TLS handshake parallel issues)
    if args.apply:
        print("\napplying patches sequentially...", file=sys.stderr)
        for d in diffs:
            if (d.to_add or (args.replace and d.to_remove)) and not d.error and not d.skip_reason:
                if not args.report_only:
                    print(f"  her-{d.her_id}: +{len(d.to_add)} -{len(d.to_remove)}",
                          file=sys.stderr, end=" ", flush=True)
                apply_patch(d, replace=args.replace)
                if not args.report_only:
                    print(f"{'✓' if d.patched else '✗'}", file=sys.stderr)
                time.sleep(0.3)  # gentle pacing

    # Report
    print(render_report(diffs, apply_mode=args.apply, replace=args.replace))

    # Exit code
    return 1 if any(d.error for d in diffs) else 0


if __name__ == "__main__":
    sys.exit(main())
