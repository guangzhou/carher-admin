#!/usr/bin/env node
/*
cursor_team_setup.js — 一条命令给同事装好 cr-g(Cursor 3.16~3.21 验过 / macOS+Windows+Linux)。

═══ 零依赖原理 ═══
不需要 Python、不需要装 Node:Cursor 本体就是 Electron,自带完整 Node 运行时
(ELECTRON_RUN_AS_NODE=1)+ 内置 node:sqlite。本脚本用 Cursor 自己跑自己:
  macOS   : ./cursor_team_setup.sh  [--apply|--revert|...]
  Windows : cursor_team_setup.cmd   [--apply|--revert|...]
两个启动器只做一件事:找到 Cursor 可执行文件,以 node 模式运行本 JS。
安装目录/用户目录全部从 process.execPath 推导,不猜路径。

═══ 做的事(全幂等+自动备份+失败即拒) ═══
  1. 软版本闸:VERIFIED_VERSIONS(3.16~3.21)里数过锚点命中,别的版本只告警不拒;
     真正安全阀=bundle 锚点必须 exactly-1(结构变了认不出 → 自动拒绝,绝不改坏)。
  2. Cursor GUI 必须已退出(排除本进程自身;外部写 state.vscdb 有内存覆盖竞态)。
  3. 两条 workbench bundle(desktop+glass)打 3 处解锁补丁 + 排队泵 v3:
     锚点=稳定语义地标+通用捕获,全文件恰好命中 1 次才动手;补完整 bundle 过
     `--check` 语法校验才落盘。旧版排队泵(v1/v2diag)在场则原地升级 v3。
  4. BYOK 配置并进 state.vscdb applicationUser blob(node:sqlite,读旧去重合并):
     base-url、useOpenAIKey、**这把 Key 在网关上能用的模型**(现拉 /models,出图名除外,按家族排),
     默认选中 DEFAULT_MODEL(Key 里没有就用第一个);不在新清单里的旧名字整表删掉。
  5. #2 写 Key:mac 用 in-process keytar 读钥匙串主密码 + OSCrypt(AES-128-CBC)加密写
     secret://cursorAuth/openAIKey。写前两道自检(能解开现有 Key + 加密回环)才落盘,
     否则回退手填(防假绿 401)。win=DPAPI 未实测→回退手填;linux=best-effort。
  6. 默认**禁止** Cursor 自动升级(10-13 起;--allow-update 才放开)—— 升级会冲掉全部补丁。
  7. zk-delta 本机小代理(默认开,只 macOS 实测):Cursor → 127.0.0.1:8788 → 公网只发增量
     → 198 上重建出逐字节相同的全量 → LiteLLM。**上游收到的字节和不装它时完全一样**,
     省的只是"你家宽带 → 机房"这一跳(实测 12 轮省 86.2%)。小代理用 Cursor 自带 Electron 跑
     (同事不用装 Node);同事机器上抓包硬关(不往你磁盘写任何请求体);
     集群挂→小代理自动回落直连,进程挂→launchd KeepAlive 拉起,想彻底退→--no-zk-delta。

用法(不带参数=dry-run 只检查不写):
  --apply            执行(末尾会提示粘一次 Key;可用 --key <k> 无人值守)
  --upgrade          老用户升级:=--apply 但**不问 Key**(Key 已在库里)。按 Key 重拉菜单
                     (新的加、旧的删,选中位若钉着已删的名字则改回默认)
                     + 重打 bundle 补丁 + 更新小代理。库里没 Key 时会明确报出来,不假绿。
  --repair           只重打 bundle 补丁(Cursor 升级后失效用;不碰配置/Key)
  --refresh-models   「刷模型」:只按 Key 重拉模型菜单(Cursor 开着会自动退出→写→重开),不碰别的
  --revert           从最近备份回滚(bundle+blob+settings+Key secret,并卸掉小代理)
                     ⚠️ 只回滚**与当前 Cursor 同版本**的备份;版本不匹配会拒绝并让你走 --uninstall
  --uninstall        彻底恢复原状:bundle 还原(同版本备份→否则教你重装 Cursor)+ BYOK 配置清掉
                     + 摘 Key + 卸小代理 + 去掉 update.mode 锁。比 --revert 稳:不挑错版本的备份
  --key <k>          直接给 Key(CI/无人值守;否则 TTY 下交互粘)
  --allow-update     允许 Cursor 自动升级(默认**禁止**:写 update.mode=none 等三项,
                     升级会冲掉全部补丁 ⇒ BYOK 又报 custom API keys)
  --chain            装 @cx-chain:v3 链式增量(默认**不装**:服务端那半已下线,装了只会
                     让上游静默少收上下文。不给这个开关时,安装器会把已装的自动摘掉。)
  --no-zk-delta      不装小代理 / 把已装的卸掉,BYOK 地址退回公网直连
  --zk-delta-only    **只**装小代理 + 改 BYOK 地址:不碰 bundle、不碰模型、不碰 Key
  --lark             (默认不做)装飞书 lark-cli / lark-* skills / 飞书登录;已有的一律跳过
                     (给已经装好的机器加增量传输用;也是唯一不会换掉选中模型的模式)
  --keep-model       不覆盖当前选中模型(默认逻辑是"不是 MODEL_PREFIXES 之一开头就换成 DEFAULT_MODEL",
                     在基准机上那等于换掉量具)
  --base-url <url> / --models a,b,c / --force-version
  --bare-sa          把菜单里的 sa-* 换成裸名(sa-grok-4.6 → grok-4.6)。裸名是 Cursor
                     自带目录认识的名字,菜单里才画得出 Context/Effort/Fast 控件。
                     🔴 **默认关**:裸名能不能用是 key 属性,得先用 sa_prefix_alias.py
                     在那把 key 上补 models+aliases 两处,否则点一个报一个(403/400)。
                     ⚠️ 控件画出来 ≠ 档位能传到上游(Cursor 在 BYOK 线上不发这些字段)。
回滚备份在 ~/.cursor-team-setup-backup(与 Python 版同格式,互相可 revert)。
⚠️ 装过 CursorX 的机器:3 解锁锚点命中 0 → 自动拒绝(防重复打),先还原 pristine bundle。
*/
"use strict";
const fs = require("fs");
const path = require("path");
const os = require("os");
const crypto = require("crypto");
const readline = require("readline");
const { spawnSync } = require("child_process");

/* ── 平台路径推导(全部从运行时自身出发,不猜安装位置) ── */
/* 🔴 2026-10-28:EXEC 被**写进三处会长期留在机器上的东西**里:
     ① zk-delta 小代理的 launchd plist(ProgramArguments[0])
     ② 「刷模型」skill 里那条命令 ③ 画图 skill 里那条命令
   被 Cursor 二进制当 node 跑时它就是 Cursor,写进去是对的(本机现状即如此)。
   但管理员在仓库里直接 `node cursor_team_setup.js --apply` 时它是 **node 自己**,
   例如 `/opt/homebrew/Cellar/node/26.12.0/bin/node` —— 带版本号的目录,
   homebrew 一升级 node 这个路径就不存在了 ⇒ 小代理起不来(而 Cursor 的 BYOK 地址
   还指着 127.0.0.1:8788 = 直接连不上)、两个 skill 也一起失效,**全都不报错**。
   ⇒ 落盘用的那个二进制单独算一份:优先用「真的是 Cursor」的那个,
     推不出来再退回 RES 推导出的安装位置;两者都不是才退回 process.execPath。
   纯读的地方(syntaxCheck 的 --check、pgrep 模式)仍用 process.execPath,行为不变。 */
const EXEC = process.execPath; // 当前运行时(被 Cursor 当 node 跑时 = Cursor 本体)
/* 🔴 2026-10-28:`process.execPath` 只有在**被 Cursor 二进制当 node 跑**时才指向 Cursor
   (打包里的 cursor_team_setup.sh 就是这么起的)。直接 `node cursor_team_setup.js`
   (管理员在仓库里最常这么跑)时它指向 node 自己 ⇒ RES 算成 node 的 Resources/app,
   于是 DOCTOR 把 workbench / ctxwin / noloop 全报「不存在」= 满屏假红,
   而那台机器上补丁其实好好的。判据:推出来的目录里**没有 package.json**。
   ⇒ 推不出来就退回常规安装位置;仍找不到才如实说找不到(不假装有)。
   不改 Cursor 二进制跑时的行为:那条路径有 package.json,第一发就返回。 */
function appRoot() {
  if (process.env.CURSOR_APP_ROOT) return process.env.CURSOR_APP_ROOT; // 测试钩子
  const d = path.dirname(EXEC);
  const fromExec = process.platform === "darwin"
    ? path.join(d, "..", "Resources", "app")
    : path.join(d, "resources", "app"); // win32 / linux
  const looksLikeCursor = (r) => { try { return fs.existsSync(path.join(r, "out", "vs", "workbench")); } catch (e) { return false; } };
  if (looksLikeCursor(fromExec)) return fromExec;
  for (const c of appRootFallbacks()) if (looksLikeCursor(c)) return c;
  return fromExec;   // 都不是 ⇒ 维持原值,由调用方如实报「不存在」
}
// 常规安装位置(只在 execPath 推不出来时才查)。CURSOR_APP 允许手动指。
function appRootFallbacks() {
  const out = [];
  const add = (app) => { if (app) out.push(process.platform === "darwin" ? path.join(app, "Contents", "Resources", "app") : path.join(app, "resources", "app")); };
  add(process.env.CURSOR_APP);
  if (process.platform === "darwin") { add("/Applications/Cursor.app"); add(path.join(os.homedir(), "Applications", "Cursor.app")); }
  else if (process.platform === "win32") {
    for (const b of [process.env.LOCALAPPDATA && path.join(process.env.LOCALAPPDATA, "Programs", "cursor"), process.env.ProgramFiles && path.join(process.env.ProgramFiles, "cursor")]) if (b) out.push(path.join(b, "resources", "app"));
  } else { out.push("/usr/share/cursor/resources/app", "/opt/cursor/resources/app"); }
  return out;
}
function userDir() {
  if (process.env.CURSOR_USER_DIR) return process.env.CURSOR_USER_DIR; // 测试钩子
  if (process.platform === "darwin") return path.join(os.homedir(), "Library", "Application Support", "Cursor", "User");
  if (process.platform === "win32") return path.join(process.env.APPDATA || "", "Cursor", "User");
  return path.join(os.homedir(), ".config", "Cursor", "User");
}
const RES = appRoot();
/* 写进 plist / skill 的 Cursor 二进制。只在「要落盘的路径」用它,不改只读逻辑。
   mac: <app>/Contents/MacOS/Cursor(RES = <app>/Contents/Resources/app);
   win/linux: <dir>/Cursor.exe | <dir>/cursor(RES = <dir>/resources/app)。 */
function cursorBin() {
  const isCursorExe = (p) => /(?:^|[\\/])(?:Cursor(?:\.exe)?|cursor)$/.test(p);
  if (isCursorExe(EXEC)) return EXEC;                       // 被 Cursor 当 node 跑(打包启动器)
  // RES = mac:<app>/Contents/Resources/app ; win/linux:<dir>/resources/app
  //   ⇒ mac 的二进制在 RES/../../MacOS/Cursor;win/linux 在 RES/../../<exe>
  const up2 = path.resolve(RES, "..", "..");                 // mac: <app>/Contents ; win/linux: <dir>
  const cands = process.platform === "darwin" ? [path.join(up2, "MacOS", "Cursor")]
    : process.platform === "win32" ? [path.join(up2, "Cursor.exe")]
    : [path.join(up2, "cursor"), path.join(up2, "Cursor")];
  for (const c of cands) { try { if (fs.existsSync(c)) return c; } catch (e) { /* 继续 */ } }
  return EXEC;                                              // 找不到就维持原行为(不假装)
}
const BUNDLES = [
  path.join("out", "vs", "workbench", "workbench.desktop.main.js"),
  path.join("out", "vs", "workbench", "workbench.glass.main.js"),
];
const STATE_DB = path.join(userDir(), "globalStorage", "state.vscdb");
const SETTINGS_JSON = path.join(userDir(), "settings.json");
const APP_USER_KEY = "src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl" +
  ".persistentStorage.applicationUser";
const BACKUP_ROOT = path.join(os.homedir(), ".cursor-team-setup-backup");

/* 🔴 2026-10-28:这张表原来只按**大版本**判(["3.16",…,"3.24","3.27"]),而证据是**小版本**
   (10-12 只在 3.24.12 上数过)。后果:同事的 3.24.18 / 3.25.9 / 3.27.22 大版本对得上 ⇒
   软闸**一声不吭**,而那几个小版本我们从没数过锚点。10-28 的「自动压缩风暴」4 个人里
   就有 3.24.18。锚点真漂了有 planBundle 兜底(命中 !=1 就拒绝)、现在还会列红,
   但屏幕上**事先**不该假装"这版验过"。
   ⇒ 改成记真实量过的小版本;大版本对得上、小版本没量过 ⇒ 打一行提示并指向 DOCTOR。
   (只改"说什么",不改"做什么":照样继续尝试,真正的安全阀仍是锚点 exactly-1。) */
const VERIFIED_EXACT = [
  "3.16.0", "3.17.19", "3.18.25",        // 09-03
  "3.19.19",                              // 09-10
  "3.20.17", "3.20.21",                   // 09-15
  "3.21.16", "3.21.18",                   // 09-22 / 09-23(件C 四目标 4/4)
  "3.24.12",                              // 10-12
  "3.27.12", "3.27.14",                   // 10-26
  "3.28.5",                               // 10-28 全量量过:workbench 16 锚点 ×2 份全 exactly-1、件D 2/2、件C 3 个该打的全过 + 1 个不适用(agent-host 那份上游挪走了)
];
const VERIFIED_VERSIONS = [...new Set(VERIFIED_EXACT.map((v) => v.split(".").slice(0, 2).join(".")))];
/* 2026-09-22 加 "3.21" 的实测依据(不是"看起来应该行"):同事报 3.21.16/win32 弹未验告警,
   于是把 3.21.16 的真 bundle 拉下来数了命中 —— 下载 commit 8ae78e8ee1e63479c7e0504b664bc0a80c6800f
   的 darwin-arm64 dmg(win32 那份是 Inno 6.4 打的,本机 innoextract 1.9 解不开;同一 commit 的
   workbench bundle 是同一份构建产物),desktop+glass 两条跑 CX_APPLY_TO_FILE:
     desktop: gate, localagent, dedicated, queue-pump, qserial, nosteer-mod, nopromote(x2), norelay
     glass  : gate, localagent, dedicated, queue-pump, qserial, nosteer-mod, nopromote(x5), norelay
   非 multi 的七条全 exactly-1(不是 1 就 exit(2),所以"列出来"本身就是 ==1 的证据),
   nopromote 的 multi 计数 2/5 与 3.20.x 逐字不变。AST 路径(CX_REQUIRE_AST=1)与正则路径
   (CX_FORCE_REGEX=1)分别跑,两条路径命中形状一致 ⇒ 3.20.21 那种 `qUy`→`$7y` 的漂移这版没有。
   阴性对照:把 3.21.16 的 desktop bundle 截到前 5MB 再跑,gate 命中=0 → 拒绝动手
   (证明这把尺子在 3.21.16 上真会红,不是恒绿)。
   ⚠️ 与本次无关但顺带量到的既有状态:件B @cx-chain 的锚 `customHeaders:d}=e,m=` 在
   3.21.16 的两条 exthost bundle 上 count=0 —— 但本机 pristine 的 3.20.17 上同样是 0
   (marker/__cxWrap 都不在,排除"已打过"),所以是早就漂掉的老状态,不是 3.21 新增回归;
   它按设计非致命(告警跳过,不阻断解锁补丁)。
   2026-09-23 3.21.18(Universal)实测补记:8 条解锁锚点在 desktop/glass 上仍是
   非 multi 全 exactly-1、nopromote 2/5 逐字不变 ⇒ 解锁面零漂移。
   🔴 但件C(ctxwin)在 3.21.18 上原本全灭:锚点 B 的局部变量 `const n=[]` 摇成
   `const r=[]`、锚点 C 的第三参数与局部变量互换(`(e,t,n)/const r`→`(e,t,r)/const n`),
   三条 min bundle 全部命中 0(daemon.cjs 不受影响,它不 minify)。原因是我把 minify 的
   **参数名和局部变量名写死**了 —— 与 3.20.21 栽在函数名(`qUy`→`$7y`)同一个病,只是低一层。
   现已全部改成 ID 捕获 + 反向引用绑定语义;3.21.18 与 3.20.17 两代都 4/4,产物与单机版
   逐字节相同。台架同时补了①b 腿(新版 app 件C 4/4),之前①只量 workbench,件C 没人看。
   2026-10-12 加 "3.24" 的实测依据(3.21 之后官方跳到 3.24,中间 3.22/3.23 我们没见过机器,
   **故不写进 VERIFIED_VERSIONS** —— 这张表只记真数过命中的版本,软闸漏报比假绿好):
   下 commit 37b865941ccb31e2fc90c685157534efca87f964 的 darwin-arm64 dmg(先比 Content-Length
   295402051 再挂载),desktop+glass 两条跑 CX_REQUIRE_AST=1 + CX_APPLY_TO_FILE:
     desktop: gate, localagent, dedicated, queue-pump, qserial, nosteer-mod, nopromote(x2),
              norelay, nocloud-submit, nocloud-set(x2), keepmine-noevict, keepmine-claim,
              byok-force, byok-keepon
     glass  : 同上,nopromote(x5)
   非 multi 的 13 条全 exactly-1(不是 1 就 exit(2),"列出来"本身即 ==1 的证据),
   nopromote 的 multi 计数 2/5、nocloud-set 2/2 与 3.21.x 逐字不变。
   🔴 **正则路径(py 那条)上 `localagent` 命中 0**,AST 路径 15/15 全中 ⇒ 这是 minify
   名字漂移、不是结构变了,按 09-15 立的规矩**不动 LOCATORS 也不动正则**。
   代价要说清:`cursor_team_setup.py` 在 3.24 上会少打 localagent 这一处;它不进交付 zip
   (只发 js),所以同事侧无影响,但第③腿(js==py byte-equal)在 3.24 上不可能绿 ——
   那是两实现对同一版本的真实分叉,不许靠改期望值糊过去。 */
const DEFAULT_BASE_URL = "https://cc.auto-link.com.cn/pro/v1";
/* ── 菜单 = 这把 key 在网关上能用的模型(2026-10-19 起**动态**,不再写死)──────────────
   原来这里是一个写死的 39 名数组(DEFAULT_MODELS)。10-19 量 198:488 把当代 cursor key 全都有
   gpt-reserve / kimi-k3,菜单里却没有;菜单里的 deepseek-v4-flash / composer-2.5 已经 0 把 key 有,
   gpt-5.2 / gpt-5.4 / grok-4.6-latest 等只剩 2~4 把 ⇒ 同事点了就 403。
   用户点名:执行脚本时从这把 key 的模型列表现拉,旧的删、新的加。
   ⇒ INSTALL / UPGRADE / `--refresh-models`(「刷模型」)都走 fetchKeyModels → orderMenu。
   规则(用户 10-19 拍板):
     · 出图/视频/音乐专用名(名字里有 image / imagine / video / music)不进聊天菜单 —— chat 线型上点了就报错,画图走 carher-image skill。
     · 按家族分段:① grok / composer ② gpt ③ claude ④ 其他;段内 DEFAULT_MODEL 排第一,其余按名字。
     · 拉不到(没 Key / 断网 / 网关返回空)就**不动**现有菜单,明说原因 —— 绝不拿空列表把人家菜单清空。
   ⚠️ 「/v1/models 里有」≠「点了一定出字」(缺 alias 的名字 400、上游挂了 502),那是 key/上游的事,
      同事发「测模型」能看到逐个读数。历史读数(cr-g 下架、出图名实打 400/500、同名撞 Cursor
      自带模型等)见 git 历史与 skill cursor-client-bundle-patch。 */
const MENU_SKIP = /image|imagine|video|music/i;   // 出图/视频/音乐生成名(10-19 网关 mode 实读:web-gemini-video=video_generation)
const MENU_SEGMENTS = [/^(grok-|composer-)/, /^gpt-/, /^claude-/];
function orderMenu(ids, preferred) {
  const seen = new Set(), names = [];
  for (const x of ids || []) {
    if (typeof x !== "string" || !x || seen.has(x) || MENU_SKIP.test(x)) continue;
    seen.add(x); names.push(x);
  }
  const seg = (n) => { const k = MENU_SEGMENTS.findIndex((rx) => rx.test(n)); return k < 0 ? MENU_SEGMENTS.length : k; };
  return names.sort((a, b) => (seg(a) - seg(b)) || ((b === preferred) - (a === preferred))
    || a.localeCompare(b, "en", { numeric: true }));
}
// 默认选中:DEFAULT_MODEL 在这把 Key 里就用它,不在就用菜单第一个(10-16 fenglili 那种只开 deepseek 的 Key)。
function pickDefault(menu) { return menu.includes(DEFAULT_MODEL) ? DEFAULT_MODEL : (menu[0] || DEFAULT_MODEL); }
/* RETIRED_MODELS：历史下架名。删除早已不靠它 —— 整表赋值下,不在这把 Key 菜单里的名字都会被移出。
   留着只为两件事:报告里点名,以及前缀表/--uninstall 认得老机器上还躺着的这些名字。 */
const RETIRED_MODELS = ["cr-g-5.6-pro", "sa-grok-imagine",
  // 2026-10-12 用户点名把整个 cr-g 段摘掉（上面段 ③ 原来那 13 个名）。
  "cr-g-5.6", "cr-g-5.6-instant", "cr-g-5.6-mini", "cr-g-5.6-t-mini",
  "cr-g-research", "cr-g-5.6-thinking", "cr-g-5.6-luna",
  "cr-g-5.6-thinking-min", "cr-g-5.6-thinking-high", "cr-g-5.6-thinking-max",
  "cr-g-5.6-luna-min", "cr-g-5.6-luna-high", "cr-g-5.6-luna-max"];
/* 与 Cursor 自带模型同名的那几个（2026-09-21 本机实测：写进 userAddedModels 的 30 条，
   Cursor 启动后自己剔掉了这 5 条 —— 读回来 userAddedModels=25 / modelOverrideEnabled=30，
   差集恰好是下面这张表）。
   后果是**产品级**的，不是 bug：它们不出现在 Settings → Models 的「自定义模型」区，
   而是混在上面 Cursor **自带**的模型列表里，靠 `modelOverrideEnabled` 那个开关走我们的
   BYOK 地址。同事按文档去"自定义模型"里找 → 找不到 → 以为没装上。
   🔴 这张表只用于**打开开关 + 给用户指路**，不许拿它去改名（改名会改变菜单里显示的名字，
      用户点名要这几个名字）。
   ⚠️ 它是**实测产物**，不是推导：网关新上同名模型后要重新量一次
      （`cursor_localagent_doctor.js` 会把两个数组的长度和差集印出来）。 */
const BUILTIN_COLLIDING = ["gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5",
  "kimi-k2.7-code"];
// 装完直接选中它。2026-09-20 用户点名从 cr-g-5.6 改成 sa-grok-4.6-latest；
// 2026-09-22 全员切裸名后改成 grok-4.6-latest（同一个上游组，走 per-key alias）。
// 10-19 起菜单动态:它只是「Key 里有就默认选它」的偏好,Key 里没有就选菜单第一个(pickDefault)。
const DEFAULT_MODEL = "grok-4.7";
/* 运行时真正写进库的那个默认名。平时 == DEFAULT_MODEL；只有 `--bare-sa` 会把它
   改成去前缀后的裸名。**常量本身不动** —— parity 门（setup_impl_parity.py）按
   `const DEFAULT_MODEL = "…"` 正则比对 js/py 两份，改常量会把门弄红，而这个开关
   是运行时行为、不该改变两份实现的常量契约。 */
let ACTIVE_DEFAULT_MODEL = DEFAULT_MODEL;
// 判断"当前选中的是不是本方案的名字"用这个前缀。
// ⚠️ 改 DEFAULT_MODEL 时必须一起改这里：漏改的后果是老用户升级后被打回旧名
// （`--keep-model` 的默认分支会认为"当前选中的不是我们的名字"从而覆盖它）。
// ⚠️ 加新模型名时必须让它命中其中一条：漏加的后果是 `--uninstall` 摘不掉它
// （dropOurs 按 isOurs 过滤），同事以为卸干净了、库里还留着我们加的名字。
// 10-19 起菜单动态:前缀表认不出的新名字由菜单账本 MENU_LEDGER(~/.carher/cursor-menu.json)兜底。
// 2026-09-21：新清单带进 gpt-* / codex- / deepseek- / kimi- / glm- 五类前缀。
// ⚠️ `gpt-` 这条**会同时命中 Cursor 自带的 gpt-***（gpt-5.3-codex、gpt-5.4…）。
//    后果只落在 `--uninstall` 的 dropOurs 上：它按 isOurs 过滤，所以卸载时会把
//    同事自己加的 gpt-* 自定义名一起摘掉。整表赋值(EXACT)语义下装机本来就会清掉
//    那些名字，两者方向一致，不新增损失。但**别把 isOurs 当"这是我们装的"的证据**
//    用在别处 —— 它现在是个偏宽的判据。
// 2026-09-22：菜单换成裸名 ⇒ 必须加 `grok-` / `composer-` 两条，否则菜单名不命中前缀，
//    `--uninstall` 的 dropOurs 摘不掉它们（门 setup_impl_parity.py 会红）。
//    ⚠️ 这两条**也命中 Cursor 自带的 grok / composer 模型**，与 `gpt-` 同病，isOurs 因此更宽。
//    `sa-grok-` / `sa-composer-` 两条**保留**：老用户库里还躺着上一代的 sa- 名字，
//    不认它们就等于升级/卸载时摘不掉（整表赋值会换掉菜单，但 dropOurs 走的是 isOurs）。
// 2026-10-12:段 ③ 换成 claude 家族 ⇒ 必须加能命中那 5 个名的前缀,否则菜单名不命中前缀,
//    `--uninstall` 的 dropOurs 摘不掉它们(门 setup_impl_parity.py 会红)。
// 🔴 **这里故意写三条窄前缀,不写一条 `claude-`**。写宽的那一条当场被台架 ⑨ 腿判红:
//    fixture 里同事自己加的第三方名正是 `claude-opus-4.6`,`claude-` 会让 isOurs 认领它
//    ⇒ `--uninstall` 把他的名字**删掉而不是还给他**(dropOurs 在还原之后过滤)。
//    `gpt-`/`grok-` 那种宽前缀是历史遗留、后果已登记;新加的别再扩大这个面。
//    这三条恰好覆盖全部 5 个菜单名(`claude-opus-5` 也前缀命中 `claude-opus-5-5`,
//    `claude-sonnet-5` 同理),且不命中 `claude-opus-4.6` / `claude-4.6-sonnet` 这类别人的名字。
// 2026-10-12 对齐 std 时 `jev` 是个**没有分隔符的裸名**,前缀表里要单独一条
//    (`jev` 自己就是前缀,整名匹配)。少了它 parity 门会红 + `--uninstall` 摘不掉。
// 2026-10-12 晚新增那 5 个:claude 4.x 四个 + gemini-3.8-flash。**整名当前缀(最窄)**,
//    实测不命中同事自己可能有的 `claude-opus-4.6` / `claude-sonnet-4.6` / `claude-4.6-sonnet`
//    / `gemini-3.8-pro`(点号 vs 连字符正好把两类名字分开)⇒ `--uninstall` 不会误删他的东西。
//    ⚠️ `grok-4.3` / `grok-build-0.1` 已被既有的宽前缀 `grok-` 覆盖,不另加。
const MODEL_PREFIXES = ["cr-g-", "grok-", "composer-", "sa-grok-", "sa-composer-",
  "qwen3-coder-", "gpt-", "codex-", "deepseek-", "kimi-", "glm-",
  "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5", "jev",
  "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
  "gemini-3.8-flash",
  // 10-13 muse 两个:整名(最窄),同事库里不会有撞名的第三方 muse- 名
  "muse-spark", "muse-image"];
/* `--bare-sa` 往这里塞它生成的裸名（`sa-grok-4.6` → `grok-4.6`）。
   为什么不直接往 MODEL_PREFIXES 里加 `grok-` / `composer-`：那两条会**同时命中
   Cursor 自带的 grok / composer 模型**，而 isOurs 已经是个偏宽的判据（`gpt-` 那条
   同病），再宽下去 `--uninstall` 的 dropOurs 会连同事自己的名字一起摘。
   用「这一趟实际装了哪些裸名」这张精确名单，比多一条前缀通配安全。 */
const EXTRA_OURS = new Set();
const isOurs = (name) => typeof name === "string" &&
  (EXTRA_OURS.has(name) || MENU_LEDGER.has(name) || MODEL_PREFIXES.some((p) => name.startsWith(p)));

/* ── 动态菜单的三样本地状态(2026-10-19)──────────────────────────────────────────
   ① Key 副本 ~/.carher/image.json(600,{key, base}):Cursor 库里的 Key 是钥匙串加密的,
      3.24.12 起 keytar 被上游摘掉 ⇒ 脚本解不开。画图工具本来就存了这一份,菜单同步复用它。
      INSTALL 时粘了 Key 就写;UPGRADE/刷模型 从这里读(读不到再试解库里的密文)。
   ② 菜单账本 ~/.carher/cursor-menu.json:上次装进菜单的名字。动态菜单的名字不再都能被
      MODEL_PREFIXES 认出(网关明天上一个 minimax-x,前缀表里没有)⇒ --uninstall / 选中位判断
      靠「装过哪些」这张精确名单兜底,而不是靠前缀猜。
   ③ 拉不到列表(没 Key/断网/返回空)一律不改菜单 —— 空列表整表赋值 = 把人家菜单清空。 */
const CARHER_DIR = path.join(os.homedir(), ".carher");
const KEY_COPY = path.join(CARHER_DIR, "image.json");
const MENU_LEDGER_FILE = path.join(CARHER_DIR, "cursor-menu.json");
const MENU_LEDGER = new Set((() => {
  try { const j = JSON.parse(fs.readFileSync(MENU_LEDGER_FILE, "utf8")); return Array.isArray(j.models) ? j.models : []; }
  catch (e) { return []; }
})());
function writeMenuLedger(models) {
  fs.mkdirSync(CARHER_DIR, { recursive: true });
  fs.writeFileSync(MENU_LEDGER_FILE, JSON.stringify({ updatedAt: new Date().toISOString(), models }, null, 1) + "\n");
  for (const m of models) MENU_LEDGER.add(m);
}
const cleanKey = (k) => String(k || "").replace(/[\s\u00A0\u200B-\u200F\u2028-\u202F\uFEFF]/g, "");
function readKeyCopy() {
  try { const j = JSON.parse(fs.readFileSync(KEY_COPY, "utf8")); return cleanKey(j.key); } catch (e) { return ""; }
}
// 库里那条 OSCrypt 密文能解就解(mac + keytar 还在的老版本 Cursor);解不开返回 ""。
async function readKeyFromDb() {
  try {
    const db = openDb(); const k = await db.get(OPENAI_KEY_SECRET); db.close();
    if (!keyPresent(k)) return "";
    const ok = await deriveOsCryptKey(RES);
    if (!ok) return "";
    const enc = Buffer.from(JSON.parse(String(k)).data);
    return enc.slice(0, 3).toString() === "v10" ? cleanKey(oscryptDecrypt(ok, enc)) : "";
  } catch (e) { return ""; }
}
/* GET {base}/models —— 网关按这把 key 的白名单过滤后的名字。返回 {ok, ids, why}。
   用 Cursor 自带 Node 的全局 fetch(24.x 有);测试可用 CX_MODELS_JSON 注入一份响应(不出网)。 */
async function fetchKeyModels(key, base) {
  if (!key) return { ok: false, ids: [], why: "拿不到 Key" };
  let body;
  try {
    if (process.env.CX_MODELS_JSON != null) body = JSON.parse(process.env.CX_MODELS_JSON);
    else {
      const r = await fetch(String(base).replace(/\/+$/, "") + "/models",
        { headers: { Authorization: "Bearer " + key }, signal: AbortSignal.timeout(20000) });
      if (r.status === 401 || r.status === 403) return { ok: false, ids: [], why: "Key 无效或已停用(HTTP " + r.status + ")" };
      if (!r.ok) return { ok: false, ids: [], why: "网关返回 HTTP " + r.status };
      body = await r.json();
    }
  } catch (e) { return { ok: false, ids: [], why: "连不上网关(" + ((e && e.message) || e) + ")" }; }
  const ids = ((body && body.data) || []).map((x) => x && x.id).filter((x) => typeof x === "string" && x);
  if (!ids.length) return { ok: false, ids: [], why: "网关没返回任何模型" };
  return { ok: true, ids };
}
const OPENAI_KEY_SECRET = "secret://cursorAuth/openAIKey"; // Cursor 存 BYOK Key 的 secret 行(OSCrypt 密文)

/* ── bundle 补丁常量(与 Python 版逐字节一致,有等价断言测试守着) ── */
const GATE_FN = "(function(c){if(!c)return c;const o={...c};" +
  "if(o.namedModelsViewConfig){o.namedModelsViewConfig={...o.namedModelsViewConfig};" +
  "delete o.namedModelsViewConfig.namedViewToRoutedModelViewButton;" +
  "delete o.namedModelsViewConfig.namedViewToRoutedModelViewToggle;" +
  "delete o.namedModelsViewConfig.namedViewToRoutedModelViewNoButton;}" +
  "if(o.routedModelViewConfig){o.routedModelViewConfig={...o.routedModelViewConfig};" +
  "delete o.routedModelViewConfig.routedModelViewToNamedViewButton;" +
  "delete o.routedModelViewConfig.routedModelViewToNamedViewToggle;" +
  "o.routedModelViewConfig.hideRoutedModelView=false;}return o;})";

/* ── minify 标识符字符集(2026-09-15 修 3.20.21 glass gate 漂移)────────────────────
   坑:锚点里捕获 minify 出来的变量/函数名一直写的是 `\w+` = [A-Za-z0-9_],**不含 `$`**。
   JS 合法标识符首字符含 `$`,而 esbuild/terser 到了后段名字用尽就会大量吐 `$xx`:
   3.20.21 glass 里 `$` 开头的标识符有 1163 个不同名字、5186 处调用点,不是偶发。
   3.20.21 上 glass 的 gate 包装函数名从 `qUy` 变成 **`$7y`** → `\w+` 认不出 → hits=0 →
   安装器整体拒绝动手 = 一个补丁都没打(desktop 那次抽到 `l_f`,下划线在 \w 里所以躲过了)。
   所以这不是"官方重构了代码"(那段代码逐字未变),是我们的锚点字符集写窄了。
   ID 用在所有 minify 名字的位置;`ID1` 是可选参数位(有的版本参数被摇掉了)。
   放宽不等于放松安全阀:四条 bundle(3.20.17/3.20.21 × desktop/glass)命中仍全是 exactly-1,
   nopromote 的 multi 计数也不变(2/5/2/5),"命中 !=1 就拒绝"这道门原样保留。 */
