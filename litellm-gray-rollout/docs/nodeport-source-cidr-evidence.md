# NodePort 源地址实测 —— `--ingress-cidr` 该填什么（runbook line 148～151）

`prepare-values.py` 会把 NodePort 可达性契约焊进 frozen values：NetworkPolicy 的
`ingressFrom` 必须包含**节点源地址**，否则宿主 nginx 的真实流量会被自己的策略黑洞。
脚本自己的 docstring 就写了这条：

> The host nginx reaches the proxy through the NodePort, so kube-proxy
> presents the node address as the source. A selector-only ingressFrom
> therefore blocks the real traffic path.

问题是 **"节点地址"具体是哪一个地址**。198 上有四个候选：

| 接口 | 地址 |
|---|---|
| `enp4s1` | `10.68.13.198/24` |
| `flannel.1` | `10.42.0.0/32` |
| `cni0` | `10.42.0.1/24` |
| `docker0` | `172.17.0.1/16` |

**猜错任何一个，灰度一上 NetworkPolicy 就是全站 502。** 所以这一项不能推理，只能实测。

---

## 1. 结论（先给答案）

```
--ingress-cidr 10.42.0.0/32     # 跨节点路径：198 的 flannel.1（VXLAN）
--ingress-cidr 10.42.0.1/32     # 同节点路径：198 的 cni0
```

⚠️ **不是 `10.68.13.198/32`。** 实测 pod 侧从来没有看到过这个地址，一次都没有。

⚠️ **也不是"每个节点一条"**（runbook 原文 "one per node" 的措辞会把人带沟里）：
两条都是 **198 一台机器的两个接口**，对应两条**不同的转发路径**，而不是两台不同的机器。
242 / standby 的地址一条都不需要 —— 因为流量从来不从它们进来。

两条都是 `/32`，满足 `prepare-values.py` 的 `MIN_NODE_CIDR_PREFIX = 24`（32 ≥ 24）。

---

## 2. 为什么必须实测 —— 拓扑决定了"节点地址"不等于节点 IP

- 宿主 nginx 在 198 上，prod upstream 是 `server 127.0.0.1:30402`（§4.1 实测）。
- `litellm-product/litellm-proxy-nodeport` 是 `externalTrafficPolicy: Cluster`。
- 4 个 prod proxy pod **一个都不在 198 上**：两个在 `aiyjy-litellm-242`
  （`10.42.2.223/.224`），两个在 `aiyjy-litellm-standby`（`10.42.1.253/.254`）。

⇒ kube-proxy 必须**跨节点转发并 MASQUERADE**，源地址被换成 198 上**朝目的地那条路由的出接口地址**。
目的地是 `10.42.1.x` / `10.42.2.x`（别的节点的 pod 网段）⇒ 出接口是 `flannel.1` ⇒ 源变成 `10.42.0.0`。

而如果灰度 pod 落在 198 本机（deployment 无 `nodeSelector`，198 无 taint，完全可能），
目的地是 `10.42.0.x` ⇒ 出接口变成 `cni0` ⇒ 源变成 `10.42.0.1`。**两条路径两个地址，都得声明。**

---

## 3. 实测（`captured_at=2026-09-13`，198 现网只读）

量具用 conntrack 而不是"在 pod 里 tcpdump"：conntrack 的**回复元组**直接给出
NAT 之后的地址对，一行同时含「原始入口」和「pod 侧看到的源」，不需要两把尺子对时间。

条目形状：

```
src=<入口源> dst=<入口目的> sport=.. dport=30402   src=<pod IP> dst=<pod看到的源> sport=4000 ..
└──────── 原始元组 ────────┘                        └──────── 回复元组 ────────┘
```

### 3.1 阳性对照 —— 跨节点路径（30402，pod 在 242 / standby）

现网 conntrack 里 `dport=30402` 共 **2072** 条，按入口分组、看 pod 侧源地址：

| 入口 | 条数 | pod 侧看到的源 |
|---|---|---|
| `127.0.0.1 -> 127.0.0.1:30402`（宿主 nginx 的真实流量） | 2069 | **`10.42.0.0`** |
| `10.68.13.198 -> 10.68.13.198:30402`（我手工打的对照） | 2 | **`10.42.0.0`** |
| `10.68.13.188 -> 10.68.13.198:30402`（188 的旁路消费者，跨机） | 1 | **`10.42.0.0`** |

**2072/2072 全部收敛到同一个值。** 逐 pod 交叉验证（4 个 prod pod 各自的
`/proc/net/tcp`，解小端十六进制）也都出现 `10.42.0.0`，与 conntrack 互为独立证据。

### 3.2 证伪腿 —— 三个不同入口，其中一个来自另一台物理机

假设是「声明 `10.68.13.198/32` 就够」。如果它成立，
`10.68.13.188 -> 10.68.13.198:30402` 那条**至少**应该保留 `10.68.13.188`，
或呈现 `10.68.13.198`。原文：

```
tcp 6 41 TIME_WAIT src=10.68.13.188 dst=10.68.13.198 sport=50126 dport=30402 \
                   src=10.42.1.254 dst=10.42.0.0 sport=4000 dport=14093 [ASSURED]
```

两者都不是。**假设被证伪**：声明 `10.68.13.198/32` 会把 188 的消费者、宿主 nginx、
以及 198 自己的手工请求**全部**挡在外面。

> 这条腿的价值在于它跨了物理机：只用 loopback 一个入口做实验，
> 无法区分"SNAT 到 flannel"和"loopback 特例"。

### 3.3 同节点路径 —— 换一个 endpoint 在 198 上的 NodePort

