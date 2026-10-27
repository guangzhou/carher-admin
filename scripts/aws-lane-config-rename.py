#!/usr/bin/env python3
"""198 三条车道（litellm-proxy / -gray / -guarded-old）config.yaml 里老 kiro 实例的 `kiro-*` 组改名。
2026-10-27 用户定「后台 kiro 统一改 aws，先改名不删」。在 198 上以 root 跑（kubectl 读写）。

  python3 aws-lane-config-rename.py plan     <deploy>
  python3 aws-lane-config-rename.py apply    <deploy>
  python3 aws-lane-config-rename.py rollback <deploy> <backup.json>

只做外科式文本替换（不 round-trip YAML，理由见 kiro-lane-add.py 文件头），每条 `kiro-*` 道改这几行：
  model_name: kiro-X            → model_name: aws-legacy-X
  （旧名 kiro-X 另由 router_settings.model_group_alias 隐藏别名接住 → aws-X，见 aws-legacy-yaml-alias 步骤）
  id: kiro-rs/...               → id: aws-legacy/...
  kiro_rate_multiplier:          → upstream_rate_multiplier:
  kiro_upstream_model_id:        → upstream_model_id:
  model: anthropic/kiro-...      → model: anthropic/...（gpt-5.6 三个改大写 GPT-，躲 LiteLLM OpenAI 同名表；
                                   kimi-k3 不撞表）。老实例镜像 ctxwin 的 map_model 先 to_lowercase，10-27 实测认大写名
api_base / api_key 不动（老实例 api_base 本来就是裸 IP、api_key 是 os.environ 引用）。

断言：改前后行数相等；变化行只落在上面五种形状；改后 config 里 `kiro` 只剩 api_key 的 env 名
`KIRO_RS_API_KEY`（env 变量名不对外，不改）；yaml 能 load，model_name 集合 = 旧集合把 kiro-* 换掉。

切换 = 新建 CM（复制旧 CM 全部 key，只换 config.yaml）→ 带 test op 的 JSON patch 换卷名 → rollout status。
RollingUpdate maxUnavailable=0，零中断。改前整份 Deployment 存 /root/cb-add-backup/<deploy>.cfg.<ts>.json。
"""
import hashlib, json, os, re, subprocess, sys, time
import yaml

NS = "litellm-product"
BACKUP_DIR = "/root/cb-add-backup"
OPENAI_CLASH = {"kiro-gpt-5.6-sol": "GPT-5.6-sol", "kiro-gpt-5.6-terra": "GPT-5.6-terra",
                "kiro-gpt-5.6-luna": "GPT-5.6-luna"}


def kc(*args, stdin=None):
    r = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True, input=stdin)
    if r.returncode:
        sys.exit(f"kubectl {' '.join(args[:3])} failed: {r.stderr.strip()[:400]}")
    return r.stdout


def get_json(*a):
    return json.loads(kc("get", *a, "-o", "json"))


def config_volume(spec):
    c = spec["containers"][0]
    mount = [m for m in c["volumeMounts"] if m["mountPath"] == "/app/config.yaml"]
    assert len(mount) == 1, mount
    vi = [i for i, v in enumerate(spec["volumes"]) if v["name"] == mount[0]["name"]]
    assert len(vi) == 1
    return vi[0], spec["volumes"][vi[0]]["configMap"]["name"], mount[0].get("subPath", "config.yaml")


def rename(text):
    out, changed = [], 0
    for line in text.split("\n"):
        new = line
        m = re.match(r"^(\s*-?\s*model_name:\s*)kiro-(\S+)\s*$", line)
        if m:
            new = f"{m.group(1)}aws-legacy-{m.group(2)}"
        m = re.match(r"^(\s*id:\s*)kiro-rs/(\S+)\s*$", new)
        if m:
            new = f"{m.group(1)}aws-legacy/{m.group(2).removeprefix('kiro-')}"
        new = re.sub(r"^(\s*)kiro_rate_multiplier:", r"\1upstream_rate_multiplier:", new)
        new = re.sub(r"^(\s*)kiro_upstream_model_id:", r"\1upstream_model_id:", new)
        m = re.match(r"^(\s*model:\s*anthropic/)(kiro-\S+)\s*$", new)
        if m:
            new = m.group(1) + OPENAI_CLASH.get(m.group(2), m.group(2)[len("kiro-"):])
        changed += new != line
        out.append(new)
    return "\n".join(out), changed


