---
name: codex-oneclick-rollout
description: 给全公司铺 Codex 接入（198 carher 网关）的一键脚本 SOP——每个系统一个文件（codex-mac.command / codex-windows.bat），写配置 + 装生图工具 + 把 key 能用的全部模型同步进 Codex CLI 和 Desktop App 的选择器并每日自动刷新。含 src/+build.sh 单文件打包、飞书《Codex一键配置》附件替换与 sha 回环、非 OpenAI 模型目录元数据的三个实测坑、gpt-image-2 白名单批量开通。Use when 要给同事配 codex / 改 codex-oneclick 下任何脚本 / 用户说选择器里没有某模型、选了 claude/grok 改不了文件、画图画成 SVG 或 403 / 要更新《Codex一键配置》文档。
---

# Codex 一键接入（配置 + 生图 + 模型同步）

> 实证：2026-09-10 生图全链路 + key 铺满；**2026-10-08/09 模型自动同步上线、改单文件、review 修 8 处**。
> 档案：memory `project_codex_oneclick_imagegen_rollout_2026_09_10`、`project_codex_oneclick_models_autosync_2026_10_08`。
> 用户文档：飞书《Codex一键配置》wiki `BzVZwGMSOiSYOykc6UYcN2opn3b` = docx `HMj6d1ttlorc5KxbFnlcVuopntf`(10-15 起唯一一篇,外部分享已全删);正文由 `scripts/agent-oneclick-docs/` 生成。
> **同族**(2026-10-12 用户拍板:三个 agent 每人一把 cursor key 共用):
> Cursor《一键配置》`OQCPdLd4MovEVoxGzdMcD3CJnCf`(10-15 起只剩这一篇)(skill `cursor-client-bundle-patch`)、
> Claude Code《一键配置》`MeLad7oMiobwf6xnpA2cWy0nnSg`(源 `claude-oneclick/`,结构照抄本目录的 `build.sh`)。
> 三篇文档里都有一句「同一把 key」+ 互链,改一篇时顺手核另两篇别说法打架。

## 0. 先判卡在哪

| 现象 | 卡在 | 去哪节 |
|---|---|---|
| 401 / url 是 api.openai.com | 配置没写上 | 重跑脚本 |
| 选择器里只有几个 GPT | 模型同步没跑（多半没 Python） | §3 |
| 选了 claude/grok/kimi，改文件报 `first line of the patch must be '*** Begin Patch'`、或回 DONE 文件没变、或说没有 shell | 模型目录元数据不对 | §3 坑①② |
| 长对话后 claude-haiku/sonnet-5 被上游拒 | 上下文上限按 272k 登记 | §3 坑③ |
| 选了某模型报 `key not allowed to access model` | 这把 key 没开该模型 | §4 |
| 画成 SVG / DNS 解析失败 / 403 | 生图 | §2 |
| qwen3-coder-next、claude-haiku-4-5 在 codex 里转圈不结束 | **模型本身**（有无目录同样复现，未查根因） | 换模型 |

## 1. 脚本本体与发版

```
codex-oneclick/
  src/install-mac.sh        ← 改这里
  src/install-windows.ps1   ← 改这里
  src/carher_image.py       ← 生图工具
  src/carher_models_sync.py ← 模型同步（唯一实现；scripts/sync-codex-model-catalog.py 只是转调它）
  tests/                    ← 回归测试，每条对应一个实测坑
  build.sh                  ← 跑测试 → 把两个 .py 内嵌 → 产出下面两个
  codex-mac.command       ← 给用户（macOS/Linux）
  codex-windows.bat       ← 给用户（bat 头 + PowerShell 正文，cmd 跑到 exit /b，后半段 PS 自读执行）
```

**发版四步，缺一步用户拿到的就是旧的**（10-08 前文档挂的一直是 08-27 旧包，10-01 那轮生图根本没传上去）：

1. 只改 `src/`，`bash codex-oneclick/build.sh`（测试不过不出产物；末尾打印两个 sha256）
2. 干净 home 真跑：`printf '%s\n' "$KEY" | HOME=$T CODEX_HOME=$T/.codex bash codex-mac.command`，
   跑完 `launchctl bootout gui/$(id -u)/net.carher.codex-models-sync` + 删 plist，别在本机留定时任务
3. 传附件：`+media-insert` → **下载回来比 sha**（同 build.sh 打印的）→ `block_move_after` 到
   「👇 macOS / Linux 一键脚本：」「👇 Windows 一键脚本：」两段后（10-13 起在第 1 节里，dmg/msix 在第 3 节） → `block_delete` 旧 figure。顺序别反（先删再传失败 = 文档没附件）