const ID = "[A-Za-z0-9_$]+";
const ID1 = "[A-Za-z0-9_$]*"; // 可能为空(参数被摇掉)

const QP_MARKER = "@cx-queue-pump:v4";
const QP_ANCHOR = new RegExp("(addToQueue\\((" + ID + ")\\)\\{if\\(!this\\.isValidQueueItem\\(\\2\\)\\)return;)", "g");
// v4 修 v3.5 残留折叠(真凶):官方 3.17 在 turnEnded 事件里原生接力队列
// (removeFromQueue+appendQueuedHumanMessage+新请求),不走 dispatch、不碰
// inFlightDispatchItemIds → 泵在它起跑窗口里 heal+抢发下一条 = 一请求两问只答后一条。
// 修:泵每 tick 对比队列长度,发现被别人消费 → 让路 5 tick(不 heal 不派发);
// 官方不管的场景(真僵尸/用户停止饿死)照旧兜底。
const QP_SNIPPET =
  "/*" + QP_MARKER + "*/try{if(this._cxQP===void 0){let _cxS=0,_lq=-1,_cool=0,_pfl=!1;const _cxT=()=>{" +
  "this._cxQP=void 0;try{const _q=this.getQueueItems().length;if(_q===0)return;" +
  "const _fl=this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0;" +
  "if(_lq>=0&&_q<_lq&&!_pfl){_cool=5;_cxS=0;" +
  'try{this.structuredLogService.info("composer","[cx-queue-pump] queue consumed externally, yielding",{composerId:this.composerId,from:_lq,to:_q})}catch(_e){}}' +
  "_lq=_q;_pfl=_fl;" +
  "if(_cool>0){_cool--}else{" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  "let _go=!1;" +
  'if(_d&&_d.status==="generating"){' +
  "if(!_fl&&(_d.generatingBubbleIds??[]).length===0){" +
  "const _u=_d.chatGenerationUUID;" +
  "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;" +
  'const _stale=_u===void 0||(_m&&typeof _m.has==="function"&&!_m.has(_u));' +
  "_cxS=_stale?_cxS+1:0;" +
  "if(_cxS>=3){_cxS=0;" +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",chatGenerationUUID:void 0,generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0});_go=!0}catch(_e){}}}' +
  "else{_cxS=0}}" +
  "else{_cxS=0;_go=!_fl}" +
  "if(_go)this.tryDispatchNextQueueItem()}" +
  "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}";

// 旧版排队泵原文(逐字节),在场则原地升级 v4
const QP_OLD = [
  "/*@cx-queue-pump:v3.5*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  "const _fl=this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0;" +
  "let _go=!1;" +
  'if(_d&&_d.status==="generating"){' +
  "if(!_fl&&(_d.generatingBubbleIds??[]).length===0){" +
  "const _u=_d.chatGenerationUUID;" +
  "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;" +
  'const _stale=_u===void 0||(_m&&typeof _m.has==="function"&&!_m.has(_u));' +
  "_cxS=_stale?_cxS+1:0;" +
  "if(_cxS>=3){_cxS=0;" +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",chatGenerationUUID:void 0,generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0});_go=!0}catch(_e){}}}' +
  "else{_cxS=0}}" +
  "else{_cxS=0;_go=!_fl}" +
  "if(_go)this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
  "/*@cx-queue-pump:v3.4*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){' +
  "const _u=_d.chatGenerationUUID;" +
  "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;" +
  "const _fl=this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0;" +
  'const _stale=!_fl&&(_u===void 0||(_m&&typeof _m.has==="function"&&!_m.has(_u)));' +
  "_cxS=_stale?_cxS+1:0;" +
  "if(_cxS>=3){" +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",chatGenerationUUID:void 0,generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}' +
  "else{_cxS=0}" +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
  "/*@cx-queue-pump:v3.3*/try{if(this._cxQP===void 0){let _cxS=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){' +
  "const _u=_d.chatGenerationUUID;" +
  "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;" +
  'const _stale=_u===void 0||(_m&&typeof _m.has==="function"&&!_m.has(_u));' +
  "_cxS=_stale?_cxS+1:0;" +
  "if(_u===void 0||_cxS>=3){" +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",chatGenerationUUID:void 0,generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}' +
  "else{_cxS=0}" +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
  "/*@cx-queue-pump:v3*/try{if(this._cxQP===void 0){let _cxN=0,_cxS=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'if(_d&&_d.status==="generating"&&(_d.generatingBubbleIds??[]).length===0){' +
  "const _u=_d.chatGenerationUUID;" +
  "const _m=this.composerChatService&&this.composerChatService._aiService&&this.composerChatService._aiService.streamingAbortControllers;" +
  'const _stale=_u===void 0||(_m&&typeof _m.has==="function"&&!_m.has(_u));' +
  "_cxS=_stale?_cxS+1:0;" +
  "if(_u===void 0||_cxS>=3){" +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",chatGenerationUUID:void 0,generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId,hadUUID:_u!==void 0})}catch(_e){}}}' +
  "else{_cxS=0}" +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0&&++_cxN<120){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
  "/*@cx-queue-pump:v1*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'if(_d&&_d.status==="generating"&&_d.chatGenerationUUID===void 0&&(_d.generatingBubbleIds??[]).length===0){' +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-pump] healed stuck generating status",{composerId:this.composerId})}catch(_e){}}' +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0&&++_cxN<60){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
  "/*@cx-queue-diag:v2*/try{if(this._cxQP===void 0){let _cxN=0;const _cxT=()=>{" +
  "this._cxQP=void 0;try{if(this.getQueueItems().length===0)return;" +
  "const _h=this.getComposerHandleIfLoaded();" +
  "const _d=_h?this.composerDataService.getComposerData(_h):void 0;" +
  'try{this.structuredLogService.info("composer","[cx-queue-diag] tick",' +
  "{composerId:this.composerId,n:_cxN,queueLen:this.getQueueItems().length," +
  "status:(_d&&_d.status)||null,hasUUID:!!(_d&&_d.chatGenerationUUID)," +
  "bubbleIds:((_d&&_d.generatingBubbleIds)||[]).length})}catch(_e){}" +
  'if(_d&&_d.status==="generating"&&_d.chatGenerationUUID===void 0&&(_d.generatingBubbleIds??[]).length===0){' +
  'try{this.composerDataService.updateComposerData(_h,{status:"completed",generatingBubbleIds:[]});' +
  'this.structuredLogService.info("composer","[cx-queue-diag] healed stuck generating status",{composerId:this.composerId})}catch(_e){}}' +
  "this.tryDispatchNextQueueItem();" +
  "if(this.getQueueItems().length>0&&++_cxN<120){this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}};" +
  "this._cxQP=setTimeout(_cxT,1000)}}catch(_e){}",
];

/* ══ AST 定位层(2026-09-15)══════════════════════════════════════════════════
   为什么加这一层:正则锚点必须把 minify 出来的名字写进模式里,而那些名字每版都变
   (3.20.21 glass 的 gate 包装函数 `qUy`→`$7y`,一个字符就让安装器整体拒绝动手)。
   实测 3.16.29/3.17.19/3.18.25/3.20.21 × desktop/glass:**源码给的方法名/属性名逐字不变**,
   变的只有 minify 局部名。所以定位改成只认源码给的名字 + AST 结构,替换串从 AST
   子节点的源码切片拼出——minify 名字换成什么都与定位无关,这一整类漂移天然免疫。
   负对照:把 gate 锚点还原成出事的 `\w+`,3.20.21 glass 正则 hits=0(同事那条报错),
   AST 仍 hits=1。

   ⚠️ AST 不是免检金牌,它比正则**找得更宽**,实测两处多命中:
     · nopromote:多认非 async 的 `promoteQueueItemToSteer(t){return Promise.resolve(!1)}`
       存根 → 用 `async===true` 判别掉(判别后 5 处,与正则同数)。
     · localagent:多认日志 helper `if(kl.localMode)try{Msm(t).info(e)}` → 目标点的
       consequent 是 BlockStatement,helper 是裸 TryStatement,用这个判别掉。
   所以 applyPatchesToText 里「命中 !=1 就拒绝动手」那道安全阀原样保留,一条都不撤。

   acorn 来自 Cursor 自带 `resources/app/node_modules/acorn`(win32 安装包同路径也有),
   零新依赖。同事跑的是 Cursor 的 Electron(ELECTRON_RUN_AS_NODE),默认堆上限 4096MB,
   实测解析 glass(45MB)峰值 733MB / 6.3s —— 不需要任何 --max-old-space-size。 */
/* acorn 要从**正在跑的运行时**取,不是从"被打补丁的目标"取。真实安装里两者是同一个
   目录,但回归台架的假 app(CURSOR_APP_ROOT 指到临时目录)里没有 node_modules —— 那时
   仍应该用真 Cursor 的 acorn,否则第④腿测不到 AST 那条路。所以候选里 runtimeRoot
   (由 process.execPath 推导,不受 CURSOR_APP_ROOT 影响)排在 RES 前面。 */
function runtimeRoot() {
  const d = path.dirname(process.execPath);
  return process.platform === "darwin"
    ? path.join(d, "..", "Resources", "app")
    : path.join(d, "resources", "app");
}
function loadAcorn() {
  const cands = [
    path.join(runtimeRoot(), "node_modules", "acorn"),
    path.join(RES, "node_modules", "acorn"),
    "acorn",
  ];
  for (const p of cands) {
    try { return require(p); } catch (e) { /* 下一个候选 */ }
  }
  return null;
}

// 不认节点类型的通用遍历(acorn-walk 没随 Cursor 发)。
function astWalk(node, cb) {
  const st = [node];
  while (st.length) {
    const n = st.pop();
    if (!n || typeof n !== "object") continue;
    if (Array.isArray(n)) { for (const c of n) st.push(c); continue; }
    if (typeof n.type === "string") cb(n);
    for (const k in n) {
      if (k === "type" || k === "start" || k === "end") continue;
      const v = n[k];
      if (v && typeof v === "object") st.push(v);
    }
  }
}

const isMethod = (n, name) => n.type === "MethodDefinition" && n.key && n.key.name === name;

/* 每个 locator 返回 [{start, end, make(src)}]:把 [start,end) 换成 make(src) 的结果。
   make 只用 src.slice(子节点区间) 拼——不写死任何 minify 名字。 */
const LOCATORS = {
  // gate: getModelPickerDisplayConfiguration(){…return F(x)} → 用 GATE_FN 包住 return 的实参
  gate(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (!isMethod(n, "getModelPickerDisplayConfiguration")) return;
      const b = n.value.body.body, ret = b[b.length - 1];
      if (!ret || ret.type !== "ReturnStatement") return;
      const a = ret.argument;
      if (!a || a.type !== "CallExpression") return;
      out.push({ start: a.start, end: a.end,
        make: () => "/*@cxteam-gate*/" + GATE_FN + "(" + src.slice(a.start, a.end) + ")" });
    });
    return out;
  },

  // localagent: if(X.localMode){try{…}} → 条件恒真。
  // 判别:consequent 必须是 BlockStatement(日志 helper 那处是裸 TryStatement)。
  localagent(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (n.type !== "IfStatement") return;
      // 3.27.12 起条件变成逗号表达式 `if(X.releaseAfterSend(u.conversationId),mc.localMode){try{`
      // ⇒ 只换最后那一项(前面的 releaseAfterSend 调用必须照常执行),替换范围还是那个成员表达式。
      const t = n.test.type === "SequenceExpression" ? n.test.expressions[n.test.expressions.length - 1] : n.test;
      if (!(t.type === "MemberExpression" && t.property && t.property.name === "localMode")) return;
      if (n.consequent.type !== "BlockStatement") return;
      const first = n.consequent.body[0];
      if (!first || first.type !== "TryStatement") return;
      out.push({ start: t.start, end: t.end, make: () => "/*@cxteam-localagent*/!0" });
    });
    return out;
  },

  // dedicated: X(this.storageService,"useDedicatedLocalAgentRuntimeHost")?await this.runLocalAgentInDedicatedExtensionHost(…)
  dedicated(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (n.type !== "ConditionalExpression") return;
      const t = n.test;
      if (t.type !== "CallExpression") return;
      if (!t.arguments.some((a) => a.type === "Literal" && a.value === "useDedicatedLocalAgentRuntimeHost")) return;
      const c = n.consequent;
      const call = c.type === "AwaitExpression" ? c.argument : c;
      if (!call || call.type !== "CallExpression" || call.callee.type !== "MemberExpression") return;
      if (call.callee.property.name !== "runLocalAgentInDedicatedExtensionHost") return;
      out.push({ start: t.start, end: t.end, make: () => "(/*@cxteam-dedicated*/!1)" });
    });
    return out;
  },

  // noagenthost: `o=e.checkFeatureGate("cursor_agent_host",{…})` ← 右值前面插 `!1&&`
  // 🔴 必须有 AST locator:没有的话这条退回正则、在 AST 区间落地前先改了 out ⇒ 后面
  //    nosteer-mod 等按原偏移落地时错位(10-13 台架 ③④ 当场抓到:js/py 不等 + 重跑不幂等)。
  noagenthost(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (n.type !== "AssignmentExpression" || n.operator !== "=") return;
      const c = n.right;
      if (!c || c.type !== "CallExpression" || c.callee.type !== "MemberExpression") return;
      if (c.callee.property.name !== "checkFeatureGate") return;
      if (!(c.arguments[0] && c.arguments[0].type === "Literal" && c.arguments[0].value === "cursor_agent_host")) return;
      out.push({ start: c.start, end: c.start, make: () => "/*@cxteam-noagenthost*/!1&&" });
    });
    return out;
  },

  // queue-pump: addToQueue(x){if(!this.isValidQueueItem(x))return; ← 在这句之后插泵
  "queue-pump"(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (!isMethod(n, "addToQueue")) return;
      const first = n.value.body.body[0];
      if (!first || first.type !== "IfStatement") return;
      const t = first.test;
      if (!(t.type === "UnaryExpression" && t.operator === "!" && t.argument.type === "CallExpression" &&
            t.argument.callee.type === "MemberExpression" &&
            t.argument.callee.property.name === "isValidQueueItem")) return;
      if (first.consequent.type !== "ReturnStatement") return;
      out.push({ start: first.end, end: first.end, make: () => QP_SNIPPET });
    });
    return out;
  },

  // qserial: tryDispatchNextQueueItem(){const h=this.getComposerHandleIfLoaded();if(!h)return; ← 之后插在飞守卫
  qserial(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (!isMethod(n, "tryDispatchNextQueueItem")) return;
      const b = n.value.body.body;
      const d = b[0], g = b[1];
      if (!d || d.type !== "VariableDeclaration" || d.declarations.length !== 1) return;
      const init = d.declarations[0].init;
      if (!init || init.type !== "CallExpression" || init.callee.type !== "MemberExpression" ||
          init.callee.property.name !== "getComposerHandleIfLoaded") return;
      if (!g || g.type !== "IfStatement" || g.consequent.type !== "ReturnStatement") return;
      out.push({ start: g.end, end: g.end,
        make: () => "/*@cxteam-qserial*/if(this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0)return;" });
    });
    return out;
  },

  // nosteer-mod: case"send":case"queue": 连写的那条 return → 行为恒为 queue。
  // 两代形状:①ConditionalExpression `t&&F(t)?{behavior:"steer"…}:{…}` ②ObjectExpression
  // `{behavior:n?e==="steer"?"steer":"stop-and-send":"queue",isAlternate:n}`。
  // 判别靠 case 连写 + 返回值里带 behavior 属性,不靠任何 minify 名。
  "nosteer-mod"(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (n.type !== "SwitchStatement") return;
      for (let i = 0; i + 1 < n.cases.length; i++) {
        const a = n.cases[i], b = n.cases[i + 1];
        if (!(a.test && a.test.type === "Literal" && a.test.value === "send")) continue;
        if (a.consequent.length !== 0) continue; // "send" 必须是空 case(与 queue 连写)
        if (!(b.test && b.test.type === "Literal" && b.test.value === "queue")) continue;
        const ret = b.consequent[0];
        if (!ret || ret.type !== "ReturnStatement" || !ret.argument) continue;
        const arg = ret.argument;
        // 形①:三元,consequent 是带 behavior:"steer" 的对象 → 只把那个 "steer" 换成 "queue"
        if (arg.type === "ConditionalExpression" && arg.consequent.type === "ObjectExpression") {
          const p = arg.consequent.properties.find((x) => x.key && x.key.name === "behavior");
          if (!p || p.value.type !== "Literal" || p.value.value !== "steer") continue;
          out.push({ start: p.value.start, end: arg.consequent.end,
            make: () => '"queue"' + src.slice(p.value.end, arg.consequent.end) + "/*@cxteam-nosteermod*/" });
          continue;
        }
        // 形②:对象,behavior 是三元 → 整个换成字面 "queue"
        if (arg.type === "ObjectExpression") {
          const p = arg.properties.find((x) => x.key && x.key.name === "behavior");
          if (!p || p.value.type !== "ConditionalExpression") continue;
          out.push({ start: p.value.start, end: p.value.end,
            make: () => '/*@cxteam-nosteermod*/"queue"' });
          continue;
        }
      }
    });
    return out;
  },

  // nopromote: async promoteQueueItemToSteer(…){ ← 函数体开头 return!1。
  // 判别 async===true:非 async 那处是 unsupported 存根(本来就返回 false)。multi。
  nopromote(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (!isMethod(n, "promoteQueueItemToSteer")) return;
      if (n.value.async !== true) return;
      const body = n.value.body;
      if (!body || body.type !== "BlockStatement") return;
      out.push({ start: body.start + 1, end: body.start + 1,
        make: () => "/*@cxteam-nopromote*/return!1;" });
    });
    return out;
  },

  // norelay: if(F({…isNewRequestIdGateEnabled:()=>this.isQueuedPromptNewRequestIdEnabled()})) → 恒 false。
  // 认对象字面量的属性名集合(全是源码给的);agentBackend 3.18 起没了,不做要求。
  norelay(ast, src) {
    const out = [];
    astWalk(ast, (n) => {
      if (n.type !== "IfStatement") return;
      const t = n.test;
      if (t.type !== "CallExpression" || t.arguments.length !== 1) return;
      const a = t.arguments[0];
      if (!a || a.type !== "ObjectExpression") return;
      const keys = a.properties.map((p) => p.key && (p.key.name || p.key.value));
      for (const k of ["isLocalMode", "isAgentHostEnabled", "isNewRequestIdGateEnabled"]) {
        if (!keys.includes(k)) return;
      }
      out.push({ start: t.start, end: t.end,
        make: () => "/*@cxteam-norelay*/!1&&" + src.slice(t.start, t.end) });
    });
    return out;
  },
};

// keepmine-fill 插入的那段(变量名 v 是目录数组;只用 __cx 前缀的局部名,不碰原代码的 minify 名)
/* 档位模板(2026-10-16):本机目录里我们 21 个带档位的名字的 parameterDefinitions/variants 等原样快照,
   见 cursor_tier_catalog.json。KEEPFILL 把它当字面量塞进 bundle;JSON 是合法 JS 表达式,里面没有 *\/ 和行分隔符。 */
const TIER_CATALOG_JSON = (() => { try { return fs.readFileSync(path.join(__dirname, "cursor_tier_catalog.json"), "utf8").trim(); }
  catch (e) { return "{}"; } })();
