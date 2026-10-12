#!/usr/bin/env bash
# CarHer Codex 一键配置 (macOS / Linux)
# 用法：Finder 里双击本文件，或终端里执行 `bash install-mac.command`
# 由 build.sh 生成，别直接改；源在 codex-oneclick/src/
# 没有 codex 命令行会自动装（npmmirror）；Desktop App 需用户自己装（文档里有安装包）
set -euo pipefail

BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'; NC=$'\033[0m'
say()  { printf '%s%s%s\n' "$BLUE" "$1" "$NC"; }
ok()   { printf '%s%s%s\n' "$GREEN" "$1" "$NC"; }
warn() { printf '%s%s%s\n' "$YELLOW" "$1" "$NC"; }
die()  { printf '%s%s%s\n' "$RED" "$1" "$NC" >&2; exit 1; }

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"

# 固定配置（用户无需感知）
BASE_URL="https://cc.auto-link.com.cn/pro/v1"
PROVIDER="carher_prod"
MODEL="gpt-5.6-terra"

echo
say "==================================================="
say "   CarHer Codex 一键配置 (macOS / Linux)"
say "==================================================="
echo

# --- 0. 没有 codex 命令行 / 太旧 ⇒ 自动装最新版 ---
# 和 Claude Code 一键脚本同一套做法：Node 装到 ~/.carher-node、npm 全局装到 ~/.carher-npm-global，
# 都走 npmmirror（不要 sudo、不碰系统、不碰用户原来的 Node/codex）。
# 装失败不阻断：配置照样写，Desktop App 不依赖命令行。
CODEX_MIN="0.144.0"
NPM_REGISTRY="https://registry.npmmirror.com"
NODE_MIRROR="https://cdn.npmmirror.com/binaries/node"
NODE_MAJOR_MIN=18      # @openai/codex 要求 node>=16；自己装时装 22 LTS
NODE_INSTALL_MAJOR=22
NODE_HOME="$HOME/.carher-node"
NPM_PREFIX="$HOME/.carher-npm-global"
[ -x "$NODE_HOME/bin/node" ] && export PATH="$NODE_HOME/bin:$PATH"
[ -d "$NPM_PREFIX/bin" ] && export PATH="$NPM_PREFIX/bin:$PATH"

ver_ge() {  # ver_ge A B ⇒ A >= B(纯数字点分)
  [ "$(printf '%s\n%s\n' "$1" "$2" | sort -t. -k1,1n -k2,2n -k3,3n | head -n1)" = "$2" ]
}
node_major() { node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0; }
cur_codex_ver() { codex --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n1; }

ensure_node() {
  if command -v node >/dev/null 2>&1 && [ "$(node_major)" -ge "$NODE_MAJOR_MIN" ]; then return 0; fi
  local os arch ext ver pkg tmp want got
  case "$(uname -s)" in Darwin) os=darwin; ext=tar.gz ;; Linux) os=linux; ext=tar.xz ;; *) return 1 ;; esac
  case "$(uname -m)" in x86_64|amd64) arch=x64 ;; arm64|aarch64) arch=arm64 ;; *) return 1 ;; esac
  if [ "$os" = darwin ] && [ "$arch" = x64 ] && [ "$(sysctl -n sysctl.proc_translated 2>/dev/null)" = 1 ]; then arch=arm64; fi
  say "→ 装一个 Node.js 到 $NODE_HOME(不需要管理员密码，不影响你原来的)"
  ver="$(curl -fsSL --max-time 30 "$NODE_MIRROR/index.json" \
    | grep -oE '"version":"v'"$NODE_INSTALL_MAJOR"'\.[0-9]+\.[0-9]+"[^}]*"lts":"[A-Za-z]+"' \
    | head -n1 | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')" || return 1
  [ -n "$ver" ] || return 1
  pkg="node-$ver-$os-$arch"; tmp="$(mktemp -d)"
  curl -fSL --max-time 600 --progress-bar -o "$tmp/$pkg.$ext" "$NODE_MIRROR/$ver/$pkg.$ext" || { rm -rf "$tmp"; return 1; }
  # 校验:用官方 SHASUMS256.txt 比对,下坏的/被篡改的包不装
  want="$(curl -fsSL --max-time 30 "$NODE_MIRROR/$ver/SHASUMS256.txt" | awk -v f="$pkg.$ext" '$2==f{print $1}')"
  if command -v shasum >/dev/null 2>&1; then got="$(shasum -a 256 "$tmp/$pkg.$ext" | awk '{print $1}')"
  else got="$(sha256sum "$tmp/$pkg.$ext" | awk '{print $1}')"; fi
  if [ -z "$want" ] || [ "$want" != "$got" ]; then rm -rf "$tmp"; warn "⚠ Node.js 安装包校验失败，已停止"; return 1; fi
  rm -rf "$NODE_HOME"; mkdir -p "$NODE_HOME"
  tar -xf "$tmp/$pkg.$ext" -C "$NODE_HOME" --strip-components=1 || { rm -rf "$tmp"; return 1; }
  rm -rf "$tmp"
  export PATH="$NODE_HOME/bin:$PATH"
  [ "$(node_major)" -ge "$NODE_MAJOR_MIN" ]
}

