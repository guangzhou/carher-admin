#!/usr/bin/env python3
"""Distill a CarHer instance into sanitized handoff reports."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path
from typing import Any


SECRET_KEY_RE = re.compile(
    r"(secret|token|api[_-]?key|apikey|password|passwd|credential|cookie|session|litellmkey|appsecret)",
    re.I,
)
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|app-[A-Za-z0-9_\-]{8,}|lct_[A-Za-z0-9_\-]{8,}|Bearer\s+[A-Za-z0-9._\-]{16,})"
)
OPEN_ID_RE = re.compile(r"\b(ou|oc)_[A-Za-z0-9]{12,}\b")
APP_ID_RE = re.compile(r"cli_[A-Za-z0-9]{12,}")
TEXT_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>[\"']?(?:api[_-]?key|token|access[_-]?token|refresh[_-]?token|lifecycle[_-]?token|sandbox[_-]?token|secret|password|app[_-]?secret)[\"']?\s*[:=]\s*[\"'])(?P<value>[^\"'\n]+)(?P<suffix>[\"'])",
    re.I,
)

SENSITIVE_PATH_MARKERS = (
    "/sessions/",
    "/.lark-cli/",
    "/devices/",
    "/delivery-queue/",
    "/memory/main.sqlite",
    "/auth",
    "appsecret",
    "master.key",
    "cookie",
    "token",
)
KNOWLEDGE_EXTENSIONS = (".md", ".txt", ".xml", ".docx", ".pptx", ".xlsx", ".pdf")
TEXT_SNIPPET_EXTENSIONS = (".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".xml")
REMOTE_ROOTS = (
    "/data/.openclaw/workspace",
    "/data/.openclaw/compaction-reports",
    "/data/.openclaw/feishu-doc-backups",
    "/data/.openclaw/wiki/main",
    "/data/.openclaw/skills-custom",
    "/data/.openclaw/workflow",
    "/data/.openclaw/cron",
    "/data/.openclaw/feishu-groups",
    "/data/.openclaw/memory",
    "/data/.openclaw/sessions",
    "/opt/data/.hermes",
)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def redact(value: Any) -> Any:
    """Recursively redact secret-like keys and values."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                result[key] = "[REDACTED]"
            else:
                result[key] = redact(item)
        return result
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    text = TEXT_SECRET_ASSIGNMENT_RE.sub(r"\g<prefix>[REDACTED]\g<suffix>", text)
    return SECRET_VALUE_RE.sub("[REDACTED]", text)


