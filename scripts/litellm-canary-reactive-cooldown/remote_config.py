#!/usr/bin/env python3
# Runs on 198 (AIYJY-litellm). Don't invoke directly from Mac.
# Driver: scripts/litellm-canary-reactive-cooldown-config.py

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import time

import yaml

NS = "litellm-product"
PROD_CM = "litellm-config"
CANARY_CM = "litellm-config-canary"
CANARY_DEPLOY = "litellm-proxy-canary"
CANARY_SVC = "litellm-proxy-canary"
CANARY_MASTER_KEY_SECRET = "litellm-canary-master-key"
CANARY_IMAGE = "127.0.0.1:5000/litellm-carher:vanilla-v1.89.4.capacity-20260626-205435"
CANARY_ACCTS = [49, 68]


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, check=check, **kw)


def sh_ok(cmd):
    return sh(cmd, check=False)


def stamp(msg):
    print("[" + time.strftime("%H:%M:%S") + "] " + msg, flush=True)


def get_prod_master_key():
    r = sh(["kubectl", "-n", NS, "get", "secret", "chatgpt-pool-master-key",
            "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"])
    return base64.b64decode(r.stdout).decode()


def get_prod_cm():
    r = sh(["kubectl", "-n", NS, "get", "cm", PROD_CM,
            "-o", "jsonpath={.data.config\\.yaml}"])
    return yaml.safe_load(r.stdout)


def build_canary_cm_yaml(prod_cfg, pool_master_key):
    canary_cfg = dict(prod_cfg)
    canary_models = []
    for acct in CANARY_ACCTS:
        # Downstream sub-proxy (chatgpt-acct-N.svc) has model_name=chatgpt-gpt-5.5;
        # we forward as `openai/chatgpt-gpt-5.5` so sub-proxy resolves it.
        # Router-side deployment_id stays canary-prefixed → Redis cooldown
        # key `deployment:chatgpt-acct-canary-N:cooldown` won't collide with prod.
        canary_models.append({
            "model_name": "chatgpt-canary-gpt-5.5",
            "litellm_params": {
                "model": "openai/chatgpt-gpt-5.5",
                "api_base": "http://chatgpt-acct-" + str(acct) + "." + NS + ".svc.cluster.local:4000/v1",
                "api_key": pool_master_key,
            },
            "model_info": {
                "id": "chatgpt-acct-canary-" + str(acct),
                "mode": "responses",
            },
        })
    canary_cfg["model_list"] = canary_models

    rs = dict(prod_cfg.get("router_settings", {}))
    # v1.89.4 cooldown 决策：
    #   _is_allowed_fails_set_on_router=True (allowed_fails 显式设过) → 走 v1 legacy:
    #     should_cooldown_based_on_allowed_fails_policy 要 `updated_fails > allowed_fails`
    #     严格大于，allowed_fails=1 → 第 2 次失败才触发。
    #   _is_allowed_fails_set_on_router=False (未设) → 走 v2:
    #     _should_cooldown_deployment 直接 `429 and not is_single_deployment_model_group → True`
    #     第 1 次 429 立刻 cooldown。
    # canary 要 1-failure-to-cooldown，必须删 allowed_fails 走 v2 路径。
    rs.pop("allowed_fails", None)
    rs["cooldown_time"] = 3600
    rs["fallbacks"] = []
    rs["model_group_alias"] = {}
    canary_cfg["router_settings"] = rs

    gs = dict(prod_cfg.get("general_settings", {}))
    gs["store_model_in_db"] = False
    canary_cfg["general_settings"] = gs

    return yaml.dump(canary_cfg, sort_keys=False, allow_unicode=True)


def ensure_canary_master_key_secret():
    r = sh_ok(["kubectl", "-n", NS, "get", "secret", CANARY_MASTER_KEY_SECRET])
    if r.returncode == 0:
        r2 = sh(["kubectl", "-n", NS, "get", "secret", CANARY_MASTER_KEY_SECRET,
                 "-o", "jsonpath={.data.LITELLM_MASTER_KEY}"])
        return base64.b64decode(r2.stdout).decode()
    key = "sk-canary-master-" + secrets.token_urlsafe(24)
    sh(["kubectl", "-n", NS, "create", "secret", "generic",
        CANARY_MASTER_KEY_SECRET,
        "--from-literal=LITELLM_MASTER_KEY=" + key])
    return key


