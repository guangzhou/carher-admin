# cursor-g-* 命名上线计划(活文档,围绕本文档推进)

> 创建 2026-08-24。对象:bpi-web 线(Cursor → 198 LiteLLM → zero-cursor-bpi[-82]:8201 → 网页
> ChatGPT `/f/conversation`)。上游链路活文档见
> `docs/cursor-ide-chatgpt-web-status-and-plan-20260823.md`,本文档只管**命名换装 + 品类扩充**
> 这一件事,做完即归档。每步做完更新「状态」列,验收数据写进对应小节。

---

## 一、真实目标(复述)

1. **给用户一套干净的模型菜单**:`cursor-g-*` 命名——见名知意(版本+变体+档位),
   **不暴露 "web"**(不让用户困扰于额度面实现),不暴露账号拓扑(池名无数字)。
2. **名实相符**:菜单背后是真实存在的上游模型(2026-08-24 已铁证 terra/sol/luna dot-slug
   均为虚构、静默降级 `gpt-5-6`,不再延续);把两个 Pro 账号 web 面真正可用的品类端上来。
3. **保留全部旧名**(单线直连 6 名 + 旧池 3 名)当调试/定位工具,零删除、零行为变化。
4. **可扩容**:以后加账号,克隆脚本 v2 一键出线+入池,用户面名字永不变;
   新线直连名用 `cursor-g-<N>-*`(带账号号=运维名,不带=用户名)。
5. **计费正常**(已 review:正常。成功请求正确落账,零费行全是 failure,无需改动)。
6. **纪律**:每步有验收、不假绿、可回滚、不影响其他链路。

## 二、命名定稿(用户已拍板)

| 层 | 格式 | 例子 | 用途 |
|---|---|---|---|
| 用户面(池) | `cursor-g-<版本>-<变体>[-high\|-xhigh]`,无数字 | `cursor-g-5.6-sol-xhigh` | 人用;WA 自动跨线负载+容灾 |
| 运维面(直连) | `cursor-g-<N>-<版本>-<变体>[...]` | `cursor-g-82-5.6-sol` | 调试定位钉单线(自下个新号启用) |
| 遗产(冻结) | `cursor-web-fc*` 全部 9 名 | `cursor-web-fc-pool-terra` | 保留不动不删 |

档位后缀 = `reasoning_effort` 参数值原文(`-high`);`-xhigh`/`-max` 均不使用
(`xhigh→max` 对 gpt-5-6 空产出,已退役;旧名里的 `-max` 冻结不动)。

**本次上线的池别名(6 名 × 2 线 = 12 deployment):**

> ⚠️ **Step 9 修正(2026-08-24)**:下表"真身 slug"直接注册进 litellm 会拆掉 litellm 的
> chat→responses 桥(版本解析只认点号)导致 GUI 全崩——litellm 层现注册**点分载体 slug**,
> 真身由 lane ALIASES 映射回来(名实相符在 lane 层保证)。详见 §Step 9 事故与修复。

| 池别名 | litellm 载体 slug(桥接承重) | lane 映射真身(上游实跑) | reasoning_effort |
|---|---|---|---|
| `cursor-g-5.6-sol` | `openai/gpt-5.6-sol` | `gpt-5-6`(官方 title=GPT-5.6 Sol) | 不设 |
| `cursor-g-5.6-sol-high` | `openai/gpt-5.6-sol` | `gpt-5-6` | `high`(→web `extended`) |
| ~~`cursor-g-5.6-sol-xhigh`~~ **已退役** | — | `gpt-5-6` + `xhigh`(→web `max`)| **虚构档,见 Step 4** |
| `cursor-g-5.6-luna` | `openai/gpt-5.6-luna` | `gpt-5-6-t-mini`(title=GPT-5.6 Luna) | 不设 |
| `cursor-g-5.6-pro` | `openai/gpt-5.6-pro` | `gpt-5-6-pro`(lane 原有映射) | 不设 |
| `cursor-g-5.6-instant` | `openai/gpt-5.6-instant` | `gpt-5-6-instant` | 不设 |
| `cursor-g-5.5` | `openai/gpt-5.5-thinking` | `gpt-5-5-thinking` | 不设 |

