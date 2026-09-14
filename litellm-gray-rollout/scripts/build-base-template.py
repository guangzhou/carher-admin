#!/usr/bin/env python3
"""Build the frozen nginx base template from the live product site config.

`render-production-nginx.py` consumes a base template: the reviewed live config
with one `http` marker and one product-proxy marker per managed `/pro` location.
Producing that template by hand means hand-editing a live production config
during a change window, which is exactly what this repo's operating rules
forbid. This tool does it deterministically instead, and refuses on anything it
cannot account for.

What it will NOT do:

* guess. Every managed `/pro` location must contain exactly one `proxy_pass`
  statement, and that statement is the only thing replaced. `rewrite`,
  `proxy_set_header` and everything else in the block are carried through
  untouched, because they encode behaviour the renderer knows nothing about.
* silently drop a pre-existing upstream split. A `proxy_pass` whose target is a
  variable is not the product upstream -- it is a decision some earlier change
  made. Measured on 198 2026-09-13, `location = /pro/v1/responses` reads
  `proxy_pass http://$pro_responses_backend;`, which sends
  `Upgrade: websocket` to the codex incremental terminator. Such a location gets
  the ws-split marker so the pre-existing half survives; a SECOND one is a shape
  this tool has never seen and it stops rather than assume they compose.
* print the config. The output template is written mode 0600 and the tool emits
  only a structural digest: counts, location headers and checksums.

The caller still has to read the resulting template before freezing it. This
tool removes the transcription errors, not the review.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

TOOL = "build-base-template"
HERE = Path(__file__).resolve().parent


def fail(message: str) -> "NoReturn":  # type: ignore[valid-type]
    raise SystemExit(f"{TOOL}: {message}")


def load_renderer() -> Any:
    """Import the renderer as a module so we share ONE parser.

    A second, independent nginx parser here would be a ruler that disagrees with
    the one that actually gates the render -- this tool could go green on a
    template the renderer then rejects, or worse, agree on a shape the renderer
    reads differently.
    """
    path = HERE / "render-production-nginx.py"
    spec = importlib.util.spec_from_file_location("_gray_renderer", path)
    if spec is None or spec.loader is None:
        fail(f"cannot load renderer at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROXY_PASS_RE = re.compile(r"^(?P<indent>[ \t]*)proxy_pass[ \t]+(?P<target>[^;]+);[ \t]*$")


def location_spans(renderer: Any, text: str) -> list[tuple[str, str, int, int]]:
    """`renderer.location_blocks`, but carrying each body's offsets.

    Offsets are unavoidable: on 198 five of the seven managed locations have
    BYTE-IDENTICAL bodies (a lone `proxy_pass http://litellm_product;`), so
    replacing by content would rewrite the first match five times and leave four
    locations unmanaged. The renderer only needs the text, so it does not expose
    offsets; rather than trust two scanners to agree, this one is cross-checked
    against it below and stops the build if they ever disagree.
    """
    masked = renderer.syntax_mask(text)
    spans: list[tuple[str, str, int, int]] = []
    for match in re.finditer(r"\blocation\b(?P<header>[^;{}]*?)\{", masked, re.S):
        opening = match.end() - 1
        depth = 0
        closing = None
        for index in range(opening, len(masked)):
            if masked[index] == "{":
                depth += 1
            elif masked[index] == "}":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing is None:
            fail("live config contains an unterminated location block")
        header = text[match.start("header") : opening].strip()
        spans.append((header, text[opening + 1 : closing], opening + 1, closing))

    reference = renderer.location_blocks(text)
    if [(header, body) for header, body, _, _ in spans] != reference:
        fail(
            "internal error: the offset scanner disagrees with the renderer's "
            "parser; refusing to build a template the renderer reads differently"
        )
    return spans


def product_locations(renderer: Any, text: str) -> list[tuple[str, str, int, int]]:
    """Every location that can match /pro, redirect-only ones included."""
    found: list[tuple[str, str, int, int]] = []
    for header, body, start, end in location_spans(renderer, text):
        modifier, value = renderer.parse_location_header(header)
        value = value.replace(r"\/", "/")
        literal = modifier not in {"~", "~*", "@"} and (
            value == "/pro" or value.startswith("/pro/")
        )
        regex = modifier in {"~", "~*"} and renderer.regex_can_match_product(value)
        if literal or regex:
            found.append((header, body, start, end))
    return found


def managed_locations(renderer: Any, text: str) -> list[tuple[str, str, int, int]]:
    """The /pro locations that must carry a marker, in source order."""
    return [
        span
        for span in product_locations(renderer, text)
        if not renderer.is_redirect_only(span[1])
    ]


def replace_proxy_pass(body: str, marker: str) -> tuple[str, str]:
    """Swap the single proxy_pass line for `marker`; return (new body, target)."""
    lines = body.splitlines(keepends=True)
    hits = [index for index, line in enumerate(lines) if PROXY_PASS_RE.match(line.rstrip("\n"))]
    if len(hits) != 1:
        fail(
            f"a managed /pro location has {len(hits)} proxy_pass statements; "
            "exactly one is required so the substitution is unambiguous"
        )
    index = hits[0]
    match = PROXY_PASS_RE.match(lines[index].rstrip("\n"))
    assert match is not None
    # The marker is a COMMENT, so it must always end the line. Five of the seven
    # managed locations on 198 are one-liners -- `location ^~ /pro/ui/ {
    # proxy_pass http://litellm_product; }` -- whose body carries no trailing
    # newline. Reusing the original line ending there would comment out the
    # closing brace, and the only symptom is nginx reporting an unterminated
    # block somewhere else in the file.
    lines[index] = f"{match.group('indent')}{marker}\n"
    return "".join(lines), match.group("target").strip()


def insert_http_marker(renderer: Any, text: str) -> str:
    """Put the http marker before the FIRST directive in the file.

    Not before `upstream litellm_product`, which was the obvious anchor and is
    wrong. The rendered http block sets `map_hash_bucket_size`, and nginx binds
    that value when it parses the first `map` in the http context -- setting it
    afterwards is rejected as `"map_hash_bucket_size" directive is duplicate`,
    which fails `nginx -t` for the whole site. Measured on nginx 1.31 and
    consistent with 1.18's source: a `map` before the directive is enough.

    198's live file opens with `map $http_referer $route_env` at line 7, well
    before any upstream, so the only placement that works is the top of the
    file. That is also the earliest point available: `conf.d/` is empty and this
    file sorts first in `sites-enabled/`, so nothing in the http block is parsed
    before it.

    ⚠️ The ordering constraint is real and outside this file's control. Adding a
    `conf.d/*.conf` that contains a `map`, or a `sites-enabled` file sorting
    before this one, would make the directive a duplicate again. That fails
    closed at `nginx -t`, before any reload -- but it fails during the window,
    so re-check before freezing if either directory has changed.
    """
    masked = renderer.syntax_mask(text)
    if len(list(re.finditer(r"upstream[ \t]+litellm_product[ \t]*\{", masked))) != 1:
        fail("expected exactly one `upstream litellm_product` in the live config")

    depth = 0
    offset = 0
    for line in masked.splitlines(keepends=True):
        if depth == 0 and line.strip():
            return text[:offset] + renderer.HTTP_MARKER + "\n" + text[offset:]
        depth += line.count("{") - line.count("}")
        offset += len(line)
    fail("live config has no http-scope directive to anchor the http marker to")


def atomic_write(path: Path, content: str, mode: int) -> None:
    if path.is_symlink():
        fail("output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode, follow_symlinks=False)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        fail(f"cannot write {path}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--expect-managed",
        type=int,
        required=True,
        help="managed /pro locations expected; a drifted live config must stop here",
    )
    parser.add_argument(
        "--expect-redirect-only",
        type=int,
        required=True,
        help="redirect-only /pro locations expected (198 2026-09-13: 2)",
    )
    args = parser.parse_args(argv)

    renderer = load_renderer()
    try:
        info = args.live_config.lstat()
    except OSError as exc:
        fail(f"cannot stat live config: {exc}")
    if not stat.S_ISREG(info.st_mode) or args.live_config.is_symlink():
        fail("live config must be a regular non-symlink file")
    try:
        live = args.live_config.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read live config: {exc}")

    if "@@LITELLM_GRAY" in live:
        fail("live config already contains gray markers; it is not a clean base")

    redirect_only = [
        header
        for header, body, _, _ in product_locations(renderer, live)
        if renderer.is_redirect_only(body)
    ]
    managed = managed_locations(renderer, live)
    if len(managed) != args.expect_managed:
        fail(
            f"live config has {len(managed)} managed /pro locations, "
            f"--expect-managed said {args.expect_managed}; re-measure before rendering"
        )
    if len(redirect_only) != args.expect_redirect_only:
        fail(
            f"live config has {len(redirect_only)} redirect-only /pro locations, "
            f"--expect-redirect-only said {args.expect_redirect_only}"
        )

    # Splice the managed bodies by offset, left to right. Bodies are
    # non-overlapping and in source order, so one pass with a cursor is enough.
    pieces: list[str] = []
    cursor = 0
    rows: list[dict[str, str]] = []
    variable_targets = 0
    for header, body, start, end in managed:
        if start < cursor:
            fail(f"internal error: overlapping location spans at {header}")
        probe_new, probe_target = replace_proxy_pass(body, renderer.PROXY_MARKER)
        if "$" in probe_target:
            variable_targets += 1
            if variable_targets > 1:
                fail(
                    "more than one managed /pro location proxies to a variable "
                    "upstream; this tool refuses to assume how two pre-existing "
                    "splits compose with the gray state machine"
                )
            new_body, _ = replace_proxy_pass(body, renderer.WS_SPLIT_MARKER)
            marker = "ws-split"
        else:
            new_body, marker = probe_new, "product"
        pieces.append(live[cursor:start])
        pieces.append(new_body)
        cursor = end
        rows.append(
            {
                "location": " ".join(header.split()),
                "proxy_pass_target": probe_target,
                "marker": marker,
            }
        )
    pieces.append(live[cursor:])
    result = "".join(pieces)

    result = insert_http_marker(renderer, result)

    # Final authority is the renderer's own validator, not ours.
    if result.count(renderer.HTTP_MARKER) != 1:
        fail("internal error: http marker was not inserted exactly once")
    renderer.validate_product_locations(result)

    atomic_write(args.output, result, 0o600)
    digest = {
        "tool": TOOL,
        "schema_version": 1,
        "status": "PASS",
        "live_config": str(args.live_config.resolve()),
        "live_config_sha256": hashlib.sha256(live.encode("utf-8")).hexdigest(),
        "output": str(args.output.resolve()),
        "output_sha256": hashlib.sha256(result.encode("utf-8")).hexdigest(),
        "managed_locations": len(managed),
        "redirect_only_exempt": len(redirect_only),
        "ws_split_locations": variable_targets,
        "locations": rows,
    }
    print(json.dumps(digest, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
