#!/usr/bin/env python3
"""Render the production nginx candidate from a reviewed base template."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path


HTTP_MARKER = "# @@LITELLM_GRAY_HTTP_DIRECTIVES@@"
PROXY_MARKER = "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@"
GENERATION_FILES = (
    "protected-prod.map",
    "force-prod.map",
    "force-gray.map",
    "key-sid.map",
    "convergence-mode.map",
    "bridge-override.map",
    "split.conf",
)
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{8,128}$")
SPLIT_RULE = re.compile(
    r"(?:\*|(?:100|[1-9]?\d)(?:\.\d{1,2})?%)\s+litellm_(?:gray|product);"
)


def syntax_mask(text: str) -> str:
    """Mask comments and quoted strings while preserving brace positions."""

    masked = list(text)
    quote: str | None = None
    escaped = False
    comment = False
    for index, char in enumerate(text):
        if comment:
            if char == "\n":
                comment = False
            else:
                masked[index] = " "
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            if char != "\n":
                masked[index] = " "
            continue
        if char == "#":
            comment = True
            masked[index] = " "
        elif char in {'"', "'"}:
            quote = char
            masked[index] = " "
    return "".join(masked)


def location_blocks(text: str) -> list[tuple[str, str]]:
    masked = syntax_mask(text)
    result: list[tuple[str, str]] = []
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
            fail("base template contains an unterminated location block")
        header = text[match.start("header") : opening].strip()
        result.append((header, text[opening + 1 : closing]))
    return result


def parse_location_header(header: str) -> tuple[str, str]:
    normalized = header.strip()
    for modifier in ("^~", "~*", "=", "~"):
        if normalized == modifier or normalized.startswith(modifier + " "):
            return modifier, normalized[len(modifier) :].strip().strip('"\'')
    return "", normalized.strip('"\'')


def regex_can_match_product(value: str) -> bool:
    compact = re.sub(r"\s+", "", value.replace(r"\/", "/"))
    product_tokens = ("/pro", "|pro", "pro|", "(pro", "[pP][rR][oO]")
    return any(token in compact for token in product_tokens)


def validate_product_locations(base: str) -> None:
    blocks = location_blocks(base)
    marker_count = base.count(PROXY_MARKER)
    if marker_count < 1:
        fail("base template must contain at least one product proxy marker")

    generic_locations = 0
    managed_markers = 0
    for header, body in blocks:
        modifier, value = parse_location_header(header)
        value = value.replace(r"\/", "/")
        markers = body.count(PROXY_MARKER)
        if markers > 1:
            fail("a /pro location contains the product proxy marker more than once")
        managed_markers += markers

        is_literal_product = modifier not in {"~", "~*", "@"} and (
            value == "/pro" or value.startswith("/pro/")
        )
        is_regex_product = modifier in {"~", "~*"} and regex_can_match_product(value)
        if not is_literal_product and not is_regex_product:
            if markers:
                fail("product proxy marker must be inside a literal /pro location")
            continue
        if markers != 1:
            fail(f"unmanaged /pro location: {header}")
        if is_regex_product and any(token in value for token in ("|", "[", ")")):
            fail(f"shared or ambiguous regex /pro location must be split before rollout: {header}")
        if modifier in {"", "^~"} and value == "/pro/":
            generic_locations += 1

    if generic_locations != 1:
        fail("base template must contain exactly one managed literal /pro/ location")
    if managed_markers != marker_count:
        fail("product proxy marker is outside a managed location")


def fail(message: str) -> None:
    raise SystemExit(f"render-production-nginx: {message}")


def require_regular(path: Path, mode: int) -> str:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot stat {path}: {exc}")
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        fail(f"{path} must be a regular non-symlink file")
    if stat.S_IMODE(info.st_mode) != mode:
        fail(f"{path} must have mode {mode:04o}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read {path}: {exc}")


def validate_generation(path: Path) -> dict[str, str]:
    try:
        info = path.lstat()
    except OSError as exc:
        fail(f"cannot stat generation: {exc}")
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or stat.S_IMODE(info.st_mode) != 0o700:
        fail("generation must be a real mode 0700 directory")
    values = {name: require_regular(path / name, 0o600) for name in GENERATION_FILES}
    lines = [line.strip() for line in values["split.conf"].splitlines() if line.strip()]
    if not lines or any(not SPLIT_RULE.fullmatch(line) for line in lines):
        fail("split.conf contains an unsupported rule")
    if lines[-1] not in {"* litellm_product;", "* litellm_gray;"}:
        fail("split.conf must end with a wildcard fallback")
    values["split.conf"] = "\n".join(lines)
    return values


def safe_include_path(path: Path) -> str:
    text = str(path.resolve(strict=True))
    if any(char in text for char in ('\n', '\r', '"', "'", ';', '{', '}')):
        fail("generation path contains unsafe nginx syntax")
    return text


def http_directives(
    generation: Path,
    split_rules: str,
    debug_token: str,
    *,
    gray_port: int = 30405,
    bridge_port: int = 30406,
) -> str:
    root = safe_include_path(generation)
    split = "\n".join(f"        {line}" for line in split_rules.splitlines())
    return f'''upstream litellm_gray {{ server 127.0.0.1:{gray_port}; keepalive 32; }}
upstream litellm_guarded_old {{ server 127.0.0.1:{bridge_port}; keepalive 32; }}

map $http_authorization $auth_token {{
    default "";
    "~*^\\s*Bearer\\s+(?<auth_token_value>sk-[A-Za-z0-9._~-]{{8,512}})\\s*$" $auth_token_value;
}}
map $http_authorization $auth_present {{ "" 0; default 1; }}
map $http_x_api_key $x_api_token {{
    default "";
    "~^\\s*(?<x_api_token_value>sk-[A-Za-z0-9._~-]{{8,512}})\\s*$" $x_api_token_value;
}}
map "$auth_present:$auth_token" $canonical_key {{
    "0:" $x_api_token;
    ~^1:.+ $auth_token;
    default "";
}}
map $canonical_key $bucket_key {{ "" ""; default $canonical_key; }}
map $canonical_key $protected_prod {{ default 0; include {root}/protected-prod.map; }}
map $canonical_key $force_prod {{ default 0; include {root}/force-prod.map; }}
map $canonical_key $force_gray {{ default 0; include {root}/force-gray.map; }}
map $canonical_key $key_sid {{ default "-"; include {root}/key-sid.map; }}
split_clients "$bucket_key" $pct_pool_raw {{
{split}
}}
map "$bucket_key:$pct_pool_raw" $pct_pool {{ ~^: litellm_product; default $pct_pool_raw; }}
map "$protected_prod:$force_prod:$force_gray:$pct_pool" $normal_inference_upstream {{
    ~^1: litellm_product;
    ~^0:1: litellm_product;
    ~^0:0:1: litellm_gray;
    "0:0:0:litellm_gray" litellm_gray;
    default litellm_product;
}}
map "" $convergence_mode {{ include {root}/convergence-mode.map; }}
map "" $bridge_override {{ include {root}/bridge-override.map; }}
map "$bridge_override:$convergence_mode" $whole_product_override {{
    ~^guarded-old: litellm_guarded_old;
    ~^off:1$ litellm_gray;
    default "";
}}
map $uri $normal_route_upstream {{
    ~^/(pro/)?(v1/)?(messages|responses|chat/completions|completions|embeddings|images/generations)(/|$) $normal_inference_upstream;
    default litellm_product;
}}
map "$bridge_override:$convergence_mode:$normal_route_upstream" $effective_upstream {{
    ~^guarded-old: litellm_guarded_old;
    ~^off:1: litellm_gray;
    default $normal_route_upstream;
}}
map $whole_product_override $product_upstream {{
    litellm_gray litellm_gray;
    litellm_guarded_old litellm_guarded_old;
    default $normal_route_upstream;
}}
map $effective_upstream $pool_label {{
    litellm_gray canary;
    litellm_guarded_old guarded-old;
    default stable;
}}
map $uri $uri_class {{
    ~*/embeddings embedding;
    ~*/images/ image;
    ~*/messages messages;
    ~*/responses responses;
    ~*/chat/ chat;
    default other;
}}
geo $gray_debug_source {{ default 0; 127.0.0.1/32 1; }}
map "$gray_debug_source:$http_x_llm_debug" $pool_hdr {{
    default "";
    "1:{debug_token}" $pool_label;
}}
log_format litellm_gray 'ts=$time_iso8601 $remote_addr $status $request_time '
                        'pool=$pool_label upstream=$upstream_addr rt=$upstream_response_time '
                        'uri_class=$uri_class sid=$key_sid';'''


def proxy_directives(access_log: str = "/var/log/nginx/cc-auto-link.gray.log") -> str:
    return f"""access_log {access_log} litellm_gray;