`model_info.id` 规范:`zerokey-cursor-g-{101|82}-<变体>[-high]`。
注册参数(每条):`api_key: sk-zerokey-web-noop`(假绿①)、`mode: chat`、`weight: 1`、
api_base 指各自 svc `/v1`。

> **xhigh 退役(2026-08-24 Step 4 前门验收发现)**:`xhigh` 经 lane `_CARHER_TE_MAP`
> 映射为 web `thinking_effort=max`,对真身 `gpt-5-6` **确定性返回空 completion**
> (status=completed / output_tokens=0 / reasoning_tokens=0,trivial 与实质 prompt 均空,
> 跨 4 次复打一致)。旧名 `-max` 能"出字"仅因它走**虚构 slug** `openai/gpt-5.6-terra` 触发
> 上游 fallback、max 被丢弃(实为默认档)。故 `max` 是继 terra/sol/luna dot-slug、`-max` 之后
> 发现的**第三个虚构档**——已删两 deployment、从 key03/key04 摘除。**上线档位止于
> `sol`(standard)/`sol-high`(extended),两档实测均正常出字。**

**不上的**:spark(codex 面,已砍)、`research`/`agent-mode`(特殊模式)、全部 `-wm`
(空流,管线消费不了)、`o3` 系(被 5.5/5.6 替代)。

## 三、缺少的关键信息

| # | 缺口 | 处置 |
|---|---|---|
| 1 | 授权范围是否只有 key03/key04 | 默认只授这两个(与旧池一致);有别人用户说 |
| 2 | luna/pro/instant/5.5 上 `thinking_effort` 是否生效 | 不阻塞:先单档,待验后再加档 |
| 3 | Cursor GUI ground truth | Step 9 需用户在 GUI 真点一次,无法替代 |
| 4 | proxy 滚动时间窗偏好 | 默认直接滚(滚动更新零中断) |

## 四、最小可执行步骤

### Step 0 — 前置快照 ✅ 完成(2026-08-24 16:59)

- 输入:198 live 状态。
- 动作:备份 CM `litellm-callbacks`;记录 proxy 就绪数、cursor 族模型计数。
- 验收标准:备份 JSON 可解析、含 hook key;基线数字记录在案。
- **实测结果**:备份 `/Data/backups/litellm-callbacks-pre-cursor-g-20260824-165950.json`
  (576554 字节,31 key,hook present=True)。基线:proxy 4/4;总模型 859,cursor 族 31
  (web:12 gpt:6 fc:6 grok:2 ultra:2 kimi:1 opus:1 composer:1);`cursor-g-*` 现存 0。
- ⚠️ 事故记录:首次备份命令 `kubectl … | { echo 密码 | sudo -S tee … }` 管道打架,落盘
  13 字节=sudo 密码明文(凭据泄漏)。已删除泄漏文件并用「先写 /tmp 再 sudo mv」重做。
  教训:**sudo -S 的密码管道和数据管道不能共用一条 stdin 链**。

### Step 1 — 改 hook gate(唯一共享面)✅ 完成(2026-08-24)

- 输入:CM `litellm-callbacks` 的 `cursor_web_fc_sys_rewrite.py`,
  原值 `_TARGET_PREFIX = "cursor-web-fc-"`。
- 动作:改为 `_TARGET_PREFIX = ("cursor-web-fc-", "cursor-g-")`(str.startswith 原生吃元组);
  `kubectl patch --type merge` 单 key 更新;滚动重启 proxy,`rollout status` 盯到 4/4。
- 预期输出:proxy 全就绪,hook 加载无报错。
- 验收标准:①proxy 日志无 import/语法错误;②打一发非本线模型(acct 池任一名)行为不变;
  ③打一发旧名 `cursor-web-fc-pool-terra` 带 tools,hook 仍 fire(日志有注入痕迹)。
