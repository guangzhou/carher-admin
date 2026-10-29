#!/usr/bin/env python3
"""198 号池 router 腿(arms)审计 —— 在 198 本机跑(proxy 只在 127.0.0.1:30402 / pod IP 可达)。

    K=<litellm-secrets-r2 master key> python3 - <cmd> ... < chatgpt-pool-198-arms.py

子命令:
    pick-ref <N>                   选参照号:除 N 外 unique arms 最多的号(并列取编号最大)
    audit <N> <REF> <url>...       逐个 proxy 副本数 N/REF 的 unique arms,列坏行和缺的腿
    missing <N> <REF> <url>        机读:N 相对 REF 缺的腿,每行 `slug group leaf in out max_in max_out`
    clone <N> <REF> <url> [--apply]  照 REF 逐字段克隆 N 缺的腿(env AK=chatgpt-pool-master-key)

判据只按 unique `model_info.id` 数(/model/info 是 alias 展开的,数行必错)。
坏行 = id 以 chatgpt-acct-<N>- 开头但 api_base 不指向 chatgpt-acct-<N> 的 svc,
或任何 `chatgpt-acct-acct-*`(2026-10-05 把 `acct-207` 当 targets 喂给 add-codex-model 造出来的)。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

K = os.environ.get("K", "")
NODEPORT = "http://127.0.0.1:30402"


def info(url):
    req = urllib.request.Request(url.rstrip("/") + "/model/info",
                                 headers={"Authorization": "Bearer " + K})
    return json.loads(urllib.request.urlopen(req, timeout=90).read())["data"]


def arms(data):
    """{acct: {suffix: entry}},一个 id 只记一次(取 model_name == litellm model 去前缀那条优先)。"""
    out = {}
    for e in data:
        mid = (e.get("model_info") or {}).get("id", "")
        m = re.match(r"^chatgpt-acct-(\d+)-(.+)$", mid)
        if not m:
            continue
        a, suf = m.group(1), m.group(2)
        cur = out.setdefault(a, {})
        # 同一 id 会以多个 alias 名重复出现(gpt-5.5 / claude-gpt-5.5 …),克隆要的是真组名:
        # 优先 chatgpt-<suffix>,其次 == leaf 名,否则先到先得
        leaf = (e.get("litellm_params") or {}).get("model", "").replace("openai/", "", 1)
        rank = lambda x: (x.get("model_name") == "chatgpt-" + suf) * 2 + (x.get("model_name") == leaf)
        if suf not in cur or rank(e) > rank(cur[suf]):
            cur[suf] = e
    return out


def bad_ids(data, n):
    bad = set()
    for e in data:
        mid = (e.get("model_info") or {}).get("id", "")
        base = (e.get("litellm_params") or {}).get("api_base", "")
        if mid.startswith("chatgpt-acct-acct-"):
            bad.add(mid)
        elif mid.startswith("chatgpt-acct-%s-" % n) and "//chatgpt-acct-%s." % n not in base:
            bad.add(mid)
    return sorted(bad)


def row_params(e):
    lp, mi = e.get("litellm_params") or {}, e.get("model_info") or {}
    pick = lambda k: lp.get(k) if lp.get(k) is not None else mi.get(k)
    return (lp.get("model", "").replace("openai/", "", 1), pick("input_cost_per_token"),
            pick("output_cost_per_token"), mi.get("max_input_tokens"), mi.get("max_output_tokens"))


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "pick-ref":
        n = args[0]
        a = arms(info(NODEPORT))
        cand = sorted(((len(v), int(k)) for k, v in a.items() if k != n), reverse=True)
        print(cand[0][1] if cand else "")
        return
    n, ref, urls = args[0], args[1], args[2:]
    if cmd == "missing":
        a = arms(info(urls[0]))
        mine, theirs = a.get(n, {}), a.get(ref, {})
        for suf in sorted(set(theirs) - set(mine)):
            leaf, i, o, mi_, mo = row_params(theirs[suf])
            print(suf, theirs[suf].get("model_name"), leaf, i, o, mi_, mo)
        return
    if cmd == "clone":
        # 照参照号逐字段克隆缺的腿(价格/窗口/leaf 名都从参照行读,不手填)。
        # 不走 add-codex-model.py:它的 leaf 恒为 openai/chatgpt-<slug>,
        # 对 gpt-5.3-codex 这种 leaf=…-spark 的腿会写错;且 targets 要裸号,写 acct-N 造坏行。
        apply = "--apply" in urls
        urls = [u for u in urls if u != "--apply"]
        ak = os.environ.get("AK", "")
        if not ak:
            sys.exit("AK(chatgpt-pool-master-key)为空 —— 漏了叶子会回 No connected db.")
        a = arms(info(urls[0]))
        mine, theirs = a.get(n, {}), a.get(ref, {})
        todo = sorted(set(theirs) - set(mine))
        # ONLY=gpt-6-luna,...:只克隆这些腿。free 号上游不支持 gpt-6-sol / 6.1-sol
        # ("not supported when using Codex with a ChatGPT account"),整组克隆会建出必 400 的腿。
        only = [s for s in os.environ.get("ONLY", "").split(",") if s]
        if only:
            todo = [s for s in todo if s in only]
        print("clone %s←%s todo=%s" % (n, ref, todo))
        bad = dup = 0
        for suf in todo:
            e = theirs[suf]
            leaf, i, o, mi_, mo = row_params(e)
            ref_mi = e.get("model_info") or {}
            lp = {"model": "openai/" + leaf,
                  "api_base": "http://chatgpt-acct-%s.litellm-product.svc.cluster.local:4000" % n,
                  "api_key": ak}
            mi = {"id": "chatgpt-acct-%s-%s" % (n, suf), "mode": ref_mi.get("mode", "responses")}
            for k, v in (("base_model", ref_mi.get("base_model")), ("max_input_tokens", mi_),
                         ("max_output_tokens", mo)):
                if v is not None:
                    mi[k] = v
            if i is not None and o is not None:
                lp.update(input_cost_per_token=i, output_cost_per_token=o)
                mi.update(input_cost_per_token=i, output_cost_per_token=o)
            body = {"model_name": e.get("model_name"), "litellm_params": lp, "model_info": mi}
            show = json.loads(json.dumps(body)); show["litellm_params"]["api_key"] = "<AK>"
            print(("  NEW " if apply else "  DRY ") + json.dumps(show, ensure_ascii=False))
            if apply:
                req = urllib.request.Request(urls[0].rstrip("/") + "/model/new",
                                             data=json.dumps(body).encode(), headers={
                                                 "Authorization": "Bearer " + K,
                                                 "Content-Type": "application/json"})
                try:
                    r = urllib.request.urlopen(req, timeout=60)
                    print("   ->", r.status)
                except urllib.error.HTTPError as ex:
                    txt = ex.read()[:200].decode(errors="replace")
                    # 刚 resume 完,DB 里已有行但 /model/info 还没传播到 ⇒ 这里误判缺腿再建一次,
                    # 撞 DB 唯一约束回 500 "Failed to add model to db"(2026-10-06 acct-210:7 条)。
                    # 不算失败:是不是真缺由随后的逐副本 audit 判,不靠这里的状态码。
                    if ex.code == 500 and "Failed to add model to db" in txt:
                        dup += 1
                        print("   -> 500 已在 DB(未传播到 /model/info),交给 audit")
                    else:
                        bad += 1
                        print("   -> FAIL", ex.code, txt)
        print("CLONE=%s%s" % ("FAIL" if bad else "OK", " dup_in_db=%d" % dup if dup else ""))
        return
    if cmd == "audit":
        ok = True
        for u in urls:
            d = info(u)
            a = arms(d)
            mine, theirs = a.get(n, {}), a.get(ref, {})
            miss = sorted(set(theirs) - set(mine))
            extra = sorted(set(mine) - set(theirs))
            bad = bad_ids(d, n)
            good = len(mine) == len(theirs) and not miss and not bad
            ok &= good
            print("%s %s N=%d REF=%d missing=%s extra=%s bad=%s" % (
                "OK " if good else "BAD", u, len(mine), len(theirs), miss, extra, bad))
        print("AUDIT=%s" % ("PASS" if ok else "FAIL"))
        return
    sys.exit("unknown cmd " + cmd)


if __name__ == "__main__":
    main()