def check(old, new):
    a, b = old.split("\n"), new.split("\n")
    assert len(a) == len(b), "行数变了"
    for x, y in zip(a, b):
        if x != y:
            assert re.search(r"model_name:|id:|_rate_multiplier:|_upstream_model_id:|model:\s*anthropic/", x), x
    # 剩下允许的两种：api_key 的 env 变量名、callback 模块名（文件名，不进任何响应）
    left = [l for l in b if "kiro" in l.lower() and "KIRO_RS_API_KEY" not in l
            and not re.match(r"^\s*-\s*kiro_slots_identity\.kiro_slots_identity\s*$", l)]
    assert not left, left[:5]
    yo, yn = yaml.safe_load(old), yaml.safe_load(new)
    no = [m["model_name"] for m in yo["model_list"]]
    nn = [m["model_name"] for m in yn["model_list"]]
    exp = [("aws-legacy-" + n[len("kiro-"):]) if n.startswith("kiro-") else n for n in no]
    assert nn == exp, "model_name 集合不对"
    assert {k: v for k, v in yo.items() if k != "model_list"} == {k: v for k, v in yn.items() if k != "model_list"}
    return sum(n.startswith("kiro-") for n in no)


def main():
    cmd, deploy = sys.argv[1], sys.argv[2]
    d = get_json("deploy", deploy)
    spec = d["spec"]["template"]["spec"]
    if cmd == "rollback":
        old = json.load(open(sys.argv[3]))
        ops = [{"op": "replace", "path": "/spec/template/spec/volumes",
                "value": old["spec"]["template"]["spec"]["volumes"]}]
        kc("patch", "deploy", deploy, "--type", "json", "-p", json.dumps(ops))
        print(kc("rollout", "status", f"deploy/{deploy}", "--timeout=1500s").strip().splitlines()[-1])
        return
    assert d["spec"]["strategy"]["type"] == "RollingUpdate"
    assert d["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] in (0, "0")
    vi, old_cm, key = config_volume(spec)
    data = get_json("cm", old_cm)["data"]
    old = data[key]
    new, changed = rename(old)
    n = check(old, new)
    print(f"{deploy}: CM {old_cm} key={key} | kiro 组 {n} 个 | 改动行 {changed}")
    if n == 0:
        print("nothing to do"); return
    if cmd != "apply":
        for x, y in [(x, y) for x, y in zip(old.split("\n"), new.split("\n")) if x != y][:12]:
            print(f"  - {x.strip()}\n  + {y.strip()}")
        return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    bk = f"{BACKUP_DIR}/{deploy}.cfg.{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    json.dump(d, open(bk, "w"))
    nd = dict(data); nd[key] = new
    h = hashlib.sha256(json.dumps(nd, sort_keys=True).encode()).hexdigest()[:12]
    base = old_cm.rsplit("-", 1)[0] if len(old_cm.rsplit("-", 1)[-1]) == 12 else old_cm
    new_cm = f"{base}-{h}"
    kc("create", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": new_cm, "namespace": NS, "labels": {"carher.io/aws-rename-from": old_cm[:63]}},
        "data": nd}))
    assert get_json("cm", new_cm)["data"][key] == new
    ops = [{"op": "test", "path": f"/spec/template/spec/volumes/{vi}/configMap/name", "value": old_cm},
           {"op": "replace", "path": f"/spec/template/spec/volumes/{vi}/configMap/name", "value": new_cm}]
    kc("patch", "deploy", deploy, "--type", "json", "-p", json.dumps(ops))
    print(f"backup {bk}\npatch config {old_cm} -> {new_cm}")
    print(kc("rollout", "status", f"deploy/{deploy}", "--timeout=1500s").strip().splitlines()[-1])
    print(f"ROLLBACK: python3 {sys.argv[0]} rollback {deploy} {bk}")


main()
