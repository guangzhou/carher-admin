#!/usr/bin/env python3
"""
cursor_queue_diag_patch.py — 排队卡死「一次性诊断」补丁(只加观测,不改行为)。

═══ 为什么要它(2026-08-25 二轮:连发中间条静默不回复)═══
线上实测:用户一次排队发多条,中间某几条(如"纽约天气")**根本不回复**——不是答错,是没答。
结构化日志里 20 个 turn 全 outcome=success、没有 aborted,说明那几条**大概率压根没变成 turn
(没被派发)**,连失败记录都没有。当前 v3 泵(@cx-queue-pump:v3)在场且每轮都在 heal 僵尸
generating(hadUUID 全 true),但**为什么中间条还是漏派发,磁盘/结构化日志看不到队列本身**
(不打 addToQueue/dispatch 遥测)。四条候选机制无法凭 grep 分辨:
  ① 泵每 tick 无条件调 tryDispatchNextQueueItem,若不自守卫→在上条还在生成时硬派发下条→掐掉上条;
  ② tryDispatchNextQueueItem 先出队再派发,派发撞 "Failed to resolve hook model legacy slug"
     (线上报 27 次)→ 出队了却没跑起来 = 静默丢;
  ③ 泵某 tick 队列瞬间见底就停(不再 reschedule),晚到那条没人再泵;
  ④ 连发那条在 start-submit-chat 关卡被 "Composer is generating" 挡回,从没进队。

本补丁把当前 v3 snippet **原地换成 v3diag**:自愈条件与 v3 逐字节等价(仍 3-tick 僵尸判活、
真生成绝不碰),只在派发**前后**各读一次队列,每 tick 打:
  status / hasUUID / bubbleIds / inflight(streamingAbortControllers.size,真生成计数)/
  qlenBefore / qlenAfter / dispatched(前后差)/ qids(队列里每条 id,跨 tick 追踪具体哪条)。
判据:
  - dispatched=1 且 inflight>=1(派发时已有在飞)→ 机制①硬派发;
  - dispatched=1 但随后无新 inflight/无完成、该 qid 消失 → 机制②出队即丢;
  - qlen>0 却 tick 停(n→120 或最后一行 qlen>0 后无后续)→ 机制③泵停;
  - burst 中 qlen 峰值远低于发送条数(发 8 条 qlen 从没超过 ~2)→ 机制④没进队(关卡挡回)。

═══ 用法(--apply 需先 ⌘Q 退出 Cursor;bundle 只在窗口启动时加载)═══
  python3 cursor_queue_diag_patch.py            # dry-run:验证能找到 v3 snippet(Cursor 开着也能跑)
  python3 cursor_queue_diag_patch.py --apply    # 原地替换 v3→v3diag(自动备份;需先退出 Cursor)
  python3 cursor_queue_diag_patch.py --revert   # 从本脚本备份回滚(还原成 v3)

打完重启 Cursor → 复现「一次排队发 8 条(带上"纽约天气")」→ 看日志:
  LOGDIR=~/Library/Application\\ Support/Cursor/logs
  grep -h "cx-queue-diag" "$LOGDIR"/*/window*/exthost/anysphere.cursor-always-local/*Structured*.log | tail -80
把 tick 行贴回来即可定型。诊断完 --revert 换回 v3(或直接上精确修复版)。
"""
import argparse, json, os, re, shutil, subprocess, sys, time

APP = os.environ.get("CURSOR_APP", "/Applications/Cursor.app")
BUNDLES = [
    "Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js",
    "Contents/Resources/app/out/vs/workbench/workbench.glass.main.js",
]
BACKUP_ROOT = os.path.expanduser("~/.cursor-queue-diag-backup")

