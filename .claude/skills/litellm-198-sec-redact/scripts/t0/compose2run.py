"""把 build_t0.py 生成的 docker-compose.yml 转成 docker run 命令（198 的 docker 没装 compose 插件）。"""
import shlex, sys, yaml
c = yaml.safe_load(open("docker-compose.yml"))["services"]
wd = "/home/cltx/t0-sec-redact"
net = "t0sec"
out = [f"docker rm -f t0sec-db t0sec-redis t0sec-mock t0sec-litellm >/dev/null 2>&1 || true",
       f"docker network create {net} >/dev/null 2>&1 || true"]
for name in ("db", "redis", "mock", "litellm"):
    s = c[name]
    a = ["docker", "run", "-d", "--name", f"t0sec-{name}", "--network", net, "--network-alias", name]
    for k, v in (s.get("environment") or {}).items():
        a += ["-e", f"{k}={v}"]
    for v in s.get("volumes") or []:
        src, dst, *mode = v.split(":")
        a += ["-v", f"{wd}/{src[2:] if src.startswith('./') else src}:{dst}:{mode[0] if mode else 'rw'}"]
    for p in s.get("ports") or []:
        a += ["-p", p]
    if s.get("entrypoint"):
        ep = s["entrypoint"]
        a += ["--entrypoint", ep[0]]
        a.append(s["image"])
        a += ep[1:]
    else:
        a.append(s["image"])
    a += s.get("command") or []
    out.append(" ".join(shlex.quote(x) for x in a))
open("run_t0.sh", "w").write("#!/bin/bash\nset -euo pipefail\n" + "\n".join(out) + "\n")
print("wrote run_t0.sh")
