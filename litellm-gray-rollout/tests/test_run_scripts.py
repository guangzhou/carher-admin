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
