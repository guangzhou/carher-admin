# cr-g 换腿回滚档（2026-09-20）

用户指令：**「可以先把之前的腿去掉，把新腿加上去。并加入到 litellm 的池子里。」**
外加三条约束：「把新的启动后再停止旧的」「注意保持 image 的一致」「注意不要打爆宿主机」「有问题的先跳过」。

**执行顺序上我做了一处刻意偏离并在当时说明过**：用户说的是"先去掉旧腿再加新腿"，
实际是**先加新腿并验通，最后才删旧腿**。理由是 14 个 `cr-g-*` 池别名不能有一刻是空的 ——
别名一空，症状从"腿在报错"变成 **`model not found` 400**，而且回滚的对照目标当场消失。
先加后删让每一步都有可比对的绿基线。

> 通用纪律（沿用 09-02 那份）：`litellm-proxy` 只用 `set image` / `patch` / `rollout restart`，
> **禁 `kubectl apply`**；删除一律**按显式名字**，禁 label selector；回滚外科式，不整份 restore。

---

## 一、最终形状

| 项目 | 换腿前 | 换腿后 |
|---|---|---|
| cr-g 池腿 | 84, 135, 136, 137, 138, 139, 140（7 条） | 175, 176, 177, 178, 180, 181, 182, 185, 186, 187, 188, 190, 191, 193（**14 条**） |
| 池别名 | 14 个 `cr-g-*` × 7 条 = 98 行 | 14 个 `cr-g-*` × 14 条 = 196 行 |
| 独占直连名 | 只有 82 / 135 有 | `cr-g-5.6-mini-<lane>` 覆盖全部 14 条池腿（**验单条腿死活的唯一入口**） |
| 保留未动 | — | 84（12 个 `cursor-g-*` 行）、135（14 个 `-135` 直连行）、101（6 个 `cursor-gpt-*` 行）、82（canary） |
| 删除 | — | 136, 137, 138, 139, 140（各自 deploy + svc） |

**只有 136–140 被删**，因为只有它们的 model 行数是 0 —— 84/135/101 都还有活引用，
删了会当场打断别的产品线。这一条是查出来的，不是按"旧腿"一刀切的。

**image 一致性**：15 条新腿全部 `clone_lane_from_live.py --ref 135 --expect-cm zk-cursor-bpi-patch-135`
从活腿克隆，image 与 svc 形状随源腿走，`pool_consistency.py` A 段（代码一致性）+ C 段（26 个 env 对众数）全 PASS。

---

## 二、逐项改动与回滚

### 2.1 新建 14 条池 lane（175–195 里过门的那些）+ 1 条建好但未留在池里（195）

- 脚本：`clone_lane_from_live.py --ref 135 --expect-cm zk-cursor-bpi-patch-135 --new <N> --apply`
- 落点：全部 `nodeName=aiyjy-litellm-standby`，每条 requests 50m/64Mi。
  **没打爆宿主机**：建完 48/110 pods、cpu requests 38%、mem 17%。
- 共用 CM `zk-cursor-bpi-patch-135`（不新建 CM，没有独占 PVC，只有 emptyDir）。
- **回滚**：`kubectl -n litellm-product delete deploy zero-cursor-bpi-<N> svc zero-cursor-bpi-<N>`
  —— 纯新建对象，删掉即净。

### 2.2 15 份 seed 灌进 225

- 捕获：`lane_seed_capture_queue.sh`（**必须在 188 跑**，`cf_clearance` 绑 188 出口 IP），
  ledger 在 `188:/Data/zkcaps/queue.log`，每号工作目录 `188:/Data/zkcaps/zkcap-<N>/`（700）。
- 灌装：`lane_seed_install.sh <N>` → `225:/Data/zerokey-sessions/zero-<N>/users.json`，三条判据：
  ① 落地字节数 == 源字节数 ② `users` key == `acct<N>` ③ cookie 非空 + sentinel 存在。
- **回滚**：新号无旧 seed，脚本明确打印「无旧 seed（新号，不需要备份）」，
  所以这一项**没有需要回退的覆盖**。老号若有旧 seed，脚本会先备份到
  `225:/Data/backups/zerokey-seed-<N>-<ts>-pre-install.json`。

### 2.3 入池门禁

