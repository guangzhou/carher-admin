---
name: host-198-chat-subpath
description: >-
  把 198（10.68.13.198）上的服务挂到 https://chat.auto-link.com.cn/<name>/ 子路径（或删掉）。
  当有人说「198 加个域名 / 子域名 / 入口」「新服务要公网 https 地址」「chat.auto-link.com.cn/xxx」时使用。
  不用找 IT（DNS/证书/回源都是现成的），一条命令装上，带冲突检查、备份、nginx -t、回滚和外网自检。
---

# 198 加入口：chat.auto-link.com.cn/<name>/

## 为什么走子路径而不是新子域名（用户 10-29 定的方案）

新子域名 `xxx.auto-link.com.cn` 要 IT 建 DNS + 入口回源。IT 入口**没有泛回源**：没登记的名字打进来是别家的 Sonatype Nexus 404，
198 access.log 里根本没有这一发（10-29 实测）。证书倒是泛证书 `*.auto-link.com.cn`（到 2026-12-04）。
真要独立子域名（比如前端写死根路径又改不了）：先找 IT 登记 DNS→58.241.5.230 + 回源 198:80，再照 `dash.auto-link.com.cn.conf` 建独立文件。
`chat.auto-link.com.cn` 这三样都已经通到 198 了，挂子路径只改 198 本机 nginx，当场生效。
`/oneclick`、`/keyprobe`、`/feedback`、`/myai` 都是这么挂的。

```
用户 ──https──▶ chat.auto-link.com.cn（IT 入口 58.241.5.230 终结 SSL）──▶ 198:80 nginx
      chat.auto-link.com.cn.conf：/<name>/ → 你的服务；其余 → Open WebUI
```

## 一条命令

```bash
scripts/host-198-chat-subpath.sh check  kevin-demo                    # 只读：名字能不能用
scripts/host-198-chat-subpath.sh add    kevin-demo 127.0.0.1:30999    # 装上
#   --keep-prefix   原样转发 /kevin-demo/...（服务支持 base path 时用，最稳）
#   --ws            WebSocket
#   --body 100m     上传上限（默认 32m）
#   --timeout 600   读超时秒（默认 300；流式/长请求调大）
#   --dry-run       只打印要插的段，不碰 198
scripts/host-198-chat-subpath.sh remove kevin-demo                    # 回滚
scripts/host-198-chat-subpath.sh list                                 # 看 chat 上已有哪些段
scripts/host-198-chat-subpath.sh selftest                             # 改完脚本必跑（约 30s）
```

`selftest` = 198 上起临时 http.server → `add zz-csp-selftest` → 外网取到 marker → `remove` → conf sha256 与改前一致 → 杀临时服务。
末行 `SELFTEST PASS` 才算过。跑完会在 `/etc/nginx/backups/` 留两份 `*zz-csp-selftest` 备份，可删。

前提：本机 `ssh cltx@10.68.13.198` 免密 + `sudo -n`（`H198=` 可改）。

`add` 顺序，任何一步失败都打 `STOP: 原因` 停下：

1. 名字不能是 Open WebUI 自己的路径（`api static assets _app oauth ws c s admin …`）或已有段，也不能和 conf 里已有 location 重名
2. 198 上 curl 上游，`000` 就停
3. 备份整份 conf 到 `/etc/nginx/backups/chat.auto-link.com.cn.conf.<时间>.pre-<name>`
4. 在 `location / { proxy_pass http://open_webui_upstream;` 之前插一段 `# >>> chat-subpath:<name> >>> … # <<< … <<<`
   （锚点必须唯一，否则不改）
5. `nginx -t`，失败恢复备份、不 reload
6. `nginx -s reload`
7. 外网自检：`/<name>/` **不能是 Open WebUI 页面**（最多等 60s 让旧 worker 退），之后 `/<name>` 应 302；
   `/api/version`（Open WebUI 对照）不是 200 ⇒ **自动 remove 回滚**

退出码：0 成功 / 1 前置不过 / 2 nginx -t 失败已回滚 / 3 上线后自检失败 / 4 ssh 失败。

## 改这个脚本时踩过的坑

