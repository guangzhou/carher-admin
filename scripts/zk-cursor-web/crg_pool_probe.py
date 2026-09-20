#!/usr/bin/env python3
"""crg_pool_probe.py —— cr-g 池的形状验收：临时真 key + **复刻 Cursor 真实线型**。

为什么不能用现成的 lane82_regress_probe.sh：它打 `/pro/v1/responses`，而真 Cursor 对本线
一律发 `/v1/chat/completions`（反编译 3.16.29 的端点决策：非 api.openai.com host 且名不含
codex → chat），能走正路全靠 litellm 的 chat→responses 桥。**用 /v1/responses 探针测出来的
绿是假绿**：它绕开了桥，桥断了也照样绿。

三条纪律刻在这里：
  · **必须临时真 key**（`/key/generate` → 打 → `/key/delete`）。master key 绕过 per-key
    鉴权和 openai/ 的 api_key gate，漏了占位符也照样 200 —— 真用户却 401。
  · **body 绝不带 `reasoning_effort`**。真 Cursor 不发它，档位由模型行的配置提供；
    探针补上 = 把「配置漏了档位」这种红构造性屏蔽掉。
  · **判据建立在收割后的文本上**，不是原始响应串。答案按 delta 分片回来，暗号会被劈开
    （2026-09-02 serve_check 就这么假红过一次）。

落点判定：**只认 SpendLogs 的 `model_id`**（litellm 自己逐发记的账）。09-02 实测响应头这条路
是死的 —— `x-litellm-model-id` 回的是内部句柄 `3974894ba1bb`，`x-litellm-model-api-base` 被 198
对外脱敏成 `-`。SpendLogs 是异步写的，查不到先当"还没落盘"重查，**别当成"没落地"**；等到齐
不了就照实说对不上，不拿不完整的分布去下「每条腿都被选中过」的结论。

用法：
  python3 crg_pool_probe.py cr-g-5.6 --repeat 8          # 一个名字打 8 发
  python3 crg_pool_probe.py --all --repeat 1             # 14 名各一发
  python3 crg_pool_probe.py --all --keys 4               # 4 把 key 轮换，验分流覆盖

**`--keys` 是验「每条腿都被选中过」的唯一办法**：weighted_affinity 是 **key 级亲和**
（Cursor 不发 session 头），同一把 key 会被钉死在同一条 pod 上。拿单把 key 打 100 发全落
一条 lane，那不是池坏了，是我用错了尺子 —— 换 key 才换腿。
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
SUDO = "sudo -n kubectl -n %s " % NS
def _require_env(name):
    """凭据只从环境变量读，缺了直接退出。

    不设内置默认值：写死一个真 PG 口令等于把凭据提交进仓库，而且口令轮转后
    老默认值还会静默生效，打出来的认证失败看不出是"忘了设 env"还是"口令真的换了"。
    """
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(
            "缺少环境变量 %s —— 先 export %s=<litellm-db-0 的 PG 口令>（别写进文件/命令行历史）"
            % (name, name)
        )
    return v


PG_PW = _require_env("LITELLM_PG_PW")
ENDPOINT = "https://cc.auto-link.com.cn/pro/v1/chat/completions"
# 池腿。**2026-09-02 傍晚换过一次**：原来是 81/83/84/85，但 81/83/85 背后的账号是 free 档
# （`/backend-api/models` 只有 10 个 slug，没有 thinking/pro/instant），物理上答不出菜单里
# 大多数名字。已摘腿 + 删 deploy/svc。现在是 84 + 135~140 七条 pro 腿。
# **这张表必须跟着真实拓扑走**：它是"每条腿都被选中过"那条判据的基准，
# 拿旧腿表去判 = 假绿（漏掉的腿从没被证明过，删掉的腿永远等不到）。
POOL_LANES = ["84", "135", "136", "137", "138", "139", "140"]   # 82 是 canary，不在池里
POOL_NAMES = [
    "cr-g-5.6", "cr-g-5.6-instant", "cr-g-5.6-mini", "cr-g-5.6-t-mini",
    "cr-g-5.6-pro", "cr-g-research", "cr-g-5.6-thinking", "cr-g-5.6-thinking-min",
    "cr-g-5.6-thinking-high", "cr-g-5.6-thinking-max",
    "cr-g-5.6-luna", "cr-g-5.6-luna-min", "cr-g-5.6-luna-high", "cr-g-5.6-luna-max",
]
# 真 Cursor 每发都带一大票工具；带 1 个足以让 litellm 的 chat→responses 桥成立
# （桥要求 tools 非空）。工具本身不需要被调用，这里验的是"这条线通不通"。
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a file from the workspace",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                   "required": ["path"]}}}]


def proxy_post(path, payload):
    """在 proxy pod 内打它自己的 localhost:4000。

    两个设计选择都是踩出来的：
      · **master key 只从 pod 自己的 env 取**，绝不经我的 argv / 命令串 —— ps 能看见 argv。
      · **exec 打 `deploy/litellm-proxy` 而不是具体 pod 名**：pod 名一 rollout 就过期，
        09-02 就是拿着 restart 前的名字去 exec，查询返回空、本地 JSON 解析崩。

    程序整份走 stdin 给 `python3 -`，payload 作为字面量内嵌（里面没有凭据，只有模型名和
    alias）—— 不往 ssh 的引号命令串里塞带引号的代码。
    """
    prog = (
        "import urllib.request, json, os\n"
        "p = json.loads(%r)\n"
        "req = urllib.request.Request('http://localhost:4000/%s',\n"
        "    data=json.dumps(p).encode(),\n"
        "    headers={'Authorization':'Bearer '+os.environ['LITELLM_MASTER_KEY'],\n"
        "             'Content-Type':'application/json'})\n"
        "print(json.dumps(json.load(urllib.request.urlopen(req, timeout=30))))\n"
        % (json.dumps(payload), path))
    r = subprocess.run(
        SSH + ["sudo -n kubectl -n %s exec -i deploy/litellm-proxy -- python3 -" % NS],
        input=prog, capture_output=True, text=True, timeout=120)
    out = [l for l in r.stdout.splitlines() if l.startswith("{")]
    if not out:
        raise SystemExit("proxy_post %s 失败:\n  stdout=%s\n  stderr=%s"
                         % (path, r.stdout[-400:], r.stderr[-400:]))
    return json.loads(out[-1])


def harvest(raw):
    """SSE → 完整文本。判据必须建立在这上面，不是 raw 串。"""
    text = ""
    if "data:" in raw:
        for ln in raw.splitlines():
            if not ln.startswith("data:"):
                continue
            p = ln[5:].strip()
            if not p or p == "[DONE]":
                continue
            try:
                c = (json.loads(p).get("choices") or [{}])[0]
                d = c.get("delta") or {}
                if isinstance(d.get("content"), str):
                    text += d["content"]
                elif isinstance((c.get("message") or {}).get("content"), str):
                    text += c["message"]["content"]
            except Exception:
                pass
    else:
        try:
            text = ((json.loads(raw).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except Exception:
            pass
    return text


def one_shot(key, model, i):
    nonce = "CRG-%d-%d" % (int(time.time()), i)
    body = {"model": model, "stream": True, "tools": TOOLS,
            "messages": [{"role": "user",
                          "content": "请原样输出这一行，不要加别的字：%s" % nonce}]}
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read().decode("utf-8", "replace")
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            code = r.status
    except urllib.error.HTTPError as e:
        raw, hdrs, code = e.read().decode("utf-8", "replace"), {}, e.code
    except Exception as e:
        return {"ok": False, "code": "ERR", "lane": "?", "dt": time.time() - t0,
                "why": "%s: %s" % (type(e).__name__, e)}
    text = harvest(raw)
    # 落点判据 = SpendLogs，**不是响应头**。09-02 实测头里那两个字段都不可用：
    #   x-litellm-model-id       = '3974894ba1bb'（内部句柄，不是我设的 model_info.id）
    #   x-litellm-model-api-base = '-'（198 对外脱敏，svc 地址不外泄）
    # 这里仍然解析一次，是为了「哪天头变得可用了能自动受益」；判据落在 spendlog_lanes()。
    mid = hdrs.get("x-litellm-model-id", "")
    abase = hdrs.get("x-litellm-model-api-base", "")
    lane = "?"
    if "zero-cursor-bpi" in abase:
        tail = abase.split("zero-cursor-bpi")[1]
        lane = tail.split(".")[0].lstrip("-") or "101"
    elif "zerokey-cr-g-" in mid:
        lane = mid.split("zerokey-cr-g-")[1].split("-")[0]
    return {"ok": nonce in text, "code": code, "lane": lane, "dt": time.time() - t0,
            "mid": mid, "abase": abase, "hdrs": hdrs, "nonce": nonce,
            "why": "" if nonce in text else
            ("text=%r raw_head=%r" % (text[:80], raw[:160]))}


def spendlog_lanes(token_hash, since_ts):
    """从 SpendLogs 反查每一发落在哪条 lane —— **落点的权威源**。

    为什么不能只靠响应头：198 网关对外做过错误/头脱敏，`x-litellm-model-id` 这类
    `llm_provider-*` 家族的头会被剥掉（记忆 feedback_litellm_provider_prefixed_headers_leak_upstream_state）。
    头缺失时不能当"落点未知"放过 —— 那等于本轮没验分流。

    SpendLogs 的 `model_id` 就是 deployment 的 `model_info.id`，逐发一行，是 litellm
    自己记的账，不是我推的。
    """
    sql = ("select \\\"model_id\\\" from \\\"LiteLLM_SpendLogs\\\" "
           "where api_key='%s' and \\\"startTime\\\" > to_timestamp(%d) "
           "order by \\\"startTime\\\";" % (token_hash, since_ts))
    cmd = (SUDO + "exec litellm-db-0 -- env PGPASSWORD='%s' psql -U litellm -d litellm "
           "-At -c \"%s\" 2>/dev/null" % (PG_PW, sql))
    r = subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=120)
    out = []
    for ln in r.stdout.strip().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        lane = ln.split("zerokey-cr-g-")[1].split("-")[0] if "zerokey-cr-g-" in ln else "?"
        out.append((lane, ln))
    return out


def main():
    args = sys.argv[1:]
    rep = int(args[args.index("--repeat") + 1]) if "--repeat" in args else 1
    nkeys = int(args[args.index("--keys") + 1]) if "--keys" in args else 1
    names = POOL_NAMES if "--all" in args else [a for a in args if a.startswith("cr-g-")]
    if not names:
        print("要打哪个名字？给 cr-g-xxx 或 --all")
        return 2

    since = int(time.time()) - 5
    keys = []          # [(alias, key, token_hash)]
    stamp = int(time.time())
    for n in range(nkeys):
        alias = "crgprobe-%d-%d" % (stamp, n)
        m = proxy_post("key/generate",
                       {"models": names, "key_alias": alias, "duration": "30m"})
        keys.append((alias, m["key"], m.get("token") or ""))
    print("临时 scoped key %d 把已 mint（30m 自灭, models=%d 个）" % (len(keys), len(names)))
    print("  " + ", ".join("%s→%s…" % (a, t[:10]) for a, _, t in keys) + "\n")

    bad, first_hdrs, sent = [], None, 0
    try:
        shot = 0
        for name in names:
            for i in range(rep):
                # key 轮换：亲和是 key 级的，同一把 key 永远同一条腿。
                ki = shot % len(keys)
                alias, key, _ = keys[ki]
                shot += 1
                r = one_shot(key, name, i)
                sent += 1
                if first_hdrs is None:
                    first_hdrs = r.get("hdrs") or {}
                print("  %-24s #%d key=%-2d http=%-4s %5.1fs 暗号=%s %s"
                      % (name, i, ki,
                         r["code"], r["dt"],
                         "命中 ✅" if r["ok"] else "未命中 ❌", r.get("why", "")[:110]))
                if not r["ok"]:
                    bad.append((name, i, r.get("why", "")))

        # 落点回查（**删 key 之前**做）。SpendLogs 是异步写的，第一次查常常少几行 ——
        # 少行数不是"没落地"，是还没落盘；重查到齐为止，齐不了就照实说对不上，
        # **不拿一份不完整的分布去下「每条腿都被选中过」的结论**。
        cnt, got, per_key = {}, 0, {}
        for _ in range(8):
            time.sleep(5)
            cnt, got, per_key = {}, 0, {}
            for ki, (_a, _k, th) in enumerate(keys):
                if not th:
                    continue
                for lane, _mid in spendlog_lanes(th, since):
                    cnt[lane] = cnt.get(lane, 0) + 1
                    per_key.setdefault(ki, []).append(lane)
                    got += 1
            if got >= sent:
                break
        print("\n== 落点（SpendLogs 权威，%d 行 / 实发 %d 发）==" % (got, sent))
        for lane in sorted(cnt):
            print("  lane %-4s %d 发" % (lane, cnt[lane]))
        # 逐 key 打印：亲和是 key 级的，出问题时必须能指名道姓说"哪把 key 钉在哪条腿上"，
        # 而不是只给一个聚合分布 —— 聚合分布分不出"某条腿全坏"和"随机偶发"。
        if len(keys) > 1:
            print("  逐 key（亲和把每把 key 钉在一条腿上）:")
            for ki in sorted(per_key):
                ls = per_key[ki]
                print("    key%-2d → %s" % (ki, ",".join(sorted(set(ls)))
                                            + ("  (%d 发)" % len(ls))))
        covered = {l for l in cnt if l != "?"}
        missing = set(POOL_LANES) - covered
        if got < sent:
            print("  ⚠️  等了 40s 仍差 %d 行 —— 别拿这份分布判「每条腿都被选中过」"
                  % (sent - got))
        elif missing:
            print("  ⚠️  这一轮没有任何一发落到 lane %s —— **不能说每条腿都验过**。"
                  "亲和是 key 级的，加 --keys 多开几把再打。" % ",".join(sorted(missing)))
        else:
            print("  ✅ %d 条腿(%s)都被选中过"
                  % (len(POOL_LANES), ",".join(sorted(POOL_LANES, key=int))))
    finally:
        for alias, _k, _t in keys:
            try:
                proxy_post("key/delete", {"key_aliases": [alias]})
            except SystemExit as e:
                print("⚠️ 临时 key %s 没删干净，手动 /key/delete（%s）" % (alias, e))
        print("\n临时 key %d 把已删" % len(keys))

    if first_hdrs is not None:
        print("（落点头不可用是已知的: model-id=%r api-base=%r —— 判据在 SpendLogs）"
              % (first_hdrs.get("x-litellm-model-id", ""),
                 first_hdrs.get("x-litellm-model-api-base", "")))
    print("失败 %d 发 / 共 %d 发" % (len(bad), sent))
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
