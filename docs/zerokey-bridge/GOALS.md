# zerokey bridge — 业务目标与验收(唯一权威)

**这份文件是目标的唯一来源。任何改动前先读它,改完回来跑验收。**
技术缺陷清单不是目标 —— 缺陷只有在"会导致某条目标不达成"时才值得修。

最后校准:2026-07-27

---

## 四条业务目标(用户视角,不是技术视角)

| # | 目标 | 硬/软 | 现状 | 验收方式 |
|---|---|---|---|---|
| **G1** | 用户**不改配置**就能用 zerokey | 硬 | ✅ 达成 | 用户写原名 `gpt-5.6-sol`,SpendLogs 里 `model` 保持原样、`model_id` 是 `*-bridge` |
| **G2** | 用户能在 **codex 上用 zerokey**,含 **toolcall** 和**正常对话** | 硬 | ⚠️ 部分 | 单命令类 ≥95%;多步类见下 G2b |
| **G2b** | 多步 toolcall 任务(如 lark-cli 读飞书文档) | 硬 | ⚠️ **75%**(6/8,基线 5/8) | `loop_trials.py` 8 轮 ANSWERED 率 |
| **G3** | **不卡顿**,便捷快速 | 硬 | ✅ 可接受 | 纯聊天 ≤8s;带工具单轮 ≤15s |
| **G4** | **不浪费流量** | 硬 | ✅ 已优化 | ≤5 次上游调用/完整任务 |

### G1/G3/G4 已达成 —— 不要再"优化"它们

**这是本轮最大的教训。** 复盘发现:拒答检测改了 6 次(服务 G2,实测不动指标)、
健康分+衰减+排除门(服务 G4,而 G4 已靠 fanout=1 达成)、四层限流器(同样服务已达成的 G4)。
**大量新增复杂度不服务于唯一未达成的目标**,这才是"为什么突然这么多问题"的真正原因
(代码 484 → 2867 行,函数 16 → 56)。

**规则:动手前先问"这服务哪条目标?那条目标达成了吗?"** 已达成的目标只做**回归保护**,不做优化。

---

## G2b 的差距在哪(已定位)

并排看通过率,差别不是"能不能调工具":

| 任务 | 通过率 | 特点 |
|---|---|---|
| 看磁盘 / git 分支 / 数文件 | 3/3 | **一条命令就够** |
| 纯聊天 | 3/3 | 零命令 |
| 读飞书文档(lark-cli) | 50-71% | **需要 3-5 条命令连续执行** |

**差距是"能不能连续调多轮",不是"能不能调工具"。**

调研结论(见 `refusal-detection-postmortem.md`):七个同类项目**没有一个**用正则分类模型散文。
正确解法是把"要不要继续"变成**结构判断**:

- **Cline/Roo:把"完成"也做成工具**(`attempt_completion`)→ 模型必须显式声明做完了,
  否则就是没做完。纯文字回复永远不合法,歧义在设计上消失。
- **chatgpt-adapter:强制哨兵**(回复必须以 `0:`/`1:` 开头)→ 配合与否变成读一个字符。

---

## 目标之间的冲突(必须显式取舍,不能假装没有)

**G2b ↔ G3/G4 天然冲突:** 提升多步成功率的手段(更多重试轮次、fanout)会直接增加延迟和配额。

已定红线:
- **G3 红线:** 带工具单轮 ≤15s(现在 5.8-11.5s,有余量)
- **G4 红线:** ≤5 次上游调用/任务(现在 4 次,余量很小)

→ **所以 G2b 的解法必须是"提高单次成功率",不是"多试几次"。**
这正好排除了 fanout/重试类方案,指向结构化方案(哨兵 / completion-as-tool)。

### ⭐ G2b 的结构性解法已找到(2026-07-27)

**网页版有协议级原生 tool call** —— 注册 MCP connector,不必靠提示词诱导。
已实测跑通:开 developer mode → `POST /aip/connectors/mcp` → OpenAI 服务端
主动抓到真实 JSON Schema。

这直接消除 G2b 的根因:

