#!/usr/bin/env python3
"""Fetch and summarize recent alert emails from the CarHer 263 mailbox."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIL_DIR = REPO_ROOT / ".codex" / "tmp" / "263-mail"
FETCH_SCRIPT = (
    REPO_ROOT
    / ".codex"
    / "skills"
    / "carher-263-webmail"
    / "scripts"
    / "fetch-263-mail.py"
)
SHANGHAI = ZoneInfo("Asia/Shanghai")
ALERT_START = re.compile(r"^阿里云容器服务：(?P<kind>.+)$", re.MULTILINE)
ALARM_TIME = re.compile(r"报警时间[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取并汇总 263 邮箱最近 N 小时的阿里云/K8s 告警"
    )
    parser.add_argument("--hours", type=float, default=5, help="回看小时数，默认 5")
    parser.add_argument("--limit", type=int, default=30, help="最多抓取的最新邮件数")
    parser.add_argument("--input", type=Path, help="跳过登录，分析已有邮件 JSON")
    parser.add_argument("--raw-out", type=Path, help="抓取邮件 JSON 保存路径")
    parser.add_argument("--json-out", type=Path, help="结构化汇总 JSON 保存路径")
    parser.add_argument("--fetch-timeout", type=int, default=240, help="网页抓取超时秒数")
    parser.add_argument("--headed", action="store_true", help="显示浏览器用于验证码调试")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    return parser.parse_args()


def parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(SHANGHAI)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


def fetch_mail(args: argparse.Namespace, now: datetime) -> Path:
    MAIL_DIR.mkdir(parents=True, exist_ok=True)
    raw_out = args.raw_out or MAIL_DIR / f"messages-{now:%Y%m%d-%H%M%S}.json"
    raw_out = raw_out if raw_out.is_absolute() else REPO_ROOT / raw_out
    log_path = MAIL_DIR / f"fetch-{now:%Y%m%d-%H%M%S}.log"
    command = [
        sys.executable,
        str(FETCH_SCRIPT),
        "--limit",
        str(max(1, args.limit)),
        "--out",
        str(raw_out),
    ]
    if args.headed:
        command.append("--headed")

    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=args.fetch_timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
            raise RuntimeError(
                f"邮箱抓取超过 {args.fetch_timeout}s，已终止浏览器；日志：{log_path}，"
                f"调试截图：{MAIL_DIR / 'debug' / 'last-page.png'}"
            )
    if return_code != 0 or not raw_out.is_file():
        tail = ""
        if log_path.is_file():
            tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
        raise RuntimeError(
            f"邮箱抓取失败（exit={return_code}）。日志：{log_path}\n{tail}".rstrip()
        )
    return raw_out


def field(block: str, *names: str) -> str:
    for name in names:
        match = re.search(rf"^{re.escape(name)}[：:]\s*(.+)$", block, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def classify(kind: str, info: str, subject: str) -> str:
    text = f"{kind} {info} {subject}".lower()
    if "oom" in text or "out of memory" in text:
        return "Pod OOM"
    if "pvc" in text and ("space" in text or "capacity" in text):
        return "PVC 容量"
    if "memory.used.utilization" in text or "内存" in text:
        return "节点内存"
    if "image filesystem" in text or "ephemeral-storage" in text or "free disk" in text:
        return "节点磁盘"
    if "crashloop" in text or "back-off restarting" in text:
        return "Pod 重启"
    if "deadline" in text or "backofflimitexceeded" in text or "job" in text:
        return "Job/CronJob"
    if "failed" in text or "error" in text or "异常" in text or "warn" in text:
        return "K8s 事件"
    return "其他告警"


def normalize_info(info: str) -> str:
    payload = info.strip()
    if payload.startswith("["):
        try:
            values = json.loads(payload)
            if isinstance(values, list):
                payload = json.dumps(sorted({str(value) for value in values}), ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    text = re.sub(r"\bcurrent value\s*[:：]?\s*\d+(?:\.\d+)?%?", "current value", payload, flags=re.I)
    text = re.sub(r"当前值\s*[:：]?\s*\d+(?:\.\d+)?%?", "当前值", text)
    text = re.sub(r"used capacity:\s*\d+(?:\.\d+)?Gi", "used capacity", text, flags=re.I)
    text = re.sub(r"used percentage:\s*\d+(?:\.\d+)?%", "used percentage", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()[:500]


def logical_object(name: str) -> str:
    # CronJob-generated names contain a changing timestamp suffix.
    return re.sub(r"^(carher-\d+-healer)-\d+$", r"\1", name)


def extract_alerts(message: dict[str, Any]) -> list[dict[str, Any]]:
    body = str(message.get("body") or "")
    starts = list(ALERT_START.finditer(body))
    alerts: list[dict[str, Any]] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(body)
        block = body[match.start():end]
        time_match = ALARM_TIME.search(block)
        if not time_match:
            continue
        alarm_at = datetime.strptime(time_match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=SHANGHAI)
        info = field(block, "报警信息")
        event_count_raw = field(block, "报警事件数")
        count_match = re.search(r"\d+", event_count_raw)
        alerts.append(
            {
                "alarm_at": alarm_at,
                "kind": match.group("kind").strip(),
                "namespace": field(block, "Namespace"),
                "object": field(block, "ObjectName", "PodName", "实例", "NodeName") or "未标明对象",
                "node": field(block, "NodeName"),
                "info": info,
                "event_count": int(count_match.group()) if count_match else 1,
                "subject": str(message.get("subject") or ""),
                "sender": str(message.get("sender") or ""),
            }
        )
    return alerts


def summarize(data: dict[str, Any], cutoff: datetime, now: datetime) -> dict[str, Any]:
    selected_messages: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []
    for message in data.get("messages", []):
        message_alerts = [a for a in extract_alerts(message) if cutoff <= a["alarm_at"] <= now]
        if message_alerts:
            selected_messages.append(message)
            alerts.extend(message_alerts)

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for alert in alerts:
        category = classify(alert["kind"], alert["info"], alert["subject"])
        alert["category"] = category
        object_name = logical_object(alert["object"])
        key = (category, object_name, normalize_info(alert["info"]))
        group = groups.setdefault(
            key,
            {
                "category": category,
                "object": object_name,
                "namespace": alert["namespace"],
                "kind": alert["kind"],
                "occurrences": 0,
                "event_count": 0,
                "first_at": alert["alarm_at"],
                "last_at": alert["alarm_at"],
                "latest_info": alert["info"],
            },
        )
        group["occurrences"] += 1
        group["event_count"] += alert["event_count"]
        group["first_at"] = min(group["first_at"], alert["alarm_at"])
        if alert["alarm_at"] >= group["last_at"]:
            group["last_at"] = alert["alarm_at"]
            group["latest_info"] = alert["info"]

    ordered = sorted(groups.values(), key=lambda item: (item["last_at"], item["occurrences"]), reverse=True)
    category_counts = Counter(a["category"] for a in alerts)
    return {
        "account": data.get("account", ""),
        "source_fetched_at": data.get("fetched_at", ""),
        "window_start": cutoff,
        "window_end": now,
        "scanned_messages": len(data.get("messages", [])),
        "matched_messages": len(selected_messages),
        "alert_records": len(alerts),
        "category_counts": dict(category_counts),
        "groups": ordered,
    }


def json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    return value


def render(summary: dict[str, Any]) -> str:
    start = summary["window_start"].strftime("%Y-%m-%d %H:%M:%S")
    end = summary["window_end"].strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"邮箱告警汇总（北京时间 {start} ～ {end}）",
        f"扫描 {summary['scanned_messages']} 封，命中 {summary['matched_messages']} 封，解析 {summary['alert_records']} 条告警。",
    ]
    if not summary["groups"]:
        lines.append("最近时间窗口内没有解析到阿里云/K8s 告警。")
        return "\n".join(lines)
    lines.append("")
    for index, group in enumerate(summary["groups"], 1):
        when = group["last_at"].strftime("%m-%d %H:%M:%S")
        namespace = f" namespace={group['namespace']}" if group["namespace"] else ""
        lines.append(
            f"{index}. [{group['category']}] {group['object']}{namespace}；"
            f"出现 {group['occurrences']} 次/事件 {group['event_count']} 条；最新 {when}"
        )
        lines.append(f"   {group['latest_info'] or group['kind']}")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    if args.hours <= 0:
        raise SystemExit("--hours 必须大于 0")
    now = parse_now(args.now)
    try:
        source = args.input if args.input else fetch_mail(args, now)
        source = source if source.is_absolute() else REPO_ROOT / source
        data = json.loads(source.read_text(encoding="utf-8"))
        summary = summarize(data, now - timedelta(hours=args.hours), now)
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(render(summary))
    if args.json_out:
        output = args.json_out if args.json_out.is_absolute() else REPO_ROOT / args.json_out
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(json_ready(summary), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结构化结果：{output}")
    print(f"原始邮件：{source}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
