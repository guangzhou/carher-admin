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
       openAIBaseUrl=<公网网关>、useOpenAIKey=true,并把
       aiSettings.userAddedModels + modelOverrideEnabled **整表换成** DEFAULT_MODELS
       (2026-09-20 第二轮:不再是"读旧去重合并" —— 清单之外的名字,包括同事自己加的,
        会被清掉。所以写库**之前**先落一份旧 blob 到 <ver>-<ts>-preconfig/,见
        backup_blob_before_write;备份写不下去就 exit(4),不带着"没有退路"动手)。
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
      --models cr-g-5.6,cr-g-5.6-instant,cr-g-5.6-mini,cr-g-5.6-pro,cr-g-5.6-thinking,cr-g-5.6-luna

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
# 菜单 = 这份清单,**逐字、按序、全量**(2026-09-20 第二轮改成整表赋值)。
# **必须与 cursor_team_setup.js 的 DEFAULT_MODELS 逐字相等**(含顺序)—— 两份实现分叉过一次
# (js 已换代、py 还留着旧 6 名),门在 setup_impl_parity.py。
# ⚠️ 数组顺序 = 同事菜单里看到的顺序。用户点名要 Grok 系列 + sa-composer 排最前。
# ⚠️ `sa-grok-imagine` 09-20 当天加、当天又被点名移除:chat 线型恒 400(出图专用),
#    留在菜单里 = 点了就报错。**别再加回来**。
# cr-g 前 7 个是载体代表,在真 Cursor 里跑过;紧跟 6 个是档位变体,只共用已验过的载体、
# reasoning_effort 不同 —— 装机文档里标「实验档」。
# 2026-09-21 用户给了新清单(30 名)并点名排序:①sa-* ②gpt-* ③cr-* ④其他。
# 数组顺序 == 菜单顺序,段序与段内顺序都是产品要求,别按字母重排。
# ⚠️ 清单里**故意含 5 个与 Cursor 自带模型同名**的名字(gpt-5.5/gpt-5.6-luna/
#    gpt-5.6-sol/gpt-5.6-terra/kimi-k2.7-code)。实测:同名时 Cursor 不把它登记成
#    自定义模型(不进 userAddedModels),只在 modelOverrideEnabled 里留「走我的 key」
#    开关 —— 靠那个开关照样打到 BYOK 地址。用户点名这样装,别改成带前缀的等价名
#    (会改变菜单显示名),也别据此断言不可用(判据只有拿真 key 实打)。
# 2026-09-21 实打(两把真 key 各跑一轮全 30 名,Cursor 线型 chat+tools+唯一 nonce;
#   工具 = scripts/litellm-198-cursor-newnames-probe.py --key <真key> --names <清单>):
#   cursor-liuguoxian-std(68 models / aliases n=0)  : 27/30 出字,
#     400 = kimi-k2.7-code / glm-5.3-flash / qwen3-coder-next
#   cursor-liuguoxian04-5rub(172 models / aliases n=27): 那 3 个里 qwen3-coder-next 200 出字
# 🔴 同一个名字在两把 key 上读数相反 ⇒ 400 是 key 属性不是名字属性。真因:这些名字在网关里
#    只是 per-key alias 入口、没有同名真实组。有 alias 的 key 改写到真实组 → 200;
#    没 alias 的 key 虽然 allowlist 放行(/v1/models 看得见)但路由找不到落点 → 400。
#    三个 alias 目标直打全部 200 出字(claude-kimi-k2.7-code / zai-coding-glm-5.3-flash /
#    kiro-qwen3-coder-next)⇒ 上游是活的,坏的只是那把 key 缺 alias。
# ⚠️ 两道闸独立:allowlist(models) 决定 403 key_model_access_denied;
#    per-key aliases 决定 400(名字没落点)。「/v1/models 里有这个名」两者都不证明。
# ⚠️ 别再把 cursor-liuguoxian-std 当授权模板(09-21 早些时候写过,当天证伪):它 models 更宽
#    但 aliases 是空的,反而比 04-5rub 少一层。模板是 04-5rub 那 27 条 aliases,
#    至少含 kimi-k2.7-code / glm-5.3-flash / qwen3-coder-next 三条。补 alias 是生产变更。
DEFAULT_MODELS = [
    # ── ① grok/composer 裸名(用户点名排最前;默认模型在本段首位)──
    # 2026-09-22 从 sa-* 换成裸名。前置条件已完成:687 把 cursor-* key 全部补上
    # `aliases 的「裸名 → sa-原名」` + allowlist 裸名(sa_prefix_alias_all.py --apply)。
    # 09-22 加 grok-4.7 并定为默认。它拒绝回显 nonce ⇒ 验活的尺子换算术
    # (6193+2748 → 8941, 2.2s, 假名阴性对照 403);拿 nonce 量它会得到假红。
    "grok-4.7",
    "grok-4.6", "grok-4.6-latest", "grok-4.5-latest", "grok-4.5",
    "grok-4.20", "grok-4.20-0309-reasoning",
    "composer-2.5-fast",
    # ── ② gpt-*(5 个里 4 个与 Cursor 自带同名,见上方说明)──
    "gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5", "gpt-6-astra",
    # ── ③ cr-*(前 7 个是载体代表,在真 Cursor 里跑过)──
    "cr-g-5.6", "cr-g-5.6-instant", "cr-g-5.6-mini", "cr-g-5.6-t-mini",
    "cr-g-research", "cr-g-5.6-thinking", "cr-g-5.6-luna",
    # ↓ 实验档
    "cr-g-5.6-thinking-min", "cr-g-5.6-thinking-high", "cr-g-5.6-thinking-max",
    "cr-g-5.6-luna-min", "cr-g-5.6-luna-high", "cr-g-5.6-luna-max",
    # ── ④ 其他 ──
    "codex-auto-review", "deepseek-v4-flash", "kimi-k2.7-code",
    "glm-5.3-flash", "qwen3-coder-next",
]
# 整表赋值之后「退役」不再需要单独的摘除路径(名字不在 DEFAULT_MODELS 里,跑一遍就没了)。
# 这里只留给选中位 re-point:功能位钉着一个菜单里已经没有的名字时要改回 DEFAULT_MODEL。
RETIRED_MODELS = ["cr-g-5.6-pro", "sa-grok-imagine"]
# 装完直接选中它。2026-09-20 用户点名从 cr-g-5.6 改成 sa-grok-4.6-latest;
# 2026-09-22 全员切裸名后改成 grok-4.6-latest(同一个上游组,走 per-key alias)。
DEFAULT_MODEL = "grok-4.7"
# 判断"当前选中的是不是本方案的名字"用这个前缀。
# ⚠️ 改 DEFAULT_MODEL 时必须一起改这里:漏改的后果是老用户升级后被打回旧名。
# ⚠️ 加新模型名时必须让它命中其中一条,否则 --uninstall 摘不掉它。门在 setup_impl_parity.py。
# 2026-09-21:新清单带进 gpt-* / codex- / deepseek- / kimi- / glm- 五类前缀。
# ⚠️ `gpt-` 这条会同时命中 Cursor 自带的 gpt-*(gpt-5.3-codex、gpt-5.4…)。后果只落在
#    --uninstall 的 drop_ours 上:卸载时会把同事自己加的 gpt-* 自定义名一起摘掉。
#    整表赋值(EXACT)装机本来就会清掉那些名字,方向一致、不新增损失;但别把 is_ours
#    当"这是我们装的"的证据用在别处 —— 它现在是个偏宽的判据。
# 2026-09-22:菜单换成裸名 ⇒ 必须加 grok- / composer- 两条,否则菜单名不命中前缀,
#    --uninstall 的 drop_ours 摘不掉它们。这两条也命中 Cursor 自带的 grok / composer,
#    与 gpt- 同病。sa-grok- / sa-composer- 保留:老用户库里还躺着上一代的 sa- 名字。
MODEL_PREFIXES = ["cr-g-", "grok-", "composer-", "sa-grok-", "sa-composer-",
                  "qwen3-coder-", "gpt-", "codex-", "deepseek-", "kimi-", "glm-"]