# 以后新开的终端也能直接敲 codex：只追加一个带标记的块,重跑先剥旧块,不碰用户别的内容
add_path_block() {
  local rc="$1" tmp; [ -e "$rc" ] || touch "$rc"
  tmp="$(mktemp)"
  awk '/# >>> carher-codex >>>/{s=1;next} /# <<< carher-codex <<</{s=0;next} !s' "$rc" > "$tmp"
  {
    cat "$tmp"
    echo '# >>> carher-codex >>>'
    [ -x "$NODE_HOME/bin/node" ] && echo "export PATH=\"$NODE_HOME/bin:\$PATH\""
    echo "export PATH=\"$NPM_PREFIX/bin:\$PATH\""
    echo '# <<< carher-codex <<<'
  } > "$rc"
  rm -f "$tmp"
}

CUR="$(cur_codex_ver || true)"
if [ -n "$CUR" ] && ver_ge "$CUR" "$CODEX_MIN"; then
  ok "✔ 检测到 codex 命令行 $CUR"
else
  if [ -n "$CUR" ]; then warn "⚠ codex $CUR 太旧(需要 ≥ $CODEX_MIN),自动升级 ..."
  else say "→ 没有 codex 命令行，自动安装(约 1~3 分钟)..."; fi
  if ensure_node; then
    mkdir -p "$NPM_PREFIX"
    export PATH="$NPM_PREFIX/bin:$PATH"
    if npm_config_prefix="$NPM_PREFIX" npm install -g @openai/codex --registry="$NPM_REGISTRY" \
         --no-fund --no-audit --update-notifier=false --loglevel=error; then
      hash -r 2>/dev/null || true
      for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
        # .bash_profile 只在它已存在时改(macOS 上新建它会让 .bashrc 不再被读)
        [ "$rc" = "$HOME/.bash_profile" ] && [ ! -e "$rc" ] && continue
        add_path_block "$rc"
      done
      CUR="$(cur_codex_ver || true)"
      [ -n "$CUR" ] && ok "✔ codex 命令行 $CUR 已装好" || warn "⚠ 装完了但找不到 codex 命令"
    else
      warn "⚠ 自动安装 codex 命令行失败（配置照样会写好，Desktop App 不受影响；命令行可稍后重跑本脚本）"
    fi
  else
    warn "⚠ 准备 Node.js 失败，跳过安装 codex 命令行（配置照样会写好，Desktop App 不受影响）"
  fi
fi
echo

# --- 1. 读 key ---
say "粘贴你的 API key（sk- 开头，运维发给你的那个），然后回车："
printf "key: "
read -r APIKEY || true
APIKEY="$(printf '%s' "${APIKEY:-}" | tr -d '[:space:]')"
[ -n "$APIKEY" ] || die "✗ 没有输入 key，已取消。"
case "$APIKEY" in
  sk-*) : ;;
  *) warn "⚠ key 不是 sk- 开头，仍然继续（如果是运维给的其它格式忽略此提醒）。" ;;
esac
echo