4. 收尾核：figure 只有 4 个（两个安装器 + dmg + msix）、`<img>` 仍 2 张、旧 token 0 处
   ⇒ 这一步已进代码：`python3 scripts/agent-oneclick-docs-audit.py`（只读，附件回环 sha + 块数 + 陈旧词，`--selftest` 有阳性对照）

**第 0 步自动装 codex 命令行（10-13 加）**：没有 / < 0.144 ⇒ Node 装 `~/.carher-node`、
`npm -g @openai/codex` 走 npmmirror 装 `~/.carher-npm-global`，写 `# >>> carher-codex >>>` PATH 块
（与 claude-oneclick 同一套，块名不同、互不覆盖）。**失败只 warn 不 die**：App 不依赖命令行。
回归 `tests/test_cli_autoinstall.py` 用假 npm 跑真 bash；真下载 10-13 在空 HOME 实测过（装上 0.162.1，
重跑只打「检测到」）。Windows 段只有静态断言（本机无 pwsh），没有 Windows 真机验过。
Desktop App 不由脚本装 ⇒ 文档第 3 节挂 dmg/msix。

用户侧文档只讲"下载一个文件、双击、粘 key、回车"。**别把本 skill 的排查细节写进用户文档**，
也**别再挂 zip / .ps1 / .py**——用户原话「不要增加那么多脚本，让用户一脸茫然」。

## 2. 生图那一段（脚本第 6 步）

> 🔴 **10-13 本机回归抓到**:Codex 自己给 `carher_image.py` 传「.」当输出路径 ⇒ 旧逻辑写成隐藏文件 `..webp`;
> 已改「给的是目录就在里面自动起名」。`codex exec` 不关 stdin 会挂在 `Reading additional input from stdin` ⇒ 一律 `< /dev/null`。
> 在用户本机验安装器 = `config.toml` 被整份重写 ⇒ 先整目录 rsync 备份,验完还原 config/auth/chatgpt 三件 + 撤 LaunchAgent。

> **10-13 起统一走 `muse-image`**(用户点名「画图都用 muse 那个工具」)。它返回**相对路径** `/v1/media/<32hex>.webp`,
> 文件在 muse2api 上 ⇒ 198 nginx 加了只读出口 `https://cc.auto-link.com.cn/muse-media/<32hex>.<ext>`
> (`# >>> carher-muse-media >>>` 段,备份在 `/etc/nginx/backup-ccdl/`,回滚=删段+reload),工具里改写过去下载;
> 输出后缀按文件头改成 .webp。🔴 key **先读 auth.json 再兜底环境变量**:用户 shell 常有另一把 `OPENAI_API_KEY`(本机实测没开 muse ⇒ 恒 403)。
> 验收=空 HOME 装完后 `codex exec "画一张…"` 日志里出现 `carher_image.py` 且产出图片(10-13 PASS)。

内置 `image_gen` 是 ExtensionItem，本环境**全目录无模型声明该能力 ⇒ 没注册**，修不好，只能绕。
细节与证据见 skill `codex-local-cli-toolchain` §4。绕法两件事：

1. 安装器把内嵌的 `carher_image.py` 写到 `~/.codex/`（**只用 Python 标准库**，用户机器上没有 openai SDK，
   所以不能直接调 `~/.codex/skills/.system/imagegen/scripts/image_gen.py`——那个 import openai）
2. 往 `~/.codex/AGENTS.md` 写一段常驻授权，告诉 Codex 直接调它、不许退化 SVG

### 四个坑，脚本里都埋了

- 🔴 **沙箱默认断网 ⇒ 生图工具连 DNS 都解析不了。** config.toml 必须写：
  ```toml
  [sandbox_workspace_write]
  network_access = true
  ```
  不写的话报 `URLError: [Errno 8] nodename nor servname provided, or not known`。
  ⚠️ **这个坑差点被我漏掉**：我第一次端到端是在自己机器上验的，而我的
  `~/.codex/config.toml` 有 `sandbox_mode = "danger-full-access"`——
  **拿一台配置不同的机器验，验的就不是用户会拿到的东西**。
  判据只能是「用 `CODEX_HOME=/tmp/fakehomeX` 跑一遍脚本，再在那个 home 下跑真实用法」。
- ⛔ **别改 `~/.codex/skills/.system/imagegen/`** —— 每次 codex 启动重新解包覆盖，改了白改。
- ⛔ **python 探测必须实跑一次**，不能只 `command -v`：macOS `/usr/bin/python3` 在没装开发者工具的
  机器上是个**会弹窗的桩**，Windows 上 `python` 可能是微软商店的假桩。判据是
  `"$c" -c 'import json,urllib.request'` 退出码 0。Windows 上通常叫 `python`/`py`，**不叫 `python3`**。
