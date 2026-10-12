# CarHer Codex 一键配置

给同事用的傻瓜化脚本：**输入你的 key，一键配好 Codex 接入 CarHer 网关**。
用系统自带的 shell / PowerShell 运行，基础配置不依赖任何环境；画图和模型列表同步需要 Python 3
（Mac 一般自带，没有也不影响基础使用）。**不需要设置环境变量，也不用重开终端**。

> 没有 codex 命令行（或 < 0.144）会**自动装最新版**：Node 装到 `~/.carher-node`、`@openai/codex` 走 npmmirror 装到 `~/.carher-npm-global`（不要管理员，不碰用户原来的 Node/codex；与 claude-oneclick 同一套）。装失败只警告不中断——配置照写，Desktop App 不依赖命令行。Desktop App 本身不由脚本装（安装包在飞书文档第 3 节）。

## 用哪个文件

每个系统**只有一个文件**，画图工具和模型同步都内嵌在里面。

| 你的系统 | 双击运行 |
|---------|---------|
| **macOS** | `install-mac.command` |
| **Windows** | `install-windows.bat` |
| Linux | 终端执行 `bash install-mac.command` |

## 维护者：改脚本

只改 `src/`（`install-mac.sh`、`install-windows.ps1`、两个 `.py`），然后 `bash build.sh`
（先跑 `tests/`，不过不出产物），生成上面两个单文件，再传到飞书文档替换附件
（传完下载回来，sha256 要等于 build.sh 末尾打印的值）。完整发版步骤见 skill `codex-oneclick-rollout` §1。
Windows 的 `.bat` 是 bat 头 + PowerShell 正文，cmd 执行到 `exit /b` 为止，后半段由 PowerShell 从文件自身读出执行。

## 操作步骤（30 秒）

1. 双击对应文件（Windows 用 `.bat`，Mac 用 `.command`）。
2. 粘贴运维发给你的 `sk-...` key，回车。
3. 看到「全部搞定」→ 终端里直接 `codex`（`codex --profile chatgpt` 也兼容），
   Desktop App 完全退出重开即可。

## 它做了什么

- 备份你原有的 `config.toml` / `auth.json` / `chatgpt.config.toml`
  （到 `~/.codex/backup-carher-<时间>/`，不会丢东西）。
- 写好 `config.toml`：裸 `codex`、Desktop App、`--profile chatgpt` 三种用法全通，
  模型固定 `gpt-5.6-terra`，并开启 WebSocket 增量传输（省流量、更快，异常自动回落 HTTP），
  同时允许沙箱内命令联网（否则画图工具连不上网关）。
- 写好 `chatgpt.config.toml`（新版 codex 的 profile 文件，兼容老命令）。
- 写好 `auth.json`（apikey 模式）。
- **开通画图**：装 `carher_image.py` 到 `~/.codex/`，并往 `~/.codex/AGENTS.md` 写一段常驻授权。
  装完直接跟 Codex 说「画一张…」即可，约 40 秒出真 PNG。
- **同步模型列表**：把这把 key 能用的全部模型写进 Codex 的模型选择器（CLI 的 `/model` 和 Desktop App 共用），每天自动刷新。
- 不写 env_key、不设环境变量：GUI App 读不到 shell 环境变量，env_key 缺变量会硬报错，
  这是以前"App 配置不生效"的常见根因。

## 关于画图

Codex 内置的 `image_gen` 工具在本环境**没有注册**（模型目录里没有任何模型声明该能力），
所以它遇到画图需求会自作主张退化成画 SVG。脚本的解法是绕过内置工具：
装一个零依赖小工具（只用 Python 标准库，**不需要 pip 装任何东西**）+ 一条常驻授权。

- `AGENTS.md` 用 `<!-- CARHER-IMAGE-BEGIN/END -->` 标记块管理，
  **重复运行只替换本块**，用户自己写在 `AGENTS.md` 里的内容不会被冲掉。
- 安装时会**实际执行一次** python 再采纳（macOS `/usr/bin/python3` 在没装开发者工具的机器上
  是个会弹窗的桩、Windows 微软商店那个也是假的 —— 只判"命令存在"会装出一个跑不了的配置）。
- 找不到可用 Python 时只跳过画图，其余配置照常生效。

## 关于模型选择器

`carher_models_sync.py`（第 7 步）把这把 key 在网关 `/v1/models` 上能看到的模型写进
`~/.codex/carher-models.json`，并在 `config.toml` 补 `model_catalog_json`。之后每天 10:17
和每次登录各跑一次（Mac=launchd `net.carher.codex-models-sync`，Windows=计划任务
`CarHer Codex Models Sync`）。日志 `~/.codex/carher-models-sync.log`。

- 模板取本机 codex 自带目录（`codex debug models --bundled`），字段跟随 codex 版本；
  新目录先让 codex 自己解析一遍，通不过不替换（目录写坏 codex 直接起不来）。
- 非 OpenAI 模型套 `gpt-5.5` 元数据，并钉死 `tool_mode=null`、`use_responses_lite=false`、
  `apply_patch_tool_type=null`；上下文按网关 `/model/info` 的 `max_input_tokens` 往下压。
  原因与 A/B 数据见 skill `codex-oneclick-rollout` §3。
- 生图模型和 `codex-auto-review` 不进选择器。

## 常见问题

- **macOS 双击提示「无法打开，来自身份不明的开发者」**：右键文件 → 打开 → 再点「打开」。
  或终端执行 `bash install-mac.command`。
- **macOS 双击没反应 / 权限**：浏览器下载的文件没有执行权限，属正常。终端执行 `bash ~/Downloads/install-mac.command`。
- **Windows 提示脚本被禁用**：双击 `.bat` 即可（已加 `-ExecutionPolicy Bypass`）。弹「Windows 已保护你的电脑」点 更多信息 → 仍要运行。
- **Desktop App 改完没生效**：必须**完全退出进程**再重开（Mac `Cmd+Q`，Windows 托盘右键 Quit）。
- **想换模型**：终端 `codex` 里输入 `/model`；临时用一次 `codex -m <模型名>`；Desktop App 点底部选择器。
- **选择器里只有几个 GPT**：模型同步没跑成，多半是没 Python。装 Python 3 后重跑脚本。日志 `~/.codex/carher-models-sync.log`。
- **画图报 403 / 说没权限**：你这把 key 还没开通画图，找运维加一下（模型名 `gpt-image-2`）。
- **Codex 还是画 SVG**：多半是装的时候没找到可用 Python（安装日志会有提示），
  或你手工改的是 `~/.codex/skills/.system/imagegen/` —— 那个目录每次启动会被重新解包覆盖，改了白改。

## 换电脑

直接在新机器上重跑一次本脚本即可（所有配置都由脚本生成，没有别的依赖）。