proxy_http_version 1.1;
proxy_buffering off;
# Must equal chart values drain.streamDrainSeconds: nginx may not promise to
# wait longer than a Terminating Pod is allowed to live, or a rolling update
# truncates the SSE stream instead of returning a clean 504.
proxy_read_timeout 570s;
proxy_pass http://$product_upstream;
add_header X-LLM-Pool $pool_hdr always;"""


def atomic_replace(path: Path, content: str) -> None:
    if path.is_symlink():
        fail("output must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        fail("output parent is unsafe")
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        fail(f"cannot atomically replace output: {exc}")


def sha256_path(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        fail(f"cannot checksum {path}: {exc}")


def canonical_path(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except OSError as exc:
        fail(f"cannot resolve {path}: {exc}")


def generation_config_checksum(generation: Path) -> str:
    state = require_regular(generation / "state.env", 0o600)
    matches = [
        line.split("=", 1)[1]
        for line in state.splitlines()
        if line.startswith("config_checksum=")
    ]
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0]):
        fail("generation state has an invalid config_checksum")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-template", type=Path, required=True)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--debug-token-file", type=Path, required=True)
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--gray-port", type=int, default=30405, help=argparse.SUPPRESS)
    parser.add_argument("--bridge-port", type=int, default=30406, help=argparse.SUPPRESS)
    parser.add_argument("--access-log", default="/var/log/nginx/cc-auto-link.gray.log", help=argparse.SUPPRESS)
    args = parser.parse_args()

    base = require_regular(args.base_template, 0o600)
    debug_token = require_regular(args.debug_token_file, 0o600).strip()
    if not SAFE_TOKEN.fullmatch(debug_token):
        fail("debug token is invalid")
    values = validate_generation(args.generation)
    if base.count(HTTP_MARKER) != 1:
        fail("base template must contain the HTTP gray marker exactly once")
    if not re.search(r"\bupstream\s+litellm_product\s*\{", base):
        fail("base template must preserve the litellm_product upstream")
    validate_product_locations(base)
    rendered = base.replace(
        HTTP_MARKER,
        http_directives(
            args.generation, values["split.conf"], debug_token,
            gray_port=args.gray_port, bridge_port=args.bridge_port,
        ),
    ).replace(PROXY_MARKER, proxy_directives(args.access_log))
    if "@@LITELLM_GRAY" in rendered:
        fail("unresolved gray marker remains")
    atomic_replace(args.output, rendered)
    result = {
        "tool": "render-production-nginx",
        "status": "PASS",
        "generation": canonical_path(args.generation),
        "output": canonical_path(args.output),
        "sha256": sha256_path(args.output),
    }
    if args.attestation is not None:
        generation = args.generation.resolve(strict=True)
        attestation = args.attestation.resolve(strict=False)
        if attestation.parent != generation:
            fail("attestation must be written inside the candidate generation")
        evidence = {
            "tool": "render-production-nginx-attestation",
            "schema_version": 1,
            "status": "PASS",
            "generation": str(generation),
            "generation_config_checksum": generation_config_checksum(args.generation),
            "base_template": canonical_path(args.base_template),
            "base_template_sha256": sha256_path(args.base_template),
            "debug_token_file": canonical_path(args.debug_token_file),
            "debug_token_sha256": sha256_path(args.debug_token_file),
            "renderer": canonical_path(Path(__file__)),
            "renderer_sha256": sha256_path(Path(__file__)),
            "output": canonical_path(args.output),
            "output_sha256": sha256_path(args.output),
        }
        atomic_replace(
            args.attestation,
            json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
        )
        result["attestation"] = canonical_path(args.attestation)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
