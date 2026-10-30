#!/usr/bin/env python3
"""在 198 上搭 sec_redact 的全链 T0：照抄 gray 车道 Deployment 的镜像、全部 CM 挂载、callback 顺序，
只多一个 sec_redact.py（插在 callbacks 列表最后），上游换成本地 mock。

用法（198 上，sudo）：
    sudo python3 build_t0.py <gray-deploy.json> <sec_redact.py> [--shadow]
产物：/home/cltx/t0-sec-redact/{docker-compose.yml,config.yaml,mounts/...}
不碰集群对象：只 `kubectl get` 读 CM。
"""
import json
import os
import subprocess
import sys

NS = "litellm-product"
WD = "/home/cltx/t0-sec-redact"
ENV_KEEP_PREFIX = ("STREAMING_BRIDGE_", "FORCE_STREAM_", "BUDGET_", "CURSOR_G_", "DEAD_ARM_", "WEIGHTED_AFFINITY_",
                   "STORE_", "LITELLM_LOG", "AIOHTTP_", "CHATGPT_TOKEN_DIR", "PROXY_BATCH_POLLING_ENABLED")


def kget_cm(name):
    out = subprocess.run(["kubectl", "-n", NS, "get", "cm", name, "-o", "json"], capture_output=True, text=True,
                         check=True, env={**os.environ, "KUBECONFIG": "/etc/rancher/k3s/k3s.yaml"}).stdout
    return json.loads(out)["data"]


def main():
    deploy = json.load(open(sys.argv[1]))
    sec_path = sys.argv[2]
    shadow = "--shadow" in sys.argv
    spec = deploy["spec"]["template"]["spec"]
    c = spec["containers"][0]
    vols = {v["name"]: v for v in spec["volumes"]}
    os.makedirs(f"{WD}/mounts", exist_ok=True)

    cm_cache = {}
    mounts = []
    config_text = None
    for m in c["volumeMounts"]:
        v = vols[m["name"]]
        if "configMap" not in v:
            continue
        cm = v["configMap"]["name"]
        data = cm_cache.setdefault(cm, kget_cm(cm))
        if m["mountPath"] == "/app/config.yaml":
            config_text = data[m["subPath"]]
            continue
        if m.get("subPath"):
            local = f"mounts/{cm}/{m['subPath']}"
            os.makedirs(os.path.dirname(f"{WD}/{local}"), exist_ok=True)
            if m["subPath"] not in data:
                # 与集群行为一致：CM 里没有这个 key 时 kubelet 挂出一个空目录/空文件；callbacks 列表里也没引用它
                print(f"WARN: {cm} has no key {m['subPath']} (mounted at {m['mountPath']}); skipping like an absent file")
                continue
            open(f"{WD}/{local}", "w").write(data[m["subPath"]])
            mounts.append((local, m["mountPath"]))
        else:
            d = f"mounts/{cm}"
            os.makedirs(f"{WD}/{d}", exist_ok=True)
            for k, val in data.items():
                open(f"{WD}/{d}/{k}", "w").write(val)
            mounts.append((d, m["mountPath"]))
    assert config_text, "config.yaml mount not found"

    # 待验文件：车道上已挂着 sec_redact 时，替换那个挂载源；没挂就新增一个挂载
    open(f"{WD}/mounts/sec_redact.py", "w").write(open(sec_path).read())
    mounts = [(l, p) for (l, p) in mounts if p != "/app/sec_redact.py"]
    mounts.append(("mounts/sec_redact.py", "/app/sec_redact.py"))

    # config：保留 gray 的 litellm_settings / router_settings（去掉 redis 地址），model_list 换 mock
    import yaml
    cfg = yaml.safe_load(config_text)
    ls = cfg["litellm_settings"]
    cbs = list(ls["callbacks"])
    if "sec_redact.sec_redact" not in cbs:  # 车道已有则保留其原位置
        cbs.append("sec_redact.sec_redact")
    ls["callbacks"] = cbs
    rs = cfg.get("router_settings", {})
    rs["redis_host"], rs["redis_port"] = "redis", 6379
    gs = cfg.get("general_settings", {})
    gs.pop("database_url", None)
    gs["master_key"] = "os.environ/LITELLM_MASTER_KEY"
    mock = lambda name, model, base: {"model_name": name, "litellm_params": {
        "model": model, "api_base": base, "api_key": "sk-mock",
        "input_cost_per_token": 1e-6, "output_cost_per_token": 1e-6}}
    cfg["model_list"] = [mock("t0-chat", "openai/mock-gpt", "http://mock:9999/v1"),
                         mock("t0-claude", "anthropic/claude-mock", "http://mock:9999")]
    cfg.pop("search_tools", None)
    open(f"{WD}/config.yaml", "w").write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))

    env = {e["name"]: e["value"] for e in c.get("env", []) if "value" in e and e["name"].startswith(ENV_KEEP_PREFIX)}
    env.update({"DATABASE_URL": "postgresql://litellm:litellm@db:5432/litellm", "LITELLM_MASTER_KEY": "sk-t0master",
                "STORE_MODEL_IN_DB": "False"})
    env.pop("SEC_REDACT_SHADOW", None)  # T0 的模式由 t0_matrix.sh 控制，不继承车道的影子开关
    if shadow:
        env["SEC_REDACT_SHADOW"] = "1"

    vol_lines = "\n".join(f"      - ./{l}:{p}:ro" for l, p in mounts)
    env_lines = "\n".join(f"      {k}: {json.dumps(v)}" for k, v in sorted(env.items()))
    compose = f"""services:
  db:
    image: 127.0.0.1:5000/postgres:16-prod-71e27bf
    environment:
      POSTGRES_USER: litellm
      POSTGRES_PASSWORD: litellm
      POSTGRES_DB: litellm
  redis:
    image: 127.0.0.1:5000/redis:7-alpine-qualification
  mock:
    image: {c['image']}
    entrypoint: ["/app/.venv/bin/python3", "/mock.py"]
    volumes:
      - ./mock_upstream.py:/mock.py:ro
  litellm:
    image: {c['image']}
    depends_on: [db, redis, mock]
    ports: ["127.0.0.1:14100:4000"]
    entrypoint: {json.dumps(c.get('command') or [])}
    command: ["--config", "/app/config.yaml", "--port", "4000", "--num_workers", "1"]
    environment:
{env_lines}
    volumes:
      - ./config.yaml:/app/config.yaml:ro
{vol_lines}
"""
    open(f"{WD}/docker-compose.yml", "w").write(compose)
    print(f"T0 built: {len(mounts)} mounts, {len(cbs)} callbacks (last={cbs[-1]}), shadow={shadow}")


if __name__ == "__main__":
    main()