# --- 2. 备份已有配置 ---
mkdir -p "$CODEX_HOME"
TS="$(date +%Y%m%d-%H%M%S)"
BKDIR="$CODEX_HOME/backup-carher-$TS"
BACKED=0
for f in config.toml auth.json chatgpt.config.toml; do
  if [ -f "$CODEX_HOME/$f" ]; then
    mkdir -p "$BKDIR"; cp "$CODEX_HOME/$f" "$BKDIR/$f"; BACKED=1
  fi
done
[ "$BACKED" = "1" ] && warn "↳ 已备份原有配置到：$BKDIR"

# --- 3. 写 config.toml ---
# 顶层 model_provider 让裸 `codex` 和 Desktop App 都默认走 CarHer 网关（WS 增量同时生效）。
# 不用 env_key：GUI App 读不到 shell 环境变量，写了 env_key 而变量缺失会硬报错。
# 不用 [profiles.x]：codex ≥0.147 视为 legacy，与 --profile 组合会报错；改用 chatgpt.config.toml。
cat > "$CODEX_HOME/config.toml" <<EOF
# Generated by CarHer Codex 一键配置 — $TS
model = "$MODEL"
model_reasoning_effort = "medium"
model_provider = "$PROVIDER"
openai_base_url = "$BASE_URL"

# 画图工具要联网：codex exec 默认是只读沙箱，且 workspace-write 沙箱默认断网，
# 两个都不放开的话工具报 nodename nor servname provided。
# ⚠️ sandbox_mode 是顶层裸键，必须写在任何 [table] 之前，否则会被吃进那个表。
sandbox_mode = "workspace-write"

[model_providers.$PROVIDER]
name = "CarHer"
base_url = "$BASE_URL"
wire_api = "responses"
supports_websockets = true
experimental_bearer_token = "$APIKEY"

[sandbox_workspace_write]
network_access = true
EOF
chmod 600 "$CODEX_HOME/config.toml" 2>/dev/null || true
ok "✔ 已写 $CODEX_HOME/config.toml"

# --- 4. 写 chatgpt.config.toml（兼容老命令 codex --profile chatgpt） ---
cat > "$CODEX_HOME/chatgpt.config.toml" <<EOF
model = "$MODEL"
model_provider = "$PROVIDER"
EOF
ok "✔ 已写 $CODEX_HOME/chatgpt.config.toml"

# --- 5. 写 auth.json（apikey 模式，Desktop App 必需） ---
cat > "$CODEX_HOME/auth.json" <<EOF
{
  "auth_mode": "apikey",
  "OPENAI_API_KEY": "$APIKEY"
}
EOF
chmod 600 "$CODEX_HOME/auth.json" 2>/dev/null || true
ok "✔ 已写 $CODEX_HOME/auth.json"

# --- 6. 装生图能力 ---
# 内置画图工具在本环境未注册，Codex 会退化成画 SVG。装一个零依赖小工具 + 一条常驻授权，
# 让 Codex 需要画图时直接调它。工具只用 Python 标准库，不需要 pip 装任何东西。
# 两个 Python 小工具内嵌在本脚本里（build.sh 打包时填入），用户只需要这一个文件。
cat > "$CODEX_HOME/carher_image.py" <<'CARHER_PY_EOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CarHer 生图工具 — 由「Codex 一键配置」自动安装，用户无需关心。

只用 Python 标准库，不需要 pip 装任何东西。
key / base_url 从同目录 auth.json + config.toml 读取，不接受命令行传 key。

用法：
  python3 ~/.codex/carher_image.py "画面描述" [输出路径.png]
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_SIZE = "1024x1024"


def read_credentials():
    """key 取自 auth.json，base_url 取自 config.toml —— 与一键脚本写入的位置一致。"""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        try:
            with open(os.path.join(CODEX_HOME, "auth.json"), encoding="utf-8") as f:
                key = (json.load(f).get("OPENAI_API_KEY") or "").strip()
        except Exception:
            pass
    if not key:
        sys.exit("找不到 key。请重新运行一次「Codex 一键配置」脚本。")

    base = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base:
        try:
            with open(os.path.join(CODEX_HOME, "config.toml"), encoding="utf-8") as f:
                m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', f.read(), re.M)
            if m:
                base = m.group(1).strip()
        except Exception:
            pass
    if not base:
        sys.exit("找不到网关地址。请重新运行一次「Codex 一键配置」脚本。")
    return key, base.rstrip("/")


