"""carher_video.js 离线回归(11-01 加):本机假网关模拟 OpenAI 视频三段式,不打真实网关、不扣额度。

为什么要测排队锁:🔴 同一把 key 同时提交两个视频,网关只能取回一个(另一个轮询 404,额度照扣)
⇒ 工具用本机目录锁排队。锁错了的后果是静默多扣额度,用户不会来报 ⇒ 用假网关看两次 POST 是否在时间上错开。
隔离:CARHER_IMAGE_KEY/BASE 指向假网关,CARHER_VIDEO_LOCK_DIR 指向 tmp_path,HOME 也指向 tmp_path,碰不到真实 ~/.carher。
"""
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parent.parent / "src" / "carher_video.js"
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 4096


class FakeGateway:
    """mode 控制失败形态;events 记 (时刻, 事件, 任务 id)。"""

    def __init__(self, mode="ok", polls=2):
        self.mode, self.polls = mode, polls
        self.events, self.posts, self.lock = [], [], threading.Lock()
        self.jobs = {}
        gw = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode()
                elif isinstance(body, str):
                    body = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with gw.lock:
                    gw.posts.append({"auth": self.headers.get("Authorization"), **body})
                    jid = "vid_%d" % len(gw.posts)
                    gw.jobs[jid] = 0
                    gw.events.append((time.monotonic(), "post", jid))
                if self.path != "/v1/videos":
                    return self._send(404, {"error": {"message": "no route " + self.path}})
                if gw.mode in (401, 403, 429):
                    msg = "a video is already being generated" if gw.mode == 429 else "denied"
                    return self._send(gw.mode, {"error": {"message": msg}})
                self._send(200, {"id": jid, "object": "video", "status": "queued", "progress": 0})

            def do_GET(self):
                parts = self.path.strip("/").split("/")   # v1 videos <id> [content]
                jid = parts[2] if len(parts) >= 3 else ""
                if jid not in gw.jobs:
                    return self._send(404, {"error": {"message": "video not found"}})
                if len(parts) == 4 and parts[3] == "content":
                    with gw.lock:
                        gw.events.append((time.monotonic(), "content", jid))
                    if gw.mode == "html":
                        return self._send(200, "<html>oops</html>" * 100, "text/html")
                    return self._send(200, MP4, "video/mp4")
                if gw.mode == "poll404":                   # 同 key 并发时另一个任务的真实形态
                    return self._send(404, {"error": {"message": "video not found"}})
                with gw.lock:
                    gw.jobs[jid] += 1
                    n = gw.jobs[jid]
                if gw.mode == "failed" and n >= 1:
                    return self._send(200, {"id": jid, "status": "failed",
                                            "error": {"code": "generation_failed", "message": "上游拒绝了这个描述"}})
                if n >= gw.polls:
                    return self._send(200, {"id": jid, "status": "completed", "progress": 100})
                self._send(200, {"id": jid, "status": "in_progress", "progress": 50})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = "http://127.0.0.1:%d/v1" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def gw_factory():
    made = []

    def make(**kw):
        g = FakeGateway(**kw)
        made.append(g)
        return g

    yield make
    for g in made:
        g.close()


def env_for(tmp_path, gw, wait_ms=60000):
    return {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "USERPROFILE": str(tmp_path),
            "CARHER_IMAGE_KEY": "sk-test", "CARHER_IMAGE_BASE": gw.base + "/",
            "CARHER_VIDEO_LOCK_DIR": str(tmp_path / "lockdir"), "CARHER_VIDEO_POLL_MS": "100",
            "CARHER_VIDEO_WAIT_MS": str(wait_ms)}


def run(tmp_path, gw, *args, cwd=None, wait_ms=60000):
    return subprocess.run(["node", str(TOOL), *args], capture_output=True, text=True,
                          env=env_for(tmp_path, gw, wait_ms), cwd=cwd or tmp_path, timeout=90)


