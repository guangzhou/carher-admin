#!/usr/bin/env python3
"""xai_change_pw.py — 在 225 的 Xvfb 里改 x.ai 登录密码（用 sso cookie 登录，不需要旧会话被踢）。

一般由 Mac 侧 `grok-pw-rotate.py` 调用，不单独手跑。

路径（2026-10-21 实测，acct 163 一把过，无 OTP、无验证码）：
  种 sso/sso-rw 到 .x.ai → `accounts.x.ai/account#security`
  → 「Login with password」那一行的 Change → 弹窗填 oldPassword/password → `Save password`

🔴 两个会让脚本点错东西的坑，都焊在下面的选择器里：
  ① 页面上有**三个** `Change` 按钮（改名 / 改邮箱 / 改密码），`button:has-text('Change')`
     取第一个 ⇒ 点出来是「Edit name」弹窗。必须从 `Manage the password` 这段文字往上
     找**只含一个** Change 按钮的最小祖先（往上找太高会同时圈进前两个）。
  ② OneTrust cookie 横幅本身是个 `[role=dialog]`，`pg.query_selector("[role=dialog]")`
     会抓到它 ⇒ 看到 0 个 password input 还以为「表单形状变了」。所以挑 dialog 要排除
     privacy preference，并且断言弹窗文字里有 `password`。

MODE=dry 只开弹窗、dump、不提交；MODE=apply 才填并提交。
⛔ 全程不打印 sso / 密码（`clean()` 把它们换成 <secret> 再输出）。

env: SSO_FILE, PW_FILE(json {"old_pw","new_pw"}), SHOTS, PROXY, MODE=dry|apply
退出：stdout 最后一行 `RESULT ...`，调用方按它判，别只看 returncode。
"""
import json
import os
import time

from patchright.sync_api import sync_playwright

SSO = open(os.environ["SSO_FILE"], encoding="utf-8").read().strip()
PW = json.load(open(os.environ["PW_FILE"], encoding="utf-8"))
SHOTS = os.environ["SHOTS"]
PROXY = os.environ.get("PROXY", "socks5://127.0.0.1:40000")
MODE = os.environ.get("MODE", "dry")
SECRETS = [s for s in (SSO, PW["old_pw"], PW["new_pw"]) if s]
SECURITY_URL = "https://accounts.x.ai/account#security"


def clean(s):
    for x in SECRETS:
        s = s.replace(x, "<secret>")
    return s


def log(*a):
    print(*a, flush=True)


def dump(pg, tag, scope=None):
    log("\n===== %s =====" % tag)
    log("[url] %s" % pg.url)
    root = scope or pg
    try:
        log("[text] %s" % clean((scope.inner_text() if scope else pg.inner_text("body"))[:1200]))
    except Exception as e:
        log("[text-err] %s" % str(e)[:120])
    for sel in ("input", "button"):
        for e in root.query_selector_all(sel)[:30]:
            try:
                at = {k: e.get_attribute(k) for k in
                      ("type", "name", "id", "autocomplete", "data-testid", "aria-label")}
                at = {k: v for k, v in at.items() if v}
                if at.get("type") in ("checkbox", "file"):
                    continue
                log("  <%s> %r %s" % (sel, (e.inner_text() or "").strip()[:40], at))
            except Exception:
                pass
    pg.screenshot(path=os.path.join(SHOTS, tag + ".png"))


def password_change_button(pg):
    """坑①：锚在 `Manage the password` 文案上，取只含一个 Change 的最小祖先。"""
    return pg.evaluate_handle("""() => {
        const all = [...document.querySelectorAll('*')];
        const t = all.find(n => n.children.length === 0
                              && /Manage the password/i.test(n.textContent || ''));
        if (!t) return null;
        let n = t.parentElement;
        for (let i = 0; i < 8 && n; i++, n = n.parentElement) {
            const bs = [...n.querySelectorAll('button')]
                        .filter(b => (b.innerText || '').trim() === 'Change');
            if (bs.length === 1) return bs[0];
            if (bs.length > 1) return null;   // 圈太大，已经把改名/改邮箱也圈进来了
        }
        return null;
    }""").as_element()


def visible_dialog(pg):
    """坑②：排掉 OneTrust 那个 role=dialog。"""
    for d in pg.query_selector_all("[role=dialog]"):
        try:
            if d.is_visible() and "privacy preference" not in (d.inner_text() or "").lower():
                return d
        except Exception:
            pass
    return None


def main():
    with sync_playwright() as p:
        br = p.chromium.launch(headless=False, proxy={"server": PROXY},
                               args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = br.new_context(locale="en-US", viewport={"width": 1400, "height": 950})
            ctx.add_cookies([{"name": n, "value": SSO, "domain": ".x.ai", "path": "/",
                              "secure": True, "httpOnly": True, "sameSite": "Lax"}
                             for n in ("sso", "sso-rw")])
            pg = ctx.new_page()
            pg.goto(SECURITY_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(6)
            for t in ("Allow All", "Reject All"):
                b = pg.query_selector("button:has-text('%s')" % t)
                if b and b.is_visible():
                    b.click()
                    time.sleep(1)
                    break
            btn = password_change_button(pg)
            if not btn:
                dump(pg, "no-button")
                log("RESULT NO_CHANGE_BUTTON")
                return 3
            btn.scroll_into_view_if_needed()
            btn.click()
            time.sleep(3)
            dlg = visible_dialog(pg)
            dump(pg, "dialog", dlg)
            if "password" not in (dlg.inner_text() if dlg else "").lower():
                log("RESULT WRONG_DIALOG (no 'password' in dialog -- see shots/dialog.png)")
                return 3
            old_in = dlg.query_selector("[data-testid=oldPassword]")
            new_in = dlg.query_selector("[data-testid=password]")
            submit = dlg.query_selector("button[type=submit]:has-text('Save password')")
            log("[form] old=%s new=%s submit=%s" % (bool(old_in), bool(new_in), bool(submit)))
            if not (old_in and new_in and submit):
                log("RESULT UNEXPECTED_FORM")
                return 3
            if MODE != "apply":
                log("RESULT DRY_OK")
                return 0
            old_in.fill(PW["old_pw"])
            new_in.fill(PW["new_pw"])
            time.sleep(1)
            log("[submit] clicking %r" % (submit.inner_text() or "").strip())
            submit.click()
            time.sleep(6)
            dump(pg, "after")
            body = pg.inner_text("body").lower()
            bad = [k for k in ("incorrect", "invalid", "wrong", "too weak",
                               "could not verify", "error") if k in body]
            still = visible_dialog(pg)
            still_open = bool(still and still.query_selector("input[type=password]"))
            log("RESULT SUBMITTED bad_words=%s dialog_still_open=%s" % (bad, still_open))
            return 0 if not bad and not still_open else 4
        finally:
            br.close()


if __name__ == "__main__":
    raise SystemExit(main())
