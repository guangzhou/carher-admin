#!/usr/bin/env python3
"""
in-pod 探针(由 chatgpt-acct-verify-aliyun.sh 注入执行)。一次 exec 拿全四件事:

  IDENT   —— auth.json 里 id_token 解出的 email / account_id（判「这个编号装的是哪个号」，
             唯一权威身份，别按编号推邮箱，见 memory project_acct_162_174_reauth_renew_pool）
  RENEW   —— live `/backend-api/accounts/check/v4-2023-04-27` 的
             `last_active_subscription.will_renew` + plan + 到期日
             （**续订 NEED/RESULT 的唯一判据**，token claims 与 billing in-page 读都会骗）
  SMOKE   —— 打 pod 自己的 127.0.0.1:4000 /v1/responses，**流式**且断言真出字
             （非流式 chatgpt responses 会空 output；ready 1/1 不等于活）
  USAGE   —— 上游 /backend-api/codex/usage 的 7d 百分比（号买来可能已被用掉）

请求形状是硬约束：`Originator: codex_cli_rs` + `User-Agent: codex_cli_rs/...` +
`ChatGPT-Account-ID` 三件套缺一个就 403，那**不是出口问题**（2026-09-08 实证：
阿里云节点和 188 跳板同样 403）。
"""
import json
import os
import urllib.request
import urllib.error

AUTH = os.environ.get("CHATGPT_TOKEN_DIR", "/chatgpt-auth") + "/auth.json"
UA = "codex_cli_rs/0.30.0 (Linux; x86_64)"
MODEL = os.environ.get("SMOKE_MODEL", "chatgpt-gpt-5.5")


def b64url(seg):
    import base64
    seg += "=" * (-len(seg) % 4)
    return json.loads(base64.urlsafe_b64decode(seg))


def get(url, tok, acct):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}",
        "ChatGPT-Account-ID": acct,
        "Originator": "codex_cli_rs",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.getcode(), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:180]
    except Exception as e:
        return -1, repr(e)


def main():
    out = {}
    d = json.load(open(AUTH))
    tok, acct = d["access_token"], d.get("account_id", "")
    claims = b64url(d["id_token"].split(".")[1])
    a = claims.get("https://api.openai.com/auth", {})
    out["ident"] = {"email": claims.get("email"), "account_id": acct,
                    "token_plan": a.get("chatgpt_plan_type"),
                    "token_until": a.get("chatgpt_subscription_active_until"),
                    "access_len": len(tok)}

    code, body = get("https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27", tok, acct)
    r = {"http": code}
    if code == 200 and isinstance(body, dict):
        # ⚠ 响应是 **两层**: {"accounts": {"<account_id>": {...}, "default": {...}}}。
        # 顶层没有 last_active_subscription —— 直接 body.get("last_active_subscription")
        # 会恒为 None，被渲染成 will_renew=? 而不是报错，是典型的「量具坏了却看着像
        # 数据缺失」。取自己的 account_id，取不到才退 "default"。
        accs = body.get("accounts") or {}
        node = accs.get(acct) or accs.get("default") or {}
        sub = node.get("last_active_subscription") or {}
        ent = node.get("entitlement") or {}
        r.update(will_renew=sub.get("will_renew"),
                 cancellation_outcome=sub.get("cancellation_outcome"),
                 plan=ent.get("subscription_plan") or node.get("account", {}).get("plan_type"),
                 has_active=ent.get("has_active_subscription"),
                 active_until=ent.get("renews_at") or ent.get("expires_at"),
                 is_deactivated=node.get("account", {}).get("is_deactivated"))
        if not sub and not ent:
            r["err"] = "no subscription node for acct=%s (keys=%s)" % (acct, list(accs)[:4])
    else:
        r["err"] = body
    out["renew"] = r

    code, body = get("https://chatgpt.com/backend-api/codex/usage", tok, acct)
    u = {"http": code}
    if code == 200 and isinstance(body, dict):
        # 7d 百分比在 rate_limit.primary_window.used_percent（不是顶层 *_used_percent，
        # 那套旧字段名现在**一个都不存在**，照旧名取只会全读成"没设置"）。
        rl = body.get("rate_limit") or {}
        pw = rl.get("primary_window") or {}
        sw = rl.get("secondary_window") or {}
        u.update(plan_type=body.get("plan_type"),
                 primary_used_percent=pw.get("used_percent"),
                 primary_window_seconds=pw.get("limit_window_seconds"),
                 secondary_used_percent=sw.get("used_percent"),
                 limit_reached=rl.get("limit_reached"))
        if not pw:
            u["raw_keys"] = list(body)[:12]
    else:
        u["err"] = body
    out["usage"] = u

    if os.environ.get("SMOKE_SKIP") == "1":
        out["smoke"] = {"verdict": "SKIP", "chars": "-"}
        print("PROBE_JSON " + json.dumps(out, ensure_ascii=False))
        return

    mk = os.environ.get("LITELLM_MASTER_KEY", "")
    payload = json.dumps({
        "model": MODEL, "stream": True,
        "input": [{"role": "user",
                   "content": [{"type": "input_text",
                                "text": os.environ.get("SMOKE_PROMPT", "say ok")}]}],
    }).encode()
    req = urllib.request.Request("http://127.0.0.1:4000/v1/responses", data=payload,
                                 headers={"Authorization": f"Bearer {mk}",
                                          "Content-Type": "application/json"},
                                 method="POST")
    s = {"model": MODEL}
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            s["http"] = resp.getcode()
            text, done = "", False
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk in ("", "[DONE]"):
                    continue
                try:
                    ev = json.loads(chunk)
                except Exception:
                    continue
                t = ev.get("type", "")
                if t == "response.output_text.delta":
                    text += ev.get("delta", "")
                elif t == "response.completed":
                    done = True
                elif t == "error" or "error" in ev:
                    s.setdefault("stream_err", str(ev)[:180])
            s.update(text=text[:120], chars=len(text), completed=done,
                     verdict="PASS" if (len(text) > 0 and done) else "FAIL")
    except urllib.error.HTTPError as e:
        s.update(http=e.code, verdict="FAIL", err=e.read().decode()[:200])
    except Exception as e:
        s.update(verdict="FAIL", err=repr(e))
    out["smoke"] = s

    print("PROBE_JSON " + json.dumps(out, ensure_ascii=False))


main()
