#!/usr/bin/env python3
"""定时停掉「当前挂着 STUDENT 号」的槽，先消耗其他档位的号；生效槽剩到 FLOOR（默认 8）个就不再停。

用户 11-01 定：每 30 分钟查一次；STUDENT 号在槽上 ⇒ 停这个槽；最后剩 8 个生效槽就不再停槽。
198 上 cltx 的 crontab 跑（见 skill kiro-rs-slots §1.3）：

    */30 * * * * /usr/bin/python3 /home/cltx/kiro-slots-student-off.py >> /home/cltx/kiro-student-off/run.log 2>&1

    python3 kiro-slots-student-off.py --dry-run     # 只打印会停哪些，不写
    python3 kiro-slots-student-off.py --floor 8     # 默认 8

规则：
- 只动「生效 + 当前号档位含 STUDENT」的槽；只改 enabled=false，别的字段原样带回（密码栏是打码，服务端按槽名还原）。
- 能停的个数 = 生效槽数 − FLOOR；不够停全部时，先停在途 0、绑定人数少的（少打断人）。
- 写之前再读一次配置，和规划时不一致（有人在页面上保存）⇒ 这轮跳过；每次写前备份到 ~/kiro-student-off/。
- 同一时刻只跑一个（flock）。每轮打一行：OK / STOP / FLOOR + 原因。
"""
import argparse, fcntl, json, os, sys, time, urllib.error, urllib.request

B = "http://127.0.0.1:30891/api/admin"
HOME = os.path.expanduser("~")
DIR = os.path.join(HOME, "kiro-student-off")


def ak() -> str:
    return open(os.path.join(HOME, "kiro-us-egress/.ak")).read().strip()


def api(method, path, body=None):
    req = urllib.request.Request(B + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"x-api-key": ak(), "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def is_student(s: dict) -> bool:
    return "STUDENT" in (s.get("accountTier") or "").upper()


def plan(running: list, floor: int) -> tuple[list, list]:
    """返回 (要停的槽名, 因为到底线没停的槽名)"""
    cand = sorted((s for s in running if is_student(s)), key=lambda s: (s.get("inFlight", 0), s.get("users", 0), s["name"]))
    room = max(0, len(running) - floor)
    return [s["name"] for s in cand[:room]], [s["name"] for s in cand[room:]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    lock = open(os.path.join(DIR, ".lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"{time.strftime('%F %T')} SKIP 上一轮还在跑"); return 0
    ts = time.strftime("%F %T")
    st, snap = api("GET", "/slots")
    if st != 200 or not isinstance(snap, dict):
        print(f"{ts} STOP 读 /slots 失败 {st}"); return 1
    running = snap["slots"]
    stu = [s["name"] for s in running if is_student(s)]
    off, kept = plan(running, a.floor)
    head = f"{ts} 生效 {len(running)} · STUDENT 在槽 {len(stu)} · 冷备 {snap.get('spare')}"
    if not off:
        tag = "FLOOR" if kept else "OK"
        print(f"{head} · {tag} 不停" + (f"（到底线 {a.floor}，留着：{', '.join(kept)}）" if kept else "")); return 0
    st, cfg = api("GET", "/slots/config")
    if st != 200 or not isinstance(cfg, list):
        print(f"{head} · STOP 读配置失败 {st}"); return 1
    if not all(r.get("proxyPassword") in (None, "", "******") for r in cfg):
        print(f"{head} · STOP 配置里出现未打码的密码，不写"); return 1
    new = [dict(r, enabled=False) if r["name"] in off and r["enabled"] else r for r in cfg]
    if a.dry_run:
        print(f"{head} · DRY 会停 {off}" + (f"；到底线留着 {kept}" if kept else "")); return 0
    bk = os.path.join(DIR, f"slots-config-before-{time.strftime('%Y%m%dT%H%M%S')}.json")
    json.dump(cfg, open(bk, "w"), ensure_ascii=False); os.chmod(bk, 0o600)
    if api("GET", "/slots/config")[1] != cfg:
        print(f"{head} · SKIP 配置刚被改过，下一轮再看"); return 0
    st, j = api("PUT", "/slots/config", new)
    if st != 200:
        print(f"{head} · STOP PUT {st} {j}"); return 1
    after = api("GET", "/slots")[1]
    left = [s["name"] for s in after["slots"] if is_student(s)]
    print(f"{head} · OK 停了 {off} ⇒ 生效 {len(after['slots'])}、STUDENT 在槽 {len(left)}" + (f"（到底线留着 {kept}）" if kept else "") + f" · 备份 {bk}")
    # 只留最近 50 份备份
    bks = sorted(f for f in os.listdir(DIR) if f.startswith("slots-config-before-"))
    for f in bks[:-50]:
        os.remove(os.path.join(DIR, f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
