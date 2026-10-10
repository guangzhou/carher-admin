---
name: univpn-mac-loopback-triage
description: 诊断 macOS 上 UniVPN（华为/Leagsoft 客户端，进程三件套 UniVPN + UniVPNPromoteService + UniVPNCS）报「接收返回码超时」。2026-10-10 实证真因=UniVPNCS 在 ROUTE_Ensure_ExitRoute 段错误时把系统回环路由 127/8 -> lo0 一起删掉且没恢复，导致 UI 连不上本机 RPC 127.0.0.1:29191。含一个不写盘的八段量具脚本、一个 root 修复脚本，以及四条把本轮踩过的错误归因固化成判据的红线。Use when 用户说 UniVPN 连不上/接收返回码超时/之前好好的突然连不上，or 提到 UniVPNCS 崩溃、29191 连不上、Can't assign requested address、utun 没有 IPv4。
---

# UniVPN (macOS)「接收返回码超时」诊断

> 实证：**2026-10-10 定因并修复**。症状=界面点连接后报「接收返回码超时」，
> 重开客户端、重装、换线路全无效。
> 根因=`UniVPNCS` 于 12:02:04 在 `ROUTE_Ensure_ExitRoute` 段错误（SIGSEGV），
> 崩溃时把 **系统回环路由 `127/8 -> lo0` 一起删掉且没恢复** ⇒ 全机本机连接失效
> ⇒ UI 连不上 `127.0.0.1:29191` ⇒ 等不到回码 ⇒ 报超时。
> 修复=`sudo ifconfig lo0 down && sudo ifconfig lo0 up` 后完全重启客户端。
> 验收=`utun4 -> 10.68.90.92` + UI 日志 `[Cnem succed!]`。

## 零号判据：先看回环，不要先看网络

**「接收返回码超时」这句话里的"返回码"是本机 IPC 的返回码，不是网关的。**
它描述的是 UI 等不到后台服务回话，和 VPN 网关通不通**没有任何关系**。

```bash
.claude/skills/univpn-mac-loopback-triage/scripts/univpn-triage.sh
```

只读不写，可反复跑。退出码：`0`=正常 / `1`=回环损坏（根因）/ `2`=回环正常但 CS 没起来 /
`3`=回环与进程都正常但网关不可达。

回环损坏时的修复（需 root，可逆，重启也会自动重建）：

```bash
sudo .claude/skills/univpn-mac-loopback-triage/scripts/fix-loopback.sh
```

## 架构：三个进程，坏一个就全盘超时

| 进程 | 身份 | 怎么起来的 |
|------|------|-----------|
| `UniVPN` | Qt 界面 | 用户双击 |
| `UniVPNPromoteService` | 提权 RPC 服务，**监听 127.0.0.1:29191** | UI 启动时 `UI_Start_PromoteService` 拉起 |
| `UniVPNCS` | **真正建隧道的**（setuid root） | 由 UI 经 29191 的 RPC 请求拉起 |

UI 和 CS 之间的唯一通道就是 `127.0.0.1:29191`。
**回环一坏，这条链在第一步就断，后面全是假象。**

只有 UI + Promote 活着、`UniVPNCS` 不在 = 典型现场。

## 真因链条（2026-10-10 完整时间线）

```
12:01:56  换网络（旧网关 192.168.0.1 → 新网关 10.20.45.217）
          旧隧道失效，CS 开始重连
12:01:57  [netc connect][connect 2e0d2399 :4433 succeed]   TCP 连上
12:01:58  ↓ 7 秒内反复删两条默认路由 8 次（10.68.90.104 / 192.168.0.1）
12:02:02  [SSL Connect failed][reason:ssl time out]        握手超时（拿的是旧隧道参数）
12:02:03  [Auth logout failed] → [Restore Route][begin]    进入清理路径
12:02:04.728  [ROUTE][Ensure exit route][begin]            最后一行日志
12:02:04.756  SIGSEGV @ ROUTE_Ensure_ExitRoute             崩溃（毫秒级对齐）
          ⇒ 回环路由 127/8 被删掉，没恢复
12:23~12:57  每次重开：[ui can't connect RPC!] → 界面报「接收返回码超时」
```

崩溃栈（`~/Library/Logs/DiagnosticReports/UniVPNCS-*.ips`）：

```
ROUTE_Ensure_ExitRoute ← ROUTE_Add_Route_Full ← ROUTE_Set_Route_Model
                       ← ROUTE_Handle_RtMsg ← ROUTE_Monitor
```

**加重因素**：机器上有三条默认路由（`en0` + Docker 建的 `bridge0`/`bridge100`，
后两条带 `!` 失效标记）。`Ensure exit route` 要在多条默认路由里挑出口，这是它踩野指针的
输入条件。诊断期间建议先退掉 Docker Desktop 以减少复发面。

## 四条红线（全部是本轮我自己踩错后固化的）

### 🔴 1. `Can't assign requested address` ≠ 防火墙/被拦，= 回环路由坏

```
nc: connectx to 127.0.0.1 port 29191 failed: Can't assign requested address
```

这不是 `Connection refused`（那才是"没人监听"）。它是**系统分配不出本地端口**。
本轮我先去查火绒（HipsMain + `cn.huorong.HipsMain.hractmond` 系统扩展）和 macOS 防火墙，
全是空的（`Firewall is disabled`）——纯浪费时间。

**判据**：起一个任意端口做阳性对照自连。对照组也失败 ⇒ 不是 UniVPN 的问题，
是整机回环坏了。量具脚本 ② 段就是干这个的。

### 🔴 2. `flags` 带 `WASCLONED` 的路由删不掉，别去删它

