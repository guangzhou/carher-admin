"""Run REA for real and shrink its answers to what an analyst reads.

REA's raw Evidence is large (a VS Code ``node_modules.asar`` analysis is ~280 MB of JSON), so each
command saves the full Evidence under ``~/codes/rea-deps/runs/`` and returns a summary plus that path.
Summaries only copy fields; they never recompute or reinterpret REA's results.

Provider selection: REA's ``auto`` binds only when exactly one deep engine is usable and reports
``ambiguous`` otherwise (``AnalysisProviderRegistry.ts`` 650-670). With Ghidra *and* Kuna installed
here, every native command must name ``--provider``; we default to ghidra.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Final

from cli_anything.rea.core import env as env_core

DEEP_PROVIDERS: Final = ("ghidra", "kuna", "hopper", "ida")


def runs_dir() -> Path:
    d = env_core.deps_root() / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _call(args: list[str], save_as: str, timeout: int) -> tuple[dict[str, object], Path, float]:
    t0 = time.monotonic()
    r = env_core.run_rea([*args, "--format", "json"], timeout=timeout)
    wall = round(time.monotonic() - t0, 1)
    out = runs_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}-{save_as}.json"
    out.write_text(r.stdout, encoding="utf-8")
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        raise env_core.ReaError(f"rea {args[0]} exit {r.returncode}: no JSON on stdout", r.stderr[-2000:]) from exc
    if isinstance(data, dict) and "error" in data and "code" in data:
        raise env_core.ReaError(f"rea {args[0]}: {data['code']}: {data.get('message', data['error'])}",
                                json.dumps(data.get("details", {}), ensure_ascii=False)[:2000])
    return data, out, wall


def doctor() -> dict[str, object]:
    r = env_core.run_rea(["doctor", "--json"], timeout=600)
    data = json.loads(r.stdout)
    rows = []
    for c in data["checks"]:
        group = "agent" if c["name"].startswith(("registration:", "skill:")) else "engine"
        rows.append({"name": c["name"], "ok": c["ok"], "group": group, "class": c.get("classification"),
                     "detail": (c.get("detail") or c.get("remediation") or "")[:140]})
    engines = [r for r in rows if r["group"] == "engine"]
    return {"healthy": data["healthy"], "engines_ok": sum(1 for r in engines if r["ok"]), "engines": len(engines),
            "agents_registered": sum(1 for r in rows if r["group"] == "agent" and r["ok"]), "checks": rows}


def _evidence_head(ev: dict[str, object]) -> dict[str, object]:
    subject = ev.get("subject") or {}
    return {
        "evidence_id": ev.get("evidence_id"),
        "provider": (ev.get("provider") or {}).get("id"),  # type: ignore[union-attr]
        "provider_version": (ev.get("provider") or {}).get("version"),  # type: ignore[union-attr]
        "operation": ev.get("operation"),
        "confidence": ev.get("confidence"),
        "authority": ev.get("authority"),
        "target_sha256": ((subject.get("digest") or {}).get("sha256") if isinstance(subject, dict) else None),
        "limitations": len(ev.get("limitations") or []),  # type: ignore[arg-type]
    }


def decompile(path: str, procedure: str, provider: str = "ghidra", timeout: int = 900) -> dict[str, object]:
    if provider not in DEEP_PROVIDERS:
        raise env_core.ReaError(f"provider must be one of {DEEP_PROVIDERS}")
    target = str(Path(path).expanduser().resolve())
    ev, saved, wall = _call(["decompile", target, procedure, "--provider", provider], f"decompile-{provider}", timeout)
    return _evidence_head(ev) | {"wall_s": wall, "saved": str(saved), "pseudocode": ev.get("normalized_result")}


def analyze_js(path: str, record_integrity: bool = False, timeout: int = 3600) -> dict[str, object]:
    """Static JS/Electron reconstruction. `record_integrity` = REA's `--integrity-policy record-and-continue`.

    Signed apps (e.g. VS Code) often fail closed with `artifact_integrity_mismatch` because the
    packager re-signs unpacked .node files after writing the ASAR; REA itself suggests the flag.
    """
    target = str(Path(path).expanduser().resolve())
    args = ["analyze-javascript-application", target]
    if record_integrity:
        args += ["--integrity-policy", "record-and-continue"]
    ev, saved, wall = _call(args, "analyze-js", timeout)
    n = ev.get("normalized_result") or {}
    lim = n.get("limitations") or []  # type: ignore[union-attr]
    return _evidence_head(ev) | {
        "wall_s": wall, "saved": str(saved), "saved_bytes": saved.stat().st_size,
        "statistics": n.get("statistics"),  # type: ignore[union-attr]
        "summary": n.get("summary"),  # type: ignore[union-attr]
        "integrity_contradictions": len(n.get("integrity_contradictions") or []),  # type: ignore[union-attr]
        "result_limitations": len(lim),
        "result_limitations_sample": [str(x)[:160] for x in lim[:5]],
    }


def providers() -> dict[str, object]:
    r = env_core.run_rea(["providers", "--json"], timeout=300)
    data = json.loads(r.stdout)
    cands = data.get("analysis_provider_candidates") or []
    return {"binding": data.get("analysis_provider_binding"),
            "candidates": [{k: c.get(k) for k in ("id", "available", "status", "reason") if k in c}
                           for c in cands] if isinstance(cands, list) else cands}