def is_ours(name):
    return isinstance(name, str) and any(name.startswith(p) for p in MODEL_PREFIXES)

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
# ── minify 标识符字符集(2026-09-15 修 3.20.21 glass gate 漂移)────────────────────
# 坑:锚点里捕获 minify 名字一直写的是 `\w+` = [A-Za-z0-9_],**不含 `$`**。JS 合法标识符
# 首字符含 `$`,esbuild/terser 名字用尽就大量吐 `$xx`:3.20.21 glass 里 `$` 开头的标识符
# 有 1163 个不同名字、5186 处调用点。3.20.21 上 glass 的 gate 包装函数名从 `qUy` → **`$7y`**
# ⇒ `\w+` 认不出 ⇒ hits=0 ⇒ 安装器整体拒绝动手 = 一个补丁都没打(desktop 抽到 `l_f` 躲过了)。
# 那段代码逐字未变,所以这不是官方重构,是我们锚点字符集写窄了。
# 放宽不放松安全阀:四条 bundle(3.20.17/3.20.21 × desktop/glass)仍全 exactly-1,
# nopromote multi 计数不变(2/5/2/5)。js 版同名常量 ID/ID1,两侧必须同步。
_ID = r"[A-Za-z0-9_$]+"
_ID1 = r"[A-Za-z0-9_$]*"  # 可能为空(参数被摇掉)

