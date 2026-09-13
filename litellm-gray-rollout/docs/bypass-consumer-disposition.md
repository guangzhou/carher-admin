# 30402 旁路消费者清点与处置（方案 §5.6 第 1 条）

这份文件回答一个问题：**nginx 打到 100% gray 之后，prod 就真的没流量了吗？**

答案是**不一定**，而且这正是 §5.6 存在的理由。nginx 只管
`cc.auto-link.com.cn` 这一个入口；任何直连 NodePort `10.68.13.198:30402` 的
cron / 常驻服务 / 容器对 `convergence_mode` 完全免疫——它们不经过 nginx，
所以 gray 权重对它们是 0 效果。**把"入口无流量"读成"prod 无流量"，
就是拿一把量不到目标的尺子去收货。**

> 权威输入是**执行时现扫的清单**，不是这张表。
> 扫描脚本：`scripts/collect-bypass-inventory.sh`（全程只读，不写远端任何文件）。
> 本文件记录的是 **2026-09-13 的一次扫描结果 + 每个消费者的处置决定**；
> 执行当天必须重扫一次，差异逐条判读后再进 §5.6 gate。

---

## 0. 扫描范围与方法

| 维度 | 扫了什么 | 为什么 |
|---|---|---|
| cron | 198 全部用户 crontab + `/etc/crontab` + `/etc/cron.d/*`；188 同 | 周期性消费者的唯一藏身处 |
| 脚本正文 | cron 命令里引用到的绝对路径脚本，**再跟进一层**它调用的 `.py`/`.sh` | wrapper 脚本自己不带 URL，URL 在被它调用的 python 里 |
| env 文件 | 188 上 `source *.env` 引入的 base 覆盖 | **决定它打 dev 30400 还是 prod 30402**，不查就会把 dev 消费者误列进来 |
| systemd | 两台的 `/etc/systemd/system/`、`/usr/lib/systemd/system/` | 常驻服务的另一种注册方式 |
| 进程表 | `ps -eo args` 命中 `3040[0-9]` | 抓"没在 cron 里、但确实在跑"的漏网之鱼 |
| docker | 188 全部运行中容器的 `Config.Env` | 容器化消费者的 base 从 env 注入，脚本里看不到 |

**主机只有两台**：198 和 188。⚠️ 方案里写的"198 / JSZX-AI-03 / 188"是**三个名字两台机器**
——`JSZX-AI-03` 就是 `10.68.13.188`（见 memory `reference_188_direct_ssh_and_nas_layout`）。
按三台去找第三台会永远找不到，不是漏扫。

### 0.1 扫描器自己先过阳性对照

"198 没有旁路消费者"这句话，只有在**扫描器有能力扫出消费者**的前提下才有意义。
第一版扫描器对 198 输出了干净的空结果——而 198 明明有一个每小时 `:23` 在跑的
A 类写消费者。**那不是 198 干净，是尺子瞎了。**

| 缺陷 | 后果 | 修法 |
|---|---|---|
| 用 `sed 's#.*\(/[…]*\.py\).*#\1#'` 抓子脚本路径 | 前导 `.*` 贪婪，把 `/home/cltx/x.py` 截成 `/x.py`（不存在）⇒ `test -f` 失败 ⇒ **消费者被静默丢掉** | 改 `tr -c` 按字符集切词，token 完整 |
| cron 行里的 `/` token 不判类型/体积 | 目录（`run-parts /etc/cron.hourly`）刷噪声；2.3 GB 的 `engine.db` 被整个 `cat` 过 ssh ⇒ 扫描卡死 | 一次 `stat -c '%s\|%F'` 同时判类型和体积，>1 MB 打印 `SKIP-LARGE` 后跳过 |
| `@@PROC@@` 匹配裸 `3040[0-9]` | 捞进 5 个 playwright/chrome 进程（随机长整数里恰好含该数字串） | 锚 `:3040[0-9]`，端口前必有冒号 |
| `BODY198` / `BODY188` 两份副本 | 给 198 加了体积闸、188 那份忘了加 ⇒ 188 立刻卡死 | 合并成**一份** body，两台共用 |

判据是：修好之后，扫描器必须**独立扫出**我此前手工确认的那三个消费者。实测全部命中（见 §1.3）。

> 通用教训：一份报告"什么都没发现"时，先问的不是"是不是真没有"，
> 而是"这把尺子扫得出东西吗"。**合成绿和合成红同样不可信。**

---

## 1. 活消费者清单（2026-09-13 扫描）

| # | 消费者 | 主机 | 触发方式 | 端点 | 分类 |
|---|---|---|---|---|---|
| 1 | `acct-base-model-sweep.sh` → `litellm-acct-base-model-fix.py --apply` | 198 | cron 每小时 `:23`，`flock -n /tmp/acct-bm-sweep.lock` | `GET /model/info`<br>`POST /model/{id}/update` | **A 控制面写** |
| 2 | `zerokey-meta-collector.py` | 188 | cron 每 2 分钟 | `GET /pro/v1/model/info` | **B 控制面读** |
| 3 | `acct-admin-backend` 容器 | 188 | 常驻服务，**按用户点击触发** | `GET /v1/model/info`；pause/resume → `POST /model/delete` + `/model/new` | **A 控制面写**（on-demand） |
| 4 | `quota-rebalance.py` | 188 | cron —— **已暂停** | `/pro` | A（已冻结） |
| 5 | `zerokey-rebalance.py run-dev` | 188 | cron 每 2 分钟 | **30400（dev）** | **不在范围** |

