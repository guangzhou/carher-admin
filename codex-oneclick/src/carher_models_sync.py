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
SKIP = re.compile(r"image|imagine|video|music|^codex-auto-review$")  # 出图/视频/音乐生成名不能对话
PREFERRED = "gpt-5.6-terra"  # 与一键脚本的默认模型一致


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


def codex_candidates() -> list[str]:
    """本机所有 codex 候选,按优先级。10-21 guozhenhua:先查到了老的 /opt/homebrew 版(没有 debug models)
    ⇒ 同步失败。⇒ 一键装的 ~/.carher-npm-global 排第一,load_templates 失败就换下一个。与 Node 版一致。"""
    out: list[str] = []

    def add(c):
        if c and Path(c).exists() and c not in out:
            out.append(c)

    for pre in (Path.home() / ".carher-npm-global" / "bin" / "codex",):
        add(str(pre))
    appdata = os.environ.get("APPDATA")
    if appdata:  # Windows npm 装的：优先真二进制，绕开 codex.cmd 这层 cmd 转义
        npm = Path(appdata) / "npm" / "node_modules" / "@openai"
        try:
            hit = next(npm.glob("codex*/**/codex.exe"), None)
        except OSError:
            hit = None
        if hit:
            add(str(hit))
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
        add(c)
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
                add(str(hit))
    return out


def run_codex(codex: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CODEX_HOME=str(CODEX_HOME))
    # codex 输出 UTF-8（含中文/符号）；不写 encoding 会按系统编码解，中文 Windows(GBK) 直接解码失败
    return subprocess.run([codex, "debug", "models", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60, env=env)


def load_templates(cands: list[str], current: Path) -> tuple[str | None, list[dict]]:
    """依次试候选,第一个能吐出自带目录的就用它(也用它校验新目录)。"""
    why = []
    for c in cands:
        r = run_codex(c, "--bundled")
        if r.returncode == 0:
            try:
                return c, json.loads(r.stdout)["models"]
            except (ValueError, KeyError):
                why.append(f"{c}:输出不是 JSON")
                continue
        why.append(f"{c}:{(r.stderr.strip().splitlines() or [''])[0][:120]}")
    if why:
        log(f"这些 codex 拿不到自带目录(版本太老?):{' | '.join(why)}")
    if current.exists():
        return None, json.loads(current.read_text(encoding="utf-8"))["models"]
    if cands:
        raise RuntimeError("本机的 codex 都太老(没有 debug models),也没有现有目录可当模板:重跑一次一键配置装新版")
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


def fix_default_model(text: str, shown: list[str], preferred: str, our_provider: str | None) -> dict | None:
    """config 里钉着的 model 已不在这把 key 里 ⇒ 每一轮都 403。换成 preferred(在的话)或列表第一个。
    只动顶层 `model = "…"`,且只在顶层 model_provider 是我们网关时才动。与 Node 版 fixDefaultModel 一致。"""
    first = re.search(r"^\[", text, re.M)
    head = text[: first.start()] if first else text
    m = re.search(r'^(\s*model\s*=\s*)"([^"]*)"', head, re.M)
    if not m or not shown or m.group(2) in shown:
        return None
    pv = re.search(r'^\s*model_provider\s*=\s*"([^"]+)"', head, re.M)
    if not pv or pv.group(1) != our_provider:
        return None
    nxt = preferred if preferred in shown else shown[0]
    return {"text": text[: m.start()] + m.group(1) + json.dumps(nxt) + text[m.end():], "from": m.group(2), "to": nxt}


def our_provider_name(text: str, base: str) -> str | None:
    """config.toml 里 base_url 等于这次同步所用网关的那个 [model_providers.X] 的名字 X。"""
    for m in re.finditer(r"^\[model_providers\.([^\]\s]+)\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S):
        bu = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', m.group(2), re.M)
        if bu and bu.group(1).rstrip("/") == str(base).rstrip("/"):
            return m.group(1)
    return None


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
    codex, templates = load_templates(codex_candidates(), target)
    catalog = build_catalog(ids, templates, fetch_input_limits(key, base_url))
    try:
        before = [m["slug"] for m in json.loads(target.read_text(encoding="utf-8"))["models"] if m.get("visibility") == "list"]
    except (OSError, ValueError, KeyError, TypeError):
        before = []

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
    added = [x for x in shown if x not in before]
    removed = [x for x in before if x not in shown]
    if before and added:
        log(f"新增 {len(added)}：{', '.join(added)}")
    if before and removed:
        log(f"删除 {len(removed)}：{', '.join(removed)}")
    if before and not added and not removed:
        log("和上次一样，没有变化")
    ours = our_provider_name(config_text, base_url)
    for f in (CONFIG, CODEX_HOME / "chatgpt.config.toml"):
        try:
            t = f.read_text(encoding="utf-8")
        except OSError:
            continue
        r = fix_default_model(t, shown, PREFERRED, ours)
        if r:
            f.write_text(r["text"], encoding="utf-8")
            log(f"默认模型 {r['from']} 你的 key 已不能用，改成 {r['to']}（{f.name}）")
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
