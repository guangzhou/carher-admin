---
name: cli-anything-mihomo
description: >-
  用 cli-anything-mihomo 在 198 上跑 mihomo(ShellCrash 的内核)当代理出口:拉 iGuge Clash 订阅(可多账户)、
  起只听 127.0.0.1 的实例、换出口(切节点/端口钉死单节点/自动选)、实测每个节点真实出口国家、
  测各出口连 Kiro / 谷歌学术的延迟、生成「加 kiro 30891 出口槽」handoff。
  Use when 用户说"198 换代理出口/切节点/哪个节点快/加个美国(某国)出口/订阅里有哪些国家/学术代理挂了/
  更新订阅/新的 iGuge 账户/iGuge 额度还剩多少/流量用了多少/什么时候到期/ShellCrash 在 198 怎么用/
  给 kiro 加 IP 槽写 handoff/测下网速延迟"。
---

# cli-anything-mihomo

真身 `tools/cli-anything-mihomo/`（v1.2；源码分析 `MIHOMO.md`；测试 `cli_anything/mihomo/tests/TEST.md`，60 条含真 198 E2E）。

## 一键入口（先用这个）

```bash
scripts/mihomo-198.sh install                 # 装/更新 CLI
scripts/mihomo-198.sh quota                   # 所有 iGuge 账户 已用/剩余/重置日/状态/到期（≥80% 或 <30 天 ⇒ exit 1）；详见 skill `iguge-quota`
scripts/mihomo-198.sh status                  # 198 所有实例 + 每端口 出口IP/国家/连 Kiro 耗时
scripts/mihomo-198.sh verify                  # 每端口真搜一次谷歌学术
scripts/mihomo-198.sh countries acct2         # 订阅里真实有哪些国家（几分钟）
scripts/mihomo-198.sh handoff acct2 18891-18895 [--no-country] [--out f.md]   # kiro 30891 加槽 handoff
scripts/mihomo-198.sh -a acct2 exit ports     # 其它一律透传给 cli-anything-mihomo
```
每步打印 `OK/STOP <步骤>`。188 SOCKS 隧道（GitHub 和订阅都靠它；**198 直连订阅 = 403**）由工具自动检查/拉起（`cli-anything-mihomo tunnel`）。

## 现网（2026-10-28，以 `instance list` / `exit table` 现量为准）

| 实例(=账户) | 端口 → 出口 IP | 用途 |
|---|---|---|
| `default`（老 iGuge 账户） | 17890 规则分流（现新加坡 64.118.136.15）· **17891 美国自动组**（现 170.178.173.10）· **17892 英国钉死** 213.255.246.28 | 学术代理 + 美/英出口槽候选 |
| `acct2`（新 iGuge 账户） | 18891 203.231.254.184 · 18892 49.157.61.223 · 18893 109.176.19.134 · 18894 103.47.211.19 · 18895 64.118.159.27（各钉死单节点） | kiro 出口槽候选 |

上表都是 198 上 cltx 的普通进程，**只听 127.0.0.1、198 重启不自启、242 上的 kiro-rs-slots 连不到**。
给 kiro 槽用的出口**已另做成集群内代理**（10-28 起，11-01 现量）：`kiro-us-egress` / `kiro-uk-egress` / `kiro-px-<n>`
（n = 1~5、16~39，n = acct2 端口 − 18890）各一份 mihomo Deployment+ClusterIP+NetworkPolicy+密码，钉死单节点，
槽里填 `http://<deploy>.kiro-rs.svc:7890`、用户 `kiro`。这些 pod 的节点配置是**从 `~/mihomo-harness/acct2/config.yaml` 抽出来的快照**，
198 上的实例停了/换节点不影响它们；反过来 198 上 `exit select` / `config pin` 也改不到它们。
生成/验收/加槽脚本和现网槽表见 skill `kiro-rs-slots`（`k8s/kiro-rs/slots/kiro-px-egress.sh` / `kiro-px-verify.sh` / `kiro-egress-ip-watch.sh`）。

