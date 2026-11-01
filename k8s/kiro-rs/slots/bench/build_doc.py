"""Build the replacement DocxXML for the 36-exit ranking doc (31 iGuge + 5 self-built).

  python3 build_doc.py rank36.json > doc-new.xml

Keeps the original doc's four tables but over 36 exits, adds a 来源 column to each, and states which
下载 figures are measured vs. an aliyun EIP cap. Live slot state (生效槽 N / 停用) comes from the
slots config snapshot so the doc and 30891 cannot drift.
"""
import html
import json
import re
import sys

SRC_LABEL = {"iguge": "iGuge", "self-243": "自建·243", "self-aliyun-squid": "自建·阿里云"}


def esc(s):
    return html.escape(str(s), quote=False)


def cells(vals, tag="td"):
    return "".join("<%s vertical-align=\"top\"><p>%s</p></%s>" % (tag, esc(v), tag) for v in vals)


def table(header, rows):
    out = ["<table><thead><tr>"]
    out.append("".join("<th vertical-align=\"top\" background-color=\"light-gray\"><p>%s</p></th>" % esc(h)
                       for h in header))
    out.append("</tr></thead><tbody>")
    for r in rows:
        out.append("<tr>" + cells(r) + "</tr>")
    out.append("</tbody></table>")
    return "".join(out)