- 回滚:apply Step 0 备份 + 滚动 proxy。
- **实测结果(全 ✅)**:
  - line 63 已是元组、line 128 `startswith(_TARGET_PREFIX)` 原生匹配;CM 31 key 不变;proxy 4/4。
  - ① proxy 4 副本全服务,hook import+运行无 traceback/syntaxerror(日志只有 spend_tracking 截断 INFO 噪声)。
  - ② 临时 scoped key(禁 master,假绿③)打 `deepseek-v4-flash` → 200 + 干净回显 `ZZMARKER2CTRL`,
    且**完全不出现在 rewrite 日志**里 → 非匹配模型 hook 正确不 fire,行为零变化。
  - ③ **活证据优于合成**:grep 到真实生产流量 `cursor-web-fc-82-terra`(tools=19,真 Cursor 客户端)
    在 patch 后跨 4 副本持续 `rewrote 1 carrier(s)`(line 175)→ 旧前缀 `cursor-web-fc-` 被元组匹配、
    hook 正常改写。合成 `cursor-web-fc-pool-terra` via `/v1/responses` 也 200(web 面回 `resp_` id);
    该 minimal payload 无 system carrier → touched=0 未打 rewrite 行(不影响结论)。
  - 副产:注释「裸 getLogger 无 handler 到不了 stdout」**被证伪**——line 175 INFO 实际到 stdout,
    firing 可直接 grep(不必依赖 SpendLogs metadata 戳);SpendLogs `/spend/logs?request_id=` 查单请求
    行本轮未命中(带日期参数返回的是按天聚合行),故 fire 判据以 stdout grep 为准。
  - 临时 key 已删(`deleted_keys:["tmp-step1-accept-verify"]`),零残留。

### Step 2 — 注册 14 个 pool deployment ✅ 完成(2026-08-24)

- 输入:§二表格(7 名 × {101,82} api_base × 真身 slug × effort)。
- 动作:proxy pod 内 `/model/new` ×14;已存在 id 跳过(幂等)。
- 预期输出:14/14 OK(或跳过)。
- 验收标准:DB raw `litellm_params` 逐条核 api_key/slug/effort(不信 /model/info 脱敏视图,
  假绿②);`/model/info` 计数 = 859+14。
- 回滚:`/model/delete` 14 个 id。
- **实测结果(全 ✅)**:
  - 注册 14/14 OK,FAILS none。id 规范 `zerokey-cursor-g-{101|82}-<变体>`(sol/sol-high/sol-xhigh/
    luna/pro/instant/5.5)。
  - **计数**:`/model/info` TOTAL_DEPLOYMENTS=873 = 859+14 ✅;cursor-g 计 14。
  - **slug/effort/api_base**(`/model/info` 对这些字段不脱敏,服务端用真 salt 解密后是权威):
    14 条 model 全 `openai/gpt-5-6|gpt-5-6-t-mini|gpt-5-6-pro|gpt-5-6-instant|gpt-5-5-thinking`
    与 §二逐一相符;sol-high=`high`、sol-xhigh=`xhigh`、其余 eff=None;api_base 全含
    `zero-cursor-bpi`(101→bpi、82→bpi-82)✅。
  - **api_key(破假绿②)**:①横向对照——已知可用 prod 行 `zerokey-cursor-web-fc-pool-101-terra`
    的 api_key 在 `/model/info` **同样是 None** → 证明 None 纯属脱敏 artefact 非真缺失;
    ②DB `LiteLLM_ProxyModelTable` 14 行俱在、api_key/model/effort **密文非空**(杀假绿①空 key);
    ③底层 `decrypt_value(salt=MASTER_fallback)` **2 条干净命中**均为精确明文——
    `cursor-g-101-instant`→`sk-zerokey-web-noop`、`cursor-g-101-pro`→`sk-zerokey-web-noop`
    + `openai/gpt-5-6-pro`;其余 12 条 `Incorrect padding` 是临时 b64 解码路径的填充瑕疵(非数据
    缺陷,已由 /model/info 全 14 条 model/effort 明文正确佐证 salt round-trip 无误)。
  - 结论:14 条全部 api_key=`sk-zerokey-web-noop`、slug/effort/base 名实相符、持久化落库。
  - ⚠️ **事后修正(Step 4)**:其中 2 条 xhigh deployment 已因空产出退役,现存 12 条,
    总模型 873→871。

### Step 3 — 授权 key03/key04 ✅ 完成(2026-08-24)

