#!/usr/bin/env python3
"""198 生产侧：中流兜底链到底下降了没 —— 只读审计。

和 `litellm-midstream-chain-descent-mock.py` 是**两把不同的尺子**，缺一不可：
  - mock 那把量的是「我们进程里的 Router 行为」，能带阳性对照，但它不是生产 worker。
  - 本脚本量的是「生产 worker 真实发生了什么」，没有对照可带（合不出中流失败），
    但它是唯一能回答「线上真的兜住了吗」的东西。

判据分两面，两面都要过：
  A 面（日志）：补丁自己打的 `mid-stream failover attempt N/M (mode=...) trace=...`
     按 trace 归组 ⇒ 每条流重发了几发、第几发开始跨组、有没有打满预算耗尽抛错。
  B 面（SpendLogs）：`metadata.original_model_group != model_group` 的行 ⇒ 跨组兜底
     的**落点**。SpendLogs 只记成功那一发，所以这些行就是「某一层兜住了」的实据。

只读：全程只有 `kubectl logs` / `kubectl exec psql SELECT`，不写任何配置。

用法：
    python3 scripts/litellm-midstream-chain-descent-prod-audit.py --group gpt-6-astra
    python3 scripts/litellm-midstream-chain-descent-prod-audit.py --group gpt-6-astra --hours 6
    python3 scripts/litellm-midstream-chain-descent-prod-audit.py --no-db      # 只跑 A 面
"""

from __future__ import annotations

import argparse
import collections
import re
import subprocess
import sys

SSH = ["ssh", "-o", "ConnectTimeout=10", "cltx@10.68.13.198"]
NS = "litellm-product"
DEPLOY_SELECTOR = "app=litellm-proxy"
DB_POD = "litellm-db-0"
DB_USER, DB_NAME = "litellm", "litellm"

# 补丁自己打的行。**从源码抄，别凭印象写** —— 见 skill §5 的红字：
# 第一版把装载行写成 `installed`（源码里没这个词），会把装好的补丁读成没装。
RE_ATTEMPT = re.compile(
    r"attempt (\d+)/(\d+) for model_group=(\S+) \(mode=(\S+)\) trace=(\S+)"
)
ANCHOR_INSTALLED = "patched Router._aresponses_streaming_iterator"
ANCHOR_REFUSED = "NOT patching|install failed, skipped|import failed, skip"
ANCHOR_EXHAUSTED = "failovers .*exhausted, surfacing"
# 运行期装载证据：pod 跑过几小时后启动行会被日志滑窗吃掉，那时只能靠这行。
ANCHOR_RUNNING = "mid-stream failover attempt"


def sh(remote_cmd: str, timeout: int = 180) -> str:
    p = subprocess.run(
        SSH + [remote_cmd], capture_output=True, text=True, timeout=timeout
    )
    if p.returncode != 0 and not p.stdout:
        print("⛔ ssh 失败: %s" % (p.stderr or "")[:400], file=sys.stderr)
        sys.exit(2)
    return p.stdout


def psql(sql: str, timeout: int = 300) -> str:
    """走 stdin 喂 SQL，避开多层引号转义。"""
    inner = (
        "sudo kubectl -n %s exec -i %s -- psql -U %s -d %s "
        "--pset=format=unaligned --pset=fieldsep='|' -f /dev/stdin"
        % (NS, DB_POD, DB_USER, DB_NAME)
    )
    p = subprocess.run(
        SSH + [inner], input=sql, capture_output=True, text=True, timeout=timeout
    )
    return p.stdout


MIN_POD_AGE_S = 180  # 比这更嫩的 pod 日志里可能还没打出装载行 ⇒ 判据无效


