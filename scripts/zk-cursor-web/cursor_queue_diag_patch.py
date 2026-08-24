#!/usr/bin/env python3
"""
cursor_queue_diag_patch.py — 排队卡死「一次性诊断」补丁(只加观测,不改行为)。

═══ 为什么要它 ═══
线上实测(2026-08-25,liuguoxian 机器,模型 sa-grok-4.5):
  - 一轮 "Stream completed successfully" 之后,后台任务把 composer.status 又置回 "generating"
    并挂死(2 分多无 token/无完成),新消息在 start-submit-chat 关卡被 "Composer is generating"
    挡回,队列永远派发不出去。
  - v1 排队泵(@cx-queue-pump:v1)在场且已随重启加载,但**没吐 heal 日志** —— 因为它只在
    `chatGenerationUUID===void 0 && generatingBubbleIds 空` 才自愈(怕误杀真生成)。
  - 挂死那一刻 `chatGenerationUUID` 到底有没有值,**磁盘读不到(在 renderer 内存里)**,
    决定了修法是「清标记」还是「abort 挂死的 _streamingAbortControllers 控制器」。

本补丁把 v1 snippet 原地换成 v2diag:每秒把真实字段打进结构化日志(status / hasUUID /
bubbleIds / queueLen),tick 上限 60→120(覆盖更长挂死)。**行为与 v1 完全一致**(仍只在无 uuid
时自愈),只多了观测。拿到 tick 日志的真实形态后,再上精确修法(v3),不猜。

═══ 用法(需先退出 Cursor;bundle 只在窗口启动时加载)═══
  python3 cursor_queue_diag_patch.py            # dry-run:验证能找到 v1 snippet,不写
  python3 cursor_queue_diag_patch.py --apply    # 原地替换 v1→v2diag(自动备份)
  python3 cursor_queue_diag_patch.py --revert    # 从本脚本备份回滚(还原成打之前的样子)

打完重启 Cursor → 复现「回复刚落地就发下一条」→ 看日志:
  LOGDIR=~/Library/Application\\ Support/Cursor/logs
  grep -h "cx-queue-diag" "$LOGDIR"/*/window*/exthost/anysphere.cursor-always-local/*Structured*.log | tail -40
把 tick 行贴回来即可定型。诊断完 --revert 换回 v1(或直接上 v3 修复版)。
"""
import argparse, json, os, re, shutil, subprocess, sys, time

APP = os.environ.get("CURSOR_APP", "/Applications/Cursor.app")
BUNDLES = [
    "Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js",
    "Contents/Resources/app/out/vs/workbench/workbench.glass.main.js",
]
BACKUP_ROOT = os.path.expanduser("~/.cursor-queue-diag-backup")

# ── 现有 v1 snippet 原文(逐字节;来自 cursor_queue_pump_patch.py,用于定位替换)──
V1_MARKER = "@cx-queue-pump:v1"
V1_SNIPPET = (
    "/*" + V1_MARKER + "*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{"
    "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;"
    "const _h=this.getComposerHandleIfLoaded();"
    "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
    'if(_d&&_d.status==="generating"&&_d.chatGenerationUUID===void 0&&(_d.generatingBubbleIds??[]).length===0){'
    "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",generatingBubbleIds:[]});"
    'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId})}catch(_e){}}'
    "this.tryDispatchNextQueueItem();"
    "if(this.getQueueItems().length>0&&++_cxN<60){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
    "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
)

# ── v2diag:v1 + 每秒 tick 日志 + tick 上限 120;自愈条件不变 ──
V2_MARKER = "@cx-queue-diag:v2"
V2_SNIPPET = (
    "/*" + V2_MARKER + "*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{"
    "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;"
    "const _h=this.getComposerHandleIfLoaded();"
    "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
    'try{this.structuredLogService.info("composer","[cx-queue-diag] tick",'
    "{composerId:this.composerId,n:_cxN,queueLen:this.getQueueItems().length,"
    "status:(_d&&_d.status)||null,hasUUID:!!(_d&&_d.chatGenerationUUID),"
    "bubbleIds:((_d&&_d.generatingBubbleIds)||[]).length})}catch(_e){}"
    'if(_d&&_d.status==="generating"&&_d.chatGenerationUUID===void 0&&(_d.generatingBubbleIds??[]).length===0){'
    "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",generatingBubbleIds:[]});"
    'this.structuredLogService.info("composer","[cx-queue-diag] healed stuck generating status",{composerId:this.composerId})}catch(_e){}}'
    "this.tryDispatchNextQueueItem();"
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
    a = ap.parse_args()
    ver = cursor_version()
    print("Cursor version:", ver)

    if a.revert:
        baks = sorted(os.listdir(BACKUP_ROOT)) if os.path.isdir(BACKUP_ROOT) else []
        if not baks:
            print("!! 无备份可回滚"); sys.exit(1)
        b = os.path.join(BACKUP_ROOT, baks[-1])
        for rel in BUNDLES:
            src = os.path.join(b, os.path.basename(rel))
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(APP, rel)); print("restored:", rel)
        print("回滚完成,重启 Cursor 生效。"); return

    if cursor_running():
        print("!! Cursor 正在运行 —— 请先 ⌘Q 完全退出再跑。"); sys.exit(2)

    plans = []
    for rel in BUNDLES:
        p = os.path.join(APP, rel)
        src = open(p, encoding="utf8", errors="replace").read()
        if V2_MARKER in src:
            print("SKIP(已是 v2diag):", rel); continue
        n = src.count(V1_SNIPPET)
        if n != 1:
            print("   !! %s 里 v1 snippet 命中=%d != 1 → 拒绝(先确认 v1 在场且唯一)" % (rel, n)); sys.exit(3)
        out = src.replace(V1_SNIPPET, V2_SNIPPET, 1)
        if not node_check(out, os.path.basename(rel).split(".")[1]):
            print("   !! 语法校验不过,终止。"); sys.exit(4)
        plans.append((p, rel, out)); print("   将替换 v1→v2diag:", os.path.basename(rel), "(node --check OK)")

    if not plans:
        print("无事可做(可能都已是 v2diag)。"); return
    if not a.apply:
        print("[dry-run] 通过。加 --apply 执行(会先备份到 %s)。" % BACKUP_ROOT); return

    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, "%s-%s" % (ver, ts)); os.makedirs(bdir, exist_ok=True)
    for p, rel, out in plans:
        shutil.copy2(p, os.path.join(bdir, os.path.basename(rel)))
        open(p, "w", encoding="utf8").write(out)
        print("patched:", rel)
    print("备份在:", bdir)
    print("完成。重启 Cursor → 复现卡死 → grep 'cx-queue-diag' 结构化日志。回滚:--revert")


if __name__ == "__main__":
    main()
