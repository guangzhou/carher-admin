#!/usr/bin/env python3
"""Probe Wangsu Anthropic gateway latency for selected Claude models."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import tempfile
import time
from pathlib import Path


MODELS = ("anthropic.claude-opus-4-7", "anthropic.claude-opus-4-8")


def run_once(endpoint: str, api_key: str, model: str, prompt: str, timeout: int) -> dict:
    payload = {
        "model": model,
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as req:
        json.dump(payload, req, ensure_ascii=False)
        req_path = Path(req.name)
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as body:
        body_path = Path(body.name)

    curl_format = "\n__CURL_METRICS__%{json}\n"
    cmd = [
        "curl",
        "-sS",
        "--max-time",
        str(timeout),
        "-o",
        str(body_path),
        "-w",
        curl_format,
        "-X",
        "POST",
        endpoint,
        "-H",
        "content-type: application/json",
        "-H",
        "anthropic-version: 2023-06-01",
        "-H",
        f"x-api-key: {api_key}",
        "--data-binary",
        f"@{req_path}",
    ]
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout + 5)
        raw_body = body_path.read_text(errors="replace")
        marker = "__CURL_METRICS__"
        metrics_raw = proc.stdout.split(marker, 1)[1].strip() if marker in proc.stdout else "{}"
        metrics = json.loads(metrics_raw)
        parsed = json.loads(raw_body) if raw_body else {}
        text = ""
        for item in parsed.get("content", []) if isinstance(parsed, dict) else []:
            if item.get("type") == "text":
                text += item.get("text", "")
        error = ""
        if proc.returncode != 0:
            error = (proc.stderr or proc.stdout).strip()[:220]
        elif isinstance(parsed, dict) and parsed.get("error"):
            error = json.dumps(parsed.get("error"), ensure_ascii=False)[:220]
        return {
            "time": started,
            "model": model,
            "http_code": int(metrics.get("http_code") or 0),
            "exit_code": proc.returncode,
            "ttft_s": round(float(metrics.get("time_starttransfer") or 0), 3),
            "total_s": round(float(metrics.get("time_total") or 0), 3),
            "connect_s": round(float(metrics.get("time_connect") or 0), 3),
            "output_chars": len(text),
            "error": error,
        }
    finally:
        req_path.unlink(missing_ok=True)
        body_path.unlink(missing_ok=True)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[idx]


def summarize(rows: list[dict]) -> list[dict]:
    result = []
    for model in MODELS:
        model_rows = [r for r in rows if r["model"] == model]
        ok_rows = [r for r in model_rows if r["http_code"] == 200 and r["exit_code"] == 0 and not r["error"]]
        totals = [r["total_s"] for r in ok_rows]
        ttfts = [r["ttft_s"] for r in ok_rows]
        result.append(
            {
                "model": model,
                "success": len(ok_rows),
                "total": len(model_rows),
                "avg_total_s": round(statistics.mean(totals), 3) if totals else 0,
                "p50_total_s": round(statistics.median(totals), 3) if totals else 0,
                "p90_total_s": round(percentile(totals, 0.9), 3) if totals else 0,
                "min_total_s": round(min(totals), 3) if totals else 0,
                "max_total_s": round(max(totals), 3) if totals else 0,
                "avg_ttft_s": round(statistics.mean(ttfts), 3) if ttfts else 0,
            }
        )
    return result


def write_markdown(path: Path, endpoint: str, rows: list[dict], summary: list[dict]) -> None:
    lines = [
        "# 网宿 Opus 4.7 / 4.8 简单延迟测试",
        "",
        f"- 测试时间：{time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 测试入口：`{endpoint}`",
        "- 请求方式：Anthropic `/v1/messages`，非流式，`max_tokens=16`，短 prompt，每个模型 10 次串行请求",
        "- 指标说明：TTFT 为 curl `time_starttransfer`，总耗时为 curl `time_total`；单位均为秒",
        "",
        "## 汇总",
        "",
        "| 模型 | 成功/总数 | 平均总耗时 | P50 总耗时 | P90 总耗时 | 最小 | 最大 | 平均 TTFT |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary:
        lines.append(
            f"| `{item['model']}` | {item['success']}/{item['total']} | {item['avg_total_s']:.3f} | "
            f"{item['p50_total_s']:.3f} | {item['p90_total_s']:.3f} | {item['min_total_s']:.3f} | "
            f"{item['max_total_s']:.3f} | {item['avg_ttft_s']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## 明细",
            "",
            "| 轮次 | 模型 | HTTP | TTFT | 总耗时 | 连接耗时 | 输出字符 | 错误摘要 |",
            "|---:|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    counters = {model: 0 for model in MODELS}
    for row in rows:
        counters[row["model"]] += 1
        error = row["error"].replace("|", "\\|") if row["error"] else ""
        lines.append(
            f"| {counters[row['model']]} | `{row['model']}` | {row['http_code']} | {row['ttft_s']:.3f} | "
            f"{row['total_s']:.3f} | {row['connect_s']:.3f} | {row['output_chars']} | {error} |"
        )
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-id", default="sqix2pnh")
    parser.add_argument("--api-key", default=os.getenv("WANGSU_GATEWAY_KEY", ""))
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--prompt", default="Reply with exactly OK.")
    parser.add_argument("--out", default="/tmp/wangsu-opus-latency-report.md")
    parser.add_argument("--json-out", default="/tmp/wangsu-opus-latency-results.json")
    args = parser.parse_args()
    if not args.api_key:
        raise SystemExit("missing --api-key or WANGSU_GATEWAY_KEY")

    endpoint = f"https://aigateway.edgecloudapp.com/v2/gws/{args.gateway_id}/anthropic/v1/messages"
    rows: list[dict] = []
    for model in MODELS:
        for idx in range(args.count):
            row = run_once(endpoint, args.api_key, model, args.prompt, args.timeout)
            rows.append(row)
            print(
                f"{model} #{idx + 1}: http={row['http_code']} ttft={row['ttft_s']:.3f}s "
                f"total={row['total_s']:.3f}s err={bool(row['error'])}",
                flush=True,
            )
            time.sleep(0.5)

    summary = summarize(rows)
    Path(args.json_out).write_text(json.dumps({"endpoint": endpoint, "rows": rows, "summary": summary}, ensure_ascii=False, indent=2))
    write_markdown(Path(args.out), endpoint, rows, summary)
    print(f"wrote {args.out}")
    print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
