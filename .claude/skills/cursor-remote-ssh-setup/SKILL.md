---
name: cursor-remote-ssh-setup
description: >-
  把 Cursor 的 Remote-SSH 接到一台新机器上，并**预置**与本地 Cursor commit 精确匹配的
  cursor-server（Cursor 的 server 分发源 `cursor.blob.core.windows.net` 慢到 ~55KB/s
  且**拒绝开放式 range**，`curl -C -` 必 `curl: (33)` —— 指望首连自动下完会挂在半路）。
  含：ssh config + 公钥免密、`remote.SSH.*` 三个设置项、**8 路闭区间 range 并行分块**
  下载法、commit 匹配与「真启动」两道验收、以及远程窗口里 BYOK 自定义模型要的
  `RemoteForward` 反向隧道。Use when 用户说"配置 cursor 远程访问"/"cursor 连不上远程"/
  "Remote-SSH 卡在 downloading vscode server / installing server"/"远程窗口里模型打不通"/
  "给某台机器配远程开发"。
---

# Cursor Remote-SSH 接一台新机器

## 0. 先说结论：唯一会卡住你的是 server 下载

Remote-SSH 首连时要在远端装一份 `cursor-server`，版本必须与**本地 Cursor 的 commit 完全一致**。
Cursor 的分发源实测：

| 事实 | 数据（2026-09-21 实测） |
|---|---|
| 单流速度 | 远端 ~65KB/s、本地 Mac ~54KB/s —— **是源站慢，不是你的网络** |
| 单流稳定性 | 下到 ~39.6MB 必停滞，最终 `curl: (28)` 超时 |
| `curl -C -` 续传 | **199/199 次全失败**：`curl: (33) HTTP server doesn't seem to support byte ranges` |
| 显式闭区间 `-r s-e` | ✅ 返 **206** —— 支持的是闭区间，拒的是开放式 `bytes=X-` |
| 8 路并行分块 | ~200KB/s（3.4×），119MB 约 17 分钟拉完 |

⛔ **别信 `downloader.cursor.sh`** —— 远端 DNS 根本解析不了它（`Could not resolve host`）。
真正的下载地址在本地 `product.json` 的 `serverDownloadUrlTemplate` 里，当前是
`https://cursor.blob.core.windows.net/remote-releases/${commit}/vscode-reh-${os}-${arch}.tar.gz`。
**每次用前重读这个字段**，别把上面这条 URL 抄成常量。

---

## 1. 取本地 commit（一切的锚点）

```bash
python3 -c "
import json;d=json.load(open('/Applications/Cursor.app/Contents/Resources/app/product.json'))
print(d['version'], d['commit']); print(d['serverDownloadUrlTemplate'])"
```

## 2. ssh config + 公钥免密

```bash
# ~/.ssh/config，每台一段
Host <alias>
    HostName <ip>
    User <user>
    Port <port>
    IdentityFile ~/.ssh/id_rsa
    ServerAliveInterval 30      # 不加的话 Cursor 长连接会被中途掐断
    ServerAliveCountMax 6

chmod 600 ~/.ssh/config
sshpass -p '<pw>' ssh-copy-id -o StrictHostKeyChecking=accept-new -i ~/.ssh/id_rsa.pub <alias>
```

验收**必须**带 `-o PasswordAuthentication=no`，否则你验的是密码登录不是公钥：

```bash
ssh -o BatchMode=yes -o PasswordAuthentication=no <alias> \
  'echo OK $(whoami)@$(hostname) $(hostname -I | awk "{print \$1}")'
```

> 同一台机器有内网/公网两个入口时，分别建两个 Host 段就行，**不是 ProxyJump**。
> 用 `hostname` 回显确认两个别名落到同一台，别靠 IP 注释推断。

## 3. Cursor settings.json

`~/Library/Application Support/Cursor/User/settings.json`（**先 `cp` 一份 `.bak-<日期>`**）：

```jsonc
"remote.SSH.remotePlatform":  { "<alias>": "linux" },   // 省掉首连问你平台
"remote.SSH.connectTimeout":  60,                        // 默认 15s，公网入口不够
"remote.SSH.serverInstallPath": { "<alias>": "<远端$HOME>/.cursor-server" }
```

⚠️ `serverInstallPath` 要填**真实 $HOME**。别默认是 `/home/<user>` —— 实机上遇到过
`/Data/lorin`。先 `ssh <alias> 'echo $HOME'`。

## 4. 预置 server（核心）

远端布局是 `~/.cursor-server/bin/linux-x64/<commit>/`，tar 顶层是 `vscode-reh-linux-x64/`
⇒ 解压要 `--strip-components=1`。

先拿准确长度（**不要猜**，也不要拿同目录旧 tarball 的大小当分母）：

