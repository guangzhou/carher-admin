---
name: codex-local-cli-toolchain
description: 修本机 Codex 调本地 CLI 工具（imagegen 出图、任何 python 脚本）跑不起来的整条链路——「需要环境中配置 OPENAI_API_KEY」、pip 装不了任何包、platform.mac_ver() 返回空、truststore 报 int('')、编译报 cannot run C compiled programs / 进程莫名 137，以及「当前环境没有可用的内置图像生成接口」导致 agent 退化画 SVG。含四段体检量具、homebrew python 源码重编（三个坑全埋好）、exec 期 SIGKILL 三腿分诊、image_gen Extension 注册判据与 AGENTS.md 绕法。Use when 本地 codex 说环境变量没配 / 画图画成 SVG 或说没有内置图像接口 / homebrew python3 崩了 / pyexpat 符号找不到 / brew build-from-source 卡死或 configure 跑不了编译产物。
---

# 本机 Codex 本地 CLI 工具链

> 实证：**2026-09-10 全链路修通**。起因是 Codex 报「当前图像生成工具不可用，需要环境中配置
> `OPENAI_API_KEY`」。往下挖出**两个完全独立**的病，别混成一个：
> ① GUI 启动的 Codex 不读任何 shell rc ⇒ 环境变量必须写 `config.toml`；
> ② homebrew python 瓶子因 expat 版本错配整体崩 ⇒ 修完 ① 也照样跑不了脚本。
> 档案见 memory `reference_mac_homebrew_python_expat_broken_2026_09_10`。

## 0. 三十秒分诊

```
cd /tmp && scripts/toolchain_doctor.py        # 四段体检，退出码 0 = 全绿
```

⚠️ **必须 `cd` 到仓库外再跑**。carher-admin 仓库根下有 `operator/` 目录，会遮蔽 stdlib 的
`operator` 模块，让健康的 python 报 `cannot import name 'eq' from 'operator'` —— 假红。

四段任一段红就停在那儿修，别往下跑：下游全是它的下游。

| 红在哪段 | 去哪节 |
|---|---|
| 1. Codex 子进程拿不到 env | §1 |
| 2. python3 崩 | §2 |
| 3. openai SDK 导不进 | §2 末尾一行 pip |
| 4. 上游挂住 | memory `reference_mac_egress_hangs_and_working_mirrors` |

**四段全绿但 Codex 仍不肯画图（说"没有可用的内置图像生成接口"、改画 SVG）⇒ 直接去 §4。**
那是工具没注册，不是工具链坏，体检不会翻红。

## 1. GUI 启动的 Codex 没有 shell 环境

**这是最容易判错的一条。** 终端里 `codex` 能拿到 `OPENAI_API_KEY`，是因为 zsh 读了
`.zshenv`/`.zshrc`；**双击图标起的 Codex 不经过任何 shell**，那些文件一个都不读。
所以「我 export 过了啊」在 GUI 那条路上完全不成立。

修法——写进 `~/.codex/config.toml`：

```toml
[shell_environment_policy.set]
OPENAI_BASE_URL = "https://cc.auto-link.com.cn/pro/v1"
OPENAI_API_KEY  = "sk-…"     # 值取自同目录 auth.json，不新增秘密暴露面
```

**判据不是「文件里有这行」**，是「子进程真拿到了」。零 token 的实测（`codex sandbox`
跑命令时会套用同一套 env 策略，等价于 GUI 那条路）：

```bash
env -u OPENAI_API_KEY -u OPENAI_BASE_URL \
  codex sandbox -- /bin/sh -c 'echo "len=${#OPENAI_API_KEY} base=$OPENAI_BASE_URL"'
# 父进程已清空这两个变量，仍打印出 len=25 base=… 才算成立
```

`toolchain_doctor.py` 第 1 段就是这个，别手搓。

## 2. homebrew python 瓶子会整体崩（expat 错配）

