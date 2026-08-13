#!/usr/bin/env python3
"""litellm-cm-patch-key.py — 只替换 ConfigMap 单个 data key 的安全变更工具

背景（两次事故教训）:
- 2026-08-10: kubectl replace 整包 CM 吃掉别人 8-02 的旧配置（feedback_tmp_fixed_path_runs_stale_foreign_file）
- 正解: merge-patch 只碰目标 key，其余 key 零接触；上传文件唯一命名 + md5 校验后才动手

用法:
  python3 litellm-cm-patch-key.py <src_file> <cm_key> <patch_out.json>
  # 然后人工执行（或加 --apply 自动执行，需 kubectl 上下文）:
  kubectl -n <ns> patch cm <cm-name> --type=merge --patch-file=<patch_out.json>
  # 变更后必须校验:
  kubectl -n <ns> get cm <cm-name> -o json | python3 -c \\
    "import json,sys,hashlib; d=json.load(sys.stdin); print(hashlib.md5(d['data']['<cm_key>'].encode()).hexdigest())"
  # ↑ 应与本脚本打印的 src md5 一致；pod 用 subPath 挂载的话需 rollout restart 才生效

沉淀自 2026-08-12/13 WA v3/v4 双集群四次上线（全部零事故）。
⚠ 远程多步操作纪律: 必须 && 串联 + 每步校验回显；scp 输出不许重定向 /dev/null
（2026-08-13 阿里云 v4 第一轮因 scp 静默失败 + 未串联白滚一次）。
"""
import hashlib
import json
import sys

if len(sys.argv) != 4:
    print(__doc__)
    sys.exit(2)

src, cm_key, out = sys.argv[1], sys.argv[2], sys.argv[3]
content = open(src, encoding="utf-8").read()
with open(out, "w", encoding="utf-8") as f:
    json.dump({"data": {cm_key: content}}, f, ensure_ascii=False)
print(f"patch written: {out}")
print(f"key           : {cm_key}")
print(f"content_bytes : {len(content)}")
print(f"src md5       : {hashlib.md5(content.encode()).hexdigest()}")