```bash
curl -sSI "<url>" | grep -iE '^(HTTP|content-length)'
```

然后在远端 `nohup` 跑并行分块（ssh 断开不影响）：

```bash
#!/bin/bash
C=<commit>; TOTAL=<content-length>; CH=4194304
U="https://cursor.blob.core.windows.net/remote-releases/$C/vscode-reh-linux-x64.tar.gz"
D=/tmp/reh-chunks; mkdir -p $D
N=$(( (TOTAL + CH - 1) / CH ))
fetch() {
  i=$1; s=$((i*CH)); e=$((s+CH-1)); [ $e -ge $TOTAL ] && e=$((TOTAL-1))
  want=$((e-s+1)); f=$D/p$(printf %04d $i)
  for t in $(seq 8); do
    [ -f "$f" ] && [ "$(stat -c %s "$f")" = "$want" ] && return 0
    curl -sS -L --max-time 300 -r "$s-$e" -o "$f" "$U" 2>/dev/null
  done
  echo "CHUNKFAIL $i" >&2; return 1
}
export -f fetch; export D U CH TOTAL
seq 0 $((N-1)) | xargs -P 8 -I{} bash -c "fetch {}"
cat $D/p???? > /tmp/reh-$C.tar.gz
echo "final=$(stat -c %s /tmp/reh-$C.tar.gz) expect=$TOTAL"
```

**进度会回退是正常的**：某个 chunk 失败重试时 `curl -o` 会把它截断重下，`du` 因此掉几 MB。
判卡住不看总量，看**逐块对账**：

```bash
for f in $D/p*; do i=$((10#${f##*/p})); s=$((i*CH)); e=$((s+CH-1))
  [ $e -ge $TOTAL ] && e=$((TOTAL-1)); want=$((e-s+1))
  [ "$(stat -c %s $f)" != "$want" ] && echo "BAD $i"; done
```

装进去：

```bash
gzip -t /tmp/reh-$C.tar.gz            # CRC，先证明拼接没坏
DEST=$HOME/.cursor-server/bin/linux-x64/$C
mkdir -p "$DEST" && tar xzf /tmp/reh-$C.tar.gz -C "$DEST" --strip-components=1
rm -rf /tmp/reh-chunks /tmp/reh-$C.tar.gz
```

## 5. 两道验收（缺一不可）

⛔ **「目录里有文件」不是绿。** 必须这两条都过：

```bash
# ① commit 对账 —— 装错版本 Cursor 会绕过它重新下载，等于白装
python3 -c "import json;d=json.load(open('$DEST/product.json'))
print(d['version'], d['commit'])"     # 必须 == 本地 product.json 的 commit

# ② 真启动 —— 证明 glibc/node 在这台机上跑得动
timeout 12 "$DEST/bin/cursor-server" --start-server --host=127.0.0.1 --port=0 \
  --without-connection-token --accept-server-license-terms
# 看到 "Server bound to 127.0.0.1:<port>" + "Extension host agent listening" 才算数
```

顺带确认运行时：`ldd --version`（Cursor server 要 glibc ≥ 2.28）。

## 6. 远程窗口里的 BYOK 自定义模型

Cursor 里 base URL 写的 `http://127.0.0.1:8788/v1` 指的是 **Mac 上**的 sidecar。
远程窗口里请求由远端发起，它的 `127.0.0.1` 是那台 Linux ⇒ **模型全打不通**。
在 ssh config 该 Host 段加反向隧道，同一个 base URL 两侧都成立，Cursor 设置一个都不用改：

```
RemoteForward 8788 127.0.0.1:8788
```

⚠️ 已有连接占着该端口时，新连接会 warn `remote port forwarding failed for listen port 8788`
—— **无害**，连接照建，复用已存在的隧道。别把这行当故障去"修"。

### 6.1 给同事的一键版（09-21 加，已进装机包）

不要让同事手改 `~/.ssh/config`。装机包 `cursor-g-setup.zip` 里已带：

```
REMOTE-SSH-Mac.command / REMOTE-SSH-Windows.cmd     双击版（列主机→预演→输 yes 才写）
cursor_remote_ssh.sh --host <h> [--apply|--remove --apply]   命令行版
```

引擎 `scripts/zk-cursor-web/cursor_remote_ssh.js`，跑在 Cursor 自带 node 上（零依赖）。
它的五条分支**都跑过真机**（09-21）：公网直连早退 / 幂等 / 已有 Host 块加行 / 新建块 / remove。

几个设计点，改它之前先读：

- **第 ① 步先读库里的 `openAIBaseUrl` 再决定做不做**。不是 `127.0.0.1` 的机器（Windows、
  关了 sidecar 的 Mac）直接早退、一个字不改 —— 默认"人人都是 127.0.0.1:8788" 会给
  这些人白改 ssh 配置。端口也从这个地址里读，不写死 8788。
