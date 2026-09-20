#!/usr/bin/env python3
"""Capacity and admission benchmark for the local GPU DeepSeek V4.1 endpoint.

This intentionally uses only the Python standard library so it can be piped to
the GPU host through scripts/jms.  The V4.1 reference server emits a complete
generation before it emits SSE chunks, so `ttfb_s` is recorded but must not be
interpreted as model-internal token latency.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import json
import os
import statistics
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit


BLOCK = (
    "CarHer local GPU DeepSeek V4.1 Flash capacity benchmark stable prefix. "
    "Numbers: 0123456789. Markers: alpha beta gamma delta epsilon zeta eta "
    "theta iota kappa. The instruction remains deterministic across requests.\n"
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def emit(event: dict) -> None:
    event.setdefault("ts", now_iso())
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)


def make_payload(repeats: int, stream: bool, max_tokens: int = 1) -> bytes:
    prompt = BLOCK * repeats + "Final instruction: answer exactly OK."
    body = {
        "model": "deepseek-v4.1-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": stream,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(values) - 1)
    if lo == hi:
        return values[lo]
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def parse_json_body(body: bytes) -> dict | None:
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def request_once(url: str, payload: bytes, timeout: float, case: str, mode: str, idx: int) -> dict:
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = (parts.path.rstrip("/") if parts.path else "") + "/v1/chat/completions"
    if parts.query:
        path += "?" + parts.query
    started = time.perf_counter()
    status = 0
    body = b""
    ttfb = None
    error = None
    try:
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=timeout)
        conn.request(
            "POST",
            path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
                "Connection": "close",
            },
        )
        response = conn.getresponse()
        status = response.status
        first = response.read(1)
        ttfb = time.perf_counter() - started
        body = first + response.read()
        conn.close()
    except Exception as exc:  # benchmark must retain failures as data
        error = f"{type(exc).__name__}: {exc}"
    total = time.perf_counter() - started
    parsed = parse_json_body(body)
    prompt_tokens = None
    error_message = None
    if parsed:
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
        err = parsed.get("error")
        if isinstance(err, dict):
            error_message = err.get("message")
    if error_message and not error:
        error = str(error_message)
    return {
        "event": "request",
        "case": case,
        "mode": mode,
        "idx": idx,
        "status": status,
        "ok": status == 200,
        "ttfb_s": round(ttfb, 6) if ttfb is not None else None,
        "total_s": round(total, 6),
        "prompt_tokens": prompt_tokens,
        "response_bytes": len(body),
        "completion_tokens": (
            parsed.get("usage", {}).get("completion_tokens")
            if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict)
            else None
        ),
        "error": error,
    }


def run_request(url: str, payload: bytes, timeout: float, case: str, mode: str, idx: int) -> dict:
    result = request_once(url, payload, timeout, case, mode, idx)
    emit(result)
    return result


def summarize(rows: list[dict], case: str, mode: str, concurrency: int, wave: int) -> None:
    ok = [row for row in rows if row["ok"]]
    durations = [row["total_s"] for row in ok]
    ttfb = [row["ttfb_s"] for row in ok if row["ttfb_s"] is not None]
    completions = [row["completion_tokens"] for row in ok if isinstance(row.get("completion_tokens"), int)]
    statuses: dict[str, int] = {}
    for row in rows:
        key = str(row["status"])
        statuses[key] = statuses.get(key, 0) + 1
    emit(
        {
            "event": "wave_summary",
            "case": case,
            "mode": mode,
            "concurrency": concurrency,
            "wave": wave,
            "n": len(rows),
            "ok": len(ok),
            "statuses": statuses,
            "total_p50_s": round(percentile(durations, 50), 6) if durations else None,
            "total_p95_s": round(percentile(durations, 95), 6) if durations else None,
            "total_max_s": round(max(durations), 6) if durations else None,
            "ttfb_p50_s": round(percentile(ttfb, 50), 6) if ttfb else None,
            "ttfb_p95_s": round(percentile(ttfb, 95), 6) if ttfb else None,
            "ttfb_max_s": round(max(ttfb), 6) if ttfb else None,
            "completion_tokens_avg": round(statistics.mean(completions), 2) if completions else None,
            "slow_gt_10s": sum(value > 10 for value in ttfb),
            "slow_gt_30s": sum(value > 30 for value in ttfb),
        }
    )


class HostSampler:
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.samples: list[dict] = []

    def start(self) -> None:
        self.thread.start()

    def join(self) -> dict:
        self.stop.set()
        self.thread.join(timeout=3)
        gpu_util: list[float] = []
        gpu_mem: list[float] = []
        loads: list[float] = []
        for sample in self.samples:
            gpu_util.extend(sample.get("gpu_util", []))
            gpu_mem.extend(sample.get("gpu_mem_pct", []))
            if sample.get("load1") is not None:
                loads.append(sample["load1"])
        return {
            "sample_count": len(self.samples),
            "gpu_util_max_pct": max(gpu_util) if gpu_util else None,
            "gpu_util_avg_pct": round(statistics.mean(gpu_util), 2) if gpu_util else None,
            "gpu_mem_max_pct": max(gpu_mem) if gpu_mem else None,
            "load1_max": max(loads) if loads else None,
            "last": self.samples[-1] if self.samples else None,
        }

    def _run(self) -> None:
        while not self.stop.is_set():
            sample: dict = {"ts": now_iso()}
            try:
                raw = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                        "--format=csv,noheader,nounits",
                    ],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=3,
                )
                utils: list[float] = []
                mem_pct: list[float] = []
                temps: list[float] = []
                for line in raw.splitlines():
                    fields = [item.strip() for item in line.split(",")]
                    if len(fields) >= 4:
                        utils.append(float(fields[0]))
                        mem_pct.append(float(fields[1]) / float(fields[2]) * 100)
                        temps.append(float(fields[3]))
                sample.update({"gpu_util": utils, "gpu_mem_pct": mem_pct, "gpu_temp_max": max(temps) if temps else None})
            except Exception as exc:
                sample["gpu_error"] = type(exc).__name__
            try:
                sample["load1"] = float(open("/proc/loadavg").read().split()[0])
            except Exception:
                pass
            self.samples.append(sample)
            self.stop.wait(self.interval)


def health_check(url: str, timeout: float) -> None:
    parts = urlsplit(url)
    for endpoint in ("/health", "/v1/models"):
        # Use a small independent GET to keep the benchmark dependency-free.
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        conn_cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=timeout)
        started = time.perf_counter()
        try:
            conn.request("GET", endpoint)
            resp = conn.getresponse()
            body = resp.read()
            emit({
                "event": "health",
                "endpoint": endpoint,
                "status": resp.status,
                "total_s": round(time.perf_counter() - started, 6),
                "body": parse_json_body(body),
            })
        except Exception as exc:
            emit({"event": "health", "endpoint": endpoint, "status": 0, "total_s": round(time.perf_counter() - started, 6), "error": f"{type(exc).__name__}: {exc}"})
        finally:
            conn.close()


def run_boundary(url: str, timeout: float) -> None:
    cases = [
        ("small", 16),
        ("10k", 204),
        ("14k", 285),
        ("near-cap", 334),
        ("over-cap", 335),
        ("100k", 3600),
        ("800k", 28800),
        ("1M", 36000),
    ]
    emit({"event": "boundary_start", "url": url, "cases": [{"case": c, "repeats": n} for c, n in cases]})
    for case, repeats in cases:
        payload = make_payload(repeats, stream=False)
        emit({"event": "case_start", "suite": "boundary", "case": case, "repeats": repeats, "payload_bytes": len(payload)})
        run_request(url, payload, timeout, case, "boundary", 1)


def parse_levels(raw: str) -> list[int]:
    levels = [int(item.strip()) for item in raw.split(",") if item.strip()]
    if not levels or any(level < 1 for level in levels):
        raise ValueError("levels must contain positive integers")
    return levels


def run_matrix(url: str, timeout: float, levels: list[int], waves: int, repeats: int, max_tokens: int, stream: bool) -> None:
    payload = make_payload(repeats, stream=stream, max_tokens=max_tokens)
    emit({"event": "matrix_start", "url": url, "case": "10k", "repeats": repeats, "max_tokens": max_tokens, "stream": stream, "payload_bytes": len(payload), "levels": levels, "waves": waves})
    next_idx = 0
    for concurrency in levels:
        for wave in range(1, waves + 1):
            rows: list[dict] = []
            emit({"event": "wave_start", "suite": "matrix", "case": "10k", "concurrency": concurrency, "wave": wave})
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = []
                for _ in range(concurrency):
                    next_idx += 1
                    futures.append(pool.submit(request_once, url, payload, timeout, "10k", "concurrent", next_idx))
                for future in concurrent.futures.as_completed(futures):
                    row = future.result()
                    emit(row)
                    rows.append(row)
            summarize(rows, "10k", "concurrent", concurrency, wave)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8768")
    parser.add_argument("--suite", choices=("boundary", "matrix"), required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--levels", default="1,2,4,8,16")
    parser.add_argument("--waves", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=204)
    parser.add_argument("--max-tokens", type=int, default=1)
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()
    levels = parse_levels(args.levels)
    emit({"event": "bench_start", "suite": args.suite, "url": args.url, "host": os.uname().nodename, "pid": os.getpid()})
    health_check(args.url, min(args.timeout, 20))
    sampler = HostSampler()
    sampler.start()
    try:
        if args.suite == "boundary":
            run_boundary(args.url, args.timeout)
        else:
            run_matrix(args.url, args.timeout, levels, max(1, args.waves), args.repeats, args.max_tokens, not args.no_stream)
    finally:
        metrics = sampler.join()
        emit({"event": "host_metrics", **metrics})
        emit({"event": "bench_done", "suite": args.suite})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
