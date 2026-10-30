#!/usr/bin/env python3
"""Read the newest 6-digit muse login code out of a generator.email throwaway inbox. Runs on 188.

    python3 genemail_code.py <email> [--since-epoch N] [--wait 120]

generator.email has no JSON API: the inbox page is rendered server-side and selected by the
`inbox_ctx` cookie (set by first fetching /<email>), then read from /inbox1/.
Prints `CODE=123456` on success, nothing otherwise. Never prints mail bodies.
"""
import argparse
import re
import subprocess
import sys
import time

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"


def fetch(email, jar):
    dom, user = email.split("@")[1], email.split("@")[0]
    # 1) set inbox_ctx for this address, 2) read the rendered inbox
    subprocess.run(["curl", "-s", "--max-time", "25", "-A", UA, "-c", jar, "-b", jar, "-o", "/dev/null",
                    "https://generator.email/%s" % email], capture_output=True, timeout=40)
    r = subprocess.run(["curl", "-s", "-L", "--max-time", "25", "-A", UA, "-b", jar, "-c", jar,
                        "https://generator.email/inbox1/"], capture_output=True, text=True, timeout=40)
    return r.stdout


MAIL_RE = re.compile(
    r'from_div[^>]*>(?P<frm>[^<]*)</div>\s*<div[^>]*subj_div[^>]*>(?P<subj>[^<]*)</div>'
    r'\s*<div[^>]*time_div[^>]*>(?P<time>[^<]*)</div>', re.S)


def codes(html):
    """(code, when) from Meta/muse login mails only, in page order (newest first).

    The inbox page also contains ad ids and dates, so a bare six-digit match picks up junk --
    anchor on the sender/subject cells the inbox renders for each message.
    """
    out = []
    for m in MAIL_RE.finditer(html):
        frm, subj, when = m.group("frm"), m.group("subj"), m.group("time").strip()
        if not re.search(r"meta\.com|muse", frm, re.I) and not re.search(r"muse|log ?in|code", subj, re.I):
            continue
        c = re.search(r"\b(\d{6})\b", subj)
        if c:
            out.append((c.group(1), when))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--wait", type=int, default=120, help="seconds to poll")
    ap.add_argument("--stale", default="", help="code seen before asking; a code equal to it is not fresh")
    a = ap.parse_args()
    jar = "/tmp/ge-%s.cookies" % re.sub(r"[^A-Za-z0-9]", "", a.email)
    deadline = time.time() + a.wait
    while True:
        html = fetch(a.email, jar)
        for c, when in codes(html):
            if c != a.stale:
                print("CODE=%s WHEN=%s" % (c, when))
                return 0
        if time.time() >= deadline:
            print("NOCODE", file=sys.stderr)
            return 1
        time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