分类口径（方案 §5.6）：**A** = 控制面写（`/model/*` `/key/*` `/team/*` `/budget/*`）；
**B** = 控制面读/对账；**C** = 推理/探活。

### 1.1 第 5 条是证伪腿，不是漏网之鱼

`zerokey-rebalance.py` 看起来该进清单——它每 2 分钟跑一次，脚本里确实有 LiteLLM 调用。
但它 `source` 的是 `/home/cltx/.zerokey-rebalance/dev.env`，里面写死
`LITELLM_BASE=http://10.68.13.198:30400`（**dev**，不是 prod 的 30402）。

**这一条的价值在于它证明扫描方法有判别力**：如果 env 文件那一腿没扫，它会被当成
prod 消费者列进来，然后在窗口内被无谓地冻结。反过来说——扫描能把它**排除掉**，
说明同样的方法有能力把真消费者**纳进来**。只列命中、不列排除的清单是不可信的。

### 1.2 第 1 条"有写能力"但"当前零写入"——两件事要分开说

`litellm-acct-base-model-fix.py` 带 `--apply` 跑，**具备**改 `/model/{id}/update` 的能力。
但近 6 小时的每一次执行日志都是 `待补=0`，即实际写入次数为 0。

⚠️ **不能因此把它降级成 B**。「最近没写」≠「不会写」——它是个收敛器，
上游一旦出现 base_model 缺失的部署，下一个 `:23` 就会写。窗口内必须按 A 处置。

### 1.3 原始证据（`captured_at=2026-09-13T13:14:56Z`）

扫描器输出全文 33 行，去掉 `SKIP-LARGE` / `cannot statx` 后的**全部命中**只有三条：

```
# [198] /home/cltx/litellm-acct-base-model-fix.py:35:EP = "http://127.0.0.1:30402"
# [188] /home/cltx/zerokey-meta/zerokey-meta-collector.py:44:
#         LITELLM_BASE = os.environ.get("LITELLM_BASE", "http://10.68.13.198:30402/pro")
# [188] CONTAINER acct-admin-backend: ACCT_ADMIN_LITELLM_BASE=http://10.68.13.198:30402/pro
```

对应消费者 1 / 2 / 3。两台机的 `@@SYSTEMD@@` 与 `@@PROC@@` 段**均为空**。

⚠️ 消费者 1 是**跟进一层**才抓到的：cron 里写的是 wrapper
`/home/cltx/acct-base-model-sweep.sh`，wrapper 自己不含 URL，URL 在它调用的
`litellm-acct-base-model-fix.py` 里。**只扫 cron 命令行、不跟进被调用文件的扫描
会漏掉它**——这也是 §0.1 那条贪婪 `sed` 缺陷影响最大的地方。

消费者 4（`quota-rebalance.py`）本次扫描**未出现在任何 crontab 中**，
与"已暂停"的记录一致，构成一次独立复核。

---

## 2. 反向证据（negative legs）

这些"没找到"同样是清单的一部分。只报命中、不报排除，读者无法判断是"真没有"还是"没扫到"。

| 检查 | 198 | 188 |
|---|---|---|
| systemd unit 引用 `:30402` | 无 | 无 |
| 长驻进程 cmdline 含 `30402` | 无 | 无 |
| root crontab | 无 30402 条目 | **空** |
| 其它非系统 cron | 无 | —— |

---

## 3. 仓库里那 ~55 个脚本为什么不进清单

`grep -rlE ':30402' --include='*.py' --include='*.sh'` 在仓库里命中 **55 个文件**。
它们**不进本清单**，理由是 CLAUDE.md 的硬红线：**代码存在 ≠ 该路径被执行。**

这些是手工运维工具——只有人敲命令时才跑，没有任何调度器会自动触发它们。
把它们列成"活消费者"会让清单膨胀十倍且全是噪声，真正该盯的三个反而被淹掉。

它们的处置是一句纪律，不是一张表：

> **灰度窗口内（§5.3 起至 §5.7 收敛完成）禁止手工运行任何直连 30402 的仓库脚本。**
> 需要临时操作走 nginx 入口，或等窗口结束。

---

## 4. 处置方案

### 消费者 1 —— `acct-base-model-sweep.sh`（198，A 类写）

| 项 | 内容 |
|---|---|
| owner | 198 运维（本仓库） |
| 原入口 | `http://127.0.0.1:30402`，cron 每小时 `:23` |
| 处置方式 | **窗口内注释掉该 cron 行**（不删文件、不改脚本本身） |
| 动了什么 | 只动 `198:/var/spool/cron/crontabs/root` 的一行；`crontab -l > ~/crontab.bak.<ts>` 先备份 |
| 怎么回滚 | `crontab ~/crontab.bak.<ts>` |
| 冻结影响 | 窗口期内新出现的 base_model 缺失不会被自动补齐 |
| 补偿/恢复 | 收敛完成后恢复 cron；**并手工跑一次 `--apply` 补齐窗口内的欠账**，确认输出 `待补=0` 才算收尾 |

