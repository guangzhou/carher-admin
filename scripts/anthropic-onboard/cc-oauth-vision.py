#!/usr/bin/env python3
"""
cc-oauth-vision.py — sessionKey OAuth with a human/vision-in-the-loop harness to
clear the Arkose FunCaptcha that Anthropic shows at the OAuth Authorize step.

Runs inside the playwright docker image (patchright) headed under Xvfb. Keeps a
persistent browser alive and is driven by two host-mounted dirs:

  /work/screenshots/round-<i>.png   ← full-viewport screenshot each round (I read)
  /work/out/status.json             ← {round,url,done,code,note} each round
  /work/ctl/round-<i>.json          ← commands I write; script polls & executes

Command file schema (round-<i>.json), an object:
  {"actions": [
     {"type":"click","x":123,"y":456},
     {"type":"drag","x1":..,"y1":..,"x2":..,"y2":..},
     {"type":"move","x":..,"y":..},
     {"type":"key","key":"Enter"},
     {"type":"wait","ms":1500},
     {"type":"authorize"},          # (re)click the Authorize button
     {"type":"shot"},               # just re-screenshot (no-op action)
     {"type":"done_check"}
  ]}

Success = URL reaches .../oauth/code/callback with a real ?code=, or the page
shows a `<code>#<state>` block. Writes /work/out/code.txt and prints CODE=.

ENV: SESSION_KEY, CC_OAUTH_URL, CC_EMAIL(optional), MAX_ROUNDS(default 40),
     ROUND_TIMEOUT_SEC(default 600 per round wait)
"""
import json, os, re, time
from patchright.sync_api import sync_playwright

EMAIL = os.environ.get("CC_EMAIL", "(unknown)")
SESSION_KEY = os.environ["SESSION_KEY"]
OAUTH_URL = os.environ["CC_OAUTH_URL"]
MAX_ROUNDS = int(os.environ.get("MAX_ROUNDS", "40"))
ROUND_TIMEOUT = float(os.environ.get("ROUND_TIMEOUT_SEC", "600"))

SS = "/work/screenshots"
CTL = "/work/ctl"
OUT = "/work/out"
for d in (SS, CTL, OUT):
    os.makedirs(d, exist_ok=True)


def log(m):
    print(m, flush=True)


def human_move(page, x, y, steps=18):
    page.mouse.move(x, y, steps=steps)


def do_click(page, x, y):
    human_move(page, x, y)
    time.sleep(0.25)
    page.mouse.down()
    time.sleep(0.08)
    page.mouse.up()


def do_drag(page, x1, y1, x2, y2):
    human_move(page, x1, y1)
    time.sleep(0.2)
    page.mouse.down()
    time.sleep(0.15)
    # move in an arc-ish set of steps
    n = 24
    for i in range(1, n + 1):
        xi = x1 + (x2 - x1) * i / n
        yi = y1 + (y2 - y1) * i / n
        page.mouse.move(xi, yi, steps=2)
        time.sleep(0.02)
    time.sleep(0.15)
    page.mouse.up()


def extract_code(page):
    u = page.url or ""
    if "code/callback" in u or "oauth/callback" in u:
        m = re.search(r"[?&]code=([^&\s]+)", u)
        if m and m.group(1) != "true":
            return m.group(1)
    # on-page code block (setup-token callback page renders code#state)
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    m = re.search(r"\b([A-Za-z0-9_-]{40,}#[A-Za-z0-9_-]{16,})\b", body)
    if m:
        return m.group(1)
    # a bare long code param anywhere that is not literal 'true'
    m = re.search(r"[?&]code=([A-Za-z0-9_-]{20,})", u)
    if m and m.group(1) != "true":
        return m.group(1)
    return None


def has_arkose(page):
    for fr in page.frames:
        u = (fr.url or "").lower()
        if "arkoselabs" in u or "funcaptcha" in u or "arkose" in u:
            return True
    try:
        t = page.inner_text("body").lower()
    except Exception:
        t = ""
    return any(s in t for s in (
        "drag the segment", "click on things", "pick the", "select the",
        "use the arrows", "rotate", "complete the line",
    ))


