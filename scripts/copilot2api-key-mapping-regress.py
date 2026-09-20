#!/usr/bin/env python3
"""198 litellm-product:Anthropic 系列产品名 → copilot2api 映射回归。

覆盖 key(见 EXPECT_BY_KEY):
  - carher-1                        裸产品名 claude-*
  - claude-code-liuguoxian02-7w7g   前缀产品名 anthropic.claude-* + fable5

判据纪律
--------
* 阳性对照:非 anthropic 产品名必须仍走原通道,证明尺子能区分。
* 归属证据:不认 content、不认响应头(198 网关脱敏,api-base 一律 "-"、group 是哈希),
  只认 SpendLogs 的 model_id,再经 pod 内 /model/info 解析成 api_base 判落在哪条腿。
  **不许用 model_id 的字符串前缀判归属** —— 前缀只在 c1 那批 host 前缀腿上成立,
  clone 出来的腿是自动 UUID(09-10 实测 c4/c5 各 21 条腿 0 条带前缀)。
* 一个产品名一把临时 key:归属靠 api_key 哈希(网关自己写,被测方伪造不了),
  不靠 end_user nonce —— 后者实测会错位,见 spendlog_by_apikey 的注释。
* 复刻 key:目标 key 的 sk- 不可回读,临时 key 逐字段复刻其 models+aliases;
  用 master key 直发是不合格对照(它不走 per-key alias 这条链路)。

用法
----
  python3 scripts/copilot2api-key-mapping-regress.py
  python3 scripts/copilot2api-key-mapping-regress.py --keys carher-1 --json out.json
"""
import argparse, base64, datetime, json, subprocess, sys, time, urllib.parse, urllib.request, uuid