### 消费者 2 —— `zerokey-meta-collector.py`（188，B 类读）

| 项 | 内容 |
|---|---|
| owner | 188 / zerokey |
| 原入口 | `http://10.68.13.198:30402/pro`，cron 每 2 分钟 |
| 处置方式 | **不冻结，改为允许**。只读 `/v1/model/info`，不改任何状态 |
| 理由 | 冻结它反而制造 metadata 空洞；且它是灰度期一把免费的**活性量具**——它持续成功 = prod 控制面还活着 |
| 风险 | 若 §5.7 要求 prod **零请求**，这一条会破坏该判据 ⇒ 届时改为窗口最后阶段再停 |
| 怎么回滚 | 未改动，无需回滚 |

### 消费者 3 —— `acct-admin-backend` 容器（188，A 类写，on-demand）

| 项 | 内容 |
|---|---|
| owner | acct 管理面使用者（**人，不是调度器**） |
| 原入口 | `ACCT_ADMIN_LITELLM_BASE=http://10.68.13.198:30402/pro` |
| 处置方式 | **不停容器**（停了管理面直接不可用）；改为**窗口内禁止执行 pause/resume 操作**，口头/公告约束 |
| 为什么不改 env | 改常驻服务配置属于"改用户常驻配置"，按纪律只能由用户本人动手；且改完要重启容器 = 管理面中断 |
| 冻结影响 | 窗口内不能暂停/恢复 ChatGPT 账号 |
| 补偿/恢复 | 窗口结束后照常操作；若期间有账号必须紧急暂停，走 §5.6 例外流程并记录到 evidence |

### 消费者 4 —— `quota-rebalance.py`（188，已暂停）

已于本轮准备阶段暂停，**本次扫描确认它不在 crontab 中**。
恢复方式：`crontab /home/cltx/.chatgpt-quota/crontab.bak.20260913-172109`。

### 消费者 5 —— `zerokey-rebalance.py run-dev`（188，dev）

不在范围，不处置。窗口内保持原样。

---

## 5. 执行当天的动作清单

1. 重跑 `scripts/collect-bypass-inventory.sh --output <run-dir>/bypass-raw.txt`。
2. **与本文件 §1 逐条比对**。出现新消费者 ⇒ 先判分类再决定处置，**不许直接放行**。
3. 按 §4 执行冻结（消费者 1 注释 cron，消费者 3 发布操作禁令）。
4. 把结论行写成 `collect-runtime.py --bypass-inventory` 能吃的格式，喂进 evidence。
5. §5.7 收敛后按 §4 各条的"补偿/恢复"逐条恢复，**恢复也要留证**。

> ⚠️ 第 4 步的结论行由**人**判读后写，扫描脚本不自动生成。
> 让脚本把 grep 命中直接变成"活消费者"，就是把「代码存在」自动升级成「该路径被执行」。

---

## 6. 顺带发现：两台机上都有空转的 cron（不属于本次范围，但该修）

扫描器的 `cannot statx` 那几行不是噪声——**它们是"cron 指向的文件不存在"的物证**。

### 6.1 198：`litellm-aiohttp-pool-guard`

`/etc/cron.d/litellm-aiohttp-pool-guard` 每 5 分钟跑
`/opt/litellm-ops/aiohttp-pool-guard.sh`，但 `/opt/litellm-ops/` **自 6 月 15 日起就是空目录**。

```
sh: 1: /opt/litellm-ops/aiohttp-pool-guard.sh: not found     rc=127
```

syslog 证实它每 5 分钟触发一次并失败，**且没有任何告警**。
它与 30402 无关（不是旁路消费者），但它意味着**曾经有一个 pool guard 在保护 aiohttp
连接池，现在没有了**，而没有人知道。

### 6.2 188：12 个 zerokey codex 账号的 `refresh.sh` 全部缺失

```
stat: cannot statx '/home/cltx/zerokey-codex-accounts/{dvo,hgg,owp,timothy,acct32,
      herbert,acct34,olga,tania,iheyv,acct37,elise}/ops/refresh.sh': No such file
```

12 个账号目录的 `ops/refresh.sh` 都不在了，但 cron 条目还在按时触发。
同 6.1 一样：**静默失败，无告警**。这批 refresh 本该维持 codex 账号的凭据新鲜度。

另有一条 cron 写着 `/nas/chatgpt-quota-engine/engine.db.$(date` ——
`$(date ...)` 在 crontab 里**不会被展开**（cron 不跑命令替换，且 `%` 还会被当成换行），
这个备份目标名是字面量，等于备份从来没按预期落地过。

**三项均未处置**：删除或修复都是在改生产常驻配置，需要用户拍板。这里只记录。
⚠️ 6.1 / 6.2 都**只有"cron 在空转"这一个事实**，
「因此 X 出过问题」是另一个需要独立取证的命题，本文件不下这个结论。
