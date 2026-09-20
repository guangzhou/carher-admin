#!/usr/bin/env python3
"""给 her 实例的 openclaw.runtime.json5 打上「xAI 视频」两块配置。

默认干跑：只写 <path>.vidnew + 打 diff，绝不碰原文件。--apply 才落地，且先备份。

两块配置：
  1) providers.xai —— baseUrl 指向 198 上的 xai-vidshim.service
  2) videoGenerationModel.primary + mediaGenerationAutoProviderFallback:false

为什么需要 shim（不是直连 xAI）：openclaw 不发 storage_options，xAI 就只返回短命的
ephemeral 相对地址；而 openclaw 的 xai provider 只读 video.url 这一个字段。shim
去程补 storage_options、回程把 file_output.public_url 搬到 video.url。

为什么 apiKey 用 ${CARHER_PROD_KEY}：openclaw 的 coerceSecretRef 只认 env 来源
（没有 file:），auth profile 存在 SQLite 里不可手写，paste-api-key 又被 $include
布局挡住 ⇒ 给实例发新 key 就得加 env var，加 env var 就得重建容器（生产动作）。
所以让实例拿它本来就有的 CARHER_PROD_KEY 当门票，shim 按 sha256 白名单换成
上游 key。纯配置改动 ⇒ openclaw 热加载，无需重建容器。

用法:
  python3 her-video-config-patch.py /Data/carher-runtime/deploy/carher-14/openclaw.runtime.json5
  python3 her-video-config-patch.py <path> --apply

SOP: .claude/skills/her-xai-video-generation/SKILL.md
"""
import argparse
import datetime
import os
import shutil
import subprocess
import sys

SHIM_BASE_URL = "https://cc.auto-link.com.cn/xaivid-9f4c2e7a1b/v1"
VIDEO_MODEL = "xai/grok-imagine-video-1.5"

XAI_ANCHOR = '    providers: {\n      "litellm": {'
XAI_BLOCK = '''    providers: {
      // xAI 视频：baseUrl 指向 198 上的 xai-vidshim.service —— 它去程补
      // storage_options（不补则 xAI 只返回短命 ephemeral 相对地址），回程把
      // file_output.public_url 搬到 video.url（openclaw 只读这一个字段）。
      // apiKey 用实例本来就有的 CARHER_PROD_KEY 当门票，shim 按 sha256 白名单
      // 换成 sub2api 的 key，故无需新增 env var、无需重建容器。
      "xai": {
        baseUrl: "%s",
        apiKey: "${CARHER_PROD_KEY}",
      },
      "litellm": {''' % SHIM_BASE_URL

VID_ANCHOR = '      imageGenerationModel: {'
VID_BLOCK = '''      videoGenerationModel: {
        primary: "%s",
      },
      // 关掉自动补别家 provider 的 defaultModel（会把请求和凭据发给 sora/veo）
      mediaGenerationAutoProviderFallback: false,
      imageGenerationModel: {''' % VIDEO_MODEL


def patch(text):
    """返回 (新文本, 说明)。已存在则原样返回。"""
    if SHIM_BASE_URL in text and "videoGenerationModel" in text:
        return text, "ALREADY_PRESENT"

    out = text
    notes = []

    if SHIM_BASE_URL not in out:
        n = out.count(XAI_ANCHOR)
        if n != 1:
            raise SystemExit("ANCHOR_MISS: providers 锚点命中 %d 次（要 1 次）" % n)
        out = out.replace(XAI_ANCHOR, XAI_BLOCK, 1)
        notes.append("providers.xai added")

    if "videoGenerationModel" not in out:
        n = out.count(VID_ANCHOR)
        if n != 1:
            raise SystemExit("ANCHOR_MISS: imageGenerationModel 锚点命中 %d 次（要 1 次）" % n)
        out = out.replace(VID_ANCHOR, VID_BLOCK, 1)
        notes.append("videoGenerationModel added")

    # 曾经踩过：经 ssh 多层引号传递时 \$ 把反斜杠带进了配置，
    # 变成 apiKey: "\${CARHER_PROD_KEY}"，openclaw 解析不出 env 引用。
    assert "\\${" not in out, "ESCAPED_DOLLAR: 配置里出现 \\${，env 引用会失效"

    # 图片那半必须原样保留：gpt 仍是 primary，x 只做 fallback。
    if "imageGenerationModel" in text:
        assert "imageGenerationModel" in out, "IMAGE_BLOCK_LOST"

    return out, "; ".join(notes) or "NOOP"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="openclaw.runtime.json5 绝对路径")
    ap.add_argument("--apply", action="store_true",
                    help="真正落地（默认干跑）。落地前先备份 <path>.bak-vidgen-<UTC>")
    args = ap.parse_args()

    if not os.path.isfile(args.path):
        raise SystemExit("NO_SUCH_FILE: %s" % args.path)

    with open(args.path, encoding="utf-8") as f:
        original = f.read()

    new, note = patch(original)
    print("[patch] %s" % note)

    if note == "ALREADY_PRESENT":
        print("ALREADY_PRESENT — 无需改动")
        return

    tmp = args.path + ".vidnew"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new)

    print("--- diff ---")
    subprocess.call(["diff", "-u", args.path, tmp])
    print("--- end diff ---")

    if not args.apply:
        print("PREPARED_NOT_APPLIED（要落地加 --apply）；预览文件 %s" % tmp)
        return

    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    backup = "%s.bak-vidgen-%s" % (args.path, stamp)
    shutil.copy2(args.path, backup)

    mode = os.stat(args.path).st_mode
    with open(args.path, "w", encoding="utf-8") as f:
        f.write(new)
    os.chmod(args.path, mode & 0o7777)
    os.remove(tmp)

    print("APPLIED")
    print("  备份: %s" % backup)
    print("  回滚: cp %s %s   # openclaw 热加载，无需重启容器" % (backup, args.path))


if __name__ == "__main__":
    sys.exit(main())