**一个根，三种看着毫不相干的症状**：

| 你看到的 | 其实是 |
|---|---|
| `pip install <任何包>` → `Symbol not found: _XML_SetAllocTrackerActivationThreshold` | pyexpat dlopen 失败 |
| `uv pip install --python …` → `Broken Python installation, platform.mac_ver() returned an empty value` | 同上（mac_ver 走 plistlib→pyexpat） |
| openai SDK 发请求 → `ValueError: invalid literal for int() with base 10: ''` | truststore 拿 mac_ver 空串去 int() |

**根因**：python 配方是 `uses_from_macos "expat", since: :sequoia` ⇒ macOS 26 上链**系统**
expat、不自带。homebrew CI 烤瓶子那台机的系统 expat 是 **2.7.2+**（那个符号 2.7.2 才加），
本机只有 **2.7.1** ⇒ 一 import 就崩。

**修法**（`scripts/brew_rebuild_python.sh`，三个坑都埋在脚本里了）：源码重编，编出来就按本机
2.7.1 编。本机实测 3.14.6 → 3.14.7，**约 2.5 分钟**，`pipx`/`yt-dlp` 不受影响。
完事补一句 `python3 -m pip install --break-system-packages openai`。

坑三条，缺一个就卡：

1. `raw.githubusercontent.com` 在本机 **connect 挂住** ⇒ brew 拉配方**静默卡死**：没有任何
   编译进程、Cellar 时间戳不动、只有一个 0% CPU 的 curl。**别当成"编译很慢"干等。**
   正解是从镜像 clone core tap（tuna/aliyun），不是把 `.rb` 下到本地 —— 那会被
   `Homebrew requires formulae to be in a tap` 拒掉。
2. brew 6.x 起本地 tap 默认不受信，要 `brew trust homebrew/core`。
3. **`HOMEBREW_NO_SANDBOX=1`**，见 §3。

判**版本**别用错量具：
- ⛔ `/usr/lib/libexpat.1.dylib` **在磁盘上不存在**（在 dyld shared cache 里），`ls`/`nm`/`strings`
  全读不到。"文件没有"不是结论。
- ⛔ `python3.9 -c "import xml.parsers.expat; print(version_info)"` 报的是**编译期宏**，不是运行时库版本。
- ✅ 唯一能用的：`grep XML_M..._VERSION /Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk/usr/include/expat.h`

## 3. `cannot run C compiled programs` / 莫名 137

`137 = 128+SIGKILL` = 二进制**在 exec 瞬间被杀，根本没跑**。这台机上至少三种来源，长得一模一样。

```
scripts/exec_kill_triage.sh [可疑目录]     # 四条腿一次跑完
```

| 腿 | 测什么 | 本机 2026-09-10 实测 |
|---|---|---|
| A | 编译器/SDK 本身：干净目录 + 同一套 flag 编 hello world | 全 **42**（好的） |
| B | 路径被针对：换到可疑目录再编再跑 | `/Applications` 42；但 `/Applications/Codex.app` 下的 helper 是 137 |
| C | brew 构建沙箱：读 `~/Library/Logs/Homebrew/*/config.log` 找 `Killed: 9` | 命中 ⇒ **就是它** |
| D | 有没有能在 exec 期发 SIGKILL 的东西（只读） | 唯一 ES 扩展 = 火绒 `cn.huorong.HipsMain.hractmond` |

本机已定的两案，**形状相同来源不同，别互相套用结论**：

- brew configure 的 conftest 137 = **腿 C**。`HOMEBREW_NO_SANDBOX=1` 解决。
  证伪腿：同一条 `-isysroot MacOSX26.sdk` 命令在 `/tmp/cctest` 编的二进制退出码 42；
  手工造同名目录 `/private/tmp/pythonA3.14-testdir/Python-3.14.7/` 再编再跑也是 42
  ⇒ 既不是编译器坏，也不是目录名，只剩沙箱。**别去重装 CLT。**
