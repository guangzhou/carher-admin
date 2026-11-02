#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CarHer 生图工具 — 由「Codex 一键配置」自动安装，用户无需关心。

只用 Python 标准库，不需要 pip 装任何东西。
key / base_url 从同目录 auth.json + config.toml 读取，不接受命令行传 key。

用法：
  python3 ~/.codex/carher_image.py "画面描述" [输出路径]

2026-10-13 起统一用 muse(用户点名「画图都用 muse 那个工具」)——11-02 起客户端写的是它的对外名 `gemini-image`,
后端还是组 `muse-image`,同一套图;两个名字都没开才退到别家生图。
muse-image 返回的是**相对路径** `/v1/media/<32hex>.webp`(文件在 muse2api 上,经网关拿不到),
198 nginx 加了只读出口 `https://<网关域名>/muse-media/<32hex>.<ext>` ⇒ 这里把相对路径改写过去下载。
输出文件后缀以**实际图片格式**为准(webp 就存 .webp),不把 webp 字节写进 .png 名字里骗看图软件。
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
# 11-02:别写死模型名。画图仍走 muse,变的只是客户端该写哪个名字:`gemini-image` 是后端组
# `muse-image` 的**对外公开名**(alias),同一套图;而能直接写 muse-image 的 key 只剩 3 把 ⇒ 写死就 403。
# 先挑 muse 的两个名字,都没开才退到别家生图。详见 carher_image.js 同处注释。
MODEL_ENV = os.environ.get("CARHER_IMAGE_MODEL", "")
MODEL_PREF = ["gemini-image", "muse-image", "gpt-image-2", "grok-imagine-image-quality",
              "grok-imagine-image-2.0", "grok-imagine", "gemini-3.1-flash-image"]


def pick_model(key, base):
    if MODEL_ENV:
        return MODEL_ENV
    ids = []
    try:
        req = urllib.request.Request(base + "/models", headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=30) as resp:
            ids = [m.get("id") for m in (json.loads(resp.read()).get("data") or []) if m.get("id")]
    except Exception:
        pass
    for m in MODEL_PREF:
        if m in ids:
            return m
    other = next((i for i in ids if ("image" in i or "imagine" in i) and "edit" not in i), None)
    if other:
        return other
    if ids:
        sys.exit("你的 key 没有开通任何画图模型，请联系运维开通（比如 gemini-image）。")
    return MODEL_PREF[0]
DEFAULT_SIZE = "1024x1024"


def read_credentials():
    """key 取自 auth.json，base_url 取自 config.toml —— 与一键脚本写入的位置一致。"""
    # 🔴 先读 auth.json(一键脚本写的那把),环境变量只作兜底:用户 shell 里常有一把**别的**
    #    OPENAI_API_KEY(10-13 本机实测:env 里那把没开 muse-image ⇒ 每次 403),
    #    Codex 本身用的是 auth.json 那把,画图要和它一致。
    key = ""
    try:
        with open(os.path.join(CODEX_HOME, "auth.json"), encoding="utf-8") as f:
            key = (json.load(f).get("OPENAI_API_KEY") or "").strip()
    except Exception:
        pass
    if not key:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("找不到 key。请重新运行一次「Codex 一键配置」脚本。")

    base = ""
    if True:
        try:
            with open(os.path.join(CODEX_HOME, "config.toml"), encoding="utf-8") as f:
                m = re.search(r'^\s*base_url\s*=\s*"([^"]+)"', f.read(), re.M)
            if m:
                base = m.group(1).strip()
        except Exception:
            pass
    if not base:
        base = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base:
        sys.exit("找不到网关地址。请重新运行一次「Codex 一键配置」脚本。")
    return key, base.rstrip("/")


def media_url(url, base):
    """相对路径 /v1/media/<hex>.<ext> → https://<网关根>/muse-media/<hex>.<ext>;绝对 URL 原样。"""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    m = re.match(r"^/v1/media/([0-9a-f]{32}\.(?:webp|png|jpe?g))$", url)
    if not m:
        sys.exit("生成失败：看不懂返回的图片地址 %s" % url[:120])
    root = re.match(r"^(https?://[^/]+)", base).group(1)
    return root + "/muse-media/" + m.group(1)


def download(url):
    last = None
    for _ in range(3):   # 文件刚生成时偶有一两秒还没落盘
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(2)
    sys.exit("图片已生成但下载失败：%s: %s" % (type(last).__name__, last))


def fix_ext(path, data):
    """按文件头认格式,后缀不对就改成对的(webp 存成 .png 会让不少看图软件打不开)。"""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        ext = ".webp"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = ".png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    else:
        return path
    root, cur = os.path.splitext(path)
    return path if cur.lower() in ((ext, ".jpeg") if ext == ".jpg" else (ext,)) else root + ext


def generate(prompt, out_path):
    key, base = read_credentials()
    model = pick_model(key, base)
    body = json.dumps({
        "model": model,
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
    print("正在生成，约需 30 秒…", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        if e.code == 403:
            sys.exit("你的 key 还没有开通画图模型 %s（这是按你的 key 能用的列表挑的）。请联系运维开通画图模型。" % model)
        if e.code == 401:
            sys.exit("key 已失效，请找运维重新颁发。")
        sys.exit("生成失败（HTTP %s）：%s" % (e.code, detail[:300]))
    except Exception as e:
        sys.exit("生成失败：%s: %s" % (type(e).__name__, e))

    item = (payload.get("data") or [{}])[0]
    if item.get("b64_json"):
        data = base64.b64decode(item["b64_json"])
    elif item.get("url"):
        data = download(media_url(item["url"], base))
    else:
        sys.exit("生成失败：返回里没有图片数据。")

    out_path = fix_ext(out_path, data)
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
    name = "image-%s.png" % time.strftime("%Y%m%d-%H%M%S")
    out = args[1] if len(args) > 1 else os.getcwd()
    # 🔴 10-13 本机回归:Codex 传了「.」当输出路径 ⇒ 原来拼成 `..webp`(隐藏文件,用户找不到)。
    #    给的是目录(含 . / 结尾带斜杠)⇒ 在目录里自动起名;没有后缀的普通名字照常补。
    if os.path.isdir(out) or out.endswith(("/", "\\")):
        os.makedirs(out, exist_ok=True)
        out = os.path.join(out, name)
    generate(prompt, out)   # 后缀按实际格式自动改(muse-image 出的是 .webp)


if __name__ == "__main__":
    main()
