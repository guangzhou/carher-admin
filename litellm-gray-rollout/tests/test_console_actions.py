"""动作层：白名单、驾驶锁、参数校验。用临时 run 目录 + 假脚本，不碰真集群。"""
from __future__ import annotations

import fcntl
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "console"))

import actions  # noqa: E402


def _rd(tmp_path, monkeypatch):
    rd = tmp_path / "run"
    rd.mkdir()
    (rd / "step.sh").write_text("#!/bin/bash\necho step \"$@\"\n")
    ptr = tmp_path / "ptr"
    ptr.write_text(str(rd))
    monkeypatch.setenv("GRAY_RUN_POINTER", str(ptr))
    actions._running.clear()
    return rd


def _wait():
    for _ in range(50):
        if all(x["done"] for x in actions.running().values()):
            return
        time.sleep(0.05)


def test_unknown_action_rejected(tmp_path, monkeypatch):
    _rd(tmp_path, monkeypatch)
    assert actions.start("rm_rf", None)["ok"] is False


def test_ramp_only_allowed_levels(tmp_path, monkeypatch):
    _rd(tmp_path, monkeypatch)
    assert actions.start("ramp", 37)["ok"] is False


def test_ramp_calls_step_with_zero_dwell_and_logs(tmp_path, monkeypatch):
    rd = _rd(tmp_path, monkeypatch)
    res = actions.start("ramp", 5, who="machine")
    assert res["ok"]
    _wait()
    assert "step 5 0" in Path(res["log"]).read_text()
    kinds = [json.loads(l)["kind"] for l in (rd / "console-events.jsonl").read_text().splitlines()]
    assert kinds[:2] == ["action_start", "action_end"]


def test_lock_held_by_someone_else_refuses(tmp_path, monkeypatch):
    rd = _rd(tmp_path, monkeypatch)
    f = open(rd / ".console.lock", "a+")
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = actions.start("ramp", 5)
        assert res["ok"] is False and "接管" in res["error"]
        assert actions.lock_holder_busy() is True
    finally:
        f.close()
    assert actions.lock_holder_busy() is False


def test_tail_log_refuses_paths_outside_run(tmp_path, monkeypatch):
    _rd(tmp_path, monkeypatch)
    assert actions.tail_log("/etc/passwd") == ""


def test_failed_auto_ramp_is_not_retried_in_same_generation(tmp_path, monkeypatch):
    """10-17 沙盒回放发现：step.sh 结束但 split 没变时，驾驶台每分钟重敲一次。现在同一 generation 只试一次。"""
    rd = _rd(tmp_path, monkeypatch)
    (rd / "step.sh").write_text("#!/bin/bash\necho STOP: capacity; exit 1\n")
    import server  # noqa: E402

    assert server._last_auto_attempt("g1") is None
    res = actions.start("ramp", 50, who="machine", generation="g1")
    assert res["ok"]
    _wait()
    time.sleep(0.1)
    last = server._last_auto_attempt("g1")
    assert last and last["rc"] == 1 and "STOP" in last["tail"]
    assert server._last_auto_attempt("g2") is None  # 新 generation（真升档了）重新计