def write_status(rnd, page, done=False, code=None, note=""):
    st = {
        "round": rnd,
        "url": (page.url or "")[:300],
        "has_arkose": has_arkose(page),
        "done": done,
        "code": code,
        "note": note,
        "ts": int(time.time()),
    }
    with open(f"{OUT}/status.json", "w") as f:
        json.dump(st, f)
    log(f"[status] round={rnd} done={done} arkose={st['has_arkose']} url={st['url'][:90]}")


def shoot(page, name):
    try:
        page.screenshot(path=f"{SS}/{name}.png")
    except Exception as e:
        log(f"  shot {name} failed: {e}")


def wait_for_cmd(rnd):
    """Poll for /work/ctl/round-<rnd>.json; return parsed dict or None on timeout."""
    path = f"{CTL}/round-{rnd}.json"
    deadline = time.time() + ROUND_TIMEOUT
    while time.time() < deadline:
        if os.path.exists(path):
            time.sleep(0.3)
            try:
                with open(path) as f:
                    data = json.load(f)
                return data
            except Exception as e:
                log(f"  bad cmd json: {e}")
                time.sleep(1)
                continue
        time.sleep(1.0)
    return None


def click_authorize(page):
    btn = page.get_by_role("button", name=re.compile(r"authorize|allow|approve", re.I))
    if btn.count() > 0:
        try:
            box = btn.first.bounding_box()
            if box:
                do_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                return True
        except Exception:
            pass
        try:
            btn.first.click()
            return True
        except Exception:
            pass
    return False


def is_login_page(page):
    try:
        t = page.inner_text("body").lower()
    except Exception:
        t = ""
    return "continue with email" in t or "enter your email" in t


