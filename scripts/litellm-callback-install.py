#!/usr/bin/env python3
"""把一个回调模块挂上 198 的 litellm-proxy（带断言，可回滚）。

**必须在 198 上以 root 执行**（需要 kubectl + 集群凭据）::

    scp scripts/litellm-callback-install.py cltx@10.68.13.198:/tmp/
    scp k8s/litellm-callbacks/<mod>.py       cltx@10.68.13.198:/tmp/
    ssh cltx@10.68.13.198 'sudo python3 /tmp/litellm-callback-install.py install /tmp/<mod>.py'

三件事一次做完，每步都有断言：CM 加 key（并逐字节校验其余 key 未变）→
callbacks 列表加一行（定点插行 + 长度差 + 行级 diff 三重校验）→
rollout restart + status。

**还有一件本脚本不做的事**：Deployment 里的 subPath volumeMount。litellm 按
``/app/<module>.py`` 找文件，光加 CM key 会让新 pod 报
``Could not find module file`` 起不来（滚动更新会保住生产，老 pod 继续服务）::

    kubectl -n litellm-product patch deploy litellm-proxy --type json -p '[{"op":"add",
     "path":"/spec/template/spec/containers/0/volumeMounts/-","value":{
     "mountPath":"/app/<mod>.py","name":"callbacks","readOnly":true,"subPath":"<mod>.py"}}]'

``ANCHOR`` 决定插在哪 —— **顺序有实际后果**：改 ``input`` items 的回调必须排在
``chatgpt_responses_normalize`` 之前，否则它的 ``compaction_drop``
（``:151``）已经把 compaction item 删掉了（2026-08-07 实测）。

用法（在 198 上，需 root）::

    python3 /tmp/deploy_ccv2.py install /tmp/codex_compaction_v2.py
    python3 /tmp/deploy_ccv2.py rollback

做法上的取舍：
- CM 一律用 ``kubectl patch --type merge`` **只提交要改的那个 key**，不用
  apply/replace —— 后者会带上本地那份可能已漂移的全量，且撞
  resourceVersion。
- config.yaml 是个 47KB 的字符串，**不做 YAML 往返**（会丢注释、重排全文）。
  只做一次定点插行，并断言「新旧长度差 == 插入行长度」+「差异只有这一行」。
- 不删 Pod，只 rollout restart + rollout status（零中断规则）。
"""
import json
import os
import subprocess
import sys

NS = os.environ.get("LITELLM_NS", "litellm-product")
CB_CM = "litellm-callbacks"
CFG_CM = "litellm-config"

# 要装的模块。改这三行就能装别的回调。
KEY = os.environ.get("CB_MODULE", "codex_compaction_v2.py")
CALLBACK_LINE = "  - " + os.environ.get(
    "CB_ENTRY", "codex_compaction_v2.codex_compaction_v2")
# 插在锚点**之前**。见模块头关于顺序的说明。
ANCHOR = "  - " + os.environ.get(
    "CB_ANCHOR", "chatgpt_responses_normalize.chatgpt_responses_normalize")


def kubectl(*args, stdin=None):
    r = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                       input=stdin)
    if r.returncode != 0:
        raise SystemExit(f"kubectl {' '.join(args)} FAILED:\n{r.stderr[:800]}")
    return r.stdout


def get_cm(name):
    return json.loads(kubectl("get", "cm", name, "-o", "json"))


def patch_cm(name, data):
    payload = json.dumps({"data": data}, ensure_ascii=False)
    kubectl("patch", "cm", name, "--type", "merge", "--patch", payload)


def install(src_path):
    src = open(src_path, encoding="utf-8").read()

    # ── 1. 回调模块进 CM ──
    cm = get_cm(CB_CM)
    before_keys = sorted(cm["data"].keys())
    print(f"[1] {CB_CM} keys={len(before_keys)}")
    is_update = KEY in cm["data"]
    patch_cm(CB_CM, {KEY: src})
    cm2 = get_cm(CB_CM)
    after_keys = sorted(cm2["data"].keys())
    want = len(before_keys) if is_update else len(before_keys) + 1
    assert len(after_keys) == want, f"key 数异常 {len(after_keys)} != {want}"
    assert set(after_keys) - set(before_keys) <= {KEY}
    assert cm2["data"][KEY] == src, "写入内容与本地不一致"
    for k in before_keys:
        if k == KEY:
            continue
        assert cm2["data"][k] == cm["data"][k], f"动到了别的 key: {k}"
    print(f"    OK keys={len(after_keys)}，其余 {len(before_keys)} 个 key 逐字节未变")

    # ── 2. callbacks 列表加一行 ──
    cfg = get_cm(CFG_CM)
    old = cfg["data"]["config.yaml"]
    if CALLBACK_LINE in old:
        print("[2] callbacks 已包含该项，跳过")
    else:
        assert old.count(ANCHOR) == 1, f"锚点出现 {old.count(ANCHOR)} 次，拒绝改"
        new = old.replace(ANCHOR, CALLBACK_LINE + "\n" + ANCHOR, 1)
        assert len(new) - len(old) == len(CALLBACK_LINE) + 1, "长度差不符"
        d_old, d_new = old.split("\n"), new.split("\n")
        assert len(d_new) == len(d_old) + 1
        diff = [l for l in d_new if l not in d_old]
        assert diff == [CALLBACK_LINE], f"意外差异: {diff[:3]}"
        patch_cm(CFG_CM, {"config.yaml": new})
        back = get_cm(CFG_CM)["data"]["config.yaml"]
        assert back == new, "回读不一致"
        print(f"[2] callbacks +1（{len(d_old)} -> {len(d_new)} 行），其余全文未动")

    # ── 3. 滚动重启（显式写全 deploy 名，别用空参数）──
    print("[3] rollout restart deployment/litellm-proxy")
    kubectl("rollout", "restart", "deployment/litellm-proxy")
    print(kubectl("rollout", "status", "deployment/litellm-proxy", "--timeout=300s"))


def rollback():
    cm = get_cm(CB_CM)
    if KEY in cm["data"]:
        kubectl("patch", "cm", CB_CM, "--type", "json", "--patch",
                json.dumps([{"op": "remove", "path": "/data/" + KEY.replace("/", "~1")}]))
        print(f"[1] 已从 {CB_CM} 摘掉 {KEY}")
    cfg = get_cm(CFG_CM)
    old = cfg["data"]["config.yaml"]
    if CALLBACK_LINE in old:
        new = old.replace("\n" + CALLBACK_LINE, "", 1)
        assert len(old) - len(new) == len(CALLBACK_LINE) + 1
        patch_cm(CFG_CM, {"config.yaml": new})
        print("[2] 已从 callbacks 摘掉该项")
    kubectl("rollout", "restart", "deployment/litellm-proxy")
    print(kubectl("rollout", "status", "deployment/litellm-proxy", "--timeout=300s"))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "install":
        install(sys.argv[2])
    elif cmd == "rollback":
        rollback()
    else:
        raise SystemExit(__doc__)