```
route to: 127.0.0.1    gateway: 10.20.45.217    interface: en0
flags: <UP,GATEWAY,HOST,DONE,WASCLONED,IFSCOPE,IFREF,GLOBAL>
```

看到 `127.0.0.1` 指向外部网关，我让用户 `sudo route delete -host 127.0.0.1 10.20.45.217`，
回报 **`not in table`**。因为 `WASCLONED` 的含义是"这条是从 `default` 克隆出来的"，
它不是真实写入的表项。

**真正的缺失是 `127/8 -> lo0` 那条不见了**，`127.0.0.1` 无路可走才去匹配 `default`。
**正确动作是"加回"而不是"删掉"**，且 `ifconfig lo0 down/up` 比手工 `route add` 更干净
（系统会一次性恢复 `127/8`、`127.0.0.1/32`、`::1` 全套）。

验证缺失只认一句：`netstat -rn -f inet | grep '^127'` 返回空。

### 🔴 3. 日志里的网关"IP"是 32 位整数原样打印，不是读反了

```
[netc connect][connect 2e0d2399 :4433 succeed]
```

我把 `2e0d2399` 按大端读成 `46.13.35.153`，实测不可达，于是下了
**"客户端在 Rosetta 下把 IP 字节序读反了"** 这个结论——**完全错误**。
小端读就是 `153.35.13.46`，即配置里 `国际.ini` 的正确网关。

**判据**：拿日志里的十六进制去跟 `~/.UniVPN/config/*.ini` 的 `GatewayAddress` 对，
两种字节序都试；对上了就是正常的，不要编"客户端有字节序 bug"的故事。
同理 `:4433` 是隧道内部通道端口，不是配置里的 `44330`，不是配错。

### 🔴 4. `ping` / TCP 通不代表 SSL 能握手；反之 SSL 能握手就排除网关

本轮网关一直是好的：`openssl s_client -connect 153.35.13.46:44330` **秒拿到完整证书链**
（华为网关 `CN=LOCAL-1021A0001545`，有效期到 2031）。
日志里的 `ssl time out` 是客户端拿**旧隧道参数**去握手造成的，不是网关拒绝。

**判据**：报 SSL 超时时，先用 `openssl s_client` 独立握一次。
握得上 ⇒ 问题在客户端状态，别再查网络。

## 日志在哪（比 unified log 有用得多）

`log show --predicate 'process == "UniVPNCS"'` **什么都拿不到**（它不走 unified log）。
真正的日志全在 `~/.UniVPN/log/`：

| 文件 | 看什么 |
|------|--------|
| `UniVPN_UniVPNUI_<n>.log` | `[ui can't connect RPC!]` / `[UI and RPC connection failed!]` ⇒ 本机通道断。成功则是 `[Cnem succed!]` + `[GetLogined]` |
| `UniVPN_UniVPNCS_<n>.log` | 崩溃前最后一行就是崩溃点；`SSL Connect failed` / `Ensure exit route` / `Delete default route` |
| `UniVPN_UniVPNPromoteService_0.log` | `[RPC Listen][port 29191]` 每次启动都有；**有没有 `RPC listen port have new connection`** 是判断 UI 连没连上的铁证 |

编号滚动到 `_219` 这种，**取 `ls -t` 最新那个**，别按编号猜。

## 其他实证事实

- **那个带 `KeepAlive` 的特权服务从来没装上**：`/Library/PrivilegedHelperTools/com.leagsoft.UniVPNCS`
  和 `/Library/LaunchDaemons/com.leagsoft.UniVPNPromoteService.plist` 都不存在。
  所以 **CS 崩了没有任何东西会拉起它**，只能完全退出客户端再开。
  `promote/install_PromoteService.sh` 能装，但它还会去 load 早已废弃的
  `net.sf.tuntaposx.tun` kext（机器上没有 `/dev/tun*`，CS 实际用系统原生
  `com.apple.net.utun_control`，不需要那个驱动）⇒ **收益有限、引入加载不了的 kext，不建议动**。
- **裸跑 `./UniVPNCS` 必然 exit 1 且零输出**，带 `-d` 也一样。它要由 UI 经 RPC 喂
  `LoginUser=... InstallPath=... RemotePort=29191` 才能起。**别用"手动跑不起来"当判据。**
- **`utun0-3` 是 macOS 自带的**（iCloud Private Relay 等），光看 utun 存在不算隧道通。
  **只认带 IPv4 的 utun**：连上后是 `utun4 -> 10.68.90.92`。
- 客户端是 **2022-03-29 构建的 x86_64 二进制，完全无代码签名**
  （`spctl` 判 `rejected: no usable signature`），在 M5 + macOS 26.2 上靠 Rosetta 跑。
  `ROUTE_*` 这类崩溃**会复发**，根治只能等厂商出 arm64 原生版。
- 两条线路在 `~/.UniVPN/config/`：`cltx.ini`（`58.241.5.230:44330`，`TunnelMode=0`）、
  `国际.ini`（`153.35.13.46:44330`，`TunnelMode=2`）。崩的是后者。
  当前选哪条看 `~/.UniVPN/sysconfig.ini` 的 `ClientLastAccessSession`。

## 复发时的最短路径

1. 跑 `univpn-triage.sh`，看退出码。
2. `1` ⇒ `sudo fix-loopback.sh`，然后 `pkill -f UniVPN && sleep 2 && open -a UniVPN`。
3. `2` ⇒ 只需 `pkill -f UniVPN`（三个进程都要死）再打开。
4. `3` ⇒ 换另一条线路。
5. 都不是 ⇒ 读 `~/.UniVPN/log/` 里 `ls -t` 最新的 CS 日志尾部，看它这次停在哪。
