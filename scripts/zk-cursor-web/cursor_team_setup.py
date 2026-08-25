#!/usr/bin/env python3
"""
cursor_team_setup.py — 一条命令给同事装好 cursor-g（Cursor 3.16.x / macOS）。

═══ 它替代了什么 ═══
以前同事要:①装 CursorX 应用 ②开面板配 provider ③再单独跑排队补丁脚本 —— 三步、两个工具。
本脚本把这些合成一条命令,而且比 CursorX 更小:CursorX 打 5 处 bundle 补丁,其中 2 处
(byok-autosync + loaderHook fetch 拦截)在我们 byok:false / providers:[] 下**是死代码**
(loaderHook 每条路径都 `if(!isByokEnabled())return` 直接放行;applyByokAutoSync 同样 no-op)。
真正让 Cursor 能用自定义 OpenAI 模型的是 **Cursor 原生的 OpenAI base-URL 覆盖**
(useOpenAIKey/openAIBaseUrl/userAddedModels);CursorX 的 3 处补丁只是把它在非 Pro 档解锁。
所以本脚本只复刻那 3 处 + 排队修复 + 写配置,死代码不带。

═══ 做的 6 件事(全部幂等 + 自动备份 + 失败即拒不动手) ═══
  1. 版本闸:只认 Cursor 3.16.x(package.json version);不匹配拒绝动手。
  2. Cursor 必须已退出(外部写 state.vscdb 有内存覆盖竞态——loaderHook 注释亲述);没退就停。
  3. 打两条 workbench bundle(desktop+glass)3 处解锁补丁 + 排队泵补丁:
       A gate       —— 模型选择器不再把自定义模型挤出可选视图(free-tier-gate)
       B localagent —— 强制走本地 agent 通道(free-tier-localagent)
       C dedicated  —— 走 in-process runLocalAgent 而非 dedicated host(dedicated-host-bypass)
       D queue-pump —— 连发消息被吞的排队竞态修复(= cursor_queue_pump_patch.py 同款,同 marker,幂等)
     每处锚点=**稳定语义地标 + 通用捕获 minify 尾巴**(不写死 x4g/p3s/Cl 这类每版会变的名),
     每处断言全文件恰好命中 1 次,补完对整份 bundle 跑 `node --check` 才落盘。
  4. 写 BYOK 配置进 state.vscdb 的 applicationUser blob:
       openAIBaseUrl=<公网网关>、useOpenAIKey=true、把 cursor-g-* 并进
       aiSettings.userAddedModels + modelOverrideEnabled(读旧去重合并,不动同事已有模型)。
  5. settings.json 写 "update.mode":"none"(Cursor 自动升级会掀翻所有 bundle 补丁)。
  6. 打印**唯一的一步人工**:在 Cursor 设置里粘贴自己的 key 并重启。

═══ 为什么 key 不能自动写(诚实边界) ═══
Cursor 把 OpenAI key 存在 `secret://cursorAuth/openAIKey`,值是 `v10`+AES 密文,用 macOS
钥匙串条目「Cursor Safe Storage」加密(实测:读钥匙串会弹授权框)。从外部脚本写它要么
弹钥匙串框、要么自己复刻 OSCrypt 加密——脆且一旦错就是"看着已登录其实 401"的假绿。
所以 key 这一步交给 Cursor 自己(同事在设置里粘一次),其余全自动。base-url/模型/开关都已
预填,同事只需粘 key 那一个框。比装 CursorX+配面板+跑脚本省太多。

═══ 用法 ═══
  python3 cursor_team_setup.py                      # dry-run:验版本/锚点/配置差异,不写
  python3 cursor_team_setup.py --apply              # 执行(自动备份到 ~/.cursor-team-setup-backup)
  python3 cursor_team_setup.py --revert             # 从最近备份回滚(bundle+vscdb行+settings)
  python3 cursor_team_setup.py --apply \
      --base-url https://cc.auto-link.com.cn/pro/v1 \
      --models cursor-g-5.6-sol,cursor-g-5.6-sol-high,cursor-g-5.6-luna,cursor-g-5.6-pro,cursor-g-5.6-instant,cursor-g-5.5

打完必须重启 Cursor(bundle 只在窗口启动时加载;vscdb 改动也要重启才读)。
⚠️ 若这台机器装过 CursorX:本脚本的 3 处补丁锚点会因 CursorX 已改写而命中 0 次 → 自动拒绝
   (防重复打)。要用本脚本,先把 bundle 还原到 CursorX 备份的原版,再跑本脚本。
"""
import argparse, json, os, re, shutil, sqlite3, subprocess, sys, time

