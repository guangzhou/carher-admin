# prepare-values / chart 对真实 prod Deployment 的彩排（2026-09-13）

> 目的：在升级窗口**之前**把 `prepare-values.py` 与 chart 会在当天硬红的地方全部跑出来。
> 做法：把今天生产的 `litellm-proxy` Deployment 原样取下（`-n litellm-product`，0600，
> 用完即删），喂给 `prepare-values.py` 自己的 `load_deployment` / `main_container` /
> `freeze_runtime_shape`，以及 `helm template`。
> 全程只读，不 apply、不 patch，不打印任何变量值。

结果：**4 条闸门问题 + 1 条结构性阻塞**。前两条是闸门自己的缺陷（已修），
第三条是无理由的建模约束（已改），第四条是真实冲突（需裁决），
第五条是**当时工具链完全测不到的静默失效路径（🔴 仍阻塞收敛）**：
尺子已经补上（§5.1 `check-pod-spec-shape.py`，已用真实 prod 立过阳性对照，红），
chart 也已改到能表达现网挂载形状（§5.2：`subPath` / `callbacks.mountPaths` / `postStart`），
但 prod 的挂载清单还没誊进 values、审批文件还没写，所以第 8 步依旧禁止执行。

---

## 1. `CHATGPT_TOKEN_DIR` 被误判为内联凭据 —— 闸门假阳（已修）

```
FAIL freeze_runtime_shape: environment variable CHATGPT_TOKEN_DIR contains an inline credential
```

`SENSITIVE_NAME_RE` 匹配的是**变量名**，`CHATGPT_TOKEN_DIR` 命中 `_TOKEN_`，
而它的值是一个绝对目录路径。全量扫描 27 个字面量 env，只有这一个命中名字规则，
**没有任何一个命中值规则** `INLINE_SECRET_RE`。

修法（`scripts/prepare-values.py`）：值先过 `INLINE_SECRET_RE`（这条不放松）；
名字规则命中时，若值是 `is_plain_filesystem_path()`（以 `/` 开头、只含路径安全字符）
则放行。`Bearer …`/`sk-…`/DSN/URL 都进不了这个形状。

## 2. `{name: X}` 无 value 的 env 被判「unsupported value source」—— 闸门缺口（已修）

生产有两个：`UA_ROUTE_KEY_ALIASES`、`UA_ROUTE_KEY_PREFIXES`，结构就是 `{"name": "<str>"}`。
这是**合法的 Kubernetes 写法，语义是空字符串**。旧逻辑既不认 `value` 也不认 `valueFrom`，
直接 fail。这是缺口不是发现：空串不可能是凭据，而悄悄丢掉该变量会改变容器环境。

修法：把 `{name}` 归一成 `{name, value: ""}` 再走正常校验。

## 3. readiness 与 liveness 时序必须相等 —— 无理由的建模约束（已改）

```
FAIL freeze_runtime_shape: readiness and liveness timing must match the chart probe contract
```

实测现网：

| | initialDelay | period | failureThreshold | timeout |
|---|---:|---:|---:|---:|
| readiness | 60 | 10 | 12 | 5 |
| liveness | 180 | 30 | 10 | 8 |

旧 chart 只有一个 `probes` 块，`deployment.yaml` 把它 `toYaml` 给两个探针共用 ——
这是 `toYaml` 复用的副产物，schema 与模板里**都没有写过任何理由**。
强行取齐是对线上探针的一次真实行为变更：readiness 要快（尽早摘流量），
liveness 要慢（别在冷启动/GC 停顿时误杀），k8s 故意把两者分开。

修法：`probes` 拆成 `probes.readiness` / `probes.liveness`（schema 用 `$defs/probeTiming`
共用字段定义），默认值直接写成现网实测值。`freeze_runtime_shape` 分别冻结两套，
并对缺字段报明确的错。

⇒ 三条修完后彩排全绿：

```
PASS  load_deployment (exactly one Deployment)
PASS  main_container (exactly one container named litellm, no initContainers)
PASS  freeze_runtime_shape (env/envFrom/probes/lifecycle)
   secretRefs: ['litellm-secrets', 'carher-env-keys']
```

## 4. lifecycle / grace 冲突 —— 真实冲突，但**fail-closed**（需裁决，不需担心静默）

现网：`lifecycle` 只有 `postStart`（`python3 /patches/patch.py`），**没有 preStop**；
`terminationGracePeriodSeconds: 30`。chart 要求 preStop `sleep 30` 且 `grace >= 30+570`。