def main():
    rows = json.load(open(sys.argv[1]))
    slots = json.load(open(sys.argv[2]))["slots"]
    # live slot state by exit IP: 生效槽号（只数 enabled，和页面卡片一致）或「停用」
    state = {}
    card = 0
    for s in slots:
        if s.get("enabled"):
            card += 1
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)$", s["name"])
        if m:
            state[m.group(1)] = ("槽 %d" % card) if s.get("enabled") else "停用"

    def st(ip):
        return state.get(ip, "不是槽")

    kb = min(r["kiroSecs"] for r in rows)
    kw = max(r["kiroSecs"] for r in rows)
    db = max(r["downMbps"] for r in rows)
    dw = min(r["downMbps"] for r in rows)
    n = len(rows)
    n_cap = sum(1 for r in rows if r["down_is_cap"])

    p = []
    p.append("<h1>出口节点排名：iGuge + 公司自建（排除香港）</h1>")
    p.append("<p>共 <b>%d 个</b>出口：iGuge 机场 %d 个、公司自建 %d 个。"
             "自建 = 阿里云 EIP（squid）4 个 + 日本 243（astrill）1 个。</p>"
             % (n, sum(1 for r in rows if r["source"] == "iguge"),
                sum(1 for r in rows if r["source"] != "iguge")))
    p.append("<p>「连 Kiro」= 经该出口访问 Kiro 接口的耗时中位数（18 次），全部实测。"
             "「下载」= 4 次 10MB 下载的速度中位数；<b>阿里云那 4 个是 EIP 带宽上限（控制台标注），不是实测</b>"
             "—— 它们的 squid 白名单只放了 Kiro 接口和 api.ipify.org，任何下载源都连不通，"
             "表里带「上限」二字的就是这种。198 直连（日本槽）对照约 0.61 秒。</p>")
    p.append("<callout emoji=\"⚠️\" background-color=\"light-yellow\"><p>"
             "口径变了：上一版是 31 个 iGuge 节点内部归一化，这一版把 5 个自建出口并进来一起算，"
             "最好/最差两端都变了 ⇒ <b>所有排名和综合分都已重算，旧的「第 N/31」不能再用</b>。</p></callout>")
    p.append("<h2>综合排名（分数从高到低）</h2>")
    p.append("<p>综合分 = 连 Kiro 得分 × 60%% + 下载得分 × 40%%。两项各自按这 %d 个出口里的最好/最差换算成 0~100 分"
             "（连 Kiro 最快 %.2f 秒 = 100 分、最慢 %.2f 秒 = 0 分；下载最快 %.0f Mbps = 100 分、"
             "最慢 %.0f Mbps = 0 分）。连 Kiro 权重高，因为 kiro 槽主要看它。</p>" % (n, kb, kw, db, dw))

    hdr = ["排名", "节点", "来源", "出口 IP", "综合分", "现状", "连 Kiro", "下载"]
    p.append(table(hdr, [[r["rank"], r["node"], SRC_LABEL[r["source"]], r["ip"], "%.1f" % r["score"],
                          st(r["ip"]), "%.2f 秒" % r["kiroSecs"],
                          "%.0f Mbps%s" % (r["downMbps"], "（上限）" if r["down_is_cap"] else "")]
                         for r in rows]))

    p.append("<h2>按连 Kiro 速度排序（越小越快）</h2>")
    p.append("<p>这一项全部是实测，自建和 iGuge 可以直接比。</p>")
    by_k = sorted(rows, key=lambda r: r["kiroSecs"])
    p.append(table(["#", "节点", "来源", "出口 IP", "连 Kiro", "现状"],
                   [[i, r["node"], SRC_LABEL[r["source"]], r["ip"], "%.2f 秒" % r["kiroSecs"], st(r["ip"])]
                    for i, r in enumerate(by_k, 1)]))

    p.append("<h2>按下载速度排序（越大越快）</h2>")
    p.append("<p>🔴 这一项不同源不可直接比：iGuge 和 243 是实测带宽，阿里云 4 个是 EIP 上限"
             "（标「上限」），实际跑到多少没测。</p>")
    by_d = sorted(rows, key=lambda r: -r["downMbps"])
    p.append(table(["#", "节点", "来源", "出口 IP", "下载", "是否实测", "现状"],
                   [[i, r["node"], SRC_LABEL[r["source"]], r["ip"], "%.0f Mbps" % r["downMbps"],
                     "EIP 上限" if r["down_is_cap"] else "实测", st(r["ip"])]
                    for i, r in enumerate(by_d, 1)]))

    good = [r for r in rows if r["kiroSecs"] < 0.8 and r["downMbps"] > 50]
    p.append("<h2>两项都好的（连 Kiro &lt; 0.8 秒 且 下载 &gt; 50 Mbps）</h2>")
    if good:
        p.append(table(["节点", "来源", "出口 IP", "连 Kiro", "下载", "现状"],
                       [[r["node"], SRC_LABEL[r["source"]], r["ip"], "%.2f 秒" % r["kiroSecs"],
                         "%.0f Mbps%s" % (r["downMbps"], "（上限）" if r["down_is_cap"] else ""), st(r["ip"])]
                        for r in sorted(good, key=lambda r: r["kiroSecs"])]))
    else:
        p.append("<p>没有。</p>")

    enabled_now = [r for r in rows if st(r["ip"]).startswith("槽")]
    p.append("<h2>现在在带量的出口</h2>")
    p.append("<p>共 %d 个生效槽。排名前 6 的出口<b>目前一个都没启用</b>；"
             "在带量的里最靠后的是第 %d 名。启用/停用在 30891 设置页改，这篇只报现量。</p>"
             % (len(enabled_now), max(r["rank"] for r in enabled_now) if enabled_now else 0))
    p.append(table(["现状", "排名", "节点", "来源", "出口 IP", "综合分", "连 Kiro", "下载"],
                   [[st(r["ip"]), r["rank"], r["node"], SRC_LABEL[r["source"]], r["ip"],
                     "%.1f" % r["score"], "%.2f 秒" % r["kiroSecs"],
                     "%.0f Mbps%s" % (r["downMbps"], "（上限）" if r["down_is_cap"] else "")]
                    for r in sorted(enabled_now, key=lambda r: r["rank"])]))

    p.append("<p>说明：iGuge 节点是共享的机场节点，「两天没变」只代表测试期间没变，不是厂商承诺；"
             "自建出口的 IP 是我们自己的，不会变。%d 个出口的下载是 EIP 上限而非实测。"
             "数据同时写进了 30891 槽位配置（设置页「测速」列），两边同源。</p>" % n_cap)

    sys.stdout.write("".join(p))
    print("# %d rows, %d enabled, %d capped" % (n, len(enabled_now), n_cap), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