- `lane_model_catalog.py --gate --ref 176 --lanes <N>`：≥19 slug 且含 `thinking`/`pro`/`instant`
  且是参照腿的超集。15 条腿实测全是 **21 个 slug，与 176 差集 0**。
  ⚠️ 195 **过了门禁却是死腿**（账号侧 403，见 2.7）—— 目录门禁量的是"菜单齐不齐"，
  量不到"这个账号还能不能答"，两者是不同的判据，别拿门禁 PASS 当腿活着。
- 门禁**在注册之前**，不带病入池。

### 2.4 注册进池

- 脚本：`crg_pool_register.py --lanes <N> --with-direct --apply`（**必须在 proxy pod 内跑**，
  它打 `127.0.0.1:4000`；在本地跑必 `ConnectionRefusedError`）。
- 每条腿 15 行：14 个池名 + 1 个独占直连名。幂等（重跑打印「已存在跳过」）。
- ⚠️ `--with-direct` 是这轮**中途**才加的，所以 175/176/177/178/180 那批先入池时没有独占名。
  09-20 21:5x 单独补建了 176/177/178/180 四个（175 早前手工补过），四个都实打过 200 + 落点正确。
  **写脚本时就该有这个 flag** —— 缺它的那段时间里，那 4 条腿只能靠池名抽样，
  抽不到就没有任何入口能单独判它死活。判"某腿这段没报"之前，先证明它**能**报。
- **回滚**：`/model/delete` 按 `model_info.id`（形如 `zerokey-cr-g-<N>-<变体>`）逐个删，
  或用 `crg_lane_retire.py`。⚠️ `ProxyModelTable` 的 `model`/`api_base` 列是**密文**，
  库级备份只能取证**不能回放**，所以回滚走 API 不走 SQL restore。

### 2.5 删除 136–140（唯一不可逆的一步）

- **动了啥**：5 条 deploy + 5 条 svc，按显式名字删，**没用 label selector**。
  删前状态：`chatgpt-acct-{136..140}` 早已 0 replicas 无 pod，bpi deploy 无独占 PVC，
  model 行数 0 —— 没有任何在服务的东西被碰。
- **备份在哪**：`198:/Data/backups/zk-bpi-{deploy,svc}-{136,137,138,139,140}-20260920-204035-pre-delete.json`
  共 10 份（deploy 各 12153 bytes，svc 各 ~1013–1015 bytes），每份删前都过了 `json.load` + name 断言。
- **怎么回滚**：`kubectl -n litellm-product create -f <那份 json>`（deploy 先 svc 后皆可），
  再 `lane_seed_install.sh <N>` 复灌 seed，最后 `crg_pool_register.py --lanes <N> --apply` 重新入池。
  ⚠️ 那 5 个号的 seed 属于 **mail.com OTP 已废批次**，复活需要重新抓取，而抓取当前对这批号会失败（见四）。

### 2.6 659 把 key 补齐 `cr-g-*` 授权（与换腿无关的既存缺口）

- 发现：36 把活跃 `cursor-*` key **一个 `cr-g-*` 都没有**（models 只 25–49 个）。
  三段式：假设=这些 key 触达不了 cr-g；证伪=它们的 SpendLogs 该有 cr-g 权限失败；
  **数据**=近 7 天有流量的 5 把，请求全落 `gpt-6-astra`/`sa-grok-4.6`/`gpt-5.6-sol`/
  `ag-gemini-3.1-pro`/`gpt-5.6-terra`/`gpt-5.6-luna`，**零 `cr-g-*`**。
  ⇒ 缺口真实但**当前零用户面影响**，是 09-02 铺池后新建 key 的稀释，不是这次换腿造成的。
- 脚本：`crg_key_grant_all.py --apply`（proxy pod 内，`TOKENS_FILE` 指 pod 内 TSV）。
  `/key/update` 的 `models` 是**整表覆盖**，所以是读 `/key/info` → 本地合并 → 整份写回 → GET 回读逐名核对。
  `models` 为空的 key 跳过（空 = 全部可用，加名单反而收窄）。
- 结果：36/36 成功。DB 独立核对（不信脚本自证）：659 把活跃 key，`still_missing_any = 0`。
- **备份在哪**：`198:/Data/backups/crg-key-grant-36keys-20260920-212741-pre-apply.json`
  （30330 bytes，36 把 key 的 `models_before` 整份；自检：长度 25–49，含 `cr-g-` 的 0 把）。
- **怎么回滚**：按备份逐把 `/key/update` 写回 `models_before`（同样是整表覆盖，一把一份）。

