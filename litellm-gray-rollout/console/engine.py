"""驾驶台的判定核心：读数进、结论出。纯函数，不碰网络、不碰磁盘，方便用 10-17 那晚的真实读数回放。

判定三态（借 Argo Rollouts 的 Successful / Failed / Inconclusive）：
  PASS          ODD 全满足 ⇒ 机器可以自己推进
  INCONCLUSIVE  没坏但证据不够 ⇒ 继续驻留；拖太久 ⇒ 升级成决策点
  FAIL          有一条硬条件不过 ⇒ 停在当前档，立刻要人拍板

决策点永远不会因为没人回答而前进（Spinnaker Manual Judgment 的教训：超时默认动作必须是安全的那个）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RAMP = (1, 5, 10, 50, 100)
# 每档最短驻留（分钟）。50/100 多 5 分钟：流量大一个数量级，止损腿需要的窗口一样多。
DWELL_MINUTES = {0: 25, 1: 25, 5: 25, 10: 25, 50: 30, 100: 30}
MIN_OK_CYCLES = 5          # = step.sh / check-monitor-continuity 的 ≥4 再多一个余量
FIVE_XX_MARGIN_PP = 1.0    # 新版 5xx 率最多比旧版高 1 个百分点
MIN_GRAY_SAMPLE = 50       # 新版请求少于这个数，5xx 率不判（样本不足 = 不确定，不是通过）
INCONCLUSIVE_ESCALATE_FACTOR = 2  # 不确定状态拖到 2 倍驻留时间 ⇒ 升级成决策点

# 必须人拍板的动作（无论多顺）：开始、收敛、提交、回退/关 run
HUMAN_ONLY = frozenset({"start_pilot", "converge", "commit", "close_run", "global_rollback"})


@dataclass
class Readings:
    """一次采集的读数。缺的字段 = None，判定时当「不知道」，绝不当「没问题」。"""

    phase: str | None = None
    split: int | None = None
    run_id: str | None = None
    generation: str | None = None
    minutes_at_split: float | None = None
    ok_cycles: int | None = None
    bad_cycles: int | None = None
    gray_requests: int | None = None
    gray_5xx: int | None = None
    stable_requests: int | None = None
    stable_5xx: int | None = None
    worker_killed: int | None = None
    restarts_changed: bool | None = None
    capacity_status: str | None = None          # PASS / FAIL / None(没跑)
    capacity_errors: list[str] = field(default_factory=list)
    capacity_bound_to_run: bool | None = None   # 证据是不是本 run 本 generation 的
    guarded_old_ready: str | None = None        # "2/2"
    guarded_old_health: int | None = None
    monitor_alive: bool | None = None
    last_metrics_reason_codes: list[str] = field(default_factory=list)
    last_metrics_action: str | None = None
    noisy_keys_protected: bool | None = None    # stability-probe / carher-hangzhou 是否已钉旧版


@dataclass
class Verdict:
    state: str                       # PASS / INCONCLUSIVE / FAIL / ROLLED_BACK / IDLE / HUMAN_STEP / DONE
    headline: str                    # 一句话，说人话
    reasons: list[str]
    next_action: str | None = None   # 机器想做的动作（白名单名字）
    next_arg: Any = None
    decision: dict | None = None     # 需要人拍板时的决策卡


def _rate(n: int | None, e: int | None) -> float | None:
    if not n:
        return None
    return round(100.0 * (e or 0) / n, 2)


def next_split(cur: int) -> int | None:
    if cur == 0:
        return 1
    if cur in RAMP:
        i = RAMP.index(cur)
        return RAMP[i + 1] if i + 1 < len(RAMP) else None
    return None


def evaluate(r: Readings, auto_enabled: bool = True) -> Verdict:
    """给一次读数下结论。顺序有讲究：先看「已经出事了吗」，再看「够不够条件往前」。"""
    # 0. 没有活动 run / 终态
    if r.phase in (None, "", "none"):
        return Verdict("IDLE", "没有进行中的灰度", ["active 目录不存在或已关闭"],
                       decision=_decision_start())
    if r.phase == "committed":
        return Verdict("DONE", "升级已完成：生产已切到新版本", ["phase=committed"],
                       decision=_decision_close_run())
    if r.phase == "rolled_back":
        return Verdict("ROLLED_BACK", "自动止损已把全部流量退回旧版本（用户无感）",
                       [f"触发原因：{', '.join(r.last_metrics_reason_codes) or '未知（看 metrics 证据）'}"],
                       decision=_decision_after_rollback(r))
    if r.phase in ("preflight",):
        return Verdict("HUMAN_STEP", "新 run 已初始化，等你决定开始试点", ["phase=preflight"],
                       decision=_decision_start())
    if r.phase in ("convergence_ready", "prod_offline_upgrading", "prod_verified"):
        return Verdict("HUMAN_STEP", f"收敛进行中（{r.phase}）", [f"phase={r.phase}"],
                       decision=_decision_commit() if r.phase == "prod_verified" else None)
    if r.phase != "normal_gray" or r.split is None:
        return Verdict("FAIL", f"状态看不懂：phase={r.phase} split={r.split}", ["读数不全，机器不敢动"],
                       decision=_decision_manual(r, "状态读不全"))

    cur = r.split
    nxt = next_split(cur)
    dwell = DWELL_MINUTES.get(cur, 30)
    fails: list[str] = []
    unsure: list[str] = []

    # 1. 硬失败（任一 ⇒ 停在这里等人）
    if r.monitor_alive is False:
        fails.append("监控循环没在跑：止损腿没武装，机器不能往前推")
    if r.bad_cycles:
        fails.append(f"本档有 {r.bad_cycles} 个监控周期失败")
    if r.worker_killed:
        fails.append(f"新版 worker 被杀 {r.worker_killed} 次（1.103 那次 502 事故的形状）")
    if r.restarts_changed:
        fails.append("新版 pod 重启数变了")
    gr, sr = _rate(r.gray_requests, r.gray_5xx), _rate(r.stable_requests, r.stable_5xx)
    if gr is not None and sr is not None and (r.gray_requests or 0) >= MIN_GRAY_SAMPLE and gr > sr + FIVE_XX_MARGIN_PP:
        fails.append(f"新版 5xx {gr}% 高于旧版 {sr}% 超过 {FIVE_XX_MARGIN_PP} 个百分点")

    # 2. 不确定（继续等）
    if r.minutes_at_split is None or r.ok_cycles is None:
        unsure.append("驻留时间 / 监控周期读不到")
    else:
        if r.minutes_at_split < dwell:
            unsure.append(f"本档还要再观察 {dwell - r.minutes_at_split:.0f} 分钟（共 {dwell}）")
        if r.ok_cycles < MIN_OK_CYCLES:
            unsure.append(f"监控周期 {r.ok_cycles}/{MIN_OK_CYCLES}")
    if (r.gray_requests or 0) < MIN_GRAY_SAMPLE and cur > 0:
        unsure.append(f"新版样本只有 {r.gray_requests or 0} 个请求（< {MIN_GRAY_SAMPLE}），5xx 率不判")
    if cur > 0 and r.noisy_keys_protected is False:
        fails.append("巡检号 / 杭州 gw 凭据没钉在旧版：它们的失败会被算到新版头上（10-17 两次误回退）")

    # 3. ≥50 档的额外条件
    if nxt is not None and nxt >= 50:
        if r.capacity_status is None:
            unsure.append("容量还没测（升 50/100 前必须测）")
        elif r.capacity_status != "PASS":
            fails.append(f"容量检查不过：{', '.join(r.capacity_errors) or r.capacity_status}")
        elif r.capacity_bound_to_run is False:
            fails.append("容量证据不是本 run 本 generation 的（10-17 读到上一轮残留）")
        if r.guarded_old_ready not in (None, "") and r.guarded_old_ready.split("/")[0] != r.guarded_old_ready.split("/")[-1]:
            fails.append(f"回退保险丝 guarded-old 不全 Ready（{r.guarded_old_ready}）")
        if r.guarded_old_health not in (None, 200):
            fails.append(f"回退保险丝健康检查 {r.guarded_old_health}")

    progress = f"{cur}%"
    if fails:
        return Verdict("FAIL", f"停在 {progress}：有问题需要你看", fails + unsure,
                       decision=_decision_hold(r, fails))
    if unsure:
        overdue = (r.minutes_at_split or 0) >= dwell * INCONCLUSIVE_ESCALATE_FACTOR
        v = Verdict("INCONCLUSIVE", f"{progress} 观察中", unsure)
        if overdue:
            v.headline = f"{progress} 已观察 {r.minutes_at_split:.0f} 分钟仍证据不足"
            v.decision = _decision_inconclusive(r, unsure)
        return v
    if nxt is None:
        return Verdict("PASS", "100% 稳定，可以把生产本体换成新版本", ["全部档位已过"],
                       decision=_decision_converge())
    if not auto_enabled:
        return Verdict("PASS", f"{progress} 条件都满足，自动推进已关，等你点", [f"可以升到 {nxt}%"],
                       decision=_decision_ramp_manual(cur, nxt))
    return Verdict("PASS", f"{progress} 稳定，机器自动升到 {nxt}%", [f"驻留 {r.minutes_at_split:.0f} 分钟、{r.ok_cycles} 个周期全过"],
                   next_action="ramp", next_arg=nxt)


# ---------- 决策卡 ----------
# 每张卡：发生了什么 / 证据 / 建议 / 选项（每个写清后果）/ 不选会怎样

def _card(title, what, recommend, options, if_no_answer="保持现状：当前档继续跑，自动止损照常武装。"):
    return {"title": title, "what": what, "recommend": recommend, "options": options, "if_no_answer": if_no_answer}


def _decision_start():
    # 开始试点要挑人（近 24h 单次输入最大的在职用户）+ 从杭州机管道钉 carher-hangzhou，
    # 这两步需要人判断 / 需要 Mac 上的 jms 通道，驾驶台不代劳，只给清单。
    return _card("开始灰度？", "新版本已就绪。开始试点需要在终端做三件事（驾驶台不代劳）：", "noop",
                 [{"action": "noop", "label": "我在终端做",
                   "effect": "① gray-run-init + workload-pin + gray-phase set normal_gray；"
                             "② run/protect-noisy-keys.sh 并在 Mac 上跑杭州凭据管道；"
                             "③ 试点 10 把 force-gray、monitor loop 起来。做完驾驶台自动接手放量。"}],
                 if_no_answer="什么都不做。")


def _decision_close_run():
    return _card("收尾", "升级已完成。", "close_run",
                 [{"action": "close_run", "label": "关闭本轮灰度", "effect": "归档本轮状态，不改路由。"},
                  {"action": "noop", "label": "先放着", "effect": "不影响线上。"}],
                 if_no_answer="不影响线上。")


def _decision_after_rollback(r: Readings):
    codes = r.last_metrics_reason_codes
    hint = ""
    if "SPEND_RECONCILIATION_FAILED" in codes:
        hint = "上次见过：探针模型欠费（402）。先确认探针模型在旧版上能用。"
    elif "USER_FAILURE_RATE" in codes:
        hint = "上次见过：巡检号 / 杭州凭据扫坏模型被算进新版。先看失败是不是集中在这几把 key。"
    return _card("自动回退了，怎么办？",
                 f"止损触发（{', '.join(codes) or '未知'}），全部流量已回旧版。{hint}",
                 "noop",
                 [{"action": "close_run", "label": "关掉这轮（准备重开）", "effect": "归档；之后可以重新开始试点。"},
                  {"action": "noop", "label": "先看证据", "effect": "什么都不做，线上全在旧版，安全。"}],
                 if_no_answer="线上全在旧版，安全。")


def _decision_hold(r: Readings, fails: list[str]):
    return _card("停在当前档，需要你看",
                 "；".join(fails),
                 "noop",
                 [{"action": "noop", "label": "继续观察", "effect": "停在当前档不动，止损照常。问题自己消失后机器会继续。"},
                  {"action": "global_rollback", "label": "全部退回旧版", "effect": "所有用户立刻回旧版本；本轮之后要重开。"},
                  {"action": "pause_auto", "label": "关掉自动推进", "effect": "以后每一档都等你点。"}])


def _decision_inconclusive(r: Readings, unsure: list[str]):
    return _card("证据一直不够",
                 "；".join(unsure),
                 "noop",
                 [{"action": "noop", "label": "继续等", "effect": "不动。"},
                  {"action": "ramp_force", "label": "人工放行下一档", "effect": "你确认后升一档；门禁仍会检查，不过就拒。"},
                  {"action": "global_rollback", "label": "全部退回旧版", "effect": "所有用户回旧版本。"}])


def _decision_ramp_manual(cur: int, nxt: int):
    return _card(f"升到 {nxt}%？", f"{cur}% 的条件都满足了。", "ramp",
                 [{"action": "ramp", "arg": nxt, "label": f"升到 {nxt}%", "effect": f"按 key 哈希再多放一批用户到新版。"},
                  {"action": "noop", "label": "再看看", "effect": "停在当前档。"}])


def _decision_converge():
    return _card("把生产本体换成新版本？",
                 "100% 用户已在新版上稳定。下一步把生产的 4 台机器原地升级成和新版逐字节一样（升级期间流量全在新版热备上）。",
                 "converge",
                 [{"action": "converge", "label": "开始换生产", "effect": "约 15 分钟；每台机器换完核对文件；不对就停，不切流量。"},
                  {"action": "noop", "label": "先不换", "effect": "继续由新版热备扛 100%，旧生产空着。"}])


def _decision_commit():
    return _card("把流量切回已升级的生产？",
                 "生产已经换成新版本并通过核对。最后一步：流量从热备切回生产。",
                 "commit",
                 [{"action": "commit", "label": "切回生产（完成升级）", "effect": "流量回到生产 4 台；热备保留做回退。"},
                  {"action": "noop", "label": "先不切", "effect": "流量继续在热备上。"}])


def _decision_manual(r: Readings, why: str):
    return _card("机器看不懂当前状态", why, "noop",
                 [{"action": "noop", "label": "我来手动处理", "effect": "驾驶台不做任何动作。"}])
