---
name: chatgpt-acct-console-dev
description: >
  198 号池管理台（`10.68.13.198:8796`，目录 `/opt/chatgpt-acct-console/`）**改代码和重启**的 SOP：
  加接口/加按钮/加探针要动哪几处、部署前怎么自检、怎么安全重启、怎么验收。
  Use when 用户说「8796 上加个功能/按钮」「管理台页面改一下」「重启一下 8796」「管理台打不开了」，
  或要在它上面加新的批量动作（兑重置卡、换出口、查额度那一类）。
  🔴 它是**裸进程**不是 k8s 服务，重启方式和别的服务完全不同；它挂着 ChatGPT/Google/Gemini/muse
  四个号池的运维入口，停了这几个池子就没有网页操作面。
---

# 198 号池管理台（8796）改代码 SOP

## 拓扑（先看清，别按 k8s 服务的习惯动它）

| | |
|---|---|
| 怎么跑 | **裸 python 进程**，不在 k8s 里：`python3 /opt/chatgpt-acct-console/server.py --bind 10.68.13.198 --port 8796` |
| 代码 | `198:/opt/chatgpt-acct-console/`，仓库对应 `chatgpt-acct-console/`（**untracked，不在 git 里**）|
| 前端 | 单文件 `static/index.html`（~176KB，原生 JS 无框架）|
| 后端 | `server.py`（HTTP + 路由）、`probes.py`（探针/动作）、`collect.py`（采集）、按池子分的 `gemini_web_pool.py` / `google_bind.py` / `muse_pool.py` / `quota_extra.py` |
| 口令 | `data/.console-token`，请求头 `X-Console-Token`；白名单默认 `10.0.0.0/8` |
| 数据 | `data/probes.json`（探针结果，进程重启后从盘恢复）、`data/server.pid` |

## 加一个新功能 = 四处，漏任何一处都是静默失效

以 11-02 加「兑重置卡」为例：

```
1) probes.py        加执行函数（真干活的那段，通常在号 pod 里 exec python）
2) server.py        do_POST 里加 `if self.path == "/api/xxx"` 分支 → run_job(kind, ids, fn)
3) server.py        🔴 PROBES 字典加 "xxx": {}   ← 最容易漏，见下
4) server.py        _WRITE_PATHS 加一条（写操作要留审计：谁的 IP、什么时候、做了什么）
5) static/index.html  按钮 + onclick + 「正在跑」那行的任务名映射
```

🔴 **第 3 步漏了的后果（11-02 实测）**：`run_job` 里是 `PROBES[kind][i] = r`，
`kind` 没在 `PROBES` 里登记就抛 `KeyError`，而异常被 `ThreadPoolExecutor` **吞掉** ⇒
任务显示「跑完了」、结果 **0 条**、页面什么都不显示。
**而底层动作其实已经执行了** —— 那次兑重置卡的卡真扣了（3→2 张、周额度 100%→0%），
只是结果写不进页面。我当时先说了「卡没被兑掉」，是错的，查号的真实 usage 才看清。
⇒ 判「这个动作到底做了没」只认**被操作对象自身的状态**，不是页面有没有结果。

## 部署前自检：`py_compile` 不够

🔴 **`python3 -m py_compile` 查不出 `NameError`**（它只查语法）。
11-02 我加了用 `os.environ` 的代码但 `probes.py` 里没 `import os`，
`py_compile` 全绿、推上去进程起不来，**页面停了约 2 分钟**。

部署脚本里必须有这一步，过不了就不重启：

```bash
cd /opt/chatgpt-acct-console
python3 -c "
import sys; sys.path.insert(0,'/opt/chatgpt-acct-console')
import probes, server
print('真 import OK')
print('新 kind 已登记:', 'xxx' in server.PROBES)
print('写操作已登记审计:', '/api/xxx' in server._WRITE_PATHS)
" || { echo 'STOP import 失败，不重启'; exit 1; }
```

本机想自检（没有 kubectl）可以塞个假 `collect`：
```python
import sys,types
m=types.ModuleType('collect'); m.K=['kubectl']; m.sh=lambda *a,**k:''
sys.modules['collect']=m
import probes   # 这样能在 Mac 上验出 NameError
```

## 重启：三个坑，每个都让我白跑一轮

🔴 **坑 1：别用 `pgrep -f 'server.py'` 挑 pid** —— 这个模式会匹配到**执行它的 ssh/bash 自己**。
11-02 我 kill 掉的是自己的命令行，真进程没动，于是「部署完了」但接口还是 404。
**判据只认「真在听 8796 的那个 pid」**：
```bash
PID=$(ss -lntpH | grep ':8796' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
```

