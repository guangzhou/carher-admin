#!/usr/bin/env python3
"""refresh_seed_token.py —— 给**已存在**的 lane 刷 web seed 的登录态（不克隆、不建模型行）。

为什么单独有这个脚本：`clone_web_fc_lane_v2.py --live-from-ws` 只在**克隆新 lane** 时跑
step0 刷 seed；老 lane 的 seed bearer 被上游作废时，没有一条不带副作用的路子去刷它
（跑克隆脚本会顺带去建模型行/挂池/改 key）。这里只复用它的 `refresh_seed_from_ws`，
**逐字复用不重写**，然后 rollout restart 让 lane 读到新 seed。

⚠️ 判据纪律：**seed JWT 的 exp 在未来 ≠ 上游还认它**。2026-09-02 实测 lane 83 与 84 的
seed 是同一批抓的、exp 都是 09-04，83 被上游判 `token_expired` 而 84 正常。所以
「exp 还没到」不能当活证，刷完必须跑 `lane_live_probe.py` 复测。

用法：
  python3 refresh_seed_token.py 81 83          # 刷 + 重启
  python3 refresh_seed_token.py 81 --no-restart
刷完必跑：
  ssh cltx@10.68.13.198 'cd <dir> && python3 lane_live_probe.py --control cr-g-5.6-82 cursor-g-81-5.6-sol …'
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clone_web_fc_lane_v2 as C   # noqa: E402  （顶层只有常量+函数，import 无副作用）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("accts", nargs="+", help="lane 号，如 81 83")
    ap.add_argument("--no-restart", action="store_true")
    a = ap.parse_args()

    for acct in a.accts:
        print("=" * 60, "lane", acct)
        # 内含：ssh 到 standby 先 cp 一份 /Data/backups/zero-N-users-pre-livetoken-<ts>.json
        # token 经 ssh stdin 传，不过 argv（防 ps 泄漏）
        # ⚠️ 返回值必须门住：seed 没写成功就重启 = 把「旧 token 带病服务」变成 CrashLoop
        # （pod 启动即 sentinel 握手，失败直接 `[fatal] failed to start` 退出）。
        # 2026-09-02 lane 82 实踩：写盘 PermissionError 被吞，照样 restart，pod 再没起来。
        if not C.refresh_seed_from_ws(acct):
            print("!! lane %s seed 没刷成功 —— 不重启，保持现状。先按上面的 stderr 修。" % acct)
            return 1
        if a.no_restart:
            print("  （--no-restart：seed 已改，但 lane 还没重读，此刻不算生效）")
            continue
        dep = "zero-cursor-bpi-%s" % acct
        out, err, rc = C.kubectl("rollout restart deploy/%s" % dep)
        print(" ", (out or err).strip()[:200])
        out, err, rc = C.kubectl("rollout status deploy/%s --timeout=180s" % dep)
        print(" ", (out or err).strip()[:200])
        if rc != 0:
            print("!! rollout 未就绪，停手")
            return 1
    print("\n⚠️ 还没完：seed 换了不等于上游认。现在去跑 lane_live_probe.py 复测，"
          "带上一个已知好的 --control 做阳性对照。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