- 输入:两 key 现有 models 列表(DB 查 token → /key/info)。
- 动作:旧表 ∪ 7 新名,`/key/update` 写回(**整表覆盖坑,必须合并**)。
- 预期输出:两 key 各 +7。
- 验收标准:/key/info 复读,新名在列且旧名一个不少(计数=旧+7)。
- **实测结果(全 ✅)**:目标 = `cursor-liuguoxian03`(46)与 `cursor-liuguoxian04-5rub`(47)
  (与 memory「key03 46/key04 47」吻合;`claude-code-liuguoxian03/04` 是 CC key,非本线,未动)。
  读旧 ∪ 7 合并 `/key/update` 写回:04 47→54、03 46→53,st=200。DB 复读:两 key 各 =旧+7,
  `all7_present=True missing_new=none`,旧名一个不少。
  - ⚠️ **事后修正(Step 4)**:xhigh 退役后从两 key 各摘 1 名 → 04 54→53、03 53→52。

### Step 4 — 前门验收(真 key 路径)✅ 完成(2026-08-24)

- 输入:`/key/generate` 临时 scoped key(只挂 7 新名)。
- 动作:7 个池名各打一发 `/v1/responses` 带唯一暗号(sol 的 -high/-xhigh 也各一发,共 9 发)。
- 预期输出:9/9 HTTP 200 + 暗号逐字回显。
- 验收标准:①200+回显;②lane pod 日志 grep 暗号归属与 WA 日志一致(同 key 钉同线);
  ③proxy WA 日志 MISS→HIT 黏性链;④SpendLogs 落账 status=success。**禁 master key(假绿③)**。
- **实测结果**:
  - **关键发现:xhigh 退役**。首轮 9 发里 `cursor-g-5.6-sol-xhigh` st=200 但**空正文**。
    诊断三段式:①横向对照旧 `cursor-web-fc-pool-terra-max`(同 eff=xhigh)回显正常 → 证伪
    "thinking=max 固有空";②逐字段 diff 两 deployment,**唯一差异是 slug**(旧 max=虚构
    `openai/gpt-5.6-terra`、新 xhigh=真身 `openai/gpt-5-6`,其余 keys 全同);③实质 prompt
    (17×23)复验:sol=`391`✅、sol-high=`391`✅、**sol-xhigh 空**;dump 响应=status=completed
    / output_tokens=0 / reasoning_tokens=0(答案没藏 reasoning,是真没产出)。→ **定论:web 面
    对 gpt-5-6 的 `max` 档产不出正文,是第三个虚构档;旧 max 靠虚构 slug fallback 丢弃 max 才
    "出字"**。已删 `zerokey-cursor-g-{101,82}-sol-xhigh`、从 key03/04 摘除 → 总模型 871、
    cursor-g 12、xhigh 残留 0。
  - 用新临时 scoped key `tmp-step4-accept`(只挂 6 有效名,禁 master)复跑验收 **9/9 st=200
    + 暗号逐字回显**(6 名各一 + sol 追 3 发黏性):sol/sol-high/luna/pro/instant/5.5 全 echo=True。
  - **① 200+回显 ✅**。
  - **② lane 归属 ✅**:grep 两 lane pod——101 线收 8 暗号(sol 的 ZSASOL0/STICK7/8/9 全在)、
    82 线收 ZSAPRO3;与 WA 路由逐一吻合。
  - **③ WA MISS→HIT 黏性链 ✅**(user=`0a137f4f` 即临时 key 身份,全程同一):
    `group=cursor-g-5.6-sol` 首发 **MISS→pick zerokey-cursor-g-101-sol**,随后 **HIT×3 pinned
    同 deployment**(09:55:15/28/32);`cursor-g-5.6-pro` **MISS→zerokey-cursor-g-82-pro**(故
    ZSAPRO3 独立落 82,pro 是独立 group 不违反 sol 黏性);luna/instant/5.5 **MISS→101-***。
  - **④ 落账**:请求层(proxy 200×9)+ 上游层(lane `200 OK`+`[RES] DONE`×9)成功已铁证。
    SpendLogs 亚分钟窗查 **0 条 cursor-g 行**、临时 key `spend=0.0`——这是 **web 订阅额度面的
    预期形态**(不按 token 计费、费用≈0;该线 SpendLogs 历来 dead-end + 批量 flush 延迟),
    非异常;计费正常性已在 §一.5 review 确认,新名与旧池同一 responses bridge 路径。
    (按硬红线:SpendLogs 行数据栏诚实留空,不强主张,判据以请求/上游成功 + 账路径同构为准。)

