#!/usr/bin/env python3
"""lane_conv_dump.py —— 事后把一条 lane 最近的网页会话整份拉出来看，**判「是不是我收割太早」**。

要回答的问题只有一个：某一发被网关判成空（`raw=""` → reroll → honest error）之后，
**那条会话里到底有没有 assistant 的正文？什么时候出现的？**

  · 有正文、且时间戳晚于我放弃的那一刻 ⇒ 是我等得不够，不是模型没回。
  · 一条 assistant 文本都没有 ⇒ 才轮到查别的。

关键实现选择：**用 lane 自己的 ChatGPTAPI 去拉，不用裸 fetch**。裸脚本打 chatgpt.com 一律被
Cloudflare 边缘挡（09-02 实测五条 lane 全 403 挑战页，含已知好的 82/84 —— 那把尺子当场作废）。
lane 的客户端重放的是整份捕获的浏览器会话（UA 从 proof token 里取、cookie jar、sentinel），
它能过，所以借它的手。

只回显 role / recipient / content_type / 时间 / 文本长度与首尾片段，**不回显 token、cookie**。

用法：
    python3 lane_conv_dump.py 81                 # 最近 8 条会话的概览
    python3 lane_conv_dump.py 81 --detail 1      # 再把第 1 条(最新)的消息逐条摊开
    python3 lane_conv_dump.py 81 --conv <id>     # 指定会话
"""
import json
import subprocess
import sys

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]

PROG = r'''
const fs = require('fs')
const CFG = JSON.parse(process.env.DUMP_CFG)
const { ChatGPTAPI } = require('/app/core/chatgpt/api.js')

// seed 的外层形状各号可能不同：递归找同时带 headers+body 的那个对象
function findFetch(o) {
  if (o && typeof o === 'object') {
    if (o.headers && o.body) return o
    for (const k of Object.keys(o)) {
      const r = findFetch(o[k])
      if (r) return r
    }
  }
  return null
}

function out(x) { console.log(JSON.stringify(x)) }

;(async () => {
  const seed = JSON.parse(fs.readFileSync('/seed/users.json', 'utf8'))
  const pf = findFetch(seed)
  if (!pf) { out({ err: 'seed 里找不到 {headers, body}' }); return }
  const api = new ChatGPTAPI()
  await api.initializeFromJSON(pf)

  async function raw(path) {
    const res = await api._fetch('https://chatgpt.com' + path, {
      method: 'GET', headers: api._buildHeaders({ accept: '*/*' }, path),
    })
    if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + (await res.text()).slice(0, 120))
    return res.json()
  }

  let convId = CFG.conv
  if (!convId || CFG.list) {
    const d = await raw('/backend-api/conversations?offset=0&limit=' + (CFG.limit || 8) + '&order=updated')
    const items = d.items || []
    out({ n_conv: items.length })
    items.forEach((c, idx) => out({ conv_row: idx + 1, id: c.id, title: (c.title || '').slice(0, 40),
                                    create: c.create_time, update: c.update_time }))
    if (!convId && CFG.detail) convId = (items[CFG.detail - 1] || {}).id
  }
  if (!convId) return

  const c = await raw('/backend-api/conversation/' + convId)
  const mp = c.mapping || {}
  const msgs = Object.values(mp).map((n) => n && n.message).filter(Boolean)
  msgs.sort((a, b) => (a.create_time || 0) - (b.create_time || 0))
  out({ conv: convId, title: (c.title || '').slice(0, 60), n_msg: msgs.length })
  for (const m of msgs) {
    const parts = (m.content && m.content.parts) || []
    const txt = parts.filter((p) => typeof p === 'string').join('')
    out({
      msg: 1,
      role: (m.author && m.author.role) || '',
      recipient: m.recipient || (m.author && m.author.recipient) || 'all',
      ctype: (m.content && m.content.content_type) || '',
      model: (m.metadata && m.metadata.model_slug) || '',
      t: m.create_time || null,
      len: txt.length,
      head: txt.slice(0, 90),
      tail: txt.length > 90 ? txt.slice(-40) : '',
    })
  }
})().catch((e) => out({ err: String(e).slice(0, 300) }))
'''


def main():
    argv = sys.argv[1:]

    def opt(name, default=None):
        if name in argv:
            i = argv.index(name)
            v = argv[i + 1]
            del argv[i:i + 2]
            return v
        return default

    conv = opt("--conv")
    detail = int(opt("--detail", "0"))
    limit = int(opt("--limit", "8"))
    lanes = [a for a in argv if not a.startswith("--")] or ["81"]
    cfg = {"conv": conv, "detail": detail, "limit": limit, "list": not conv}

    import time

    def ts(v, fmt="%m-%d %H:%M:%S"):
        """会话列表的 update_time 是 ISO 字符串，消息里的 create_time 是 epoch 浮点 ——
        两种都吃，不要因为格式不同炸在打印上。"""
        if v is None:
            return "?"
        if isinstance(v, (int, float)):
            return time.strftime(fmt, time.localtime(v))
        return str(v)[:19].replace("T", " ")

    for lane in lanes:
        print("== lane %s ==  (本地此刻 %s)" % (lane, time.strftime("%H:%M:%S")))
        r = subprocess.run(
            SSH + ["sudo -n kubectl -n %s exec -i deploy/zero-cursor-bpi-%s -- env DUMP_CFG=%s node -"
                   % (NS, lane, json.dumps(json.dumps(cfg)))],
            input=PROG, capture_output=True, text=True, timeout=300)
        got = False
        for ln in r.stdout.splitlines():
            if not ln.startswith("{"):
                continue
            d = json.loads(ln)
            got = True
            if "err" in d:
                print("  ❌ %s" % d["err"])
            elif "conv_row" in d:
                print("  #%-2d %s  %-40s upd=%s"
                      % (d["conv_row"], d["id"], d["title"], ts(d.get("update"))))
            elif "conv" in d:
                print("  ---- 会话 %s  %r  共 %d 条消息 ----"
                      % (d["conv"], d["title"], d["n_msg"]))
            elif "msg" in d:
                print("    %-9s recipient=%-10s %-14s %-16s %s len=%-5d %s%s"
                      % (d["role"], d["recipient"], d["ctype"], d["model"] or "-",
                         ts(d.get("t"), "%H:%M:%S"),
                         d["len"], repr(d["head"]),
                         (" …" + repr(d["tail"])) if d["tail"] else ""))
        if not got:
            print("  ❌ 没有输出:\n  %s\n  %s" % (r.stdout[-300:], r.stderr[-300:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
