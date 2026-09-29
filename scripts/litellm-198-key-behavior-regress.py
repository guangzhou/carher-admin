#!/usr/bin/env python3
r"""行为层回归：改完一批 key 的白名单/alias 之后，证「还真能打」。

这是**两层回归的第二层**。第一层（值层）是
`scripts/litellm-key-drift-verify.py` —— 比快照，判 `now == old ∪ {新增名}`。
值层只证明"名字躺在表里"，证不了"发这个名字真的通"：
白名单里有名而缺 alias 的裸名照样 **400**
（[[feedback_allowlist_name_without_alias_is_400]]）。

🔴 为什么必须起临时 key，不能用 master key
----------------------------------------
**master key 不过 per-key 白名单闸门**，用它打什么都 200 ⇒ 必然假绿。
而真 key 的明文取不回来（库里只有 hash），所以唯一的路是：**克隆一把被改过的
真 key 的 `models`/`aliases` 形状**起一把短命 key，打完删掉。

🔴 三类名字，缺一类这轮就没有分辨力
----------------------------------
| 打什么 | 期望 | 它证明什么 |
|---|---|---|
| `--old` 改动前就有的名 | 200 + nonce 回得来 | 没回退 |
| `--new` 本轮新增名 | 200 | 改动真生效 |
| `--outside` 白名单外的名 | **403** | 门禁真在挡 ⇒ 上面那些 200 不是因为门没关 |

阴性对照不是可选项：少了它，全绿读数可能只是闸门根本没在工作
（[[feedback_synthetic_red_is_as_untrusted_as_synthetic_green]]）。
**脚本把"阴性对照没返 403"判成红**，而不是当成一条提示。

🔴 每发唯一 nonce
----------------
响应缓存是开的，逐字节相同的请求到不了上游 —— 那种 200 量的是缓存不是车道。

⚠️ 判据是「回了非空内容」，不是 200
-----------------------------------
空内容 200 在 198 上是真实存在的一类故障
（[[feedback_model_always_replies_empty_means_my_harvest_missed_it]]）。
另外**响应体的 `model` 字段是请求名的逐字回显**，它一个字都不能用来判落点；
判落点要么看 SpendLogs 的 `model` 列
（[[feedback_spendlogs_model_group_is_request_name_model_is_landing]]），
要么上游侧开 debug 看真收到什么
（[[feedback_kiro_map_model_substring_needs_landing_ruler]]）。

非 200 先按状态码分病，别合成一个"失败数"：
  403 → 名字不在白名单 ⇒ 比 `--backup` 快照，看是不是被收敛掉了
  400 → 白名单有名但缺 alias，或名字压根不存在
  超时不返回 → 与真挂死同形，看耗时分（真推理十几秒 / 明确拒绝 0~1s）

足迹 / 清理
----------
建 1 把 `duration` 默认 20m、`max_budget` 默认 1.0 的临时 key，跑完 `/key/delete`
并回读确认没了。**不碰任何既有 key、不碰 CM、不碰 fallbacks。**
异常退出也会走 finally 删 key；真删不掉会把 key_alias 打出来让你手删。

用法（在 198 本机跑）
--------------------
    MK=$(sudo kubectl -n litellm-product get secret litellm-secrets \
           -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

    # 形状从一把**被改过的真 key** 现读（--clone-from 收 key_alias 或 token hash）
    LITELLM_MASTER_KEY=$MK python3 scripts/litellm-198-key-behavior-regress.py \
        --clone-from cursor-liuguoxian-std \
        --new claude-opus-5-5 \
        --old claude-opus-5 \
        --outside claude-opus-4-8 \
        --protocol anthropic

    # 不想克隆、手写形状（比如新名还没进任何真 key）
    ... --models claude-opus-5-5,claude-opus-5 \
        --aliases claude-opus-5-5=kiro-claude-opus-5-5 --new ... --old ...

退出码：0 全绿 / 1 有判据不过 / 2 参数或前置条件不成立
"""
from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.request


DEFAULT_BASE = "http://127.0.0.1:30402"


def nonce() -> str:
    return "NONCE-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=10))


