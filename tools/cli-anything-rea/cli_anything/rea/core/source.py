"""Read REA's source at the revision we built, not whatever the working tree drifted to.

All reads go through ``git -C ~/codes/rea`` with an explicit ref (default ``HEAD``), so a
reference doc that says ``foo.ts:120`` can be re-checked at the commit it was written against
(``references/main-<sha7>/``). Nothing is copied into this repo.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Final

from cli_anything.rea.core import env as env_core

# TS/JS declarations worth jumping to. Methods inside classes are matched by the
# `name(`-at-line-start form, which also catches calls; `find` reports the kind so callers can filter.
DECL: Final = (r"^\s*(export\s+)?(default\s+)?(abstract\s+)?(async\s+)?"
               r"(function\*?|class|interface|type|enum|const|let)\s+{name}\b")
METHOD: Final = r"^\s+(public\s+|private\s+|protected\s+|static\s+|readonly\s+|async\s+|#)*{name}\s*[(<]"
LAYERS: Final = (
    ("entry", "scripts/rea.mjs, src/main*, src/cli*", "分发 mcp/http/doctor/CLI", "01-entry-mcp-cli.md"),
    ("server", "src/server/", "MCP 请求翻译、工具注册、结果投递、HTTP", "01-entry-mcp-cli.md"),
    ("contracts", "src/contracts/", "210 个工具的 schema 与副作用表", "01-entry-mcp-cli.md"),
    ("composition", "src/composition/", "provider/session 工厂、四个深引擎候选", "03-application.md"),
    ("application", "src/application/", "CLI/MCP 共用流程、选引擎、会话、缓存、setup", "03-application.md"),
    ("domain", "src/domain/", "Evidence/Unknown/Result/错误代数、纯语义", "02-domain.md"),
    ("providers", "src/{ghidra,hopper,ida,kuna,rizin,gdb,...}/", "深引擎与离线工具适配器", "04-native-providers.md"),
    ("web", "src/{browser,javascript,inspector,electron,artifacts}/", "JS/Electron/浏览器/产物读取", "05-web-js-artifacts.md"),
    ("process", "src/process/, src/windows/", "子进程所有权、截止时间、清理", "06-process-setup-security.md"),
    ("bridge", "bridge/", "跑在 Ghidra(Java)/Hopper(Python) 里的桥脚本", "04-native-providers.md"),
)


def _git(args: list[str], timeout: int = 60) -> str:
    r = subprocess.run(["git", "-C", str(env_core.rea_root()), *args], capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode not in (0, 1):  # grep: 1 = no match
        raise env_core.ReaError(f"git {' '.join(args[:2])} failed", r.stderr)
    return r.stdout


def resolve(ref: str = "HEAD") -> str:
    out = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"]).strip()
    if not out:
        raise env_core.ReaError(f"unknown ref {ref!r}")
    return out


def find(symbol: str, ref: str = "HEAD", path: str | None = None, include_tests: bool = False) -> list[dict[str, object]]:
    """Declarations of SYMBOL (function/class/const/type/method) at REF."""
    if not re.fullmatch(r"[A-Za-z_$#][\w$]*", symbol):
        raise env_core.ReaError(f"not an identifier: {symbol!r}")
    name = re.escape(symbol)
    hits: list[dict[str, object]] = []
    for kind, pat in (("decl", DECL), ("method", METHOD)):
        # -P, not -E: git grep's ERE has no \s / \b (silently matches nothing).
        args = ["grep", "-n", "-P", pat.format(name=name), resolve(ref), "--"]
        args += [path] if path else ["src", "scripts", "bridge"]
        for line in _git(args).splitlines():
            _, file, lineno, text = line.split(":", 3)
            if not include_tests and (".test." in file or "/tests/" in file or "/fixtures/" in file):
                continue
            if kind == "method" and re.search(r"\b(return|await|if|while|new)\b", text):
                continue
            hits.append({"path": file, "line": int(lineno), "kind": kind, "text": text.strip()[:160]})
    seen: set[tuple[str, int]] = set()
    uniq = []
    for h in hits:
        key = (str(h["path"]), int(h["line"]))  # type: ignore[arg-type]
        if key not in seen:
            seen.add(key)
            uniq.append(h)
    return uniq


def show(location: str, ref: str = "HEAD", context: int = 40) -> dict[str, object]:
    """PATH:LINE → that line and the next CONTEXT lines at REF (enough for most TS functions)."""
    m = re.fullmatch(r"(.+?):(\d+)", location)
    if not m:
        raise env_core.ReaError("expected PATH:LINE")
    file, line = m.group(1), int(m.group(2))
    text = _git(["show", f"{resolve(ref)}:{file}"])
    if not text:
        raise env_core.ReaError(f"{file} not found at {ref}")
    lines = text.splitlines()
    if not 1 <= line <= len(lines):
        raise env_core.ReaError(f"{file} has {len(lines)} lines")
    end = min(len(lines), line - 1 + context)
    return {"path": file, "ref": ref, "start": line, "end": end,
            "lines": [f"{n}: {lines[n - 1]}" for n in range(line, end + 1)]}


def grep(pattern: str, ref: str = "HEAD", paths: tuple[str, ...] = ("src",), fixed: bool = False,
         limit: int = 200) -> list[dict[str, object]]:
    args = ["grep", "-n", "-F" if fixed else "-P", pattern, resolve(ref), "--", *paths]
    out = []
    for line in _git(args).splitlines()[:limit]:
        _, file, lineno, text = line.split(":", 3)
        out.append({"path": file, "line": int(lineno), "text": text.strip()[:200]})
    return out


def env_vars(ref: str = "HEAD") -> list[dict[str, object]]:
    """Every REA_* name in non-test source with how many files mention it (config surface)."""
    counts: dict[str, set[str]] = {}
    for h in grep(r"REA_[A-Z0-9_]{3,}", ref, ("src",), limit=1_000_000):
        if ".test." in str(h["path"]):
            continue
        for name in re.findall(r"REA_[A-Z0-9_]{3,}", str(h["text"])):
            counts.setdefault(name, set()).add(str(h["path"]))
    return [{"name": n, "files": len(f)} for n, f in sorted(counts.items(), key=lambda kv: (-len(kv[1]), kv[0]))]


def layer_sizes(ref: str = "HEAD") -> list[dict[str, object]]:
    """Non-test .ts line counts per src/ top-level directory at REF."""
    files = _git(["ls-tree", "-r", "--name-only", resolve(ref), "src"]).splitlines()
    sizes: dict[str, int] = {}
    for f in files:
        if not f.endswith(".ts") or ".test." in f:
            continue
        top = f.split("/")[1] if f.count("/") >= 2 else "(src root)"
        sizes[top] = sizes.get(top, 0) + 1
    return [{"dir": k, "files": v} for k, v in sorted(sizes.items(), key=lambda kv: -kv[1])]


def reference_dir() -> Path | None:
    """The skill's deep-read docs for the built commit, if they exist."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / ".claude" / "skills" / "cli-anything-rea" / "references"
        if cand.is_dir():
            return cand
    return None
