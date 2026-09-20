#!/usr/bin/env python3
"""Add NCCL collective-communication optimizations to the DSpark launcher.

Run ON the GPU host (h100 / 192.168.3.205).

WHY THESE TWO FLAGS
-------------------
Prefill on tp=8 does an all-reduce per layer, and this deployment is almost
purely prefill-bound: prompts are p50 ~46k / p90 ~106k tokens while output
averages only ~366 tokens. sglang's own help text for --enable-nccl-nvls says
"Enable NCCL NVLS for prefill heavy requests", which is exactly this workload.
Hardware supports it: nvidia-smi topo -m shows NV18 between all 8 GPUs (full
NVLink mesh / NVSwitch), not PCIe.

--enable-symm-mem ("NCCL symmetric memory for fast collectives") is in the same
subsystem and is enabled together deliberately, so that if collectives misbehave
there is ONE thing to bisect rather than two half-changes.

NOT INCLUDED, ON PURPOSE
------------------------
--chunked-prefill-size 16384: only ~1.5 GB per GPU is free right now
  (80074/81559 MiB). This restart also disables HiCache, which should give GPU
  memory back; measure available_gpu_mem AFTER this restart and only then decide
  whether 16384 fits. Guessing here costs a ~14min outage per OOM.
--enable-flashinfer-allreduce-fusion: flashinfer x mxfp4-MoE interaction is
  unknown, and --disable-flashinfer-autotune being set (with no documented
  reason) hints this deployment has hit flashinfer trouble before.
--cuda-graph-backend-prefill: 'full' is rejected by this build
  ("allowed: breakable, tc_piecewise, disabled"); the allowed modes interact
  with chunked prefill + DSPARK speculative decoding in ways not yet tested.
--enable-mixed-chunk: concurrency here is 1-3, so mixing prefill and decode in
  one batch has nothing to gain yet.

All of the above passed sglang's arg-level validation in a throwaway
--gpus all container, but note that only catches argparse/post-init errors, NOT
runtime failures (the HiCache rejection surfaced only after weight load).

Usage:
  python3 patch_dspark_nccl.py --check | --apply | --revert
"""
import argparse
import glob
import shutil
import sys
import time

DEPLOY = "/home/cltx/deepseek-v4-dspark/deploy"
ENV_SH = f"{DEPLOY}/env.sh"
RUN_SH = f"{DEPLOY}/10-docker-run-dspark.sh"
SUFFIX = ".bak-nccl-" + time.strftime("%Y%m%dT%H%M%S")

ENV_ANCHOR = 'WATCHDOG_TIMEOUT="${WATCHDOG_TIMEOUT:-300}"'
ENV_ADD = '''

# --- 2026-08-07: NCCL collective optimizations for a prefill-bound workload ---
# tp=8 all-reduces dominate prefill here (p50 ~46k-token prompts, ~366-token
# outputs). Hardware check: nvidia-smi topo -m reports NV18 across all 8 GPUs,
# so NVLS-capable. Toggle both off with =0 to get the pre-change command back.
ENABLE_NCCL_NVLS="${ENABLE_NCCL_NVLS:-1}"
ENABLE_SYMM_MEM="${ENABLE_SYMM_MEM:-1}"'''

RUN_ANCHOR = 'HICACHE_ARGS=""'
RUN_ADD = '''COMM_ARGS=""
if [[ "${ENABLE_NCCL_NVLS:-0}" == "1" ]]; then
  COMM_ARGS="$COMM_ARGS --enable-nccl-nvls"
fi
if [[ "${ENABLE_SYMM_MEM:-0}" == "1" ]]; then
  COMM_ARGS="$COMM_ARGS --enable-symm-mem"
fi

HICACHE_ARGS=""'''

CMD_ANCHOR = "    ${HICACHE_ARGS} \\\\"
CMD_NEW = "    ${HICACHE_ARGS} \\\\\n    ${COMM_ARGS} \\\\"


def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def problems(env, run):
    errs = []
    if "ENABLE_NCCL_NVLS" in env or "COMM_ARGS" in run:
        errs.append("already patched")
    if env.count(ENV_ANCHOR) != 1:
        errs.append(f"env.sh anchor count={env.count(ENV_ANCHOR)}, need 1")
    if run.count(RUN_ANCHOR) != 1:
        errs.append(f"run.sh HICACHE_ARGS anchor count={run.count(RUN_ANCHOR)}, need 1")
    if run.count(CMD_ANCHOR) != 1:
        errs.append(f"run.sh cmd anchor count={run.count(CMD_ANCHOR)}, need 1")
    return errs


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--revert", action="store_true")
    a = ap.parse_args()

    if a.revert:
        rc = 0
        for p in (ENV_SH, RUN_SH):
            baks = sorted(glob.glob(p + ".bak-nccl-*"))
            if not baks:
                print(f"no backup for {p}")
                rc = 1
                continue
            shutil.copy2(baks[-1], p)
            print(f"restored {p} from {baks[-1]}")
        return rc

    env, run = read(ENV_SH), read(RUN_SH)
    errs = problems(env, run)
    for e in errs:
        print("BLOCKED:", e)
    if errs:
        return 1

    new_env = env.replace(ENV_ANCHOR, ENV_ANCHOR + ENV_ADD, 1)
    new_run = run.replace(RUN_ANCHOR, RUN_ADD, 1).replace(CMD_ANCHOR, CMD_NEW, 1)

    if a.check:
        print("checks PASS — would add ENABLE_NCCL_NVLS/ENABLE_SYMM_MEM and ${COMM_ARGS}")
        return 0

    for p in (ENV_SH, RUN_SH):
        shutil.copy2(p, p + SUFFIX)
        print("backup:", p + SUFFIX)
    open(ENV_SH, "w", encoding="utf-8").write(new_env)
    open(RUN_SH, "w", encoding="utf-8").write(new_run)

    assert "ENABLE_NCCL_NVLS" in read(ENV_SH)
    assert "${COMM_ARGS}" in read(RUN_SH)
    print("APPLIED — verify with: bash 10-docker-run-dspark.sh --dry-run start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