APP = os.environ.get("CURSOR_APP", "/Applications/Cursor.app")
RES = os.path.join(APP, "Contents/Resources/app")
BUNDLES = [
    "out/vs/workbench/workbench.desktop.main.js",
    "out/vs/workbench/workbench.glass.main.js",
]
STATE_DB = os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
SETTINGS_JSON = os.path.expanduser("~/Library/Application Support/Cursor/User/settings.json")
APP_USER_KEY = ("src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
                ".persistentStorage.applicationUser")
BACKUP_ROOT = os.path.expanduser("~/.cursor-team-setup-backup")

SUPPORTED_MAJOR_MINOR = "3.16"
DEFAULT_BASE_URL = "https://cc.auto-link.com.cn/pro/v1"
DEFAULT_MODELS = [
    "cursor-g-5.6-sol", "cursor-g-5.6-sol-high", "cursor-g-5.6-luna",
    "cursor-g-5.6-pro", "cursor-g-5.6-instant", "cursor-g-5.5",
]
DEFAULT_MODEL = "cursor-g-5.6-sol"  # 装完直接选中它,用户不用在菜单里挑

# ── bundle 补丁定义:每处一个 (name, marker, compiled_regex, replace_fn) ──
# 锚点全部挂在稳定语义地标上(getModelPickerDisplayConfiguration / clientSupportsRoutedModelUpdate /
# useDedicatedLocalAgentRuntimeHost / runLocalAgentInDedicatedExtensionHost / addToQueue),
# minify 变量(x4g/p3s/Cl/Op…)用通用捕获组吃掉,不写死——这样跨 Cursor 小版本不易碎;
# 一旦地标搬走 → 命中数 != 1 → 拒绝动手(fail-safe,绝不改歪)。

GATE_FN = ("(function(c){if(!c)return c;const o={...c};"
    "if(o.namedModelsViewConfig){o.namedModelsViewConfig={...o.namedModelsViewConfig};"
    "delete o.namedModelsViewConfig.namedViewToRoutedModelViewButton;"
    "delete o.namedModelsViewConfig.namedViewToRoutedModelViewToggle;"
    "delete o.namedModelsViewConfig.namedViewToRoutedModelViewNoButton;}"
    "if(o.routedModelViewConfig){o.routedModelViewConfig={...o.routedModelViewConfig};"
    "delete o.routedModelViewConfig.routedModelViewToNamedViewButton;"
    "delete o.routedModelViewConfig.routedModelViewToNamedViewToggle;"
    "o.routedModelViewConfig.hideRoutedModelView=false;}return o;})")

