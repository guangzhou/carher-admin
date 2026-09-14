"""Contracts for turning the live product nginx config into a base template.

The shapes asserted here are not invented. They are what the renderer's own
parser reported against 198's live `sites-enabled/cc.auto-link.com.cn.conf` on
2026-09-14:

    locations_total=38  pro_matching=9  redirect_only_exempt=2
    markers_required=7  literal_pro_slash_catchall=1

and the directive shape inside those 7 -- note that five of them are written as
ONE-LINERS, body and closing brace on the same line:

    ^~ /pro/swagger/              { proxy_pass http://litellm_product; }
    ^~ /pro/litellm-asset-prefix/ { proxy_pass http://litellm_product; }
    =  /pro/openapi.json          { proxy_pass http://litellm_product; }
    ^~ /pro/ui/                   { proxy_pass http://litellm_product; }
    ^~ /pro/_next/                { proxy_pass http://litellm_product; }
    =  /pro/v1/responses          rewrite; proxy_pass http://$pro_responses_backend;
                                  proxy_set_header Upgrade; proxy_set_header Connection
       /pro/                      rewrite; proxy_pass http://litellm_product;

The one-liners are not cosmetic. The marker is an nginx COMMENT, so substituting
it for a one-liner's `proxy_pass` without adding a newline comments out the
closing brace -- and nginx then reports an unterminated block, pointing somewhere
else in the file entirely. The first version of this fixture guessed multi-line
bodies for all seven and went green while the real config failed.

The other thing that matters is `= /pro/v1/responses`. Its proxy_pass target is a
variable, not the product upstream: a map sends `Upgrade: websocket` to
`ws_ingress`, the codex incremental terminator. Substituting the plain product
marker there would take that terminator out of the path -- the config would
still render, nginx would still pass `-t`, and the feature would just be gone.
Most of this file exists to keep that from being possible.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build-base-template.py"
RENDERER = ROOT / "scripts" / "render-production-nginx.py"

# A structural stand-in for the live file: same 9/2/7/1 shape, same directive
# mix, none of the site's unrelated 29 locations. It is not a sample of live
# traffic or a measurement, so reproducing it verbatim buys nothing -- what has
# to be reproduced is the SHAPE, and that is asserted against the live counts in
# the module docstring.
LIVE_LIKE = """
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
map $http_upgrade $pro_responses_backend {
    default      litellm_product;
    ~*websocket  ws_ingress;
}

upstream litellm_product { server 127.0.0.1:30402; keepalive 32; }
upstream ws_ingress      { server 127.0.0.1:30403; keepalive 32; }

