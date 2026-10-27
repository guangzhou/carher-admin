# v1.106.4 升级基线（2026-10-26）

上一代 `../v1.105.1/`。198 现场工作目录：`/root/l3/up1064-20261025T164519Z/`（root 0700，指针 `/root/l3/.up1064-latest`）。

## 镜像

| | tag | digest |
|---|---|---|
| vanilla | `127.0.0.1:5000/litellm-carher:vanilla-v1.106.4` | `sha256:f6589ec874ed5e37ed90452d0b86613ad07393a1148a8a401639a87533f11b0c`（= ghcr 官方 amd64） |
| 烘焙 | `vanilla-v1.106.4.capacity.sse-fix-bare.keyalias-auth-20261025-163220` | `sha256:7e0c329c78ee95625a7491a78475571e0b1b78c29a2120895184d6c84a7f7ccf` |

md5 join：两边各 21129 个文件、零增删，只 3 个补丁文件不同。

## 烘焙补丁（3 个，逐字节同 v1.105.1）

| 脚本 | 1.106.4 上 |
|---|---|
| `litellm-patch-capacity-ratelimit.py` | 锚点 1 命中 |
| `litellm-patch-sse-lifecycle.py` | 全部锚点命中 |
| `litellm-patch-key-alias-auth-compat.py` | 命中（上游 #43049 仍在） |
| ~~`litellm-patch-stream-alias-pricing.py`~~ | **下线**：#44341 已进 1.106（脚本 SKIP）。它只提供一条行为（流式经别名按 deployment 计价），上游 `client_facing_stream_model` 同名实现，Dockerfile 改为断言上游这段还在 |

## 追加补丁：DeepSeek 搜索（10-26 已上线 gray rev30 + prod rev23）

总览 / 杭州 gw 那份 / her 侧怎么切：skill `litellm-deepseek-search`。同一补丁 10-26 也打进了杭州 gw（1.101.0，锚点同样恰 1 次）。


镜像 `vanilla-v1.106.4.capacity.sse-fix-bare.keyalias-auth.dssearch-20261026-113746` = `sha256:5461b2d68a7f9df2aa072882b522792697081748e3ccd1e1aa288a670e6b23c5`。
md5 join 对 `7e0c329c`：+2 新文件、2 个文件变（`types/utils.py`、`utils.py`），零删除。价表 json 文件 md5 没变：原文件已带 `deepseek/search`（上游 10-25 构建的价表早就有这一项），补丁里那步是空操作。
上线方式：只换镜像 digest（values 里恰 1 处，helm template 和 live 比只差 image 一行），先 gray 再 prod；guarded-old 仍是 1.105.1 不动。
search tool 存在 DB（`LiteLLM_SearchToolsTable`，名 `deepseek-search`，key 取自 `deepseek-official/deepseek-v4-flash` 行，加密存），**没改 config.yaml**；四车道每 10s 同步。
回滚：两车道 values 备份在 `$D/dssearch-20261026/{gray,prod}-values.before.yaml`，`helm upgrade … --values <before> --reset-values`；
DB 行可留（旧镜像只在调用时报 `not a valid SearchProviders`，加载不报错，已实测）。
回归：并排 8 条（gray vs prod）sweep/budget 同形、kiro 同组同 code 0 单边（整轮超时 1800s = 老实例一批组 502，见 skill §4）、其余 PASS；转正后 prod vs guarded-old 4 条 PASS/同形。
hermestest-14 全链路：`scripts/deepseek-search/run-probe.sh docker hermestest-14 <BASE> agent`（隔离副本 + LiteLLM transport 插件，不动在服务配置），gray/公网 prod 入口 L2 命中、真回合答对，指旧镜像 prod 500（阴性对照）。

### 当时的说明（上线前写）

`litellm-patch-deepseek-search.py` = 上游 #47401（10-19 合 main，merge `9bafafc2df3c`）回移。1.106.4 / 1.107.0rc2 都没带，
`search_provider: deepseek` 原样报 `'deepseek' is not a valid SearchProviders`。

- 改动：新增 `llms/deepseek/search/` 两个文件（`deepseek-search/upstream/`，逐字节取自上游 merge commit）+ 枚举 1 行 + 注册表 2 行 + 本地价表 `deepseek/search` $0.00075/次。
  锚点在 1.106.4 与上游 PR 前的 diff 完全一致（只差这 3 行）。⇒ md5 join 验收会多出 2 个新增文件，属预期。