两条都实测确认会**响亮地失败**，不会静默通过：

| 喂进去的现网值 | 结果（实测） |
|---|---|
| `terminationGracePeriodSeconds: 30` | `helm template` 报 `terminationGracePeriodSeconds=30 is below drain.preStopSeconds+drain.streamDrainSeconds=600` |
| `lifecycle` 只有 postStart | values.schema.json 报 `at '/lifecycle': missing property 'preStop'` + `additional properties 'postStart' not allowed` |

含义有两层：

1. **今天的 prod 没有 preStop、grace=30** ⇒ 每次滚动更新，在途 SSE 在 30 s 后就被砍。
   这与 `docs/drain-budget-evidence-2026-09-13.md` 的结论一致，采用 chart 其实是**改善**。
2. 但 chart 的 schema **完全禁止 `postStart`**，而 prod 的 postStart 正是两层补丁的装载入口。
   ⇒ chart 必须放开可选的 `postStart`（保留 preStop 的强等式），否则 prod 无法被表达。
   **已实现，见 §5.2。**

   ⚠️ 改完之后重测（2026-09-13）：只写 `lifecycle.postStart`、完全不提 preStop 的 values
   文件，`helm template` **rc=0** —— 因为 helm 把用户值**递归合并**在 chart 默认值之上，
   `lifecycle.preStop` 直接从 `chart/values.yaml` 继承。所以上表第二行的两个错误现在
   一个都不会出现。这不是表达能力问题了，而是第 1 点那个**真实行为变更**：
   采用 chart ⇒ prod 凭空多出一个它今天没有的 preStop 和 600 s grace。这条要裁决。

### 4.1 这条裁决的数已经量过了 —— 它是**减少**截断，不是新增风险

裁决听起来像是"要不要给 prod 引入一个新行为"，但 `drain-budget-evidence-2026-09-13.md`
§3.1 的超阈分布已经把两边的代价都量出来了，只是没被接到这条裁决上。接上之后取舍是单向的：

| | 今天的 prod | 采用 chart 之后 |
|---|---|---|
| preStop | 无 ⇒ SIGTERM 立刻打给还在吐字的进程 | `sleep 30`，先从 endpoints 摘掉再让它死 |
| grace | **30 s** | **600 s** |
| 滚动更新时被砍的在途流 | 所有 > 30 s 的 | 所有 > 570 s 的 = **0.0940%** |

`> 30 s` 那一档**没有直接量过**——§3.1 最低一档是 60 s。但阈值越低超阈条数只增不减，
所以 `> 30 s` 的条数 **≥ 60 s 档的 8,129 条 / ≥ 0.6537%**。这是单调性给的**下界**，
不是估算；真值只会更大（`ALL /pro` 的 p99 = 37.74 s 就已经越过 30 s 了）。

⇒ 采用 chart 把每次滚动更新的截断面从 **≥0.6537%** 压到 **0.0940%**，
**至少缩到七分之一**，且受影响消费者不变（Codex Desktop / codex-tui 打 `/pro/v1/responses`）。
按「最小限度影响线上正式用户」这条判据，**采用 chart 的排水几何是更优的一侧**。

两点必须同时说清，否则这条结论会被读成"零截断"：

1. **它不消除截断，只是把门槛从 30 s 抬到 570 s。** 抬到覆盖 p100 需要 grace = 22.7 小时，
   那条路 `drain-budget-evidence` §1 已经否掉了（Terminating pod 挂一整天，害处更大）。
2. **它带一个配套的真实变更**：现网 nginx `/pro` 的 `proxy_read_timeout` 是 600 s，
   必须改成 **570 s**，否则出现"nginx 还在等一个已经被 SIGKILL 的 Pod"。
   见 `drain-budget-evidence-2026-09-13.md` §4/§5——**这一条本身就是待办，不因本节而消失。**

⚠️ 仍然需要人拍板的是**第 2 点那次 nginx 改动**（动的是现网正式流量入口），
而不是"要不要 600 s grace"。后者在数据面前没有第二个合理选项。

## 5. 🔴 阻塞：删除集 gate 是**对象级**的，而补丁机制是**挂载级**的