// v1 整段(变量名在第 1 组),只用于识别老机器上已打的 v1 并原地换成 v2
const KEEPFILL_V1_RX = /\/\*@cxteam-keepfill\*\/\(\(\)=>\{try\{const __cxs=.*?__cxh=new Set\(([A-Za-z_$][A-Za-z0-9_$]*)\.map\(.*?\}catch\(__cxe\)\{\}\}\)\(\),/g;
const KEEPFILL = (v) => '/*@cxteam-keepfill2*/(()=>{try{const __cxs=this._reactiveStorageService.applicationUserPersistentStorage,__cxu=(__cxs.aiSettings&&__cxs.aiSettings.userAddedModels)||[],__cxt=globalThis.__cxTiers||(globalThis.__cxTiers=JSON.parse(@@TIERS@@)),__cxh=new Map(@@V@@.map((__cxm,__cxi)=>[__cxm.name,__cxi]));for(const __cxn of __cxu){if(typeof __cxn!=="string"||!__cxn)continue;const __cxk=__cxt[__cxn];if(__cxh.has(__cxn)){const __cxi=__cxh.get(__cxn),__cxo=@@V@@[__cxi];if(__cxk&&!(__cxo.variants&&__cxo.variants.length))@@V@@[__cxi]={...__cxo,...__cxk,name:__cxn,isUserAdded:!0};continue}__cxh.set(__cxn,@@V@@.length);@@V@@.push({name:__cxn,defaultOn:!1,supportsAgent:!0,degradationStatus:0,supportsThinking:!0,supportsImages:!0,supportsMaxMode:!0,supportsNonMaxMode:!0,serverModelName:__cxn,isRecommendedForBackgroundComposer:!1,supportsPlanMode:!0,supportsSandboxing:!0,inputboxShortModelName:__cxn,parameterDefinitions:[],variants:[],legacySlugs:[],idAliases:[],namedModelSectionIndex:1,cloudAgentEffortModes:[],modelPickerBadges:[],...(__cxk||{}),isUserAdded:!0})}}catch(__cxe){}})(),'.split("@@V@@").join(v).split("@@TIERS@@").join(JSON.stringify(TIER_CATALOG_JSON));

const PATCHES = [
  {
    name: "gate", marker: "@cxteam-gate",
    // 3.20.21 glass 的包装函数名是 `$7y`(见 ID 注释)→ 名字位置必须用 ID 不能用 \w+。
    // 参数位也放宽:旧写法钉死小写单字母 `[a-z]`,minify 同样可能给出 `$e`/`t2`。
    rx: new RegExp("(modelPickerDisplayConfiguration\\?\\?" + ID + ";return )(" + ID + "\\(" + ID + "\\))(\\}resolveModelNameToCatalog)", "g"),
    sub: (m) => m[1] + "/*@cxteam-gate*/" + GATE_FN + "(" + m[2] + ")" + m[3],
  },
  {
    name: "localagent", marker: "@cxteam-localagent",
    // 3.16/3.17: `clientSupportsRoutedModelUpdate:!0};if(x.localMode){try{`
    // 3.18.25:   `…localMode:vl.localMode});if(vl.localMode){try{h.onNetworkPhaseStart`(同一个 run(),只是前缀变了)
    // 3.24.12:   `…enableSmokeTestDriver===!0});if(Cc.localMode){try{`(desktop;glass 是 `kl`)
    //   —— 这是**真的形状变了**:那个对象字面量的最后一个字段第三次换人,不是 minify 名字被摇,
    //   所以 ID 捕获救不了,必须按 SOP 第 3 步补一代前缀。锚的落点 `if(<ID>.localMode){try{` 三代未变。
    //   ⚠️ 这条正则是 **py 那条路 + bundle_anchor_probe.js** 的量具(探针读的是本文件的 PATCHES,
    //   不是 py 的)⇒ 漏改这里,探针会在 AST 已经打上的版本上报 hits=0,看着像没适配。
    // 三代都认;命中仍必须恰好 1 次,多了照样拒。
    // 3.27.12: `…enableSmokeTestDriver===!0});if(aGo.releaseAfterSend(u.conversationId),mc.localMode){try{` —— 条件前多了
    //   一个逗号项;它必须原样保留(副作用调用),所以并进第 1 组,只换 `<ID>.localMode`。
    rx: new RegExp("((?:clientSupportsRoutedModelUpdate:!0\\}|localMode:" + ID
      + "\\.localMode\\}\\)|enableSmokeTestDriver===!0\\}\\));if\\((?:" + ID + "\\.releaseAfterSend\\(" + ID + "\\.conversationId\\),)?)("
      + ID + "\\.localMode)(\\)\\{try\\{)", "g"),
    sub: (m) => m[1] + "/*@cxteam-localagent*/!0" + m[3],
  },
  {
    name: "dedicated", marker: "@cxteam-dedicated",
    rx: new RegExp("(" + ID + "\\(this\\.storageService,\"useDedicatedLocalAgentRuntimeHost\"\\))(\\?await this\\.runLocalAgentInDedicatedExtensionHost\\()", "g"),
    sub: (m) => "(/*@cxteam-dedicated*/!1)" + m[2],
  },
  /* ── noagenthost(2026-10-13):服务端灰度 `cursor_agent_host` 一开,BYOK 全报
     「This model does not support custom API keys」──────────────────────────────
     假设:请求没走本机 BYOK 线(agentExecProviderService.runLocalAgent → 小代理),而是被
           Agent Host 拓扑送去了 Cursor 服务端,服务端不认自定义 key。
     证伪条件:若假设错,成功与失败的会话应走同一条路。
     数据(本机 3.24.12,同一台、同一 key、同一 grok-4.7):
       10-12 21:43 会话 `cursor_agent_host gate is disabled … topology is disabled`
         + `[AgentHostService] using legacy implementation` ⇒ `[local_agent_retries] Request successful`;
       10-13 16:55 会话 `gate is enabled … topology is enabled` ⇒ agent-host 扩展日志
         `Selected Agent Host turn runtime {"runtime":"connect",…}` ⇒ ConnectError invalid_argument
         = 「does not support custom API keys」,小代理零请求。gate 由服务端按账号/时间下发,
         所以同一台机器时好时坏、同事之间也不一样。
     ⇒ 件D noloop 只把 agent host 里的 local-loop 改成 connect,**拦不住 agent host 本身**;
       connect 运行时是服务端推理,BYOK 在那条路上根本不存在。
     修:enablement 服务里读 gate 那一处恒 `!1` ⇒ topology 恒 disabled ⇒ 走 legacy 实现
       (即 localagent/dedicated 补丁兜住的本机 BYOK 线)。全 app 只有 desktop/glass 两处读这个
       gate(各 exactly-1),扩展里 0 处。private inference 那条腿(`||s`)不动。 */
  {
    name: "noagenthost", marker: "@cxteam-noagenthost",
    rx: new RegExp("(try\\{" + ID + "=)(" + ID + "\\.checkFeatureGate\\(\"cursor_agent_host\",\\{disableExposureLog:" + ID + "\\}\\))", "g"),
    sub: (m) => m[1] + "/*@cxteam-noagenthost*/!1&&" + m[2],
  },
  { name: "queue-pump", marker: QP_MARKER, rx: QP_ANCHOR, sub: (m) => m[1] + QP_SNIPPET },
  // qserial(2026-08-25):官方 tryDispatchNextQueueItem 闸门只看 status,不看"在飞派发"。
  // 实测竞态:heal 写 status 触发响应式监听,双派发 30ms 内齐发,后枪掐死前枪预网络轮 →
  // 两问挤一轮。修:入口加官方自家 inFlightDispatchItemIds 守卫,派发严格串行。
  {
    name: "qserial", marker: "@cxteam-qserial",
    rx: new RegExp("(tryDispatchNextQueueItem\\(\\)\\{const (" + ID + ")=this\\.getComposerHandleIfLoaded\\(\\);if\\(!\\2\\)return;)", "g"),
    sub: (m) => m[1] + "/*@cxteam-qserial*/if(this.inFlightDispatchItemIds&&this.inFlightDispatchItemIds.size>0)return;",
  },
  // nosteer-mod(2026-08-25 真凶):官方把「配置=queue 但按修饰键发送」设计为强制 steer
  // (⌘+回车正是修饰键!)→ 生成中发的每条都被注入当前轮 → 两问挤一轮/前问无答。
  // 3.19.19 起该 switch 从 composer 类方法搬进纯函数 Meo({newMessageBehavior,manualSendBehavior,isAlternate}),
  // 且 send/queue 分支的修饰键结果换成 `n?t==="steer"?"steer":"stop-and-send":"queue"`
  // (新增 manualSendBehavior 维度;非 steer 时落 stop-and-send=掐断当轮立即发,对本线同样是坏结果)。
  // 两代都认,命中仍必须恰好 1 次;两代改法都收敛到「修饰键照常发送、行为=queue」。
  {
    name: "nosteer-mod", marker: "@cxteam-nosteermod",
    rx: new RegExp("case\"send\":case\"queue\":return(?: (" + ID + "&&" + ID + "\\(" + ID + "\\)\\?\\{behavior:\")steer(\",isModifierOverride:!0\\})|\\{behavior:(" + ID + ")\\?" + ID + "===\"steer\"\\?\"steer\":\"stop-and-send\":\"queue\",isAlternate:\\3\\})", "g"),
    sub: (m) => m[1] !== undefined
      ? 'case"send":case"queue":return ' + m[1] + "queue" + m[2] + "/*@cxteam-nosteermod*/"
      : 'case"send":case"queue":return{behavior:/*@cxteam-nosteermod*/"queue",isAlternate:' + m[3] + "}",
  },
  // nopromote(2026-08-25 终极 steer 封口):promoteQueueItemToSteer=所有 steer 注入总入口,
  // 短路后排队消息全部老实排队。multi:glass 打包多份 composer,全部命中逐处打。
  {
    name: "nopromote", marker: "@cxteam-nopromote", multi: true,
    // 参数位用 ID1(可为空):将来参数被摇掉成 `promoteQueueItemToSteer(){` 也认得出。
    rx: new RegExp("(async promoteQueueItemToSteer\\(" + ID1 + "\\)\\{)", "g"),
    sub: (m) => m[1] + "/*@cxteam-nopromote*/return!1;",
  },
  // norelay(2026-08-25 真·根因):官方 turnEnded 的「轮内接力」在 BYOK 本地线必死
  // (agent 循环轮末已退,接力消息成孤儿,status 复位被 break 跳过=僵尸;孤儿被下一请求
  // prepend 捎走=一请求两问只答后一条)。能力闸门 k_d 在本线误判为真 → 调用点恒 false,
  // turnEnded 走复位分支,官方队列机制逐条各自成轮。僵尸+折叠同根拔除。
  {
    name: "norelay", marker: "@cxteam-norelay",
    // 3.18.25 起参数表没有 agentBackend 了,设成可选;其余逐字不变。
    rx: new RegExp("(if\\()(" + ID + "\\(\\{(?:agentBackend:" + ID + ",)?isLocalMode:" + ID + "\\.localMode,isAgentHostEnabled:" + ID + ",isNewRequestIdGateEnabled:\\(\\)=>this\\.isQueuedPromptNewRequestIdEnabled\\(\\)\\}\\))(\\)\\{)", "g"),
    sub: (m) => m[1] + "/*@cxteam-norelay*/!1&&" + m[2] + m[3],
  },
  // ── nocloud(2026-09-23):把会话钉死在本地线,不许被"送去 Cloud Agents" ──
  // 病:同事升到 3.21.18 后每次发消息弹
  //   「Upgrade to run Cloud Agents / Cloud Agents are not available on your current plan」
  // ⇒ 请求根本没走本地 BYOK,而是被送去 Cursor 云端跑,被套餐闸门拒 ⇒ 我们的模型一个都用不了。
  //
  // 三段式:
  //   假设:该会话的 `pendingBackgroundAgent` 被置 true,提交岔路口因此改道云端。
  //   证伪条件:若改道与该标志无关,岔路口条件里不应出现它。
  //   数据:唯一岔路口 `shouldRunOnBeforeSubmitChat(){…return e!==void 0&&Oke(e)||!!e?.pendingBackgroundAgent}`
  //         为真 ⇒ 走 `onBeforeSubmitChat` ⇒ `addAsyncFollowupBackgroundComposer`(云端 RPC)。
  //         那句报错客户端只在埋点翻译函数里出现
  //         (`case"Upgrade to run Cloud Agents":return"cloud_agent_pro_trial_denied"`),
  //         正文全 app 0 命中 ⇒ 文案来自服务端 CUSTOM_MESSAGE,**但改道与否在客户端**。
  //   置位来源有二:输入框右侧 Send-to-Cloud 手滑点中;或服务端把 `push_local_agent_to_cloud` /
  //   `send_to_cloud_on_followup`(都是 `client:!0,default:!1` 的客户端闸门)给这个号打开。
  //   ⚠️ 该标志**持久化在会话数据里**,升级/重启都不清 ⇒ 中招后每轮都弹,自己好不了。
  //
  // 修两处,缺一不可:
  //   nocloud-submit:岔路口摘掉 pendingBackgroundAgent 这条腿 —— 已中招的老会话立刻回本地线。
  //                   `Oke(e)` 那条腿**保留**:那是"这本来就是个云端 agent 会话"(createdFromBackgroundAgent),
  //                   用户主动在云端开的会话仍按原样走云端,我们只拦"本地会话被改道"。
  //   nocloud-set:   置位入口恒 false —— 从源头堵住,含误点与服务端闸门两条来路。
  // 名字位(参数名/局部变量名)实测 desktop/glass 全不同(e/t、Os/ji、Hs/vr)⇒ 一律 ID 捕获 + 反向引用。
  {
    name: "nocloud-submit", marker: "@cxteam-nocloud",
    rx: new RegExp("(shouldRunOnBeforeSubmitChat\\(\\)\\{const (" + ID + ")=this\\.composerDataService\\.getComposerData\\(this\\.getComposerHandle\\(\\)\\);return \\2!==void 0&&" + ID + "\\(\\2\\))\\|\\|!!\\2\\?\\.pendingBackgroundAgent(\\})", "g"),
    sub: (m) => m[1] + "/*@cxteam-nocloud*/" + m[3],
  },
  {
    // ⚠️ 09-23 自查覆盖面时发现:写入点**不止一个**。除了输入框的 Send-to-Cloud
    //   (`updateComposerData(Os,{pendingBackgroundAgent:ji})`),ComposerPlanService 里还有
    //   `updateComposerData(e,{unifiedMode:L,pendingBackgroundAgent:x})` —— 前面多带一个字段,
    //   原来那条"`{` 紧跟字段名"的锚点匹配不到它 ⇒ 漏堵。所以放开成「对象里任意位置」并改成 multi。
    //   判据:每条 bundle 恰好 2 处(不是 2 就该回来看是不是又多了写入点)。
    //   `:!1}` 那三处是**取消/失败时的复位**(本来就写 false),`ID` 不匹配 `!1` ⇒ 不会误伤;
    //   `pendingBackgroundAgent:s.pendingBackgroundAgent===!0` 是序列化拷贝,不是 updateComposerData。
    //   真正兜住路由的是 nocloud-submit(读侧,与谁写的无关);这条是源头补强,两条都要有。
    name: "nocloud-set", marker: "@cxteam-nocloudset", multi: true,
    rx: new RegExp("(updateComposerData\\(" + ID + ",\\{[^{}]{0,160}?pendingBackgroundAgent:)(" + ID + ")(\\}\\))", "g"),
    sub: (m) => m[1] + "/*@cxteam-nocloudset*/!1" + m[3],
  },

  /* ── keepmine(2026-09-23):不许 Cursor 把我们装的同名模型从清单里吞掉 ──────────────
     病:同事选 `Grok 4.7`(芯片上还带 High/Fast 档位)发消息,服务端回
       「This model does not support custom API keys」—— 请求压根没到我们的 LiteLLM。

     三段式:
       假设:安装器写进 userAddedModels 的名字,若与服务端目录里 Cursor 自家的同名条目撞上,
             会被客户端**删掉**,于是选中的是 Cursor 自家那条 ⇒ 不走 BYOK。
       证伪条件:若假设错,用户机器上 userAddedModels 应仍是安装器写的全部 31 个,
             且撞名的那几个在目录里应带 isUserAdded。
       数据(本机 3.20.17,读 state.vscdb 的 reactiveStorage.persistentStorage.applicationUser):
             userAddedModels **只剩 24 个**,少的正是
             grok-4.7 / grok-4.6 / grok-4.5 / gpt-5.6-{sol,luna,terra} / gpt-5.5 / kimi-k2.7-code
             —— 一个不多一个不少,全是撞名的那批;活下来的 23 个在目录里都带 `isUserAdded:true`,
             而 `grok-4.7` 那条**没有** isUserAdded、却有 parameterDefinitions
             (context 256k/500k + reasoning_effort low/medium/high/xhigh)
             = 同事截图里 `Grok 4.7 High Fast` 那两个芯片的来源。
       对照组(没坏的那批):23 个带 isUserAdded 的裸名一直好用 ⇒ 不是"裸名"的错,是**撞名**的错。

     机制(desktop 3.20.17 的名字,四条 bundle 形状一致):
       Yb_(e,t){return e.name===t||e.clientDisplayName===t||e.serverModelName===t}
       Xb_(e,t){return e.filter(n=>t.some(i=>Yb_(i,n)&&!i.isUserAdded))}   // 要删谁
       → Zb_() 把它当 userAddedModelsToRemove 返回 → refreshDefaultModels 里
         `k.length>0&&set…("aiSettings","userAddedModels",t.filter(V=>!k.includes(V)))`
         **真写回持久化存储** ⇒ 删一次就永久没了,重装 Cursor 也不回来,用户自己好不了。
       ⇒ 同族于 nocloud:判定在服务端(目录谁发的),但**落地动作在客户端**,所以我们能拦。

     修两处,缺一不可:
       keepmine-noevict:驱逐名单恒空 —— 名字不再被从 userAddedModels 里抹掉。
       keepmine-claim:  目录落盘前,凡名字在 userAddedModels 里的条目一律改成
                        `isUserAdded:!0` 并清掉 parameterDefinitions/variants,
                        与那 23 个活着的条目**逐字段同形** ⇒ 走 BYOK 到我们的 base url。
     ⚠️ 只动"名字在我们自己 userAddedModels 里"的条目:用户没装的 Cursor 自带模型零影响。
     名字位(函数名/参数名)desktop/glass/两代全不同(Xb_/h5k/SC_/Vz0)⇒ 一律 ID 捕获 + 反向引用。
     两代四条 bundle 实测两个锚点均 exactly-1。 */
  {
    name: "keepmine-noevict", marker: "@cxteam-keepmine",
    rx: new RegExp("function (" + ID + ")\\((" + ID + "),(" + ID + ")\\)\\{return \\2\\.filter\\((" + ID + ")=>\\3\\.some\\((" + ID + ")=>" + ID + "\\(\\5,\\4\\)&&!\\5\\.isUserAdded\\)\\)\\}", "g"),
    sub: (m) => "function " + m[1] + "(" + m[2] + "," + m[3] + "){/*@cxteam-keepmine*/return[]}",
  },
  {
    // `const k=h(c);c=c.map(V=>ipn(V))` —— ipn 只是 protobuf message → 普通对象,
    // 这是目录写进 availableDefaultModels2 之前**最后一个能动的点**(下面紧跟两处 set…)。
    // `this` 在此是 refreshDefaultModels 的方法 this(紧随其后的 Jh(()=>{…this._reactiveStorageService…})
    // 同一 this)⇒ 直接从 reactiveStorage 读 userAddedModels,不依赖上文局部变量名。
    /* 🔴 2026-10-12 修:原来这里连 `parameterDefinitions:[],variants:[]` 一起清掉,
       **把档位选择器(High / Fast / Medium 那两个芯片)一并剥没了**。
       现象只在**全新装机的人**身上出现,所以一直没被发现:
         · 老用户(如本机)装机**前** Cursor 早已把撞名的 9 个名字认领进自带目录
           (不在 userAddedModels)⇒ 本补丁条件不成立、碰不到它们 ⇒ 档位一直好着;
         · 全新机器装完 `userAddedModels` 是**30 条全在**(老用户只有 14)⇒ 本补丁全部接管
           ⇒ 干净 user 目录实测 **9/9 撞名名字的 variants 从 16/8/6/2/24/15/20/32/1 全被清成 0**,
           而且条目变成"自定义"后在 Settings 里也不能编辑。
       **清它的理由(注释原话"与活着的条目逐字段同形 ⇒ 走 BYOK")已被同一份文件证伪**:
       走不走 BYOK 只看「模型名前缀 + useOpenAIKey」(`bOd`),**不看 isUserAdded**,
       而 `byok-force` 已经让"名字在 userAddedModels 里就恒走 openai"。
       ⇒ 认领(isUserAdded:!0,防被驱逐)是必要的,**剥档位不是**。两个字段都保留。
       ⚠️ 别再"顺手"把它们清回去:档位是服务端按名字下发的数据,清掉用户自己救不回来。 */
    name: "keepmine-claim", marker: "@cxteam-keepmine2",
    rx: new RegExp("(const " + ID + "=" + ID + "\\((" + ID + ")\\);\\2=\\2\\.map\\((" + ID + ")=>" + ID + "\\(\\3\\)\\))", "g"),
    sub: (m) => m[1] + "/*@cxteam-keepmine2*/.map(" + m[3] + "=>{try{const __cxu=this._reactiveStorageService.applicationUserPersistentStorage.aiSettings.userAddedModels||[];return __cxu.includes(" + m[3] + ".name)&&" + m[3] + ".isUserAdded!==!0?{..." + m[3] + ",isUserAdded:!0}:" + m[3] + "}catch(__cxe){return " + m[3] + "}})",
  },

  /* ── keepmine-fill(2026-10-16):服务端目录**没给**我们的名字 ⇒ 菜单里根本没有它 ──────
     现场:同事 liangjingyu(Win,3.24.12,补丁全在,key 有全部 opus)菜单里没有任何 claude/gpt。
     他的 doctor:目录 24 条,我们 39 个名字里 20 个不在目录(全部 gpt 系、claude 系和 gemini-3.8-flash)。
     同一办公网的本机 + 同版本:目录 64 条全在;直接调服务端 AvailableModels(带 additional_model_names)
     本机也是 39/39 ⇒ 不是网络/地区,是**服务端按账号**下发的目录里少了这些条目。
     菜单只画目录里有的条目(computeAvailableDefaultModels ← availableDefaultModels2),
     keepmine-claim 只能认领"目录里已有"的条目,补不出缺的 ⇒ 名字在 userAddedModels 里也看不见。
     修:目录落盘前,把 userAddedModels 里目录缺的名字**按自定义条目的形状合成**塞回去。
       形状逐字段取自本机目录里活着的自定义条目(gpt-6-sol 等 21 字段,isUserAdded:!0、variants 空)。
       走不走我们的 key 只看名字前缀 + useOpenAIKey(byok-force),与条目是谁合成的无关。
     ⚠️ 只补"我们 userAddedModels 里有、目录里没有"的名字;目录里已有的一律不碰(档位等服务端数据保留)。 */
  {
    name: "keepmine-fill", marker: "@cxteam-keepfill2",
    rx: new RegExp("(\\{)(this\\.persistAvailableDefaultModels\\((" + ID + ")\\),this\\._reactiveStorageService\\.setApplicationUserPersistentStorage\\(\"modelPickerDisplayConfiguration\")", "g"),
    sub: (m) => m[1] + KEEPFILL(m[3]) + m[2],
  },

  /* ── byok(2026-09-23):Free 档把 BYOK 开关关掉 ⇒ 我们的名字改道 Cursor 服务端 ─────────
     同事 win32/3.21.18 发消息弹「Named modes unavailable / Free plans can only use Auto」。

     **这与 keepmine 是两个病,别混。** keepmine 管"名字还在不在菜单里";这一条管"请求发给谁"。

     假设:请求没走 BYOK,而是发到了 Cursor 服务端,被 Free 档的 named-model 闸门拒。
     证伪条件:若假设错,那句文案应该能在客户端 bundle 里 grep 到(纯客户端拦截)。
     数据:`Named mode` / `can only use Auto` / `upgrade plans to continue` 在 3.21.18
       desktop+glass 两份 bundle 里均 **0 命中**;按钮由
       `hIv(e,t,n,i){…const r=n?.details?.buttons??[]…case"switchModel"…}` 从**服务端**
       error details 构造 ⇒ 请求确实到了 Cursor 服务端。

     那么是什么决定走不走 BYOK?全 bundle 唯一判定:
       `bOd(e,t){return B7f(e)?t.useClaudeKey?…:U7f(e)?t.useGoogleKey?…:t.useOpenAIKey?"openai":void 0}`
       `Qoo=bOd!==void 0` → `m7t=vOd||Qoo` → 提交处 `isByok:m7t(i,…applicationUserPersistentStorage)`
     🔴 **只看模型名前缀 + `useOpenAIKey` 这个开关,不看 `isUserAdded`。**
     ⇒ 这也**证伪了**"keepmine 能顺手修好这个"——名字被吞掉只影响菜单,不影响路由。

     安装器装机时写的是 `d.useOpenAIKey = true`(mergeConfig)。所以开关是后来被客户端翻掉的,
     全 bundle 只有两处会翻成 false,两处两代四 bundle 均 exactly-1:
       A `fOd({isLocalMode:…})&&g===dr.FREE&&m!==dr.FREE&&this.setUseOpenAIKey(!1)` ← 订阅降到 Free
       B admin-policy `byokDisabled=true` 那支(还顺手 removeModel/removeUserAddedModel)
     ⚠️ **他那台机器上 `useOpenAIKey` 的实际值我没有,这一格是空的。** 所以下面第一条不去猜
     开关状态,而是**让开关管不着我们自己装的名字**:名字在 userAddedModels 里就恒走 openai。

     byok-force:  `bOd` 开头加一句 —— 名字是我们装进去的 ⇒ 直接返回 "openai"。
                  我们菜单里 32 个名字没有 claude-/gemini- 前缀,全部经 LiteLLM 走 OpenAI 兼容口,
                  所以恒 "openai" 是对的。⚠️ 只认 userAddedModels 里的名字,Cursor 自带模型零影响。
     byok-keepon: A 那条腿摘掉 —— 降到 Free 不再把开关关掉(否则设置页显示和实际路由不一致)。
     B 留着不动:它挂在 admin-policy `byokDisabled` 上,个人号不走这条;而 force 那条已经覆盖路由。
     ⚠️ 还空着的一格:`--repair` 按设计**不碰配置/Key**,所以开关被翻掉过的机器光跑 REPAIR
       补不回来 —— 要跑一次 `--apply`/`--upgrade` 才会重写 `useOpenAIKey=true`。 */
  {
    name: "byok-force", marker: "@cxteam-byokforce",
    rx: new RegExp("function (" + ID + ")\\((" + ID + "),(" + ID + ")\\)\\{return (" + ID + ")\\(\\2\\)\\?\\3\\.useClaudeKey\\?\"anthropic\":void 0:(" + ID + ")\\(\\2\\)\\?\\3\\.useGoogleKey\\?\"google\":void 0:\\3\\.useOpenAIKey\\?\"openai\":void 0\\}", "g"),
    sub: (m) => "function " + m[1] + "(" + m[2] + "," + m[3] + "){/*@cxteam-byokforce*/try{if((" + m[3] + "?.aiSettings?.userAddedModels||[]).includes(" + m[2] + "))return\"openai\"}catch(__cxe){}return " + m[4] + "(" + m[2] + ")?" + m[3] + ".useClaudeKey?\"anthropic\":void 0:" + m[5] + "(" + m[2] + ")?" + m[3] + ".useGoogleKey?\"google\":void 0:" + m[3] + ".useOpenAIKey?\"openai\":void 0}",
  },
  {
    name: "byok-keepon", marker: "@cxteam-byokkeepon",
    rx: new RegExp("(" + ID + ")\\(\\{isLocalMode:(" + ID + ")\\.localMode\\}\\)&&(" + ID + ")===(" + ID + ")\\.FREE&&(" + ID + ")!==\\4\\.FREE&&this\\.setUseOpenAIKey\\(!1\\)", "g"),
    sub: () => "/*@cxteam-byokkeepon*/void 0",
  },
];

/* ── 件B @cx-chain:v3 链式增量(exthost bundle fetch seam;与 cursor_chain_patch.py 逐字节一致) ──
   目标 bundle 与 workbench 那三条无关:这是 extension-host 进程里真发 /responses 的两条 dist/main.js。
   在工厂底层 fetch(锚 customHeaders:d}=e,m=,括号配平包住 builder 调用)+ responses SDK 客户端
   fetch:t.fetch 双处包 __cxWrap;force-responses 让 terra 模型也走 /responses。shim base64 内嵌:
   含正则反斜杠(\s \/ \n),JS 字符串字面量会吃掉未识别转义 → 必须 base64 运行时解码才能逐字节搬运。
   门控 CX_CHAIN=0 关;幂等靠 CHAIN_MARKER;非致命(锚点没了只告警跳过,不阻断核心解锁补丁)。 */
const CHAIN_MARKER = "@cx-chain:v3";
const CHAIN_ANCHOR = "customHeaders:d}=e,m=";
const CHAIN_TARGETS = [
  path.join("extensions", "cursor-agent-exec", "dist", "main.js"),
  path.join("extensions", "cursor-local-agent-runtime", "dist", "main.js"),
];
const CHAIN_SHIM_B64 =
  "LyogQGN4LWNoYWluOnYzIOKAlCBwcmV2aW91c19yZXNwb25zZV9pZCDpk77lvI/lop7ph48o5bel5Y6C5bqV5bGCIGZldGNoIHNlYW07Q1hfQ0hBSU49MCDlhbMpICovCjsoKCk9Pnt0cnl7CmlmKGdsb2JhbFRoaXMuX19jeFdyYXApcmV0dXJuOwpjb25zdCBzdD1uZXcgTWFwKCk7CmNvbnN0IFRSQUNFPScvdG1wL2N4LWNoYWluLXRyYWNlLmxvZyc7CmNvbnN0IHRyYWNlPShvKT0+e3RyeXtyZXF1aXJlKCdmcycpLmFwcGVuZEZpbGVTeW5jKFRSQUNFLEpTT04uc3RyaW5naWZ5KE9iamVjdC5hc3NpZ24oe3RzOkRhdGUubm93KCl9LG8pKSsnXG4nKX1jYXRjaChfKXt9fTsKdHJhY2Uoe2V2OidpbnN0YWxsZWQnLHBpZDooZ2xvYmFsVGhpcy5wcm9jZXNzJiZwcm9jZXNzLnBpZCl8fDB9KTsKY29uc3QgSD0ocyk9PntsZXQgaD01MzgxO2ZvcihsZXQgaT0wO2k8cy5sZW5ndGg7aSsrKWg9KChoPDw1KStoK3MuY2hhckNvZGVBdChpKSk+Pj4wO3JldHVybiBoLnRvU3RyaW5nKDM2KX07CmNvbnN0IERKPShvKT0+e3RyeXtyZXR1cm4gSChKU09OLnN0cmluZ2lmeShvKSl9Y2F0Y2goXyl7cmV0dXJuICd4J319OwpnbG9iYWxUaGlzLl9fY3hXcmFwPWZ1bmN0aW9uKE9SSUcpewogIGlmKHR5cGVvZiBPUklHIT09J2Z1bmN0aW9uJylyZXR1cm4gT1JJRzsKICByZXR1cm4gYXN5bmMgZnVuY3Rpb24odXJsLGluaXQpewogICAgdHJ5ewogICAgICB0cnl7aWYocHJvY2Vzcy5lbnYuQ1hfQ0hBSU49PT0nMCcpcmV0dXJuIE9SSUcodXJsLGluaXQpfWNhdGNoKF8pe30KICAgICAgY29uc3QgdT1TdHJpbmcodHlwZW9mIHVybD09PSdzdHJpbmcnP3VybDoodXJsJiZ1cmwudXJsKXx8JycpOwogICAgICB0cnl7CiAgICAgICAgbGV0IGJsPShpbml0JiZ0eXBlb2YgaW5pdC5ib2R5PT09J3N0cmluZycpP2luaXQuYm9keS5sZW5ndGg6LTEsIG1jPS0xLCBpYz0tMSwgaGFzUHJldj1mYWxzZSwgbW9kZWw9Jyc7CiAgICAgICAgaWYoYmw+MCl7dHJ5e2NvbnN0IGpiPUpTT04ucGFyc2UoaW5pdC5ib2R5KTttYz1BcnJheS5pc0FycmF5KGpiLm1lc3NhZ2VzKT9qYi5tZXNzYWdlcy5sZW5ndGg6LTE7aWM9QXJyYXkuaXNBcnJheShqYi5pbnB1dCk/amIuaW5wdXQubGVuZ3RoOi0xO2hhc1ByZXY9ISFqYi5wcmV2aW91c19yZXNwb25zZV9pZDttb2RlbD1TdHJpbmcoamIubW9kZWx8fCcnKX1jYXRjaChfKXt9fQogICAgICAgIHRyYWNlKHtldjond3JhcC1lbnRyeScsdTp1LnNsaWNlKDAsNjApLGJvZHlMZW46YmwsbXNnQ291bnQ6bWMsaW5wdXRDb3VudDppYyxoYXNQcmV2LG1vZGVsfSk7CiAgICAgIH1jYXRjaChfKXt9CiAgICAgIGlmKCEvXC9yZXNwb25zZXMoXD98JCkvLnRlc3QodSkpcmV0dXJuIE9SSUcodXJsLGluaXQpOwogICAgICBpZighaW5pdHx8dHlwZW9mIGluaXQuYm9keSE9PSdzdHJpbmcnKXJldHVybiBPUklHKHVybCxpbml0KTsKICAgICAgbGV0IGJvZHk7dHJ5e2JvZHk9SlNPTi5wYXJzZShpbml0LmJvZHkpfWNhdGNoKF8pe3JldHVybiBPUklHKHVybCxpbml0KX0KICAgICAgaWYoIUFycmF5LmlzQXJyYXkoYm9keS5pbnB1dCl8fGJvZHkuaW5wdXQubGVuZ3RoPDF8fGJvZHkucHJldmlvdXNfcmVzcG9uc2VfaWQpcmV0dXJuIE9SSUcodXJsLGluaXQpOwogICAgICBjb25zdCBrZXk9REooYm9keS5pbnB1dFswXSkrJzonK1N0cmluZyhib2R5Lm1vZGVsfHwnJyk7CiAgICAgIGNvbnN0IGRpZ3M9Ym9keS5pbnB1dC5tYXAoREopOwogICAgICBjb25zdCBzPXN0LmdldChrZXkpOwogICAgICBsZXQgY2hhaW5lZD1mYWxzZSxzZW5kSW5pdD1pbml0OwogICAgICBpZihzJiZzLnJpZCYmYm9keS5pbnB1dC5sZW5ndGg+cy5jb3VudCl7CiAgICAgICAgbGV0IG9rPXRydWU7Zm9yKGxldCBpPTA7aTxzLmNvdW50O2krKylpZihkaWdzW2ldIT09cy5kaWdzW2ldKXtvaz1mYWxzZTticmVha30KICAgICAgICBpZihvayl7CiAgICAgICAgICBjb25zdCBuYj1PYmplY3QuYXNzaWduKHt9LGJvZHkse2lucHV0OmJvZHkuaW5wdXQuc2xpY2Uocy5jb3VudCkscHJldmlvdXNfcmVzcG9uc2VfaWQ6cy5yaWR9KTsKICAgICAgICAgIHNlbmRJbml0PU9iamVjdC5hc3NpZ24oe30saW5pdCx7Ym9keTpKU09OLnN0cmluZ2lmeShuYil9KTsKICAgICAgICAgIGNoYWluZWQ9dHJ1ZTt0cmFjZSh7ZXY6J2NoYWluZWQnLGRlbHRhOmJvZHkuaW5wdXQubGVuZ3RoLXMuY291bnQsdG90YWw6Ym9keS5pbnB1dC5sZW5ndGgscmlkOnMucmlkLnNsaWNlKDAsMTYpfSk7CiAgICAgICAgfQogICAgICB9CiAgICAgIGlmKCFjaGFpbmVkKXRyYWNlKHtldjoncGFzc3Rocm91Z2gtZnVsbCcsaW5wdXRMZW46Ym9keS5pbnB1dC5sZW5ndGgsaGFkU3RvcmVkOiEhcyx1OnUuc2xpY2UoMCw2MCl9KTsKICAgICAgbGV0IHJlcz1hd2FpdCBPUklHKHVybCxzZW5kSW5pdCk7CiAgICAgIGlmKGNoYWluZWQmJnJlcyYmKHJlcy5zdGF0dXM9PT00MDB8fHJlcy5zdGF0dXM9PT00MDQpKXtzdC5kZWxldGUoa2V5KTt0cmFjZSh7ZXY6J2ZhbGxiYWNrLWZ1bGwnLHN0YXR1czpyZXMuc3RhdHVzfSk7cmVzPWF3YWl0IE9SSUcodXJsLGluaXQpfQogICAgICBpZihyZXMmJnJlcy5vayl7CiAgICAgICAgdHJ5ewogICAgICAgICAgcmVzLmNsb25lKCkudGV4dCgpLnRoZW4oKHQpPT57CiAgICAgICAgICAgIHRyeXsKICAgICAgICAgICAgICBjb25zdCBtPXQubWF0Y2goLyJpZCJccyo6XHMqIihyZXNwX1teIl0rKSIvKTsKICAgICAgICAgICAgICBpZihtKXtzdC5zZXQoa2V5LHtjb3VudDpib2R5LmlucHV0Lmxlbmd0aCxkaWdzLHJpZDptWzFdfSk7aWYoc3Quc2l6ZT41MClzdC5kZWxldGUoc3Qua2V5cygpLm5leHQoKS52YWx1ZSk7dHJhY2Uoe2V2OidzYXZlZC1yaWQnLGNvdW50OmJvZHkuaW5wdXQubGVuZ3RoLHJpZDptWzFdLnNsaWNlKDAsMjApfSl9CiAgICAgICAgICAgICAgZWxzZXt0cmFjZSh7ZXY6J25vLXJpZCcsc2FtcGxlOnQuc2xpY2UoMCwxMDApfSl9CiAgICAgICAgICAgIH1jYXRjaChfKXt9CiAgICAgICAgICB9KS5jYXRjaCgoKT0+e30pOwogICAgICAgIH1jYXRjaChfKXt9CiAgICAgIH0KICAgICAgcmV0dXJuIHJlczsKICAgIH1jYXRjaChlKXt0cnl7cmV0dXJuIE9SSUcodXJsLGluaXQpfWNhdGNoKF8pe3Rocm93IGV9fQogIH07Cn07Cn1jYXRjaChfKXt9fSkoKTsK";
const CHAIN_SHIM = Buffer.from(CHAIN_SHIM_B64, "base64").toString("utf8");

// 括号配平包住工厂 builder 调用(与 cursor_chain_patch.py 的 wrap_builder_call 同逻辑)。
// 返回 {ok, out?, why}。锚点 count!=1 / 括号不配平 → ok=false(调用方告警跳过,不改坏)。
function wrapBuilderCall(src) {
  const n = src.split(CHAIN_ANCHOR).length - 1;
  if (n !== 1) return { ok: false, why: "锚 count=" + n };
  const i = src.indexOf(CHAIN_ANCHOR), j = i + CHAIN_ANCHOR.length, k = src.indexOf("(", j);
  if (k < 0) return { ok: false, why: "锚后无 (" };
  let depth = 0, p = k;
  for (; p < src.length; p++) { const c = src[p]; if (c === "(") depth++; else if (c === ")") { depth--; if (depth === 0) break; } }
  if (depth !== 0) return { ok: false, why: "括号不配平" };
  const call = src.slice(j, p + 1);
  const wrapped = CHAIN_ANCHOR + "(globalThis.__cxWrap||(f=>f))(" + call + ")";
  return { ok: true, out: src.slice(0, i) + wrapped + src.slice(p + 1), why: call.slice(0, 40) };
}

// 两条 exthost bundle 同名 main.js → 备份/回滚必须用扁平化文件名防碰撞。
function chainBakName(rel) { return "cxchain__" + rel.split(path.sep).join("__"); }

// 计算一条 chain bundle 的补丁(不落盘)。返回 {p, out, bakName, applied} 或 null(跳过)。
function planChainBundle(rel) {
  const p = path.join(RES, rel);
  if (!fs.existsSync(p)) { console.log("   SKIP chain %s(不存在)", rel); return null; }
  const src = fs.readFileSync(p, "utf8");
  if (src.includes(CHAIN_MARKER)) { console.log("   SKIP chain %s(已打过)", path.basename(path.dirname(path.dirname(rel)))); return null; }
  const wb = wrapBuilderCall(src);
  if (!wb.ok) { console.log("   ⚠️  chain %s 锚点问题(%s)→ 跳过(不阻断解锁补丁)", rel, wb.why); return null; }
  let out = wb.out; const applied = ["wrap:" + wb.why];
  const rOld = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:t.fetch})";
  const rNew = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:(globalThis.__cxWrap||(f=>f))(t.fetch)})";
  if (out.split(rOld).length - 1 === 1) { out = out.replace(rOld, rNew); applied.push("resp:t.fetch"); }
  const ufOld = '?"responses":"chat_completions"', ufNew = '?"responses":"responses"';
  if (out.split(ufOld).length - 1 === 1) { out = out.replace(ufOld, ufNew); applied.push("force-responses"); }
  out = CHAIN_SHIM + "\n" + out;
  const dir2 = path.basename(path.dirname(path.dirname(rel)));
  console.log("   chain %s: 将打 [%s]", dir2, applied.join(", "));
  return { p, out, bakName: chainBakName(rel), applied };
}

/* ── @cx-chain:v3 下线(2026-09-01):默认不再安装,装过的机器跑一次就自动摘掉 ──────────
   为什么下线:服务端那半(解析 previous_response_id)已经不在线上了。实测三条腿——
   ① 线上 responses.js 只解构 {input,instructions,stream},全文 grep previous_response_id = 0 次;
   ② 带一个**伪造**的 previous_response_id 打过去仍返 HTTP 200(字段被端到端忽略);
   ③ 本机 trace 里 fallback-full = 0 次(shim 唯一的兜底是 400/404,服务端返 200 就永不触发)。
   于是客户端这半的净效果是「只发增量,而服务端把增量当成整段上下文」= 静默丢历史,
   而且丢了不报错。收益为零、风险非零 ⇒ 摘掉。
   服务端那半回来时用 --chain 重新装上即可(代码原样保留,没删)。
   摘除是 planChainBundle 的**精确逆操作**(含 force-responses 那处端点还原),
   逆完必须 ①不含 CHAIN_MARKER ②不含 __cxWrap ③过语法校验 才落盘;
   任一条不满足就保持原样并提示走 --revert 从备份还原(绝不留半截)。 */
function unwrapChainCall(src) {
  const WP = CHAIN_ANCHOR + "(globalThis.__cxWrap||(f=>f))(";
  const n = src.split(WP).length - 1;
  if (n !== 1) return { ok: false, why: "wrap 锚 count=" + n };
  const i = src.indexOf(WP);
  const openAt = i + WP.length - 1; // 包装器自己那个 "("
  let depth = 0, p = openAt;
  for (; p < src.length; p++) { const c = src[p]; if (c === "(") depth++; else if (c === ")") { depth--; if (depth === 0) break; } }
  if (depth !== 0) return { ok: false, why: "括号不配平" };
  const call = src.slice(openAt + 1, p); // 原始 builder 调用,原样放回
  return { ok: true, out: src.slice(0, i) + CHAIN_ANCHOR + call + src.slice(p + 1), why: call.slice(0, 40) };
}

// 纯函数:从一段已打过 chain 的源码里逆出原始源码。{ok,out?,why,removed[]}
function chainRemoveFromSource(src) {
  let out = src, removed = [];
  const head = CHAIN_SHIM + "\n";
  if (!out.startsWith(head)) return { ok: false, why: "头部 shim 形状不符" };
  out = out.slice(head.length); removed.push("shim");
  const uw = unwrapChainCall(out);
  if (!uw.ok) return { ok: false, why: uw.why };
  out = uw.out; removed.push("unwrap");
  const rOld = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:t.fetch})";
  const rNew = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:(globalThis.__cxWrap||(f=>f))(t.fetch)})";
  if (out.split(rNew).length - 1 === 1) { out = out.replace(rNew, rOld); removed.push("resp:t.fetch"); }
  // force-responses 还原:摘掉链式后端点回到 chat_completions,即 litellm chat→responses 桥那条
  // 设计内的路(载体是点分 slug 就是为它准备的)。留着 force-responses 等于留半截改动。
  const ufOld = '?"responses":"chat_completions"', ufNew = '?"responses":"responses"';
  if (out.split(ufNew).length - 1 === 1) { out = out.replace(ufNew, ufOld); removed.push("force-responses"); }
  if (out.includes(CHAIN_MARKER)) return { ok: false, why: "逆完仍含 marker" };
  if (out.includes("__cxWrap")) return { ok: false, why: "逆完仍含 __cxWrap" };
  return { ok: true, out, removed };
}

// 计算一条 chain bundle 的**摘除**(不落盘)。没装过 → null(静默,不刷屏)。
function planChainRemoval(rel) {
  const p = path.join(RES, rel);
  if (!fs.existsSync(p)) return null;
  const src = fs.readFileSync(p, "utf8");
  if (!src.includes(CHAIN_MARKER)) return null;
  const dir2 = path.basename(path.dirname(path.dirname(rel)));
  const r = chainRemoveFromSource(src);
  if (!r.ok) {
    console.log("   ⚠️  chain %s 摘除失败(%s)→ 保持原样。想清干净请跑 --revert 从备份还原。", dir2, r.why);
    return null;
  }
  console.log("   chain %s: 将摘除 [%s]", dir2, r.removed.join(", "));
  return { p, out: r.out, bakName: chainBakName(rel), removed: r.removed };
}

/* ── 件C @cx-ctxwin:v3 上下文窗口单位归一(2026-09-22) ─────────────────────────
   病:Cursor 服务端在 `InferenceExtendedUsageInfo.max_tokens` 里回的是**档位名的数字**
   (500k→500,1m→1),不是真实 token 数。于是 Cursor 自家阈值 min(max-10000, max*0.9)
   变成负数,`used >= 负数` 恒真 ⇒ **连发一句 "hi" 也每轮触发摘要压缩**,
   界面上下文百分比飙到 8146%~27706%。

   实测(本机 932 会话 / 84 模型):非 0 的 maxTokens 只落在 {256,272,300,500} —— 全是
   真实窗口的 k 数字。**不是按模型分的,是按时间分的**:5/17~6/26 共 42 次全对 0 次错;
   9/17 是最后一个正确值;9/20 起 19 次全错。同一个 grok-4.6 在 9/14、9/17 拿到 256000,
   9/22 拿到 0 ⇒ 服务端 9/17~9/20 之间引入的回归,**任何模型都会中**,不是某个模型专属。
   ⛔ 不是我们自己的补丁造成的:21 条坏值时间戳全部早于本机第一次打补丁,
   且本文件全文 grep 不到 maxTokens / tokenLimit / summariz。

   补三个点,是**穷举**出来的不是挑的 —— daemon.cjs 里对 maxTokens 做算术/比较的代码行
   共 10 处:4 处在 B 体内、5 处走 `tokenDetails.maxTokens`(A 覆盖)、剩下 1 处就是 C。
     A createRedactedConversationTokenDetails  tokenDetails 唯一构造入口(显示/超限拦截/持久化)
     B getBackgroundSummarizationTriggerThreshold  所有「该不该压」的判定都过它;
       🔴 有一条触发路径直接用 currentUsage.maxTokens **不经过 tokenDetails**,只补 A 盖不住
     C shouldPersistBackgroundSummarization  它自己又拿裸 maxTokens 算了一次 unusedTokens,
       B 的归一只活在 B 的局部变量里 ⇒ 不补 C 会让 persist 跟着 start 一起必然触发
   换算沿用 Cursor 自家的 WS_() 解析器语义(k→1e3 m→1e6):1→1e6;0<n<4096→n×1e3;
   0 与 ≥4096 原样不动(0 = 自定义模型窗口未知,行为零变化;≥4096 = 已经是真实 token 数)。
   界 4096 的依据:最小档 200k 映射成 200,真实窗口最小 200000,4096 落在两簇中间。

   ⚠️ 该字段由 Cursor 自家后端下发。**后端为什么给 k 数字这一项没有数据**(没抓包),
   不许当成因写出去。本补丁只做「收到什么就归一什么」,不依赖任何对后端的推测。 */