- sha256：`__init__.py` 3d9c35fb… · `transformation.py` d6c45616… · 测试 dea8717e…
- 本地验证（`.work/ds-search-review/`，Python 3.13 + PyPI 1.106.4）：上游 15 条单测打补丁后全过、未打补丁收集即报错；幂等第二次 SKIP；
  起 proxy + 假 DeepSeek 上游，`POST /v1/search/deepseek-search` 200，上游收到 `/v1/tools/search`、`count` 从 99 夹到 50、`need_content` 透传。
- 上线前的这四项后来的结论：key 不用注入 env（DB search tool 直接存 `deepseek-official` 那把）；search_tools 进 DB 不进 config；
  carher-* key 没有 object_permission ⇒ 不用授权；198 pod 出网 api.deepseek.com 直通（401 即连通）。
- 下线：上游发版带上后脚本会因 `llms/deepseek/search` 已存在返回 2，build 红，到时删掉这一步。

## 整文件覆盖件（overlays/，按 1.106.4 源重做）

| 文件 | 改动 | 重放 |
|---|---|---|
| `litellm_core_utils/streaming_handler.py` | +7/-0 | 偏移 62 行，干净 |
| `proxy/pass_through_endpoints/streaming_handler.py` | +18/-4 | 上游 `_update_completion_start_time` → `update_completion_start_time` 改名，我们那行跟着改；两个调用点都带 chunk |
| `.../anthropic_passthrough_logging_handler.py` | +14/-0 | 偏移 3 行，干净 |

## 审计

- callbacks（live prod 39 个 + gray 的 sec_redact）引用的上游符号：GONE=0、CHANGED=15。
  `_maybe_run_weighted_failover`（sgfc 包的）只改了一个内部函数名；`async_get_available_deployment` 改成委托内部函数，helper 仍调它；
  `_is_cooldown_required` 新增 402 冷却（与 dead_deployment_retry 只包 400 不冲突）。
- callback pytest 套件新旧镜像结果集合逐文件相同（625 passed，失败/错误集合同形，均为本机缺依赖）。
- 迁移 18 条（全加法）。clone 四腿（new / old / concurrent-new / concurrent-old）PASS；生产 21 s 级，196→214，零表重写、零列/索引丢失。
  🔴 第 6 条 CONCURRENTLY 索引被别的会话一条 13 分钟的只读 SpendLogs 查询卡住（virtualxid），cancel 掉后完成。
- 计费：LIT-145（超时 / 499 / 重试失败行开始计价）进 1.106，并排 spend-row-parity 两车道 9/9。
- Codex `/v1/models?client_version=`：1.106 回 Codex 原生 `{"models":[...]}`，1 MB 上限会截掉排在后面的组（gray 日志
  `Codex model catalog cut at 1048576 bytes`）。实测我们的一键配置用 `model_catalog_json` 本地目录，`codex debug models`
  在 gray / prod 输出逐字节相同；带 client_version 的请求只来自少数 Codex Desktop（6h 2 次）。

## 上线结果（10-26，run `litellm-198-v1064-20261026`，`/root/l3/up1064-20261025T164519Z/`）

| 车道 | helm rev | 镜像 |
|---|---|---|
| prod `litellm-proxy` | rev22（回退：`gray-post-commit-rollback.sh`，备份 `prod-target/backup-20261026T004950Z`） | 1.106.4 `7e0c329c` |
| gray | rev29（2 副本热备） | 1.106.4，+ sec_redact 影子 |
| guarded-old | rev13（备份 `go/backup-20261026T011159Z`） | 1.105.1 `d8a6bfbc` + 当前 prod config |

0→1→5→10→50→100 全由驾驶台自动升档，每档 5xx 0、worker_killed 0；收敛 R10 4 pod OK，三车道兜底契约 OK，转正后 15 分钟 5274 请求 5xx 0。

## 这轮踩的坑（下次照做）

- **快照取 live prod 的 CM，Deployment 形状取上一轮冻结的那份 + 新 callback mount**：live prod 的 Deployment 卷名是 helm 的 `snapshot-*`，
  prepare-values 认不出（报 `volume snapshot-anthropic-logging-patch ... has no --snapshot`）。悬空 mount（`websearch_you_com.py`）要先去掉。