## 为什么不装 ShellCrash

ShellCrash = 一堆 shell 菜单 + mihomo + 改防火墙。198 是 k3s master，它的「本机/局域网/旁路」模式会改
iptables、把 `ip_forward=1` 写进 sysctl、往 resolv.conf 塞 127.0.0.1，litellm/kiro 全受影响。
唯一无害的「纯净模式」= 只跑 mihomo 开端口，本工具就是这个。出处：`cli-anything-mihomo shellcrash facts`。

## 常用

```bash
M=cli-anything-mihomo                    # 不带 --account = default；--account acct2 = 第二个账户
$M instance list                         # 198 上所有实例(全部账户)：属主、在跑、端口；有端口出了 127.0.0.1 ⇒ exit 2
$M -a acct2 exit table                   # 每端口：出口 IP + 国家(ipinfo) + 连 Kiro 中位耗时 ← 写 handoff 就用它
$M exit verify                           # 每端口：出口 IP + 真搜一次学术、结果条数>0 才算 OK
$M exit select "♻️ 自动选择 - JP"        # 切默认端口的出口，立即生效，reload/重启不丢(cache.db)
$M config pin 17893 "KR - 韩国 02" && $M instance reload   # 新开一个端口钉死一个节点
$M exit measure --all                    # mihomo 自测各节点延迟(默认测 scholar)
$M -a acct2 exit geo --all               # 订阅里「真实有哪些国家」：临时实例每节点一个端口、串行问 ipinfo、测完自动删
$M sub quota --all-accounts               # 查额度：重新拉订阅读响应头；--cached 读上次的；--warn-pct/--warn-days 改阈值
```

所有命令带 `--json`；`--name X` 同账户多实例；不带子命令进 REPL。

### 新增一个 iGuge 账户

```bash
printf '%s\n' '<订阅URL>' > /tmp/u && $M -a acct3 sub set-url --from-file /tmp/u && rm /tmp/u   # 别让 URL 进对话/日志
$M -a acct3 sub fetch
$M -a acct3 config set --mixed-port 20890 --ctl-port 20091 --version v1.19.33   # 端口段别和现有实例撞
$M -a acct3 exit geo --all               # 先看真实国家再决定钉哪些
```

### 给 kiro 30891 加出口槽写 handoff（脚本，数字全现量）

```bash
python3 scripts/kiro-slots-egress-handoff.py --account acct2 --ports 18891-18895 --out .work/handoff/x.md
python3 scripts/kiro-slots-egress-handoff.py --account default --ports 17891,17892 --no-country   # 不写国家
```
每端口打印 OK/STOP；不通的端口在表里标「不要加」；两个端口同一出口 IP ⇒ STOP（一槽一 IP）。
`--no-country` 只去掉国家/城市/运营商/节点名，**出口 IP 一查就知道国家**，文档里会写明。

## 选出口的规则（这次 session 实测出来的）

- **国家只认出口 IP**：节点名会骗人（`DE - 德国2线` 实际出口法国）。问「有哪些国家」一律 `exit geo --all`，别看 `sub nodes`。
- **选槽前查重**：新槽 IP 不能和现有槽 / 198 上已开端口重复（`instance list` + `exit table` 两个账户都看）。
  两个 iGuge 账户节点名、顺序完全相同，出口 IP 大部分也相同 ⇒ **新账户 ≈ 多 200GB 流量，不是多国家**。
- **测延迟要串行**：同一节点并发打会把彼此拖慢；`exit table` / `geo` 都是一个一个测。同一节点两次实测差 0.5s 很常见，handoff 用生成时的现量。
- **钉死单节点 ≠ 固定 IP**：有的节点背后是 IP 池。10-29 两账户全量每分钟测 30 分钟，**会变的 7 个**：
  `JP - 日本 - 01`、`JP - 日本原生 - 05`、`US - 洛杉矶 - 02`、`US - 圣何塞 - 04`、`US - 洛杉矶 - 09`、`US - 洛杉矶 - 13`、`HK - 香港 - 12`（老账户）。
  完整分类（稳定/会变/时通时断/不通）在 `.work/shellcrash/ipdrift-summary.txt`。做 kiro 槽只用「稳定」的；新节点先 `exit table` 隔几分钟测几次。
