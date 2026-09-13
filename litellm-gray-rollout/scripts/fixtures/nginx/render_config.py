#!/usr/bin/env python3
"""Render a self-contained nginx 1.18 fixture using fake credentials only."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path


FAKE_KEYS = {
    "protected-prod.map": ["sk-protected0001"],
    "force-prod.map": ["sk-incident0001"],
    "force-gray.map": ["sk-forcegray0001"],
}
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")


def _write(path: Path, content: str, mode: int = 0o600) -> None:
    path.write_text(content)
    path.chmod(mode)


def render(
    *,
    template: Path,
    output: Path,
    generation: Path,
    listen_port: int,
    prod_port: int,
    gray_port: int,
    bridge_port: int,
    debug_token: str,
    split_rules: str = "* litellm_product;",
) -> None:
    if not SAFE_TOKEN.fullmatch(debug_token):
        raise ValueError("debug token contains unsafe nginx syntax")
    for port in (listen_port, prod_port, gray_port, bridge_port):
        if not 1 <= port <= 65535:
            raise ValueError("port is outside the valid TCP range")
    split_lines = [line.strip() for line in split_rules.splitlines() if line.strip()]
    if not split_lines or any(
        not re.fullmatch(
            r"(?:\*|(?:100|[1-9]?\d)(?:\.\d{1,2})?%)\s+litellm_(?:gray|product);",
            line,
        )
        for line in split_lines
    ):
        raise ValueError("split rules contain unsafe nginx syntax")
    for path in (output, generation):
        if any(char in str(path) for char in ('\n', '\r', '"', "'", ';')):
            raise ValueError("path contains unsafe nginx syntax")
    generation.mkdir(parents=True, exist_ok=True, mode=0o700)
    generation.chmod(0o700)
    sid_lines: list[str] = []
    for filename, keys in FAKE_KEYS.items():
        lines = [f'"{key}" 1;' for key in keys]
        _write(generation / filename, "\n".join(lines) + "\n")
        sid_lines.extend(
            f'"{key}" {hashlib.sha256(key.encode()).hexdigest()[:12]};' for key in keys
        )
    _write(generation / "key-sid.map", "\n".join(sorted(set(sid_lines))) + "\n")
    _write(generation / "convergence-mode.map", "default 0;\n")
    _write(generation / "bridge-override.map", "default off;\n")
    _write(generation / "split.conf", "\n".join(split_lines) + "\n")

    replacements = {
        "@@GENERATION_DIR@@": str(generation),
        "@@LISTEN_PORT@@": str(listen_port),
        "@@PROD_PORT@@": str(prod_port),
        "@@GRAY_PORT@@": str(gray_port),
        "@@BRIDGE_PORT@@": str(bridge_port),
        "@@DEBUG_TOKEN@@": debug_token,
        "@@SPLIT_RULES@@": "\n".join(f"        {line}" for line in split_lines),
        "@@PID_FILE@@": str(output.parent / "nginx.pid"),
        "@@ERROR_LOG@@": str(output.parent / "error.log"),
        "@@ACCESS_LOG@@": str(output.parent / "access.log"),
    }
    rendered = template.read_text()
    for needle, value in replacements.items():
        rendered = rendered.replace(needle, value)
    if "@@" in rendered:
        raise ValueError("unresolved nginx fixture placeholder")
    output.parent.mkdir(parents=True, exist_ok=True)
    _write(output, rendered, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--listen-port", type=int, default=18080)
    parser.add_argument("--prod-port", type=int, default=18082)
    parser.add_argument("--gray-port", type=int, default=18083)
    parser.add_argument("--bridge-port", type=int, default=18084)
    parser.add_argument("--debug-token", default="fixture-debug-token")
    parser.add_argument("--split-rules", default="* litellm_product;")
    args = parser.parse_args()
    os.umask(0o077)
    render(
        template=args.template,
        output=args.output,
        generation=args.generation,
        listen_port=args.listen_port,
        prod_port=args.prod_port,
        gray_port=args.gray_port,
        bridge_port=args.bridge_port,
        debug_token=args.debug_token,
        split_rules=args.split_rules,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