def live_pods() -> tuple[list[str], list[str]]:
    """返回 (可判的副本, 太嫩/正在退出而跳过的副本)。

    只取当前 ReplicaSet 的 Running 且非 Terminating 副本。滚更期间混进
    Terminating 的旧副本 = 在读一份即将消失的代码；混进刚起 90 秒的新副本
    = 在读一份还没打完启动日志的代码。两种都会产出假红。
    """
    out = sh(
        "sudo kubectl -n %s get pods -l %s -o "
        "'jsonpath={range .items[*]}{.metadata.name}|{.status.phase}|"
        "{.metadata.deletionTimestamp}|{.status.startTime}{\"\\n\"}{end}' 2>/dev/null"
        % (NS, DEPLOY_SELECTOR)
    )
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    ok, skipped = [], []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 4 or not parts[0].strip():
            continue
        name, phase, deleting, started = (p.strip() for p in parts[:4])
        if deleting:
            skipped.append("%s (Terminating)" % name)
            continue
        if phase != "Running":
            skipped.append("%s (%s)" % (name, phase))
            continue
        try:
            age = (now - _dt.datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds()
        except Exception:
            age = MIN_POD_AGE_S + 1
        if age < MIN_POD_AGE_S:
            skipped.append("%s (只起了 %ds，装载行可能还没打出来)" % (name, int(age)))
            continue
        ok.append(name)
    if not ok:
        print("⛔ 没有可判的 proxy 副本 —— 这轮报废（尺子量不到东西）。")
        for s in skipped:
            print("   跳过:", s)
        print("   若正在滚更，等 `rollout status` 收敛后重跑。")
        sys.exit(2)
    return ok, skipped


def install_state(pods: list[str]) -> bool:
    """A0：先断言「补丁在跑」。会数东西的门禁必须先证明自己数得到。

    🔴 必须排除**刚起的 pod 和 Terminating 的 pod**，否则是假红：
    2026-09-29 本脚本首跑时生产正在滚更（不是我干的），一个 95 秒的新 pod
    日志里还没打出装载行，被判成"⚠️ 判不了"；同一时刻 `sha256sum` 和
    稍后的日志都证明它装得好好的。**滚更窗口里的 0 是 rollout 窗口，不是回归。**
    """
    print("== A0. 补丁装载状态（逐副本，不抽样）==")
    all_ok = True
    for pod in pods:
        c = sh(
            "L=$(sudo kubectl -n %s logs %s 2>/dev/null); "
            "echo \"$(echo \"$L\" | grep -c '%s')|"
            "$(echo \"$L\" | grep -cE '%s')|"
            "$(echo \"$L\" | grep -c '%s')|"
            "$(echo \"$L\" | grep -c '%s')\""
            % (NS, pod, ANCHOR_INSTALLED, ANCHOR_REFUSED, ANCHOR_RUNNING, ANCHOR_EXHAUSTED)
        ).strip()
        try:
            inst, refused, running, exhausted = (int(x) for x in c.split("|"))
        except ValueError:
            print("  %-46s 读数解析失败: %r" % (pod, c[:80]))
            all_ok = False
            continue
        # 装载行是启动期打的，日志滑窗只留 2~4 分钟 ⇒ pod 跑久了它必然是 0。
        # 那不等于没装：改认运行期行。
        if inst > 0:
            verdict, why = "装上了", "启动行 %d" % inst
        elif running > 0:
            verdict, why = "装上了", "启动行已滑出窗口，运行行 %d" % running
        else:
            verdict, why = "⚠️ 判不了", "两种证据都是 0（可能真没装，也可能只是没流量）"
            all_ok = False
        if refused:
            verdict, why = "🔴 拒装", "拒装行 %d" % refused
            all_ok = False
        print("  %-46s %-10s (%s, 耗尽抛错 %d)" % (pod, verdict, why, exhausted))
    return all_ok


def face_a(pods: list[str], group: str | None, same_group_budget: int) -> dict:
    """A 面：日志按 trace 归组，看每条流重发几发、第几发跨组。"""
    print("\n== A 面. 日志：每条流重发了几发 ==")
    lines: list[str] = []
    for pod in pods:
        lines += sh(
            "sudo kubectl -n %s logs %s 2>/dev/null | grep '%s'"
            % (NS, pod, ANCHOR_RUNNING)
        ).splitlines()

    rows = []
    for ln in lines:
        m = RE_ATTEMPT.search(ln)
        if m:
            rows.append((int(m.group(1)), int(m.group(2)), m.group(3), m.group(4), m.group(5)))
    if not rows:
        print("  窗口内零次中流重发 —— **这不是绿，是没数据**。")
        print("  日志只留 2~4 分钟，且中流失败要碰上上游容量抖动才会发生。")
        print("  唯一合法结论：这轮没量到，不能说「链子好了」也不能说「坏了」。")
        return {}

    budget = rows[0][1]
    tr: dict[str, list] = collections.OrderedDict()
    for a, _b, g, mo, t in rows:
        tr.setdefault(t, []).append((a, g, mo))

    if group:
        tr = collections.OrderedDict(
            (t, v) for t, v in tr.items() if any(group in g for _, g, _ in v)
        )
        print("  （已按 --group %s 过滤）" % group)
        if not tr:
            print("  该组窗口内零次中流重发 —— 没量到，不出结论。")
            return {}

    flat = [x for v in tr.values() for x in v]
    cross = [x for x in flat if x[0] > same_group_budget]
    deepest = max((max(a for a, _, _ in v), t) for t, v in tr.items())

    print("  预算 = %d 发 / 同组闸门 = 前 %d 发" % (budget, same_group_budget))
    print("  有中流失败并重发的流 = %d 条，总重发 = %d 发" % (len(tr), len(flat)))
    print("  同组重发(第1~%d发) = %d 发" % (same_group_budget, len(flat) - len(cross)))
    print("  跨组重发(第%d发起) = %d 发   <-- 病灶未修时这些发会全砸第一层"
          % (same_group_budget + 1, len(cross)))
    print("  顶到第%d发的流 = %d 条 / 开闸跨组的流 = %d 条"
          % (same_group_budget,
             sum(1 for v in tr.values() if any(a >= same_group_budget for a, _, _ in v)),
             sum(1 for v in tr.values() if any(a > same_group_budget for a, _, _ in v))))
    print("  打满 %d 发耗尽 = %d 条" % (budget, sum(1 for v in tr.values()
                                                if any(a > budget for a, _, _ in v))))

    seq = tr[deepest[1]]
    print("\n  最深那条流：trace=%s，最高 attempt=%d，共 %d 行"
          % (deepest[1][:8], deepest[0], len(seq)))
    print("   ", " -> ".join("%d:%s" % (a, mo) for a, _, mo in seq[:14]))
    if len(seq) > deepest[0]:
        print("    （行数 > 最高 attempt：同一 trace 下 attempt 会归零重数，"
              "说明这条流被上游重试了多轮，不是一条连续的 10 发）")
    return {"traces": tr, "cross": len(cross), "budget": budget}


def face_a2(pods: list[str], traces: dict) -> None:
    """A2：这些流有没有把错抛给前端用户。

    🔴 归属只能按 trace id，**不许按组名 grep 整行**。
    2026-09-29 我用 `grep astra` 数出「32 条 astra 报错」，实际是别的组的长报错串里
    嵌了 `astra` 这个子串（fallbacks 字典会被整个打进消息里，里面列着所有组名）。
    按 trace 归属重查是 0。组名 grep 在这里**恒定虚高**，且高得很像真的。
    """
    print("\n== A2. 这些流有没有把错抛给用户（按 trace 归属，不按组名 grep）==")
    if not traces:
        print("  A 面无数据，跳过。")
        return
    errs: list[str] = []
    for pod in pods:
        errs += sh(
            "sudo kubectl -n %s logs %s 2>/dev/null | grep -E ':ERROR:|surfacing error'"
            % (NS, pod)
        ).splitlines()
    hit = [l for l in errs if any(t in l for t in traces)]
    print("  全部 ERROR 行 = %d，其中能归属到这些流的 = %d" % (len(errs), len(hit)))
    for l in hit[:5]:
        print("   ", l.strip()[:170])
    if not hit:
        print("  ⇒ 零条抛给用户。（注意：这是「窗口内没发生」，不是「永不会发生」）")


def face_b(group: str | None, hours: int) -> None:
    """B 面：SpendLogs 里跨组兜底的落点。"""
    print("\n== B 面. SpendLogs：跨组兜底真的落在了哪一层 ==")
    where = ""
    if group:
        where = ("   AND (metadata::jsonb->>'original_model_group') "
                 "IN ('%s','chatgpt-%s')\n" % (group, group))
    sql = (
        "SELECT coalesce(metadata::jsonb->>'original_model_group','-') AS orig,\n"
        "       model_group AS landed, status, count(*) n,\n"
        "       max(\"startTime\")::timestamp(0) AS last_seen\n"
        "  FROM \"LiteLLM_SpendLogs\"\n"
        " WHERE \"startTime\" > now() - interval '%d hours'\n"
        "   AND metadata::jsonb->>'original_model_group' IS NOT NULL\n"
        "   AND metadata::jsonb->>'original_model_group' <> ''\n"
        "   AND metadata::jsonb->>'original_model_group' <> model_group\n"
        "%s"
        " GROUP BY 1,2,3 ORDER BY n DESC LIMIT 40;\n" % (hours, where)
    )
    out = psql(sql)
    rows = [l for l in out.splitlines() if "|" in l]
    if len(rows) <= 1:
        print("  窗口内零条跨组落点 —— 没量到，不出结论。")
        print("  ⚠️ 别写成 `metadata::text LIKE '%%<trace>%%'`：25 万行上全表扫会超时。")
        return
    for l in rows:
        print("  ", l)
    print("\n  读法：orig != landed 的行就是「兜住了」的实据 —— SpendLogs 只记成功那一发，")
    print("  失败的中流重发不落行，所以这里出现的每一行都是某一层真的接住了。")
    return len(rows) - 1


def cross_check(a_cross: int | None, b_rows: int | None, hours: int) -> None:
    """🔴 两面窗口差着数量级，读数必须先对齐再比，否则会得出相反结论。

    2026-09-29 首跑就撞上：A 面（日志，窗口只有 2~4 分钟）跨组 = 0 发，
    B 面（SpendLogs，窗口 11h）跨组落点 = 7 条。单看 A 面会把**已经修好的**
    判成"链子还是不下降"。日志滑窗是**唯一**能读到 attempt 序列的地方，
    但它短得离谱 —— A 面为 0 时正确结论是「这几分钟没赶上」，不是「没下降」。
    """
    print("\n== 两面互校（窗口不对齐会得出相反结论）==")
    print("  A 面窗口 = pod 日志滑窗，**只有 2~4 分钟**")
    print("  B 面窗口 = SpendLogs %d 小时" % hours)
    if a_cross is None or b_rows is None:
        print("  有一面无数据，无法互校。")
        return
    if a_cross == 0 and b_rows > 0:
        print("  ⚠️ A 面跨组 0 发、B 面跨组落点 %d 条 —— **不矛盾**：" % b_rows)
        print("     A 面那几分钟刚好没赶上跨组，B 面证明 %dh 内真的跨过。" % hours)
        print("     🔴 此时**不许**只拿 A 面说「链子不下降」。要 A 面的 0 有意义，")
        print("     只能盯着日志等下一次真实中流失败（或按 B 面的 last_seen 回看那一刻）。")
    elif a_cross > 0:
        print("  ✅ A 面直接量到 %d 发跨组重发 —— 这是最强的一手证据" % a_cross)
        print("     （attempt 序列只有日志有，SpendLogs 答不了"
              "「第几发开始跨的」）。")
    else:
        print("  两面都是 0 —— 窗口内没发生跨组兜底。**没量到，不出结论。**")


def chain_from_db(group: str | None) -> None:
    """链表现值只认 DB —— DB 整键覆盖 config.yaml，读 yaml 会读到早已不生效的那份。"""
    print("\n== 链表现值（只认 DB，yaml 那份不参与）==")
    out = psql(
        "SELECT jsonb_pretty((SELECT param_value::jsonb->'fallbacks' "
        "FROM \"LiteLLM_Config\" WHERE param_name='router_settings'));"
    )
    if group:
        keep, depth = [], 0
        for l in out.splitlines():
            if group in l and '"' in l:
                keep.append(l)
                depth = 1
            elif depth and (l.strip().startswith('"') or l.strip() in ("],", "]")):
                keep.append(l)
                if l.strip() in ("],", "]"):
                    depth = 0
        out = "\n".join(keep) if keep else "  （DB 里没有 %s 这一行 ⇒ 该组没配链子）" % group
    print(out[:1200])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="只看这个原始组名，如 gpt-6-astra")
    ap.add_argument("--hours", type=int, default=12, help="SpendLogs 回看窗口（默认 12h）")
    ap.add_argument("--same-group-budget", type=int, default=3,
                    help="同组重试发数，与补丁的 _MAX_SAME_GROUP_RETRIES 一致（默认 3）")
    ap.add_argument("--no-db", action="store_true", help="只跑 A 面，不查 SpendLogs")
    a = ap.parse_args()

    print("198 中流兜底链 —— 生产侧只读审计（不写任何配置）")
    print("⚠️ 本脚本**没有阳性对照**：中流失败合成不出来（`mock_testing_fallbacks` 在 198 上硬 400）。")
    print("   所以它只能回答「窗口内发生了什么」，答不了「如果坏了我能不能看出来」——")
    print("   后者要靠 scripts/litellm-midstream-chain-descent-mock.py（那把带对照）。\n")

    pods, skipped = live_pods()
    print("可判副本 = %d 个" % len(pods))
    for s in skipped:
        print("  跳过: %s  ← 算进验收就是假红" % s)
    print()
    install_state(pods)
    r = face_a(pods, a.group, a.same_group_budget)
    face_a2(pods, r.get("traces", {}))
    b_rows = None
    if not a.no_db:
        chain_from_db(a.group)
        b_rows = face_b(a.group, a.hours)
    cross_check(r.get("cross"), b_rows, a.hours)
    print("\n完成。两把尺子都过才算「链子在生产上真的会逐层下降」。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