升级方案第 8 步用 `helm upgrade --reset-values` 把 prod 换到新 chart，前置 gate 是
`scripts/check-release-deletion-set.py`。该脚本逐对象比对 live manifest 与目标渲染，
对消失的 ConfigMap/Service/PDB fail-closed。**它不读 Pod spec 内部** ——
全文没有 `volumeMount` / `volumes` / `lifecycle` / `postStart` 任何一个词。

而生产的能力几乎全部活在 Pod spec 内部。实测今天的 `litellm-proxy`：

| 卷 | 挂载数 | 其中 subPath 单文件 |
|---|---:|---:|
| callbacks | 33 | 33 |
| config | 1 | 1 |
| hooks | 1 | 1 |
| streaming-handler-patch | 1 | 1 |
| passthrough-streaming-handler-patch | 1 | 1 |
| anthropic-logging-patch | 1 | 1 |
| chatgpt-noauth | 1 | 0（整目录 `/chatgpt-noauth`） |
| deepcopy-patch | 1 | 0（整目录 `/patches`） |
| **合计** | **40** | **38** |

其中 **4 处直接覆盖上游库文件**（`site-packages/`）：

- `litellm/proxy/pass_through_endpoints/streaming_handler.py`
- `litellm/proxy/pass_through_endpoints/llm_provider_handlers/anthropic_passthrough_logging_handler.py`
- `litellm/litellm_core_utils/streaming_handler.py`
- `sitecustomize.py`

而新 chart 能表达的只有：`config`（1 个文件）、`callbacks.data`（按 key 挂到 `/app/<key>`）、
`additionalSnapshots`（**整目录，没有 subPath**）。对照下来（此表是 §5.2 改动**之前**的状态，
现已全部转绿，见 §5.2）：

| 现网形状 | 改动前 | 改动后（§5.2） |
|---|---|---|
| 4 个 patch CM 的单文件 subPath 覆盖 | ❌ `additionalSnapshots` 只能整目录挂载 | ✅ `additionalSnapshots[].subPath` |
| `hooks` → `/app/register_pricing.py` | ❌ 同上 | ✅ 同上 |
| `callbacks` 的 `sitecustomize.py` | ❌ chart 规则会挂到 `/app/sitecustomize.py`，现网在 `site-packages/` 下 | ✅ `callbacks.mountPaths` per-key 覆盖 |
| `/chatgpt-noauth`、`/patches` 整目录 | ✅ `additionalSnapshots` 可以 | ✅ 不变（`subPath` 是可选的）|
| postStart `python3 /patches/patch.py` | ❌ schema 明令禁止 `postStart` | ✅ `lifecycle.postStart` 可选 |

**失败形状（这才是要害）：**
这些 ConfigMap **对象一个都不会被删**（它们不由该 release 拥有，或仍被渲染），
所以 `check-release-deletion-set.py` **读出绿**；Deployment 正常收敛，
`kubectl rollout status` **读出绿**；容器起得来、/health 返回 200、探针**全绿**。
唯一变化是：所有运行时补丁**没有被挂载**，于是**全部静默失效**。

⇒ 三把尺子（对象删除集、rollout status、健康探针）**没有一把能看见这次失效**。
`prepare-values.py` 更是压根不读 `volumes`/`volumeMounts` —— 它只冻结
command/args/lifecycle/env/secretRefs/resources/调度/grace/probes。

**这一条在解决之前，不得执行方案第 8 步的 prod 收敛升级。**

### 5.1 尺子已补上：`scripts/check-pod-spec-shape.py`（已实现，已用真实 prod 立过阳性对照）

与删除集 gate 同构：只读、fail-closed、要审批文件、输出 0600 证据 JSON。比对项：

| 比对项 | key | value |
|---|---|---|
| volumeMounts | `container/<名>/mount/<mountPath>/<subPath>` | `<源 kind>/<源名>|ro\|rw` |
| volumes | `volume/<名>` | `<源 kind>/<源名>` |
| lifecycle | `container/<名>/lifecycle/{postStart,preStop}` | hook 的 JSON |
| command / args | `container/<名>/{command,args}` | JSON |
| env / envFrom | `container/<名>/env/<变量名>` | 恒为 `set` |

两个设计点值得单独记：

1. **挂载源进 value 而不是 key**。同一个 `mountPath` 背后换了一个 ConfigMap 是一次静默
   换料；如果只按「在/不在」比对，这两条会被配成一对、判成没变。
2. **env 只读名字，值一个字都不读**。所以 diff、证据文件、审批文件都可以直接贴进评审，
   不需要脱敏，凭据不可能漏出去。

