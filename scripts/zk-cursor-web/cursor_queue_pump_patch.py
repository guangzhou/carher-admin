#!/usr/bin/env python3
"""
cursor_queue_pump_patch.py — 修 Cursor 3.16.x「连发消息被吞」排队竞态(本地 client 补丁)。

═══ 病根(2026-08-24 源码级定位,liuguoxian 机器实测) ═══
Cursor 本地 agent 通道(CursorX 启用的 BYOK 路径)下:
  - 回复刚结束的几秒内快速发下一条消息 → composer.status 仍是 "generating"
    (轮结束后后台摘要任务会再次置位;对自定义模型该任务还会撞
    "Failed to resolve hook model legacy slug" 而中断,状态复位被跳过)
  - 消息被 addToQueue() 收进队列(界面输入框清空,看起来"发出去了")
  - 但补发器 tryDispatchNextQueueItem() 只在少数事件被调用(流结束/状态复位),
    那班车已经开走 → 队列里的消息永远无人补发 = 静默丢失。
  - 本机观测:3 次全部同型(22:23:54/22:24:05/22:38:26),0 次自动补发。

═══ 修法演进(v1→v3→v3.4→v3.5→v4;当前=v4,2026-08-25 3.17.19 实测定型) ═══
在 addToQueue() 入口挂一个自清理看门狗,每秒 tick(队列空即停,无寿命上限):
  - 僵尸判活(v3 定型):口径抄 bundle 自己的 aiService.streamingAbortControllers.has(uuid),
    uuid 在但表里没它=僵尸,连续 3 tick 确认后清;表里有它=真在生成,绝不碰。
  - 在飞防护(v3.4):inFlightDispatchItemIds 非空=官方派发在飞,不判僵尸。
  - 派发收敛(v3.5):只在 ①刚 heal 完 ②空闲且无在飞派发 时才调 tryDispatchNextQueueItem。
  - 官方让路(v4,治折叠真凶):官方 3.17 在 turnEnded 事件里【原生接力队列】
    (removeFromQueue+appendQueuedHumanMessage+新请求),不走 dispatch 机制、不碰
    inFlightDispatchItemIds → 泵在它起跑窗口里 heal+抢发下一条 = 一请求两问只答后一条。
    v4 每 tick 对比队列长度,发现被别人消费 → 让路 5 tick(不 heal 不派发)。
    官方不管的场景(真僵尸/用户停止饿死)泵照旧兜底。

═══ 用法 ═══
  python3 cursor_queue_pump_patch.py            # dry-run:验锚点,不写
  python3 cursor_queue_pump_patch.py --apply    # 打补丁(自动备份)
  python3 cursor_queue_pump_patch.py --revert   # 从最近备份回滚
打完必须重启 Cursor(workbench 只在窗口启动时加载)。
⚠️ 跑 CursorX 的 cli update/restore 前先 --revert 本补丁(避免快照打架);
   Cursor 自动升级会掀翻一切 patch,settings.json 须保持 "update.mode":"none"。
验证:回复刚落地后 1 秒内快速发下一条 → 应在 ~1-2s 内自动发出;
  结构化日志出现 "[cx-queue-pump] healed stuck generating status" 即自愈踩中。
"""
import argparse, json, os, re, shutil, subprocess, sys, time

APP = os.environ.get("CURSOR_APP", "/Applications/Cursor.app")
BUNDLES = [
    "Contents/Resources/app/out/vs/workbench/workbench.desktop.main.js",
    "Contents/Resources/app/out/vs/workbench/workbench.glass.main.js",
]
BACKUP_ROOT = os.path.expanduser("~/.cursor-queue-pump-backup")
MARKER = "@cx-queue-pump:v4"

# 锚点:addToQueue 方法入口(仅参数名被 minify,用捕获组适配)
ANCHOR = re.compile(r"(addToQueue\((\w+)\)\{if\(!this\.isValidQueueItem\(\2\)\)return;)")

