---
name: agent-safety-hooks
description: >-
  给 carher-admin 增加防御性 cursor/claude hook，拦截 LLM agent 的高风险动作 ——
  改 k8s prod config、跑破坏性 kubectl、改 lint 配置放宽规则、误带个人绝对路径
  入仓。提供 4 种安全 hook 模式（Fact-Forcing Gate / Config Protection /
  Path Sanitize / No-Personal-Paths CI）的设计原理 + carher 特化的取证清单
  + 可改可用模板。Use when the user mentions "防止 agent 误操作 / 拦截 hook /
  fact-forcing / 取证 gate / 保护 prod config / 防 LLM 改回放宽规则 / 怎么写
  cursor hook 阻止 X / pre-tool-use 拦截 / 防 prompt-injection 走私文件名"，
  or whenever a new "agent shouldn't do X without verification" rule needs
  to be enforced as a hook (not just documented in another skill). 本 skill
  专做"安全 / 防误操作 hook"，不做 LiteLLM 业务 hook（请用 litellm-hook-dev）。
---

# Agent Safety Hooks

把"先检查再改"的纸面流程变成 hook 层强制执行的硬约束。**不依赖 agent 自觉**。

设计模式来自 [`affaan-m/everything-claude-code`](https://github.com/affaan-m/everything-claude-code) 的 `scripts/hooks/` 体系（参考 `references/design-rationale.md`），针对 carher-admin 的 200+ her 实例运维场景重写了取证清单。

## 何时使用本 skill

| 场景 | 用本 skill |
|---|---|
| "agent 又想改 `k8s/litellm-proxy.yaml` 的探针，让 OOM 告警表面消失" | **Fact-Forcing Gate** —— 第一次 Edit 时拒绝并强制取证 |
| "agent 把 ruff 的 `ignore` 列表加长来过 lint，而不是修代码" | **Config Protection** —— 硬拦 lint/formatter config 修改 |
| "agent 想跑 `kubectl delete pod`、`kubectl scale --replicas=0`、`kubectl drain`" | **Fact-Forcing Gate** 的 destructive 分支 —— 强制写回滚命令 + 复述用户原话 |
| "我担心提交到 GitHub 的 docs / skill 里带 `/Users/<your-name>/`" | **No-Personal-Paths CI** —— 直接可用脚本，加进 CI |
| "文件名里可能藏 prompt-injection（unicode bidi override / 控制字符）" | **Path Sanitize** —— 嵌进任何 hook 的入口处 |
| "想给 carher-instance-config-override 加上 hook 强制执行 7 步检查" | 用本 skill 的 fact-force 模板，把检查清单填进 `editGateMsg` |

## 何时不使用本 skill

- 写 LiteLLM 的 **业务** hook（请求改写 / 流式打点 / SSE 心跳）→ `litellm-hook-dev`
- 不是 hook 而是 cursor 的 rule（`.cursor/rules/*.mdc`）→ 直接在 `.cursor/rules/` 写 mdc
- 已经发生的误操作的回滚 → `her-oom-alert-triage` / `litellm-ops` / `verify-fix-callback-dns`
- 想做"agent 自我学习运维 instinct"那套自动化提炼 → 不是本 skill；ECC 的 `continuous-learning-v2` 思路单独参考，本 skill 只做"硬拦截"

## 4 种安全 hook 模式

| 模式 | 触发点 | 行为 | 适合 |
|---|---|---|---|
| **Fact-Forcing Gate** | PreToolUse: Edit/Write/Bash | 第一次拒绝 + 给 agent 一份取证清单（"列出 importer / public API / data schema / 一字不差复述用户指令"），第二次放行 | 中等风险操作：改 k8s yaml、改 LiteLLM callback、跑非破坏性但影响面大的 kubectl |
| **Config Protection** | PreToolUse: Edit/Write | basename 命中保护清单 → 直接 exit 2，不给 agent 重试机会 | 死保护：lint/formatter config、生产探针/资源限制 spec |
| **Path Sanitize** | 任何 hook 的入口 | 剥离控制字符 + Unicode bidi override (`U+200E-200F / U+202A-202E / U+2066-2069`) + 截断 500 字符 | 任何 hook 处理 file_path 之前都要过一遍，防 prompt-injection 走私 |
| **No-Personal-Paths CI** | CI 阶段（不是 hook） | 扫 docs/skills/rules/AGENTS.md，正则匹配本机绝对路径 → exit 1 | 防止 commit 误带 `/Users/<name>/` 等 |

## 决策树：碰到一个新场景怎么选

```
agent 要做某事 X
   │
   ▼
X 是不是 100% 不该发生？
   │
   ├── 是 ──► Config Protection（硬拦，命中文件名 exit 2）
   │
   └── 否 ──► X 是不是值得 agent 先证明它理解了影响面？
              │
              ├── 是 ──► Fact-Forcing Gate
              │          │
              │          ▼
              │       根据风险写 4 类取证清单之一：
              │       - editGateMsg：改现有文件
              │       - writeGateMsg：创建新文件
              │       - destructiveBashMsg：rm -rf / 破坏性 kubectl
              │       - routineBashMsg：每 session 第一次 Bash
              │
              └── 否 ──► 不需要 hook；写进 .cursor/rules/ 或对应 skill 文档即可
```

## carher-admin 必须保护的文件清单

参考 `references/protected-files-catalog.md`。简版：

| 类别 | 文件 / 模式 | 推荐模式 |
|---|---|---|
| K8s prod config | `k8s/litellm-proxy.yaml` 的探针/资源段 | Fact-Forcing |
| K8s prod config | `k8s/her-instance-template.yaml` 的资源限制 | Fact-Forcing |
| LiteLLM callback | `k8s/litellm-callbacks/*.py`（已有 hook 在跑流量） | Fact-Forcing |
| Cloudflare 路由 | `cloudflare/tunnels/*.json` | Fact-Forcing |
| Lint config | `pyproject.toml` 的 `[tool.ruff]` / `eslint.config.js` / `.markdownlint.json` | Config Protection |
| Test config | `pytest.ini` / `backend/pytest.ini` / `setup.cfg` | Config Protection |
| 破坏性脚本入口 | `scripts/jms` 加包装层时 | Fact-Forcing 的 destructive 分支 |

## 落地步骤（用 Fact-Forcing Gate 举例）

1. **拷模板**

   ```bash
   cp .cursor/skills/agent-safety-hooks/scripts/templates/carher-fact-force.template.js \
      .cursor/hooks/carher-fact-force.js
   ```

2. **改取证清单**

   打开 `carher-fact-force.js`，找到 `editGateMsg` / `destructiveBashMsg` 等函数，把里面的取证 1/2/3/4 步替换成你这次场景的具体内容。
   - 改 k8s yaml 的清单：见 `references/protected-files-catalog.md` 的 `kube-config` 段
   - 改 LiteLLM callback 的清单：见同文件 `litellm-callback` 段
   - 跑破坏性 kubectl 的清单：见同文件 `destructive-kubectl` 段

3. **改路径分类**

   找到 `classifyFilePath()`，把 carher-admin 的路径模式加进去：
   ```js
   if (/\/k8s\/litellm-proxy\.ya?ml$/.test(norm)) return 'kube-config-litellm';
   if (/\/k8s\/litellm-callbacks\/.+\.py$/.test(norm)) return 'litellm-callback';
   ```

4. **改破坏性正则**

   找到 `DESTRUCTIVE_BASH`，加 carher 场景：
   ```js
   const DESTRUCTIVE_BASH = /\b(rm\s+-rf|kubectl\s+(delete|drain|cordon)|kubectl\s+rollout\s+restart|kubectl\s+scale\s+.*--replicas=0|helm\s+(uninstall|delete))\b/i;
   ```

5. **注册到 cursor hook**

   在 `.cursor/hooks.json`（如果不存在就新建）里加 PreToolUse entry，指向 `carher-fact-force.js`。具体格式参考 cursor 文档。

6. **干跑测试**

   ```bash
   echo '{"tool_name":"Bash","tool_input":{"command":"kubectl delete pod foo"},"session_id":"test-1"}' \
     | node .cursor/hooks/carher-fact-force.js
   ```
   预期：第一次 stdout 是 deny JSON + 取证清单，第二次同样输入是空（放行）。

7. **30 分钟自动重置**

   过 30 分钟同一个文件 / 同一个破坏性命令会再被强制取证一次。可以通过 `GATEGUARD_STATE_DIR` 环境变量改 state 路径。

## Config Protection 落地（更简单）

```bash
cp .cursor/skills/agent-safety-hooks/scripts/templates/carher-config-protect.template.sh \
   .cursor/hooks/carher-config-protect.sh
chmod +x .cursor/hooks/carher-config-protect.sh
```

打开 `carher-config-protect.sh`，把 `PROTECTED_BASENAMES` 里的列表按你环境调整（默认已经包含 lint config + carher prod config 的合理初始集）。

## No-Personal-Paths CI 落地（最简单，10 分钟）

直接用，不需要改：

```bash
# 加进 GitHub Actions、pre-commit 或本地 git hook
.cursor/skills/agent-safety-hooks/scripts/check-no-personal-paths.sh
```

它会扫 `docs/`、`.cursor/skills/`、`.cursor/rules/`、`AGENTS.md`、`README.md`、`k8s/`、`backend/`，命中本机绝对路径模式（脚本里 `PATTERNS` 数组定义）就 exit 1。

> **首次跑会发现的现存 finding（2026-04-29 验证）**：
> `.cursor/skills/carher-upgrade-compare/SKILL.md` 里有 5 处硬编码 `/Users/Liuguoxian/codes/carher`。
> 修复方式：把 `cd /Users/Liuguoxian/codes/carher` 改成 `cd "${CARHER_REPO:-../carher}"`，
> 或在文档开头加 `> 假设 carher 主仓库在 ../carher 目录` 说明，然后用相对路径。
> 不修就把这条 CI 加成"warn-only"先不阻塞合并，逐步收敛。

## 与现有 skill 的边界

| 你想做什么 | 用哪个 skill |
|---|---|
| **写一个安全 hook** 拦截 agent 高风险动作 | **本 skill** |
| 写一个 LiteLLM 业务 hook（请求改写、SSE 心跳、TTFT 打点） | `litellm-hook-dev` |
| 已经发生的 OOM / 实例卡死的处置 | `her-oom-alert-triage` / `her-memory-reindex-rescue` |
| 已经发生的 502 / DNS / 回调断 | `verify-fix-callback-dns` / `cloudflare-tunnel-routing` |
| ConfigMap 改了但 pod 没生效 | `k8s-configmap-mount-debug` |
| 做灰度 rollout | `hot-grayscale` / `carher-k8s-zero-downtime-rollout` |

## 关键设计要点（不要漏的工程细节）

直接抄 ECC 的，不要简化：

1. **Session-keyed state**：每个 session 独立 state file（`<state-dir>/state-<sessionkey>.json`）。session_key 优先取 `data.session_id`，否则 hash transcript 路径，最后 fallback `cwd`。这样并行 cursor session 不互踩。
2. **30 分钟过期**：超时清空 checked 列表，重新逼一次取证。否则 agent 一次取证就永远不查了。
3. **Atomic write**：`<file>.tmp.<pid>` → `rename`，避免半截文件被读到。
4. **Bounded growth**：`MAX_CHECKED_ENTRIES = 500`、`MAX_SESSION_KEYS = 50`，防 state 文件无限大。
5. **Path sanitize 强制**：所有进入取证清单的 file_path 都要剥控制字符 + Unicode bidi override。**这是防 prompt-injection 走私文件名的关键。**
6. **Fail-closed on truncated stdin**：hook 拿到截断的 input 时不要放行，应该 exit 2。安全 hook 永远 fail-closed。
7. **`.claude/settings*.json` 豁免**：cursor / claude 自己经常要写设置，硬拦会卡死。同理放行 cursor 自己的内部路径。
8. **只读 git introspection 白名单**：`git status --porcelain`、`git log --oneline`、`git rev-parse --abbrev-ref HEAD` 这些直接放行，且要求**不能含 shell 元字符**（`/[\r\n;&|><`$()]/`）—— 防注入。

模板里这 8 点都已经写好，**不要为了简化删掉它们**。

## 模板里要改的位置（速查）

打开 `scripts/templates/carher-fact-force.template.js`，搜以下注释标记：

| 标记 | 你要做什么 |
|---|---|
| `// CARHER:CUSTOMIZE classify` | 加你这个 carher 项目要分类的路径模式 |
| `// CARHER:CUSTOMIZE destructive-regex` | 加你想拦的 kubectl/helm 破坏性命令 |
| `// CARHER:CUSTOMIZE gate-msg` | 改取证清单为 carher 运维场景 |
| `// CARHER:CUSTOMIZE allowlist` | 加额外豁免路径（cursor 内部、tmp 文件等） |

## ECC 原文件参考

如果你要回查原始实现做交叉验证（路径相对本仓库根，假设 `everything-claude-code/` clone 到 carher-admin 同级目录）：

- Fact-Forcing Gate：`../everything-claude-code/scripts/hooks/gateguard-fact-force.js` (416 行)
- Config Protection：`../everything-claude-code/scripts/hooks/config-protection.js` (141 行)
- No-Personal-Paths CI：`../everything-claude-code/scripts/ci/validate-no-personal-paths.js` (64 行)
- Hook dispatcher 设计（一次 node 启动跑多个 hook + profile gating）：`../everything-claude-code/scripts/hooks/bash-hook-dispatcher.js`

GitHub 直链：[affaan-m/everything-claude-code](https://github.com/affaan-m/everything-claude-code/tree/main/scripts/hooks)。

更详细的设计原理见 `references/design-rationale.md`。