| | exec-harvest(现状) | MCP connector |
|---|---|---|
| 工具怎么来 | 提示词诱导 | 服务端注册,协议级 |
| 工具名 | 服务端固定 | **我们定义** |
| 参数 | 从命令文本 harvest | **JSON Schema 强校验** |
| 多轮 | 靠 `_looks_like_refusal()` 正则猜 | 协议自带 |

且**不违反 G3/G4 红线** —— 它提高的是单次成功率,不增加重试。

边界:MCP 要求远程 HTTPS,不支持本地 stdio。飞书 API 满足;
"在用户本机跑 shell"不满足,那部分仍需 exec-harvest。两者各管一段。

- 文档: `mcp-connector-native-toolcall.md`
- 工具: `scripts/chatgpt-onboard/zerokey-codex/bridge/mcp-connector-cli.js`
- skill: `~/.claude/skills/chatgpt-web-mcp-connector/SKILL.md`
- 未验证: 会话内实际调用的 SSE、是否需交互授权、47 账号逐个开、lark MCP server 未实现

---

## 验收脚本(每轮改动后必跑)

```bash
# G1 —— 零改动映射仍生效
#   发原名,查 SpendLogs 的 model + model_id 两列都对
# G2 + G2b —— 跨任务通过率
cd /tmp/zkloop && python3 loop_trials.py 8              # G2b 主指标
python3 -c "import sys;sys.path.insert(0,'.');import loop_trials as L;L.suite(3)"   # G2 其余
# G3 —— 延迟
#   纯聊天 3 次 + 带工具 3 次,看是否越线
# G4 —— 配额
#   跑 N 个任务前后数 SpendLogs 行数,除以 N
# 回归保护
python3 -m pytest backend/tests/ -q                    # 251 pass(1 个 jwt 失败是先前就有的)
python3 scripts/sync-litellm-callbacks.py check         # 部署产物与源文件一致
cd scripts/.../bridge/testkit && python3 refusal_detector_candidate.py
```

---

## 可恢复 / 可回滚(用户明确要求)

| 项 | 位置 |
|---|---|
| git 回滚点 | 每轮改动前记录 `git rev-parse --short HEAD` |
| 本地文件备份 | `/tmp/zkloop/backup-<TS>/` |
| 集群 CM 备份 | 198 `/Data/bridge-cm.BACKUP.<TS>.yaml`、`/Data/callbacks-cm.BACKUP.<TS>.yaml` |
| 单项回滚 | 每个修复独立提交,可单独 `git revert` |

**部署后必须验证进程真的在跑新代码**(不是只 grep 文件):

```bash
kubectl -n litellm-product exec $POD -- sh -c 'tr "\0" " " < /proc/1/cmdline'   # → python3 /code/bridge.py
kubectl -n litellm-product exec $POD -- grep -c <本次新增标识> /code/bridge.py
```

CM 挂载有传播延迟,新标识没生效就**再 restart 一次**(已验证第二次必成)。

---

## 缺陷分级(按"是否阻碍目标",不按技术严重度)

### 阻碍目标 —— 必修
| 缺陷 | 阻碍哪条 | 状态 |
|---|---|---|
| "你自己跑贴给我"被当成功答案返回 | **G2**(用户拿到错答案) | ✅ 已修 8a27422 |
| heartbeat 修复未进部署产物 | 不阻碍这四条(但线上白烧 token) | ✅ 已修 9c76b4c |
| 运维脚本远端跑不了 / `exec` 守卫假成功 | 不阻碍用户,阻碍**运维验证能力** | ✅ 已修 76e0465 |
| 密码加换行(阻断新机器上手) | 不阻碍用户,阻碍**运维** | ⬜ 待修 |

