#!/usr/bin/env python3
"""Sync ``k8s/litellm-callbacks/*.py`` source files into the inline
``litellm-callbacks`` ConfigMap block of ``k8s/litellm-proxy.yaml``.

Why this script exists
======================
The deploy YAML carries each LiteLLM custom callback as an inline literal
block scalar (``<file>.py: |``) so that ``kubectl apply`` is a single
atomic operation. The same files also live as standalone ``.py`` source
files under ``k8s/litellm-callbacks/`` for syntax-aware editing, code
review, and CI lint. That arrangement creates a drift hazard: edits to
the source file silently fail to reach production unless the inline
block is also updated.

This script enforces the rule "the standalone .py files are the single
source of truth" by mechanically rewriting each inline block from its
matching source file.

Modes
-----
``check``  exits 1 if any inline block does not byte-for-byte match the
           source file, printing per-file diffs. Use in CI.
``write``  rewrites the YAML in place. Idempotent.

Usage
-----
    python3 scripts/sync-litellm-callbacks.py check
    python3 scripts/sync-litellm-callbacks.py write
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path
from typing import List, Tuple

# Paths are resolved relative to the repo root (the parent of scripts/).
REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_PATH = REPO_ROOT / "k8s" / "litellm-proxy.yaml"
SOURCE_DIR = REPO_ROOT / "k8s" / "litellm-callbacks"

# The four callback modules currently mounted into /app/<file>.py.
# Order is irrelevant; the script finds each block by name.
CALLBACK_FILES: Tuple[str, ...] = (
    "opus_47_fix.py",
    "embedding_sanitize.py",
    "streaming_bridge.py",
    "force_stream.py",
)

# Indentation of the literal block's content. This MUST match the YAML
# layout: the ConfigMap's data: dict has 2-space indent for the keys,
# and YAML literal block scalars use 4-space content indent under that.
CONTENT_INDENT = "    "

# Regex anchors that mark the start of an inline block under the
# ``litellm-callbacks`` ConfigMap. Two-space indent + filename + ``: |``.
KEY_HEADER_RE = re.compile(r"^  (?P<name>[A-Za-z_][A-Za-z0-9_]*\.py): \|\s*$")

# A document terminator marks the end of the ConfigMap. Beyond it, any
# content belongs to the next k8s manifest and must NEVER be touched.
DOC_END_RE = re.compile(r"^---\s*$")


class SyncError(RuntimeError):
    """Raised when the YAML structure does not match expectations."""


def _read_lines(path: Path) -> List[str]:
    return path.read_text().splitlines(keepends=True)


def _indent_source(source: str) -> List[str]:
    """Return the source string as a list of YAML-indented lines.

    YAML literal block scalars require every body line to be indented
    at least to the block-content indent. Empty lines may stay empty
    (PyYAML treats both ``\n`` and ``    \n`` identically inside a
    ``|`` scalar) but we choose to leave empty lines empty to keep the
    diff minimal.
    """
    out: List[str] = []
    for line in source.splitlines(keepends=False):
        if line == "":
            out.append("\n")
        else:
            out.append(f"{CONTENT_INDENT}{line}\n")
    return out


def _split_documents(yaml_lines: List[str]) -> List[Tuple[int, int]]:
    """Split the full YAML into per-document ``(start, end)`` index spans.

    YAML separates documents with a sole ``---`` line. ``end`` is
    exclusive. The first document starts at index 0 even when there is
    no leading ``---``. Empty trailing documents are dropped.
    """
    spans: List[Tuple[int, int]] = []
    start = 0
    for idx, line in enumerate(yaml_lines):
        if DOC_END_RE.match(line):
            if idx > start:
                spans.append((start, idx))
            start = idx + 1
    if start < len(yaml_lines):
        spans.append((start, len(yaml_lines)))
    return spans


# Top-level (indent 0) ``kind:`` and the metadata.name (indent 2) lines
# uniquely identify a k8s manifest within a multi-doc YAML. The
# ``volumes.configMap.name`` reference inside a Deployment matches the
# string ``name: litellm-callbacks`` too, which is why we scope the
# locator to the top of each document via these regex anchors.
KIND_LINE_RE = re.compile(r"^kind:\s*(?P<kind>\S+)\s*$")
META_NAME_RE = re.compile(r"^  name:\s*(?P<name>\S+)\s*$")


def _is_callbacks_configmap(yaml_lines: List[str], start: int, end: int) -> bool:
    """Return True iff the given document slice is the ConfigMap whose
    metadata.name is exactly ``litellm-callbacks``.

    Matching by parsing only the top of the document avoids picking up
    the ``volumes.configMap.name: litellm-callbacks`` reference inside
    the Deployment (a real bug observed during script development on
    2026-04-27)."""
    seen_configmap = False
    for idx in range(start, end):
        line = yaml_lines[idx]
        m = KIND_LINE_RE.match(line)
        if m:
            seen_configmap = m.group("kind") == "ConfigMap"
            continue
        m = META_NAME_RE.match(line)
        if m and seen_configmap:
            return m.group("name") == "litellm-callbacks"
    return False


def _locate_blocks(yaml_lines: List[str]) -> List[Tuple[str, int, int]]:
    """Return ``(filename, header_line_idx, body_end_line_idx)`` for each
    callback inline block, where indices are 0-based and ``body_end`` is
    exclusive (slice-style).

    A block's body extends from ``header + 1`` until either:
      * the next ``  <name>.py: |`` header within the same ConfigMap
        document, or
      * the document's exclusive end index.
    """
    headers: List[Tuple[str, int]] = []
    doc_end = len(yaml_lines)
    matched_doc = False

    for start, end in _split_documents(yaml_lines):
        if not _is_callbacks_configmap(yaml_lines, start, end):
            continue
        if matched_doc:
            raise SyncError(
                "multiple ConfigMaps named ``litellm-callbacks`` found; "
                "the YAML structure is ambiguous"
            )
        matched_doc = True
        doc_end = end
        for idx in range(start, end):
            m = KEY_HEADER_RE.match(yaml_lines[idx])
            if m:
                headers.append((m.group("name"), idx))

    if not matched_doc:
        raise SyncError(
            "no ``ConfigMap`` document with metadata.name=litellm-callbacks "
            "found in the YAML"
        )
    if not headers:
        raise SyncError(
            "ConfigMap litellm-callbacks contains no inline ``<name>.py: |`` "
            "blocks; nothing to sync"
        )

    blocks: List[Tuple[str, int, int]] = []
    for i, (name, header_idx) in enumerate(headers):
        next_header = headers[i + 1][1] if i + 1 < len(headers) else doc_end
        blocks.append((name, header_idx, next_header))
    return blocks


def _trim_trailing_blank_lines(lines: List[str]) -> List[str]:
    """Drop trailing pure-blank lines so each block ends with one
    blank-line separator regardless of how the source was edited."""
    while lines and lines[-1].strip() == "":
        lines.pop()
    lines.append("\n")
    return lines


def _build_new_yaml(yaml_lines: List[str]) -> List[str]:
    """Return a new list-of-lines with every inline block replaced from
    its corresponding source file."""
    blocks = _locate_blocks(yaml_lines)
    blocks_by_name = {name: (start, end) for name, start, end in blocks}

    for name in CALLBACK_FILES:
        if name not in blocks_by_name:
            raise SyncError(
                f"YAML is missing an inline block for {name!r}; expected "
                f"a line ``  {name}: |`` inside the litellm-callbacks "
                "ConfigMap"
            )
        src_path = SOURCE_DIR / name
        if not src_path.is_file():
            raise SyncError(
                f"source file {src_path} not found; cannot sync"
            )

    # Walk from the bottom up so earlier rewrites don't shift later
    # block indices.
    new_lines = list(yaml_lines)
    for name, header_idx, end_idx in sorted(blocks, key=lambda t: -t[1]):
        if name not in CALLBACK_FILES:
            # Block exists in YAML but no matching source -- leave alone
            # rather than guess.
            continue
        src = (SOURCE_DIR / name).read_text()
        body = _indent_source(src)
        body = _trim_trailing_blank_lines(body)
        new_lines[header_idx + 1 : end_idx] = body

    return new_lines


def _dump_diff(name: str, expected: str, actual: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            actual.splitlines(),
            fromfile=f"{name} (source)",
            tofile=f"{name} (yaml inline)",
            lineterm="",
        )
    )


def cmd_check() -> int:
    yaml_lines = _read_lines(YAML_PATH)
    blocks = _locate_blocks(yaml_lines)
    drift = []
    for name, start, end in blocks:
        if name not in CALLBACK_FILES:
            continue
        src = (SOURCE_DIR / name).read_text()
        expected_body = "".join(_trim_trailing_blank_lines(_indent_source(src)))
        actual_body = "".join(yaml_lines[start + 1 : end])
        if expected_body != actual_body:
            drift.append((name, expected_body, actual_body))

    if not drift:
        print(f"OK: all {len(CALLBACK_FILES)} callback blocks match source files")
        return 0

    print(
        f"FAIL: {len(drift)} callback block(s) drift from source",
        file=sys.stderr,
    )
    for name, expected, actual in drift:
        print(_dump_diff(name, expected, actual), file=sys.stderr)
        print(file=sys.stderr)
    print(
        "Run `python3 scripts/sync-litellm-callbacks.py write` to "
        "regenerate the inline blocks from the source files.",
        file=sys.stderr,
    )
    return 1


def cmd_write() -> int:
    yaml_lines = _read_lines(YAML_PATH)
    new_lines = _build_new_yaml(yaml_lines)
    if new_lines == yaml_lines:
        print("OK: YAML already in sync, no rewrite needed")
        return 0
    YAML_PATH.write_text("".join(new_lines))
    print(
        f"WROTE: rebuilt {YAML_PATH.relative_to(REPO_ROOT)} from source files"
    )
    return 0


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=("check", "write"))
    args = p.parse_args(argv)
    if args.mode == "check":
        return cmd_check()
    return cmd_write()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