### Step 5 — 模型身份抽验 ✅ 完成(2026-08-24,证据链组合)

- 输入:Step 4 的 proxy router 日志 + §六 已有 live 探针结论。
- 动作:抽 `model_slug` 验五个真身;若日志不含 slug,用探针脚本(/tmp/slug_probe*.js 系)补发。
- 预期输出:5/5 身份相符。
- 验收标准:model_slug 与 §二注册表逐一相符(尺子有效性已由控制组证明)。
- **实测结果(全 ✅,证据链组合,未重烧订阅额度)**:
  - **本轮 proxy router 日志**逐名证实 cursor-g 名 → 正确 `openai/` deployment slug:
    sol→`gpt-5-6`、sol-high→`gpt-5-6`(eff=high)、luna→`gpt-5-6-t-mini`、pro→`gpt-5-6-pro`、
    instant→`gpt-5-6-instant`、5.5→`gpt-5-5-thinking`(config 层,`Selected deployment` 行)。
  - **§六已 live 铁证**:这 5 个真身 slug 经 /tmp/slug_probe*.js 直连上游、`model_slug` 逐一回显
    (尺子有效性由控制组 gpt-5-5/-thinking/-mini 跟变证明);故"config slug=真身"且"上游 honor"。
  - **本轮端到端行为佐证**:sol 实质答 `391`(gpt-5-6 真算)、sol-high extended 正常、
    luna/pro/instant/5.5 各出文本回显 → cursor-g 路径端到端 honor 真身。
  - lane stdout 不落 `model_slug`,且底层 slug 回显 §六 已具;为守"临时探针用完拆 + 不浪费订阅
    额度"纪律,**未重发探针**(数据已在手,非跳步)。

### Step 6 — 删临时 key + 残留检查 ✅ 完成(2026-08-24)

- 动作:`/key/delete` 临时 key;`/model/info` 无 probe/tmp 残留。
- 验收标准:key 查无此人;模型计数 = 859+14 不多不少。
- **实测结果(全 ✅)**:删 `tmp-step4-frontdoor`+`tmp-step4-accept` st=200(deleted_keys 二枚,
  其中 `0a137f4f…` 正是 Step 4③ WA 日志里的 user 身份,闭环确认);`tmp-step4%` 残留 0。
  `/model/info` **TOTAL=871**(=859+12,xhigh 退役后)、cursor-g 名恰 6:`cursor-g-5.5 /
  -5.6-instant / -5.6-luna / -5.6-pro / -5.6-sol / -5.6-sol-high`;probe/tmp 模型残留 0。
  > 注:计数由计划初的 873 修正为 871——xhigh 两 deployment 退役(Step 4 发现),非残留漏删。

### Step 7 — v2 克隆脚本 ✅ 完成(2026-08-24)

- 输入:`scripts/zk-cursor-web/clone_web_fc_lane.py` + `pool_register.py` + `pool_accept.py`。
- 动作:step3 改注册 `cursor-g-<N>-*` 直连名(真身 slug、幂等跳过);吸收 pool_register 为
  step5(自动入池别名,SUMMARY 用 len 不写死);吸收 pool_accept 为 step6(自动验收);
  docstring 补两前提(新号首次必须手抓 web seed;全部 lane 钉 standby 单 node);
  live token 改 stdin 传(不过命令行,防 ps 泄漏——同 Step 0 事故家族)。
