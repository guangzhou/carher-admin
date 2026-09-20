#!/usr/bin/env python3
"""灰度升级进度台账 —— 把「现在第几环、走过哪些、还剩几环、还要多久」从人的记忆里搬到磁盘上。

为什么需要它（2026-09-19 那一轮的实测）：

  整轮 126.0 小时。其中 1% → 100% 的五档放量只用了 **83 分钟**，
  convergence → committed 只用了 **19 分钟**。剩下 124 小时里，
  09-14 14:13 → 09-18 21:20 的 **79 小时零代数零提交**：不是在观察，
  是停在那里没人知道下一步该干什么。

  同一轮里 5%/10%/50% 三档各只跑了 **2 个监控周期**，而绝对 5xx 止损腿
  深度是 4（`metrics.py: ABS_SUSTAIN_WINDOWS`）—— 那三档的止损腿从来
  没有武装过，而门禁全绿。没人看得出来，因为没有任何地方把
  「这一档停了多久 / 观察了几个窗口 / 够不够」打印出来。

所以这个工具做两件事，都只读，不碰路由状态，不发网络请求：

  1. 从 `$GRAY_ROOT/generations/` 重建完整时间轴。每次状态变更都会新建一个
     `g<UTC 时间戳>-<pid>-<rand>/` 目录且从不清理，所以进度本来就在磁盘上，
     只是从来没人读出来。
  2. 把剩余时间算出来，并且**分开算**：机器时间（放量档位的最小观察时长，
     算术推导）和人签时间（等审批，不可预测）。把两者混成一个数字就是编。

ETA 的分母不许猜：放量一档的下限 = `--min-cycles` × `--cycle-interval-seconds`，
这两个值都从执行单里抄，跟 `check-monitor-continuity.py` 用的是同一对。
非放量环节用上一轮实测值，并标明是实测。没有实测就打印「未测」，不编。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, NoReturn


TOOL = "gray-progress"
SCHEMA_VERSION = 1
CST = dt.timezone(dt.timedelta(hours=8))
GEN_RE = re.compile(r"^g(\d{8}T\d{6}Z)-\d+-\d+$")
INITIAL_RE = re.compile(r"^(?P<run>[A-Za-z0-9._:-]+)-g0*1$")

# 放量一档最少要观察几个监控周期。4 来自 metrics.py 的 ABS_SUSTAIN_WINDOWS：
# 绝对 5xx 止损腿要连续 4 个窗口同向越线才促发，所以停留不到 4 个周期的那一档，
# 止损腿在物理上不可能报——门禁却是绿的。改这个数要同时改 metrics.py，
# 否则台账会说「够了」而止损腿仍然熄灯。
DEFAULT_MIN_CYCLES = 4
DEFAULT_CYCLE_INTERVAL = 300

RAMP_LADDER = ("1", "5", "10", "50", "100")

# 环节清单。这是「一共多少环」的唯一定义，顺序即执行顺序。
#
# kind 决定剩余时间怎么算，不许混：
#   machine — 时长由算术决定（放量档位 = min_cycles × cycle_interval），可预测
#   human   — 时长由等人签决定，不可预测；只报上一轮实测，不进 ETA 下限
#   gate    — 事务本身，秒级；计 0
#
# measured_seconds 是 2026-09-19 那一轮从 generations/ 目录名实测出来的停留时长。
# 它是「参考」不是「承诺」：79 小时的空转也在那一轮里，所以人签环节的实测值
# 只说明「上次花了这么久」，不说明「这次也这么久」。
STAGES: tuple[dict[str, Any], ...] = (
    {"key": "preflight", "label": "① 预检：产物冻结 + 可观测性自检", "kind": "human",
     "phase": "preflight", "split": None, "measured_seconds": 1010 * 60,
     "note": "含 observability-preflight.py 五条腿；尺子先得活着再谈门禁"},
    {"key": "bridge", "label": "② guarded-old 保险丝（可跳过）", "kind": "human",
     "phase": "bridge_verified", "split": None, "measured_seconds": 282 * 60,
     "optional": True, "note": "只在需要给旧 prod 加保险丝时走"},
    # ③ 和 ④ 的 (phase, split) 是**同一对**：都是 normal_gray / 0%。光看状态机分不开，
    # 只能看 key-sid.map 有没有行 —— 0 行 = 还没放任何人进来（③），有行 = 已经在试点（④）。
    # 不加这一维的后果实测过：④ 永远命中不到，一次干净的升级也会把它报成「跳过⚠」。
    {"key": "gray_entry", "label": "③ 进入 normal_gray（0% 流量）", "kind": "gate",
     "phase": "normal_gray", "split": "0", "keys_enrolled": False,
     "measured_seconds": 0,
     "note": "本身不动流量；在这里冻结 0% 基线"},
    {"key": "named_keys", "label": "④ 指名 key 试点", "kind": "human",
     "phase": "normal_gray", "split": "0", "keys_enrolled": True,
     "measured_seconds": 5087 * 60,
     "note": "上一轮这一环含 79 小时空转，实测值不可当预算"},
    *(
        {"key": f"split_{p}", "label": f"⑤.{i} 放量 {p}%", "kind": "machine",
         "phase": "normal_gray", "split": p, "measured_seconds": m,
         "note": note}
        for i, (p, m, note) in enumerate(
            (
                ("1", 49 * 60, "上一轮 49min / 4 周期，够深度 4"),
                ("5", 9 * 60, "上一轮仅 9min / 2 周期 —— 止损腿没武装"),
                ("10", 15 * 60, "上一轮仅 15min / 2 周期 —— 止损腿没武装"),
                ("50", 13 * 60, "上一轮仅 13min / 2 周期 —— 止损腿没武装；本档起需容量+bridge 证据"),
                ("100", 1073 * 60, "上一轮 1073min / 139 周期"),
            ),
            start=1,
        )
    ),
    {"key": "convergence_ready", "label": "⑥ 准备收敛（冻结路由）", "kind": "gate",
     "phase": "convergence_ready", "split": "100", "measured_seconds": 6 * 60},
    {"key": "prod_offline", "label": "⑦ 离线升级旧 prod", "kind": "machine",
     "phase": "prod_offline_upgrading", "split": "100", "measured_seconds": 12 * 60,
     "note": "含删除集 gate + Pod spec 形状 gate"},
    {"key": "prod_verified", "label": "⑧ 新 prod 验证通过", "kind": "gate",
     "phase": "prod_verified", "split": "100", "measured_seconds": 1 * 60},
    {"key": "committed", "label": "⑨ 提交：新版本成为稳定版", "kind": "gate",
     "phase": "committed", "split": None, "measured_seconds": 0},
)

TERMINAL_PHASES = {
    "committed", "rolled_back", "aborted", "post_commit_rolled_back",
}

# 异常终态：这些 phase 出现时台账不许继续报「还剩 N 环」，那是在假装升级还在轨道上。
ABORTED_PHASES = {"rolled_back", "aborted", "post_commit_rolled_back"}


def fail(message: str) -> NoReturn:
    raise SystemExit(f"{TOOL}: {message}")


def read_state(directory: Path) -> dict[str, str] | None:
    """读一个 generation 目录的 state.env。读不到返回 None，不抛 —— 台账缺一行
    比整个台账打不出来好。"""
    path = directory / "state.env"
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    out: dict[str, str] = {}
    for line in raw.splitlines():
        key, _, value = line.partition("=")
        if key:
            out[key.strip()] = value.strip()
    return out or None


def count_lines(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def generation_time(name: str, directory: Path) -> tuple[int, dt.datetime] | None:
    """(排序档, 进入时刻)。排序档必须显式，不能只靠时间戳。

    后续每一代目录名里都带 UTC 时间戳，直接用。初代目录叫 `<run_id>-g000001`，
    名字里**没有**时间戳，只能退回 mtime —— 而 mtime 是可以被 `cp -p`、恢复、
    重打包改掉的，一旦它晚于真实的第二代，整条时间轴就会被重排，
    「当前在第几环」直接读错（这个 bug 在合成对照里真的发生了）。

    所以初代恒定排在档 0：它叫 g000001，按构造就是第一个，这件事不需要
    时间戳来证明。mtime 只用来填它的显示时刻。
    """
    matched = GEN_RE.match(name)
    if matched:
        return (
            1,
            dt.datetime.strptime(matched.group(1), "%Y%m%dT%H%M%SZ").replace(
                tzinfo=dt.timezone.utc
            ),
        )
    if INITIAL_RE.match(name):
        try:
            return (0, dt.datetime.fromtimestamp(directory.stat().st_mtime, dt.timezone.utc))
        except OSError:
            return None
    return None


def build_timeline(generations: Path, run_id: str | None) -> list[dict[str, Any]]:
    """把 generations/ 折叠成状态变更时间轴。

    一次状态变更生成一代，但**同状态**也会生成一代（改名单、重渲染）。
    上一轮 674 代里只有 13 次真的状态变更，其余 661 代是逐个 key 加灰度。
    所以折叠的键是 (phase, split, 名单空不空)，同时记住这一段压了多少代 ——
    那个数字本身是信息：662 代 x 同一个状态 = 有人在一个一个手工加 key。

    「名单空不空」必须进折叠键：③（进 normal_gray）和 ④（指名 key 试点）的
    phase/split 完全相同，唯一的区别就是 key-sid.map 有没有行。少了这一维，
    两环会被折成一段，④ 在任何一轮里都显示成「跳过」。
    """
    if not generations.is_dir():
        fail(f"generations 目录不存在：{generations}")
    entries: list[tuple[int, dt.datetime, str, dict[str, str], int]] = []
    for name in sorted(os.listdir(generations)):
        directory = generations / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        ordered = generation_time(name, directory)
        if ordered is None:
            continue
        rank, stamp = ordered
        state = read_state(directory)
        if state is None:
            continue
        if run_id is not None and state.get("run_id") != run_id:
            continue
        entries.append((rank, stamp, name, state, count_lines(directory / "key-sid.map")))
    entries.sort(key=lambda item: (item[0], item[1]))

    # 初代的时刻来自 mtime，可能被改得比第二代还晚。位置由 rank 钉住了，
    # 但时刻要夹一下，否则第一段会算出负的停留时长。
    if len(entries) > 1 and entries[0][0] == 0 and entries[0][1] > entries[1][1]:
        rank, _, name, state, keys = entries[0]
        entries[0] = (rank, entries[1][1], name, state, keys)

    timeline: list[dict[str, Any]] = []
    for _rank, stamp, name, state, keys in entries:
        signature = (state.get("phase"), state.get("split"), bool(keys))
        if timeline and timeline[-1]["_signature"] == signature:
            timeline[-1]["generations"] += 1
            timeline[-1]["last_generation"] = name
            timeline[-1]["keys_at_end"] = keys
            continue
        timeline.append(
            {
                "_signature": signature,
                "entered_at": stamp,
                "phase": state.get("phase"),
                "split": state.get("split"),
                "run_id": state.get("run_id"),
                "first_generation": name,
                "last_generation": name,
                "generations": 1,
                "keys_at_end": keys,
                # 折叠键的一部分，段内恒定：进这一段时名单是不是空的。
                "keys_enrolled": bool(keys),
            }
        )
    for index, item in enumerate(timeline):
        nxt = timeline[index + 1]["entered_at"] if index + 1 < len(timeline) else None
        item["left_at"] = nxt
        item["duration_seconds"] = (
            int((nxt - item["entered_at"]).total_seconds()) if nxt else None
        )
    return timeline


def read_heartbeat(path: Path) -> list[dt.datetime]:
    """监控周期完成时刻。这是「这一档观察了几个窗口」的唯一证据源 ——
    metrics evidence 文件只证明跑过的周期，跑没跑够要数这个。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    stamps: list[dt.datetime] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            text = str(record["cycle_completed_at"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        text = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            stamp = dt.datetime.fromisoformat(text)
        except ValueError:
            continue
        if stamp.tzinfo is not None:
            stamps.append(stamp.astimezone(dt.timezone.utc))
    stamps.sort()
    return stamps


def stage_index_for(
    phase: str | None, split: str | None, keys: int | None = None,
) -> int | None:
    """当前状态落在第几环。匹配不上返回 None —— 不许猜成最近的一环，
    那会把「状态机跑到了台账没写的地方」藏成一个看起来正常的进度条。

    `keys` 是该代 key-sid.map 的行数，只有 ③/④ 这对同 (phase, split) 的环需要它来分开。
    拿不到行数（keys is None）时退回只按 (phase, split) 匹配，会命中两者中靠前的那个。
    """
    for index, stage in enumerate(STAGES):
        if stage["phase"] != phase:
            continue
        if stage["split"] is not None and stage["split"] != split:
            continue
        wants = stage.get("keys_enrolled")
        if wants is not None and keys is not None and bool(keys) != wants:
            continue
        return index
    return None


def visited_stage_indexes(timeline: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """走过的环 → 该环在时间轴上的第一段。同一环可能被走过多次（回退再进），
    取第一次进入的时刻，但停留时长累加所有段。"""
    visited: dict[int, dict[str, Any]] = {}
    for item in timeline:
        index = stage_index_for(
            item["phase"], item["split"], int(item["keys_enrolled"])
        )
        if index is None:
            continue
        slot = visited.setdefault(
            index,
            {"entered_at": item["entered_at"], "duration_seconds": 0,
             "generations": 0, "segments": 0, "open": False},
        )
        slot["segments"] += 1
        slot["generations"] += item["generations"]
        if item["duration_seconds"] is None:
            slot["open"] = True
        else:
            slot["duration_seconds"] += item["duration_seconds"]
    return visited


def cycles_between(
    stamps: list[dt.datetime], start: dt.datetime, end: dt.datetime | None
) -> int:
    limit = end or dt.datetime.now(dt.timezone.utc)
    return sum(1 for stamp in stamps if start <= stamp < limit)


def machine_floor_seconds(stage: dict[str, Any], min_cycles: int, interval: int) -> int:
    """一环的机器时间下限。放量档位是算术：min_cycles × interval。

    非放量的 machine 环（离线升级 prod）没有这样的算术，只有上一轮实测；
    照实报实测，并在输出里标明来源，不要把实测冒充成下限。

    human / gate 环恒 0：等人签没有下限可言。它们上一轮的实测值另有
    `measured_last_run_seconds` 那一栏承载 —— 把实测填进「机器时间下限」这个名字里，
    等于说「①预检至少还要 16.8 小时」，那不是下限，那是上一轮等人的时长。
    """
    if stage["kind"] != "machine":
        return 0
    if stage["key"].startswith("split_"):
        return min_cycles * interval
    return int(stage.get("measured_seconds") or 0)


def humanize(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    minutes, second = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}min" if second == 0 else f"{minutes}min{second}s"
    hours, minute = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minute:02d}min"
    day, hour = divmod(hours, 24)
    return f"{day}d{hour:02d}h{minute:02d}min"


def local(stamp: dt.datetime | None) -> str:
    """一律北京时间显示。磁盘上存的是 UTC，人看的是 CST，两者不能混。"""
    if stamp is None:
        return "—"
    return stamp.astimezone(CST).strftime("%m-%d %H:%M")


def assemble(args: argparse.Namespace) -> dict[str, Any]:
    now = dt.datetime.now(dt.timezone.utc)
    timeline = build_timeline(args.generations, args.run_id)
    if not timeline:
        fail(
            "generations 目录里没有可读的 run。--run-id 过滤掉了全部？"
            "先跑 gray-phase.sh current 看 active 是哪个 run。"
        )
    run_id = args.run_id or timeline[-1]["run_id"]
    timeline = [item for item in timeline if item["run_id"] == run_id]

    stamps = read_heartbeat(args.ledger) if args.ledger else []
    current = timeline[-1]
    started_at = timeline[0]["entered_at"]
    current_index = stage_index_for(
        current["phase"], current["split"], int(current["keys_enrolled"])
    )
    visited = visited_stage_indexes(timeline)

    min_cycles = args.min_cycles
    interval = args.cycle_interval_seconds

    # 「已经走到哪」的分界线。正常情况就是当前环；但终态 phase（rolled_back /
    # aborted）本身不在环节清单里，current_index 是 None —— 此时分界线是走过的最深
    # 那一环，否则整张表会一个「跳过」都认不出来，头行也只会说「不在清单内」，
    # 看不出是死在第几环。
    frontier = current_index
    if frontier is None and visited:
        frontier = max(visited)

    stages: list[dict[str, Any]] = []
    for index, stage in enumerate(STAGES):
        seen = visited.get(index)
        if index == current_index:
            status = "当前"
        elif seen is not None:
            status = "已过"
        elif frontier is not None and index < frontier:
            # 跳过的环。optional 的跳过是设计，非 optional 的跳过是异常，要看得见。
            status = "跳过" if stage.get("optional") else "跳过⚠"
        else:
            status = "未到"

        floor = machine_floor_seconds(stage, min_cycles, interval)
        entry: dict[str, Any] = {
            "index": index + 1,
            "key": stage["key"],
            "label": stage["label"],
            "kind": stage["kind"],
            "status": status,
            "phase": stage["phase"],
            "split": stage["split"],
            "entered_at": None,
            "elapsed_seconds": None,
            "generations": 0,
            "cycles_observed": None,
            "cycles_required": min_cycles if stage["key"].startswith("split_") else None,
            "machine_floor_seconds": floor,
            "measured_last_run_seconds": stage.get("measured_seconds"),
            "note": stage.get("note"),
            "warnings": [],
        }
        if seen is not None:
            entry["entered_at"] = seen["entered_at"].isoformat().replace("+00:00", "Z")
            entry["generations"] = seen["generations"]
            elapsed = seen["duration_seconds"]
            if seen["open"]:
                elapsed += int((now - _last_entry_at(timeline, index)).total_seconds())
            entry["elapsed_seconds"] = elapsed
            if stamps and stage["key"].startswith("split_"):
                entry["cycles_observed"] = _cycles_for_stage(timeline, index, stamps, now)
        stages.append(entry)

    _attach_warnings(stages, frontier, min_cycles, interval, bool(stamps))
    remaining = _remaining(stages, current_index, current["phase"])

    return {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "run_id": run_id,
        "generation": _active_generation(timeline),
        "phase": current["phase"],
        "split": current["split"],
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "elapsed_total_seconds": int((now - started_at).total_seconds()),
        "current_stage_index": (current_index + 1) if current_index is not None else None,
        "deepest_stage_index": (frontier + 1) if frontier is not None else None,
        "total_stages": len(STAGES),
        "cycle_interval_seconds": interval,
        "min_cycles_per_ramp": min_cycles,
        "heartbeat_ledger": str(args.ledger) if args.ledger else None,
        "heartbeat_cycles_total": len(stamps),
        "stages": stages,
        **remaining,
    }


def _last_entry_at(timeline: list[dict[str, Any]], index: int) -> dt.datetime:
    """该环最后一段的进入时刻 —— 用来给还没离开的那一段算 elapsed。"""
    for item in reversed(timeline):
        if stage_index_for(
            item["phase"], item["split"], int(item["keys_enrolled"])
        ) == index:
            return item["entered_at"]
    return timeline[-1]["entered_at"]


def _cycles_for_stage(
    timeline: list[dict[str, Any]], index: int, stamps: list[dt.datetime],
    now: dt.datetime,
) -> int:
    total = 0
    for item in timeline:
        if stage_index_for(
            item["phase"], item["split"], int(item["keys_enrolled"])
        ) != index:
            continue
        total += cycles_between(stamps, item["entered_at"], item["left_at"] or now)
    return total


def _active_generation(timeline: list[dict[str, Any]]) -> str:
    return str(timeline[-1]["last_generation"])


def _attach_warnings(
    stages: list[dict[str, Any]], current_index: int | None, min_cycles: int,
    interval: int, have_ledger: bool,
) -> None:
    """把「看起来过了其实没观察够」变成台账里能看见的一行。

    这是上一轮真实发生过的事：5%/10%/50% 各只跑了 2 个周期，深度 4 的绝对 5xx
    止损腿在物理上不可能促发，而 check-monitor-continuity.py 只查缺口不查**数量**，
    所以门禁全绿。现在它至少会在台账里写着。
    """
    for stage in stages:
        if not stage["key"].startswith("split_"):
            continue
        if stage["status"] not in {"已过", "当前"}:
            continue
        if not have_ledger:
            stage["warnings"].append(
                "无 heartbeat 台账，观察窗口数量无从证明 —— 给 --ledger"
            )
            continue
        observed = stage["cycles_observed"]
        if observed is None:
            continue
        if observed < min_cycles:
            stage["warnings"].append(
                f"只观察了 {observed} 个周期 < 要求 {min_cycles} 个"
                f"（{humanize(min_cycles * interval)}）：深度 {min_cycles} 的止损腿"
                "在这一档没有武装过"
            )
    if current_index is None:
        return
    for stage in stages[:current_index]:
        if stage["status"] == "跳过⚠":
            stage["warnings"].append("这一环被跳过了，且它不是 optional")


def _remaining(
    stages: list[dict[str, Any]], current_index: int | None, phase: str | None,
) -> dict[str, Any]:
    """剩余环数与剩余时间。

    机器时间和人签时间**分开报**。把等审批的时间折进 ETA 就是编一个自己都不信的
    数字：上一轮「指名 key 试点」那一环实测 84.8 小时，其中 79 小时是没人动。
    所以 ETA 的正确形状是「机器至少还要 X」+「另有 N 个人签环节待批」。
    """
    if phase in ABORTED_PHASES:
        return {
            "terminal": True,
            "outcome": phase,
            "remaining_stages": 0,
            "remaining_machine_seconds": 0,
            "remaining_human_stages": 0,
            "eta_note": f"已终止于 {phase}，不再报剩余环节",
        }
    if phase == "committed":
        return {
            "terminal": True,
            "outcome": "committed",
            "remaining_stages": 0,
            "remaining_machine_seconds": 0,
            "remaining_human_stages": 0,
            "eta_note": "已提交：新版本就是稳定版",
        }
    if current_index is None:
        return {
            "terminal": False,
            "outcome": None,
            "remaining_stages": None,
            "remaining_machine_seconds": None,
            "remaining_human_stages": None,
            "eta_note": (
                f"phase={phase} 不在台账的环节清单里 —— 先修台账，"
                "不要相信任何进度数字"
            ),
        }
    ahead = stages[current_index + 1 :]
    machine = sum(
        int(stage["machine_floor_seconds"] or 0)
        for stage in ahead
        if stage["kind"] == "machine"
    )
    # 当前这一环如果是放量档且还没观察够，缺的那几个周期也要计进去。
    current = stages[current_index]
    if current["kind"] == "machine" and current["cycles_observed"] is not None:
        short = max(0, int(current["cycles_required"] or 0) - current["cycles_observed"])
        if short:
            machine += short * (
                int(current["machine_floor_seconds"] or 0)
                // max(1, int(current["cycles_required"] or 1))
            )
    human = [stage for stage in ahead if stage["kind"] == "human" and stage["status"] != "跳过"]
    return {
        "terminal": False,
        "outcome": None,
        "remaining_stages": len(ahead),
        "remaining_machine_seconds": machine,
        "remaining_human_stages": len(human),
        "eta_note": (
            f"机器时间下限 {humanize(machine)}；另有 {len(human)} 个环节等人签，"
            "人签时长不可预测，不计入下限"
        ),
    }


MARK = {"已过": "✓", "当前": "▶", "未到": "·", "跳过": "○", "跳过⚠": "⚠"}


def render(report: dict[str, Any]) -> str:
    lines: list[str] = []
    total = report["total_stages"]
    current = report["current_stage_index"]
    done = sum(1 for s in report["stages"] if s["status"] == "已过")
    bar = "".join(
        MARK.get(stage["status"], "?") for stage in report["stages"]
    )

    lines.append("═" * 72)
    lines.append(f"灰度升级进度  run_id={report['run_id']}")
    lines.append("═" * 72)
    if current:
        head = f"  当前：第 {current}/{total} 环"
    elif report.get("deepest_stage_index"):
        # phase 不在清单里（终态或状态机跑偏）。报「走到过第几环」而不是只说
        # 「不在清单内」—— 回滚发生在第几环是这张表最该回答的问题。
        head = f"  当前：{report['phase']}（不是环节）；最深走到第 {report['deepest_stage_index']}/{total} 环"
    else:
        head = f"  当前：不在清单内（共 {total} 环）"
    lines.append(
        f"{head}   phase={report['phase']} split={report['split']}%"
    )
    # 剩余环数拿不到时印「未知」而不是 None —— None 是「我没算」，不是一个环数。
    left = report["remaining_stages"]
    lines.append(
        f"  进度条：{bar}   已过 {done} 环，"
        + (f"剩 {left} 环" if left is not None else "剩几环未知")
    )
    lines.append(
        f"  开始于 {local(dt.datetime.fromisoformat(report['started_at'].replace('Z','+00:00')))}"
        f"（北京时间）   已耗时 {humanize(report['elapsed_total_seconds'])}"
    )
    if report.get("terminal"):
        lines.append(f"  终态：{report['outcome']} —— {report['eta_note']}")
    else:
        lines.append(f"  剩余：{report['eta_note']}")
    lines.append(
        f"  监控节奏：每 {report['cycle_interval_seconds']}s 一周期，"
        f"每档至少 {report['min_cycles_per_ramp']} 周期"
        f"（= {humanize(report['min_cycles_per_ramp'] * report['cycle_interval_seconds'])}）；"
        f"台账共 {report['heartbeat_cycles_total']} 周期"
    )
    lines.append("")
    lines.append("  状态 环节                                进入      耗时      观察窗口")
    lines.append("  ──── ─────────────────────────────────── ───────── ───────── ────────")
    for stage in report["stages"]:
        mark = MARK.get(stage["status"], "?")
        cycles = "—"
        if stage["cycles_required"]:
            got = stage["cycles_observed"]
            cycles = f"{'?' if got is None else got}/{stage['cycles_required']}"
        label = stage["label"]
        if len(label) > 34:
            label = label[:33] + "…"
        lines.append(
            f"  {mark} {stage['status']:<4s} {label:<35s} "
            f"{local(dt.datetime.fromisoformat(stage['entered_at'].replace('Z','+00:00'))) if stage['entered_at'] else '—':<9s} "
            f"{humanize(stage['elapsed_seconds']):<9s} {cycles}"
        )
        for warning in stage["warnings"]:
            lines.append(f"       ⚠ {warning}")
    lines.append("")
    lines.append("  图例：✓已过  ▶当前  ·未到  ○跳过(设计)  ⚠跳过(非预期)")
    lines.append("═" * 72)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    root = Path(os.environ.get("GRAY_ROOT", "/var/lib/litellm-gray-rollout"))
    parser.add_argument(
        "--generations", type=Path, default=root / "generations",
        help="generation 目录（默认 $GRAY_ROOT/generations）",
    )
    parser.add_argument(
        "--ledger", type=Path,
        default=Path(os.environ.get("GRAY_GATE_EVIDENCE_DIR", str(root / "evidence")))
        / "monitor-heartbeat.jsonl",
        help="gray-monitor-cycle.sh 的 heartbeat 台账；没有它就证明不了观察窗口数",
    )
    parser.add_argument("--run-id", help="只看这个 run（默认取最后活动的那个）")
    parser.add_argument(
        "--cycle-interval-seconds", type=int, default=DEFAULT_CYCLE_INTERVAL,
        help="监控周期间隔，从执行单抄，跟 check-monitor-continuity.py 用同一个值",
    )
    parser.add_argument(
        "--min-cycles", type=int, default=DEFAULT_MIN_CYCLES,
        help=f"每档最少观察几个周期（默认 {DEFAULT_MIN_CYCLES}，"
             "= metrics.py 的 ABS_SUSTAIN_WINDOWS）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")
    args = parser.parse_args()

    if args.cycle_interval_seconds < 1:
        fail("--cycle-interval-seconds 必须为正")
    if args.min_cycles < 1:
        fail("--min-cycles 必须为正")
    if args.ledger is not None and not args.ledger.exists():
        args.ledger = None

    report = assemble(args)
    if args.json:
        json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True, indent=2)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render(report) + "\n")
    # 台账是只读观测，不是门禁：永远 exit 0。要红请用
    # check-monitor-continuity.py（放量前那道），台账只负责让人看见。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
