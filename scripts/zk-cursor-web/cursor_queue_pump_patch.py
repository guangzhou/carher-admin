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

═══ 修法 v3(2026-08-25 端到端实测定型;v1 只救一半) ═══
在 addToQueue() 入口挂一个自清理看门狗,每秒 tick(最多 120 次,队列空即停):
  - v1 只敢清「无 uuid」的卡死;实测真实卡死是**流已完成但 chatGenerationUUID 残留**
    (诊断 tick:status=generating/hasUUID=true/bubbles=0 持续 2min+,队列 1→3 永不派发)。
  - v3 判活口径抄 bundle 自己的:aiService.streamingAbortControllers.has(uuid)
    (流一结束控制器必被删)。uuid 在但表里没它=僵尸标记,连续 3 tick 确认后清;
    表里有它=真在生成,绝不碰(实测 70s 长生成全程未误杀);摸不到表=退化 v1(fail-safe)。
  - 每次 tick 调官方补发器 tryDispatchNextQueueItem(),内部守卫原样生效 → 幂等不抢发。
端到端验证(2026-08-25 01:17,本机):4 次 heal 全部 hadUUID=true,积压队列逐条自动
派发(完成→~3s heal→派发→完成…),真生成不受影响。

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
MARKER = "@cx-queue-pump:v3.4"

# 锚点:addToQueue 方法入口(仅参数名被 minify,用捕获组适配)
ANCHOR = re.compile(r"(addToQueue\((\w+)\)\{if\(!this\.isValidQueueItem\(\2\)\)return;)")

# v3.4(2026-08-25 修 v3.3 误杀:折叠/漏答的真凶):
# 实测证据链:heal 后 85ms 即 "Aborted current chat" + 空助手气泡 + 两问挤一轮。
# 机制:官方派发启动新轮的前 2-3s(pre_network 窗口)status=generating 但 uuid 未登记,
# 旧判活 `_u===void 0 立刻 heal` 把起跑轮误判为僵尸 → heal → 官方派发下一条 →
# submitChatMaybeAbortCurrent 掐死起跑轮(空回复)→ 下一轮带着两问 → 折叠/漏答。
# 修法(仍全走官方件):
#   ① 官方在飞标记 inFlightDispatchItemIds 非空(官方派发从入队到流结束全程置位)= 起跑/
#     生成中,绝不判僵尸;
#   ② 取消"无 uuid 立刻判死"特权(v1 遗留),一律连续 3 tick 确认。
# 其余同 v3.3:队列非空就守(无寿命上限),派发全走官方 tryDispatchNextQueueItem()。
SNIPPET = (
    "/*" + MARKER + "*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{"
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
)

# 旧版本 snippet 原文(逐字节),--apply 时若在场则原地替换升级
OLD_SNIPPETS = {
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