- 预期输出:v2 脚本落仓库,dry-run 可跑。
- 验收标准:dry-run 输出完整计划;语法过;**不实跑**(无新账号,实跑留到下次加号)。
- **实测结果(全 ✅)**:落 `scripts/zk-cursor-web/clone_web_fc_lane_v2.py`(v1 保留当 terra
  冻结era 历史参照,不删)。关键设计:
  - **`VARIANTS` 真身表 6 条,无 xhigh**(sol/sol-high/luna/pro/instant/5.5;slug 逐一取自
    §二铁证表;唯一 effort 档 sol-high=`high`)——彻底剔除 v1 的虚构 `openai/gpt-5.6-terra`
    与 `-max/xhigh` 档(空产出教训固化进 docstring 与 accept 注释)。
  - **双层注册**:step3 `register_direct` 建 6 运维直连名 `cursor-g-<N>-5.6-*`
    (id=`zerokey-cursor-g-<N>-direct-<vkey>`);step5 `register_pool` 把 6 池成员挂进
    用户面别名 `cursor-g-5.6-*`(id=`zerokey-cursor-g-<N>-<vkey>`,WA 自动收编新线)。
    两者均 `/model/new` 幂等(already→SKIP),SUMMARY 用 `len(rows)` 不写死。
  - **grant** 读旧 `/key/info` ∪ 12 新名 `/key/update` 写回(整表覆盖坑),打印 before/after/added。
  - **step6 `accept`** 临时 scoped key(1h、max_budget=2、禁 master 假绿③)打 6 直连名暗号 →
    断言 200+回显 → 删 key;并打印两步人工(WA 黏性 grep + lane 落流 grep)。
  - **登录态两前提写进 docstring**:前提 A `--live-from-ws`(借同号 WS PVC 活 OAuth token,
    经 **ssh stdin** 灌 seed,token 绝不进 argv/远端命令串——py 脚本走 `python3 -c` 内联、
    token 走 `sys.stdin.read()`,防 ps 泄漏);前提 B 全新号首次必须人工手抓 web seed。
  - **拓扑前提**:模板 `nodeName: aiyjy-litellm-standby` + `dnsPolicy:None`(照 bpi 逐字节),
    seed hostPath 只在 standby。
  - **验收执行**:`py_compile` PASS;dry-run 两分支(--live-from-ws 前提 A / 裸 前提 B)均输出
    完整 6-step 计划 + 真身 slug 表,exit 0;**未实跑**(无新账号,留到下次加号)。

### Step 8 — 文档/skill/记忆同步 ✅ 完成(2026-08-24)

- 动作:更新 `docs/cursor-ide-chatgpt-web-status-and-plan-20260823.md`(命名三层/terra 证伪
  /新池)、skill `zk-cursor-web-fc-iterate`(gate 元组/入池清单/真身 slug 表)、memory 索引;
  git commit。
- 验收标准:三处一致,commit 落 main。
- **实测结果(全 ✅)**:
  - status-and-plan 活文档:§0 加命名换装指针 blockquote + 新增 §1.3(6 池别名真身 slug 表/
    xhigh 三段式退役/gate 元组/v2 脚本/计费口径),指向本文档为 canonical。
  - skill `zk-cursor-web-fc-iterate/SKILL.md`:顶部加换装 blockquote + 新增「cursor-g 命名换装
    速查」节(gate 元组、6 池别名真身 slug 表、xhigh 退役=第三虚构档、v2 脚本两前提)。
  - memory `topic_zk_cursor_bpi_web_channel_index`:frontmatter description 加换装维度,正文
    「组池」后插命名换装块(gate/6 名/xhigh 退役/v2 脚本),link 到隔离边界 feedback。
  - **git commit `5febf51`**:**外科提交仅 4 个 cursor-g 文件**(两 doc + v2 脚本 + skill);
    未切 main——当前分支 `codex/litellm-198-gray-rollout` 压着大量无关的 litellm-compact 未提交
    改动(chatgpt-pool-gateway 删除等),脏树切 main 会拖带/冲突,故提交到当前分支(可 cherry-pick
    到 main)。memory 文件在 `~/.claude` 仓库外,不入 commit。

### Step 9 — Cursor GUI ground truth(需用户)⬜(修复已落地,待用户复测)

- 动作:用户在 Cursor 模型名填 `cursor-g-5.6-sol`,真实点一次含工具调用的任务。
- 验收标准:正常出字 + 工具真的动手。前 8 步全绿也替代不了这一步。

#### Step 9 事故与修复(2026-08-24,GUI 首测失败 → 根因三反转 → 已修)

**症状**:GUI 发 "hi"/「快速排序」无响应,后台 `No deployments available … cooldown_list=[zerokey-cursor-g-101-sol, zerokey-cursor-g-82-sol]`,每试必复现。