# ── 当前 v3 pump 原文(逐字节;来自 cursor_team_setup.py / cursor_queue_pump_patch.py 的 QP_SNIPPET)──
# find-replace 全靠这个字节精确;若线上不是这一份→命中 !=1→拒绝(fail-safe)。
V3_MARKER = "@cx-queue-pump:v3"
V3_SNIPPET = (
    "/*" + V3_MARKER + "*/try{if(this._cxQP===void 0){let _cxN=0,_cxS=0;const _cxT=()=>{"
    "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;"
    "const _h=this.getComposerHandleIfLoaded();"
    "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
    'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){'
    "const _u=_d.chatGenerationUUID;"
    "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;"
    "const _stale=_u===void 0||(_m&&typeof _m.has===\"function\"&&!_m.has(_u));"
    "_cxS=_stale?_cxS+1:0;"
    "if(_u===void 0||_cxS>=3){"
    "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",chatGenerationUUID:void 0,generatingBubbleIds:[]});"
    'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}'
    "else{_cxS=0}"
    "this.tryDispatchNextQueueItem();"
    "if(this.getQueueItems().length>0&&++_cxN<120){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
    "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
)

# ── 旧 v3diag(@cx-queue-diag:v3):已部署在 live bundle;v3.2 apply 时把它(或 v3 泵)原地换掉 ──
OLD_DIAG_V3_SNIPPET = (
    "/*@cx-queue-diag:v3*/try{if(this._cxQP===void 0){let _cxN=0,_cxS=0;const _cxT=()=>{"
    "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;"
    "const _h=this.getComposerHandleIfLoaded();"
    "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
    'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){'
    "const _u=_d.chatGenerationUUID;"
    "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;"
    "const _stale=_u===void 0||(_m&&typeof _m.has===\"function\"&&!_m.has(_u));"
    "_cxS=_stale?_cxS+1:0;"
    "if(_u===void 0||_cxS>=3){"
    "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",chatGenerationUUID:void 0,generatingBubbleIds:[]});"
    'this.structuredLogService.info("composer","[cx-queue-diag] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}'
    "else{_cxS=0}"
    "var _qb;try{_qb=this.getQueueItems()}catch(_e){_qb=[]}"
    "var _qids;try{_qids=_qb.map(function(q){return q&&(q.id||q.bubbleId||q.messageId||q.requestId)||\"?\"})}catch(_e){_qids=[]}"
    "var _if;try{var _mm=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;_if=_mm&&_mm.size!==void 0?_mm.size:-1}catch(_e){_if=-2}"
    "this.tryDispatchNextQueueItem();"
    "try{var _qa=this.getQueueItems();this.structuredLogService.info(\"composer\",\"[cx-queue-diag] tick\","
    "{composerId:this.composerId,n:_cxN,status:(_d&&_d.status)||null,hasUUID:!!(_d&&_d.chatGenerationUUID),"
    "bubbleIds:((_d&&_d.generatingBubbleIds)||[]).length,inflight:_if,"
    "qlenBefore:_qb.length,qlenAfter:_qa.length,dispatched:_qb.length-_qa.length,qids:_qids})}catch(_e){}"
    "if(this.getQueueItems().length>0&&++_cxN<120){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
    "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
)