BASE = "https://cc.auto-link.com.cn/pro"
SSH = ["sshpass", "-p", "Hn8#mKLp3QxZ", "ssh", "-o", "StrictHostKeyChecking=no",
       "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]

# 产品名(调用侧不变) -> 期望落到的 model group(copilot2api 承载)
EXPECT_BY_KEY = {
    "carher-1": {
        "claude-opus-4-8":   "claude-opus-4.8",
        "claude-sonnet-5":   "claude-sonnet-5-copilot",
        "claude-haiku-4-5":  "claude-haiku-4.5",
        "claude-opus-5":     "claude-opus-5",
        "claude-fable-5":    "claude-fable-5",
        "claude-fable-5.1":  "claude-fable-5.1",
    },
    # carher-2 是 carher-1 的完整克隆(models/aliases 逐字段相同)
    "carher-2": {
        "claude-opus-4-8":   "claude-opus-4.8",
        "claude-sonnet-5":   "claude-sonnet-5-copilot",
        "claude-haiku-4-5":  "claude-haiku-4.5",
        "claude-opus-5":     "claude-opus-5",
        "claude-fable-5":    "claude-fable-5",
        "claude-fable-5.1":  "claude-fable-5.1",
    },
    "claude-code-buyitian": {
        "anthropic.claude-opus-4-8":   "claude-opus-4.8",
        "anthropic.claude-opus-4-7":   "claude-opus-4.7",
        "anthropic.claude-opus-5":     "claude-opus-5",
        "anthropic.claude-sonnet-5":   "claude-sonnet-5-copilot",
        "anthropic.claude-haiku-4-5":  "claude-haiku-4.5",
        "anthropic.claude-fable-5":    "claude-fable-5",
        "fable5":                      "claude-fable-5",
        "anthropic.claude-fable-5.1":  "claude-fable-5.1",
        "fable5.1":                    "claude-fable-5.1",
    },
    "claude-code-liuguoxian02-7w7g": {
        "anthropic.claude-opus-4-8":   "claude-opus-4.8",
        "anthropic.claude-opus-4-7":   "claude-opus-4.7",
        "anthropic.claude-sonnet-5":   "claude-sonnet-5-copilot",
        "anthropic.claude-haiku-4-5":  "claude-haiku-4.5",
        "anthropic.claude-opus-5":     "claude-opus-5",
        "anthropic.claude-fable-5":    "claude-fable-5",
        "fable5":                      "claude-fable-5",
        "anthropic.claude-fable-5.1":  "claude-fable-5.1",
        "fable5.1":                    "claude-fable-5.1",
    },
}
# 阳性对照:必须**不**落 copilot2api。取该 key 本就有权限的非 anthropic 模型。
CONTROL_BY_KEY = {
    # claude-opus-4-6:上游 2026-09-01 下架该模型,产品名已回指网宿。
    # 放进 control 而不是删掉 —— 它必须继续可用,且必须**不**落 copilot2api。
    # claude-opus-4-6 / claude-sonnet-4-6:上游 2026-09-01/02 先后下架,产品名已回指
    # 第三方通道。放进 control 而非删掉 —— 它们必须继续可用,且必须**不**落 copilot2api。
    "carher-1": ["gpt-5.6-sol", "gpt-5.6-luna", "claude-opus-4-6", "claude-sonnet-4-6"],
    "carher-2": ["gpt-5.6-sol", "gpt-5.6-luna", "claude-opus-4-6", "claude-sonnet-4-6"],
    "claude-code-buyitian": ["claude-glm-5.3", "claude-deepseek-v4-flash",
                            "anthropic.claude-opus-4-6", "anthropic.claude-sonnet-4-6"],
    "claude-code-liuguoxian02-7w7g": ["claude-zerokey-gpt-5.6-sol", "claude-glm-5.3",
                                      "anthropic.claude-opus-4-6",
                                      "anthropic.claude-sonnet-4-6"],
}


def master_key():
    out = subprocess.run(SSH + ['echo "Hn8#mKLp3QxZ" | sudo -S sh -c '
                                '"kubectl -n litellm-product get secret litellm-secrets '
                                '-o jsonpath={.data.LITELLM_MASTER_KEY}"'],
                         capture_output=True, text=True, timeout=60)
    return base64.b64decode(out.stdout.strip().split("\n")[-1]).decode()


# model_id -> (model_name, api_base)。归属只能按 api_base 判:
#   * 组名会撒谎(见记忆 feedback_litellm_group_name_lies_judge_by_api_base);
#   * id 前缀更不能当判据 —— 老版本判 `model_id.startswith("copilot2api/")`,那只对
#     c1 那批 host 前缀腿成立;09-04 c1 退池后 c4/c5 的 openai 腿全是自动 UUID,
#     于是六个产品名恒 FAIL(假红,实测 c4 单腿时期同样 0 条带该前缀)。
# 必须在 pod 内取:api_base 在 DB 是加密列,公网网关那份 /model/info 也脱敏。
_ARM_DUMP = (
    "import json,os,urllib.request\n"
    "r=urllib.request.Request('http://127.0.0.1:4000/v1/model/info',"
    "headers={'Authorization':'Bearer '+os.environ['MK']})\n"
    "d=json.load(urllib.request.urlopen(r,timeout=30))['data']\n"
    "o={str(m['model_info']['id']):[m['model_name'],"
    "str(m.get('litellm_params',{}).get('api_base') or '')]"
    " for m in d if m.get('model_info',{}).get('id')}\n"
    "print('ARMMAP'+json.dumps(o))\n"
)


def arm_map():
    b64 = base64.b64encode(_ARM_DUMP.encode()).decode()
    remote = (
        'echo "Hn8#mKLp3QxZ" | sudo -S sh -c \''
        'POD=$(kubectl -n litellm-product get pod -l app=litellm-proxy '
        '-o jsonpath="{.items[0].metadata.name}"); '
        'MK=$(kubectl -n litellm-product get secret litellm-secrets '
        '-o jsonpath="{.data.LITELLM_MASTER_KEY}" | base64 -d); '
        f'kubectl -n litellm-product exec "$POD" -- sh -c '
        f'"echo {b64} | base64 -d > /tmp/armmap.py; MK=$MK python3 /tmp/armmap.py"\''
    )
    out = subprocess.run(SSH + [remote], capture_output=True, text=True, timeout=180)
    for line in out.stdout.splitlines():
        if line.startswith("ARMMAP"):
            return json.loads(line[len("ARMMAP"):])
    print("    [warn] arm_map empty — attribution falls back to id prefix")
    return {}


def api(mk, method, path, body=None, timeout=120, retries=3):
    """公网链路会偶发超时/瞬时 4xx —— 单次失败不算判据,重试后仍失败才算。"""
    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + mk, "Content-Type": "application/json"},
            method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode()), dict(r.headers)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            last = f"HTTP {e.code}: {detail}"
            if e.code < 500 and "rate" not in detail.lower():
                if attempt == retries - 1:
                    raise RuntimeError(f"{method} {path} -> {last}") from None
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{method} {path} failed after {retries}: {last}")


