#!/usr/bin/env python3
"""mcp-provision-198.py — 在 198 上跑，通过 kubectl exec 在 pod 内部调 chatgpt.com API

核心思路：198 打不通 chatgpt.com（GFW），但 zero-* pod 能。
所以把 mcp-connector-cli.js cp 进 pod，在 pod 内部执行 provision。
每个 pod 自带 node + 自己的 session（users.json）。

用法：
  # dry-run（默认）
  python3 mcp-provision-198.py --url https://cc.auto-link.com.cn/lark-mcp-13d090a521d12aa42c2eb9ac8c977ac1/mcp --name lark

  # 金丝雀
  python3 mcp-provision-198.py --url ... --name lark --only zero-115 --apply

  # 全量
  python3 mcp-provision-198.py --url ... --name lark --apply --min-actions 24
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

NS = "litellm-product"
KUBECONFIG = "/home/cltx/.kube/config"
CLI_LOCAL = "/tmp/mcp-connector-cli.js"
CLI_POD_PATH = "/tmp/mcp-connector-cli.js"
USERS_JSON_PATH = "/app/temp/users.json"


def kubectl(cmd, timeout=120):
    env = dict(os.environ, KUBECONFIG=KUBECONFIG)
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "(timeout)"
    except Exception as e:
        return 1, str(e)


def list_pods(only=None):
    rc, out = kubectl("kubectl get pods -n %s -o name" % NS)
    if rc != 0:
        print("错误: 列 pod 失败: %s" % out[:200])
        sys.exit(2)
    pods = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("pod/"):
            continue
        name = line[4:]
        if not name.startswith("zero-"):
            continue
        if only and not any(name.startswith(o) for o in only):
            continue
        pods.append(name)
    return sorted(pods)


def copy_cli_to_pod(pod):
    """kubectl cp CLI script into pod."""
    rc, out = kubectl(
        "kubectl cp %s %s/%s:%s -n %s" % (CLI_LOCAL, NS, pod, CLI_POD_PATH, NS),
        timeout=30,
    )
    return rc == 0


def run_in_pod(pod, node_args, timeout=240):
    """Run node script inside pod, passing session from its own users.json.

    The inline JS:
    1. Reads /app/temp/users.json
    2. Extracts the first account with valid authorization
    3. Writes a temp session file
    4. Spawns mcp-connector-cli.js with that session
    """
    # Build a one-liner that creates session and runs CLI
    wrapper = """
const fs = require('fs');
const {execSync} = require('child_process');
try {
  const d = JSON.parse(fs.readFileSync('%s', 'utf8'));
  const chatgpt = d.chatgpt || {};
  let hdrs = null;
  for (const [acct, rec] of Object.entries(chatgpt)) {
    const pf = (rec || {}).parsedFetch || {};
    const h = pf.headers;
    if (h && h.authorization) { hdrs = h; break; }
  }
  if (!hdrs) { process.stdout.write('ERROR:no_session\\n'); process.exit(1); }
  const sess = '/tmp/_sess.json';
  fs.writeFileSync(sess, JSON.stringify({headers: hdrs}), {mode: 0o600});
  const out = execSync('node %s --session ' + sess + ' %s', {
    timeout: 200000, encoding: 'utf8', stdio: ['pipe','pipe','pipe']
  });
  process.stdout.write(out);
} catch(e) {
  process.stdout.write('ERROR:' + (e.stderr || e.message || e) + '\\n');
  process.exit(1);
}
""".strip() % (USERS_JSON_PATH, CLI_POD_PATH, node_args)

    # Write wrapper to a temp file in pod and execute
    wrapper_escaped = wrapper.replace("'", "'\"'\"'")
    cmd = "kubectl exec -n %s %s -- node -e '%s'" % (NS, pod, wrapper_escaped)
    return kubectl(cmd, timeout=timeout)


def provision_pod(pod, url, name, min_actions):
    """Full provision for one pod: devmode → register → actions check → link."""
    # First ensure CLI is in the pod
    if not copy_cli_to_pod(pod):
        return False, "cp CLI 失败"

    args = "provision --url %s --name %s" % (url, name)
    rc, out = run_in_pod(pod, args, timeout=240)

    if "ERROR:no_session" in out:
        return None, "无有效 session"

    if rc != 0 or "完成:" not in out:
        # Extract last meaningful line
        lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
        detail = lines[-1][:120] if lines else "无输出"
        return False, detail

    # Extract connector id
    cid = ""
    for line in out.splitlines():
        m = re.search(r"connector=(\S+)", line)
        if m:
            cid = m.group(1)

    # Verify actions count
    if cid:
        rc2, out2 = run_in_pod(pod, "actions %s" % cid, timeout=60)
        m2 = re.search(r"抓到 (\d+) 个 action", out2)
        if not m2:
            m2 = re.search(r"(\d+) 个 action", out2)
        n_actions = int(m2.group(1)) if m2 else 0
        if n_actions < min_actions:
            return False, "actions=%d (需>=%d) connector=%s" % (n_actions, min_actions, cid)
        return True, "connector=%s actions=%d" % (cid, n_actions)

    return True, "provision OK (未提取 cid)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="MCP server URL")
    ap.add_argument("--name", default="lark", help="connector 名 (默认 lark)")
    ap.add_argument("--only", nargs="*", help="只处理这些 pod 前缀")
    ap.add_argument("--apply", action="store_true", help="真执行（默认 dry-run）")
    ap.add_argument("--min-actions", type=int, default=1)
    ap.add_argument("--cli", default=CLI_LOCAL, help="mcp-connector-cli.js 路径")
    a = ap.parse_args()

    global CLI_LOCAL
    CLI_LOCAL = a.cli

    if not a.url:
        print("错误: 需要 --url")
        sys.exit(2)

    if not os.path.isfile(CLI_LOCAL):
        print("错误: CLI 文件不存在: %s" % CLI_LOCAL)
        sys.exit(2)

    pods = list_pods(a.only)
    if not pods:
        print("无匹配 pod (ns=%s)" % NS)
        sys.exit(2)

    mode = "APPLY" if a.apply else "DRY-RUN"
    print("=== MCP Provision | %s | %d pods ===" % (mode, len(pods)))
    host = a.url.split("//")[-1].split("/")[0]
    print("    target: %s  name: %s  min_actions: %d" % (host, a.name, a.min_actions))

    ok = skip = fail = 0
    for pod in pods:
        tag = "-".join(pod.split("-")[:2])  # zero-115

        if not a.apply:
            print("  DRY   %-20s 将 provision" % tag)
            ok += 1
            continue

        result = provision_pod(pod, a.url, a.name, a.min_actions)
        if result is None:
            print("  SKIP  %-20s %s" % (tag, result[1] if isinstance(result, tuple) else ""))
            skip += 1
        elif isinstance(result, tuple):
            success, detail = result
            if success:
                print("  OK    %-20s %s" % (tag, detail))
                ok += 1
            elif success is None:
                print("  SKIP  %-20s %s" % (tag, detail))
                skip += 1
            else:
                print("  FAIL  %-20s %s" % (tag, detail))
                fail += 1
        time.sleep(1)

    print("\n== ok=%d skip=%d fail=%d ==" % (ok, skip, fail))
    if not a.apply:
        print("(dry-run — 加 --apply 执行)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