def run_command(argv: list[str], *, timeout: int = 60) -> str:
    completed = subprocess.run(
        argv,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return completed.stdout


def run_command_json(argv: list[str], *, timeout: int = 60) -> dict[str, Any]:
    return parse_json_output(run_command(argv, timeout=timeout))


def run_remote(command: str, *, timeout: int = 90) -> str:
    return run_command(["scripts/jms", "ssh", "k8s-work-226", command], timeout=timeout)


def remote_kubectl(args: str, *, timeout: int = 90) -> str:
    return run_remote(f"kubectl -n carher {args}", timeout=timeout)


def parse_json(raw: str, fallback: Any = None) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return fallback


def parse_json_output(raw: str) -> dict[str, Any]:
    parsed = parse_json(raw)
    if isinstance(parsed, dict):
        return parsed
    start = raw.rfind("\n{")
    if start >= 0:
        parsed = parse_json(raw[start + 1 :])
        if isinstance(parsed, dict):
            return parsed
    stripped = raw.strip()
    if stripped.startswith("{"):
        parsed = parse_json(stripped)
        if isinstance(parsed, dict):
            return parsed
    return {}


def load_fixture(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def safe_json(raw: str) -> Any:
    return parse_json(raw, {})


def collect_k8s_metadata(uid: int) -> dict[str, Any]:
    crd_name = f"her-{uid}"
    deploy_name = f"carher-{uid}"
    pvc_name = f"carher-{uid}-data"
    cm_name = f"carher-{uid}-user-config"

    her = safe_json(remote_kubectl(f"get herinstance {crd_name} -o json"))
    deploy = safe_json(remote_kubectl(f"get deploy {deploy_name} -o json"))
    pvc = safe_json(remote_kubectl(f"get pvc {pvc_name} -o json"))
    configmap = safe_json(remote_kubectl(f"get cm {cm_name} -o json"))
    pods = safe_json(
        remote_kubectl(
            f"get pods -l app=carher-user,user-id={uid} -o json"
        )
    )
    return {
        "herinstance": redact(her),
        "deployment": redact(summarize_deployment(deploy)),
        "pvc": redact(summarize_pvc(pvc)),
        "configmap": redact(summarize_configmap(configmap)),
        "pod": redact(summarize_pod_list(pods)),
    }


def summarize_deployment(deploy: dict[str, Any]) -> dict[str, Any]:
    template = deploy.get("spec", {}).get("template", {})
    container = next(
        (c for c in template.get("spec", {}).get("containers", []) if c.get("name") == "carher"),
        {},
    )
    return {
        "name": deploy.get("metadata", {}).get("name"),
        "annotations": deploy.get("metadata", {}).get("annotations", {}),
        "replicas": deploy.get("spec", {}).get("replicas"),
        "availableReplicas": deploy.get("status", {}).get("availableReplicas"),
        "image": container.get("image"),
        "resources": container.get("resources", {}),
        "env": {
            item.get("name"): item.get("value", "<secretRef>")
            for item in container.get("env", [])
            if item.get("name")
        },
        "volumes": template.get("spec", {}).get("volumes", []),
    }


def summarize_pvc(pvc: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": pvc.get("metadata", {}).get("name"),
        "phase": pvc.get("status", {}).get("phase"),
        "storageClassName": pvc.get("spec", {}).get("storageClassName"),
        "capacity": pvc.get("status", {}).get("capacity", {}),
        "accessModes": pvc.get("status", {}).get("accessModes", []),
        "createdAt": pvc.get("metadata", {}).get("creationTimestamp"),
    }


def summarize_configmap(configmap: dict[str, Any]) -> dict[str, Any]:
    raw = configmap.get("data", {}).get("openclaw.json", "{}")
    parsed = parse_json(raw, {})
    return {
        "name": configmap.get("metadata", {}).get("name"),
        "openclaw": redact(parsed),
    }


def summarize_pod_list(pods: dict[str, Any]) -> dict[str, Any]:
    items = pods.get("items", [])
    summarized = []
    for item in items:
        summarized.append(
            {
                "name": item.get("metadata", {}).get("name"),
                "phase": item.get("status", {}).get("phase"),
                "podIP": item.get("status", {}).get("podIP"),
                "node": item.get("spec", {}).get("nodeName"),
                "conditions": [
                    {
                        "type": c.get("type"),
                        "status": c.get("status"),
                        "reason": c.get("reason"),
                    }
                    for c in item.get("status", {}).get("conditions", [])
                ],
            }
        )
    return {"items": summarized}


def collect_pod_evidence(uid: int) -> dict[str, Any]:
    script = f"""
set -eu
POD=$(kubectl -n carher get pod -l app=carher-user,user-id={uid} -o jsonpath='{{.items[0].metadata.name}}')
echo "###POD###"
printf '%s\\n' "$POD"
echo "###ENGINE###"
kubectl -n carher exec "$POD" -c carher -- sh -lc 'cat /data/.engine/active 2>/dev/null || true'
echo "###DU###"
kubectl -n carher exec "$POD" -c carher -- sh -lc 'du -sh /data/.openclaw /data/.openclaw/workspace /data/.openclaw/compaction-reports /data/.openclaw/feishu-groups /data/.openclaw/memory /data/.openclaw/sessions /opt/data/.hermes 2>/dev/null || true'
echo "###FILES###"
kubectl -n carher exec "$POD" -c carher -- sh -lc {shlex.quote(file_listing_script())}
echo "###SQLITE###"
kubectl -n carher exec "$POD" -c carher -- sh -lc {shlex.quote(sqlite_probe_script())}
echo "###SNIPPETS###"
kubectl -n carher exec "$POD" -c carher -- sh -lc {shlex.quote(snippet_script())}
"""
    raw = run_remote(script, timeout=180)
    return parse_pod_evidence(raw)


def collect_offline_pvc_evidence(uid: int, image: str | None = None) -> dict[str, Any]:
    """Collect bounded evidence from a paused Her PVC via a read-only scan pod."""
    scan_pod = f"her-distill-scan-{uid}-{dt.datetime.now().strftime('%H%M%S')}"
    image = image or "cltx-her-ck-registry-vpc.ap-southeast-1.cr.aliyuncs.com/her/carher:h75-runtime-2588e74-her75-20260623"
    pod_yaml = textwrap.dedent(
        f"""
        apiVersion: v1
        kind: Pod
        metadata:
          name: {scan_pod}
          namespace: carher
          labels:
            app: her-distill-scan
            user-id: "{uid}"
        spec:
          restartPolicy: Never
          containers:
            - name: scan
              image: {image}
              imagePullPolicy: IfNotPresent
              command: ["sh", "-lc", "sleep 3600"]
              volumeMounts:
                - name: user-data
                  mountPath: /data/.openclaw
                  readOnly: true
          volumes:
            - name: user-data
              persistentVolumeClaim:
                claimName: carher-{uid}-data
                readOnly: true
        """
    ).strip()
    script = f"""
set -eu
cat <<'YAML' | kubectl -n carher apply -f -
{pod_yaml}
YAML
cleanup() {{
  kubectl -n carher delete pod {shlex.quote(scan_pod)} --ignore-not-found --wait=false >/dev/null 2>&1 || true
}}
trap cleanup EXIT
for i in $(seq 1 60); do
  phase=$(kubectl -n carher get pod {shlex.quote(scan_pod)} -o jsonpath='{{.status.phase}}' 2>/dev/null || true)
  if [ "$phase" = "Running" ]; then break; fi
  if [ "$phase" = "Failed" ] || [ "$phase" = "Succeeded" ]; then
    kubectl -n carher describe pod {shlex.quote(scan_pod)} >&2 || true
    exit 1
  fi
  sleep 1
done
phase=$(kubectl -n carher get pod {shlex.quote(scan_pod)} -o jsonpath='{{.status.phase}}' 2>/dev/null || true)
if [ "$phase" != "Running" ]; then
  kubectl -n carher describe pod {shlex.quote(scan_pod)} >&2 || true
  exit 1
fi
echo "###POD###"
printf '%s\\n' {shlex.quote(scan_pod)}
echo "###ENGINE###"
printf '%s\\n' "offline-pvc"
echo "###DU###"
kubectl -n carher exec {shlex.quote(scan_pod)} -c scan -- sh -lc 'du -sh /data/.openclaw /data/.openclaw/workspace /data/.openclaw/compaction-reports /data/.openclaw/feishu-groups /data/.openclaw/memory /data/.openclaw/sessions /opt/data/.hermes 2>/dev/null || true'
echo "###FILES###"
kubectl -n carher exec {shlex.quote(scan_pod)} -c scan -- sh -lc {shlex.quote(file_listing_script())}
echo "###SQLITE###"
kubectl -n carher exec {shlex.quote(scan_pod)} -c scan -- sh -lc {shlex.quote(sqlite_probe_script())}
echo "###SNIPPETS###"
kubectl -n carher exec {shlex.quote(scan_pod)} -c scan -- sh -lc {shlex.quote(snippet_script())}
"""
    raw = run_remote(script, timeout=240)
    return parse_pod_evidence(raw)


def format_command_failure(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or exc.stdout or "").strip()
        detail = "\n".join(detail.splitlines()[-8:])
        return redact_text(f"exit={exc.returncode}; {detail}"[:1200])
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"timeout after {exc.timeout}s"
    return redact_text(str(exc)[:1200])


def file_listing_script() -> str:
    roots = " ".join(shlex.quote(root) for root in REMOTE_ROOTS)
    return (
        f"find {roots} -maxdepth 4 -type f "
        "-printf '%p\t%s\t%TY-%Tm-%TdT%TH:%TM:%TS\n' 2>/dev/null | "
        "sort | head -n 1800"
    )


def sqlite_probe_script() -> str:
    return textwrap.dedent(
        """
        if command -v sqlite3 >/dev/null 2>&1 && [ -f /data/.openclaw/memory/main.sqlite ]; then
          echo "sqlite_available=yes"
          sqlite3 /data/.openclaw/memory/main.sqlite ".tables" 2>/dev/null | head -n 20 || true
          sqlite3 /data/.openclaw/memory/main.sqlite "select name, type from sqlite_master where type in ('table','view') order by name limit 40;" 2>/dev/null || true
        else
          echo "sqlite_available=no"
        fi
        """
    ).strip()


def snippet_script() -> str:
    paths = [
        "/data/.openclaw/workspace/MEMORY.md",
        "/data/.openclaw/workspace/IDENTITY.md",
        "/data/.openclaw/workspace/USER.md",
        "/data/.openclaw/workspace/SOUL.md",
        "/data/.openclaw/workspace/AGENTS.md",
        "/data/.openclaw/skills-custom/feishu-bot-direct/SKILL.md",
        "/data/.openclaw/workflow/dify-config.json",
        "/data/.openclaw/wiki/main/index.md",
        "/data/.openclaw/wiki/main/WIKI.md",
    ]
    lines: list[str] = []
    for path in paths:
        q = shlex.quote(path)
        lines.append(f"if [ -f {q} ]; then echo '---FILE {path}'; sed -n '1,120p' {q}; fi")
    lines.append(
        "find /data/.openclaw/compaction-reports -maxdepth 1 -type f -name '*.md' "
        "-printf '%T@ %p\n' 2>/dev/null | sort -nr | head -n 5 | cut -d' ' -f2- | "
        "while read f; do echo \"---FILE $f\"; sed -n '1,80p' \"$f\"; done"
    )
    return "\n".join(lines)


def parse_pod_evidence(raw: str) -> dict[str, Any]:
    sections: dict[str, str] = {}
    current = ""
    for line in raw.splitlines():
        if line.startswith("###") and line.endswith("###"):
            current = line.strip("#")
            sections[current] = ""
        elif current:
            sections[current] += line + "\n"

    files = []
    for line in sections.get("FILES", "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            files.append(
                {
                    "path": parts[0],
                    "size": parse_int(parts[1]),
                    "modified": parts[2] if len(parts) > 2 else "",
                }
            )

    snippets = parse_snippets(sections.get("SNIPPETS", ""))
    return {
        "podName": sections.get("POD", "").strip(),
        "activeEngine": sections.get("ENGINE", "").strip(),
        "diskUsage": sections.get("DU", "").strip().splitlines(),
        "sqliteProbe": sections.get("SQLITE", "").strip().splitlines(),
        "files": files,
        "snippets": redact(snippets),
    }


def parse_int(raw: str) -> int:
    try:
        return int(float(raw))
    except ValueError:
        return 0


def parse_snippets(raw: str) -> dict[str, str]:
    snippets: dict[str, list[str]] = {}
    current = ""
    for line in raw.splitlines():
        if line.startswith("---FILE "):
            current = line.removeprefix("---FILE ").strip()
            snippets[current] = []
        elif current:
            snippets[current].append(line)
    return {path: "\n".join(lines).strip() for path, lines in snippets.items()}


def classify_assets(files: list[dict[str, Any]]) -> dict[str, list[str]]:
    inventory = {
        "knowledge_candidates": [],
        "skill_candidates": [],
        "workflow_candidates": [],
        "group_archive_metadata": [],
        "metadata_only": [],
        "excluded_sensitive": [],
    }
    for item in files:
        path = item.get("path", "")
        lower = path.lower()
        if "/feishu-groups/" in lower and lower.endswith("messages.jsonl"):
            inventory["group_archive_metadata"].append(path)
            continue
        if any(marker in lower for marker in SENSITIVE_PATH_MARKERS):
            if "/memory/main.sqlite" in lower:
                inventory["metadata_only"].append(path)
            elif "/feishu-groups/" in lower and lower.endswith("messages.jsonl"):
                inventory["group_archive_metadata"].append(path)
            else:
                inventory["excluded_sensitive"].append(path)
            continue
        if "/skills-custom/" in lower or "/workspace/skills/" in lower:
            inventory["skill_candidates"].append(path)
        elif "/workflow/" in lower or "/cron/" in lower:
            inventory["workflow_candidates"].append(path)
        elif lower.endswith(KNOWLEDGE_EXTENSIONS):
            inventory["knowledge_candidates"].append(path)
        elif "/memory/" in lower:
            inventory["metadata_only"].append(path)
    return {key: sorted(set(paths)) for key, paths in inventory.items()}


def build_evidence(uid: int, *, fixture: Path | None, collect: bool) -> dict[str, Any]:
    if fixture:
        evidence = load_fixture(fixture)
    elif collect:
        evidence = collect_k8s_metadata(uid)
        evidence["collectionMode"] = "live-pod"
        try:
            pod_evidence = collect_pod_evidence(uid)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            evidence["collectionMode"] = "offline-pvc"
            evidence["collectionWarning"] = (
                "live pod collection failed; fell back to read-only PVC scan: "
                + format_command_failure(exc)
            )
            pod_evidence = collect_offline_pvc_evidence(uid)
        evidence.update(pod_evidence)
    else:
        raise SystemExit("Either --fixture or --collect is required")
    evidence["uid"] = uid
    evidence.setdefault("generated_at", now_iso())
    files = evidence.get("files", [])
    evidence["assets"] = classify_assets(files)
    evidence = redact(evidence)
    return evidence


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_outputs(evidence: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    handoff_dir = output_dir / "handoff-pack"
    handoff_dir.mkdir(parents=True, exist_ok=True)

    redacted = public_mask(redact(evidence))
    report = render_report(redacted)
    inventory = {
        "uid": redacted.get("uid"),
        "generated_at": redacted.get("generated_at"),
        "collectionMode": redacted.get("collectionMode", ""),
        "collectionWarning": redacted.get("collectionWarning", ""),
        "assets": public_assets(redacted.get("assets", {})),
        "diskUsage": redacted.get("diskUsage", []),
        "sqliteProbe": redacted.get("sqliteProbe", []),
        "files": summarize_files(redacted.get("files", [])),
    }
    files = {
        "report": output_dir / "report.md",
        "inventory": output_dir / "asset-inventory.json",
        "raw_redacted": output_dir / "evidence-redacted.json",
        "handoff_readme": handoff_dir / "README.md",
        "knowledge_index": handoff_dir / "knowledge-index.md",
        "security_review": handoff_dir / "security-review.md",
        "skill_candidates": handoff_dir / "skill-candidates.md",
        "retrospective": output_dir / "retrospective.md",
        "archive": output_dir / "handoff-pack.zip",
    }
    files["report"].write_text(report)
    write_json(files["inventory"], inventory)
    write_json(files["raw_redacted"], redacted)
    files["handoff_readme"].write_text(render_handoff_readme(redacted))
    files["knowledge_index"].write_text(render_knowledge_index(redacted))
    files["security_review"].write_text(render_security_review(redacted))
    files["skill_candidates"].write_text(render_skill_candidates(redacted))
    files["retrospective"].write_text(render_retrospective(redacted))
    write_archive(files["archive"], handoff_dir, files["inventory"], files["report"])
    return files


def summarize_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in files:
        path = item.get("path", "")
        public_path = public_path_for_inventory(path)
        result.append(
            {
                "path": public_path,
                "size": item.get("size", 0),
                "modified": item.get("modified", ""),
                "handling": asset_handling(path),
            }
        )
    return result


def public_assets(assets: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        key: [public_path_for_inventory(path) for path in paths]
        for key, paths in assets.items()
    }


def asset_handling(path: str) -> str:
    lower = path.lower()
    if SECRET_KEY_RE.search(path) or any(marker in lower for marker in ("appsecret", "master.key", "cookie", "token")):
        return "excluded_sensitive"
    if any(marker in lower for marker in SENSITIVE_PATH_MARKERS):
        if "/memory/main.sqlite" in lower or "/feishu-groups/" in lower:
            return "metadata_only"
        return "excluded_sensitive"
    if "/skills-custom/" in lower:
        return "review_for_shared_skill"
    if lower.endswith(KNOWLEDGE_EXTENSIONS):
        return "review_for_knowledge_pack"
    return "metadata_only"


def public_path_for_inventory(path: str) -> str:
    if SECRET_KEY_RE.search(path) or any(marker.strip("/") in path.lower() for marker in SENSITIVE_PATH_MARKERS):
        return "[SENSITIVE_PATH]"
    return mask_public_ids(path)


def spec(evidence: dict[str, Any]) -> dict[str, Any]:
    return evidence.get("herinstance", {}).get("spec", {})


def status(evidence: dict[str, Any]) -> dict[str, Any]:
    return evidence.get("herinstance", {}).get("status", {})


def render_report(evidence: dict[str, Any]) -> str:
    uid = evidence.get("uid", "")
    spec_data = spec(evidence)
    status_data = status(evidence)
    assets = evidence.get("assets", {})
    snippets = evidence.get("snippets", {})
    disk_usage = evidence.get("diskUsage", [])
    sqlite_probe = evidence.get("sqliteProbe", [])
    collection_mode = evidence.get("collectionMode", "")
    collection_warning = evidence.get("collectionWarning", "")

    return "\n".join(
        [
            f"# carher-{uid} 离职蒸馏报告",
            "",
            f"- 生成时间: {evidence.get('generated_at', '')}",
            f"- Her 名称: {spec_data.get('name', '')}",
            f"- 运行状态: {status_data.get('phase', '')} / 飞书 WS: {status_data.get('feishuWS', '')}",
            f"- 模型链路: provider={spec_data.get('provider', '')}, model={spec_data.get('model', '')}",
            f"- 部署组: {spec_data.get('deployGroup', '')}",
            f"- Owner: {mask_public_ids(str(spec_data.get('owner', '')))}",
            f"- 当前引擎: {evidence.get('activeEngine', '')}",
            f"- 采集模式: {collection_mode or 'unknown'}",
            "",
            "## 结论摘要",
            "",
            render_executive_summary(uid, status_data, collection_mode, collection_warning),
            "",
            "## 安全评价",
            "",
            "- 配置中存在 appSecret、LiteLLM key、运行时 token 等字段，报告与附件已统一替换为 `[REDACTED]`。",
            "- `/data/.openclaw/sessions`、`.lark-cli`、设备配对、delivery queue、原始 memory SQLite 不纳入交接内容。",
            "- 飞书群消息归档只作为群活跃度和资产规模证据，不复制 messages.jsonl 原文。",
            "- 建议后续接替 Her 使用新 owner、新 bot Secret、新 OAuth/session；不要 clone 原 PVC 给新人。",
            "",
            "## 资产盘点",
            "",
            f"- 岗位知识候选: {len(assets.get('knowledge_candidates', []))} 个",
            f"- Skill 候选: {len(assets.get('skill_candidates', []))} 个",
            f"- Workflow/Cron 候选: {len(assets.get('workflow_candidates', []))} 个",
            f"- 群归档元数据: {len(assets.get('group_archive_metadata', []))} 个",
            f"- 仅保留元数据: {len(assets.get('metadata_only', []))} 个",
            f"- 敏感排除项: {len(assets.get('excluded_sensitive', []))} 个",
            "",
            "## 知识主题初判",
            "",
            render_topic_summary(assets, snippets),
            "",
            "## 交接建议",
            "",
            "1. 建立新的岗位 Her，使用新 appId/appSecret、owner open_id 和空 sessions。",
            "2. 从 `handoff-pack/knowledge-index.md` 人工选择可沉淀文档，优先导入 Markdown 和已脱敏业务材料。",
            "3. 复核 `handoff-pack/skill-candidates.md` 中的 skill 候选；可复用能力升级为部门共享 skill 前，先删除个人身份、聊天 ID 和临时链接。",
            "4. 对合同、客户、海外市场、供应商协作材料做二次人工审核，再决定是否进入部门知识库。",
            "5. 原 PVC 建议只读保留 30-90 天，保留期内禁止直接复用 sessions/token。",
            "",
            "## 证据摘录",
            "",
            render_snippets(snippets),
            "",
            "## 存储和数据库元数据",
            "",
            fenced("\n".join(disk_usage + [""] + sqlite_probe)),
            "",
            "## 附件说明",
            "",
            "- `asset-inventory.json`: 脱敏后的资产清单和处理策略。",
            "- `handoff-pack.zip`: 可交接附件包，包含知识索引、安全复核和 skill 候选。",
            "- `evidence-redacted.json`: 完整脱敏证据，供后续脚本调试。",
            "",
        ]
    )


def render_topic_summary(assets: dict[str, list[str]], snippets: dict[str, str]) -> str:
    names = "\n".join(assets.get("knowledge_candidates", [])[:80])
    snippet_text = "\n".join(snippets.values())[:8000]
    combined = names + "\n" + snippet_text
    rules = [
        ("海外市场/北美进入策略", ("海外", "北美", "美国", "市场")),
        ("车载芯片/高通/SA8797", ("高通", "Qualcomm", "SA8797", "芯片")),
        ("客户项目/EDAG/BMW/北汽/延锋", ("EDAG", "BMW", "北汽", "延锋", "客户")),
        ("合同/顾问协议/合规", ("合同", "Agreement", "合规", "顾问", "法定代表人")),
        ("飞书群知识沉淀", ("feishu-groups", "群", "weekly")),
        ("Dify/H75/OpenClaw 运行能力", ("Dify", "H75", "OpenClaw", "workflow")),
    ]
    hits = [name for name, words in rules if any(word in combined for word in words)]
    if not hits:
        return "- 暂未从文件名和摘录中稳定识别主题，需要人工复核。"
    return "\n".join(f"- {item}" for item in hits)


def render_executive_summary(
    uid: int | str,
    status_data: dict[str, Any],
    collection_mode: str,
    collection_warning: str,
) -> str:
    phase = status_data.get("phase", "")
    if collection_mode == "offline-pvc":
        lines = [
            f"carher-{uid} 当前通过只读 PVC 离线模式完成快速蒸馏。该 Her 当前状态为 {phase or 'Unknown'}，未依赖原实例 Pod 运行；知识资产集中在 workspace、compaction reports、feishu doc backups、wiki 和 skills-custom；memory SQLite、sessions、lark-cli 状态、群消息原文只做元数据级审计，不进入交接包。",
        ]
        if collection_warning:
            lines.append(f"活 Pod 采集不可用，降级原因: {collection_warning}")
        return "\n\n".join(lines)
    health = "运行正常" if phase == "Running" else f"当前状态为 {phase or 'Unknown'}"
    return (
        f"carher-{uid} 当前可作为离职蒸馏试点。该 Her {health}，知识资产集中在 workspace、"
        "compaction reports、feishu doc backups、wiki 和 skills-custom；memory SQLite、sessions、"
        "lark-cli 状态、群消息原文只做元数据级审计，不进入交接包。"
    )


def render_snippets(snippets: dict[str, str]) -> str:
    if not snippets:
        return "_未采集到文本摘录。_"
    chunks = []
    for path, content in list(snippets.items())[:12]:
        compact = content.strip()
        if len(compact) > 1600:
            compact = compact[:1600] + "\n...[truncated]"
        chunks.append(f"### {mask_public_ids(path)}\n\n{fenced(mask_public_ids(compact))}")
    return "\n\n".join(chunks)


def fenced(content: str) -> str:
    return "```text\n" + content.strip() + "\n```"


def mask_public_ids(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        value = match.group(0)
        prefix, _, rest = value.partition("_")
        return f"{prefix}_{rest[:4]}...{rest[-4:]}"

    text = OPEN_ID_RE.sub(repl, text)
    return APP_ID_RE.sub(lambda match: "cli_" + match.group(0)[4:8] + "..." + match.group(0)[-4:], text)


def public_mask(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: public_mask(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_mask(item) for item in value]
    if isinstance(value, str):
        return mask_public_ids(value)
    return value


def render_handoff_readme(evidence: dict[str, Any]) -> str:
    uid = evidence.get("uid", "")
    return f"""# carher-{uid} Handoff Pack

This package is sanitized. It does not contain raw sessions, raw chat logs,
K8s Secrets, OAuth state, cookies, or the raw memory SQLite database.

Recommended import order:

1. Review `security-review.md`.
2. Review `knowledge-index.md` and select approved materials.
3. Review `skill-candidates.md` and promote reusable skills only after removing personal identifiers.
4. Import approved knowledge into a new Her with new owner, app credentials, and empty sessions.
"""


def render_knowledge_index(evidence: dict[str, Any]) -> str:
    assets = public_assets(evidence.get("assets", {}))
    candidates = assets.get("knowledge_candidates", [])
    lines = [
        "# Knowledge Index",
        "",
        "These files are candidates for human review. They are not automatically imported.",
        "",
    ]
    for path in candidates[:300]:
        lines.append(f"- `{path}`")
    if len(candidates) > 300:
        lines.append(f"- ... {len(candidates) - 300} more candidates omitted from Markdown; see asset-inventory.json")
    return "\n".join(lines) + "\n"


def render_security_review(evidence: dict[str, Any]) -> str:
    assets = public_assets(evidence.get("assets", {}))
    lines = [
        "# Security Review",
        "",
        "## Excluded Sensitive Paths",
        "",
    ]
    for path in assets.get("excluded_sensitive", [])[:300]:
        lines.append(f"- `{path}`")
    lines.extend(
        [
            "",
            "## Metadata Only",
            "",
        ]
    )
    for path in assets.get("metadata_only", [])[:300]:
        lines.append(f"- `{path}`")
    lines.extend(
        [
            "",
            "## Mandatory Controls",
            "",
            "- Do not clone the original PVC into a successor Her.",
            "- Rotate or replace app credentials before handoff.",
            "- Keep raw sessions and memory databases out of shared docs.",
            "- Require human approval before importing customer or contract materials.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_skill_candidates(evidence: dict[str, Any]) -> str:
    assets = public_assets(evidence.get("assets", {}))
    lines = [
        "# Skill Candidates",
        "",
        "Review these paths for reusable behavior. Promote only after removing personal context and secrets.",
        "",
    ]
    for path in assets.get("skill_candidates", []):
        lines.append(f"- `{path}`")
    lines.extend(["", "## Workflow Candidates", ""])
    for path in assets.get("workflow_candidates", []):
        lines.append(f"- `{path}`")
    return "\n".join(lines) + "\n"


def render_retrospective(evidence: dict[str, Any]) -> str:
    assets = evidence.get("assets", {})
    return f"""# carher-{evidence.get('uid')} Distillation Retrospective

## Worked

- Read-only collection through Kubernetes succeeded.
- Secret-like keys and values were redacted before report generation.
- Raw session, OAuth, memory DB, and chat archive content stayed out of the handoff pack.

## Gaps

- This first pass uses file names, snippets, and metadata rather than full semantic extraction.
- Human review is still required for customer, contract, and personnel-sensitive documents.
- Feishu publication depends on local `lark-cli` authentication and permissions.
- Text snippets can hide key material inside JSON/YAML strings; scan for provider-specific key prefixes before publishing.
- Feishu create can hit retryable frequency limits; publication should use backoff and then verify with `docs +fetch`.

## Batch Changes

- Add batch input for multiple Her IDs.
- Add optional owner/offboarding ticket metadata.
- Add a manual approval checklist before importing into successor Her instances.
- Add provider-specific secret scanners for Dify (`app-*`, `lct_*`), Feishu app IDs, and any future workflow engines.
- Prefer creating a fresh final Feishu document after a redaction bug; overwriting an old doc may not eliminate all revision-history risk.

## Counts

- Knowledge candidates: {len(assets.get('knowledge_candidates', []))}
- Skill candidates: {len(assets.get('skill_candidates', []))}
- Sensitive exclusions: {len(assets.get('excluded_sensitive', []))}
"""


def write_archive(archive_path: Path, handoff_dir: Path, inventory_path: Path, report_path: Path) -> None:
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(handoff_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(handoff_dir.parent))
        zf.write(inventory_path, inventory_path.name)
        zf.write(report_path, report_path.name)


def scan_output_for_secrets(output_dir: Path) -> dict[str, Any]:
    patterns = {
        "openai_key": re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
        "dify_app_key": re.compile(r"app-[A-Za-z0-9_\-]{8,}"),
        "lifecycle_token": re.compile(r"lct_[A-Za-z0-9_\-]{8,}"),
        "bearer": re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
        "raw_open_id": OPEN_ID_RE,
        "raw_app_id": APP_ID_RE,
    }
    hits: dict[str, list[str]] = {name: [] for name in patterns}
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or path.suffix == ".zip":
            continue
        text = path.read_text(errors="ignore")
        for name, pattern in patterns.items():
            if pattern.search(text):
                hits[name].append(str(path))
    return {
        "ok": all(not paths for paths in hits.values()),
        "hits": {name: paths for name, paths in hits.items() if paths},
    }


def publish_to_feishu(outputs: dict[str, Path], *, title: str, as_identity: str = "user") -> dict[str, Any]:
    create = run_command_json(
        [
            "lark-cli",
            "docs",
            "+create",
            "--as",
            as_identity,
            "--doc-format",
            "markdown",
            "--title",
            title,
            "--content",
            f"@{outputs['report']}",
            "--json",
        ],
        timeout=90,
    )
    document = create.get("data", {}).get("document", {})
    doc_id = document.get("document_id")
    if not doc_id:
        raise RuntimeError(f"Feishu create returned no document_id: {create}")

    media = run_command_json(
        [
            "lark-cli",
            "docs",
            "+media-insert",
            "--as",
            as_identity,
            "--doc",
            doc_id,
            "--file",
            str(outputs["archive"]),
            "--type",
            "file",
            "--file-view",
            "card",
            "--json",
        ],
        timeout=90,
    )
    return {"create": create, "media": media}


def write_feishu_publication(output_dir: Path, uid: int, publish: dict[str, Any]) -> Path:
    document = publish.get("create", {}).get("data", {}).get("document", {})
    media = publish.get("media", {}).get("data", {})
    path = output_dir / "feishu-publication.md"
    path.write_text(
        "\n".join(
            [
                f"# carher-{uid} Feishu Publication",
                "",
                f"- Published at: {now_iso()}",
                f"- Document: {document.get('url', '')}",
                f"- Document ID: {document.get('document_id', '')}",
                "- Attachment: handoff-pack.zip",
                f"- Attachment file token: {media.get('file_token', '')}",
                "- Verification: created document and inserted sanitized handoff archive.",
                "",
            ]
        )
    )
    return path


def default_output_dir(uid: int) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("docs") / "her-distillations" / f"carher-{uid}-{stamp}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--publish-feishu", action="store_true", help="Create a Feishu document and attach handoff-pack.zip.")
    parser.add_argument("--feishu-title", help="Feishu document title. Defaults to carher-<uid> 离职蒸馏报告 <date>.")
    parser.add_argument("--feishu-as", default="user", choices=["user", "bot"], help="lark-cli identity for publishing.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_dir = args.output_dir or default_output_dir(args.uid)
    evidence = build_evidence(args.uid, fixture=args.fixture, collect=args.collect)
    outputs = write_outputs(evidence, output_dir)
    result: dict[str, Any] = {key: str(path) for key, path in outputs.items()}
    secret_scan = scan_output_for_secrets(output_dir)
    result["secret_scan"] = secret_scan
    if args.publish_feishu:
        if not secret_scan["ok"]:
            raise SystemExit(f"Refusing to publish; secret scan found hits: {secret_scan['hits']}")
        title = args.feishu_title or f"carher-{args.uid} 离职蒸馏报告 {dt.datetime.now().strftime('%Y%m%d')}"
        publish = publish_to_feishu(outputs, title=title, as_identity=args.feishu_as)
        publication_path = write_feishu_publication(output_dir, args.uid, publish)
        result["feishu"] = {
            "document": publish.get("create", {}).get("data", {}).get("document", {}),
            "media": publish.get("media", {}).get("data", {}),
            "publication": str(publication_path),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