**阳性对照（实测，不是推演）**：拿今天真实的 `litellm-product/litellm-proxy`
（`kubectl get deploy -o yaml`）对 `helm template` 出来的 prod 目标渲染跑一次：

```
status FAIL   errors ["UNAPPROVED_SHAPE_CHANGES"]
counts {live: 80, target: 10, removed: 73, changed: 3, added: 3, mount_removals: 39}
```

拆开看正好落在 §5 说的那些点上：`mount_removals` 39（含 4 个 `site-packages/` 覆盖）、
`container/litellm/lifecycle/postStart` 在 removed 里、6 个补丁 volume 全在 removed 里、
`/app/config.yaml` 落在 changed（同路径换了 ConfigMap）。快照用完即删。

**它自己的假绿也堵上了**：两种喂错方式都会 fail-closed，实测过——

| 喂法 | 结果 |
|---|---|
| live 侧喂渲染（例如 `helm get manifest`） | `LIVE_SIDE_IS_NOT_A_LIVE_OBJECT` |
| target 侧喂活对象 | `TARGET_SIDE_IS_NOT_A_RENDER`（且 removed=0，本来会是一片干净的绿）|
| live 侧一个 volume 都没有 | `LIVE_SIDE_HAS_NO_VOLUMES`（这是采集坏了，不是线上不挂东西了）|

⚠️ live 一侧必须是 `kubectl get deploy -o yaml`。`helm get manifest` 是 **helm 以为自己
apply 过的东西**，而 `litellm-proxy` 只用 `set image`/`patch`、从不 `apply`，两者会漂。

#### 5.1.1 内容摘要：不然这把尺子会被自己逼成橡皮章（SCHEMA_VERSION 2）

§5.2 的 chart 改动落地之后马上冒出一个新问题：**chart 对自己的 ConfigMap 做内容寻址命名**
（`<release>-<snapshot>-<checksum>`），而现网的 ConfigMap 是手写名字。于是同一份字节换个名字
出现，40 条挂载会**全部**落进 `changed`——审批文件变成 40 行「是的，同一份字节」。
那正是本文档反复警告的形状：**拿审批去掩盖**。一张每次都必须签 40 个名的表，签的人会闭眼签。

修法：可选地喂进 ConfigMap 快照（`--live-configmaps` / `--target-configmaps`，后者默认取
`--target`），工具只对**挂载进去的字节**算 sha256，字节确实相同的差异标成 `inert`，
不进 `needs_approval`。三条 fail-closed 性质，每条都用**破坏性改动跑过一次**验证它真的在拦：

| 性质 | 为什么必须是这样 |
|---|---|
| 不喂快照 ⇒ **什么都不 inert** | 工具行为原样退回旧版。缺省是"多签几个名"，不是"少看几处" |
| **removed 的挂载永远不能被内容豁免** | 没有任何摘要能替"这个补丁根本没被挂载"开脱——那正是这把尺子存在的理由。真出现就报 `INTERNAL_MOUNT_REMOVAL_MARKED_INERT` |
| live 侧喂渲染出来的 CM ⇒ 硬红 | 这是比"拿渲染当 live"更毒的一版：它不只读出绿，它把一次**真实换料**证明成"无差异"。两侧各查 `uid`/`resourceVersion`，错了报 `LIVE_CONFIGMAPS_ARE_NOT_LIVE_OBJECTS` / `TARGET_CONFIGMAPS_ARE_NOT_A_RENDER` |

另有三条细节，每条都对应一种会把红读成绿的写法：

- **subPath 挂载按 key 摘要，不按整个 ConfigMap**。一次 subPath 挂载只暴露一个 key：两个
  ConfigMap 在**没挂载的 key** 上不同，对这条挂载而言就是同一份；反过来，挂载的那个 key 变了，
  也不许躲在"整对象摘要因别的原因动了"后面。
- **内容相同不代表 ro/rw 相同**。value 的后半段（`ro`/`rw`）单独比；一个补丁挂载变成可写是真变更。
- **Secret 一个字节都不摘要**。低熵 secret 的摘要是可爆破的产物，Secret 支撑的挂载永远只按名字比。

新增输出：`inert_by_content`、`renamed_volumes`（只在摘要 1:1 配得上时才配对，含糊就不配），
`removed`/`changed` 每条多一个 `"inert"` 布尔，counts 多 `inert_by_content` / `renamed_volumes` /
`live_configmaps` / `target_configmaps`。快照含 ConfigMap 全文 ⇒ 0600、用完即删；
**证据 JSON 里只有 sha256，没有内容**。