- Codex GPU helper 137 = **腿 B**。换包名到 `/Applications/OpenAI Codex.app` 解决。
  见 memory `project_codex_desktop_app_path_sigkill_2026_09_10`。

⚠️ 腿 C 读的是**存档日志**，一次失败留下的 `config.log` 会永远亮红。脚本会打出该日志的
mtime —— 判"现在还死不死"只认新跑一次的日志。
⚠️ 腿 D 只是"存在"，**ES 扩展存在 ≠ 它干的**。要坐实必须停掉它再复测同一条腿，本机**没做过**。

## 4. 内置 `image_gen` 工具不可用 ⇒ agent 退化画 SVG

症状：Codex 说「当前环境没有可用的内置图像生成接口」，然后自作主张改画 SVG / ASCII 图。
**这跟 §1~§3 是另一条线**：环境和 python 全绿也照样这样。

`image_gen` 不是普通函数工具，是三个 **ExtensionItem** 之一（二进制里的映射：
`image_gen.generation` / `clock.sleep` / `web.search`）。是否注册，看模型目录：

| ExtensionItem | 目录里的开关 | 本机 |
|---|---|---|
| `web.search` | `supports_search_tool: True` | ✅ 现身为 `web__run` |
| `clock.sleep` | `experimental_supported_tools: ['clock']` | ✅ |
| `image_gen.generation` | **11 个模型、零个字段提到 image_gen** | ❌ 没注册 |

判据（都别省）：

```bash
# 全量工具清单（不要只 filter image_gen —— 拿不到全貌就看不出 web__run 在里面）
codex exec --skip-git-repo-check -C /tmp \
  '只做一件事：执行 text(JSON.stringify(ALL_TOOLS.map(t=>t.name))) 并原样贴出。'
# 目录里所有 image/tool 相关字段
codex debug models | python3 -c '...'   # 见 memory 里的一段
```

⛔ **`codex features list` 里 `image_generation stable true` 不是"能用"的判据** ——
特性开着、工具照样没注册，本机实测就是这个组合。
⛔ **换 `CODEX_HOME` 拉一份"官方目录"来做对照是脏量具** —— 如果你把同一把 API-key
`auth.json` 和同一个 `openai_base_url` 抄过去，两次读的是同一条路径下的同一份内置目录，
不构成对照。我 2026-09-10 就是这么误判过一次并当场撤回。

**根因未坐实**：本机 `codex doctor` 判 `auth mode = api_key`，二进制里确有一族 ChatGPT-only
的门，日志也在刷 `remote plugin bundle sync failed ... api key auth is not supported`；
**但没有任何证据把 image_gen 与 auth mode 直接连起来，别说成结论。**

### 绕法（已实测，推荐直接用）

> 🆕 **2026-09-10 起这条绕法已产品化，别再手工摆。**
> 一键脚本 `codex-oneclick/install-*.{command,ps1}` 第 6 步会自动装好，
> 铺给同事、改脚本、批量开 key 一律走 skill **`codex-oneclick-rollout`**。
> 本节只留原理，手工只在给自己的机器打补丁时用。

CLI 那条路是好的，障碍只是 imagegen skill 写着"默认内置工具、未经用户确认不许换 CLI"。
在 **`~/.codex/AGENTS.md`** 里加一条常驻授权即可（等价于 skill 要的"用户已确认 CLI 模式"），
要点：说清内置不可用、给出完整命令、指明 `/opt/homebrew/bin/python3`、
声明 key 已由 `config.toml` 注入（禁止向用户要 key）、禁止退化成 SVG。

⛔ **别让别人的机器去调 `.system/imagegen/scripts/image_gen.py`** —— 那个 `import openai`，
普通同事机器上没这个包（而且本机 pip 一度整个是坏的）。铺开用的是零依赖版
`~/.codex/carher_image.py`（只用标准库），由一键脚本安装。

