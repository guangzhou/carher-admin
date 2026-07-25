#!/usr/bin/env python3
"""Convenience wrapper for the repo-local 263 webmail skill script."""

from __future__ import annotations

import runpy
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / ".codex"
    / "skills"
    / "carher-263-webmail"
    / "scripts"
    / "fetch-263-mail.py"
)


if __name__ == "__main__":
    runpy.run_path(str(SCRIPT), run_name="__main__")