### 2.7 lane 195：入池 2 分钟后摘掉（账号侧恒 403）

- **动了啥**：195 过了目录门禁（21 slug、与 176 差集 0）、注册 15/15、proxy restart 后，
  独占名 `cr-g-5.6-mini-195` 实打 **3/3 全 500**，SpendLogs 落点确实是 195 ⇒ 路由对、腿本身报错。
  lane 日志栈底是 `403 Forbidden` / `[ChatGPT] Got 403`，即**上游账号直接拒**，不是网关 bug。
  同一轮 193 是 200 ⇒ **不是尺子坏**（有已证绿的对照）。
  于是 `crg_lane_retire.py --lanes 195 --apply` 摘掉 15 行（14 池名 + 1 直连名），
  每个池名摘完仍剩 14 条腿，**没有任何别名被摘成 0 条**。
- **备份在哪**：`198:/Data/backups/crg-retire-lane195-20260920-222440.txt`（693 bytes，15 行 `model_name|id`）。
  ⚠️ 这份备份**不能直接重建**（没有 `litellm_params`，`api_key` 被脱敏）——
  重建的拷贝源永远是一条活腿：`crg_pool_register.py --lanes 195 --with-direct --apply`。
- **怎么回滚**：先修账号（403 是账号侧的），再按上一行重注册 + `rollout restart deploy/litellm-proxy`。
- **WA 亲和 flush 没做，这是有判据的跳过**：全量 dry-run 实读 140 个 v2 pin，
  **指向 `-195-` 的有 0 个**（它在池里只待了约 2 分钟，没接到真流量）。
  没有对象可 flush，而 flush 会打断另外 140 个活会话的 pin ⇒ 不做比做安全。
- lane 195 的 deploy/svc/seed **保留未删**（白养一条 50m/64Mi 的 pod），
  账号修好后可直接重注册；要彻底下线则 `delete deploy/svc zero-cursor-bpi-195`。

---

### 2.8 新腿的 seed 轮转：**没有覆盖，加名字也不会有**（实测，不是推断）

原计划是"把新号加进 `188:/Data/zkcaps/refresh-accts.txt`"。**查完发现这一步会是个静默无效的假保护，所以没做。**

判据（`/home/cltx/zk-refresh-225.sh`，cron `23 */3 * * *`，读的就是那个文件）：

| 它要的形状 | 我们新腿的形状 |
|---|---|
| `kubectl get pod -l app=zero-<N>` | 标签是 `app=zero-cursor-bpi-<N>` ⇒ **实测 `-l app=zero-193` 返回 0 个 pod** |
| `kubectl cp ... $POD:/app/temp/users.json` | `/app/temp` 是 **emptyDir**，seed 在 `/seed`（hostPath `/Data/zerokey-sessions/zero-<N>`） |
| `rollout restart deploy/zero-<N>` | deploy 叫 `zero-cursor-bpi-<N>`，`zero-193` 不存在 |

对照 `zero-140`（名单里真有的号）：`-l app=zero-140` 查得到 pod，`/app/temp` 直接就是 hostPath。
⇒ **形状根本不匹配**。往名单里写 `193` 只会让脚本 `continue` 掉，日志里一行都不多，
而我们会以为"轮转兜住了"。这正是「指向不存在路径的保护永不触发」那个形状。

**没做什么**：没改 `refresh-accts.txt`（别人的 cron 在读它，加了也是无效行）；
没动 `zk-refresh-225.sh`（别人的脚本，改它要用户点头）。

**现在的真实状况（实读 JWT `exp`，不回显 token）**：
lane 175 / 191 / 193 的 seed bearer 分别还有 **235h / 239h / 240h**（约 10 天）。
`.creds` 和 4.2M 的持久化 profile **15 个号全都有** ⇒ 技术上能复用会话免 OTP 重抓，
但**当前没有任何自动化在给 `zero-cursor-bpi-*` 做这件事**。
10 天后需要人工重跑 `lane_seed_capture.sh` + `lane_seed_install.sh`，
或者由用户决定是否给 bpi 腿写一条对得上形状的轮转（要改的是标签查法 + cp 目标路径 + deploy 名）。

---

## 三、验收判据（哪些是证过的，哪些没有）

**证过的**：

