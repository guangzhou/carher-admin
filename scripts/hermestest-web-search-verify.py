#!/usr/bin/env python3
"""hermestest 实例 web_search provider 的分层回归量具。

在 Mac 上跑，经 scripts/jms 打到 188，一次 ssh 往返跑完全部层。

分层（编号与 skill hermestest-web-search-provider 一致）：
  L0  阳性对照 —— `openclaw config get gateway` 必须读得到。
      读不到 ⇒ 尺子坏了（几乎总是 docker exec 的 HOME=/root），
      **立刻 exit 3，不解读任何业务层**。
  L1  配置装载 —— config validate + 读回 plugins.entries.<plugin>.config
      + gateway 热重载日志里带路径的那行。
  L2  插件内实打 —— openclaw infer web search --query，断言返回 JSON 的
      provider / model 与期望一致。**这是唯一能证明 ${VAR} 插值成功的层**：
      config get 会把 secret 打成 __OPENCLAW_REDACTED__，看不出插值。
  L3  容器健康 + 既存功能没被带坏。
  L4  真实 agent turn 的 dispatch 区间 —— 只输出区间，判定要人拿上游调用
      日志来套。**这一层永远是 MANUAL，脚本不给绿。**

基线：先 --save-baseline 存一份（改动前），改动后带 --baseline 比对，
只把 NEWLY BROKEN 判红 —— 否则既存故障会落进同一个时间窗，
形状和「我搞坏了」一模一样。

用法：
  # 改动前
  scripts/hermestest-web-search-verify.py --save-baseline /tmp/h14.base.json
  # 改动后
  scripts/hermestest-web-search-verify.py --baseline /tmp/h14.base.json
  # 只跑到 L2、换实例、换期望模型名
  scripts/hermestest-web-search-verify.py --instance hermestest-13 \
      --expect-model perplexity-sonar --max-layer 2

退出码：0 全过（L4 除外，它不参与）/ 1 有层判红 / 3 尺子坏了 / 4 调用失败
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
JMS = os.path.join(HERE, "jms")

# L1 期望读回的三个键，来自插件 openclaw.plugin.json#configSchema
# （additionalProperties:false，多一个键就是无效配置）
EXPECT_KEYS = {"apiKey", "baseUrl", "model"}

MARK = "@@SEC@@"


def build_remote(a: argparse.Namespace) -> str:
    """一次往返跑完，各段用 MARK 分节；远端不做判定，只取证。"""
    # --no-home-fix 是给 L0 探测器做合成红的：故意不带 -e HOME=/data，
    # L0 必须报红并 exit 3。探测器自己报不出红，它就不是量具。
    home = "" if a.no_home_fix else "-e HOME=/data"
    return f"""
set -u
C={a.instance}
X="docker exec {home} $C"

echo "{MARK}L0"
$X openclaw config get gateway 2>&1 | head -20

echo "{MARK}L1_VALIDATE"
$X openclaw config validate 2>&1 | tail -40

echo "{MARK}L1_READBACK"
$X openclaw config get plugins.entries.{a.plugin}.config 2>&1 | head -20

echo "{MARK}L1_RELOAD"
docker logs $C 2>&1 | grep -F 'config hot reload applied' | tail -5
docker logs $C 2>&1 | grep -F 'config reload skipped' | tail -3

echo "{MARK}L2"
$X timeout {a.timeout} openclaw infer web search --query {json.dumps(a.query)} 2>&1 | grep -E '^\\{{"result"' | tail -1

echo "{MARK}L3_STATE"
docker inspect -f 'running={{{{.State.Running}}}} restarts={{{{.RestartCount}}}} health={{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{else}}}}none{{{{end}}}}' $C 2>&1
echo ""

echo "{MARK}L3_HEALTH"
docker exec $C sh -c 'curl -s --max-time 8 127.0.0.1:{a.gateway_port}/health' 2>&1
echo ""

