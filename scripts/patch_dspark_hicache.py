#!/usr/bin/env python3
"""Patch the DSpark canonical launcher to enable sglang hierarchical KV cache.

Run ON the GPU host (h100 / 192.168.3.205).

WHY THIS FILE AND NOT `docker run`
----------------------------------
The deepseek-v4-dspark container is NOT hand-managed. It is owned by:
    cron */2  ops/daynight/watchdog_infer.sh   (relaunch if :8767 down)
    cron 08:30 ops/daynight/to_infer.sh        (day: inference on)
    cron 23:30 ops/daynight/to_train.sh        (night: GPUs to training)
all of which funnel into deploy/10-docker-run-dspark.sh. Editing a live
container is therefore pointless: the watchdog destroys and recreates it from
this script. This patch changes the source of truth so the tuning survives the
watchdog and the nightly train/infer cycle.

WHAT IT CHANGES
---------------
  env.sh                     + ENABLE_HIERARCHICAL_CACHE / HICACHE_RATIO /
                               HICACHE_WRITE_POLICY
                             ~ CHUNKED_PREFILL_SIZE 1024 -> 8192
  10-docker-run-dspark.sh    + hicache flags in build_cmd()

Measured justification (2026-08-07, 24h of production traffic):
  prompt tokens p50 71.9k / p90 146k / p99 197k / max 355k
  prefill ~8.6k tok/s  =>  a cold 146k prompt is ~15s to first token
  64.9% of requests hit the prefix cache >=90% (TTFT 0.2-0.5s)
  24.4% hit <10% and recompute p90 143k / p99 280k tokens
HiCache spills evicted prefixes to host RAM (2015 GB total, ~1959 GB free)
instead of discarding them, which is what turns that 24.4% back into hits.

NOT --hicache-size: the DSV4 path raises
  "DeepSeek V4 HiCache currently does not support --hicache-size;
   use --hicache-ratio instead"   (hybrid_pool_assembler.py:260)
even though --help claims hicache-size overrides hicache-ratio.

Host pool is ratio * device pool LINEARLY (_deepseek_v4_num_host_pages), so
ratio stays at the vendor default 2 until real host RAM use is measured.

Usage:
  python3 patch_dspark_hicache.py --check    # show planned diff, change nothing
  python3 patch_dspark_hicache.py --apply
  python3 patch_dspark_hicache.py --revert   # restore newest .bak-hicache-*
"""
import argparse
import glob
import os
import shutil
import sys
import time

DEPLOY = "/home/cltx/deepseek-v4-dspark/deploy"
ENV_SH = os.path.join(DEPLOY, "env.sh")
RUN_SH = os.path.join(DEPLOY, "10-docker-run-dspark.sh")
STAMP = time.strftime("%Y%m%dT%H%M%S")
SUFFIX = f".bak-hicache-{STAMP}"

ENV_BLOCK = """
# --- 2026-08-07: prefix-cache tuning for long-context openclaw traffic ---
# Measured: 24.4% of requests prefill with <10% cache hit; they recompute
# p90 143k / p99 280k tokens at ~8.6k tok/s => 15-30s to first token.
# HiCache spills evicted prefixes to host RAM instead of dropping them.
# ⚠ DSV4 rejects --hicache-size; ratio only. Host pool = ratio x device pool
# linearly, so raise HICACHE_RATIO only after measuring host RAM headroom.
ENABLE_HIERARCHICAL_CACHE="${ENABLE_HIERARCHICAL_CACHE:-1}"
HICACHE_RATIO="${HICACHE_RATIO:-2}"
HICACHE_WRITE_POLICY="${HICACHE_WRITE_POLICY:-write_through}"
"""

HICACHE_SHELL = """
# hicache flags are assembled here so ENABLE_HIERARCHICAL_CACHE=0 cleanly
# reverts to the pre-2026-08-07 command with no leftover flags.
HICACHE_ARGS=""
if [[ "${ENABLE_HIERARCHICAL_CACHE:-0}" == "1" ]]; then
  HICACHE_ARGS="--enable-hierarchical-cache --hicache-ratio ${HICACHE_RATIO:-2} --hicache-write-policy ${HICACHE_WRITE_POLICY:-write_through}"
fi

"""

