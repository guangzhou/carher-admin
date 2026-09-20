---
name: astrill-243-exit-ops
description: >-
  243（10.68.13.243）上 LXD + Astrill 多出口的全套运维：巡检还剩几个并发坑位、
  切换某条出口走哪台服务器（~26s）、判"某台服务器到底连不连得上"（唯一可信量具=抓包数入向包）、
  给出口 IP 打 ipok 风险分。
  Use when 用户说 "切一下出口/换个IP"、"现在几条出口在线"、"xx 服务器连不上"、
  "出口 IP 干不干净/多少分"、"能不能快速切 IP"、"住宅 IP 能用吗"、
  或者要在 243 上动 Astrill 容器。
---

# 243 Astrill 多出口运维

## §0 拓扑速查

| 项 | 值 |
|---|---|
| 宿主 | `10.68.13.243`，用户 `cltx`，**独立单节点 k3s**，不属于 198 集群 |
| LXD | `lxdbr0` 10.148.100.1/24 |
| 容器 | `test-lxd-2` / `astrill-2` / `astrill-3`（**在服务，勿动**）· `astrill-4` / `astrill-5`（闲置，实验用） |
| 代理 | LXD proxy device `px1`，宿主 **8118/8119/8120** → 容器 `127.0.0.1:8888` tinyproxy（BasicAuth） |
| pod 直连 | k3s pod 可直连容器 IP `10.148.100.136:8888`，不必经宿主端口（实测过） |
| 账号上限 | **5 条并发**（官方 FAQ），现用 3 条 |
| 协议 | StealthVPN（默认）。⚠️ 有效期 **2026-09-24** |

## §1 三条在服务的出口

| # | 容器 | 宿主端口 | 服务器 | 出口 IP | ipok |
|---|---|---|---|---|---|
| 1 | test-lxd-2 | 8118 | San Jose Supercharged (Private) | `104.168.13.250` | 未测 |
| 2 | astrill-2 | 8119 | Seattle Supercharged 1 (Private) | `38.246.151.75` | 未测 |
| 3 | astrill-3 | 8120 | Los Angeles Supercharged 1 (Private) | `104.129.16.172` | **35 / hosting** |

**这三个是机房段，不是住宅段。** 35 是 ipok 对 hosting 的 floor，再干净也降不下去。

## §2 脚本

全部在 `scripts/`，密码走环境变量 `ASTRILL243_PW`（**禁止命令行传参**，会进 ps/history）。

```sh
export ASTRILL243_PW='...'          # 243 的密码，末位是一个单引号
scripts/astrill-243-survey.sh                                   # 巡检五个容器 + 剩几个坑位
scripts/astrill-243-switch.sh astrill-4 "Seattle Supercharged 1 (Private)"   # 切出口 ~26s
scripts/astrill-243-probe.sh  astrill-5 "USA - St Louis" 142.214.202.4       # 判某台连不连得上
scripts/ipok-score.py -f ips.txt --proxy-file proxies.txt --json out.json    # 批量打分
```

`astrill-243-lx.sh` 是底层包装器，其余三个都调它，一般不直接用。

⚠️ **只读性**：survey/probe 纯只读；switch 会改所在容器的连接状态（这是它的目的），
但不写配置、不删文件。**三个脚本都只该对闲置容器用**，对 `test-lxd-2/astrill-2/astrill-3`
跑 switch 会打断线上出口。

## §3 🔑 GUI 自动化的坑（每条都是踩出来的）

**坐标是常量**：主窗口 (400,98) 250x315 · ON/OFF (525,192) · 服务器下拉箭头 (616,264) ·
搜索框 (515,264) · 第一行结果 y=291 · **All 标签页 (615,495)** ·
协议下拉 (570,114) → OpenWeb y=136 / OpenVPN y=157 / StealthVPN y=178 / WireGuard y=199。