# v4(2026-08-25 修 v3.5 残留折叠;3.17 实测定型):
# 真凶=官方 3.17 在 turnEnded 事件里【原生接力队列】:removeFromQueue(队首)+
# appendQueuedHumanMessage(直接写进对话)+发起新请求。这条路不走
# dispatchQueueItemKeepingRowUntilOwned,不碰 inFlightDispatchItemIds → 泵的在飞守卫全瞎。
# 实测形态(17:49:06):上轮攒满的僵尸计数器让泵在官方接力起跑的 74ms 内 heal+派发下一条,
# 后枪掐前枪 → 一请求两问(convLen 每轮+2 用户气泡)→ 模型只答后一条;末条被 heal 踩死。
# 修:泵每 tick 对比队列长度,发现【被别人消费】(掉了但不是自己派的)→ 让路 5 tick
# (不 heal 不派发),官方接力起跑窗(实测预网络 2.1-3.4s)全程不受干扰;
# 官方不管的场景(真僵尸卡死/用户停止后饿死)泵照旧兜底。僵尸判活同 v3.5。
SNIPPET = (
    "/*" + MARKER + "*/try{if(this._cxQP===void 0){let _cxS=0,_lq=-1,_cool=0,_pfl=!1;const _cxT=()=>{"
    "this._cxQP=void 0;try{const _q=this.getQueueItems().length;if(_q===0)return;"
    "const _fl=this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0;"
    "if(_lq>=0&&_q<_lq&&!_pfl){_cool=5;_cxS=0;"
    "try{this.structuredLogService.info(\"composer\",\"[cx-queue-pump] queue consumed externally, yielding\",{composerId:this.composerId,from:_lq,to:_q})}catch(_e){}}"
    "_lq=_q;_pfl=_fl;"
    "if(_cool>0){_cool--}else{"
    "const _h=this.getComposerHandleIfLoaded();"
    "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
    "let _go=!1;"
    'if(_d&&_d.status==="generating"){'
    "if(!_fl&&(_d.generatingBubbleIds??[]).length===0){"
    "const _u=_d.chatGenerationUUID;"
    "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;"
    "const _stale=_u===void 0||(_m&&typeof _m.has===\"function\"&&!_m.has(_u));"
    "_cxS=_stale?_cxS+1:0;"
    "if(_cxS>=3){_cxS=0;"
    "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",chatGenerationUUID:void 0,generatingBubbleIds:[]});"
    'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0});_go=!0}catch(_e){}}}'
    "else{_cxS=0}}"
    "else{_cxS=0;_go=!_fl}"
    "if(_go)this.tryDispatchNextQueueItem()}"
    "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
    "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
)

# 旧版本 snippet 原文(逐字节),--apply 时若在场则原地替换升级
OLD_SNIPPETS = {
    "@cx-queue-pump:v3.5": (
        "/*@cx-queue-pump:v3.5*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{"
        "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;"
        "const _h=this.getComposerHandleIfLoaded();"
        "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
        "const _fl=this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0;"
        "let _go=!1;"
        'if(_d&&_d.status==="generating"){'
        "if(!_fl&&(_d.generatingBubbleIds??[]).length===0){"
        "const _u=_d.chatGenerationUUID;"
        "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;"
        "const _stale=_u===void 0||(_m&&typeof _m.has===\"function\"&&!_m.has(_u));"
        "_cxS=_stale?_cxS+1:0;"
        "if(_cxS>=3){_cxS=0;"
        "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",chatGenerationUUID:void 0,generatingBubbleIds:[]});"
        'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0});_go=!0}catch(_e){}}}'
        "else{_cxS=0}}"
        "else{_cxS=0;_go=!_fl}"
        "if(_go)this.tryDispatchNextQueueItem();"
        "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
        "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
    ),
    "@cx-queue-pump:v3.4": (
        "/*@cx-queue-pump:v3.4*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{"
        "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;"
        "const _h=this.getComposerHandleIfLoaded();"
        "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
        'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){'
        "const _u=_d.chatGenerationUUID;"
        "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;"
        "const _fl=this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0;"
        "const _stale=!_fl&&(_u===void 0||(_m&&typeof _m.has===\"function\"&&!_m.has(_u)));"
        "_cxS=_stale?_cxS+1:0;"
        "if(_cxS>=3){"
        "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",chatGenerationUUID:void 0,generatingBubbleIds:[]});"
        'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}'
        "else{_cxS=0}"
        "this.tryDispatchNextQueueItem();"
        "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
        "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
    ),
    "@cx-queue-pump:v3.3": (
        "/*@cx-queue-pump:v3.3*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{"
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
        "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
        "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
    ),
    "@cx-queue-pump:v3": (
        "/*@cx-queue-pump:v3*/try{if(this._cxQP===void 0){let _cxN=0,_cxS=0;const _cxT=()=>{"
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
    ),
    "@cx-queue-diag:v3.2": (
        "/*@cx-queue-diag:v3.2*/try{if(this._cxQP===void 0){let _cxN=0,_cxS=0;const _cxT=()=>{"
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
        "var _qids;try{_qids=_qb.map(function(q){"
        "var _id=q&&(q.id||q.bubbleId||q.messageId||q.requestId)||\"?\";"
        "var _dl;try{_dl=(q&&q.delivery===void 0)?\"none\":(((q.delivery&&q.delivery.kind)||\"set\")+\"/sc=\"+!!(q.delivery&&q.delivery.serverConfirmed===true))}catch(_e){_dl=\"err\"}"
        "return _id+\"|\"+_dl})}catch(_e){_qids=[]}"
        "var _if;try{var _mm=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;_if=_mm&&_mm.size!==void 0?_mm.size:-1}catch(_e){_if=-2}"
        "this.tryDispatchNextQueueItem();"
        "try{var _qa=this.getQueueItems();this.structuredLogService.info(\"composer\",\"[cx-queue-diag] tick\","
        "{composerId:this.composerId,n:_cxN,status:(_d&&_d.status)||null,hasUUID:!!(_d&&_d.chatGenerationUUID),"
        "bubbleIds:((_d&&_d.generatingBubbleIds)||[]).length,inflight:_if,"
        "qlenBefore:_qb.length,qlenAfter:_qa.length,dispatched:_qb.length-_qa.length,qids:_qids})}catch(_e){}"
        "if(this.getQueueItems().length>0&&++_cxN<120){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
        "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
    ),
    "@cx-queue-pump:v1": (
        "/*@cx-queue-pump:v1*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{"
        "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;"
        "const _h=this.getComposerHandleIfLoaded();"
        "const _d=_h?this.composerDataService.getComposerData(_h):void 0;"
        'if(_d&&_d.status==="generating"&&_d.chatGenerationUUID===void 0&&(_d.generatingBubbleIds??[]).length===0){'
        "try{this.composerDataService.updateComposerData(_h,{status:\"completed\",generatingBubbleIds:[]});"
        'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId})}catch(_e){}}'
        "this.tryDispatchNextQueueItem();"
        "if(this.getQueueItems().length>0&&++_cxN<60){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};"
        "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}"
    ),
    "@cx-queue-diag:v2": (
        "/*@cx-queue-diag:v2*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{"
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
    ),
}


