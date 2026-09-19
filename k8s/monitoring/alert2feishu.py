#!/usr/bin/env python3
"""
Grafana 告警 → 飞书群机器人 适配器 (198)
=========================================

为什么需要这一层
----------------
Grafana 的 "webhook" contact point 发出的是它自己那套 JSON
（`{"status":"firing","alerts":[...]}`），飞书群机器人只认
`{"msg_type":"interactive","card":{...}}`。两边格式对不上，飞书会直接回
`{"code":9499,"msg":"Bad Request"}` 且**HTTP 仍然是 200** —— 也就是说
不转这一层的话，Grafana 侧看不出任何异常，告警静默丢失。
（这正是"量具坏了读成绿"的形状：发送成功 ≠ 送达。）

所以这里除了转格式，还做一件事：**把飞书回的 body 里的 code 也当判据**，
非 0 就 log 出来并计入 `alert2feishu_delivery_failures_total`，
让"告警没送到"本身可被监控。

webhook URL 从哪来
------------------
Secret `feishu-alert-webhook` 的 `url` 字段。没配就整个服务 CrashLoop ——
故意的：宁可 Pod 红着让人看见，也不要它静静地跑着但一条都发不出去。

签名校验
--------
飞书群机器人如果开了"签名校验"，需要 `timestamp` + `sign`。
Secret 里可选 `secret` 字段，配了就自动带签名，没配就按无校验发。
"""

import os
import json
import time
import hmac
import base64
import hashlib
import logging
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from prometheus_client import Counter, start_http_server

log = logging.getLogger("alert2feishu")

WEBHOOK = os.environ["FEISHU_WEBHOOK"]          # 缺了就直接起不来，见 docstring
SIGN_SECRET = os.environ.get("FEISHU_SIGN_SECRET", "").strip()
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8080"))
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9110"))
GRAFANA_BASE = os.environ.get("GRAFANA_BASE", "http://10.68.13.242:30300")

c_recv = Counter("alert2feishu_received_total", "收到的 Grafana 告警组数", ["status"])
c_sent = Counter("alert2feishu_sent_total", "成功送达飞书的条数")
c_fail = Counter("alert2feishu_delivery_failures_total", "送达失败", ["reason"])

# 颜色：firing 按 severity 分红/橙，resolved 一律绿
COLOR = {"critical": "red", "warning": "orange"}


def _sign(ts):
    """飞书签名：以 '<timestamp>\\n<secret>' 为 key 对空串做 HMAC-SHA256，再 base64。"""
    key = "%s\n%s" % (ts, SIGN_SECRET)
    digest = hmac.new(key.encode("utf-8"), b"", digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_card(payload):
    status = payload.get("status", "firing")
    alerts = payload.get("alerts") or []
    fired = [a for a in alerts if a.get("status") == "firing"] or alerts
    sev = (payload.get("commonLabels") or {}).get("severity", "warning")

    if status == "resolved":
        color, head = "green", "✅ 已恢复 · LiteLLM (198)"
    else:
        color = COLOR.get(sev, "orange")
        head = "🔴 告警 · LiteLLM (198)" if sev == "critical" else "🟠 提醒 · LiteLLM (198)"

    lines = []
    for a in fired[:15]:
        lb = a.get("labels") or {}
        an = a.get("annotations") or {}
        title = lb.get("alertname", "(无名告警)")
        summary = an.get("summary", "")
        # 谁出的问题：探针告警看 endpoint，业务告警看 requested_model
        who = lb.get("endpoint") or lb.get("requested_model") or lb.get("model_id") or ""
        bits = ["**%s**" % title]
        if who:
            bits.append("`%s`" % who)
        lines.append("• " + " · ".join(bits))
        if summary:
            lines.append("  %s" % summary)
    if len(fired) > 15:
        lines.append("_…另有 %d 条未列出_" % (len(fired) - 15))

    # 只放公共入口，不带任何个人网关/token
    lines.append("")
    lines.append("[打开看板](%s/d/litellm-stability-198)" % GRAFANA_BASE)

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"template": color,
                       "title": {"tag": "plain_text", "content": head}},
            "elements": [{"tag": "div",
                          "text": {"tag": "lark_md", "content": "\n".join(lines)}}],
        },
    }


def send(card):
    body = dict(card)
    if SIGN_SECRET:
        ts = str(int(time.time()))
        body["timestamp"] = ts
        body["sign"] = _sign(ts)
    req = urllib.request.Request(
        WEBHOOK, method="POST", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        c_fail.labels("exception").inc()
        log.error("发飞书异常: %r", e)
        return False

    # ⚠️ HTTP 200 不代表送达：飞书把业务错误放在 body 的 code 里
    try:
        j = json.loads(raw)
    except ValueError:
        j = {}
    if code == 200 and j.get("code", 0) == 0:
        c_sent.inc()
        return True
    c_fail.labels("http%s_code%s" % (code, j.get("code"))).inc()
    log.error("飞书拒收 HTTP=%s body=%s", code, raw[:300])
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        try:
            payload = json.loads(raw)
        except ValueError:
            self.send_response(400)
            self.end_headers()
            c_fail.labels("bad_json").inc()
            return
        c_recv.labels(payload.get("status", "unknown")).inc()
        ok = send(build_card(payload))
        # 回 200 而不是 5xx：Grafana 重投也救不了格式/凭据问题，只会刷屏。
        # 送不出去这件事由 alert2feishu_delivery_failures_total 自己告警。
        self.send_response(200 if ok else 502)
        self.end_headers()
        self.wfile.write(b"ok" if ok else b"feishu rejected")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"alert2feishu alive")


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    start_http_server(METRICS_PORT)
    log.info("alert2feishu 启动 listen=%d metrics=%d 签名=%s",
             LISTEN_PORT, METRICS_PORT, "开" if SIGN_SECRET else "关")
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
