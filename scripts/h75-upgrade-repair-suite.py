#!/usr/bin/env python3
"""Single entrypoint for H75 upgrade repair and regression gates.

This orchestrates the repair scripts that cover the repeated H75 upgrade
failure classes: Dify login-entry 500s, runtime internal URLs, Hermes
LiteLLM config drift, title failure cards, and canary issue-login smoke.
Fleet mutation is intentionally opt-in and must follow a successful canary.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ROOT / "runs" / "h75-upgrade-repair-suite"
DEFAULT_REQUESTER_OPEN_ID = "ou_fbaf6eec244d1c4a4541a477678dab17"
DEFAULT_REQUESTER_NAME = "刘国现"
DEFAULT_SOURCE_CHAT_ID = "oc_62da991ea6725b4bc9632a1bf0664759"
DEFAULT_SOURCE_MESSAGE_ID = "om_x100b6d119f839ca8b13999415cc3a1f"
SENSITIVE_DEPLOYMENTS = {"carher-2"}
RUNTIME_TABLE_HEADER = [
    "deployment",
    "h75",
    "bad_env",
    "missing_env",
    "public_url",
    "paused",
    "replicas",
    "ready",
    "updated",
    "unavailable",
    "surge_rs",
    "pod_config_checked",
    "pod_config_bad",
    "hermes_config_checked",
    "hermes_config_bad",
    "title_patch_checked",
    "title_patch_bad",
    "needs_rollout",
]

REPAIR_MATRIX = [
    {
        "problem": "266 /dify 登录入口生成失败",
        "repair": "Dify API 启用 SQLALCHEMY_POOL_PRE_PING/RECYCLE；bootstrap 登录与 workspace switch 增加 transient retry；登录 nonce 跨 bootstrap 副本共享。",
        "verify": "读取 Dify live config；grep bootstrap retry/shared nonce 代码；主动调用 pod 内 /runtime-patches/dify-login-card.py 生成登录入口并消费 /v1/exchange。",
    },
    {
        "problem": "266 Hermes 回复 Unknown provider 'litellm'",
        "repair": "收敛 H75 env 和 Hermes config.yaml 到 litellm + chat_completions + K8s 内网 LiteLLM endpoint。",
        "verify": "pod 内检查 /opt/data/.hermes/config.yaml provider/transport/base_url，禁止旧 S3/公网 endpoint。",
    },
    {
        "problem": "266 回复后出现 title 生成失败提示卡",
        "repair": "通过 H75 runtime postStart 注入 title failure silent patch；title 失败只记录，不向用户发失败卡。",
        "verify": "pod 内检查 title patch marker CARHER_TITLE_FAILURE_SILENT_PATCH。",
    },
    {
        "problem": "群内 /dify 验证方式错误",
        "repair": "脚本只做 pod 内登录入口主动 smoke；真实群验证必须 @目标 bot 并校验目标 app_id，不用普通群消息替代。",
        "verify": "issue-login smoke 使用 requester/source message 生成测试登录入口，并验证 auto exchange；不输出短效登录 URL/token。",
    },
    {
        "problem": "H75 runtime 配置漂移",
        "repair": "收敛 REDIS_URL、FEISHU_GROUP_POLICY、FEISHU_ALLOW_ALL_USERS、Dify 内网 URL、LiteLLM 内网 URL、插件刷新开关。",
        "verify": "deployment template 审计；rollout 后检查当前 Ready pod 的 workflow/dify-config、Hermes config、title patch。",
    },
    {
        "problem": "Dify issue-login 404 / bot not bootstrapped",
        "repair": "当 workflow/dify-config.json 的 bot_id 或 lifecycle health 与当前 carher-N 不一致时，调用内网 bootstrap 重新登记规范 bot_id，并写回 workspace/api_key/lifecycle_token。",
        "verify": "pod_config gate 校验 bot_id、精确 lifecycle_base_url 和 lifecycle /health；随后 Dify issue-login + auto exchange smoke 必须 ok=True。",
    },
    {
        "problem": "Dify auto 页面显示链接已失效",
        "repair": "修复 bootstrap 2 副本内存 nonce 不共享：将 nonce 加密写入 /Data/dify-bootstrap/login-nonces，并在 /v1/exchange 按 token 哈希读取。",
        "verify": "每个 smoke 生成测试登录链接后立即调用公网 /v1/exchange，要求首次 200 且同一 cookie jar 二次访问仍 200。",
    },
    {
        "problem": "脚本顺序导致旧 Her 先 smoke 后修复",
        "repair": "batch 模式固定顺序为 Dify infra -> 266/268 runtime canary -> 全量 runtime repair -> 全量 Dify smoke。",
        "verify": "全量 Dify smoke 只在 runtime repair/rollout 成功后执行，避免旧公网 lifecycle 配置导致误失败。",
    },
    {
        "problem": "敏感 Her 显式授权只在总入口生效",
        "repair": "`--include-sensitive` 必须从 suite 透传到下层 runtime runner 和 runtime repair 脚本。",
        "verify": "summary 记录 sensitive_override，runtime-batch log 中必须出现目标 Her 的 pod_config/hermes_config/title_patch gate。",
    },
    {
        "problem": "历史 Failed pod 污染 Pod 异常扫描",
        "repair": "Pod anomaly collection 同时输出全部异常、当前异常、历史遗留异常，避免用旧 Failed pod 否定当前 Ready pod。",
        "verify": "current-pod gate 使用 Deployment selector 选择当前 Ready pod；stale-pod 文件仅作为清理线索。",
    },
]


class StepFailure(RuntimeError):
    def __init__(self, step: str, returncode: int) -> None:
        super().__init__(f"{step} failed with exit code {returncode}")
        self.step = step
        self.returncode = returncode


TRANSPORT_FAILURE_SIGNATURES = [
    "unexpected EOF",
    "TLS handshake timeout",
    "connection reset by peer",
    "use of closed network connection",
    "transport is closing",
]


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def has_transport_failure(log_path: Path) -> bool:
    if not log_path.exists():
        return False
    text = log_path.read_text(encoding="utf-8", errors="replace")[-50000:]
    return any(signature in text for signature in TRANSPORT_FAILURE_SIGNATURES)


def run_step(
    name: str,
    cmd: list[str],
    *,
    run_dir: Path,
    env: dict[str, str] | None = None,
    transport_retries: int = 0,
) -> None:
    log_path = run_dir / f"{name}.log"
    attempts = transport_retries + 1
    for attempt in range(1, attempts + 1):
        print(f"step_start\t{name}\tattempt={attempt}/{attempts}\t{' '.join(cmd)}", flush=True)
        mode = "w" if attempt == 1 else "a"
        with log_path.open(mode, encoding="utf-8") as log:
            log.write(f"# step={name}\n# attempt={attempt}/{attempts}\n# cwd={ROOT}\n# command={' '.join(cmd)}\n\n")
            proc = subprocess.Popen(
                cmd,
                cwd=ROOT,
                env={**os.environ, **(env or {})},
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
            returncode = proc.wait()
        print(f"step_done\t{name}\tattempt={attempt}/{attempts}\trc={returncode}\tlog={log_path}", flush=True)
        if returncode == 0:
            return
        if attempt < attempts and has_transport_failure(log_path):
            print(f"step_retry\t{name}\treason=transport_failure\tlog={log_path}", flush=True)
            continue
        raise StepFailure(name, returncode)


def kubectl_json(args: list[str], *, namespace: str) -> dict:
    kubeconfig = str(Path("~/.kube/config").expanduser())
    proc = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, "-n", namespace, *args, "-o", "json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)


def current_pod(namespace: str, deployment: str) -> str:
    dep = kubectl_json(["get", "deployment", deployment], namespace=namespace)
    labels = dep.get("spec", {}).get("selector", {}).get("matchLabels") or {}
    selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
    pods = kubectl_json(["get", "pods", "-l", selector], namespace=namespace)
    ready: list[tuple[str, str]] = []
    for item in pods.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        status = item.get("status", {})
        if status.get("phase") != "Running":
            continue
        is_ready = any(
            cond.get("type") == "Ready" and cond.get("status") == "True"
            for cond in status.get("conditions") or []
        )
        if is_ready:
            ready.append((item.get("metadata", {}).get("creationTimestamp", ""), name))
    if not ready:
        raise RuntimeError(f"{deployment} has no Ready running pod")
    ready.sort(reverse=True)
    return ready[0][1]


def carher_container(deployment: dict) -> dict:
    for container in deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []) or []:
        if container.get("name") == "carher":
            return container
    return {}


def is_h75_deployment(deployment: dict) -> bool:
    template = deployment.get("spec", {}).get("template", {})
    template_text = json.dumps(template, ensure_ascii=False)
    annotations = template.get("metadata", {}).get("annotations") or {}
    image = carher_container(deployment).get("image", "")
    return (
        "h75" in image
        or annotations.get("carher.io/runtime-profile") == "h75-openclaw"
        or "carher-base-config-h75" in template_text
    )


def h75_her_ids(namespace: str, *, include_sensitive: bool = False) -> list[str]:
    data = kubectl_json(["get", "deployments", "-l", "app=carher-user"], namespace=namespace)
    ids: list[int] = []
    for item in data.get("items", []):
        name = item.get("metadata", {}).get("name", "")
        if not name.startswith("carher-") or not is_h75_deployment(item):
            continue
        if name in SENSITIVE_DEPLOYMENTS and not include_sensitive:
            continue
        suffix = name.removeprefix("carher-")
        if suffix.isdigit():
            ids.append(int(suffix))
    return [str(value) for value in sorted(ids)]


def write_gate_summary(run_dir: Path, rows: list[dict[str, str]]) -> None:
    path = run_dir / "gate-summary.tsv"
    cols = ["gate", "status", "evidence", "detail"]
    with path.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(cols) + "\n")
        for row in rows:
            fh.write("\t".join(row.get(col, "") for col in cols) + "\n")
    print(f"summary\t{path}", flush=True)


def write_repair_matrix(run_dir: Path) -> None:
    path = run_dir / "repair-verification-matrix.md"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("# H75 266 问题修复与验证矩阵\n\n")
        fh.write("## 脚本设计反思\n\n")
        fh.write("- 不能用日志空白证明没问题；默认门禁必须是配置读取和主动 smoke。\n")
        fh.write("- 不能在 runtime 修复前做全量 Dify smoke；旧 pod 可能仍保留公网 lifecycle 配置。\n")
        fh.write("- 出现新失败类型时，先把检测和修复写回脚本，再继续推广。\n\n")
        fh.write("| 问题 | 脚本修复动作 | 脚本验证办法 |\n")
        fh.write("|---|---|---|\n")
        for item in REPAIR_MATRIX:
            fh.write(f"| {item['problem']} | {item['repair']} | {item['verify']} |\n")
    print(f"matrix\t{path}", flush=True)
    for item in REPAIR_MATRIX:
        print(f"coverage\t{item['problem']}\trepair={item['repair']}\tverify={item['verify']}", flush=True)


def write_tsv(path: Path, rows: list[dict[str, str]], cols: list[str]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write("\t".join(cols) + "\n")
        for row in rows:
            fh.write("\t".join(row.get(col, "") for col in cols) + "\n")
    print(f"collection\t{path}\trows={len(rows)}", flush=True)


def parse_runtime_log(log_path: Path) -> list[dict[str, str]]:
    check_details: dict[str, dict[str, str]] = {}
    rows: list[dict[str, str]] = []
    if not log_path.exists():
        return rows
    for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.split("\t")
        if len(parts) >= 4 and parts[0] in {"pod_config", "hermes_config", "title_patch"}:
            check, deployment, pod, detail = parts[:4]
            item = check_details.setdefault(deployment, {})
            item[f"{check}_pod"] = pod
            item[f"{check}_detail"] = detail
            continue
        if len(parts) >= 4 and parts[0] in {"pod_config_patch", "hermes_config_patch", "title_patch_apply"}:
            check, deployment, pod, detail = parts[:4]
            item = check_details.setdefault(deployment, {})
            item[f"{check}_pod"] = pod
            item[f"{check}_detail"] = detail
            continue
        if len(parts) >= 3 and parts[0] == "rollout_result":
            _, deployment, detail = parts[:3]
            item = check_details.setdefault(deployment, {})
            item["rollout_result"] = detail
            continue
        if len(parts) >= 5 and parts[0] == "error":
            _, error_code, deployment, pod, detail = parts[:5]
            item = check_details.setdefault(deployment, {})
            item["runtime_error"] = error_code
            item["runtime_error_pod"] = pod
            item["runtime_error_detail"] = detail
            continue
        if len(parts) == len(RUNTIME_TABLE_HEADER) and parts[0].startswith("carher-"):
            row = dict(zip(RUNTIME_TABLE_HEADER, parts, strict=True))
            rows.append(row)
    # Transport retries append another full scan to the same log. Keep the
    # newest row per deployment so collection TSVs describe final state.
    deduped: dict[str, dict[str, str]] = {}
    for row in rows:
        deduped[row["deployment"]] = row
    rows = list(deduped.values())
    for row in rows:
        details = check_details.get(row["deployment"], {})
        row.update(details)
        drift_reasons: list[str] = []
        for key in ["bad_env", "missing_env"]:
            if row.get(key) and row[key] != "-":
                drift_reasons.append(f"{key}={row[key]}")
        for key in ["public_url", "paused", "surge_rs", "pod_config_bad", "hermes_config_bad", "title_patch_bad"]:
            if row.get(key) == "True":
                drift_reasons.append(key)
        for key in ["pod_config_detail", "hermes_config_detail", "title_patch_detail"]:
            value = row.get(key, "")
            if value and value != "ok":
                drift_reasons.append(f"{key}={value}")
        for key in ["pod_config_patch_detail", "hermes_config_patch_detail", "title_patch_apply_detail"]:
            value = row.get(key, "")
            if value and value != "ok":
                drift_reasons.append(f"{key}={value}")
        rollout_result = row.get("rollout_result", "")
        if rollout_result and rollout_result != "ok":
            drift_reasons.append(f"rollout_result={rollout_result}")
        if row.get("runtime_error"):
            drift_reasons.append(
                f"runtime_error={row.get('runtime_error')}:{row.get('runtime_error_detail', '')}"
            )
        row["drift_reason"] = ",".join(drift_reasons) or "-"
    return rows


def write_runtime_collection(run_dir: Path, step_name: str) -> None:
    log_path = run_dir / f"{step_name}.log"
    rows = parse_runtime_log(log_path)
    if not rows:
        return
    cols = [
        "deployment",
        "needs_rollout",
        "drift_reason",
        "bad_env",
        "missing_env",
        "public_url",
        "paused",
        "replicas",
        "ready",
        "updated",
        "unavailable",
        "pod_config_detail",
        "hermes_config_detail",
        "title_patch_detail",
        "pod_config_pod",
        "hermes_config_pod",
        "title_patch_pod",
        "pod_config_patch_detail",
        "hermes_config_patch_detail",
        "title_patch_apply_detail",
        "pod_config_patch_pod",
        "hermes_config_patch_pod",
        "title_patch_apply_pod",
        "rollout_result",
        "runtime_error",
        "runtime_error_pod",
        "runtime_error_detail",
    ]
    all_path = run_dir / f"collection-runtime-{step_name}-all.tsv"
    drift_path = run_dir / f"collection-runtime-{step_name}-drift.tsv"
    write_tsv(all_path, rows, cols)
    drift_rows = [row for row in rows if row.get("needs_rollout") == "True" or row.get("drift_reason") not in {"", "-"}]
    write_tsv(drift_path, drift_rows, cols)


def parse_smoke_logs(run_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for log_path in sorted(run_dir.glob("dify-smoke-*.log")):
        for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.split("\t")
            if len(parts) < 4 or parts[0] != "smoke":
                continue
            row = {
                "deployment": parts[1],
                "pod": parts[2],
                "ok": "",
                "detail": "",
                "log": str(log_path),
            }
            detail_parts: list[str] = []
            for item in parts[3:]:
                if item.startswith("ok="):
                    row["ok"] = item.removeprefix("ok=")
                else:
                    detail_parts.append(item)
            row["detail"] = "\t".join(detail_parts)
            rows.append(row)
    return rows


def write_smoke_collection(run_dir: Path) -> None:
    rows = parse_smoke_logs(run_dir)
    if not rows:
        return
    cols = ["deployment", "pod", "ok", "detail", "log"]
    write_tsv(run_dir / "collection-dify-smoke-all.tsv", rows, cols)
    failures = [row for row in rows if row.get("ok") != "True"]
    write_tsv(run_dir / "collection-dify-smoke-failures.tsv", failures, cols)


def deployment_from_pod_name(name: str) -> str:
    parts = name.split("-")
    if len(parts) <= 2:
        return name
    return "-".join(parts[:-2])


def pod_ready(status: dict) -> str:
    for cond in status.get("conditions") or []:
        if cond.get("type") == "Ready":
            return str(cond.get("status") == "True")
    return "False"


def pod_reason(status: dict) -> str:
    if status.get("reason"):
        return str(status.get("reason"))
    reasons: list[str] = []
    for item in status.get("containerStatuses") or []:
        state = item.get("state") or {}
        for state_name in ["waiting", "terminated"]:
            detail = state.get(state_name)
            if detail and detail.get("reason"):
                reasons.append(f"{item.get('name')}:{detail.get('reason')}")
    return ",".join(reasons) or "-"


def pod_row(item: dict) -> dict[str, str]:
    name = item.get("metadata", {}).get("name", "")
    status = item.get("status", {})
    return {
        "deployment": deployment_from_pod_name(name),
        "pod": name,
        "phase": status.get("phase", "") or "-",
        "ready": pod_ready(status),
        "reason": pod_reason(status),
        "node": status.get("nodeName") or "-",
        "created": item.get("metadata", {}).get("creationTimestamp") or "-",
    }


def is_healthy_pod_row(row: dict[str, str]) -> bool:
    return row.get("phase") == "Running" and row.get("ready") == "True" and row.get("reason") == "-"


def write_pod_anomaly_collection(run_dir: Path, namespace: str) -> None:
    cols = ["deployment", "pod", "phase", "ready", "reason", "node", "created"]
    try:
        pods = kubectl_json(["get", "pods", "-l", "app=carher-user"], namespace=namespace)
    except Exception as exc:
        row = {"deployment": "-", "pod": "-", "phase": "collection_error", "ready": "False", "reason": str(exc), "node": "-", "created": "-"}
        write_tsv(
            run_dir / "collection-pod-anomalies.tsv",
            [row],
            cols,
        )
        write_tsv(
            run_dir / "collection-current-pod-anomalies.tsv",
            [row],
            cols,
        )
        return

    all_rows = [pod_row(item) for item in pods.get("items", [])]
    rows_by_deployment: dict[str, list[dict[str, str]]] = {}
    for row in all_rows:
        rows_by_deployment.setdefault(row["deployment"], []).append(row)

    anomaly_rows: list[dict[str, str]] = []
    current_rows: list[dict[str, str]] = []
    stale_rows: list[dict[str, str]] = []
    for deployment_rows in rows_by_deployment.values():
        ready_created = sorted(row["created"] for row in deployment_rows if is_healthy_pod_row(row))
        has_healthy_pod = bool(ready_created)
        for row in deployment_rows:
            if is_healthy_pod_row(row):
                continue
            anomaly_rows.append(row)
            is_stale_terminal = (
                has_healthy_pod
                and row["phase"] in {"Failed", "Succeeded"}
            )
            if is_stale_terminal:
                stale_rows.append(row)
            else:
                current_rows.append(row)

    sort_key = lambda row: (row["deployment"], row["created"], row["pod"])
    anomaly_rows.sort(key=sort_key)
    current_rows.sort(key=sort_key)
    stale_rows.sort(key=sort_key)
    write_tsv(run_dir / "collection-pod-anomalies.tsv", anomaly_rows, cols)
    write_tsv(run_dir / "collection-current-pod-anomalies.tsv", current_rows, cols)
    write_tsv(run_dir / "collection-stale-pod-anomalies.tsv", stale_rows, cols)


def current_pod_gate_ids(args: argparse.Namespace, dify_smoke_targets: list[str]) -> list[str]:
    ids = ["266", "268"]
    if args.mode == "batch":
        ids.extend(parse_targets(args.targets))
    if not args.dify_smoke_all_h75:
        ids.extend(dify_smoke_targets)

    out: list[str] = []
    seen: set[str] = set()
    for her_id in ids:
        if her_id in seen:
            continue
        seen.add(her_id)
        out.append(her_id)
    return out


def write_available_collections(run_dir: Path) -> None:
    for step_name in ["runtime-canary-before-batch", "runtime-canary", "runtime-audit", "runtime-batch"]:
        write_runtime_collection(run_dir, step_name)
    write_smoke_collection(run_dir)
    write_pod_anomaly_collection(run_dir, "carher")


def parse_targets(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for part in value.replace(",", " ").split():
            part = part.strip()
            if part:
                out.append(part)
    return out


def chunked(values: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        raise ValueError("chunk size must be positive")
    return [values[index : index + size] for index in range(0, len(values), size)]


def run_dify_smoke_chunks(args: argparse.Namespace, run_dir: Path, targets: list[str]) -> int:
    if not targets:
        return 0
    total = 0
    for index, chunk in enumerate(chunked(targets, args.dify_smoke_chunk_size), start=1):
        cmd = [
            "bash",
            "scripts/dify-login-smoke-runner.sh",
            "--carher-namespace",
            args.namespace,
            "--requester-open-id",
            args.requester_open_id,
            "--requester-name",
            args.requester_name,
            "--source-chat-id",
            args.source_chat_id,
            "--source-message-id",
            args.source_message_id,
        ]
        for her_id in chunk:
            cmd += ["--smoke-her", her_id]
        run_step(f"dify-smoke-{index:03d}", cmd, run_dir=run_dir, transport_retries=2)
        write_smoke_collection(run_dir)
        total += len(chunk)
    return total


def resolve_dify_smoke_targets(args: argparse.Namespace) -> list[str]:
    targets = parse_targets(args.dify_smoke_targets)
    if args.dify_smoke_all_h75:
        return h75_her_ids(args.namespace, include_sensitive=args.include_sensitive)
    if args.mode == "batch" and not targets:
        return parse_targets(args.targets)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full H75 repair/regression suite from one entrypoint.")
    parser.add_argument("--mode", choices=["canary", "audit", "batch"], default="canary")
    parser.add_argument("--namespace", default="carher")
    parser.add_argument("--dify-namespace", default="dify")
    parser.add_argument("--targets", nargs="*", default=[], help="Target Her ids for batch/runtime repair.")
    parser.add_argument("--wave-size", type=int, default=10)
    parser.add_argument("--skip-dify-infra", action="store_true")
    parser.add_argument("--skip-runtime-repair", action="store_true")
    parser.add_argument("--skip-canary-smoke", action="store_true")
    parser.add_argument("--scan-dify-logs", action="store_true", help="Optional incident evidence only; not a default gate.")
    parser.add_argument("--skip-log-scan", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--dify-smoke-all-h75", action="store_true", help="Run Dify issue-login smoke for every H75 Her except sensitive exclusions.")
    parser.add_argument("--dify-smoke-targets", nargs="*", default=[], help="Specific Her ids for Dify issue-login smoke.")
    parser.add_argument("--dify-smoke-chunk-size", type=int, default=25)
    parser.add_argument("--include-sensitive", action="store_true", help="Include sensitive deployments such as carher-2.")
    parser.add_argument("--requester-open-id", default=DEFAULT_REQUESTER_OPEN_ID)
    parser.add_argument("--requester-name", default=DEFAULT_REQUESTER_NAME)
    parser.add_argument("--source-chat-id", default=DEFAULT_SOURCE_CHAT_ID)
    parser.add_argument("--source-message-id", default=DEFAULT_SOURCE_MESSAGE_ID)
    parser.add_argument("--log-since", default="15m")
    parser.add_argument("--collect-only", default="", help="Only generate collection TSV files from an existing run directory.")
    parser.add_argument(
        "--allow-batch-without-canary",
        action="store_true",
        help="Dangerous escape hatch. Default batch mode reruns canary gates first.",
    )
    args = parser.parse_args()

    if args.collect_only:
        collect_dir = Path(args.collect_only).expanduser()
        if not collect_dir.is_absolute():
            collect_dir = ROOT / collect_dir
        write_available_collections(collect_dir)
        return 0

    run_dir = RUN_ROOT / timestamp()
    run_dir.mkdir(parents=True, exist_ok=True)
    write_repair_matrix(run_dir)
    rows: list[dict[str, str]] = []

    try:
        if args.include_sensitive:
            explicit_targets = parse_targets(args.targets) or parse_targets(args.dify_smoke_targets)
            detail = ",".join(explicit_targets) if explicit_targets else "all selected sensitive H75 targets"
            rows.append(
                {
                    "gate": "sensitive_override",
                    "status": "pass",
                    "evidence": "--include-sensitive",
                    "detail": f"explicit user override for {detail}",
                }
            )

        if not args.skip_dify_infra:
            cmd = [
                sys.executable,
                "scripts/dify-login-entry-repair.py",
                "--apply",
                "--verify",
            ]
            if args.scan_dify_logs and not args.skip_log_scan:
                cmd += ["--scan-logs", "--log-since", args.log_since]
            if not args.skip_canary_smoke:
                cmd += [
                    "--canary-smoke",
                    "--requester-open-id",
                    args.requester_open_id,
                    "--requester-name",
                    args.requester_name,
                    "--source-chat-id",
                    args.source_chat_id,
                    "--source-message-id",
                    args.source_message_id,
                ]
            run_step("dify-login-entry-repair", cmd, run_dir=run_dir)
            rows.append(
                {
                    "gate": "dify_login_entry",
                    "status": "pass",
                    "evidence": str(run_dir / "dify-login-entry-repair.log"),
                    "detail": "pool pre-ping, bootstrap retry, active issue-login smoke",
                }
            )

        dify_smoke_targets = resolve_dify_smoke_targets(args)
        should_smoke_before_runtime = args.mode != "batch" or args.skip_runtime_repair
        if dify_smoke_targets and should_smoke_before_runtime:
            smoke_count = run_dify_smoke_chunks(args, run_dir, dify_smoke_targets)
            rows.append(
                {
                    "gate": "dify_issue_login_smoke",
                    "status": "pass",
                    "evidence": str(run_dir),
                    "detail": f"smoked {smoke_count} H75 Her issue-login paths",
                }
            )

        if args.mode == "batch" and not args.allow_batch_without_canary and not args.skip_runtime_repair:
            run_step(
                "runtime-canary-before-batch",
                ["bash", "scripts/h75-runtime-repair-runner.sh", "canary"],
                run_dir=run_dir,
                transport_retries=2,
            )
            write_runtime_collection(run_dir, "runtime-canary-before-batch")
            rows.append(
                {
                    "gate": "runtime_canary_before_batch",
                    "status": "pass",
                    "evidence": str(run_dir / "collection-runtime-runtime-canary-before-batch-drift.tsv"),
                    "detail": "266/268 runtime repair gates",
                }
            )

        if not args.skip_runtime_repair:
            if args.mode == "canary":
                runner_args = ["canary"]
            elif args.mode == "audit":
                runner_args = ["audit"]
            else:
                targets = parse_targets(args.targets)
                runner_args = ["batch", "--wave-size", str(args.wave_size)]
                if targets:
                    runner_args += ["--targets", *targets]
            if args.include_sensitive:
                runner_args.append("--include-sensitive")
            run_step(
                f"runtime-{args.mode}",
                ["bash", "scripts/h75-runtime-repair-runner.sh", *runner_args],
                run_dir=run_dir,
                transport_retries=2,
            )
            write_runtime_collection(run_dir, f"runtime-{args.mode}")
            rows.append(
                {
                    "gate": f"runtime_{args.mode}",
                    "status": "pass",
                    "evidence": str(run_dir / f"collection-runtime-runtime-{args.mode}-drift.tsv"),
                    "detail": "H75 env/internal URLs/Hermes config/title patch/pod config",
                }
            )

        if dify_smoke_targets and not should_smoke_before_runtime:
            smoke_count = run_dify_smoke_chunks(args, run_dir, dify_smoke_targets)
            write_smoke_collection(run_dir)
            rows.append(
                {
                    "gate": "dify_issue_login_smoke_after_runtime_repair",
                    "status": "pass",
                    "evidence": str(run_dir / "collection-dify-smoke-failures.tsv"),
                    "detail": f"smoked {smoke_count} H75 Her issue-login paths after runtime repair",
                }
            )

        # Explicit pod selection gates protect against old Error/Evicted pods
        # hiding behind deployment-level kubectl shortcuts.
        for her_id in current_pod_gate_ids(args, dify_smoke_targets):
            pod = current_pod(args.namespace, f"carher-{her_id}")
            rows.append(
                {
                    "gate": f"current_pod_{her_id}",
                    "status": "pass",
                    "evidence": pod,
                    "detail": "current Ready pod selected explicitly",
                }
            )
    except StepFailure as exc:
        write_available_collections(run_dir)
        rows.append({"gate": exc.step, "status": "fail", "evidence": str(run_dir / f"{exc.step}.log"), "detail": str(exc)})
        write_gate_summary(run_dir, rows)
        return exc.returncode
    except Exception as exc:
        write_available_collections(run_dir)
        rows.append({"gate": "suite", "status": "fail", "evidence": str(run_dir), "detail": str(exc)})
        write_gate_summary(run_dir, rows)
        return 1

    write_available_collections(run_dir)
    write_gate_summary(run_dir, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