⛔ **AGENTS.md 别无脑 `>>` 追加** —— 用户会重跑、也会自己往里写东西。
用 `<!-- CARHER-IMAGE-BEGIN/END -->` 标记块，先剥旧块再追加，判据 `grep -c BEGIN == 1`。

⛔ **别改 `~/.codex/skills/.system/imagegen/SKILL.md`** —— 该目录每次 codex 启动被重新解包覆盖
（实测整目录 mtime 跟着启动时间走），改了白改。

验收（真实用法）：`codex exec -C /tmp/imgtest '画一张「小鸡吃米」的插画'` ⇒ 直接 exec 调
`image_gen.py generate --model gpt-image-2`，没再问、没退化，35.4s 出 2.58MB PNG，**肉眼看过确实是小鸡啄米**。

## 5. 已证伪，别再捡回来

| 假设 | 证伪数据 |
|---|---|
| key 本来就传得下去，不用写 config.toml | 只在**终端**成立；GUI 启动不读任何 shell rc，实测子进程里没这个变量 |
| `cannot run C compiled programs` = 编译器/CLT 坏了 | 同 SDK 同 flag 在 /tmp 编的二进制退出码 42 |
| 137 是那个随机临时目录名触发的 | 手工造同名目录再编再跑，42 |
| 健康的 python 报 `cannot import name 'eq' from 'operator'` = python 也坏了 | 是 cwd 在 carher-admin 仓库，`operator/` 目录遮蔽了 stdlib；`cd /tmp` 即好 |
| 把 openai 包直接拷进坏 python 的 site-packages 能绕过去 | `import openai` 过了，但一发请求就在 `truststore/_macos.py` 撞同一个根 |
| 重编要等半小时 | 实测 2.5 分钟 |
| 拿 acct 池的 ChatGPT 凭证就能直连官方出图 API | `POST api.openai.com/v1/images/generations` → **401 `Missing scopes: api.model.images.request`**。订阅态 OAuth ≠ platform key，两套账。|
| 一个端点 scope 403 ⇒ 别的端点也不行 | **不成立**。`/v1/models` 要 `api.model.read`，出图要 `api.model.images.request`，是不同 scope。我 09-10 用前者推后者，被用户当场驳回。 |

## 6. 验收：只认用户的真实用法

不要拿 `import openai` 当验收——那不是用户的用法。跑真的：

```bash
cd /tmp
python3 ~/.codex/skills/.system/imagegen/scripts/image_gen.py generate \
  --model gpt-image-2 --prompt "..." --size 1024x1024 --out /tmp/acc.png
```

本机 2026-09-10 实测：**30.1s**，产出 1254×1254 / 1.29MB PNG。
`file` 确认是真 PNG，别只看"命令退出码 0"。

## 脚本

| 脚本 | 作用 | 退出码 |
|---|---|---|
| `scripts/toolchain_doctor.py` | 四段体检（env 注入 / python 健康 / openai SDK / 上游），主量具 | 0=全绿 |
| `scripts/brew_rebuild_python.sh` | 源码重编 homebrew python，三个坑已埋好；`DRY_RUN=1` 可空跑 | 0=重编并验收通过 |
| `scripts/exec_kill_triage.sh` | 137 / `cannot run C compiled programs` 四腿分诊 | 恒 0，看输出 |

三个脚本 2026-09-10 均在本机实跑通过。`toolchain_doctor.py` 另做过**反向对照**：
把一个必崩的假 `python3` 塞进 PATH，它确实翻红并给出正确修复提示、退出码 1
—— 一个只出过绿的量具不算量具。
`brew_rebuild_python.sh` 的重编命令体就是当天真正修好的那条（脚本形态只验了 `DRY_RUN`，
没有为了测试再把好 python 拆一遍）。

相关 skill：`codex-desktop-startup-diagnose`（同一台机，但那是**启动/发送卡顿**，别混）。