30402 的 endpoint 全在别的节点，测不了同节点。改用
`litellm-product/ws-ingress-nodeport`（nodePort **30403**，endpoint `10.42.0.244`，
**就在 `aiyjy-litellm` 即 198 上**）：

| 入口 | 条数 | pod 侧看到的源 |
|---|---|---|
| `127.0.0.1 -> 127.0.0.1:30403` | 37 | **`10.42.0.1`** |
| `10.68.13.198 -> 10.68.13.198:30403` | 1 | **`10.42.0.1`** |

⇒ 同节点确实换成 `cni0`。**这就是"只声明 `10.42.0.0/32`"会踩的雷**：
今天四个 pod 恰好都不在 198，策略看起来是对的；哪天调度器把一个灰度 pod 放到 198，
那个 pod 就单独失联，而且是**部分实例故障**这种最难查的形状。

### 3.4 反向证据

| 检查 | 结果 |
|---|---|
| pod 侧出现过 `10.68.13.198` | **一次都没有**（2072 + 38 条全查） |
| pod 侧出现过 `172.17.0.1`（docker0） | 无 |
| `ss -lntp` 里有 30402 监听 socket | **无** —— kube-proxy 走 netfilter DNAT，不开用户态端口，属正常，不是故障 |

---

## 4. 附带核实

### 4.1 nginx upstream 指向

只取结构行，不 dump 配置（`nginx -T` 禁止直出终端）：

```
server 127.0.0.1:30400      # dev
server 127.0.0.1:30401      # staging
server 127.0.0.1:30402      # prod   ← 本次目标
server 127.0.0.1:30403      # ws-ingress
server 127.0.0.1:30404      # zk-delta
```

与 §3.1 里 2069 条 `127.0.0.1 -> 127.0.0.1:30402` 完全对上 —— 入口路径确认。

### 4.2 当前落点（会变，执行当天必须重取）

| 节点 | taint | prod proxy pod |
|---|---|---|
| `aiyjy-litellm` (198) | 无 | 无 |
| `aiyjy-litellm-242` | `dedicated` | `10.42.2.223` `10.42.2.224` |
| `aiyjy-litellm-standby` | `dedicated` | `10.42.1.253` `10.42.1.254` |

**落点是会漂的，但结论不随它漂** —— 因为两条路径的地址（`10.42.0.0` / `10.42.0.1`）
都已声明，无论 pod 落在 198 还是别的节点都被覆盖。这正是要同时声明两条的理由。

---

## 5. 执行当天怎么复核（不要照抄本文件的数字就完事）

1. 重取 198 的接口地址：`ip -4 -o addr show dev flannel.1; ip -4 -o addr show dev cni0`。
   flannel 的 `/32` 在集群重建后会变。
2. 应用 NetworkPolicy **之后**、接真实流量**之前**，按 runbook line 149~151 做三条实测：
   - **阳性腿**：从 198 `curl 127.0.0.1:30402/health/liveliness` ⇒ 期望 200。
   - **证伪腿**：从一个**未声明**的源打同一个端点 ⇒ 期望超时/拒绝。
     ⚠️ 别拿 188 当"未声明源"—— 它经 NodePort 进来后源地址同样被换成 `10.42.0.0`，
     属于**已声明**，会读出假绿。真正的未声明源要在 pod 网络里找（例如临时起一个
     普通 pod 直连 `10.42.x.y:4000`）。
   - **line 151**：把 pod 侧实际看到的源地址逐条抄回执行单，与声明列表对齐。
3. 三条都过，才允许把 frozen values 里的 `ingressFrom` 当成已验证。

---

## 6. 顺带修正：frozen values **不能**提前生成

`prepare-values.py` 有 `MAX_SCHEDULER_EVIDENCE_AGE = timedelta(minutes=15)`：

```python
age = datetime.now(timezone.utc) - captured
if age < -timedelta(minutes=5) or age > MAX_SCHEDULER_EVIDENCE_AGE:
    fail("scheduler evidence is stale")
```

evidence 把 `config_sha256` / `callbacks_sha256` / `runtime_sha256` 绑定到某个时刻，
**15 分钟就过期**。这是设计如此：防止一份陈旧的 values 把已经漂移的 schema 偷渡进窗口。

⇒ **frozen values 是窗口内产物，不是窗口前可以准备好的交付物。**
提前能定死的只有本文件这两个 `--ingress-cidr` 值（它们不依赖 evidence 时效）。
把 values 列进"提前准备清单"是错的，会让执行人在窗口里发现手上的文件已作废。

---

## 6. 执行当天复核（2026-09-14 01:28，§5 第 1 步）

```
flannel.1    inet 10.42.0.0/32  scope global flannel.1
cni0         inet 10.42.0.1/24  brd 10.42.0.255 scope global cni0
```

**与 §1 记录逐字相同** ⇒ 两条 `--ingress-cidr`（`10.42.0.0/32` + `10.42.0.1/32`）原样沿用，
不需要改数。

落点（§4.2 说它会漂，所以重取；**结论不随它漂**）：

| 节点 | prod proxy pod |
|---|---|
| `aiyjy-litellm` (198) | 无 |
| `aiyjy-litellm-242` | `10.42.2.223` `10.42.2.224` |
| `aiyjy-litellm-standby` | `10.42.1.253` `10.42.1.254` |

⚠️ §5 第 2 步的**三条实测腿（阳性 / 证伪 / 同节点）还没做** —— 它们必须在
NetworkPolicy 应用之后、接真实流量之前跑，本节只完成了第 1 步的地址复核。