**根因链(四环,每环有数据,前两次归因是错的)**:
1. **Cursor 对所有自定义模型一律发 `/v1/chat/completions`**(不发 /responses)。证据:①事发 SpendLogs
   traceback 入口帧 = proxy `chat_completion`;②反编译本机 Cursor 3.16.29 `cursor-agent-exec` 的端点
   决策函数:`(baseUrl 为 api.openai.com 且名以 gpt-5 开头) || 名含 codex → responses,否则 chat`,
   且 `/models` 目录 `api_types` 字段可显式覆盖(198 的 /v1/models 不带该字段)。terra 和 cursor-g
   **都发 chat**——"Cursor直发/v1/responses"的旧认知对本线不成立。
2. **旧 terra 名能走通是歪打正着**:litellm `responses_api_bridge_check`(main.py:955)对
   **点分 gpt-5.4+ 版本名** + tools + reasoning_effort 的 chat 请求自动桥接成 responses → 打到 lane
   `/v1/responses`(responses.js 正路)。函数级真值表实测:`gpt-5.6-*`/`gpt-5.5*`(点分)→ BRIDGE。
3. **换装的真身 slug 拆了这座桥**:litellm 版本解析器只认点号,`is_model_gpt_5_4_plus("gpt-5-6")=False`
   (横杠全 False,实测)→ 不桥接 → chat 直达 lane 坏路 chatgpt.js(把 litellm api_key 当 IDE 名查表,
   fallback mapper 缺 `user` 函数)→ `TypeError: user is not a function`(lane 栈帧)→ 500 × litellm
   重试 → 两 deployment 冷却 60s → "No deployments available"。
4. Cursor 本地重试第 5 发赶上冷却结束、且 resume 消息末条非 user 恰好绕开崩溃行 → 只吐 14 token
   垃圾,即用户看到的"成功但没内容"。

**修复(两层配合,只碰 cursor-g + bpi 专属 CM,已验收)**:
- **A. litellm 层**:12 条 cursor-g deployment 的 slug 换**点分载体**(桥接承重):sol/sol-high→
  `openai/gpt-5.6-sol`、luna→`openai/gpt-5.6-luna`、pro→`openai/gpt-5.6-pro`、instant→
  `openai/gpt-5.6-instant`、5.5→`openai/gpt-5.5-thinking`。`/model/update` 全量重建 litellm_params
  (api_key 占位符保住),12/12 OK,复读确认。
- **B. lane 层(名实相符)**:bpi CM `zk-cursor-bpi-patch` 的 `raw.js` ALIASES 加 4 行
  **载体→真实预设**映射:`gpt-5.6-sol→gpt-5-6`、`gpt-5.6-luna→gpt-5-6-t-mini`、
  `gpt-5.6-instant→gpt-5-6-instant`、`gpt-5.5-thinking→gpt-5-5-thinking`(pro 已有现成映射)。
  SOP 六步:锚点断言唯一性 + node --check + CM 备份
  `/Data/backups/zk-cursor-bpi-cm-20260824-200228-pre-cursor-g-alias.json` + merge patch(12 key
  校验)+ 滚动两 lane。**旧名零变化**:`gpt-5.6-terra` 依旧原样透传(live 复核)。
- **验收(标准已修正,见下)**:临时 scoped key 按 **Cursor 真实线型**(`/v1/chat/completions` +
  stream:true + tools + reasoning_effort + user 结尾)打 6 变体 → **6/6 200+暗号逐字回显**
  (6.6–8.4s);lane 重启后 **TypeError=0、[RES] DONE×12**(全走桥→responses.js 正路);live pod
  `resolveModel` 函数真值 4 载体全中、旧名全原样。
- 诚实边界:①本轮探针因 key 级亲和全落 82 线,101 线未直接吃到端到端流量(同 CM 同代码,机制层
  由 live 函数真值佐证);②各变体上游实跑模型未重新烧探针回显(载体→真身映射是确定性代码 + 真身
  slug 上游 honor 已由 §六 昨日 4/4 slug 回显铁证),最终以 GUI ground truth 收口;③桥接触发依赖
  客户端带 reasoning_effort(Cursor agent 实测恒带——terra 能通即证明;若未来 Cursor 不带,chat
  坏路仍在,备选加固=RAW_IDES 加占位 key 让误入流量走 raw 透传,未做)。

