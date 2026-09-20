#!/usr/bin/env python3
"""把 sub2api 的 grok video 挂到 198 生产 LiteLLM —— 走 pass-through，不走 model_list。

为什么不是 `/model/new`：LiteLLM 的 `/v1/videos` 通路和 sub2api **结构性不兼容**，
三处各自独立，升级也修不好（`OpenAIVideoConfig.use_multipart_form_data()` 返 True
是 upstream PR #38104 于 2026-08-24 故意改的，早于我们的 v1.100.1）：

  1. 编码：LiteLLM 无条件发 multipart，sub2api 只吃 JSON（multipart 一律 415）；
  2. 路径：LiteLLM 硬编码 `{api_base}/videos`，sub2api 真身是 `/v1/videos/generations`；
  3. 响应：`VideoObject` 强制 `id` + `object` + `status`，sub2api 返的是
     `{"request_id": ...}` / `{"status":"done","video":{...}}`，`model_validate` 必炸。

所以正解是 `/config/pass_through_endpoint`：纯配置、零代码、零重启，LiteLLM 只当
反代，请求体原样透传。代价写在 §代价 里（不进 SpendLogs）。

在 198 本机跑：

    MK=$(sudo kubectl -n litellm-product get secret litellm-secrets \
          -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)
    export LITELLM_MASTER_KEY=$MK
    python3 litellm-198-sub2api-video-passthrough.py plan       # 只读，看现状
    python3 litellm-198-sub2api-video-passthrough.py probe      # 证 JSON/multipart 那条腿
    python3 litellm-198-sub2api-video-passthrough.py apply --apply
    python3 litellm-198-sub2api-video-passthrough.py verify     # 三段梯子，跑真片子
    python3 litellm-198-sub2api-video-passthrough.py remove --apply

sub2api 的上游 key：默认从 sub2api 自己的 postgres 读（`api_keys.key` where
`name='grok-litellm'`），也可以用 `SUB2API_GROK_KEY` 覆盖。**本文件里不写死任何
凭据，也不用 `os.environ.get("K","<真key>")` 那种兜底默认值** —— 那等于把凭据
提交进仓库，一旦推上 origin 就只有轮转能解。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

PROXY = os.environ.get("LITELLM_198", "http://127.0.0.1:30402")

# pass-through 的三个不变量。改任何一个都要重跑 verify 那三段梯子。
PT_PATH = "/sa-video"
PT_TARGET = "http://sub2api.litellm-dev.svc.cluster.local:8080/v1/videos"
SUB2API_NODEPORT = os.environ.get("SUB2API_DIRECT", "http://127.0.0.1:31880")

KEY_ENV = "SUB2API_GROK_KEY"
SUB2API_KEY_NAME = "grok-litellm"
SUB2API_NS = "litellm-dev"

# sub2api 的 usage tick：4000000000 ticks == $0.40（8s 片子实测）。
TICKS_PER_USD = 1e10


# ---------------------------------------------------------------------------
# 纯函数（可单测，无副作用）
# ---------------------------------------------------------------------------
def build_passthrough_payload(api_key: str, *, path: str = PT_PATH,
                              target: str = PT_TARGET) -> dict:
    """spec -> POST /config/pass_through_endpoint body。

    `include_subpath` 必须 True：sub2api 的 video 是异步三段式，
    `/generations`、`/{rid}`、`/{rid}/content` 三条子路径都得转发过去。
    `auth` 必须 True：保住 LiteLLM 侧的 key 认证，否则这条路成了裸开的匿名口。
    """
    return {
        "path": path,
        "target": target,
        "headers": {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
        "include_subpath": True,
        "auth": True,
    }


def redact_endpoint(ep: dict) -> dict:
    """打印用：把 headers 里的凭据掩掉。日志/终端里不留 Bearer 全文。"""
    out = dict(ep)
    h = out.get("headers") or {}
    out["headers"] = {
        k: (v[:8] + "…REDACTED" if k.lower() == "authorization" else v)
        for k, v in h.items()
    }
    return out


def ticks_to_usd(ticks: int | float) -> float:
    """sub2api `usage.cost_in_usd_ticks` -> 美元。4e9 ticks = $0.40。"""
    return float(ticks) / TICKS_PER_USD


def video_probe_matrix(base: str) -> list[dict]:
    """要跑的编码/路径矩阵。只描述，不发请求 —— 让判据本身可被单测锁住。

    这四发是"LiteLLM 为什么打不通"的直接证据：同一把 key、同一个 body，
    只变编码，JSON 200 / multipart 415。
    """
    body = {"model": "grok-imagine-video", "prompt": "a red cube rotating"}
    out = []
    for path in ("/v1/videos", "/v1/videos/generations"):
        for enc in ("json", "multipart"):
            out.append(dict(url=base.rstrip("/") + path, encoding=enc,
                            body=body, expect=200 if enc == "json" else 415))
    return out


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------
def master_key() -> str:
    k = os.environ.get("LITELLM_MASTER_KEY")
    if not k:
        sys.exit("LITELLM_MASTER_KEY not set "
                 "(sudo kubectl -n litellm-product get secret litellm-secrets "
                 "-o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)")
    return k


def sub2api_key() -> str:
    """env 优先，否则从 sub2api 的 postgres 读。永不硬编码。"""
    k = os.environ.get(KEY_ENV)
    if k:
        return k
    pod = _sh([
        "sudo", "kubectl", "-n", SUB2API_NS, "get", "pod", "-l", "app=sub2api-postgres",
        "--field-selector=status.phase=Running",
        "-o", "jsonpath={.items[0].metadata.name}",
    ]).strip()
    if not pod:
        sys.exit(f"no Running sub2api-postgres pod in ns {SUB2API_NS} "
                 f"(⚠️ 这个 ns 里有 Completed 的僵尸 pod，必须带 field-selector)")
    # ⚠️ 列名是 `key`（不是 `key_value`），软删判据是 `deleted_at is null`
    # （**不是** `status` —— 实测 id=3 是 `status=active` 但 `deleted_at` 非空的
    # 已删行，只看 status 会把删掉的 key 当活的取出来），而且**没有** `is_active` 列。
    sql = ("select key from api_keys "
           "where name = $$%s$$ and deleted_at is null and status = $$active$$ "
           "order by id limit 1" % SUB2API_KEY_NAME)
    out = _sh(["sudo", "kubectl", "-n", SUB2API_NS, "exec", pod, "--",
               "psql", "-U", "sub2api", "-d", "sub2api", "-t", "-A", "-c", sql]).strip()
    if not out:
        sys.exit(f"api_keys.key for name={SUB2API_KEY_NAME!r} came back empty "
                 f"(⚠️ 列名是 `key` 不是 `key_value`)")
    return out.splitlines()[0].strip()


def _sh(cmd: list[str]) -> str:
    """⛔ 不吞 stderr：吞了之后失败会读成"查到空值"，方向与真相相反。"""
    p = subprocess.run(cmd, capture_output=True, text=True)
    err = "\n".join(l for l in p.stderr.splitlines() if not l.startswith("[sudo]"))
    if p.returncode != 0:
        sys.exit(f"command failed ({p.returncode}): {' '.join(cmd[:6])}…\n{err}")
    if err.strip():
        print(f"  (stderr) {err.strip()}", file=sys.stderr)
    return p.stdout


def _req(method: str, url: str, body, key: str | None, *, timeout: int = 120,
         ctype: str = "application/json", raw: bool = False):
    """返 (status, parsed_or_bytes, headers)。HTTPError 也返，不 raise。"""
    if body is None:
        data = None
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    else:
        data = json.dumps(body).encode()
    headers = {"Content-Type": ctype} if data is not None else {}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        payload = r.read()
        st, hdrs = r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        payload, st, hdrs = e.read(), e.code, dict(e.headers)
    if raw:
        return st, payload, hdrs
    try:
        return st, json.loads(payload.decode() or "{}"), hdrs
    except Exception:
        return st, {"raw": payload[:400].decode("utf-8", "replace")}, hdrs


def list_endpoints(key: str) -> list[dict]:
    st, j, _ = _req("GET", PROXY + "/config/pass_through_endpoint", None, key, timeout=30)
    if st != 200:
        sys.exit(f"GET /config/pass_through_endpoint -> {st} {str(j)[:300]}")
    return j.get("endpoints", [])


def _multipart(body: dict) -> tuple[bytes, str]:
    b = "----sa-video-probe-boundary"
    parts = []
    for k, v in body.items():
        parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n")
    parts.append(f"--{b}--\r\n")
    return "".join(parts).encode(), f"multipart/form-data; boundary={b}"


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_plan(a) -> int:
    key = master_key()
    eps = list_endpoints(key)
    mine = [e for e in eps if e.get("path") == PT_PATH]
    print(f"proxy={PROXY}  pass_through_endpoints={len(eps)}  ours({PT_PATH})={len(mine)}")
    for e in eps:
        print("  " + json.dumps(redact_endpoint(e), ensure_ascii=False))
    if not mine:
        print(f"\nwould CREATE {PT_PATH} -> {PT_TARGET}  (include_subpath=True, auth=True)")
    else:
        cur = mine[0]
        drift = [f"target={cur.get('target')} want={PT_TARGET}"] \
            if cur.get("target") != PT_TARGET else []
        if not cur.get("include_subpath"):
            drift.append("include_subpath=False want=True  (子路径全 404)")
        if not cur.get("auth"):
            drift.append("auth=False want=True  (这条路成了匿名口)")
        print("\nDRIFT: " + "; ".join(drift) if drift else "\nin desired state")
    return 0


def cmd_probe(a) -> int:
    """直打 sub2api，证明"JSON 200 / multipart 415"这条腿。绕开 LiteLLM。

    这是「LiteLLM 打不通」的第一段证据：同一把 key、同一个 body，只变编码。
    """
    ak = sub2api_key()
    bad = 0
    for c in video_probe_matrix(SUB2API_NODEPORT):
        if c["encoding"] == "json":
            st, j, _ = _req("POST", c["url"], c["body"], ak, timeout=60)
        else:
            data, ctype = _multipart(c["body"])
            st, j, _ = _req("POST", c["url"], data, ak, timeout=60, ctype=ctype)
        ok = st == c["expect"]
        bad += 0 if ok else 1
        tag = "ok " if ok else "BAD"
        print(f"  {tag} {c['encoding']:9s} {c['url']:52s} -> {st} "
              f"(expect {c['expect']}) {str(j)[:90]}")
    print(f"\nbad={bad}/4")
    if not bad:
        print("⇒ sub2api 只吃 JSON。LiteLLM `/v1/videos` 无条件发 multipart"
              "（PR #38104, 2026-08-24 故意的）⇒ 升级永不修好，只能 pass-through。")
    return 1 if bad else 0


def cmd_apply(a) -> int:
    key = master_key()
    ak = sub2api_key()
    eps = list_endpoints(key)
    if any(e.get("path") == PT_PATH for e in eps):
        print(f"{PT_PATH} already exists — 先 remove 再重建，或直接 verify")
        return 0
    payload = build_passthrough_payload(ak)
    if not a.apply:
        print("DRY-RUN: would POST /config/pass_through_endpoint")
        print(json.dumps(redact_endpoint(payload), ensure_ascii=False, indent=1))
        print("re-run with --apply to write")
        return 0
    st, j, _ = _req("POST", PROXY + "/config/pass_through_endpoint", payload, key, timeout=60)
    print(f"POST /config/pass_through_endpoint -> {st} {str(j)[:200]}")
    if st != 200:
        return 1
    # 200 只证明写了一行。判生效读回来。
    got = [e for e in list_endpoints(key) if e.get("path") == PT_PATH]
    print(f"readback: present={len(got)}")
    for e in got:
        print("  " + json.dumps(redact_endpoint(e), ensure_ascii=False))
    return 0 if got else 1


def cmd_verify(a) -> int:
    """三段梯子。⛔ 缺任何一段都不算通 —— 202 pending 也可能永远不 done。

    需要一把能打 198 的 key（默认用 master key）。跑一发真片子，会真花钱。
    """
    key = os.environ.get("LITELLM_PROBE_KEY") or master_key()
    base = PROXY + PT_PATH

    # ① POST /generations -> request_id
    st, j, _ = _req("POST", base + "/generations",
                    {"model": a.model, "prompt": a.prompt}, key, timeout=120)
    rid = (j or {}).get("request_id")
    print(f"① POST {PT_PATH}/generations -> {st} request_id={rid!r}")
    if st != 200 or not rid:
        print("   ⛔ 拿不到 request_id 就别往下轮询 —— 空 id 会得到一排 404 "
              "`Video request not found`，形状和 pass-through 坏了一模一样")
        return 1

    # ② 轮询到 done
    deadline = time.time() + a.timeout
    done = None
    while time.time() < deadline:
        st, j, _ = _req("GET", f"{base}/generations/{rid}", None, key, timeout=60)
        status = (j or {}).get("status")
        prog = (j or {}).get("progress")
        print(f"② GET {PT_PATH}/generations/{rid[:8]}… -> {st} status={status} progress={prog}")
        if st == 200 and status == "done":
            done = j
            break
        if st == 200 and status in ("failed", "error"):
            print(f"   ⛔ upstream said {status}: {str(j)[:300]}")
            return 1
        if st not in (200, 202):
            print(f"   ⛔ unexpected {st}: {str(j)[:300]}")
            return 1
        time.sleep(a.interval)
    if not done:
        print(f"   ⛔ 超过 {a.timeout}s 还没 done —— 202 pending 不是绿")
        return 1
    ticks = ((done.get("usage") or {}).get("cost_in_usd_ticks") or 0)
    vid = done.get("video") or {}
    print(f"   duration={vid.get('duration')}s  cost_in_usd_ticks={ticks} "
          f"(${ticks_to_usd(ticks):.4f})  url={vid.get('url')}")

    # ③ 真取字节，判 mp4 magic。⛔ 200 + Content-Type 不够，要看头四字节后的 ftyp
    st, body, hdrs = _req("GET", f"{base}/{rid}/content", None, key, timeout=180, raw=True)
    ctype = hdrs.get("Content-Type", "")
    head = body[:12].hex()
    is_mp4 = b"ftyp" in body[:16]
    print(f"③ GET {PT_PATH}/{rid[:8]}…/content -> {st} type={ctype} "
          f"bytes={len(body)} head={head} ftyp={is_mp4}")
    if st != 200 or not is_mp4 or len(body) < 10000:
        print("   ⛔ 不是一个真 mp4")
        return 1
    print("\nall three rungs green")
    return 0


def cmd_remove(a) -> int:
    key = master_key()
    eps = [e for e in list_endpoints(key) if e.get("path") == PT_PATH]
    if not eps:
        print(f"{PT_PATH} not present, nothing to remove")
        return 0
    print("would DELETE:")
    for e in eps:
        print("  " + json.dumps(redact_endpoint(e), ensure_ascii=False))
    if not a.apply:
        print("DRY-RUN — re-run with --apply. "
              "回滚 = 重跑 `apply --apply`（凭据从 DB 现取，不依赖备份）")
        return 0
    st, j, _ = _req("DELETE",
                    f"{PROXY}/config/pass_through_endpoint?endpoint_id={PT_PATH}",
                    None, key, timeout=60)
    print(f"DELETE -> {st} {str(j)[:200]}")
    left = [e for e in list_endpoints(key) if e.get("path") == PT_PATH]
    print(f"readback: remaining={len(left)}")
    return 0 if st == 200 and not left else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="只读：现有 pass-through + 与期望的漂移")
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("probe", help="直打 sub2api，证 JSON 200 / multipart 415")
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("apply", help="建 pass-through（默认 dry-run）")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("verify", help="三段梯子，跑一发真片子（会花钱）")
    p.add_argument("--model", default="grok-imagine-video")
    p.add_argument("--prompt", default="a red cube slowly rotating on a white table")
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--interval", type=int, default=10)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("remove", help="删 pass-through（默认 dry-run）")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(fn=cmd_remove)

    a = ap.parse_args()
    return a.fn(a) or 0


if __name__ == "__main__":
    sys.exit(main())
