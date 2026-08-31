---
name: zk-delta-ops
version: 1.0.0
description: >-
  Operate zk-delta — the Cursor→LiteLLM WAN incremental-transport layer on 198
  (local sidecar :8788 → nginx /zkd/ → zk-delta pod → LiteLLM → gateway → GPT).
  Use when the user says 增量传输 / zk-delta / "cursor 到 litellm 是不是增量" /
  巡检红了 / 全员发那个小代理 / 要改 zk-delta 的服务端 / 要给同事装或关掉增量,
  or when audit.sh reports drift. Covers deploy (gated on tests), the 7-leg
  read-only audit, capacity math, the team installer, and turn-off.
---

# zk-delta 运维 SOP

Cursor 每轮都把整个会话重发一遍。zk-delta 让**公网上只走增量**，在集群侧
逐字节重建成全量再交给 LiteLLM。实测 30 轮省 93.9%，长会话稳定省 75–87%。

```
Cursor → 本机小代理 :8788 → 公网只发增量 → 198 nginx /zkd/ → zk-delta
       → 逐字节重建全量 → LiteLLM → zerokey 网关 → GPT 网页
```

**重建发生在 LiteLLM 之前**，这一条同时买到两件事，是这套东西的承重结构：

1. 门①（干净载荷）**按构造成立**——上游收到的字节与不装时逐字节相同。
2. zk-delta **按构造改不了** ChatGPT 侧 24h/7d 配额消耗。所以"会不会影响
   codex 那条线"这个问题的答案是结构性的 no，不依赖谁的自律。

## 关键坐标

| 项 | 值 |
|---|---|
| namespace | `litellm-product`（198，`sudo k3s kubectl`） |
| deploy / svc / cm | `zk-delta` / `zk-delta` / `zk-delta-src`（源码用 CM 挂进容器） |
| 公网入口 | `https://cc.auto-link.com.cn/zkd/`（nginx `location ^~ /zkd/`） |
| 本机小代理 | `127.0.0.1:8788`，launchd `KeepAlive`，跑 **Cursor 自带 Electron** |
| 仓库 | `zk-delta/`（server/ common/ k8s/ tests/ sidecar/） |
| 分发安装器 | `scripts/zk-cursor-web/cursor_team_setup.js` |

**三条运行时不变量，破一条就静默退化**（不报错，只是白装或错位）：
`replicas=1`、`strategy=Recreate`、集群跑的源码 == 仓库这份。
会话状态在**进程内存**里，多副本各存各的 → 狂 409 → 全部退化成全量。

## 改服务端 → 上线

```bash
ZKD_SSH_PASS='<198口令>' ./zk-delta/k8s/apply.sh          # 步骤 0 就是回归门
ZKD_SSH_PASS='<198口令>' ./zk-delta/k8s/apply.sh --dry-run
```

`apply.sh` 步骤 0 跑 `tests/run.js`，**退出码不为 0 就不推**。这道门是必须的：
`node --check` 只证明文件能被 parse，证明不了重建出来的字节还对——而那是这个
服务的**全部**价值。已实测：注入一个行为性错误（重建时丢最后一条 item），
`node --check` 照样过，回归门拦住、退出码 1、全程没碰集群。

`ZKD_SKIP_TESTS=1` 是救火开关，会大声打印，正常上线不该出现。

**上线代价要先算**：`Recreate` + 单副本 ⇒ 活着的会话丢掉内存里的 handle，
每条下一发退化成一次全量重发（设计行为，不是故障）。推之前先看：

```bash
curl -s https://cc.auto-link.com.cn/zkd/metrics.json | python3 -m json.tool | grep -E 'conv_live|req_total'
```

`req_total` 与上次读数相同 ⇒ 期间没有新流量 ⇒ 那些会话是自己的，随便重启。

## 巡检（只读，会用退出码说话）

```bash
ZKD_SSH_PASS='<198口令>' ./zk-delta/k8s/audit.sh   # 0=全绿 1=有漂移 2=连不上
```

七组：副本/策略 · 镜像来源(`pullPolicy:Never`) · 源码指纹 · pod 现状 ·
nginx 入口 · 运行计数器 · 容量。

### 第 [6] 组红了怎么读

非 2xx **分状态码判，别一锅炖**：

- `401/403` = 客户端凭据问题，忠实透传，**不算漂移**。
- `400` = 专门在猎的形状（Cursor 长会话撞入口闸门那条线），**一发就红、不参与降级**。
- 其余硬失败：看 `upstream_last_hard_at` / `upstream_ok_since_hard` 两个字段——
  ≥100 发连续干净 **且** ≥2 小时没再犯，才降级成 `!`（照样每次打印）。

> **累计计数器分不清"历史一次"和"正在发生"。** 一发 413 会把巡检钉成永久红
> 直到有人重启 pod，而关不掉的警报等于没有警报。修法是补 recency 字段，
> **不是放松阈值**——为了让自己的产物过关去改判据，就是弯尺子。
>
> 推论：**重启之后的绿不算验证**（计数器被清零了，不是新逻辑判出来的）。
> 要验判定逻辑，抽出来灌构造数据跑分支。

### 第 [7] 组容量怎么算

单副本 + 状态在内存 ⇒ OOMKill 一次是**所有人**会话全丢，**且不报错**。
所以要盯活的 RSS，不能事后从 `restartCount` 反推。