def find_key(mk, alias):
    for page in range(1, 20):
        d, _ = api(mk, "GET", f"/key/list?return_full_object=true&size=100&page={page}")
        for k in d.get("keys", []):
            if isinstance(k, dict) and k.get("key_alias") == alias:
                return k
        if page >= d.get("total_pages", 1):
            break
    return None


def chat(key, model, stream=False, tools=False, kwargs_no_required=False):
    body = {"model": model,
            "messages": [{"role": "user", "content": "reply with the single word OK"}],
            "max_tokens": 64}
    if stream:
        body["stream"] = True
        body["messages"] = [{"role": "user", "content": "count 1 to 3"}]
    if tools:
        body["messages"] = [{"role": "user", "content": "What's the weather in Paris? Use the tool."}]
        body["tools"] = [{"type": "function", "function": {
            "name": "get_weather", "description": "get weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}]
        if not kwargs_no_required:
            body["tool_choice"] = "required"
    req = urllib.request.Request(
        BASE + "/v1/chat/completions", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": f"{type(e).__name__}: {e}"[:200],
                "ms": int((time.time() - t0) * 1000)}
    res = {"ok": True, "ms": int((time.time() - t0) * 1000)}
    if stream:
        chunks = [l for l in raw.splitlines() if l.startswith("data: ") and "[DONE]" not in l]
        text = ""
        for c in chunks:
            try:
                text += (json.loads(c[6:])["choices"][0].get("delta", {}) or {}).get("content") or ""
            except Exception:  # noqa: BLE001
                pass
        res.update(chunks=len(chunks), text=text[:80])
        res["ok"] = len(chunks) > 1 and bool(text.strip())
    else:
        try:
            d = json.loads(raw)
            msg = d["choices"][0]["message"]
            res.update(model_echo=d.get("model"), text=(msg.get("content") or "")[:80],
                       tool_calls=[t["function"]["name"] for t in (msg.get("tool_calls") or [])])
            res["ok"] = bool(res["text"].strip()) or bool(res["tool_calls"])
        except Exception as e:  # noqa: BLE001
            res.update(ok=False, err=f"parse: {e}", raw=raw[:200])
    return res


def chat_retry(key, model, tries=3, **kw):
    """公网抖动(urlopen timeout)不是被测对象的失败 —— 重试后仍失败才判红。"""
    last = None
    for i in range(tries):
        r = chat(key, model, **kw)
        if r["ok"]:
            if i:
                r["retried"] = i
            return r
        last = r
        time.sleep(3)
    last["retried"] = tries
    return last


def tool_probe(key, model):
    """工具调用探针。先 tool_choice:"required",被拒(400)则退回 auto。

    ⚠ 2026-09-01 实测:claude-fable-5.1 对 tool_choice:"required" 稳定 400
      (同一上游的 opus-4.8/opus-5/sonnet-5/haiku-4.5 都接受),而 auto 下它
      正常发出 tool_call。这是**该模型的参数兼容性**,不是映射错。
      判据写死 required 会把一个能用的模型判成坏的。
    """
    r = chat_retry(key, model, tools=True)
    if r["ok"]:
        return r
    r2 = chat_retry(key, model, tools=True, kwargs_no_required=True)
    r2["required_rejected"] = True
    return r2


def spendlog_by_apikey(mk, token_hashes, since_utc):
    """归属证据:SpendLogs 按 api_key(token 哈希)关联,取 model_id。

    ⚠ 为什么不用 end_user nonce —— 2026-09-01 把这条路走死了:
      把 nonce 塞进请求体 user 字段后,SpendLogs 的 end_user **会错位**:
      实见 copilot2api/claude-sonnet-4.6 的行挂着 ...opus-4-8 的 nonce,
      也有 end_user 为空的行。连跑三轮,每轮报红的是**不同**的模型,
      而定向重测证明它们全部落得完全正确。判据自己在漂 = 尺子坏了,不是货坏了。
      改成一个产品名一把临时 key 后,api_key 哈希由网关自己写,零歧义。

    ⚠ 另两个坑同样制造过假 FAIL:
      - /spend/logs?request_id=<x-litellm-call-id> 永远返回 []:SpendLogs 记的
        request_id 是上游的 msg_*/resp_*,与响应头 call-id 是两个命名空间。
      - start_date/end_date 按 **UTC** 解析(填北京时间查空);且分页必须走完
        total_pages —— 这台机器半小时窗口有 4000+ 行 = 40 页,只翻 5 页会大面积漏。
    """
    s = since_utc.strftime("%Y-%m-%d %H:%M:%S")
    e = (datetime.datetime.now(datetime.timezone.utc)
         + datetime.timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")
    want, found, page = set(token_hashes), {}, 1
    while page <= 200:
        d, _ = api(mk, "GET", f"/spend/logs/ui?start_date={urllib.parse.quote(s)}"
                              f"&end_date={urllib.parse.quote(e)}&page={page}&page_size=100")
        for r in d.get("data", []):
            ak = r.get("api_key")
            if ak in want and ak not in found:
                found[ak] = r
        if page >= d.get("total_pages", 1) or len(found) == len(want):
            break
        page += 1
    return found


def spendlog_wait(mk, token_hashes, since_utc, tries=6, gap=20):
    """SpendLogs 异步落库,实测最长 >60s。轮询到齐或超时,不一次定生死。"""
    got = {}
    for i in range(tries):
        time.sleep(gap)
        got.update(spendlog_by_apikey(mk, token_hashes, since_utc))
        missing = [t for t in token_hashes if t not in got]
        print(f"      [poll {i + 1}/{tries}] got={len(got)}/{len(token_hashes)}"
              + (f" missing={len(missing)}" if missing else ""))
        if not missing:
            break
    return got


def run_one(mk, key_alias):
    EXPECT, CONTROL = EXPECT_BY_KEY[key_alias], CONTROL_BY_KEY[key_alias]
    print(f"\n{'=' * 66}\n### KEY: {key_alias}\n{'=' * 66}")

    # ---- A. 静态门:aliases 已落盘(只证明写进去了,不证明生效)
    k = find_key(mk, key_alias)
    assert k, f"{key_alias} not found on 198"
    al = k["aliases"]
    static_bad = [f"{p}->{al.get(p)}" for p, g in EXPECT.items() if al.get(p) != g]
    print(f"\n[A] key aliases: {'PASS' if not static_bad else 'FAIL ' + str(static_bad)}")
    for p, g in EXPECT.items():
        print(f"    {p:<30} -> {al.get(p)}")

    results = {"static": {"pass": not static_bad, "bad": static_bad, "aliases": al},
               "probes": {}, "control": {}}
    run = str(int(time.time()))
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
    keys, tok = {}, {}
    names = list(EXPECT) + CONTROL

    try:
        # ---- B. 一个产品名一把临时 key,逐字段复刻目标 key
        print(f"\n[B] 建 {len(names)} 把临时 key(复刻 models={len(k['models'])} "
              f"aliases={len(al)})")
        for i, pname in enumerate(names):
            ta = f"tmpreg-{run}-{i}-{uuid.uuid4().hex[:6]}"
            gen, _ = api(mk, "POST", "/key/generate",
                         {"key_alias": ta, "models": k["models"], "aliases": al,
                          "max_budget": 3.0})
            keys[pname] = gen["key"]
            tok[pname] = find_key(mk, ta)["token"]

        # ---- C. 三形态实调:非流式 / 流式 / 强制工具调用
        print("\n[C] live probes")
        for pname in EXPECT:
            row = {"nonstream": chat_retry(keys[pname], pname),
                   "stream": chat_retry(keys[pname], pname, stream=True),
                   "tools": tool_probe(keys[pname], pname)}
            results["probes"][pname] = row
            print(f"    {pname:<30} ns={row['nonstream']['ok']} "
                  f"st={row['stream']['ok']}({row['stream'].get('chunks')}) "
                  f"tc={row['tools'].get('tool_calls')}")

        # ---- D. 阳性对照:非 anthropic 名不许被改道
        print("\n[D] control (must NOT be copilot2api)")
        for pname in CONTROL:
            r = chat_retry(keys[pname], pname)
            results["control"][pname] = r
            print(f"    {pname:<30} ok={r['ok']}")

        # ---- E. 归属证据
        print("\n[E] SpendLogs attribution")
        logs = spendlog_wait(mk, list(tok.values()), since)
        arms = arm_map()
        results["spendlogs"] = {}
        for pname in names:
            row = logs.get(tok[pname], {})
            mid = row.get("model_id", "MISSING")
            want_cp = pname in EXPECT
            group, api_base = arms.get(str(mid), ["", ""])
            if arms:
                is_cp = "copilot2api" in api_base
                leg = api_base.split("//")[-1].split("/")[0].split(":")[0] or "?"
            else:  # 拿不到 arm map 时退回老判据,并在行里标明尺子降级
                is_cp = str(mid).startswith("copilot2api/")
                leg = "(no-armmap)"
            bad_group = want_cp and group and group != EXPECT[pname]
            verdict = "PASS" if (is_cp == want_cp and not bad_group) else "FAIL"
            extra = f" group={group} want={EXPECT[pname]}" if bad_group else ""
            print(f"    {pname:<30} leg={leg:<46} -> {verdict}{extra}")
            results["spendlogs"][pname] = {"model_id": mid, "model_group": row.get("model_group"),
                                           "arm_group": group, "api_base": api_base,
                                           "spend": row.get("spend"), "verdict": verdict}
    finally:
        dead = [t for t in tok.values()]
        if dead:
            api(mk, "POST", "/key/delete", {"keys": dead})
            print(f"\n[cleanup] {len(dead)} temp keys deleted")

    fails = (static_bad
             + [p for p in EXPECT
                if not all(results["probes"][p][x]["ok"] for x in ("nonstream", "stream", "tools"))]
             + [p for p, v in results.get("spendlogs", {}).items() if v["verdict"] == "FAIL"])
    results["fails"] = sorted(set(fails))
    print(f"\n---- {key_alias}: "
          f"{'ALL PASS' if not fails else 'FAIL: ' + str(results['fails'])} ----")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write full result json here")
    ap.add_argument("--keys", nargs="*", default=list(EXPECT_BY_KEY))
    args = ap.parse_args()

    mk = master_key()
    print(f"master key ok ({mk[:12]}...)")

    allres = {ka: run_one(mk, ka) for ka in args.keys}

    if args.json:
        json.dump(allres, open(args.json, "w"), ensure_ascii=False, indent=2)
        print(f"\njson -> {args.json}")

    bad = {ka: r["fails"] for ka, r in allres.items() if r["fails"]}
    print(f"\n==== {'ALL PASS' if not bad else 'FAIL: ' + json.dumps(bad, ensure_ascii=False)} ====")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