- ⛔ **AGENTS.md 不能无脑 `>>` 追加** —— 用户会重跑脚本，也会自己往里写东西。
  用 `<!-- CARHER-IMAGE-BEGIN/END -->` 标记块，先剥旧块再追加。
  回归判据：预置一份含用户自有内容的 AGENTS.md，跑两遍，
  **用户内容还在 + `grep -c CARHER-IMAGE-BEGIN` == 1**。

### key 从哪来

`carher_image.py` 从 `~/.codex/auth.json` 读 key、从 `config.toml` 正则抠 `base_url`，
**不接受命令行传 key**（避免 agent 向用户索要）。这与一键脚本写入的位置严格对应，改一边要改另一边。

## 3. 模型同步那一段（脚本第 7 步）

**为什么只能客户端同步**：Codex 的 `model_catalog_json` 只认本地文件（openai/codex #36532 远程拉取未实现）。
网关 `/v1/models` 已按 key 白名单过滤（实测 `/key/info` 32 == `/v1/models` 32），列出来的就是能用的。
CLI 的 `/model` 和 Desktop App 选择器读**同一份**目录，一次同步两边都生效。

`carher_models_sync.py` 流程：`/v1/models` 取名单 → `/model/info` 取每个模型 `max_input_tokens`（多部署取最小）→
模板取 `codex debug models --bundled`（字段跟随用户 codex 版本）→ 生成 → **让 codex 自己解析一遍，不过不替换** →
原子替换 + 留 `.bak` → config.toml 没有 `model_catalog_json` 就在第一个 `[table]` 前补一行。
任何一步失败都不动现有文件（目录写坏 codex 直接起不来）。定时：Mac launchd `net.carher.codex-models-sync`
（10:17 + RunAtLoad），Windows 计划任务 `CarHer Codex Models Sync`（DAILY + ONLOGON，pythonw 直调），
Linux crontab。日志由脚本自己写 `~/.codex/carher-models-sync.log`（超 512KB 清空）。

### 三个实测坑（tests/ 里各有一条回归）

非 OpenAI 模型要从官方条目复制元数据，但 5.6/6 系的元数据是 OpenAI 专属工具形状：

| # | 字段 | 错的值会怎样（A/B 实测） | 现在 |
|---|---|---|---|
| ① | `tool_mode` / `use_responses_lite` | 套 `code_mode_only` ⇒ grok-4.7 两次都拿不到 shell，一个文件建不出；同 home 去掉目录即好 | 模板换 gpt-5.5 + 钉 `None/False` |
| ② | `apply_patch_tool_type` | `freeform` ⇒ kimi 报错 5 次、composer/sonnet-5/5.5 各 2 次；**sonnet-5.5 回 DONE 但文件没改** | `None`（codex 只认 `freeform` 或 null） |
| ③ | `context_window` | 全按 272k ⇒ haiku/sonnet-5 实际 200k，超过才压缩、上游先拒 | 用 `/model/info` 往下压，不放大 |

🔴 **②是"新建文件"测不出来的**——前几轮全用"Create file X"验，全绿；换成"修 calc.py 里的 bug"才暴露。
验非 OpenAI 模型**必须用改已有文件的任务**。

### 其余坑

- `subprocess(text=True)` 不写 `encoding` ⇒ 中文 Windows(GBK) 解 codex 输出崩；`LC_ALL=C` 可在 mac 复现。已写死 utf-8。
- 路径写 TOML 用 `json.dumps`（基本字符串），**不用单引号字面量**：用户名 `O'Brien` 会破。
  `codex -c model_catalog_json=<路径>` 值**不加引号**（TOML 解析失败按原样字符串），Windows 走 `codex.cmd` 也不经 cmd 转义。
- Windows 只装 Desktop App（msix）时 PATH 里没有 codex ⇒ 去 `WindowsApps/OpenAI.Codex*` glob；npm 装的优先找真 `codex.exe` 绕开 `codex.cmd`。
- PS 顶部 `ErrorActionPreference=Stop`：`schtasks` 不可用会**中断整个安装**（容器复现），必须 try 包住。
- 用户 `/model` 选完会写回 config.toml 的 `model=`；**重跑安装脚本会重置回 gpt-5.6-terra**。
- 生图模型和 `codex-auto-review` 不进选择器（前者不能对话，后者 codex 自己调）。

### 验收（只认真实用法）

