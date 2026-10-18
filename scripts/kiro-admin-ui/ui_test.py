"""用 live 快照验查询项：每个条件的结果数 = Python 独立算出的期望数；另截图。"""
import json, sys, re
from collections import Counter
from playwright.sync_api import sync_playwright
PORT, POOL, SHOT = sys.argv[1], json.load(open(sys.argv[2])), sys.argv[3]
C = POOL["credentials"]; N = len(C)
dom = lambda c: c["email"].rsplit("@",1)[1].lower() if c.get("email") and "@" in c["email"] else "(无邮箱)"
fails = 0
def check(name, got, exp):
    global fails
    ok = got == exp; fails += (not ok)
    print(("OK  " if ok else "STOP"), name, "got", got, "exp", exp)
with sync_playwright() as pw:
    b = pw.chromium.launch(); pg = b.new_page(viewport={"width": 1440, "height": 1000})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(f"http://127.0.0.1:{PORT}/admin/"); pg.evaluate("localStorage.setItem('adminApiKey','mock')"); pg.reload()
    pg.wait_for_selector("text=凭据管理")
    summary = lambda: pg.locator("span[aria-live=polite]").inner_text()
    cards = lambda: pg.locator("main .grid.lg\\:grid-cols-3 > div").count()
    def matched():
        m = re.search(r"筛选出 (\d+) / (\d+)", summary()); return int(m.group(1)) if m else int(re.search(r"共 (\d+)", summary()).group(1))
    check("初始总数", matched(), N); check("首页卡片数", cards(), min(12, N))
    # 首页顺序仍是 0004 规则：当前在用置顶
    first = pg.locator("main .grid.lg\\:grid-cols-3 > div").first.inner_text()
    check("首卡=当前在用", "当前" in first, True)
    sel = pg.locator("select")
    st, au, pr, dm = sel.nth(0), sel.nth(1), sel.nth(2), sel.nth(3)
    st.select_option("enabled"); check("状态=启用", matched(), sum(not c["disabled"] for c in C))
    st.select_option("current"); check("状态=当前", matched(), 1)
    st.select_option("disabled"); check("状态=已禁用", matched(), sum(c["disabled"] for c in C))
    for r, n in Counter(c.get("disabledReason") or "未知" for c in C if c["disabled"]).items():
        st.select_option(f"disabled:{r}"); check(f"禁用原因={r}", matched(), n)
    st.select_option("all")
    for a, n in Counter(c.get("authMethod") or "" for c in C).items():
        au.select_option(a); check(f"登录方式={a}", matched(), n)
    au.select_option("")
    for p, n in Counter(str(c["priority"]) for c in C).items():
        pr.select_option(p); check(f"优先级={p}", matched(), n)
    pr.select_option("")
    for d, n in Counter(dom(c) for c in C).items():
        dm.select_option(d); check(f"域名={d}", matched(), n)
    dm.select_option("")
    # 组合：启用 + binus
    st.select_option("enabled"); dm.select_option("binus.ac.id")
    check("组合 启用+binus", matched(), sum((not c["disabled"]) and dom(c)=="binus.ac.id" for c in C))
    pg.screenshot(path=SHOT.replace(".png", "-combo.png"), full_page=False)
    pg.get_by_role("button", name="重置筛选").click(); check("重置后", matched(), N)
    # 关键字：ID 精确 + 邮箱子串 + 多词 OR
    box = pg.get_by_label("搜索邮箱或 ID")
    cid = POOL["currentId"]; box.fill(f"#{cid}")
    check(f"关键字 #{cid}", matched(), sum(c["id"]==cid or str(cid) in (c.get("email") or "").lower() for c in C))
    box.fill("icloud"); check("关键字 icloud", matched(), sum("icloud" in (c.get("email") or "").lower() for c in C))
    box.fill("icloud gmail"); check("关键字 icloud|gmail", matched(), sum(any(t in (c.get("email") or "").lower() for t in ("icloud","gmail")) for c in C))
    box.fill("zzz-not-exist-zzz"); check("关键字 无命中", matched(), 0); check("无命中提示", pg.locator("text=没有符合条件的凭据").count(), 1)
    box.fill("")
    pg.locator("text=只看有失败").click(); check("只看有失败", matched(), sum(c["failureCount"]>0 or c["refreshFailureCount"]>0 for c in C))
    pg.get_by_role("button", name="重置筛选").click()
    # 翻页后改条件回到第 1 页
    pg.get_by_role("button", name="下一页").click(); pg.get_by_role("button", name="下一页").click()
    check("翻到第3页", "第 3 /" in pg.locator("text=/第 \\d+ \\/ \\d+ 页/").inner_text(), True)
    st.select_option("disabled"); check("改条件回第1页", "第 1 /" in pg.locator("text=/第 \\d+ \\/ \\d+ 页/").inner_text(), True)
    st.select_option("all")
    pg.screenshot(path=SHOT, full_page=False)
    check("页面 JS 报错", errs, [])
    b.close()
print("FAILS", fails); sys.exit(1 if fails else 0)
