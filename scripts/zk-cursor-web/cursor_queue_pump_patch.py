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

═══ 修法(只加不改,CursorX 同款手术) ═══
在 addToQueue() 入口挂一个自清理看门狗:
  - 入队 1s 后开始,每秒调一次官方补发器 tryDispatchNextQueueItem(),
    最多 60 次;队列一空立即停;不重复武装(this._cxQP 哨兵)。
  - 每次 tick 先做官方同款状态自愈:status==="generating" 且
    chatGenerationUUID 为空 且 generatingBubbleIds 为空(= Cursor 自己在
    summarization finally 里判定"实际无生成"的原条件)→ 置回 completed。
  - 补发器内部守卫 I1k 原样生效:真在生成时照样拒发 → 本补丁幂等,
    不会重复发、不会抢发,只是把错过的 pump 补上。

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
MARKER = "@cx-queue-pump:v1"

# 锚点:addToQueue 方法入口(仅参数名被 minify,用捕获组适配)
ANCHOR = re.compile(r"(addToQueue\((\w+)\)\{if\(!this\.isValidQueueItem\(\2\)\)return;)")

# 看门狗(只引用类方法名与 this 成员,与 minify 变量无关;全 try/catch fail-open)
SNIPPET = (
    "/*" + MARKER + "*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{"
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
            print("SKIP(已打过):", rel); continue
        hits = ANCHOR.findall(src)
        assert len(hits) == 1, "%s 锚点数=%d != 1,版本不匹配,拒绝动手" % (rel, len(hits))
        plans.append((p, rel, src))
        print("锚点 OK:", rel)
    if not plans:
        print("无事可做。"); return
    if not a.apply:
        print("[dry-run] 通过。加 --apply 执行(会先备份到 %s)。" % BACKUP_ROOT)
        return

    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, "%s-%s" % (ver, ts))
    os.makedirs(bdir, exist_ok=True)
    for p, rel, src in plans:
        shutil.copy2(p, os.path.join(bdir, os.path.basename(rel)))
        patched = ANCHOR.sub(lambda m: m.group(1) + SNIPPET, src, count=1)
        assert patched.count(MARKER) == 1 and len(patched) > len(src)
        open(p, "w", encoding="utf8").write(patched)
        print("patched: %s (+%d bytes)" % (rel, len(patched) - len(src)))
    print("备份在:", bdir)
    print("完成。重启 Cursor 生效;回滚: python3 %s --revert" % sys.argv[0])


if __name__ == "__main__":
    main()