const CTXWIN_MARKER = "@cx-ctxwin:v3";
const CTXWIN_TARGETS = [
  { rel: path.join("extensions", "cursor-local-agent-runtime", "dist", "main.js"), kind: "min" },
  { rel: path.join("extensions", "cursor-agent-host", "dist", "main.js"), kind: "min" },
  { rel: path.join("extensions", "cursor-agent-exec", "dist", "main.js"), kind: "min" },
  { rel: path.join("extensions", "cursor-agent-host", "dist", "agent-host-daemon", "dist", "bin", "daemon.cjs"), kind: "src" },
];
// 🔴 名字位一律用 ID 捕获 + 反向引用绑定语义 —— 函数名、**参数名、局部变量名**都算名字位。
//    3.20.21 栽在函数名($7y);3.21.18 栽在局部变量名与参数名:B 的 `const n=[]` 摇成
//    `const r=[]`,C 的第三参数与局部变量互换(`(e,t,n)/const r` → `(e,t,r)/const n`),
//    写死名字的锚点当场命中 0。下面不许再出现字面量 e/t/n/r。
// 🔴 2026-10-12(3.24.12):`promptContextUsageSnapshotBlobId:void 0,` 这一个字段被上游**删了**,
//    A 的其余部分逐字不变 ⇒ 四个目标(含不 minify 的 daemon.cjs)锚 A 全部命中 0。
//    这跟 3.20.21/3.21.18 那两次**不是同一个病**:那两次是名字位被摇,这次是**字段消失**,
//    所以 ID 捕获救不了它 —— 判据就是「连不 minify 的 daemon.cjs 都命中 0」。
//    修法 = 把那个字段整段写成可选 `(?:…)?`,两代都认(B/C 在 3.24.12 上逐字未变,不动)。
//    ⚠️ 重建产物时也必须按**捕获到的实际字段**写回,不能照旧无条件插回那个字段
//    (插回去 = 给新版凭空加一个上游已经删掉的键)。
const CW_A_MIN = new RegExp("function\\s+(" + ID + ")\\((" + ID + "),(" + ID + ")\\)\\{return\\{"
  + "usedTokens:0,maxTokens:0,breakdown:void 0,promptContextUsageTree:void 0,"
  + "(promptContextUsageSnapshotBlobId:void 0,)?\\.\\.\\.\\3,_privacyMode:\\2\\}\\}", "g");
const CW_B_MIN = new RegExp("function\\s+(" + ID + ")\\((" + ID + "),(" + ID + ")\\)\\{if\\(\\2<=0\\)return;"
  + "const (" + ID + ")=\\[\\];return void 0!==\\3\\.unusedTokensThresholdToStartBackgroundSummarization", "g");
const CW_C_MIN = new RegExp("function\\s+(" + ID + ")\\((" + ID + "),(" + ID + "),(" + ID + ")\\)\\{"
  + "const (" + ID + ")=\\3-\\2;return\\s+" + ID + "\\(\\2,\\3,\\4\\)&&"
  + "\\(void 0!==\\4\\.unusedTokensThresholdToPersistBackgroundSummarization", "g");
// 两代各一份字面量(daemon.cjs 不 minify,所以这里是精确字面量而不是正则)。
// 3.24.12 删掉了 `promptContextUsageSnapshotBlobId` 那一行;≤3.21 还有。
// 判法 = 谁命中就用谁,**两代都命中或都不命中都算锚点坏**(见下面 na 的算法)。
const CW_A_SRC_BLOB = `    promptContextUsageSnapshotBlobId: void 0,
`;
const cwASrc = (blobLine) => `function createRedactedConversationTokenDetails(privacyMode, partial2) {
  return {
    usedTokens: 0,
    maxTokens: 0,
    breakdown: void 0,
    promptContextUsageTree: void 0,
${blobLine}    ...partial2,
    _privacyMode: privacyMode
  };
}`;
const CW_A_SRC = cwASrc(CW_A_SRC_BLOB);        // ≤3.21 那代
const CW_A_SRC_NEW = cwASrc("");               // 3.24.12 起
const CW_B_SRC = `function getBackgroundSummarizationTriggerThreshold(maxTokens, props) {
  if (maxTokens <= 0) {`;
const CW_C_SRC = `function shouldPersistBackgroundSummarization(usedTokens, maxTokens, props) {
  const unusedTokens = maxTokens - usedTokens;`;
// daemon.cjs 的三处锚点:名字逐字钉死,只容忍空白;A 的第 1 组 = 老代那行 snapshotBlobId(有就是 ≤3.21)
const CW_A_SRC_RX = /function\s+createRedactedConversationTokenDetails\(\s*privacyMode\s*,\s*partial2\s*\)\s*\{\s*return\s*\{\s*usedTokens:\s*0,\s*maxTokens:\s*0,\s*breakdown:\s*void 0,\s*promptContextUsageTree:\s*void 0,\s*(promptContextUsageSnapshotBlobId:\s*void 0,\s*)?\.\.\.partial2,\s*_privacyMode:\s*privacyMode\s*\};?\s*\}/g;
const CW_B_SRC_RX = /function\s+getBackgroundSummarizationTriggerThreshold\(\s*maxTokens\s*,\s*props\s*\)\s*\{(?=\s*if\s*\(\s*maxTokens\s*<=\s*0\s*\))/g;
const CW_C_SRC_RX = /function\s+shouldPersistBackgroundSummarization\(\s*usedTokens\s*,\s*maxTokens\s*,\s*props\s*\)\s*\{(?=\s*const\s+unusedTokens\s*=\s*maxTokens\s*-\s*usedTokens\s*;)/g;
const CW_NORM_MIN = 'function __cxN(x){return "number"==typeof x&&x>0&&x<4096?(1===x?1e6:1e3*x):x}';
const cwNormSrc = (v) =>
  `${v} = typeof ${v} === "number" && ${v} > 0 && ${v} < 4096 ? (${v} === 1 ? 1e6 : ${v} * 1e3) : ${v};`;

// 纯函数:把件C 打进一段文本。三锚点任一不是 exactly-1 ⇒ {ok:false},调用方整条跳过。
// 与 scripts/zk-cursor-web/cursor_ctxwin_patch.js 逐字节同语义(单机版供已装好的人补打)。
/* 🔴 2026-10-28(3.28.5):`cursor-agent-host/dist/main.js` 里那段代码**整段搬走了** ——
   `unusedTokens`/`createRedactedConversationTokenDetails` 全 0,只剩 protobuf 字段声明,
   阈值算术都挪到 agent-exec 与 local-agent-runtime(这两个仍 3/3)。
   「这个文件里压根没有要补的东西」和「锚点漂了」后果完全不同:前者无害,
   后者=用户每轮被压缩,但原来两者都回同一句 `锚A count=0` ⇒ 会被当成漂移去追,
   更糟的是上面新加的红字会把无害情况也报成「没打上会很慢」,制造假红。
   判据(不是猜):这个文件里有没有 `unusedTokensThresholdTo…` 这两个阈值名 ——
   它们是 B/C 两处要改的那段算术的名字,**不被 minify**(上游自己的字段名)。
   一个都没有 ⇒ 这份 bundle 不参与摘要判定 ⇒ 不适用,不是漂移。 */
const CW_RELEVANT = /unusedTokensThresholdTo(?:Start|Persist)BackgroundSummarization/;
function ctxwinApplyToText(src, kind) {
  if (src.includes(CTXWIN_MARKER)) return { ok: false, why: "已打过", skip: true };
  if (!CW_RELEVANT.test(src)) return { ok: false, why: "这份 bundle 不含摘要阈值(上游把那段挪走了)⇒ 不适用", skip: true, na: true };
  if (kind === "min") {
    CW_A_MIN.lastIndex = 0; const a = [...src.matchAll(CW_A_MIN)];
    CW_B_MIN.lastIndex = 0; const b = [...src.matchAll(CW_B_MIN)];
    CW_C_MIN.lastIndex = 0; const c = [...src.matchAll(CW_C_MIN)];
    if (a.length !== 1) return { ok: false, why: "锚A count=" + a.length };
    if (b.length !== 1) return { ok: false, why: "锚B count=" + b.length };
    if (c.length !== 1) return { ok: false, why: "锚C count=" + c.length };
    const af = a[0][1], bf = b[0][1], cf = c[0][1];
    const aPriv = a[0][2], aPart = a[0][3];       // A 的两个参数名(捕获来的)
    // 3.24.12 起 `promptContextUsageSnapshotBlobId:void 0,` 可能不在 ⇒ 用捕获到的原文回填,
    // 捕获为空就一个字都不加(别凭空给新版插回上游删掉的键)。
    const aBlob = a[0][4] || "";
    const bMax = b[0][2];                          // B 的第 1 参 = maxTokens
    const cMax = c[0][3];                          // C 的第 2 参 = maxTokens
    let out = src.replace(a[0][0],
      `function ${af}(${aPriv},${aPart}){/*${CTXWIN_MARKER}*/${CW_NORM_MIN}` +
      `const __cxR={usedTokens:0,maxTokens:0,breakdown:void 0,promptContextUsageTree:void 0,` +
      `${aBlob}...${aPart},_privacyMode:${aPriv}};` +
      `__cxR.maxTokens=__cxN(__cxR.maxTokens);` +
      `if(void 0!==__cxR.breakdown&&"object"==typeof __cxR.breakdown)__cxR.breakdown={...__cxR.breakdown,maxTokens:__cxN(__cxR.breakdown.maxTokens)};` +
      `return __cxR}`);
    // 插入点 = 参数表后的第一个 `{`(参数表里不可能有 `{`)⇒ 不依赖空格也不依赖名字
    const cwIns = (m, v) => m.replace("{", `{/*${CTXWIN_MARKER}*/` +
      `${v}="number"==typeof ${v}&&${v}>0&&${v}<4096?(1===${v}?1e6:1e3*${v}):${v};`);
    out = out.replace(b[0][0], cwIns(b[0][0], bMax));
    out = out.replace(c[0][0], cwIns(c[0][0], cMax));
    return { ok: true, out, applied: [`A=${af}`, `B=${bf}`, `C=${cf}`] };
  }
  // A 有两代字面量:老的带 snapshotBlobId、3.24.12 起没有。各数一次,**加起来必须恰好 1**
  // ——两代同时命中说明文件里有两个同名函数(形状变了),和 0 命中一样不许动手。
  // 3.27.12 起 daemon.cjs 去掉了空白(`function createRedactedConversationTokenDetails(privacyMode,partial2){return{…`),
  // 名字没变 ⇒ 字面量锚点全 0。改成「只容忍空白差异」的正则(名字仍逐字钉死),换进去的代码与原来逐字相同。
  const aM = [...src.matchAll(CW_A_SRC_RX)], bM = [...src.matchAll(CW_B_SRC_RX)], cM = [...src.matchAll(CW_C_SRC_RX)];
  const naOld = aM.filter((m) => m[1]).length, naNew = aM.length - naOld;
  const na = aM.length;
  const aBlobSrc = naOld === 1 ? CW_A_SRC_BLOB : "";
  const nb = bM.length, nc = cM.length;
  if (na !== 1) return { ok: false, why: "锚A count=" + na + "(老代=" + naOld + " 新代=" + naNew + ")" };
  if (nb !== 1) return { ok: false, why: "锚B count=" + nb };
  if (nc !== 1) return { ok: false, why: "锚C count=" + nc };
  const aSrc = aM[0][0];
  let out = src.replace(aSrc, () => `function createRedactedConversationTokenDetails(privacyMode, partial2) {
  /*${CTXWIN_MARKER}*/
  const __cxN = (x) => typeof x === "number" && x > 0 && x < 4096 ? (x === 1 ? 1e6 : x * 1e3) : x;
  const __cxR = {
    usedTokens: 0,
    maxTokens: 0,
    breakdown: void 0,
    promptContextUsageTree: void 0,
${aBlobSrc}    ...partial2,
    _privacyMode: privacyMode
  };
  __cxR.maxTokens = __cxN(__cxR.maxTokens);
  if (__cxR.breakdown !== void 0 && typeof __cxR.breakdown === "object") {
    __cxR.breakdown = { ...__cxR.breakdown, maxTokens: __cxN(__cxR.breakdown.maxTokens) };
  }
  return __cxR;
}`);
  // B/C:正则只吃到函数体的 `{`(后面靠前瞻钉语义),所以只在 `{` 后插入,原函数体一个字不动
  out = out.replace(bM[0][0], (m) => m + `
  /*${CTXWIN_MARKER}*/
  ${cwNormSrc("maxTokens")}`);
  out = out.replace(cM[0][0], (m) => m + `
  /*${CTXWIN_MARKER}*/
  ${cwNormSrc("maxTokens")}`);
  return { ok: true, out, applied: ["A", "B", "C"] };
}

// 四个目标里有两个与 chain 家族同名 main.js ⇒ 备份名必须扁平化防碰撞。
function ctxwinBakName(rel) { return "cxctxwin__" + rel.split(path.sep).join("__"); }

// 计算一条 ctxwin bundle 的补丁(不落盘)。baseText 非空 = 同一文件已有别的家族的计划,
// 必须接着它的产物改,否则两条计划先后写同一个路径,后写的把前一个悄悄冲掉。
function planCtxwinBundle(t, baseText) {
  const p = path.join(RES, t.rel);
  if (!fs.existsSync(p)) { console.log("   SKIP ctxwin %s(不存在)", t.rel); return null; }
  const src = baseText != null ? baseText : fs.readFileSync(p, "utf8");
  const r = ctxwinApplyToText(src, t.kind);
  if (!r.ok) {
    /* 🔴 2026-10-28:原来这里只在屏幕中段打一行 ⚠️ 就 return,**不进 MISSED** ⇒ 收尾仍是
       「✅ 完成」、退出码 0。件C 没打上的后果是用户每轮都被自动压缩(10-28 实测 4 个人
       每 100 条主请求压 10~54 次、触发时上下文只有 8~30k,压缩本身中位数 43~98s),
       他只会觉得「Cursor 比 Claude 慢」,不会知道要来双击 REPAIR。
       ⇒ 和 workbench 家族同等对待:进 MISSED,收尾列红、退出码 5。仍然不阻断其余补丁。 */
    if (!r.skip) {
      console.log("   ⚠️  ctxwin %s 锚点问题(%s)→ 跳过(不阻断解锁补丁)", path.basename(t.rel), r.why);
      MISSED.push({ rel: t.rel, why: "件C(上下文窗口归一)锚点认不出:" + r.why + " —— 没打上会每轮自动压缩对话,体感『很慢』" });
    }
    return null;
  }
  console.log("   ctxwin %s: 将打 [%s]", path.basename(t.rel), r.applied.join(", "));
  return { p, out: r.out, bakName: ctxwinBakName(t.rel), applied: r.applied };
}

// 测试钩子:对任意 bundle 跑真实件C,写出到 CX_CTXWIN_OUT,供离线台架断言。
// CX_CTXWIN_KIND=min|src。退出码 0=打上 / 17=锚点不满足。
if (process.env.CX_CTXWIN_APPLY_TO_FILE) {
  const f = process.env.CX_CTXWIN_APPLY_TO_FILE;
  const r = ctxwinApplyToText(fs.readFileSync(f, "utf8"), process.env.CX_CTXWIN_KIND || "min");
  if (!r.ok) { console.error("ctxwin 打不上:" + r.why); process.exit(17); }
  fs.writeFileSync(process.env.CX_CTXWIN_OUT || (f + ".cwout"), r.out);
  console.error("ctxwin applied: " + r.applied.join(", "));
  process.exit(0);
}

/* ── 件D @cx-noloop:v1 关掉 Agent Host 本地循环(2026-09-23) ───────────────────
   病:同事 Windows/3.21.18 发消息报
     `[permission_denied] InferenceService.RunInference is not enabled for this account`
     （之前那一发是 `An unexpected error occurred. Request ID: …`）。
   他本地日志里两句关键的:选中模型 `grok-4.7` 被当成 Cursor 的 `catalogModelId`;
   这一轮的 runtime 是 **`managed-local`**。

   这跟 `bOd`(件byok,决定"这次请求走不走 BYOK")**不是一处**。agent-host 里另有一个
   独立的决策点 `createLocalLoopTurnRouter`,决定这一轮跑在哪个 runtime 上:
     `connect`       = 老路,交给 Cursor 后端循环(BYOK 一直是走这条活的)
     `managed-local` = 新路,本地跑 agent 循环、直接打 Cursor 自家 InferenceService
     `fail`          = 当场抛错
   日志行 `Selected Agent Host turn runtime {runtime, reason}` 就是它打的。

   🔴 Cursor 自己的源码写明了 local loop **不支持 BYOK**(daemon.cjs 未压缩那份):
     private-model-not-supported:
       "Turns with custom model credentials (BYOK/private models) are not supported
        on the local loop"
     且给出解法:"Disable agent_host_local_loop to run on the backend loop."
   ⇒ 对我们这批**只用 BYOK**的机器,这条新路在任何情况下都是错的:
     带 key  → `getSharedTurnIneligibilityReason` 返 private-model-not-supported → `fail` 抛错
     不带 key → 判 eligible → `managed-local` → 打 Cursor 自家推理 → 没权限 → permission_denied
   两个出口都是坏的,所以补丁不去改判定条件,而是让路由器**恒返 `connect`**。

   三段式(证据链):
   - 假设:同事那一发之所以打到 Cursor 自家推理,是因为 `agent_host_local_loop` 这个
     服务端 feature gate 在他那边是 on,于是路由器把这一轮判给了 managed-local。
   - 证伪条件:若假设错(差异不在这个 gate),那么**能正常用 BYOK 的机器**上这个 gate
     也应该是 on。
   - 数据:本机(BYOK 正常、同版本 3.21.18)`state.vscdb` 的
     `workbench.experiments.statsigBootstrap` 里
     `feature_gates[djb2("agent_host_local_loop")].value === false`
     (rule_id `3MgjZ8CeD3wwbjykIVegtc`)。而源码里 `managed-local` 只在 gate 为 true
     且不被判不合格时产生,gate 为 false 的唯一出口是 `{runtime:"connect",reason:"gate-off"}`。
     ⇒ 他那台 gate 为 on,本机为 off,差异点就在这里。

   ⚠️ **这条补丁不保证他就好了,只保证不再打到 Cursor 自家推理**。他那一轮
   `hasModelCredentials` 必然是 false(否则会走 `fail` 而不是 managed-local),也就是说
   modelDetails 上**没有挂 apiKey**。`credentials` 这个 oneof 只有一个来源:
   `convertModelDetailsToRequestedModelCredentials` 里 `e.apiKey ? apiKeyCredentials : {case:void 0}`,
   而 `getModelDetailsFromName` 里 `(!i||!n)&&(n=void 0)` —— 开关关着或库里没 Key 都会把它抹掉。
   所以摘掉 local loop 之后他会退回到「连 Cursor 后端」那条老路:Key 真在库里就通,
   Key 不在就会看到 Free 档那句话。**哪一种,得看他的 DOCTOR 输出**(第 4 节新增了这一格)。
   在拿到之前不许说"修好了"。

   为什么不用 Cursor 自带的 feature flag override(`_featureFlagOverrides`):
   `_canUseOverrides()` 在正式构建里要求 `isDevUser` 这个 contextKey 或服务端下发的
   dev 资格,普通账号拿不到;而那份资格缓存会被服务端刷新覆盖 ⇒ 覆盖掉之后 override
   静默失效、症状原样回来。补丁在我们自己的 REPAIR/UPGRADE 里可重放,是可控的那一半。 */
const NOLOOP_MARKER = "@cx-noloop:v1";
const NOLOOP_TARGETS = [
  { rel: path.join("extensions", "cursor-agent-host", "dist", "main.js"), kind: "min" },
  { rel: path.join("extensions", "cursor-agent-host", "dist", "agent-host-daemon", "dist", "bin", "daemon.cjs"), kind: "src" },
];
// 🔴 名字位一律 ID 捕获 —— 压缩版里 `Ps/Js/Ms/e/t/Rs` 全是会摇的名字。锚点靠两个
//    **字符串字面量**钉死语义:gate 名 `agent_host_local_loop` 与 logger 名
//    `@anysphere/agent-host:local-loop-turn-router`,它们是源码里的常量,不参与混淆。
// 🔴 2026-10-12(3.24.12):**降级目标变了** —— 上游把 `__awaiter`+生成器那套转译产物
//    换成了**原生 async 箭头**:`function h(e){return async t=>{`(min)、
//    `function createLocalLoopTurnRouter(options2) { return async (runOptions) => {`(src)。
//    语义一字未改,只是不再经 TS 的 downlevel 转译 ⇒ 写死 `(this,void 0,void 0,function*(){`
//    的锚点必然 count=0。两代都认:生成器那代保留,新增原生 async 那代。
//    ⚠️ **插入点对两代是同一个**(函数体第一句 `return` 我们的对象),所以 NL_RET 不用分叉:
//    生成器那代靠 `__awaiter` 拿 {done:true,value} 直接 resolve;async 那代直接 return。
//    🔴 这处是**离线台架漏掉的第三断**:它和件C 一样「不阻断、只告警跳过」,而台架腿⑦
//    喂的是 ~/.cursor-ctxwin-backup 里的**旧版**快照 ⇒ 新版形状从来没进过门禁。
//    教训与 09-23 件C 那次逐字相同:不阻断的补丁必须有一腿量**新版** app。
// 🔴 同一轮还踩到第二层:原来锚点要求 gate 名 `agent_host_local_loop` 与 logger 名
//    **相距 80 字以内**。3.24.12 上游在两者之间插了代码 ⇒ 实测距离 **826 字**,
//    光放宽 async 那一代仍然 count=0。诊断手法 = 把正则**逐段加压**数命中,
//    发现连第一段(两个字面量)就已经是 0,才看出问题不在 async 那半。
//    ⇒ 去掉 gate 名这个联合条件:**logger 名 `local-loop-turn-router` 全文只出现 1 次**
//    (实测 min=1 处 / daemon.cjs=3 处但只有一处紧跟函数定义),它自己就足够钉死语义,
//    而"两个字面量必须挨得近"是对**排版**的假设,不是对语义的 —— 排版每版都会动。
const NL_MIN = new RegExp('"@anysphere/agent-host:local-loop-turn-router"\\);'
  + 'function (' + ID + ')\\((' + ID + ')\\)\\{return (?:'
  + '(' + ID + ')=>(' + ID + ')\\(this,void 0,void 0,function\\*\\(\\)\\{'   // ≤3.21:转译产物
  + '|async (' + ID + ')=>\\{'                                               // 3.24.12 起:原生 async
  + ')', "g");
// 未压缩那份(daemon.cjs)esbuild 保住了真名,但 `options2`/`__awaiter73` 这种后缀会随
// 打包结果变 ⇒ 同样只钉函数名,参数名/helper 名全用 ID 捕获。
const NL_SRC = new RegExp('function\\s+createLocalLoopTurnRouter\\((' + ID + ')\\)\\s*\\{\\s*'
  + 'return\\s*(?:'
  + '\\((' + ID + ')\\)\\s*=>\\s*(' + ID + ')\\(this,\\s*void 0,\\s*void 0,\\s*function\\*\\s*\\(\\)\\s*\\{'
  + '|async\\s*\\(?(' + ID + ')\\)?\\s*=>\\s*\\{'      // 3.27.12 起单参数不带括号:`async runOptions=>{`
  + ')', "g");
// reason 用我们自己的字面量而不是照抄 "gate-off":日志里要能一眼看出这是**我们**摘的,
// 而不是服务端本来就没开。把自己的动作伪装成环境的原样是给下一轮诊断埋雷。
const NL_RET = 'return{runtime:"connect",reason:"cxteam-local-loop-off"};';

// 纯函数:把件D 打进一段文本。锚点不是 exactly-1 ⇒ {ok:false},调用方整条跳过(不硬来)。
function noloopApplyToText(src, kind) {
  if (src.includes(NOLOOP_MARKER)) return { ok: false, why: "已打过", skip: true };
  const re = kind === "min" ? NL_MIN : NL_SRC;
  re.lastIndex = 0;
  const m = [...src.matchAll(re)];
  if (m.length !== 1) return { ok: false, why: "锚点 count=" + m.length };
  // 生成器体的第一句插 return:`Rs/__awaiter` 拿到 {done:true,value:我们的对象} 就直接
  // resolve,后面那些 `var _a;if(...privateInference)` 全成死代码 —— 两条分支(私有推理
  // 那支在 gate 检查**之前**就 return 了)于是一起被盖住。只补 gate 那支会漏掉私有推理那支。
  const out = src.replace(m[0][0], m[0][0] + "/*" + NOLOOP_MARKER + "*/" + NL_RET);
  return { ok: true, out, applied: ["noloop-" + kind] };
}
function noloopBakName(rel) { return "cxnoloop__" + rel.split(path.sep).join("__"); }

function planNoloopBundle(t, baseText) {
  const p = path.join(RES, t.rel);
  if (!fs.existsSync(p)) { console.log("   SKIP noloop %s(不存在)", t.rel); return null; }
  const src = baseText != null ? baseText : fs.readFileSync(p, "utf8");
  const r = noloopApplyToText(src, t.kind);
  if (!r.ok) {
    // 同件C(见 planCtxwinBundle):没打上 ⇒ 这一轮可能打到 Cursor 自家 InferenceService
    // (BYOK 的 key 压根不参与),后果比"慢"更硬。必须进 MISSED,不许收尾只写「✅ 完成」。
    if (!r.skip) {
      console.log("   ⚠️  noloop %s 锚点问题(%s)→ 跳过(不阻断其它补丁)", path.basename(t.rel), r.why);
      MISSED.push({ rel: t.rel, why: "件D(关本地循环)锚点认不出:" + r.why + " —— 没打上可能发不到公司网关" });
    }
    return null;
  }
  console.log("   noloop %s: 将打 [%s]", path.basename(t.rel), r.applied.join(", "));
  return { p, out: r.out, bakName: noloopBakName(t.rel), applied: r.applied };
}

// 测试钩子:对任意文件跑真实件D,写出到 CX_NOLOOP_OUT,供离线台架断言。
// CX_NOLOOP_KIND=min|src。退出码 0=打上 / 17=锚点不满足。
if (process.env.CX_NOLOOP_APPLY_TO_FILE) {
  const f = process.env.CX_NOLOOP_APPLY_TO_FILE;
  const r = noloopApplyToText(fs.readFileSync(f, "utf8"), process.env.CX_NOLOOP_KIND || "min");
  if (!r.ok) { console.error("noloop 打不上:" + r.why); process.exit(17); }
  fs.writeFileSync(process.env.CX_NOLOOP_OUT || (f + ".nlout"), r.out);
  console.error("noloop applied: " + r.applied.join(", "));
  process.exit(0);
}

// 测试钩子:对任意 exthost bundle 跑「装 → 摘」往返,断言逐字节回到原样。
// 这是摘除逻辑唯一可信的判据——只看"没报错"会放行留半截的逆操作。
// 退出码:0=往返逐字节相同 / 14=装不上(锚点) / 15=摘失败 / 16=往返有差异。
if (process.env.CX_CHAIN_ROUNDTRIP) {
  const f = process.env.CX_CHAIN_ROUNDTRIP;
  const orig = fs.readFileSync(f, "utf8");
  const wb = wrapBuilderCall(orig);
  if (!wb.ok) { console.error("装不上:" + wb.why); process.exit(14); }
  let patched = wb.out;
  const rOld = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:t.fetch})";
  const rNew = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:(globalThis.__cxWrap||(f=>f))(t.fetch)})";
  if (patched.split(rOld).length - 1 === 1) patched = patched.replace(rOld, rNew);
  const ufOld = '?"responses":"chat_completions"', ufNew = '?"responses":"responses"';
  if (patched.split(ufOld).length - 1 === 1) patched = patched.replace(ufOld, ufNew);
  patched = CHAIN_SHIM + "\n" + patched;
  const back = chainRemoveFromSource(patched);
  if (!back.ok) { console.error("摘失败:" + back.why); process.exit(15); }
  if (back.out !== orig) {
    let i = 0; while (i < Math.min(back.out.length, orig.length) && back.out[i] === orig[i]) i++;
    console.error("往返有差异 @%d: orig=%j back=%j (len %d vs %d)",
      i, orig.slice(i, i + 60), back.out.slice(i, i + 60), orig.length, back.out.length);
    process.exit(16);
  }
  console.log("ROUNDTRIP OK: %s (%d chars, 摘除 [%s])", path.basename(f), orig.length, back.removed.join(", "));
  process.exit(0);
}

/* ── 测试钩子:打印常量供与 Python 版做逐字节等价断言 ── */
if (process.env.CX_DUMP_CONSTANTS) {
  process.stdout.write(JSON.stringify({ GATE_FN, QP_SNIPPET, QP_OLD, DEFAULT_BASE_URL, DEFAULT_MODEL, RETIRED_MODELS }));
  process.exit(0);
}
// 测试钩子:对任意文件跑真实 PATCHES,写出到 CX_APPLY_OUT,供与 Python 版做逐字节等价断言。
// 走的是 applyPatchesToText —— 与 --apply/--repair 同一份语义(marker 跳过 / 泵原地升级 / multi),
// 所以能直接喂真 bundle(旧版只认"每锚点恰好 1 次",norelay 是 multi → 在真 bundle 上必假红)。
if (process.env.CX_APPLY_TO_FILE) {
  const t = applyPatchesToText(fs.readFileSync(process.env.CX_APPLY_TO_FILE, "utf8"), true);
  fs.writeFileSync(process.env.CX_APPLY_OUT || (process.env.CX_APPLY_TO_FILE + ".jsout"), t.out);
  console.error("applied: " + (t.applied.join(", ") || "(无,全已打过)"));
  process.exit(0);
}
// 测试钩子:对任意 exthost bundle 跑真实 chain 补丁,写出到 CX_CHAIN_APPLY_OUT,供与 cursor_chain_patch.py 逐字节等价断言
if (process.env.CX_CHAIN_APPLY_TO_FILE) {
  const src = fs.readFileSync(process.env.CX_CHAIN_APPLY_TO_FILE, "utf8");
  const wb = wrapBuilderCall(src);
  if (!wb.ok) { console.error("chain wrap 失败:" + wb.why); process.exit(12); }
  let out = wb.out;
  const rOld = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:t.fetch})";
  const rNew = "baseURL:t.baseUrl,apiKey:t.apiKey,fetch:(globalThis.__cxWrap||(f=>f))(t.fetch)})";
  if (out.split(rOld).length - 1 === 1) out = out.replace(rOld, rNew);
  const ufOld = '?"responses":"chat_completions"', ufNew = '?"responses":"responses"';
  if (out.split(ufOld).length - 1 === 1) out = out.replace(ufOld, ufNew);
  out = CHAIN_SHIM + "\n" + out;
  fs.writeFileSync(process.env.CX_CHAIN_APPLY_OUT || (process.env.CX_CHAIN_APPLY_TO_FILE + ".jsout"), out);
  process.exit(0);
}

/* ── 工具函数 ── */
function cursorVersion() {
  try { return JSON.parse(fs.readFileSync(path.join(RES, "package.json"), "utf8")).version || "unknown"; }
  catch (e) { return "unknown"; }
}