1. **单次切换必须先 `pkill` 清残留，否则大概率不生效。**
   现象极具欺骗性：搜 Seattle，结果仍连着上一台 Los Angeles，且开关停在 OFF，**不报任何错**。
   批量连切之所以"看起来好用"，是因为前一轮的收尾恰好起了这个作用。
   ⇒ `xdotool 点OFF → sleep 4 → pkill -x asovpnc/asovpnc.real/asproxy → sleep 2` 再开始选。
   加上这一步后实测 3/3 成功、26s/次；不加则单次调用几乎必失败。

2. **必须点 All 标签页 (615,495)。** 下拉框默认落在 Recommended 页，
   搜公共服务器名是 `Nothing found`，而且**静默**连回原来那台。

3. **ON 态下换服务器不会重连。** 不先 OFF 就是在测旧服务器。

4. **搜索框 `ctrl+a` 不清空**（是追加），要连按 45 次 BackSpace。

5. **`pkill -f <pattern>` 会杀掉自己那个 shell**（匹配到自身 `sh -c`）⇒ 一律 `pkill -x`。
   症状是命令输出完全为空。

6. **连上了 ≠ 连对了。** GUI 选行有时序竞争，实测出现过"搜 Seattle 却选中 Los Angeles"。
   switch 脚本内置了按已知 IP 反查落点的核对，对不上直接 exit 1。

7. **终判只认截屏**：`import -window root /tmp/xxx-lgx.png` + base64 拉回本地。

## §4 🔴 量具纪律：在 243 上什么能信、什么不能信

| 量具 | 能不能信 | 原因 |
|---|---|---|
| **tcpdump 入向包数** | ✅ **唯一可信** | 服务器回了就是回了，不受下面任何一条影响 |
| `tun0` 存在 | ✅ 可信 | 建成隧道的充分标志 |
| `ps` 的 `--port` | ❌ | 是参数回显**不是真实行为**。显示 `--port 1`，实际打的是 35700/14890 这类随机口 |
| 容器内 `curl` 出口 IP | ❌ | curl 不走隧道，永远读成宿主的 `172.235.204.67`，把"连上了"误判成"没通" |
| `/dev/tcp`、`curl telnet://` | ❌❌ | **243 宿主网络劫持所有出站 TCP**，任何 IP:PORT 都报 open。阴性对照 `1.2.3.4:12345` 也 open |
| `ping` | ❌ | **无判别力**：能连的三台 Private 也是 100% 丢包 |

⇒ 判"某台服务器活不活"**只用 `astrill-243-probe.sh`**（数入向包），别的都会骗人。

## §5 🔴 200 台公共服务器对本账号集体沉默（成因未知）

**现象**（同容器、同时刻、只换服务器，tcpdump 实测）：

| | Private（能连） | 公共 St Louis |
|---|---|---|
| 目的端口 | `48215`（目录写死） | 随机 `35700` / `14890` |
| 出向包 | 38 | 5 |
| **入向包** | **33** | **0** |

**已证伪的假设，别再重复走**：
- ❌ **并发坑位被占满** —— 上限 5 实占 3；空闲的 astrill-4 能正常建第 4 条
- ❌ **容器身份互踢** —— 五个容器 `Astrill.ini` sha 各不相同，证书独立
- ❌ **端口挑错** —— 目录里 5 个候选口（8304/8301/443/53/8637）逐个硬填，
  tcpdump 确认每次都真打到目标口上，**入向全 0**
- ❌ **~~GUI 把 `1-65535` atoi 成端口 1~~** —— 我自己提出又自己推翻的。
  抓包证明客户端把它当**区间**并随机挑口，行为正常。当时只看 `ps` 没抓包，量具没立住

**形状吻合但无证据、不许当结论**：目录里恰好 3 台 `(Private)`，账号能连的恰好就是这 3 台。
「套餐只含这 3 台」这话**没有任何来自 Astrill 侧的数据支持**，不许说。

**未做完的腿**：OpenWeb 协议下公共服务器会不会回包。
进程能起（`openweb -p <port> --proxy-port 3213`）但抓包出入向全空 —— 它是**按需连接**的代理，
没流量经过 `127.0.0.1:3213` 就不去连服务器。**要重做必须先灌流量再抓包**，现有结果不算数。

