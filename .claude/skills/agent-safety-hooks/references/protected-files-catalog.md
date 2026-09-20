# carher-admin Protected Files Catalog

完整列出本仓库里需要被 safety hook 保护的文件 / 路径模式，以及每类文件应该用哪种 hook、对应的取证清单是什么。

> **维护原则**：清单要"少而准"。装得太多 agent 会被卡到无法工作；装得太少防不住误操作。每加一行都写清"为什么这是高风险"。

---

## 分类原则

| 风险等级 | 处理 | 对应 hook |
|---|---|---|
| **死保护**：100% 不该被 LLM 直接改 | basename 命中即拦，无重试 | `carher-config-protect.sh` |
| **强约束**：可以改但必须先取证 | 第一次拒绝 + 取证清单，第二次放行 | `carher-fact-force.js` 的 Edit/Write 分支 |
| **破坏性命令**：跑之前必须证明影响面 | 第一次拒绝 + 列资源 + 写回滚，第二次放行 | `carher-fact-force.js` 的 Bash 分支 |

---

## 死保护清单

### Lint / Formatter / Test config

| 文件 | 为什么死保护 |
|---|---|
| `pyproject.toml` 的 `[tool.ruff]` 段 | agent 倾向加 ignore 让代码"通过 lint"，但代码本身没改对 |
| `eslint.config.js` / `.eslintrc*` | 同理 |
| `.markdownlint.json` | 你已经在用，加 rule 容易造成跨文档不一致 |
| `pytest.ini` / `.coveragerc` | agent 想 skip 失败的测试，正确做法是修代码 |
| `.prettierrc*` / `biome.json` / `.stylelintrc*` | 格式化规则不该被改放宽 |
| `.shellcheckrc` | shell 安全规则不该被改放宽 |

> **注意**：`pyproject.toml` 整体不在死保护清单（因为它同时是依赖管理）；只有当 agent 试图修改它时，由 `carher-fact-force.js` 的 generic edit 分支强制取证。

### Carher prod resource

| 文件 / 路径 | 为什么死保护 |
|---|---|
| `k8s/litellm-proxy.yaml` | 改探针 / 资源限制让 OOM 告警表面消失但埋雷的真实事故已发生过 |
| `k8s/her-instance-template.yaml` | 200+ 实例的 template，一动全动 |
| `cloudflare/tunnels/*.json` | 200+ 实例的 OAuth callback 路由全在这；改错全部 502 |

---

## 强约束清单（要取证才能改）

### Class A: K8s YAML（generic）

**触发**：`carher-fact-force.js` 的 `classifyFilePath()` 返回 `kube-config-litellm` / `kube-config-her` / `kube-config-generic`

**取证清单**（写在 `editGateMsgKubeConfig`）：

```
1. Run kubectl describe for the resource this file targets and paste the current spec
   (e.g., kubectl -n <ns> describe deploy/<name> or kubectl get cm <name> -o yaml)
2. Identify which her instances / pods / deployments this change affects (count + names)
3. Show the exact rollback command (kubectl rollout undo deployment/<name>)
4. State the blast radius: how many pods will restart? Estimated rollout window?
   Will any in-flight WebSocket / streaming session be interrupted?
5. Quote the user's current instruction verbatim (one paragraph)
```

**为什么这 5 项**：
- 第 1 项：agent 经常在不读 spec 的情况下改 yaml，这步强制拉一遍现状
- 第 2 项：影响面量化，不是"some pods"而是"具体 12 个 her 实例"
- 第 3 项：rollback 命令必须**可复制粘贴**才算数
- 第 4 项：rollout window 直接对应是否影响用户消息
- 第 5 项：复述用户原话防 agent 自己加戏

**对应 skill**：与 `carher-deploy` / `carher-admin-deploy` / `hot-grayscale` / `carher-k8s-zero-downtime-rollout` 配合使用。本 hook 是这些 skill 的硬执行层。

---

### Class B: LiteLLM Callback Module

**触发**：`carher-fact-force.js` 返回 `litellm-callback`，匹配模式 `k8s/litellm-callbacks/*.py`

**取证清单**（写在 `editGateMsgLitellmCallback`）：

```
1. List which callback hooks this file registers (async_pre_call_hook /
   async_post_call_streaming_iterator_hook / log events / module-level monkey-patches)
2. Show the current ConfigMap contents:
   kubectl -n carher get cm litellm-callbacks -o yaml
3. Confirm: does this change require a pod restart, or is it hot-reloaded?
   Reference: skills/k8s-configmap-mount-debug
4. Identify the canary/grayscale plan: which pods or env-var gate sees this change first?
5. Confirm a regression test exists in k8s/litellm-callbacks/tests/
6. Quote the user's current instruction verbatim
```