def spawn(tmp_path, gw, *args, wait_ms=60000):
    return subprocess.Popen(["node", str(TOOL), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env_for(tmp_path, gw, wait_ms), cwd=tmp_path)


def jobs(tmp_path):
    d = tmp_path / "lockdir" / "video-jobs"
    return [json.loads(f.read_text()) for f in sorted(d.glob("*.json"))] if d.exists() else []


def wait_for(cond, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def last_line(r):
    return r.stdout.strip().splitlines()[-1]


def generated_path(r):
    line = last_line(r)
    assert line.startswith("已生成:"), r.stdout + r.stderr
    return Path(line[len("已生成:"):].split("(")[0])


def test_success_full_flow(tmp_path, gw_factory):
    gw = gw_factory(polls=3)
    r = run(tmp_path, gw, "一只猫在弹钢琴", "out.mp4")
    assert r.returncode == 0, r.stderr
    p = generated_path(r)
    assert p == tmp_path / "out.mp4" and p.read_bytes() == MP4 and p.read_bytes()[4:8] == b"ftyp"
    post = gw.posts[0]
    assert post == {"auth": "Bearer sk-test", "model": "gemini-video", "prompt": "一只猫在弹钢琴", "size": "1280x720"}
    assert len(gw.posts) == 1 and "已提交" in r.stdout
    assert not (tmp_path / "lockdir" / "video.lock").exists()   # 正常结束要放锁


def test_portrait_sends_720x1280(tmp_path, gw_factory):
    gw = gw_factory()
    for flag in ("--portrait", "--竖屏"):
        r = run(tmp_path, gw, flag, "竖屏视频", "v_" + flag.strip("-"))
        assert r.returncode == 0, r.stderr
    assert [p["size"] for p in gw.posts] == ["720x1280", "720x1280"]
    assert [p["prompt"] for p in gw.posts] == ["竖屏视频", "竖屏视频"]   # flag 不能混进描述/路径
    assert (tmp_path / "v_portrait.mp4").exists() and (tmp_path / "v_竖屏.mp4").exists()


@pytest.mark.parametrize("arg,expect", [
    (None, None),                 # 不给 ⇒ 当前目录自动起名
    (".", None),
    ("sub/new/", "sub/new"),      # 结尾斜杠 + 不存在 ⇒ 建目录再起名
    ("deep/clip", "deep/clip.mp4"),   # 无后缀 ⇒ 补 .mp4;父目录不存在要先建好(否则白扣一份)
    ("a.MP4", "a.MP4"),
])
def test_output_path_forms(tmp_path, gw_factory, arg, expect):
    gw = gw_factory()
    r = run(tmp_path, gw, "描述", *([arg] if arg else []))
    assert r.returncode == 0, r.stderr
    p = generated_path(r)
    assert p.is_absolute() and p.read_bytes() == MP4
    if expect is None or expect.endswith("new"):
        parent = tmp_path / (expect or "")
        assert p.parent == parent and p.name.startswith("video-") and p.suffix == ".mp4"
    else:
        assert p == tmp_path / expect


def test_same_dir_same_second_does_not_overwrite(tmp_path, gw_factory):
    gw = gw_factory(polls=1)
    a, b = spawn(tmp_path, gw, "一", "."), spawn(tmp_path, gw, "二", ".")
    outs = [p.communicate(timeout=60) for p in (a, b)]
    assert a.returncode == 0 and b.returncode == 0, outs
    assert len(list(tmp_path.glob("video-*.mp4"))) == 2


@pytest.mark.parametrize("code,needle", [
    (403, "还没有开通视频模型 gemini-video"),
    (401, "key 已失效"),
    (429, "视频服务这会儿忙"),
])
def test_http_error_wording(tmp_path, gw_factory, code, needle):
    gw = gw_factory(mode=code)
    r = run(tmp_path, gw, "描述", "x.mp4")
    assert r.returncode == 1 and needle in r.stderr, r.stderr
    assert len(gw.posts) == 1                       # 生成类不重试
    assert not (tmp_path / "x.mp4").exists()
    assert not (tmp_path / "lockdir" / "video.lock").exists()


def test_status_failed(tmp_path, gw_factory):
    gw = gw_factory(mode="failed")
    r = run(tmp_path, gw, "描述", "x.mp4")
    assert r.returncode == 1 and "上游拒绝了这个描述" in r.stderr and "HTTP 0" not in r.stderr, r.stderr
    assert len(gw.posts) == 1 and not (tmp_path / "x.mp4").exists()


def test_poll_404_explains_concurrent_submit(tmp_path, gw_factory):
    gw = gw_factory(mode="poll404")
    r = run(tmp_path, gw, "描述", "x.mp4")
    assert r.returncode == 1 and "任务查不到了" in r.stderr and len(gw.posts) == 1, r.stderr


def test_content_not_mp4(tmp_path, gw_factory):
    gw = gw_factory(mode="html")
    r = run(tmp_path, gw, "描述", "x.mp4")
    assert r.returncode == 1 and "下载到的不是视频文件" in r.stderr, r.stderr
    assert not (tmp_path / "x.mp4").exists()
    assert len(gw.posts) == 1


def test_two_processes_serialize(tmp_path, gw_factory):
    """第二个 POST 必须在第一个 content 下载之后:同一把 key 并发提交 = 一个取不回、额度照扣。"""
    gw = gw_factory(polls=5)
    a = spawn(tmp_path, gw, "第一个", "a.mp4")
    time.sleep(0.3)
    b = spawn(tmp_path, gw, "第二个", "b.mp4")
    oa, ob = a.communicate(timeout=60), b.communicate(timeout=60)
    assert a.returncode == 0 and b.returncode == 0, (oa, ob)
    assert "排队中" in ob[0]
    ev = gw.events
    posts = [t for t, k, _ in ev if k == "post"]
    first_content = [t for t, k, j in ev if k == "content" and j == "vid_1"]
    assert len(posts) == 2 and first_content and posts[1] > first_content[-1], ev
    assert (tmp_path / "a.mp4").read_bytes() == MP4 and (tmp_path / "b.mp4").read_bytes() == MP4


def _dead_pid():
    p = subprocess.Popen(["node", "-e", ""])
    p.wait()
    return p.pid


def test_stale_lock_of_dead_pid_is_taken_over(tmp_path, gw_factory):
    lock = tmp_path / "lockdir" / "video.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(str(_dead_pid()))
    gw = gw_factory()
    r = run(tmp_path, gw, "描述", "x.mp4")
    assert r.returncode == 0, r.stderr
    assert "排队" not in r.stdout and len(gw.posts) == 1
    assert not lock.exists()


def test_no_args_usage_exit_1(tmp_path, gw_factory):
    gw = gw_factory()
    r = run(tmp_path, gw)
    assert r.returncode == 1 and "用法" in r.stderr and gw.posts == []


def test_waiters_racing_on_dead_lock_still_serialize(tmp_path, gw_factory):
    """死锁 + 几个进程同时到:各自「判死→删→mkdir」会把对方刚建的锁删掉 ⇒ 两个一起提交。
    注意:这个窗口很窄,本测试在修之前的版本上也是绿的(没复现出来),只当回归烟测,不是该竞态的证据。"""
    lock = tmp_path / "lockdir" / "video.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(str(_dead_pid()))
    gw = gw_factory(polls=3)
    procs = [spawn(tmp_path, gw, "并发%d" % i, "o%d.mp4" % i) for i in range(3)]
    outs = [p.communicate(timeout=90) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    ev = sorted(gw.events)
    # 任意时刻最多一个任务在飞:每个 post 之前,前一个任务的 content 必须已经下完
    inflight = None
    for _, kind, jid in ev:
        if kind == "post":
            assert inflight is None, ev
            inflight = jid
        elif kind == "content" and jid == inflight:
            inflight = None


# ── 11-01 新形态:后台进程 + 前台最多等一会儿 + --wait 续等 + 防重复提交 ──

def test_foreground_returns_early_and_wait_finishes(tmp_path, gw_factory):
    """App 里的 agent 等不了 2 分钟:前台到点就返回(exit 0)并给出 --wait 命令,后台照常出片。"""
    gw = gw_factory(polls=30)                      # 30 次 × 100ms ≈ 3 秒
    r = run(tmp_path, gw, "慢慢出", "slow.mp4", wait_ms=500)
    assert r.returncode == 0, r.stderr
    assert "视频还没好" in r.stdout and "--wait" in r.stdout and "不要重新提交" in r.stdout
    assert not (tmp_path / "slow.mp4").exists()
    w = run(tmp_path, gw, "--wait")
    assert w.returncode == 0, w.stderr
    assert generated_path(w) == tmp_path / "slow.mp4" and len(gw.posts) == 1


def test_wait_command_line_is_runnable(tmp_path, gw_factory):
    gw = gw_factory(polls=30)
    r = run(tmp_path, gw, "慢", "s.mp4", wait_ms=300)
    line = [l for l in r.stdout.splitlines() if "--wait" in l][-1].strip()
    jid = jobs(tmp_path)[0]["id"]
    assert line.endswith("--wait " + jid) and str(TOOL) in line
    w = run(tmp_path, gw, "--wait", jid)
    assert w.returncode == 0 and generated_path(w) == tmp_path / "s.mp4", w.stderr


def test_rerun_same_prompt_attaches_instead_of_resubmitting(tmp_path, gw_factory):
    """Codex App 里模型以为输出丢了就重跑同一条命令 —— 只能扣一份额度。"""
    gw = gw_factory(polls=30)
    run(tmp_path, gw, "同一个", "same.mp4", wait_ms=300)
    r2 = run(tmp_path, gw, "同一个", "same.mp4")
    assert r2.returncode == 0, r2.stderr
    assert "已经在生成了" in r2.stdout and generated_path(r2) == tmp_path / "same.mp4"
    r3 = run(tmp_path, gw, "同一个", "same.mp4")
    assert "刚生成过" in r3.stdout and r3.returncode == 0
    assert len(gw.posts) == 1
    r4 = run(tmp_path, gw, "同一个", "same.mp4", "--retry")
    assert r4.returncode == 0 and len(gw.posts) == 2 and gw.posts[1]["prompt"] == "同一个"


def test_rerun_after_failure_needs_retry_flag(tmp_path, gw_factory):
    gw = gw_factory(mode="failed")
    r1 = run(tmp_path, gw, "会失败", "f.mp4")
    assert r1.returncode == 1
    r2 = run(tmp_path, gw, "会失败", "f.mp4")
    assert r2.returncode == 1 and "刚失败过" in r2.stderr and "--retry" in r2.stderr
    assert len(gw.posts) == 1
    r3 = run(tmp_path, gw, "会失败", "f.mp4", "--retry")
    assert r3.returncode == 1 and len(gw.posts) == 2


def test_killing_foreground_does_not_kill_generation(tmp_path, gw_factory):
    """App 关会话 / 命令被掐 = 前台进程被杀;后台必须照样出完落盘。"""
    gw = gw_factory(polls=20)
    a = spawn(tmp_path, gw, "被掐的", "k.mp4")
    assert wait_for(lambda: gw.posts)
    a.send_signal(signal.SIGKILL)
    a.wait()
    assert wait_for(lambda: (tmp_path / "k.mp4").exists(), 30)
    assert (tmp_path / "k.mp4").read_bytes() == MP4
    assert wait_for(lambda: jobs(tmp_path)[0]["state"] == "done")
    assert not (tmp_path / "lockdir" / "video.lock").exists()


def test_killed_worker_is_reported_and_lock_taken_over(tmp_path, gw_factory):
    """后台进程被 kill -9:--wait 要报失败(不能永远等),下一条能接管锁。"""
    gw = gw_factory(polls=10_000)
    run(tmp_path, gw, "卡住的", "a.mp4", wait_ms=300)
    assert wait_for(lambda: gw.posts)
    pid = jobs(tmp_path)[0]["pid"]
    os.kill(pid, signal.SIGKILL)
    assert wait_for(lambda: not pid_alive(pid))
    # 记录里的 updated 要超过 15 秒才判死;把它改老,省得测试干等
    j = jobs(tmp_path)[0]
    j["updated"] -= 60_000
    (tmp_path / "lockdir" / "video-jobs" / (j["id"] + ".json")).write_text(json.dumps(j))
    w = run(tmp_path, gw, "--wait")
    assert w.returncode == 1 and "意外退出" in w.stderr, w.stdout + w.stderr
    gw.polls = 1
    r = run(tmp_path, gw, "下一个", "b.mp4")
    assert r.returncode == 0 and (tmp_path / "b.mp4").read_bytes() == MP4, r.stderr


def test_sigterm_worker_records_failure_and_releases_lock(tmp_path, gw_factory):
    gw = gw_factory(polls=10_000)
    run(tmp_path, gw, "描述", "a.mp4", wait_ms=300)
    assert wait_for(lambda: gw.posts)
    pid = jobs(tmp_path)[0]["pid"]
    os.kill(pid, signal.SIGTERM)
    assert wait_for(lambda: jobs(tmp_path)[0]["state"] == "failed")
    assert "被结束" in jobs(tmp_path)[0]["msg"]
    assert wait_for(lambda: not (tmp_path / "lockdir" / "video.lock").exists())


def test_live_foreign_lock_keeps_job_queued(tmp_path, gw_factory):
    lock = tmp_path / "lockdir" / "video.lock"
    lock.mkdir(parents=True)
    (lock / "owner").write_text(str(os.getpid()))   # pytest 自己 = 活着的持锁者
    gw = gw_factory()
    r = run(tmp_path, gw, "描述", "x.mp4", wait_ms=1500)
    assert "排队中" in r.stdout and gw.posts == [], r.stdout
    assert (lock / "owner").read_text() == str(os.getpid())
    shutil.rmtree(lock)                              # 前一个出完 ⇒ 排队的接着跑
    w = run(tmp_path, gw, "--wait")
    assert w.returncode == 0 and len(gw.posts) == 1, w.stderr