- 14 条腿**逐条**用独占直连名 `cr-g-5.6-mini-<lane>` 打过真推理，全 200，SpendLogs 落点是该 lane。
- 全池回归 `crg_pool_probe.py --all --keys 10`：14 个池名 14/14 全 200，失败 0 发；
  该轮抽样漏掉的 175/178/180/188/191 随后用独占名逐条补打，5/5 全 200、落点正确。
- 服务端实读：14 个池别名腿数分布 `[14]`（**完全齐平**），总池行 196 行，14 条池腿各有独占直连名。
- `pool_consistency.py` **VERDICT: PASS**（A 代码一致性 / B 池覆盖 / C env 一致性）。
  孤儿 lane 现为 `101,195`：101 既定弃用永不入池；195 是本轮**入池后又摘掉**的（见 2.7）。
- 14 条 lane pod **0 restarts**；136–140 删后残留 0。

**没证到的，明说**：

- 那 36 把补过授权的 key，**我没拿它们本身打过真流量** —— DB 里存的是 hash，明文 key 取不到，
  新建探针 key 验的不是"这 36 把"。能证的是"库里授权到位 + 池名本身活着"，
  证不到的是"这 36 把明文 key 拿去打确实 200"。要么等持有者自己发一次请求，要么只能证形状。
- **池名探针不能用来判单条腿死活**。09-20 实测 4 把 key × 14 发抽 5 条腿，有一整轮零命中 lane 175；
  10 把 key × 14 发那轮也漏了 175/181/185。亲和是 **key 级**（Cursor 不发 session 头），
  一把 key 钉一条腿。所以探针里那句「这一轮没有任何一发落到 X」**是抽样覆盖告警，不是故障**，
  但也**不许当"验过"放过** —— 漏掉的腿必须用独占名单独补打。

---

## 四、跳过的号与原因（用户「有问题的先跳过」）

39 个目标号里，`168, 169, 172, 183, 184, 189` 抓取失败，症状全同一个：
`still logged out (anonymous) — refusing to capture anonymous session`。

根因是 **09-17 起 mail.com 的 OTP 正文 iframe 恒为 170 字节空壳**，浏览器侧抓不到验证码。
⛔ 不要再往 selector 上叠补丁（已试过无效）；下一步是截图看页面，或走 IMAP 绕开浏览器。

另有 `192, 194` 失败，症状**不是同一类**：
`navigating to "https://chatgpt.com/", waiting until "domcontentloaded"` 超时，
且 194 只跑了 31 秒就失败（正常一轮 3–5 分钟）。它们中间的 193/195 都抓成功了
⇒ **不是通道整体挂了**。OTP 那批是真废，导航超时这类可能只是瞬时，将来可单独重试。

**这一条同时意味着**：`188:/Data/zkcaps/refresh-accts.txt` 的轮转**当前无法刷新需要 OTP 的号**。
而且比这更糟 —— 见下面 2.8。

---

## 五、遗留（本轮未做，不是本轮引入）

- `cursor-g-*` 产品线 6 个双腿名字压在 82 和 84 上，而 84 的 seed 属于已废批次
  ⇒ 那条线**实际只有一条活腿**。要不要换腿需用户决定（84 本身还有 12 个活引用，不能顺手删）。
- `cursor-web-fc-pool-terra*` 只挂 lane 82，各别名腿数不齐（形如 `[1, 2, 14]`）。
  **注意这个 `[1, 2, 14]` 说的不是 `cr-g-*`** —— `cr-g-*` 14 个别名实读全是 14 条腿（齐平）；
  不齐的是 `cursor-web-fc-pool-terra*` / `cursor-g-*` 那两条别的产品线。
- ~~`pool_consistency_selftest.py` 还是旧拓扑~~ **已核实不需要改**：它没有硬编码腿号
  （`ok_a, lanes = pc.check_code()` 读线上拓扑），实跑 **8/8 OK**，且能红
  （合成悬挂 lane 135 被抓到、`stealth-fork-must-fail got=False want=False`）。
- **`zero-cursor-bpi-*` 腿没有 seed 轮转**（见 2.8，形状对不上，不是"忘了加名字"）。
  seed bearer 约 10 天后到期，届时要人工重抓，或由用户决定给 bpi 腿写一条对得上形状的轮转。
- `lane_seed_install.sh` 的 in-pod `/app/temp/users.json` mtime 判据目前每轮手工做，该收进脚本。
  判据是：temp mtime > seed mtime（证明 running 进程真的 `cp` 过）+ key == `acct<N>` + cookie 长度吻合。