### 5.2 chart 已能表达现网形状（已实现）

尺子有了不等于能过：上面那次阳性对照红在 73 项，其中绝大多数**不是「该删」而是
「chart 根本写不出来」**。三条 chart 改动已落地：

| 改动 | 位置 | 为什么 |
|---|---|---|
| `additionalSnapshots[].subPath`（可选） | `values.schema.json` + `deployment.yaml` | 覆盖那 38 个单文件挂载。不给 subPath 就是**整目录挂载**，会把上游包目录整个盖掉，而不是换掉里面一个文件 |
| `callbacks.mountPaths`（per-key 覆盖，可选） | 同上 + `_helpers.tpl` `callbackMountPath` | 现网 `sitecustomize.py` 在 `site-packages/` 下；chart 原来硬编码 `/app/<key>`，挂到 Python 根本不 import 的地方 |
| `lifecycle.postStart`（可选，`exec` 形状与 preStop 同源 `$defs/execHook`） | 同上 | 现网 postStart `python3 /patches/patch.py` 是两层补丁的**装载入口**；没有它 = 补丁全挂载、全不加载 |

`preStop` 的强等式（`sleep <drain.preStopSeconds>`）一个字没动，postStart 是纯加法。

**顺手补上的四个 fail-closed 校验**（都实测过红，不是推演）——新增能力自己会带出新的静默形状：

| 喂法 | 报错 | 静默后果（如果不拦） |
|---|---|---|
| `subPath` 不是该 snapshot `data` 的 key | `additionalSnapshots <名> subPath <k> is not a key in its data` | kubelet 把**空路径**盖在目标上：上游文件被换成空文件 |
| `callbacks.mountPaths` 的 key 不在 `callbacks.data` 里 | `callbacks.mountPaths references a key that is not in callbacks.data` | 整条覆盖是 no-op，但读起来像配好了 |
| callbacks 路径与某个 snapshot `mountPath` 撞 | `duplicate additionalSnapshots mountPath: <p>` | 一个路径两个 volumeMount，kubelet 挑一个、一声不响 |
| callbacks 路径撞 `/app/config.yaml` | `callbacks mountPath collides with config` | 同上，且撞掉的是 LiteLLM 主配置 |

另有两条走 schema：`mountPaths` 的值必须绝对路径（相对路径会悄悄相对 workdir 解析）；
`subPath` 只允许 `^[A-Za-z0-9._-]+$`，所以 `../../etc/x` 这种越出卷的写法进不来。

⇒ 现在 chart **能**表达现网形状了。**但第 8 步仍然禁止执行**，剩下的不是表达能力问题：

1. prod 的 values 文件还没有把这 40 个挂载真正写进去（`prepare-values.py` 只冻结
   command/args/env/probes/lifecycle，**不读 `volumes`/`volumeMounts`**——挂载清单要人工从
   现网 40 条逐条誊过来，每条带源 ConfigMap 的内容快照）；
2. 誊完之后重跑 2c，红会收敛到「真的要删/要改的那几项」，那时才轮到写审批文件。

顺序不能反：**先让 chart 能表达（已完成），再誊挂载清单，最后写审批**；
反过来等于拿审批去掩盖表达不出来。

### 5.3 三步已全部走完，2c 已收敛为 PASS（2026-09-13 实测）

按 5.2 的顺序全跑了一遍真实 prod（`litellm-product/litellm-proxy`，generation 847）。
**注意：下面每一条都是拿真 artefact 量出来的；先前 276 条测试全绿，但没有一条能发现第 6 节
那六个缺陷** —— 这一节的价值不在"红变绿"，在于"绿是假的"这件事只有真数据能说出来。

链路（单次跑通，四步）：

| 步 | 结果 |
|---|---|
| `collect-scheduler-evidence.py` | `{"mode":"primary","nonzero_observations":[],"profile":"prod","status":"PASS"}` |
| `prepare-values.py` | `{"callbacks":33,"mounts":40,"snapshots":6,"shadowed_chart_keys":["callbacks.README.txt"],"status":"PASS"}` |
| `helm template -n litellm-product --set terminationGracePeriodSeconds=600` | rc=0；40 挂载 / 38 subPath / 8 卷 / `lifecycle:[postStart,preStop]` / callbacks CM 33 键且 README 不在 |
| `check-pod-spec-shape.py --approval` | **`status PASS`**，`unapproved_shape_changes: []`，`stale_approvals: []`，`errors: []` |

