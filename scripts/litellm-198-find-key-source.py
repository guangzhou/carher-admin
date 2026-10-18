"""按 sha256 在集群 CM/Secret 里找某把 key 的明文来源；明文只写 0600 文件，不打印。

用法（198 root，有 kubectl）：python3 litellm-198-find-key-source.py <key_sha256> <out_file>
  key_sha256 = LiteLLM_VerificationToken.token（库里存的就是 sha256）。用完 shred <out_file>。
  10-17 用它确认巡检号 key 在哪个 CM、杭州 key 不在 198 集群（只在杭州机器上）。
"""
import base64, hashlib, json, os, re, subprocess, sys
want, out = sys.argv[1], sys.argv[2]
def kc(*a):
    r = subprocess.run(["kubectl", *a], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""
for ns in kc("get", "ns", "-o", "jsonpath={.items[*].metadata.name}").split():
    for kind in ("configmap", "secret"):
        raw = kc("-n", ns, "get", kind, "-o", "json")
        if not raw: continue
        for it in json.loads(raw)["items"]:
            for k, v in (it.get("data") or {}).items():
                txt = v
                if kind == "secret":
                    try: txt = base64.b64decode(v).decode("utf-8", "ignore")
                    except Exception: continue
                for cand in set(re.findall(r"sk-[A-Za-z0-9._-]{8,}", txt)):
                    if hashlib.sha256(cand.encode()).hexdigest() == want:
                        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600); os.write(fd, cand.encode()); os.close(fd)
                        print("FOUND", ns, kind, it["metadata"]["name"], k); sys.exit(0)
print("NOT FOUND")
