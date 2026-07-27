#!/usr/bin/env python3
"""mcp-provision-pool.py -- 给 zerokey pod 池批量开通 MCP connector

为什么需要:connector 是**账号级**(`owners:[{"type":"USER"}]`),所以 47 个
pod 账号必须各自 register + link 一次。手工做不现实,也容易漏。

**默认 dry-run。** 必须显式 --apply 才会真的写。

用法:
  # 1. 看会动哪些 pod(不写任何东西)
  ./mcp-provision-pool.py --url https://host/lark-mcp-<secret>/mcp --name lark

  # 2. 先只做一个 pod 验证
  ./mcp-provision-pool.py --url ... --name lark --only zero-100 --apply

  # 3. 全量
  ./mcp-provision-pool.py --url ... --name lark --apply

  # 4. 查现状
  ./mcp-provision-pool.py --list

设计取舍:
  - session bundle 从每个 pod 的 users.json 现取,不缓存 —— 凭据会轮转
  - 串行执行。47 个账号并发注册会同时打 OpenAI,且失败难归因;
    每个约 3 次 HTTP,串行总耗时可接受
  - **不幂等**:register 每次都会创建**新** connector,不会复用同名的。
    重复跑会在账号上堆积 connector,需先用 CLI delete 清理。
    (要做到幂等需要 list-by-account 接口,当前 API 未找到)
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "mcp-connector-cli.js")
NS = os.environ.get("ZK_NS", "litellm-product")
JMS = os.environ.get("ZK_JMS", "AIYJY-litellm")
REPO = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))


def sh(cmd, timeout=180):
    """Run locally, return (rc, stdout+stderr)."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "(timeout)"
    except Exception as e:                                   # noqa: BLE001
        return 1, "(runner error: %s)" % e


def kube(inner, timeout=180):
    """Run a kubectl command on 198 via the jms hop."""
    jms = os.path.join(REPO, "scripts", "jms")
    return sh("%s ssh %s %s" % (jms, JMS, shq(inner)), timeout=timeout)


def shq(s):
    """POSIX shell single-quote. NOT json -- the old name `json_quote` was
    misleading; this does shell quoting and is used for shell interpolation."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def list_pods(only=None):
    rc, out = kube("kubectl get pods -n %s -o name" % NS)
    if rc != 0:
        die("列 pod 失败: %s" % out.strip()[:300])
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


def pod_acct_sessions(pod):
    """Pull {acct: headers} from a pod's users.json.

    Returns [] on any failure -- a pod we cannot read is reported and skipped,
    never silently treated as 'no accounts'.
    """
    # 路径实测在 /app/temp/users.json(不是 /app/data)。留 env 覆盖以防镜像变更。
    path = os.environ.get("ZK_USERS_JSON", "/app/temp/users.json")
    rc, out = kube("kubectl exec -n %s %s -- cat %s 2>/dev/null"
                   % (NS, pod, path), timeout=120)
    if rc != 0 or not out.strip():
        return None, "读 %s 失败" % path
    # jms may prepend banner lines; find the JSON body
    i = out.find("{")
    if i < 0:
        return None, "%s 里没有 JSON" % path
    try:
        d = json.loads(out[i:])
    except Exception as e:                                   # noqa: BLE001
        return None, "users.json 解析失败: %s" % str(e)[:80]

    sessions = {}
    for acct, rec in (d.get("chatgpt") or {}).items():
        pf = (rec or {}).get("parsedFetch") or {}
        hdrs = pf.get("headers")
        if isinstance(hdrs, dict) and hdrs.get("authorization"):
            sessions[acct] = hdrs
    if not sessions:
        return None, "没有带 authorization 的账号"
    return sessions, None


def run_cli(headers, args, timeout=240):
    """Invoke mcp-connector-cli.js with a temp session file (mode 600)."""
    import tempfile
    fd, path = tempfile.mkstemp(prefix="zksess-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"headers": headers}, fh)
        return sh("node %s --session %s %s" % (shq(CLI), shq(path), args),
                  timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def die(msg):
    print("错误: %s" % msg)
    sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="MCP server URL (register 时必需)")
    ap.add_argument("--name", default="lark", help="connector 名 (默认 lark)")
    ap.add_argument("--only", nargs="*", help="只处理这些 pod 前缀")
    ap.add_argument("--apply", action="store_true",
                    help="真的执行。不给就是 dry-run")
    ap.add_argument("--list", action="store_true", help="只列 pod/账号,不动任何东西")
    a = ap.parse_args()

    if not a.list and not a.url:
        die("register 需要 --url(或用 --list 只看现状)")

    pods = list_pods(a.only)
    if not pods:
        die("没有匹配的 pod(ns=%s)" % NS)

    mode = "LIST" if a.list else "PROVISION"
    print("=== %s | ns=%s | %d 个 pod | %s ===" %
          (mode, NS, len(pods), "APPLY(会真写)" if a.apply else "DRY-RUN(不写)"))
    if a.url:
        # 不打印完整 URL —— 密钥路径不应进日志
        print("    url: %s/...(密钥路径已隐去)" % a.url.split("//")[-1].split("/")[0])

    ok = skip = fail = 0
    for pod in pods:
        sessions, err = pod_acct_sessions(pod)
        if err:
            print("  SKIP  %-28s %s" % (pod, err))
            skip += 1
            continue

        for acct, hdrs in sorted(sessions.items()):
            tag = "%s/%s" % (pod, acct)

            if a.list:
                print("  %-40s 可用" % tag)
                ok += 1
                continue

            if not a.apply:
                print("  DRY   %-40s 将 %s" % (tag, mode.lower()))
                ok += 1
                continue

            rc, out = run_cli(hdrs, "provision --url %s --name %s"
                              % (shq(a.url), shq(a.name)))
            if rc == 0 and "完成:" in out:
                cid = ""
                for line in out.splitlines():
                    if line.startswith("完成:"):
                        cid = line.strip()
                print("  OK    %-40s %s" % (tag, cid[:70]))
                ok += 1
            else:
                last = [l for l in out.strip().splitlines() if l.strip()]
                print("  FAIL  %-40s %s" % (tag, (last[-1] if last else "无输出")[:80]))
                fail += 1

    print("\n== ok=%d skip=%d fail=%d ==" % (ok, skip, fail))
    if not a.apply and not a.list:
        print("(dry-run —— 加 --apply 才会真写)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