# 队列泵:与 cursor_queue_pump_patch.py v4 逐字节相同(同 marker 保幂等/可互换)
# v4 修 v3.5 残留折叠(真凶):官方 3.17 在 turnEnded 事件里原生接力队列
# (removeFromQueue+appendQueuedHumanMessage+新请求),不走 dispatch、不碰
# inFlightDispatchItemIds → 泵在它起跑窗口里 heal+抢发下一条 = 一请求两问只答后一条。
# 修:泵每 tick 对比队列长度,发现被别人消费 → 让路 5 tick(不 heal 不派发);
# 官方不管的场景(真僵尸/用户停止饿死)照旧兜底。
QP_MARKER = "@cx-queue-pump:v4"
QP_ANCHOR = re.compile(r"(addToQueue\((\w+)\)\{if\(!this\.isValidQueueItem\(\2\)\)return;)")
QP_SNIPPET = (
    "/*" + QP_MARKER + "*/try{if(this._cxQP===void 0){let _cxS=0,_lq=-1,_cool=0,_pfl=!1;const _cxT=()=>{"
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

# 旧版泵 snippet(逐字节)——plan_bundle 里 queue-pump 若发现旧版在场,原地替换升级
# (没有这个,--repair 会在旧泵仍在时按锚点二次注入 = 双泵)。更老版本用
# cursor_queue_pump_patch.py 升级(它带完整 OLD_SNIPPETS 清单)。
QP_OLD_SNIPPETS = [
    (  # v3.5
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
]


def _gate_sub(m):
    # return <EXPR>}resolveModelNameToCatalog  →  return GATE(<EXPR>)}resolve...
    return m.group(1) + "/*@cxteam-gate*/" + GATE_FN + "(" + m.group(2) + ")" + m.group(3)


PATCHES = [
    dict(name="gate", marker="@cxteam-gate",
         rx=re.compile(r"(modelPickerDisplayConfiguration\?\?\w+;return )(\w+\([a-z]\))(\}resolveModelNameToCatalog)"),
         sub=_gate_sub),
    dict(name="localagent", marker="@cxteam-localagent",
         rx=re.compile(r"(clientSupportsRoutedModelUpdate:!0\};if\()(\w+\.localMode)(\)\{try\{)"),
         sub=lambda m: m.group(1) + "/*@cxteam-localagent*/!0" + m.group(3)),
    dict(name="dedicated", marker="@cxteam-dedicated",
         rx=re.compile(r"(\w+\(this\.storageService,\"useDedicatedLocalAgentRuntimeHost\"\))(\?await this\.runLocalAgentInDedicatedExtensionHost\()"),
         sub=lambda m: "(/*@cxteam-dedicated*/!1)" + m.group(2)),
    dict(name="queue-pump", marker=QP_MARKER,
         rx=QP_ANCHOR, sub=lambda m: m.group(1) + QP_SNIPPET),
    # qserial(2026-08-25):官方 tryDispatchNextQueueItem 的闸门 Vyb 只看 status,不看
    # "是否已有派发在飞"。实测竞态:heal 写 status 触发响应式监听,官方派发器与泵在 30ms 内
    # 各派一条,后枪的 submitChatMaybeAbortCurrent 掐死前枪的预网络轮 → 两问挤一轮/前问无答。
    # 修:入口加官方自家 inFlightDispatchItemIds 在飞守卫 → 派发严格串行(所有调用方生效)。
    # 兜底:若守卫挡掉了"轮完成事件"的那次派发,泵 1s 内以空闲态补发,最多多等 1-2s。
    dict(name="qserial", marker="@cxteam-qserial",
         rx=re.compile(r"(tryDispatchNextQueueItem\(\)\{const (\w+)=this\.getComposerHandleIfLoaded\(\);if\(!\2\)return;)"),
         sub=lambda m: m.group(1) + "/*@cxteam-qserial*/if(this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0)return;"),
    # nosteer-mod(2026-08-25 真凶):官方把「配置=queue 但按修饰键发送」设计为强制 steer
    # (⌘+回车正是修饰键!)→ 生成中发的每条都被注入当前轮 → 两问挤一轮/前问无答。
    # 本团队发送手势就是 ⌘+回车,故掐掉该 override:修饰键照常发送,但行为仍是 queue。
    dict(name="nosteer-mod", marker="@cxteam-nosteermod",
         rx=re.compile(r'(case"send":case"queue":return \w+&&\w+\(\w+\)\?\{behavior:")steer(",isModifierOverride:!0\})'),
         sub=lambda m: m.group(1) + 'queue' + m.group(2) + '/*@cxteam-nosteermod*/'),
    # nopromote(2026-08-25 终极 steer 封口):3.17 自动把排队消息 steer 注入当前轮(预网络
    # 窗口内注入 → 一请求两问 → 模型只答最后一条)。promoteQueueItemToSteer 是所有 steer
    # 注入的总入口(gate/自动/NUX),短路=全部走老实排队。multi:glass 打包多份 composer,
    # 全部命中都要打(count>=1 即可,逐处插入)。
    dict(name="nopromote", marker="@cxteam-nopromote", multi=True,
         rx=re.compile(r'(async promoteQueueItemToSteer\(\w+\)\{)'),
         sub=lambda m: m.group(1) + '/*@cxteam-nopromote*/return!1;'),
    # norelay(2026-08-25 真·根因,3.17.19 五轮实测闭环):官方 turnEnded 里的「轮内接力」——
    # 有排队消息时弹队首+写气泡+submitConversationAction 续进当前 agent 循环,然后 break
    # (不走后面的 status 复位)。官方设计:接力=同轮继续,status 该留 generating。
    # 但 BYOK 本地线轮末 agent 循环已退出("Request successful"即收摊),接力消息没人接:
    #   ① status 永卡 generating(僵尸真身=接力分支 break 掉复位代码);
    #   ② 弹队的消息成孤儿气泡,被下一请求 "prepending user messages" 捎走
    #      → 一请求两问只答后一条(甲乙丙丁戊己/壬癸子丑寅卯 折叠真身)。
    # 能力闸门 k_d(agentBackend==="cursor-agent"&&!isLocalMode&&!isAgentHostEnabled&&
    # !isNewRequestIdGateEnabled())在本线误判为真。修:调用点恒 false → turnEnded 走
    # 复位分支 → status 正常复位,官方队列机制逐条各自成轮,僵尸+折叠同根拔除。
    dict(name="norelay", marker="@cxteam-norelay",
         rx=re.compile(r'(if\()(\w+\(\{agentBackend:\w+,isLocalMode:\w+\.localMode,isAgentHostEnabled:\w+,isNewRequestIdGateEnabled:\(\)=>this\.isQueuedPromptNewRequestIdEnabled\(\)\}\))(\)\{)'),
         sub=lambda m: m.group(1) + '/*@cxteam-norelay*/!1&&' + m.group(2) + m.group(3)),
]


def cursor_version():
    try:
        return json.load(open(os.path.join(RES, "package.json"))).get("version", "unknown")
    except Exception:
        return "unknown"


def cursor_running():
    r = subprocess.run(["pgrep", "-f", "Cursor.app/Contents/MacOS/Cursor"],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() != ""


def node_check(text, tag):
    p = "/tmp/cxteam_check_%s.js" % tag
    open(p, "w", encoding="utf8").write(text)
    r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
    os.unlink(p)
    if r.returncode != 0:
        print("   !! node --check 失败(%s):%s" % (tag, r.stderr[:300]))
        return False
    return True


def plan_bundle(rel):
    """返回 (path, patched_text) 或 None(无需动/已打过);命中异常直接 SystemExit。"""
    p = os.path.join(RES, rel)
    src = open(p, encoding="utf8", errors="replace").read()
    out = src
    applied = []
    for pt in PATCHES:
        if pt["marker"] in out:
            print("   SKIP %-11s(已打过)" % pt["name"]); continue
        if pt["name"] == "queue-pump":
            old = next((s for s in QP_OLD_SNIPPETS if s in out), None)
            if old is not None:
                if out.count(old) != 1:
                    print("   !! queue-pump 旧版命中!=1 → 拒绝"); sys.exit(2)
                out = out.replace(old, QP_SNIPPET, 1)
                applied.append("queue-pump(升级)")
                continue
        hits = pt["rx"].findall(out)
        if pt.get("multi"):
            if len(hits) < 1:
                print("   !! %-11s 锚点命中=0 → 拒绝动手" % pt["name"]); sys.exit(2)
            out = pt["rx"].sub(pt["sub"], out)  # multi:全部命中逐处打
            applied.append("%s(x%d)" % (pt["name"], len(hits)))
            continue
        if len(hits) != 1:
            print("   !! %-11s 锚点命中=%d != 1 → 拒绝动手(版本不匹配或已被 CursorX 改写)"
                  % (pt["name"], len(hits)))
            sys.exit(2)
        out = pt["rx"].sub(pt["sub"], out, count=1)
        applied.append(pt["name"])
    if out == src:
        print("   %s: 全部已打过,跳过" % os.path.basename(rel)); return None
    print("   %s: 将打 [%s]" % (os.path.basename(rel), ", ".join(applied)))
    return (p, out)


def merge_config(dry):
    con = sqlite3.connect(STATE_DB)
    cur = con.cursor()
    row = cur.execute("SELECT value FROM ItemTable WHERE key=?", (APP_USER_KEY,)).fetchone()
    if not row:
        print("   !! applicationUser blob 不存在(Cursor 没初始化过?)"); con.close(); sys.exit(3)
    d = json.loads(row[0])
    before = dict(baseUrl=d.get("openAIBaseUrl"), useKey=d.get("useOpenAIKey"))
    d["openAIBaseUrl"] = ARGS.base_url
    d["useOpenAIKey"] = True
    ai = d.setdefault("aiSettings", {})
    def dedup(existing):
        seen, outl = set(), []
        for x in (existing or []) + ARGS.models:
            if isinstance(x, str) and x not in seen:
                seen.add(x); outl.append(x)
        return outl
    uam_before = list(ai.get("userAddedModels") or [])
    ai["userAddedModels"] = dedup(ai.get("userAddedModels"))
    ai["modelOverrideEnabled"] = dedup(ai.get("modelOverrideEnabled"))
    added = [m for m in ARGS.models if m not in uam_before]
    # #3 默认模型:装完直接选中 cursor-g-5.6-sol。保守——仅当当前选中的不是任一 cursor-g 时才设。
    mc = ai.setdefault("modelConfig", {})
    cur_name = (mc.get("composer") or {}).get("modelName")
    if not (isinstance(cur_name, str) and cur_name.startswith("cursor-g")):
        for feat in ("composer", "cmd-k"):
            f = dict(mc.get(feat) or {})
            f["modelName"] = DEFAULT_MODEL
            f["selectedModels"] = [{"modelId": DEFAULT_MODEL, "parameters": []}]
            mc[feat] = f
        def_model_set = DEFAULT_MODEL
    else:
        def_model_set = "(skip, 已是 %s)" % cur_name
    print("   config diff: baseUrl %r -> %r ; useOpenAIKey %r -> True ; +models %s ; defaultModel -> %s"
          % (before["baseUrl"], ARGS.base_url, before["useKey"],
             added or "(none, all present)", def_model_set))
    if not dry:
        cur.execute("UPDATE ItemTable SET value=? WHERE key=?",
                    (json.dumps(d, ensure_ascii=False), APP_USER_KEY))
        con.commit()
    con.close()
    return row[0]  # old blob (for backup)


def set_update_none(dry):
    if not os.path.exists(SETTINGS_JSON):
        s = {}
    else:
        try:
            s = json.loads(open(SETTINGS_JSON, encoding="utf8").read() or "{}")
        except Exception:
            print("   !! settings.json 不是纯 JSON(含注释?)→ 跳过自动写,请手动加 \"update.mode\":\"none\"")
            return None
    old = s.get("update.mode")
    if old == "none":
        print("   update.mode 已是 none"); return json.dumps(s)
    print("   update.mode %r -> none" % old)
    if not dry:
        s["update.mode"] = "none"
        open(SETTINGS_JSON, "w", encoding="utf8").write(json.dumps(s, ensure_ascii=False, indent=2))
    return old


def do_backup(ver, bundle_plans, old_blob, old_settings):
    ts = time.strftime("%Y%m%d-%H%M%S")
    bdir = os.path.join(BACKUP_ROOT, "%s-%s" % (ver, ts))
    os.makedirs(bdir, exist_ok=True)
    for p, _ in bundle_plans:
        shutil.copy2(p, os.path.join(bdir, os.path.basename(p)))
    if old_blob is not None:
        open(os.path.join(bdir, "applicationUser.blob.json"), "w", encoding="utf8").write(old_blob)
    if os.path.exists(SETTINGS_JSON):
        shutil.copy2(SETTINGS_JSON, os.path.join(bdir, "settings.json"))
    print("   备份 ->", bdir)
    return bdir


def revert():
    baks = sorted(os.listdir(BACKUP_ROOT)) if os.path.isdir(BACKUP_ROOT) else []
    if not baks:
        print("!! 无备份可回滚"); sys.exit(1)
    # 优先选「含 bundle 的最新备份」(跳过 -cfgonly:那种只存了配置,回滚它会漏掉 bundle);都没有再退回最新
    def has_bundle(d):
        return any(os.path.exists(os.path.join(BACKUP_ROOT, d, os.path.basename(rel))) for rel in BUNDLES)
    pick = next((d for d in reversed(baks) if has_bundle(d)), baks[-1])
    b = os.path.join(BACKUP_ROOT, pick)
    print("从备份回滚:", b)
    for rel in BUNDLES:
        src = os.path.join(b, os.path.basename(rel))
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(RES, rel)); print("  restored bundle:", os.path.basename(rel))
    blob = os.path.join(b, "applicationUser.blob.json")
    if os.path.exists(blob):
        con = sqlite3.connect(STATE_DB); cur = con.cursor()
        cur.execute("UPDATE ItemTable SET value=? WHERE key=?",
                    (open(blob, encoding="utf8").read(), APP_USER_KEY))
        con.commit(); con.close(); print("  restored applicationUser blob")
    sj = os.path.join(b, "settings.json")
    if os.path.exists(sj):
        shutil.copy2(sj, SETTINGS_JSON); print("  restored settings.json")
    print("回滚完成,重启 Cursor 生效。")


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    ap.add_argument("--repair", action="store_true", help="只重打 bundle(Cursor 升级后失效用;不碰配置)")
    ap.add_argument("--pin-update", action="store_true", help="写 update.mode:none 锁定不升级(默认不写)")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS),
                    type=lambda s: [x.strip() for x in s.split(",") if x.strip()])
    ap.add_argument("--force-version", action="store_true", help="跳过版本闸(自担风险)")
    ARGS = ap.parse_args()
    if isinstance(ARGS.models, str):
        ARGS.models = [x.strip() for x in ARGS.models.split(",") if x.strip()]

    if sys.platform != "darwin":
        print("!! 本脚本目前只支持 macOS。"); sys.exit(1)
    if not os.path.isdir(RES):
        print("!! 找不到 Cursor:", APP); sys.exit(1)

    ver = cursor_version()
    print("Cursor version:", ver)
    if ARGS.revert:
        return revert()

    if not ver.startswith(SUPPORTED_MAJOR_MINOR + ".") and not ARGS.force_version:
        # #4 软版本闸:只告警不拒;真正安全阀=plan_bundle 里锚点必须 exactly-1。
        print("⚠️  本安装器按 Cursor %s.x 设计,当前 %s。将继续尝试;bundle 结构变了认不出锚点会自动拒绝(不改坏)。"
              % (SUPPORTED_MAJOR_MINOR, ver))
    if cursor_running():
        print("!! Cursor 正在运行 —— 请先完全退出(⌘Q)再跑,否则 state.vscdb 写入会被内存覆盖。"); sys.exit(2)

    print("--- 1) bundle 补丁计划 ---")
    plans = []
    for rel in BUNDLES:
        r = plan_bundle(rel)
        if r: plans.append(r)
    print("--- 2) node --check 校验补后 bundle ---")
    for p, txt in plans:
        ok = node_check(txt, os.path.basename(p).split(".")[1])
        if not ok:
            print("   !! 语法校验不过,终止,未落任何盘。"); sys.exit(4)
        print("   node --check OK:", os.path.basename(p))

    # #4 修复模式:只重打 bundle,不碰配置/Key。
    if ARGS.repair:
        if not plans:
            print("\n✅ bundle 补丁都在,无需修复。"); return
        print("--- 修复:重打 bundle ---")
        do_backup(ver, plans, None, None)
        for p, txt in plans:
            open(p, "w", encoding="utf8").write(txt); print("   patched:", os.path.basename(p))
        print("\n✅ 修复完成,重启 Cursor 即可继续用 cursor-g。"); return

    print("--- 3) BYOK 配置差异 ---")
    old_blob = merge_config(dry=not ARGS.apply)
    print("--- 4) update.mode ---")
    if ARGS.pin_update:
        old_settings = set_update_none(dry=not ARGS.apply)
    else:
        old_settings = None
        print("   跳过(允许 Cursor 自动升级;升级后失效跑 --repair)。加 --pin-update 可锁定不升级。")

    if not ARGS.apply:
        print("\n[dry-run] 以上全部通过。加 --apply 执行(会先备份到 %s)。" % BACKUP_ROOT)
        return

    print("--- 落盘 ---")
    if plans:
        do_backup(ver, plans, old_blob, old_settings)
        for p, txt in plans:
            open(p, "w", encoding="utf8").write(txt)
            print("   patched:", os.path.basename(p))
    else:
        # 仍备份配置侧(bundle 无改动时 do_backup 不被调,单独存 blob)
        ts = time.strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(BACKUP_ROOT, "%s-%s-cfgonly" % (ver, ts)); os.makedirs(bdir, exist_ok=True)
        open(os.path.join(bdir, "applicationUser.blob.json"), "w", encoding="utf8").write(old_blob)
        print("   备份(仅配置)->", bdir)

    # Python 版不自动写 Key:本进程非 Cursor 签名,读钥匙串会弹框(JS 版走 in-process keytar 才不弹)。
    print("\n✅ 完成。还差一步(只此一步,交给 Cursor 自己做——key 是钥匙串加密的,Python 版不自动写):")
    print("   1. 启动 Cursor → Settings → Models → OpenAI API Key,粘贴你的 key,点 Verify。")
    print("      (base-url、模型列表、开关都已预填好,默认模型已是 cursor-g-5.6-sol,你只需粘 key。)")
    print("   2. 就绪。(想让脚本自动写 Key 请用 JS 版 cursor_team_setup.sh --apply。)")
    print("   回滚整包:python3 %s --revert;Cursor 升级后失效:python3 %s --repair"
          % (os.path.basename(sys.argv[0]), os.path.basename(sys.argv[0])))


if __name__ == "__main__":
    ARGS = None
    main()
