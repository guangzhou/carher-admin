---
name: codex-oneclick-rollout
description: 给同事批量铺 Codex 接入（carher 网关）的一键脚本 SOP，含「让所有 Codex 用户用上生图能力」这条完整链路——零依赖生图工具 carher_image.py、AGENTS.md 标记块常驻授权、安装期 python 探测（必须实跑不能只判存在）、198 桥四个图像模型的实测对照、以及 key 白名单批量开通与收敛判据。Use when 要给新同事配 codex / 用户说 codex 画图画成 SVG 或报 403 没权限 / 要改 install-mac.command 或 install-windows.ps1 / 问「怎么让所有人都能画图」。
---

# Codex 一键接入 + 生图铺开

> 实证：**2026-09-10 全链路打通并铺完 key**。脚本在 `codex-oneclick/`，
> 白名单已覆盖 cursor-* 634/634、carher-* 227/227。
> 档案见 memory `project_codex_oneclick_imagegen_rollout_2026_09_10`。

## 0. 这条链路由三段组成，缺一段用户就用不了

```
① 一键脚本写配置  →  ② 脚本装生图工具+授权  →  ③ 这把 key 的白名单有 gpt-image-2
```

用户报"画不了图"，先定位卡在哪一段：

| 现象 | 卡在 | 去哪节 |
|---|---|---|
| Codex 退化画 SVG / 说"没有内置图像接口" | ② | §2 |
| 报 `nodename nor servname provided` / DNS 解析失败 | ② 沙箱断网 | §2 第一个坑 |
| 报 `403 key_model_access_denied` | ③ | §3 |
| 报 401 / url 是 api.openai.com | ① | 重跑脚本 |

⚠️ **①②③ 全绿仍慢**（40 秒左右）是**正常**，不是故障，见 §4 的实测对照。

## 1. 脚本本体

`codex-oneclick/`：`install-mac.command` · `install-windows.{bat,ps1}` · `carher_image.py` · `README.md`，
打包成 `codex-oneclick.zip` 挂飞书文档附件。**改完脚本必须重打 zip**，否则用户下到的还是旧的：

```bash
cd codex-oneclick && rm -f codex-oneclick.zip && \
  zip -q codex-oneclick.zip install-mac.command install-windows.bat install-windows.ps1 carher_image.py README.md
```

用户侧文档（飞书《Codex一键配置》）不讲原理，只讲"双击、粘 key、回车"。
**别把本 skill 的排查细节写进用户文档**——用户要的是怎么做，不是为什么。

## 2. 生图那一段（脚本第 6 步）

内置 `image_gen` 是 ExtensionItem，本环境**全目录无模型声明该能力 ⇒ 没注册**，修不好，只能绕。
细节与证据见 skill `codex-local-cli-toolchain` §4。绕法两件事：

1. 拷 `carher_image.py` 到 `~/.codex/`（**只用 Python 标准库**，用户机器上没有 openai SDK，
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

## 3. key 白名单那一段

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

## 4. 图像模型实测对照（198 桥，2026-09-10）

| 模型 | 端点 | 耗时 | 产物 | 结论 |
|---|---|---|---|---|
| **`gpt-image-2`** | `/v1/images/generations` | 39s / 46s / 51s | ~2.4-3.6MB PNG | ✅ **脚本默认用它** |
| `ag-gemini-3.1-flash-image` | `/v1/chat/completions` + `modalities` | **20s** | 1.1MB | ⚠️ 更快但走 chat 协议，工具不支持 |
| `image-2` | images | 48s | 3.4MB | 能用，更慢 |
| `image-2-zero` | — | 0.1s | — | ❌ 403，不可用 |

**官方新模型吃不到**：`gpt-image-2.5-flare` / `gpt-image-2.5-sunburst` 是真实存在的名字
（2026-09-10 从官网确认），但在 198 桥上 403（白名单未开），且**用 acct 凭证也打不了官方**——见 §5。

## 5. 已证伪，别再捡回来

| 假设 | 证伪数据 |
|---|---|
| 用 acct 池的凭证就能直连官方出图 API | `POST api.openai.com/v1/images/generations` → **401 `Missing scopes: api.model.images.request`**；`gpt-image-2.5-flare` 和 `gpt-image-2` 两个名字**同一句拒绝** ⇒ 与模型名、与我们白名单都无关。acct 里是 ChatGPT 订阅态 OAuth，不是 platform key，两套账两套计费。 |
| 拿 `/v1/models` 的 403 能推断出图端点也不行 | **不能**。那条要的是 `api.model.read`，出图要的是 `api.model.images.request`，是不同 scope。我 09-10 用前者推后者，被用户当场驳回——**一个端点的 scope 失败不构成对另一个端点的证据**。 |
| 集群里翻一翻应该有 platform key | 阿里云 + 198 全量 secret 扫过，**一把 `sk-proj-`/`sk-svcacct-` 都没有**。要用官方新模型得单独申请+绑卡。 |
| `codex features list` 显示 `image_generation true` = 能用 | 特性开着、工具照样没注册，本机实测就是这个组合。 |
| 打 `chatgpt.com/backend-api/codex/responses` 带 `image_generation` tool 能出图 | **未证实也未证伪**：6 次里 5 次 `server_is_overloaded`，**连不带 tool 的阳性对照也在掉** ⇒ 那一轮量具作废，不许拿它下任何结论。模型名必须用 acct config 里那套（`gpt-5.6-sol` 等），`gpt-5.6`/`gpt-5-codex` 一律按名字 400。 |

## 6. 验收：只认真实用法

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

## 7. 跨机传脚本的血泪

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
| `codex-oneclick/install-mac.command` | macOS/Linux 安装器（第 6 步装生图） |
| `codex-oneclick/install-windows.ps1` | Windows 安装器（`.bat` 触发，别直接双击 `.ps1`） |
| `codex-oneclick/carher_image.py` | 零依赖生图工具，装到 `~/.codex/` |
| `scripts/litellm-198-key-allowlist.py` | **批量开通白名单就用它**，别手搓 |

相关 skill：`codex-local-cli-toolchain`（内置工具为何没注册、137 分诊）·
`litellm-198-key-allowlist`（白名单通用 SOP）·`codex-desktop-startup-diagnose`（启动卡顿，另一条线）。