# Deploy + Svc manifest. Defined as a Python format string (NOT f-string)
# so curly braces in YAML are plain and `.format()` substitutes named tokens.
DEPLOY_YAML_TEMPLATE = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {canary_deploy}
  namespace: {ns}
  labels:
    app: {canary_deploy}
    pool: canary
spec:
  replicas: 1
  strategy:
    rollingUpdate:
      maxSurge: 0
      maxUnavailable: 1
    type: RollingUpdate
  selector:
    matchLabels:
      app: {canary_deploy}
  template:
    metadata:
      labels:
        app: {canary_deploy}
        pool: canary
    spec:
      tolerations:
        - effect: NoSchedule
          key: dedicated
          operator: Equal
          value: standby
      containers:
        - name: litellm
          image: {canary_image}
          imagePullPolicy: IfNotPresent
          command: ["/app/docker/prod_entrypoint.sh"]
          args: ["--config", "/app/config.yaml", "--port", "4000", "--num_workers", "1"]
          ports:
            - name: http
              containerPort: 4000
              protocol: TCP
          env:
            - name: LITELLM_LOG
              value: INFO
            - name: LITELLM_ENV
              value: canary
            - name: SERVER_ROOT_PATH
              value: /canary
            - name: STORE_MODEL_IN_DB
              value: "False"
            - name: STORE_PROMPTS_IN_SPEND_LOGS
              value: "False"
            - name: LITELLM_MASTER_KEY
              valueFrom:
                secretKeyRef:
                  name: {canary_mk_secret}
                  key: LITELLM_MASTER_KEY
          envFrom:
            - secretRef:
                name: litellm-secrets
            - secretRef:
                name: carher-env-keys
          resources:
            requests:
              cpu: 200m
              memory: 1Gi
            limits:
              cpu: 2
              memory: 4Gi
          livenessProbe:
            httpGet:
              path: /health/liveliness
              port: 4000
            initialDelaySeconds: 60
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 6
          readinessProbe:
            httpGet:
              path: /health/readiness
              port: 4000
            initialDelaySeconds: 30
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 12
          volumeMounts:
            - mountPath: /app/config.yaml
              name: config
              readOnly: true
              subPath: config.yaml
            - mountPath: /app/encrypted_content_degrade_strip.py
              name: callbacks
              readOnly: true
              subPath: encrypted_content_degrade_strip.py
            - mountPath: /app/opus_47_fix.py
              name: callbacks
              readOnly: true
              subPath: opus_47_fix.py
            - mountPath: /app/embedding_sanitize.py
              name: callbacks
              readOnly: true
              subPath: embedding_sanitize.py
            - mountPath: /app/streaming_bridge.py
              name: callbacks
              readOnly: true
              subPath: streaming_bridge.py
            - mountPath: /app/force_stream.py
              name: callbacks
              readOnly: true
              subPath: force_stream.py
            - mountPath: /app/null_byte_sanitize.py
              name: callbacks
              readOnly: true
              subPath: null_byte_sanitize.py
            - mountPath: /app/anthropic_passthrough_pingfix.py
              name: callbacks
              readOnly: true
              subPath: anthropic_passthrough_pingfix.py
            - mountPath: /app/chatgpt_responses_output_fallback.py
              name: callbacks
              readOnly: true
              subPath: chatgpt_responses_output_fallback.py
            - mountPath: /app/client_metadata_strip.py
              name: callbacks
              readOnly: true
              subPath: client_metadata_strip.py
            - mountPath: /app/responses_aclose.py
              name: callbacks
              readOnly: true
              subPath: responses_aclose.py
            - mountPath: /app/baiyu_image_route.py
              name: callbacks
              readOnly: true
              subPath: baiyu_image_route.py
            - mountPath: /app/chatgpt_responses_normalize.py
              name: callbacks
              readOnly: true
              subPath: chatgpt_responses_normalize.py
            - mountPath: /app/register_pricing.py
              name: hooks
              readOnly: true
              subPath: register_pricing.py
            - mountPath: /app/.venv/lib/python3.13/site-packages/litellm/proxy/pass_through_endpoints/llm_provider_handlers/anthropic_passthrough_logging_handler.py
              name: anthropic-logging-patch
              readOnly: true
              subPath: anthropic_passthrough_logging_handler.py
      volumes:
        - name: config
          configMap:
            defaultMode: 420
            name: {canary_cm}
        - name: callbacks
          configMap:
            defaultMode: 420
            name: litellm-callbacks
        - name: hooks
          configMap:
            defaultMode: 420
            name: litellm-hooks
        - name: anthropic-logging-patch
          configMap:
            name: litellm-anthropic-logging-patch
