"""REA's MCP tool catalog, two ways, and the diff between them.

* source: ``src/contracts/toolEffects.ts`` ``TOOL_EFFECTS``. Every tool has one entry; upstream
  tests assert it equals ``TOOL_CONTRACTS`` (``toolEffects.test.ts``) and that ``tools/list``
  length equals ``TOOL_CONTRACTS.length``. The MCP annotations are *derived* from these flags
  (``annotationsFromEffects``), never from tool names.
* runtime: ``rea mcp`` over stdio, ``tools/list`` paged to the end (``probes/mcp_tools.mjs``).

``destructive = mayDiscardData or executesForeignCode or mutatesDevice`` is copied from
``annotationsFromEffects``; ``test_catalog_rule_matches_upstream`` re-reads that function so a
change upstream breaks our test instead of silently drifting.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Final

from cli_anything.rea.core import env as env_core

FLAGS: Final = ("mutatesTarget", "mutatesSession", "writesFilesystem", "launchesProcess", "accessesNetwork",
                "changesUiState", "mayDiscardData", "executesForeignCode", "mutatesDevice")
# Shorthand entries in TOOL_EFFECTS (`foo: evidence,`) resolve to these preset constants in the same file.
PRESET_RE: Final = re.compile(r"^const (\w+)\s*=\s*effects\(\{(.*?)\}\);", re.S | re.M)
ENTRY_RE: Final = re.compile(r"^  ([a-z_0-9]+):\s*(.*)$")
FLAG_RE: Final = re.compile(r"(\w+):\s*(true|false)")
DESTRUCTIVE_RULE: Final = "value.mayDiscardData || value.executesForeignCode || value.mutatesDevice"


def effects_file(root: Path | None = None) -> Path:
    return (root or env_core.rea_root()) / "src" / "contracts" / "toolEffects.ts"


def _flags(body: str) -> dict[str, bool]:
    return {k: v == "true" for k, v in FLAG_RE.findall(body)}


def parse_effects(text: str) -> dict[str, dict[str, bool]]:
    """TOOL_EFFECTS → {tool: {flag: bool}} with presets resolved and defaults (all False) filled."""
    presets = {name: _flags(body) for name, body in PRESET_RE.findall(text)}
    m = re.search(r"^export const TOOL_EFFECTS[^=]*=\s*\{\n(.*?)^\};", text, re.S | re.M)
    if not m:
        raise env_core.ReaError("TOOL_EFFECTS block not found in toolEffects.ts")
    lines = m.group(1).splitlines()
    out: dict[str, dict[str, bool]] = {}
    i = 0
    while i < len(lines):
        em = ENTRY_RE.match(lines[i])
        if not em:
            i += 1
            continue
        name, rest = em.group(1), em.group(2)
        body = rest
        if rest.startswith("effects(") and not rest.rstrip().endswith((")", "),")):
            # multi-line effects({ ... }),
            while not lines[i].rstrip().endswith(("}),", "})")):
                i += 1
                body += lines[i]
        token = rest.rstrip(",").strip()
        if token in presets:
            flags = dict(presets[token])
        elif token.startswith("effects"):
            flags = _flags(body)
        else:
            raise env_core.ReaError(f"{name}: unknown TOOL_EFFECTS shorthand {token!r}")
        out[name] = {f: flags.get(f, False) for f in FLAGS} | {"idempotent": flags.get("idempotent", True)}
        i += 1
    return out


def annotate(flags: dict[str, bool]) -> dict[str, bool]:
    return {
        "readOnly": not any(flags[f] for f in FLAGS),
        "destructive": flags["mayDiscardData"] or flags["executesForeignCode"] or flags["mutatesDevice"],
        "openWorld": flags["launchesProcess"] or flags["accessesNetwork"],
    }


def source_catalog(root: Path | None = None) -> list[dict[str, object]]:
    effects = parse_effects(effects_file(root).read_text(encoding="utf-8"))
    return [{"name": n, **annotate(f), "flags": [k for k in FLAGS if f[k]]} for n, f in sorted(effects.items())]


def runtime_catalog(timeout: int = 180) -> dict[str, object]:
    env = env_core.load_env()
    probe = Path(__file__).resolve().parent.parent / "probes" / "mcp_tools.mjs"
    r = subprocess.run(["node", str(probe), str(env_core.rea_root())], env=env, capture_output=True,
                       text=True, timeout=timeout)
    try:
        data = json.loads(r.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise env_core.ReaError(f"mcp probe produced no JSON (exit {r.returncode})", r.stderr) from exc
    if not data.get("ok"):
        raise env_core.ReaError(str(data.get("error")), str(data.get("stderr", "")))
    return data


def diff(source: list[dict[str, object]], runtime: dict[str, object]) -> dict[str, object]:
    """Names and annotation agreement between the TS table and what `tools/list` advertises."""
    src = {str(t["name"]): t for t in source}
    rt = {str(t["name"]): t for t in runtime["tools"]}  # type: ignore[union-attr]
    mismatched = []
    for name in sorted(src.keys() & rt.keys()):
        a = rt[name]["annotations"]
        want = {"destructive": src[name]["destructive"], "readOnly": src[name]["readOnly"]}
        got = {"destructive": bool(a.get("destructiveHint")), "readOnly": bool(a.get("readOnlyHint"))}
        if want != got:
            mismatched.append({"name": name, "source": want, "runtime": got})
    return {
        "source": len(src),
        "runtime": len(rt),
        "only_in_source": sorted(src.keys() - rt.keys()),
        "only_in_runtime": sorted(rt.keys() - src.keys()),
        "annotation_mismatch": mismatched,
        "ok": not (src.keys() ^ rt.keys()) and not mismatched,
    }