function cursorRunning() {
  // 本脚本自己就跑在 Cursor 二进制上,必须排除自身 pid。
  // 2026-09-03 再踩一次:zk-delta 小代理也是借 Cursor 二进制当 node 跑的常驻进程
  // (`…/MacOS/Cursor ~/.zk-delta/sidecar/sidecar.js`),第一次 INSTALL 装上它之后,
  // 第二次跑安装器 pgrep 一抓就把它当成 Cursor 主程序 → "Cursor 正在运行"假阳,
  // 用户明明已经 ⌘Q 了。所以只认**命令行里不带 .js 脚本参数**的进程 = GUI 主程序。
  if (process.platform === "win32") {
    const r = spawnSync("wmic", ["process", "where", "name='Cursor.exe'", "get", "ProcessId,CommandLine", "/FORMAT:CSV"], { encoding: "utf8" });
    if (r.status === 0 && r.stdout) {
      return r.stdout.split(/\r?\n/).some((line) => {
        const cols = line.split(","); if (cols.length < 3) return false;
        const pid = Number(cols[cols.length - 1]), cmd = cols.slice(1, -1).join(",");
        return pid && pid !== process.pid && !/\.js(\s|"|$)/i.test(cmd);
      });
    }
    const t = spawnSync("tasklist", ["/FI", "IMAGENAME eq Cursor.exe", "/FO", "CSV", "/NH"], { encoding: "utf8" });
    if (t.status !== 0 || !t.stdout) return false;
    return t.stdout.split(/\r?\n/).some((line) => { const m = line.match(/^"Cursor\.exe","(\d+)"/i); return m && Number(m[1]) !== process.pid; });
  }
  const pat = process.platform === "darwin" ? "Cursor.app/Contents/MacOS/Cursor" : path.join(path.dirname(EXEC), "cursor");
  const r = spawnSync("pgrep", ["-fl", pat], { encoding: "utf8" });
  if (r.status !== 0 || !r.stdout) return false;
  return r.stdout.split(/\r?\n/).filter(Boolean).some((line) => {
    const m = line.match(/^(\d+)\s+(.*)$/); if (!m) return false;
    const pid = Number(m[1]), cmd = m[2];
    if (pid === process.pid || pid === process.ppid) return false;
    if (!isCursorExe(cmd)) return false;      // 只是参数里提到了 Cursor 路径(比如一条 zsh -c "…Cursor…" 命令)
    return !/\.js(\s|$)/.test(cmd);            // 带 .js 参数 = node 模式(小代理/安装器),不是 GUI
  });
}
/* 10-27:pgrep -f 按整行匹配 ⇒ 一个 `zsh -c "…/Cursor.app/Contents/MacOS/Cursor …"` 的 shell 也被当成 Cursor GUI,
   刷模型 worker 等它「退出」等了 53 秒(实测)。⇒ 只认**可执行文件本身**是 Cursor 的进程(命令行以它开头)。 */
function isCursorExe(cmd) {
  const exe = process.platform === "darwin" ? "/Contents/MacOS/Cursor" : path.join(path.dirname(EXEC), "cursor");
  const first = String(cmd).trim();
  return first.startsWith(exe) || new RegExp("^\\S*" + exe.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&") + "(\\s|$)").test(first);
}

function syntaxCheck(text, tag) {
  const p = path.join(os.tmpdir(), "cxteam_check_" + tag + ".js");
  fs.writeFileSync(p, text);
  const r = spawnSync(EXEC, ["--check", p], { encoding: "utf8", env: { ...process.env, ELECTRON_RUN_AS_NODE: "1" } });
  fs.unlinkSync(p);
  if (r.status !== 0) { console.log("   !! 语法校验失败(%s):%s", tag, String(r.stderr).slice(0, 300)); return false; }
  return true;
}

function countMatches(rx, text) { rx.lastIndex = 0; let n = 0; while (rx.exec(text)) n++; rx.lastIndex = 0; return n; }
function subOnce(rx, text, fn) {
  rx.lastIndex = 0;
  const m = rx.exec(text); rx.lastIndex = 0;
  if (!m) return text;
  return text.slice(0, m.index) + fn(m) + text.slice(m.index + m[0].length);
}

/* 把 PATCHES 打进一段文本。planBundle 与测试钩子 CX_APPLY_TO_FILE **共用这一份**——
   钩子跑的必须就是同事机器上真跑的语义,否则回归台架是假绿。quiet=只写不打印。 */
/* AST 定位一整份文本:一次解析、跑全部 locator、按 start 倒序切片替换(倒序=前面的
   区间不被后面的替换挪位)。返回 null 表示这条路走不通(没有 acorn / 解析失败),
   调用方退回正则——退回不降低安全阀,正则路径的「命中 !=1 就拒绝」照样跑。 */
function planByAst(src) {
  if (process.env.CX_FORCE_REGEX) return null; // 回归台架用它取正则基线做逐字节对照
  const acorn = loadAcorn();
  if (!acorn) return null;
  let ast;
  try { ast = acorn.parse(src, { ecmaVersion: "latest", sourceType: "module" }); }
  catch (e) { return { err: "解析失败:" + e.message }; }
  const sites = {};
  for (const [name, fn] of Object.entries(LOCATORS)) {
    try { sites[name] = fn(ast, src); }
    catch (e) { return { err: name + " locator 抛错:" + e.message }; }
  }
  return { sites };
}

/* 🔴 锚点认不出时「拒绝动手」的**范围**:只该拒绝这一个文件,不该拒绝整次安装。
   数据(09-24 台架 ⑩,可复现):拿 3.21.18 的 pristine 造假 app,只把 workbench.desktop
   里 byok-keepon 那**一个**锚点改漂移(命中 1→0),跑 --repair ——
     对照(不漂移):workbench ×2 + ctxwin ×4 + noloop ×2 全打上;
     漂移后      :第一行就 `!! byok-keepon 锚点命中=0 → 拒绝动手` 然后 process.exit(2),
                   agent-host 上 @cx-ctxwin:v3 / @cx-noloop:v1 计数**都是 0**。
   件C(8146%/每轮压缩)和件D(InferenceService 报错)打的是 agent-host 那几个**完全不同
   的文件**,锚点好好的,却因为 workbench 里另一条腿漂了而一个都没打上。Cursor 每次自动
   升级都可能漂一个锚点 ⇒ 这就是"补丁一个字没改,同事那边却突然整片失效"的形状。
   现在:planBundle 捕获 → 这个文件整份跳过(一个字节不动)→ 别的家族照常打 → 结尾大声报红 + 非 0 退出。
   ⚠️ 测试钩子(CX_APPLY_TO_FILE)是直调 applyPatchesToText,不开软失败 ⇒ 台架原来靠
      exit 2 断言锚点的那几腿语义**一个字没变**。 */
// var 不是 let:测试钩子 CX_APPLY_TO_FILE 在文件更靠前的位置就调 applyPatchesToText,锚点一 miss 走到 patchRefuse
// 会碰到这里 —— let 在声明之前读是 TDZ 报错(10-26 3.27.12 台架③腿 ReferenceError),掩盖真正的「哪个锚点没认出」。
var SOFT_MISS = false;
var MISSED = [];
function patchRefuse(msg) {
  if (SOFT_MISS) { const e = new Error(msg); e.cxRefuse = true; throw e; }
  console.log(msg);
  process.exit(2);
}
function reportMissed() {
  if (!MISSED.length) return false;
  console.log("\n❌ 有 %d 个文件的锚点认不出,**这部分补丁没打上**(那几个文件一个字节没动):", MISSED.length);
  for (const m of MISSED) console.log("   · %s —— %s", path.basename(m.rel), m.why.replace(/^\s*!!\s*/, "").trim());
  console.log("   通常是 Cursor 升级后这段代码改了,需要出新版锚点。请把上面整屏发给管理员。");
  console.log("   其余文件的补丁**已经打上了**,Cursor 照常能用,只是上面这几条没生效。");
  return true;
}

function applyPatchesToText(src, quiet) {
  const say = (...a) => { if (!quiet) console.log(...a); };
  let out = src; const applied = [];

  /* AST 定位**只解析一次**,全部区间都是相对 src 的偏移。
     所以一旦有任何别的改动落到 out 上(queue-pump 旧版升级的字符串替换、或某个补丁
     退回正则),这些偏移就失效了 → 在那之前必须先 flush。flushAst 按 start 倒序落地,
     倒序保证前面的区间不被后面的替换挪位。 */
  const astEdits = [];
  const flushAst = () => {
    if (!astEdits.length) return;
    astEdits.sort((a, b) => b.start - a.start);
    for (const s of astEdits) out = out.slice(0, s.start) + s.make() + out.slice(s.end);
    astEdits.length = 0;
  };

  const plan = planByAst(src);
  // 退回正则必须**大声**:静默退回会让回归台架把"走了正则"读成"AST 通过了"= 假绿。
  // CX_REQUIRE_AST=1 时不许退回(回归台架用它锁死这一腿真的在跑 AST)。
  if (plan && plan.err) console.log("   ⚠️  AST 定位不可用(%s)→ 退回正则锚点", plan.err);
  else if (!plan) console.log("   ⚠️  没找到 acorn(RES=%s)→ 退回正则锚点", RES);
  const useAst = !!(plan && plan.sites);
  if (!useAst && process.env.CX_REQUIRE_AST) {
    console.log("   !! CX_REQUIRE_AST=1 但 AST 定位没跑起来 → 拒绝(不许静默退回正则)");
    process.exit(3);
  }

  for (const pt of PATCHES) {
    // 幂等判据看 out 就够:astEdits 里待落地的都是本轮新加的 marker,
    // 每个补丁的 marker 互不相同,不会自己把自己判成"已打过"。
    if (out.includes(pt.marker)) { say("   SKIP %s(已打过)", pt.name.padEnd(11)); continue; }
    if (pt.name === "keepmine-fill") {
      // v1 → v2 原地升级(10-16 v1 只补缺的名字、不带档位;v2 用档位模板补档位)
      const m1 = KEEPFILL_V1_RX.exec(out); KEEPFILL_V1_RX.lastIndex = 0;
      if (m1) {
        flushAst();
        out = out.slice(0, m1.index) + KEEPFILL(m1[1]) + out.slice(m1.index + m1[0].length);
        applied.push("keepmine-fill(升级)"); continue;
      }
    }
    if (pt.name === "queue-pump") {
      const old = QP_OLD.find((s) => out.includes(s));
      if (old) {
        if (out.split(old).length - 1 !== 1) patchRefuse("   !! queue-pump 旧版命中!=1 → 拒绝");
        flushAst(); // 这一步要改 out → 先把待落地的 AST 区间落完,否则后者偏移失效
        out = out.replace(old, QP_SNIPPET); applied.push("queue-pump(升级)"); continue;
      }
    }

    if (useAst && plan.sites[pt.name]) {
      // 收集不改文本(改动统一在循环后按 start 倒序一次性落地)——这样只解析一次:
      // 逐个补丁改完再重新解析要 8 次 × ~7s ≈ 67s/bundle,实测过,太贵。
      const hits = plan.sites[pt.name].length;
      if (pt.multi) {
        if (hits < 1) patchRefuse("   !! " + pt.name.padEnd(11) + " AST 定位命中=0 → 拒绝动手");
      } else if (hits !== 1) {
        patchRefuse("   !! " + pt.name.padEnd(11) + " AST 定位命中=" + hits + " != 1 → 拒绝动手(版本不匹配或已被 CursorX 改写)");
      }
      for (const s of plan.sites[pt.name]) astEdits.push(s);
      applied.push(pt.name + (pt.multi ? "(x" + hits + ")" : ""));
      continue;
    }
    if (useAst) say("   ⚠️  %s 没有 AST locator → 用正则", pt.name.padEnd(11));

    flushAst(); // 正则分支要改 out → 先落地待办的 AST 区间
    const hits = countMatches(pt.rx, out);
    if (pt.multi) {
      if (hits < 1) patchRefuse("   !! " + pt.name.padEnd(11) + " 锚点命中=0 → 拒绝动手");
      out = out.replace(pt.rx, (...a) => pt.sub(a)); // sub 吃 exec 风格数组(a[1]=组1)
      applied.push(pt.name + "(x" + hits + ")"); continue;
    }
    if (hits !== 1) {
      patchRefuse("   !! " + pt.name.padEnd(11) + " 锚点命中=" + hits + " != 1 → 拒绝动手(版本不匹配或已被 CursorX 改写)");
    }
    out = subOnce(pt.rx, out, pt.sub);
    applied.push(pt.name);
  }
  flushAst();
  return { out, applied };
}

function planBundle(rel) {
  const p = path.join(RES, rel);
  const src = fs.readFileSync(p, "utf8");
  let r;
  SOFT_MISS = true;
  try { r = applyPatchesToText(src, false); }
  catch (e) {
    if (!e.cxRefuse) throw e;
    console.log(e.message);
    console.log("   ↳ %s 整份跳过(一个字节没动),其余补丁家族继续打。", path.basename(rel));
    MISSED.push({ rel, why: e.message });
    return null;
  }
  finally { SOFT_MISS = false; }
  const { out, applied } = r;
  if (out === src) { console.log("   %s: 全部已打过,跳过", path.basename(rel)); return null; }
  console.log("   %s: 将打 [%s]", path.basename(rel), applied.join(", "));
  return { p, out };
}

/* ── SQLite:优先 node:sqlite,备胎 Cursor 自带 @vscode/sqlite3 ──
   set 用 UPSERT(INSERT OR REPLACE)不用 UPDATE:secret://cursorAuth/openAIKey 这行在
   从没填过 key 的新用户机器上不存在,UPDATE 会 0 行静默成功=假绿(printed 写了其实没写)。 */
function openDb() {
  /* 10-26(Linux 实测,mac 同样复现):Cursor 从没打开过 ⇒ state.vscdb 不存在 ⇒ node:sqlite 抛错落到备胎
     @vscode/sqlite3,它在打不开的库上跑查询时**原生层直接 abort**(`terminating due to uncaught exception
     of type Napi::Error`,exit 134,core dump),try/catch 接不住,屏幕上停在「--- 2.8) 模型菜单」什么都不说。
     ⇒ 先看文件在不在,不在就抛一个普通错误,调用方各自给人话提示。 */
  if (!fs.existsSync(STATE_DB)) {
    const e = new Error("找不到 Cursor 的设置库 " + STATE_DB + "(Cursor 还没打开过?先打开一次 Cursor 并登录,再完全退出后重跑)");
    e.cxNoDb = true; throw e;
  }
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(STATE_DB);
    return {
      get: (k) => { const r = db.prepare("SELECT value FROM ItemTable WHERE key=?").get(k); return r ? r.value : null; },
      set: (k, v) => db.prepare("INSERT INTO ItemTable(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(k, v),
      close: () => db.close(),
    };
  } catch (e) { /* 落到备胎 */ }
  const sqlite3 = require(path.join(RES, "node_modules", "@vscode", "sqlite3"));
  const db = new sqlite3.Database(STATE_DB);
  const call = (fn, sql, args) => new Promise((res, rej) => db[fn](sql, args, function (err, row) { err ? rej(err) : res(row); }));
  return {
    get: (k) => call("get", "SELECT value FROM ItemTable WHERE key=?", [k]).then((r) => (r ? r.value : null)),
    set: (k, v) => call("run", "INSERT INTO ItemTable(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [k, v]),
    close: () => db.close(),
  };
}

async function mergeConfig(args, dry) {
  let db;
  try { db = openDb(); }
  catch (e) { if (!e.cxNoDb) throw e; console.log("   !! " + e.message); process.exit(3); }
  const raw = await db.get(APP_USER_KEY);
  if (!raw) { console.log("   !! applicationUser blob 不存在(Cursor 没初始化过?)"); db.close(); process.exit(3); }
  const d = JSON.parse(raw);
  const before = { baseUrl: d.openAIBaseUrl, useKey: d.useOpenAIKey };
  d.openAIBaseUrl = args.baseUrl;
  d.useOpenAIKey = true;
  const ai = (d.aiSettings = d.aiSettings || {});
  // --zk-delta-only 连模型清单都不加：那台机器要么已经装好了，要么就是我的基准机，
  // 这一趟的唯一目的是把网络路径换成小代理。少动一样东西，少一个变量。
  const wantModels = args.zkDeltaOnly ? [] : args.models;
  /* ── 2026-09-20 第二轮:整表赋值(EXACT)──────────────────────────────────────
     原来 dedup 是**纯并集**:只加不减。后果是同事库里会越堆越多 —— 历史上装过的
     旧代名字(`cursor-g-*`/`cr-g-*-135`)、他自己手工加的第三方名、Cursor 自带名
     全留着(本机实测 userAddedModels 52 条、modelOverrideEnabled 50 条),菜单一拉
     一屏找不到要用的那个,而且"文档写了什么"和"他菜单里有什么"永久性地对不上。
     用户点名:**除文档里那份之外全部去掉,包括他自己配的**。
     所以这里改成整表赋值:两个数组直接等于本次菜单(10-19 起 = 这把 Key 现拉的列表),顺序照抄
     (菜单顺序 = 数组顺序,用户要 Grok 系列排最前)。
     ⚠️ 这是**破坏性**的:他自己加的名字会被删掉。所以 main() 里的备份必须在
        写库**之前**落盘(09-20 第二轮同时修的,见 mergeConfig 调用点),
        `--revert` 能从 applicationUser.blob.json 整份还回去。
     ⚠️ `--zk-delta-only` 例外:那一趟的唯一目的是换网络路径,wantModels 为空,
        绝不能把人家的清单清空 —— 下面 EXACT 只在 wantModels 非空时生效。 */
  const exact = !args.zkDeltaOnly && wantModels.length > 0;
  // 选中位 re-point 用:不在新清单里的名字 = 菜单里已经没有它了。
  // (RETIRED_MODELS 仍登记显式下架名,便于报告里点出来;但判据是"在不在新清单里"。)
  const wantSet = new Set(wantModels);
  const isRetired = (x) =>
    typeof x === "string" && exact && !wantSet.has(x);
  const uamBefore = [...(ai.userAddedModels || [])];
  const oveBefore = [...(ai.modelOverrideEnabled || [])];
  const ovdBefore = [...(ai.modelOverrideDisabled || [])];
  if (exact) {
    // 整表赋值:逐字、按序、全量。dedup 只防常量里自己写重了。
    const seen = new Set(), outl = [];
    for (const x of wantModels) { if (!seen.has(x)) { seen.add(x); outl.push(x); } }
    ai.userAddedModels = [...outl];
    ai.modelOverrideEnabled = [...outl];
    /* ── 2026-09-21 开关必须是"装上就开"，不让同事去点 ──────────────────────
       `modelOverrideEnabled` 只是"开"的那一半。Cursor 另有一张**显式关闭**表
       `modelOverrideDisabled`，两张表同时出现同一个名字时以"关"为准 ——
       后果专打**升级路径**：同事之前手动关过某个名字，装机器把它写进 Enabled，
       但 Disabled 里那条还在，开关看起来是灰的，他得自己找到再点一次。
       本机初装 `modelOverrideDisabled` 是空数组，所以这个洞在新机器上**零症状**，
       只在"关过再升级"的机器上出现 —— 这正是它一直没被发现的原因。
       处置：把本方案的名字从 Disabled 里整个剔掉；**不碰**他自己关的其它名字
       （那是他的选择，EXACT 语义清的是清单，不是他的开关偏好）。 */
    ai.modelOverrideDisabled = ovdBefore.filter((x) => !seen.has(x));
    /* ── 2026-10-14 菜单里冒出 Cursor 自家的 claude-fable-5-1 / gemini-4-argon / muse-spark-1.3 ──
       Cursor 的显示规则(workbench 原文):`enabled.includes(n) || (!disabled.includes(n) && defaultOn)`。
       服务端目录里 defaultOn=true 的自家模型,我们没登记也会显示 ⇒ 同事一点就走 Cursor 自家推理
       (免费号报 "Free plans can only use Auto" / "does not support custom API keys")。
       ⇒ 把目录里 defaultOn、又不在本方案清单里的名字加进 Disabled(`default`=Auto 留着)。
       目录随服务端变,UPGRADE/INSTALL 每次都按当时的目录重算;UNINSTALL 不还原这项(只是隐藏)。 */
    const cat = Array.isArray(d.availableDefaultModels2) ? d.availableDefaultModels2 : [];
    const hide = cat.filter((m) => m && m.defaultOn && m.name && m.name !== "default" && !seen.has(m.name)).map((m) => m.name);
    for (const n of hide) if (!ai.modelOverrideDisabled.includes(n)) ai.modelOverrideDisabled.push(n);
    if (hide.length) console.log("   隐藏 Cursor 自带模型 %d 个(避免点到走不了我们 key 的模型):%s", hide.length, hide.join(", "));
  } else {
    // --zk-delta-only:一个字不动。
    ai.userAddedModels = uamBefore;
    ai.modelOverrideEnabled = oveBefore;
    ai.modelOverrideDisabled = ovdBefore;
  }
  const unmuted = ovdBefore.filter((x) => wantSet.has(x));
  const added = wantModels.filter((m) => !uamBefore.includes(m));
  const removed = exact
    ? [...new Set([...uamBefore, ...oveBefore])].filter((m) => !wantSet.has(m))
    : [];
  // #3 默认模型:装完直接选中 DEFAULT_MODEL,用户不用在菜单里挑。
  // 保守——仅当当前选中的不是本方案的名字(MODEL_PREFIXES)时才设,不覆盖用户自己已选的。
  // --keep-model / --zk-delta-only：一个字都不动选中模型。
  //   为什么要有这条：这台机器上 composer 可能正钉着某个基准名(量具)，
  //   而这个函数的默认分支会把它换成 DEFAULT_MODEL。
  //   拿安装器当 zk-delta 的开关用 = 顺手换掉量具，测出来的东西就不是同一个东西了。
  const mc = (ai.modelConfig = ai.modelConfig || {});
  /* 退役名如果正被某个功能位选中,光从 userAddedModels 里摘掉是不够的:菜单里没了、
     composer 却还钉着它 —— 同事一发消息就报错,而且他在菜单里找不到那个名字、
     不知道该怎么换回来。所以**按值扫全部功能位**(不只 composer/cmd-k:实测本机
     modelConfig 有 8 个键,同事可能在 deep-search / plan-execution 里也挑了它),
     命中就改回 DEFAULT_MODEL。这一条**连 --keep-model 也要做** —— 那个开关是
     "别动我钉的量具",而钉在一个已经不存在的名字上不是量具,是坏的。 */
  const repointed = [];
  for (const feat of Object.keys(mc)) {
    const e = mc[feat];
    if (!e || typeof e !== "object") continue;
    const selRetired = Array.isArray(e.selectedModels) && e.selectedModels.some((s) => s && isRetired(s.modelId));
    if (!isRetired(e.modelName) && !selRetired) continue;
    const was = e.modelName;
    e.modelName = ACTIVE_DEFAULT_MODEL;
    e.selectedModels = [{ modelId: ACTIVE_DEFAULT_MODEL, parameters: [] }];
    repointed.push(feat + ":" + (was || "?") + "→" + ACTIVE_DEFAULT_MODEL);
  }
  const curName = mc.composer && mc.composer.modelName;
  const alreadyOurs = isOurs(curName);
  let defModelSet = "(skip, 已是 " + (curName || "?") + ")";
  if (args.keepModel) {
    defModelSet = "(skip, --keep-model：保持 " + (curName || "?") + " 不动)";
  } else if (!alreadyOurs) {
    for (const feat of ["composer", "cmd-k"]) {
      mc[feat] = { ...(mc[feat] || {}), modelName: ACTIVE_DEFAULT_MODEL, selectedModels: [{ modelId: ACTIVE_DEFAULT_MODEL, parameters: [] }] };
    }
    defModelSet = ACTIVE_DEFAULT_MODEL;
  }
  console.log("   config diff: baseUrl %j -> %j ; useOpenAIKey %j -> true ; defaultModel -> %s",
    before.baseUrl, args.baseUrl, before.useKey, defModelSet);
  if (exact) {
    console.log("   模型清单 = 本方案那 %d 个(整表赋值,顺序即菜单顺序):%d 条 -> %d 条",
      wantModels.length, uamBefore.length, ai.userAddedModels.length);
    if (added.length) console.log("     + 新增 %d:%s", added.length, added.join(","));
    /* 删掉了什么必须**逐个列出来**,不能只报个数字:这一步是破坏性的,
       同事(和我自己)要能一眼看出"我原来那个 xxx 是不是被删了"。 */
    if (removed.length) console.log("     - 移除 %d:%s", removed.length, removed.join(","));
    if (!added.length && !removed.length) console.log("     (已经一致,无变化)");
    if (unmuted.length) console.log("     ✔ 已自动打开(原先被显式关闭):%s", unmuted.join(","));
    /* 指路,不是免责声明:这 5 个名字**不会**出现在"自定义模型"区(Cursor 对同名的处置),
       开关已经替他打开了,但他仍然可能在错误的地方找。所以把位置直接印出来。 */
    const collided = wantModels.filter((m) => BUILTIN_COLLIDING.includes(m));
    if (collided.length) {
      console.log("     ℹ 下面 %d 个与 Cursor 自带模型同名,开关已自动打开,但它们显示在", collided.length);
      console.log("       Settings → Models **上面那段「自带模型」列表**里,不在「自定义模型」区:");
      console.log("       %s", collided.join(", "));
    }
    if (repointed.length) console.log("     选中位改回:[%s]", repointed.join(", "));
  } else {
    console.log("   模型清单:一个字不动(%s)", args.zkDeltaOnly ? "--zk-delta-only" : (args.menuWhy || "没拉到列表"));
  }
  /* ── 备份必须在写库**之前** ────────────────────────────────────────────────
     2026-09-20 第二轮发现的时序洞:旧代码把 `raw` 返回给 main(),由 main() 在
     **写库之后**才落盘备份(doBackup 在 mergeConfig 调用点的下一行)。
     并集语义下这个窗口无所谓(丢了也能重建);改成整表赋值之后不行了 ——
     这一步会删掉同事自己加的名字,窗口里崩一次(或 bundle 写盘失败提前 exit)
     旧清单就只存在过内存里,永久丢失,`--revert` 也无从还原。
     所以:写库前先把旧 blob 落盘,拿到路径再写。main() 里那次 doBackup 照旧
     (它还要备份 bundle/settings/Key),两者不冲突 —— 这里只保证**不可逆那一步
     的正前方**有一份快照。 */
  if (!dry) {
    const pre = backupBlobBeforeWrite(raw);
    if (pre) console.log("   ✔ 改配置前已备份旧清单 -> %s", pre);
    await db.set(APP_USER_KEY, JSON.stringify(d));
    if (exact) { try { writeMenuLedger(ai.userAddedModels); } catch (e) { /* 账本写不了不影响菜单 */ } }
  }
  db.close();
  return raw; // 旧 blob 用于备份
}

/* 写库前的单点快照:**只**存 applicationUser blob(Key 这一步根本不碰,不需要).
   独立成目录 `<ver>-<ts>-preconfig`,--revert 的 listBackupDirs 能看到它
   (bakHasBundle 为 false,所以它只会在"该版本没有含 bundle 的备份"时被选中,
    那种情况下还原 blob 正是我们要的)。
   备份失败 = 直接 exit(4),绝不带着"没有退路"往下写。 */
function backupBlobBeforeWrite(raw) {
  const bdir = path.join(BACKUP_ROOT, `${cursorVersion()}-${ts()}-preconfig`);
  try {
    fs.mkdirSync(bdir, { recursive: true });
    fs.writeFileSync(path.join(bdir, "applicationUser.blob.json"), raw);
  } catch (e) {
    console.log("   !! 改配置前备份失败(%s)——拒绝继续,你的模型清单一个字没动。", e && e.message);
    console.log("      这一步会用本方案那份清单**整表替换**你库里的清单,没有备份不许动手。");
    process.exit(4);
  }
  return bdir;
}

/* 升级锁(2026-10-13 起**默认写**,用户点名「禁止 Cursor 升级」):
   ① `update.mode:"none"` —— Cursor 自动升级会整包换掉 app,所有 bundle 补丁(含 noagenthost)一起没,
      同事那边就又是「This model does not support custom API keys」;
   ② `update.enableWindowsBackgroundUpdates:false` —— Windows 上后台静默装新版那条路;
   ③ `cursor.agentHost.updateMode:"none"` —— Agent Host 自己有一条**独立的**在线更新通道
      (默认 `default` 跟随 ①,显式写死更稳:它若单独被设成 on,会绕开我们打过补丁的那份)。
   只写这三个键,其余设置原样保留;卸载时只删「值正是我们写的」那几个。 */
const UPDATE_LOCK = { "update.mode": "none", "update.enableWindowsBackgroundUpdates": false, "cursor.agentHost.updateMode": "none" };
function setUpdateNone(dry) {
  let s = {};
  if (fs.existsSync(SETTINGS_JSON)) {
    try { s = JSON.parse(fs.readFileSync(SETTINGS_JSON, "utf8") || "{}"); }
    catch (e) { console.log('   !! settings.json 不是纯 JSON(含注释?)→ 跳过自动写,请在 Cursor 设置里把 Update: Mode 改成 none'); return; }
  }
  const diff = Object.entries(UPDATE_LOCK).filter(([k, v]) => s[k] !== v);
  if (!diff.length) { console.log("   已锁定:Cursor 不会自动升级"); return; }
  for (const [k, v] of diff) console.log("   %s %j -> %j", k, s[k] === undefined ? null : s[k], v);
  if (!dry) {
    for (const [k, v] of diff) s[k] = v;
    fs.mkdirSync(path.dirname(SETTINGS_JSON), { recursive: true });
    fs.writeFileSync(SETTINGS_JSON, JSON.stringify(s, null, 2));
    console.log("   ✔ 已锁定:Cursor 不会自动升级(新版本等管理员适配后,用 UPGRADE 一起换)");
  }
}

/* ── #2 写 OpenAI Key 进 Cursor 的加密 secret(mac 实测;win/linux best-effort,自检回退) ──
   Key 存 secret://cursorAuth/openAIKey,值=Electron safeStorage 的 OSCrypt 密文序列化成
   {"type":"Buffer","data":[...]}。mac/linux:主密码在系统钥匙串,keytar 读(本进程=Cursor
   已签名二进制,in-process 读不弹框);密文=v10+AES-128-CBC,iv=16空格,
   key=PBKDF2("saltysalt",mac 1003/linux 1,16,sha1)。
   诚实边界:Windows 的 OSCrypt 是 DPAPI,方案不同且本机无法验证 → 直接回退手填,不冒险写坏。 */
/* 🔴 2026-10-12(3.24.12 真机):**上游把 keytar 的原生模块删了** ——
   `node_modules/keytar/` 只剩 `package.json` + `lib/keytar.js`,而那个 js 第一行就是
   `require('../build/Release/keytar.node')`,`build/` 整个目录没了 ⇒ require 必抛。
   不是打包失误:同 app 里 node-pty / sqlite3 / kerberos 等 10 个原生模块**都还在**,
   只有 keytar 被摘。3.21.18 里还有(`Cursor.app.old321` 可对照)。
   失败路径本来就是安全的(返回 null → 跳过自动写 → 提示手填,不假绿),但有一个
   **连带后果必须说出来**:ZWSP 那个病的修复挂在 `sanitizeKey()` 上,而它**只在自动写
   这条路上被调到** ⇒ 3.24+ 的新装机只能手工粘 Key,从飞书表格复制带进来的零宽字符
   没人替他剥,他会撞 `ByteString ... value of 8203` 且请求一个字节都没出网。
   ⇒ 所以 3.24+ 上提示语必须**显式让他别从表格/聊天里复制**(见 writeOpenAIKey 的提示)。 */
async function deriveOsCryptKey(RES) {
  if (process.platform === "win32") return null;
  let keytar;
  try { keytar = require(path.join(RES, "node_modules", "keytar")); }
  catch (e) { return null; }
  let pw = null;
  try { pw = await keytar.getPassword("Cursor Safe Storage", "Cursor"); } catch (e) { pw = null; }
  if (!pw) return null;
  const iters = process.platform === "darwin" ? 1003 : 1;
  return crypto.pbkdf2Sync(pw, "saltysalt", iters, 16, "sha1");
}
function oscryptEncrypt(key, plaintext) {
  const iv = Buffer.alloc(16, 0x20);
  const c = crypto.createCipheriv("aes-128-cbc", key, iv);
  return Buffer.concat([Buffer.from("v10"), c.update(Buffer.from(plaintext, "utf8")), c.final()]);
}
function oscryptDecrypt(key, enc) {
  const iv = Buffer.alloc(16, 0x20);
  const d = crypto.createDecipheriv("aes-128-cbc", key, iv);
  return Buffer.concat([d.update(enc.slice(3)), d.final()]).toString("utf8");
}
/* ── Key 卫生:不可见字符 ────────────────────────────────────────────────────
   病(2026-09-29 同事实报):Cursor 里
     `Local agent request failed / Cannot convert argument to a ByteString
      because the character at index 32 has a value of 8203 which is greater than 255`
   8203 = U+200B 零宽空格。那是 WHATWG `Headers` 在拼 header 时抛的 ⇒ **请求一个字节都没出网**,
   小代理 / 198 / 模型全都无关,日志里也不会有这一发。飞书 key 表 1374 行 key 全长 25,
   `"Bearer "`(7) + 25 = 32 ⇒ index 32 正好是紧贴 key 末尾的那一格。

   🔴 `trim()` 去不掉零宽字符:`"  ​sk-abc​  ".trim() === "​sk-abc​"`。
      所以"看着没有空格、长度也差不多"完全不能排除,凭肉眼和 length 都判不出来。
   🔴 尺子得看**码位**,不能看渲染:doctor 原来只打密文的 sha256 前 16 位,
      带 ZWSP 的 key 在它眼里与干净 key 同形(都是"存在 len=… sha256_16=…"),一路假绿。 */
const INVIS_CP = {
  0x200b: "ZWSP 零宽空格", 0x200c: "ZWNJ 零宽不连字", 0x200d: "ZWJ 零宽连字",
  0x2060: "WORD JOINER", 0xfeff: "BOM/ZWNBSP", 0x00a0: "NBSP 不换行空格",
  0x3000: "IDEOGRAPHIC SPACE 全角空格", 0x200e: "LRM", 0x200f: "RLM",
};
/* 逐**码位**扫(用 for..of 而不是下标,免得把代理对劈开)。
   报出来的 index 是**字符下标**,与 `Headers` 报错里那个 index 同一把尺子(减去 "Bearer " 的 7)。 */
function scanInvisible(s) {
  const hits = [];
  let i = 0;
  for (const ch of String(s)) {
    const cp = ch.codePointAt(0);
    // 已知的不可见字符,外加"任何 > 255 的码位"—— 后者才是 Headers 真正的判据:
    // 只列白名单会漏掉下次换成别的字符的同形病(全角引号、CJK 等照样抛)。
    if (INVIS_CP[cp] || cp > 255 || cp < 0x20 || cp === 0x7f) {
      hits.push({ index: i, cp, name: INVIS_CP[cp] || (cp > 255 ? "码位 > 255(ByteString 装不下)" : "控制字符") });
    }
    i += ch.length;
  }
  return hits;
}
const cpHex = (cp) => "U+" + cp.toString(16).toUpperCase().padStart(4, "0");
// 剥掉首尾空白 + 全部不可见/越界字符。返回 {clean, stripped:[…]},**剥了什么必须说出来**:
// 静默剥掉等于把"他复制错了"这件事藏起来,下次还会犯。
function sanitizeKey(raw) {
  const before = String(raw).trim();
  const stripped = scanInvisible(before);
  let clean = "";
  for (const ch of before) {
    const cp = ch.codePointAt(0);
    if (INVIS_CP[cp] || cp > 255 || cp < 0x20 || cp === 0x7f) continue;
    clean += ch;
  }
  return { clean: clean.trim(), stripped };
}

// 返回 {ok, reason?, confirmed?}。两道自检:①能解开 Cursor 现有 Key(证明方案与 Cursor 完全一致)
// ②加密回环相等。任一不过 → 不写,回退手填(绝不假绿)。
async function writeOpenAIKey(rawKey, RES, dry) {
  if (process.platform === "win32")
    return { ok: false, reason: "Windows 暂不支持自动写(OSCrypt=DPAPI,未实测),请在 Cursor 里手动粘一次" };
  const key = await deriveOsCryptKey(RES);
  if (!key) {
    // 分清两种成因:keytar 原生模块**被上游摘了**(3.24.12 起) vs 真的读不到钥匙串。
    // 合成一句"keytar 不可用"会让下一个人去查钥匙串权限,查错方向。
    const kt = path.join(RES, "node_modules", "keytar", "build", "Release", "keytar.node");
    if (!fs.existsSync(kt))
      return { ok: false, reason:
        "这版 Cursor 把 keytar 原生模块删了(3.24.12 起实测)⇒ 自动写 Key 这条路没了。" +
        "请在 Cursor 里 Settings → Models 手工粘一次。" +
        "🔴 **全选删掉重新输入,别从飞书表格/聊天/文档里复制** —— 那三个地方最容易带零宽字符," +
        "而手工粘这条路上没有自动剥除,带进去会报 `ByteString ... 8203` 且请求根本不出网。" +
        "粘完可以跑 `--doctor` 让它扫一遍码位。" };
    return { ok: false, reason: "读不到钥匙串主密码(keytar 在,但取不到密码)" };
  }
  /* 落盘前剥不可见字符,并**把剥了什么打出来**。不打=把病藏进密文里,
     下一次他自己在 Cursor 设置框里粘还会再中一次,而屏幕上只写了"✅ Key 已写入"。 */
  const san = sanitizeKey(rawKey);
  if (san.stripped.length) {
    console.log("   ⚠️  这把 Key 里有 %d 个不可见/越界字符,已剥掉(否则 Cursor 拼 header 会抛 ByteString):",
      san.stripped.length);
    for (const h of san.stripped)
      console.log("        字符下标 %d  %s  %s   ← header 里的下标 = %d(\"Bearer \" 占 7)",
        h.index, cpHex(h.cp), h.name, h.index + 7);
    console.log("        清理后 len=%d(原 len=%d) —— 多半是从飞书表格/聊天里复制带进来的",
      san.clean.length, String(rawKey).trim().length);
  }
  if (!san.clean) return { ok: false, reason: "剥掉不可见字符后 Key 是空的 —— 复制到的全是空白,请重新复制" };
  rawKey = san.clean;
  const db = openDb();
  let schemeOk = null; // true=解开现有Key(已确认) / false=解不开(方案不符) / null=无现存Key
  try {
    const cur = await db.get(OPENAI_KEY_SECRET);
    if (cur) {
      const enc = Buffer.from(JSON.parse(cur).data);
      try { oscryptDecrypt(key, enc); schemeOk = true; } catch (e) { schemeOk = false; }
    }
  } catch (e) { /* ignore */ }
  if (schemeOk === false) { db.close(); return { ok: false, reason: "现有 Key 解密失败,方案与本机不符,不冒险覆盖" }; }
  if (schemeOk === null && process.platform !== "darwin") { db.close(); return { ok: false, reason: "非 mac 且无现存 Key 可校验方案,回退手填" }; }
  const enc = oscryptEncrypt(key, rawKey);
  if (oscryptDecrypt(key, enc) !== rawKey) { db.close(); return { ok: false, reason: "加密回环自检不过" }; }
  if (!dry) await db.set(OPENAI_KEY_SECRET, JSON.stringify({ type: "Buffer", data: [...enc] }));
  db.close();
  return { ok: true, confirmed: schemeOk === true };
}
function promptLine(q) {
  return new Promise((res) => {
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    rl.question(q, (a) => { rl.close(); res((a || "").trim()); });
  });
}

function ts() { const d = new Date(), z = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}${z(d.getMonth() + 1)}${z(d.getDate())}-${z(d.getHours())}${z(d.getMinutes())}${z(d.getSeconds())}`; }

async function doBackup(ver, plans, oldBlob) {
  const bdir = path.join(BACKUP_ROOT, `${ver}-${ts()}`);
  fs.mkdirSync(bdir, { recursive: true });
  for (const pl of plans) fs.copyFileSync(pl.p, path.join(bdir, pl.bakName || path.basename(pl.p)));
  if (oldBlob != null) fs.writeFileSync(path.join(bdir, "applicationUser.blob.json"), oldBlob);
  if (fs.existsSync(SETTINGS_JSON)) fs.copyFileSync(SETTINGS_JSON, path.join(bdir, "settings.json"));
  // #2 备份现有 Key secret(若有),便于 --revert 还原
  try { const db = openDb(); const k = await db.get(OPENAI_KEY_SECRET); db.close();
    if (keyPresent(k)) fs.writeFileSync(path.join(bdir, "openAIKey.secret.json"), k); } catch (e) { /* ignore */ }
  console.log("   备份 ->", bdir);
}

/* 列出备份目录。**必须过滤掉非目录**:BACKUP_ROOT 里还躺着历史上手工存的
   `applicationUser-*.json` / `empty-state-draft-*.json` 之类散文件,它们排序在数字版本号之后,
   老代码 `baks[baks.length-1]` 那条兜底会挑到**一个 JSON 文件当备份目录**用(实测本机 7 个)。 */
function listBackupDirs() {
  if (!fs.existsSync(BACKUP_ROOT)) return [];
  return fs.readdirSync(BACKUP_ROOT)
    .filter((d) => { try { return fs.statSync(path.join(BACKUP_ROOT, d)).isDirectory(); } catch (e) { return false; } })
    .sort();
}
const bakHasBundle = (d) =>
  BUNDLES.some((rel) => fs.existsSync(path.join(BACKUP_ROOT, d, path.basename(rel)))) ||
  CHAIN_TARGETS.some((rel) => fs.existsSync(path.join(BACKUP_ROOT, d, chainBakName(rel)))) ||
  CTXWIN_TARGETS.some((t) => fs.existsSync(path.join(BACKUP_ROOT, d, ctxwinBakName(t.rel)))) ||
  NOLOOP_TARGETS.some((t) => fs.existsSync(path.join(BACKUP_ROOT, d, noloopBakName(t.rel))));
// 备份目录名形如 `3.18.25-20260903-105032`(可能带 `-cfgonly`)。取版本号那一段。
const bakVersion = (d) => (d.match(/^(\d+\.\d+\.\d+)-/) || [])[1] || null;

/* 🔴 还原源必须**按真实路径逐个**解析,不能"挑一个备份目录、然后只从它里面拿"。
   数据(09-24,/tmp/cxaudit 可复现):造两代备份 ——
     A `3.21.18-20260922-100000`:workbench ×2 + cxctxwin__…agent-host…main.js
     B `3.21.18-20260923-100000`:只有 cxnoloop__ ×2(09-23 那次只改了 agent-host)
   老代码 `[...baks].reverse().find(bakHasBundle)` 挑到 B(它确实"含 bundle"),然后
   **只从 B 还原** ⇒ 带着 @cxteam-keepmine marker 的 workbench ×2 一个字节没还原,
   屏幕上照样打「✅ 恢复原状完成(bundle + 配置 + Key + 小代理)」。半还原报成全还原。
   这正是先跑 09-22 包、又跑 09-23 包的人留下的备份形状 —— 不是边角情况。
   规则:同版本备份目录**从旧到新**扫(listBackupDirs 已按名排序 = 时间序),
   每条真实路径取**第一个**含它任一备份名的目录。从旧到新 = 那一版第一次落的备份才是
   原厂的;更新的那些是"已经打过某些补丁的样子",拿来还原会留下别家族的 marker。 */
function restorePlan(ver) {
  const dirs = listBackupDirs().filter((d) => bakVersion(d) === ver);
  // 同一条真实路径在不同代里可能以**不同扁平名**备份过(agent-host/main.js 既是 ctxwin
  // 目标又是 noloop 目标,谁先建计划就归谁的名)⇒ 必须按真实路径合并候选名,否则会漏。
  const byRel = new Map();
  const want = (rel, kind, name) => {
    const e = byRel.get(rel) || { rel, names: [] };
    if (!e.names.some((n) => n.name === name)) e.names.push({ name, kind });
    byRel.set(rel, e);
  };
  for (const rel of BUNDLES) want(rel, "bundle", path.basename(rel));
  for (const rel of CHAIN_TARGETS) want(rel, "chain", chainBakName(rel));
  for (const t of CTXWIN_TARGETS) want(t.rel, "ctxwin", ctxwinBakName(t.rel));
  for (const t of NOLOOP_TARGETS) want(t.rel, "noloop", noloopBakName(t.rel));
  const items = [], noSource = [];
  for (const e of byRel.values()) {
    let hit = null;
    for (const d of dirs) {
      // 家族标签取**真正命中的那个备份名**的,不是第一个注册的 —— 否则 agent-exec 那份
      // 明明是 ctxwin 的备份却被打成 "chain",给下一轮诊断递假情报。
      const n = e.names.find((x) => fs.existsSync(path.join(BACKUP_ROOT, d, x.name)));
      if (n) { hit = { rel: e.rel, kind: n.kind, from: path.join(BACKUP_ROOT, d, n.name), dir: d }; break; }
    }
    if (hit) items.push(hit);
    else if (fileHasOurMarker(e.rel)) noSource.push(e.rel); // 没备份源、但**现在身上有 marker** 才算缺口
  }
  return { dirs, items, noSource };
}

const ALL_MARKERS = () => [CHAIN_MARKER, CTXWIN_MARKER, NOLOOP_MARKER]
  .concat(PATCHES.map((p) => p.marker).filter(Boolean));

function fileHasOurMarker(rel) {
  const p = path.join(RES, rel);
  if (!fs.existsSync(p)) return false;
  const src = fs.readFileSync(p, "utf8");
  return ALL_MARKERS().some((m) => src.includes(m));
}

/* 还原完必须**重新数一遍 marker**。「拷贝了 N 个文件」不是「还原干净了」——
   只有这一格能把"半还原"和"全还原"分开。⇒ 会数东西的判据先得自己数得到。 */
function leftoverMarkers() {
  const left = [];
  const seen = new Set();
  const check = (rel) => {
    if (seen.has(rel)) return; seen.add(rel);
    const p = path.join(RES, rel);
    if (!fs.existsSync(p)) return;
    const src = fs.readFileSync(p, "utf8");
    const hit = ALL_MARKERS().filter((m) => src.includes(m));
    if (hit.length) left.push({ rel, marks: hit });
  };
  for (const rel of BUNDLES) check(rel);
  for (const rel of CHAIN_TARGETS) check(rel);
  for (const t of CTXWIN_TARGETS) check(t.rel);
  for (const t of NOLOOP_TARGETS) check(t.rel);
  return left;
}

/* 🔴 「Key 在不在」不能只判 null:--uninstall 摘 Key 写的是**空串**(`db.set(…,"")`)而不是删行。
   于是卸载完再跑 --upgrade,`k != null` 成立 ⇒ 打印「库里已有 Key,原样保留」、keyDone=true、
   最后一行还写「直接用」—— 一路假绿,而他其实一个 Key 都没有,打开 Cursor 必 401。
   doctor 那格同病(打成「存在 len=0」)。统一用这把尺子。 */
function keyPresent(k) {
  if (k == null) return false;
  let s = String(k).trim();
  if (s.length >= 2 && s[0] === '"' && s[s.length - 1] === '"') s = s.slice(1, -1).trim();
  return s.length > 0;
}

async function revert() {
  const baks = listBackupDirs();
  if (!baks.length) { console.log("!! 无备份可回滚"); process.exit(1); }
  const ver = cursorVersion();
  /* --revert 会**直接覆写 bundle**,和 --uninstall 的破坏性一样 ⇒ 同样要先确认 Cursor 已退出。
     Cursor 跑着的时候覆写 workbench,它自己的 mmap/懒加载会拿到半新半旧的字节。
     (09-24 补:原来只有 --uninstall 有这道闸,--revert 从入口就绕过去了。) */
  if (!process.env.CX_SKIP_RUNNING_CHECK && cursorRunning()) {
    console.log("!! Cursor 正在运行 —— 请先完全退出(mac ⌘Q / Windows 右键托盘图标退出)再跑 --revert。");
    process.exit(2);
  }
  /* ⚠️ **版本必须匹配**。老代码只挑「最新的含 bundle 备份」,不看版本:本机实测 live=3.20.17
     而最新含 bundle 备份是 3.18.25 → 会把**跨两个大版本的 bundle 盖到新 app 上**,那份产物
     与 app 里其余几千个文件不配套,等于把 Cursor 弄坏,而且这一步没有备份可再退。 */
  const rp = restorePlan(ver);
  if (!rp.items.length) {
    const newest = [...baks].reverse().find(bakHasBundle);
    console.log("!! 没有与当前 Cursor %s 同版本的备份 —— 拒绝回滚(不拿旧版 bundle 覆盖新版 app)。", ver);
    if (newest) console.log("   最近的含 bundle 备份是 %s(版本 %s),盖上去会把 Cursor 弄坏。", newest, bakVersion(newest) || "?");
    console.log("   想恢复原状请跑 --uninstall:它会在没有同版本备份时教你用官方安装包重装(bundle 回原厂,配置/Key 照样清干净)。");
    process.exit(1);
  }
  console.log("从备份回滚(逐路径取同版本里**最早**那份 = 原厂的那份):");
  for (const it of rp.items) {
    fs.copyFileSync(it.from, path.join(RES, it.rel));
    console.log("  restored %s: %s  ← %s", it.kind, path.basename(it.from), it.dir);
  }
  if (rp.noSource.length) {
    console.log("  !! 这几条**没有可用备份**,身上还带着我们的 marker,没还原:");
    for (const rel of rp.noSource) console.log("     · " + rel);
  }
  /* 配置那三样(blob / settings / key)同样逐个解析,取同版本里**最早**含它的目录 ——
     和 bundle 一个道理:后面每次 --upgrade 都会再落一份,里面躺着的已经是"我们的样子"了。 */
  const cfgSrc = (name) => {
    for (const d of rp.dirs) { const p = path.join(BACKUP_ROOT, d, name); if (fs.existsSync(p)) return p; }
    return null;
  };
  const blob = cfgSrc("applicationUser.blob.json");
  if (blob) {
    const db = openDb();
    await db.set(APP_USER_KEY, fs.readFileSync(blob, "utf8"));
    db.close(); console.log("  restored applicationUser blob ←", path.basename(path.dirname(blob)));
  }
  const sj = cfgSrc("settings.json");
  if (sj) { fs.copyFileSync(sj, SETTINGS_JSON); console.log("  restored settings.json"); }
  const ks = cfgSrc("openAIKey.secret.json");
  if (ks) {
    const db = openDb();
    await db.set(OPENAI_KEY_SECRET, fs.readFileSync(ks, "utf8"));
    db.close(); console.log("  restored openAIKey secret");
  }
  // 拷完必须复查:「拷了几个文件」不是「还原干净了」。
  const left = leftoverMarkers();
  if (left.length) {
    console.log("\n❌ 回滚**不完整** —— 这些文件上还留着我们的 marker:");
    for (const l of left) console.log("   · %s  [%s]", l.rel, l.marks.join(","));
    console.log("   bundle 这半请用官方安装包覆盖安装一次(聊天记录/设置在 ~ 目录里,不会丢)。");
    process.exitCode = 6;
  } else {
    console.log("回滚完成(已复查:app 上不再有本方案 marker),重启 Cursor 生效。");
  }
  // 回滚会把 BYOK 地址还原成备份里那个（公网直连），此时再留着小代理服务就是个孤儿：
  // 没人连它，但它还占着 8788、还在 KeepAlive。一起收掉。
  zkdUninstall(false);
}

/* ── --uninstall:彻底恢复原状 ────────────────────────────────────────────────
   与 --revert 的分工:
     --revert    = "回到我装之前那一刻"(需要同版本备份,能连 Key/选中模型一起还原)
     --uninstall = "把 cursor-g 从这台机器上拿掉"(**不要求**有备份也能走完配置那半)

   为什么不能用「按 marker 摘补丁」来还原 bundle(试过,行不通):
     8 个补丁里有两个把原文**销毁**了 ——
       dedicated : 整个条件 `<minify名>(this.storageService,"useDedicatedLocalAgentRuntimeHost")`
                   被换成一个写死的 !1(外加 @cxteam-dedicated 注释),那个 minify 函数名在产物里不存在了;
       localagent: `<minify名>.localMode` 被换成 `!0`,旧形状的前缀里也不含那个标识符。
     补后文本里没有重建原文所需的信息 → 逐字节还原**只可能**来自 pristine 副本。
     所以 bundle 这半是三层:同版本备份 → 官方安装包重装 → (都没有就明说,不硬来)。

   配置那半永远做得到,且它才是同事真正在意的("模型菜单里那堆 cr-g-* 清掉、别再用我的 Key")。
   即使 bundle 没法还原也照做,并**明确告诉他哪半做了哪半没做** —— 不许报一个含糊的"已恢复"。 */
async function uninstall(dry) {
  const ver = cursorVersion();
  console.log("=== 恢复原状(--uninstall)%s ===", dry ? " [dry-run,什么都不写]" : "");

  // ① bundle:优先同版本备份;没有就给出官方重装指引(绝不拿别的版本盖)
  console.log("--- 1) bundle 还原 ---");
  const rp = restorePlan(ver);
  let bundleDone = false;
  if (rp.items.length) {
    const used = [...new Set(rp.items.map((i) => i.dir))];
    console.log("   用同版本备份(逐路径取最早那份):%s", used.join(" + "));
    for (const it of rp.items) {
      if (!dry) fs.copyFileSync(it.from, path.join(RES, it.rel));
      console.log("   %s %s: %s  ← %s", dry ? "将还原" : "已还原", it.kind, path.basename(it.from), it.dir);
    }
    bundleDone = true;
    if (rp.noSource.length) {
      bundleDone = false;
      console.log("   !! 这几条**没有可用备份**、身上却还带着我们的 marker,%s:", dry ? "不会被还原" : "没还原");
      for (const rel of rp.noSource) console.log("      · " + rel);
    }
    /* 🔴 真跑完必须重数一遍 marker。老代码在这里无条件 `bundleDone = true`,
       于是"只还原了 agent-host、workbench 一个字节没动"照样打印「✅ 恢复原状完成」。 */
    if (!dry) {
      const left = leftoverMarkers();
      if (left.length) {
        bundleDone = false;
        console.log("   ❌ 复查:还原后这些文件上仍有本方案 marker:");
        for (const l of left) console.log("      · %s  [%s]", l.rel, l.marks.join(","));
      } else {
        console.log("   ✓ 复查:app 上已无本方案 marker。");
      }
    }
  } else {
    // 数一下现在到底还有没有补丁在身上,免得让人白重装。
    // 🔴 必须把 exthost 家族(chain/ctxwin/noloop)一起数:它们不在 BUNDLES 里。只数 workbench
    //    的话,agent-host 上还挂着 noloop 却报「本来就是原厂的,不用动」—— 假绿。
    const still = leftoverMarkers();
    if (!still.length) {
      console.log("   当前 bundle 上没有本方案的 marker —— 本来就是原厂的,不用动。");
      bundleDone = true;
    } else {
      console.log("   !! 没有与当前 Cursor %s 同版本的备份,而 bundle 上还有补丁。", ver);
      console.log("   不拿别的版本的 bundle 覆盖(那会把 Cursor 弄坏)。bundle 这半请用官方安装包覆盖安装:");
      console.log("     mac : https://cursor.com/downloads 下 dmg,拖进「应用程序」选替换");
      console.log("     win : 下官方安装器 exe 直接跑一遍(装在原位即可)");
      console.log("   覆盖安装会把 workbench 换回原厂,聊天记录/设置都在 ~ 目录里,不会丢。");
      console.log("   下面的配置/Key/小代理**照样清干净**,不受这一条影响。");
    }
  }

  // ② BYOK 配置:把 baseUrl / useOpenAIKey / 我们加进去的模型名 / 被我们设过的选中模型 撤掉
  console.log("--- 2) BYOK 配置清理 ---");
  let cfgNote = "";
  try {
    const db = openDb();
    const raw = await db.get(APP_USER_KEY);
    if (!raw) { console.log("   applicationUser blob 不存在,跳过"); db.close(); }
    else {
      const d = JSON.parse(raw);
      const before = { baseUrl: d.openAIBaseUrl, useKey: d.useOpenAIKey };
      // 地址/开关回到"没配过 BYOK"的样子
      d.openAIBaseUrl = "";
      d.useOpenAIKey = false;
      const ai = (d.aiSettings = d.aiSettings || {});
      /* 装前那份 blob(版本不必匹配 —— 这是配置不是 bundle)。
         `--apply` 改成整表赋值之后,这份备份是**唯一**还知道他装前有哪些名字的东西:
         库里已经只剩我们那 20 个了。下面模型清单和 modelConfig 都要用它,所以读一次。

         ⚠️ **不能取"最新"那份**(旧代码就是 `.reverse().find(...)`,台架 ⑨ 当场抓到)。
         装过一次之后,后面每次 --upgrade 都会再落一份 -preconfig,而那里面躺着的已经是
         **我们的 20 个**了 —— 拿它当"装前"等于把"他自己加的名字"读成空集,卸载会
         一个都还不回来,输出却照样写"还回你装前自己加的 0 个"(假绿:数字对,语义错)。
         判据改成:**取最早那份里还带非我们名字的**。理由是时间上第一份才是真正的"装前";
         再退一步,如果连它都没有非我们的名字,那他装前本来就没自己加过,空集是对的。 */
      let preAi = null, preSrc = null;
      for (const x of listBackupDirs()) {  // 正序 = 从最早那份开始
        const f = path.join(BACKUP_ROOT, x, "applicationUser.blob.json");
        if (!fs.existsSync(f)) continue;
        let a = null;
        try { a = JSON.parse(fs.readFileSync(f, "utf8")).aiSettings || null; } catch (e) { continue; }
        if (!a) continue;
        if (!preAi) { preAi = a; preSrc = x; }  // 兜底:最早一份可读的
        const mine = (a.userAddedModels || []).some((m) => typeof m === "string" && !isOurs(m));
        if (mine) { preAi = a; preSrc = x; break; }  // 找到真正的"装前"就停
      }
      const blobBak = preSrc;
      /* 模型清单:删我们的名字 + **把他装前自己加的名字还回去**。
         为什么要"还回去"而不是"没动过":09-20 第二轮 mergeConfig 改成整表赋值,
         他自己加的第三方名在装的时候就被清掉了 —— 到这一步库里根本没有可"不动"的东西。
         旧代码只做 dropOurs(库里留下的就是他的),那个前提已经不成立:
         照旧只 dropOurs 的话,卸载完他的清单是**空的**,而他会以为"恢复原状"了。
         判据不是"我们的名字没了",是"他装前那份清单回来了"。
         取不到备份就只能 dropOurs —— 这种情况必须在输出里说出来,不许静默。 */
      const dropOurs = (arr) => (arr || []).filter((x) => !(typeof x === "string" && isOurs(x)));
      // 摘掉的 = 库里命中 MODEL_PREFIXES 的那些(先数,下面 merge 会改数组)
      const dropped = (ai.userAddedModels || [])
        .filter((x) => typeof x === "string" && isOurs(x)).length;
      const merge = (live, pre) => {
        const out = dropOurs(live), seen = new Set(out);
        // 备份里他自己的名字(我们的名字不还 —— 那等于又装回去了)
        for (const x of (pre || [])) {
          if (typeof x === "string" && !isOurs(x) && !seen.has(x)) { seen.add(x); out.push(x); }
        }
        return out;
      };
      ai.userAddedModels = merge(ai.userAddedModels, preAi && preAi.userAddedModels);
      ai.modelOverrideEnabled = merge(ai.modelOverrideEnabled, preAi && preAi.modelOverrideEnabled);
      const restored = (preAi && preAi.userAddedModels || [])
        .filter((x) => typeof x === "string" && !isOurs(x)).length;
      /* 选中模型:只有当它是我们设的名字时才动(否则会把同事自己挑的模型弄掉)。
         **优先从备份里取他装前挑的那个名字**,取不到才退回 "default" ——
         `--apply` 会把 composer 从他自己的模型换成 DEFAULT_MODEL,到卸载时库里已经没有原值了;
         只写 "default" 等于**静默弄丢他的选择**(台架实测:装前 composer=claude-opus-4.6,
         卸完变 default,判据当场报红)。装前的值在同一份备份的 applicationUser.blob.json 里躺着。

         另外两个细节也是看真库定的:
          - **不能 `delete mc[feat]`**:那个对象里还有 `maxMode` 这类兄弟设置(实测
            `cmd-k` = {modelName, maxMode, selectedModels}),整键删掉会连带把它抹了。
            改成写回 Cursor 自己对"没选过"的表示法:modelName/selectedModels 都是 "default"
            (实测 background-composer / spec / deep-search 等未选过的键就是这个形状)。
          - **要扫全部功能位,不只 composer/cmd-k**:安装时只设这两个,但同事可能自己在
            deep-search / plan-execution 里也挑了 cr-g(实测本机 modelConfig 有 8 个键)。
            "恢复原状"的判据是"库里不该再有我们的名字被选中",所以按值判而不是按键名判。 */
      const mc = (ai.modelConfig = ai.modelConfig || {});
      // 装前的 modelConfig:上面那份 preAi 里就有(同一份备份,读一次即可)
      const preMc = (preAi && preAi.modelConfig) || null;
      const cleared = [];
      for (const feat of Object.keys(mc)) {
        const e = mc[feat];
        if (!e || typeof e !== "object") continue;
        const nm = e.modelName;
        const selOurs = Array.isArray(e.selectedModels) && e.selectedModels.some((s) => s && isOurs(s.modelId));
        if (!isOurs(nm) && !selOurs) continue;
        // 装前那个名字只有在**不是我们的名字**时才算"他自己的选择"(否则等于又装回去了)
        const pre = preMc && preMc[feat] && preMc[feat].modelName;
        const back = pre && !isOurs(pre) ? pre : "default";
        e.modelName = back;
        e.selectedModels = (preMc && preMc[feat] && back === pre && Array.isArray(preMc[feat].selectedModels))
          ? preMc[feat].selectedModels
          : [{ modelId: back, parameters: [] }];
        cleared.push(feat + ":" + (nm || "?") + "→" + back);
      }
      cfgNote = `baseUrl ${JSON.stringify(before.baseUrl)}→"" ; useOpenAIKey ${JSON.stringify(before.useKey)}→false ; ` +
        `摘掉 ${dropped} 个模型名 ; 还回你装前自己加的 ${restored} 个 ; 选中还原 [${cleared.join(", ") || "无"}]` +
        (blobBak ? ` (装前值取自备份 ${blobBak})`
                 : " (⚠️ 没有备份 blob —— 装前你自己加的模型名还不回来,选中位只能回 default)");
      console.log("   %s%s", dry ? "将改:" : "已改:", cfgNote);
      if (!dry) await db.set(APP_USER_KEY, JSON.stringify(d));
      db.close();
    }
  } catch (e) { console.log("   !! 配置清理失败(%s)—— 可在 Cursor 设置里手动关掉 BYOK", e.message); }

  // ③ Key:把我们写进 keytar/OSCrypt 的那条摘掉。同事的 Key 是他自己的东西,不留在盘上。
  console.log("--- 3) OpenAI Key ---");
  try {
    const db = openDb();
    const k = await db.get(OPENAI_KEY_SECRET);
    if (!keyPresent(k)) console.log("   库里没有 Key 记录(或已是空串),跳过");
    else { if (!dry) await db.set(OPENAI_KEY_SECRET, ""); console.log("   %s Key(置空)", dry ? "将摘掉" : "已摘掉"); }
    db.close();
  } catch (e) { console.log("   !! Key 清理失败(%s)—— 可在 Cursor 设置里手动删", e.message); }

  // ④ 小代理:卸服务 + 杀进程(源码留在 ~/.zk-delta,想重装不用再解包)
  console.log("--- 4) zk-delta 小代理 ---");
  zkdUninstall(dry);

  // ⑤ update.mode:只有当它正是我们写的 "none" 时才删,同事自己设的别的值不动
  console.log("--- 5) 升级锁 update.mode ---");
  try {
    if (!fs.existsSync(SETTINGS_JSON)) console.log("   没有 settings.json,跳过");
    else {
      const s = JSON.parse(fs.readFileSync(SETTINGS_JSON, "utf8") || "{}");
      const ours = Object.entries(UPDATE_LOCK).filter(([k, v]) => s[k] === v).map(([k]) => k);
      if (ours.length) {
        for (const k of ours) delete s[k];
        if (!dry) fs.writeFileSync(SETTINGS_JSON, JSON.stringify(s, null, 2));
        console.log("   %s %s(恢复自动升级)", dry ? "将删" : "已删", ours.join(" / "));
      } else console.log("   升级锁不是我们写的(或没有),不动");
    }
  } catch (e) { console.log("   !! settings.json 不是纯 JSON(含注释?)→ 没动,可手动删掉 \"update.mode\":\"none\""); }

  console.log("");
  if (dry) { console.log("[dry-run] 以上都没写盘。加 --apply 真执行。"); return; }
  if (bundleDone) console.log("✅ 恢复原状完成(bundle + 配置 + Key + 小代理,已复查无 marker),重启 Cursor 生效。");
  else {
    // 「哪些还原了、哪些没有」上面第 1 步逐条打过了 ⇒ 这里只说结论,不含糊成"bundle 没还原"。
    console.log("⚠️  配置 / Key / 小代理 / 升级锁 已清干净;**bundle 只还原了一部分**(上面第 1 步标 ❌ / !! 的那几个文件还带着补丁)。");
    console.log("   那几个文件请用官方安装包覆盖安装一次即可回原厂(聊天记录/设置在 ~ 目录里,不会丢)。");
    process.exitCode = 6;
  }
  console.log("   备份目录 %s **没有删**,想装回来跑 --apply。", BACKUP_ROOT);
}

/* ── zk-delta：本机小代理（只发增量到公网）────────────────────────────────────
   形状：Cursor → 127.0.0.1:8788（小代理）→ 公网只发增量 → 198 上的 zk-delta
        → 那边重建出逐字节相同的全量 → LiteLLM → 网关 → GPT。
   重建发生在 LiteLLM **之前**，所以上游收到的字节和不装它时完全一样 ——
   模型看到的东西没变，省的只是「你家宽带 → 机房」这一跳。

   为什么小代理要用 Cursor 自带的 Electron 跑，而不是 node：
     分发包的整个卖点是「不用装 Node」。plist 里写 `node` 的话，同事机器上没有 node
     就起不来，而 Cursor 的 BYOK 地址已经指到 127.0.0.1:8788 了 → 直接连不上，
     **等于把 Cursor 弄坏**。所以复用本安装器同一个技巧：ELECTRON_RUN_AS_NODE=1 + Cursor 本体。
     已实测：sidecar 在 Cursor 3.17.19 的 Electron 下正常起来并服务 /healthz。

   为什么同事这边一定要关掉抓包：
     开发机上小代理默认把请求体存到 tests/fixtures 攒金样。那是给我自己验证用的，
     同事机器上开着就是**把别人的真实工作内容写到他自己磁盘上**。这里硬写 MAX=0。

   失效怎么办（三层，逐层都不需要人介入）：
     1) 集群那边挂了 → 小代理自己回落成直连上游（passthru），Cursor 照用，只是不省流量。
     2) 小代理进程挂了 → launchd KeepAlive 拉起来。实测 kill 后 ~1s 回来，
        代价是内存里的会话句柄丢了，每条会话下一发 409 → 重发一次全量。
     3) 想彻底退回去 → --no-zk-delta：卸服务 + 把 BYOK 地址改回公网直连。

   诚实边界：**只在 macOS 实测过。** Windows/Linux 没有 launchd，本步骤直接跳过并说明，
   不写一个没验证过的服务定义假装支持。 */
const ZKD_HOME = path.join(os.homedir(), ".zk-delta");
const ZKD_PLIST = path.join(os.homedir(), "Library", "LaunchAgents", "com.zkdelta.sidecar.plist");
const ZKD_LOG = path.join(os.homedir(), "Library", "Logs", "zk-delta-sidecar.log");
const ZKD_LOCAL_URL = "http://127.0.0.1:8788/v1";
const ZKD_DELTA_URL = "https://cc.auto-link.com.cn/zkd/v1/delta";
const ZKD_UPSTREAM = "https://cc.auto-link.com.cn/pro";
// 源文件在包里的位置。装的时候必须保住这个相对结构：sidecar.js 里写的是 require('../common/framing')。
const ZKD_FILES = [
  ["zk-delta/sidecar/sidecar.js", path.join("sidecar", "sidecar.js")],
  ["zk-delta/common/framing.js", path.join("common", "framing.js")],
];

function zkdSupported() { return process.platform === "darwin"; }

/* 8788 上**现在**有没有一个活着的小代理。判据只认 /healthz 真响应,不认
   "~/.zk-delta 目录在不在" —— 目录在而进程死了的机器,地址指过去就是连不上。 */
function zkdAlive() {
  if (process.env.CX_ZKD_ALIVE) return process.env.CX_ZKD_ALIVE === "1"; // 测试钩子(台架⑪)
  try {
    const c = spawnSync("curl", ["-fsS", "-m", "2", "http://127.0.0.1:8788/healthz"], { encoding: "utf8" });
    return c.status === 0 && /"ok":true/.test(c.stdout || "");
  } catch (e) { return false; }
}

function zkdInstall(dry) {
  if (!zkdSupported()) {
    console.log("   跳过：zk-delta 的服务定义只在 macOS 实测过（launchd）。当前 %s —— 不写没验证过的东西。", process.platform);
    console.log("   → 本机仍走公网直连，功能不受影响，只是不省流量。");
    return { ok: false, reason: "platform_" + process.platform };
  }
  // 源文件必须齐。缺一个就拒绝，不半装 —— 半装的后果是 BYOK 指向一个起不来的端口。
  const srcs = [];
  for (const [rel, dst] of ZKD_FILES) {
    const p = path.join(__dirname, rel);
    if (!fs.existsSync(p)) {
      console.log("   !! 包里缺 %s —— 拒绝安装 zk-delta（半装会把 Cursor 指到一个起不来的端口）。", rel);
      return { ok: false, reason: "missing_" + rel };
    }
    srcs.push([p, path.join(ZKD_HOME, dst)]);
  }
  const shas = srcs.map(([p]) => crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex").slice(0, 12));
  console.log("   小代理源码 sha256[:12] = %s", shas.join(" / "));
  console.log("   装到 %s，服务定义 %s", ZKD_HOME, ZKD_PLIST);
  console.log("   Cursor BYOK 地址 → %s（增量端点 %s）", ZKD_LOCAL_URL, ZKD_DELTA_URL);
  console.log("   抓包：关（ZKD_CAPTURE_MAX=0）—— 不往你磁盘上写任何请求体");
  if (dry) return { ok: true, dry: true };

  for (const [p, dst] of srcs) {
    fs.mkdirSync(path.dirname(dst), { recursive: true });
    fs.copyFileSync(p, dst);
  }
  // 先清掉占着 8788 的游离进程，否则 launchd 拉起来的那个会因端口被占反复重启，
  // 而 KeepAlive 会把这种失败藏起来（看着像"服务在跑"，其实一直在崩溃循环）。
  try {
    const pg = spawnSync("pgrep", ["-f", "zk-delta.*sidecar\\.js"], { encoding: "utf8" });
    const pids = (pg.stdout || "").trim().split("\n").filter(Boolean).filter((x) => x !== String(process.pid));
    if (pids.length) { console.log("   清掉占着 8788 的旧进程:", pids.join(",")); spawnSync("kill", pids); }
  } catch (e) { /* pgrep 没有也无所谓 */ }
  spawnSync("launchctl", ["unload", ZKD_PLIST], { stdio: "ignore" });

  fs.mkdirSync(path.dirname(ZKD_PLIST), { recursive: true });
  fs.mkdirSync(path.dirname(ZKD_LOG), { recursive: true });
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  fs.writeFileSync(ZKD_PLIST, `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.zkdelta.sidecar</string>
  <key>ProgramArguments</key>
  <array>
    <string>${esc(cursorBin())}</string>
    <string>${esc(path.join(ZKD_HOME, "sidecar", "sidecar.js"))}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>ELECTRON_RUN_AS_NODE</key><string>1</string>
    <key>ZKD_PORT</key><string>8788</string>
    <key>ZKD_DELTA_URL</key><string>${esc(ZKD_DELTA_URL)}</string>
    <key>ZKD_UPSTREAM</key><string>${esc(ZKD_UPSTREAM)}</string>
    <key>ZKD_CAPTURE</key><string></string>
    <key>ZKD_CAPTURE_MAX</key><string>0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${esc(ZKD_LOG)}</string>
  <key>StandardErrorPath</key><string>${esc(ZKD_LOG)}</string>
</dict></plist>
`);
  const lr = spawnSync("launchctl", ["load", ZKD_PLIST], { encoding: "utf8" });
  if (lr.status !== 0) {
    console.log("   !! launchctl load 失败: %s", (lr.stderr || "").trim());
    return { ok: false, reason: "launchctl_load" };
  }
  // 自检：起不来就必须当场说，不能装完就走 —— BYOK 已经指过去了，
  // 起不来的话同事下一次用 Cursor 就是连不上，而他不会知道是这一步的问题。
  let alive = "";
  for (let i = 0; i < 12; i++) {
    const c = spawnSync("curl", ["-fsS", "-m", "3", "http://127.0.0.1:8788/healthz"], { encoding: "utf8" });
    if (c.status === 0 && /"ok":true/.test(c.stdout || "")) { alive = c.stdout.trim(); break; }
    spawnSync("sleep", ["1"]);
  }
  if (!alive) {
    console.log("   !! 小代理起不来（12 秒内 /healthz 无响应）。日志: %s", ZKD_LOG);
    try { console.log(fs.readFileSync(ZKD_LOG, "utf8").split("\n").slice(-8).join("\n")); } catch (e) {}
    console.log("   → 为了不把 Cursor 指到一个死端口，本次**不改** BYOK 地址。");
    return { ok: false, reason: "healthz_timeout" };
  }
  console.log("   ✅ 小代理已起来: %s", alive);
  return { ok: true };
}

function zkdUninstall(dry) {
  if (!zkdSupported()) { console.log("   （非 macOS，本来就没装）"); return; }
  console.log("   %s %s，BYOK 地址改回公网直连", dry ? "将卸掉" : "卸掉", ZKD_PLIST);
  if (dry) return;
  spawnSync("launchctl", ["unload", ZKD_PLIST], { stdio: "ignore" });
  try { fs.unlinkSync(ZKD_PLIST); } catch (e) {}
  try {
    const pg = spawnSync("pgrep", ["-f", "zk-delta.*sidecar\\.js"], { encoding: "utf8" });
    const pids = (pg.stdout || "").trim().split("\n").filter(Boolean);
    if (pids.length) spawnSync("kill", pids);
  } catch (e) {}
  console.log("   ✅ 已卸。%s 里的源码没删（想重装不用再解包）。", ZKD_HOME);
}

/* ── main ── */

/* ── 飞书 lark-cli + 官方 lark-* skills + 登录（2026-09-03）──────────────────
   为什么：网页线模型现在会先搜本机 skill 库再动手（skill-hint/skill-kick，服务端已开）。
   它搜的是**用户自己电脑**的 ~/.claude/skills，跑的是本机 lark-cli。没这三样，"建个飞书文档"
   只能到"我搜过了、没有"为止（不会伪造链接，但也建不出来）。
   怎么装：lark-cli 是原生二进制，npm 包只是壳。同事机器没 npm、GitHub 不通，所以直接从
   npmmirror 拉官方平台包（/-/binary/lark-cli/v<ver>/…，与 npm 包 checksums.txt 同源），
   sha256 不对就不落盘。skills 用包里带的官方副本（27 个 lark-*），一个不覆盖已有的。
   app secret 只走 stdin（config init --app-secret-stdin），不进 argv、不写日志。
   全部幂等："有就跳过"是用户定的规矩：已装 lark-cli 的机器一个字节不碰。 */
const LARK_VER = "1.0.90";
const LARK_APP_ID = "cli_a91569fab9b81bc6";
// 与 @larksuite/cli@1.0.90 包内 checksums.txt 逐字相同
const LARK_SHA = {
  "darwin-arm64": "894c68176bd4015e8478094ded6d9c7ad76abf9d9cd5679d36b23d0b74d4db02",
  "darwin-amd64": "d5fac57d8b0b674144a5ff2f1f408d0cca8ec9a1d923ed136d6e08522c3b01f0",
  "windows-amd64": "7c5adfaf212a00533a658dfe7754a80564f2512f3e2adefc80c3cc3ab22fa30f",
  "windows-arm64": "be3fee31b950837d0eceb6b154dfe0258098d948deed0ac4f9c3f68284abfdb6",
};
const LARK_HOME = path.join(os.homedir(), ".lark-cli");
const LARK_BIN_DIR = path.join(LARK_HOME, "bin");
const LARK_BIN = path.join(LARK_BIN_DIR, "lark-cli" + (process.platform === "win32" ? ".exe" : ""));
const LARK_SKILLS_SRC = path.join(__dirname, "lark-skills");
const CLAUDE_SKILLS = path.join(os.homedir(), ".claude", "skills");

function larkExe() {
  // 先看 PATH 上有没有（用户自己装过的最优先），再看我们装的位置。
  const which = process.platform === "win32" ? "where" : "which";
  const w = spawnSync(which, ["lark-cli"], { encoding: "utf8" });
  const fromPath = (w.status === 0 && w.stdout.trim().split(/\r?\n/)[0]) || null;
  for (const cand of [fromPath, LARK_BIN]) {
    if (!cand || !fs.existsSync(cand)) continue;
    const v = spawnSync(cand, ["--version"], { encoding: "utf8", timeout: 15000 });
    if (v.status === 0) return cand;
  }
  return null;
}
function larkRun(exe, argv, opts) {
  return spawnSync(exe, argv, { encoding: "utf8", timeout: 60000, env: { ...process.env, PATH: LARK_BIN_DIR + path.delimiter + (process.env.PATH || "") }, ...(opts || {}) });
}
function sha256File(p) { return crypto.createHash("sha256").update(fs.readFileSync(p)).digest("hex"); }

function larkInstallBinary() {
  const plat = process.platform === "darwin" ? "darwin" : process.platform === "win32" ? "windows" : null;
  const arch = process.arch === "arm64" ? "arm64" : process.arch === "x64" ? "amd64" : null;
  if (!plat || !arch) return { ok: false, reason: "不支持的平台 " + process.platform + "/" + process.arch };
  const key = plat + "-" + arch, want = LARK_SHA[key];
  if (!want) return { ok: false, reason: "没有 " + key + " 的校验值" };
  const archive = "lark-cli-" + LARK_VER + "-" + key + (plat === "windows" ? ".zip" : ".tar.gz");
  const url = "https://registry.npmmirror.com/-/binary/lark-cli/v" + LARK_VER + "/" + archive;
  const tmp = path.join(os.tmpdir(), "lark-cli-dl-" + Date.now());
  fs.mkdirSync(tmp, { recursive: true });
  const dl = path.join(tmp, archive);
  console.log("   下载 " + url);
  const d = plat === "windows"
    ? spawnSync("powershell", ["-NoProfile", "-Command", "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -Uri '" + url + "' -OutFile '" + dl + "'"], { encoding: "utf8", timeout: 300000 })
    : spawnSync("curl", ["-fsSL", "--retry", "2", "-o", dl, url], { encoding: "utf8", timeout: 300000 });
  if (d.status !== 0 || !fs.existsSync(dl)) return { ok: false, reason: "下载失败: " + ((d.stderr || d.stdout || "").trim().slice(0, 200) || "rc=" + d.status) };
  const got = sha256File(dl);
  if (got !== want) return { ok: false, reason: "sha256 不符（期望 " + want.slice(0, 12) + "… 实得 " + got.slice(0, 12) + "…），不落盘" };
  fs.mkdirSync(LARK_BIN_DIR, { recursive: true });
  const x = plat === "windows"
    ? spawnSync("powershell", ["-NoProfile", "-Command", "Expand-Archive -Force -Path '" + dl + "' -DestinationPath '" + LARK_BIN_DIR + "'"], { encoding: "utf8", timeout: 120000 })
    : spawnSync("tar", ["-xzf", dl, "-C", LARK_BIN_DIR, "lark-cli"], { encoding: "utf8", timeout: 120000 });
  if (x.status !== 0 || !fs.existsSync(LARK_BIN)) return { ok: false, reason: "解包失败: " + ((x.stderr || "").trim().slice(0, 200) || "rc=" + x.status) };
  if (plat !== "windows") fs.chmodSync(LARK_BIN, 0o755);
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch (_) {}
  // 让 Cursor 里的终端/模型能直接敲 lark-cli：追加 PATH（幂等）。
  if (plat === "darwin") {
    const line = 'export PATH="$HOME/.lark-cli/bin:$PATH"  # lark-cli (cursor-g setup)';
    for (const rc of [".zprofile", ".zshrc", ".bash_profile"]) {
      const f = path.join(os.homedir(), rc);
      const cur = fs.existsSync(f) ? fs.readFileSync(f, "utf8") : "";
      if (!cur.includes(".lark-cli/bin")) fs.appendFileSync(f, (cur.endsWith("\n") || !cur ? "" : "\n") + line + "\n");
    }
    for (const link of ["/opt/homebrew/bin/lark-cli", "/usr/local/bin/lark-cli"]) {
      try { if (!fs.existsSync(link) && fs.existsSync(path.dirname(link))) { fs.symlinkSync(LARK_BIN, link); break; } } catch (_) {}
    }
  } else {
    const ps = "$p=[Environment]::GetEnvironmentVariable('Path','User'); if(($p -split ';') -notcontains '" + LARK_BIN_DIR + "'){[Environment]::SetEnvironmentVariable('Path', ($p.TrimEnd(';') + ';" + LARK_BIN_DIR + "'), 'User')}";
    spawnSync("powershell", ["-NoProfile", "-Command", ps], { encoding: "utf8", timeout: 60000 });
  }
  return { ok: true, sha: got };
}

function larkInstallSkills() {
  if (!fs.existsSync(LARK_SKILLS_SRC)) return { ok: false, reason: "包里没有 lark-skills/ 目录" };
  fs.mkdirSync(CLAUDE_SKILLS, { recursive: true });
  let added = [], kept = 0;
  for (const name of fs.readdirSync(LARK_SKILLS_SRC).sort()) {
    const src = path.join(LARK_SKILLS_SRC, name), dst = path.join(CLAUDE_SKILLS, name);
    if (!fs.statSync(src).isDirectory()) continue;
    if (fs.existsSync(dst)) { kept++; continue; }          // 有就跳过：符号链接/实体目录都算有
    fs.cpSync(src, dst, { recursive: true });
    added.push(name);
  }
  return { ok: true, added, kept };
}

async function larkSetup(args) {
  console.log("--- 6) 飞书 lark-cli / skills / 登录（全部有就跳过）---");
  if (!args.lark) return;   // 默认不做、也不打印,别让同事看见一行看不懂的东西
  // ① 二进制
  let exe = larkExe();
  if (exe) console.log("   ✅ lark-cli 已有: " + exe + "（不动）");
  else {
    const r = larkInstallBinary();
    if (!r.ok) { console.log("   ⚠️  lark-cli 没装上: " + r.reason + " —— 其余功能不受影响，飞书相关以后可重跑安装器补。"); }
    else { exe = LARK_BIN; console.log("   ✅ lark-cli " + LARK_VER + " 已装到 " + LARK_BIN + "（sha256 " + r.sha.slice(0, 12) + "… 校验通过）"); }
  }
  // ② skills
  const sk = larkInstallSkills();
  if (!sk.ok) console.log("   ⚠️  skills: " + sk.reason);
  else console.log("   ✅ skills: 新装 " + sk.added.length + " 个，已有 " + sk.kept + " 个未动" + (sk.added.length ? "（" + sk.added.slice(0, 5).join(", ") + (sk.added.length > 5 ? " …" : "") + "）" : ""));
  if (!exe) return;
  // ③ app 配置 + ④ 登录。判据用 auth status --json（appId / identities.user.status）。
  let st = null;
  try { const r = larkRun(exe, ["auth", "status", "--json"]); st = JSON.parse((r.stdout || "").trim() || "null"); } catch (_) {}
  const hasApp = !!(st && st.appId);
  const userReady = !!(st && st.identities && st.identities.user && st.identities.user.status === "ready");
  if (userReady) { console.log("   ✅ 飞书已登录（" + ((st.identities.user.userName) || "user") + "），不动。"); return; }
  if (!hasApp) {
    if (!process.stdin.isTTY) { console.log("   ⚠️  没配飞书应用且不在终端里，跳过。以后手动: lark-cli config init --app-id " + LARK_APP_ID + " --app-secret-stdin"); return; }
    const secret = (await promptLine("   请粘贴飞书 App Secret 后回车(和 API Key 在同一张领取表里;直接回车=跳过飞书登录): ")).trim();
    if (!secret) { console.log("   (跳过飞书登录。以后可重跑安装器，或手动 lark-cli auth login)"); return; }
    const r = larkRun(exe, ["config", "init", "--app-id", LARK_APP_ID, "--app-secret-stdin", "--brand", "feishu", "--lang", "zh"], { input: secret + "\n" });
    if (r.status !== 0) { console.log("   ⚠️  config init 失败: " + ((r.stderr || r.stdout || "").trim().slice(0, 200))); return; }
    console.log("   ✅ 飞书应用已配置（secret 只经 stdin）");
  }
  if (!process.stdin.isTTY) { console.log("   ⚠️  不在终端里，跳过登录。以后: lark-cli auth login"); return; }
  console.log("   下面会弹浏览器让你确认飞书授权（Device Flow），确认完这里自动继续…");
  const lg = spawnSync(exe, ["auth", "login", "--domain", "docs,drive,im,wiki,sheets,base,calendar,task"], { stdio: "inherit", timeout: 600000, env: { ...process.env, PATH: LARK_BIN_DIR + path.delimiter + (process.env.PATH || "") } });
  if (lg.status === 0) console.log("   ✅ 飞书已登录");
  else console.log("   ⚠️  登录没完成（rc=" + lg.status + "）。以后随时补: lark-cli auth login");
}

/* ── `--doctor`(2026-09-23):把「判据那一栏」一次性打出来,**只读,不写任何东西** ──────
   为什么要这把尺子:同事报「This model does not support custom API keys」,那句话在
   3.21.18 **整个 app 里 0 命中**(desktop/glass/agent-host/local-agent-runtime 全搜过),
   且带 Cursor 形状的 `Request ID: <uuid>` ⇒ 它来自 Cursor 后端,不是我们的腿
   (我们的腿返的是 `API 异常 (req: 8位)`,中文短 id;repo 里也 0 命中)。
   ⇒ 剩下唯一没数据的格子全在**他那台机器的状态里**,而我拿不到。这把尺子就是让他
      一条命令把那几格交出来,不用截图、不用来回问。

   🔴 打出来的每一项都要能单独判真假,别打成一段散文:
     - 补丁装没装:按 marker 逐条数(装了几处),不是"看着装了"。
     - Key 有没有:只打 **sha256 前16位**,明文一个字节都不落盘/不上屏。
     - 开关/档位/baseUrl:原值照抄。
     - 目录撞名:我们装的名字里 `isUserAdded!==true` 的**逐个列出来** —— 这一格非 0
       说明 keepmine 没生效(补丁没装、或装了没重启 Cursor 刷目录)。
     - 最近会话选的哪个模型:报错那一发到底选的是谁,这格空着就没法归因。 */
async function doDoctor() {
  const crypto = require("crypto");
  const out = [];
  const P = (...a) => { const s = a.join(" "); out.push(s); console.log(s); };
  P("=== cursor-g doctor ===");
  P("时间      :", new Date().toISOString());
  P("平台      :", process.platform, process.arch);
  P("Cursor    :", cursorVersion());
  P("资源目录  :", RES);

  P("\n--- 1) bundle 补丁(按 marker 数命中处数) ---");
  // 🔴 数 marker 必须数**完整注释形状** `/*@cxteam-x*/`,不许裸数名字:
  //    `@cxteam-keepmine` 是 `@cxteam-keepmine2` 的子串,`-nocloud` 是 `-nocloudset` 的子串,
  //    裸数会互相灌水(我 09-23 在 `setUseOpenAIKey(!1)` 上已经栽过一次同形的假红)。
  // 名单不写死:直接把 bundle 里出现的所有 cxteam marker 枚举出来,免得以后加了补丁忘改这里。
  const MUST = ["byokforce", "byokkeepon", "keepmine", "keepmine2", "keepfill2", "nocloud", "nocloudset", "noagenthost"];
  for (const rel of BUNDLES) {
    const p = path.join(RES, rel);
    if (!fs.existsSync(p)) { P("  !!", rel, "不存在"); continue; }
    const s = fs.readFileSync(p, "utf8");
    const seen = new Map();
    for (const m of s.match(/\/\*@cxteam-[a-z0-9]+\*\//g) || []) seen.set(m, (seen.get(m) || 0) + 1);
    const line = [...seen.entries()].map(([k, v]) => k.slice(10, -2) + "=" + v).sort().join(" ");
    P("  " + path.basename(rel) + ": " + (line || "(一个都没有 ⇒ 补丁没装)"));
    const missing = MUST.filter((n) => !seen.has("/*@cxteam-" + n + "*/"));
    if (missing.length) P("    🔴 缺:" + missing.join(", ") + " ⇒ 这台机器没跑过新版安装器(或 Cursor 升级后冲掉了,双击 REPAIR)");
  }
  /* 件C:每个目标应当 =3(锚点 A/B/C)。<3 ⇒ Cursor 会在上下文还很空时就每轮自动压缩
     对话,那一发压缩中位数 43~98s(10-28 实测),同事只会说「Cursor 比 Claude 慢」。
     原来这里只打数字、不解释,一屏数字里 "0" 和 "3" 没人看得出区别。 */
  P("  ctxwin(" + CTXWIN_MARKER + "):");
  let cwBad = 0, cwNeed = 0;
  for (const t of CTXWIN_TARGETS) {
    const p = path.join(RES, t.rel);
    if (!fs.existsSync(p)) { P("    !!", t.rel, "不存在"); continue; }
    const s = fs.readFileSync(p, "utf8");
    const n = s.split(CTXWIN_MARKER).length - 1;
    /* 3.28.5 起 agent-host 那份里上游把摘要阈值整段挪走了 ⇒ 它**本来就不该有** marker。
       把它算成"缺"= 假红,会让人去跑一个修不了也不用修的 REPAIR。 */
    const need = n > 0 || CW_RELEVANT.test(s);
    if (need) { cwNeed++; if (n < 3) cwBad++; }
    P("    " + n + "  " + t.rel + (!need ? "   (这版不含摘要阈值,不适用)" : n < 3 ? "   🔴 应为 3" : ""));
  }
  if (cwBad) P("    🔴 件C 没打全(该打的 " + cwNeed + " 个文件里有 " + cwBad + " 个不对)⇒ 会**每轮都自动压缩对话**,"
    + "表现就是「发一句要等很久」。先双击 REPAIR,再重启 Cursor;修完还慢就把这份报告发给管理员。");
  else if (!cwNeed) P("    ⚠️ 四个文件都不含摘要阈值 —— 要么 Cursor 换了结构,要么路径不对,把这份报告发给管理员。");
  // 件D:0 = local loop 没被摘 ⇒ 若下面第 4 节那个 gate 是 on,这台机器就会把这一轮
  // 打到 Cursor 自家 InferenceService(BYOK 的 key 压根不参与)。
  P("  noloop(" + NOLOOP_MARKER + "):");
  for (const t of NOLOOP_TARGETS) {
    const p = path.join(RES, t.rel);
    if (!fs.existsSync(p)) { P("    !!", t.rel, "不存在"); continue; }
    const n = fs.readFileSync(p, "utf8").split(NOLOOP_MARKER).length - 1;
    P("    " + n + "  " + t.rel + (n === 0 ? "   🔴 没摘" : ""));
  }

  P("\n--- 2) BYOK 配置 / Key ---");
  let d = null;
  try {
    const db = openDb();
    const raw = await db.get(APP_USER_KEY);
    const k = await db.get(OPENAI_KEY_SECRET);
    db.close();
    if (!raw) P("  !! applicationUser blob 不存在(Cursor 没初始化过?)");
    else d = JSON.parse(raw);
    // 明文不上屏:只打长度 + sha256 前16位,够对账、不泄露。
    P("  openAIKey secret:", !keyPresent(k) ? "**不存在**(没配过 Key,或被 --uninstall 置空)"
      : "存在 len=" + String(k).length + " sha256_16=" + crypto.createHash("sha256").update(String(k)).digest("hex").slice(0, 16));
    /* 🔴 上面那两个读数**量不到零宽字符**:密文长度和 sha256 对带 ZWSP 的 key 与干净 key 同形。
       所以这里解一次明文,只报**码位**(不打任何明文字节),回答"是不是 ByteString 那个病"。
       解不开 / 读不到主密码都如实报"量不到",不许把沉默当成"干净"。 */
    if (keyPresent(k)) {
      let pt = null, why = null;
      try {
        const ok = await deriveOsCryptKey(RES);
        if (!ok) why = "读不到钥匙串主密码(是不是漏了 ELECTRON_RUN_AS_NODE=1 用 Cursor 自己的二进制跑?)";
        else {
          const encBuf = Buffer.from(JSON.parse(String(k)).data);
          if (encBuf.slice(0, 3).toString() !== "v10") why = "密文没有 v10 前缀,方案不符";
          else pt = oscryptDecrypt(ok, encBuf);
        }
      } catch (e) { why = (e && e.message) || String(e); }
      if (pt == null) {
        P("    不可见字符扫描:**量不到**(" + why + ") ⇒ 这一格是空的,别当成\"干净\"");
      } else {
        const hits = scanInvisible(pt);
        P("    明文 len=" + pt.length + "(码位数=" + [...pt].length + ")");
        if (!hits.length) P("    ✅ 不可见/越界字符 = 0 ⇒ 这把 Key 不会触发 ByteString 报错");
        else {
          P("    🔴 不可见/越界字符 = " + hits.length + " ⇒ **就是这个病**:Cursor 拼 header 时会抛" +
            " `Cannot convert argument to a ByteString …`,请求一个字节都出不了网");
          for (const h of hits)
            P("        字符下标 " + h.index + "  " + cpHex(h.cp) + "  " + h.name +
              "   ← 报错里的 index 应是 " + (h.index + 7) + "(\"Bearer \" 占 7)");
          P("        解:在 Cursor 设置里把 Key 全选删掉重填(别从飞书表格/聊天里复制),或跑一次 INSTALL/--key");
        }
      }
    }
  } catch (e) { P("  !! 读库失败:", e && e.message || e); }
  if (d) {
    for (const f of ["membershipType", "isEnterprise", "useOpenAIKey", "useClaudeKey", "openAIBaseUrl", "hasTokenBasedPricing"]) {
      P("  " + f + " =", JSON.stringify(d[f]));
    }
    const uam = (d.aiSettings && d.aiSettings.userAddedModels) || [];
    P("  userAddedModels =", uam.length, "个");
    const cat = d.availableDefaultModels2 || [];
    const mine = cat.filter((e) => uam.includes(e.name));
    const bad = mine.filter((e) => e.isUserAdded !== true);
    P("  目录 =", cat.length, "条; 其中属于我们的 =", mine.length, "条");
    P("  🔴 我们的名字里 isUserAdded!==true 的 =", bad.length, "个",
      bad.length ? "(keepmine 没兜住 ⇒ 这几个点了就不走 BYOK)" : "(全部兜住)");
    for (const e of bad) P("      -", e.name, "isUserAdded=" + JSON.stringify(e.isUserAdded),
      "params=" + ((e.parameterDefinitions || []).length));
    const miss = uam.filter((n) => !cat.some((e) => e.name === n));
    /* 档位(10-16 wangzhongwei):名字在但 variants 空 ⇒ opus 只有一个、不能换 High/Medium/Low。
       对照档位模板,模板里有档位、目录里却没有的名字逐个列出来。 */
    try {
      const tiers = JSON.parse(TIER_CATALOG_JSON || "{}");
      const noTier = mine.filter((e) => tiers[e.name] && !(e.variants && e.variants.length)).map((e) => e.name);
      P("  有档位的名字(High/Medium/Low 等):", mine.filter((e) => e.variants && e.variants.length).length, "个");
      if (noTier.length) P("  ⚠️ 该有档位却没有的:", noTier.join(", "), "\n     ⇒ 不能换挡。用新包双击 UPGRADE(keepfill2 会补);补丁在仍没有就发报告。");
    } catch (e) { /* 模板读不到不影响其余自检 */ }
    if (miss.length) P("  ⚠️ 装了但目录里没有的名字:", miss.join(", "),
      "\n     ⇒ 服务端给这个账号的目录少了这些名字(10-16 liangjingyu:菜单里没有 opus 就是这个)。" +
      "\n       keepfill2 补丁会在 Cursor 刷目录时补回;上面第 1) 节没有 keepfill2 就用新包双击 UPGRADE,有了仍缺就把这份报告发给运维。");
  }

  /* 🔴 2026-10-28:「刷模型」跑的不是用户手上这个包,而是 INSTALL/UPGRADE 当时拷到
     ~/.carher/cursor-setup/ 的那一份(解压目录会被删,所以必须拷)。它**只在再跑一次
     INSTALL/UPGRADE 时才更新** ⇒ 管理员修好的东西,对只点过「刷模型」的人等于没发布,
     而屏幕上没有任何地方说得出这件事。这里把那份副本的日期/版本打出来,让它可见。 */
  P("\n--- 2.5) 「刷模型」用的那份脚本(可能比你手上的包旧) ---");
  try {
    const self = path.join(CARHER_DIR, "cursor-setup", "cursor_team_setup.js");
    if (!fs.existsSync(self)) P("  没有(这台机器没跑过带「刷模型」的安装器)⇒ 说「刷模型」不会生效,双击 UPGRADE 一次即可");
    else {
      const crypto2 = require("crypto");
      const h = (f) => crypto2.createHash("sha256").update(fs.readFileSync(f)).digest("hex").slice(0, 12);
      const mine = h(self), now = h(__filename);
      P("  " + self);
      P("  改于 " + new Date(fs.statSync(self).mtimeMs).toISOString().slice(0, 16).replace("T", " ")
        + "  sha=" + mine + (path.resolve(self) === path.resolve(__filename) ? "(就是当前这份)" : "  当前这份 sha=" + now));
      if (path.resolve(self) !== path.resolve(__filename) && mine !== now)
        P("    ⚠️ 两份不一样 ⇒ 「刷模型」走的是旧逻辑。想让它跟上:双击一次 UPGRADE(不会问 Key、不动聊天记录)");
    }
  } catch (e) { P("  量不到(" + ((e && e.message) || e) + ")"); }

  P("\n--- 3) 最近几个会话选的模型(报错那一发选的是谁) ---");
  // ⚠️ `composerData:*` 躺在 **globalStorage/state.vscdb 的 cursorDiskKV** 表里,
  //    不在 workspaceStorage —— 第一版写错了路径,拿到 0 行还把它当成"没有会话"打出去,
  //    典型的「指向不存在路径的尺子恒绿/恒空」。所以下面**先数总行数并打出来**:
  //    行数=0 和"读不到" 必须能区分,不然这一格永远在撒谎。
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(STATE_DB, { readOnly: true });
    const tot = db.prepare("SELECT count(*) AS n FROM cursorDiskKV WHERE key LIKE 'composerData:%'").get();
    P("  composerData 行数 =", tot && tot.n);
    const rs = db.prepare("SELECT key,value FROM cursorDiskKV WHERE key LIKE 'composerData:%' ORDER BY rowid DESC LIMIT 60").all();
    db.close();
    const list = [];
    for (const r of rs) {
      try {
        const c = JSON.parse(String(r.value));
        const mc = c.modelConfig || {};
        list.push({ t: c.lastUpdatedAt || c.createdAt || 0, model: mc.modelName, maxMode: mc.maxMode,
          params: ((mc.selectedModels || [])[0] || {}).parameters });
      } catch (e) { /* 单条坏行不挡整份报告 */ }
    }
    list.sort((a, b) => b.t - a.t);
    if (!list.length) P("  (行数非 0 但一条都解不出来 ⇒ 结构变了,把这句原样发出来)");
    for (const r of list.slice(0, 5)) {
      P("  " + (r.t ? new Date(r.t).toISOString() : "无时间") +
        "  model=" + JSON.stringify(r.model) + "  maxMode=" + JSON.stringify(r.maxMode) +
        "  params=" + JSON.stringify(r.params || []));
    }
  } catch (e) { P("  !! 读 composerData 失败:", e && e.message || e); }

  /* 4) Cursor 服务端下发的 feature gate 实际值。
     为什么这一格必需:`agent_host_local_loop` 决定这一轮跑在哪个 runtime 上 ——
     on ⇒ agent-host 可能把这一轮判给 `managed-local`,直接打 Cursor 自家
     InferenceService(**完全绕过 BYOK**),账号没这个权限就是
     `[permission_denied] InferenceService.RunInference is not enabled for this account`。
     ⚠️ gate 名在 statsig bootstrap 里是 **djb2 哈希**过的,不是明文,所以必须自己算哈希
        再查表;直接 grep 名字必然 0 命中(那是假红,不是"没有这个 gate")。
     判据:本机 BYOK 正常的那台读出来是 false。这一格是 true 且上面 noloop=0 ⇒ 就是它。 */
  P("\n--- 4) Cursor 服务端 feature gate(与 BYOK 路由相关的那几个) ---");
  try {
    const { DatabaseSync } = require("node:sqlite");
    const db = new DatabaseSync(STATE_DB, { readOnly: true });
    const row = db.prepare("SELECT value FROM ItemTable WHERE key=?").get("workbench.experiments.statsigBootstrap");
    db.close();
    if (!row) P("  (没有 statsigBootstrap ⇒ 这台机器还没拉过实验配置)");
    else {
      const j = JSON.parse(Buffer.isBuffer(row.value) ? row.value.toString("utf8") : String(row.value));
      const gates = j.feature_gates || {};
      // statsig djb2:32 位无符号。算法必须与 Cursor 用的一致,否则查不到 = 假"没有这个 gate"。
      const djb2 = (s) => { let h = 0; for (let i = 0; i < s.length; i++) { h = (h << 5) - h + s.charCodeAt(i); h = h & h; } return String(h >>> 0); };
      P("  hash_used =", JSON.stringify(j.hash_used), " gate 总数 =", Object.keys(gates).length);
      // 阳性对照:先证明这张表**查得到东西**。一个都查不到时,下面的 "NOT FOUND" 要能
      // 和"这个 gate 确实没下发"区分开 —— 会数东西的尺子必须先断言自己数得到。
      let hit = 0;
      for (const name of ["cursor_agent_host", "agent_host_local_loop", "agent_host_enabled", "dedicated_local_agent_runtime_host"]) {  // cursor_agent_host=true 且没打 noagenthost ⇒ BYOK 报 custom API keys(10-13)
        const k = djb2(name);
        const g = gates[k];
        if (g) hit++;
        P("  " + name + " = " + (g ? JSON.stringify(g.value) : "(未下发)") + "  [djb2=" + k + "]");
      }
      if (!hit) P("  ⚠️ 三个都查不到 ⇒ 要么哈希算法换了,要么这几个 gate 都没下发。别只凭这一格下结论。");
    }
  } catch (e) { P("  !! 读 feature gate 失败:", e && e.message || e); }

  const dst = path.join(os.tmpdir(), "cursor-g-doctor.txt");
  try { fs.writeFileSync(dst, out.join("\n") + "\n"); P("\n📋 这份报告也存在:", dst, "—— 把它整份发出来即可(里面没有明文 Key)"); }
  catch (e) { P("\n(写报告文件失败,直接复制上面这一屏即可)"); }
}

async function main() {
  const argv = process.argv.slice(2);
  const has = (f) => argv.includes(f);
  // --doctor 必须在所有写路径**之前**返回:它的唯一契约是只读。
  if (has("--doctor")) { await doDoctor(); return; }
  const opt = (f, dflt) => { const i = argv.indexOf(f); return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt; };
  const args = {
    // --upgrade = 老用户升级档:等价于 --apply,但**一个字都不问 Key**(他的 Key 已经在
    // 库里,升级只同步"菜单该有哪些名字"和 bundle 补丁)。隐含 --apply —— 双击式升级不该
    // 先给他一屏 dry-run 再要他找终端敲 --apply。
    upgrade: has("--upgrade"),
    apply: has("--apply") || has("--upgrade"), revert: has("--revert"), repair: has("--repair"),
    uninstall: has("--uninstall"),
    forceVersion: has("--force-version"), pinUpdate: !has("--allow-update"),
    // @cx-chain:v3 链式增量:默认**关**(服务端那半已下线)。不给这个开关时,
    // 安装器会把已装的 chain 补丁摘掉;服务端那半回来再用 --chain 装回去。
    chain: has("--chain"),
    key: opt("--key", ""),
    // zk-delta：默认**开**。理由是它对上游逐字节透明（重建在 LiteLLM 之前），
    // 门① 不可能被它破坏，而省的那一跳是同事每天都在付的成本。
    // 三条退路都验过了（集群挂→回落直连 / 进程挂→KeepAlive / 想彻底退→--no-zk-delta）。
    zkDelta: !has("--no-zk-delta"),
    zkDeltaOnly: has("--zk-delta-only"),
    // 2026-09-03：飞书 lark-cli + 官方 lark-* skills + 登录。全部"有就跳过"，--no-lark 整段不做。
    // 09-03 用户反馈同事被飞书那步搞糊涂 → 默认关,显式 --lark 才做(代码留着)。
    lark: has("--lark") && !has("--zk-delta-only"),
    // 已经在用某个模型的机器（比如我这台基准机是 cursor-web-fc-82-terra），
    // 不许被安装器顺手换成 DEFAULT_MODEL —— 换掉选中模型等于换掉量具。
    keepModel: has("--keep-model") || has("--zk-delta-only"),
    baseUrl: opt("--base-url", ""),
    // 不给 --models 时菜单 = 这把 key 在网关上的模型(下面 resolveMenu 现拉);给了就按给的装(台架/排障用)。
    models: opt("--models", "").split(",").map((s) => s.trim()).filter(Boolean),
    // 「刷模型」:只按 key 重拉菜单写库,不动 bundle / Key / 小代理 / 升级锁。Cursor 开着就先退出再重开。
    refreshModels: has("--refresh-models"),
    bareSa: has("--bare-sa"),
  };

  /* ── `--bare-sa`：把菜单里的 `sa-*` 换成去前缀的裸名（2026-09-22 加）────────
     用户面的理由：裸名（`grok-4.6`、`grok-4.7`…）是 **Cursor 自带目录里认识的名字**，
     菜单里写裸名才画得出 Context / Effort / Fast 那几个控件；`sa-` 开头的名字
     Cursor 不认识，`parameterDefinitions` 是空的，连控件都不画。

     🔴 **默认关，而且必须一直默认关。** 裸名能不能用是 **key 属性不是名字属性**：
        网关侧要在那把 key 上同时补 `models`（allowlist，缺 → 403）和 `aliases`
        （缺 → 400）两处，两道闸独立。给没补过的同事装上裸名菜单 = 他点一个就报错。
        补 key 的那一步是 `sa_prefix_alias.py`，**先补 key，再用这个开关装菜单**。

     ⚠️ 控件画出来了 ≠ 档位能用。2026-09-22 实测：35 分钟窗口内 209 条 Cursor
        流量里 `fast` / `effort` / `reasoning_effort` 出现 **0 次**，而同窗口其它
        客户端出现 450+ 次（尺子是好的）。Cursor 在 BYOK / OpenAI 兼容线上只拼标准
        字段，界面上那些开关**没有出口**。这个开关解决的是「名字好写、菜单里有」，
        不解决「档位能传到上游」。别把这两件事混成一件。 */
  if (args.bareSa) {
    const mapped = args.models.map((m) => (m.startsWith("sa-") ? m.slice(3) : m));
    // 去前缀可能撞名（比如清单里同时有 `sa-x` 和 `x`）—— 撞了就保留一份，不做去重之外的事。
    const seen = new Set();
    args.models = mapped.filter((m) => (seen.has(m) ? false : (seen.add(m), true)));
    // 让本趟的 isOurs 认得这些裸名。不认的后果有两个,都是静默的:
    //   ① 选中位判断会认为"当前选中的不是我们的名字",把他选的 grok-4.7 打回默认;
    //   ② --uninstall 的 dropOurs 摘不掉它们,同事以为卸干净了、库里还留着。
    for (const m of args.models) if (!MODEL_PREFIXES.some((p) => m.startsWith(p))) EXTRA_OURS.add(m);
    ACTIVE_DEFAULT_MODEL = DEFAULT_MODEL.startsWith("sa-") ? DEFAULT_MODEL.slice(3) : DEFAULT_MODEL;
    console.log("--bare-sa: 菜单 %d 名已去掉 sa- 前缀,默认选中 %s(裸名需要该 key 已补 alias,见 sa_prefix_alias.py)",
      args.models.length, ACTIVE_DEFAULT_MODEL);
  }

  if (!fs.existsSync(RES)) { console.log("!! 找不到 Cursor 资源目录:", RES); process.exit(1); }
  const ver = cursorVersion();
  console.log("Cursor version:", ver, "| platform:", process.platform, "| runtime:", process.version);

  if (args.revert) return revert();
  // --uninstall 也吃 Cursor 运行检查(下面那段)和 dry-run 默认:不带 --apply 只预览。
  // 放在软版本闸**之前**:恢复原状不该被"这个版本没验过"的告警劝退。
  if (args.uninstall) {
    if (!process.env.CX_SKIP_RUNNING_CHECK && cursorRunning()) {
      console.log("!! Cursor 正在运行 —— 请先完全退出(mac ⌘Q / Windows 右键托盘图标退出)再跑。");
      process.exit(2);
    }
    return uninstall(!args.apply);
  }

  if (args.refreshModels) return refreshModels(args);

  // #4 软版本闸:非 3.16.x 只告警不拒绝——真正的安全阀是 planBundle 里"锚点必须 exactly-1"。
  // 这样 Cursor 升级后 --repair 也能尝试;bundle 变到锚点认不出 → planBundle 自动拒绝(不会改坏)。
  if (!args.forceVersion && !VERIFIED_EXACT.includes(ver)) {
    const majorOk = VERIFIED_VERSIONS.some((v) => ver.startsWith(v + "."));
    if (!majorOk) {
      console.log("⚠️  本安装器在 Cursor %s 上验过,当前 %s 这个大版本没验过。将继续尝试;若锚点认不出会自动拒绝(不会改坏),请把这段输出发给管理员。",
        VERIFIED_VERSIONS.map((v) => v + ".x").join(" / "), ver);
    } else {
      // 大版本对得上但这个小版本没量过:别假装验过。打完建议他自查一次。
      console.log("ℹ️  当前 Cursor %s 这个**小版本**没在台架上数过锚点(量过的:%s)。将继续尝试,打完请双击一次 DOCTOR,看 ctxwin 那四行是否都 =3;不是就把报告发给管理员。",
        ver, VERIFIED_EXACT.join(" / "));
    }
  }
  if (!process.env.CX_SKIP_RUNNING_CHECK && cursorRunning()) {
    console.log("!! Cursor 正在运行 —— 请先完全退出(mac ⌘Q / Windows 右键托盘图标退出)再跑。");
    process.exit(2);
  }

  console.log("--- 1) bundle 补丁计划 ---");
  const plans = [];
  if (args.zkDeltaOnly) {
    console.log("   跳过（--zk-delta-only：只装小代理 + 改 BYOK 地址，不碰 bundle / 模型 / Key）");
  } else {
  for (const rel of BUNDLES) { const r = planBundle(rel); if (r) plans.push(r); }
  // @cx-chain:v3 默认**不装**(服务端那半已下线,见 planChainRemoval 上面的三条腿)。
  // 不带 --chain 时反过来做:装过的机器在这里被摘干净,同事只要跑一次安装器就恢复。
  for (const rel of CHAIN_TARGETS) {
    const r = args.chain ? planChainBundle(rel) : planChainRemoval(rel);
    if (r) plans.push(r);
  }
  // 件C @cx-ctxwin:v3 上下文窗口单位归一,**默认装**(这是所有人都在挨的,不是可选项)。
  // 🔴 与 chain 家族有两个同名 main.js 重叠:同一个路径若已有计划,必须**接着那份产物改**
  // 并原地替换,不能再 push 一条 —— 两条计划先后 writeFileSync 同一路径,后写的会把
  // 前一个静默冲掉,而且两份都"成功"了,不报错。
  for (const t of CTXWIN_TARGETS) {
    const abs = path.join(RES, t.rel);
    const i = plans.findIndex((x) => x.p === abs);
    const r = planCtxwinBundle(t, i >= 0 ? plans[i].out : null);
    if (!r) continue;
    if (i >= 0) plans[i] = { ...plans[i], out: r.out, applied: (plans[i].applied || []).concat(r.applied) };
    else plans.push(r);
  }
  // 件D @cx-noloop:v1 关掉 agent-host 本地循环,**默认装**(local loop 与 BYOK 互斥,
  // 是 Cursor 自己源码里写明的,不是可选项)。两个目标都与 ctxwin 家族同路径重叠
  // ⇒ 必须接着已有计划的产物改并原地替换,不能再 push 一条(两条计划先后写同一路径,
  // 后写的会把前一个静默冲掉,而且两份都"成功"、不报错)。
  for (const t of NOLOOP_TARGETS) {
    const abs = path.join(RES, t.rel);
    const i = plans.findIndex((x) => x.p === abs);
    const r = planNoloopBundle(t, i >= 0 ? plans[i].out : null);
    if (!r) continue;
    if (i >= 0) plans[i] = { ...plans[i], out: r.out, applied: (plans[i].applied || []).concat(r.applied) };
    else plans.push(r);
  }
  console.log("--- 2) 语法校验补后 bundle ---");
  for (const { p, out } of plans) {
    if (!syntaxCheck(out, path.basename(p).split(".")[1])) { console.log("   !! 语法校验不过,终止,未落任何盘。"); process.exit(4); }
    console.log("   syntax OK:", path.basename(p));
  }
  }

  // #4 修复模式:只重打 bundle(配置/Key 在库里,升级不动它们),不碰 config/update.mode/Key。
  if (args.repair) {
    // REPAIR 也补上升级锁:修好之后别让 Cursor 当晚又自动升级、把刚打的补丁冲掉
    console.log("--- 禁止 Cursor 自动升级 ---");
    if (args.pinUpdate) setUpdateNone(false);
    // 🔴 `plans.length===0` 有两种含义,不许合并成一句「都在」:①真的都打过了 ②锚点认不出被跳过了。
    if (!plans.length) {
      if (reportMissed()) { process.exitCode = 5; return; }
      console.log("\n✅ bundle 补丁都在,无需修复。"); return;
    }
    console.log("--- 修复:重打 bundle ---");
    await doBackup(ver, plans, null);
    for (const { p, out } of plans) { fs.writeFileSync(p, out); console.log("   patched:", path.basename(p)); }
    if (reportMissed()) {
      process.exitCode = 5;
      console.log("\n⚠️  部分完成:上面列红的文件没打上,其余已重打。重启 Cursor 后那几条对应的毛病仍在。");
    } else {
      console.log("\n✅ 修复完成,重启 Cursor 即可继续用 " + ACTIVE_DEFAULT_MODEL + "。");
    }
    return;
  }

  // ── 2.5) zk-delta 小代理。**必须在 mergeConfig 之前**：
  //    BYOK 地址要不要指到 127.0.0.1，取决于小代理有没有真起来。
  //    顺序倒过来的后果是：小代理起不来，而地址已经改过去了 → Cursor 直接连不上。
  console.log("--- 2.5) zk-delta 本机小代理（只发增量到公网）---");
  let zkdOn = false;
  if (!args.zkDelta) {
    zkdUninstall(!args.apply);
  } else {
    const r = zkdInstall(!args.apply);
    zkdOn = !!r.ok;
    /* 🔴 装不上 ≠ 这台机器上没有小代理。最常见的一种:引擎**不是从包里跑的**
       (管理员在 repo 里直接跑 `node cursor_team_setup.js`),`zk-delta/` 不在
       __dirname 旁边 ⇒ 返回 missing_*,而他机器上那个小代理**跑得好好的**。
       老逻辑到这里一律把 baseUrl 改写成公网直连,于是一次无关的 --upgrade 就把
       一台配好的机器静默降级成"不省流量",屏幕上只有一行"拒绝安装"。
       判据用**真相**不用原因码:直接打 /healthz —— 活着就保住本机地址。
       (healthz 不通时仍然改回公网:那种情况下指着 127.0.0.1 是彻底连不上,更坏。) */
    if (!zkdOn && zkdSupported() && zkdAlive()) {
      zkdOn = true;
      console.log("   ↳ 这一步装不上,但 8788 上已有一个活着的小代理 ⇒ 地址保持 %s(不降级)。", ZKD_LOCAL_URL);
    } else if (!zkdOn && args.apply) console.log("   → 地址保持公网直连（%s），功能不受影响。", DEFAULT_BASE_URL);
  }
  // dry-run 下 zkdInstall 返回 {ok:true,dry:true} = "这些前置条件都满足，真跑能装上"；
  // 返回 ok:false（缺文件 / 非 mac / 起不来）就必须**一路影响到地址**，
  // 否则会出现"上面说拒绝安装，下面又说地址改成 127.0.0.1"这种自相矛盾的预览。
  const wantLocal = args.zkDelta && zkdOn;
  if (!args.baseUrl) args.baseUrl = wantLocal ? ZKD_LOCAL_URL : DEFAULT_BASE_URL;

  // 菜单要按这把 Key 现拉 ⇒ Key 必须在写菜单**之前**拿到(只读,写 Key 仍在落盘之后的第 5 步)。
  let rawKey = cleanKey(args.key);
  if (!args.zkDeltaOnly && !args.upgrade && !rawKey && process.stdin.isTTY && args.apply) {
    rawKey = cleanKey(await promptLine("   请粘贴你的 API Key 后回车(直接回车=稍后自己在 Cursor 里填): "));
  }
  if (!args.zkDeltaOnly) await resolveMenu(args, rawKey);

  console.log("--- 3) BYOK 配置差异 ---");
  const oldBlob = await mergeConfig(args, !args.apply);
  console.log("--- 4) 禁止 Cursor 自动升级 ---");
  if (args.pinUpdate) setUpdateNone(!args.apply);
  else console.log("   跳过(--allow-update:允许 Cursor 自动升级;升级后模型没了就双击 REPAIR)");

  if (!args.apply) {
    if (reportMissed()) { process.exitCode = 5; console.log("\n[dry-run] 其余项通过,但上面列红的文件锚点认不出。"); return; }
    console.log("\n[dry-run] 以上全部通过。加 --apply 执行(会先备份到 %s)。", BACKUP_ROOT); return;
  }

  console.log("--- 落盘 ---");
  if (plans.length) {
    await doBackup(ver, plans, oldBlob);
    for (const { p, out } of plans) { fs.writeFileSync(p, out); console.log("   patched:", path.basename(p)); }
  } else {
    const bdir = path.join(BACKUP_ROOT, `${ver}-${ts()}-cfgonly`);
    fs.mkdirSync(bdir, { recursive: true });
    fs.writeFileSync(path.join(bdir, "applicationUser.blob.json"), oldBlob);
    try { const db = openDb(); const k = await db.get(OPENAI_KEY_SECRET); db.close();
      if (keyPresent(k)) fs.writeFileSync(path.join(bdir, "openAIKey.secret.json"), k); } catch (e) { /* ignore */ }
    console.log("   备份(仅配置)->", bdir);
  }

  // #2 写 Key:命令行给了 --key 用它,否则 TTY 下提示粘一次;空/非 TTY → 回退手填。
  console.log("--- 5) 写入 API Key ---");
  // --upgrade:不提示、不覆盖。但要**读一下库里到底有没有那条 secret** ——
  // "不问 Key" 和 "他其实从没配过 Key" 是两件事,后者升级完打开 Cursor 会 401,
  // 而屏幕上只写了"完成"。所以查一次,没有就当场告诉他下一步是什么。
  let upgradeHasKey = null;
  if (args.upgrade && !args.zkDeltaOnly) {
    try { const db = openDb(); const k = await db.get(OPENAI_KEY_SECRET); db.close(); upgradeHasKey = keyPresent(k); }
    catch (e) { upgradeHasKey = null; }
    console.log(upgradeHasKey === null
      ? "   跳过(--upgrade:不覆盖你已有的 Key;这次没读到库,状态未知)"
      : upgradeHasKey
      ? "   跳过(--upgrade:库里已有 Key,原样保留)"
      : "   !! --upgrade:库里**没有** Key —— 升级本身已完成,但你还没配过 Key。");
  } else if (args.zkDeltaOnly) {
    console.log("   跳过（--zk-delta-only：Key 已经在库里，不动它）");
  }
  let keyDone = false;
  if (args.upgrade && !args.zkDeltaOnly) { keyDone = upgradeHasKey !== false; }
  else if (args.zkDeltaOnly) { keyDone = true; }
  else if (rawKey) {
    const r = await writeOpenAIKey(rawKey, RES, false);
    if (r.ok) { keyDone = true; console.log("   ✅ Key 已写入" + (r.confirmed ? "(方案已用现有 Key 校验一致)" : "")); }
    else console.log("   ⚠️  自动写 Key 跳过:%s —— 请稍后在 Cursor 里手动粘一次。", r.reason);
  } else {
    console.log("   (没输入 Key,跳过——稍后在 Cursor 里粘一次即可)");
  }

  // #6 画图(10-16):装一个 Cursor 个人 skill(~/.cursor/skills/carher-image)+ 零依赖画图小工具。
  //    Key 在 Cursor 库里是加密的,工具读不到 ⇒ 用户这次粘了 Key 就另存一份到 ~/.carher/image.json(600);
  //    --upgrade 不问 Key ⇒ 已有 image.json 就沿用,没有就只装 skill、提示下次 INSTALL 时补上。
  //    Node 用 Cursor 自己的二进制(ELECTRON_RUN_AS_NODE=1),同事不用另装 Node。
  if (!args.zkDeltaOnly) { try { installImageSkill(rawKey || args.upgradeKey); } catch (e) { console.log("   ⚠️  画图这一步出错(不影响其余): " + (e && e.message || e)); } }
  if (!args.zkDeltaOnly) { try { installRefreshSkill(); } catch (e) { console.log("   ⚠️  「刷模型」这一步出错(不影响其余): " + (e && e.message || e)); } }

  if (args.lark) { try { await larkSetup(args); } catch (e) { console.log("   ⚠️  飞书这一步出错（不影响其余）: " + (e && e.message || e)); } }

  console.log("\n✅ 完成。" + (args.zkDeltaOnly
    ? "启动 Cursor 即可，选中的模型没被动过。"
    : keyDone
    ? "启动 Cursor,模型菜单默认就是 " + ACTIVE_DEFAULT_MODEL + ",直接用。"
    : "还差一步:启动 Cursor → Settings → Models → OpenAI API Key,粘贴你的 key 点 Verify。"));
  if (reportMissed()) process.exitCode = 5;
  if (zkdOn) {
    console.log("   zk-delta 已开：Cursor → 127.0.0.1:8788 → 公网只发增量。");
    console.log("     看省了多少: curl -s http://127.0.0.1:8788/metrics.json");
    console.log("     想退回公网直连: 启动器加 --no-zk-delta（Cursor 要先退出）");
  }
  console.log("   回滚整包:启动器加 --revert;Cursor 升级后失效:双击 REPAIR 或跑 --repair。");
}

/* ── 菜单从 Key 现拉(INSTALL / UPGRADE 共用)────────────────────────────────────
   给了 --models 就照用(台架/排障);否则按 Key 拉 /models → orderMenu。拉不到 ⇒ args.models=[]
   ⇒ mergeConfig 不改菜单(exact=false),并把原因留给它打印。 */
async function resolveMenu(args, rawKey) {
  console.log("--- 2.8) 模型菜单(按你的 Key 从网关现拉)---");
  if (args.models.length) {
    console.log("   用 --models 给定的 %d 个(不拉网关)", args.models.length);
    ACTIVE_DEFAULT_MODEL = args.models.includes(ACTIVE_DEFAULT_MODEL) ? ACTIVE_DEFAULT_MODEL : args.models[0];
    return;
  }
  let key = rawKey || readKeyCopy() || await readKeyFromDb();
  // UPGRADE 本来不问 Key;但 3.24+ 解不开库里的 Key,又没存过副本 ⇒ 不问就刷不了菜单(会留着已下架的名字)。
  if (!key && args.upgrade && process.stdin.isTTY) {
    key = cleanKey(await promptLine("   菜单要按你的 Key 刷新,这台机器还没存过 —— 粘一次 Key 后回车(直接回车=菜单不动): "));
    if (key) args.upgradeKey = key;
  }
  const r = await fetchKeyModels(key, DEFAULT_BASE_URL);
  if (!r.ok) {
    args.models = []; args.menuWhy = r.why;
    console.log("   ⚠️  没拉到模型列表:%s ⇒ 这次菜单不动。", r.why);
    return;
  }
  const menu = orderMenu(r.ids, DEFAULT_MODEL);
  if (!menu.length) {
    args.models = []; args.menuWhy = "这把 Key 只有画图模型";
    console.log("   ⚠️  这把 Key 只开了画图模型(%s),聊天菜单不动;画图直接说「画一张…」。", r.ids.join(", "));
    return;
  }
  args.models = menu;
  ACTIVE_DEFAULT_MODEL = pickDefault(menu);
  const skipped = r.ids.length - menu.length;
  console.log("   ✔ 你的 Key 能用 %d 个模型,菜单放 %d 个%s;默认选中 %s",
    r.ids.length, menu.length, skipped ? "(" + skipped + " 个画图专用名不进聊天菜单)" : "", ACTIVE_DEFAULT_MODEL);
}

/* ── 「刷模型」:--refresh-models ─────────────────────────────────────────────────
   只做一件事:按 Key 重拉菜单写进库(旧的删、新的加、钉着已下架名字的功能位改回默认)。
   不碰 bundle / Key / 小代理 / 升级锁。
   🔴 Cursor 开着时**写库没用**:它退出时会把内存里那份旧菜单写回去盖掉(安装器要求先退出也是这个原因)。
      ⇒ 由 Cursor 里的 agent 调用时:本进程先返回(让 agent 把话说完),后台另起一个脱离的
         worker:等几秒 → 让 Cursor 正常退出 → 写库 → 重新打开 Cursor。聊天记录在库里,重开还在。 */
const REFRESH_LOG = path.join(CARHER_DIR, "cursor-refresh.log");
function rlog(msg) {
  const line = new Date().toISOString() + " " + msg;
  console.log(msg);
  try { fs.mkdirSync(CARHER_DIR, { recursive: true }); fs.appendFileSync(REFRESH_LOG, line + "\n"); } catch (e) { /* ignore */ }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// 正常退出(不强杀):Cursor 可能弹「有 agent 在跑 / 有未保存文件,确定退出?」—— 那需要用户点,不能越过。
// osascript 的 quit 会等 App 回应(最多 ~2 分钟),所以给 timeout,不让它挂住 worker。
function quitCursor() {
  const t = Date.now();
  let r;
  if (process.platform === "darwin") r = spawnSync("osascript", ["-e", 'tell application "Cursor" to quit'], { encoding: "utf8", timeout: 15000 });
  else if (process.platform === "win32") r = spawnSync("taskkill", ["/IM", "Cursor.exe"], { encoding: "utf8", timeout: 15000 });   // 不带 /F:正常关窗
  else r = spawnSync("pkill", ["-TERM", "-f", path.join(path.dirname(EXEC), "cursor")], { encoding: "utf8", timeout: 15000 });
  return "rc=" + (r && r.status) + " sig=" + (r && r.signal) + " " + (Date.now() - t) + "ms " + String((r && r.stderr) || "").trim().slice(0, 160);
}
// 诊断用:当前被判成「Cursor GUI 还在」的进程(只列 pid + 命令,不含参数里的内容)
function runningLines() {
  if (process.platform === "win32") return "";
  const r = spawnSync("pgrep", ["-fl", process.platform === "darwin" ? "Cursor.app/Contents/MacOS/Cursor" : path.join(path.dirname(EXEC), "cursor")], { encoding: "utf8" });
  return String(r.stdout || "").split("\n").filter((l) => { const m = l.match(/^(\d+)\s+(.*)$/); return m && isCursorExe(m[2]) && !/\.js(\s|$)/.test(m[2]); }).map((l) => l.slice(0, 120)).join(" | ");
}
function launchCursor() {
  // 子进程不能继承 ELECTRON_RUN_AS_NODE,否则拉起来的是一个 node 而不是 Cursor 窗口
  const env = { ...process.env }; delete env.ELECTRON_RUN_AS_NODE;
  const opt = { detached: true, stdio: "ignore", env };
  // 🔴 10-28:原来从 process.execPath 推 .app —— 普通 node 跑时会去 open 一个
  //    `/opt/homebrew/Cellar/node/26.12.0` 目录(或 spawn 一个裸 node),
  //    结果是「刷模型」把 Cursor 关了却再也没打开。走 cursorBin() 才是真二进制。
  const bin = cursorBin();
  if (process.platform === "darwin") {
    const app = path.resolve(path.dirname(bin), "..", "..");
    require("child_process").spawn("open", [app], opt).unref();
  } else require("child_process").spawn(bin, [], opt).unref();
}
/* 🔴 2026-10-28 定住的因果链:Cursor **自动升级会冲掉全部 bundle 补丁**,而升级锁是
   10-13 才默认开的。10-13 之前装的人(实测那 4 位都是 3.21~3.24)升级后件C 失效 ⇒
   `getBackgroundSummarizationTriggerThreshold` 拿到档位数字 300 ⇒
   阈值 = min(300-10000, 300*0.9) = **-9700** ⇒ `used >= -9700` 恒真 ⇒ **每轮都压缩对话**,
   每压一次 43~98s。他只会说「Cursor 比 Claude 慢」,而装完那次脚本写的是「✅ 完成」。
   (官方默认值从 3.27.14 的 cursor-agent-exec 读出:unusedTokensThresholdToStart=1e4、
    unusedPercent=.1;第三条腿 requestBodyChars=8MiB 与 maxTokens 无关、补丁碰不到,
    他们最大 27 万字符差 30 倍,已排除。)
   ⇒ 「刷模型」是同事**唯一会主动用**的入口,让它顺手查一次补丁在不在。
   只读、不自动改:发现缺了就告诉他双击 REPAIR(自动打 bundle 要先退出 Cursor,
   而「刷模型」正跑在 Cursor 里,不能在这里偷偷动手)。 */
function patchHealth() {
  const out = { ok: true, lines: [] };
  try {
    for (const rel of BUNDLES) {
      const p = path.join(RES, rel);
      if (!fs.existsSync(p)) continue;
      const s = fs.readFileSync(p, "utf8");
      if (!s.includes("/*@cxteam-byokforce*/")) { out.ok = false; out.lines.push("解锁补丁没了(" + path.basename(rel) + ")"); }
    }
    let need = 0, have = 0;
    for (const tt of CTXWIN_TARGETS) {
      const p = path.join(RES, tt.rel);
      if (!fs.existsSync(p)) continue;
      const s = fs.readFileSync(p, "utf8");
      const n = s.split(CTXWIN_MARKER).length - 1;
      if (n > 0 || CW_RELEVANT.test(s)) { need++; if (n >= 3) have++; }
    }
    if (need && have < need) { out.ok = false; out.lines.push("件C 上下文补丁没了(" + have + "/" + need + ")⇒ 会每轮自动压缩对话,发一句要等很久"); }
  } catch (e) { return { ok: true, lines: [] }; }   // 量不到就别吓人
  return out;
}

async function refreshModels(args) {
  const worker = process.argv.includes("--refresh-worker");
  const key = readKeyCopy() || await readKeyFromDb();
  if (!key) {
    rlog("!! 刷模型:这台机器没存 Key(~/.carher/image.json)。先双击 INSTALL 粘一次 Key,以后就能「刷模型」了。");
    process.exitCode = 3; return;
  }
  const r = await fetchKeyModels(key, DEFAULT_BASE_URL);
  if (!r.ok) { rlog("!! 刷模型:没拉到模型列表(" + r.why + "),菜单没动。"); process.exitCode = 4; return; }
  const menu = orderMenu(r.ids, DEFAULT_MODEL);
  if (!menu.length) { rlog("!! 刷模型:这把 Key 只有画图模型,聊天菜单没动。"); process.exitCode = 4; return; }
  let d;
  try { const db = openDb(); d = JSON.parse(await db.get(APP_USER_KEY)); db.close(); }
  catch (e) { rlog("!! 刷模型:读不到 Cursor 配置库(" + ((e && e.message) || e) + ")"); process.exitCode = 3; return; }
  const curOn = ((d.aiSettings || {}).modelOverrideEnabled) || [];
  const add = menu.filter((m) => !curOn.includes(m)), del = curOn.filter((m) => !menu.includes(m));
  if (!worker) {
    console.log("你的 Key 现在能用 %d 个聊天模型。", menu.length);
    if (add.length) console.log("  + 新增 %d:%s", add.length, add.join(", "));
    if (del.length) console.log("  - 删除 %d:%s", del.length, del.join(", "));
    // 只比 modelOverrideEnabled:与 Cursor 自带同名的几个会被 Cursor 自己从 userAddedModels 剔掉,比那张表永远「不一致」
    const h = patchHealth();
    if (!h.ok) {
      console.log("");
      console.log("⚠️  顺手查到:" + h.lines.join(";"));
      console.log("    多半是 Cursor 自动升级把补丁冲掉了。处理:完全退出 Cursor,双击 REPAIR,再打开。");
    }
    if (!add.length && !del.length) { console.log("菜单已经是最新的,不用改。"); if (!h.ok) console.log("(菜单不用改,但上面那条要处理)"); return; }
  }
  const running = !process.env.CX_SKIP_RUNNING_CHECK && cursorRunning();
  if (running && !worker) {
    // worker 也要用真 Cursor 二进制:普通 node 下 EXEC 是 node,worker 起来后
    // launchCursor/quitCursor 全是错的对象(10-28)。
    const child = require("child_process").spawn(cursorBin(), [__filename, "--refresh-models", "--refresh-worker"],
      { detached: true, stdio: "ignore", env: { ...process.env, ELECTRON_RUN_AS_NODE: "1" } });
    child.unref();
    console.log("Cursor 约 8 秒后会自动退出并重新打开,菜单就更新了(聊天记录都在)。进度日志:" + REFRESH_LOG);
    return;
  }
  if (worker) {
    rlog("刷模型 worker(pid " + process.pid + "):+" + add.length + " -" + del.length + ",等 8 秒后退出 Cursor");
    await sleep(8000);
    rlog("  退出前在跑:" + runningLines());
    rlog("  请求退出:" + quitCursor());
    let t = 0;
    while (cursorRunning() && t < 180) {
      await sleep(1000); t++;
      if (t % 15 === 0) { rlog("  第 " + t + " 秒仍在跑:" + runningLines()); if (t === 30) rlog("  再请求一次退出:" + quitCursor()); }
    }
    if (cursorRunning()) {
      rlog("!! Cursor 3 分钟内没退出(多半弹了「确定退出?」或有未保存文件),菜单没动。处理掉弹窗后再说一次「刷模型」。");
      return;
    }
    rlog("  Cursor 已退出(" + t + " 秒)");
    await sleep(1500);
  }
  args.models = menu; args.keepModel = true; args.baseUrl = d.openAIBaseUrl || DEFAULT_BASE_URL;
  ACTIVE_DEFAULT_MODEL = pickDefault(menu);
  await mergeConfig(args, false);
  rlog("✅ 刷模型完成:菜单 " + menu.length + " 个" + (add.length ? ";新增 " + add.join(",") : "") + (del.length ? ";删除 " + del.join(",") : ""));
  if (worker) { launchCursor(); rlog("已重新打开 Cursor"); }
}

/* 「刷模型」skill:把安装器本体拷到 ~/.carher/cursor-setup/(解压目录可能被删),
   skill 里写死 Cursor 二进制 + 那份拷贝的绝对路径。 */
function installRefreshSkill() {
  console.log("--- 7) 「刷模型」---");
  const dir = path.join(CARHER_DIR, "cursor-setup");
  fs.mkdirSync(dir, { recursive: true });
  const self = path.join(dir, "cursor_team_setup.js");
  if (path.resolve(__filename) !== path.resolve(self)) fs.copyFileSync(__filename, self);
  const tier = path.join(__dirname, "cursor_tier_catalog.json");
  if (fs.existsSync(tier) && path.resolve(path.dirname(tier)) !== path.resolve(dir)) fs.copyFileSync(tier, path.join(dir, "cursor_tier_catalog.json"));
  const CB = cursorBin();
  const cmd = process.platform === "win32"
    ? 'cmd /c "set ELECTRON_RUN_AS_NODE=1&& "' + CB + '" "' + self + '" --refresh-models"'
    : 'ELECTRON_RUN_AS_NODE=1 "' + CB + '" "' + self + '" --refresh-models';
  // 10-27:Cursor 的 agent 也读 ~/.claude/skills;两边都叫 carher-refresh-models 时它挑了 Claude 那份(实测「刷模型」
  //   在 Cursor 里跑成 Claude 的刷新)⇒ 改名 + 描述写死只给 Cursor 用;删掉旧名目录。
  try { fs.rmSync(path.join(os.homedir(), ".cursor", "skills", "carher-refresh-models"), { recursive: true, force: true }); } catch (e) {}
  const sd = path.join(os.homedir(), ".cursor", "skills", "carher-refresh-models-cursor");
  fs.mkdirSync(sd, { recursive: true });
  fs.writeFileSync(path.join(sd, "SKILL.md"), [
    "---",
    "name: carher-refresh-models-cursor",
    "description: 仅限 Cursor 里用:用户说「刷模型」「更新模型」「刷新模型列表」「模型菜单不对/少了/多了」时。按用户的 key 从公司网关重新拉模型列表,更新 Cursor 的模型菜单(新开通的加上、没权限的删掉)。⛔ 你在 Cursor 里时,一律用这个,不要用 carher-refresh-models-claude(那是 Claude Code 的)。",
    "---",
    "",
    "# 刷模型",
    "",
    "直接在终端运行下面这条命令,不要先问用户:",
    "",
    "```",
    cmd,
    "```",
    "",
    "- 命令会打印新增/删除了哪些模型,把这几行原样告诉用户。",
    "- 如果输出说「Cursor 约 8 秒后会自动退出并重新打开」,告诉用户:稍等,Cursor 会自己重开,聊天记录都在。说完就结束,不要再运行别的命令。",
    "- 命令报错就把原文告诉用户,不要自己改配置文件。",
    "",
  ].join("\n"));
  console.log("   ✅ 以后在 Cursor 里说「刷模型」,就按你的 Key 更新模型菜单");
}