---
apiVersion: v1
kind: Service
metadata:
  name: {canary_svc}
  namespace: {ns}
  labels:
    app: {canary_deploy}
spec:
  type: ClusterIP
  selector:
    app: {canary_deploy}
  ports:
    - name: http
      port: 4000
      protocol: TCP
      targetPort: 4000
"""


def build_deploy_svc_yaml():
    return DEPLOY_YAML_TEMPLATE.format(
        ns=NS,
        canary_deploy=CANARY_DEPLOY,
        canary_svc=CANARY_SVC,
        canary_cm=CANARY_CM,
        canary_mk_secret=CANARY_MASTER_KEY_SECRET,
        canary_image=CANARY_IMAGE,
    )


def cmd_apply():
    stamp("=== Step 1.0: snapshot prod baseline (read-only) ===")
    prod_rs = get_prod_cm().get("router_settings", {})
    stamp("  prod router_settings: allowed_fails=" + str(prod_rs.get("allowed_fails"))
          + " cooldown_time=" + str(prod_rs.get("cooldown_time")))

    r = sh_ok([
        "kubectl", "-n", NS, "exec", "litellm-redis-0", "--",
        "redis-cli", "--scan", "--pattern", "deployment:*:cooldown",
    ])
    prod_cd = sorted([line.strip() for line in r.stdout.splitlines() if line.strip()])
    with open("/root/litellm-canary/prod-cooldown-baseline.txt", "w") as f:
        f.write("\n".join(prod_cd) + "\n")
    stamp("  prod Redis cooldown key count: " + str(len(prod_cd))
          + " (snap to /root/litellm-canary/prod-cooldown-baseline.txt)")

    stamp("=== Step 1.1: scale=1 chatgpt-acct-{49,68} so svc has endpoint ===")
    for acct in CANARY_ACCTS:
        sh(["kubectl", "-n", NS, "scale", "deploy/chatgpt-acct-" + str(acct), "--replicas=1"])
    for acct in CANARY_ACCTS:
        deadline = time.time() + 120
        ok = False
        while time.time() < deadline:
            r = sh_ok([
                "kubectl", "-n", NS, "get", "endpoints", "chatgpt-acct-" + str(acct),
                "-o", "jsonpath={.subsets[*].addresses[*].ip}",
            ])
            if r.stdout.strip():
                stamp("  chatgpt-acct-" + str(acct) + " endpoint ready: " + r.stdout.strip())
                ok = True
                break
            time.sleep(3)
        if not ok:
            sys.exit("FAIL: chatgpt-acct-" + str(acct) + " svc endpoint not ready in 120s")

    stamp("=== Step 1.2: ensure canary master key secret ===")
    canary_mk = ensure_canary_master_key_secret()
    stamp("  CANARY_MASTER_KEY (16-char prefix): " + canary_mk[:16] + "...")

    stamp("=== Step 1.3: build canary ConfigMap from prod CM ===")
    prod_cfg = get_prod_cm()
    pool_mk = get_prod_master_key()
    canary_yaml = build_canary_cm_yaml(prod_cfg, pool_mk)
    assert len(canary_yaml) < 1024 * 1024, "canary CM too big: " + str(len(canary_yaml)) + "B"
    with open("/root/litellm-canary/canary-config.yaml", "w") as f:
        f.write(canary_yaml)
    stamp("  canary CM YAML: " + str(len(canary_yaml)) + "B  ("
          + str(len(canary_yaml.splitlines())) + " lines)")

    r = sh_ok(["kubectl", "-n", NS, "get", "cm", CANARY_CM])
    if r.returncode == 0:
        bak = "/root/litellm-canary/canary-cm.bak-" + str(int(time.time())) + ".yaml"
        b = sh(["kubectl", "-n", NS, "get", "cm", CANARY_CM, "-o", "yaml"])
        with open(bak, "w") as f:
            f.write(b.stdout)
        stamp("  existing canary CM backed up: " + bak)
        sh(["kubectl", "-n", NS, "delete", "cm", CANARY_CM])

    sh(["kubectl", "-n", NS, "create", "cm", CANARY_CM,
        "--from-file=config.yaml=/root/litellm-canary/canary-config.yaml"])

    stamp("=== Step 1.4: apply canary Deploy + Svc ===")
    deploy_yaml = build_deploy_svc_yaml()
    with open("/root/litellm-canary/40-deploy-svc.yaml", "w") as f:
        f.write(deploy_yaml)
    sh(["kubectl", "-n", NS, "apply", "-f", "/root/litellm-canary/40-deploy-svc.yaml"])

    stamp("=== Step 1.5: wait canary rollout ===")
    sh(["kubectl", "-n", NS, "rollout", "status", "deploy/" + CANARY_DEPLOY,
        "--timeout=300s"])

    r = sh(["kubectl", "-n", NS, "get", "pod",
            "-l", "app=" + CANARY_DEPLOY,
            "-o", "jsonpath={.items[*].status.phase}"])
    stamp("  canary pod phase: " + r.stdout.strip())

    stamp("=== Step 1 DONE ===")
    print()
    print("Next:")
    print("  CANARY_MASTER_KEY: stored in secret/" + CANARY_MASTER_KEY_SECRET)
    print("  canary endpoint:   http://" + CANARY_SVC + "." + NS + ".svc.cluster.local:4000")
    print("  port-forward:      kubectl -n " + NS + " port-forward svc/" + CANARY_SVC + " 4001:4000")
    print("  model_group:       chatgpt-canary-gpt-5.5")
    print("  Redis cooldown prefix: deployment:chatgpt-acct-canary-*:cooldown")


def cmd_teardown():
    stamp("=== Teardown canary ===")
    sh_ok(["kubectl", "-n", NS, "delete", "deploy", CANARY_DEPLOY])
    sh_ok(["kubectl", "-n", NS, "delete", "svc", CANARY_SVC])
    sh_ok(["kubectl", "-n", NS, "delete", "cm", CANARY_CM])
    sh_ok(["kubectl", "-n", NS, "delete", "secret", CANARY_MASTER_KEY_SECRET])
    sh_ok([
        "kubectl", "-n", NS, "exec", "litellm-redis-0", "--",
        "sh", "-c",
        "redis-cli --scan --pattern 'deployment:chatgpt-acct-canary-*:cooldown' "
        "| xargs -r redis-cli DEL",
    ])
    for acct in CANARY_ACCTS:
        sh_ok(["kubectl", "-n", NS, "scale", "deploy/chatgpt-acct-" + str(acct),
               "--replicas=0"])
    stamp("DONE")


def cmd_status():
    stamp("=== canary status ===")
    sh_ok(["kubectl", "-n", NS, "get", "deploy", CANARY_DEPLOY, "-o", "wide"])
    sh_ok(["kubectl", "-n", NS, "get", "svc", CANARY_SVC])
    sh_ok(["kubectl", "-n", NS, "get", "pod", "-l", "app=" + CANARY_DEPLOY, "-o", "wide"])
    print()
    print("=== canary cooldown keys in Redis ===")
    r = sh_ok([
        "kubectl", "-n", NS, "exec", "litellm-redis-0", "--",
        "redis-cli", "--scan", "--pattern", "deployment:chatgpt-acct-canary-*:cooldown",
    ])
    print(r.stdout)


def main():
    p = argparse.ArgumentParser()
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--teardown", action="store_true")
    grp.add_argument("--status", action="store_true")
    args = p.parse_args()

    os.makedirs("/root/litellm-canary", exist_ok=True)
    if args.apply:
        cmd_apply()
    elif args.teardown:
        cmd_teardown()
    elif args.status:
        cmd_status()


if __name__ == "__main__":
    main()
