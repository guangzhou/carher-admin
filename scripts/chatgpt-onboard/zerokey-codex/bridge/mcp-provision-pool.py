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
  - **幂等**:同名 connector 已存在时 API 回 409 且响应体带
    existing_connector_id,CLI 会复用它。所以重复跑安全,不会堆积。
    (最初以为不幂等,是因为把 409 归成了未分类错误。)
  - **注册后独立复查 action 数**:provision 打印"完成"只说明它自己三步都 2xx;
    真正要保证的是 OpenAI 侧抓到了 schema,所以单独再查一次 actions。
    实测 links/noauth 会接受不存在的 action 名并仍回 200,
    所以 link 成功 != 工具可用。lark-mcp 应为 24,用 --min-actions 24 卡住。
"""

import argparse
import json
import os
import re
import subprocess
import time
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


def kube(inner, timeout=180, tries=3):
    """Run a kubectl command on 198 via the jms hop.

    Retries on transport failure: the jms hop intermittently returns
    "Permission denied (password,publickey)" -- observed repeatedly during this
    work, and a single such blip made three pods report "读 users.json 失败" and
    get SKIPped. A batch that silently under-covers is worse than one that stops,
    so retry the transport before believing the pod is broken.
    """
    jms = os.path.join(REPO, "scripts", "jms")
    cmd = "%s ssh %s %s" % (jms, JMS, shq(inner))
    last = (1, "")
    for i in range(max(1, tries)):
        rc, out = sh(cmd, timeout=timeout)
        if rc == 0:
            return rc, out
        # 只对"传输层"失败重试;kubectl 自己的错误(NotFound 等)直接返回
        if "Permission denied" not in out and "Connection closed" not in out \
                and "connect to host" not in out:
            return rc, out
        last = (rc, out)
        if i + 1 < tries:
            time.sleep(2 + 2 * i)
    return last


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
    ap.add_argument("--min-actions", type=int, default=1,
                    help="注册后必须抓到的最少 action 数(默认 1);"
                         "低于此数记 FAIL 而非 OK。lark-mcp 应为 24")
    ap.add_argument("--list", action="store_true", help="只列 pod/账号,不动任何东西")
    a = ap.parse_args()

    if not a.list and not a.url:
        die("register 需要 --url(或用 --list 只看现状)")

    min_actions = a.min_actions
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
            if rc != 0 or "完成:" not in out:
                last = [l for l in out.strip().splitlines() if l.strip()]
                print("  FAIL  %-40s %s" % (tag, (last[-1] if last else "无输出")[:80]))
                fail += 1
                continue

            cid = ""
            for line in out.splitlines():
                m = re.search(r"connector=(\S+)", line)
                if m:
                    cid = m.group(1)

            # 独立复查:provision 打印"完成"只说明它自己的三步都返回 2xx。
            # 真正要保证的是"OpenAI 侧确实抓到了 action schema" —— 这一步单独查,
            # 不复用上一步的输出。API 曾接受不存在的 action 名而仍返回 200
            # (传 no_such_tool_xyz 也 200),所以 link 成功 != 工具可用。
            n_actions = 0
            if cid:
                rc2, out2 = run_cli(hdrs, "actions %s" % shq(cid))
                mm = re.search(r"抓到 (\d+) 个 action", out2)
                if rc2 == 0 and mm:
                    n_actions = int(mm.group(1))

            if n_actions >= min_actions:
                print("  OK    %-40s connector=%s  actions=%d"
                      % (tag, cid[:34], n_actions))
                ok += 1
            else:
                # 注册成功但 schema 抓不到 -> 这个 connector 是残废的,报 FAIL
                # 而不是 OK,否则批量跑完会以为全好了。
                print("  FAIL  %-40s connector=%s  actions=%d (期望>=%d)"
                      % (tag, cid[:34], n_actions, min_actions))
                fail += 1

    print("\n== ok=%d skip=%d fail=%d ==" % (ok, skip, fail))
    if not a.apply and not a.list:
        print("(dry-run —— 加 --apply 才会真写)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