```
rss ≈ 175MB + 1.13 × store      保守取相邻点最大边际斜率 1.24x
ZKD_MAX_BYTES=800MB  撑满 → RSS ≈ 1169MB，2Gi limit 下安全
ZKD_MAX_CONV=400 单独 → 1847MB store → RSS ≈ 2471MB → 拦不住，会 OOM
⇒ 真正在拦的是**字节上限**，两条都得留着
```

**"哪条上限在拦"随会话大小翻转，别背结论**：巡检那句话是**当场按均值算的**——
`400 × 均值`；均值 4.6MB（长会话）时字节上限先到，均值 0.8MB（刚重启、都是短会话）时
条数上限先到。上面那个"会 OOM"的算例取的是长会话，是**坏情况**，所以两条都得留。

**不许用 `rss/store` 这个总倍率**——它被固定基座污染，store 小时飙到 20x，
照它算出来的上限小得荒唐。要拟合"多存 1MB，RSS 多涨多少"。
重算用 `node zk-delta/tests/capacity_probe.js`（纯本地，**一发不打上游**）。

## 回归

| 脚本 | 打什么 |
|---|---|
| `tests/run.js` | 离线 45 项，含真 Cursor 抓包金样重放；**apply.sh 已内置** |
| `tests/live_multiturn.js` | 真流量多轮，验省了多少 + 逐字节重建 |
| `tests/capacity_probe.js` | 内存边际拟合，纯本地 |
| `k8s/codex7d_guard.sh mine 3` | 按 小时 × model_group 看自己打了谁 |

```bash
ZKD_KEY=<LITELLM_MASTER_KEY> node zk-delta/tests/live_multiturn.js local-deepseek-v4-flash 6 100
```

**回归一律打 `local-deepseek-v4-flash`（自建 GPU 盒），不碰 ChatGPT 额度。**
注意该上游 body 上限约 1.64MB，超了返 413——那是它的限制，不是 zk-delta 的。

> **判"有没有吃 codex 额度"只能按模型归因。** 7 天窗口是滑动的，前后 `diff`
> 只在集群空闲时才等于"我打的量"；上班时段 100% 假阳（实测同一小时
> 别人 `gpt-5.6-sol` 4165 发 vs 我 26 发）。别用 `snap`/`diff` 下这个结论。

## 给同事装 / 关

```bash
node scripts/zk-cursor-web/cursor_team_setup.js --apply                      # 装机（会换模型）
node scripts/zk-cursor-web/cursor_team_setup.js --apply --zk-delta-only      # 只装增量，不碰模型
node scripts/zk-cursor-web/cursor_team_setup.js --revert                     # 卸载（含小代理）
```

四条硬规则：

1. **顺序是承重的**：落文件 → 起小代理 → healthz 通了（12s）→ **才**改 BYOK 地址。
   倒过来 = 服务没起来而 Cursor 已指向死端口 = **把同事的 Cursor 弄坏**。
2. **小代理必须跑 Cursor 自带 Electron**（`ELECTRON_RUN_AS_NODE=1`）。
   plist 里写 `node` 的话，同事机器上没有它 → 死端口。分发包的卖点就是免装 Node。
3. **同事端抓包必须硬关**（`ZKD_CAPTURE_MAX=0`）。开着 = 把别人的真实工作内容
   写到他自己磁盘上。
4. **安装器不是开关**：默认路径的 `mergeConfig()` 会把 composer 覆盖成
   `cursor-g-5.6-sol`。本机基准是 `cursor-web-fc-82-terra`，被覆盖 = **换掉量具**
   且不报错。在基准机上永远走 `zk-delta/switch.sh`，或显式带 `--zk-delta-only`。

打包用 `scripts/zk-cursor-web/package_team_setup.sh`。**`zip -r` 是往已有归档里
追加、不是重建**，改过文件名就必须先 `rm -f` 再打，否则旧名条目还留在包里；
zip 里禁中文名（跨平台乱码）。

**只有 macOS 有小代理常驻服务**（launchd）。Windows/Linux 直接跳过并说明，
那边是公网直连——功能正常，只是不省流量。**不要**写一个没验证过的服务定义假装支持。

三层兜底（都实测过）：集群挂 → 小代理回落直连；小代理挂 → launchd ~1s 拉起；
想彻底退出 → 双击 `TURN-OFF-DELTA-Mac.command`。

## 禁忌

- `zk-delta/tests/fixtures/` 是**真实会话正文**，已进 `.gitignore`，**绝不入库**。
  取证的门要认被验对象**伪造不了**的东西——认 UA 等于没设（UA 在我自己手里），
  所以测试脚本一律带 `x-zkd-synthetic: 1`，出处当场记 `_manifest.jsonl`。
- 这套脚本只碰 zk-delta 自己的 Service/Deployment/ConfigMap。
  **litellm-proxy 禁用 `kubectl apply`**（仓库 manifest 陈旧，会同时回退 image
  和内嵌 CM），只用 `set image` / `patch`。
- 镜像 `imagePullPolicy: Never` 用节点本地镜像；改成 `Always` 会去打公网仓库。

## 活文档

`docs/zk-delta-plan-20260831.md`（S1–S12 全程）、
`docs/zk-delta-regression-20260831.md`（R0–R9 回归）、`zk-delta/README.md`。
飞书：`https://t83dfrspj4.feishu.cn/docx/CINndAwmqoDqLuxiTqGcoYVkn2S` 第 12–13 章。
记忆总入口：`topic_zk_delta_index`（含"尺子本身会坏的五种形状"判据表）。