- **一个账户能开多少口**：10-29 在 198 一个实例里同时开 17 个节点、10 分钟持续打，0 条鉴权/踢线/限流，原有端口不受影响。
  官方写「4~5 台设备」（TOS / 定价页），实测不按同一机器上的节点数算；多台机器共用一个订阅没测过，别这么用。
- **钉死单节点 vs 自动组**：钉死 = IP 固定（符合「一槽一 IP」）但节点挂了槽就挂；自动组 = 自愈但 IP 会变。kiro 槽默认钉死。
- 2026-10-28 两账户实测有 12 国：US(18 个不同 IP) HK(11) JP(8) SG(7) TW(3) DE/FR/GB(各 2) KR/MY/PH/TR(各 1)。
  **没有印度/越南/加拿大/澳大利亚**；`IN - 印度孟买` 入口在广州、握手被 reset，两账户都不通（iGuge 侧问题，不是 198 出口）。
  经代理连 Kiro 中位：日本 0.77s、美国 0.8s、新加坡 TUIC 0.96s、韩国 1.0s、菲律宾 1.3s、新加坡其它 1.5~2.2s、英国 1.8~2.2s、德国 2.2~2.9s、法国 2.7s；198 直连 0.61s。

## 坑（都实测/读源码确认过）

- **在 url-test 组上 `exit select` = 钉死**，直到那个节点测不通才自动换；要放回自动用 `exit unfix <组>`。
- `GET /group/<组>/delay`（面板「测速整组」）会**顺手取消钉死**；本工具不调它。
- 订阅自带 url-test 间隔 3600s；本工具默认改成 300s（`config set --url-test-interval`）。
- mihomo secret 为空 = 控制接口**完全不鉴权**；本工具在 198 生成 secret、启动时 env 注入，不进配置文件、不回本机。
- **198 上实例目录全账户共用**（`~/mihomo-harness/<name>/`）：每个目录有 `account` 属主标记，别的账户 start/stop/reload/destroy/读控制口都会被拒。
  🔴 v1.1 第一版 `stop` 漏了这道检查，`--account acct2 --name default instance stop` 真把 default 停了（10-28，立即拉起、补测试）。新加写操作必须先 `check_owner`。
- 订阅链接 = 凭据，只在 `~/.config/cli-anything-mihomo[-<账户>]/sub.url`（0600）；`config render` 不带 `--audit-only` 会打印节点密码，只在本机看。
  给别人部署时别直接给 render 全文（含 62 个节点名和密码），只抽要用的那几条。
- **查 iGuge 额度只有一个来源**：订阅响应头 `subscription-userinfo`（`sub info` 读的就是它，计下行）。插件里只有 VIP 有效期，没有流量；
  mihomo `/connections` 的流量是进程启动后累计、重启清零，别当额度。
- 🔴 起 188 隧道别加 `-o ClearAllForwardings=yes`：它把 `-D 18880` 也清了，ssh 不报错但端口不监听（10-28 因此 fetch 全报 rc=7）。工具 `tunnel` 已按这个写。
- 188 隧道偶发 `curl (97) connection to proxy closed`：fetch 自动重试 3 次；失败不会覆盖上次的订阅/额度（10-29 前会把 sub.headers 截成空文件 ⇒ 下一次 quota 报「no subscription-userinfo header」）。
- 本机到 198 偶发「Network is unreachable / Operation timed out」（utun4 VPN 抖），重跑即可，不是工具问题。
- `ipinfo.io` 只出 IP（`/ip`）时看不到国家，但 IP 本身可查；Cloudflare 测速 `__down?bytes=100000000` 会 403（单次上限），测带宽用 4×25MB 并发。