def generate(prompt, out_path):
    key, base = read_credentials()
    body = json.dumps({
        "model": DEFAULT_MODEL,
        "prompt": prompt,
        "size": DEFAULT_SIZE,
        "n": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        base + "/images/generations",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )

    started = time.time()
    print("正在生成，约需 40 秒…", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 403:
            sys.exit("你的 key 还没有开通生图权限，请联系运维开通后重试。")
        if e.code == 401:
            sys.exit("key 已失效，请找运维重新颁发。")
        sys.exit("生成失败（HTTP %s）：%s" % (e.code, detail[:300]))
    except Exception as e:
        sys.exit("生成失败：%s: %s" % (type(e).__name__, e))

    item = (payload.get("data") or [{}])[0]
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        with urllib.request.urlopen(item["url"], timeout=120) as r:
            data = r.read()
    else:
        sys.exit("生成失败：返回里没有图片数据。")

    with open(out_path, "wb") as f:
        f.write(data)
    print("已生成：%s（%.1f MB，用时 %.0f 秒）"
          % (out_path, len(data) / 1048576.0, time.time() - started))
    return out_path


def main():
    args = [a for a in sys.argv[1:] if a.strip()]
    if not args:
        sys.exit('用法：python3 %s "画面描述" [输出路径.png]' % sys.argv[0])
    prompt = args[0]
    out = args[1] if len(args) > 1 else os.path.join(
        os.getcwd(), "image-%s.png" % time.strftime("%Y%m%d-%H%M%S"))
    if not out.lower().endswith(".png"):
        out += ".png"
    generate(prompt, out)


if __name__ == "__main__":
    main()
CARHER_PY_EOF
cat > "$CODEX_HOME/carher_models_sync.py" <<'CARHER_PY_EOF'
#!/usr/bin/env python3
"""把这把 key 在 CarHer 网关上能用的全部模型同步进 Codex 的模型选择器。

只用 Python 标准库。key / base_url 从 CODEX_HOME 下的 auth.json + config.toml 读，
与一键脚本写入的位置一致。可反复运行（定时任务每天跑一次）。

流程：
  1. GET {base_url}/models —— 网关已按 key 的白名单过滤，列出来的就是能用的
  2. 模板取本机 codex 自带的目录（codex debug models --bundled），
     字段格式自动跟随用户的 codex 版本，避免 schema 漂移导致 codex 起不来
  3. 生成新目录 → 用 codex 自己解析一遍 → 通过才原子替换
  4. config.toml 里没有 model_catalog_json 就补一行

任何一步失败都不动现有文件：目录写坏了 codex 会直接启动失败。
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
CONFIG = CODEX_HOME / "config.toml"
DEFAULT_CATALOG = CODEX_HOME / "carher-models.json"
# 非 OpenAI 模型套 gpt-5.5 的元数据：它是普通函数调用。5.6/6 系的 tool_mode=code_mode_only +
# use_responses_lite 是 OpenAI 专属工具形状，套给 grok 实测拿不到 shell，一个文件都建不出来。
TEMPLATE_SLUG = "gpt-5.5"
# 不进选择器：生图模型不能对话；审查专用模型 codex 自己会调
SKIP = re.compile(r"image|^codex-auto-review$")


LOG_FILE = CODEX_HOME / "carher-models-sync.log"


def log(msg: str) -> None:
    line = f"[carher-models] {msg}"
    if sys.stdout is not None:  # pythonw（Windows 定时任务）没有 stdout
        print(line, flush=True)
    try:  # 定时任务里没人看终端，自己写日志，不依赖任务的输出重定向
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass


def read_credentials() -> tuple[str, str]:
    key = ""
    try:
        key = json.loads((CODEX_HOME / "auth.json").read_text(encoding="utf-8")).get("OPENAI_API_KEY", "")
    except (OSError, ValueError):
        pass
    m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', CONFIG.read_text(encoding="utf-8"), re.M)
    if not key or not m:
        raise RuntimeError("auth.json 里没有 key 或 config.toml 里没有 base_url，先跑一键配置脚本")
    return key, m.group(1).rstrip("/")


def fetch_model_ids(key: str, base_url: str) -> list[str]:
    req = urllib.request.Request(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp).get("data", [])
    return sorted({x["id"] for x in data if isinstance(x, dict) and x.get("id")}, key=str.casefold)


def fetch_input_limits(key: str, base_url: str) -> dict[str, int]:
    """网关 /model/info 里每个模型的输入上限（多条部署取最小）。拿不到就返回空，不影响同步。

    模板的 context_window 是 272000；claude-haiku 实际只有 200000，不压下来的话
    codex 会在超过 200k 后才想起压缩，请求先被上游拒掉。
    """
    req = urllib.request.Request(f"{base_url}/model/info", headers={"Authorization": f"Bearer {key}"})
    limits: dict[str, int] = {}
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            rows = json.load(resp).get("data", [])
    except Exception as exc:
        log(f"读不到模型上下文上限，沿用默认值：{exc}")
        return limits
    for row in rows:
        name = row.get("model_name") if isinstance(row, dict) else None
        cap = (row.get("model_info") or {}).get("max_input_tokens") if name else None
        if isinstance(cap, int) and cap > 0:
            limits[name] = min(cap, limits.get(name, cap))
    return limits


def find_codex() -> str | None:
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows npm 装的：优先真二进制，绕开 codex.cmd 这层 cmd 转义
        npm = Path(appdata) / "npm" / "node_modules" / "@openai"
        try:
            hit = next(npm.glob("codex*/**/codex.exe"), None)
        except OSError:
            hit = None
        if hit:
            return str(hit)
    candidates = [
        shutil.which("codex"),
        "/Applications/Codex.app/Contents/Resources/codex",
        "/Applications/OpenAI Codex.app/Contents/Resources/codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:  # Windows App 常见位置
        candidates += [str(Path(local) / "Programs" / "codex" / "codex.exe")]
    for c in candidates:
        if c and Path(c).exists():
            return c
    # Windows 只装了 Desktop App（msix）时，codex.exe 在 App 包目录里
    for root in (os.environ.get("ProgramFiles"), local):
        if not root:
            continue
        for pattern in ("WindowsApps/OpenAI.Codex*/**/codex.exe", "Programs/*odex*/**/codex.exe"):
            try:
                hit = next(Path(root).glob(pattern), None)
            except OSError:  # WindowsApps 目录可能没有列目录权限
                hit = None
            if hit:
                return str(hit)
    return None


def run_codex(codex: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CODEX_HOME=str(CODEX_HOME))
    # codex 输出 UTF-8（含中文/符号）；不写 encoding 会按系统编码解，中文 Windows(GBK) 直接解码失败
    return subprocess.run([codex, "debug", "models", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, env=env)


def load_templates(codex: str | None, current: Path) -> list[dict]:
    if codex:
        r = run_codex(codex, "--bundled")
        if r.returncode == 0:
            return json.loads(r.stdout)["models"]
        log(f"codex --bundled 失败，改用现有目录当模板：{r.stderr.strip()[:200]}")
    if current.exists():
        return json.loads(current.read_text(encoding="utf-8"))["models"]
    raise RuntimeError("找不到 codex，也没有现有目录可当模板")


def build_catalog(ids: list[str], templates: list[dict], limits: dict[str, int]) -> dict:
    by_slug = {m["slug"]: m for m in templates if m.get("slug")}
    base = by_slug.get(TEMPLATE_SLUG) or next(iter(by_slug.values()))
    wanted = [i for i in ids if not SKIP.search(i)]
    out: list[dict] = []
    # 官方自带的模型保留官方元数据，按官方顺序排在前面
    for slug, m in by_slug.items():
        m = copy.deepcopy(m)
        m["visibility"] = "list" if slug in wanted else "hide"
        out.append(m)
    for slug in wanted:
        if slug in by_slug:
            continue
        m = copy.deepcopy(base)
        m.update(slug=slug, display_name=slug, description=f"CarHer 网关模型：{slug}",
                 visibility="list", supported_in_api=True)
        m.update(tool_mode=None, use_responses_lite=False, multi_agent_version=None,
                 additional_speed_tiers=[], service_tiers=[])  # 模板版本变了也钉住
        # apply_patch 用普通 JSON 函数参数，不用 freeform：非 OpenAI 模型不会写裸 patch 语法。
        # 10-09 实测 freeform 下 kimi/composer/claude-sonnet-5/5.5 反复报
        # "first line of the patch must be '*** Begin Patch'"，sonnet-5.5 还会回 DONE 但文件没改；
        # 改 null 后 8 个模型改文件 0 报错、全部改对。
        m["apply_patch_tool_type"] = None
        m.pop("upgrade", None)
        m.pop("availability_nux", None)
        out.append(m)
    for priority, m in enumerate(out, start=1):
        m["priority"] = priority
        cap = limits.get(m["slug"])
        if cap:  # 只往下压，不放大
            for field in ("context_window", "max_context_window"):
                if isinstance(m.get(field), int) and m[field] > cap:
                    m[field] = cap
    return {"models": out}


def catalog_path_from_config(text: str) -> Path | None:
    m = re.search(r"""^\s*model_catalog_json\s*=\s*("(?:[^"\\]|\\.)*"|'[^']*')""", text, re.M)
    if not m:
        return None
    raw = m.group(1)
    value = json.loads(raw) if raw.startswith('"') else raw[1:-1]
    return Path(value).expanduser()