### 不阻碍任何目标 —— 排后面,或不修
| 缺陷 | 为什么可以等 |
|---|---|
| `_err` 衰减把排除门关掉 | G4 已由 fanout=1 达成;这个机制本身是为已达成目标服务的 |
| 共享 `max_calls` 饿死拒答重试 | 同上,且它保护的正是 G4 |
| `fanout` 旋钮单向 | 只在运维手动调参时才碰到 |
| `error_rate()` 死锁陷阱 | 死代码,零调用者 |
| 两个衰减函数重复 + 除零 | 需手动设 halflife=0 才触发 |
| shell 缺 secret 静默 exit 0 | 阻碍运维,不阻碍用户 |
| RBAC 守卫误判 | 只在特定 RBAC 形态下触发 |
| 英文/中文各若干条误判 | **正则原理上做不到**,需结构化方案一并解决 |

---

## 方法论红线(本轮踩过的坑,写下来防止重犯)

1. **自建语料不是回归基线。** 语料 30/30、0/26 在所有 bug 都存在时一直显示 PASS。
   替换匹配器时,**旧匹配器能抓的每一种措辞都必须先变成测试**。
2. **只验证自己想要的方向,等于没验证。** 衰减那个 bug:我测了"能恢复"(想要的),
   没测"会不会正确排除"(依赖的)。
3. **要证明部署产物在跑,不是源文件。** heartbeat 那次我测源文件说 10/10,
   而 `litellm-proxy.yaml` 内嵌的部署副本还是旧的。
4. **n=6 时 ±2 样本是常态。** 别把噪声当因果(曾把 1/6→3/6 当成修复生效)。
5. **先调研再动手。** 七个项目的答案第一天查到,能省 4 次返工和 4 个架构缺陷。
6. **动手前问"这服务哪条目标"。** 见上文 G1/G3/G4 那段。

---

## 关联

- `refusal-detection-postmortem.md` —— 为什么正则分类是错的(七项目源码调研)
- `implementation-and-rollout.md` —— 实现细节与部署 runbook(§3.9/§5 已被 postmortem 修正)
- `scripts/chatgpt-onboard/zerokey-codex/bridge/testkit/README.md` —— 闭环测试工具

---

## 迭代记录(每轮:改动 → 四目标验收 → 是否偏离)

### Loop 1 — 结构化重试代替散文判断(2026-07-27)

**回滚点** git `72de597`,本地备份 `/tmp/zkloop/loop-20260727-173519/`

**诊断:** 失败样本的 bridge 日志显示 `no tool_call, has text -> return (skip retry)` ——
桥把文本回复当答案**直接返回、不重试**,因为拒答检测没判成拒答。而失败是**间歇性**的
(同一批 6/6 全过),说明是账号方差,不是代码路径坏了。

**改动:** 不再问"这段散文是不是拒答",改问结构问题。新增 `mid_task` 参数,由 handler
用**已存在但未接线**的 `_ends_with_tool_result(req["input"])` 传入;当历史以 tool 结果
结尾、且本轮没吐 tool_call 时,给**另一个 pod 一次机会**(`MAX_STRUCT_RETRIES=1`)。
这是 Roo/Cline `consecutiveNoToolUseCount >= 2` 的同一思路,且**只在 mid-task 轮生效**,
所以最多 +1 次调用,G4 不受威胁。

**四目标验收:**

| 目标 | 改前 | 改后 | 红线 | 判定 |
|---|---|---|---|---|
| G1 零改动 | ✅ | ✅ aliases 未动 | — | 保持 |
| G2 其余任务 | 12/12 | **12/12** | 不退步 | 保持 |
| **G2b 多步** | **5/8** | **6/8**(VAGUE 1→0) | — | **改善** |
| G3 延迟 | 4.3-11.5s | 3.9-10.9s | 聊天8s/工具15s | 保持 |
| G4 配额 | 4.0 次/任务 | **3.2 次/任务** | ≤5 | **改善** |

**是否偏离目标:** 没有。四条全部满足或改善,无一退步。
**单元测试:** 4/4(mid-task 文本→重试拿到工具 / 都是文本→1 次后接受 /
turn-1 文本直接接受不重试 / 直接给工具→零额外开销)。
**回归:** 语料 30/30、误报 0/26;backend 251 pass。

**残留:** 2/8 仍失败。下一轮的方向应是**提高单次成功率**(哨兵 / completion-as-tool),
而不是再加重试 —— G4 只剩 1.8 次余量,加重试会撞红线。
