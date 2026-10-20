"""用例回归：在驾驶台上看「回归哪些用例、每条什么判据、上次结果如何」，并一键跑一遍。

只调用仓库里已有的回归脚本（和 skill litellm-198-version-upgrade §2 / 手册 §5.5 R7 是同一套），驾驶台只负责：
  1) 列清单：每条用例测什么、判据是什么、为什么要有它（哪次踩过坑）
  2) 后台跑：按「车道 A vs 车道 B」并排跑（默认 新版=gray vs 旧版=prod），结果落 $D/console/regress/<ts>/
  3) 读结果：解析每个脚本自己的结论行，统一成 PASS / FAIL / 两边一样的 FAIL（= 不是版本问题）

纪律（来自 10-17）：单边绿不算过；两边同样红 = 上游 / 用例自身问题，标成「同形」而不是「通过」也不是「版本问题」。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
from pathlib import Path

from collect import _kubectl, run_dir

REPO_SCRIPTS = Path(os.environ.get("GRAY_REPO_SCRIPTS", "/opt/gray-console/scripts"))

# 每条用例：id、名字、测什么、判据、来由；runner = 怎么跑（side 参数 = 车道的 pod IP）
CASES = [
    {"id": "sweep", "name": "流量前 24 组 × 3 协议", "paired": True,
     "what": "近 6 小时请求量最大的 24 个模型组，各用 chat / responses / messages 三种协议流式打一发，带唯一 nonce 和算术题",
     "pass": "两边成功数相同、0 单边；阴性对照（不存在的模型）两边都 4xx",
     "why": "手册 §5.5 R7 基线集；10-02 1.103.2 灰度用它确认修后两边 72/72"},
    {"id": "key_behavior", "name": "真 key 行为（cursor / claude-code）", "paired": False,
     "what": "克隆真 key 的白名单和别名起短命 key：老名字、新名字必须 200 且回显 nonce，白名单外名字必须 403",
     "pass": "每条车道都「✅ 行为层通过」",
     "why": "master key 不过白名单闸门，用它测必然假绿"},
    {"id": "auth_parity", "name": "真 key 形状鉴权并排", "paired": True,
     "what": "取全部不同的 (白名单, 别名) 形状，各克隆一把，对每个名字两边各打一发 1-token 请求，比 403 与否",
     "pass": "0 单边",
     "why": "10-17 上游 #43049 让 1323/2116 把 key 对白名单内名字 403，只有这条抓到"},
    {"id": "kiro", "name": "kiro 全部车道", "paired": False,
     "what": "config 里所有 kiro-* 组各打 3 轮，记成功率和 input_tokens 分层",
     "pass": "每条车道全部成功；两边 in_tok 分层一致（±5%）",
     "why": "in_tok 跨族塌陷 = 静默降级到别的上游"},
    {"id": "her", "name": "her 逐 pod", "paired": False,
     "what": "克隆 carher-1 的 key，在每个 pod 上打 her-pro / her-flash，加一个不存在的名字做阴性对照",
     "pass": "每个 pod 每个名字 OK、阴性对照被拒",
     "why": "每个 pod 各自缓存 key，单打 VIP 会把「某个 pod 没刷新」藏起来"},
    {"id": "budget", "name": "预算软拦截", "paired": False,
     "what": "超预算 key 两种协议都返回 200 友好文案；高配桶记账落 redis、第二发被拦；非高配模型不拦（阴性对照）",
     "pass": "8/8 PASS",
     "why": "budget_notice 依赖 429 状态码；1.104+ 默认改 422，必须验"},
    {"id": "failover", "name": "主组全挂兜底契约", "paired": False,
     "what": "按车道自己的 callbacks + env 建真 Router，主组 16 腿全部开流即过载、7 跳链只有最后一跳活，走真 aresponses 流式；同 pod 再跑一遍不带补丁的 vanilla 当对照",
     "pass": "每条车道都 OK：请求成功、主组 ≤4 次、链上每跳恰好 1 次",
     "why": "10-20 升级下线 midstream_fallback_loop 时连带丢了「同组换号≤3」，健康流量的回归全绿，第一次大面积过载就全打穿"},
    {"id": "codex", "name": "codex 压缩", "paired": True,
     "what": "zerokey / chatgpt 池 / deepseek 三条路的压缩轮和普通轮，比输出项形状",
     "pass": "compaction 三格两边都 compaction=1；其余格两边形状同类（模型随机出不出 reasoning 不算差异）",
     "why": "压缩回程丢了会让 Codex 长会话直接断"},
]
CASE_BY_ID = {c["id"]: c for c in CASES}

_mu = threading.Lock()
_job: dict = {}


def _dir() -> Path | None:
    rd = run_dir()
    return (rd / "console" / "regress") if rd else None


def lane_ip(app: str) -> str | None:
    raw = _kubectl("get", "po", "-l", f"app={app}", "--field-selector=status.phase=Running", "-o", "json")
    if not raw:
        return None
    for i in json.loads(raw)["items"]:
        if not i["metadata"].get("deletionTimestamp") and i["status"].get("podIP"):
            return i["status"]["podIP"]
    return None


def lane_ips(app: str) -> list[str]:
    raw = _kubectl("get", "po", "-l", f"app={app}", "--field-selector=status.phase=Running", "-o", "json")
    if not raw:
        return []
    return [i["status"]["podIP"] for i in json.loads(raw)["items"]
            if not i["metadata"].get("deletionTimestamp") and i["status"].get("podIP")]


def _master_key() -> str:
    import base64
    raw = _kubectl("get", "secret", "litellm-secrets", "-o", "jsonpath={.data.LITELLM_MASTER_KEY}")
    return base64.b64decode(raw).decode() if raw else ""


def _top24(out: Path) -> Path:
    sql = ("set statement_timeout='120s'; select model_group, count(*) from \"LiteLLM_SpendLogs\" where \"startTime\" > now()-interval '6 hours' "
           "and model_group is not null and model_group<>'' group by 1 order by 2 desc limit 24;")
    r = subprocess.run(["kubectl", "--kubeconfig", os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"), "-n", "litellm-product",
                        "exec", "-i", "litellm-db-0", "--", "psql", "-U", "litellm", "-d", "litellm", "-At", "-F", "|"],
                       input=sql, capture_output=True, text=True, timeout=180)
    f = out / "top24.txt"
    f.write_text(r.stdout)
    return f


def _kiro_models() -> str:
    rd = run_dir()
    for cand in (rd / "prod-target/snap/config/config.yaml" if rd else None, rd / "gray-inputs/snap/config/config.yaml" if rd else None):
        if cand and cand.exists():
            import yaml
            c = yaml.safe_load(cand.read_text())
            return ",".join(sorted({m["model_name"] for m in c["model_list"] if m["model_name"].startswith("kiro-")}))
    return ""


def _cmds(case: str, a_ip: str, b_ip: str, out: Path) -> list[tuple[str, list[str], dict]]:
    """返回 [(标签, argv, 额外 env)]；成对用例一条命令跑两边，非成对用例每边一条。"""
    S = REPO_SCRIPTS
    if case == "sweep":
        return [("A|B", ["python3", str(S / "litellm-198-lane-sweep.py"), a_ip, b_ip, str(_top24(out))], {})]
    if case == "auth_parity":
        return [("A|B", ["python3", str(S / "litellm-198-auth-parity.py"), a_ip, b_ip], {})]
    if case == "key_behavior":
        res = []
        for side, ip in (("A", a_ip), ("B", b_ip)):
            res.append((side + ":cursor", ["python3", str(S / "litellm-198-key-behavior-regress.py"), "--base", f"http://{ip}:4000",
                        "--clone-from", "cursor-miaosen-5pn4", "--new", "grok-4.7", "--old", "gpt-6-astra", "--old", "claude-opus-5",
                        "--old", "claude-opus-5-5", "--outside", "o3-pro", "--protocol", "openai"], {}))
            res.append((side + ":claude-code", ["python3", str(S / "litellm-198-key-behavior-regress.py"), "--base", f"http://{ip}:4000",
                        "--clone-from", "claude-code-00a715a4-7729", "--new", "anthropic.claude-opus-5", "--old", "anthropic.claude-sonnet-4-6",
                        "--outside", "o3-pro", "--protocol", "anthropic"], {}))
        return res
    if case == "kiro":
        models = _kiro_models()
        return [(side, ["python3", str(S / "litellm-lane-bench.py"), "--models", models, "--host", ip, "--port", "4000",
                        "--rounds", "3", "--out", str(out / f"kiro-{side}.jsonl")], {}) for side, ip in (("A", a_ip), ("B", b_ip))]
    if case == "her":
        ips = lane_ips(_LANES["A"]) + lane_ips(_LANES["B"])
        args = []
        for ip in ips:
            args += ["--pod-ip", ip]
        return [("A+B", ["python3", str(S / "litellm-198-her-name-probe.py"), "--clone-key", "carher-1", *args,
                         "--model", "her-pro", "--model", "her-flash"], {})]
    if case == "budget":
        return [(side, ["bash", str(S / "litellm-198-budget-notice-smoke.sh")], {"BASE_OVERRIDE": f"http://{ip}:4000"})
                for side, ip in (("A", a_ip), ("B", b_ip))]
    if case == "failover":
        lanes = [k for k, v in LANE_CHOICES.items() if v in (_LANES["A"], _LANES["B"])]
        return [("A+B", ["python3", str(S / "litellm-198-failover-contract-check.py"), "--local",
                         *[x for lane in lanes for x in ("--lane", lane)]], {})]
    if case == "codex":
        return [(side, ["python3", str(S / "codex-compaction-regression.py"), "before" if side == "B" else "after"],
                 {"LITELLM_BASE": f"http://{ip}:4000/v1", "REG_OUT_DIR": str(out)}) for side, ip in (("A", a_ip), ("B", b_ip))] \
            + [("diff", ["python3", str(S / "codex-compaction-regression.py"), "diff"], {"REG_OUT_DIR": str(out)})]
    raise ValueError(case)


_LANES = {"A": "litellm-proxy-gray", "B": "litellm-proxy"}
LANE_CHOICES = {"gray": "litellm-proxy-gray", "prod": "litellm-proxy", "guarded-old": "litellm-proxy-guarded-old"}


def parse(case: str, logs: dict[str, str]) -> dict:
    """把各脚本自己的结论行统一成 {status, summary, details[]}。status: PASS / FAIL / SAME_FAIL / ERROR。"""
    d: list[str] = []
    if case == "sweep":
        t = "\n".join(logs.values())
        m = re.search(r"TOTAL prod (\d+)/(\d+)\s+gray (\d+)/(\d+)\s+one-sided=(\d+)", t)
        neg = re.search(r"negative control: prod (\d+) gray (\d+)", t)
        if not m:
            return {"status": "ERROR", "summary": "没拿到汇总行", "details": t.splitlines()[-5:]}
        a_ok, n, b_ok, _, one = int(m.group(3)), int(m.group(2)), int(m.group(1)), 0, int(m.group(5))
        d = [ln.strip() for ln in t.splitlines() if "ONE-SIDED" in ln or " X " in ln][:20]
        neg_ok = bool(neg) and neg.group(1).startswith("4") and neg.group(2).startswith("4")
        st = "PASS" if one == 0 and neg_ok and a_ok == n else ("SAME_FAIL" if one == 0 and neg_ok else "FAIL")
        return {"status": st, "summary": f"A {a_ok}/{n} · B {b_ok}/{n} · 单边 {one}" + ("" if neg_ok else " · 阴性对照失败"), "details": d}
    if case == "auth_parity":
        t = "\n".join(logs.values())
        m = re.search(r"names checked (\d+) auth one-sided (\d+)", t)
        if not m:
            return {"status": "ERROR", "summary": "没拿到汇总行", "details": t.splitlines()[-5:]}
        d = [ln.strip() for ln in t.splitlines() if "AUTH DIFF" in ln][:20]
        return {"status": "PASS" if int(m.group(2)) == 0 else "FAIL", "summary": f"{m.group(1)} 个名字 · 单边 {m.group(2)}", "details": d}
    if case == "key_behavior":
        res = {k: ("✅ 行为层通过" in v) for k, v in logs.items()}
        a = all(v for k, v in res.items() if k.startswith("A")); b = all(v for k, v in res.items() if k.startswith("B"))
        d = [f"{k}: {'通过' if v else '不过'}" for k, v in res.items()]
        for k, v in logs.items():
            d += [f"  {k} {ln.strip()}" for ln in v.splitlines() if "RED" in ln][:4]
        return {"status": "PASS" if a and b else ("SAME_FAIL" if a == b else "FAIL"), "summary": f"A {'过' if a else '不过'} · B {'过' if b else '不过'}", "details": d}
    if case == "kiro":
        sums = {}
        for k, v in logs.items():
            m = re.search(r"全局 (\d+)/(\d+) 成功", v)
            sums[k] = (int(m.group(1)), int(m.group(2))) if m else None
            d += [f"{k} {ln.strip()}" for ln in v.splitlines() if re.match(r"\s*kiro-\S+\s+\d+/\d+", ln) and not re.search(r"\s(\d+)/\1\s", ln)][:10]
        if not all(sums.values()):
            return {"status": "ERROR", "summary": "有一边没跑出汇总", "details": d}
        a, b = sums.get("A"), sums.get("B")
        ok_a, ok_b = a[0] == a[1], b[0] == b[1]
        return {"status": "PASS" if ok_a and ok_b else ("SAME_FAIL" if a == b else "FAIL"),
                "summary": f"A {a[0]}/{a[1]} · B {b[0]}/{b[1]}", "details": d}
    if case == "her":
        t = "\n".join(logs.values())
        bad = [ln.strip() for ln in t.splitlines() if re.search(r"\s(HTTP_\d+|MISS|NOT_BLOCKED)", ln)]
        passed = "PASS: every pod serves every name" in t
        # her-flash 上游欠费这类：A/B 两边同一个名字同一种错 ⇒ 同形
        return {"status": "PASS" if passed else ("SAME_FAIL" if bad and _same_across_lanes(bad) else "FAIL"),
                "summary": "每个 pod 每个名字都通" if passed else f"{len(bad)} 个格子不通", "details": bad[:20]}
    if case == "budget":
        # 逐行数 PASS/FAIL（不依赖末尾汇总行，脚本中途挂了也能看出跑到哪）
        cnt = {}
        for k, v in logs.items():
            p_ = sum(1 for ln in v.splitlines() if ln.startswith("PASS "))
            f_ = [ln.split("::")[0].strip() for ln in v.splitlines() if ln.startswith("FAIL ")]
            cnt[k] = (p_, f_)
            d += [f"{k} {x}" for x in f_]
        if not cnt or any(p_ + len(f_) == 0 for p_, f_ in cnt.values()):
            return {"status": "ERROR", "summary": "有一边一条结果都没有", "details": [ln for v in logs.values() for ln in v.splitlines()[-3:]]}
        (pa, fa), (pb, fb) = cnt.get("A", (0, [])), cnt.get("B", (0, []))
        st = "PASS" if not fa and not fb else ("SAME_FAIL" if sorted(fa) == sorted(fb) else "FAIL")
        return {"status": st, "summary": f"A {pa}/{pa + len(fa)} · B {pb}/{pb + len(fb)}", "details": d[:20]}
    if case == "failover":
        t = "\n".join(logs.values())
        m = re.search(r"^SUMMARY (.+)$", t, re.M)
        d = [ln.strip() for ln in t.splitlines() if ln.startswith(("FAIL", "STOP", "NOTE", "OK"))]
        if not m:
            return {"status": "ERROR", "summary": "没拿到 SUMMARY 行", "details": t.splitlines()[-5:]}
        st = "PASS" if "FAIL" not in m.group(1) and "STOP" not in m.group(1) else ("ERROR" if "STOP" in m.group(1) else "FAIL")
        return {"status": st, "summary": m.group(1), "details": d}
    if case == "codex":
        t = logs.get("diff", "")
        changed = re.search(r"变了: \[(.*?)\]", t)
        ch = [x.strip(" '") for x in (changed.group(1).split(",") if changed and changed.group(1) else []) if x.strip(" '")]
        must = [c for c in ch if "compact" in c]   # 压缩格变了才是真问题；普通格是模型随机出不出 reasoning
        d = [ln for ln in t.splitlines() if re.match(r"^(zk|acct|ds|chat)\.", ln)]
        if not d:
            return {"status": "ERROR", "summary": "没拿到 diff 表", "details": t.splitlines()[-5:]}
        return {"status": "FAIL" if must else "PASS",
                "summary": "压缩格两边一致" + (f" · 普通格有 {len(ch)} 处形状随机差异（不算）" if ch else "") if not must else f"压缩格不一致：{must}",
                "details": d}
    return {"status": "ERROR", "summary": "未知用例", "details": []}


def _same_across_lanes(bad: list[str]) -> bool:
    by_name = {}
    for ln in bad:
        p = ln.split()
        if len(p) >= 3:
            by_name.setdefault(p[1], set()).add(p[2])
    return all(len(v) == 1 for v in by_name.values())


def start(cases: list[str], lane_a: str = "gray", lane_b: str = "prod", who: str = "human") -> dict:
    from actions import log_event
    base = _dir()
    if base is None:
        return {"ok": False, "error": "找不到 run 目录"}
    cases = [c for c in cases if c in CASE_BY_ID] or [c["id"] for c in CASES]
    with _mu:
        if _job.get("running"):
            return {"ok": False, "error": "已有一轮回归在跑"}
        _LANES["A"], _LANES["B"] = LANE_CHOICES[lane_a], LANE_CHOICES[lane_b]
        a_ip, b_ip = lane_ip(_LANES["A"]), lane_ip(_LANES["B"])
        if not a_ip or not b_ip:
            return {"ok": False, "error": f"找不到车道 pod：A={a_ip} B={b_ip}"}
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = base / ts
        out.mkdir(parents=True, mode=0o700)
        meta = {"ts": ts, "lane_a": lane_a, "lane_b": lane_b, "a_ip": a_ip, "b_ip": b_ip, "cases": cases,
                "results": {}, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "who": who}
        (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        _job.update(running=True, ts=ts, current=None)
    log_event("regress_start", who, cases=cases, lane_a=lane_a, lane_b=lane_b)
    threading.Thread(target=_run, args=(out, meta, a_ip, b_ip), daemon=True).start()
    return {"ok": True, "ts": ts}


def _run(out: Path, meta: dict, a_ip: str, b_ip: str):
    from actions import log_event
    mk = _master_key()
    env0 = dict(os.environ, MK=mk, LITELLM_MASTER_KEY=mk, OPENAI_API_KEY=mk,
                KUBECONFIG=os.environ.get("KUBECONFIG", "/etc/rancher/k3s/k3s.yaml"))
    try:
        for case in meta["cases"]:
            with _mu:
                _job["current"] = case
            t0 = dt.datetime.now(dt.timezone.utc)
            logs: dict[str, str] = {}
            try:
                for label, argv, extra in _cmds(case, a_ip, b_ip, out):
                    r = subprocess.run(argv, capture_output=True, text=True, timeout=1800, env=dict(env0, **extra), cwd=str(out))
                    txt = re.sub(r"sk-[A-Za-z0-9._~-]{6,}", "<R>", (r.stdout or "") + (r.stderr or ""))
                    logs[label.split(":")[0] if case != "key_behavior" else label] = txt
                    (out / f"{case}-{label.replace('|', '_').replace(':', '_').replace('+', '_')}.log").write_text(txt)
                res = parse(case, logs)
            except Exception as e:
                res = {"status": "ERROR", "summary": f"{type(e).__name__}: {e}"[:300], "details": []}
            res["seconds"] = int((dt.datetime.now(dt.timezone.utc) - t0).total_seconds())
            meta["results"][case] = res
            (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
            log_event("regress_case", "machine", case=case, status=res["status"], summary=res["summary"])
        meta["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
        st = [r["status"] for r in meta["results"].values()]
        log_event("regress_end", "machine", passed=st.count("PASS"), same=st.count("SAME_FAIL"), failed=st.count("FAIL") + st.count("ERROR"))
    finally:
        with _mu:
            _job.update(running=False, current=None)


def state(limit: int = 8) -> dict:
    base = _dir()
    runs = []
    if base and base.exists():
        for d in sorted(base.iterdir(), reverse=True)[:limit]:
            try:
                runs.append(json.loads((d / "meta.json").read_text()))
            except Exception:
                pass
    with _mu:
        job = dict(_job)
    return {"cases": CASES, "runs": runs, "job": job, "lanes": list(LANE_CHOICES)}


def import_past(ts: str, lane_a: str, lane_b: str, results: dict, when: str) -> None:
    """把已经跑过的一轮（例如 10-17 升级时手跑的）登记进来，页面能看到历史。"""
    base = _dir()
    if base is None:
        return
    d = base / ts
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    meta = {"ts": ts, "lane_a": lane_a, "lane_b": lane_b, "cases": list(results), "results": results,
            "started_at": when, "finished_at": when, "who": "imported", "imported": True}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