QP_MARKER = "@cx-queue-pump:v4"
QP_ANCHOR = re.compile(r"(addToQueue\((" + _ID + r")\)\{if\(!this\.isValidQueueItem\(\2\)\)return;)")
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
         # 3.20.21 glass 包装函数名是 `$7y`(见 _ID 注释);参数位旧写法钉死 `[a-z]`,一并放宽。
         rx=re.compile(r"(modelPickerDisplayConfiguration\?\?" + _ID + r";return )("
                       + _ID + r"\(" + _ID + r"\))(\}resolveModelNameToCatalog)"),
         sub=_gate_sub),
    dict(name="localagent", marker="@cxteam-localagent",
         rx=re.compile(r"((?:clientSupportsRoutedModelUpdate:!0\}|localMode:" + _ID + r"\.localMode\}\));if\()("
                       + _ID + r"\.localMode)(\)\{try\{)"),  # 3.18.25 前缀变了,两代都认
         sub=lambda m: m.group(1) + "/*@cxteam-localagent*/!0" + m.group(3)),
    dict(name="dedicated", marker="@cxteam-dedicated",
         rx=re.compile(r"(" + _ID + r"\(this\.storageService,\"useDedicatedLocalAgentRuntimeHost\"\))(\?await this\.runLocalAgentInDedicatedExtensionHost\()"),
         sub=lambda m: "(/*@cxteam-dedicated*/!1)" + m.group(2)),
    dict(name="queue-pump", marker=QP_MARKER,
         rx=QP_ANCHOR, sub=lambda m: m.group(1) + QP_SNIPPET),
    # qserial(2026-08-25):官方 tryDispatchNextQueueItem 的闸门 Vyb 只看 status,不看
    # "是否已有派发在飞"。实测竞态:heal 写 status 触发响应式监听,官方派发器与泵在 30ms 内
    # 各派一条,后枪的 submitChatMaybeAbortCurrent 掐死前枪的预网络轮 → 两问挤一轮/前问无答。
    # 修:入口加官方自家 inFlightDispatchItemIds 在飞守卫 → 派发严格串行(所有调用方生效)。
    # 兜底:若守卫挡掉了"轮完成事件"的那次派发,泵 1s 内以空闲态补发,最多多等 1-2s。
    dict(name="qserial", marker="@cxteam-qserial",
         rx=re.compile(r"(tryDispatchNextQueueItem\(\)\{const (" + _ID + r")=this\.getComposerHandleIfLoaded\(\);if\(!\2\)return;)"),
         sub=lambda m: m.group(1) + "/*@cxteam-qserial*/if(this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0)return;"),
    # nosteer-mod(2026-08-25 真凶):官方把「配置=queue 但按修饰键发送」设计为强制 steer
    # (⌘+回车正是修饰键!)→ 生成中发的每条都被注入当前轮 → 两问挤一轮/前问无答。
    # 本团队发送手势就是 ⌘+回车,故掐掉该 override:修饰键照常发送,但行为仍是 queue。
    # 3.19.19 起该 switch 搬进纯函数 Meo({newMessageBehavior,manualSendBehavior,isAlternate}),
    # send/queue 分支修饰键结果变成 `n?t=="steer"?"steer":"stop-and-send":"queue"`
    # (非 steer 落 stop-and-send=掐断当轮立即发,对本线同样坏)。两代都认,命中仍必须恰好 1 次。
    dict(name="nosteer-mod", marker="@cxteam-nosteermod",
         rx=re.compile(r'case"send":case"queue":return(?: (' + _ID + r'&&' + _ID + r'\(' + _ID + r'\)\?\{behavior:")steer(",isModifierOverride:!0\})'
                       r'|\{behavior:(' + _ID + r')\?' + _ID + r'==="steer"\?"steer":"stop-and-send":"queue",isAlternate:\3\})'),
         sub=lambda m: ('case"send":case"queue":return ' + m.group(1) + 'queue' + m.group(2) + '/*@cxteam-nosteermod*/')
                       if m.group(1) is not None else
                       ('case"send":case"queue":return{behavior:/*@cxteam-nosteermod*/"queue",isAlternate:'
                        + m.group(3) + '}')),
    # nopromote(2026-08-25 终极 steer 封口):3.17 自动把排队消息 steer 注入当前轮(预网络
    # 窗口内注入 → 一请求两问 → 模型只答最后一条)。promoteQueueItemToSteer 是所有 steer
    # 注入的总入口(gate/自动/NUX),短路=全部走老实排队。multi:glass 打包多份 composer,
    # 全部命中都要打(count>=1 即可,逐处插入)。
    dict(name="nopromote", marker="@cxteam-nopromote", multi=True,
         # 参数位用 _ID1(可为空):将来摇成 `promoteQueueItemToSteer(){` 也认得出。
         rx=re.compile(r'(async promoteQueueItemToSteer\(' + _ID1 + r'\)\{)'),
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
         rx=re.compile(r'(if\()(' + _ID + r'\(\{(?:agentBackend:' + _ID + r',)?isLocalMode:' + _ID
                       + r'\.localMode,isAgentHostEnabled:' + _ID
                       + r',isNewRequestIdGateEnabled:\(\)=>this\.isQueuedPromptNewRequestIdEnabled\(\)\}\))(\)\{)'),  # 3.18.25 起无 agentBackend
         sub=lambda m: m.group(1) + '/*@cxteam-norelay*/!1&&' + m.group(2) + m.group(3)),
    # nocloud(2026-09-23):把会话钉死在本地线,不许被"送去 Cloud Agents"。
    # 病:3.21.18 上发消息弹「Upgrade to run Cloud Agents / not available on your current plan」
    # ⇒ 请求被送去 Cursor 云端跑、被套餐闸门拒 ⇒ 我们的模型一个都用不上。
    # 唯一岔路口 shouldRunOnBeforeSubmitChat() 里的 pendingBackgroundAgent 那条腿为真就改道云端;
    # 该标志持久化在会话里,升级/重启不清。置位来源:Send-to-Cloud 误点,或服务端打开
    # push_local_agent_to_cloud / send_to_cloud_on_followup(都是 client:!0,default:!1)。
    # submit 摘腿治已中招的老会话(Oke() 那条腿保留=用户主动开的云端会话不动),set 堵住源头。
    # 名字位 desktop/glass 全不同(e/t、Os/ji、Hs/vr)⇒ 一律 _ID 捕获 + 反向引用。
    dict(name="nocloud-submit", marker="@cxteam-nocloud",
         rx=re.compile(r'(shouldRunOnBeforeSubmitChat\(\)\{const (' + _ID
                       + r')=this\.composerDataService\.getComposerData\(this\.getComposerHandle\(\)\);return \2!==void 0&&'
                       + _ID + r'\(\2\))\|\|!!\2\?\.pendingBackgroundAgent(\})'),
         sub=lambda m: m.group(1) + '/*@cxteam-nocloud*/' + m.group(3)),
    # ⚠️ 09-23:写入点不止一个(ComposerPlanService 那处前面多带 `unifiedMode:L`),
    #    放开成「对象里任意位置」+ multi;每条 bundle 恰好 2 处。详注见 js 同名条目。
    dict(name="nocloud-set", marker="@cxteam-nocloudset", multi=True,
         rx=re.compile(r'(updateComposerData\(' + _ID + r',\{[^{}]{0,160}?pendingBackgroundAgent:)(' + _ID + r')(\}\))'),
         sub=lambda m: m.group(1) + '/*@cxteam-nocloudset*/!1' + m.group(3)),
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