- gray 要保留的带外件（sec_redact）照 **live gray 的列表位置** 插，不要默认放末尾（形状门禁会报 ORDER）。
- gray 冻结时 replicas 取 live 值（2），**放量前扩到与 prod 同数（4）**，只改 replicas、渲染 diff 恰 2 行；扩完要重 pin + 重 baseline、monitor `--expected-ready 4`。
- 驾驶台要带 `GRAY_PER_CONTAINER_CONCURRENCY=50` 启动，否则 ≥50 档 step.sh 直接报缺参。50/100 档的容量证据会被上一档 split 事务作废，
  驾驶台卡「容量还没测」时用 step.sh 同一条 `check-split-capacity.py` 命令现量一次即可。
- 名单事务（force-gray/remove）每把都轮转 generation ⇒ `key_pilot_entry` 每把前重签。
- clone 迁移 Job 必须串行（post-mig dump 完再 restore B/C）；dump job 的「非日志表 ≤1 GiB」闸门已过时（现在 1.1 GB），本轮改 4 GiB。
- `kubectl exec -i` 在 `bash -s` 读 stdin 的脚本里会吞掉后续脚本行（迁移脚本静默只跑了一半）⇒ `kubectl exec ... < /dev/null` 或先 scp 成文件跑。
- 驾驶台回归两条假红已修：key_behavior 参考 key alias 10-25 改拼音（`claude-code-shangwenze-7729`）；
  budget 冒烟先把 key 打满再 `/v1/models`（超额 key 连列模型都 429）⇒ 改成先列模型再打满。S1/S2「超额软拦截 200」三车道（含 1.103.2）同为 429 = 既有行为，不是版本差异，待查。

## 10-27 追加：uvloop 0.22.1 → 0.23.0（修 worker 偶发崩溃）

**现象**：pod 0 重启，但 worker 偶尔整个死掉（`Child process [N] died`），它手上所有在途请求一起断（nginx `upstream prematurely closed` 一分钟几十条；zk-delta `socket hang up`）。
**根因**（有现场证据 + 受控复现）：uvloop < 0.23.0 的 fd 双关 bug（MagicStack/uvloop#738，修复 #740 进 0.23.0；LiteLLM 上游跟踪 BerriAI/litellm#44623，uv.lock 仍钉 0.22.1）。
`create_connection(sock=)` 被取消（aiohttp 3.14 经 aiohappyeyeballs 走的就是这条）⇒ libuv 关了 fd、Python socket 不知道 ⇒ GC 再关一次 ⇒ 关掉别人刚复用到这个号的 fd。
被关的是 glibc getaddrinfo 线程里的 netlink socket 时 ⇒ `Unexpected error 9 on netlink descriptor N` ⇒ `abort()`。
- 现场：10-27 06:32:33.78Z l258r stderr `Unexpected error 9 on netlink descriptor 60.`，0.38s 后 `Child process [79] died`；225 apport 同秒 `host pid 1424990 crashed in a container`（= 那个 worker）。225/242 apport 10-08 以来共记 4 次容器内崩溃（10-11 / 10-12 / 10-26 两次 / 10-27），其余日志已轮转无法逐个对上。
- 复现（`.work/zkd-hangup/repro/abort_repro.py`，8 线程 getaddrinfo + 主循环反复取消 create_connection）：现役镜像 `5461b2d6` 三次三崩（rc=139，同款 netlink 报错）；只换 uvloop 0.23.0 的新镜像三次 0 泄漏；stdlib asyncio 0 泄漏。

镜像 `vanilla-v1.106.4.capacity.sse-fix-bare.keyalias-auth.dssearch.uvloop023-20261027-164202` = `sha256:92410b94ce1f43245d59152d77b26eca02490523417dc9d30242d2c9e7359fbe`（配方 `uvloop-0.23/`：wheel 按 PyPI sha256 校验后直接解压替换，venv 里没有 pip；构建期行为断言 cancel 后 `fileno()==-1`）。
md5 join 对 `5461b2d6`：只有 `site-packages/uvloop/` 与 dist-info 变，其它 0 变化。
上线（§5.5 换 digest）：values 恰 1 处替换，helm template 对 live manifest 只差 image 一行；gray 先（rev31），回归并排后 prod。