红的收敛轨迹：**73 → 48 → 12 → 10 → 0**。
每一次下降都对应一个被修掉的真实缺陷，不是放宽判据：

- 73→48：chart 补上 `subPath` / `callbacks.mountPaths` / `postStart`（§5.2）
- 48→12：`--live-configmaps` 的 `kind: List` 解包（§7.3）+ ConfigMap 模板字节修复（§7.1）
- 12→10：CRLF 保真（§7.2）
- 10→0：审批文件 `k8s/prod-pod-spec-approval.json`

`mount_removals: 0` —— 现网 40 个挂载**一个都没丢**。`inert_by_content: 38`：38 项虽然
ConfigMap 改了名，但挂进去的字节被证明逐字节相同，所以自动判为惰性，不需要人签字。
这正是 §5.1.1 要的效果：**剩下需要人看的只有 10 项，而不是 40 张"是的，同样的字节"的橡皮章。**

那 10 项（已写进 `k8s/prod-pod-spec-approval.json`，`approver` 留 `<FILL-APPROVER>`，
**未签字时闸门仍然红**）：

| 项 | 数量 | 判据 |
|---|---|---|
| 9 个回调挂载 `readOnly: false → true` | 9 | **只是 spec 字段变化**。在现网 pod 里实测 `/proc/mounts`：40 个挂载**全部**是 `ro`，`rw,` 零命中 —— kubelet 对 ConfigMap 卷本来就只读挂，容器从来没有过写权限可丢 |
| `volume/config` | 1 | chart 的内容寻址 config CM 只带被挂载的 `config.yaml`；现网 `litellm-config` 另有 `raw.js`(15284B)/`responses.js`(18904B)/`web-tools.js`(16807B) 三个**没有被本 Deployment 挂载**的键。唯一的挂载 `subPath config.yaml` 已判惰性；`litellm-config` 不属于本 release、升级不会删它，其它消费者照旧读原对象 |

⚠️ **`readOnly` 那 9 项没有顺手放宽闸门**。"实测都是 ro"是**这一次**的批准理由，
不是"这类差异不用管"的通则 —— 闸门保持严格，下次还会红，还得重新量一次再签。

⚠️ 审批文件**不是一次写好永久有效**：`check-pod-spec-shape.py` 会把审批清单和当天实际
diff 对账，对不上就报 `APPROVAL_DOES_NOT_MATCH_DIFF`。窗口当天必须重跑，
**对不上时改的是调查方向，不是这张清单**。

---

## 6. 只有真 artefact 才暴露得出来的六个缺陷（全部已修）

§1~§5 是"闸门判错"，这一节是"**工具本身会把生产字节改掉 / 根本渲染不出来**"。
六个全部是 2026-09-13 用真实 prod 快照跑出来的，六个在 276 条测试里**一个都不红**。

为什么测试全绿：测试夹具**全部是纯 ASCII、全部是 LF、没有一条端到端渲染过**。
这三条正好是六个缺陷的盲区。**"测试通过"证明的是夹具的形状，不是产物的形状。**

| # | 缺陷 | 位置 | 症状 |
|---|---|---|---|
| 6.1 | chart 默认的占位 `callbacks.data["README.txt"]` 被重新合并进每一份冻结 values | `prepare-values.py` | 渲染出 **34** 个键（应为 33），占位文件会被挂进生产容器 |
| 6.2 | `callbacksSha256` 的编码永远等不了 `toRawJson` | `prepare-values.py` | 真 artefact **无条件** `helm template` 失败 |
| 6.3 | `runtimeSha256` 的载荷 + 编码都与 chart 不一致 | `prepare-values.py` + chart + schema | 同上 |
| 6.4 | 调度器证据文件**在本仓库里没有生产者** | 全仓库 | 操作员只能从报错里抄摘要，直到闸门不叫 —— 橡皮章 |
| 6.5 | `--live-configmaps` 从它自己文档里那条采集命令中**一个对象都解析不出来**，且不报错 | `check-pod-spec-shape.py` | 内容摘要功能形同没有，48 张"同样的字节"要人签 |
| 6.6 | ConfigMap 模板把**每一个文件的字节**都改了 | `chart/templates/configmaps.yaml` | 见 §7.1 |

### 6.1 Helm 的 map 合并是**加法**