# ── v3.2diag:v3 自愈逐字节等价(3-tick 僵尸判活不变);唯一增量=每个排队项打 delivery 指纹 ──
#    (id|kind/sc=<bool>):判定 reconcileSteerItemsWhenIdle 会不会把该项当"已完成 steer"folded(removeFromQueue)。
#    这是写 fix 前差的最后一块数据——分辨"折叠且已投递(模型没答)"vs"折叠即丢(没喂给模型)"。
DIAG_MARKER = "@cx-queue-diag:v3.2"
V3DIAG_SNIPPET = (
    "/*" + DIAG_MARKER + "*/try{if(this._cxQP===void 0){let _cxN=0,_cxS=0;const _cxT=()=>{"
    "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;"
    "const _h=this.getComposerHandleIfLoaded();"
    "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
    # --- v3 自愈逻辑(与 V3_SNIPPET 等价,仅 heal 日志文案改 diag)---
    'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){'
    "const _u=_d.chatGenerationUUID;"
    "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;"
    "const _stale=_u===void 0||(_m&&typeof _m.has===\"function\"&&!_m.has(_u));"
    "_cxS=_stale?_cxS+1:0;"
    "if(_u===void 0||_cxS>=3){"
    "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",chatGenerationUUID:void 0,generatingBubbleIds:[]});"
    'this.structuredLogService.info("composer","[cx-queue-diag] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}'
    "else{_cxS=0}"
    # --- observe BEFORE dispatch(v3.2 增量:每项带 delivery 指纹 id|kind/sc=bool)---
    "var _qb;try{_qb=this.getQueueItems()}catch(_e){_qb=[]}"
    "var _qids;try{_qids=_qb.map(function(q){"
    "var _id=q&&(q.id||q.bubbleId||q.messageId||q.requestId)||\"?\";"
    "var _dl;try{_dl=(q&&q.delivery===void 0)?\"none\":(((q.delivery&&q.delivery.kind)||\"set\")+\"/sc=\"+!!(q.delivery&&q.delivery.serverConfirmed===true))}catch(_e){_dl=\"err\"}"
    "return _id+\"|\"+_dl})}catch(_e){_qids=[]}"
    "var _if;try{var _mm=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;_if=_mm&&_mm.size!==void 0?_mm.size:-1}catch(_e){_if=-2}"
    "this.tryDispatchNextQueueItem();"
    # --- observe AFTER dispatch ---
    "try{var _qa=this.getQueueItems();this.structuredLogService.info(\"composer\",\"[cx-queue-diag] tick\","
    "{composerId:this.composerId,n:_cxN,status:(_d&&_d.status)||null,hasUUID:!!(_d&&_d.chatGenerationUUID),"
    "bubbleIds:((_d&&_d.generatingBubbleIds)||[]).length,inflight:_if,"
    "qlenBefore:_qb.length,qlenAfter:_qa.length,dispatched:_qb.length-_qa.length,qids:_qids})}catch(_e){}"
    "if(this.getQueueItems().length>0&&++_cxN<120){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
    "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
)


def cursor_version():
    try:
        return json.load(open(os.path.join(APP, "Contents/Resources/app/package.json"))).get("version", "unknown")
    except Exception:
        return "unknown"