**为什么这 6 项**：
- 第 1 项：现有 callback 列表（`opus_47_fix.py` / `embedding_sanitize.py` / `streaming_bridge.py`）都在跑生产流量，改之前必须知道它注册了什么 hook 类型
- 第 2 项：ConfigMap 是 single source of truth，必须对齐
- 第 3 项：你已经踩过 ConfigMap subPath 不同步的坑（见 `k8s-configmap-mount-debug`），这步逼 agent 想清楚是 hot reload 还是要 restart
- 第 4 项：你的 `litellm-hook-dev` skill 强制要求灰度 gate（key prefix / env var），但纸面要求不强制；这里强制
- 第 5 项：你的 `litellm-hook-dev` 要求 `k8s/litellm-callbacks/tests/` 必须有回归测试，本步是硬执行
- 第 6 项：常规

**对应 skill**：与 `litellm-hook-dev` / `litellm-ops` / `k8s-configmap-mount-debug` 配合使用。

---

### Class C: Cloudflare Tunnel Config

**触发**：`classifyFilePath()` 返回 `cf-tunnel`

**取证清单**（建议加，目前模板里是 generic）：

```
1. Run cloudflare tunnel ls / cf api 列出当前 ingress rules + 影响的 hostname 列表
2. Confirm DNS records pointing to this tunnel
   (use scripts in skill: verify-fix-callback-dns)
3. State which her instances depend on this routing
   (search by hostname pattern: <name>.carher.net)
4. Provide the rollback: previous tunnel config snapshot path
5. Quote the user's instruction verbatim
```

**对应 skill**：与 `verify-fix-callback-dns` / `cloudflare-tunnel-routing` 配合。

---

## 破坏性命令清单

`carher-fact-force.js` 的 `DESTRUCTIVE_BASH` 正则覆盖：

| 命令 | 风险 |
|---|---|
| `rm -rf` | 经典 |
| `git reset --hard` / `git checkout --` / `git clean -f` | 丢未提交工作 |
| `git push --force` | 改远端历史 |
| `dd if=` | 块设备级写入 |
| `drop table` / `delete from` / `truncate` | DB 数据丢失 |
| `kubectl delete` / `kubectl drain` / `kubectl cordon` | pod / node 直接干掉 |
| `kubectl rollout restart` | 全量滚动重启，影响所有用户消息 |
| `kubectl scale ... --replicas=0` | 下线整个服务 |
| `kubectl exec ... rm` | 跨容器破坏数据 |
| `helm uninstall` / `helm delete` | release 全删 |

**取证清单**（写在 `destructiveBashMsg`）：

```
1. List ALL resources this command will modify or delete
   (pods / PVCs / deployments / configmaps by name, not just count)
2. State expected user impact:
   - WebSocket disconnect? Message loss?
   - Affected user count?
   - Time window before service resumes?
3. Write the EXACT rollback command
4. Confirm timing: is this a low-traffic window? Match canary/grayscale practice?
5. Quote the user's current instruction verbatim
```

---

## 白名单（永远放行，不要拦）

| 路径 / 命令 | 为什么放行 |
|---|---|
| `.claude/settings*.json` | claude 自己要改 |
| `.cursor/state/` / `.cursor/cache/` | cursor 自己要改 |
| `git status --porcelain` / `git log --oneline` / `git rev-parse --abbrev-ref HEAD` | 只读 introspection，无副作用 |
| `kubectl get` / `kubectl describe` / `kubectl logs` / `kubectl top` / `kubectl explain` | 只读 introspection |

**前提**：命令不能含 shell 元字符（`\r\n;&\|><\`\$()`），防 agent 通过 `kubectl get pods; rm -rf /` 这种走私破坏性命令。

---

## 维护清单

每次发生以下事件，更新本文件 + 对应模板：

1. **新增了一个 prod resource**（新加 ConfigMap / Deployment 类型）→ 加入 Class A 或 Class B
2. **新发生了 agent 误操作事故** → 把那次的命令模式加进 `DESTRUCTIVE_BASH`
3. **某个取证清单实战中证明不够强** → 加一项（例如发现取证后还是改错了，说明取证步骤漏了某个角度）
4. **Skill 体系新增了 skill** → 在取证清单里加交叉引用（`Reference: skills/<name>`）

不要静默扩大白名单，**每加一个白名单都要写清"为什么放行不会出事"**。