🔴 **坑 2：`data/server.pid` 会过期** —— 进程常被人用 `sudo setsid nohup` 手工起（没走 `run.sh`），
pidfile 里还是上一个 pid。直接 `run.sh restart` 会停不掉真进程，
**留下两个进程抢 8796**。先把 pidfile 对齐再重启。

🔴 **坑 3：先杀再起，中间等端口真的释放** —— 只负责「起」不负责「杀」会得到
`OSError: [Errno 98] Address already in use`，而且**旧进程还在跑旧代码**，
看着像「部署没生效」。

完整的重启段（放在脚本里跑，别在命令行套三层引号）：
```bash
PID=$(ss -lntpH 2>/dev/null | grep ':8796' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
[ -n "$PID" ] && kill "$PID"
for i in $(seq 1 15); do ss -lntH | grep -q ':8796' || break; sleep 1; done
setsid nohup python3 /opt/chatgpt-acct-console/server.py --bind 10.68.13.198 --port 8796 \
  > /var/log/console8796.log 2>&1 < /dev/null &
sleep 12
NEW=$(ss -lntpH | grep ':8796' | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2)
echo "$NEW" > /opt/chatgpt-acct-console/data/server.pid
```
🔴 日志别写 `/tmp/console8796.log` —— 属主可能是别的用户，`Permission denied` 会让进程起不来
（11-02 踩过：服务停了但起不来）。用 `/var/log/`。

## 验收：三层，缺一层都可能假绿

1. **进程层**：只有 1 个在听 8796、pidfile 和真 pid 一致
2. **接口层**：新接口不再回 404；各道闸门按预期拒（没 ids / 超上限 / 没口令）
3. **🔴 渲染层**：真起浏览器打开页面，看 window 错误 0、目标文案真出现在 DOM 里、窄屏不横滚
   —— **只看 HTTP 200 或 bundle 里 grep 到字符串都不算**
   （记忆 `feedback_ui_ship_check_200_plus_bundle_is_false_green`）

🔴 **grep 页面源码数文案会把代码本身数进去**：11-02 我 grep `retrying` 在页面里有 6 次命中，
以为改漏了，其实那 6 处全在**注释和那个剥除函数自己**里（`msgZh` 正是用来去掉它的）。
判「用户看到什么」要看**渲染后的 DOM**，不是源码。

## 改页面前先问：这个数是不是在前端抄死的

`server.py:375` 的注释记着一次旧事故：那 7 个出口 IP 曾在 `index.html` 里又抄了一份，
改了后端页面不跟。11-02 又发现两处同类：`EG_KBPS` 带宽快照、`muse_pool.DEFAULT_EXIT` 的字面量兜底。
⇒ 页面上任何「看起来像现量」的数字，先 grep 一遍它是不是前端常量。

## 现有的批量动作（照它们加新的）

| 接口 | 干什么 | 并发 |
|---|---|---|
| `/api/quota` | 每号在 pod 里读 `wham/usage` + 订阅状态（只读）| 4 |
| `/api/egress` | 探每号的实际出口 IP | 4 |
| `/api/test` | **真推理**（花额度），一次最多 10 个 | 3 |
| `/api/proxy-check` | 探每个出口能不能连 chatgpt.com | 4 |
| `/api/change-egress` | 换出口（**重启号 pod + 刷一次性 refresh_token**）| **1** |
| `/api/reset-card` | 兑重置卡（**花钱**，每号每月只补 1 张）| 2 |

🔴 `/api/test` 的结果判据是 **`verdict == "SERVES"`**，不是 `ok`。
11-02 我拿 `v.get('ok')` 读，5 个号全判「失败」，而 detail 里明明写着
`completed · 回复「ok」` ⇒ 看字段名之前先打印一条看清形状。

## 相关

- 重置卡的底层通路/端点/headers ⇒ 记忆 `chatgpt-banked-reset-cli-redeem`，CLI `scripts/chatgpt-acct-reset-bank.{py,sh}`
- 换出口的风险 ⇒ `egress_ops.run` 会先刷 grant，**ChatGPT 的 refresh_token 是一次性的**，
  写回失败那个号就作废 ⇒ 永远 `workers=1`、一个一个来、每个都核验出口真落对了再做下一个
- 各池子的业务规则 ⇒ `gemini-web-pool-198` / `muse2api-pool-ops` / `cliproxy-antigravity-ops`
