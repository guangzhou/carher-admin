#!/usr/bin/env python3
"""只跑 mail.com 登录那一段，把页面文字读出来 —— 188 上没有 OCR，截图只能做像素统计，
读不到"密码错 / 风控 / 限速"这三种处置完全相反的原因。

复制 zerokey-capture:latest 里 mailcom_login() 的选择器与顺序（sed 读出来核对过），
只加：每一步 print url + 落点页面的可见文字。不碰 chatgpt.com，不占 CF 登录通道。

密码走 MAIL_LOGIN_PW_FILE，不进 argv、不进日志（只印长度）。
"""
import os, sys, time, traceback

# xvfb-run 那层会吞掉 stdout（实测 docker logs 与重定向文件都是 0 字节，
# 连网络操作之前的第一行都没有）⇒ 日志不依赖 stdout，自己写文件。
LOG = os.environ.get("PROBE_LOG", "/state/probe.log")
_lf = open(LOG, "a", buffering=1)


def log(*a):
    line = " ".join(str(x) for x in a)
    _lf.write(line + "\n")
    try:
        print(line, flush=True)
    except Exception:
        pass


log("[probe] start pid=%d argv=%r" % (os.getpid(), sys.argv))

from patchright.sync_api import sync_playwright  # 镜像里是 patchright（CF 要真 Chrome TLS），不是 playwright

EMAIL = os.environ["MAIL_USER"]
MAIL_PW = open(os.environ["MAIL_LOGIN_PW_FILE"]).read().strip()
OUT = os.environ.get("TEXT_OUT", "/state/page.txt")

log(f"[probe] email={EMAIL} pw_len={len(MAIL_PW)}")


def dump(p, tag):
    log(f"[probe] {tag} url={p.url}")
    try:
        txt = p.locator("body").inner_text(timeout=10000)
    except Exception as e:
        txt = f"<inner_text failed: {e}>"
    txt = "\n".join(l.strip() for l in txt.splitlines() if l.strip())
    with open(OUT, "a") as f:
        f.write(f"\n===== {tag} url={p.url} =====\n{txt}\n")
    log(f"[probe] {tag} text_len={len(txt)} first_600:")
    log(txt[:600])


try:
  with sync_playwright() as pw:
      b = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
      ctx = b.new_context(viewport={"width": 1280, "height": 800})
      p = ctx.new_page()
      p.goto("https://www.mail.com/", wait_until="domcontentloaded")
      dump(p, "01-landing")

      p.locator("a:has-text('Log in')").first.click()
      p.wait_for_timeout(1500)
      dump(p, "02-after-login-click")

      p.locator("input[placeholder='Email address']").first.fill(EMAIL)
      p.locator("input[placeholder='Password']").first.fill(MAIL_PW)
      btns = p.locator("button:has-text('Log in')")
      n = btns.count()
      log(f"[probe] login buttons={n}")
      clicked = False
      for i in range(n):
          box = btns.nth(i).bounding_box()
          if box and box["y"] > 50:
              log(f"[probe] clicking button idx={i} y={box['y']}")
              btns.nth(i).click()
              clicked = True
              break
      log(f"[probe] clicked={clicked}")

      # 逐秒记录 url 变化，看它是"提交后被踢回 logout"还是"根本没跳"
      prev = p.url
      for s in range(30):
          if p.url != prev:
              log(f"[probe] t={s}s url -> {p.url}")
              prev = p.url
          if "navigator" in p.url:
              log(f"[probe] reached navigator at t={s}s")
              break
          time.sleep(1)

      dump(p, "03-final")
      p.screenshot(path="/state/probe-final.png", full_page=True)
      b.close()
except Exception:
    log("[probe] EXCEPTION:\n" + traceback.format_exc())
    raise
log("[probe] done")