function installImageSkill(rawKey) {
  console.log("--- 6) 画图工具 ---");
  const src = path.join(__dirname, "carher_image.js");
  if (!fs.existsSync(src)) { console.log("   跳过(包里没有 carher_image.js)"); return; }
  const home = os.homedir(), dir = path.join(home, ".carher"), cfg = path.join(dir, "image.json");
  fs.mkdirSync(dir, { recursive: true });
  const tool = path.join(dir, "carher_image.js");
  fs.copyFileSync(src, tool);
  // 画图和「刷模型」共用这一份 Key 副本(600)。路径就是 KEY_COPY,这里按 home 现算(台架会换 HOME)。
  const key = String(rawKey || "").replace(/[\s\u00A0\u200B-\u200F\u2028-\u202F\uFEFF]/g, "");
  if (key) {
    fs.writeFileSync(cfg, JSON.stringify({ key, base: DEFAULT_BASE_URL }, null, 2) + "\n", { mode: 0o600 });
    try { fs.chmodSync(cfg, 0o600); } catch (e) { /* Windows 无 chmod 语义 */ }
  }
  const md = fs.readFileSync(path.join(__dirname, "image_skill.md"), "utf8");
  // Cursor 二进制在 node 模式下跑这个工具:命令里先设 ELECTRON_RUN_AS_NODE=1
  const node = process.platform === "win32"
    ? 'cmd /c "set ELECTRON_RUN_AS_NODE=1&& "' + cursorBin() + '""'
    : "ELECTRON_RUN_AS_NODE=1 \"" + cursorBin() + "\"";
  const sd = path.join(home, ".cursor", "skills", "carher-image");
  fs.mkdirSync(sd, { recursive: true });
  // 注意:skill 模板里命令是 `@@NODE@@ "@@TOOL@@"`,NODE 这里已带好自己的引号
  fs.writeFileSync(path.join(sd, "SKILL.md"), md.split('@@NODE@@ "@@TOOL@@"').join(node + ' "' + tool + '"'));
  if (fs.existsSync(cfg)) console.log("   ✅ 已开通画图:在 Cursor 里说「画一张…」即可(用 muse-image)");
  else console.log("   ⚠️  画图 skill 已装,但还没有 Key —— 下次双击 INSTALL 粘一次 Key 就能画了");
}

main().catch((e) => { console.error("!! 未预期错误:", e && e.message ? e.message : e); process.exit(9); });