def toml_str(value: object) -> str:
    # json 字符串转义规则是 TOML 基本字符串的子集：反斜杠、引号、中文路径都安全
    return json.dumps(str(value), ensure_ascii=False)


def ensure_config_line(text: str, path: Path) -> str:
    line = f"model_catalog_json = {toml_str(path)}\n"
    first_table = re.search(r"^\[", text, re.M)  # 顶层键必须在任何 [table] 之前
    if first_table is None:
        return text.rstrip("\n") + "\n" + line
    return text[: first_table.start()] + line + "\n" + text[first_table.start():]


def main() -> int:
    key, base_url = read_credentials()
    ids = fetch_model_ids(key, base_url)
    if not ids:
        raise RuntimeError("网关没返回任何模型，保持现有目录不变")

    config_text = CONFIG.read_text(encoding="utf-8")
    target = catalog_path_from_config(config_text) or DEFAULT_CATALOG
    codex = find_codex()
    catalog = build_catalog(ids, load_templates(codex, target), fetch_input_limits(key, base_url))

    fd, tmp_name = tempfile.mkstemp(prefix=".carher-models-", suffix=".json", dir=str(CODEX_HOME))
    tmp = Path(tmp_name)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    try:
        if codex:  # 让 codex 自己解析一遍，通不过就不替换
            # 值不加引号：-c 的值解析 TOML 失败时按原样字符串用，路径天然不是合法 TOML。
            # 这样 Windows 走 codex.cmd 时也不用经过 cmd 的引号转义。
            r = run_codex(codex, "-c", f"model_catalog_json={tmp}")
            if r.returncode != 0:
                raise RuntimeError(f"codex 解析新目录失败，未替换：{r.stderr.strip()[:300]}")
        if target.exists():
            shutil.copy2(target, target.with_name(target.name + ".bak"))
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

    if catalog_path_from_config(config_text) is None:
        CONFIG.write_text(ensure_config_line(config_text, target), encoding="utf-8")
        log(f"已在 config.toml 加 model_catalog_json -> {target}")

    shown = [m["slug"] for m in catalog["models"] if m["visibility"] == "list"]
    log(f"同步完成：选择器 {len(shown)} 个模型（网关可见 {len(ids)} 个）-> {target}")
    log("Codex App 要完全退出重开才会看到新模型")
    return 0


