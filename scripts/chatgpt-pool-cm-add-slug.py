#!/usr/bin/env python3
"""给 acct 池的 config.yaml ConfigMap 加一个 codex slug 条目（结构化插入，不是文本追加）。

为什么要单独一把工具：

- **acct pod 的 `/app/config.yaml` 是 subPath 挂载，改了 CM 不会进容器**，必须 rollout。
  所以这一步只管「把条目写对」，重启由调用方受控分波做（重启在服务的号可能永久打死它，
  见 feedback_restarting_a_serving_acct_can_kill_it_permanently）。
- **禁止文本追加。** CM 的 `data["config.yaml"]` 是一整段 YAML；用 `sed`/`>>` 拼字符串
  会在缩进和 `litellm_settings:` 这类尾部段落上悄悄破结构，而 `kubectl apply` 不报错。
  这里解析成 Python 对象 → 插进 `model_list` → 整段回写。
- **acct 池不止一份 config**（`chatgpt-pool-config` / `-135` / `-168`）。patch 主 CM
  是「覆盖大多数」不是「覆盖全池」，CM 名必须显式给，判据是 pod 内
  `grep -c model_name /app/config.yaml` 对不对得上。

用法：
    # dry-run（默认）：打印将要插入的条目和插入后的 model_name 清单
    python3 chatgpt-pool-cm-add-slug.py --cm chatgpt-pool-config --slug gpt-6.1-sol \
        --max-input-tokens 872000 --max-output-tokens 128000
    # 落盘
    ... --apply --backup ~/pool-cm-pre-<ts>.yaml
    # 撤销
    ... --slug gpt-6.1-sol --delete --apply

退出码：0 = 成功（含 dry-run）；2 = 条目已存在且字段不一致；3 = CM 读不到/结构不符。
"""
import argparse
import json
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("需要 PyYAML：pip3 install --user pyyaml")


def sh(cmd, inp=None, timeout=120):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       input=inp, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def kc(args, ns, sudo):
    return ("sudo -n kubectl " if sudo else "kubectl ") + "-n %s %s" % (ns, args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cm", required=True, help="ConfigMap 名，如 chatgpt-pool-config")
    ap.add_argument("--slug", required=True, help="上游 codex slug，如 gpt-6.1-sol")
    ap.add_argument("--model-name", default=None,
                    help="池内组名，默认 chatgpt-<slug>")
    ap.add_argument("--max-input-tokens", type=int, default=872000,
                    help="上游 max_context_window；⛔别填 10000000（那是把闸门关掉）")
    ap.add_argument("--max-output-tokens", type=int, default=128000)
    ap.add_argument("--ns", default="litellm-product")
    ap.add_argument("--key", default="config.yaml", help="CM 里的数据键名")
    ap.add_argument("--after", default=None,
                    help="插到这个 model_name 之后（默认追加到 model_list 末尾前的位置）")
    ap.add_argument("--delete", action="store_true", help="删除该条目而不是新增")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--backup", default=None, help="落盘前把整个 CM 存到这个文件")
    ap.add_argument("--no-sudo", action="store_true")
    a = ap.parse_args()
    sudo = not a.no_sudo
    name = a.model_name or ("chatgpt-" + a.slug)

    rc, out, err = sh(kc("get cm %s -o json" % a.cm, a.ns, sudo))
    if rc != 0:
        print("🔴 读不到 CM %s：%s" % (a.cm, err.strip()[:200]))
        return 3
    cm = json.loads(out)
    raw = (cm.get("data") or {}).get(a.key)
    if raw is None:
        print("🔴 CM %s 里没有 data[%r]（有的键：%s）"
              % (a.cm, a.key, list((cm.get("data") or {}).keys())))
        return 3
    doc = yaml.safe_load(raw)
    ml = doc.get("model_list")
    if not isinstance(ml, list):
        print("🔴 config.yaml 里 model_list 不是列表，结构不符，拒改")
        return 3

    names = [e.get("model_name") for e in ml]
    print("== 现有 %d 条：%s" % (len(ml), ", ".join(str(x) for x in names)))

    entry = {
        "model_name": name,
        "litellm_params": {"model": "chatgpt/%s" % a.slug},
        "model_info": {
            "mode": "responses",
            "max_input_tokens": a.max_input_tokens,
            "max_output_tokens": a.max_output_tokens,
        },
    }

    if a.delete:
        keep = [e for e in ml if e.get("model_name") != name]
        if len(keep) == len(ml):
            print("== %s 不在表里，无事可做" % name)
            return 0
        print("== 将删除 %s（%d → %d 条）" % (name, len(ml), len(keep)))
        doc["model_list"] = keep
    else:
        exist = [e for e in ml if e.get("model_name") == name]
        if exist:
            if exist[0] == entry:
                print("== %s 已存在且字段一致，无事可做" % name)
                return 0
            print("🔴 %s 已存在但字段不一致，拒绝覆盖。现有：\n%s\n期望：\n%s"
                  % (name, json.dumps(exist[0], ensure_ascii=False, indent=2),
                     json.dumps(entry, ensure_ascii=False, indent=2)))
            return 2
        idx = len(ml)
        if a.after:
            try:
                idx = names.index(a.after) + 1
            except ValueError:
                print("🔴 --after %s 不在表里" % a.after)
                return 3
        ml.insert(idx, entry)
        print("== 将插入到第 %d 位：\n%s" % (idx + 1,
                                        json.dumps(entry, ensure_ascii=False, indent=2)))

    new_raw = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print("== 插入后 %d 条：%s"
          % (len(doc["model_list"]), ", ".join(str(e.get("model_name")) for e in doc["model_list"])))

    if not a.apply:
        print("\n(dry-run，没写。加 --apply 落盘)")
        return 0

    if a.backup:
        with open(a.backup, "w") as f:
            f.write(out)
        print("backup -> %s (%d bytes)" % (a.backup, len(out)))

    patch = json.dumps({"data": {a.key: new_raw}})
    rc, o, e = sh(kc("patch cm %s --type merge -p %s"
                     % (a.cm, json.dumps(patch)), a.ns, sudo))
    if rc != 0:
        print("🔴 patch 失败：%s" % (e.strip()[:300]))
        return 3
    print(o.strip())

    # 回读校验：只认「解析后 model_name 集合里有它」，不认 patch 的退出码
    rc, out2, _ = sh(kc("get cm %s -o json" % a.cm, a.ns, sudo))
    back = yaml.safe_load(json.loads(out2)["data"][a.key])
    got = [e.get("model_name") for e in back.get("model_list", [])]
    hit = name in got
    want = not a.delete
    print("回读：%d 条，%s %s（期望 %s）"
          % (len(got), name, "在" if hit else "不在", "在" if want else "不在"))
    if hit != want:
        print("🔴 回读与期望不符")
        return 3
    print("\n⚠️  CM 改完了，但 /app/config.yaml 是 subPath 挂载 ⇒ **pod 里还是旧的**。"
          "要受控分波 rollout，判据是 pod 内 `grep -c model_name /app/config.yaml`。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