def fill_email_and_submit(page, email):
    """On the claude.ai login page: type the email and click Continue with email."""
    try:
        inp = page.get_by_placeholder(re.compile(r"enter your email", re.I))
        if inp.count() == 0:
            inp = page.locator("input[type='email'], input[name='email'], input")
        inp.first.click()
        time.sleep(0.3)
        page.keyboard.type(email, delay=45)
        time.sleep(0.5)
        btn = page.get_by_role("button", name=re.compile(r"continue with email", re.I))
        if btn.count() > 0:
            box = btn.first.bounding_box()
            if box:
                do_click(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            else:
                btn.first.click()
        else:
            page.keyboard.press("Enter")
        return True
    except Exception as e:
        log(f"  fill_email err: {e}")
        return False


with sync_playwright() as pw:
    launch_kwargs = dict(headless=False, args=[
        "--no-sandbox", "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ])
    proxy_server = os.environ.get("PROXY_SERVER", "").strip()
    if proxy_server:
        launch_kwargs["proxy"] = {"server": proxy_server}
        log(f"[proxy] browser routed via {proxy_server}")
    br = pw.chromium.launch(**launch_kwargs)
    ctx = br.new_context(
        viewport={"width": 1280, "height": 900}, locale="en-US",
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"),
    )

    # ── Egress-IP gate: verify the browser exits from the expected IP ─
    expect_ip = os.environ.get("EXPECT_EXIT_IP", "").strip()
    _probe = ctx.new_page()
    exit_ip = ""
    try:
        _probe.goto("https://ipinfo.io/ip", wait_until="domcontentloaded", timeout=45000)
        exit_ip = (_probe.inner_text("body") or "").strip()
    except Exception as e:
        log(f"  exit-ip probe err: {e}")
    log(f"[egress] browser exit IP = {exit_ip!r} (expect={expect_ip!r})")
    with open(f"{OUT}/exit_ip.txt", "w") as f:
        f.write(exit_ip)
    _probe.close()
    if expect_ip and exit_ip != expect_ip:
        with open(f"{OUT}/status.json", "w") as f:
            json.dump({"round": -1, "done": False, "code": None,
                       "note": f"EGRESS_MISMATCH exit={exit_ip} expect={expect_ip}",
                       "url": ""}, f)
        log(f"\n❌ EGRESS MISMATCH: exit={exit_ip} != expect={expect_ip}; aborting before any Anthropic/email contact")
        br.close()
        raise SystemExit(2)

    expires = int(time.time()) + 30 * 86400
    if os.environ.get("INJECT_COOKIE", "1") != "0":
        ctx.add_cookies([{
            "name": "sessionKey", "value": SESSION_KEY, "domain": ".claude.ai",
            "path": "/", "httpOnly": True, "secure": True, "sameSite": "Lax",
            "expires": expires,
        }])
        log("  injected sessionKey cookie")
    else:
        log("  INJECT_COOKIE=0 -> fresh (magic-link) login mode")
    page = ctx.new_page()

    # ── Warm-up: establish a real session before touching OAuth ──────
    log("[warm] goto claude.ai and settle")
    try:
        page.goto("https://claude.ai/new", wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        log(f"  warm goto err: {e}")
    time.sleep(6)
    # gentle human-like mouse wiggle
    for (x, y) in [(300, 300), (640, 400), (900, 500), (500, 600)]:
        human_move(page, x, y); time.sleep(0.4)
    shoot(page, "00-warm")
    log(f"  warm url: {page.url[:120]}")

    # ── Go to OAuth authorize ────────────────────────────────────────
    log("[oauth] goto authorize url")
    page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
    time.sleep(6)
    # accept team invite if present
    try:
        bt = page.inner_text("body")
    except Exception:
        bt = ""
    if "invited you to join" in bt:
        log("  team invite -> accept")
        ab = page.get_by_role("button", name=re.compile(r"^accept", re.I))
        if ab.count() > 0:
            ab.first.click(); time.sleep(8)
            page.goto(OAUTH_URL, wait_until="domcontentloaded", timeout=45000)
            time.sleep(6)
    shoot(page, "01-authorize-page")
    log(f"  url: {page.url[:120]}")

    # ── Fresh (INJECT_COOKIE=0) magic-link login: auto-submit the email so the
    #    magic-link is sent WITHOUT depending on an external fill_email command.
    #    (The external orchestrator only needs to fetch the link + feed goto.)
    if is_login_page(page):
        log(f"[login] auto-fill email {EMAIL}")
        fill_email_and_submit(page, EMAIL)
        time.sleep(5)
        shoot(page, "01a-code-entry")

    # ── Click Authorize (may raise Arkose) ───────────────────────────
    code = extract_code(page)
    if not code and not is_login_page(page):
        log("[authorize] clicking")
        click_authorize(page)
        time.sleep(6)
        code = extract_code(page)

    # ── Vision-in-the-loop rounds ────────────────────────────────────
    rnd = 0
    while not code and rnd < MAX_ROUNDS:
        shoot(page, f"round-{rnd}")
        write_status(rnd, page, done=False, code=None)
        code = extract_code(page)
        if code:
            break
        cmd = wait_for_cmd(rnd)
        if cmd is None:
            log(f"[round {rnd}] timed out waiting for command; stopping")
            break
        for act in cmd.get("actions", []):
            t = act.get("type")
            try:
                if t == "click":
                    do_click(page, act["x"], act["y"])
                elif t == "drag":
                    do_drag(page, act["x1"], act["y1"], act["x2"], act["y2"])
                elif t == "move":
                    human_move(page, act["x"], act["y"])
                elif t == "key":
                    page.keyboard.press(act["key"])
                elif t == "type":
                    page.keyboard.type(act["text"], delay=45)
                elif t == "goto":
                    page.goto(act["url"], wait_until="domcontentloaded", timeout=60000)
                elif t == "wait":
                    time.sleep(act.get("ms", 500) / 1000.0)
                elif t == "authorize":
                    click_authorize(page)
                elif t == "fill_email":
                    fill_email_and_submit(page, act.get("email", EMAIL))
                elif t in ("shot", "done_check"):
                    pass
            except Exception as e:
                log(f"  action {t} err: {e}")
            time.sleep(0.4)
        time.sleep(2.0)
        code = extract_code(page)
        rnd += 1

    shoot(page, "99-final")
    if code:
        with open(f"{OUT}/code.txt", "w") as f:
            f.write(code)
        write_status(rnd, page, done=True, code=code, note="success")
        log(f"\n✅ CALLBACK_CODE={code}")
    else:
        write_status(rnd, page, done=False, code=None, note="no code")
        try:
            log(f"\n❌ No code. final url={page.url[:200]}")
        except Exception:
            log("\n❌ No code.")
    # keep browser a moment so late redirects settle
    time.sleep(3)
    br.close()