def cursor_running():
    r = subprocess.run(["pgrep", "-f", "Cursor.app/Contents/MacOS/Cursor"], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() != ""


def node_check(text, tag):
    p = "/tmp/cxdiag_%s.js" % tag
    open(p, "w").write(text)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    os.unlink(p)
    if r.returncode != 0:
        print("   !! node --check 失败(%s):%s" % (tag, r.stderr[:300])); return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--baseline", action="store_true",
                    help="纯原厂对照组:把队列补丁(v3泵/v3diag/v3.2)整段移除,保留 cursor-g 解锁。用 --revert 还原。")
    a = ap.parse_args()
    ver = cursor_version()
    print("Cursor version:", ver)

    if a.baseline:
        # 移除队列补丁 = 恢复 addToQueue 原样(无泵),验证「原厂连发到底折不折/卡不卡」。
        if a.apply and cursor_running():
            print("!! Cursor 正在运行 —— --apply 前请先 ⌘Q 完全退出。"); sys.exit(2)
        plans = []
        for rel in BUNDLES:
            p = os.path.join(APP, rel)
            src = open(p, encoding="utf8", errors="replace").read()
            hit = None
            for snip, tag in ((V3_SNIPPET, "v3泵"), (OLD_DIAG_V3_SNIPPET, "v3diag"), (V3DIAG_SNIPPET, "v3.2diag")):
                if src.count(snip) == 1:
                    hit = (snip, tag); break
            if hit is None:
                print("   !! %s 里找不到唯一队列补丁 snippet → 拒绝(可能已是纯原厂,或版本不符)" % rel); sys.exit(3)
            out = src.replace(hit[0], "", 1)
            if not node_check(out, os.path.basename(rel).split(".")[1]):
                print("   !! 语法校验不过,终止。"); sys.exit(4)
            plans.append((p, rel, out)); print("   将移除 %s(回纯原厂队列):" % hit[1], os.path.basename(rel), "(node --check OK)")
        if not a.apply:
            print("[dry-run] 通过。⌘Q 退出 Cursor 后加 `--baseline --apply` 执行(会先备份到 %s)。" % BACKUP_ROOT); return
        ts = time.strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(BACKUP_ROOT, "%s-%s-prebaseline" % (ver, ts)); os.makedirs(bdir, exist_ok=True)
        for p, rel, out in plans:
            shutil.copy2(p, os.path.join(bdir, os.path.basename(rel)))
            open(p, "w", encoding="utf8").write(out); print("baseline(已移除队列补丁):", rel)
        print("备份在:", bdir)
        print("完成。重启 Cursor → 同一会话连发 5–8 条 → 看聊天记录:每条是否各答一轮。还原:--revert"); return

    if a.revert:
        baks = sorted(os.listdir(BACKUP_ROOT)) if os.path.isdir(BACKUP_ROOT) else []
        if not baks:
            print("!! 无备份可回滚"); sys.exit(1)
        if cursor_running():
            print("!! Cursor 正在运行 —— 请先 ⌘Q 完全退出再回滚。"); sys.exit(2)
        b = os.path.join(BACKUP_ROOT, baks[-1])
        for rel in BUNDLES:
            src = os.path.join(b, os.path.basename(rel))
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(APP, rel)); print("restored:", rel)
        print("回滚完成(还原成 v3),重启 Cursor 生效。"); return

    # dry-run 不写盘,Cursor 开着也能验锚点;只有 --apply 才要求退出。
    if a.apply and cursor_running():
        print("!! Cursor 正在运行 —— --apply 前请先 ⌘Q 完全退出(否则 bundle 改动不生效/竞态)。"); sys.exit(2)

    plans = []
    for rel in BUNDLES:
        p = os.path.join(APP, rel)
        src = open(p, encoding="utf8", errors="replace").read()
        if DIAG_MARKER in src:
            print("SKIP(已是 %s):" % DIAG_MARKER, rel); continue
        # 现行 snippet 可能是 v3 泵,也可能是已部署的旧 v3diag —— 任一原地换成 v3.2
        if src.count(V3_SNIPPET) == 1:
            cur_snip, cur_tag = V3_SNIPPET, "v3泵"
        elif src.count(OLD_DIAG_V3_SNIPPET) == 1:
            cur_snip, cur_tag = OLD_DIAG_V3_SNIPPET, "v3diag"
        else:
            print("   !! %s 里 v3泵/v3diag snippet 命中都 != 1 → 拒绝(先确认现行 snippet 与仓库逐字节一致)" % rel); sys.exit(3)
        out = src.replace(cur_snip, V3DIAG_SNIPPET, 1)
        if not node_check(out, os.path.basename(rel).split(".")[1]):
            print("   !! 语法校验不过,终止。"); sys.exit(4)
        plans.append((p, rel, out)); print("   将替换 %s→%s:" % (cur_tag, DIAG_MARKER), os.path.basename(rel), "(node --check OK)")

    if not plans:
        print("无事可做(可能都已是 v3diag)。"); return
    if not a.apply:
        print("[dry-run] 通过。⌘Q 退出 Cursor 后加 --apply 执行(会先备份到 %s)。" % BACKUP_ROOT); return

    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, "%s-%s" % (ver, ts)); os.makedirs(bdir, exist_ok=True)
    for p, rel, out in plans:
        shutil.copy2(p, os.path.join(bdir, os.path.basename(rel)))
        open(p, "w", encoding="utf8").write(out)
        print("patched:", rel)
    print("备份在:", bdir)
    print("完成。重启 Cursor → 复现「排队连发 8 条」→ grep 'cx-queue-diag' 结构化日志。回滚:--revert")


if __name__ == "__main__":
    main()