```bash
CODEX_HOME=$T/.codex codex debug models | python3 -c "import json,sys;print(sum(m['visibility']=='list' for m in json.load(sys.stdin)['models']))"
# 改已有文件，每个非 OpenAI 家族至少一个：grok / claude / kimi / deepseek
printf 'def add(a, b):\n    return a - b\n' > calc.py
CODEX_HOME=$T/.codex codex exec --skip-git-repo-check -m claude-sonnet-5-5 "Fix the bug in calc.py so add returns a + b"
grep -c 'a + b' calc.py; grep -c 'apply_patch verification failed' <日志>   # 1 和 0
```

CLI 的 `/model` 交互要验时用 tmux：连发 Down 只动一格，要 `sleep 0.25` 逐个发；在顶部按 ↑ 回绕到末尾。
claude 系改一个小 bug 要 2~3 分钟，**有无目录一样慢**，别当回归。

## 4. key 白名单那一段

**用现成的 `scripts/litellm-198-key-allowlist.py`，别手搓。**
它有 dry-run 默认 / `--backup` 快照 / `--restore` 回滚 / `--limit` 灰度，手搓脚本这些都没有。
2026-09-10 我就手搓了一版一次性脚本，属于重造轮子——能跑，但没有回滚快照。

```bash
# 在 198 上跑
export LITELLM_MASTER_KEY=$(kubectl -n litellm-product get secret litellm-secrets \
  -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
python3 litellm-198-key-allowlist.py --prefix carher- --add-model gpt-image-2          # dry-run
python3 litellm-198-key-allowlist.py --prefix carher- --add-model gpt-image-2 --limit 1 \
  --apply --backup ~/img-canary-$(date +%Y%m%dT%H%M%S).json                            # 灰度一把
python3 litellm-198-key-allowlist.py --prefix carher- --add-model gpt-image-2 \
  --apply --backup ~/img-full-$(date +%Y%m%dT%H%M%S).json                              # 全量
```

不变量（脚本已编码，自己写也必须守）：
- `/key/update` 是**整字段替换** ⇒ 每次写都得 read-merge-write，绝不能发裸 list。
- `models == []` 是**无限制**，给它加白名单等于**收窄权限** ⇒ 必须跳过。
- 白名单只授权、**不路由**；放进 fallback 链是另一回事。

### 收敛判据（psql，不信脚本自报）

```sql
SELECT count(*) AS n,
       count(*) FILTER (WHERE models @> ARRAY['gpt-image-2']) AS has_img,
       count(*) FILTER (WHERE array_length(models,1)>0
                        AND NOT (models @> ARRAY['gpt-image-2'])) AS needs
FROM "LiteLLM_VerificationToken"
WHERE (blocked IS NULL OR blocked=false) AND key_alias ILIKE 'carher%';
```

`needs = 0` 才算收敛。**另外看 `avg(array_length(models,1))` 是否恰好 +1** ——
并发写会静默互相覆盖，均值没涨说明有人把你的字段整个替掉了。

2026-09-10 实测终态：`cursor-* 634/634` · `carher-* 227/227` · 全库 1853 活跃里 1486 有权限、
285 无限制、**余 82 把是 `wa-*`/`tmpreg-*`/`probe-*` 等内部探针 key，故意不给**。

⚠️ 铺之前先在群里/用 SendMessage 问一句有没有人也在批量写 key——
两边同时 read-merge-write 会**双方都返回 200 而互相覆盖**。收尾要跑反向对照：
拿对方的字段当尺子，确认自己没冲掉他的。

## 5. 图像模型实测对照（198 桥，2026-09-10）

| 模型 | 端点 | 耗时 | 产物 | 结论 |
|---|---|---|---|---|
| **`gpt-image-2`** | `/v1/images/generations` | 39s / 46s / 51s | ~2.4-3.6MB PNG | ✅ **脚本默认用它** |
| `ag-gemini-3.1-flash-image` | `/v1/chat/completions` + `modalities` | **20s** | 1.1MB | ⚠️ 更快但走 chat 协议，工具不支持 |
| `image-2` | images | 48s | 3.4MB | 能用，更慢 |
| `image-2-zero` | — | 0.1s | — | ❌ 403，不可用 |

**官方新模型吃不到**：`gpt-image-2.5-flare` / `gpt-image-2.5-sunburst` 是真实存在的名字
（2026-09-10 从官网确认），但在 198 桥上 403（白名单未开），且**用 acct 凭证也打不了官方**——见 §6。

## 6. 已证伪，别再捡回来

