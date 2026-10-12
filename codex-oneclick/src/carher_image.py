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