def apply_patches_to_text(src, quiet=False):
    """把 PATCHES 打进一段文本,返回 (out, applied)。plan_bundle 与测试钩子 CX_APPLY_TO_FILE
    **共用这一份**——钩子跑的必须就是真跑的语义,否则回归台架是假绿。"""
    say = (lambda *a: None) if quiet else print
    out = src
    applied = []
    for pt in PATCHES:
        if pt["marker"] in out:
            say("   SKIP %-11s(已打过)" % pt["name"]); continue
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
    return out, applied


def plan_bundle(rel):
    """返回 (path, patched_text) 或 None(无需动/已打过);命中异常直接 SystemExit。"""
    p = os.path.join(RES, rel)
    src = open(p, encoding="utf8", errors="replace").read()
    out, applied = apply_patches_to_text(src)
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
    # ── 2026-09-20 第二轮:整表赋值(EXACT)。与 js 版 mergeConfig 同语义 ──────────
    # 原来 dedup 是**纯并集**:只加不减 ⇒ 同事库里越堆越多(本机实测 userAddedModels
    # 52 条),菜单一屏找不到要用的那个,"文档写了什么"和"他菜单里有什么"永久对不上。
    # 用户点名:除文档里那份之外全部去掉,包括他自己配的。所以两个数组直接等于
    # DEFAULT_MODELS,顺序也跟常量走(菜单顺序 = 数组顺序)。
    # ⚠️ 这是**破坏性**的 ⇒ 写库前必须先落一份旧 blob(见下面 backup_blob_before_write)。
    want_models = list(ARGS.models)
    exact = len(want_models) > 0
    want_set = set(want_models)
    # 选中位 re-point 的判据 = "在不在新清单里"(不在 = 菜单里已经没有它了)。
    def is_retired(x):
        return isinstance(x, str) and exact and x not in want_set
    uam_before = list(ai.get("userAddedModels") or [])
    ove_before = list(ai.get("modelOverrideEnabled") or [])
    if exact:
        seen, outl = set(), []
        for x in want_models:
            if x not in seen:
                seen.add(x); outl.append(x)
        ai["userAddedModels"] = list(outl)
        ai["modelOverrideEnabled"] = list(outl)
    else:
        ai["userAddedModels"] = uam_before
        ai["modelOverrideEnabled"] = ove_before
    added = [m for m in want_models if m not in uam_before]
    removed = ([m for m in dict.fromkeys(uam_before + ove_before) if m not in want_set]
               if exact else [])
    # #3 默认模型:装完直接选中 DEFAULT_MODEL。保守——仅当当前选中的不是本方案的名字时才设。
    # 注意 "cursor-g-*"(旧代)不以 MODEL_PREFIXES 之一开头,所以老用户升级会被换到新名 —— 这是换代想要的。
    mc = ai.setdefault("modelConfig", {})
    # 退役名若正被某功能位选中,只摘 userAddedModels 不够:菜单里没了、composer 还钉着它
    # ⇒ 一发消息就报错且他在菜单里找不到那个名字。按值扫全部功能位(不只 composer/cmd-k)。
    repointed = []
    for feat in list(mc.keys()):
        e = mc.get(feat)
        if not isinstance(e, dict):
            continue
        sel = e.get("selectedModels")
        sel_retired = isinstance(sel, list) and any(
            isinstance(s, dict) and is_retired(s.get("modelId")) for s in sel)
        if not is_retired(e.get("modelName")) and not sel_retired:
            continue
        was = e.get("modelName")
        f = dict(e)
        f["modelName"] = DEFAULT_MODEL
        f["selectedModels"] = [{"modelId": DEFAULT_MODEL, "parameters": []}]
        mc[feat] = f
        repointed.append("%s:%s→%s" % (feat, was or "?", DEFAULT_MODEL))
    cur_name = (mc.get("composer") or {}).get("modelName")
    if not is_ours(cur_name):
        for feat in ("composer", "cmd-k"):
            f = dict(mc.get(feat) or {})
            f["modelName"] = DEFAULT_MODEL
            f["selectedModels"] = [{"modelId": DEFAULT_MODEL, "parameters": []}]
            mc[feat] = f
        def_model_set = DEFAULT_MODEL
    else:
        def_model_set = "(skip, 已是 %s)" % cur_name
    print("   config diff: baseUrl %r -> %r ; useOpenAIKey %r -> True ; defaultModel -> %s"
          % (before["baseUrl"], ARGS.base_url, before["useKey"], def_model_set))
    if exact:
        print("   模型清单 = 本方案那 %d 个(整表赋值,顺序即菜单顺序):%d 条 -> %d 条"
              % (len(want_models), len(uam_before), len(ai["userAddedModels"])))
        if added:
            print("     + 新增 %d:%s" % (len(added), ",".join(added)))
        # 删掉了什么必须**逐个列出来**,不能只报个数字:这一步是破坏性的。
        if removed:
            print("     - 移除 %d:%s" % (len(removed), ",".join(removed)))
        if not added and not removed:
            print("     (已经一致,无变化)")
        if repointed:
            print("     选中位改回:[%s]" % ", ".join(repointed))
    else:
        print("   模型清单:一个字不动(--models 为空)")
    # 备份必须在写库**之前**:整表赋值会删掉同事自己加的名字,窗口里崩一次旧清单
    # 就只存在过内存里,永久丢失。见 js 版同一处注释。
    if not dry:
        pre = backup_blob_before_write(row[0])
        if pre:
            print("   ✔ 改配置前已备份旧清单 -> %s" % pre)
        cur.execute("UPDATE ItemTable SET value=? WHERE key=?",
                    (json.dumps(d, ensure_ascii=False), APP_USER_KEY))
        con.commit()
    con.close()
    return row[0]  # old blob (for backup)