- **reload 后立刻测会假红**：旧 worker 还在，`/<name>` 回的是 Open WebUI 的 200 不是 302。302 检查必须放在内容检查通过之后。
- **ssh 里起后台进程卡死**：`mkdir … && cd … && nohup srv &` 的 `&` 作用于整条 `&&` 链，子 shell 攥着 ssh 的 stdout ⇒ ssh 永不返回。
  后台进程单独一句 + `setsid … </dev/null >/dev/null 2>&1 &`。
- **`pkill -f <pattern>` 放进 `ssh '…'` 会把这次 ssh 自己杀掉**（远端 shell 命令行里就有那串 pattern）⇒ 后面的清理不执行。用 pid 文件 `kill $(cat x.pid)`。
- **bash 3.2（Mac 自带）`$VAR` 后紧跟全角字符会把字节吞进变量名**（`unbound variable ENTRY_IP�`）⇒ 中文旁一律 `${VAR}`，改完跑 `scripts/lint-bash-cjk-vars.py`。
- **`grep -r` 不跟软链**：`sites-enabled/` 里 dash 是软链，查重名要 `grep -R`。

## 两种转发方式怎么选

| | 默认（去前缀） | `--keep-prefix` |
|---|---|---|
| 浏览器请求 | `/kevin-demo/a/b` | `/kevin-demo/a/b` |
| 服务收到 | `/a/b` | `/kevin-demo/a/b` |
| 服务要改吗 | 不用 | 要支持 base path（FastAPI `root_path`、Vite `base`、Next `basePath` 等） |
| 坑 | 前端写死 `/api`、`/assets` 这类**根路径** ⇒ 打到 Open WebUI 上 | 无 |

默认模式已自动补：服务回的根路径跳转改写到 `https://chat.auto-link.com.cn/<name>/…`，cookie path 改成 `/<name>/`，
并带 `X-Forwarded-Prefix: /<name>`。前端是纯相对路径（`./api`、`assets/x.js`）就没问题；写死根路径的话要么改前端，
要么换 `--keep-prefix`，要么像 `/myai` 那样手写 `sub_filter`（见 `scripts/myai-chat-subpath/`）。

## 红线

- **Open WebUI 是单页应用，任何不存在的路径都回 200 + Open WebUI 首页。** 状态码 200 不代表挂上了；判据是返回的内容是你的服务。
- **只动自己那段**，别手改别的段；其它段有各自的部署脚本（oneclick / feedback / myai），它们认同一个锚点。
- **绝不往 `cc.auto-link.com.cn.conf` 里加**：LiteLLM 灰度切流量会整份重写它，手加的会被静默冲掉。
- **备份不放 `sites-enabled/`**（`include sites-enabled/*` 会把 .bak 一起加载）。只 reload，不 restart。
- reload 后旧 worker 还会用旧配置跑一两分钟，自检失败先等一分钟再测。
- 服务**没有自己的鉴权**的话，挂上去就是全公网可访问。
- 和 Open WebUI 同域 ⇒ 同源，浏览器里你的页面能读到 chat 域下的 cookie / localStorage。别挂不可信的第三方页面。
- 上游只允许 `127.0.0.1:<端口>` 或 `10.x.x.x:<端口>`，不许指公网。

## 排障

| 现象 | 看哪 |
|---|---|
| `/<name>/` 打开是 Open WebUI | 段没生效：`list` 看在不在；刚 reload 等一分钟 |
| 页面白屏，控制台 404 `/assets/…` 或 `/api/…` | 前端写死根路径，见上面「两种转发方式」 |
| 登录后跳回 chat 首页 / 跳成 `http://` | 服务生成了绝对跳转，要认 `X-Forwarded-Proto: https` + `X-Forwarded-Prefix` |
| 502 / 504 | 上游没起或端口错：198 `/var/log/nginx/error.log`，或 `scripts/host-198-nginx-5xx.sh` |
| WebSocket 连不上 | 加 `--ws`；IT 入口本身也可能吃掉 Upgrade，见 `owui-ops` §WS |

## 相关

- `owui-ops`：chat 站本体、`Server` 头判断是哪一层
- `scripts/myai-chat-subpath/`：前端写死根路径时用 sub_filter 改写的完整例子
- `scripts/feedback/deploy_198.sh`：静态页 + 小服务挂子路径的例子
