# kiro-rs-slots 版本台账（从 SKILL.md 挪出来，SKILL 只留现役 + 回滚一行）

每版 = 上一版 + 这里写的改动。补丁 `k8s/kiro-rs/patches/0011-*.patch`（及 0012~0014）。回滚一般 `set image … <上一版>`，
例外见各行。状态备份 `198:~/kiro-deploy/backup/slots-pre-<tag>-<ts>/`（ship 自动做）。

| 版本 | 上线（北京） | 改动 | 回滚注意 |
|---|---|---|---|
| slots63 | 11-02 11:10 | 合并 slots62（30s 巡检把停用/退役/FREE 号从槽上换掉、测试测出 402 当场换号、report_* 停号后立即巡检）+ 排版重构：顶层标签 总览/槽位配置/账户（hash+localStorage 记忆，旧「设置」按钮删除）；总览问题条（有异常/空槽/暂不接新人/排队/额度低计数 + 只看异常 + 问题优先排序）；**固定槽号 `SlotView.no`**（= 配置全表 1-based 行号，停用不变；卡片/配置表/账户表/挪人下拉/toast 统一）；`SlotConfig.source`（iguge/self-aliyun/self-243）+ 配置表「来源」列；账户批量条常驻（未勾选置灰「先勾选账户」）；key→槽搜索；所有弹窗 Header/Body/Footer、footer `-bottom-6` 不盖内容；Badge 浅底深字 | 上线后 40s 内巡检已自动换下 1 个 TooManyRefreshFailures 的死号（槽 22 #189→#194） |
| slots62 | 未上线（并入 slots63） | | 槽上挂「停用 / 退役 / FREE」号 ⇒ 每 30s 巡检当场换冷备号，冷备不够摘成空槽（`sweep_dead_slots`）；测试测出 402 额度用尽 ⇒ 当场退役换号（结果带 `replacedWith`）；`report_failure / report_refresh_* / report_quota_exhausted` 停号后立即巡检。🔴 仓库 0011 补丁 e2d426c5 起就含这些，**现网仍是 slots61** | 上线前先修 ui61 回归的 2 处（footer 盖住最后 8~24px、批量验活按钮初始在视口外） |
| slots61 | 11-02 01:15 | 弹窗统一封顶视口高度、DialogBody + sticky DialogFooter；下拉菜单按可用高度滚；抽屉 max-h-dvh；槽位配置保存栏 sticky | |
| slots60 | 11-01 19:20 | 设置 → 槽位配置表加「槽号」列（= 卡片编号，按槽名对运行时 index+1；停用显示「—」）+ 该槽现在的号 #id · 档位；可按槽号排序 | |
| slots59 | 11-01 12:24 | 账户「批量启用/停用」`POST /credentials/disabled-batch`（在槽停用即换冷备号，冷备不够摘成空槽；启用清失败计数、去退役、补空槽；全部跳过也发请求以自愈）；单个停用同样摘空槽；交接中拒写；状态筛选「启用且不在用」；`slotEligible` 字段；「代理余额」卡片 `GET /egress/quota`（Secret `kiro-egress-subs`）| 🔴 pod template 加了 volume `egress-subs`（json patch，不是 ship）。回 slots58 及以前 `set image` 即可，volume 留着无害（optional）。整份回滚 `kubectl replace -f 198:~/kiro-rs-slots-deploy.pre-slots59-20261101T122329.json` |
| slots58 | 未上线 | = slots59 去掉 review 修复（重定向 / 自愈发请求） | — |
| slots57 | 未上线 | 批量启停第一版 | — |
| slots56 | 未上线 | 批量启停原型 | — |
| slots55 | 11-01 10:27 | 槽只改名、密码栏打码 ⇒ 按「同出口地址 + 用户名」沿用原密码（以前 400） | |
| slots54 | 11-01 10:17 | 去掉「生效最多 20」（总条数上限 200）；封口平均只算有号的槽；全部测试 8 个一批、前端超时按槽数放大；空槽橙色徽标；槽位配置表排序 + 按行勾选 + 批量启用/停用；保存确认列名单与冷备余量 | |
| slots53 | 未上线 | slots54 去 review 修复 | — |
| slots52 | 10-31 17:36 | `SlotConfig.bench` 测速（卡片 + 配置表一列），改出口清旧测速 | 回 slots50 可读（没有 deny_unknown_fields），保存时丢 bench |
| slots51 | 未上线 | bench 第一版 | — |
| slots50 | 10-28 17:12 | 槽位 key 列表弹窗：搜索 / 状态 / 用量筛选 + 排序 | |
| slots49 | 10-28 16:26 | 整请求预算 `slotRequestBudgetSecs`（默认 60：排队 + 429 退避 + 重发）⇒ 529 交兜底；429 重试用尽也回 529（以前 502）。0013 按新 0011 重做（挪一处上下文） | |
| slots48 | 10-28 | 独立 review 6 条（fill_slot_expect、待命不写盘、mark_took_over 前移等） | |
| slots47 | 10-27 22:48 | 0013 改版：安全审查提示去掉「没有作答（不是网络或服务故障）」，按备用模型名出文案；现网 `slotRefusalFallbackModel=claude-opus-5` | |
| slots46 | 10-27 17:20 | 卡片「额度用尽」改名「429 重试仍失败」+ 悬停说明 | 状态备份目录名是撞号时取的 `…slots43-20261027T171551` |
| slots45 | 10-27 14:30 | 0014 tool_use_id 规整（非法字符 / >64 ⇒ Kiro 400 ⇒ 兜底 grok） | |
| slots43 | 10-27 10:30 | 0013 opus-5.5 安全审查自动换备用模型 | 🔴 同号被另一会话覆盖过一次，已用 OCI import 还原 manifest f11a4f25 |
| slots42 | 10-27 01:16 | 告警只看 24h 可清除、接新人开关、专属 key 槽 | |
| slots41 | 10-26 19:25 | 重新分配 / 槽位权重 / 手动挪 key | 🔴 回 slots31 不能 set image（没有 /readyz）：`kubectl replace -f 198:~/kiro-rs-slots-deploy.pre-slots41.json` |
| slots40 | 10-26 | 无缝换版（租约交接 + /readyz + 主动请求交接） | |
| slots31 | 10-26 前 | Recreate + TCP 探针；空回复明细 | |
| slots29 | 10-25 | 积分趋势图、每页 10/100/1000、批量查额度、用量单位 | |
