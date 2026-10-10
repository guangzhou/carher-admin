#!/usr/bin/env python3
"""按标题前缀批量删除飞书云文档（默认前缀「飞书文档测试」）。

默认 **只列不删**（dry-run）。要真删必须显式 --yes。

判据与坑（都是踩过的）：
- `drive +search` 是**模糊搜索**：query「飞书文档测试」也会命中「飞书文档写入测试」。
  ⇒ 服务端结果只当候选集，**前缀由本地 startswith 判定**，标题取自
  `title_highlighted` 去掉 <h></h> 高亮标签。
- `--page-size` 上限 20（默认 15）⇒ **必须翻页**，否则分页外的文档看起来「不存在」。
- 命中分两类，删除动词不同：
  `entity_type == "WIKI"` → `lark-cli wiki +node-delete`（知识库节点）
  其他（DOC/FILE/...）    → `lark-cli drive +delete --type <docx|sheet|...>`
- 删除**不可逆**（飞书侧进回收站，能否捞回取决于租户回收站策略）。
  ⇒ 跑之前先看 dry-run 清单；真删时清单会落盘到 --manifest 留证。

用法：
  # 1) 先看要删什么（不会动任何东西）
  ./scripts/lark-delete-docs-by-title-prefix.py

  # 2) 确认无误再真删
  ./scripts/lark-delete-docs-by-title-prefix.py --yes

  # 别的前缀 / 只删自己名下的
  ./scripts/lark-delete-docs-by-title-prefix.py --prefix "临时导出" --mine --yes
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from typing import Any

LARK_CLI = "lark-cli"
HIGHLIGHT_RE = re.compile(r"</?h>")
# drive +search 的 --query 服务端上限 30 个码点，超了直接 99992402
QUERY_MAX_CODEPOINTS = 30


def run_cli(args: list[str], *, timeout: int = 120) -> dict[str, Any]:
    """跑一条 lark-cli，把 stdout 解析成 JSON。

    lark-cli 有两个已知坑：业务失败时 exit code 仍可能是 0（要读 body 里的 ok），
    stdout 尾部还会挂 _notice。所以这里既看 returncode 也看 ok。
    """
    proc = subprocess.run(
        [LARK_CLI, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    raw = proc.stdout.strip()
    if not raw:
        raise RuntimeError(
            f"lark-cli 无输出 (exit={proc.returncode}): {' '.join(args)}\n"
            f"stderr: {proc.stderr.strip()[:500]}"
        )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"lark-cli 输出不是 JSON (exit={proc.returncode}): {raw[:500]}"
        ) from exc


def plain_title(hit: dict[str, Any]) -> str:
    """去掉搜索高亮标签，拿回真实标题。"""
    return HIGHLIGHT_RE.sub("", hit.get("title_highlighted") or "")


def search_all(query: str, *, only_mine: bool, doc_types: str | None) -> list[dict[str, Any]]:
    """翻完所有页，返回候选命中（未做前缀过滤）。"""
    hits: list[dict[str, Any]] = []
    page_token: str | None = None
    pages = 0
    while True:
        args = [
            "drive", "+search",
            "--query", query,
            "--only-title",
            "--page-size", "20",
            "--format", "json",
        ]
        if only_mine:
            args.append("--mine")
        if doc_types:
            args += ["--doc-types", doc_types]
        if page_token:
            args += ["--page-token", page_token]

        body = run_cli(args)
        if not body.get("ok"):
            raise RuntimeError(f"搜索失败: {json.dumps(body, ensure_ascii=False)[:500]}")

        data = body.get("data") or {}
        batch = data.get("results") or []
        hits.extend(batch)
        pages += 1

        page_token = data.get("page_token") if data.get("has_more") else None
        if not page_token or not batch:
            break
        if pages >= 100:  # 兜底，别被翻页游标带着无限转
            print("⚠️  已翻 100 页仍 has_more，提前停止（结果可能不完整）", file=sys.stderr)
            break
    return hits


def pick_targets(hits: list[dict[str, Any]], prefix: str) -> tuple[list[dict[str, Any]], list[str]]:
    """本地按前缀判定，顺手按 token 去重。返回 (命中, 被前缀筛掉的标题)。"""
    targets: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    for hit in hits:
        title = plain_title(hit)
        meta = hit.get("result_meta") or {}
        token = meta.get("token")
        if not token:
            continue
        if not title.startswith(prefix):
            rejected.append(title)
            continue
        targets.setdefault(token, {
            "token": token,
            "title": title,
            "entity_type": hit.get("entity_type") or "",
            "doc_type": (meta.get("doc_types") or "").lower(),
            "owner": meta.get("owner_name") or "",
            "url": meta.get("url") or "",
            "create_time": meta.get("create_time_iso") or "",
        })
    ordered = sorted(targets.values(), key=lambda t: (t["create_time"], t["title"]))
    return ordered, rejected


def delete_one(target: dict[str, Any], *, dry_run: bool) -> tuple[bool, str]:
    """删一个。WIKI 节点和 Drive 文件是两个动词，不能混。"""
    if target["entity_type"] == "WIKI":
        args = [
            "wiki", "+node-delete",
            "--node-token", target["token"],
            "--obj-type", "wiki",
            "--include-children",
            "--format", "json",
        ]
    else:
        doc_type = target["doc_type"] or "docx"
        args = [
            "drive", "+delete",
            "--file-token", target["token"],
            "--type", doc_type,
            "--format", "json",
        ]
    args.append("--dry-run" if dry_run else "--yes")

    try:
        body = run_cli(args)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:300]

    if body.get("ok"):
        return True, ""
    return False, json.dumps(body, ensure_ascii=False)[:300]


def route_of(target: dict[str, Any]) -> str:
    """这条目标走哪个删除动词。wiki 节点和 Drive 文件的 scope 是分开的。"""
    if target["entity_type"] == "WIKI":
        return "wiki"
    return f"drive:{target['doc_type'] or 'docx'}"


def preflight_scope(targets: list[dict[str, Any]]) -> dict[str, str]:
    """开删之前先断言「我删得动」，别让 N 条一样的 missing_scope 冒充 N 个故障。

    返回 {route: "" 表示通, 非空表示该路径的失败原因}。

    `--dry-run` 会在发请求之前跑本地 scope 门禁，所以它既不动数据、又能
    把缺 scope 问题当场炸出来。每种删除路径各验一条就够。
    2026-09-28 实测：当前 token 缺 `space:document:delete` ⇒ 28 条 docx 全失败，
    而 wiki 路径靠 `wiki:wiki` 是通的 —— 两条路径必须分开验。
    """
    problems: dict[str, str] = {}
    for t in targets:
        route = route_of(t)
        if route in problems:
            continue
        ok, err = delete_one(t, dry_run=True)
        problems[route] = "" if ok else err
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="按标题前缀批量删除飞书云文档")
    ap.add_argument("--prefix", default="飞书文档测试", help="标题前缀（本地精确判定）")
    ap.add_argument("--query", default=None,
                    help="给搜索接口的关键词，默认用 prefix（服务端上限 30 码点）")
    ap.add_argument("--doc-types", default=None,
                    help="限定类型，如 docx,sheet,bitable,wiki（默认不限）")
    ap.add_argument("--mine", action="store_true", help="只处理我名下（owner）的文档")
    ap.add_argument("--yes", action="store_true", help="真删。不给就只列不删")
    ap.add_argument("--manifest", default=None,
                    help="把清单写成 CSV 留证（真删模式默认写 /tmp/lark-deleted-<ts>.csv）")
    ap.add_argument("--sleep", type=float, default=0.2, help="每次删除之间的间隔秒数")
    args = ap.parse_args()

    query = args.query or args.prefix
    if len(query) > QUERY_MAX_CODEPOINTS:
        query = query[:QUERY_MAX_CODEPOINTS]
        print(f"ℹ️  query 超 30 码点，已截成「{query}」（前缀判定仍用完整 prefix）")

    print(f"搜索关键词：{query}   前缀判据：{args.prefix}"
          f"{'   仅我名下' if args.mine else ''}")

    try:
        hits = search_all(query, only_mine=args.mine, doc_types=args.doc_types)
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"❌ 搜索阶段失败：{exc}", file=sys.stderr)
        return 2

    targets, rejected = pick_targets(hits, args.prefix)
    print(f"服务端候选 {len(hits)} 条 → 前缀命中 {len(targets)} 条"
          f"（模糊命中但前缀不符被排除 {len(rejected)} 条）")
    if rejected:
        for title in sorted(set(rejected))[:10]:
            print(f"   ⏭  跳过（前缀不符）：{title}")

    if not targets:
        print("没有匹配的文档，什么都没做。")
        return 0

    print()
    for idx, t in enumerate(targets, 1):
        kind = "WIKI 节点" if t["entity_type"] == "WIKI" else f"Drive {t['doc_type'] or 'docx'}"
        print(f"{idx:>3}. [{kind}] {t['title']}  ({t['owner']})\n     {t['url']}")
    print()

    manifest_path = args.manifest
    if args.yes and not manifest_path:
        manifest_path = f"/tmp/lark-deleted-{time.strftime('%Y%m%d-%H%M%S')}.csv"

    # 先断言删得动，再动手。缺 scope 会让每一条都失败，报成 N 个故障很误导。
    # 但**按路径分流**：wiki 和 drive 的 scope 是两回事，一个缺不该拖累另一个。
    print("自检：验证每条删除路径的权限…")
    routes = preflight_scope(targets)
    blocked = {r for r, err in routes.items() if err}
    for route, err in sorted(routes.items()):
        if err:
            scope_hint = ""
            if "missing_scope" in err:
                m = re.search(r"missing required scope\(s\): ([\w:.,\s]+)", err)
                if m:
                    scope_hint = f"  缺 scope: {m.group(1).strip()}"
            print(f"   ❌ {route} 不可删{scope_hint}")
        else:
            print(f"   ✅ {route} 权限就绪")

    runnable = [t for t in targets if route_of(t) not in blocked]
    skipped = [t for t in targets if route_of(t) in blocked]
    if blocked:
        print(f"\n⚠️  {len(skipped)} 条落在无权限路径上，本轮会**跳过**（不是删失败，是没去删）。")
        print("   补权限：lark-cli auth login --scope \"space:document:delete\" --no-wait --json")
        print("   （点开 verification_url 授权，再 lark-cli auth login --device-code <code>；scope 是累积的，不会动现有权限）")
    if not runnable:
        print("\n🔴 没有任何一条是当前权限删得动的，什么都没做。")
        return 3

    if not args.yes:
        print(f"\n🟡 dry-run：可删 {len(runnable)} 条 / 跳过 {len(skipped)} 条，**均未执行**。确认后加 --yes。")
        print("   注意：删除不可逆（飞书侧进回收站，能否捞回看租户策略）。")
        return 0

    targets = runnable

    ok_rows: list[dict[str, Any]] = []
    failed: list[tuple[dict[str, Any], str]] = []
    for idx, t in enumerate(targets, 1):
        done, err = delete_one(t, dry_run=False)
        flag = "✅" if done else "❌"
        print(f"{flag} [{idx}/{len(targets)}] {t['title']}" + (f"  ← {err}" if err else ""))
        (ok_rows.append(t) if done else failed.append((t, err)))
        if args.sleep:
            time.sleep(args.sleep)

    if manifest_path:
        with open(manifest_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["title", "token", "entity_type", "doc_type", "owner", "url", "create_time"]
            )
            writer.writeheader()
            writer.writerows(ok_rows)
        print(f"\n清单已落盘：{manifest_path}（{len(ok_rows)} 行）")

    # 复查：重搜一次，确认前缀命中确实归零
    print("\n复查（重搜一次）…")
    try:
        remain, _ = pick_targets(
            search_all(query, only_mine=args.mine, doc_types=args.doc_types), args.prefix
        )
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"⚠️  复查没跑成：{exc}（不代表删除失败，重跑本脚本 dry-run 自己看）")
        remain = None

    print(f"\n删除成功 {len(ok_rows)} / 失败 {len(failed)}")
    if remain is not None:
        print(f"复查残留：{len(remain)} 条" + ("（搜索索引有延迟，稍后再查一次）" if remain else " ✅"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
