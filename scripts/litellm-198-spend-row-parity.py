#!/usr/bin/env python3
"""两条车道并排：每个真实请求是否都落了 LiteLLM_SpendLogs 行、token 数是否非零（198 root 跑，读 DB + 打 pod IP）。

用途：改了 DB 连接池 / 下线日志类 callback（anthropic_passthrough_pingfix 的病就是「200 但 0 行 SpendLogs」）之后，
证明记账没丢。阴性对照：同样的请求打旧车道，行数应该一样。

    python3 litellm-198-spend-row-parity.py --lane-a litellm-proxy-gray --lane-b litellm-proxy --model <组名> [-n 12]

key：临时生成一把只开 --model 的短命 key（15 分钟、$1 预算），用完删除；明文只在内存里。
每条车道一把独立短命 key，回读 SpendLogs 按 api_key（sha256，有索引）计数——request_id 与 x-litellm-call-id 不是同一个值，按它匹配会全 0（10-20 实测假红）。
协议：/v1/messages 流式、/v1/chat/completions 流式、/v1/chat/completions 非流式，各 n/3 个。
退出码 0 = 两条车道都是 n/n 落行且 token>0。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid

NS = "litellm-product"
KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"


def kc(*a, stdin=None):
    r = subprocess.run(["kubectl", "-n", NS, *a], capture_output=True, text=True, input=stdin,
                       env=dict(os.environ, KUBECONFIG=KUBECONFIG))
    if r.returncode:
        sys.exit(f"kubectl {' '.join(a[:3])}: {r.stderr[:300]}")
    return r.stdout


def master_key():
    import base64
    raw = kc("get", "secret", "litellm-secrets", "-o", "jsonpath={.data.LITELLM_MASTER_KEY}")
    if not raw:
        sys.exit("no master key in secret litellm-secrets")
    return base64.b64decode(raw).decode()


def pod_ip(app):
    d = json.loads(kc("get", "po", "-l", f"app={app}", "-o", "json"))
    ips = [i["status"]["podIP"] for i in d["items"] if i["status"].get("phase") == "Running" and not i["metadata"].get("deletionTimestamp")]
    if not ips:
        sys.exit(f"no running pod for {app}")
    return ips


def http(url, body, key, timeout=180, stream=False):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        call_id = r.headers.get("x-litellm-call-id")
        raw = r.read().decode("utf-8", "replace")
    return r.status, call_id, raw


def psql(sql):
    return kc("exec", "-i", "litellm-db-0", "--", "psql", "-U", "litellm", "-d", "litellm", "-At", "-F", "|", "-c", sql)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lane-a", required=True)
    ap.add_argument("--lane-b", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("-n", type=int, default=12)
    a = ap.parse_args()
    mk = master_key()
    base0 = f"http://{pod_ip(a.lane_b)[0]}:4000"
    import hashlib
    keys = {}
    try:
        for lane in (a.lane_a, a.lane_b):
            _, _, raw = http(f"{base0}/key/generate", {"models": [a.model], "duration": "15m", "max_budget": 1.0,
                                                         "key_alias": f"spend-parity-{uuid.uuid4().hex[:6]}",
                                                         "metadata": {"purpose": "spend-row-parity probe"}}, mk, timeout=30)
            keys[lane] = json.loads(raw)["key"]
        sent = {}
        for lane in (a.lane_a, a.lane_b):
            ips = pod_ip(lane)
            out = []
            for i in range(a.n):
                base = f"http://{ips[i % len(ips)]}:4000"
                shape = ("messages-stream", "chat-stream", "chat")[i % 3]
                body = {"model": a.model, "max_tokens": 64,
                        "messages": [{"role": "user", "content": f"reply with the word ok. nonce {uuid.uuid4().hex[:10]}"}]}
                try:
                    if shape == "messages-stream":
                        st, _, _ = http(f"{base}/v1/messages", dict(body, stream=True), keys[lane])
                    else:
                        st, _, _ = http(f"{base}/v1/chat/completions", dict(body, stream=shape == "chat-stream"), keys[lane])
                except Exception as e:
                    st = getattr(e, "code", 0)
                out.append((shape, st))
            sent[lane] = out
        time.sleep(30)  # 批量写库周期（proxy_batch_write_at 默认 10s）留余量
        ok_all = True
        for lane, out in sent.items():
            h = hashlib.sha256(keys[lane].encode()).hexdigest()
            rows = psql(f"set statement_timeout='30s'; select call_type, count(*), count(*) filter (where prompt_tokens>0 and completion_tokens>0) "
                        f"from \"LiteLLM_SpendLogs\" where api_key='{h}' group by 1").splitlines()
            got = {}
            for line in rows:
                p = line.split("|")
                if len(p) == 3:
                    got[p[0]] = (int(p[1]), int(p[2]))
            n200 = sum(1 for _, s in out if s == 200)
            n_rows = sum(v[0] for v in got.values()); n_tok = sum(v[1] for v in got.values())
            good = n200 == len(out) and n_rows == len(out) and n_tok == len(out)
            ok_all &= good
            print(f"{lane:28s} http200={n200}/{len(out)} rows={n_rows} rows_with_tokens={n_tok} by_call_type={got} {'OK' if good else 'BAD'}")
        print("PASS" if ok_all else "FAIL")
        return 0 if ok_all else 1
    finally:
        for k in keys.values():
            try:
                http(f"{base0}/key/delete", {"keys": [k]}, mk, timeout=30)
            except Exception as e:
                print(f"WARN key delete failed: {type(e).__name__}", file=sys.stderr)

if __name__ == "__main__":
    sys.exit(main())