def cursor_version():
    try:
        pkg = json.load(open(os.path.join(APP, "Contents/Resources/app/package.json")))
        return pkg.get("version", "unknown")
    except Exception:
        return "unknown"


def syntax_check():
    """把 SNIPPET 包进桩类过 node --check,语法错在这一步就拦死。"""
    stub = ("class X{constructor(){this.composerId=1}getQueueItems(){return[]}"
            "getComposerHandleIfLoaded(){}tryDispatchNextQueueItem(){}"
            "addToQueue(t){if(!this.isValidQueueItem(t))return;%s const e=1;return e}"
            "isValidQueueItem(){return true}}" % SNIPPET)
    p = "/tmp/cxqp_syntax.js"
    open(p, "w").write(stub)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    os.unlink(p)
    if r.returncode != 0:
        print("!! SNIPPET 语法不过:", r.stderr[:300]); sys.exit(1)
    print("snippet 语法: OK")


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
            dst = os.path.join(APP, rel)
            shutil.copy2(src, dst)
            print("restored:", rel, "<-", src)
        print("回滚完成,重启 Cursor 生效。")
        return

    syntax_check()
    plans = []
    for rel in BUNDLES:
        p = os.path.join(APP, rel)
        src = open(p, encoding="utf8", errors="replace").read()
        if MARKER in src:
            print("SKIP(已是 %s):" % MARKER, rel); continue
        old = next((s for mk, s in OLD_SNIPPETS.items() if mk in src), None)
        if old is not None:
            assert src.count(old) == 1, "%s 旧 snippet 命中 !=1,拒绝" % rel
            plans.append((p, rel, src, ("upgrade", old)))
            print("旧版在场,将原地升级到 %s:" % MARKER, rel)
        else:
            hits = ANCHOR.findall(src)
            assert len(hits) == 1, "%s 锚点数=%d != 1,版本不匹配,拒绝动手" % (rel, len(hits))
            plans.append((p, rel, src, ("inject", None)))
            print("锚点 OK:", rel)
    if not plans:
        print("无事可做。"); return
    if not a.apply:
        print("[dry-run] 通过。加 --apply 执行(会先备份到 %s)。" % BACKUP_ROOT)
        return

    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, "%s-%s" % (ver, ts))
    os.makedirs(bdir, exist_ok=True)
    for p, rel, src, (mode, old) in plans:
        shutil.copy2(p, os.path.join(bdir, os.path.basename(rel)))
        if mode == "upgrade":
            patched = src.replace(old, SNIPPET, 1)
        else:
            patched = ANCHOR.sub(lambda m: m.group(1) + SNIPPET, src, count=1)
        assert patched.count(MARKER) == 1
        open(p, "w", encoding="utf8").write(patched)
        print("patched(%s): %s (%+d bytes)" % (mode, rel, len(patched) - len(src)))
    print("备份在:", bdir)
    print("完成。重启 Cursor 生效;回滚: python3 %s --revert" % sys.argv[0])


if __name__ == "__main__":
    main()