用户 values 盖在 chart 默认之上，但对 map 是递归+加法：**只在 `chart/values.yaml` 里出现
的键会活着进渲染**，哪怕冻结文件从没提过它。实测：带 prod 33 个回调的 values 文件渲染出
34 个键。写 `README.txt: null` 才能删掉（实测 33 个、README 不在）。

修法 `shadow_chart_default_keys()`：把本次没冻结的 chart 默认内容键显式置 null。
**没有交给 checksum 去兜** —— 今天它恰好会撞坏 `callbacksSha256` 所以叫得响，
但那是另一个目的的校验和的副作用；**任何 checksum 没覆盖到的 chart 默认值都会静默混进来**。

### 6.2 / 6.3 `toRawJson` 的等价物只有一个

Helm 的 `toRawJson` == `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",",":"))`。
`ensure_ascii` 是陷阱：**prod 33 个回调里有 30 个含非 ASCII 字节**，默认的 `ensure_ascii=True`
会转义成 `\uXXXX`，摘要永远不可能相等。全部夹具是纯 ASCII ⇒ 测试永远看不到。
现在全文件只有一个 `raw_json_sha256()`，**没得选，也就无从漂移**。

`runtimeSha256` 另有一层：chart 只对 `{args,command,extraEnv,secretRefs}` 四个键重算，
而 `prepare-values` 把 Secret 快照也折了进去 ⇒ 真 values 文件永远渲染不出来。
Secret 快照（uid + resourceVersion + 每个 Secret 的 data 摘要）改为自带一个
`schedulerSafety.secretMetadataSha256`：**chart 验不了它**（chart 看不到 Secret 内容），
只拒绝占位值；但直接丢掉会让 `--secret-metadata` 变成装饰，冻结 values 也就不再和它被
审计时的那个 Secret 版本绑定。

### 6.4 证据没有生产者 = 注定的橡皮章

`--scheduler-evidence` 要的 JSON 里，`callbacks_sha256` 覆盖 33 个文件，
`runtime_sha256` 是 Helm 自己的编码，`source_payload_sha256` 摘要其余部分 —— **没有一项是
人能手算的**。新增 `scripts/collect-scheduler-evidence.py`：

- **摘要**（机械绑定）：调 `prepare-values.py --emit-bindings` 取，不做第二份实现 ——
  第二份实现就是第二个会漂的东西；这个绑定的全部意义就是"等于一分钟后 prepare-values 会算出的那个"，
  所以唯一安全的生产者就是 prepare-values 本身。**这不是循环论证**：摘要只断言 artefact 身份，
  对集群零断言。
- **`observations`**（集群事实）：三个计数**必须显式传**，`--source` 必须记下怎么量的
  （`clone:<id>` / `direct:<id>`）。**没量过的 0 比没有证据更糟，因为它读起来是干净的绿。**
- 非零计数不由本脚本裁决（它只记录），`prepare-values.py` 对任何非零 fail-closed —— 停在该停的地方。
- 拒绝覆盖已存在的证据文件：陈旧证据悄悄变成新鲜证据，正是这道闸门要防的。

---

## 7. 三个"工具改掉生产字节"的缺陷（全部已修）

这三个比闸门判错严重一个量级：它们不会让你看到红，它们**让你部署一份字节不同的东西**。

### 7.1 ConfigMap 模板把每个文件都改了字节

原来三处都是手搓块标量：

```yaml
{{ $key }}: |-
{{ $value | nindent 4 }}
```

`nindent` 在块标量体内**会先补一个换行**，而 `|-` **会砍掉结尾换行**。
净效果：每个文件开头多一个 `\n`、结尾少一个 `\n`、**长度不变** ——
所以"长度一样"这种朴素比对也发现不了。实测 33 个回调**全部**被改。

改成 `{{- toYaml <map> | nindent 2 }}`，让 YAML 发射器自己按每个值挑标量样式。

⚠️ 这是最值得记的一条：**它挂载的是覆盖 `site-packages` 里模块的补丁文件**。
Python 对行尾无所谓 ⇒ 不会崩、不会报错，**只是跑的不再是被审计过的那份文件**。

### 7.2 `Path.read_text()` 把 CRLF 悄悄变成 LF

Python 文本模式带 universal newlines。在一个**唯一职责就是逐字节冻结生产内容**的工具里，
这是静默内容篡改。实测：现网 `litellm-passthrough-streaming-handler-patch` 的
`streaming_handler.py` 真的是 CRLF（14016 B），经 `read_text()` 读回来是 13728 B 的 LF。

