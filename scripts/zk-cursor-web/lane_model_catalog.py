#!/usr/bin/env python3
"""lane_model_catalog.py —— 逐条 lane 问它自己的账号：**你手里到底有哪些模型 slug**。

为什么需要它：`raw.js` 顶部那张 `WEB_MODELS` 表是**写死在代码里**的，六条 lane 共用同一份
CM ⇒ 它对每条 lane 都一模一样，**解释不了任何 lane 间差异**（记忆
feedback_hardcoded_log_string_reports_stale_truth 那一族）。档位/型号的权威源是账号自己的
`/backend-api/models`（reference_chatgpt_web_model_catalog_tiers_2026_09_01）。

用法：
    python3 lane_model_catalog.py                # 默认 81 83 84 85
    python3 lane_model_catalog.py 82 83          # 指定
    python3 lane_model_catalog.py --grep pro     # 只看含某字样的 slug

凭据纪律：bearer **只在 pod 内部流转** —— 程序整份走 ssh stdin 喂给 pod 里的 python3，
token 不过 argv、不回本地、不打印，只回显 `bearer_len`。
"""
import subprocess
import sys

NS = "litellm-product"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=20", "cltx@10.68.13.198"]
DEFAULT_LANES = ["81", "83", "84", "85"]

# 在 pod 内跑：**借 lane 自己的 ChatGPTAPI** 去问 chatgpt.com 的模型目录。
#
# 09-02 踩过的坑，别回去：第一版用裸 `fetch` + seed 里的 bearer，**五条 lane 全 403**
# ——包括已知好的 82/84，返回的是 Cloudflare 边缘挑战页 HTML。阳性对照没复现已知答案 ⇒
# 那版尺子当场作废。lane 的客户端重放的是整份捕获的浏览器会话（UA 从 proof token 里取、
# cookie jar、sentinel proof），它能过；所以借它的手，不要自己拼 header。
#
# **必须用 node 不能用 python3**：lane 镜像是 node 基底
# （`exec: "python3": executable file not found in $PATH`）。
PROG = r'''
const fs = require('fs')
const { ChatGPTAPI } = require('/app/core/chatgpt/api.js')

function findFetch(o) {
  if (o && typeof o === 'object') {
    if (o.headers && o.body) return o
    for (const k of Object.keys(o)) { const r = findFetch(o[k]); if (r) return r }
  }
  return null
}
function out(x) { console.log(JSON.stringify(x)) }

;(async () => {
  const pf = findFetch(JSON.parse(fs.readFileSync('/seed/users.json', 'utf8')))
  if (!pf) { out({ err: 'seed 里找不到 {headers, body}' }); return }
  const api = new ChatGPTAPI()
  await api.initializeFromJSON(pf)
  async function raw(path) {
    const res = await api._fetch('https://chatgpt.com' + path, {
      method: 'GET', headers: api._buildHeaders({ accept: '*/*' }, path),
    })
    const t = await res.text()
    if (!res.ok) throw new Error('HTTP ' + res.status + ' ' + t.slice(0, 120))
    return JSON.parse(t)
  }
  const o = {}
  try {
    const d = await raw('/backend-api/models?history_and_training_disabled=false')
    const ms = d.models || []
    o.n = ms.length
    o.slugs = ms.map((m) => m.slug)
    o.tiers = {}
    for (const m of ms) {
      const t = m.supported_reasoning_efforts
      if (t && t.length) o.tiers[m.slug] = t.map((x) => (typeof x === 'string' ? x : x.id))
    }
  } catch (e) { o.err = String(e).slice(0, 200) }
  try {
    const a = await raw('/backend-api/accounts/check/v4')
    const accs = (a && a.accounts) || {}
    const k = Object.keys(accs)[0]
    const plan = k && accs[k] && accs[k].account && accs[k].account.plan_type
    o.plan = plan || null
  } catch (e) { o.plan_err = String(e).slice(0, 120) }
  out(o)
})().catch((e) => out({ err: String(e).slice(0, 300) }))
'''


def main():
    argv = sys.argv[1:]
    grep = None
    if "--grep" in argv:
        i = argv.index("--grep")
        grep = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]            # 值也要吃掉，否则它会被当成一条 lane（踩过）
    lanes = [a for a in argv if not a.startswith("--")] or DEFAULT_LANES
    import json
    cat = {}
    for lane in lanes:
        r = subprocess.run(
            SSH + ["sudo -n kubectl -n %s exec -i deploy/zero-cursor-bpi-%s -- node -" % (NS, lane)],
            input=PROG, capture_output=True, text=True, timeout=180)
        line = [l for l in r.stdout.splitlines() if l.startswith("{")]
        if not line:
            print("lane %-4s ❌ 没拿到（%s）" % (lane, (r.stderr or r.stdout)[-160:]))
            continue
        d = json.loads(line[-1])
        if "err" in d:
            print("lane %-4s ❌ %s (bearer_len=%s)" % (lane, d["err"], d.get("bearer_len")))
            continue
        slugs = d.get("slugs") or []
        cat[lane] = set(slugs)
        show = [s for s in slugs if not grep or grep in (s or "")]
        print("lane %-4s plan=%-10s slug 共 %d 个%s"
              % (lane, d.get("plan") or d.get("plan_err") or "?", d.get("n", 0),
                 ("，含 %r 的: %s" % (grep, ", ".join(show) or "(无)")) if grep else ""))
        if not grep:
            print("    " + ", ".join(sorted(s for s in slugs if s)))
        if d.get("tiers"):
            print("    有档位的: " + "; ".join("%s=%s" % (k, ",".join(v))
                                             for k, v in sorted(d["tiers"].items())))

    # lane 间差集 —— 这才是"为什么这条腿行那条腿不行"的直接判据
    if len(cat) > 1:
        common = set.intersection(*cat.values())
        print("\n== lane 间差集（共有 %d 个 slug）==" % len(common))
        for lane in sorted(cat):
            only = cat[lane] - common
            print("  lane %-4s 独有 %d 个%s" % (lane, len(only),
                                              ("：" + ", ".join(sorted(only))) if only else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
