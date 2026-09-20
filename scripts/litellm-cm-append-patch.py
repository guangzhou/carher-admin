#!/usr/bin/env python3
"""litellm-cm-append-patch.py —— 往一个共用 ConfigMap 的**某一个 key 末尾追加**内容。

存在的理由：`litellm-callbacks` 这类 CM 里躺着 33 个各自独立的补丁文件，
「读出来 → 改 → 整体写回」这种常规做法一旦哪步出错就是**静默丢补丁**，
而丢了的那些文件不会报错，只会在几天后以"某个功能悄悄没了"的形式冒出来。
所以这个脚本只做一件事：**追加**，并且把"没动别的"做成机器断言而不是肉眼比对。

判据（每一条不过就 abort，绝不半途写入）：
  写前  new.startswith(old) 逐字节  ·  len(new) > len(old)  ·  ast.parse(new) 通过（.py key）
        目标 key 里不许已经含有 --marker 那个串（防重复追加）
  写后  key 集合完全不变  ·  其余每一个 key 逐字节不变  ·  目标 key == 预期的 new

⚠️ **subPath 挂载不热更新。** 改完必须 rollout restart 所有挂了这个 CM 的 Deployment，
   否则容器里还是旧文件，等于没改。脚本会把挂载者列出来提醒你。
   判"真的生效"只认容器内 `sha256sum <挂载路径>`，不认 CM 里的内容。

动了啥 / 备份在哪 / 怎么回滚：
  动了啥：只有 --key 这一个 key，只在末尾追加。
  备份：运行时自动把**整份 CM** 存到 --backup-dir（默认 ./cm-backup），文件名带时间戳，0600。
  回滚：`kubectl -n <ns> replace -f <那份备份>` 然后 rollout restart。

用法：
    # 预演（默认，不写入）
    ./litellm-cm-append-patch.py --ns litellm-product --cm litellm-callbacks \
        --key streaming_output_backfill.py --patch-file patches/xxx.py \
        --marker _install_nonblocking_responses_failure_handler

    # 真写
    ... --apply

远端执行（198 的 kubectl 要 sudo）：
    scp scripts/litellm-cm-append-patch.py cltx@10.68.13.198:~/
    ssh cltx@10.68.13.198 'python3 ~/litellm-cm-append-patch.py ... --kubectl "sudo kubectl"'
"""

import argparse
import ast
import datetime
import hashlib
import json
import os
import shlex
import subprocess
import sys


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kw).stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ns", required=True)
    ap.add_argument("--cm", required=True)
    ap.add_argument("--key", required=True, help="CM data 里的那个 key，例如 streaming_output_backfill.py")
    ap.add_argument("--patch-file", required=True, help="要追加的内容（本地文件）")
    ap.add_argument(
        "--marker",
        required=True,
        help="幂等标记：这个串若已出现在目标 key 里就拒绝再追加。取补丁里独一无二的函数名。",
    )
    ap.add_argument("--backup-dir", default="./cm-backup")
    ap.add_argument("--kubectl", default="kubectl", help='198 上要写 "sudo kubectl"')
    ap.add_argument("--apply", action="store_true", help="不加就是预演")
    a = ap.parse_args()

    kubectl = shlex.split(a.kubectl)

    def kc(*args):
        return sh([*kubectl, "-n", a.ns, *args])

    patch = open(a.patch_file, encoding="utf-8").read()
    if a.marker not in patch:
        sys.exit(f"❌ --marker {a.marker!r} 在补丁文件里都不存在，标记写错了")

    raw = kc("get", "cm", a.cm, "-o", "json")
    cur = json.loads(raw)
    data = cur.get("data") or {}
    keys_before = sorted(data)
    if a.key not in data:
        sys.exit(f"❌ CM {a.cm} 没有 key {a.key}。现有 {len(keys_before)} 个：{keys_before}")

    old = data[a.key]
    print(f"CM {a.cm} 共 {len(keys_before)} 个 key")
    print(f"目标 key {a.key}: 旧 {len(old)} 字节 sha256={hashlib.sha256(old.encode()).hexdigest()}")

    # ---- 幂等闸：已经追加过就退出，绝不重复叠 ----
    if a.marker in old:
        sys.exit(f"❌ 目标 key 里已经含有 {a.marker}，看起来打过了，拒绝重复追加")

    # ---- 备份整份 CM（不只目标 key）----
    os.makedirs(a.backup_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(a.backup_dir, f"{a.cm}.{ts}.json")
    fd = os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(raw)
    print(f"整份 CM 已备份 → {bak} (0600)")
    print(f"  回滚：{a.kubectl} -n {a.ns} replace -f {bak}  然后 rollout restart 所有挂载者")

    # ---- 组新内容 + 写前断言 ----
    new = old + ("" if old.endswith("\n") else "\n") + patch
    if not new.startswith(old):
        sys.exit("❌ 追加校验失败：新内容不是以旧内容逐字节开头")
    if len(new) <= len(old):
        sys.exit("❌ 新内容没变长，追加没发生")
    if a.key.endswith(".py"):
        ast.parse(new)  # 语法不过直接抛，不会走到写入
    print(f"新 {len(new)} 字节（+{len(new) - len(old)}）｜前缀逐字节一致 ✅｜ast.parse ✅")
    print(f"新 sha256={hashlib.sha256(new.encode()).hexdigest()}")

    # ---- 谁挂了这个 CM（爆炸半径）----
    print("\n挂载了这个 CM 的 Deployment（这些都要 rollout restart，否则不生效）：")
    deploys = json.loads(kc("get", "deploy", "-o", "json"))["items"]
    mounts = []
    for d in deploys:
        vols = d["spec"]["template"]["spec"].get("volumes") or []
        if any((v.get("configMap") or {}).get("name") == a.cm for v in vols):
            mounts.append(d["metadata"]["name"])
    for m in mounts:
        print(f"  - {m}")
    print(f"  共 {len(mounts)} 个。⚠️ 没重启的那些会在**下次自己重启时**静默拿到这个补丁。")

    if not a.apply:
        print("\n（预演，未写入。确认无误后加 --apply）")
        return

    body = json.dumps({"data": {a.key: new}})
    pf = os.path.join(a.backup_dir, f"patch-body.{ts}.json")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(body)
    sh([*kubectl, "-n", a.ns, "patch", "cm", a.cm, "--type=merge", "--patch-file", pf])

    # ---- 写后断言：其余 key 一个字节都不许变 ----
    back = json.loads(kc("get", "cm", a.cm, "-o", "json")).get("data") or {}
    if sorted(back) != keys_before:
        sys.exit(f"🔴 key 集合变了！before={keys_before}\nafter={sorted(back)}\n立刻用备份回滚")
    if back[a.key] != new:
        sys.exit("🔴 写回内容与预期不符，立刻用备份回滚")
    if not back[a.key].startswith(old):
        sys.exit("🔴 旧内容不再是前缀，立刻用备份回滚")
    for k in keys_before:
        if k != a.key and back[k] != data[k]:
            sys.exit(f"🔴 另一个 key 被改动了: {k}，立刻用备份回滚")

    print(f"\n✅ 写入成功。{len(keys_before)} 个 key 原样，仅 {a.key} 末尾 +{len(new) - len(old)} 字节")
    print("下一步（缺了等于没改）：")
    for m in mounts:
        print(f"  {a.kubectl} -n {a.ns} rollout restart deploy/{m}")
    print(f"验收只认容器内 sha256sum == {hashlib.sha256(new.encode()).hexdigest()}")


if __name__ == "__main__":
    main()