#### 反思:为什么走偏(对着本文档 review,2026-08-24)

1. **验收标准错了(主因)**。Step 4 用 `/v1/responses` 合成探针验收——但真实 Cursor 发的是
   `/v1/chat/completions`。**验的不是真实流量走的那条路**,9/9 全绿全是假绿。讽刺的是红线早就写在
   §3:「前门全绿 ≠ Cursor GUI 能过」、skill 里「合成探针全绿 ≠ 真流量能过」——我把 Step 9(GUI)
   当成"最后补一下的手续",而不是把 Step 4 的探针**设计成复刻真实线型**。教训固化:**验收探针的
   形状必须逐字段对齐真实客户端的线型(端点/stream/tools/effort/消息形状),否则不算验收**。
2. **换装前提没做全链路审计**。计划把"虚构 slug→真身 slug"当纯正名操作,没有先回答"旧名为什么能
   通"——虚构 slug 恰是触发 litellm 桥接的承重件。§三"缺少的关键信息"里从来没有"Cursor 实际发哪个
   端点/litellm 内部走哪条路"这一行,它是未经检验的假设。教训:**改名/换 slug 也要先画请求全链路,
   每一跳标"实测/假设"**。
3. **诊断期连续三次无据归因**(litellm 桥接方向说反两次、"Cursor 对不同名字发不同端点"),都是
   拿部分数据编完整故事。入口铁证(traceback 首帧=chat_completion)一直躺在 SpendLogs 里,却最后
   才去拉。教训:**"X 导致 Y"先拉请求入口的原始记录(traceback/access log),再谈机制**。

## 五、风险与回滚总表

| 步骤 | 爆炸半径 | 回滚 |
|---|---|---|
| Step 1(hook) | 全 proxy 共享面,但双 gate+fail-open,只有两前缀+带 tools 的请求被改写 | apply CM 备份 + 滚 proxy |
| Step 2(注册) | 新名字自身,现有路由零变化 | /model/delete 14 id |
| Step 3(授权) | key03/key04 | /key/update 写回旧表 |
| 其余 | 只读/仓库文件 | git revert |

## 六、事实 vs 推测

**事实(live 实测,2026-08-24)**:
- **Cursor 3.16.29 对本线所有名字一律发 `/v1/chat/completions`**(反编译端点决策函数 + 事发
  traceback 双证);litellm `responses_api_bridge_check` 只对**点分 gpt-5.4+ 名**+tools+effort
  桥接到 responses(函数真值表);横杠真身 slug 解析 False → 直達 chat 坏路崩(Step 9 事故)。
- terra/sol/luna dot-slug 直发上游会静默降级 `gpt-5-6`;`model_slug` 尺子有效(控制组
  `gpt-5-5`/`-thinking`/`-mini` 逐一跟变);`-wm` 变体空流。**但 dot-slug 在 litellm 层是桥接
  承重件**(Step 9)——"虚构名"在两层各有一真一假两重身份。
- 两账号(acct101/acct82)`/backend-api/models` 清单一致(均 Pro,20 slug);
  luna 真身=`gpt-5-6-t-mini`、sol 真身=`gpt-5-6`(官方 title 佐证)。
- 新增 4 slug(pro/instant/t-mini/5-5-thinking)经管线 4/4 出流+slug 回显。
- 计费正常;组池/WA/fail-over 已落地验收;clone 模板与 live 零漂移(args/env/CM 12 key
  逐字节比对);hook 双 gate+fail-open。
- Step 0 基线与备份(§Step 0 实测结果);首次备份事故与修复(密码泄漏文件已删)。
- 本轮调研消耗:acct101 约 12 条网页消息(4+4+4 探针),临时 key/探针模型已清零。

**推测(不当结论用)**:
- luna/pro/instant/5.5 上 `thinking_effort` 生效与否未验(先单档)。
- pro 真实编码负载下"明显慢"从机制推得(探针 3.3s 因问题太短),未实测长任务。
- hook 改元组后在 proxy 里的实际行为,以 Step 1 验收②③为准(语言层 startswith 吃元组
  是事实,该文件上下文里的行为要实测)。
- "老用户零感知"依赖旧名零触碰的构造推理,Step 1 验收②③即其证伪测试。
