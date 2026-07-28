#!/usr/bin/env python3
"""把一个文件加进 CM 的某个 key,保留其余全部 key。
教训:上一版用 shell 传 env 给内联 python 失败,导出目录是空的,
但 `kubectl create cm --from-file=<空目录>` 仍然成功 -> CM 从 9 key 塌成 1 key。
所以这里:导出后必须断言 key 数,不达标直接退出,绝不 apply。"""
import json, os, subprocess, sys, tempfile

NS = "litellm-product"
cm, key, src = sys.argv[1], sys.argv[2], sys.argv[3]

raw = subprocess.run(["kubectl", "get", "cm", "-n", NS, cm, "-o", "json"],
                     capture_output=True, text=True)
if raw.returncode != 0:
    sys.exit("读取 CM 失败: " + raw.stderr[:200])
data = json.loads(raw.stdout).get("data") or {}
before = len(data)
if before == 0:
    sys.exit("CM 是空的,拒绝操作")

d = tempfile.mkdtemp(prefix="cm_")
for k, v in data.items():
    open(os.path.join(d, k), "w").write(v)
written = len(os.listdir(d))
if written != before:
    sys.exit("导出不完整: %d/%d,拒绝 apply" % (written, before))

# 覆盖/新增目标 key
import shutil
shutil.copyfile(src, os.path.join(d, key))
expect = before + (0 if key in data else 1)
got = len(os.listdir(d))
if got != expect:
    sys.exit("目录 key 数异常: %d 期望 %d" % (got, expect))

y = subprocess.run(["kubectl", "create", "cm", cm, "-n", NS,
                    "--from-file=" + d, "--dry-run=client", "-o", "json"],
                   capture_output=True, text=True)
if y.returncode != 0:
    sys.exit("生成失败: " + y.stderr[:200])
# apply 前再校验一次生成物
newdata = json.loads(y.stdout).get("data") or {}
if len(newdata) != expect:
    sys.exit("生成物 key 数 %d 期望 %d,拒绝 apply" % (len(newdata), expect))

p = subprocess.run(["kubectl", "apply", "-f", "-"], input=y.stdout,
                   capture_output=True, text=True)
print("  %s: %d -> %d keys  %s" % (cm, before, len(newdata),
                                   (p.stdout or p.stderr).strip()[:60]))
