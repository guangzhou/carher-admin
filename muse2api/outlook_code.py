#!/usr/bin/env python3
"""Fetch the latest Meta/Muse 6-digit login code from an Outlook mailbox.

Reads one line "email----password----client_id----refresh_token" from stdin
(the usual 成品号 delivery format). Uses Microsoft OAuth refresh_token -> IMAP XOAUTH2,
falls back to Graph. Prints only: received time, subject without the code, and the code
on the last line prefixed with CODE=. Never prints the password or tokens.

Usage: echo "$LINE" | outlook_code.py [--since-epoch N]
"""
import argparse
import datetime
import email
import email.utils
import imaplib
import json
import re
import sys
import time
import urllib.parse
import urllib.request

TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def refresh(client_id, rt, scope):
    body = urllib.parse.urlencode({
        "client_id": client_id, "grant_type": "refresh_token",
        "refresh_token": rt, "scope": scope,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def via_imap(addr, client_id, rt, since):
    at = refresh(client_id, rt, "https://outlook.office.com/IMAP.AccessAsUser.All offline_access")
    m = imaplib.IMAP4_SSL("outlook.office365.com", 993)
    m.authenticate("XOAUTH2", lambda _: f"user={addr}\x01auth=Bearer {at}\x01\x01".encode())
    best = None
    for box in ("INBOX", "Junk"):
        if m.select(box, readonly=True)[0] != "OK":
            continue
        _, ids = m.search(None, '(FROM "meta")')
        for i in ids[0].split()[-5:]:
            _, data = m.fetch(i, "(RFC822)")
            msg = email.message_from_bytes(data[0][1])
            ts = email.utils.parsedate_to_datetime(msg["Date"]).timestamp()
            subj = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
            text = subj
            for part in msg.walk():
                if part.get_content_type() in ("text/plain", "text/html"):
                    text += " " + part.get_payload(decode=True).decode(errors="replace")
            mm = CODE_RE.search(subj) or CODE_RE.search(text)
            if mm and ts >= since and (best is None or ts > best[0]):
                best = (ts, subj, mm.group(1))
    m.logout()
    return best


def via_graph(client_id, rt, since):
    at = refresh(client_id, rt, "https://graph.microsoft.com/Mail.Read offline_access")
    url = ("https://graph.microsoft.com/v1.0/me/messages?$top=10&$orderby=receivedDateTime desc"
           "&$select=subject,receivedDateTime,bodyPreview,from")
    req = urllib.request.Request(url.replace(" ", "%20"), headers={"Authorization": f"Bearer {at}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        items = json.loads(r.read())["value"]
    for it in items:
        frm = (it.get("from") or {}).get("emailAddress", {}).get("address", "")
        if "meta" not in frm.lower() and "muse" not in frm.lower():
            continue
        ts = datetime.datetime.fromisoformat(it["receivedDateTime"].replace("Z", "+00:00")).timestamp()
        mm = CODE_RE.search(it.get("subject", "")) or CODE_RE.search(it.get("bodyPreview", ""))
        if mm and ts >= since:
            return (ts, it.get("subject", ""), mm.group(1))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since-epoch", type=float, default=0)
    a = ap.parse_args()
    parts = sys.stdin.read().strip().split("----")
    if len(parts) < 4:
        sys.exit("expected email----password----client_id----refresh_token")
    addr, _pw, client_id, rt = parts[0], parts[1], parts[2], parts[3]
    best, err = None, None
    try:
        best = via_imap(addr, client_id, rt, a.since_epoch)
    except Exception as e:  # noqa: BLE001
        err = f"imap: {type(e).__name__}: {str(e)[:120]}"
    if best is None:
        try:
            best = via_graph(client_id, rt, a.since_epoch)
        except Exception as e:  # noqa: BLE001
            err = (err or "") + f" | graph: {type(e).__name__}: {str(e)[:120]}"
    if best is None:
        print(f"NO_CODE {err or ''}")
        sys.exit(3)
    ts, subj, code = best
    print(f"received {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))} subject={CODE_RE.sub('<code>', subj)[:60]}")
    print(f"CODE={code}")


if __name__ == "__main__":
    main()