def backup_blob_before_write(raw):
    """写库前的单点快照:**只**存 applicationUser blob。备份失败 = exit(4),
       绝不带着"没有退路"往下写。目录名 `<ver>-<ts>-preconfig`,与 js 版一致。"""
    bdir = os.path.join(BACKUP_ROOT, "%s-%s-preconfig" % (cursor_version(), time.strftime("%Y%m%d-%H%M%S")))
    try:
        os.makedirs(bdir, exist_ok=True)
        open(os.path.join(bdir, "applicationUser.blob.json"), "w", encoding="utf8").write(raw)
    except Exception as e:
        print("   !! 改配置前备份失败(%s)——拒绝继续,你的模型清单一个字没动。" % e)
        print("      这一步会用本方案那份清单**整表替换**你库里的清单,没有备份不许动手。")
        sys.exit(4)
    return bdir


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
    # 只取目录 —— 备份根目录里还躺着 zk-delta 指纹之类的散装 JSON 文件,它们的名字排在版本号
    # 后面,`baks[-1]` 会挑到一个文件当"备份目录"(js 侧实测踩过,同一个坑)。
    baks = sorted(d for d in os.listdir(BACKUP_ROOT)
                  if os.path.isdir(os.path.join(BACKUP_ROOT, d))) if os.path.isdir(BACKUP_ROOT) else []
    if not baks:
        print("!! 无备份可回滚"); sys.exit(1)
    # 优先选「含 bundle 的最新备份」(跳过 -cfgonly:那种只存了配置,回滚它会漏掉 bundle);都没有再退回最新
    def has_bundle(d):
        return any(os.path.exists(os.path.join(BACKUP_ROOT, d, os.path.basename(rel))) for rel in BUNDLES)
    def bak_version(d):
        m = re.match(r"^(\d+\.\d+\.\d+)-", d)
        return m.group(1) if m else None
    # **只回滚与当前 Cursor 同版本的备份**。跨版本盖 bundle = 把新版 app 塞进旧版界面文件,
    # Cursor 起不来。这机器上就有过 live 3.20.17 / 最新含 bundle 备份 3.18.25 的组合。
    ver = cursor_version()
    same = [d for d in reversed(baks) if bak_version(d) == ver]
    pick = next((d for d in same if has_bundle(d)), same[0] if same else None)
    if pick is None:
        newest = next((d for d in reversed(baks) if has_bundle(d)), None)
        print("!! 没有与当前 Cursor %s 同版本的备份 —— 拒绝回滚(不拿旧版 bundle 覆盖新版 app)。" % ver)
        if newest:
            print("   最近的含 bundle 备份是 %s(版本 %s),盖上去会把 Cursor 弄坏。"
                  % (newest, bak_version(newest) or "?"))
        print("   想恢复原状请用 js 版的 --uninstall(它在没有同版本备份时会教你用官方安装包重装,"
              "配置/Key 照样清干净):node cursor_team_setup.js --uninstall --apply")
        sys.exit(1)
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
    print("      (base-url、模型列表、开关都已预填好,默认模型已是 %s,你只需粘 key。)" % DEFAULT_MODEL)
    print("   2. 就绪。(想让脚本自动写 Key 请用 JS 版 cursor_team_setup.sh --apply。)")
    print("   回滚整包:python3 %s --revert;Cursor 升级后失效:python3 %s --repair"
          % (os.path.basename(sys.argv[0]), os.path.basename(sys.argv[0])))


if __name__ == "__main__":
    ARGS = None
    # 测试钩子(与 JS 版同名同语义):对任意文件跑真实 PATCHES,写 CX_APPLY_OUT,
    # 供 js/py 产物逐字节等价断言(bundle_patch_regress.sh 第③腿)。
    if os.environ.get("CX_APPLY_TO_FILE"):
        _f = os.environ["CX_APPLY_TO_FILE"]
        _out, _applied = apply_patches_to_text(open(_f, encoding="utf8", errors="replace").read(), quiet=True)
        open(os.environ.get("CX_APPLY_OUT") or (_f + ".pyout"), "w", encoding="utf8").write(_out)
        sys.stderr.write("applied: " + (", ".join(_applied) or "(无,全已打过)") + "\n")
        sys.exit(0)
    main()
