#!/usr/bin/env python3
"""从阿里云 litellm ConfigMap 里外科式摘掉 chatgpt-acct-N 的 entry。

    python3 aliyun-cm-drop-acct.py <in.yaml> <out.yaml> <acct> [acct...]

`aliyun-cm-add-acct.py` 的反向操作。**阿里云那边 `/model/delete` 无效**:arms
来自 ConfigMap 不来自 DB,24 个 arms 全返 400 `Model with id=... not found in
db` 且 readback 不变(2026-09-07 实证)。唯一通路是改 CM + rollout。

## 为什么是行区间切除而不是 YAML round-trip

只删目标行、其余字节原样保留,这样"没动别的地方"是**可证的**(见下面的
top-level key 逐 key 相等校验)。round-trip 会重排 key、丢注释/anchor,
diff 里全是噪声,出事时无法二分。

## ⚠️ 切块检测器不能认 `- model_name:`

`litellm-config-canary` 里有一批 entry 是 `litellm_params` 打头的。按"首键是
model_name"匹配,在 canary 上只找到 **14/75** 条 —— 于是只摘掉一部分,写出一份
半对的 config。必须按 `model_list:` 下的**列表缩进**切块,与首键无关。
(当时是"残留引用"那道 guard 拦住的,不是我先想到的 —— 所以 guard 一条都不能省。)

## 五道 guard,任何一道不过就 abort 而不是写出半对的文件

  * 命中条数为 0
  * 被删块里定义的 YAML anchor(`&x`)在保留部分仍被 `*x` 引用
  * 保留部分仍有目标 acct 的任何引用
  * `yaml.safe_load` 后 `model_list` 的条数差 != 被删块数
  * 除 `model_list` 外任何 top-level key 不相等

## 写回后还必须做的事(脚本不做,人做)

  1. 检查没有 model 组被清空(组数 7→5 这种可以,→0 不行)。
  2. `kubectl create cm ... --dry-run=client -o yaml | kubectl apply -f -`
     前先存原 CM 备份并记 sha256。
  3. `kubectl rollout restart deploy/litellm-proxy` + canary。
     ⚠️ **rollout 会卡住**:`hostNetwork: True` + `maxSurge:0/maxUnavailable:1`
     + nodeAffinity 只钉 3 台 ⇒ 新 pod `FailedScheduling ... didn't have free
     ports for the requested pod ports`,得等滚动腾出宿主端口。**别当失败去回滚。**
  4. 逐 pod 验容器内 `/app/config.yaml` 的 sha == 预期 sha,且 `/model/info`
     里目标 acct 的 arms == 0。
  5. 真实流量回归:剩余健康腿有 `POST /responses 200`、全池 401 == 0。
"""
import re
import sys

try:
    import yaml
except ImportError:
    yaml = None


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__.strip().splitlines()[2].strip())
    src, out = sys.argv[1], sys.argv[2]
    targets = [int(x) for x in sys.argv[3:]]
    pat = re.compile(r"chatgpt-acct-(%s)\b" % "|".join(str(t) for t in targets))

    lines = open(src).read().splitlines(keepends=True)

    ml_idx = next((i for i, l in enumerate(lines)
                   if re.match(r"^(\s*)model_list:\s*$", l)), None)
    if ml_idx is None:
        sys.exit("FATAL: no model_list: key found")
    ml_indent = len(re.match(r"^(\s*)", lines[ml_idx]).group(1))

    # 按列表缩进切块 —— 与 entry 的首键是什么无关
    item_re = None
    starts = []
    end = len(lines)
    for i in range(ml_idx + 1, len(lines)):
        l = lines[i]
        if not l.strip():
            continue
        ind = len(re.match(r"^(\s*)", l).group(1))
        if item_re is None:
            if not re.match(r"^\s*-\s", l):
                sys.exit("FATAL: first model_list child is not a list item: %r" % l)
            item_re = re.compile(r"^%s-\s" % (" " * ind))
        if item_re.match(l):
            starts.append(i)
            continue
        if ind <= ml_indent:          # 下一个 top-level key -> model_list 块结束
            end = i
            break
    if not starts:
        sys.exit("FATAL: model_list has no items")

    bounds = [(i, starts[k + 1] if k + 1 < len(starts) else end)
              for k, i in enumerate(starts)]
    drop = [(i, j) for (i, j) in bounds if pat.search("".join(lines[i:j]))]

    print("total model_list entries : %d" % len(bounds))
    print("entries matching targets : %d" % len(drop))
    if not drop:
        sys.exit("FATAL: 0 条命中 —— 目标号本来就不在这份 CM 里,还是检测器又瞎了?")
    for i, j in drop:
        blob = "".join(lines[i:j])
        nm = re.search(r"model_name:\s*(\S+)", blob)
        idm = re.search(r"id:\s*(\S+)", blob)
        print("  lines %5d-%-5d  group=%-24s id=%s" % (
            i + 1, j, nm.group(1) if nm else "?", idm.group(1) if idm else "?"))

    dropped_text = "".join("".join(lines[i:j]) for i, j in drop)
    kept_text = "".join(l for k, l in enumerate(lines)
                        if not any(i <= k < j for i, j in drop))

    for anchor in set(re.findall(r"&([A-Za-z0-9_.-]+)", dropped_text)):
        if re.search(r"\*%s\b" % re.escape(anchor), kept_text):
            sys.exit("FATAL: 被删块定义的 anchor &%s 在保留部分仍被引用" % anchor)

    if pat.search(kept_text):
        sys.exit("FATAL: model_list 之外仍有目标引用: %s"
                 % sorted(set(pat.findall(kept_text))))

    if yaml:
        a = yaml.safe_load("".join(lines))
        b = yaml.safe_load(kept_text)
        na, nb = len(a.get("model_list") or []), len(b.get("model_list") or [])
        print("model_list: %d -> %d (expected -%d)" % (na, nb, len(drop)))
        if na - nb != len(drop):
            sys.exit("FATAL: model_list delta %d != %d" % (na - nb, len(drop)))
        for key in set(a) | set(b):
            if key != "model_list" and a.get(key) != b.get(key):
                sys.exit("FATAL: non-model_list key changed: %s" % key)
        print("其余 top-level key 全部相等: %s" % sorted(set(a) - {"model_list"}))
    else:
        print("WARN: 没有 pyyaml,跳过了解析校验 —— 这份输出不算验过")

    open(out, "w").write(kept_text)
    print("wrote %s  (%d -> %d lines)" % (out, len(lines), len(kept_text.splitlines())))
    print("\n别忘了:备份记 sha -> apply -> rollout(会卡 free ports,别回滚)"
          " -> 逐 pod 验 sha + arms==0 -> 真实流量回归。")


if __name__ == "__main__":
    main()