server {
    listen 80;
    proxy_read_timeout 600s;
    access_log /var/log/nginx/zkreq.log zkreq;

    location = /pro { return 301 /pro/; }
    location = /pro/ui { return 301 /pro/ui/; }

    location ^~ /pro/swagger/ { proxy_pass http://litellm_product; }
    location ^~ /pro/litellm-asset-prefix/ { proxy_pass http://litellm_product; }
    location = /pro/openapi.json { proxy_pass http://litellm_product; }
    location ^~ /pro/ui/ { proxy_pass http://litellm_product; }
    location ^~ /pro/_next/ { proxy_pass http://litellm_product; }
    location = /pro/v1/responses {
        rewrite ^/pro/(.*)$ /$1 break;
        proxy_pass http://$pro_responses_backend;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
    }
    location /pro/ {
        rewrite ^/pro/(.*)$ /$1 break;
        proxy_pass http://litellm_product;
    }
    location /other/ {
        proxy_pass http://litellm_product;
    }
}
"""


def build(tmp_path: Path, live: str = LIVE_LIKE, *, managed: int = 7, redirect: int = 2):
    source = tmp_path / "live.conf"
    source.write_text(live, encoding="utf-8")
    output = tmp_path / "base-template.conf"
    process = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--live-config",
            str(source),
            "--output",
            str(output),
            "--expect-managed",
            str(managed),
            "--expect-redirect-only",
            str(redirect),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    digest = json.loads(process.stdout) if process.stdout.strip() else None
    return process, digest, output


def mutate(old: str, new: str, live: str = LIVE_LIKE) -> str:
    """`live.replace(old, new)`, but a no-op replacement is an error.

    Every negative test below builds its input by editing the fixture. A
    `str.replace` that matches nothing returns the fixture unchanged, so the
    test would then assert that the UNMODIFIED config is rejected -- and go
    green or red for reasons unrelated to what it claims to check. Reformatting
    the fixture once already broke this silently.
    """
    assert old in live, f"fixture no longer contains:\n{old}"
    return live.replace(old, new, 1)


def test_the_live_shape_produces_seven_markers_and_two_exemptions(tmp_path: Path):
    process, digest, output = build(tmp_path)
    assert process.returncode == 0, process.stderr
    assert digest["managed_locations"] == 7
    assert digest["redirect_only_exempt"] == 2
    assert digest["ws_split_locations"] == 1
    text = output.read_text(encoding="utf-8")
    assert text.count("# @@LITELLM_GRAY_HTTP_DIRECTIVES@@") == 1
    assert text.count("# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@") == 6
    assert text.count("# @@LITELLM_GRAY_PRODUCT_PROXY_WS_SPLIT_DIRECTIVES@@") == 1


def test_the_websocket_split_location_gets_the_ws_marker_not_the_plain_one(tmp_path: Path):
    """The whole point. A plain marker here silently drops the terminator."""
    _, digest, _ = build(tmp_path)
    rows = {row["location"]: row for row in digest["locations"]}
    assert rows["= /pro/v1/responses"]["marker"] == "ws-split"
    assert rows["= /pro/v1/responses"]["proxy_pass_target"] == "http://$pro_responses_backend"
    for location, row in rows.items():
        if location != "= /pro/v1/responses":
            assert row["marker"] == "product", location


def test_everything_that_is_not_the_proxy_pass_survives_untouched(tmp_path: Path):
    """rewrite/proxy_set_header encode behaviour the renderer knows nothing about."""
    _, _, output = build(tmp_path)
    text = output.read_text(encoding="utf-8")
    assert text.count("rewrite ^/pro/(.*)$ /$1 break;") == 2
    assert "proxy_set_header Upgrade $http_upgrade;" in text
    assert "proxy_set_header Connection $connection_upgrade;" in text
    # The pre-existing map must still be there; the ws marker consumes it.
    assert "map $http_upgrade $pro_responses_backend {" in text
    # The only surviving product proxy_pass is the non-/pro location.
    assert text.split("server {")[1].count("proxy_pass http://litellm_product;") == 1


def test_a_location_outside_pro_is_never_touched(tmp_path: Path):
    _, _, output = build(tmp_path)
    text = output.read_text(encoding="utf-8")
    other = text.split("location /other/ {")[1]
    assert "proxy_pass http://litellm_product;" in other
    assert "@@LITELLM_GRAY" not in other


def test_output_is_mode_600(tmp_path: Path):
    _, _, output = build(tmp_path)
    assert oct(output.stat().st_mode)[-3:] == "600"


def test_a_one_liner_location_keeps_its_closing_brace(tmp_path: Path):
    """The marker is a comment; on a one-liner it must not swallow the `}`.

    Five of 198's seven managed locations are one-liners. Emitting the marker
    without a newline produces `location ^~ /pro/ui/ { # @@...@@ }` -- the brace
    is inside the comment, so the block never closes and nginx blames some later
    block in the file. It also renders and checksums perfectly, so nothing
    upstream of `nginx -t` would have caught it.
    """
    process, _, output = build(tmp_path)
    assert process.returncode == 0, process.stderr
    text = output.read_text(encoding="utf-8")
    for location in ("^~ /pro/swagger/", "= /pro/openapi.json", "^~ /pro/_next/"):
        block = text.split(f"location {location} {{")[1]
        head, _, _ = block.partition("}")
        assert "# @@LITELLM_GRAY_PRODUCT_PROXY_DIRECTIVES@@" in head, location
        # The brace has to survive on its own line, after the comment.
        assert head.endswith("\n"), location
    # And the braces still balance -- the concrete failure was a `}` that got
    # commented away, which shows up here as one fewer closing brace.
    assert text.count("{") == text.count("}")


def test_a_drifted_live_config_stops_instead_of_rendering(tmp_path: Path):
    """The expected counts are a tripwire, not decoration.

    The live config changes -- it changed twice in the three weeks before this
    was written. Rendering against a shape nobody re-measured is how an
    unmanaged /pro location reaches production still pointing at old prod.
    """
    extra = mutate(
        "    location /other/ {",
        "    location ^~ /pro/extra/ { proxy_pass http://litellm_product; }\n"
        "    location /other/ {",
    )
    process, _, _ = build(tmp_path, extra)
    assert process.returncode != 0
    assert "managed /pro locations" in process.stderr


def test_two_variable_upstreams_are_refused_rather_than_guessed(tmp_path: Path):
    doubled = mutate(
        "    location ^~ /pro/swagger/ { proxy_pass http://litellm_product; }",
        "    location ^~ /pro/swagger/ { proxy_pass http://$pro_responses_backend; }",
    )
    process, _, _ = build(tmp_path, doubled)
    assert process.returncode != 0
    assert "more than one" in process.stderr


def test_a_location_with_two_proxy_pass_statements_is_refused(tmp_path: Path):
    ambiguous = mutate(
        "    location ^~ /pro/ui/ { proxy_pass http://litellm_product; }",
        "    location ^~ /pro/ui/ {\n        proxy_pass http://litellm_product;\n"
        "        proxy_pass http://litellm_product;\n    }",
    )
    process, _, _ = build(tmp_path, ambiguous)
    assert process.returncode != 0
    assert "proxy_pass statements" in process.stderr


def test_an_already_marked_config_is_not_accepted_as_a_clean_base(tmp_path: Path):
    process, _, _ = build(tmp_path, LIVE_LIKE + "\n# @@LITELLM_GRAY_HTTP_DIRECTIVES@@\n")
    assert process.returncode != 0
    assert "already contains gray markers" in process.stderr


DEBUG_TOKEN = "debug-token-value"


def render(
    tmp_path: Path,
    template: Path,
    *extra: str,
    debug_token: str = DEBUG_TOKEN,
    key_sid: str = "",
):
    generation = tmp_path / "gen"
    generation.mkdir(mode=0o700)
    for name in (
        "protected-prod.map",
        "force-prod.map",
        "force-gray.map",
        "key-sid.map",
        "convergence-mode.map",
        "bridge-override.map",
    ):
        path = generation / name
        path.write_text("" if "mode" not in name and "override" not in name else
                        ('default 0;\n' if name == "convergence-mode.map" else 'default off;\n'),
                        encoding="utf-8")
        path.chmod(0o600)
    if key_sid:
        (generation / "key-sid.map").write_text(key_sid, encoding="utf-8")
        (generation / "key-sid.map").chmod(0o600)
    split = generation / "split.conf"
    split.write_text("* litellm_product;\n", encoding="utf-8")
    split.chmod(0o600)
    state = generation / "state.env"
    state.write_text("config_checksum=" + "a" * 64 + "\n", encoding="utf-8")
    state.chmod(0o600)
    token = tmp_path / "token"
    token.write_text(debug_token + "\n", encoding="utf-8")
    token.chmod(0o600)
    output = tmp_path / "candidate.conf"
    process = subprocess.run(
        [
            sys.executable,
            str(RENDERER),
            "--base-template",
            str(template),
            "--generation",
            str(generation),
            "--output",
            str(output),
            "--debug-token-file",
            str(token),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return process, output


def test_rendering_the_built_template_keeps_the_websocket_half_on_the_terminator(
    tmp_path: Path,
):
    """End to end: build, then render, then read the routing actually produced."""
    _, _, template = build(tmp_path)
    process, output = render(
        tmp_path,
        template,
        "--ws-split-upstream",
        "ws_ingress",
        "--inherited-access-log",
        "/var/log/nginx/zkreq.log zkreq",
    )
    assert process.returncode == 0, process.stderr
    text = output.read_text(encoding="utf-8")
    # The gray decision reaches the responses location only through the split.
    assert "map $http_upgrade $gray_ws_upgrade { default 0; ~*websocket 1; }" in text
    assert "~^1: ws_ingress;" in text
    assert "proxy_pass http://$gray_ws_split_upstream;" in text
    # ...and the other six go straight at the state machine's variable.
    assert text.count("proxy_pass http://$product_upstream;") == 6
    assert "@@LITELLM_GRAY" not in text


def test_the_inherited_site_access_log_is_re_emitted_beside_the_gray_one(tmp_path: Path):
    """A location-scope access_log REPLACES the inherited one.

    198's product server logs to zkreq.log, which is what the latency-triage SOP
    reads. Emitting only the gray log would delete /pro from it for the whole
    window -- blinding the path used precisely when a rollout looks wrong.
    """
    _, _, template = build(tmp_path)
    _, output = render(
        tmp_path,
        template,
        "--ws-split-upstream",
        "ws_ingress",
        "--inherited-access-log",
        "/var/log/nginx/zkreq.log zkreq",
    )
    text = output.read_text(encoding="utf-8")
    # 7 managed locations + the untouched server-scope directive it inherits from.
    assert text.count("access_log /var/log/nginx/zkreq.log zkreq;") == 8
    assert text.count("access_log /var/log/nginx/cc-auto-link.gray.log litellm_gray;") == 7


def test_without_the_flag_the_inherited_log_is_not_invented(tmp_path: Path):
    _, _, template = build(tmp_path)
    _, output = render(tmp_path, template, "--ws-split-upstream", "ws_ingress")
    text = output.read_text(encoding="utf-8")
    assert "zkreq" in text  # still present at server scope, untouched
    assert text.count("access_log /var/log/nginx/zkreq.log zkreq;") == 1


def test_ws_marker_without_its_upstream_fails_closed(tmp_path: Path):
    _, _, template = build(tmp_path)
    process, _ = render(tmp_path, template)
    assert process.returncode != 0
    assert "ws-split" in process.stderr


def test_ws_upstream_without_a_ws_marker_fails_closed(tmp_path: Path):
    """Guards against the flag outliving the location it was added for."""
    plain = mutate("http://$pro_responses_backend", "http://litellm_product")
    _, _, template = build(tmp_path, plain)
    process, _ = render(tmp_path, template, "--ws-split-upstream", "ws_ingress")
    assert process.returncode != 0
    assert "no ws-split marker" in process.stderr


@pytest.mark.parametrize(
    "spec", ["/var/log/nginx/x.log; root /etc", "relative.log fmt", "/var/log/x.log fmt extra"]
)
def test_a_malformed_inherited_access_log_is_refused(tmp_path: Path, spec: str):
    """It is pasted into the config verbatim; the shape is the only guard."""
    _, _, template = build(tmp_path)
    process, _ = render(
        tmp_path, template, "--ws-split-upstream", "ws_ingress", "--inherited-access-log", spec
    )
    assert process.returncode != 0
    assert "inherited-access-log" in process.stderr


def map_hash_values(text: str) -> tuple[int, int]:
    bucket = re.search(r"map_hash_bucket_size (\d+);", text)
    max_size = re.search(r"map_hash_max_size (\d+);", text)
    assert bucket and max_size, "rendered config carries no map hash sizing"
    return int(bucket.group(1)), int(max_size.group(1))


def test_map_hash_is_sized_above_the_longest_literal_key(tmp_path: Path):
    """A 64-hex debug token overflows nginx's 64-byte default bucket.

    Measured on 198 2026-09-14 in a scratch nginx root: `openssl rand -hex 32`
    makes the `$pool_hdr` key `"1:<64 hex>"` = 68 bytes, and `nginx -t` refuses
    the whole config with `could not build map_hash`. It is not a soft
    degradation and not specific to that map -- any literal key over the bucket
    size does it.
    """
    _, _, template = build(tmp_path)
    process, output = render(
        tmp_path, template, "--ws-split-upstream", "ws_ingress", debug_token="a" * 64
    )
    assert process.returncode == 0, process.stderr
    text = output.read_text(encoding="utf-8")
    bucket, _ = map_hash_values(text)
    assert bucket >= 68 + 8
    assert f'"1:{"a" * 64}"' in text


def test_map_hash_grows_for_a_long_key_in_the_generation_map_files(tmp_path: Path):
    """key-sid.map keys are whole virtual keys, not hashes of them.

    Sizing off the debug token alone would pass here and then fail `nginx -t`
    at the first generation that actually pins a key -- i.e. not at 0%, where
    the map is empty, but one step later.
    """
    long_key = "sk-" + "b" * 300
    _, _, template = build(tmp_path)
    process, output = render(
        tmp_path,
        template,
        "--ws-split-upstream",
        "ws_ingress",
        key_sid=f"{long_key} sid-1;\n",
    )
    assert process.returncode == 0, process.stderr
    bucket, _ = map_hash_values(output.read_text(encoding="utf-8"))
    assert bucket >= len(long_key) + 8


def test_an_absurd_map_key_fails_closed_instead_of_reserving_huge_buckets(tmp_path: Path):
    _, _, template = build(tmp_path)
    process, _ = render(
        tmp_path,
        template,
        "--ws-split-upstream",
        "ws_ingress",
        key_sid="sk-" + "c" * 5000 + " sid-1;\n",
    )
    assert process.returncode != 0
    assert "refusing to render" in process.stderr


def test_the_default_sized_render_is_unchanged_for_a_short_token(tmp_path: Path):
    """The 0% generation with a short token must not inflate anything."""
    _, _, template = build(tmp_path)
    _, output = render(tmp_path, template, "--ws-split-upstream", "ws_ingress")
    bucket, max_size = map_hash_values(output.read_text(encoding="utf-8"))
    assert (bucket, max_size) == (64, 2048)


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx not installed")
def test_nginx_itself_accepts_the_rendered_production_config(tmp_path: Path):
    """The only ruler that counts. Everything above is a proxy for this.

    The suite's other real-nginx test drives the *fixture* renderer, so without
    this the production renderer's output is never shown to nginx in CI -- which
    is exactly how the map_hash failure got as far as a live rehearsal.

    What this test does and does not cover, measured rather than assumed: on
    nginx 1.31 it catches the directive-ORDERING bug (a `map_hash_bucket_size`
    after the first `map` is rejected as a duplicate) but it passes with an
    oversized key and a 64-byte bucket, because newer nginx no longer refuses
    that. 198 runs nginx 1.18, which does refuse it. So this test is the ruler
    for shape and ordering; the sizing itself is guarded by the unit tests above
    and by the pre-window rehearsal against the real nginx on 198.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    # The fixture's own server-scope access_log points at /var/log/nginx, which
    # `nginx -t` opens for real. Leaving it would make this test red on any
    # machine without that directory -- an environment failure wearing the mask
    # of a config failure.
    live = mutate("/var/log/nginx/zkreq.log", f"{logs}/zkreq.log")
    _, _, template = build(tmp_path, live)
    process, output = render(
        tmp_path,
        template,
        "--ws-split-upstream",
        "ws_ingress",
        "--access-log",
        f"{logs}/gray.log",
        "--inherited-access-log",
        f"{logs}/zkreq.log zkreq",
        debug_token="a" * 64,
    )
    assert process.returncode == 0, process.stderr

    main = tmp_path / "nginx.conf"
    main.write_text(
        f"pid {tmp_path}/nginx.pid;\n"
        "events {}\n"
        "http {\n"
        "    log_format zkreq '$remote_addr $status';\n"
        f"    include {output};\n"
        "}\n",
        encoding="utf-8",
    )
    test = subprocess.run(
        ["nginx", "-t", "-c", str(main), "-p", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert test.returncode == 0, test.stderr