OLD_CHUNK = 'CHUNKED_PREFILL_SIZE="${CHUNKED_PREFILL_SIZE:-1024}"'
NEW_CHUNK = (
    "# 2026-08-07: 1024 -> 8192. Prefill measured at ~8.6k tok/s with 1024 and\n"
    "# max_prefill_tokens is 16384 so 8192 fits. NOTE: GPU util during prefill\n"
    "# was 74-100%, which does NOT confirm small chunks were the bottleneck —\n"
    "# this half of the change is a measurable experiment, not a known fix.\n"
    'CHUNKED_PREFILL_SIZE="${CHUNKED_PREFILL_SIZE:-8192}"'
)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def plan():
    env = read(ENV_SH)
    run = read(RUN_SH)
    errs = []
    if "ENABLE_HIERARCHICAL_CACHE" in env or "hicache" in run:
        errs.append("already patched (hicache present) — use --revert first")
    if OLD_CHUNK not in env:
        errs.append(f"env.sh: expected literal not found: {OLD_CHUNK}")
    if "--enable-metrics" not in run:
        errs.append("10-docker-run-dspark.sh: no --enable-metrics anchor")
    if "build_cmd() {" not in run:
        errs.append("10-docker-run-dspark.sh: no build_cmd() anchor")
    if run.count("--enable-metrics") != 1:
        errs.append(
            f"10-docker-run-dspark.sh: --enable-metrics appears "
            f"{run.count('--enable-metrics')}x, need exactly 1"
        )
    return env, run, errs


def build_new(env, run):
    new_env = env.replace(OLD_CHUNK, NEW_CHUNK, 1) + ENV_BLOCK
    new_run = run.replace("build_cmd() {", HICACHE_SHELL.lstrip("\n") + "build_cmd() {", 1)
    new_run = new_run.replace(
        "    --enable-metrics \\\\",
        "    ${HICACHE_ARGS} \\\\\n    --enable-metrics \\\\",
        1,
    )
    return new_env, new_run


def cmd_check():
    env, run, errs = plan()
    for e in errs:
        print(f"BLOCKED: {e}")
    if errs:
        return 1
    new_env, new_run = build_new(env, run)
    assert "ENABLE_HIERARCHICAL_CACHE" in new_env
    assert "8192" in new_env
    assert "${HICACHE_ARGS}" in new_run
    assert "--hicache-size" not in new_run
    print("checks PASS — planned changes:")
    print("  env.sh: CHUNKED_PREFILL_SIZE 1024 -> 8192, + 3 hicache vars")
    print("  10-docker-run-dspark.sh: + HICACHE_ARGS assembly, + ${HICACHE_ARGS} in build_cmd")
    return 0


def cmd_apply():
    env, run, errs = plan()
    for e in errs:
        print(f"BLOCKED: {e}")
    if errs:
        return 1
    new_env, new_run = build_new(env, run)
    for path in (ENV_SH, RUN_SH):
        shutil.copy2(path, path + SUFFIX)
        print(f"backup: {path}{SUFFIX}")
    write(ENV_SH, new_env)
    write(RUN_SH, new_run)
    # re-read from disk and assert, rather than trusting the in-memory strings
    assert "ENABLE_HIERARCHICAL_CACHE" in read(ENV_SH)
    assert 'CHUNKED_PREFILL_SIZE="${CHUNKED_PREFILL_SIZE:-8192}"' in read(ENV_SH)
    assert "${HICACHE_ARGS}" in read(RUN_SH)
    assert "--hicache-size" not in read(RUN_SH)
    print("APPLIED. Verify with:  bash 10-docker-run-dspark.sh --dry-run start")
    return 0


def cmd_revert():
    rc = 0
    for path in (ENV_SH, RUN_SH):
        baks = sorted(glob.glob(path + ".bak-hicache-*"))
        if not baks:
            print(f"no backup for {path}")
            rc = 1
            continue
        shutil.copy2(baks[-1], path)
        print(f"restored {path} from {baks[-1]}")
    return rc


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--revert", action="store_true")
    a = ap.parse_args()
    if a.check:
        return cmd_check()
    if a.apply:
        return cmd_apply()
    return cmd_revert()


if __name__ == "__main__":
    sys.exit(main())