修法：`read_bytes().decode("utf-8")`。已确认三个内容读取口（snapshot 目录 / callbacks / config）
全部走这条路径。另外验证过**磁盘上的采集本身一直是忠实的**（从 `cms.json` 逐字节重建
`/tmp/prodsnap/` 报告"无差异"）—— 篡改只发生在工具里。

### 7.3 `kubectl get configmap -o yaml` 是 `kind: List`，不是文档流

`load_configmaps` 按多文档流解析，于是从**它自己文档里写的那条采集命令**的输出里
一个对象都没读到，**且不报错**：`live_configmaps: 0`，静默。
后果不是崩，是内容摘要功能**在窗口里直接死掉**，操作员被塞 48 张"是的，同样的字节"要签。

两处修：加 `kind: List` 解包；**解析出 0 个对象且无其它错误时报
`LIVE_CONFIGMAPS_CAPTURE_IS_EMPTY`** —— 解析不出东西是采集坏了，不是集群里没有 ConfigMap。
沉默在这里的唯一代价是"多签几张审批"，所以它能在 review 里活很久而闸门一直空转。

---

## 8. 测试套件为什么全绿 —— 以及已经做了什么

**"276 条测试通过"证明的是夹具的形状，不是产物的形状。** 夹具的三个性质
（全 ASCII、全 LF、从不端到端渲染）正好是 §6/§7 七个缺陷的盲区。已做的三件事：

1. **删掉测试里那份编码器的复制品**。`tests/test_litellm_gray_chart.py` 原来自己重写了
   一遍 `json.dumps(..., ensure_ascii=True, ...)` —— 一个"存在意义就是等于 `toRawJson`"的
   契约有了第二份实现，那第二份就是漂移住的地方。现在直接 import
   `prepare-values.py` 的 `raw_json` / `raw_json_sha256`。
2. **新增 `tests/test_litellm_gray_byte_fidelity.py`**，故意用生产真有的性质：
   非 ASCII 内容、CRLF 内容、chart 默认键冲突、`kind: List` 采集、以及
   **完整的 `prepare-values → helm template → 比字节` 往返**。

   ⚠️ 这 5 条**每一条都立过阳性对照**：把对应的修法逐个还原回去，对应的测试当场变红
   （`toYaml` → 块标量、`read_bytes` → `read_text`、去掉 `shadow_chart_default_keys`、
   去掉 `kind: List` 解包）。**没有阳性对照的绿和合成绿一样不可信。**
   —— 第一次跑第 4 条对照时替换字符串缩进写错、静默没匹配上，于是"红"没出现而我差点把它
   读成"这条测试是好的"。重写缩进后才真的红。

3. **更正 `SITE_PACKAGES`**：现网实测是 `/app/.venv/lib/python3.13/site-packages`，
   原值 `/usr/lib/python3.13/site-packages` 是**猜的**。猜出来的夹具形状，
   对产物形状零证明力。

修完后 31 条因契约变更而红的老测试全部回绿（`scheduler_safety()` 新增必填
`secret_metadata_sha`、schema 新增必填 `schedulerSafety.secretMetadataSha256`、
摘要编码变更、`configmaps.yaml` 输出变更、`--output` 变为条件可选）。

### 待办

- `scripts/collect-scheduler-evidence.py` 已补进升级方案工具表与操作手册
  （手册里 `prepare-values.py` 的调用原来用 `...` 略过了 `--scheduler-evidence`）。

---

## 量具与清理

| 项 | 值 |
|---|---|
| 快照 | `kubectl -n litellm-product get deploy litellm-proxy -o yaml`，本机 `/tmp` 0600，用完即删 |
| 只读性 | 全程无 apply/patch/set image；198 上只跑了 `kubectl get` |
| 脱敏 | 只输出变量**名**与值的形状（`absolute path` / `integer` / `opaque len=N`），不输出值 |
| 现网 helm release | `litellm-product-proxy` rev 8，chart `litellm-product-proxy-0.1.0`，appVersion `v1.90.2-capacity`（**与本仓库 chart 同名同版本号但不是同一份**，本仓库 appVersion 为 `1.95.0`）|

⚠️ 最后一行单独记：**chart 名字和版本号对得上，不代表是同一个 chart**。
判断必须看 appVersion 与实际渲染形状，别拿 `helm list` 的 CHART 列当身份。