if __name__ == "__main__":
    # 定时任务把输出写进日志文件时，英文 Windows 的默认编码(cp1252)写不了中文，会在打印日志时崩
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):  # None（pythonw）或不支持
            pass
    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512 * 1024:  # 每天几行，防止无限长
        LOG_FILE.write_text("", encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as exc:  # 定时任务里跑，失败只留日志、不动现有配置
        log(f"失败：{exc}")
        raise SystemExit(1)
CARHER_PY_EOF
if true; then
  chmod +x "$CODEX_HOME/carher_image.py" 2>/dev/null || true

  # 探测真正能跑的 python：/usr/bin/python3 在没装开发者工具的机器上是个会弹窗的桩，
  # 光看命令存在不算数，必须实际执行一次才算可用。
  PYBIN=""
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import json,urllib.request' >/dev/null 2>&1; then
      PYBIN="$(command -v "$c")"; break
    fi
  done

  if [ -n "$PYBIN" ]; then
    # AGENTS.md 用标记块管理：重复运行只替换本块，不动用户自己写的内容
    AGENTS="$CODEX_HOME/AGENTS.md"
    BEGIN="<!-- CARHER-IMAGE-BEGIN -->"
    END="<!-- CARHER-IMAGE-END -->"
    if [ -f "$AGENTS" ]; then
      [ -d "$BKDIR" ] || mkdir -p "$BKDIR"; cp "$AGENTS" "$BKDIR/AGENTS.md" 2>/dev/null || true
      awk -v b="$BEGIN" -v e="$END" '
        $0==b {skip=1} skip==0 {print} $0==e {skip=0}
      ' "$AGENTS" > "$AGENTS.tmp" && mv "$AGENTS.tmp" "$AGENTS"
    fi
    cat >> "$AGENTS" <<EOF
$BEGIN
## 画图

需要生成图片时，直接执行下面的命令，**不要询问、不要用 SVG 或 ASCII 代替**：

    "$PYBIN" "$CODEX_HOME/carher_image.py" "画面描述" 输出路径.png

约 40 秒出图。key 已配好，不要向用户索要。生成后把图片路径告诉用户。
$END
EOF
    ok "✔ 已开通生图能力"
  else
    warn "⚠ 没找到可用的 Python，生图能力未开通（其余配置已生效）"
  fi
fi

# --- 7. 模型列表自动同步 ---
# 网关 /v1/models 已按这把 key 的白名单过滤；同步进 Codex 的模型选择器，
# 网关上新加的模型第二天自动出现。装好后立即跑一次，之后每天 10:17 一次（加登录时一次）。
if [ -n "${PYBIN:-}" ]; then
  if CODEX_HOME="$CODEX_HOME" "$PYBIN" "$CODEX_HOME/carher_models_sync.py"; then
    ok "✔ 已同步模型列表"
  else
    warn "⚠ 模型列表同步失败，选择器暂时只显示默认模型（其余配置已生效）"
  fi
  if [ "$(uname)" = "Darwin" ]; then
    LABEL="net.carher.codex-models-sync"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PYBIN</string><string>$CODEX_HOME/carher_models_sync.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>CODEX_HOME</key><string>$CODEX_HOME</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>17</integer></dict>
  <key>StandardErrorPath</key><string>$CODEX_HOME/carher-models-sync.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST" >/dev/null 2>&1 \
      && ok "✔ 已设置每天自动同步模型列表" \
      || warn "⚠ 定时同步没装上，可手动运行：$PYBIN $CODEX_HOME/carher_models_sync.py"
  elif command -v crontab >/dev/null 2>&1; then
    ( crontab -l 2>/dev/null | grep -v carher_models_sync.py
      echo "17 10 * * * CODEX_HOME='$CODEX_HOME' '$PYBIN' '$CODEX_HOME/carher_models_sync.py' >/dev/null 2>&1"
    ) | crontab - && ok "✔ 已设置每天自动同步模型列表"
  fi
fi

echo
say "==================================================="
ok  "✅ 全部搞定！"
say "==================================================="
echo "下一步："
echo "  1) 终端用法：   新开一个终端窗口，直接运行 codex（codex --profile chatgpt 也兼容）"
echo "  2) Desktop App：完全退出(Cmd+Q)再重开即可"
echo
echo "顶部应显示 model: $MODEL"
echo "想画图直接跟 Codex 说「画一张…」即可。"
echo "想换模型（列表里都是你这把 key 能用的，每天自动更新）："
echo "  - 终端 codex：输入 /model 回车，上下键选，回车确认（会记住，下次默认用它）"
echo "  - 只这一次用某个模型：codex -m grok-4.7"
echo "  - Desktop App：点底部的模型选择器"
echo