def api(base: str, path: str, key: str, payload=None, method=None, timeout=90):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        base + path, data=data,
        headers={"Authorization": "Bearer " + key, "content-type": "application/json"},
        method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def probe(base: str, key: str, name: str, protocol: str, timeout: float):
    """发一发，回 (status, 耗时, 文本, 错误体)。每发带唯一 nonce 绕缓存。"""
    n = nonce()
    if protocol == "anthropic":
        path, body = "/v1/messages", {
            "model": name, "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with exactly: " + n}]}
        headers = {"content-type": "application/json", "x-api-key": key,
                   "anthropic-version": "2023-06-01"}
    else:
        path, body = "/v1/chat/completions", {
            "model": name, "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with exactly: " + n}]}
        headers = {"content-type": "application/json", "Authorization": "Bearer " + key}
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                 headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read())
            if protocol == "anthropic":
                txt = "".join(b.get("text", "") for b in (d.get("content") or []))
            else:
                txt = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            return resp.status, time.time() - t0, txt, None
    except urllib.error.HTTPError as e:
        return e.code, time.time() - t0, "", e.read().decode()[:240]
    except Exception as e:                                  # 超时/连接断
        return None, time.time() - t0, "", repr(e)[:240]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("LITELLM_BASE", DEFAULT_BASE))
    ap.add_argument("--clone-from", metavar="KEY_ALIAS_OR_HASH",
                    help="从这把**被改过的真 key** 现读 models/aliases 形状")
    ap.add_argument("--models", help="手写白名单，逗号分隔（与 --clone-from 二选一）")
    ap.add_argument("--aliases", action="append", default=[], metavar="SRC=DST",
                    help="手写 alias，可重复")
    ap.add_argument("--new", action="append", default=[], required=True,
                    help="本轮新增名，期望 200 + 非空内容")
    ap.add_argument("--old", action="append", default=[],
                    help="改动前就有的名（阳性对照，判没回退）")
    ap.add_argument("--outside", action="append", default=[],
                    help="白名单外的名（阴性对照，期望 403）。⚠️ 不给就没有分辨力")
    ap.add_argument("--protocol", choices=("anthropic", "openai"), default="anthropic")
    ap.add_argument("--duration", default="20m")
    ap.add_argument("--max-budget", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--allow-no-negative", action="store_true",
                    help="明知没有分辨力也要跑（只在阴性名确实不存在时用）")
    a = ap.parse_args()

    mk = os.environ.get("LITELLM_MASTER_KEY")
    if not mk:
        # 兜底默认值等于把凭据提交进仓库，所以这里只报错不兜底
        print("🔴 需要环境变量 LITELLM_MASTER_KEY（⛔ 不许写死兜底默认值）", file=sys.stderr)
        return 2
    if bool(a.clone_from) == bool(a.models):
        print("🔴 --clone-from 与 --models 二选一", file=sys.stderr)
        return 2
    if not a.outside and not a.allow_no_negative:
        print("🔴 没给 --outside ⇒ 这轮**没有阴性对照**，全绿可能只是闸门没在工作。\n"
              "   给一个确定不在白名单里的名，或显式 --allow-no-negative。", file=sys.stderr)
        return 2

    # ── 形状 ────────────────────────────────────────────────────────────
    if a.clone_from:
        q = a.clone_from
        path = f"/key/info?key={q}" if len(q) == 64 and all(
            c in string.hexdigits for c in q) else f"/key/info?key_alias={q}"
        try:
            info = api(a.base, path, mk)
        except urllib.error.HTTPError as e:
            # ⛔ /key/info?key_alias= 这个 filter 在某些版本上不存在，404 会被
            #    误读成"这把 key 没了"。分清病再报。
            print(f"🔴 读参考 key 失败 HTTP {e.code}：{e.read().decode()[:200]}\n"
                  f"   若是 404，先确认 key_alias 拼写；这个 filter 在旧版上可能不支持，"
                  f"改用 64 位 token hash。", file=sys.stderr)
            return 2
        ki = info.get("info", info)
        models = list(ki.get("models") or [])
        aliases = dict(ki.get("aliases") or {})
        print(f"# 形状来自真 key `{ki.get('key_alias')}`："
              f"{len(models)} 个名 / {len(aliases)} 条 alias")
        if not models:
            print("🔴 参考 key 是 `models=[]`（全放行）⇒ 克隆它 = 闸门全开，"
                  "这轮不会有 403，量不出东西。换一把有白名单的。", file=sys.stderr)
            return 2
    else:
        models = [m.strip() for m in a.models.split(",") if m.strip()]
        aliases = dict(p.split("=", 1) for p in a.aliases)
        print(f"# 形状手写：{len(models)} 个名 / {len(aliases)} 条 alias")

    # 前置断言：阴性名真的不在白名单里，否则它不该返 403，红会读错
    for nm in a.outside:
        if nm in models:
            print(f"🔴 阴性对照 `{nm}` 就在白名单里 ⇒ 它不会返 403，"
                  f"这个对照是坏的。换一个真的在外面的名。", file=sys.stderr)
            return 2
    for nm in a.new + a.old:
        if nm not in models:
            print(f"⚠️ `{nm}` 不在这份白名单里 ⇒ 预期它会 403 而不是 200。"
                  f"（形状取错了？还是这轮的写入没落到这把参考 key 上？）")

    # ── 临时 key ────────────────────────────────────────────────────────
    # 带随机尾巴：同一秒内跑两次会撞 alias，撞上之后哪一把是哪一把就说不清了
    alias = "tmp-behavior-regress-%d-%s" % (
        int(time.time()), "".join(random.choices(string.ascii_lowercase, k=4)))
    try:
        created = api(a.base, "/key/generate", mk, {
            "key_alias": alias, "models": models, "aliases": aliases,
            "max_budget": a.max_budget, "duration": a.duration})
        tk = created["key"]
    except Exception as e:
        # 起不出临时 key ⇒ 这轮**什么都没量到**，不是"回归失败"。分清病：
        # 连不上 = 端口转发/NodePort 不对；401 = master key 不对。
        print(f"🔴 建临时 key 失败：{e!r}\n"
              f"   ⇒ 这轮没量到任何东西（不等于回归不过）。先确认 {a.base} 通、"
              f"master key 对。", file=sys.stderr)
        return 2
    print(f"# 临时 key `{alias}`（{a.duration} / ${a.max_budget}），跑完即删\n")

    rows, red = [], 0
    try:
        plan = ([(n, "new", 200) for n in a.new]
                + [(n, "old(阳性)", 200) for n in a.old]
                + [(n, "outside(阴性)", 403) for n in a.outside])
        for name, kind, want in plan:
            st, dt, txt, err = probe(a.base, tk, name, a.protocol, a.timeout)
            if want == 200:
                # 判据是**非空内容**，不是 200：空内容 200 是真实故障面
                ok = st == 200 and txt.strip() != ""
                why = "" if ok else (
                    "200 但内容为空 ⇒ 不算通" if st == 200 else f"期望 200，得 {st}")
            else:
                ok = st == want
                why = "" if ok else (
                    f"🔴 阴性对照没返 {want}（得 {st}）⇒ **闸门可能没在挡**，"
                    f"同轮那些 200 不能当绿" if st == 200 else f"期望 {want}，得 {st}")
            red += 0 if ok else 1
            rows.append((name, kind, st, dt, ok, why, (txt[:36] or err or "")))
            print("  %-26s %-14s %-5s %6.2fs %s %s" % (
                name, kind, st, dt, "OK " if ok else "RED", why or repr(txt[:36])))
    finally:
        try:
            api(a.base, "/key/delete", mk, {"keys": [tk]})
            tok = created.get("token")
            if not tok:
                # 没拿到 hash 就没法回读；**不许把"查不到"当成"删掉了"**
                note = " —— ⚠️ /key/generate 没回 token hash，删除未回读确认"
            else:
                try:
                    api(a.base, f"/key/info?key={tok}", mk)
                    note = " —— ⚠️ 回读仍在，手查"
                except urllib.error.HTTPError:
                    note = "（回读确认没了）"
            print(f"\n# 临时 key 已删{note}")
        except Exception as e:
            print(f"\n🔴 临时 key 删除失败：{e!r}\n"
                  f"   手删：curl -X POST {a.base}/key/delete -H 'Authorization: Bearer $MK' "
                  f"-d '{{\"keys\":[\"<key>\"]}}'  # key_alias={alias}", file=sys.stderr)

    # 结论。三类分开数 —— 合成一个"失败数"会让阴性对照坏掉被当成一条普通失败。
    print()
    for kind in ("new", "old(阳性)", "outside(阴性)"):
        sub = [r for r in rows if r[1] == kind]
        if sub:
            print(f"  {kind:<14} {sum(1 for r in sub if r[4])}/{len(sub)}")
    if any(r[1] == "outside(阴性)" and not r[4] for r in rows):
        print("\n🔴 **阴性对照不过 ⇒ 这轮整体无效**，别拿其余的绿下结论："
              "闸门没在挡时，通过与不通过读数相同。")
    if not a.outside:
        print("\n⚠️ 这轮没有阴性对照（--allow-no-negative）⇒ 绿只说明"
              "「发得出去」，不说明「白名单真在管事」。")
    print(f"\n{'✅ 行为层通过' if red == 0 else f'🔴 {red} 项不过'}")
    print("⚠️ 行为层不证落点：响应体 `model` 是请求名的逐字回显。"
          "判真落到哪个上游看 SpendLogs 的 `model` 列或上游侧 debug 日志。")
    return 0 if red == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