- **第 ② 步先量本机 sidecar**。它没起来的话，隧道配对了症状照样是"模型连不上"，
  不先排这一步就会把"sidecar 没开"误判成"隧道没配好"。
- **④ 阴性 → ⑦ 阳性认的是「监听者是谁」，不是状态码**（10-13 改）：远端打
  `127.0.0.1:<port>/healthz`，body 含 `delta_url` 才判 `ours`。🔴 只看非 000 曾把
  188 上 zerokey-codex bridge 占着的 8788（回 200 + codex 模型列表）读成「已经通了」。
  `other` ⇒ 退出 4，给两条出路（管理员让端口 / 只这台把 Base URL 改公网 `/pro/v1`）。
  ④=`ours` 幂等早退时，`--apply` 下仍跑 ⑧（老用户恰恰是升级后出问题那批）。
- 所有 ssh 调用带 `ControlPath=none`：复用的老连接不带新 `RemoteForward`，
  验收会读出**假红**。
- 测试钩子 `CRG_SSH_CONFIG` **同时改道写入和探针**（`ssh -F`）。只改"写哪个文件"
  而让探针照旧读真配置，写入分支就只能靠读代码发布 —— 而那正是同事用得最多的一条。
- 加的行都带 `# cr-g-remote-ssh` 标记，`--remove` 只删带标记的行；写前必备份
  `~/.ssh/config.bak-<stamp>`，备份失败即中止。

### 6.2 服务器上的 Cursor 组件也要补（10-13 加，⑧ 步）

「升级 Cursor 后远程窗口坏了」的另一半：`cursor-agent-exec` / `cursor-agent-host` 没有
`extensionKind` ⇒ 远程窗口里它们跑在**服务器 cursor-server 那份**上，本机安装器的件C/件D
补丁管不到 ⇒ 远程窗口「每轮都压缩上下文」/ 偶发打到 Cursor 自家推理报 permission_denied。

- 引擎 `scripts/zk-cursor-web/cursor_remote_patch.js`：从 `cursor_team_setup.js` **按行区间**
  抠 ID 常量 / 件C / 件D 家族（⛔ 按语句切会把 `NOLOOP_MARKER` 切成两份），经 ssh 用服务器
  `~/.cursor-server/bin/linux-x64/<本机commit>/node -` 执行；补 4 个文件，每个先备份
  `<file>.carher-orig`、`node --check` 过了才写。模式 dry / `--apply` / `--revert`。
- 服务器上没有本机同 commit 的目录 ⇒ 打 `NOSERVER`，⑧ 提示「先用 Cursor 连一次再双击」。
  ⇒ **每次升级 Cursor：先连一次服务器（装新 commit），再双击 REMOTE-SSH**。文档和 README 都写了。
  用户侧写在两篇 Cursor 文档「2. 连远程服务器使用」+ zip 里 README.txt;改了提示文案要两处一起改。
- 验收（198，10-13）：出厂干净 cursor-server → ⑧「✅ 已补好(4 个文件)」→ 重连后
  `agent-host-daemon ready`；**用 zip 解压出来的那份跑**，不是工作副本（工作副本绿≠包里有，
  freshness 的 `REQUIRED_MEMBERS` 就是为此加的）。
- 计数按「行首是 `✅ 已打` / `已打过`」的行数，⛔ 不按命中次数（每行尾还带一次「(已打过)」，曾报成 8）。

## 7. 踩坑速查

| 现象 | 真因 |
|---|---|
| 卡在 "Downloading VS Code Server" | 源站慢/停滞 ⇒ 按 §4 预置 |
| 装好了还是重新下载 | commit 不匹配（§5①） |
| 本地 zsh 报 `unmatched '` | 远端多行脚本被引号吃掉 ⇒ 一律 `ssh <alias> 'bash -s' <<'EOS'` |
| 升级 Cursor 后远程连不上 | commit 变了，要重新预置。设 `"update.mode": "none"` 可冻住 |
| 长任务断线就没 | Cursor 终端不是 tmux ⇒ 自己 `tmux new -s <name>` |

## 8. 足迹与回滚

每次做完把这几项写进正文交付：

1. `~/.ssh/config` 段落（新建还是追加、备份在哪）
2. 远端 `~/.ssh/authorized_keys` 多出的那一行（回滚 = 删该行；**不动 sshd 配置**，密码仍可用）
3. Cursor `settings.json` 加的键 + `.bak-<日期>` 位置
4. 远端新增的 `~/.cursor-server/bin/linux-x64/<commit>/`（约 450M；**同级历史 commit 一个都别删**）
5. `/tmp` 下的下载中间物已清（分块 + tarball 约 230M）
