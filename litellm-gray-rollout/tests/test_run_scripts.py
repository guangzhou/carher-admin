"""run/ 操作层脚本的静态门禁：把 2026-10-17 夜里踩过的四个坑钉住，防止以后改脚本时改丢。"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

RUN = Path(__file__).resolve().parents[1] / "scripts" / "run"
SCRIPTS = sorted(RUN.glob("*.sh"))


def _read(name: str) -> str:
    return (RUN / name).read_text(encoding="utf-8")


def test_all_scripts_parse():
    assert SCRIPTS, "run/ 下没有脚本"
    for path in SCRIPTS:
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_no_run_specific_hardcoding():
    # 签字人、版本 digest、具体 run 目录都必须从 env / target.env 读
    for path in SCRIPTS:
        body = path.read_text(encoding="utf-8")
        code = "\n".join(line for line in body.splitlines() if not line.lstrip().startswith("#"))
        assert "刘国现" not in code, path.name
        assert not re.search(r"sha256:[0-9a-f]{64}", code), path.name
        assert ".up1051-latest" not in code, path.name


def test_ramp_fails_when_split_did_not_change():
    # 10-17：gate 拒了但 ramp.sh 照样 exit 0，step 链继续往 100 走
    assert 'STOP: split 未变成' in _read("ramp.sh")


def test_capacity_evidence_is_regenerated_and_bound_to_this_run():
    step = _read("step.sh")
    # 10-17：读到 10-02 上一轮留下的 split_capacity.json，打印 PASS
    assert "rm -f $EV/split_capacity.json" in step
    assert "d['run_id']=='$RUN'" in step and "d['generation']=='$GEN'" in step
    # 默认单容器并发 8 远低于实测；必须显式给
    assert "GRAY_PER_CONTAINER_CONCURRENCY:?" in step


def test_step_stops_on_auto_rollback():
    assert "phase=rolled_back" in _read("step.sh")


def test_converge_r10_skips_terminating_pods():
    # 10-17：R10 把还在优雅退出的旧 pod 算进去，报 4 个 BAD
    assert "deletionTimestamp" in _read("converge.sh")


# ---- 10-26 v1.106.4 收进 run/ 的四个脚本：把当晚踩的坑钉住 ----

def test_capacity_script_matches_step_and_needs_concurrency():
    cap = _read("capacity.sh")
    # 与 step.sh ≥50 档同一组参数；并发没有默认值
    for frag in ("--headroom-fraction 0.3", "--window-seconds 7200", "impute-class-cost-from-stable", "GRAY_PER_CONTAINER_CONCURRENCY:?"):
        assert frag in cap and frag in _read("step.sh"), frag
    assert "d['generation']=='$GEN'" in cap


def test_clone_qualify_serial_and_bare_digest():
    cq = _read("clone-qualify.sh")
    # post-mig dump 必须在 restore B/C 之前且分开等完成（10-26 同批 apply ⇒ B/C 读不到 sha256）
    order = ["run1 litellm-clone-a-post-migration-dump", "run1 litellm-clone-b-restore", "run1 litellm-clone-c-restore"]
    pos = [cq.index(x) for x in order]
    assert pos == sorted(pos)
    # GRAY_IMAGE_DIGEST 写裸 digest，不是全引用
    assert 'e["value"]=old if "old" in n else new' in cq
    assert "DUMP_LIMIT_BYTES" in cq and "RUNNER_SHA" in cq


def test_prod_migrate_guards():
    pm = _read("prod-migrate.sh")
    assert "export LC_ALL=C" in pm
    assert "</dev/null" in pm                       # kubectl exec 不吃 bash -s 的 stdin
    assert "pg_blocking_pids" in pm and "pg_cancel_backend" in pm and "pg_terminate_backend" not in pm
    assert "application_name='psql'" in pm          # 只 cancel 人手跑的只读查询
    for k in ("rewritten=", "cols_lost=", "idx_lost=", "tables_lost=", "invalid_idx=", "failed_rows="):
        assert k in pm


def test_gates_never_auto_approve_unknown_shape_or_service_deletion():
    g = _read("gates.sh")
    assert "固定口径之外的形状变化，不自动批" in g
    assert '"/Service/" in x["object"]' in g


def test_snap_from_live_uses_prev_shape_and_live_prod_cms():
    s = (RUN / "snap-from-live.py").read_text(encoding="utf-8")
    assert "--prev-shape" in s and '"litellm-proxy"' in s
    assert "PROXY_BATCH_POLLING_ENABLED" in s      # gray 独有 env 里这条不能从 live 抄
    assert "列表顺序与 live gray 不一致" in s