echo "{MARK}L3_ERRORS"
docker logs --since {a.window} $C 2>&1 \
  | grep -oE '(Request was aborted|AbortError|PortInUseError|Cannot find module .[a-z0-9.@/-]+.|MissingEnvVarError|reload skipped)' \
  | sort | uniq -c | sort -rn

echo "{MARK}L3_LANE"
docker logs --since {a.window} $C 2>&1 | grep -cF 'received message from'

echo "{MARK}L4"
docker logs --since {a.window} $C 2>&1 \
  | grep -E 'received message from|dispatching to agent|dispatch complete' \
  | tail -40
"""


def run_remote(a: argparse.Namespace) -> dict[str, str]:
    if not os.access(JMS, os.X_OK):
        sys.exit(f"[FATAL] 找不到可执行的 {JMS}")
    for attempt in range(1, a.retries + 1):
        p = subprocess.run(
            [JMS, "ssh", a.asset, "bash -s"],
            input=build_remote(a), capture_output=True, text=True, timeout=a.timeout + 180,
        )
        if MARK in p.stdout:
            break
        # jms 有已知的偶发 Permission denied(password,publickey)，原样重试
        sys.stderr.write(f"[warn] jms 第 {attempt} 次没拿到分节输出，重试\n")
        time.sleep(4)
    else:
        sys.exit(4)

    out: dict[str, str] = {}
    cur = None
    for line in p.stdout.splitlines():
        if line.startswith(MARK):
            cur = line[len(MARK):].strip()
            out[cur] = ""
        elif cur:
            out[cur] += line + "\n"
    return out


def errsig(block: str) -> dict[str, int]:
    """把 `   3 Request was aborted` 这种 uniq -c 输出变成 {签名: 次数}。"""
    sig = {}
    for line in block.splitlines():
        m = re.match(r"\s*(\d+)\s+(.*\S)", line)
        if m:
            sig[m.group(2)] = int(m.group(1))
    return sig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="JSZX-AI-03", help="jms 资产名（188）")
    ap.add_argument("--instance", default="hermestest-14")
    ap.add_argument("--plugin", default="perplexity")
    ap.add_argument("--expect-provider", default="perplexity")
    ap.add_argument("--expect-model", default="perplexity-sonar",
                    help="必须等于网关侧的组名，不是上游真名")
    ap.add_argument("--query", default=None,
                    help="必须是离线绝对答不出的当期问题；默认自带 nonce")
    ap.add_argument("--gateway-port", type=int, default=18789,
                    help="容器内端口。宿主映射是 29131，在容器里打它返 000")
    ap.add_argument("--window", default="40m", help="docker logs --since 窗口")
    ap.add_argument("--timeout", type=int, default=150)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--max-layer", type=int, default=4)
    ap.add_argument("--save-baseline", metavar="FILE")
    ap.add_argument("--baseline", metavar="FILE")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-home-fix", action="store_true",
                    help="自检用：故意不带 -e HOME=/data，L0 必须报红 exit 3")
    a = ap.parse_args()

    if a.query is None:
        nonce = f"nonce-{int(time.time()) % 100000:05d}"
        a.query = f"{nonce} 今天是 {time.strftime('%Y年%m月%d日')}，用一句话说今天的一条国际新闻"

    sec = run_remote(a)
    res: dict[str, dict] = {}

    # ── L0 阳性对照：一切判定的前置 ────────────────────────────────
    l0 = sec.get("L0", "")
    l0_ok = bool(l0.strip()) and "not found" not in l0.lower()
    res["L0"] = {"ok": l0_ok, "detail": l0.strip()[:200]}
    if not l0_ok:
        print("[L0] ✗ 阳性对照失败：`config get gateway` 读不到")
        print("     ⇒ 尺子坏了，不是功能坏了。几乎总是 docker exec 给的 HOME=/root。")
        print("     ⇒ 正确调用 docker exec -e HOME=/data <容器> openclaw ...")
        print(f"     原样输出：{l0.strip()[:300]}")
        return 3
    print("[L0] ✓ 阳性对照通过（CLI 读到配置了，后面的红才可信）")

    # ── L1 配置装载 ───────────────────────────────────────────────
    val = sec.get("L1_VALIDATE", "")
    # validate 自己声明 `N warning(s):`，然后每条以 `!` 起（不是 `-`，我第一版
    # 照 infer 的告警框写成 `-` 数出 0 —— 会数东西的量具必须先断言自己数得到）
    declared = int(m.group(1)) if (m := re.search(r"(\d+)\s+warning\(s\)", val)) else 0
    warns = re.findall(r"^\s*!\s+([A-Za-z][\w.<>/@-]*?):", val, re.M)
    extractor_ok = declared == len(warns)
    valid = "Config valid" in val

    rb = sec.get("L1_READBACK", "")
    try:
        keys = set(json.loads(rb).get("webSearch", {}))
    except Exception:
        keys = set()

    # 🔴 `reload skipped` 不能只看「日志里有没有」—— 整条日志史里任何一次历史
    # 失败都会让它永久判红（我第一版就是这样：12:35 那次写错路径的 skipped
    # 让改对之后的回归照样红）。只有「最近一次 reload 事件是 skipped」才是红。
    def last_ts(pat: str) -> str:
        hits = [l for l in sec.get("L1_RELOAD", "").splitlines() if pat in l]
        return hits[-1][:29] if hits else ""

    ts_applied, ts_skipped = last_ts("hot reload applied"), last_ts("reload skipped")
    reload_ok = bool(ts_applied)
    # 同一 tz 的 ISO 串，字典序即时间序
    skipped_is_latest = bool(ts_skipped) and ts_skipped > ts_applied
    res["L1"] = {"ok": valid and keys == EXPECT_KEYS and not skipped_is_latest
                 and extractor_ok,
                 "valid": valid, "warn_declared": declared, "warn_paths": warns,
                 "extractor_ok": extractor_ok,
                 "readback_keys": sorted(keys),
                 "last_hot_reload": ts_applied, "last_reload_skipped": ts_skipped,
                 "skipped_is_latest": skipped_is_latest}
    print(f"[L1] {'✓' if res['L1']['ok'] else '✗'} validate={valid} "
          f"readback_keys={sorted(keys) or '空'} hot_reload={reload_ok} "
          f"warnings={declared}（提取到 {len(warns)}）")
    if not extractor_ok:
        print(f"     ✗ 提取器坏了：validate 声明 {declared} 条告警，我只提取到 "
              f"{len(warns)} 条 ⇒ 别读下面任何告警结论，先修正则")

    if keys and keys != EXPECT_KEYS:
        print(f"     ⚠️ 键集合不是 {sorted(EXPECT_KEYS)} —— configSchema 是 "
              f"additionalProperties:false，多一个键整块无效")
    if skipped_is_latest:
        print(f"     ✗ 最近一次 reload 事件是 skipped（{ts_skipped} 晚于 {ts_applied or '无'}）"
              f" ⇒ 你的改动根本没进去，别去查业务侧")
    elif ts_skipped:
        print(f"     · 历史上有过 skipped（{ts_skipped}），但之后 {ts_applied} 已 applied，不判红")


    # ── L2 插件内实打（唯一能证明 ${VAR} 插值成功的层）───────────────
    if a.max_layer >= 2:
        raw = sec.get("L2", "").strip()
        got = {}
        try:
            got = json.loads(raw).get("result", {})
        except Exception:
            pass
        body = (got.get("content") or "")
        ok = (got.get("provider") == a.expect_provider
              and got.get("model") == a.expect_model and len(body) > 40)
        res["L2"] = {"ok": ok, "provider": got.get("provider"), "model": got.get("model"),
                     "tookMs": got.get("tookMs"), "content_len": len(body),
                     "citations": len(got.get("citations") or [])}
        print(f"[L2] {'✓' if ok else '✗'} provider={got.get('provider')} "
              f"model={got.get('model')} tookMs={got.get('tookMs')} 正文={len(body)}字")
        if not ok and not got:
            print(f"     原样输出：{raw[:300] or '（空）'}")
            print("     ⚠️ 空输出先查调用形状：是 `infer web search --query <文本>`，"
                  "裸位置参数会报 Missing required option 并 exit 0（假绿）")
        if ok:
            print("     ↑ 这一层过了才说明 ${VAR} 插值成功 —— "
                  "config get 会把 secret 打成 __OPENCLAW_REDACTED__，验不了插值")

    # ── L3 容器健康 + 既存故障 ────────────────────────────────────
    if a.max_layer >= 3:
        st = sec.get("L3_STATE", "").strip()
        hl = sec.get("L3_HEALTH", "").strip()
        cur = errsig(sec.get("L3_ERRORS", ""))
        lane = (sec.get("L3_LANE", "").strip() or "0").splitlines()[0]
        # 🔴 没有基线时，「既存」和「新增」在数据上无法区分 ⇒ 一律不判红。
        # 第一版把 base={} 当基线，结果把 4 次既存 abort 判成 NEWLY BROKEN，
        # 同时还打了「一律不判红」—— 自相矛盾的输出比假红更坏。
        newly: dict[str, int] = {}
        worse: dict[str, tuple[int, int]] = {}
        if a.baseline:
            with open(a.baseline) as f:
                base = json.load(f).get("errors", {})
            newly = {k: v for k, v in cur.items() if k not in base}
            worse = {k: (base[k], v) for k, v in cur.items() if k in base and v > base[k]}
        ok = ("running=true" in st.lower() and "restarts=0" in st
              and '"ok":true' in hl.replace(" ", "") and not newly and not worse)
        res["L3"] = {"ok": ok, "state": st, "health": hl[:120], "lane_msgs": lane,
                     "errors": cur, "baseline_used": bool(a.baseline),
                     "newly_broken": newly, "got_worse": worse}
        print(f"[L3] {'✓' if ok else '✗'} {st} health={hl[:60]} 飞书收消息={lane}条")
        if not a.baseline and cur:
            print(f"     ⚠️ 没给 --baseline ⇒ 无法区分既存/新增，下列一律不判红："
                  f"{cur}")

        for k, v in newly.items():
            print(f"     ✗ NEWLY BROKEN: {k} ×{v}")
        for k, (b, c) in worse.items():
            print(f"     ✗ 变多了: {k} {b} → {c}")

    # ── L4 真实 turn 的 dispatch 区间：永远 MANUAL ──────────────────
    if a.max_layer >= 4:
        lines = [l for l in sec.get("L4", "").splitlines() if l.strip()]
        res["L4"] = {"ok": None, "lines": len(lines)}
        print(f"[L4] — MANUAL：{len(lines)} 行 dispatch 痕迹。"
              f"判据不是「上游出现新调用」，是「每条调用都落在 "
              f"dispatching to agent → dispatch complete 的区间内」。")
        for l in lines[-12:]:
            print("     " + l[:160])

    if a.save_baseline:
        with open(a.save_baseline, "w") as f:
            json.dump({"instance": a.instance, "ts": time.strftime("%FT%T%z"),
                       "errors": errsig(sec.get("L3_ERRORS", "")),
                       "warn_paths": warns}, f, ensure_ascii=False, indent=2)
        print(f"[baseline] 已存 {a.save_baseline} —— 改动后带 --baseline 比对，"
              f"只把 NEWLY BROKEN 判红")

    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))

    graded = [v["ok"] for v in res.values() if v.get("ok") is not None]
    return 0 if all(graded) else 1


if __name__ == "__main__":
    sys.exit(main())