## §6 住宅 IP 现状

161 个出口 IP 里打完 45 个（其余卡在 ipok 限流）。低于 35 分的只有 8 个，**全是住宅段，且全部连不上**：

```
 3  142.214.202.4    USA - St Louis          10  103.157.217.150  Vietnam
22  103.6.219.3      Australia 1             22  103.82.100.180   France - Strasbourg
22  147.189.162.24   Hungary                 22  149.6.162.85     France - Paris 2
22  149.7.16.170     UK - London GT1         22  158.255.76.196   Nigeria - Lagos
```

⇒ **分数低的连不上，连得上的分数锁死 35。** 这是当前的死结。

⚠️ 用户说"我买了家庭 IP"。**Astrill 的 "Private IP" = 独享 IP，不等于住宅 IP**（独享但仍是机房段）。
用户最早给的链接正是 `ipok.io/?ip=104.168.13.250` —— 就是现在在用的 San Jose 那条。
**用户到底买了什么，只有 Astrill 能回答**，逆向回答不了，别替他猜。

## §7 ipok.io 打分

- API `https://ipok.io/api/ip?ip=<IP>`，`final = max(weightedAvg, floors)`，**hosting floor = 35**
  ⇒ 机房 IP 不可能低于 35，想要低分只能 residential/mobile
- **限流按来源 IP 算，约 11 次就封**，窗口是分钟级
- `scripts/ipok-score.py` 已内置 429 长退避 + `--proxy-file` 多出口轮转
- ⚠️ 但**出口只有 3 条 ⇒ 配额摊不开**，整批 161 个跑一夜只成 45 个。
  这正是 §5 的后果：连不上更多服务器 ⇒ 没有更多出口 ⇒ 打分打不完。**三件事是同一个问题。**

## §8 其它运维备注

- 243 上 `lxc exec` **抢 stdin** ⇒ `echo pw | sudo -S` 会永久挂住。
  正解 = `SUDO_ASKPASS` 临时脚本 + `sudo -A` + `lxc exec ... < /dev/null`（`astrill-243-lx.sh` 已封装）
- **243 密码末位是一个单引号** ⇒ 任何把它塞进单引号字符串的写法都会 `Unterminated quoted string`。
  helper 里走 base64 传递
- ⚠️ **`set -e` 会把这些脚本搞成静默失败**：轮询 tun0 时 grep 无匹配返回非 0，
  那是正常中间态。survey/switch 都**故意不用 `set -e`**
- 克隆容器必须 `rm -f Astrill.ini client.crt client.key ca.crt` 再重新登录，否则身份相同互踢
- ⚠️ **GUI 活不过重启**（`astrill-gui.service` 是 oneshot，子进程秒死）。**服务本身没修**。
  手动救活：`setsid runuser -u astrill -- env HOME=/home/astrill DISPLAY=:99 /usr/local/Astrill/astrill --no-xshm &`
- ⚠️ **astrill-5 里挂着调试用 shim**：`/usr/local/Astrill/asovpnc` 是个 shell 脚本，
  读 `/tmp/asport`、`/tmp/asproto` 覆盖端口/协议。
  **原始二进制 = `asovpnc.real`，回滚 = `mv /usr/local/Astrill/asovpnc.real /usr/local/Astrill/asovpnc`。**
  只动了 astrill-5 这一个闲置容器
- 宿主 `/tmp` 写固定名会撞 root 旧文件**静默失败**（读出昨天的内容）⇒ 文件名一律带 `_lgx`/`-lgx` 后缀

## 关联

[[project_243_astrill_lxd_multi_exit_2026_09_12]] ·
[[feedback_codex_desktop_gui_probe_judges_that_lie]]（GUI 只认截屏，同族）·
[[feedback_first_plausible_mechanism_is_not_a_conclusion]] ·
[[feedback_curl_o_to_shared_tmp_path_reads_stale_root_file]] ·
[[topic_ruler_failure_shapes]]
