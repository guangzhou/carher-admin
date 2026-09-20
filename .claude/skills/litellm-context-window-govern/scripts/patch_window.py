#!/usr/bin/env python3
"""LiteLLM CM 上下文窗口外科式改写模板（在能 kubectl 的机器上跑，例如 226）。

两种模式：
  REPLACE — 目标行已存在 max_input_tokens，改右值（断言旧值 == 预期，防止改到不认识的行）
  INSERT  — 目标行没有该字段，在 model_info 的锚点行下新增（断言全文原本不含该字段）

强制四道门：备份+sha256 / 逐行断言 / YAML 结构门（除目标字段外逐字段相等）/ 回读比对。
dry-run 是默认，--apply 才写。

改之前把 CONFIG 段按本次任务改掉即可。历史用例见 SKILL.md §4。
"""
import subprocess, datetime, sys, json, yaml, difflib
from collections import Counter

# ======================= CONFIG =======================
NS = "carher"
CM = "litellm-config"          # 或 chatgpt-pool-config
MODE = "REPLACE"               # REPLACE | INSERT
FIELD = "max_input_tokens"
TARGET = 922000
# model_name 后缀 -> 期望旧值（REPLACE 模式用；INSERT 模式该值忽略）
RULES = [
    ("gpt-5.6-sol", 1000000), ("gpt-5.6-terra", 1000000), ("gpt-5.6-luna", 1000000),
    ("gpt-6-astra", 1050000),
]
EXPECT_CHANGED = 56            # 期望改动行数，对不上就停
ANCHOR = "mode: responses"     # INSERT 模式：在这一行下面插
EXTRA_INSERT = {}              # INSERT 模式可顺带插的别的字段，如 {"max_output_tokens": 128000}
EXPECT_MODEL_LIST_LEN = 138    # 结构门：条数必须不变
# ======================================================


def sh(c):
    r = subprocess.run(c, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        sys.exit(1)
    return r.stdout


cur = sh(f"kubectl -n {NS} get cm {CM} -o jsonpath='{{.data.config\\.yaml}}'")
ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
bak = f"/tmp/{CM}-{ts}.yaml"
open(bak, "w").write(cur)
print("backup:", bak, "sha256:", sh(f"sha256sum {bak}").split()[0])

lines = cur.split("\n")
out, cur_model, changed = [], None, []

if MODE == "INSERT":
    assert FIELD not in cur, f"CM 里已经有 {FIELD}，INSERT 模式前提不成立（该用 REPLACE）"

for i, ln in enumerate(lines):
    s = ln.strip()
    if s.startswith("- model_name:"):
        cur_model = s.split(":", 1)[1].strip()
    hit = [r for r in RULES if cur_model and cur_model.endswith(r[0])]

    if MODE == "REPLACE" and s.startswith(FIELD + ":") and hit:
        old = int(s.split(":", 1)[1].strip())
        assert old == hit[0][1], f"line {i+1} {cur_model}: 旧值 {old} != 预期 {hit[0][1]}"
        out.append(ln.replace(str(old), str(TARGET)))
        changed.append((i + 1, cur_model, old))
        continue

    out.append(ln)

    if MODE == "INSERT" and s == ANCHOR and hit:
        indent = " " * (len(ln) - len(ln.lstrip()))
        out.append(f"{indent}{FIELD}: {TARGET}")
        for k, v in EXTRA_INSERT.items():
            out.append(f"{indent}{k}: {v}")
        changed.append((i + 1, cur_model, None))

print("changed rows:", len(changed), Counter(m for _, m, _ in changed))
new = "\n".join(out)

d = list(difflib.unified_diff(lines, out, lineterm="", n=0))
adds = [x for x in d if x.startswith("+") and not x.startswith("+++")]
dels = [x for x in d if x.startswith("-") and not x.startswith("---")]
print(f"diff: +{len(adds)} -{len(dels)}")
if MODE == "REPLACE":
    assert len(lines) == len(out), "REPLACE 模式行数必须不变"
    assert len(adds) == len(dels) == len(changed) == EXPECT_CHANGED, (len(adds), len(dels), len(changed))
    # 注意 diff 行带 +/- 前缀，必须切掉再比
    assert all(a[1:].strip() == f"{FIELD}: {TARGET}" for a in adds)
else:
    assert len(dels) == 0, "INSERT 模式不许删任何行"
    assert len(changed) == EXPECT_CHANGED, len(changed)

a, b = yaml.safe_load(cur), yaml.safe_load(new)
assert list(a.keys()) == list(b.keys())
assert len(a["model_list"]) == len(b["model_list"]) == EXPECT_MODEL_LIST_LEN
for k in a:
    if k != "model_list":
        assert a[k] == b[k], f"顶层段 {k} 被动了"
touched = {FIELD, *EXTRA_INSERT}
for x, y in zip(a["model_list"], b["model_list"]):
    assert {k: v for k, v in x.items() if k != "model_info"} == \
           {k: v for k, v in y.items() if k != "model_info"}, x.get("model_name")
    xi, yi = dict(x.get("model_info") or {}), dict(y.get("model_info") or {})
    for f in touched:
        xi.pop(f, None)
        yi.pop(f, None)
    assert xi == yi, x.get("model_name")
print("structural gate: OK")

if "--apply" not in sys.argv:
    print("dry-run，未写入。加 --apply 生效。")
    sys.exit(0)

pf = f"/tmp/patch-{CM}-{ts}.json"
open(pf, "w").write(json.dumps({"data": {"config.yaml": new}}))
sh(f"kubectl -n {NS} patch cm {CM} --type=merge --patch-file {pf}")
back = sh(f"kubectl -n {NS} get cm {CM} -o jsonpath='{{.data.config\\.yaml}}'")
print("readback match:", back == new, f"| {FIELD}: {TARGET} count:", back.count(f"{FIELD}: {TARGET}"))
print("下一步：kubectl -n", NS, "rollout restart deploy/litellm-proxy （12~20min，progress deadline 属正常）",
      "然后逐副本跑 regress_window.py")
