"""zk-session-reaper — zerokey-pool (225 K3s) session 自动摘除器.

设计（2026-07-15，来自 sol cooldown 风暴排查的教训）：
  - bridge 双引擎：/v1/responses 带 tools → codex OAuth 池；不带 tools → ChatGPT web session。
  - litellm 真实流量基本带 tools（cursor/codex），所以判死只看 codex 路径。
  - codex 路径连续两次 401/403 → DEAD：删除该 pod 的全部 zk-N-* litellm 条目。
  - 仅 web 路径 401 → 只告警（pod 对主流量仍健康，摘除会白丢容量，需人工重 capture）。
  - 超时/5xx → 只告警（可能是负载/重启，不自动摘）。
安全阀：单次运行最多摘 MAX_REAP 个 pod；健康 pod 低于 FLOOR 时停止摘除。
恢复：重新注册用 zerokey-pool-register.py（见 skill zerokey-pool-add）。
"""
import json, os, re, socket, time, urllib.request, urllib.error

socket.setdefaulttimeout(45)
LITELLM = os.environ.get("LITELLM_BASE", "http://litellm-proxy.litellm-product.svc.cluster.local:4000")
MK = os.environ["LITELLM_MK"]
MAX_REAP = int(os.environ.get("MAX_REAP", "3"))
FLOOR = int(os.environ.get("POOL_FLOOR", "15"))
NS = "litellm-product"


API_RETRIES = int(os.environ.get("API_RETRIES", "4"))
API_BACKOFF = float(os.environ.get("API_BACKOFF", "5"))


def _open(req):
    """打一次 litellm，把**传输层**错误（连接被拒/超时/DNS）当可重试，HTTP 状态码原样抛。

    2026-09-19：litellm-proxy 侧一次 NetworkPolicy 变更把集群内 Pod→Pod 那条腿切了 3.5h，
    这里的 urlopen 直接抛 URLError [Errno 111]，而调用方只 catch HTTPError ⇒ 整轮 run
    连一行业务日志都没打就带 40 行 traceback 退出（12:43Z/13:13Z/13:43Z 三轮全废）。
    重试不解决网络问题，但能吃掉滚动更新/重启这种秒级抖动 —— 一轮 run 要跑 8 分半，
    为一次 2 秒的抖动全废不合算。
    """
    last = None
    for attempt in range(API_RETRIES):
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError:
            raise  # 状态码是业务信号（404 要走下一个 prefix），不重试
        except (urllib.error.URLError, OSError, socket.timeout) as e:
            last = e
            if attempt < API_RETRIES - 1:
                wait = API_BACKOFF * (attempt + 1)
                print(f"  api transport error ({type(e).__name__}: {e}); "
                      f"retry {attempt + 1}/{API_RETRIES - 1} in {wait:.0f}s", flush=True)
                time.sleep(wait)
    raise TransportDown(f"{type(last).__name__}: {last}")


class TransportDown(RuntimeError):
    """litellm 在传输层不可达 —— 不是业务判定，本轮不做任何摘除。"""


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {MK}"}
    if data:
        headers["Content-Type"] = "application/json"
    for prefix in ("/pro", ""):
        req = urllib.request.Request(LITELLM + prefix + path, data=data, headers=headers, method=method)
        try:
            return _open(req)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
    raise RuntimeError(f"both /pro{path} and {path} returned 404")


def probe(n, with_tools):
    url = f"http://zero-{n}.{NS}.svc.cluster.local:8200/v1/responses"
    body = {"model": "gpt-5-5", "input": "hi", "stream": True, "max_output_tokens": 16}
    if with_tools:
        body["tools"] = [{"type": "function", "name": "noop", "description": "no-op",
                          "parameters": {"type": "object", "properties": {}}}]
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer raw"})
    try:
        with urllib.request.urlopen(req) as r:
            r.read(120)
            return "OK", ""
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="ignore")[:200]
        auth_dead = e.code in (401, 403) or ("401" in text and ("expired" in text or "invalid" in text.lower()))
        return ("AUTH_DEAD" if auth_dead else f"HTTP_{e.code}"), text.replace("\n", " ")
    except Exception as e:
        return type(e).__name__, str(e)[:120]


def main():
    info = api("GET", "/model/info")
    zk = {}
    for m in info.get("data", []):
        mid = (m.get("model_info") or {}).get("id", "")
        mt = re.match(r"^zk-(\d+)-", mid)
        if mt:
            zk.setdefault(int(mt.group(1)), []).append(mid)
    pods = sorted(zk)
    print(f"pool members: {len(pods)} pods, {sum(len(v) for v in zk.values())} entries")

    dead, web_dead, other = [], [], []
    for n in pods:
        st, detail = probe(n, with_tools=True)
        if st == "AUTH_DEAD":
            time.sleep(20)
            st2, detail2 = probe(n, with_tools=True)
            if st2 == "AUTH_DEAD":
                dead.append(n)
                print(f"zero-{n}: CODEX DEAD (2x auth fail) {detail2[:100]}")
                continue
            st, detail = st2, detail2
        if st == "OK":
            wst, wdetail = probe(n, with_tools=False)
            if wst == "AUTH_DEAD":
                web_dead.append(n)
                print(f"zero-{n}: codex OK, WEB session expired (needs re-capture)")
            else:
                print(f"zero-{n}: healthy (codex {st}, web {wst})")
        else:
            other.append(n)
            print(f"zero-{n}: SUSPECT {st} {detail[:100]} (no action)")

    healthy = len(pods) - len(dead)
    if dead and healthy < FLOOR:
        print(f"ABORT reap: healthy={healthy} < floor={FLOOR}")
        dead = []
    for n in dead[:MAX_REAP]:
        for mid in zk[n]:
            try:
                api("POST", "/model/delete", {"id": mid})
                print(f"  - deleted {mid}")
            except Exception as e:
                print(f"  ! delete {mid} failed: {e}")
    if len(dead) > MAX_REAP:
        print(f"deferred (MAX_REAP={MAX_REAP}): {dead[MAX_REAP:]}")
    print(f"summary: reaped={dead[:MAX_REAP]} web_dead={web_dead} suspect={other}")


if __name__ == "__main__":
    try:
        main()
    except TransportDown as e:
        # 仍然以非 0 退出（reaper 确实没干活，这个信号不能丢），但只留一行可读的原因，
        # 不要 40 行 traceback —— traceback 里看不出「打的是 litellm 还是 zero-N」。
        print(f"FATAL litellm unreachable at {LITELLM} after {API_RETRIES} attempts: {e}; "
              f"no reap performed this run", flush=True)
        raise SystemExit(1)
