"""离线验收 deploy_198.sh 里 4b) 那段 nginx 加固 reconcile。

为什么必须离线跑:它改的是**生产 nginx 配置**。靠"部署时 nginx -t 会兜住"不够 ——
nginx -t 只说语法对,不会说"你把 allow 规则插到了永不命中的地方"。
11-05 踩到的就是这个:internal 段写 `allow 127.0.0.1; deny all;`,但 198 的 nginx 前面还有一层
前置代理,这个 location 又**没有** realip 指令 ⇒ $remote_addr 恒为前置代理(实测 10.68.13.97),
那条 allow 一次都不可能命中。nginx -t 全绿,机器人带对密钥照样吃 nginx 自己的 403,
外部人因此登不进来(机器人侧 audit:11-04 14:08 还成功签发过,18:26 加固后断掉)。

输入是 `nginx_live_sample.conf` —— 198 上那份真配置的快照(已扫过,无凭据)。
线上配置变了之后这份快照要重新取:
    scripts/jms ssh AIYJY-litellm "sudo -n cat /etc/nginx/sites-enabled/chat.auto-link.com.cn.conf" \
      > scripts/ai-portal/nginx_live_sample.conf

用法: python3 scripts/ai-portal/test_nginx_harden.py   (deploy_198.sh 的门禁里会跑)
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LIVE = HERE / "nginx_live_sample.conf"
PROXY = "10.68.13.97"
BOT = "47.96.23.21"

fails = []
total = 0


def chk(name, ok, detail=""):
    global total
    total += 1
    print(("OK   " if ok else "FAIL ") + name + (("  — " + detail) if detail and not ok else ""))
    if not ok:
        fails.append(name)


def extract():
    """从 deploy_198.sh 里抽出 4b) 那段内嵌 python(与部署时跑的是同一份字符)。"""
    s = (ROOT / "scripts/ai-portal/deploy_198.sh").read_text()
    body = s[s.index("# 4b)"):]
    m = re.search(r"<<'PY'\n(.*?)\nPY\n", body, re.S)
    assert m, "没找到 4b) 里的内嵌 python"
    return m.group(1).replace("\\$", "$")


def run(code, conf_text, bot=BOT):
    """照部署时的调用方式跑一遍:argv = [_, conf 路径, 机器人出口 IP]。"""
    p = Path(tempfile.gettempdir()) / "_ai_portal_harden_tmp.conf"
    p.write_text(conf_text)
    r = subprocess.run([sys.executable, "-c", code, str(p), bot],
                       capture_output=True, text=True)
    out = p.read_text()
    p.unlink(missing_ok=True)
    return r, out


def seg(text, tag):
    m = re.search(r">>> carher-ai-portal:%s >>>(.*?)<<< carher-ai-portal:%s <<<" % (tag, tag),
                  text, re.S)
    return m.group(1) if m else ""


def run_all():
    """跑完所有断言,返回 (失败项, 总数)。pytest 和命令行共用这一份。"""
    global fails, total
    fails, total = [], 0
    code = extract()
    live = LIVE.read_text()

    # ---- 1) 对线上这份配置跑一遍:两段都已存在 ⇒ 必须是"替换",不能变成插入第二份
    r, out = run(code, live)
    chk("对线上配置跑得通", r.returncode == 0, r.stderr.strip()[:200])
    chk("internal 段只有一份", out.count(">>> carher-ai-portal:internal >>>") == 1)
    chk("harden 段只有一份", out.count(">>> carher-ai-portal:harden >>>") == 1)
    # 🔴 只数**真的 location 指令**(行首 location,后面跟 = / ^~ / ~ / 路径),不能数 "location " 子串:
    #    注释里写了"这个 location 没有 realip"也会被数进去 ⇒ 34→35 的假红(第一版就是这么红的)。
    LOC_RE = re.compile(r"^\s*location\s+[=^~/]", re.M)


    def nloc(t):
        return len(LOC_RE.findall(t))


    chk("没把别的 location 弄丢", nloc(out) == nloc(live),
        "改前 %d 改后 %d" % (nloc(live), nloc(out)))

    # ---- 2) 真正修的那个 bug:internal 段必须同时有 realip 和机器人出口 IP
    ins = seg(out, "internal")
    chk("internal 段带 realip(否则 allow 永不命中)", "real_ip_header X-Real-IP;" in ins)
    chk("internal 段信任前置代理", "set_real_ip_from %s;" % PROXY in ins)
    chk("internal 段放行机器人出口 IP", "allow %s;" % BOT in ins)
    chk("internal 段仍然 deny all 兜底", "deny all;" in ins)
    chk("internal 段仍然只转给本机门户", "proxy_pass http://127.0.0.1:8793;" in ins)

    # ---- 2b) 阴性对照:把 realip 那行从输入里删掉,再跑一遍 —— reconcile 必须把它补回来。
    #      (这条用来证明上面的 OK 不是"输入里本来就有"蒙到的)
    broken = live.replace("        set_real_ip_from 10.68.13.97;\n        real_ip_header X-Real-IP;\n", "", 1)
    chk("阴性对照的输入确实被改坏了", broken != live)
    r2, out2 = run(code, broken)
    chk("阴性对照:跑完 realip 被补回", r2.returncode == 0 and "real_ip_header X-Real-IP;" in seg(out2, "internal"))

    # ---- 3) 幂等:再跑一遍,内容必须逐字节不变
    r3, out3 = run(code, out)
    chk("幂等(第二遍不再改动)", r3.returncode == 0 and out3 == out)

    # ---- 4) 从"完全没有加固段"的配置起跑 ⇒ 插入,且插在 /ai/ 兜底 location **之前**
    bare = re.sub(r"[ \t]*#[^\n]*>>> carher-ai-portal:(internal|harden) >>>.*?"
                  r"<<< carher-ai-portal:\1 <<<[^\n]*\n", "", live, flags=re.S)
    chk("构造出的 bare 配置里确实没有加固段", ">>> carher-ai-portal:internal >>>" not in bare)
    r4, out4 = run(code, bare)
    chk("bare 起跑能插入", r4.returncode == 0 and ">>> carher-ai-portal:internal >>>" in out4)
    if ">>> carher-ai-portal:internal >>>" in out4:
        i_int = out4.index(">>> carher-ai-portal:internal >>>")
        i_fallback = out4.index("    location ^~ /ai/ {")
        chk("internal 段在 /ai/ 兜底之前(nginx 前缀匹配取最长,但顺序错了人看不懂)", i_int < i_fallback)

    # ---- 5) 防呆:段落出现两次 ⇒ 必须拒绝,不许瞎改
    dup = out.replace(seg(out, "internal"), seg(out, "internal"))  # 先拿到原样
    blk = re.search(r"[ \t]*#[^\n]*>>> carher-ai-portal:internal >>>.*?<<< carher-ai-portal:internal <<<[^\n]*\n",
                    out, re.S).group(0)
    r5, _ = run(code, out.replace(blk, blk + blk, 1))
    chk("重复段落会被拒绝(不瞎改)", r5.returncode != 0 and "出现 2 次" in (r5.stdout + r5.stderr))

    # ---- 6) 防呆:找不到锚点 ⇒ 拒绝
    r6, _ = run(code, bare.replace("    location ^~ /ai/ {", "    location ^~ /zzz/ {"))
    chk("找不到锚点会被拒绝", r6.returncode != 0 and "锚点" in (r6.stdout + r6.stderr))

    # ---- 7) 不许把 key / 密钥写进配置
    chk("改完的配置里没有 sk- 形状的东西", not re.search(r"sk-[A-Za-z0-9]{10,}", out))

    return fails, total


def test_nginx_harden_reconcile():
    """pytest 入口:任何一条断言不过就红,报告里直接列出哪几条。"""
    bad, n = run_all()
    assert n >= 18, "断言条数变少了(%d)⇒ 多半是某一段提前 return 了" % n
    assert not bad, "nginx 加固 reconcile 有 %d 条不过:%s" % (len(bad), bad)


if __name__ == "__main__":
    bad, n = run_all()
    print()
    print("FAILED: %d / %d" % (len(bad), n) if bad else "ALL PASS (%d checks)" % n)
    for f in bad:
        print("  -", f)
    sys.exit(1 if bad else 0)