| 假设 | 证伪数据 |
|---|---|
| 用 acct 池的凭证就能直连官方出图 API | `POST api.openai.com/v1/images/generations` → **401 `Missing scopes: api.model.images.request`**；`gpt-image-2.5-flare` 和 `gpt-image-2` 两个名字**同一句拒绝** ⇒ 与模型名、与我们白名单都无关。acct 里是 ChatGPT 订阅态 OAuth，不是 platform key，两套账两套计费。 |
| 拿 `/v1/models` 的 403 能推断出图端点也不行 | **不能**。那条要的是 `api.model.read`，出图要的是 `api.model.images.request`，是不同 scope。我 09-10 用前者推后者，被用户当场驳回——**一个端点的 scope 失败不构成对另一个端点的证据**。 |
| 集群里翻一翻应该有 platform key | 阿里云 + 198 全量 secret 扫过，**一把 `sk-proj-`/`sk-svcacct-` 都没有**。要用官方新模型得单独申请+绑卡。 |
| `codex features list` 显示 `image_generation true` = 能用 | 特性开着、工具照样没注册，本机实测就是这个组合。 |
| 打 `chatgpt.com/backend-api/codex/responses` 带 `image_generation` tool 能出图 | **未证实也未证伪**：6 次里 5 次 `server_is_overloaded`，**连不带 tool 的阳性对照也在掉** ⇒ 那一轮量具作废，不许拿它下任何结论。模型名必须用 acct config 里那套（`gpt-5.6-sol` 等），`gpt-5.6`/`gpt-5-codex` 一律按名字 400。 |

## 7. 生图验收：只认真实用法

不要拿"脚本退出码 0"当验收。三条都要：

```bash
# ① 工具本身能出图（管理员侧）
python3 ~/.codex/carher_image.py "a yellow chick pecking rice" /tmp/acc.png && file /tmp/acc.png
# ② Codex 真的会自己调它（用户侧用法）
codex exec --skip-git-repo-check -C /tmp/imgtest '画一张小鸡吃米的插画'
# ③ 重复安装不破坏用户内容
grep -c 'CARHER-IMAGE-BEGIN' ~/.codex/AGENTS.md   # 必须 == 1
```

`file` 必须报 `PNG image data`，**别只看命令成功**。产物要肉眼看一眼是不是画的那个东西。

2026-09-10 用脚本生成的干净 home 实测②：Codex 自主读 AGENTS.md → 直接 exec
`carher_image.py` → `已生成：chick_pecking_rice.png（2.1 MB，用时 59 秒）`，
`file` 报 1370×1148 PNG，肉眼确认是小鸡啄米。日志里那行
`sandbox: workspace-write [...] (network access enabled)` 就是配置生效的判据。

## 8. 跨机传脚本的血泪

给 198 传脚本时 **`scp` 静默产出过坏文件**：字节数一模一样（1925），
但开头是一整片 `\x00`。要不是传完核了一次指纹，227 把 key 就要被一个行为不可预测的脚本处理。

- ✅ 传完**必核 sha256**，不核就别执行。
- ⛔ 比指纹要用**同一把尺子**：mac 的 `shasum -a 256` 和 linux 的 `sha256sum` 输出格式不同，
  我第一次就因此误判成"传输损坏"，差点把好文件当坏的。统一用 `openssl dgst -sha256`。
- ✅ 更稳的传法：`B=$(base64 < f | tr -d '\n')`，远端 `echo $B | base64 -d > f`，内容自校验。

多层 ssh 下发含引号的命令一律走 base64 封装，别硬拼转义（三层 ssh + heredoc + SQL 引号必炸）。

## 脚本

| 路径 | 作用 |
|---|---|
| `codex-oneclick/build.sh` | 跑 tests → 内嵌 → 产出两个单文件 + 打印 sha256 |
| `codex-oneclick/src/install-mac.sh` · `src/install-windows.ps1` | 安装器源 |
| `codex-oneclick/src/carher_image.py` | 零依赖生图工具 |
| `codex-oneclick/src/carher_models_sync.py` | 模型同步唯一实现 |
| `codex-oneclick/tests/test_carher_models_sync.py` | 回归：§3 三个坑 + TOML 路径 + 过滤规则 |
| `scripts/sync-codex-model-catalog.py` | 本机手用入口，转调上面那份（10-09 前是独立实现，带着①②③三个坑） |
| `scripts/litellm-198-key-allowlist.py` | **批量开通白名单就用它**，别手搓 |

相关 skill：`codex-local-cli-toolchain`（内置工具为何没注册、137 分诊）·
`litellm-198-key-allowlist`（白名单通用 SOP）·`codex-desktop-startup-diagnose`（启动卡顿，另一条线）。
