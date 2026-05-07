# Agent Safety Hooks — 设计原理

记录 4 种安全 hook 模式的来源、为什么这么设计、以及不要为了简化而删掉哪些细节。

---

## 1. Fact-Forcing Gate（取证强制）

### 来源

[`affaan-m/everything-claude-code`](https://github.com/affaan-m/everything-claude-code) `scripts/hooks/gateguard-fact-force.js`（416 行）。
原文件相对路径：`../everything-claude-code/scripts/hooks/gateguard-fact-force.js`（GitHub: `affaan-m/everything-claude-code`）

### 核心洞察

> 问 LLM "你确定吗" 永远得到 yes。问"列出所有 import 此文件的地方"，它会被迫真的去用 Grep 找一遍。**调查行为本身产生自我评估永远产生不了的觉察。**

ECC 项目作者把这套模式发布成一个独立 pip 包 [`gateguard-ai`](https://github.com/zunoworks/gateguard)，本身就是这个想法值得独立产品化的证据。

### 设计要点（不要简化）

| # | 细节 | 为什么不能省 |
|---|---|---|
| 1 | **Session-keyed state**（`<dir>/state-<sessionkey>.json`） | 并行 cursor session 各自独立，不互踩。session_key 优先取 `data.session_id`，否则 hash transcript 路径，最后 fallback `cwd` |
| 2 | **30 分钟过期** | 时间长了上下文/用户意图变了，应该重新逼一次取证。不过期 = 一次取证后永远不查 |
| 3 | **Atomic write**（`.tmp.<pid>` → `rename`） | hook 高并发触发时半截文件会破坏 state |
| 4 | **Bounded growth**（`MAX_CHECKED_ENTRIES = 500`） | 长 session 会积累几千个文件，state JSON 涨到 MB 级；prune 时优先保留 session-level 标记 |
| 5 | **取证清单四类**（edit / write / destructive / routine） | 不同操作的影响面不同，"列 importer" 对 `kubectl delete pod` 没意义 |
| 6 | **Path sanitize 强制** | 防 prompt-injection 通过文件名走私（unicode bidi override / 控制字符） |
| 7 | **Bash 只读白名单 + 禁止 shell 元字符** | `git status` 总是放行，但 `git status; rm -rf /` 必须拦 |
| 8 | **`.claude/settings*.json` 豁免** | claude/cursor 自己要写设置，硬拦会卡死 |

### 在 carher 的特化

替换原 ECC 的"列 importer / list public API / data schema"取证清单为运维场景：

| 场景 | carher 取证清单要点 |
|---|---|
| 改 K8s yaml | `kubectl describe` 现状 + 影响实例名单 + rollback 命令 + rollout window |
| 改 LiteLLM callback | 列出注册的 hook 类型 + ConfigMap 现状 + hot-reload 还是 restart + 灰度 gate + 回归测试 |
| 跑破坏性 kubectl | 列资源名 + WebSocket 影响 + rollback + 时间窗口 |
| 任何 Bash 第一次 | 复述用户请求 + 此命令验证什么 |

### 反例

**不要这样写**：

```js
// 错误示范：用一个全局 set 而不是 session-keyed
const checked = new Set();
function isChecked(p) { return checked.has(p); }
```

进程退出 set 就丢了，hook 每次启动都重新逼一次取证，agent 永远卡住。

**也不要这样写**：

```js
// 错误示范：取证清单写得太宽泛
return "Before editing, think about what you're doing.";
```

LLM 看到这种话只会回 "I'm carefully considering this change" 然后照样改。**取证清单必须可机械执行**（"run kubectl describe X"、"grep for Y"），LLM 不能用文字回答糊弄过去。

---

## 2. Config Protection（硬保护）

### 来源

ECC 的 `scripts/hooks/config-protection.js`（141 行）。

### 核心洞察

LLM 修代码失败时，下意识的 fallback 是**改规则放过去**：

- ruff 报错 → 加 `ignore = ["E501"]`
- pytest 失败 → 加 `@pytest.mark.skip`
- 没 type → 改 `pyproject.toml` 的 `strict = false`

这种行为在每个使用 cursor / claude code 的工程团队里都见过。Config Protection 的方案：**这些文件根本不让 agent 改**，逼它去改源码。

### 设计要点

| # | 细节 | 为什么 |
|---|---|---|
| 1 | **basename 匹配，不是全路径** | 仓库里多个目录可能有 `.eslintrc`，全部命中 |
| 2 | **`pyproject.toml` 不在死保护** | 因为它同时是依赖管理；改 deps 是合法操作；改 `[tool.ruff]` 段需要 fact-force（强约束）而不是死保护 |
| 3 | **Truncated stdin = exit 2** | 安全 hook 永远 fail-closed。input 截断时不能因为没看到完整内容就放行 |
| 4 | **stderr 写"如何合法绕过"** | 给 agent / 用户留退路（`CARHER_PROTECT_CONFIG=0`），否则会被卡死无法做合法变更 |

### 在 carher 的扩展

ECC 原版只覆盖 lint / formatter。在 carher 加了第二类：**prod resource 死保护**（`k8s/litellm-proxy.yaml` / `k8s/her-instance-template.yaml` / `cloudflare/tunnels/*.json`）。

注意这第二类有 fact-force 已经覆盖了，**为什么还要 config-protect 再拦一层**？

> **多层防御**：fact-force 是"取证后放行"，agent 可以胡编一份取证清单（虽然要它真去 grep / kubectl describe，但可以装样子）。config-protect 是绝对拒绝，要改必须人工介入（`CARHER_PROTECT_CONFIG=0` 临时关闭）。两层一起做才靠谱。

---

## 3. Path Sanitize（防 prompt-injection）

### 来源

ECC 的 `gateguard-fact-force.js` 里的 `sanitizePath()` 函数（约 13 行）。

### 核心威胁模型

文件名可以含 unicode bidi override 字符（`U+202E` 等），让人眼里看到的文件名和实际不一样：

```
agent 看到的："update_config.py"
实际文件名："update_‮yp.gifnoc"  // U+202E 反转后半段
```

或者文件名含控制字符直接破坏 hook 的解析逻辑。

### 不严重？

在普通 prompt 里也许不严重，但 hook 把文件名拼进取证清单里给 LLM 看的时候很严重 —— **LLM 看到的内容可能和你 stdin 给它的不一样**。

### 在 carher 的应用

`carher-fact-force.template.js` 的 `sanitizePath()` 已经实现：剥离

- ASCII 控制字符（`\x00-\x1f, \x7f`）
- Unicode bidi override（`U+200E-200F` LRM/RLM, `U+202A-202E` LRE/RLE/PDF/LRO/RLO, `U+2066-2069` LRI/RLI/FSI/PDI）

并截断到 500 字符防超长 path 干扰展示。

任何处理文件名的新 hook 都应该在入口处过一遍 `sanitizePath()`。

---

## 4. No-Personal-Paths CI（防个人路径泄漏）

### 来源

ECC 的 `scripts/ci/validate-no-personal-paths.js`（64 行）。

### 核心问题

调试时 agent 经常生成路径写死的脚本 / 文档：

```bash
# bad
SOURCE_DIR=$REPO_ROOT/k8s/...

# good
SOURCE_DIR="$(git rev-parse --show-toplevel)/k8s/..."
```

这些一旦 commit 进 docs / skills / rules，团队成员 clone 之后就坏了。

### 实现

`scripts/check-no-personal-paths.sh` 用 `rg`（你机器上有）扫指定目录，正则匹配本机绝对路径模式（脚本顶部 `PATTERNS` 数组定义，按需加你团队成员的 username），命中就 exit 1 + 列出文件 + 行号。

### 在 carher 怎么用

挂到三个地方任选：

1. **GitHub Actions**（推荐）：每次 PR 自动跑，命中失败
2. **pre-commit hook**：本地 commit 前自动跑
3. **手动**：每周跑一次大扫除

加进 GitHub Actions 的 yaml 片段：

```yaml
- name: Check no personal paths
  run: bash .cursor/skills/agent-safety-hooks/scripts/check-no-personal-paths.sh
```

---

## 总结：4 种模式的关系

```
agent 想做某事
   │
   ▼
┌───────────────────────────────────────────────┐
│  Path Sanitize                                │
│  (任何 hook 入口；防 prompt-injection)        │
└────────────────┬──────────────────────────────┘
                 │
                 ▼
┌───────────────────────────────────────────────┐
│  Config Protection                            │
│  (lint / formatter / prod yaml；硬拦 exit 2)  │
└────────────────┬──────────────────────────────┘
                 │ (未命中)
                 ▼
┌───────────────────────────────────────────────┐
│  Fact-Forcing Gate                            │
│  (k8s yaml / litellm callback / 破坏性 bash;  │
│   第一次拒绝 + 取证清单，第二次放行)          │
└────────────────┬──────────────────────────────┘
                 │ (取证后)
                 ▼
            agent 操作放行
                 │
─────────────────┴──────────────────────────────
                 (commit 时)
                 ▼
┌───────────────────────────────────────────────┐
│  No-Personal-Paths CI                         │
│  (扫 docs/skills/rules/k8s/backend，          │
│   命中 /Users/<dev>/ 就 fail PR)              │
└───────────────────────────────────────────────┘
```

四层不重叠，组合起来覆盖：
- **运行时拦截**（前三层）：Path Sanitize → Config Protection → Fact-Forcing
- **提交时拦截**（第四层）：No-Personal-Paths CI

---

## 不要做的事

ECC 仓库里还有一些更激进的 hook 模式（`continuous-learning-v2` / `governance-capture` / `gateguard-fact-force` 升级版的 `pre:edit-write:gateguard-fact-force`）。**carher 当前阶段不要全装**，原因：

1. carher 是运维仓库不是产品代码仓库，hook 太多会卡死日常运维操作
2. 你已经有 30+ 业务 skill，每个都是经过沉淀的"取证清单"，不需要再加一层自动学习
3. ECC 的 hook 之间有依赖（`scripts/hooks/plugin-hook-bootstrap.js` / `run-with-flags.js` 一套），单独抠出来一两个最容易维护

**先做 4 种模式的 MVP，跑 2-3 周看效果，再决定要不要扩展到 ECC 的其他 hook。**
